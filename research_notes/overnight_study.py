"""Overnight study (24 Oct 2026): how past political shocks repriced on Polymarket, and what trading SIG's lag would earn.

Read-only. Public Polymarket data only (gamma + CLOB prices-history, 1-minute), cached in data/research_overnight/raw/.
No SIG venue calls; SIG depth comes from data/books_latest.json.

  nice -n 15 python3 -I research_notes/overnight_study.py            # fetch (cached) + analyse -> results.json
  nice -n 15 <venv>/bin/python -I research_notes/overnight_study.py plot   # figure (needs matplotlib)
"""
from __future__ import annotations

import json
import math
import os
import statistics as st
import sys
import subprocess
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "research_overnight")
RAW = os.path.join(OUT, "raw")
FIG = os.path.join(ROOT, "research_notes", "figures", "shock_lag_ev.png")
TICK = 0.005

# (event, nominal onset UTC, [(event slug, question substring), ...]); the first market is the shocked one
EVENTS = [
    ("Biden debate", "2024-06-28 01:00", [
        ("democratic-nominee-2024", "Joe Biden"), ("presidential-election-winner-2024", "Joe Biden"),
        ("presidential-election-winner-2024", "Donald Trump"), ("democratic-nominee-2024", "Kamala Harris"),
        ("which-party-will-control-the-us-senate-after-the-2024-election", ""),
        ("house-control-after-2024-election", "")]),
    ("Trump shooting", "2024-07-13 22:11", [
        ("presidential-election-winner-2024", "Donald Trump"), ("presidential-election-winner-2024", "Joe Biden"),
        ("which-party-will-control-the-us-senate-after-the-2024-election", ""),
        ("house-control-after-2024-election", "")]),
    ("Biden withdraws", "2024-07-21 17:46", [
        ("democratic-nominee-2024", "Kamala Harris"), ("democratic-nominee-2024", "Joe Biden"),
        ("presidential-election-winner-2024", "Donald Trump"), ("presidential-election-winner-2024", "Kamala Harris"),
        ("which-party-will-control-the-us-senate-after-the-2024-election", ""),
        ("house-control-after-2024-election", "")]),
    ("NC Robinson story", "2024-09-19 20:00", [
        ("north-carolina-governor-election-winner", "Republican"),
        ("presidential-election-winner-2024", "Donald Trump"),
        ("which-party-will-control-the-us-senate-after-the-2024-election", ""),
        ("house-control-after-2024-election", "")]),
]
PRE_MIN, POST_MIN = 120, 24 * 60
HORIZONS = [1, 2, 3, 5, 10, 15, 30, 60, 120, 240, 480, 1440]

# SIG DMM lag: share of an outside move SIG has followed after t minutes (bot_exploits.md, 43 events); beyond 60 min
# no measurement exists, so 12 h is an assumption (0.75, with 0.6 / 0.9 as the low / high case)
LAG_T = [0, 1, 2, 3, 10, 30, 60, 720]
LAG_F = [0, 0, 0, .09, .29, .44, .53, .75]


def get(url: str, cache: str):
    path = os.path.join(RAW, cache)
    if os.path.exists(path):
        return json.load(open(path))
    # curl, not urllib: the system Python here has no CA bundle
    d = json.loads(subprocess.run(["curl", "-s", "--max-time", "30", url], capture_output=True, check=True).stdout)
    json.dump(d, open(path, "w"))
    time.sleep(0.3)
    return d


def token(slug: str, sub: str):
    ev = get(f"https://gamma-api.polymarket.com/events?slug={slug}", f"ev_{slug}.json")
    for m in ev[0]["markets"]:
        if sub in m["question"] or (sub and sub in (m.get("groupItemTitle") or "")):
            return m["question"], json.loads(m["clobTokenIds"])[0]
    m = ev[0]["markets"][0]
    if sub == "":
        return m["question"], json.loads(m["clobTokenIds"])[0]
    raise KeyError(slug + "/" + sub)


def path(tok: str, t0: int):
    pts = {}
    for s in range(t0 - PRE_MIN * 60, t0 + POST_MIN * 60, 6 * 3600):
        e = min(s + 6 * 3600, t0 + POST_MIN * 60)
        h = get(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={s}&endTs={e}&fidelity=1",
                f"ph_{tok[:16]}_{s}.json")["history"]
        for p in h:
            pts[int(p["t"]) // 60 * 60] = float(p["p"])
    out, last = [], None   # forward-filled minute series, index 0 = t0 - PRE_MIN
    for i in range(PRE_MIN + POST_MIN + 1):
        t = t0 + (i - PRE_MIN) * 60
        last = pts.get(t, last)
        out.append(last)
    first = next((x for x in out if x is not None), None)
    return [first if x is None else x for x in out]


def logit(p):
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1 - p))


def interp(x, xs, ys):
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - xs[i - 1]) / (xs[i] - xs[i - 1])


