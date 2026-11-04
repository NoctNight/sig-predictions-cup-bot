"""Forward paper test of the filtered lag signal on data that did not exist when the rule was found. Read-only, no orders.
Every 5 minutes: rebuild the signal from the last 3 days of venue_fair and tape, log any new entry at the touch, mark open ones.
Rule frozen 31 Oct 01:20 UTC: fresh gap (outside minus SIG, against its 48 h median) >= 2c, SIG mid 15-85, not a chamber leg,
both venues moved >= 1c that way in 24 h, signed taker flow in 24 h not below -5,000 shares against us, one bet per race per 24 h.
    python3 paper.py [stop_utc_iso]
Writes data/research_diralpha/paper.jsonl (entries) and paper_status.json (marks)."""
import json, os, sqlite3, sys, time
from datetime import datetime, timezone
import numpy as np, pandas as pd
OUT = "data/research_diralpha/"
STOP = datetime.fromisoformat(sys.argv[1]).timestamp() if len(sys.argv) > 1 else time.time() + 7 * 3600
BIN = 300
meta = pd.read_pickle(OUT + "meta.pkl")
ent = []
if os.path.exists(OUT + "paper.jsonl"):
    ent = [json.loads(l) for l in open(OUT + "paper.jsonl")]
START = time.time()
RESTART = bool(ent)


