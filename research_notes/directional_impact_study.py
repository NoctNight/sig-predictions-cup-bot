"""Directional trades toward the outside consensus on liquid SIG legs, with a market-impact model for sizing.

Research only: reads data/ (DB opened read-only), writes data/research_directional/. No venue calls.

    python3 research_notes/directional_impact_study.py            # everything, prints markdown tables
    python3 research_notes/directional_impact_study.py events     # event study only
    python3 research_notes/directional_impact_study.py impact     # impact model only

Two event sets:
  A  7-21 Oct: SIG 5-min / 1-h candle closes (event clock, forward-filled) vs Polymarket 5-min + Kalshi 1-min D-leg history
     (data/model_history.json). SIG mid is not observed: the close stands in for it and a per-leg spread is charged.
  B  21 Oct 23:49 - 22 Oct 22:21 UTC: venue_fair (the direction sleeve's own poll, ~2 min): real SIG bid/ask + outside fair.
Split: IS = A before 17 Oct 00:00 UTC; OOS1 = A from 17 Oct; OOS2 = B (true quotes, never used for any choice).
"""
import bisect
import collections
import json
import math
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from sigbot.proposals.volatile_moves import load_markets, load_outside  # noqa: E402  (pure loaders, no side effects)

TICK = 0.005
OUT = os.path.join(HERE, "data", "research_directional")
SPLIT_TS = 1792195200.0          # 2026-10-17 00:00 UTC
BUCKETS = [(1, 2), (2, 4), (4, 6), (6, 10), (10, 99)]
HORIZONS = [("15m", 900), ("1h", 3600), ("4h", 14400), ("24h", 86400)]
SPACING = 3600.0                 # one event per race per hour (de-clustering)
MAX_DISAGREE = 0.04              # PM-Kalshi agreement for a "consensus"
EXIT_H = [1, 4, 12, 24]          # max holding hours for the exit rule
CLOSE_THR = 0.0                  # exit when the signed gap (fair - mid, toward our side) <= this many ticks


def db():
    return sqlite3.connect("file:" + os.path.join(HERE, "data", "sigbot.sqlite") + "?mode=ro", uri=True)


# ----------------------------------------------------------------------------- books, liquidity
def load_books():
    b = json.load(open(os.path.join(HERE, "data", "books.json")))
    out = {}
    for ex, v in b.items():
        bids = sorted([(float(x["price"]), float(x["quantity"])) for x in v.get("bids") or []], reverse=True)
        asks = sorted([(float(x["price"]), float(x["quantity"])) for x in v.get("asks") or []])
        if bids and asks:
            out[str(ex)] = (bids, asks)
    return out


def walk(levels, q):
    """Average distance (ticks) from the touch of a q-share market order through `levels` [(px, qty)] (touch first)."""
    if not levels:
        return None
    touch = levels[0][0]
    left, cost = q, 0.0
    for px, qty in levels:
        take = min(left, qty)
        cost += take * abs(px - touch)
        left -= take
        if left <= 0:
            break
    if left > 0:
        return None
    return cost / q / TICK


def depth_within(levels, ticks):
    if not levels:
        return 0.0
    t = levels[0][0]
    return sum(q for p, q in levels if abs(p - t) <= ticks * TICK + 1e-9)


def leg_liquidity(books, vf_spreads):
    """exchange -> dict(touch_bid, touch_ask, d5_bid, d5_ask, med_spread_ticks, liquid)."""
    out = {}
    for ex, (bids, asks) in books.items():
        sp = vf_spreads.get(ex)
        mid = (bids[0][0] + asks[0][0]) / 2
        d = dict(touch_bid=bids[0][1], touch_ask=asks[0][1], d5_bid=depth_within(bids, 5), d5_ask=depth_within(asks, 5),
                 med_spread=sp if sp is not None else (asks[0][0] - bids[0][0]) / TICK, mid=mid)
        d["liquid"] = (d["med_spread"] <= 1.0 + 1e-9 and min(d["touch_bid"], d["touch_ask"]) >= 2000
                       and min(d["d5_bid"], d["d5_ask"]) >= 20000 and 0.03 <= mid <= 0.97)
        out[ex] = d
    return out


# ----------------------------------------------------------------------------- data set B (live polls)
def load_B(con):
    rows = con.execute("SELECT ts, race, party, pm, kalshi, fair, sig_bid, sig_ask, sig_mid, exchange_id FROM venue_fair "
                       "WHERE party IN ('D','R') AND sig_bid IS NOT NULL AND sig_ask IS NOT NULL ORDER BY ts").fetchall()
    legs = collections.defaultdict(list)
    for ts, race, party, pm, k, fair, bid, ask, mid, ex in rows:
        legs[(race, party, str(ex))].append((ts, pm, k, fair, bid, ask, mid))
    return legs


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def B_spreads(legsB):
    return {ex: median([(a - b) / TICK for _, _, _, _, b, a, _ in s]) for (_, _, ex), s in legsB.items()}


