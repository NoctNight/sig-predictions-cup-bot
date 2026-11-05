"""Round 6: the generated hypotheses that the panel can test now (A4, A7, A14, A23, A27, A34, A35, A37, A1, A5 and variants)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, survivors, show
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
meta = L.meta; cols = L.cols; ds = L.dsign
sig = {}
empty = lambda: pd.DataFrame(np.nan, index=mid.index, columns=mid.columns)
# A4 stale clock: outside change since the last SIG quote step, plus age of the step
A = empty(); AGE = empty()
mv = mid.ffill().values; fv = fair.ffill().values
out = np.full(mv.shape, np.nan); age = np.full(mv.shape, np.nan)
for j in range(mv.shape[1]):
    last = 0
    for i in range(1, mv.shape[0]):
        if not np.isfinite(mv[i, j]): continue
        if mv[i, j] != mv[i - 1, j]: last = i
        out[i, j] = fv[i, j] - fv[last, j]; age[i, j] = (i - last) / 12
A = pd.DataFrame(out, index=mid.index, columns=cols); AGE = pd.DataFrame(age, index=mid.index, columns=cols)
sig["A4stale"] = A
for ah in (1, 3, 8):
    sig[f"A4stale:age>={ah}h"] = A.where(AGE >= ah)
# A14 slow average of outside plus a leg offset
for tau, tn in ((72, "6h"), (144, "12h"), (288, "24h"), (576, "48h")):
    e = fair.ewm(halflife=tau, min_periods=tau // 2).mean()
    off = (mid - e).rolling(576, min_periods=288).mean()
    sig[f"A14ema:{tn}"] = (e + off) - mid
    # the fast one: outside now against its slow mean, the part SIG has not taken in
    sig[f"A14fastslow:{tn}"] = (fair - e) - (mid - mid.ewm(halflife=tau, min_periods=tau // 2).mean())
# A23 residual from the compression curve, by office, hourly
step = 12
bins = np.linspace(0, 1, 11)
Rv = np.full(mid.shape, np.nan)
gv, fv2 = gap.values, fair.values
for off_ in ("senate", "governor", "house"):
    c = np.flatnonzero(L.office == off_)
    for i in range(0, mid.shape[0], step):
        f = fv2[i, c]; g = gv[i, c]
        ok = np.isfinite(f) & np.isfinite(g)
        if ok.sum() < 20: continue
        b = np.digitize(f, bins)
        m = pd.Series(g[ok]).groupby(b[ok]).transform("mean").values
        tmp = np.full(len(c), np.nan); tmp[ok] = g[ok] - m
        Rv[i, c] = tmp
R = pd.DataFrame(Rv, index=mid.index, columns=cols).ffill(limit=step)
sig["A23curve"] = R
sig["A23curvefresh"] = R - R.rolling(576, min_periods=288).median()
# A27 regional factors: PCA on hourly outside changes, drop the national one
d = L.lag(fair, 12).iloc[::12].dropna(how="all", axis=1).fillna(0)
c_ = (L.meta.loc[d.columns, "office"] != "chamber").values
X = d.values[:, c_]; X = X - X.mean(0)
U, s_, Vt = np.linalg.svd(X, full_matrices=False)
print("variance share of first 6 factors", np.round((s_ ** 2 / (s_ ** 2).sum())[:6], 3))
exs = list(np.array(d.columns)[c_])
for k_, n in ((12, "1h"), (48, "4h"), (288, "24h")):
    df = L.lag(fair, k_)[exs].fillna(0); dm = L.lag(mid, k_)[exs].fillna(0)
    V = Vt[1:5].T
    imp = pd.DataFrame((df.values @ V) @ V.T, index=mid.index, columns=exs)
    sig[f"A27region:{n}"] = (imp - dm).reindex(columns=cols)
# A34 same-state House cluster
Hc = empty()
for st, g_ in meta[(meta.office == "house")].groupby("state"):
    races = g_.race.unique()
    if len(races) < 3: continue
    dg = gap[g_.index].mul(ds[g_.index], axis=1)
    mean = dg.mean(axis=1)
    for e in g_.index:
        Hc[e] = (mean - dg[e]) * ds[e]
sig["A34cluster"] = -Hc
# A35 logit-implied SIG change for the outside move
lg = lambda x: np.log(x.clip(.01, .99) / (1 - x.clip(.01, .99)))
ex = lambda x: 1 / (1 + np.exp(-x))
for k_, n in ((48, "4h"), (288, "24h")):
    implied = pd.DataFrame(ex((lg(mid.shift(k_)) + (lg(fair) - lg(fair.shift(k_)))).values), index=mid.index, columns=cols)
    sig[f"A35logit:{n}"] = implied - mid
# A37 favourite differs by venue
flip = ((mid > .5) & (fair < .5)) | ((mid < .5) & (fair > .5))
sig["A37flip"] = gap.where(flip)
# A7 chaser bid
nf = L.rsum(P["sflow"] * mid, 288)
sig["A7chaser"] = nf.where((fair >= .15) & (fair <= .35))
sig["A7chaserfade"] = -sig["A7chaser"]
# A15 selling in favourites
nf6 = L.rsum(P["sflow"] * mid, 72)
sig["A15favdip"] = (-nf6).where((fair > .85) & (nf6 < 0))
# A5 / A17 chamber legs
cham = np.array(L.office == "chamber")
ch = lambda S: S.where(pd.Series(cham, index=cols), axis=1)
for n, S in (("lag4", L.lag(fair, 48) - L.lag(mid, 48)), ("lag24", L.lag(fair, 288) - L.lag(mid, 288)), ("fresh48", gap - gap.rolling(576, min_periods=288).median())):
    sig[f"A5chamber:{n}"] = ch(S)
dm2 = L.lag(mid, 24); df2 = L.lag(fair, 24)
sig["A17chamberfade"] = ch((-dm2).where((dm2.abs() >= .015) & (df2.abs() < .005)))
# variant ideas on the main family
gd = gap - gap.rolling(576, min_periods=288).median()
sig["V1median72"] = gap - gap.rolling(864, min_periods=432).median()
sig["V2mean48"] = gap - gap.rolling(576, min_periods=288).mean()
sig["V3zscore"] = (gap - gap.rolling(576, min_periods=288).mean()) / gap.rolling(576, min_periods=288).std()
sig["V4pct"] = gd / (mid * (1 - mid)).pow(.5)
sig["V5acc"] = L.lag(gd, 48)
sig["V6newhigh"] = gap.where((gap >= gap.rolling(576, min_periods=288).max()) | (gap <= gap.rolling(576, min_periods=288).min()))
rows = []
for i, (k, s) in enumerate(sig.items()):
    offs = (None,) if k.startswith(("A5", "A17")) else (None, "senate", "governor", "house")
    rows.append(L.test(k, s, hs=("4h", "12h", "24h"), qs=(.9, .98) if not k.startswith(("A5", "A17")) else (0.5, .9), offices=offs, family=k.split(":")[0]))
    if i % 8 == 0: print(i, k, flush=True)
A_ = pd.concat(rows, ignore_index=True); A_.to_pickle("data/research_diralpha/r6.pkl")
print("tests", L.n_tests, "signals", len(sig))
S = survivors(A_); print("survivors", len(S)); show(S, 45)
print("\nBest test net, n >= 20:"); show(A_[A_.get("test_n", 0) >= 20].sort_values("test_net_c", ascending=False), 20)
print(A_[A_.q >= .9].groupby(["family", "h"]).test_net_c.median().unstack().round(2).to_string())
