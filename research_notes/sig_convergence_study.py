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
        lev = trailing_level(g) if mode == "dev" else np.zeros(len(g))
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
