"""Fed decisions on Polymarket: all legs' hourly history T-14..T+1, T = FOMC statement 14:00 ET. Writes raw/pm_candidates_fed.json"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from others_common import *
# (event id, y, m, d of last meeting day). 14:00 ET = 19:00 UTC in EST (Nov-Mar), 18:00 UTC in EDT
M = [(903022,2024,1,31),(903377,2024,3,20),(10098,2024,5,1),(10535,2024,6,12),(11074,2024,7,31),(11696,2024,9,18),(11827,2024,11,7),(11878,2024,12,18),
     (14220,2025,1,29),(15665,2025,3,19),(17140,2025,5,7),(17792,2025,6,18),(21255,2025,7,30),(24087,2025,9,17),(27824,2025,10,29),(35090,2025,12,10),
     (45883,2026,1,28),(67284,2026,3,18),(75478,2026,4,29),(101772,2026,6,17),(287395,2026,7,29),(481717,2026,9,16),(606422,2026,10,28)]
EDT = [(utc(2024,3,10,7), utc(2024,11,3,6)), (utc(2025,3,9,7), utc(2025,11,2,6)), (utc(2026,3,8,7), utc(2026,11,1,6))]
def stmt(y, m, d):
    t = utc(y, m, d, 18)
    return t if any(a <= t < b for a, b in EDT) else utc(y, m, d, 19)
def label(g):
    g = (g or "").lower()
    if "other" in g: return None
    if "no change" in g or g.startswith("0 bps"): return "nochange"
    n = g.split(" ")[0]
    dirn = "cut" if "decrease" in g else "hike"
    plus = "plus" if "+" in n else ""
    return f"{dirn}{n.strip('+')}{plus}"
out = []
for eid, y, m, d in M:
    ev = pm_event(eid); ev = ev[0] if isinstance(ev, list) else ev
    T = stmt(y, m, d)
    for mk in ev["markets"]:
        lab = label(mk.get("groupItemTitle"))
        if not lab: continue
        tok, win = yes_token_and_win(mk)
        h = pm_hist(tok, T)
        out.append(dict(cluster=f"fed-{y}-{m:02d}", contract=f"fed-{y}-{m:02d}-{lab}", token=tok, T=T, outcome=win, question=mk["question"], volume=float(mk.get("volume") or 0),
                        n=len(h), p10=price_at(h, T - 10 * DAY), p1=price_at(h, T - 3600), plast=h[-1]["p"] if h else None))
    print(y, m, [(o["contract"].split("-")[-1], o["n"], o["p10"], o["outcome"]) for o in out if o["cluster"] == f"fed-{y}-{m:02d}"], flush=True)
json.dump(out, open(os.path.join(RAW, "pm_candidates_fed.json"), "w"))
