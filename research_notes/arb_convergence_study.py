"""Arb convergence study: do set-arb overrounds converge enough that exiting a set early (and re-deploying) beats holding?

Read-only research. Opens a COPY of data/sigbot.sqlite read-only (sigbot.backtest_arb.open_copy_ro), reads JSON in data/,
writes only data/research_arb_convergence/ and research_notes/figures/arb_convergence*.png. No venue calls.

    <venv>/bin/python research_notes/arb_convergence_study.py        (numpy + matplotlib)

Formulas (n legs, YES touch prices, sb = sum of YES bids, sa = sum of YES asks, one tick = 0.005):
  sell-set (NO on every leg, pays n-1):  entry cost n - sb0, hold profit to settlement P = sb0 - 1
      exit at t1 = sell the NOs back = buy YES at the asks (YES+NO redeem 1): proceeds n - sa1
      round trip R = (n - sa1) - (n - sb0) = sb0 - sa1;  exit cost (profit forgone vs payout) = sa1 - 1
  buy-set (YES on every leg, pays 1):    entry cost sa0, hold profit P = 1 - sa0 (minus third-party risk x)
      exit at t1 = sell YES at the bids: proceeds sb1; R = sb1 - sa0; exit cost = 1 - sb1
  early-exit share = R / P (can exceed 100% when the book flips past fair, e.g. sa1 < 1 for a sell-set).

Panels:
  A (in-sample, proxy)  14 Oct 00:00 - 21 Oct 22:15 UTC, 5-min grid, leg mid = last 5-min trade close
                        (data/candles_5m.json), bid/ask = mid -/+ the leg's median half-spread measured in panel B.
                        Calibrated against real quotes on panel B's period with tape-built closes (section 0 of the note).
  B (out-of-sample, real) 21 Oct 23:50 - 23 Oct 21:25 UTC, 5-min grid, real touch from venue_quotes (venue='sig'),
                        forward-filled; grid points more than 10 min after the last poll are dropped (outages).
"""
from __future__ import annotations

import bisect
import collections
import json
import math
import os
import sys
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from sigbot.proposals.volatile_moves import load_markets, parse_iso  # noqa: E402  (pure loaders)
from sigbot.backtest_arb import open_copy_ro  # noqa: E402  (copies the db, opens the copy read-only)

DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "research_arb_convergence")
FIG = os.path.join(HERE, "research_notes", "figures")
os.makedirs(OUT, exist_ok=True)
T = 0.005
STEP = 300.0
UTC = timezone.utc
A0 = datetime(2026, 10, 14, 0, 0, tzinfo=UTC).timestamp()
A1 = datetime(2026, 10, 21, 22, 15, tzinfo=UTC).timestamp()
B0 = datetime(2026, 10, 21, 23, 50, tzinfo=UTC).timestamp()
END_PUNT = datetime(2026, 11, 3, 23, 0, tzinfo=UTC).timestamp()     # 18:00 ET (EST) 3 Nov
SELL_MIN_RET, SELL_MIN_EDGE = 0.005, 0.005
BUY_MIN_RET, BUY_MIN_EDGE = 0.024, 0.005
H_LIST = [("1 h", 3600), ("6 h", 6 * 3600), ("1 d", 86400), ("2 d", 2 * 86400), ("3 d", 3 * 86400), ("5 d", 5 * 86400)]


def fmt_ts(t):
    return datetime.fromtimestamp(t, UTC).strftime("%m-%d %H:%M")


# ----------------------------------------------------------------------------------------------- data
def load_db():
    con, tmp = open_copy_ro(os.path.join(DATA, "sigbot.sqlite"))
    vq = con.execute("SELECT ts, race, party, instrument, bid, ask FROM venue_quotes WHERE venue='sig' ORDER BY ts").fetchall()
    arb = con.execute("SELECT id, ts, race, kind, legs, quantity, edge_per_set, cost_per_set FROM arb_trades WHERE ok=1 ORDER BY ts").fetchall()
    qs = con.execute("SELECT ts, exchange_id, best_bid, best_ask, bid_qty, ask_qty FROM quotes ORDER BY ts").fetchall()
    tape = con.execute("SELECT exchange_id, price, executed_at FROM tape WHERE executed_at >= '2026-10-21T22'").fetchall()
    cyc = con.execute("SELECT ts, cash FROM cycles ORDER BY ts").fetchall()
    con.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return vq, arb, qs, tape, cyc


def races_from_markets():
    m = load_markets()
    races = collections.defaultdict(dict)
    for ex, (race, party, _) in m.items():
        races[race][party] = ex
    return {r: [p[k] for k in sorted(p)] for r, p in races.items()}, m


def x_table(races):
    try:
        by = json.load(open(os.path.join(HERE, "params", "x_live.json")))["by_race"]
    except (OSError, ValueError, KeyError):
        by = {}
    return {r: (0.0 if len(ex) == 3 else float(by.get(r, {}).get("x", 0.003))) for r, ex in races.items()}


class Panel:
    """Per race arrays on a common grid: sb, sa (NaN when missing), n legs."""

    def __init__(self, name, grid, sb, sa, n, src):
        self.name, self.grid, self.sb, self.sa, self.n, self.src = name, grid, sb, sa, n, src


def panel_B(vq, races):
    by_inst = collections.defaultdict(list)
    polls = sorted({r[0] for r in vq})
    for ts, race, party, inst, bid, ask in vq:
        by_inst[inst].append((ts, bid, ask))
    grid = np.arange(B0, polls[-1] + 1, STEP)
    pidx = np.searchsorted(polls, grid, side="right") - 1
    fresh = np.array([i >= 0 and g - polls[i] <= 600 for g, i in zip(grid, pidx)])
    leg = {}
    spreads = {}
    for inst, rows in by_inst.items():
        ts = np.array([r[0] for r in rows])
        b = np.array([np.nan if r[1] is None else r[1] for r in rows])
        a = np.array([np.nan if r[2] is None else r[2] for r in rows])
        i = np.searchsorted(ts, grid, side="right") - 1
        bb = np.where(i >= 0, b[np.maximum(i, 0)], np.nan)
        aa = np.where(i >= 0, a[np.maximum(i, 0)], np.nan)
        leg[inst] = (bb, aa)
        sp = a - b
        sp = sp[np.isfinite(sp)]
        spreads[inst] = float(np.median(sp)) if len(sp) else T
    sb, sa, n = {}, {}, {}
    for r, exs in races.items():
        if not all(e in leg for e in exs):
            continue
        s1 = sum(leg[e][0] for e in exs)
        s2 = sum(leg[e][1] for e in exs)
        s1 = np.where(fresh, s1, np.nan)
        s2 = np.where(fresh, s2, np.nan)
        sb[r], sa[r], n[r] = s1, s2, len(exs)
    return Panel("B", grid, sb, sa, n, "real touch"), spreads, leg, fresh


def closes_from_candles(path):
    out = {}
    for ex, v in json.load(open(path)).items():
        cs = v.get("candles") or []
        if cs:
            out[ex] = (np.array([parse_iso(c["time"]) + STEP for c in cs]), np.array([c["close"] for c in cs]))
    return out


def closes_from_tape(tape):
    by = collections.defaultdict(list)
    for ex, px, at in tape:
        s = at.replace("Z", "+00:00")
        if "." in s:
            head, tail = s.split(".", 1)
            digits = "".join(ch for ch in tail if ch.isdigit())
            s = f"{head}.{digits[:6]}{tail[len(digits):]}"
        by[str(ex)].append((datetime.fromisoformat(s).timestamp(), px))
    out = {}
    for ex, rows in by.items():
        rows.sort()
        out[ex] = (np.array([r[0] for r in rows]), np.array([r[1] for r in rows]))
    return out


def roll_med(x, w):
    """Trailing median over w grid points (NaN-aware)."""
    if w <= 1:
        return x
    out = np.full_like(x, np.nan)
    for i in range(len(x)):
        s = x[max(0, i - w + 1):i + 1]
        s = s[np.isfinite(s)]
        if len(s):
            out[i] = np.median(s)
    return out


def proxy_panel(name, closes, races, spreads, grid, max_age=None, smooth=1):
    sb, sa, n, age = {}, {}, {}, {}
    for r, exs in races.items():
        if not all(e in closes for e in exs):
            continue
        tot = np.zeros(len(grid))
        hs = 0.0
        oldest = np.zeros(len(grid))
        ok = np.ones(len(grid), bool)
        for e in exs:
            ts, px = closes[e]
            i = np.searchsorted(ts, grid, side="right") - 1
            ok &= i >= 0
            tot += np.where(i >= 0, px[np.maximum(i, 0)], 0)
            oldest = np.maximum(oldest, np.where(i >= 0, grid - ts[np.maximum(i, 0)], 1e9))
            hs += spreads.get(e, T) / 2
        if max_age is not None:
            ok &= oldest <= max_age
        tot = roll_med(np.where(ok, tot, np.nan), smooth)
        sb[r] = tot - hs
        sa[r] = tot + hs
        n[r] = len(exs)
        age[r] = oldest
    return Panel(name, grid, sb, sa, n, "5-min close +- half spread"), age


