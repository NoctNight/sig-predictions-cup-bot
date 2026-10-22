"""Edge-based capital allocation across sleeves. Paper only: reads data/sigbot.sqlite read-only, never calls the venue.

    python3 -m sigbot.allocator --report       # measure now, write params/allocation.json, print the table
    python3 -m sigbot.allocator --report --hours 24

Every hour (one `tick(bot)` per cycle, same pattern as sigbot.shadow) the trailing `lookback_hours` (48) of our own fills
and arb baskets are turned into a realised edge per 1,000 of capital per day for each sleeve:

  arb        sets bought in the window. P&L = locked settlement profit (qty x edge_per_set) minus an unwind cost of
             UNWIND_COST_FRAC (1%) of face (payout n-1 per sell-set, 1 per buy-set), because every set has to be turned back
             into cash before EXIT_DEADLINE (Nov 3 00:00 ET) for the end-of-competition trade. Sets already unwound
             realise entry edge minus forgone edge (their logged unwind edge). Capital-days = cost x (exit - entry):
             a held set's capital stays locked until the deadline, so the rate is what arb capital earns per day held.
  mm         purpose 'mm' fills: cash flow plus open inventory marked at the latest SIG mid (venue_fair, else our quote
             snapshot, else the last fill). Capital-days = resting-order collateral (each order lives from its placement
             batch to the next requote batch on that exchange, or expiry) + inventory collateral, integrated over time.
             The 2026-10-21 residual-hedge fills ('hedge', a since-disabled bug on MM inventory) are reported apart.
  tilt,      purpose 'tilt' / 'direction' fills plus the 'exit' fills that closed them (an exit is attributed to the
  direction  sleeve that held the exchange). P&L = cash flow plus open quantity marked at the latest outside fair
             (venue_fair; the sleeves' own valuation), with the SIG-mid mark shown alongside. Capital = position collateral
             x time.

Recommendation: the pool is POOL_FRAC (95%) of account value (fractions are of starting cash; direction's is of cash).
It is split in proportion to each sleeve's measured rate (negative rates count as zero), then clamped to a floor of
FLOOR_FRAC (20%) of the current value and to HARD_CAPS, with the excess re-split among unclamped sleeves. Sleeves with too
little evidence (MIN_EVIDENCE) keep their current value. Finally the worst-case exposure (MM resting + tilt + direction
capital, plus 2% of the arb capital for unwind cost and slippage) must stay under WORST_CASE_MAX (50,000); non-arb sleeves
are scaled down together if it does not. Nothing is applied unless config.json has "allocator": {"auto_apply": true};
then the fractions are set on the live config objects (in memory, until restart).

Out-of-sample gate: the same measurement is repeated on entries in the older 70% and in the newest 30% of the window
alone; a sleeve's fraction changes only if all three (full, older, newer) recommend moving it the same way with enough
evidence, and two consecutive runs agree (CONFIRM_RUNS). Each applied change moves halfway (STEP_FRAC) to the target.

Wiring: none needed. sigbot.shadow's tick calls `tick(bot, root)` here every cycle; runs every 12 h (`every_sec` 43200)
in a daemon thread. Applied values are remembered and audited through sigbot/tuned.py, like the shadow tuner's.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from . import tuned

log = logging.getLogger("sigbot.allocator")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "params", "allocation.json")

UNWIND_COST_FRAC = 0.01
EXIT_DEADLINE = "2026-11-03T04:00:00Z"        # Nov 3 00:00 ET: every arb set back in cash by then
FLOOR_FRAC = 0.20                             # never recommend below 20% of the current value
HARD_CAPS = {"arb": 0.90, "mm": 0.35, "tilt": 0.25, "direction": 0.20}   # ceilings raised by Sizhe 2026-10-22 15:05 ("use all of our capital")   # arb: Sizhe's ceiling (2026-10-22); tilt: never above its live value
POOL_FRAC = 0.95                               # the pool split by edge: 95% of account value (the rest stays as a cash buffer)
CONFIRM_RUNS = 2                               # consecutive runs that must agree on the direction of a change before it is applied
WORST_CASE_MAX = 75000.0   # raised with the ceilings (arb sets excluded: riskless to settlement)
ARB_WORST_FRAC = 0.02                          # arb capital at risk: unwind cost + slippage
MIN_EVIDENCE = {"arb": 3, "mm": 30, "tilt": 10, "direction": 10}         # baskets or fills
SPLIT_FRAC = 0.70                              # older 70% / newest 30% for the out-of-sample consistency gate
STEP_FRAC = 0.5                                # each application moves halfway from the current value to the target
SLEEVES = ("arb", "mm", "tilt", "direction")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


def open_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def latest_marks(con) -> tuple[dict, dict]:
    """exchange -> latest outside fair, exchange -> latest SIG mid (venue_fair, then our quote snapshots)."""
    fair, mid = {}, {}
    for r in con.execute("SELECT exchange_id, fair, sig_mid, MAX(ts) FROM venue_fair GROUP BY exchange_id"):
        if r["fair"] is not None:
            fair[r["exchange_id"]] = r["fair"]
        if r["sig_mid"] is not None:
            mid[r["exchange_id"]] = r["sig_mid"]
    for r in con.execute("SELECT exchange_id, mid, MAX(ts) FROM quotes GROUP BY exchange_id"):
        if r["mid"] is not None and r["exchange_id"] not in mid:
            mid[r["exchange_id"]] = r["mid"]
    return fair, mid


# ------------------------------------------------------------------------------------------------ measurement
def measure_arb(con, t0: float, now: float, t1: Optional[float] = None) -> dict:
    deadline = _ts(EXIT_DEADLINE)
    lots: dict[str, list] = defaultdict(list)
    pnl = cap_days = 0.0
    baskets = sets = 0
    unwound = 0
    t1 = now if t1 is None else t1
    for r in con.execute("SELECT ts, race, kind, legs, quantity, edge_per_set, cost_per_set FROM arb_trades WHERE ok=1 AND ts>=? ORDER BY ts", (t0,)):
        n = len(json.loads(r["legs"]))
        if r["kind"] in ("sell_set", "buy_set"):
            if r["ts"] >= t1:
                continue                               # entries outside [t0, t1) belong to another window
            payout = (n - 1) if r["kind"] == "sell_set" else 1
            lots[r["race"]].append(dict(ts=r["ts"], qty=r["quantity"], edge=r["edge_per_set"], cost=r["cost_per_set"], payout=payout))
            baskets += 1; sets += r["quantity"]
        elif r["kind"] == "unwind_set":
            q = r["quantity"]
            for lot in lots[r["race"]]:                # FIFO
                if q <= 0:
                    break
                take = min(q, lot["qty"])
                if take <= 0:
                    continue
                pnl += take * (lot["edge"] + r["edge_per_set"])          # entry edge minus forgone edge, no unwind cost charged twice
                cap_days += take * lot["cost"] * max(0.0, r["ts"] - lot["ts"]) / 86400.0
                lot["qty"] -= take; q -= take; unwound += take
    held_cap = 0.0
    for race, ls in lots.items():
        for lot in ls:
            if lot["qty"] <= 0:
                continue
            pnl += lot["qty"] * (lot["edge"] - UNWIND_COST_FRAC * lot["payout"])
            cap_days += lot["qty"] * lot["cost"] * max(1.0, deadline - lot["ts"]) / 86400.0
            held_cap += lot["qty"] * lot["cost"]
    return dict(pnl=pnl, cap_days=cap_days, evidence=baskets, evidence_kind="baskets", sets=sets, unwound_sets=unwound, held_capital=held_cap,
                note=f"held to {EXIT_DEADLINE[:10]}, {100 * UNWIND_COST_FRAC:.0f}% of face unwind cost")


def _fills(con, purposes: tuple, t0: float, t1: Optional[float] = None) -> list:
    lo = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    hi = datetime.fromtimestamp(t1 if t1 is not None else time.time() + 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    q = (f"SELECT exchange_id, side, price, quantity, purpose, filled_at FROM fills WHERE purpose IN ({','.join('?' * len(purposes))}) "
         "AND filled_at>=? AND filled_at<? AND price IS NOT NULL ORDER BY filled_at")
    return con.execute(q, purposes + (lo, hi)).fetchall()


def _position_book(fills: list, now: float, marks: dict, alt_marks: Optional[dict] = None) -> dict:
    """Signed YES quantity per exchange from fills (quantity is the signed YES delta, price YES-denominated);
    P&L = cash + inventory x mark; capital-days = collateral (|q| x price paid per share) integrated over time."""
    inv: dict[str, float] = defaultdict(float)
    cash: dict[str, float] = defaultdict(float)
    last_px: dict[str, float] = {}
    last_t: dict[str, float] = {}
    cap_days = 0.0
    max_coll = 0.0
    coll: dict[str, float] = defaultdict(float)
    for f in fills:
        ex, t = f["exchange_id"], _ts(f["filled_at"])
        if ex in last_t:
            cap_days += coll[ex] * (t - last_t[ex]) / 86400.0
        q, p = float(f["quantity"]), float(f["price"])
        inv[ex] += q; cash[ex] -= q * p
        last_px[ex], last_t[ex] = p, t
        coll[ex] = abs(inv[ex]) * (p if inv[ex] > 0 else 1 - p)
        max_coll = max(max_coll, sum(coll.values()))
    for ex, t in last_t.items():
        cap_days += coll[ex] * (now - t) / 86400.0
    def value(m):
        return sum(cash[ex] + inv[ex] * m.get(ex, last_px[ex]) for ex in inv)
    out = dict(pnl=value(marks), cap_days=cap_days, evidence=len(fills), evidence_kind="fills", open_exchanges=sum(1 for v in inv.values() if abs(v) >= 1),
               max_collateral=max_coll, open_collateral=sum(coll.values()))
    if alt_marks is not None:
        out["pnl_alt"] = value(alt_marks)
    return out


def resting_cap_days(con, purpose: str, t0: float, now: float, t1: Optional[float] = None) -> float:
    """Collateral of resting orders x lifetime. An order lives from its placement batch to the next batch on the same
    exchange (each requote is cancel-all + a new batch), its expiry, or now. Fills are not subtracted (small)."""
    by_ex: dict[str, list] = defaultdict(list)
    for o in con.execute("SELECT exchange_id, price, quantity, placed_at, expires_at FROM orders WHERE purpose=? AND placed_at>=? AND placed_at<? ORDER BY placed_at",
                         (purpose, t0, now if t1 is None else t1)):
        by_ex[o["exchange_id"]].append(o)
    total = 0.0
    for ex, orders in by_ex.items():
        starts = []
        for o in orders:
            if not starts or o["placed_at"] - starts[-1] > 3.0:
                starts.append(o["placed_at"])
        si = 0
        for o in orders:
            while si + 1 < len(starts) and starts[si + 1] <= o["placed_at"] + 3.0 and starts[si + 1] <= o["placed_at"]:
                si += 1
            end = starts[si + 1] if si + 1 < len(starts) else now
            if o["expires_at"]:
                end = min(end, _ts(o["expires_at"]))
            total += o["quantity"] * o["price"] * max(0.0, end - o["placed_at"]) / 86400.0
    return total


def measure_mm(con, t0: float, now: float, mid: dict, t1: Optional[float] = None) -> dict:
    fills = _fills(con, ("mm",), t0, t1)
    book = _position_book(fills, now, mid)
    resting = resting_cap_days(con, "mm", t0, now, t1)
    book["inventory_cap_days"] = book["cap_days"]
    book["resting_cap_days"] = resting
    book["cap_days"] += resting
    hedge = _position_book(_fills(con, ("hedge",), t0, t1), now, mid)
    book["hedge_pnl_excluded"] = round(hedge["pnl"], 1)
    book["note"] = "inventory marked at latest SIG mid; residual-hedge fills (disabled bug) excluded"
    return book


def measure_held(con, purpose: str, other: str, t0: float, now: float, fair: dict, mid: dict, t1: Optional[float] = None) -> dict:
    """tilt / direction: own fills plus the exits on exchanges this sleeve held (exits on exchanges both held go to the
    sleeve with more filled quantity there)."""
    own = defaultdict(float); oth = defaultdict(float)
    for f in _fills(con, (purpose,), t0, t1):
        own[f["exchange_id"]] += abs(f["quantity"])
    for f in _fills(con, (other,), t0, t1):
        oth[f["exchange_id"]] += abs(f["quantity"])
    # own entries inside [t0, t1); exits of those positions at any later time, never more than the entered quantity
    exit_room = dict(own)
    fills = []
    for f in _fills(con, (purpose, "exit"), t0):
        ex = f["exchange_id"]
        if f["purpose"] == purpose:
            if t1 is None or _ts(f["filled_at"]) < t1:
                fills.append(f)
        elif own[ex] > 0 and own[ex] >= oth[ex] and exit_room.get(ex, 0.0) >= 1:
            q = min(abs(f["quantity"]), exit_room[ex])
            exit_room[ex] -= q
            fills.append({"exchange_id": ex, "side": f["side"], "price": f["price"], "quantity": q if f["quantity"] > 0 else -q,
                          "purpose": "exit", "filled_at": f["filled_at"]})
    m = dict(mid); m.update(fair)
    book = _position_book(fills, now, m, alt_marks=mid)
    book["pnl_mid_mark"] = book.pop("pnl_alt")
    book["note"] = "marked at latest outside fair (SIG mid in pnl_mid_mark)"
    return book


def measure_all(con, t0: float, t1: Optional[float], now: float, fair: dict, mid: dict) -> dict:
    """All four sleeves for entries in [t0, t1), marked now; adds rate_per_1k_day and rate_alloc (conservative mark)."""
    m = {"arb": measure_arb(con, t0, now, t1), "mm": measure_mm(con, t0, now, mid, t1),
         "tilt": measure_held(con, "tilt", "direction", t0, now, fair, mid, t1),
         "direction": measure_held(con, "direction", "tilt", t0, now, fair, mid, t1)}
    for s, x in m.items():
        per = (x["cap_days"] / 1000.0) if x["cap_days"] > 1e-9 else None
        x["rate_per_1k_day"] = x["pnl"] / per if per else 0.0
        if "pnl_mid_mark" in x:
            x["rate_mid_per_1k_day"] = x["pnl_mid_mark"] / per if per else 0.0
            x["rate_alloc"] = min(x["rate_per_1k_day"], x["rate_mid_per_1k_day"])
        else:
            x["rate_alloc"] = x["rate_per_1k_day"]
    return m


def _sign(x: float, eps: float = 1e-4) -> int:
    return 1 if x > eps else (-1 if x < -eps else 0)


def _worst(amt: dict) -> float:
    return amt["mm"] + amt["tilt"] + amt["direction"] + ARB_WORST_FRAC * amt["arb"]


# ------------------------------------------------------------------------------------------------ per-race arb caps
RACE_CEILING_FRAC = 0.15                       # Sizhe's per-race ceiling (config arb.max_race_frac); caps never exceed it
RACE_DEFAULT_FRAC = 0.02                       # races with no measured opportunity in the window: cap 2,000 (new ones can start)
RACE_MIN_COMMON = 5                            # races seen in both halves needed for the rank test
RACE_MIN_CORR = 0.30                           # Spearman of race rates, older 70% vs newest 30%, needed to apply race caps


def days_left(now: float) -> float:
    return max(0.25, (_ts(EXIT_DEADLINE) - now) / 86400.0)


def held_sets(con, before: float) -> dict:
    """race -> dict(qty, capital) of arb sets held at `before` (arb_trades entries minus FIFO unwinds)."""
    lots: dict[str, list] = defaultdict(list)
    for r in con.execute("SELECT ts, race, kind, quantity, cost_per_set FROM arb_trades WHERE ok=1 AND ts<? ORDER BY ts", (before,)):
        if r["kind"] in ("sell_set", "buy_set"):
            lots[r["race"]].append([int(r["quantity"]), float(r["cost_per_set"])])
        elif r["kind"] == "unwind_set":
            q = int(r["quantity"])
            for lot in lots[r["race"]]:
                take = min(q, lot[0]); lot[0] -= take; q -= take
                if q <= 0:
                    break
    out = {}
    for race, ls in lots.items():
        qty = sum(l[0] for l in ls)
        if qty > 0:
            out[race] = {"qty": qty, "capital": sum(l[0] * l[1] for l in ls)}
    return out


def measure_races(con, t0: float, t1: float, now: float) -> dict:
    """race -> availability (share of polls with a set arb of >= 0.5c edge), mean net return when available (edge minus
    UNWIND_COST_FRAC x face, over cost), and the net rate per 1k of capital per day if held to EXIT_DEADLINE."""
    from .shadow import ArbData           # lazy: shadow imports this module lazily too
    ad = ArbData(con, t0, t1)
    if not ad.ok:
        return {}
    polls = [p for p in ad.opps if t0 <= p[0] < t1]
    if not polls:
        return {}
    acc: dict[str, list] = defaultdict(list)
    for ts, snap, ops in polls:
        best: dict[str, float] = {}
        for ret, race, kind, edge, cost, ssum in ops:
            n = len(ad.races.get(race, ())) or 2
            face = (n - 1) if kind == "sell_set" else 1
            net = (edge - UNWIND_COST_FRAC * face) / cost
            if net > best.get(race, -9.0):
                best[race] = net
        for race, net in best.items():
            acc[race].append(net)
    dl = days_left(now)
    out = {}
    for race, nets in acc.items():
        mean = sum(nets) / len(nets)
        out[race] = {"avail": len(nets) / len(polls), "net_ret": mean, "rate_per_1k_day": 1000.0 * mean / dl, "polls": len(nets)}
    return out


def spearman(a: list, b: list) -> Optional[float]:
    n = len(a)
    if n < 3:
        return None
    def ranks(x):
        order = sorted(range(n), key=lambda i: x[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and x[order[j + 1]] == x[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return cov / (va * vb) ** 0.5 if va > 0 and vb > 0 else None


def race_caps(stats: dict, arb_capital: float, held: dict, starting_cash: float) -> tuple[dict, float]:
    """Split the arb sleeve's capital across races in proportion to net rate x availability (positive only), each race
    between its held capital (caps never force an unwind) and the per-race ceiling. Returns (race -> cap, default cap)."""
    ceiling = RACE_CEILING_FRAC * starting_cash
    default = min(ceiling, RACE_DEFAULT_FRAC * starting_cash)
    w = {r: max(0.0, s["rate_per_1k_day"]) * s["avail"] for r, s in stats.items()}
    caps = {r: min(ceiling, held.get(r, {}).get("capital", 0.0)) for r in set(stats) | set(held)}
    remaining = max(0.0, arb_capital - sum(caps.values()))
    active = [r for r in caps if w.get(r, 0.0) > 0 and caps[r] < ceiling]
    for _ in range(20):
        tot = sum(w[r] for r in active)
        if remaining <= 1.0 or tot <= 0:
            break
        spent, nxt = 0.0, []
        for r in active:
            give = min(remaining * w[r] / tot, ceiling - caps[r])
            caps[r] += give; spent += give
            if caps[r] < ceiling - 1e-6:
                nxt.append(r)
        remaining -= spent
        active = nxt
    return {r: round(c) for r, c in caps.items()}, round(default)


# ------------------------------------------------------------------------------------------------ real-time offload
def build_state_from_bot(bot) -> dict:
    """Snapshot for offload_check from the running bot (no API calls): latest bulk prices, directional fair values,
    positions and sleeve holdings from the fills table, the scanner's best unfunded basket, cap-blocked direction signals."""
    prices, _ = getattr(bot, "latest_prices", ({}, 0.0)) or ({}, 0.0)
    px = {ex: (p.get("bestBid"), p.get("bestAsk")) for ex, p in (prices or {}).items()}
    fair = {r["exchange_id"]: r["fair"] for r in (getattr(getattr(bot, "direction", None), "rows", None) or []) if r.get("fair") is not None}
    st = {"prices": px, "fair": fair, "cash": float(getattr(bot, "cash", 0.0) or 0.0), "now": time.time(), "opps": []}
    st.update(_holdings(bot.store.rows))
    u = getattr(bot, "universe", None)
    st["races"] = {name: list(r.exchange_ids) for name, r in u.races.items()} if u is not None else {}
    sc = getattr(bot, "scanner", None)
    best = getattr(sc, "best_unfunded", None) if sc is not None else None
    if best and time.time() - best.get("ts", 0) < 120 and not best.get("failed"):
        arb = bot.arb
        blocked = arb.budget_left(st["cash"]) < best["cost"] * max(arb.cfg.min_set_qty, 1)
        if blocked:
            n = len(st["races"].get(best["race"], [])) or 2
            face = (n - 1) if best["kind"] == "sell_set" else 1
            edge = best["ret"] * best["cost"]
            st["opps"].append({"kind": "arb", "race": best["race"], "ret_net": (edge - UNWIND_COST_FRAC * face) / best["cost"],
                               "capital": best["cost"] * best["qty"], "why": "arb budget/cash exhausted", "funded_by": ["arb_set"]})
    st["opps"].extend(_blocked_direction(bot.store.rows, st))
    tilt = getattr(bot, "tilt", None)
    if tilt is not None and time.time() - getattr(tilt, "unfunded_ts", 0) < 180:
        for c in sorted(getattr(tilt, "unfunded", []) or [], key=lambda c: -c["edge"])[:5]:
            # tilt edge is return on collateral to settlement; charge one tick of exit cost on the collateral
            coll = (1 - c["touch"]) if c["kind"] == "longshot" else c["touch"]
            st["opps"].append({"kind": "tilt", "exchange_id": c["exchange_id"], "side": "no" if c["kind"] == "longshot" else "yes",
                               "ret_net": c["edge"] - 0.005 / max(coll, 0.05), "capital": min(c["capital"], 1000.0),
                               "why": "tilt budget/cash exhausted", "funded_by": ["arb_set"]})
    st["arb_races"] = {r["race"] for r in bot.store.rows("SELECT DISTINCT race FROM arb_trades WHERE ok=1 AND kind IN ('sell_set','buy_set')")}
    return st


