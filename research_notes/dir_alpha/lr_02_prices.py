from lr_common import *
L, D, tp = load()
d = D["A"]; Res, ok = residual(d); Mv = d["S"].values
V, Q, NT = candles(d); 
Fv = ((d["P"] + d["K"]) / 2).values
print("=== A: last-trade bias ===")
Es = {"oos8": engine(d, Res, ok).query("res>=.03"), "stated": engine(d, Res, ok, trig=.03)}
# trailing 1h vwap, needs 2+ trades in the hour (A)
vn = (V.values * Q.values); vn[~np.isfinite(vn)] = 0
qs = pd.DataFrame(Q.values).rolling(12, min_periods=1).sum().values; vw = pd.DataFrame(vn).rolling(12, min_periods=1).sum().values / np.where(qs > 0, qs, np.nan)
nts = pd.DataFrame(NT.values).rolling(12, min_periods=1).sum().values
vw24 = None
# residual measured on vwap, with the k from close (k unchanged)
Rv_vw, _ = residual(d, Mv=pd.DataFrame(vw).ffill(limit=288).values)
for nm, E in Es.items():
    t, j, e = E.t.values, E.j.values, E.e.values; s = E.s.values
    r_lt = Res[t, j]; r_vw = Rv_vw[t, j]; r_e = Res[e, j]
    print(nm, st(E, "lt"))
    print("  mean |res| at t (last) %.2fc; on 1h VWAP same direction %.2fc; at the entry trade e %.2fc" % (100 * np.nanmean(np.abs(r_lt)), 100 * np.nanmean(-s * r_vw), 100 * np.nanmean(-s * r_e)))
    print("  last-print offside against 1h vwap at t, in the signal direction: mean %.2fc" % (100 * np.nanmean(-s * (Mv[t, j] - vw[t, j]))))
    for lab, m in (("|res|>=3c at entry trade too", (-s * r_e) >= .03), ("under 3c at entry trade", (-s * r_e) < .03), ("<1.5c at entry", (-s * r_e) < .015),
                   ("1h VWAP also 3c+", (-s * r_vw) >= .03), ("1h VWAP under 3c", (-s * r_vw) < .03), ("VWAP 2c+", (-s * r_vw) >= .02), ("2+ trades in hour and last within 1c of vwap", (nts[t, j] >= 2) & (np.abs(Mv[t, j] - vw[t, j]) <= .01)), ("1 trade in hour", nts[t, j] <= 1), ("3+ prints in hour, VWAP 3c+, entry 3c+", (nts[t, j] >= 3) & ((-s * r_vw) >= .03) & ((-s * r_e) >= .03))):
        print("   ", lab, st(E[m]))
# Exit also at vwap of the hour around? entry and exit priced at the next-hour vwap
for nm, E in Es.items():
    e, x, j, s = E.e.values, E.x.values, E.j.values, E.s.values
    # vwap of bins e..e+11 and x..x+11 (forward window: what an execution over an hour gets) 
    fq = pd.DataFrame(Q.values).rolling(12, min_periods=1).sum().shift(-11).values; fv = pd.DataFrame(vn).rolling(12, min_periods=1).sum().shift(-11).values
    fvw = fv / np.where(fq > 0, fq, np.nan)
    g = s * (fvw[np.minimum(x, len(fvw) - 1), j] - fvw[e, j]) - .0075
    print(nm, "VWAP-to-VWAP over next hour after entry and exit, net of 0.75c: %.2fc (n %d), same trades last-to-last %.2fc" % (100 * np.nanmean(g), np.isfinite(g).sum(), 100 * np.nanmean(E["lt"].values[np.isfinite(g)])))
# rule with signal on vwap from the start
Ev = engine(d, Rv_vw, ok, trig=.03); print("Rule run with 1h VWAP as the SIG price, mask 3c (entry and exit still last trade):", st(Ev, "lt"))
Ev2 = engine(d, Rv_vw, ok); print("  same, oos8 mask 2c then cut 3c:", st(Ev2[Ev2.res >= .03], "lt"))
print("=== B: true-book checks ===")
d = D["B"]; Res, ok = residual(d); Mv = d["S"].values; b, a = d["book"]; Fv = ((d["P"] + d["K"]) / 2).values
fresh = L.P["fresh"].values; rawb = L.bid.values; mid = L.mid.ffill(limit=12).values
def age(fr):
    n, m = fr.shape; A_ = np.zeros((n, m), int)
    for j in range(m):
        c = 10**6
        for i in range(n):
            c = 0 if fr[i, j] else c + 1; A_[i, j] = c
    return A_
