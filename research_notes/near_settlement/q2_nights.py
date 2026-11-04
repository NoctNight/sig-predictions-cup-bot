"""Q2 by night and office: favourites at 85-95c at T-3 and T-10, how far they moved, so one night's swing cannot pass as a rule."""
import numpy as np, pandas as pd, sys
L = pd.read_pickle("data/research_near_settlement/q2_legs_election.pkl")
L = L[L.outcome.isin([0, 1])]
L["nm"] = L.night.map({"2022-11-08": "2022", "2024-11-05": "2024", "2025-11-04": "2025"}).fillna("other")
out = []
for A, B in ((10, 3), (10, 1), (3, 1), (2, 1), (1, .25), (1, 0)):
    a = L[L[A].notna() & L[B].notna()].copy()
    f = np.maximum(a[A], 1 - a[A]); a = a[(f >= .85) & (f <= .95)]
    fl = a[A] < .5
    a["d"] = 100 * (np.where(fl, 1 - a[B], a[B]) - np.where(fl, 1 - a[A], a[A]))
    for key, g in (("night", a.groupby("nm")), ("office", a[a.nm == "2024"].groupby("office"))):
        for k, q in g:
            if len(q) >= 3:
                out.append(dict(move=f"T-{A} to T-{B}", cut=f"{key} {k}", n=len(q), races=q.race.nunique(), mean_c=q.d.mean(), median_c=q.d.median(), up=(q.d > 0).mean()))
T = pd.DataFrame(out); T.to_pickle("data/research_near_settlement/q2_nights.pkl")
print(T.round(2).to_string(index=False))
# how many independent chances to see a loser
C = pd.read_csv("data/research_ev_hunt/calib/legs_all.csv", dtype={"market_id": str}); P = pd.read_csv("data/research_ev_hunt/calib/paths.csv", dtype={"market_id": str})
x = P[P.days_before == 3].merge(C, on=["venue", "market_id"]); x = x[(x.us == 1) & (x.party != "other") & (x.volume_usd >= 5000) & x.outcome.isin([0, 1])].dropna(subset=["price"])
x["f"] = np.maximum(x.price, 1 - x.price); x["won"] = np.where(x.price < .5, 1 - x.outcome, x.outcome)
for lo, hi in ((.80, .85), (.85, .90), (.90, .95), (.95, .985)):
    q = x[(x.f >= lo) & (x.f < hi)]
    print(f"{lo:.2f}-{hi:.3f}: legs {len(q)}, races {q.race_id.nunique()}, lost {(q.won == 0).sum()}, by cycle {q.groupby('cycle').race_id.nunique().to_dict()}")
