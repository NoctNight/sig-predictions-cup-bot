"""Per-market tracker: volatility, flow, book shape, our fill quality and an expected-MM-edge score per exchange.

Nothing here talks to the venue. The bot pushes what it already holds each cycle::

    tracker = Tracker(universe)                      # or Tracker.from_store(db_path, hours=24) to warm up
    tracker.update(feed.drain_trades(), feed.books(), now, fills=new_fill_rows, quoted=mm.quoted)
    tracker.score(ex) / tracker.rank(12) / tracker.snapshot()

and ``params/market_tracker.json`` is rewritten every 5 minutes from inside ``update``. Offline the same object is
rebuilt from the SQLite log (``quotes`` for the ~32 quoted markets' mids, ``tape`` for every market's prints,
``fills`` / ``orders`` for our own MM activity) and reported with ``python3 -m sigbot.tracker --report``.

Definitions (tick = 0.005; all vols in ticks)
--------------------------------------------
* mid series: book mid when a book / quote snapshot exists for the exchange, else the last trade price (``vol_source``
  says which). The series is sampled on a 5-minute calendar grid, forward-filled (an interval without a print is a
  zero change, as in ``hfstats``). vol_5m = sqrt(EW mean of squared 5-minute changes) (non-overlapping);
  vol_1h = sqrt(EW mean of squared 1-hour changes) sampled every 5 minutes (overlapping). EW half-life 2 h.
  nvol_* = the same on changes divided by sqrt(p(1-p)) of the starting mid, so 0.5 and 0.9 markets compare.
  ``*_win`` are the plain (unweighted) versions over the whole observation window, used by the research tables.
* trades_per_h / volume_per_h: EW (half-life 2 h) rates, normalised by the kernel mass actually observed so the first
  hours are not under-counted; avg_trade_size = volume / count. ``*_win`` = plain window averages.
* spread_ticks: latest quoted spread; spread_ticks_ew: EW mean over snapshots. bid_qty / ask_qty: resting size at
  the best prices. dmm_touch: the size of a resting order of 1,800-2,700 seen at the best bid or ask (the designated
  maker's 2,000-2,500 clip), with dmm_frac = share of snapshots where one was present.
* sweeps: moves of >= 2 ticks within 60 s on the mid series (one count per burst), as an EW rate per hour and a total.
* our MM fills: fills_per_h = MM fills / hours quoted (orders' resting intervals, union); markouts = (mid at t+h minus
  fill price) signed in our favour, in ticks (same convention as analytics.py); half_spread_ticks = mean distance of the
  fill price from the mid at placement; adverse_30m = half_spread - markout_30m (how far the mid moved against us).
* score = fills_per_h x (half_spread_ticks - adverse_30m) = fills_per_h x markout_30m, in tick-fills per hour.
  With n own fills the own estimate has weight n/(n+10) against a prior: fill rate = k x venue trades_per_h (k pooled
  from the exchanges with >= 10 fills, default 0.05) and edge = spread/2 - pooled adverse markout (default 0.5 x vol_5m).
  ``edge_susq_per_h`` multiplies by the mean fill size and the tick value.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

try:
    from .config import TICK
except Exception:  # pragma: no cover - the module must work standalone
    TICK = 0.005
try:
    from .universe import Universe
except Exception:  # pragma: no cover
    Universe = None  # type: ignore

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(HERE, "data", "sigbot.sqlite")
DEFAULT_OUT = os.path.join(HERE, "params", "market_tracker.json")
DEFAULT_MARKETS = os.path.join(HERE, "data", "markets.json")

HALF_LIFE_S = 2 * 3600.0
GRID_S = 300.0
VOL_HORIZONS = {"5m": 300.0, "1h": 3600.0}
MARKOUT_HORIZONS = {"5m": 300.0, "30m": 1800.0}
RETAIN_S = 2 * 3600.0 + 600.0
SWEEP_WINDOW_S, SWEEP_TICKS = 60.0, 2
DMM_RANGE = (1800.0, 2700.0)
N0_FILLS = 10.0
WRITE_EVERY_S = 300.0
K_FILL_DEFAULT = 0.05
MAX_FILLS_KEPT = 500

_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def iso_ts(s: Any) -> Optional[float]:
    """ISO-8601 (any fractional precision, Z or offset) -> unix seconds; a number passes through."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        s = _FRAC_RE.sub(r"\1", str(s).replace("Z", "+00:00"))
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def _get(d: Any, *keys, default=None):
    for k in keys:
        try:
            v = d[k]
        except (KeyError, IndexError, TypeError):
            continue
        if v is not None:
            return v
    return default


def _r(x, nd=3):
    return None if x is None else round(float(x), nd)


