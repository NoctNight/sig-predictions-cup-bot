"""P&L review by sleeve: marked value vs settlement value, recent fills, markouts, warnings.

    python3 -m sigbot.review [--minutes 15] [--json]

Uses 2 API reads (positions, pnl). Everything else comes from data/sigbot.sqlite and data/live.log.

Definitions
  marked P&L      market value at the venue's marks minus cost basis (what the leaderboard shows today)
  settlement P&L  for arb sets (NO on every leg of a race, equal quantities): (legs - 1) x sets minus cost basis,
                  i.e. the profit locked in regardless of who wins; for other positions: marked P&L
  markout         mid N minutes after a fill minus the fill price, signed in our favour, in ticks (0.005)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK = 0.005
TITLE_RE = re.compile(r"Will the (Democratic|Republican|Independent) Party win the (.+)\?")


def api(path: str) -> dict:
    key = open(os.path.join(HERE, ".sig_api_key")).read().strip()
    r = subprocess.run(["curl", "-s", "https://sig.thesuper.market/api/v1" + path, "-H", "Authorization: Bearer " + key],
                       capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout)


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    now = time.time()
    db = sqlite3.connect(f"file:{os.path.join(HERE, 'data/sigbot.sqlite')}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    markets = json.load(open(os.path.join(HERE, "data/markets.json")))
    leg = {}
    for m in markets:
        mt = TITLE_RE.match(m["title"])
        if mt:
            leg[str(m["exchanges"][0]["id"])] = (mt.group(2), mt.group(1)[0])
    pos = api("/tournaments/midterm-elections/portfolio/positions")
    pnl = api("/tournaments/midterm-elections/portfolio/pnl?period=all")
    P = {str(p["exchangeId"]): p for p in pos.get("positions", []) if not p.get("settled")}

    # sleeve of each exchange = sleeve with most filled shares there
    sl = defaultdict(lambda: defaultdict(float))
    for r in db.execute("SELECT exchange_id, COALESCE(purpose,'?') p, SUM(ABS(quantity)) q FROM fills GROUP BY 1,2"):
        sl[r["exchange_id"]][r["p"]] += r["q"]
    sleeve_of = {ex: max(v, key=v.get) for ex, v in sl.items()}

    # arb sets: races where every leg is short (NO) -> common quantity is a set
    by_race = defaultdict(dict)
    for ex, p in P.items():
        if ex in leg:
            by_race[leg[ex][0]][ex] = p
    out = {"ts": datetime.now(timezone.utc).isoformat(), "account": {k: pnl.get(k) for k in ("totalAccountValue", "totalHoldingsValue", "totalCostBasis", "unrealizedPnl")}}
    sleeves = defaultdict(lambda: {"cost": 0.0, "marked": 0.0, "settle": 0.0, "n": 0})
    sets = []
    # buy-sets (YES on every leg, pays 1) count at payout too, but only in races the arb engine really bought them:
    # without this, recycling a sell-set into a buy-set showed as a settlement loss (23 Oct: RI -> Hawaii Governor)
    try:
        buy_races = {r[0] for r in db.execute("SELECT DISTINCT race FROM arb_trades WHERE ok=1 AND kind='buy_set'")}
    except Exception:
        buy_races = set()
    for race, legs in by_race.items():
        n_legs = sum(1 for e in leg.values() if e[0] == race)
        qs = [p["quantity"] for p in legs.values()]
        sell = all(q < 0 for q in qs)
        buy = race in buy_races and all(q > 0 for q in qs)
        if len(legs) == n_legs and n_legs >= 2 and (sell or buy):
            k = min(abs(q) for q in qs)
            cost_set = sum(p["costBasis"] * (k / abs(p["quantity"])) for p in legs.values())
            mark_set = sum(p["marketValue"] * (k / abs(p["quantity"])) for p in legs.values())
            locked = ((n_legs - 1) if sell else 1) * k - cost_set
            sets.append({"race": race, "sets": k, "cost": round(cost_set, 1), "marked_pnl": round(mark_set - cost_set, 1), "locked_profit": round(locked, 1)})
            s = sleeves["arb_sets"]; s["cost"] += cost_set; s["marked"] += mark_set - cost_set; s["settle"] += locked; s["n"] += 1
            for ex, p in legs.items():           # leftover beyond the common quantity is ordinary inventory
                extra = abs(p["quantity"]) - k
                if extra > 0:
                    frac = extra / abs(p["quantity"])
                    s2 = sleeves[sleeve_of.get(ex, "?") + "_residual"]
                    s2["cost"] += p["costBasis"] * frac; s2["marked"] += (p["marketValue"] - p["costBasis"]) * frac
                    s2["settle"] += (p["marketValue"] - p["costBasis"]) * frac; s2["n"] += 1
            continue
        for ex, p in legs.items():
            s = sleeves[sleeve_of.get(ex, "?")]
            u = p["marketValue"] - p["costBasis"]
            s["cost"] += p["costBasis"]; s["marked"] += u; s["settle"] += u; s["n"] += 1
    out["sleeves"] = {k: {kk: round(vv, 1) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in sleeves.items()}
    out["arb_sets"] = sorted(sets, key=lambda x: -x["locked_profit"])
    tv = pnl.get("totalAccountValue") or 0.0
    out["settlement_value"] = round(tv - sum(v["marked"] for v in sleeves.values()) + sum(v["settle"] for v in sleeves.values()), 1)

    # recent fills and markouts
    since = now - args.minutes * 60
    rec = defaultdict(lambda: [0, 0.0])
    for r in db.execute("SELECT purpose, quantity, filled_at FROM fills"):
        if _ts(r["filled_at"]) >= since:
            rec[r["purpose"] or "?"][0] += 1; rec[r["purpose"] or "?"][1] += abs(r["quantity"])
    out["fills_window"] = {k: {"n": v[0], "shares": v[1]} for k, v in rec.items()}
    mk = defaultdict(list)
    for f in db.execute("SELECT exchange_id, side, price, filled_at, purpose FROM fills WHERE purpose IN ('mm','tilt','inside','direction') AND price IS NOT NULL"):
        t = _ts(f["filled_at"])
        for h, secs in (("5m", 300), ("30m", 1800)):
            if now - t < secs:
                continue
            q = db.execute("SELECT mid FROM quotes WHERE exchange_id=? AND ts>=? ORDER BY ts ASC LIMIT 1", (f["exchange_id"], t + secs)).fetchone()
            ref = q["mid"] if q and q["mid"] is not None else None
            if ref is None:   # markets the MM does not quote (inside quoter, lag follower): first public print after t + h
                iso = datetime.fromtimestamp(t + secs, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                tr = db.execute("SELECT price FROM tape WHERE exchange_id=? AND executed_at>=? ORDER BY executed_at ASC LIMIT 1", (f["exchange_id"], iso)).fetchone()
                ref = tr["price"] if tr else None
            if ref is not None:
                e = (ref - f["price"]) if f["side"] == "yes" else (f["price"] - ref)
                mk[(f["purpose"], h)].append(e / TICK)
    out["markouts_ticks"] = {f"{p}:{h}": {"n": len(v), "mean": round(sum(v) / len(v), 3)} for (p, h), v in mk.items()}

    # log warnings in the window
    warn = defaultdict(int)
    try:
        for line in open(os.path.join(HERE, "data/live.log"), errors="replace"):
            if line[:19] < datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M:%S"):
                continue
            for key in ("429", "Traceback", "ERROR", "KILL SWITCH", "volatility wave ON", "RECYCLE", "not filled", "partisan cap"):
                if key in line:
                    warn[key] += 1
    except FileNotFoundError:
        pass
    out["log_flags_window"] = dict(warn)
    last = db.execute("SELECT * FROM cycles ORDER BY cycle_id DESC LIMIT 1").fetchone()
    out["last_cycle_age_sec"] = round(now - last["ts"]) if last else None

    if args.json:
        print(json.dumps(out, indent=1))
        return
    a = out["account"]
    print(f"marked total {a['totalAccountValue']} | settlement-value total {out['settlement_value']} | last cycle {out['last_cycle_age_sec']}s ago")
    for k, v in out["sleeves"].items():
        print(f"  {k:16} cost {v['cost']:9.1f} marked {v['marked']:+8.1f} settle {v['settle']:+8.1f} n={v['n']}")
    print("  arb sets:", [(s['race'], s['sets'], s['locked_profit']) for s in out["arb_sets"]])
    print(f"  fills last {args.minutes:.0f}m:", out["fills_window"], "| markouts:", out["markouts_ticks"])
    print("  log flags:", out["log_flags_window"])


if __name__ == "__main__":
    main()
