"""Drill-down 38. A sum of a race's mids above 1: do the rich legs fall? filter on strict trades. B overshoot: SIG copied 130%+ of a 3c+ both-venue move in 24 h: fade.
C outside volatility regime on strict trades. D same-race gap dispersion: outside D minus SIG D against outside R minus SIG R disagree (leg-specific error, not a race move)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); P = L.P; pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
mid = L.mid; Mv = mid.values; n, m = Mv.shape
races = pd.Series(L.race)
sumrace = mid.T.groupby(L.race).sum(min_count=2).T          # bins x races
Sm = sumrace[L.race].values                                  # bins x legs
nleg = races.map(races.value_counts()).values
Es = pd.read_pickle("data/research_diralpha/r3_slow.pkl"); Ef = pd.read_pickle("data/research_diralpha/r3_fast.pkl")
vol = (L.fair - L.fair.shift(12)).abs().rolling(288, min_periods=100).mean().mean(axis=1).values * 100
for nm, E in (("slow", Es), ("fast", Ef)):
    E = E.copy(); j, t = E.j.values, E.t.values
    E["over"] = (Sm[t, j] - 1) * 100 * E.s      # + means we buy into a rich race (buying YES when sum>1, or NO when sum<1)
    print(nm, "by sum of race mids minus 1, signed to our side (cents)"); print(E.groupby(pd.cut(E.over, [-20, -1, -.25, .25, 1, 20]), observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
    E["vol"] = vol[t]; print(nm, "by outside busyness (mean abs 1 h move, cents)"); print(E.groupby(pd.qcut(E.vol, 3, duplicates="drop"), observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
# generic: rich-sum races, next move of legs by gap sign
S = pd.DataFrame(0.0, index=mid.index, columns=mid.columns)
ok2 = nleg == 2
sv = Sm - 1
A = pd.DataFrame(np.where((sv >= .015) & ok2[None, :] & (L.gap.values < 0), -1.0, 0.0), index=mid.index, columns=mid.columns)
for h in (12, 48, 144):
    tr = L.trades(A, h, thr=.5, cool=144); r = L.score(tr) if tr is not None else None
    if r: print(f"race sum 1.015+, sell the leg rich against outside, hold {h//12} h: n {r['n']} races {r['races']} gross {r['gross_c']:.2f}c net {r['net_c']:.2f}c win {r['win']:.2f} t_day {r['t_day']:.1f}")
dF, dM = (L.fair - L.fair.shift(288)).values, (mid - mid.shift(288)).values
dP, dK = (L.pm - L.pm.shift(288)).values, (L.k - L.k.shift(288)).values
for mv_, c in ((.03, 1.3), (.03, 1.6), (.05, 1.3)):
    M = (np.abs(dF) >= mv_) & (np.sign(dP) == np.sign(dF)) & (np.sign(dK) == np.sign(dF)) & (dM * np.sign(dF) >= c * np.abs(dF)) & (Mv >= .15) & (Mv <= .85)
    M[:, L.office == "chamber"] = False
    F = pd.DataFrame(np.where(M, -np.sign(dF), 0.0), index=mid.index, columns=mid.columns)
    for h in (48, 144, 288):
        tr = L.trades(F, h, thr=.5, cool=288); r = L.score(tr) if tr is not None else None
        if r: print(f"overshoot move>={mv_*100:.0f}c copied>={c:.1f}: fade {h//12} h n {r['n']} races {r['races']} gross {r['gross_c']:.2f}c net {r['net_c']:.2f}c win {r['win']:.2f} t_day {r['t_day']:.1f}")
