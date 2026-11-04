from lr_common import *
L = Lab(); A = pickle.load(open(O + "panel.pkl", "rb"))
ofA = np.asarray(A["office"], object); ofB = np.asarray(L.office, object)
def resid(Mv, Fv, office, dis, lag=1):
    ok = np.isfinite(Mv) & np.isfinite(Fv) & (office != "chamber")[None, :] & (Fv > .03) & (Fv < .97) & dis
    n = Mv.shape[0]; k = np.full(n, np.nan)
    for t in range(0, n, 12):
        o = ok[t]
        if o.sum() > 60: k[t] = np.sum((Mv[t, o] - .5) * (Fv[t, o] - .5)) / np.sum((Fv[t, o] - .5) ** 2)
    k = pd.Series(k).ffill().rolling(72, min_periods=1).mean().shift(lag).values
    return Mv - (.5 + k[:, None] * (Fv - .5)), k
RA, ka = resid(A["S"].values, ((A["P"] + A["K"]) / 2).values, ofA, (np.abs(A["P"].values - A["K"].values) <= .04))
RB, kb = resid(L.mid.values, ((L.pm + L.k) / 2).values, ofB, (np.abs(L.pm.values - L.k.values) <= .04))
Mv = A["S"].values
Sa = pd.read_pickle(O + "slow24.pkl"); Fa = pd.read_pickle(O + "fast6.pkl"); x = np.minimum(Fa.e.values + 96, Mv.shape[0] - 1); Fa["net8"] = Fa.s * (Mv[x, Fa.j.values] - Mv[Fa.e.values, Fa.j.values]) - .0075; Fa = Fa.dropna(subset=["net8"]); Fa["n"] = Fa.net8; Sa["n"] = Sa.net
Ss = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Fs = pd.read_pickle("data/research_diralpha/strict_fast.pkl")
for q in (Ss, Fs): q["race"] = L.race[q.j.values]; q["day"] = L.day[q.t.values]; q["n"] = q.net
sets = (("slow 3-20", Sa, RA), ("fast 3-20", Fa, RA), ("slow 21-30", Ss, RB), ("fast 21-30", Fs, RB))
rng = np.random.default_rng(5)
for lab, E, R in sets:
    E = E.copy(); E["sd"] = -E.s.values * R[E.t.values, E.j.values]
    print("==", lab, "n", len(E), "net", round(100 * E.n.mean(), 2))
    print("  NaN sd", E.sd.isna().sum())
    for th in (.005, .01, .015, .02, .025, .03):
        a = E[E.sd >= th]; b = E[E.sd < th]; print(f"  sd>={th*100:.1f}c: {len(a)} {100*a.n.mean():.2f}c win {(a.n>0).mean():.2f} | rest {len(b)} {100*b.n.mean():.2f}c  diff {100*(a.n.mean()-b.n.mean()):.2f}c")
    a = E[E.sd >= .02]; b = E[~(E.sd >= .02)]
    print("  right", summary(a)); print("  rest ", summary(b))
    # permutation of the right/rest label by race-group (keeps legs of one race together) and within day
    obs = a.n.mean() - b.n.mean(); lab_ = (E.sd >= .02).values; nv = E.n.values; day = E.day.values; c = 0; N = 5000
    idx = pd.Series(np.arange(len(E))).groupby(day).groups
    for _ in range(N):
        l2 = lab_.copy()
        for g in idx.values():
            g = np.asarray(g); l2[g] = rng.permutation(l2[g])
        if l2.sum() and (~l2).sum() and nv[l2].mean() - nv[~l2].mean() >= obs: c += 1
    print(f"  diff {100*obs:.2f}c, within-day permutation p (one-sided) {(c+1)/(N+1):.3f}")
    c2 = 0
    for _ in range(N):
        l2 = rng.permutation(lab_)
        if nv[l2].mean() - nv[~l2].mean() >= obs: c2 += 1
    print(f"  plain permutation p {(c2+1)/(N+1):.3f}")
    rb = E.copy(); rb["party"] = "X"
    # one row per race and day for the right side
    ra_ = a.groupby(["race", "day"]).n.mean(); rr = b.groupby(["race", "day"]).n.mean(); print(f"  race-days: right {len(ra_)} {100*ra_.mean():.2f}c, rest {len(rr)} {100*rr.mean():.2f}c")
    # regress net on sd and on own price
# other-sign case: strongly wrong side
for lab, E, R in sets:
    E = E.copy(); E["sd"] = -E.s.values * R[E.t.values, E.j.values]
    print(lab, E.groupby(pd.cut(E.sd, [-1, -.02, -.01, 0, .01, .02, .03, 1]), observed=True).n.agg(n="size", net=lambda v: round(100 * v.mean(), 2)).T.to_string())
# strict sets: t index sanity: gap sign agrees with s
gap = (L.fair - L.mid).values; S48 = (L.fair - L.mid); S48 = (S48 - S48.rolling(576, min_periods=288).median()).values
for lab, E in (("slow", Ss),): print(lab, "s agrees with S48 sign at t:", np.mean(np.sign(S48[E.t.values, E.j.values]) == E.s.values))
# k uses L.mid: compare distance on last vs on mid for sd in B
print("== pooled: day-demeaned net regressed on right-side flag, all four sets")
P = []
for lab, E, R in sets:
    q = E.copy(); q["sd"] = -q.s.values * R[q.t.values, q.j.values]; q["set"] = lab; P.append(q[["n", "sd", "day", "race", "set"]])
P = pd.concat(P).dropna(); P["right"] = (P.sd >= .02).astype(float); P["g"] = P["set"] + P.day
for lab, q in (("all", P), ("slow only", P[P["set"].str.startswith("slow")]), ("fast only", P[P["set"].str.startswith("fast")])):
    y = q.n - q.groupby("g").n.transform("mean"); x = q.right - q.groupby("g").right.transform("mean"); b = (x * y).sum() / (x * x).sum(); u = y - b * x; cl = (x * u).groupby(q.race).sum(); se = np.sqrt((cl ** 2).sum()) / (x * x).sum()
    print(lab, f"n {len(q)} right-side premium within day {100*b:.2f}c se {100*se:.2f}c t {b/se:.1f}; raw right {100*q[q.right==1].n.mean():.2f}c rest {100*q[q.right==0].n.mean():.2f}c")
