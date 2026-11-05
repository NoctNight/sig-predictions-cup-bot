"""Round 3 and 4: cross-market, calendar and structure."""
import sys, time
import numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, survivors, show

L = Lab()
P = L.P
mid, fair = L.mid, L.fair
ds = L.dsign
cols = L.cols
meta = L.meta
sig = {}
two = (meta.nlegs == 2).values
dmid = mid.mul(ds, axis=1)                 # price of "D does better" on every leg
dfair = fair.mul(ds, axis=1)
OFF = ["senate", "governor", "house"]
LB = {"1h": 12, "4h": 48, "12h": 144, "24h": 288}


def factor(df, off, k, excl_same=None):
    c = (L.office == off) & two
    return (df.loc[:, c] - df.loc[:, c].shift(k)).mean(axis=1)


def apply(f, office):
    """A D-direction factor series as a leg signal on `office` legs."""
    S = pd.DataFrame(np.outer(f.values, ds.values), index=mid.index, columns=cols)
    S.loc[:, L.office != office] = np.nan
    return S


# H14: one office's SIG move leads another's; H15: laggards of the national move catch up
for n, k in LB.items():
    for a in OFF:
        fa = factor(dmid, a, k)
        for b in OFF:
            if a != b:
                sig[f"H14lead:{a}>{b}:{n}"] = apply(fa, b)
    nat = pd.concat([factor(dmid, o, k) for o in OFF], axis=1).mean(axis=1)
    natf = pd.concat([factor(dfair, o, k) for o in OFF], axis=1).mean(axis=1)
    legchg = (dmid - dmid.shift(k))
    res = pd.DataFrame(np.outer(nat.values, np.ones(len(cols))), index=mid.index, columns=cols) - legchg
    sig[f"H15laggard:{n}"] = res.mul(ds, axis=1)
    sig[f"H15natfollow:{n}"] = pd.DataFrame(np.outer(nat.values, ds.values), index=mid.index, columns=cols)
    sig[f"H15natfade:{n}"] = -sig[f"H15natfollow:{n}"]
    sig[f"H15outsidenat:{n}"] = pd.DataFrame(np.outer(natf.values, ds.values), index=mid.index, columns=cols)
    # H16 same-state peer
    peer = pd.DataFrame(np.nan, index=mid.index, columns=cols)
    chg = dmid - dmid.shift(k)
    for st, grp in meta.groupby("state"):
        ex = [e for e in grp.index if ds[e] != 0]
        offs = meta.loc[ex, "office"]
        if offs.nunique() < 2:
            continue
        for e in ex:
            oth = [x for x in ex if meta.office[x] != meta.office[e]]
            peer[e] = chg[oth].mean(axis=1) * ds[e]
    sig[f"H16peer:{n}"] = peer
