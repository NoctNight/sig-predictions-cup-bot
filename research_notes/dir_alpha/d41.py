"""Drill-down 41: is House D special or just on the market maker's compression line? Fit SIG mid - .5 = k (outside - .5) across non-chamber legs, by day, and show where each chamber leg sits."""
import sqlite3, numpy as np, pandas as pd
pd.set_option("display.width", 200, "display.max_rows", 100)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
rows = []
for day in ("2026-10-22", "2026-10-24", "2026-10-26", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31"):
    T = pd.Timestamp(day + " 03:30", tz="UTC").timestamp()
    t = db.execute("select min(ts) from venue_fair where ts>=?", (T,)).fetchone()[0]
    v = pd.read_sql_query("select race,party,exchange_id ex,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where ts=? and exchange_id is not null", db, params=(t,))
    v["mid"] = (v.sig_bid + v.sig_ask) / 2
    v = v[np.isfinite(v.fair) & np.isfinite(v.mid)]
    x = v[~v.race.isin(["U.S. House", "U.S. Senate"]) & (v.party != "I")]
    res = {}
    for lab, g in (("fav .85-.97", x[(x.fair >= .85) & (x.fair <= .97)]), ("mid .3-.7", x[(x.fair >= .3) & (x.fair <= .7)])):
        k = np.sum((g.mid - .5) * (g.fair - .5)) / np.sum((g.fair - .5) ** 2)
        res[lab] = k
    k = np.sum((x.mid - .5) * (x.fair - .5)) / np.sum((x.fair - .5) ** 2)
    sd = (x.mid - .5 - k * (x.fair - .5)).std()
    r = dict(day=day[5:], k=round(k, 3), k_fav=round(res["fav .85-.97"], 3), k_mid=round(res["mid .3-.7"], 3), resid_sd_c=round(100 * sd, 2))
    for ex, nm in (("840", "HD"), ("842", "SD")):
        q = v[v.ex == ex].iloc[0]
        kk = res["fav .85-.97"] if ex == "840" else res["mid .3-.7"]
        r[nm + "_mid"] = round(q.mid, 3); r[nm + "_fair"] = round(q.fair, 3); r[nm + "_line"] = round(.5 + kk * (q.fair - .5), 3); r[nm + "_off_c"] = round(100 * (q.mid - (.5 + kk * (q.fair - .5))), 1)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))
g = x[(x.fair >= .90) & (x.fair <= .96)].copy(); g["gap"] = 100 * (g.fair - g.mid)
print("legs with outside 90-96c now: n", len(g), "mean gap %.1fc, sd %.1fc, range %.1f to %.1f" % (g.gap.mean(), g.gap.std(), g.gap.min(), g.gap.max()))
print(g.sort_values("gap", ascending=False)[["race", "party", "fair", "mid", "gap"]].head(10).round(3).to_string(index=False))
g = x[(x.fair >= .62) & (x.fair <= .78)].copy(); g["gap"] = 100 * (g.fair - g.mid)
print("legs with outside 62-78c now: n", len(g), "mean gap %.1fc, sd %.1fc, min %.1f" % (g.gap.mean(), g.gap.std(), g.gap.min()))
print(g.sort_values("gap")[["race", "party", "fair", "mid", "gap"]].head(8).round(3).to_string(index=False))
