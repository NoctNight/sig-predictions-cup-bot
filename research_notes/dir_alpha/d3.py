"""Drill-down 3: event table for the fresh-gap signal, then
 A what strengthens it, B the path after entry (hit rate and time to X% of the eventual move), C exit rule search, D walk-forward."""
import sys, numpy as np, pandas as pd, pickle
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, H
L = Lab()
mid, gap, P = L.mid, L.gap, L.P
pd.set_option("display.width", 200, "display.max_rows", 300, "display.max_columns", 40)
S48 = gap - gap.rolling(576, min_periods=288).median()
M = ((mid >= .15) & (mid <= .85)).values.copy()
M[:, L.office == "chamber"] = False
hold = H["24h"]
tr = L.trades(S48, hold, thr=0.02, mask=M)
E = pd.DataFrame(tr, columns=["j", "i", "s", "gross", "net"]); E[["j", "i"]] = E[["j", "i"]].astype(int)
j, i = E.j.values, E.i.values
g = lambda df: df.values[i, j]
E["race"] = L.race[j]; E["office"] = L.office[j]; E["party"] = L.party[j]; E["day"] = L.day[i]; E["hour"] = L.hour[i]
E["sig"] = np.abs(g(S48)); E["mid"] = g(mid); E["spr"] = g(L.spr); E["gaplvl"] = g(gap); E["ts"] = L.idx[i]
E["align"] = np.sign(E.gaplvl) == E.s
E["absgap"] = np.abs(E.gaplvl)
for n, k in (("4h", 48), ("12h", 144), ("24h", 288)):
    E["dfair" + n] = E.s * g(L.lag(L.fair, k)); E["dmid" + n] = E.s * g(L.lag(mid, k))
    E["dpm" + n] = E.s * g(L.lag(L.pm, k)); E["dk" + n] = E.s * g(L.lag(L.k, k))
    v = L.rsum(P["vol"], k); E["vol" + n] = g(v); E["flow" + n] = E.s * g(L.rsum(P["sflow"], k))
E["both"] = (E.dpm24h >= .01) & (E.dk24h >= .01)
E["venuegap"] = np.abs(g(L.pm - L.k))
E["depth"] = g(P["pm_depth_usd"])
E["dside"] = E.s * np.where(E.party == "D", 1, -1)
E["speed"] = E.dfair4h / E.dfair24h.where(E.dfair24h.abs() > .005)
E["sigfrac"] = E.dmid24h / E.dfair24h.where(E.dfair24h.abs() > .005)
E["train"] = E.ts < L.SPLIT
E.to_pickle("data/research_diralpha/events.pkl")
print("events", len(E), "net", round(100 * E.net.mean(), 2), "win", round((E.net > 0).mean(), 3), flush=True)


def tab(col, bins=None, q=None):
    x = E[col]
    if q: b = pd.qcut(x, q, duplicates="drop")
    elif bins is not None: b = pd.cut(x, bins)
    else: b = x
    t = E.groupby(b, observed=True).agg(n=("net", "size"), net=("net", lambda v: 100 * v.mean()), win=("net", lambda v: (v > 0).mean()), races=("race", "nunique"))
    t["net_train"] = E[E.train].groupby(b, observed=True).net.mean() * 100
    t["net_test"] = E[~E.train].groupby(b, observed=True).net.mean() * 100
    print("\n--", col); print(t.round(2).to_string())


print("\nA. WHAT STRENGTHENS IT")
tab("sig", [.02, .025, .03, .04, .05, .08, .3])
for c in ("office", "dside", "s", "align", "both"): tab(c)
tab("mid", [.15, .25, .35, .5, .65, .75, .85]); tab("absgap", q=4); tab("venuegap", q=4); tab("speed", q=4); tab("sigfrac", q=4); tab("vol24h", q=4); tab("vol4h", q=4); tab("flow24h", q=4); tab("depth", q=4); tab("spr", [0, .0051, .0101, .03])
tab("hour", [-1, 5, 11, 17, 23])
# B path after entry
Bid, Ask, Mid = L.B, L.A, ((L.B + L.A) / 2)
HH = [12, 48, 96, 144, 216, 288, 432, 576, 864]
rows = []
for r in E.itertuples():
    p = Mid[r.i:r.i + 865, r.j] - Mid[r.i, r.j]
    rows.append(r.s * p if len(p) == 865 else np.r_[r.s * p, [np.nan] * (865 - len(p))])
