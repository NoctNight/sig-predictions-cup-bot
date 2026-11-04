"""Drill-down 25 (round 3, ideas 3, 7, 8, 11, 12).
 3 basket: many legs show a 1c+ fresh gap the same way with both venues agreeing. Buy them all, small each.
 7 outside quality at trigger: venue spread and depth.  8 which venue led.
 11 long-short pairs: one D-side and one R-side signal within 6 h, net D flat.
 12 exit when the gap has closed, not on a clock."""
import sys, sqlite3, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); P = L.P
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
Es = pd.read_pickle("data/research_diralpha/r3_slow.pkl"); Ef = pd.read_pickle("data/research_diralpha/r3_fast.pkl")
mid, fair, pm, k = L.mid, L.fair, L.pm, L.k
gap = L.gap; S48 = gap - gap.rolling(576, min_periods=288).median()
Sv, Bv, Av, Mv, Gv = S48.values, L.B, L.A, mid.values, gap.values
n, m = Mv.shape; dsign = L.dsign.values; nochm = L.office != "chamber"
dP, dK = (pm - pm.shift(288)).values, (k - k.shift(288)).values
print("=== 3 basket")
for th, vm, N, h in ((.01, .005, 8, 288), (.01, .005, 15, 288), (.015, .005, 5, 288), (.015, .0075, 8, 288), (.01, .005, 8, 96), (.01, .005, 15, 96), (.01, .005, 25, 288)):
    D = np.nan_to_num(Sv) * dsign
    ok = (np.abs(Sv) >= th) & (Mv >= .15) & (Mv <= .85) & (dP * np.sign(Sv) >= vm) & (dK * np.sign(Sv) >= vm)
    ok[:, ~nochm] = False
    up = ok & (D > 0); dn = ok & (D < 0)
    res = []; last = {1: -10**9, -1: -10**9}
    for t in range(576, n - h - 1):
        for way, M_ in ((1, up), (-1, dn)):
            legs = np.flatnonzero(M_[t])
            if t - last[way] < 288: continue
            # one leg per race, biggest signal
            best = {}
            for j in legs:
                r = L.race[j]
                if r not in best or abs(Sv[t, j]) > abs(Sv[t, best[r]]): best[r] = j
            if len(best) < N: continue
            nets = []
            for j in best.values():
                sg = np.sign(Sv[t, j]); e = t + 1; x = e + h
                b0, a0, b1, a1 = Bv[e, j], Av[e, j], Bv[x, j], Av[x, j]
                if not np.isfinite([b0, a0, b1, a1]).all(): continue
                nets.append(b1 - a0 if sg > 0 else b0 - a1)
            if len(nets) < N // 2: continue
            last[way] = t
            res.append((L.day[t], way, len(nets), 100 * np.mean(nets), np.mean(np.array(nets) > 0)))
    R = pd.DataFrame(res, columns=["day", "way", "legs", "net", "win"])
    print(f"gap>={th*100:.1f}c venues>={vm*100:.2f}c N>={N} hold {h//12}h: baskets {len(R)}, legs {R.legs.sum() if len(R) else 0}, leg-weighted net {np.average(R.net, weights=R.legs) if len(R) else 0:.2f}c, D {R[R.way>0].net.mean() if len(R) else 0:.2f} R {R[R.way<0].net.mean() if len(R) else 0:.2f}")
    if len(R): print(R.round(2).to_string(index=False))
print("=== 11 long-short")
for nm, E in (("slow", Es), ("fast", Ef), ("both", pd.concat([Es.assign(k_="s"), Ef.assign(k_="f")], ignore_index=True))):
    E = E.sort_values("t").reset_index(drop=True); used = set(); pairs = []
    for a in E.itertuples():
        if a.Index in used: continue
        for b in E.itertuples():
            if b.Index <= a.Index or b.Index in used or b.ds == a.ds or abs(b.t - a.t) > 72 or L.race[b.j] == L.race[a.j]: continue
            used |= {a.Index, b.Index}; pairs.append((a.net + b.net) / 2); break
    p = np.array(pairs)
    single = E.net.values
    print(f"{nm}: pairs {len(p)} of {len(E)} trades, net per trade {100*p.mean():.2f}c sd {100*p.std():.2f}c (singles {100*single.mean():.2f}c sd {100*single.std():.2f}c) unpaired left {len(E)-2*len(p)} D-side share {(E.ds>0).mean():.2f}")
