"""The favourites the sleeve can buy now: 90-92c at SIG's ask, fair 4c+ higher. Depth when we sell is taken from the last local book snapshot."""
import sqlite3, json, numpy as np, pandas as pd
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
t = db.execute("select max(ts) from venue_fair").fetchone()[0]
v = pd.read_sql_query(f"select * from venue_fair where ts>{t-900} and exchange_id is not null and sig_bid is not null and sig_ask is not null and fair is not null order by ts", db)
v = v.groupby("exchange_id").last().reset_index()
b = json.load(open("data/books_latest.json"))
rows = []
for _, r in v.iterrows():
    for side in ("yes", "no"):
        ask = r.sig_ask if side == "yes" else 1 - r.sig_bid
        bid = r.sig_bid if side == "yes" else 1 - r.sig_ask
        f = r.fair if side == "yes" else 1 - r.fair
        if not (.895 <= ask <= .925 and f - ask >= .035):
            continue
        k = b.get(str(r.exchange_id))
        d = dict(ex=r.exchange_id, race=r.race, party=r.party, side=side, ask=ask, bid=bid, fair=f, pm=r.pm, kalshi=r.kalshi)
        if k:
            lv = k["bids"] if side == "yes" else [dict(price=1 - x["price"], quantity=x["quantity"]) for x in k["asks"]]
            lv = sorted(lv, key=lambda x: -x["price"])
            top = lv[0]["price"] if lv else np.nan
            def walk(n):
                left, cost = n, 0.0
                for x in lv:
                    q = min(left, x["quantity"]); cost += q * (top - x["price"]); left -= q
                    if left <= 0:
                        return cost / n
                return np.nan
            d.update(w1200=walk(1200), w2400=walk(2400), top=top, d0=sum(x["quantity"] for x in lv if x["price"] >= top - 1e-9), d1=sum(x["quantity"] for x in lv if x["price"] >= top - .0101), d2=sum(x["quantity"] for x in lv if x["price"] >= top - .0201))
        rows.append(d)
L = pd.DataFrame(rows)
L["edge"], L["spr"] = L.fair - L.ask, L.ask - L.bid
L.to_pickle("data/research_near_settlement/live_book.pkl")
print(pd.Timestamp(t, unit="s"), len(L), "sides")
print(L.sort_values("edge", ascending=False)[["race", "party", "side", "ask", "bid", "fair", "pm", "kalshi", "edge", "d0", "d1", "d2"]].round(3).to_string(index=False))
print(L[["ask", "bid", "fair", "edge", "spr", "d0", "d1", "d2", "w1200", "w2400"]].describe().loc[["mean", "50%", "min"]].round(3).to_string())
print("pm vs kalshi disagreement on these legs, c:", (100 * (L.pm - L.kalshi).abs()).describe()[["mean", "50%", "max"]].round(2).to_dict())
