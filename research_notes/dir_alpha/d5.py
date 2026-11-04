"""Drill-down 5: Sizhe's exit question on the filtered signal (both venues moved, flow not against, fresh gap >= 2c, mid 15-85).
 A how often right, time to X% of the eventual move  B remaining edge given time held and profit so far
 C exit grid (take profit, stop, max hold, signal-closed exit)  D when and how the catch-up happens (hour of day, taker flow or quote moves)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, H
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
pd.set_option("display.width", 200, "display.max_rows", 300, "display.max_columns", 40)
S = gap - gap.rolling(576, min_periods=288).median()
s = np.sign(S)
dpm, dk = L.lag(L.pm, 288), L.lag(L.k, 288)
fl = L.rsum(P["sflow"], 288)
both = (dpm * s >= .01) & (dk * s >= .01); flowok = fl * s >= -5000
M = (((mid >= .15) & (mid <= .85)) & both & flowok).values.copy()
M[:, L.office == "chamber"] = False
tr = L.trades(S, H["24h"], thr=.02, mask=M, cool=H["24h"])
E = pd.DataFrame(tr, columns=["j", "i", "s", "gross", "net"]); E[["j", "i"]] = E[["j", "i"]].astype(int)
E["ts"] = L.idx[E.i]; E["train"] = E.ts < L.SPLIT; E["office"] = L.office[E.j]; E["race"] = L.race[E.j]; E["sig"] = np.abs(S.values[E.i, E.j])
print("events", len(E), "win", round((E.net > 0).mean(), 3), "net", round(100 * E.net.mean(), 2))
B, A_, Mid = L.B, L.A, (L.B + L.A) / 2
T = Bid = None
LEN = 72 * 12 + 1
PATH = np.full((len(E), LEN), np.nan); VAL = np.full((len(E), LEN), np.nan); SIGP = np.full((len(E), LEN), np.nan); FP = np.full((len(E), LEN), np.nan)
for k, r in enumerate(E.itertuples()):
    n = min(LEN, len(L.idx) - r.i)
    PATH[k, :n] = r.s * (Mid[r.i:r.i + n, r.j] - Mid[r.i, r.j])
    px = A_[r.i, r.j] if r.s > 0 else 1 - B[r.i, r.j]
    VAL[k, :n] = (B[r.i:r.i + n, r.j] if r.s > 0 else 1 - A_[r.i:r.i + n, r.j]) - px     # net P&L if sold now at the touch
    SIGP[k, :n] = r.s * S.values[r.i:r.i + n, r.j]
    FP[k, :n] = r.s * (fair.values[r.i:r.i + n, r.j] - fair.values[r.i, r.j])
Hs = [1, 2, 4, 6, 8, 12, 16, 20, 24, 30, 36, 48, 72]
print("\nA. After entry, by hours held")
print(pd.DataFrame({"h": Hs, "n": [np.isfinite(PATH[:, h * 12 if h < 72 else -1]).sum() for h in Hs], "right %": [100 * np.nanmean(PATH[:, min(h * 12, LEN - 1)][np.isfinite(PATH[:, min(h * 12, LEN - 1)])] > 0) for h in Hs], "SIG move c": [100 * np.nanmean(PATH[:, min(h * 12, LEN - 1)]) for h in Hs], "outside move c": [100 * np.nanmean(FP[:, min(h * 12, LEN - 1)]) for h in Hs], "net if sold c": [100 * np.nanmean(VAL[:, min(h * 12, LEN - 1)]) for h in Hs], "p10 net": [100 * np.nanquantile(VAL[:, min(h * 12, LEN - 1)], .1) for h in Hs], "net/hour": [100 * np.nanmean(VAL[:, min(h * 12, LEN - 1)]) / h for h in Hs]}).round(2).to_string(index=False))
ev = PATH[:, 48 * 12]; ok = np.isfinite(ev)
print(f"\nCorrect at 48 h: {(ev[ok] > 0).mean():.2f} of {ok.sum()}; move when right {100*ev[ok & (ev>0)].mean():.2f}c, when wrong {100*ev[ok & (ev<=0)].mean():.2f}c")
good = ok & (ev >= .01)
for f in (.25, .5, .6, .75, .9, 1.0):
    tt = np.array([(lambda h: h[0] / 12 if len(h) else np.nan)(np.flatnonzero(PATH[k, :577] >= f * ev[k])) for k in np.flatnonzero(good)])
    print(f"reach {int(f*100)}% of the 48 h move: median {np.nanmedian(tt):.1f} h (p25 {np.nanquantile(tt,.25):.1f}, p75 {np.nanquantile(tt,.75):.1f})")
# B remaining edge: from time t, conditional on P&L so far, mean further SIG move to 36 h
print("\nB. Expected further net from holding on, by hours held and profit so far (cents; n)")
rows = []
end = 36 * 12
for h in (2, 4, 8, 12, 16, 20, 24, 30):
    t = h * 12
    cur = VAL[:, t]; fut = VAL[:, end] - cur
    for lo, hi, lab in ((-1, -.015, "down >1.5c"), (-.015, -.005, "down .5-1.5"), (-.005, .005, "flat"), (.005, .015, "up .5-1.5"), (.015, .03, "up 1.5-3"), (.03, 1, "up >3")):
        m = (cur > lo) & (cur <= hi) & np.isfinite(fut)
        if m.sum() >= 4:
            rows.append(dict(hours=h, pnl_now=lab, n=int(m.sum()), further=100 * fut[m].mean(), better_to_hold=(fut[m] > 0).mean()))
G = pd.DataFrame(rows)
print(G.pivot(index="pnl_now", columns="hours", values="further").reindex(["down >1.5c", "down .5-1.5", "flat", "up .5-1.5", "up 1.5-3", "up >3"]).round(2).to_string())
print(G.pivot(index="pnl_now", columns="hours", values="n").reindex(["down >1.5c", "down .5-1.5", "flat", "up .5-1.5", "up 1.5-3", "up >3"]).to_string())


# C exit grid, touch prices, first exit wins
def sim(rows_, tp, sl, tmax, close, minhold=0):
    out = []
    for k in rows_:
        res, held = None, None
        lim = min(int(tmax * 12), LEN - 1)
        for t in range(1, lim + 1):
            v = VAL[k, t]
            if not np.isfinite(v): continue
            if tp is not None and v >= tp: res, held = v, t; break
            if sl is not None and v <= -sl and t >= minhold: res, held = v, t; break
            if close is not None and t >= minhold and SIGP[k, t] <= close * E.sig.values[k]: res, held = v, t; break
        if res is None:
            w = VAL[k, :lim + 1]; f = np.flatnonzero(np.isfinite(w))
            res, held = w[f[-1]], f[-1]
        out.append((res, held / 12))
    return np.array(out)


a = np.flatnonzero(E.train.values); b = np.flatnonzero(~E.train.values)
R = []
for tmax in (8, 12, 18, 24, 30, 36, 48):
    for tp in (None, .015, .02, .03, .04, .05, .08):
        for sl in (None, .02, .03, .04, .06):
            for cl in (None, .5, .25, 0.0, -.5):
                x, y = sim(a, tp, sl, tmax, cl), sim(b, tp, sl, tmax, cl)
                z = np.r_[x, y]
                R.append(dict(tmax=tmax, tp=tp, sl=sl, close=cl, train=100 * x[:, 0].mean(), test=100 * y[:, 0].mean(), net=100 * z[:, 0].mean(), hours=z[:, 1].mean(), win=(z[:, 0] > 0).mean(), p05=100 * np.quantile(z[:, 0], .05), worst=100 * z[:, 0].min(), per_hour=100 * z[:, 0].mean() / z[:, 1].mean()))
R = pd.DataFrame(R); R.to_pickle("data/research_diralpha/exit_grid2.pkl")
print("\nC. Exit grid. Hold only:"); print(R[R.tp.isna() & R.sl.isna() & R.close.isna()].round(2).to_string(index=False))
ok_ = R[(R.train > 0) & (R.test > 0)]
print("\nHighest net, train and test both positive:"); print(ok_.sort_values(["net"], ascending=False).head(10).round(2).to_string(index=False))
print("\nFastest money (net per hour held), net at least 1.2c:"); print(ok_[ok_.net >= 1.2].sort_values("per_hour", ascending=False).head(10).round(3).to_string(index=False))
print("\nStops only, 24 h and 36 h:"); print(R[R.tp.isna() & R.close.isna() & R.tmax.isin([24, 36])].round(2).to_string(index=False))
print("\nTake profit only, 36 h:"); print(R[R.sl.isna() & R.close.isna() & (R.tmax == 36)].round(2).to_string(index=False))
# D when does the catch-up happen
inc = np.diff(PATH, axis=1)
hour_of = np.array([[(pd.Timestamp(int(E.ts.values[k] + t * 300), unit="s").hour) for t in range(LEN - 1)] for k in range(len(E))])
bh = pd.Series(inc[:, :288].ravel()).groupby(hour_of[:, :288].ravel()).mean() * 12 * 100
print("\nD. Catch-up speed by hour UTC (cents per hour, first 24 h after entry):"); print(bh.round(3).to_dict())
fl_val = P["sflow"].values
rows = []
for k, r in enumerate(E.itertuples()):
    f = r.s * np.nansum(fl_val[r.i + 1:r.i + 288, r.j]); v = np.nansum(P["vol"].values[r.i + 1:r.i + 288, r.j])
    rows.append((f, v, PATH[k, 287], FP[k, 287]))
Q = pd.DataFrame(rows, columns=["flow", "vol", "move", "outside"]).dropna()
print("During the hold: corr(move, taker flow our way)", round(Q.move.corr(Q.flow), 2), " share of events with flow our way", round((Q.flow > 0).mean(), 2), " corr(move, outside further move)", round(Q.move.corr(Q.outside), 2))
print(Q.groupby(pd.qcut(Q.flow, 3, duplicates="drop"), observed=True).move.agg(["size", lambda x: 100 * x.mean()]).round(2).to_string())
print("mean SIG move at 24 h %.2fc, of which outside moved further %.2fc" % (100 * Q.move.mean(), 100 * Q.outside.mean()))
