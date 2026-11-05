"""Q5 robustness: an unselected panel (all favourite sides, by outside fair) so that picking wide gaps cannot fake closure; our own fills; the current holdings."""
import sqlite3, numpy as np, pandas as pd
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
v = pd.read_sql_query("select ts,exchange_id,race,party,sig_bid,sig_ask,pm,kalshi,fair from venue_fair where exchange_id is not null and sig_bid is not null and sig_ask is not null and fair is not null order by ts", db)
v["mid"] = (v.sig_bid + v.sig_ask) / 2
v = v[(v.sig_ask - v.sig_bid) < .10]
v["day"] = pd.to_datetime(v.ts, unit="s").dt.floor("D")
v["b"] = (v.ts // 21600).astype("int64") * 21600
d = v.groupby(["b", "exchange_id"]).last().reset_index()
d["fav_fair"] = np.maximum(d.fair, 1 - d.fair)
d["fav_mid"] = np.where(d.fair >= .5, d.mid, 1 - d.mid)
d["spr"] = d.sig_ask - d.sig_bid
fs = d[(d.fav_fair >= .93) & (d.fav_fair <= .985)]
fs = fs.assign(gap=fs.fav_fair - fs.fav_mid, k=(fs.fav_mid - .5) / (fs.fav_fair - .5))
t = fs.groupby("day").agg(n=("gap", "size"), legs=("exchange_id", "nunique"), gap=("gap", "mean"), med=("gap", "median"), k=("k", "median"), spr=("spr", "mean"))
t[["gap", "med", "spr"]] *= 100
print("all favourites with fair 93-98.5c (no selection on the gap)\n", t.round(3).to_string())
t.to_pickle("data/research_near_settlement/sig_panel_daily.pkl")
# fixed legs: same legs on 22 Oct and now, so entry and exit from the band cannot change the mean
s = fs.groupby("exchange_id").day.nunique()
keep = s[s >= fs.day.nunique() - 1].index
f2 = fs[fs.exchange_id.isin(keep)]
print("fixed legs", len(keep)); print((100 * f2.groupby("day").gap.mean()).round(2).to_string())
f = pd.read_sql_query("select exchange_id,side,price,quantity,filled_at,purpose from fills where filled_at>='2026-10-21'", db)
f["day"] = f.filled_at.str[:10]
print(f.groupby("purpose").quantity.sum().sort_values(ascending=False).head(16).round(0).to_string())
c = f[f.purpose.fillna("").str.contains("cheapfav|gap")]
c = c.assign(notional=c.quantity * np.where(c.side == "yes", c.price, 1 - c.price))
print(c.groupby(["day", "purpose"]).agg(n=("price", "size"), qty=("quantity", "sum"), notional=("notional", "sum"), px=("price", "mean")).round(2).to_string())
