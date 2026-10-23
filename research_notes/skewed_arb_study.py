"""Skewed arb (Sizhe): when a 2-leg race's overround reverts, is the move shared unequally between the legs, and is it
worth skewing the deviation-churn ("high-frequency arb") sell-set toward that asymmetry?

Read-only: copies data/sigbot.sqlite (via arb_convergence_study.load_db), 0 venue reads.
Panels (same as arb_convergence.md): A = training, 5-min trade-close proxy 14-21 Oct, per leg 30-min trailing median
+- the leg's median half-spread; B = test, real touch from venue_quotes (21 Oct 23:50 -> latest poll).

Decomposition, per race and step: dfav = dsum/2 + dq, ddog = dsum/2 - dq. dq = (dfav - ddog)/2 is the part of the move
that is directional; a skew earns s * (change of the leg we hold less NO on - change of the other), so it pays only if
E[dq | entry] != 0. Ticks of 0.005 throughout.

Usage: python research_notes/skewed_arb_study.py            (all stages, writes data/research_skewed_arb/)
       python research_notes/skewed_arb_study.py --figures  (redraw from results.pkl)
"""
from __future__ import annotations

import collections
import json
import os
import pickle
import sys
from datetime import datetime, timezone

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "research_notes"))
import arb_convergence_study as acs  # noqa: E402  (loaders; main() only runs as a script)

OUT = os.path.join(HERE, "data", "research_skewed_arb")
FIG = os.path.join(HERE, "research_notes", "figures")
os.makedirs(OUT, exist_ok=True)
T = acs.T
UTC = timezone.utc
W24, MINW = 288, 144                       # 24-h trailing median, needs >= 12 h of samples
HZ = [("1 h", 12), ("6 h", 72), ("24 h", 288)]
BANDS = [(0.5, 0.7), (0.7, 0.85), (0.85, 0.95), (0.95, 1.01)]
BAND_LAB = ["0.50-0.70", "0.70-0.85", "0.85-0.95", "> 0.95"]
RNG = np.random.default_rng(7)


# ------------------------------------------------------------------------------------------------ panels
def ffill_on(ts, v, grid):
    i = np.searchsorted(ts, grid, side="right") - 1
    return np.where(i >= 0, v[np.maximum(i, 0)], np.nan)


def trail_med(x, w, minw):
    pad = np.concatenate([np.full(w - 1, np.nan), x])
    win = sliding_window_view(pad, w)
    cnt = np.isfinite(win).sum(1)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            m = np.nanmedian(win, axis=1)
    return np.where(cnt >= minw, m, np.nan)


class LegPanel:
    """2-leg races only: per race (legA, legB) bid/ask/mid arrays on a common grid, party labels."""

    def __init__(self, name, grid):
        self.name, self.grid, self.r = name, grid, {}


def build_panels(smooth=6):
    vq, arb, qs, tape, cyc = acs.load_db()
    races, mk = acs.races_from_markets()
    two = {r: ex for r, ex in races.items() if len(ex) == 2}
    PB, spreads, leg, fresh = acs.panel_B(vq, races)
    B = LegPanel("B", PB.grid)
    for r, exs in two.items():
        if not all(e in leg for e in exs):
            continue
        d = []
        for e in exs:
            bb, aa = leg[e]
            bb = np.where(fresh, bb, np.nan)
            aa = np.where(fresh, aa, np.nan)
            d.append(dict(ex=e, party=mk[e][1], bid=bb, ask=aa, mid=(bb + aa) / 2))
        B.r[r] = d
    cc = acs.closes_from_candles(os.path.join(HERE, "data", "candles_5m.json"))
    A = LegPanel("A", np.arange(acs.A0, acs.A1 + 1, acs.STEP))
    for r, exs in two.items():
        if not all(e in cc for e in exs):
            continue
        d = []
        for e in exs:
            px = ffill_on(*cc[e], A.grid)
            px = trail_med(px, smooth, 1)                 # 30-min trailing median (smooth=6), as arb_convergence
            hs = spreads.get(e, T) / 2
            d.append(dict(ex=e, party=mk[e][1], bid=px - hs, ask=px + hs, mid=px))
        A.r[r] = d
    for P in (A, B):
        for r, d in P.r.items():
            o = d[0]["mid"] + d[1]["mid"] - 1
            d[0]["o"] = o
            d[0]["dev"] = o - trail_med(o, W24, MINW)
            d[0]["sb"] = d[0]["bid"] + d[1]["bid"]
            d[0]["sa"] = d[0]["ask"] + d[1]["ask"]
            for k in (0, 1):                              # each leg's own excess over its 24-h median
                d[k]["exc"] = d[k]["mid"] - trail_med(d[k]["mid"], W24, MINW)
    return A, B


