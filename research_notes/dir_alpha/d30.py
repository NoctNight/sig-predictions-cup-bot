"""Drill-down 30: chamber legs in full. 12-hourly SIG against outside since the start, tape by day and who pushed, our position, the book."""
import json, sqlite3, numpy as np, pandas as pd
pd.set_option("display.width", 220, "display.max_rows", 300, "display.max_columns", 30)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
v = pd.read_sql_query("select ts,race,party,exchange_id ex,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where race in ('U.S. House','U.S. Senate')", db)
v["mid"] = (v.sig_bid + v.sig_ask) / 2; v["t"] = pd.to_datetime(v.ts, unit="s", utc=True)
v["b"] = v.t.dt.floor("12h")
for r, p in (("U.S. House", "D"), ("U.S. Senate", "D")):
    g = v[(v.race == r) & (v.party == p)].groupby("b").agg(pm=("pm", "last"), k=("kalshi", "last"), fair=("fair", "last"), bid=("sig_bid", "last"), ask=("sig_ask", "last"), mid=("mid", "last"))
    g["gap_c"] = 100 * (g.fair - g.mid)
    print(r, p); print(g.round(3).to_string())
tp = pd.read_sql_query("select exchange_id ex,price,quantity,aggressor,executed_at from tape where exchange_id in ('840','841','842','843')", db)
tp["t"] = pd.to_datetime(tp.executed_at, format="ISO8601", utc=True); tp["day"] = tp.t.dt.strftime("%m-%d"); tp["q"] = tp.quantity.abs()
tp["sq"] = tp.q * tp.aggressor.map({"buy": 1.0, "sell": -1.0}).fillna(0)
print(tp.groupby(["ex", "day"]).agg(n=("q", "size"), shares=("q", "sum"), net_taker=("sq", "sum"), avg=("price", "mean"), biggest=("q", "max")).round(3).to_string())
print(tp.sort_values("q", ascending=False).head(12)[["ex", "t", "price", "q", "aggressor"]].to_string(index=False))
f = pd.read_sql_query("select exchange_id ex,purpose,sum(quantity) q,count(*) n,avg(price) px from fills where exchange_id in ('840','841','842','843') group by 1,2", db)
print("our fills"); print(f.round(3).to_string(index=False))
b = json.load(open("data/books_latest.json"))
print("book age", json.load(open("data/books_latest.meta.json")) if True else "")
for ex in ("840", "841", "842", "843"):
    x = b.get(ex) if isinstance(b, dict) else None
    print(ex, str(x)[:700])
