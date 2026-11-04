"""Drill-down 33: the House D seller. Top-of-book size on leg 840 through time (bot quotes table), tape at each level since 28 Oct, net flow into the wall."""
import sqlite3, numpy as np, pandas as pd, json, glob, os
pd.set_option("display.width", 220, "display.max_rows", 300, "display.max_columns", 30)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
q = pd.read_sql_query("select ts,best_bid b,best_ask a,bid_qty bq,ask_qty aq from quotes where exchange_id='840' and ts>1793000000 order by ts", db)
q["t"] = pd.to_datetime(q.ts, unit="s", utc=True)
if len(q):
    x = q.set_index("t").resample("2h").last()
    print(x[["b", "a", "bq", "aq"]].dropna().tail(40).to_string())
tp = pd.read_sql_query("select price,quantity q,aggressor a,executed_at e from tape where exchange_id='840' and executed_at>='2026-10-28'", db)
tp["t"] = pd.to_datetime(tp.e, format="ISO8601", utc=True); tp["q"] = tp.q.abs()
tp["h"] = tp.t.dt.floor("6h")
print(tp.groupby("h").agg(n=("q", "size"), shares=("q", "sum"), lo=("price", "min"), hi=("price", "max"), vwap=("price", lambda s: np.average(s, weights=tp.loc[s.index, "q"]))).round(3).to_string())
t2 = tp[tp.t >= "2026-10-30 20:00"]
print(t2.groupby("price").q.agg(["size", "sum"]).to_string())
print(t2.sort_values("t").tail(25)[["t", "price", "q", "a"]].to_string(index=False))
print("our 30 Oct 14:16 burst, prints per second on 840"); b = tp[(tp.t >= "2026-10-30 14:15") & (tp.t <= "2026-10-30 14:24")]; print(b.groupby(b.t.dt.floor("10s")).agg(n=("q", "size"), shares=("q", "sum"), lo=("price", "min"), hi=("price", "max")).to_string())
# all saved books: the biggest three ask levels within 5c of the touch, and bids, for 840
for f in sorted(glob.glob("data/books_history/books_*.json"))[-24:] + ["data/books_latest.json"]:
    B = json.load(open(f)); B = B.get("books", B); b = B.get("840")
    if not b: continue
    a = sorted(b["asks"], key=lambda x: x["price"]); bd = sorted(b["bids"], key=lambda x: -x["price"])
    big = sorted(a, key=lambda x: -x["quantity"])[:3]
    tot = sum(l["quantity"] for l in a if l["price"] <= a[0]["price"] + .05)
    print(os.path.basename(f)[-19:-5], "bid", bd[0]["price"], "ask", a[0]["price"], "asks within 5c %9.0f" % tot, "bids within 5c %9.0f" % sum(l["quantity"] for l in bd if l["price"] >= bd[0]["price"] - .05), [(l["price"], int(l["quantity"])) for l in big])
