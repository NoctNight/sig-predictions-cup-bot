"""Survivorship, tape sanity, own footprint, double counting, independence, decomposition of the P&L, timing placebo."""
import sys; sys.path.insert(0, "research_notes/dir_alpha")
import numpy as np, pandas as pd
import audit_common as c
pd.set_option("display.width", 220, "display.max_columns", 40, "display.max_rows", 200)
G = c.Grid(); sf = G.flow(); tp = G.tape; M = G.masks(sf)
d = G.trades(M["full"])
def r(n, x):
    day = x.groupby("day").net.mean()
    print(f"{n:52s} n {len(x):4d} races {x.race.nunique():3d} net {100*x.net.mean():6.2f}c win {100*(x.net>0).mean():4.0f}% days+ {(day>0).sum()}/{len(day)} worst {100*x.net.min():.1f}")
print("\n--- SURVIVORSHIP")
a = G.trades(M["full"], need_exit=False)
print("signals consumed if a missing exit does not skip:", len(a), "dropped for missing exit quote:", int(a.dropped.sum()))
# entries whose exit would be after the end of the panel: mark at last available quote
v = G.S48.values; B, A = G.bid.values, G.ask.values; n = len(G.idx)
late = []
for j in range(v.shape[1]):
    prev = d[d.j == j].i.max() if (d.j == j).any() else -10**9
    nxt = prev + c.HOLD
    for i in np.flatnonzero((np.abs(v[:, j]) >= c.TH) & M["full"][:, j]):
        if i < nxt or i + c.HOLD < n: continue
        s = np.sign(v[i, j]); y = n - 1
        while y > i and not np.isfinite(B[y, j]): y -= 1
        late.append(dict(j=j, i=i, s=s, race=G.race[j], t=G.t[i], hours=(y - i) / 12, net=(B[y, j] - A[i, j]) if s > 0 else (B[i, j] - A[y, j])))
        nxt = i + c.HOLD
L = pd.DataFrame(late)
print("signals in the last 24 h cut off by the panel end:", len(L), "marked at last touch, mean net c", round(100 * L.net.mean(), 2) if len(L) else None, "mean hours", round(L.hours.mean(), 1) if len(L) else None)
print(L.groupby("race").net.mean().round(3).head(12).to_dict() if len(L) else "")
vf_last = c.raw("vf").groupby("exchange_id").ts.max()
print("legs whose last poll is > 6 h before panel end:", int((vf_last < vf_last.max() - 21600).sum()))
print("legs in signal universe (band, not chamber) ever:", int(M["base"].any(axis=0).sum()), "of", M["base"].shape[1])

print("\n--- TAPE SANITY")
mid = G.mid
tt = tp[(tp.ts >= G.idx[0]) & (tp.b.isin(G.idx))].copy()
ix = pd.Series(np.arange(len(G.idx)), index=G.idx); cx = pd.Series(np.arange(len(G.cols)), index=G.cols)
tt = tt[tt.exchange_id.isin(G.cols)]
mv = mid.values[ix[tt.b].values, cx[tt.exchange_id].values]
dv = np.abs(tt.price.values - mv)
print("prints > 5c from mid:", round(np.nanmean(dv > .05), 4), " > 15c:", round(np.nanmean(dv > .15), 4), " 1-p closer than p:", round(np.nanmean(np.abs(1 - tt.price.values - mv) < dv), 4))
# outages: tape rate
polls = np.sort(c.raw("vf").ts.unique()); gaps = [(a_, b_) for a_, b_ in zip(polls[:-1], polls[1:]) if b_ - a_ > 1800]
ing = tp.ingested_at - tp.ts
out = np.zeros(len(tp), bool)
for a_, b_ in gaps: out |= ((tp.ts > a_) & (tp.ts < b_)).values
span = polls[-1] - polls[0]; dn = sum(b_ - a_ for a_, b_ in gaps)
print(f"outage time share {dn/span:.3f}; tape rows/h up {((~out)&(tp.ts>polls[0])).sum()/((span-dn)/3600):.0f}, down {out.sum()/(dn/3600):.0f}; legs with prints during outages {tp[out].exchange_id.nunique()} (of {tp.exchange_id.nunique()})")
print("aggressor null share in outages", round(tp.aggressor[out].isna().mean(), 3), " tick-test vs quote can't be split post hoc")
# flow threshold is it binding?
print("flow sensitivity:")
fl = sf.rolling(c.HOLD, min_periods=1).sum(); s = np.sign(G.S48)
for thr in (-1000, -2500, -5000, -10000, -25000):
    r(f"  both + flow >= {thr}", G.trades(M["base"] & M["both"] & (fl * s >= thr).values))

