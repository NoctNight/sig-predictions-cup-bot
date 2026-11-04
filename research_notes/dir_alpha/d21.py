"""Drill-down 21: a simple confirmation count on the strict trades, leave-one-day-out honest by construction (no weights fitted): no public polls, toss-up rating, leg quote unchanged 2 h+, signal in the top two thirds."""
import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
pd.set_option("display.width", 200)
fm = json.load(open("params/fair_model.json"))["races"]
mv = L.mid.ffill().values
age = np.full(mv.shape, np.nan)
for j in range(mv.shape[1]):
    last = 0
    for i in range(1, mv.shape[0]):
        if np.isfinite(mv[i, j]) and mv[i, j] != mv[i - 1, j]: last = i
        age[i, j] = (i - last) / 12
gap = L.fair - L.mid
S48 = (gap - gap.rolling(576, min_periods=288).median()).values
df = L.lag(L.fair, 6).values
for name, f, sv, thr in (("slow", "strict_slow", S48, .0218), ("fast", "strict_fast", df, .0228)):
    E = pd.read_pickle(f"data/research_diralpha/{f}.pkl")
    r = L.race[E.j.values]
    ip = [fm.get(x, {}).get("inputs", {}) or {} for x in r]
    E["c1"] = [1 if p.get("n_polls") == 0 else 0 for p in ip]
    E["c2"] = [1 if (p.get("rating_margin") is not None and abs(p["rating_margin"]) <= 2) else 0 for p in ip]
    E["c3"] = (age[E.t.values, E.j.values] >= 2).astype(int)
    E["c4"] = (np.abs(sv[E.t.values, E.j.values]) >= thr).astype(int)
    E["score"] = E.c1 + E.c2 + E.c3 + E.c4
    E["tr"] = L.idx[E.t.values] < L.SPLIT
    t = E.groupby(E.score.clip(upper=3)).agg(n=("net", "size"), net=("net", lambda v: 100 * v.mean()), win=("net", lambda v: (v > 0).mean()))
    t["train"] = E[E.tr].groupby(E.score.clip(upper=3)).net.mean() * 100; t["test"] = E[~E.tr].groupby(E.score.clip(upper=3)).net.mean() * 100
    print(name); print(t.round(2).to_string())

# conviction sizing: expected profit to 3 Nov after haircut, cost 0.02c per 1,000 shares each way, no capital limit, then capital needed
print("\nConviction sizing (to 3 Nov, 3.9 of 8.5 days). Haircut 0.4c on low-count trades, 0.8c on 2+ because the count itself was found after looking.")
all_ = []
for name, f, sv, thr in (("slow", "strict_slow", S48, .0218), ("fast", "strict_fast", df, .0228)):
    E = pd.read_pickle(f"data/research_diralpha/{f}.pkl"); r = L.race[E.j.values]
    ip = [fm.get(x, {}).get("inputs", {}) or {} for x in r]
    E["score"] = np.array([1 if p.get("n_polls") == 0 else 0 for p in ip]) + np.array([1 if (p.get("rating_margin") is not None and abs(p["rating_margin"]) <= 2) else 0 for p in ip]) + (age[E.t.values, E.j.values] >= 2).astype(int) + (np.abs(sv[E.t.values, E.j.values]) >= thr).astype(int)
    E["rule"] = name; all_.append(E)
A = pd.concat(all_)
def val(lo, hi, h_lo=.004, h_hi=.008):
    t = 0
    for r in A.itertuples():
        sh = hi if r.score >= 2 else lo
        h = h_hi if r.score >= 2 else h_lo
        t += (r.net - h - 2 * .02 * sh / 1e5) * sh
    return t / 8.5 * 3.9
for lo, hi in ((10000, 10000), (5000, 10000), (5000, 20000), (5000, 30000), (0, 10000), (0, 20000), (0, 30000), (0, 40000), (3000, 40000)):
    print(f"low count {lo:6d} shares, 2+ count {hi:6d} shares: ${val(lo, hi):7.0f}")
print("trades 2+ count", int((A.score >= 2).sum()), "of", len(A), "per day", round((A.score >= 2).sum() / 8.5, 1), "net %.2fc" % (100 * A[A.score >= 2].net.mean()), "fast part %.2fc" % (100 * A[(A.score >= 2) & (A.rule == "fast")].net.mean()))

# the conviction book with the real limits: capital, one bet per race, net D cap
Bv, Av = L.B, L.A
def book(sh, CAP, D_cap=None, D0=4800.0, lo=0, hair=.008):
    c = sorted(A.itertuples(), key=lambda r: r.e)
    op, used, nd, res, skipD, skipC = [], 0.0, D0, [], 0, 0
    for r in c:
        size = sh if r.score >= 2 else lo
        if size == 0: continue
        keep = []
        for p in op:
            if p[0] <= r.e: used -= p[1]; nd -= p[2] * p[1]
            else: keep.append(p)
        op = keep
        race = L.race[r.j]
        if any(p[3] == race for p in op): continue
        px = Av[r.e, r.j] if r.s > 0 else 1 - Bv[r.e, r.j]
        N = size * px
        if used + N > CAP: skipC += 1; continue
        ds = r.s * (1 if L.party[r.j] == "D" else -1 if L.party[r.j] == "R" else 0)
        if D_cap is not None and abs(nd + ds * N) > D_cap and abs(nd + ds * N) > abs(nd): skipD += 1; continue
        pl = (r.net - hair - 2 * .02 * size / 1e5) * size
        res.append((r.e, pl, N, ds)); op.append((r.x, N, ds, race)); used += N; nd += ds * N
    R = pd.DataFrame(res, columns=["e", "pl", "N", "ds"])
    return dict(trades=len(R), to_3Nov=R.pl.sum() / 8.5 * 3.9, mean_N=R.N.mean(), win=(R.pl > 0).mean(), worst=R.pl.min(), D_share=(R.ds > 0).mean(), blocked_cap=skipD, blocked_capital=skipC)
rows = []
for lab, kw in (("20k shares, $40k capital, no cap", dict(sh=20000, CAP=40000)), ("20k shares, $60k, no cap", dict(sh=20000, CAP=60000)), ("30k shares, $60k, no cap", dict(sh=30000, CAP=60000)), ("10k shares, $40k, no cap", dict(sh=10000, CAP=40000)), ("20k shares, $60k, net D 5k cap", dict(sh=20000, CAP=60000, D_cap=5000)), ("10k shares, $40k, net D 5k cap", dict(sh=10000, CAP=40000, D_cap=5000)), ("5k shares, $20k, net D 5k cap", dict(sh=5000, CAP=20000, D_cap=5000)), ("20k shares, $60k, net D 15k cap", dict(sh=20000, CAP=60000, D_cap=15000)), ("20k shares, $60k, net D 30k cap", dict(sh=20000, CAP=60000, D_cap=30000)), ("20k shares + 5k low count, $60k, net D 30k", dict(sh=20000, CAP=60000, D_cap=30000, lo=5000))):
    rows.append(dict(case=lab, **book(**kw)))
print(pd.DataFrame(rows).round(2).to_string(index=False))
