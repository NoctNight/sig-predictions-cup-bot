"""Overnight directional alpha study (31 Oct): pull everything onto one 5-minute panel, one row per SIG leg and bin.
Read-only on data/sigbot.sqlite. Output: data/research_diralpha/panel.pkl (dict of DataFrames indexed by bin, one column per
exchange id) and meta.pkl (leg metadata)."""
import sqlite3, sys, re, pickle
import numpy as np, pandas as pd
sys.path.insert(0, ".")
BIN = 300
OUT = "data/research_diralpha/"
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
vf = pd.read_sql_query("select ts,race,party,pm,kalshi,fair,pm_w,k_w,sig_bid,sig_ask,exchange_id from venue_fair where exchange_id is not null", db)
print("venue_fair", len(vf), flush=True)
vf["b"] = (vf.ts // BIN).astype("int64") * BIN
g = vf.sort_values("ts").groupby(["b", "exchange_id"]).last().reset_index()
P = {}
for c in ["pm", "kalshi", "fair", "pm_w", "k_w", "sig_bid", "sig_ask"]:
    P[c] = g.pivot(index="b", columns="exchange_id", values=c)
idx = np.arange(P["sig_bid"].index.min(), P["sig_bid"].index.max() + BIN, BIN)
for c in list(P):
    P[c] = P[c].reindex(idx).ffill(limit=36)          # a stale poll is carried at most 3 h (outages)
P["fresh"] = g.pivot(index="b", columns="exchange_id", values="sig_bid").reindex(idx).notna()
meta = vf.groupby("exchange_id")[["race", "party"]].last()
del vf, g
tp = pd.read_sql_query("select exchange_id,price,quantity,executed_at,aggressor,trade_id from tape", db)
print("tape", len(tp), flush=True)
tp["ts"] = (pd.to_datetime(tp.executed_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
print("tape span", tp.ts.min(), tp.ts.max(), flush=True)
tp.to_pickle(OUT + "tape.pkl")
tp["b"] = (tp.ts // BIN).astype("int64") * BIN
tp["q"] = tp.quantity.abs()
tp["n"] = tp.q * tp.price
tp["sgn"] = tp.aggressor.map({"buy": 1.0, "sell": -1.0}).fillna(0.0)
tp["sq"] = tp.sgn * tp.q
tp["big"] = (tp.q * tp.price >= 2000).astype(float)
agg = tp.groupby(["b", "exchange_id"]).agg(vol=("q", "sum"), notional=("n", "sum"), sflow=("sq", "sum"), ntr=("q", "size"), nbig=("big", "sum"), last=("price", "last")).reset_index()
for c in ["vol", "notional", "sflow", "ntr", "nbig"]:
    P[c] = agg.pivot(index="b", columns="exchange_id", values=c).reindex(idx).fillna(0.0).reindex(columns=P["sig_bid"].columns, fill_value=0.0)
P["last"] = agg.pivot(index="b", columns="exchange_id", values="last").reindex(idx).reindex(columns=P["sig_bid"].columns).ffill()
# longer SIG price series from the tape (starts 15 Oct)
idx2 = np.arange((int(tp.ts.min()) // BIN) * BIN, idx[-1] + BIN, BIN)
assert len(idx2) < 20000
P["last_long"] = agg.pivot(index="b", columns="exchange_id", values="last").reindex(idx2).reindex(columns=P["sig_bid"].columns).ffill()
vq = pd.read_sql_query("select ts,venue,race,party,bid,ask,bid_sz,ask_sz,depth_usd from venue_quotes where venue='polymarket'", db)
print("pm quotes", len(vq), flush=True)
key = {(r.race, r.party): e for e, r in meta.iterrows()}
vq["ex"] = [key.get(k) for k in zip(vq.race, vq.party)]
vq = vq[vq.ex.notna()]
vq["b"] = (vq.ts // BIN).astype("int64") * BIN
g = vq.sort_values("ts").groupby(["b", "ex"]).last().reset_index()
for c in ["bid_sz", "ask_sz", "depth_usd"]:
    P["pm_" + c] = g.pivot(index="b", columns="ex", values=c).reindex(idx).ffill(limit=12).reindex(columns=P["sig_bid"].columns)
q = pd.read_sql_query("select ts,exchange_id,bid_qty,ask_qty from quotes", db)
print("own quotes", len(q), flush=True)
q["b"] = (q.ts // BIN).astype("int64") * BIN
g = q.sort_values("ts").groupby(["b", "exchange_id"]).last().reset_index()
for c in ["bid_qty", "ask_qty"]:
    P[c] = g.pivot(index="b", columns="exchange_id", values=c).reindex(idx).ffill(limit=6).reindex(columns=P["sig_bid"].columns)
def office(n):
    if n in ("U.S. House", "U.S. Senate"): return "chamber"
    if "House race" in n: return "house"
    if n.endswith("Senate"): return "senate"
    if n.endswith("Governor"): return "governor"
    return "other"
meta["office"] = meta.race.map(office)
meta["state"] = meta.race.map(lambda n: re.split(r"-\d\d | Senate| Governor| House", n)[0].strip())
meta["nlegs"] = meta.groupby("race").party.transform("size")
pickle.dump(P, open(OUT + "panel.pkl", "wb"), protocol=5)
meta.to_pickle(OUT + "meta.pkl")
print({k: v.shape for k, v in P.items()})
print(meta.office.value_counts(), meta.state.nunique())
