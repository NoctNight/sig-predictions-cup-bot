"""Score the directional sleeve from `direction_signals` (no API calls; reads data/sigbot.sqlite only).

    python3 -m sigbot.direction_report               # hit rates, average SIG move, paper P&L
    python3 -m sigbot.direction_report --backfill    # first fill missing mid_5/30/120 from the recorded SIG quotes
    python3 -m sigbot.direction_report --since 24    # only signals from the last 24 hours

Prediction quality uses every logged signal (dry, traded and skipped: sizing does not change whether the call was
right). A signal is a hit at horizon h when the SIG mid moved in the predicted direction (strictly); "toward fair" is
the share of the gap (fair - SIG mid at signal) that closed by h. P&L uses only positions (dry paper rows in dry runs,
traded rows when live):
  * mark @SIG mid      qty x (latest SIG mid - entry) in the bought side's terms
  * value @fair        qty x (latest outside fair - entry): expected P&L if held to settlement
  * exit @+120m        move signals only: a paper round trip that sells at the SIG mid 120 minutes later (no spread)
"""
from __future__ import annotations

import argparse
import os
import time
from statistics import mean

from .store import Store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def backfill(store: Store) -> int:
    """Fill mid_N / fair_N from recorded SIG quotes (venue_quotes, venue 'sig') and venue_fair at ts + N minutes."""
    n = 0
    now = time.time()
    for m in (5, 30, 120):
        rows = store.rows(f"SELECT id, ts, exchange_id, race, party FROM direction_signals WHERE mid_{m} IS NULL AND ts<=?", (now - m * 60,))
        for r in rows:
            t = r["ts"] + m * 60
            q = store.one("SELECT bid, ask FROM venue_quotes WHERE venue='sig' AND instrument=? AND ts<=? ORDER BY ts DESC LIMIT 1", (r["exchange_id"], t))
            f = store.one("SELECT fair, ts FROM venue_fair WHERE race=? AND party=? AND ts<=? ORDER BY ts DESC LIMIT 1", (r["race"], r["party"], t))
            if q and q["bid"] is not None and q["ask"] is not None:
                store.exec(f"UPDATE direction_signals SET mid_{m}=?, fair_{m}=COALESCE(fair_{m}, ?) WHERE id=?",
                           ((q["bid"] + q["ask"]) / 2, f["fair"] if f and t - f["ts"] < 900 else None, r["id"]))
                n += 1
    return n


def _sign(direction: str) -> int:
    return 1 if direction == "up" else -1


def prediction_table(store: Store, since_ts: float) -> list[str]:
    out = [f"{'kind':6s} {'h':>4s} {'n':>5s} {'hit%':>6s} {'flat%':>6s} {'avg move':>9s} {'toward fair':>12s}"]
    for kind in ("value", "move"):
        rows = store.rows("SELECT * FROM direction_signals WHERE kind=? AND ts>=? AND sig_mid IS NOT NULL", (kind, since_ts))
        for h in (5, 30, 120):
            moves, closes = [], []
            for r in rows:
                mh = r[f"mid_{h}"]
                if mh is None:
                    continue
                mv = (mh - r["sig_mid"]) * _sign(r["direction"])
                moves.append(mv)
                gap = (r["fair"] - r["sig_mid"]) if r["fair"] is not None else None
                if gap and abs(gap) > 1e-6:
                    closes.append((mh - r["sig_mid"]) / gap)
            if not moves:
                out.append(f"{kind:6s} {h:4d} {0:5d}")
                continue
            hit = 100 * sum(1 for m in moves if m > 1e-9) / len(moves)
            flat = 100 * sum(1 for m in moves if abs(m) <= 1e-9) / len(moves)
            out.append(f"{kind:6s} {h:4d} {len(moves):5d} {hit:6.1f} {flat:6.1f} {100 * mean(moves):+8.2f}pt {100 * mean(closes) if closes else 0:+11.1f}%")
    return out


