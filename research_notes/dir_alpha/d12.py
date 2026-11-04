"""Drill-down 12: placebos and honest portfolio.
 A placebo 1: each leg's signal applied to another leg of the same office (is it leg-specific, or just the national drift?)
 B placebo 2: circular time shift of the mask on the filtered rule
 C placebo 3: sign flipped
 D walk-forward parameter choice: each day trades the slow rule's threshold and hold picked only from earlier days
 E combined book (fast + slow), day by day."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, H
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
pd.set_option("display.width", 200, "display.max_rows", 300, "display.max_columns", 30)
nochm = L.office != "chamber"
S48 = gap - gap.rolling(576, min_periods=288).median()
s = np.sign(S48)
both = (L.lag(L.pm, 288) * s >= .01) & (L.lag(L.k, 288) * s >= .01)
flowok = L.rsum(P["sflow"], 288) * s >= -5000
M1 = (((mid >= .15) & (mid <= .85)) & both & flowok).values.copy(); M1[:, ~nochm] = False
real = L.trades(S48, H["24h"], thr=.02, mask=M1, cool=H["24h"])
print("real slow rule:", len(real), "net %.2fc gross %.2fc" % (100 * real[:, 4].mean(), 100 * real[:, 3].mean()))
W = 6
dp, dk, dm, df = L.lag(L.pm, W), L.lag(L.k, W), L.lag(mid, W), L.lag(fair, W)
sf = np.sign(df)
M2 = (((dp * sf >= .015) & (dk * sf >= .015) & ((dm * sf) < .5 * (df * sf)) & (mid >= .25) & (mid <= .75))).values.copy(); M2[:, ~nochm] = False
fast = L.trades(df, H["4h"], thr=1e-9, mask=M2, cool=48)
print("real fast rule:", len(fast), "net %.2fc gross %.2fc" % (100 * fast[:, 4].mean(), 100 * fast[:, 3].mean()))
rng = np.random.default_rng(7)
Bv, Av = L.B, L.A
n, m = Bv.shape


def pl(tr_rows, hold, perm):
    """Enter on perm[leg]'s prices at the same bin, same sign, same hold, measured at the touch."""
    v = []
    for j, i, sg in tr_rows:
        k = perm[j]
        if i + hold >= n: continue
        a0, b0, a1, b1 = Av[i, k], Bv[i, k], Av[i + hold, k], Bv[i + hold, k]
        if not np.isfinite(a0 + b0 + a1 + b1): continue
        v.append((b1 - a0) if sg > 0 else (b0 - a1))
    return np.mean(v) if v else np.nan


for name, tr, hold in (("slow", real, 288), ("fast", fast, 48)):
    rows = [(int(a), int(b), c) for a, b, c in tr[:, :3]]
    # D direction preserved: map a leg to a random leg of the same office and party and same price band
    res = []
    midv = mid.values
    cats = {}
    for j in range(m):
        cats.setdefault((L.office[j], L.party[j]), []).append(j)
    for _ in range(300):
        perm = np.arange(m)
        for c, ls in cats.items():
            perm[ls] = rng.permutation(ls)
        res.append(pl(rows, hold, perm))
    res = np.array(res)
    print(f"A placebo {name}: same office and party, other leg's prices: mean {100*np.nanmean(res):.2f}c, p95 {100*np.nanquantile(res,.95):.2f}c, real {100*tr[:,4].mean():.2f}c, p={(np.sum(res>=tr[:,4].mean())+1)/301:.4f}")
    print(f"C placebo {name}: flipped sign at the same entries: {100*pl([(a,b,-c) for a,b,c in rows],hold,np.arange(m)):.2f}c")
# B circular shift of the mask, slow rule
Sv = S48.values; res = []
for k in range(200):
    sh = int(rng.integers(288, n - 288))
    Mx = np.roll(M1, sh, axis=0); Sx = pd.DataFrame(np.roll(Sv, sh, axis=0), index=mid.index, columns=mid.columns)
    tr = L.trades(Sx, H["24h"], thr=.02, mask=Mx, cool=H["24h"])
    if tr is not None: res.append(tr[:, 4].mean())
res = np.array(res)
print(f"B time-shift placebo slow: mean {100*res.mean():.2f}c, p95 {100*np.quantile(res,.95):.2f}c, max {100*res.max():.2f}c, real {100*real[:,4].mean():.2f}c, p={(np.sum(res>=real[:,4].mean())+1)/(len(res)+1):.4f}")
# D walk-forward choice of threshold and hold
ud = sorted(set(L.day))
cells = [(th, hn) for th in (.015, .02, .025, .03) for hn in ("12h", "24h", "36h", "48h")]
H2 = {"12h": 144, "24h": 288, "36h": 432, "48h": 576}
all_ = {}
for th, hn in cells:
    t = L.trades(S48, H2[hn], thr=th, mask=M1, cool=H2[hn])
    all_[(th, hn)] = pd.DataFrame(dict(day=L.day[t[:, 1].astype(int)], end=L.idx[np.minimum(t[:, 1].astype(int) + H2[hn], n - 1)], ts=L.idx[t[:, 1].astype(int)], net=t[:, 4]))
wf = []
for d in ud[3:]:
    t0 = L.idx[L.day == d][0]
    best, bs = None, -9
    for c, df_ in all_.items():
        past = df_[df_.end < t0]
        if len(past) >= 8:
            sc = past.net.mean() * np.sqrt(len(past))
            if sc > bs: bs, best = sc, c
    if best is None: continue
    x = all_[best][all_[best].day == d]
    wf.append(dict(day=d, pick=f"{best[0]*100:.1f}c {best[1]}", n=len(x), net=100 * x.net.mean() if len(x) else np.nan, win=(x.net > 0).mean() if len(x) else np.nan))
W_ = pd.DataFrame(wf); print("\nD walk-forward parameter choice"); print(W_.round(2).to_string(index=False))
print("pooled: trades", int(W_.n.sum()), "net %.2fc" % ((W_.net * W_.n).sum() / W_.n.sum()))
