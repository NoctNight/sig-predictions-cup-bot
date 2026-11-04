"""Weekends. Lag trades entered Saturday or Sunday UTC against weekdays, unseen days and the strict backtest. Path by hours held. Does a Friday-night signal wait for Monday?"""
import pickle, sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
pd.set_option("display.width", 200)
O = "data/research_diralpha/oos/"
A = pickle.load(open(O + "panel.pkl", "rb")); S = A["S"]; Mv = S.values; idx = S.index
L = Lab(); Bm = L.mid.values; ib = pd.to_datetime(L.idx, unit="s", utc=True)
def wk(i, t): return (i[t].dayofweek >= 5)
for nm, E, M, I, c in (("slow unseen", pd.read_pickle(O + "slow24.pkl"), Mv, idx, "e"), ("fast unseen", pd.read_pickle(O + "fast6.pkl"), Mv, idx, "e"), ("slow strict", pd.read_pickle("data/research_diralpha/strict_slow.pkl"), Bm, ib, "e"), ("fast strict", pd.read_pickle("data/research_diralpha/strict_fast.pkl"), Bm, ib, "e")):
    E = E.copy(); e = E[c].values; E["we"] = wk(I, e)
    rows = []
    for h in (4, 8, 12, 24, 36, 48):
        x = e + 12 * h; ok = x < M.shape[0]
        g = E.s.values[ok] * (M[x[ok], E.j.values[ok]] - M[e[ok], E.j.values[ok]]); w = E.we.values[ok]; f = np.isfinite(g)
        rows.append(dict(hours=h, wkday_n=int((~w & f).sum()), wkday=round(100 * np.nanmean(g[~w]), 2), wkend_n=int((w & f).sum()), wkend=round(100 * np.nanmean(g[w]), 2) if w.sum() else np.nan))
    print(nm, "move our way before spread (cents)"); print(pd.DataFrame(rows).to_string(index=False))
# copy speed: after a 24 h outside move of 2c+, how much SIG followed in the next 12 h, by weekday, unseen days
F = ((A["P"] + A["K"]) / 2); dF = (F - F.shift(288)).values; dS12 = (S.shift(-144) - S).values; gap = (F - S).values
sel = (np.abs(gap) >= .02) & (np.abs(dF) >= .02) & (np.sign(gap) == np.sign(dF))
d = pd.DataFrame(dict(dow=np.repeat(idx.dayofweek.values, Mv.shape[1]), m=(np.sign(gap) * dS12).ravel(), s=sel.ravel())); d = d[d.s].dropna()
print(d.groupby("dow").m.agg(n="size", follow=lambda v: 100 * v.mean()).round(2).to_string())
