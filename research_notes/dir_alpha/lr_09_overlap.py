from lr_common import *
L, D, tp = load(); Z = pd.read_pickle("research_notes/dir_alpha/lr_sets.pkl"); dA, dB = D["A"], D["B"]
Sa = pd.read_pickle(O + "slow24.pkl"); Fa = pd.read_pickle(O + "fast6.pkl"); Ss = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Fs = pd.read_pickle("data/research_diralpha/strict_fast.pkl")
Ra, _ = residual(dA); qa = book_ctx(L, dB); C, Rc, okc = clean(L, dB, qa)
def ov(lab, E, Lag, race, party, W=864):
    Lg = Lag.copy(); Lg["race"] = race[Lg.j.values]; pty = party[Lg.j.values]; Lg["ds"] = np.where(pty == "D", Lg.s, np.where(pty == "R", -Lg.s, 0))
    ds = dside(E); hit = np.zeros(len(E), bool); hit_any = np.zeros(len(E), bool); e_t = E.t.values
    for i, (r, d, t) in enumerate(zip(E.race.values, ds, e_t)):
        g = Lg[(Lg.race == r) & (Lg.ds == d)]
        hit_any[i] = ((g.t.values - t <= W) & (g.t.values - t >= -W)).any()
        hit[i] = ((g.t.values <= t) & (g.t.values >= t - 288)).any()       # a lag signal in the same direction in the prior 24 h: holds at the same time
    # lag trades that are also line trades
    q = Lg.copy(); ld = np.zeros(len(q), bool)
    for i, (r, d, t) in enumerate(zip(q.race.values, q.ds.values, q.t.values)):
        h = (E.race.values == r) & (ds == d) & (np.abs(E.t.values - t) <= W); ld[i] = h.any()
    print(f"{lab}: {len(E)} line trades; same race and direction with a lag signal within +-72 h: {hit_any.mean():.2f}, lag signal in prior 24 h: {hit.mean():.2f}; net overlap {100*E.n[hit_any].mean():.2f}c ({hit_any.sum()}), no overlap {100*E.n[~hit_any].mean():.2f}c ({(~hit_any).sum()}) | of {len(q)} lag trades {ld.mean():.2f} are near a line trade")
    return hit_any
print("-- lag")
a = ov("A stated vs slow+fast", Z["A2"], pd.concat([Sa, Fa]), dA["race"], dA["party"]); a1 = ov("A oos8 vs slow+fast", Z["A1"], pd.concat([Sa, Fa]), dA["race"], dA["party"])
b = ov("B clean vs strict", Z["C"], pd.concat([Ss, Fs]), dB["race"], dB["party"]); b1 = ov("B oos8 vs strict", Z["B1"], pd.concat([Ss, Fs]), dB["race"], dB["party"]); 
print("stats no-overlap: A stated", st(Z["A2"][~a]), "B clean", st(Z["C"][~b]), "\nstats overlap: A", st(Z["A2"][a]), "B", st(Z["C"][b]))
# Excluding lag-driven: outside moved in prior 24h on the side
# Cheap favourites: fair of bought side >=.8, edge >=2c, entries from 24 Oct. Share of line buys in same territory
for lab, E, d, Mx in (("A stated", Z["A2"], dA, dA["S"].values), ("B clean", Z["C"], dB, (dB["book"][0] + dB["book"][1]) / 2), ("A oos8", Z["A1"], dA, dA["S"].values), ("B oos8", Z["B1"], dB, dB["S"].values)):
    F = ((d["P"] + d["K"]) / 2).values; t, j, s = E.t.values, E.j.values, E.s.values; ownF = np.where(s > 0, F[t, j], 1 - F[t, j]); ownM = np.where(s > 0, Mx[t, j], 1 - Mx[t, j]); edge = ownF - ownM
    cf = (ownF >= .8) & (edge >= .02)
    print(lab, f"cheapfav territory (own outside fair>=80c, 2c+ under): {cf.mean():.2f}, net {100*E.n.values[cf].mean() if cf.any() else float('nan'):.2f}c ({cf.sum()}), rest {100*E.n.values[~cf].mean():.2f}c | own side <50c {(ownM<.5).mean():.2f} net {100*E.n.values[ownM<.5].mean():.2f}c; own >=50 net {100*E.n.values[ownM>=.5].mean():.2f}c | own side >=70c {(ownM>=.7).mean():.2f} net {100*E.n.values[ownM>=.7].mean():.2f}c")
# Gap: is it the same as buying where outside is above SIG by any amount (gap with k=1)? rich/cheap vs raw gap
for lab, E, d, Mx in (("A stated", Z["A2"], dA, dA["S"].values), ("B clean", Z["C"], dB, (dB["book"][0] + dB["book"][1]) / 2)):
    F = ((d["P"] + d["K"]) / 2).values; t, j, s = E.t.values, E.j.values, E.s.values; g = s * (F[t, j] - Mx[t, j]); print(lab, "raw gap outside-SIG on our side: mean %.2fc, share 0-or-negative %.2f, net when raw gap <1c %.2f (%d), >=1c %.2f" % (100 * g.mean(), (g <= 0).mean(), 100 * E.n.values[g < .01].mean() if (g < .01).any() else np.nan, (g < .01).sum(), 100 * E.n.values[g >= .01].mean()))
    # the level model: price buckets of the residual (is the line a good fit?)
print("-- persistent residual by price bucket (B mid, all legs, mean signed residual)")
b0, a0 = dB["book"]; m0 = (b0 + a0) / 2; v = pd.DataFrame(dict(m=m0[okc], r=Rc[okc])).dropna(); print(v.groupby(pd.cut(v.m, [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1]), observed=True).r.agg(n="size", mean=lambda x: round(100 * x.mean(), 2), sd=lambda x: round(100 * x.std(), 2)).T.to_string())
print("-- overlap, label from the past only")
def past(E, Lag, race, party, W):
    Lg = Lag.copy(); Lg["race"] = race[Lg.j.values]; pty = party[Lg.j.values]; Lg["ds"] = np.where(pty == "D", Lg.s, np.where(pty == "R", -Lg.s, 0)); ds = dside(E); h = np.zeros(len(E), bool); fut = h.copy()
    for i, (r, d, t) in enumerate(zip(E.race.values, ds, E.t.values)):
        g = Lg[(Lg.race == r) & (Lg.ds == d)].t.values
        h[i] = ((g <= t) & (g >= t - W)).any(); fut[i] = ((g > t) & (g <= t + W)).any()
    return h, fut
for lab, E, Lag, d in (("A stated", Z["A2"], pd.concat([Sa, Fa]), dA), ("B clean", Z["C"], pd.concat([Ss, Fs]), dB), ("B stated", Z["B2"], pd.concat([Ss, Fs]), dB)):
    for W in (288, 864):
        h, fut = past(E, Lag, d["race"], d["party"], W)
        print(f"{lab} W {W//12}h: lag in past: {st(E[h])} | none in past: {st(E[~h])} | none in past, lag fires later: {st(E[~h & fut])} | neither: {st(E[~h & ~fut])}")