# ----------------------------------------------------------------------------------------------- EW primitives
class EWRate:
    """Exponentially weighted event rate; rate = lambda x sum(w) / observed kernel mass, so it is unbiased early on."""

    def __init__(self, half_life: float = HALF_LIFE_S):
        self.lam = math.log(2) / half_life
        self.s, self.t, self.t0, self.n, self.total = 0.0, None, None, 0, 0.0

    def observe_from(self, t0: float):
        if self.t0 is None or t0 < self.t0:
            self.t0 = t0

    def _decay(self, ts: float):
        if self.t is None:
            self.t = ts
        elif ts > self.t:
            self.s *= math.exp(-self.lam * (ts - self.t))
            self.t = ts

    def add(self, ts: float, x: float = 1.0):
        if self.t0 is None:
            self.t0 = ts
        self._decay(max(ts, self.t or ts))
        self.s += x
        self.n += 1
        self.total += x

    def per_hour(self, now: float) -> Optional[float]:
        if self.t0 is None:
            return None
        self._decay(now)
        span = max(now - self.t0, 1.0)
        mass = (1.0 - math.exp(-self.lam * span)) / self.lam   # seconds of kernel mass actually observed
        return self.s / mass * 3600.0

    def window_per_hour(self, now: float) -> Optional[float]:
        if self.t0 is None:
            return None
        return self.total / max(now - self.t0, 1.0) * 3600.0


class EWMean:
    def __init__(self, half_life: float = HALF_LIFE_S):
        self.lam = math.log(2) / half_life
        self.s = self.w = 0.0
        self.t = None
        self.sum = 0.0
        self.n = 0

    def add(self, ts: float, x: float):
        if self.t is not None and ts > self.t:
            d = math.exp(-self.lam * (ts - self.t))
            self.s *= d
            self.w *= d
        self.t = max(ts, self.t or ts)
        self.s += x
        self.w += 1.0
        self.sum += x
        self.n += 1

    def value(self) -> Optional[float]:
        return self.s / self.w if self.w > 0 else None

    def window_value(self) -> Optional[float]:
        return self.sum / self.n if self.n else None


class PriceSeries:
    """Forward-filled price path with ~2 h retention."""

    def __init__(self):
        self.ts: list[float] = []
        self.px: list[float] = []

    def add(self, ts: float, px: float):
        if self.ts and ts < self.ts[-1]:
            i = bisect.bisect_right(self.ts, ts)
            self.ts.insert(i, ts)
            self.px.insert(i, px)
        else:
            self.ts.append(ts)
            self.px.append(px)
        if len(self.ts) > 64 and self.ts[-1] - self.ts[0] > RETAIN_S:
            k = bisect.bisect_left(self.ts, self.ts[-1] - RETAIN_S)
            if k > 1:
                del self.ts[: k - 1]
                del self.px[: k - 1]

    def at(self, ts: float) -> Optional[float]:
        i = bisect.bisect_right(self.ts, ts)
        return self.px[i - 1] if i > 0 else None

    def at_or_after(self, ts: float, tolerance: float = 600.0) -> Optional[float]:
        i = bisect.bisect_left(self.ts, ts)
        if i < len(self.ts) and self.ts[i] - ts <= tolerance:
            return self.px[i]
        return self.at(ts)

    def last(self) -> Optional[float]:
        return self.px[-1] if self.px else None


class VolTracker:
    """Realised vol of one price series on the 5-minute grid plus a sweep counter."""

    def __init__(self, half_life: float = HALF_LIFE_S):
        self.series = PriceSeries()
        self.var = {h: EWMean(half_life) for h in VOL_HORIZONS}      # squared changes, ticks^2
        self.nvar = {h: EWMean(half_life) for h in VOL_HORIZONS}     # squared normalised changes
        self.last_sample = None
        self.sweeps = EWRate(half_life)
        self._win: list[tuple[float, float]] = []                   # (ts, px) in the last 60 s

    def add(self, ts: float, px: float):
        self.series.add(ts, px)
        self._win = [(t, p) for (t, p) in self._win if ts - t <= SWEEP_WINDOW_S]
        if any(abs(px - p) >= SWEEP_TICKS * TICK - 1e-9 for _, p in self._win):
            self.sweeps.add(ts)
            self._win = []
        self._win.append((ts, px))

    def sample(self, now: float):
        if self.last_sample is not None and now - self.last_sample < GRID_S - 1e-6:
            return
        px = self.series.at(now)
        if px is None:
            return
        self.last_sample = now
        for h, secs in VOL_HORIZONS.items():
            p0 = self.series.at(now - secs)
            if p0 is None:
                continue
            r = (px - p0) / TICK
            p = min(0.99, max(0.01, p0))
            self.var[h].add(now, r * r)
            self.nvar[h].add(now, r * r / (p * (1.0 - p)))

    @staticmethod
    def _sq(m: EWMean, window: bool) -> Optional[float]:
        v = m.window_value() if window else m.value()
        return None if v is None else math.sqrt(max(v, 0.0))

    def vol(self, h: str, window: bool = False) -> Optional[float]:
        return self._sq(self.var[h], window)

    def nvol(self, h: str, window: bool = False) -> Optional[float]:
        return self._sq(self.nvar[h], window)

    def n(self, h: str = "5m") -> int:
        return self.var[h].n


