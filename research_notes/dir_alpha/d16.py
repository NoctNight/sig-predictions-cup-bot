"""Drill-down 16: exit answers on the strict trades only. Paths, time to X% of the eventual move, expected further gain, stops, and resting exits.
Resting exit model, conservative: a sell at price T rests from entry; it fills only when the market ask has moved above T (the quote passed through), so it is
never first in queue at T. If unfilled at the time limit we cross at the bid."""
import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl")
Bv, Av = L.B, L.A
Mid = (Bv + Av) / 2
fair = L.fair.values
LEN = 60 * 12 + 1
out = {}
for name, E in (("slow", Es), ("fast", Ef)):
    N = len(E)
    VAL = np.full((N, LEN), np.nan); MV = VAL.copy(); FO = VAL.copy(); ASKV = VAL.copy()
    for k, r in enumerate(E.itertuples()):
        n = min(LEN, Bv.shape[0] - r.e)
        px = Av[r.e, r.j] if r.s > 0 else 1 - Bv[r.e, r.j]
        VAL[k, :n] = (Bv[r.e:r.e + n, r.j] if r.s > 0 else 1 - Av[r.e:r.e + n, r.j]) - px
        ASKV[k, :n] = (Av[r.e:r.e + n, r.j] if r.s > 0 else 1 - Bv[r.e:r.e + n, r.j]) - px     # the side's own ask, relative to entry
        MV[k, :n] = r.s * (Mid[r.e:r.e + n, r.j] - Mid[r.e, r.j])
        FO[k, :n] = r.s * (fair[r.e:r.e + n, r.j] - fair[r.e, r.j])
    Hs = [1, 2, 4, 6, 8, 12, 16, 20, 24, 30, 36, 48]
    T = pd.DataFrame({"hours": Hs, "n": [int(np.isfinite(VAL[:, h * 12]).sum()) for h in Hs], "right_pct": [100 * np.nanmean(MV[:, h * 12][np.isfinite(MV[:, h * 12])] > 0) for h in Hs], "SIG_move": [100 * np.nanmean(MV[:, h * 12]) for h in Hs], "outside_move": [100 * np.nanmean(FO[:, h * 12]) for h in Hs], "net_if_sold": [100 * np.nanmean(VAL[:, h * 12]) for h in Hs], "se": [100 * np.nanstd(VAL[:, h * 12]) / np.sqrt(np.isfinite(VAL[:, h * 12]).sum()) for h in Hs], "p10": [100 * np.nanquantile(VAL[:, h * 12], .1) for h in Hs]})
    T["per_hour"] = T.net_if_sold / T.hours
    print(f"\n=== {name}: {N} trades"); print(T.round(2).to_string(index=False))
    out[name] = T.to_dict("list")
    ev = MV[:, 36 * 12]; ok = np.isfinite(ev)
    print(f"right at 36 h {100*(ev[ok]>0).mean():.0f}% of {ok.sum()}; when right +{100*ev[ok&(ev>0)].mean():.2f}c, when wrong {100*ev[ok&(ev<=0)].mean():.2f}c")
    good = ok & (ev >= .01)
    for f in (.25, .5, .75, 1.0):
        tt = np.array([(lambda h: h[0] / 12 if len(h) else np.nan)(np.flatnonzero(MV[k, :433] >= f * ev[k])) for k in np.flatnonzero(good)])
        print(f"  reach {int(f*100):3d}% of the 36 h move: median {np.nanmedian(tt):4.1f} h, middle half {np.nanquantile(tt,.25):.1f} to {np.nanquantile(tt,.75):.1f}")
    end = 36 * 12
    rows = []
    for h in (2, 4, 8, 12, 16, 20, 24):
        t = h * 12; cur = VAL[:, t]; fut = VAL[:, end] - cur
        for lo, hi, lab in ((-1, -.01, "losing 1c+"), (-.01, .005, "about flat"), (.005, .02, "up 0.5-2c"), (.02, .035, "up 2-3.5c"), (.035, 1, "up 3.5c+")):
            m = (cur > lo) & (cur <= hi) & np.isfinite(fut)
            if m.sum() >= 3: rows.append(dict(hours=h, now=lab, n=int(m.sum()), further=100 * fut[m].mean()))
    G = pd.DataFrame(rows)
    order = ["losing 1c+", "about flat", "up 0.5-2c", "up 2-3.5c", "up 3.5c+"]
    print("expected further net if held to 36 h (cents):"); print(G.pivot(index="now", columns="hours", values="further").reindex(order).round(2).to_string())
    print("n:"); print(G.pivot(index="now", columns="hours", values="n").reindex(order).to_string())

    def rule(tp=None, sl=None, tmax=36, rest=None):
        res = []
        for k in range(N):
            lim = min(int(tmax * 12), LEN - 1); got = None
            for t in range(1, lim + 1):
                v = VAL[k, t]
                if not np.isfinite(v): continue
                if rest is not None and ASKV[k, t] > rest: got = rest; break
                if tp is not None and v >= tp: got = v; break
                if sl is not None and v <= -sl: got = v; break
            if got is None:
                w = VAL[k, :lim + 1]; f = np.flatnonzero(np.isfinite(w)); got = w[f[-1]]
            res.append(got)
        return np.array(res)
    print("Exit rules, net cents (win):")
    tmaxes = (4, 8, 12, 24, 36) if name == "fast" else (24, 36, 48)
    for tm in tmaxes:
        base = rule(tmax=tm)
        s_ = f"max {tm:2d} h: cross only {100*base.mean():5.2f} ({(base>0).mean():.2f})"
        for r_ in (.02, .03, .04, .05):
            x = rule(rest=r_, tmax=tm); s_ += f" | rest at +{int(r_*100)}c {100*x.mean():5.2f} ({(x>0).mean():.2f})"
        print(s_)
    for sl in (.015, .02, .03, .04):
        x = rule(sl=sl, tmax=36 if name == "slow" else 24); print(f"  stop at -{100*sl:.1f}c: {100*x.mean():5.2f}, worst {100*x.min():.1f}")
json.dump(out, open("data/research_diralpha/exit_tables.json", "w"), default=float)
