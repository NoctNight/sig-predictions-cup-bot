"""Stale data: how many of the 97 entries or exits sit on forward-filled rows or outages; does P&L survive?"""
import sys; sys.path.insert(0, "research_notes/dir_alpha")
import numpy as np, pandas as pd
import audit_common as c
G = c.Grid(); sf = G.flow(); M = G.masks(sf)
d = G.trades(M["full"])
fr = G.fresh.values; age = G.age.values
d["e_fresh"] = fr[d.x, d.j]; d["x_fresh"] = fr[d.y, d.j]
d["e_age_m"] = age[d.x, d.j] / 60; d["x_age_m"] = age[d.y, d.j] / 60
# bot outage flag from polls in any leg
polls = np.sort(raw_ts) if (raw_ts := c.raw("vf").ts.unique()) is not None else None
gaps = [(a, b) for a, b in zip(polls[:-1], polls[1:]) if b - a > 600]
def since_restart(t):
    k = np.searchsorted(polls, t, side="right") - 1
    last = polls[k]
    r = [b for a, b in gaps if b <= t]
    return (t - r[-1]) / 60 if r else 1e9
d["e_since_restart_m"] = [since_restart(t + 300) for t in G.idx[d.x]]
d["x_since_restart_m"] = [since_restart(t + 300) for t in G.idx[d.y]]
def r(n, x):
    day = x.groupby("day").net.mean()
    print(f"{n:52s} n {len(x):4d} races {x.race.nunique():3d} net {100*x.net.mean():6.2f}c win {100*(x.net>0).mean():4.0f}% days+ {(day>0).sum()}/{len(day)} worst {100*x.net.min():.1f}")
r("as reported", d)
print("entry bin no real poll:", int((~d.e_fresh).sum()), " exit bin no real poll:", int((~d.x_fresh).sum()), " either:", int((~(d.e_fresh & d.x_fresh)).sum()))
print("exit row age (min) among stale exits:", d.x_age_m[~d.x_fresh].describe().round(0).to_dict())
r("stale exit only", d[~d.x_fresh]); r("stale entry only", d[~d.e_fresh])
r("require real poll in entry and exit bins", d[d.e_fresh & d.x_fresh])
for m in (15, 30, 60):
    r(f"+ entry not within {m} min of restart", d[d.e_fresh & d.x_fresh & (d.e_since_restart_m > m)])
print(d.loc[~d.x_fresh, ["race", "party", "t", "s", "net", "e_age_m", "x_age_m"]].round(3).to_string())
# exit at the next real poll in place of the stale row
B, A = G.bid.values, G.ask.values
rows = []
for k in d.itertuples():
    y = k.y
    while y < len(G.idx) and not (fr[y, k.j] and np.isfinite(B[y, k.j])):
        y += 1
    if y >= len(G.idx): rows.append(np.nan); continue
    rows.append((B[y, k.j] - A[k.x, k.j]) if k.s > 0 else (B[k.x, k.j] - A[y, k.j]))
d["net_nextpoll"] = rows
r("exit at the next real poll", d.assign(net=d.net_nextpoll).dropna(subset=["net"]))
d.to_pickle(c.CACHE + "/d_stale.pkl")
# masks fresh at signal time in the first place, re-select entries (a stale bin cannot trigger), exit must be fresh
m2 = M["full"] & fr
e = G.trades(m2); e["xf"] = fr[e.y, e.j]; r("entry signal only on real poll, exit any", e); r("entry signal only on real poll, exit real", e[e.xf])
# whole study rebuilt with NO forward fill at all (rolling median and 24h lags as-of real polls only)
G0 = c.Grid(ffill=0)
sf0 = sf.copy()
s = G0.S48; sg = np.sign(s)
pm = G0.P["pm"]; k = G0.P["kalshi"]
# 24h lag as of the most recent real value at t-24h within 15 min, from the unfilled series
def lag(x):
    return x - x.ffill(limit=3).shift(c.HOLD)
both = ((lag(pm) * sg >= .01) & (lag(k) * sg >= .01)).values
flow = (sf.rolling(c.HOLD, min_periods=1).sum() * sg >= -5000).values
band = ((G0.mid >= .15) & (G0.mid <= .85)).values & ~G0.chamber[None, :]
e0 = G0.trades(both & flow & band, need_exit=False)
print("no-ffill signals", len(e0), "of which exit bin empty", int(e0.dropped.sum()))
r("NO ffill, exit bin must be real (drops the rest)", e0[~e0.dropped])
B0, A0 = G0.bid.values, G0.ask.values
nn = []
for k_ in e0.itertuples():
    y = k_.y
    while y < len(G0.idx) and not np.isfinite(B0[y, k_.j]): y += 1
    nn.append(np.nan if y >= len(G0.idx) else ((B0[y, k_.j] - A0[k_.x, k_.j]) if k_.s > 0 else (B0[k_.x, k_.j] - A0[y, k_.j])))
e0["net"] = nn; e0 = e0.dropna(subset=["net"])
r("NO ffill anywhere, exit at next real poll", e0)
print(e0.groupby("day").net.agg(["size", lambda x: 100*x.mean()]).round(2).T.to_string())
e0.to_pickle(c.CACHE + "/e0.pkl")
# raw nulls
vf = c.raw("vf")
print("raw rows with null sig_bid", int(vf.sig_bid.isna().sum()), "null sig_ask", int(vf.sig_ask.isna().sum()), "of", len(vf), " crossed/locked", int((vf.sig_bid >= vf.sig_ask).sum()))
vf["b"] = (vf.ts // 300).astype("int64") * 300
L = vf.sort_values("ts").drop_duplicates(["b", "exchange_id"], keep="last").set_index(["b", "exchange_id"])
nb = 0; nu = []
for k_ in d.itertuples():
    for t_ in (G.idx[k_.x], G.idx[k_.y]):
        key = (t_, k_.ex)
        if key in L.index:
            row = L.loc[key]
            if pd.isna(row.sig_bid) or pd.isna(row.sig_ask): nu.append(key)
print("one-sided raw rows at entries/exits", len(nu), nu[:6])
print("pm/k weight at entry: pm_w>0 & k_w>0 share", np.mean((G.P["pm_w"].values[d.x, d.j] > 0) & (G.P["k_w"].values[d.x, d.j] > 0)))
