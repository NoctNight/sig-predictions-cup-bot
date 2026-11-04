"""Round 10: remaining generated ideas. Book snapshots (A8, A9), touch depletion (A25), attention premium (A30), topical news (A29, A36, A40),
fund-the-punt selling (A15 variant), plus second-order ideas on the lag family: gap accelerating, and the lag on the OTHER leg of the race."""
import sys, glob, json, re, sqlite3
import numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, survivors, show, H
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
meta = L.meta
empty = lambda: pd.DataFrame(np.nan, index=mid.index, columns=mid.columns)
sig = {}
# A8, A9 from book snapshots: only valid at snapshot times
files = sorted(glob.glob("data/books_history/*.json"))
A8 = empty(); A9 = empty()
for f in files:
    tag = f.split("_")[-1].replace(".json", "")
    ts = pd.Timestamp(tag[:4] + "-" + tag[4:6] + "-" + tag[6:8] + " " + tag[9:11] + ":" + tag[11:13], tz="UTC").timestamp()
    b = int(ts // 300 * 300) + 300
    if b not in A8.index: continue
    d = json.load(open(f))
    d = d.get("books", d)
    for ex, bk in d.items():
        if ex not in A8.columns or not isinstance(bk, dict) or not bk.get("bids") or not bk.get("asks"): continue
        bs = sorted(bk["bids"], key=lambda x: -x["price"]); az = sorted(bk["asks"], key=lambda x: x["price"])
        tb, ta = bs[0]["price"], az[0]["price"]
        db = sum(x["quantity"] for x in bs if tb - x["price"] < .0251); da = sum(x["quantity"] for x in az if x["price"] - ta < .0251)
        A8.at[b, ex] = (db - da) / max(db + da, 1)
        hb = next((x["price"] for x in bs if x["quantity"] >= 20000), None); ha = next((x["price"] for x in az if x["quantity"] >= 20000), None)
        if hb and ha:
            A9.at[b, ex] = (hb + ha) / 2 - (tb + ta) / 2
print("snapshot signals", A8.notna().sum().sum(), A9.notna().sum().sum(), flush=True)
sig["A8depth"] = A8; sig["A8depthfade"] = -A8; sig["A9heavy"] = A9.where(A9.abs() >= .01); sig["A9heavyfade"] = -sig["A9heavy"]
bq, aq = P["bid_qty"], P["ask_qty"]
rb = bq.rolling(72, min_periods=24).median() / bq.rolling(1440, min_periods=288).median()
ra = aq.rolling(72, min_periods=24).median() / aq.rolling(1440, min_periods=288).median()
sig["A25depletion"] = (rb - ra).where((rb - ra).abs() >= .2)
sig["A25depletionfade"] = -sig["A25depletion"]
n24 = L.rsum(P["notional"], 288)
share = n24.div(n24.sum(axis=1), axis=0)
base = share.rolling(1440, min_periods=288).mean()
sig["A30attention"] = -(share / base).where(share / base >= 2) * np.sign(gap)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
print([x[1] for x in db.execute("pragma table_info(news_items)")][:14])
ne = pd.read_sql_query("select ts,race,party,exchange_id,direction,headline from news_events where exchange_id is not null", db)
for nm, rx in (("A36triage", r"cancel|pull|ad spend|reservation|triage"), ("A29early", r"early vot|mail ballot|turnout"), ("A40rating", r"rating|toss.?up|moves to|shifts to")):
    x = ne[ne.headline.fillna("").str.contains(rx, case=False, regex=True)]
    S_ = empty()
    for r in x.itertuples():
        b = int(r.ts // 300 * 300) + 300
        d = {"up": 1.0, "down": -1.0}.get(str(r.direction).lower(), 0.0)
        if d and b in S_.index and r.exchange_id in S_.columns: S_.at[b, r.exchange_id] = d
    print(nm, len(x), flush=True)
    sig[nm] = S_; sig[nm + "fade"] = -S_
S48 = gap - gap.rolling(576, min_periods=288).median()
sig["C1accel"] = S48.where((S48.abs() >= .015) & (L.lag(S48, 24) * np.sign(S48) >= .01))
sig["C2fading"] = S48.where((S48.abs() >= .015) & (L.lag(S48, 24) * np.sign(S48) <= -.005))
sig["C3old"] = S48.where(S48.abs().rolling(144, min_periods=144).min() >= .015)
sig["C4new"] = S48.where((S48.abs() >= .02) & (S48.abs().shift(24) < .01))
fav = meta.party.map({"D": 1, "R": -1}).fillna(0)
rows = []
for k, s in sig.items():
    rows.append(L.test(k, s, hs=("4h", "12h", "24h"), qs=(0.0, .8), offices=(None, "house"), family=k.split(":")[0]))
A = pd.concat(rows, ignore_index=True); A.to_pickle("data/research_diralpha/r10.pkl")
print("tests", L.n_tests, "signals", len(sig))
Sv = survivors(A, min_n=12); print("survivors", len(Sv)); show(Sv, 25)
print("best:"); show(A[A.get("test_n", 0) >= 10].sort_values("test_net_c", ascending=False), 16)
print(A.groupby(["family", "h"]).test_net_c.median().unstack().round(2).to_string())
