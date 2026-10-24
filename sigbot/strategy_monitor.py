"""Strategy monitor (Sizhe 24 Oct): test every live sleeve on its own real-time results and switch a sleeve's NEW entries
off once it is statistically clear that it loses money; only a human switches it back on.

    python3 -m sigbot.strategy_monitor              # read-only report on data/sigbot.sqlite (writes nothing)
    python3 -m sigbot.strategy_monitor --simulate   # false-alarm rate and detection delay of the rule on synthetic data

Per-trade metric of each sleeve (all reconstructed read-only from the DB, the sleeves' ledgers and the bot's log):
  mm        30-min markout of each MM fill in ticks (mid 30 min later minus our price, signed in our favour), fills on
            one market within 30 min of the first merged into one share-weighted observation (they share the same later
            mids, so they are not independent)
  catchup   realised P&L per closed catch-up trade (flat -> position -> flat on one market), ticks per share
  hf_arb    "high-frequency arb" (code: dev_churn): realised P&L per set of each exit from the log ("CHURN exit", partial
            ones too, and "no longer held ... booked as an exit" when another path closed ledger sets), plus each open lot
            in params/churn_state.json older than 6 h at what selling it back at the current asks would fetch, ticks per
            set (a younger lot would only show the round-trip spread as a loss)
  recycle   per recycle: locked profit gained by the follow-through baskets (other races, within 60 min, capped at the
            capital the unwind freed: cash mode may spend the freed credit later) minus the locked profit the unwind gave up, in % of the capital freed; a burst of
            unwinds of one race within 30 min of the first is one observation (capital-weighted)
  cheapfav  cheap favourites (settle 4 Nov): per open leg the outside fair now minus our average price (ticks per share,
            signed for the side we hold, i.e. the leg's P&L at the outside fair), plus closed legs at their realised exit

The rule: a one-sided normal-mixture sequential probability ratio test (Robbins 1970; the "mSPRT" of Johari et al.
2017). With n observations, sum S and standard deviation s (plug-in Newey-West long-run sd over 5 lags, never below the
plain sd, floored; observations clipped at a generous bound),
the evidence that the mean is not 0 is the likelihood ratio, mixed over a N(0, (rho*s)^2) prior on the mean,

    L_n = (1 + n rho^2) ** -0.5 * exp(rho^2 S^2 / (2 s^2 (1 + n rho^2)))

Under a zero-edge strategy L_n is a nonnegative martingale (known variance), so by Ville's inequality
P(L_n >= 1/alpha at ANY n) <= alpha: re-checking every 15 min until 4 Nov does not inflate false alarms the way a
t-test re-run every hour does. Kill when n >= 30, the mean is negative and L_n >= 100 (alpha 1%); warn at L_n >= 10.
A strategy with positive edge is even less likely to be killed. `--simulate` checks the plug-in variance, heavy tails
and autocorrelation do not break the 1% (research_notes/strategy_monitor.md).

Cheap favourites hold to settlement, so its observations are a cross-section of open legs that all move with the
national swing, not a sequence of independent trades: it is flagged only on clear deterioration (mean <= -2 ticks,
L_n >= 1000, mark P&L negative, >= 8 legs).

Action: kills are added to params/strategy_kill.json ({name: {ts, utc, reason, n, mean, stat}}), written atomically and
never removed by the bot. Each sleeve checks the file before NEW entries (exits continue). A human re-enables a sleeve by
deleting its entry; the monitor notices and restarts that sleeve's test from that moment (params/strategy_monitor_state.json),
so the old evidence cannot re-kill it at once. The global kill switch and hard limits are never touched.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK = 0.005

ALPHA_KILL = 0.01               # P(a zero-edge sleeve is ever killed before 4 Nov) <= 1%
ALPHA_WARN = 0.10
N_MIN = 30                      # no verdict below 30 observations (20 let dependent series false-alarm ~2%: --simulate)
RHO = 0.3                       # prior sd of the mean, in units of the observation sd (tuned in --simulate)
MARKOUT_SEC = 1800.0
CLUSTER_SEC = 1800.0            # MM fills on one market within 30 min of the first share the same later mids: one observation
NW_LAGS = 5                     # Newey-West (Bartlett) lags for the long-run variance of the time-ordered observations
AR1_MAX = 0.8                   # cap on the lag-1 autocorrelation used for the AR(1) variance factor (1 + r) / (1 - r)
FOLLOW_SEC = 3600.0             # follow-through baskets counted for a recycle unwind (cash mode spends leftover recycle
                                # credit later; capped at the capital the unwind freed). Assumes cash mode (arb.new_sets
                                # False, live since 23 Oct): with new sets on, ordinary baskets would be credited too
LOG_MATCH_SEC = 15.0            # an arb_trades row and its log line are at most this far apart
QUOTE_MAX_AGE = 1800.0
OPEN_LOT_MIN_AGE = 6 * 3600.0   # a high-frequency arb lot counts at its exit value only once this old: a fresh lot always
                                # shows the round-trip spread as a loss (gaps revert with a 2-5 h half-life, 24 h max hold)

SPECS = {
    "mm": {"label": "market making", "unit": "ticks per fill (30-min markout)", "clip": 15.0, "sd_floor": 0.5},
    "catchup": {"label": "catch-up sweep", "unit": "ticks per share per closed trade", "clip": 30.0, "sd_floor": 1.0},
    "hf_arb": {"label": "high-frequency arb", "unit": "ticks per set per round trip or open lot", "clip": 30.0, "sd_floor": 1.0},
    "recycle": {"label": "recycles", "unit": "% of freed capital per recycle", "clip": 20.0, "sd_floor": 0.2,
                "alpha_kill": 0.002, "n_min": 40},   # bursts stay dependent (lag-1 0.49): block bootstrap of the live series,
                                                       # 120 bursts: 0.7% false pauses (block 4), 2.9% (block 8); 1% rule gave 2-5%
    "cheapfav": {"label": "cheap favourites", "unit": "ticks per share vs outside fair per leg", "clip": 50.0, "sd_floor": 1.0},
}
CHEAPFAV_MIN_LEGS = 8
CHEAPFAV_ALPHA = 0.001
CHEAPFAV_MIN_LOSS = 2.0         # ticks per share

KILL_FILE = "strategy_kill.json"
STATE_FILE = "strategy_monitor_state.json"


# ---------------------------------------------------------------------------------------------------------------- rule
def evidence(xs: list, sd_floor: float, clip: Optional[float] = None, rho: float = RHO) -> dict:
    """Mixture likelihood ratio that the mean of xs is not 0 (see module doc). Returns n, mean, sd and L."""
    if clip is not None:
        xs = [max(-clip, min(clip, x)) for x in xs]
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "L": 1.0}
    s = sum(xs)
    ss = sum(x * x for x in xs)
    lag = [sum(xs[t] * xs[t - k] for t in range(k, n)) for k in range(1, NW_LAGS + 1)]
    var = long_run_var(n, s, ss, lag)
    sd = math.sqrt(max(0.0, (ss - s * s / n) / (n - 1))) if n > 1 else 0.0
    return {"n": n, "mean": s / n, "sd": sd, "lrsd": math.sqrt(var), "L": mixture_lr(n, s, max(var, sd_floor ** 2), rho)}


def long_run_var(n: int, s: float, ss: float, lag_sums: list) -> float:
    """Variance of one observation for the test: the sample variance, raised (never lowered) to the larger of the
    Newey-West long-run variance (Bartlett weights, NW_LAGS lags) and the AR(1) long-run variance from the lag-1
    autocorrelation, so autocorrelated results (bursts, a shared market move) do not look more significant than they
    are. lag_sums[k-1] = sum over t of x_t * x_(t-k)."""
    if n < 2:
        return 0.0
    m = s / n
    g0 = (ss - s * m) / (n - 1)
    if g0 <= 0:
        return 0.0
    lrv = g0
    L = min(NW_LAGS, n - 1, len(lag_sums))
    for k in range(1, L + 1):
        lrv += 2.0 * (1.0 - k / (L + 1.0)) * (lag_sums[k - 1] - (n - k) * m * m) / n
    r1 = min(AR1_MAX, max(0.0, (lag_sums[0] - (n - 1) * m * m) / n / g0)) if lag_sums else 0.0
    return max(g0, lrv, g0 * (1.0 + r1) / (1.0 - r1))   # AR(1) long-run factor: Newey-West alone under-corrects at small n


def mixture_lr(n: int, s: float, var: float, rho: float = RHO) -> float:
    a = 1.0 + n * rho * rho
    lg = -0.5 * math.log(a) + rho * rho * s * s / (2.0 * var * a)
    return math.exp(min(lg, 700.0))


def verdict(ev: dict, n_min: int = N_MIN, alpha_kill: float = ALPHA_KILL, alpha_warn: float = ALPHA_WARN,
            min_loss: float = 0.0) -> tuple:
    n, mean, L = ev["n"], ev["mean"], ev["L"]
    if n < n_min:
        return "ok", f"n={n} < {n_min}: not tested yet"
    if mean >= 0:
        return "ok", f"mean {mean:+.2f} >= 0"
    if L >= 1.0 / alpha_kill and mean <= -min_loss:
        return "kill", f"losing: mean {mean:+.2f} over n={n}, evidence {L:.3g} >= {1 / alpha_kill:.0f}"
    if L >= 1.0 / alpha_warn:
        return "warn", f"mean {mean:+.2f} over n={n}, evidence {L:.1f} (kill at {1 / alpha_kill:.0f})"
    return "ok", f"mean {mean:+.2f} over n={n}, evidence {L:.1f}: not significant"


# ------------------------------------------------------------------------------------------------------------- helpers
def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def normalise_kills(d) -> Optional[dict]:
    """{name: entry dict} from the kill file; None when the file content is not a JSON object. An entry may be written
    by hand as any value: false / null / 0 / "" mean not paused; anything else pauses, a non-dict becomes its reason."""
    if not isinstance(d, dict):
        return None
    out = {}
    for name, k in d.items():
        if k is None or k is False or k == 0 or k == "":
            continue
        out[str(name)] = k if isinstance(k, dict) else {"reason": str(k)}
    return out


def _write_json(path: str, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


_LOG_RES = {
    # entries may be partial: "x<booked> of <requested> (...)"; the arb_trades row carries the requested quantity
    "churn_enter": re.compile(r"CHURN enter (.+?) x(\d+)(?: of (\d+) \(.*?\))? at cost ([\d.]+)/set"),
    # exits may be partial: "x<sold> of <held>"
    "churn_exit": re.compile(r"CHURN exit (.+?) x(\d+)(?: of \d+)? at ([\d.]+)/set \((.*?)\): ([+-]?[\d.]+)"),
    # ledger sets closed by another path (a recycle, free unwind, manual trade) are booked as an exit too
    "churn_reconcile": re.compile(r"CHURN (.+?): (\d+) sets no longer held \(closed by another path\), booked as an exit at "
                                  r"([\d.]+)/set \((.*?)\): ([+-]?[\d.]+)"),
    "unwind": re.compile(r"UNWIND \((.*?)\) (.+?): (\d+) sets at proceeds"),
    "free": re.compile(r"FREE UNWIND (.+?): (\d+) sets"),
    "recycle": re.compile(r"RECYCLE (.+?): unwind (\d+) sets"),
}
_log_cache: dict = {}


def log_events(path: str) -> list:
    """Sleeve events from the bot's log (local-time stamps, as logging writes them), read incrementally."""
    c = _log_cache.get(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if c is None or size < c["off"]:
        c = _log_cache[path] = {"off": 0, "ev": []}
    with open(path, "rb") as f:
        f.seek(c["off"])
        chunk = f.read()
    end = chunk.rfind(b"\n") + 1                     # keep a half-written last line for the next read
    c["off"] += end
    for raw in chunk[:end].decode("utf-8", "replace").splitlines():
        if "CHURN" not in raw and "UNWIND" not in raw and "RECYCLE" not in raw:   # cheap pre-filter
            continue
        try:
            t = time.mktime(time.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")) + int(raw[20:23]) / 1000.0
        except ValueError:
            continue
        for kind, rx in _LOG_RES.items():
            m = rx.search(raw)
            if not m:
                continue
            g = m.groups()
            if kind == "churn_enter":
                e = {"race": g[0], "qty": int(g[1]), "qty_requested": int(g[2]) if g[2] else int(g[1]), "cost": float(g[3])}
            elif kind in ("churn_exit", "churn_reconcile"):
                e = {"race": g[0], "qty": int(g[1]), "proceeds": float(g[2]), "why": g[3], "pnl": float(g[4])}
                kind = "churn_exit"
            elif kind == "unwind":
                e = {"why": g[0], "race": g[1], "qty": int(g[2])}
                kind = "churn_unwind" if g[0].startswith("churn") else ("offload" if g[0].startswith("fund") else "other_unwind")
            else:
                e = {"race": g[0], "qty": int(g[1])}
            e.update(kind=kind, ts=t)
            c["ev"].append(e)
            break
    return c["ev"]


def _match(events: list, kinds: tuple, race: str, qty: int, ts: float) -> Optional[dict]:
    best = None
    for e in events:
        if (e["kind"] in kinds and e["race"] == race and qty in (e["qty"], e.get("qty_requested"))
                and abs(e["ts"] - ts) <= LOG_MATCH_SEC):
            if best is None or abs(e["ts"] - ts) < abs(best["ts"] - ts):
                best = e
    return best


def _sig_asks(store, race: str, now: float) -> dict:
    rows = store.rows("SELECT party, ask, MAX(ts) t FROM venue_quotes WHERE venue='sig' AND race=? AND ts<=? AND ts>? GROUP BY party",
                      (race, now, now - QUOTE_MAX_AGE))
    return {r["party"]: r["ask"] for r in rows if r["ask"] is not None}


def _fair_row(store, ex: str, now: float):
    return store.one("SELECT fair, sig_mid FROM venue_fair WHERE exchange_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1",
                     (str(ex), now, now - QUOTE_MAX_AGE))


# ------------------------------------------------------------------------------------------------------------- metrics
_markout_cache: dict = {}


def mm_observations(store, now: float, since: float = 0.0) -> list:
    """30-min markouts of MM fills (ticks), merged per market over 30 min from its first fill (share-weighted), in time order."""
    raw = []
    for f in store.rows("SELECT id, exchange_id, side, price, quantity, filled_at FROM fills WHERE purpose='mm' AND price IS NOT NULL "
                        "AND filled_at>=? ORDER BY filled_at", (_iso(since),)):
        t = _ts(f["filled_at"])
        if now - t < MARKOUT_SEC or t < since:
            continue
        key = (getattr(store, "path", None), f["id"], f["filled_at"], f["exchange_id"], f["price"])
        e = _markout_cache.get(key)
        if e is None:
            q = store.one("SELECT mid FROM quotes WHERE exchange_id=? AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT 1",
                          (f["exchange_id"], t + MARKOUT_SEC, t + MARKOUT_SEC + 3600))
            ref = q["mid"] if q and q["mid"] is not None else None
            if ref is None:                            # no quote snapshot: first public print after t + 30 min
                tr = store.one("SELECT price FROM tape WHERE exchange_id=? AND executed_at>=? AND executed_at<=? ORDER BY executed_at ASC LIMIT 1",
                               (f["exchange_id"], _iso(t + MARKOUT_SEC), _iso(t + MARKOUT_SEC + 3600)))
                ref = tr["price"] if tr else None
            if ref is None:
                continue
            e = ((ref - f["price"]) if f["side"] == "yes" else (f["price"] - ref)) / TICK
            _markout_cache[key] = e
        raw.append((str(f["exchange_id"]), f["side"], t, e, abs(float(f["quantity"]))))
    out, cur = [], None
    for ex, side, t, e, q in sorted(raw, key=lambda r: (r[0], r[2])):
        if cur and cur[0] == ex and t - cur[1] <= CLUSTER_SEC:
            cur[2].append((e, q))
        else:
            if cur:
                out.append(cur)
            cur = [ex, t, [(e, q)]]
    if cur:
        out.append(cur)
    out.sort(key=lambda c: c[1])                       # time order (the long-run variance looks at neighbours)
    return [sum(e * max(q, 1e-9) for e, q in c[2]) / sum(max(q, 1e-9) for _, q in c[2]) for c in out]


def catchup_observations(store, now: float, since: float = 0.0) -> tuple:
    """Closed catch-up trades: ticks per share. Also the open positions marked at the latest SIG mid (reported only)."""
    fills = store.rows("SELECT exchange_id, quantity, price, filled_at FROM fills WHERE purpose IN ('catchup','catchup_exit') "
                       "AND price IS NOT NULL ORDER BY filled_at")
    st, obs = {}, []
    for f in fills:
        ex, q, p, t = str(f["exchange_id"]), float(f["quantity"]), float(f["price"]), _ts(f["filled_at"])
        s = st.setdefault(ex, {"pos": 0.0, "cash": 0.0, "peak": 0.0, "t0": t, "last": p})
        if abs(s["pos"]) < 1:
            s.update(cash=0.0, peak=0.0, t0=t)
        s["pos"] += q
        s["cash"] -= q * p
        s["last"] = p
        s["peak"] = max(s["peak"], abs(s["pos"]))
        if abs(s["pos"]) < 1 and s["peak"] >= 1:
            if s["t0"] >= since:
                obs.append(s["cash"] / s["peak"] / TICK)
            s.update(pos=0.0, cash=0.0, peak=0.0)
    open_pnl, n_open = 0.0, 0
    for ex, s in st.items():
        if abs(s["pos"]) >= 1:
            r = _fair_row(store, ex, now)
            mark = r["sig_mid"] if r is not None and r["sig_mid"] is not None else s["last"]
            open_pnl += s["cash"] + s["pos"] * mark
            n_open += 1
    return obs, {"open_positions": n_open, "open_pnl": round(open_pnl, 1)}


def hf_arb_observations(store, now: float, events: list, ledger: dict, since: float = 0.0) -> tuple:
    """Realised: one observation per round trip of a race (its entries until the sleeve holds none there again), P&L per
    set over the whole round trip, quantity-weighted, so a partial exit or a small reconcile (an MM fill on a leg) is not
    a full observation. Exit P&L = sets x (proceeds - FIFO entry cost) from the entry lines (exact, not the log's rounded
    P&L; the logged P&L is used only for sets with no entry line, e.g. before the log starts). Open lots: see below."""
    obs, realised = [], 0.0
    fifo: dict = {}                                    # race -> [[qty, cost, ts]]
    trip: dict = {}                                    # race -> {"pnl", "qty", "t0"}
    # in log-file order, never re-sorted by the parsed local time: in the repeated hour of a clock change (25 Oct, BST ->
    # GMT) the parsed times collide and a sort could put an exit before its own entry (review 24 Oct)
    for e in (e for e in events if e["kind"] in ("churn_enter", "churn_exit")):
        race = e["race"]
        if e["kind"] == "churn_enter":
            if e["qty"] > 0:
                fifo.setdefault(race, []).append([e["qty"], e["cost"], e["ts"]])
                trip.setdefault(race, {"pnl": 0.0, "qty": 0, "t0": e["ts"]})
            continue
        if e["qty"] <= 0:
            continue
        left, pnl = e["qty"], 0.0
        q = fifo.get(race, [])
        while left > 0 and q:
            take = min(left, q[0][0])
            pnl += take * (e["proceeds"] - q[0][1])
            q[0][0] -= take
            left -= take
            if q[0][0] <= 0:
                q.pop(0)
        if left > 0:                                   # sets with no entry line: the logged (rounded) P&L, pro rata
            pnl += e["pnl"] * left / e["qty"]
        t = trip.setdefault(race, {"pnl": 0.0, "qty": 0, "t0": e["ts"]})
        t["pnl"] += pnl
        t["qty"] += e["qty"]
        realised += pnl
        if not q:                                      # nothing of this race left in the ledger: the round trip is done
            if t["t0"] >= since and t["qty"] > 0:
                obs.append(t["pnl"] / t["qty"] / TICK)
            trip.pop(race, None)
    n_trips = len(obs)
    open_val, n_open, unpriced, young = 0.0, 0, 0, 0
    asks_by_race: dict = {}
    for lot in ledger.get("lots") or []:
        if lot.get("qty", 0) <= 0 or float(lot.get("ts", 0)) < since:
            continue
        if now - float(lot.get("ts", now)) < OPEN_LOT_MIN_AGE:
            young += 1                                 # reported, not tested yet
            continue
        race = lot["race"]
        if race not in asks_by_race:
            asks_by_race[race] = _sig_asks(store, race, now)
        asks = asks_by_race[race]
        if len(asks) < 2:                              # churn trades two-leg races only
            unpriced += 1
            continue
        proceeds = len(asks) - sum(asks.values())
        obs.append((proceeds - float(lot["cost"])) / TICK)
        open_val += lot["qty"] * (proceeds - float(lot["cost"]))
        n_open += 1
    return obs, {"round_trips": n_trips, "open_lots": n_open, "young_lots": young, "unpriced_lots": unpriced, "realised": round(realised, 1),
                 "open_value": round(open_val, 1), "ledger_realised": ledger.get("realised")}


def recycle_observations(store, now: float, events: list, since: float = 0.0) -> tuple:
    rows = [dict(r) for r in store.rows("SELECT ts, race, kind, quantity, edge_per_set, cost_per_set FROM arb_trades "
                                        "WHERE ok=1 AND ts<=? ORDER BY ts", (now,))]
    unwinds, baskets, counts = [], [], {"recycle": 0, "churn": 0, "offload": 0, "free": 0, "unknown": 0}
    for r in rows:
        if r["kind"] == "unwind_set":
            m = _match(events, ("recycle", "churn_unwind", "offload", "free", "other_unwind"), r["race"], r["quantity"], r["ts"])
            # no matching log line: free if it gave nothing up, else unknown and left out (never a recycle loss by default)
            kind = m["kind"] if m else ("free" if r["edge_per_set"] >= -1e-9 else "unknown")
            kind = {"churn_unwind": "churn", "other_unwind": "unknown"}.get(kind, kind)
            counts[kind] += 1
            if kind == "recycle" and r["ts"] >= since:
                unwinds.append(dict(r, freed=r["quantity"] * r["cost_per_set"], left=r["quantity"] * r["cost_per_set"], gained=0.0))
        elif r["kind"] in ("sell_set", "buy_set"):
            if _match(events, ("churn_enter",), r["race"], r["quantity"], r["ts"]):
                continue                               # a high-frequency arb entry, not a recycle follow-through
            baskets.append(r)
    for b in baskets:                                  # each basket funds the oldest open recycle before it (other race)
        spend = b["quantity"] * b["cost_per_set"]
        for u in unwinds:
            if spend <= 1e-9:
                break
            if u["race"] == b["race"] or not (u["ts"] < b["ts"] <= u["ts"] + FOLLOW_SEC) or u["left"] <= 1e-9:
                continue
            take = min(spend, u["left"])
            u["gained"] += b["quantity"] * b["edge_per_set"] * take / (b["quantity"] * b["cost_per_set"])
            u["left"] -= take
            spend -= take
    obs, cur = [], None                                # a burst (same unwound race within 30 min of the first) is one decision
    for u in unwinds:
        if now - u["ts"] < FOLLOW_SEC or u["freed"] <= 0:
            continue                                   # follow-through window still open
        net = u["gained"] + u["quantity"] * u["edge_per_set"]
        if cur and cur["race"] == u["race"] and u["ts"] - cur["ts"] <= CLUSTER_SEC:
            cur["net"] += net
            cur["freed"] += u["freed"]
        else:
            if cur:
                obs.append(100.0 * cur["net"] / cur["freed"])
            cur = {"race": u["race"], "ts": u["ts"], "net": net, "freed": u["freed"]}
    if cur:
        obs.append(100.0 * cur["net"] / cur["freed"])
    return obs, {"unwinds_by_kind": counts, "recycle_unwinds": len(unwinds)}


def cheapfav_observations(store, now: float, since: float = 0.0) -> tuple:
    fills = store.rows("SELECT exchange_id, quantity, price, filled_at FROM fills WHERE purpose LIKE 'cheapfav%' AND price IS NOT NULL "
                       "AND filled_at>=? ORDER BY filled_at", (_iso(since),))
    st, closed = {}, []
    for f in fills:
        ex, q, p = str(f["exchange_id"]), float(f["quantity"]), float(f["price"])
        s = st.setdefault(ex, {"pos": 0.0, "cash": 0.0, "peak": 0.0})
        s["pos"] += q
        s["cash"] -= q * p
        s["peak"] = max(s["peak"], abs(s["pos"]))
        if abs(s["pos"]) < 1 and s["peak"] >= 1:
            closed.append(s["cash"] / s["peak"] / TICK)
            s.update(pos=0.0, cash=0.0, peak=0.0)
    obs, mark_pnl, unpriced = list(closed), 0.0, 0
    for ex, s in st.items():
        if abs(s["pos"]) < 1:
            continue
        r = _fair_row(store, ex, now)
        if r is None or r["fair"] is None:
            unpriced += 1
            continue
        obs.append((s["cash"] + s["pos"] * r["fair"]) / abs(s["pos"]) / TICK)
        mark_pnl += s["cash"] + s["pos"] * (r["sig_mid"] if r["sig_mid"] is not None else r["fair"])
    return obs, {"open_legs": len(obs) - len(closed), "closed_legs": len(closed), "unpriced_legs": unpriced,
                 "mark_pnl": round(mark_pnl, 1)}


# ------------------------------------------------------------------------------------------------------------ evaluate
def evaluate(store, now: Optional[float] = None, params_dir: Optional[str] = None, log_path: Optional[str] = None,
             write: bool = True, since: Optional[dict] = None) -> dict:
    """{strategy: {n, mean, stat, verdict: ok|warn|kill, reason, ...}}. With write, new kills are added to
    params/strategy_kill.json (never removed here) and the per-sleeve test start is kept in the state file."""
    now = time.time() if now is None else now
    params_dir = params_dir or os.path.join(HERE, "params")
    log_path = log_path or os.path.join(HERE, "data", "sigbot.log")
    kill_path, state_path = os.path.join(params_dir, KILL_FILE), os.path.join(params_dir, STATE_FILE)
    raw = _read_json(kill_path, None) if os.path.exists(kill_path) else {}   # None = exists but unreadable
    kills = normalise_kills(raw)
    kill_file_ok = kills is not None
    if not kill_file_ok:
        # unreadable (half-edited) file: never rewrite it and never treat its entries as removed; the sleeves keep the
        # last good content (KillFile), and this run only reports
        kills = {}
        write_kills = False
    else:
        write_kills = write
    state = _read_json(state_path, {}) or {}
    starts = state.setdefault("test_start", {})
    seen = state.setdefault("paused_seen", {})
    seen.update(state.pop("killed_by_monitor", {}))   # older state files
    if kill_file_ok:
        for name, k in list(seen.items()):
            if name not in kills:                      # a human removed the entry: restart the test from now
                starts[name] = now
                seen.pop(name)
                state.setdefault("reenabled", []).append({"strategy": name, "ts": now, "utc": _iso(now), "was": k})
        for name, k in kills.items():                  # every pause, the monitor's or a manual one, restarts when removed
            seen.setdefault(name, k)
    since = dict(since or {})
    t0 = {n: max(float(since.get(n, 0.0)), float(starts.get(n, 0.0))) for n in SPECS}
    events = log_events(log_path)
    ledger = _read_json(os.path.join(params_dir, "churn_state.json"), {}) or {}

    out = {}
    data = {
        "mm": (mm_observations(store, now, t0["mm"]), {}),
        "catchup": catchup_observations(store, now, t0["catchup"]),
        "hf_arb": hf_arb_observations(store, now, events, ledger, t0["hf_arb"]),
        "recycle": recycle_observations(store, now, events, t0["recycle"]),
        "cheapfav": cheapfav_observations(store, now, t0["cheapfav"]),
    }
    for name, (obs, extra) in data.items():
        sp = SPECS[name]
        ev = evidence(obs, sp["sd_floor"], sp["clip"])
        if name == "cheapfav":
            v, why = verdict(ev, n_min=CHEAPFAV_MIN_LEGS, alpha_kill=CHEAPFAV_ALPHA, min_loss=CHEAPFAV_MIN_LOSS)
            if v == "kill" and extra.get("mark_pnl", 0.0) >= 0:
                v, why = "warn", why + "; mark P&L not negative"
            if v == "ok" and ev["n"] >= 3 and ev["mean"] <= -CHEAPFAV_MIN_LOSS and extra.get("mark_pnl", 0.0) < 0:
                v, why = "warn", f"edge vs outside fair {ev['mean']:+.1f} ticks per share, mark P&L {extra['mark_pnl']:+.0f}"
        else:
            v, why = verdict(ev, n_min=sp.get("n_min", N_MIN), alpha_kill=sp.get("alpha_kill", ALPHA_KILL))
        r = {"label": sp["label"], "unit": sp["unit"], "n": ev["n"],
             "mean": None if ev["mean"] is None else round(ev["mean"], 3),
             "sd": None if ev["sd"] is None else round(ev["sd"], 3), "stat": round(ev["L"], 2),
             "verdict": v, "reason": why, "test_start": _iso(t0[name]) if t0[name] else None, **extra}
        if not kill_file_ok:
            r["kill_file"] = "unreadable: nothing written; sleeves keep the last good content"
        if name in kills:
            r["verdict"] = "kill"
            r["reason"] = "paused since %s: %s (only a human re-enables: delete the entry in params/%s)" % (
                kills[name].get("utc", "?"), kills[name].get("reason", "?"), KILL_FILE)
        elif v == "kill" and write_kills:
            kills[name] = {"ts": now, "utc": _iso(now), "reason": why, "n": ev["n"], "mean": r["mean"], "stat": r["stat"],
                           "unit": sp["unit"]}
            seen[name] = kills[name]
            r["new_kill"] = True
        out[name] = r
    if write:
        if write_kills and any(r.get("new_kill") for r in out.values()):
            merged = dict(raw if isinstance(raw, dict) else {})
            merged.update({k: v for k, v in kills.items() if out.get(k, {}).get("new_kill")})
            _write_json(kill_path, merged)             # hand-written entries are kept exactly as written
        state["last"] = {"ts": now, "utc": _iso(now), "results": out}
        _write_json(state_path, state)
    return out


class KillFile:
    """What the sleeves check before a NEW entry. Re-read when the file changes; an unreadable file keeps the last good
    content (a half-edited file never re-enables a sleeve)."""

    def __init__(self, path: str):
        self.path, self.mtime, self.kills = path, None, {}

    def refresh(self) -> dict:
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            self.mtime, self.kills = None, {}
            return self.kills
        if m != self.mtime:
            d = normalise_kills(_read_json(self.path, None))
            if d is not None:
                self.kills, self.mtime = d, m
        return self.kills

    def paused(self, name: str) -> Optional[str]:
        k = self.refresh().get(name)
        return None if k is None else str(k.get("reason", "paused"))


# ------------------------------------------------------------------------------------------------------------ simulate
def simulate(mu: float, sigma: float, n_max: int, paths: int, noise: str = "normal", phi: float = 0.0, rho: float = RHO,
             sd_floor: float = 0.5, clip: Optional[float] = None, seed: int = 1, alpha_kill: float = ALPHA_KILL) -> dict:
    """Run the rule after every observation up to n_max. noise: normal | t3 (unit-variance Student t, 3 dof); phi: AR(1)
    autocorrelation. Returns P(kill), P(warn) and the kill sample sizes (quantiles)."""
    rng = random.Random(seed)
    lk, lw = math.log(1 / alpha_kill), math.log(1 / ALPHA_WARN)
    kills, warns = [], 0
    for _ in range(paths):
        s = ss = 0.0
        lag, hist = [0.0] * NW_LAGS, []
        e_prev, warned, killed_at = 0.0, False, None
        for n in range(1, n_max + 1):
            z = rng.gauss(0, 1)
            if noise == "t3":
                z = z / math.sqrt(rng.gammavariate(1.5, 2.0) / 3.0) / math.sqrt(3.0)
            e = phi * e_prev + math.sqrt(1 - phi * phi) * z
            e_prev = e
            x = mu + sigma * e
            if clip is not None:
                x = max(-clip, min(clip, x))
            s += x
            ss += x * x
            for k in range(1, min(NW_LAGS, len(hist)) + 1):
                lag[k - 1] += x * hist[-k]
            hist.append(x)
            if len(hist) > NW_LAGS:
                hist.pop(0)
            if n < N_MIN or s >= 0:
                continue
            var = max(long_run_var(n, s, ss, lag), sd_floor ** 2)
            a = 1 + n * rho * rho
            lg = -0.5 * math.log(a) + rho * rho * s * s / (2 * var * a)
            if lg >= lw:
                warned = True
            if lg >= lk:
                killed_at = n
                break
        warns += warned
        if killed_at:
            kills.append(killed_at)
    kills.sort()
    q = lambda p: kills[min(len(kills) - 1, int(p * len(kills)))] if kills else None
    return {"p_kill": len(kills) / paths, "p_warn": warns / paths, "kill_n_median": q(0.5), "kill_n_p90": q(0.9), "kill_times": kills}


class ReadOnlyStore:
    """Duck-typed Store (rows/one) on a read-only connection, so the CLI never writes to the live database."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def rows(self, sql, args=()):
        return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql, args=()):
        return self.conn.execute(sql, tuple(args)).fetchone()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--paths", type=int, default=1000)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.simulate:
        for sigma, n_max in ((1.8, 1500), (4.0, 300)):
            for noise, phi in (("normal", 0.0), ("t3", 0.0), ("normal", 0.3), ("t3", 0.3)):
                for mu in (0.0, 0.5, -0.5):
                    r = simulate(mu, sigma, n_max, args.paths, noise, phi, sd_floor=0.5)
                    print(f"sd {sigma} n_max {n_max} {noise:6} phi {phi}  edge {mu:+.1f}: P(kill) {r['p_kill']:.3f}  "
                          f"P(warn) {r['p_warn']:.3f}  kill n median {r['kill_n_median']} p90 {r['kill_n_p90']}")
        return
    res = evaluate(ReadOnlyStore(os.path.join(HERE, "data", "sigbot.sqlite")), write=False)
    if args.json:
        print(json.dumps(res, indent=1))
        return
    for name, r in res.items():
        extra = {k: v for k, v in r.items() if k not in ("label", "unit", "n", "mean", "sd", "stat", "verdict", "reason", "test_start")}
        print(f"{name:9} {r['verdict']:5} n={r['n']:4} mean={r['mean']} sd={r['sd']} evidence={r['stat']}  {r['reason']}  {extra}")


if __name__ == "__main__":
    main()
