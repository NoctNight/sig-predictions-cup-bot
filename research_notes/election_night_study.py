"""2024 (and 2022) election night on Polymarket / Kalshi: how far outside prices moved after polls closed, how long before
the first network / AP call they priced the winner, and how often a 0.95 price was wrong.

    python3 research_notes/election_night_study.py            # reads data/election_night/ only (no network)

Inputs: paths_2024.csv (1-minute Polymarket prices-history, Kalshi 1-minute candles), race_calls.csv (AP via the NYT feed,
AP text, Fox, DDHQ, NBC call times), paths_2022.csv when present (2022 Polymarket AMM trades). Poll closes and the price
cleaning (stale Polymarket last trades, wide Kalshi books) come from sigbot/proposals/election_night.py.
"""
from __future__ import annotations

import collections
import csv
import json
import os
import statistics
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from sigbot.proposals.election_night import DATA, EST, _at, load_2024, poll_close_ts  # noqa: E402


def calls_2024() -> dict:
    """(office, ST, district) -> {'ap': ts, 'first': ts, 'first_by': caller}; AP from the NYT feed or AP text."""
    out: dict = {}
    for r in csv.DictReader(open(os.path.join(DATA, "race_calls.csv"))):
        if r["cycle"] != "2024" or not r["call_time_utc"]:
            continue
        try:
            ts = datetime.fromisoformat(r["call_time_utc"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        dist = r["district"] or ""
        dist = "special" if "special" in dist.lower() else "".join(ch for ch in dist if ch.isdigit()).lstrip("0")
        key = (r["office"].lower(), r["state"], dist)
        if "UNVERIFIED" in r["note"] or "default" in r["note"].lower():
            continue
        e = out.setdefault(key, {})
        if r["caller"].startswith("AP"):
            e["ap"] = min(e.get("ap", 1e12), ts)
        if ts < e.get("first", 1e12):
            e["first"], e["first_by"] = ts, r["caller"]
    return out


def first_cross(series_list: list, t0: float, t1: float, level: float, winner_d: bool, step: int = 60):
    """First minute in [t0, t1] at which every available venue prices the winner at >= level."""
    t = t0
    while t <= t1:
        vals = []
        for s in series_list:
            v = _at(s, t) if s else None
            if v is not None:
                vals.append(v if winner_d else 1 - v)
        if vals and min(vals) >= level:
            return t
        t += step
    return None


def pctl(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def main():
    data = load_2024()
    calls = calls_2024()
    rows = []
    for key, d in data.items():
        st = key[1]
        close = poll_close_ts(st, 2024, "last")
        if close is None:
            continue
        wd = d["winner"] == "D"
        ser = [d.get("pm") or [], d.get("kalshi") or []]
        end = close + 30 * 3600
        both = [s for s in ser if s]

        def w_at(t):
            vals = [(_at(s, t)) for s in both]
            vals = [v if wd else 1 - v for v in vals if v is not None]
            return sum(vals) / len(vals) if vals else None
        p_close = w_at(close)
        if p_close is None:
            continue
        t95 = first_cross(both, close, end, 0.95, wd)
        t95_pm = first_cross([ser[0]], close, end, 0.95, wd) if ser[0] else None
        t95_k = first_cross([ser[1]], close, end, 0.95, wd) if ser[1] else None
        t90 = first_cross(both, close, end, 0.90, wd)
        # loser ever >= 0.95 / 0.90 on all available venues after the close (a wrong "call" by the price rule)
        loser95 = first_cross(both, close, end, 0.95, not wd)
        loser90 = first_cross(both, close, end, 0.90, not wd)
        loser95_any = any(first_cross([s], close, end, 0.95, not wd) for s in both)
        c = calls.get(key, {})
        rows.append(dict(key="/".join(k for k in key if k), office=key[0], close=close, p_close=p_close,
                         p30=w_at(close + 1800), p60=w_at(close + 3600), p120=w_at(close + 7200), p240=w_at(close + 14400),
                         t95=t95, t95_pm=t95_pm, t95_k=t95_k, t90=t90, loser95=loser95, loser90=loser90, loser95_any=loser95_any,
                         ap=c.get("ap"), first=c.get("first"), first_by=c.get("first_by"), venues=len(both)))
    n = len(rows)
    print(f"2024 races with outside prices at poll close: {n} ({collections.Counter(r['office'] for r in rows)})")
    # ---- 1. how far prices moved after close
    print("\n## 1. Winner's outside price after the state's last poll close (mean of Polymarket and Kalshi)")
    print("| bucket at close | n | at close | +30 min | +60 | +120 | +240 | crossed 0.95 | median min to 0.95 |\n|---|---|---|---|---|---|---|---|---|")
    for lo, hi, lab in ((0.95, 1.01, ">= 0.95"), (0.80, 0.95, "0.80-0.95"), (0.50, 0.80, "0.50-0.80"), (0.0, 0.50, "< 0.50 (upset)")):
        g = [r for r in rows if lo <= r["p_close"] < hi]
        if not g:
            continue
        m = lambda k: statistics.mean(r[k] for r in g if r[k] is not None)
        mins = [(r["t95"] - r["close"]) / 60 for r in g if r["t95"]]
        print(f"| {lab} | {len(g)} | {m('p_close'):.3f} | {m('p30'):.3f} | {m('p60'):.3f} | {m('p120'):.3f} | {m('p240'):.3f} | "
              f"{len(mins)}/{len(g)} | {statistics.median(mins) if mins else '-'} |")
    # ---- 2. lead over calls
    print("\n## 2. Minutes from the both-venue 0.95 crossing to the call (positive = markets first)")
    for lab, k in (("AP", "ap"), ("first network/desk", "first")):
        leads = [((r[k] - r["t95"]) / 60, r) for r in rows if r[k] and r["t95"] and r["p_close"] < 0.95]
        at_close = [((r[k] - r["t95"]) / 60) for r in rows if r[k] and r["t95"] and r["p_close"] >= 0.95]
        xs = [x for x, _ in leads]
        if xs:
            print(f"* {lab}, races not already >= 0.95 at close: n {len(xs)}, median {statistics.median(xs):+.0f} min, "
                  f"p25 {pctl(xs, 0.25):+.0f}, p75 {pctl(xs, 0.75):+.0f}, markets first in {sum(x > 0 for x in xs)}/{len(xs)}")
        if at_close:
            print(f"  ({lab}, races >= 0.95 at close: n {len(at_close)}, median {statistics.median(at_close):+.0f} min)")
        if k == "ap":
            for x, r in sorted(leads, key=lambda z: -z[0])[:14]:
                f = lambda t: datetime.fromtimestamp(t, EST).strftime("%d %H:%M") if t else "-"
                print(f"    {r['key']:18s} close {f(r['close'])} p_close {r['p_close']:.2f} 0.95 at {f(r['t95'])} AP {f(r['ap'])} "
                      f"first {f(r['first'])} ({r['first_by']}) lead {x:+.0f} min")
    # ---- 3. Polymarket vs Kalshi
    d = [(r["t95_k"] - r["t95_pm"]) / 60 for r in rows if r["t95_pm"] and r["t95_k"] and r["p_close"] < 0.95]
    if d:
        print(f"\n## 3. Kalshi 0.95 crossing minus Polymarket's: n {len(d)}, median {statistics.median(d):+.0f} min, "
              f"Polymarket first {sum(x > 0 for x in d)}, Kalshi first {sum(x < 0 for x in d)}, same minute {sum(x == 0 for x in d)}")
    # ---- 4. wrong calls by the price rule
    print("\n## 4. Price-rule false calls after the close")
    for lab, k in (("loser >= 0.95 on every venue", "loser95"), ("loser >= 0.90 on every venue", "loser90"),
                   ("loser >= 0.95 on any one venue", "loser95_any")):
        bad = [r["key"] for r in rows if r[k]]
        print(f"* {lab}: {len(bad)}/{n} {bad[:8]}")
    # ---- 5. 2022 if present
    p22 = os.path.join(DATA, "paths_2022.csv")
    if os.path.exists(p22):
        study_2022(p22)
    json.dump([{k: v for k, v in r.items()} for r in rows], open(os.path.join(DATA, "study_2024.json"), "w"), indent=0, default=str)


def study_2022(path: str, min_usd: float = 50.0):
    """One row per 2022 market (the D or I leg; trades under min_usd dropped as AMM slippage noise)."""
    import re
    from sigbot.venues.states import NAME_TO_ABBR
    legs: dict = collections.defaultdict(list)
    meta = {}
    for r in csv.DictReader(open(path)):
        if not r.get("price") or r["party"] == "R" or float(r["usd_size"] or 0) < min_usd:
            continue
        legs[r["market_id"]].append((int(float(r["ts"])), float(r["price"])))
        meta[r["market_id"]] = (r["office"], r["outcome"], r["question"])
    calls = {}
    for r in csv.DictReader(open(os.path.join(DATA, "race_calls.csv"))):
        if r["cycle"] == "2022" and r["call_time_utc"]:
            try:
                k = (r["office"].lower(), r["state"])
                calls[k] = min(calls.get(k, 1e12), datetime.fromisoformat(r["call_time_utc"].replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
    f = lambda t: datetime.fromtimestamp(t, EST).strftime("%d %H:%M") if t else "-"
    out = []
    for mid, s in legs.items():
        s.sort()
        office, won, q = meta[mid]
        m = re.search(r"in ([A-Z][A-Za-z ]+)\?", q) or re.search(r"^([A-Z]{2})-\d+", q)
        st = NAME_TO_ABBR.get(m.group(1).strip()) if m and len(m.group(1)) > 2 else (m.group(1) if m else None)
        close = poll_close_ts(st, 2022, "last") if st else None
        if close is None or len(s) < 5:
            continue
        w = (lambda p: p) if won == "1" else (lambda p: 1 - p)
        pc, p60, p240 = (_at(s, close + d) for d in (0, 3600, 14400))
        x95 = next((t for t, p in s if t >= close and w(p) >= 0.95), None)
        out.append((office, st, won, len(s), pc, p60, p240, x95, calls.get((office, st)), close))
    print(f"\n## 5. 2022 Polymarket (AMM trades >= {min_usd:.0f} USD): {len(out)} markets")
    print("| race | won | trades | winner at close | +60 min | +240 | first >= 0.95 | first call | lead (min) |\n|---|---|---|---|---|---|---|---|---|")
    for office, st, won, n, pc, p60, p240, x95, c, close in sorted(out, key=lambda r: r[-1]):
        w = (lambda p: p) if won == "1" else (lambda p: 1 - p)
        g = lambda p: f"{w(p):.2f}" if p is not None else "-"
        lead = f"{(c - x95) / 60:+.0f}" if c and x95 else "-"
        print(f"| {st} {office} | {'D/I' if won == '1' else 'R'} | {n} | {g(pc)} | {g(p60)} | {g(p240)} | {f(x95)} | {f(c)} | {lead} |")


if __name__ == "__main__":
    main()