# --------------------------------------------------------------------------------------- calibration
def calibrate(PB, tape_closes, races, spreads, smooth=1):
    """Proxy (tape closes +- half spread) vs real touch on panel B's grid."""
    P, age = proxy_panel("Bproxy", tape_closes, races, spreads, PB.grid, smooth=smooth)
    rows = []
    lev, dif = [], {}
    for r in PB.sb:
        if r not in P.sb:
            continue
        real_mid = (PB.sb[r] + PB.sa[r]) / 2
        prox_mid = (P.sb[r] + P.sa[r]) / 2
        m = np.isfinite(real_mid) & np.isfinite(prox_mid)
        if m.sum() < 50:
            continue
        e = (prox_mid - real_mid)[m] / T
        lev.extend(e.tolist())
        # change over 1 day (288 steps) and 6 h (72 steps)
        for lag, lab in ((12, "1h"), (72, "6h"), (288, "1d")):
            d_real = real_mid[lag:] - real_mid[:-lag]
            d_prox = prox_mid[lag:] - prox_mid[:-lag]
            mm = np.isfinite(d_real) & np.isfinite(d_prox)
            if mm.sum() > 10:
                dif.setdefault(lab, []).append((d_real[mm] / T, d_prox[mm] / T))
        rows.append((r, PB.n[r], float(np.mean(e)), float(np.std(e)), int(m.sum())))
    lev = np.array(lev)
    out = {"level_err_ticks": dict(mean=float(lev.mean()), sd=float(lev.std()), p05=float(np.percentile(lev, 5)),
                                   p95=float(np.percentile(lev, 95)), n=len(lev))}
    for lab, pairs in dif.items():
        dr = np.concatenate([p[0] for p in pairs])
        dp = np.concatenate([p[1] for p in pairs])
        out[f"change_{lab}"] = dict(corr=float(np.corrcoef(dr, dp)[0, 1]), sd_real=float(dr.std()), sd_proxy=float(dp.std()),
                                    sd_err=float((dp - dr).std()), n=len(dr))
    return out, rows


# ------------------------------------------------------------------------------- opportunity helpers
def qualify(P, r, x):
    sb, sa, n = P.sb[r], P.sa[r], P.n[r]
    e_s = sb - 1
    sell = (e_s >= SELL_MIN_EDGE - 1e-9) & (e_s / (n - sb) >= SELL_MIN_RET - 1e-9)
    e_b = 1 - sa - x
    buy = (e_b >= BUY_MIN_EDGE - 1e-9) & (e_b / sa >= BUY_MIN_RET - 1e-9)
    return np.nan_to_num(sell, nan=0).astype(bool), np.nan_to_num(buy, nan=0).astype(bool)


def carry(q, valid):
    """State used for 'fresh run' detection: at missing grid points keep the last observed state, so an outage does
    not end a run and restart it as a new one."""
    out = q.copy()
    for i in range(1, len(q)):
        if not valid[i]:
            out[i] = out[i - 1]
    return out


def entries(P, xt, fresh_only=True, hourly=False):
    """List of dicts: race, kind, i0 (grid index), P (hold profit), ret."""
    out = []
    for r in P.sb:
        sell, buy = qualify(P, r, xt.get(r, 0.003))
        valid = np.isfinite(P.sb[r]) & np.isfinite(P.sa[r])
        for kind, q in (("sell", sell), ("buy", buy)):
            qc = carry(q, valid)
            prev = np.concatenate([[False], qc[:-1]])
            if fresh_only:
                idx = np.where(q & ~prev)[0]
                idx = idx[idx > 0]          # the first grid point is not a 'fresh' run (state unknown before)
            else:
                idx = np.where(q)[0]
                if hourly:
                    idx = idx[(np.round((P.grid[idx] % 3600)) == 0)]
            for i in idx:
                if kind == "sell":
                    p = P.sb[r][i] - 1
                    ret = p / (P.n[r] - P.sb[r][i])
                else:
                    p = 1 - P.sa[r][i]
                    ret = (p - xt.get(r, 0.003)) / P.sa[r][i]
                out.append(dict(race=r, kind=kind, i0=int(i), P=float(p), ret=float(ret), n=P.n[r]))
    return out


def rt_path(P, e):
    """Round trip per set if exited at each grid index >= i0 (NaN where missing)."""
    r, i0 = e["race"], e["i0"]
    if e["kind"] == "sell":
        return P.sb[r][i0] - P.sa[r][i0:]
    return P.sb[r][i0:] - P.sa[r][i0]


def horizon_stats(P, ents, k_list=(0, 1, 2, 4, 6, 8)):
    """Per entry and horizon: share at h, best share within h (hindsight), and rule outcomes."""
    res = []
    N = len(P.grid)
    for e in ents:
        path = rt_path(P, e)
        Pp = e["P"]
        row = dict(e)
        row["t0"] = float(P.grid[e["i0"]])
        for lab, h in H_LIST:
            j = int(round(h / STEP))
            if e["i0"] + j >= N:
                continue
            seg = path[1:j + 1]
            v_h = path[j]
            if not np.isfinite(v_h):
                # last finite value within 30 min before h
                back = path[max(1, j - 6):j + 1]
                back = back[np.isfinite(back)]
                if not len(back):
                    continue
                v_h = back[-1]
            fin = seg[np.isfinite(seg)]
            best = float(fin.max()) if len(fin) else np.nan
            row[f"at_{lab}"] = float(v_h) / Pp
            row[f"best_{lab}"] = best / Pp
            for k in k_list:
                hit = np.where(np.nan_to_num(seg, nan=-9) >= k * T - 1e-9)[0]
                if len(hit):
                    row[f"rule{k}_{lab}"] = float(seg[hit[0]]) / Pp
                    row[f"rule{k}_t_{lab}"] = float((hit[0] + 1) * STEP)
                else:
                    row[f"rule{k}_{lab}"] = float(v_h) / Pp          # forced exit at h
                    row[f"rule{k}_t_{lab}"] = None
        res.append(row)
    return res


def pct(a, q):
    a = [x for x in a if x is not None and np.isfinite(x)]
    return float(np.percentile(a, q)) if a else float("nan")


# ------------------------------------------------------------------------------------------ strategy simulation
def race_cap(n, ret):
    """Per-race ceiling in currency, from config: max_race_frac 0.15, three_leg_race_frac 0.25, high_ret (>=4%) 0.40 of 100k."""
    if ret >= 0.04 and n == 2:
        return 40_000.0
    return 25_000.0 if n == 3 else 15_000.0


def trailing_mid(panels, w=288, every=6):
    """Trailing 24-h median of the mid sum per race over the concatenated panels (A then B). -> {panel name: {race: arr}}."""
    out = {P.name: {} for P in panels}
    races = set.intersection(*[set(P.sb) for P in panels])
    for r in races:
        mid = np.concatenate([(P.sb[r] + P.sa[r]) / 2 for P in panels])
        tm = np.full_like(mid, np.nan)
        for i in range(0, len(mid), every):
            s = mid[max(0, i - w + 1):i + 1]
            s = s[np.isfinite(s)]
            if len(s) >= w // 2:
                tm[i:i + every] = np.median(s)
        o = 0
        for P in panels:
            out[P.name][r] = tm[o:o + len(P.grid)]
            o += len(P.grid)
    return out


def candidates(P, xt, trail=None, dev=None, sell_ret=SELL_MIN_RET):
    """step index -> list of (ret, race, kind, price0, cost, hold P). dev (ticks): mid sum must exceed the trailing
    24-h median by >= dev ticks (sell) or be below it by >= dev ticks (buy)."""
    by = collections.defaultdict(list)
    for r in P.sb:
        x = xt.get(r, 0.003)
        sb, sa, n = P.sb[r], P.sa[r], P.n[r]
        mid = (sb + sa) / 2
        with np.errstate(invalid="ignore", divide="ignore"):
            e_s = sb - 1
            sell = (e_s >= SELL_MIN_EDGE - 1e-9) & (e_s / (n - sb) >= sell_ret - 1e-9)
            e_b = 1 - sa - x
            buy = (e_b >= BUY_MIN_EDGE - 1e-9) & (e_b / sa >= BUY_MIN_RET - 1e-9)
            if dev is not None:
                tm = trail[r] if trail is not None and r in trail else np.full(len(sb), np.nan)
                sell &= (mid - tm) >= dev * T - 1e-9
                buy &= (tm - mid) >= dev * T - 1e-9
        for i in np.where(np.nan_to_num(sell, nan=0).astype(bool))[0]:
            by[i].append((float(e_s[i] / (n - sb[i])), r, "sell", float(sb[i]), float(n - sb[i]), float(e_s[i])))
        for i in np.where(np.nan_to_num(buy, nan=0).astype(bool))[0]:
            by[i].append((float(e_b[i] / sa[i]), r, "buy", float(sa[i]), float(sa[i]), float(1 - sa[i])))
    for i in by:
        by[i].sort(reverse=True)
    return by


