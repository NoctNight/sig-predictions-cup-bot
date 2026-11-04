"""Drill-down 14: settle execution size with OUR OWN taker orders, which are real two-party fills. For each order: shares, levels walked, VWAP against the
touch before it. Then bursts: same leg, same side, within 5 minutes, did the price walk away as we kept taking?"""
import sqlite3, sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
L = Lab()
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
print([x[1] for x in db.execute("pragma table_info(orders)")])
f = pd.read_sql_query("select f.order_id,f.exchange_id,f.side,f.price,f.quantity,f.filled_at,f.purpose from fills f where f.purpose not in ('mm','inside') and f.purpose is not null", db)
f["ts"] = (pd.to_datetime(f.filled_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
f["q"] = f.quantity.abs()
f["buyyes"] = np.where(f.quantity > 0, 1, -1)   # signed in YES terms
print("non-MM fills", len(f), f.purpose.value_counts().to_dict())
g = f.groupby(["order_id"]).agg(ex=("exchange_id", "first"), purpose=("purpose", "first"), sgn=("buyyes", "first"), shares=("q", "sum"), fills=("q", "size"), levels=("price", "nunique"), lo=("price", "min"), hi=("price", "max"), t=("ts", "min"), t1=("ts", "max"), vw=("price", lambda x: np.nan)).reset_index()
f["pq"] = f.price * f.q
vw = f.groupby("order_id").pq.sum() / f.groupby("order_id").q.sum()
g["vw"] = g.order_id.map(vw)
g["range_c"] = 100 * (g.hi - g.lo)
g = g[g.t >= L.idx[0]]
b = np.searchsorted(L.idx, g.t.values) - 1
cols = {e: i for i, e in enumerate(L.cols)}
j = g.ex.map(cols)
ok = j.notna() & (b >= 0)
g = g[ok].copy(); b = b[ok.values]; j = j[ok].astype(int).values
g["bid0"], g["ask0"] = L.B[b, j], L.A[b, j]
g["slip_c"] = 100 * np.where(g.sgn > 0, g.vw - g.ask0, g.bid0 - g.vw)    # cost over the touch seen before the order
g["size"] = pd.cut(g.shares, [0, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 1e7])
print("\nOur orders by size: cost over the prior touch (cents), levels walked, price range inside the order")
print(g.groupby("size", observed=True).agg(n=("shares", "size"), shares=("shares", "median"), slip=("slip_c", "mean"), slip_med=("slip_c", "median"), p90=("slip_c", lambda x: x.quantile(.9)), levels=("levels", "median"), range_c=("range_c", "median"), secs=("t", lambda x: 0)).round(2).drop(columns="secs").to_string())
print("\nby purpose"); print(g.groupby("purpose").agg(n=("shares", "size"), shares=("shares", "median"), mx=("shares", "max"), slip=("slip_c", "mean")).round(2).to_string())
print(g.sort_values("shares", ascending=False).head(12)[["ex", "purpose", "sgn", "shares", "fills", "levels", "range_c", "slip_c"]].round(2).to_string(index=False))
# bursts of ours: consecutive orders same leg, same direction, gaps <= 300 s
g = g.sort_values("t")
br = []
for (ex, sg), x in g.groupby(["ex", "sgn"]):
    t = x.t.values; st = 0
    for i in range(1, len(t) + 1):
        if i == len(t) or t[i] - t[i - 1] > 300:
            y = x.iloc[st:i]
            if len(y) >= 3:
                br.append(dict(ex=ex, sgn=sg, orders=len(y), shares=y.shares.sum(), secs=y.t.max() - y.t.min(), first=y.vw.iloc[0], last=y.vw.iloc[-1], walk_c=100 * sg * (y.vw.iloc[-1] - y.vw.iloc[0]), slip=y.slip_c.mean(), purpose=y.purpose.iloc[0]))
            st = i
Br = pd.DataFrame(br)
print("\nBursts of 3+ orders:", len(Br))
if len(Br):
    Br["size"] = pd.cut(Br.shares, [0, 1000, 3000, 10000, 30000, 1e7])
    print(Br.groupby("size", observed=True).agg(n=("shares", "size"), shares=("shares", "median"), orders=("orders", "median"), secs=("secs", "median"), walk=("walk_c", "mean"), walk_med=("walk_c", "median"), slip=("slip", "mean")).round(2).to_string())
    print(Br.sort_values("shares", ascending=False).head(10).round(2).to_string(index=False))
# who is on the tape: does a large run trade against the market maker ladder or against other resting orders? compare run price levels with the book snapshot depth at the touch
