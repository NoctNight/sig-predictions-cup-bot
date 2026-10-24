"""Behaviour tests for sigbot/strategy_stops.py (per-strategy stops replacing the bot-wide kill switch). Offline: a
temporary store and params dir, a hand-kept venue account; never touches data/ or params/.

    python3 tests/test_strategy_stops.py

Valuation (touch, complete sets at payout), attribution (orders joined at evaluation time, arb_trades responses,
untagged), each rule (stop since armed, trailing-hour drop, markout, sleeves' own stops, exits/unwinds through zero,
untagged loss, unattributed loss with its 3-evaluation persistence and escalation, stuck orders, bypass), persistence
and re-arming after a human clears a pause, the quarantine of a paused strategy's legs, the gates (aliases, "all", exit
paths), attribution-noise cases that must never pause anything, and that every purpose the code records has an owner.
"""
from __future__ import annotations

import ast
import glob
import json
import os
import shutil
import sys
import tempfile
import time
import types
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sigbot.strategy_monitor as SM  # noqa: E402
import sigbot.strategy_stops as ST  # noqa: E402
from sigbot.store import Store  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def cfg(**k):
    c = types.SimpleNamespace(kill_drawdown=400.0, kill_markout_ticks=-1.0, kill_pause_sec=1800, stop_limits={},
                              unattributed_limit=2000.0, untagged_limit=200.0)
    for a, b in k.items():
        setattr(c, a, b)
    return c


class Env:
    """A store, a params dir and the venue's account kept by hand (cash, signed positions, YES marks)."""

    def __init__(self, races=(("A", "B"),), **c):
        self.dir = tempfile.mkdtemp(prefix="stops_test_")
        self.params = os.path.join(self.dir, "params")
        os.makedirs(self.params)
        self.store = Store(os.path.join(self.dir, "t.sqlite"))
        self.races = [list(r) for r in races]
        self.cfg = cfg(**c)
        self.ids = 1000
        self.fid = 1
        self.cash = 100000.0
        self.pos: dict = {}
        self.mark: dict = {}
        self.touch: dict = {}
        self.now = time.time()
        self.stops = self.new()

    def new(self):
        return ST.StrategyStops(self.cfg, self.store, self.params, self.races, now=self.now)

    def order(self, ex, purpose, side="yes", price=0.5, qty=100, placed=None, expires=None, record=True):
        self.ids += 1
        if record:
            self.store.record_order(self.ids, ex, None, side, "buy", price, qty, purpose, expires_at=expires)
            if placed is not None:
                self.store.exec("UPDATE orders SET placed_at=? WHERE order_id=?", (placed, self.ids))
        return self.ids

    def fill(self, ex, q, px, purpose=None, oid=None, venue=True, record=True):
        """q signed YES shares (+ buys YES, - buys NO) at YES price px; the venue account moves too (merges pay 1)."""
        if oid is None:
            oid = self.order(ex, purpose, "yes" if q > 0 else "no", px if q > 0 else round(1 - px, 4), abs(q), record=record)
        side = "yes" if q > 0 else "no"
        self.store.ingest_fills([{"id": self.fid, "orderId": oid, "exchangeId": ex, "side": side,
                                  "price": px if q > 0 else 1 - px, "quantity": q, "filledAt": iso(self.now)}])
        self.store.exec("UPDATE fills SET ingested_at=? WHERE id=?", (self.now, self.fid))   # the test's clock, as in the bot
        self.fid += 1
        if venue:
            self.venue_trade(ex, q, px)
        return oid

    def venue_trade(self, ex, q, px):
        p = self.pos.get(ex, 0.0)
        if q > 0:
            self.cash -= q * px
            m = min(q, -p) if p < 0 else 0
        else:
            self.cash -= -q * (1 - px)
            m = min(-q, p) if p > 0 else 0
        self.cash += m
        self.pos[ex] = p + q

    def set_touch(self, ex, bid, ask):
        self.touch[ex] = (bid, ask)
        self.mark[ex] = (bid + ask) / 2

    def mv(self):
        out = {}
        for ex, q in self.pos.items():
            m = self.mark.get(ex, 0.5)
            out[ex] = q * m if q > 0 else -q * (1 - m)
        return out

    def ev(self, dt=60.0, venue=True, **k):
        self.now += dt
        a = dict(touch=self.touch)
        if venue:
            a.update(cash=self.cash, positions={e: q for e, q in self.pos.items() if abs(q) > 1e-9}, mv=self.mv())
        a.update(k)
        return self.stops.evaluate(self.now, **a)

    def kills(self):
        return SM.normalise_kills(SM._read_json(os.path.join(self.params, SM.KILL_FILE), {})) or {}

    def close(self):
        self.store.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def t_valuation():
    L = ST.Ledger()
    L.add("A", 100, 0.5)
    check("touch: a long is worth the bid", abs(ST.touch_value(L, {"A": (0.48, 0.52)}) - (-2.0)) < 1e-9)
    S = ST.Ledger()
    S.add("A", -100, 0.5)
    check("touch: a short (NO) is worth closing at the ask", abs(ST.touch_value(S, {"A": (0.48, 0.52)}) - (-2.0)) < 1e-9)
    N = ST.Ledger()
    N.add("A", -100, 0.6)
    N.add("B", -100, 0.3)                              # NO costs 0.4 + 0.7 = 1.1 per set paying 1: -10
    v = ST.touch_value(N, {"A": (0.1, 0.9), "B": (0.1, 0.9)}, [["A", "B"]])
    check("sets: a complete NO set is worth its payout whatever the touch", abs(v - (-10.0)) < 1e-9, v)
    N.add("A", -50, 0.6)
    v2 = ST.touch_value(N, {"A": (0.58, 0.62), "B": (0.28, 0.32)}, [["A", "B"]])
    check("sets: the part beyond the common quantity is at the touch", abs(v2 - (-10.0 + 30.0 - 31.0)) < 1e-9, v2)
    Y = ST.Ledger()
    Y.add("A", 100, 0.6)
    Y.add("B", 100, 0.35)
    check("sets: a complete YES set is worth 1 per set", abs(ST.touch_value(Y, {}, [["A", "B"]]) - 5.0) < 1e-9)


