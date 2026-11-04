"""Kalshi election-winner series for five elections that are skewed on Polymarket; favourite leg only. Writes raw/kalshi_registry_extra_elec.json"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from others_select import *
from others_k import kcall
from others_oscar_kalshi import plain
SER = {"germany-2025": "KXGERMANYPARTY", "canada-2025": "KXCANADAPARTY", "skorea-2025": "KXKOREAPRESPARTY", "mexico-2024": "KXMEXICOPRES", "australia-2025": "KXAUSTRALIA"}
if __name__ == "__main__":
    for s in SER.values():
        r, f = kcall("/historical/markets", dict(series_ticker=s, limit=1000))
        if not (r["data"] or {}).get("markets"): kcall("/markets", dict(series_ticker=s, status="settled", limit=1000)) if False else None
        print(s, len((r["data"] or {}).get("markets", [])), flush=True)
    km = kalshi_markets(); sel = select_pm(load_pm()); reg = {}
    for o in sel:
        if o["cluster"] not in SER or o["p10"] < .5: continue
        want = plain(o["contract"][len(o["cluster"]) + 5:].replace("-", " ").replace("labour", "labor")).split(); want = [w for w in want if w not in ("party", "the")]
        cand = [(t, m) for t, m in km.items() if t.startswith(SER[o["cluster"]])]
        best = [(t, m) for t, m in cand if m.get("result") in ("yes", "no") and any(w in plain(m.get("yes_sub_title")) for w in want if len(w) > 2)]
        print(o["contract"], [(t, m.get("yes_sub_title"), m["result"]) for t, m in best][:3], flush=True)
        if len(best) == 1: reg[best[0][0]] = dict(contract=o["contract"], cluster=o["cluster"], category="intl_election", T=o["T"])
    json.dump(reg, open(os.path.join(RAW, "kalshi_registry_extra_elec.json"), "w"))
    for t, r in reg.items():
        T = r["T"]; kcall(f"/historical/markets/{t}/candlesticks", dict(start_ts=T - 14 * DAY, end_ts=T + DAY, period_interval=60)); print("candles", t, flush=True)
