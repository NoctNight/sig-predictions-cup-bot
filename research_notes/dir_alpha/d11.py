"""Drill-down 11: the fast trigger surface. Both venues moved >= m in window W, SIG has copied < c of it. Grid over W, m, c, hold, office, then
stability by neighbours (a cell is only as good as the cells around it), split by market type, direction, price, clock."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
pd.set_option("display.width", 220, "display.max_rows", 300, "display.max_columns", 40)
nocham = L.office != "chamber"
band = ((mid >= .1) & (mid <= .9)).values
HOLD = {"2h": 24, "4h": 48, "8h": 96, "12h": 144, "24h": 288}
rows = []
store = {}
for wn, W in (("30m", 6), ("1h", 12), ("2h", 24), ("4h", 48), ("8h", 96)):
    dp, dk, dm, df = L.lag(L.pm, W), L.lag(L.k, W), L.lag(mid, W), L.lag(fair, W)
    s = np.sign(df)
    for m_ in (.01, .015, .02, .03):
        for c in (.3, .5, .75):
            ok = (dp * s >= m_) & (dk * s >= m_) & ((dm * s) < c * (df * s))
            M = (ok.values & band).copy()
            M[:, ~nocham] = False
            for hn, h in HOLD.items():
                for off in (None, "house", "governor", "senate"):
                    Mx = M.copy()
                    if off: Mx[:, L.office != off] = False
                    tr = L.trades(df, h, thr=1e-9, mask=Mx, cool=max(h, 48))
                    if tr is None or len(tr) < 12: continue
                    i, j = tr[:, 1].astype(int), tr[:, 0].astype(int)
                    ts = L.idx[i]
                    d = pd.Series(tr[:, 4]).groupby(L.day[i]).mean()
                    a, b = tr[ts < L.SPLIT, 4], tr[ts >= L.SPLIT, 4]
                    rows.append(dict(W=wn, m=m_, c=c, h=hn, office=off or "all", n=len(tr), races=len(set(L.race[j])), net=100 * tr[:, 4].mean(), gross=100 * tr[:, 3].mean(), win=(tr[:, 4] > 0).mean(), train=100 * a.mean() if len(a) > 4 else np.nan, test=100 * b.mean() if len(b) > 4 else np.nan, nt=len(b), days=f"{int((d>0).sum())}/{len(d)}", t_day=L._t(tr[:, 4], L.day[i])[0], t_race=L._t(tr[:, 4], L.race[j])[0], perh=100 * tr[:, 4].mean() / (h / 12), worst=100 * tr[:, 4].min()))
    print(wn, len(rows), flush=True)
A = pd.DataFrame(rows); A.to_pickle("data/research_diralpha/d11.pkl")
print("\nall offices, copied < 50%, both venues >= m: net cents (rows window, m; columns hold)")
x = A[(A.office == "all") & (A.c == .5)]
print(x.pivot_table(index=["W", "m"], columns="h", values="net")[["2h", "4h", "8h", "12h", "24h"]].round(2).to_string())
print(x.pivot_table(index=["W", "m"], columns="h", values="n")[["2h", "4h", "24h"]].to_string())
print("\ntest days only:"); print(x.pivot_table(index=["W", "m"], columns="h", values="test")[["2h", "4h", "8h", "12h", "24h"]].round(2).to_string())
print("\nnet per hour held:"); print(x.pivot_table(index=["W", "m"], columns="h", values="perh")[["2h", "4h", "8h", "12h", "24h"]].round(3).to_string())
print("\nby office, W 1h and 4h, m 1.5c, copied < 50%:")
print(A[(A.c == .5) & (A.m == .015) & A.W.isin(["1h", "4h"])].pivot_table(index=["W", "office"], columns="h", values="net")[["2h", "4h", "8h", "12h", "24h"]].round(2).to_string())
G = A[(A.train > 0) & (A.test > 0) & (A.n >= 30)].copy()
G["score"] = G.net * np.sqrt(G.n)
print("\nBest cells, train and test positive, n >= 30:"); print(G.sort_values("score", ascending=False).head(20).round(2).to_string(index=False))
print("\nBest per hour:"); print(G[G.h.isin(["2h", "4h"])].sort_values("perh", ascending=False).head(14).round(3).to_string(index=False))
# one chosen fast rule, split every way
W = 12; m_ = .015; c = .5; h = 48
dp, dk, dm, df = L.lag(L.pm, W), L.lag(L.k, W), L.lag(mid, W), L.lag(fair, W)
s = np.sign(df)
ok = (dp * s >= m_) & (dk * s >= m_) & ((dm * s) < c * (df * s))
M = (ok.values & band).copy(); M[:, ~nocham] = False
tr = L.trades(df, h, thr=1e-9, mask=M, cool=48)
E = pd.DataFrame(tr, columns=["j", "i", "s", "gross", "net"]); E[["j", "i"]] = E[["j", "i"]].astype(int)
E["office"] = L.office[E.j]; E["party"] = L.party[E.j]; E["dside"] = E.s * np.where(E.party == "D", 1, -1); E["hour"] = L.hour[E.i]; E["mid"] = mid.values[E.i, E.j]
E["move"] = np.abs(df.values[E.i, E.j]); E["copied"] = (dm.values[E.i, E.j] * E.s) / E.move
E["vol24"] = L.rsum(P["vol"], 288).values[E.i, E.j]; E["flow24"] = E.s * L.rsum(P["sflow"], 288).values[E.i, E.j]
E["gapfresh"] = E.s * (gap - gap.rolling(576, min_periods=288).median()).values[E.i, E.j]
E["toward50"] = (E.s * (.5 - E.mid)) > 0
print("\nFast rule W 1h, m 1.5c, copied < 50%, hold 4 h: n", len(E), "net", round(100 * E.net.mean(), 2), "win", round((E.net > 0).mean(), 2))
for col, b in (("office", None), ("dside", None), ("toward50", None), ("hour", [-1, 5, 11, 17, 23]), ("mid", [.1, .25, .4, .6, .75, .9]), ("move", [.01, .02, .03, .05, .5]), ("copied", [-5, 0, .25, .5]), ("vol24", [0, 20000, 60000, 150000, 1e9]), ("flow24", [-1e9, -10000, 0, 10000, 1e9]), ("gapfresh", [-1, 0, .01, .02, .03, 1])):
    k = E[col] if b is None else pd.cut(E[col], b)
    print(E.groupby(k, observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