def day_of(t):
    return datetime.fromtimestamp(t, UTC).strftime("%m-%d")


# ------------------------------------------------------------------------------------------------ stats
def cl_t(x, g):
    """Mean, cluster-robust SE and t (clusters g)."""
    x, g = np.asarray(x, float), np.asarray(g)
    n = len(x)
    if n < 3:
        return dict(mean=float(np.mean(x)) if n else np.nan, se=np.nan, t=np.nan, n=n, G=len(set(g)))
    m = x.mean()
    cs = collections.defaultdict(float)
    for xi, gi in zip(x - m, g):
        cs[gi] += xi
    G = len(cs)
    se = np.sqrt(sum(v * v for v in cs.values()) * G / max(G - 1, 1)) / n
    return dict(mean=float(m), se=float(se), t=float(m / se) if se > 0 else np.nan, n=n, G=G)


def signflip_p(x, g, nperm=10000):
    """Two-sided sign-flip permutation p of sum(x) with whole clusters flipped (H0: E[x]=0, symmetric)."""
    cs = collections.defaultdict(float)
    for xi, gi in zip(x, g):
        cs[gi] += xi
    v = np.array(list(cs.values()))
    if len(v) < 3:
        return np.nan
    obs = abs(v.sum())
    flips = RNG.choice([-1.0, 1.0], size=(nperm, len(v)))
    return float((np.abs(flips @ v) >= obs - 1e-12).mean())


# ------------------------------------------------------------------------------------------------ stage 1
def episodes(P, thr, sign=+1, cooldown=12):
    """Onsets where sign*dev crosses >= thr ticks (first point of a run, then no new onset in the race for 1 h)."""
    out = []
    for r, d in P.r.items():
        dev = sign * d[0]["dev"] / T
        on = np.nan_to_num(dev >= thr - 1e-9, nan=0).astype(bool)
        last = -10 ** 9
        prev = False
        for i in range(len(on)):
            if not np.isfinite(dev[i]):
                continue                                  # outage: keep prev state
            if on[i] and not prev and i - last >= cooldown:
                out.append((r, i))
                last = i
            prev = on[i]
    return out


def leg_moves(P, r, i, h):
    d = P.r[r]
    j = i + h
    if j >= len(P.grid):
        return None
    m0 = np.array([d[0]["mid"][i], d[1]["mid"][i]])
    m1 = np.array([d[0]["mid"][j], d[1]["mid"][j]])
    if not (np.all(np.isfinite(m0)) and np.all(np.isfinite(m1))):
        return None
    f = int(np.argmax(m0))
    u = 1 - f
    dm = (m1 - m0) / T
    jump = int(np.argmax([d[0]["exc"][i], d[1]["exc"][i]])) if np.all(np.isfinite([d[0]["exc"][i], d[1]["exc"][i]])) else -1
    return dict(race=r, day=day_of(P.grid[i]), pf=float(m0[f]), dfav=float(dm[f]), ddog=float(dm[u]),
                dsum=float(dm.sum()), dq=float((dm[f] - dm[u]) / 2), jump_is_fav=(jump == f) if jump >= 0 else None,
                dog_ticks=float(m0[u] / T), dev0=float(d[0]["dev"][i] / T))


def band_of(pf):
    for k, (lo, hi) in enumerate(BANDS):
        if lo <= pf < hi:
            return k
    return None


def summarise(rows, key="dq"):
    x = [r[key] for r in rows]
    s = cl_t(x, [r["race"] for r in rows])
    sd = cl_t(x, [r["day"] for r in rows])
    out = dict(n=s["n"], races=s["G"], days=sd["G"], mean=s["mean"], t_race=s["t"],
               t_day=sd["t"] if sd["G"] >= 4 else np.nan,                  # < 4 day clusters: no usable SE
               p_perm=signflip_p(x, [r["race"] for r in rows]) if len(x) >= 3 else np.nan)
    if rows:
        out.update(dsum=float(np.mean([r["dsum"] for r in rows])), dfav=float(np.mean([r["dfav"] for r in rows])),
                   ddog=float(np.mean([r["ddog"] for r in rows])))
        out["fav_share"] = out["dfav"] / out["dsum"] if abs(out["dsum"]) > 1e-9 else np.nan
    return out


