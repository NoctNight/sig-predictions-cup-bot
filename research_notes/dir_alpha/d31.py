"""Drill-down 31: the House-Senate digital. Long House D + short Senate D = +1 if D House and R Senate, -1 if R House and D Senate, 0 otherwise.
 A the spread on SIG, outside and Kalshi's own DR leg since 21 Oct (does it close?)
 B who moved it: prints of 20k+ since 27 Oct
 C cost to enter from the book, by size
 D scenario table by size and route, cap use, cash used"""
import json, sqlite3, numpy as np, pandas as pd
pd.set_option("display.width", 220, "display.max_rows", 300, "display.max_columns", 30)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
v = pd.read_sql_query("select ts,exchange_id ex,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where exchange_id in ('840','842')", db)
v["mid"] = (v.sig_bid + v.sig_ask) / 2
v["b"] = (v.ts // 1800).astype("int64") * 1800
p = v.sort_values("ts").groupby(["b", "ex"]).last().unstack("ex")
S = pd.DataFrame(dict(sig=p["mid"]["840"] - p["mid"]["842"], out=p["fair"]["840"] - p["fair"]["842"], pm=p["pm"]["840"] - p["pm"]["842"], k=p["kalshi"]["840"] - p["kalshi"]["842"]))
S["gap"] = S.out - S.sig
S.index = pd.to_datetime(S.index, unit="s", utc=True)
S.to_pickle("data/research_diralpha/chamber_spread.pkl")
print((S.iloc[::24] * 100).round(1).to_string())
g = S.gap.dropna()
print("gap now %.1fc, median since start %.1fc, share of time >=8c %.2f, >=4c %.2f" % (100 * g.iloc[-1], 100 * g.median(), (g >= .08).mean(), (g >= .04).mean()))
for h in (24, 48, 96):
    x = g.values[:-h]; y = g.values[h:] - x
    b = np.polyfit(x, y, 1)
    print(f"gap change over next {h//2} h on gap: slope {b[0]:.2f} (a slope of -1 means fully closes), intercept {100*b[1]:.2f}c, independent stretches about {len(x)//h}")
print("\nprints of 20k+ since 27 Oct")
tp = pd.read_sql_query("select exchange_id ex,price,quantity q,aggressor a,executed_at from tape where exchange_id in ('840','841','842','843') and executed_at>='2026-10-27'", db)
tp["t"] = pd.to_datetime(tp.executed_at, format="ISO8601", utc=True).dt.strftime("%m-%d %H:%M:%S"); tp["q"] = tp.q.abs()
big = tp[tp.q >= 20000].sort_values("t"); print(big[["t", "ex", "price", "q", "a"]].to_string(index=False))
print("\nladder cost (books %s)" % json.load(open("data/books_latest.meta.json"))["utc"])
B = json.load(open("data/books_latest.json"))
def walk(levels, q, sign):
    # levels sorted best first
    got = 0; cost = 0
    for l in levels:
        t = min(q - got, l["quantity"]); cost += t * l["price"]; got += t
        if got >= q: break
    return (cost / got if got else np.nan), got, (l["price"] if got else np.nan)
for ex in ("840", "841", "842", "843"):
    b = sorted(B[ex]["bids"], key=lambda x: -x["price"]); a = sorted(B[ex]["asks"], key=lambda x: x["price"])
    print(ex, "bids", [(l["price"], int(l["quantity"])) for l in b[:8]]); print(ex, "asks", [(l["price"], int(l["quantity"])) for l in a[:8]])
print("\nsize   House D avg/last   Senate D sell avg/last   digital cost")
rows = []
for q in (5000, 10000, 20000, 40000, 60000, 100000, 150000, 200000):
    a0, g0, l0 = walk(sorted(B["840"]["asks"], key=lambda x: x["price"]), q, 1)
    a1, g1, l1 = walk(sorted(B["842"]["bids"], key=lambda x: -x["price"]), q, -1)
    # alternative House: sell House R (buy NO) at its bids
    a2, g2, l2 = walk(sorted(B["841"]["bids"], key=lambda x: -x["price"]), q, -1)
    # alternative Senate: buy Senate R yes
    a3, g3, l3 = walk(sorted(B["843"]["asks"], key=lambda x: x["price"]), q, 1)
    rows.append(dict(q=q, hD_avg=a0, hD_last=l0, hD_got=g0, sD_bid_avg=a1, sD_last=l1, sD_got=g1, hR_bid_avg=a2, hR_got=g2, sR_ask_avg=a3, sR_got=g3, digital=min(a0, 1 - a2) - a1))
R = pd.DataFrame(rows); print(R.round(4).to_string(index=False))
R.to_pickle("data/research_diralpha/chamber_ladder.pkl")
