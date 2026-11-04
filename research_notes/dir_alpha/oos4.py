"""Do gaps opened by SIG moving alone close? d34's test on 3-20 Oct (last trades, 0.75c assumed spread), horizons to 87 h. One event per leg per 87 h.
Also the level version: any leg with a gap of 6c+, whatever made it. Chamber legs left out, so this is independent of the chamber episodes."""
import pickle, numpy as np, pandas as pd
pd.set_option("display.width", 200, "display.max_rows", 100)
O = "data/research_diralpha/oos/"
P = pickle.load(open(O + "panel.pkl", "rb")); S = P["S"]; Mv = S.values; n, m = Mv.shape; office = P["office"]; race = P["race"]
fair = (P["P"] + P["K"]) / 2; gap = fair - S; Gv = gap.values
dG = (gap - gap.shift(576)).values; dF = (fair - fair.shift(576)).values
Ra = P["R"].rolling(12, min_periods=1).max().fillna(0).values > 0
def ev(M, cool=1044):
    out = []
    for j in range(m):
        if office[j] == "chamber": continue
        last = -10**9
        for t in np.flatnonzero(M[:, j]):
            if t - last < cool or not Ra[t, j]: continue
            s = 1.0 if Gv[t, j] > 0 else -1.0; g0 = abs(Gv[t, j]); r = dict(j=j, t=t, g0=g0, own=Mv[t, j] if s > 0 else 1 - Mv[t, j], race=race[j])
            for h, nm in ((288, "24"), (576, "48"), (864, "72"), (1044, "87")):
                x = t + h
                if x >= n: r["m" + nm] = np.nan; r["c" + nm] = np.nan; continue
                r["m" + nm] = s * (Mv[x, j] - Mv[t, j]); r["c" + nm] = 1 - (Gv[x, j] * s) / g0
            out.append(r); last = t
    return pd.DataFrame(out)
def rep(lab, E):
    if E.empty: print(lab, "none"); return
    print(f"{lab}: {len(E)} events, {E.race.nunique()} races, gap {100*E.g0.mean():.1f}c | net 24 h {100*E.m24.mean()-.75:.2f}c, 48 h {100*E.m48.mean()-.75:.2f}c ({E.m48.notna().sum()}), 72 h {100*E.m72.mean()-.75:.2f}c ({E.m72.notna().sum()}), 87 h {100*E.m87.mean()-.75:.2f}c ({E.m87.notna().sum()}) | median closed 48 h {E.c48.median():.2f}, 87 h {E.c87.median():.2f} | won 87 h {(E.m87>.0075).sum()/max(E.m87.notna().sum(),1):.2f}")
for W in (.03, .04, .05, .07):
    M = (np.abs(dG) >= W) & (np.sign(dG) == np.sign(Gv)) & (np.abs(dF) < .015) & (np.abs(Gv) >= W * .75) & (Mv >= .1) & (Mv <= .9)
    E = ev(M); rep(f"SIG moved {W*100:.0f}c+ alone", E)
    if W == .04:
        for lab, q in (("  our side 60-90c", E[(E.own >= .6) & (E.own <= .9)]), ("  our side under 60c", E[E.own < .6])): rep(lab, q)
for g in (.04, .06, .08):
    M = (np.abs(Gv) >= g) & (Mv >= .1) & (Mv <= .9); rep(f"any gap {g*100:.0f}c+", ev(M))
