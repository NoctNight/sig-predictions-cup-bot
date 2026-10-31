"""Fake venue for the offline pipeline simulation (tests/sim_pipeline.py). No network, no files written.

  SimClock     a settable clock; sim_pipeline installs it as time.time / time.sleep and as datetime.now in sigbot modules
  Venue        price-time limit order books per exchange with a designated market maker (DMM) resting a ladder from
               ~2,000 shares at the touch, refilled at the same prices after every trade; a stream of fake taker flow;
               our orders match against the DMM, never against ourselves (self-trade prevention cancels the crossing
               remainder); every order is a BUY of YES or NO (a NO buy at p rests as a YES ask at 1 - p); cash and positions
               are kept like the venue: buys pay price x qty, a buy opposite a held side nets and credits 1 per netted share,
               a buy is shrunk to what cash can pay; all-or-none multi-leg baskets fill every leg completely or nothing.
  FakeClient   the SuperMarketClient surface the bot calls (reads, writes, new_key, stats, tournament_id)
  FakeFeed     the RealtimeFeed surface the bot calls; can go silent (books frozen, no messages) on the venue's command

Everything the bot sends is logged in `Venue.calls` and every one of our fills in `Venue.fill_meta` (position before and
after, price versus mid), so the invariant checker can audit each step. Order-level problems (bad quantity, bad price,
missing or past expiry) are recorded in `Venue.violations` at arrival.
"""
from __future__ import annotations

import bisect
import itertools
import math
import random
from datetime import datetime, timezone
from typing import Callable, Optional

TICK = 0.005
DMM_SIZES = (2000, 3000, 5000, 10000, 20000, 40000)


def T(p: float) -> int:
    return int(math.floor(p / TICK + 0.5))


def P(t: int) -> float:
    return round(t * TICK, 3)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % int((ts % 1) * 1000)


def parse_iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class SimClock:
    def __init__(self, t0: float):
        self.t = float(t0)

    def time(self) -> float:
        return self.t

    def sleep(self, s: float):
        if s and s > 0:
            self.t += s

    def advance(self, s: float):
        self.t += s


class Order:
    __slots__ = ("id", "key", "prefix", "ex", "side", "limit", "tick", "book", "qty", "orig", "filled", "expiry", "t", "open",
                 "reason")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Pos:
    __slots__ = ("yes", "yes_cost", "no", "no_cost")

    def __init__(self):
        self.yes = self.no = 0
        self.yes_cost = self.no_cost = 0.0

    @property
    def q(self) -> int:
        return self.yes - self.no


