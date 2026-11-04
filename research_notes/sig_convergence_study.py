"""Does SIG move toward the Polymarket/Kalshi fair?  How much, how fast, and is it tradeable?

Legs: data/research_contract_rules/tc_mapping.json, terms "same" and no *_late_settle code (194 legs). Each SIG leg is
compared with the SAME party's outside leg (raw PM and Kalshi mids of that party's own contract, never 1 - other party).
fair = mean(PM mid, Kalshi mid) when both are fresh and agree within AGREE, else the leg is skipped at that time.

Samples (split as in directional_impact.md / outside_stable_sweep.md):
  A    1-21 Oct 21:55 UTC: SIG 5-min candle close (a trade price) on a 5-min grid; PM prices-history (fidelity 5) and
       Kalshi 1-min candle bid/ask close mid, fetched per leg (public endpoints, cached in data/research_sig_convergence/raw).
       IS = A before 17 Oct 00:00 UTC, OOS1 = A from 17 Oct.
  B    21 Oct 23:49 - 23 Oct 22:46 UTC: live SIG bid/ask, PM and Kalshi same-party mids per bot poll (venue_quotes). OOS2.

Read-only: data/sigbot.sqlite opened mode=ro; no SIG venue calls.  Reproduce:
  python3 research_notes/sig_convergence_study.py fetch     # once (~5 min)
  python3 research_notes/sig_convergence_study.py           # analysis, tables, figure
One tick = 0.005.
"""
import bisect
import collections
import json
import math
import os
import pickle
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "research_sig_convergence")
RAW = os.path.join(OUT, "raw")
FIG = os.path.join(HERE, "research_notes", "figures")
T = 0.005
STEP = 300
utc = lambda *a: datetime(*a, tzinfo=timezone.utc).timestamp()  # noqa: E731
A_START, A_END = utc(2026, 10, 1), utc(2026, 10, 21, 22)
SPLIT = utc(2026, 10, 17)
AGREE = 0.04
PM_AGE, K_AGE, SIG_AGE = 1800, 6 * 3600, 3600
HZ = {"15m": 3, "1h": 12, "4h": 48, "24h": 288, "3d": 864}
BUCKETS = [(2, 4), (4, 8), (8, 16), (16, 999)]
CATS = ("house_seat", "senate_seat", "governor", "chamber_control")


def db():
    return sqlite3.connect(f"file:{os.path.join(DATA, 'sigbot.sqlite')}?mode=ro", uri=True)


def legs():
    """exchange id -> dict(race, party, cat, pm token, kalshi ticker)."""
    tc = json.load(open(os.path.join(DATA, "research_contract_rules", "tc_mapping.json")))
    ins = collections.defaultdict(dict)
    con = db()
    for race, party, venue, i in con.execute("SELECT DISTINCT race, party, venue, instrument FROM venue_quotes"):
        ins[(race, party)][venue] = i
    by_ex = {v["sig"]: dict(race=r, party=p, pm=v.get("polymarket"), kalshi=v.get("kalshi")) for (r, p), v in ins.items() if "sig" in v}
    out = {}
    for ex, v in tc.items():
        if ex.startswith("_") or v["terms"] != "same" or any(d.endswith("late_settle") for d in v["differences"]):
            continue
        b = by_ex.get(ex) or {}
        k = (v.get("kalshi_market") or {}).get("tickers") or []
        out[ex] = dict(race=b.get("race", v["race"]), party=v["party"], cat=v["category"], pm=b.get("pm"),
                       kalshi=b.get("kalshi") or (k[0] if len(k) == 1 else None))
    return out


# ------------------------------------------------------------------------------------------------------------- fetch
def get(url, tries=4):
    for k in range(tries):
        try:
            r = subprocess.run(["curl", "-s", "--max-time", "30", "-H", "Accept: application/json", url], capture_output=True, timeout=40)
            return json.loads(r.stdout.decode())
        except Exception as e:  # noqa: BLE001
            if k == tries - 1:
                return {"_error": str(e)}
            time.sleep(1.5 * (k + 1))


def fetch_one(item):
    ex, leg = item
    fn = os.path.join(RAW, f"{ex}.json")
    if os.path.exists(fn):
        return ex, "cached"
    rec = dict(ex=ex, leg=leg, pm=[], k=[])
    if leg["pm"]:
        t = A_START
        while t < A_END:
            u = min(A_END, t + 5 * 86400)
            h = get(f"https://clob.polymarket.com/prices-history?market={leg['pm']}&startTs={int(t)}&endTs={int(u)}&fidelity=5")
            rec["pm"] += [(x["t"], x["p"]) for x in (h.get("history") or [])] if isinstance(h, dict) else []
            t = u
    if leg["kalshi"]:
        series = leg["kalshi"].split("-")[0]
        t = A_START
        while t < A_END:
            u = min(A_END, t + 3 * 86400)
            c = get(f"https://api.elections.kalshi.com/trade-api/v2/series/{series}/markets/{leg['kalshi']}/candlesticks"
                    f"?start_ts={int(t)}&end_ts={int(u)}&period_interval=1")
            for x in (c.get("candlesticks") or []) if isinstance(c, dict) else []:
                b, a = x.get("yes_bid", {}).get("close_dollars"), x.get("yes_ask", {}).get("close_dollars")
                if b is not None and a is not None and float(a) >= float(b) and float(a) > 0:
                    rec["k"].append((x["end_period_ts"], 0.5 * (float(b) + float(a))))
            time.sleep(0.1)
            t = u
    json.dump(rec, open(fn, "w"))
    return ex, f"{leg['race']} {leg['party']} pm {len(rec['pm'])} k {len(rec['k'])}"


def fetch():
    from concurrent.futures import ThreadPoolExecutor
    os.makedirs(RAW, exist_ok=True)
    with ThreadPoolExecutor(4) as pool:
        for ex, msg in pool.map(fetch_one, sorted(legs().items())):
            print(ex, msg, flush=True)