def t_attribution():
    e = Env()
    oid = e.order("A", None, record=False)                       # a fill ingested before its order row existed
    e.fill("A", 100, 0.5, oid=oid)
    e.store.record_order(oid, "A", None, "yes", "buy", 0.5, 100, "cheapfav")
    L, un, _ = e.stops.ledgers()
    check("attribution: the order's purpose is joined when the stops evaluate, not at ingest", L["cheapfav"].pos.get("A") == 100
          and not un.pos, (L["cheapfav"].pos, un.pos))
    e.store.record_arb("R", "unwind_set", [], 10, 0, 0, True, {"results": [{"data": {"orderId": 777, "exchangeId": "A"}}]})
    e.fill("A", 10, 0.5, oid=777)
    e.fill("B", 5, 0.5, oid=888)
    L, un, _ = e.stops.ledgers()
    check("attribution: a fill without an order row but in an arb_trades response is the set ledger's", L["sets"].pos.get("A") == 10)
    check("attribution: anything else is untagged", un.pos.get("B") == 5)
    e.close()


def t_stop_and_rearm():
    e = Env()
    e.set_touch("A", 0.59, 0.61)
    e.fill("A", 1000, 0.6, "cheapfav")
    e.ev()
    e.set_touch("A", 0.10, 0.12)                                  # armed at -10 (the bid 0.59): -490 since armed
    r = e.ev()
    check("stop: cheap favourites at -490 since armed is not paused (limit -500)", "cheapfav" not in e.kills(), r["status"])
    e.set_touch("A", 0.088, 0.12)
    r = e.ev()
    check("hold: a first breach only holds entries (in memory, nothing written)", "cheapfav" not in e.kills()
          and e.stops.hold.get("cheapfav", 0) > e.now, (sorted(e.kills()), e.stops.hold))
    r = e.ev()
    k = e.kills()
    check("stop: -502 since armed on two valuations in a row pauses cheap favourites only", "cheapfav" in k and set(k) == {"cheapfav"}, sorted(k))
    check("stop: the entry says who wrote it and why", k.get("cheapfav", {}).get("by") == "strategy_stops"
          and "since armed" in k.get("cheapfav", {}).get("reason", ""))
    check("quarantine: the MM keeps off the paused strategy's legs for 30 min",
          abs(e.stops.quarantine.get("A", 0) - (e.now + 1800)) < 1e-6, e.stops.quarantine)
    # persistence: a restart keeps the pause and the armed baseline
    e.stops = e.new()
    e.ev()
    check("restart: the pause is still in the kill file", "cheapfav" in e.kills())
    check("restart: the armed baseline survives (the loss is still measured from the old arm)",
          e.stops.last["status"]["pnl"].get("cheapfav", 0) < -490, e.stops.last["status"]["pnl"])
    # a human clears it: re-armed from now, the old loss cannot re-pause it at once
    kp = os.path.join(e.params, SM.KILL_FILE)
    SM._write_json(kp, {})
    e.ev()
    e.ev()
    check("re-arm: after a human deletes the entry the strategy is not paused again by the old loss", "cheapfav" not in e.kills(),
          sorted(e.kills()))
    check("re-arm: its P&L restarts from the clearing", abs(e.stops.last["status"]["pnl"].get("cheapfav", 99)) < 1e-6)
    e.close()


def t_drop():
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.fill("A", 5000, 0.49, "mm")
    e.ev()
    e.set_touch("A", 0.59, 0.61)                                  # +500 at the bid
    e.ev()
    e.set_touch("A", 0.50, 0.52)                                  # back to +50: a 450 drop inside the hour
    e.ev()
    e.ev()
    k = e.kills()
    check("drop: a 450 fall from the trailing-hour peak pauses the MM although it is +50 since armed", "mm" in k
          and k["mm"].get("rule") == "drop", sorted(k))
    e.close()
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.fill("A", 5000, 0.49, "mm")
    e.ev()
    e.set_touch("A", 0.59, 0.61)
    e.ev()
    e.set_touch("A", 0.50, 0.52)
    e.ev(dt=3700)                                                 # the same fall, but more than an hour after the peak
    e.ev()
    check("drop: a fall from a peak older than an hour is not a drop", "mm" not in e.kills(), sorted(e.kills()))
    e.close()


def t_markout_and_sleeves():
    e = Env()
    e.ev(markout=(-1.0000000000000009, 30))
    check("markout: exactly -1 (float noise) does not pause the MM", "mm" not in e.kills())
    e.ev(markout=(-3.0, 19))
    check("markout: fewer than 20 fills never pause", "mm" not in e.kills())
    e.ev(markout=(-1.05, 30))
    k = e.kills()
    check("markout: < -1 on 20+ fills pauses the MM only", set(k) == {"mm"} and k["mm"]["rule"] == "markout", sorted(k))
    e.ev(churn_killed=True, catchup_killed=True)
    k = e.kills()
    check("sleeves: high-frequency arb's and catch-up's own stops are mirrored", {"hf_arb", "catchup"} <= set(k), sorted(k))
    e.close()


