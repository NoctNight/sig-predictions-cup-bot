"""Round-2 follow-up on the FEC signal: is it just momentum or the D drift? Controls for the past 3-day outside move, removes the day mean, permutes races within a day,
splits days and spending types, and overlays the strict lag trades. argv[1] = directory with fec_ie.csv."""
import os, sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
from h2_fec_load import load
OUT = sys.argv[1]
L = Lab()
ix, mid, fair = L.idx, L.mid, L.fair
m, f = mid.values, fair.values
key = {(r, p): j for j, (r, p) in enumerate(zip(L.race, L.party))}
c = lambda v: round(100 * float(np.nanmean(v)), 2)
F = load(OUT, set(L.race))
if len(sys.argv) > 2 and sys.argv[2] == "party":
    F = F[F.spe_nam.fillna("").str.upper().str.contains(r"\bDCCC\b|DEMOCRATIC CONGRESSIONAL CAMPAIGN|\bNRCC\b|NATIONAL REPUBLICAN CONGRESSIONAL|\bDSCC\b|DEMOCRATIC SENATORIAL|\bNRSC\b|NATIONAL REPUBLICAN SENATORIAL|CONGRESSIONAL LEADERSHIP FUND|\bCLF\b|HOUSE MAJORITY PAC|\bHMP\b|SENATE LEADERSHIP FUND|\bSLF\b|SENATE MAJORITY PAC|\bSMP\b|WINSENATE")]
    print("party and leadership committees only, rows", len(F), "share of dollars", round(F[F.rc >= "2026-10-15"].amt.sum() / 1e6, 1), "$M since 15 Oct")
dly = F.pivot_table(index=["race", "rc"], columns="side", values="amt", aggfunc="sum").fillna(0).reset_index()
rows = []
days = pd.date_range("2026-10-22", "2026-10-29")
for race, g in dly.groupby("race"):
    if (race, "D") not in key: continue
    g = g.set_index("rc")[["D", "R"]].reindex(pd.date_range("2026-08-01", "2026-10-30")).fillna(0)
    j = key[(race, "D")]
    for d in days:
        r3 = g.loc[d - pd.Timedelta(days=2):d].sum(); r14 = g.loc[d - pd.Timedelta(days=16):d - pd.Timedelta(days=3)].sum()
        if r3.sum() < 50000 or r14.sum() < 100000: continue
        t = int(np.searchsorted(ix, (d + pd.Timedelta(days=1, hours=12)).tz_localize("UTC").timestamp()))
        if t + 288 >= len(ix): continue
        t3 = max(t - 864, 0)
        s = np.sign(r3.D / r3.sum() - r14.D / r14.sum()) or 1.0
        rows.append(dict(race=race, d=d, j=j, t=t, ds=r3.D / r3.sum() - r14.D / r14.sum(), dm=m[t + 288, j] - m[t, j], df=f[t + 288, j] - f[t, j], past=f[t, j] - f[t3, j], mid=m[t, j], gap=f[t, j] - m[t, j],
                         h72=(m[min(t + 864, len(ix) - 1), j] - m[t, j]) if t + 864 < len(ix) else np.nan,
                         net=(L.B[t + 288, j] - L.A[t, j]) if s > 0 else (L.B[t, j] - L.A[t + 288, j])))
