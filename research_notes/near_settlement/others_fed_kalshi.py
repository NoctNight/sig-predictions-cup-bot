"""Kalshi Fed-decision candles for the contracts selected on Polymarket (same contract, T-14..T+1). ~1 call per historical leg, 1 batch call per live meeting."""
import os, sys, json, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from others_common import *
from others_k import kcall
MAP = {"nochange": "H0", "cut25": "C25", "cut50plus": "C26", "hike25": "H25"}   # PM contract suffix -> Kalshi suffix
def pm_selected():
    c = json.load(open(os.path.join(RAW, "pm_candidates_fed.json")))
    by = collections.defaultdict(list)
    for o in c: by[o["cluster"]].append(o)
    sel = []
    for k, v in by.items():
        v = sorted(v, key=lambda o: -(o["p10"] or 0))
        sel += [o for o in v[:2] if o["p10"] is not None and .02 <= o["p10"] <= .98]
    return sel
def kalshi_tickers():
    """(y,m) -> {suffix: ticker}, from the cached /historical/markets list and the live settled list"""
    ym = collections.defaultdict(lambda: collections.defaultdict(dict))
    for f in os.listdir(RAW):
        if f.startswith("k_historical_markets_") and "candlesticks" not in f or f.startswith("k_markets_") and "candle" not in f:
            d = json.load(open(os.path.join(RAW, f)))["data"]
            for m in d.get("markets", []):
                t = m["ticker"]; ev = m["event_ticker"]
                if "FEDDECISION" not in ev: continue
                ym[(int(m["close_time"][:4]), int(m["close_time"][5:7]))][ev][t.split("-")[-1]] = (t, float(m.get("volume_fp") or 0))
    out = {}
    for k, evs in ym.items():
        ev = max(evs, key=lambda e: sum(v[1] for v in evs[e].values()))
        out[k] = {s: v[0] for s, v in evs[ev].items()}
    return out
if __name__ == "__main__":
    kt = kalshi_tickers()
    live = {(2026, 9), (2026, 10)}
    done_live = set()
    for o in pm_selected():
        y, m = int(o["cluster"][4:8]), int(o["cluster"][9:])
        suf = MAP.get(o["contract"].split("-", 3)[-1])
        if not suf or suf not in kt.get((y, m), {}):
            print("no kalshi map", o["contract"]); continue
        tk = kt[(y, m)][suf]; T = o["T"]
        if (y, m) in live:
            if (y, m) in done_live: continue
            done_live.add((y, m))
            tks = ",".join(kt[(y, m)][s] for s in ("H0", "H25", "C25", "C26", "H26")) if (y, m) == (2026, 10) else ",".join(kt[(y, m)][s] for s in ("H0", "H25", "C25", "C26", "H26"))
            r, f = kcall("/markets/candlesticks", dict(market_tickers=tks, start_ts=T - 14 * DAY, end_ts=T + DAY, period_interval=60))
        else:
            r, f = kcall(f"/historical/markets/{tk}/candlesticks", dict(start_ts=T - 14 * DAY, end_ts=T + DAY, period_interval=60))
        print(tk, r and r["code"], flush=True)
