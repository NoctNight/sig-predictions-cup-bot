"""Behavioural failure-path tests for the gap-trade pilot (Bot._gap_trade / Bot._gap_blind in sigbot/run.py).

    python3 tests/test_gap_failure_paths.py

tests/test_guards.py checks the pilot's wiring mostly as source text. These tests DRIVE the real methods
(R.Bot.gap_cycle -> R.Bot._gap_trade -> R.Bot._gap_blind, the real Store on an in-memory SQLite, the real Universe /
risk.exposure / OwnLevels / cheap-favourites ledger) against a stub Bot whose api.place_order can time out, raise
URLError / ApiError(4xx, 5xx), return no orderId, or return an id. Nothing touches the network, the SIG API or the
shared tree; the clock is a fake one (time.time is patched for the run), so 900 s / 24 h windows are exact.

Covered (numbers are the task's):
  1 entry timeout -> _gap_blind: gap_blind holds the notional, gap_exited the cooldown, the leg is not re-entered, caps hold
  2 definite 4xx entry reject -> cooldown only, no blind
  3 ApiError 409 / 429 / 408 (and 5xx, URLError, socket errors) -> blind
  4 entry response without orderId -> blind
  5 exit that raises -> gap_exit_unknown, no second exit (even after 900 s) until the ledger shows the leg closed
  6 exit with a definite 4xx reject -> retried after 900 s, not before
  7 exit cooldown / try stamp / stop count are persisted BEFORE the call (an exception, even a BaseException, cannot skip them)
  8 three stopped legs end entries
  9 the label (gap_legs) is written before place_order
 10 (robustness) an exit is not sent for a leg the venue already shows flat
"""
from __future__ import annotations

import http.client
import logging
import os
import socket
import sys
import time
import traceback
import types
import urllib.error
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sigbot.run as R  # noqa: E402
from sigbot.api import ApiError  # noqa: E402
from sigbot.proposals import gap_pilot as GP  # noqa: E402
from sigbot.store import Store  # noqa: E402
from sigbot.universe import Leg, Race, Universe  # noqa: E402

logging.disable(logging.CRITICAL)           # the pilot logs every failure at warning / error: keep the test output readable

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ------------------------------------------------------------------------------------------------ fake clock
BASE = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc).timestamp()
CLOCK = [BASE]
_real_time = time.time
H1, H24 = 3600.0, 24 * 3600.0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------------------------------------ fake venue / api
class Abort(BaseException):
    """Not an Exception: what `except Exception` in the pilot cannot catch (a kill signal turned into SystemExit, say)."""


class Venue:
    """api stub. entry_mode / exit_mode: "ok" (an order id), "noid" ({}), "none" (None), an exception instance (raised),
    or a callable returning the response / raising."""

    def __init__(self):
        self.calls, self.seq, self.hook = [], 5000, None
        self.entry_mode = self.exit_mode = "ok"
        self.keyn = 0

    def new_key(self, prefix="sb"):
        self.keyn += 1
        return f"{prefix}-{self.keyn}"

    def place_order(self, exchange_id, side, action, quantity, price, expiration=None, tournament_id=None, key=None, retries=None):
        kind = "exit" if str(key).startswith("gpx") else "entry"
        rec = dict(ex=str(exchange_id), side=side, action=action, qty=int(quantity), px=float(price), expiration=expiration, key=key,
                   t=CLOCK[0], kind=kind)
        self.calls.append(rec)
        if self.hook:
            self.hook(rec)
        mode = self.exit_mode if kind == "exit" else self.entry_mode
        if isinstance(mode, BaseException):
            raise mode
        if callable(mode):
            return mode()
        if mode == "ok":
            self.seq += 1
            rec["order_id"] = self.seq
            return {"orderId": self.seq, "status": "open"}
        if mode == "noid":
            return {}
        if mode == "none":
            return None
        raise AssertionError(mode)

    def of(self, kind, ex=None):
        return [c for c in self.calls if c["kind"] == kind and (ex is None or c["ex"] == ex)]


# ------------------------------------------------------------------------------------------------ the harness
RACES = {"Ohio Governor": ("n1", "n2"), "Iowa Governor": ("g1", "g2"), "Kansas Governor": ("k1", "k2"), "Maine Governor": ("m1", "m2")}
SAME = {e for pair in RACES.values() for e in pair}


def book(bid, ask, qty=5000):
    return {"bids": [{"price": bid, "quantity": qty}], "asks": [{"price": ask, "quantity": qty}], "feed_ts": CLOCK[0]}


