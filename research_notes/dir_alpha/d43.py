"""Drill-down 43: do the chamber gaps close? Full October, Polymarket hourly against SIG (daily candles 1-11 Oct, hourly 11-21 Oct, polls after).
Gap, House: PM - SIG. Senate: SIG - PM. Pair: their sum. If entered when the gap was this wide, what had the gap done 87 h later (now to 18:00 ET 3 Nov)?"""
import json, sqlite3, numpy as np, pandas as pd
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
def pm(n):
    h = json.load(open(f"data/research_diralpha/chamber/pm_{n}.json"))["history"]
    return pd.Series([x["p"] for x in h], index=pd.to_datetime([x["t"] for x in h], unit="s", utc=True)).resample("1h").last().ffill()
def sig(ex):
    d = pd.DataFrame(json.load(open("data/candles_1d.json"))[ex]["candles"]); s1 = pd.Series(d.close.values, index=pd.to_datetime(d.time, utc=True) + pd.Timedelta(days=1) - pd.Timedelta(hours=1))
    h = pd.DataFrame(json.load(open("data/hist.json"))[ex]["candles"]); s2 = pd.Series(h.close.values, index=pd.to_datetime(h.time, utc=True))
    db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
    v = pd.read_sql_query("select ts,sig_bid,sig_ask from venue_fair where exchange_id=? and sig_bid is not null", db, params=(ex,))
    s3 = pd.Series(((v.sig_bid + v.sig_ask) / 2).values, index=pd.to_datetime(v.ts, unit="s", utc=True)).resample("1h").last()
    s = pd.concat([s1[s1.index < s2.index[0]], s2[s2.index < s3.index[0]], s3]).sort_index()
    return s.resample("1h").last().ffill(limit=30)
PMh, PMs = pm("hD"), pm("sD"); Sh, Ss = sig("840"), sig("842")
D = pd.DataFrame(dict(pm_h=PMh, pm_s=PMs, sig_h=Sh, sig_s=Ss)).dropna()
D["gh"] = D.pm_h - D.sig_h; D["gs"] = D.sig_s - D.pm_s; D["gp"] = D.gh + D.gs
D.to_pickle("data/research_diralpha/chamber/gaps.pkl")
print((D[["gh", "gs", "gp"]].resample("2D").last() * 100).round(1).to_string())
print("means since 1 Oct (cents)", (100 * D[["gh", "gs", "gp"]].mean()).round(2).to_dict(), "now", (100 * D[["gh", "gs", "gp"]].iloc[-1]).round(2).to_dict())
daily = D[["gh", "gs", "gp"]].resample("1D").last().dropna()
for c in ("gh", "gs", "gp"):
    x = daily[c].values; b = np.polyfit(x[:-1], x[1:], 1); print(f"{c}: AR1 {b[0]:.2f}, long-run mean {100*b[1]/(1-b[0]):.1f}c, half-life {np.log(.5)/np.log(b[0]):.1f} d, n {len(x)}; expected in 3.6 d from now {100*(b[1]/(1-b[0])+(x[-1]-b[1]/(1-b[0]))*b[0]**3.6):.1f}c")
H = 87
for c, thr in (("gp", .07), ("gp", .10), ("gh", .05), ("gh", .07), ("gs", .03), ("gs", .05)):
    s = D[c]; fut = s.shift(-H)
    m = (s >= thr) & fut.notna()
    if not m.sum(): print(c, thr, "none"); continue
    ch = (s - fut)[m]
    # episodes: runs separated by 48 h+
    idx = s[m].index; ep = (pd.Series(idx).diff() > pd.Timedelta(hours=48)).cumsum().values
    e = ch.groupby(ep).agg(["size", "mean", "min", "max"]); e["start"] = pd.Series(idx).groupby(ep).first().dt.strftime("%m-%d %H")
    print(f"\n{c} >= {thr*100:.0f}c: {m.sum()} hours, {e.shape[0]} episodes, closed by {100*ch.mean():.2f}c in {H} h (median {100*ch.median():.2f}c), share closing at least 3c {(ch>=.03).mean():.2f}, share widening {(ch<0).mean():.2f}")
    print((e.assign(mean=e["mean"] * 100, min=e["min"] * 100, max=e["max"] * 100)).round(1).to_string())
