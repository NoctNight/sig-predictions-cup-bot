"""One-screen status of the live bot from its SQLite log and the API.

    python -m sigbot.status [--minutes 30]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone

from .config import BotConfig, TICK
from .store import Store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def api(cfg, path):
    r = subprocess.run(["curl", "-s", cfg.base_url + path, "-H", "Authorization: Bearer " + cfg.api_key], capture_output=True, text=True)
    return json.loads(r.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--api", action="store_true", help="also query the API (costs ~5 reads of the shared budget)")
    args = ap.parse_args()
    cfg = BotConfig.load()
    store = Store(os.path.join(HERE, cfg.db_path))
    now = time.time()
    since = now - args.minutes * 60

    # last cycle line
    cyc = store.one("SELECT * FROM cycles ORDER BY cycle_id DESC LIMIT 1")
    age = now - cyc["ts"] if cyc else None
    if args.api:
        pnl = api(cfg, f"/tournaments/{cfg.tournament_slug}/portfolio/pnl?period=all")
        acct = api(cfg, "/account")
        print(f"cycle {cyc['cycle_id'] if cyc else '-'} {age:.0f}s ago | cash {acct.get('balance')} | holdings {pnl.get('totalHoldingsValue')} "
              f"| total {pnl.get('totalAccountValue')} | unrealized {pnl.get('unrealizedPnl')}")
    else:
        print(f"cycle {cyc['cycle_id'] if cyc else '-'} {age:.0f}s ago | cash {cyc['cash'] if cyc else '-'} | holdings {cyc['positions_value'] if cyc else '-'} "
              f"| total {cyc['total_value'] if cyc else '-'} (from the bot's last cycle snapshot)")

    # fills by purpose in window, and overall
    rows = store.rows("SELECT purpose, side, price, quantity, filled_at, exchange_id, level FROM fills ORDER BY filled_at")
    by_p = defaultdict(lambda: [0, 0.0]); recent = defaultdict(lambda: [0, 0.0])
    for r in rows:
        by_p[r["purpose"]][0] += 1; by_p[r["purpose"]][1] += abs(r["quantity"])
        if _ts(r["filled_at"]) >= since:
            recent[r["purpose"]][0] += 1; recent[r["purpose"]][1] += abs(r["quantity"])
    print("fills total:", {k: f"{v[0]}x/{v[1]:.0f}sh" for k, v in by_p.items()}, f"| last {args.minutes:.0f}m:", {k: f"{v[0]}x/{v[1]:.0f}sh" for k, v in recent.items()})

    # MM markouts: mid 5 min after each mm fill vs fill price, in ticks, signed in our favour
    mo = defaultdict(list)
    for r in rows:
        if r["purpose"] != "mm" or r["price"] is None:
            continue
        t = _ts(r["filled_at"])
        q = store.one("SELECT mid FROM quotes WHERE exchange_id=? AND ts>=? ORDER BY ts ASC LIMIT 1", (r["exchange_id"], t + 300))
        if q is None or q["mid"] is None:
            continue
        edge = (q["mid"] - r["price"]) if r["side"] == "yes" else (r["price"] - q["mid"])
        mo[(r["side"], r["level"])].append(edge / TICK)
    if mo:
        print("MM 5-min markouts (ticks, + = kept edge):", {f"{s}:L{l}": f"n={len(v)} mean={sum(v)/len(v):+.2f}" for (s, l), v in sorted(mo.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0))})
    allmo = [x for v in mo.values() for x in v]
    if allmo:
        print(f"MM markout all: n={len(allmo)} mean={sum(allmo)/len(allmo):+.2f} ticks")

    # positions ex-arb and exposure
    if not args.api:
        last = store.rows("SELECT * FROM cycles ORDER BY cycle_id DESC LIMIT 1")
        q429 = store.one("SELECT COUNT(*) AS n FROM orders WHERE placed_at>=?", (since,))
        print(f"orders placed last {args.minutes:.0f}m: {q429['n'] if q429 else 0} | wave/hot state is in data/live.log")
        return
    pos = api(cfg, f"/tournaments/{cfg.tournament_slug}/portfolio/positions")
    from .universe import Universe
    from . import risk
    u = Universe.from_markets(json.load(open(os.path.join(HERE, "data/markets.json"))))
    positions = {str(p["exchangeId"]): float(p["quantity"]) for p in pos.get("positions", [])}
    prices = {}
    exs = list(positions.keys())
    for i in range(0, len(exs), 100):
        res = api(cfg, "/exchanges/prices?ids=" + ",".join(exs[i:i+100]) + "&tournamentId=" + (u and "bda92870-621e-47b0-bc3c-3602c5c26f55"))
        for row in res.get("data", []):
            prices[row["exchangeId"]] = row
    e = risk.exposure(u, positions, prices)
    net = risk.net_positions_ex_arb(u, positions)
    inv = {ex: q for ex, q in net.items() if abs(q) >= 1}
    print(f"net D notional {e.net_d_notional:+.0f} (long {e.long_d:.0f} / short {e.short_d:.0f}) | non-arb positions: {len(inv)} | biggest:",
          sorted(((round(abs(q) * ((prices.get(ex) or {}).get('latestPrice') or 0.5 if q > 0 else 1 - ((prices.get(ex) or {}).get('latestPrice') or 0.5))), ex, q) for ex, q in inv.items()), reverse=True)[:5])
    oo = api(cfg, "/orders?status=open&limit=200&tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55")
    n_open = len(oo.get("data", []))
    rest = sum((o["priceLimit"] or 0) * o["quantity"] for o in oo.get("data", []))
    print(f"open orders {n_open}{'+' if oo.get('pagination', {}).get('hasMore') else ''} | resting notional (first page) {rest:.0f}")
    ov = store.get_param("mm_overrides") or {}
    exo = ov.get("exchanges", {})
    hot = [ex for ex, v in exo.items() if v.get("shape") in ("toxic", "calm")]
    print(f"overrides: {len(exo)} exchanges, regime switches: {hot[:8]}")


if __name__ == "__main__":
    main()
