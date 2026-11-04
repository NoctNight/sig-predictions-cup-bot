"""Ladder exits: sell arb sets in increments at rising price levels (holding some for a better price) vs all at once.

Read-only research (Sizhe, 24 Oct). Opens data/sigbot.sqlite read-only (URI mode=ro), reads data/books_history/*.json and
data/research_arb_convergence/race_day_overround.csv. Writes only data/research_ladder_exits/ (small json + tables) and
research_notes/figures/ladder_exits_decision.png. No venue calls.

    nice -n 5 <venv with numpy+matplotlib>/bin/python research_notes/ladder_exits_study.py [--figures]

Exit cost of one set, in ticks (0.005), at the touch (n legs, YES quotes):
  sell-set (NO on every leg, pays n-1): exit = buy YES at the asks, cost c = (sum asks - 1) / tick
  buy-set  (YES on every leg, pays 1):  exit = sell YES at the bids, cost c = (1 - sum bids) / tick
A clip of k sets also walks the book: per-set cost c(t) + I(k), I from the depth snapshots (median over snapshots).
Lower c is better. A "level" is a cost target: m0 + delta, m0 = the race's trailing 24-h median cost at the decision.

Panels (real SIG touch, venue_quotes, 2-min grid, forward-filled, legs > 30 min stale dropped, polls > 10 min old dropped):
  warm-up 21 Oct 23:50 - 22 Oct 12:00 UTC (trailing medians only)
  IS  (training)  22 Oct 12:00 - 23 Oct 12:00 UTC
  OOS (test)      23 Oct 12:00 UTC - end of data (24 Oct ~13:00)
Horizons beyond the real data (2-10 days) use synthetic paths: 6-h blocks of the race's own deviation from its trailing
median (from that half only), plus a level path = the change in a same-leg-count race's daily median cost over 14-23 Oct.
"""
from __future__ import annotations

import collections
import csv
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "research_ladder_exits")
FIG = os.path.join(HERE, "research_notes", "figures")
UTC = timezone.utc
T = 0.005
STEP = 120.0
B0 = datetime(2026, 10, 21, 23, 50, tzinfo=UTC).timestamp()
IS0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC).timestamp()
OOS0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC).timestamp()
DAY = 86400.0
TARGETS = {  # race: (kind, sets held 24 Oct, legs)
    "Rhode Island Governor": ("sell", 12304),
    "MN-05 House race": ("buy", 21162),
    "Hawaii Governor": ("buy", 7658),
}
DELTAS = [3, 2, 1, 0, -1, -2, -3, -4, -6]          # level = trailing median + delta ticks (negative = better than usual)
H_REAL = [1, 3, 6, 12, 24]                           # hours, on real paths
H_SYN = [24, 48, 72, 120, 168, 240]                  # hours, on synthetic paths
RNG = np.random.default_rng(7)


def ts_iso(t):
    return datetime.fromtimestamp(t, UTC).strftime("%d %b %H:%M")


# ------------------------------------------------------------------------------------------------ data
def load():
    con = sqlite3.connect("file:" + os.path.join(DATA, "sigbot.sqlite") + "?mode=ro", uri=True)
    vq = con.execute("SELECT ts, race, party, instrument, bid, ask FROM venue_quotes WHERE venue='sig' ORDER BY ts").fetchall()
    arb = con.execute("SELECT ts, race, kind, quantity, legs FROM arb_trades WHERE ok=1 AND ts > ? ORDER BY ts", (B0,)).fetchall()
    tape = con.execute("SELECT exchange_id, price, quantity, executed_at, aggressor FROM tape WHERE executed_at >= '2026-10-21T23'").fetchall() \
        if "aggressor" in [r[1] for r in con.execute("PRAGMA table_info(tape)")] else []
    con.close()
    return vq, arb, tape


