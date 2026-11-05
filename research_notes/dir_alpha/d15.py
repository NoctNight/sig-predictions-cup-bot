"""Drill-down 15: the honest version after the audit. Strict mechanics for BOTH rules: signal from real polls only (no forward fill in the history), fill at the next
real poll, one bet per race, exit at the first real poll. Then the book with the cost curve from our own orders and a selection haircut.
Cost per share each way: 0.02c per 1,000 shares (own orders: about 0 to 5k, 0.25c at 10k, about 0.75c at 37k)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
P = L.P
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
fresh = P["fresh"].reindex(columns=L.cols).fillna(False)
mid = L.mid.where(fresh); fair = L.fair.where(fresh); pm = L.pm.where(fresh); k = L.k.where(fresh)
gap = fair - mid
n, m = mid.shape
nochm = L.office != "chamber"
near = lambda df, k_: df.shift(k_).combine_first(df.shift(k_ + 1)).combine_first(df.shift(k_ - 1))      # anchor: a real poll within one bin of the lag
S48 = gap - gap.rolling(576, min_periods=288).median()
s = np.sign(S48)
both = ((pm - near(pm, 288)) * s >= .01) & ((k - near(k, 288)) * s >= .01)
flowok = L.rsum(P["sflow"], 288) * s >= -5000
slow = ((S48.abs() >= .02) & (mid >= .15) & (mid <= .85) & both & flowok).values.copy(); slow[:, ~nochm] = False


def trig(W, m_, c, lo, hi):
    dp, dk, dm, df = pm - near(pm, W), k - near(k, W), mid - near(mid, W), fair - near(fair, W)
    sf = np.sign(df)
    M = ((dp * sf >= m_) & (dk * sf >= m_) & ((dm * sf) < c * (df * sf)) & (mid >= lo) & (mid <= hi)).values.copy()
    M[:, ~nochm] = False
    return M, df.values


fast, Dv = trig(6, .015, .5, .25, .75)
fast1, Dv1 = trig(12, .015, .5, .25, .75)
Sv = S48.values
Bv, Av = L.B, L.A
fv = fresh.values


def first_real(t, j, limit=24):
    for x in range(t, min(t + limit, n)):
        if fv[x, j] and np.isfinite(Bv[x, j]) and np.isfinite(Av[x, j]): return x
    return None


def events(M, sv, hold, cool):
    out, last = [], {}
    for t in range(n):
        idx = np.flatnonzero(M[t])
        if not len(idx): continue
        for j in sorted(idx, key=lambda q: -abs(sv[t, q])):
            r = L.race[j]
            if t - last.get(r, -10**9) < cool: continue
            e = first_real(t + 1, j, 6)
            if e is None or e + hold >= n: continue
            x = first_real(e + hold, j)
            if x is None: continue
            sg = 1.0 if sv[t, j] > 0 else -1.0
            px = Av[e, j] if sg > 0 else 1 - Bv[e, j]
            val = Bv[x, j] if sg > 0 else 1 - Av[x, j]
            last[r] = t
            out.append((j, t, e, x, sg, val - px, sg * (Bv[x, j] + Av[x, j] - Bv[e, j] - Av[e, j]) / 2))
    return pd.DataFrame(out, columns=["j", "t", "e", "x", "s", "net", "gross"])


def rep(nm, E):
    if E.empty: print(nm, "no trades"); return
    ts = L.idx[E.t.values]; day = L.day[E.t.values]; race = L.race[E.j.values]
    d = E.groupby(day).net.mean()
    tr, te = E[ts < L.SPLIT], E[ts >= L.SPLIT]
    ds = E.s * np.where(L.party[E.j.values] == "D", 1, -1)
    block = pd.Series(E.net.values).groupby(day).mean()
    print(f"{nm:40s} n {len(E):3d} races {len(set(race)):3d} net {100*E.net.mean():5.2f}c median {100*E.net.median():5.2f}c win {100*(E.net>0).mean():3.0f}% train {100*tr.net.mean():5.2f} ({len(tr)}) test {100*te.net.mean():5.2f} ({len(te)}) days+ {(d>0).sum()}/{len(d)} D-side {100*E.net[ds>0].mean():5.2f} ({(ds>0).sum()}) R-side {100*E.net[ds<0].mean():5.2f} ({(ds<0).sum()}) worst {100*E.net.min():5.1f}")


print("STRICT MECHANICS")
Es = events(slow, Sv, 288, 288); rep("slow, 24 h hold", Es)
rep("slow, 36 h hold", events(slow, Sv, 432, 288)); rep("slow, 12 h hold", events(slow, Sv, 144, 288))
for hn, h in (("2h", 24), ("4h", 48), ("8h", 96), ("24h", 288)):
    rep(f"fast 30 min trigger, {hn} hold", events(fast, Dv, h, max(h, 48)))
    rep(f"fast 1 h trigger, {hn} hold", events(fast1, Dv1, h, max(h, 48)))
Ef = events(fast, Dv, 48, 48)
print("office split, slow:", {o: (int((L.office[Es.j] == o).sum()), round(100 * Es.net[L.office[Es.j] == o].mean(), 2)) for o in ("house", "governor", "senate")})
print("office split, fast 4h:", {o: (int((L.office[Ef.j] == o).sum()), round(100 * Ef.net[L.office[Ef.j] == o].mean(), 2)) for o in ("house", "governor", "senate")})
# delay test for the fast rule: entry 5, 15, 30 min later
for dl in (0, 3, 6, 12):
    E = Ef.copy(); v = []
    for r in E.itertuples():
        e = first_real(r.e + dl, r.j, 6)
        if e is None or e + 48 >= n: continue
        x = first_real(e + 48, r.j)
        if x is None: continue
        v.append((Bv[x, r.j] - Av[e, r.j]) if r.s > 0 else (Bv[e, r.j] - Av[x, r.j]))
    print(f"fast 4 h, entry delayed {dl*5} min: net {100*np.mean(v):.2f}c n {len(v)}")
# book
H_ = .004   # selection haircut per share, audit section 7


def book(N, CAP, rules=("slow", "fast"), haircut=H_, c1k=.02, D_cap=None, D0=0.0, fast_only_mid=True):
    cand = []
    if "slow" in rules:
        cand += [(r.e, r.x, r.j, r.s, r.net, "slow") for r in Es.itertuples()]
    if "fast" in rules:
        cand += [(r.e, r.x, r.j, r.s, r.net, "fast") for r in Ef.itertuples()]
    cand.sort()
    op, used, nd, res = [], 0.0, D0, []
    for e, x, j, sg, net, rule in cand:
        still = []
        for p in op:
            if p[0] <= e:
                used -= N; nd -= p[1] * N
            else:
                still.append(p)
        op = still
        if any(p[2] == L.race[j] for p in op) or used + N > CAP: continue
        ds = sg * (1 if L.party[j] == "D" else -1 if L.party[j] == "R" else 0)
        if D_cap is not None and abs(nd + ds * N) > D_cap and abs(nd + ds * N) > abs(nd): continue
        p0 = (Av[e, j] if sg > 0 else 1 - Bv[e, j])
        sh = N / p0
        cost = 2 * c1k * sh / 1000 / 100
        pl = (net - haircut - cost) * sh
        res.append(dict(e=e, day=L.day[e], pl=pl, rule=rule, ds=ds)); op.append((x, ds, L.race[j])); used += N; nd += ds * N
    return pd.DataFrame(res)


rows = []
for lab, kw in (("2.5k a trade, 40k capital", dict(N=2500, CAP=40000)), ("5k, 40k", dict(N=5000, CAP=40000)), ("5k, 80k", dict(N=5000, CAP=80000)), ("10k, 40k", dict(N=10000, CAP=40000)), ("10k, 80k", dict(N=10000, CAP=80000)), ("20k, 80k", dict(N=20000, CAP=80000)), ("40k, 80k (1-2 big trades)", dict(N=40000, CAP=80000)), ("5k, 40k, no haircut", dict(N=5000, CAP=40000, haircut=0)), ("10k, 80k, no haircut", dict(N=10000, CAP=80000, haircut=0)), ("10k, 80k, costs doubled", dict(N=10000, CAP=80000, c1k=.04)), ("5k, 40k, costs doubled", dict(N=5000, CAP=40000, c1k=.04)), ("5k, 40k, net D cap 5k from +4.9k", dict(N=5000, CAP=40000, D_cap=5000, D0=4900)), ("5k, 40k, net D cap 20k", dict(N=5000, CAP=40000, D_cap=20000)), ("slow only 5k 40k", dict(N=5000, CAP=40000, rules=("slow",))), ("fast only 5k 40k", dict(N=5000, CAP=40000, rules=("fast",)))):
    Q = book(**kw)
    d = Q.groupby("day").pl.sum() if len(Q) else pd.Series(dtype=float)
    rows.append(dict(case=lab, trades=len(Q), net_pct=100 * (Q.pl / kw["N"]).mean() if len(Q) else 0, win=(Q.pl > 0).mean() if len(Q) else 0, total=Q.pl.sum() if len(Q) else 0, per_day=(Q.pl.sum() / 8.5) if len(Q) else 0, to_3Nov=(Q.pl.sum() / 8.5 * 3.9) if len(Q) else 0, worst_day=d.min() if len(d) else 0, worst_trade=Q.pl.min() if len(Q) else 0, days=f"{int((d>0).sum())}/{len(d)}"))
R = pd.DataFrame(rows); print("\nBOOK after strict mechanics, selection haircut 0.4c, own-order costs"); print(R.round(2).to_string(index=False))
R.to_pickle("data/research_diralpha/book_final.pkl")
Es.to_pickle("data/research_diralpha/strict_slow.pkl"); Ef.to_pickle("data/research_diralpha/strict_fast.pkl")
