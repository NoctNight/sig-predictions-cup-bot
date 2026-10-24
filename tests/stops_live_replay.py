"""Would the per-strategy stops have paused anything on the live record? (strategy_stops.py acceptance (d))

    python3 tests/stops_live_replay.py [--root DIR] [--step 300]

Read-only on DIR/data/sigbot.sqlite and DIR/data/live.log. Every `step` seconds since the first fill, every strategy's
ledger is rebuilt from the fills ingested by then (attributed as the stops do) and valued at SIG's touch at that moment
(venue_quotes: the latest bid/ask at or before it). For each strategy: its worst fall from ANY earlier point (so whatever
moment the stop is armed at, a loss since armed can be no larger) and its worst fall within one hour, against its limit
and drop threshold. The markout rule is replayed on the HEALTH lines (markout30 / n), and today's switch firings are
counted for reference. Set arb, recycles and high-frequency arb share one pooled set ledger here (churn's own ledger has
no history), valued at settlement basis; the unattributed check needs the venue's positions per leg and is not replayed.
"""
from __future__ import annotations

import argparse
import bisect
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import sigbot.strategy_stops as ST  # noqa: E402

HEALTH = re.compile(r"^(\S+ \S+),\d+ INFO sigbot: HEALTH .*markout30=(\S+?)\(n=(\d+)\)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(HERE))
    ap.add_argument("--step", type=float, default=300.0)
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{os.path.join(a.root, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    class Store:                                   # the read-only surface StrategyStops.ledgers() uses
        def rows(self, q, p=()):
            return db.execute(q, p).fetchall()

        def one(self, q, p=()):
            return db.execute(q, p).fetchone()
    st = ST.StrategyStops(type("C", (), {})(), Store(), "/nonexistent", [], write=False)
    fills = db.execute("SELECT ingested_at FROM fills WHERE price IS NOT NULL ORDER BY ingested_at").fetchall()
    if not fills:
        print("no fills")
        return 0
    races = {}
    for r in db.execute("SELECT legs FROM arb_trades WHERE kind IN ('sell_set','buy_set')"):
        try:
            import json
            legs = sorted({str(l["exchangeId"]) for l in json.loads(r["legs"] or "[]")})
            if len(legs) >= 2:
                races[tuple(legs)] = 1
        except ValueError:
            pass
    st.races = [list(k) for k in races]
    quotes = {}
    for r in db.execute("SELECT instrument, ts, bid, ask FROM venue_quotes WHERE venue='sig' ORDER BY ts"):
        q = quotes.setdefault(str(r["instrument"]), ([], []))
        q[0].append(r["ts"])
        q[1].append((r["bid"], r["ask"]))
    t0, t1 = fills[0][0], max(fills[-1][0], max(v[0][-1] for v in quotes.values()))
    keys = ("mm", "cheapfav", "catchup", "direction", "arb (pooled sets)")
    series = {k: [] for k in keys}
    real_seen = st.seen_fill
    t = t0
    while t <= t1 + 1:
        st.seen_fill = real_seen
        def _at(q, t=t):                           # the fills ingested by then (both spellings of the ledger query)
            q = q.replace("WHERE f.price IS NOT NULL", "WHERE f.price IS NOT NULL AND f.ingested_at <= %f" % t)
            return q.replace("WHERE price IS NOT NULL", "WHERE price IS NOT NULL AND ingested_at <= %f" % t)
        assert "f.price IS NOT NULL" in open(ST.__file__).read()   # the substitution must keep matching the module
        st.store = type("S", (), {"rows": lambda self, q, p=(): db.execute(_at(q), p).fetchall(),
                                  "one": lambda self, q, p=(): db.execute(q, p).fetchone()})()
        L, _, _ = st.ledgers()
        touch = {}
        for ex, (ts, ba) in quotes.items():
            i = bisect.bisect_right(ts, t) - 1
            if i >= 0:
                touch[ex] = ba[i]
        for k in ("mm", "cheapfav", "catchup", "direction"):
            series[k].append((t, ST.touch_value(L[k], touch)))
        series["arb (pooled sets)"].append((t, ST.touch_value(L["sets"], touch, st.races)))
        t += a.step
    lim = dict(ST.DEFAULT_LIMITS)
    limit_of = {"mm": lim["mm"], "cheapfav": lim["cheapfav"], "catchup": lim["catchup"], "direction": lim["direction"],
                "arb (pooled sets)": lim["arb"]}
    fmt = lambda x: datetime.fromtimestamp(x, timezone.utc).strftime("%m-%d %H:%M")
    print(f"live record {fmt(t0)} -> {fmt(t1)} UTC, every {a.step:.0f} s, {len(fills)} fills")
    print(f"{'strategy':20s} {'limit':>7s} {'worst fall (any arm)':>22s} {'worst 1-h fall':>16s} {'drop limit':>10s}  would pause?")
    bad = 0
    for k in keys:
        s = series[k]
        peak, worst, wt = -1e18, 0.0, None
        hw, ht = 0.0, None
        for i, (t, v) in enumerate(s):
            if v > peak:
                peak = v
            if peak - v > worst:
                worst, wt = peak - v, t
            j = i
            while j > 0 and t - s[j - 1][0] <= 3600:
                j -= 1
            hp = max(x for _, x in s[j:i + 1])
            if hp - v > hw:
                hw, ht = hp - v, t
        drop_lim = abs(limit_of[k]) if k in ST.WIDE_DROP else 400.0       # as StrategyStops (kill_drawdown 400)
        fire = worst >= abs(limit_of[k]) or hw >= drop_lim
        bad += fire
        print(f"{k:20s} {limit_of[k]:7.0f} {worst:12.1f} at {fmt(wt) if wt else '-':11s} {hw:7.1f} at {fmt(ht) if ht else '-':11s}"
              f" {drop_lim:7.0f}  {'YES' if fire else 'no'}")
    mk, kills = [], 0
    try:
        for ln in open(os.path.join(a.root, "data", "live.log"), errors="replace"):
            if "KILL SWITCH" in ln:
                kills += 1
            m = HEALTH.match(ln)
            if m and m[2] != "None" and int(m[3]) >= 20 and round(float(m[2]), 6) < -1.0:
                mk.append(m[1][:16])
    except OSError:
        pass
    print(f"markout rule (MM, n >= 20, < -1): {'fires at ' + ', '.join(sorted(set(mk))[:5]) if mk else 'never fires'}")
    pp = []
    by = {}
    for r in db.execute("SELECT ts, race, kind FROM arb_trades WHERE ok=1 ORDER BY ts, id"):
        by.setdefault(r["race"], []).append((r["ts"], "unwind" if r["kind"] == "unwind_set" else "buy"))
    for race, s in by.items():
        for i in range(len(s)):
            w = [k for tt, k in s if s[i][0] - 3600 < tt <= s[i][0]]
            flips = sum(1 for x, y in zip(w, w[1:]) if x != y)
            if flips >= ST.PINGPONG_FLIPS and w.count("unwind") >= ST.PINGPONG_UNWINDS:
                pp.append((s[i][0], race))
                break
    print("ping-pong rule (>= %d alternations, >= %d unwinds in an hour): %s" % (
        ST.PINGPONG_FLIPS, ST.PINGPONG_UNWINDS, ", ".join(f"{race} at {fmt(t_)}" for t_, race in sorted(pp)) or "never fires"))
    print(f"today's bot-wide switch fired {kills} time(s) in live.log over the same record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
