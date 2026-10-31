"""Offload executor (Sizhe 31 Oct, research_notes/offload_plan.md): sell a target's holdings in about 5 minutes, for a big
directional trade. OFF unless config offload_enabled is true, and it only logs while offload_dry_run is true (the default).

Sizhe approves a target in chat; the session then re-runs research_notes/offload_plan_study.py, puts the plan file's SHA-256
in offload_plan_sha256, sets offload_target, offload_speed, offload_start and offload_stop, and only then offload_enabled and
offload_dry_run false. Any other bytes, an old plan, a target the plan did not reach, or a missing window are refused. The window
is what stops a flag left on from selling later; the state file stops a restart selling the list twice.

The list is plan["speeds"][speed], the row of the target, in plan order; nothing outside it is ever sold.
  Preflight  positions are read and the list clamped to what is held; if that is under 80% of the target, the sale aborts
             (the positions moved: re-run the plan).
  Test       the first basket is one MN-05 basket (else the list's first YES set; no YES set in the list, no test), because a
             YES set has never been sold live. Worse than the plan by more than the slack, a basket that will not fill, or a
             position or cash change that does not match: the whole sale aborts. Only then does anything else go.
  Sets       all-or-none baskets, min(5, ceil(sets / 500)) per race, BASKET_GAP apart, each on a fresh book. A basket that would
             price under the plan shrinks by halves, else waits. AllOrNoneNotFilled: new book, new key, up to MAX_RETRIES times.
             A buy (the opposite side of each leg, which nets) is admitted against the cash balance on its gross, so where the
             gross is above CASH_MARGIN of the balance the basket is a conventional sell of the same legs instead.
  Legs       single legs go by batch, up to BATCH_ROWS rows a write, TTL-second expiry, only the size priced at or above the plan.
Before any send, under the trade lock: our quotes on the legs are pulled (the MM stays off them for 15 min), positions are
re-read, the size is clamped to what is held now, and the partisan cap is checked: after a basket, and for a batch as if only
the orders that widen net D filled. A step the cap refuses waits; the plan's order normally clears it.
Tags (known to strategy_stops.PURPOSE_LEDGER): baskets raise_set, legs raise, the cheap-favourites sleeve's own legs
cheapfav_exit. Those are sold only while that sleeve is paused by hand ("cheapfav" in params/strategy_kill.json): else it buys
them back as its budget reopens.
While a live sale runs its races count as punt races (Bot.punt_races), so no other path sells, quotes or enters them; this is
also why set baskets do not go through arb.unwind_set, which refuses protected and cheap-favourite races. Races a punt, election,
shock or high-frequency arb position sits in are left alone (Adapter.busy).
Stops: params/offload_abort (cancels what rests), exit:raise paused or the kill switch (holds; over HELD_MAX the sale ends and its
races go back), 3 hard rejections in a row (abort), the window closing, or the target raised. All are read again under the lock.
A row that cannot sell for STUCK_MAX is given up, and a race another sleeve owns is not protected for the sale. Units sold come from
the venue's responses; the position settles only sends whose outcome was unknown or that rested, so another row of the same leg
is never mistaken for a sale. Orders of unknown outcome hold every leg until they have expired.
A list's below_payout_sets near 2,000 can trip rule 1b of the strategy stops (the raise ledger's hourly fall): 60k is about 1,840
before slack and the wind-down's own costs, 80k is over. The sale then holds and ends: raising that limit is Sizhe's decision.
A conventional sell has never been sent live (every order on record is a buy) and the fills table has no action column, so every
ledger would book it as a buy: off (the basket waits for cash) unless config offload_conventional_sell is true.
A race in wind_down_set_floor is never sold under that floor, at the worst level of a basket (the limits, not the average).

Dry run, against a book dump (sends nothing; positions come from the plan):
    python3 -m sigbot.proposals.offload_executor --target 20000
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Optional

from .punt_executor import HERE, TRANSIENT, sha256_file, ts_of
from . import wind_down as WD

log = logging.getLogger("sigbot")
TICK = 0.005
SET_TYPES = ("set_late", "set_early")
LEG_TYPES = ("leg", "mm_leg", "leg_held", "cheapfav")
TEST_RACE = "MN-05 House race"
TTL = 30.0                    # seconds a leg order rests
BASKETS = 5                   # at most this many baskets per race
BASKET_SETS = 500             # one basket per this many sets (the study's count)
BASKET_GAP = 60.0             # seconds between two baskets of a race
RETRY_GAP = 15.0              # after a basket that did not fill, or a price that waits
MAX_RETRIES = 3               # new-key retries of a basket that did not fill
MIN_BASKET = 100              # a basket shrinks no further than this (or what is left)
CASH_MARGIN = 0.9             # a buy grossing above this share of the balance goes as a conventional sell
MIN_REACHABLE = 0.8           # preflight: the list as held now must still raise this share of the target
MAX_PLAN_AGE = 6 * 3600.0     # seconds from the plan's build to the sale's start
MAX_WINDOW = 2 * 3600.0
MAX_HARD = 3                  # hard rejections in a row that abort the sale
BATCH_ROWS = 25               # rows a batch prices (each a book read, feed first)
PENDING_MAX = 100.0           # seconds an unconfirmed basket is resent with its key (past the venue's 90 s lease of a key)
LEASE = 95.0                  # a timed-out or in-flight request may still be running this long after it was sent
IN_FLIGHT_WAIT = 90.0         # the spec's wait before an in-flight key is retried
BACKOFF_MAX = 16.0
SETTLE_SEC = 4.0              # a position that must say what sold is read twice this far apart, and must agree
TEST_WAIT = 15.0              # seconds before the test basket is checked against the venue
TEST_TIMEOUT = 90.0           # ... and how long the venue gets to show it
CASH_TOL = (20.0, 200.0, 0.5) # the test's cash may miss by min(200, half of it), at least 20: other sleeves trade meanwhile
HELD_MAX = 300.0              # seconds a hold by the stops may last before the sale ends and gives its races back
STUCK_MAX = 300.0             # seconds a row may wait (price, cap, nothing read) before it is given up
FALL_LIMIT = 2000.0           # strategy_stops.WIDE_FALL


def tick_down(p: float) -> float:
    return round(math.floor(p / TICK + 1e-6) * TICK, 3)


def baskets_of(qty: int) -> int:
    return min(BASKETS, max(1, int(math.ceil(qty / BASKET_SETS))))


def load_rows(plan: dict, speed: str, target: float) -> tuple[list, Optional[str]]:
    """(rows of this target in plan order, None) or ([], why not). Each row gets its race and kind from plan["holdings"]."""
    entry = next((r for r in (plan.get("speeds") or {}).get(speed) or [] if r.get("target") == target), None)
    if entry is None:
        return [], f"no target {target} at speed {speed!r}"
    if not entry.get("reached"):
        return [], f"target {target} was not reached by the plan"
    names = {h.get("name"): h for h in plan.get("holdings") or []}
    rows = []
    for o in entry.get("order") or []:
        h = names.get(o.get("holding"))
        if h is None:
            return [], f"holding {o.get('holding')!r} is not in the plan's holdings"
        if o.get("type") not in SET_TYPES + LEG_TYPES:
            return [], f"{o.get('holding')}: type {o.get('type')!r} is not sold by this executor"
        q = int(o.get("qty") or 0)
        worst = {str(k): float(v) for k, v in (o.get("worst_cash_per_share") or {}).items()}
        if q < 1 or float(o.get("cash") or 0.0) <= 0.0 or set(worst) != {str(e) for e in o.get("legs") or []}:
            return [], f"{o.get('holding')}: bad row"
        rows.append({"step": str(o["step"]), "holding": o["holding"], "type": o["type"], "race": h.get("race"), "kind": h.get("kind"),
                     "legs": [str(e) for e in o["legs"]], "qty": q, "plan_cash": float(o["cash"]), "worst": worst})
    return (rows, None) if rows else ([], f"target {target} has an empty list")


def leg_quote(book: dict, sign: int, want: int, floor: float) -> tuple[int, Optional[float]]:
    """Selling up to `want` of one leg: (shares priced at >= floor cash a share, the worst cash a share). A YES holding (sign +1)
    sells at the bids, a NO holding at 1 - the asks."""
    got, worst = 0, None
    for lvl in (book.get("bids" if sign > 0 else "asks") or []):
        px = float(lvl["price"]) if sign > 0 else 1.0 - float(lvl["price"])
        if px < floor - 1e-9 or got >= want:
            break
        n = min(want - got, int(lvl["quantity"]))
        if n > 0:
            got, worst = got + n, px
    return got, worst


class OffloadExecutor:
    """One action a tick; the adapter does all I/O. Adapter interface (see tests/test_offload.py for a fake):
      dry;  now() -> epoch s;  lock() -> context manager (the trade lock);  positions() -> {exchange: YES qty} (raises);
      sale_view(pos) -> pos without other sleeves' ledgers;  cheapfav_open() -> {exchange: signed qty};  balance() -> venue cash;
      book(ex) -> book net of our orders, or None;  race_legs(race) -> [exchanges];  busy(race) -> why not, or None;
      blocked() -> why every send waits, or None;  cheapfav_ok() -> why not, or None;  pull(exs, now) -> bool;
      cap_ok(before, after) -> bool;  net_d(pos) -> float or None;  cap -> float;  abort_requested() -> bool;
      basket(race, kind, legs, key, q, proceeds, payout, sgn) -> response (raises with .status/.code/.details);
      batch(orders, key) -> [{"ok", "status", "data"}, ...] (raises);  record_leg(data, order, purpose);  cancel(ids) -> None (raises);
      optional: pull_cost(exs) -> writes a pull will take;  after_batch() -> None;  set_floor(race) -> lowest price a set, or None;
      sell_ok -> conventional sells allowed"""

    def __init__(self, plan_path: str, state_path: str, expected_sha: str, adapter, target: float, speed: str, start: str, stop: str,
                 writes_per_min: int = 15, slack_ticks: float = 2.0):
        self.a = adapter
        self.plan_path, self.state_path = plan_path, state_path
        self.target, self.speed = float(target or 0.0), speed
        self.slack = float(slack_ticks) * TICK
        self.writes_per_min = int(writes_per_min)
        self.refused = None
        self.rows: list = []
        self.sha = None
        self.fall = 0.0
        self.t0 = self.t1 = 0.0
        if getattr(adapter, "dry", False) and os.path.basename(state_path) == "offload_state.json":
            self.refused = "dry run with the live state file: use a state file of its own"
        try:
            self.t0, self.t1 = ts_of(start), ts_of(stop)
            if not 0 < self.t1 - self.t0 <= MAX_WINDOW:
                self.refused = self.refused or f"window of {self.t1 - self.t0:.0f} s is not in (0, {MAX_WINDOW:.0f}]"
        except (TypeError, ValueError) as e:
            self.refused = self.refused or f"offload_start / offload_stop: {e}"
        if not self.refused:
            try:
                raw = open(plan_path, "rb").read()
                self.sha = sha256_file(plan_path)
                if not expected_sha or self.sha != expected_sha:
                    self.refused = f"plan hash {self.sha[:12]} does not match the approved {str(expected_sha)[:12] or '(none)'}"
                else:
                    plan = json.loads(raw)
                    built = datetime.strptime(str(plan.get("built")), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).timestamp()
                    if self.t0 - built > MAX_PLAN_AGE:
                        self.refused = f"plan built {plan.get('built')}: over {MAX_PLAN_AGE / 3600:.0f} h before the start, re-run it"
                    else:
                        self.rows, why = load_rows(plan, speed, float(target or 0.0))
                        self.refused = why
                        self.fall = next((float(x.get("below_payout_sets") or 0.0) for x in plan["speeds"].get(speed) or []
                                          if x.get("target") == float(target or 0.0)), 0.0)
            except (OSError, ValueError, TypeError) as e:
                self.refused = f"plan unreadable: {e}"
        if self.refused:
            log.warning("OFFLOAD refused: %s", self.refused)
        self.by_step = {r["step"]: r for r in self.rows}
        self.state = self._load()

    # ---------------------------------------------------------------- state
    def _load(self) -> dict:
        try:
            st = json.load(open(self.state_path))
            if st.get("sha") == self.sha and st.get("target") == self.target and st.get("speed") == self.speed:
                return st
            log.warning("OFFLOAD state belongs to another plan: starting fresh")
        except FileNotFoundError:
            pass
        except ValueError as e:
            log.warning("OFFLOAD state unreadable (%s): nothing is sent until it is removed by hand", e)
            self.refused = self.refused or f"state file unreadable: {e}"
        return {"sha": self.sha, "target": self.target, "speed": self.speed, "status": "waiting", "steps": {}, "raised": 0.0, "n": 0,
                "pending": None, "test": None, "hard": 0, "writes": [], "log": []}

    def save(self):
        d = os.path.dirname(self.state_path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".offload_state.")
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    def note(self, msg: str, error: bool = False):
        (log.error if error else log.warning)("OFFLOAD %s", msg)
        self.state["log"] = (self.state.get("log") or [])[-299:] + [[self.a.now(), msg]]

    def once(self, key: str, msg: str):
        seen = self.state.setdefault("noted", [])
        if key not in seen:
            seen.append(key)
            self.note(msg)

    def st(self, step: str) -> dict:
        return self.state["steps"].setdefault(step, {"status": "pending", "base": None, "done": 0, "cash": 0.0, "tries": 0, "next": 0.0,
                                                     "live": None, "unk": False, "busy": False, "stuck": None})

    def races(self) -> set:
        """The races other sleeves keep off: only while a live sale runs (a dry run changes nothing on the bot)."""
        if self.refused or self.a.dry:
            return set()
        if self.state.get("status") != "running":
            if self.state.get("status") in ("done", "aborted") and self.a.now() < self.state.get("amb_until", 0.0):
                return {r["race"] for r in self.rows}    # an order of unknown outcome may still rest on these legs
            return set()
        steps = self.state["steps"]                  # other threads call this: read only, st() would add keys under save()
        out = set()
        for r in self.rows:
            v = steps.get(r["step"]) or {}
            if v.get("status", "pending") == "pending" and not v.get("busy"):
                out.add(r["race"])
        return out

    # ---------------------------------------------------------------- end of the sale
    def _live_ids(self) -> list:
        return [v["live"][0] for v in self.state["steps"].values() if v.get("live")]

    def _finish(self, status: str, why: str):
        ids = self._live_ids()
        if ids and not self.a.dry:
            try:
                self.a.cancel(ids)
            except Exception as e:
                self.note(f"cancel of {len(ids)} resting orders failed ({e}): they expire within {TTL:.0f} s")
        for v in self.state["steps"].values():
            v["live"] = None
        left = [r["holding"] for r in self.rows if self.st(r["step"])["status"] != "done"]
        self.state["status"] = status
        self.state["why"] = why
        self.state["ended"] = self.a.now()
        self.state["pending"] = None
        self.note(f"{status.upper()}: {why}: raised {self.state['raised']:.0f} of {self.target:.0f}, {len(left)} of {len(self.rows)} rows unsold",
                  error=status == "aborted")
        self.save()

    def abort(self, why: str):
        self._finish("aborted", why)

    # ---------------------------------------------------------------- tick
    def tick(self) -> Optional[str]:
        """At most one send. Returns a short description of what it did, or None."""
        if self.refused or self.state["status"] in ("done", "aborted"):
            return None
        now = self.a.now()
        if self.a.abort_requested():
            self.abort("abort file present")
            return "abort"
        if now < self.t0:
            return None
        if now >= self.t1:
            self._finish("done", "window closed")
            return None
        why = self.a.blocked()
        if why:
            self.state.setdefault("held_since", now)
            self.once("blocked:" + why, f"held: {why}")
            if self.state["status"] == "running" and now - self.state["held_since"] > HELD_MAX:
                self.abort(f"held over {HELD_MAX:.0f} s: {why}")
            return None
        since = self.state.pop("held_since", None)
        if since is not None:                        # a hold is not a row's own wait
            for v in self.state["steps"].values():
                if v.get("stuck") is not None:
                    v["stuck"] += now - since
        if self.state["status"] == "waiting" and not self._preflight(now):
            return None
        if self.state.get("pending"):
            return self._resend(now)
        if not self._test_done(now):
            return self._test(now)
        if self.state["raised"] >= self.target:
            self._finish("done", "target raised")
            return None
        if all(self.st(r["step"])["status"] != "pending" for r in self.rows):
            self._finish("done", "list finished")
            return None
        w = [t for t in self.state.get("writes") or [] if now - t < 60.0]
        self.state["writes"] = w
        if len(w) >= self.writes_per_min:
            return None
        due = [r for r in self.rows if self.st(r["step"])["status"] == "pending" and self.st(r["step"])["next"] <= now]
        if not due:
            return None
        if due[0]["type"] in SET_TYPES:
            return self._set(due[0], now)
        return self._legs([r for r in due if r["type"] in LEG_TYPES][:BATCH_ROWS], now)

    def _gate(self) -> Optional[float]:
        """Under the lock, right before a send: the stops may have changed while it waited. The time now, or None."""
        now = self.a.now()
        if self.a.abort_requested():
            self.abort("abort file present")
            return None
        if now >= self.t1 or self.a.blocked():
            return None
        return now

    def _wait(self, r: dict, v: dict, now: float, key: str, msg: str):
        """A row that cannot go now: tried again shortly, given up after STUCK_MAX."""
        v["next"] = now + RETRY_GAP
        if v["stuck"] is None:
            v["stuck"] = now
        self.once(f"{key}:{r['step']}", msg)
        if now - v["stuck"] > STUCK_MAX:
            self._row_end(r, v, "failed")

    def _preflight(self, now: float) -> bool:
        try:
            pos = self.a.positions()
        except Exception as e:
            log.warning("OFFLOAD positions read failed (%s): next tick", e)
            return False
        view = self.a.sale_view(pos)
        cf = self.a.cheapfav_open()
        reach, seen = 0.0, {}
        for r in self.rows:
            why = self.a.busy(r["race"]) or (self.a.cheapfav_ok() if r["type"] == "cheapfav" else None)
            if why:
                if r["type"] == "cheapfav":
                    self._row_end(r, self.st(r["step"]), "failed")
                self.once(f"skip:{r['step']}", f"{r['holding']}: left out: {why}")
                continue
            held = self._held(r, pos, view, cf)[0]
            reach += min(r["qty"], held) * r["plan_cash"] / r["qty"]
        if reach < MIN_REACHABLE * self.target:
            self.abort(f"the list as held now raises about {reach:.0f}, under {100 * MIN_REACHABLE:.0f}% of {self.target:.0f}: re-run the plan")
            return False
        self.state["status"] = "running"
        self.state["started"] = now
        if self.fall >= 0.9 * FALL_LIMIT:
            self.note(f"this list gives up about {self.fall:.0f} against payout: the strategy stops pause exits at {FALL_LIMIT:.0f} in an hour", error=True)
        self.note(f"{'DRY ' if self.a.dry else ''}start: {len(self.rows)} rows, about {reach:.0f} as held now, target {self.target:.0f}")
        self.save()
        return True

    def _held(self, r: dict, pos: dict, view: dict, cf: dict) -> tuple[int, int]:
        """(units of this row held now, sign). Sets: sets of the plan's kind. A leg: what is left beyond the sets, on the plan's
        side; a cheap favourite: the sleeve's own open position, never more than the venue's."""
        if r["type"] in SET_TYPES:
            kind, k, _ = WD.decompose(self.a.race_legs(r["race"]), view)
            return (k if kind == r["kind"] else 0), (1 if r["kind"] == "sell_set" else -1)
        ex, sign = r["legs"][0], (1 if r["kind"] == "yes" else -1)
        if r["type"] == "cheapfav":
            q = min(float(cf.get(ex, 0.0)) * sign, float(pos.get(ex, 0.0)) * sign)
        else:
            q = float(WD.decompose(self.a.race_legs(r["race"]), view)[2].get(ex, 0.0)) * sign
        return max(0, int(q)), sign

    def _sync(self, r: dict, v: dict, held: int, now: float) -> Optional[int]:
        """Units left to sell, or None while a position must be read again. After a send of unknown outcome, or one that rested, the
        position says what sold (at the plan's price), once two reads SETTLE_SEC apart agree: a read that lags a fill would resell it.
        Otherwise only the responses count: a cheap-favourites sale on the same leg shrinks this row's view until its fills are
        read, which is not a sale of this row."""
        if v["base"] is None:
            v["base"] = held
        if v.get("unk"):
            rd = v.get("rd")
            if rd is None or rd[0] != held:
                v["rd"] = [held, now]
                return None
            if now - rd[1] < SETTLE_SEC:
                return None
            v["unk"], v["rd"] = False, None
            sold = max(0, v["base"] - held)
            if sold > v["done"]:
                extra = (sold - v["done"]) * r["plan_cash"] / r["qty"]
                v["cash"] += extra
                self.state["raised"] += extra
                v["done"] = sold
        return max(0, min(r["qty"] - v["done"], held))

    def _wrote(self, n: int = 1):
        self.state.setdefault("writes", []).extend([self.a.now()] * n)

    # ---------------------------------------------------------------- the test basket
    def _test_row(self) -> Optional[dict]:
        yes = [r for r in self.rows if r["type"] in SET_TYPES and r["kind"] == "buy_set"]
        return next((r for r in yes if r["race"] == TEST_RACE), max(yes, key=lambda r: r["qty"]) if yes else None)

    def _test_done(self, now: float) -> bool:
        t = self.state.get("test")
        if t is None:
            r = self._test_row()
            t = self.state["test"] = {"status": "pending", "step": r["step"]} if r else {"status": "passed", "why": "no YES set in this list"}
        return t["status"] == "passed"

    def _test(self, now: float) -> Optional[str]:
        t = self.state["test"]
        r = self.by_step[t["step"]]
        v = self.st(r["step"])
        if t["status"] == "pending":
            if v["status"] != "pending":
                self.abort(f"test basket: {r['holding']} cannot be sold ({v['status']}): see the log above; re-run the plan")
                return None
            return self._set(r, now, test=True) if v["next"] <= now else None
        if now - t["t"] < TEST_WAIT:
            return None
        try:
            pos = self.a.positions()
            bal = float(self.a.balance())
        except Exception as e:
            log.warning("OFFLOAD test check: read failed (%s)", e)
            return None
        k = self._held(r, pos, self.a.sale_view(pos), {})[0]
        gained = bal - t["bal"]
        ok_pos = t["k"] - k >= t["q"] - 0.5
        ok_cash = self.a.dry or gained >= t["expect"] - max(CASH_TOL[0], min(CASH_TOL[1], CASH_TOL[2] * t["expect"]))
        if ok_pos and ok_cash:
            t["status"] = "passed"
            self.note(f"test basket passed: {t['q']} sets at {t['ps']:.4f} (plan {t['plan']:.4f}), cash up {gained:.0f}")
            self.save()
        elif now - t["t"] > TEST_TIMEOUT:
            self.abort(f"test basket: sets fell {t['k'] - k} of {t['q']}, cash up {gained:.0f} of about {t['expect']:.0f}")
        return None

    # ---------------------------------------------------------------- sets
    def _set(self, r: dict, now: float, test: bool = False) -> Optional[str]:
        v = self.st(r["step"])
        why = self.a.busy(r["race"])
        v["busy"] = bool(why)
        if why:
            self._wait(r, v, now, "busy", f"{r['holding']}: left alone: {why}")
            return None
        exs = self.a.race_legs(r["race"])
        books = {e: self.a.book(e) for e in exs}
        if any(not b for b in books.values()):
            v["next"] = now + RETRY_GAP
            return None
        kind = r["kind"]
        plan_ps = r["plan_cash"] / r["qty"]
        floor = plan_ps - self.slack
        hard = getattr(self.a, "set_floor", lambda race: None)(r["race"])   # Sizhe's floor: judged on the worst level, what the limits allow

        def priced(w):
            return (w is not None and w["proceeds"] >= floor - 1e-9
                    and (not hard or WD.worst_set_price(kind, w, exs) >= hard - 1e-9))

        with self.a.lock():
            now = self._gate()
            if now is None:
                return None
            self._wrote(self._pull_cost(exs))
            if not self.a.pull(exs, now):
                self._wait(r, v, now, "pull", f"{r['holding']}: quotes not pulled (an order we did not place, or a cancel failed): waiting")
                return None
            try:
                pos = self.a.positions()
                bal = float(self.a.balance())
            except Exception as e:
                log.warning("OFFLOAD %s: read failed (%s): next tick", r["holding"], e)
                return None
            held, sgn = self._held(r, pos, self.a.sale_view(pos), {})
            left = self._sync(r, v, held, now)
            if left is None:
                v["next"] = now + SETTLE_SEC
                self.save()
                return None
            if left < 1:
                if v["done"] > 0:
                    self._row_end(r, v, "done")
                else:
                    self._wait(r, v, now, "none", f"{r['holding']}: none held now: waiting")
                return None
            q = min(left, int(math.ceil(r["qty"] / baskets_of(r["qty"]))))
            wq = WD.walk_quote(kind, books, exs, q)
            while not test and q > min(MIN_BASKET, left) and not priced(wq):
                q = max(min(MIN_BASKET, left), q // 2)                # a shallow or dear book: a smaller basket, the rest later
                wq = WD.walk_quote(kind, books, exs, q)
            if not priced(wq):
                got = "no depth" if wq is None else f"{wq['proceeds']:.4f}"
                if test:
                    self.abort(f"test basket: {q} sets of {r['holding']} would fetch {got}, the plan says {plan_ps:.4f}")
                    return None
                self._wait(r, v, now, "px", f"{r['holding']}: {q} sets at {got}, under the plan's {plan_ps:.4f}: waiting")
                return None
            after = dict(pos)
            for e in exs:
                after[e] = after.get(e, 0.0) + sgn * q
            if not self.a.cap_ok(pos, after):
                self._wait(r, v, now, "cap", f"{r['holding']}: {q} sets would break the partisan cap: waiting")
                return None
            buy_side = "yes" if kind == "sell_set" else "no"
            gross = q * sum(wq["limits"].values())
            if gross > CASH_MARGIN * bal:              # admitted on the gross: a conventional sell needs no cash
                if not getattr(self.a, "sell_ok", False):   # no ledger books a sell yet (fills have no action): off until one is verified live
                    self._wait(r, v, now, "cash", f"{r['holding']}: {q} sets would gross {gross:.0f} against {bal:.0f}: waiting for cash")
                    return None
                legs = [{"exchangeId": e, "side": "no" if buy_side == "yes" else "yes", "action": "sell", "quantity": q,
                         "price": round(1.0 - lim, 3)} for e, lim in wq["limits"].items()]
                self.note(f"{r['holding']}: {q} sets would gross {gross:.0f} against {bal:.0f}: conventional sell")
            else:
                legs = [{"exchangeId": e, "side": buy_side, "action": "buy", "quantity": q, "price": lim} for e, lim in wq["limits"].items()]
            self.state["n"] += 1
            key = f"off-{self.sha[:8]}-{self.state['n']}"
            self.state["pending"] = {"what": "basket", "step": r["step"], "key": key, "legs": legs, "q": q, "ps": wq["proceeds"], "t": now,
                                     "sgn": sgn, "test": test, "k": held, "bal": bal, "plan": plan_ps, "floor": floor}
            self.save()
            return self._send_basket(now)

    def _send_basket(self, now: float) -> Optional[str]:
        p = self.state["pending"]
        r = self.by_step[p["step"]]
        v = self.st(p["step"])
        n = len(p["legs"])
        payout = n - 1.0 if r["kind"] == "sell_set" else 1.0
        self._wrote()
        try:
            resp = self.a.basket(r["race"], r["kind"], p["legs"], p["key"], p["q"], p["ps"], payout, p["sgn"])
        except Exception as e:
            status, code = int(getattr(e, "status", 0) or 0), str(getattr(e, "code", "") or "")
            det = getattr(e, "details", None)
            if status in TRANSIENT or (status == 409 and code == "REQUEST_IN_FLIGHT"):
                v["unk"] = True
                self._backoff(p, e, now, status == 409)
                self.note(f"{r['holding']}: basket outcome unknown ({e}): same key at +{p['at'] - now:.0f} s")
                self.save()
                return None
            self.state["pending"] = None
            aon = isinstance(det, dict) and det.get("rejectionReasonCode") == "AllOrNoneNotFilled"
            v["tries"] += 1
            v["next"] = now + RETRY_GAP
            self.note(f"{r['holding']}: basket of {p['q']} {'not filled' if aon else 'rejected'} ({e}); try {v['tries']}")
            if not aon:
                self.state["hard"] += 1
            if self.state["hard"] >= MAX_HARD:
                self.abort(f"{MAX_HARD} hard rejections in a row")
            elif v["tries"] > MAX_RETRIES:
                if p["test"]:
                    self.abort(f"test basket did not fill in {MAX_RETRIES + 1} tries")
                else:
                    self._row_end(r, v, "failed")
            self.save()
            return None
        self.state["pending"] = None
        data = [x.get("data") or {} for x in resp.get("results", [])]
        traded = [float(d.get("quantityTraded") or 0) for d in data]
        if len(data) != n or any(t < p["q"] for t in traded):     # all-or-none cannot do this: stop and look
            self.abort(f"{r['holding']}: basket of {p['q']} came back with {traded}")
            return "abort"
        ps = self._proceeds(p, data)
        self.state["hard"] = 0
        v["tries"] = 0
        v["stuck"] = None
        v["done"] += p["q"]
        v["cash"] += p["q"] * ps
        self.state["raised"] += p["q"] * ps
        v["next"] = now + BASKET_GAP
        self.note(f"{r['holding']}: {'DRY ' if self.a.dry else ''}sold {p['q']} sets at {ps:.4f} (plan {p['plan']:.4f}, key {p['key']})")
        if p["test"]:
            if ps < p["floor"] - 1e-9:
                self.abort(f"test basket filled at {ps:.4f}, under the plan's {p['plan']:.4f}")
                return "abort"
            self.state["test"].update({"status": "wait", "t": now, "q": p["q"], "k": p["k"], "bal": p["bal"], "ps": ps, "plan": p["plan"],
                                       "expect": p["q"] * ps})
        if v["done"] >= r["qty"]:
            self._row_end(r, v, "done")
        self.save()
        return f"basket {r['holding']}"

    @staticmethod
    def _proceeds(p: dict, data: list) -> float:
        """Cash a set from the venue's own figures: a buy of the opposite side nets to 1 - its price a share, a sell pays its price.
        A leg without totalCost counts at its limit, the worst it could have filled at."""
        tot = 0.0
        for leg, d in zip(p["legs"], data):
            tc = d.get("totalCost")
            px = float(tc) / p["q"] if tc is not None else float(leg["price"])
            if tc is None or d.get("action") is None or d.get("action") == leg["action"]:
                tot += px if leg["action"] == "sell" else 1.0 - px
            else:                                     # the venue turned the leg into its complement: its figures are of that order
                tot += px if d["action"] == "sell" else 1.0 - px
        return tot

    def _row_end(self, r: dict, v: dict, status: str):
        v["status"] = status
        self.note(f"{r['holding']}: {status}: {v['done']} of {r['qty']} for {v['cash']:.0f}")
        self.save()

    def _backoff(self, p: dict, e, now: float, in_flight: bool):
        """The next resend of an unknown send: the venue's Retry-After, else doubling; the spec's 90 s for a key in flight."""
        p["tries"] = p.get("tries", 0) + 1
        ra = getattr(e, "retry_after", None)
        p["at"] = now + (IN_FLIGHT_WAIT if in_flight else float(ra) if ra else min(BACKOFF_MAX, 2.0 ** p["tries"]))

    def _resend(self, now: float) -> Optional[str]:
        """A send whose outcome is unknown: the same key and payload, paced. Past PENDING_MAX, or for a leg payload past its expiry, it
        is dropped: the row is marked unknown, so a position that agrees twice says what happened."""
        p = self.state["pending"]
        if p["what"] == "batch":
            if now > p["exp"] + 2.0:
                self.state["pending"] = None
                self._unknown(p)
                self.note(f"batch {p['key']} expired unconfirmed: the positions settle it")
                self.save()
                return None
        elif now - p["t"] > PENDING_MAX:
            self.state["pending"] = None
            v = self.st(p["step"])
            v["unk"] = True
            v["next"] = now + RETRY_GAP
            if p["test"]:
                self.abort("test basket never confirmed")
                return None
            self.note(f"basket {p['key']} never confirmed: the positions settle it")
            self.save()
            return None
        w = [t for t in self.state.get("writes") or [] if now - t < 60.0]
        if now < p.get("at", 0.0) or len(w) >= self.writes_per_min:
            return None
        with self.a.lock():
            now = self._gate()
            if now is None:
                return None
            if p["what"] == "batch":
                return self._send_batch(now) if now <= p["exp"] - 2.0 else None
            return self._send_basket(now)

    # ---------------------------------------------------------------- legs
    def _pull_cost(self, exs: list) -> int:
        return int(getattr(self.a, "pull_cost", lambda e: 0)(exs))

    def _legs(self, rows: list, now: float) -> Optional[str]:
        """Pulls and the send are two lock holds: up to BATCH_ROWS cancels must not stale the expiry, or keep the other sleeves out."""
        if not rows or now < self.state.get("amb_until", 0.0):
            return None                               # an order of unknown outcome may rest: it is in no cap check or position yet
        priced = []
        room = max(1, self.writes_per_min - len(self.state.get("writes") or []))
        for r in rows:
            v = self.st(r["step"])
            why = self.a.busy(r["race"])
            v["busy"] = bool(why)
            if r["type"] == "cheapfav":
                why = why or self.a.cheapfav_ok()
            if why:
                self._wait(r, v, now, "busy", f"{r['holding']}: left alone: {why}")
                continue
            cost = self._pull_cost(r["legs"]) + (1 if v.get("live") else 0)
            if priced and cost > room:
                break                                 # the rest next tick: pulls share the account's writes with the MM
            room -= cost
            b = self.a.book(r["legs"][0])
            if not b:
                self._wait(r, v, now, "book", f"{r['holding']}: no book: waiting")
                continue
            priced.append((r, v, b))
        if not priced:
            return None
        pulled = []
        with self.a.lock():
            now = self._gate()
            if now is None:
                return None
            for it in priced:
                self._wrote(self._pull_cost(it[0]["legs"]))
                if self.a.pull(it[0]["legs"], now):
                    pulled.append(it)
                else:
                    self._wait(it[0], it[1], now, "pull", f"{it[0]['holding']}: quotes not pulled: waiting")
        if not pulled:
            return None
        with self.a.lock():
            now = self._gate()
            if now is None:
                return None
            live = [v["live"][0] for _, v, _ in pulled if v.get("live")]
            if live and not self.a.dry:               # these rows' remainders: their expiry has passed, the venue's sweep may lag
                self._wrote(len(live))
                try:
                    self.a.cancel(live)
                except Exception as e:
                    if int(getattr(e, "status", 0) or 0) in TRANSIENT:
                        self.note(f"cancel of remainders failed ({e}): next tick")
                        return None
            for _, v, _ in pulled:
                v["live"] = None
            try:
                pos = self.a.positions()
                bal = float(self.a.balance())
            except Exception as e:
                log.warning("OFFLOAD legs: read failed (%s): next tick", e)
                return None
            view, cf = self.a.sale_view(pos), self.a.cheapfav_open()
            nd = self.a.net_d(pos)
            if nd is None:
                return None
            cap = self.a.cap
            up, down, gross = 0.0, 0.0, 0.0            # net D if only the orders widening it fill, each way
            now = self.a.now()
            for v in self.state["steps"].values():     # other rows' remainders may still fill: they count as orders of this batch
                lv = v.get("live")
                if lv and now < lv[1] + 10.0:
                    after = dict(pos)
                    after[lv[2]] = after.get(lv[2], 0.0) + lv[3]
                    d = self.a.net_d(after)
                    if d is None:
                        return None
                    up, down = up + max(0.0, d - nd), down + min(0.0, d - nd)
            exp = now + TTL
            stamp = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            orders, purposes, steps = [], [], []
            for r, v, b in pulled:
                ex = r["legs"][0]
                held, sign = self._held(r, pos, view, cf)
                left = self._sync(r, v, held, now)
                if left is None:
                    v["next"] = now + SETTLE_SEC
                    continue
                if left < 1:
                    if v["done"] > 0:
                        self._row_end(r, v, "done")
                    else:
                        self._wait(r, v, now, "none", f"{r['holding']}: none held now: waiting")
                    continue
                q, worst = leg_quote(b, sign, left, r["worst"][ex] - self.slack)
                if q < 1:
                    self._wait(r, v, now, "px", f"{r['holding']}: nothing at or above the plan's {r['worst'][ex]:.3f}: waiting")
                    continue
                price = tick_down(1.0 - worst)
                after = dict(pos)
                after[ex] = after.get(ex, 0.0) - sign * q
                d = self.a.net_d(after)
                if d is None:
                    return None
                dd = d - nd
                u, w = up + max(0.0, dd), down + min(0.0, dd)
                worst_nd = nd + (u if dd > 0 else w)
                if abs(worst_nd) > cap + 1e-6 and abs(worst_nd) > abs(nd) + 1e-6:
                    self._wait(r, v, now, "cap", f"{r['holding']}: {q} shares would break the partisan cap: waiting")
                    continue
                if gross + q * price > CASH_MARGIN * bal:
                    self._wait(r, v, now, "cash", f"{r['holding']}: {q} shares would gross above the cash: waiting")
                    continue
                up, down, gross = u, w, gross + q * price
                orders.append({"exchangeId": ex, "side": "no" if sign > 0 else "yes", "action": "buy", "quantity": q, "price": price,
                               "expirationDate": stamp})
                purposes.append("cheapfav_exit" if r["type"] == "cheapfav" else "raise")
                steps.append(r["step"])
            if not orders:
                self.save()
                return None
            self.state["n"] += 1
            self.state["pending"] = {"what": "batch", "key": f"off-{self.sha[:8]}-{self.state['n']}", "orders": orders, "purposes": purposes,
                                     "steps": steps, "exp": exp, "t": now}
            self.save()
            return self._send_batch(now)

    def _unknown(self, p: dict, lease: bool = False, steps: Optional[list] = None):
        """Orders of this batch may rest with no record of ours: no leg is priced until they are dead (lease: the request itself may
        still be running at the venue)."""
        p.setdefault("amb0", self.state.get("amb_until", 0.0))
        until = max(p["exp"] + 10.0, p["t"] + LEASE) if lease else p["exp"] + 10.0
        self.state["amb_until"] = max(self.state.get("amb_until", 0.0), until)
        for s in steps if steps is not None else p["steps"]:
            self.st(s)["unk"] = True

    def _send_batch(self, now: float) -> Optional[str]:
        p = self.state["pending"]
        self._wrote()
        try:
            res = self.a.batch(p["orders"], p["key"])
        except Exception as e:
            status, code = int(getattr(e, "status", 0) or 0), str(getattr(e, "code", "") or "")
            if status in TRANSIENT or (status == 409 and code == "REQUEST_IN_FLIGHT"):
                inflight = status == 409
                self._unknown(p, lease=status == 0 or inflight)
                self._backoff(p, e, now, inflight)
                self.note(f"batch {p['key']} outcome unknown ({e}): same key at +{p['at'] - now:.0f} s")
                self.save()
                return None
            self.state["pending"] = None
            self.state["hard"] += 1
            self.note(f"batch {p['key']} rejected ({e})")
            for s in p["steps"]:
                self.st(s)["next"] = max(self.st(s)["next"], now + RETRY_GAP)
            if self.state["hard"] >= MAX_HARD:
                self.abort(f"{MAX_HARD} hard rejections in a row")
            self.save()
            return None
        again = False
        seen = p.setdefault("seen", [])               # a resend replays the items already settled
        for i, x in enumerate(res):
            if i >= len(p["steps"]) or i in seen:
                continue
            r, v, o = self.by_step[p["steps"][i]], self.st(p["steps"][i]), p["orders"][i]
            d = x.get("data") or {}
            if x.get("ok"):
                self.a.record_leg(d, o, p["purposes"][i])
                seen.append(i)
                got = int(float(d.get("quantityTraded") or d.get("filledQuantity") or 0))
                if got:
                    px = 1.0 - float(o["price"])
                    v["done"] += got
                    v["cash"] += got * px
                    self.state["raised"] += got * px
                v["live"] = None
                if d.get("open") and d.get("orderId") is not None:
                    left = int(o["quantity"]) - got
                    v["live"] = [d["orderId"], p["exp"], o["exchangeId"], left if o["side"] == "yes" else -left]
                v["unk"] = bool(d.get("open"))
                v["next"] = max(now + TTL + 2.0 if d.get("open") else now + RETRY_GAP, v["next"])
                if v["done"] >= r["qty"]:
                    self._row_end(r, v, "done")
                elif got or d.get("open"):
                    v["stuck"] = None
                else:                                 # accepted, nothing traded or rests (orderId null, self-trade prevented)
                    self._wait(r, v, now, "nofill", f"{r['holding']}: accepted with no fill: waiting")
            elif int(x.get("status") or 0) in TRANSIENT:
                again = True
                self._unknown(p, steps=[p["steps"][i]])
            else:
                seen.append(i)
                v["tries"] += 1
                v["next"] = now + RETRY_GAP
                self.note(f"{r['holding']}: order rejected ({d}); try {v['tries']}")
                if v["tries"] > MAX_RETRIES:
                    self._row_end(r, v, "failed")
        getattr(self.a, "after_batch", lambda: None)()
        if len(res) < len(p["orders"]):               # a 2xx that does not cover the batch: what it left out is unknown
            again = True
            self._unknown(p, steps=[p["steps"][i] for i in range(len(p["steps"])) if i not in seen])
        if again:
            self._backoff(p, None, now, False)
        elif "amb0" in p:                             # every item settled: nothing of this batch is unknown any more
            self.state["amb_until"] = p["amb0"]
        if again:                                     # never a new key here: an unknown item may rest, and a repriced order could
            self.save()                               # then sell through zero. The same key resumes it, until the payload expires
            return "batch"
        self.state["pending"] = None
        self.state["hard"] = 0
        self.note(f"{'DRY ' if self.a.dry else ''}batch of {len(p['orders'])} orders sent (key {p['key']})")
        self.save()
        return "batch"


# ---------------------------------------------------------------- dry-run CLI against a book dump
class BookAdapter:
    """Dry adapter over a book dump: positions come from the plan's holdings, cheap-favourite rows count as held and paused, baskets
    and batches fill virtually at their limits. Books do not move, so this shows the sequence, not the price."""

    dry = True
    cap = 5000.0

    def __init__(self, plan: dict, books: dict, at: float):
        self.books, self.t = books, at
        self.pos: dict = {}
        self.cf: dict = {}
        self.exs: dict = {}
        self.bal = float(plan.get("cash") or 0.0)
        self.sent: list = []
        for h in plan.get("holdings") or []:
            self.exs.setdefault(h["race"], [])
            for e in h["legs"]:
                if str(e) not in self.exs[h["race"]]:
                    self.exs[h["race"]].append(str(e))
                if h["kind"] in ("sell_set", "no"):
                    self.pos[str(e)] = self.pos.get(str(e), 0.0) - h["qty"]
                else:
                    self.pos[str(e)] = self.pos.get(str(e), 0.0) + h["qty"]
            if h["type"] == "cheapfav":
                self.cf[str(h["legs"][0])] = float(h["qty"]) * (1 if h["kind"] == "yes" else -1)

    def now(self):
        return self.t

    def lock(self):
        return contextlib.nullcontext()

    def positions(self):
        return dict(self.pos)

    def sale_view(self, pos):
        return {e: q - self.cf.get(e, 0.0) for e, q in pos.items()}

    def cheapfav_open(self):
        return dict(self.cf)

    def balance(self):
        return self.bal

    def book(self, ex):
        return self.books.get(str(ex))

    def race_legs(self, race):
        return self.exs.get(race, [])

    def busy(self, race):
        return None

    def blocked(self):
        return None

    def cheapfav_ok(self):
        return None

    def pull(self, exs, now):
        return True

    def cap_ok(self, before, after):
        return True

    def net_d(self, pos):
        return 0.0

    def abort_requested(self):
        return False

    def basket(self, race, kind, legs, key, q, proceeds, payout, sgn):
        self.sent.append(("basket", race, q, round(proceeds, 4), legs[0]["action"]))
        for l in legs:
            self.pos[l["exchangeId"]] += sgn * q
        self.bal += q * proceeds
        return {"results": [{"data": {"exchangeId": l["exchangeId"], "quantityTraded": q, "open": False}} for l in legs]}

    def batch(self, orders, key):
        out = []
        for o in orders:
            self.sent.append(("leg", o["exchangeId"], o["quantity"], round(1.0 - o["price"], 3), o["side"]))
            self.pos[o["exchangeId"]] += o["quantity"] if o["side"] == "yes" else -o["quantity"]
            self.bal += o["quantity"] * (1.0 - o["price"])
            if o["exchangeId"] in self.cf:
                self.cf[o["exchangeId"]] += o["quantity"] if o["side"] == "yes" else -o["quantity"]
            out.append({"ok": True, "status": 201, "data": {"orderId": len(self.sent), "quantityTraded": o["quantity"], "open": False}})
        return out

    def record_leg(self, data, order, purpose):
        pass

    def cancel(self, ids):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="offload executor dry run against a book dump (sends nothing)")
    ap.add_argument("--plan", default=os.path.join(HERE, "data", "research_offload", "plan.json"))
    ap.add_argument("--books", default=os.path.join(HERE, "data", "books_latest.json"))
    ap.add_argument("--target", type=float, required=True)
    ap.add_argument("--speed", default="5 min")
    ap.add_argument("--minutes", type=float, default=15.0, help="simulated minutes, one tick per 2 s")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    plan = json.load(open(a.plan))
    t0 = datetime.strptime(plan["built"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).timestamp() + 60.0
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    ad = BookAdapter(plan, {str(k): v for k, v in json.load(open(a.books)).items()}, t0)
    st = os.path.join(tempfile.mkdtemp(prefix="offload_dry_"), "state.json")
    ex = OffloadExecutor(a.plan, st, sha256_file(a.plan), ad, a.target, a.speed, iso(t0), iso(t0 + a.minutes * 60.0))
    if ex.refused:
        print("REFUSED:", ex.refused)
        return 1
    for _ in range(int(a.minutes * 30)):
        ex.tick()
        ad.t += 2.0
    for row in ad.sent:
        print(*row)
    for t, msg in ex.state["log"]:
        print(f"+{t - t0:5.0f}s {msg}")
    print(f"{ex.state['status']}: raised {ex.state['raised']:.0f} of {a.target:.0f} in {ad.t - t0:.0f} s, {len(ad.sent)} sends")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
