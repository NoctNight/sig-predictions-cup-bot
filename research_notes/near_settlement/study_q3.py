"""Q3: when Polymarket and Kalshi disagree on a skewed contract, does the gap close faster nearer settlement?
Gap = 6-hour average of (venue A - venue B) on the favourite's side. Entries every 6 hours while the gap has kept its sign and stayed at least `MIN` for the
whole trailing 6 hours. Closure over h hours compares it with the next, non-overlapping 6-hour average, so a noisy print cannot pose as closure.
The cheap venue plays SIG, the dear venue plays the outside fair. Errors: bootstrap over races. Run: python -I study_q3.py election|others"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import net
OUT = net.OUT
GROUP = sys.argv[1] if len(sys.argv) > 1 else "election"
P, M = pd.read_pickle(f"{OUT}/panel.pkl"), pd.read_pickle(f"{OUT}/legs.pkl")
M = M[M.group == "election"] if GROUP == "election" else M[M.group != "election"]
pm_, k_ = M[M.venue == "polymarket"], M[M.venue == "kalshi"]
pairs = []
for _, k in k_.iterrows():
    c = pm_[pm_.race == k.race]
    if c.empty:
        continue
    same = c[c.party == k.party]
    if len(same):
        pairs.append((same.sort_values("vol", ascending=False).iloc[0], k, False))
    elif k.party in ("D", "R") and c.party.isin(["D", "R"]).any():
        pairs.append((c[c.party.isin(["D", "R"])].sort_values("vol", ascending=False).iloc[0], k, True))
wide = {}
for a, k, flip in pairs:
    x = P[P.key == a.key].set_index("h").p
    y = P[P.key == k.key].set_index("h").p
    if flip:
        x = 1 - x
    d = pd.DataFrame({"a": x, "b": y}).dropna()
    if len(d) < 72:
        continue
    fav = (d.a + d.b) / 2
    if fav.loc[fav.index <= -24].median() < .5:      # favourite's side, fixed once per pair (by its pre-result median) so the frame does not flip
        d = 1 - d
    wide[(k.race, k.night, k.office, a.vol, k.vol)] = d
print("pairs", len(wide), "groups", pd.Series([k[2] for k in wide]).value_counts().to_dict())
H = [12, 24, 48]


def events(minimum, step=6, persist=6):
    rows = []
    for (race, night, office, va, vk), d in wide.items():
        d = d.reindex(range(-14 * 24, 1)).ffill(limit=3)
        g = d.a - d.b
        avg = g.rolling(6).mean()
        lo, hi = g.rolling(persist).min(), g.rolling(persist).max()
        av_a, av_b = d.a.rolling(6).mean(), d.b.rolling(6).mean()
        for t in range(-14 * 24 + 6, -5, step):
            if np.isnan(avg.get(t, np.nan)):
                continue
            s = np.sign(avg[t])
            if not ((lo[t] >= minimum) if s > 0 else (hi[t] <= -minimum)):
                continue
            lvl = (av_a[t] + av_b[t]) / 2
            r = dict(race=race, night=night, office=office, t=t, tau=-t / 24, gap=abs(avg[t]), lvl=lvl, va=va, vk=vk, cheap_is_b=s > 0)
            cheap0, dear0 = (av_b[t], av_a[t]) if s > 0 else (av_a[t], av_b[t])
            for h in H + [("T",)]:
                tt = 0 if isinstance(h, tuple) else t + h
                if tt > 0 or (tt - t) < 6:
                    continue
                name = "T" if isinstance(h, tuple) else h
                g1 = s * avg.get(tt, np.nan)
                ch1, de1 = (av_b.get(tt, np.nan), av_a.get(tt, np.nan)) if s > 0 else (av_a.get(tt, np.nan), av_b.get(tt, np.nan))
                r[f"g{name}"], r[f"ch{name}"], r[f"de{name}"] = g1, ch1 - cheap0, de1 - dear0
            rows.append(r)
    return pd.DataFrame(rows)


BK = [(14, 10, "T-14 to T-10"), (10, 7, "T-10 to T-7"), (7, 5, "T-7 to T-5"), (5, 3, "T-5 to T-3"), (3, 2, "T-3 to T-2"), (2, 1, "T-2 to T-1"), (1, .25, "T-1 to T-6h")]


def boot_close(df, h, n=300):
    gb = {k: (v.gap.values, v[f"g{h}"].values) for k, v in df.groupby("race")}
    keys = list(gb)
    rng = np.random.default_rng(3)
    out = []
    for _ in range(n):
        ks = rng.choice(len(keys), len(keys))
        a = np.concatenate([gb[keys[i]][0] for i in ks]); b = np.concatenate([gb[keys[i]][1] for i in ks])
        out.append(1 - b.mean() / a.mean())
    return np.percentile(out, [16, 84])


res = []
for band, lo_, hi_ in (("skewed 80-96c", .80, .96), ("mid 50-75c", .50, .75)):
    for minimum in (.01, .02, .03):
        E = events(minimum)
        if E.empty:
            continue
        E = E[(E.lvl >= lo_) & (E.lvl <= hi_)]
        for h in (24, 12):
            for a_, b_, name in BK:
                q = E[(E.tau <= a_) & (E.tau > b_)].dropna(subset=[f"g{h}"])
                if len(q) < 6 or q.race.nunique() < 3:
                    continue
                c = 1 - q[f"g{h}"].mean() / q.gap.mean()
                lo, hi = boot_close(q, h)
                res.append(dict(band=band, min_gap=minimum, h=h, bucket=name, n=len(q), races=q.race.nunique(), gap_c=100 * q.gap.mean(), close=100 * c, lo=100 * lo, hi=100 * hi,
                                cheap_up=100 * q[f"ch{h}"].mean(), dear_down=-100 * q[f"de{h}"].mean()))
R = pd.DataFrame(res)
R.to_pickle(f"{OUT}/q3_{GROUP}.pkl")
print(R[(R.h == 24)].round(2).to_string(index=False))
print(R[(R.h == 12) & (R.min_gap == .02)].round(2).to_string(index=False))
# direct analogue of the live trade: gap open at T-3, what is left at T-2, T-1, T-6h, T
out = []
for start in (10, 7, 5, 3):
    for band, lo_, hi_ in (("skewed 80-96c", .80, .96),):
        for minimum in (.01, .02):
            E = events(minimum, step=6)
            q = E[(E.t == -start * 24) & (E.lvl >= lo_) & (E.lvl <= hi_)]
            if len(q) < 4:
                continue
            r = dict(start=start, min_gap=minimum, n=len(q), gap_c=100 * q.gap.mean())
            for tt, nm in ((-48, "T-2"), (-24, "T-1"), (-6, "T-6h"), (0, "T")):
                if tt <= -start * 24:
                    continue
                vals, cheap = [], []
                for _, e in q.iterrows():
                    d = wide[next(k for k in wide if k[0] == e.race)].reindex(range(-14 * 24, 1)).ffill(limit=3)
                    g = (d.a - d.b).rolling(6).mean()
                    s = -1 if not e.cheap_is_b else 1
                    vals.append(s * g.get(tt, np.nan))
                r[nm] = 100 * (1 - np.nanmean(vals) / q.gap.mean())
            out.append(r)
print("\nShare of the gap open at T-x that has closed by each time"); print(pd.DataFrame(out).round(1).to_string(index=False))
pd.DataFrame(out).to_pickle(f"{OUT}/q3_direct_{GROUP}.pkl")

# level of the gap by day, nothing selected on the gap: if convergence sped up near T this falls
lv = []
for (race, night, office, va, vk), d in wide.items():
    d = d.reindex(range(-14 * 24, 1)).ffill(limit=3)
    for day in range(1, 15):
        w = d.loc[-day * 24:-(day - 1) * 24 - 1]
        if w.a.notna().sum() < 12 or w.b.notna().sum() < 12:
            continue
        lvl = (w.a.mean() + w.b.mean()) / 2
        lv.append(dict(race=race, night=night, office=office, day=day, lvl=lvl, gap=(w.a - w.b).abs().mean(), sgap=(w.a - w.b).mean()))
V = pd.DataFrame(lv)
V = V[(V.lvl >= .80) & (V.lvl <= .96)]
tb = []
for day, q in V.groupby("day"):
    rng = np.random.default_rng(5)
    races = q.race.unique()
    gb = {k: v.gap.values for k, v in q.groupby("race")}
    bs = [np.mean(np.concatenate([gb[r] for r in rng.choice(races, len(races))])) for _ in range(300)]
    tb.append(dict(day=day, n=len(q), races=len(races), gap_c=100 * q.gap.mean(), med=100 * q.gap.median(), lo=100 * np.percentile(bs, 16), hi=100 * np.percentile(bs, 84)))
TB = pd.DataFrame(tb)
TB.to_pickle(f"{OUT}/q3_level_{GROUP}.pkl")
print("\nMean absolute gap by day, skewed pairs, no selection"); print(TB.round(2).to_string(index=False))
# robustness: drop the presidential states; and the SIG-like case, the cheap venue is the thin one
E = events(.02)
E = E[(E.lvl >= .80) & (E.lvl <= .96)]
if GROUP == "election":
    cuts = {"all": E, "no president": E[E.office != "president"], "Kalshi cheap": E[E.cheap_is_b], "Polymarket cheap": E[~E.cheap_is_b], "thin Kalshi cheap": E[E.cheap_is_b & (E.vk < E.vk.median())]}
else:
    cuts = {"all": E}
    cuts.update({k: E[E.office == k] for k in E.office.unique()})
    cuts.update({"Kalshi cheap": E[E.cheap_is_b], "Polymarket cheap": E[~E.cheap_is_b]})
E48 = events(.015, persist=48)
E48 = E48[(E48.lvl >= .80) & (E48.lvl <= .96)]
if len(E48):
    cuts["open 48h+, 1.5c+"] = E48
    cuts["open 48h+, Kalshi cheap"] = E48[E48.cheap_is_b]
rb = []
for name, e in cuts.items():
    for lab, a_, b_ in (("T-14 to T-3", 14, 3), ("T-3 to T-1", 3, 1)):
        q = e[(e.tau <= a_) & (e.tau > b_)].dropna(subset=["g24"])
        if len(q) < 6:
            continue
        lo, hi = boot_close(q, 24)
        rb.append(dict(cut=name, window=lab, n=len(q), races=q.race.nunique(), gap=100 * q.gap.mean(), close=100 * (1 - q.g24.mean() / q.gap.mean()), lo=100 * lo, hi=100 * hi, cheap_up=100 * q.ch24.mean(), dear_down=-100 * q.de24.mean()))
RB = pd.DataFrame(rb)
RB.to_pickle(f"{OUT}/q3_cuts_{GROUP}.pkl")
print("\n24-hour closure of gaps of 2c+, early against late"); print(RB.round(2).to_string(index=False))
# the speed-up that goes into the exit sums, from a bootstrap of the ratio over races: closure per day T-3..T-1 over T-14..T-3
E1 = events(.01)
E1 = E1[(E1.lvl >= .80) & (E1.lvl <= .96)].dropna(subset=["g24"])
for nm, cut in (("1c+ gaps", E1), ("1c+ gaps, Kalshi cheap", E1[E1.cheap_is_b])):
    a_, b_ = cut[cut.tau > 3], cut[(cut.tau <= 3) & (cut.tau > 1)]
    if len(a_) >= 5 and len(b_) >= 5:
        print(nm, "closure early", round(100 * (1 - a_.g24.mean() / a_.gap.mean()), 1), "late", round(100 * (1 - b_.g24.mean() / b_.gap.mean()), 1), "n late", len(b_), "races", b_.race.nunique())
q_ = E.dropna(subset=["g24"]) if not E.empty else E
early, late = q_[q_.tau > 3], q_[(q_.tau <= 3) & (q_.tau > 1)]
if len(late) < 5 or late.race.nunique() < 3:
    print("\nToo few gaps of 2c+ inside T-3 to measure a speed-up:", len(late))
    sys.exit()
def _r(e, l):
    return (1 - l.g24.mean() / l.gap.mean()) / (1 - e.g24.mean() / e.gap.mean())
rng = np.random.default_rng(9)
races = q_.race.unique(); ge = dict(tuple(early.groupby("race"))); gl = dict(tuple(late.groupby("race")))
bs = []
for _ in range(1000):
    s = rng.choice(races, len(races))
    es, ls = [ge[r] for r in s if r in ge], [gl[r] for r in s if r in gl]
    if not es or not ls:
        continue
    e, l = pd.concat(es), pd.concat(ls)
    if len(l) >= 5 and len(e) >= 5:
        bs.append(_r(e, l))
sp = dict(ratio=float(_r(early, late)), lo=float(np.percentile(bs, 16)), hi=float(np.percentile(bs, 84)), lo95=float(np.percentile(bs, 2.5)), hi95=float(np.percentile(bs, 97.5)), n_late=len(late), races=int(late.race.nunique()))
json.dump(sp, open(f"{OUT}/q3_speedup_{GROUP}.json", "w"), indent=1)
print("\nSpeed-up of 24-hour closure, T-3..T-1 over T-14..T-3:", {k: round(v, 2) for k, v in sp.items()})
