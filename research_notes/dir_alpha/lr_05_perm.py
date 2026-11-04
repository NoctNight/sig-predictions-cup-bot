from lr_common import *
L, D, tp = load()
Z = pd.read_pickle("research_notes/dir_alpha/lr_sets.pkl"); rng = np.random.default_rng(11)
dA, dB = D["A"], D["B"]; qa = book_ctx(L, dB)
Ra, oka = residual(dA); nxa = next_real(dA["R"].values); raa = dA["R"].rolling(12, min_periods=1).max().fillna(0).values > 0
Rb, okb = residual(dB); nxb = next_real(dB["R"].values); rab = dB["R"].rolling(12, min_periods=1).max().fillna(0).values > 0
_, Rc, okc = clean(L, dB, qa)
def perm(lab, fn, real, N=200):
    cm, pl = [], []
    for k in range(N):
        sh = int(rng.integers(288, Ra.shape[0] - 288))
        cm.append(fn(sh, None))
    if True:
        for k in range(N):
            pl.append(fn(None, rng))
    cm, pl = np.array(cm, float), np.array(pl, float)
    print(f"{lab}: real {100*real:.2f}c | common shift null mean {100*np.nanmean(cm):.2f}c p95 {100*np.nanquantile(cm,.95):.2f}c p={(np.sum(cm>=real)+1)/(np.isfinite(cm).sum()+1):.3f} | per-leg shift null mean {100*np.nanmean(pl):.2f}c p95 {100*np.nanquantile(pl,.95):.2f}c p={(np.sum(pl>=real)+1)/(np.isfinite(pl).sum()+1):.3f}", flush=True)
def shift(X, sh, r):
    if sh is not None: return np.roll(X, sh, axis=0)
    n, m = X.shape; o = r.integers(288, n - 288, m); ix = (np.arange(n)[:, None] - o[None, :]) % n
    return np.take_along_axis(X, ix, axis=0)
def fnA(thr_mask, cut):
    def f(sh, r):
        n, m = Ra.shape; o = (sh if sh is not None else 0)
        if r is not None:
            off = r.integers(288, n - 288, m); ix = (np.arange(n)[:, None] - off[None, :]) % n
            R_ = np.take_along_axis(Ra, ix, 0); ok_ = np.take_along_axis(oka, ix, 0)
        else: R_ = np.roll(Ra, sh, 0); ok_ = np.roll(oka, sh, 0)
        E = engine(dA, R_, ok_, trig=thr_mask, nx=nxa, ra=raa)
        if cut: E = E[E.res >= cut]
        return E.n.mean() if len(E) > 5 else np.nan
    return f
def fnB(thr_mask, cut):
    def f(sh, r):
        n, m = Rc.shape
        if r is not None:
            off = r.integers(288, n - 288, m); ix = (np.arange(n)[:, None] - off[None, :]) % n
            R_ = np.take_along_axis(Rc, ix, 0); ok_ = np.take_along_axis(okc, ix, 0)
        else: R_ = np.roll(Rc, sh, 0); ok_ = np.roll(okc, sh, 0)
        E = clean(L, dB, qa, trig=thr_mask, Rmid=(R_, ok_))[0]
        if cut and len(E): E = E[E.res >= cut]
        return E.n.mean() if len(E) > 5 else np.nan
    return f
if len(sys.argv) > 1: perm("A stated", fnA(.03, None), Z["A2"].n.mean()); perm("A oos8", fnA(.02, .03), Z["A1"].n.mean())
if len(sys.argv) > 1: perm("B clean", fnB(.03, None), Z["C"].n.mean(), N=150)
# national factor, matched placebo
def nat(d, Mx):
    ds = np.where(d["party"] == "D", 1., np.where(d["party"] == "R", -1., 0.)); use = (d["office"] != "chamber") & (ds != 0)
    return ds, use
