"""Ladder market maker for binary contracts.

Per quoted exchange, each cycle:
  1. fair       = microprice of the touch (bid/ask weighted by opposite-side size), nudged by order-flow pressure
  2. reserve    = fair shifted against race-level inventory (log-odds Avellaneda-Stoikov style, expressed in ticks)
  3. ladder     = `levels` bids and asks at level_ticks[k] from reserve, sizes = base * level_size_mult[k] * scale,
                  with sizes INCREASING with depth ("decreasing volume as we get tighter"), then skewed:
                  side that would add to inventory gets size * (1 - size_skew), side hit by flow pressure shrinks.
  4. prices never cross the touch; on wide spreads (>= improve_when_spread_ticks_ge) level 1 improves the touch
     by one tick, otherwise it joins the queue at the touch.
  5. requote only when needed (fair moved >= requote_ticks, a fill happened, TTL elapsed, orders missing):
     cancel-all scoped to the exchange (1 write) + one batch per 50 orders (1 write each).
  6. a known book with nothing to post (the partisan block leaves one side and the outside-fair gate closes it, or the
     inventory cap zeroes both sides) pulls our resting quotes there (pull_quotes): a quote left resting fills on a side
     we no longer want (sim 'day' 29 Oct: a bid rested 19 min on a gated side). An unknown book never cancels.
  7. the main cycle polls the outside fair AFTER this block; on legs where the new fair closed a side (gate_closed) the
     resting quotes are pulled in the same cycle (Bot.mm_gate_sweep -> pull_closed), not a cycle later (sim 'catchup'
     29 Oct); nothing is posted there, the next cycle requotes the open side.

Everything placed is logged with level/ticks-from-mid/fair/imbalance/inventory so that fills can be attributed
and the analytics module can re-size the ladder from realized markouts and fill rates.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .api import ApiError, SuperMarketClient
from .config import MMConfig, PRICE_MAX, PRICE_MIN, TICK, round_tick, ticks
from .store import Store
from .universe import Universe
from . import risk

log = logging.getLogger("sigbot.mm")


@dataclass
class QuoteState:
    exchange_id: str
    last_reserve: Optional[float] = None
    last_quote_ts: float = 0.0
    fills_seen: int = 0
    last_gate: tuple = (True, True)


@dataclass
class Quote:
    exchange_id: str
    market_id: Optional[str]
    side: str          # yes (bid) | no (ask expressed as NO buy)
    price: float       # price we SEND (YES price for bids, NO price for asks)
    yes_price: float   # YES-denominated resting price
    qty: int
    level: int
    ticks_from_mid: float


class MarketMaker:
    hot_until: dict = {}
    hot_anchor: dict = {}

    def __init__(self, api: SuperMarketClient, store: Store, cfg: MMConfig, universe: Universe, slug: str,
                 starting_cash: float, dry_run: bool = True, root: str = "."):
        self.api, self.store, self.cfg, self.u, self.slug = api, store, cfg, universe, slug
        self.starting_cash, self.dry_run, self.root = starting_cash, dry_run, root
        self.state: dict[str, QuoteState] = {}
        self.overrides: dict = {}
        self.overrides_mtime = 0.0
        self.quoted: list[str] = []
        self.vol: dict[str, float] = {}      # per exchange hourly sigma of YES price (abs)
        self.blocked: set = set()            # partisan directions that may not be added to (set each cycle)
        self.wave: bool = False              # market-wide volatility wave in progress
        self.prices: dict = {}               # latest bulk price snapshot (all legs), set each cycle for race-consistency caps
        self.meanrev_pred: dict[str, float] = {}   # exchange -> predicted next-5-min YES change (set each cycle)
        self.vol5m: dict[str, float] = {}    # exchange -> sigma of 5-minute close changes (from candle history)

    # -------------------------------------------------------------- selection
    def select_markets(self, prices: dict[str, dict], volume_by_ex: dict[str, float]) -> list[str]:
        cands = []
        for ex in self.u.all_exchanges():
            p = prices.get(ex)
            if not p or p["bestBid"] is None or p["bestAsk"] is None:
                continue
            mid = 0.5 * (p["bestBid"] + p["bestAsk"])
            if not (self.cfg.min_price <= mid <= self.cfg.max_price):
                continue
            cands.append((volume_by_ex.get(ex, 0.0), ex))
        cands.sort(reverse=True)
        self.quoted = [ex for _, ex in cands[: self.cfg.n_markets]]
        return self.quoted

    def load_overrides(self):
        path = os.path.join(self.root, self.cfg.overrides_file)
        try:
            m = os.path.getmtime(path)
            if m != self.overrides_mtime:
                with open(path) as f:
                    self.overrides = json.load(f)
                self.overrides_mtime = m
                log.info("loaded mm overrides for %d exchanges", len(self.overrides.get("exchanges", {})))
        except FileNotFoundError:
            self.overrides = {}
        except ValueError as e:                      # half-written file: keep the previous overrides, retry next time
            log.warning("mm overrides unreadable (%s); keeping the previous set", e)

    # --------------------------------------------------------------- pricing
    @staticmethod
    def microprice(book: dict, min_size: float = 100.0, cap: float = 3000.0) -> tuple[Optional[float], Optional[float], float]:
        """Mid, microprice and touch imbalance. Sizes come from the first level on each side with at least `min_size`
        shares and are capped at `cap`, so a single large flickering order cannot swing our fair value."""
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            return None, None, 0.0
        bb, ba = bids[0]["price"], asks[0]["price"]
        qb = min(cap, next((l["quantity"] for l in bids if l["quantity"] >= min_size), bids[0]["quantity"]))
        qa = min(cap, next((l["quantity"] for l in asks if l["quantity"] >= min_size), asks[0]["quantity"]))
        mid = 0.5 * (bb + ba)
        imb = (qb - qa) / (qb + qa) if (qb + qa) > 0 else 0.0
        micro = (bb * qa + ba * qb) / (qb + qa) if (qb + qa) > 0 else mid
        return mid, micro, imb

    def race_inventory(self, ex: str, positions: dict[str, float]) -> float:
        """Inventory of this leg net of offsetting legs in the same race, in YES shares."""
        race = self.u.race_of(ex)
        if race is None:
            return positions.get(ex, 0.0)
        mine = positions.get(ex, 0.0)
        others = [positions.get(l.exchange_id, 0.0) for p, l in race.legs.items() if l.exchange_id != ex]
        # YES on another leg is (approximately) NO on this leg
        return mine - (sum(others) / len(others) if others else 0.0)

    def hours_to_end(self) -> float:
        end = datetime.fromisoformat(self.cfg.stop_quoting_at.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600.0)

    def vol_regime(self, ex: str, mid: float) -> bool:
        """True while the market is 'hot': a >= vol_gate_move_ticks move over the last 15 minutes, or 1h realised variance
        above vol_gate_var_mult x the hourly baseline. Hot status persists vol_gate_cooldown_sec after the last trigger and
        remembers the pre-move mid as an anchor for the reducing side."""
        if self.store is None:
            return False
        now = time.time()
        rows = self.store.rows("SELECT ts, mid FROM quotes WHERE exchange_id=? AND ts>=? ORDER BY ts ASC", (ex, now - 3600))
        trig = False
        if rows:
            older = [r for r in rows if r["ts"] <= now - 900]
            ref = older[-1]["mid"] if older else rows[0]["mid"]
            if ref is not None and abs(mid - ref) >= self.cfg.vol_gate_move_ticks * TICK - 1e-9:
                trig = True
                self.hot_anchor.setdefault(ex, ref)
            # realised variance over the hour from mids sampled at >= 5-minute spacing (no bid-ask flicker),
            # against 12 x the 5-minute variance from candle history
            samp, last_t = [], None
            for r in rows:
                if r["mid"] is None:
                    continue
                if last_t is None or r["ts"] - last_t >= 290:
                    samp.append(r["mid"]); last_t = r["ts"]
            samp.append(mid)
            d = [b - a for a, b in zip(samp, samp[1:])]
            if len(d) >= 4:
                var1h = sum(x * x for x in d)
                base = 12 * (self.vol5m.get(ex, 0.004) ** 2)
                if base > 0 and var1h > self.cfg.vol_gate_var_mult * base:
                    trig = True
                    self.hot_anchor.setdefault(ex, samp[0])
        if trig:
            self.hot_until[ex] = now + self.cfg.vol_gate_cooldown_sec
        hot = self.hot_until.get(ex, 0) > now
        if not hot:
            self.hot_anchor.pop(ex, None)
        return hot

    def news_paused(self) -> dict:
        """exchange -> pause-until timestamp, written by sigbot.news (params key 'news_pause'); expired entries dropped."""
        if self.store is None:
            return {}
        now = time.time()
        with self.store.lock:                        # read-modify-write shared with news and shock mode
            raw = self.store.get_param("news_pause", {}) or {}
            live = {ex: float(t) for ex, t in raw.items() if float(t) > now}
            if len(live) != len(raw):
                self.store.set_param("news_pause", live)
        return live

    def paused_sides(self, ex: str) -> set:
        """Sides whose level 1 is paused: 3+ same-side fills within same_side_pause_sec (one-sided sweep in progress)."""
        if self.store is None:
            return set()
        out = set()
        since = datetime.now(timezone.utc) - timedelta(seconds=self.cfg.same_side_pause_sec)
        rows = self.store.rows("SELECT side, COUNT(*) AS n FROM fills WHERE exchange_id=? AND purpose='mm' AND filled_at>=? GROUP BY side",
                               (ex, since.strftime("%Y-%m-%dT%H:%M:%SZ")))
        for r in rows:
            if r["n"] >= 3:
                out.add(r["side"])
        rp = getattr(self, "retrace_pause", {}).get(ex)
        if rp and rp[1] > time.time():
            out.add(rp[0])
        return out

    def market_id_of(self, ex: str) -> Optional[str]:
        r = self.u.by_exchange.get(ex)
        return self.u.races[r[0]].legs[r[1]].market_id if r else None

    def gate_for(self, ex: str, mid: float) -> tuple:
        """The outside-fair gate (allow bid, allow ask) for `ex` at `mid`: xv_gate on the leg's fresh Polymarket/Kalshi fair
        (xv_fair, set by the main cycle), open when there is none or the gate is off. One place, so that build_quotes and
        gate_changed agree."""
        xv = getattr(self, "xv_fair", {}).get(ex)
        if not xv or self.cfg.xv_gate_ticks <= 0:
            return (True, True)
        from .proposals.mm_upgrade import xv_gate
        ok_bid, ok_ask = xv_gate(mid, xv[0], xv[1], xv[2], time.time() - xv[3], gap_ticks=self.cfg.xv_gate_ticks)
        return (ok_bid, ok_ask)

    def gate_closed(self, books: dict) -> dict:
        """The quoted legs on which the gate, at the book's mid and the current xv_fair, has CLOSED a side that was open
        when their resting quotes were built (QuoteState.last_gate): ex -> (old gate, new gate). Legs never quoted (or
        forced to requote by a wave) and legs with an unknown mid are left out; a side that only opened is left to the
        next cycle's requote."""
        out = {}
        for ex in self.quoted:
            st, bk = self.state.get(ex), books.get(ex)
            if st is None or st.last_reserve is None or not bk:
                continue
            mid = self.microprice(bk)[0]
            if mid is None:
                continue
            new, old = self.gate_for(ex, mid), tuple(st.last_gate)
            if (old[0] and not new[0]) or (old[1] and not new[1]):
                out[ex] = (old, new)
        return out

    def pull_closed(self, books: dict, open_orders: dict, budget: int) -> dict:
        """Right after the outside-fair poll (Bot.mm_gate_sweep): on every quoted leg where the fresh fair closed a side that
        was open when its quotes were built, pull our quotes resting on that side now; nothing is posted here, the next
        cycle requotes as usual. When the leg is all ours (and no other sleeve placed an order there since the list was
        read) that is one cancel-all; otherwise only our orders on the closed side go, one by one, and the open side keeps
        quoting. `open_orders` is the cycle's list, read before this cycle's quotes: own_resting adds those from the store.
        Returns {legs, pulled, writes, skipped_budget}."""
        closed = self.gate_closed(books)
        out = {"legs": sorted(closed), "pulled": [], "writes": 0, "skipped_budget": 0}
        for ex, (old, new) in closed.items():
            sides = ({"yes"} if old[0] and not new[0] else set()) | ({"no"} if old[1] and not new[1] else set())
            oo = open_orders.get(ex, [])
            mine = self.own_resting(ex, oo, dedupe=True)
            gone = [o for o in mine if o.get("side") in sides]
            if not gone:
                continue
            if out["writes"] >= budget:
                out["skipped_budget"] += 1
                continue
            n, clean = self.pull_quotes(ex, oo, mine, budget - out["writes"], only=gone)
            out["writes"] += n
            out["pulled"].append(ex)
            log.info("MM gate sweep pulled %d resting order(s) on %s: the poll closed %s (gate %s -> %s, clean %s)", len(gone), ex, sorted(sides), old, new, clean)
            if clean:
                self.state[ex].last_gate = new       # the closed side is empty; a leg left empty is re-posted by the next cycle
        return out

    def build_quotes(self, ex: str, book: dict, positions: dict[str, float], pressure: float, per_market_budget: float) -> tuple[list[Quote], dict]:
        mid, micro, imb = self.microprice(book)
        if mid is None:
            return [], {}
        bb, ba = book["bids"][0]["price"], book["asks"][0]["price"]
        spread_ticks = ticks(ba) - ticks(bb)
        ov = (self.overrides.get("exchanges", {}) or {}).get(ex, {})
        pressure = float(ov.get("pressure", pressure))
        level_mult = list(ov.get("level_size_mult") or self.cfg.level_size_mult)
        if len(level_mult) != len(self.cfg.level_size_mult):   # overrides from a different ladder shape: ignore them
            level_mult = list(self.cfg.level_size_mult)
        spread_add = int(ov.get("spread_add_ticks", 0))
        size_scale_ov = float(ov.get("size_scale", 1.0))

        # 1. fair: microprice tilted by flow pressure (pressure>0 = buyers hitting asks -> fair up)
        mr = max(-self.cfg.meanrev_max_ticks * TICK, min(self.cfg.meanrev_max_ticks * TICK, self.cfg.meanrev_weight * self.meanrev_pred.get(ex, 0.0)))
        fair = micro + pressure * self.cfg.pressure_skew_ticks * TICK + mr

        # 2. reservation price: shift against race-level inventory (notional at risk / max)
        inv = self.race_inventory(ex, positions)
        inv_notional = inv * (mid if inv > 0 else (1 - mid))
        # the wind-down shrinks the cap from 1 Nov through inv_cap_override (the shared config, which the shadow tuner's replays
        # read, stays as set)
        cap = getattr(self, "inv_cap_override", None) or self.cfg.max_inventory_notional
        inv_frac = max(-1.0, min(1.0, inv_notional / cap))
        # log-odds AS term: shift grows with sigma^2 * T; sigma is the hourly YES-price sigma, T hours (capped)
        sigma = self.vol.get(ex, 0.01)
        T = min(24.0, self.hours_to_end())
        as_shift_ticks = self.cfg.gamma * inv_frac * (sigma / TICK) ** 2 * T / 100.0
        shift_ticks = max(-self.cfg.inventory_skew_ticks_at_max, min(self.cfg.inventory_skew_ticks_at_max,
                          inv_frac * self.cfg.inventory_skew_ticks_at_max + as_shift_ticks))
        reserve = fair - shift_ticks * TICK

        # 2b. volatility gate: when hot, pull the inventory-adding side, halve the reducing side, widen, and anchor the
        #     reducing side at the pre-move mid (sell into a reversion rather than at the new low)
        hot = self.vol_regime(ex, mid)
        if self.wave:
            spread_add += self.cfg.wave_spread_add
        if hot:
            spread_add += self.cfg.vol_gate_spread_add
            anchor = self.hot_anchor.get(ex)
            if inv > 0:        # long: no bids; asks anchored at max(reserve, anchor)
                if anchor is not None:
                    reserve = max(reserve, anchor)
            elif inv < 0:      # short: no asks; bids anchored at min(reserve, anchor)
                if anchor is not None:
                    reserve = min(reserve, anchor)

        # 3. sizes
        p_yes = mid
        base_bid = [self.cfg.base_qty * m for m in level_mult]
        base_ask = list(base_bid)
        if inv_frac > 0:    # long: shrink bids, grow asks
            base_bid = [q * (1 - self.cfg.size_skew_at_max * inv_frac) for q in base_bid]
            base_ask = [q * (1 + 0.5 * self.cfg.size_skew_at_max * inv_frac) for q in base_ask]
        elif inv_frac < 0:
            base_ask = [q * (1 - self.cfg.size_skew_at_max * -inv_frac) for q in base_ask]
            base_bid = [q * (1 + 0.5 * self.cfg.size_skew_at_max * -inv_frac) for q in base_bid]
        if pressure > 0:    # buyers lifting offers: our asks are the ones getting picked off
            base_ask = [q * (1 - self.cfg.pressure_size_skew * min(1.0, pressure)) for q in base_ask]
        elif pressure < 0:
            base_bid = [q * (1 - self.cfg.pressure_size_skew * min(1.0, -pressure)) for q in base_bid]
        # hard per-race stop at the inventory cap, and portfolio partisan block
        if self.cfg.hard_inventory_stop:
            if inv_frac >= 1.0:
                base_bid = [0.0 for _ in base_bid]
            elif inv_frac <= -1.0:
                base_ask = [0.0 for _ in base_ask]
        if risk.adds(self.u, ex, "yes") in self.blocked:
            base_bid = [0.0 for _ in base_bid]
        if risk.adds(self.u, ex, "no") in self.blocked:
            base_ask = [0.0 for _ in base_ask]
        if hot:
            # the adding side is the fade side (buy after a down sweep / sell after an up sweep): reversion data says it
            # pays passively, so it keeps vol_gate_fade_mult of its size; the reducing side is anchored at the pre-move mid
            if inv > 0:
                base_bid = [q * self.cfg.vol_gate_fade_mult for q in base_bid]
                base_ask = [q * self.cfg.vol_gate_size_mult for q in base_ask]
            elif inv < 0:
                base_ask = [q * self.cfg.vol_gate_fade_mult for q in base_ask]
                base_bid = [q * self.cfg.vol_gate_size_mult for q in base_bid]
            else:
                base_bid = [q * self.cfg.vol_gate_size_mult for q in base_bid]
                base_ask = [q * self.cfg.vol_gate_size_mult for q in base_ask]
        if self.wave:
            base_bid = [q * self.cfg.wave_size_mult for q in base_bid]
            base_ask = [q * self.cfg.wave_size_mult for q in base_ask]
        bid_notional = sum(q * p_yes for q in base_bid) or 1.0
        ask_notional = sum(q * (1 - p_yes) for q in base_ask) or 1.0
        half = 0.5 * per_market_budget
        sb = min(1.0, half / bid_notional) * size_scale_ov
        sa = min(1.0, half / ask_notional) * size_scale_ov

        # race consistency: our bid on this leg plus the other legs' best bids must stay below 1 (else selling YES on
        # every leg into our bid and the book is a riskless arb against us); symmetric for asks
        bid_cap, ask_floor = PRICE_MAX, PRICE_MIN
        race = self.u.race_of(ex)
        if race is not None and self.prices:
            others = [l.exchange_id for l in race.legs.values() if l.exchange_id != ex]
            ob = [self.prices.get(o, {}).get("bestBid") for o in others]
            oa = [self.prices.get(o, {}).get("bestAsk") for o in others]
            if others and all(v is not None for v in ob):
                bid_cap = math.floor((1.0 - TICK - sum(ob)) / TICK + 1e-9) * TICK
            if others and all(v is not None for v in oa):
                ask_floor = math.ceil((1.0 + TICK - sum(oa)) / TICK - 1e-9) * TICK
        quotes: list[Quote] = []
        market_id = self.market_id_of(ex)
        n_levels = self.cfg.levels if self.hours_to_end() >= 24 else min(self.cfg.levels, 4)
        paused = self.paused_sides(ex)
        # outside-fair gate (research_notes/mm_upgrade.md, verified on realised fills): when the fresh Polymarket/Kalshi
        # fair sits >= xv_gate_ticks away from our mid, stop taking the other side of that convergence on every level
        gate = self.gate_for(ex, mid)
        if not gate[0]:
            paused = set(paused) | {"yes_all"}
        if not gate[1]:
            paused = set(paused) | {"no_all"}
        for k, dticks in enumerate(self.cfg.level_ticks[:n_levels]):
            dt = dticks + spread_add
            improve = TICK if (k == 0 and spread_ticks >= self.cfg.improve_when_spread_ticks_ge) else 0.0
            bid_px = min(round_tick(reserve - dt * TICK), round_tick(bb + improve), round_tick(ba - TICK), round(bid_cap, 3))
            qb = int(base_bid[k] * sb)
            if (k == 0 and "yes" in paused) or "yes_all" in paused:
                qb = 0
            if qb >= 1 and PRICE_MIN <= bid_px <= PRICE_MAX:
                quotes.append(Quote(ex, market_id, "yes", bid_px, bid_px, qb, k + 1, (mid - bid_px) / TICK))
            ask_px = max(round_tick(reserve + dt * TICK), round_tick(ba - improve), round_tick(bb + TICK), round(ask_floor, 3))
            qa = int(base_ask[k] * sa)
            if (k == 0 and "no" in paused) or "no_all" in paused:
                qa = 0
            if qa >= 1 and PRICE_MIN <= ask_px <= PRICE_MAX:
                quotes.append(Quote(ex, market_id, "no", round_tick(1 - ask_px), ask_px, qa, k + 1, (ask_px - mid) / TICK))
        merged: dict[tuple, Quote] = {}
        for q in quotes:   # levels may collapse onto one tick after capping: merge
            key = (q.side, q.price)
            if key in merged:
                merged[key].qty += q.qty
            else:
                merged[key] = q
        quotes = list(merged.values())
        info = dict(gate=gate, mid=mid, micro=micro, imb=imb, fair=fair, reserve=reserve, inv=inv, inv_frac=inv_frac, pressure=pressure, hot=hot, mr=mr,
                    spread_ticks=spread_ticks, shift_ticks=shift_ticks, bb=bb, ba=ba, bid_qty=book["bids"][0]["quantity"],
                    ask_qty=book["asks"][0]["quantity"], last=book.get("latestPrice"))
        return quotes, info

    # ------------------------------------------------------------- requoting
    def needs_requote(self, ex: str, info: dict, fills_on_ex: int, open_orders_on_ex: int, expected_orders: int,
                      stale_side: bool = False) -> bool:
        st = self.state.setdefault(ex, QuoteState(ex))
        now = time.time()
        if st.last_reserve is None:
            return True
        if stale_side:
            return True                          # one of our quotes rests on a side the ladder no longer has (a gate, the
                                                 # partisan block or the inventory cap closed it since): requote at once
        if info.get("gate", (True, True)) != st.last_gate:
            return True                          # the outside-fair gate opened or closed a side: requote at once
        if abs(info["reserve"] - st.last_reserve) >= self.cfg.requote_ticks * TICK - 1e-9:
            return True
        if fills_on_ex > st.fills_seen:
            return True
        if now - st.last_quote_ts > self.cfg.refresh_sec:
            return True
        if open_orders_on_ex < expected_orders:
            return True
        return False

    def own_resting(self, ex: str, oo: list, dedupe: bool = False) -> list:
        """Our resting orders on `ex`: the orders among `oo` (the venue's open orders there) that are the MM's or the inside
        quoter's. With `dedupe` (a caller whose list may be older than what it did since: the fast path's cache, the gate
        sweep) the orders the store already holds as cancelled are left out, so a cancel is not repeated, and our orders
        the store holds open that were placed after the list was read (this cycle's quotes) are added, so they can be
        pulled. A fresh venue list is trusted as it is (a cancel-all that answered OK but left an order resting is retried)."""
        if self.store is None:
            return []
        out, seen = [], set()
        for o in oo:
            meta = self.store.order_meta(o.get("id")) if o.get("id") is not None else None
            if meta is None or meta["purpose"] not in ("mm", "inside"):
                continue
            if dedupe and meta["status"] == "cancelled":
                continue
            out.append(o)
            seen.add(str(o.get("id")))
        if dedupe:
            since = float(getattr(self, "open_orders_ts", 0.0) or 0.0)
            for r in self.store.rows("SELECT order_id, side FROM orders WHERE exchange_id=? AND purpose IN ('mm', 'inside') AND status='open' "
                                     "AND placed_at>=? ORDER BY placed_at", (str(ex), since)):
                if str(r["order_id"]) not in seen:
                    out.append({"id": r["order_id"], "side": r["side"]})
                    seen.add(str(r["order_id"]))
        return out

    def mark_ours_cancelled(self, ex: str):
        """After an exchange-scoped cancel-all: every order of ours the store still holds open on the leg is off the venue now,
        listed or not (this cycle's quotes, an earlier fast requote's), so mark them all; an unmarked one is a phantom a later
        pull would spend a cancel on (review 2, finding 1)."""
        self.store.exec("UPDATE orders SET status='cancelled' WHERE exchange_id=? AND purpose IN ('mm', 'inside') AND status='open' AND placed_at<=?",
                        (str(ex), time.time()))

    def pull_quotes(self, ex: str, oo: list, mine: list, budget: int, only: Optional[list] = None) -> tuple[int, bool]:
        """Pull our resting quotes `mine` on `ex` (a subset of `oo`, the venue's open orders there): one exchange-scoped
        cancel-all when every order the venue lists there is ours and no other sleeve placed an order there since the list
        was read (the check cancel_mm_quotes makes), otherwise order by order, so that an exit, a catch-up entry or an
        order the store does not know is left alone. `mine` may hold orders the list lacks (this cycle's quotes, from the
        store). With `only` (a subset of `mine`: the orders on a side that just closed), the order-by-order pull cancels
        those alone and the rest keep resting; the cancel-all, when allowed, pulls everything of ours (one write; the next
        cycle re-posts the open side). Returns (writes spent, clean): clean when every targeted order was cancelled without
        an error; what the budget did not reach, and a cancel that failed, is pulled next cycle."""
        if budget < 1:
            return 0, False
        since = float(getattr(self, "open_orders_ts", 0.0) or 0.0) - 60.0
        newer = self.store.one("SELECT 1 FROM orders WHERE exchange_id=? AND purpose NOT IN ('mm', 'inside') AND placed_at>=? LIMIT 1",
                               (str(ex), since))
        ours = {str(o.get("id")) for o in mine}

        def listed_ours(o):                      # ours whatever the status: an id of ours we cancelled earlier is no stranger
            if str(o.get("id")) in ours:
                return True
            meta = self.store.order_meta(o.get("id")) if o.get("id") is not None else None
            return meta is not None and meta["purpose"] in ("mm", "inside")
        if all(listed_ours(o) for o in oo) and newer is None:
            if self.dry_run:
                log.info("[DRY] pull: cancel-all exchange %s (%d orders)", ex, len(mine))
                return 1, True
            try:
                self.api.cancel_all(exchange_id=ex)
                self.store.mark_orders([o["id"] for o in mine], "cancelled")
                self.mark_ours_cancelled(ex)
                return 1, True
            except Exception as e:               # a network error (not every one is an ApiError) must not abort the cycle
                log.warning("pull cancel-all %s failed: %s", ex, e)
                return 1, False
        writes, clean = 0, True
        for o in (mine if only is None else only):
            if writes >= budget:
                clean = False
                break
            if self.dry_run:
                log.info("[DRY] pull: cancel order %s on %s", o.get("id"), ex)
            else:
                try:
                    self.api.cancel_order(o["id"])
                    self.store.mark_orders([o["id"]], "cancelled")
                except Exception as e:
                    if getattr(e, "status", None) in (404, 409, 410):
                        # the venue has no open order by that id (filled, expired or cancelled since): nothing rests, so
                        # mark it; otherwise it would be retried on every pass until the next open-orders read (review 2)
                        self.store.mark_orders([o["id"]], "cancelled")
                        log.info("pull cancel %s on %s: gone (%s)", o.get("id"), ex, e)
                    else:
                        log.warning("pull cancel %s on %s failed: %s", o.get("id"), ex, e)
                        clean = False
            writes += 1
        return writes, clean

    @staticmethod
    def last_look(to_place: list, touch_fn, stats: Optional[dict] = None) -> list:
        """Quotes (q, info) that do not cross the freshest touch. touch_fn(ex) -> (best bid, best ask) or None; one look
        per exchange (it may cost a REST read while the feed is down); an unknown touch drops the quote instead of
        sending it unchecked (sim 'day' 24 Oct: a bid went into the 0.76 ask during the feed outage)."""
        kept, looked = [], {}
        for q, info in to_place:
            if q.exchange_id not in looked:
                looked[q.exchange_id] = touch_fn(q.exchange_id)
            t = looked[q.exchange_id]
            st = stats if stats is not None else {}
            if t is None:
                st["unknown"] = st.get("unknown", 0) + 1
                continue
            bb, ba = t
            if (q.side == "yes" and ba is not None and q.yes_price >= ba - 1e-9) or \
               (q.side == "no" and bb is not None and q.yes_price <= bb + 1e-9):
                st["crossing"] = st.get("crossing", 0) + 1
                continue
            kept.append((q, info))
        return kept

    def quote_cycle(self, books: dict[str, dict], positions: dict[str, float], pressures: dict[str, float],
                    open_orders: dict[str, list], fills_count: dict[str, int], cash: float, cycle_id: int,
                    write_budget: int) -> dict:
        self.load_overrides()
        summary = {"requoted": [], "orders": 0, "writes": 0, "skipped_budget": 0}
        if self.hours_to_end() <= 0:
            if any(open_orders.values()) and not self.dry_run:
                # review S2b: never an account-wide cancel while punt legs rest orders (the punt executor's chunks and the
                # election-night sleeve's); then exchange by exchange, the protected legs skipped
                prot = set(getattr(self, "protected_fn", None) and self.protected_fn() or ())
                if prot & {ex for ex, lst in open_orders.items() if lst}:
                    for ex, lst in open_orders.items():
                        if lst and ex not in prot:
                            self.api.cancel_all(exchange_id=ex)
                            summary["writes"] += 1
                else:
                    self.api.cancel_all()
                    summary["writes"] += 1
            log.info("past stop_quoting_at; not quoting")
            return summary
        budget_total = min(self.cfg.capital_frac * self.starting_cash, cash * 0.9)
        per_market = budget_total / max(1, len(self.quoted))
        now_ts = time.time()
        n_hot = sum(1 for ex in self.quoted if self.hot_until.get(ex, 0) > now_ts)
        was_wave = self.wave
        self.wave = len(self.quoted) > 0 and n_hot / len(self.quoted) >= self.cfg.wave_hot_frac
        if self.wave != was_wave:
            log.warning("volatility wave %s: %d of %d quoted markets hot", "ON" if self.wave else "off", n_hot, len(self.quoted))
            for st in self.state.values():
                st.last_reserve = None   # force a requote at the new sizes
        summary["wave"] = self.wave
        to_place: list[tuple[Quote, dict]] = []
        writes = 0
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=self.cfg.order_ttl_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        paused = self.news_paused()
        # legs of a strategy the stops just paused (strategy_stops.py quarantine): the MM keeps off them; only its own
        # resting quotes there are pulled, order by order (never cancel_all on the leg: exits and raise clips rest there)
        quar = {ex for ex, until in (getattr(self, "quarantine", None) or {}).items() if until > now_ts}
        raising = {ex: t for ex, t in (getattr(self, "raise_pause", None) or {}).items() if t > now_ts}
        start = (cycle_id or 0) % max(1, len(self.quoted))          # rotate so the same markets are not always starved by the budget
        for ex in self.quoted[start:] + self.quoted[:start]:
            if ex in quar:
                for o in open_orders.get(ex, []):
                    if writes >= write_budget:
                        break
                    meta = self.store.order_meta(o.get("id")) if self.store is not None and o.get("id") is not None else None
                    if meta is None or meta["purpose"] not in ("mm", "inside"):
                        continue
                    if not self.dry_run:
                        try:
                            self.api.cancel_order(o["id"])
                            self.store.mark_orders([o["id"]], "cancelled")
                        except Exception as e:           # a network error must not abort this quote cycle
                            log.warning("quarantine cancel %s on %s failed: %s", o.get("id"), ex, e)
                    writes += 1
                summary.setdefault("quarantined", []).append(ex)
                continue
            if ex in raising:
                # the cash raise is selling this leg: no quotes here (they would buy it back); pull any still resting
                oo = open_orders.get(ex, [])
                if oo and writes < write_budget and not self.dry_run:
                    try:
                        self.api.cancel_all(exchange_id=ex)
                        pulled = [o["id"] for o in oo]
                        self.store.mark_orders(pulled, "cancelled")
                    except ApiError as e:
                        log.warning("raise pause cancel-all %s failed: %s", ex, e)
                    writes += 1
                summary.setdefault("raise_paused", []).append(ex)
                continue
            if ex in paused:
                # news sweeper took liquidity on this race: pull our quotes and stay out until the pause ends, then come back hot
                oo = open_orders.get(ex, [])
                self.hot_until[ex] = max(self.hot_until.get(ex, 0), paused[ex] + self.cfg.vol_gate_cooldown_sec)
                if oo and writes < write_budget:
                    if self.dry_run:
                        log.info("[DRY] news pause: cancel-all exchange %s (%d orders)", ex, len(oo))
                    else:
                        try:
                            self.api.cancel_all(exchange_id=ex)
                            self.store.mark_orders([o["id"] for o in oo], "cancelled")
                        except ApiError as e:
                            log.warning("news pause cancel-all %s failed: %s", ex, e)
                    writes += 1
                summary.setdefault("news_paused", []).append(ex)
                continue
            book = books.get(ex)
            if not book:
                continue
            quotes, info = self.build_quotes(ex, book, positions, pressures.get(ex, 0.0), per_market)
            oo = open_orders.get(ex, [])
            # our orders resting on the leg; a caller with a list that may be older than a cancel it sent (the fast path's
            # cache, the gate sweep: force_cancel set) does not repeat that cancel
            mine = self.own_resting(ex, oo, dedupe=bool(getattr(self, "force_cancel", False)))
            if not quotes:
                if not info:
                    continue                 # a side of the book is missing: the leg is unknown this cycle, never cancel blind
                # a known book with nothing to post: the partisan block left one side and the outside-fair gate closed it
                # (sim 'day' 29 Oct, Nevada Governor 872: bids only from 10:05, the bid side gated from 10:22, the resting
                # bid swept at 10:24), or the inventory cap / the budget zeroed both sides. Pull our resting quotes (one
                # cancel-all when nothing else rests there, else order by order) and, once the leg is clean, record the
                # gate, so that the re-open is a gate change needs_requote acts on at once. Nothing is recorded while a
                # quote of ours may still rest (a failed or partial pull, a list that may be stale): the next cycle's fresh
                # list pulls it, or requotes through the stale-side trigger. The quotes row keeps the vol-regime history.
                self.store.record_quote(cycle_id, ex, info["bb"], info["ba"], info["bid_qty"], info["ask_qty"], info.get("last"),
                                        info["mid"], info["micro"], info["imb"], info["fair"], info["inv"], info["pressure"])
                if not mine:
                    continue
                if writes >= write_budget:
                    summary["skipped_budget"] += 1
                    continue
                n, clean = self.pull_quotes(ex, oo, mine, write_budget - writes)
                writes += n
                summary.setdefault("pulled", []).append(ex)
                log.info("MM pulled %d resting order(s) on %s: no quotable side (gate %s, bid/ask %.3f/%.3f, inv %.0f, clean %s)", len(mine), ex,
                         info.get("gate"), info["bb"], info["ba"], info["inv"], clean)
                if clean:
                    st = self.state.setdefault(ex, QuoteState(ex))
                    st.last_reserve, st.last_quote_ts, st.fills_seen = info["reserve"], time.time(), fills_count.get(ex, 0)
                    st.last_touch = (info["bb"], info["ba"])
                    st.last_gate = info.get("gate", (True, True))
                continue
            self.store.record_quote(cycle_id, ex, info["bb"], info["ba"], info["bid_qty"], info["ask_qty"], info.get("last"),
                                    info["mid"], info["micro"], info["imb"], info["fair"], info["inv"], info["pressure"])
            sides = {q.side for q in quotes}
            stale = any(o.get("side") in ("yes", "no") and o.get("side") not in sides for o in mine)
            if not self.needs_requote(ex, info, fills_count.get(ex, 0), len(oo), len(quotes), stale_side=stale):
                continue
            if writes + 1 + math.ceil((len(to_place) + len(quotes)) / self.cfg.batch_size) > write_budget:
                summary["skipped_budget"] += 1
                continue
            tf = getattr(self, "touch_fn", None)
            if tf is not None and tf(ex) is None:
                # no touch to check new quotes against: keep the resting ones rather than cancel and go dark (review 24 Oct)
                summary["skipped_unknown"] = summary.get("skipped_unknown", 0) + 1
                continue
            if oo or getattr(self, "force_cancel", False):
                if self.dry_run:
                    log.info("[DRY] cancel-all exchange %s (%d orders)", ex, len(oo))
                else:
                    try:
                        self.api.cancel_all(exchange_id=ex)
                        self.store.mark_orders([o["id"] for o in oo], "cancelled")
                        self.mark_ours_cancelled(ex)     # also this cycle's quotes the list lacks (review 2, finding 1)
                    except ApiError as e:
                        log.warning("cancel-all %s failed: %s", ex, e)
                        continue
                writes += 1
            st = self.state.setdefault(ex, QuoteState(ex))
            st.last_reserve, st.last_quote_ts, st.fills_seen = info["reserve"], time.time(), fills_count.get(ex, 0)
            st.last_touch = (info["bb"], info["ba"])
            st.last_gate = info.get("gate", (True, True))
            to_place.extend((q, info) for q in quotes)
            summary["requoted"].append(ex)
        # last look before sending: the touch can move inside the cycle (another sleeve's taker order, a DMM requote),
        # and a resting bid at or above the current ask fills at once as a taker (sim 'day' 23 Oct: bid 0.76 into a
        # 0.75/0.76 book). Drop any quote that would cross the freshest book.
        # One look per exchange (the touch_fn may cost a REST read when the feed is down). An unknown touch drops the
        # quote rather than sending it unchecked (sim 'day' 24 Oct, feed outage).
        touch_fn = getattr(self, "touch_fn", None)
        summary["dropped_crossing"] = summary["dropped_unknown"] = 0
        for i in range(0, len(to_place), self.cfg.batch_size):
            chunk = to_place[i : i + self.cfg.batch_size]
            if touch_fn is not None and chunk:
                # looked right before each batch is sent, so a look is never several REST reads old (review 24 Oct)
                st = {}
                chunk = self.last_look(chunk, touch_fn, st)
                summary["dropped_crossing"] += st.get("crossing", 0)
                summary["dropped_unknown"] += st.get("unknown", 0)
                if not chunk:
                    continue
            orders = [dict(exchangeId=q.exchange_id, side=q.side, action="buy", quantity=q.qty, price=q.price, expirationDate=ttl,
                           tournamentId=self.api.tournament_id) for q, _ in chunk]
            if self.dry_run:
                for q, info in chunk:
                    log.info("[DRY] quote %s %s px=%.3f q=%d yes_px=%.3f lvl=%d ticks=%.1f fair=%.4f inv=%.0f pres=%+.2f", q.exchange_id,
                             "BID" if q.side == "yes" else "ASK", q.price, q.qty, q.yes_price, q.level, q.ticks_from_mid, info["fair"],
                             info["inv"], info["pressure"])
                summary["orders"] += len(chunk)
                continue
            try:
                resp = self.api.place_batch(orders)
            except ApiError as e:
                log.warning("batch failed: %s", e)
                continue
            writes += 1
            for r in resp.get("results", []):
                q, info = chunk[r["index"]]
                d = r.get("data") or {}
                if r.get("ok") and d.get("orderId"):
                    self.store.record_order(d["orderId"], q.exchange_id, q.market_id, q.side, "buy", q.price, q.qty, "mm", q.level,
                                            q.ticks_from_mid, info["fair"], info["mid"], info["imb"], info["inv"], cycle_id, ttl,
                                            "open" if d.get("open") else "filled", d)
                    summary["orders"] += 1
                elif not r.get("ok"):
                    log.info("order rejected %s: %s", q.exchange_id, str(d)[:200])
        summary["writes"] = writes
        return summary