PATH = np.array(rows)
np.save("data/research_diralpha/paths.npy", PATH)
print("\nB. PATH: signed SIG mid move in cents after entry (all events)")
t = pd.DataFrame({"h": [x / 12 for x in HH], "n": [np.isfinite(PATH[:, x]).sum() for x in HH], "mean": [100 * np.nanmean(PATH[:, x]) for x in HH], "median": [100 * np.nanmedian(PATH[:, x]) for x in HH], "p10": [100 * np.nanquantile(PATH[:, x], .1) for x in HH], "p90": [100 * np.nanquantile(PATH[:, x], .9) for x in HH], "right": [np.nanmean(PATH[:, x][np.isfinite(PATH[:, x])] > 0) for x in HH]})
print(t.round(2).to_string(index=False))
for th in (.03,):
    sel = (E.sig >= th).values
    t = pd.DataFrame({"h": [x / 12 for x in HH], "n": [np.isfinite(PATH[sel, x]).sum() for x in HH], "mean": [100 * np.nanmean(PATH[sel, x]) for x in HH], "median": [100 * np.nanmedian(PATH[sel, x]) for x in HH], "p10": [100 * np.nanquantile(PATH[sel, x], .1) for x in HH], "right": [np.nanmean(PATH[sel, x][np.isfinite(PATH[sel, x])] > 0) for x in HH]})
    print(f"signal >= {th}"); print(t.round(2).to_string(index=False))
# time to X% of eventual (48h) move, among those right at 48 h
ev = PATH[:, 576]
good = np.isfinite(ev) & (ev >= .01)
print("\nright at 48 h by at least 1c:", good.sum(), "of", np.isfinite(ev).sum())
for f in (.25, .5, .75, 1.0):
    tt = []
    for k in np.flatnonzero(good):
        hit = np.flatnonzero(PATH[k, :577] >= f * ev[k])
        tt.append(hit[0] / 12 if len(hit) else np.nan)
    tt = np.array(tt)
    print(f"reach {int(f*100)}% of the 48 h move: median {np.nanmedian(tt):.1f} h, p25 {np.nanquantile(tt,.25):.1f}, p75 {np.nanquantile(tt,.75):.1f}")
print("max favourable and adverse in 48 h: MFE median %.2f, MAE median %.2f, MAE p10 %.2f cents" % (100 * np.nanmedian(np.nanmax(PATH[:, :577], axis=1)), 100 * np.nanmedian(np.nanmin(PATH[:, :577], axis=1)), 100 * np.nanquantile(np.nanmin(PATH[:, :577], axis=1), .1)))


# C exit rule search: touch-to-touch. tp and sl in cents vs entry price; tmax in hours; exit on gap closing to `close` of the entry signal
def sim(sel, tp, sl, tmax, close=None, wait=0):
    out = []
    for r in E[sel].itertuples():
        i0, j0, s = r.i, r.j, r.s
        px = Ask[i0, j0] if s > 0 else 1 - Bid[i0, j0]        # price paid for the side bought
        end = min(i0 + int(tmax * 12), Bid.shape[0] - 1)
        res = None
        sv = S48.values[:, j0] * s
        for t in range(i0 + 1, end + 1):
            if not (np.isfinite(Bid[t, j0]) and np.isfinite(Ask[t, j0])): continue
            val = Bid[t, j0] if s > 0 else 1 - Ask[t, j0]      # what we can sell it for now
            pl = val - px
            if tp and pl >= tp: res = pl; break
            if sl and pl <= -sl: res = pl; break
            if close is not None and t - i0 >= wait and np.isfinite(sv[t]) and sv[t] <= close * r.sig: res = pl; break
        if res is None:
            t = end
            while t > i0 and not np.isfinite(Bid[t, j0]): t -= 1
            res = (Bid[t, j0] if s > 0 else 1 - Ask[t, j0]) - px
        out.append((res, t - i0))
    return np.array(out)


print("\nC. EXIT RULES (train / test net cents per trade, mean hours held), events with signal >= 3c")
G = []
base = E.sig >= .03
for tmax in (12, 24, 36, 48, 72):
    for tp in (None, .01, .015, .02, .03, .04, .06):
        for sl in (None, .01, .02, .03, .05):
            for close in (None, .5, .25, 0.0):
                a = sim(base & E.train, tp, sl, tmax, close); b = sim(base & ~E.train, tp, sl, tmax, close)
                if len(a) < 5 or len(b) < 5: continue
                G.append(dict(tmax=tmax, tp=tp, sl=sl, close=close, n=len(a) + len(b), train=100 * a[:, 0].mean(), test=100 * b[:, 0].mean(), hours=np.r_[a[:, 1], b[:, 1]].mean() / 12, worst=100 * np.r_[a[:, 0], b[:, 0]].min(), win=(np.r_[a[:, 0], b[:, 0]] > 0).mean()))
    print("tmax", tmax, len(G), flush=True)
G = pd.DataFrame(G); G.to_pickle("data/research_diralpha/exit_grid.pkl")
G["permin"] = (G.train + G.test) / 2 / G.hours
print("\nBest by net:"); print(G.sort_values("test", ascending=False).head(12).round(2).to_string(index=False))
G2 = G[(G.train > 0) & (G.test > 0)].copy()
print("\nBest net per hour held (fast capital turnover):"); print(G2.sort_values("permin", ascending=False).head(14).round(3).to_string(index=False))
print("\nHold-only baseline:"); print(G[G.tp.isna() & G.sl.isna() & G.close.isna()].round(2).to_string(index=False))
print("\nStops alone (no take profit) at 48 h:"); print(G[G.tp.isna() & G.close.isna() & (G.tmax == 48)].round(2).to_string(index=False))