def series_B(legsB, liq):
    """race -> list of points for its D leg (else R leg): dict(ts, fair, bid, ask, mid, pm, k), YES terms of that leg."""
    by_race = {}
    for (race, party, ex), s in legsB.items():
        if party == "R" and (race, "D") in {(r, p) for (r, p, _) in legsB}:
            continue
        pts = [dict(ts=ts, pm=pm, k=k, fair=fair, bid=b, ask=a, mid=m) for ts, pm, k, fair, b, a, m in s]
        by_race[race] = dict(ex=ex, party=party, pts=pts, liquid=liq.get(ex, {}).get("liquid", False))
    return by_race


# ----------------------------------------------------------------------------- data set A (candles + outside history)
def load_sig_prices():
    """exchange -> (ts list, close list), event clock: 5-min closes stamped at bar end; 1-h closes before the first 5-min bar."""
    c5 = json.load(open(os.path.join(HERE, "data", "candles_5m.json")))
    c1 = json.load(open(os.path.join(HERE, "data", "hist.json")))
    from datetime import datetime

    def p(t):
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    out = {}
    for ex in set(c5) | set(c1):
        a = [(p(c["time"]) + 300, c["close"]) for c in (c5.get(ex) or {}).get("candles") or []]
        first5 = a[0][0] if a else float("inf")
        b = [(p(c["time"]) + 3600, c["close"]) for c in (c1.get(ex) or {}).get("candles") or [] if p(c["time"]) + 3600 < first5]
        s = sorted(b + a)
        if s:
            out[str(ex)] = ([x[0] for x in s], [x[1] for x in s])
    return out


def at(series, t, max_age=None):
    if not series:
        return None
    ts, px = series
    i = bisect.bisect_right(ts, t) - 1
    if i < 0:
        return None
    if max_age is not None and t - ts[i] > max_age:
        return None
    return px[i]


def series_A(mk, liq, spreads):
    outside = load_outside()
    sig = load_sig_prices()
    d_leg = {}
    for ex, (race, party, _) in mk.items():
        if party == "D":
            d_leg[race] = ex
    h = json.load(open(os.path.join(HERE, "data", "model_history.json")))
    t0, t1 = float(h["start"]), float(h["end"])
    grid = [t0 + 900 * i for i in range(int((t1 - t0) / 900) + 1)]
    by_race = {}
    for race, o in outside.items():
        ex = d_leg.get(race)
        if ex is None or ex not in sig:
            continue
        sp = spreads.get(ex) or 1.0
        pts = []
        for t in grid:
            m = at(sig[ex], t)
            pm = at(o.get("pm"), t, 1800)
            k = at(o.get("kalshi"), t, 1800)
            if m is None or (pm is None and k is None):
                continue
            fair = (pm + k) / 2 if (pm is not None and k is not None) else (pm if pm is not None else k)
            half = sp * TICK / 2
            pts.append(dict(ts=t, pm=pm, k=k, fair=fair, bid=m - half, ask=m + half, mid=m))
        if pts:
            by_race[race] = dict(ex=ex, party="D", pts=pts, liquid=liq.get(ex, {}).get("liquid", False))
    return by_race


# ----------------------------------------------------------------------------- events
def bucket_of(g):
    a = abs(g)
    for lo, hi in BUCKETS:
        if lo <= a < hi:
            return f"{lo}-{hi}" if hi < 99 else f"{lo}+"
    return None


