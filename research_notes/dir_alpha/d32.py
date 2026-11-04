"""Drill-down 32. A how long the chamber walls have rested (saved books). B scan: every leg, every book level priced 2c+ better than outside, ranked by total edge on offer.
 Outside is the bot's fair at the latest poll, venues within 4c of each other. Edge at a level = shares x (fair - price) to buy YES, shares x (price - fair) to sell YES."""
import json, glob, os, sqlite3, numpy as np, pandas as pd
pd.set_option("display.width", 220, "display.max_rows", 300, "display.max_columns", 30)
fs = sorted(glob.glob("data/books_history/books_*.json"))[-14:] + ["data/books_latest.json"]
print("A walls")
for f in fs:
    try: B = json.load(open(f))
    except Exception as e: print(f, e); continue
    if isinstance(B, dict) and "books" in B: B = B["books"]
    r = []
    for ex in ("840", "842"):
        b = B.get(ex)
        if not b: r.append("none"); continue
        a = sorted(b["asks"], key=lambda x: x["price"]); bd = sorted(b["bids"], key=lambda x: -x["price"])
        ma = max(a[:6], key=lambda x: x["quantity"]); mb = max(bd[:6], key=lambda x: x["quantity"])
        r.append(f"{ex}: touch {bd[0]['price']}/{a[0]['price']} biggest ask {ma['quantity']:,.0f}@{ma['price']} biggest bid {mb['quantity']:,.0f}@{mb['price']}")
    print(os.path.basename(f)[-22:-5], " | ".join(r))
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
t = db.execute("select max(ts) from venue_fair").fetchone()[0]
v = pd.read_sql_query("select race,party,exchange_id ex,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where ts=? and exchange_id is not null", db, params=(t,))
B = json.load(open("data/books_latest.json"))
rows = []
for r in v.itertuples():
    if not (np.isfinite(r.fair)) or r.ex not in B: continue
    dis = abs(r.pm - r.kalshi) if np.isfinite(r.pm) and np.isfinite(r.kalshi) else np.nan
    b = B[r.ex]
    for side, lv in (("buy YES", b["asks"]), ("sell YES", b["bids"])):
        e = 0; sh = 0; cash = 0; rows_ = sorted(lv, key=lambda x: x["price"] * (1 if side == "buy YES" else -1))
        for l in rows_:
            ed = (r.fair - l["price"]) if side == "buy YES" else (l["price"] - r.fair)
            if ed < .02: break
            e += ed * l["quantity"]; sh += l["quantity"]; cash += l["quantity"] * (l["price"] if side == "buy YES" else 1 - l["price"])
        if sh: rows.append(dict(race=r.race, party=r.party, ex=r.ex, side=side, pm=r.pm, k=r.kalshi, fair=round(r.fair, 3), touch=rows_[0]["price"], touch_edge_c=round(100 * ((r.fair - rows_[0]["price"]) if side == "buy YES" else (rows_[0]["price"] - r.fair)), 1), shares=int(sh), cash=int(cash), edge_usd=int(e), ret=round(e / cash, 3) if cash else 0, dis=dis))
R = pd.DataFrame(rows).sort_values("edge_usd", ascending=False)
R.to_pickle("data/research_diralpha/scan.pkl")
print("\nB legs with 2c+ edge on the book:", len(R), "total edge on offer", R.edge_usd.sum())
print(R.head(30).round(3).to_string(index=False))
print("\nexcluding venues more than 4c apart, and any leg priced under 5c or above 95c")
Q = R[(R.dis.fillna(0) <= .04) & (R.fair > .05) & (R.fair < .95)]
print(Q.head(25).round(3).to_string(index=False))
print("\nbiggest walls anywhere (a single level over 100k within 2 ticks of the touch)")
out = []
for ex, b in B.items():
    for sd, lv in (("ask", sorted(b["asks"], key=lambda x: x["price"])[:3]), ("bid", sorted(b["bids"], key=lambda x: -x["price"])[:3])):
        for l in lv:
            if l["quantity"] >= 100000:
                x = v[v.ex == ex]
                out.append(dict(race=x.race.iloc[0] if len(x) else ex, party=x.party.iloc[0] if len(x) else "", sd=sd, price=l["price"], q=int(l["quantity"]), fair=round(float(x.fair.iloc[0]), 3) if len(x) else None))
print(pd.DataFrame(out).sort_values("q", ascending=False).head(20).to_string(index=False))
