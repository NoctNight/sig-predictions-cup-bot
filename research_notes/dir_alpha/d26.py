"""Drill-down 26: is the basket real selection or just the D drift of 27-30 Oct?
 For each basket moment: the mid move of basket legs (D-way) against every other leg priced 15-85 in the same office mix, same window.
 Also the basket trigger at every moment, not just the first in 24 h, and the same thing for R-way baskets at a lower count, to find any R-way evidence."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); P = L.P
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
mid, fair, pm, k = L.mid, L.fair, L.pm, L.k
gap = L.gap; S48 = gap - gap.rolling(576, min_periods=288).median()
Sv, Bv, Av, Mv = S48.values, L.B, L.A, mid.values
n, m = Mv.shape; dsign = L.dsign.values; nochm = L.office != "chamber"
dP, dK = (pm - pm.shift(288)).values, (k - k.shift(288)).values
h = 288
for th, vm, N in ((.01, .005, 8), (.015, .005, 5), (.01, .005, 3), (.015, .005, 3)):
    ok = (np.abs(Sv) >= th) & (Mv >= .15) & (Mv <= .85) & (dP * np.sign(Sv) >= vm) & (dK * np.sign(Sv) >= vm); ok[:, ~nochm] = False
    D = np.nan_to_num(Sv) * dsign
    res = []
    for way, M_ in ((1, ok & (D > 0)), (-1, ok & (D < 0))):
        last = -10**9
        for t in range(576, n - h - 1):
            if t - last < 288: continue
            best = {}
            for j in np.flatnonzero(M_[t]):
                r = L.race[j]
                if r not in best or abs(Sv[t, j]) > abs(Sv[t, best[r]]): best[r] = j
            if len(best) < N: continue
            legs = np.array(list(best.values()))
            mv = (Mv[t + 1 + h] - Mv[t + 1]) * dsign * way                # D-way move of every leg in the window
            band = (Mv[t + 1] >= .15) & (Mv[t + 1] <= .85) & nochm & np.isfinite(mv) & (dsign != 0)
            inb = np.zeros(m, bool); inb[legs] = True
            rs = set(L.race[legs]); rest = band & ~np.isin(L.race, list(rs))
            sg = np.sign(Sv[t, legs]); e = t + 1; x = e + h
            nets = np.where(sg > 0, Bv[x, legs] - Av[e, legs], Bv[e, legs] - Av[x, legs])
            last = t
            res.append((L.day[t], way, len(legs), 100 * np.nanmean(nets), 100 * np.nanmean(mv[legs]), 100 * np.nanmean(mv[rest]), int(rest.sum())))
    R = pd.DataFrame(res, columns=["day", "way", "legs", "net", "basket_move", "rest_move", "nrest"])
    print(f"gap>={th*100:.1f}c venues>={vm*100:.1f}c N>={N}")
    if len(R):
        R["excess"] = R.basket_move - R.rest_move
        print(R.round(2).to_string(index=False)); print("mean excess %.2fc, mean net %.2fc" % (np.average(R.excess, weights=R.legs), np.average(R.net, weights=R.legs)))
# all moments, overlapping: does the count of same-way legs predict the rest of the market's next 24 h D move? (is a wave a national signal)
c_up = np.zeros(n); c_dn = np.zeros(n)
ok = (np.abs(Sv) >= .01) & (Mv >= .15) & (Mv <= .85) & (dP * np.sign(Sv) >= .005) & (dK * np.sign(Sv) >= .005); ok[:, ~nochm] = False
D = np.nan_to_num(Sv) * dsign
c_up = (ok & (D > 0)).sum(1); c_dn = (ok & (D < 0)).sum(1)
house = (L.office == "house") & (dsign != 0)
idx = pd.Series(np.nanmean((Mv * dsign)[:, house & (np.nanmean(Mv, 0) > .15) & (np.nanmean(Mv, 0) < .85)], axis=1))
fw = idx.shift(-288) - idx
df = pd.DataFrame(dict(net=c_up - c_dn, fw=fw * 100, day=L.day)).dropna()
print(df.groupby(pd.cut(df.net, [-99, -5, 0, 5, 10, 20, 99]), observed=True).fw.agg(n="size", move="mean").round(2).to_string())
print("by day: mean wave count, next 24 h House D move"); print(df.groupby("day")[["net", "fw"]].mean().round(2).to_string())
