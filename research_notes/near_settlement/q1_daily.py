"""Q1 by election night on daily closes, so 2022 and 2025 count apart from 2024. d = days before 12:00 UTC on election day."""
import numpy as np, pandas as pd
C = pd.read_csv("data/research_ev_hunt/calib/legs_all.csv", dtype={"market_id": str})
P = pd.read_csv("data/research_ev_hunt/calib/paths.csv", dtype={"market_id": str})
C = C[(C.us == 1) & (C.party != "other") & (C.volume_usd >= 5000)]
C = C.sort_values("volume_usd", ascending=False).drop_duplicates(["venue", "race_id"])
P = P[P.days_before.isin(range(0, 13)) & (P.stale_days.fillna(9) <= 1.5)]
W = P.merge(C[["venue", "market_id", "race_id", "cycle", "election_date", "office"]]).pivot_table(index=["venue", "race_id", "election_date", "office"], columns="days_before", values="price")
rows = []
for k, r in W.iterrows():
    for d in range(1, 12):
        a, b = r.get(float(d)), r.get(float(d - 1))
        if pd.isna(a) or pd.isna(b):
            continue
        f = max(a, 1 - a)
        rows.append(dict(venue=k[0], race=k[1], night=k[2], office=k[3], d=d, f=f, r2=(b - a) ** 2))
R = pd.DataFrame(rows)
R["band"] = np.select([(R.f >= .80) & (R.f <= .95), (R.f >= .5) & (R.f <= .65)], ["skewed", "mid"], "x")
main = {"2022-11-08": "2022", "2024-11-05": "2024", "2025-11-04": "2025"}
R["n"] = R.night.map(main)
R = R[R.n.notna() & (R.band != "x")]
out = []
for (n, v, b), q in R.groupby(["n", "venue", "band"]):
    e, l = q[q.d >= 5], q[q.d <= 2]
    if len(l) < 4 or len(e) < 8:
        continue
    races = q.race.unique(); gb = {k: v_ for k, v_ in q.groupby("race")}
    rng = np.random.default_rng(1); bs = []
    for _ in range(300):
        s = pd.concat([gb[x] for x in rng.choice(races, len(races))])
        bs.append(np.sqrt(s[s.d <= 2].r2.mean() / s[s.d >= 5].r2.mean()))
    out.append(dict(night=n, venue=v, band=b, races=len(races), early_c=100 * np.sqrt(e.r2.mean()), late_c=100 * np.sqrt(l.r2.mean()), ratio=np.sqrt(l.r2.mean() / e.r2.mean()), lo=np.nanpercentile(bs, 16), hi=np.nanpercentile(bs, 84)))
T = pd.DataFrame(out); T.to_pickle("data/research_near_settlement/q1_daily.pkl")
print("Daily-close volatility: T-11..T-5 against the last 2 days (to 12:00 UTC on election day)"); print(T.round(2).to_string(index=False))