class H:
    """One pilot + one stub Bot. restart() builds a new Bot stub on the same store / venue (a bot restart)."""

    def __init__(self, rows_on=tuple(RACES), budget=900.0, leg_cap=300.0, order_notional=150.0, net_max=500.0, cash=20000.0,
                 asks=None, edges=None):
        CLOCK[0] = BASE
        self.store = Store(":memory:")
        self.venue = Venue()
        self.rows_on = list(rows_on)
        self.asks = dict(asks or {})                  # race -> the D leg's SIG ask (default 0.55)
        self.edges = dict(edges or {})                # race -> gap between the fair and that ask (default 5c)
        self.books = {}                               # exchange -> book override (held legs in the exit tests)
        self.positions = {"zz": 0.0}                  # the venue's positions (YES-signed), never empty
        self.cash, self.fill_id = cash, 0
        self.kw = dict(budget=budget, leg_cap=leg_cap, order_notional=order_notional, net_max=net_max)
        self.gp = GP.GapPilot(same_terms=set(SAME), last_entry=BASE + 1e6, hard_exit_ts=BASE + 1e7, headroom=1000.0, **self.kw)
        self.universe = Universe()
        for name, (d, r) in RACES.items():
            self.universe.races[name] = Race(name=name, legs={"D": Leg("D", "m" + d, d, "t"), "R": Leg("R", "m" + r, r, "t")})
            self.universe.by_exchange[d], self.universe.by_exchange[r] = (name, "D"), (name, "R")
        self.restart()

    # ---- the Bot stub: only what _gap_trade touches, the methods are the REAL ones bound to it
    def restart(self):
        h = self
        b = types.SimpleNamespace(
            gap=self.gp, dry_run=False, store=self.store, universe=self.universe, api=self.venue, direction=types.SimpleNamespace(rows=[]),
            trade_lock=__import__("threading").Lock(), open_orders_cache={}, open_orders_ts=0.0, positions_snapshot=None,
            cash=self.cash, mm=types.SimpleNamespace(blocked=set()),
            cfg=types.SimpleNamespace(cheapfav_budget=15000.0, cheapfav_cash_floor=1000.0, churn_cash_floor=0.0,
                                      mm=types.SimpleNamespace(portfolio_max_partisan_notional=5000.0)),
            fresh_book=lambda ex, age=None: h.books.get(str(ex)) or h.default_book(str(ex)),
            fresh_positions=lambda: dict(h.positions))
        for name in ("gap_cycle", "_gap_trade", "_gap_blind", "cheapfav_refresh"):
            setattr(b, name, types.MethodType(getattr(R.Bot, name), b))
        self.bot = b
        return b

    # ---- market data
    def row_set(self, race):
        d, r = RACES[race]
        ask = self.asks.get(race, 0.55)
        fair = round(ask + self.edges.get(race, 0.05), 4)
        # D: SIG sits `edge` under the fair (buy YES at the ask). R leg priced to the same market: its NO route is 1c worse
        return [dict(race=race, party="D", exchange_id=d, fair=fair, sig_bid=round(ask - 0.02, 4), sig_ask=ask, pm=fair, kalshi=fair),
                dict(race=race, party="R", exchange_id=r, fair=round(1 - fair, 4), sig_bid=round(1 - ask - 0.01, 4),
                     sig_ask=round(1 - ask + 0.02, 4), pm=round(1 - fair, 4), kalshi=round(1 - fair, 4))]

    def rows(self):
        return [x for race in self.rows_on for x in self.row_set(race)]

    def default_book(self, ex):
        for race in self.rows_on:
            for r in self.row_set(race):
                if r["exchange_id"] == ex:
                    return book(r["sig_bid"], r["sig_ask"])
        return None

    # ---- ledger / positions
    def label(self, ex, age_h=1.0):
        legs = dict(self.store.get_param("gap_legs", {}) or {})
        legs[ex] = CLOCK[0] - age_h * H1
        self.store.set_param("gap_legs", legs)

    def add_fill(self, ex, qty_yes, price_yes, purpose="cheapfav", order_id=None):
        self.fill_id += 1
        self.store.exec("INSERT INTO fills(id,order_id,exchange_id,side,price,quantity,filled_at,purpose,ingested_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (self.fill_id, order_id, ex, "yes" if qty_yes > 0 else "no", price_yes, qty_yes, iso(CLOCK[0]), purpose, CLOCK[0]))

    def hold(self, ex, qty=300, price_yes=0.55, age_h=1.0):
        """A pilot leg in the ledger (fills with the cheapfav purpose), labelled in gap_legs, and on the venue."""
        self.add_fill(ex, qty, price_yes)
        self.positions[ex] = self.positions.get(ex, 0.0) + qty
        self.label(ex, age_h)

    def close_leg(self, ex, purpose="cheapfav_exit"):
        q = self.positions.get(ex, 0.0)
        self.add_fill(ex, -q, 0.48, purpose=purpose)
        self.positions[ex] = 0.0

    def stop_book(self, ex, bid=0.48, ask=0.50):
        self.books[ex] = book(bid, ask)           # a YES long bought at 0.55: mid 0.49 <= 0.55 - 0.04 -> stop

    def calm_book(self, ex, bid=0.54, ask=0.56):
        self.books[ex] = book(bid, ask)

    # ---- driving
    def cycle(self, dt=90.0):
        CLOCK[0] += dt
        self.bot.cash = self.cash
        self.bot.direction.rows = self.rows()
        return R.Bot.gap_cycle(self.bot, {"polled": True}, dict(self.positions), {}, False)

    def param(self, key, default=None):
        return self.store.get_param(key, default)

    def orders(self):
        return [dict(r) for r in self.store.rows("SELECT order_id, exchange_id, side, price, quantity, purpose FROM orders ORDER BY order_id")]

    def entries(self, ex=None):
        return self.venue.of("entry", ex)

    def exits(self, ex=None):
        return self.venue.of("exit", ex)


def notional(call):
    return call["qty"] * call["px"]


# ================================================================================================ controls
def t_control():
    h = H(rows_on=["Ohio Governor"])
    h.cycle(0)
    e = h.entries()
    check("control: a clean entry is one buy-YES at the ask on the underpriced D leg, recorded as a cheapfav order with an id",
          len(e) == 1 and e[0]["ex"] == "n1" and e[0]["side"] == "yes" and e[0]["action"] == "buy" and abs(e[0]["px"] - 0.55) < 1e-9
          and e[0]["qty"] >= 20 and [o["purpose"] for o in h.orders()] == ["cheapfav"], str(e))
    check("control: a clean entry leaves no blind notional and no cooldown, and labels the leg",
          not h.param("gap_blind") and not h.param("gap_exited") and "n1" in (h.param("gap_legs") or {}))
    h.cycle(90)
    check("control: while its order is in flight the leg is busy (no second order 90 s later)", len(h.entries()) == 1, str(h.venue.calls))

    h = H(rows_on=[])
    h.hold("n1")
    h.stop_book("n1")
    h.cycle(0)
    x = h.exits()
    check("control: a stopped leg is sold at the touch (buy NO at 1 - bid for the whole ledger quantity) and recorded as cheapfav_exit",
          len(x) == 1 and x[0]["side"] == "no" and x[0]["qty"] == 300 and abs(x[0]["px"] - 0.52) < 1e-9
          and [o["purpose"] for o in h.orders()] == ["cheapfav_exit"], str(x))
    check("control: that exit is flagged neither unknown nor blind; the stop and the cooldown are counted",
          not h.param("gap_exit_unknown") and not h.param("gap_blind") and h.param("gap_stops") == ["n1"] and "n1" in (h.param("gap_exited") or {}))


# ================================================================================================ 1
def t1_entry_timeout():
    h = H(rows_on=["Ohio Governor"])
    h.venue.entry_mode = TimeoutError("timed out")
    h.cycle(0)
    e = h.entries()
    n = notional(e[0]) if e else 0.0
    blind = h.param("gap_blind") or {}
    check("1: entry place_order TimeoutError: one call, gap_blind holds that order's whole notional (qty x limit)",
          len(e) == 1 and set(blind) == {"n1"} and abs(blind["n1"]["n"] - n) < 1e-6 and n > 100, f"{e} {blind}")
    check("1: ... gap_exited has the 24 h cooldown for the leg (stamped now), gap_legs keeps the label, nothing recorded as an order",
          abs((h.param("gap_exited") or {}).get("n1", 0) - CLOCK[0]) < 1e-6 and "n1" in (h.param("gap_legs") or {}) and h.orders() == [],
          f"{h.param('gap_exited')} {h.param('gap_legs')} {h.orders()}")
    # the venue now answers: if the pilot is still willing, an order goes out
    h.venue.entry_mode = "ok"
    for dt in (90, 90, 300, 3600, 2 * 3600, 8 * 3600):
        h.cycle(dt)
    check("1: the blind leg is not re-entered on later cycles (90 s ... 11 h later), nor is the other leg of its race, though the 5c gap is still there",
          len(h.entries()) == 1, str(len(h.entries())))
    h.bot = h.restart()
    h.cycle(3 * 3600)      # 14 h after the timeout
    check("1: ... and not after a bot restart either (blind and cooldown live in the store)", len(h.entries()) == 1)
    h.cycle(10 * H1 + 60)  # 24 h + 1 min after the timeout: the cooldown has lapsed
    check("1: control for the above: once the 24 h cooldown has lapsed the leg is a candidate again (so the earlier silence came from the cooldown)",
          len(h.entries()) == 2, str(len(h.entries())))

    # caps: budget 200, two races time out (149.6 + 50) -> a third race has no room, because the blind notional counts
    h = H(rows_on=["Ohio Governor", "Iowa Governor"], budget=200.0, asks={"Iowa Governor": 0.50}, edges={"Iowa Governor": 0.045})
    h.venue.entry_mode = TimeoutError("timed out")
    h.cycle(0)
    e = h.entries()
    blind = h.param("gap_blind") or {}
    check("1 caps: two timed-out entries in one cycle are both blind, sized inside the 200 budget (the second gets only what the first left)",
          len(e) == 2 and set(blind) == {"n1", "g1"} and sum(b["n"] for b in blind.values()) <= 200 + 1e-6 and abs(sum(b["n"] for b in blind.values()) - sum(notional(c) for c in e)) < 1e-6,
          f"{e} {blind}")
    h.venue.entry_mode = "ok"
    h.rows_on = ["Kansas Governor", "Maine Governor"]
    h.cycle(120)
    check("1 caps: budget 200 is used up by the blind notional, so a new race gets no order",
          len(h.entries()) == 2 and sum(b["n"] for b in blind.values()) > 199, f"{len(h.entries())} {blind}")

    # leg cap: 200 per leg; 149.6 blind; after the cooldown lapses the leg may take only the rest
    h = H(rows_on=["Ohio Governor"], leg_cap=200.0)
    h.venue.entry_mode = TimeoutError("timed out")
    h.cycle(0)
    first = notional(h.entries()[0])
    h.venue.entry_mode = "ok"
    h.cycle(H24 + 60)                      # cooldown lapsed; the label (6 h) is gone, the blind notional stays
    e = h.entries()
    check("1 caps: after the cooldown the leg cap (200) still counts the blind notional: the next order is at most 200 - blind",
          len(e) == 2 and notional(e[1]) <= 200.0 - first + 1e-6, f"first {first:.1f}, second {notional(e[1]) if len(e) > 1 else None}")

    # partisan own-net band: blind D notional counts in the budget and leg caps; does it count in the +-net_max band?
    h = H(rows_on=list(RACES), budget=2000.0, net_max=450.0)
    h.venue.entry_mode = TimeoutError("timed out")
    h.cycle(0)                             # three D-YES entries (max 3 per cycle), each ~150, all blind: ~449 of possible net D
    blind_total = sum(b["n"] for b in (h.param("gap_blind") or {}).values())
    h.venue.entry_mode = "ok"
    h.rows_on = ["Maine Governor"]
    h.cycle(120)                           # a fourth D entry: net_max 450 is already used by the three blind ones if they filled
    check("1 caps (partisan): blind D entries count against the pilot's own +-net_max band (3 blind D = %.0f of 450, a 4th D order must not go out)" % blind_total,
          len(h.entries()) == 3, f"orders sent: {len(h.entries())}, blind total {blind_total:.0f}")


# ================================================================================================ 2
def t2_definite_reject():
    for status in (400, 401, 403, 404, 422):
        h = H(rows_on=["Ohio Governor"])
        h.venue.entry_mode = ApiError(status, "REJECTED", "no")
        h.cycle(0)
        ok = (len(h.entries()) == 1 and not h.param("gap_blind") and abs((h.param("gap_exited") or {}).get("n1", 0) - CLOCK[0]) < 1e-6
              and h.orders() == [])
        check(f"2: ApiError {status} on an entry: cooldown only (gap_exited), no gap_blind, nothing recorded", ok,
              f"{h.param('gap_blind')} {h.param('gap_exited')}")
    h = H(rows_on=["Ohio Governor"])
    h.venue.entry_mode = ApiError(400, "REJECTED", "no")
    h.cycle(0)
    h.venue.entry_mode = "ok"
    for dt in (90, 600, 3600, 12 * H1):
        h.cycle(dt)
    check("2: a rejected leg is not hammered: no new order for 24 h", len(h.entries()) == 1)
    h.cycle(12 * H1 + 120)
    check("2: ... and the leg is tried again once the cooldown lapses (the reject was definite, nothing is counted against the caps)",
          len(h.entries()) == 2 and not h.param("gap_blind"))
    # cooldown for one rejected leg must not erase another leg's blind record from the same cycle (the 4xx path re-reads the store)
    h = H(rows_on=["Ohio Governor", "Iowa Governor"], asks={"Iowa Governor": 0.50}, edges={"Iowa Governor": 0.045})
    seq = iter([TimeoutError("t"), ApiError(400, "R", "no")])

    def mode():
        raise next(seq)
    h.venue.entry_mode = mode
    h.cycle(0)
    cool, blind = h.param("gap_exited") or {}, h.param("gap_blind") or {}
    check("2: a blind leg and a rejected leg in one cycle both keep their cooldown, and only the blind one holds notional",
          set(cool) == {"n1", "g1"} and set(blind) == {"n1"}, f"{cool} {blind}")


# ================================================================================================ 3
def t3_ambiguous_entry():
    errors = [("ApiError 409", ApiError(409, "CONFLICT", "duplicate")), ("ApiError 429", ApiError(429, "RATE", "slow down")),
              ("ApiError 408", ApiError(408, "TIMEOUT", "t")), ("ApiError 500", ApiError(500, "ERR", "boom")),
              ("ApiError 503", ApiError(503, "UNAVAILABLE", "x")), ("urllib URLError", urllib.error.URLError("unreachable")),
              ("socket.timeout", socket.timeout("t")), ("ConnectionResetError", ConnectionResetError("reset")),
              ("http.client.RemoteDisconnected", http.client.RemoteDisconnected("closed")), ("ValueError (bad JSON)", ValueError("x")),
              ("RuntimeError", RuntimeError("x"))]
    for label, err in errors:
        h = H(rows_on=["Ohio Governor"])
        h.venue.entry_mode = err
        h.cycle(0)
        e = h.entries()
        blind = h.param("gap_blind") or {}
        check(f"3: {label} on an entry -> blind (notional counted, cooldown set, label kept)",
              len(e) == 1 and abs(blind.get("n1", {"n": 0})["n"] - notional(e[0])) < 1e-6 and abs((h.param("gap_exited") or {}).get("n1", 0) - CLOCK[0]) < 1e-6
              and "n1" in (h.param("gap_legs") or {}), f"{blind} {h.param('gap_exited')}")


# ================================================================================================ 4
def t4_no_orderid():
    for label, mode in (("{} (no orderId)", "noid"), ("None (no body)", "none")):
        h = H(rows_on=["Ohio Governor"])
        h.venue.entry_mode = mode
        h.cycle(0)
        e = h.entries()
        blind = h.param("gap_blind") or {}
        check(f"4: entry response {label}: blind (notional counted), cooldown set, no order row (nothing to attribute the fill to)",
              len(e) == 1 and abs(blind.get("n1", {"n": 0})["n"] - notional(e[0])) < 1e-6 and abs((h.param("gap_exited") or {}).get("n1", 0) - CLOCK[0]) < 1e-6
              and h.orders() == [], f"{blind} {h.param('gap_exited')} {h.orders()}")
        h.venue.entry_mode = "ok"
        h.cycle(120)
        h.cycle(1800)
        check(f"4: ... and the leg is not bought again (response {label})", len(h.entries()) == 1)


# ================================================================================================ 5
def t5_exit_unknown():
    h = H(rows_on=[])
    h.hold("n1")
    h.stop_book("n1")
    h.venue.exit_mode = TimeoutError("timed out")
    h.cycle(0)
    x0 = len(h.exits())
    check("5: exit place_order TimeoutError: one call; gap_exit_unknown, gap_exit_try and the cooldown are set; no order row",
          x0 == 1 and "n1" in (h.param("gap_exit_unknown") or {}) and "n1" in (h.param("gap_exit_try") or {})
          and "n1" in (h.param("gap_exited") or {}) and h.orders() == [], f"{h.param('gap_exit_unknown')}")
    h.venue.exit_mode = "ok"                                  # were a second exit allowed it would now succeed (and show up as a call)
    for dt in (90, 400, 410, 1, 600, 3000):                   # 90 ... ~4,500 s: well past the 900 s retry window
        h.cycle(dt)
    check("5: no second exit for the leg while the ledger still shows it, not after 900 s and not after 75 min",
          len(h.exits()) == 1 and CLOCK[0] - BASE > 4000, f"{len(h.exits())} exits")
    h.bot = h.restart()
    h.cycle(3600)
    check("5: ... nor after a bot restart (gap_exit_unknown lives in the store)", len(h.exits()) == 1)
    h.close_leg("n1")                                          # the ledger now shows the leg closed
    h.cycle(120)
    check("5: once the ledger shows the leg closed there is nothing to sell (no exit)", len(h.exits()) == 1)
    # the block must be gone for good: the same leg held again later (after its 24 h cooldown the pilot may re-enter it) must be sellable
    h.cycle(H24)
    h.hold("n1", qty=200, price_yes=0.55, age_h=0.0)
    h.stop_book("n1")
    h.cycle(120)
    check("5: and it stays cleared: the same leg bought again later is sold at its stop (the old unknown-exit mark must not outlive the closed ledger)",
          len(h.exits()) == 2, f"{len(h.exits())} exits; gap_exit_unknown param = {h.param('gap_exit_unknown')}")

    # every ambiguous exit outcome blocks; every definite 4xx does not
    cases = [("TimeoutError", TimeoutError("t"), True), ("URLError", urllib.error.URLError("x"), True),
             ("ApiError 500", ApiError(500, "E", "x"), True), ("ApiError 503", ApiError(503, "E", "x"), True),
             ("ApiError 409", ApiError(409, "C", "x"), True), ("ApiError 429", ApiError(429, "R", "x"), True),
             ("ApiError 408", ApiError(408, "T", "x"), True), ("no orderId ({})", "noid", True), ("None response", "none", True),
             ("ApiError 400", ApiError(400, "B", "x"), False), ("ApiError 403", ApiError(403, "B", "x"), False),
             ("ApiError 404", ApiError(404, "B", "x"), False), ("ApiError 422", ApiError(422, "B", "x"), False)]
    for label, mode, unknown in cases:
        h = H(rows_on=[])
        h.hold("n1")
        h.stop_book("n1")
        h.venue.exit_mode = mode
        h.cycle(0)
        got = "n1" in (h.param("gap_exit_unknown") or {})
        check(f"5/6: exit answered with {label}: {'outcome unknown (blocked)' if unknown else 'definite reject (not blocked)'}", got == unknown,
              f"unknown={got}")


# ================================================================================================ 6
def t6_exit_reject_retry():
    h = H(rows_on=[])
    h.hold("n1")
    h.stop_book("n1")
    h.venue.exit_mode = ApiError(400, "INSUFFICIENT", "no")
    h.cycle(0)
    check("6: exit rejected with a definite 4xx: not unknown, but stamped (gap_exit_try) and on cooldown", len(h.exits()) == 1
          and not h.param("gap_exit_unknown") and "n1" in (h.param("gap_exit_try") or {}) and "n1" in (h.param("gap_exited") or {}))
    h.venue.exit_mode = "ok"
    for dt in (60, 300, 400, 139):                             # 60, 360, 760, 899 s after the reject
        h.cycle(dt)
    check("6: not retried 899 s after the reject", len(h.exits()) == 1 and abs(CLOCK[0] - BASE - 899) < 1e-6, f"{len(h.exits())} exits at +{CLOCK[0] - BASE:.0f}")
    h.cycle(2)                                                 # 901 s
    x = h.exits()
    check("6: retried after 900 s (one more exit, same size, now accepted and recorded as cheapfav_exit)",
          len(x) == 2 and x[1]["qty"] == 300 and [o["purpose"] for o in h.orders()] == ["cheapfav_exit"], f"{len(x)} exits at +{CLOCK[0] - BASE:.0f}")
    h.cycle(90)
    h.cycle(300)
    check("6: and after the accepted retry it waits again (busy, then the 900 s spacing): no third exit within 6.5 min", len(h.exits()) == 2)
    # the same through a restart: the stamp is in the store
    h = H(rows_on=[])
    h.hold("n1")
    h.stop_book("n1")
    h.venue.exit_mode = ApiError(400, "INSUFFICIENT", "no")
    h.cycle(0)
    h.bot = h.restart()
    h.venue.exit_mode = "ok"
    h.cycle(120)
    check("6: ... also across a bot restart (no retry 120 s after the reject)", len(h.exits()) == 1)
    # a stopped leg whose exit is rejected twice is one stop, not two
    h.cycle(800)
    check("6: the same leg stopped twice (reject, then retry) counts once in gap_stops", h.param("gap_stops") == ["n1"] and len(h.exits()) == 2, str(h.param("gap_stops")))


# ================================================================================================ 7
def t7_stamped_before_the_call():
    seen = {}

    def hook(rec):
        if rec["kind"] == "exit":
            seen.update(cool=dict(h.param("gap_exited") or {}), tries=dict(h.param("gap_exit_try") or {}), stops=list(h.param("gap_stops") or []))
    h = H(rows_on=[])
    h.hold("n1")
    h.stop_book("n1")
    h.venue.hook = hook
    h.cycle(0)
    check("7: at the moment place_order is entered for an exit, the 24 h cooldown, the 900 s try stamp and the stop count are already persisted",
          abs(seen.get("cool", {}).get("n1", 0) - CLOCK[0]) < 1e-6 and abs(seen.get("tries", {}).get("n1", 0) - CLOCK[0]) < 1e-6 and seen.get("stops") == ["n1"],
          str(seen))

    # an exception that `except Exception` does not catch: the state written before the call is what is left
    h = H(rows_on=[])
    h.hold("n1")
    h.stop_book("n1")
    h.venue.exit_mode = Abort()
    try:
        h.cycle(0)
        escaped = False
    except Abort:
        escaped = True
    check("7: a BaseException from an exit's place_order escapes the pilot (it is not swallowed) ...", escaped)
    check("7: ... and the cooldown, the try stamp and the stop count were persisted before it", abs((h.param("gap_exited") or {}).get("n1", 0) - CLOCK[0]) < 1e-6
          and "n1" in (h.param("gap_exit_try") or {}) and h.param("gap_stops") == ["n1"])
    h.bot = h.restart()
    h.venue.exit_mode = "ok"
    h.cycle(60)
    h.cycle(500)
    check("7: ... so the restarted bot does not send the exit again within 900 s", len(h.exits()) == 1)

    # entries: the label is the only thing written before the call; show what a hard kill (BaseException) leaves behind
    h = H(rows_on=["Ohio Governor"])
    h.venue.entry_mode = Abort()
    try:
        h.cycle(0)
    except Abort:
        pass
    state = (dict(h.param("gap_legs") or {}), dict(h.param("gap_blind") or {}), dict(h.param("gap_exited") or {}))
    check("7: an entry cut off by a BaseException keeps its label in gap_legs (cooldown / blind are written in the handler, see 1)",
          "n1" in state[0], str(state))


# ================================================================================================ 8
def t8_three_stops():
    def stopped(h, legs):
        for ex in legs:
            h.hold(ex)
            h.stop_book(ex)

    # two stops: entries go on
    h = H(rows_on=["Maine Governor"])
    stopped(h, ["n1", "g1"])
    h.cycle(0)
    check("8: two stopped legs: both exits sent, gap_stops has two, and entries still go out in the same cycle",
          len(h.exits()) == 2 and sorted(h.param("gap_stops") or []) == ["g1", "n1"] and len(h.entries()) == 1, f"exits {len(h.exits())} entries {len(h.entries())}")
    # third stop (same cycle): no entry
    h = H(rows_on=["Maine Governor"])
    stopped(h, ["n1", "g1", "k1"])
    h.cycle(0)
    check("8: three stopped legs in one cycle: three exits, gap_stops has three, and NO entry in that cycle",
          len(h.exits()) == 3 and len(h.param("gap_stops") or []) == 3 and h.entries() == [], f"exits {len(h.exits())} entries {len(h.entries())}")
    for dt in (90, 1800, 7 * H1, 2 * H24):
        h.cycle(dt)
    check("8: ... and none later (90 s ... 2 days: the pilot's own kill switch is permanent), nor after a restart",
          h.entries() == [], str(len(h.entries())))
    h.bot = h.restart()
    h.cycle(90)
    check("8: ... nor after a bot restart (gap_stops is in the store)", h.entries() == [])
    # stops one per cycle, over time (each exit fills and reaches the ledger before the next cycle)
    h = H(rows_on=["Maine Governor"])
    h.hold("n1"); h.stop_book("n1")
    h.cycle(0)
    e1 = len(h.entries())
    h.close_leg("n1")
    h.hold("g1"); h.stop_book("g1")
    h.cycle(1000)
    e2 = len(h.entries())
    h.close_leg("g1")
    h.hold("k1"); h.stop_book("k1")
    h.cycle(1000)
    e3 = len(h.entries())
    h.close_leg("k1")
    h.cycle(1000)
    h.cycle(H24)
    check("8: stops arriving one cycle apart: entries go on after the 1st and 2nd stop (Maine is bought again each cycle) and end for good with the 3rd",
          sorted(h.param("gap_stops") or []) == ["g1", "k1", "n1"] and len(h.exits()) == 3 and e1 >= 1 and e2 > e1 and e3 == e2 and len(h.entries()) == e2,
          f"entries after each cycle {e1},{e2},{e3}, final {len(h.entries())}, exits {len(h.exits())}, stops {h.param('gap_stops')}")
    # a stop is a 'stop:' exit only: time stops and gap-closed exits do not count
    h = H(rows_on=["Maine Governor"], net_max=2000.0)          # (net_max high: three held D legs would otherwise fill the +-500 band)
    for ex in ("n1", "g1", "k1"):
        h.hold(ex, age_h=80.0)                                 # past the 72 h time stop
        h.calm_book(ex)
    h.cycle(0)
    check("8: three TIME-stop exits are not stop-outs: gap_stops stays empty and entries continue",
          len(h.exits()) == 3 and not h.param("gap_stops") and len(h.entries()) == 1, f"exits {len(h.exits())} stops {h.param('gap_stops')} entries {len(h.entries())}")
    # a rejected stop exit still counts (the leg is stopped out in the market)
    h = H(rows_on=["Maine Governor"])
    stopped(h, ["n1", "g1", "k1"])
    h.venue.exit_mode = ApiError(400, "B", "x")
    h.cycle(0)
    check("8: stops whose exit orders were rejected (4xx) still count: three of them end entries", h.entries() == [] and len(h.param("gap_stops") or []) == 3)


# ================================================================================================ 9
def t9_label_first():
    seen = []

    def hook(rec):
        seen.append((rec["kind"], dict(h.param("gap_legs") or {}), dict(h.param("gap_blind") or {}), dict(h.param("gap_exited") or {})))
    h = H(rows_on=["Ohio Governor", "Iowa Governor"], asks={"Iowa Governor": 0.50}, edges={"Iowa Governor": 0.045})
    h.venue.hook = hook
    h.cycle(0)
    e = h.entries()
    check("9: at the moment place_order is entered for each entry, that leg's label (first-entry time) is already in gap_legs",
          len(e) == 2 and all(e[i]["ex"] in seen[i][1] and abs(seen[i][1][e[i]["ex"]] - CLOCK[0]) < 1e-6 for i in range(2)), str(seen))
    check("9: ... before any blind / cooldown record exists for it (those only follow a failure)", all(not s[2] and not s[3] for s in seen), str(seen))
    h = H(rows_on=["Ohio Governor"])
    h.venue.entry_mode = TimeoutError("t")
    h.cycle(0)
    check("9: and the label survives the failure (a leg that may have filled is never left unlabelled)", "n1" in (h.param("gap_legs") or {}))
    # the label is kept for 6 h even though the ledger shows nothing, then dropped; blind stays
    h.cycle(5 * H1)
    kept = "n1" in (h.param("gap_legs") or {})
    h.cycle(2 * H1)
    gone = "n1" not in (h.param("gap_legs") or {})
    check("9: a label with no ledger position is kept 6 h (late fill ingest must not orphan a leg) and dropped after, the blind notional stays",
          kept and gone and "n1" in (h.param("gap_blind") or {}), f"kept {kept} gone {gone} blind {h.param('gap_blind')}")


# ================================================================================================ 10
def t10_exit_vs_venue_position():
    # The exit's size is the LEDGER's open quantity. If the first exit filled but the fill has not reached the ledger (fills
    # ingest down for more than the 900 s spacing), the second exit buys NO through zero.
    h = H(rows_on=[])
    h.hold("n1")
    h.stop_book("n1")
    h.cycle(0)                                                 # exit 1, accepted
    h.positions["n1"] = 0.0                                    # it filled at the venue; the fills table has not seen it
    h.cycle(901 + 90)
    check("10: (robustness) no second exit for a leg the venue already shows flat (the ledger lags >15 min): it would flip the leg to NO 300",
          len(h.exits()) == 1, f"{len(h.exits())} exits; the second: {[(c['side'], c['qty'], c['px']) for c in h.exits()[1:]]}")
    h.cycle(2700 + 120)                                        # 45 min after the exit: the wait is over, the ledger still says +300
    check("10b: the wait lasts at most 45 min (10x the usual ingest lag); after that the ledger rules again",
          len(h.exits()) == 2, f"{len(h.exits())} exits")

    # another sleeve short on the same leg with a CURRENT ledger: the venue shows our side gone, but the pilot's first exit has an
    # ingested fill, so there is nothing to wait for; the next stop exit must go out after the 900 s spacing (review 29 Oct: a
    # venue-sign wait stalled legs the market maker was short on for 6 h at a time)
    h = H(rows_on=[])
    h.hold("n1")
    h.stop_book("n1")
    h.cycle(0)                                                 # exit 1 (x300), accepted
    x1 = h.exits()[-1]
    h.add_fill("n1", -120, 0.48, purpose="cheapfav_exit", order_id=x1["order_id"])   # 120 filled and INGESTED, 180 left
    h.positions["n1"] = -70.0                                  # ledger +180 but the market maker is short 250 on the leg
    h.cycle(901 + 90)
    check("10c: with the latest exit's fill ingested, another sleeve's short on the leg never delays the next exit (x180 goes out)",
          len(h.exits()) == 2 and h.exits()[-1]["qty"] == 180, f"{len(h.exits())} exits; last {[(c['side'], c['qty']) for c in h.exits()[-1:]]}")

    # a partial fill whose ingest lags: the venue still shows +200 of our 300, the ledger +300: the next exit sells at most 200
    h = H(rows_on=[])
    h.hold("n1")
    h.stop_book("n1")
    h.cycle(0)
    h.positions["n1"] = 200.0                                  # 100 filled at the venue, nothing ingested
    h.cycle(901 + 90)
    check("10d: a partial fill not yet ingested caps the next exit at the venue's same-sign quantity (x200, never x300)",
          len(h.exits()) == 2 and h.exits()[-1]["qty"] == 200, f"{len(h.exits())} exits; last {[(c['side'], c['qty']) for c in h.exits()[-1:]]}")


TESTS = [t_control, t1_entry_timeout, t2_definite_reject, t3_ambiguous_entry, t4_no_orderid, t5_exit_unknown, t6_exit_reject_retry,
         t7_stamped_before_the_call, t8_three_stops, t9_label_first, t10_exit_vs_venue_position]


def main():
    time.time = lambda: CLOCK[0]
    try:
        for t in TESTS:
            try:
                t()
            except Exception:
                tb = traceback.format_exc().strip().splitlines()
                check(f"{t.__name__}: crashed", False, tb[-1] + " @ " + (tb[-3].strip() if len(tb) >= 3 else ""))
    finally:
        time.time = _real_time
    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + '; '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
