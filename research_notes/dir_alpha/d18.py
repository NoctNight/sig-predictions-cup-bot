"""Drill-down 18: the same bet through either leg, entered on the cheaper one and exited ON THAT SAME LEG (a cross-leg exit makes a set, which locks cash to settlement)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl")
Bv, Av = L.B, L.A
meta = L.meta
cols = {e: i for i, e in enumerate(L.cols)}
mate = {}
for r, g in meta.groupby("race"):
    if len(g) == 2:
        a, b = list(g.index)
        if a in cols and b in cols: mate[cols[a]] = cols[b]; mate[cols[b]] = cols[a]
for name, E in (("slow", Es), ("fast", Ef)):
    rows = []
    for r in E.itertuples():
        k = mate.get(r.j)
        own_in = Av[r.e, r.j] if r.s > 0 else 1 - Bv[r.e, r.j]; own_out = Bv[r.x, r.j] if r.s > 0 else 1 - Av[r.x, r.j]
        if k is None or not np.isfinite(Av[r.e, k] + Bv[r.e, k] + Av[r.x, k] + Bv[r.x, k]):
            rows.append((own_out - own_in, own_out - own_in, np.nan, 0)); continue
        alt_in = (1 - Bv[r.e, k]) if r.s > 0 else Av[r.e, k]; alt_out = (1 - Av[r.x, k]) if r.s > 0 else Bv[r.x, k]
        ow = own_out - own_in; al = alt_out - alt_in
        ch = al if alt_in < own_in else ow
        rows.append((ow, ch, al, float(alt_in < own_in)))
    R = pd.DataFrame(rows, columns=["own", "chosen", "alt", "sw"])
    print(f"{name}: own leg {100*R.own.mean():.2f}c, cheaper entry leg then same leg out {100*R.chosen.mean():.2f}c, always the other leg {100*R.alt.mean():.2f}c, switched {100*R.sw.mean():.0f}%, win {100*(R.chosen>0).mean():.0f}%")