for lab, E, d, Mx, R_, in (("A stated", Z["A2"], dA, dA["S"].values, Ra), ("A oos8", Z["A1"], dA, dA["S"].values, Ra), ("B clean", Z["C"], dB, (dB["book"][0] + dB["book"][1]) / 2, Rc), ("B oos8", Z["B1"], dB, dB["S"].values, Rb)):
    ds, use = nat(d, Mx); e, x, j, s = E.e.values, E.x.values, E.j.values, E.s.values; own = np.where(s > 0, Mx[e, j], 1 - Mx[e, j]); side = ds[j] * s
    gross = s * (Mx[x, j] - Mx[e, j]); natm = np.zeros(len(E)); pl = np.full(len(E), np.nan); plsame = np.full(len(E), np.nan)
    race = d["race"]
    for i in range(len(E)):
        ee, xx, jj = e[i], x[i], j[i]; m0 = Mx[ee]; mv = (Mx[xx] - m0)
        u = use & (m0 >= .15) & (m0 <= .85) & (race != race[jj]) & np.isfinite(mv)
        natm[i] = np.nanmean(mv[u] * ds[u]) * side[i]
        # matched: same partisan direction, own price within 10c, |res|<1.5c at e
        o2 = np.where(ds > 0, m0 if side[i] > 0 else 1 - m0, m0 if side[i] < 0 else 1 - m0)   # price of the same-direction side
        c = use & (np.abs(R_[ee]) < .015) & (np.abs(o2 - own[i]) <= .1) & (race != race[jj]) & np.isfinite(mv)
        if c.sum() >= 3: pl[i] = np.nanmean(mv[c] * ds[c]) * side[i]
    print(f"{lab}: gross {100*np.nanmean(gross):.2f}c | same-direction national move {100*np.nanmean(natm):.2f}c | matched near-line placebo {100*np.nanmean(pl):.2f}c (n {np.isfinite(pl).sum()}), excess {100*np.nanmean(gross-pl):.2f}c")
    m = np.isfinite(natm) & np.isfinite(gross); X = np.c_[np.ones(m.sum()), natm[m]]; b = np.linalg.lstsq(X, gross[m], rcond=None)[0]; print(f"    regress gross on national in trade direction: alpha {100*b[0]:.2f}c beta {b[1]:.2f}; net alpha {100*(b[0]+E.n.mean()-np.nanmean(gross)):.2f}c")
    # national: long D index drift in sample
    for sgn, nm in ((1, "D-side"), (-1, "R-side")):
        q = side == sgn
        if q.sum() > 3: print(f"    {nm}: n {q.sum()} gross {100*np.nanmean(gross[q]):.2f}c net {100*E.n.values[q].mean():.2f}c national in trade dir {100*np.nanmean(natm[q]):.2f}c placebo {100*np.nanmean(pl[q]):.2f}c excess {100*np.nanmean((gross-pl)[q]):.2f}c")
    # independent bets: day-block se vs iid
    v = E.copy(); v["xs"] = gross - pl; v = v.dropna(subset=["xs"]); iid = v["xs"].std() / np.sqrt(len(v))
    # block: 3-day blocks by entry, side clusters
    v["blk"] = (v.t // 864).astype(str) + "_" + np.sign(side[np.isfinite(gross - pl)]).astype(str)
    xv = v["xs"]; se = np.sqrt(((xv - xv.mean()).groupby(v.blk).sum() ** 2).sum()) / len(v)
    print(f"    effective independent bets: raw net: sd {100*E.n.std():.2f}c; block (3-day x side) se on excess {100*se:.2f}c vs iid {100*iid:.2f}c, n_eff {(v["xs"].std()/se)**2:.0f}, blocks {v.blk.nunique()}, t on excess {v["xs"].mean()/se:.1f}")
# drift of the D index across both samples
for lab, d, Mx in (("A", dA, dA["S"].values), ("B", dB, (dB["book"][0] + dB["book"][1]) / 2)):
    ds, use = nat(d, Mx); n = Mx.shape[0]; m0 = Mx[int(n * .02)]; ok = use & (m0 > .15) & (m0 < .85); print(lab, "D index move, start to end of sample %.2fc" % (100 * np.nanmean(((Mx[-1] - m0) * ds)[ok])))
