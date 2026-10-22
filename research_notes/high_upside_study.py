"""Reproduces the tables in research_notes/high_upside.md from the cached data files (no API reads).

    python3 research_notes/high_upside_study.py

Sections: opening-session transfer (candles_1d), per-market 5-minute volatility (candles_5m), book floors (books.json).
"""
import collections
import json
import os
import statistics as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    with open(os.path.join(ROOT, "data", name)) as f:
        return json.load(f)


def opening_transfer(markets, c1d):
    tot = {}
    spans = 0
    fav_vwap = []
    for ex, d in c1d.items():
        ks = d["candles"]
        if not ks:
            continue
        last = ks[-1]["close"]
        first = [k for k in ks if k["time"][:10] <= "2026-10-02"]
        if first and min(k["low"] for k in first) <= 0.005 and max(k["high"] for k in first) >= 0.995:
            spans += 1
        if first and last >= 0.8 and first[0]["time"][:10] == "2026-10-01":
            fav_vwap.append(first[0]["vwap"])
        for k in ks:
            day = k["time"][:10]
            if day > "2026-10-05":
                break
            tr = (last - k["vwap"]) * k["volume"]
            t = tot.setdefault(day, [0.0, 0.0, 0.0])
            t[0] += abs(tr)
            t[1] += max(tr, 0.0)
            t[2] += k["volume"] * k["vwap"]
    print("day        |transfer|  buyer-gain  notional   (marked at today's close)")
    for day in sorted(tot):
        a, b, n = tot[day]
        print(f"{day}  {a/1e6:6.2f}M  {b/1e6:6.2f}M  {n/1e6:6.1f}M")
    print(f"markets printing both 0.005 and 0.995 on 1-2 Oct: {spans} of {len(c1d)}")
    if fav_vwap:
        print(f"favourites (now >= 0.80) day-1 VWAP median {st.median(fav_vwap):.2f}, min {min(fav_vwap):.2f}, n {len(fav_vwap)}")


def volatility(markets, c5m):
    rows = []
    for ex, d in c5m.items():
        ks = d["candles"]
        if len(ks) < 50:
            continue
        moves = [abs(ks[i]["close"] - ks[i - 1]["close"]) for i in range(1, len(ks))]
        big3 = sum(1 for x in moves if x >= 0.0299)
        days = len(ks) / 288.0
        rows.append((big3 / days, ks[-1]["close"], markets.get(d["marketId"], {}).get("title", ex)[:50]))
    buckets = collections.defaultdict(list)
    for rate, px, title in rows:
        p = min(px, 1 - px)
        b = "<0.05" if p < 0.05 else "<0.15" if p < 0.15 else "<0.30" if p < 0.30 else "0.30-0.50"
        buckets[b].append(rate)
    print("\n>= 3-tick 5-min moves per market-day, by distance from 0/1 (median / mean / n)")
    for b in ("<0.05", "<0.15", "<0.30", "0.30-0.50"):
        v = buckets[b]
        print(f"  {b:10s} {st.median(v):4.1f} / {st.mean(v):4.1f} / {len(v)}")
    rows.sort(reverse=True)
    print("most active markets:")
    for rate, px, title in rows[:10]:
        print(f"  {rate:4.1f}/day  px {px:.3f}  {title}")
    print(f"venue total per day: {sum(r[0] for r in rows):.0f}")


def floors(markets, books):
    big = 0
    floor_bids = []
    for ex, d in books.items():
        for side in ("bids", "asks"):
            big += sum(1 for lv in d[side] if lv["quantity"] >= 100000)
        if d["bids"] and d["bids"][0]["price"] >= 0.8:
            floor_bids.append((d["bids"][-1]["price"], d["bids"][-1]["quantity"]))
    print(f"\nbook levels with >= 100k shares: {big}")
    if floor_bids:
        q = sorted(x[1] for x in floor_bids)
        print(f"favourite legs {len(floor_bids)}: deepest bid price {max(x[0] for x in floor_bids):.3f} max, size median {q[len(q)//2]:,}")


if __name__ == "__main__":
    markets = {m["id"]: m for m in load("markets.json")}
    opening_transfer(markets, load("candles_1d.json"))
    volatility(markets, load("candles_5m.json"))
    floors(markets, load("books.json"))
