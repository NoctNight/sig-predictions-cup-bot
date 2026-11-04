"""Candidates right now, read-only: forward test status scored by rule and age, open lag signals with the conviction count and the 70c filter, chamber gaps."""
import json, sqlite3, numpy as np, pandas as pd
pd.set_option("display.width", 200, "display.max_rows", 100, "display.max_columns", 30)
st = json.load(open("data/research_diralpha/paper_status.json"))
M = pd.DataFrame(st["marks"]); print("status", st["utc"], "entries", st["n"], "mean net now %.2fc" % st["mean_net_c"])
if len(M):
    M["age"] = pd.cut(M.hours, [-1, 2, 4, 8, 12, 99], labels=["0-2 h", "2-4 h", "4-8 h", "8-12 h", "12 h+"])
    print(M.groupby(["rule", "age"], observed=True).net_c.agg(n="size", net="mean", win=lambda v: (v > 0).mean()).round(2).to_string())
    print(M.groupby("rule").net_c.agg(n="size", net="mean", win=lambda v: (v > 0).mean()).round(2).to_string())
    print("not pre-existing"); print(M[~M.pre].groupby("rule").net_c.agg(n="size", net="mean").round(2).to_string())
fm = json.load(open("params/fair_model.json"))["races"]
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
ent = [json.loads(l) for l in open("data/research_diralpha/paper.jsonl")]
import time
now = time.time(); rows = []
for e in ent:
    if now - e["t"] > 12 * 3600: continue
    q = pd.read_sql_query("select ts,sig_bid,sig_ask,fair from venue_fair where exchange_id=? and ts>? order by ts", db, params=(e["ex"], e["t"] - 7300))
    if q.empty: continue
    m = (q.sig_bid + q.sig_ask) / 2; lc = q.ts[m.diff().fillna(0) != 0]
    pre = lc[lc <= e["t"]]; stale = (e["t"] - pre.iloc[-1]) / 3600 if len(pre) else 2.1
    p = fm.get(e["race"], {}).get("inputs", {}) or {}
    c = int(p.get("n_polls") == 0) + int(p.get("rating_margin") is not None and abs(p["rating_margin"]) <= 2) + int(stale >= 2) + int(e["sig"] >= (.0228 if e.get("rule") == "fast" else .0218))
    rows.append(dict(rule=e.get("rule", "slow"), race=e["race"], party=e["party"], side="YES" if e["s"] > 0 else "NO", at=e["utc"], px=round(e["px"], 3), own70=e["px"] >= .70, points=c, gap_c=round(100 * e["sig"], 1), bid=q.sig_bid.iloc[-1], ask=q.sig_ask.iloc[-1], fair=round(q.fair.iloc[-1], 3)))
print(pd.DataFrame(rows).sort_values(["points", "gap_c"], ascending=False).to_string(index=False))
c = pd.read_sql_query("select race,party,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where ts=(select max(ts) from venue_fair) and race in ('U.S. House','U.S. Senate')", db); print(c.round(3).to_string(index=False))
