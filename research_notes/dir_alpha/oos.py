"""Out-of-sample test of the frozen lag rules on 5-21 Oct, data they were never fitted on.
SIG: 5-minute candle closes (last trade, no book). Polymarket: 5-minute prices. Kalshi: hourly bid/ask mid. Fair: mean of the two.
Slow rule as frozen, minus the taker flow filter (no tape before 21 Oct). Fast rule on a 1 h window (Kalshi is hourly), the d15 fast1 variant.
No bid or ask, so profit = mid-to-mid on last trades, less an assumed 0.75c round trip (typical spread on the strict trades). Needs a real trade within 1 h of entry and a real trade within 6 h after exit time."""
import glob, json, pickle, numpy as np, pandas as pd
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
O = "data/research_diralpha/oos/"
idx = json.load(open(O + "index.json")); meta = pd.read_pickle("data/research_diralpha/meta.pkl")
meta["key"] = meta.race + "|" + meta.party
key2ex = {k: e for e, k in meta.key.items()}
C = json.load(open("data/candles_5m.json"))
grid = pd.date_range("2026-10-02", "2026-10-21 23:55", freq="5min", tz="UTC")
sig, real, pmv, kv = {}, {}, {}, {}
for ex, v in C.items():
    d = pd.DataFrame(v["candles"])
    if d.empty: continue
    s = pd.Series(d.close.values, index=pd.to_datetime(d.time, utc=True))
    s = s[~s.index.duplicated(keep="last")].reindex(grid)
    real[ex] = s.notna(); sig[ex] = s.ffill(limit=288)
for f, v in idx.items():
    ex = key2ex.get(v["race"] + "|" + v["party"])
    if ex is None: continue
    d = json.load(open(O + f))
    if v["venue"] == "pm":
        h = d["history"]; s = pd.Series([x["p"] for x in h], index=pd.to_datetime([x["t"] for x in h], unit="s", utc=True))
        s = s[~s.index.duplicated(keep="last")].resample("5min").last().reindex(grid).ffill(limit=36); pmv[ex] = s
    else:
        h = d["candlesticks"]
        t = pd.to_datetime([x["end_period_ts"] for x in h], unit="s", utc=True)
        def f_(x, k):
            try: return float(x[k]["close_dollars"])
            except Exception: return np.nan
        b = np.array([f_(x, "yes_bid") for x in h]); a = np.array([f_(x, "yes_ask") for x in h])
        s = pd.Series((b + a) / 2, index=t).where((a - b) <= .1)
        s = s[~s.index.duplicated(keep="last")].resample("5min").last().reindex(grid).ffill(limit=24); kv[ex] = s
        # minute candles where we have them, same mid rule
        base = f[:-5]; ms = []
        for g in sorted(glob.glob(O + "k1/" + base + "_*.json")):
            try: c1 = json.load(open(g)).get("candlesticks") or []
            except Exception: c1 = []
            if not c1: continue
            t1 = pd.to_datetime([x["end_period_ts"] for x in c1], unit="s", utc=True)
            b1 = np.array([f_(x, "yes_bid") for x in c1]); a1 = np.array([f_(x, "yes_ask") for x in c1])
            ms.append(pd.Series((b1 + a1) / 2, index=t1).where((a1 - b1) <= .1))
        if ms:
            q = pd.concat(ms); q = q[~q.index.duplicated(keep="last")].resample("5min").last().reindex(grid).ffill(limit=12)
            kv[ex] = q.combine_first(s)
