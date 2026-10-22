"""Per-race third-party probability x from live outside markets (Polymarket, Kalshi).

For each SIG race without its own Independent market, x = probability that neither the Democrat nor the Republican
wins. The outside venues list the serious non-major candidates in a race (e.g. Idaho Senate: Todd Achilles); we take,
per venue, the sum over non-D/R candidates of their ASK (the cost of buying insurance, i.e. the cautious side), then
the larger of the two venues, floored at `floor` for write-ins and freak results. Races where the outside venues list
no non-major candidate get the floor. Races with no outside quote at all fall back to the per-state historical rate
(params/x.json by_race) and then the office rate.

Reads the latest quote per instrument from the `venue_quotes` table (written by sigbot.direction) and writes
params/x_live.json: {"generated_at", "max_quote_age_h", "by_race": {race: {"x", "pm", "kalshi", "source", "age_h"}}}.

    python3 -m sigbot.xlive            # rebuild from the latest logged quotes
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "params", "x_live.json")


def build(db_path: str, floor: float = 0.003, max_age_h: float = 336.0) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        WITH last AS (SELECT venue, instrument, MAX(ts) ts FROM venue_quotes WHERE venue IN ('polymarket','kalshi') GROUP BY 1,2)
        SELECT q.race, q.venue, q.party, q.bid, q.ask, q.mid, q.ts FROM venue_quotes q
        JOIN last l ON q.venue=l.venue AND q.instrument=l.instrument AND q.ts=l.ts""").fetchall()
    now = time.time()
    agg: dict = {}
    for r in rows:
        if now - r["ts"] > max_age_h * 3600:
            continue
        a = agg.setdefault(r["race"], {"polymarket": {"other": 0.0, "seen": False, "ts": 0}, "kalshi": {"other": 0.0, "seen": False, "ts": 0}})
        v = a[r["venue"]]
        v["seen"] = True
        v["ts"] = max(v["ts"], r["ts"])
        if r["party"] not in ("D", "R"):
            px = r["ask"] if r["ask"] is not None else r["mid"]
            if px is not None:
                v["other"] += float(px)
    out = {}
    for race, a in agg.items():
        vals = {k: round(v["other"], 4) for k, v in a.items() if v["seen"]}
        if not vals:
            continue
        x = max(floor, max(vals.values()))
        age = (now - max(v["ts"] for v in a.values() if v["seen"])) / 3600
        out[race] = {"x": round(x, 4), "pm": vals.get("polymarket"), "kalshi": vals.get("kalshi"), "source": "outside", "age_h": round(age, 2)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "floor": floor, "max_quote_age_h": max_age_h, "by_race": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "data", "sigbot.sqlite"))
    ap.add_argument("--floor", type=float, default=0.003)
    ap.add_argument("--max-age-hours", type=float, default=336.0)
    args = ap.parse_args()
    res = build(args.db, args.floor, args.max_age_hours)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(res, f, indent=1)
    br = res["by_race"]
    print(f"wrote {OUT}: {len(br)} races from outside quotes")
    for race, v in sorted(br.items(), key=lambda kv: -kv[1]["x"])[:12]:
        print(f"  {race:26} x={100 * v['x']:.2f}%  pm={v['pm']}  kalshi={v['kalshi']}  age={v['age_h']}h")
    print("  at floor:", sum(1 for v in br.values() if v["x"] <= res["floor"] + 1e-9))


if __name__ == "__main__":
    main()
