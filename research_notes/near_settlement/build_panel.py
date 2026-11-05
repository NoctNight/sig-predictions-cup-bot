"""Hourly panel of the resolved election legs, one leg per race per venue, plus the comparison markets when they exist.

tau = days to T, where T is 23:00 UTC on election day (18:00 ET, before any result). A leg's YES price is flipped when needed so
`p` always means the race's eventual favourite-by-price at T-10 is not assumed: we keep YES and let the studies pick the side.
Run after fetch_elections.py. Output data/research_near_settlement/panel.pkl, legs.pkl.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import net
OUT = net.OUT
HOUR = 3600
pl = pd.read_csv(f"{OUT}/pm_legs.csv", dtype={"market_id": str, "token": str})
kl = pd.read_csv(f"{OUT}/kalshi_legs.csv", dtype={"market_id": str})


def grid(ts, px, T, bid=None, ask=None):
    """Last price at or before each whole hour of T-14..T+1, carried at most 6 h."""
    s = pd.DataFrame({"ts": ts, "p": px, "bid": bid, "ask": ask}).sort_values("ts")
    s["h"] = np.ceil((s.ts - T) / HOUR).astype("int64")
    s = s.groupby("h").last()
    idx = np.arange(-14 * 24, 24 + 1)
    s = s.reindex(idx)
    s["fresh"] = s.p.notna()
    s[["p", "bid", "ask"]] = s[["p", "bid", "ask"]].ffill(limit=6)
    med = s.p.rolling(7, center=True, min_periods=4).median()      # bad prints: a 1-3 hour excursion that comes straight back (a real step survives this)
    bad = (s.p - med).abs() > .04
    s["spike"] = bad
    s["p"] = s.p.where(~bad, med)
    s["tau"] = -s.index / 24
    return s.reset_index(names="h")


rows, meta = [], []
for _, r in pl.iterrows():
    p = f"{OUT}/raw/pm/h_{r.token}.json"
    if not os.path.exists(p):
        continue
    h = json.load(open(p)).get("history") or []
    if len(h) < 24:
        continue
    g = grid([x["t"] for x in h], [x["p"] for x in h], r["T"])
    g["key"] = "pm:" + r.market_id
    rows.append(g)
    meta.append(dict(key="pm:" + r.market_id, venue="polymarket", race=r.race_id, cycle=r.cycle, office=r.office, party=r.party, outcome=r.outcome, vol=r.volume_usd, T=r["T"], group="election", night=r.election_date, multi=r.market_id))
for _, r in kl.iterrows():
    p = f"{OUT}/raw/kalshi/{r.market_id}.json"
    if not os.path.exists(p):
        continue
    c = json.load(open(p))
    if len(c) < 24:
        continue
    d = pd.DataFrame(c, columns=["ts", "bid", "ask", "last", "volume"])
    for k in ("bid", "ask", "last"):
        d[k] = pd.to_numeric(d[k], errors="coerce")
    two = d.bid.notna() & d.ask.notna() & ((d.ask - d.bid) < .25) & (d.bid > 0)
    px = ((d.bid + d.ask) / 2).where(two, d["last"])
    g = grid(d.ts.values, px.values, r["T"], d.bid.where(two).values, d.ask.where(two).values)
    g["key"] = "k:" + r.market_id
    rows.append(g)
    meta.append(dict(key="k:" + r.market_id, venue="kalshi", race=r.race_id, cycle=r.cycle, office=r.office, party=r.party, outcome=r.outcome, vol=r.volume_usd, T=r["T"], group="election", night=r.election_date, multi=r.market_id))
if os.path.exists(f"{OUT}/others/legs.csv") and os.path.exists(f"{OUT}/others/hourly.csv"):
    ol = pd.read_csv(f"{OUT}/others/legs.csv", dtype={"market_id": str})
    oh = pd.read_csv(f"{OUT}/others/hourly.csv", dtype={"market_id": str})
    for (v, m), d in oh.groupby(["venue", "market_id"]):
        q = ol[(ol.venue == v) & (ol.market_id == m)]
        if q.empty or len(d) < 24:
            continue
        r = q.iloc[0]
        g = grid(d.ts.values, d.price.values, int(r.t_settle), d.bid.values, d.ask.values)
        pre = "pm:" if v == "polymarket" else "k:"
        g["key"] = pre + m
        rows.append(g)
        meta.append(dict(key=pre + m, venue=v, race=r.contract, cycle=r.cluster, office=r.category, party="", outcome=r.outcome, vol=r.volume, T=int(r.t_settle), group=r.category, night=r.cluster, multi=m))
P, M = pd.concat(rows, ignore_index=True), pd.DataFrame(meta)
P.to_pickle(f"{OUT}/panel.pkl"); M.to_pickle(f"{OUT}/legs.pkl")
print(M.groupby(["group", "venue"]).agg(legs=("key", "size"), races=("race", "nunique")).to_string())
print(P[P.key == M.key.iloc[0]].iloc[::48][["tau", "p", "fresh"]].head(8).to_string())
print("spikes replaced", int(P.spike.sum()), "of", int(P.p.notna().sum()))
print("share of leg-hours with a fresh print by venue", P.merge(M[["key", "venue"]]).groupby("venue").fresh.mean().round(3).to_dict())
