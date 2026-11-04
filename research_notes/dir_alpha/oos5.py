"""Is the unseen-day result an artefact of last-trade prices? Same code on two panels:
 A 3-20 Oct: SIG candle closes.  B 21-30 Oct: SIG last trades rebuilt from the tape at 5 minutes, outside from the main panel.
Entry either at the signal bar's own last trade (biased if that print is off-market) or at the next real trade after it, which removes that bias. Assumed spread 0.75c.
Panel B can also be priced from the true book (bid/ask), so the last-trade method can be calibrated against what was really tradeable."""
import pickle, sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
pd.set_option("display.width", 200, "display.max_rows", 100)
O = "data/research_diralpha/oos/"
A = pickle.load(open(O + "panel.pkl", "rb"))
L = Lab()
tp = pd.read_pickle("data/research_diralpha/tape.pkl"); tp = tp[(tp.ts >= L.idx[0])].sort_values("ts")
tp["b"] = (tp.ts // 300).astype("int64") * 300; tp["ex"] = tp.exchange_id.astype(str)
last = tp.groupby(["b", "ex"]).price.last().unstack().reindex(index=L.idx, columns=L.cols)
SB = last.ffill(limit=288); RB = last.notna()
idx = pd.to_datetime(L.idx, unit="s", utc=True)
B = dict(S=SB.set_axis(idx), R=RB.set_axis(idx), P=L.pm.set_axis(idx), K=L.k.set_axis(idx), race=L.race, office=L.office, party=L.party)
def analyse(lab, D, book=None):
    S, R = D["S"], D["R"]; Mv = S.values; Rv = R.values; n, m = Mv.shape; race, office = D["race"], D["office"]
    fair = (D["P"] + D["K"]) / 2; gap = fair - S; Gv = gap.values
    S48 = (gap - gap.rolling(576, min_periods=288).median()).values
    dp = (D["P"] - D["P"].shift(288)).values; dk = (D["K"] - D["K"].shift(288)).values
    ra = R.rolling(12, min_periods=1).max().fillna(0).values > 0
    nx = np.full(Mv.shape, -1, int)
    for j in range(m):
        k = -1
        for i in range(n - 1, -1, -1):
            nx[i, j] = k
            if Rv[i, j]: k = i
    def px(i, j, s, side):
        if book is None: return Mv[i, j]
        b, a = book
        return (a[i, j] if s > 0 else b[i, j]) if side == "in" else (b[i, j] if s > 0 else a[i, j])
    def run(M, sv, hold, cool, one_per_race=True):
        rows, last_ = [], {}
        for t in range(n):
            for j in sorted(np.flatnonzero(M[t]), key=lambda q: -abs(sv[t, q])):
                key = race[j] if one_per_race else j
                if t - last_.get(key, -10**9) < cool or not ra[t, j]: continue
                s = 1.0 if sv[t, j] > 0 else -1.0
                e = nx[t, j]
                if e < 0 or e - t > 24: continue
                x = -1
                for i in range(e + hold, min(e + hold + 72, n)):
                    if Rv[i, j]: x = i; break
                if x < 0: continue
                last_[key] = t
                rows.append((s * (Mv[x, j] - Mv[t, j]), s * (Mv[x, j] - Mv[e, j]), (px(x, j, s, "out") - px(e, j, s, "in")) * s if book is not None else np.nan, Mv[e, j] * (s > 0) + (1 - Mv[e, j]) * (s < 0), L.day[0] if False else t))
        return pd.DataFrame(rows, columns=["own_bar", "next_trade", "book", "own", "t"])
    def rep(nm, Q):
        if Q.empty: print(f"  {nm}: none"); return
        t = f"  {nm}: {len(Q)} trades | signal-bar entry {100*Q.own_bar.mean()-.75:.2f}c | next-trade entry {100*Q.next_trade.mean()-.75:.2f}c (se {100*Q.next_trade.std()/np.sqrt(len(Q)):.2f}c), won {(Q.next_trade>.0075).mean():.2f}"
        if book is not None: t += f" | true book {100*Q.book.mean():.2f}c"
        print(t)
    print(lab)
    M = (np.abs(S48) >= .02) & (Mv >= .15) & (Mv <= .85) & (dp * np.sign(S48) >= .01) & (dk * np.sign(S48) >= .01); M[:, office == "chamber"] = False
    Q = run(M, S48, 288, 288); rep("slow 24 h", Q); rep("slow 24 h, own side under 70c", Q[Q.own < .7])
    dp1 = (D["P"] - D["P"].shift(6)).values; dk1 = (D["K"] - D["K"].shift(6)).values; dm1 = (S - S.shift(6)).values; df1 = (fair - fair.shift(6)).values; sf = np.sign(df1)
    F = (dp1 * sf >= .015) & (dk1 * sf >= .015) & ((dm1 * sf) < .5 * (df1 * sf)) & (Mv >= .25) & (Mv <= .75); F[:, office == "chamber"] = False
    rep("fast 8 h", run(F, df1, 96, 48))
    dG = (gap - gap.shift(576)).values; dF = (fair - fair.shift(576)).values
    G = (np.abs(dG) >= .04) & (np.sign(dG) == np.sign(Gv)) & (np.abs(dF) < .015) & (np.abs(Gv) >= .03) & (Mv >= .1) & (Mv <= .9); G[:, office == "chamber"] = False
    for h, nm in ((288, "24 h"), (576, "48 h"), (864, "72 h")): rep(f"SIG alone 4c+, {nm}", run(G, Gv, h, 864, False))
    H = (np.abs(Gv) >= .06) & (Mv >= .1) & (Mv <= .9); H[:, office == "chamber"] = False
    for h, nm in ((576, "48 h"), (864, "72 h")): rep(f"any gap 6c+, {nm}", run(H, Gv, h, 864, False))
analyse("A 3-20 Oct, last trades", A)
analyse("B 21-30 Oct, last trades, and priced on the true book", B, book=(L.bid.ffill(limit=12).values, L.ask.ffill(limit=12).values))
# split A by week and B by half for SIG-alone
