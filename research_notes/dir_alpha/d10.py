"""Drill-down 10: what big taker runs really paid (VWAP against the touch before the run), then the portfolio with that cost.
Capital limit, one bet per race, best signals first, exits by the rule found in d5 (sell when the fresh gap has closed, or at 36 h)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 40)
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
tp = pd.read_pickle("data/research_diralpha/tape.pkl")
tp = tp[tp.aggressor.isin(["buy", "sell"]) & (tp.ts >= L.idx[0])].sort_values("ts")
tp["q"] = tp.quantity.abs(); tp["sgn"] = np.where(tp.aggressor == "buy", 1, -1)
rows = []
for ex, g in tp.groupby("exchange_id"):
    if ex not in L.cols: continue
    j = L.cols.index(ex)
    t, q, s, p = g.ts.values, g.q.values, g.sgn.values, g.price.values
    st = 0
    for i in range(1, len(t) + 1):
        if i == len(t) or s[i] != s[st] or t[i] - t[i - 1] > 90:
            sh = q[st:i].sum()
            if sh >= 2000:
                b = np.searchsorted(L.idx, t[st]) - 1
                if b >= 0:
                    a0, b0 = L.A[b, j], L.B[b, j]
                    if np.isfinite(a0) and np.isfinite(b0) and .1 <= (a0 + b0) / 2 <= .9:
                        vw = (q[st:i] * p[st:i]).sum() / sh
                        rows.append(dict(j=j, office=L.office[j], sh=sh, secs=t[i - 1] - t[st], prints=i - st, side=s[st], slip=100 * (s[st] * vw - (a0 if s[st] > 0 else -b0)), worst=100 * (s[st] * p[st:i].max() if s[st] > 0 else -p[st:i].min()) - 100 * (a0 if s[st] > 0 else -b0), spr=100 * (a0 - b0), mid=(a0 + b0) / 2))
                st = i
            else:
                st = i
R = pd.DataFrame(rows)
print("runs", len(R))
R["size"] = pd.cut(R.sh, [1999, 5000, 10000, 20000, 40000, 80000, 200000, 1e8])
print("VWAP above the pre-run touch, cents per share (negative = better than the old touch):")
print(R.groupby(["office", "size"], observed=True).agg(n=("sh", "size"), shares=("sh", "median"), secs=("secs", "median"), prints=("prints", "median"), slip=("slip", "mean"), slip_med=("slip", "median"), p90=("slip", lambda x: x.quantile(.9)), worst_px=("worst", "median")).round(2).to_string())
f = R[(R.office != "chamber")]
x = np.sqrt(f.sh.values / 1000); X = np.c_[np.ones(len(x)), x]
b = np.linalg.lstsq(X, f.slip.values, rcond=None)[0]
print("fit: slip c = %.3f + %.3f * sqrt(thousand shares)" % tuple(b))
A0, B1 = b
S = gap - gap.rolling(576, min_periods=288).median()
s = np.sign(S)
both = (L.lag(L.pm, 288) * s >= .01) & (L.lag(L.k, 288) * s >= .01)
flowok = L.rsum(P["sflow"], 288) * s >= -5000
M = (((mid >= .15) & (mid <= .85)) & both & flowok).values.copy()
M[:, L.office == "chamber"] = False
Sv = S.values
BB, AA = L.B, L.A
n, m = Sv.shape
races = L.race
lam = .012 / 1000          # lasting impact, cents per share (d9)


def run(N, CAP, th=.02, tmax=36 * 12, D_cap=None, dside_only=None, cost_mult=1.0, close=-.5, D0=0.0):
    """Walk time forward. Enter top signals while capital remains. Exit when the fresh gap has fallen to close x entry gap, or at tmax."""
    openp = []; cash_used = 0.0; pnl = []; netD = D0
    cand = np.argwhere(M & (np.abs(Sv) >= th))
    bytime = {}
    for i, j in cand:
        bytime.setdefault(i, []).append(j)
    last_race = {}
    for t in range(n):
        keep = []
        for p in openp:
            val = (BB[t, p["j"]] if p["s"] > 0 else 1 - AA[t, p["j"]])
            done = t >= p["i"] + tmax or t == n - 1
            if np.isfinite(val) and not done:
                sv = p["s"] * Sv[t, p["j"]]
                if np.isfinite(sv) and sv <= close * p["sig"] and t > p["i"]:
                    done = True
            if done and np.isfinite(val):
                extra = cost_mult * (A0 + B1 * np.sqrt(p["sh"] / 1000) if p["sh"] > 2000 else 0) / 100
                pl = (val + extra * 0 - p["px"]) * p["sh"] - max(0, extra) * p["sh"]
                pnl.append(dict(t=t, i=p["i"], pl=pl, ret=pl / p["N"], race=p["race"], held=(t - p["i"]) / 12, ds=p["ds"], sh=p["sh"], j=p["j"], day=L.day[p["i"]]))
                cash_used -= p["N"]; netD -= p["ds"] * p["N"]
            else:
                keep.append(p)
        openp = keep
        for j in sorted(bytime.get(t, []), key=lambda k: -abs(Sv[t, k])):
            r = races[j]
            if any(p["race"] == r for p in openp) or t - last_race.get(r, -10**9) < 288: continue
            if cash_used + N > CAP: break
            sg = 1.0 if Sv[t, j] > 0 else -1.0
            px = AA[t, j] if sg > 0 else 1 - BB[t, j]
            if not np.isfinite(px): continue
            ds = sg * (1 if L.party[j] == "D" else -1 if L.party[j] == "R" else 0)
            if dside_only and ds != dside_only: continue
            if D_cap is not None and ds * (netD + ds * N) > D_cap: continue
            sh = N / px
            extra = cost_mult * (A0 + B1 * np.sqrt(sh / 1000) if sh > 2000 else 0) / 100
            openp.append(dict(j=j, i=t, s=sg, sig=abs(Sv[t, j]), px=px + max(0, extra), sh=sh, N=N, race=r, ds=ds))
            cash_used += N; netD += ds * N; last_race[r] = t
    Q = pd.DataFrame(pnl)
    return Q


rows = []
for CAP, N in ((10000, 2500), (20000, 5000), (20000, 2500), (40000, 5000), (40000, 10000), (80000, 10000), (80000, 20000), (80000, 40000), (40000, 20000), (40000, 40000)):
    Q = run(N, CAP)
    if Q.empty: continue
    d = Q.groupby("day").pl.sum()
    ts = L.idx[Q.i.values]
    rows.append(dict(capital=CAP, per_trade=N, trades=len(Q), net_pct=100 * Q.ret.mean(), win=(Q.pl > 0).mean(), hours=Q.held.mean(), total=Q.pl.sum(), per_day=Q.pl.sum() / 8.5, worst_trade=Q.pl.min(), train=Q[ts < L.SPLIT].pl.sum(), test=Q[ts >= L.SPLIT].pl.sum(), days_up=f"{int((d>0).sum())}/{len(d)}"))
print("\nPortfolio, realised run costs applied both ways:"); print(pd.DataFrame(rows).round(2).to_string(index=False))
print("\nStress on the 80k / 20k case:")
for lab, kw in (("cost x2", dict(cost_mult=2)), ("cost x3", dict(cost_mult=3)), ("signal >= 3c only", dict(th=.03)), ("24 h hold, no gap exit", dict(tmax=288, close=-99)), ("12 h hold", dict(tmax=144, close=-99)), ("D-side trades only", dict(dside_only=1)), ("R-side trades only", dict(dside_only=-1)), ("net D capped at 5,000", dict(D_cap=5000)), ("net D capped at 5,000, starting at +4,900", dict(D_cap=5000, D0=4900)), ("net D capped at 40k", dict(D_cap=40000))):
    Q = run(20000, 80000, **kw)
    if Q.empty: print(lab, "no trades"); continue
    d = Q.groupby("day").pl.sum()
    print(f"{lab:42s} trades {len(Q):3d} net {100*Q.ret.mean():5.2f}% win {(Q.pl>0).mean():.2f} hours {Q.held.mean():4.1f} total {Q.pl.sum():8.0f} worst {Q.pl.min():7.0f} days+ {int((d>0).sum())}/{len(d)}")