def simulate(P, cands, i_start, i_end, K=32_000.0, clip=2000, k=None, maxhold=None, cooldown=1800, min_qty=20, path=False):
    """Hold (k=None, maxhold=None) or churn. Both take free unwinds (exit value >= payout) and redeploy freed cash.
    Churn exits a lot when its round trip per set >= k ticks, or (maxhold) after maxhold seconds whatever the price.
    Fills: at most `clip` sets per race per 5-min step, each side, at the touch. Terminal: held sets marked at the touch
    exit value (cash objective) and at payout (settlement basis)."""
    cash = K
    lots = []
    committed = collections.defaultdict(float)
    last_exit = {}
    realised = 0.0
    n_entries = n_exits = 0
    traded = 0.0
    cd = int(cooldown / STEP)
    mh = int(maxhold / STEP) if maxhold else None
    vpath = []
    exit_stats = collections.defaultdict(lambda: dict(sets=0, R=0.0, n=0, hold_h=[]))
    race_R = collections.defaultdict(float)
    for i in range(i_start, i_end + 1):
        used = collections.defaultdict(int)
        # exits
        for lot in lots:
            if lot["qty"] <= 0:
                continue
            r = lot["race"]
            if lot["kind"] == "sell":
                px = P.sa[r][i]
                if not np.isfinite(px):
                    continue
                R = lot["p0"] - px
                proceeds = P.n[r] - px
            else:
                px = P.sb[r][i]
                if not np.isfinite(px):
                    continue
                R = px - lot["p0"]
                proceeds = px
            free = R >= lot["P"] - 1e-9
            trig = free or (k is not None and R >= k * T - 1e-9) or (mh is not None and i - lot["i0"] >= mh)
            if not trig:
                continue
            q = min(lot["qty"], clip - used[r])
            if q <= 0:
                continue
            why = "free" if free else ("k" if (k is not None and R >= k * T - 1e-9) else "maxhold")
            xs = exit_stats[why]
            xs["sets"] += q; xs["R"] += q * R; xs["n"] += 1; xs["hold_h"].append((i - lot["i0"]) * STEP / 3600)
            race_R[r] += q * R
            used[r] += q
            lot["qty"] -= q
            cash += q * proceeds
            committed[r] -= q * lot["cost"]
            realised += q * R
            traded += q * proceeds
            n_exits += 1
            last_exit[r] = i
        lots = [l for l in lots if l["qty"] > 0]
        # entries
        for ret, r, kind, p0, cost, Pp in cands.get(i, ()):
            if cash < min_qty * cost:
                break
            if r in last_exit and i - last_exit[r] < cd:
                continue
            if used[r] > 0:
                continue
            room = race_cap(P.n[r], ret) - committed[r]
            q = int(min(clip, min(cash, room) / cost))
            if q < min_qty:
                continue
            cash -= q * cost
            committed[r] += q * cost
            used[r] += q
            traded += q * cost
            n_entries += 1
            lots.append(dict(race=r, kind=kind, p0=p0, cost=cost, P=Pp, qty=q, i0=i))
        if path:
            v = cash
            for lot in lots:
                r = lot["race"]
                px = (P.sa[r][i] if lot["kind"] == "sell" else P.sb[r][i])
                if not np.isfinite(px):
                    px = lot.get("last_px", lot["p0"])
                lot["last_px"] = px
                v += lot["qty"] * ((P.n[r] - px) if lot["kind"] == "sell" else px)
            vpath.append((P.grid[i], v - K))
    # terminal marks: touch at the end, 'typical' = the race's median exit price over the whole panel (what a patient
    # exit on 3 Nov gets if the overround is stationary), and payout (settlement basis)
    mark_exit = mark_typ = mark_pay = 0.0
    for lot in lots:
        r = lot["race"]
        arr = P.sa[r] if lot["kind"] == "sell" else P.sb[r]
        seg = arr[i_start:i_end + 1]
        fin = seg[np.isfinite(seg)]
        px = fin[-1] if len(fin) else (lot["p0"])
        typ = float(np.nanmedian(arr))
        mark_exit += lot["qty"] * ((P.n[r] - px) if lot["kind"] == "sell" else px)
        mark_typ += lot["qty"] * ((P.n[r] - typ) if lot["kind"] == "sell" else typ)
        mark_pay += lot["qty"] * ((P.n[r] - 1) if lot["kind"] == "sell" else 1.0)
    return dict(cash_gain=cash + mark_exit - K, typ_gain=cash + mark_typ - K, pay_gain=cash + mark_pay - K, realised=realised, entries=n_entries,
                exits=n_exits, turnover=traded, held_cost=sum(l["qty"] * l["cost"] for l in lots), cash_end=cash,
                path=vpath, exit_stats=dict(exit_stats), race_R=dict(race_R), lots_end=[(l["race"], l["kind"], l["qty"]) for l in lots])


# ------------------------------------------------------------------------------------------------ our actual sets
def actual_sets(arb, PB):
    """FIFO lots of our sell/buy sets, matched against unwinds; touch value paths from panel B."""
    lots = collections.defaultdict(list)
    out_lots = []
    unw = []
    for _id, ts, race, kind, legs, qty, edge, cost in arb:
        if kind in ("sell_set", "buy_set"):
            k = "sell" if kind == "sell_set" else "buy"
            Pp = edge if k == "sell" else 1 - cost
            lot = dict(id=_id, ts=ts, race=race, kind=k, qty=qty, left=qty, P=Pp, cost=cost,
                       p0=(1 + edge) if k == "sell" else cost, realised=0.0, unwound=0, hold_s=[])
            lots[race].append(lot)
            out_lots.append(lot)
        elif kind == "unwind_set":
            q = qty
            unw.append((ts, race, qty, edge, cost))
            for lot in lots[race]:
                if q <= 0:
                    break
                if lot["left"] <= 0 or lot["kind"] != "sell":
                    continue
                take = min(q, lot["left"])
                lot["left"] -= take
                lot["unwound"] += take
                lot["realised"] += take * (lot["P"] + edge)      # round trip per set = entry edge + unwind edge
                lot["hold_s"].append((ts - lot["ts"], take))
                q -= take
    # value paths at h from panel B touch
    for lot in out_lots:
        i0 = int(np.searchsorted(PB.grid, lot["ts"]))
        r = lot["race"]
        if r not in PB.sb:
            continue
        path = (lot["p0"] - PB.sa[r][i0:]) if lot["kind"] == "sell" else (PB.sb[r][i0:] - lot["p0"])
        for lab, h in H_LIST:
            j = int(round(h / STEP))
            if j < len(path):
                seg = path[1:j + 1]
                fin = seg[np.isfinite(seg)]
                v = path[j] if np.isfinite(path[j]) else (fin[-1] if len(fin) else np.nan)
                lot[f"at_{lab}"] = v / lot["P"] if lot["P"] > 0 else np.nan
                lot[f"best_{lab}"] = (fin.max() / lot["P"]) if len(fin) and lot["P"] > 0 else np.nan
        fin = path[np.isfinite(path)]
        lot["now"] = fin[-1] if len(fin) else np.nan             # round trip per set at the last panel point
        lot["best_ever"] = fin[1:].max() if len(fin) > 1 else np.nan
    return out_lots, unw


# ---------------------------------------------------------------------------------------------- capacity
def touch_sizes(qs):
    by = collections.defaultdict(list)
    for ts, ex, bb, ba, bq, aq in qs:
        by[str(ex)].append((ts, bb, ba, bq, aq))
    out = {}
    for ex, rows in by.items():
        out[ex] = (np.array([r[0] for r in rows]), rows)
    return out


def size_at(ts_sizes, ex, t, side, px):
    v = ts_sizes.get(ex)
    if v is None:
        return None
    ts, rows = v
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > 300:
        return None
    _, bb, ba, bq, aq = rows[i]
    if side == "bid":
        return bq if bb is not None and abs(bb - px) < 1e-6 else None
    return aq if ba is not None and abs(ba - px) < 1e-6 else None


def runs(P, xt):
    """Qualifying runs (fresh entries) with duration until the race stops qualifying."""
    out = []
    for r in P.sb:
        sell, buy = qualify(P, r, xt.get(r, 0.003))
        valid = np.isfinite(P.sb[r]) & np.isfinite(P.sa[r])
        for kind, q in (("sell", sell), ("buy", buy)):
            q = carry(q, valid)
            i = 1
            N = len(q)
            while i < N:
                if q[i] and not q[i - 1]:
                    j = i
                    while j < N and q[j]:
                        j += 1
                    out.append(dict(race=r, kind=kind, i0=i, dur=(j - i) * STEP, censored=j >= N,
                                    P=float(P.sb[r][i] - 1 if kind == "sell" else 1 - P.sa[r][i]),
                                    cost=float(P.n[r] - P.sb[r][i] if kind == "sell" else P.sa[r][i]), n=P.n[r]))
                    i = j
                else:
                    i += 1
    return out


# ---------------------------------------------------------------------------------------------- main
def tbl(head, rows):
    s = "| " + " | ".join(head) + " |\n|" + "---|" * len(head) + "\n"
    for r in rows:
        s += "| " + " | ".join(str(x) for x in r) + " |\n"
    return s


def f2(x, nd=2):
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{nd}f}"


def pc(x):
    return "n/a" if x is None or not np.isfinite(x) else f"{100 * x:.0f}%"


