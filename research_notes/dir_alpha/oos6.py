"""Level gap trade: buy the side where outside is 6c+ better than SIG, hold 48-72 h, sell. Both periods, one event per leg per 72 h, chamber out.
 Control: same-price legs with a gap under 2c at the same moment (is it just favourites drifting up?). Splits: gap size, own-side price, D or R side, week, office. Next-trade entry and exit in A, true book in B."""
import pickle, sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
pd.set_option("display.width", 200, "display.max_rows", 100)
L = Lab()
A = pickle.load(open("data/research_diralpha/oos/panel.pkl", "rb"))
def trades(S, R, P_, K_, race, office, party, book=None, H=(576, 864)):
    Mv = S.values; Rv = R.values; n, m = Mv.shape; fair = (P_ + K_) / 2; Gv = (fair - S).values
    dsign = np.where(party == "D", 1.0, np.where(party == "R", -1.0, 0.0))
    ra = R.rolling(12, min_periods=1).max().fillna(0).values > 0
    nx = np.full(Mv.shape, -1, int)
    for j in range(m):
        k = -1
        for i in range(n - 1, -1, -1):
            nx[i, j] = k
            if Rv[i, j]: k = i
    out = []
    M = (np.abs(Gv) >= .04) & (Mv >= .1) & (Mv <= .9); M[:, office == "chamber"] = False
    for j in range(m):
        last = -10**9
        for t in np.flatnonzero(M[:, j]):
            if t - last < 864 or not ra[t, j]: continue
            e = nx[t, j]
            if e < 0 or e - t > 24: continue
            s = 1.0 if Gv[t, j] > 0 else -1.0; own = Mv[t, j] if s > 0 else 1 - Mv[t, j]; r = dict(j=j, t=t, g=abs(Gv[t, j]), own=own, ds=s * dsign[j], office=office[j], week=S.index[t].isocalendar()[1], day=S.index[t].strftime("%m-%d"), race=race[j])
            # control: other legs, our side within 5c of this price, gap under 2c, same side of the favourite line
            sgn = np.where(Mv[e] >= .5, 1.0, -1.0) * (1.0 if own >= .5 else -1.0)
            oth = 0.5 + sgn * (Mv[e] - .5); ctl = (np.abs(oth - own) <= .05) & (np.abs(Gv[e]) < .02) & (race != race[j]) & (office != "chamber")
            for h in H:
                x = -1
                for i in range(e + h, min(e + h + 72, n)):
                    if Rv[i, j]: x = i; break
                nm = str(h // 12)
                if x < 0: r["n" + nm] = np.nan; r["c" + nm] = np.nan; continue
                if book is None: r["n" + nm] = s * (Mv[x, j] - Mv[e, j]) - .0075
                else:
                    b, a = book; r["n" + nm] = (b[x, j] - a[e, j]) if s > 0 else (b[e, j] - a[x, j])
                fav = np.where(oth >= .5, 1.0, -1.0)
                r["c" + nm] = np.nanmean((sgn * (Mv[x] - Mv[e]))[ctl]) - .0075 if ctl.sum() >= 3 else np.nan
            out.append(r); last = t
    return pd.DataFrame(out)
idx = pd.to_datetime(L.idx, unit="s", utc=True)
tp = pd.read_pickle("data/research_diralpha/tape.pkl"); tp = tp[tp.ts >= L.idx[0]].sort_values("ts"); tp["b"] = (tp.ts // 300).astype("int64") * 300; tp["ex"] = tp.exchange_id.astype(str)
last = tp.groupby(["b", "ex"]).price.last().unstack().reindex(index=L.idx, columns=L.cols)
EB = trades(last.ffill(limit=288).set_axis(idx), last.notna().set_axis(idx), L.pm.set_axis(idx), L.k.set_axis(idx), L.race, L.office, L.party, book=(L.bid.ffill(limit=12).values, L.ask.ffill(limit=12).values))
EA = trades(A["S"], A["R"], A["P"], A["K"], A["race"], A["office"], A["party"])
EA.to_pickle("data/research_diralpha/oos/level_a.pkl"); EB.to_pickle("data/research_diralpha/oos/level_b.pkl")
for lab, E in (("A 3-20 Oct", EA), ("B 21-30 Oct, true book", EB)):
    print("\n" + lab, len(E))
    t = lambda q: pd.Series(dict(n=len(q), races=q.race.nunique(), n48=100 * q.n48.mean(), ctl48=100 * q.c48.mean(), n72=100 * q.n72.mean(), ctl72=100 * q.c72.mean(), win72=(q.n72 > 0).sum() / max(q.n72.notna().sum(), 1), se72=100 * q.n72.std() / np.sqrt(max(q.n72.notna().sum(), 1))))
    print(pd.DataFrame({f"gap {a}-{b}c": t(E[(E.g >= a / 100) & (E.g < b / 100)]) for a, b in ((4, 6), (6, 8), (8, 12), (12, 50))}).T.round(2).to_string())
    Q = E[E.g >= .06]
    print(pd.DataFrame({f"own {a}-{b}c": t(Q[(Q.own >= a / 100) & (Q.own < b / 100)]) for a, b in ((10, 50), (50, 70), (70, 80), (80, 91))}).T.round(2).to_string())
    print(pd.DataFrame({k: t(g) for k, g in Q.groupby("ds")}).T.round(2).to_string())
    print(pd.DataFrame({k: t(g) for k, g in Q.groupby("office")}).T.round(2).to_string())
    print(pd.DataFrame({k: t(g) for k, g in Q.groupby("week")}).T.round(2).to_string())