def stage1(A, B):
    res = {}
    for P in (A, B):
        for thr, sign, lab in ((2, 1, "dev>=+2"), (4, 1, "dev>=+4"), (2, -1, "dev<=-2"), (4, -1, "dev<=-4")):
            E = episodes(P, thr, sign)
            for hl, h in HZ:
                rows = [m for m in (leg_moves(P, r, i, h) for r, i in E) if m]
                key = (P.name, lab, hl)
                res[key] = dict(all=summarise(rows),
                                band={b: summarise([m for m in rows if band_of(m["pf"]) == b]) for b in range(4)},
                                jump_fav=summarise([m for m in rows if m["jump_is_fav"] is True]),
                                jump_dog=summarise([m for m in rows if m["jump_is_fav"] is False]),
                                no_floor=summarise([m for m in rows if m["dog_ticks"] > 4]),
                                rows=rows)
    # unconditional: every race every hour
    unc = {}
    for P in (A, B):
        for hl, h in HZ:
            rows = []
            for r in P.r:
                for i in range(W24, len(P.grid), 12):
                    m = leg_moves(P, r, i, h)
                    if m:
                        rows.append(m)
            unc[(P.name, hl)] = dict(band={b: summarise([m for m in rows if band_of(m["pf"]) == b], "dfav") for b in range(4)},
                                     band_dog={b: summarise([m for m in rows if band_of(m["pf"]) == b], "ddog") for b in range(4)},
                                     band_dq={b: summarise([m for m in rows if band_of(m["pf"]) == b], "dq") for b in range(4)})
    return res, unc


def fmt(s, k="mean"):
    if not s or s.get("n", 0) == 0:
        return "-"
    return f"{s[k]:+.2f}"


