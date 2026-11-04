"""Q1 volatility by days to settlement and Q2 favourite drift, on the hourly panel. Errors: bootstrap over races; nights shown apart.
Run: python -I study_q12.py election|fed|awards|intl_election|others  (others pools the non-election groups)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from statistics import NormalDist
import net
OUT = net.OUT
GROUP = sys.argv[1] if len(sys.argv) > 1 else "election"
P, M = pd.read_pickle(f"{OUT}/panel.pkl"), pd.read_pickle(f"{OUT}/legs.pkl")
M = M[M.group != "election"] if GROUP == "others" else M[M.group == GROUP]
M = M[M.party != "other"].copy()
MQ2 = M.copy()
if GROUP == "election":
    M = M[M.vol >= 25000]
    MQ2 = MQ2[MQ2.vol >= 5000]
inv = np.vectorize(NormalDist().inv_cdf)


def probit(p):
    return inv(np.clip(p, .005, .995))


def one_per_race(m):
    """Keep one leg per race per venue (the rest are mirrors or near-mirrors): the one with most money."""
    return m.sort_values("vol", ascending=False).drop_duplicates(["venue", "race"])


def rv_rows(key, d, meta):
    d = d.set_index("h").sort_index()
    out = []
    p = d.p
    if p.notna().sum() < 48:
        return out
    z = pd.Series(probit(p.fillna(.5).values), index=p.index).where(p.notna())
    # buckets: day d holds tau in (d-1, d]; bucket 0 = the first 12 hours after T (result night)
    for b in list(range(1, 11)) + [0]:
        lo, hi = (-(b) * 24, -(b - 1) * 24) if b else (0, 12)
        w = p.loc[lo:hi]
        if w.notna().sum() < (hi - lo) * .7:
            continue
        zw = z.loc[lo:hi]
        p0 = w.dropna().iloc[0]
        r1 = (w.diff() ** 2).sum()
        r4 = np.mean([(w.iloc[o::4].diff() ** 2).sum() for o in range(4)])
        rz4 = np.mean([(zw.iloc[o::4].diff() ** 2).sum() for o in range(4)])
        a = w.iloc[::4].diff().dropna().abs().values
        bv = (np.pi / 2) * np.sum(a[1:] * a[:-1]) if len(a) > 1 else np.nan          # bipower: variance without the jumps
        out.append(dict(key=key, race=meta.race, night=meta.night, office=meta.office, venue=meta.venue, b=b, p0=p0, fav0=max(p0, 1 - p0), r1=r1, r4=r4, rz4=rz4, bv=bv))
    return out


rows = []
for _, m in one_per_race(M).iterrows():
    rows += rv_rows(m.key, P[P.key == m.key], m)
R = pd.DataFrame(rows)
R["band"] = np.select([(R.fav0 >= .80) & (R.fav0 <= .95), (R.fav0 >= .50) & (R.fav0 <= .65), R.fav0 > .95], ["skewed 80-95c", "mid 50-65c", "safe 95c+"], "other")
R.to_pickle(f"{OUT}/q1_{GROUP}.pkl")


def boot(df, f, n=400):
    races = df.race.unique()
    gb = {k: v for k, v in df.groupby("race")}
    rng = np.random.default_rng(7)
    res = []
    for _ in range(n):
        s = pd.concat([gb[r] for r in rng.choice(races, len(races))], ignore_index=True)
        res.append(f(s))
    return np.nanpercentile(res, [16, 84], axis=0)


tab = []
for band in ("skewed 80-95c", "mid 50-65c", "safe 95c+"):
    for b in list(range(10, 0, -1)) + [0]:
        q = R[(R.band == band) & (R.b == b)]
        if len(q) < 5:
            continue
        v = 100 * np.sqrt(q.r4.mean())
        lo, hi = boot(q, lambda s: 100 * np.sqrt(s.r4.mean()), 200)
        tab.append(dict(band=band, day=b, n=len(q), races=q.race.nunique(), vol_c=v, lo=lo, hi=hi, vol1h=100 * np.sqrt(q.r1.mean()), med=100 * np.sqrt(q.r4.median()), bip=100 * np.sqrt(q.bv.mean()), probit=np.sqrt(q.rz4.mean())))
T = pd.DataFrame(tab)
T.to_pickle(f"{OUT}/q1_tab_{GROUP}.pkl")
print(f"\n=== Q1 {GROUP}: legs {R.key.nunique()} races {R.race.nunique()} ===\nvolatility in cents per day (4-hour sampling); day 1 = last 24 h before T; day 0 = first 12 h after T")
print(T.round(2).to_string(index=False))
slopes = {}
for band in ("skewed 80-95c", "mid 50-65c"):
    q = R[(R.band == band) & (R.b >= 1)]
    if q.empty:
        continue
    def sl(s, col="rz4"):
        a = s.groupby("b")[col].mean()
        a = a[a > 0]
        if len(a) < 4:
            return np.nan
        return np.polyfit(np.log(a.index - .5), .5 * np.log(a.values), 1)[0]
    def ratio(s, col="r4"):
        return np.sqrt(s[s.b <= 2][col].mean() / s[s.b >= 6][col].mean())
    x, (lo, hi) = sl(q), boot(q, sl, 200)
    y, (lo2, hi2) = ratio(q), boot(q, ratio, 200)
    y1, (lo3, hi3) = ratio(q, "bv"), boot(q, lambda s: ratio(s, "bv"), 200)
    y2 = np.sqrt(q[q.b == 1].r4.mean() / q[q.b >= 6].r4.mean())
    slopes[band] = dict(slope=x, lo=lo, hi=hi, ratio=y, r_lo=lo2, r_hi=hi2, ratio_bip=y1, day1_ratio=y2)
    print(f"{band}: slope of log probit vol on log days left {x:+.2f} [{lo:+.2f},{hi:+.2f}] (steady news -0.50, no news 0). Vol last 2 days / T-10..T-6: {y:.2f} [{lo2:.2f},{hi2:.2f}]; without jumps {y1:.2f}; last day alone {y2:.2f}")
    for n, s in q.groupby("night"):
        if s.race.nunique() >= 5:
            print(f"   night {n}: ratio {ratio(s):.2f}, slope {sl(s):+.2f}, races {s.race.nunique()}")
    for n, s in q.groupby("office"):
        if s.race.nunique() >= 5:
            print(f"   office {n}: ratio {ratio(s):.2f}, slope {sl(s):+.2f}, races {s.race.nunique()}")
json.dump(slopes, open(f"{OUT}/q1_slopes_{GROUP}.json", "w"), indent=1, default=float)

# ---- Q2
Taus = [10, 7, 5, 3, 2, 1, .25, 0]
hs = {t: int(round(-t * 24)) for t in Taus}
legs = []
for _, m in one_per_race(MQ2).iterrows():
    d = P[P.key == m.key].set_index("h").p
    r = dict(key=m.key, race=m.race, night=m.night, office=m.office, venue=m.venue, outcome=m.outcome, multi=m.multi)
    for t, h in hs.items():
        r[t] = d.get(h, np.nan)
    legs.append(r)
L = pd.DataFrame(legs)
if GROUP == "election":                  # 2020-22 from the AMM-trade daily paths: d=0 (12:00 UTC election day) stands in for T-0.25, T not available
    C = pd.read_csv(os.path.join(net.ROOT, "data/research_ev_hunt/calib/legs_all.csv"), dtype={"market_id": str})
    Pp = pd.read_csv(os.path.join(net.ROOT, "data/research_ev_hunt/calib/paths.csv"), dtype={"market_id": str})
    C = C[(C.source == "chain") & (C.us == 1) & (C.party != "other") & (C.volume_usd >= 5000) & ~C.market_id.str.contains("#")]
    C = C.sort_values("volume_usd", ascending=False).drop_duplicates("race_id")
    w = Pp[Pp.market_id.isin(C.market_id) & (Pp.stale_days.fillna(9) <= 1.5)].pivot_table(index="market_id", columns="days_before", values="price")
    add = []
    for _, c in C.iterrows():
        if c.market_id in w.index:
            r = dict(key="chain:" + c.market_id, race=c.race_id, night=c.election_date, office=c.office, venue="polymarket-amm", outcome=c.outcome, multi=c.market_id)
            for t in Taus:
                r[t] = w.loc[c.market_id].get(t if t >= 1 else (0 if t == .25 else -1), np.nan)
            add.append(r)
    L = pd.concat([L, pd.DataFrame(add)], ignore_index=True)
L.to_pickle(f"{OUT}/q2_legs_{GROUP}.pkl")
L = L[L.outcome.isin([0, 1])]


def setup(A, lo_, hi_):
    a = L[L[A].notna()].copy()
    fp = np.maximum(a[A], 1 - a[A])
    a = a[(fp >= lo_) & (fp <= hi_)]
    flip = a[A] < .5
    for t in Taus:
        a[f"f{t}"] = np.where(flip, 1 - a[t], a[t])
    a["won"] = np.where(flip, 1 - a.outcome, a.outcome)
    return a


tab = []
for lo_, hi_, name in ((.88, .93, "88-93c"), (.85, .95, "85-95c")):
    for A in (10, 7, 5, 3, 2):
        a = setup(A, lo_, hi_)
        for t in [x for x in Taus if x < A]:
            q = a.dropna(subset=[f"f{t}"])
            if len(q) < 4:
                continue
            ca = lambda s: 100 * (s[f"f{t}"].mean() - s[f"f{A}"].mean())
            d0 = 100 * (1 - q[f"f{A}"].mean())
            lo, hi = boot(q, ca, 200)
            w, l = q[q.won == 1], q[q.won == 0]
            # what the price implies for winners if prices are honest: E[p_t | win] = E[p_t^2] / E[p_t]
            imp_w = 100 * ((q[f"f{t}"] ** 2).mean() / q[f"f{t}"].mean() - q[f"f{A}"].mean())
            tab.append(dict(band=name, anchor=A, tau=t, n=len(q), races=q.race.nunique(), start=100 * q[f"f{A}"].mean(), dist=d0, all_c=ca(q), lo=lo, hi=hi, win_c=ca(w), lose_c=ca(l) if len(l) else np.nan, n_lose=len(l), win_implied=imp_w, closed_all=100 * ca(q) / d0, closed_win=100 * ca(w) / d0, winrate=100 * q.won.mean()))
Q = pd.DataFrame(tab)
Q.to_pickle(f"{OUT}/q2_tab_{GROUP}.pkl")
print(f"\n=== Q2 {GROUP}: favourites at the anchor; cents moved by that favourite, all / winners / losers ===")
print(Q[Q.anchor.isin([10, 7, 5, 3])].round(2).to_string(index=False))
# drift per day by leg of the path, band 85-95 at the start of each leg (fresh selection, so no look-through), pooled
seg = []
for a_, b_ in ((10, 7), (7, 5), (5, 3), (3, 2), (2, 1), (1, .25), (.25, 0)):
    q = setup(a_, .85, .95).dropna(subset=[f"f{b_}"])
    if len(q) < 4:
        continue
    days = a_ - b_
    z = lambda s: 100 * (s[f"f{b_}"] - s[f"f{a_}"]).mean() / days
    lo, hi = boot(q, z, 200)
    seg.append(dict(segment=f"T-{a_} to T-{b_}", n=len(q), races=q.race.nunique(), c_per_day=z(q), lo=lo, hi=hi, win=z(q[q.won == 1]), lose=z(q[q.won == 0]) if (q.won == 0).any() else np.nan, n_lose=int((q.won == 0).sum()), wr=100 * q.won.mean(), px=100 * q[f"f{a_}"].mean()))
S = pd.DataFrame(seg)
S.to_pickle(f"{OUT}/q2_seg_{GROUP}.pkl")
print("\nDrift per day of favourites at 85-95c at the start of each stretch"); print(S.round(2).to_string(index=False))
cal = []
for A in (10, 5, 3, 2, 1, .25):
    for lo_, hi_ in ((.85, .90), (.90, .95), (.95, .98)):
        a = setup(A, lo_, hi_)
        if len(a) < 4:
            continue
        e = lambda s: 100 * (s.won.mean() - s[f"f{A}"].mean())
        lo, hi = boot(a, e, 200)
        cal.append(dict(tau=A, band=f"{lo_:.2f}-{hi_:.2f}", n=len(a), races=a.race.nunique(), px=100 * a[f"f{A}"].mean(), won=100 * a.won.mean(), edge=e(a), lo=lo, hi=hi))
C_ = pd.DataFrame(cal)
C_.to_pickle(f"{OUT}/q2_cal_{GROUP}.pkl")
print("\nCalibration: did favourites win more than their price?"); print(C_.round(2).to_string(index=False))
