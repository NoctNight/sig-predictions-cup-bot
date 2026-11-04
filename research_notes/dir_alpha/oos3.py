"""Exit rule and wave filter on unseen days, 3-20 Oct. Path of the trade by hours held, take-profit and stop, wave count at entry. Last-trade prices, 0.75c assumed round trip."""
import pickle, numpy as np, pandas as pd
pd.set_option("display.width", 200, "display.max_rows", 100)
O = "data/research_diralpha/oos/"
P = pickle.load(open(O + "panel.pkl", "rb")); S = P["S"]; Mv = S.values; n = Mv.shape[0]; race = P["race"]; office = P["office"]; party = P["party"]
dsign = np.where(party == "D", 1.0, np.where(party == "R", -1.0, 0.0))
Es = pd.read_pickle(O + "slow36.pkl"); Ef = pd.read_pickle(O + "fast6.pkl")
HR = (1, 2, 4, 6, 8, 12, 16, 20, 24, 30, 36, 48)
rows = {}
for nm, E in (("slow", Es), ("fast", Ef)):
    tab = []
    for h in HR:
        x = E.e.values + 12 * h; ok = x < n
        g = E.s.values[ok] * (Mv[x[ok], E.j.values[ok]] - Mv[E.e.values[ok], E.j.values[ok]])
        g = g[np.isfinite(g)]
        tab.append(dict(hours=h, n=len(g), right=round((g > 0).mean(), 2), gross=round(100 * g.mean(), 2), net=round(100 * g.mean() - .75, 2), se=round(100 * g.std() / np.sqrt(len(g)), 2)))
    T = pd.DataFrame(tab); T["per_hour"] = (T.gross / T.hours).round(3); rows[nm] = T; print(nm); print(T.to_string(index=False))
pd.to_pickle(rows, O + "paths.pkl")
def exitsim(E, lim, tp=None, sl=None):
    v, hrs = [], []
    for r in E.itertuples():
        e, j, s = r.e, r.j, r.s; px = Mv[e, j]; xv = None
        for i in range(e + 1, min(e + lim + 1, n)):
            if not np.isfinite(Mv[i, j]): continue
            g = s * (Mv[i, j] - px); last, li = g, i
            if tp is not None and g >= tp: xv = g; break
            if sl is not None and g <= -sl: xv = g; break
        if xv is None: xv = last
        v.append(xv); hrs.append((li - e) / 12)
    v = np.array(v); return f"net {100*v.mean()-.75:.2f}c win {(v>.0075).mean():.2f} hours {np.mean(hrs):.1f} worst {100*v.min():.1f}c"
for nm, E, lim in (("slow", Es, 432), ("fast", Ef, 96)):
    print(nm, "hold only:", exitsim(E, lim))
    for tp, sl in ((.025, None), (.035, None), (.05, None), (None, .015), (None, .03), (.035, .03)):
        print(f"  take {tp} stop {sl}:", exitsim(E, lim, tp, sl))
# flat or down at 12 h on slow: does holding pay?
x = Es.e.values + 144; ok = x + 288 < n
g12 = Es.s.values[ok] * (Mv[x[ok], Es.j.values[ok]] - Mv[Es.e.values[ok], Es.j.values[ok]])
more = Es.s.values[ok] * (Mv[np.minimum(x[ok] + 288, n - 1), Es.j.values[ok]] - Mv[x[ok], Es.j.values[ok]])
d = pd.DataFrame(dict(g=g12, m=more)).dropna(); print("slow, gain so far at 12 h, then the next 24 h:"); print(d.groupby(pd.cut(d.g, [-1, -.01, 0, .01, .025, 1])).m.agg(n="size", more=lambda v: 100 * v.mean()).round(2).to_string())
# wave
fair = (P["P"] + P["K"]) / 2; gap = fair - S; S48 = (gap - gap.rolling(576, min_periods=288).median()).values
dp = (P["P"] - P["P"].shift(288)).values; dk = (P["K"] - P["K"].shift(288)).values
ok = (np.abs(S48) >= .01) & (Mv >= .15) & (Mv <= .85) & (dp * np.sign(S48) >= .005) & (dk * np.sign(S48) >= .005); ok[:, office == "chamber"] = False
D = np.sign(np.nan_to_num(S48)) * dsign * ok
up = (pd.DataFrame(D > 0).groupby(race, axis=1).max().sum(axis=1)).values if False else None
races = pd.factorize(race)[0]; nr = races.max() + 1
ru = np.zeros((n, nr)); rd = np.zeros((n, nr))
for j in range(D.shape[1]): ru[:, races[j]] = np.maximum(ru[:, races[j]], D[:, j] > 0); rd[:, races[j]] = np.maximum(rd[:, races[j]], D[:, j] < 0)
cu, cd = ru.sum(1), rd.sum(1)
for nm, E in (("slow", Es), ("fast", Ef)):
    E = E.copy(); E["wave"] = (cu[E.t.values] - cd[E.t.values]) * E.ds.values - 1
    print(nm, "by wave count"); print(E.groupby(pd.cut(E.wave, [-99, -5, 0, 3, 7, 99])).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