def build(vq):
    polls = np.array(sorted({r[0] for r in vq}))
    grid = np.arange(B0, polls[-1] + 1, STEP)
    pi = np.searchsorted(polls, grid, side="right") - 1
    fresh = (pi >= 0) & (grid - polls[np.maximum(pi, 0)] <= 600)
    by, races = collections.defaultdict(list), collections.defaultdict(set)
    for ts, race, party, inst, b, a in vq:
        by[inst].append((ts, b, a))
        races[race].add(inst)
    leg = {}
    for inst, rows in by.items():
        ts = np.array([r[0] for r in rows])
        b = np.array([np.nan if r[1] is None else r[1] for r in rows])
        a = np.array([np.nan if r[2] is None else r[2] for r in rows])
        i = np.searchsorted(ts, grid, side="right") - 1
        ok = (i >= 0) & (grid - ts[np.maximum(i, 0)] <= 1800) & fresh
        leg[inst] = (np.where(ok, b[np.maximum(i, 0)], np.nan), np.where(ok, a[np.maximum(i, 0)], np.nan))
    races = {r: sorted(ex) for r, ex in races.items() if len(ex) in (2, 3)}
    cost = {}
    for r, ex in races.items():
        sb = sum(leg[e][0] for e in ex)
        sa = sum(leg[e][1] for e in ex)
        cost[(r, "sell")] = (sa - 1) / T
        cost[(r, "buy")] = (1 - sb) / T
    return grid, races, leg, cost


def trailing_median(grid, c, w=DAY, min_h=12 * 3600):
    """Trailing 24-h median of c at each grid point (needs >= 12 h of history); every 15 min, carried forward."""
    out = np.full(len(c), np.nan)
    every = int(900 / STEP)
    last = np.nan
    for i in range(len(c)):
        if i % every == 0:
            lo = np.searchsorted(grid, grid[i] - w)
            s = c[lo:i + 1]
            s = s[np.isfinite(s)]
            last = np.median(s) if (grid[i] - grid[lo] >= min_h and len(s) >= 0.5 * min_h / STEP) else np.nan
        out[i] = last
    return out


# ------------------------------------------------------------------------------------------- depth
def depth_impact(races, kinds_q):
    """I(k): extra ticks per set from walking k sets beyond the touch, summed over legs, per depth snapshot.
    kinds_q: {(race, kind): [k...]}. Returns {(race, kind): {k: [per-snapshot ticks]}} and the touch cost per snapshot."""
    out = collections.defaultdict(lambda: collections.defaultdict(list))
    for path in sorted(glob.glob(os.path.join(DATA, "books_history", "*.json"))):
        b = json.load(open(path))
        for (r, kind), ks in kinds_q.items():
            ex = races[r]
            if not all(e in b for e in ex):
                continue
            side = "asks" if kind == "sell" else "bids"
            for k in ks:
                extra = 0.0
                for e in ex:
                    lv = b[e][side]
                    if not lv:
                        extra = np.nan
                        break
                    left, spent = k, 0.0
                    for x in lv:
                        t = min(left, x["quantity"])
                        spent += t * x["price"]
                        left -= t
                        if left <= 0:
                            break
                    if left > 0:
                        extra = np.nan
                        break
                    extra += abs(spent / k - lv[0]["price"]) / T
                out[(r, kind)][k].append(extra)
    return out