# ------------------------------------------------------------------------------------------------- per market
class MarketStats:
    def __init__(self, ex: str, race: Optional[str], party: Optional[str], half_life: float = HALF_LIFE_S):
        self.ex, self.race, self.party = ex, race, party
        self.mid_vol = VolTracker(half_life)
        self.trade_vol = VolTracker(half_life)
        self.trades = EWRate(half_life)
        self.volume = EWRate(half_life)
        self.spread = EWMean(half_life)
        self.n_books = self.n_dmm = 0
        self.dmm_sizes: list[float] = []
        self.last_book: dict = {}
        self.mid = None
        self.fills: list[dict] = []          # resolved + pending MM fills (bounded)
        self.pending: list[dict] = []
        self.mm_fills = EWRate(half_life)
        self.quoted_s = 0.0
        self.first_seen = None

    # ---- inputs
    def on_trade(self, ts: float, price: float, qty: float):
        self.trades.add(ts, 1.0)
        self.volume.add(ts, abs(qty))
        self.trade_vol.add(ts, price)

    def on_book(self, ts: float, bb, ba, bq, aq, depth: Optional[int] = None):
        if bb is None or ba is None or ba < bb:
            return
        mid = (bb + ba) / 2.0
        self.mid = mid
        self.mid_vol.add(ts, mid)
        self.spread.add(ts, (ba - bb) / TICK)
        self.n_books += 1
        sizes = [q for q in (bq, aq) if q is not None and DMM_RANGE[0] <= q <= DMM_RANGE[1]]
        if sizes:
            self.n_dmm += 1
            self.dmm_sizes.append(sizes[0])
            del self.dmm_sizes[:-200]
        self.last_book = {"ts": ts, "bb": bb, "ba": ba, "bq": bq, "aq": aq, "depth": depth}

    def on_fill(self, ts: float, side: str, price: float, qty: float, ticks_from_mid, level, vol5, vol1h):
        self.mm_fills.add(ts, 1.0)
        mid = self.series.at(ts)
        f = {"ts": ts, "side": side, "price": price, "qty": abs(qty), "level": level,
             "ticks_from_mid": ticks_from_mid if ticks_from_mid is not None else (None if mid is None else
                               ((mid - price) if side == "yes" else (price - mid)) / TICK),
             "vol5_at": vol5, "vol1h_at": vol1h, "m5": None, "m30": None}
        self.fills.append(f)
        self.pending.append(f)
        del self.fills[:-MAX_FILLS_KEPT]

    def resolve(self, now: float):
        keep = []
        for f in self.pending:
            done = True
            for key, (h, secs) in (("m5", ("5m", 300.0)), ("m30", ("30m", 1800.0))):
                if f[key] is not None:
                    continue
                if now < f["ts"] + secs:
                    done = False
                    continue
                m = self.series.at_or_after(f["ts"] + secs)
                if m is None:
                    done = False
                    continue
                f[key] = ((m - f["price"]) if f["side"] == "yes" else (f["price"] - m)) / TICK
            if not done:
                keep.append(f)
        self.pending = keep

    # ---- views
    @property
    def series(self) -> PriceSeries:
        return self.mid_vol.series if self.mid_vol.series.ts else self.trade_vol.series

    @property
    def vol_source(self) -> str:
        return "book" if self.mid_vol.series.ts else ("tape" if self.trade_vol.series.ts else "none")

    @property
    def vt(self) -> VolTracker:
        return self.mid_vol if self.mid_vol.series.ts else self.trade_vol

    def sample(self, now: float):
        self.mid_vol.sample(now)
        self.trade_vol.sample(now)
        self.resolve(now)

    def markout(self, key: str) -> tuple[Optional[float], int]:
        vals = [f[key] for f in self.fills if f[key] is not None]
        return (sum(vals) / len(vals) if vals else None), len(vals)

    def half_spread(self) -> Optional[float]:
        vals = [f["ticks_from_mid"] for f in self.fills if f["ticks_from_mid"] is not None]
        return sum(vals) / len(vals) if vals else None

    def avg_fill_qty(self) -> Optional[float]:
        return sum(f["qty"] for f in self.fills) / len(self.fills) if self.fills else None

    def fills_per_h(self) -> Optional[float]:
        if self.quoted_s < 900:
            return None
        return len(self.fills) / (self.quoted_s / 3600.0)


