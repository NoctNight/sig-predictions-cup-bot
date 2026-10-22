"""Leaderboard model behind research_notes/volume_target.md.

    python3 research_notes/volume_target_model.py [data/leaderboard_full.json]

Prints volume quantiles for the top 3 / 10 / 50 / 1 %, per-day paces, the rank correlation of volume with P&L
by rank band, and a logistic fit of "finishes top 10 / top 50" on log10 volume. Stdlib only, no API calls.
"""
import json
import math
import statistics as st
import sys
from datetime import datetime, timezone

PATH = sys.argv[1] if len(sys.argv) > 1 else "data/leaderboard_full.json"
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
ME = "sunny-beacon-7397"

raw = json.load(open(PATH))
rows = raw["rows"] if isinstance(raw, dict) else raw
pulled = raw.get("pulled_at") if isinstance(raw, dict) else None
now = datetime.fromisoformat(pulled.replace("Z", "+00:00")) if pulled else datetime.now(timezone.utc)
days = (now - START).total_seconds() / 86400
rows = [r for r in rows if r.get("volume") is not None]
rows.sort(key=lambda r: -r["pnl"])
n = len(rows)
print(f"{n} accounts, pulled {pulled}, {days:.1f} days since 1 Oct")


def q(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = math.floor(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summ(name, sub):
    v = [r["volume"] for r in sub]
    t = [r["tradesCount"] for r in sub]
    p = [r["pnl"] for r in sub]
    print(f"| {name} | {len(sub)} | {q(v, .25) / 1e6:.2f} / {q(v, .5) / 1e6:.2f} / {q(v, .75) / 1e6:.2f} | "
          f"{q(v, .5) / days / 1e3:.0f}k | {q(t, .5) / days:.0f} | {q([r['volume'] / max(r['tradesCount'], 1) for r in sub], .5):.0f} | "
          f"{q(p, .5) / 1e3:.0f}k | {100 * q([r['pnl'] / max(r['volume'], 1) for r in sub], .5):.1f}% |")


print("\n| group | n | volume p25/med/p75 (M) | vol/day (median) | trades/day | avg trade | P&L median | P&L/vol |")
print("|---|---|---|---|---|---|---|---|")
summ("top 3", rows[:3])
summ("top 10", rows[:10])
summ("top 20", rows[:20])
summ("top 50", rows[:50])
summ("top 1%", rows[:max(1, n // 100)])
summ("top 5%", rows[:n // 20])
summ("all", rows)
me = [r for r in rows if r["username"] == ME]
if me:
    summ("us", me)
    byv = sorted(rows, key=lambda r: -r["volume"])
    print(f"us: P&L rank {rows.index(me[0]) + 1}, volume rank {byv.index(me[0]) + 1}")


def rank(xs):
    s = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0] * len(xs)
    for k, i in enumerate(s):
        r[i] = k
    return r


def spearman(a, b):
    ra, rb = rank(a), rank(b)
    ma, mb = st.mean(ra), st.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else float("nan")


V = [r["volume"] for r in rows]
P = [r["pnl"] for r in rows]
T = [r["tradesCount"] for r in rows]
print(f"\nSpearman volume~P&L all: {spearman(V, P):.3f}; trades~P&L: {spearman(T, P):.3f}")
for lo, hi in [(0, 50), (50, 200), (200, 1000), (1000, n)]:
    sub = rows[lo:hi]
    print(f"  P&L ranks {lo + 1}-{hi}: volume~P&L {spearman([r['volume'] for r in sub], [r['pnl'] for r in sub]):+.3f}, "
          f"volume~|P&L| {spearman([r['volume'] for r in sub], [abs(r['pnl']) for r in sub]):+.3f}")
print("\nP&L by volume bucket:")
for lo, hi in [(5e6, 1e12), (1e6, 5e6), (3e5, 1e6), (1e5, 3e5), (0, 1e5)]:
    sub = [r for r in rows if lo <= r["volume"] < hi]
    if not sub:
        continue
    print(f"  {lo / 1e6:.1f}-{'inf' if hi > 1e11 else f'{hi / 1e6:.1f}'}M: n={len(sub)}, median P&L {st.median(r['pnl'] for r in sub):.0f}, "
          f"median P&L/vol {100 * st.median(r['pnl'] / max(r['volume'], 1) for r in sub):.2f}%, "
          f"P&L>0 {100 * sum(r['pnl'] > 0 for r in sub) / len(sub):.0f}%, p90 P&L {q([r['pnl'] for r in sub], .9):.0f}")


def fit(x, y, iters=200):
    b0, b1 = -5.0, 0.5
    for _ in range(iters):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for xi, yi in zip(x, y):
            z = b0 + b1 * xi
            p = 1 / (1 + math.exp(-z)) if z > -700 else 0.0
            w = p * (1 - p)
            g0 += yi - p
            g1 += (yi - p) * xi
            h00 += w
            h01 += w * xi
            h11 += w * xi * xi
        det = h00 * h11 - h01 * h01
        if det == 0:
            break
        d0 = (h11 * g0 - h01 * g1) / det
        d1 = (-h01 * g0 + h00 * g1) / det
        b0 += d0
        b1 += d1
        if abs(d0) + abs(d1) < 1e-8:
            break
    return b0, b1


x = [math.log10(max(r["volume"], 1)) for r in rows]
for k in (10, 50):
    b0, b1 = fit(x, [1 if i < k else 0 for i in range(n)])
    probs = {v: 1 / (1 + math.exp(-(b0 + b1 * math.log10(v)))) for v in (1e5, 1e6, 3e6, 1e7, 2e7)}
    print(f"\nlogit P(top {k}) = s({b0:.2f} + {b1:.2f} log10 volume): " +
          ", ".join(f"{v / 1e6:g}M -> {p:.3f}" for v, p in probs.items()))
# volume needed for 50% odds
    if b1 > 0:
        print(f"  volume at 50% odds of top {k}: {10 ** (-b0 / b1) / 1e6:.1f}M total, {10 ** (-b0 / b1) / days / 1e3:.0f}k/day")
byv = sorted(rows, key=lambda r: -r["volume"])
print("\ntop 10 by volume -> P&L ranks:", [rows.index(r) + 1 for r in byv[:10]])
print("top 10 by P&L -> volume ranks:", [byv.index(r) + 1 for r in rows[:10]])
print(f"sum of volume, all accounts: {sum(V) / 1e6:.0f}M = {sum(V) / days / 1e6:.1f}M/day; top 10: {sum(r['volume'] for r in rows[:10]) / days / 1e6:.1f}M/day")