print("\n--- OWN FOOTPRINT")
f = c.raw("fills"); f["ts"] = (pd.to_datetime(f.filled_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
print(tp.q.sum(), f.quantity.abs().sum())
# yes-equivalent sign: side yes + qty>0 = long yes; side no = long no. quantity sign may mean sell
print(f.groupby(["side", np.sign(f.quantity)]).size().to_dict())
f["yes_q"] = np.where(f.side == "yes", 1, -1) * f.quantity
q = c.raw("quotes"); quoted = q.groupby("exchange_id").ts.agg(["min", "max"])
rows = []
polls_ = G.P["ts"].values
for k in d.itertuples():
    t0, t1 = G.idx[k.x], G.idx[k.y] + 300
    w = f[(f.exchange_id == k.ex) & (f.ts >= t0 - 3600) & (f.ts <= t1)]
    nm = w[w.purpose != "mm"]
    tv = tp[(tp.exchange_id == k.ex) & (tp.ts >= t0) & (tp.ts <= t1)]
    qq = q[(q.exchange_id == k.ex) & (q.ts >= t0 - 300) & (q.ts <= t0 + 60)]
    rows.append(dict(own_q=w.yes_q.sum() * k.s, own_abs=w.quantity.abs().sum(), own_nonmm=nm.yes_q.sum() * k.s, tape=tv.q.sum(), quoted=len(qq) > 0, purposes=",".join(sorted(set(w.purpose.dropna())))))
F = pd.DataFrame(rows, index=d.index); d = d.join(F)
d["own_share"] = d.own_abs / d.tape.replace(0, np.nan)
print("legs with any own fill in [entry-1h, exit]:", int((d.own_abs > 0).sum()), " with own share of tape > 10%:", int((d.own_share > .1).sum()), " median share", round(d.own_share.median(), 4))
print("signed own qty in trade direction, mean", round(d.own_q.mean(), 1), "(positive = we traded the same way)")
r("no own fill in window", d[d.own_abs == 0]); r("any own fill", d[d.own_abs > 0]); r("no non-mm own fill", d[d.own_nonmm == 0]); r("non-mm own fill, same direction", d[d.own_nonmm * 1 > 0]); r("non-mm own fill, opposite", d[d.own_nonmm < 0])
r("we had a quote row on the leg at entry", d[d.quoted]); r("no quote row on the leg at entry (not market-making it)", d[~d.quoted])
print(d[d.purposes != ""].purposes.value_counts().head(12).to_string())
# signal's dependence on hour bot was running: fraction of hold in outage
ov = []
for k in d.itertuples():
    t0, t1 = G.idx[k.x], G.idx[k.y]
    ov.append(sum(max(0, min(b_, t1) - max(a_, t0)) for a_, b_ in gaps) / (t1 - t0))
d["down"] = ov
r("hold had < 5% bot downtime", d[d.down < .05]); r("hold had >= 5% bot downtime", d[d.down >= .05])

print("\n--- DOUBLE COUNTING")
d = d.sort_values("x")
d["dside"] = d.s * np.where(d.party == "D", 1, np.where(d.party == "R", -1, 0))
keep, last = [], {}
for k in d.itertuples():
    if k.race in last and k.x - last[k.race] < c.HOLD: continue
    keep.append(k.Index); last[k.race] = k.x
u = d.loc[keep]
r("first trade per race per 24 h", u)
# pair-merge: same race overlapping legs average in place of first
g = (d.assign(ep=d.groupby("race").x.transform(lambda z: (z.diff().fillna(0) >= c.HOLD).cumsum()))).groupby(["race", "ep"]).agg(net=("net", "mean"), n=("net", "size"), t=("t", "first"), day=("day", "first"), ds=("dside", "first"))
print("race episodes:", len(g), "net", round(100 * g.net.mean(), 2), "win", round((g.net > 0).mean(), 2), "legs per ep", g.n.value_counts().to_dict())
print("mirror pairs share of trades:", round((g.n[g.n > 1].sum()) / g.n.sum(), 2))
print("by party direction:", u.groupby("dside").net.agg(["size", lambda x: 100 * x.mean()]).round(2).to_dict("index"))
print("by day D-direction:"); print(u.groupby("day").dside.agg(["size", "mean"]).round(2).T.to_string())
# national factor: D fair move over 24h from each entry, mean over 2-leg races
two = pd.Series(G.race).map(pd.Series(G.race).value_counts()).values == 2
Dm = np.where(G.party == "D", 1.0, np.where(G.party == "R", -1.0, 0.0))
nat_fair = (G.fair.values * Dm)[:, two & ~G.chamber & (Dm != 0)]
nat_mid = (G.mid.values * Dm)[:, two & ~G.chamber & (Dm != 0)]
fair_d = pd.Series(np.nanmean(nat_fair, axis=1), index=G.idx); mid_d = pd.Series(np.nanmean(nat_mid, axis=1), index=G.idx)
print("national D mean (half the sum of signed prices) daily at 00:00:"); print(pd.DataFrame({"fair": fair_d, "mid": mid_d}).iloc[::288].round(4).T.to_string())
u = u.assign(natf=(fair_d.values[np.minimum(u.y, n - 1)] - fair_d.values[u.x]) * u.dside, natm=(mid_d.values[np.minimum(u.y, n - 1)] - mid_d.values[u.x]) * u.dside)
print("trade-aligned national fair move over hold c", round(100 * u.natf.mean(), 2), " national mid move", round(100 * u.natm.mean(), 2), " corr(net, natm)", round(u.net.corr(u.natm), 2))
u["res"] = u.net - u.natm
print("net after removing national mid move c:", round(100 * u.res.mean(), 2), " win", round((u.res > 0).mean(), 2))
# clustering / effective N
def cl(x, g_):
    m = pd.Series(x.values).groupby(np.asarray(g_)).mean()
    return m.mean() / (m.std() / np.sqrt(len(m))), len(m)
print("t iid", round(u.net.mean() / (u.net.std() / np.sqrt(len(u))), 2), " t by day", round(cl(u.net, u.day)[0], 2), cl(u.net, u.day)[1], " t by race", round(cl(u.net, u.race)[0], 2), cl(u.net, u.race)[1])
state = u.race.str.replace(r"-\d\d House race| Senate| Governor", "", regex=True)
print("states:", state.nunique())
sets = u.sort_values("x")
# greedy 12 h episode clusters by D-direction: trades in one direction within 12 h = one factor bet
ep, cur, lastx = [], {1: 0, -1: 0, 0: 0}, {1: -10**9, -1: -10**9, 0: -10**9}; nx = 0
for k in sets.itertuples():
    ds = int(k.dside)
    if k.x - lastx[ds] > 144:
        nx += 1; cur[ds] = nx
    lastx[ds] = k.x; ep.append(cur[ds])
sets["ep"] = ep
E_ = sets.groupby("ep").agg(n=("net", "size"), net=("net", "mean"), t=("t", "first"), ds=("dside", "first"), rc=("race", "nunique"))
print("direction-time clusters (12 h):", len(E_), " size dist", E_.n.describe()[["mean", "50%", "max"]].round(1).to_dict(), " cluster net c", round(100 * E_.net.mean(), 2), " win", round((E_.net > 0).mean(), 2))
print(" t over clusters", round(E_.net.mean() / (E_.net.std() / np.sqrt(len(E_))), 2))
# effective N: pairwise correlation of 24h mid changes across traded legs in D terms (hourly-sampled, overlapping)
ch = (G.mid * Dm).diff(c.HOLD).iloc[::12]
sel = sorted(set(u.j)); C = ch.iloc[:, sel].corr().values
iu = np.triu_indices_from(C, 1); rho = np.nanmean(C[iu])
print("mean pairwise corr of 24 h D-signed mid moves across traded legs:", round(rho, 3))
hold_overlap = []
xs = u.x.values; ys = u.y.values; sg = (u.dside.values)
ov_w = np.zeros((len(u), len(u)))
for a_ in range(len(u)):
    ov_w[a_] = np.clip(np.minimum(ys[a_], ys) - np.maximum(xs[a_], xs), 0, None) / c.HOLD
Cj = ch.iloc[:, u.j.values].corr().fillna(0).values
S_ = ov_w * Cj * np.outer(sg, sg) + 0.0; np.fill_diagonal(S_, 1)
Neff = len(u) ** 2 / S_.sum()
print("effective bets using overlap-scaled |corr| of hold windows, signed:", round(Neff, 1))
Sabs = ov_w * np.abs(Cj); np.fill_diagonal(Sabs, 1); print(" same, unsigned correlation:", round(len(u) ** 2 / Sabs.sum(), 1))
# empirical variance-ratio N: use day-block bootstrap se vs iid se
rng = np.random.default_rng(0); days = u.day.unique(); bs = []
for _ in range(2000):
    pick = rng.choice(days, len(days)); bs.append(np.concatenate([u.net.values[u.day.values == x_] for x_ in pick]).mean())
se = np.std(bs); print("day-block bootstrap: mean", round(100 * np.mean(bs), 2), "se", round(100 * se, 2), "95%", np.round(100 * np.quantile(bs, [.025, .975]), 2), " Neff from SE", round((u.net.std() / se) ** 2, 1))
u.to_pickle(c.CACHE + "/dedup.pkl")

print("\n--- DECOMPOSITION")
Fa, Mi = G.fair.values, G.mid.values
u["dfair"] = u.s * (Fa[u.y, u.j] - Fa[u.x, u.j]); u["dmid"] = u.s * (Mi[u.y, u.j] - Mi[u.x, u.j])
u["gap0"] = u.s * (Fa[u.x, u.j] - Mi[u.x, u.j]); u["gap1"] = u.s * (Fa[u.y, u.j] - Mi[u.y, u.j])
print(u[["dmid", "dfair", "gap0", "gap1"]].mul(100).describe().loc[["mean", "50%"]].round(2).to_string())
print("mean SIG move = closing of gap", round(100 * (u.gap0 - u.gap1).mean(), 2), "+ further outside move", round(100 * u.dfair.mean(), 2))
print("net when outside continued (dfair>0.5c):", round(100 * u.net[u.dfair > .005].mean(), 2), int((u.dfair > .005).sum()), " flat:", round(100 * u.net[u.dfair.abs() <= .005].mean(), 2), int((u.dfair.abs() <= .005).sum()), " reversed:", round(100 * u.net[u.dfair < -.005].mean(), 2), int((u.dfair < -.005).sum()))
print("share of total net P&L from top 5 / top 10 trades:", round(u.net.nlargest(5).sum() / u.net.sum(), 2), round(u.net.nlargest(10).sum() / u.net.sum(), 2))
print("biggest:", u.nlargest(6, "net")[["race", "party", "t", "net", "dfair"]].round(3).to_string())

print("\n--- TIMING PLACEBO: circular shift of signal and all filters vs P&L")
rng = np.random.default_rng(3)
S = G.S48.values; m = M["full"]
res = []
class X: pass
for it in range(100):
    k = int(rng.integers(288, len(G.idx) - 288))
    Sx = pd.DataFrame(np.roll(S, k, axis=0), index=G.idx, columns=G.cols)
    mx = np.roll(m, k, axis=0)
    res.append(G.trades(mx, S=Sx).net.mean() * 100)
res = np.array(res); print("null mean", round(np.nanmean(res), 2), "p95", round(np.nanquantile(res, .95), 2), "max", round(np.nanmax(res), 2), "reported 2.02")
