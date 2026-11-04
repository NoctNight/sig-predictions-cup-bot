"""Drill-down 36: follow, not fade. Same events as d34 (SIG moved 4c+ alone over 48 h). Trade the way SIG moved, away from outside. Placebo: flipped sign is d34 itself."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); Bv, Av = L.B, L.A; n = Bv.shape[0]
E = pd.read_pickle("data/research_diralpha/sig_alone.pkl")
for h, nm in ((288, "24 h"), (576, "48 h"), (864, "72 h")):
    v = []; d = []
    for r in E.itertuples():
        e = r.t + 1; x = e + h
        if x >= n or not np.isfinite([Bv[e, r.j], Av[e, r.j], Bv[x, r.j], Av[x, r.j]]).all(): continue
        s = -r.s; v.append((Bv[x, r.j] - Av[e, r.j]) if s > 0 else (Bv[e, r.j] - Av[x, r.j])); d.append(r.day)
    v = np.array(v); s_ = pd.Series(v).groupby(np.array(d)).mean()
    print(f"follow {nm}: n {len(v)} net {100*v.mean():.2f}c win {(v>0).mean():.2f} days up {(s_>0).sum()}/{len(s_)} se {100*v.std()/np.sqrt(len(v)):.2f}c")
