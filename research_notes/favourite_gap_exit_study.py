"""Do 90-92c favourites under the outside fair by 4c+ close that gap before the result? Sizhe 31 Oct: buy them, sell by 2 Nov.
Entry at the side's ask when price is 0.88-0.93 and outside fair is 4c+ higher, once per leg per day; exit at the side's bid H hours later."""
import sqlite3, numpy as np, pandas as pd
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
v = pd.read_sql_query("select ts,exchange_id,race,sig_bid,sig_ask,pm,kalshi,fair from venue_fair where exchange_id is not null and sig_bid is not null and sig_ask is not null and fair is not null order by ts", db)
print(len(v), pd.Timestamp(v.ts.min(), unit="s"), pd.Timestamp(v.ts.max(), unit="s"))
v["b"] = (v.ts // 1800).astype("int64") * 1800
g = v.groupby(["b", "exchange_id"]).last().reset_index()
H = [6, 12, 24, 48, 72, 96]
rows = []
for ex, d in g.groupby("exchange_id"):
    d = d.sort_values("b").set_index("b")
    for side in ("yes", "no"):
        px = d.sig_ask if side == "yes" else 1 - d.sig_bid
        out = d.sig_bid if side == "yes" else 1 - d.sig_ask
        f = d.fair if side == "yes" else 1 - d.fair
        last = -1e18
        for t in d.index[((px >= .88) & (px <= .93) & (f - px >= .04)).values]:
            if t - last < 86400: continue
            last = t
            r = dict(ex=ex, side=side, t=t, race=d.race.loc[t], px=px.loc[t], f=f.loc[t], gap0=f.loc[t] - px.loc[t])
            for h in H:
                tt = t + h * 3600
                if tt in d.index:
                    r[f"net{h}"] = out.loc[tt] - px.loc[t]
                    r[f"gap{h}"] = f.loc[tt] - px.loc[tt]
            rows.append(r)
E_ = pd.DataFrame(rows)
E_.to_pickle("data/research_diralpha/fav_gap.pkl")
print("entries", len(E_), "legs", E_.ex.nunique(), "races", E_.race.nunique(), "mean gap at entry %.3f" % E_.gap0.mean())
print("%4s %5s %6s %6s %6s %6s %6s" % ("h", "n", "net c", "se c", "win", "gap c", "close%"))
tab = []
for h in H:
    q = E_.dropna(subset=[f"net{h}"])
    race = q.groupby("race")[f"net{h}"].mean()
    n, m, se = len(q), 100 * q[f"net{h}"].mean(), 100 * race.std() / max(1, np.sqrt(len(race)))
    gp = 100 * q[f"gap{h}"].mean(); cl = 100 * (1 - q[f"gap{h}"].mean() / q.gap0.mean())
    print("%4d %5d %6.2f %6.2f %6.2f %6.2f %6.1f" % (h, n, m, se, (q[f"net{h}"] > 0).mean(), gp, cl))
    tab.append(dict(h=h, n=n, net=m, se=se, win=(q[f"net{h}"] > 0).mean(), gap=gp, close=cl))
pd.DataFrame(tab).to_pickle("data/research_diralpha/fav_gap_tab.pkl")
E_["day"] = pd.to_datetime(E_.t, unit="s").dt.strftime("%m-%d")
print(E_.groupby("day").agg(n=("ex", "size"), gap=("gap0", "mean"), net24=("net24", "mean"), net48=("net48", "mean")).round(3).to_string())
