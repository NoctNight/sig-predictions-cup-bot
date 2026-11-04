from lr_common import *
L, D, tp = load()
def dec(lab, E, Mx, Res, d, cl="n"):
    Fv = ((d["P"] + d["K"]) / 2).values; e, x, j, s, t = E.e.values, E.x.values, E.j.values, E.s.values, E.t.values
    dS = s * (Mx[x, j] - Mx[e, j]); dF = s * (Fv[x, j] - Fv[e, j]); clos = s * (Res[x, j] - Res[e, j])
    dPk = s * (d["P"].values[x, j] - d["P"].values[e, j]); dK = s * (d["K"].values[x, j] - d["K"].values[e, j])
    print(f"{lab}: n {len(E)} start dist {100*np.nanmean(-s*Res[e,j]):.2f}c, closes {100*np.nanmean(clos):.2f}c = SIG move {100*np.nanmean(dS):.2f}c + outside move toward SIG {100*np.nanmean(clos-dS):.2f}c | outside moved our way {100*np.nanmean(dF):.2f}c (PM {100*np.nanmean(dPk):.2f} K {100*np.nanmean(dK):.2f}) | k x outside share of SIG move {np.nanmean(dF)*.92/np.nanmean(dS):.2f}, sd of dF {100*np.nanstd(dF):.1f}c")
    # the part of the SIG move not explained by outside moving: pure closure
    q = pd.DataFrame(dict(dF=dF, dS=dS, n=E[cl].values, race=E.race.values, day=E.day.values))
    print("   by outside direction over the hold:", q.groupby(pd.cut(q.dF, [-1, -.01, .01, 1]), observed=True).agg(c=("n", "size"), net=("n", lambda v: round(100 * v.mean(), 2)), sig=("dS", lambda v: round(100 * v.mean(), 2)), out=("dF", lambda v: round(100 * v.mean(), 2))).to_dict("index"))
    return dS, dF
def fresh_cut(lab, E, d, Res, cl="n"):
    Pa = age_since_change(d["P"]); Ka = age_since_change(d["K"]); P, K = d["P"].values, d["K"].values
    t, j, s = E.t.values, E.j.values, E.s.values
    gap = np.abs(P[t, j] - K[t, j]); pa, ka = Pa[t, j], Ka[t, j]
    S = d["S"].values; Fv = (P + K) / 2; t0 = np.maximum(t - 288, 0)
    dS24 = s * (S[t, j] - S[t0, j]); dF24 = s * (Fv[t, j] - Fv[t0, j]); r24 = np.abs(Res[t0, j])
    print(f"{lab}: median age P {np.median(pa)*5:.0f} min, K {np.median(ka)*5:.0f} min; P-K gap median {100*np.median(gap):.2f}c; PM side/Kalshi side of SIG: {np.mean(np.sign(-s*0+s)*0)}")
    cuts = (("P age <=1h & K age <=1h", (pa <= 12) & (ka <= 12)), ("P or K older than 3h", (pa > 36) | (ka > 36)), ("P-K <=1c", gap <= .01), ("P-K >1c", gap > .01), ("P-K<=1.5c, both <=1h", (gap <= .015) & (pa <= 12) & (ka <= 12)), ("P-K<=1c, both <=1h", (gap <= .01) & (pa <= 12) & (ka <= 12)),
            ("both venues past SIG on our side by 2c+ (each venue's own line distance)", (s * (P[t, j] - S[t, j]) >= .0) & (s * (K[t, j] - S[t, j]) >= .0)),
            ("24h: outside moved our way 1c+ (SIG lag)", dF24 >= .01), ("24h: outside flat, SIG moved away", (np.abs(dF24) < .01) & (dS24 < -.005)), ("24h: outside moved against us 1c+", dF24 <= -.01), ("24h: distance already 2.5c+ a day ago", r24 >= .025), ("24h: distance under 2.5c a day ago", r24 < .025))
    for n_, m in cuts: print("   ", n_, st(E[m], cl))
d = D["A"]; Res, ok = residual(d); Mv = d["S"].values
A1 = engine(d, Res, ok).query("res>=.03").reset_index(drop=True); A2 = engine(d, Res, ok, trig=.03).reset_index(drop=True)
dec("A oos8", A1, Mv, Res, d); dec("A stated", A2, Mv, Res, d); fresh_cut("A oos8", A1, d, Res); fresh_cut("A stated", A2, d, Res)
# minute kalshi or hourly
K = d["K"].values; 
ch = np.vstack([np.zeros((1, K.shape[1]), bool), (K[1:] != K[:-1]) & np.isfinite(K[1:]) & np.isfinite(K[:-1])]); hr = (d["S"].index.minute == 0)[:, None]
offh = pd.DataFrame(ch & ~hr).rolling(36, min_periods=1).sum().values
for lab, E in (("oos8", A1), ("stated", A2)):
    m = offh[E.t.values, E.j.values] > 0; print("A", lab, "Kalshi minute data live in past 3h:", st(E[m]), "| hourly only:", st(E[~m]))
d = D["B"]; Res, ok = residual(d); Mv = d["S"].values; qa = book_ctx(L, d)
B2 = engine(d, Res, ok, trig=.03).reset_index(drop=True); B1 = engine(d, Res, ok).query("res>=.03").reset_index(drop=True)
C, R2, ok2 = clean(L, d, qa); b, a = d["book"]; m0 = (b + a) / 2
dec("B oos8 (last)", B1, Mv, Res, d); dec("B stated (last)", B2, Mv, Res, d); dec("B clean (mid)", C, m0, R2, d)
fresh_cut("B oos8", B1, d, Res); fresh_cut("B stated", B2, d, Res); fresh_cut("B clean", C, d, R2)
# in B raw polls: is the outside stale? venue_quotes has ts for PM; kalshi in venue_fair