def _holdings(rows_fn) -> dict:
    """positions (signed YES per exchange, all sleeves) and the tilt/direction holdings net of exits, from the fills table."""
    pos: dict[str, float] = defaultdict(float)
    for r in rows_fn("SELECT exchange_id, SUM(quantity) q FROM fills GROUP BY exchange_id"):
        pos[r["exchange_id"]] = float(r["q"] or 0.0)
    held: dict[tuple, dict] = {}
    for r in rows_fn("SELECT exchange_id, side, purpose, SUM(ABS(quantity)) q FROM fills WHERE purpose IN ('direction','tilt') GROUP BY 1,2,3"):
        k = (r["exchange_id"], r["side"])
        h = held.setdefault(k, {"qty": 0.0, "sleeve": r["purpose"]})
        h["qty"] += float(r["q"] or 0.0)
    for r in rows_fn("SELECT exchange_id, side, SUM(ABS(quantity)) q FROM fills WHERE purpose='exit' GROUP BY 1,2"):
        k = (r["exchange_id"], "no" if r["side"] == "yes" else "yes")       # an exit buying YES closed held NO
        if k in held:
            held[k]["qty"] -= float(r["q"] or 0.0)
    return {"positions": dict(pos), "sleeve_held": {k: v for k, v in held.items() if v["qty"] >= 1}}