def analyse():
    os.makedirs(RAW, exist_ok=True)
    res = {"events": []}
    for name, t0s, mkts in EVENTS:
        t0 = int(datetime.strptime(t0s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
        rows, lead = [], None
        for slug, sub in mkts:
            try:
                q, tok = token(slug, sub)
                p = path(tok, t0)
            except Exception as ex:   # noqa: BLE001 (a missing market is reported, not fatal)
                rows.append({"market": f"{slug}/{sub}", "error": str(ex)[:80]})
                continue
            pre = st.median(p[PRE_MIN - 30:PRE_MIN])           # median of the 30 min before onset
            at = {h: p[PRE_MIN + h] for h in HORIZONS}
            final = at[1440]
            move = final - pre
            peak_i = max(range(PRE_MIN, len(p)), key=lambda i: (p[i] - pre) * (1 if move >= 0 else -1))
            peak = p[peak_i] - pre
            frac = {h: (at[h] - pre) / move if abs(move) > 1e-9 else None for h in HORIZONS}
            t50 = next((i - PRE_MIN for i in range(PRE_MIN, len(p)) if abs(move) > 0.01
                        and (p[i] - pre) / move >= 0.5), None)
            t90 = next((i - PRE_MIN for i in range(PRE_MIN, len(p)) if abs(move) > 0.01
                        and (p[i] - pre) / move >= 0.9), None)
            r = {"market": q[:70], "pre": round(pre, 3), "move_24h_pts": round(100 * move, 1),
                 "peak_pts": round(100 * peak, 1), "peak_min": peak_i - PRE_MIN,
                 "overshoot_pts": round(100 * (abs(peak) - abs(move)), 1),
                 "move_pts": {h: round(100 * (at[h] - pre), 1) for h in HORIZONS},
                 "frac_of_24h": {h: (round(f, 2) if f is not None else None) for h, f in frac.items()},
                 "t50_min": t50, "t90_min": t90, "dlogit_24h": round(logit(final) - logit(pre), 2),
                 "series": [round(x, 4) for x in p]}
            if lead is None:
                lead = r
            else:
                r["beta_logit_vs_lead"] = round(r["dlogit_24h"] / lead["dlogit_24h"], 2) if lead["dlogit_24h"] else None
            rows.append(r)
        res["events"].append({"event": name, "t0_utc": t0s, "markets": rows})
    res["depth"] = sig_depth()
    res["ev"] = ev_model(res)
    res["sizing"] = sizing(res)
    slim = json.loads(json.dumps(res))
    for e in slim["events"]:
        for m in e["markets"]:
            m.pop("series", None)
    json.dump(res, open(os.path.join(OUT, "results_full.json"), "w"))
    json.dump(slim, open(os.path.join(OUT, "results.json"), "w"), indent=1)
    return slim


def sig_depth():
    """Median cumulative ask depth within k ticks of the touch, live SIG books (24 Oct 00:01 UTC), mids 0.15-0.85."""
    books = json.load(open(os.path.join(ROOT, "data", "books_latest.json")))
    cum = {k: [] for k in (0, 1, 2, 4, 6, 10, 20)}
    for b in books.values():
        if not b.get("asks") or not b.get("bids"):
            continue
        a0, b0 = b["asks"][0]["price"], b["bids"][0]["price"]
        if not 0.15 <= (a0 + b0) / 2 <= 0.85:
            continue
        for k in cum:
            cum[k].append(sum(l["quantity"] for l in b["asks"] if l["price"] <= a0 + k * TICK + 1e-9))
    return {"n_books": len(cum[0]), **{f"within_{k}_ticks": int(st.median(v)) for k, v in cum.items() if v}}


def lag(age: float, l12: float, fast: bool = False) -> float:
    """Share of an outside step SIG has copied `age` minutes later. fast = a DMM that reacts to big moves within minutes."""
    if fast:
        return min(l12, interp(age, [0, 2, 5, 15, 60, 720], [0, 0, .5, .7, .8, max(l12, .8)]))
    return interp(age, LAG_T, LAG_F[:-1] + [l12])


def sig_path(series, l12, fast=False, tmax=720):
    """SIG mid = pre + sum over past outside 1-min increments x lag(age): the DMM copies each outside step with the
    measured delay kernel (bot_exploits.md), starting from the pre-shock level."""
    o = series[PRE_MIN - 1:PRE_MIN + tmax + 1]
    inc = [o[i] - o[i - 1] for i in range(1, len(o))]
    out = [o[0]]
    for t in range(1, len(o)):
        out.append(o[0] + sum(inc[j] * lag(t - 1 - j, l12, fast) for j in range(t)))
    return out   # index 0 = one minute before onset, index t = minute t-1


def ev_model(res):
    """Profit per share of taking SIG's stale side at minute t (direction of the outside move so far, only if it is
    >= 2 pts) and selling back at 12 h, on each shock path (shocked market plus correlated markets that moved >= 5 pts).
    Cost: pay a 1-tick spread across entry and exit plus 0.1 tick slippage."""
    cost = 2 * TICK / 2 + 0.1 * TICK
    paths = [(e["event"], m) for e in res["events"] for m in e["markets"]
             if "series" in m and abs(m["move_24h_pts"]) >= 5 and "control" not in m["market"]]   # chamber books too thin
    grid = [1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240]
    out = {"cost_pts": round(100 * cost, 2), "paths": [], "cases": {}}
    for case, l12, fast in (("slow, 60% by 12 h", .6, False), ("slow, 75% by 12 h", .75, False),
                            ("slow, 90% by 12 h", .9, False), ("fast DMM, 50% in 5 min", .8, True)):
        rows = {}
        for ev, m in paths:
            sp = sig_path(m["series"], l12, fast)
            o0 = m["series"][PRE_MIN - 1]
            r = {}
            for t in grid:
                seen = m["series"][PRE_MIN + t] - o0
                if abs(seen) < 0.02:
                    r[t] = None
                    continue
                d = 1 if seen > 0 else -1
                gap = d * (o0 + seen - sp[t + 1])             # outside minus SIG at entry, our direction
                pnl = d * (sp[-1] - sp[t + 1]) - cost
                m12 = abs(m["series"][PRE_MIN + 720] - o0)
                r[t] = {"pnl_pts": round(100 * pnl, 2), "gap_pts": round(100 * gap, 2),
                        "pnl_per_pt_12h_move": round(pnl / m12, 3) if m12 > 0.02 else None,
                        "capture": round(pnl / gap, 2) if gap > 0.005 else None}
            rows[f"{ev} / {m['market'][:40]}"] = r
        med = {t: (round(st.median(v), 2) if v else None)
               for t in grid for v in [[x[t]["pnl_pts"] for x in rows.values() if x[t]]]}
        cap = {t: (round(st.median(v), 2) if v else None)
               for t in grid for v in [[x[t]["capture"] for x in rows.values() if x[t] and x[t]["capture"] is not None]]}
        per = {t: (round(st.median(v), 3) if v else None) for t in grid
               for v in [[x[t]["pnl_per_pt_12h_move"] for x in rows.values() if x[t] and x[t]["pnl_per_pt_12h_move"]]]}
        n = {t: sum(1 for x in rows.values() if x[t]) for t in grid}
        out["cases"][case] = {"median_pnl_pts": med, "per_pt_of_12h_move": per, "n": n, "median_capture_of_gap": cap, "by_path": rows}
    return out


def sizing(res):
    """Best size and expected profit per shock. Edge per share = k(t) x M (k from the 'slow, 75%' case); we buy a
    ladder level j ticks behind the touch only while edge > 2j ticks (pay j in, about j again selling back into the
    other side's ladder at 12 h). Ladder = median cumulative depth of live SIG books. 'ladder pulls' = the DMM requotes
    after our first sweep, so only the levels within 2 ticks fill. Cash cap = notional at ~0.5 per share."""
    d = res["depth"]
    lv = [(0, d["within_0_ticks"]), (1, d["within_1_ticks"]), (2, d["within_2_ticks"]), (4, d["within_4_ticks"]),
          (6, d["within_6_ticks"]), (10, d["within_10_ticks"])]
    k = res["ev"]["cases"]["slow, 75% by 12 h"]["per_pt_of_12h_move"]
    out = {}
    # calibration: on SIG's own 3+ and 5+ pt outside moves (outside_stable_sweep.md) the fast follow realised about
    # 0.44-0.49 pt per pt of move vs the model's 0.73 at entry within 2 min, so 'central' scales k by 0.6
    for calib in (0.6, 1.0):
        for t, M, cash, pull in ((t, M, c, p) for t in (2, 10, 30) for M in (5, 10, 20)
                                 for c in (25000, 50000) for p in (False, True)):
            if True:
                if True:
                    edge_ticks = calib * (k[t] or 0) * M * 2          # 1 pt = 2 ticks
                    q_prev, j_prev, prof, q = 0, 0, 0.0, 0
                    for j, cum in lv:
                        if pull and j > 2:
                            break
                        if edge_ticks - 2 * j <= 0:
                            break
                        add = min(cum - q_prev, max(0, cash / 0.5 - q))
                        prof += add * (edge_ticks - (j + j_prev)) * TICK   # mean level cost in the band, in and out
                        q += add
                        q_prev, j_prev = cum, j
                        if q >= cash / 0.5:
                            break
                    out[f"{'central' if calib < 1 else 'model'}_t{t}_M{M}_cash{cash}_{'pull' if pull else 'hold'}"] = \
                        {"shares": int(q), "profit": round(prof)}
    return out


def plot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    res = json.load(open(os.path.join(OUT, "results_full.json")))
    ts = list(range(0, 241))
    fig, ax = plt.subplots(3, 1, figsize=(8.5, 10.5), sharex=True)
    for e in res["events"]:
        for m in e["markets"]:
            if "series" not in m or abs(m["move_24h_pts"]) < 5 or "control" in m["market"]:
                continue
            s = m["series"]
            pre = s[PRE_MIN - 1]
            ax[0].plot(ts, [100 * abs(s[PRE_MIN + t] - pre) for t in ts], lw=1.4,
                       label=f"{e['event']}: {m['market'][5:38]} ({m['move_24h_pts']:+.0f} at 24 h)")
    ax[0].set_ylabel("outside move (pts)")
    ax[0].set_title("A  Outside price (Polymarket) after four 2024 shocks", loc="left", fontsize=10)
    ax[0].legend(fontsize=6.5, loc="upper left", ncol=1)
    ax[1].plot(ts, [lag(t, .75) for t in ts], color="C3", lw=2, label="slow DMM: measured to 60 min, then to 75% by 12 h")
    ax[1].plot(ts, [lag(t, .8, True) for t in ts], color="C0", lw=1.5, ls="--", label="fast DMM: if big moves get a quick requote")
    ax[1].axvline(60, color="grey", lw=0.6, ls=":")
    ax[1].set_ylabel("share of an outside step\nSIG has copied")
    ax[1].set_title("B  SIG's lag behind the outside price", loc="left", fontsize=10)
    ax[1].legend(fontsize=8, loc="lower right")
    for case, c, lw in (("slow, 90% by 12 h", "C2", 1), ("slow, 75% by 12 h", "k", 2.2),
                        ("slow, 60% by 12 h", "C1", 1), ("fast DMM, 50% in 5 min", "C0", 1.5)):
        med = res["ev"]["cases"][case]["per_pt_of_12h_move"]
        tt = [int(t) for t in med if med[t] is not None and res["ev"]["cases"][case]["n"][t] >= 4]
        ax[2].plot(tt, [med[str(t)] for t in tt], color=c, lw=lw, marker="o", ms=3, label=case,
                   ls="--" if "fast" in case else "-")
    ax[2].axhline(0, color="grey", lw=0.8)
    ax[2].set_ylabel("profit per share\nper pt of outside move")
    ax[2].set_title("C  Profit of taking SIG's stale side at minute t, selling back at 12 h", loc="left", fontsize=10)
    ax[2].legend(fontsize=8, loc="upper right")
    ax[2].set_xlabel("minutes after the shock")
    fig.suptitle("Trading SIG's lag after a big shock", fontsize=12)
    fig.text(0.02, 0.005, "Glossary: pts = percentage points of price (1 pt = 2 ticks of 0.005). Outside = Polymarket 1-minute "
             "prices; the 6 markets that moved 5+ pts in 24 h.\nSIG lag = share of an outside move SIG's market maker has copied "
             "(43 moves of 1.5+ pts, Oct 2026). Profit is net of a 1-tick spread and slippage;\nentry once the outside "
             "has moved 2+ pts, on its side. C: median over shocks, minutes with 4+ shocks qualifying.", fontsize=7.5, va="bottom")
    fig.tight_layout(rect=(0, 0.06, 1, 0.97))
    fig.savefig(FIG, dpi=130)
    print(FIG)


# ---- part 2 (24 Oct, continued): SIG's lag beyond 60 min and on bigger moves, from the bot's own venue_fair table ----
def lag_study(filtered: bool = True):
    """Event = outside fair (both venues present, within 4 pts) moved >= M pts over 60 min and SIG copied < 1/3.
    Copied share at +t = (SIG mid(t0+t) - SIG mid(t0-60)) / (fair(t0) - fair(t0-60)); one event per leg per 6 h."""
    import sqlite3
    con = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True, timeout=10)
    rows = con.execute("select race, party, ts, pm, kalshi, fair, sig_mid from venue_fair where fair is not null "
                       "and sig_mid is not null order by race, party, ts").fetchall()
    series = {}
    for race, party, ts, pm, k, f, s in rows:
        series.setdefault((race, party), []).append((ts, pm, k, f, s))
    hz = [10, 30, 60, 120, 240, 480, 720]
    ev = []
    for key, pts in series.items():
        t0s, tN = pts[0][0], pts[-1][0]
        n = int((tN - t0s) // 60) + 1
        grid, j = [None] * n, 0           # forward-filled 1-minute grid
        for i in range(n):
            t = t0s + i * 60
            while j + 1 < len(pts) and pts[j + 1][0] <= t:
                j += 1
            grid[i] = pts[j] if t - pts[j][0] < 600 else None
        last = -10 ** 9
        for i in range(60, n):
            a, b = grid[i - 60], grid[i]
            if not a or not b or i - last < 360:
                continue
            mv = b[3] - a[3]
            if abs(mv) < 0.02 or b[1] is None or b[2] is None or a[1] is None or a[2] is None:
                continue
            if (b[1] - a[1]) * mv <= 0 or (b[2] - a[2]) * mv <= 0 or abs(b[1] - b[2]) > 0.04:
                continue
            if filtered and (b[4] - a[4]) / mv >= 1 / 3:
                continue
            r = {"leg": f"{key[0]} {key[1]}", "ts": b[0], "move_pts": round(100 * mv, 1),
                 "sig_at": {d: (grid[i + d][4] if i + d < n and grid[i + d] else None)
                            for d in (0, 1, 2, 3, 5, 10, 60, 240, 720)}}
            for h in hz:
                c = grid[i + h] if i + h < n else None
                r[h] = round((c[4] - a[4]) / mv, 2) if c else None
                r[f"f{h}"] = round((c[3] - a[3]) / mv, 2) if c else None   # outside's own path, same scale
            ev.append(r)
            last = i
    out = {"n": len(ev), "span_h": None, "by_size": {}}
    for lo, hi in ((2, 3), (3, 5), (5, 100), (2, 100)):
        sel = [e for e in ev if lo <= abs(e["move_pts"]) < hi]
        out["by_size"][f"{lo}-{hi}"] = {"n": len(sel), **{
            f"sig_{h}": (round(st.median(v), 2), len(v)) if v else None
            for h in hz for v in [[e[h] for e in sel if e[h] is not None]]}, **{
            f"out_{h}": round(st.median(v), 2) if v else None
            for h in hz for v in [[e[f"f{h}"] for e in sel if e[f"f{h}"] is not None]]}}
    # P&L per share (pts) of taking SIG at onset + d minutes in the outside's direction, out at +h, net of 1 tick + slip
    cost = 100 * (TICK + 0.1 * TICK)
    out["pnl"] = {}
    for lo, hi in ((2, 3), (3, 5), (5, 100), (2, 100)):
        sel = [e for e in ev if lo <= abs(e["move_pts"]) < hi]
        for d in (1, 2, 3, 5, 10):
            for h in (60, 240, 720):
                v = [100 * (e["sig_at"][h] - e["sig_at"][d]) * (1 if e["move_pts"] > 0 else -1) - cost
                     for e in sel if e["sig_at"][d] is not None and e["sig_at"][h] is not None]
                if len(v) >= 2:
                    sd = st.pstdev(v)
                    out["pnl"][f"{lo}-{hi} d{d} h{h}"] = (round(st.mean(v), 2), round(st.mean(v) / (sd / math.sqrt(len(v))), 1)
                                                         if sd else None, len(v))
    out["events"] = ev
    json.dump(out, open(os.path.join(OUT, "lag_study.json" if filtered else "lag_study_unfiltered.json"), "w"), indent=1)
    return out


def base_rate():
    """How often both outside venues jump >= M pts within W minutes (cached 1-min Polymarket + Kalshi, 114 races)."""
    import glob
    import bisect
    ev = []
    span = [10 ** 12, 0]
    for f in glob.glob(os.path.join(ROOT, "data", "research_outside_sweep", "raw", "*.json")):
        d = json.load(open(f))
        pm = sorted(d.get("pm_hist") or [])
        kc = sorted(d.get("k_candles") or [])
        if len(pm) < 100 or len(kc) < 10:
            continue
        kt = [x[0] for x in kc]
        km = [(x[1] + x[2]) / 2 for x in kc]
        span = [min(span[0], pm[0][0]), max(span[1], pm[-1][0])]

        def kat(t):
            i = bisect.bisect_right(kt, t) - 1
            return km[i] if i >= 0 and t - kt[i] < 1800 else None
        for W in (10, 60):
            last = 0
            pt = [x[0] for x in pm]
            for i, (t, p) in enumerate(pm):
                j = bisect.bisect_left(pt, t - W * 60)
                if j >= i or t - last < 6 * 3600:
                    continue
                mv = p - pm[j][1]
                k1, k0 = kat(t), kat(pm[j][0])
                if abs(mv) >= 0.05 and k1 is not None and k0 is not None and (k1 - k0) * (1 if mv > 0 else -1) >= 0.03:
                    ev.append({"leg": f"{d['race']} {d['party']}", "t": t, "W": W, "pm_pts": round(100 * mv, 1),
                               "k_pts": round(100 * (k1 - k0) * (1 if mv > 0 else -1), 1)})
                    last = t
    days = (span[1] - span[0]) / 86400
    out = {"days": round(days, 1), "n": len(ev), "per_day": round(len(ev) / days, 2), "events": ev}
    json.dump(out, open(os.path.join(OUT, "base_rate.json"), "w"), indent=1)
    return out


def refill_study():
    """Fable idea 2: after a print that takes (most of) the touch, does the DMM put the same price back, and how fast?
    Uses the 22 Oct full-feed recording (book snapshots + prints). Sells and buys are tested separately."""
    import bisect
    books, trades = {}, []
    for line in open(os.path.join(ROOT, "data", "feed_capture_20261022.jsonl")):
        d = json.loads(line)
        if d["t"] == "book":
            books.setdefault(d["ex"], []).append((d["rx"], d["bids"], d["asks"]))
        else:
            trades.append(d)
    out = {"n_trades": len(trades), "rows": []}
    for tr in trades:
        bk = books.get(tr["ex"])
        if not bk:
            continue
        ts = [b[0] for b in bk]
        i = bisect.bisect_left(ts, tr["rx"]) - 1
        if i < 0 or tr["rx"] - ts[i] > 30:
            continue
        b0, a0 = bk[i][1], bk[i][2]   # recorded side is null: infer it from which touch the print hit
        if a0 and abs(a0[0][0] - tr["price"]) < 1e-9:
            side, lv = "buy", a0
        elif b0 and abs(b0[0][0] - tr["price"]) < 1e-9:
            side, lv = "sell", b0
        else:
            continue
        q0 = lv[0][1]
        frac = tr["qty"] / q0 if q0 else None
        res, first_away = None, None
        for b in bk[i + 1:]:
            dt = b[0] - tr["rx"]
            if dt > 20:
                break
            l2 = b[2] if side == "buy" else b[1]
            if not l2:
                continue
            if abs(l2[0][0] - tr["price"]) < 1e-9 and l2[0][1] >= 0.8 * q0:
                res = ("refilled_same", round(dt, 1))
                break
            if first_away is None and abs(l2[0][0] - tr["price"]) > 1e-9:
                worse = (l2[0][0] > tr["price"]) if side == "buy" else (l2[0][0] < tr["price"])
                first_away = ("moved_away" if worse else "moved_toward", round(dt, 1))
        res = res or first_away
        out["rows"].append({"ex": tr["ex"], "side": side, "qty": tr["qty"], "touch_qty": q0,
                            "frac": round(frac, 2) if frac else None, "res": res})
    from collections import Counter
    summ = {}
    for lab, cond in (("took >= 80% of touch", lambda r: r["frac"] and r["frac"] >= 0.8),
                      ("took < 80%", lambda r: r["frac"] and r["frac"] < 0.8)):
        for sd in sorted({r["side"] for r in out["rows"]}):
            sel = [r for r in out["rows"] if cond(r) and r["side"] == sd]
            c = Counter(r["res"][0] if r["res"] else "no change/unknown <20 s" for r in sel)
            dts = [r["res"][1] for r in sel if r["res"] and r["res"][0] == "refilled_same"]
            summ[f"{lab} / {sd}"] = {"n": len(sel), **c, "median_refill_s": st.median(dts) if dts else None}
    out["summary"] = summ
    json.dump(out, open(os.path.join(OUT, "refill_study.json"), "w"), indent=1)
    return out


def jump_rate_2024():
    """Do 5+ pt one-hour jumps get more frequent as election day nears? 2024 Polymarket downballot races
    (one D-leg token per race, 10-min fidelity, 1 Sep - 4 Nov 2024)."""
    legs = json.load(open(os.path.join(ROOT, "data", "election_night", "legs_2024.json")))
    slugs = sorted({l["slug_or_ticker"] for l in legs if l["venue"] == "polymarket"
                    and l["office"] in ("house", "senate", "governor")})
    eday = int(datetime(2024, 11, 5, 11, tzinfo=timezone.utc).timestamp())   # polls open; stop before results
    s0 = eday - 65 * 86400
    jumps, cover, cover_b = [], {}, {}
    for slug in slugs:
        try:
            q, tok = token(slug, "Democrat")
        except Exception:   # noqa: BLE001
            try:
                q, tok = token(slug, "")
            except Exception:   # noqa: BLE001
                continue
        h = []
        for a in range(s0, eday, 15 * 86400):
            h += get(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={a}&endTs={min(a + 15 * 86400, eday)}"
                     f"&fidelity=10", f"ph10_{tok[:16]}_{a}.json").get("history", [])
        h = sorted({int(x["t"]): float(x["p"]) for x in h}.items())
        if len(h) < 100:
            continue
        last = -10 ** 9
        for i, (t, p) in enumerate(h):
            dbin = (eday - t) // 86400
            cover[dbin] = cover.get(dbin, 0) + 1 / 144    # race-days covered (10-min points)
            band = "competitive 0.25-0.75" if 0.25 <= p <= 0.75 else ("lean 0.10-0.25/0.75-0.90" if 0.10 <= p <= 0.90 else "safe <0.10/>0.90")
            cover_b[band] = cover_b.get(band, 0) + 1 / 144
            j = i
            while j > 0 and t - h[j - 1][0] <= 3600:
                j -= 1
            if 0.03 <= p <= 0.97 and abs(p - h[j][1]) >= 0.05 and t - last > 6 * 3600:
                jumps.append({"slug": slug, "t": t, "days_out": dbin, "pts": round(100 * (p - h[j][1]), 1), "pre": h[j][1]})
                last = t
    out = {"n_races": len(slugs), "jumps": jumps, "rate": {}}
    for lo, hi in ((0, 3), (3, 10), (10, 20), (20, 35), (35, 65)):
        n = sum(1 for j in jumps if lo <= j["days_out"] < hi)
        rd = sum(v for d, v in cover.items() if lo <= d < hi)
        out["rate"][f"{lo}-{hi} days out"] = {"jumps": n, "race_days": round(rd), "per_100_race_days": round(100 * n / rd, 2) if rd else None}
    for band, rd in cover_b.items():
        n = sum(1 for j in jumps if ("competitive 0.25-0.75" if 0.25 <= j["pre"] <= 0.75 else
                                     ("lean 0.10-0.25/0.75-0.90" if 0.10 <= j["pre"] <= 0.90 else "safe <0.10/>0.90")) == band)
        out["rate"][band] = {"jumps": n, "race_days": round(rd), "per_100_race_days": round(100 * n / rd, 2)}
    json.dump(out, open(os.path.join(OUT, "jump_rate_2024.json"), "w"), indent=1)
    return out


def plot_measured():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    lag = json.load(open(os.path.join(OUT, "lag_study.json")))
    jr = json.load(open(os.path.join(OUT, "jump_rate_2024.json")))
    fig, ax = plt.subplots(3, 1, figsize=(8.5, 10.5))
    offs = [0, 1, 2, 3, 5, 10, 60]
    for e in lag["events"]:
        if abs(e["move_pts"]) < 4.5:
            continue
        d = 1 if e["move_pts"] > 0 else -1
        s = e["sig_at"]
        xs = [o for o in offs if s[str(o)] is not None]
        ax[0].plot(xs, [100 * d * (s[str(o)] - s["0"]) / abs(e["move_pts"]) * 100 for o in xs], marker="o", ms=3,
                   label=f"{e['leg'][:22]} ({e['move_pts']:+.1f} pts)")
    ax[0].set_ylabel("share of outside move\nSIG has copied (%)")
    ax[0].set_xlabel("minutes after the outside move")
    ax[0].set_title("A  SIG's market maker on 5+ pt outside moves (22-23 Oct)", loc="left", fontsize=10)
    ax[0].legend(fontsize=7, loc="upper left")
    for lab, c in (("5-100", "k"), ("3-5", "C1"), ("2-3", "C0")):
        pts = [(d, lag["pnl"].get(f"{lab} d{d} h60")) for d in (1, 2, 3, 5, 10)]
        pts = [(d, v) for d, v in pts if v]
        name = {"5-100": "5+ pts", "3-5": "3-5 pts", "2-3": "2-3 pts"}[lab]
        ax[1].plot([d for d, _ in pts], [v[0] for d, v in pts], marker="o", color=c, lw=2 if c == "k" else 1,
                   label=f"outside move {name} (n {pts[0][1][2]})")
    ax[1].axhline(0, color="grey", lw=0.8)
    ax[1].set_ylabel("profit per share (pts)")
    ax[1].set_xlabel("minutes from the outside move to our entry")
    ax[1].set_title("B  Taking SIG's stale side, selling back after 60 min", loc="left", fontsize=10)
    ax[1].legend(fontsize=8)
    ks = list(jr["rate"])
    ax[2].bar(range(len(ks)), [jr["rate"][k]["per_100_race_days"] for k in ks], color="C2")
    ax[2].set_xticks(range(len(ks)))
    ax[2].set_xticklabels([k.replace(" days out", "") for k in ks])
    ax[2].set_xlabel("days before the 2024 election")
    ax[2].set_ylabel("5+ pt one-hour jumps\nper 100 race-days")
    ax[2].set_title("C  Big outside jumps get more frequent near election day (2024, 65 races)", loc="left", fontsize=10)
    fig.suptitle("SIG's lag on big moves, measured", fontsize=12)
    fig.text(0.02, 0.005, "Glossary: pts = percentage points of price (1 pt = 2 ticks). Outside = Polymarket and Kalshi "
             "(both moved the same way).\nA, B: bot's venue_fair table, 22-24 Oct 2026. Profit is net of a 1-tick spread "
             "and slippage. C: Polymarket 10-minute prices for 2024 House, Senate and Governor races.", fontsize=7.5,
             va="bottom")
    fig.tight_layout(rect=(0, 0.05, 1, 0.97))
    f = os.path.join(ROOT, "research_notes", "figures", "shock_sig_measured.png")
    fig.savefig(f, dpi=130)
    print(f)


def walker_2024(v_list=(0.2, 0.5, 1.0)):
    """Fable round 2, ideas 1 and 3: replay 2024 election night (Polymarket 1-min legs) against a synthetic SIG that
    walks toward the outside price at v pts/min from 19:00 ET. Idea 1 (stage-2 stale gap): legs where outside >=
    walker + 15 pts while the walker is at 0.15-0.35 -> win rate of buying at the walker price. Idea 3 (lag tickets):
    legs where outside >= 0.95 while the walker is at 0.60-0.80 -> win rate, multiplier, minutes until the walker reaches
    0.92 (cash locked). First qualifying minute per leg only."""
    import csv
    legs = {}
    t_start = int(datetime(2024, 11, 6, 0, 0, tzinfo=timezone.utc).timestamp())    # 19:00 ET
    t_end = t_start + 17 * 3600                                                     # 12:00 ET 6 Nov
    for r in csv.DictReader(open(os.path.join(ROOT, "data", "election_night", "paths_2024.csv"))):
        if r["venue"] != "polymarket":
            continue
        t = int(r["ts"]) // 60 * 60
        if t_start - 3600 <= t <= t_end:
            legs.setdefault((r["slug_or_ticker"], r["party"], r["outcome"]), {})[t] = float(r["price"])
    out = {"n_legs": len(legs)}
    for v in v_list:
        i1, i3 = [], []
        for (slug, party, won), pts in legs.items():
            last, w = None, None
            got1 = got3 = False
            for t in range(t_start - 3600, t_end, 60):
                last = pts.get(t, last)
                if last is None:
                    continue
                if w is None or t < t_start:
                    w = last
                    continue
                w += max(-v / 100, min(v / 100, last - w))     # SIG walks toward outside at v pts/min
                et_h = (t - t_start) / 3600 + 19
                if not got1 and 0.15 <= w <= 0.35 and last - w >= 0.15 and et_h <= 24:
                    i1.append({"leg": f"{slug} {party}", "won": int(won), "walker": round(w, 3), "outside": round(last, 3),
                               "et": round(et_h, 2)})
                    got1 = True
                if not got3 and 0.60 <= w <= 0.80 and last >= 0.95 and et_h <= 24:
                    lock = next((m for m in range(0, 600) if min(1, w + v / 100 * m) >= 0.92), None)
                    i3.append({"leg": f"{slug} {party}", "won": int(won), "walker": round(w, 3), "et": round(et_h, 2),
                               "lock_min": lock})
                    got3 = True
        def summ(x):
            if not x:
                return {"n": 0}
            return {"n": len(x), "win": round(sum(e["won"] for e in x) / len(x), 2),
                    "avg_price": round(st.mean(e["walker"] for e in x), 3),
                    "ev_per_S": round(sum(e["won"] for e in x) / sum(e["walker"] for e in x), 2)}
        out[f"v{v}"] = {"idea1_stale_stage2": {**summ(i1), "rows": i1}, "idea3_lag_tickets": {**summ(i3), "rows": i3}}
    json.dump(out, open(os.path.join(OUT, "walker_2024.json"), "w"), indent=1)
    return out


def replay_big():
    """Shock-mode replay on the 4.5+ pt events of lag_study: entry at the detecting venue_fair poll (SIG ask, or NO at
    1 - bid), exit at the first poll >= 60 min later (SIG bid, or NO at 1 - ask); walk the 24-Oct median ladder j ticks,
    paying j ticks in and j out. Note: in-sample exit choice; MT-01 D and R are one event."""
    import sqlite3
    con = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
    ev = [e for e in json.load(open(os.path.join(OUT, "lag_study.json")))["events"] if abs(e["move_pts"]) >= 4.5]
    d = json.load(open(os.path.join(OUT, "results.json")))["depth"]
    cum = {0: d["within_0_ticks"], 1: d["within_1_ticks"], 2: d["within_2_ticks"],
           3: (d["within_2_ticks"] + d["within_4_ticks"]) // 2, 4: d["within_4_ticks"]}
    rules = {"touch only": lambda M: 0, "<=1 tick": lambda M: 1, "<=2 ticks": lambda M: 2,
             "floor((M-2.5)/2), max 4": lambda M: max(0, min(4, int((M - 2.5) // 2)))}
    rows = []
    for e in ev:
        race, party = e["leg"].rsplit(" ", 1)
        q = lambda t: con.execute("select ts, fair, sig_bid, sig_ask from venue_fair where race=? and party=? and ts>=? "
                                  "order by ts limit 1", (race, party, t)).fetchone()
        r0, r60 = q(e["ts"] - 1), q(e["ts"] + 3600)
        if not r60:
            continue
        up = e["move_pts"] > 0
        rows.append((e["leg"], abs(e["move_pts"]), r0[3] if up else 1 - r0[2], r60[2] if up else 1 - r60[3]))
    out = {}
    for name, f in rules.items():
        per = []
        for leg, M, en, ex in rows:
            prev = prof = 0
            for k in range(f(M) + 1):
                prof += (cum[k] - prev) * (ex - en - 2 * k * TICK)
                prev = cum[k]
            per.append((leg, round(prof)))
        out[name] = {"total": sum(p for _, p in per), "per_event": per}
    json.dump(out, open(os.path.join(OUT, "replay_big.json"), "w"), indent=1)
    return out


def venue_split():
    """Fable round 3 idea 1: does SIG copy single-venue outside moves? 3+ pt moves over 60 min classified by venue:
    pm-only (Kalshi moved < 1 pt or the other way), kalshi-only, both. Copy share = SIG change / moving venue's change.
    P&L: take SIG's touch at detection on the moving venue's side, exit at the 30 / 60-min touch (bid/ask from venue_fair)."""
    import sqlite3
    con = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
    rows = con.execute("select race, party, ts, pm, kalshi, sig_bid, sig_ask from venue_fair where pm is not null and "
                       "kalshi is not null and sig_bid is not null and sig_ask is not null order by race, party, ts").fetchall()
    ser = {}
    for r in rows:
        ser.setdefault((r[0], r[1]), []).append(r[2:])
    import bisect
    res = {"pm-only": [], "kalshi-only": [], "both": []}
    for key, s in ser.items():
        ts = [x[0] for x in s]
        last = -1e18
        for i, (t, pm, k, b, a) in enumerate(s):
            j = bisect.bisect_left(ts, t - 3600)
            if j >= i or t - ts[j] < 2700 or t - last < 6 * 3600:
                continue
            dpm, dk = pm - s[j][1], k - s[j][2]
            mid0, mid = (s[j][3] + s[j][4]) / 2, (b + a) / 2
            for cls, mv, ok in (("both", (dpm + dk) / 2, abs(dpm) >= .03 and abs(dk) >= .03 and dpm * dk > 0),
                                ("pm-only", dpm, abs(dpm) >= .03 and (abs(dk) < .01 or dk * dpm < 0)),
                                ("kalshi-only", dk, abs(dk) >= .03 and (abs(dpm) < .01 or dk * dpm < 0))):
                if not ok or (mid - mid0) / mv >= 1 / 3:
                    continue
                up = mv > 0
                e = {"leg": " ".join(key), "t": t, "mv": round(100 * mv, 1),
                     "entry": a if up else 1 - b}
                for h in (10, 30, 60, 240):
                    m = bisect.bisect_left(ts, t + h * 60)
                    if m < len(s) and s[m][0] - (t + h * 60) < 600:
                        e[f"copy{h}"] = round(((s[m][3] + s[m][4]) / 2 - mid0) / mv, 2)
                        e[f"pnl{h}"] = round(100 * ((s[m][3] if up else 1 - s[m][4]) - e["entry"]), 2)
                res[cls].append(e)
                last = t
                break
    out = {}
    for cls, ev in res.items():
        o = {"n": len(ev)}
        for h in (10, 30, 60, 240):
            c = [e[f"copy{h}"] for e in ev if f"copy{h}" in e]
            p = [e[f"pnl{h}"] for e in ev if f"pnl{h}" in e]
            if c:
                o[f"copy{h}"] = round(st.median(c), 2)
                sd = st.pstdev(p) if len(p) > 1 else 0
                o[f"pnl{h}"] = (round(st.mean(p), 2), round(st.mean(p) / (sd / math.sqrt(len(p))), 1) if sd else None, len(p))
        for lo in (3, 5):
            sel = [e for e in ev if abs(e["mv"]) >= lo and "pnl60" in e]
            o[f"pnl60_mv{lo}+"] = (round(st.mean(e["pnl60"] for e in sel), 2), len(sel)) if sel else None
        out[cls] = o
    json.dump({"summary": out, "events": res}, open(os.path.join(OUT, "venue_split.json"), "w"), indent=1)
    return out


def shock_cap_curve():
    """Shock-mode decision curve vs the per-shock notional cap: replay profit on the 4.5+ pt events (real SIG quotes,
    median ladder, walk floor((M-2.5)/2) ticks, 60-min exit), the loss of a false shock (SIG never moves; exit taker 2
    ticks behind the bid), and net EV per shock for a 10% / 25% share of false shocks. Writes figures/shock_mode_decision.png."""
    import sqlite3
    con = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
    ev = [e for e in json.load(open(os.path.join(OUT, "lag_study.json")))["events"] if abs(e["move_pts"]) >= 4.5]
    d = json.load(open(os.path.join(OUT, "results.json")))["depth"]
    lad = [(0, d["within_0_ticks"]), (1, d["within_1_ticks"]), (2, d["within_2_ticks"]),
           (3, (d["within_2_ticks"] + d["within_4_ticks"]) // 2), (4, d["within_4_ticks"])]
    rows = []
    for e in ev:
        race, party = e["leg"].rsplit(" ", 1)
        q = lambda t: con.execute("select ts, fair, sig_bid, sig_ask from venue_fair where race=? and party=? and ts>=? "
                                  "order by ts limit 1", (race, party, t)).fetchone()
        r0, r60 = q(e["ts"] - 1), q(e["ts"] + 3600)
        if r60:
            up = e["move_pts"] > 0
            rows.append((e["leg"], abs(e["move_pts"]) / 100, r0[3] if up else 1 - r0[2], r60[2] if up else 1 - r60[3]))

    def fill(M, cap, entry):
        j = max(0, min(4, int(math.floor((M - 0.025) / (2 * TICK) + 1e-9))))
        got, prev, cost_t = 0, 0, 0.0
        for k, cum in lad:
            if k > j:
                break
            take = min(cum - prev, max(0, cap / (entry + k * TICK) - got))
            got += take
            cost_t += take * k
            prev = cum
        return got, (cost_t / got if got else 0.0)
    caps = [5000, 10000, 20000, 30000, 40000, 60000, 80000]     # per shock, both legs (each leg gets half)
    prof, hoax = [], []
    for cap in caps:
        tot = 0.0
        for leg, M, en, ex in rows:
            sh, wk = fill(M, cap / 2, en)
            tot += 2 * sh * (ex - en - 2 * wk * TICK)     # both legs; walk paid in and out
        prof.append(tot / len(rows))
        sh, wk = fill(0.06, cap / 2, 0.45)
        hoax.append(-2 * sh * (wk + 3) * TICK)            # walk + half spread in, 2 ticks + half spread out, SIG never moved
    out = {"caps": caps, "replay_profit_per_event": [round(x) for x in prof], "false_shock_loss": [round(x) for x in hoax],
           "n_events": len(rows)}
    json.dump(out, open(os.path.join(OUT, "shock_cap_curve.json"), "w"), indent=1)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return out
    k = [c / 1000 for c in caps]
    fig, ax = plt.subplots(3, 1, figsize=(8.5, 10), sharex=True)
    ax[0].plot(k, prof, "o-", color="C2", lw=2, label=f"replay of {len(rows)} real 4.5+ pt moves (mean per shock, both legs)")
    ax[0].set_ylabel("profit per shock (S)")
    ax[0].set_title("A  Profit of a real shock (SIG follows)", loc="left", fontsize=10)
    ax[0].legend(fontsize=8)
    ax[1].plot(k, hoax, "o-", color="C3", lw=2, label="false shock: outside reverts, SIG never moved")
    ax[1].axhline(-2500, color="grey", ls="--", lw=1, label="proposed sleeve stop -2,500")
    ax[1].set_ylabel("loss per false shock (S)")
    ax[1].set_title("B  Cost of a false shock", loc="left", fontsize=10)
    ax[1].legend(fontsize=8)
    for p, c in ((0.10, "k"), (0.25, "C1")):
        ax[2].plot(k, [(1 - p) * a + p * b for a, b in zip(prof, hoax)], "o-", color=c, lw=2 if p == 0.10 else 1,
                   label=f"{int(p * 100)}% of shocks false")
    ax[2].axvline(40, color="grey", ls=":", lw=1)
    ax[2].text(41, 0, "proposed: 40k", fontsize=8)
    ax[2].axhline(0, color="grey", lw=0.8)
    ax[2].set_ylabel("expected profit per shock (S)")
    ax[2].set_xlabel("per-shock notional cap, both legs (thousand S)")
    ax[2].set_title("C  Net expected profit per shock", loc="left", fontsize=10)
    ax[2].legend(fontsize=8)
    fig.suptitle("Shock mode: how big to go per shock", fontsize=12)
    fig.text(0.02, 0.005, "Glossary: shock = Polymarket and Kalshi both jump 5+ pts while SIG's market maker lags. Replay = "
             "real SIG quotes (bot venue_fair, 22-24 Oct),\nmedian live ladder, walk floor((M-2.5)/2) ticks, exit at 60 min. "
             "False shock = outside reverts within minutes and SIG never moves.", fontsize=7.5, va="bottom")
    fig.tight_layout(rect=(0, 0.05, 1, 0.97))
    fig.savefig(os.path.join(ROOT, "research_notes", "figures", "shock_mode_decision.png"), dpi=130)
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["shockcap"]:
        print(shock_cap_curve())
    elif sys.argv[1:] == ["venues"]:
        for k, v in venue_split().items():
            print(k, v)
    elif sys.argv[1:] == ["lagall"]:
        for flt in (True, False):
            r = lag_study(flt)
            for k, v in r["by_size"].items():
                print("filtered" if flt else "unfiltered", k, "n", v["n"], {h: v[f"sig_{h}"] for h in (10, 60, 240, 720)})
    elif sys.argv[1:] == ["replay"]:
        for k, v in replay_big().items():
            print(k, v["total"], v["per_event"])
    elif sys.argv[1:] == ["walker"]:
        r = walker_2024()
        print(r["n_legs"], "legs")
        for k, v in r.items():
            if k.startswith("v"):
                for idea, x in v.items():
                    print(k, idea, {kk: vv for kk, vv in x.items() if kk != "rows"})
    elif sys.argv[1:] == ["plot2"]:
        plot_measured()
    elif sys.argv[1:] == ["jumps"]:
        r = jump_rate_2024()
        print(r["n_races"], "races")
        for k, v in r["rate"].items():
            print(k, v)
    elif sys.argv[1:] == ["refill"]:
        for k, v in refill_study()["summary"].items():
            print(k, v)
    elif sys.argv[1:] == ["rate"]:
        r = base_rate()
        print(r["days"], "days", r["n"], "events", r["per_day"], "/day")
        for e in r["events"]:
            print(time.strftime("%m-%d %H:%M", time.gmtime(e["t"])), e)
    elif sys.argv[1:] == ["lag"]:
        r = lag_study()
        for k, v in r["pnl"].items():
            print(k, v)
    elif sys.argv[1:] == ["plot"]:
        plot()
    else:
        r = analyse()
        for e in r["events"]:
            print("==", e["event"], e["t0_utc"])
            for m in e["markets"]:
                if "error" in m:
                    print("  ERR", m["market"], m["error"])
                    continue
                print(f"  {m['market'][:48]:48s} pre {m['pre']:.3f} 24h {m['move_24h_pts']:+6.1f} peak {m['peak_pts']:+6.1f}"
                      f"@{m['peak_min']:>4} t50 {m['t50_min']} t90 {m['t90_min']} beta {m.get('beta_logit_vs_lead', '-')}"
                      f" | 1/5/10/30/60/240: " + " ".join(str(m['move_pts'][str(h)]) for h in (1, 5, 10, 30, 60, 240)))
        print("depth", r["depth"])
        for kk, v in r["sizing"].items():
            if "cash50000" in kk or "cash5000_" in kk:
                print("size", kk, v)
        for c, v in r["ev"]["cases"].items():
            print("per-pt", c, v["per_pt_of_12h_move"], "n", v["n"])
            print("ev", c, "pnl", v["median_pnl_pts"], "capture", v["median_capture_of_gap"])









