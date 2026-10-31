"""Main loop.

    python -m sigbot.run --dry-run            # compute and log everything, place nothing (default)
    python -m sigbot.run --live               # trade
    python -m sigbot.run --once --dry-run     # one cycle

Each cycle (cycle_sec):
  1. account + positions + open orders (3 reads)
  2. bulk price snapshot for the whole universe (3 reads)
  3. arb pre-filter from prices -> order books of candidate races -> greedy plan -> atomic execution
  4. fills ingestion (new fills -> store, attributed to our orders)
  5. tape polling, round-robin over quoted + arb markets (<= tape_poll_markets reads)
  6. order books for quoted markets -> market-maker quote cycle (cancel/replace within a write budget)
  7. every analytics_every_sec: recompute mm overrides from logged fills/tape/quotes
Rate limits: 100 reads / 30 writes per minute per account; the client budgets 90 / 27 and the loop sizes its
reads so a cycle fits: 3 + 3 + ~n_candidates*2.5 + 1 + tape_poll_markets + n_markets.
"""
from __future__ import annotations

import argparse
from zoneinfo import ZoneInfo
import json
import logging
import math
import os
import signal
import threading
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

from .analytics import compute as compute_overrides
from .api import ApiError, SuperMarketClient
from .arb import ArbEngine, Chunk
from .config import BotConfig
from .mm import MarketMaker, TICK
from .news import NewsSweeper
from . import risk
from . import meanrev
from .store import Store
from .tilt import TiltHarvester
from .direction import DirectionTrader
from .feed import RealtimeFeed
from .shadow import tick as shadow_tick
from .tracker import Tracker
from .allocator import build_state_from_bot, offload_check
from .proposals.dmm_lag_follow import LagFollower
from .proposals.inside_quoter import InsideQuoter
from .proposals.volatile_moves import MoveDetector, VolatileMoves
from .proposals.outside_sweep import (CatchupSweep, avg_yes_from_fills, inputs_from_model, pnl_from_fills, rows_from_venue_quotes,
                                      BUILD_WINDOW_FAST, BUILD_WINDOW_SLOW, CLIP_SLIPPAGE, CLIP_WAIT, LAST_ENTRY as CATCHUP_LAST_ENTRY, TAKE_PROFIT_SLOW)
from .proposals import cheap_favourites as CF
from .proposals import gap_pilot as GP
from .proposals import cash_raise as CR
from .proposals import shock_mode as SHK
from .venues.http import Http as _ShockHttp
from .venues.polymarket import Polymarket as _ShockPM
from .venues.kalshi import Kalshi as _ShockKS
from .proposals.dev_churn import (DevChurn, overround_series, medians_from_series, vol_ratio, mid_overround, big_holding,
                                  sell_all_due, LAST_ENTRY as CHURN_LAST_ENTRY, VOL_RECENT, VOL_BASE, EXCLUDE as CHURN_EXCLUDE)
from .proposals import election_night as EN
from .proposals import wind_down as WD
from .proposals import punt_executor as PE
from .proposals import offload_executor as OE
from .proposals.own_book import OwnLevels
from . import strategy_monitor as SMON
from . import strategy_stops as STOPS


def cheapfav_races_of(bot) -> set:
    """Races the cheap-favourites sleeve holds (empty when the sleeve is not wired, e.g. on test stubs)."""
    f = getattr(bot, "cheapfav_races", None)
    return f() if callable(f) else set()


LASTLOOK_MAX_AGE = 15.0     # seconds: the oldest REST touch the MM's last look accepts when the feed book is stale
def _blind_rec(v) -> dict:
    """A gap-pilot blind entry record: {"n": notional, "win": "D" / "R" / None}; old records were a bare notional."""
    if isinstance(v, dict):
        return {"n": float(v.get("n", 0.0) or 0.0), "win": v.get("win") if v.get("win") in ("D", "R") else None}
    return {"n": float(v or 0.0), "win": None}


def expiry_in(seconds: float) -> str:
    """An order expiry `seconds` from NOW. Computed right before each order, never once for a batch: with slow writes each order of a
    batch takes a few write latencies, and a batch-wide expiry makes every order after the first arrive expired (sim slow_writes)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def shock_races_of(bot) -> set:
    """Races where shock mode holds positions or has an entry in flight (empty when it is not wired, e.g. on test stubs).
    The set paths (recycler, free unwinds, offload, churn) leave these races alone: a single-leg position next to another
    sleeve's NO looks like an arb set."""
    g = getattr(bot, "shock_races", None)
    return g() if callable(g) else set()


def punt_races_of(bot) -> set:
    """Races the punt executor or the election-night sleeve trade (any fill or order of WD.HOLD_PURPOSES) plus every race
    of an enabled punt plan (BACKLOG 0c / 0000). No other path sells, unwinds, quotes, clips or enters them (empty when
    not wired, e.g. on test stubs)."""
    g = getattr(bot, "punt_races", None)
    return g() if callable(g) else set()