# ----------------------------------------------------------------------------------------------------- tracker
class Tracker:
    def __init__(self, universe=None, now: Optional[float] = None, out_path: str = DEFAULT_OUT,
                 write_every: float = WRITE_EVERY_S, half_life: float = HALF_LIFE_S, window_h: Optional[float] = None):
        self.universe = universe
        self.half_life = half_life
        self.out_path = out_path
        self.write_every = write_every
        self.markets: dict[str, MarketStats] = {}
        self.now = now or time.time()
        self.t0 = self.now
        self.window_h = window_h
        self._last_write = 0.0
        self._last_update = None
        self._last_grid = None

    # ---- bookkeeping
    def _m(self, ex) -> MarketStats:
        ex = str(ex)
        m = self.markets.get(ex)
        if m is None:
            race = party = None
            if self.universe is not None:
                rp = getattr(self.universe, "by_exchange", {}).get(ex)
                if rp:
                    race, party = rp
            m = self.markets[ex] = MarketStats(ex, race, party, self.half_life)
            m.first_seen = self.now
            for r in (m.trades, m.volume, m.mm_fills, m.mid_vol.sweeps, m.trade_vol.sweeps):
                r.observe_from(self.t0)
        return m

    def _advance(self, now: float):
        """Move the clock, sampling the vol grid for every market at each 5-minute boundary crossed."""
        if self._last_grid is None:
            self._last_grid = math.floor(now / GRID_S) * GRID_S
        while self._last_grid + GRID_S <= now:
            self._last_grid += GRID_S
            for m in self.markets.values():
                m.sample(self._last_grid)
        self.now = max(self.now, now)

    # ---- inputs
    def on_trade(self, ex, ts: float, price: float, qty: float):
        if ts is None or price is None or qty is None:
            return
        self._m(ex).on_trade(ts, float(price), float(qty))

    def on_book(self, ex, ts: float, book: Any):
        """`book` is the feed / REST shape ({bids, asks} of {price, quantity}) or a quotes row (best_bid, best_ask, bid_qty, ask_qty)."""
        bids, asks = _get(book, "bids"), _get(book, "asks")
        if bids is not None or asks is not None:
            bb = bids[0]["price"] if bids else None
            ba = asks[0]["price"] if asks else None
            bq = bids[0].get("quantity", bids[0].get("size")) if bids else None
            aq = asks[0].get("quantity", asks[0].get("size")) if asks else None
            depth = max(len(bids or []), len(asks or []))
        else:
            bb, ba = _get(book, "best_bid", "bestBid"), _get(book, "best_ask", "bestAsk")
            bq, aq = _get(book, "bid_qty", "bidQty"), _get(book, "ask_qty", "askQty")
            depth = _get(book, "depth")
        self._m(ex).on_book(ts, bb, ba, bq, aq, depth)

    def on_fill(self, f: Any):
        purpose = _get(f, "purpose")
        if purpose not in (None, "mm"):
            return
        ex = _get(f, "exchange_id", "exchangeId")
        ts = iso_ts(_get(f, "filled_at", "filledAt", "executedAt"))
        price, qty = _get(f, "price"), _get(f, "quantity")
        if ex is None or ts is None or price is None or qty is None:
            return
        side = _get(f, "side") or ("no" if float(qty) < 0 else "yes")
        m = self._m(ex)
        vt = m.vt
        m.on_fill(ts, side, float(price), float(qty), _get(f, "ticks_from_mid"), _get(f, "level"),
                  vt.vol("5m"), vt.vol("1h"))

    def update(self, trades: Iterable[Any] = (), books: Optional[dict] = None, now: Optional[float] = None,
               fills: Iterable[Any] = (), quoted: Optional[Iterable] = None, write: bool = True):
        """One bot cycle: public prints since the last call, the held books, our new MM fills, the quoted set."""
        now = now or time.time()
        for tr in trades or ():
            self.on_trade(_get(tr, "exchangeId", "exchange_id"), iso_ts(_get(tr, "executedAt", "executed_at", "createdAt")),
                          _get(tr, "price"), _get(tr, "quantity", "size"))
        for ex, b in (books or {}).items():
            if b:
                self.on_book(ex, iso_ts(_get(b, "feed_ts", "ts")) or now, b)
        for f in fills or ():
            self.on_fill(f)
        if quoted is not None and self._last_update is not None:
            dt = max(0.0, min(now - self._last_update, 600.0))
            for ex in quoted:
                self._m(ex).quoted_s += dt
        self._last_update = now
        self._advance(now)
        for m in self.markets.values():
            m.resolve(now)
        if write and self.out_path and now - self._last_write >= self.write_every:
            try:
                self.write()
            finally:
                self._last_write = now

    # ---- pooled priors
    def priors(self) -> dict:
        own = [m for m in self.markets.values() if len(m.fills) >= N0_FILLS and m.fills_per_h() is not None]
        fr = sum(m.fills_per_h() for m in own)
        tr = sum((m.trades.per_hour(self.now) or 0.0) for m in own)
        k_fill = fr / tr if own and tr > 0 else K_FILL_DEFAULT
        adv = [f["ticks_from_mid"] - f["m30"] for m in self.markets.values() for f in m.fills
               if f["m30"] is not None and f["ticks_from_mid"] is not None]
        adverse = sum(adv) / len(adv) if len(adv) >= N0_FILLS else None
        return {"k_fill": k_fill, "adverse_30m": adverse, "n_adverse": len(adv), "n_calibrated": len(own)}

    # ---- outputs
    def row(self, ex, pri: Optional[dict] = None) -> dict:
        m = self._m(ex)
        pri = pri or self.priors()
        now = self.now
        vt = m.vt
        m5, n5 = m.markout("m5")
        m30, n30 = m.markout("m30")
        hs = m.half_spread()
        spread = ((m.last_book["ba"] - m.last_book["bb"]) / TICK) if m.last_book else None
        own_rate = m.fills_per_h()
        n = n30 if n30 else 0
        w = n / (n + N0_FILLS)
        trades_h = m.trades.per_hour(now)
        prior_rate = pri["k_fill"] * trades_h if trades_h is not None else None
        v5 = vt.vol("5m")
        adverse_prior = pri["adverse_30m"] if pri["adverse_30m"] is not None else (0.5 * v5 if v5 is not None else 1.0)
        half_prior = max(0.5, spread / 2.0) if spread is not None else (hs if hs is not None else 0.5)  # no book: assume a 1-tick touch
        prior_edge = half_prior - adverse_prior
        rate = ((w * own_rate + (1 - w) * prior_rate) if own_rate is not None and prior_rate is not None
                else (own_rate if own_rate is not None and w >= 0.5 else prior_rate))
        edge = (w * m30 + (1 - w) * prior_edge) if m30 is not None else prior_edge
        score = None if rate is None else rate * edge
        qty = m.avg_fill_qty() or 100.0
        r = {
            "ex": m.ex, "race": m.race, "party": m.party, "mid": _r(m.mid if m.mid is not None else m.series.last(), 4),
            "vol_source": m.vol_source,
            "vol5": _r(vt.vol("5m")), "vol1h": _r(vt.vol("1h")), "nvol5": _r(vt.nvol("5m")), "nvol1h": _r(vt.nvol("1h")),
            "vol5_win": _r(vt.vol("5m", True)), "vol1h_win": _r(vt.vol("1h", True)),
            "nvol5_win": _r(vt.nvol("5m", True)), "nvol1h_win": _r(vt.nvol("1h", True)), "n_vol_samples": vt.n(),
            "tape_vol1h": _r(m.trade_vol.vol("1h")), "tape_vol1h_win": _r(m.trade_vol.vol("1h", True)),
            "tape_nvol1h_win": _r(m.trade_vol.nvol("1h", True)),
            "trades_per_h": _r(trades_h, 2), "volume_per_h": _r(m.volume.per_hour(now), 1),
            "trades_per_h_win": _r(m.trades.window_per_hour(now), 2), "volume_per_h_win": _r(m.volume.window_per_hour(now), 1),
            "n_trades": m.trades.n, "avg_trade_size": _r(m.volume.total / m.trades.n, 1) if m.trades.n else None,
            "spread_ticks": _r(spread, 1), "spread_ticks_ew": _r(m.spread.value(), 2),
            "bid_qty": m.last_book.get("bq") if m.last_book else None, "ask_qty": m.last_book.get("aq") if m.last_book else None,
            "depth_levels": m.last_book.get("depth") if m.last_book else None, "n_books": m.n_books,
            "dmm_touch": _r(sorted(m.dmm_sizes)[len(m.dmm_sizes) // 2], 0) if m.dmm_sizes else None,
            "dmm_frac": _r(m.n_dmm / m.n_books, 2) if m.n_books else None,
            "sweeps_per_h": _r(vt.sweeps.per_hour(now), 2), "sweeps": vt.sweeps.n,
            "mm_fills": len(m.fills), "fills_per_h": _r(own_rate, 2), "quoted_h": _r(m.quoted_s / 3600.0, 2),
            "half_spread_ticks": _r(hs, 2),
            "markout_5m": {"mean": _r(m5), "n": n5}, "markout_30m": {"mean": _r(m30), "n": n30},
            "adverse_30m": _r(hs - m30, 3) if hs is not None and m30 is not None else None,
            "edge_ticks": _r(edge), "own_weight": _r(w, 2), "prior_rate": _r(prior_rate, 2), "prior_edge": _r(prior_edge),
            "score": _r(score, 3), "edge_susq_per_h": _r(score * qty * TICK, 2) if score is not None else None,
        }
        return r

    def score(self, ex) -> Optional[float]:
        if str(ex) not in self.markets:
            return None
        return self.row(ex)["score"]

    def rank(self, n: int = 10, pri: Optional[dict] = None, price_band: Optional[tuple] = None) -> list[tuple[str, float]]:
        """Top n (exchange_id, score); `price_band=(lo, hi)` keeps only markets whose mid is inside it (the MM's quoting band)."""
        pri = pri or self.priors()
        scored = []
        for ex in self.markets:
            r = self.row(ex, pri)
            if price_band and (r["mid"] is None or not (price_band[0] <= r["mid"] <= price_band[1])):
                continue
            scored.append((ex, r["score"]))
        scored = [(ex, s) for ex, s in scored if s is not None]
        scored.sort(key=lambda t: -t[1])
        return scored[:n]

    def races(self, rows: Optional[dict] = None) -> dict:
        rows = rows or {ex: self.row(ex) for ex in self.markets}
        out: dict = {}
        by_race: dict = defaultdict(list)
        for r in rows.values():
            by_race[r["race"] or f"ex:{r['ex']}"].append(r)
        for race, rs in by_race.items():
            def agg(key, f=sum):
                vals = [r[key] for r in rs if r.get(key) is not None]
                return _r(f(vals), 3) if vals else None
            def mean(key):
                vals = [r[key] for r in rs if r.get(key) is not None]
                return _r(sum(vals) / len(vals), 3) if vals else None
            n30 = sum(r["markout_30m"]["n"] for r in rs)
            m30 = (sum(r["markout_30m"]["mean"] * r["markout_30m"]["n"] for r in rs if r["markout_30m"]["mean"] is not None) / n30) if n30 else None
            out[race] = {"exchanges": [r["ex"] for r in rs], "vol1h_mean": mean("vol1h"), "nvol1h_mean": mean("nvol1h"),
                         "trades_per_h": agg("trades_per_h"), "volume_per_h": agg("volume_per_h"),
                         "spread_ticks_mean": mean("spread_ticks"), "sweeps_per_h": agg("sweeps_per_h"),
                         "mm_fills": agg("mm_fills"), "markout_30m": {"mean": _r(m30), "n": n30},
                         "score": agg("score"), "edge_susq_per_h": agg("edge_susq_per_h")}
        return out

    def snapshot(self) -> dict:
        pri = self.priors()
        rows = {ex: self.row(ex, pri) for ex in self.markets}
        return {"generated_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(timespec="seconds"),
                "window_h": _r((self.now - self.t0) / 3600.0, 2), "half_life_h": self.half_life / 3600.0,
                "n_exchanges": len(rows), "priors": {k: _r(v, 4) if isinstance(v, float) else v for k, v in pri.items()},
                "rank": self.rank(25, pri), "exchanges": rows, "races": self.races(rows)}

    def write(self, path: Optional[str] = None) -> str:
        path = path or self.out_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.snapshot(), fh, indent=1)
        os.replace(tmp, path)
        return path

    # ---- offline rebuild
    @classmethod
    def from_store(cls, db_path: str = DEFAULT_DB, hours: float = 24.0, now: Optional[float] = None, universe=None,
                   out_path: str = DEFAULT_OUT, markets_path: str = DEFAULT_MARKETS) -> "Tracker":
        """Replay quotes (book mids of the quoted markets), tape (every market's prints) and our MM fills/orders."""
        now = now or time.time()
        since = now - hours * 3600.0
        if universe is None and Universe is not None and os.path.exists(markets_path):
            try:
                universe = Universe.from_markets(json.load(open(markets_path)))
            except Exception:
                universe = None
        tr = cls(universe, now=since, out_path=out_path, window_h=hours)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            events: list[tuple] = []
            for q in conn.execute("SELECT ts, exchange_id, best_bid, best_ask, bid_qty, ask_qty FROM quotes WHERE ts>=?", (since,)):
                events.append((q["ts"], 0, "q", q["exchange_id"], {"best_bid": q["best_bid"], "best_ask": q["best_ask"],
                                                                     "bid_qty": q["bid_qty"], "ask_qty": q["ask_qty"], "depth": 1}))
            since_iso = datetime.fromtimestamp(since, timezone.utc).isoformat()
            for t in conn.execute("SELECT exchange_id, price, quantity, executed_at FROM tape WHERE executed_at>=?", (since_iso,)):
                ts = iso_ts(t["executed_at"])
                if ts is not None:
                    events.append((ts, 1, "t", t["exchange_id"], (t["price"], t["quantity"])))
            for f in conn.execute("SELECT exchange_id, side, price, quantity, filled_at, purpose, level, ticks_from_mid FROM fills "
                                  "WHERE purpose='mm' AND filled_at>=?", (since_iso,)):
                ts = iso_ts(f["filled_at"])
                if ts is not None:
                    events.append((ts, 2, "f", f["exchange_id"], dict(f)))
            events.sort(key=lambda e: (e[0], e[1]))
            for ts, _, kind, ex, payload in events:
                tr._advance(ts)
                if kind == "q":
                    tr.on_book(ex, ts, payload)
                elif kind == "t":
                    tr.on_trade(ex, ts, payload[0], payload[1])
                else:
                    tr.on_fill(payload)
            tr._advance(now)
            # quoted time: union of our resting MM order intervals per exchange
            ivals: dict = defaultdict(list)
            for o in conn.execute("SELECT exchange_id, placed_at, expires_at FROM orders WHERE purpose='mm' AND placed_at>=?", (since,)):
                end = min(now, iso_ts(o["expires_at"]) or now)
                if end > o["placed_at"]:
                    ivals[o["exchange_id"]].append((o["placed_at"], end))
            for ex, iv in ivals.items():
                iv.sort()
                total, cur_s, cur_e = 0.0, None, None
                for s, e in iv:
                    if cur_e is None or s > cur_e:
                        if cur_e is not None:
                            total += cur_e - cur_s
                        cur_s, cur_e = s, e
                    else:
                        cur_e = max(cur_e, e)
                if cur_e is not None:
                    total += cur_e - cur_s
                tr._m(ex).quoted_s = total
        finally:
            conn.close()
        for m in tr.markets.values():
            m.resolve(now)
        tr._last_update = now
        return tr


# ---------------------------------------------------------------------------------------------------- research
def _spearman(xs: list[float], ys: list[float]) -> tuple[Optional[float], int]:
    n = len(xs)
    if n < 3:
        return None, n

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0 + 1
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    return (sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else None), n


def _fmt_corr(xs, ys) -> str:
    rho, n = _spearman(xs, ys)
    if rho is None:
        return f"n={n}: n/a"
    z = rho * math.sqrt(max(n - 1, 1))
    return f"rho={rho:+.2f} (n={n}, z={z:+.1f})"


def _terciles(pairs: list[tuple[float, dict]]) -> list[list[dict]]:
    pairs = sorted(pairs, key=lambda p: p[0])
    k = len(pairs)
    cuts = [0, k // 3, 2 * k // 3, k]
    return [[p[1] for p in pairs[cuts[i]:cuts[i + 1]]] for i in range(3)]


def research(tr: Tracker) -> str:
    rows = list(tr.snapshot()["exchanges"].values())
    out = []
    tape = [r for r in rows if r["n_trades"] >= 3 and r["tape_nvol1h_win"] is not None]
    quoted = [r for r in rows if r["vol_source"] == "book" and r["quoted_h"] and r["quoted_h"] >= 1.0 and r["nvol1h_win"] is not None]
    filled = [r for r in quoted if r["markout_30m"]["n"] >= 3]
    fills = [(f, m) for m in tr.markets.values() for f in m.fills if f["m30"] is not None and f["vol5_at"] is not None]
    out.append(f"window {tr.window_h} h ending {datetime.fromtimestamp(tr.now, timezone.utc):%Y-%m-%d %H:%M} UTC; "
               f"{len(rows)} exchanges seen, {len(tape)} with >=3 prints (tape sample), {len(quoted)} quoted >=1 h (quote sample), "
               f"{len(filled)} with >=3 resolved MM fills, {len(fills)} resolved MM fills.")
    out.append("")
    out.append("| test | sample | x | y | Spearman |")
    out.append("|---|---|---|---|---|")
    out.append(f"| (a) vol -> venue volume | tape, n={len(tape)} | tape nvol 1h (window) | volume/h | "
               f"{_fmt_corr([r['tape_nvol1h_win'] for r in tape], [r['volume_per_h_win'] for r in tape])} |")
    out.append(f"| (a) vol -> venue trade count | tape, n={len(tape)} | tape nvol 1h (window) | trades/h | "
               f"{_fmt_corr([r['tape_nvol1h_win'] for r in tape], [r['trades_per_h_win'] for r in tape])} |")
    out.append(f"| (a) vol -> venue volume | quoted, n={len(quoted)} | mid nvol 1h (window) | volume/h | "
               f"{_fmt_corr([r['nvol1h_win'] for r in quoted], [r['volume_per_h_win'] for r in quoted])} |")
    out.append(f"| (b) vol -> our MM fills | quoted, n={len(quoted)} | mid nvol 1h (window) | MM fills / quoted h | "
               f"{_fmt_corr([r['nvol1h_win'] for r in quoted], [(r['fills_per_h'] or 0.0) for r in quoted])} |")
    out.append(f"| (b) vol -> our MM fills | quoted, n={len(quoted)} | mid vol 5m (window, ticks) | MM fills / quoted h | "
               f"{_fmt_corr([r['vol5_win'] for r in quoted], [(r['fills_per_h'] or 0.0) for r in quoted])} |")
    out.append(f"| (c) vol -> our 30m markout | exchanges with >=3 fills, n={len(filled)} | mid nvol 1h (window) | mean markout 30m | "
               f"{_fmt_corr([r['nvol1h_win'] for r in filled], [r['markout_30m']['mean'] for r in filled])} |")
    out.append(f"| (c) vol -> our 30m markout | fills, n={len(fills)} | EW vol 5m at fill time | fill markout 30m | "
               f"{_fmt_corr([f['vol5_at'] for f, _ in fills], [f['m30'] for f, _ in fills])} |")
    f1h = [(f, m) for f, m in fills if f["vol1h_at"] is not None]
    out.append(f"| (c) vol -> our 30m markout | fills, n={len(f1h)} | EW vol 1h at fill time | fill markout 30m | "
               f"{_fmt_corr([f['vol1h_at'] for f, _ in f1h], [f['m30'] for f, _ in f1h])} |")
    f5 = [(f, m) for f, m in fills if f["m5"] is not None]
    out.append(f"| (c) vol -> our 5m markout | fills, n={len(f5)} | EW vol 5m at fill time | fill markout 5m | "
               f"{_fmt_corr([f['vol5_at'] for f, _ in f5], [f['m5'] for f, _ in f5])} |")
    out.append("")

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return (sum(vals) / len(vals)) if vals else None

    def fm(v, nd=2):
        return "-" if v is None else f"{v:.{nd}f}"
    out.append("Terciles by tape nvol 1h (tape sample):")
    out.append("")
    out.append("| tercile | n | nvol 1h | volume/h | trades/h | avg size | spread (quoted only) |")
    out.append("|---|---|---|---|---|---|---|")
    for name, grp in zip(("low", "mid", "high"), _terciles([(r["tape_nvol1h_win"], r) for r in tape])):
        out.append(f"| {name} | {len(grp)} | {fm(mean(r['tape_nvol1h_win'] for r in grp))} | {fm(mean(r['volume_per_h_win'] for r in grp), 0)} | "
                   f"{fm(mean(r['trades_per_h_win'] for r in grp), 1)} | {fm(mean(r['avg_trade_size'] for r in grp), 0)} | "
                   f"{fm(mean(r['spread_ticks_ew'] for r in grp), 1)} |")
    out.append("")
    out.append("Terciles by mid nvol 1h (quote sample):")
    out.append("")
    out.append("| tercile | n | nvol 1h | vol 5m ticks | volume/h | spread ticks | sweeps/h | MM fills | fills/quoted h | markout 5m (n) | markout 30m (n) | half-spread |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, grp in zip(("low", "mid", "high"), _terciles([(r["nvol1h_win"], r) for r in quoted])):
        n5 = sum(r["markout_5m"]["n"] for r in grp)
        n30 = sum(r["markout_30m"]["n"] for r in grp)
        m5 = (sum(r["markout_5m"]["mean"] * r["markout_5m"]["n"] for r in grp if r["markout_5m"]["mean"] is not None) / n5) if n5 else None
        m30 = (sum(r["markout_30m"]["mean"] * r["markout_30m"]["n"] for r in grp if r["markout_30m"]["mean"] is not None) / n30) if n30 else None
        out.append(f"| {name} | {len(grp)} | {fm(mean(r['nvol1h_win'] for r in grp))} | {fm(mean(r['vol5_win'] for r in grp))} | "
                   f"{fm(mean(r['volume_per_h_win'] for r in grp), 0)} | {fm(mean(r['spread_ticks_ew'] for r in grp), 1)} | "
                   f"{fm(mean(r['sweeps_per_h'] for r in grp))} | {sum(r['mm_fills'] for r in grp)} | "
                   f"{fm(mean((r['fills_per_h'] or 0.0) for r in grp))} | {fm(m5)} ({n5}) | {fm(m30)} ({n30}) | "
                   f"{fm(mean(r['half_spread_ticks'] for r in grp))} |")
    out.append("")
    out.append("Fill-level terciles by EW vol 5m at fill time (ticks):")
    out.append("")
    out.append("| tercile | n fills | vol 5m at fill | half-spread | markout 5m | markout 30m | adverse 30m |")
    out.append("|---|---|---|---|---|---|---|")
    for name, grp in zip(("low", "mid", "high"), _terciles([(f["vol5_at"], f) for f, _ in fills])):
        hs = mean(f["ticks_from_mid"] for f in grp)
        m30 = mean(f["m30"] for f in grp)
        out.append(f"| {name} | {len(grp)} | {fm(mean(f['vol5_at'] for f in grp))} | {fm(hs)} | {fm(mean(f['m5'] for f in grp))} | "
                   f"{fm(m30)} | {fm(hs - m30 if hs is not None and m30 is not None else None)} |")
    return "\n".join(out)


# --------------------------------------------------------------------------------------------------------- CLI
def report(tr: Tracker, top: int = 25, band: Optional[tuple] = None) -> str:
    snap = tr.snapshot()
    rows = snap["exchanges"]
    ranked = tr.rank(top, price_band=band) if band else snap["rank"]
    lines = [f"market tracker: {snap['n_exchanges']} exchanges, window {snap['window_h']} h to {snap['generated_at']}, "
             f"priors k_fill={snap['priors']['k_fill']} adverse30={snap['priors']['adverse_30m']} (n={snap['priors']['n_adverse']})"
             + (f", mid in [{band[0]}, {band[1]}]" if band else ""),
             f"{'ex':>5} {'race':<28} {'P':1} {'mid':>6} {'src':4} {'vol5':>5} {'vol1h':>5} {'nvol1h':>6} {'vol/h':>7} {'tr/h':>5} "
             f"{'sprd':>4} {'dmm':>4} {'swp/h':>5} {'fills':>5} {'f/h':>5} {'mo5':>6} {'mo30':>6} {'n':>3} {'score':>6} {'susq/h':>6}"]
    for ex, s in ranked[:top]:
        r = rows[ex]
        def f(v, nd=2, w=5):
            return f"{'-':>{w}}" if v is None else f"{v:>{w}.{nd}f}"
        lines.append(f"{ex:>5} {(r['race'] or '?')[:28]:<28} {(r['party'] or '?'):1} {f(r['mid'], 3, 6)} {r['vol_source'][:4]:4} "
                     f"{f(r['vol5'], 2, 5)} {f(r['vol1h'], 2, 5)} {f(r['nvol1h'], 1, 6)} {f(r['volume_per_h'], 0, 7)} {f(r['trades_per_h'], 1, 5)} "
                     f"{f(r['spread_ticks'], 0, 4)} {(str(int(r['dmm_touch'])) if r['dmm_touch'] else '-'):>4} {f(r['sweeps_per_h'], 2, 5)} "
                     f"{r['mm_fills']:>5} {f(r['fills_per_h'], 2, 5)} {f(r['markout_5m']['mean'], 2, 6)} {f(r['markout_30m']['mean'], 2, 6)} "
                     f"{r['markout_30m']['n']:>3} {f(s, 2, 6)} {f(r['edge_susq_per_h'], 1, 6)}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="per-market tracker: rebuild from the SQLite log and report")
    ap.add_argument("--report", action="store_true", help="print the top markets by expected MM edge")
    ap.add_argument("--research", action="store_true", help="print the volatility vs volume / fills / markouts tables")
    ap.add_argument("--write", action="store_true", help="also write params/market_tracker.json")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--band", type=float, nargs=2, metavar=("LO", "HI"), default=None,
                    help="rank only markets whose mid lies in [LO, HI] (e.g. the MM band 0.12 0.88)")
    a = ap.parse_args(argv)
    tr = Tracker.from_store(a.db, hours=a.hours)
    if a.report or not a.research:
        print(report(tr, a.top, tuple(a.band) if a.band else None))
    if a.research:
        print(research(tr))
    if a.write:
        print("wrote", tr.write())


if __name__ == "__main__":
    main()
