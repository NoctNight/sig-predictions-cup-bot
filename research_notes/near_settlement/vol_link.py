"""Does volatility itself bring convergence? Gaps sorted by how much the market moved over the same 24 hours, on SIG and on Polymarket-Kalshi."""
import sqlite3, numpy as np, pandas as pd
OUT = "data/research_near_settlement"
E = pd.read_pickle(f"{OUT}/sig_entries.pkl")
E = E[E.kind == "fav_loose"].dropna(subset=["gap24"]).copy()
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
v = pd.read_sql_query("select ts,exchange_id,sig_bid,sig_ask,fair from venue_fair where exchange_id is not null and sig_bid is not null and sig_ask is not null and fair is not null order by ts", db)
v["b"] = (v.ts // 3600).astype("int64") * 3600
g = v.groupby(["b", "exchange_id"]).last().reset_index()
g["mid"] = (g.sig_bid + g.sig_ask) / 2
series = {k: d.set_index("b") for k, d in g.groupby("exchange_id")}
fv, sv = [], []
for _, r in E.iterrows():
    d = series[r.ex]; w = d.loc[r.t:r.t + 86400]
    fv.append(np.sqrt((w.fair.iloc[::4].diff() ** 2).sum()) if len(w) > 8 else np.nan)
    sv.append(np.sqrt((w.mid.iloc[::4].diff() ** 2).sum()) if len(w) > 8 else np.nan)
E["fair_vol"], E["sig_vol"] = fv, sv
E = E.dropna(subset=["fair_vol", "sig_vol"])
rows = []
for name, col in (("outside fair's swing", "fair_vol"), ("SIG's own swing", "sig_vol")):
    E["q"] = pd.qcut(E[col], 3, labels=["quiet", "middle", "busy"], duplicates="drop")
    for k, q in E.groupby("q", observed=True):
        rows.append(dict(venue="SIG", sorted_on=name, third=k, n=len(q), races=q.race.nunique(), vol_c=100 * q[col].mean(), gap_c=100 * q.gap0.mean(), close=100 * (1 - q.gap24.mean() / q.gap0.mean()), up=100 * q.dmid24.mean(), fair=100 * q.df24.mean()))
s = E.gap0.abs() > .005
rho = np.corrcoef(E.fair_vol[s], (1 - E.gap24[s] / E.gap0[s]).clip(-2, 2))[0, 1]
print("SIG: correlation of the outside fair's swing with closure", round(rho, 3))
# Polymarket-Kalshi: rebuild the pairs from the panel
P, M = pd.read_pickle(f"{OUT}/panel.pkl"), pd.read_pickle(f"{OUT}/legs.pkl")
M = M[M.group == "election"]
ev = []
for _, k in M[M.venue == "kalshi"].iterrows():
    c = M[(M.venue == "polymarket") & (M.race == k.race)]
    same = c[c.party == k.party]
    flip = same.empty
    if flip:
        c = c[c.party.isin(["D", "R"])]
        if c.empty or k.party not in ("D", "R"):
            continue
        a = c.sort_values("vol", ascending=False).iloc[0]
    else:
        a = same.sort_values("vol", ascending=False).iloc[0]
    x = P[P.key == a.key].set_index("h").p; y = P[P.key == k.key].set_index("h").p
    d = pd.DataFrame({"a": 1 - x if flip else x, "b": y}).reindex(range(-14 * 24, 1)).ffill(limit=3).dropna()
    if len(d) < 72:
        continue
    if ((d.a + d.b) / 2).loc[d.index <= -24].median() < .5:
        d = 1 - d
    gap = (d.a - d.b).rolling(6).mean()
    low, hi = (d.a - d.b).rolling(6).min(), (d.a - d.b).rolling(6).max()
    midp = (d.a + d.b) / 2
    for t in range(-14 * 24 + 6, -24, 6):
        if t not in gap.index or t + 24 not in gap.index or np.isnan(gap[t]) or np.isnan(gap[t + 24]):
            continue
        sg = np.sign(gap[t])
        if not (low[t] >= .01 if sg > 0 else hi[t] <= -.01):
            continue
        if not .80 <= midp[t] <= .96:
            continue
        w = midp.loc[t:t + 24].iloc[::4]
        ev.append(dict(race=k.race, tau=-t / 24, gap=abs(gap[t]), g1=sg * gap[t + 24], vol=np.sqrt((w.diff() ** 2).sum())))
X = pd.DataFrame(ev)
X["q"] = pd.qcut(X.vol, 3, labels=["quiet", "middle", "busy"])
for k, q in X.groupby("q", observed=True):
    rows.append(dict(venue="Polymarket-Kalshi", sorted_on="the contract's swing", third=k, n=len(q), races=q.race.nunique(), vol_c=100 * q.vol.mean(), gap_c=100 * q.gap.mean(), close=100 * (1 - q.g1.mean() / q.gap.mean())))
T = pd.DataFrame(rows); T.to_pickle(f"{OUT}/vol_link.pkl")
print(T.round(2).to_string(index=False))
