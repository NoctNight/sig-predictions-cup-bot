"""Drill-down 13: combined book. Slow rule (24 h filtered signal, exit at gap overshoot or 36 h) plus fast rule (30 min trigger, 25-75, exit at 4 h).
One open position per race, capital limit, real run costs both ways, optional cap on net D, edge haircut. Saves day-by-day numbers for the plot."""
import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, H
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
n, m = mid.shape
nochm = L.office != "chamber"
S48 = gap - gap.rolling(576, min_periods=288).median()
s = np.sign(S48)
both = (L.lag(L.pm, 288) * s >= .01) & (L.lag(L.k, 288) * s >= .01)
flowok = L.rsum(P["sflow"], 288) * s >= -5000
M1 = (((mid >= .15) & (mid <= .85)) & both & flowok).values.copy(); M1[:, ~nochm] = False
slow = (M1 & (np.abs(np.nan_to_num(S48.values)) >= .02))
W = 6
dp, dk, dm, df = L.lag(L.pm, W), L.lag(L.k, W), L.lag(mid, W), L.lag(fair, W)
sf = np.sign(df)
fast = (((dp * sf >= .015) & (dk * sf >= .015) & ((dm * sf) < .5 * (df * sf)) & (mid >= .25) & (mid <= .75))).values.copy(); fast[:, ~nochm] = False
Sv, Dv = S48.values, df.values
Bv, Av = L.B, L.A
A0, B1 = -0.232, 0.078          # d10: cents over the touch = A0 + B1 sqrt(thousand shares)


def slip(sh, mult=1.0):
    return max(0.0, mult * (A0 + B1 * np.sqrt(sh / 1000))) / 100


def run(N, CAP, rules=("slow", "fast"), mult=1.0, D_cap=None, D0=0.0, days=None, start=0):
    op, used, res, netD, last = [], 0.0, [], D0, {}
    for t in range(start, n):
        keep = []
        for p in op:
            val = Bv[t, p["j"]] if p["s"] > 0 else 1 - Av[t, p["j"]]
            done = t >= p["end"] or t == n - 1
            if p["rule"] == "slow" and np.isfinite(val) and not done:
                sv = p["s"] * Sv[t, p["j"]]
                done = np.isfinite(sv) and sv <= -.5 * p["sig"]
            if done and np.isfinite(val):
                pl = (val - slip(p["sh"], mult) - p["px"]) * p["sh"]
                res.append(dict(t=t, i=p["i"], pl=pl, N=N, rule=p["rule"], race=p["race"], ds=p["ds"], day=L.day[t]))
                used -= N; netD -= p["ds"] * N
            else:
                keep.append(p)
        op = keep
        cand = []
        if "fast" in rules:
            for j in np.flatnonzero(fast[t]): cand.append((abs(Dv[t, j]) + .01, j, "fast", 1.0 if Dv[t, j] > 0 else -1.0))
        if "slow" in rules:
            for j in np.flatnonzero(slow[t]): cand.append((abs(Sv[t, j]), j, "slow", 1.0 if Sv[t, j] > 0 else -1.0))
        for sc, j, rule, sg in sorted(cand, key=lambda x: -x[0]):
            r = L.race[j]
            if any(p["race"] == r for p in op) or t - last.get(r, -10**9) < 96: continue
            if used + N > CAP: break
            px0 = Av[t, j] if sg > 0 else 1 - Bv[t, j]
            if not np.isfinite(px0): continue
            ds = sg * (1 if L.party[j] == "D" else -1 if L.party[j] == "R" else 0)
            if D_cap is not None and ds * (netD + ds * N) > D_cap and ds * ds * 1 > 0 and abs(netD + ds * N) > abs(netD): continue
            sh = N / px0
            op.append(dict(j=j, i=t, s=sg, sig=abs(Sv[t, j]) if rule == "slow" else 0, px=px0 + slip(sh, mult), sh=sh, race=r, rule=rule, ds=ds, end=t + (432 if rule == "slow" else 48)))
            used += N; netD += ds * N; last[r] = t
    return pd.DataFrame(res)


rows = []
for lab, kw in (("slow only, 40k, 5k a trade", dict(N=5000, CAP=40000, rules=("slow",))), ("fast only, 40k, 5k", dict(N=5000, CAP=40000, rules=("fast",))), ("both, 40k, 5k", dict(N=5000, CAP=40000)), ("both, 80k, 5k", dict(N=5000, CAP=80000)), ("both, 80k, 10k", dict(N=10000, CAP=80000)), ("both, 80k, 20k", dict(N=20000, CAP=80000)), ("both, 80k, 40k (two big trades)", dict(N=40000, CAP=80000)), ("both, 18k free cash, 2.5k", dict(N=2500, CAP=3000)), ("both, 80k, 10k, costs x2", dict(N=10000, CAP=80000, mult=2)), ("both, 80k, 10k, costs x3", dict(N=10000, CAP=80000, mult=3)), ("both, 80k, 10k, net D cap 5k from +4.9k", dict(N=10000, CAP=80000, D_cap=5000, D0=4900)), ("both, 80k, 5k, net D cap 5k from +4.9k", dict(N=5000, CAP=80000, D_cap=5000, D0=4900)), ("both, 80k, 10k, net D cap 20k", dict(N=10000, CAP=80000, D_cap=20000)), ("both, 80k, 10k, net D cap 40k", dict(N=10000, CAP=80000, D_cap=40000))):
    Q = run(**kw)
    if Q.empty: rows.append(dict(case=lab, trades=0)); continue
    d = Q.groupby("day").pl.sum(); tt = L.idx[Q.i.values]
    rows.append(dict(case=lab, trades=len(Q), net_pct=100 * (Q.pl / Q.N).mean(), win=(Q.pl > 0).mean(), total=Q.pl.sum(), per_day=Q.pl.sum() / 8.5, train=Q[tt < L.SPLIT].pl.sum(), test=Q[tt >= L.SPLIT].pl.sum(), worst_day=d.min(), worst_trade=Q.pl.min(), days_up=f"{int((d>0).sum())}/{len(d)}"))
print(pd.DataFrame(rows).round(2).to_string(index=False))
Q = run(N=10000, CAP=80000)
Q["ts"] = L.idx[Q.t.values]
Q = Q.sort_values("ts")
Q.to_pickle("data/research_diralpha/book.pkl")
print(Q.groupby("rule").pl.agg(["size", "sum", "mean"]).round(0).to_string())
print(Q.groupby("day").pl.sum().round(0).to_dict())
# worst run of the book
c = Q.pl.cumsum(); print("max drawdown of closed trades", round((c - c.cummax()).min(), 0))
