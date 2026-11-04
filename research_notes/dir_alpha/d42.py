"""Drill-down 42: what SIG's compression does to a punt. For every leg and side: price to buy at the touch against the outside chance. Expected value per dollar staked = outside chance / SIG price - 1.
By price band, then the longshots where SIG is cheapest, with size available at or below 1c above the touch."""
import json, sqlite3, numpy as np, pandas as pd
pd.set_option("display.width", 200, "display.max_rows", 100, "display.max_columns", 30)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
t = db.execute("select max(ts) from venue_fair").fetchone()[0]
v = pd.read_sql_query("select race,party,exchange_id ex,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where ts=? and exchange_id is not null", db, params=(t,))
B = json.load(open("data/books_latest.json"))
rows = []
for r in v.itertuples():
    if not np.isfinite(r.fair) or r.ex not in B: continue
    b = B[r.ex]
    a = sorted(b["asks"], key=lambda x: x["price"]); d = sorted(b["bids"], key=lambda x: -x["price"])
    if a and np.isfinite(r.sig_ask):
        px = r.sig_ask; sz = sum(l["quantity"] for l in a if l["price"] <= px + .0101)
        rows.append(dict(race=r.race, leg=f"{r.party} YES", p=r.fair, px=px, sz=int(sz), dis=abs(r.pm - r.kalshi) if np.isfinite(r.pm) and np.isfinite(r.kalshi) else np.nan))
    if d and np.isfinite(r.sig_bid):
        px = 1 - r.sig_bid; sz = sum(l["quantity"] for l in d if 1 - l["price"] <= px + .0101)
        rows.append(dict(race=r.race, leg=f"{r.party} NO", p=1 - r.fair, px=px, sz=int(sz), dis=abs(r.pm - r.kalshi) if np.isfinite(r.pm) and np.isfinite(r.kalshi) else np.nan))
R = pd.DataFrame(rows); R["ev"] = R.p / R.px - 1; R["mult"] = 1 / R.px; R["cash"] = (R.sz * R.px).astype(int)
R = R[(R.px > .005) & (R.px < .995)]
g = pd.cut(R.px, [0, .03, .05, .08, .12, .2, .3, .4, .5, .7, .9, 1])
o = R.groupby(g, observed=True).agg(n=("ev", "size"), px=("px", "mean"), outside=("p", "mean"), ev_mean=("ev", "mean"), ev_median=("ev", "median"), best=("ev", "max"), share_pos=("ev", lambda v: (v > 0).mean()))
print(o.round(3).to_string())
L = R[(R.px <= .40) & (R.dis.fillna(0) < .04) & (R.p >= .01)].sort_values("ev", ascending=False)
print("\nlongshots at 40c or less, venues agree, best first"); print(L.head(24)[["race", "leg", "p", "px", "mult", "ev", "sz", "cash"]].round(3).to_string(index=False))
print("\nsame, 12c or less"); print(L[L.px <= .12].head(16)[["race", "leg", "p", "px", "mult", "ev", "sz", "cash"]].round(3).to_string(index=False))
print("\nhow much does the discount cost a punt: to multiply money by M with one leg on SIG")
for M in (2, 3, 4, 5, 8, 10, 15, 20):
    px = 1 / M; c = R[(R.px >= px * .85) & (R.px <= px * 1.15)]
    print(f"{M:3d}x: legs {len(c):3d}, SIG price about {px:.3f}, outside chance mean {c.p.mean():.3f}, best {c.p.max() if len(c) else 0:.3f}, chance needed for fair {px:.3f}")