print("=== 12 exit on gap close (strict slow, exit at touch; limit 36 h, fast limit 8 h)")
for nm, E, lim in (("slow", Es, 432), ("fast", Ef, 96)):
    for tgt in (None, .015, .01, .005, 0.0, -.005):
        v = []; hrs = []
        for r in E.itertuples():
            j, e, s = r.j, r.e, r.s; px = Av[e, j] if s > 0 else 1 - Bv[e, j]; xv = None
            for i in range(e + 1, min(e + lim, n)):
                if not (np.isfinite(Bv[i, j]) and np.isfinite(Av[i, j])): continue
                val = (Bv[i, j] if s > 0 else 1 - Av[i, j]) - px
                if tgt is not None and np.isfinite(Gv[i, j]) and Gv[i, j] * s <= tgt:
                    xv = val; hrs.append((i - e) / 12); break
                last_val = val; li = i
            if xv is None:
                xv = last_val; hrs.append((li - e) / 12)
            v.append(xv)
        print(f"{nm} exit when gap <= {tgt}: net {100*np.mean(v):.2f}c win {np.mean(np.array(v)>0):.2f} hours {np.mean(hrs):.1f}")
print("=== 7, 8 outside quality and venue lead")
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
meta = L.meta.reset_index()
for nm, E in (("slow", Es), ("fast", Ef)):
    rows = []
    for r in E.itertuples():
        T = L.idx[r.t]; race, party = L.race[r.j], L.party[r.j]
        q = pd.read_sql_query("select venue,bid,ask,bid_sz,ask_sz,depth_usd from venue_quotes where race=? and party=? and venue in ('polymarket','kalshi') and ts between ? and ? ", db, params=(race, party, T - 600, T + 300))
        if q.empty: rows.append((np.nan, np.nan, np.nan, np.nan)); continue
        g = q.groupby("venue").median(numeric_only=True)
        sp = (g.ask - g.bid) * 100
        rows.append((sp.get("polymarket", np.nan), sp.get("kalshi", np.nan), g.depth_usd.get("polymarket", np.nan), g.depth_usd.get("kalshi", np.nan)))
    Q = pd.DataFrame(rows, columns=["pm_sp", "k_sp", "pm_dep", "k_dep"]); E2 = E.reset_index(drop=True).join(Q)
    E2["wide"] = E2[["pm_sp", "k_sp"]].max(axis=1)
    E2["dep"] = E2[["pm_dep", "k_dep"]].min(axis=1)
    print(nm, "covered", E2.wide.notna().sum())
    print(E2.groupby(pd.qcut(E2.wide, 3, duplicates="drop"), observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
    print(E2.groupby(pd.qcut(E2.dep, 3, duplicates="drop"), observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
    # 8 lead: over the window before the trigger, which venue reached half its move first
    W = 288 if nm == "slow" else 6; pv, kv = pm.values, k.values; lead = []
    for r in E2.itertuples():
        t, j = r.t, r.j; s = np.sign(L.fair.values[t, j] - L.fair.values[max(t - W, 0), j]) or 1
        def first(x):
            a = x[max(t - W, 0)]; tot = (x[t, j] - a[j]) * s
            for i in range(max(t - W, 0), t + 1):
                if (x[i, j] - a[j]) * s >= .5 * tot: return i
            return t
        lead.append(first(kv) - first(pv))
    E2["pm_lead_bins"] = lead
    print(E2.groupby(pd.cut(E2.pm_lead_bins, [-999, -1, 0, 999], labels=["Kalshi first", "same bin", "Polymarket first"]), observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