def cycle():
    now = time.time()
    db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
    t0 = now - 3.2 * 86400
    v = pd.read_sql_query("select ts,exchange_id,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where ts>? and exchange_id is not null", db, params=(t0,))
    iso = datetime.fromtimestamp(now - 24 * 3600 - 600, timezone.utc).strftime("%Y-%m-%dT%H:%M")
    tp = pd.read_sql_query("select exchange_id,quantity,aggressor,executed_at from tape where executed_at>=?", db, params=(iso,))
    db.close()
    v["b"] = (v.ts // BIN).astype("int64") * BIN
    g = v.sort_values("ts").groupby(["b", "exchange_id"]).last().reset_index()
    pv = {c: g.pivot(index="b", columns="exchange_id", values=c).ffill(limit=36) for c in ["pm", "kalshi", "fair", "sig_bid", "sig_ask"]}
    bid, ask = pv["sig_bid"], pv["sig_ask"]
    mid = ((bid + ask) / 2).where(ask > bid)
    gap = pv["fair"] - mid
    S = gap - gap.rolling(576, min_periods=288).median()
    last = S.index[-1]
    if now - last > 900:
        return dict(ok=False, why="stale data", age=now - last)
    tp["ts"] = (pd.to_datetime(tp.executed_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
    tp = tp[tp.ts >= now - 86400]
    tp["sq"] = tp.quantity.abs() * tp.aggressor.map({"buy": 1.0, "sell": -1.0}).fillna(0.0)
    fl = tp.groupby("exchange_id").sq.sum()
    row = S.iloc[-1]
    dpm = (pv["pm"].iloc[-1] - pv["pm"].iloc[-289]); dk = (pv["kalshi"].iloc[-1] - pv["kalshi"].iloc[-289])
    live = []
    for ex, s in row.items():
        if not np.isfinite(s) or abs(s) < .02 or ex not in meta.index or meta.office[ex] == "chamber": continue
        m = mid[ex].iloc[-1]
        if not (.15 <= m <= .85): continue
        sg = 1.0 if s > 0 else -1.0
        if not (dpm[ex] * sg >= .01 and dk[ex] * sg >= .01): continue
        if fl.get(ex, 0.0) * sg < -5000: continue
        live.append(dict(ex=ex, race=meta.race[ex], party=meta.party[ex], office=meta.office[ex], s=sg, sig=float(abs(s)), mid=float(m), bid=float(bid[ex].iloc[-1]), ask=float(ask[ex].iloc[-1]), fair=float(pv["fair"][ex].iloc[-1]), dpm=float(dpm[ex]), dk=float(dk[ex]), flow=float(fl.get(ex, 0.0))))
    for r in sorted(live, key=lambda x: -x["sig"]):
        if any(e.get("rule", "slow") == "slow" and e["race"] == r["race"] and now - e["t"] < 86400 for e in ent):
            continue
        r["t"] = now; r["utc"] = datetime.fromtimestamp(now, timezone.utc).strftime("%m-%d %H:%M"); r["px"] = r["ask"] if r["s"] > 0 else 1 - r["bid"]; r["pre_existing"] = not RESTART and now - START < 600
        ent.append(r)
        with open(OUT + "paper.jsonl", "a") as f:
            f.write(json.dumps(r) + "\n")
    # fast rule, frozen 31 Oct 01:32 UTC: both venues moved >= 1.5c in 30 min, SIG copied under half, mid 25-75, not chamber, one per race per 4 h
    if len(mid) > 7:
        f1, f0 = pv["fair"].iloc[-1], pv["fair"].iloc[-7]
        p1, p0 = pv["pm"].iloc[-1], pv["pm"].iloc[-7]; k1, k0 = pv["kalshi"].iloc[-1], pv["kalshi"].iloc[-7]
        m1, m0 = mid.iloc[-1], mid.iloc[-7]
        for ex in f1.index:
            if ex not in meta.index or meta.office[ex] == "chamber": continue
            df = f1[ex] - f0[ex]
            if not np.isfinite(df) or df == 0: continue
            sg = 1.0 if df > 0 else -1.0
            if not (sg * (p1[ex] - p0[ex]) >= .015 and sg * (k1[ex] - k0[ex]) >= .015): continue
            if not (.25 <= m1[ex] <= .75) or sg * (m1[ex] - m0[ex]) >= .5 * sg * df: continue
            if any(e.get("rule") == "fast" and e["race"] == meta.race[ex] and now - e["t"] < 14400 for e in ent): continue
            r = dict(rule="fast", ex=ex, race=meta.race[ex], party=meta.party[ex], office=meta.office[ex], s=sg, sig=float(abs(df)), mid=float(m1[ex]), bid=float(bid[ex].iloc[-1]), ask=float(ask[ex].iloc[-1]), fair=float(f1[ex]), t=now, utc=datetime.fromtimestamp(now, timezone.utc).strftime("%m-%d %H:%M"))
            r["px"] = r["ask"] if sg > 0 else 1 - r["bid"]
            ent.append(r)
            with open(OUT + "paper.jsonl", "a") as f:
                f.write(json.dumps(r) + "\n")
    marks = []
    for e in ent:
        ex = e["ex"]
        val = float(bid[ex].iloc[-1]) if e["s"] > 0 else 1 - float(ask[ex].iloc[-1])
        marks.append(dict(rule=e.get("rule", "slow"), ex=ex, race=e["race"], utc=e["utc"], s=e["s"], sig=e["sig"], px=e["px"], now=val, net_c=100 * (val - e["px"]), hours=(now - e["t"]) / 3600, pre=e.get("pre_existing", False), gap_now=float(row.get(ex, np.nan)) * e["s"]))
    st = dict(ok=True, utc=datetime.now(timezone.utc).strftime("%m-%d %H:%M"), live=live, n=len(ent), mean_net_c=float(np.mean([m["net_c"] for m in marks])) if marks else None, marks=marks)
    json.dump(st, open(OUT + "paper_status.json.tmp", "w"), indent=0); os.replace(OUT + "paper_status.json.tmp", OUT + "paper_status.json")
    return st


while time.time() < STOP:
    try:
        st = cycle()
        print(datetime.now(timezone.utc).strftime("%H:%M"), "ok" if st.get("ok") else st, "open", st.get("n"), "live", len(st.get("live", [])), "mean_net", st.get("mean_net_c"), flush=True)
    except Exception as e:
        print("error", repr(e)[:200], flush=True)
    time.sleep(300)