# H17 chamber against seats
hc = (L.office == "house") & two
pD = (mid.loc[:, hc & (L.party == "D")]).sum(axis=1)     # expected D seats among listed seats
for chamber, off in (("U.S. House", "house"), ("U.S. Senate", "senate")):
    ex = meta.index[(meta.race == chamber) & (meta.party == "D")]
    if len(ex) == 0:
        continue
    e = ex[0]
    c = (L.office == off) & (L.party == "D")
    seats = mid.loc[:, c].sum(axis=1)
    z = lambda s, w: (s - s.rolling(w, min_periods=w // 2).mean()) / s.rolling(w, min_periods=w // 2).std()
    for w, wn in ((288, "24h"), (576, "48h")):
        d = z(mid[e], w) - z(seats, w)
        S1 = pd.DataFrame(np.nan, index=mid.index, columns=cols)
        S1[e] = -d
        sig[f"H17chamber:{chamber}:{wn}"] = S1
        sig[f"H17seats:{chamber}:{wn}"] = apply(d, off)
# H18 overround and leg share
g = meta.groupby("race")
over = mid.T.groupby(L.race).transform("sum").T - 1
sig["H18over"] = -over
ba = P["sig_ask"].where(L.bid.notna())
for n, k in (("1h", 12), ("4h", 48)):
    other = pd.DataFrame(np.nan, index=mid.index, columns=cols)
    ch = mid - mid.shift(k)
    for r, grp in meta.groupby("race"):
        if len(grp) == 2:
            a, b = list(grp.index)
            other[a] = -ch[b] - ch[a]
            other[b] = -ch[a] - ch[b]
    sig[f"H18compl:{n}"] = other
# H19 office-level gap
for off in OFF:
    c = L.office == off
    of = L.gap.mul(ds, axis=1).loc[:, c].mean(axis=1)
    dev = of - of.rolling(288, min_periods=144).mean()
    sig[f"H19officegap:{off}"] = apply(dev, off)
# H22 favourite drift
for off in [None] + OFF:
    n = off or "all"
    S = (mid - 0.5)
    sig[f"H22fav:{n}"] = S if off is None else S.where(pd.Series(L.office == off, index=cols), axis=1)
sig["H22favmid"] = (mid - 0.5).where((mid.sub(0.5).abs() >= 0.25) & (mid.sub(0.5).abs() <= 0.42))
# H23 round numbers: just above round number tends to hold or fall through
for lv in (0.5, 0.25, 0.75, 0.9, 0.1):
    near = (mid - lv)
    sig[f"H23away:{lv}"] = near.where(near.abs() <= 0.01)
    sig[f"H23to:{lv}"] = (-near).where((near.abs() >= 0.003) & (near.abs() <= 0.015))
# H24 quote steps: bid moved, ask did not (or the reverse)
b, a = L.bid, L.ask
db, da = b - b.shift(3), a - a.shift(3)
sig["H24step"] = (db - da).where((db != 0) ^ (da != 0))
sig["H24stepfade"] = -sig["H24step"]
sig["H24widen"] = (L.spr - L.spr.rolling(288, min_periods=100).median()).where(lambda x: x > 0) * np.sign(L.gap)
# H20 hour of day (D direction per office, fitted on training days only, tested after)
hrs = L.hour
for off in OFF:
    c = (L.office == off) & two
    r = (dmid.loc[:, c].shift(-12) - dmid.loc[:, c]).mean(axis=1)
    tr = L.idx < L.SPLIT
    prof = pd.Series(r.values[tr]).groupby(hrs[tr]).mean()
    s = pd.Series(prof.reindex(hrs).values, index=mid.index)
    sig[f"H20hour:{off}"] = apply(s, off)
all_, t0 = [], time.time()
for i, (k, s) in enumerate(sig.items()):
    d = L.test(k, s, family=k.split(":")[0], offices=(None,) if ("lead" in k or "officegap" in k or "chamber" in k or "seats" in k or "hour" in k or ":house" in k or ":senate" in k or ":governor" in k) else (None, "senate", "governor", "house"))
    all_.append(d)
    if i % 25 == 0:
        print(i, k, round(time.time() - t0), flush=True)
A = pd.concat(all_, ignore_index=True)
A.to_pickle("data/research_diralpha/r3.pkl")
print("tests", L.n_tests, "signals", len(sig))
S = survivors(A)
print("survivors", len(S)); show(S, 40)
print("\nBest test-day net:"); show(A[A.get("test_n", 0) >= 30].sort_values("test_net_c", ascending=False), 25)
B = A[(A.train_net_c > 0) & (A.test_net_c > 0) & (A.test_n >= 30)].sort_values("test_t_day", ascending=False)
print("\nBest by day t, train agrees:"); show(B, 25)
print(A[A.q == 0.95].groupby(["family", "h"]).test_net_c.median().unstack().round(2).to_string())
# hour-of-day size of moves (H21)
m = (mid - mid.shift(12)).abs().mean(axis=1)
print("abs 1h move by hour UTC (cents)", (100 * m.groupby(L.hour).mean()).round(3).to_dict())
