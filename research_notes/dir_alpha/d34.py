"""Drill-down 34: base rate for the House D trade. A gap opens because SIG moved alone: gap (outside - SIG) widened by W+ over 48 h while outside moved under 1.5c.
How much of it closes in the next 24, 48, 72 h? All legs, one event per leg per 72 h, entry at the touch, exit at the touch. Also net if crossed both ways, and by price band.
Includes the favourite side only versus all, and a level-only control (gap already that big, not newly widened)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
mid, fair = L.mid, L.fair; gap = L.gap
Gv, Mv, Fv, Bv, Av = gap.values, mid.values, fair.values, L.B, L.A
n, m = Mv.shape
dG = (gap - gap.shift(576)).values; dF = (fair - fair.shift(576)).values
def ev(M, cool=864):
    out = []
    for j in range(m):
        last = -10**9
        for t in np.flatnonzero(M[:, j]):
            if t - last < cool: continue
            s = 1.0 if Gv[t, j] > 0 else -1.0
            e = t + 1; r = dict(j=j, t=t, s=s, gap=abs(Gv[t, j]), band=Mv[t, j] if s > 0 else 1 - Mv[t, j], office=L.office[j], day=L.day[t], race=L.race[j])
            ok = False
            for h, nm in ((288, "24"), (576, "48"), (864, "72")):
                x = e + h
                if x >= n or not np.isfinite([Bv[e, j], Av[e, j], Bv[x, j], Av[x, j]]).all(): r["net" + nm] = np.nan; r["cl" + nm] = np.nan; continue
                r["net" + nm] = (Bv[x, j] - Av[e, j]) if s > 0 else (Bv[e, j] - Av[x, j])
                r["cl" + nm] = 1 - (Gv[x, j] * s) / abs(Gv[t, j]); ok = True
            if ok: out.append(r); last = t
    return pd.DataFrame(out)
def rep(lab, E):
    if E.empty: print(lab, "none"); return
    print(f"{lab}: events {len(E)} races {E.race.nunique()} mean gap {100*E.gap.mean():.1f}c | net 24 h {100*E.net24.mean():.2f}c ({E.net24.notna().sum()}), 48 h {100*E.net48.mean():.2f}c ({E.net48.notna().sum()}), 72 h {100*E.net72.mean():.2f}c ({E.net72.notna().sum()}) | share closed 24 h {E.cl24.median():.2f} 48 h {E.cl48.median():.2f} 72 h {E.cl72.median():.2f} | win 48 h {(E.net48>0).sum()/max(E.net48.notna().sum(),1):.2f}")
for W in (.03, .04, .05, .07):
    M = (np.abs(dG) >= W) & (np.sign(dG) == np.sign(Gv)) & (np.abs(dF) < .015) & (np.abs(Gv) >= W * .75) & (Mv >= .1) & (Mv <= .9)
    E = ev(M); rep(f"SIG moved alone, widened {W*100:.0f}c+", E)
    if W == .04:
        E.to_pickle("data/research_diralpha/sig_alone.pkl")
        for lab, q in (("own side 60-90c", E[(E.band >= .6) & (E.band <= .9)]), ("own side under 60c", E[E.band < .6]), ("chamber", E[E.office == "chamber"]), ("senate", E[E.office == "senate"]), ("governor", E[E.office == "governor"]), ("house", E[E.office == "house"])): rep("   " + lab, q)
        print(E.groupby("day").net48.agg(["size", "mean"]).round(3).to_string())
# control: gap already that big for days (not newly opened)
M = (np.abs(Gv) >= .04) & (np.abs(dG) < .01) & (Mv >= .1) & (Mv <= .9)
rep("control: gap 4c+ that has been there 48 h", ev(M))
# after a big single-leg sweep, 50k+ shares in 10 min with outside flat: does the price come back?
tp = pd.read_pickle("data/research_diralpha/tape.pkl"); tp = tp[tp.ts >= L.idx[0]].copy(); tp["q"] = tp.quantity.abs(); tp["b"] = (tp.ts // 600).astype("int64") * 600
vol = tp.groupby(["b", "exchange_id"]).q.sum().reset_index()
big = vol[vol.q >= 100000]; rows = []
cols = {c: i for i, c in enumerate(L.cols)}
for r in big.itertuples():
    j = cols.get(str(r.exchange_id))
    if j is None: continue
    t = np.searchsorted(L.idx, r.b + 900)
    if t + 288 >= n or t < 3: continue
    d0 = Mv[t, j] - Mv[max(t - 6, 0), j]
    if not np.isfinite(d0) or abs(d0) < .01 or abs(Fv[t, j] - Fv[max(t - 6, 0), j]) >= .01: continue
    rows.append(dict(ex=r.exchange_id, q=r.q, move=d0, back4=-(np.sign(d0)) * (Mv[t + 48, j] - Mv[t, j]) / abs(d0), back24=-(np.sign(d0)) * (Mv[t + 288, j] - Mv[t, j]) / abs(d0), m4=-(np.sign(d0)) * (Mv[t + 48, j] - Mv[t, j]), m24=-(np.sign(d0)) * (Mv[t + 288, j] - Mv[t, j])))
Q = pd.DataFrame(rows)
print("\n100k+ share bursts in 10 min that moved SIG 1c+ with outside flat:", len(Q)); 
if len(Q): print(Q[["move", "back4", "back24", "m4", "m24"]].describe().loc[["mean", "50%"]].round(3).to_string())