def t_crossings():
    e = Env()
    e.fill("A", 100, 0.5, "mm")
    e.ev()
    e.fill("A", -200, 0.5, "mm")                                  # the MM selling more than it holds is allowed
    e.ev()
    check("crossing: an entry may flip (the MM sells more than it holds)", not e.kills(), sorted(e.kills()))
    e.fill("A", -150, 0.5, "exit")                                # gross YES held 100, exits closing 150 of it
    e.ev()
    first = not e.kills()
    e.ev(dt=100)
    e.ev(dt=100)                                                  # still so 3 min later: written
    k = e.kills()
    check("crossing: first seen is only a candidate (an unread fill may explain it)", first)
    check("crossing: a zero-EV exit closing more than the sleeves ever held pauses that exit path and its owners",
          {"exit:zero_ev", "mm", "direction"} <= set(k), sorted(k))
    e.close()
    e = Env()
    e.fill("A", 100, 0.5, "cheapfav")
    e.ev()
    e.fill("A", -150, 0.5, "cheapfav_exit")
    e.ev()
    e.ev(dt=100)
    e.ev(dt=100)
    check("crossing: a cheap-favourite exit through zero pauses exit:cheapfav and cheapfav", {"exit:cheapfav", "cheapfav"} <= set(e.kills()))
    e.close()
    for mm_no, want in ((0, True), (-100, False)):
        e = Env()
        e.fill("A", -100, 0.6, "arb")
        e.fill("B", -100, 0.3, "arb")
        if mm_no:
            e.fill("A", mm_no, 0.6, "mm")                         # MM NO inventory on the same leg (the venue nets it)
        e.ev()
        e.store.record_arb("R", "unwind_set", [], 150, 0, 0, True, {"results": [{"data": {"orderId": 4242, "exchangeId": "A"}}]})
        e.fill("A", 150, 0.6, oid=4242)
        e.ev()
        e.ev(dt=100)
        e.ev(dt=100)
        got = "exit:unwinds" in e.kills()
        check("crossing: an unwind leg ending long YES on the account pauses unwinds" if want else
              "crossing: an unwind that also closes MM NO the venue nets in is not a crossing", got == want, sorted(e.kills()))
        e.close()


def t_unattributed():
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.set_touch("B", 0.49, 0.51)
    e.fill("A", -1000, 0.5, "arb")
    e.fill("B", -1000, 0.5, "arb")
    e.fill("A", 300, 0.49, "mm")                                  # MM YES netted against the sets' NO
    for _ in range(3):
        e.ev()
    e.set_touch("A", 0.20, 0.22)
    e.set_touch("B", 0.78, 0.80)
    for _ in range(3):
        r = e.ev()
    check("noise: sets, MM netting, NO merges and big marks moves leave nothing unattributed",
          abs(r["status"]["unattributed"]) < 1e-6, r["status"])
    check("noise: and pause nothing", not e.kills(), sorted(e.kills()))
    # one evaluation with a cash read that does not match the positions (a fill between the reads): never acted on
    e.cash -= 3000
    e.ev()
    e.cash += 3000
    e.ev()
    check("unattributed: a one-evaluation spike of 3,000 pauses nothing", not e.kills(), sorted(e.kills()))
    # a fill the ledgers never see (venue only): 10,000 YES at 0.5 that falls to 0.21
    e.order("C", "mm", placed=e.now)                              # the MM quoted C since the baseline
    e.venue_trade("C", 10000, 0.5)
    e.set_touch("C", 0.20, 0.22)
    for _ in range(3):
        e.ev()
    check("unattributed: 3 evaluations at -2,900 over 2 min are not enough (5 min needed)", not e.kills(), sorted(e.kills()))
    e.ev(dt=200)
    k = e.kills()
    check("unattributed: the 3rd pauses the strategy that traded the leg where it sits (the MM), not all",
          "mm" in k and "all" not in k, sorted(k))
    e.close()
    e = Env()
    e.ev()
    e.venue_trade("D", 10000, 0.5)
    e.set_touch("D", 0.20, 0.22)
    for _ in range(3):
        e.ev(dt=150)
    check("unattributed: nobody traded the leg: every entry is paused (KILL-ENTRIES)", "all" in e.kills(), sorted(e.kills()))
    e.close()


def t_untagged():
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.ev()
    e.order("A", "mm", placed=e.now + 1)                          # the MM quotes A after the baseline
    for i in range(20):                                            # an untagged path crossing the spread: -0.01/round trip x 2,000
        e.fill("A", 2000, 0.51, oid=90000 + 2 * i, record=False)
        e.fill("A", -2000, 0.49, oid=90001 + 2 * i, record=False)
        e.ev()
        if "mm" in e.kills():
            break
    k = e.kills()
    check("untagged: a 200 loss on untagged fills pauses the strategy that trades that leg", "mm" in k and "all" not in k, sorted(k))
    for i in range(30):
        e.fill("A", 2000, 0.51, oid=95000 + 2 * i, record=False)
        e.fill("A", -2000, 0.49, oid=95001 + 2 * i, record=False)
        e.ev()
        if "all" in e.kills():
            break
    check("untagged: still losing another 200 after that: every entry is paused", "all" in e.kills(), sorted(e.kills()))
    e.close()


