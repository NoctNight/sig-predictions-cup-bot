"""Adversarial paper simulator: can a counter-bot that watches our public behaviour bleed us?

    python3 -m sigbot.adversary --attacks all --cycles 200
    python3 -m sigbot.adversary --attacks sweep,flipflop --cycles 50 --dmm thin
    python3 -m sigbot.adversary --attacks all --cycles 200 --mitigations none      # baseline only

Nothing here touches the network, the live bot or data/sigbot.sqlite. The venue is a price-time-priority limit order
book per exchange with a designated-market-maker (DMM) ladder: `touch_qty` shares one tick either side of the
anchor, growing by `growth` per tick (default 2.4k -> ~170k ten ticks deep, the shape measured in data/books.json),
refilled at the same prices after every trade (what the quotes table shows: the touch moves 0.4x/hour and refills at
the same price 91% of the time) with an optional `recenter` share of the last print.

Our side is the real code: `sigbot.mm.MarketMaker.build_quotes` / `quote_cycle`, `sigbot.tilt.TiltHarvester.cycle`,
`sigbot.arb.ArbEngine.plan/execute` and `sigbot.analytics.compute`, instantiated on a Universe from data/markets.json
with the touch of every market seeded from data/books.json. They talk to a fake API that places orders into the paper
book and to an in-memory `Store` (sqlite ':memory:', no file), so the volatility gate, the same-side pause, the tilt
per-market cap and the markout analytics all run on the real SQL. Module clocks (`time`, `datetime.now`) are patched
to the simulated clock. The bot cycle is 30 s (fast hours); an attack cycle is one or two bot cycles.

Accounting: every owner (US, ATK, DMM) has cash and signed YES positions; P&L is cash plus positions marked at the
DMM anchor mid, i.e. the resting fair of a zero-drift world, so the only P&L is what the attacker extracts from us
or pays the DMM. Attacks are run with and without the proposed mitigations (`Mitig` flags); each is a parameter change
or a small rule that could be applied in run.py/mm.py.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import time as _time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import analytics as an_mod
from . import arb as arb_mod
from . import mm as mm_mod
from . import risk
from . import store as store_mod
from . import tilt as tilt_mod
from .api import ApiError
from .arb import ArbEngine
from .config import TICK, BotConfig
from .mm import MarketMaker
from .store import Store
from .tilt import TiltHarvester
from .universe import Universe

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
US, ATK, DMM = "US", "ATK", "DMM"
BOT_CYCLE_SEC = 30.0


def T(p: float) -> int:
    return int(round(p / TICK))


def P(t: int) -> float:
    return round(t * TICK, 3)


# ----------------------------------------------------------------------------------------------------- clock patch
class SimClock:
    def __init__(self, t0: float):
        self.t = t0

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float):
        self.t += s

    def advance(self, s: float):
        self.t += s


_REAL = {m: (m.time, getattr(m, "datetime", None)) for m in (mm_mod, tilt_mod, store_mod, an_mod, arb_mod)}


def install_clock(clock: SimClock):
    class SimDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            d = datetime.fromtimestamp(clock.time(), timezone.utc)
            return d if tz is not None else d.replace(tzinfo=None)

    for m in (mm_mod, tilt_mod, store_mod, an_mod, arb_mod):
        m.time = clock
    mm_mod.datetime = SimDateTime
    tilt_mod.datetime = SimDateTime


def restore_clock():
    for m, (t, dt) in _REAL.items():
        m.time = t
        if dt is not None:
            m.datetime = dt


# ------------------------------------------------------------------------------------------------------ order book
@dataclass
class Order:
    id: int
    owner: str
    ex: str
    side: str            # B = buy YES, S = sell YES (a NO buy in venue terms)
    tick: int
    qty: int
    ts: float
    expiry: Optional[float] = None
    tag: str = ""


class Book:
    def __init__(self):
        self.bids: dict[int, list[Order]] = {}
        self.asks: dict[int, list[Order]] = {}
        self.last: Optional[int] = None

    def levels(self, side: str) -> dict[int, list[Order]]:
        return self.bids if side == "B" else self.asks

    def best(self, side: str) -> Optional[int]:
        ticks = [t for t, l in self.levels(side).items() if l]
        if not ticks:
            return None
        return max(ticks) if side == "B" else min(ticks)

    def qty(self, side: str, tick: int, owner: Optional[str] = None) -> int:
        return sum(o.qty for o in self.levels(side).get(tick, []) if owner is None or o.owner == owner)

    def snapshot(self, depth: int = 10) -> dict:
        bids = sorted(((t, sum(o.qty for o in l)) for t, l in self.bids.items() if l), reverse=True)[:depth]
        asks = sorted(((t, sum(o.qty for o in l)) for t, l in self.asks.items() if l))[:depth]
        return {"bids": [{"price": P(t), "quantity": q} for t, q in bids],
                "asks": [{"price": P(t), "quantity": q} for t, q in asks],
                "latestPrice": P(self.last) if self.last is not None else None}


@dataclass
class DmmLadder:
    bid: int
    ask: int
    q0: float
    growth: float
    depth: int
    recenter: float = 0.0
    real_bids: Optional[list] = None     # per-level quantities from data/books.json (dmm=real), else synthetic
    real_asks: Optional[list] = None

    def target(self, k: int, side: str = "B") -> int:
        real = self.real_bids if side == "B" else self.real_asks
        if real:
            return int(real[k]) if k < len(real) else int(real[-1] * self.growth ** (k - len(real) + 1))
        return int(self.q0 * self.growth ** k)

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


class Venue:
    """Paper venue: books, DMM ladders, accounts. Owner US is our bot (fills are pushed into its Store)."""

    def __init__(self, clock: SimClock):
        self.clock = clock
        self.books: dict[str, Book] = {}
        self.dmm: dict[str, DmmLadder] = {}
        self.orders: dict[int, Order] = {}
        self.accounts: dict[str, dict] = {}
        self.next_id = 1
        self.fills: list[dict] = []
        self.on_us_fill = None                   # callback(order, tick, qty)
        self.flows: dict[str, dict[str, float]] = {}   # owner -> tag -> cash flow (for per-sleeve P&L)
        self.pos_by_tag: dict[str, dict[str, dict[str, int]]] = {}
        self.refill_frac = 1.0                   # share of the missing DMM quantity restored per refill call (per 30 s cycle)

    # -------------------------------------------------------------- setup
    def add_market(self, ex: str, bid_tick: int, ask_tick: int, q0: float, growth: float, depth: int, recenter: float,
                   real_bids: Optional[list] = None, real_asks: Optional[list] = None):
        self.books[ex] = Book()
        self.dmm[ex] = DmmLadder(bid_tick, ask_tick, q0, growth, depth, recenter, real_bids, real_asks)
        self.refill(ex)

    def account(self, owner: str) -> dict:
        return self.accounts.setdefault(owner, {"cash": 0.0, "pos": {}})

    def position(self, owner: str, ex: str) -> int:
        return self.account(owner)["pos"].get(ex, 0)

    def positions(self, owner: str) -> dict[str, float]:
        return {ex: float(q) for ex, q in self.account(owner)["pos"].items() if q}

    def marked_pnl(self, owner: str) -> float:
        a = self.account(owner)
        return a["cash"] + sum(q * self.dmm[ex].mid * TICK for ex, q in a["pos"].items())

    def pnl_by_tag(self, owner: str) -> dict[str, float]:
        out = {}
        for tag, cash in self.flows.get(owner, {}).items():
            pos = self.pos_by_tag.get(owner, {}).get(tag, {})
            out[tag] = cash + sum(q * self.dmm[ex].mid * TICK for ex, q in pos.items())
        return out

    # ------------------------------------------------------------ matching
    def _book_fill(self, ex: str, tick: int, q: int, taker: str, taker_side: str, maker: Order):
        px = P(tick)
        buyer, seller = (taker, maker.owner) if taker_side == "B" else (maker.owner, taker)
        for owner, sign in ((buyer, +1), (seller, -1)):
            a = self.account(owner)
            a["pos"][ex] = a["pos"].get(ex, 0) + sign * q
            a["cash"] -= sign * q * px
        self._flow(maker.owner, maker.tag, ex, (+1 if maker.side == "B" else -1), q, px)
        self._flow(taker, self._taker_tag, ex, (+1 if taker_side == "B" else -1), q, px)
        self.books[ex].last = tick
        self.fills.append(dict(ex=ex, tick=tick, qty=q, buyer=buyer, seller=seller, maker=maker.owner, ts=self.clock.time()))
        if maker.owner == US and self.on_us_fill:
            self.on_us_fill(maker, tick, q)

    _taker_tag = "take"

    def _flow(self, owner: str, tag: str, ex: str, sign: int, q: int, px: float):
        self.flows.setdefault(owner, {}).setdefault(tag, 0.0)
        self.flows[owner][tag] -= sign * q * px
        pb = self.pos_by_tag.setdefault(owner, {}).setdefault(tag, {})
        pb[ex] = pb.get(ex, 0) + sign * q

    def submit(self, owner: str, ex: str, side: str, tick: int, qty: int, tag: str = "", expiry: Optional[float] = None,
               ioc: bool = False, taker_tag: str = "take") -> tuple[Optional[int], int]:
        """Limit order; crosses by price-time priority, remainder rests unless ioc. Returns (order id or None, filled qty)."""
        book = self.books[ex]
        opp_side = "S" if side == "B" else "B"
        opp = book.levels(opp_side)
        filled = 0
        self._taker_tag = taker_tag
        while qty > 0:
            bt = book.best(opp_side)
            if bt is None or (side == "B" and bt > tick) or (side == "S" and bt < tick):
                break
            queue = opp[bt]
            while queue and qty > 0:
                mo = queue[0]
                if mo.owner == owner:        # self-trade prevention: the resting order is cancelled
                    queue.pop(0); self.orders.pop(mo.id, None)
                    continue
                q = min(qty, mo.qty)
                self._book_fill(ex, bt, q, owner, side, mo)
                mo.qty -= q; qty -= q; filled += q
                if mo.qty == 0:
                    queue.pop(0); self.orders.pop(mo.id, None)
            if not queue:
                opp.pop(bt, None)
        if qty > 0 and not ioc:
            o = Order(self.next_id, owner, ex, side, tick, qty, self.clock.time(), expiry, tag)
            self.next_id += 1
            book.levels(side).setdefault(tick, []).append(o)
            self.orders[o.id] = o
            return o.id, filled
        return None, filled

    def cancel(self, oid: int):
        o = self.orders.pop(oid, None)
        if o is None:
            return
        lv = self.books[o.ex].levels(o.side)
        q = lv.get(o.tick, [])
        if o in q:
            q.remove(o)
        if not q:
            lv.pop(o.tick, None)

    def cancel_owner(self, owner: str, ex: Optional[str] = None, tag: Optional[str] = None):
        for oid in [i for i, o in self.orders.items() if o.owner == owner and (ex is None or o.ex == ex) and (tag is None or o.tag == tag)]:
            self.cancel(oid)

    def expire(self):
        now = self.clock.time()
        for oid in [i for i, o in self.orders.items() if o.expiry is not None and o.expiry <= now]:
            self.cancel(oid)

    def owner_orders(self, owner: str, ex: str) -> list[Order]:
        return [o for o in self.orders.values() if o.owner == owner and o.ex == ex]

    # ----------------------------------------------------------------- DMM
    def refill(self, ex: str, frac: Optional[float] = None):
        frac = self.refill_frac if frac is None else frac
        d = self.dmm[ex]
        book = self.books[ex]
        if d.recenter > 0 and book.last is not None:
            shift = int(round(d.recenter * (book.last - d.mid)))
            if shift:
                d.bid += shift; d.ask += shift
                for o in list(self.orders.values()):
                    if o.owner == DMM and o.ex == ex and not ((d.bid - d.depth < o.tick <= d.bid and o.side == "B") or (d.ask <= o.tick < d.ask + d.depth and o.side == "S")):
                        self.cancel(o.id)
        for k in range(d.depth):
            for side, tick in (("B", d.bid - k), ("S", d.ask + k)):
                have = book.qty(side, tick, DMM)
                need = d.target(k, side) - have
                if need > 0:
                    add = need if frac >= 1.0 else int(math.ceil(need * frac))
                    self.submit(DMM, ex, side, tick, add, tag="dmm")

    def refill_all(self):
        for ex in self.books:
            self.refill(ex)

    def dmm_touch(self, ex: str) -> tuple[int, int]:
        d = self.dmm[ex]
        return d.bid, d.ask

    # --------------------------------------------------------------- takers
    def sweep(self, owner: str, ex: str, side: str, limit_tick: int, max_qty: Optional[int] = None, tag: str = "take") -> int:
        """IOC: buy (side B) everything up to limit_tick / sell (S) everything down to limit_tick."""
        q = max_qty if max_qty is not None else 10 ** 9
        _, filled = self.submit(owner, ex, side, limit_tick, q, ioc=True, taker_tag=tag)
        return filled


# ------------------------------------------------------------------------------------------------------- fake API
class FakeApi:
    """What the real SuperMarketClient does, against the paper venue. Orders carry a sim expiry parsed from expirationDate."""

    def __init__(self, venue: Venue, clock: SimClock, atomic: bool = True):
        self.venue, self.clock, self.atomic = venue, clock, atomic
        self.tournament_id = "sim"
        self.stats = {"reads": 0, "writes": 0, "retries": 0, "errors": 0}
        self.tag = "mm"

    @staticmethod
    def new_key(prefix: str = "sb") -> str:
        return f"{prefix}-sim"

    @staticmethod
    def _exp(s: Optional[str]) -> Optional[float]:
        if not s:
            return None
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    def _place(self, o: dict) -> tuple[Optional[int], int]:
        side = "B" if o["side"] == "yes" else "S"
        tick = T(o["price"]) if side == "B" else T(1 - o["price"])
        return self.venue.submit(US, str(o["exchangeId"]), side, tick, int(o["quantity"]), tag=self.tag, expiry=self._exp(o.get("expirationDate")))

    def cancel_all(self, exchange_id=None, market_id=None, tournament_id=None):
        self.stats["writes"] += 1
        self.venue.cancel_owner(US, str(exchange_id) if exchange_id else None)
        return {}

    def place_batch(self, orders: list[dict], key=None, max_rounds=6) -> dict:
        self.stats["writes"] += 1
        res = []
        for i, o in enumerate(orders):
            oid, filled = self._place(o)
            res.append({"index": i, "ok": True, "data": {"orderId": oid if oid is not None else -(self.venue.next_id + i), "open": oid is not None}})
        return {"results": res}

    def place_order(self, exchange_id, side, action, quantity, price, expiration=None, tournament_id=None, key=None) -> dict:
        self.stats["writes"] += 1
        oid, filled = self._place(dict(exchangeId=exchange_id, side=side, price=price, quantity=quantity, expirationDate=expiration))
        return {"orderId": oid, "open": oid is not None}

    def place_multi_leg(self, legs: list[dict], all_or_none: bool = True, key=None) -> dict:
        """allOrNone: every leg must be fillable for its whole quantity at or better than its limit, else VALIDATION_ERROR
        AllOrNoneNotFilled and nothing trades. With atomic=False the venue fills legs one by one and rests the remainder
        (the failure mode we cannot rule out from outside)."""
        self.stats["writes"] += 1
        v = self.venue
        plan = []
        for leg in legs:
            ex = str(leg["exchangeId"]); side = "B" if leg["side"] == "yes" else "S"
            lim = T(leg["price"]) if side == "B" else T(1 - leg["price"])
            book = v.books[ex]
            avail = 0
            for t, l in book.levels("S" if side == "B" else "B").items():
                if (side == "B" and t <= lim) or (side == "S" and t >= lim):
                    avail += sum(o.qty for o in l if o.owner != US)
            plan.append((ex, side, lim, int(leg["quantity"]), avail))
        if all_or_none and self.atomic and any(a < q for _, _, _, q, a in plan):
            bad = next(l for l, (_, _, _, q, a) in zip(legs, plan) if a < q)
            raise ApiError(422, "VALIDATION_ERROR", "all-or-none not filled", details={"rejectionReasonCode": "AllOrNoneNotFilled", "leg": bad})
        results = []
        for leg, (ex, side, lim, q, _) in zip(legs, plan):
            oid, filled = v.submit(US, ex, side, lim, q, tag="arb", ioc=self.atomic)
            results.append({"data": {"orderId": oid if oid is not None else -1, "exchangeId": ex, "side": leg["side"], "action": "buy",
                                     "price": leg["price"], "quantityTraded": filled, "open": oid is not None}})
        return {"results": results}

    # the sleeves only read through the main loop; the simulator feeds them directly
    def orderbook(self, ex, depth=50, tournament_id=None):
        self.stats["reads"] += 1
        return self.venue.books[str(ex)].snapshot(depth)


# ---------------------------------------------------------------------------------------------------- mitigations
@dataclass
class Mitig:
    rotate_requote: bool = False       # M1 rotate the requote order so budget-skipped markets are not always the same
    fill_requote_min: int = 0          # M2 a fill only triggers a requote when >= this many shares filled on the exchange
    requote_ticks: Optional[float] = None   # M3 hysteresis on the reserve-move requote trigger (ticks)
    micro_floor: int = 0               # M4 microprice ignores touch levels thinner than this (uses the next level)
    micro_cap: int = 0                 # M4 and caps each side's quantity at this in the imbalance
    tilt_touch_floor: int = 0          # M5 tilt treats a touch level thinner than this as absent
    inside_size_cap: int = 0           # M6 cap on level-1 size when we are alone inside the spread
    arb_bait_blacklist_sec: float = 0  # M7 after AllOrNoneNotFilled on a race whose bid vanished, skip it for this long
    arb_scan_jitter: float = 0.0       # M7 random phase on the scanner
    arb_flatten: bool = False          # M8 act on a partial multi-leg: flatten the residual at the touch at once
    gate_persist: int = 1              # M9 the vol gate needs the move on this many consecutive cycles (sim-side wrapper)
    recycle_verify: bool = False       # M10 never recycle for a best_unfunded whose own execution just failed all-or-none

    @staticmethod
    def proposed() -> "Mitig":
        return Mitig(rotate_requote=True, fill_requote_min=10, requote_ticks=1.0, micro_floor=100, micro_cap=3000, tilt_touch_floor=100,
                     inside_size_cap=25, arb_bait_blacklist_sec=600, arb_scan_jitter=2.0, arb_flatten=True, gate_persist=2, recycle_verify=True)


def robust_microprice(floor: int, cap: int):
    def microprice(book: dict):
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            return None, None, 0.0
        def eff(levels):
            acc = 0
            for l in levels:
                acc += l["quantity"]
                if acc >= floor:
                    return l["price"], min(cap, acc) if cap else acc
            return levels[-1]["price"], min(cap, acc) if cap else acc
        bb0, ba0 = bids[0]["price"], asks[0]["price"]
        bb, qb = eff(bids); ba, qa = eff(asks)
        mid = 0.5 * (bb0 + ba0)
        imb = (qb - qa) / (qb + qa) if (qb + qa) > 0 else 0.0
        micro = (bb * qa + ba * qb) / (qb + qa) if (qb + qa) > 0 else mid
        micro = min(ba0, max(bb0, micro))     # never outside the real touch
        return mid, micro, imb
    return microprice


# ---------------------------------------------------------------------------------------------------------- bot
class OurBot:
    """The real sleeves wired to the paper venue."""

    def __init__(self, venue: Venue, clock: SimClock, cfg: BotConfig, universe: Universe, mm_markets: list[str],
                 tilt_markets: list[str], mitig: Mitig, atomic_api: bool = True):
        self.venue, self.clock, self.cfg, self.u, self.mitig = venue, clock, cfg, universe, mitig
        self.store = Store(":memory:")
        self.api = FakeApi(venue, clock, atomic=atomic_api)
        mmcfg = replace(cfg.mm)
        if mitig.requote_ticks is not None:
            mmcfg.requote_ticks = mitig.requote_ticks
        self.mm = MarketMaker(self.api, self.store, mmcfg, universe, cfg.tournament_slug, cfg.starting_cash, dry_run=False,
                              root=os.path.join(HERE, "data", "__no_overrides__"))
        self.mm.hot_until, self.mm.hot_anchor = {}, {}      # class-level dicts in mm.py: isolate this instance
        self.mm.quoted = list(mm_markets)
        self.mm.vol = {ex: 0.01 for ex in mm_markets}
        self.mm.vol5m = {ex: 0.004 for ex in mm_markets}
        if mitig.micro_floor or mitig.micro_cap:
            self.mm.microprice = robust_microprice(mitig.micro_floor, mitig.micro_cap)
        self.tilt = TiltHarvester(self.api, self.store, cfg.tilt, universe, cfg.starting_cash, dry_run=False)
        self.tilt_markets = list(tilt_markets)
        self.cycle_id = 0
        self.fill_seq = 0
        self.sig_fills: dict[str, int] = {}
        self.hot_streak: dict[str, int] = {}
        self.summaries: list[dict] = []
        venue.on_us_fill = self.on_fill

    def on_fill(self, o: Order, tick: int, q: int):
        self.fill_seq += 1
        yes_px = P(tick)
        side = "yes" if o.side == "B" else "no"
        f = {"id": self.fill_seq, "orderId": o.id, "exchangeId": o.ex, "marketId": None, "side": side,
             "price": yes_px if side == "yes" else round(1 - yes_px, 3), "quantity": q if side == "yes" else -q,
             "filledAt": datetime.fromtimestamp(self.clock.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}
        self.store.ingest_fills([f])
        if q >= self.mitig.fill_requote_min:
            self.sig_fills[o.ex] = self.sig_fills.get(o.ex, 0) + 1

    def open_orders(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for o in self.venue.orders.values():
            if o.owner == US:
                out.setdefault(o.ex, []).append({"id": o.id, "quantity": o.qty, "priceLimit": P(o.tick) if o.side == "B" else round(1 - P(o.tick), 3)})
        return out

    def fills_count(self) -> dict[str, int]:
        if self.mitig.fill_requote_min:
            return dict(self.sig_fills)
        return {r["exchange_id"]: r["n"] for r in self.store.rows("SELECT exchange_id, COUNT(*) AS n FROM fills GROUP BY exchange_id")}

    def touch(self, ex: str, floor: int = 0) -> dict:
        b = self.venue.books[ex].snapshot(10)
        def eff(levels):
            acc = 0
            for l in levels:
                acc += l["quantity"]
                if acc >= floor:
                    return l["price"]
            return levels[-1]["price"] if levels else None
        return {"bestBid": eff(b["bids"]), "bestAsk": eff(b["asks"]), "latestPrice": b.get("latestPrice")}

    def cycle(self):
        v = self.venue
        v.expire()
        self.cycle_id += 1
        positions = v.positions(US)
        prices = {ex: self.touch(ex) for ex in list(self.mm.quoted) + self.tilt_markets}
        expo = risk.exposure(self.u, positions, prices)
        self.mm.blocked = self.tilt.blocked = risk.blocked_directions(expo, self.cfg.mm.portfolio_max_partisan_notional)
        open_orders = self.open_orders()
        fills_count = self.fills_count()
        books = {ex: v.books[ex].snapshot(10) for ex in self.mm.quoted}
        if self.mitig.rotate_requote and self.mm.quoted:
            k = self.cycle_id % len(self.mm.quoted)
            self.mm.quoted = self.mm.quoted[k:] + self.mm.quoted[:k]
        cash = self.cfg.starting_cash + v.account(US)["cash"]
        write_budget = max(4, self.cfg.mm_write_budget - 4)
        self.api.tag = "mm"
        if self.mitig.gate_persist > 1:
            # sim-side wrapper for M9: a hot trigger only counts after gate_persist consecutive cycles of the same reading
            real = self.mm.vol_regime
            def gated(ex, mid):
                raw = real(ex, mid)
                self.hot_streak[ex] = self.hot_streak.get(ex, 0) + 1 if raw else 0
                if raw and self.hot_streak[ex] < self.mitig.gate_persist:
                    self.mm.hot_until.pop(ex, None); self.mm.hot_anchor.pop(ex, None)
                    return False
                return raw
            self.mm.vol_regime = gated
        if self.mitig.inside_size_cap:
            real_bq = self.mm.build_quotes
            cap = self.mitig.inside_size_cap
            def capped(ex, book, positions, pressure, budget):
                quotes, info = real_bq(ex, book, positions, pressure, budget)
                bb, ba = info.get("bb"), info.get("ba")
                for q in quotes:
                    if q.level == 1 and ((q.side == "yes" and q.yes_price > bb + 1e-9) or (q.side == "no" and q.yes_price < ba - 1e-9)):
                        q.qty = min(q.qty, cap)
                return quotes, info
            self.mm.build_quotes = capped
        s = self.mm.quote_cycle(books, positions, {}, open_orders, fills_count, cash, self.cycle_id, write_budget)
        if self.mitig.gate_persist > 1:
            self.mm.vol_regime = real
        if self.mitig.inside_size_cap:
            self.mm.build_quotes = real_bq
        self.api.tag = "tilt"
        tprices = {ex: self.touch(ex, self.mitig.tilt_touch_floor) for ex in self.tilt_markets}
        ts = self.tilt.cycle(tprices, positions, self.open_orders(), set(self.mm.quoted), cash, self.cycle_id,
                             max(2, self.cfg.mm_write_budget - s.get("writes", 0)))
        s["tilt"] = ts
        self.summaries.append(s)
        return s

    def our_orders(self, ex: str) -> list[Order]:
        return self.venue.owner_orders(US, ex)


# ---------------------------------------------------------------------------------------------------------- sim
@dataclass
class SimArgs:
    touch_qty: float = 2400.0
    growth: float = 1.53
    depth: int = 12
    recenter: float = 0.0
    n_mm: int = 15
    n_tilt: int = 8
    seed: int = 1
    real: bool = True          # per-level DMM quantities from data/books.json (keeps the real touch imbalance)
    refill_frac: float = 0.5   # share of a depleted DMM level restored per 30 s cycle (quotes table: ~half within a cycle)


class Sim:
    def __init__(self, cfg: BotConfig, universe: Universe, books: dict, volume: dict, sa: SimArgs, mitig: Mitig, atomic_api: bool = True):
        self.cfg, self.u, self.sa, self.mitig = cfg, universe, sa, mitig
        self.clock = SimClock(_time.time())
        install_clock(self.clock)
        self.venue = Venue(self.clock)
        prices = {}
        for ex, b in books.items():
            if b.get("bids") and b.get("asks"):
                prices[ex] = {"bestBid": b["bids"][0]["price"], "bestAsk": b["asks"][0]["price"], "latestPrice": b.get("latestPrice")}
        probe = MarketMaker(None, None, cfg.mm, universe, cfg.tournament_slug, cfg.starting_cash, dry_run=True)
        probe.cfg = replace(cfg.mm, n_markets=sa.n_mm)
        self.mm_markets = probe.select_markets(prices, volume)
        tprobe = TiltHarvester(None, None, cfg.tilt, universe, cfg.starting_cash, dry_run=True)
        cands = tprobe.candidates(prices, set(self.mm_markets))
        # prefer the tail markets with the widest spreads (where the touch can be baited), then edge
        cands.sort(key=lambda r: (-(T(prices[r[0]]["bestAsk"]) - T(prices[r[0]]["bestBid"])), -r[3]))
        self.tilt_markets = [r[0] for r in cands[: sa.n_tilt]]
        for ex in self.mm_markets + self.tilt_markets:
            p = prices[ex]
            rb = ra = None
            if sa.real:
                rb = [int(l["quantity"]) for l in books[ex]["bids"][: sa.depth]]
                ra = [int(l["quantity"]) for l in books[ex]["asks"][: sa.depth]]
            self.venue.add_market(ex, T(p["bestBid"]), T(p["bestAsk"]), sa.touch_qty, sa.growth, sa.depth, sa.recenter, rb, ra)
        self.venue.refill_frac = sa.refill_frac
        self.bot = OurBot(self.venue, self.clock, cfg, universe, self.mm_markets, self.tilt_markets, mitig, atomic_api)
        self.t0 = self.clock.time()
        self.rng = random.Random(sa.seed)

    def bot_cycle(self):
        """30 s pass: our bot reads the book as the attacker left it (it knows our phase), then the DMM refills partially."""
        self.clock.advance(BOT_CYCLE_SEC)
        s = self.bot.cycle()
        self.venue.refill_all()
        return s

    def hours(self) -> float:
        return (self.clock.time() - self.t0) / 3600.0

    def spread(self, ex: str) -> int:
        d = self.venue.dmm[ex]
        return d.ask - d.bid

    def close(self):
        restore_clock()

    def result(self, name: str, extra: dict) -> dict:
        v = self.venue
        by = v.pnl_by_tag(US)
        return dict(attack=name, hours=round(self.hours(), 2), attacker=round(v.marked_pnl(ATK), 2), us=round(v.marked_pnl(US), 2),
                    us_mm=round(by.get("mm", 0.0), 2), us_tilt=round(by.get("tilt", 0.0), 2), us_arb=round(by.get("arb", 0.0) + by.get("hedge", 0.0), 2),
                    dmm=round(v.marked_pnl(DMM), 2), us_per_hour=round(v.marked_pnl(US) / max(1e-9, self.hours()), 2), **extra)


# ------------------------------------------------------------------------------------------------------ attacker
class Attacker:
    """Counter-bot. Sees the book (and, like any watcher of our very regular 45/65/86/108 ladder, which orders are ours)."""

    def __init__(self, sim: Sim):
        self.sim, self.v, self.bot = sim, sim.venue, sim.bot

    def ours(self, ex: str, side: str) -> list[Order]:
        return sorted([o for o in self.bot.our_orders(ex) if o.side == side], key=lambda o: o.tick, reverse=(side == "B"))

    def inside(self, ex: str) -> tuple[list[Order], list[Order]]:
        """Our orders strictly better than the DMM touch (we are alone in front of the queue there)."""
        bb, ba = self.v.dmm_touch(ex)
        return [o for o in self.ours(ex, "B") if o.tick > bb], [o for o in self.ours(ex, "S") if o.tick < ba]

    def ahead(self, o: Order) -> int:
        """Shares that must be traded before reaching our order: better-priced levels plus the queue ahead at its price."""
        book = self.v.books[o.ex]
        lv = book.levels(o.side)
        better = sum(x.qty for t, l in lv.items() if (t > o.tick if o.side == "B" else t < o.tick) for x in l)
        queue = lv.get(o.tick, [])
        pos = queue.index(o) if o in queue else 0
        return better + sum(x.qty for x in queue[:pos])

    def hit(self, o: Order, qty: Optional[int] = None, max_ahead: Optional[int] = None) -> int:
        """Trade against one of our resting orders at its price, paying for everything ahead of it (better levels and the
        queue at its price). Returns shares of OURS taken; 0 when more than max_ahead shares stand in the way."""
        ahead = self.ahead(o)
        if max_ahead is not None and ahead > max_ahead:
            return 0
        side = "S" if o.side == "B" else "B"
        want = o.qty if qty is None else min(qty, o.qty)
        before = o.qty
        self.v.sweep(ATK, o.ex, side, o.tick, ahead + want)
        return before - (o.qty if o.id in self.v.orders else 0)

    def unwind(self, ex: str, tag: str = "unwind") -> int:
        q = self.v.position(ATK, ex)
        if q == 0:
            return 0
        bb, ba = self.v.dmm_touch(ex)
        if q > 0:
            return self.v.sweep(ATK, ex, "S", bb - 12, q, tag=tag)
        return self.v.sweep(ATK, ex, "B", ba + 12, -q, tag=tag)

    def unwind_all(self):
        for ex in self.v.books:
            self.unwind(ex)
            self.v.refill(ex)

    def spoof(self, ex: str, side: str, qty: int) -> Optional[int]:
        """Join the touch on `side` with at least `qty`, scaled to 3x the resting touch so the imbalance flips whatever the
        DMM shows there. The order sits at the back of the DMM queue: with a refilling ladder it is almost never filled."""
        bb, ba = self.v.dmm_touch(ex)
        book = self.v.books[ex]
        touch = max(book.qty("B", bb), book.qty("S", ba))
        qty = max(qty, 3 * touch)
        oid, _ = self.v.submit(ATK, ex, side, bb if side == "B" else ba, qty, tag="spoof")
        return oid


# ------------------------------------------------------------------------------------------------------- attacks
def atk_sweep(sim: Sim, cycles: int) -> dict:
    """(a) Sweep our whole ask ladder through the DMM, let the DMM refill, sweep the bid ladder on the way back."""
    a = Attacker(sim)
    sim.bot_cycle()
    took_ours = 0; dmm_eaten = 0; max_ahead = 60000
    for c in range(cycles):
        for ex in sim.mm_markets:
            for o in sorted(a.ours(ex, "S"), key=lambda o: o.tick):        # level by level, paying for the DMM in between
                dmm_eaten += min(a.ahead(o), max_ahead)
                took_ours += a.hit(o, max_ahead=max_ahead)
        sim.bot_cycle()                                   # we requote on the fills; the DMM half-refills
        for ex in sim.mm_markets:
            for o in sorted(a.ours(ex, "B"), key=lambda o: -o.tick):
                if sim.venue.position(ATK, ex) <= 0:
                    break
                dmm_eaten += min(a.ahead(o), max_ahead)
                took_ours += a.hit(o, qty=sim.venue.position(ATK, ex), max_ahead=max_ahead)
            a.unwind(ex)
        sim.bot_cycle()
    return sim.result("sweep_revert", dict(dmm_shares_eaten=dmm_eaten, our_shares_taken=took_ours))


def atk_pickoff(sim: Sim, cycles: int) -> dict:
    """(a') The cheap sweep: only take our quotes that sit alone inside the DMM spread (no DMM to eat), round-trip them
    through our own requote, flatten the rest at the DMM touch each cycle."""
    a = Attacker(sim)
    sim.bot_cycle()
    taken = 0; rt = 0
    for c in range(cycles):
        for ex in sim.mm_markets:
            bids, asks = a.inside(ex)
            for o in asks:
                taken += a.hit(o)
            for o in bids:
                taken += a.hit(o)
        sim.bot_cycle()
        for ex in sim.mm_markets:
            pos = sim.venue.position(ATK, ex)
            bids, asks = a.inside(ex)
            if pos > 0 and bids:
                rt += a.hit(bids[0], pos)
            elif pos < 0 and asks:
                rt += a.hit(asks[0], -pos)
            a.unwind(ex)
        sim.bot_cycle()
    return sim.result("inside_pickoff", dict(our_shares_taken=taken, round_tripped_vs_us=rt))


def atk_spoof(sim: Sim, cycles: int, spoof_qty: int = 30000) -> dict:
    """(b) Size imbalance at the touch moves our microprice/fair; trade against the shifted ladder, flip, repeat."""
    a = Attacker(sim)
    sim.bot_cycle()
    taken = 0; requotes = 0
    for c in range(cycles):
        for phase in ("B", "S"):
            ids = {ex: a.spoof(ex, phase, spoof_qty) for ex in sim.mm_markets}
            s = sim.bot_cycle()
            requotes += len(s.get("requoted", []))
            for ex in sim.mm_markets:
                if ids[ex] is not None:
                    sim.venue.cancel(ids[ex])
                bids, asks = a.inside(ex)
                if phase == "B":      # we leaned up: sell into any bid of ours above the DMM bid
                    for o in bids:
                        taken += a.hit(o)
                else:
                    for o in asks:
                        taken += a.hit(o)
        for ex in sim.mm_markets:
            a.unwind(ex)
    return sim.result("spoof_microprice", dict(our_shares_taken=taken, requotes=requotes))


def atk_flipflop(sim: Sim, cycles: int, spoof_qty: int = 30000) -> dict:
    """(f/g) Zero-cost denial: flip a large resting order between the bid and ask touch every cycle so the microprice
    moves >= requote_ticks everywhere; 15 cancel-alls + batches exceed the write budget; the same markets are skipped
    every cycle until their orders expire (TTL 15 min) and never come back."""
    a = Attacker(sim)
    sim.bot_cycle()
    blank_cycles = 0; skipped = 0; writes = 0; last = {}
    for c in range(cycles):
        side = "B" if c % 2 == 0 else "S"
        for ex in sim.mm_markets:
            if last.get(ex):
                sim.venue.cancel(last[ex])
            last[ex] = a.spoof(ex, side, spoof_qty)
        s = sim.bot_cycle()
        skipped += s.get("skipped_budget", 0); writes += s.get("writes", 0)
        blank_cycles += sum(1 for ex in sim.mm_markets if not sim.bot.our_orders(ex))
    blank_now = sum(1 for ex in sim.mm_markets if not sim.bot.our_orders(ex))
    return sim.result("micro_flipflop_dos", dict(markets_blank_now=blank_now, blank_market_cycles=blank_cycles, skipped_budget=skipped,
                                                 writes_per_cycle=round(writes / max(1, cycles), 1)))


def atk_tilt_bait(sim: Sim, cycles: int) -> dict:
    """(c) Tighten the touch by one tick with a 1-share order on a tail market; the tilt ladder re-posts at the new touch
    (the touch moved); pull the order and trade into our improved level."""
    a = Attacker(sim)
    sim.bot_cycle()
    taken = 0; baits = 0; slip = 0.0
    for c in range(cycles):
        ids = {}
        for ex in sim.tilt_markets:
            if sim.spread(ex) < 2:
                continue
            bb, ba = sim.venue.dmm_touch(ex)
            mid = 0.5 * (bb + ba) * TICK
            if mid >= 0.5:
                ids[ex], _ = sim.venue.submit(ATK, ex, "B", bb + 1, 1, tag="bait")
            else:
                ids[ex], _ = sim.venue.submit(ATK, ex, "S", ba - 1, 1, tag="bait")
            baits += 1
        sim.bot_cycle()
        for ex, oid in ids.items():
            if oid is not None:
                sim.venue.cancel(oid)
            bb, ba = sim.venue.dmm_touch(ex)
            bids, asks = a.inside(ex)
            for o in bids:            # our improved buy: the attacker sells to us 1+ ticks above the DMM bid, and keeps the position
                q = a.hit(o); taken += q; slip += q * (o.tick - bb) * TICK
            for o in asks:
                q = a.hit(o); taken += q; slip += q * (ba - o.tick) * TICK
        sim.bot_cycle()
    # the position the attacker handed us is one it wanted anyway: it holds it; our 'loss' is the slippage versus the
    # unbaited touch (the fill would have happened at the DMM touch), realised when we hold to settlement
    out = sim.result("tilt_bait", dict(baits=baits, our_shares_taken=taken, slippage_vs_touch=round(slip, 2),
                                       tilt_markets=len(sim.tilt_markets), wide_tilt_markets=sum(1 for ex in sim.tilt_markets if sim.spread(ex) >= 2)))
    out["us"] = out["us_tilt"] = round(-slip, 2)          # mid-marking a wide-spread tail fill is meaningless; the loss is the slippage
    out["us_per_hour"] = round(-slip / max(1e-9, sim.hours()), 2)
    return out


def atk_gate_poke(sim: Sim, cycles: int, n_markets: int = 3, ticks: int = 3) -> dict:
    """(d) Print the mid `ticks` up on a few markets, hold through one of our cycles so the volatility gate (3 ticks in
    15 min) and the wave (15% of quoted markets hot) fire, then sell back. Measures the attacker's cost per trigger and
    what it does to our resting size."""
    a = Attacker(sim)
    sim.bot_cycle()
    base_size = sum(o.qty for ex in sim.mm_markets for o in sim.bot.our_orders(ex))
    # the cheapest markets to move: least resting ask quantity within `ticks` of the touch
    def ask_depth(ex):
        d = sim.venue.dmm[ex]
        return sum(d.target(k, "S") for k in range(ticks))
    targets = sorted(sim.mm_markets, key=ask_depth)[:n_markets]
    hot = 0; waves = 0; sizes = []; eaten = 0
    for c in range(cycles):
        parked = []
        for ex in targets:
            bb, ba = sim.venue.dmm_touch(ex)
            eaten += sim.venue.sweep(ATK, ex, "B", ba + ticks - 1)          # eat `ticks` ask levels ...
            oid, _ = sim.venue.submit(ATK, ex, "B", ba + ticks - 1, 1, tag="park")   # ... and bid there: mid is up `ticks`
            parked.append(oid)
        s = sim.bot_cycle()                                               # we read a 3-tick move; then the DMM half-refills
        hot += sum(1 for ex in sim.mm_markets if sim.bot.mm.hot_until.get(ex, 0) > sim.clock.time())
        waves += 1 if s.get("wave") else 0
        sizes.append(sum(o.qty for ex in sim.mm_markets for o in sim.bot.our_orders(ex)))
        for oid in parked:
            if oid is not None:
                sim.venue.cancel(oid)
        for ex in targets:
            a.unwind(ex)
        sim.bot_cycle()
    return sim.result("vol_gate_poke", dict(hot_market_cycles=hot, wave_cycles=waves, resting_size_before=base_size,
                                            resting_size_min=min(sizes) if sizes else None, dmm_shares_eaten=eaten,
                                            attacker_cost_per_trigger=round(-sim.venue.marked_pnl(ATK) / max(1, cycles), 2)))


def atk_cap_push(sim: Sim, cycles: int) -> dict:
    """(h) Sell us D-yes (and R-no) through our bids until the per-race / partisan caps block the adding side, then try to
    buy it back from the one-sided ladder."""
    a = Attacker(sim)
    sim.bot_cycle()
    blocked_cycles = 0; sold = 0; bought = 0
    for c in range(cycles):
        for ex in sim.mm_markets:
            if risk.adds(sim.u, ex, "yes") != "D":
                continue
            for o in a.ours(ex, "B"):                 # cheapest first; pay for at most 15k DMM shares per level
                sold += a.hit(o, max_ahead=15000)
        s = sim.bot_cycle()
        if sim.bot.mm.blocked:
            blocked_cycles += 1
            for ex in sim.mm_markets:
                bids, asks = a.inside(ex)
                for o in asks:
                    bought += a.hit(o)
        sim.bot_cycle()
    a.unwind_all()
    return sim.result("partisan_cap_push", dict(blocked_cycles=blocked_cycles, sold_to_us=sold, bought_back_inside=bought,
                                                blocked_now=sorted(sim.bot.mm.blocked)))


def atk_poison(sim: Sim, cycles: int, mode: str = "calm") -> dict:
    """(i) Feed the markout model: take our inside quote, then park a 1-share order so the mid moves half a tick in
    (calm) or against (toxic) our fill; run the real analytics after 30 minutes and apply the overrides."""
    a = Attacker(sim)
    sim.bot_cycle()
    fed = 0
    for c in range(cycles):
        sim.venue.cancel_owner(ATK, tag="park")       # lift the park so our next requote puts an inside ask back ...
        sim.bot_cycle()
        for ex in sim.mm_markets:                     # ... take that whole level, then re-park on our fill price
            bids, asks = a.inside(ex)
            if asks:
                o = asks[0]; fed += a.hit(o)
                if mode == "calm":
                    sim.venue.submit(ATK, ex, "S", o.tick, 1, tag="park")       # ask touch at our fill price: mid drops
                else:
                    sim.venue.submit(ATK, ex, "B", o.tick, 1, tag="park")       # bid touch at our fill price: mid rises
        sim.bot_cycle()
    for _ in range(62):                      # 31 minutes so the 30m markouts exist
        sim.bot_cycle()
    ov = an_mod.compute(sim.bot.store, sim.cfg)
    sim.bot.mm.overrides = ov
    sim.bot.mm.overrides_mtime = -1
    sim.bot.mm.load_overrides = lambda: None
    for st in sim.bot.mm.state.values():
        st.last_reserve = None
    sim.venue.cancel_owner(ATK)
    sim.venue.refill_all()
    before = sum(o.qty for ex in sim.mm_markets for o in sim.bot.our_orders(ex))
    sim.bot_cycle()
    after = sum(o.qty for ex in sim.mm_markets for o in sim.bot.our_orders(ex))
    exs = ov.get("exchanges", {})
    shapes = {}
    for ex, e in exs.items():
        shapes[e.get("shape", "default")] = shapes.get(e.get("shape", "default"), 0) + 1
    scales = [e.get("size_scale", 1.0) for e in exs.values()]
    a.unwind_all()
    return sim.result(f"analytics_poison_{mode}", dict(fed_fills=fed, shapes=shapes, size_scale_mean=round(sum(scales) / len(scales), 3) if scales else None,
                                                       resting_before=before, resting_after=after))


def atk_arb_bait(sim: Sim, cycles: int, n_bait: int = 3, real_rate_per_hour: float = 6.0, real_value: float = 100.0,
                 nonatomic: bool = False, min_ret: Optional[float] = None) -> dict:
    """(e/f) Post bids on two legs of a race that sum above 1, pull one as our :x4.5 all-or-none order arrives. Each pass
    the bait races carry the biggest apparent edge, so they take the `arb_scan_books` book slots; genuine arbs that
    appear elsewhere are not scanned. Cost to the attacker: zero. With `nonatomic` the venue fills the legs one by one
    (the guarantee we cannot verify from outside) and our code leaves the residual resting."""
    v, cfg = sim.venue, sim.cfg
    arb_cfg = cfg.arb if min_ret is None else replace(cfg.arb, min_ret=min_ret)
    arb = ArbEngine(sim.bot.api, sim.bot.store, arb_cfg, sim.u, cfg.tournament_slug, cfg.starting_cash, dry_run=False)
    sim.bot.api.atomic = not nonatomic
    # every two-leg race of the universe at its real touch (the scanner ranks the whole universe, not just quoted markets)
    with open(os.path.join(HERE, "data", "books.json")) as f:
        allbooks = json.load(f)
    for ex, b in allbooks.items():
        if ex not in v.books and b.get("bids") and b.get("asks") and ex in sim.u.by_exchange:
            v.add_market(ex, T(b["bids"][0]["price"]), T(b["asks"][0]["price"]), sim.sa.touch_qty, sim.sa.growth, 12, 0.0,
                         [int(l["quantity"]) for l in b["bids"][:12]], [int(l["quantity"]) for l in b["asks"][:12]])
    races = [r for r in sim.u.races.values() if len(r.legs) == 2 and all(l.exchange_id in v.books for l in r.legs.values())]
    # books.json is a per-market snapshot taken over a minute: 60 races show bids already summing above 1 (stale or genuinely
    # crossed; the live scanner cannot act on them because the 8k arb budget is fully committed). Neutralise them so the
    # test measures the scanner's reaction to bait, not the snapshot: shift the richer leg's ladder down to sum(bids) <= 1.
    for r in sim.u.races.values():
        exs = [e for e in r.exchange_ids if e in v.books]
        if len(exs) != len(r.exchange_ids):
            continue
        excess = sum(v.dmm_touch(e)[0] for e in exs) - 200
        if excess > 0:                               # sell-set showing: lower the richest leg
            e = max(exs, key=lambda x: v.dmm_touch(x)[0])
            d = v.dmm[e]; d.bid -= excess; d.ask -= excess
            v.cancel_owner(DMM, e); v.refill(e, 1.0)
        short = 200 - int(round(arb.x_for(r) / TICK)) - 1 - sum(v.dmm_touch(e)[1] for e in exs)
        if short > 0:                                # buy-set showing: raise the cheapest leg
            e = min(exs, key=lambda x: v.dmm_touch(x)[1])
            d = v.dmm[e]; d.bid += short; d.ask += short
            v.cancel_owner(DMM, e); v.refill(e, 1.0)
    # a bait needs bids summing above 1 without crossing, and the plan needs ret >= arb.min_ret: sum(bids) - 1 >= min_ret * cost,
    # i.e. `need` ticks of room between the asks. The attacker makes room where it is missing by eating the thinner leg's
    # asks (and pays the ladder for it), so it picks the races where that is cheapest.
    need_ticks = int(math.ceil(max(arb_cfg.min_ret, arb_cfg.min_edge_sell) * 0.99 / TICK)) + 2
    def room(r):
        return sum(v.dmm_touch(l.exchange_id)[1] for l in r.legs.values()) - 2 - 200
    def thin_leg(r):
        return min(r.exchange_ids, key=lambda e: sum(v.dmm[e].target(k, "S") for k in range(max(0, need_ticks - room(r)))))
    def make_room_cost(r):
        k = max(0, need_ticks - room(r))
        return sum(v.dmm[thin_leg(r)].target(j, "S") * (j + 1) for j in range(k)) * TICK      # ticks paid above the touch
    baitable = sorted(races, key=make_room_cost)
    bait_races = baitable[:n_bait]
    others = baitable[n_bait:]
    real_race = others[len(others) // 2] if others else None
    def make_room(r):
        """Eat `k` ask levels on the thinner leg so the bait bids can sum to 1 + min_ret. Returns the bait tick per leg."""
        k = max(0, need_ticks + 1 - room(r))       # one tick more than a genuine arb at the threshold: it ranks first
        levels = {e: v.dmm_touch(e)[1] - 1 for e in r.exchange_ids}
        if k > 0:
            e = thin_leg(r)
            v.sweep(ATK, e, "B", v.dmm_touch(e)[1] + k - 1, tag="room")
            levels[e] = v.dmm_touch(e)[1] + k - 1
        return levels, k
    period = cfg.arb_scan_sec
    passes = int(cycles * 60 / period)
    blacklist: dict[str, float] = {}
    stats = dict(passes=passes, baitable_races=len(baitable), bait_attempts=0, aon_rejects=0, writes=0, slots_bait=0, real_arbs=0,
                 real_missed=0, real_taken=0, bait_filled_sets=0, residual_shares_max=0, arb_edge=0.0, hedge_cost=0.0)
    rng = sim.rng
    touched: set = set()
    for p in range(passes):
        sim.clock.advance(period)
        now = sim.clock.time()
        arb.committed = {}            # keep the 8k budget free: the test is the scanner's reaction, not budget exhaustion
        bait_ids = []
        if stats.get("attacker_losing_fills", 0) >= 2 and not stats.get("gave_up_pass"):
            stats["gave_up_pass"] = p   # a rational attacker stops once the bait has been filled twice (it pays us the edge)
        for r in ([] if stats.get("gave_up_pass") else bait_races):
            if nonatomic and any(v.owner_orders(US, l.exchange_id) for l in r.legs.values()):
                continue                # never re-bait a leg where our residual rests: that would complete our set
            levels, k = make_room(r)
            stats["room_ticks_eaten"] = stats.get("room_ticks_eaten", 0) + k
            for ex, t in levels.items():
                oid, _ = v.submit(ATK, ex, "B", t, 5000, tag="bait")
                bait_ids.append(oid); touched.add(ex)
        real_here = real_race is not None and rng.random() < real_rate_per_hour * period / 3600
        real_shift = None
        if real_here:
            # a genuine sell-set arb: one leg's whole DMM ladder reprices up so the DMM bids sum to 1 + need_ticks ticks
            stats["real_arbs"] += 1
            e0 = real_race.exchange_ids[0]
            sh = need_ticks - (sum(v.dmm_touch(e)[0] for e in real_race.exchange_ids) - 200)
            d = v.dmm[e0]; d.bid += sh; d.ask += sh
            v.cancel_owner(DMM, e0); v.refill(e0, 1.0)
            real_shift = (e0, sh)
            alone = arb.plan({real_race.name: {ex: v.books[ex].snapshot(50) for ex in real_race.exchange_ids}}, cfg.starting_cash)
            real_worth = sum(ch.qty * ch.edge for ch in alone)   # what the scanner would have earned with no bait around
            stats["real_worth"] = stats.get("real_worth", 0.0) + real_worth
        real_done = False
        # ---- our scanner pass (the logic of run.ArbScanner.one_pass)
        prices = {ex: sim.bot.touch(ex) for ex in v.books}
        cands = arb.candidates_from_prices(prices)
        def apparent(race):
            bids = [prices[e]["bestBid"] for e in race.exchange_ids]
            return sum(bids) - 1 if all(x is not None for x in bids) else 0.0
        if sim.mitig.arb_bait_blacklist_sec:
            cands = [r for r in cands if blacklist.get(r.name, 0) <= now]
        cands.sort(key=apparent, reverse=True)
        cand_books = {}
        for race in cands[: cfg.arb_scan_books]:
            cand_books[race.name] = {ex: v.books[ex].snapshot(50) for ex in race.exchange_ids}
            if race in bait_races:
                stats["slots_bait"] += 1
        if sim.mitig.arb_scan_jitter:
            sim.clock.advance(rng.random() * sim.mitig.arb_scan_jitter)
        plan = arb.plan(cand_books, cfg.starting_cash)
        # the attacker pulls one bait leg just before our multi-leg lands: it knows the :x4.5 phase. With a random phase it
        # is too late half the time (the bait fills and it has paid us the edge) or too early (no candidate at all).
        pulled = not (sim.mitig.arb_scan_jitter and rng.random() < 0.5)
        if pulled:
            for oid in bait_ids[::2]:
                v.cancel(oid)
        bait_names = {r.name for r in bait_races}
        for chunk in plan:
            stats["writes"] += 1
            if chunk.race in bait_names:
                stats["bait_attempts"] += 1
        res = arb.execute(plan, None)
        for r in res:
            chunk = r["chunk"]; resp = r.get("resp") or {}
            if not r.get("ok") and isinstance(resp, dict) and isinstance(resp.get("details"), dict) and resp["details"].get("rejectionReasonCode") == "AllOrNoneNotFilled":
                stats["aon_rejects"] += 1
                if sim.mitig.arb_bait_blacklist_sec:
                    blacklist[chunk.race] = now + sim.mitig.arb_bait_blacklist_sec
            if r.get("ok"):
                traded = [x["data"].get("quantityTraded", 0) for x in resp.get("results", [])]
                sets = min(traded) if traded else 0
                if real_race is not None and chunk.race == real_race.name:
                    stats["real_taken"] += 1; real_done = True
                    stats["arb_edge"] += sets * chunk.edge      # guaranteed settlement value of the complete sets
                    stats["real_captured"] = stats.get("real_captured", 0.0) + sets * chunk.edge
                if chunk.race in bait_names:
                    stats["bait_filled_sets"] += sets
                    stats["arb_edge"] += sets * chunk.edge
                    if sets > 0:
                        stats["attacker_losing_fills"] = stats.get("attacker_losing_fills", 0) + 1
        if real_here and not real_done:
            stats["real_missed"] += 1
            stats["real_missed_worth"] = stats.get("real_missed_worth", 0.0) + real_worth
        if real_shift:
            e0, sh = real_shift
            d = v.dmm[e0]; d.bid -= sh; d.ask -= sh
            v.cancel_owner(DMM, e0); v.refill(e0, 1.0)
        v.cancel_owner(ATK, tag="bait")
        v.cancel_owner("THIRD")
        for ex in touched:               # the room-making shares are sold back into the ladder: the attacker's cost
            q = v.position(ATK, ex)
            if q > 0:
                v.sweep(ATK, ex, "S", v.dmm_touch(ex)[0] - 12, q, tag="room")
        if nonatomic:
            # a leg that filled while the other did not: our code logs `filled=False`, records the resting remainder as an
            # open order and (hedge_residual=false, flatten_residuals unreachable while the scanner thread runs) does nothing
            for race in bait_races:
                legs = list(race.legs.values())
                qs = {l.exchange_id: v.position(US, l.exchange_id) for l in legs}
                res_q = max(qs.values()) - min(qs.values())
                if res_q:
                    stats["residual_shares_max"] = max(stats["residual_shares_max"], res_q)
                    if sim.mitig.arb_flatten:
                        # M8: one partial fill = treat the race as hostile: blacklist it, then flatten once
                        blacklist[race.name] = now + max(600.0, sim.mitig.arb_bait_blacklist_sec)
                        for ex, q in qs.items():
                            v.cancel_owner(US, ex)
                            if q < max(qs.values()):      # we are short this leg without the other: buy it back at the touch
                                cost0 = v.account(US)["cash"]
                                v.sweep(US, ex, "B", v.dmm_touch(ex)[1], max(qs.values()) - q, tag="hedge")
                                stats["hedge_cost"] += -(v.account(US)["cash"] - cost0) - (max(qs.values()) - q) * v.dmm[ex].mid * TICK
        for ex in touched:
            v.refill(ex)
    # accounting: complete sets are worth their edge at settlement; a one-sided residual is directional risk (3-tick move)
    extra = dict(stats)
    extra["arb_edge"] = round(stats["arb_edge"], 2)
    extra["expected_forgone_arb"] = round(stats.get("real_missed_worth", 0.0), 1)
    for k in ("real_worth", "real_captured", "real_missed_worth"):
        extra[k] = round(stats.get(k, 0.0), 1)
    extra["residual_exposure_3ticks"] = round(stats["residual_shares_max"] * 3 * TICK, 2)
    out = sim.result("arb_bait" + ("_nonatomic" if nonatomic else "") + (f"_minret{min_ret}" if min_ret is not None else ""), extra)
    if nonatomic:
        # one-sided fills are directional: mark everything at the DMM mid (sim.result), minus the genuine arbs we missed
        out["us"] = round(out["us"] - extra["expected_forgone_arb"], 2)
    else:
        out["us"] = round(stats["arb_edge"] - extra["expected_forgone_arb"], 2)
    out["us_arb"] = out["us"]
    out["us_per_hour"] = round(out["us"] / max(1e-9, sim.hours()), 2)
    return out


def atk_recycle(sim: Sim, cycles: int, held_sets: int = 4500, relock: bool = True) -> dict:
    """(j) Recycle bait. The scanner stores `best_unfunded` (the best chunk it saw, executed or not) and the main loop, when
    the arb budget is locked, unwinds a HELD riskless set at the asks (all-or-none) to fund it if the new return beats the
    held set's remaining return by recycle_min_gain. A bait with >= recycle_min_new_ret of phantom edge, pulled as our
    order arrives, makes us sell a riskless position for nothing. `relock` models the steady state in which the budget is
    always fully committed (the live state: 28k shares of sets held against a 25% budget)."""
    v, cfg = sim.venue, sim.cfg
    arb = ArbEngine(sim.bot.api, sim.bot.store, cfg.arb, sim.u, cfg.tournament_slug, cfg.starting_cash, dry_run=False)
    with open(os.path.join(HERE, "data", "books.json")) as f:
        allbooks = json.load(f)
    for ex, b in allbooks.items():
        if ex not in v.books and b.get("bids") and b.get("asks") and ex in sim.u.by_exchange:
            v.add_market(ex, T(b["bids"][0]["price"]), T(b["asks"][0]["price"]), sim.sa.touch_qty, sim.sa.growth, 12, 0.0,
                         [int(l["quantity"]) for l in b["bids"][:12]], [int(l["quantity"]) for l in b["asks"][:12]])
    for r in sim.u.races.values():
        exs = [e for e in r.exchange_ids if e in v.books]
        if len(exs) != len(r.exchange_ids):
            continue
        excess = sum(v.dmm_touch(e)[0] for e in exs) - 200
        if excess > 0:
            e = max(exs, key=lambda x: v.dmm_touch(x)[0]); d = v.dmm[e]; d.bid -= excess; d.ask -= excess
            v.cancel_owner(DMM, e); v.refill(e, 1.0)
        short = 200 - int(round(arb.x_for(r) / TICK)) - 1 - sum(v.dmm_touch(e)[1] for e in exs)
        if short > 0:
            e = min(exs, key=lambda x: v.dmm_touch(x)[1]); d = v.dmm[e]; d.bid += short; d.ask += short
            v.cancel_owner(DMM, e); v.refill(e, 1.0)
    races = [r for r in sim.u.races.values() if len(r.legs) == 2 and all(l.exchange_id in v.books for l in r.legs.values())]
    def room(r):
        return sum(v.dmm_touch(l.exchange_id)[1] for l in r.legs.values()) - 2 - 200
    # held sets: three one-tick-spread races (the cheapest to unwind, so the first the recycler picks), 4,500 sets each
    tight = sorted(races, key=lambda r: (sum(v.dmm_touch(l.exchange_id)[1] - v.dmm_touch(l.exchange_id)[0] for l in r.legs.values()),
                                         -sum(v.dmm[l.exchange_id].target(0, "S") for l in r.legs.values())))
    held = [r for r in tight if all(v.dmm[l.exchange_id].target(0, "S") >= held_sets for l in r.legs.values())][:3] or tight[:3]
    for r in held:
        for l in r.legs.values():
            v.account(US)["pos"][l.exchange_id] = -held_sets
        arb.committed[r.name] = held_sets * 0.99
    def lock():
        arb.committed["__other_sets__"] = max(0.0, cfg.arb.capital_frac * cfg.starting_cash - sum(q for k, q in arb.committed.items() if k != "__other_sets__") - 1.0)
    lock()
    need_ticks = int(math.ceil(max(cfg.arb.recycle_min_new_ret, cfg.arb.recycle_min_gain + 0.015) * 0.99 / TICK)) + 2
    cands = [r for r in races if r not in held]
    def thin_leg(r):
        return min(r.exchange_ids, key=lambda e: sum(v.dmm[e].target(k, "S") for k in range(max(0, need_ticks - room(r)))))
    def make_room_cost(r):
        k = max(0, need_ticks - room(r))
        return sum(v.dmm[thin_leg(r)].target(j, "S") * (j + 1) for j in range(k)) * TICK
    bait = sorted(cands, key=make_room_cost)[0]
    stats = dict(cycles=cycles, held_races=[r.name for r in held], bait_race=bait.name, bait_need_ticks=need_ticks, unwinds=0, sets_unwound=0,
                 forgone_edge=0.0, best_unfunded_seen=0, aon_rejects=0, verify_blocked=0, held_sets_left=held_sets * len(held))
    for c in range(cycles):
        sim.clock.advance(BOT_CYCLE_SEC)
        if relock:
            lock()
        k = max(0, need_ticks - room(bait))
        e_thin = thin_leg(bait)
        levels = {e: v.dmm_touch(e)[1] - 1 for e in bait.exchange_ids}
        if k > 0:
            v.sweep(ATK, e_thin, "B", v.dmm_touch(e_thin)[1] + k - 1, tag="room")
            levels[e_thin] = v.dmm_touch(e_thin)[1] + k - 1
        bait_ids = []
        for ex, t in levels.items():
            oid, _ = v.submit(ATK, ex, "B", t, 5000, tag="bait"); bait_ids.append(oid)
        # scanner pass: best_unfunded from the bait's book, plan (budget-locked or below min_ret), execution fails all-or-none
        books = {ex: v.books[ex].snapshot(50) for ex in bait.exchange_ids}
        allc = [ch for ch in arb.chunks_for_race(bait, books) if ch.qty >= cfg.arb.min_set_qty]
        best = None
        if allc:
            b = max(allc, key=lambda ch: ch.ret)
            best = {"race": b.race, "kind": b.kind, "ret": b.ret, "cost": b.cost, "qty": b.qty, "ts": sim.clock.time()}
            stats["best_unfunded_seen"] += 1
        plan = arb.plan({bait.name: books}, cfg.starting_cash)
        for oid in bait_ids[::2]:
            v.cancel(oid)                           # the attacker pulls one leg as our order arrives
        res = arb.execute(plan, None) if plan else []
        failed = any(not r.get("ok") for r in res)
        if failed:
            stats["aon_rejects"] += 1
        # main loop: recycle a held set to fund `best`
        if best is not None:
            if sim.mitig.recycle_verify:
                fresh = {ex: v.books[ex].snapshot(50) for ex in bait.exchange_ids}
                still = [ch for ch in arb.chunks_for_race(bait, fresh) if ch.qty >= cfg.arb.min_set_qty and ch.ret >= best["ret"] - 1e-9]
                if failed or not still:
                    stats["verify_blocked"] += 1
                    best = None
        if best is not None:
            held_books = {r.name: {ex: v.books[ex].snapshot(50) for ex in r.exchange_ids} for r in held if r.name in arb.committed}
            positions = v.positions(US)
            rec = arb.recycle(best, held_books, positions, cfg.starting_cash, None)
            if rec and not rec.get("dry"):
                stats["unwinds"] += 1; stats["sets_unwound"] += rec["qty"]
                stats["forgone_edge"] += rec["qty"] * (1.0 - rec["proceeds"])
        v.cancel_owner(ATK, tag="bait")
        q = v.position(ATK, e_thin)
        if q > 0:
            v.sweep(ATK, e_thin, "S", v.dmm_touch(e_thin)[0] - 12, q, tag="room")
        for r in held + [bait]:
            for l in r.legs.values():
                v.refill(l.exchange_id)
        stats["held_sets_left"] = sum(-min(0, v.position(US, l.exchange_id)) for r in held for l in r.legs.values()) // 2
        if stats["held_sets_left"] <= 0:
            stats["all_unwound_at_cycle"] = c + 1
            break
    out = sim.result("recycle_bait", dict(stats, forgone_edge=round(stats["forgone_edge"], 2)))
    out["us"] = out["us_arb"] = round(-stats["forgone_edge"], 2)      # riskless settlement profit given away by the unwinds
    out["us_per_hour"] = round(out["us"] / max(1e-9, sim.hours()), 2)
    return out


ATTACKS = {
    "sweep": atk_sweep, "pickoff": atk_pickoff, "spoof": atk_spoof, "flipflop": atk_flipflop, "tilt_bait": atk_tilt_bait,
    "gate_poke": atk_gate_poke, "cap_push": atk_cap_push, "poison": atk_poison, "poison_toxic": lambda s, c: atk_poison(s, c, "toxic"),
    "arb_bait": atk_arb_bait, "arb_partial": lambda s, c: atk_arb_bait(s, c, nonatomic=True), "recycle": atk_recycle,
    "arb_bait_lowret": lambda s, c: atk_arb_bait(s, c, min_ret=0.005),     # the scanner before the 4% min_ret floor
}
# which of the proposed mitigations each attack is meant to be answered by (the rest stay at baseline)
MITIG_FOR = {
    "sweep": dict(inside_size_cap=25, micro_floor=100, micro_cap=3000),
    "pickoff": dict(inside_size_cap=25),
    "spoof": dict(micro_floor=100, micro_cap=3000, requote_ticks=1.0),
    "flipflop": dict(rotate_requote=True, requote_ticks=1.0, micro_cap=3000, fill_requote_min=10),
    "tilt_bait": dict(tilt_touch_floor=100),
    "gate_poke": dict(gate_persist=2),
    "cap_push": dict(inside_size_cap=25),
    "poison": dict(fill_requote_min=10),
    "poison_toxic": dict(fill_requote_min=10),
    "arb_bait": dict(arb_bait_blacklist_sec=600, arb_scan_jitter=2.0),
    "arb_partial": dict(arb_flatten=True, arb_bait_blacklist_sec=600),
    "recycle": dict(recycle_verify=True),
    "arb_bait_lowret": dict(arb_bait_blacklist_sec=600, arb_scan_jitter=2.0),
}


def load_inputs(cfg: BotConfig):
    with open(os.path.join(HERE, "data", "markets.json")) as f:
        u = Universe.from_markets(json.load(f))
    with open(os.path.join(HERE, "data", "books.json")) as f:
        books = json.load(f)
    vol_path = os.path.join(HERE, "data", "volume_cache.json")
    volume = json.load(open(vol_path)) if os.path.exists(vol_path) else {}
    return u, books, volume


def run_one(name: str, cycles: int, cfg: BotConfig, u, books, volume, sa: SimArgs, mitig: Mitig) -> dict:
    sim = Sim(cfg, u, books, volume, sa, mitig)
    try:
        return ATTACKS[name](sim, cycles)
    finally:
        sim.close()


def fmt_row(r: dict, keys: list[str]) -> str:
    return " | ".join(f"{r.get(k, ''):>12}" if not isinstance(r.get(k), float) else f"{r[k]:>12.2f}" for k in keys)


def main():
    ap = argparse.ArgumentParser(description="sigbot adversarial paper simulator")
    ap.add_argument("--attacks", default="all")
    ap.add_argument("--cycles", type=int, default=200)
    ap.add_argument("--mitigations", default="both", choices=["none", "proposed", "both"])
    ap.add_argument("--dmm", default="real", choices=["real", "deep", "thin"],
                    help="real: per-level quantities from data/books.json; deep: 2.4k touch growing 1.53x/tick; thin: 500 touch")
    ap.add_argument("--touch-qty", type=float, default=None)
    ap.add_argument("--growth", type=float, default=1.53)
    ap.add_argument("--recenter", type=float, default=0.0, help="share of the last print the DMM ladder re-centres on (0 = static)")
    ap.add_argument("--refill", type=float, default=0.5, help="share of a depleted DMM level restored per 30 s cycle (1 = instant)")
    ap.add_argument("--n-mm", type=int, default=None, help="quoted markets to simulate (default: mm.n_markets from config)")
    ap.add_argument("--n-tilt", type=int, default=8)
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", default=None, help="write results here")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    cfg = BotConfig.load(args.config)
    cfg.api_key = ""
    u, books, volume = load_inputs(cfg)
    touch = args.touch_qty or (2400.0 if args.dmm == "deep" else 500.0)
    n_mm = args.n_mm or cfg.mm.n_markets
    sa = SimArgs(touch_qty=touch, growth=args.growth, recenter=args.recenter, n_mm=n_mm, n_tilt=args.n_tilt, seed=args.seed,
                 real=(args.dmm == "real"), refill_frac=args.refill)
    names = list(ATTACKS) if args.attacks == "all" else [a.strip() for a in args.attacks.split(",")]
    results = []
    dmm_desc = "real books.json ladders" if sa.real else f"synthetic touch={touch:.0f} growth={args.growth}"
    print(f"sigbot adversary: cycles={args.cycles} dmm={dmm_desc} refill/cycle={args.refill} recenter={args.recenter} "
          f"ladder={cfg.mm.level_ticks[:cfg.mm.levels]} base_qty={cfg.mm.base_qty} n_mm={n_mm} arb.min_ret={cfg.arb.min_ret} "
          f"recycle={cfg.arb.recycle_enabled}")
    for name in names:
        variants = []
        if args.mitigations in ("none", "both"):
            variants.append(("baseline", Mitig()))
        if args.mitigations in ("proposed", "both"):
            variants.append(("mitigated", Mitig(**MITIG_FOR.get(name, {}))))
        for label, mit in variants:
            t0 = _time.time()
            r = run_one(name, args.cycles, cfg, u, books, volume, sa, mit)
            r["variant"] = label
            r["mitig"] = {k: v for k, v in mit.__dict__.items() if v not in (False, 0, None, 0.0, 1)}
            r["wall_s"] = round(_time.time() - t0, 1)
            results.append(r)
            core = {k: r[k] for k in ("attack", "variant", "hours", "attacker", "us", "us_mm", "us_tilt", "us_arb", "dmm", "us_per_hour")}
            extra = {k: v for k, v in r.items() if k not in core and k not in ("mitig", "wall_s")}
            print(json.dumps(core), "\n   ", json.dumps(extra), "\n    mitig:", json.dumps(r["mitig"]))
    print("\nsummary (SUSQies; us = our marked P&L, negative is what the attacker or the DMM took from us):")
    keys = ["attack", "variant", "hours", "attacker", "us", "us_per_hour"]
    print(" | ".join(f"{k:>12}" for k in keys))
    for r in results:
        print(fmt_row(r, keys))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
