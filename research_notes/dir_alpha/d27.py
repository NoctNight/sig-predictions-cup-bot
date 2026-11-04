"""Drill-down 27: the wave. Loose signal (fresh gap >= th, both venues >= vm the same way in 24 h, mid 15-85, not chamber) under the strict mechanics of d15:
real polls only, fill at the next real poll, exit at the first real poll after the hold, one bet per race per 24 h.
Every event carries the wave count at its trigger: races with a loose signal the same D-way, minus the other way, itself excluded. Nothing fitted.
Placebos: the other leg of the same office and party, and the same list shifted 24 h earlier."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); P = L.P
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
fresh = P["fresh"].reindex(columns=L.cols).fillna(False)
mid = L.mid.where(fresh); fair = L.fair.where(fresh); pm = L.pm.where(fresh); k = L.k.where(fresh)
gap = fair - mid; n, m = mid.shape
nochm = L.office != "chamber"; dsign = L.dsign.values
near = lambda df, k_: df.shift(k_).combine_first(df.shift(k_ + 1)).combine_first(df.shift(k_ - 1))
S48 = gap - gap.rolling(576, min_periods=288).median(); s = np.sign(S48)
Sv = S48.values; Bv, Av = L.B, L.A; fv = fresh.values; Mf = L.mid.ffill().values
dp = (pm - near(pm, 288)); dk = (k - near(k, 288))


def first_real(t, j, limit=24):
    for x in range(t, min(t + limit, n)):
        if fv[x, j] and np.isfinite(Bv[x, j]) and np.isfinite(Av[x, j]): return x
    return None


def build(th, vm, hold=288):
    M = ((S48.abs() >= th) & (mid >= .15) & (mid <= .85) & (dp * s >= vm) & (dk * s >= vm)).values.copy(); M[:, ~nochm] = False
    # wave count: keep a signal alive for 3 h (polls are not all in the same bin)
    Dway = np.where(M, np.sign(np.nan_to_num(Sv)) * dsign, 0.0)
    W = pd.DataFrame(Dway).rolling(36, min_periods=1)
    up = W.max().values > 0; dn = W.min().values < 0
    races = pd.factorize(L.race)[0]; nr = races.max() + 1
    ru = np.zeros((n, nr), bool); rd = np.zeros((n, nr), bool)
    for j in range(m): ru[:, races[j]] |= up[:, j]; rd[:, races[j]] |= dn[:, j]
    cu, cd = ru.sum(1), rd.sum(1)
    out, last = [], {}
    for t in range(n):
        idx = np.flatnonzero(M[t])
        for j in sorted(idx, key=lambda q: -abs(Sv[t, q])):
            r = L.race[j]
            if t - last.get(r, -10**9) < 288: continue
            e = first_real(t + 1, j, 6)
            if e is None or e + hold >= n: continue
            x = first_real(e + hold, j)
            if x is None: continue
            sg = 1.0 if Sv[t, j] > 0 else -1.0
            px = Av[e, j] if sg > 0 else 1 - Bv[e, j]; val = Bv[x, j] if sg > 0 else 1 - Av[x, j]
            way = sg * dsign[j]
            wave = (cu[t] - cd[t]) * way - 1
            mv = (Mf[x] - Mf[e]) * dsign * way
            bnd = (Mf[e] >= .15) & (Mf[e] <= .85) & nochm & (dsign != 0) & np.isfinite(mv) & (L.race != r)
            last[r] = t
            out.append(dict(j=j, t=t, e=e, x=x, s=sg, net=val - px, gross=sg * (Mf[x, j] - Mf[e, j]), way=way, wave=wave, rest=np.nanmean(mv[bnd]), office=L.office[j], day=L.day[t], tr=L.idx[t] < L.SPLIT, sig=abs(Sv[t, j])))
    return pd.DataFrame(out)


for th, vm in ((.01, .005), (.015, .005), (.015, .01), (.02, .01)):
    E = build(th, vm)
    E["excess"] = E.gross - E.rest
    print(f"\n=== gap>={th*100:.1f}c venues>={vm*100:.1f}c: {len(E)} events, net {100*E.net.mean():.2f}c")
    g = pd.cut(E.wave, [-99, -5, 0, 3, 7, 15, 30, 300])
    o = E.groupby(g, observed=True).agg(n=("net", "size"), days=("day", "nunique"), net=("net", lambda v: 100 * v.mean()), win=("net", lambda v: (v > 0).mean()), rest=("rest", lambda v: 100 * v.mean()), excess=("excess", lambda v: 100 * v.mean()))
    o["train"] = E[E.tr].groupby(g, observed=True).net.mean() * 100; o["test"] = E[~E.tr].groupby(g, observed=True).net.mean() * 100
    print(o.round(2).to_string())
    print("D-way / R-way, wave >= 8"); print(E[E.wave >= 8].groupby("way").agg(n=("net", "size"), net=("net", lambda v: 100 * v.mean()), excess=("excess", lambda v: 100 * v.mean())).round(2).to_string())
    print("wave >= 8 by day"); print(E[E.wave >= 8].groupby("day").agg(n=("net", "size"), net=("net", lambda v: 100 * v.mean()), win=("net", lambda v: (v > 0).mean()), excess=("excess", lambda v: 100 * v.mean())).round(2).to_string())
    print("wave >= 8 by office"); print(E[E.wave >= 8].groupby("office").agg(n=("net", "size"), net=("net", lambda v: 100 * v.mean()), excess=("excess", lambda v: 100 * v.mean())).round(2).to_string())
    if th == .01:
        E.to_pickle("data/research_diralpha/wave_events.pkl")
        # placebo: same-office same-party leg of a random other race with mid 15-85, same entry and exit bins
        rng = np.random.default_rng(1); pl = []
        for r in E[E.wave >= 8].itertuples():
            c = np.flatnonzero((L.office == r.office) & (L.party == L.party[r.j]) & (Mf[r.e] >= .15) & (Mf[r.e] <= .85) & (L.race != L.race[r.j]))
            if not len(c): continue
            q = rng.choice(c, min(10, len(c)), replace=False); v = []
            for j in q:
                if np.isfinite([Bv[r.e, j], Av[r.e, j], Bv[r.x, j], Av[r.x, j]]).all():
                    v.append(Bv[r.x, j] - Av[r.e, j] if r.s > 0 else Bv[r.e, j] - Av[r.x, j])
            if v: pl.append(np.mean(v))
        print("placebo, same office and party, other races, wave >= 8: %.2fc on %d events" % (100 * np.mean(pl), len(pl)))