R = pd.DataFrame(rows)
R = R[(R.mid > .1) & (R.mid < .9)].dropna(subset=["dm", "df", "past"])
R["dmd"] = R.dm - R.groupby("d").dm.transform("mean")
R["dfd"] = R.df - R.groupby("d").df.transform("mean")
print("n", len(R), "races", R.race.nunique())
X = pd.DataFrame(dict(ds=R.ds - R.ds.mean(), past=R.past - R.past.mean(), gap=R.gap - R.gap.mean()))
for y in ("dmd", "dfd"):
    b = np.linalg.lstsq(np.c_[X.values, np.ones(len(X))], R[y].values, rcond=None)[0]
    print(y, "OLS on share shift, past 3 d fair move, gap:", np.round(b[:3], 4))
    res = R[y].values - np.c_[X.values, np.ones(len(X))] @ b
    # race-cluster SE for ds
    A = np.c_[X.values, np.ones(len(X))]; XtXi = np.linalg.inv(A.T @ A); meat = np.zeros((4, 4))
    for r, idx in pd.Series(range(len(R))).groupby(R.race.values).groups.items():
        u = A[list(idx)].T @ res[list(idx)]; meat += np.outer(u, u)
    V = XtXi @ meat @ XtXi; print("   t(ds)", round(b[0] / np.sqrt(V[0, 0]), 2), "t(past)", round(b[1] / np.sqrt(V[1, 1]), 2))
print("corr(ds, past 3 d fair)", round(R.ds.corr(R.past), 3))
rng = np.random.default_rng(3)
for th in (.2, .3):
    g = R[R.ds.abs() >= th]
    real = (np.sign(g.ds) * g.dmd).mean()
    null = []
    v = R.copy()
    for _ in range(2000):
        sh = v.groupby("d").ds.transform(lambda s: rng.permutation(s.values))
        k = sh.abs() >= th
        null.append((np.sign(sh[k]) * v.dmd[k]).mean())
    print(f"th {th}: n {len(g)} day-demeaned mid 24 h {100*real:.2f}c, permutation within day p {np.mean(np.array(null) >= real):.3f}, null p95 {100*np.quantile(null,.95):.2f}c")
g = R[R.ds.abs() >= .3].copy(); g["sg"] = np.sign(g.ds)
print("against past move only: n", (g.sg * g.past < 0).sum(), "mid 24 h", c((g.sg * g.dm)[g.sg * g.past < 0]), "| with past move: n", (g.sg * g.past >= 0).sum(), "mid", c((g.sg * g.dm)[g.sg * g.past >= 0]))
print("toward D:", (g.sg > 0).sum(), c((g.sg * g.dm)[g.sg > 0]), "toward R:", (g.sg < 0).sum(), c((g.sg * g.dm)[g.sg < 0]), "| demeaned D", c((g.sg * g.dmd)[g.sg > 0]), "R", c((g.sg * g.dmd)[g.sg < 0]))
print("by day:", g.assign(x=g.sg * g.dm).groupby(g.d.dt.strftime("%m-%d")).x.agg(["size", lambda v: round(100 * v.mean(), 2)]).T.to_string())
one = g.sort_values("d").groupby("race").head(1); print("first event per race: n", len(one), "mid 24 h", c(one.sg * one.dm), "fair", c(one.sg * one.df), "72 h", c(one.sg * one.h72), "net 24 h", c(one.net))
print("\nstrict trades: FEC share shift agrees, disagrees, no data")
Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl"); Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl")
Ef["rule"] = "fast"; Es["rule"] = "slow"; E = pd.concat([Ef, Es], ignore_index=True)
E["j"] = E.j.astype(int); E["t"] = E.t.astype(int); E["race"] = L.race[E.j]; E["party"] = L.party[E.j]
E["dside"] = E.s * np.where(E.party == "D", 1, np.where(E.party == "R", -1, 0))
R2 = R.sort_values("t")
st = []
for r in E.itertuples():
    q = R2[(R2.race == r.race) & (R2.t <= r.t) & (R2.t > r.t - 288 * 3)]
    if q.empty or abs(q.ds.iloc[-1]) < .1: st.append("none")
    else: st.append("agree" if np.sign(q.ds.iloc[-1]) == r.dside else "disagree")
E["fec"] = st
print(E.groupby(["rule", "fec"]).net.agg(n="size", net=lambda v: round(100 * v.mean(), 2), win=lambda v: round((v > 0).mean(), 2)).to_string())
print(E.groupby("fec").net.agg(n="size", net=lambda v: round(100 * v.mean(), 2)).to_string())
