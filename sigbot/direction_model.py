"""Senate-shift model: how much does each race's market move when the Senate-control market moves?

    python3 -m sigbot.direction_model --fetch          # download 14 days of Polymarket/Kalshi history (public, ~1-2 min, no SIG reads)
    python3 -m sigbot.direction_model                  # fit on the cached history + SIG 5-minute candles, print the report
    python3 -m sigbot.direction_model --source live    # fit on what the direction loop recorded itself (venue_fair, exact poll times)

Series
------
For every race and venue (sig, pm, kalshi) the Democratic-party probability on a common 5-minute UTC grid, each value
the last observation at or before the bucket END (SIG candle close, Polymarket prices-history point, Kalshi 1-minute
candle bid/ask mid), so the three venues are compared at the same instant. The "senate series" is the Senate-control
market on each venue: SIG "U.S. Senate" D leg, Polymarket "Which party will win the Senate", Kalshi DD + RD from the
balance-of-power combo.

Model (one per target venue and horizon h in {1, 6} bars = 5 and 30 minutes)
----------------------------------------------------------------------------
    y        = race move on the target venue over the next h bars (points)
    own      : same-venue Senate move over the last bar (S_own), its "big" interaction S_own * big,
               the race's own last-bar move (mean reversion / bid-ask bounce)
    cross    : other venues' Senate moves over the last bar, other venues' race moves over the last bar,
               the gap (outside race fair - target race price)
    features are multiplied by the race's Senate beta where that makes sense (S terms), so one pooled model
    covers safe races (beta ~ 0) and swing races (beta large).
Ridge with per-feature penalties: own features lambda, cross-market features lambda * cross_penalty (default 10), i.e.
cross-market information is used but shrunk harder, as requested. Train on the first 70% of time, test on the last
30% (out of sample); report R^2, sign hit rate on |y| > 0, and the same fit with cross_penalty 1 and 1000 for contrast.

"Big" Senate shift
------------------
z = |S move over 5 min| / robust scale, scale = max(1.4826 * MAD of the trailing 24 h of non-zero 5-minute moves,
0.25 points). Big = z >= 3 AND |move| >= 1 point. The event study prints, for big Senate moves on each venue, the
average same-bar and following-bar race moves scaled by beta (how much of the Senate move each venue passes through
and how fast). On election night the S series will be replaced by the results-derived expected Senate control.

Per-race Senate betas (contemporaneous co-movement, 30-minute changes) are shrunk toward the office mean
(empirical Bayes) and saved with the pooled coefficients to params/direction_model.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from statistics import median
from typing import Optional

from .venues.http import Http, VenueError
from .venues.kalshi import Kalshi
from .venues.mapping import load as load_map
from .venues.polymarket import Polymarket

log = logging.getLogger("sigbot.direction_model")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_PATH = os.path.join(HERE, "data", "model_history.json")
OUT_PATH = os.path.join(HERE, "params", "direction_model.json")
BAR = 300
CHAMBER = "U.S. Senate"
VENUES = ("sig", "pm", "kalshi")


# ============================================================ history download
def fetch(days: float = 14.0, end_ts: Optional[float] = None, only_missing: bool = False) -> dict:
    """Polymarket (5-minute) and Kalshi (1-minute) history of every mapped race's D party. Public endpoints only.
    only_missing: keep the cached file and refetch just the instruments that came back empty (rate-limit gaps)."""
    m = load_map()
    http = Http()
    pm, ks = Polymarket(http), Kalshi(http, os.path.join(HERE, "data"))
    prev = json.load(open(HIST_PATH)) if only_missing and os.path.exists(HIST_PATH) else None
    end = int(prev["end"] if prev else (end_ts or time.time()))
    start = int(prev["start"] if prev else end - days * 86400)
    jobs_pm, jobs_k = [], []
    for race, e in m["races"].items():
        for l in e.get("polymarket", {}).get("legs", {}).get("D", []):
            if prev is None or not prev["pm"].get(race, {}).get(l["token"]):
                jobs_pm.append((race, l["token"]))
        for l in e.get("kalshi", {}).get("legs", {}).get("D", []):
            if prev is None or not prev["kalshi"].get(race, {}).get(l["ticker"]):
                jobs_k.append((race, e["kalshi"]["series"], l["ticker"]))

    def get_pm(job):
        race, tok = job
        pts = []
        t = start
        while t < end:                                   # fidelity=5 serves at most ~7 days per call
            e2 = min(end, t + 6 * 86400)
            try:
                pts += pm.history(tok, t, e2, fidelity=5)
            except VenueError as ex:
                log.warning("pm history %s %s: %s", race, tok[:10], ex)
            t = e2
        return race, tok, sorted(set(pts))

    def get_k(job):
        race, series, tk = job
        try:
            c = ks.candles(series, tk, start, end, 1)
        except VenueError as ex:
            log.warning("kalshi candles %s %s: %s", race, tk, ex)
            c = []
        return race, tk, [(x["ts"], x["mid"]) for x in c if x["mid"] is not None]

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        pm_res = list(ex.map(get_pm, jobs_pm))
    with ThreadPoolExecutor(max_workers=2) as ex:             # Kalshi rate-limits history calls
        k_res = list(ex.map(get_k, jobs_k))
    out = prev or {"start": start, "end": end, "fetched_at": time.time(), "pm": {}, "kalshi": {}}
    for race, tok, pts in pm_res:
        out["pm"].setdefault(race, {})[tok] = pts
    for race, tk, pts in k_res:
        out["kalshi"].setdefault(race, {})[tk] = pts
    os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
    json.dump(out, open(HIST_PATH, "w"))
    log.info("history: %d PM tokens, %d Kalshi tickers in %.0fs", len(pm_res), len(k_res), time.time() - t0)
    return out


# ============================================================ series on a grid
def _ffill(points: list, grid: list, max_age: float) -> list:
    """points [(ts, v)] sorted -> value at each grid time (last at or before), None if older than max_age."""
    ts = [p[0] for p in points]
    out = []
    for g in grid:
        i = bisect_right(ts, g) - 1
        out.append(points[i][1] if i >= 0 and g - ts[i] <= max_age else None)
    return out


def _sum_series(series: list[list]) -> list:
    out = []
    for vals in zip(*series):
        out.append(None if any(v is None for v in vals) else sum(vals))
    return out


def build_grid_offline(hist: dict) -> tuple[list, dict]:
    """{race: {venue: [D prob per bar]}} from cached outside history + SIG 5-minute candles."""
    from .universe import Universe
    rows = json.load(open(os.path.join(HERE, "data", "markets.json")))
    u = Universe.from_markets(rows)
    candles = json.load(open(os.path.join(HERE, "data", "candles_5m.json")))
    sig_end = max(datetime.fromisoformat(c["candles"][-1]["time"].replace("Z", "+00:00")).timestamp() for c in candles.values() if c.get("candles")) + BAR
    end = min(hist["end"], sig_end)
    start = hist["start"]
    grid = list(range(int(start // BAR * BAR + BAR), int(end), BAR))
    data: dict = {}
    for race, r in u.races.items():
        if "D" not in r.legs:
            continue
        d = {}
        c = candles.get(r.legs["D"].exchange_id, {}).get("candles", [])
        # SIG candle 'time' is the bucket start; the close is the price at the bucket end. Trades are sparse: carry
        # the last close forward for up to 24 h (the book does not move without trades on this venue's thin legs).
        pts = sorted((datetime.fromisoformat(x["time"].replace("Z", "+00:00")).timestamp() + BAR, x["close"]) for x in c if x.get("close") is not None)
        if pts:
            d["sig"] = _ffill(pts, grid, 86400)
        pm = hist["pm"].get(race, {})
        if pm:
            d["pm"] = _sum_series([_ffill(v, grid, 3600) for v in pm.values() if v])  if any(pm.values()) else None
        ks = hist["kalshi"].get(race, {})
        if ks:
            d["kalshi"] = _sum_series([_ffill(v, grid, 3600) for v in ks.values() if v]) if any(ks.values()) else None
        data[race] = {k: v for k, v in d.items() if v}
    return grid, data


def build_grid_live(store) -> tuple[list, dict]:
    """Same structure from the direction loop's own venue_fair rows (identical timestamps across venues)."""
    rows = store.rows("SELECT ts, race, pm, kalshi, sig_mid FROM venue_fair WHERE party='D' ORDER BY ts")
    if not rows:
        return [], {}
    start, end = rows[0]["ts"], rows[-1]["ts"]
    grid = list(range(int(start // BAR * BAR + BAR), int(end) + 1, BAR))
    pts: dict = {}
    for r in rows:
        for v, col in (("sig", "sig_mid"), ("pm", "pm"), ("kalshi", "kalshi")):
            if r[col] is not None:
                pts.setdefault(r["race"], {}).setdefault(v, []).append((r["ts"], r[col]))
    data = {race: {v: _ffill(p, grid, 900) for v, p in d.items()} for race, d in pts.items()}
    return grid, data


# ============================================================ linear algebra
def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-15:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-15 else 0.0 for i in range(n)]


def ridge(X: list[list[float]], y: list[float], penalties: list[float]) -> list[float]:
    """Ridge without intercept on standardised features; penalties are per-feature multipliers of lambda * n."""
    k = len(penalties)
    n = len(y)
    sd = [math.sqrt(sum(r[j] ** 2 for r in X) / max(1, n)) or 1.0 for j in range(k)]
    A = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for r, yy in zip(X, y):
        z = [r[j] / sd[j] for j in range(k)]
        for i in range(k):
            b[i] += z[i] * yy
            zi = z[i]
            Ai = A[i]
            for j in range(i, k):
                Ai[j] += zi * z[j]
    for i in range(k):
        for j in range(i):
            A[i][j] = A[j][i]
        A[i][i] += penalties[i] * n
    w = _solve(A, b)
    return [w[j] / sd[j] for j in range(k)]


def _r2(y: list[float], yhat: list[float]) -> float:
    ss = sum(v * v for v in y)                    # no intercept: compare against predicting zero change
    return 1 - sum((a - b) ** 2 for a, b in zip(y, yhat)) / ss if ss > 0 else float("nan")


# ============================================================ features
def diffs(s: list, lag: int) -> list:
    return [None if (i < lag or s[i] is None or s[i - lag] is None) else s[i] - s[i - lag] for i in range(len(s))]


def robust_scale(moves: list, i: int, window: int = 288) -> float:
    w = [abs(m) for m in moves[max(0, i - window): i] if m is not None and abs(m) > 1e-9]
    if len(w) < 10:
        return 0.0025
    return max(1.4826 * median(w), 0.0025)


def senate_betas(data: dict, offices: dict, lag: int = 6) -> dict:
    """Per race and venue: beta of the race's 30-minute D move on the same venue's Senate-control move, shrunk
    toward the office mean with weight n_eff / (n_eff + 50) where n_eff = number of bars with a non-zero Senate move."""
    raw: dict = {}
    S = {v: diffs(data.get(CHAMBER, {}).get(v) or [], lag) for v in VENUES}
    for race, d in data.items():
        if race == CHAMBER:
            continue
        for v in VENUES:
            s, r = S[v], diffs(d.get(v) or [], lag)
            if not s or not r:
                continue
            sxy = sxx = 0.0
            n = 0
            for i in range(0, min(len(s), len(r)), lag):          # non-overlapping 30-minute changes
                if s[i] is None or r[i] is None:
                    continue
                sxy += s[i] * r[i]
                sxx += s[i] * s[i]
                n += abs(s[i]) > 1e-9
            if sxx > 0:
                raw[(race, v)] = (sxy / sxx, n)
    out: dict = {}
    for v in VENUES:
        for off in set(offices.values()):
            vals = [raw[(r, v)][0] for r in data if (r, v) in raw and offices.get(r) == off]
            mu = sum(vals) / len(vals) if vals else 0.0
            for r in data:
                if (r, v) in raw and offices.get(r) == off:
                    b, n = raw[(r, v)]
                    w = n / (n + 50.0)
                    out.setdefault(r, {})[v] = {"beta": w * b + (1 - w) * mu, "raw": b, "n": n}
    return out


FEATURES = [  # (name, kind) kind: own | cross
    ("beta*S_own", "own"), ("beta*S_own*big", "own"), ("race_own_last", "own"),
    ("beta*S_cross", "cross"), ("race_cross_last", "cross"), ("gap_cross", "cross"),
]


def dataset(grid: list, data: dict, betas: dict, target: str, h: int) -> list[tuple]:
    """Rows (t_index, race, x[6], y)."""
    S = {v: diffs(data.get(CHAMBER, {}).get(v) or [None] * len(grid), 1) for v in VENUES}
    scale = {v: [robust_scale(S[v], i) for i in range(len(grid))] for v in VENUES}
    rows = []
    others = [v for v in VENUES if v != target]
    for race, d in data.items():
        if race == CHAMBER or target not in d:
            continue
        tgt = d[target]
        r1 = {v: diffs(d.get(v) or [None] * len(grid), 1) for v in VENUES}
        b = {v: (betas.get(race, {}).get(v) or {}).get("beta", 0.0) for v in VENUES}
        for i in range(2, len(grid) - h):
            if tgt[i] is None or tgt[i + h] is None:
                continue
            s_own = S[target][i]
            if s_own is None:
                continue
            big = 1.0 if (abs(s_own) / scale[target][i] >= 3 and abs(s_own) >= 0.01) else 0.0
            s_cross = [S[v][i] * b[v] for v in others if S[v][i] is not None]
            r_cross = [r1[v][i] for v in others if r1[v][i] is not None]
            outs = [d[v][i] for v in others if d.get(v) and d[v][i] is not None]
            own_last = r1[target][i] if r1[target][i] is not None else 0.0
            x = [b[target] * s_own, b[target] * s_own * big, own_last,
                 sum(s_cross) / len(s_cross) if s_cross else 0.0,
                 sum(r_cross) / len(r_cross) if r_cross else 0.0,
                 (sum(outs) / len(outs) - tgt[i]) if outs else 0.0]
            rows.append((i, race, x, tgt[i + h] - tgt[i]))
    return rows


def fit_eval(rows: list[tuple], lam: float, cross_pen: float, split_frac: float = 0.7) -> dict:
    if len(rows) < 200:
        return {"n": len(rows)}
    ts = sorted({r[0] for r in rows})
    cut = ts[int(len(ts) * split_frac)]
    tr = [r for r in rows if r[0] < cut]
    te = [r for r in rows if r[0] >= cut]
    pens = [lam * (cross_pen if k == "cross" else 1.0) for _, k in FEATURES]
    w = ridge([r[2] for r in tr], [r[3] for r in tr], pens)
    pred = lambda x: sum(a * b for a, b in zip(w, x))
    yte = [r[3] for r in te]
    pte = [pred(r[2]) for r in te]
    nz = [(a, b) for a, b in zip(yte, pte) if abs(a) > 1e-9 and abs(b) > 1e-9]
    hit = sum(1 for a, b in nz if a * b > 0) / len(nz) if nz else float("nan")
    big = [(r[3], pred(r[2])) for r in te if r[2][1] != 0]
    return {"n_train": len(tr), "n_test": len(te), "coef": dict(zip([f for f, _ in FEATURES], w)),
            "r2_train": _r2([r[3] for r in tr], [pred(r[2]) for r in tr]), "r2_test": _r2(yte, pte),
            "hit_test": hit, "n_hit": len(nz), "big_test_n": len(big),
            "big_test_r2": _r2([a for a, _ in big], [b for _, b in big]) if big else float("nan")}


def event_study(grid: list, data: dict, betas: dict) -> list[str]:
    """Cross-sectional pass-through: for each big Senate move on venue sv (z >= 3 and >= 1 pt), regress the same-bar and
    +30-minute moves of the COMPETITIVE races (D price in [0.15, 0.85]) on each venue tv on the Senate move, pooled over
    events (slope through the origin). Slope 0.5 = competitive races move half a point per point of Senate move.
    Pre-election the Senate markets barely move, so this table is mostly a placeholder for election night."""
    out = [f"{'S venue':8s} {'events':>6s} {'races':>6s}   " + "  ".join(f"{v + ' same/+30m':>22s}" for v in VENUES)]
    for sv in VENUES:
        S = diffs(data.get(CHAMBER, {}).get(sv) or [], 1)
        if not S:
            continue
        ev = [i for i in range(1, len(S) - 7) if S[i] is not None and abs(S[i]) >= 0.01 and abs(S[i]) / robust_scale(S, i) >= 3]
        cells, nr = [], 0
        for tv in VENUES:
            sxy0 = sxy6 = sxx0 = sxx6 = 0.0
            races = set()
            for race, d in data.items():
                if race == CHAMBER or not d.get(tv):
                    continue
                s = d[tv]
                for i in ev:
                    if None in (s[i - 1], s[i], s[i + 6]) or not (0.15 <= s[i - 1] <= 0.85):
                        continue
                    races.add(race)
                    sxy0 += S[i] * (s[i] - s[i - 1]); sxx0 += S[i] ** 2
                    sxy6 += S[i] * (s[i + 6] - s[i - 1]); sxx6 += S[i] ** 2
            nr = max(nr, len(races))
            cells.append(f"{(sxy0 / sxx0 if sxx0 else float('nan')):+6.2f} / {(sxy6 / sxx6 if sxx6 else float('nan')):+6.2f}")
        out.append(f"{sv:8s} {len(ev):6d} {nr:6d}   " + "  ".join(f"{c:>22s}" for c in cells))
    return out


def run(source: str = "offline", lam: float = 0.01, cross_pen: float = 10.0) -> dict:
    m = load_map()
    offices = {r: e["office"] for r, e in m["races"].items()}
    if source == "live":
        from .store import Store
        store = Store(os.path.join(HERE, "data", "sigbot.sqlite"))
        grid, data = build_grid_live(store)
    else:
        if not os.path.exists(HIST_PATH):
            fetch()
        grid, data = build_grid_offline(json.load(open(HIST_PATH)))
    print(f"grid: {len(grid)} bars of 5 min ({len(grid) * BAR / 86400:.1f} days) from "
          f"{datetime.fromtimestamp(grid[0]).strftime('%Y-%m-%d %H:%M') if grid else '-'}; races with data: {len(data)}")
    ch = data.get(CHAMBER, {})
    for v in VENUES:
        s = [x for x in ch.get(v) or [] if x is not None]
        d1 = [abs(x) for x in diffs(ch.get(v) or [], 1) if x is not None]
        print(f"  Senate control on {v:6s}: {len(s)} bars, range {min(s) if s else float('nan'):.3f}-{max(s) if s else float('nan'):.3f}, "
              f"non-zero 5-min moves {sum(1 for x in d1 if x > 1e-9)}, max |move| {max(d1) * 100 if d1 else 0:.1f} pt")
    betas = senate_betas(data, offices)
    print("\nSenate betas (30-minute co-movement, shrunk toward office mean), top swing races by SIG beta:")
    top = sorted(((r, b) for r, b in betas.items() if "sig" in b), key=lambda kv: -abs(kv[1]["sig"]["beta"]))[:12]
    for r, b in top:
        print(f"  {r:26s} " + "  ".join(f"{v}: {b[v]['beta']:+.2f} (raw {b[v]['raw']:+.2f}, n {b[v]['n']})" for v in VENUES if v in b))
    results: dict = {"built_at": time.time(), "source": source, "lambda": lam, "cross_penalty": cross_pen,
                     "features": FEATURES, "betas": betas, "models": {}}
    print(f"\nRidge, lambda {lam}, cross-market penalty x{cross_pen} (contrast: x1 and x1000). Out-of-sample = last 30% of time.")
    print(f"{'target':7s} {'h':>4s} {'n_test':>7s} {'R2 test':>8s} {'R2 x1':>7s} {'R2 x1000':>9s} {'hit%':>6s} {'bigR2':>7s}  coefficients")
    for target in VENUES:
        for h in (1, 6):
            rows = dataset(grid, data, betas, target, h)
            res = fit_eval(rows, lam, cross_pen)
            if "coef" not in res:
                print(f"{target:7s} {h * 5:3d}m  too few rows ({res['n']})")
                continue
            r1 = fit_eval(rows, lam, 1.0)
            r1000 = fit_eval(rows, lam, 1000.0)
            results["models"][f"{target}_{h * 5}m"] = res
            co = " ".join(f"{k}={v:+.3f}" for k, v in res["coef"].items())
            print(f"{target:7s} {h * 5:3d}m {res['n_test']:7d} {res['r2_test']:+8.3f} {r1['r2_test']:+7.3f} {r1000['r2_test']:+9.3f} "
                  f"{100 * res['hit_test']:6.1f} {res['big_test_r2']:+7.3f}  {co}")
    print("\nBig Senate moves (z >= 3 and >= 1 pt): competitive-race move per point of Senate move (pooled slope), same bar / +30 min")
    print("\n".join(event_study(grid, data, betas)))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    json.dump(results, open(OUT_PATH, "w"), indent=1, default=str)
    print(f"\nsaved {OUT_PATH}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--fetch-missing", action="store_true", help="refetch only instruments whose history came back empty")
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--source", choices=["offline", "live"], default="offline")
    ap.add_argument("--lam", type=float, default=0.01)
    ap.add_argument("--cross-penalty", type=float, default=10.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.fetch:
        fetch(args.days)
    elif args.fetch_missing:
        fetch(args.days, only_missing=True)
    run(args.source, args.lam, args.cross_penalty)


if __name__ == "__main__":
    main()
