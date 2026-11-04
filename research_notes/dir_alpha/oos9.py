"""Lag trades by where they start against the compression line. Signed distance > 0: the leg is cheap against the line and the lag signal says buy, or rich and it says sell. Both periods, nothing fitted."""
import pickle, sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
pd.set_option("display.width", 200, "display.max_rows", 100)
L = Lab()
A = pickle.load(open("data/research_diralpha/oos/panel.pkl", "rb"))
def resid(Mv, Fv, office, dis):
    ok = np.isfinite(Mv) & np.isfinite(Fv) & (office != "chamber")[None, :] & (Fv > .03) & (Fv < .97) & dis
    n = Mv.shape[0]; k = np.full(n, np.nan)
    for t in range(0, n, 12):
        o = ok[t]
        if o.sum() > 60: k[t] = np.sum((Mv[t, o] - .5) * (Fv[t, o] - .5)) / np.sum((Fv[t, o] - .5) ** 2)
    k = pd.Series(k).ffill().rolling(72, min_periods=1).mean().shift(1).values
    return Mv - (.5 + k[:, None] * (Fv - .5))
ofA = np.asarray(A["office"], dtype=object); ofB = np.asarray(L.office, dtype=object)
RA = resid(A["S"].values, ((A["P"] + A["K"]) / 2).values, ofA, (np.abs(A["P"].values - A["K"].values) <= .04))
RB = resid(L.mid.values, ((L.pm + L.k) / 2).values, ofB, (np.abs(L.pm.values - L.k.values) <= .04))
bins = [-1, -.02, -.005, .005, .02, 1]
def cut(lab, E, R, net="net"):
    E = E.copy(); E["sd"] = -E.s.values * R[E.t.values, E.j.values]
    print(lab); print(E.groupby(pd.cut(E.sd, bins), observed=True)[net].agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
    return E
O = "data/research_diralpha/oos/"
a = cut("slow, 3-20 Oct", pd.read_pickle(O + "slow24.pkl"), RA)
Fa = pd.read_pickle(O + "fast6.pkl"); Mv = A["S"].values; x = np.minimum(Fa.e.values + 96, Mv.shape[0] - 1); Fa["net8"] = Fa.s * (Mv[x, Fa.j.values] - Mv[Fa.e.values, Fa.j.values]) - .0075
cut("fast 8 h, 3-20 Oct", Fa.dropna(subset=["net8"]), RA, "net8")
b = cut("slow strict, 21-30 Oct", pd.read_pickle("data/research_diralpha/strict_slow.pkl"), RB)
cut("fast strict, 21-30 Oct", pd.read_pickle("data/research_diralpha/strict_fast.pkl"), RB)
for lab, q in (("3-20 Oct", a), ("21-30 Oct", b)):
    print(lab, "own side by signed distance"); q = q.copy()
    q["own"] = (np.where(q.s > 0, (A["S"].values if lab[0] == "3" else L.mid.values)[q.t.values, q.j.values], 1 - (A["S"].values if lab[0] == "3" else L.mid.values)[q.t.values, q.j.values]))
    print(q.groupby(q.own >= .7).sd.mean().mul(100).round(2).to_dict())