def find_events(by_race, label, consensus=True, liquid_only=True, max_gap_poll=1200):
    """One event per race per SPACING seconds when |fair - mid| >= 1 tick. Returns list of dicts."""
    evs = []
    for race, d in by_race.items():
        if liquid_only and not d["liquid"]:
            continue
        pts = d["pts"]
        ts = [p["ts"] for p in pts]
        last = -1e18
        for i, p in enumerate(pts):
            if p["ts"] - last < SPACING:
                continue
            if consensus and (p["pm"] is None or p["k"] is None or abs(p["pm"] - p["k"]) > MAX_DISAGREE):
                continue
            if not (0.03 <= p["mid"] <= 0.97) or (p["ask"] - p["bid"]) > 2 * TICK + 1e-9:
                continue
            g = (p["fair"] - p["mid"]) / TICK
            b = bucket_of(g)
            if b is None:
                continue
            s = 1.0 if g > 0 else -1.0
            ev = dict(set=label, race=race, ex=d["ex"], ts=p["ts"], gap=g, bucket=b, sgn=s, mid=p["mid"], fair=p["fair"],
                      spread=(p["ask"] - p["bid"]) / TICK, entry=p["ask"] if s > 0 else p["bid"])
            # horizon moves
            for name, h in HORIZONS:
                j = bisect.bisect_right(ts, p["ts"] + h) - 1
                if j > i and (p["ts"] + h) - ts[j] <= max_gap_poll and p["ts"] + h <= ts[-1]:
                    ev["dm_" + name] = s * (pts[j]["mid"] - p["mid"]) / TICK
                    ev["df_" + name] = s * (pts[j]["fair"] - p["fair"]) / TICK
                    ev["gapleft_" + name] = s * (pts[j]["fair"] - pts[j]["mid"]) / TICK
            # exit rule: gap closed (signed gap <= CLOSE_THR) or H hours
            for H in EXIT_H:
                end = p["ts"] + H * 3600
                if end > ts[-1]:
                    continue
                k = None
                for jj in range(i + 1, len(pts)):
                    q = pts[jj]
                    if q["ts"] > end:
                        break
                    if s * (q["fair"] - q["mid"]) / TICK <= CLOSE_THR:
                        k = jj
                        break
                if k is None:
                    k = bisect.bisect_right(ts, end) - 1
                q = pts[k]
                exit_touch = q["bid"] if s > 0 else q["ask"]
                ev[f"pnl_tt_{H}h"] = s * (exit_touch - ev["entry"]) / TICK            # taker in, taker out
                ev[f"pnl_mm_{H}h"] = s * (q["mid"] - p["mid"]) / TICK                 # mid to mid (gross)
                ev[f"pnl_tp_{H}h"] = s * ((q["ask"] if s > 0 else q["bid"]) - ev["entry"]) / TICK  # taker in, passive out (upper bound)
                ev[f"hold_{H}h"] = (q["ts"] - p["ts"]) / 3600
                ev[f"exitspread_{H}h"] = (q["ask"] - q["bid"]) / TICK
            evs.append(ev)
            last = p["ts"]
    return evs


def clustered(evs, key):
    """mean, cluster-robust t (clusters = races), n, n clusters."""
    xs = [(e["race"], e[key]) for e in evs if e.get(key) is not None]
    n = len(xs)
    if n < 2:
        return (xs[0][1] if xs else None), None, n, len({r for r, _ in xs})
    m = sum(x for _, x in xs) / n
    g = collections.defaultdict(float)
    for r, x in xs:
        g[r] += x - m
    G = len(g)
    if G < 2:
        return m, None, n, G
    var = sum(v * v for v in g.values()) / n / n * G / (G - 1)
    return m, (m / math.sqrt(var) if var > 0 else None), n, G


