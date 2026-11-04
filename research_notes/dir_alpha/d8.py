"""Drill-down 8: fast version, the actual high-frequency punt. Enter as soon as both venues have moved in a short window and SIG has not fully
copied. Uses raw polls (about 81 s), not the 5-minute panel. Entry is the NEXT poll's touch. Exit at the touch after a fixed time."""
import sqlite3, numpy as np, pandas as pd, sys
pd.set_option("display.width", 200, "display.max_rows", 300, "display.max_columns", 40)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
df = pd.read_sql_query("select ts,race,party,pm,kalshi,fair,sig_bid,sig_ask,exchange_id from venue_fair where exchange_id is not null and sig_bid>0 and sig_ask>sig_bid order by ts", db)
meta = pd.read_pickle("data/research_diralpha/meta.pkl")
print(len(df), flush=True)
SPLIT = df.ts.min() + .5 * (df.ts.max() - df.ts.min())
rows = []
WINS = [300, 900, 1800, 3600]
HOLDS = [900, 1800, 3600, 7200, 14400, 28800, 86400]
for ex, g in df.groupby("exchange_id"):
    if ex not in meta.index or meta.office[ex] == "chamber": continue
    t = g.ts.values; pm = g.pm.values; k = g.kalshi.values; f = g.fair.values; b = g.sig_bid.values; a = g.sig_ask.values; m = (a + b) / 2
    n = len(t)
    if n < 500: continue
    for W in WINS:
        i0 = np.searchsorted(t, t - W)                # first poll inside the window
        dpm = pm - pm[i0]; dk = k - k[i0]; dm = m - m[i0]; dfv = f - f[i0]
        for i in range(n - 1):
            if t[i] - t[i0[i]] < .5 * W or t[i] - t[i0[i]] > 2 * W: continue
            s = np.sign(dfv[i])
            if s == 0: continue
            if not (s * dpm[i] >= .01 and s * dk[i] >= .01 and s * dfv[i] >= .015): continue
            if s * dm[i] > .6 * s * dfv[i]: continue            # SIG has already copied most of it
            if not (.1 <= m[i] <= .9): continue
            e = i + 1
            if t[e] - t[i] > 180: continue
            r = dict(ex=ex, W=W, t=t[e], s=s, dfv=s * dfv[i], dm=s * dm[i], m=m[i], spr=a[e] - b[e], office=meta.office[ex], race=meta.race[ex])
            px = a[e] if s > 0 else 1 - b[e]
            for h in HOLDS:
                x = np.searchsorted(t, t[e] + h)
                if x < n and t[x] - (t[e] + h) < 600:
                    v = b[x] if s > 0 else 1 - a[x]
                    r[f"h{h}"] = v - px
                    r[f"o{h}"] = s * (f[x] - f[e])
            rows.append(r)
E = pd.DataFrame(rows)
print("raw", len(E), flush=True)
E = E.sort_values("t")
# one trigger per leg per window size per 4 h
E["bk"] = (E.t // 14400)
E = E.drop_duplicates(["ex", "W", "bk"])
E["train"] = E.t < SPLIT
E["day"] = pd.to_datetime(E.t, unit="s").dt.strftime("%m-%d")
E["gapleft"] = E.dfv - E.dm
E.to_pickle("data/research_diralpha/fast_events.pkl")
cols = [f"h{h}" for h in HOLDS]


def t_(v, g):
    x = pd.Series(v.values).groupby(np.asarray(g)).mean(); return x.mean() / (x.std() / np.sqrt(len(x))) if len(x) > 2 and x.std() > 0 else np.nan


def show(x, name):
    r = {"group": name, "n": len(x), "races": x.race.nunique()}
    for c in cols:
        v = x[c].dropna()
        r[c.replace("h", "") + "s"] = 100 * v.mean() if len(v) else np.nan
    v = x["h3600"].dropna()
    r["win1h"] = (v > 0).mean() if len(v) else np.nan
    v2 = x["h14400"].dropna()
    r["t_day4h"] = t_(x["h14400"].dropna(), x.loc[x["h14400"].notna(), "day"]) if len(v2) > 4 else np.nan
    return r


R = []
for W in WINS:
    x = E[E.W == W]
    R.append(show(x, f"window {W//60} min"))
    R.append(show(x[x.train], f"  training days")); R.append(show(x[~x.train], f"  test days"))
    for o in ("house", "governor", "senate"):
        R.append(show(x[x.office == o], f"  {o}"))
    R.append(show(x[x.gapleft >= .02], "  gap left >= 2c")); R.append(show(x[x.gapleft >= .03], "  gap left >= 3c")); R.append(show(x[x.dfv >= .03], "  outside move >= 3c"))
    R.append(show(x[(x.gapleft >= .02) & (x.office == "house")], "  house, gap left >= 2c"))
print(pd.DataFrame(R).round(2).to_string(index=False))