def t_stuck_and_bypass():
    e = Env()
    old = datetime.fromtimestamp(e.now - 700, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = datetime.fromtimestamp(e.now + 600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    a = e.order("A", "mm", expires=old)
    b = e.order("A", "exit", expires=old)
    c = e.order("A", "mm", expires=fresh)
    r = e.ev(open_orders={"A": [{"id": a}, {"id": b}, {"id": c}, {"id": 999999}]})
    check("stuck: first seen past expiry: nothing yet (the venue may be purging it)", not r["cancel"] and not e.kills(), r["cancel"])
    r = e.ev(open_orders={"A": [{"id": a}, {"id": b}, {"id": c}, {"id": 999999}]})
    check("stuck: still listed a minute later: an order 10+ min past its expiry is cancelled, a live one and an unknown one are not",
          sorted(r["cancel"]) == sorted([a, b]), r["cancel"])
    k = e.kills()
    check("stuck: the owner's entries are paused; a stuck exit only cancelled", "mm" in k and not any(n.startswith("exit:") for n in k),
          sorted(k))
    e.close()
    e = Env()
    e.ev(markout=(-2.0, 30))                                     # MM paused
    ts = e.kills()["mm"]["ts"]
    e.order("A", "mm", placed=ts + 200)
    e.order("A", "mm", placed=ts + 200)                           # one batch that was in flight at the pause
    e.order("A", "mm", placed=ts + 230)                           # and a scanner requote in flight too
    e.ev()
    check("bypass: two late placements (writes retried in flight) are not a bypass", "all" not in e.kills())
    e.order("A", "mm", placed=ts + 260)
    e.ev()
    check("bypass: MM-tagged orders placed at 3+ times after the MM's pause pause every entry", "all" in e.kills(), sorted(e.kills()))
    kp = os.path.join(e.params, SM.KILL_FILE)
    e.now += 150                                                  # (a hand edit, not 2 min after the stops wrote it)
    d = SM._read_json(kp, {})
    d.pop("all")
    SM._write_json(kp, d)                                         # Sizhe clears "all", keeps mm paused
    e.ev()
    e.ev()
    check("bypass: clearing only all does not re-fire on the order already reported", "all" not in e.kills() and "mm" in e.kills(),
          sorted(e.kills()))
    e.close()


def t_pingpong():
    e = Env()
    for i, k in enumerate(("sell_set", "unwind_set", "sell_set", "unwind_set")):      # churn's most in an hour: 3 flips
        e.store.record_arb("R", k, [], 10, 0, 0, True, {})
    e.ev()
    check("ping-pong: 3 alternations in an hour (churn's 30-min re-entry allows that) pause nothing", not e.kills(), sorted(e.kills()))
    e.store.record_arb("R", "sell_set", [], 10, 0, 0, True, {})
    e.ev()
    k = e.kills()
    check("ping-pong: 4 alternations with 2 unwinds in an hour pause set arb and high-frequency arb", {"arb", "hf_arb"} <= set(k), sorted(k))
    e.close()


def t_review_fixes():
    # one bad snapshot: held, then released when the next valuation is fine
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.fill("A", 5000, 0.49, "mm")
    e.ev()
    e.set_touch("A", 0.40, 0.52)                                  # a hole in the book: the bid 9 ticks down for one read
    e.ev()
    held = "mm" in e.stops.hold
    e.set_touch("A", 0.49, 0.51)
    e.ev()
    check("hold: a one-snapshot breach is held, then released, never written", held and "mm" not in e.stops.hold
          and not e.kills(), (held, e.stops.hold, sorted(e.kills())))
    e.close()
    # restart: history is not re-judged
    e = Env()
    e.fill("A", 100, 0.5, "mm")
    e.fill("A", -150, 0.5, "exit")                                # a crossing in the record before the stops existed
    e.stops = e.new()
    e.ev()
    check("restart: a crossing already in the record pauses nothing at deploy", not e.kills(), sorted(e.kills()))
    e.close()
    # an unreadable kill file: nothing rewritten, no pause forgiven
    e = Env()
    e.ev(markout=(-2.0, 30))
    kp = os.path.join(e.params, SM.KILL_FILE)
    good = open(kp).read()
    open(kp, "w").write(good.rstrip().rstrip("}").rstrip() + ',\n}')     # a trailing comma: invalid JSON
    bad = open(kp).read()
    e.ev(churn_killed=True)
    check("kill file unreadable: never rewritten (no pause wiped), although a new rule fired", open(kp).read() == bad, open(kp).read()[:80])
    check("kill file unreadable: decisions use the last good copy (mm still paused for the bot)", "mm" in (e.stops.good_kills or {}))
    e.close()
    # settlement: the unattributed check waits
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.set_touch("B", 0.49, 0.51)
    e.fill("A", -1000, 0.5, "arb")
    e.fill("B", -1000, 0.5, "arb")
    e.ev()
    e.pos.pop("A")                                                # A settles NO-loses: the position leaves the read
    e.touch.pop("A")
    for _ in range(4):
        r = e.ev(settled={"A"})
    check("settlement: the unattributed check waits after a position settles", r["status"]["unattributed"] == "paused after a settlement"
          and not e.kills(), (r["status"], sorted(e.kills())))
    e.close()
    # bot-wide backstop: three strategies each -350 within the hour (each under its own rules)
    e = Env()
    for ex, k in (("A", "mm"), ("B", "cheapfav"), ("C", "direction")):
        e.set_touch(ex, 0.59, 0.61)
        e.fill(ex, 3500, 0.59, k)
    e.ev()
    for ex in ("A", "B", "C"):
        e.set_touch(ex, 0.49, 0.51)
    e.ev()
    e.ev()
    k = e.kills()
    check("combined: three sleeves losing 350 each in an hour (1,050) are paused; cheap favourites (under its own -500) "
          "by the bot-wide backstop", {"mm", "cheapfav", "direction"} <= set(k) and k["cheapfav"]["rule"] == "combined", sorted(k))
    e.close()
    # a pause the stops did not write, cleared by hand: re-armed, not re-paused by the same loss
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.fill("A", 1000, 0.49, "mm")
    e.ev()
    kp = os.path.join(e.params, SM.KILL_FILE)
    SM._write_json(kp, {"mm": {"reason": "strategy monitor", "ts": e.now}})
    e.set_touch("A", 0.15, 0.17)                                  # -340 since armed
    e.ev()
    e.ev()
    SM._write_json(kp, {})
    e.ev()
    e.ev()
    check("re-arm: clearing a pause the stops did not write re-arms it too", "mm" not in e.kills(), sorted(e.kills()))
    e.close()
    # ping-pong: free unwinds (proceeds >= payout) are not alternations
    e = Env()
    two = json.dumps([{"exchangeId": "A"}, {"exchangeId": "B"}])
    for k, c in (("sell_set", 0.98), ("unwind_set", 1.0), ("sell_set", 0.98), ("unwind_set", 1.0), ("sell_set", 0.98), ("unwind_set", 1.0)):
        e.store.exec("INSERT INTO arb_trades(ts,race,kind,legs,quantity,edge_per_set,cost_per_set,ok,response) VALUES(?,?,?,?,?,?,?,?,?)",
                     (time.time(), "R", k, two, 10, 0, c, 1, "{}"))
    e.ev()
    check("ping-pong: buying sets and free-unwinding them at the payout is not a loop", not e.kills(), sorted(e.kills()))
    e.close()
    check("purposes: the cash raise's raise_set has an owner", ST.PURPOSE_LEDGER.get("raise_set") == "raise")


def t_round2():
    # a raise clip sells MM inventory: the MM's ledger must not keep a phantom holding
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.fill("A", 3000, 0.50, "mm")
    e.ev()
    e.fill("A", -3000, 0.49, "raise")
    e.set_touch("A", 0.37, 0.39)                                  # the price moves 12 ticks after the sale
    e.ev()
    e.ev()
    L, _, _ = e.stops.ledgers()
    check("raise: a clip closes the MM's own holding (no phantom position, no pause on the later move)",
          abs(L["mm"].pos.get("A", 0.0)) < 1e-9 and "mm" not in e.kills(), (L["mm"].pos, sorted(e.kills())))
    e.close()
    # one bad snapshot breaching rule 1 and the backstop in the same evaluation: still only a hold
    e = Env()
    e.set_touch("C", 0.49, 0.51)
    e.fill("C", 3000, 0.49, "mm")
    e.ev()
    e.set_touch("C", 0.20, 0.51)                                  # a 900 drop on one read
    e.ev()
    e.set_touch("C", 0.49, 0.51)
    e.ev()
    check("hold: a one-snapshot breach of both the stop and the backstop is not written", not e.kills(), sorted(e.kills()))
    e.close()
    # a pause decided while the file does not parse is kept in force and written once it is fixed
    e = Env()
    kp = os.path.join(e.params, SM.KILL_FILE)
    SM._write_json(kp, {"cheapfav": {"reason": "monitor", "ts": e.now}})
    e.ev()
    open(kp, "w").write('{"cheapfav": {"reason": "monitor"},\n}')    # mid-edit: does not parse
    e.ev(markout=(-2.0, 30))
    still = open(kp).read()
    held = e.stops.hold.get("mm", 0) > e.now
    SM._write_json(kp, {"cheapfav": {"reason": "monitor"}})
    e.ev()
    k = e.kills()
    check("kill file mid-edit: nothing written then, the new pause holds in memory, and is written once it parses",
          still.endswith(",\n}") and held and "mm" in k and "cheapfav" in k, (still[-20:], held, sorted(k)))
    e.close()
    # markout does not re-pause the MM right after a human clears it
    e = Env()
    e.ev(markout=(-2.0, 30))
    e.now += 150
    SM._write_json(os.path.join(e.params, SM.KILL_FILE), {})
    e.ev(markout=(-2.0, 30))
    e.ev(markout=(-2.0, 30))
    check("markout: a cleared MM is not re-paused by the same 3-h markout window", "mm" not in e.kills(), sorted(e.kills()))
    e.close()
    # a fill ingested before its order is recorded is judged after the order is known (an exit crossing is not missed)
    e = Env()
    e.fill("A", 100, 0.5, "mm")
    e.ev(dt=120)
    oid = e.order("A", None, record=False)
    e.fill("A", -150, 0.5, oid=oid)
    e.ev(dt=1)                                                    # ingested, order not recorded yet: not judged
    e.store.record_order(oid, "A", None, "no", "buy", 0.5, 150, "exit")
    e.ev(dt=120)
    e.ev(dt=100)
    e.ev(dt=100)
    check("judge lag: an exit whose order is recorded after its fill still trips the crossing rule", "exit:zero_ev" in e.kills(),
          sorted(e.kills()))
    e.close()


def t_round2_d():
    # quarantine only where the paused strategy traded in the last hour
    e = Env()
    e.set_touch("A", 0.59, 0.61)
    e.set_touch("B", 0.59, 0.61)
    e.fill("A", 1000, 0.6, "cheapfav")
    e.ev(dt=4000)                                                 # A bought more than an hour ago
    e.fill("B", 1000, 0.6, "cheapfav")
    e.ev(dt=120)
    e.set_touch("A", 0.30, 0.32)
    e.set_touch("B", 0.30, 0.32)
    e.ev()
    e.ev()
    q = set(e.stops.quarantine)
    check("quarantine: only legs the paused strategy traded in the last hour", "cheapfav" in e.kills() and q == {"B"}, (sorted(e.kills()), q))
    e.close()
    # the wide backstop: the raise ledger falling 2,000 within an hour stops the raise path
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.fill("A", 10000, 0.5, "raise_set")
    e.ev()
    e.set_touch("A", 0.29, 0.31)
    e.ev()
    e.ev()
    check("wide: a 2,000 fall of the (no-limit) raise ledger pauses exit:raise", "exit:raise" in e.kills(), sorted(e.kills()))
    e.close()
    # localisation never pauses an exit path or election night
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.ev()
    e.order("A", "exit", placed=e.now + 1)
    e.order("A", "election", placed=e.now + 1)
    e.order("A", "mm", placed=e.now + 1)
    for i in range(12):
        e.fill("A", 2000, 0.51, oid=97000 + 2 * i, record=False)
        e.fill("A", -2000, 0.49, oid=97001 + 2 * i, record=False)
        e.ev()
        if "mm" in e.kills():
            break
    k = e.kills()
    check("localisation: pauses the MM there, never exit:zero_ev or election", "mm" in k and "exit:zero_ev" not in k and "election" not in k,
          sorted(k))
    e.close()
    # an unwind ending long by exactly the MM's own long fill is not a crossing
    e = Env()
    e.fill("A", -1000, 0.6, "arb")
    e.fill("B", -1000, 0.3, "arb")
    e.ev(dt=120)
    e.fill("A", 200, 0.6, "mm")                                   # an MM bid fills after the unwind read the positions
    e.store.record_arb("R", "unwind_set", [], 1000, 0, 0, True, {"results": [{"data": {"orderId": 5151, "exchangeId": "A"}}]})
    e.fill("A", 1000, 0.6, oid=5151)
    e.ev(dt=120)
    check("unwind: ending long by the MM's own fill is not a crossing", "exit:unwinds" not in e.kills(), sorted(e.kills()))
    e.close()


def t_wind_down():
    # a wind-down leg clip (purpose exit) sells the venue's holding beyond the sets, more than any sleeve owns: no crossing
    e = Env()
    e.venue_trade("A", 500, 0.5)                                  # a legacy holding no ledger owns
    e.fill("A", 300, 0.5, oid=60001, record=False)                # ... recorded as untagged
    e.fill("A", 200, 0.5, "mm")
    e.ev(dt=120)
    e.fill("A", -900, 0.5, "exit")                                # the clip sells all 1,000 (venue 1,000 -> 100)
    e.ev(dt=120)
    check("wind-down: a leg clip that only reduces the account's position is not a crossing", "exit:zero_ev" not in e.kills(),
          sorted(e.kills()))
    e.close()
    # a wind-down set clip below payout is not charged to set arb
    e = Env()
    e.set_touch("A", 0.59, 0.61)
    e.set_touch("B", 0.39, 0.41)
    e.fill("A", -5000, 0.6, "arb")
    e.fill("B", -5000, 0.4, "arb")
    e.ev(dt=120)
    for ex, px in (("A", 0.63), ("B", 0.43)):                     # sold back 6 ticks under payout: -300
        e.fill(ex, 5000, px, "wind_down")
    for _ in range(3):
        r = e.ev(dt=60)
    check("wind-down: set clips below payout are not charged to set arb", "arb" not in e.kills() and abs(r["status"]["pnl"].get("arb", 0)) < 1,
          (sorted(e.kills()), r["status"]["pnl"]))
    e.close()


def t_round3():
    # election night buying stale asks: its touch value falls 2,000+ by design; nothing pauses it (no limit set)
    e = Env()
    e.set_touch("A", 0.78, 0.80)
    e.ev()
    e.fill("A", 28000, 0.88, "election")                          # bought up to fair while the bid stays stale
    for _ in range(4):
        e.ev(dt=60)
    check("election: its own trade falling 2,000+ at the touch pauses nothing (only a stop_limits entry would)", not e.kills(), sorted(e.kills()))
    e.close()
    # clearing a rule-1b pause sticks
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.fill("A", 10000, 0.5, "raise_set")
    e.ev()
    e.set_touch("A", 0.29, 0.31)
    e.ev(dt=61)
    e.ev(dt=61)
    paused = "exit:raise" in e.kills()
    e.now += 150
    SM._write_json(os.path.join(e.params, SM.KILL_FILE), {})
    for _ in range(3):
        e.ev(dt=61)
    check("wide: a cleared exit:raise is not re-paused by the same fall", paused and "exit:raise" not in e.kills(), (paused, sorted(e.kills())))
    e.close()
    # an exit that takes a flat account short is a crossing, even when the MM traded both sides
    e = Env()
    e.fill("A", 300, 0.5, "mm")
    e.fill("A", -300, 0.5, "mm")
    e.ev(dt=120)
    e.fill("A", -300, 0.5, "exit")
    e.ev(dt=120)
    e.ev(dt=100)
    e.ev(dt=100)
    check("crossing: an exit from a flat account (MM bought and sold 300) is caught", "exit:zero_ev" in e.kills(), sorted(e.kills()))
    e.close()
    # a held leg that leaves the read with no fill of ours: a settlement (the quiet period starts)
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.fill("A", -1000, 0.5, "arb")
    e.ev(dt=200)
    e.pos.pop("A")
    r = e.ev(dt=60)
    first = r["status"]["unattributed"] != "paused after a settlement"
    r = e.ev(dt=60)
    check("settlement: gone on one read is not yet a settlement (its fill may be unread)", first)
    check("settlement: a held leg vanishing without our fills on two reads starts the quiet period", r["status"]["unattributed"] == "paused after a settlement",
          r["status"])
    e.close()


def t_round4():
    # a first-breach hold that lapses before a delayed evaluation: MM requotes after the lapse are not a bypass
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.fill("A", 3000, 0.49, "mm")
    e.ev()
    e.set_touch("A", 0.37, 0.39)                                  # -360: the MM is held
    e.ev(dt=30)
    t_hold = e.now
    for dt in (170, 180, 190):                                    # the hold lapsed at +150: requotes resume legitimately
        e.order("A", "mm", placed=t_hold + dt)
    e.set_touch("A", 0.49, 0.51)                                  # and the breach is gone by the next evaluation
    e.ev(dt=200)
    check("hold lapse: MM requotes after a lapsed hold are not a bypass (no KILL-ENTRIES)", "all" not in e.kills(), sorted(e.kills()))
    e.close()
    # a clear and a reported bypass survive a restart
    e = Env()
    e.ev(markout=(-2.0, 30))
    ts = e.kills()["mm"]["ts"]
    for dt in (100, 130, 160):
        e.order("A", "mm", placed=ts + dt)
    e.ev()
    kp = os.path.join(e.params, SM.KILL_FILE)
    had_all = "all" in e.kills()
    e.now += 150
    d = SM._read_json(kp, {})
    d.pop("all", None)
    SM._write_json(kp, d)
    e.ev()
    e.stops = e.new()                                             # a restart (a fix deployed)
    e.ev()
    e.ev()
    check("restart: a bypass already reported and cleared does not re-fire after a restart", had_all and "all" not in e.kills(),
          (had_all, sorted(e.kills())))
    e.close()


def t_shock():
    # shock's unknown-outcome orders have no order row; shock_claim tags their fills through fills.purpose
    e = Env()
    e.set_touch("A", 0.49, 0.51)
    e.ev()
    e.fill("A", 4000, 0.53, oid=70001, record=False)              # a 409 / timeout: no orders row
    e.store.exec("UPDATE fills SET purpose='shock' WHERE order_id=70001")
    L, un, _ = e.stops.ledgers()
    check("shock: a claimed unknown-outcome fill is shock's, not untagged", L["shock"].pos.get("A") == 4000 and not un.pos,
          (L["shock"].pos, un.pos))
    for _ in range(4):
        e.ev(dt=60)
    check("shock: so a walked sweep does not trip the untagged rule", not e.kills(), sorted(e.kills()))
    e.close()
    # a wind-down set clip that also closes MM NO inventory the venue netted in: no MM phantom
    e = Env()
    e.fill("A", -1000, 0.6, "arb")
    e.fill("B", -1000, 0.3, "arb")
    e.fill("A", -200, 0.6, "mm")                                  # MM NO inventory on A
    e.ev(dt=120)
    e.store.record_arb("R", "unwind_set", [], 1200, 0, 0, True, {"results": [{"data": {"orderId": 7300, "exchangeId": "A"}}]})
    oid = 7300
    e.store.record_order(oid, "A", None, "yes", "buy", 0.6, 1200, "wind_down")
    e.fill("A", 1200, 0.6, oid=oid)
    L, _, _ = e.stops.ledgers()
    check("wind-down: a set clip beyond the set ledger's holding closes the MM's inventory (no phantom)",
          abs(L["mm"].pos.get("A", 0.0)) < 1e-9 and abs(L["sets"].pos.get("A", 0.0) + 1000) < 1e-9, (L["mm"].pos, L["sets"].pos))
    e.close()


def t_unread_fill():
    e = Env()
    e.fill("A", 100, 0.5, "mm")
    e.ev(dt=120)
    e.fill("A", -100, 0.5, "exit")                                # the exit flattens the account
    e.ev(dt=120)                                                  # judged: fine
    e.venue_trade("A", -50, 0.5)                                  # the MM's ask lifted: in the venue's read, not ingested yet
    e.ev(dt=30)
    e.ev(dt=30)
    oid = e.order("A", "mm", "no", 0.5, 50)
    e.fill("A", -50, 0.5, oid=oid, venue=False)                   # ... ingested now
    for _ in range(5):
        e.ev(dt=60)
    check("unread fill: a flattening exit followed by an unread MM fill pauses nothing", not e.kills(), sorted(e.kills()))
    e.close()


def t_round6():
    # a pause removed seconds after the stops wrote it (an editor saving a stale copy) is put back once
    e = Env()
    e.ev(markout=(-2.0, 30))
    kp = os.path.join(e.params, SM.KILL_FILE)
    SM._write_json(kp, {})
    e.ev(dt=30)
    back = "mm" in e.kills()
    e.now += 150
    SM._write_json(kp, {})
    e.ev(dt=30)
    check("stale editor: a pause removed within 2 min of being written is put back once; deleted again it stays cleared",
          back and "mm" not in e.kills(), (back, sorted(e.kills())))
    e.close()
    # a crossing is not confirmed in a cycle whose fills read failed
    e = Env()
    e.fill("A", 100, 0.5, "mm")
    e.ev(dt=120)
    e.fill("A", -150, 0.5, "exit")
    e.ev(dt=120)
    for _ in range(4):
        e.ev(dt=100, fills_ok=False)
    held = not e.kills()
    e.ev(dt=100)
    check("fills read failing: a crossing candidate is not confirmed; it is once a read succeeds", held and "exit:zero_ev" in e.kills(),
          (held, sorted(e.kills())))
    e.close()
    # shock exits are not judged (clamped to the venue; shock nets its own residuals)
    e = Env()
    e.fill("A", 5000, 0.5, "shock")
    e.ev(dt=120)
    e.fill("A", -6000, 0.5, "shock_exit")
    for _ in range(4):
        e.ev(dt=100)
    check("shock: its exits are never judged, never paused", not e.kills(), sorted(e.kills()))
    e.close()


def t_round7():
    # live shapes (review 24 Oct): correct zero-EV exits on legs where cheap favourites sit too are not crossings
    for name, fills, exit_q in (
            ("1051", [("direction", -677), ("cheapfav", -878), ("mm", 67)], 610),
            ("988", [("mm", -1422), ("cheapfav", -94), ("arb", 451)], 1065),
            ("1076", [("direction", 689), ("cheapfav", 2224), ("mm", -339)], -350)):
        e = Env()
        for purp, q in fills:
            e.fill("A", q, 0.5, purp)
        e.ev(dt=120)
        e.fill("A", exit_q, 0.5, "exit")
        for _ in range(4):
            e.ev(dt=100)
        check("zero-EV: a correct exit on a %s-shaped leg (cheap favourites there too) is not a crossing" % name,
              not e.kills(), sorted(e.kills()))
        e.close()
    # untagged loss on a leg no strategy traded (a manual trade): no KILL-ENTRIES unless untagged fills keep coming
    e = Env()
    e.set_touch("B", 0.49, 0.51)
    e.ev()
    e.fill("B", 5000, 0.5, oid=81000, record=False)
    e.set_touch("B", 0.43, 0.45)                                  # -300 at the marks
    for _ in range(8):
        e.ev(dt=60)
    calm = "all" not in e.kills()
    for i in range(12):
        e.fill("B", 3000, 0.45, oid=81100 + i, record=False)       # untagged fills keep coming, losing
        e.set_touch("B", 0.43 - 0.01 * (i + 1), 0.45 - 0.01 * (i + 1))
        e.ev(dt=60)
    check("untagged with no suspects: a manual-looking loss pauses nothing; all entries once untagged fills keep losing",
          calm and "all" in e.kills(), (calm, sorted(e.kills())))
    e.close()


def t_gates():
    import sigbot.run as R
    d = tempfile.mkdtemp()
    kp = os.path.join(d, SM.KILL_FILE)
    b = types.SimpleNamespace(strategy_kill=SM.KillFile(kp), monitor_logged=set())
    SM._write_json(kp, {"arb": {"reason": "x"}})
    check("gates: an arb pause pauses recycles and offload (aliases), not the MM",
          R.strategy_paused(b, "recycle") and R.strategy_paused(b, "offload") and not R.strategy_paused(b, "mm"))
    time.sleep(0.01)
    SM._write_json(kp, {"all": {"reason": "x"}})
    os.utime(kp, (time.time() + 5, time.time() + 5))
    check("gates: 'all' pauses every entry path", all(R.strategy_paused(b, n) for n in
                                                      ("mm", "inside", "arb", "recycle", "hf_arb", "catchup", "cheapfav", "direction", "news")))
    check("gates: 'all' never pauses an exit path", not R.strategy_paused(b, "exit:zero_ev") and not R.strategy_paused(b, "exit:unwinds"))
    SM._write_json(kp, {"exit:zero_ev": {"reason": "x"}})
    os.utime(kp, (time.time() + 10, time.time() + 10))
    check("gates: an exit path is paused by its own name", R.strategy_paused(b, "exit:zero_ev") and not R.strategy_paused(b, "mm"))
    shutil.rmtree(d, ignore_errors=True)


def t_purposes_owned():
    seen = {}
    for f in glob.glob(os.path.join(ROOT, "sigbot", "**", "*.py"), recursive=True):
        tree = ast.parse(open(f).read())
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                for arg, d in zip(a.args[len(a.args) - len(a.defaults):], a.defaults):
                    if arg.arg == "purpose" and isinstance(d, ast.Constant) and isinstance(d.value, str):
                        seen.setdefault(d.value, os.path.relpath(f, ROOT))
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "record_order":
                p = n.args[7] if len(n.args) > 7 else next((k.value for k in n.keywords if k.arg == "purpose"), None)
                if isinstance(p, ast.Constant) and isinstance(p.value, str):
                    seen.setdefault(p.value, os.path.relpath(f, ROOT))
            if isinstance(n, ast.Call):                   # purpose="..." passed to any helper (unwind_set(..., purpose=))
                for k in n.keywords:
                    if k.arg == "purpose" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str):
                        seen.setdefault(k.value.value, os.path.relpath(f, ROOT))
    missing = {p: f for p, f in seen.items() if p not in ST.PURPOSE_LEDGER}
    check("every purpose the code records has an owner in strategy_stops.PURPOSE_LEDGER", not missing and len(seen) >= 8,
          missing or sorted(seen))


if __name__ == "__main__":
    for fn in (t_valuation, t_attribution, t_stop_and_rearm, t_drop, t_markout_and_sleeves, t_crossings, t_unattributed,
               t_untagged, t_stuck_and_bypass, t_pingpong, t_review_fixes, t_round2, t_round2_d, t_wind_down, t_round3, t_round4, t_shock, t_unread_fill, t_round6, t_round7, t_gates, t_purposes_owned):
        fn()
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1 if FAILS else 0)