# ------------------------------------------------------------------------------------------------------------- panel
import numpy as np  # noqa: E402


def ffgrid(ts, px, grid, age):
    """last value at or before each grid time, NaN when older than age."""
    ts = np.asarray(ts, float)
    px = np.asarray(px, float)
    o = np.argsort(ts, kind="stable")
    ts, px = ts[o], px[o]
    i = np.searchsorted(ts, grid, side="right") - 1
    v = np.where(i >= 0, px[np.clip(i, 0, None)], np.nan)
    a = np.where(i >= 0, grid - ts[np.clip(i, 0, None)], np.inf)
    return np.where(a <= age, v, np.nan)


def fair_of(pm, k, has_k):
    if not has_k:
        return pm
    f = 0.5 * (pm + k)
    return np.where(np.abs(pm - k) <= AGREE, f, np.nan)


def build():
    """leg -> dict(A=dict(t, mid, bid, ask, fair), B=...) on 5-min grids; half spreads from B."""
    L = legs()
    con = db()
    # B: live polls
    rows = collections.defaultdict(lambda: collections.defaultdict(list))
    key = {(v["race"], v["party"]): ex for ex, v in L.items()}
    for ts, venue, race, party, bid, ask, mid in con.execute(
            "SELECT ts, venue, race, party, bid, ask, mid FROM venue_quotes"):
        ex = key.get((race, party))
        if ex is None:
            continue
        if venue == "sig" and bid and ask and ask > bid:
            rows[ex]["sig"].append((ts, bid, ask))
        elif venue in ("polymarket", "kalshi") and mid is not None and bid and ask and ask >= bid:
            rows[ex][venue].append((ts, mid))
    b0 = min(r["sig"][0][0] for r in rows.values() if r["sig"])
    b1 = max(r["sig"][-1][0] for r in rows.values() if r["sig"])
    gB = np.arange(math.ceil(b0 / STEP) * STEP, b1, STEP, dtype=float)
    gA = np.arange(A_START, A_END, STEP, dtype=float)
    candles = json.load(open(os.path.join(DATA, "candles_5m.json")))
    P = {}
    for ex, leg in L.items():
        r = rows.get(ex, {})
        d = dict(leg=leg)
        hs = 0.5 * T
        if r.get("sig"):
            sp = sorted(a - b for _, b, a in r["sig"])
            hs = max(0.5 * T, 0.5 * sp[len(sp) // 2])
            s = sorted(r["sig"])
            bid = ffgrid([x[0] for x in s], [x[1] for x in s], gB, STEP)
            ask = ffgrid([x[0] for x in s], [x[2] for x in s], gB, STEP)
            pm = ffgrid(*zip(*r["polymarket"]), gB, STEP) if r.get("polymarket") else np.full(len(gB), np.nan)
            k = ffgrid(*zip(*r["kalshi"]), gB, STEP) if r.get("kalshi") else np.full(len(gB), np.nan)
            d["B"] = dict(t=gB, bid=bid, ask=ask, mid=0.5 * (bid + ask), fair=fair_of(pm, k, bool(r.get("kalshi"))), pm=pm)
        d["hs"] = hs
        cs = (candles.get(ex) or {}).get("candles") or []
        fn = os.path.join(RAW, f"{ex}.json")
        if cs and os.path.exists(fn):
            raw = json.load(open(fn))
            ct = [datetime.fromisoformat(c["time"].replace("Z", "+00:00")).timestamp() + STEP for c in cs]
            mid = ffgrid(ct, [c["close"] for c in cs], gA, SIG_AGE)
            pm = ffgrid(*zip(*raw["pm"]), gA, PM_AGE) if raw["pm"] else np.full(len(gA), np.nan)
            k = ffgrid(*zip(*raw["k"]), gA, K_AGE) if raw["k"] else np.full(len(gA), np.nan)
            d["A"] = dict(t=gA, mid=mid, bid=mid - hs, ask=mid + hs, fair=fair_of(pm, k, bool(raw["k"])), pm=pm)
        P[ex] = d
    return P


def band_of(f):
    return "favourite" if f > 0.7 else ("underdog" if f < 0.3 else "middle")


def bucket_of(a):
    for lo, hi in BUCKETS:
        if lo <= a < hi:
            return f"{lo}-{hi}" if hi < 999 else f"{lo}+"
    return None


# ----------------------------------------------------------------------------------------------- closure measurement
def level_for(s, g, mode):
    """expected gap: 0 (raw), the leg's trailing 24 h mean (dev), or a model prediction stored in the panel (pred_*)."""
    if mode == "raw":
        return np.zeros(len(g))
    if mode == "dev":
        return trailing_level(g)
    return s[mode]


HZF = {"5m": 1, "15m": 3, "30m": 6, "1h": 12, "2h": 24, "4h": 48, "8h": 96, "12h": 144, "24h": 288, "48h": 576, "3d": 864}
LEVEL_N = 288          # trailing level window: 24 h of grid points


def trailing_level(g, n=LEVEL_N, min_n=72):
    """mean of the PAST n grid gaps (excluding t), NaN with < min_n valid points."""
    v = np.where(np.isnan(g), 0.0, g)
    c = np.where(np.isnan(g), 0.0, 1.0)
    cv, cc = np.concatenate([[0], np.cumsum(v)]), np.concatenate([[0], np.cumsum(c)])
    i = np.arange(len(g))
    lo = np.clip(i - n, 0, None)
    s, k = cv[i] - cv[lo], cc[i] - cc[lo]
    return np.where(k >= min_n, s / np.maximum(k, 1), np.nan)


def events(P, sample, mode="raw"):
    """one row per (leg, grid t0) with |x0| >= 2 ticks; x = gap (raw) or gap minus its trailing 24 h level (dev).
    For each horizon: closed share parts (SIG move, outside move) relative to |x0|, in ticks."""
    out = []
    for ex, d in P.items():
        if sample not in d:
            continue
        s = d[sample]
        g = (s["mid"] - s["fair"]) / T
        lev = level_for(s, g, mode)
        x = g - lev
        n = len(g)
        for i in np.nonzero(np.abs(x) >= 2)[0]:
            sg = 1.0 if x[i] > 0 else -1.0
            r = dict(ex=ex, race=d["leg"]["race"], cat=d["leg"]["cat"], t0=s["t"][i], x0=abs(x[i]), band=band_of(s["fair"][i]),
                     day=int(s["t"][i] // 86400), sub=("IS" if s["t"][i] < SPLIT else "OOS1") if sample == "A" else "OOS2")
            for h, k in HZF.items():
                j = i + k
                if j < n and not np.isnan(g[j]):
                    sig_mv = -sg * (s["mid"][j] - s["mid"][i]) / T
                    out_mv = sg * (s["fair"][j] - s["fair"][i]) / T
                    r[h] = (sig_mv, out_mv)
            out.append(r)
    return out


def closure(rows, h):
    """share of the initial gap closed at h (ratio of sums), SIG part, outside part, n, clustered (race-day) SE of share."""
    rr = [r for r in rows if h in r]
    if len(rr) < 10:
        return None
    den = sum(r["x0"] for r in rr)
    sig = sum(r[h][0] for r in rr) / den
    out = sum(r[h][1] for r in rr) / den
    tot = sig + out
    cl = collections.defaultdict(float)
    for r in rr:
        cl[(r["race"], r["day"])] += (r[h][0] + r[h][1]) - tot * r["x0"]
    se = math.sqrt(sum(v * v for v in cl.values())) / den if len(cl) > 1 else float("nan")
    return dict(share=tot, sig=sig, out=out, n=len(rr), legs=len({r["ex"] for r in rr}), clusters=len(cl), se=se)


def half_life(rows):
    hs = list(HZF)
    prev = (0.0, 0.0)
    for h in hs:
        c = closure(rows, h)
        if c is None:
            return None
        m = HZF[h] * STEP / 3600
        if c["share"] >= 0.5:
            (m0, s0) = prev
            return m0 + (0.5 - s0) * (m - m0) / (c["share"] - s0) if c["share"] > s0 else m
        prev = (m, c["share"])
    return float("inf")


if __name__ == "__main__" and sys.argv[1:] == ["fetch"]:
    fetch()


# ---------------------------------------------------------------------------------------------- level persistence
def level_stats(P):
    """per-leg mean raw gap by period (IS, OOS1, B), cross-period correlation, variance share explained by leg means,
    mean gap by price band."""
    per = collections.defaultdict(dict)
    allg, legmean = [], []
    band = collections.defaultdict(list)
    for ex, d in P.items():
        for smp in ("A", "B"):
            if smp not in d:
                continue
            s = d[smp]
            g = (s["mid"] - s["fair"]) / T
            ok = ~np.isnan(g)
            for lab, m in ((("IS", ok & (s["t"] < SPLIT)), ("OOS1", ok & (s["t"] >= SPLIT))) if smp == "A" else (("B", ok),)):
                if m.sum() >= 72:
                    per[ex][lab] = float(g[m].mean())
            if smp == "A" and ok.sum() >= 72:
                allg.append(g[ok])
                legmean.append(np.full(ok.sum(), g[ok].mean()))
            for f, x in zip(s["fair"][ok], g[ok]):
                band[(smp, band_of(f))].append(x)
    G, M = np.concatenate(allg), np.concatenate(legmean)
    r2 = 1 - np.var(G - M) / np.var(G)
    corr = {}
    for a, b in (("IS", "OOS1"), ("IS", "B"), ("OOS1", "B")):
        xs = [(v[a], v[b]) for v in per.values() if a in v and b in v]
        if len(xs) > 5:
            x, y = np.array(xs).T
            corr[f"{a}-{b}"] = dict(r=float(np.corrcoef(x, y)[0, 1]), n=len(xs), slope=float(np.polyfit(x, y, 1)[0]))
    bands = {f"{k[0]} {k[1]}": dict(mean=float(np.mean(v)), n=len(v)) for k, v in sorted(band.items())}
    return dict(r2_leg_mean=float(r2), corr=corr, bands=bands)


# ------------------------------------------------------------------------------------------------- trading test
IMP = 0.5              # impact/slippage per side, ticks (<= 3k shares)
XS = (2, 3, 4, 6, 8, 12, 16)
HS = {"1h": 12, "4h": 48, "24h": 288}
YS = (0.5, None)       # exit once 50% closed, or hold to h
NOTIONAL, MAX_Q, PARTISAN_CAP = 600.0, 3000, 5000.0


def trades(P, sample, mode, X, H, Y):
    """non-overlapping trades per leg: enter at SIG's touch when |x| >= X ticks (x = gap or deviation from the trailing
    level), exit at the opposite touch after H grid steps or once Y of x is closed. Net of IMP per side; in sample A
    (trade-price mids) also a half spread per side for bid-ask bounce."""
    out = []
    for ex, d in P.items():
        if sample not in d:
            continue
        s = d[sample]
        g = (s["mid"] - s["fair"]) / T
        lev = level_for(s, g, mode)
        x = g - lev
        bounce = d["hs"] / T if sample == "A" else 0.0
        n = len(g)
        i = 0
        cand = np.nonzero(np.abs(x) >= X)[0]
        k = 0
        while k < len(cand):
            i = cand[k]
            if np.isnan(s["bid"][i]) or np.isnan(s["ask"][i]):
                k += 1
                continue
            sg = 1.0 if x[i] > 0 else -1.0
            side = -sg                                       # +1 buy YES (SIG cheap), -1 sell YES (SIG rich)
            entry = (s["ask"][i] if side > 0 else s["bid"][i]) / T + side * (IMP + bounce)
            L0 = lev[i]
            j_exit = None
            for j in range(i + 1, min(n, i + H + 13)):
                if np.isnan(g[j]) or np.isnan(s["bid"][j]):
                    continue
                if j >= i + H or (Y is not None and sg * (x[i] - (g[j] - L0)) >= Y * abs(x[i])):
                    j_exit = j
                    break
            if j_exit is None:
                k += 1
                continue
            j = j_exit
            ex_px = (s["bid"][j] if side > 0 else s["ask"][j]) / T - side * (IMP + bounce)
            px = s["ask"][i] if side > 0 else 1 - s["bid"][i]
            out.append(dict(ex=ex, race=d["leg"]["race"], party=d["leg"]["party"], cat=d["leg"]["cat"], band=band_of(s["fair"][i]),
                            t0=float(s["t"][i]), t1=float(s["t"][j]), day=int(s["t"][i] // 86400), side=side, cost=float(px),
                            sub=("IS" if s["t"][i] < SPLIT else "OOS1") if sample == "A" else "OOS2", pnl=float(side * (ex_px - entry)), x0=float(abs(x[i]))))
            k = int(np.searchsorted(cand, j, side="right"))
    return out


def stat(rows):
    """mean ticks/share, race-day clustered t, n, clusters."""
    if len(rows) < 5:
        return None
    m = sum(r["pnl"] for r in rows) / len(rows)
    cl = collections.defaultdict(float)
    for r in rows:
        cl[(r["race"], r["day"])] += r["pnl"] - m
    se = math.sqrt(sum(v * v for v in cl.values())) / len(rows) if len(cl) > 1 else float("nan")
    return dict(m=m, se=se, t=m / se if se and se > 0 else float("nan"), n=len(rows), cl=len(cl), races=len({r["race"] for r in rows}))


def capacity(rows, days):
    """shares/day entered under 600 notional per leg, MAX_Q, and a 5,000 net partisan notional cap (pro-D = long D or short R)."""
    ev = sorted([(r["t0"], 1, r) for r in rows] + [(r["t1"], 0, r) for r in rows], key=lambda e: (e[0], e[1]))
    net, shares, taken, pnl = 0.0, 0.0, 0, 0.0
    live = {}
    for t, kind, r in ev:
        key = id(r)
        if kind == 0:
            if key in live:
                net -= live.pop(key)
            continue
        q = min(MAX_Q, NOTIONAL / max(r["cost"], 0.01))
        dirn = (1 if r["party"] == "D" else -1 if r["party"] == "R" else 0) * r["side"]
        exp = dirn * q * r["cost"]
        if abs(net + exp) > PARTISAN_CAP:
            continue
        net += exp
        live[key] = exp
        shares += q
        pnl += q * r["pnl"] * T
        taken += 1
    return dict(shares_day=shares / days, trades_day=taken / days, cash_day=pnl / days)


# ---------------------------------------------------------------------------------------------------------- main
EDGES = (2, 3, 4, 6, 8, 12, 16, 24, 40)
CATS_ALL = ("all",) + CATS + ("favourite", "middle", "underdog")


def in_group(r, gname):
    if gname == "all":
        return True
    if gname in CATS:
        return r["cat"] == gname
    if gname in ("favourite", "middle", "underdog"):
        return r["band"] == gname
    c, b = gname.split("|")
    return r["cat"] == c and r["band"] == b


def analyse():
    pk = os.path.join(OUT, "panel.pkl")
    if os.path.exists(pk):
        P = pickle.load(open(pk, "rb"))
    else:
        P = build()
        pickle.dump(P, open(pk, "wb"))
    res = dict(level=level_stats(P))
    # 1-2: closure, raw and deviation, A+B pooled and per sample
    curves, tables = {}, {}
    for mode in ("raw", "dev"):
        E = {smp: events(P, smp, mode) for smp in "AB"}
        both = E["A"] + E["B"]
        for lab, rows in (("A", E["A"]), ("B", E["B"]), ("AB", both)):
            for lo, hi in BUCKETS:
                rr = [r for r in rows if lo <= r["x0"] < hi]
                tables[f"{mode} {lab} {bucket_of(lo)}"] = dict({h: closure(rr, h) for h in HZ}, hl=half_life(rr), n=len(rr))
        cv = []
        for lo, hi in zip(EDGES, EDGES[1:]):
            rr = [r for r in both if lo <= r["x0"] < hi]
            if len(rr) >= 30:
                cv.append(dict(x=float(np.median([r["x0"] for r in rr])), lo=lo, hi=hi, n=len(rr), **{h: closure(rr, h) for h in ("1h", "4h", "24h")}))
        curves[mode] = cv
        cb = {}
        for c in CATS:
            for b in ("favourite", "middle", "underdog"):
                for lo in (2, 4):
                    rr = [r for r in both if r["cat"] == c and r["band"] == b and r["x0"] >= lo]
                    if len(rr) >= 30:
                        cb[f"{c}|{b}|{lo}+"] = dict({h: closure(rr, h) for h in ("1h", "4h", "24h", "3d")}, hl=half_life(rr), n=len(rr),
                                                    legs=len({r["ex"] for r in rr}))
        tables[f"catband {mode}"] = cb
    res["closure"], res["curves"] = tables, curves
    # 3: trading grid
    grid = {}
    for mode in ("raw", "dev"):
        for X in XS:
            for hn, H in HS.items():
                for Y in YS:
                    rows = trades(P, "A", mode, X, H, Y) + trades(P, "B", mode, X, H, Y)
                    grid[(mode, X, hn, Y)] = rows
        print("grid", mode, flush=True)
    t_is = (SPLIT - A_START) / 86400
    t_test = (A_END - SPLIT) / 86400 + (max(d["B"]["t"][-1] for d in P.values()) - min(d["B"]["t"][0] for d in P.values())) / 86400
    groups = list(CATS_ALL) + [f"{c}|{b}" for c in CATS for b in ("favourite", "middle", "underdog")]
    sel = {}
    for gname in groups:
        for mode in ("raw", "dev"):
            best = None
            for (m, X, hn, Y), rows in grid.items():
                if m != mode:
                    continue
                tr = stat([r for r in rows if r["sub"] == "IS" and in_group(r, gname)])
                if tr and tr["n"] >= 15 and tr["cl"] >= 5 and tr["m"] > 0 and (best is None or tr["t"] > best[0]["t"]):
                    best = (tr, X, hn, Y)
            if best is None:
                sel[f"{gname} {mode}"] = None
                continue
            tr, X, hn, Y = best
            rows = [r for r in grid[(mode, X, hn, Y)] if in_group(r, gname)]
            te = [r for r in rows if r["sub"] != "IS"]
            # smallest X positive in training (t > 2) at the chosen h, Y
            xmin = None
            for X2 in XS:
                s2 = stat([r for r in grid[(mode, X2, hn, Y)] if r["sub"] == "IS" and in_group(r, gname)])
                if s2 and s2["m"] > 0 and s2["t"] > 2:
                    xmin = X2
                    break
            sel[f"{gname} {mode}"] = dict(X=X, h=hn, Y=Y, train=tr, oos1=stat([r for r in te if r["sub"] == "OOS1"]),
                                          oos2=stat([r for r in te if r["sub"] == "OOS2"]), test=stat(te), xmin_train=xmin,
                                          cap=capacity(te, t_test) if te else None)
    res["select"] = sel
    res["n_configs"] = len(grid) * len(groups)
    # figure curves: per mode, (h, Y) best for "all" in training; net vs X, training and test
    figc = {}
    for mode in ("raw", "dev"):
        s = sel.get(f"all {mode}")
        hn, Y = (s["h"], s["Y"]) if s else ("24h", None)
        pts = []
        for X in XS:
            rows = grid[(mode, X, hn, Y)]
            pts.append(dict(X=X, train=stat([r for r in rows if r["sub"] == "IS"]), test=stat([r for r in rows if r["sub"] != "IS"])))
        figc[mode] = dict(h=hn, Y=Y, pts=pts)
    res["fig_trade"] = figc
    res["days"] = dict(train=t_is, test=t_test)
    json.dump(res, open(os.path.join(OUT, "results.json"), "w"), indent=1, default=str)
    return res


if __name__ == "__main__" and sys.argv[1:] == ["analyse"]:
    analyse()


# -------------------------------------------------------------------------------------------------------- figure
def plot(res=None, modes=("raw", "dev"), names=None, fname="sig_convergence.png", sub=None, gloss=None, title=None):
    import textwrap
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker
    from matplotlib.lines import Line2D
    from matplotlib.patches import FancyBboxPatch
    res = res or json.load(open(os.path.join(OUT, "results.json")))
    plt.style.use("default")
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10, "xtick.labelsize": 9.5,
                         "ytick.labelsize": 9.5, "legend.fontsize": 9.5})
    fig = plt.figure(figsize=(10, 19), facecolor="white", layout="constrained")
    gs = fig.add_gridspec(5, 1, height_ratios=[0.16, 1, 1, 1.1, 0.62 if gloss else 0.4])
    hax = fig.add_subplot(gs[0])
    axes = [fig.add_subplot(gs[1])]
    axes += [fig.add_subplot(gs[i], sharex=axes[0]) for i in (2, 3)]
    hc = {"1h": "#9ecae1", "4h": "#3182bd", "24h": "#08306b"}
    mls = dict(zip(modes, ("-", "--")))
    mname = names or {"raw": "whole gap", "dev": "gap beyond the leg's usual level"}
    # A
    for mode in modes:
        cv = res["curves"][mode]
        for h in ("1h", "4h", "24h"):
            pts = [(c["x"], c[h]) for c in cv if c.get(h)]
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1]["share"] for p in pts]
            axes[0].plot(xs, ys, mls[mode], color=hc[h], marker="o", ms=4)
            if h == "24h":
                axes[0].fill_between(xs, [p[1]["share"] - 1.96 * p[1]["se"] for p in pts], [p[1]["share"] + 1.96 * p[1]["se"] for p in pts],
                                     color=hc[h], alpha=0.10)
    axes[0].axhline(0, color="grey", lw=0.8)
    axes[0].set_title(f"A. How much of the {'leftover ' if gloss else ''}gap has closed after 1 h, 4 h and 24 h?", loc="left", fontweight="bold")
    axes[0].set_ylabel("Share of the starting gap closed")
    axes[0].legend(handles=[Line2D([], [], color=hc[h], lw=2, label=f"after {h}") for h in hc]
                   + [Line2D([], [], color="0.3", ls=mls[m], label=f"{'solid' if mls[m] == '-' else 'dashed'}: {mname[m]}") for m in mls],
                   loc="upper left", bbox_to_anchor=(0, -0.05), ncol=2, frameon=False)
    # B
    bc = {"sig": "#d62728", "out": "#2ca02c"}
    for mode in modes:
        cv = res["curves"][mode]
        for part in ("sig", "out"):
            pts = [(c["x"], c["24h"]) for c in cv if c.get("24h")]
            axes[1].plot([p[0] for p in pts], [p[1][part] for p in pts], mls[mode], color=bc[part], marker="o", ms=4)
    axes[1].axhline(0, color="grey", lw=0.8)
    axes[1].set_title("B. Who closes it after 24 h: SIG moving, or Polymarket/Kalshi moving?", loc="left", fontweight="bold")
    axes[1].set_ylabel("Share of the starting gap\nclosed by each side (24 h)")
    axes[1].legend(handles=[Line2D([], [], color=bc["sig"], lw=2, label="SIG's price moving toward the outside price"),
                            Line2D([], [], color=bc["out"], lw=2, label="Polymarket/Kalshi moving toward SIG")]
                   + [Line2D([], [], color="0.3", ls=mls[m], label=f"{'solid' if mls[m] == '-' else 'dashed'}: {mname[m]}") for m in mls],
                   loc="upper left", bbox_to_anchor=(0, -0.05), ncol=2, frameon=False)
    # C
    tc = dict(zip(modes, ("#7f7f7f", "#ff7f0e")))
    for mode in modes:
        f = res["fig_trade"][mode]
        for scope, ls, mk in (("train", ":", "^"), ("test", "-", "o")):
            pts = [(p["X"], p[scope]) for p in f["pts"] if p[scope]]
            xs = [p[0] for p in pts]
            axes[2].plot(xs, [p[1]["m"] for p in pts], ls, color=tc[mode], marker=mk, ms=4)
            if scope == "test":
                axes[2].fill_between(xs, [p[1]["m"] - 1.96 * p[1]["se"] for p in pts], [p[1]["m"] + 1.96 * p[1]["se"] for p in pts],
                                     color=tc[mode], alpha=0.12)
    axes[2].axhline(0, color="grey", lw=0.8)
    fr, fd = res["fig_trade"][modes[0]], res["fig_trade"][modes[1]]
    yname = lambda f: f"held {f['h']}" if f["Y"] in (None, "None") else f"sold at half closed or {f['h']}"  # noqa: E731
    axes[2].set_title("C. Profit per share after costs, trading SIG when the gap is at least x", loc="left", fontweight="bold")
    axes[2].set_ylabel("Profit per share after costs\n(ticks)")
    axes[2].legend(handles=[Line2D([], [], color=tc[modes[0]], lw=2, label=f"trade the {mname[modes[0]]} ({yname(fr)})"),
                            Line2D([], [], color=tc[modes[1]], lw=2, label=f"trade the {mname[modes[1]]} ({yname(fd)})"),
                            Line2D([], [], color="0.3", ls=":", marker="^", ms=4, label="dotted: training days (1-16 Oct), average"),
                            Line2D([], [], color="0.3", ls="-", marker="o", ms=4, label="solid: test days (17-23 Oct), average, band = 95% range")],
                   loc="upper left", bbox_to_anchor=(0, -0.12), ncol=1, frameon=False)
    axes[2].set_xscale("log")
    axes[2].set_xticks([2, 3, 4, 6, 8, 12, 16, 24, 32])
    axes[2].set_xticklabels(["2", "3", "4", "6", "8", "12", "16", "24", "32"])
    axes[2].xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    axes[2].set_xlim(1.8, 34)
    axes[2].set_xlabel("Starting gap between SIG and the outside price, in ticks (log scale; C: smallest gap we trade, x)")
    for a in axes:
        a.grid(True, alpha=0.3)
        a.tick_params(labelbottom=True)
    hax.axis("off")
    hax.text(0.5, 1.0, title or "Does SIG move toward the Polymarket/Kalshi price?", ha="center", va="top", fontsize=15, fontweight="bold",
             transform=hax.transAxes)
    hax.annotate(sub or "194 legs with the same contract terms, each against the same party's outside leg, 1-23 Oct, 5-min grid",
                 xy=(0.5, 1.0), xycoords="axes fraction", xytext=(0, -24), textcoords="offset points", ha="center", va="top",
                 fontsize=10.5, color="0.25")
    gax = fig.add_subplot(gs[4])
    gax.axis("off")
    gax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.01", transform=gax.transAxes,
                                 facecolor="#f6f6f2", edgecolor="0.6", lw=0.8, clip_on=False))
    gax.text(0.01, 0.97, "Glossary", ha="left", va="top", fontsize=10, fontweight="bold", transform=gax.transAxes)
    gl = [("outside price", "average of the Polymarket and Kalshi prices for the same party, used only when they agree within 4 points"),
          ("gap", "SIG's mid price minus the outside price"),
          ("tick", "0.005, the smallest SIG price step (half a cent)"),
          ("leg's usual level", "the leg's average gap over the previous 24 h; most gaps are this steady offset (favourites cheap, underdogs rich on SIG)"),
          ("share closed", "how much of the starting gap is gone after the wait; 1 = SIG and outside agree, below 0 = the gap grew"),
          ("costs", "SIG's bid-ask spread plus 0.5 tick each way; before 22 Oct SIG prices are 5-min trades, so another half spread each way"),
          ("training / test days", "the rule was chosen on 1-16 Oct and checked on 17-23 Oct (real SIG quotes from 22 Oct)"),
          ("bands", "95% uncertainty range, grouped by race and day")]
    gl = gloss or gl
    wrapped = ["\n".join(textwrap.wrap(f"{t}: {e}", width=66, subsequent_indent="    ")) for t, e in gl]
    nl = [w.count("\n") + 1 for w in wrapped]
    per = min(range(1, len(wrapped)), key=lambda k: abs(sum(nl[:k]) - sum(nl[k:])))
    for c in range(2):
        gax.text(0.01 + 0.5 * c, 0.88, "\n".join(wrapped[:per] if c == 0 else wrapped[per:]), ha="left", va="top", fontsize=9.2,
                 transform=gax.transAxes, linespacing=1.35)
    fig.savefig(os.path.join(FIG, fname), dpi=130)


