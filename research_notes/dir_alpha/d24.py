"""Drill-down 24 (round 3, ideas 3-6, 9-11, 13, 14). Every cut is on the frozen strict trades, or a stated new rule on the same mechanics, so the numbers compare."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); P = L.P
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl")
mid, fair, pm, k = L.mid, L.fair, L.pm, L.k
gap = L.gap; S48 = gap - gap.rolling(576, min_periods=288).median()
Sv, Bv, Av, Mv, Gv = S48.values, L.B, L.A, mid.values, gap.values
n, m = Mv.shape
dsign = L.dsign.values
mate = {}
for r, g in pd.Series(range(m)).groupby(L.race):
    if len(g) == 2: a, b = g.values; mate[a] = b; mate[b] = a
sflow24 = L.rsum(P["sflow"], 288).values
dP24, dK24 = (pm - pm.shift(288)).values, (k - k.shift(288)).values
dF24 = (fair - fair.shift(288)).values; dM24 = (mid - mid.shift(288)).values
dF6, dM6 = (fair - fair.shift(6)).values, (mid - mid.shift(6)).values
tape = pd.read_pickle("data/research_diralpha/tape.pkl")


def cut(nm, E, col, bins=None, labels=None):
    x = E[col]
    g = pd.cut(x, bins, labels=labels) if bins is not None else x
    ts = L.idx[E.t.values]
    o = E.groupby(g, observed=True).agg(n=("net", "size"), net=("net", lambda v: 100 * v.mean()), win=("net", lambda v: (v > 0).mean()))
    o["train"] = E[ts < L.SPLIT].groupby(g, observed=True).net.mean() * 100
    o["test"] = E[ts >= L.SPLIT].groupby(g, observed=True).net.mean() * 100
    print(f"\n{nm} by {col}"); print(o.round(2).to_string())


for nm, E in (("slow", Es), ("fast", Ef)):
    E = E.copy()
    j, t = E.j.values, E.t.values
    E["ds"] = E.s * dsign[j]
    # 4 cluster: other legs (other races) with |S48|>=1c the same D-way right now
    Dw = np.nan_to_num(Sv) * dsign
    cl = []
    for tt, jj, ds in zip(t, j, E.ds):
        v = Dw[tt]; rs = L.race; same = (np.sign(v) == ds) & (np.abs(v) >= .01) & (L.office != "chamber"); same[rs == rs[jj]] = False
        opp = (np.sign(v) == -ds) & (np.abs(v) >= .01) & (L.office != "chamber"); opp[rs == rs[jj]] = False
        cl.append(same.sum() - opp.sum())
    E["cluster"] = cl
    cut(nm, E, "cluster", [-99, -2, 2, 8, 20, 300])
    # 5 mirror
    mir = []
    for tt, jj, s in zip(t, j, E.s):
        q = mate.get(jj); mir.append(np.nan if q is None else -s * Sv[tt, q])
    E["mirror"] = mir
    cut(nm, E, "mirror", [-1, .005, .01, .02, 1])
    # 6 big moves
    E["move24"] = np.abs(dF24[t, j]) * 100
    cut(nm, E, "move24", [0, 2, 3.5, 5, 8, 100])
    E["copied"] = np.clip(np.nan_to_num(dM24[t, j] / dF24[t, j]), -1, 2)
    cut(nm, E, "copied", [-1.01, 0, .25, .5, .75, 2.1])
    # 10 hour
    E["hour"] = L.hour[t]
    cut(nm, E, "hour", [-1, 5, 11, 17, 23], ["00-05", "06-11", "12-17", "18-23"])
    # 13 band, spread
    E["band"] = Mv[t, j]
    E.loc[E.s < 0, "band"] = 1 - E.band
    cut(nm, E, "band", [0, .3, .45, .55, .7, 1])
    E["spr"] = (Av[t, j] - Bv[t, j]) * 100
    cut(nm, E, "spr", [0, .5, 1, 1.5, 2, 100])
    E.to_pickle(f"data/research_diralpha/r3_{nm}.pkl")
# 14 tape staleness at trigger
tp = tape[(tape.ts >= L.idx[0])].copy(); tp["exchange_id"] = tp.exchange_id.astype(str)
last = {ex: g.ts.sort_values().values for ex, g in tp.groupby("exchange_id")}
for nm in ("slow", "fast"):
    E = pd.read_pickle(f"data/research_diralpha/r3_{nm}.pkl"); ag = []
    for tt, jj in zip(E.t.values, E.j.values):
        x = last.get(L.cols[jj]); T = L.idx[tt]
        if x is None: ag.append(np.nan); continue
        i = np.searchsorted(x, T) - 1
        ag.append((T - x[i]) / 3600 if i >= 0 else np.nan)
    E["tape_age"] = ag
    cut(nm, E, "tape_age", [0, .25, 1, 3, 8, 500])
# 9 SIG-only moves, fade: SIG moved 1.5c+ in 1 h with outside flat (<0.3c), both venues
dm12, df12 = (mid - mid.shift(12)).values, (fair - fair.shift(12)).values
dp12, dk12 = (pm - pm.shift(12)).values, (k - k.shift(12)).values
for th in (.015, .025, .04):
    M = (np.abs(dm12) >= th) & (np.abs(dp12) < .005) & (np.abs(dk12) < .005) & (Mv >= .15) & (Mv <= .85)
    M[:, L.office == "chamber"] = False
    S = pd.DataFrame(np.where(M, -np.sign(dm12), 0.0), index=mid.index, columns=mid.columns)
    for h in (12, 48, 288):
        tr = L.trades(S, h, thr=.5, cool=48)
        if tr is None: continue
        r = L.score(tr); print(f"SIG-only move {th*100:.1f}c fade {h//12}h: n {r['n']} races {r['races']} gross {r['gross_c']:.2f}c net {r['net_c']:.2f}c win {r['win']:.2f} t_day {r['t_day']:.1f}")
