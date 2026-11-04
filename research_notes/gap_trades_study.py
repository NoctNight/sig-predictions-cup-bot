"""Gap-trade measurement (27 Oct): enter at the SIG touch when |fair - touch| >= 3c, exit at the SIG touch h hours later.
Read-only on data/sigbot.sqlite. Split IS/OOS at SPLIT; t clustered by race (race means). Pure python."""
import sqlite3, json, math, sys
from collections import defaultdict
sys.path.insert(0, ".")
from sigbot.correlation_model import race_state
from sigbot.proposals.cheap_favourites import FAST_CLOSE_ET
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
rows = db.execute("select ts,race,party,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where fair is not null and sig_bid is not null "
                  "and sig_ask is not null and sig_ask>sig_bid order by race,party,ts").fetchall()
ser = defaultdict(list)
for r in rows: ser[(r[1], r[2])].append(r)
T0, T1 = rows[0][0], max(r[0] for r in rows)
SPLIT = T0 + 0.5 * (T1 - T0)
MIN = 0.03
HS = [24, 48, 96]
def stats(xs):
    by = defaultdict(list)
    for race, v in xs: by[race].append(v)
    m = [sum(v) / len(v) for v in by.values()]
    n = len(m)
    if n < 3: return dict(n=n, mean=None)
    mu = sum(m) / n; sd = math.sqrt(sum((x - mu) ** 2 for x in m) / (n - 1))
    return dict(n=n, trades=len(xs), mean=round(mu, 4), t=round(mu / (sd / math.sqrt(n)), 2) if sd else None)
out = {}
for grp in ("all", "fast", "early"):
    for half in ("IS", "OOS"):
        for side in ("yes", "no"):
            for h in HS:
                net, gross, close, edge_l = [], [], [], []
                for (race, party), s in ser.items():
                    st = race_state(race)
                    if grp == "fast" and st not in FAST_CLOSE_ET: continue
                    if grp == "early" and not (st in FAST_CLOSE_ET and FAST_CLOSE_ET[st] <= 21.0): continue
                    ts = [r[0] for r in s]
                    last_entry = -1e18
                    j = 0
                    for i, r in enumerate(s):
                        t, pm, k, fair, bid, ask = r[0], r[3], r[4], r[5], r[6], r[7]
                        if (t >= SPLIT) != (half == "OOS") or t - last_entry < 86400: continue
                        if pm is not None and k is not None and abs(pm - k) > 0.04: continue
                        e = fair - ask if side == "yes" else bid - fair
                        if e < MIN: continue
                        tt = t + h * 3600
                        if tt > T1: continue
                        while j < len(s) - 1 and s[j][0] < tt: j += 1
                        if s[j][0] < tt or s[j][0] - tt > 3600: continue
                        x = s[j]
                        if side == "yes":
                            nv = x[6] - ask; gv = (x[6] + x[7]) / 2 - (bid + ask) / 2; cl = ((x[6] + x[7]) / 2 - (bid + ask) / 2) / e
                        else:
                            nv = bid - x[7]; gv = (bid + ask) / 2 - (x[6] + x[7]) / 2; cl = ((bid + ask) / 2 - (x[6] + x[7]) / 2) / e
                        net.append((race, nv)); gross.append((race, gv)); close.append((race, cl)); edge_l.append(e)
                        last_entry = t
                if net:
                    out[f"{grp}|{half}|{side}|{h}h"] = dict(net=stats(net), gross=stats(gross), closed_frac=stats(close),
                                                          avg_edge=round(sum(edge_l) / len(edge_l), 4))
json.dump(out, open("data/research_gap/study.json", "w"), indent=1)
for k, v in out.items():
    print(k, "net", v["net"].get("mean"), "t", v["net"].get("t"), "n", v["net"]["n"], "| gross", v["gross"].get("mean"),
          "| closed", v["closed_frac"].get("mean"), "| edge", v["avg_edge"])
