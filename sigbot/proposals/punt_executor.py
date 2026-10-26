"""3 Nov punt executor (BACKLOG 0000; research_notes/punt_plan.md). OFF unless config punt_enabled is true.

Sizhe approves the exact plan in chat (memory sig-punt-execution); the session then writes the plan file, puts its
SHA-256 in config punt_plan_sha256 and sets punt_enabled. The executor refuses any plan whose bytes do not match that
hash, so an edited file never trades. It never places anything without a valid, hash-matched plan.

Plan file (JSON):
  {"plan_id": "P1", "budget": 90000, "cap_exempt": true,
   "steps": [
     {"id": "s1", "kind": "buy", "exchange_id": "1007", "side": "yes", "max_price": 0.35, "max_cash": 90000,
      "chunk_cash": 5000, "start": "2026-11-03T17:30:00-05:00", "stop": "2026-11-03T19:45:00-05:00", "min_gap_sec": 20,
      "abort_if_outside_below": 0.20},
     {"id": "s1_sell", "kind": "sell", "of_step": "s1", "min_price": 0.85, "chunk_shares": 20000,
      "outside_min": 0.95, "outside_polls": 3, "single_venue_min": 0.97,
      "start": "2026-11-03T19:00:00-05:00", "stop": "2026-11-04T04:00:00-05:00", "min_gap_sec": 20},
     {"id": "s2", "kind": "target_buy", "after_step": "s1_sell", "min_sold_frac": 0.7,
      "target_min": 1550000, "target_k_leader": 1.12, "target_fallback": 1600000,
      "candidates": [{"exchange_id": "1045", "side": "yes", "max_price": 0.30, "min_price": 0.08}, ...],
      "chunk_cash": 10000, "start": "2026-11-03T22:00:00-05:00", "stop": "2026-11-04T04:00:00-05:00", "min_gap_sec": 20}]}

Kinds:
  buy         buy `side` on `exchange_id` in chunks: each order is a capped limit order (limit = the book level that fills
              the chunk, never above max_price) with a short TTL; at most `max_cash` (and the plan budget) committed,
              counted at the limit price until the venue's fills say otherwise.
  sell        sell what step `of_step` bought (buy the opposite side at 1 - limit; the venue nets YES against NO), never
              below `min_price`, once Polymarket AND Kalshi show our side >= outside_min for outside_polls consecutive
              ticks (or one venue >= single_venue_min when the other is missing). Never more than the venue position.
  target_buy  after `after_step` sold min_sold_frac (or finished): target T = max(target_min, k x best live rival), else
              target_fallback; pick the first candidate whose capped ladder turns the cash into >= T shares; buy it
              like `buy`. No candidate fits by `stop`: keep the cash.
Progress is the venue position (server record) relative to the position when the step started. Orders use deterministic
idempotency keys (plan, step, chunk number): after a 5xx or a timeout the same key is retried, so a restart or a lost
response never doubles an order. State is params/punt_state.json (atomic writes).
Aborts: params/punt_abort exists (everything stops, our resting orders cancelled), the plan hash or validation fails
(never starts), 3 rejected orders in a row (that step), a buy step whose outside price fell below abort_if_outside_below,
the window closing (that step ends; resting orders cancelled).
Dry run: the bot's dry-run mode, or the CLI below, logs the orders it would send and fills them virtually at the walked
book prices, so the whole sequence prints:
    python3 -m sigbot.proposals.punt_executor --plan params/punt_plan.json --books data/books_latest.json --at 2026-11-03T17:31:00-05:00
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
import tempfile
import time
from datetime import datetime
from typing import Callable, Optional

log = logging.getLogger("sigbot")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TICK = 0.005
KINDS = ("buy", "sell", "target_buy")
MAX_STEPS = 8
ORDER_TTL = 30.0                     # seconds a chunk order rests before it expires on the venue
MIN_ORDER_CASH = 50.0
MAX_ERRORS = 3
TRANSIENT = (0, 429, 500, 502, 503, 504)   # 0 = no response (timeout / connection): retry with the same key


def sha256_file(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def ts_of(s: str) -> float:
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        raise ValueError(f"time {s!r} has no UTC offset (3 Nov is EST: -05:00)")
    return d.timestamp()


def on_tick(p: float) -> bool:
    return abs(p / TICK - round(p / TICK)) < 1e-6


def validate(plan: dict, known: Callable[[str], bool], max_budget: float) -> list:
    """Every reason the plan cannot run; empty when it is valid."""
    err = []
    if not isinstance(plan, dict):
        return ["plan is not an object"]
    if not plan.get("plan_id"):
        err.append("plan_id missing")
    b = plan.get("budget")
    if not isinstance(b, (int, float)) or not 0 < b <= max_budget:
        err.append(f"budget {b!r} not in (0, {max_budget:.0f}]")
    if not isinstance(plan.get("cap_exempt"), bool):
        err.append("cap_exempt must be true or false (Sizhe's partisan-cap answer)")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return err + [f"steps must be a list of 1-{MAX_STEPS}"]
    ids = [s.get("id") for s in steps]
    if len(set(ids)) != len(ids) or not all(ids):
        err.append("step ids missing or repeated")
    for i, s in enumerate(steps):
        sid = s.get("id", f"#{i}")
        k = s.get("kind")
        if k not in KINDS:
            err.append(f"{sid}: kind {k!r}")
            continue
        try:
            if ts_of(s["stop"]) <= ts_of(s["start"]):
                err.append(f"{sid}: stop before start")
        except (KeyError, ValueError, TypeError) as e:
            err.append(f"{sid}: start/stop: {e}")
        if not isinstance(s.get("min_gap_sec"), (int, float)) or s["min_gap_sec"] < 5:
            err.append(f"{sid}: min_gap_sec >= 5 required")
        legs = []
        if k == "buy":
            legs = [(s.get("exchange_id"), s.get("side"), s.get("max_price"))]
            for f in ("max_cash", "chunk_cash"):
                if not isinstance(s.get(f), (int, float)) or s[f] <= 0:
                    err.append(f"{sid}: {f} must be > 0")
        elif k == "sell":
            src = next((x for x in steps[:i] if x.get("id") == s.get("of_step")), None)
            if src is None or src.get("kind") not in ("buy", "target_buy"):
                err.append(f"{sid}: of_step must name an earlier buy step")
            else:
                try:
                    if ts_of(s["start"]) < ts_of(src["stop"]):
                        err.append(f"{sid}: starts before {src['id']} stops (its base position would include buys still coming)")
                except (KeyError, ValueError, TypeError):
                    pass
            p = s.get("min_price")
            if not isinstance(p, (int, float)) or not 0.01 <= p <= 0.99 or not on_tick(p):
                err.append(f"{sid}: min_price {p!r} not a tick in [0.01, 0.99]")
            if not isinstance(s.get("chunk_shares"), (int, float)) or s["chunk_shares"] < 1:
                err.append(f"{sid}: chunk_shares >= 1 required")
            if not isinstance(s.get("outside_min"), (int, float)) or not 0.5 <= s["outside_min"] <= 1.0:
                err.append(f"{sid}: outside_min in [0.5, 1] required")
            if not isinstance(s.get("outside_polls", 3), int) or s.get("outside_polls", 3) < 1:
                err.append(f"{sid}: outside_polls >= 1 required")
        else:
            if not any(x.get("id") == s.get("after_step") for x in steps[:i]):
                err.append(f"{sid}: after_step must name an earlier step")
            c = s.get("candidates")
            if not isinstance(c, list) or not c:
                err.append(f"{sid}: candidates required")
                c = []
            legs = [(x.get("exchange_id"), x.get("side"), x.get("max_price")) for x in c]
            for f in ("target_min", "target_fallback", "chunk_cash", "max_cash"):
                if not isinstance(s.get(f), (int, float)) or s[f] <= 0:
                    err.append(f"{sid}: {f} must be > 0")
        for ex, side, px in legs:
            if not known(str(ex)):
                err.append(f"{sid}: unknown exchange {ex!r}")
            if side not in ("yes", "no"):
                err.append(f"{sid}: side {side!r}")
            if not isinstance(px, (int, float)) or not 0.01 <= px <= 0.99 or not on_tick(px):
                err.append(f"{sid}: max_price {px!r} not a tick in [0.01, 0.99]")
    return err


def asks_for(book: dict, side: str) -> list:
    """Cheapest-first (price, qty) to BUY `side`: YES asks, or NO via 1 - YES bids."""
    if side == "yes":
        lad = [(float(l["price"]), float(l["quantity"])) for l in book.get("asks") or [] if float(l.get("quantity") or 0) > 0]
    else:
        lad = [(round(1 - float(l["price"]), 3), float(l["quantity"])) for l in book.get("bids") or [] if float(l.get("quantity") or 0) > 0]
    return sorted(lad)


def walk(lad: list, cash: float, cap: float) -> tuple:
    """(shares, cost, limit) buying with `cash` through levels priced <= cap; limit = the last level touched."""
    sh = cost = 0.0
    limit = None
    for px, q in lad:
        if px > cap + 1e-9 or cash - cost < px:
            break
        n = min(q, math.floor((cash - cost) / px))
        if n <= 0:
            break
        sh += n
        cost += n * px
        limit = px
    return sh, cost, limit


class PuntExecutor:
    """One tick at a time; the adapter does all I/O. Adapter interface:
      now() -> epoch s;  positions() -> {exchange: YES qty} (raises on failure);  book(ex) -> {bids, asks} or None (stale);
      cash() -> free cash;  outside(ex, side) -> (polymarket p, kalshi p) of `side` winning, either may be None;
      leader() -> best rival balance or None;  place(ex, side, qty, price, key, expires_at_epoch, purpose) -> order id (raises with .status);
      cancel_ours(order_ids) -> None;  dry -> bool;  abort_requested() -> bool;  fills_cost(order_ids) -> (shares, cost) or None;
      optional: known(ex) -> bool, outside_stamp(ex) -> poll time, clear_others(ex, keep_ids) -> None, settled() -> set of exchanges"""

    def __init__(self, plan_path: str, state_path: str, expected_sha: str, adapter, max_budget: float, cash_floor: float = 0.0):
        self.a = adapter
        self.plan_path, self.state_path = plan_path, state_path
        self.cash_floor = cash_floor
        self.refused = None
        self.plan: dict = {}
        try:
            raw = open(plan_path, "rb").read()
            got = hashlib.sha256(raw).hexdigest()
            if not expected_sha or got != expected_sha:
                self.refused = f"plan hash {got[:12]} does not match the approved {str(expected_sha)[:12] or '(none)'}"
            else:
                self.plan = json.loads(raw)
                errs = validate(self.plan, getattr(adapter, "known", lambda ex: True), max_budget)
                if errs:
                    self.refused = "invalid plan: " + "; ".join(errs)
        except (OSError, ValueError) as e:
            self.refused = f"plan unreadable: {e}"
        if self.refused:
            log.warning("PUNT refused: %s", self.refused)
        self.state = self._load()

    # ---------------------------------------------------------------- state
    def _load(self) -> dict:
        try:
            st = json.load(open(self.state_path))
            if st.get("plan_id") == self.plan.get("plan_id") and st.get("sha") == self._sha():
                return st
            log.warning("PUNT state belongs to another plan: starting fresh")
        except FileNotFoundError:
            pass
        except ValueError as e:
            log.warning("PUNT state unreadable (%s): starting fresh", e)
        return {"plan_id": self.plan.get("plan_id"), "sha": self._sha(), "steps": {}, "aborted": None, "log": []}

    def _sha(self) -> Optional[str]:
        try:
            return sha256_file(self.plan_path) if self.plan else None
        except OSError:
            return None

    def save(self):
        d = os.path.dirname(self.state_path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".punt_state.")
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    def note(self, msg: str):
        log.warning("PUNT %s", msg)
        self.state["log"] = (self.state.get("log") or [])[-199:] + [[self.a.now(), msg]]

    def st(self, sid: str) -> dict:
        return self.state["steps"].setdefault(sid, {"status": "pending", "base": None, "n": 0, "orders": [], "last": 0.0,
                                                     "errors": 0, "committed": 0.0, "polls": 0, "chosen": None})

    # ---------------------------------------------------------------- helpers
    def legs(self) -> set:
        """Every exchange the plan may trade (the bot keeps its other sleeves off these races)."""
        out = set()
        for s in self.plan.get("steps") or []:
            if s.get("exchange_id"):
                out.add(str(s["exchange_id"]))
            for c in s.get("candidates") or []:
                out.add(str(c.get("exchange_id")))
        return out

    def held(self, pos: dict, ex: str, side: str) -> float:
        q = float(pos.get(str(ex), 0.0))
        return q if side == "yes" else -q

    def our_orders(self) -> list:
        return [o for v in self.state["steps"].values() for o in v.get("orders", [])]

    def spent(self, step: dict, filled: float, limit_cap: float) -> float:
        """Cash this step has used: the venue's own fill cost when the fills ingested match the position, else filled shares
        at the cap (an upper bound, so the budget is never overrun when the fill feed lags)."""
        ids = [o["id"] for o in step.get("orders", []) if o.get("id") is not None]
        fc = self.a.fills_cost(ids) if ids else None
        if fc is not None and abs(fc[0] - filled) < 0.5:
            return fc[1]
        # upper bound: every share filled at no more than its own order's limit; give the fills to the highest limits first
        left, cost = filled, 0.0
        for o in sorted(step.get("orders", []), key=lambda o: -o["price"]):
            n = min(left, o["qty"])
            cost += n * o["price"]
            left -= n
        return cost + max(0.0, left) * limit_cap

    def budget_left(self) -> float:
        """The plan budget bounds the cash brought in from outside the punt: the "buy" steps. A target_buy reinvests what
        the sell steps raised and is bounded by its own max_cash and the free cash."""
        b = float(self.plan.get("budget") or 0.0)
        kinds = {x["id"]: x["kind"] for x in self.plan.get("steps") or []}
        return b - sum(v.get("committed", 0.0) for k, v in self.state["steps"].items() if kinds.get(k) == "buy")

    def abort_all(self, why: str):
        if self.state.get("aborted"):
            return
        self.state["aborted"] = why
        self.note(f"ABORT: {why}")
        live = [o["id"] for o in self.our_orders() if o.get("id") is not None and self.a.now() - o.get("t", 0) < ORDER_TTL + 5]
        if live and not self.a.dry:
            try:
                self.a.cancel_ours(live)
            except Exception as e:
                self.note(f"cancel on abort failed: {e}")
        self.save()

    # ---------------------------------------------------------------- tick
    def tick(self) -> Optional[str]:
        """Do at most one order. Returns a short description of what it did, or None."""
        if self.refused or self.state.get("aborted"):
            return None
        if self.a.abort_requested():
            self.abort_all("abort file present")
            return "abort"
        now = self.a.now()
        steps = self.plan["steps"]
        if all(self.st(s["id"])["status"] in ("done", "aborted") for s in steps):
            return None
        active = [s for s in steps if self.st(s["id"])["status"] not in ("done", "aborted") and ts_of(s["start"]) <= now]
        if not active:
            return None
        try:
            pos = self.a.positions()
        except Exception as e:
            log.warning("PUNT positions read failed (%s): nothing this tick", e)
            return None
        for s in steps:
            v = self.st(s["id"])
            if v["status"] in ("done", "aborted") or ts_of(s["start"]) > now:
                continue
            if now > ts_of(s["stop"]):
                v["status"] = "done"
                self.note(f"{s['id']}: window closed ({v.get('why') or 'time'})")
                self._cancel_step(v)
                self.save()
                continue
            if v.get("pending"):
                did = self._send_pending(s, v)       # the same payload, next tick (it expires within ORDER_TTL)
            else:
                did = getattr(self, "_" + s["kind"])(s, v, pos, now)
            self.save()
            if did:
                return did
        return None

    def _cancel_step(self, v: dict):
        live = [o["id"] for o in v.get("orders", []) if o.get("id") is not None and self.a.now() - o.get("t", 0) < ORDER_TTL + 5]
        if live and not self.a.dry:
            try:
                self.a.cancel_ours(live)
            except Exception as e:
                self.note(f"cancel failed: {e}")

    def _send(self, s: dict, v: dict, ex: str, side: str, qty: int, price: float, what: str) -> Optional[str]:
        """Place one capped limit order with the step's next idempotency key. The exact payload (incl. its expiry) is saved
        BEFORE the send, so a lost response, a 5xx or a crash resends exactly that payload (the venue replays it; a changed
        payload under a used key is a 409)."""
        now = self.a.now()
        last = v["orders"][-1] if v.get("orders") else None
        if last is not None and not last.get("closed"):
            # review B4: the venue's expiry sweep lags the TTL, so the previous chunk may still rest: cancel it and size the
            # next one on the next tick's position (an order of unknown id is waited out instead)
            if last["id"] is not None and not self.a.dry:
                try:
                    self.a.cancel_ours([last["id"]])
                except Exception as e:
                    if int(getattr(e, "status", 0) or 0) in TRANSIENT:
                        self.note(f"{s['id']}: cancel of {last['id']} failed ({e}): next tick")
                        v["last"] = now
                        return None                  # 4xx: already filled, expired or cancelled
            elif last["id"] is None and now - last["t"] < ORDER_TTL + 60:
                return None
            last["closed"] = True
            v["last"] = now
            self.save()
            return None
        if ex not in (v.get("cleared") or []) and not self.a.dry:
            # the venue never matches us with ourselves: any other resting order of ours on this leg (MM, a sleeve) is
            # cancelled before the first punt order there
            try:
                getattr(self.a, "clear_others", lambda ex, keep: None)(ex, {o["id"] for o in self.our_orders()})
            except Exception as e:
                self.note(f"{s['id']}: clearing other orders on {ex} failed ({e}): next tick")
                v["last"] = now
                return None
            v["cleared"] = (v.get("cleared") or []) + [ex]
        # the plan hash is in the key: a revised plan reusing its plan_id never collides with the old keys (review R2-6)
        v["pending"] = {"key": f"punt-{self.plan['plan_id']}-{s['id']}-{v['n']}-{str(self.state.get('sha') or '')[:8]}", "ex": ex, "side": side, "qty": int(qty),
                        "price": round(price, 3), "exp": now + ORDER_TTL, "purpose": "punt_exit" if what == "SELL" else "punt",
                        "what": what}
        self.save()
        return self._send_pending(s, v)

    def _send_pending(self, s: dict, v: dict) -> Optional[str]:
        p, now = v["pending"], self.a.now()
        if now > p["exp"] - 2:
            # never resend an expired payload; it may have been placed: count it at its limit and move to the next key
            return self._booked(s, v, None, "outcome unknown, payload expired: counted at its limit")
        try:
            oid = self.a.place(p["ex"], p["side"], p["qty"], p["price"], p["key"], p["exp"], p["purpose"])
        except Exception as e:
            status = int(getattr(e, "status", 0) or 0)
            if status == 409:                        # our own pending key was already used by this payload: it exists
                return self._booked(s, v, None, f"409 on our pending key ({e}): counted at its limit")
            if status in TRANSIENT:
                self.note(f"{s['id']}: {p['what']} {p['ex']} {p['side']} x{p['qty']} @{p['price']} outcome unknown ({e}): "
                          "same payload next tick")
                v["last"] = now
                return None
            v["pending"] = None                      # rejected: the order does not exist; the next order takes a new key
            v["n"] += 1
            v["errors"] += 1
            self.note(f"{s['id']}: {p['what']} rejected ({e}); {v['errors']} in a row")
            if v["errors"] >= MAX_ERRORS:
                v["status"] = "aborted"
                v["why"] = "3 rejected orders"
                self.note(f"{s['id']}: ABORTED after {MAX_ERRORS} rejected orders")
            v["last"] = now
            return None
        self._booked(s, v, oid, "")
        return f"{s['id']} {p['what']}"

    def _booked(self, s: dict, v: dict, oid, why: str) -> None:
        p, now = v["pending"], self.a.now()
        v["pending"] = None
        v["orders"].append({"id": oid, "key": p["key"], "ex": p["ex"], "side": p["side"], "qty": p["qty"], "price": p["price"], "t": now})
        v["n"] += 1
        v["errors"] = 0
        v["last"] = now
        self.note(f"{s['id']}: {'DRY ' if self.a.dry else ''}{p['what']} {p['ex']} {p['side']} x{p['qty']} limit {p['price']} "
                  f"(key {p['key']}){': ' + why if why else ''}")
        return None

    def _waiting(self, s: dict, v: dict, now: float) -> bool:
        """One order at a time per step, at least min_gap_sec apart, the last one expired."""
        return now - v["last"] < max(float(s["min_gap_sec"]), ORDER_TTL + 2 if v.get("orders") else 0.0)

    def _buy_into(self, s: dict, v: dict, pos: dict, now: float, ex: str, side: str, cap: float, max_cash: float) -> Optional[str]:
        if v["base"] is None:
            v["base"] = self.held(pos, ex, side)
            v["status"] = "active"
        filled = max(0.0, self.held(pos, ex, side) - v["base"])
        v["filled"] = filled
        if self._waiting(s, v, now):
            return None                              # the last order may still rest: keep it counted at its limit
        used = self.spent(v, filled, cap)
        v["committed"] = used
        budget = self.budget_left() if s["kind"] == "buy" else float("inf")
        room = min(max_cash - used, budget, float(self.a.cash()) - self.cash_floor, float(s["chunk_cash"]))
        if not self.plan.get("cap_exempt"):
            room = min(room, 5000.0 - used)          # no exemption: the 5,000 partisan cap bounds the whole step
        if room < MIN_ORDER_CASH:
            if max_cash - used < MIN_ORDER_CASH or budget < MIN_ORDER_CASH:
                v["status"] = "done"
                self.note(f"{s['id']}: complete: {filled:.0f} shares for ~{used:.0f}")
            return None
        book = self.a.book(ex)
        if not book:
            return None
        sh, cost, limit = walk(asks_for(book, side), room, cap)
        if not sh or limit is None:
            return None                              # touch above the cap: wait (never chase)
        qty = math.floor(room / limit)
        v["committed"] = used + qty * limit          # counted at the limit until the venue says otherwise
        return self._send(s, v, ex, side, qty, limit, "BUY")

    def _buy(self, s, v, pos, now):
        lo = s.get("abort_if_outside_below")
        if lo is not None:
            o = [x for x in self.a.outside(str(s["exchange_id"]), s["side"]) if x is not None]
            if o and max(o) < lo:
                v["status"] = "aborted"
                v["why"] = f"outside price {max(o):.3f} below {lo}"
                self.note(f"{s['id']}: ABORTED: outside {max(o):.3f} < {lo} (called against us)")
                self._cancel_step(v)
                return None
        return self._buy_into(s, v, pos, now, str(s["exchange_id"]), s["side"], float(s["max_price"]), float(s["max_cash"]))

    def _sell(self, s, v, pos, now):
        src = next(x for x in self.plan["steps"] if x["id"] == s["of_step"])
        sv = self.st(src["id"])
        ch = sv.get("chosen") or {}
        ex = str(src.get("exchange_id") or ch.get("exchange_id") or "")
        side = src.get("side") or ch.get("side")
        if not ex or side is None:
            return None
        if ex in (getattr(self.a, "settled", lambda: set())() or set()):
            # review B2: a settled market drops out of the positions read; never read that as a sale (stage 2 stays shut)
            v["status"] = "aborted"
            v["why"] = "stage-1 market settled: no sale; stage 2 stays shut (by hand from here)"
            self.note(f"{s['id']}: ABORTED: {ex} settled")
            return None
        bought = float(sv.get("filled") or 0.0)
        have = self.held(pos, ex, side)
        if v["base"] is None:
            if bought < 1:
                return None
            v["base"] = have
            v["bought"] = bought
            v["status"] = "active"
        sold = min(max(0.0, v["base"] - have), float(sum(o["qty"] for o in v.get("orders", []))))   # only what our sells can explain
        v["sold"] = sold
        left = min(v.get("bought", bought) - sold, have)          # never more than the venue position on that side
        if left < 1:
            if sold > 0 or have < 1:
                v["status"] = "done"
                self.note(f"{s['id']}: sold {sold:.0f} (nothing left to sell)")
            return None
        pm, k = self.a.outside(ex, side)
        both = pm is not None and k is not None and min(pm, k) >= float(s["outside_min"])
        single = (pm is None) != (k is None) and max(x for x in (pm, k) if x is not None) >= float(s.get("single_venue_min", 0.97))
        stamp = getattr(self.a, "outside_stamp", lambda ex: None)(ex)    # count each outside poll once, not each tick
        if stamp is None or stamp != v.get("stamp"):
            v["stamp"] = stamp
            v["polls"] = v["polls"] + 1 if (both or single) else 0
        elif not (both or single):
            v["polls"] = 0
        if v["polls"] < int(s.get("outside_polls", 3)) or self._waiting(s, v, now):
            return None
        q = int(min(left, float(s["chunk_shares"])))
        if q < 1:
            return None
        opp = "no" if side == "yes" else "yes"     # selling `side` at >= p = buying the other side at <= 1 - p
        return self._send(s, v, ex, opp, q, round(1 - float(s["min_price"]), 3), "SELL")

    def _target_buy(self, s, v, pos, now):
        if v.get("chosen") is None:
            # the gate is checked only until a candidate is chosen (review R2-1: a sell step that aborts later, e.g. the
            # market settles after 80% was sold, must not freeze a stage 2 already under way)
            prev = self.st(s["after_step"])
            prev_s = next(x for x in self.plan["steps"] if x["id"] == s["after_step"])
            if prev_s["kind"] == "sell":
                frac = float(prev.get("sold") or 0.0) / max(1.0, float(prev.get("bought") or 0.0))
                if prev["status"] == "aborted" or frac < float(s.get("min_sold_frac", 1.0)):
                    return None                      # review S6: a sale that sold nothing never opens stage 2
                proceeds = float(prev.get("sold") or 0.0) * float(prev_s["min_price"])
            else:
                if prev["status"] != "done":
                    return None
                proceeds = 0.0
            if v.get("target") is None:
                lead = None
                for _ in range(2):
                    try:
                        lead = self.a.leader()
                    except Exception as e:
                        log.warning("PUNT leaderboard read failed: %s", e)
                    if lead:
                        break
                t = max(float(s["target_min"]), float(s.get("target_k_leader", 0.0)) * lead) if lead else float(s["target_fallback"])
                v["target"] = t
                self.note(f"{s['id']}: target {t:,.0f} (best rival {lead if lead else 'unread'})")
            # review B2/S9: stage 2 reinvests what the sale raised (at its floor) plus what is left of the plan budget
            cash = min(float(self.a.cash()) - self.cash_floor, float(s["max_cash"]), proceeds + max(0.0, self.budget_left()))
            for c in s["candidates"]:
                book = self.a.book(str(c["exchange_id"]))
                if not book:
                    continue
                lad = asks_for(book, c["side"])
                if not lad or lad[0][0] < float(c.get("min_price", 0.0)) or lad[0][0] > float(c["max_price"]):
                    continue
                sh, cost, limit = walk(lad, cash, float(c["max_price"]))
                if sh + (cash - cost) >= v["target"]:
                    v["chosen"] = {"exchange_id": str(c["exchange_id"]), "side": c["side"], "cap": limit, "cash": cash}
                    self.note(f"{s['id']}: chose {c['exchange_id']} {c['side']}: {sh:,.0f} shares for {cost:,.0f} (cap {limit})")
                    break
            if v.get("chosen") is None:
                v["why"] = "no candidate reaches the target: cash kept"
                return None
        ch = v["chosen"]
        return self._buy_into(s, v, pos, now, ch["exchange_id"], ch["side"], float(ch["cap"]), float(ch["cash"]))


# ---------------------------------------------------------------- dry-run CLI against a book dump
class BookAdapter:
    """Dry adapter over a book dump: orders fill virtually at once at the walked prices (the position moves), so the
    whole plan prints. Outside prices come from --outside {exchange: [pm, kalshi]} JSON (default: none)."""

    dry = True

    def __init__(self, books: dict, cash: float, at: float, outside: Optional[dict] = None, leader: Optional[float] = None):
        self.books, self.c, self.t = books, cash, at
        self.pos: dict = {}
        self.out = outside or {}
        self.lead = leader
        self.sent: list = []

    def now(self):
        return self.t

    def positions(self):
        return dict(self.pos)

    def book(self, ex):
        return self.books.get(str(ex))

    def cash(self):
        return self.c

    def outside(self, ex, side):
        v = self.out.get(str(ex))
        if not v:
            return (None, None)
        return tuple(None if x is None else (x if side == "yes" else 1 - x) for x in v)

    def leader(self):
        return self.lead

    def known(self, ex):
        return str(ex) in self.books

    def abort_requested(self):
        return False

    def fills_cost(self, ids):
        rows = [r for r in self.sent if r[6] in set(ids)]
        return (sum(r[4] for r in rows), sum(r[5] for r in rows))

    def cancel_ours(self, ids):
        pass

    def place(self, ex, side, qty, price, key, expires_at, purpose="punt"):
        lad = asks_for(self.books[str(ex)], side)
        got = cost = 0.0
        for px, q in lad:
            if px > price + 1e-9 or got >= qty:
                break
            n = min(q, qty - got)
            got += n
            cost += n * px
        held = self.pos.get(str(ex), 0.0)
        pairs = min(got, max(0.0, -held if side == "yes" else held))   # the venue nets YES against NO: each pair pays 1
        self.c += pairs - cost
        self.pos[str(ex)] = held + (got if side == "yes" else -got)
        oid = f"dry-{len(self.sent) + 1}"
        self.sent.append((ex, side, qty, price, got, cost, oid))
        return oid


def main(argv=None):
    ap = argparse.ArgumentParser(description="punt executor dry run against a book dump (sends nothing)")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--books", default=os.path.join(HERE, "data", "books_latest.json"))
    ap.add_argument("--cash", type=float, default=87000.0)
    ap.add_argument("--at", required=True, help="ISO time with offset, e.g. 2026-11-03T17:31:00-05:00")
    ap.add_argument("--minutes", type=float, default=30.0, help="simulated minutes to run, one tick per 10 s")
    ap.add_argument("--outside", default=None, help='JSON {"exchange": [pm, kalshi]}')
    ap.add_argument("--leader", type=float, default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    books = json.load(open(a.books))
    ad = BookAdapter(books, a.cash, ts_of(a.at), json.loads(a.outside) if a.outside else None, a.leader)
    st = os.path.join(tempfile.mkdtemp(prefix="punt_dry_"), "state.json")
    ex = PuntExecutor(a.plan, st, sha256_file(a.plan), ad, max_budget=200000.0)
    if ex.refused:
        print("REFUSED:", ex.refused)
        return 1
    for _ in range(int(a.minutes * 6)):
        ex.tick()
        ad.t += 10.0
    for row in ad.sent:
        print("order %s %s x%d limit %.3f -> filled %d for %.0f (%s)" % row)
    print("cash left %.0f, positions %s" % (ad.c, {k: round(v) for k, v in ad.pos.items()}))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "orders"} for k, v in ex.state["steps"].items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
