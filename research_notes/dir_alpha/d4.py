"""Drill-down 4: confirmation filters as fixed rules, national move removed, and a walk-forward ridge model where each day is
scored only by a model fitted on earlier days (purged by the 24 h hold)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, H
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
pd.set_option("display.width", 200, "display.max_rows", 300, "display.max_columns", 40)
S48 = gap - gap.rolling(576, min_periods=288).median()
s = np.sign(S48)
dpm, dk, df, dm = L.lag(L.pm, 288), L.lag(L.k, 288), L.lag(fair, 288), L.lag(mid, 288)
fl = L.rsum(P["sflow"], 288); vol = L.rsum(P["vol"], 288)
both = ((dpm * s >= .01) & (dk * s >= .01))
flowok = (fl * s >= -5000)
thin = vol < 60000
notyet = (dm * s) <= 0.3 * (df * s).clip(lower=.005)
band = (mid >= .15) & (mid <= .85)
nocham = np.ones(mid.shape, bool); nocham[:, L.office == "chamber"] = False
R = {"base >=2c": band, "both venues": band & both, "flow ok": band & flowok, "both+flow": band & both & flowok, "both+thin": band & both & thin, "both+notyet": band & both & notyet, "both+flow+notyet": band & both & flowok & notyet, "both+flow+thin": band & both & flowok & thin, "flow+thin": band & flowok & thin}
# national move in D terms by office, to take the market out of each trade
dsign = L.dsign.values
dm_all = mid.mul(L.dsign, axis=1)
fwn = {}
for hn in ("12h", "24h", "48h"):
    h = H[hn]
    f = (dm_all.shift(-h) - dm_all)
    fo = {o: f.loc[:, (L.office == o) & (L.meta.nlegs == 2).values].mean(axis=1).values for o in ("senate", "governor", "house")}
    fwn[hn] = fo


def rep(name, tr, hn):
    j = tr[:, 0].astype(int); i = tr[:, 1].astype(int)
    d = pd.Series(tr[:, 4]).groupby(L.day[i]).mean()
    nat = np.array([fwn[hn].get(L.office[a], np.zeros(len(L.idx)))[b] for a, b in zip(j, i)]) * dsign[j] * tr[:, 2]
    res = tr[:, 4] - np.nan_to_num(nat)
    ts = L.idx[i]
    return dict(rule=name, hold=hn, n=len(tr), races=len(set(L.race[j])), net=100 * tr[:, 4].mean(), win=(tr[:, 4] > 0).mean(), train=100 * tr[ts < L.SPLIT, 4].mean(), test=100 * tr[ts >= L.SPLIT, 4].mean(), net_ex_nat=100 * res.mean(), t_race=L._t(tr[:, 4], L.race[j])[0], t_day=L._t(tr[:, 4], L.day[i])[0], days=f"{int((d>0).sum())}/{len(d)}", p05=100 * np.quantile(tr[:, 4], .05), worst=100 * tr[:, 4].min(), per_day=len(tr) / len(d))


rows = []
for th in (.015, .02, .03):
    for nm, m in R.items():
        for off in (None, "house", "governor", "senate"):
            for hn in ("12h", "24h", "48h"):
                M = m.values & nocham
                if off:
                    M = M.copy(); M[:, L.office != off] = False
                tr = L.trades(S48, H[hn], thr=th, mask=M)
                if tr is None or len(tr) < 12: continue
                r = rep(nm, tr, hn); r["th"] = th; r["office"] = off or "all"; rows.append(r)
A = pd.DataFrame(rows); A.to_pickle("data/research_diralpha/d4.pkl")
print(A[(A.office == "all") & (A.th == .02)].round(2).to_string(index=False))
print(A[(A.office == "house") & (A.th == .02)].round(2).to_string(index=False))
print(A[(A.office == "all") & (A.th == .015) & (A.hold == "24h")].round(2).to_string(index=False))
print(A[(A.office == "all") & (A.th == .03) & (A.hold == "24h")].round(2).to_string(index=False))

# walk-forward ridge on hourly samples
step = 12
idxs = np.arange(0, len(L.idx), step)
F = {}
for w in (144, 288, 576, 864):
    F[f"gapdev{w}"] = gap - gap.rolling(w, min_periods=w // 2).median()
F["gap"] = gap
for k in (48, 144, 288):
    F[f"lag{k}"] = L.lag(fair, k) - L.lag(mid, k)
    F[f"dfair{k}"] = L.lag(fair, k)
    F[f"dpm{k}"] = L.lag(L.pm, k); F[f"dk{k}"] = L.lag(L.k, k)
    F[f"flow{k}"] = np.tanh(L.rsum(P["sflow"], k) / 50000)
F["both"] = both.astype(float) * s
F["flowok"] = flowok.astype(float) * s
names = list(F)
Y = (mid.shift(-288) - mid).values[idxs]
X = np.stack([F[n].values[idxs] for n in names], axis=2)     # time, leg, feature
thinv = np.log1p(vol.values[idxs])
ok = np.isfinite(X).all(axis=2) & np.isfinite(Y) & band.values[idxs] & nocham[idxs]
sd = np.nanstd(X[ok], axis=0)
Xz = X / sd
# interactions that are odd in direction: every feature above is already signed like the price move
house = (L.office == "house")[None, :, None].repeat(len(idxs), 0).astype(float)
thin = (-(thinv - np.nanmean(thinv[ok])) / np.nanstd(thinv[ok]))[:, :, None]
Xf = np.concatenate([Xz, Xz[:, :, :4] * house, Xz[:, :, :4] * thin], axis=2)
days = L.day[idxs]
ud = sorted(set(days))
pred = np.full(Y.shape, np.nan)
for di, d in enumerate(ud):
    if di < 3: continue
    t = idxs[days == d][0]
    tr_mask = idxs < t - 288                                  # purge: training labels must end before this day starts
    m = ok & tr_mask[:, None]
    A_ = Xf[m]; y = Y[m]
    lam = 200.0
    b = np.linalg.solve(A_.T @ A_ + lam * np.eye(A_.shape[1]), A_.T @ y)
    te = days == d
    pred[te] = Xf[te] @ b
pred[~ok] = np.nan
arr = np.full(mid.shape, np.nan); arr[idxs] = pred
Sp = pd.DataFrame(arr, index=mid.index, columns=mid.columns)
print("\nWalk-forward model, entry when the predicted 24 h move exceeds a threshold")
rows = []
for th in (.005, .0075, .01, .015, .02, .03):
    for off in (None, "house", "governor", "senate"):
        for hn in ("12h", "24h"):
            M = nocham.copy()
            if off:
                M[:, L.office != off] = False
            tr = L.trades(Sp, H[hn], thr=th, mask=M)
            if tr is None or len(tr) < 12: continue
            r = rep("model", tr, hn); r["th"] = th; r["office"] = off or "all"; rows.append(r)
print(pd.DataFrame(rows).drop(columns=["rule", "train", "test"]).round(2).to_string(index=False))
Sp.to_pickle("data/research_diralpha/model_signal.pkl")
b = pd.Series(b, index=names + [n + "*house" for n in names[:4]] + [n + "*thin" for n in names[:4]]) / 1
print(b.round(4).sort_values().to_string())
