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
from .proposals.outside_sweep import CatchupSweep, pnl_from_fills, rows_from_venue_quotes
from .proposals import cheap_favourites as CF
from .proposals.dev_churn import (DevChurn, overround_series, medians_from_series, vol_ratio, mid_overround, big_holding,
                                  sell_all_due, LAST_ENTRY as CHURN_LAST_ENTRY, VOL_RECENT, VOL_BASE, EXCLUDE as CHURN_EXCLUDE)
from .proposals import election_night as EN
from .proposals.own_book import OwnLevels


def cheapfav_races_of(bot) -> set:
    """Races the cheap-favourites sleeve holds (empty when the sleeve is not wired, e.g. on test stubs)."""
    f = getattr(bot, "cheapfav_races", None)
    return f() if callable(f) else set()


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
        if not b.cfg.arb.enabled or time.time() < b.killed_until:
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
        if b.arb.budget_left(b.cash) < b.cfg.arb.min_set_qty * min_cost and not b.cfg.arb.recycle_enabled:
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
            plan = b.arb.plan(cand_books, b.cash)
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
        if n_max <= 0 or not b.cfg.mm.enabled or not b.feed_ok() or not b.mm.quoted or time.time() < b.killed_until:
            return
        now = time.time()
        self.fast_log = [t for t in self.fast_log if now - t < 60]
        if len(self.fast_log) >= n_max:
            return
        for ex in list(b.mm.quoted):
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
                                            b.fills_count_cache, b.cash, b.last_cycle_id, 2)
                finally:
                    b.mm.force_cancel = False
            b.book_cache[ex] = (fb, bb, ba, fb.get("latestPrice"))
            if summ.get("requoted"):
                self.fast_log.append(time.time())
                self.stats["fast_requotes"] = self.stats.get("fast_requotes", 0) + 1
                log.info("fast requote %s: touch %.3f/%.3f -> %.3f/%.3f", ex, touch[0], touch[1], bb, ba)
            else:
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
                or not b.arb.committed or not b.feed_ok() or time.time() < b.killed_until):
            return
        if b.cfg.arb.new_sets and b.arb.budget_left(b.cash) >= best["cost"] * max(b.cfg.arb.min_set_qty, 1):
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
            rec = b.arb.recycle(dict(best, fresh_books=target), held_books, b.positions_snapshot, b.cash, None, refresh=b.fresh_positions)
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
                plan = [c for c in b.arb.plan({tgt: fresh}, b.cash, extra_budget=(b.arb.recycle_credit if cash_mode else freed), recycle=True)
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
                plan = [c for c in b.arb.plan({tgt: fresh}, b.cash, extra_budget=b.arb.recycle_credit, recycle=True) if c.race == tgt]
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
        if time.time() - self.last_offload < b.cfg.offload_scan_sec or time.time() < b.killed_until:
            return
        self.last_offload = time.time()
        b.offload_report(0)

    def free_unwind_pass(self):
        """Every free_unwind_scan_sec, with every leg's feed book fresh: unwind held sets whose asks sum to the payout or
        less (the locked profit is realised at no cost and the capital comes back). Positions are re-read after any
        unwind so a stale snapshot can never buy legs for sets we no longer hold."""
        b = self.bot
        if not b.arb.committed or not b.feed_ok() or time.time() < b.killed_until:
            return
        if time.time() - b.last_free_unwind < b.cfg.free_unwind_scan_sec:
            return
        held = {}
        cf_races = cheapfav_races_of(b)                  # a cheap-favourite NO next to another sleeve's NO is not a set
        for rname in list(b.arb.committed):
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
        self.catchup = (CatchupSweep(notional=cfg.catchup_notional, kill_pnl=cfg.catchup_kill)
                        if getattr(cfg, "catchup_enabled", False) else None)
        self.catchup_state_path = os.path.join(HERE, "params", "catchup_state.json")
        if self.catchup is not None:
            try:
                st = json.load(open(self.catchup_state_path))
                self.catchup.ctx = {str(k): v for k, v in (st.get("ctx") or {}).items()}
                self.catchup.last_signal = dict(st.get("last_signal") or {})
                self.catchup.killed = bool(st.get("killed", False))
            except Exception as e:                   # missing, corrupt or not a dict: start empty (positions without
                log.warning("catchup state unreadable (%s): starting empty", e)   # context are then closed)
            try:                                     # the 60-min look-back from stored quotes: no hour of waiting after a restart
                leg_of = {(r.name, p): l.exchange_id for r in self.universe.races.values() for p, l in r.legs.items()}
                vq = self.store.rows("SELECT ts, venue, race, party, COALESCE(mid, (bid + ask) / 2.0) m FROM venue_quotes "
                                     "WHERE ts > ? AND venue IN ('polymarket', 'kalshi', 'sig')", (time.time() - 5400,))
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
                                                   net_max=cfg.cheapfav_net_max, headroom=cfg.cheapfav_headroom)
                log.warning("CHEAPFAV sleeve enabled: budget %.0f, min edge %.3f, %.0f per leg, %d same-terms legs",
                            cfg.cheapfav_budget, cfg.cheapfav_min_edge, cfg.cheapfav_leg_cap, len(same))
        self.churn = (DevChurn(capital=cfg.churn_capital, cash_floor=cfg.churn_cash_floor, kill=cfg.churn_kill)
                      if getattr(cfg, "churn_enabled", False) else None)
        self.churn_state_path = os.path.join(HERE, "params", "churn_state.json")
        self.churn_med, self.churn_med_ts = {}, 0.0
        if self.churn is not None:
            try:
                self.churn.load(json.load(open(self.churn_state_path)))
            except Exception as e:                   # missing/corrupt: start empty; held sets stay under the normal rules
                log.warning("churn state unreadable (%s): starting empty", e)
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
        self.mm = MarketMaker(self.api, self.store, cfg.mm, self.universe, self.slug, cfg.starting_cash, dry_run, HERE)
        self.mm.touch_fn = self.mm_touch       # last look at the freshest book before quotes are sent
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
        for r in self.store.rows("SELECT exchange_id, side, SUM(ABS(quantity)) q FROM fills WHERE purpose='exit' GROUP BY 1,2"):
            opp = "no" if r["side"] == "yes" else "yes"      # an exit buying YES closed held NO, and vice versa
            held[(r["exchange_id"], opp)] = held.get((r["exchange_id"], opp), 0.0) - r["q"]
        # exit orders still resting (60-s TTL vs a 30-s cycle) already cover part of the holding: never send a second one
        # on top (review 22 Oct: two positions were closed past zero and flipped)
        exit_ids = {str(r["order_id"]) for r in self.store.rows("SELECT order_id FROM orders WHERE purpose='exit' AND order_id IS NOT NULL")}
        resting_exit: dict = {}
        for ex_o, lst in (getattr(self, "open_orders_cache", None) or {}).items():
            for o in lst:
                if str(o.get("id")) in exit_ids:
                    held_side = "no" if o.get("side") == "yes" else "yes"   # an exit buying YES closes held NO
                    rem = float(o.get("quantity") or 0) - float(o.get("quantityFilled") or o.get("filledQuantity") or 0)
                    resting_exit[(str(ex_o), held_side)] = resting_exit.get((str(ex_o), held_side), 0.0) + max(0.0, rem)
        done = 0
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for (ex, side), q in held.items():
            if done >= write_budget or q < 1 or ex not in fair:
                continue
            pos = positions.get(ex, 0.0)
            q = int(min(q, -pos if side == "no" else pos) - resting_exit.get((ex, side), 0.0))   # never close more than held, net of resting exits
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
        except ApiError as e:
            log.warning("open orders: %s", e)
        return out

    def ingest_fills(self) -> dict[str, int]:
        """Pull fills newest-first until we hit ones we already have. Returns per-exchange total fill counts."""
        new = 0; cursor = None; pages = 0
        while pages < 5:
            try:
                page = self.api.fills(self.slug, 200, cursor)
            except ApiError as e:
                log.warning("fills: %s", e)
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
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
                resp = self.api.place_order(ex, side, "buy", q, px, expiration=ttl, key=self.api.new_key("lag"))
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
        orders = self.inside.cycle(books, positions, exclude=set(self.mm.quoted), blocked=blocked, prices=prices)
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

    def churn_cycle(self, positions: dict, killed: bool, cycle_id=None):
        """Deviation churn (proposals/dev_churn.py), "high-frequency arb": exits first (+2 ticks/set, 24 h; the stop or a
        6-tick loss per set sells at the touch), reconciled against the sets actually held (sets another path closed are
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
            uq = self.arb.unwind_quote(race, books, held) or quote_down(race, books, held, 1)[1]   # thin asks: what part fetches
            proceeds = uq["proceeds"] if uq else None
            lots = [l for l in ch.lots if l["race"] == rname]
            avg = {"cost": sum(l["qty"] * l["cost"] for l in lots) / held, "ts": min(l["ts"] for l in lots)}
            if proceeds is not None:
                open_value += held * (proceeds - avg["cost"])
            plans.append((race, books, held, avg, proceeds))
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
                if rname in cheapfav_races_of(self):
                    continue                                   # cheap-favourite NO legs are not sets: held to the call
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
        if not killed and not ch.killed and now <= CHURN_LAST_ENTRY and not sell_all:
            own = own_levels(self)
            for race in self.universe.races.values():
                if not ch.eligible(race, now) or race.name in self.arb.hostile or self.arb.cooldown.get(race.name, 0) > now:
                    continue
                if race.name in cheapfav_races_of(self):
                    continue                                   # cheap-favourite race: the two ledgers stay apart
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
                q = ch.entry_qty(best, self.cash, min_qty)
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
        if changed:
            try:
                tmp = self.churn_state_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(ch.to_json(), f)
                os.replace(tmp, self.churn_state_path)
            except OSError as e:
                log.warning("churn state: %s", e)

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
        """Positions the MM manages: the venue's, minus the catch-up sweep's and the cheap-favourites sleeve's own open
        positions (the MM used to count them as inventory and skew to unload them before they converged; review 23 Oct)."""
        cu = dict(getattr(self, "catchup_open", None) or {})
        for ex, q in (getattr(self, "cheapfav_open", None) or {}).items():   # held to the call: not MM inventory either
            cu[ex] = cu.get(ex, 0.0) + q
        if not cu:
            return positions
        return {ex: q - cu.get(ex, 0.0) for ex, q in positions.items()}

    def mm_touch(self, ex: str):
        """Freshest (best bid, best ask) for the MM's last look before it sends quotes; None when no fresh book."""
        bk = self.fresh_book(ex, 10.0)
        if not bk:
            return None
        return ((bk.get("bids") or [{}])[0].get("price") if bk.get("bids") else None,
                (bk.get("asks") or [{}])[0].get("price") if bk.get("asks") else None)

    def catchup_cycle(self, dir_summary: dict, positions: dict, prices: dict, killed: bool):
        """Catch-up sweep trial (proposals/outside_sweep.py): exits first (convergence, 12 h stop, outside disagreement or
        reversal, kill, 3 Nov flatten), then entries on a fresh directional poll. Positions come from the fills table
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
        self.catchup_open = dict(open_pos)           # the MM excludes these from its inventory
        if not cs.killed and cs.check_kill(pnl):
            log.warning("CATCHUP: sleeve P&L %.1f at or below %.0f: switched off; open positions are being closed", pnl, cs.kill_pnl)
        resting = {str(r["exchange_id"]) for r in self.store.rows(
            "SELECT exchange_id FROM orders WHERE purpose IN ('catchup','catchup_exit') AND status='open' AND placed_at > ?", (now - 90,))}
        cu_ids = {str(r["order_id"]) for r in self.store.rows("SELECT order_id FROM orders WHERE purpose IN ('catchup','catchup_exit') AND placed_at > ?",
                                                             (now - 6 * 3600,))}
        for ex_o, lst in (self.open_orders_cache or {}).items():
            if any(str(o.get("id")) in cu_ids for o in lst):
                resting.add(str(ex_o))                 # the venue still shows one of our catchup orders resting here
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        changed = False
        for ex, q in open_pos.items():
            if ex in resting:
                continue
            why = cs.exit_reason(ex, byex.get(ex), now)
            if not why:
                continue
            bk = self.fresh_book(ex, 30.0)
            if not bk:
                try:
                    bk = self.get_book(ex, 5)          # REST read: never price an exit off a stale direction row
                except ApiError as e:
                    log.warning("catchup exit book %s: %s", ex, e)
                    continue
            bid = bk["bids"][0]["price"] if bk.get("bids") else None
            ask = bk["asks"][0]["price"] if bk.get("asks") else None
            if q > 0 and bid is None or q < 0 and ask is None:
                log.warning("CATCHUP exit %s waits: no %s in the book (%s)", ex, "bid" if q > 0 else "ask", why)
                continue
            side, px = ("no", round(1 - bid, 3)) if q > 0 else ("yes", round(ask, 3))
            try:
                resp = self.api.place_order(ex, side, "buy", int(abs(q)), px, expiration=ttl, key=self.api.new_key("cux"))
                self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, int(abs(q)), "catchup_exit", response=resp)
                log.info("CATCHUP exit %s %s x%d @%.3f (%s)", ex, side, int(abs(q)), px, why)
            except ApiError as e:
                log.warning("catchup exit %s: %s", ex, e)
        for ex in list(cs.ctx):
            if ex not in open_pos and ex not in resting and now - cs.ctx[ex].get("ts", now) > 300:
                cs.ctx.pop(ex)                      # flat and settled: forget the entry context
                changed = True
        if not killed and not cs.killed and dir_summary.get("polled"):
            sigs = cs.signals(rows, now)
            if sigs:
                expo = risk.exposure(self.universe, positions, prices)
                n_open = len(open_pos) + len([e for e in resting if e not in open_pos])
                for s in sigs:
                    ex = s["exchange_id"]
                    if ex in open_pos or ex in resting or ex in cs.ctx:
                        continue
                    bk = self.fresh_book(ex, 10.0)
                    if not bk or not bk.get("bids") or not bk.get("asks"):
                        continue
                    b0, a0 = bk["bids"][0], bk["asks"][0]
                    if a0["price"] - b0["price"] > 2 * TICK + 1e-9:
                        continue
                    s = dict(s, bid=b0["price"], ask=a0["price"])
                    side, px, q = cs.size(s, (a0 if s["d"] > 0 else b0)["quantity"])
                    if not q:
                        continue
                    if not cs.can_enter(n_open, risk.adds(self.universe, ex, side), expo.net_d_notional, q * px,
                                        cap=self.cfg.mm.portfolio_max_partisan_notional):
                        continue
                    try:
                        resp = self.api.place_order(ex, side, "buy", q, px, expiration=ttl, key=self.api.new_key("cu"))
                        self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, q, "catchup", response=resp)
                    except ApiError as e:
                        log.warning("catchup entry %s: %s", ex, e)
                        continue
                    adds_dir = risk.adds(self.universe, ex, side)
                    expo.net_d_notional += q * px if adds_dir == "D" else (-q * px if adds_dir == "R" else 0.0)   # next signal sees it
                    cs.ctx[ex] = {"ts": now, "d": s["d"], "f_then": s["f_then"], "move": s["move"], "mid0": s["mid"], "gap": s["gap"]}
                    cs.last_signal[s["race"]] = now
                    n_open += 1
                    changed = True
                    log.info("CATCHUP enter %s %s %s x%d @%.3f: outside moved %+.3f, SIG gap %.1f ticks", s["race"], ex, side, q, px,
                             s["move"], s["gap"] / TICK)
        if changed or cs.killed:
            try:
                tmp = self.catchup_state_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"ctx": cs.ctx, "last_signal": cs.last_signal, "killed": cs.killed}, f)
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
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lock = getattr(self, "trade_lock", None)
        if lock is None:
            self._cheapfav_trade(cf, now, rows, byex, race_of, open_pos, held, deployed, cf_pend, resting, ttl, positions, prices, killed, dir_summary)
            return
        with lock:                                   # the scanner thread trades sets between cycles: read its snapshot here
            self._cheapfav_trade(cf, now, rows, byex, race_of, open_pos, held, deployed, cf_pend, resting, ttl, positions, prices, killed, dir_summary)

    def _cheapfav_trade(self, cf, now, rows, byex, race_of, open_pos, held, deployed, cf_pend, resting, ttl, positions, prices, killed, dir_summary):
        positions = getattr(self, "positions_snapshot", None) or positions
        # one exit per leg per 5 min, filled or not: if the fill ingest fails after an exit filled, the ledger still shows
        # the holding and a second exit would flip it (review 24 Oct); a reversed edge is not urgent
        exited = {str(r["exchange_id"]) for r in self.store.rows(
            "SELECT exchange_id FROM orders WHERE purpose='cheapfav_exit' AND placed_at > ?", (now - 300,))}
        for ex, q in open_pos.items():
            if ex in resting or ex in exited or ex not in race_of:
                continue
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
                resp = self.api.place_order(ex, side, "buy", n, px, expiration=ttl, key=self.api.new_key("cfx"))
                self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, n, "cheapfav_exit", response=resp)
                log.info("CHEAPFAV exit %s %s x%d @%.3f (%s)", ex, side, n, px, why)
            except ApiError as e:
                log.warning("cheapfav exit %s: %s", ex, e)
        if killed or not dir_summary.get("polled") or now > cf.last_entry or not positions:
            return                                   # no positions (read failed): every holding guard would be blind
        race_legs = {r.name: list(r.exchange_ids) for r in self.universe.races.values()}
        ch = getattr(self, "churn", None)               # races the high-frequency arb holds sets in: its ledger books what
        skip = getattr(self, "cheapfav_skip", None) or {}
        # the catch-up sweep has priority: never trade a race where it holds a position or an entry context
        cu_ex = set(getattr(self, "catchup_open", None) or {}) | set(getattr(getattr(self, "catchup", None), "ctx", None) or {})
        cu_races = {self.universe.by_exchange[e][0] for e in cu_ex if e in self.universe.by_exchange}
        cands = [c for c in cf.candidates(rows, positions, race_legs) if c["exchange_id"] not in resting   # the venue shows,
                 and not (ch is not None and ch.held(c["race"])) and skip.get(c["exchange_id"], 0) <= now   # keep them apart
                 and c["race"] not in cu_races]
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
                                                   "('catchup','direction','tilt','inside') AND (placed_at > ? OR status='open')", (now - 330,))]
        others = [o for o in others if float(o.get("placed_at") or 0) > now - 330 or str(o.get("order_id")) in resting_ids]
        o_filled: dict = {}
        if others:
            for f in self.store.rows("SELECT order_id, quantity FROM fills WHERE purpose IN ('catchup','direction','tilt','inside') "
                                     "AND ingested_at > ?", (now - 6 * 3600,)):
                o_filled[str(f["order_id"])] = o_filled.get(str(f["order_id"]), 0.0) + abs(float(f["quantity"]))
        pend_d, pend_r = cf_pend[1], cf_pend[2]
        for o in others:
            left = float(o["quantity"]) - o_filled.get(str(o.get("order_id")), 0.0)
            d = risk.adds(self.universe, str(o["exchange_id"]), o["side"])
            if left >= 1 and d in ("D", "R"):
                n = left * float(o["price"])
                pend_d, pend_r = (pend_d + n, pend_r) if d == "D" else (pend_d, pend_r + n)
        orders = cf.entries(cands, held, deployed, self.cash, now, touch, positions, expo.net_d_notional,
                            cap=self.cfg.mm.portfolio_max_partisan_notional, pend=(pend_d, pend_r), own=cf_pend,
                            blocked=set(getattr(getattr(self, "mm", None), "blocked", None) or set()))   # the bot's block this cycle
        for o in orders:
            try:
                resp = self.api.place_order(o["exchange_id"], o["side"], "buy", o["qty"], o["price"], expiration=ttl, key=self.api.new_key("cf"))
                self.store.record_order(resp.get("orderId"), o["exchange_id"], None, o["side"], "buy", o["price"], o["qty"], "cheapfav", response=resp)
                if resp.get("orderId") is None:          # its fill cannot be attributed: never buy this leg again blind
                    self.cheapfav_skip = dict(getattr(self, "cheapfav_skip", None) or {}, **{o["exchange_id"]: now + 3600})
                    log.error("CHEAPFAV entry %s: no orderId in the response; leg skipped for 1 h", o["exchange_id"])
                self.cash -= o["notional"]
                log.info("CHEAPFAV enter %s %s (%s %s) x%d @%.3f: fair %.3f, edge %.1fc", o["race"], o["win_party"], o["exchange_id"], o["side"],
                         o["qty"], o["price"], o["p_win"], 100 * (o["p_win"] - o["price"]))
            except ApiError as e:
                log.warning("cheapfav entry %s: %s", o["exchange_id"], e)

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
            ttl = (datetime.now(timezone.utc) + timedelta(seconds=en.cfg.ttl_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for o in orders:
                log.info("ELECTION %s %s %s x%s at %s: %s", o.get("kind"), o.get("exchangeId"), o.get("side"), o.get("quantity"), o.get("price"), o.get("reason"))
                if self.dry_run:
                    continue
                try:
                    with self.trade_lock:
                        resp = self.api.place_order(str(o["exchangeId"]), o["side"], "buy", int(o["quantity"]), float(o["price"]),
                                                    expiration=ttl, key=self.api.new_key("elec"))
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
                room = getattr(self.tilt, "free_last", 0.0) if opp.get("kind") == "tilt" else self.cash * 0.9
                if self.cash >= self.cfg.arb.cash_reserve or room >= opp["capital"] or time.time() - funded.get(okey, 0) < 1800:
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
                if len(exs) >= max(4, n // 2):
                    self.mm.quoted = exs[:n]
                    log.info("mm markets by tracker edge: %d chosen (top %s)", len(self.mm.quoted), [(e, round(sc, 2)) for e, sc in ranked[:5]])
                    return self.mm.quoted
            except Exception as e:
                log.warning("tracker rank: %s", e)
        return self.mm.select_markets(prices, self.volume)

    # --------------------------------------------------------------- realtime feed
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
        # Read cash and positions together under the trade lock: an arb basket executed by the scanner thread between
        # the two reads (or between them and the health check) made the account look ~1,100 lower for one cycle and
        # tripped the drawdown kill switch on 22 Oct 15:39. cycle_cash is the cash that matches pos_value.
        with self.trade_lock:
            try:
                acct = self.api.account()
                self.cash = float(acct.get("balance") or self.cash)
            except ApiError as e:
                log.warning("account: %s", e)
            positions, pos_value, n_pos = self.positions_map()
            cycle_cash = self.cash
            cycle_adj = getattr(self, "settle_adj", 0.0)   # positions_map() set it from these positions; later calls
                                                          # (scanner thread, recycle backstop) move it, so keep this one
            # read by the scanner thread (free unwinds between cycles); set under the lock: set after it, a scanner unwind
            # (and its in-lock refresh) in between was overwritten by these older positions
            self.positions_snapshot = positions
        open_orders = self.open_orders_map()
        self.open_orders_cache = open_orders
        self.open_orders_ts = time.time()
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
        if self.cfg.arb.enabled and self.cfg.arb.new_sets and self.scanner is None:
            cands = self.arb.candidates_from_prices(prices)
            arb_summary["candidates"] = len(cands)
            cand_books = {}
            for race in cands[: self.cfg.arb_books_per_cycle]:
                try:
                    cand_books[race.name] = {ex: self.get_book(ex, 50) for ex in race.exchange_ids}
                except ApiError as e:
                    log.warning("book %s: %s", race.name, e)
            plan = self.arb.plan(cand_books, self.cash)
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
                    else None)   # the scanner recycles itself when the feed is up; cash mode and kill pauses: no backstop
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
                    rec = self.arb.recycle(best, held_books, self.positions_snapshot, self.cash, cycle_id, refresh=self.fresh_positions)
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
                            plan = [c for c in self.arb.plan({tgt: fresh}, self.cash, extra_budget=rec.get("qty", 0) * rec.get("proceeds", 0.0),
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
        hl = self.health(cycle_cash + pos_value + cycle_adj, cycle_id)
        killed = time.time() < self.killed_until
        if not killed and (hl["drawdown"] > self.cfg.kill_drawdown or (hl["n_markout"] >= 20 and hl["markout30"] is not None and hl["markout30"] < self.cfg.kill_markout_ticks)):
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
        mm_summary = {}
        self.mm.xv_fair = {r["exchange_id"]: (r.get("fair"), r.get("pm"), r.get("kalshi"), r.get("ts") or time.time())
                           for r in (getattr(self.direction, "rows", None) or []) if r.get("fair") is not None and r.get("exchange_id")}
        gate_ready = self.mm_gate_ready(killed)
        if not gate_ready and not killed:
            log.info("MM waits for the first outside-fair poll before quoting (xv gate)")
        if self.cfg.mm.enabled and not killed and gate_ready:
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
                except ApiError as e:
                    log.warning("book %s: %s", ex, e)
            write_budget = max(4, self.cfg.mm_write_budget - 4)   # leave room for the tilt sleeve
            mm_summary = self.mm.quote_cycle(books, self.mm_positions(positions), self.pressures(), open_orders, fills_count, self.cash, cycle_id, write_budget)

        # ---- inside-spread quoting where the DMM's spread is 3+ ticks (exploit 2.2), outside the MM's own markets
        inside_summary = {}
        if self.inside is not None and not killed and self.feed_ok():
            try:
                inside_summary = self.inside_cycle(positions, prices, blocked, cycle_id)
            except Exception as e:
                log.exception("inside quoter: %s", e)

        # ---- tilt harvest (tails only, disjoint from the MM band)
        tilt_summary = {}
        if self.cfg.tilt.enabled and not killed:
            tilt_summary = self.tilt.cycle(prices, positions, open_orders, set(self.mm.quoted), self.cash, cycle_id,
                                           max(2, self.cfg.mm_write_budget - (mm_summary.get("writes", 0) if mm_summary else 0)))

        # ---- news sweeper (feeds polled every news.poll_sec; taker orders only when --live)
        news_summary = {}
        if self.news is not None:
            try:
                news_summary = self.news.cycle(prices, self.cash, cycle_id)
            except Exception as e:                 # the sweeper must never kill the loop
                log.exception("news cycle failed: %s", e)

        # ---- directional sleeve: explained legs only, small size (orders only if direction.allow_live)
        dir_summary = {}
        if not killed:
            dir_summary = self.direction.cycle(prices, positions, self.cash, write_budget=2, dry_run=self.dry_run)
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

        # ---- cheap safe favourites: buy fast-count favourites SIG prices >= 2c under the outside fair, hold to the call
        if self.cheapfav is not None:
            try:
                self.cheapfav_cycle(dir_summary, positions, prices, killed)
            except Exception as e:                   # the sleeve must never kill the loop
                log.exception("cheapfav cycle failed: %s", e)

        # ---- volatile moves: pause the MM's retrace side after a 5+ tick move (verified), paper-log the move trades
        if self.feed_ok():
            try:
                self.volatile_cycle()
            except Exception as e:
                log.warning("volatile moves: %s", e)

        # ---- outside-move follower: take the DMM touch when Polymarket/Kalshi moved and SIG has not (exploit 2.1)
        if self.lag is not None and not killed:
            try:
                self.lag_cycle(positions, blocked)
            except Exception as e:
                log.exception("lag follower: %s", e)

        # ---- zero-EV exits (directional / tilt) and free arb-set unwinds
        if not killed:
            self.zero_ev_exits(positions, getattr(self.direction, "rows", []))
            if self.arb.committed and time.time() - self.last_free_unwind > self.cfg.free_unwind_every_sec:
                self.last_free_unwind = time.time()
                held_books = {}
                cf_races = cheapfav_races_of(self)       # a cheap-favourite NO next to another sleeve's NO is not a set
                for rname in list(self.arb.committed):
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
        while not self.stop:
            start = time.time()
            try:
                self.cycle()
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
