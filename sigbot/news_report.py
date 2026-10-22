"""Measure the news sweeper: hit rate and move captured, from the news_events table.

    python -m sigbot.news_report              # table of events + aggregates
    python -m sigbot.news_report --backfill   # first fill missing mid_5/30/120 from 5-minute candles (uses the API)
    python -m sigbot.news_report --since 24   # last 24 hours only

Definitions
  predicted move   expected_move_pts, signed by direction (up = +)
  realised move    (mid_T - mid_before) * 100 signed in the predicted direction, T in {5, 30, 120} minutes
  hit              realised move at T >= expected / 3 (the same bar the classifier is asked to be confident about)
  captured         realised move at T in points; for traded/dry rows also P&L per share vs the fill price
Rows with action skipped_* are measured too, so the thresholds can be tuned: a skip that would have paid is visible.
"""
from __future__ import annotations

import argparse
import os
import statistics
import time
from datetime import datetime, timezone

from .api import ApiError, SuperMarketClient
from .config import BotConfig
from .news import parse_date
from .store import Store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = {"mid_5": 5, "mid_30": 30, "mid_120": 120}


def backfill(store: Store, api: SuperMarketClient):
    rows = store.rows("SELECT * FROM news_events WHERE exchange_id IS NOT NULL AND (mid_5 IS NULL OR mid_30 IS NULL OR mid_120 IS NULL)")
    cache: dict[str, list] = {}
    n = 0
    for ev in rows:
        ex = ev["exchange_id"]
        if ex not in cache:
            try:
                cache[ex] = api.price_history(ex, "5m", 500).get("candles", [])
            except ApiError as e:
                print(f"price history {ex}: {e}")
                cache[ex] = []
        candles = [(parse_date(c.get("time")), c.get("close")) for c in cache[ex] if c.get("close") is not None]
        for col, minutes in COLS.items():
            if ev[col] is not None:
                continue
            target = ev["ts"] + minutes * 60
            if time.time() < target:
                continue
            best = None
            for ct, close in candles:
                if ct is not None and ct <= target:
                    best = close
            if best is not None:
                store.set_news_followup(ev["id"], col, float(best))
                n += 1
    print(f"backfilled {n} follow-up mids from candles")


def fmt(x, w=6, d=1):
    return f"{x:{w}.{d}f}" if isinstance(x, (int, float)) else " " * (w - 1) + "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--since", type=float, default=None, help="hours")
    args = ap.parse_args()
    cfg = BotConfig.load(args.config)
    store = Store(os.path.join(HERE, cfg.db_path))
    if args.backfill:
        if not cfg.api_key:
            raise SystemExit("backfill needs the SIG API key")
        api = SuperMarketClient(cfg.api_key, cfg.base_url)
        api.tournament_id = api.tournament(cfg.tournament_slug)["id"]
        backfill(store, api)
    since = time.time() - args.since * 3600 if args.since else 0
    rows = store.rows("SELECT * FROM news_events WHERE ts>=? ORDER BY ts", (since,))
    n_items = store.one("SELECT COUNT(*) AS n, SUM(prefilter) AS p, SUM(screened>=1) AS s, SUM(screened=2) AS f FROM news_items WHERE first_seen>=?", (since,))
    print(f"news_items: {n_items['n']} seen, {n_items['p'] or 0} passed the pre-filter, {n_items['s'] or 0} screened, {n_items['f'] or 0} reached the final call")
    if not rows:
        print("no news_events yet")
        return
    print(f"\n{'when (UTC)':16s} {'action':17s} {'race':24s} P dir  exp  conf  moved   m+5  m+30 m+120  headline")
    per = {c: [] for c in COLS}
    hits = {c: [] for c in COLS}
    pnl = {c: [] for c in COLS}
    by_action: dict[str, int] = {}
    for ev in rows:
        by_action[ev["action"]] = by_action.get(ev["action"], 0) + 1
        sign = 1.0 if ev["direction"] == "up" else -1.0
        mids = {}
        for c in COLS:
            if ev[c] is not None and ev["mid_before"] is not None:
                real = (ev[c] - ev["mid_before"]) * 100 * sign
                mids[c] = real
                if ev["action"] in ("traded", "dry", "skipped_moved", "skipped_race_cap", "skipped_budget"):
                    per[c].append(real)
                    if ev["expected_move_pts"]:
                        hits[c].append(real >= ev["expected_move_pts"] / 3)
                    if ev["price"] is not None and ev["action"] in ("traded", "dry"):
                        # P&L per share in YES space: bought YES at price (up) or sold YES at 1 - price (down)
                        entry = ev["price"] if ev["direction"] == "up" else 1 - ev["price"]
                        pnl[c].append((ev[c] - entry) * 100 * sign)
        when = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%m-%d %H:%M")
        print(f"{when:16s} {ev['action']:17s} {ev['race'][:24]:24s} {ev['party']} {ev['direction'][:4]:4s} {fmt(ev['expected_move_pts'], 4, 0)} "
              f"{fmt(ev['confidence'], 5, 2)} {fmt(ev['moved_pts'], 6)} {fmt(mids.get('mid_5'), 5)} {fmt(mids.get('mid_30'), 5)} {fmt(mids.get('mid_120'), 5)}  "
              f"{(ev['headline'] or '')[:60]}")
    print("\nactions:", ", ".join(f"{k}={v}" for k, v in sorted(by_action.items())))
    print(f"\n{'horizon':8s} {'n':>4s} {'hit rate':>9s} {'avg move pts':>13s} {'median':>7s} {'avg P&L/share pts':>18s}")
    for c, minutes in COLS.items():
        if per[c]:
            hr = f"{100 * sum(hits[c]) / len(hits[c]):.0f}%" if hits[c] else "-"
            pl = f"{statistics.mean(pnl[c]):+.2f}" if pnl[c] else "-"
            print(f"+{minutes:<7d} {len(per[c]):4d} {hr:>9s} {statistics.mean(per[c]):+13.2f} {statistics.median(per[c]):+7.2f} {pl:>18s}")
        else:
            print(f"+{minutes:<7d} {0:4d} {'-':>9s} {'-':>13s} {'-':>7s} {'-':>18s}")
    acted = [r for r in rows if r["action"] in ("traded", "dry")]
    if acted:
        exp = statistics.mean(r["expected_move_pts"] or 0 for r in acted)
        print(f"\nacted on {len(acted)} events; mean expected move {exp:+.1f} pts; mean confidence {statistics.mean(r['confidence'] or 0 for r in acted):.2f}")
        for c in COLS:
            if pnl[c] and per[c]:
                print(f"  +{COLS[c]:<4d} realised/expected = {statistics.mean(per[c]) / exp:+.2f}" if exp else "")


if __name__ == "__main__":
    main()
