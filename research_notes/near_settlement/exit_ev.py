"""What the 90-92c favourites earn sold on 2 Nov, sold at noon on 3 Nov, or held, per share after spread and the walk down the bids.
Now is T-3. 2 Nov 18:00 ET is 48 h away, 3 Nov 12:00 ET is 66 h. Closure rates are SIG's own; the speed-up is the ratio measured on Polymarket-Kalshi gaps."""
import json, pandas as pd
OUT = "data/research_near_settlement"
L = pd.read_pickle(f"{OUT}/live_book.pkl")
B = L[(L.ask >= .899) & (L.ask <= .921) & (L.edge >= .039)].copy()
if len(B) < 8:
    B = L[L.edge >= .039].copy()
B["mid"] = (B.ask + B.bid) / 2
ask, mid, f = B.ask.mean(), B.mid.mean(), B.fair.mean()
g, spr_now = f - mid, B.spr.mean()
import sqlite3
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
t = db.execute("select max(ts) from venue_fair").fetchone()[0]
ids = ",".join(f"'{x}'" for x in B.ex)
sp48 = db.execute(f"select avg(sig_ask-sig_bid) from venue_fair where ts>{t-172800} and exchange_id in ({ids}) and sig_ask-sig_bid<.1").fetchone()[0]
spr = (spr_now + sp48) / 2       # entry pays today's spread (half of it), the exit pays a usual one: picking wide gaps also picks wide spreads at the entry moment
walk = B.w1200.mean(skipna=True)
H = pd.read_pickle(f"{OUT}/sig_horizons.pkl").set_index(["sample", "h"])
sp = json.load(open(f"{OUT}/q3_speedup_election.json"))
print(f"live band: {len(B)} sides, ask {ask:.3f} mid {mid:.3f} fair {f:.3f} gap to mid {100*g:.1f}c spread now {100*spr_now:.2f}c, 48h avg {100*sp48:.2f}c, cost used {100*spr:.2f}c walk for 1,200 shares {100*walk:.2f}c")
c = lambda s, h: H.loc[(s, h), "close"] / 100
base = [("SIG's rate since 24 Oct, no speed-up", c("from 24 Oct", 48), c("from 24 Oct", 66), 1.0),
        ("SIG's rate since 24 Oct, history's speed-up", c("from 24 Oct", 48), c("from 24 Oct", 66), sp["ratio"]),
        ("SIG's rate since 21 Oct, history's speed-up", c("all entries", 48), c("all entries", 66), sp["ratio"]),
        ("SIG's rate since 21 Oct, top of the speed-up", c("all entries", 48), c("all entries", 66), sp["hi95"]),
        ("SIG closes like real-money venues (30% a day)", 1 - .7 ** 2, 1 - .7 ** 2.75, 1.0)]
rows = []
for name, c48, c66, m in base:
    a, b = c48 * m, c66 * m
    rows.append(dict(scenario=name, close_2nov=100 * a, close_3nov=100 * b, sold_2nov=100 * (a * g - spr - walk), sold_3nov=100 * (b * g - spr - walk)))
S = pd.DataFrame(rows)
n = 21000 / ask
S["usd_2nov"], S["usd_3nov"] = S.sold_2nov / 100 * n, S.sold_3nov / 100 * n
S.to_pickle(f"{OUT}/exit_scenarios.pkl")
print(S.round(2).to_string(index=False))
q = {"outside fair right": f, "fair 2c too high": f - .02, "favourites all win at past rate (99.5c)": .995}
H_ = pd.DataFrame([dict(case=k, q=100 * v, per_share=100 * (v - ask), usd=(v - ask) * n) for k, v in q.items()])
H_.to_pickle(f"{OUT}/exit_hold.pkl")
print(H_.round(2).to_string(index=False))
be = (spr + walk) / g
print(f"closure needed to break even on a sale: {100*be:.0f}% of the gap")
w6 = H.loc[("from 24 Oct, gap 6c+", 48)], H.loc[("from 24 Oct, gap 6c+", 66)]
print("realised on SIG, 6c+ gaps since 24 Oct: net", round(w6[0].net, 2), [round(w6[0].net_lo, 2), round(w6[0].net_hi, 2)], "at 48 h,", round(w6[1].net, 2), [round(w6[1].net_lo, 2), round(w6[1].net_hi, 2)], "at 66 h")
print(f"sale at the 2 Nov central case nets {100*(c('from 24 Oct',48)*sp['ratio']*g-spr-walk):+.2f}c; cash multiple on punt money that makes selling equal to holding: {f/(ask+c('from 24 Oct',48)*sp['ratio']*g-spr-walk):.3f}")
print(f"win rate below which a 2 Nov sale beats holding (central): {100*(ask+c('from 24 Oct',48)*sp['ratio']*g-spr-walk):.1f}%, against fair {100*f:.1f}%")
json.dump(dict(ask=ask, mid=mid, fair=f, gap=g, spread=spr, walk=walk, n=n, sides=len(B), breakeven=be), open(f"{OUT}/exit_inputs.json", "w"), indent=1)
