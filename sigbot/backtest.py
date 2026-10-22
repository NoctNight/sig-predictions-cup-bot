"""Quick backtests on the tournament's own price history (candles from /exchanges/{id}/price-history).

    python -m sigbot.backtest --candles data/hist.json            # 1h candles
    python -m sigbot.backtest --candles data/candles_5m.json      # 5m candles

Tests (all in-sample, no fees, YES prices):
  1. AR(1) of price changes per horizon: mean reversion if the slope on the previous change is negative.
  2. Reversal-after-large-move: when |dp| over one bar exceeds k * sigma, what is the mean next-bar move, signed
     against the shock? Reports hit rate and average edge in ticks, and a naive "fade" P&L assuming we could
     trade at the close (upper bound) and at close +/- half the typical spread (0.0025) (closer to reality).
  3. Complement-sum reversion: for each race, s_t = sum of leg closes - 1; does s_t revert (AR(1) on s)?
     This is the dynamic version of the set arb: trade the set when |s| is large and unwind when it closes.
  4. Momentum: sign of the last 6-bar move vs next-bar move.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK = 0.005
TITLE_RE = re.compile(r"Will the (Democratic|Republican|Independent) Party win the (.+)\?")


def ols_slope(x, y):
    n = len(x)
    if n < 10:
        return float("nan"), float("nan"), n
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx == 0:
        return float("nan"), float("nan"), n
    b = sum((a - mx) * (c - my) for a, c in zip(x, y)) / sxx
    resid = [c - (my + b * (a - mx)) for a, c in zip(x, y)]
    se = math.sqrt(sum(r * r for r in resid) / max(1, n - 2) / sxx)
    return b, (b / se if se > 0 else float("nan")), n


def load_series(path):
    raw = json.load(open(path))
    out = {}
    for ex, h in raw.items():
        c = [x for x in h.get("candles", []) if x.get("close") is not None]
        if len(c) >= 15:
            out[ex] = c
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", default="data/hist.json")
    ap.add_argument("--markets", default="data/markets.json")
    ap.add_argument("--min-price", type=float, default=0.05)
    ap.add_argument("--max-price", type=float, default=0.95)
    args = ap.parse_args()
    series = load_series(os.path.join(HERE, args.candles))
    markets = json.load(open(os.path.join(HERE, args.markets)))
    ex_to_race = {}
    for m in markets:
        mt = TITLE_RE.match(m["title"])
        if mt:
            ex_to_race[m["exchanges"][0]["id"]] = (mt.group(2), mt.group(1)[0])
    print(f"series: {len(series)} exchanges, bars per series median {statistics.median(len(v) for v in series.values())}")

    # ---------------------------------------------------------------- 1. AR(1) on changes
    for horizon in (1, 3, 6, 12):
        xs, ys = [], []
        for ex, c in series.items():
            closes = [x["close"] for x in c]
            for i in range(horizon, len(closes) - horizon):
                p0, p1, p2 = closes[i - horizon], closes[i], closes[i + horizon]
                if not (args.min_price <= p1 <= args.max_price):
                    continue
                xs.append(p1 - p0); ys.append(p2 - p1)
        b, t, n = ols_slope(xs, ys)
        print(f"AR(1) changes, horizon={horizon} bars: slope={b:+.3f} t={t:+.1f} n={n}  ({'mean-reverting' if b < 0 else 'trending'})")

    # ---------------------------------------------------------------- 2. fade large moves
    print()
    for k in (1.5, 2.0, 3.0):
        hits = tot = 0; edges = []; pnl_mid = []; pnl_spread = []
        for ex, c in series.items():
            closes = [x["close"] for x in c]
            d = [b - a for a, b in zip(closes, closes[1:])]
            if len(d) < 20:
                continue
            sig = statistics.pstdev(d) or 1e-9
            for i in range(1, len(d)):
                if not (args.min_price <= closes[i] <= args.max_price):
                    continue
                if abs(d[i - 1]) >= k * sig and abs(d[i - 1]) >= TICK:
                    nxt = d[i]
                    fade = -math.copysign(1, d[i - 1]) * nxt   # + if the next move reverses the shock
                    tot += 1; hits += fade > 0; edges.append(fade / TICK)
                    pnl_mid.append(fade); pnl_spread.append(fade - 2 * 0.0025)
        if tot:
            print(f"fade |dp|>={k}sigma: n={tot} hit={hits/tot:.2f} mean reversal={statistics.mean(edges):+.2f} ticks "
                  f"P&L/share at mid={statistics.mean(pnl_mid)*100:+.3f}c, crossing spread={statistics.mean(pnl_spread)*100:+.3f}c")

    # ---------------------------------------------------------------- 3. complement-sum reversion
    print()
    by_race = defaultdict(dict)
    for ex, (race, party) in ex_to_race.items():
        if ex in series:
            by_race[race][party] = series[ex]
    xs, ys, dev = [], [], []
    for race, legs in by_race.items():
        if len(legs) < 2:
            continue
        times = None
        for c in legs.values():
            ts = {x["time"]: x["close"] for x in c}
            times = set(ts) if times is None else times & set(ts)
        times = sorted(times)
        s = []
        for t in times:
            s.append(sum({x["time"]: x["close"] for x in c}[t] for c in legs.values()) - 1.0)
        for i in range(1, len(s) - 1):
            xs.append(s[i] - 0.0); ys.append(s[i + 1] - s[i]); dev.append(abs(s[i]))
    b, t, n = ols_slope(xs, ys)
    print(f"complement sum s=sum(legs)-1: mean|s|={statistics.mean(dev)*100:.2f}c, AR(1) on s: slope={b:+.3f} t={t:+.1f} n={n}"
          f"  (negative = sum deviations close; half-life {(-math.log(2)/math.log(1+b) if -1 < b < 0 else float('nan')):.1f} bars)")

    # ---------------------------------------------------------------- 4. momentum
    print()
    for look in (6, 24):
        hits = tot = 0; edges = []
        for ex, c in series.items():
            closes = [x["close"] for x in c]
            for i in range(look, len(closes) - 1):
                if not (args.min_price <= closes[i] <= args.max_price):
                    continue
                mv = closes[i] - closes[i - look]
                if abs(mv) < TICK:
                    continue
                nxt = closes[i + 1] - closes[i]
                cont = math.copysign(1, mv) * nxt
                tot += 1; hits += cont > 0; edges.append(cont / TICK)
        if tot:
            print(f"momentum look={look}: n={tot} continuation hit={hits/tot:.2f} mean next move along trend={statistics.mean(edges):+.3f} ticks")


if __name__ == "__main__":
    main()