def pnl_table(store: Store, since_ts: float) -> list[str]:
    pos = store.rows("SELECT * FROM direction_signals WHERE action IN ('dry','traded') AND ts>=? ORDER BY ts", (since_ts,))
    out = []
    if not pos:
        return ["no paper or traded positions yet"]
    tot = {"cost": 0.0, "mark": 0.0, "fair": 0.0, "exit120": 0.0, "n_exit": 0}
    by_kind: dict = {}
    for r in pos:
        q = store.one("SELECT bid, ask FROM venue_quotes WHERE venue='sig' AND instrument=? ORDER BY ts DESC LIMIT 1", (r["exchange_id"],))
        f = store.one("SELECT fair FROM venue_fair WHERE race=? AND party=? ORDER BY ts DESC LIMIT 1", (r["race"], r["party"]))
        mid = (q["bid"] + q["ask"]) / 2 if q and q["bid"] is not None and q["ask"] is not None else r["sig_mid"]
        fair = f["fair"] if f and f["fair"] is not None else r["fair"]
        yes = r["side"] == "yes"
        val_mid = mid if yes else 1 - mid
        val_fair = fair if yes else 1 - fair
        qty = r["quantity"] or 0
        k = by_kind.setdefault((r["kind"], r["action"]), {"n": 0, "cost": 0.0, "mark": 0.0, "fair": 0.0, "exit120": 0.0, "n_exit": 0})
        for d in (tot, k):
            d["cost"] += r["cost"] or 0.0
            d["mark"] += qty * (val_mid - r["price"])
            d["fair"] += qty * (val_fair - r["price"])
        if r["kind"] == "move" and r["mid_120"] is not None:
            ex = r["mid_120"] if yes else 1 - r["mid_120"]
            for d in (tot, k):
                d["exit120"] += qty * (ex - r["price"])
                d["n_exit"] += 1
        if "n" in k:
            k["n"] += 1
    out.append(f"{'kind':6s} {'action':7s} {'n':>4s} {'cost':>9s} {'mark@SIG':>10s} {'value@fair':>11s} {'exit@+120m':>11s}")
    for (kind, action), d in sorted(by_kind.items()):
        out.append(f"{kind:6s} {action:7s} {d['n']:4d} {d['cost']:9.0f} {d['mark']:+10.1f} {d['fair']:+11.1f} "
                   f"{(f'{d['exit120']:+.1f} ({d['n_exit']})' if d['n_exit'] else '-'):>11s}")
    out.append(f"{'total':14s} {len(pos):4d} {tot['cost']:9.0f} {tot['mark']:+10.1f} {tot['fair']:+11.1f}")
    return out


def latency_table(store: Store, since_ts: float) -> list[str]:
    out = []
    for v in ("polymarket", "kalshi"):
        r = store.rows("SELECT latency_ms FROM venue_quotes WHERE venue=? AND ts>=? AND latency_ms IS NOT NULL", (v, since_ts))
        lat = sorted(x["latency_ms"] for x in r)
        if lat:
            out.append(f"{v:10s} per-call latency p50 {lat[len(lat) // 2]:.0f} ms, p90 {lat[int(len(lat) * 0.9)]:.0f} ms (n={len(lat)} quote rows)")
    polls = store.one("SELECT COUNT(DISTINCT ts) AS n, MIN(ts) AS a, MAX(ts) AS b FROM venue_fair WHERE ts>=?", (since_ts,))
    if polls and polls["n"]:
        out.append(f"polls: {polls['n']} over {(polls['b'] - polls['a']) / 3600:.1f} h")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--since", type=float, default=None, help="hours")
    args = ap.parse_args()
    store = Store(os.path.join(HERE, "data", "sigbot.sqlite"))
    from .direction import SCHEMA
    with store.lock:
        store.conn.executescript(SCHEMA)
    since = time.time() - args.since * 3600 if args.since else 0.0
    if args.backfill:
        print(f"backfilled {backfill(store)} follow-ups from recorded quotes")
    n = store.one("SELECT COUNT(*) AS n, MIN(ts) AS a FROM direction_signals WHERE ts>=?", (since,))
    print(f"direction_signals: {n['n']} rows" + (f" since {time.strftime('%Y-%m-%d %H:%M', time.localtime(n['a']))}" if n["a"] else ""))
    for r in store.rows("SELECT kind, action, COUNT(*) AS n FROM direction_signals WHERE ts>=? GROUP BY 1,2 ORDER BY 1,2", (since,)):
        print(f"  {r['kind']:6s} {r['action']:16s} {r['n']}")
    print("\nPrediction (SIG mid move in the predicted direction after the signal)")
    print("\n".join(prediction_table(store, since)))
    print("\nPositions (paper rows in dry runs)")
    print("\n".join(pnl_table(store, since)))
    print("\nOutside sources")
    print("\n".join(latency_table(store, since)))


if __name__ == "__main__":
    main()