class Venue:
    def __init__(self, clock: SimClock, markets: list, touch: dict, rng: random.Random, api_error, tournament_id: str,
                 volume: Optional[dict] = None, starting_cash: float = 100000.0, trades_per_sec: float = 0.6):
        self.clock, self.rng, self.ApiError, self.tid = clock, rng, api_error, tournament_id
        self.markets = [m for m in markets if m.get("status") == "open" and m.get("exchanges")]
        self.ex_market = {str(m["exchanges"][0]["id"]): str(m["id"]) for m in self.markets}
        self.exs = sorted(self.ex_market)
        self.bid_t: dict[str, int] = {}
        self.ask_t: dict[str, int] = {}
        for ex in self.exs:
            b, a = touch[ex]
            self.bid_t[ex], self.ask_t[ex] = b, a
        self.dmm = {ex: {"bid": {}, "ask": {}} for ex in self.exs}
        self.ours: dict[str, list] = {ex: [] for ex in self.exs}
        self.orders: dict[int, Order] = {}
        self.cash = float(starting_cash)
        self.pos: dict[str, Pos] = {ex: Pos() for ex in self.exs}
        self.fills: list = []                 # our fills, oldest first (API shape)
        self.fill_meta: dict = {}             # fill id -> audit record
        self.trades: list = []                # public prints (feed/tape shape)
        self.trades_by_ex: dict = {ex: [] for ex in self.exs}
        self.last_px: dict = {}
        self.ver = {ex: 0 for ex in self.exs}
        self._book_cache: dict = {}
        self.calls: list = []                 # everything we sent (for rate and invariant checks)
        self.step = ""                        # label of the simulation step in progress (stamped on every call)
        self.violations: list = []            # order-level violations found at arrival
        self.outside: dict = {}               # ex -> outside (Polymarket/Kalshi) fair
        self.kalshi_offset: dict = {}         # ex -> Kalshi minus Polymarket
        self.write_latency = 0.0              # seconds the clock advances inside every write call (slow write path)
        self.reject_aon: Optional[Callable] = None   # (legs, prefix) -> True to reject the basket (someone else took it)
        self.silent_since: Optional[float] = None
        self.silent_until: Optional[float] = None
        self.frozen_books: dict = {}
        self.spread_paid = 0.0                # sum over our fills of max(0, price paid - mid) * qty (crossing cost)
        self.flow_bias: dict = {}             # ex -> P(taker buys YES) - 0.5
        self._ids = itertools.count(1001)
        self._fids = itertools.count(500001)
        self._tseq = itertools.count(9000001)
        self._keys = itertools.count(1)
        self._ver_seq = itertools.count(1)
        vol = volume or {}
        w = [max(1.0, float(vol.get(ex, 0.0))) ** 0.7 for ex in self.exs]
        tot = sum(w)
        self.rate = {ex: trades_per_sec * x / tot for ex, x in zip(self.exs, w)}
        self._cum = list(itertools.accumulate(w))
        self.total_rate = trades_per_sec
        for ex in self.exs:
            self._refill(ex)

    # ------------------------------------------------------------------ basics
    @property
    def now(self) -> float:
        return self.clock.t

    def new_key(self, prefix: str = "sb") -> str:
        return f"{prefix}-{next(self._keys)}"

    def mid(self, ex: str) -> float:
        return (self.bid_t[ex] + self.ask_t[ex]) * TICK / 2

    def _touch(self, ex):
        self.ver[ex] = next(self._ver_seq)

    def _violate(self, kind: str, detail: str):
        self.violations.append({"t": self.now, "kind": kind, "detail": detail})

    def _refill(self, ex: str):
        d = self.dmm[ex]
        bids, asks = {}, {}
        for k, q in enumerate(DMM_SIZES):
            bt, at = self.bid_t[ex] - k, self.ask_t[ex] + k
            if 1 <= bt <= 199:
                bids[bt] = q
            if 1 <= at <= 199:
                asks[at] = q
        if bids != d["bid"] or asks != d["ask"]:
            d["bid"], d["ask"] = bids, asks
            self._touch(ex)

    # ------------------------------------------------------------- accounting
    def _affordable(self, ex: str, side: str, px: float, q: int) -> int:
        p = self.pos[ex]
        opp = p.no if side == "yes" else p.yes
        if q <= opp:
            return q
        if px <= 0:
            return q
        return int(max(opp, min(q, math.floor((self.cash + opp) / px + 1e-9))))

    def _apply_fill(self, o: Optional[Order], ex: str, side: str, px: float, q: int, maker: bool, key: str = "", prefix: str = ""):
        p = self.pos[ex]
        before = p.q
        self.cash -= q * px
        if side == "yes":
            m = min(q, p.no)
            if m:
                p.no_cost -= p.no_cost * m / p.no
                p.no -= m
                self.cash += m
            p.yes += q - m
            p.yes_cost += (q - m) * px
        else:
            m = min(q, p.yes)
            if m:
                p.yes_cost -= p.yes_cost * m / p.yes
                p.yes -= m
                self.cash += m
            p.no += q - m
            p.no_cost += (q - m) * px
        mid = self.mid(ex)
        mark = mid if side == "yes" else 1 - mid
        cvm = q * (px - mark)
        if cvm > 0:
            self.spread_paid += cvm
        fid = next(self._fids)
        f = {"id": fid, "orderId": o.id if o is not None else None, "exchangeId": ex, "marketId": self.ex_market[ex], "price": round(px, 4),
             "quantity": q if side == "yes" else -q, "side": side, "filledAt": iso(self.now)}
        self.fills.append(f)
        self.fill_meta[fid] = {"t": self.now, "order_id": f["orderId"], "key": o.key if o is not None else key,
                               "prefix": o.prefix if o is not None else prefix, "ex": ex, "side": side, "qty": q, "price": px,
                               "before": before, "after": p.q, "maker": maker, "cost_vs_mid": cvm, "mid": mid}
        if self.cash < -1e-6:
            self._violate("harness_negative_cash", f"cash {self.cash:.2f} after fill {f}")
        return f

    def _print(self, ex: str, tick: int, q: int, space: str):
        seq = next(self._tseq)
        px = P(tick)
        t = {"id": str(seq), "sequence": seq, "exchangeId": ex, "marketId": self.ex_market[ex], "price": px, "size": int(q),
             "quantity": int(q), "side": space, "volume": round(q * (px if space == "YES" else 1 - px), 4), "createdAt": iso(self.now),
             "executedAt": iso(self.now)}
        self.trades.append(t)
        self.trades_by_ex[ex].append(t)
        del self.trades_by_ex[ex][:-400]
        self.last_px[ex] = px

    # ----------------------------------------------------------------- books
    def _our_at(self, ex: str, book: str, tick: int) -> list:
        return [o for o in self.ours[ex] if o.book == book and o.tick == tick and o.open]

    def _levels(self, ex: str, book: str) -> list:
        ticks = set(self.dmm[ex][book]) | {o.tick for o in self.ours[ex] if o.book == book and o.open}
        return sorted(ticks, reverse=(book == "bid"))

    def book(self, ex: str, depth: int = 50) -> dict:
        c = self._book_cache.get(ex)
        if c is not None and c[0] == self.ver[ex] and c[1] >= depth:
            return c[2]
        out = {}
        for book in ("bid", "ask"):
            rows = []
            for tk in self._levels(ex, book)[:depth]:
                q = self.dmm[ex][book].get(tk, 0) + sum(o.qty for o in self._our_at(ex, book, tk))
                if q > 0:
                    rows.append({"price": P(tk), "quantity": q})
            out[book] = rows
        bids, asks = out["bid"], out["ask"]
        bb, ba = (bids[0]["price"] if bids else None), (asks[0]["price"] if asks else None)
        b = {"exchangeId": ex, "marketId": self.ex_market[ex], "asOf": {"sequence": self.ver[ex], "at": iso(self.now)}, "depth": depth,
             "bids": bids, "asks": asks, "bestBid": bb, "bestAsk": ba, "spread": round(ba - bb, 3) if bb is not None and ba is not None else None}
        self._book_cache[ex] = (self.ver[ex], depth, b)
        return b

    def book_copy(self, ex: str, depth: int = 50, **extra) -> dict:
        b = self.book(ex, depth)
        return dict(b, bids=[dict(l) for l in b["bids"]], asks=[dict(l) for l in b["asks"]], **extra)

    # -------------------------------------------------------------- matching
    def _walk(self, ex: str, take_book: str, limit_tick: int, qty: int, taker_us: bool):
        """Levels of `take_book` ('ask' for a YES buy, 'bid' for a NO buy) within limit. Returns ([(tick, from_dmm, [(order, q)])],
        stp). For our own taker orders, reaching a level that holds one of our orders stops the walk (self-trade prevention)."""
        out, rem, stp = [], qty, False
        for tk in self._levels(ex, take_book):
            if rem <= 0:
                break
            if (take_book == "ask" and tk > limit_tick) or (take_book == "bid" and tk < limit_tick):
                break
            dq = self.dmm[ex][take_book].get(tk, 0)
            ours = self._our_at(ex, take_book, tk)
            if taker_us:
                take = min(rem, dq)
                if take > 0:
                    out.append((tk, take, []))
                    rem -= take
                if ours and rem > 0:
                    stp = True
                    break
                continue
            our_tot = sum(o.qty for o in ours)
            take = min(rem, dq + our_tot)
            if take <= 0:
                continue
            ours_share = take if dq == 0 else min(our_tot, int(take * our_tot / (dq + our_tot)))
            alloc, left = [], ours_share
            for o in ours:
                if left <= 0:
                    break
                a = min(o.qty, left)
                alloc.append((o, a))
                left -= a
            out.append((tk, take - (ours_share - left), alloc))
            rem -= take - left
        return out, stp

    def _consume_dmm(self, ex, book, tk, q):
        d = self.dmm[ex][book]
        d[tk] = max(0, d.get(tk, 0) - q)
        self._touch(ex)

    def _fill_maker(self, o: Order, q: int):
        """One of our resting orders trades q as maker at its own price (shrunk to what cash pays)."""
        px = o.limit
        q = min(q, self._affordable(o.ex, o.side, px, q))
        if q <= 0:
            self._close(o, "InsufficientBalance")
            return 0
        self._apply_fill(o, o.ex, o.side, px, q, maker=True)
        o.qty -= q
        o.filled += q
        if o.qty <= 0:
            self._close(o, None)
        self._touch(o.ex)
        return q

    def _close(self, o: Order, reason):
        o.open = False
        o.reason = reason
        try:
            self.ours[o.ex].remove(o)
        except ValueError:
            pass
        self._touch(o.ex)

    def external_take(self, ex: str, buy_yes: bool, qty: int, max_ticks: int = 3):
        """A taker from the crowd: buys YES lifting asks (or sells YES hitting bids) up to max_ticks through the touch."""
        if buy_yes:
            lim = self.ask_t[ex] + max_ticks - 1
            levels, _ = self._walk(ex, "ask", lim, qty, taker_us=False)
            book, space = "ask", "YES"
        else:
            lim = self.bid_t[ex] - max_ticks + 1
            levels, _ = self._walk(ex, "bid", lim, qty, taker_us=False)
            book, space = "bid", "NO"
        for tk, from_dmm, alloc in levels:
            done = from_dmm
            if from_dmm:
                self._consume_dmm(ex, book, tk, from_dmm)
            for o, a in alloc:
                done += self._fill_maker(o, a)
            if done:
                self._print(ex, tk, done, space)

    # ---------------------------------------------------------------- orders
    def _validate(self, ex, side, action, qty, price, expiration, key, aon=False):
        tag = f"{key} {ex} {side} q={qty} px={price} exp={expiration}"
        if ex not in self.ex_market:
            raise self.ApiError(404, "NOT_FOUND", f"exchange {ex}")
        if action != "buy" and not (aon and action == "sell"):     # a conventional sell only in a basket (the offload executor)
            self._violate("non_buy_order", tag)
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            self._violate("bad_quantity", tag)
            raise self.ApiError(400, "VALIDATION_ERROR", "quantity must be a positive integer")
        if price is None:
            self._violate("market_order", tag)
            raise self.ApiError(400, "VALIDATION_ERROR", "simulation: market orders not supported")
        if not (0.005 - 1e-9 <= price <= 0.995 + 1e-9) or abs(price / TICK - round(price / TICK)) > 1e-6:
            self._violate("bad_price", tag)
            raise self.ApiError(400, "VALIDATION_ERROR", "price must be on the 0.005 tick in [0.005, 0.995]")
        if aon:
            return None
        if not expiration:
            self._violate("no_expiry", tag)
            return None
        exp = parse_iso(expiration)
        if exp <= self.now:
            self._violate("expired_on_arrival", f"{tag} arrived {iso(self.now)} ({self.now - exp:.0f}s late)")
            raise self.ApiError(400, "VALIDATION_ERROR", "expirationDate must be in the future")
        return exp

    def place(self, ex, side, action, qty, price, expiration=None, key=None, aon=False) -> dict:
        ex = str(ex)
        key = key or self.new_key("ord")
        prefix = key.split("-")[0]
        exp = self._validate(ex, side, action, qty, price, expiration, key)
        rec = {"t": self.now, "step": self.step, "kind": "order", "key": key, "prefix": prefix, "ex": ex, "side": side, "qty": qty, "price": price, "mid": self.mid(ex),
               "exp": exp, "cash_before": self.cash, "pos_before": self.pos[ex].q}
        self.calls.append(rec)
        p = self.pos[ex]
        opp = p.no if side == "yes" else p.yes
        if opp <= 0 and self.cash < price:
            rec["ok"] = False
            raise self.ApiError(400, "VALIDATION_ERROR", "insufficient balance")
        o = Order(id=next(self._ids), key=key, prefix=prefix, ex=ex, side=side, limit=round(price, 3), qty=qty, orig=qty, filled=0,
                  expiry=exp, t=self.now, open=True, reason=None)
        if side == "yes":
            o.tick, o.book, take_book, lim = T(price), "bid", "ask", T(price)
        else:
            o.tick, o.book, take_book, lim = T(1 - price), "ask", "bid", T(1 - price)
        self.orders[o.id] = o
        levels, stp = self._walk(ex, take_book, lim, qty, taker_us=True)
        traded = 0
        for tk, from_dmm, _ in levels:
            px = P(tk) if side == "yes" else round(1 - P(tk), 3)
            q = min(from_dmm, self._affordable(ex, side, px, from_dmm))
            if q <= 0:
                break
            self._consume_dmm(ex, take_book, tk, q)
            self._apply_fill(o, ex, side, px, q, maker=False)
            self._print(ex, tk, q, "YES" if side == "yes" else "NO")
            traded += q
            if q < from_dmm:
                break
        o.qty -= traded
        o.filled = traded
        if o.qty > 0 and not stp:
            self.ours[ex].append(o)
            self._touch(ex)
        else:
            o.open = False
            o.reason = "SelfTradePrevented" if stp and o.qty > 0 else None
        rec.update(ok=True, order_id=o.id, traded=traded, stp=stp, cash_after=self.cash, pos_after=self.pos[ex].q)
        return {"orderId": o.id, "exchangeId": ex, "open": o.open, "remainingQuantity": o.qty if o.open else 0, "action": "buy",
                "side": side, "price": o.limit, "quantity": qty, "filledQuantity": traded, "quantityTraded": traded,
                "terminalReasonCode": o.reason}

    def multi_leg(self, legs: list, all_or_none: bool, key: Optional[str]) -> dict:
        key = key or self.new_key("ml")
        prefix = key.split("-")[0]
        rec = {"t": self.now, "step": self.step, "kind": "multileg", "key": key, "prefix": prefix, "aon": all_or_none, "legs": [dict(l) for l in legs],
               "cash_before": self.cash, "ok": False}
        self.calls.append(rec)
        if not all_or_none:
            self._violate("non_atomic_basket", f"{key}: multi-leg without allOrNone {legs}")
        for i, l in enumerate(legs):
            try:
                self._validate(str(l["exchangeId"]), l["side"], l.get("action", "buy"), l["quantity"], l.get("price"), None, key, aon=True)
            except self.ApiError as e:
                e.details = {"leg": i}
                raise
        # a fully-backed sell closes like its complement buy (openapi: `sell yes q@p` = `buy no q@(1-p)`)
        legs = [l if l.get("action", "buy") == "buy" else dict(l, side="no" if l["side"] == "yes" else "yes", action="buy",
                                                                price=round(1.0 - float(l["price"]), 3)) for l in legs]
        if self.reject_aon is not None and self.reject_aon(legs, prefix):
            rec["reason"] = "rejected (scenario)"
            raise self.ApiError(400, "VALIDATION_ERROR", "all-or-none basket not filled",
                                {"rejectionReasonCode": "AllOrNoneNotFilled", "leg": 0})
        plans = []
        cash = self.cash
        for i, l in enumerate(legs):
            ex, side, q, price = str(l["exchangeId"]), l["side"], int(l["quantity"]), float(l["price"])
            take_book, lim = ("ask", T(price)) if side == "yes" else ("bid", T(1 - price))
            levels, stp = self._walk(ex, take_book, lim, q, taker_us=True)
            got = sum(x[1] for x in levels)
            if got < q:
                rec["reason"] = f"leg {i} fillable {got}/{q}" + (" (own order: self-trade)" if stp else "")
                raise self.ApiError(400, "VALIDATION_ERROR", "all-or-none basket not filled",
                                    {"rejectionReasonCode": "AllOrNoneNotFilled", "leg": i})
            p = self.pos[ex]
            opp = p.no if side == "yes" else p.yes
            cost = sum(x[1] * (P(x[0]) if side == "yes" else 1 - P(x[0])) for x in levels)
            cash -= cost - min(q, opp)
            if cash < -1e-9:
                rec["reason"] = f"leg {i} insufficient balance"
                raise self.ApiError(400, "VALIDATION_ERROR", "insufficient balance", {"leg": i})
            plans.append((i, ex, side, q, price, take_book, levels))
        results = []
        for i, ex, side, q, price, take_book, levels in plans:
            o = Order(id=next(self._ids), key=key, prefix=prefix, ex=ex, side=side, limit=round(price, 3), qty=0, orig=q, filled=q,
                      expiry=None, t=self.now, open=False, reason=None, tick=None, book=None)
            self.orders[o.id] = o
            tot = 0.0
            for tk, from_dmm, _ in levels:
                px = P(tk) if side == "yes" else round(1 - P(tk), 3)
                self._consume_dmm(ex, take_book, tk, from_dmm)
                self._apply_fill(o, ex, side, px, from_dmm, maker=False)
                self._print(ex, tk, from_dmm, "YES" if side == "yes" else "NO")
                tot += px * from_dmm
            results.append({"index": i, "data": {"orderId": o.id, "exchangeId": ex, "open": False, "remainingQuantity": 0, "action": "buy",
                                                 "side": side, "price": round(price, 3), "quantity": q, "quantityTraded": q,
                                                 "totalCost": round(tot, 4), "terminalReasonCode": None}})
        rec.update(ok=True, cash_after=self.cash, traded=[r["data"]["quantityTraded"] for r in results],
                   order_ids=[r["data"]["orderId"] for r in results])
        return {"results": results}

    def cancel_all(self, exchange_id=None, market_id=None) -> dict:
        rec = {"t": self.now, "step": self.step, "kind": "cancel_all", "ex": str(exchange_id) if exchange_id else None}
        self.calls.append(rec)
        n = 0
        exs = [str(exchange_id)] if exchange_id else list(self.ours)
        for ex in exs:
            for o in list(self.ours.get(ex, [])):
                self._close(o, "Cancelled")
                n += 1
        rec["cancelled"] = n
        return {"cancelled": n}

    def cancel_order(self, order_id) -> dict:
        o = self.orders.get(int(order_id))
        if o is not None and o.open:
            self._close(o, "Cancelled")
        self.calls.append({"t": self.now, "kind": "cancel", "order_id": order_id})
        return {"ok": True}

    def expire(self):
        now = self.now
        for ex, lst in self.ours.items():
            for o in list(lst):
                if o.expiry is not None and o.expiry <= now:
                    self._close(o, "Expired")

    # ------------------------------------------------------------ market moves
    def set_touch(self, ex: str, bid_t: int, ask_t: int):
        """Move the DMM's touch. Our resting orders the new DMM quotes cross trade against the DMM at our price."""
        bid_t, ask_t = max(1, min(198, bid_t)), max(2, min(199, ask_t))
        if ask_t <= bid_t:
            ask_t = bid_t + 1
        self.bid_t[ex], self.ask_t[ex] = bid_t, ask_t
        self._refill(ex)
        for o in list(self.ours[ex]):
            if (o.book == "bid" and o.tick >= ask_t) or (o.book == "ask" and o.tick <= bid_t):
                want = o.qty
                q = self._fill_maker(o, want)
                if q:
                    self._print(ex, o.tick, q, "NO" if o.book == "bid" else "YES")
                if o.open:                     # could not pay for all of it: the venue cancels what cash cannot cover
                    self._close(o, "InsufficientBalance")

    def shift(self, ex: str, ticks: int):
        self.set_touch(ex, self.bid_t[ex] + ticks, self.ask_t[ex] + ticks)

    def sweep(self, ex: str, ticks: int):
        """A taker sweeps |ticks| ticks through the book (taking the DMM and any of our quotes on the way), then the DMM
        re-centres there."""
        if ticks > 0:
            self.external_take(ex, True, 10 ** 7, max_ticks=ticks)
        else:
            self.external_take(ex, False, 10 ** 7, max_ticks=-ticks)
        self.shift(ex, ticks)

    def flow(self, dt: float):
        """Random taker flow for dt seconds, then expiries and DMM refills."""
        n, t = 0, self.rng.expovariate(self.total_rate)
        while t < dt:
            n += 1
            t += self.rng.expovariate(self.total_rate)
        for _ in range(n):
            ex = self.exs[bisect.bisect_left(self._cum, self.rng.random() * self._cum[-1])]
            q = int(min(3000, max(1, self.rng.lognormvariate(math.log(150), 1.0))))
            buy = self.rng.random() < 0.5 + self.flow_bias.get(ex, 0.0)
            self.external_take(ex, buy, q)
        self.expire()
        for ex in self.exs:
            self._refill(ex)

    # -------------------------------------------------------------- read views
    def prices(self, exs) -> dict:
        out = {}
        for ex in exs:
            ex = str(ex)
            if ex not in self.ex_market:
                continue
            b = self.book(ex, 10)
            out[ex] = {"exchangeId": ex, "marketId": self.ex_market[ex], "option": "YES",
                       "latestPrice": self.last_px.get(ex, round(self.mid(ex), 3)), "bestBid": b["bestBid"], "bestAsk": b["bestAsk"],
                       "spread": b["spread"]}
        return out

    def positions_payload(self) -> dict:
        rows, tot_mv, tot_cb = [], 0.0, 0.0
        for ex, p in self.pos.items():
            if p.yes == 0 and p.no == 0:
                continue
            m = self.mid(ex)
            if p.yes:
                q, cb, mv = p.yes, p.yes_cost, p.yes * m
            else:
                q, cb, mv = -p.no, p.no_cost, p.no * (1 - m)
            tot_mv += mv
            tot_cb += cb
            rows.append({"exchangeId": ex, "marketId": self.ex_market[ex], "marketTitle": "", "option": "YES", "settled": False,
                         "quantity": q, "avgCost": cb / abs(q), "currentPrice": round(m, 4), "marketValue": round(mv, 4),
                         "costBasis": round(cb, 4), "unrealizedPnl": round(mv - cb, 4), "unrealizedPnlPct": 0.0, "moneyEarned": 0, "lots": []})
        return {"positions": rows, "summary": {"totalMarketValue": round(tot_mv, 4), "totalCostBasis": round(tot_cb, 4),
                                               "totalUnrealizedPnl": round(tot_mv - tot_cb, 4)}}

    def value(self) -> float:
        v = self.cash
        for ex, p in self.pos.items():
            if p.yes or p.no:
                m = self.mid(ex)
                v += p.yes * m + p.no * (1 - m)
        return v

    def signed_positions(self) -> dict:
        return {ex: float(p.q) for ex, p in self.pos.items() if p.q}

    def open_orders_payload(self) -> list:
        out = []
        for ex, lst in self.ours.items():
            for o in lst:
                if o.open:
                    out.append({"id": o.id, "exchangeId": ex, "side": o.side, "action": "buy", "quantity": o.qty, "priceLimit": o.limit,
                                "open": True, "createdAt": iso(o.t), "expirationDate": iso(o.expiry) if o.expiry else None,
                                "tournamentId": self.tid, "terminalReasonCode": None, "quantityFilled": None})
        return out

    def outside_quotes(self) -> tuple[dict, dict]:
        pmq, kq = {}, {}
        for ex, f in self.outside.items():
            f2 = min(0.99, max(0.01, f + self.kalshi_offset.get(ex, 0.0)))
            pmq["pm" + ex] = {"bid": round(max(0.001, f - 0.005), 4), "ask": round(min(0.999, f + 0.005), 4), "bid_sz": 20000, "ask_sz": 20000,
                              "depth_usd": 40000.0}
            kq["k" + ex] = {"bid": round(max(0.001, f2 - 0.01), 4), "ask": round(min(0.999, f2 + 0.01), 4), "bid_sz": 5000, "ask_sz": 5000,
                            "depth_usd": 20000.0}
        return pmq, kq

    # -------------------------------------------------------------------- feed
    def begin_silence(self, seconds: float):
        self.silent_since, self.silent_until = self.now, self.now + seconds
        self.frozen_books = {ex: self.book_copy(ex, 50, feed_ts=self.now) for ex in self.exs}

    def silent(self) -> bool:
        return self.silent_since is not None and self.silent_since <= self.now < (self.silent_until or 0)


