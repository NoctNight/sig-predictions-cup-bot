"""Time pattern of the line trade and the age of the distance."""
from lr_common import *
L, D, tp = load(); Z = pd.read_pickle("research_notes/dir_alpha/lr_sets.pkl"); dA, dB = D["A"], D["B"]; qa = book_ctx(L, dB)
Ra, oka = residual(dA); C, Rc, okc = clean(L, dB, qa); b, a = dB["book"]; mB = (b + a) / 2
def age_over(R, thr=.02):
    """bins since |R| was last below thr, on the same side; NaN (big) if it has never been"""
    n, m = R.shape; A_ = np.full((n, m), np.nan)
    for j in range(m):
        c = np.nan
        for i in range(n):
            r = R[i, j]
            if not np.isfinite(r): A_[i, j] = c if np.isfinite(c) else np.nan; c = c + 1 if np.isfinite(c) else c; continue
            if abs(r) < thr: c = 0
            elif np.isfinite(c): c += 1
            A_[i, j] = c
    return A_
for lab, E, R_ in (("A oos8", Z["A1"], Ra), ("A stated", Z["A2"], Ra), ("B clean", Z["C"], Rc), ("B clean first-touch", Z["C1"], Rc)):
    a_ = age_over(R_)[E.t.values, E.j.values] / 12
    q = E.assign(ageh=a_); bins = [-1, 1, 6, 24, 72, 1e6]
    print(lab, "by hours since under 2c:"); print(q.groupby(pd.cut(q.ageh, bins), observed=True).n.agg(n="size", net=lambda v: round(100 * v.mean(), 2), win=lambda v: round((v > 0).mean(), 2)).T.to_string())
    print("   never under 2c since panel start: n %d net %.2fc" % (q.ageh.isna().sum(), 100 * q[q.ageh.isna()].n.mean() if q.ageh.isna().any() else np.nan))
    # D side split by age
    ds = dside(E); print("   under 24h: D-side", st(q[(q.ageh < 24) & (ds > 0)]), " R-side", st(q[(q.ageh < 24) & (ds < 0)])); print("   24h+: D-side", st(q[(q.ageh >= 24) & (ds > 0)]), " R-side", st(q[(q.ageh >= 24) & (ds < 0)]))
# panel-start effect: drop first 3 days of each panel
for lab, E in (("A stated", Z["A2"]), ("A oos8", Z["A1"]), ("B clean", Z["C"]), ("B stated", Z["B2"]), ("B oos8", Z["B1"]), ("B clean ft", Z["C1"])):
    t0 = E.t.min(); print(lab, "first entry bin", t0, "| first 3 days", st(E[E.t < t0 + 864]), "| after", st(E[E.t >= t0 + 864]))
# day table for B clean by side
E = Z["C"].copy(); E["ds"] = dside(E); print(E.pivot_table(index="day", columns="ds", values="n", aggfunc=["size", lambda v: round(100 * v.mean(), 2)]).to_string())
# marks for late entries, shorter holds, on mid-net touch
for h in (288, 432, 576):
    C2 = clean(L, dB, qa, hold=h, cool=864)[0]; C2["ds"] = dside(C2)
    for a_, b_, lab in ((0, 96 * 4, "21-24"), (96 * 4, 288 * 6.0, "25-27"), (288 * 6.0, 10**9, "27+")):
        m_ = (C2.t - C2.t.min() >= a_) & (C2.t - C2.t.min() < b_)
        print(f"hold {h//12}h entries {lab}", st(C2[m_]), "D", st(C2[m_ & (C2.ds > 0)]).get("net"), "R", st(C2[m_ & (C2.ds < 0)]).get("net"))
# by race pair confound: is D-side vs R-side in B clean about the House D drift?
print("D index B by day:"); ds = np.where(dB["party"] == "D", 1., np.where(dB["party"] == "R", -1., 0.)); use = (dB["office"] != "chamber") & (ds != 0)
dd = pd.DataFrame(mB, index=dB["S"].index); xx = (dd.iloc[:, use] * ds[use]).resample("1D").last(); print((xx.diff().mean(axis=1) * 100).round(2).to_dict())