class _PuntAdapter:
    """The punt executor's I/O on the live bot (proposals/punt_executor.py). In the bot's dry-run mode orders are logged
    and filled virtually at the walked feed book, so a dry run shows the whole sequence."""

    def __init__(self, bot):
        self.b = bot
        self.vpos: dict = {}
        self.vcash = 0.0

    @property
    def dry(self) -> bool:
        return bool(self.b.dry_run)

    def now(self) -> float:
        return time.time()

    def positions(self) -> dict:
        p = self.b.api.positions(self.b.slug)           # raises on failure: the executor does nothing that tick
        m = {str(x["exchangeId"]): float(x["quantity"]) for x in p.get("positions", []) if not x.get("settled")}
        self._settled = {str(x["exchangeId"]) for x in p.get("positions", []) if x.get("settled")}
        for ex, q in self.vpos.items():
            m[ex] = m.get(ex, 0.0) + q
        return m

    def book(self, ex):
        return self.b.fresh_book(str(ex), 30.0)

    def cash(self) -> float:
        try:
            c = float(self.b.api.account().get("balance"))
        except Exception as e:                          # unread: the cycle's figure (the venue rejects an overdraft anyway)
            log.warning("PUNT account read: %s", e)
            c = float(getattr(self.b, "cash", 0.0) or 0.0)
        return c - self.vcash - shock_reserve(self.b)   # shock's reserve is not ours (0 once its last entry has passed, 3 Nov)

    def exit_cap_ok(self, ex, side, qty, pos) -> bool:
        """Would buying qty of `side` keep |net D| within the partisan cap (or no larger than now)? Unknown means no: no
        marks yet (after a restart every leg would count at 0.5)."""
        try:
            prices = self.b.mm.prices
            if not prices:
                return False
            before = {str(k): float(v) for k, v in pos.items()}
            after = dict(before)
            after[str(ex)] = after.get(str(ex), 0.0) + (qty if side == "yes" else -qty)
            return self.b.wind_down_partisan_ok(before, after, prices)
        except Exception as e:
            log.warning("PUNT exit cap check: %s", e)
            return False

    def _row(self, ex):
        return next((r for r in (getattr(self.b.direction, "rows", None) or []) if str(r.get("exchange_id")) == str(ex)), None)

    OUTSIDE_MAX_AGE = 300.0     # review S4: an outside row older than this (feed stalled) is no evidence for a sale or an abort

    def outside(self, ex, side):
        r = self._row(ex)
        if not r or time.time() - float(r.get("ts") or 0.0) > self.OUTSIDE_MAX_AGE:
            return (None, None)
        f = lambda x: None if x is None else (float(x) if side == "yes" else 1.0 - float(x))
        return f(r.get("pm")), f(r.get("kalshi"))

    def outside_stamp(self, ex):
        r = self._row(ex)
        return r.get("ts") if r else None

    def leader(self):
        r = self.b.api.get(f"/tournaments/{self.b.slug}/leaderboard", period="all", sort="pnl", limit=5, offset=0)
        mine = r.get("myRank")
        rows = [x for x in r.get("leaderboard") or [] if x.get("rank") != mine]
        return 100000.0 + max(float(x["pnl"]) for x in rows) if rows else None

    def known(self, ex) -> bool:
        return str(ex) in self.b.universe.by_exchange

    def abort_requested(self) -> bool:
        return os.path.exists(os.path.join(HERE, "params", "punt_abort"))

    def fills_cost(self, ids):
        ids = [i for i in ids if i is not None and not str(i).startswith("dry-")]
        if not ids or self.b.store is None:
            return None
        rows = self.b.store.rows(f"SELECT side, ABS(quantity) q, price FROM fills WHERE order_id IN ({','.join('?' * len(ids))})", ids)
        if any(r["side"] != "yes" for r in rows):
            return None                                 # NO-side fill prices are stored in YES terms: use the upper bound
        return sum(r["q"] for r in rows), sum(r["q"] * r["price"] for r in rows)

    def fills_qty(self, ids):
        """Shares our orders `ids` filled (any side), per the ingested fills; None when unreadable (review R2-3)."""
        ids = [i for i in ids if i is not None and not str(i).startswith("dry-")]
        if not ids or self.b.store is None:
            return None
        try:
            rows = self.b.store.rows(f"SELECT ABS(quantity) q FROM fills WHERE order_id IN ({','.join('?' * len(ids))})", ids)
        except Exception as e:
            log.warning("PUNT fills read: %s", e)
            return None
        return float(sum(r["q"] for r in rows))

    def cancel_ours(self, ids):
        err = None
        for i in ids:                                    # every id is tried; a transient failure is raised at the end (review R3-6)
            try:
                self.b.api.cancel_order(i)
            except ApiError as e:
                log.warning("PUNT cancel %s: %s", i, e)
                if e.status in PE.TRANSIENT:            # unknown whether it still rests: the executor decides (review B4 / R2-4)
                    err = err or e
        if err is not None:
            raise err

    def recover_order(self, ex, side, qty, price, exp, purpose):
        """review R2-2: find a placeholder order on the venue (orders carry no idempotency key, so by exchange, side, buy,
        limit, quantity and the expiry second) and record it with its purpose; None when it is not there."""
        want = int(exp)
        # GET /orders?status=all returns newest first (confirmed by a live read, 26 Oct 2026), so a page wholly older than the send
        # time ends the paging; at most 5 pages whatever the order (review R3b-1)
        for o in self.b.api.open_orders(exchange_id=str(ex), status="all", max_pages=5,
                                        created_after=exp - PE.ORDER_TTL - 120.0) or []:
            try:
                if o.get("side") != side or o.get("action") != "buy" or o.get("priceLimit") is None \
                        or abs(float(o["priceLimit"]) - price) > 1e-6 or not o.get("expirationDate"):
                    continue
                q = float(o.get("quantity") or 0.0)
                if not 0 < q <= qty:                                        # the venue may shrink an order to the cash (R3-3)
                    continue
                got = datetime.fromisoformat(str(o["expirationDate"]).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                continue
            if abs(got - want) <= 1.0:
                self.b.store.record_order(o["id"], str(ex), None, side, "buy", price, int(qty), purpose, response=o)
                return o["id"]
        return None

    def clear_others(self, ex, keep):
        for o in self.b.api.open_orders(exchange_id=str(ex)) or []:
            if o.get("id") not in keep:
                self.b.api.cancel_order(o["id"])
                log.warning("PUNT cancelled our other order %s on %s before trading it", o.get("id"), ex)

    def settled(self) -> set:
        return set(getattr(self, "_settled", set()))

    def place(self, ex, side, qty, price, key, expires_at, purpose="punt"):
        if self.dry:
            got, cost = 0.0, 0.0
            for px, q in PE.asks_for(self.book(ex) or {}, side):
                if px > price + 1e-9 or got >= qty:
                    break
                n = min(q, qty - got)
                got, cost = got + n, cost + n * px
            held = self.positions().get(str(ex), 0.0)
            pairs = min(got, max(0.0, -held if side == "yes" else held))
            self.vcash += cost - pairs
            self.vpos[str(ex)] = self.vpos.get(str(ex), 0.0) + (got if side == "yes" else -got)
            return f"dry-{key}"
        exp = datetime.fromtimestamp(expires_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # fixed per payload (review B1)
        with self.b.trade_lock:
            # review S10: one attempt, no internal retries while the trade lock is held; the executor resends the saved payload
            resp = self.b.api.place_order(str(ex), side, "buy", int(qty), price, expiration=exp, key=key, retries=0)
            self.b.store.record_order(resp.get("orderId"), str(ex), None, side, "buy", price, int(qty), purpose, response=resp)
        return resp.get("orderId")


class _OffloadAdapter:
    """The offload executor's I/O on the live bot (proposals/offload_executor.py). While dry (the bot's dry run or offload_dry_run)
    nothing is sent or cancelled: fills are virtual, at the basket's quote or the order's limit."""

    def __init__(self, bot):
        self.b = bot
        self.sell_ok = bool(getattr(bot.cfg, "offload_conventional_sell", False))
        self.vpos: dict = {}
        self.vcash = 0.0

    @property
    def dry(self) -> bool:
        return bool(self.b.dry_run or self.b.cfg.offload_dry_run)

    @property
    def cap(self) -> float:
        return float(self.b.cfg.mm.portfolio_max_partisan_notional)

    def now(self) -> float:
        return time.time()

    def lock(self):
        return self.b.trade_lock

    def positions(self) -> dict:
        p = self.b.api.positions(self.b.slug)           # raises on failure: the executor sends nothing that tick
        m = {str(x["exchangeId"]): float(x["quantity"]) for x in p.get("positions", []) if not x.get("settled")}
        for ex, q in self.vpos.items():
            m[ex] = m.get(ex, 0.0) + q
        return m

    def sale_view(self, pos: dict) -> dict:
        return self.b.wind_down_positions(pos)

    def cheapfav_open(self) -> dict:
        return {str(k): float(v) for k, v in (getattr(self.b, "cheapfav_open", None) or {}).items()}

    def balance(self) -> float:
        return float(self.b.api.account().get("balance")) + self.vcash

    def book(self, ex):
        try:
            return own_levels(self.b).net(str(ex), self.b.get_book(str(ex), 50, max_age=10.0))
        except ApiError as e:
            log.warning("OFFLOAD book %s: %s", ex, e)
            return None

    def race_legs(self, race: str) -> list:
        return list(self.b.universe.races[race].exchange_ids)

    def busy(self, race: str) -> Optional[str]:
        """Why another sleeve owns this race: never the sale's to touch (the sale's own races are not in punt_held_races)."""
        b = self.b
        if race in Bot.punt_held_races(b):
            return "punt / election night"
        if race in shock_races_of(b):
            return "shock mode"
        if b.churn is not None and any(l["race"] == race for l in b.churn.lots):
            return "high-frequency arb"
        try:
            hold = json.load(open(getattr(b, "wind_down_hold_path", None) or os.path.join(HERE, "params", "wind_down_hold.json")))
        except FileNotFoundError:
            hold = {}
        except Exception as e:
            return f"hold file unreadable ({e})"
        if race in {str(r) for r in hold.get("races", [])}:
            return "hold file"
        held = {str(e) for e in hold.get("exchanges", [])}
        catchup = {str(e) for e, q in (getattr(b, "catchup_open", None) or {}).items() if q}
        catchup |= {str(e) for e in (getattr(getattr(b, "catchup", None), "ctx", None) or {})}   # a fill not ingested yet is in the positions, not the ledger
        for e in b.universe.races[race].exchange_ids:
            if e in held:
                return "hold file"
            if e in catchup:
                return "catch-up sweep"
        return None

    def blocked(self) -> Optional[str]:
        b = self.b
        if not getattr(b, "first_cycle_done", False):
            return "first cycle not finished"          # the ledgers the sale takes out of the positions are not built yet
        if time.time() < b.killed_until:
            return "kill switch"
        if strategy_paused(b, "exit:raise"):
            return "exit:raise paused"
        return None

    def cheapfav_ok(self) -> Optional[str]:
        if strategy_paused(self.b, "exit:cheapfav"):
            return "exit:cheapfav paused"
        return None if strategy_paused(self.b, "cheapfav") else "the cheap-favourites sleeve is not paused: it would buy these back"

    def pull(self, exs: list, now: float) -> bool:
        if self.dry:
            return True
        try:
            return Bot.wind_down_pause_legs(self.b, exs, now, retries=0)     # no client retries under the lock (they sleep)
        except Exception as e:                          # a timeout: unknown whether the cancel landed, so the leg waits
            log.warning("OFFLOAD pull %s: %s", exs, e)
            return False

    def pull_cost(self, exs: list) -> int:
        return 0 if self.dry else sum(1 for e in exs if (self.b.open_orders_cache or {}).get(e))

    def set_floor(self, race: str):
        return (getattr(self.b.cfg, "wind_down_set_floor", None) or {}).get(race)

    def cap_ok(self, before: dict, after: dict) -> bool:
        prices = self.b.mm.prices
        return bool(prices) and Bot.wind_down_partisan_ok(self.b, before, after, prices)

    def net_d(self, pos: dict) -> Optional[float]:
        prices = self.b.mm.prices
        return risk.exposure(self.b.universe, pos, prices).net_d_notional if prices else None

    def abort_requested(self) -> bool:
        return os.path.exists(os.path.join(HERE, "params", "offload_abort"))

    def basket(self, race, kind, legs, key, q, proceeds, payout, sgn) -> dict:
        b, tid = self.b, self.b.api.tournament_id
        if self.dry:
            log.info("[DRY] offload basket %s x%d at %.4f: %s", race, q, proceeds, legs)
            for l in legs:
                self.vpos[l["exchangeId"]] = self.vpos.get(l["exchangeId"], 0.0) + sgn * q
            self.vcash += q * proceeds
            return {"results": [{"data": {"exchangeId": l["exchangeId"], "quantityTraded": q, "open": False}} for l in legs]}
        body = [dict(l, tournamentId=tid) for l in legs]
        rec = "unwind_set" if kind == "sell_set" else "unwind_buy_set"
        Bot.stamp_exit(b, race, "offload")
        try:
            resp = b.api.place_multi_leg(body, all_or_none=True, key=key, retries=0)
        except ApiError as e:
            b.store.record_arb(race, rec, body, q, proceeds - payout, proceeds, False, {"error": e.code, "why": "offload"})
            raise
        b.store.record_arb(race, rec, body, q, proceeds - payout, proceeds, True, resp)
        for r, l in zip(resp.get("results", []), legs):
            d = r.get("data") or {}
            b.store.record_order(d.get("orderId"), l["exchangeId"], None, l["side"], l["action"], l["price"], q, "raise_set",
                                 status="filled" if not d.get("open") else "open", response=d)
            self._tag_fills(d.get("orderId"), "raise_set")
        b.cash += q * proceeds
        self.after_basket({e["exchangeId"]: sgn * q for e in legs})
        return resp

    def _tag_fills(self, oid, purpose: str):
        """The main thread ingests fills outside the trade lock: one read before the order was recorded has no purpose, and the
        cheap-favourites ledger and the exit sizing read fills.purpose (the strategy stops join the orders table)."""
        if oid is not None:
            self.b.store.exec("UPDATE fills SET purpose=? WHERE order_id=? AND purpose IS NULL", (purpose, oid))

    def after_basket(self, deltas: dict):
        try:
            self.b.refresh_snapshot(deltas)
        except Exception as e:                          # the basket traded: this must not read as a failed send
            log.warning("OFFLOAD snapshot refresh: %s", e)

    def after_batch(self):
        if not self.dry:
            self.after_basket({})

    def batch(self, orders: list, key: str) -> list:
        b = self.b
        if self.dry:
            out = []
            for o in orders:
                log.info("[DRY] offload leg %s", o)
                d = 1 if o["side"] == "yes" else -1
                self.vpos[o["exchangeId"]] = self.vpos.get(o["exchangeId"], 0.0) + d * o["quantity"]
                self.vcash += o["quantity"] * (1.0 - o["price"])
                out.append({"ok": True, "status": 201, "data": {"orderId": None, "quantityTraded": o["quantity"], "open": False}})
            return out
        tid = b.api.tournament_id
        return b.api.place_batch([dict(o, tournamentId=tid) for o in orders], key=key, max_rounds=1, retries=0).get("results", [])

    def record_leg(self, data: dict, order: dict, purpose: str):
        if self.dry:
            return
        b, ex = self.b, order["exchangeId"]
        b.store.record_order(data.get("orderId"), ex, None, order["side"], "buy", order["price"], order["quantity"], purpose,
                             expires_at=order["expirationDate"], status="open" if data.get("open") else "filled", response=data)
        self._tag_fills(data.get("orderId"), purpose)
        Bot.stamp_exit(b, b.universe.race_of(ex).name, "offload")

    def cancel(self, ids: list):
        err = None
        for i in ids:
            try:
                self.b.api.cancel_order(i, retries=0)
                self.b.store.mark_orders([i], "cancelled")
            except ApiError as e:
                if e.status in PE.TRANSIENT:
                    err = err or e
            except Exception as e:                      # a timeout: it may still rest
                err = err or e
        if err is not None:
            raise err


class ExplainedDirection(DirectionTrader):
    """Directional sleeve restricted to legs whose gap versus outside markets has been explained and marked tradable
    (params/fair_explained.json verdict trade_yes / trade_no), value signals only, one leg per race (both legs of a
    race are the same bet)."""

    VERDICTS = "params/fair_explained.json"

    def _allowed(self) -> dict:
        path = os.path.join(HERE, self.VERDICTS)
        try:
            m = os.path.getmtime(path)
        except OSError:
            return {}
        if getattr(self, "_allow_mtime", None) != m:
            legs = json.load(open(path)).get("legs", {})
            allow = {}
            for key, v in legs.items():
                verdict = v.get("verdict", "")
                if verdict in ("trade_yes", "trade_no"):
                    race = key.rsplit(" [", 1)[0]
                    allow[(race, v.get("party"))] = "yes" if verdict == "trade_yes" else "no"
            self._allow, self._allow_mtime = allow, m
        return self._allow

    CONSENSUS_GAP = 0.05        # Sizhe 2026-10-22: also trade toward the Polymarket/Kalshi consensus when both venues agree
    CONSENSUS_MAX_DISAGREE = 0.04

    def consensus(self, rows) -> dict:
        """(race, party) -> side for legs where Polymarket and Kalshi agree within 4 points and this venue's mid is 5+ points
        away from them, on the favourite's side only (buy the favourite / sell the longshot): favourite_drift.md found
        favourites at 0.75-0.95 win at least as often as priced and that taking the longshot side was the losing trade."""
        out = {}
        for r in rows or []:
            pm, k, fair, mid = r.get("pm"), r.get("kalshi"), r.get("fair"), r.get("sig_mid")
            if None in (pm, k, fair, mid) or abs(pm - k) > self.CONSENSUS_MAX_DISAGREE:
                continue
            gap = fair - mid
            if gap >= self.CONSENSUS_GAP and fair >= 0.5:
                out[(r.get("race"), r.get("party"))] = "yes"     # favourite priced too low here
            elif gap <= -self.CONSENSUS_GAP and fair <= 0.5:
                out[(r.get("race"), r.get("party"))] = "no"      # longshot priced too high here
        return out

    def signals(self, rows, ts):
        allow = dict(self.consensus(rows))
        allow.update(self._allowed())             # explained verdicts take precedence where they exist
        out, races = [], set()
        for s in super().signals(rows, ts):
            if s["kind"] != "value" or allow.get((s["race"], s["party"])) != s["side"] or s["race"] in races:
                continue
            races.add(s["race"])
            out.append(s)
        return out
from .universe import Universe

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log = logging.getLogger("sigbot")


def shock_reserve(bot) -> float:
    """Cash kept free for shock mode (Sizhe 24 Oct: "yes add money for the shock reserve"): cfg.shock_reserve while
    shock_enabled, else 0. Only purpose 'shock' spends it (the shock sleeve reads bot.cash itself)."""
    cfg = getattr(bot, "cfg", None)
    if not getattr(cfg, "shock_enabled", False) or getattr(getattr(bot, "shock", None), "killed", False):
        return 0.0
    sm = sys.modules.get("sigbot.proposals.shock_mode")          # (loaded by shock mode's wiring when it deploys)
    if sm is not None and time.time() > float(getattr(sm, "LAST_ENTRY", float("inf"))):
        return 0.0                                     # no more shock entries: the reserve is free (review 7)
    kf = getattr(bot, "strategy_kill", None)
    if kf is not None and (kf.paused("shock") is not None or kf.paused("all") is not None):   # written pauses, not holds
        return 0.0                                     # the sleeve is stopped: nothing to keep the cash for
    return float(getattr(cfg, "shock_reserve", 0.0) or 0.0)


def cfg_ts(cfg, key: str) -> Optional[float]:
    """A config time as epoch seconds, None when it is empty."""
    s = getattr(cfg, key, "") or ""
    return datetime.fromisoformat(s).timestamp() if s else None


def spendable_cash(bot) -> float:
    """The cash every sleeve but shock mode may plan with: free cash minus the shock reserve, so each sleeve's own floor
    sits on top of it (cheap favourites 1k + reserve, high-frequency arb 3k + reserve, arbs' cash_reserve + reserve)."""
    return float(getattr(bot, "cash", 0.0) or 0.0) - shock_reserve(bot)


# entry paths gated by another strategy's pause: recycles and offload fund set arb, the inside quoter is MM, tilt / lag /
# news are directional. "all" (KILL-ENTRIES, strategy_stops.py) pauses every entry path; exit paths ("exit:...") are
# paused only by name (an exit that closed more than it held) and never by "all".
PAUSE_ALIASES = {"recycle": ("arb",), "offload": ("arb",), "inside": ("mm",), "tilt": ("direction",), "lag": ("direction",),
                 "news": ("direction",), "gap": ("cheapfav",), "exit:gap": ("exit:cheapfav",)}   # the pilot trades through the cheapfav ledger


def strategy_paused(bot, name: str) -> bool:
    """True when params/strategy_kill.json pauses this sleeve's NEW entries (strategy monitor, strategy stops or by
    hand), directly, through PAUSE_ALIASES or through "all"; each pause is logged once. A bot without the kill file (test
    stubs) is never paused."""
    kf = getattr(bot, "strategy_kill", None)
    if kf is None:
        return False
    why = None
    held = getattr(getattr(bot, "stops", None), "hold", None) or {}
    for n in (name,) + PAUSE_ALIASES.get(name, ()) + (() if name.startswith("exit:") else ("all",)):
        why = kf.paused(n)
        if why is None and held.get(n, 0.0) > time.time():
            why = "held by the strategy stops (first breach)"
        if why is not None:
            why = why if n == name else "%s (via %s)" % (why, n)
            break
    logged = bot.monitor_logged
    if why is None:
        logged.discard(name)
        return False
    if name not in logged:
        logged.add(name)
        log.warning("STRATEGY MONITOR: %s paused: %s", name, why)
    return True


def own_levels(b) -> OwnLevels:
    """Our resting orders as book levels, to net out of the books the scanner prices: the cycle's open-order read, plus
    orders the store recorded after that read (placed by the MM/tilt/direction since; the venue never matches us with
    ourselves, so a leg priced off our own bid is a rejected basket)."""
    own = OwnLevels()
    for ex_o, lst in (b.open_orders_cache or {}).items():
        for o in lst:
            rem = float(o.get("quantity") or 0) - float(o.get("quantityFilled") or o.get("filledQuantity") or 0)
            if o.get("priceLimit") is not None:
                own.upsert(o.get("id"), ex_o, o.get("side"), o.get("action", "buy"), float(o["priceLimit"]), rem)
    snap_ts = getattr(b, "open_orders_ts", 0.0)
    if b.store is not None and snap_ts:
        try:
            for o in b.store.rows("SELECT order_id, exchange_id, side, action, price, quantity FROM orders "
                                  "WHERE placed_at > ? AND status='open' AND purpose != 'arb'", (snap_ts - 1.0,)):
                if o["price"] is not None and o["order_id"] not in own.orders:
                    own.upsert(o["order_id"], str(o["exchange_id"]), o["side"], o["action"] or "buy", float(o["price"]), float(o["quantity"]))
        except Exception as e:
            log.debug("own orders since snapshot: %s", e)
    return own


class ArbScanner(threading.Thread):
    """Polls the bulk price snapshot every `arb_scan_sec` seconds, aligned to `arb_scan_offset` past the 5-second grid,
    fetches books only for races whose best bids/asks already show edge, and executes atomic baskets at once.
    Shares the API client (rate buckets are thread-safe), the store and the arb engine with the main loop."""

    def __init__(self, bot: "Bot"):
        super().__init__(name="arb-scanner", daemon=True)
        self.bot = bot
        self.stats = {"passes": 0, "candidates": 0, "executed": 0, "errors": 0, "last_pass": 0.0}
        self.best_unfunded = None     # best chunk seen that the arb budget could not pay for (for capital recycling)
        self.fast_log: list = []      # timestamps of fast requotes in the last minute
        self.last_offload = 0.0

    def run(self):
        b = self.bot
        last = 0.0
        while not b.stop:
            if b.feed_ok():
                # event-driven: run as soon as the feed delivers a batch (the venue publishes 1-s batches), at most every
                # arb_scan_min_gap seconds and at least every arb_scan_sec_feed seconds
                feed = b.feed
                if feed is not None:
                    feed.wait_update(b.cfg.arb_scan_sec_feed)
                else:
                    time.sleep(b.cfg.arb_scan_sec_feed)
                gap = b.cfg.arb_scan_min_gap - (time.time() - last)
                if gap > 0:
                    time.sleep(gap)
            else:
                period = b.cfg.arb_scan_sec
                offset = b.cfg.arb_scan_offset % period
                now = time.time()
                target = math.floor(now / period) * period + offset
                if target <= now:
                    target += period
                time.sleep(max(0.0, target - time.time()))
            last = time.time()
            try:
                self.one_pass()
                self.recycle_pass()
                self.offload_pass()
                self.free_unwind_pass()
                self.fast_requote_pass()
            except Exception as e:
                self.stats["errors"] += 1
                log.exception("arb scanner: %s", e)
                time.sleep(2.0)

    def one_pass(self):
        b = self.bot
        feed_ok = b.feed_ok()
        base, base_ts = b.rest_snapshot
        if feed_ok and base and time.time() - base_ts < 30:
            prices = b.feed_prices(base)         # feed books carry the touch; the REST snapshot supplies the rest
        else:
            prices = b.scan_api.prices(b.universe.all_exchanges())
            b.rest_snapshot = (prices, time.time())
            if feed_ok:
                prices = b.feed_prices(prices)
        b.latest_prices = (prices, time.time())
        self.stats["passes"] += 1
        self.stats["last_pass"] = time.time()
        if not b.cfg.arb.enabled or time.time() < b.killed_until or strategy_paused(b, "arb"):
            return
        cands = b.arb.candidates_from_prices(prices)
        if not cands:
            return
        self.stats["candidates"] += len(cands)
        # rank by apparent edge from the snapshot so the few book reads go to the best races
        def apparent(race):
            bids = [prices[e]["bestBid"] for e in race.exchange_ids]
            asks = [prices[e]["bestAsk"] for e in race.exchange_ids]
            sell = sum(bids) - 1 if all(x is not None for x in bids) else 0.0
            buy = 1 - sum(asks) - b.arb.x_for(race) if all(x is not None for x in asks) else 0.0
            return max(sell, buy)
        # skip races whose arb budget is already used up, and races whose snapshot edge is below the threshold:
        # only then spend reads on books
        def sell_ret(race):
            # snapshot return of a sell-set (edge over its cost), so races eligible for the high-return per-race
            # ceiling are not filtered out at their normal cap before their books are even read (review, 22 Oct)
            bids = [prices[e]["bestBid"] for e in race.exchange_ids]
            if not all(x is not None for x in bids):
                return None
            cost = len(bids) - sum(bids)
            return (sum(bids) - 1) / cost if cost > 0 else None
        min_cost = 1.0
        cands = [r for r in cands if b.arb.race_budget_left(r.name, sell_ret(r)) >= b.cfg.arb.min_set_qty * min_cost
                 and apparent(r) >= min(b.cfg.arb.min_edge_sell, b.cfg.arb.min_edge_buy)]
        if b.arb.budget_left(spendable_cash(b)) < b.cfg.arb.min_set_qty * min_cost and not b.cfg.arb.recycle_enabled:
            cands = []
        cands.sort(key=apparent, reverse=True)
        # hft_strategies.md: 17-18 of 33 failed baskets priced a leg off our OWN resting tilt/MM bid (the venue will not
        # match us with ourselves), 7 re-sent a basket on a book that still showed the size we had just taken
        own = own_levels(b)
        last_fill = getattr(self, "last_fill_ts", {})
        self.last_fill_ts = last_fill
        cand_books, n_rest = {}, 0
        for race in cands[: (b.cfg.arb_scan_books_feed if feed_ok else b.cfg.arb_scan_books)]:
            fb = {ex: b.fresh_book(ex, 30.0) for ex in race.exchange_ids}
            if race.name in last_fill and any(v is not None and v.get("feed_ts", 0) <= last_fill[race.name] for v in fb.values()):
                continue                        # no book newer than our last basket in this race yet
            if all(v is not None for v in fb.values()):
                cand_books[race.name] = {ex: own.net(ex, bk) for ex, bk in fb.items()}
                continue
            if n_rest >= b.cfg.arb_scan_books:
                continue
            n_rest += 1
            try:
                cand_books[race.name] = {ex: b.get_book(ex, 50, b.scan_api) for ex in race.exchange_ids}
            except ApiError as e:
                log.warning("scanner book %s: %s", race.name, e)
        with b.trade_lock:
            # remember the best chunk regardless of budget, so the main loop can decide whether to recycle capital
            allc = []
            for rname, books in cand_books.items():
                allc.extend(b.arb.chunks_for_race(b.universe.races[rname], books))
            allc = [c for c in allc if c.qty >= b.cfg.arb.min_set_qty and not b.arb.no_buy(c.race)]   # a target recycling may buy
            if allc:
                best = max(allc, key=lambda c: c.ret)
                self.best_unfunded = {"race": best.race, "kind": best.kind, "ret": best.ret, "cost": best.cost, "qty": best.qty, "ts": time.time()}
            if not b.cfg.arb.new_sets:
                return      # cash mode: the best set is remembered for recycle_pass; free cash never buys a new one
            plan = b.arb.plan(cand_books, spendable_cash(b))
            if not plan:
                return
            log.info("scanner arb plan: %s", [(c.race, c.kind, c.qty, round(c.edge, 4), round(100 * c.ret, 2)) for c in plan])
            res = b.arb.execute(plan, None)
            n = sum(1 for r in res if r.get("ok"))
            for r in res:
                if r.get("ok"):
                    self.last_fill_ts[r["chunk"].race] = time.time()
            if self.best_unfunded and any(not r.get("ok") and r["chunk"].race == self.best_unfunded["race"] for r in res):
                self.best_unfunded["failed"] = True
            self.stats["executed"] += n
            if n:
                try:
                    b.cash = float(b.scan_api.account().get("balance") or b.cash)
                except ApiError:
                    pass
                b.refresh_snapshot({e: -q for e, q in b.basket_fills(res).items()})

    def fast_requote_pass(self):
        """Half of our MM fills were stale: the DMM moves its touch 1-2 ticks in bursts and our level-1 order sat at the old
        price until the next 30-60 s cycle. Between cycles, re-quote a market whose best bid or ask moved by a tick or more
        since we last quoted it, from its feed book, at most mm_fast_requotes_per_min times a minute (2 writes each)."""
        b = self.bot
        n_max = b.cfg.mm_fast_requotes_per_min
        if (n_max <= 0 or not b.cfg.mm.enabled or not b.feed_ok() or not b.mm.quoted or time.time() < b.killed_until
                or strategy_paused(b, "mm") or (getattr(b, "wind_down", None) is not None and WD.mm_flat_due(time.time(), getattr(b, "wd_mm_end", WD.MM_FLAT)))):
            return
        now = time.time()
        self.fast_log = [t for t in self.fast_log if now - t < 60]
        if len(self.fast_log) >= n_max:
            return
        for ex in list(b.mm.quoted):
            if strategy_paused(b, "mm"):
                break                                # paused or held while this pass ran (a pass can take minutes)
            st = b.mm.state.get(ex)
            touch = getattr(st, "last_touch", None) if st is not None else None
            if not touch or touch[0] is None or touch[1] is None or st.last_reserve is None:
                continue
            fb = b.fresh_book(ex, 30.0)
            if fb is None or fb.get("bestBid") is None or fb.get("bestAsk") is None:
                continue
            bb, ba = fb["bestBid"], fb["bestAsk"]
            if abs(bb - touch[0]) < TICK - 1e-9 and abs(ba - touch[1]) < TICK - 1e-9:
                continue
            with b.trade_lock:
                b.mm.force_cancel = True
                try:
                    summ = b.mm.quote_cycle({ex: fb}, b.mm_positions(b.positions_snapshot), b.pressures(), {ex: b.open_orders_cache.get(ex, [])},
                                            b.fills_count_cache, max(0.0, spendable_cash(b)), b.last_cycle_id, 2)
                finally:
                    b.mm.force_cancel = False
            b.book_cache[ex] = (fb, bb, ba, fb.get("latestPrice"))
            b.book_ts.pop(ex, None)                  # a feed book: never a last-look fallback under an older REST stamp
            if summ.get("requoted"):
                self.fast_log.append(time.time())
                self.stats["fast_requotes"] = self.stats.get("fast_requotes", 0) + 1
                log.info("fast requote %s: touch %.3f/%.3f -> %.3f/%.3f", ex, touch[0], touch[1], bb, ba)
            else:
                if summ.get("pulled"):
                    # the move closed the last quotable side (the xv gate is measured from the mid): the resting quotes were
                    # pulled, a write that counts against the per-minute budget like a requote
                    self.fast_log.append(time.time())
                    self.stats["fast_pulls"] = self.stats.get("fast_pulls", 0) + 1
                    log.info("fast pull %s: touch %.3f/%.3f -> %.3f/%.3f left no quotable side", ex, touch[0], touch[1], bb, ba)
                st.last_touch = (bb, ba)       # reserve did not move enough to requote: do not re-check this move every second
            if len(self.fast_log) >= n_max:
                break

    def recycle_pass(self):
        """Capital recycling every scanner pass (was: once per 30-60 s main cycle, against the first 3 held sets only).
        When the best arb seen is blocked for capital, price the exit of EVERY held set from its feed book (unwind_quote
        walks the asks, so the spread and depth are in the cost), unwind the one that gives up the least if the new set
        beats it by recycle_min_gain, and buy the new set at once from a fresh book, all inside the trade lock."""
        b = self.bot
        best = self.best_unfunded
        if (not best or best.get("failed") or time.time() - best.get("ts", 0) > 10 or not b.cfg.arb.recycle_enabled
                or not b.arb.committed or not b.feed_ok() or time.time() < b.killed_until or strategy_paused(b, "recycle")):
            return
        if b.cfg.arb.new_sets and b.arb.budget_left(spendable_cash(b)) >= best["cost"] * max(b.cfg.arb.min_set_qty, 1):
            return                                  # not capital-blocked: the normal scanner path takes it (in cash mode
                                                    # free cash never buys sets, so a better set is reached only by recycling)
        tgt = best["race"]
        cash_mode = not b.cfg.arb.new_sets
        if cash_mode and b.arb.recycle_credit >= best["cost"] * max(b.cfg.arb.min_set_qty, 1):
            self.spend_recycle_credit(tgt)      # an earlier unwind's cash is still unspent: buy the better set with it,
            return                              # never unwind another held set while it waits
        target = {ex: b.fresh_book(ex, 10.0) for ex in b.universe.races[tgt].exchange_ids}
        if any(v is None for v in target.values()):
            return
        held_books = {}
        prot = b.arb.protected()
        for rname in list(b.arb.committed):
            race = b.universe.races.get(rname)
            if race is None or rname == tgt or rname in prot:     # high-frequency arb's sets never fund anything
                continue
            fb = {ex: b.fresh_book(ex, 30.0) for ex in race.exchange_ids}
            if all(v is not None for v in fb.values()):
                held_books[rname] = fb
        if not held_books:
            return
        with b.trade_lock:
            rec = b.arb.recycle(dict(best, fresh_books=target), held_books, b.positions_snapshot, spendable_cash(b), None, refresh=b.fresh_positions)
            if rec and not rec.get("dry"):
                b.refresh_snapshot(b.unwind_deltas([rec]))
        if not rec:
            return
        self.stats["recycles"] = self.stats.get("recycles", 0) + 1
        self.best_unfunded = None
        if rec.get("dry"):
            return
        freed = rec.get("qty", 0) * rec.get("proceeds", 0.0)
        if cash_mode:
            b.arb.recycle_credit += freed
        try:
            b.cash = float(b.scan_api.account().get("balance") or b.cash)
            fresh = {ex: b.get_book(ex, 50, b.scan_api, max_age=3.0) for ex in b.universe.races[tgt].exchange_ids}
            with b.trade_lock:
                plan = [c for c in b.arb.plan({tgt: fresh}, spendable_cash(b), extra_budget=(b.arb.recycle_credit if cash_mode else freed), recycle=True)
                        if c.race == tgt]
                if plan:
                    res = b.arb.execute(plan, None)
                    spent = sum(r["chunk"].qty * r["chunk"].cost for r in res if r.get("ok") and r.get("chunk") is not None)
                    if cash_mode:
                        b.arb.recycle_credit = max(0.0, b.arb.recycle_credit - spent)
                    log.info("recycle follow-through %s: %d basket(s) filled", tgt, sum(1 for r in res if r.get("ok")))
                    b.refresh_snapshot({e: -q for e, q in b.basket_fills(res).items()})
                else:
                    b.arb.cooldown[tgt] = time.time() + 60      # do not unwind for this target again for 60 s
                    log.warning("recycle: target %s gone after unwind; capital stays free for the scanner", tgt)
            b.cash = float(b.scan_api.account().get("balance") or b.cash)
        except ApiError as e:
            log.warning("recycle follow-through %s: %s", tgt, e)

    def spend_recycle_credit(self, tgt: str):
        """Cash mode: buy the better set with cash an earlier recycle unwind freed but its follow-through could not spend."""
        b = self.bot
        if b.arb.cooldown.get(tgt, 0) > time.time():
            return
        try:
            fresh = {ex: b.get_book(ex, 50, b.scan_api, max_age=3.0) for ex in b.universe.races[tgt].exchange_ids}
            with b.trade_lock:
                plan = [c for c in b.arb.plan({tgt: fresh}, spendable_cash(b), extra_budget=b.arb.recycle_credit, recycle=True) if c.race == tgt]
                if not plan:
                    b.arb.cooldown[tgt] = time.time() + 60
                    return
                res = b.arb.execute(plan, None)
                spent = sum(r["chunk"].qty * r["chunk"].cost for r in res if r.get("ok") and r.get("chunk") is not None)
                b.arb.recycle_credit = max(0.0, b.arb.recycle_credit - spent)
                log.info("recycle credit spent on %s: %.0f (credit left %.0f)", tgt, spent, b.arb.recycle_credit)
                b.refresh_snapshot({e: -q for e, q in b.basket_fills(res).items()})
            b.cash = float(b.scan_api.account().get("balance") or b.cash)
        except ApiError as e:
            log.warning("recycle credit %s: %s", tgt, e)

    def offload_pass(self):
        """Cross-sleeve offload every offload_scan_sec (was: every 5th main cycle, ~2.5 min): exit costs of every held
        set and tilt/directional position are recomputed from the latest feed-overlaid prices each run."""
        b = self.bot
        if time.time() - self.last_offload < b.cfg.offload_scan_sec or time.time() < b.killed_until or strategy_paused(b, "offload"):
            return
        self.last_offload = time.time()
        b.offload_report(0)

    def free_unwind_pass(self):
        """Every free_unwind_scan_sec, with every leg's feed book fresh: unwind held sets whose asks sum to the payout or
        less (the locked profit is realised at no cost and the capital comes back). Positions are re-read after any
        unwind so a stale snapshot can never buy legs for sets we no longer hold."""
        b = self.bot
        if not b.arb.committed or not b.feed_ok() or time.time() < b.killed_until or strategy_paused(b, "exit:unwinds"):
            return
        if time.time() - b.last_free_unwind < b.cfg.free_unwind_scan_sec:
            return
        held = {}
        cf_races = cheapfav_races_of(b) | shock_races_of(b) | punt_races_of(b)   # a cheap-favourite / shock / punt position next to another
        for rname in list(b.arb.committed):                  # sleeve's NO is not a set
            race = b.universe.races.get(rname)
            if race is None or rname in cf_races:
                continue
            fb = {ex: b.fresh_book(ex, 30.0) for ex in race.exchange_ids}
            if all(v is not None for v in fb.values()):
                held[rname] = fb
        if not held:
            return
        b.last_free_unwind = time.time()
        with b.trade_lock:
            fu = b.arb.free_unwinds(held, b.positions_snapshot, None, refresh=b.fresh_positions)
            if fu:
                b.refresh_snapshot(b.unwind_deltas(fu))      # in the lock: churn re-checks its sets against it under the lock
        if fu:
            log.info("free unwinds (scanner): %s", fu)
            try:
                b.cash = float(b.scan_api.account().get("balance") or b.cash)
            except ApiError:
                pass


class Bot:
    def __init__(self, cfg: BotConfig, dry_run: bool):
        self.cfg = cfg
        self.dry_run = dry_run
        os.makedirs(os.path.join(HERE, os.path.dirname(cfg.db_path)), exist_ok=True)
        self.store = Store(os.path.join(HERE, cfg.db_path))
        self.api = SuperMarketClient(cfg.api_key, cfg.base_url, reads_per_min=cfg.reads_per_min, writes_per_min=cfg.writes_per_min)
        self.scan_api = SuperMarketClient(cfg.api_key, cfg.base_url, reads_per_min=cfg.scan_reads_per_min, writes_per_min=cfg.scan_writes_per_min)
        t = self.api.tournament(cfg.tournament_slug)
        self.api.tournament_id = t["id"]
        self.scan_api.tournament_id = t["id"]
        self.slug = cfg.tournament_slug
        self.cash = float(t.get("myBalance") or 0.0)
        log.info("tournament %s id=%s balance=%.2f status=%s end=%s", t["name"], t["id"], self.cash, t["status"], t.get("endDate"))
        mk = self.api.markets()
        self.universe = Universe.from_markets(mk)
        self.feed = None
        if cfg.feed_enabled:
            try:
                self.feed = RealtimeFeed(cfg.api_key, cfg.base_url, t["id"], log=logging.getLogger("sigbot.feed"))
                self.exchanges_by_market = {str(m["id"]): [str(e["id"]) for e in m.get("exchanges", [])] for m in mk}
                self.feed.set_universe(self.exchanges_by_market)
                self.feed.start()
                self.feed_started = time.time()
            except Exception as e:
                log.warning("realtime feed disabled: %s", e)
                self.feed = None
        self.lag = LagFollower(move_pts=cfg.lag_move_pts, qty=cfg.lag_qty) if cfg.lag_enabled else None
        self.inside = (InsideQuoter(universe=self.universe, qty=cfg.inside_qty, min_dmm_spread_ticks=3, min_price=cfg.mm.min_price,
                                    max_price=cfg.mm.max_price, inv_cap=300, ttl_sec=cfg.inside_ttl_sec, max_markets=cfg.inside_max_markets)
                       if cfg.inside_enabled else None)
        self.election = None
        if cfg.election_enabled:
            try:
                ecfg = EN.Config(per_race_cap=cfg.election_per_race_cap, total_cap=cfg.election_total_cap, paper=dry_run)
                self.election = EN.ElectionNight(ecfg, legs=EN.universe_legs(self.universe))
                self.election_stream = EN.OutsideStream()
                self.election_orders: dict = {}
                threading.Thread(target=self.election_loop, name="election-night", daemon=True).start()
                log.warning("ELECTION sleeve enabled: per race %.0f, total %.0f, from %s", ecfg.per_race_cap, ecfg.total_cap,
                            EN.et_str(ecfg.day0() + ecfg.start_et * 3600))
            except Exception as e:
                log.warning("election sleeve disabled: %s", e)
                self.election = None
        self.catchup = (CatchupSweep(notional=cfg.catchup_notional, kill_pnl=cfg.catchup_kill,
                                     max_notional=getattr(cfg, "catchup_notional_max", 5000.0),
                                     max_open_notional=getattr(cfg, "catchup_max_open", 15000.0))
                        if getattr(cfg, "catchup_enabled", False) else None)
        self.catchup_state_path = os.path.join(HERE, "params", "catchup_state.json")
        if self.catchup is not None:
            try:
                st = json.load(open(self.catchup_state_path))
                self.catchup.ctx = {str(k): v for k, v in (st.get("ctx") or {}).items()}
                self.catchup.last_signal = dict(st.get("last_signal") or {})
                self.catchup.exit_try = {str(k): float(v) for k, v in (st.get("exit_try") or {}).items()}
                self.catchup.killed = bool(st.get("killed", False))
            except Exception as e:                   # missing, corrupt or not a dict: start empty (positions without
                log.warning("catchup state unreadable (%s): starting empty", e)   # context are then closed)
            try:                                     # polls / ratings for the conviction points (params/fair_model.json, read once)
                self.catchup.inputs = inputs_from_model(json.load(open(os.path.join(HERE, "params", "fair_model.json"))))
            except Exception as e:
                log.warning("catchup conviction inputs unreadable (%s): no poll / rating points", e)
            try:                                     # the look-back from stored quotes (3 h: the 60-min window and the 2 h unchanged point): no waiting after a restart
                leg_of = {(r.name, p): l.exchange_id for r in self.universe.races.values() for p, l in r.legs.items()}
                vq = self.store.rows("SELECT ts, venue, race, party, COALESCE(mid, (bid + ask) / 2.0) m FROM venue_quotes "
                                     "WHERE ts > ? AND venue IN ('polymarket', 'kalshi', 'sig')", (time.time() - 3 * 3600,))
                boot = rows_from_venue_quotes([(r["ts"], r["venue"], r["race"], r["party"], r["m"]) for r in vq], leg_of)
                self.catchup.push(boot)
                log.info("catchup: %d stored quote rows loaded as history (%d legs)", len(boot), len({r["exchange_id"] for r in boot}))
            except Exception as e:
                log.warning("catchup history load: %s", e)
        self.cheapfav = None
        if getattr(cfg, "cheapfav_enabled", False):
            same = CF.same_terms_exchanges(os.path.join(HERE, "data", "research_contract_rules", "tc_mapping.json"))
            if same is None:
                log.warning("cheapfav: data/research_contract_rules/tc_mapping.json missing: sleeve stays off")
            else:
                self.cheapfav = CF.CheapFavourites(budget=cfg.cheapfav_budget, min_edge=cfg.cheapfav_min_edge, leg_cap=cfg.cheapfav_leg_cap,
                                                   order_notional=cfg.cheapfav_order_notional, cash_floor=cfg.cheapfav_cash_floor,
                                                   hold_limit=getattr(cfg, "churn_skip_holding", 4000.0) or 4000.0, same_terms=same,
                                                   net_max=cfg.cheapfav_net_max, headroom=cfg.cheapfav_headroom,
                                                   min_price=cfg.cheapfav_min_price, max_price=cfg.cheapfav_max_price)
                log.warning("CHEAPFAV sleeve enabled: budget %.0f, min edge %.3f, %.0f per leg, %d same-terms legs",
                            cfg.cheapfav_budget, cfg.cheapfav_min_edge, cfg.cheapfav_leg_cap, len(same))
        self.gap = None
        if getattr(cfg, "gap_enabled", False) and self.cheapfav is not None:   # the pilot trades through the cheap-favourites ledger
            self.gap = GP.GapPilot(budget=cfg.gap_budget, min_edge=cfg.gap_min_edge, leg_cap=cfg.gap_leg_cap,
                                   order_notional=cfg.gap_order_notional, net_max=cfg.gap_net_max, headroom=cfg.cheapfav_headroom,
                                   max_hold_h=cfg.gap_hold_hours, stop=cfg.gap_stop, take_edge=cfg.gap_take_edge,
                                   hold_limit=getattr(cfg, "churn_skip_holding", 4000.0) or 4000.0, same_terms=self.cheapfav.same_terms)
            log.warning("GAP pilot enabled: budget %.0f, min edge %.3f, %.0f per leg, hold <= %.0f h", cfg.gap_budget, cfg.gap_min_edge,
                        cfg.gap_leg_cap, cfg.gap_hold_hours)
        self.shock = (SHK.ShockMode(per_shock=cfg.shock_per_shock, total=cfg.shock_total, kill_pnl=cfg.shock_kill)
                      if getattr(cfg, "shock_enabled", False) else None)
        self.shock_state_path = os.path.join(HERE, "params", "shock_state.json")
        self.shock_open: dict = {}
        self.shock_last_poll = 0.0                   # last fast (midpoints) check
        self.shock_last_full = 0.0                   # last full two-venue poll
        self.shock_rows_last: list = []
        self.shock_fast = SHK.FastDetector()
        self.shock_races_cache: frozenset = frozenset()
        self.shock_hold: dict = {}                   # exchange -> no exit before (a maker cancel failed: it may have filled)
        self.shock_vpos = (0.0, {})                  # (ts, venue positions) re-read under the lock before exits
        # the fast detector's own Polymarket client and circuit breaker: its failures must not blind the direction poller
        self.shock_pm = _ShockPM(_ShockHttp(timeout=5.0)) if self.shock is not None else None
        self.shock_ks = _ShockKS(_ShockHttp(timeout=8.0), os.path.join(HERE, "data")) if self.shock is not None else None
        self.shock_vfail = 0                         # consecutive failed venue position reads (flatten fallback)
        self.shock_exit_sent: dict = {}              # exchange -> last shock exit order (taker or maker) sent
        self.shock_ingest_ts = 0.0                   # last successful fills pull by the shock thread
        self.shock_cleared: dict = {}                # race -> when its orders were cleared for a shock (once per shock)
        self.shock_unknown: list = []                # [(exchange, sent ts, purpose)]: orders whose outcome is unknown
        if self.shock is None and self.store is not None:
            try:                                     # switched off with positions still open: nothing will close them
                _f = [dict(r) for r in self.store.rows("SELECT exchange_id, quantity, price FROM fills WHERE purpose IN ('shock','shock_exit')")]
                _open = pnl_from_fills(_f, {})[1] if _f else {}
                if _open:
                    log.error("SHOCK mode is OFF but its ledger still holds %s: close these by hand or re-enable it", _open)
            except Exception as e:
                log.warning("shock ledger check: %s", e)
        if self.shock is not None:
            try:
                self.shock.load(json.load(open(self.shock_state_path)))
            except Exception as e:                   # missing or corrupt: start empty (positions without context are closed)
                log.warning("shock state unreadable (%s): starting empty", e)
            try:                                     # the 45-min reference window from stored quotes: no wait after a restart
                leg_of = {(r.name, p): l.exchange_id for r in self.universe.races.values() for p, l in r.legs.items()}
                vq = self.store.rows("SELECT ts, venue, race, party, COALESCE(mid, (bid + ask) / 2.0) m FROM venue_quotes "
                                     "WHERE ts > ? AND venue IN ('polymarket', 'kalshi', 'sig')", (time.time() - 3 * 3600,))
                self.shock.push(rows_from_venue_quotes([(r["ts"], r["venue"], r["race"], r["party"], r["m"]) for r in vq], leg_of))
            except Exception as e:
                log.warning("shock history load: %s", e)
            log.warning("SHOCK mode enabled: %.0f per shock, %.0f total, stop %.0f, poll %.0fs (exempt from the partisan cap)",
                        cfg.shock_per_shock, cfg.shock_total, cfg.shock_kill, cfg.shock_poll_sec)
        self.churn = (DevChurn(capital=cfg.churn_capital, cash_floor=cfg.churn_cash_floor, kill=cfg.churn_kill)
                      if getattr(cfg, "churn_enabled", False) else None)
        self.churn_state_path = os.path.join(HERE, "params", "churn_state.json")
        self.churn_med, self.churn_med_ts = {}, 0.0
        if self.churn is not None:
            try:
                self.churn.load(json.load(open(self.churn_state_path)))
            except Exception as e:                   # missing/corrupt: start empty; held sets stay under the normal rules
                log.warning("churn state unreadable (%s): starting empty", e)
        # wind-down (proposals/wind_down.py, backlog 000): late-call sets, legs and MM inventory to cash by 18:00 ET 3 Nov
        self.wind_down = WD.WindDownState() if getattr(cfg, "wind_down_enabled", False) else None
        self.wind_down_path = os.path.join(HERE, "params", "wind_down_state.json")
        self.wd_med, self.wd_med_ts, self.wd_report_ts, self.wd_cap_logged = {}, 0.0, 0.0, 0.0
        self.exit_lock: dict = {}                # race -> {routine: time of its last clip} (wind-down / cash raise; stamp_exit)
        self.wd_foreign_logged: dict = {}
        self.mm_inv_base = float(cfg.mm.max_inventory_notional)
        self.wd_mm_end = WD.mm_end(getattr(cfg.mm, "stop_quoting_at", None))   # MM flat: 12:00 ET 3 Nov or its own stop, if earlier
        if self.wind_down is not None:
            try:
                self.wind_down.load(json.load(open(self.wind_down_path)))
            except FileNotFoundError:
                pass
            except Exception as e:                   # corrupt: start empty (spacing restarts; nothing is sold twice: every
                log.warning("wind-down state unreadable (%s): starting empty", e)   # clip re-reads the positions)
            log.warning("WIND-DOWN enabled: sells from %s ET; MM inventory cap shrinks from %s, flat at %s ET; everything late by %s ET",
                        WD.et_str(WD.START), WD.et_str(WD.MM_SHRINK), WD.et_str(self.wd_mm_end), WD.et_str(WD.DEADLINE))
            if time.time() >= WD.START and cfg.arb.recycle_enabled:
                cfg.arb.recycle_enabled = False          # restart inside the window: off before the scanner thread starts
                log.warning("WIND-DOWN: recycling switched off for the wind-down (free unwinds continue)")
        # strategy monitor (strategy_monitor.py): a sleeve found losing (or paused by hand) in params/strategy_kill.json takes
        # no NEW entries until a human deletes its entry; exits continue; the global kill switch and hard limits are separate
        self.strategy_kill = SMON.KillFile(os.path.join(HERE, "params", SMON.KILL_FILE))
        self.monitor_ts = 0.0
        self.monitor_logged: set = set()
        # per-strategy stops (strategy_stops.py) replace the bot-wide 30-min kill switch when enabled
        self.stops = (STOPS.StrategyStops(cfg, self.store, os.path.join(HERE, "params"),
                                          [r.exchange_ids for r in self.universe.races.values()], write=not dry_run)
                      if getattr(cfg, "strategy_stops_enabled", False) and self.store is not None else None)
        self.stops_log_ts = 0.0
        self.pos_mv: dict = {}
        self.move_detector = MoveDetector()
        self.volatile_paper = VolatileMoves()
        self.inside_posted: dict = {}            # ex -> time of our last inside post (re-post before the venue TTL expires)
        self.tracker = None
        try:
            self.tracker = Tracker.from_store(os.path.join(HERE, cfg.db_path), hours=24, universe=self.universe)
            log.info("market tracker rebuilt from the last 24 h of logs")
        except Exception as e:
            log.warning("market tracker disabled: %s", e)
        self.tracker_mark = time.time()
        log.info("universe: %d open markets, %d races (%d with an Independent leg)", len(self.universe.by_exchange), len(self.universe.races),
                 sum(r.has_independent for r in self.universe.races.values()))
        self.arb = ArbEngine(self.api, self.store, cfg.arb, self.universe, self.slug, cfg.starting_cash, dry_run)
        if self.churn is not None:
            self.arb.protected = lambda: {l["race"] for l in self.churn.lots}
            self.arb.no_buy = self.churn_no_buy
        self.cheapfav_open: dict = {}
        self.cheapfav_skip: dict = {}            # exchange -> until: an entry whose response had no orderId (ledger blind)
        if self.cheapfav is not None:
            # races holding cheap-favourite positions are never unwound by the recycler / free unwinds / offload: a cheapfav
            # R-NO next to another sleeve's D-NO looks like an arb set to every set path (review 24 Oct)
            _prot = self.arb.protected
            self.arb.protected = lambda: set(_prot()) | self.cheapfav_races()
            try:
                self.cheapfav_refresh()
            except Exception as e:
                log.warning("cheapfav ledger at start-up: %s", e)
        if self.shock is not None:
            # races holding shock positions are never touched by the set paths (same reason as cheap favourites)
            _prot2 = self.arb.protected
            self.arb.protected = lambda: set(_prot2()) | self.shock_races()
            try:
                self.shock_ledger()
            except Exception as e:
                log.warning("shock ledger at start-up: %s", e)
        self.punt = None
        if getattr(cfg, "punt_enabled", False):
            try:
                self.punt = PE.PuntExecutor(os.path.join(HERE, cfg.punt_plan_path),
                                            os.path.join(HERE, "params", "punt_state_dry.json" if dry_run else "punt_state.json"),   # review S7
                                            cfg.punt_plan_sha256, _PuntAdapter(self), max_budget=cfg.punt_max_budget,
                                            cash_floor=cfg.punt_cash_floor)
                log.warning("PUNT executor enabled: plan %s, %s", cfg.punt_plan_path,
                            "REFUSED: " + self.punt.refused if self.punt.refused else "hash matches, %d steps" % len(self.punt.plan["steps"]))
            except Exception as e:
                log.warning("punt executor disabled: %s", e)
                self.punt = None
        self.offload = None
        if getattr(cfg, "offload_enabled", False):
            try:
                ad = _OffloadAdapter(self)
                self.offload = OE.OffloadExecutor(os.path.join(HERE, cfg.offload_plan_path),
                                                  os.path.join(HERE, "params", "offload_state_dry.json" if ad.dry else "offload_state.json"),
                                                  cfg.offload_plan_sha256, ad, cfg.offload_target, cfg.offload_speed, cfg.offload_start,
                                                  cfg.offload_stop, cfg.offload_writes_per_min, cfg.offload_price_slack_ticks)
                log.warning("OFFLOAD executor enabled (%s): %s", "DRY" if ad.dry else "LIVE",
                            "REFUSED: " + self.offload.refused if self.offload.refused else "%d rows, target %.0f" % (len(self.offload.rows), cfg.offload_target))
            except Exception as e:
                log.warning("offload executor disabled: %s", e)
                self.offload = None
        # punt / election-night races are never touched by the set paths (recycler, free unwinds, offload, churn)
        _prot3 = self.arb.protected
        self.arb.protected = lambda: set(_prot3()) | punt_races_of(self)
        self.mm = MarketMaker(self.api, self.store, cfg.mm, self.universe, self.slug, cfg.starting_cash, dry_run, HERE)
        self.mm.touch_fn = self.mm_touch       # last look at the freshest book before quotes are sent
        self.mm.protected_fn = lambda: Bot.punt_legs(self)    # review S2b: past stop_quoting_at the MM leaves punt legs' orders alone
        self.cash_needs: dict = {}             # sleeve -> (cash short this cycle, why): what the cash raise may sell for
        self.load_raise_pause()
        self.tilt = TiltHarvester(self.api, self.store, cfg.tilt, self.universe, cfg.starting_cash, dry_run)
        self.direction = ExplainedDirection(self.api, self.store, None, self.universe)
        self.news = NewsSweeper(self.api, self.store, cfg.news, self.universe, cfg.starting_cash, dry_run) if cfg.news.enabled else None
        self.volume: dict[str, float] = {}
        self.book_cache: dict[str, tuple] = {}   # ex -> (book, bestBid, bestAsk, latest)
        self.bars: dict[str, list] = {}          # ex -> 5-minute bars built from live mids (seeded from candle history)
        self.killed_until = 0.0
        self.peak_value = 0.0
        self.last_free_unwind = 0.0
        self.positions_snapshot: dict = {}
        self.open_orders_cache: dict = {}
        self.fills_count_cache: dict = {}
        self.last_cycle_id = 0
        self.allocation_mtime = 0.0
        try:
            self.offload_live = bool((json.load(open(os.path.join(HERE, "config.json"))).get("allocator") or {}).get("offload_live", False))
        except Exception:
            self.offload_live = False
        self.latest_prices: tuple = ({}, 0.0)
        self.book_ts: dict = {}                  # ex -> when book_cache[ex] was read from REST (MM last look)
        self.rest_snapshot: tuple = ({}, 0.0)    # last REST bulk price snapshot (feed quotes are overlaid on it)
        self.trade_lock = threading.Lock()
        self.scanner = None
        self.tape_cursor = 0
        self.last_analytics = 0.0
        self.last_universe_refresh = time.time()
        self.stop = False
        self._warmup_volume()
        self._seed_bars()

    # ------------------------------------------------------------ warm-up
    def _warmup_volume(self):
        """Liquidity ranking from 1h candles (one read per exchange, paced by the client). Cached for the run."""
        cache = os.path.join(HERE, "data/volume_cache.json")
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 48 * 3600:   # fallback ranking only (the tracker picks MM markets); a 7-min rebuild at startup is not worth it
            self.volume = json.load(open(cache))
            self.mm.vol = {k: v for k, v in json.load(open(cache + ".vol")).items()} if os.path.exists(cache + ".vol") else {}
            log.info("volume cache loaded (%d exchanges)", len(self.volume))
            return
        vols, sig = {}, {}
        for ex in self.universe.all_exchanges():
            try:
                h = self.api.price_history(ex, "1h", 168)
            except ApiError as e:
                log.warning("price history %s: %s", ex, e)
                continue
            c = h.get("candles", [])
            vols[ex] = sum(x.get("volume", 0) or 0 for x in c[-168:])
            closes = [x["close"] for x in c[-self.cfg.mm.vol_lookback_hours:] if x.get("close") is not None]
            diffs = [b - a for a, b in zip(closes, closes[1:])]
            sig[ex] = statistics.pstdev(diffs) if len(diffs) > 2 else 0.01
        self.volume, self.mm.vol = vols, sig
        json.dump(vols, open(cache, "w")); json.dump(sig, open(cache + ".vol", "w"))
        log.info("volume computed for %d exchanges", len(vols))

    def _seed_bars(self):
        path = os.path.join(HERE, "data/candles_5m.json")
        try:
            raw = json.load(open(path))
        except Exception:
            raw = {}
        for ex in self.universe.all_exchanges():
            c = [x for x in (raw.get(ex, {}).get("candles") or []) if x.get("close") is not None][-60:]
            self.bars[ex] = [dict(time=x["time"], open=x["open"], high=x["high"], low=x["low"], close=x["close"], volume=x.get("volume") or 0, tradeCount=x.get("tradeCount") or 0) for x in c]
            closes = [x["close"] for x in c[-300:]]
            d = [b - a for a, b in zip(closes, closes[1:])]
            self.mm.vol5m[ex] = statistics.pstdev(d) if len(d) > 10 else 0.004
        log.info("seeded 5-minute bars for %d exchanges", sum(1 for v in self.bars.values() if v))

    def update_bars(self, prices: dict[str, dict]):
        now = time.time()
        bucket = int(now // 300) * 300
        tkey = datetime.fromtimestamp(bucket, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for ex, p in prices.items():
            if p.get("bestBid") is None or p.get("bestAsk") is None:
                continue
            mid = round(0.5 * (p["bestBid"] + p["bestAsk"]), 4)
            bars = self.bars.setdefault(ex, [])
            if bars and bars[-1]["time"] == tkey:
                b = bars[-1]; b["close"] = mid; b["high"] = max(b["high"], mid); b["low"] = min(b["low"], mid)
            else:
                bars.append(dict(time=tkey, open=mid, high=mid, low=mid, close=mid, volume=0, tradeCount=0))
                del bars[:-80]

    def meanrev_predictions(self, exchanges) -> dict[str, float]:
        out = {}
        for ex in exchanges:
            race = self.universe.race_of(ex)
            others = [self.bars.get(l.exchange_id, []) for l in race.legs.values() if l.exchange_id != ex] if race else []
            try:
                out[ex] = meanrev.predict_market(self.bars.get(ex, []), others, horizon=1)
            except Exception as e:
                log.debug("meanrev %s: %s", ex, e)
                out[ex] = 0.0
        return out

    def health(self, total_value: float, cycle_id: int) -> dict:
        """Per-cycle health: fills by sleeve over 5/30 min, 30-min MM markout, drawdown vs trailing-hour peak."""
        now = time.time()
        h = {"fills5": {}, "fills30": {}, "markout30": None, "n_markout": 0, "drawdown": 0.0}
        for mins, key in ((5, "fills5"), (30, "fills30")):
            since = datetime.fromtimestamp(now - mins * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for r in self.store.rows("SELECT purpose, COUNT(*) n FROM fills WHERE filled_at>=? GROUP BY purpose", (since,)):
                h[key][r["purpose"]] = r["n"]
        vals = []
        since = datetime.fromtimestamp(now - 3 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for f in self.store.rows("SELECT exchange_id, side, price, filled_at FROM fills WHERE purpose='mm' AND filled_at>=? AND price IS NOT NULL", (since,)):
            ts = datetime.fromisoformat(f["filled_at"].replace("Z", "+00:00")).timestamp()
            if now - ts < 1800:
                continue
            q = self.store.one("SELECT mid FROM quotes WHERE exchange_id=? AND ts>=? ORDER BY ts ASC LIMIT 1", (f["exchange_id"], ts + 1800))
            if q and q["mid"] is not None:
                vals.append(((q["mid"] - f["price"]) if f["side"] == "yes" else (f["price"] - q["mid"])) / 0.005)
        if vals:
            h["markout30"] = sum(vals) / len(vals); h["n_markout"] = len(vals)
        # trailing-hour peak of the SETTLEMENT-basis value (arb sets at their locked payout), kept in memory: the cycles
        # table stores marked values, which fall when sets are recycled at the asks even as locked profit rises
        hist = getattr(self, "settle_hist", None)
        if hist is None:
            hist = self.settle_hist = []
        hist.append((now, total_value))
        while hist and now - hist[0][0] > 3600:
            hist.pop(0)
        self.peak_value = max(v for _, v in hist)
        h["drawdown"] = self.peak_value - total_value
        return h

    def zero_ev_exits(self, positions: dict[str, float], dir_rows: list, write_budget: int = 2) -> int:
        """Close directional and tilt holdings once this venue's price is within exit_band of the outside fair value:
        the edge is gone, the exit costs ~nothing in expectation and frees the collateral. Arb-set legs are untouched
        (only the sleeve's own filled quantity is closed). Exits cross the spread with a 60 s limit order."""
        if getattr(self, "positions_ok", True) is False:   # an unread account is not an empty one (review 24 Oct)
            return 0
        band = self.cfg.exit_band
        fair = {r["exchange_id"]: r for r in (dir_rows or []) if r.get("fair") is not None and r.get("sig_bid") is not None and r.get("sig_ask") is not None}
        held, strict = {}, set()
        for r in self.store.rows("SELECT exchange_id, side, SUM(ABS(quantity)) q FROM fills WHERE purpose IN ('direction','tilt') GROUP BY 1,2"):
            held[(r["exchange_id"], r["side"])] = held.get((r["exchange_id"], r["side"]), 0.0) + r["q"]
        # market-making inventory too, but only when the exit beats the outside fair by exit_band (inventory left on the
        # wrong side of the Polymarket/Kalshi consensus): crossing the spread on fair-priced MM inventory would just pay it
        for r in self.store.rows("SELECT exchange_id, side, SUM(ABS(quantity)) q FROM fills WHERE purpose IN ('mm','inside') GROUP BY 1,2"):
            key = (r["exchange_id"], r["side"])
            held[key] = held.get(key, 0.0) + r["q"]
            strict.add(key)
        # (the cash raise's tier-1 clips close these holdings too: netted here, or the next exit closes them again and eats
        # into an arb set; review 24 Oct)
        for r in self.store.rows("SELECT exchange_id, side, SUM(ABS(quantity)) q FROM fills WHERE purpose IN ('exit', 'raise') GROUP BY 1,2"):
            opp = "no" if r["side"] == "yes" else "yes"      # an exit buying YES closed held NO, and vice versa
            held[(r["exchange_id"], opp)] = held.get((r["exchange_id"], opp), 0.0) - r["q"]
        # exit orders still resting (60-s TTL vs a 30-s cycle) already cover part of the holding: never send a second one
        # on top (review 22 Oct: two positions were closed past zero and flipped)
        # (the cash raise's clips close holdings the same way: count them too)
        exit_ids = {str(r["order_id"]) for r in self.store.rows("SELECT order_id FROM orders WHERE purpose IN ('exit', 'raise') AND order_id IS NOT NULL")}
        # a leg with an exit / raise order placed in the last 90 s is left alone: positions and open orders are read at
        # different moments, so a 60-s exit can be missing from both and be sent again (live 23 Oct leg 960, 24 Oct leg
        # 898: -300 -> +300 / +100, two exits seconds apart; review 24 Oct)
        recent_exit = {str(r["exchange_id"]) for r in self.store.rows(
            "SELECT DISTINCT exchange_id FROM orders WHERE purpose IN ('exit', 'raise') AND placed_at > ?", (time.time() - 90.0,))
            if "exchange_id" in r.keys()}
        resting_exit: dict = {}
        for ex_o, lst in (getattr(self, "open_orders_cache", None) or {}).items():
            for o in lst:
                if str(o.get("id")) in exit_ids:
                    held_side = "no" if o.get("side") == "yes" else "yes"   # an exit buying YES closes held NO
                    rem = float(o.get("quantity") or 0) - float(o.get("quantityFilled") or o.get("filledQuantity") or 0)
                    resting_exit[(str(ex_o), held_side)] = resting_exit.get((str(ex_o), held_side), 0.0) + max(0.0, rem)
        # BACKLOG 0c: `held` sums fills GROSS, so it can exceed what the sleeves still hold. Never close punt / election legs
        # (whole races), and never the shares that make up a held set (a set's legs all on one side: min |position|)
        punt_ex = Bot.punt_legs(self) if getattr(self, "universe", None) is not None else set()
        set_sh: dict = {}
        for race in (getattr(getattr(self, "universe", None), "races", None) or {}).values():
            qs = [positions.get(e, 0.0) for e in race.exchange_ids]
            if len(qs) >= 2 and all(q < 0 for q in qs):
                for e in race.exchange_ids:
                    set_sh[(e, "no")] = min(-q for q in qs)
            elif len(qs) >= 2 and all(q > 0 for q in qs):
                for e in race.exchange_ids:
                    set_sh[(e, "yes")] = min(qs)
        done = 0
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for (ex, side), q in held.items():
            if done >= write_budget or q < 1 or ex not in fair or ex in recent_exit or ex in punt_ex:
                continue
            # catch-up / cheap favourites / shock keep their own: what they hold on this side is not ours to close. One-sided,
            # so never more than the venue position alone allows (their ledgers and the positions read are taken at
            # different moments; a sleeve on the other side must not make room; review 24 Oct)
            sleeves = [(getattr(self, a, None) or {}).get(ex, 0.0) for a in ("catchup_open", "cheapfav_open", "shock_open")]
            pos = positions.get(ex, 0.0)
            cap = -pos - sum(max(0.0, -v) for v in sleeves) if side == "no" else pos - sum(max(0.0, v) for v in sleeves)
            cap -= set_sh.get((ex, side), 0.0)            # set legs are the set paths' (0c)
            q = int(min(q, cap) - resting_exit.get((ex, side), 0.0))   # never close more than held, net of resting exits
            if q < 1:
                continue
            f = fair[ex]
            if (ex, side) in strict:
                if f.get("pm") is None or f.get("kalshi") is None or abs(f["pm"] - f["kalshi"]) > 0.04:
                    continue
                if not ((side == "no" and f["sig_ask"] <= f["fair"] - band) or (side == "yes" and f["sig_bid"] >= f["fair"] + band)):
                    continue
            if side == "no" and f["sig_ask"] <= f["fair"] + band:        # held NO: buy YES at the ask to close
                order = ("yes", round(f["sig_ask"], 3))
            elif side == "yes" and f["sig_bid"] >= f["fair"] - band:     # held YES: buy NO at 1 - bid to close
                order = ("no", round(1 - f["sig_bid"], 3))
            else:
                continue
            ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            log.info("ZERO-EV EXIT %s %s x%d: venue %s/%s vs fair %.3f", ex, side, q, f["sig_bid"], f["sig_ask"], f["fair"])
            if self.dry_run:
                done += 1; continue
            try:
                resp = self.api.place_order(ex, order[0], "buy", q, order[1], expiration=ttl, key=self.api.new_key("exit"))
                self.store.record_order(resp.get("orderId"), ex, None, order[0], "buy", order[1], q, "exit", response=resp)
                done += 1
            except ApiError as e:
                log.warning("exit %s failed: %s", ex, e)
        return done

    # ------------------------------------------------------------- helpers
    def positions_map(self, strict: bool = False) -> tuple[dict[str, float], float, int]:
        """Venue positions. A failed read returns {} (and positions_ok False), or raises with strict=True: an exit must
        never take an unread account for an empty one."""
        try:
            p = self.api.positions(self.slug)
        except ApiError as e:
            log.warning("positions: %s", e)
            self.positions_ok = False
            if strict:
                raise
            return {}, 0.0, 0
        self.positions_ok = True
        m = {str(x["exchangeId"]): float(x["quantity"]) for x in p.get("positions", []) if not x.get("settled")}
        # the venue's own mark per YES share (marketValue / |quantity|; a NO holding's value is 1 - YES per share): shock
        # mode values its ledger on this basis for the kill switch's drawdown, the same basis as pos_value
        vm = {}
        for x in p.get("positions", []):
            q = float(x.get("quantity") or 0.0)
            if not x.get("settled") and abs(q) >= 1 and x.get("marketValue") is not None:
                per = float(x["marketValue"]) / abs(q)
                vm[str(x["exchangeId"])] = per if q > 0 else 1.0 - per
        self.venue_mark = vm
        self.cost_basis = {str(x["exchangeId"]): float(x.get("costBasis") or 0.0) for x in p.get("positions", []) if not x.get("settled")}
        # arb capital already locked, per race, derived from positions (survives restarts): NO legs' cost basis
        committed = {}
        for race in self.universe.races.values():
            qs = [m.get(l.exchange_id, 0.0) for l in race.legs.values()]
            if qs and all(q < 0 for q in qs):           # a held set: NO on every leg
                committed[race.name] = sum(self.cost_basis.get(l.exchange_id, 0.0) for l in race.legs.values())
        self.arb.committed = committed
        # settlement adjustment: real arb sets valued at their locked payout instead of their marks (the kill switch's
        # drawdown must not count profitable recycling, which closes sets at the asks, as a loss; 22 Oct 21:49)
        mv = {str(x["exchangeId"]): float(x.get("marketValue") or 0.0) for x in p.get("positions", []) if not x.get("settled")}
        self.pos_mv = mv
        self.pos_settled = {str(x["exchangeId"]) for x in p.get("positions", []) if x.get("settled")}
        adj = 0.0
        # read every time (one small query per cycle): a 5-min cache left sets bought in a NEW race unadjusted, so their
        # spread counted as drawdown and tripped the kill switch a minute after an arb burst (sim_pipeline aon_reject, 22 Oct)
        real = {r["race"] for r in self.store.rows("SELECT DISTINCT race FROM arb_trades WHERE ok=1 AND kind IN ('sell_set','buy_set')")}
        for race in self.universe.races.values():
            if race.name not in real:
                continue
            legs = [l.exchange_id for l in race.legs.values()]
            qs = [m.get(e, 0.0) for e in legs]
            if qs and all(q < 0 for q in qs):
                k, payout = min(-q for q in qs), len(legs) - 1
            elif qs and all(q > 0 for q in qs):
                k, payout = min(qs), 1
            else:
                continue
            adj += payout * k - sum(mv.get(e, 0.0) * k / abs(m[e]) for e in legs)
        self.settle_adj = adj
        return m, float(p.get("summary", {}).get("totalMarketValue", 0.0)), len(m)

    def refresh_snapshot(self, deltas: Optional[dict] = None):
        """Re-read positions into the shared snapshot. Call it INSIDE the trade lock, right after a basket or an unwind:
        high-frequency arb's exits trust the snapshot under the lock, and a refresh after the lock let another thread sell a
        free unwind's sets a second time (sim churn_interleave, 24 Oct). If the read fails, `deltas` ({exchange: signed
        change}) are applied to the old snapshot instead, so it never keeps sets that are gone. Returns True only after a
        successful read."""
        try:
            self.positions_snapshot = self.positions_map(strict=True)[0]
            return True
        except ApiError as e:
            snap = dict(self.positions_snapshot or {})
            for ex, d in (deltas or {}).items():
                snap[ex] = snap.get(ex, 0.0) + d
            self.positions_snapshot = snap
            log.warning("positions refresh: %s; snapshot adjusted locally", e)
            return False

    def fresh_positions(self) -> Optional[dict]:
        """Inside the trade lock, right before an exit is sent: the venue's positions now (and the shared snapshot set to
        them), or None if the read failed. Fable 24 Oct: an MM fill between the cycle's read and churn's exit made the exit
        buy YES through zero (-1,700 -> +300) when it was sized from the older snapshot."""
        return self.positions_snapshot if self.refresh_snapshot() else None

    def unwind_deltas(self, unwinds) -> dict:
        """Snapshot change of unwinds [{race, qty}]: every leg's NO short shrinks by qty (NO legs are negative)."""
        out: dict = {}
        for u in unwinds or []:
            race = self.universe.races.get(u.get("race"))
            if race is None or u.get("dry"):
                continue
            for e in race.exchange_ids:
                out[e] = out.get(e, 0.0) + u.get("qty", 0)
        return out

    @staticmethod
    def basket_fills(results) -> dict:
        """Shares actually traded per leg by executed baskets ({exchange: qty}); an ok basket may still fill partly."""
        out: dict = {}
        for r in results or []:
            if not r.get("ok"):
                continue
            for x in (r.get("resp") or {}).get("results", []):
                d = x.get("data") or {}
                if d.get("exchangeId") is not None:
                    out[str(d["exchangeId"])] = out.get(str(d["exchangeId"]), 0.0) + float(d.get("quantityTraded") or 0)
        return out

    def load_raise_pause(self):
        """Legs the cash raise is selling: the MM stays off them until the pause ends, across a restart (store param)."""
        try:
            self.mm.raise_pause = {str(e): float(t) for e, t in (self.store.get_param("raise_pause", {}) or {}).items() if float(t) > time.time()}
        except Exception as e:
            self.mm.raise_pause = {}
            log.warning("raise pause unreadable: %s", e)

    def post_cash_need(self, sleeve: str, amount: float, why: str = ""):
        """A sleeve with more edge than the cheapest holdings is blocked by cash right now (cleared every cycle): the cash
        raise may sell up to this much for it, never more."""
        if amount > 0.5:
            needs = getattr(self, "cash_needs", None)
            if needs is None:
                needs = self.cash_needs = {}
            needs[sleeve] = (float(amount), why)

    def note_churn_skip(self, race: str, wanted: int, allowed: int, cost: float):
        """High-frequency arb skipped (or cut) a qualifying entry for cash: logged for the brief's unmet-demand line and
        counted for tier 2 of the cash raise. One record per race per 5 min (the sleeve retries every cycle)."""
        now = time.time()
        skips = [s for s in (self.store.get_param("churn_cash_skips", []) or []) if float(s[0]) > now - 6 * 3600]
        if any(s[1] == race and now - float(s[0]) < 300 for s in skips):
            return
        skips.append([now, race, int(wanted), int(allowed), float(cost)])
        self.store.set_param("churn_cash_skips", skips)
        log.info("CHURN %s for cash: %s wanted x%d at %.4f/set, cash allowed x%d (%.0f unmet); %d in the last 6 h",
                 "skipped" if allowed == 0 else "cut", race, wanted, cost, allowed, (wanted - allowed) * cost,
                 sum(1 for s in skips if float(s[0]) > now - 6 * 3600))

    def churn_skips_6h(self) -> int:
        """Entries high-frequency arb skipped (or cut) for cash in the last 6 h (one per race per 5 min): for the brief."""
        now = time.time()
        return sum(1 for s in (self.store.get_param("churn_cash_skips", []) or []) if float(s[0]) > now - 6 * 3600)

    def churn_skip_windows_6h(self, window: float = 1800.0) -> int:
        """Cash skips in the last 6 h counted at least `window` apart: the tier-2 gate. A burst of races blocked in one
        cycle, or one opportunity retried for 10 min, counts once (review 24 Oct: 3 races in one cycle met a count of 3),
        and 3 needs demand spread over >= 2 windows (an hour), not a fixed grid's edges (3 in 35 min, review 2)."""
        now = time.time()
        n, last = 0, None
        for t in sorted(float(s[0]) for s in (self.store.get_param("churn_cash_skips", []) or []) if float(s[0]) > now - 6 * 3600):
            if last is None or t - last >= window:
                n, last = n + 1, t
        return n

    def raise_cycle(self, prices: dict, killed: bool):
        """Cash raise (proposals/cash_raise.py): when a higher-edge sleeve posted a cash need this cycle, raise the shortfall
        plus raise_buffer (at most raise_max_total a day). Tier 1: holdings beyond the arb sets that cost < raise_max_cost
        to exit, cheapest first, at the touch; the MM stops quoting a leg being sold; net D stays inside the cap. Tier 2
        (raise_sets_enabled): whole sets, all-or-none, only when tier 1 cannot cover it, high-frequency arb skipped
        >= raise_set_min_skips entries for cash in 6 h and the exit costs <= raise_set_max_cost."""
        if strategy_paused(self, "exit:raise"):
            return                                   # a raise clip closed more than the account held (strategy stops)
        cfg = self.cfg
        reserve = shock_reserve(self)
        shock_on = bool(getattr(self, "shock_active", lambda: False)())   # a shock holds or is entering positions
        if reserve > 0 and spendable_cash(self) < -0.5 and not shock_on:
            # (review 6: refilled during a shock, the raise sold into the very move shock mode trades, and the shock's own
            # exit brought the cash back an hour later: selling just to hold cash; the reserve refills once it closes)
            # the one need without a waiting trade: keep free cash >= the shock reserve (refills after a shock spends it); on
            # the board like the others, so the logs and the brief see it
            Bot.post_cash_need(self, "shock_reserve", -spendable_cash(self), f"free cash {self.cash:.0f} under the {reserve:.0f} shock reserve")
        needs = dict(getattr(self, "cash_needs", None) or {})
        if reserve > 0 and spendable_cash(self) < 0:
            # free cash is under the reserve: whatever is raised lands under it first, so the sleeves asking could not spend
            # it; only the reserve's own refill counts (none during a shock: review 7, sales into the shock move otherwise)
            needs = {k: v for k, v in needs.items() if k == "shock_reserve"}
        if not getattr(cfg, "raise_enabled", False) or self.dry_run or self.store is None or killed or not needs:
            return
        if getattr(self, "open_orders_ok", True) is False:
            # an MM bid we cannot see could fill between the re-read and a clip (a leg through zero): no raise this cycle
            log.warning("RAISE: skipped this cycle: the open-orders read failed")
            return
        now = time.time()
        need = sum(a for a, _ in needs.values())
        why = ", ".join(f"{k} {a:.0f} ({w})" for k, (a, w) in sorted(needs.items()))
        hist = [(float(t), float(x)) for t, x in (self.store.get_param("raise_log", []) or []) if float(t) > now - 86400]
        left = cfg.raise_max_total - sum(x for _, x in hist)
        target = min(need + cfg.raise_buffer, left)
        if target <= 0.5:
            log.info("RAISE: %s short %.0f, but %.0f already raised in 24 h (cap %.0f)", sorted(needs), need, cfg.raise_max_total - left, cfg.raise_max_total)
            return
        race_legs = {r.name: list(r.exchange_ids) for r in self.universe.races.values()}
        prot = set(self.arb.protected())
        busy = set(getattr(self, "catchup_open", None) or {}) | set(getattr(self, "cheapfav_open", None) or {})
        busy |= {str(r["exchange_id"]) for r in self.store.rows(          # other sleeves' orders: leave their legs alone
            "SELECT DISTINCT exchange_id FROM orders WHERE placed_at > ? AND purpose NOT IN ('mm', 'raise', 'raise_set', 'arb')", (now - 600,))}
        busy |= {str(r["exchange_id"]) for r in self.store.rows(          # shock positions are shock mode's to close
            "SELECT DISTINCT exchange_id FROM orders WHERE placed_at > ? AND purpose IN ('shock', 'shock_exit')", (now - 86400,))}
        for rn in (getattr(self, "shock_races", lambda: set())() or ()):   # incl. entries of unknown outcome (no orders row)
            r_ = self.universe.races.get(rn)
            busy |= set(r_.exchange_ids) if r_ is not None else set()
        # whole races: a catch-up / cheap-favourite position netted into a set leg shows on the race's OTHER leg (review 24 Oct:
        # catch-up's 500 YES on D next to 1,000 sets read as 500 NO on R, which tier 1 then sold)
        busy_races = {self.universe.race_of(e).name for e in busy if self.universe.race_of(e) is not None}
        excl_races = set(cfg.raise_exclude_races or []) | prot | busy_races
        # the wind-down owns every race it does not exempt from 31 Oct 00:00 ET to 18:00 ET 3 Nov, and a race it clipped less
        # than 30 min ago waits (shared exit lock)
        excl_races |= Bot.wind_down_owned(self, now) | {r for r in race_legs if Bot.exit_locked(self, r, "raise", now=now)}
        sent = 0.0
        with self.trade_lock:
            # shock mode trades in its own thread under this lock: a race it entered since the raise planned is excluded,
            # and a reserve refill waits while a shock is open (review 14: the plan above was read before the lock)
            sk_now = shock_races_of(self)
            busy_races |= sk_now
            excl_races |= sk_now
            pr_now = punt_races_of(self)             # punt / election races: never the raise's to sell
            busy_races |= pr_now
            excl_races |= pr_now
            if set(needs) == {"shock_reserve"} and bool(getattr(self, "shock_active", lambda: False)()):
                return
            # a clip of ours that may still rest anywhere: pulled before positions are read (review 3: a rested clip filling
            # later is in no cap check, net D -1 -> +1199 past a 900 cap)
            for r in self.store.rows("SELECT DISTINCT exchange_id FROM orders WHERE purpose='raise' AND status='open' AND placed_at > ?", (now - 120,)):
                ids = [x["order_id"] for x in self.store.rows("SELECT order_id FROM orders WHERE exchange_id=? AND purpose='raise' AND status='open' "
                                                              "AND placed_at > ?", (r["exchange_id"], now - 120))]
                try:
                    self.api.cancel_all(exchange_id=r["exchange_id"])
                    self.store.mark_orders(ids, "cancelled")
                except ApiError as e:
                    log.warning("RAISE: could not pull an earlier clip on %s (%s): no raise this cycle", r["exchange_id"], e)
                    return
            pos = self.fresh_positions()               # sized from the venue now, under the lock (never through zero)
            if pos is None:
                return
            elig = CR.eligible(race_legs, pos, excl_races, busy, cfg.raise_hold_limit)
            own = own_levels(self)
            books = {}

            def book(ex):
                if ex not in books:
                    bk = self.fresh_book(ex, 10.0)
                    books[ex] = own.net(ex, bk) if bk else None   # our own resting quotes are not liquidity we can sell into
                return books[ex]
            for ex in elig:
                book(ex)
            net_d = lambda p: risk.exposure(self.universe, p, prices).net_d_notional
            clips = CR.plan(elig, books, target, cfg.raise_max_cost, net_d, cfg.mm.portfolio_max_partisan_notional, pos,
                            cfg.raise_cap_margin, cfg.raise_max_clips, getattr(cfg, "raise_min_clip", 0.0))
            # tier 2 only when tier 1 cannot cover the shortfall over the coming cycles, and high-frequency arb's unmet demand
            # has repeated (not a one-off)
            pieces, t1 = [], CR.tier1_available(elig, books, cfg.raise_max_cost)
            skips = self.churn_skip_windows_6h(getattr(cfg, "raise_set_skip_window", 1800.0)) if getattr(cfg, "raise_sets_enabled", False) else 0
            # (never for the shock reserve: tier 2 sizes from the other sleeves' needs only)
            t2_target = min(sum(a for k, (a, _) in needs.items() if k != "shock_reserve") + cfg.raise_buffer, target) \
                if any(k != "shock_reserve" for k in needs) and spendable_cash(self) >= 0 else 0.0
            # (review 6: with the reserve short, tier 2's cash refilled the reserve first, so the other need never cleared and
            # sets were sold at up to 4.5% for the reserve; tier 2 waits until tier 1 has refilled it)
            if getattr(cfg, "raise_sets_enabled", False) and t2_target > 0 and t1 < t2_target and skips >= cfg.raise_set_min_skips:
                s_excl = prot | set(cfg.raise_set_hold_races or []) | busy_races
                # the wind-down's races (31 Oct - 3 Nov) and races it clipped < 30 min ago: tier 2 too (merge review M1)
                s_excl |= Bot.wind_down_owned(self, now) | {r for r in race_legs if Bot.exit_locked(self, r, "raise", now=now)}
                for r, exs in race_legs.items():
                    if r not in s_excl and len(exs) >= 2:
                        qs = [pos.get(e, 0.0) for e in exs]
                        if all(q < 0 for q in qs) or all(q > 0 for q in qs):
                            for e in exs:
                                book(e)
                # only the part tier 1 can never cover: its cheaper holdings are used first, over the coming cycles
                pieces = CR.set_pieces(race_legs, pos, books, t2_target - t1, cfg.raise_set_max_cost,
                                       s_excl, cfg.raise_set_max_pieces, getattr(cfg, "raise_min_clip", 0.0))
                if not pieces:
                    log.info("RAISE tier 2: %.0f still short after tier 1 (%.0f available); no set exits at <= %.1f%%", target - t1, t1,
                             100 * cfg.raise_set_max_cost)
            if not clips and not pieces:
                log.info("RAISE: %s short %.0f; no clip under %.1f%% of mid (%d eligible legs)%s", sorted(needs), need,
                         100 * cfg.raise_max_cost, len(elig), "" if not getattr(cfg, "raise_sets_enabled", False)
                         else f"; tier 2: cash skips in {skips} half-hours of the last 6 h (need {cfg.raise_set_min_skips}), tier 1 has {t1:.0f}")
                return
            pause = {e: t for e, t in (getattr(self.mm, "raise_pause", None) or {}).items() if t > now}
            for c in clips:                            # every leg of the race: the other leg's MM trades the same exposure
                for e in race_legs.get(self.universe.race_of(c["exchange_id"]).name, [c["exchange_id"]]):
                    pause[e] = now + cfg.raise_pause_sec
            for p in pieces:
                for e in race_legs[p["race"]]:
                    pause[e] = now + cfg.raise_pause_sec
            self.mm.raise_pause = pause                # before the clip: the MM must not buy the leg back
            self.store.set_param("raise_pause", pause)

            def pull(ex):
                # our resting orders on the leg (MM quotes, an earlier clip's remainder): pulled first, so nothing of ours
                # can fill there between the positions re-read below and the clip (no self-match, no buy-back)
                ids = [oid for oid, o in own.orders.items() if o[0] == ex]
                ids += [r["order_id"] for r in self.store.rows(   # an earlier clip, or an MM quote, the cache may not show
                    "SELECT order_id FROM orders WHERE exchange_id=? AND status='open' AND purpose IN ('raise', 'mm') AND placed_at > ?",
                    (ex, now - max(120.0, float(getattr(cfg.mm, "order_ttl_sec", 1800)))))]
                if ids:
                    self.api.cancel_all(exchange_id=ex)
                    self.store.mark_orders(ids, "cancelled")
            for c in clips:
                ex = c["exchange_id"]
                try:
                    for e in race_legs.get(self.universe.race_of(ex).name, [ex]):
                        pull(e)
                    # re-read after the pull, right before the send, and clip to what the leg holds now beyond any set, same
                    # sign (review 24 Oct: an MM bid filling between the cycle's read and the clip took a leg -800 -> +300)
                    now_pos = self.fresh_positions()
                    if now_pos is None:
                        log.warning("RAISE: positions re-read failed; remaining clips skipped")
                        break
                    q_now = CR.eligible(race_legs, now_pos, excl_races, busy, cfg.raise_hold_limit).get(ex, 0)
                    q = min(int(c["qty"]), abs(q_now)) if q_now * c["sign"] > 0 else 0
                    if q < 1 or q * c["pps"] < getattr(cfg, "raise_min_clip", 0.0):
                        log.info("RAISE clip %s skipped: %s now holds %+d beyond its sets (clip of %d)", ex, ex, q_now, q)
                        continue
                    # the plan assumed every earlier clip filled; re-check the cap on the positions now (review 24 Oct: an
                    # earlier clip that errored or rested let a later one take net D from +840 to +990 past a 900 cap)
                    d_now = net_d(now_pos)
                    p2 = dict(now_pos)
                    p2[ex] = p2.get(ex, 0.0) - c["sign"] * q
                    d2 = net_d(p2)
                    cap = cfg.mm.portfolio_max_partisan_notional
                    if abs(d2) > cap + 1e-6 and abs(d2) >= abs(d_now) - 1e-6:
                        log.info("RAISE clip %s skipped: net D %+.0f -> %+.0f would breach the %.0f cap", ex, d_now, d2, cap)
                        continue
                    c = dict(c, qty=q, proceeds=q * c["pps"], net_d_after=d2)
                    try:
                        resp = self.api.place_order(ex, c["side"], "buy", q, float(c["price"]), expiration=expiry_in(60), key=self.api.new_key("raise"))
                    except OSError as e:                     # URLError / timeout / connection reset after the client's retries:
                        raise ApiError(0, "NETWORK", str(e))  # placement status unknown (review 5)
                    self.store.record_order(resp.get("orderId"), ex, None, c["side"], "buy", float(c["price"]), q, "raise", response=resp)
                    Bot.stamp_exit(self, self.universe.race_of(ex).name, "raise", now)
                    traded = resp.get("quantityTraded", resp.get("filledQuantity"))
                    sent += (min(q, float(traded)) if traded is not None else q) * c["pps"]   # cash: what traded, if known
                    hist.append((now, c["proceeds"]))                                # the 24-h cap: what was sent (safe side)
                    self.store.set_param("raise_log", hist)
                    if (traded is None or float(traded) < q - 1e-9) and resp.get("open") is not False:
                        # fill-or-cancel: a rested remainder is in no later cap check (review 3); if it cannot be cancelled,
                        # or its fill is unknown, send nothing more this cycle (a remainder the venue already closed: nothing rests)
                        oid = resp.get("orderId")
                        try:
                            if oid is None:
                                raise ApiError(0, "NO_ORDER_ID", "no orderId in the response")
                            self.api.cancel_order(oid)
                            self.store.mark_orders([oid], "cancelled")
                            log.info("RAISE clip %s: %s of %d traded; the rest cancelled", ex, "?" if traded is None else int(float(traded)), q)
                        except ApiError as e:
                            log.warning("RAISE clip %s: remainder not cancelled (%s): no more clips this cycle", ex, e)
                            break
                        if traded is None:
                            break
                        hist[-1] = (now, float(traded) * c["pps"])   # cancelled: the 24-h cap is charged what traded (review 4)
                    self.store.set_param("raise_log", hist)        # at once: an exception later must not drop it from the cap
                    log.info("RAISE clip %s: sell %s x%d @%.3f (buy %s), proceeds %.0f, cost %.2f (%.2f%% vs mid %.4f), net D after %+.0f; for %s",
                             ex, c["holding"], c["qty"], c["pps"], c["side"], c["proceeds"], c["cost"], 100 * c["cost_frac"], c["mid"],
                             c["net_d_after"], why)
                except ApiError as e:
                    if e.status is None or e.status >= 500 or e.status == 0 or e.code == "REQUEST_IN_FLIGHT":
                        # the order may have been placed (status unknown) and is not in the store, so no later pull finds it:
                        # pull the leg and stop (review 4)
                        log.warning("raise clip %s: %s (status unknown): leg pulled, no more clips this cycle", ex, e)
                        hist.append((now, c["proceeds"]))   # it may have filled: charged to the 24-h cap (safe side)
                        self.store.set_param("raise_log", hist)
                        try:
                            self.api.cancel_all(exchange_id=ex)
                        except ApiError as e2:
                            log.warning("raise clip %s: pull after an unknown placement failed: %s", ex, e2)
                        break
                    log.warning("raise clip %s: %s", ex, e)
            for p in pieces:
                if strategy_paused(self, "exit:unwinds"):
                    break                                # an unwind took a leg through zero (strategy stops)
                try:
                    for e in race_legs[p["race"]]:
                        pull(e)
                    bk = {e: books[e] for e in race_legs[p["race"]]}
                    Bot.stamp_exit(self, p["race"], "raise", now)
                    if p["kind"] == "sell_set":
                        res = self.arb.unwind_set(p["race"], bk, p["qty"], why="raise tier 2", min_qty=1, refresh=self.fresh_positions, purpose="raise_set")
                    else:
                        res = self.arb.unwind_buy_set(p["race"], bk, p["qty"], why="raise tier 2", refresh=self.fresh_positions, purpose="raise_set")
                    if res and not res.get("dry"):
                        got = res["qty"] * res["proceeds"]
                        sent += got
                        hist.append((now, got))
                        self.store.set_param("raise_log", hist)
                        log.info("RAISE set %s (%s): unwound %d of %d sets at %.4f/set (payout %.0f), proceeds %.0f, cost %.2f%%; %d cash "
                                 "skip half-hours by high-frequency arb in 6 h; for %s", p["race"], p["kind"], res["qty"], p["held"], res["proceeds"],
                                 p["payout"], got, 100 * (p["payout"] - res["proceeds"]) / res["proceeds"], skips, why)
                except ApiError as e:
                    log.warning("raise set %s: %s", p["race"], e)
            self.store.set_param("raise_log", hist)
            self.refresh_snapshot()
        if sent:
            self.cash += sent
            log.info("RAISE: %.0f raised for a %.0f shortfall (+%.0f buffer); %.0f left of the 24-h cap", sent, need, cfg.raise_buffer, left - sent)

    def ri_trimmed(self) -> float:
        """Sets the RI trim has unwound so far: its own log, cross-checked against its 'ri_trim' orders (per leg; a set is
        one order on every leg), whichever is larger, so the cap survives a restart and a lost param."""
        logged = sum(float(x[2]) for x in (self.store.get_param("ri_trim_log", []) or []))
        rows = self.store.rows("SELECT exchange_id, SUM(quantity) q FROM orders WHERE purpose='ri_trim' AND status != 'cancelled' GROUP BY 1")
        per_leg = [float(r["q"] or 0.0) for r in rows]
        return max(logged, max(per_leg) if per_leg else 0.0)

    def ri_trim_cycle(self, prices: dict, killed: bool):
        """The RI trim (Sizhe 24 Oct: "more trades to be made with higher edge but ... locked up by lack of capital"): tier 2
        for raise_sets_races only (Rhode Island Governor). While free cash is short of what cheap favourites' remaining
        budget plus the shock reserve can use, unwind whole sets (every leg, all-or-none) in clips of <= raise_ri_trim_clip,
        at most one per raise_ri_trim_gap_sec, only when the clip's walked exit cost is at or under the race's trailing
        24-h median (level 0, research_notes/ladder_exits.md) and its proceeds cover the sets' cost basis (no realised
        loss), net D inside the cap, until raise_ri_trim_sets have been unwound or the need is met."""
        if strategy_paused(self, "exit:raise") or strategy_paused(self, "exit:unwinds"):
            return                                   # its set unwinds stop with the raise / unwind exit paths
        cfg = self.cfg
        races = list(getattr(cfg, "raise_sets_races", None) or [])
        cf = getattr(self, "cheapfav", None)
        if (not getattr(cfg, "raise_enabled", False) or not races or cf is None or self.dry_run or self.store is None or killed
                or getattr(self, "open_orders_ok", True) is False or bool(getattr(self, "shock_active", lambda: False)())):
            return
        now = time.time()
        if strategy_paused(self, "cheapfav") or now > float(getattr(cf, "last_entry", float("inf"))):
            return                                     # cheap favourites cannot spend the cash: nothing to trim for (review 9)
        if now - float(getattr(self, "ri_trim_fail_ts", 0.0)) < 600.0:
            return                                     # a basket was just rejected: no pause / pull / retry every cycle
        try:
            deployed = float(self.cheapfav_refresh()[4])
        except Exception as e:
            log.warning("RI trim: cheap-favourites ledger unreadable (%s): no trim", e)
            return
        need = max(0.0, float(cf.budget) - deployed) + shock_reserve(self) - float(self.cash)
        if need <= 0.5:
            return
        cap = float(getattr(cfg, "raise_ri_trim_sets", 0.0) or 0.0)
        trimmed = self.ri_trimmed()
        tlog = [list(x) for x in (self.store.get_param("ri_trim_log", []) or [])]
        if trimmed >= cap - 0.5:
            return
        if tlog and now - max(float(x[0]) for x in tlog) < float(getattr(cfg, "raise_ri_trim_gap_sec", 1800.0)):
            return
        hist = [(float(t), float(x)) for t, x in (self.store.get_param("raise_log", []) or []) if float(t) > now - 86400]
        left = cfg.raise_max_total - sum(x for _, x in hist)
        if left <= 0.5:
            return
        need = min(need, left)                         # the 24-h raise cap binds the trim too (review 9: one clip over it)
        for rname in races:
            race = self.universe.races.get(rname)
            if race is None or len(race.exchange_ids) < 2 or rname in self.arb.protected():
                continue
            if rname in Bot.wind_down_owned(self, now) or Bot.exit_locked(self, rname, "raise", now=now):
                continue                                   # the wind-down sells it (31 Oct - 3 Nov) or clipped it < 30 min ago
            exs = list(race.exchange_ids)
            payout = float(len(exs) - 1)
            rows = self.store.rows("SELECT race, party, ts, ask FROM venue_quotes WHERE venue='sig' AND race=? AND ts > ? AND ask IS NOT NULL",
                                   (rname, now - 86400 - 3600))
            med = CR.exit_cost_median([(r["race"], r["party"], r["ts"], r["ask"]) for r in rows], rname, len(exs), now)
            if med is None:
                log.info("RI trim %s: less than 12 h of quotes for the level-0 median: no clip", rname)
                continue
            with self.trade_lock:
                pos = self.fresh_positions()
                if pos is None:
                    return
                held = self.arb.held_sets(race, pos)
                own = own_levels(self)
                books = {e: (own.net(e, self.fresh_book(e, 10.0)) if self.fresh_book(e, 10.0) else None) for e in exs}
                if held < 1 or any(b is None or not b.get("asks") for b in books.values()):
                    continue
                touch_pps = len(exs) - sum(b["asks"][0]["price"] for b in books.values())
                q = CR.trim_qty(held, need, touch_pps, trimmed, cap, int(getattr(cfg, "raise_ri_trim_clip", 1000)))
                uq = self.arb.unwind_quote(race, books, q) if q >= 1 else None
                if not uq:
                    continue
                cost_now = payout - uq["proceeds"]                     # per set, walked
                basis = sum(float(self.cost_basis.get(e, 0.0)) / abs(pos[e]) for e in exs if pos.get(e))
                d0 = risk.exposure(self.universe, pos, prices).net_d_notional
                p2 = {**pos, **{e: pos.get(e, 0.0) + q for e in exs}}
                d2 = risk.exposure(self.universe, p2, prices).net_d_notional
                capd = cfg.mm.portfolio_max_partisan_notional
                if cost_now > med + 1e-9:
                    log.info("RI trim %s: exit cost %.4f/set above the 24-h median %.4f (level 0): waiting", rname, cost_now, med)
                    continue
                if uq["proceeds"] < basis - 1e-9:
                    log.info("RI trim %s: proceeds %.4f/set under the cost basis %.4f: no realised loss, waiting", rname, uq["proceeds"], basis)
                    continue
                if abs(d2) > capd + 1e-6 and abs(d2) >= abs(d0) - 1e-6:
                    log.info("RI trim %s: net D %+.0f -> %+.0f would breach the cap: waiting", rname, d0, d2)
                    continue
                pause = {e: t for e, t in (getattr(self.mm, "raise_pause", None) or {}).items() if t > now}
                for e in exs:
                    pause[e] = now + cfg.raise_pause_sec
                self.mm.raise_pause = pause
                self.store.set_param("raise_pause", pause)
                for e in exs:                                          # our own quotes on the legs: pulled first
                    ids = [oid for oid, o in own.orders.items() if o[0] == e]
                    if ids:
                        try:
                            self.api.cancel_all(exchange_id=e)
                            self.store.mark_orders(ids, "cancelled")
                        except ApiError as e2:
                            log.warning("RI trim %s: could not pull our orders on %s (%s): no clip", rname, e, e2)
                            return
                t_send = time.time()
                Bot.stamp_exit(self, rname, "raise", now)
                try:
                    res = self.arb.unwind_set(rname, books, q, why="RI trim", min_qty=1, refresh=self.fresh_positions, purpose="ri_trim")
                except OSError as e:                   # network error after the client's retries: outcome unknown
                    log.warning("RI trim %s: basket outcome unknown (%s)", rname, e)
                    res = None
                sent = float(getattr(self.arb, "last_unwind_attempt", 0.0) or 0.0) >= t_send
                if res and res.get("dry"):
                    return
                if not res:
                    # rejected, or outcome unknown (502 after retries / network error: no order rows): charge whatever the
                    # venue shows gone, so neither the 4,500 cap nor the 30-min gap can be skipped (review 9)
                    self.ri_trim_fail_ts = now
                    if not sent:
                        return                         # never sent (its pre-send re-read failed): nothing to charge (review 10)
                    try:
                        after = self.fresh_positions()
                    except OSError:                    # the network is likely still down: assume it filled (review 10)
                        after = None
                    gone = q if after is None else max(0, held - self.arb.held_sets(race, after))
                    if gone > 0:
                        tlog.append([now, rname, int(gone), float(uq["proceeds"])])
                        self.store.set_param("ri_trim_log", tlog)
                        hist.append((now, gone * float(uq["proceeds"])))   # and the 24-h raise cap (review 10)
                        self.store.set_param("raise_log", hist)
                        log.warning("RI trim %s: basket not confirmed but %d sets are gone: charged to the cap", rname, gone)
                    return
                got = res["qty"] * res["proceeds"]
                tlog.append([now, rname, int(res["qty"]), float(res["proceeds"])])
                self.store.set_param("ri_trim_log", tlog)
                hist.append((now, got))
                self.store.set_param("raise_log", hist)
                self.refresh_snapshot(self.unwind_deltas([res]))
            self.cash += got
            log.info("RI trim %s: unwound %d of %d sets at %.4f/set (exit cost %.4f vs 24-h median %.4f, basis %.4f), proceeds %.0f; "
                     "%d of %.0f trimmed; cash short %.0f of cheap favourites' budget + shock reserve", rname, res["qty"], held,
                     res["proceeds"], cost_now, med, basis, got, trimmed + res["qty"], cap, need)
            return                                                     # one clip per cycle

    def churn_no_buy(self, rname: str) -> bool:
        """Races recycling may not buy into while high-frequency arb runs: those it holds sets in, its exclusions (Rhode
        Island Governor) and races where we already hold more than churn_skip_holding shares on a leg (Fable 24 Oct: the
        recycler bought Rhode Island Governor with cash from unwound churn sets)."""
        ch = self.churn
        if ch is None:
            return False
        if rname in CHURN_EXCLUDE or ch.held(rname) > 0 or (rname in ch.races and sell_all_due(time.time())):
            return True                    # (from the sell-all on, no race it traded is bought back into)
        race = self.universe.races.get(rname)
        lim = getattr(self.cfg, "churn_skip_holding", 0.0)
        return bool(race is not None and lim > 0 and big_holding(self.positions_snapshot or {}, race.exchange_ids, ch.held(rname), lim))

    def open_orders_map(self) -> dict[str, list]:
        out: dict[str, list] = {}
        try:
            for o in self.api.open_orders():
                out.setdefault(str(o["exchangeId"]), []).append(o)
            self.open_orders_ok = True
        except ApiError as e:
            log.warning("open orders: %s", e)
            self.open_orders_ok = False          # the cash raise must not trust an empty cache (review 24 Oct)
        return out

    def ingest_fills(self) -> dict[str, int]:
        """Pull fills newest-first until we hit ones we already have. Returns per-exchange total fill counts."""
        new = 0; cursor = None; pages = 0
        self.fills_read_ok = True
        while pages < 5:
            try:
                page = self.api.fills(self.slug, 200, cursor)
            except ApiError as e:
                log.warning("fills: %s", e)
                self.fills_read_ok = False               # the strategy stops do not confirm a crossing this cycle
                break
            data = page.get("data", [])
            n = self.store.ingest_fills(data)
            new += n; pages += 1
            if n < len(data) or not page["pagination"].get("hasMore"):
                break
            cursor = page["pagination"]["nextCursor"]
        if new:
            log.info("ingested %d new fills", new)
        counts = {r["exchange_id"]: r["n"] for r in self.store.rows("SELECT exchange_id, COUNT(*) AS n FROM fills WHERE ABS(quantity) > 10 GROUP BY exchange_id")}
        self.fills_count_cache = counts
        return counts

    def poll_tapes(self, exchanges: list[str]):
        if not exchanges:
            return
        k = self.cfg.tape_poll_markets
        batch = [exchanges[(self.tape_cursor + i) % len(exchanges)] for i in range(min(k, len(exchanges)))]
        self.tape_cursor = (self.tape_cursor + k) % max(1, len(exchanges))
        for ex in batch:
            since = self.store.latest_tape_time(ex)
            try:
                t = self.api.trades(ex, 200, since=since)
            except ApiError as e:
                log.warning("tape %s: %s", ex, e)
                continue
            self.store.ingest_tape(ex, t.get("data", []))

    def pressures(self) -> dict[str, float]:
        ov = self.mm.overrides.get("exchanges", {}) if self.mm.overrides else {}
        return {ex: float(v.get("pressure", 0.0)) for ex, v in ov.items()}

    # --------------------------------------------------------------- exploit sleeves (proposals wired 2026-10-22)
    def lag_cycle(self, positions: dict, blocked: set) -> int:
        """Entries at the SIG touch in the direction of a fresh outside move, exits at half convergence or after an hour;
        size = min(lag_qty, DMM touch size, the directional per-leg notional cap); partisan-blocked sides skipped."""
        rows = getattr(self.direction, "rows", None) or []
        if not rows:
            return 0
        touch = {}
        for r in rows:
            ex = r.get("exchange_id")
            fb = self.fresh_book(ex) if ex else None
            if fb and fb.get("bids") and fb.get("asks"):
                touch[ex] = min(fb["bids"][0]["quantity"], fb["asks"][0]["quantity"])
        orders = self.lag.cycle(time.time(), rows, touch)
        cap = float(getattr(self.direction.cfg, "per_market_cap", 300.0))
        done = 0
        for o in orders:
            ex, side, px = str(o["exchangeId"]), o["side"], float(o["price"])
            q = int(o["quantity"])
            if o.get("reason") != "exit":
                q = min(q, int(cap / max(px, 0.005)))
                if risk.adds(self.universe, ex, side) in blocked or q < 10:
                    log.info("LAG skip %s %s q=%d (blocked or below cap)", ex, side, q)
                    self.lag.open.pop(ex, None)
                    continue
            log.info("LAG %s %s %s x%d at %.3f: %s", "exit" if o.get("reason") == "exit" else "enter", ex, side, q, px, o.get("reason"))
            if self.dry_run:
                done += 1
                continue
            try:
                resp = self.api.place_order(ex, side, "buy", q, px, expiration=expiry_in(60), key=self.api.new_key("lag"))
                self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, q, "exit" if o.get("reason") == "exit" else "direction", response=resp)
                done += 1
            except ApiError as e:
                log.warning("lag order %s failed: %s", ex, e)
                self.lag.open.pop(ex, None)
        return done

    def inside_cycle(self, positions: dict, prices: dict, blocked: set, cycle_id: int) -> dict:
        """Rest one tick inside the DMM on markets with a 3+-tick DMM spread that the MM does not quote. Re-posts only
        when the DMM touch moved or our post is near its TTL; each re-posted market costs one cancel plus a share of
        one batch write."""
        now = time.time()
        for ex, t in list(self.inside_posted.items()):     # let the quoter re-post before the venue expires our orders
            if now - t > self.cfg.inside_ttl_sec - 45:
                self.inside.last.pop(ex, None)
        books = {ex: b for ex, b in self.feed.books().items() if ex not in self.mm.quoted}
        orders = self.inside.cycle(books, positions, exclude=set(self.mm.quoted) | Bot.punt_legs(self), blocked=blocked, prices=prices)
        by_ex: dict = {}
        for o in orders:
            by_ex.setdefault(str(o["exchangeId"]), []).append(o)
        chosen = list(by_ex.items())[: self.cfg.inside_max_markets]
        summary = {"markets": len(chosen), "orders": 0, "writes": 0}
        if not chosen:
            return summary
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=self.cfg.inside_ttl_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        batch = []
        for ex, os_ in chosen:
            if ex in self.inside_posted and not self.dry_run:
                try:
                    self.api.cancel_all(exchange_id=ex)
                    summary["writes"] += 1
                except ApiError as e:
                    log.warning("inside cancel-all %s: %s", ex, e)
                    continue
            self.inside_posted[ex] = now
            for o in os_:
                batch.append(dict(exchangeId=ex, side=o["side"], action="buy", quantity=int(o["quantity"]), price=float(o["price"]),
                                  expirationDate=ttl, tournamentId=self.api.tournament_id, _yes=o.get("yes_price"), _why=o.get("reason")))
        if self.dry_run:
            for b in batch:
                log.info("[DRY] inside %s %s x%d at %.3f (%s)", b["exchangeId"], b["side"], b["quantity"], b["price"], b["_why"])
            summary["orders"] = len(batch)
            return summary
        for i in range(0, len(batch), 15):
            chunk = batch[i:i + 15]
            try:
                resp = self.api.place_batch([{k: v for k, v in b.items() if not k.startswith("_")} for b in chunk])
            except ApiError as e:
                log.warning("inside batch failed: %s", e)
                continue
            summary["writes"] += 1
            for r in resp.get("results", []):
                b = chunk[r["index"]]
                d = r.get("data") or {}
                if r.get("ok") and d.get("orderId"):
                    self.store.record_order(d["orderId"], b["exchangeId"], None, b["side"], "buy", b["price"], b["quantity"], "inside",
                                            cycle_id=cycle_id, response=d)
                    summary["orders"] += 1
        if summary["orders"]:
            log.info("INSIDE posted %d orders on %d markets (%d writes)", summary["orders"], summary["markets"], summary["writes"])
        return summary

    def stops_cycle(self, cash: Optional[float], positions: dict, mv: dict, prices: dict, open_orders: dict, hl: dict,
                    settled: Optional[set] = None):
        """Per-strategy stops (strategy_stops.py), every cycle; never raises (a failure keeps the last pauses in force)."""
        try:
            self._stops_cycle(cash, positions, mv, prices, open_orders, hl, settled)
            if getattr(self, "stops_fallback", False):
                log.warning("STRATEGY STOPS: working again: the bot-wide kill switch is off again")
            self.stops_failures, self.stops_fallback = 0, False
        except Exception as e:                       # the stops must never kill the loop (or skip this cycle's exits)
            log.exception("strategy stops failed: %s", e)
            self.stops_failures = getattr(self, "stops_failures", 0) + 1
            if self.stops_failures >= 3 and not getattr(self, "stops_fallback", False):
                self.stops_fallback = True           # never fail open: today's switch takes over until the stops work
                log.error("STRATEGY STOPS: failed %d cycles in a row: the bot-wide kill switch is back on", self.stops_failures)

    def _stops_cycle(self, cash, positions, mv, prices, open_orders, hl, settled):
        st = self.stops
        now = time.time()
        touch = {}
        for ex, p in (prices or {}).items():
            touch[str(ex)] = (p.get("bestBid"), p.get("bestAsk"))
        # the feed's live books where it has them: the cycle's price snapshot can be a scanner pass old, and a jump must
        # be seen in the same cycle as the venue's marks see it (sim zz_swing: one cycle after today's switch otherwise)
        if self.feed_ok():
            for ex in list(touch):
                fb = self.fresh_book(ex, 10.0)
                if fb is not None and fb.get("bestBid") is not None and fb.get("bestAsk") is not None:
                    touch[ex] = (fb["bestBid"], fb["bestAsk"])
        ch, cs = self.churn, self.catchup
        churn = None
        if ch is not None:
            # its ledger (realised, lots) is restored from its state at start; its open value once its cycle valued it
            churn = {"realised": ch.realised, "open": getattr(ch, "last_open_value", None), "lots": list(ch.lots),
                     "legs": {e for l in ch.lots if l["race"] in self.universe.races for e in self.universe.races[l["race"]].exchange_ids}}
        ok = cash is not None                        # None: this cycle's account or positions read failed
        res = st.evaluate(now, touch=touch, cash=cash, positions=positions if ok else None, mv=mv if ok else None,
                          settled=settled, churn=churn, churn_killed=bool(ch is not None and ch.killed),
                          catchup_killed=bool(cs is not None and cs.killed),
                          shock_killed=bool(getattr(self, "shock", None) is not None and getattr(self.shock, "killed", False)),
                          fills_ok=bool(getattr(self, "fills_read_ok", True)),
                          markout=(hl.get("markout30"), hl.get("n_markout", 0)), open_orders=open_orders)
        self.mm.quarantine = dict(st.quarantine)    # a copy: the scanner thread's fast requotes read it
        # today's switch cancelled every resting order; scoped to the paused strategy's legs: our resting ENTRY orders
        # there are pulled (sim churn_widen: an MM order left at the ask made churn's all-or-none exit unfillable, so the
        # widening sets were sold 10 min and 2 ticks later than under the old switch). Only purposes in
        # strategy_stops.PULLABLE: never exits, punt, raise, hedge or election orders. 8 cancels per cycle.
        pull = []
        legs = set(open_orders or {}) if strategy_paused(self, "all") else set(st.quarantine)   # KILL-ENTRIES: every leg
        if legs and open_orders:
            ids = [str(o.get("id")) for ex in legs if ex in open_orders for o in open_orders[ex] if o.get("id") is not None]
            if ids:
                ex_purp = {str(r["order_id"]): r["purpose"] for r in self.store.rows(
                    "SELECT order_id, purpose FROM orders WHERE order_id IN (%s)" % ",".join("?" * len(ids)), tuple(int(i) for i in ids))}
                pull = [i for i in ids if ex_purp.get(i) in STOPS.PULLABLE and i not in getattr(self, "stops_pulled", set())]
        # at most 4 + 4 writes: the exits later in this cycle use the same write budget
        todo = [(oid, "stuck") for oid in res["cancel"][:4]] + [(int(i), "pull") for i in pull[:4]]
        t_c = time.time()
        for oid, why in todo:
            if self.dry_run or time.time() - t_c > 10.0:
                break                                # a slow venue must not hold up this cycle's exits
            try:
                self.api.cancel_order(oid)
                self.store.mark_orders([oid], "cancelled")
                if why == "pull":
                    self.stops_pulled = getattr(self, "stops_pulled", set()) | {str(oid)}
                    log.info("STRATEGY STOPS: pulled order %s on a paused strategy's leg", oid)
                else:
                    log.warning("STRATEGY STOPS: cancelled %s order %s", why, oid)
            except Exception as e:                   # any failure (network too): next order, the cycle goes on
                log.warning("stops cancel %s: %s", oid, e)
        if res["new"] or now - self.stops_log_ts >= 300:
            self.stops_log_ts = now
            s = res["status"]
            log.info("STOPS pnl since armed %s; unattributed %s (untagged %s)", s["pnl"], s["unattributed"], s["untagged"])

    def monitor_cycle(self):
        """Every strategy_monitor_sec: test each sleeve on its own results; new kills go to params/strategy_kill.json.
        Read-only on the store; never touches killed_until (the global kill switch) or any hard limit."""
        now = time.time()
        if (not getattr(self.cfg, "strategy_monitor_enabled", False) or self.store is None
                or now - self.monitor_ts < getattr(self.cfg, "strategy_monitor_sec", 900.0)):
            return
        self.monitor_ts = now
        res = SMON.evaluate(self.store, now, params_dir=os.path.join(HERE, "params"),
                            log_path=os.path.join(HERE, self.cfg.log_path), write=not self.dry_run)
        log.info("STRATEGY MONITOR: %s", "; ".join("%s %s n=%d mean=%s evidence=%s" % (k, v["verdict"], v["n"], v["mean"], v["stat"])
                                                   for k, v in res.items()))
        for k, v in res.items():
            if v["verdict"] == "warn":
                log.warning("STRATEGY MONITOR: %s warn: %s", k, v["reason"])
            if v.get("new_kill"):
                strategy_paused(self, k)              # logs "STRATEGY MONITOR: <name> paused: <reason>"

    def cancel_mm_quotes(self, open_orders: dict, budget: int):
        """MM paused by the monitor: pull its resting quotes (they would rest up to 30 min). Only orders recorded as purpose
        'mm'; a market with nothing else of ours resting gets one cancel-all, otherwise order by order. A non-MM order placed
        on that market since the open-orders read (hedge, exit) is not in the snapshot: then order by order too."""
        mm_ids = {str(r["order_id"]) for r in self.store.rows("SELECT order_id FROM orders WHERE purpose='mm' AND status='open'")}
        since = getattr(self, "open_orders_ts", 0.0) - 60.0   # the stamp is taken after a read that can be slow
        writes = 0
        for ex, lst in (open_orders or {}).items():
            mine = [o for o in lst if str(o.get("id")) in mm_ids]
            if not mine:
                continue
            if writes >= budget:
                break
            done = []                                # mark only what was really cancelled: the rest is retried next cycle
            try:
                newer = self.store.one("SELECT 1 FROM orders WHERE exchange_id=? AND purpose!='mm' AND placed_at>=? LIMIT 1",
                                       (str(ex), since))
                if len(mine) == len(lst) and newer is None:
                    self.api.cancel_all(exchange_id=ex)
                    writes += 1
                    done = mine
                else:
                    for o in mine:
                        if writes >= budget:
                            break
                        self.api.cancel_order(o["id"])
                        writes += 1
                        done.append(o)
            except ApiError as e:
                log.warning("monitor MM cancel %s: %s", ex, e)
            if done:
                self.store.mark_orders([o["id"] for o in done], "cancelled")
        return writes

    def churn_cycle(self, positions: dict, killed: bool, cycle_id=None):
        """Deviation churn (proposals/dev_churn.py), "high-frequency arb": exits first (+2 ticks/set, 24 h; the stop or a
        9-tick loss per set sells at the touch), reconciled against the sets actually held (sets another path closed are
        booked as an exit); from 3 Nov 22:30 UTC every two-leg set in a race it traded is sold, halving on thin asks,
        retried every cycle; then entries in two-leg races whose mid overround is >= 4 ticks above its 24-h median and
        whose sell-set clears the arb thresholds. Baskets are all-or-none via the arb engine; only what traded is booked."""
        ch = self.churn
        if ch is None or self.dry_run or self.store is None:
            return
        if getattr(self, "positions_ok", True) is False:
            log.warning("CHURN: skipped this cycle: the positions read failed (an empty account must not reconcile the ledger away)")
            return
        now = time.time()
        changed = False
        if now - self.churn_med_ts > 900:            # 24-h medians from this venue's own quotes, refreshed every 15 min
            span = VOL_RECENT + VOL_BASE
            rows = self.store.rows("SELECT race, party, ts, COALESCE(mid, (bid + ask) / 2.0) m FROM venue_quotes "
                                   "WHERE venue='sig' AND ts > ?", (now - span - 3600,))
            n_legs = {r.name: len(r.exchange_ids) for r in self.universe.races.values() if len(r.exchange_ids) == 2}
            series = overround_series([(r["race"], r["party"], r["ts"], r["m"]) for r in rows], n_legs, now - span, now)
            self.churn_med = medians_from_series(series, now)
            vr = vol_ratio(series, now)
            change = ch.update_vol(vr)
            if change:
                changed = True
                log.warning("CHURN: gap movement %.2fx normal: new entries %s (pause >= 1.5x, resume < 1.2x)", vr, change)
            log.info("CHURN medians: %d two-leg races with 12+ h of history; gap movement %s x normal%s", len(self.churn_med),
                     "?" if vr is None else "%.2f" % vr, " (entries paused)" if ch.vol_paused else "")
            self.churn_med_ts = now
        open_value = 0.0
        min_qty = self.cfg.arb.min_set_qty
        sell_all = sell_all_due(now)
        lim = getattr(self.cfg, "churn_skip_holding", 0.0)

        def books_for(race, max_age):
            bk = {e: self.fresh_book(e, max_age) for e in race.exchange_ids}
            if any(v is None for v in bk.values()):
                try:
                    bk = {e: self.get_book(e, 50) for e in race.exchange_ids}
                except ApiError as e:
                    log.warning("churn books %s: %s", race.name, e)
                    return None
            return bk

        def venue_sets(race) -> int:
            # the common NO quantity on both legs, whichever sleeve's (MM inventory, standing sets, ours)
            snap = self.positions_snapshot or {}
            qs = [snap.get(e, 0.0) for e in race.exchange_ids]
            return int(min(-q for q in qs)) if qs and all(q < 0 for q in qs) else 0

        def sets_held(race) -> int:
            # this sleeve's sets: the ledger capped by the common NO quantity (MM inventory is never counted as ours)
            return min(ch.held(race.name), venue_sets(race))

        def snap_add(deltas: dict):
            # keep the shared snapshot current after our own basket (other paths unwind from it); NO legs are negative
            snap = dict(self.positions_snapshot or {})
            for e, d in deltas.items():
                snap[e] = snap.get(e, 0.0) + d
            self.positions_snapshot = snap

        def last_arb_id(rname) -> int:
            r = self.store.one("SELECT MAX(id) m FROM arb_trades WHERE race=?", (rname,))
            return int(r["m"] or 0) if r else 0

        def quote_down(race, books, q, floor):
            # the most of q the asks can take now: all of it, else halving (thin asks), never below floor sets
            while q >= max(1, floor):
                uq = self.arb.unwind_quote(race, books, q)
                if uq:
                    return q, uq
                q //= 2
            return 0, None

        def reconcile(race, books) -> int:
            """Under the trade lock: shrink the ledger to the sets actually held. Sets another path closed (a free unwind)
            are booked as an exit at that unwind's recorded proceeds (arb_trades), else at the current exit value, and the
            30-min re-entry gap starts (Fable 24 Oct: dropped silently, no P&L for the stop, re-bought 5 min later)."""
            rname = race.name
            have = venue_sets(race)
            excess = ch.held(rname) - have
            if excess <= 0:
                return 0
            lots = [l for l in ch.lots if l["race"] == rname]
            rows = self.store.rows("SELECT id, quantity q, cost_per_set p FROM arb_trades WHERE race=? AND kind='unwind_set' AND ok=1 "
                                   "AND id>? AND ts>=?", (rname, ch.seen_unwind.get(rname, 0), min(l["ts"] for l in lots) - 1.0))
            nq = sum(r["q"] for r in rows)
            if nq > 0:
                px, src = sum(r["q"] * r["p"] for r in rows) / nq, "recorded unwind"
                ch.seen_unwind[rname] = max(r["id"] for r in rows)
            else:
                uq = self.arb.unwind_quote(race, books, excess) if books else None
                px, src = (uq["proceeds"], "current exit value") if uq else (None, "")
            if px is None:
                px, src = sum(l["qty"] * l["cost"] for l in lots) / max(1, sum(l["qty"] for l in lots)), "cost (no quote)"
            dropped = ch.reconcile(rname, have, px, now)
            log.info("CHURN %s: %d sets no longer held (closed by another path), booked as an exit at %.4f/set (%s): %+.1f, realised so far %+.1f",
                     rname, dropped, px, src, ch.last_reconcile_pnl, ch.realised)
            return dropped

        def sell(race, books, q, why, floor) -> Optional[tuple]:
            """Unwind up to q sets now: re-checked under the lock against the sets actually held, halving when the asks
            are too thin for all of it. Returns (sets sold, proceeds per set) or None."""
            with self.trade_lock:
                # clamp to what the venue holds NOW: a fill on our MM quotes since the cycle's read may have shrunk a leg's
                # NO; the common NO quantity never takes a leg through zero
                if self.fresh_positions() is None:
                    log.warning("CHURN %s: exit (%s) skipped: positions re-read failed; retried next cycle", race.name, why)
                    return None
                reconcile(race, books)
                q = min(q, venue_sets(race))
                q, uq = quote_down(race, books, q, floor)
                if not uq:
                    log.warning("CHURN %s: cannot sell (%s): the asks are too thin even for %d sets; retried next cycle", race.name, why, max(1, floor))
                    return None
                res = self.arb.unwind_set(race.name, books, q, why="churn: " + why, allow_protected=True, min_qty=floor)
                if not res or res.get("dry"):
                    return None
                snap_add({e: q for e in race.exchange_ids})
                ch.seen_unwind[race.name] = last_arb_id(race.name)     # our own exit is booked by close(), not reconcile
            return q, res["proceeds"]

        # ---- exits: value every race first (the stop sees the whole sleeve), then sell what a rule says to sell
        plans = []
        for rname in sorted({l["race"] for l in ch.lots}):
            race = self.universe.races.get(rname)
            if race is None:
                continue
            books = books_for(race, 30.0)
            with self.trade_lock:
                if reconcile(race, books):
                    changed = True
            held = ch.held(rname)
            if held <= 0 or (held < min_qty and not sell_all) or books is None:
                continue
            if rname in punt_races_of(self):
                continue                               # review S3: a set unwind there would sell the punt's shares (also at sell-all)
            uq = self.arb.unwind_quote(race, books, held) or quote_down(race, books, held, 1)[1]   # thin asks: what part fetches
            proceeds = uq["proceeds"] if uq else None
            lots = [l for l in ch.lots if l["race"] == rname]
            avg = {"cost": sum(l["qty"] * l["cost"] for l in lots) / held, "ts": min(l["ts"] for l in lots)}
            if proceeds is not None:
                open_value += held * (proceeds - avg["cost"])
            plans.append((race, books, held, avg, proceeds))
        ch.last_open_value = open_value                # the strategy stops read the sleeve's own P&L (realised + open)
        was = ch.killed
        if ch.check_kill(open_value) and not was:
            changed = True
            log.warning("CHURN: realised %+.1f + open %+.1f at or below %.0f: no more entries; every churn set is sold at the touch",
                        ch.realised, open_value, ch.kill)
        for race, books, held, avg, proceeds in plans:
            why = ch.exit_reason(avg, proceeds, now)
            if not why:
                continue
            got = sell(race, books, held, why, 1 if sell_all else min_qty)
            changed = True
            if got:
                q, px = got
                pnl = ch.close(race.name, q, px, now, why)
                log.info("CHURN exit %s x%d of %d at %.4f/set (%s): %+.1f, realised so far %+.1f", race.name, q, held, px, why, pnl, ch.realised)
        if sell_all:
            # 3 Nov 22:30 UTC: every two-leg set in a race this sleeve traded, ledger or not (the ledger's own go first,
            # above), except Rhode Island Governor and races where we hold > churn_skip_holding shares on a leg
            for rname in sorted(ch.races):
                race = self.universe.races.get(rname)
                if race is None or len(race.exchange_ids) != 2 or rname in CHURN_EXCLUDE or ch.held(rname) > 0:
                    continue
                if rname in cheapfav_races_of(self) or rname in shock_races_of(self) or rname in punt_races_of(self):
                    continue                                   # cheap-favourite NO legs (held to the call) and shock positions are not sets
                n = venue_sets(race)
                if n <= 0:
                    continue
                if lim > 0 and big_holding(self.positions_snapshot or {}, race.exchange_ids, 0, lim):
                    log.info("CHURN sell-all: %s skipped: %d sets outside the ledger in a race holding > %.0f shares on a leg", rname, n, lim)
                    continue
                books = books_for(race, 30.0)
                if books is None:
                    continue
                got = sell(race, books, n, "sell-all before the 3 Nov punt (outside the ledger)", 1)
                if got:
                    log.info("CHURN sell-all %s: %d of %d sets outside the ledger sold at %.4f/set", rname, got[0], n, got[1])
        # ---- entries
        short = 0.0                                    # cost of entries cash (its floor) blocked this cycle: a cash need
        if not killed and not ch.killed and now <= CHURN_LAST_ENTRY and not sell_all and not strategy_paused(self, "hf_arb"):
            own = own_levels(self)
            for race in self.universe.races.values():
                if not ch.eligible(race, now) or race.name in self.arb.hostile or self.arb.cooldown.get(race.name, 0) > now:
                    continue
                if race.name in cheapfav_races_of(self) or race.name in shock_races_of(self) or race.name in punt_races_of(self):
                    continue                                   # a cheap-favourite or shock race: the ledgers stay apart
                med = self.churn_med.get(race.name)
                if med is None:
                    continue
                if lim > 0 and big_holding(self.positions_snapshot or positions, race.exchange_ids, ch.held(race.name), lim):
                    continue                                   # big existing position here: leave the race alone
                fb = {e: self.fresh_book(e, 10.0) for e in race.exchange_ids}
                if any(v is None for v in fb.values()) or not ch.unusually_cheap(mid_overround(fb, race.exchange_ids), med):
                    continue
                nb = {e: own.net(e, b) for e, b in fb.items()}
                chunks = [c for c in self.arb.chunks_for_race(race, nb) if c.kind == "sell_set"
                          and c.edge >= self.cfg.arb.min_edge_sell - 1e-9 and c.ret >= max(0.0, self.cfg.arb.min_ret_sell) - 1e-9]
                if not chunks:
                    continue
                best = max(chunks, key=lambda c: c.ret)
                q = ch.entry_qty(best, spendable_cash(self), min_qty)
                q_free = ch.entry_qty(best, 1e12, min_qty)     # the same entry with unlimited cash (pot and race caps only)
                if q_free > q:
                    short += (q_free - q) * best.cost
                    if q_free - q >= min_qty and hasattr(self, "note_churn_skip") and spendable_cash(self) >= 0:
                        self.note_churn_skip(race.name, q_free, q, best.cost)   # unmet demand: the brief and the raise's tier 2
                if not q:
                    continue
                ch.last_entry[race.name] = now                 # one attempt per race per 5 min, filled or not
                changed = True
                with self.trade_lock:
                    base = ch.held(race.name)                  # ledger sets here (reconciled to the venue above)
                    res = self.arb.execute([Chunk(best.race, "sell_set", q, best.edge, best.cost, best.limits)], cycle_id)
                    traded = self.basket_fills(res)            # an ok basket can still fill partly: only what traded counts
                    got = int(min(traded.get(e, 0.0) for e in race.exchange_ids)) if traded else 0
                    new = 0
                    if traded:
                        self.refresh_snapshot({e: -t for e, t in traded.items()})
                        # the venue nets a NO buy against YES we already hold on that leg (MM inventory): book only the
                        # sets the venue now shows beyond the ledger, never more than traded
                        new = max(0, min(got, venue_sets(race) - base))
                    if new > 0:
                        if base == 0:
                            ch.seen_unwind[race.name] = last_arb_id(race.name)
                        ch.add_lot(race.name, new, best.cost, now)
                if got > 0:
                    self.cash -= got * best.cost
                if new > 0:
                    log.info("CHURN enter %s x%d%s at cost %.4f/set (edge %.4f, overround %+.3f vs 24-h median %+.3f)", race.name, new,
                             "" if new == q else f" of {q} ({got} traded: partial fill or netted against YES held on a leg)", best.cost,
                             best.edge, mid_overround(fb, race.exchange_ids), med)
        if short > 0:
            need = min(short, max(0.0, ch.capital - ch.used())) - max(0.0, spendable_cash(self) - ch.cash_floor)
            self.post_cash_need("hf_arb", need, f"entries blocked by the {ch.cash_floor:.0f} cash floor")
        if changed:
            try:
                tmp = self.churn_state_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(ch.to_json(), f)
                os.replace(tmp, self.churn_state_path)
            except OSError as e:
                log.warning("churn state: %s", e)

    # ------------------------------------------------------------------ exit lock shared by the wind-down and the cash raise
    def stamp_exit(self, race: str, who: str, now: Optional[float] = None):
        """Record a clip in `race` by `who` ('wind_down' | 'raise'): the OTHER routine leaves the race alone for 30 min."""
        lk = getattr(self, "exit_lock", None)
        if lk is None:
            lk = self.exit_lock = {}
        lk.setdefault(race, {})[who] = time.time() if now is None else now

    def exit_locked(self, race: str, who: str, gap: float = 1800.0, now: Optional[float] = None) -> bool:
        """True while another routine clipped `race` less than `gap` ago (each routine keeps its own spacing as well)."""
        t = time.time() if now is None else now
        return any(w != who and t - ts < gap for w, ts in ((getattr(self, "exit_lock", None) or {}).get(race) or {}).items())

    def wind_down_owned(self, now: Optional[float] = None) -> set:
        """Races the wind-down owns while it runs (31 Oct 00:00 ET - 18:00 ET 3 Nov): every race it does not exempt. The
        cash raise and the RI trim leave them alone then (agreed with the cash-raise session, 24 Oct)."""
        t = time.time() if now is None else now
        if getattr(self, "wind_down", None) is None or not WD.active(t):
            return set()
        ex_r, _ = self.wind_down_exempt()
        return {r for r in self.universe.races if r not in ex_r}

    # ------------------------------------------------------------------ wind-down (proposals/wind_down.py, backlog 000)
    def wind_down_exempt(self) -> tuple[dict, dict]:
        """Races and legs the wind-down never sells or cancels orders on: cheap favourites' races (held through the call),
        high-frequency arb lots (flat by 3 Nov 17:30 ET), the catch-up sweep's legs (flat by 3 Nov), legs the punt executor
        or the election-night sleeve trade (any fill or order of WD.HOLD_PURPOSES) and params/wind_down_hold.json
        ({"races": [...], "exchanges": [...]}, written by hand or by the punt executor; read every call)."""
        races = {r: "cheap favourites" for r in cheapfav_races_of(self)}
        races.update({r: "shock mode" for r in shock_races_of(self)})   # shock closes its own (flat by 12:00 ET 3 Nov)
        races.update({r: "punt / election night" for r in punt_races_of(self)})   # whole races, not just the traded leg
        if self.churn is not None:
            races.update({l["race"]: "high-frequency arb" for l in self.churn.lots})
        legs = {str(e): "catch-up sweep" for e, q in (getattr(self, "catchup_open", None) or {}).items() if q}
        legs.update({str(e): "catch-up sweep" for e in (getattr(getattr(self, "catchup", None), "ctx", None) or {})})   # a trade being built in clips
        if self.store is not None:
            ph = ",".join("?" * len(WD.HOLD_PURPOSES))
            for r in self.store.rows(f"SELECT DISTINCT exchange_id FROM fills WHERE purpose IN ({ph}) UNION "
                                     f"SELECT DISTINCT exchange_id FROM orders WHERE purpose IN ({ph})", WD.HOLD_PURPOSES * 2):
                legs[str(r["exchange_id"])] = "punt / election night"
        try:
            hold = json.load(open(getattr(self, "wind_down_hold_path", None) or os.path.join(HERE, "params", "wind_down_hold.json")))
            races.update({str(r): "hold file" for r in hold.get("races", [])})
            legs.update({str(e): "hold file" for e in hold.get("exchanges", [])})
        except FileNotFoundError:
            pass
        except Exception as e:                       # unreadable hold file: sell nothing this cycle rather than guess
            log.warning("WIND-DOWN: params/wind_down_hold.json unreadable (%s): no clips this cycle", e)
            races["*"] = "hold file unreadable"
        return races, legs

    def wind_down_positions(self, positions: dict) -> dict:
        """The venue's positions minus the catch-up sweep's and the cheap-favourites sleeve's own open positions: those are not
        sets or inventory of ours to sell (review 1: a catch-up NO counted inside a set was sold, then catch-up's own exit
        crossed zero)."""
        out = dict(positions or {})
        for led in (getattr(self, "catchup_open", None) or {}, getattr(self, "cheapfav_open", None) or {},
                    getattr(self, "shock_open", None) or {}):
            for ex, q in led.items():
                out[str(ex)] = out.get(str(ex), 0.0) - q
        return out

    def wind_down_fairs(self, prices: dict) -> tuple[dict, callable]:
        """(exchange -> fair value: the outside fair, else a two-sided SIG mid; race -> favourite probability)."""
        fair = {r["exchange_id"]: float(r["fair"]) for r in (getattr(self.direction, "rows", None) or [])
                if r.get("exchange_id") and r.get("fair") is not None}
        for ex, p in (prices or {}).items():
            if ex not in fair and p.get("bestBid") is not None and p.get("bestAsk") is not None:
                fair[ex] = 0.5 * (p["bestBid"] + p["bestAsk"])

        def fav_of(race_name):
            r = self.universe.races.get(race_name)
            xs = [fair[e] for e in (r.exchange_ids if r else []) if e in fair]
            return max(xs) if xs else None
        return fair, fav_of

    def wind_down_report(self, now: float, prices: dict, every: float = 3600.0):
        """Hourly (backlog 000e): projected liquid cash at 18:00 ET 3 Nov by holding -> params/wind_down_report.json. Feed books
        only (no API reads)."""
        if now - self.wd_report_ts < every or not self.positions_snapshot:
            return
        self.wd_report_ts = now
        races = {r.name: r.exchange_ids for r in self.universe.races.values()}
        books = {}
        for e in self.universe.all_exchanges():
            b = self.fresh_book(e, 3600.0) if self.feed is not None else None
            if b is not None:
                books[e] = b
        _, fav_of = self.wind_down_fairs(prices)
        ex_r, ex_l = self.wind_down_exempt()
        rep = WD.project(races, self.wind_down_positions(self.positions_snapshot), books, fav_of, self.cash, clip=self.cfg.wind_down_clip_sets,
                         gap_sec=self.cfg.wind_down_gap_sec, max_walk=self.cfg.wind_down_max_walk_ticks * TICK,
                         min_clip=self.cfg.wind_down_min_clip, force_late=tuple(self.cfg.wind_down_force_late),
                         exempt_races=ex_r, exempt_legs=ex_l, mm_quoted=set(self.mm.quoted))
        rep["ts"], rep["et"] = now, WD.et_str(now)
        rep["sold"] = self.wind_down.sold[-50:]
        try:
            tmp = os.path.join(HERE, "params", "wind_down_report.json.tmp")
            with open(tmp, "w") as f:
                json.dump(rep, f, indent=1)
            os.replace(tmp, os.path.join(HERE, "params", "wind_down_report.json"))
        except OSError as e:
            log.warning("wind-down report: %s", e)
        t = rep["totals"]
        log.info("WIND-DOWN projection: cash %.0f + late sets %.0f + legs %.0f = %.0f by 18:00 ET 3 Nov; early-call sets %.0f today "
                 "(payout %.0f, held through the call); exempt: %s", t["cash_now"], t["late_sets_sale"], t["legs_sale"],
                 t["projected_cash_1800_et"], t["early_sets_sale_today"], t["early_sets_payout"], ", ".join(t["exempt"]) or "none")

    def wind_down_save(self):
        try:
            tmp = self.wind_down_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.wind_down.to_json(), f)
            os.replace(tmp, self.wind_down_path)
        except OSError as e:
            log.warning("wind-down state: %s", e)

    def wind_down_books(self, exs: list, rest_left: list) -> Optional[tuple]:
        """(raw, netted) books: feed books <= 30 s old, else REST (at most rest_left[0] reads a cycle); netted = our own resting
        orders taken out (what a clip can trade against). The timing gate compares the RAW touch with the medians, which
        are of the raw touch too (review 1: own-netted live cost vs raw medians kept the gate shut on MM-quoted races)."""
        bk = {e: self.fresh_book(e, 30.0) for e in exs}
        if any(v is None for v in bk.values()):
            if rest_left[0] <= 0:
                return None
            rest_left[0] -= 1
            try:
                bk = {e: self.get_book(e, 50) for e in exs}
            except ApiError as e:
                log.warning("wind-down books %s: %s", exs, e)
                return None
        own = own_levels(self)
        return bk, {e: own.net(e, b) for e, b in bk.items()}

    def wind_down_candidates(self, now: float, prices: dict) -> list:
        """Every clip the wind-down could sell now, with its cost; only those inside their cost cap and timing gate."""
        cfg, wd = self.cfg, self.wind_down
        venue = self.positions_snapshot or {}
        snap = self.wind_down_positions(venue)
        ex_r, ex_l = self.wind_down_exempt()
        if "*" in ex_r:
            return []
        fair, fav_of = self.wind_down_fairs(prices)
        gap = cfg.wind_down_gap_sec
        rest_left = [2]
        out = []
        sets, cost_left, clips_total = [], 0.0, 0
        for race in self.universe.races.values():
            exs = race.exchange_ids
            if race.name in ex_r or not any(abs(snap.get(e, 0.0)) >= 1 for e in exs):
                continue
            kind, k, res = WD.decompose(exs, snap)
            if kind and k >= 1 and not any(e in ex_l for e in exs):
                cls, why = WD.call_class(race.name, fav_of(race.name), tuple(cfg.wind_down_force_late))
                if cls == "late":
                    payout = (len(exs) - 1.0) if kind == "sell_set" else 1.0
                    clips_total += WD.clips_left(k, cfg.wind_down_clip_sets)
                    last = wd.last_race.get(race.name, 0.0)
                    free = now - last >= gap and not self.exit_locked(race.name, "wind_down", gap, now)
                    bb = self.wind_down_books(exs, rest_left) if free and not any(self.wind_down_foreign(e) for e in exs) else None
                    if bb is None:
                        cost_left += k * 0.03 * payout             # not quoted this cycle: a typical 3% exit cost
                        continue
                    raw, books = bb
                    floor = (getattr(cfg, "wind_down_set_floor", None) or {}).get(race.name)
                    if floor:
                        q, wq = WD.floor_clip(kind, books, exs, min(k, cfg.wind_down_floor_clip), min(cfg.wind_down_min_clip, k), floor)
                    else:
                        q, wq = WD.clip_quote(kind, books, exs, min(k, cfg.wind_down_clip_sets), cfg.wind_down_max_walk_ticks * TICK,
                                              min(cfg.wind_down_min_clip, k))
                    cost_left += k * (wq["forgone"] if wq else 0.03 * payout)
                    sets.append((race, kind, k, raw, books, q, wq, why))
            for e, r in res.items():
                if e in ex_l or abs(r) < cfg.wind_down_min_leg or wd.pulled.get(e, 0.0) > now or now - wd.last_leg.get(e, 0.0) < gap:
                    continue
                if self.exit_locked(race.name, "wind_down", gap, now):
                    continue                                       # the cash raise clipped this race < 30 min ago
                if self.wind_down_foreign(e):
                    continue
                bb = self.wind_down_books([e], rest_left)
                if not bb:
                    continue
                raw, nb = bb[0].get(e) or {}, bb[1].get(e) or {}
                lv = (nb.get("bids") if r > 0 else nb.get("asks")) or []
                if not lv:
                    continue
                rb, ra = (raw.get("bids") or [None])[0], (raw.get("asks") or [None])[0]
                mark = 0.5 * (float(rb["price"]) + float(ra["price"])) if rb and ra else None
                f = fair.get(e, mark)
                if f is None:
                    continue                                       # no outside fair and a one-sided book: no idea of the value
                touch, touch_q = float(lv[0]["price"]), float(lv[0]["quantity"])
                cost = (f - touch) if r > 0 else (touch - f)       # per share, vs the fair value
                px = touch if r > 0 else 1.0 - touch                # cash per share sold
                sellable = abs(r)
                if e in self.mm.quoted and now < self.wd_mm_end:
                    # MM inventory: only what is above the shrinking cap (notional at risk / price at risk)
                    risk_px = max(0.01, f if r > 0 else 1.0 - f)
                    sellable = max(0.0, abs(r) - WD.mm_inventory_cap(now, self.mm_inv_base, self.wd_mm_end) / risk_px)
                q = int(min(sellable, cfg.wind_down_leg_clip, 0.8 * touch_q))
                cap = WD.leg_cost_cap(now, px)
                if q >= cfg.wind_down_min_leg and px > 0 and cost <= cap + 1e-9:
                    fb = WD.fallback_ts(WD.clips_left(int(sellable), cfg.wind_down_leg_clip), gap, cfg.wind_down_fallback_buffer_sec)
                    vs_mark = 0.0 if mark is None else ((mark - touch) if r > 0 else (touch - mark))
                    out.append({"type": "leg", "race": race, "ex": e, "sign": 1 if r > 0 else -1, "qty": q, "price": touch, "cost_ps": cost,
                                "cap": cap, "cost_frac": max(0.0, cost) / px, "fallback": fb,
                                # what the kill switch sees: the drop against the marks, not only against the fair (review 1)
                                "est_cost": q * max(0.0, cost, vs_mark), "book_ps": max(0.0, cost, vs_mark),
                                "why": f"fair {f:.3f}, cost {cost / TICK:+.1f} ticks (cap {cap / TICK:.1f})"})
        # every late race drops its timing gate once the total exit cost (at the budget the fallback may use) or the total
        # clips need all the time left (review 2: races falling back together stranded sets at 18:00)
        gfb = WD.global_fallback_ts(cost_left + sum(c["est_cost"] for c in out), clips_total + len(out),
                                    max(cfg.wind_down_max_cost_per_hour, cfg.kill_drawdown - 100.0),
                                    60.0 / max(1, cfg.wind_down_clips_per_cycle), cfg.wind_down_fallback_buffer_sec)
        self.wd_global_fallback = gfb
        for c in out:
            c["fallback"] = min(c["fallback"], gfb)
        for race, kind, k, raw, books, q, wq, why in sets:
            exs = race.exchange_ids
            fb = min(gfb, WD.fallback_ts(WD.clips_left(k, cfg.wind_down_clip_sets), gap, cfg.wind_down_fallback_buffer_sec))
            m12, m24 = (self.wd_med.get(race.name) or {}).get(kind, (None, None))
            ok, gwhy = WD.gate_ok(now, WD.touch_cost(kind, raw, exs), m12, m24, fb)
            cap = WD.set_cost_cap(now, WD.locked_return(kind, getattr(self, "cost_basis", {}) or {}, venue, exs, k))
            floor = (getattr(cfg, "wind_down_set_floor", None) or {}).get(race.name)
            if floor:                                      # sold now at or above the floor, never under it: no schedule, no deadline
                ok, gwhy, cap = True, f"floor {floor:.3f} a set", wq["cost_frac"] if wq else 0.0
            if wq is not None and q >= 1 and ok and wq["cost_frac"] <= cap + 1e-9:
                out.append({"type": "set", "race": race, "kind": kind, "qty": q, "held": k, "books": books, "quote": wq,
                            "cap": cap, "cost_frac": wq["cost_frac"], "est_cost": q * wq["forgone"], "fallback": fb,
                            "floor": floor, "why": f"{why}; {gwhy}"})
        out.sort(key=lambda c: (0 if c.get("floor") else 1, c["cost_frac"]))   # Sizhe's floored sale goes first
        return out

    def wind_down_foreign(self, e: str) -> bool:
        """An open order on this leg that our store does not know (the punt executor before it records its order, Sizhe's
        own): the leg is left alone, no cancel and no clip (review 2)."""
        ids = {str(o.get("id")) for o in (self.open_orders_cache or {}).get(e, []) if o.get("id") is not None}
        if not ids:
            return False
        known = {str(r["order_id"]) for r in self.store.rows("SELECT order_id FROM orders WHERE exchange_id=? AND order_id IS NOT NULL", (str(e),))}
        unknown = ids - known
        if unknown and self.wd_foreign_logged.get(e, 0.0) < time.time() - 3600:
            self.wd_foreign_logged[e] = time.time()
            log.warning("WIND-DOWN: leg %s has open orders the bot did not place (%s): left alone", e, sorted(unknown)[:3])
        return bool(unknown)

    def wind_down_pause_legs(self, exs: list, now: float, sec: float = 900.0, retries: Optional[int] = None) -> bool:
        """Keep the MM off these legs for 15 min (the cash raise's raise_pause: no quotes, and no "hot" widening afterwards
        as with the news pause) and pull our resting orders on them,
        so a clip never meets our own quote. Called under the trade lock BEFORE positions are re-read (review 1: a fill
        between the re-read and the cancel took a leg through zero). False when a cancel failed or a leg carries an order
        the bot did not place (nothing is cancelled or sent)."""
        if any(self.wind_down_foreign(e) for e in exs):
            return False
        pause = {e: t for e, t in (getattr(self.mm, "raise_pause", None) or {}).items() if t > now}
        for e in exs:
            pause[e] = max(float(pause.get(e, 0.0)), now + sec)
        self.mm.raise_pause = pause
        self.store.set_param("raise_pause", pause)
        since = getattr(self, "open_orders_ts", 0.0) - 60.0
        for e in exs:
            mine = list((self.open_orders_cache or {}).get(e, []))
            newer = self.store.one("SELECT 1 FROM orders WHERE exchange_id=? AND status='open' AND placed_at>=? LIMIT 1", (str(e), since))
            if not mine and newer is None:
                continue
            try:
                self.api.cancel_all(exchange_id=e, **({} if retries is None else {"retries": retries}))
            except ApiError as ex_:
                log.warning("WIND-DOWN: cancel on %s failed (%s): clip not sent", e, ex_)
                return False
            ids = {o["id"] for o in mine} | {r["order_id"] for r in self.store.rows(
                "SELECT order_id FROM orders WHERE exchange_id=? AND status='open' AND order_id IS NOT NULL", (str(e),))}
            if ids:
                self.store.mark_orders(list(ids), "cancelled")
            if e in (self.open_orders_cache or {}):
                self.open_orders_cache[e] = []
        return True

    def wind_down_partisan_ok(self, before: dict, after: dict, prices: dict) -> bool:
        """The 5,000 partisan cap after the clip: |net D| within the cap, or no larger than before (a sale may only reduce it)."""
        cap = self.cfg.mm.portfolio_max_partisan_notional
        b = abs(risk.exposure(self.universe, before, prices).net_d_notional)
        a = abs(risk.exposure(self.universe, after, prices).net_d_notional)
        return a <= cap + 1e-6 or a <= b + 1e-6

    def wind_down_set_clip(self, c: dict, now: float, prices: dict):
        """Sell one set clip, all legs or none. Under the trade lock: pull our quotes on the legs, THEN re-read positions,
        re-clamp to the sets held now (other sleeves' ledgers taken out), re-check the cost cap on the clamped quote and the
        partisan cap, send. Returns the exit cost booked, None (nothing sent), or 'stop' (stop this cycle)."""
        race, kind, exs, wd = c["race"], c["kind"], c["race"].exchange_ids, self.wind_down
        t_lock = time.time()
        with self.trade_lock:
            if time.time() >= WD.STOP:
                return "stop"                            # the lock came after 18:00 ET: the punt / election night trade now
            ex_r, ex_l = self.wind_down_exempt()
            if race.name in ex_r or "*" in ex_r or any(e in ex_l for e in exs):
                return None
            if not self.wind_down_pause_legs(exs, now):
                return None
            fresh = self.fresh_positions()
            if fresh is None:
                log.warning("WIND-DOWN %s: positions re-read failed; stopping this cycle", race.name)
                return "stop"
            kind2, k2, _ = WD.decompose(exs, self.wind_down_positions(fresh))
            if kind2 != kind or k2 < 1:
                return None
            q = min(c["qty"], k2)
            wq = WD.walk_quote(kind, c["books"], exs, q)
            if wq is None or wq["cost_frac"] > c["cap"] + 1e-9:
                return None
            floor = (getattr(self.cfg, "wind_down_set_floor", None) or {}).get(race.name)
            if floor and WD.worst_set_price(kind, wq, exs) < floor - 1e-9:
                return None
            sgn = 1 if kind == "sell_set" else -1               # NO sets: every leg's short shrinks; YES sets: every long
            after = dict(fresh)
            for e in exs:
                after[e] = after.get(e, 0.0) + sgn * q
            if not self.wind_down_partisan_ok(fresh, after, prices):
                log.warning("WIND-DOWN %s: clip of %d sets skipped: it would leave |net D| above the partisan cap", race.name, q)
                return None
            # spacing and the shared lock start at the attempt, before the send: a rejected basket, or one whose send raised
            # (network error), waits a full gap (merge review L4)
            wd.last_race[race.name] = now
            self.stamp_exit(race.name, "wind_down", now)
            if kind == "sell_set":
                res = self.arb.unwind_set(race.name, c["books"], q, why="wind-down", min_qty=1, purpose="wind_down")
            else:
                res = self.arb.unwind_buy_set(race.name, c["books"], q, why="wind-down", purpose="wind_down")
            if not res or res.get("dry"):
                self.wind_down_save()
                return None
            self.refresh_snapshot({e: sgn * q for e in exs})
            payout = (len(exs) - 1.0) if kind == "sell_set" else 1.0
            cost = q * (payout - res["proceeds"])
            wd.book(now, f"{kind} {race.name}", q, res["proceeds"], cost)
            self.wind_down_save()
        held_s = time.time() - t_lock
        if held_s > 5.0:
            log.warning("WIND-DOWN %s: trade lock held %.1f s", race.name, held_s)
        self.cash += q * res["proceeds"]
        log.info("WIND-DOWN sold %d %s of %s at %.4f/set (cost %.1f, %.2f%% of proceeds, cap %.2f%%; %d left): %s", q, kind, race.name,
                 res["proceeds"], cost, 100 * wq["cost_frac"], 100 * c["cap"], k2 - q, c["why"])
        return cost

    def wind_down_leg_clip(self, c: dict, now: float, prices: dict):
        """Sell one single-leg clip at the touch, fill-or-cancel. Under the trade lock: skip a leg with a zero-EV exit sent in the
        last 2 min, pull our quotes on the leg, THEN re-read positions and clamp to the residual held now (same sign, never
        through zero; other sleeves' ledgers taken out), check the partisan cap, send, cancel any remainder at once. A
        remainder that cannot be cancelled, or an order whose status is unknown, pulls the leg for 30 min and stops the cycle."""
        e, race, wd, sgn = c["ex"], c["race"], self.wind_down, c["sign"]
        t_lock = time.time()
        with self.trade_lock:
            if time.time() >= WD.STOP:
                return "stop"                            # the lock came after 18:00 ET: the punt / election night trade now
            ex_r, ex_l = self.wind_down_exempt()
            if e in ex_l or race.name in ex_r or "*" in ex_r:
                return None
            if self.store.one("SELECT 1 FROM orders WHERE exchange_id=? AND purpose='exit' AND placed_at>? LIMIT 1", (str(e), now - 120.0)):
                return None
            if not self.wind_down_pause_legs([e], now):
                return None
            fresh = self.fresh_positions()
            if fresh is None:
                log.warning("WIND-DOWN %s: positions re-read failed; stopping this cycle", e)
                return "stop"
            _, _, res = WD.decompose(race.exchange_ids, self.wind_down_positions(fresh))
            r = res.get(e, 0.0)
            if r * sgn <= 0:
                return None
            q = int(min(c["qty"], abs(r)))
            if q < self.cfg.wind_down_min_leg:
                return None
            after = dict(fresh)
            after[e] = after.get(e, 0.0) - sgn * q
            if not self.wind_down_partisan_ok(fresh, after, prices):
                log.warning("WIND-DOWN %s: leg clip of %d skipped: it would leave |net D| above the partisan cap", e, q)
                return None
            side, px = ("no", round(1.0 - c["price"], 3)) if sgn > 0 else ("yes", round(c["price"], 3))
            ttl = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            wd.last_leg[e] = now
            self.stamp_exit(race.name, "wind_down", now)
            try:
                resp = self.api.place_order(e, side, "buy", q, px, expiration=ttl, key=self.api.new_key("wdx"))
            except ApiError as ex_:
                wd.pulled[e] = now + 1800.0
                self.wind_down_save()
                log.warning("WIND-DOWN %s: order failed (%s): leg pulled for 30 min, cycle stopped", e, ex_)
                return "stop"
            oid = resp.get("orderId")
            self.store.record_order(oid, e, None, side, "buy", px, q, "exit", response=resp)
            traded = int(float(resp.get("quantityTraded") or resp.get("filledQuantity") or 0))
            if resp.get("open"):
                try:
                    self.api.cancel_order(oid)
                    self.store.mark_orders([oid], "cancelled")
                except ApiError as ex_:
                    wd.pulled[e] = now + 1800.0
                    self.refresh_snapshot({e: -sgn * traded})
                    self.wind_down_save()
                    log.warning("WIND-DOWN %s: remainder of order %s not cancelled (%s): leg pulled for 30 min, cycle stopped", e, oid, ex_)
                    return "stop"
            elif oid is not None:
                self.store.mark_orders([oid], "filled")
            self.refresh_snapshot({e: -sgn * traded})
            cost = traded * c["book_ps"]                 # vs the fair or the mark, whichever is worse: what the kill switch sees
            if traded > 0:
                wd.book(now, f"leg {e} {'yes' if sgn > 0 else 'no'}", traded, c["price"] if sgn > 0 else 1.0 - c["price"], cost)
            self.wind_down_save()
        held_s = time.time() - t_lock
        if held_s > 5.0:
            log.warning("WIND-DOWN %s: trade lock held %.1f s", e, held_s)
        if traded > 0:
            log.info("WIND-DOWN sold %d %s of leg %s (%s) at %.3f (%s)", traded, "YES" if sgn > 0 else "NO", e, race.name, px, c["why"])
        return cost

    def wind_down_cycle(self, prices: dict, killed: bool, cycle_id=None):
        """Backlog 000 (Sizhe 23 Oct: RAISE_TARGET = everything): from 31 Oct 00:00 ET to 18:00 ET 3 Nov sell the late-call
        sets, the single legs and the MM inventory above its shrinking cap, at most wind_down_clips_per_cycle clips a cycle,
        cheapest exit cost per unit of cash first, inside the cost caps, the timing gate, the hourly cost budget and the
        partisan cap. Before the start: only the hourly projection. Early-call sets are held (selling them at the call is
        report-only). Nothing at or after 18:00 ET: the punt and the election-night sleeve trade then."""
        wd = self.wind_down
        if wd is None or self.store is None:
            return
        now = time.time()
        try:
            self.wind_down_report(now, prices)
        except Exception as e:                       # the report must never stop the sales
            log.warning("wind-down report: %s", e)
        if self.dry_run or not WD.active(now):
            return
        if self.cfg.arb.recycle_enabled:
            # every set a recycle buys from now on would have to be sold again before 18:00 ET 3 Nov
            self.cfg.arb.recycle_enabled = False
            log.warning("WIND-DOWN: recycling switched off for the wind-down (free unwinds continue)")
        if getattr(self, "positions_ok", True) is False:
            log.warning("WIND-DOWN: skipped this cycle: the positions read failed")
            return
        if now - self.wd_med_ts > 900:
            rows = self.store.rows("SELECT race, party, ts, bid, ask FROM venue_quotes WHERE venue='sig' AND ts > ?", (now - 86400.0 - 1800.0,))
            legs_of = {r.name: list(r.legs) for r in self.universe.races.values()}
            self.wd_med = WD.cost_medians([(r["race"], r["party"], r["ts"], r["bid"], r["ask"]) for r in rows], legs_of, now)
            self.wd_med_ts = now
        spent = wd.cost_last_hour(now)
        budget = self.cfg.wind_down_max_cost_per_hour
        # past a clip's fallback time the budget may use the kill switch's remaining headroom (review 1: 200/h stranded
        # leftovers at 18:00 when several races fell back at once); 100 kept as a margin for the MM's marks
        late_budget = max(budget, self.cfg.kill_drawdown - 100.0)
        done = 0
        for c in self.wind_down_candidates(now, prices):
            if done >= self.cfg.wind_down_clips_per_cycle:
                break
            late = now >= c["fallback"]
            if (killed or strategy_paused(self, "exit:raise")
                    or (c["type"] != "set" and strategy_paused(self, "exit:zero_ev"))) and not late:
                continue                                   # kill switch / stops: only clips that cannot wait any longer
            # past its fallback a clip may use the kill switch's room: the drawdown already holds this hour's exits, so the
            # larger of the two is counted once (review 2)
            used = max(spent, getattr(self, "wd_drawdown", 0.0)) if late else spent
            if not c.get("floor") and used + c["est_cost"] > (late_budget if late else budget):   # a floored race's cost is bounded by its floor
                if now - self.wd_cap_logged > 900:
                    self.wd_cap_logged = now
                    log.info("WIND-DOWN: hourly exit-cost budget %.0f reached (%.0f used): waiting", late_budget if late else budget, used)
                continue
            got = self.wind_down_set_clip(c, now, prices) if c["type"] == "set" else self.wind_down_leg_clip(c, now, prices)
            if got == "stop":
                break
            if got is not None:
                done += 1
                spent += got

    def snapshot_books(self, every: float = 3600.0, history_every: float = 6 * 3600.0):
        """Hourly copy of every order book the feed holds (no API reads) to data/books_latest.json, the format of
        data/books.json ({exchange_id: book}) so the punt screener reads current depth; every 6 h also to
        data/books_history/books_<UTC time>.json to see how depth changes into 3 Nov."""
        now = time.time()
        if now - getattr(self, "books_snap_ts", 0.0) < every or not self.feed_ok():
            return
        books = self.feed.books()
        if len(books) < 0.8 * len(self.universe.all_exchanges()):
            return                                    # feed still filling after a (re)connect: wait for a full set
        self.books_snap_ts = now
        d = os.path.join(HERE, "data")
        tmp = os.path.join(d, "books_latest.json.tmp")
        with open(tmp, "w") as f:
            json.dump(books, f)
        os.replace(tmp, os.path.join(d, "books_latest.json"))
        with open(os.path.join(d, "books_latest.meta.json"), "w") as f:
            json.dump({"ts": now, "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"), "n": len(books)}, f)
        if now - getattr(self, "books_hist_ts", 0.0) >= history_every:
            self.books_hist_ts = now
            os.makedirs(os.path.join(d, "books_history"), exist_ok=True)
            with open(os.path.join(d, "books_history", "books_%s.json" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")), "w") as f:
                json.dump(books, f)
        log.info("order books saved: %d markets -> data/books_latest.json", len(books))

    def mm_positions(self, positions: dict) -> dict:
        """Positions the MM manages: the venue's, minus the catch-up sweep's, the cheap-favourites sleeve's and shock mode's
        own open positions (the MM used to count them as inventory and skew to unload them before they converged; review 23 Oct)."""
        cu = dict(getattr(self, "catchup_open", None) or {})
        for ex, q in (list((getattr(self, "cheapfav_open", None) or {}).items())     # held to the call: not MM inventory
                      + list((getattr(self, "shock_open", None) or {}).items())):     # shock closes its own within ~60 min
            cu[ex] = cu.get(ex, 0.0) + q
        if not cu:
            return positions
        return {ex: q - cu.get(ex, 0.0) for ex, q in positions.items()}

    def mm_touch(self, ex: str):
        """Freshest (best bid, best ask) for the MM's last look before it sends quotes, with no new read: the feed book when
        the feed counts it fresh, else the newest of this cycle's REST order book for `ex` and the REST price snapshot
        (rest_snapshot), if it is at most LASTLOOK_MAX_AGE old. None otherwise, and the MM then drops the quote. Before,
        a stale feed book returned None and the quote went out unchecked (sim 'day' 24 Oct: during the feed outage an
        MM bid went into the 0.76 ask on 932)."""
        def touch(bk):
            return ((bk.get("bids") or [{}])[0].get("price") if bk.get("bids") else None,
                    (bk.get("asks") or [{}])[0].get("price") if bk.get("asks") else None)
        bk = self.fresh_book(ex)
        if bk:
            return touch(bk)
        now, best = time.time(), None
        c, t = (self.book_cache.get(ex) or (None,))[0], (getattr(self, "book_ts", None) or {}).get(ex)
        if c and t is not None and now - t <= LASTLOOK_MAX_AGE:
            best = (t, touch(c))
        snap, snap_ts = getattr(self, "rest_snapshot", None) or ({}, 0.0)   # stamped when REST was read (not when overlaid)
        p = (snap or {}).get(ex) or {}
        if (p.get("bestBid") is not None or p.get("bestAsk") is not None) and now - snap_ts <= LASTLOOK_MAX_AGE \
                and (best is None or snap_ts > best[0]):
            best = (snap_ts, (p.get("bestBid"), p.get("bestAsk")))
        return best[1] if best else None

    def catchup_cycle(self, dir_summary: dict, positions: dict, prices: dict, killed: bool):
        """Catch-up sweep (proposals/outside_sweep.py): exits first (8 h fast / 20 h slow time stops, slow take-profit, outside
        disagreement or reversal, kill, 3 Nov flatten; no convergence exit), then more clips of the trades being built, then
        entries on a fresh directional poll. A trade is built in repeat clips of at most 0.8 of the touch. Only entries and
        clips are limited by the partisan cap and the cash reserve; exits never are. Positions come from the fills table
        (purposes catchup / catchup_exit), so a restart cannot lose them. One order per exchange in flight (60 s TTL)."""
        cs = self.catchup
        if cs is None or self.dry_run or self.store is None:
            return
        now = time.time()
        rows = getattr(self.direction, "rows", None) or []
        if dir_summary.get("polled"):
            cs.push(rows)
        byex = {str(r["exchange_id"]): r for r in rows if r.get("exchange_id")}
        fills = [dict(r) for r in self.store.rows("SELECT exchange_id, quantity, price FROM fills WHERE purpose IN ('catchup','catchup_exit') ORDER BY filled_at")]
        marks = {ex: r.get("sig_mid") for ex, r in byex.items()}
        pnl, open_pos = pnl_from_fills(fills, marks)
        avg = avg_yes_from_fills(fills)
        self.catchup_open = dict(open_pos)           # the MM excludes these from its inventory
        # a failed fills read leaves the ledger behind the venue: an exit or a clip off it would repeat or flip an order (review 31 Oct)
        fills_ok = getattr(self, "fills_read_ok", True) is not False
        if not fills_ok:
            log.warning("CATCHUP: fills read failed this cycle: no exits, clips or entries until the ledger is current")
        if not cs.killed and cs.check_kill(pnl):
            log.warning("CATCHUP: sleeve P&L %.1f at or below %.0f: switched off; open positions are being closed", pnl, cs.kill_pnl)
        resting = {str(r["exchange_id"]) for r in self.store.rows(
            "SELECT exchange_id FROM orders WHERE purpose IN ('catchup','catchup_exit') AND status='open' AND placed_at > ?", (now - 90,))}
        cu_ids = {str(r["order_id"]) for r in self.store.rows("SELECT order_id FROM orders WHERE purpose IN ('catchup','catchup_exit') AND placed_at > ?",
                                                             (now - 6 * 3600,))}
        for ex_o, lst in (self.open_orders_cache or {}).items():
            if any(str(o.get("id")) in cu_ids for o in lst):
                resting.add(str(ex_o))                 # the venue still shows one of our catchup orders resting here
        changed = False
        own = None
        for ex, q in open_pos.items():
            if ex in resting or not fills_ok:
                continue
            if now - cs.exit_try.get(ex, 0.0) < CLIP_WAIT:
                continue                               # an exit went out (or timed out unanswered) a moment ago
            why = cs.exit_reason(ex, byex.get(ex), now, avg.get(ex), q)
            if not why:
                continue
            bk = self.fresh_book(ex, 30.0)
            if not bk:
                try:
                    bk = self.get_book(ex, 5)          # REST read: never price an exit off a stale direction row
                except ApiError as e:
                    log.warning("catchup exit book %s: %s", ex, e)
                    continue
            if own is None:
                own = own_levels(self)
            bk = own.net(ex, bk)                       # our own MM quotes are not liquidity: the venue never matches us with ourselves
            bid = bk["bids"][0]["price"] if bk.get("bids") else None
            ask = bk["asks"][0]["price"] if bk.get("asks") else None
            if q > 0 and bid is None or q < 0 and ask is None:
                log.warning("CATCHUP exit %s waits: no %s in the book (%s)", ex, "bid" if q > 0 else "ask", why)
                continue
            if why.startswith("slow take-profit"):     # the trigger read a direction row: confirm on the book the order is priced off
                live_v = (bid - avg[ex]) if q > 0 and ex in avg else (avg[ex] - ask) if q < 0 and ex in avg else None
                if live_v is None or live_v < TAKE_PROFIT_SLOW - 1e-9:
                    continue
            side, px = ("no", round(1 - bid, 3)) if q > 0 else ("yes", round(ask, 3))
            cs.exit_try[ex] = now                      # set before the call: a timeout leaves the outcome unknown, never re-sent blind
            if ex in cs.ctx:
                cs.ctx[ex]["exiting"] = True           # no more clips of a trade that is being closed
            changed = True
            self.catchup_save()                        # saved first: a restart inside CLIP_WAIT must still see the throttle
            try:
                resp = self.api.place_order(ex, side, "buy", int(abs(q)), px, expiration=expiry_in(60), key=self.api.new_key("cux"))
                self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, int(abs(q)), "catchup_exit", response=resp)
                log.info("CATCHUP exit %s %s x%d @%.3f (%s)", ex, side, int(abs(q)), px, why)
            except ApiError as e:
                log.warning("catchup exit %s: %s", ex, e)
            except Exception as e:                     # timeout / connection reset: the order may be live; the ledger will show it
                log.warning("catchup exit %s: unknown outcome (%s): not re-sent for %.0f s", ex, e, CLIP_WAIT)
        for ex in list(cs.ctx):
            if ex not in open_pos and ex not in resting and now - cs.ctx[ex].get("ts", now) > 300:
                cs.ctx.pop(ex)                      # flat and settled: forget the entry context
                changed = True
        # an unread account is not an empty one: net D would read 0 and the clip would take the whole room again
        can_trade = (not killed and not cs.killed and dir_summary.get("polled") and not strategy_paused(self, "catchup") and fills_ok
                     and getattr(self, "positions_ok", True) is not False)
        building = [ex for ex, c in cs.ctx.items() if can_trade and c.get("target") and not c.get("exiting") and ex not in resting
                    and now < c.get("build_until", 0.0) and now <= CATCHUP_LAST_ENTRY]   # the reserve is free after LAST_ENTRY: no clip spends it
        sigs = [s for s in cs.signals(rows, now) if s["race"] not in punt_races_of(self)] if can_trade else []
        if building or sigs:
            lock = getattr(self, "trade_lock", None)
            if lock is None:
                changed |= self._catchup_trade(cs, building, sigs, open_pos, resting, avg, byex, positions, prices, now)
            else:
                with lock:                               # the scanner thread trades sets between cycles: read the book under the lock
                    changed |= self._catchup_trade(cs, building, sigs, open_pos, resting, avg, byex, positions, prices, now)
        if changed or cs.killed:
            self.catchup_save()

    def catchup_pending(self, now: float) -> tuple:
        """(D, R) notional of the other sleeves' entry orders that have not filled yet (placed in the last 330 s, the longest TTL,
        or still resting): the cheap-favourites pattern, one-sided so a D order only counts against D adds."""
        resting_ids = {str(o.get("id")) for lst in (getattr(self, "open_orders_cache", None) or {}).values() for o in lst}
        others = [dict(r) for r in self.store.rows(
            "SELECT order_id, exchange_id, side, quantity, price, placed_at FROM orders WHERE purpose IN "
            "('cheapfav','direction','tilt','inside','shock') AND (placed_at > ? OR status='open')", (now - 330,))]
        others = [o for o in others if float(o.get("placed_at") or 0) > now - 330 or str(o.get("order_id")) in resting_ids]
        filled: dict = {}
        if others:
            for f in self.store.rows("SELECT order_id, quantity FROM fills WHERE purpose IN ('cheapfav','direction','tilt','inside','shock') "
                                     "AND ingested_at > ?", (now - 6 * 3600,)):
                filled[str(f["order_id"])] = filled.get(str(f["order_id"]), 0.0) + abs(float(f["quantity"]))
        pend = {"D": 0.0, "R": 0.0}
        for o in others:
            left = float(o["quantity"]) - filled.get(str(o.get("order_id")), 0.0)
            d = risk.adds(self.universe, str(o["exchange_id"]), o["side"])
            if left >= 1 and d in pend and o.get("price") is not None:
                pend[d] += left * float(o["price"])
        return pend["D"], pend["R"]

    def _catchup_trade(self, cs, building, sigs, open_pos, resting, avg, byex, positions, prices, now) -> bool:
        """Clips and entries of the catch-up sweep, under the trade lock. The partisan room is judged on a positions read made
        now (the scanner, direction and cheap favourites may have traded since the cycle started), net of the other sleeves'
        unfilled orders and our own unfilled clips, and against the book as it will be once the punt's timed exit has run:
        for D adds the larger of the net D with and without the punt's legs, for R adds the smaller (the punt exit must still
        fit the cap, exit_cap_ok; review 31 Oct)."""
        cap = self.cfg.mm.portfolio_max_partisan_notional
        fr = getattr(self, "fresh_positions", None)
        if callable(fr):
            fresh = fr()
            if fresh is None:
                log.warning("CATCHUP: positions re-read failed: no clips or entries this cycle")
                return False
            positions = fresh
        pl = self.punt_legs() if callable(getattr(self, "punt_legs", None)) else set()
        nd_all = risk.exposure(self.universe, positions, prices).net_d_notional
        nd_ex = risk.exposure(self.universe, {e: q for e, q in positions.items() if str(e) not in pl}, prices).net_d_notional if pl else nd_all
        net = {"all": nd_all, "ex": nd_ex}
        pend_d, pend_r = self.catchup_pending(now)
        pend = {"D": pend_d, "R": pend_r}
        for e, c in cs.ctx.items():                        # our own clips not filled yet
            if now - c.get("last_clip", 0.0) >= CLIP_WAIT:
                c["ordered"] = abs(open_pos.get(e, 0.0))   # the last order is over (filled or expired): count what we hold
            left = c.get("ordered", 0) - abs(open_pos.get(e, 0.0))
            d = risk.adds(self.universe, e, "yes" if c.get("d", 1) > 0 else "no")
            if left > 0 and d in pend:
                pend[d] += left * c.get("px0", 0.5)
        n_open = len(set(open_pos) | set(resting) | set(cs.ctx))
        open_cost = sum(abs(q) * (avg[e] if q > 0 else 1.0 - avg[e]) for e, q in open_pos.items() if e in avg)
        open_cost += sum(max(0.0, c.get("ordered", 0) - abs(open_pos.get(e, 0.0))) * c.get("px0", 0.5) for e, c in cs.ctx.items())   # ordered, not yet held
        own = own_levels(self)
        changed = False

        def eff_net(adds_dir):
            if adds_dir == "D":
                return max(net["all"], net["ex"]) + pend["D"]
            if adds_dir == "R":
                return min(net["all"], net["ex"]) - pend["R"]
            return net["all"]

        def clip(ex, s, remaining, first):
            """One clip on ex for signal s: priced at the fresh touch (our own quotes netted out), cut to the partisan room, the
            sleeve's open-cost limit and the spendable cash. Returns the notional placed (0 if nothing)."""
            nonlocal open_cost
            bk = self.fresh_book(ex, 10.0)
            bk = own.net(ex, bk) if bk else bk
            if not bk or not bk.get("bids") or not bk.get("asks"):
                return 0.0
            b0, a0 = bk["bids"][0], bk["asks"][0]
            if a0["price"] - b0["price"] > 2 * TICK + 1e-9:
                return 0.0
            s = dict(s, bid=b0["price"], ask=a0["price"])
            side, px, q = cs.size(s, (a0 if s["d"] > 0 else b0)["quantity"], remaining)
            if not q:
                return 0.0
            if not first and px > cs.ctx[ex]["px0"] + CLIP_SLIPPAGE + 1e-9:
                return 0.0                             # the touch ran away from the first clip
            adds_dir = risk.adds(self.universe, ex, side)
            q = min(q, int(min(cs.room(adds_dir, eff_net(adds_dir), cap), 1e9) / max(px, 0.01)),
                    int(max(0.0, cs.max_open_notional - open_cost) / max(px, 0.01)),
                    int(max(0.0, spendable_cash(self)) / max(px, 0.01)))     # cash above the shock reserve is all catch-up may spend
            if q < cs.min_shares or not cs.can_enter(n_open if first else 0, adds_dir, eff_net(adds_dir), q * px, cap=cap):
                return 0.0
            try:
                resp = self.api.place_order(ex, side, "buy", q, px, expiration=expiry_in(60), key=self.api.new_key("cu"))
                self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, q, "catchup", response=resp)
            except ApiError as e:
                log.warning("catchup entry %s: %s", ex, e)
                return 0.0
            except Exception as e:                     # timeout / reset: the order may be live. Count it as placed (context, clip clock),
                log.warning("catchup entry %s: unknown outcome (%s): counted as placed", ex, e)   # so the next poll cannot send a second one
            self.cash = getattr(self, "cash", 0.0) - q * px   # the next entry this cycle sees it (shock reserve)
            open_cost += q * px
            delta = q * px if adds_dir == "D" else (-q * px if adds_dir == "R" else 0.0)
            net["all"] += delta                            # the next clip sees it (both views: this leg is not a punt leg)
            net["ex"] += delta
            if first:
                cs.ctx[ex] = {"ts": now, "d": s["d"], "f_then": s["f_then"], "move": s["move"], "mid0": s["mid"], "gap": s["gap"],
                              "kind": s["kind"], "pts": s["pts"], "right": bool(s["right"]), "px0": px, "target": cs.target_shares(s, px),
                              "ordered": q, "clips": 1, "last_clip": now,
                              "build_until": now + (BUILD_WINDOW_FAST if s["kind"] == "fast" else BUILD_WINDOW_SLOW)}
                cs.last_signal[s["race"]] = now
            else:
                c = cs.ctx[ex]
                c["ordered"] = c.get("ordered", 0) + q
                c["clips"] = c.get("clips", 1) + 1
                c["last_clip"] = now
            log.info("CATCHUP %s %s %s %s x%d @%.3f (%s %d pts%s, clip %d of target %d): outside moved %+.3f, SIG gap %.1f ticks",
                     "enter" if first else "clip", s["race"], ex, side, q, px, s["kind"], s["pts"], ", right of the line" if s["right"] else "",
                     cs.ctx[ex]["clips"], cs.ctx[ex]["target"], s["move"], s["gap"] / TICK)
            self.catchup_save()                        # a restart now must not lose the context (the leg would be sold as "no entry context")
            return q * px

        for ex in building:                            # more clips of the trades being built
            c = cs.ctx[ex]
            held = abs(open_pos.get(ex, 0.0))
            if now - c.get("last_clip", 0.0) < CLIP_WAIT and held < c.get("ordered", 0) - cs.min_shares:
                continue                               # the last clip is still settling
            remaining = c["target"] - c["ordered"]
            row = byex.get(ex)
            if remaining < cs.min_shares or row is None or cs.exit_reason(ex, row, now, avg.get(ex), open_pos.get(ex, 0.0)):
                continue
            s = {"d": c["d"], "race": row.get("race"), "kind": c.get("kind"), "pts": c.get("pts", 0), "right": c.get("right", False),
                 "f_then": c["f_then"], "move": c["move"], "mid": row.get("sig_mid") or c["mid0"], "gap": c["gap"]}
            if clip(ex, s, remaining, False):
                changed = True
        for s in sigs:
            ex = s["exchange_id"]
            if ex in open_pos or ex in resting or ex in cs.ctx:
                continue
            if clip(ex, s, None, True):
                n_open += 1
                changed = True
        return changed

    def catchup_ledger_refresh(self):
        """catchup_open from the fills just ingested (catchup_cycle runs after the MM; without this the MM sees last cycle's ledger)."""
        if self.catchup is None or self.store is None or self.dry_run:
            return
        try:
            fills = [dict(r) for r in self.store.rows("SELECT exchange_id, quantity, price FROM fills WHERE purpose IN ('catchup','catchup_exit') ORDER BY filled_at")]
            self.catchup_open = dict(pnl_from_fills(fills, {})[1])
        except Exception as e:
            log.warning("catchup ledger refresh: %s", e)

    def catchup_save(self):
        cs = self.catchup
        try:
            tmp = self.catchup_state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"ctx": cs.ctx, "last_signal": cs.last_signal, "exit_try": cs.exit_try, "killed": cs.killed}, f)
            os.replace(tmp, self.catchup_state_path)
        except OSError as e:
            log.warning("catchup state: %s", e)

    def cheapfav_races(self) -> set:
        """Races where the cheap-favourites sleeve holds a position (its ledger, rebuilt from fills)."""
        bx = self.universe.by_exchange
        return {bx[ex][0] for ex in (getattr(self, "cheapfav_open", None) or {}) if ex in bx}

    def cheapfav_refresh(self):
        race_of = {ex: (r[0], r[1]) for ex, r in self.universe.by_exchange.items()}
        fills = [dict(r) for r in self.store.rows("SELECT order_id, exchange_id, quantity, price FROM fills WHERE purpose IN ('cheapfav','cheapfav_exit') "
                                                  "AND price IS NOT NULL ORDER BY filled_at")]
        open_pos, _cost, held, deployed = CF.ledger(fills, race_of)
        self.cheapfav_open = dict(open_pos)          # the MM excludes these from its inventory; the set paths leave their races alone
        return fills, race_of, open_pos, held, deployed

    def cheapfav_cycle(self, dir_summary: dict, positions: dict, prices: dict, killed: bool):
        """Cheap safe favourites (proposals/cheap_favourites.py): exits only on a reversed edge, then entries on a fresh
        directional poll. The ledger is the fills table (purposes cheapfav / cheapfav_exit), so a restart cannot lose a
        position. One order per exchange in flight (60 s TTL); at most 3 entries per poll."""
        cf = self.cheapfav
        if cf is None or self.dry_run or self.store is None:
            return
        now = time.time()
        rows = getattr(self.direction, "rows", None) or []
        byex = {str(r["exchange_id"]): r for r in rows if r.get("exchange_id")}
        fills, race_of, open_pos, held, deployed = self.cheapfav_refresh()
        # entry orders of the last 3 min (60 s TTL + ingest lag) not yet in the fills table count as held: a lagging fill
        # ingest must not let the sleeve buy the same leg twice, and their notional counts on the partisan axis (review 24 Oct)
        filled_by_order: dict = {}
        for f in fills:
            k = str(f.get("order_id"))
            filled_by_order[k] = filled_by_order.get(k, 0.0) + abs(float(f["quantity"]))
        recent = [dict(r) for r in self.store.rows("SELECT order_id, exchange_id, side, price, quantity FROM orders WHERE purpose='cheapfav' "
                                                   "AND placed_at > ?", (now - 180,))]
        pend, pend_total = CF.pending(recent, filled_by_order, race_of)
        own_filled = sum(x if k[1] == "D" else -x for k, x in held.items())       # the sleeve's filled net D cost
        for k, x in pend.items():
            held[k] = held.get(k, 0.0) + x
        deployed += pend_total
        # partisan: pending counted one-sided (D only against D adds, R only against R adds; review 24 Oct)
        cf_pend = (own_filled, sum(x for k, x in pend.items() if k[1] == "D"), sum(x for k, x in pend.items() if k[1] == "R"))
        resting = {str(r["exchange_id"]) for r in self.store.rows(
            "SELECT exchange_id FROM orders WHERE purpose IN ('cheapfav','cheapfav_exit') AND status='open' AND placed_at > ?", (now - 90,))}
        cf_ids = {str(r["order_id"]) for r in self.store.rows("SELECT order_id FROM orders WHERE purpose IN ('cheapfav','cheapfav_exit') AND placed_at > ?",
                                                             (now - 6 * 3600,))}
        for ex_o, lst in (self.open_orders_cache or {}).items():
            if any(str(o.get("id")) in cf_ids for o in lst):
                resting.add(str(ex_o))                 # the venue still shows one of our orders resting here
        lock = getattr(self, "trade_lock", None)
        if lock is None:
            self._cheapfav_trade(cf, now, rows, byex, race_of, open_pos, held, deployed, cf_pend, resting, positions, prices, killed, dir_summary)
            return
        with lock:                                   # the scanner thread trades sets between cycles: read its snapshot here
            self._cheapfav_trade(cf, now, rows, byex, race_of, open_pos, held, deployed, cf_pend, resting, positions, prices, killed, dir_summary)

    def cheapfav_rotation_exit(self, cf, now, exited, resting, prices):
        """Sell what the sleeve bought since cheapfav_rotation_from (Sizhe 31 Oct: rotate into 90-92c favourites, sell by 2 Nov),
        between cheapfav_rotation_exit_start and _stop: one clip per leg per 5 min at the touch, never under the lot's average
        cost less cheapfav_rotation_floor. What is unsold at the stop stays to the result. The older holdings are not touched."""
        cfg = getattr(self, "cfg", None)
        t0, t1, t2 = (cfg_ts(cfg, k) for k in ("cheapfav_rotation_from", "cheapfav_rotation_exit_start", "cheapfav_rotation_exit_stop"))
        if None in (t0, t1, t2) or not t1 <= now < t2 or strategy_paused(self, "exit:cheapfav"):
            return
        fills = [dict(r) for r in self.store.rows("SELECT exchange_id, quantity, price FROM fills WHERE purpose IN ('cheapfav','cheapfav_exit') "
                                                  "AND ingested_at >= ? ORDER BY id", (t0,))]
        lots, cost, _, _ = CF.ledger(fills, {})
        for ex, q in lots.items():
            if ex in resting or ex in exited:
                continue
            bk = self.fresh_book(ex, 10.0)
            if not bk:
                continue
            bk = own_levels(self).net(ex, bk)            # our own quotes are not liquidity
            o = CF.CheapFavourites.rotation_exit(q, cost.get(ex, 0.0) / abs(q), bk, cfg.cheapfav_rotation_floor, cfg.cheapfav_rotation_clip)
            if o is None:
                continue
            side, px, n = o
            fr = self.fresh_positions()
            if fr is None:
                return
            after = dict(fr)
            after[ex] = after.get(ex, 0.0) + (n if side == "yes" else -n)
            if not self.wind_down_partisan_ok(fr, after, prices):
                continue
            try:
                resp = self.api.place_order(ex, side, "buy", n, px, expiration=expiry_in(60), key=self.api.new_key("cfx"))
                self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, n, "cheapfav_exit", response=resp)
                exited.add(ex)
                log.info("CHEAPFAV rotation exit %s %s x%d @%.3f (lot %.0f at %.3f)", ex, side, n, px, q, cost.get(ex, 0.0) / abs(q))
            except ApiError as e:
                log.warning("cheapfav rotation exit %s: %s", ex, e)

    def _cheapfav_trade(self, cf, now, rows, byex, race_of, open_pos, held, deployed, cf_pend, resting, positions, prices, killed, dir_summary):
        positions = getattr(self, "positions_snapshot", None) or positions
        # one exit per leg per 5 min, filled or not: if the fill ingest fails after an exit filled, the ledger still shows
        # the holding and a second exit would flip it (review 24 Oct); a reversed edge is not urgent
        exited = {str(r["exchange_id"]) for r in self.store.rows(
            "SELECT exchange_id FROM orders WHERE purpose='cheapfav_exit' AND placed_at > ?", (now - 300,))}
        gap_own = set((self.store.get_param("gap_legs", {}) or {}) if getattr(self, "gap", None) is not None else ())
        for ex, q in open_pos.items():
            if ex in resting or ex in exited or ex in gap_own or ex not in race_of or strategy_paused(self, "exit:cheapfav"):
                continue                                 # (the pilot's legs: its own exits cover a closed / reversed gap; review 28 Oct)
            why = cf.exit_reason(ex, q, byex.get(ex), race_of[ex][0], now)
            if not why:
                continue
            bk = self.fresh_book(ex, 10.0)
            if not bk:
                continue                               # a reversed edge is not urgent: wait for a fresh book
            # close exactly the sleeve's own holding (its fills ledger), not the venue's net position: other sleeves trade the
            # same legs (the MM and lag-follow can be net short where this sleeve is long), every sleeve keeps its own ledger,
            # and mm_positions subtracts this one's, so closing the ledger quantity keeps all books consistent. Clamping to
            # the venue's net (sim cheapfav, 24 Oct: ledger +174 YES, venue -1,426 after a lag-follow short) blocks the exit.
            n = int(abs(q))
            if q > 0 and bk.get("bids") and n >= 1 and bk["bids"][0]["price"] - byex[ex]["fair"] >= CF.REVERSE - 1e-9:
                side, px = "no", round(1 - bk["bids"][0]["price"], 3)
            elif q < 0 and bk.get("asks") and n >= 1 and byex[ex]["fair"] - bk["asks"][0]["price"] >= CF.REVERSE - 1e-9:
                side, px = "yes", round(bk["asks"][0]["price"], 3)
            else:
                continue
            try:
                resp = self.api.place_order(ex, side, "buy", n, px, expiration=expiry_in(60), key=self.api.new_key("cfx"))
                self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, n, "cheapfav_exit", response=resp)
                log.info("CHEAPFAV exit %s %s x%d @%.3f (%s)", ex, side, n, px, why)
            except ApiError as e:
                log.warning("cheapfav exit %s: %s", ex, e)
        Bot.cheapfav_rotation_exit(self, cf, now, exited, resting, prices)
        rot_start = cfg_ts(getattr(self, "cfg", None), "cheapfav_rotation_exit_start")
        if rot_start is not None and now >= rot_start:
            return                                   # the rotation is being sold: no new buys
        if killed or not dir_summary.get("polled") or now > cf.last_entry or not positions or strategy_paused(self, "cheapfav"):
            return                                   # no positions (read failed): every holding guard would be blind
        gap_legs = set((self.store.get_param("gap_legs", {}) or {}) if getattr(self, "gap", None) is not None else ())
        gap_races = {self.universe.by_exchange[e][0] for e in gap_legs if e in self.universe.by_exchange}
        race_legs = {r.name: list(r.exchange_ids) for r in self.universe.races.values()}
        ch = getattr(self, "churn", None)               # races the high-frequency arb holds sets in: its ledger books what
        skip = getattr(self, "cheapfav_skip", None) or {}
        # the catch-up sweep has priority: never trade a race where it holds a position or an entry context
        cu_ex = set(getattr(self, "catchup_open", None) or {}) | set(getattr(getattr(self, "catchup", None), "ctx", None) or {})
        cu_races = {self.universe.by_exchange[e][0] for e in cu_ex if e in self.universe.by_exchange}
        cands = [c for c in cf.candidates(rows, positions, race_legs) if c["exchange_id"] not in resting   # the venue shows,
                 and not (ch is not None and ch.held(c["race"])) and skip.get(c["exchange_id"], 0) <= now   # keep them apart
                 and c["race"] not in cu_races and c["race"] not in shock_races_of(self)   # nor shock's races
                 and c["race"] not in punt_races_of(self)                                 # nor the punt's
                 and c["exchange_id"] not in gap_legs and c["race"] not in gap_races]     # nor the gap pilot's legs / races
        if not cands:
            return
        touch = {}
        for c in cands:
            bk = self.fresh_book(c["exchange_id"], 10.0)
            if not bk:
                continue
            if c["side"] == "yes" and bk.get("asks"):
                touch[c["exchange_id"]] = (bk["asks"][0]["price"], bk["asks"][0]["quantity"])
            elif c["side"] == "no" and bk.get("bids"):
                touch[c["exchange_id"]] = (round(1 - bk["bids"][0]["price"], 3), bk["bids"][0]["quantity"])
        # the book's net D from a positions read made now, under the lock: the catch-up sweep, the direction sleeve and the
        # scanner may have traded since the cycle started (review 24 Oct: a stale snapshot let the book pass 5,000)
        fr = getattr(self, "fresh_positions", None)
        if callable(fr):
            fresh = fr()
            if fresh is None:
                return                                   # read failed: no entries this cycle
            positions = fresh
        expo = risk.exposure(self.universe, positions, prices)
        # unfilled entry orders of the directional sleeves, one-sided like ours: placed in the last 330 s (the longest TTL,
        # the inside quoter's 300 s) or still resting at the venue (tilt orders can rest for hours); review 24 Oct
        resting_ids = {str(o.get("id")) for lst in (getattr(self, "open_orders_cache", None) or {}).values() for o in lst}
        others = [dict(r) for r in self.store.rows("SELECT order_id, exchange_id, side, quantity, price, placed_at FROM orders WHERE purpose IN "
                                                   "('catchup','direction','tilt','inside','shock') AND (placed_at > ? OR status='open')", (now - 330,))]
        others = [o for o in others if float(o.get("placed_at") or 0) > now - 330 or str(o.get("order_id")) in resting_ids]
        o_filled: dict = {}
        if others:
            for f in self.store.rows("SELECT order_id, quantity FROM fills WHERE purpose IN ('catchup','direction','tilt','inside','shock') "
                                     "AND ingested_at > ?", (now - 6 * 3600,)):
                o_filled[str(f["order_id"])] = o_filled.get(str(f["order_id"]), 0.0) + abs(float(f["quantity"]))
        pend_d, pend_r = cf_pend[1], cf_pend[2]
        for o in others:
            left = float(o["quantity"]) - o_filled.get(str(o.get("order_id")), 0.0)
            d = risk.adds(self.universe, str(o["exchange_id"]), o["side"])
            if left >= 1 and d in ("D", "R"):
                n = left * float(o["price"])
                pend_d, pend_r = (pend_d + n, pend_r) if d == "D" else (pend_d, pend_r + n)
        # the punt's timed exit adds D back: a D add is judged on the book without the punt's legs, so it cannot use that room (31 Oct)
        pl = self.punt_legs() if callable(getattr(self, "punt_legs", None)) else set()
        nd_ex = risk.exposure(self.universe, {e: q for e, q in positions.items() if str(e) not in pl}, prices).net_d_notional if pl else None
        orders = cf.entries(cands, held, deployed, spendable_cash(self), now, touch, positions, expo.net_d_notional,
                            cap=self.cfg.mm.portfolio_max_partisan_notional, pend=(pend_d, pend_r), own=cf_pend, net_d_ex=nd_ex,
                            blocked=set(getattr(getattr(self, "mm", None), "blocked", None) or set()))   # the bot's block this cycle
        if getattr(self.cfg, "raise_enabled", False):  # what it would buy with unlimited cash: the shortfall is a cash need
            free = cf.entries(cands, held, deployed, 1e12, now, touch, positions, expo.net_d_notional,
                              cap=self.cfg.mm.portfolio_max_partisan_notional, pend=(pend_d, pend_r), own=cf_pend, net_d_ex=nd_ex,
                              blocked=set(getattr(getattr(self, "mm", None), "blocked", None) or set()))
            want = sum(o["notional"] for o in free)
            if want > sum(o["notional"] for o in orders) + 1:
                self.post_cash_need("cheapfav", want - max(0.0, spendable_cash(self) - cf.cash_floor), f"{len(free)} entries want {want:.0f}")
        for o in orders:
            try:
                resp = self.api.place_order(o["exchange_id"], o["side"], "buy", o["qty"], o["price"], expiration=expiry_in(60), key=self.api.new_key("cf"))
                self.store.record_order(resp.get("orderId"), o["exchange_id"], None, o["side"], "buy", o["price"], o["qty"], "cheapfav", response=resp)
                if resp.get("orderId") is None:          # its fill cannot be attributed: never buy this leg again blind
                    self.cheapfav_skip = dict(getattr(self, "cheapfav_skip", None) or {}, **{o["exchange_id"]: now + 3600})
                    log.error("CHEAPFAV entry %s: no orderId in the response; leg skipped for 1 h", o["exchange_id"])
                self.cash -= o["notional"]
                log.info("CHEAPFAV enter %s %s (%s %s) x%d @%.3f: fair %.3f, edge %.1fc", o["race"], o["win_party"], o["exchange_id"], o["side"],
                         o["qty"], o["price"], o["p_win"], 100 * (o["p_win"] - o["price"]))
            except ApiError as e:
                log.warning("cheapfav entry %s: %s", o["exchange_id"], e)
    # ------------------------------------------------------------------ gap-trade pilot (proposals/gap_pilot.py)
    def gap_cycle(self, dir_summary: dict, positions: dict, prices: dict, killed: bool):
        """Tiny gap-trade pilot: exits (gap closed, reversed, stop, time stop, hard exit) then entries on a fresh directional
        poll. Orders carry the cheap-favourites purposes (cheapfav / cheapfav_exit), so the fills ledger, the race protections
        and the wind-down exemption are the sleeve's own; the legs the pilot opened are listed in the param gap_legs."""
        gp = self.gap
        if gp is None or self.dry_run or self.store is None:
            return
        now = time.time()
        rows = getattr(self.direction, "rows", None) or []
        byex = {str(r["exchange_id"]): r for r in rows if r.get("exchange_id")}
        lock = getattr(self, "trade_lock", None)
        if lock is None:
            self._gap_trade(gp, now, rows, byex, dir_summary, positions, prices, killed)
            return
        with lock:
            self._gap_trade(gp, now, rows, byex, dir_summary, positions, prices, killed)

    def _gap_trade(self, gp, now, rows, byex, dir_summary, positions, prices, killed):
        positions = getattr(self, "positions_snapshot", None) or positions
        fills, race_of, open_pos, held, deployed_all = self.cheapfav_refresh()
        _op, cost_ex, _by, _tot = CF.ledger(fills, race_of)
        legs = {str(k): float(v) for k, v in (self.store.get_param("gap_legs", {}) or {}).items()}
        legs = {ex: ts for ex, ts in legs.items() if ex in open_pos or ts > now - 6 * 3600}   # a late fill ingest must not orphan a leg
        filled_by_order: dict = {}
        for f in fills:
            filled_by_order[str(f.get("order_id"))] = filled_by_order.get(str(f.get("order_id")), 0.0) + abs(float(f["quantity"]))
        recent = [dict(r) for r in self.store.rows("SELECT order_id, exchange_id, side, price, quantity, purpose FROM orders "
                                                   "WHERE purpose IN ('cheapfav','cheapfav_exit') AND placed_at > ?", (now - 180,))]
        busy = {str(o["exchange_id"]) for o in recent}                 # one order per leg in flight (60 s TTL + ingest lag)
        cf_ids = {str(r["order_id"]) for r in self.store.rows("SELECT order_id FROM orders WHERE purpose IN ('cheapfav','cheapfav_exit') "
                                                              "AND placed_at > ?", (now - 6 * 3600,))}
        resting_ids = {str(o.get("id")) for lst in (getattr(self, "open_orders_cache", None) or {}).values() for o in lst}
        busy |= {str(ex_o) for ex_o, lst in (getattr(self, "open_orders_cache", None) or {}).items() if any(str(o.get("id")) in cf_ids for o in lst)}
        exited_lately = {str(r["exchange_id"]) for r in self.store.rows("SELECT exchange_id FROM orders WHERE purpose='cheapfav_exit' "
                                                                       "AND placed_at > ?", (now - 900,))}     # one exit per leg per 15 min, filled or not (fills ingest up to ~4.5 min late)
        unknown_all = {str(k): float(v) for k, v in (self.store.get_param("gap_exit_unknown", {}) or {}).items()}
        unknown_exit = {k: v for k, v in unknown_all.items() if k in open_pos}
        if len(unknown_exit) < len(unknown_all):
            self.store.set_param("gap_exit_unknown", unknown_exit)      # the ledger shows the leg closed: the block ends for good
            # (failure-path test 5: a stale mark blocked the exit of the same leg bought again later)
        tries = {str(k): float(v) for k, v in (self.store.get_param("gap_exit_try", {}) or {}).items() if float(v) > now - 900}
        exited_lately |= set(tries)                                    # an attempt whose outcome we never saw (timeout) counts too
        cool = {str(k): float(v) for k, v in (self.store.get_param("gap_exited", {}) or {}).items() if float(v) > now - 24 * 3600}
        # the latest exit order per leg in the last 45 min (10x the usual fills-ingest lag) that has NO ingested fill yet:
        # the venue position decides whether it filled (review 29 Oct: a 6 h wait on the venue sign alone stalled legs the
        # market maker was short on)
        recent_exit = {}
        for r in self.store.rows("SELECT exchange_id, order_id, placed_at FROM orders WHERE purpose='cheapfav_exit' AND placed_at > ? "
                                 "ORDER BY placed_at", (now - 2700,)):
            recent_exit[str(r["exchange_id"])] = (float(r["placed_at"]), str(r["order_id"]))
        recent_exit = {ex: t for ex, (t, oid) in recent_exit.items() if filled_by_order.get(oid, 0.0) < 1}
        for r in self.store.rows("SELECT exchange_id, MAX(placed_at) AS t FROM orders WHERE purpose='cheapfav_exit' AND placed_at > ? "
                                 "GROUP BY exchange_id", (now - 24 * 3600,)):          # the sleeve's own reversal exits of gap legs count too
            if str(r["exchange_id"]) in legs:
                cool[str(r["exchange_id"])] = max(cool.get(str(r["exchange_id"]), 0.0), float(r["t"]))
        blind = {str(k): _blind_rec(v) for k, v in (self.store.get_param("gap_blind", {}) or {}).items()}   # fills nobody can attribute
        cost = {ex: cost_ex.get(ex, 0.0) for ex in legs}
        own_net, own_pd, own_pr = 0.0, 0.0, 0.0
        for ex, b in blind.items():                                    # an entry whose outcome is unknown still counts against the caps
            cost[ex] = cost.get(ex, 0.0) + b["n"]
            if b["win"] == "D":
                own_pd += b["n"]                                        # and against the own net-D band, one-sided like pending
            elif b["win"] == "R":
                own_pr += b["n"]
            else:
                own_pd += b["n"]                                        # direction unknown (old record): count it against both
                own_pr += b["n"]
        for ex, q in open_pos.items():
            if ex in legs and ex in race_of:
                win = race_of[ex][1] if q > 0 else ("R" if race_of[ex][1] == "D" else "D")
                own_net += cost_ex.get(ex, 0.0) * (1 if win == "D" else -1)
        for o in recent:                                               # unfilled entries count as held (cost and net D)
            ex = str(o["exchange_id"])
            if o["purpose"] != "cheapfav" or ex not in legs or ex not in race_of:
                continue
            left = float(o["quantity"]) - filled_by_order.get(str(o.get("order_id")), 0.0)
            if left >= 1:
                n = left * float(o["price"])
                cost[ex] = cost.get(ex, 0.0) + n
                win = race_of[ex][1] if o["side"] == "yes" else ("R" if race_of[ex][1] == "D" else "D")
                if win == "D":
                    own_pd += n                                         # one-sided, like cheapfav: an expired R order never makes room for D
                else:
                    own_pr += n
        own = own_levels(self)
        for ex, q in open_pos.items():                                 # ---- exits
            if ex not in legs or ex in busy or ex in exited_lately or ex in unknown_exit or strategy_paused(self, "exit:gap"):
                continue
            bk = self.fresh_book(ex, 10.0)
            bk = own.net(ex, bk) if bk else None                      # our own resting quotes are not the market
            if not bk:
                continue
            n = int(abs(q))
            if n < 1:
                continue
            vp = float((positions or {}).get(ex, 0.0))
            if ex in recent_exit:
                # an exit went out on this leg lately and none of its fills is ingested yet: if the venue no longer holds our
                # side, it most likely filled and the ingest is behind, so a second exit would flip the leg through zero
                # (failure-path test 10); if the venue still holds some, never sell more than it shows (a partial fill)
                if (vp <= 0 if q > 0 else vp >= 0):
                    if now - recent_exit[ex] < 90:          # once per exit order, not every cycle
                        log.warning("GAP exit %s waits: ledger %+.0f, venue %+.0f, exit sent %.0f s ago, no fill ingested", ex, q, vp, now - recent_exit[ex])
                    continue
                n = min(n, int(abs(vp)))
                if n < 1:
                    continue
            why = gp.exit_reason(ex, q, byex.get(ex), bk, cost_ex.get(ex, 0.0) / abs(q), legs[ex], now)
            if not why:
                continue
            if q > 0 and bk.get("bids"):
                side, px = "no", round(1 - bk["bids"][0]["price"], 3)
            elif q < 0 and bk.get("asks"):
                side, px = "yes", round(bk["asks"][0]["price"], 3)
            else:
                continue
            tries[ex] = now
            self.store.set_param("gap_exit_try", tries)                 # stamped before the call: a lost response must not be re-sent at once
            busy.add(ex)
            cool[ex] = now                                              # no re-entry on a leg the pilot is leaving (24 h)
            self.store.set_param("gap_exited", cool)
            if why.startswith("stop"):                                  # three stopped legs end the pilot's entries
                self.store.set_param("gap_stops", sorted(set(self.store.get_param("gap_stops", []) or []) | {ex}))
            try:
                ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")   # fresh: writes can take 12 s each
                resp = self.api.place_order(ex, side, "buy", n, px, expiration=ttl, key=self.api.new_key("gpx"))
                self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, n, "cheapfav_exit", response=resp)
                if resp.get("orderId") is None:
                    raise RuntimeError("no orderId in the response")
                log.info("GAP exit %s %s x%d @%.3f (%s)", ex, side, n, px, why)
            except Exception as e:                                      # ApiError, or a timeout / connection error escaping the client
                log.warning("gap exit %s: %s", ex, e)
                if not (isinstance(e, ApiError) and 400 <= int(e.status) < 500 and int(e.status) not in (408, 409, 429)):
                    # outcome unknown: the fill may sit in the fills table without a purpose, so the ledger cannot tell. Never
                    # re-send by itself (a second exit would flip the leg through zero); the block clears once the ledger shows
                    # the leg closed (review 28 Oct)
                    unknown_exit[ex] = now
                    self.store.set_param("gap_exit_unknown", unknown_exit)
                    log.error("GAP exit %s: outcome unknown (%s); no automatic re-send, needs a look", ex, e)
        self.store.set_param("gap_legs", legs)
        if killed or not dir_summary.get("polled") or now > gp.last_entry or not positions or strategy_paused(self, "gap"):
            return
        if len(self.store.get_param("gap_stops", []) or []) >= 3:
            return                                                      # three stop-outs: the pilot's own kill switch
        if bool(getattr(self, "shock_active", lambda: False)()) or spendable_cash(self) < 0:
            return                                                      # a shock is open, or the reserve is not whole: no new positions
        # ---- entries
        race_legs = {r.name: list(r.exchange_ids) for r in self.universe.races.values()}
        ch = getattr(self, "churn", None)
        cu_ex = set(getattr(self, "catchup_open", None) or {}) | set(getattr(getattr(self, "catchup", None), "ctx", None) or {})
        cu_races = {self.universe.by_exchange[e][0] for e in cu_ex if e in self.universe.by_exchange}
        avoid = {ex for ex in open_pos if ex not in legs}              # the cheap-favourites sleeve's own legs
        avoid |= {str(o["exchange_id"]) for o in recent if o["purpose"] == "cheapfav" and str(o["exchange_id"]) not in legs}   # and its fresh entries
        owned = Bot.wind_down_owned(self, now)
        cands = [c for c in gp.candidates(rows, positions, race_legs, avoid, cooldown_ex=set(cool) | set(unknown_exit), own_ex=set(legs)) if c["exchange_id"] not in busy
                 and c["race"] not in owned and not Bot.exit_locked(self, c["race"], "gap", now=now)
                 and not (ch is not None and ch.held(c["race"])) and c["race"] not in cu_races
                 and c["race"] not in shock_races_of(self) and c["race"] not in punt_races_of(self)]
        if not cands:
            return
        levels = {}
        for c in cands:
            bk = self.fresh_book(c["exchange_id"], 10.0)
            bk = own.net(c["exchange_id"], bk) if bk else None
            if not bk:
                continue
            if c["side"] == "yes" and bk.get("asks"):
                levels[c["exchange_id"]] = [(a["price"], a["quantity"]) for a in bk["asks"][:6]]
            elif c["side"] == "no" and bk.get("bids"):
                levels[c["exchange_id"]] = [(round(1 - b["price"], 3), b["quantity"]) for b in bk["bids"][:6]]
        fr = getattr(self, "fresh_positions", None)
        if callable(fr):
            fresh = fr()
            if fresh is None:
                return
            positions = fresh
        expo = risk.exposure(self.universe, positions, prices)
        # unfilled orders of every entry sleeve in the last 330 s, one-sided and counted whole (fills ignored: only tighter)
        pend_d = pend_r = 0.0
        for o in self.store.rows("SELECT order_id, exchange_id, side, quantity, price, placed_at FROM orders WHERE purpose IN "
                                 "('cheapfav','catchup','direction','tilt','inside','shock') AND (placed_at > ? OR status='open')", (now - 330,)):
            if float(o["placed_at"] or 0) <= now - 330 and str(o["order_id"]) not in resting_ids:
                continue                                                # an old 'open' row the venue no longer shows
            d = risk.adds(self.universe, str(o["exchange_id"]), o["side"])
            n = float(o["quantity"]) * float(o["price"])
            if d == "D":
                pend_d += n
            elif d == "R":
                pend_r += n
        pend_cf = sum(max(0.0, float(o["quantity"]) - filled_by_order.get(str(o.get("order_id")), 0.0)) * float(o["price"])
                      for o in recent if o["purpose"] == "cheapfav")
        room_all = self.cfg.cheapfav_budget - deployed_all - pend_cf        # the 15k stays shared across favourites and the pilot
        orders = gp.entries(cands, levels, cost, sum(cost.values()), min(spendable_cash(self) - max(self.cfg.cheapfav_cash_floor, getattr(self.cfg, "churn_cash_floor", 0.0) or 0.0), room_all),
                            now, positions, expo.net_d_notional, own_net, cap=self.cfg.mm.portfolio_max_partisan_notional,
                            blocked=set(getattr(getattr(self, "mm", None), "blocked", None) or set()), pend=(pend_d, pend_r),
                            own_pend=(own_pd, own_pr))
        for o in orders:
            try:
                legs.setdefault(o["exchange_id"], now)
                self.store.set_param("gap_legs", legs)                  # the label first: a leg without it would be held to the call
                ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")   # fresh: writes can take 12 s each
                resp = self.api.place_order(o["exchange_id"], o["side"], "buy", o["qty"], o["price"], expiration=ttl, key=self.api.new_key("gp"))
                self.store.record_order(resp.get("orderId"), o["exchange_id"], None, o["side"], "buy", o["price"], o["qty"], "cheapfav", response=resp)
                if resp.get("orderId") is None:                         # its fill cannot be attributed: the leg is blind
                    self._gap_blind(o, now, "no orderId in the response")
                self.cash -= o["notional"]
                log.info("GAP enter %s %s (%s %s) x%d limit %.3f avg %.3f: fair %.3f, edge %.1fc", o["race"], o["win_party"], o["exchange_id"],
                         o["side"], o["qty"], o["price"], o["avg_price"], o["p_win"], 100 * (o["p_win"] - o["avg_price"]))
            except Exception as e:                                      # ApiError, or a timeout / connection error escaping the client
                log.warning("gap entry %s: %s", o["exchange_id"], e)
                if isinstance(e, ApiError) and 400 <= int(e.status) < 500 and int(e.status) not in (408, 409, 429):
                    cool2 = {str(k): float(v) for k, v in (self.store.get_param("gap_exited", {}) or {}).items()}
                    cool2[str(o["exchange_id"])] = now                  # a definite reject: nothing filled, but no hammering the leg
                    self.store.set_param("gap_exited", cool2)
                else:
                    self._gap_blind(o, now, f"outcome unknown ({e})")  # a timeout / 5xx may have filled: count it, no re-entry

    def _gap_blind(self, o: dict, now: float, why: str):
        """An entry whose fill the ledger may never see: its notional counts against the pilot's caps for good and the leg is on
        the 24 h cooldown (review 28 Oct: otherwise the pilot re-buys the leg every few minutes past its caps)."""
        ex = str(o["exchange_id"])
        blind = {str(k): _blind_rec(v) for k, v in (self.store.get_param("gap_blind", {}) or {}).items()}
        old = blind.get(ex, {"n": 0.0, "win": o.get("win_party")})
        blind[ex] = {"n": old["n"] + float(o["notional"]), "win": old["win"] if old["win"] == o.get("win_party") else None}
        self.store.set_param("gap_blind", blind)
        cool = {str(k): float(v) for k, v in (self.store.get_param("gap_exited", {}) or {}).items()}
        cool[ex] = now
        self.store.set_param("gap_exited", cool)
        log.error("GAP entry %s: %s; %.0f counted as deployed, leg on cooldown", ex, why, o["notional"])

    # ------------------------------------------------------------------ shock mode (proposals/shock_mode.py)
    # --------------------------------------------------------------- punt executor (BACKLOG 0000, research_notes/punt_plan.md)
    def punt_races(self) -> set:
        """Races no other path may touch: the punt's and election night's, and those of a live offload sale while it runs."""
        of = getattr(self, "offload", None)
        return Bot.punt_held_races(self) | (of.races() if of is not None else set())

    def punt_held_races(self) -> set:
        """Races of every leg with a punt / election fill or order (WD.HOLD_PURPOSES), plus the enabled plan's legs; read at
        most every 30 s. A failed read keeps the last answer."""
        now = time.time()
        c = getattr(self, "_punt_races_c", None)
        if c is not None and now - c[0] < 30.0:
            return c[1]
        ex = set()
        if getattr(self, "store", None) is not None:
            ph = ",".join("?" * len(WD.HOLD_PURPOSES))
            try:
                ex = {str(r["exchange_id"]) for r in self.store.rows(
                    f"SELECT DISTINCT exchange_id FROM fills WHERE purpose IN ({ph}) UNION "
                    f"SELECT DISTINCT exchange_id FROM orders WHERE purpose IN ({ph})", WD.HOLD_PURPOSES * 2)}
            except Exception as e:
                log.warning("punt races: %s", e)
                if c is not None:
                    return c[1]
        pe = getattr(self, "punt", None)
        if pe is not None:
            ex |= pe.legs()
        ro = getattr(getattr(self, "universe", None), "race_of", None)
        races = {r.name for r in (ro(e) for e in ex) if r is not None} if ro is not None else set()
        self._punt_races_c = (now, races)
        return races

    def punt_legs(self) -> set:
        u = getattr(self, "universe", None)
        races = punt_races_of(self)
        return {e for r in races for e in (u.races[r].exchange_ids if u is not None and r in u.races else ())}

    def punt_loop(self):
        """Thread: one executor tick every punt_tick_sec; never dies."""
        while not self.stop:
            t0 = time.time()
            try:
                self.punt.tick()
            except Exception as e:
                log.exception("punt tick failed: %s", e)
            time.sleep(max(1.0, self.cfg.punt_tick_sec - (time.time() - t0)))

    def offload_loop(self):
        """Thread: one executor tick every offload_tick_sec; never dies."""
        while not self.stop:
            t0 = time.time()
            try:
                self.offload.tick()
            except Exception as e:
                log.exception("offload tick failed: %s", e)
            time.sleep(max(0.5, self.cfg.offload_tick_sec - (time.time() - t0)))

    def shock_races(self) -> set:
        """Races where shock mode holds a position or has an entry in flight (its ledger plus entry contexts)."""
        return set(getattr(self, "shock_races_cache", None) or ())   # published by shock_ledger / shock_tick under the lock

    def shock_active(self) -> bool:
        """True while shock mode holds a position or has an entry in flight: the cash raise should not refill the shock
        reserve (or sell into these races, see shock_races) until this is False."""
        return bool(getattr(self, "shock_races_cache", None))

    def shock_publish_races(self):
        bx = self.universe.by_exchange
        out = {bx[ex][0] for ex in (self.shock_open or {}) if ex in bx}
        if self.shock is not None:
            out |= {c.get("race") for c in list(self.shock.ctx.values()) if c.get("race")}
        self.shock_races_cache = frozenset(out)

    def shock_ledger(self, venue_marks: Optional[dict] = None) -> tuple:
        """(pnl, open positions YES-signed) of the shock sleeve from its fills, marked at the venue's own per-share value
        when given (the kill switch's basis), else at the freshest SIG mid."""
        fills = [dict(r) for r in self.store.rows("SELECT exchange_id, quantity, price FROM fills WHERE purpose IN ('shock','shock_exit') ORDER BY filled_at")]
        marks = {}
        for ex in {f["exchange_id"] for f in fills}:
            if venue_marks and venue_marks.get(ex) is not None:
                marks[ex] = venue_marks[ex]
                continue
            bk = self.fresh_book(ex, 60.0)
            if bk and bk.get("bids") and bk.get("asks"):
                marks[ex] = (bk["bids"][0]["price"] + bk["asks"][0]["price"]) / 2
        pnl, open_pos = pnl_from_fills(fills, marks)
        if self.shock is not None and self.shock.netted:
            for ex in list(self.shock.netted):       # the leg's ledger is flat again: the netting episode is over
                if ex not in open_pos:
                    self.shock.netted.pop(ex)
                    self.shock_netted_dirty = True
            # residuals the venue netted against other inventory are not ours
            open_pos = {ex: q - self.shock.netted.get(ex, 0.0) for ex, q in open_pos.items()}
            open_pos = {ex: q for ex, q in open_pos.items() if abs(q) >= 1}
        self.shock_open = dict(open_pos)
        self.shock_publish_races()
        return pnl, open_pos

    def shock_venue_positions(self) -> Optional[dict]:
        """{exchange: YES-signed quantity} straight from the venue, or None when the read fails. No side effects (unlike
        positions_map, which also resets cost basis, committed sets and the settlement adjustment), and the status is
        local to this call, never a flag another thread can flip (review 4)."""
        try:
            p = self.api.positions(self.slug)
        except Exception as e:
            log.warning("shock positions read: %s", e)
            return None
        return {str(x["exchangeId"]): float(x["quantity"]) for x in p.get("positions", []) if not x.get("settled")}

    def shock_claim(self, now: float):
        """Fills with no purpose on an exchange where a shock order of unknown outcome was sent (no order row, so the
        store could not attribute them) are shock's: tag them so the ledger and its exits see them (review 8)."""
        keep = []
        for ex, ts, purpose in self.shock_unknown:
            since = datetime.fromtimestamp(ts - 5, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            try:
                self.store.exec("UPDATE fills SET purpose=? WHERE exchange_id=? AND purpose IS NULL AND filled_at >= ?",
                                (purpose, ex, since))
            except Exception as e:
                log.warning("shock claim %s: %s", ex, e)
            if now - ts < 900:                       # an order lives <= 600 s: keep claiming for 15 min
                keep.append((ex, ts, purpose))
        self.shock_unknown = keep

    def shock_ingest(self) -> bool:
        """Pull the newest fills into the store (like ingest_fills); True only when every page read succeeded."""
        cursor, pages = None, 0
        while pages < 5:
            try:
                page = self.api.fills(self.slug, 200, cursor)
            except Exception as e:
                log.warning("shock fills read: %s", e)
                return False
            data = page.get("data", [])
            n = self.store.ingest_fills(data)
            pages += 1
            if n < len(data) or not (page.get("pagination") or {}).get("hasMore"):
                if self.shock_unknown:
                    self.shock_claim(time.time())
                return True
            cursor = page["pagination"]["nextCursor"]
        return True

    def shock_rows(self) -> list:
        """A fast two-venue poll for shock mode: the directional poller's batched Polymarket + Kalshi fetch, priced
        against the freshest SIG books (no storing, no directional signals)."""
        prices = {}
        for race in self.universe.races.values():
            for leg in race.legs.values():
                bk = self.fresh_book(leg.exchange_id, 300.0)   # a lagging SIG book is quiet: detection reads it up to
                if bk:                                           # 5 min old (healthy feed, no resync); orders re-read REST
                    prices[leg.exchange_id] = {"bestBid": bk["bids"][0]["price"] if bk.get("bids") else None,
                                               "bestAsk": bk["asks"][0]["price"] if bk.get("asks") else None}
        dr = self.direction
        rows = list(getattr(dr, "rows", None) or [])
        if rows and time.time() - float(rows[0].get("ts") or 0) < 15:
            return rows                              # the directional poll just ran: no second fetch (Kalshi rate budget)
        with dr.lock:                                # the map only; the fetch runs outside the lock so a venue's
            dr._ensure_map()                         # 429 backoff never stalls the main cycle's directional poll
        pmq, kq = dr.fetch_outside(pm=getattr(self, "shock_pm", None), ks=getattr(self, "shock_ks", None))
        if not pmq or not kq:
            return []                                # one venue down: no both-venue signal, no exits on stale outside
        return dr.compute(prices, pmq, kq, time.time())

    def shock_fast_mids(self) -> dict:
        """{exchange_id: Polymarket midpoint sum} from one batched /midpoints call (no depth, no Kalshi: a trigger only)."""
        dr = self.direction
        tok_of = {}
        for race in self.universe.races.values():
            e = (dr.map.get("races") or {}).get(race.name) or {}
            for party, ls in ((e.get("polymarket") or {}).get("legs") or {}).items():
                leg = race.legs.get(party)
                if leg is not None:
                    tok_of[leg.exchange_id] = [l["token"] for l in ls if l.get("token")]
        toks = sorted({t for ts in tok_of.values() for t in ts})
        if not toks:
            return {}
        mids = (self.shock_pm or dr.pm).midpoints(toks)
        return {ex: sum(mids[t] for t in ts) for ex, ts in tok_of.items() if ts and all(t in mids for t in ts)}

    def shock_loop(self):
        """Thread: one shock_tick every shock_poll_sec; never dies."""
        while not self.stop:
            t0 = time.time()
            try:
                self.shock_tick()
            except Exception as e:
                log.exception("shock tick failed: %s", e)
            time.sleep(max(1.0, self.cfg.shock_poll_sec - (time.time() - t0)))

    def shock_tick(self, rows: Optional[list] = None):
        """Shock mode: exits first (60-min stop, convergence, outside give-back or disagreement, stop, 3 Nov flatten), then
        pyramid adds and new entries. Exempt from the partisan cap (Sizhe 24 Oct); its positions still count in net D for
        every other sleeve (risk.exposure reads venue positions). One order per exchange in flight (60 s TTL)."""
        sk = self.shock
        if sk is None or self.dry_run or self.store is None:
            return
        now = time.time()
        fresh = rows is not None
        if rows is None:
            if now - self.shock_last_poll < self.cfg.shock_poll_sec - 0.5:
                return
            self.shock_last_poll = now
            full = now - self.shock_last_full >= self.cfg.shock_full_sec - 0.5
            if not full and now - self.shock_last_full >= SHK.FULL_MIN_GAP:
                try:                                 # fast look: one batched Polymarket /midpoints call
                    hot = self.shock_fast.update(self.shock_fast_mids(), now)
                except Exception as e:               # VenueError, map not loaded yet: wait for the next full poll
                    log.debug("shock fast check: %s", e)
                    hot = []
                if hot:
                    full = True
                    log.info("SHOCK fast detector: Polymarket jumped on %s: full poll now", hot[:4])
            if full:
                self.shock_last_full = now
                rows = self.shock_rows()
                self.shock_rows_last = rows
                fresh = True
            else:                                    # between full polls: exits use the last rows while < 45 s old
                rows = [r for r in self.shock_rows_last if now - float(r.get("ts") or 0) < 45]
        if rows and fresh:
            sk.push(rows)
        byex = {str(r["exchange_id"]): r for r in rows or [] if r.get("exchange_id")}
        changed = False
        with self.trade_lock:
            if self.shock_unknown:
                self.shock_claim(now)                # fills the main cycle ingested for our unknown-outcome orders
            pnl, open_pos = self.shock_ledger()
            if getattr(self, "shock_netted_dirty", False):
                self.shock_netted_dirty = False
                changed = True
            if not sk.killed and sk.check_kill(pnl):
                changed = True
                log.warning("SHOCK: sleeve P&L %.1f at or below %.0f: stopped; open positions are being closed", pnl, sk.kill_pnl)
            # in flight = sent within its own TTL (+5 s): entries 60 s, taker exits 15 s (the venue expires them; the
            # open-orders cache is up to a cycle old and kept exits blocked for 90 s). Maker exits live in the context.
            resting = {str(r["exchange_id"]) for r in self.store.rows(
                "SELECT exchange_id FROM orders WHERE purpose='shock' AND status='open' AND placed_at > ?", (now - 65,))}
            resting |= {str(r["exchange_id"]) for r in self.store.rows(
                "SELECT exchange_id FROM orders WHERE purpose='shock_exit' AND status='open' AND placed_at > ?", (now - 20,))
                if not any((c.get("maker") or {}).get("ex") == str(r["exchange_id"]) for c in sk.ctx.values())}
            # each shock order's expiry is set at its own write (expiry_in), never once per tick: with slow writes a
            # batch-wide expiry expired every order after the first (sim slow_writes, 28 Oct)

            def venue_qty(ex: str, q: float) -> Optional[int]:
                """Shares we may close: the ledger's q, never more than the venue shows held on that side (the fills
                ledger only moves when the main cycle ingests; review 24 Oct: stale ledger -> re-sent exits flipped)."""
                if now - self.shock_vpos[0] > 3.0:
                    m = self.shock_venue_positions()     # one positions read per tick, only when an exit is due
                    self.shock_vfail = self.shock_vfail + 1 if m is None else 0
                    self.shock_vpos = (now, m)           # a failure is cached too: one count per read, not per leg
                if self.shock_vpos[1] is None:
                    if now >= SHK.FLATTEN_AT and self.shock_vfail >= 6:
                        return int(abs(q))               # past the flatten with positions unreadable for ~30 s: the ledger
                    return None                          # read failed: unknown, never "flat" (review 3)
                vpos = self.shock_vpos[1].get(ex, 0.0)
                return int(min(abs(q), abs(vpos) if vpos * q > 0 else 0.0))
            # ---- exits
            maker_on = bool(getattr(self.cfg, "shock_exit_maker", False))
            for ex, q in open_pos.items():
                c = sk.ctx.get(ex) or {}
                why = sk.exit_reason(ex, byex.get(ex), now, SHK.CONVERGE_WITH_MAKER if maker_on else SHK.CONVERGE)
                mk = c.get("maker")                       # R3: our resting maker exit {id, ts, qty}
                if mk and (why or now - mk["ts"] > 540 or abs(q) != mk["qty"]):
                    # an exit rule fired, the position changed, or it is near its TTL: cancel before forgetting it (review
                    # 3: two live makers). A failed cancel keeps it and retries every tick; no taker exit while it may rest
                    # (review 4). It is dead once cancelled, once the venue says it is unknown/closed (4xx), or past its
                    # 600-s TTL; it may have filled, so taker exits wait ~100 s for the ledger, and the venue clamp holds.
                    dead = now - mk["ts"] > 610                # past its 600-s TTL the venue has expired it
                    if not dead and mk.get("id") is None and why:   # outcome unknown (no id to cancel) and an exit is due:
                        try:                                        # clear the leg, then hold for the ledger and the clamp
                            self.api.cancel_all(exchange_id=ex)
                            dead = True
                            self.shock_hold[ex] = now + 100.0; self.shock_exit_sent[ex] = time.time()   # one clock with shock_ingest_ts (review 29 Oct)
                        except Exception as e:
                            log.warning("shock maker cancel-all %s: %s (retrying)", ex, e)
                    oo_ts, oo_ok = getattr(self, "open_orders_read", (0.0, False))
                    if not dead and oo_ok and mk.get("id") is not None and oo_ts > mk["ts"] + 5:   # a read begun after it: if
                        oo = (getattr(self, "open_orders_cache", None) or {}).get(ex, [])   # not listed, it is gone (filled,
                        if not any(str(o.get("id")) == str(mk["id"]) for o in oo):        # or a global cancel-all)
                            dead = True
                            self.shock_hold[ex] = now + 100.0; self.shock_exit_sent[ex] = time.time()
                    if not dead and mk.get("id") is not None:
                        try:
                            self.api.cancel_order(mk["id"])
                            self.store.mark_orders([mk["id"]], "cancelled")
                            dead = True
                            self.shock_exit_sent[ex] = time.time()          # it may have part-filled: fresh fills before the taker
                        except ApiError as e:
                            if int(getattr(e, "status", 0) or 0) == 404:
                                dead = True              # not found: already filled or closed (any other error: retry;
                                self.shock_hold[ex] = now + 100.0; self.shock_exit_sent[ex] = time.time()    # 429/401/409... do not prove it is gone; review 5)
                            log.warning("shock maker cancel %s: %s (%s)", ex, e, "gone" if dead else "retrying")
                        except Exception as e:
                            log.warning("shock maker cancel %s: %s (retrying)", ex, e)
                    if dead:
                        c.pop("maker", None)              # re-placed below on a later tick if still wanted
                        changed = True
                    continue
                if ex in resting or self.shock_hold.get(ex, 0.0) > now:
                    continue
                if not why:
                    if maker_on and not mk and c:
                        mx = sk.maker_exit(ex, q)
                        n = venue_qty(ex, q) if mx else 0
                        if mx and n is not None and n >= 1:
                            mttl = (datetime.now(timezone.utc) + timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
                            resp, unknown = None, False
                            self.shock_exit_sent[ex] = time.time()
                            try:
                                resp = self.api.place_order(ex, mx[0], "buy", n, mx[1], expiration=mttl, key=self.api.new_key("shm"))
                            except ApiError as e:
                                st_ = int(getattr(e, "status", 0) or 0)
                                unknown = st_ >= 500 or st_ == 409                    # may have been accepted
                                log.warning("shock maker exit %s: %s%s", ex, e, " (outcome unknown)" if unknown else "")
                                if not unknown:
                                    self.shock_hold[ex] = now + 60.0              # a clean reject: no retry storm
                            except Exception as e:
                                unknown = True
                                log.error("SHOCK maker exit %s: outcome unknown (%s)", ex, e)
                            if resp is not None:
                                self.store.record_order(resp.get("orderId"), ex, None, mx[0], "buy", mx[1], n, "shock_exit", response=resp)
                                unknown = not resp.get("orderId")
                            if unknown and not (resp or {}).get("orderId"):
                                self.shock_unknown.append((ex, now, "shock_exit"))
                            if resp is not None or unknown:  # a live (or possibly live) maker: one per leg until it is gone
                                c["maker"] = {"id": (resp or {}).get("orderId"), "ts": now, "qty": abs(q), "ex": ex}
                                changed = True
                                log.info("SHOCK maker exit %s %s x%d @%.3f rests at the 70%% price%s", ex, mx[0], n, mx[1],
                                         " (outcome unknown)" if unknown else "")
                    continue
                bk = self.fresh_book(ex, 30.0)
                if not bk:
                    try:
                        bk = self.get_book(ex, 5)
                    except ApiError as e:
                        log.warning("shock exit book %s: %s", ex, e)
                        continue
                bid = bk["bids"][0]["price"] if bk.get("bids") else None
                ask = bk["asks"][0]["price"] if bk.get("asks") else None
                if (q > 0 and bid is None) or (q < 0 and ask is None):
                    log.warning("SHOCK exit %s waits: no %s in the book (%s)", ex, "bid" if q > 0 else "ask", why)
                    continue
                if self.shock_exit_sent.get(ex, -1e18) + 16.0 >= self.shock_ingest_ts:   # per leg: a pull for the other
                    # leg while this leg's 15-s exit could still fill does not cover it (review 8)
                    # an exit (or maker) went out on this leg since our last fills pull: its partial fill is not in the
                    # ledger yet, and the venue clamp alone would then size the next exit into other sleeves' shares on
                    # the same side (an arb set's NO; review 7). Pull fills and re-size from the fresh ledger.
                    t_pull = time.time()                      # taken before the pull: it covers exits sent before this instant
                    if not self.shock_ingest():
                        continue
                    self.shock_ingest_ts = t_pull             # an exit sent later (its stamp is later) pulls again
                    q = self.shock_ledger()[1].get(ex, 0.0)
                    if abs(q) < 1:
                        continue
                n = venue_qty(ex, q)
                if n is None:
                    continue                          # positions unknown: try again next tick, never net on a failed read
                if n < 1:
                    last = self.store.rows("SELECT MAX(placed_at) t FROM orders WHERE exchange_id=? AND purpose IN ('shock','shock_exit')", (ex,))
                    t_last = (last[0]["t"] if last else None) or 0.0       # sqlite3.Row: index, no .get
                    if now - float(t_last) > 180:
                        # before calling a residual netted: pull our own fills and re-read the venue, so an exit that
                        # filled but was not ingested yet is seen as a fill, not as other sleeves' inventory (review 3)
                        if not self.shock_ingest():
                            continue                         # the fills read failed: the ledger may still be stale
                        q2 = self.shock_ledger()[1].get(ex, 0.0)
                        self.shock_vpos = (0.0, {})
                        n2 = venue_qty(ex, q2) if abs(q2) >= 1 else None
                        if n2 is not None and n2 < 1 and abs(q2) >= 1:
                            sk.netted[ex] = sk.netted.get(ex, 0.0) + q2   # the venue holds nothing on our side: other
                            changed = True                                # sleeves' inventory netted it; stop closing it
                            log.warning("SHOCK %s: %+.0f ledger shares netted at the venue by other inventory; dropped", ex, q2)
                    continue                          # the venue shows it closed already: wait for the ledger
                walk = SHK.FLATTEN_WALK_TICKS if now >= SHK.FLATTEN_AT else SHK.EXIT_WALK_TICKS
                if q > 0:
                    side, px = "no", round(min(0.995, 1 - bid + walk * SHK.TICK), 3)
                else:
                    side, px = "yes", round(min(0.995, ask + walk * SHK.TICK), 3)
                t_send = time.time()                  # the exit lives 15 s from its send, not from the tick start (review 29 Oct)
                self.shock_exit_sent[ex] = t_send
                try:
                    resp = self.api.place_order(ex, side, "buy", n, px, expiration=expiry_in(15), key=self.api.new_key("shx"))
                    self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, n, "shock_exit", response=resp)
                    log.info("SHOCK exit %s %s x%d @%.3f (%s)", ex, side, n, px, why)
                except ApiError as e:
                    st_ = int(getattr(e, "status", 0) or 0)
                    log.warning("shock exit %s: %s", ex, e)
                    self.shock_hold[ex] = t_send + (20.0 if st_ >= 500 or st_ == 409 else 60.0)   # unknown: 20 s; reject: 60 s
                    if st_ >= 500 or st_ == 409:
                        self.shock_unknown.append((ex, now, "shock_exit"))
                except Exception as e:               # unknown outcome: its fills are claimed for shock (shock_claim)
                    log.error("SHOCK exit %s: outcome unknown (%s)", ex, e)
                    self.shock_hold[ex] = t_send + 20.0
                    self.shock_unknown.append((ex, now, "shock_exit"))
            for ex in list(sk.ctx):
                if ex not in open_pos and ex not in resting and now - sk.ctx[ex].get("ts", now) > 300:
                    sk.ctx.pop(ex)                   # flat and settled: forget the entry context
                    changed = True
            # ---- entries and pyramid adds
            ok = (fresh and not sk.killed and rows and now >= self.killed_until and self.feed_ok()
                  and not strategy_paused(self, "shock"))   # strategy monitor / kill file: new entries and adds only, exits go on
            if ok:
                held = {}                            # race -> entry notional SENT (filled or not) since the shock began: the
                bx = self.universe.by_exchange       # caps bind on what we committed, never on a mark
                for ex, c in sk.ctx.items():
                    if c.get("race"):
                        held[c["race"]] = held.get(c["race"], 0.0) + float(c.get("spent", 0.0))
                for ex, q in open_pos.items():       # a position without context (closing) still blocks its race's budget
                    if ex not in sk.ctx and ex in bx:
                        held[bx[ex][0]] = held.get(bx[ex][0], 0.0) + abs(q) * 0.5
                open_races = {c.get("race") for c in sk.ctx.values()} | {bx[ex][0] for ex in open_pos if ex in bx}
                todo = []
                for ex, c in list(sk.ctx.items()):
                    if ex in resting or ex not in open_pos:
                        continue
                    am = sk.add_move(ex, byex.get(ex), now)
                    if am is not None:
                        todo.append(("add", ex, c["race"], c["d"], am[1], {"full": am[0]}))
                cf_held = cheapfav_races_of(self)    # cheap favourites hold to the call: the two ledgers never share a race
                # nor a race the wind-down or the cash raise clipped in the last 30 min (shared exit lock): shock would buy
                # what they just sold (and the wind-down exempts a shock race at once, so it would stop mid-race)
                for sgl in [g for g in sk.signals(rows, now, open_races) if g["race"] not in cf_held and g["race"] not in punt_races_of(self)
                            and not Bot.exit_locked(self, g["race"], "shock", now=now)]:
                    todo.append(("new", sgl["exchange_id"], sgl["race"], sgl["d"], min(abs(sgl["move"]), sgl["gap"]), sgl))
                cleared = set()
                cash_left = float(getattr(self, "cash", 0.0) or 0.0)   # free cash incl. the shock reserve (cash raise keeps
                vnow = self.shock_venue_positions() if todo else {}   # >= shock_reserve free); one positions read per tick
                for kind, ex, race, d, m, sgl in todo:
                    if vnow is None:
                        break                                # positions unknown: no entries or adds this tick
                    vp = vnow.get(ex, 0.0)
                    if (d > 0 and vp <= -1) or (d < 0 and vp >= 1):
                        continue      # the venue holds the other side here (an arb set's NO, MM inventory): buying would net
                                      # against it and the exit clamp would then break it (review 6)
                    if ex in resting:
                        continue
                    if (kind == "new" and race not in cleared and race not in open_races
                            and now - self.shock_cleared.get(race, -1e18) > SHK.EXIT_AFTER):
                        self.shock_clear_race(race, now)     # once per shock, not on every retried entry
                        self.shock_cleared[race] = now
                        cleared.add(race)
                    try:
                        bk = self.get_book(ex, 20, max_age=10.0)   # feed if it moved in 10 s, else one REST read
                    except ApiError as e:
                        log.warning("shock book %s: %s", ex, e)
                        continue
                    if not bk or not bk.get("bids") or not bk.get("asks"):
                        continue
                    if bk["asks"][0]["price"] - bk["bids"][0]["price"] > SHK.SPREAD_MAX_TICKS * SHK.TICK + 1e-9:
                        continue
                    # re-check the lag on the authoritative book (review 4): a dropped feed topic can show SIG "lagging"
                    # after it has already moved; never buy a touch that has copied the move
                    fmid = (bk["asks"][0]["price"] + bk["bids"][0]["price"]) / 2
                    if kind == "new":
                        if (fmid - sgl["s_ref"]) * d >= SHK.SIG_FOLLOW_MAX * abs(sgl["move"]) or abs(fmid - sgl["mid"]) > 2 * SHK.TICK + 1e-9:
                            log.info("SHOCK %s %s: SIG already at %.3f on the fresh book (feed said %.3f): no entry", race, ex, fmid, sgl["mid"])
                            continue
                        f_now = sgl["f_now"]
                    else:
                        if (fmid - sk.ctx[ex]["s_ref"]) * d >= SHK.ADD_STOP_COPY * m:
                            continue
                        r_ = byex.get(ex) or {}
                        if r_.get("pm") is None or r_.get("kalshi") is None:
                            continue
                        f_now = (r_["pm"] + r_["kalshi"]) / 2
                    fgap = (f_now - fmid) * d          # the gap still open on the authoritative book
                    if fgap < SHK.GAP_MIN - 1e-9:
                        continue
                    m = min(m, fgap)                   # walk on the edge left
                    full = abs(sgl["move"]) if kind == "new" else float(sgl["full"])
                    s_ref_ = sgl["s_ref"] if kind == "new" else sk.ctx[ex]["s_ref"]
                    conv = min(full, fgap + (fmid - s_ref_) * d)   # the most SIG can copy: up to the outside, not past it
                    budget = min(sk.budget_left(race, held), max(0.0, cash_left - 50.0))   # never more than the free cash
                    side, px, q = sk.plan(d, m, bk["asks"] if d > 0 else bk["bids"], budget)
                    if not q:
                        continue
                    cost = q * max(px, SHK.SHARE_PRICE_FLOOR)   # spend counted on the same floor the shares were sized on
                    if strategy_paused(self, "shock"):
                        break                                # paused / held while this tick ran (strategy stops)
                    resp, unknown = None, False
                    try:
                        resp = self.api.place_order(ex, side, "buy", q, px, expiration=expiry_in(60), key=self.api.new_key("shk"))
                    except ApiError as e:
                        st_ = int(getattr(e, "status", 0) or 0)
                        unknown = st_ >= 500 or st_ == 409    # a 5xx / request-in-flight may have been accepted
                        log.warning("shock %s %s: %s%s", kind, ex, e, " (outcome unknown)" if unknown else "")
                        if not unknown:
                            continue
                    except Exception as e:           # timeout / connection: it may have been accepted, unrecorded
                        unknown = True
                        log.error("SHOCK %s %s: order outcome unknown (%s)", kind, ex, e)
                    if resp is not None and not resp.get("orderId"):
                        unknown = True                # accepted without an id: its fills cannot be attributed
                        log.error("SHOCK %s %s: no orderId in the response", kind, ex)
                    if resp is not None:
                        self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, q, "shock", response=resp)
                    if unknown:                       # count the budget as if it filled and stop trading the race
                        log.error("SHOCK %s %s: race blocked, budget counted", kind, ex)
                        cash_left -= q * px                  # ... and its cash as spent (review 12)
                        self.cash = float(getattr(self, "cash", 0.0) or 0.0) - q * px
                        if not (resp or {}).get("orderId"):
                            self.shock_unknown.append((ex, now, "shock"))
                        held[race] = held.get(race, 0.0) + cost
                        sk.last_shock[race] = now
                        if kind == "new":
                            sk.ctx[ex] = {"ts": now, "race": race, "d": d, "f_ref": sgl["f_ref"], "s_ref": sgl["s_ref"],
                                          "move": sgl["move"], "conv": conv, "spent": cost, "unknown": True}
                        else:
                            sk.ctx[ex]["spent"] = float(sk.ctx[ex].get("spent", 0.0)) + cost
                            sk.ctx[ex]["unknown"] = True
                        open_races.add(race)
                        changed = True
                        continue
                    held[race] = held.get(race, 0.0) + cost
                    cash_left -= q * px                  # what this order can spend, before the next order this tick
                    self.cash = float(getattr(self, "cash", 0.0) or 0.0) - q * px   # and before the next cycle's refresh:
                    # later ticks and the other sleeves (cheap favourites' floor) must not count the same cash (review 12)
                    resting.add(ex)
                    if kind == "new":
                        sk.ctx[ex] = {"ts": now, "race": race, "d": d, "f_ref": sgl["f_ref"], "s_ref": sgl["s_ref"],
                                      "move": sgl["move"], "conv": conv, "spent": cost}
                        sk.last_shock[race] = now
                        open_races.add(race)
                    else:
                        c = sk.ctx[ex]
                        c["move"] = d * max(abs(c["move"]), full)   # the stored move only rises: the add gate and the
                        c["conv"] = max(float(c.get("conv", 0.0)), conv)   # hoax exit never loosen (review 10)
                        c["spent"] = float(c.get("spent", 0.0)) + cost
                    changed = True
                    log.warning("SHOCK %s %s %s %s x%d @%.3f: outside moved %+.1f pts, gap-limited edge %.1f, walk %d ticks",
                                "enter" if kind == "new" else "add", race, ex, side, q, px, 100 * d * full, 100 * m, SHK.walk_ticks(m))
            self.shock_publish_races()
            if changed or sk.killed:
                try:
                    tmp = self.shock_state_path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(sk.to_json(), f)
                    os.replace(tmp, self.shock_state_path)
                except OSError as e:
                    log.warning("shock state: %s", e)

    def shock_clear_race(self, race: str, now: float):
        """Before the first sweep: cancel every resting order of ours on the race's legs (no self-trade, and our MM's stale
        side would be picked off) and keep the MM out of the race until the shock's time stop has passed."""
        r = self.universe.races.get(race)
        if r is None:
            return
        for ex in r.exchange_ids:
            try:
                self.api.cancel_all(exchange_id=ex)
                ids = [o["id"] for o in (getattr(self, "open_orders_cache", None) or {}).get(ex, []) if o.get("id")]
                if ids:
                    self.store.mark_orders(ids, "cancelled")
            except ApiError as e:
                log.warning("shock cancel-all %s: %s", ex, e)
        try:
            with self.store.lock:                    # one read-modify-write: the news sweeper and the MM write this key too
                pause = dict(self.store.get_param("news_pause", {}) or {})
                until = now + SHK.EXIT_AFTER + 300
                for ex in r.exchange_ids:
                    pause[ex] = max(float(pause.get(ex, 0.0)), until)
                self.store.set_param("news_pause", pause)
        except Exception as e:
            log.warning("shock MM pause %s: %s", race, e)


    def volatile_cycle(self):
        """research_notes/volatile_moves.md: after a sweep of 5+ ticks, quotes on the retrace side lost about 1 tick over the
        next hour (t -5.4), so the MM's level-1 quote on that side is paused for an hour (after an up move: the ask). The
        fade/catch-up trades had no robust edge, so they are only paper-logged to data/volatile_paper.jsonl for a re-test."""
        now = time.time()
        books = self.feed.books()
        events = list(self.move_detector.on_books(now, books))
        rows = getattr(self.direction, "rows", None) or []
        if rows:
            events += list(self.move_detector.on_outside(now, rows))
        quoted = set(self.mm.quoted)
        if not hasattr(self.mm, "retrace_pause"):
            self.mm.retrace_pause = {}           # ex -> (side to pause at level 1, until); set after 5+ tick moves
        for ev in events:
            ex = str(ev.get("exchange_id"))
            if ev.get("kind") == "sweep" and ex in quoted:
                side = "no" if (ev.get("direction") or 0) > 0 else "yes"
                if self.mm.retrace_pause.get(ex, ("", 0))[1] < now:
                    log.info("RETRACE PAUSE %s: %+.3f move, pausing level-1 %s for 1 h", ex, ev.get("size_pts") or 0.0, "ask" if side == "no" else "bid")
                self.mm.retrace_pause[ex] = (side, now + 3600)
        fair = {r["exchange_id"]: r.get("fair") for r in rows if r.get("fair") is not None}
        paper = self.volatile_paper.cycle(now, events, books, fair)
        if events or paper:
            with open(os.path.join(HERE, "data", "volatile_paper.jsonl"), "a") as f:
                for ev in events:
                    f.write(json.dumps({"ts": now, "event": dict(ev)}, default=str) + "\n")
                for o in paper:
                    f.write(json.dumps({"ts": now, "paper_order": o}, default=str) + "\n")

    # --------------------------------------------------------------- election night (research_notes/election_night.md)
    def election_filter(self, orders: list) -> list:
        """review B3: never trade a race of the enabled punt plan (every leg of it); logged once per leg, not once per poll (R2-5)."""
        pe = getattr(self, "punt", None)
        if pe is None:
            return list(orders)
        pl = {e for x in pe.legs() for r in [self.universe.race_of(x)] if r is not None for e in r.exchange_ids}
        seen = self.__dict__.setdefault("_el_skip_logged", set())
        out = []
        for o in orders:
            ex = str(o.get("exchangeId"))
            if ex in pl:
                if ex not in seen:
                    seen.add(ex)
                    log.warning("ELECTION skips %s: a punt plan race", ex)
                continue
            out.append(o)
        return out

    def election_loop(self):
        """From 18:00 ET on election day: poll Polymarket/Kalshi every election_poll_sec for the races whose polls are
        closing, let ElectionNight decide calls and moves against the feed books, and send its orders (purpose
        'election', short TTL). Fills are reported back to the sleeve from the fills table each cycle."""
        en = self.election
        start = en.cfg.day0() + en.cfg.start_et * 3600
        while not getattr(self, "stop", False):
            now = time.time()
            if now < start - 60:
                time.sleep(min(600.0, start - 60 - now))
                continue
            if now > en.cfg.day0() + en.cfg.flatten_et * 3600 + 3600 or not self.feed_ok():
                time.sleep(self.cfg.election_poll_sec)
                continue
            try:
                rows = self.election_stream.poll(en.watched(now))
                orders = en.cycle(now, rows, self.feed.books())
            except Exception as e:
                log.warning("election cycle: %s", e)
                time.sleep(self.cfg.election_poll_sec)
                continue
            for o in Bot.election_filter(self, orders):
                log.info("ELECTION %s %s %s x%s at %s: %s", o.get("kind"), o.get("exchangeId"), o.get("side"), o.get("quantity"), o.get("price"), o.get("reason"))
                if self.dry_run:
                    continue
                if o.get("kind") != "exit" and strategy_paused(self, "election"):
                    continue                           # paused (strategy stops / KILL-ENTRIES): its exits still go out
                try:
                    with self.trade_lock:
                        resp = self.api.place_order(str(o["exchangeId"]), o["side"], "buy", int(o["quantity"]), float(o["price"]),
                                                    expiration=expiry_in(en.cfg.ttl_sec), key=self.api.new_key("elec"))
                    oid = resp.get("orderId")
                    self.store.record_order(oid, str(o["exchangeId"]), None, o["side"], "buy", float(o["price"]), int(o["quantity"]), "election", response=resp)
                    if oid:
                        self.election_orders[str(oid)] = o
                except ApiError as e:
                    log.warning("election order %s failed: %s", o.get("exchangeId"), e)
            time.sleep(max(0.5, self.cfg.election_poll_sec - (time.time() - now)))

    def election_fills(self):
        if self.election is None or not getattr(self, "election_orders", None):
            return
        seen = getattr(self, "_election_seen", set())
        self._election_seen = seen
        for f in self.store.rows("SELECT id, order_id, quantity, price FROM fills WHERE order_id IS NOT NULL AND filled_at >= datetime('now','-1 day')"):
            o = self.election_orders.get(str(f["order_id"]))
            if o is not None and f["id"] not in seen:
                seen.add(f["id"])
                self.election.on_fill(o, abs(int(f["quantity"])), float(f["price"]))

    # --------------------------------------------------------------- allocator hooks
    def load_allocation(self):
        """Per-race arb caps from params/allocation.json (edge-based, written by the allocator every 12 h): applied to
        the arb engine whenever the file changes. Caps never go below the file's default_cap so a race with a brand-new
        opportunity can still be taken at small size before the next allocator run."""
        path = os.path.join(HERE, "params", "allocation.json")
        try:
            m = os.path.getmtime(path)
        except OSError:
            return
        if m == self.allocation_mtime:
            return
        self.allocation_mtime = m
        try:
            races = (json.load(open(path)).get("races") or {})
            caps = {k: float(v) for k, v in (races.get("caps") or {}).items()}
            default = float(races.get("default_cap", 2000.0))
            if caps:
                self.arb.race_caps, self.arb.race_cap_default, self.arb.race_cap_floor = caps, default, default
                top = sorted(caps.items(), key=lambda kv: -kv[1])[:5]
                log.info("allocator race caps loaded: %d races, default %.0f, top %s", len(caps), default, [(r, round(c)) for r, c in top])
        except Exception as e:
            log.warning("allocation.json: %s", e)

    def offload_report(self, cycle_id: int):
        """When a better trade is blocked for capital, exit the holding that gives up the least (forgone edge plus exit
        cost, counted once) if the new trade beats it by recycle_min_gain. Arb-set offloads go through the atomic recycle
        path; tilt/directional holdings are exited here at the touch (60 s orders, purpose 'exit'), at most 2 per check.
        Live only with config allocator.offload_live (and not in a dry run); otherwise logged."""
        if cycle_id % 5:
            return
        try:
            recs = offload_check(build_state_from_bot(self), self.cfg.arb.recycle_min_gain)
        except Exception as e:
            log.warning("offload check: %s", e)
            return
        if not recs:
            return
        live = bool(getattr(self, "offload_live", False)) and not self.dry_run
        done = 0
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        seen = getattr(self, "_offload_seen", {})
        self._offload_seen = seen
        for r in recs:
            u = r["unwind"]
            key = (u.get("race") or u.get("exchange_id"), u.get("side"), r["opportunity"].get("race") or r["opportunity"].get("exchange_id"))
            if (u["kind"] == "arb_set" or not live) and time.time() - seen.get(key, 0) < 600:
                continue                         # same recommendation within 10 min: do not repeat it every 3 s
            seen[key] = time.time()
            log.info("OFFLOAD candidate%s: exit %s %s x%d (gives up %.2f%%) to fund %s (%.2f%% net), gain %.2f%%", "" if live else " (report-only)",
                     u.get("race") or u.get("exchange_id"), u.get("side", "set"), u["qty"], 100 * u["forgone_ret"],
                     r["opportunity"].get("race") or r["opportunity"].get("exchange_id"), 100 * r["opportunity"]["ret_net"], 100 * r["gain"])
            if live and self.cfg.offload_arb_for_sleeves and u["kind"] == "arb_set" and r["opportunity"].get("kind") in ("tilt", "direction") and done < 1:
                # Guards added after the 22 Oct 19:41-20:04 churn (38 unwinds for a handful of tilt trades):
                #  1. only while cash is genuinely short: below the arb cash reserve, and below the trade's own capital need
                #  2. each opportunity is funded at most once per 30 min (the tilt candidate list refreshes once per cycle)
                #  3. the real walked exit cost must still leave recycle_min_gain (checked inside unwind_set)
                #  4. one unwind per check, and the tilt list is cleared after it until the sleeve re-evaluates
                opp = r["opportunity"]
                okey = (opp.get("kind"), opp.get("exchange_id"))
                funded = getattr(self, "_offload_funded", {})
                self._offload_funded = funded
                room = getattr(self.tilt, "free_last", 0.0) if opp.get("kind") == "tilt" else spendable_cash(self) * 0.9
                if (spendable_cash(self) >= self.cfg.arb.cash_reserve or room >= opp["capital"] or time.time() - funded.get(okey, 0) < 1800
                        or spendable_cash(self) < 0):          # the shock reserve short: tier 1 refills it, never set unwinds
                    continue
                rname = u.get("race")
                if rname in self.arb.protected():
                    continue                     # high-frequency arb's sets never fund anything (unwind_set refuses them too)
                race = self.universe.races.get(rname)
                books = {ex: self.fresh_book(ex, 10.0) for ex in race.exchange_ids} if race else {}
                if race and all(v is not None for v in books.values()):
                    q = int(min(u["qty"], math.ceil(opp["capital"] / max(u["proceeds"], 0.05))))
                    with self.trade_lock:
                        res = self.arb.unwind_set(rname, books, q, why=f"fund {opp['kind']} {opp.get('exchange_id')}",
                                                  new_ret=opp["ret_net"], min_gain=self.cfg.arb.recycle_min_gain,
                                                  refresh=self.fresh_positions)
                        if res and not res.get("dry"):
                            self.refresh_snapshot(self.unwind_deltas([res]))
                    funded[okey] = time.time()
                    if res:
                        done += 1
                        if getattr(self, "tilt", None) is not None:
                            self.tilt.unfunded = []
                        try:
                            self.cash = float(self.api.account().get("balance") or self.cash)
                        except ApiError:
                            pass
                continue
            if not live or u["kind"] == "arb_set" or done >= 1 or not u.get("order"):
                continue
            # tilt/directional exits that fund another trade: the same guards as the arb path (22 Oct churn), plus a
            # higher bar, because the exit crosses the spread and the funded trade's edge is a model estimate
            opp = r["opportunity"]
            okey = ("exit", opp.get("kind"), opp.get("exchange_id"))
            funded = getattr(self, "_offload_funded", {})
            self._offload_funded = funded
            if time.time() - funded.get(okey, 0) < 1800 or time.time() - funded.get(("held", u.get("exchange_id")), 0) < 3600:
                continue
            if r["gain"] < max(0.03, 2 * self.cfg.arb.recycle_min_gain) or u["forgone_ret"] > 0.05:
                continue
            funded[okey] = funded[("held", u.get("exchange_id"))] = time.time()
            side, px = u["order"]
            try:
                resp = self.api.place_order(u["exchange_id"], side, "buy", int(u["qty"]), float(px), expiration=ttl, key=self.api.new_key("offload"))
                self.store.record_order(resp.get("orderId"), u["exchange_id"], None, side, "buy", float(px), int(u["qty"]), "exit", response=resp)
                log.info("OFFLOAD: exit %s %s x%d @%.3f to fund %s", u["exchange_id"], side, int(u["qty"]), float(px), opp.get("exchange_id"))
                done += 1
            except ApiError as e:
                log.warning("offload exit %s failed: %s", u["exchange_id"], e)

    # --------------------------------------------------------------- market selection
    def pick_markets(self, prices: dict) -> list:
        """MM markets by the tracker's measured edge per hour (fill rate x net markout, prior from venue trade rate and
        spread) inside the price band; falls back to volume ranking when the tracker has too few ranked markets."""
        n = self.cfg.mm.n_markets
        if self.tracker is not None:
            try:
                ranked = self.tracker.rank(n, price_band=(self.cfg.mm.min_price, self.cfg.mm.max_price))
                exs = [ex for ex, _ in ranked if (prices.get(ex) or {}).get("bestBid") is not None and (prices.get(ex) or {}).get("bestAsk") is not None]
                pl = Bot.punt_legs(self)
                exs = [ex for ex in exs if ex not in pl]          # never quote a punt / election leg (self-trade, nets the punt)
                if len(exs) >= max(4, n // 2):
                    self.mm.quoted = exs[:n]
                    log.info("mm markets by tracker edge: %d chosen (top %s)", len(self.mm.quoted), [(e, round(sc, 2)) for e, sc in ranked[:5]])
                    return self.mm.quoted
            except Exception as e:
                log.warning("tracker rank: %s", e)
        pl = Bot.punt_legs(self)
        self.mm.quoted = [ex for ex in self.mm.select_markets(prices, self.volume) if ex not in pl]   # select_markets sets quoted itself
        return self.mm.quoted

    # --------------------------------------------------------------- realtime feed
    def mm_gate_sweep(self, books: dict, open_orders: dict, budget: int) -> dict:
        """Right after the outside-fair poll: on quoted legs where the fresh fair closed a side that was open when their
        quotes were built (this cycle's book mid, mm.gate_closed), our quotes on that side are pulled now, not at the next cycle.
        The poll lands after the MM block, so until now a quote could rest a whole cycle on a side the new fair gates (sim
        'catchup' 29 Oct: a bid hit 15 s after the poll). Nothing is posted here: the next cycle requotes the open side.
        Under the trade lock, as a fast requote: the cancels and the store marks go together."""
        with self.trade_lock:
            out = self.mm.pull_closed(books, open_orders, budget)
        if out.get("legs"):
            log.info("MM gate sweep after the outside poll: %s", out)
        return out if out.get("legs") else {}

    def mm_gate_ready(self, killed: bool, now: Optional[float] = None) -> bool:
        """May the MM quote this cycle? After a restart or a kill pause (no MM orders rest then) it waits for a fresh
        outside fair so the xv gate is known before the first quote, for at most 10 min (outside venues down). Mid-session
        a stale fair does not stop the MM: xv_gate treats stale fairs as open, as before (review 22 Oct: a mid-session wait
        left resting quotes unmanaged for up to 10 min)."""
        now = time.time() if now is None else now
        if self.cfg.mm.xv_gate_ticks <= 0 or not getattr(getattr(self.direction, "cfg", None), "enabled", True):
            return True
        if killed:
            self.mm_need_xv, self.mm_gate_wait0 = True, None
            return False
        if any(now - v[3] < 540 for v in (self.mm.xv_fair or {}).values()):
            self.mm_need_xv, self.mm_gate_wait0 = False, None
            return True
        if not getattr(self, "mm_need_xv", True):
            return True
        self.mm_gate_wait0 = getattr(self, "mm_gate_wait0", None) or now
        if now - self.mm_gate_wait0 > 600:
            self.mm_need_xv, self.mm_gate_wait0 = False, None
            log.warning("MM: no fresh outside fair for 10 min after start/kill; quoting with the xv gate open")
            return True
        return False

    def feed_ok(self) -> bool:
        return self.feed is not None and self.feed.healthy(30)   # > the 25-s heartbeat; was 120 s (frozen books)

    def feed_watchdog(self):
        """A feed thread can stall inside a DNS lookup or a connect that never returns (no socket timeout applies to
        getaddrinfo). If the feed has been silent for feed_stall_sec, abandon that thread and start a fresh feed; the
        old one is told to stop and exits whenever its blocking call returns."""
        if self.feed is None:
            return
        s = self.feed.stats()
        age = s.get("last_message_age_sec")
        started = getattr(self, "feed_started", 0.0)
        silent = (age is None and time.time() - started > self.cfg.feed_stall_sec) or (age is not None and age > self.cfg.feed_stall_sec)
        if not silent:
            return
        log.warning("feed watchdog: silent for %ss (connected=%s, reconnects=%s); replacing the feed thread", "-" if age is None else int(age),
                    s.get("connected"), s.get("reconnects"))
        old = self.feed
        try:
            old.stop()
        except Exception:
            pass
        try:
            self.feed = RealtimeFeed(self.cfg.api_key, self.cfg.base_url, self.api.tournament_id, log=logging.getLogger("sigbot.feed"))
            self.feed.set_universe(self.exchanges_by_market)
            self.feed.start()
            self.feed_started = time.time()
        except Exception as e:
            log.warning("feed restart failed: %s", e)
            self.feed = None

    def fresh_book(self, ex, max_age=None):
        """The feed's book for `ex` when the feed is healthy, the book is not awaiting a resync and it arrived within
        `max_age` seconds (default feed_max_age_sec); else None, and the caller reads REST."""
        if not self.feed_ok():
            return None
        b = self.feed.book(ex)
        if not b or str(ex) in self.feed.needs_resync():
            return None
        if time.time() - b.get("feed_ts", 0.0) > (self.cfg.feed_max_age_sec if max_age is None else max_age):
            return None
        return b

    def get_book(self, ex, depth: int = 50, client=None, max_age=None) -> dict:
        """Feed book if fresh, else an authoritative REST read (which also clears the exchange's resync flag)."""
        b = self.fresh_book(ex, max_age)
        if b is not None:
            return b
        b = (client or self.api).orderbook(ex, depth)
        if self.feed is not None:
            self.feed.mark_resynced([ex])
        return b

    def feed_prices(self, base: dict) -> dict:
        """REST price snapshot with bestBid/bestAsk replaced by the feed's where the feed book is fresh."""
        out = dict(base)
        if not self.feed_ok():
            return out
        pending, now, max_age = self.feed.needs_resync(), time.time(), self.cfg.feed_max_age_sec
        for ex, (bid, ask, ts) in self.feed.bests().items():
            if ex in pending or now - ts > max_age:
                continue
            p = dict(out.get(ex) or {})
            p["bestBid"], p["bestAsk"] = bid, ask
            out[ex] = p
        return out

    def feed_summary(self) -> str:
        if self.feed is None:
            return "off"
        s = self.feed.stats()
        age = s.get("last_batch_age_sec")
        return "%s books=%d resync=%d age=%s rc=%d" % ("ok" if self.feed.healthy() else "DOWN", s["books_held"], s["resync_pending"],
                                                        "-" if age is None else "%.0fs" % age, s["reconnects"])

    # --------------------------------------------------------------- cycle
    def cycle(self):
        t0 = time.time()
        self.cash_needs = {}                     # sleeves post this cycle's cash shortfalls; the cash raise reads them
        # Read cash and positions together under the trade lock: an arb basket executed by the scanner thread between
        # the two reads (or between them and the health check) made the account look ~1,100 lower for one cycle and
        # tripped the drawdown kill switch on 22 Oct 15:39. cycle_cash is the cash that matches pos_value.
        with self.trade_lock:
            acct_ok = False
            try:
                acct = self.api.account()
                self.cash = float(acct.get("balance") or self.cash)
                acct_ok = acct.get("balance") is not None
            except ApiError as e:
                log.warning("account: %s", e)
            positions, pos_value, n_pos = self.positions_map()
            cycle_cash = self.cash
            # the stops reconcile cash, positions and these values together: only when both reads succeeded
            cycle_ok = acct_ok and getattr(self, "positions_ok", False)
            cycle_mv = dict(self.pos_mv)
            cycle_settled = set(getattr(self, "pos_settled", ()) or ())
            cycle_adj = getattr(self, "settle_adj", 0.0)   # positions_map() set it from these positions; later calls
                                                          # (scanner thread, recycle backstop) move it, so keep this one
            # read by the scanner thread (free unwinds between cycles); set under the lock: set after it, a scanner unwind
            # (and its in-lock refresh) in between was overwritten by these older positions
            self.positions_snapshot = positions
            # shock mode's own marked P&L, read in the same snapshot: left out of the kill switch's drawdown (thresholds
            # unchanged) because walking 2-4 ticks into a 20k shock marks ~-600 at once and would trip the 400 drawdown
            # kill on every entry; the sleeve has its own stop (shock_kill)
            shock_pnl = 0.0
            if self.shock is not None:
                try:
                    if self.shock.ctx or self.shock_open:
                        self.ingest_fills()          # the venue snapshot already holds the shock's fills: match the ledger
                    shock_pnl = self.shock_ledger(getattr(self, "venue_mark", None))[0]
                    self.shock_pnl_last = shock_pnl
                except Exception as e:                   # keep the last good value: 0 would jump equity by the sleeve's
                    log.warning("shock ledger for health: %s", e)   # whole P&L and could trip the drawdown kill (review 6)
                    shock_pnl = getattr(self, "shock_pnl_last", 0.0)
        oo_t0 = time.time()
        open_orders = self.open_orders_map()
        self.open_orders_cache = open_orders
        self.open_orders_ts = time.time()
        self.mm.open_orders_ts = self.open_orders_ts       # the MM's pull checks for orders other sleeves placed since the read
        # (read START time, success), set with the cache: shock mode trusts an order's absence only from a successful
        # read that began after the order was placed
        self.open_orders_read = (oo_t0, bool(getattr(self, "open_orders_ok", True)))
        self.load_allocation()
        self.feed_watchdog()
        cycle_id = self.store.new_cycle(cycle_cash, pos_value, n_pos, sum(len(v) for v in open_orders.values()),
                                        self.api.stats["reads"], self.api.stats["writes"])
        self.last_cycle_id = cycle_id
        snap, snap_ts = self.latest_prices
        if self.scanner is not None and snap and time.time() - snap_ts < 12:
            prices = snap
        else:
            prices = self.api.prices(self.universe.all_exchanges())

        # ---- arbs first (riskless, capital priority)
        arb_summary = {"candidates": 0, "planned": 0, "executed": 0}
        if self.cfg.arb.enabled and self.cfg.arb.new_sets and self.scanner is None and not strategy_paused(self, "arb"):
            cands = self.arb.candidates_from_prices(prices)
            arb_summary["candidates"] = len(cands)
            cand_books = {}
            for race in cands[: self.cfg.arb_books_per_cycle]:
                try:
                    cand_books[race.name] = {ex: self.get_book(ex, 50) for ex in race.exchange_ids}
                except ApiError as e:
                    log.warning("book %s: %s", race.name, e)
            plan = self.arb.plan(cand_books, spendable_cash(self))
            arb_summary["planned"] = len(plan)
            if plan:
                log.info("arb plan: %s", [(c.race, c.kind, c.qty, round(c.edge, 4), round(100 * c.ret, 2)) for c in plan])
                res = self.arb.execute(plan, cycle_id)
                arb_summary["executed"] = sum(1 for r in res if r.get("ok"))
                if arb_summary["executed"]:
                    positions, pos_value, n_pos = self.positions_map()
                    try:
                        self.cash = float(self.api.account().get("balance") or self.cash)
                    except ApiError:
                        pass
            self.arb.flatten_residuals(positions, prices)
        elif self.scanner is not None:
            arb_summary = dict(self.scanner.stats)
            # capital recycling: unwind a held set to fund a better one the scanner could not afford
            best = (self.scanner.best_unfunded if not self.feed_ok() and self.cfg.arb.new_sets and time.time() >= self.killed_until
                    and not strategy_paused(self, "recycle") else None)   # the scanner recycles itself when the feed is up; cash mode and kill pauses: no backstop
            if best and time.time() - best["ts"] < 120 and self.cfg.arb.recycle_enabled and self.arb.committed and not best.get("failed"):
                try:
                    best = dict(best, fresh_books={ex: self.get_book(ex, 50) for ex in self.universe.races[best["race"]].exchange_ids})
                except ApiError:
                    best = None
            if best and time.time() - best["ts"] < 120 and self.cfg.arb.recycle_enabled and self.arb.committed and not best.get("failed"):
                held_books = {}
                prot = self.arb.protected()
                for rname in [r for r in self.arb.committed if r not in prot][:3]:
                    try:
                        held_books[rname] = {ex: self.get_book(ex, 50) for ex in self.universe.races[rname].exchange_ids}
                    except ApiError as e:
                        log.warning("recycle book %s: %s", rname, e)
                with self.trade_lock:
                    rec = self.arb.recycle(best, held_books, self.positions_snapshot, spendable_cash(self), cycle_id, refresh=self.fresh_positions)
                    if rec and not rec.get("dry"):
                        self.refresh_snapshot(self.unwind_deltas([rec]))
                if rec and not rec.get("dry"):
                    arb_summary["recycled"] = rec["race"]
                    try:
                        self.cash = float(self.api.account().get("balance") or self.cash)
                    except ApiError:
                        pass
                    # spend the freed capital on the target basket right away, from a fresh book, in the same lock
                    try:
                        tgt = best["race"]
                        fresh = {ex: self.get_book(ex, 50) for ex in self.universe.races[tgt].exchange_ids}
                        with self.trade_lock:
                            plan = [c for c in self.arb.plan({tgt: fresh}, spendable_cash(self), extra_budget=rec.get("qty", 0) * rec.get("proceeds", 0.0),
                                                             recycle=True) if c.race == tgt]
                            if plan:
                                res = self.arb.execute(plan, cycle_id)
                                arb_summary["recycle_funded"] = sum(1 for r in res if r.get("ok"))
                                self.refresh_snapshot({e: -q for e, q in self.basket_fills(res).items()})
                            else:
                                log.warning("recycle: target %s gone after unwind; capital stays free for the scanner", tgt)
                    except ApiError as e:
                        log.warning("recycle follow-through %s: %s", best.get("race"), e)
                    self.scanner.best_unfunded = None

        # ---- portfolio exposure limits (arb sets netted out)
        expo = risk.exposure(self.universe, positions, prices)
        blocked = risk.blocked_directions(expo, self.cfg.mm.portfolio_max_partisan_notional)
        self.mm.blocked = self.tilt.blocked = blocked
        self.store.set_param("blocked_directions", sorted(blocked))
        if blocked:
            log.warning("partisan cap hit: net D notional %+.0f (long %.0f short %.0f); blocking adds to %s", expo.net_d_notional,
                        expo.long_d, expo.short_d, sorted(blocked))

        # ---- fills and tape
        fills_count = self.ingest_fills()
        self.catchup_ledger_refresh()          # the MM below must not count a fresh catch-up clip as its own inventory (review 31 Oct)
        quoted = self.mm.quoted or self.pick_markets(prices)
        arb_exs = [ex for r in self.universe.races.values() for ex in r.exchange_ids if r.name in self.arb.committed]
        self.election_fills()
        feed_trades: list = []
        if self.feed is not None:
            feed_trades = self.feed.drain_trades()
            by_ex: dict = {}
            for tr in feed_trades:
                by_ex.setdefault(str(tr.get("exchangeId")), []).append(tr)
            for ex, trs in by_ex.items():
                self.store.ingest_tape(ex, trs)
            self.feed.drain_fills()               # the REST ledger stays authoritative; keep the buffer from growing
        if not self.feed_ok() or (cycle_id % 5) == 0:    # with the feed, REST tape polls only backfill dropped batches
            self.poll_tapes(list(dict.fromkeys(quoted + arb_exs)))
        if self.tracker is not None:              # per-market volatility / volume / fill-quality tracker (ranks MM markets)
            try:
                new_mm = self.store.rows("SELECT * FROM fills WHERE purpose='mm' AND ingested_at > ?", (self.tracker_mark,))
                self.tracker_mark = time.time()
                self.tracker.update(feed_trades, self.feed.books() if self.feed_ok() else None, time.time(), fills=new_mm, quoted=self.mm.quoted)
            except Exception as e:
                log.warning("tracker update: %s", e)

        # ---- bars, mean-reversion skew, health and kill switch
        self.update_bars(prices)
        self.mm.meanrev_pred = self.meanrev_predictions(self.mm.quoted)
        self.mm.prices = prices
        hl = self.health(cycle_cash + pos_value + cycle_adj - shock_pnl, cycle_id)
        self.wd_drawdown = float(hl.get("drawdown") or 0.0)       # the wind-down's budget past a fallback uses the kill headroom
        if self.stops is not None:
            # per-strategy stops: a pause stops only that strategy's NEW entries (exits, unwinds and quote cancels go on);
            # the 30-min bot-wide switch below is off (killed stays False, so no exit path waits on it)
            self.stops_cycle(cycle_cash if cycle_ok else None, positions, cycle_mv, prices, open_orders, hl, cycle_settled)
        killed = time.time() < self.killed_until
        if (self.stops is None or getattr(self, "stops_fallback", False)) and not killed and (hl["drawdown"] > self.cfg.kill_drawdown or (hl["n_markout"] >= 20 and hl["markout30"] is not None and hl["markout30"] < self.cfg.kill_markout_ticks)):
            self.killed_until = time.time() + self.cfg.kill_pause_sec
            killed = True
            log.warning("KILL SWITCH: drawdown %.0f, markout30 %s (n=%d); cancelling MM/tilt quotes for %ds", hl["drawdown"], hl["markout30"], hl["n_markout"], self.cfg.kill_pause_sec)
            if not self.dry_run:
                try:
                    self.api.cancel_all()
                except ApiError as e:
                    log.warning("kill cancel-all: %s", e)
        log.info("HEALTH fills5=%s fills30=%s markout30=%s(n=%d) drawdown=%.0f netD=%+.0f killed=%s wave=%s hot=%d feed=[%s]", hl["fills5"], hl["fills30"],
                 None if hl["markout30"] is None else round(hl["markout30"], 2), hl["n_markout"], hl["drawdown"], expo.net_d_notional, killed,
                 self.mm.wave, sum(1 for ex in self.mm.quoted if self.mm.hot_until.get(ex, 0) > time.time()), self.feed_summary())

        # ---- market making
        mm_summary, mm_books = {}, {}
        self.mm.xv_fair = {r["exchange_id"]: (r.get("fair"), r.get("pm"), r.get("kalshi"), r.get("ts") or time.time())
                           for r in (getattr(self.direction, "rows", None) or []) if r.get("fair") is not None and r.get("exchange_id")}
        gate_ready = self.mm_gate_ready(killed)
        if not gate_ready and not killed:
            log.info("MM waits for the first outside-fair poll before quoting (xv gate)")
        mm_paused = strategy_paused(self, "mm")
        if self.wind_down is not None:
            # wind-down: the MM's per-race inventory cap shrinks from 1 Nov 00:00 ET to zero at 12:00 ET 3 Nov (at the cap it
            # posts nothing on the adding side), then the MM stops and its quotes are pulled like a monitor pause
            now_ = time.time()
            self.mm.inv_cap_override = max(1.0, WD.mm_inventory_cap(now_, self.mm_inv_base, self.wd_mm_end))
            if WD.mm_flat_due(now_, self.wd_mm_end):
                mm_paused = True
        if mm_paused and not self.dry_run and any(open_orders.values()):
            try:
                ids = [o.get("id") for lst in open_orders.values() for o in lst if o.get("id") is not None]
                purp = {str(r["order_id"]): r["purpose"] for r in self.store.rows(
                    "SELECT order_id, purpose FROM orders WHERE order_id IN (%s)" % ",".join("?" * len(ids)), tuple(ids))} if ids else {}
                with self.trade_lock:                 # no shock / scanner order is placed between the check and the cancel
                    newer = self.store.one("SELECT 1 FROM orders WHERE purpose NOT IN ('mm', 'inside') AND placed_at >= ? LIMIT 1",
                                           (getattr(self, "open_orders_ts", 0.0) - 60.0,))
                    allmm = ids and newer is None and all(purp.get(str(i)) in ("mm", "inside") for i in ids)
                    if allmm:
                        self.api.cancel_all()
                if allmm:
                    # nothing but MM quotes rests (no exit, raise, punt or manual order): one account-wide cancel, as
                    # fast as today's switch, instead of up to 14 markets a cycle through the write budget
                    self.store.mark_orders([int(i) for i in ids], "cancelled")
                    log.warning("MM paused: every resting MM quote cancelled at once (%d orders)", len(ids))
                    n = 1
                else:
                    n = self.cancel_mm_quotes(open_orders, max(4, self.cfg.mm_write_budget - 4))
                mm_summary["monitor_cancels"] = mm_summary["writes"] = n     # the tilt budget below counts these writes
            except Exception as e:                   # a network error must not skip the exits later in this cycle
                log.exception("monitor MM cancel: %s", e)
        if self.cfg.mm.enabled and not killed and gate_ready and not mm_paused:
            if (cycle_id % 30) == 1:
                self.pick_markets(prices)
            books = {}
            for ex in self.mm.quoted:
                p = prices.get(ex) or {}
                key = (p.get("bestBid"), p.get("bestAsk"), p.get("latestPrice"))
                fb = self.fresh_book(ex)
                if fb is not None:
                    books[ex] = fb
                    self.book_cache[ex] = (fb, fb.get("bestBid"), fb.get("bestAsk"), p.get("latestPrice"))
                    self.book_ts.pop(ex, None)
                    continue
                cached = self.book_cache.get(ex)
                st = self.mm.state.get(ex)
                due = (st is None or st.last_reserve is None or time.time() - st.last_quote_ts > self.cfg.mm.refresh_sec
                       or fills_count.get(ex, 0) > (st.fills_seen if st else 0) or self.mm.wave)
                if cached and cached[1:] == key and not due:
                    books[ex] = cached[0]
                    continue
                try:
                    b = self.api.orderbook(ex, 10)
                    books[ex] = b
                    self.book_cache[ex] = (b,) + key
                    self.book_ts[ex] = time.time()               # the MM's last look may use it while it is fresh
                except ApiError as e:
                    log.warning("book %s: %s", ex, e)
            write_budget = max(4, self.cfg.mm_write_budget - 4)   # leave room for the tilt sleeve
            mm_summary = self.mm.quote_cycle(books, self.mm_positions(positions), self.pressures(), open_orders, fills_count, max(0.0, spendable_cash(self)), cycle_id, write_budget)
            mm_books = books

        # ---- inside-spread quoting where the DMM's spread is 3+ ticks (exploit 2.2), outside the MM's own markets
        inside_summary = {}
        if self.inside is not None and not killed and self.feed_ok() and not strategy_paused(self, "inside"):
            try:
                inside_summary = self.inside_cycle(positions, prices, blocked, cycle_id)
            except Exception as e:
                log.exception("inside quoter: %s", e)

        # ---- tilt harvest (tails only, disjoint from the MM band)
        tilt_summary = {}
        if self.cfg.tilt.enabled and not killed and not strategy_paused(self, "tilt"):
            tilt_summary = self.tilt.cycle(prices, positions, open_orders, set(self.mm.quoted), max(0.0, spendable_cash(self)), cycle_id,
                                           max(2, self.cfg.mm_write_budget - (mm_summary.get("writes", 0) if mm_summary else 0)))

        # ---- news sweeper (feeds polled every news.poll_sec; taker orders only when --live)
        news_summary = {}
        if self.news is not None and not strategy_paused(self, "news"):
            try:
                news_summary = self.news.cycle(prices, max(0.0, spendable_cash(self)), cycle_id)
            except Exception as e:                 # the sweeper must never kill the loop
                log.exception("news cycle failed: %s", e)

        # ---- directional sleeve: explained legs only, small size (orders only if direction.allow_live)
        dir_summary = {}
        if not killed:
            # paused: the poll still runs (exits read it), its orders do not
            dp = self.dry_run or strategy_paused(self, "direction")
            dir_summary = self.direction.cycle(prices, positions, max(0.0, spendable_cash(self)), write_budget=2, dry_run=dp)
            if dir_summary.get("traded"):
                log.info("DIRECTION traded %d skipped %d blocked=%s", dir_summary.get("traded", 0), dir_summary.get("skipped", 0),
                         dir_summary.get("blocked"))
        if self.catchup is not None:
            try:
                self.catchup_cycle(dir_summary, positions, prices, killed)
            except Exception as e:                   # the trial must never kill the loop
                log.exception("catchup cycle failed: %s", e)
        try:
            self.snapshot_books()
        except Exception as e:                       # a snapshot must never kill the loop
            log.warning("book snapshot: %s", e)
        if self.churn is not None:
            try:
                self.churn_cycle(positions, killed, cycle_id)
            except Exception as e:                   # the sleeve must never kill the loop
                log.exception("churn cycle failed: %s", e)


        # ---- outside fair for the MM's gate, refreshed right after the directional poll: NOT redundant with the rebuild
        # before the MM block, because the scanner thread's fast requotes read it until the next cycle (sim offload /
        # slow_writes / day: removing this let fast requotes post on gated sides for a whole cycle)
        self.mm.xv_fair = {r["exchange_id"]: (r.get("fair"), r.get("pm"), r.get("kalshi"), r.get("ts") or time.time())
                           for r in (getattr(self.direction, "rows", None) or []) if r.get("fair") is not None and r.get("exchange_id")}
        # the poll can close the gate on a side the MM quoted earlier in this cycle: pull those quotes now (mm_gate_sweep),
        # within what the MM's budget has left after the MM, inside and tilt blocks and the directional sleeve's 2 writes
        if mm_books and dir_summary.get("polled") and not killed and gate_ready and not mm_paused:
            try:
                left = self.cfg.mm_write_budget - int(mm_summary.get("writes", 0) or 0) - int(inside_summary.get("writes", 0) or 0) \
                    - int(tilt_summary.get("writes", 0) or 0) - 2
                sw = self.mm_gate_sweep(mm_books, open_orders, max(0, min(8, left)))
                if sw:
                    mm_summary["gate_sweep"] = sw
            except Exception as e:                   # the sweep must never skip the sleeves after it
                log.exception("MM gate sweep: %s", e)

        # ---- cheap safe favourites: buy fast-count favourites SIG prices >= 2c under the outside fair, hold to the call
        if self.cheapfav is not None:
            try:
                self.cheapfav_cycle(dir_summary, positions, prices, killed)
            except Exception as e:                   # the sleeve must never kill the loop
                log.exception("cheapfav cycle failed: %s", e)
        if getattr(self, "gap", None) is not None:
            try:
                self.gap_cycle(dir_summary, positions, prices, killed)
            except Exception as e:                   # the pilot must never kill the loop
                log.exception("gap pilot cycle failed: %s", e)
        # ---- strategy monitor (every 15 min): pause a sleeve whose own results show it is losing (strategy_monitor.py)
        try:
            self.monitor_cycle()
        except Exception as e:                       # the monitor must never kill the loop
            log.exception("strategy monitor failed: %s", e)

        # ---- volatile moves: pause the MM's retrace side after a 5+ tick move (verified), paper-log the move trades
        if self.feed_ok():
            try:
                self.volatile_cycle()
            except Exception as e:
                log.warning("volatile moves: %s", e)

        # ---- outside-move follower: take the DMM touch when Polymarket/Kalshi moved and SIG has not (exploit 2.1)
        if self.lag is not None and not killed and not strategy_paused(self, "lag"):
            try:
                self.lag_cycle(positions, blocked)
            except Exception as e:
                log.exception("lag follower: %s", e)

        # ---- zero-EV exits (directional / tilt) and free arb-set unwinds
        if not killed:
            if not strategy_paused(self, "exit:zero_ev"):
                self.zero_ev_exits(positions, getattr(self.direction, "rows", []))
            if (self.arb.committed and time.time() - self.last_free_unwind > self.cfg.free_unwind_every_sec
                    and not strategy_paused(self, "exit:unwinds")):
                self.last_free_unwind = time.time()
                held_books = {}
                cf_races = cheapfav_races_of(self) | shock_races_of(self) | punt_races_of(self)   # a cheap-favourite / shock / punt position next to
                for rname in list(self.arb.committed):                  # another sleeve's NO is not a set
                    if rname in cf_races:
                        continue
                    try:
                        held_books[rname] = {ex: self.get_book(ex, 50) for ex in self.universe.races[rname].exchange_ids}
                    except ApiError as e:
                        log.warning("free-unwind book %s: %s", rname, e)
                with self.trade_lock:
                    # the latest snapshot, not the cycle-start positions: a scanner recycle, free unwind or churn exit
                    # earlier in this cycle may already have closed these sets (sim 'churn', 23 Oct: double unwind)
                    fu = self.arb.free_unwinds(held_books, self.positions_snapshot, cycle_id, refresh=self.fresh_positions)
                    if fu:
                        self.refresh_snapshot(self.unwind_deltas(fu))
                if fu:
                    log.info("free unwinds: %s", fu)

        # ---- wind-down (31 Oct - 3 Nov): late-call sets, legs and MM inventory to cash, in clips, cheapest first
        if self.wind_down is not None:
            try:
                self.wind_down_cycle(prices, killed, cycle_id)
            except Exception as e:                   # the routine must never kill the loop
                log.exception("wind-down cycle failed: %s", e)

        # ---- cash raise: sell cheap-to-exit holdings only for a higher-edge sleeve short of cash this cycle; after the
        # zero-EV exits, whose orders mark their legs busy (sim cash_raise: an exit sized from the cycle-start positions
        # sold a leg the raise had just sold, -900)
        if getattr(self.cfg, "raise_enabled", False):
            try:
                self.raise_cycle(prices, killed)
            except Exception as e:                   # the routine must never kill the loop
                log.exception("raise cycle failed: %s", e)
            try:
                self.ri_trim_cycle(prices, killed)
            except Exception as e:                   # the routine must never kill the loop
                log.exception("RI trim failed: %s", e)

        # ---- analytics refresh
        if time.time() - self.last_analytics > self.cfg.analytics_every_sec:
            try:
                out = compute_overrides(self.store, self.cfg)
                path = os.path.join(HERE, self.cfg.mm.overrides_file)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w") as f:          # atomic: the scanner thread may read it at any moment
                    json.dump(out, f, indent=1)
                os.replace(tmp, path)
                self.last_analytics = time.time()
            except Exception as e:  # analytics must never kill the loop
                log.exception("analytics failed: %s", e)
        if time.time() - self.last_universe_refresh > self.cfg.market_watch_sec:
            try:
                mk_now = self.api.markets()
                old = set(self.universe.all_exchanges())
                new_u = Universe.from_markets(mk_now)
                added, gone = set(new_u.all_exchanges()) - old, old - set(new_u.all_exchanges())
                if added or gone:
                    log.warning("MARKET WATCH: %d new exchange(s) %s, %d closed/voided %s", len(added), sorted(added)[:10], len(gone), sorted(gone)[:10])
                self.universe = new_u
                self.arb.u = self.mm.u = self.tilt.u = self.universe
                if self.stops is not None:          # sets in a race the watch adds are valued at payout too; a race
                    known = {tuple(r) for r in self.stops.races}   # that leaves the read keeps its payout value
                    known |= {tuple(r.exchange_ids) for r in new_u.races.values() if len(r.exchange_ids) >= 2}
                    self.stops.races = [list(r) for r in known]
                if self.news is not None:
                    self.news.u = self.universe
                self.last_universe_refresh = time.time()
            except ApiError as e:
                log.warning("universe refresh: %s", e)
        try:
            shadow_tick(self)       # paper-only parameter tuner on our own logs; applies nothing unless config shadow.auto_apply
        except Exception as e:
            log.warning("shadow tick: %s", e)
        log.info("cycle %d done in %.1fs cash=%.0f pos_value=%.0f total=%.0f netD=%+.0f arb=%s mm=%s tilt=%s dir=%s news=%s api=%s", cycle_id, time.time() - t0, self.cash,
                 pos_value, self.cash + pos_value, expo.net_d_notional, arb_summary, {k: (len(v) if isinstance(v, list) else v) for k, v in mm_summary.items()},
                 {k: (len(v) if isinstance(v, list) else v) for k, v in tilt_summary.items()},
                 {k: v for k, v in dir_summary.items() if k in ("polled", "priced", "signals", "traded", "dry", "skipped", "error")}, news_summary, self.api.stats)

    def run(self, once: bool = False):
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        if self.cfg.arb_scan_sec and self.cfg.arb.enabled and not once:
            self.arb.api = self.scan_api        # arb executions go through the scanner's own write budget
            self.scanner = ArbScanner(self)
            self.scanner.start()
            log.info("arb scanner started: every %.1fs at +%.1fs offset", self.cfg.arb_scan_sec, self.cfg.arb_scan_offset)
        if getattr(self, "punt", None) is not None and not once:
            threading.Thread(target=self.punt_loop, name="punt", daemon=True).start()
            log.warning("PUNT executor thread started (%s): every %.0fs", "DRY" if self.dry_run else "LIVE", self.cfg.punt_tick_sec)
        if getattr(self, "offload", None) is not None and not once:
            threading.Thread(target=self.offload_loop, name="offload", daemon=True).start()
            log.warning("OFFLOAD executor thread started: every %.0fs", self.cfg.offload_tick_sec)
        if self.shock is not None and not self.dry_run and not once:
            threading.Thread(target=self.shock_loop, name="shock", daemon=True).start()
            log.info("shock mode thread started: every %.0fs", self.cfg.shock_poll_sec)
        while not self.stop:
            start = time.time()
            try:
                self.cycle()
                self.first_cycle_done = True
            except Exception as e:
                log.exception("cycle failed: %s", e)
            if once:
                break
            hour_et = datetime.now(ZoneInfo("America/New_York")).hour
            fast = self.cfg.fast_hours_et[0] <= hour_et < self.cfg.fast_hours_et[1]
            cycle = self.cfg.cycle_sec_fast if fast else self.cfg.cycle_sec
            time.sleep(max(1.0, cycle - (time.time() - start)))
        if self.feed is not None:
            self.feed.stop()
        if not self.dry_run and not once:
            log.info("stopping: cancelling all resting orders")
            try:
                self.api.cancel_all()
            except ApiError as e:
                log.warning("final cancel-all: %s", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-mm", action="store_true")
    ap.add_argument("--no-arb", action="store_true")
    ap.add_argument("--no-tilt", action="store_true")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--news", action="store_true", help="enable the news sweeper even if config has news.enabled=false")
    args = ap.parse_args()
    cfg = BotConfig.load(args.config)
    if not cfg.api_key:
        raise SystemExit("no API key: set SIG_API_KEY or write it to .sig_api_key next to config.json")
    if args.no_mm:
        cfg.mm.enabled = False
    if args.no_arb:
        cfg.arb.enabled = False
    if args.no_tilt:
        cfg.tilt.enabled = False
    if args.no_news:
        cfg.news.enabled = False
    if args.news:
        cfg.news.enabled = True
    dry = not args.live
    marker = os.path.join(HERE, "deploy", "SERVER_HOST")
    if not dry and os.path.exists(marker) and not os.path.exists("/etc/systemd/system/sigbot.service"):
        raise SystemExit(f"refusing to trade live here: the bot runs on the server {open(marker).read().strip()} "
                         "(deploy/SERVER_HOST exists). Use deploy/remote.sh, or delete that file to move trading back.")
    os.makedirs(os.path.join(HERE, os.path.dirname(cfg.log_path)), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(HERE, cfg.log_path))])
    log.info("starting sigbot dry_run=%s config=%s", dry, json.dumps(cfg.dump())[:800])
    Bot(cfg, dry).run(once=args.once)


if __name__ == "__main__":
    main()