def main():
    vq, arb, qs, tape, cyc = load_db()
    races, mk = races_from_markets()
    xt = x_table(races)
    PB, spreads, leg, fresh = panel_B(vq, races)
    tc = closes_from_tape(tape)
    cal1, _ = calibrate(PB, tc, races, spreads, smooth=1)
    cal6, cal_rows = calibrate(PB, tc, races, spreads, smooth=6)
    cc = closes_from_candles(os.path.join(DATA, "candles_5m.json"))
    PA, _ = proxy_panel("A", cc, races, spreads, np.arange(A0, A1 + 1, STEP), smooth=6)
    md = []
    res = {"calibration": {"raw": cal1, "smooth30min": cal6}}
    md.append("## 0. Proxy calibration (panel B period: tape closes vs real touch)\n")
    md.append(tbl(["proxy", "level error mean / sd (ticks of mid sum)", "1 h change corr / err sd", "6 h", "1 d"],
                  [[nm, f"{c['level_err_ticks']['mean']:+.2f} / {c['level_err_ticks']['sd']:.2f}"] +
                   [f"{c[f'change_{l}']['corr']:.2f} / {c[f'change_{l}']['sd_err']:.2f} (real sd {c[f'change_{l}']['sd_real']:.2f})"
                    for l in ("1h", "6h", "1d")] for nm, c in (("last close", cal1), ("30-min trailing median (used)", cal6))]))

    # ---- 1. overround levels, deviations and half-lives per race
    rows = []
    stats = {}
    for r in sorted(PB.sb):
        st = {}
        for P in (PA, PB):
            if r not in P.sb:
                continue
            mid = (P.sb[r] + P.sa[r]) / 2 - 1
            med = np.nanmedian(mid)
            dev = mid - med
            betas = {}
            for lag in (12, 72):
                a, b = dev[:-lag], dev[lag:]
                m_ = np.isfinite(a) & np.isfinite(b)
                betas[lag] = float((a[m_] * b[m_]).sum() / max((a[m_] ** 2).sum(), 1e-12)) if m_.sum() > 20 else np.nan
            b1 = betas[12]
            hl = (-math.log(2) / math.log(b1)) if 0 < b1 < 1 else np.nan
            sell, buy = qualify(P, r, xt.get(r, 0.003))
            st[P.name] = dict(med=med / T, sd=float(np.nanstd(dev)) / T, hl=hl, beta6=betas[72],
                              sb1=float(np.nanmedian(P.sb[r] - 1)) / T, sa1=float(np.nanmedian(P.sa[r] - 1)) / T,
                              q_sell=float(np.mean(sell)), q_buy=float(np.mean(buy)))
        stats[r] = st
    res["race_stats"] = stats
    agg = {}
    for nm in ("A", "B"):
        for nlegs in (2, 3):
            ss = [v[nm] for r, v in stats.items() if nm in v and PB.n.get(r) == nlegs]
            agg[(nm, nlegs)] = ss
    md.append("\n## 1. Overround levels and how fast deviations decay\n")
    md.append(tbl(["panel", "legs", "races", "median mid-overround (ticks)", "median sell edge sb-1", "median sell exit cost sa-1",
                   "sd of deviation from race median", "1-h persistence beta -> half-life", "6-h beta", "share of time sell-set qualifies"],
                  [[nm, nl, len(ss), f2(np.median([s['med'] for s in ss]), 1), f2(np.median([s['sb1'] for s in ss]), 1),
                    f2(np.median([s['sa1'] for s in ss]), 1), f2(np.median([s['sd'] for s in ss])),
                    f"{f2(np.nanmedian([s['hl'] for s in ss]) if False else np.nan)}" if False else
                    f"{f2(np.nanmedian([(s['hl'] if np.isfinite(s['hl']) else np.nan) for s in ss]), 1)} h",
                    f2(np.nanmedian([s['beta6'] for s in ss])), pc(np.mean([s['q_sell'] for s in ss]))]
                   for (nm, nl), ss in agg.items() if ss]))
    traded = collections.Counter()
    for _id, ts, race, kind, legs, qty, edge, cost in arb:
        if kind != "unwind_set":
            traded[race] += qty * cost
    top = [r for r, _ in traded.most_common(14)]
    md.append("\nRaces we traded (largest capital first), ticks of 0.005:\n")
    md.append(tbl(["race", "legs", "capital traded", "A: med sb-1 / sa-1", "A: dev sd, half-life", "B: med sb-1 / sa-1",
                   "B: dev sd, half-life", "B: sell qualifies"],
                  [[r, PB.n.get(r), f"{traded[r]:,.0f}",
                    f"{f2(stats[r]['A']['sb1'],1)} / {f2(stats[r]['A']['sa1'],1)}" if 'A' in stats.get(r, {}) else "n/a",
                    f"{f2(stats[r]['A']['sd'])}, {f2(stats[r]['A']['hl'],1)} h" if 'A' in stats.get(r, {}) else "n/a",
                    f"{f2(stats[r]['B']['sb1'],1)} / {f2(stats[r]['B']['sa1'],1)}",
                    f"{f2(stats[r]['B']['sd'])}, {f2(stats[r]['B']['hl'],1)} h", pc(stats[r]['B']['q_sell'])]
                   for r in top if r in stats]))

    # per race x day summary and 30-min paths (CSV)
    import csv
    with open(os.path.join(OUT, "race_day_overround.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["race", "legs", "day_utc", "panel", "points", "median_sum_bids_minus_1_ticks", "max_sum_bids_minus_1_ticks",
                    "median_sum_asks_minus_1_ticks", "min_sum_asks_minus_1_ticks", "share_sell_qualifies", "share_buy_qualifies"])
        for P in (PA, PB):
            days = np.array([datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d") for t in P.grid])
            for r in sorted(P.sb):
                sell, buy = qualify(P, r, xt.get(r, 0.003))
                for day in sorted(set(days)):
                    m_ = (days == day) & np.isfinite(P.sb[r])
                    if m_.sum() < 6:
                        continue
                    w.writerow([r, P.n[r], day, P.name, int(m_.sum()), round(float(np.median(P.sb[r][m_] - 1)) / T, 2),
                                round(float(np.max(P.sb[r][m_] - 1)) / T, 2), round(float(np.median(P.sa[r][m_] - 1)) / T, 2),
                                round(float(np.min(P.sa[r][m_] - 1)) / T, 2), round(float(sell[m_].mean()), 3), round(float(buy[m_].mean()), 3)])
    with open(os.path.join(OUT, "race_paths_30min.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["race", "legs", "ts_utc", "panel", "sum_bids", "sum_asks"])
        for P in (PA, PB):
            for r in sorted(P.sb):
                for i in range(0, len(P.grid), 6):
                    if np.isfinite(P.sb[r][i]):
                        w.writerow([r, P.n[r], datetime.fromtimestamp(P.grid[i], UTC).strftime("%Y-%m-%d %H:%M"), P.name,
                                    round(float(P.sb[r][i]), 4), round(float(P.sa[r][i]), 4)])

    # ---- 2. hypothetical entries
    H = {}
    for P, lab in ((PA, "IS (A, proxy)"), (PB, "OOS (B, real)")):
        for fo in (True, False):
            E = entries(P, xt, fresh_only=fo, hourly=not fo)
            H[(P.name, fo)] = horizon_stats(P, E)
    res["entries_n"] = {f"{k[0]}_{'fresh' if k[1] else 'hourly'}": len(v) for k, v in H.items()}
    md.append("\n## 2. Hypothetical entries: early-exit round trip as a share of the hold-to-settlement profit\n")
    md.append("Share = (round trip if exited at t0+h) / (payout - entry cost). 100% = as good as holding to settlement; "
              "negative = the exit costs more than the edge earned. `best` = best touch exit inside the horizon (hindsight).\n")
    hrows = []
    for (pn, fo), HH in H.items():
        for lab, _ in H_LIST:
            a = np.array([h[f"at_{lab}"] for h in HH if f"at_{lab}" in h])
            b = np.array([h[f"best_{lab}"] for h in HH if f"best_{lab}" in h])
            b = b[np.isfinite(b)]
            if len(a) < 10:
                continue
            hrows.append([("IS A proxy" if pn == "A" else "OOS B real"), "fresh runs" if fo else "hourly, all qualifying", lab, len(a),
                          pc(np.median(a)), f"{pc(np.percentile(a, 25))} .. {pc(np.percentile(a, 75))}", pc(np.mean(a)),
                          pc(np.median(b)), pc(np.mean(b >= 0)), pc(np.mean(b >= 0.25)), pc(np.mean(b >= 0.5)), pc(np.mean(b >= 1.0))])
    md.append(tbl(["panel", "entries", "h", "n", "median share at h", "IQR", "mean", "median best within h",
                   "P(best >= 0)", ">= 25%", ">= 50%", ">= 100%"], hrows))
    # by entry size
    md.append("\nBy entry edge (fresh runs, h = 1 d): small arbs are pure spread-crossing; large ones carry a deviation that decays.\n")
    rr = []
    for (pn, fo), HH in H.items():
        if not fo:
            continue
        for lo, hi, nm in ((0, 1.5 * T, "1 tick"), (1.5 * T, 3.5 * T, "2-3 ticks"), (3.5 * T, 1, ">= 4 ticks")):
            sel = [h for h in HH if lo <= h["P"] < hi and "at_1 d" in h]
            if len(sel) < 5:
                continue
            a = np.array([h["at_1 d"] for h in sel])
            b = np.array([h["best_1 d"] for h in sel])
            b = b[np.isfinite(b)]
            rr.append([pn, nm, len(sel), pc(np.median(a)), pc(np.mean(a)), pc(np.median(b)), pc(np.mean(b >= 0.5)),
                       f2(np.mean([h['P'] * h['at_1 d'] for h in sel]) / T, 2)])
    md.append(tbl(["panel", "entry edge", "n", "median share at 1 d", "mean", "median best in 1 d", "P(best >= 50%)",
                   "mean round trip at 1 d (ticks/set)"], rr))
    # rule table
    md.append("\nNon-hindsight rule: exit at the first touch where round trip >= k ticks per set, else forced exit at h "
              "(fresh runs). Mean share of hold profit, and trigger rate.\n")
    rr = []
    for (pn, fo), HH in H.items():
        if not fo:
            continue
        for k in (0, 1, 2, 4, 6, 8):
            row = [pn, k]
            for lab in ("6 h", "1 d", "2 d", "5 d"):
                a = [h[f"rule{k}_{lab}"] for h in HH if f"rule{k}_{lab}" in h]
                tr = [h[f"rule{k}_t_{lab}"] is not None for h in HH if f"rule{k}_{lab}" in h]
                row.append(f"{pc(np.mean(a))} ({pc(np.mean(tr))} hit)" if len(a) >= 10 else "n/a")
            rr.append(row)
    md.append(tbl(["panel", "k ticks", "h 6 h", "1 d", "2 d", "5 d"], rr))
    res["H"] = {f"{k[0]}_{k[1]}": v for k, v in H.items()}

    # ---- 3. our actual sets
    lots, unw = actual_sets(arb, PB)
    md.append("\n## 3. Our actual sets (arb_trades ok=1)\n")
    per = collections.defaultdict(lambda: dict(q=0, P=0.0, unw=0, real=0.0, forg=0.0, left=0, now=0.0, hold=[]))
    for l in lots:
        d = per[(l["race"], l["kind"])]
        d["q"] += l["qty"]; d["P"] += l["qty"] * l["P"]; d["unw"] += l["unwound"]; d["real"] += l["realised"]
        d["forg"] += l["unwound"] * l["P"]; d["left"] += l["left"]
        if np.isfinite(l.get("now", np.nan)):
            d["now"] += l["left"] * l["now"]
        d["hold"] += l["hold_s"]
    rows = []
    tot = collections.Counter()
    for (r, k), d in sorted(per.items(), key=lambda kv: -kv[1]["P"]):
        hs = [h for h, q in d["hold"]]
        rows.append([r, k, f"{d['q']:,}", f"{d['P']:,.0f}", f"{d['unw']:,}", f"{d['real']:+,.0f}",
                     pc(d['real'] / d['forg']) if d['forg'] > 0 else "n/a",
                     f"{np.median(hs) / 3600:.1f} h" if hs else "", f"{d['left']:,}", f"{d['now']:+,.0f}"])
        for kk in ("q", "P", "unw", "real", "forg", "left", "now"):
            tot[kk] += d[kk]
    rows.append(["TOTAL", "", f"{tot['q']:,}", f"{tot['P']:,.0f}", f"{tot['unw']:,}", f"{tot['real']:+,.0f}",
                 pc(tot['real'] / tot['forg']), "", f"{tot['left']:,}", f"{tot['now']:+,.0f}"])
    md.append(tbl(["race", "kind", "sets entered", "hold profit at entry", "sets unwound (FIFO)", "realised round trip",
                   "realised as % of their hold profit", "median hold before unwind", "sets left (ledger)",
                   "left: round trip if exited at the touch now"], rows))
    lr = []
    for lab, _ in H_LIST[:3]:
        a = [(l[f"at_{lab}"], l["qty"] * l["P"]) for l in lots if np.isfinite(l.get(f"at_{lab}", np.nan))]
        b = [(l[f"best_{lab}"], l["qty"] * l["P"]) for l in lots if np.isfinite(l.get(f"best_{lab}", np.nan))]
        if a:
            lr.append([lab, len(a), pc(np.average([x for x, _ in a], weights=[w for _, w in a])), pc(np.median([x for x, _ in a])),
                       pc(np.average([x for x, _ in b], weights=[w for _, w in b])), pc(np.mean([x >= 0.5 for x, _ in b]))])
    md.append("\nOur lots, touch exit at t0+h (share of each lot's hold profit; weighted by hold profit):\n")
    md.append(tbl(["h", "lots", "weighted share at h", "median share at h", "weighted best within h", "P(best >= 50%)"], lr))
    res["lots"] = [{k: v for k, v in l.items() if k != "hold_s"} for l in lots]

    # ---- 4. capacity
    ts_sz = touch_sizes(qs)
    md.append("\n## 4. Capacity and turnover\n")
    cap_rows = []
    RUNS = {}
    for P in (PA, PB):
        R = runs(P, xt)
        RUNS[P.name] = R
        days = (P.grid[-1] - P.grid[0]) / 86400
        for kind in ("sell", "buy"):
            rk = [x for x in R if x["kind"] == kind]
            if not rk:
                continue
            durs = np.array([x["dur"] for x in rk]) / 60
            big = [x for x in rk if x["P"] >= 2.5 * T]
            cap_rows.append([P.name, kind, len(rk), f"{len(rk) / days:.0f}", f"{len(big) / days:.0f}",
                             f"{np.median(durs):.0f} / {np.percentile(durs, 75):.0f} / {np.percentile(durs, 90):.0f}",
                             f"{np.median([x['P'] for x in rk]) / T:.1f}",
                             f"{len(set(x['race'] for x in rk))}"])
    md.append(tbl(["panel", "kind", "fresh runs", "per day", "per day with edge >= 3 ticks", "duration min p50/p75/p90",
                   "median entry edge (ticks)", "races"], cap_rows))
    # touch sizes on B at run onset
    szs = []
    for x in RUNS["B"]:
        t = PB.grid[x["i0"]]
        exs = races[x["race"]]
        side = "bid" if x["kind"] == "sell" else "ask"
        li = 0 if side == "bid" else 1
        vals = []
        for e in exs:
            px = leg[e][li][x["i0"]] if e in leg else None
            vals.append(size_at(ts_sz, e, t, side, px) if px is not None and np.isfinite(px) else None)
        if all(v is not None for v in vals):
            szs.append((min(vals), x["cost"], x["P"], x["dur"]))
    if szs:
        mins = np.array([s[0] for s in szs])
        capt = np.array([s[0] * s[1] for s in szs])
        md.append(f"\nTouch size at run onset (B, both legs observed in the quotes table, {len(szs)} of {len(RUNS['B'])} runs): "
                  f"min-leg size p25/p50/p75 = {np.percentile(mins, 25):,.0f} / {np.median(mins):,.0f} / {np.percentile(mins, 75):,.0f} sets; "
                  f"capital at one touch p50 {np.median(capt):,.0f}, sum over all runs {capt.sum():,.0f} "
                  f"({capt.sum() / ((PB.grid[-1] - PB.grid[0]) / 86400):,.0f} per day).\n")
        res["touch"] = dict(n=len(szs), p50=float(np.median(mins)), cap_sum=float(capt.sum()))
    # ---- 5. strategy simulations
    trail = trailing_mid([PA, PB])
    md.append("\n## 5. Hold vs churn simulations\n")
    DEVS = (None, 2, 4)
    candA = {dev: candidates(PA, xt, trail["A"], dev) for dev in DEVS}
    candB = {dev: candidates(PB, xt, trail["B"], dev) for dev in DEVS}
    NA, NB = len(PA.grid), len(PB.grid)
    winA = [(s, s + 576 - 1) for s in range(0, NA - 576 + 1, 144)]
    winB = [(s, s + 288 - 1) for s in range(0, NB - 288 + 1, 36)]
    dA, dB = 2.0, 1.0
    K = 32_000.0
    MARK = "typ_gain"

    def run_set(P, cand, wins, **kw):
        return [simulate(P, cand, a, b, K=K, **kw) for a, b in wins]

    def per1k_day(xs, days):
        return np.array(xs) / days / (K / 1000)

    holdA = {dev: run_set(PA, candA[dev], winA) for dev in DEVS}
    holdB = {dev: run_set(PB, candB[dev], winB) for dev in DEVS}
    holdA_full = {dev: simulate(PA, candA[dev], 0, NA - 1, K=K, path=True) for dev in DEVS}
    holdB_full = {dev: simulate(PB, candB[dev], 0, NB - 1, K=K, path=True) for dev in DEVS}
    daysA_full = (PA.grid[-1] - PA.grid[0]) / 86400
    daysB_full = (PB.grid[-1] - PB.grid[0]) / 86400
    hrows = []
    for dev in DEVS:
        hA, hB = holdA[dev], holdB[dev]
        hrows.append([("off" if dev is None else f">= {dev} ticks"),
                      f"{np.mean([h[MARK] for h in hA]):+,.0f} [{np.percentile([h[MARK] for h in hA], 10):+,.0f}, {np.percentile([h[MARK] for h in hA], 90):+,.0f}]",
                      f"{np.mean([h['cash_gain'] for h in hA]):+,.0f}",
                      f"{np.mean([h[MARK] for h in hB]):+,.0f} [{np.percentile([h[MARK] for h in hB], 10):+,.0f}, {np.percentile([h[MARK] for h in hB], 90):+,.0f}]",
                      f"{np.mean([h['cash_gain'] for h in hB]):+,.0f}",
                      f"{holdA_full[dev][MARK]:+,.0f} / {holdA_full[dev]['pay_gain']:+,.0f}",
                      f"{holdB_full[dev][MARK]:+,.0f} / {holdB_full[dev]['pay_gain']:+,.0f}"])
    md.append(f"Capital {K:,.0f} (the current arb book at cost); at most 2,000 sets per race per 5-min step each way at the "
              "touch; per-race caps 15k (2-leg) / 25k (3-leg) / 40k (2-leg >= 4%); 30-min re-entry cooldown after an exit; "
              "both strategies take free unwinds (exit value >= payout) and redeploy freed cash. Terminal value marks every "
              "held set at the race's TYPICAL exit price (median over the panel: what a patient exit on 3 Nov gets if the "
              "overround is stationary); the end-of-window touch mark is also shown. IS = 12 rolling 2-day windows of A "
              "(start every 12 h), OOS = 8 rolling 1-day windows of B (start every 3 h): overlapping, so roughly 4 and 2 "
              "independent samples. Brackets: 10th-90th percentile across windows.\n\n")
    md.append("Hold (no exits except free unwinds), terminal gain per window:\n")
    md.append(tbl(["entry deviation filter", "IS per 2-day window, typical mark", "IS touch mark", "OOS per 1-day window, typical mark",
                   "OOS touch mark", f"full A ({daysA_full:.1f} d) typical / settlement basis",
                   f"full B ({daysB_full:.1f} d) typical / settlement basis"], hrows))
    variants = [(dev, k, mh) for dev in DEVS for k in (0, 1, 2, 3, 4, 6) for mh in (None, 86400)]
    vres = []
    for dev, k, mh in variants:
        ca = run_set(PA, candA[dev], winA, k=k, maxhold=mh)
        cb = run_set(PB, candB[dev], winB, k=k, maxhold=mh)
        exA = per1k_day([c[MARK] - h[MARK] for c, h in zip(ca, holdA[None])], dA)
        exB = per1k_day([c[MARK] - h[MARK] for c, h in zip(cb, holdB[None])], dB)
        sdA = per1k_day([c[MARK] - h[MARK] for c, h in zip(ca, holdA[dev])], dA)
        sdB = per1k_day([c[MARK] - h[MARK] for c, h in zip(cb, holdB[dev])], dB)
        fa = simulate(PA, candA[dev], 0, NA - 1, K=K, k=k, maxhold=mh)
        fb = simulate(PB, candB[dev], 0, NB - 1, K=K, k=k, maxhold=mh)
        vres.append(dict(dev=dev, k=k, mh=mh, exA=float(exA.mean()), exA_lo=float(np.percentile(exA, 10)),
                         exA_hi=float(np.percentile(exA, 90)), exB=float(exB.mean()), exB_lo=float(np.percentile(exB, 10)),
                         exB_hi=float(np.percentile(exB, 90)), sdA=float(sdA.mean()), sdB=float(sdB.mean()),
                         sdB_lo=float(np.percentile(sdB, 10)), sdB_hi=float(np.percentile(sdB, 90)),
                         fullA=fa[MARK], fullB=fb[MARK], fullA_touch=fa["cash_gain"], fullB_touch=fb["cash_gain"],
                         payA=fa["pay_gain"], payB=fb["pay_gain"], turnA=fa["turnover"], turnB=fb["turnover"],
                         exitsA=fa["exits"], exitsB=fb["exits"]))
    vres.sort(key=lambda v: -v["exA"])
    best = vres[0]
    res["variants"] = vres
    md.append("\nChurn variants, sorted by IS excess. Excess = churn minus the CURRENT hold rule (no deviation filter), per "
              "1,000 of capital per day, typical mark; `vs hold same filter` isolates the value of the exits alone.\n")
    vrows = []
    shown = vres[:10] + [x for x in vres if x["dev"] is None and x["mh"] is None and x not in vres[:10]]
    for v in shown:
        vrows.append([("off" if v["dev"] is None else f">= {v['dev']} ticks"), v["k"], "none" if v["mh"] is None else "1 d",
                      f"{v['exA']:+.1f} [{v['exA_lo']:+.1f}, {v['exA_hi']:+.1f}]", f"{v['exB']:+.1f} [{v['exB_lo']:+.1f}, {v['exB_hi']:+.1f}]",
                      f"{v['sdA']:+.1f} / {v['sdB']:+.1f} [{v['sdB_lo']:+.1f}, {v['sdB_hi']:+.1f}]",
                      f"{v['fullA']:+,.0f} / {v['fullB']:+,.0f}", f"{v['fullA_touch']:+,.0f} / {v['fullB_touch']:+,.0f}",
                      f"{v['payA']:+,.0f} / {v['payB']:+,.0f}", f"{v['turnA'] / 1e3:,.0f}k / {v['turnB'] / 1e3:,.0f}k"])
    md.append(tbl(["entry deviation filter", "exit k ticks", "max hold", "IS excess /1k/day", "OOS excess /1k/day",
                   "vs hold same filter IS / OOS", "full A / B gain, typical mark", "touch mark", "settlement basis",
                   "turnover A / B"], vrows))
    figC = []
    for lab, h in H_LIST:
        ca = run_set(PA, candA[best["dev"]], winA, k=best["k"], maxhold=h)
        cb = run_set(PB, candB[best["dev"]], winB, k=best["k"], maxhold=h) if h <= 86400 else None
        ex_A = per1k_day([c[MARK] - hh[MARK] for c, hh in zip(ca, holdA[None])], dA).tolist()
        ex_B = per1k_day([c[MARK] - hh[MARK] for c, hh in zip(cb, holdB[None])], dB).tolist() if cb else None
        figC.append(dict(h=h, lab=lab, exA=ex_A, exB=ex_B))
    res["figC"] = figC
    sens = []
    for clip in (500, 2000, 5000):
        for KK in (32_000.0, 64_000.0):
            ra = simulate(PA, candA[best["dev"]], 0, NA - 1, K=KK, clip=clip, k=best["k"], maxhold=best["mh"])
            ha = simulate(PA, candA[None], 0, NA - 1, K=KK, clip=clip)
            rb = simulate(PB, candB[best["dev"]], 0, NB - 1, K=KK, clip=clip, k=best["k"], maxhold=best["mh"])
            hb = simulate(PB, candB[None], 0, NB - 1, K=KK, clip=clip)
            sens.append([clip, f"{KK:,.0f}", f"{ha[MARK]:+,.0f}", f"{ra[MARK]:+,.0f}", f"{hb[MARK]:+,.0f}", f"{rb[MARK]:+,.0f}",
                         f"{(rb[MARK] - hb[MARK]) / daysB_full:+,.0f}"])
    md.append(f"\nSensitivity of the IS-chosen rule (deviation filter {best['dev']}, k {best['k']}, max hold "
              f"{best['mh']} s), full windows, typical mark:\n")
    md.append(tbl(["clip sets/step", "capital", f"hold A ({daysA_full:.1f} d)", "churn A", f"hold B ({daysB_full:.1f} d)",
                   "churn B", "B churn - hold per day"], sens))
    # proxy inflation check: the same rule on a tape-close proxy of panel B (same times and races as the real touch)
    PBp, _ = proxy_panel("Bp", tc, races, spreads, PB.grid, smooth=6)
    for r in list(PBp.sb):
        if r not in PB.sb:
            del PBp.sb[r], PBp.sa[r]
            continue
        m_ = np.isfinite(PB.sb[r])
        PBp.sb[r] = np.where(m_, PBp.sb[r], np.nan)
        PBp.sa[r] = np.where(m_, PBp.sa[r], np.nan)
    trp = trailing_mid([PA, PBp])["Bp"]
    infl = []
    for P_, tr_, nm in ((PB, trail["B"], "real touch"), (PBp, trp, "tape-close proxy")):
        h_ = simulate(P_, candidates(P_, xt, tr_, None), 0, NB - 1, K=K)
        c_ = simulate(P_, candidates(P_, xt, tr_, best["dev"]), 0, NB - 1, K=K, k=best["k"], maxhold=best["mh"])
        infl.append([nm, f"{h_[MARK]:+,.0f}", f"{c_[MARK]:+,.0f}", f"{c_[MARK] - h_[MARK]:+,.0f}", c_["exits"]])
    res["inflation"] = infl
    md.append("\nProxy inflation check on panel B's period (same rule, same races and times):\n")
    md.append(tbl(["prices", "hold", "churn", "churn - hold", "churn exits"], infl))
    # opportunity flow of the chosen entry filter
    flow = []
    for P, cand, nm in ((PA, candA[best["dev"]], "A"), (PB, candB[best["dev"]], "B")):
        days = (P.grid[-1] - P.grid[0]) / 86400
        seen = set()
        fresh_n = 0
        for i in sorted(cand):
            now = {(c[1], c[2]) for c in cand[i]}
            fresh_n += len(now - seen)
            seen = now
        flow.append([nm, f"{fresh_n / days:.1f}", f"{np.mean([len(cand.get(i, ())) for i in range(len(P.grid))]):.1f}"])
    md.append("\nFlow of entries passing the chosen filter:\n")
    md.append(tbl(["panel", "fresh (race, kind) qualifying per day", "races qualifying at a random moment"], flow))
    churnA_full = simulate(PA, candA[best["dev"]], 0, NA - 1, K=K, k=best["k"], maxhold=best["mh"], path=True)
    churnB_full = simulate(PB, candB[best["dev"]], 0, NB - 1, K=K, k=best["k"], maxhold=best["mh"], path=True)
    res["paths"] = dict(holdA=holdA_full[None]["path"], holdB=holdB_full[None]["path"],
                        holddevA=holdA_full[best["dev"]]["path"], holddevB=holdB_full[best["dev"]]["path"],
                        churnA=churnA_full["path"], churnB=churnB_full["path"])
    for nm, rr_ in (("A", churnA_full), ("B", churnB_full)):
        es = rr_["exit_stats"]
        md.append(f"\nChosen rule, full panel {nm}: exits by trigger: " + "; ".join(
            f"{w}: {v['n']} exits, {v['sets']:,} sets, round trip {v['R']:+,.0f} ({v['R'] / max(v['sets'], 1) / T:+.2f} ticks/set), "
            f"median hold {np.median(v['hold_h']):.1f} h" for w, v in es.items()) + ". Realised round trip by race (top): " +
            ", ".join(f"{r} {v:+,.0f}" for r, v in sorted(rr_["race_R"].items(), key=lambda kv: -abs(kv[1]))[:8]) + ".\n")
    res["best"] = best
    res["holdwin"] = {str(dev): dict(A=[h[MARK] for h in holdA[dev]], B=[h[MARK] for h in holdB[dev]]) for dev in DEVS}
    # ---- 6. current holdings and where their exit cost sits now
    md.append("\n## 6. Current book (ledger remainder) and its exit cost now vs its history\n")
    hold_rows = []
    for (r, k), d in sorted(per.items(), key=lambda kv: -kv[1]["left"]):
        if d["left"] < 50 or r not in PB.sb:
            continue
        arr = (PB.sa[r] - 1) if k == "sell" else (1 - PB.sb[r])
        fin = arr[np.isfinite(arr)]
        if not len(fin):
            continue
        now = fin[-1]
        arrA = (PA.sa[r] - 1) if k == "sell" else (1 - PA.sb[r])
        finA = arrA[np.isfinite(arrA)]
        hold_rows.append([r, k, f"{d['left']:,}", f2(now / T, 1), pc(np.mean(fin <= now + 1e-9)), f2(np.percentile(fin, 10) / T, 1),
                          f2(np.median(fin) / T, 1), f2(np.median(finA) / T, 1) if len(finA) else "n/a"])
    md.append(tbl(["race", "kind", "sets (ledger)", "exit cost now (ticks/set)", "B percentile of now", "B p10", "B median", "A median"],
                  hold_rows))
    open(os.path.join(OUT, "tables.md"), "w").write("\n".join(md))
    pickle_out = dict(res=res, PA=PA, PB=PB, lots=lots, unw=unw, arb=arb, races=races, cal_rows=cal_rows)
    import pickle
    pickle.dump(pickle_out, open(os.path.join(OUT, "results.pkl"), "wb"))
    json.dump({k: v for k, v in res.items() if k in ("calibration", "entries_n", "variants", "best", "touch", "holdwin")},
              open(os.path.join(OUT, "results.json"), "w"), indent=1, default=float)
    figures(pickle_out)
    print("\n".join(md))


# Plain-language names used on every figure and in arb_convergence.md.
TRAIN_NAME = "Training days (14-21 Oct, estimated from trade prices)"
TEST_NAME = "Test days (22-23 Oct, real quotes)"
HOLD_NAME = "Hold to settlement (current rule)"
HOLDDEV_NAME = "Hold, buy only when unusually cheap"
NOHINDSIGHT = "without hindsight (a rule we could actually run)"
HINDSIGHT = "with hindsight (best possible exit)"
OUR_NAME = "Our actual sets"


def churn_name(best):
    mh = f"{best['mh'] / 3600:.0f} h" if best.get("mh") else "no time limit"
    return f"Buy when unusually cheap, sell after +{best['k']} ticks or {mh}"


def _header(ax, title, subtitle=None):
    """Title (one line) and optional subtitle (one line) drawn in their own row, so constrained layout keeps them clear."""
    ax.axis("off")
    ax.text(0.5, 1.0, title, ha="center", va="top", fontsize=15, fontweight="bold", transform=ax.transAxes)
    if subtitle:
        ax.annotate(subtitle, xy=(0.5, 1.0), xycoords="axes fraction", xytext=(0, -24), textcoords="offset points",
                    ha="center", va="top", fontsize=11, color="0.25")


def _glossary(ax, lines, ncol=2, width=60, fontsize=9):
    """Boxed glossary in its own row: one term per entry, wrapped to `width` characters, split into `ncol` columns."""
    import textwrap
    from matplotlib.patches import FancyBboxPatch
    ax.axis("off")
    ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.01", transform=ax.transAxes,
                                facecolor="#f6f6f2", edgecolor="0.6", lw=0.8, clip_on=False))
    ax.text(0.01, 0.97, "Glossary", ha="left", va="top", fontsize=fontsize + 1, fontweight="bold", transform=ax.transAxes)
    wrapped = []
    for term, expl in lines:
        w = textwrap.wrap(f"{term}: {expl}", width=width, subsequent_indent="    ")
        wrapped.append("\n".join(w))
    per = math.ceil(len(wrapped) / ncol)
    for c in range(ncol):
        ax.text(0.01 + c / ncol, 0.86, "\n".join(wrapped[c * per:(c + 1) * per]), ha="left", va="top",
                fontsize=fontsize, linespacing=1.35, transform=ax.transAxes)


GLOSS_ARB = ("arb set", "one contract on every outcome of a race; together they are guaranteed to pay a fixed amount at settlement")
GLOSS_TICK = ("tick", "0.005, the smallest price step (half a cent)")
GLOSS_GAP = ("gap to payout (overround)", "how far a set's price is from what it pays at settlement; the profit per set if held")
GLOSS_RT = ("round trip", "buy the set, then sell it back, after paying the spread (best ask vs best bid) both ways")
GLOSS_TOUCH = ("touch", "the best bid / best ask currently on offer; every sale here is at the touch")
GLOSS_CHEAP = ("unusually cheap", "the race's gap to payout is at least 4 ticks wider than its own median of the last 24 hours")
GLOSS_TRAIN = ("training days", "rules were chosen on these days; prices are 5-minute trade prices +/- half the usual spread")
GLOSS_TEST = ("test days", "real best bids and asks, used only to check the rules after they were fixed")


def figures(d):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    res, PA, PB, lots, arb = d["res"], d["PA"], d["PB"], d["lots"], d["arb"]
    os.makedirs(FIG, exist_ok=True)
    dt = lambda t: datetime.fromtimestamp(t, UTC)
    rng = np.random.default_rng(7)
    C_IS, C_OOS, C_OUR = "tab:blue", "tab:red", "tab:green"
    best = res["best"]
    CHURN_NAME = churn_name(best)
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10, "xtick.labelsize": 9.5,
                         "ytick.labelsize": 9.5, "legend.fontsize": 9.5})

    # ---------------- figure 1: per-race paths
    traded = collections.Counter()
    for _id, ts, race, kind, legs, qty, edge, cost in arb:
        if kind != "unwind_set":
            traded[race] += qty * cost
    top = [r for r, _ in traded.most_common(12)]
    fig = plt.figure(figsize=(14, 22.5), layout="constrained")
    gs = fig.add_gridspec(8, 2, height_ratios=[0.8] + [1] * 6 + [0.95])
    hax = fig.add_subplot(gs[0, :])
    first = None
    axs = np.empty((6, 2), dtype=object)
    for i in range(6):
        for j in range(2):
            axs[i, j] = fig.add_subplot(gs[1 + i, j], sharex=first)
            first = first or axs[i, j]
    for ax, r in zip(axs.flat, top):
        for P, alpha, lab in ((PA, 0.35, "A: 5-min trade closes +- half spread (proxy)"), (PB, 0.9, "B: real touch")):
            if r not in P.sb:
                continue
            x = [dt(t) for t in P.grid]
            ax.fill_between(x, (P.sb[r] - 1) / T, (P.sa[r] - 1) / T, color="0.5", alpha=alpha * 0.5, lw=0)
            ax.plot(x, (P.sb[r] - 1) / T, color="tab:blue", lw=0.7, alpha=alpha)
            ax.plot(x, (P.sa[r] - 1) / T, color="tab:orange", lw=0.7, alpha=alpha)
        ax.axhline(0, color="k", lw=0.8)
        for _id, ts, race, kind, legs, qty, edge, cost in arb:
            if race != r:
                continue
            s = 10 + 60 * min(qty, 5000) / 5000
            if kind == "sell_set":
                ax.scatter([dt(ts)], [edge / T], marker="^", color="tab:green", s=s, zorder=5, edgecolor="k", lw=0.3)
            elif kind == "buy_set":
                ax.scatter([dt(ts)], [(cost - 1) / T], marker="v", color="tab:purple", s=s, zorder=5, edgecolor="k", lw=0.3)
            else:
                ax.scatter([dt(ts)], [-edge / T], marker="x", color="tab:red", s=s, zorder=5, lw=1)
        ax.set_title(f"{r}: {PB.n.get(r)} outcomes, we traded {traded[r]:,.0f} in cash", fontsize=11)
        ax.set_ylabel("ticks per set")
        ax.grid(alpha=0.3)
        ax.axvline(dt(B0), color="k", ls=":", lw=0.8)
    for ax in axs[:-1].flat:
        ax.tick_params(labelbottom=False)
    for ax in axs[-1]:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        ax.set_xlabel("date (UTC)")
    _header(hax, "Arb set prices in our 12 most-traded races",
            "A NO set sold early makes money only if the orange line later falls below where the blue line was when we bought")
    hax.legend(handles=[
        Line2D([], [], color="tab:blue", lw=2, label="Gap to payout when buying a NO set (= its profit if held to settlement)"),
        Line2D([], [], color="tab:orange", lw=2, label="Gap to payout when selling a NO set back (below 0: a YES set is that far below payout)"),
        Patch(color="0.5", alpha=0.45, label="Shaded: the cost of an immediate round trip (orange minus blue)"),
        Line2D([], [], color="k", ls=":", lw=1, label=f"Dotted line: {TRAIN_NAME[0].lower() + TRAIN_NAME[1:]} on its left, {TEST_NAME[0].lower() + TEST_NAME[1:]} on its right"),
        Line2D([], [], marker="^", color="tab:green", markeredgecolor="k", ls="", ms=8, label="We bought a NO set (drawn at its gap; bigger = more sets)"),
        Line2D([], [], marker="v", color="tab:purple", markeredgecolor="k", ls="", ms=8, label="We bought a YES set (drawn at its gap; bigger = more sets)"),
        Line2D([], [], marker="x", color="tab:red", ls="", ms=8, mew=1.5, label="We sold a set back early (drawn at its sell-back gap)")],
        loc="lower center", bbox_to_anchor=(0.5, 0.0), ncol=2, frameon=False, fontsize=10)
    _glossary(fig.add_subplot(gs[7, :], label="glossary"), [
        GLOSS_ARB, ("NO set / YES set", "NO on every outcome (pays outcomes minus 1) / YES on every outcome (pays 1)"),
        GLOSS_TICK, GLOSS_GAP,
        ("blue line", "sum of the best YES bids minus 1, in ticks: what a NO set bought now earns if held"),
        ("orange line", "sum of the best YES asks minus 1, in ticks: what selling a NO set back now gives up"),
        GLOSS_RT, GLOSS_TOUCH, GLOSS_TRAIN, GLOSS_TEST,
        ("outcomes", "the candidates (legs) in the race; a 3-outcome race needs 3 contracts per set")], ncol=2, width=72, fontsize=9.5)
    fig.savefig(os.path.join(FIG, "arb_convergence_paths.png"), dpi=110)
    plt.close(fig)

    # ---------------- figure 2: against holding time h
    H = res["H"]
    hx = [h for _, h in H_LIST]
    labs = [l for l, _ in H_LIST]
    fig = plt.figure(figsize=(11, 21.5), layout="constrained")
    gs = fig.add_gridspec(5, 1, height_ratios=[0.2, 1.25, 1.05, 1.05, 0.78])
    _header(fig.add_subplot(gs[0]), "Selling arb sets early vs holding to settlement",
            "Panels A, B: every set the bot's current rule would have bought. Panel C: the selling rule run on 32k of cash.")
    axs = [fig.add_subplot(gs[1])]
    axs += [fig.add_subplot(gs[i], sharex=axs[0]) for i in (2, 3)]
    ax = axs[0]
    for key, color, ls, name in (("A_True", C_IS, ":", TRAIN_NAME), ("B_True", C_OOS, "-", TEST_NAME)):
        rows = H[key]
        xs, med, lo, hi = [], [], [], []
        for lab, h in H_LIST:
            a = np.array([r[f"at_{lab}"] for r in rows if f"at_{lab}" in r])
            if len(a) < 10:
                continue
            xs.append(h); med.append(np.median(a) * 100); lo.append(np.percentile(a, 25) * 100); hi.append(np.percentile(a, 75) * 100)
            sub = a if len(a) <= 250 else rng.choice(a, 250, replace=False)
            ax.scatter(h * np.exp(rng.normal(0, 0.06, len(sub))), np.clip(sub * 100, -480, 380), s=5, color=color, alpha=0.15)
        ax.plot(xs, med, ls=ls, marker="o", color=color, label=f"{name}, {NOHINDSIGHT}: median of {len(rows)} sets")
        ax.fill_between(xs, lo, hi, color=color, alpha=0.12)
        bm = [np.nanmedian([r[f"best_{lab}"] for r in rows if f"best_{lab}" in r]) * 100 for lab, h in H_LIST if h in xs]
        ax.plot(xs, bm, ls=ls, marker="s", color=color, alpha=0.5, label=f"{name}, {HINDSIGHT}: median")
    xs, med, lo, hi = [], [], [], []
    for lab, h in H_LIST:
        a = np.array([l[f"at_{lab}"] for l in lots if np.isfinite(l.get(f"at_{lab}", np.nan))])
        if len(a) >= 10:
            xs.append(h); med.append(np.median(a) * 100); lo.append(np.percentile(a, 25) * 100); hi.append(np.percentile(a, 75) * 100)
            ax.scatter(h * np.exp(rng.normal(0, 0.06, len(a))), np.clip(a * 100, -480, 380), s=6, color=C_OUR, alpha=0.25)
    ax.plot(xs, med, "-", marker="D", color=C_OUR, label=f"{OUR_NAME} (real quotes), if sold at that time: median")
    ax.fill_between(xs, lo, hi, color=C_OUR, alpha=0.1)
    ax.axhline(100, color="k", lw=0.8, ls="--")
    ax.axhline(0, color="k", lw=0.8)
    ax.text(0.2, 104, "100% = as good as holding to settlement", transform=ax.get_yaxis_transform(), fontsize=9, va="bottom")
    ax.text(0.2, 4, "0% = no profit", transform=ax.get_yaxis_transform(), fontsize=9, va="bottom")
    ax.set_ylim(-500, 400)
    ax.set_title("A. Profit if a set is sold after a fixed holding time", loc="left", fontweight="bold")
    ax.set_ylabel("Profit from selling early,\nas % of the profit from holding")
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.03), ncol=1, frameon=False)
    ax.grid(alpha=0.3)
    ax.tick_params(labelbottom=True)
    ax = axs[1]
    for key, ls, name in (("A_True", ":", "IS"), ("B_True", "-", "OOS")):
        rows = H[key]
        for X, color in ((0.25, "tab:cyan"), (0.5, "tab:purple"), (1.0, "tab:brown")):
            xs, ys, lo, hi = [], [], [], []
            for lab, h in H_LIST:
                b = np.array([r[f"best_{lab}"] for r in rows if f"best_{lab}" in r])
                b = b[np.isfinite(b)]
                if len(b) < 10:
                    continue
                p = np.mean(b >= X)
                se = math.sqrt(p * (1 - p) / len(b))
                xs.append(h); ys.append(100 * p); lo.append(100 * (p - 1.96 * se)); hi.append(100 * (p + 1.96 * se))
            ax.plot(xs, ys, ls=ls, marker="o", color=color)
            ax.fill_between(xs, lo, hi, color=color, alpha=0.1)
    ax.set_ylim(0, 100)
    ax.set_title(f"B. How often a good early sale existed at some point, {HINDSIGHT}", loc="left", fontweight="bold")
    ax.set_ylabel("% of sets where selling early\nwould have earned at least ...")
    ax.legend(handles=[Line2D([], [], color=c, marker="o", label=f"... {int(X * 100)}% of the profit from holding")
                       for X, c in ((0.25, "tab:cyan"), (0.5, "tab:purple"), (1.0, "tab:brown"))]
              + [Line2D([], [], color="0.3", ls=":", label=f"dotted: {TRAIN_NAME}"),
                 Line2D([], [], color="0.3", ls="-", label=f"solid: {TEST_NAME}")],
              loc="upper left", bbox_to_anchor=(0, -0.03), ncol=2, frameon=False)
    ax.grid(alpha=0.3)
    ax.tick_params(labelbottom=True)
    ax = axs[2]
    for key, color, ls, name in (("exA", C_IS, ":", f"{TRAIN_NAME}: average over 2-day stretches"),
                                 ("exB", C_OOS, "-", f"{TEST_NAME}: average over 1-day stretches")):
        xs, m, lo, hi = [], [], [], []
        for row in res["figC"]:
            v = row[key]
            if not v:
                continue
            xs.append(row["h"]); m.append(np.mean(v)); lo.append(np.percentile(v, 10)); hi.append(np.percentile(v, 90))
            ax.scatter(row["h"] * np.exp(rng.normal(0, 0.04, len(v))), v, s=10, color=color, alpha=0.3)
        ax.plot(xs, m, ls=ls, marker="o", color=color, label=name)
        ax.fill_between(xs, lo, hi, color=color, alpha=0.12)
    ax.axhline(0, color="k", lw=0.8)
    ax.text(0.005, 3, "above 0: selling rule beats holding", transform=ax.get_yaxis_transform(), fontsize=9, va="bottom")
    ax.set_title(f"C. \"{CHURN_NAME.split(' or ')[0]}\" minus \"{HOLD_NAME}\"", loc="left", fontweight="bold")
    ax.set_ylabel("Extra profit vs holding,\nper 1,000 of cash per day")
    ax.set_xscale("log")
    ax.set_xticks(hx)
    ax.set_xticklabels(labs)
    ax.set_xlabel("Holding time before selling (log scale). In C: the longest a set may be held before it is sold anyway\n"
                  f"(the chosen rule, \"{CHURN_NAME}\", uses 24 h)")
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.2), ncol=1, frameon=False)
    ax.grid(alpha=0.3)
    _glossary(fig.add_subplot(gs[4], label="glossary"), [
        GLOSS_ARB, GLOSS_TICK, GLOSS_GAP,
        ("profit from holding", "the gap to payout when the set was bought, collected if it is kept until the election settles"),
        ("current rule", "buy a set when its gap to payout is at least 0.5% (NO sets) or 2.4% (YES sets) of its cost"),
        GLOSS_RT, GLOSS_TOUCH, GLOSS_CHEAP,
        (HINDSIGHT.split(" (")[0], "the best sale price reached at any time up to then (not knowable in advance)"),
        (NOHINDSIGHT.split(" (")[0], "sell at exactly the time on the x axis, whatever the price"),
        ("median", "the middle value: half the sets did better, half worse"),
        ("shaded bands", "A: range of the middle 50% of sets; B: 95% uncertainty range; C: range covering 80% of the stretches"),
        ("dots", "A: single sets (values beyond the chart edge are drawn at the edge); C: single stretches of days"),
        ("stretches", "overlapping 2-day (training) or 1-day (test) periods, each simulated separately"),
        GLOSS_TRAIN, GLOSS_TEST], ncol=2, width=68, fontsize=9)
    fig.savefig(os.path.join(FIG, "arb_convergence_horizon.png"), dpi=110)
    plt.close(fig)

    # ---------------- figure 3: strategy value paths
    paths = res["paths"]
    fig = plt.figure(figsize=(11, 12.5), layout="constrained")
    gs = fig.add_gridspec(4, 1, height_ratios=[0.34, 1, 1, 0.47])
    hax = fig.add_subplot(gs[0])
    axs = [fig.add_subplot(gs[1]), fig.add_subplot(gs[2])]
    for ax, P_ in ((axs[0], "A"), (axs[1], "B")):
        for key, color, name in (("hold", "0.3", HOLD_NAME), ("holddev", "tab:orange", HOLDDEV_NAME), ("churn", "tab:red", CHURN_NAME)):
            p = paths[key + P_]
            ax.plot([dt(t) for t, _ in p], [v for _, v in p], color=color, ls=":" if P_ == "A" else "-", label=name)
        ax.axhline(0, color="k", lw=0.8)
        ax.set_title(TRAIN_NAME if P_ == "A" else TEST_NAME, loc="left", fontweight="bold")
        ax.set_ylabel("Gain on 32k of cash")
        ax.grid(alpha=0.3)
    axs[0].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    axs[0].set_xlabel("date (UTC)")
    axs[1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b %H:%M"))
    axs[1].set_xlabel("time (UTC)")
    _header(hax, "Three ways to use 32k of cash on arb sets",
            "Value = cash plus every set still held, counted at what it would sell for right now")
    hh, ll = axs[1].get_legend_handles_labels()
    hax.legend(hh, ll, loc="lower center", bbox_to_anchor=(0.5, -0.05), ncol=1, frameon=False, fontsize=10.5)
    _glossary(fig.add_subplot(gs[3], label="glossary"), [
        GLOSS_ARB, GLOSS_TICK, GLOSS_GAP, GLOSS_CHEAP,
        (f"+{best['k']} ticks", f"sell as soon as the round trip earns {best['k']} ticks per set, then reuse the cash"),
        GLOSS_RT, ("what it would sell for", "each set is valued at the touch, the best bid currently on offer"),
        GLOSS_TRAIN, GLOSS_TEST,
        ("line style", "dotted on training days, solid on test days; colours are the same strategies")], ncol=2, width=66, fontsize=9)
    fig.savefig(os.path.join(FIG, "arb_convergence_strategy.png"), dpi=110)
    plt.close(fig)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--figures":
        import pickle
        figures(pickle.load(open(os.path.join(OUT, "results.pkl"), "rb")))
    else:
        main()