qa = age(fresh)
Es = {"oos8": engine(d, Res, ok).query("res>=.03"), "stated": engine(d, Res, ok, trig=.03)}
Ef = engine(d, Res, ok, trig=.03)
for nm, E in Es.items():
    e, x, j, s, t = E.e.values, E.x.values, E.j.values, E.s.values, E.t.values
    print(nm, st(E), "| last-trade net", st(E, "lt")["net"])
    print("  book quote age at entry/exit bins: entry p50 %d max %d, exit p50 %d max %d (bins of 5 min); share entry not fresh %.2f, exit not fresh %.2f" % (np.median(qa[e, j]), qa[e, j].max(), np.median(qa[x, j]), qa[x, j].max(), (qa[e, j] > 0).mean(), (qa[x, j] > 0).mean()))
    sp_e = (a[e, j] - b[e, j]); sp_x = (a[x, j] - b[x, j]); print("  spread at entry %.2fc, at exit %.2fc, implied round trip %.2fc against .75c assumed" % (100 * np.nanmean(sp_e), 100 * np.nanmean(sp_x), 50 * np.nanmean(sp_e + sp_x)))
    m0 = (b + a) / 2
    print("  entry-print against mid in our direction %.2fc, exit print %.2fc; signal-bar print against mid %.2fc" % (100 * np.nanmean(s * (Mv[e, j] - m0[e, j])), 100 * np.nanmean(s * (Mv[x, j] - m0[x, j])), 100 * np.nanmean(s * (Mv[t, j] - m0[t, j]))))
    # residual on mid at signal time and at entry
    print("  distance on last %.2fc, on mid at t %.2fc, on mid at e %.2fc" % (100 * np.nanmean(E.res), 100 * np.nanmean(-s * (Res[t, j] + (m0[t, j] - Mv[t, j]))), 100 * np.nanmean(-s * (Res[e, j] + (m0[e, j] - Mv[e, j])))))
    r_m = -s * (Res[t, j] + (m0[t, j] - Mv[t, j]))
    for lab, m in (("mid distance >=3c", r_m >= .03), ("mid distance <3c", r_m < .03), ("fresh quotes both ends", (qa[e, j] == 0) & (qa[x, j] == 0))):
        print("   ", lab, st(E[m]), "lt", st(E[m], "lt").get("net"))
# clean live version: signal on book mid, no print needed, entry next bin, exit first bin at or after +72 h, both on a fresh poll
def clean(trig, hold=864, lo=.1, hi=.9, cool=864, freshonly=True, mode="ols", delay=1):
    m0 = (b + a) / 2; dd = dict(d); 
    R2, ok2 = residual(d, Mv=pd.DataFrame(m0).values)
    M = (np.abs(R2) >= trig) & (m0 >= lo) & (m0 <= hi) & ok2 & (qa == 0)
    days = np.asarray(d["S"].index.strftime("%m-%d")); out = []; n, mm = m0.shape
    for j in range(mm):
        last = -10**9
        for t in np.flatnonzero(M[:, j]):
            if t - last < cool: continue
            e = t + delay
            while e < n and e < t + 12 and qa[e, j] != 0: e += 1
            if e >= n or qa[e, j] != 0: continue
            x = e + hold
            while x < n and x < e + hold + 72 and qa[x, j] != 0: x += 1
            if x >= n or qa[x, j] != 0: continue
            s = -1.0 if R2[t, j] > 0 else 1.0
            nt = (rawb[x, j] - L.ask.values[e, j]) if s > 0 else (rawb[e, j] - L.ask.values[x, j])
            if not np.isfinite(nt): continue
            out.append(dict(j=j, t=t, e=e, x=x, s=s, res=abs(R2[t, j]), rich=R2[t, j] > 0, race=d["race"][j], party=d["party"][j], day=days[t], n=nt, g=s * (m0[x, j] - m0[e, j]))); last = t
    return pd.DataFrame(out)
for tr in (.03, .035, .04):
    C = clean(tr); print("clean: mid signal, fresh poll, next poll entry, thr", tr, st(C), "gross mid", round(100 * C.g.mean(), 2), "rich", st(C[C.rich]).get("net"), "cheap", st(C[~C.rich]).get("net"))
C = clean(.02); C3 = C[C.res >= .03]; print("clean, oos8 first-touch 2c mask then cut 3c:", st(C3))
for h in (576, 864, 1044):
    C = clean(.03, hold=h, cool=864); print("clean hold", h / 12, st(C))
pd.to_pickle(clean(.03), O + "lr_clean_b.pkl") if False else None
