"""Gap trades by entry day (27 Oct): same rule as gap_trades_study.py (3c, PM/Kalshi within 4pts, 1 entry/leg/24h), no IS/OOS split
(no fitted parameter). Net = exit at the SIG touch h later. Per entry day: mean net (c), n trades, n races; t clustered by race overall."""
import sqlite3, datetime
from collections import defaultdict
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
rows = db.execute("select ts,race,party,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where fair is not null and sig_bid is not null "
                  "and sig_ask is not null and sig_ask>sig_bid order by race,party,ts").fetchall()
ser = defaultdict(list)
for r in rows: ser[(r[1], r[2])].append(r)
T1 = max(r[0] for r in rows)
for h in (24, 48, 72):
    byday = defaultdict(list)
    for (race, party), s in ser.items():
        for side in ("yes", "no"):
            last, j = -1e18, 0
            for r in s:
                t, pm, k, fair, bid, ask = r[0], r[3], r[4], r[5], r[6], r[7]
                if t - last < 86400 or (pm is not None and k is not None and abs(pm - k) > 0.04): continue
                if (fair - ask if side == "yes" else bid - fair) < 0.03: continue
                tt = t + h * 3600
                if tt > T1: break
                while j < len(s) - 1 and s[j][0] < tt: j += 1
                if s[j][0] < tt or s[j][0] - tt > 3600: continue
                x = s[j]
                nv = x[6] - ask if side == "yes" else bid - x[7]
                byday[datetime.datetime.utcfromtimestamp(t).strftime("%d %b")].append((race, nv)); last = t
    print(f"--- {h} h")
    allx = []
    for d in sorted(byday, key=lambda d: datetime.datetime.strptime(d + " 2026", "%d %b %Y")):
        v = byday[d]; allx += v
        print(d, f"net {100*sum(x for _,x in v)/len(v):+.2f}c  trades {len(v)}  races {len({r for r,_ in v})}")