def md_stage1(res, unc):
    L = ["## Stage 1: who does the reverting (ticks; dq = (dfav - ddog)/2, + = favourite gains vs underdog)\n",
         "| panel | episodes | h | n | races | mean dsum | mean dfav | mean ddog | fav share of dsum | mean dq | t (race) | t (day) | perm p | dq, jump leg = fav | dq, jump leg = dog | dq, dog > 4 ticks |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for (pn, lab, hl), v in res.items():
        a = v["all"]
        if a["n"] == 0:
            continue
        jf, jd, nf = v["jump_fav"], v["jump_dog"], v["no_floor"]
        L.append(f"| {pn} | {lab} | {hl} | {a['n']} | {a['races']} | {a['dsum']:+.2f} | {a['dfav']:+.2f} | {a['ddog']:+.2f} | "
                 f"{a['fav_share']:.0%} | {a['mean']:+.2f} | {a['t_race']:+.1f} | {a['t_day']:+.1f} | {a['p_perm']:.3f} | "
                 f"{fmt(jf)} (n {jf['n']}, t {jf['t_race']:+.1f}) | {fmt(jd)} (n {jd['n']}, t {jd['t_race']:+.1f}) | {fmt(nf)} (t {nf['t_race']:+.1f}) |")
    L += ["\nBy favourite band, episodes dev >= +2 (dq mean, t race, n):\n",
          "| panel | h | " + " | ".join(BAND_LAB) + " |", "|---|---|" + "---|" * 4]
    for (pn, lab, hl), v in res.items():
        if lab != "dev>=+2":
            continue
        L.append(f"| {pn} | {hl} | " + " | ".join(
            f"{fmt(v['band'][b])} (t {v['band'][b]['t_race']:+.1f}, n {v['band'][b]['n']})" if v['band'][b]['n'] else "-" for b in range(4)) + " |")
    L += ["\nUnconditional, every race every hour: mean dfav / mean ddog (t race of dq):\n",
          "| panel | h | " + " | ".join(BAND_LAB) + " |", "|---|---|" + "---|" * 4]
    for (pn, hl), v in unc.items():
        L.append(f"| {pn} | {hl} | " + " | ".join(
            f"{fmt(v['band'][b])} / {fmt(v['band_dog'][b])} (t {v['band_dq'][b]['t_race']:+.1f}, n {v['band'][b]['n']})" if v['band'][b]['n'] else "-"
            for b in range(4)) + " |")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ stage 2 (mechanics)
def stage2(A, B):
    """Favourite > 0.95 cell: per-race spread of dq (6 h, every race every hour), sub-bands, dog floor."""
    out = {}
    for P in (A, B):
        rows = []
        for r in P.r:
            for i in range(W24, len(P.grid), 12):
                m = leg_moves(P, r, i, 72)
                if m and m["pf"] >= 0.95:
                    d = P.r[r]
                    u = 1 if d[0]["mid"][i] > d[1]["mid"][i] else 0
                    m["dogbid"] = float(d[u]["bid"][i])
                    rows.append(m)
        by = collections.defaultdict(list)
        for m in rows:
            by[m["race"]].append(m["dq"])
        per = sorted(((r, len(v), float(np.mean(v))) for r, v in by.items()), key=lambda x: x[2])
        top3 = {r for r, _, _ in sorted(per, key=lambda x: x[1] * x[2])[:3]}
        ex3 = [m for m in rows if m["race"] not in top3]
        out[P.name] = dict(all=cl_t([m["dq"] for m in rows], [m["race"] for m in rows]),
                           ex_top3=cl_t([m["dq"] for m in ex3], [m["race"] for m in ex3]),
                           races_neg=sum(1 for _, _, v in per if v < 0), races=len(per),
                           floor=sum(1 for m in rows if m["dogbid"] < 0.01), pf_max=float(max(m["pf"] for m in rows)),
                           share_095_097=float(np.mean([m["pf"] < 0.97 for m in rows])), top3=sorted(top3))
    return out


# ------------------------------------------------------------------------------------------------ stage 3 (backtest)
RULES = {  # which leg L gets the extra NO (the other leg gets correspondingly less)
    "fav": lambda t: t["fav"],
    "dog": lambda t: 1 - t["fav"],
    "band": lambda t: t["fav"] if t["pf"] >= 0.95 else 1 - t["fav"],   # training dq signs per band
    "nojump": lambda t: 1 - t["jump"] if t["jump"] >= 0 else None,      # training: the jumped leg keeps rising
}
S_GRID = [0.1, 0.25, 0.5, 1.0]
Q_SETS = 4000            # one race at the live 4k per-race cap (a 2-leg sell-set costs about 1)


def trades(P, thr=4, k=2, maxhold=288, cooldown=6):
    """The high-frequency arb rule (arb_convergence section 7) on the leg panel; one open set per race."""
    out = []
    for r, d in P.r.items():
        sb, sa, dev = d[0]["sb"], d[0]["sa"], d[0]["dev"]
        i, n, nxt = 0, len(P.grid), 0
        while i < n:
            ok = (i >= nxt and np.isfinite(sb[i]) and np.isfinite(dev[i]) and sb[i] - 1 >= 0.005 - 1e-9
                  and (sb[i] - 1) / (2 - sb[i]) >= 0.005 - 1e-9 and dev[i] >= thr * T - 1e-9)
            if not ok:
                i += 1
                continue
            j, how = None, "eop"
            for jj in range(i + 1, n):
                if not np.isfinite(sa[jj]):
                    continue
                if sb[i] - sa[jj] >= k * T - 1e-9 or sa[jj] <= 1 + 1e-9:
                    j, how = jj, "k"
                    break
                if jj - i >= maxhold:
                    j, how = jj, "24h"
                    break
                j = jj
            if j is None:
                break
            m0 = [d[0]["mid"][i], d[1]["mid"][i]]
            exc = [d[0]["exc"][i], d[1]["exc"][i]]
            g = [(d[x]["bid"][i] - d[x]["ask"][j]) / T for x in (0, 1)]       # NO on leg x: buy at 1-bid, sell at 1-ask
            out.append(dict(race=r, day=day_of(P.grid[i]), i=i, j=j, t0=P.grid[i], t1=P.grid[j], how=how,
                            plain=(sb[i] - sa[j]) / T, g=g, fav=int(np.argmax(m0)), pf=float(max(m0)),
                            jump=int(np.argmax(exc)) if np.all(np.isfinite(exc)) else -1,
                            party=[d[0]["party"], d[1]["party"]], pD=[float(m0[x]) for x in (0, 1)]))
            nxt = j + cooldown
            i = j + 1
    return out


def overlay(t, rule):
    L = RULES[rule](t)
    if L is None:
        return 0.0, None
    return t["g"][L] - t["g"][1 - L], L


def bt_eval(TR, rule, s, slip, nboot=1000, rej=0.2):
    """Per-set net P&L (ticks), plain and skewed, with 20% basket rejections and race-cluster bootstrap."""
    plain = np.array([t["plain"] - 4 * slip for t in TR])
    ov = np.array([overlay(t, rule)[0] for t in TR]) if rule else np.zeros(len(TR))
    skew = plain + s * ov
    races = np.array([t["race"] for t in TR])
    ur = sorted(set(races))
    idx = {r: np.where(races == r)[0] for r in ur}
    bm_p, bm_s = [], []
    for _ in range(nboot):
        pick = np.concatenate([idx[r] for r in RNG.choice(ur, len(ur))])
        keep = pick[RNG.random(len(pick)) >= rej]
        bm_p.append(plain[keep].mean())
        bm_s.append(skew[keep].mean())
    ct = cl_t(s * ov, races)
    return dict(n=len(TR), plain=float(plain.mean()), skew=float(skew.mean()), extra=float((s * ov).mean()), t_extra=ct["t"],
                plain_ci=(float(np.percentile(bm_p, 5)), float(np.percentile(bm_p, 95))),
                skew_ci=(float(np.percentile(bm_s, 5)), float(np.percentile(bm_s, 95))),
                sd_plain=float(plain.std()), sd_skew=float(skew.std()), sd_extra=float((s * ov).std()))


def daily(TR, rule, s, slip):
    """Money per day at Q_SETS sets per trade: plain, skewed, and the extra directional part."""
    by = collections.defaultdict(lambda: [0.0, 0.0])
    for t in TR:
        o = overlay(t, rule)[0] if rule else 0.0
        by[t["day"]][0] += (t["plain"] - 4 * slip) * T * Q_SETS * 0.8
        by[t["day"]][1] += s * o * T * Q_SETS * 0.8
    return {k: tuple(v) for k, v in sorted(by.items())}


def peak_D(TR, P, rule, s):
    """Peak |net D notional| of the overlays over the panel (s * Q per open race, signed by which leg gets more NO)."""
    expo = np.zeros(len(P.grid))
    for t in TR:
        _, L = overlay(t, rule)
        if L is None:
            continue
        sign = -1.0 if t["party"][L] == "D" else (1.0 if t["party"][1 - L] == "D" else 0.0)   # more NO on D = short D
        expo[t["i"]:t["j"] + 1] += sign * s * Q_SETS * 0.8
    return float(np.max(np.abs(expo))), expo


def d_corr(P, h=72):
    """Mean pairwise correlation across races of the D leg's mid change over non-overlapping h-step blocks."""
    X = []
    for r, d in P.r.items():
        k = [x for x in (0, 1) if d[x]["party"] == "D"]
        if not k:
            continue
        m = d[k[0]]["mid"]
        blocks = m[::h]
        X.append(np.diff(blocks))
    X = np.array(X)
    C = []
    for a in range(len(X)):
        for b in range(a + 1, len(X)):
            mm = np.isfinite(X[a]) & np.isfinite(X[b])
            if mm.sum() >= 5 and X[a][mm].std() > 0 and X[b][mm].std() > 0:
                C.append(np.corrcoef(X[a][mm], X[b][mm])[0, 1])
    return float(np.mean(C)), float(np.median(C)), len(C)


def pair_corr(TR, rule):
    """Correlation of the extra directional P&L between trades open at the same time in different races."""
    xs, ys = [], []
    for a in range(len(TR)):
        for b in range(a + 1, len(TR)):
            ta, tb = TR[a], TR[b]
            if ta["race"] != tb["race"] and ta["t0"] < tb["t1"] and tb["t0"] < ta["t1"]:
                oa, ob = overlay(ta, rule)[0], overlay(tb, rule)[0]
                xs += [oa, ob]
                ys += [ob, oa]
    return float(np.corrcoef(xs, ys)[0, 1]) if len(xs) > 10 else np.nan, len(xs) // 2


def stage3(A, B):
    TRA, TRB = trades(A), trades(B)
    grid = {}
    for rule in RULES:
        for s in S_GRID:
            grid[(rule, s)] = {P: {sl: bt_eval(TR, rule, s, sl, nboot=300) for sl in (0.1, 0.25)}
                               for P, TR in (("A", TRA), ("B", TRB))}
    # chosen on training only, by profit per unit of risk (the mean alone is linear in s and always picks the largest s)
    best = max(grid, key=lambda k: grid[k]["A"][0.1]["skew"] / grid[k]["A"][0.1]["sd_skew"])
    rule, s = best
    by_band = {}
    for P, TR in (("A", TRA), ("B", TRB)):
        for b in range(4):
            sub = [t for t in TR if band_of(t["pf"]) == b]
            if len(sub) >= 3:
                by_band[(P, b)] = {sl: bt_eval(sub, rule, s, sl, nboot=300) for sl in (0.1, 0.25)}
    days = {P: daily(TR, rule, s, 0.1) for P, TR in (("A", TRA), ("B", TRB))}
    peaks = {P: peak_D(TR, PP, rule, s)[0] for P, TR, PP in (("A", TRA, A), ("B", TRB, B))}
    corr = {P.name: d_corr(P) for P in (A, B)}
    pcor = {P: pair_corr(TR, rule) for P, TR in (("A", TRA), ("B", TRB))}
    hows = {P: collections.Counter(t["how"] for t in TR) for P, TR in (("A", TRA), ("B", TRB))}
    return dict(TRA=TRA, TRB=TRB, grid=grid, best=best, by_band=by_band, days=days, peaks=peaks, corr=corr,
                pcor=pcor, hows=hows)


def md_stage23(m2, r3):
    L = ["\n## Stage 2: favourite > 0.95 cell (6 h dq, every race every hour)\n",
         "| panel | n | races | races with dq < 0 | mean dq (t race) | without the 3 biggest races | dog bid < 0.01 | max fav price | share at 0.95-0.97 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for p, v in m2.items():
        L.append(f"| {p} | {v['all']['n']} | {v['races']} | {v['races_neg']} | {v['all']['mean']:+.2f} ({v['all']['t']:+.1f}) | "
                 f"{v['ex_top3']['mean']:+.2f} ({v['ex_top3']['t']:+.1f}); top 3: {', '.join(v['top3'])} | {v['floor']} | {v['pf_max']:.3f} | {v['share_095_097']:.0%} |")
    L += ["\n## Stage 3: plain vs skewed high-frequency arb, same entries (ticks per set, net of spread both ways and "
          "4 x slippage; 20% rejections; 90% race-bootstrap CI)\n",
          f"Trades: A {len(r3['TRA'])} ({dict(r3['hows']['A'])}), B {len(r3['TRB'])} ({dict(r3['hows']['B'])}).\n",
          "| rule | s | A plain | A skewed | A extra (t) | B plain | B skewed | B extra (t) | B skewed @0.25 slip | sd per set A plain / skewed |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for (rule, s), v in r3["grid"].items():
        a, b, b2 = v["A"][0.1], v["B"][0.1], v["B"][0.25]
        L.append(f"| {rule} | {s} | {a['plain']:+.2f} | {a['skew']:+.2f} | {a['extra']:+.2f} ({a['t_extra']:+.1f}) | {b['plain']:+.2f} | "
                 f"{b['skew']:+.2f} | {b['extra']:+.2f} ({b['t_extra']:+.1f}) | {b2['skew']:+.2f} | {a['sd_plain']:.2f} / {a['sd_skew']:.2f} |")
    rule, s = r3["best"]
    L += [f"\nChosen on training: rule **{rule}**, s = **{s}**.\n",
          "| panel | band | n | plain [CI] | skewed [CI] | extra sd per set |", "|---|---|---|---|---|---|"]
    for (p, b), v in r3["by_band"].items():
        x = v[0.1]
        L.append(f"| {p} | {BAND_LAB[b]} | {x['n']} | {x['plain']:+.2f} [{x['plain_ci'][0]:+.2f}, {x['plain_ci'][1]:+.2f}] | "
                 f"{x['skew']:+.2f} [{x['skew_ci'][0]:+.2f}, {x['skew_ci'][1]:+.2f}] | {x['sd_extra']:.2f} |")
    L += [f"\nMoney per day at {Q_SETS} sets per trade x 0.8 fill (plain, extra from skew):\n"]
    for p, dd in r3["days"].items():
        L.append(f"* {p}: " + "; ".join(f"{k} {v[0]:+.0f} / {v[1]:+.0f}" for k, v in dd.items()))
    L += [f"\nPeak |net D notional| of the overlays: A {r3['peaks']['A']:,.0f}, B {r3['peaks']['B']:,.0f} (cap 5,000, shared).",
          f"Cross-race correlation of 6-h D-leg changes (mean, median, pairs): {r3['corr']}.",
          f"Correlation of the extra P&L between concurrent trades in different races (corr, pairs): {r3['pcor']}."]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ figure
TRAIN = "Training days (14-21 Oct, estimated from trade prices)"
TEST = "Test days (22-23 Oct, real quotes)"


def figure(d):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    res, r3 = d["res"], d["r3"]
    rule, s = r3["best"]
    x = np.arange(4)
    fig = plt.figure(figsize=(10, 17.6), constrained_layout=True)
    gs = fig.add_gridspec(6, 1, height_ratios=[0.5, 3.3, 3.3, 2.8, 0.1, 2.1])
    acs._header(fig.add_subplot(gs[0]), "Skewing the high-frequency arb toward one leg",
                "When a race's gap to payout shrinks back, does one leg do most of the moving, and does leaning on it pay?")
    ax = fig.add_subplot(gs[1])
    for P, ls, mk in (("A", ":", "^"), ("B", "-", "o")):
        v = res[(P, "dev>=+2", "6 h")]["band"]
        fs = [v[b]["fav_share"] if v[b]["n"] >= 5 else np.nan for b in range(4)]
        ax.plot(x, np.array(fs) * 100, ls=ls, marker=mk, color="C0", lw=2,
                label=f"favourite's share, {'training (dotted)' if P == 'A' else 'test (solid)'}")
        ax.plot(x, (1 - np.array(fs)) * 100, ls=ls, marker=mk, color="C1", lw=2,
                label=f"underdog's share, {'training (dotted)' if P == 'A' else 'test (solid)'}")
        if P == "B":
            for b in range(4):
                if v[b]["n"] >= 5:
                    ax.annotate(f"n {v[b]['n']}", (x[b], fs[b] * 100), textcoords="offset points", xytext=(8, 4), fontsize=9, color="0.3")
    ax.axhline(50, color="0.4", lw=1, ls="--")
    ax.text(3.45, 52, "equal split", fontsize=9, color="0.3", ha="right")
    ax.set_ylabel("Share of the 6-h fall in the gap\ndone by each leg (%)", fontsize=10)
    ax.set_title("A. Which leg does the falling after the gap widens by 2+ ticks?", loc="left", fontweight="bold", fontsize=11)
    ax.set_xticks(x, [f"favourite {b}" for b in BAND_LAB])
    ax.legend(fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=2, frameon=False)
    ax.grid(alpha=0.3)
    ax = fig.add_subplot(gs[2])
    for P, ls, mk in (("A", ":", "^"), ("B", "-", "o")):
        pl = [r3["by_band"].get((P, b), {}).get(0.1) for b in range(4)]
        yp = [p["plain"] if p else np.nan for p in pl]
        ys = [p["skew"] if p else np.nan for p in pl]
        ax.plot(x - 0.04, yp, ls=ls, marker=mk, color="0.35", lw=2, label=f"plain set, {'training (dotted)' if P == 'A' else 'test (solid)'}")
        ax.plot(x + 0.04, ys, ls=ls, marker=mk, color="C3", lw=2, label=f"skewed set, {'training (dotted)' if P == 'A' else 'test (solid)'}")
        if P == "B":
            for b in range(4):
                p = pl[b]
                if p and p["n"] >= 5 and np.all(np.isfinite(p["skew_ci"])):
                    ax.errorbar(x[b] + 0.04, ys[b], yerr=[[ys[b] - p["skew_ci"][0]], [p["skew_ci"][1] - ys[b]]], color="C3", capsize=4, lw=1)
                    ax.errorbar(x[b] - 0.04, yp[b], yerr=[[yp[b] - p["plain_ci"][0]], [p["plain_ci"][1] - yp[b]]], color="0.35", capsize=4, lw=1)
                if p:
                    ax.annotate(f"test n {p['n']}", (x[b], max(yp[b], ys[b])), textcoords="offset points", xytext=(10, 6), fontsize=9, color="0.3")
    ax.axhline(0, color="0.4", lw=1)
    ax.set_ylabel("Net profit per set (ticks)", fontsize=10)
    lean = {"fav": "the favourite", "dog": "the underdog", "band": "the underdog (favourite if > 0.95)",
            "nojump": "the leg that did not jump"}[rule]
    ax.set_title(f"B. Profit per set, plain vs skewed ({s:g} extra NO on {lean})",
                 loc="left", fontweight="bold", fontsize=11)
    ax.set_xticks(x, [f"favourite {b}" for b in BAND_LAB])
    ax.plot([], [], color="0.3", marker="|", ls="", ms=10, label="bars: 90% range, test (5+ trades)")
    ax.legend(fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=2, frameon=False)
    ax.grid(alpha=0.3)
    ax = fig.add_subplot(gs[3])
    for P, ls, mk in (("A", ":", "^"), ("B", "-", "o")):
        pl = [r3["by_band"].get((P, b), {}).get(0.1) for b in range(4)]
        ax.plot(x, [p["sd_extra"] if p else np.nan for p in pl], ls=ls, marker=mk, color="C4", lw=2,
                label=f"extra profit from the skew, {'training (dotted)' if P == 'A' else 'test (solid)'}")
        ax.plot(x, [p["sd_plain"] if p else np.nan for p in pl], ls=ls, marker=mk, color="0.5", lw=1.5,
                label=f"plain set, {'training (dotted)' if P == 'A' else 'test (solid)'}")
    ax.set_ylabel("Standard deviation\nper set (ticks)", fontsize=10)
    ax.set_title(f"C. Extra risk the skew adds (each 4,000-set race adds {s * Q_SETS * 0.8:,.0f} of one-party exposure)",
                 loc="left", fontweight="bold", fontsize=11)
    ax.set_xticks(x, [f"favourite {b}" for b in BAND_LAB])
    ax.set_xlabel("Favourite's price when the set is bought", fontsize=10)
    ax.legend(fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2, frameon=False)
    ax.grid(alpha=0.3)
    fig.add_subplot(gs[4]).axis("off")
    acs._glossary(fig.add_subplot(gs[5]), [
        acs.GLOSS_TICK,
        ("gap to payout", "sum of the two YES mid prices minus 1; the high-frequency arb buys NO on both legs when it is 4+ ticks above the race's 24-h median"),
        ("share of the fall", "of the drop in the gap over 6 h, how much came from the favourite's price and how much from the underdog's"),
        ("plain set", "1 NO on each leg; sold back at +2 ticks per set or after 24 h"),
        ("skewed set", f"1+s NO on one leg and 1-s on the other (s = {s:g}); same number of contracts, so the same spread cost, plus a bet on one party"),
        ("net profit", "after paying the spread both ways, 0.1 tick slippage per contract and dropping 20% of entries (rejected baskets)"),
        ("training / test days", "training: 14-21 Oct, prices estimated from 5-min trades; the skew was picked here only. Test: 22-23 Oct, real quotes"),
        ("90% range", "resampling races and rejections; it is wide because the test panel has few trades"),
    ], ncol=2, width=56, fontsize=9)
    fig.savefig(os.path.join(FIG, "skewed_arb_decision.png"), dpi=130, facecolor="white")


def main():
    A, B = build_panels()
    print("races A", len(A.r), "B", len(B.r), "B grid", len(B.grid), acs.fmt_ts(B.grid[0]), acs.fmt_ts(B.grid[-1]))
    res, unc = stage1(A, B)
    m2 = stage2(A, B)
    r3 = stage3(A, B)
    md = md_stage1(res, unc) + md_stage23(m2, r3)
    open(os.path.join(OUT, "tables.md"), "w").write(md)
    print(md_stage23(m2, r3))
    d = dict(res={k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in res.items()}, unc=unc, m2=m2, r3=r3)
    pickle.dump(d, open(os.path.join(OUT, "results.pkl"), "wb"))
    figure(d)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--figures":
        figure(pickle.load(open(os.path.join(OUT, "results.pkl"), "rb")))
    else:
        main()
