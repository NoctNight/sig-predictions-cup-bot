"""The live list is a snapshot of the stock of legs off the line. Score the same snapshot at every date. Market-adjusted excess by period."""
from lr_common import *
L, D, tp = load(); Z = pd.read_pickle("research_notes/dir_alpha/lr_sets.pkl"); dA, dB = D["A"], D["B"]; qa = book_ctx(L, dB)
Ra, oka = residual(dA); C, Rc, okc = clean(L, dB, qa); b, a = dB["book"]; mB = (b + a) / 2; mA = dA["S"].values
def placebo(E, d, Mx, R_, near=.015):
    ds = np.where(d["party"] == "D", 1., np.where(d["party"] == "R", -1., 0.)); use = (d["office"] != "chamber") & (ds != 0); race = d["race"]
    e, x, j, s = E.e.values, E.x.values, E.j.values, E.s.values; pl = np.full(len(E), np.nan); own = np.where(s > 0, Mx[e, j], 1 - Mx[e, j]); side = ds[j] * s
    for i in range(len(E)):
        m0 = Mx[e[i]]; mv = Mx[x[i]] - m0; o2 = np.where(ds > 0, m0 if side[i] > 0 else 1 - m0, m0 if side[i] < 0 else 1 - m0)
        c = use & (np.abs(R_[e[i]]) < near) & (np.abs(o2 - own[i]) <= .1) & (race != race[j[i]]) & np.isfinite(mv)
        if c.sum() >= 3: pl[i] = np.nanmean(mv[c] * ds[c]) * side[i]
    return pl
def snapA(T, hold=864, trig=.03):
    Mc = np.zeros(Ra.shape, bool); Mc[T] = (np.abs(Ra[T]) >= trig) & (mA[T] >= .1) & (mA[T] <= .9) & oka[T]
    E = engine(dA, Ra, oka, Mcustom=Mc, hold=hold); return E
def snapB(T, hold=864, trig=.03):
    ex = np.zeros(Rc.shape, bool); ex[T] = True
    return clean(L, dB, qa, trig=trig, hold=hold, extra=ex, Rmid=(Rc, okc))[0]
for lab, fn, Mx, R_, d, n, off in (("A", snapA, mA, Ra, dA, Ra.shape[0], 288), ("B", snapB, mB, Rc, dB, Rc.shape[0], 144)):
    rows = []
    for T in range(off, n - 870, 72):      # every 6 h, read by day
        E = fn(T)
        if len(E) < 3: continue
        pl = placebo(E, d, Mx, R_); gross = E.s.values * (Mx[E.x.values, E.j.values] - Mx[E.e.values, E.j.values]); ds = dside(E)
        rows.append(dict(T=T, t=d["S"].index[T].strftime("%m-%d %H"), n=len(E), net=100 * E.n.mean(), gross=100 * gross.mean(), pl=100 * np.nanmean(pl), xs=100 * np.nanmean(gross - pl), dshare=(ds > 0).mean()))
    Q = pd.DataFrame(rows); Q["dd"] = Q.t.str[:5]; Q2 = Q.groupby("dd")[["n", "net", "gross", "pl", "xs", "dshare"]].mean().round(2)
    print(lab, "snapshot, all legs 3c+ at that moment, 72 h hold, mean over 4 snapshots a day:"); print(Q2.to_string())
    print("  mean over snapshots: net %.2fc excess %.2fc; share of days net>0 %.2f, excess>0 %.2f" % (Q["net"].mean(), Q["xs"].mean(), (Q2["net"] > 0).mean(), (Q2["xs"] > 0).mean()))
    pass
# excess by period for trades
for lab, E, d, Mx, R_ in (("A stated", Z["A2"], dA, mA, Ra), ("B clean", Z["C"], dB, mB, Rc), ("B stated (book net)", Z["B2"], dB, dB["S"].values, Residual := residual(dB)[0])):
    E = E.copy(); pl = placebo(E, d, Mx, R_); E["g"] = E.s.values * (Mx[E.x.values, E.j.values] - Mx[E.e.values, E.j.values]); E["pl"] = pl; E["xs"] = E.g - pl; E["ds"] = dside(E); E["cost"] = E.g - E.n
    E["per"] = (E.t - E.t.min()) // 864
    print(lab); print(E.groupby("per").agg(n=("n", "size"), net=("n", lambda v: round(100 * v.mean(), 2)), gross=("g", lambda v: round(100 * v.mean(), 2)), placebo=("pl", lambda v: round(100 * v.mean(), 2)), excess=("xs", lambda v: round(100 * v.mean(), 2)), Dshare=("ds", lambda v: round((v > 0).mean(), 2))).T.to_string())
    print("  excess by side:", E.groupby(["per", "ds"]).xs.agg(n="size", xs=lambda v: round(100 * v.mean(), 2)).T.to_string())
    print("  net after market-neutral (excess - cost): %.2fc" % (100 * (E["xs"] - E["cost"]).mean()))
# live list age
Z2 = pd.read_pickle(O + "line_now.pkl"); print(Z2.head(3).to_string()); 
R_ = pd.DataFrame(Rc, columns=dB["S"].columns); key = {(r, p): i for i, (r, p) in enumerate(zip(dB["race"], dB["party"]))}
rows = []
for r in Z2.itertuples():
    p = r.leg.split()[0]; j = key.get((r.race, p))
    if j is None: rows.append(dict(race=r.race, leg=r.leg, d_now=r.dist_c, age=np.nan)); continue
    v = Rc[:, j]; v = v[np.isfinite(v)]
    # sign now
    sgn = 1 if "NO" in r.leg else -1
    below = np.flatnonzero(sgn * v < .02); ag = (len(v) - 1 - below[-1]) / 12 if len(below) else 9999
    below3 = np.flatnonzero(sgn * v < .03); ag3 = (len(v) - 1 - below3[-1]) / 12 if len(below3) else 9999
    rows.append(dict(race=r.race, leg=r.leg, side=r.side, d_now=r.dist_c, hrs_over2c=round(ag, 1), hrs_over3c=round(ag3, 1), d_24h=round(100 * sgn * v[-289], 1), d_72h=round(100 * sgn * v[-865], 1)))
Q = pd.DataFrame(rows); print(Q.to_string()); print("median hours over 2c", Q.hrs_over2c.median(), "share over 2c 24h+", (Q.hrs_over2c >= 24).mean(), "share 3c at -24h", (Q.d_24h >= 3).mean())