def _blocked_direction(rows_fn, st: dict) -> list:
    """Directional value signals skipped for budget/cap in the last 10 minutes whose edge still exists at the latest fair."""
    out = []
    for r in rows_fn("SELECT exchange_id, side, edge, price, action, MAX(ts) ts FROM direction_signals WHERE action IN ('skipped_cap','skipped_budget') "
                     "AND ts>=? GROUP BY exchange_id, side", (st["now"] - 600,)):
        ex, side = r["exchange_id"], r["side"]
        f, (bid, ask) = st["fair"].get(ex), st["prices"].get(ex, (None, None))
        if f is None or bid is None or ask is None:
            continue
        price, edge = (ask, f - ask) if side == "yes" else (1 - bid, bid - f)
        if edge <= 0 or price <= 0:
            continue
        if r["action"] == "skipped_cap":
            continue                     # per-market cap on this same leg: exiting something else cannot lift it
        out.append({"kind": "direction", "exchange_id": ex, "side": side, "ret_net": edge / price, "capital": 300.0,
                    "why": f"direction {r['action']}", "funded_by": ["direction"]})
    return out


def build_state_from_db(con, cfg, now: float) -> dict:
    """The same snapshot as build_state_from_bot, from the logs only (CLI / report): latest SIG touch per leg from
    venue_quotes, latest outside fair from venue_fair, fills for holdings, the newest poll's arb baskets when the arb budget
    is used up, and recent cap-blocked directional signals."""
    px, races_map = {}, defaultdict(list)
    for r in con.execute("SELECT instrument, race, bid, ask, MAX(ts) FROM venue_quotes WHERE venue='sig' GROUP BY instrument"):
        px[r["instrument"]] = (r["bid"], r["ask"])
        races_map[r["race"]].append(r["instrument"])
    fair = {r["exchange_id"]: r["fair"] for r in con.execute("SELECT exchange_id, fair, MAX(ts) FROM venue_fair GROUP BY exchange_id") if r["fair"] is not None}
    c = con.execute("SELECT cash FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()
    st = {"prices": px, "fair": fair, "cash": float(c["cash"]) if c else 0.0, "now": now, "races": dict(races_map), "opps": [],
          "arb_races": set(held_sets(con, now + 1))}
    st.update(_holdings(lambda q, a=(): con.execute(q, a).fetchall()))
    held = held_sets(con, now + 1)
    budget = max(0.0, min(cfg.arb.capital_frac * cfg.starting_cash - sum(h["capital"] for h in held.values()), 0.95 * st["cash"]))
    from .shadow import ArbData
    ad = ArbData(con, now - 1800, now + 1)
    if ad.ok and ad.opps:
        for ret, race, kind, edge, cost, ssum in ad.opps[-1][2]:
            if ret < cfg.arb.min_ret or budget >= cost * cfg.arb.min_set_qty:
                continue
            n = len(ad.races.get(race, ())) or 2
            face = (n - 1) if kind == "sell_set" else 1
            st["opps"].append({"kind": "arb", "race": race, "ret_net": (edge - UNWIND_COST_FRAC * face) / cost, "capital": 500 * cost,
                               "why": "arb budget/cash exhausted", "funded_by": ["arb_set"]})
    st["opps"].extend(_blocked_direction(lambda q, a=(): con.execute(q, a).fetchall(), st))
    return st


def held_positions(st: dict) -> list:
    """Every exitable holding with what exiting at the touch now gives up, as a return on the capital it frees:
    arb set: forgone = (n-1) x (1 - UNWIND_COST_FRAC) - (n - sum of asks); tilt/direction YES: fair - bid on bid;
    NO: ask - fair on 1 - ask. This forgone amount is both the remaining edge and the unwind cost, counted once."""
    out = []
    pos, px = st["positions"], st["prices"]
    for race, exs in st.get("races", {}).items():
        qs = [pos.get(e, 0.0) for e in exs]
        if len(exs) < 2 or not all(q < 0 for q in qs) or ("arb_races" in st and race not in st["arb_races"]):
            continue
        asks = [px.get(e, (None, None))[1] for e in exs]
        if any(a is None for a in asks):
            continue
        n = len(exs)
        proceeds = n - sum(asks)
        if proceeds <= 0:
            continue
        forgone = (n - 1) * (1 - UNWIND_COST_FRAC) - proceeds
        q = int(min(-x for x in qs))
        out.append({"kind": "arb_set", "race": race, "qty": q, "proceeds": proceeds, "capital": q * proceeds,
                    "forgone_ret": forgone / proceeds, "exit": "existing recycle/free-unwind path (atomic)"})
    for (ex, side), h in st.get("sleeve_held", {}).items():
        f, (bid, ask) = st["fair"].get(ex), px.get(ex, (None, None))
        if f is None or bid is None or ask is None:
            continue
        p = pos.get(ex, 0.0)
        q = int(min(h["qty"], -p if side == "no" else p))
        if q < 1:
            continue
        if side == "yes":
            proceeds, forgone, order = bid, f - bid, ("no", round(1 - bid, 3))
        else:
            proceeds, forgone, order = 1 - ask, ask - f, ("yes", round(ask, 3))
        if proceeds <= 0:
            continue
        out.append({"kind": h["sleeve"], "exchange_id": ex, "side": side, "qty": q, "proceeds": proceeds, "capital": q * proceeds,
                    "forgone_ret": forgone / proceeds, "order": order})
    return out


def offload_check(st: dict, min_gain: float) -> list:
    """For each blocked opportunity (best first), the lowest-forgone holding whose exit funds it is recommended when
    new net return - forgone return >= min_gain (both to EXIT_DEADLINE, so the same horizon)."""
    holds = sorted(held_positions(st), key=lambda h: h["forgone_ret"])
    recs, used = [], set()
    for o in sorted(st.get("opps", []), key=lambda o: -o["ret_net"]):
        for h in holds:
            key = h.get("race") or (h.get("exchange_id"), h.get("side"))
            if h["kind"] not in o.get("funded_by", (h["kind"],)):
                continue                            # this exit would not lift the constraint that blocks the opportunity
            if key in used or key == o.get("race") or key == (o.get("exchange_id"), o.get("side")):
                continue
            gain = o["ret_net"] - h["forgone_ret"]
            if gain < min_gain:
                break                               # holds are sorted: nothing later is cheaper to give up
            need = o["capital"]
            qty = h["qty"] if h["kind"] == "arb_set" else int(min(h["qty"], math.ceil(need / max(h["proceeds"], 0.01))))
            recs.append({"opportunity": o, "unwind": dict(h, qty=qty, capital=round(qty * h["proceeds"], 1)),
                         "gain": round(gain, 4), "gain_per_1k_day": round(1000 * gain / days_left(st["now"]), 2)})
            used.add(key)
            break
    return recs


# ------------------------------------------------------------------------------------------------ allocation
def current_capital(cfg, cash: float, bot=None) -> dict:
    d = getattr(cfg, "direction", None)
    dir_frac = float(d.get("overall_cash_frac", 0.04)) if isinstance(d, dict) else 0.04
    if bot is not None and getattr(getattr(bot, "direction", None), "cfg", None) is not None:
        dir_frac = float(bot.direction.cfg.overall_cash_frac)      # the live value (may have been applied in memory)
    return {"arb": dict(frac=float(cfg.arb.capital_frac), base=cfg.starting_cash),
            "mm": dict(frac=float(cfg.mm.capital_frac), base=cfg.starting_cash),
            "tilt": dict(frac=float(cfg.tilt.capital_frac), base=cfg.starting_cash),
            "direction": dict(frac=dir_frac, base=cash)}


def allocate(rates: dict, evidence_ok: dict, cur: dict, account_value: float) -> tuple[dict, list]:
    """Split POOL_FRAC x account value by water-filling proportional to positive rates within [floor, cap]; low-evidence
    sleeves keep their current capital (and are taken out of the pool first)."""
    notes = []
    amt = {s: cur[s]["frac"] * cur[s]["base"] for s in SLEEVES}
    floor = {s: FLOOR_FRAC * amt[s] for s in SLEEVES}
    cap = {s: HARD_CAPS[s] * cur[s]["base"] for s in SLEEVES}
    fixed = {s: min(amt[s], cap[s]) for s in SLEEVES if not evidence_ok[s]}
    for s in fixed:
        notes.append(f"{s}: too little evidence, kept at current value")
    free = [s for s in SLEEVES if s not in fixed]
    pool = max(sum(floor[s] for s in free), POOL_FRAC * account_value - sum(fixed.values()))
    target = {s: floor[s] for s in free}
    remaining = pool - sum(target.values())
    active = [s for s in free if max(rates[s], 0.0) > 0 and target[s] < cap[s]]
    for _ in range(10):
        if remaining <= 1e-6 or not active:
            break
        w = sum(max(rates[s], 0.0) for s in active)
        if w <= 0:
            break
        nxt = []
        spent = 0.0
        for s in active:
            add = remaining * max(rates[s], 0.0) / w
            room = cap[s] - target[s]
            give = min(add, room)
            target[s] += give; spent += give
            if target[s] < cap[s] - 1e-6:
                nxt.append(s)
        remaining -= spent
        active = nxt
    if remaining > 1e-6:
        notes.append(f"{remaining:,.0f} of the pool left unallocated (caps bind or no positive-rate sleeve has room): stays in cash")
    target.update(fixed)
    # worst-case exposure
    wc = _worst(target)
    if wc > WORST_CASE_MAX:
        nonarb = target["mm"] + target["tilt"] + target["direction"]
        k = max(0.0, (WORST_CASE_MAX - ARB_WORST_FRAC * target["arb"]) / nonarb) if nonarb > 0 else 0.0
        for s in ("mm", "tilt", "direction"):
            target[s] *= k
        notes.append(f"worst case {wc:,.0f} > {WORST_CASE_MAX:,.0f}: non-arb sleeves scaled by {k:.2f}")
    fracs = {s: round(target[s] / cur[s]["base"], 4) for s in SLEEVES}
    return fracs, notes


class Allocator:
    def __init__(self, db_path: str, cfg, root: str = ROOT, every_sec: float = 43200.0, lookback_hours: float = 48.0,
                 auto_apply: bool = False, enabled: bool = True, min_change: float = 0.005):
        self.db_path, self.cfg, self.root = db_path, cfg, root
        self.every_sec, self.lookback_hours, self.auto_apply, self.enabled, self.min_change = every_sec, lookback_hours, auto_apply, enabled, min_change
        self.last_run = 0.0
        self.bot = None
        self.confirm_runs = CONFIRM_RUNS
        self.streak: dict[str, tuple] = {}
        self.startup_values: Optional[dict] = None
        try:
            st = json.load(open(os.path.join(root, "params", "allocator_state.json")))
            self.last_run = float(st.get("last_run", 0.0))
            self.streak = {k: (int(v[0]), int(v[1])) for k, v in (st.get("streak") or {}).items()}
        except (OSError, ValueError, TypeError, IndexError):
            pass
        self._thread: Optional[threading.Thread] = None
        self._pending: Optional[dict] = None
        self._lock = threading.Lock()

    @staticmethod
    def from_config(cfg, root: str = ROOT) -> "Allocator":
        raw = {}
        try:
            raw = json.load(open(os.path.join(root, "config.json"))).get("allocator") or {}
        except Exception:
            pass
        return Allocator(os.path.join(root, cfg.db_path), cfg, root, every_sec=float(raw.get("every_sec", 43200)),
                         lookback_hours=float(raw.get("lookback_hours", 48)), auto_apply=bool(raw.get("auto_apply", False)),
                         enabled=bool(raw.get("enabled", True)), min_change=float(raw.get("min_change", 0.005)))

    def run(self, hours: Optional[float] = None, cash: Optional[float] = None) -> dict:
        cpu0 = time.thread_time()
        now = time.time()
        t0 = now - 3600.0 * (hours or self.lookback_hours)
        con = open_ro(self.db_path)
        try:
            r = con.execute("SELECT cash, total_value FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()
            if cash is None:
                cash = float(r["cash"]) if r and r["cash"] is not None else self.cfg.starting_cash
            account_value = float(r["total_value"]) if r and r["total_value"] else self.cfg.starting_cash
            first = con.execute("SELECT MIN(ts) FROM cycles").fetchone()[0]
            if first is not None:
                t0 = max(t0, float(first))              # the split must fall inside data that exists
            fair, mid = latest_marks(con)
            split = t0 + SPLIT_FRAC * (now - t0)
            windows = {"full": (t0, None), "fit": (t0, split), "test": (split, None)}
            ms = {w: measure_all(con, a, b, now, fair, mid) for w, (a, b) in windows.items()}
            races = {"full": measure_races(con, t0, now + 1, now), "fit": measure_races(con, t0, split, now),
                     "test": measure_races(con, split, now + 1, now)}
            held = held_sets(con, now + 1)
            offload_state = build_state_from_db(con, self.cfg, now)
        finally:
            con.close()
        cur = current_capital(self.cfg, cash, self.bot)
        rec = {}
        for w, m in ms.items():
            rates = {s: m[s]["rate_alloc"] for s in SLEEVES}
            ok = {s: m[s]["evidence"] >= MIN_EVIDENCE[s] for s in SLEEVES}
            rec[w] = (allocate(rates, ok, cur, account_value), ok)
        (fracs, notes), ok = rec["full"]
        m = ms["full"]
        rep = {"generated_at": _iso(now), "window": {"from": _iso(t0), "to": _iso(now), "split": _iso(split), "hours": round((now - t0) / 3600, 1)},
               "cash": round(cash), "account_value": round(account_value), "pool": round(POOL_FRAC * account_value),
               "sleeves": {}, "notes": notes, "auto_apply": self.auto_apply,
               "rules": {"floor_frac_of_current": FLOOR_FRAC, "hard_caps": HARD_CAPS, "worst_case_max": WORST_CASE_MAX, "min_evidence": MIN_EVIDENCE,
                         "unwind_cost_frac_of_face": UNWIND_COST_FRAC, "exit_deadline": EXIT_DEADLINE, "step_frac": STEP_FRAC,
                         "rate_used": "arb/mm: their only mark; tilt/direction: the lower of the outside-fair and SIG-mid marks",
                         "out_of_sample_gate": "a change is applied only if the full window, the older 70% alone and the newest 30% alone "
                                               "(entries in that part, marked now) all recommend moving the same way with enough evidence",
                         "frac_base": "starting cash for arb/mm/tilt, current cash for direction"}}
        key = {"arb": "arb.capital_frac", "mm": "mm.capital_frac", "tilt": "tilt.capital_frac", "direction": "direction.overall_cash_frac"}
        changes = []
        for s in SLEEVES:
            x = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in m[s].items()}
            c = cur[s]["frac"]
            dirs = {w: _sign(rec[w][0][0][s] - c) if rec[w][1][s] else 0 for w in rec}
            consistent = dirs["full"] != 0 and dirs["full"] == dirs["fit"] == dirs["test"]
            step = round(c + STEP_FRAC * (fracs[s] - c), 4)
            x.update(param=key[s], current_frac=c, recommended_frac=fracs[s], enough_evidence=ok[s],
                     fit_rate=round(ms["fit"][s]["rate_alloc"], 2), test_rate=round(ms["test"][s]["rate_alloc"], 2),
                     fit_evidence=ms["fit"][s]["evidence"], test_evidence=ms["test"][s]["evidence"],
                     fit_recommended=rec["fit"][0][0][s], test_recommended=rec["test"][0][0][s], consistent=consistent,
                     apply_step=step if consistent else c,
                     current_capital=round(c * cur[s]["base"]), recommended_capital=round(fracs[s] * cur[s]["base"]))
            rep["sleeves"][s] = x
            if consistent and abs(step - c) >= self.min_change:
                sign = _sign(step - c)
                prev = self.streak.get(key[s])
                n = prev[1] + 1 if prev and prev[0] == sign else 1
                self.streak[key[s]] = (sign, n)
                x["confirmations"] = n
                if n >= self.confirm_runs:
                    changes.append({"key": key[s], "from": c, "to": step, "target": fracs[s], "confirmations": n})
                else:
                    x["status"] = f"awaiting confirmation ({n}/{self.confirm_runs})"
            else:
                self.streak.pop(key[s], None)
                x["confirmations"] = 0
        rep["worst_case_current"] = round(_worst({s: cur[s]["frac"] * cur[s]["base"] for s in SLEEVES}))
        rep["worst_case_recommended"] = round(_worst({s: fracs[s] * cur[s]["base"] for s in SLEEVES}))
        rep["changes"] = changes
        # ---- per-race arb caps: rank races by measured net return; applied only if the ranking holds out of sample
        common = sorted(set(races["fit"]) & set(races["test"]))
        rho = spearman([races["fit"][r]["rate_per_1k_day"] for r in common], [races["test"][r]["rate_per_1k_day"] for r in common])
        arb_cap = cur["arb"]["frac"] * cur["arb"]["base"]
        caps, default = race_caps(races["full"], arb_cap, held, self.cfg.starting_cash)
        rank_ok = len(common) >= RACE_MIN_COMMON and rho is not None and rho >= RACE_MIN_CORR
        rep["races"] = {"rank_test": {"common_races": len(common), "spearman": None if rho is None else round(rho, 3), "min": RACE_MIN_CORR,
                                      "passes": rank_ok},
                        "ceiling": round(RACE_CEILING_FRAC * self.cfg.starting_cash), "default_cap": default, "arb_capital": round(arb_cap),
                        "caps": caps if rank_ok else {}, "proposed_caps": caps,
                        "table": {r: {"avail": round(v["avail"], 3), "net_ret": round(v["net_ret"], 4), "rate_per_1k_day": round(v["rate_per_1k_day"], 2),
                                      "held_capital": round(held.get(r, {}).get("capital", 0.0)), "cap": caps.get(r)}
                                  for r, v in sorted(races["full"].items(), key=lambda kv: -kv[1]["rate_per_1k_day"])},
                        "note": "caps never force an unwind (floor = held capital); races not listed get default_cap; needs the "
                                "race_budget_left hook in arb.py to take effect"}
        # ---- offload snapshot from the logs (the bot recomputes this every cycle from live state)
        rep["offload"] = {"recommendations": offload_check(offload_state, float(self.cfg.arb.recycle_min_gain)),
                          "held_lowest": sorted(held_positions(offload_state), key=lambda h: h["forgone_ret"])[:5],
                          "opportunities": offload_state["opps"][:5], "min_gain": float(self.cfg.arb.recycle_min_gain)}
        rep["cpu_sec"] = round(time.thread_time() - cpu0, 3)
        os.makedirs(os.path.join(self.root, "params"), exist_ok=True)
        tmp = os.path.join(self.root, "params", "allocation.json.tmp")
        with open(tmp, "w") as f:
            json.dump(rep, f, indent=1)
        os.replace(tmp, os.path.join(self.root, "params", "allocation.json"))
        return rep

    # ------------------------------------------------------------------------------------- bot integration
    KEYS = ("arb.capital_frac", "mm.capital_frac", "tilt.capital_frac", "direction.overall_cash_frac")

    def _save_state(self):
        try:
            path = os.path.join(self.root, "params", "allocator_state.json")
            with open(path + ".tmp", "w") as f:
                json.dump({"last_run": self.last_run, "streak": {k: list(v) for k, v in dict(self.streak).items()}}, f, indent=1)
            os.replace(path + ".tmp", path)
        except OSError as e:
            log.warning("allocator state not saved: %s", e)

    def tick(self, bot=None) -> list:
        """Once per cycle (the shadow tick calls it). First call: re-apply remembered allocations (auto_apply only). Starts a
        run in a daemon thread every `every_sec`; on a later call logs it and applies confirmed changes when auto_apply."""
        if not self.enabled:
            return []
        if bot is not None:
            self.bot = bot
            if self.startup_values is None:
                self.startup_values = {}
                for k in self.KEYS:
                    try:
                        self.startup_values[k] = json.loads(json.dumps(tuned.get_value(bot, k)))
                    except Exception:
                        pass
                if self.auto_apply:
                    done = tuned.reapply_on_start(bot, self.startup_values, _allowed, "allocator")
                    if done:
                        log.warning("ALLOCATOR re-applied remembered values for %s", done)
        with self._lock:
            pending, self._pending = self._pending, None
        applied: list = []
        if pending is not None:
            changes = pending.get("changes", [])
            log.info("ALLOCATOR rates/1k/day %s; confirmed changes %s; notes %s",
                     {s: round(v["rate_alloc"], 2) for s, v in pending["sleeves"].items()},
                     [(c["key"], c["from"], c["to"]) for c in changes], pending.get("notes"))
            if changes and self.auto_apply and bot is not None:
                applied = self.apply(changes, bot)
            elif changes:
                log.info("ALLOCATOR auto_apply is off: changes logged only")
        if (self._thread is None or not self._thread.is_alive()) and time.time() - self.last_run >= self.every_sec:
            self.last_run = time.time()
            cash = getattr(bot, "cash", None) if bot is not None else None
            self._thread = threading.Thread(target=self._bg, args=(cash,), name="allocator", daemon=True)
            self._thread.start()
        return applied

    def _bg(self, cash):
        try:
            rep = self.run(cash=cash)
        except Exception as e:  # must never affect the loop
            log.exception("allocator run failed: %s", e)
            return
        finally:
            self._save_state()
        with self._lock:
            self._pending = rep

    def apply(self, changes: list, bot) -> list:
        applied = []
        for c in changes:
            key, val = c["key"], float(c["to"])
            if key not in self.KEYS or not _allowed(key, val):
                continue
            try:
                live = float(tuned.get_value(bot, key))
                if abs(live - float(c["from"])) > 1e-9:
                    log.warning("ALLOCATOR skip %s: live value %s changed since the run used %s", key, live, c["from"])
                    continue
                tuned.record(bot, key, val, "allocator", {"target": c.get("target"), "confirmations": c.get("confirmations")},
                             (self.startup_values or {}).get(key, c["from"]))
                self.streak.pop(key, None)
                applied.append(key)
                log.warning("ALLOCATOR APPLIED %s: %s -> %s (target %s)", key, c["from"], val, c.get("target"))
            except Exception as e:
                log.warning("allocator apply %s failed: %s", key, e)
        if applied:
            self._save_state()
        return applied


def _allowed(key: str, val) -> bool:
    try:
        return 0.0 <= float(val) <= HARD_CAPS[key.split(".")[0]] + 1e-9
    except (TypeError, ValueError, KeyError):
        return False


def tick(bot, root: str = ROOT) -> list:
    """Creates the allocator on first use (config.json 'allocator' section under `root`), then ticks it. The shadow tick
    calls this every cycle, so run.py needs no separate hook."""
    a = getattr(bot, "_allocator", None)
    if a is None:
        a = Allocator.from_config(bot.cfg, root)
        bot._allocator = a
    return a.tick(bot)


def format_report(rep: dict) -> str:
    w = rep["window"]
    L = [f"allocation {rep['generated_at']} UTC  window {w['from']} .. {w['to']} ({w['hours']} h)  cash {rep['cash']:,}  cpu {rep.get('cpu_sec')} s"]
    L.append(f"{'sleeve':10s} {'pnl':>8s} {'cap-days':>10s} {'rate/1k/day':>11s} {'evidence':>14s} {'param':>28s} {'current':>8s} {'recommend':>9s} {'capital now->rec':>18s}")
    for s, x in rep["sleeves"].items():
        ev = f"{x['evidence']} {x['evidence_kind']}" + ("" if x["enough_evidence"] else " (low)")
        extra = f"  [mid mark {x['pnl_mid_mark']:+.0f}, {x['rate_mid_per_1k_day']:+.2f}/1k/day]" if "pnl_mid_mark" in x else ""
        L.append(f"{s:10s} {x['pnl']:+8.0f} {x['cap_days']:10,.0f} {x['rate_per_1k_day']:+11.2f} {ev:>14s} {x['param']:>28s} {x['current_frac']:8.3f} "
                 f"{x['recommended_frac']:9.3f} {x['current_capital']:>8,}->{x['recommended_capital']:<8,}{extra}")
    L.append("out-of-sample gate (rate/1k/day and recommended frac from the older 70% vs the newest 30% alone):")
    for s, x in rep["sleeves"].items():
        L.append(f"  {s:10s} fit {x['fit_rate']:+9.2f} ({x['fit_evidence']:>4} ev) -> {x['fit_recommended']:.3f}   test {x['test_rate']:+9.2f} "
                 f"({x['test_evidence']:>4} ev) -> {x['test_recommended']:.3f}   {'consistent: apply ' + format(x['apply_step'], '.3f') if x['consistent'] else 'no change'}")
    mm = rep["sleeves"]["mm"]
    L.append(f"mm capital-days: inventory {mm['inventory_cap_days']:,.0f} + resting {mm['resting_cap_days']:,.0f}; residual-hedge P&L excluded {mm['hedge_pnl_excluded']:+.0f}")
    a = rep["sleeves"]["arb"]
    L.append(f"arb: {a['sets']:,} sets bought, {a['unwound_sets']:,} unwound, {a['held_capital']:,.0f} held; {a['note']}")
    L.append(f"worst case: current {rep['worst_case_current']:,} -> recommended {rep['worst_case_recommended']:,} (max {WORST_CASE_MAX:,.0f})")
    for n in rep["notes"]:
        L.append("note: " + n)
    rc = rep.get("races")
    if rc:
        t = rc["rank_test"]
        L.append(f"per-race arb caps (arb capital {rc['arb_capital']:,}, ceiling {rc['ceiling']:,}, default {rc['default_cap']:,}); rank test older vs newer: "
                 f"spearman {t['spearman']} on {t['common_races']} races (need {t['min']}) -> {'PASS' if t['passes'] else 'FAIL (caps not applied)'}")
        L.append(f"  {'race':28s} {'avail':>6s} {'net ret':>8s} {'rate/1k/d':>9s} {'held':>7s} {'cap':>7s}")
        for r, v in list(rc["table"].items())[:12]:
            L.append(f"  {r:28s} {v['avail']:6.2f} {100 * v['net_ret']:7.2f}% {v['rate_per_1k_day']:9.2f} {v['held_capital']:7,} {(v['cap'] or 0):7,}")
    of = rep.get("offload")
    if of:
        L.append(f"offload (report only, min gain {100 * of['min_gain']:.1f}%): {len(of['opportunities'])} blocked opportunities; cheapest holdings to give up: "
                 + ", ".join(f"{h['kind']} {h.get('race') or h.get('exchange_id')} {100 * h['forgone_ret']:+.2f}%" for h in of["held_lowest"][:4]))
        for r in of["recommendations"][:3]:
            o, h = r["opportunity"], r["unwind"]
            L.append(f"  unwind {h['kind']} {h.get('race') or h.get('exchange_id')} x{h['qty']} (forgo {100 * h['forgone_ret']:.2f}%) -> fund {o['kind']} "
                     f"{o.get('race') or o.get('exchange_id')} (net {100 * o['ret_net']:.2f}%): gain {100 * r['gain']:.2f}%")
    L.append("CHANGES: " + ("; ".join(f"{c['key']} {c['from']} -> {c['to']}" for c in rep["changes"]) or "none")
             + ("" if rep["auto_apply"] else "  (allocator.auto_apply is off: not applied)"))
    return "\n".join(L)


def main():
    from .config import BotConfig
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="measure now and print the table")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = BotConfig.load()
    rep = Allocator.from_config(cfg, ROOT).run(hours=a.hours)
    print(json.dumps(rep, indent=1) if a.json else format_report(rep))


if __name__ == "__main__":
    main()
