"""Level gap trade, strict read. Rule stated before this run: gap to outside 6c+, our side 50-90c, chamber out, one bet per leg per 72 h, buy at the touch, sell at the touch 72 h later.
 1 clustered standard errors by race and by day, both periods  2 does it survive leaving out the top race and the top day  3 by venue agreement  4 what is on offer now."""
import json, sqlite3, numpy as np, pandas as pd
pd.set_option("display.width", 200, "display.max_rows", 100, "display.max_columns", 30)
A = pd.read_pickle("data/research_diralpha/oos/level_a.pkl"); B = pd.read_pickle("data/research_diralpha/oos/level_b.pkl")
def stat(E, c="n72"):
    q = E.dropna(subset=[c]); r = q.groupby("race")[c].mean(); d = q.groupby("day")[c].mean()
    return dict(trades=len(q), races=q.race.nunique(), days=q.day.nunique(), net=round(100 * q[c].mean(), 2), win=round((q[c] > 0).mean(), 2), t_race=round(r.mean() / (r.std() / np.sqrt(len(r))), 2), t_day=round(d.mean() / (d.std() / np.sqrt(len(d))), 2), days_up=f"{(d>0).sum()}/{len(d)}", drop_best_race=round(100 * q[q.race != r.idxmax()][c].mean(), 2), drop_best_day=round(100 * q[q.day != d.idxmax()][c].mean(), 2))
for lab, E in (("A 3-20 Oct", A), ("B 21-30 Oct true book", B)):
    Q = E[(E.g >= .06) & (E.own >= .5)]
    print(lab, "72 h", stat(Q)); print(lab, "48 h", stat(Q, "n48"))
    print(lab, "8c+ 72 h", stat(E[(E.g >= .08) & (E.own >= .5)]))
    print(lab, "4-6c 72 h", stat(E[(E.g < .06) & (E.own >= .5)]))
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
t = db.execute("select max(ts) from venue_fair").fetchone()[0]
v = pd.read_sql_query("select race,party,exchange_id ex,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where ts=? and exchange_id is not null", db, params=(t,))
b = json.load(open("data/books_latest.json"))
pos = pd.read_sql_query("select exchange_id ex, sum(quantity) q from fills group by 1", db).set_index("ex").q
rows = []
for r in v.itertuples():
    if r.race in ("U.S. House", "U.S. Senate") or not np.isfinite(r.fair) or not np.isfinite(r.pm) or not np.isfinite(r.kalshi) or abs(r.pm - r.kalshi) > .04: continue
    m = (r.sig_bid + r.sig_ask) / 2; g = r.fair - m
    if abs(g) < .06 or m < .1 or m > .9: continue
    own = m if g > 0 else 1 - m
    if own < .5: continue
    s = "YES" if g > 0 else "NO"; bk = b.get(r.ex) or {}
    lv = sorted(bk.get("asks", []), key=lambda x: x["price"]) if g > 0 else sorted(bk.get("bids", []), key=lambda x: -x["price"])
    px = r.sig_ask if g > 0 else 1 - r.sig_bid
    sz = sum(l["quantity"] for l in lv if abs(l["price"] - lv[0]["price"]) <= .0101) if lv else 0
    dside = "D" if (r.party == "D") == (g > 0) else "R"
    rows.append(dict(race=r.race, leg=f"{r.party} {s}", side=dside, price=round(px, 3), outside=round(r.fair if g > 0 else 1 - r.fair, 3), gap_c=round(100 * abs(g), 1), size_1c=int(sz), held=int(pos.get(r.ex, 0))))
L = pd.DataFrame(rows).sort_values("gap_c", ascending=False); print("\non offer now (poll", pd.Timestamp(t, unit="s"), ") ", len(L), "legs"); print(L.to_string(index=False)); print(L.groupby("side").size())
