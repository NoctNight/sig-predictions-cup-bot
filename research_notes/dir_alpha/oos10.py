"""Line trade path: profit by hours held, 3c+ from the line, both periods, last-trade prices for A, true book for B. Same entries as oos8 (saved), exit at the touch or next trade at each horizon."""
import pickle, sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
pd.set_option("display.width", 200)
L = Lab(); A = pickle.load(open("data/research_diralpha/oos/panel.pkl", "rb"))
Ea = pd.read_pickle("data/research_diralpha/oos/line_a.pkl"); Eb = pd.read_pickle("data/research_diralpha/oos/line_b.pkl")
Ea = Ea[Ea.res >= .03]; Eb = Eb[Eb.res >= .03]
Ma = A["S"].values; Bb, Ab = L.bid.ffill(limit=12).values, L.ask.ffill(limit=12).values
tp = pd.read_pickle("data/research_diralpha/tape.pkl"); tp = tp[tp.ts >= L.idx[0]].sort_values("ts"); tp["b"] = (tp.ts // 300).astype("int64") * 300; tp["ex"] = tp.exchange_id.astype(str)
rows = []
for h in (6, 12, 24, 36, 48, 60, 72, 87):
    k = h * 12
    xa = Ea.t.values + k; oa = xa < Ma.shape[0]; s = np.where(Ea.rich.values, -1.0, 1.0)
    ga = s[oa] * (Ma[xa[oa], Ea.j.values[oa]] - Ma[Ea.t.values[oa], Ea.j.values[oa]]); ga = ga[np.isfinite(ga)]
    xb = Eb.t.values + k; ob = xb < Bb.shape[0]; s2 = np.where(Eb.rich.values, -1.0, 1.0); t0 = Eb.t.values[ob]; j0 = Eb.j.values[ob]; x0 = xb[ob]
    nb = np.where(s2[ob] > 0, Bb[x0, j0] - Ab[t0, j0], Bb[t0, j0] - Ab[x0, j0]); nb = nb[np.isfinite(nb)]
    rows.append(dict(hours=h, n_a=len(ga), net_a=round(100 * ga.mean() - .75, 2), right_a=round((ga > 0).mean(), 2), n_b=len(nb), net_b=round(100 * nb.mean(), 2), won_b=round((nb > 0).mean(), 2)))
print(pd.DataFrame(rows).to_string(index=False))
