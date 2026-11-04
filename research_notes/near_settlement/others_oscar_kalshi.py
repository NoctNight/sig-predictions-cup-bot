"""Kalshi Oscars candles for the favourite legs (PM price at T-10 >= 0.5), same person/film. Writes raw/kalshi_registry_oscars.json (ticker -> contract info)."""
import os, sys, json, unicodedata
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from others_select import *
from others_k import kcall
SER = {"picture": "KXOSCARPIC", "actor": "KXOSCARACTO", "actress": "KXOSCARACTR", "director": "KXOSCARDIR"}
def plain(s): return norm(unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode())
def map_legs(sel, km):
    reg = {}
    for o in sel:
        if o["category"] != "awards" or o["p10"] < .5: continue
        yy = o["cluster"][-2:]
        q = plain(o["question"])
        for t, m in km.items():
            if m["event_ticker"] != f"{SER[o['subject']]}-{yy}" and m["event_ticker"] != f"{SER[o['subject']][2:]}-{yy}": continue
            nm = plain(m.get("yes_sub_title"))
            if nm and (nm in q or (len(nm.split()) > 1 and nm.split()[-1] in q.split())):
                reg[t] = dict(contract=o["contract"], cluster=o["cluster"], category="awards", T=o["T"], pm_question=o["question"]); break
        else:
            print("NO KALSHI MATCH", o["contract"])
    return reg
if __name__ == "__main__":
    for s in SER.values(): kcall("/historical/markets", dict(series_ticker=s, limit=1000))
    km = kalshi_markets()
    reg = map_legs(select_pm(load_pm()), km)
    json.dump(reg, open(os.path.join(RAW, "kalshi_registry_oscars.json"), "w"))
    print(len(reg), flush=True)
    for t, r in reg.items():
        T = r["T"]
        kcall(f"/historical/markets/{t}/candlesticks", dict(start_ts=T - 14 * DAY, end_ts=T + DAY, period_interval=60))
        print(t, km[t].get("yes_sub_title"), km[t].get("result"), flush=True)
