"""Drill-down 39: where capital earns most if held to the result. Book 03:56 UTC, every level 2c+ better than outside, one line per leg and side.
Return = edge / cash. Winner chance = outside price of our side. Cash back: poll close of the state (early = by 21:00 ET). Excludes legs where venues differ by 4c+ and our side under 50c."""
import json, numpy as np, pandas as pd
pd.set_option("display.width", 220, "display.max_rows", 200, "display.max_columns", 30)
R = pd.read_pickle("data/research_diralpha/scan.pkl")
pc = json.load(open("data/election_night/poll_close_2026.json"))
from sigbot.venues.states import parse_race as race_info
def close(r):
    i = race_info(r); ab = i.get("abbr")
    v = (pc.get(ab) or {}).get("first_close_et")
    return v
R["our_p"] = np.where(R.side == "buy YES", R.fair, 1 - R.fair)
R["our_px"] = np.where(R.side == "buy YES", R.touch, 1 - R.touch)
R["touch_ret"] = (R.our_p - R.our_px) / R.our_px
Q = R[(R.dis.fillna(0) < .04) & (R.our_p >= .5)].copy()
Q["dside"] = np.where(((Q.party == "D") & (Q.side == "buy YES")) | ((Q.party == "R") & (Q.side == "sell YES")), "D", np.where(Q.party == "I", "I", "R"))
Q["close"] = [close(r) for r in Q.race]
print(Q.sort_values("edge_usd", ascending=False).head(26)[["race", "party", "side", "dside", "fair", "touch", "touch_edge_c", "touch_ret", "shares", "cash", "edge_usd", "close"]].round(3).to_string(index=False))
for d, g in Q.groupby("dside"):
    g = g[g.touch_ret >= .05]; print(d, "legs at 5%+:", len(g), "cash", int(g.cash.sum()), "edge", int(g.edge_usd.sum()))
