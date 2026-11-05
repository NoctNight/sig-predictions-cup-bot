"""Fetch Gamma events (cached) for the comparison-group study. Usage: others_pm_events.py id1 id2 ..."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import net
RAW = os.path.join(net.OUT, "others", "raw")
def event(eid):
    return net.cached(os.path.join(RAW, f"ev_{eid}.json"), lambda: (time.sleep(.35), net.curl(net.GAMMA + "/events", {"id": eid})[1])[1])
if __name__ == "__main__":
    for eid in sys.argv[1:]:
        d = event(eid)
        e = d[0] if isinstance(d, list) and d else d
        print(eid, e["title"], e.get("endDate"))
        for m in e["markets"]:
            print("   ", m["id"], "|", m.get("groupItemTitle"), "|", m["question"][:70], "|", m.get("outcomePrices"), "|", round(float(m.get("volume") or 0)), "|", m.get("endDate"), m.get("closedTime"))