class FakeClient:
    """The SuperMarketClient surface used by the bot. Writes advance the clock by venue.write_latency first."""

    def __init__(self, venue: Venue, label: str):
        self.v, self.label = venue, label
        self.tournament_id: Optional[str] = None
        self.stats = {"reads": 0, "writes": 0, "retries": 0, "errors": 0}

    def _r(self):
        self.stats["reads"] += 1

    def _w(self):
        self.stats["writes"] += 1
        if self.v.write_latency > 0:
            self.v.clock.advance(self.v.write_latency)
            self.v.expire()

    def new_key(self, prefix: str = "sb") -> str:
        return self.v.new_key(prefix)

    # reads
    def account(self, tournament_id=None) -> dict:
        self._r()
        return {"id": "sim-user", "username": "sim", "email": None, "createdAt": iso(0), "avatarUrl": None, "bio": None,
                "balance": round(self.v.cash, 2)}

    def tournaments(self) -> list:
        self._r()
        return [self.tournament("midterm-elections")]

    def tournament(self, slug: str) -> dict:
        self._r()
        return {"id": self.v.tid, "slug": slug, "name": "Midterm Elections (simulated)", "status": "active", "endDate": "2026-11-04T17:00:00Z",
                "initialBalance": 100000, "currencyName": "SUSQies", "myBalance": round(self.v.cash, 2)}

    def markets(self, tournament_id=None, status="any") -> list:
        self._r()
        return [dict(m) for m in self.v.markets]

    def market(self, market_id, tournament_id=None) -> dict:
        self._r()
        return next(m for m in self.v.markets if str(m["id"]) == str(market_id))

    def prices(self, exchange_ids, tournament_id=None) -> dict:
        self._r()
        return self.v.prices(exchange_ids)

    def orderbook(self, exchange_id, depth=50, tournament_id=None) -> dict:
        self._r()
        return self.v.book_copy(str(exchange_id), depth)

    def trades(self, exchange_id, limit=200, cursor=None, since=None, tournament_id=None) -> dict:
        self._r()
        rows = self.v.trades_by_ex.get(str(exchange_id), [])
        if since:
            rows = [t for t in rows if t["createdAt"] > since]
        return {"data": list(reversed(rows[-limit:])), "pagination": {"hasMore": False, "nextCursor": None}}

    def price_history(self, exchange_id, resolution="1h", limit=200, tournament_id=None) -> dict:
        self._r()
        return {"candles": []}

    def positions(self, slug) -> dict:
        self._r()
        return self.v.positions_payload()

    def pnl(self, slug, period="all") -> dict:
        self._r()
        return {}

    def fills(self, slug, limit=200, cursor=None) -> dict:
        self._r()
        start = int(cursor or 0)
        rows = list(reversed(self.v.fills))
        page = rows[start:start + limit]
        more = start + limit < len(rows)
        return {"data": page, "pagination": {"hasMore": more, "nextCursor": str(start + limit) if more else None},
                "coverage": {"complete": True, "projectedThroughSequence": None}}

    def open_orders(self, tournament_id=None, exchange_id=None) -> list:
        self._r()
        rows = self.v.open_orders_payload()
        return [o for o in rows if exchange_id is None or o["exchangeId"] == str(exchange_id)]

    # writes
    def place_order(self, exchange_id, side, action, quantity, price, expiration=None, tournament_id=None, key=None,
                    retries=None) -> dict:
        self._w()
        return self.v.place(str(exchange_id), side, action, quantity, price, expiration, key or self.new_key("ord"))

    def place_batch(self, orders, key=None, max_rounds=6) -> dict:
        # the touch each order was priced against, BEFORE the write latency: lets the checker tell a bot bug (quote
        # already crossing when sent) from the market moving while the order was in flight
        sent = {str(o["exchangeId"]): (P(self.v.bid_t[str(o["exchangeId"])]), P(self.v.ask_t[str(o["exchangeId"])]))
                for o in orders[:50] if str(o["exchangeId"]) in self.v.bid_t}
        n0 = len(self.v.calls)
        self._w()
        key = key or self.new_key("bat")
        res = []
        self._sent_touch = (n0, sent)
        for i, o in enumerate(orders[:50]):
            try:
                d = self.v.place(str(o["exchangeId"]), o["side"], o.get("action", "buy"), o["quantity"], o.get("price"),
                                 o.get("expirationDate"), f"{key}-{i}")
                res.append({"index": i, "ok": True, "status": 201, "data": d})
            except self.v.ApiError as e:
                res.append({"index": i, "ok": False, "status": e.status, "data": {"error": {"code": e.code, "message": e.message}}})
        for c in self.v.calls[n0:]:
            if c.get("kind") == "order" and c.get("ex") in sent:
                c["touch_at_send"] = sent[c["ex"]]
        return {"results": res}

    def place_multi_leg(self, legs, all_or_none=True, key=None, retries=None) -> dict:
        self._w()
        return self.v.multi_leg(legs[:10], all_or_none, key)

    def cancel_order(self, order_id) -> dict:
        self._w()
        return self.v.cancel_order(order_id)

    def cancel_all(self, exchange_id=None, market_id=None, tournament_id=None) -> dict:
        self._w()
        return self.v.cancel_all(exchange_id, market_id)