def sd(evs, key):
    xs = [e[key] for e in evs if e.get(key) is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def drop_top(evs, key, k=3):
    tot = collections.defaultdict(float)
    for e in evs:
        if e.get(key) is not None:
            tot[e["race"]] += e[key]
    top = {r for r, _ in sorted(tot.items(), key=lambda kv: -kv[1])[:k]}
    return [e for e in evs if e["race"] not in top], top


def fmt(m, t=None, nd=2):
    if m is None:
        return "-"
    s = f"{m:+.{nd}f}"
    if t is not None:
        s += f" ({t:+.1f})"
    return s


# ----------------------------------------------------------------------------- impact
def impact_static(books, liq):
    qs = [250, 500, 1000, 2500, 5000, 10000, 25000, 50000]
    rows = collections.defaultdict(list)
    fits = []
    for ex, (bids, asks) in books.items():
        L = liq.get(ex)
        if not L or not L["liquid"]:
            continue
        for side, lv in (("buy", asks), ("sell", bids)):
            ws = []
            for q in qs:
                w = walk(lv, q)
                rows[q].append(w)
                ws.append((q, w))
            # fit walk(q) = k * q / touch  (through origin) on q <= 25k, and power law fit
            pts = [(q / lv[0][1], w) for q, w in ws if w is not None and q <= 25000]
            if len(pts) >= 3:
                kk = sum(x * y for x, y in pts) / sum(x * x for x, _ in pts)
                pts2 = [(q / 1000.0, w) for q, w in ws if w is not None and q <= 25000]
                k2 = sum(x * y for x, y in pts2) / sum(x * x for x, _ in pts2)
                fits.append(dict(ex=ex, side=side, touch=lv[0][1], k_touch=kk, k_per_1k=k2, d5=depth_within(lv, 5)))
    return qs, rows, fits


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def realised_taker(con):
    """Our taker orders (arb/direction/exit): VWAP vs touch and vs mid at place (bought-side terms), and the mid drift after."""
    vf = collections.defaultdict(list)
    for ts, ex, b, a in con.execute("SELECT ts, exchange_id, sig_bid, sig_ask FROM venue_fair WHERE sig_bid IS NOT NULL"):
        vf[str(ex)].append((ts, b, a))
    for ts, ex, b, a in con.execute("SELECT ts, exchange_id, best_bid, best_ask FROM quotes WHERE best_bid IS NOT NULL AND best_ask IS NOT NULL"):
        vf[str(ex)].append((ts, b, a))
    for ex in vf:
        vf[ex].sort()
    vts = {ex: [x[0] for x in v] for ex, v in vf.items()}

    def book_at(ex, t, before=True, max_age=150):
        s = vf.get(ex)
        if not s:
            return None
        i = bisect.bisect_right(vts[ex], t) - 1 if before else bisect.bisect_left(vts[ex], t)
        if i < 0 or i >= len(s):
            return None
        if abs(s[i][0] - t) > max_age:
            return None
        return s[i]
    fills = collections.defaultdict(list)
    for oid, ex, side, price, qty, purpose in con.execute(
            "SELECT order_id, exchange_id, side, price, quantity, purpose FROM fills WHERE purpose IN ('arb','direction','exit')"):
        fills[oid].append((str(ex), side, price, qty, purpose))
    placed = {oid: (pa, lim, q, side) for oid, pa, lim, q, side in con.execute(
        "SELECT order_id, placed_at, price, quantity, side FROM orders WHERE purpose IN ('arb','direction','exit')")}
    out = []
    for oid, fl in fills.items():
        if oid not in placed:
            continue
        pa, lim, oq, oside = placed[oid]
        ex = fl[0][0]
        side = oside  # bought side
        q = sum(abs(f[3]) for f in fl)
        if q <= 0:
            continue
        # fill price in bought-side terms (fills store YES price)
        vw = sum(abs(f[3]) * (f[2] if side == "yes" else 1 - f[2]) for f in fl) / q
        bk = book_at(ex, pa)
        if bk is None:
            continue
        _, b, a = bk
        touch = a if side == "yes" else 1 - b
        mid = (a + b) / 2 if side == "yes" else 1 - (a + b) / 2
        r = dict(oid=oid, ex=ex, purpose=fl[0][4], q=q, slip_touch=(vw - touch) / TICK, cost_mid=(vw - mid) / TICK,
                 spread=(a - b) / TICK, levels=len({round(f[2], 3) for f in fl}))
        m0 = (a + b) / 2
        sgn = 1.0 if side == "yes" else -1.0
        for name, h in (("1m", 60), ("5m", 300), ("30m", 1800), ("2h", 7200)):
            bk2 = book_at(ex, pa + h, before=True, max_age=300)
            r["drift_" + name] = sgn * (((bk2[1] + bk2[2]) / 2) - m0) / TICK if bk2 else None
        out.append(r)
    return out


# ----------------------------------------------------------------------------- sizing
def optimal_q(m_net, k_per_1k, sigma, risk_tol, cap_q=None):
    """Maximise q*(m_net - k*q/1000) - q^2 sigma^2 tick^2/(2 T) with the per-share move m_net (ticks, after spread) and
    walk cost on entry + exit of k ticks per 1,000 shares each way (so 2k in total). Returns q* (shares)."""
    if m_net is None or m_net <= 0:
        return 0.0
    a = 2 * k_per_1k / 1000.0 * TICK            # S per share per share (entry + exit walk), linear model
    b = (sigma * TICK) ** 2 / risk_tol if risk_tol else 0.0
    q = (m_net * TICK) / (2 * a + b)
    if cap_q is not None:
        q = min(q, cap_q)
    return q


def main(what="all"):
    os.makedirs(OUT, exist_ok=True)
    con = db()
    mk = load_markets()
    books = load_books()
    legsB = load_B(con)
    spreads = B_spreads(legsB)
    liq = leg_liquidity(books, spreads)
    n_liq = sum(1 for v in liq.values() if v["liquid"])
    print(f"# liquid legs: {n_liq} of {len(liq)}")
    res = {}
    if what in ("all", "events"):
        A = series_A(mk, liq, spreads)
        B = series_B(legsB, liq)
        evA = find_events(A, "A")
        evB = find_events(B, "B")
        evB_all = find_events(B, "B", liquid_only=False)
        evA_all = find_events(A, "A", liquid_only=False)
        for e in evA + evA_all:
            e["split"] = "IS" if e["ts"] < SPLIT_TS else "OOS1"
        for e in evB + evB_all:
            e["split"] = "OOS2"
        json.dump(dict(liquid=evA + evB, all=evA_all + evB_all), open(os.path.join(OUT, "events.json"), "w"))
        print(f"# events liquid A {len(evA)} B {len(evB)}; all-legs A {len(evA_all)} B {len(evB_all)}")
        days = dict(IS=(SPLIT_TS - 1791417425) / 86400, OOS1=(1792627025 - SPLIT_TS) / 86400)
        tsB = [p["ts"] for d in B.values() for p in d["pts"]]
        days["OOS2"] = (max(tsB) - min(tsB)) / 86400
        res["days"] = days
        print(f"# days {days}")
        ev = evA + evB
        print("\n## Horizon moves toward fair (ticks, signed; cluster t by race)\n")
        print("| split | bucket | n | races | mean gap | SIG move +15m | +1h | +4h | +24h | fair move +4h | gap left +4h |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for sp in ("IS", "OOS1", "OOS2"):
            for lo, hi in BUCKETS:
                b = f"{lo}-{hi}" if hi < 99 else f"{lo}+"
                es = [e for e in ev if e["split"] == sp and e["bucket"] == b]
                if not es:
                    continue
                g = sum(abs(e["gap"]) for e in es) / len(es)
                cells = [fmt(*clustered(es, "dm_" + h)[:2]) for h, _ in HORIZONS]
                print(f"| {sp} | {b} | {len(es)} | {len({e['race'] for e in es})} | {g:.1f} | " + " | ".join(cells)
                      + f" | {fmt(*clustered(es, 'df_4h')[:2])} | {fmt(*clustered(es, 'gapleft_4h')[:2])} |")
        print("\n## Exit rule: buy the touch, sell the touch when the gap closes or after H hours (ticks/share)\n")
        print("| split | bucket | H | n | taker/taker | t | mid/mid | taker in, passive out | mean hold h | sd T/T | T/T w/o top-3 races |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        table = {}
        for sp in ("IS", "OOS1", "OOS2"):
            for lo, hi in BUCKETS:
                b = f"{lo}-{hi}" if hi < 99 else f"{lo}+"
                for H in EXIT_H:
                    es = [e for e in ev if e["split"] == sp and e["bucket"] == b and e.get(f"pnl_tt_{H}h") is not None]
                    if len(es) < 3:
                        continue
                    m, t, n, G = clustered(es, f"pnl_tt_{H}h")
                    mm = clustered(es, f"pnl_mm_{H}h")[0]
                    tp = clustered(es, f"pnl_tp_{H}h")[0]
                    hold = sum(e[f"hold_{H}h"] for e in es) / len(es)
                    dr, top = drop_top(es, f"pnl_tt_{H}h")
                    md, tdd, nd, _ = clustered(dr, f"pnl_tt_{H}h")
                    table[(sp, b, H)] = dict(n=n, races=G, m=m, t=t, mm=mm, tp=tp, hold=hold, sd=sd(es, f"pnl_tt_{H}h"),
                                             m_drop=md, t_drop=tdd)
                    print(f"| {sp} | {b} | {H} | {n} | {fmt(m)} | {fmt(t, nd=1) if t is not None else '-'} | {fmt(mm)} | {fmt(tp)} | {hold:.1f} | "
                          f"{(sd(es, f'pnl_tt_{H}h') or 0):.1f} | {fmt(md, tdd)} |")
        res["table"] = {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in table.items()}
        # pooled >= 4 ticks, and all-legs comparison
        print("\n## Pooled gap >= 4 ticks and >= 6 ticks, H = 4 h and 12 h, liquid vs all legs\n")
        print("| set | split | gap | H | n | races | T/T | mid/mid | T/T w/o top-3 |")
        print("|---|---|---|---|---|---|---|---|---|")
        for label, pool in (("liquid", evA + evB), ("all legs", evA_all + evB_all)):
            for sp in ("IS", "OOS1", "OOS2"):
                for gmin in (4, 6):
                    for H in (4, 12):
                        es = [e for e in pool if e["split"] == sp and abs(e["gap"]) >= gmin and e.get(f"pnl_tt_{H}h") is not None]
                        if len(es) < 3:
                            continue
                        m, t, n, G = clustered(es, f"pnl_tt_{H}h")
                        mm, tm, _, _ = clustered(es, f"pnl_mm_{H}h")
                        dr, _ = drop_top(es, f"pnl_tt_{H}h")
                        print(f"| {label} | {sp} | >={gmin} | {H} | {n} | {G} | {fmt(m, t)} | {fmt(mm, tm)} | {fmt(*clustered(dr, f'pnl_tt_{H}h')[:2])} |")
        # signal frequency (liquid, per day)
        print("\n## Signal frequency (de-clustered events per day, liquid legs, consensus)\n")
        print("| split | " + " | ".join(f"{lo}-{hi}" if hi < 99 else f"{lo}+" for lo, hi in BUCKETS) + " |")
        print("|---|" + "---|" * len(BUCKETS))
        freq = {}
        for sp in ("IS", "OOS1", "OOS2"):
            cells = []
            for lo, hi in BUCKETS:
                b = f"{lo}-{hi}" if hi < 99 else f"{lo}+"
                n = sum(1 for e in ev if e["split"] == sp and e["bucket"] == b)
                freq[(sp, b)] = n / days[sp]
                cells.append(f"{n / days[sp]:.0f}")
            print(f"| {sp} | " + " | ".join(cells) + " |")
        res["freq"] = {f"{k[0]}|{k[1]}": v for k, v in freq.items()}
    if what in ("all", "impact"):
        qs, rows, fits = impact_static(books, liq)
        print("\n## Static walk from the 21 Oct 22:03 books (liquid legs, both sides): extra ticks/share beyond the touch\n")
        print("| q | p25 | median | p75 | p90 | share of books too thin |")
        print("|---|---|---|---|---|---|")
        for q in qs:
            xs = rows[q]
            thin = sum(1 for x in xs if x is None) / len(xs)
            print(f"| {q:,} | {fmt(pct(xs, .25))} | {fmt(pct(xs, .5))} | {fmt(pct(xs, .75))} | {fmt(pct(xs, .9))} | {thin:.0%} |")
        kt = [f["k_touch"] for f in fits]
        k1 = [f["k_per_1k"] for f in fits]
        tch = [f["touch"] for f in fits]
        print(f"\nFit walk(q) = kappa * q / touch: kappa median {median(kt):.3f} (p25 {pct(kt, .25):.3f}, p75 {pct(kt, .75):.3f}); "
              f"walk(q) = k * q/1000: k median {median(k1):.3f} ticks per 1k shares (p25 {pct(k1, .25):.3f}, p75 {pct(k1, .75):.3f}); "
              f"touch size median {median(tch):,.0f} (p25 {pct(tch, .25):,.0f})")
        res["impact_static"] = dict(kappa_med=median(kt), k1_med=median(k1), k1_p75=pct(k1, .75), k1_p90=pct(k1, .9), touch_med=median(tch),
                                    walk={q: (pct(rows[q], .5), pct(rows[q], .75)) for q in qs})
        rt = realised_taker(con)
        json.dump(rt, open(os.path.join(OUT, "taker_orders.json"), "w"))
        print("\n## Our realised taker orders (VWAP vs touch / mid at place, bought-side terms; drift signed with our trade)\n")
        print("| purpose | size | n | slip beyond touch | cost vs mid | multi-level share | mid drift +1m | +5m | +30m | +2h |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        for pur in ("arb", "direction", "exit"):
            for lo, hi in ((0, 250), (250, 1000), (1000, 3000), (3000, 1e9)):
                es = [r for r in rt if r["purpose"] == pur and lo <= r["q"] < hi]
                if not es:
                    continue

                def mean(k):
                    xs = [r[k] for r in es if r.get(k) is not None]
                    return (sum(xs) / len(xs), len(xs)) if xs else (None, 0)
                ml = sum(1 for r in es if r["levels"] > 1) / len(es)
                hi_s = f"{int(hi):,}" if hi < 1e9 else "+"
                print(f"| {pur} | {int(lo):,}-{hi_s} | {len(es)} | {fmt(mean('slip_touch')[0])} | {fmt(mean('cost_mid')[0])} | {ml:.0%} | "
                      + " | ".join(fmt(mean('drift_' + h)[0]) + f" (n {mean('drift_' + h)[1]})" for h in ('1m', '5m', '30m', '2h')) + " |")
        res["realised"] = dict(n=len(rt))
    json.dump(res, open(os.path.join(OUT, "summary.json"), "w"), indent=1, default=str)
    return res


def clustered_by(evs, key, cl):
    """mean and cluster-robust t with an arbitrary cluster function."""
    xs = [(cl(e), e[key]) for e in evs if e.get(key) is not None]
    n = len(xs)
    if n < 2:
        return None, None, n, 0
    m = sum(x for _, x in xs) / n
    g = collections.defaultdict(float)
    for c, x in xs:
        g[c] += x - m
    G = len(g)
    var = sum(v * v for v in g.values()) / n / n * G / max(G - 1, 1)
    return m, (m / math.sqrt(var) if var > 0 else None), n, G


def tape_series(con, exs):
    """exchange -> (ts, last trade price) from the tape (for replicating method A on the B window)."""
    from datetime import datetime
    out = collections.defaultdict(list)
    q = "SELECT executed_at, exchange_id, price FROM tape WHERE exchange_id IN (%s) ORDER BY executed_at" % ",".join("?" * len(exs))
    for t, ex, p in con.execute(q, list(exs)):
        out[str(ex)].append((datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp(), p))
    return {ex: ([a for a, _ in v], [b for _, b in v]) for ex, v in out.items()}


def robust():
    con = db()
    books = load_books()
    legsB = load_B(con)
    spreads = B_spreads(legsB)
    liq = leg_liquidity(books, spreads)
    B = series_B(legsB, liq)
    evB = find_events(B, "B")
    # market factor: mean D-leg mid change of the other liquid races over the same window
    grid = {}
    for race, d in B.items():
        if d["liquid"]:
            grid[race] = ([p["ts"] for p in d["pts"]], [p["mid"] for p in d["pts"]])

    def factor(race, t0, t1):
        xs = []
        for r, s in grid.items():
            if r == race:
                continue
            a, b = at(s, t0, 600), at(s, t1, 600)
            if a is not None and b is not None:
                xs.append((b - a) / TICK)
        return sum(xs) / len(xs) if xs else None
    for e in evB:
        for H in (4, 12):
            if e.get(f"pnl_tt_{H}h") is None:
                continue
            f = factor(e["race"], e["ts"], e["ts"] + e[f"hold_{H}h"] * 3600)
            e[f"fac_{H}h"] = e["sgn"] * f if f is not None else None
            e[f"adj_{H}h"] = e[f"pnl_tt_{H}h"] - e[f"fac_{H}h"] if f is not None else None
            fav = (e["fair"] >= 0.5) == (e["sgn"] > 0)
            e["fav"] = fav
    print("\n## OOS2 (live quotes) robustness: time clusters, market factor, favourite side\n")
    print("| gap | H | n | T/T | t race-clustered | t 2-h-block clustered (blocks) | market factor (signed) | T/T minus factor (t, 2-h blocks) | buy-YES share | entries span (UTC) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    from datetime import datetime, timezone
    for gmin in (4, 6, 10):
        for H in (4, 12):
            es = [e for e in evB if abs(e["gap"]) >= gmin and e.get(f"pnl_tt_{H}h") is not None]
            if len(es) < 3:
                continue
            m, t, n, _ = clustered(es, f"pnl_tt_{H}h")
            _, t2, _, G2 = clustered_by(es, f"pnl_tt_{H}h", lambda e: int(e["ts"] // 7200))
            fm = clustered_by(es, f"fac_{H}h", lambda e: int(e["ts"] // 7200))[0]
            am, at2, _, _ = clustered_by(es, f"adj_{H}h", lambda e: int(e["ts"] // 7200))
            up = sum(1 for e in es if e["sgn"] > 0) / len(es)
            span = (datetime.fromtimestamp(min(e["ts"] for e in es), timezone.utc).strftime("%d %H:%M") + "-"
                    + datetime.fromtimestamp(max(e["ts"] for e in es), timezone.utc).strftime("%d %H:%M"))
            print(f"| >={gmin} | {H} | {n} | {fmt(m)} | {fmt(t, nd=1) if t else '-'} | {fmt(t2, nd=1) if t2 else '-'} ({G2}) | {fmt(fm)} | {fmt(am, at2)} | {up:.0%} | {span} |")
    print("\n| gap | H | side | n | T/T (t race) | mid/mid |")
    print("|---|---|---|---|---|---|")
    for gmin in (4, 10):
        for H in (4, 12):
            for fav in (True, False):
                es = [e for e in evB if abs(e["gap"]) >= gmin and e.get(f"pnl_tt_{H}h") is not None and e.get("fav") == fav]
                if len(es) < 3:
                    continue
                print(f"| >={gmin} | {H} | {'favourite' if fav else 'longshot'} | {len(es)} | {fmt(*clustered(es, f'pnl_tt_{H}h')[:2])} | {fmt(clustered(es, f'pnl_mm_{H}h')[0])} |")
    # Method check: rebuild B with SIG tape last-trade as "mid" (method A) on the same window
    exs = {d["ex"] for d in B.values()}
    tp = tape_series(con, exs)
    B2 = {}
    for race, d in B.items():
        s = tp.get(d["ex"])
        if not s:
            continue
        sp = spreads.get(d["ex"]) or 1.0
        pts = []
        for p in d["pts"]:
            m = at(s, p["ts"])
            if m is None:
                continue
            pts.append(dict(p, mid=m, bid=m - sp * TICK / 2, ask=m + sp * TICK / 2))
        if pts:
            B2[race] = dict(d, pts=pts)
    evB2 = find_events(B2, "B-tape")
    print("\n## Method check on the B window: true quotes (B) vs tape last-trade as mid (method A)\n")
    print("| method | gap | H | n | mid/mid | T/T |")
    print("|---|---|---|---|---|---|")
    for lab, es0 in (("quotes", evB), ("tape close", evB2)):
        for gmin in (4, 6):
            for H in (4, 12):
                es = [e for e in es0 if abs(e["gap"]) >= gmin and e.get(f"pnl_tt_{H}h") is not None]
                if len(es) < 3:
                    continue
                print(f"| {lab} | >={gmin} | {H} | {len(es)} | {fmt(*clustered(es, f'pnl_mm_{H}h')[:2])} | {fmt(*clustered(es, f'pnl_tt_{H}h')[:2])} |")
    # A by day: is any day like OOS2?
    mk = load_markets()
    A = series_A(mk, liq, spreads)
    evA = find_events(A, "A")
    print("\n## Method A by UTC day, gap >= 6, H = 12 h (liquid legs)\n")
    print("| day | n | races | mid/mid (t race) | T/T | market factor not computed |")
    print("|---|---|---|---|---|---|")
    byday = collections.defaultdict(list)
    for e in evA:
        if abs(e["gap"]) >= 6 and e.get("pnl_tt_12h") is not None:
            byday[datetime.fromtimestamp(e["ts"], timezone.utc).strftime("%m-%d")].append(e)
    pos = 0
    for d in sorted(byday):
        es = byday[d]
        m = clustered(es, "pnl_tt_12h")[0]
        pos += m > 0
        print(f"| {d} | {len(es)} | {len({e['race'] for e in es})} | {fmt(*clustered(es, 'pnl_mm_12h')[:2])} | {fmt(m)} | |")
    print(f"\nDays with positive T/T: {pos} of {len(byday)}")
    json.dump(dict(B=evB, B_tape=evB2), open(os.path.join(OUT, "events_B_robust.json"), "w"), default=str)


def sizing():
    """q* per liquid book: maximise q*(m - walk_in(q) - walk_out(q)) - (q*sigma*tick)^2/(2T), walk from the real ladder
    (buy walks the asks, the exit walks the bids; and the mirror for NO buys). m = expected touch-to-touch gain per share
    (ticks) at small size, i.e. after the 1-tick round-trip spread."""
    books = load_books()
    con = db()
    liq = leg_liquidity(books, B_spreads(load_B(con)))
    grid = [100 * i for i in range(1, 201)] + [20000 + 1000 * i for i in range(1, 81)]
    print("\n## Optimal size per trade from the real ladders (liquid legs; median and p25-p75 across books x sides)\n")
    print("| m (ticks/share after spread) | sigma | risk tolerance T (S) | q* median | p25-p75 | notional at q* (median) | E[S] per trade at q* | E[S] at 750 sh (cap) |")
    print("|---|---|---|---|---|---|---|---|")
    for m, sigma in ((0.68, 2.9), (1.0, 2.9), (1.65, 2.9), (-0.62, 2.2)):
        for T in (None, 2000.0, 500.0):
            qs, ns, gs = [], [], []
            for ex, (bids, asks) in books.items():
                if not liq.get(ex, {}).get("liquid"):
                    continue
                mid = (bids[0][0] + asks[0][0]) / 2
                for inn, out, px in ((asks, bids, asks[0][0]), (bids, asks, 1 - bids[0][0])):
                    best, bq = 0.0, 0
                    for q in grid:
                        wi, wo = walk(inn, q), walk(out, q)
                        if wi is None or wo is None:
                            break
                        v = q * (m - wi - wo) * TICK - ((q * sigma * TICK) ** 2 / (2 * T) if T else 0.0)
                        if v > best:
                            best, bq = v, q
                    qs.append(bq)
                    ns.append(bq * px)
                    gs.append(best)
            g750 = 750 * m * TICK
            print(f"| {m:+.2f} | {sigma} | {'none' if T is None else int(T)} | {median(qs):,.0f} | {pct(qs, .25):,.0f}-{pct(qs, .75):,.0f} | "
                  f"{median(ns):,.0f} | {median(gs):.1f} | {g750:+.1f} |")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    if arg == "robust":
        robust()
    elif arg == "sizing":
        sizing()
    else:
        main(arg)
