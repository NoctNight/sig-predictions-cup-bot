"""Drill-down 29.
 A chamber legs: level of SIG against outside, does the level close, any trade on the gap (deep books, the natural big trade)
 B cheap favourites: is the level gap on favourites closing as the election nears?
 C delayed entry on the slow rule: nothing happens in the first 6 h, so wait and only enter if the gap is still there
 D own-side price >= .70 filter: fresh check on trades it was not found on (loose events, catch-up replay)
 E fair weighted to the deeper venue."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); P = L.P
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
mid, fair = L.mid, L.fair; gap = L.gap
Bv, Av, Mv, Gv = L.B, L.A, mid.values, gap.values; n, m = Mv.shape
print("=== A chamber legs")
ch = np.flatnonzero(L.office == "chamber")
for j in ch:
    g = pd.Series(Gv[:, j]); md = pd.Series(Mv[:, j])
    d = pd.DataFrame(dict(g=g * 100, day=L.day)).groupby("day").g.mean().round(2)
    f = pd.Series(L.fair.values[:, j])
    print(L.race[j], L.party[j], "mid %.3f fair %.3f spread %.2fc  gap by day:" % (md.iloc[-1], f.iloc[-1], 100 * L.spr.values[-1, j]), dict(d.tail(8)))
    for h in (288, 576):
        x = g.values[:-h]; y = (md.shift(-h) - md).values[:-h]; ok = np.isfinite(x) & np.isfinite(y) & (np.abs(x) > 0)
        if ok.sum() > 50: print(f"   next {h//12} h move vs gap: slope {np.polyfit(x[ok], y[ok], 1)[0]:.2f}, corr {np.corrcoef(x[ok], y[ok])[0,1]:.2f}, n {ok.sum()}")
print("=== B favourites: mean gap (outside - SIG) in cents, side above 80c, by day")
dsign = L.dsign.values
own = np.where(Mv >= .5, Mv, 1 - Mv); sgn = np.where(Mv >= .5, 1.0, -1.0)
G = Gv * sgn * 100
nochm = L.office != "chamber"
for lo, hi in ((.8, .9), (.9, .95), (.95, .985)):
    ok = (own >= lo) & (own < hi); ok[:, ~nochm] = False
    v = pd.Series(np.nanmean(np.where(ok, G, np.nan), axis=1)).groupby(L.day).mean().round(2)
    print(lo, hi, dict(v))
print("=== C delayed entry (slow)")
Es = pd.read_pickle("data/research_diralpha/r3_slow.pkl")
S48 = (gap - gap.rolling(576, min_periods=288).median()).values
for delay in (0, 12, 36, 72, 108, 144):
    for keep in (None, .01, .015):
        v = []; hrs = 36
        for r in Es.itertuples():
            e = r.e + delay; x = r.e + 432
            if x >= n or e >= n: continue
            if keep is not None and not (np.isfinite(S48[e, r.j]) and S48[e, r.j] * r.s >= keep): continue
            b0, a0, b1, a1 = Bv[e, r.j], Av[e, r.j], Bv[x, r.j], Av[x, r.j]
            if not np.isfinite([b0, a0, b1, a1]).all(): continue
            v.append(b1 - a0 if r.s > 0 else b0 - a1)
        if len(v) >= 8: print(f"delay {delay/12:.0f} h, keep gap>={keep}: n {len(v)} net to 36 h {100*np.mean(v):.2f}c win {np.mean(np.array(v)>0):.2f} per capital-day {100*np.mean(v)/((432-delay)/288):.2f}c")
print("=== D own-side price filter on other trade lists")
W = pd.read_pickle("data/research_diralpha/wave_events.pkl")
W["own"] = np.where(W.s > 0, Av[W.e.values, W.j.values], 1 - Bv[W.e.values, W.j.values])
print("loose events, 24 h"); print(W.groupby(pd.cut(W.own, [0, .3, .5, .7, .85, 1]), observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
Ws = W[W.sig < .02]; print("loose only (gap under 2c, none of the strict rule's trades)"); print(Ws.groupby(pd.cut(Ws.own, [0, .3, .5, .7, .85, 1]), observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
Q = pd.read_pickle("data/research_diralpha/catchup_replay.pkl"); print("catch-up replay columns", list(Q.columns), len(Q))
