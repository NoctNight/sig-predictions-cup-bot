"""Drill-down 37: catching flash moves with far resting orders. Every 5 min rest a bid d below and an offer d above the previous bin's mid, on every leg.
Filled only if the tape prints strictly through our level in the bin (so we were not last in the queue at that level). One fill per leg per side per hour.
Exit at the touch 30 min, 4 h, 24 h later. No look-ahead: reference is the previous bin. Tape prints with the bot's own fills are not removed (we were rarely in these)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
tp = pd.read_pickle("data/research_diralpha/tape.pkl"); tp = tp[tp.ts >= L.idx[0]].copy()
tp["q"] = tp.quantity.abs(); tp["i"] = np.searchsorted(L.idx, (tp.ts // 300).astype("int64") * 300)
col = {c: i for i, c in enumerate(L.cols)}; tp["j"] = tp.exchange_id.astype(str).map(col); tp = tp.dropna(subset=["j"]); tp["j"] = tp.j.astype(int)
Mv, Bv, Av, Fv = L.mid.ffill(limit=6).values, L.B, L.A, L.fair.ffill(limit=12).values
n, m = Mv.shape
G = tp.groupby(["i", "j"]).agg(lo=("price", "min"), hi=("price", "max"), q=("q", "sum"))
lo = {(a, b): c for (a, b), c in G.lo.items()}; hi = {(a, b): c for (a, b), c in G.hi.items()}
rows = []
keys = sorted(lo)
for d in (.02, .03, .04, .06, .08):
    last = {}
    for (i, j) in keys:
        if i < 1 or i >= n: continue
        ref = Mv[i - 1, j]
        if not np.isfinite(ref) or ref < .05 or ref > .95: continue
        for s, thr, ok in ((1, ref - d, lo[(i, j)] <= ref - d - .005), (-1, ref + d, hi[(i, j)] >= ref + d + .005)):
            if not ok or i - last.get((j, s), -999) < 12: continue
            last[(j, s)] = i
            r = dict(d=d, j=j, i=i, s=s, px=thr, office=L.office[j], day=L.day[i], ref=ref, fair=Fv[i - 1, j])
            for h, nm in ((6, "30m"), (48, "4h"), (288, "24h")):
                x = i + h
                if x >= n or not (np.isfinite(Bv[x, j]) and np.isfinite(Av[x, j])): r["n" + nm] = np.nan; continue
                r["n" + nm] = (Bv[x, j] - thr) if s > 0 else (thr - Av[x, j])
            rows.append(r)
R = pd.DataFrame(rows); R.to_pickle("data/research_diralpha/flash.pkl")
R["with_fair"] = (R.s * (R.fair - R.ref)) > 0
span = (L.idx[-1] - L.idx[0]) / 86400
def tab(Q): return Q.groupby("d").agg(fills=("j", "size"), legs=("j", "nunique"), days=("day", "nunique"), n30=("n30m", lambda v: 100 * v.mean()), win30=("n30m", lambda v: (v > 0).mean()), n4=("n4h", lambda v: 100 * v.mean()), n24=("n24h", lambda v: 100 * v.mean()), win24=("n24h", lambda v: (v > 0).mean())).round(2)
print("all legs"); print(tab(R).to_string())
print("chamber"); print(tab(R[R.office == "chamber"]).to_string())
for o in ("senate", "governor", "house"): print(o); print(tab(R[R.office == o]).to_string())
print("only fills on the side outside agrees with"); print(tab(R[R.with_fair]).to_string())
print("only fills against outside"); print(tab(R[~R.with_fair]).to_string())
tr = R[R.day <= L.day[n // 2]]; te = R[R.day > L.day[n // 2]]
print("train"); print(tab(tr).to_string()); print("test"); print(tab(te).to_string())
print("by day at 4c"); print(R[R.d == .04].groupby("day").agg(n=("j", "size"), n30=("n30m", lambda v: 100 * v.mean()), n24=("n24h", lambda v: 100 * v.mean())).round(2).to_string())
