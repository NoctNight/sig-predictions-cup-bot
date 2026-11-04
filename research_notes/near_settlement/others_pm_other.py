"""Polymarket awards + non-US national elections: top-8-by-volume live legs per event, hourly history T-14..T+1, candidates saved to raw/pm_candidates_other.json."""
import os, sys, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from others_common import *
from datetime import datetime
def slug(s): return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]
# (category, cluster, subject, event id, T)
OSC = {"oscars-2024": utc(2024,3,10,23), "oscars-2025": utc(2025,3,3,0), "oscars-2026": utc(2026,3,15,23)}
E = []
for cl, ids in {"oscars-2024": (903246, 903277, 903316, 903318), "oscars-2025": (17043, 17047, 17049, 17263), "oscars-2026": (50251, 50282, 50298, 50386)}.items():
    for sub, i in zip(("picture", "actor", "actress", "director"), ids): E.append(("awards", cl, sub, i, OSC[cl]))
EL = [("germany-2025", 14835, utc(2025,2,23,17)), ("canada-2025", 16430, utc(2025,4,29,0)), ("poland-2025-r2", 14874, utc(2025,6,1,19)), ("poland-2025-r1", 23936, utc(2025,5,18,19)),
      ("romania-2025-r2", 16868, utc(2025,5,18,18)), ("romania-2025-r1", 15369, utc(2025,5,4,18)), ("japan-2026", 177522, utc(2026,2,8,11)), ("japan-2025-upper", 18568, utc(2025,7,20,11)),
      ("portugal-2026-r2", 34587, utc(2026,2,8,19)), ("portugal-2026-r1", 96539, utc(2026,1,18,19)), ("skorea-2025", 22523, utc(2025,6,3,11)), ("australia-2025", 16589, utc(2025,5,3,8)),
      ("uk-2024", 10740, utc(2024,7,4,21)), ("france-2024", 11034, utc(2024,7,7,18)), ("mexico-2024", 903204, utc(2024,6,3,0)), ("taiwan-2024", 902772, utc(2024,1,13,8)),
      ("venezuela-2024", 10857, utc(2024,7,28,22)), ("czech-2025", 21616, utc(2025,10,4,12)), ("ecuador-2025", 14882, utc(2025,4,13,22)), ("chile-2025-r1", 35723, utc(2025,11,16,21)),
      ("bolivia-2025-r1", 32586, utc(2025,8,18,0)), ("honduras-2025", 74717, utc(2025,11,30,23)), ("hungary-2026", 106614, utc(2026,4,12,17)), ("colombia-2026-r2", 34584, utc(2026,6,21,21)),
      ("colombia-2026-r1", 34582, utc(2026,5,31,21)), ("argentina-2025", 17819, utc(2025,10,26,21))]
for cl, i, T in EL: E.append(("intl_election", cl, "win", i, T))
GG25, GG26, BA25, BA26, SAG25 = utc(2025,1,6,1), utc(2026,1,12,1), utc(2025,2,16,19), utc(2026,2,22,19), utc(2025,2,24,1)
EXTRA = [("awards", "goldenglobes-2025", "actordrama", 16271, GG25), ("awards", "goldenglobes-2025", "actormusical", 15629, GG25), ("awards", "goldenglobes-2025", "actressdrama", 15626, GG25),
         ("awards", "goldenglobes-2026", "actordrama", 57149, GG26), ("awards", "goldenglobes-2026", "actressdrama", 58950, GG26), ("awards", "goldenglobes-2026", "director", 57767, GG26), ("awards", "goldenglobes-2026", "actormusical", 58979, GG26),
         ("awards", "bafta-2025", "actor", 18406, BA25), ("awards", "bafta-2025", "actress", 18411, BA25), ("awards", "bafta-2025", "director", 18419, BA25),
         ("awards", "bafta-2026", "actress", 154000, BA26), ("awards", "bafta-2026", "director", 153823, BA26),
         ("awards", "sag-2025", "actor", 18393, SAG25), ("awards", "sag-2025", "actress", 18395, SAG25)]
if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if only == "extra": E = EXTRA
    out = []
    for cat, cl, sub, eid, T in E:
        if only and only != "extra" and cat != only: continue
        ev = pm_event(eid); ev = ev[0] if isinstance(ev, list) else ev
        ms = []
        for mk in ev["markets"]:
            try: tok, win = yes_token_and_win(mk)
            except Exception: continue
            ct = mk.get("closedTime")
            if ct:
                try: cts = datetime.fromisoformat(ct.replace("Z", "+00:00") if "+" not in ct else ct + (":00" if ct[-3] != ":" else "")).timestamp()
                except Exception: cts = None
                if cts and cts < T - 3600: continue
            ms.append((float(mk.get("volume") or 0), mk, tok, win))
        ms.sort(key=lambda x: -x[0])
        for vol, mk, tok, win in ms[:8]:
            h = pm_hist(tok, T)
            nm = mk.get("groupItemTitle") or mk["question"]
            out.append(dict(category=cat, cluster=cl, contract=f"{cl}-{sub}-{slug(nm)}", token=tok, T=T, outcome=win, question=mk["question"], volume=vol, n=len(h),
                            p10=price_at(h, T - 10 * DAY), plast=h[-1]["p"] if h else None, eid=eid))
        print(cl, sub, [(o["contract"].rsplit("-", 1)[-1][:12], o["n"], o["p10"], o["outcome"]) for o in out if o["cluster"] == cl and o["contract"].startswith(f"{cl}-{sub}-")][:8], flush=True)
        json.dump(out, open(os.path.join(RAW, "pm_candidates_other%s.json" % ("_" + only if only else "")), "w"))
