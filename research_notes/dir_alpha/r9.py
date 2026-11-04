"""Round 9: which markets. For both frozen rules, split by things known at entry: market type facts, ratings, leg slowness (ranked on training days only),
breadth (one race or a national move), entry hour, touch size, which venue moved first."""
import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, H
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
meta = L.meta
pd.set_option("display.width", 200, "display.max_rows", 400, "display.max_columns", 30)
nochm = L.office != "chamber"
S48 = gap - gap.rolling(576, min_periods=288).median()
s = np.sign(S48)
both = (L.lag(L.pm, 288) * s >= .01) & (L.lag(L.k, 288) * s >= .01)
flowok = L.rsum(P["sflow"], 288) * s >= -5000
M1 = (((mid >= .15) & (mid <= .85)) & both & flowok).values.copy(); M1[:, ~nochm] = False
slow = L.trades(S48, H["24h"], thr=.02, mask=M1, cool=H["24h"])
W = 6
dp, dk, dm, df = L.lag(L.pm, W), L.lag(L.k, W), L.lag(mid, W), L.lag(fair, W)
sf = np.sign(df)
M2 = (((dp * sf >= .015) & (dk * sf >= .015) & ((dm * sf) < .5 * (df * sf)) & (mid >= .25) & (mid <= .75))).values.copy(); M2[:, ~nochm] = False
fast = L.trades(df, H["4h"], thr=1e-9, mask=M2, cool=48)
W2 = 12
dp2, dk2, dm2, df2 = L.lag(L.pm, W2), L.lag(L.k, W2), L.lag(mid, W2), L.lag(fair, W2)
sf2 = np.sign(df2)
M3 = (((dp2 * sf2 >= .015) & (dk2 * sf2 >= .015) & ((dm2 * sf2) < .5 * (df2 * sf2)) & (mid >= .25) & (mid <= .75))).values.copy(); M3[:, ~nochm] = False
fast2 = L.trades(df2, H["4h"], thr=1e-9, mask=M3, cool=48)
print("slow", len(slow), round(100 * slow[:, 4].mean(), 2), "fast 30m", len(fast), round(100 * fast[:, 4].mean(), 2), "win", round((fast[:, 4] > 0).mean(), 2), "fast 1h", len(fast2), round(100 * fast2[:, 4].mean(), 2), "win", round((fast2[:, 4] > 0).mean(), 2))
fm = json.load(open("params/fair_model.json"))["races"]


def rating(r):
    e = fm.get(r)
    if not e: return np.nan, np.nan, np.nan, np.nan
    i = e.get("inputs", {})
    return i.get("rating_margin"), i.get("n_polls"), i.get("open_seat"), i.get("pvi")


# leg slowness: on training days, slope of the 4 h SIG change on the 4 h outside change, per leg (lower = slower)
tr_mask = L.idx < L.SPLIT
d1 = L.lag(mid, 48).values; d2 = L.lag(fair, 48).values
slope = np.full(len(L.cols), np.nan)
for j in range(len(L.cols)):
    a, b = d1[tr_mask, j], d2[tr_mask, j]
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() > 200 and b[ok].std() > 1e-4:
        slope[j] = (a[ok] @ b[ok]) / (b[ok] @ b[ok])
print("leg slope on training days: median %.2f, by office" % np.nanmedian(slope), pd.Series(slope).groupby(L.office).median().round(2).to_dict())
# gap persistence too: rank on train
close_ = ["Indiana", "Kentucky", "Virginia", "Georgia", "Florida", "South Carolina", "Vermont", "North Carolina", "Ohio", "West Virginia"]
big3 = ["California", "New York", "Texas", "Florida", "Pennsylvania"]
fire = ((M1 & (np.abs(np.nan_to_num(S48.values)) >= .02)) | M2).sum(axis=1)
fire_d = (pd.DataFrame(M2 | M3, index=mid.index, columns=mid.columns).astype(float).mul(L.dsign, axis=1)).multiply(np.sign(df2)).sum(axis=1).rolling(12, min_periods=1).sum()


def frame(tr, name):
    E = pd.DataFrame(tr, columns=["j", "i", "s", "gross", "net"]); E[["j", "i"]] = E[["j", "i"]].astype(int)
    E["ts"] = L.idx[E.i]; E["train"] = E.ts < L.SPLIT
    E["office"] = L.office[E.j]; E["party"] = L.party[E.j]; E["race"] = L.race[E.j]; E["state"] = meta.state.values[E.j]; E["nlegs"] = meta.nlegs.values[E.j]
    E["dir"] = np.where(E.s * np.where(E.party == "D", 1, -1) > 0, "toward D", "toward R")
    E["hour"] = L.hour[E.i]
    r = [rating(x) for x in E.race]
    E["rate"] = [x[0] for x in r]; E["polls"] = [x[1] for x in r]; E["open"] = [x[2] for x in r]; E["pvi"] = [x[3] for x in r]
    E["slope"] = slope[E.j]
    E["touch"] = (P["ask_qty"] if False else P["bid_qty"]).values[E.i, E.j]
    E["breadth"] = np.abs(fire_d.values[E.i])
    E["k_first"] = np.abs(L.lag(L.k, 12).values[E.i, E.j]) - np.abs(L.lag(L.pm, 12).values[E.i, E.j])
    E["early"] = E.state.isin(close_)
    E["bigstate"] = E.state.isin(big3)
    return E


def tab(E, col, bins=None, q=None):
    x = E[col]
    b = pd.cut(x, bins) if bins is not None else (pd.qcut(x, q, duplicates="drop") if q else x)
    t = E.groupby(b, observed=True).agg(n=("net", "size"), races=("race", "nunique"), net=("net", lambda v: 100 * v.mean()), win=("net", lambda v: (v > 0).mean()))
    t["train"] = E[E.train].groupby(b, observed=True).net.mean() * 100
    t["test"] = E[~E.train].groupby(b, observed=True).net.mean() * 100
    return t.round(2)


for name, tr in (("SLOW (24 h)", slow), ("FAST 30 min trigger (4 h hold)", fast), ("FAST 1 h trigger (4 h hold)", fast2)):
    E = frame(tr, name)
    print(f"\n======== {name}: n {len(E)} net {100*E.net.mean():.2f} win {(E.net>0).mean():.2f}")
    for c, kw in (("office", {}), ("dir", {}), ("nlegs", {}), ("open", {}), ("early", {}), ("bigstate", {}), ("rate", dict(bins=[-30, -10, -5, -2, 2, 5, 10, 30])), ("polls", dict(bins=[-1, 0, 2, 5, 100])), ("slope", dict(q=3)), ("breadth", dict(bins=[-1, 2, 5, 10, 500])), ("hour", dict(bins=[-1, 5, 11, 15, 19, 23])), ("touch", dict(q=3)), ("k_first", dict(q=3))):
        print("--", c); print(tab(E, c, **kw).to_string())
    E.to_pickle(f"data/research_diralpha/ev_{name.split()[0].lower()}_{len(E)}.pkl")