# ------------------------------------------------------------------------------------- own impact
def own_impact(arb, grid, races, leg):
    """After each of our taker set trades: change in the traded side's sum (ticks, + = against a repeat trade)
    at +2..+120 min vs 2 min before. Control: the same race at random times."""
    lags = [2, 10, 30, 60, 120]
    rows = []
    for ts, race, kind, q, legs in arb:
        if race not in races:
            continue
        ex = races[race]
        side = 1 if kind in ("unwind_set", "buy_set") else 0          # 1: we bought YES at the asks; 0: sold YES at the bids
        s = sum(leg[e][side] for e in ex) / T
        sign = 1.0 if side == 1 else -1.0                              # buying lifts asks; selling lowers bids
        i0 = np.searchsorted(grid, ts - 120) - 1
        if i0 < 0 or not np.isfinite(s[i0]):
            continue
        r = [q]
        for L in lags:
            j = np.searchsorted(grid, ts + L * 60)
            r.append(sign * (s[j] - s[i0]) if j < len(grid) and np.isfinite(s[j]) else np.nan)
        rows.append(r)
    a = np.array(rows, float)
    ctrl = []
    keys = list(races)
    for _ in range(3000):
        r = keys[RNG.integers(len(keys))]
        side = RNG.integers(2)
        s = sum(leg[e][side] for e in races[r]) / T
        i0 = RNG.integers(len(grid) - 70)
        ctrl.append([abs(s[i0 + 1 + L // 2] - s[i0]) if np.isfinite(s[i0]) else np.nan for L in lags])
    return lags, a, np.array(ctrl)


# ------------------------------------------------------------------------------------------ policies
def run_policy(c, i0, i_end, m0, Q, k, I_k, I_rem_fn, tiers, cool):
    """Sell Q sets in clips of k on the path c[i0:i_end] (ticks), one clip per `cool` grid steps at most.
    tiers: list of (fraction, delta) - that fraction of the clips is sold when c <= m0 + delta (delta None = at once).
    Clips are taken loosest level first. Remainder sold at i_end-1 walked all at once (cost c + I(remainder)).
    Returns (mean cost per set in ticks, mean time to cash in steps, remainder fraction)."""
    n = int(np.ceil(Q / k))
    left = []
    for f, d in tiers:
        left.append([np.inf if d is None else m0 + d, int(round(f * n))])
    tot = sum(x[1] for x in left)
    left[-1][1] += n - tot
    left.sort(key=lambda x: -x[0])
    seg = c[i0:i_end - 1]
    fin = np.isfinite(seg)
    cand = [i0 + np.flatnonzero(fin & (seg <= lv[0])) for lv in left]     # qualifying grid points per tier
    spent, sold, tsum, nxt = 0.0, 0, 0.0, i0
    while sold < Q:
        best, bt = None, None
        for t, lv in enumerate(left):                # earliest qualifying point; ties go to the loosest tier
            if lv[1] <= 0:
                continue
            j = np.searchsorted(cand[t], nxt)
            if j < len(cand[t]) and (best is None or cand[t][j] < best):
                best, bt = int(cand[t][j]), t
        if best is None:
            break
        q = min(k, Q - sold)
        spent += q * (c[best] + I_k)
        sold += q
        tsum += q * (best - i0)
        left[bt][1] -= 1
        nxt = best + cool
    rem = Q - sold
    if rem > 0:
        j = i_end - 1
        while j > i0 and not np.isfinite(c[j]):
            j -= 1
        spent += rem * (c[j] + I_rem_fn(rem))
        tsum += rem * (i_end - 1 - i0)
    return spent / Q, tsum / Q, rem / Q


def dump_cost(c, i0, Q, I_fn):
    return c[i0] + I_fn(Q)


# --------------------------------------------------------------------------------- synthetic paths
def daily_level_paths():
    """Per leg count, list of daily median exit-cost sequences (ticks) for 14-23 Oct (sell side; buy side mirrors it)."""
    rows = list(csv.DictReader(open(os.path.join(DATA, "research_arb_convergence", "race_day_overround.csv"))))
    d = collections.defaultdict(dict)
    for r in rows:
        d[(r["race"], int(r["legs"]))][r["day_utc"]] = float(r["median_sum_asks_minus_1_ticks"])
    seqs = collections.defaultdict(list)
    for (race, legs), dd in d.items():
        days = sorted(dd)
        if len(days) >= 8:
            seqs[legs].append(np.array([dd[x] for x in days]))
    return seqs


def synth_paths(dev, n_paths, n_steps, level_seqs, block=180):
    """dev: deviations from the trailing median (finite values, contiguous half). Stationary block bootstrap + level path."""
    dev = dev[np.isfinite(dev)]
    out = np.empty((n_paths, n_steps))
    spd = int(DAY / STEP)
    for p in range(n_paths):
        x = []
        while len(x) < n_steps:
            s = RNG.integers(0, max(1, len(dev) - block))
            x.extend(dev[s:s + block])
        x = np.array(x[:n_steps])
        seq = level_seqs[RNG.integers(len(level_seqs))]
        d0 = RNG.integers(0, max(1, len(seq) - int(np.ceil(n_steps / spd))))
        lv = seq[d0:] - seq[d0]
        if RNG.integers(2):                      # buy side mirrors the sell side; sign either way
            lv = -lv
        day = np.minimum(np.arange(n_steps) // spd, len(lv) - 1)
        frac = (np.arange(n_steps) % spd) / spd
        nxt = np.minimum(day + 1, len(lv) - 1)
        out[p] = x + lv[day] * (1 - frac) + lv[nxt] * frac
    return out


# ------------------------------------------------------------------------------------------- main
def tstat(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def main():
    os.makedirs(OUT, exist_ok=True)
    vq, arb, tape = load()
    grid, races, leg, cost = build(vq)
    i_is, i_oos = np.searchsorted(grid, IS0), np.searchsorted(grid, OOS0)
    halves = {"IS": (i_is, i_oos), "OOS": (i_oos, len(grid))}
    res = {"grid": [float(grid[0]), float(grid[-1]), len(grid)], "halves": {k: [ts_iso(grid[a]), ts_iso(grid[b - 1])] for k, (a, b) in halves.items()}}
    print("grid", ts_iso(grid[0]), "-", ts_iso(grid[-1]), len(grid), "races", len(races))

    med = {key: trailing_median(grid, c) for key, c in cost.items()}
    dev = {key: cost[key] - med[key] for key in cost}

    # -- 1. own impact and refill
    lags, imp, ctrl = own_impact(arb, grid, races, leg)
    res["own_impact"] = {"lags_min": lags, "n": len(imp),
                         "all_median": [float(np.nanmedian(imp[:, j + 1])) for j in range(len(lags))],
                         "all_mean": [float(np.nanmean(imp[:, j + 1])) for j in range(len(lags))],
                         "big_mean": [float(np.nanmean(imp[imp[:, 0] >= 2000, j + 1])) for j in range(len(lags))],
                         "big_n": int((imp[:, 0] >= 2000).sum()),
                         "ctrl_abs_mean": [float(np.nanmean(ctrl[:, j])) for j in range(len(lags))]}
    print("own impact", json.dumps(res["own_impact"]))

    # -- 2. depth: I(k)
    KS = [250, 500, 1000, 2000, 3000, 5000, 8000, 12304, 21162]
    kq = {(r, kind): KS for r, (kind, Q) in TARGETS.items()}
    for r in races:
        if len(races[r]) == 2:
            for kind in ("sell", "buy"):
                kq.setdefault((r, kind), [500, 1000, 2000])
    imp_k = depth_impact(races, kq)
    Ifun = {}
    for key, d in imp_k.items():
        ks = sorted(d)
        vals = [np.nanmedian(d[k]) if np.isfinite(d[k]).any() else np.nan for k in ks]
        Ifun[key] = (np.array(ks, float), np.array(vals, float))
    res["depth"] = {f"{r}|{kind}": {str(k): [None if not np.isfinite(x) else round(float(x), 2) for x in imp_k[(r, kind)][k]] for k in KS}
                    for r, (kind, Q) in TARGETS.items()}

    def I_of(key, q):
        ks, v = Ifun.get(key, (np.array([1.0]), np.array([0.0])))
        ok = np.isfinite(v)
        if not ok.any():
            return 0.0
        return float(np.interp(q, np.r_[0, ks[ok]], np.r_[0, v[ok]], right=v[ok][-1] + (q - ks[ok][-1]) / ks[ok][-1] * v[ok][-1]))

    # -- 3. level-hit probabilities on real paths: P(min c over (t0, t0+h] <= m0 + delta), expected wait, adverse move
    hit = {}
    for half, (a, b) in halves.items():
        for legs in (2, 3):
            keys = [k for k in cost if len(races[k[0]]) == legs]
            for h in H_REAL:
                H = int(h * 3600 / STEP)
                P = {d: [] for d in DELTAS}
                W = {d: [] for d in DELTAS}
                adv, fin = [], []
                for key in keys:
                    c, m = cost[key], med[key]
                    pr = {d: [] for d in DELTAS}
                    for i0 in range(a, b - H, int(3600 / STEP)):
                        if not (np.isfinite(c[i0]) and np.isfinite(m[i0])):
                            continue
                        seg = c[i0 + 1:i0 + 1 + H]
                        if np.isfinite(seg).mean() < 0.7:
                            continue
                        mn = np.fmin.accumulate(np.where(np.isfinite(seg), seg, np.inf))
                        for d in DELTAS:
                            j = np.argmax(mn <= m[i0] + d) if (mn <= m[i0] + d).any() else -1
                            pr[d].append(j >= 0)
                            if j >= 0:
                                W[d].append((j + 1) * STEP / 3600)
                        adv.append(np.nanmax(seg) - c[i0])
                        fin.append(seg[np.isfinite(seg)][-1] - c[i0])
                    for d in DELTAS:
                        if pr[d]:
                            P[d].append(np.mean(pr[d]))
                hit[(half, legs, h)] = {
                    "p": {d: (float(np.mean(P[d])) if P[d] else np.nan, float(np.std(P[d]) / np.sqrt(max(1, len(P[d]))))) for d in DELTAS},
                    "wait": {d: (float(np.median(W[d])) if W[d] else np.nan) for d in DELTAS},
                    "adv_p95": float(np.percentile(adv, 95)) if adv else np.nan,
                    "fin_p5": float(np.percentile(fin, 5)) if fin else np.nan, "fin_p95": float(np.percentile(fin, 95)) if fin else np.nan,
                    "n_races": len(P[0])}
    res["hit_real"] = {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in hit.items()}

    # -- 4. synthetic long horizons (pooled per leg count), same quantities
    seqs = daily_level_paths()
    syn = {}
    for half, (a, b) in halves.items():
        for legs in (2, 3):
            keys = [k for k in cost if len(races[k[0]]) == legs]
            nmax = int(max(H_SYN) * 3600 / STEP)
            npath = 40 if legs == 2 else 400
            P = {(h, d): [] for h in H_SYN for d in DELTAS}
            W = {(h, d): [] for h in H_SYN for d in DELTAS}
            adv = {h: [] for h in H_SYN}
            fin = {h: [] for h in H_SYN}
            for key in keys:
                dv = dev[key][a:b]
                if np.isfinite(dv).sum() < 0.5 * (b - a):
                    continue
                paths = synth_paths(dv, npath, nmax + 1, seqs[legs])
                x0 = paths[:, 0:1]
                mn = np.fmin.accumulate(np.where(np.isfinite(paths[:, 1:]), paths[:, 1:], np.inf), axis=1)
                mx = np.fmax.accumulate(np.where(np.isfinite(paths[:, 1:]), paths[:, 1:], -np.inf), axis=1)
                for h in H_SYN:
                    H = int(h * 3600 / STEP)
                    for d in DELTAS:
                        got = mn[:, H - 1] <= d          # m0 = trailing median at the start ~ level 0 of the path
                        P[(h, d)].append(got.mean())
                        if got.any():
                            first = np.argmax(mn[:, :H] <= d, axis=1)[got]
                            W[(h, d)].extend(((first + 1) * STEP / 3600).tolist())
                    adv[h].extend((mx[:, H - 1] - x0[:, 0]).tolist())
                    fin[h].extend((paths[:, H] - x0[:, 0]).tolist())
            for h in H_SYN:
                syn[(half, legs, h)] = {"p": {d: (float(np.mean(P[(h, d)])), float(np.std(P[(h, d)]) / np.sqrt(len(P[(h, d)])))) for d in DELTAS},
                                        "wait": {d: (float(np.median(W[(h, d)])) if W[(h, d)] else np.nan) for d in DELTAS},
                                        "adv_p95": float(np.percentile(adv[h], 95)), "fin_p5": float(np.percentile(fin[h], 5)),
                                        "fin_p95": float(np.percentile(fin[h], 95))}
    res["hit_syn"] = {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in syn.items()}

    # -- 5. policies. Generic 2-leg pool (Q=2000 sets, clips of 500) on real paths (horizon 12 h and 24 h), per race, then
    #       our three positions on real + synthetic paths.
    COOL = int(30 * 60 / STEP)
    ladders = {
        "all now (one clip)": None,
        "clips from now (every 30 min)": [(1.0, None)],
    }
    for d in DELTAS:
        ladders[f"single level {d:+d}"] = [(1.0, d)]
    for f0 in (0.25, 0.5, 0.75):
        for d in (0, -1, -2, -3, -4):
            ladders[f"{int(f0*100)}% now, rest at {d:+d}"] = [(f0, None), (1 - f0, d)]
    for spec in ([(1/3, 0), (1/3, -2), (1/3, -4)], [(1/3, 1), (1/3, -1), (1/3, -3)], [(0.25, None), (0.25, 0), (0.25, -2), (0.25, -4)],
                 [(0.5, 0), (0.5, -2)], [(0.5, -1), (0.5, -3)]):
        ladders["ladder " + " / ".join(("now" if d is None else f"{d:+d}") + f" {int(round(f*100))}%" for f, d in spec)] = spec

    def eval_pool(half, h, Q=2000, k=500, step_h=1):
        a, b = halves[half]
        H = int(h * 3600 / STEP)
        per = collections.defaultdict(list)          # policy -> per-race mean gain vs clips-now (ticks)
        per_t = collections.defaultdict(list)        # policy -> per-race mean time to cash (h)
        per_key = []
        per_abs = collections.defaultdict(list)
        allv = collections.defaultdict(list)
        for key in cost:
            if len(races[key[0]]) != 2:
                continue
            c, m = cost[key], med[key]
            Ik = I_of(key, k)
            g = collections.defaultdict(list)
            for i0 in range(a, b - H, int(step_h * 3600 / STEP)):
                if not (np.isfinite(c[i0]) and np.isfinite(m[i0])) or np.isfinite(c[i0:i0 + H]).mean() < 0.7:
                    continue
                base = run_policy(c, i0, i0 + H, m[i0], Q, k, Ik, lambda q: I_of(key, q), [(1.0, None)], COOL)[0]
                for name, spec in ladders.items():
                    if spec is None:
                        v, tt = dump_cost(c, i0, Q, lambda q: I_of(key, q)), 0.0
                    else:
                        v, tt, _ = run_policy(c, i0, i0 + H, m[i0], Q, k, Ik, lambda q: I_of(key, q), spec, COOL)
                    g[name].append(base - v)          # + = cheaper exit than clips-from-now
                    g["_t_" + name].append(tt * STEP / 3600)
            if not g:
                continue
            per_key.append(key)
            for name, xs in g.items():
                if name.startswith("_t_"):
                    per_t[name[3:]].append(np.mean(xs))
                    continue
                per[name].append(np.mean(xs))
                allv[name].extend(xs)
        out = {}
        for name in ladders:
            x = np.array(per[name])
            ref = np.array(per["single level +0"])
            out[name] = {"mean": float(np.mean(x)) if len(x) else np.nan, "t": float(tstat(x)), "n_races": len(x),
                         "p5": float(np.percentile(allv[name], 5)) if allv[name] else np.nan,
                         "time_h": float(np.mean(per_t[name])) if per_t[name] else np.nan,
                         "vs_level0": float(np.mean(x - ref)) if len(x) else np.nan, "vs_level0_t": float(tstat(x - ref)) if len(x) else np.nan}
        return out

    if "--pool-syn" in sys.argv:
        # 2-leg pool on simulated long horizons: per race-side, 20 paths from that half's swings + borrowed level drift
        res = json.load(open(os.path.join(OUT, "results.json")))
        names = ["all now (one clip)", "single level +1", "single level +0", "single level -1", "single level -2",
                 "single level -4", "50% now, rest at -2", "ladder +0 50% / -2 50%", "ladder +0 33% / -2 33% / -4 33%",
                 "ladder +1 33% / -1 33% / -3 33%", "ladder now 25% / +0 25% / -2 25% / -4 25%"]
        out = {}
        for half, (a, b) in halves.items():
            for h in (72, 240):
                n = int(h * 3600 / STEP) + 1
                per = collections.defaultdict(list)
                per_t = collections.defaultdict(list)
                allv = collections.defaultdict(list)
                for key in cost:
                    if len(races[key[0]]) != 2:
                        continue
                    dv = dev[key][a:b]
                    if np.isfinite(dv).sum() < 0.5 * (b - a):
                        continue
                    Ik = I_of(key, 500)
                    paths = synth_paths(dv, 20, n, seqs[2])
                    g = collections.defaultdict(list)
                    for p in paths:
                        base = run_policy(p, 0, n, 0.0, 2000, 500, Ik, lambda q: I_of(key, q), [(1.0, None)], COOL)[0]
                        for name in names:
                            spec = ladders[name]
                            if spec is None:
                                v, tt = dump_cost(p, 0, 2000, lambda q: I_of(key, q)), 0.0
                            else:
                                v, tt, _ = run_policy(p, 0, n, 0.0, 2000, 500, Ik, lambda q: I_of(key, q), spec, COOL)
                            g[name].append(base - v)
                            g["_t_" + name].append(tt * STEP / 3600)
                    for name in names:
                        per[name].append(np.mean(g[name]))
                        per_t[name].append(np.mean(g["_t_" + name]))
                        allv[name].extend(g[name])
                ref = np.array(per["single level +0"])
                out[f"{half}|{h}"] = {name: {"mean": float(np.mean(per[name])), "t": float(tstat(per[name])),
                                             "p5": float(np.percentile(allv[name], 5)), "time_h": float(np.mean(per_t[name])),
                                             "vs_level0": float(np.mean(np.array(per[name]) - ref)),
                                             "vs_level0_t": float(tstat(np.array(per[name]) - ref)), "n_races": len(per[name])}
                                      for name in names}
                for name in names:
                    x = out[f"{half}|{h}"][name]
                    print(f"{half} {h:3d}h {name:42s} {x['mean']:+.2f} t{x['t']:+.1f} p5 {x['p5']:+.1f} {x['time_h']:5.1f}h vsL0 {x['vs_level0']:+.2f} t{x['vs_level0_t']:+.1f}")
        res["pool_syn"] = out
        json.dump(res, open(os.path.join(OUT, "results.json"), "w"))
        return
    if "--pool-only" in sys.argv:
        res = json.load(open(os.path.join(OUT, "results.json")))
    pool = {}
    for half in halves:
        for h in (3, 6, 12):
            pool[(half, h)] = eval_pool(half, h)
            best = max(pool[(half, h)].items(), key=lambda kv: kv[1]["mean"])
            print(half, h, "best", best[0], best[1])
    res["pool"] = {f"{k[0]}|{k[1]}": v for k, v in pool.items()}
    if "--pool-only" in sys.argv:
        json.dump(res, open(os.path.join(OUT, "results.json"), "w"))
        return

    # our positions, synthetic horizons to the deadline (deviations from each half; level path from 14-23 Oct)
    tgt = {}
    for r, (kind, Q) in TARGETS.items():
        key = (r, kind)
        legs = len(races[r])
        for k in (500, 1000, 2000, 3000):
            pass
        clip_grid = [1000, 2000, 4000]
        for half, (a, b) in halves.items():
            dv = dev[key][a:b]
            for h in (24, 72, 240):
                n = int(h * 3600 / STEP) + 1
                paths = synth_paths(dv, 120, n, seqs[legs])
                for k in clip_grid:
                    Ik = I_of(key, k)
                    rows = collections.defaultdict(list)
                    for p in paths:
                        base = run_policy(p, 0, n, 0.0, Q, k, Ik, lambda q: I_of(key, q), [(1.0, None)], COOL)
                        rows["_dump"].append(p[0] + I_of(key, Q))
                        rows["_base"].append(base[0])
                        rows["_base_time_h"].append(base[1] * STEP / 3600)
                        for name, spec in ladders.items():
                            if spec is None:
                                continue
                            v = run_policy(p, 0, n, 0.0, Q, k, Ik, lambda q: I_of(key, q), spec, COOL)
                            rows[name].append((base[0] - v[0], v[1] * STEP / 3600, v[2]))
                    summ = {"dump_vs_base": float(np.mean(np.array(rows["_dump"]) - np.array(rows["_base"]))),
                            "base_time_h": float(np.mean(rows["_base_time_h"])), "I_k": Ik, "I_Q": I_of(key, Q)}
                    for name, xs in rows.items():
                        if name.startswith("_"):
                            continue
                        x = np.array(xs)
                        summ[name] = {"gain": float(x[:, 0].mean()), "gain_p5": float(np.percentile(x[:, 0], 5)),
                                      "gain_sd": float(x[:, 0].std()), "time_h": float(x[:, 1].mean()), "rem": float(x[:, 2].mean())}
                    tgt[f"{r}|{half}|{h}|{k}"] = summ
        print(r, "done")
    res["targets"] = tgt
    json.dump(res, open(os.path.join(OUT, "results.json"), "w"), default=lambda o: None if (isinstance(o, float) and not np.isfinite(o)) else o)
    print("wrote", os.path.join(OUT, "results.json"))


if __name__ == "__main__":
    if "--figures" in sys.argv:
        from ladder_exits_figures import figure  # noqa: E402
        figure(json.load(open(os.path.join(OUT, "results.json"))))
    else:
        main()
