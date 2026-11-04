"""Drill-down 40: favourite carry into the call. Legs in states closing by 20:00 ET, our side 90c+ outside, 5%+ return at the touch, venues within 4c.
How fast are safe favourites called in early states (race_calls.csv), and what could a $40k D/R balanced book earn by about 21:00 ET under three repricing cases."""
import json, numpy as np, pandas as pd
pd.set_option("display.width", 220, "display.max_rows", 100, "display.max_columns", 30)
c = pd.read_csv("data/election_night/race_calls.csv"); print(c.columns.tolist()); print(c.head(3).to_string())
R = pd.read_pickle("data/research_diralpha/scan.pkl")
pc = json.load(open("data/election_night/poll_close_2026.json"))
from sigbot.venues.states import parse_race
R["our_p"] = np.where(R.side == "buy YES", R.fair, 1 - R.fair); R["our_px"] = np.where(R.side == "buy YES", R.touch, 1 - R.touch)
R["ret"] = (R.our_p - R.our_px) / R.our_px
R["close"] = [(pc.get(parse_race(r).get("abbr")) or {}).get("first_close_et") for r in R.race]
R["dside"] = np.where(((R.party == "D") & (R.side == "buy YES")) | ((R.party == "R") & (R.side == "sell YES")), "D", np.where(R.party == "I", "I", "R"))
Q = R[(R.dis.fillna(0) < .04) & (R.our_p >= .90) & (R.ret >= .05) & R.close.isin(["18:00", "19:00", "19:30", "20:00"])].sort_values("ret", ascending=False)
print(Q[["race", "party", "side", "dside", "our_p", "our_px", "ret", "shares", "cash", "edge_usd", "close"]].round(3).to_string(index=False))
print(Q.groupby("dside")[["cash", "edge_usd"]].sum())
for lab, sell in (("sell at 97c after the call", .97), ("sell at 99c after the call", .99), ("no reprice before settlement, touch stays", None)):
    v = 0; cash = 0
    for d in ("D", "R"):
        g = Q[Q.dside == d].head(5); w = 20000 / len(g)
        for r in g.itertuples():
            sh = w / r.our_px; cash += w
            if sell is None: continue
            v += sh * (r.our_p * sell - r.our_px)      # the upset loses the stake
    print(f"{lab}: on ${cash:.0f}, expected ${v:.0f} ({100*v/cash:.1f}%)")