cols = sorted(set(sig) & set(pmv) & set(kv), key=int)
S_, R_, P_, K_ = (pd.DataFrame({c: x[c] for c in cols}) for x in (sig, real, pmv, kv))
print("legs", len(cols), "SIG bins with a trade by week"); print(R_.resample("7D").sum().sum(axis=1).to_string())
m = meta.reindex(cols); race = m.race.values; office = m.office.values; party = m.party.values
dsign = np.where(party == "D", 1.0, np.where(party == "R", -1.0, 0.0)); nochm = office != "chamber"
fair = (P_ + K_) / 2; gap = fair - S_
S48 = gap - gap.rolling(576, min_periods=288).median(); sg = np.sign(S48)
Rv = R_.values; Mv = S_.values; n, mm = Mv.shape; day = np.asarray(grid.strftime("%m-%d"))
ra = R_.rolling(12, min_periods=1).max().fillna(0).values > 0     # real trade in the last hour
def run(M, sv, hold, cool, lab, own_max=None):
    out, last = [], {}
    for t in range(n):
        ix = np.flatnonzero(M[t])
        for j in sorted(ix, key=lambda q: -abs(sv[t, q])):
            r = race[j]
            if t - last.get(r, -10**9) < cool or not ra[t, j]: continue
            e = next((i for i in range(t + 1, min(t + 25, n)) if Rv[i, j]), None)     # enter at the next real trade: the signal bar's own print can be off-market
            if e is None: continue
            x = None
            for i in range(e + hold, min(e + hold + 72, n)):
                if Rv[i, j]: x = i; break
            if x is None: continue
            s = 1.0 if sv[t, j] > 0 else -1.0
            fut = np.nanmean(np.where(R_.iloc[x - 12:x + 12, :].values, S_.iloc[x - 12:x + 12, :].values, np.nan)) if False else 0
            rest = np.nanmean(((Mv[x] - Mv[e]) * dsign * s * dsign[j])[(Mv[e] >= .15) & (Mv[e] <= .85) & nochm & (dsign != 0) & (race != r)])
            last[r] = t
            out.append(dict(j=j, t=t, e=e, x=x, s=s, gross=s * (Mv[x, j] - Mv[e, j]), own=Mv[e, j] if s > 0 else 1 - Mv[e, j], ds=s * dsign[j], office=office[j], day=day[t], race=r, sig=abs(sv[t, j]), rest=rest))
    E = pd.DataFrame(out)
    if E.empty: print(lab, "none"); return E
    E["net"] = E.gross - .0075
    d = E.groupby("day").net.mean()
    print(f"{lab}: {len(E)} trades, {E.race.nunique()} races, gross {100*E.gross.mean():.2f}c, net {100*E.net.mean():.2f}c (se {100*E.net.std()/np.sqrt(len(E)):.2f}c), moved our way {(E.gross>0).mean():.2f}, days up {(d>0).sum()}/{len(d)}, excess over rest {100*(E.gross-E.rest).mean():.2f}c")
    return E
dpm = (P_ - P_.shift(288)).values; dk = (K_ - K_.shift(288)).values; Sv = S48.values
M = (np.abs(Sv) >= .02) & (Mv >= .15) & (Mv <= .85) & (dpm * np.sign(Sv) >= .01) & (dk * np.sign(Sv) >= .01); M[:, ~nochm] = False
for h, nm in ((288, "24 h"), (432, "36 h")):
    E = run(M, Sv, h, 288, f"SLOW {nm}")
    if nm == "36 h":
        E.to_pickle(O + "slow36.pkl")
        print(E.groupby("office").net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
        print(E.groupby("ds").net.agg(n="size", net=lambda v: 100 * v.mean()).round(2).to_string())
        print(E.groupby("day").net.agg(n="size", net=lambda v: 100 * v.mean()).round(2).to_string())
        print(E.groupby(E.own >= .70).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
    if nm == "24 h": E.to_pickle(O + "slow24.pkl")
# placebo: the same rule with a 36 h shifted signal, and flipped sign is -gross-1.5c
pickle.dump(dict(S=S_, R=R_, P=P_, K=K_, cols=cols, race=race, office=office, party=party), open(O + "panel.pkl", "wb"))
for W, wl in ((6, "30 min"), (12, "1 h")):
    dp1 = (P_ - P_.shift(W)).values; dk1 = (K_ - K_.shift(W)).values; dm1 = (S_ - S_.shift(W)).values; df1 = (fair - fair.shift(W)).values
    sf = np.sign(df1)
    F = (dp1 * sf >= .015) & (dk1 * sf >= .015) & ((dm1 * sf) < .5 * (df1 * sf)) & (Mv >= .25) & (Mv <= .75); F[:, ~nochm] = False
    for h, nm in ((48, "4 h"), (96, "8 h"), (288, "24 h")):
        E = run(F, df1, h, 48, f"FAST {wl} window, hold {nm}")
        if len(E) and h == 48:
            E.to_pickle(O + f"fast{W}.pkl")
            print(E.groupby("office").net.agg(n="size", net=lambda v: 100 * v.mean()).round(2).to_string()); print(E.groupby("ds").net.agg(n="size", net=lambda v: 100 * v.mean()).round(2).to_string())
            print(E.groupby(pd.cut(E.sig, [0, .023, .035, 1])).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
# placebo for slow: other leg same office and party, same entry and exit bins
E = pd.read_pickle(O + "slow36.pkl"); rng = np.random.default_rng(3); pl = []
for r in E.itertuples():
    c = np.flatnonzero((office == office[r.j]) & (party == party[r.j]) & (Mv[r.t] >= .15) & (Mv[r.t] <= .85) & (race != race[r.j]))
    if len(c): q = rng.choice(c, min(10, len(c)), replace=False); pl.append(np.nanmean(r.s * (Mv[r.x, q] - Mv[r.e, q])))
print("slow placebo gross %.2fc against %.2fc" % (100 * np.nanmean(pl), 100 * E.gross.mean()))