class FakeFeed:
    """The RealtimeFeed surface. Healthy: books are the venue's live books (received 0.5 s ago). Silent (venue.begin_silence):
    a feed that existed before the silence keeps returning the frozen books and stops sending messages; a feed created
    during the silence holds nothing."""

    def __init__(self, venue: Venue):
        self.v = venue
        self.created = venue.now
        self.stopped = False
        self.trade_idx = len(venue.trades)
        self.reconnects = 0

    def set_universe(self, exchanges_by_market):
        pass

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def _mode(self) -> str:
        if self.stopped:
            return "dead"
        if self.v.silent():
            return "frozen" if self.created < self.v.silent_since else "empty"
        return "live"

    def _last_msg(self) -> Optional[float]:
        m = self._mode()
        if m == "live":
            return self.v.now - 0.5
        if m == "frozen":
            return self.v.silent_since
        return None

    def book(self, exchange_id) -> Optional[dict]:
        m, ex = self._mode(), str(exchange_id)
        if m == "live":
            return self.v.book_copy(ex, 50, feed_ts=self.v.now - 0.5)
        if m == "frozen":
            b = self.v.frozen_books.get(ex)
            return dict(b, bids=[dict(l) for l in b["bids"]], asks=[dict(l) for l in b["asks"]]) if b else None
        return None

    def books(self) -> dict:
        return {ex: b for ex in self.v.exs if (b := self.book(ex)) is not None}

    def bests(self) -> dict:
        m = self._mode()
        if m == "live":
            out = {}
            for ex in self.v.exs:
                b = self.v.book(ex, 10)
                out[ex] = (b["bestBid"], b["bestAsk"], self.v.now - 0.5)
            return out
        if m == "frozen":
            return {ex: (b["bestBid"], b["bestAsk"], b["feed_ts"]) for ex, b in self.v.frozen_books.items()}
        return {}

    def best(self, exchange_id):
        b = self.book(exchange_id)
        return (b["bestBid"], b["bestAsk"]) if b else None

    def drain_trades(self) -> list:
        if self._mode() != "live":
            self.trade_idx = len(self.v.trades)
            return []
        out = self.v.trades[self.trade_idx:]
        self.trade_idx = len(self.v.trades)
        return [dict(t, feed_ts=self.v.now) for t in out]

    def drain_fills(self) -> list:
        return []

    def needs_resync(self) -> set:
        return set()

    def mark_resynced(self, exchange_ids):
        pass

    def wait_update(self, timeout: float) -> bool:
        return True

    def stats(self) -> dict:
        lm = self._last_msg()
        age = None if lm is None else self.v.now - lm
        mode = self._mode()
        return {"connected": mode != "dead", "connected_since": self.created, "reconnects": self.reconnects, "batches": 0, "trades": 0,
                "fills": 0, "books_held": 0 if mode in ("empty", "dead") else len(self.v.exs), "resync_pending": 0,
                "last_message_age_sec": age, "last_batch_age_sec": age, "join_status": {"tournament": "joined"},
                "token_expires_in_sec": 3600, "token_refreshes": 0, "events": {}}

    def healthy(self, max_silence_sec: float = 120) -> bool:
        s = self.stats()
        return bool(s["connected"] and s["last_message_age_sec"] is not None and s["last_message_age_sec"] < max_silence_sec)
