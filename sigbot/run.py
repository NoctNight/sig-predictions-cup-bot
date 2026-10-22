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
from .arb import ArbEngine
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

    def signals(self, rows, ts):
        allow = self._allowed()
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
                b.feed.wait_update(b.cfg.arb_scan_sec_feed)
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
        min_cost = 1.0
        cands = [r for r in cands if b.arb.race_budget_left(r.name) >= b.cfg.arb.min_set_qty * min_cost
                 and apparent(r) >= min(b.cfg.arb.min_edge_sell, b.cfg.arb.min_edge_buy)]
        if b.arb.budget_left(b.cash) < b.cfg.arb.min_set_qty * min_cost and not b.cfg.arb.recycle_enabled:
            cands = []
        cands.sort(key=apparent, reverse=True)
        cand_books, n_rest = {}, 0
        for race in cands[: (b.cfg.arb_scan_books_feed if feed_ok else b.cfg.arb_scan_books)]:
            fb = {ex: b.fresh_book(ex, 30.0) for ex in race.exchange_ids}
            if all(v is not None for v in fb.values()):
                cand_books[race.name] = fb      # every leg current on the feed: no read spent
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
            allc = [c for c in allc if c.qty >= b.cfg.arb.min_set_qty]
            if allc:
                best = max(allc, key=lambda c: c.ret)
                self.best_unfunded = {"race": best.race, "kind": best.kind, "ret": best.ret, "cost": best.cost, "qty": best.qty, "ts": time.time()}
            plan = b.arb.plan(cand_books, b.cash)
            if not plan:
                return
            log.info("scanner arb plan: %s", [(c.race, c.kind, c.qty, round(c.edge, 4), round(100 * c.ret, 2)) for c in plan])
            res = b.arb.execute(plan, None)
            n = sum(1 for r in res if r.get("ok"))
            if self.best_unfunded and any(not r.get("ok") and r["chunk"].race == self.best_unfunded["race"] for r in res):
                self.best_unfunded["failed"] = True
            self.stats["executed"] += n
            if n:
                try:
                    b.cash = float(b.scan_api.account().get("balance") or b.cash)
                    b.positions_snapshot = b.positions_map()[0]
                except ApiError:
                    pass

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
                    summ = b.mm.quote_cycle({ex: fb}, b.positions_snapshot, b.pressures(), {ex: b.open_orders_cache.get(ex, [])},
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
        if b.arb.budget_left(b.cash) >= best["cost"] * max(b.cfg.arb.min_set_qty, 1):
            return                                  # not capital-blocked: the normal scanner path takes it
        tgt = best["race"]
        target = {ex: b.fresh_book(ex, 10.0) for ex in b.universe.races[tgt].exchange_ids}
        if any(v is None for v in target.values()):
            return
        held_books = {}
        for rname in list(b.arb.committed):
            race = b.universe.races.get(rname)
            if race is None or rname == tgt:
                continue
            fb = {ex: b.fresh_book(ex, 30.0) for ex in race.exchange_ids}
            if all(v is not None for v in fb.values()):
                held_books[rname] = fb
        if not held_books:
            return
        with b.trade_lock:
            rec = b.arb.recycle(dict(best, fresh_books=target), held_books, b.positions_snapshot, b.cash, None)
        if not rec:
            return
        self.stats["recycles"] = self.stats.get("recycles", 0) + 1
        self.best_unfunded = None
        if rec.get("dry"):
            return
        try:
            b.cash = float(b.scan_api.account().get("balance") or b.cash)
            fresh = {ex: b.get_book(ex, 50, b.scan_api, max_age=3.0) for ex in b.universe.races[tgt].exchange_ids}
            with b.trade_lock:
                plan = [c for c in b.arb.plan({tgt: fresh}, b.cash) if c.race == tgt]
                if plan:
                    res = b.arb.execute(plan, None)
                    log.info("recycle follow-through %s: %d basket(s) filled", tgt, sum(1 for r in res if r.get("ok")))
                else:
                    log.warning("recycle: target %s gone after unwind; capital stays free for the scanner", tgt)
            b.cash = float(b.scan_api.account().get("balance") or b.cash)
            b.positions_snapshot = b.positions_map()[0]
        except ApiError as e:
            log.warning("recycle follow-through %s: %s", tgt, e)

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
        for rname in list(b.arb.committed):
            race = b.universe.races.get(rname)
            if race is None:
                continue
            fb = {ex: b.fresh_book(ex, 30.0) for ex in race.exchange_ids}
            if all(v is not None for v in fb.values()):
                held[rname] = fb
        if not held:
            return
        b.last_free_unwind = time.time()
        with b.trade_lock:
            fu = b.arb.free_unwinds(held, b.positions_snapshot, None)
        if fu:
            log.info("free unwinds (scanner): %s", fu)
            try:
                b.cash = float(b.scan_api.account().get("balance") or b.cash)
                b.positions_snapshot = b.positions_map()[0]
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
        self.mm = MarketMaker(self.api, self.store, cfg.mm, self.universe, self.slug, cfg.starting_cash, dry_run, HERE)
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
        peak = self.store.one("SELECT MAX(total_value) m FROM cycles WHERE ts>=?", (now - 3600,))
        self.peak_value = max(float(peak["m"] or 0.0), total_value)
        h["drawdown"] = self.peak_value - total_value
        return h

    def zero_ev_exits(self, positions: dict[str, float], dir_rows: list, write_budget: int = 2) -> int:
        """Close directional and tilt holdings once this venue's price is within exit_band of the outside fair value:
        the edge is gone, the exit costs ~nothing in expectation and frees the collateral. Arb-set legs are untouched
        (only the sleeve's own filled quantity is closed). Exits cross the spread with a 60 s limit order."""
        band = self.cfg.exit_band
        fair = {r["exchange_id"]: r for r in (dir_rows or []) if r.get("fair") is not None and r.get("sig_bid") is not None and r.get("sig_ask") is not None}
        held = {}
        for r in self.store.rows("SELECT exchange_id, side, SUM(ABS(quantity)) q FROM fills WHERE purpose IN ('direction','tilt') GROUP BY 1,2"):
            held[(r["exchange_id"], r["side"])] = held.get((r["exchange_id"], r["side"]), 0.0) + r["q"]
        for r in self.store.rows("SELECT exchange_id, side, SUM(ABS(quantity)) q FROM fills WHERE purpose='exit' GROUP BY 1,2"):
            opp = "no" if r["side"] == "yes" else "yes"      # an exit buying YES closed held NO, and vice versa
            held[(r["exchange_id"], opp)] = held.get((r["exchange_id"], opp), 0.0) - r["q"]
        done = 0
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for (ex, side), q in held.items():
            if done >= write_budget or q < 1 or ex not in fair:
                continue
            pos = positions.get(ex, 0.0)
            q = int(min(q, -pos if side == "no" else pos))   # never close more than the venue position on that side
            if q < 1:
                continue
            f = fair[ex]
            if side == "no" and f["sig_ask"] <= f["fair"] + band:        # held NO: buy YES at the ask to close
                order = ("yes", round(f["sig_ask"], 3))
            elif side == "yes" and f["sig_bid"] >= f["fair"] - band:     # held YES: buy NO at 1 - bid to close
                order = ("no", round(1 - f["sig_bid"], 3))
            else:
                continue
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
    def positions_map(self) -> tuple[dict[str, float], float, int]:
        try:
            p = self.api.positions(self.slug)
        except ApiError as e:
            log.warning("positions: %s", e)
            return {}, 0.0, 0
        m = {str(x["exchangeId"]): float(x["quantity"]) for x in p.get("positions", []) if not x.get("settled")}
        self.cost_basis = {str(x["exchangeId"]): float(x.get("costBasis") or 0.0) for x in p.get("positions", []) if not x.get("settled")}
        # arb capital already locked, per race, derived from positions (survives restarts): NO legs' cost basis
        committed = {}
        for race in self.universe.races.values():
            qs = [m.get(l.exchange_id, 0.0) for l in race.legs.values()]
            if qs and all(q < 0 for q in qs):           # a held set: NO on every leg
                committed[race.name] = sum(self.cost_basis.get(l.exchange_id, 0.0) for l in race.legs.values())
        self.arb.committed = committed
        return m, float(p.get("summary", {}).get("totalMarketValue", 0.0)), len(m)

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
            log.info("OFFLOAD%s: exit %s %s x%d (gives up %.2f%%) to fund %s (%.2f%% net), gain %.2f%%", "" if live else " (report-only)",
                     u.get("race") or u.get("exchange_id"), u.get("side", "set"), u["qty"], 100 * u["forgone_ret"],
                     r["opportunity"].get("race") or r["opportunity"].get("exchange_id"), 100 * r["opportunity"]["ret_net"], 100 * r["gain"])
            if live and u["kind"] == "arb_set" and r["opportunity"].get("kind") in ("tilt", "direction") and done < 1:
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
                race = self.universe.races.get(rname)
                books = {ex: self.fresh_book(ex, 10.0) for ex in race.exchange_ids} if race else {}
                if race and all(v is not None for v in books.values()):
                    q = int(min(u["qty"], math.ceil(opp["capital"] / max(u["proceeds"], 0.05))))
                    with self.trade_lock:
                        res = self.arb.unwind_set(rname, books, q, why=f"fund {opp['kind']} {opp.get('exchange_id')}",
                                                  new_ret=opp["ret_net"], min_gain=self.cfg.arb.recycle_min_gain)
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
            if not live or u["kind"] == "arb_set" or done >= 2 or not u.get("order"):
                continue
            side, px = u["order"]
            try:
                resp = self.api.place_order(u["exchange_id"], side, "buy", int(u["qty"]), float(px), expiration=ttl, key=self.api.new_key("offload"))
                self.store.record_order(resp.get("orderId"), u["exchange_id"], None, side, "buy", float(px), int(u["qty"]), "exit", response=resp)
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
    def feed_ok(self) -> bool:
        return self.feed is not None and self.feed.healthy()

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
        self.positions_snapshot = positions      # read by the scanner thread (free unwinds between cycles)
        open_orders = self.open_orders_map()
        self.open_orders_cache = open_orders
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
        if self.cfg.arb.enabled and self.scanner is None:
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
            best = self.scanner.best_unfunded
            if best and time.time() - best["ts"] < 120 and self.cfg.arb.recycle_enabled and self.arb.committed and not best.get("failed"):
                try:
                    best = dict(best, fresh_books={ex: self.get_book(ex, 50) for ex in self.universe.races[best["race"]].exchange_ids})
                except ApiError:
                    best = None
            if best and time.time() - best["ts"] < 120 and self.cfg.arb.recycle_enabled and self.arb.committed and not best.get("failed"):
                held_books = {}
                for rname in list(self.arb.committed)[:3]:
                    try:
                        held_books[rname] = {ex: self.get_book(ex, 50) for ex in self.universe.races[rname].exchange_ids}
                    except ApiError as e:
                        log.warning("recycle book %s: %s", rname, e)
                with self.trade_lock:
                    rec = self.arb.recycle(best, held_books, positions, self.cash, cycle_id)
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
                            plan = [c for c in self.arb.plan({tgt: fresh}, self.cash) if c.race == tgt]
                            if plan:
                                res = self.arb.execute(plan, cycle_id)
                                arb_summary["recycle_funded"] = sum(1 for r in res if r.get("ok"))
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
        hl = self.health(cycle_cash + pos_value, cycle_id)
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
        if self.cfg.mm.enabled and not killed:
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
            mm_summary = self.mm.quote_cycle(books, positions, self.pressures(), open_orders, fills_count, self.cash, cycle_id, write_budget)

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
                for rname in list(self.arb.committed):
                    try:
                        held_books[rname] = {ex: self.get_book(ex, 50) for ex in self.universe.races[rname].exchange_ids}
                    except ApiError as e:
                        log.warning("free-unwind book %s: %s", rname, e)
                with self.trade_lock:
                    fu = self.arb.free_unwinds(held_books, positions, cycle_id)
                if fu:
                    log.info("free unwinds: %s", fu)

        # ---- analytics refresh
        if time.time() - self.last_analytics > self.cfg.analytics_every_sec:
            try:
                out = compute_overrides(self.store, self.cfg)
                path = os.path.join(HERE, self.cfg.mm.overrides_file)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    json.dump(out, f, indent=1)
                self.last_analytics = time.time()
            except Exception as e:  # analytics must never kill the loop
                log.exception("analytics failed: %s", e)
        if time.time() - self.last_universe_refresh > 3600:
            try:
                self.universe = Universe.from_markets(self.api.markets())
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