if __name__ == "__main__" and sys.argv[1:] == ["plot"]:
    plot()


# ================================================================================ follow-up: cross-sectional gap model
STATES = {"Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
          "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
          "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
          "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
          "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
          "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
          "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
          "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY"}
KNOTS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
MODES_M = ("pred_M2", "pred_M3")


def state_of(race):
    if race[:2].isalpha() and race[2:3] == "-":
        return race[:2]
    for name in sorted(STATES, key=len, reverse=True):
        if race.startswith(name):
            return STATES[name]
    return None


def leg_static(P):
    """per leg: median SIG spread (ticks), median touch depth (shares, bid+ask) from live polls; poll-close hour (UTC, 3/4 Nov)."""
    con = db()
    rows = collections.defaultdict(list)
    for ex, bid, ask, bs, az in con.execute("SELECT instrument, bid, ask, bid_sz, ask_sz FROM venue_quotes WHERE venue='sig'"):
        if bid and ask and ask > bid and bs is not None and az is not None:
            rows[ex].append(((ask - bid) / T, bs + az))
    pc = json.load(open(os.path.join(DATA, "election_night", "poll_close_2026.json")))
    out = {}
    for ex, d in P.items():
        r = rows.get(ex) or [(2.0, 1000.0)]
        sp = sorted(x[0] for x in r)[len(r) // 2]
        dp = sorted(x[1] for x in r)[len(r) // 2]
        st = state_of(d["leg"]["race"])
        hh = pc.get(st, {}).get("first_close_et", "20:00") if st else "20:00"
        h, m = (int(x) for x in hh.split(":"))
        close = utc(2026, 11, 4) + (h + 5 - 24) * 3600 + m * 60      # 3 Nov is EST (UTC-5)
        out[ex] = dict(spread=sp, depth=dp, close=close, state=st)
    return out


def volume_arrays(P):
    """per leg and sample: shares traded on SIG in the trailing 24 h (A: 5-min candles, B: the bot's tape)."""
    candles = json.load(open(os.path.join(DATA, "candles_5m.json")))
    con = db()
    tape = collections.defaultdict(list)
    for ex, q, t in con.execute("SELECT exchange_id, quantity, executed_at FROM tape"):
        tape[ex].append((datetime.fromisoformat(t[:19] + "+00:00").timestamp(), q))
    vol = {}
    for ex, d in P.items():
        for smp, src in (("A", [(datetime.fromisoformat(c["time"][:19] + "+00:00").timestamp() + STEP, c["volume"])
                                for c in (candles.get(ex) or {}).get("candles") or []]), ("B", tape.get(ex, []))):
            if smp not in d:
                continue
            src = sorted(src)
            ts = np.array([x[0] for x in src] or [0.0])
            cq = np.concatenate([[0.0], np.cumsum([x[1] for x in src] or [0.0])])
            g = d[smp]["t"]
            vol[(ex, smp)] = cq[np.searchsorted(ts, g, side="right")] - cq[np.searchsorted(ts, g - 86400, side="right")]
    return vol


def features(P, static, vol, ex, smp, own=False):
    """design matrix (n x k) for one leg and sample, and the column names."""
    d = P[ex]
    s = d[smp]
    f = s["fair"]
    pm = s["pm"]
    k = np.where(np.isnan(pm), np.nan, 2 * f - pm)       # Kalshi mid implied by fair = (pm + k)/2 (= pm for PM-only legs)
    st = static[ex]
    cols, names = [np.ones(len(f))], ["const"]
    cols.append(f)
    names.append("fair")
    for kn in KNOTS:
        cols.append(np.maximum(f - kn, 0))
        names.append(f"fair>{kn}")
    for c in CATS[1:]:
        z = 1.0 if d["leg"]["cat"] == c else 0.0
        cols += [np.full(len(f), z), z * f]
        names += [c, f"{c}*fair"]
    cols.append(np.full(len(f), 1.0 if d["leg"]["party"] == "R" else 0.0))
    names.append("party R")
    cols += [np.full(len(f), math.log(st["spread"])), np.full(len(f), math.log1p(st["depth"])), np.log1p(vol[(ex, smp)])]
    names += ["log spread", "log depth", "log 24h volume"]
    cols += [np.abs(pm - k) / T, (pm - k) / T]
    names += ["|PM-Kalshi|", "PM-Kalshi"]
    cols += [np.log(np.maximum(st["close"] - s["t"], 3600) / 86400), np.full(len(f), (st["close"] % 86400) / 3600)]
    names += ["log days to poll close", "poll close hour"]
    if own:
        cols.append(np.nan_to_num(trailing_level((s["mid"] - f) / T), nan=0.0))
        cols.append(np.isnan(trailing_level((s["mid"] - f) / T)).astype(float))
        names += ["own trailing gap", "own gap missing"]
    return np.column_stack(cols), names


def fit_models(P):
    """OLS of the gap (ticks) on characteristics (M2) and + own trailing gap (M3), fitted on A before SPLIT. Stores
    predictions s['pred_M2'], s['pred_M3'] for every leg/sample; returns fit stats and out-of-sample R2 by model."""
    static = leg_static(P)
    vol = volume_arrays(P)
    fits = {}
    for name, own in (("M2", False), ("M3", True)):
        Xs, ys = [], []
        for ex, d in P.items():
            if "A" not in d:
                continue
            Xm, names = features(P, static, vol, ex, "A", own)
            g = (d["A"]["mid"] - d["A"]["fair"]) / T
            m = ~np.isnan(g) & ~np.isnan(Xm).any(1) & (d["A"]["t"] < SPLIT)
            Xs.append(Xm[m])
            ys.append(g[m])
        X, y = np.vstack(Xs), np.concatenate(ys)
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        fits[name] = dict(beta=dict(zip(names, beta.round(3).tolist())), n_train=len(y))
        for ex, d in P.items():
            for smp in ("A", "B"):
                if smp in d:
                    Xm, _ = features(P, static, vol, ex, smp, own)
                    d[smp][f"pred_{name}"] = Xm @ beta
    # out-of-sample comparison on a common set of points (trailing level defined), by period
    comp = {}
    for per in ("IS", "OOS1", "OOS2"):
        e = collections.defaultdict(list)
        for ex, d in P.items():
            smp = "B" if per == "OOS2" else "A"
            if smp not in d:
                continue
            s = d[smp]
            g = (s["mid"] - s["fair"]) / T
            L = trailing_level(g)
            m = ~np.isnan(g) & ~np.isnan(L) & ~np.isnan(s["pred_M2"]) & ~np.isnan(s["pred_M3"])
            m &= (s["t"] < SPLIT) if per == "IS" else (s["t"] >= SPLIT) if per == "OOS1" else True
            e["y"].append(g[m])
            e["M0 zero"].append(np.zeros(m.sum()))
            e["M1 own trailing 24h gap"].append(L[m])
            e["M2 characteristics"].append(s["pred_M2"][m])
            e["M3 characteristics + own gap"].append(s["pred_M3"][m])
            e["cl"].append(np.array([hash((d["leg"]["race"], int(t // 86400))) for t in s["t"][m]]))
        y = np.concatenate(e["y"])
        sst = float(((y - y.mean()) ** 2).sum())
        comp[per] = {k: dict(r2=1 - float(((y - np.concatenate(v)) ** 2).sum()) / sst, rmse=float(np.sqrt(((y - np.concatenate(v)) ** 2).mean())))
                     for k, v in e.items() if k not in ("y", "cl")}
        comp[per]["n"] = len(y)
    return fits, comp, static


def analyse_model():
    P = pickle.load(open(os.path.join(OUT, "panel.pkl"), "rb"))
    fits, comp, _ = fit_models(P)
    res = dict(fits=fits, r2=comp)
    curves, tables = {}, {}
    for mode in MODES_M:
        both = events(P, "A", mode) + events(P, "B", mode)
        for lo, hi in BUCKETS:
            rr = [r for r in both if lo <= r["x0"] < hi]
            tables[f"{mode} {bucket_of(lo)}"] = dict({h: closure(rr, h) for h in HZ}, hl=half_life(rr), n=len(rr))
            rb = [r for r in rr if r["sub"] == "OOS2"]
            tables[f"{mode} B {bucket_of(lo)}"] = dict({h: closure(rb, h) for h in ("15m", "1h", "4h", "24h")}, n=len(rb))
        curves[mode] = [dict(x=float(np.median([r["x0"] for r in rr])), n=len(rr), **{h: closure(rr, h) for h in ("1h", "4h", "24h")})
                        for lo, hi in zip(EDGES, EDGES[1:]) for rr in [[r for r in both if lo <= r["x0"] < hi]] if len(rr) >= 30]
    res["closure"], res["curves"] = tables, curves
    grid = {}
    for mode in MODES_M:
        for X in XS:
            for hn, H in HS.items():
                for Y in YS:
                    grid[(mode, X, hn, Y)] = trades(P, "A", mode, X, H, Y) + trades(P, "B", mode, X, H, Y)
    t_test = (A_END - SPLIT) / 86400 + (max(d["B"]["t"][-1] for d in P.values()) - min(d["B"]["t"][0] for d in P.values())) / 86400
    groups = list(CATS_ALL)
    sel = {}
    for gname in groups:
        for mode in MODES_M:
            best = None
            for (m, X, hn, Y), rows in grid.items():
                if m != mode:
                    continue
                tr = stat([r for r in rows if r["sub"] == "IS" and in_group(r, gname)])
                if tr and tr["n"] >= 15 and tr["cl"] >= 5 and tr["m"] > 0 and (best is None or tr["t"] > best[0]["t"]):
                    best = (tr, X, hn, Y)
            if best is None:
                sel[f"{gname} {mode}"] = None
                continue
            tr, X, hn, Y = best
            te = [r for r in grid[(mode, X, hn, Y)] if in_group(r, gname) and r["sub"] != "IS"]
            sel[f"{gname} {mode}"] = dict(X=X, h=hn, Y=Y, train=tr, oos1=stat([r for r in te if r["sub"] == "OOS1"]),
                                          oos2=stat([r for r in te if r["sub"] == "OOS2"]), test=stat(te),
                                          cap=capacity(te, t_test) if te else None)
    res["select"], res["n_configs"] = sel, len(grid) * len(groups)
    figc = {}
    for mode in MODES_M:
        s = sel.get(f"all {mode}")
        hn, Y = (s["h"], s["Y"]) if s else ("24h", None)
        figc[mode] = dict(h=hn, Y=Y, pts=[dict(X=X, train=stat([r for r in grid[(mode, X, hn, Y)] if r["sub"] == "IS"]),
                                               test=stat([r for r in grid[(mode, X, hn, Y)] if r["sub"] != "IS"])) for X in XS])
    res["fig_trade"] = figc
    json.dump(res, open(os.path.join(OUT, "results_model.json"), "w"), indent=1, default=str)
    return res


if __name__ == "__main__" and sys.argv[1:] == ["model"]:
    analyse_model()


def plot_model():
    res = json.load(open(os.path.join(OUT, "results_model.json")))
    r2 = res["r2"]
    f = lambda per, k: r2[per][k]["r2"]  # noqa: E731
    gloss = [("predicted gap", "the gap SIG usually has for a leg of this kind: a curve in the outside price (favourites cheap, underdogs rich), "
                               "category, party, spread, depth, recent SIG volume, Polymarket-Kalshi disagreement, time to poll close"),
             ("leftover gap", "actual gap minus predicted gap; we trade when it is at least x ticks"),
             ("how well it predicts", f"share of the gap's ups and downs explained on test days 17-21 Oct / 22-23 Oct: leg traits "
                                      f"{f('OOS1', 'M2 characteristics'):.2f} / {f('OOS2', 'M2 characteristics'):.2f}; the leg's own "
                                      f"last-24 h gap {f('OOS1', 'M1 own trailing 24h gap'):.2f} / {f('OOS2', 'M1 own trailing 24h gap'):.2f}"),
             ("outside price", "average of the Polymarket and Kalshi prices for the same party, used only when they agree within 4 points"),
             ("tick", "0.005, the smallest SIG price step (half a cent)"),
             ("share closed", "how much of the starting leftover gap is gone after the wait (prediction frozen at the start)"),
             ("costs", "SIG's bid-ask spread plus 0.5 tick each way; before 22 Oct SIG prices are 5-min trades, so another half spread each way"),
             ("training / test days", "the model and rule were fitted on 1-16 Oct and checked on 17-23 Oct (real SIG quotes from 22 Oct)"),
             ("bands", "95% uncertainty range, grouped by race and day")]
    plot(res, modes=MODES_M, names={"pred_M2": "gap beyond the leg-type prediction", "pred_M3": "gap beyond the prediction plus the leg's own recent gap"},
         fname="sig_convergence_model.png", title="Trading SIG legs whose gap is unusual for their kind",
         sub="Predicted SIG-minus-outside gap from leg traits; 194 legs with the same contract terms, 1-23 Oct, 5-min grid", gloss=gloss)


if __name__ == "__main__" and sys.argv[1:] == ["plot_model"]:
    plot_model()
