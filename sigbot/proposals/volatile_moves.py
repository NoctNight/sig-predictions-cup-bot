"""Volatile-move strategy for the SIG Predictions Cup (paper module: nothing here sends an order).

Evidence (research_notes/volatile_moves.md, 2026-10-22, 5-minute candles 6-21 Oct joined to 15 days of Polymarket /
Kalshi history, the tape, and the 25-minute realtime capture):
  * 336 moves of 5+ ticks within an hour on 237 markets in 16 days (21/day; 36 of 10+ ticks, 2.2/day). Polymarket and
    Kalshi were flat around 85% of them; outside-led catch-ups are 1%, outside-confirmed information 5%.
  * They are mostly DMM ladder shifts, not sweeps through depth: median volume 60k shares, below one deep DMM level;
    the capture shows the DMM moving its whole ladder (its 75k-230k deep orders are pulled and re-posted, not eaten),
    and the tape in the move windows is two-sided (aggressor shares with the move 444k, against 469k).
  * No class has a robust pre-election edge (checked independently by a second model run, 22 Oct):
    - fading a move that widened the gap to the outside fair, passive, exit at half retrace or 24 h: +0.25 t/share
      (n 83, t 0.5); without its 3 best races -0.38; one day (14 Oct) carries it. Gap-closing moves: -0.84 (t -1.6).
    - a fade resting at the post-move price is under water by ~1 tick at +60 min (t -5.4, n 189): quotes left on the
      retrace side right after a 5+ tick move are adversely selected for about an hour.
    - catch-up to a Polymarket 2+ pt move: 15-day candle replay flat to negative (n 77); on logged quotes +5.5 t
      (n 10) but all on one trending night, and Polymarket moves SIG had already followed earned the same (+5.0, n 3).
  * Polymarket candidate-level legs (Alaska, California governor) produce fake 80-96 pt jumps when instruments are
    merged: the loaders skip multi-instrument legs.

Three paper trades, all sized within the directional caps passed in (per-leg notional, total notional, blocked partisan
directions from sigbot.risk):
  fade     a 5+ tick move that widened the gap to the outside fair: rest on the retrace side at the extreme, exit at
           half the move retraced or 24 h (no significant edge: keep in paper);
  follow   an outside-venue move of 2+ pts (and under 25 pts) that SIG has followed by less than a third: skip when
           Kalshi moved 1+ pt the other way; fire at once when the Polymarket spread is 2 pts or less, otherwise wait
           30 min and fire only if the move is still 75% there; take the DMM touch, exit when SIG has closed half the move or after 4 h
           (extends dmm_lag_follow with a Kalshi veto, an outlier filter and a 4 h exit; no edge established: paper only);
  headline the news sweeper's news_events rows with expected_move_pts >= 3 and confidence >= 0.6: take the touch
           (no backtest possible yet: 252 dry classifications in 13 h, none reached the threshold).
Scheduled events (--calendar) are handled by pulling quotes (the market maker's news_pause / hot_until mechanism), not by
trading them; election night is where the DMM's 3-20 minute lag behind Kalshi/Polymarket pays most (see --calendar).

CLI:  python3 -m sigbot.proposals.volatile_moves --backtest [--since 2026-10-06] [--ticks 5] [--qty 1000] [--md]
      python3 -m sigbot.proposals.volatile_moves --calendar
Live wiring (parent session): d = MoveDetector(); s = VolatileMoves(cfg); each cycle:
      ev = d.on_books(now, feed.books()) + d.on_outside(now, direction.rows) + d.on_news(store.conn)
      orders = s.cycle(now, ev, feed.books(), outside_fair, blocked, dmm_touch)      # paper orders, nothing sent
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TICK = 0.005
DST_END = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc).timestamp()     # US clocks fall back 1 Nov 2026


def et(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=-5 if ts >= DST_END else -4)))


def parse_iso(s: str) -> float:
    s = s.replace("Z", "+00:00")
    if "." in s:                                   # trim 7-digit fractions
        head, tail = s.split(".", 1)
        frac = "".join(c for c in tail if c.isdigit())[:6]
        tz = tail[len("".join(c for c in tail if c.isdigit())):]
        s = f"{head}.{frac}{tz}"
    return datetime.fromisoformat(s).timestamp()


def race_of(title: str) -> tuple[str, str]:
    t = title.replace("Will the ", "").replace(" Party win the ", "|").rstrip("?")
    party, race = t.split("|", 1)
    return race, party[0]


# ----------------------------------------------------------------------------- data
def load_markets(path=None) -> dict:
    """exchange id -> (race, party, market id)."""
    path = path or os.path.join(HERE, "data", "markets.json")
    out = {}
    for m in json.load(open(path)):
        for x in m.get("exchanges") or []:
            if x.get("option") == "YES":
                race, party = race_of(m["title"])
                out[str(x["id"])] = (race, party, str(m["id"]))
    return out


def load_candles(path=None) -> dict:
    """exchange id -> dict of parallel lists ts/close/high/low/vol (event clock: only bars that traded)."""
    path = path or os.path.join(HERE, "data", "candles_5m.json")
    out = {}
    for ex, v in json.load(open(path)).items():
        cs = v.get("candles") or []
        if cs:
            out[ex] = dict(ts=[parse_iso(c["time"]) for c in cs], close=[c["close"] for c in cs], high=[c["high"] for c in cs],
                           low=[c["low"] for c in cs], vol=[c["volume"] for c in cs])
    return out


def load_outside(path=None) -> dict:
    """race -> {"pm": (ts, px), "kalshi": (ts, px)} for the D leg (data/model_history.json, 5-min PM, 1-min Kalshi)."""
    path = path or os.path.join(HERE, "data", "model_history.json")
    h = json.load(open(path))
    out = collections.defaultdict(dict)
    for venue in ("pm", "kalshi"):
        for race, d in (h.get(venue) or {}).items():
            if d and len(d) == 1:                     # multi-token (candidate-level) races are not a clean D leg: skip
                s = next(iter(d.values()))
                if s:
                    out[race][venue] = ([x[0] for x in s], [x[1] for x in s])
    return out


def at(series, t: float):
    """Last value at or before t from (ts, px) lists, else None."""
    if not series:
        return None
    ts, px = series
    i = bisect.bisect_right(ts, t) - 1
    return px[i] if i >= 0 else None


def leg_fair(outside: dict, race: str, party: str, t: float) -> Optional[float]:
    """Outside fair for a leg at t: Polymarket D-leg, Kalshi when Polymarket is missing; None for I legs or no data."""
    if party not in ("D", "R"):
        return None
    o = outside.get(race) or {}
    p = at(o.get("pm"), t)
    if p is None:
        p = at(o.get("kalshi"), t)
    if p is None:
        return None
    return p if party == "D" else 1.0 - p


# ----------------------------------------------------------------------------- live detector
class Event(dict):
    """kind: sweep | outside_lead | headline; plus exchange_id, direction (+1 up / -1 down), size_pts, t0, p_ref, p_now."""


class MoveDetector:
    def __init__(self, sweep_ticks: int = 5, window_sec: float = 3600, outside_pts: float = 0.02, outside_window_sec: float = 1800,
                 confirm_sec: float = 1800, confirm_keep: float = 0.75, follow_frac: float = 0.34, headline_min_pts: float = 3.0,
                 headline_min_conf: float = 0.6, cooldown_sec: float = 3600, outside_max_pts: float = 0.25,
                 kalshi_confirm_pts: float = 0.01, max_pm_spread: float = 0.02):
        self.sweep_ticks, self.window_sec, self.cooldown_sec = sweep_ticks, window_sec, cooldown_sec
        self.outside_pts, self.outside_window_sec, self.confirm_sec, self.confirm_keep = outside_pts, outside_window_sec, confirm_sec, confirm_keep
        self.follow_frac, self.headline_min_pts, self.headline_min_conf = follow_frac, headline_min_pts, headline_min_conf
        self.outside_max_pts, self.kalshi_confirm_pts, self.max_pm_spread = outside_max_pts, kalshi_confirm_pts, max_pm_spread
        self.mids: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=2000))
        self.last_sweep: dict[str, float] = {}
        self.ohist: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=400))
        self.pending: dict[str, dict] = {}            # exchange -> candidate outside move awaiting confirmation
        self.last_outside: dict[str, float] = {}
        self.last_news_id = 0

    @staticmethod
    def _best(book) -> tuple:
        bids, asks = book.get("bids") or [], book.get("asks") or []
        b = bids[0] if bids else None
        a = asks[0] if asks else None
        f = lambda lvl: (lvl["price"], lvl["quantity"]) if isinstance(lvl, dict) else (lvl[0], lvl[1])
        return (f(b) if b else None), (f(a) if a else None)

    def on_books(self, ts: float, books: dict) -> list[Event]:
        """books: exchange -> book in REST shape ({bids:[{price,quantity}]}) or feed shape ([[price, qty], ...])."""
        out = []
        for ex, book in books.items():
            b, a = self._best(book)
            if not b or not a:
                continue
            mid = (b[0] + a[0]) / 2
            h = self.mids[ex]
            h.append((ts, mid))
            while h and ts - h[0][0] > self.window_sec:
                h.popleft()
            if ts - self.last_sweep.get(ex, -1e18) < self.cooldown_sec or len(h) < 3:
                continue
            lo = min(h, key=lambda x: x[1]); hi = max(h, key=lambda x: x[1])
            if mid - lo[1] >= self.sweep_ticks * TICK - 1e-9:
                out.append(Event(kind="sweep", exchange_id=ex, direction=+1, size_pts=mid - lo[1], t0=lo[0], p_ref=lo[1], p_now=mid,
                                 bid=b, ask=a))
                self.last_sweep[ex] = ts
            elif hi[1] - mid >= self.sweep_ticks * TICK - 1e-9:
                out.append(Event(kind="sweep", exchange_id=ex, direction=-1, size_pts=mid - hi[1], t0=hi[0], p_ref=hi[1], p_now=mid,
                                 bid=b, ask=a))
                self.last_sweep[ex] = ts
        return out

    def on_outside(self, ts: float, rows: list[dict]) -> list[Event]:
        """rows: the direction sleeve's rows [{exchange_id, pm, kalshi, sig_mid, sig_bid, sig_ask, pm_spread?}] (YES-leg prices).
        A move fires at once unless Kalshi moved the other way; with a wide Polymarket spread it waits confirm_sec for persistence."""
        out = []
        for r in rows:
            ex = r.get("exchange_id")
            pm, k, sig = r.get("pm"), r.get("kalshi"), r.get("sig_mid")
            o = pm if pm is not None else k
            if ex is None or o is None or sig is None:
                continue
            h = self.ohist[ex]
            h.append((ts, o, sig, k))
            while h and ts - h[0][0] > max(self.outside_window_sec, self.confirm_sec) + 60:
                h.popleft()
            if ex in self.pending:
                c = self.pending[ex]
                if ts - c["t"] >= self.confirm_sec:
                    kept = (o - c["o0"]) * c["dir"]
                    k_ok = k is None or c["k0"] is None or (k - c["k0"]) * c["dir"] >= 0.01
                    followed = (sig - c["sig0"]) * c["dir"]
                    self.pending.pop(ex)
                    if kept >= self.confirm_keep * abs(c["d"]) and k_ok and followed < self.follow_frac * abs(c["d"]):
                        out.append(Event(kind="outside_lead", exchange_id=ex, direction=c["dir"], size_pts=abs(c["d"]), t0=c["t"],
                                         p_ref=c["sig0"], p_now=sig, outside_now=o, sig_bid=r.get("sig_bid"), sig_ask=r.get("sig_ask")))
                        self.last_outside[ex] = ts
                continue
            if ts - self.last_outside.get(ex, -1e18) < self.cooldown_sec:
                continue
            ref = next((x for x in h if ts - x[0] <= self.outside_window_sec), None)
            if ref is None or ref[0] == ts:
                continue
            d = o - ref[1]
            if not (self.outside_pts <= abs(d) <= self.outside_max_pts) or abs(sig - ref[2]) >= self.follow_frac * abs(d):
                continue
            sgn = 1 if d > 0 else -1
            k_against = k is not None and ref[3] is not None and (k - ref[3]) * sgn <= -self.kalshi_confirm_pts
            spread_ok = r.get("pm_spread") is None or r["pm_spread"] <= self.max_pm_spread
            if k_against:
                continue
            if spread_ok:                                  # quote replay: Kalshi agreement added nothing (n 7 vs 7)
                out.append(Event(kind="outside_lead", exchange_id=ex, direction=sgn, size_pts=abs(d), t0=ts, p_ref=ref[2], p_now=sig,
                                 outside_now=o, sig_bid=r.get("sig_bid"), sig_ask=r.get("sig_ask")))
                self.last_outside[ex] = ts
            else:
                self.pending[ex] = dict(t=ts, o0=ref[1], sig0=ref[2], k0=ref[3], d=d, dir=sgn)
        return out

    def on_news(self, conn) -> list[Event]:
        """New news_events rows (any action) above the thresholds; conn is a read-only sqlite3 connection."""
        out = []
        try:
            rows = conn.execute("SELECT id, ts, exchange_id, direction, expected_move_pts, confidence, headline FROM news_events WHERE id > ? ORDER BY id",
                                (self.last_news_id,)).fetchall()
        except sqlite3.Error:
            return out
        for rid, ts, ex, direction, pts, conf, headline in rows:
            self.last_news_id = max(self.last_news_id, rid)
            if ex and (pts or 0) >= self.headline_min_pts and (conf or 0) >= self.headline_min_conf:
                out.append(Event(kind="headline", exchange_id=str(ex), direction=1 if direction == "up" else -1, size_pts=pts / 100.0,
                                 t0=ts, headline=headline))
        return out


# ----------------------------------------------------------------------------- strategy (paper)
class VolatileMoves:
    def __init__(self, cfg: Optional[dict] = None, universe=None):
        c = dict(per_leg_cap=300.0, total_cap=3000.0, fade_qty=1000, follow_qty=1000, headline_qty=500, fade_hold_sec=86400,
                 follow_hold_sec=14400, headline_hold_sec=14400, fade_min_widen_ticks=2, fade_exit_frac=0.5, follow_exit_frac=0.5,
                 order_ttl_sec=600, min_price=0.03, max_price=0.97)
        c.update(cfg or {})
        self.cfg = c
        self.u = universe
        self.open: dict[str, dict] = {}
        self.cooldown: dict[str, float] = {}

    def _notional_open(self) -> float:
        return sum(p["qty"] * p["cost_px"] for p in self.open.values())

    def _size(self, qty_default: int, px_cost: float, touch_qty: Optional[float]) -> int:
        room = min(self.cfg["per_leg_cap"], max(0.0, self.cfg["total_cap"] - self._notional_open()))
        q = min(qty_default, int(room / max(px_cost, 0.01)))
        if touch_qty:
            q = min(q, int(touch_qty))
        return max(q, 0)

    @staticmethod
    def _order(ex, side, price, qty, reason, kind):
        return dict(exchangeId=ex, side=side, action="buy", price=round(price, 3), quantity=int(qty), reason=reason, purpose="volatile", kind=kind)

    def _blocked(self, ex: str, side: str, blocked: set) -> bool:
        if not self.u or not blocked:
            return False
        try:
            from ..risk import adds
            return adds(self.u, ex, side) in blocked
        except Exception:
            return False

    def cycle(self, ts: float, events: list[Event], books: dict, outside_fair: dict, blocked: set = frozenset(),
              dmm_touch: Optional[dict] = None) -> list[dict]:
        """Returns paper orders. outside_fair: exchange -> fair (YES). dmm_touch: exchange -> DMM touch size."""
        out = []
        # exits first
        for ex, pos in list(self.open.items()):
            b, a = MoveDetector._best(books.get(ex, {}))
            if not b or not a:
                continue
            mid = (b[0] + a[0]) / 2
            done = ts - pos["t0"] >= pos["hold"]
            if pos["kind"] == "fade":
                done = done or (pos["p_entry"] - mid) * pos["dir"] >= pos["frac"] * pos["size"]      # retraced
            else:
                done = done or (mid - pos["p_entry"]) * pos["dir"] >= pos["frac"] * pos["size"]      # converged
            if done:                                           # exit crosses the spread (backtest charges half a tick: conservative)
                px = b[0] if pos["yes_long"] else a[0]
                side = "no" if pos["yes_long"] else "yes"
                out.append(self._order(ex, side, (1 - px) if side == "no" else px, pos["qty"], "exit", pos["kind"]))
                self.open.pop(ex); self.cooldown[ex] = ts + 1800
        for ev in events:
            ex = ev["exchange_id"]
            if ex in self.open or self.cooldown.get(ex, 0) > ts:
                continue
            b, a = MoveDetector._best(books.get(ex, {}))
            if not b or not a or not (self.cfg["min_price"] <= (b[0] + a[0]) / 2 <= self.cfg["max_price"]):
                continue
            touch = (dmm_touch or {}).get(ex)
            fair = outside_fair.get(ex)
            if ev["kind"] == "sweep":
                if fair is None:
                    continue
                widen = (abs(ev["p_now"] - fair) - abs(ev["p_ref"] - fair)) / TICK
                if widen < self.cfg["fade_min_widen_ticks"]:
                    continue                                       # the move closed the gap: let it run
                yes_long = ev["direction"] < 0                     # fade a down move by buying YES
                side = "yes" if yes_long else "no"
                if self._blocked(ex, side, blocked):
                    continue
                px = b[0] if yes_long else a[0]                    # passive: bid for YES (down move), ask for NO (up move)
                cost = px if yes_long else 1 - px
                qty = self._size(self.cfg["fade_qty"], cost, touch)
                if qty <= 0:
                    continue
                self.open[ex] = dict(kind="fade", t0=ts, dir=ev["direction"], p_entry=px, size=abs(ev["size_pts"]), qty=qty, cost_px=cost,
                                     hold=self.cfg["fade_hold_sec"], frac=self.cfg["fade_exit_frac"], yes_long=yes_long)
                out.append(self._order(ex, side, cost, qty, f"fade {ev['size_pts']*100:+.1f} pts, gap widened {widen:.0f} ticks vs fair {fair:.3f}", "fade"))
            elif ev["kind"] in ("outside_lead", "headline"):
                yes_long = ev["direction"] > 0
                side = "yes" if yes_long else "no"
                if self._blocked(ex, side, blocked):
                    continue
                px = a[0] if yes_long else b[0]                    # take the DMM touch
                cost = px if yes_long else 1 - px
                qty = self._size(self.cfg["follow_qty"] if ev["kind"] == "outside_lead" else self.cfg["headline_qty"], cost, touch)
                if qty <= 0:
                    continue
                hold = self.cfg["follow_hold_sec"] if ev["kind"] == "outside_lead" else self.cfg["headline_hold_sec"]
                self.open[ex] = dict(kind=ev["kind"], t0=ts, dir=ev["direction"], p_entry=px, size=abs(ev["size_pts"]), qty=qty, cost_px=cost,
                                     hold=hold, frac=self.cfg["follow_exit_frac"], yes_long=yes_long)
                out.append(self._order(ex, side, cost, qty, f"{ev['kind']} {ev['size_pts']*100:+.1f} pts" + (f": {ev.get('headline','')[:60]}" if ev.get("headline") else ""), ev["kind"]))
        return out


# ----------------------------------------------------------------------------- backtest
def _ff(c: dict, t: float) -> Optional[float]:
    i = bisect.bisect_right(c["ts"], t) - 1
    return c["close"][i] if i >= 0 else None


def _stats(pnl: list, qty: int, days: float) -> dict:
    if not pnl:
        return dict(n=0)
    return dict(n=len(pnl), hit=round(sum(1 for p in pnl if p > 0) / len(pnl), 2), mean_ticks=round(statistics.mean(pnl), 2),
                median_ticks=round(statistics.median(pnl), 2), worst_ticks=round(min(pnl), 1), best_ticks=round(max(pnl), 1),
                per_day=round(len(pnl) / days, 1), susqies_per_day=round(statistics.mean(pnl) * TICK * qty * len(pnl) / days, 0),
                t=round(statistics.mean(pnl) / (statistics.pstdev(pnl) / len(pnl) ** 0.5), 1) if len(pnl) > 2 and statistics.pstdev(pnl) > 0 else None)


def backtest_fade(candles, markets, outside, since: float, until: float, ticks: int, qty: int, exit_frac: float = 0.5,
                  min_widen: int = 2, hold_min: int = 1440, exit_cost: float = 0.5) -> dict:
    """In-flight detection on forward-filled 5m closes: first bar where close is >= ticks from the opposite extreme of the
    trailing 60 min. Fade order rests at the detection close; passive fill when a later bar (within 60 min) trades one tick
    through it; taker variant enters one tick worse at once. Exit at the first close that retraces exit_frac of the detected
    move, else at hold_min; the exit pays exit_cost ticks (half the one-tick spread: the close is a print at either touch).
    Markouts at +15/+60/+240/+1440 min from the detection close (no exit cost).
    Caveat: candles exist only for bars that traded, so a close can be stale when the DMM requoted without a print."""
    rows = []
    for ex, c in candles.items():
        race, party, _ = markets.get(ex, ("?", "?", None))
        ts, cl, hi, lo = c["ts"], c["close"], c["high"], c["low"]
        n = len(ts); i = 0; last_det = -1e9
        while i < n:
            t = ts[i]
            if t < since or t > until or t - last_det < 3600:
                i += 1; continue
            w = [k for k in range(max(0, i - 12), i) if t - ts[k] <= 3600]
            if not w:
                i += 1; continue
            lo_k = min(w, key=lambda k: cl[k]); hi_k = max(w, key=lambda k: cl[k])
            d_up, d_dn = cl[i] - cl[lo_k], cl[hi_k] - cl[i]
            if d_up >= ticks * TICK - 1e-9:
                direction, ref_k, size = +1, lo_k, d_up
            elif d_dn >= ticks * TICK - 1e-9:
                direction, ref_k, size = -1, hi_k, d_dn
            else:
                i += 1; continue
            p_det = cl[i]
            if not (0.03 <= p_det <= 0.97):
                i += 1; continue
            last_det = t
            fair = leg_fair(outside, race, party, t)
            widen = None if fair is None else (abs(p_det - fair) - abs(cl[ref_k] - fair)) / TICK
            cls = "nofair" if widen is None else ("widen" if widen >= min_widen else "close")
            # passive fill: price trades one tick through our level within 60 min
            fill_k = None
            for k in range(i + 1, n):
                if ts[k] - t > 3600:
                    break
                if (direction > 0 and hi[k] >= p_det + TICK - 1e-9) or (direction < 0 and lo[k] <= p_det - TICK + 1e-9):
                    fill_k = k; break
            mo = {h: _ff(c, t + h * 60) for h in (15, 60, 240, 1440)}
            mark = {h: None if v is None else round((p_det - v) * direction / TICK, 2) for h, v in mo.items()}   # ticks gained by the fade
            # exit rule: first close after entry retracing exit_frac*size, else hold_min. The passive trade searches from the
            # bar after its fill (it cannot exit before it is filled); the taker variant from the bar after detection.
            def _exit(start):
                for k in range(start, n):
                    if ts[k] - t > hold_min * 60:
                        break
                    if (p_det - cl[k]) * direction >= exit_frac * size - 1e-9:
                        return cl[k], ts[k]
                return _ff(c, t + hold_min * 60), t + hold_min * 60
            exit_px, exit_t = _exit(fill_k + 1 if fill_k is not None else i + 1)
            taker_px, _ = _exit(i + 1)
            rows.append(dict(ex=ex, race=race, party=party, t=t, et=et(t).strftime("%m-%d %H:%M"), dir=direction, size_ticks=round(size / TICK),
                             p_ref=cl[ref_k], p_det=p_det, fair=fair, widen=None if widen is None else round(widen, 1), cls=cls,
                             filled=fill_k is not None, fill_wait_min=None if fill_k is None else round((ts[fill_k] - t) / 60),
                             mark=mark, rule_ticks=None if exit_px is None else round((p_det - exit_px) * direction / TICK - exit_cost, 2),
                             rule_taker=None if taker_px is None else round((p_det - taker_px) * direction / TICK - exit_cost, 2),
                             exit_min=None if exit_t is None else round((exit_t - t) / 60), toward50=abs(p_det - 0.5) < abs(cl[ref_k] - 0.5)))
            i += 1
    days = max((until - since) / 86400, 1e-9)
    res = dict(events=len(rows), per_day=round(len(rows) / days, 1), days=round(days, 1), by_class={})
    for cls in ("widen", "close", "nofair", "all"):
        sub = [r for r in rows if cls == "all" or r["cls"] == cls]
        if not sub:
            continue
        filled = [r for r in sub if r["filled"]]
        d = dict(detections=len(sub), fill_rate=round(len(filled) / len(sub), 2))
        for label, pool in (("passive_filled", filled), ("taker_all", sub)):
            adj = 0.0 if label == "passive_filled" else 1.0          # taker pays one tick to enter
            for h in (15, 60, 240, 1440):
                pn = [r["mark"][h] - adj for r in pool if r["mark"][h] is not None]
                d[f"{label}_mark{h}"] = _stats(pn, qty, days)
            key = "rule_ticks" if label == "passive_filled" else "rule_taker"
            pn = [r[key] - adj for r in pool if r[key] is not None]
            d[f"{label}_rule"] = _stats(pn, qty, days)
        res["by_class"][cls] = d
    # size buckets (widen only, passive, rule exit)
    res["widen_by_size"] = {}
    for lab, lo_, hi_ in (("5-6", 5, 6), ("7-9", 7, 9), ("10+", 10, 99)):
        pn = [r["rule_ticks"] for r in rows if r["cls"] == "widen" and r["filled"] and lo_ <= r["size_ticks"] <= hi_ and r["rule_ticks"] is not None]
        res["widen_by_size"][lab] = _stats(pn, qty, days)
    w = [r for r in rows if r["cls"] == "widen" and r["filled"] and r["rule_ticks"] is not None]
    by_race = collections.Counter()
    for r in w:
        by_race[r["race"]] += r["rule_ticks"]
    top = {k for k, _ in by_race.most_common(3)}
    res["widen_ex_top3_races"] = dict(excluded=sorted(top), **_stats([r["rule_ticks"] for r in w if r["race"] not in top], qty, days))
    by_day = collections.Counter()
    for r in w:
        by_day[et(r["t"]).strftime("%m-%d")] += r["rule_ticks"]
    res["widen_by_day_ticks"] = dict(sorted((k, round(v, 1)) for k, v in by_day.items()))
    res["hour_et"] = dict(sorted(collections.Counter(et(r["t"]).hour for r in rows).items()))
    res["rows"] = rows
    return res


def backtest_follow(candles, markets, outside, since: float, until: float, pts: float, qty: int, confirm_min: int = 30,
                    keep: float = 0.75, exit_frac: float = 0.5, hold_min: int = 1440) -> dict:
    """Outside move of >= pts within 30 min on the Polymarket D leg (Kalshi when missing); SIG D leg followed by < 1/3 at
    detection. Confirmation after confirm_min: the outside move still >= keep of itself and Kalshi (if any) agrees by
    >= 1 pt. Entry: taker at the SIG close after confirmation, one tick worse. Exit: first close where SIG has moved
    exit_frac of the outside move (one tick worse), else at hold_min."""
    legs = collections.defaultdict(dict)
    for ex, (race, party, _) in markets.items():
        legs[race][party] = ex
    rows = []
    for race, o in outside.items():
        pm = o.get("pm") or o.get("kalshi")
        ks = o.get("kalshi") if o.get("pm") else None
        ex = legs.get(race, {}).get("D")
        if not pm or ex not in candles:
            continue
        c = candles[ex]
        ts, px = pm
        last = -1e9
        for i in range(len(ts)):
            t = ts[i]
            if t < since or t > until or t - last < 3600:
                continue
            p30 = at(pm, t - 1800)
            if p30 is None:
                continue
            d = px[i] - p30
            if abs(d) < pts:
                continue
            s0, s_pre = _ff(c, t), _ff(c, t - 1800)
            if s0 is None or s_pre is None or not (0.05 <= s0 <= 0.95) or (s0 - s_pre) * (1 if d > 0 else -1) >= abs(d) / 3:
                continue
            last = t
            tc = t + confirm_min * 60
            kept = (at(pm, tc) - px[i] + d) if at(pm, tc) is not None else None
            k0, k1 = at(ks, t - 1800), at(ks, tc)
            k_ok = ks is None or k0 is None or k1 is None or (k1 - k0) * (1 if d > 0 else -1) >= 0.01
            s_c = _ff(c, tc)
            confirmed = kept is not None and kept * (1 if d > 0 else -1) >= keep * abs(d) and k_ok and s_c is not None and (s_c - s_pre) * (1 if d > 0 else -1) < abs(d) / 3
            sgn = 1 if d > 0 else -1
            mark = {h: None if _ff(c, tc + h * 60) is None else round((_ff(c, tc + h * 60) - (s_c if s_c is not None else s0)) * sgn / TICK - 1, 2) for h in (60, 240, 1440)}
            exit_px = None
            for k in range(bisect.bisect_right(c["ts"], tc), len(c["ts"])):
                if c["ts"][k] - tc > hold_min * 60:
                    break
                if (c["close"][k] - (s_c if s_c is not None else s0)) * sgn >= exit_frac * abs(d):
                    exit_px = c["close"][k]; break
            if exit_px is None:
                exit_px = _ff(c, tc + hold_min * 60)
            rule = None if exit_px is None or s_c is None else round((exit_px - s_c) * sgn / TICK - 2, 2)
            rows.append(dict(race=race, t=t, et=et(t).strftime("%m-%d %H:%M"), d_pts=round(d * 100, 1), kalshi_ok=k_ok, confirmed=bool(confirmed),
                             s0=s0, mark=mark, rule_ticks=rule))
    days = max((until - since) / 86400, 1e-9)
    res = dict(signals=len(rows), per_day=round(len(rows) / days, 1), n_confirmed=sum(r["confirmed"] for r in rows))
    for lab, pool in (("unfiltered", rows), ("confirmed", [r for r in rows if r["confirmed"]])):
        d = {}
        for h in (60, 240, 1440):
            d[f"mark{h}"] = _stats([r["mark"][h] for r in pool if r["mark"][h] is not None], qty, days)
        d["rule"] = _stats([r["rule_ticks"] for r in pool if r["rule_ticks"] is not None], qty, days)
        res[lab] = d
    res["rows"] = rows
    return res


def backtest_follow_quotes(db: str, pts: float, qty: int, hold_min: int = 240, max_pts: float = 0.25) -> dict:
    """Replay the follow rule on logged quotes (venue_quotes: Polymarket / Kalshi / SIG bid-ask per poll, D legs). Signal:
    Polymarket mid moved pts..max_pts in 30 min, SIG mid moved < 1/3 of it, one signal per race-hour. Entry: SIG ask (up)
    or bid (down) at the signal poll; exit: the opposite touch at +hold_min. Split by Kalshi confirmation (>= 1 pt same way)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT ts, venue, race, mid, bid, ask, instrument FROM venue_quotes WHERE party='D' AND mid IS NOT NULL ORDER BY ts").fetchall()
    if not rows:
        return dict(signals=0)
    ser = collections.defaultdict(lambda: ([], []))
    insts = collections.defaultdict(set)
    for ts, v, r, m, b, a, inst in rows:
        insts[(v, r)].add(inst)
    for ts, v, r, m, b, a, inst in rows:
        if len(insts[(v, r)]) == 1:                    # several instruments per leg (candidate-level markets) made fake jumps
            ser[(v, r)][0].append(ts); ser[(v, r)][1].append((m, b, a))
    t_end, t_start = rows[-1][0], rows[0][0]
    days = max((t_end - t_start) / 86400, 1e-9)
    out = []
    for (v, race), s in list(ser.items()):
        sig, ks = ser.get(("sig", race)), ser.get(("kalshi", race))
        if v != "polymarket" or not sig or not sig[0]:
            continue
        last = -1e9
        for ts, (m, b, a) in zip(*s):
            if ts - last < 3600 or ts + hold_min * 60 > t_end:
                continue
            p30, s0, sp = at((s[0], s[1]), ts - 1800), at(sig, ts), at(sig, ts - 1800)
            if not p30 or not s0 or not sp or s0[1] is None or s0[2] is None:
                continue
            d = m - p30[0]
            sgn = 1 if d > 0 else -1
            if not (pts <= abs(d) <= max_pts):
                continue
            lagged = (s0[0] - sp[0]) * sgn < abs(d) / 3
            last = ts
            s1 = at(sig, ts + hold_min * 60)
            if not s1 or s1[1] is None or s1[2] is None:
                continue
            k0, k1 = (at(ks, ts - 1800), at(ks, ts)) if ks and ks[0] else (None, None)
            k_ok = bool(k0 and k1 and (k1[0] - k0[0]) * sgn >= 0.01)
            entry, exit_ = (s0[2], s1[1]) if sgn > 0 else (s0[1], s1[2])
            out.append(dict(race=race, t=ts, et=et(ts).strftime("%m-%d %H:%M"), d_pts=round(d * 100, 1), kalshi_ok=k_ok, lagged=lagged,
                            pm_spread=None if a is None or b is None else round(a - b, 3), ticks=round((exit_ - entry) * sgn / TICK, 1)))
    control = [o for o in out if not o["lagged"]]
    out = [o for o in out if o["lagged"]]
    res = dict(window_days=round(days, 2), signals=len(out))
    res["all"] = _stats([o["ticks"] for o in out], qty, days)
    res["kalshi_confirmed"] = _stats([o["ticks"] for o in out if o["kalshi_ok"] and (o["pm_spread"] or 0) <= 0.02], qty, days)
    res["unconfirmed"] = _stats([o["ticks"] for o in out if not (o["kalshi_ok"] and (o["pm_spread"] or 0) <= 0.02)], qty, days)
    res["control_sig_already_followed"] = _stats([o["ticks"] for o in control], qty, days)
    res["rows"] = out
    return res


def backtest(since: str, until: Optional[str], ticks: int, qty: int, outside_pts: float) -> dict:
    markets, candles, outside = load_markets(), load_candles(), load_outside()
    t0 = parse_iso(since + "T00:00:00Z") if len(since) == 10 else parse_iso(since)
    t1 = parse_iso(until + "T00:00:00Z") if until else max(c["ts"][-1] for c in candles.values()) - 86400   # leave 24 h for markouts
    fade = backtest_fade(candles, markets, outside, t0, t1, ticks, qty)
    follow = backtest_follow(candles, markets, outside, t0, t1, outside_pts, qty)
    follow_q = backtest_follow_quotes(os.path.join(HERE, "data", "sigbot.sqlite"), outside_pts, qty)
    news = dict(note="no backtest: news_events had 0 rows above the thresholds")
    try:
        conn = sqlite3.connect(f"file:{os.path.join(HERE, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
        n_all, n_hit, tmin, tmax = conn.execute("SELECT COUNT(*), SUM(expected_move_pts >= 3 AND confidence >= 0.6), MIN(ts), MAX(ts) FROM news_events").fetchone()
        news = dict(rows=n_all, above_threshold=n_hit or 0, hours=round(((tmax or 0) - (tmin or 0)) / 3600, 1))
    except sqlite3.Error:
        pass
    return dict(since=since, until=datetime.fromtimestamp(t1, timezone.utc).strftime("%Y-%m-%d %H:%M"), ticks=ticks, qty=qty, outside_pts=outside_pts,
                fade=fade, follow=follow, follow_quotes=follow_q, headline=news)


def _fmt(s: dict) -> str:
    if not s or s.get("n", 0) == 0:
        return "n 0"
    return (f"n {s['n']} hit {s['hit']:.2f} mean {s['mean_ticks']:+.2f}t (t {s['t']}) med {s['median_ticks']:+.2f}t worst {s['worst_ticks']:+.0f}t"
            f" /day {s['per_day']} S/day {s['susqies_per_day']:+.0f}")


def print_backtest(res: dict, md: bool = False):
    f, fo = res["fade"], res["follow"]
    print(f"# backtest {res['since']} -> {res['until']} ({f['days']} days), fade threshold {res['ticks']} ticks, outside {res['outside_pts']*100:.0f} pts, qty {res['qty']}")
    print(f"fade detections {f['events']} ({f['per_day']}/day); ET hour histogram {f['hour_et']}")
    sep = "|" if md else " "
    if md:
        print("\n| class | detections | fill rate | passive, rule exit | passive +60 | passive +24 h | taker, rule exit |\n|---|---|---|---|---|---|---|")
    for cls, d in f["by_class"].items():
        if md:
            print(f"| {cls} | {d['detections']} | {d['fill_rate']} | {_fmt(d['passive_filled_rule'])} | {_fmt(d['passive_filled_mark60'])} | {_fmt(d['passive_filled_mark1440'])} | {_fmt(d['taker_all_rule'])} |")
        else:
            print(f"  {cls:7} detections {d['detections']} fill {d['fill_rate']}\n    passive rule : {_fmt(d['passive_filled_rule'])}\n    passive +60m : {_fmt(d['passive_filled_mark60'])}\n    passive +4h  : {_fmt(d['passive_filled_mark240'])}\n    passive +24h : {_fmt(d['passive_filled_mark1440'])}\n    taker rule   : {_fmt(d['taker_all_rule'])}")
    if md:
        print("\n| widen, by size | passive rule exit |\n|---|---|")
    for lab, s in f["widen_by_size"].items():
        print(f"| {lab} ticks | {_fmt(s)} |" if md else f"  widen {lab} ticks: {_fmt(s)}")
    ex3 = f.get("widen_ex_top3_races") or {}
    print(f"\nwiden fade without its 3 best races ({', '.join(ex3.get('excluded', []))}): {_fmt(ex3)}")
    print(f"widen fade ticks by ET day: {f.get('widen_by_day_ticks')}")
    print(f"\nfollow (candle replay, SIG marked at trade closes) signals {fo['signals']} ({fo['per_day']}/day), confirmed {fo['n_confirmed']}")
    if md:
        print("\n| follow | +60 min | +4 h | +24 h | rule exit |\n|---|---|---|---|---|")
    for lab in ("unfiltered", "confirmed"):
        d = fo[lab]
        if md:
            print(f"| {lab} | {_fmt(d['mark60'])} | {_fmt(d['mark240'])} | {_fmt(d['mark1440'])} | {_fmt(d['rule'])} |")
        else:
            print(f"  {lab:10} +60m {_fmt(d['mark60'])}\n             +4h  {_fmt(d['mark240'])}\n             +24h {_fmt(d['mark1440'])}\n             rule {_fmt(d['rule'])}")
    fq = res.get("follow_quotes") or {}
    print(f"\nfollow on logged quotes (venue_quotes, {fq.get('window_days')} days, take touch, exit opposite touch at +4 h): signals {fq.get('signals')}")
    if md:
        print("\n| follow, quotes | result |\n|---|---|")
    for lab in ("all", "kalshi_confirmed", "unconfirmed", "control_sig_already_followed"):
        if lab in fq:
            print(f"| {lab} | {_fmt(fq[lab])} |" if md else f"  {lab:16} {_fmt(fq[lab])}")
    print(f"\nheadline: {res['headline']}")


# ----------------------------------------------------------------------------- calendar
# (date/time ET, event, markets, expected volatility, how to position). Sources: web search 22 Oct 2026; debates for NC,
# OH, TX, NH, NE Senate and AZ, NV governor were NOT scheduled as of 20-22 Oct (Washington Post 20 Oct, Washington Times).
CALENDAR = [
    ("Oct 23 Fri 14:00", "Trump rally, Fort Myers FL", "Florida Senate / Governor, FL House seats", "low; R legs +0-1 pt", "no action"),
    ("Oct 23-30 daily", "Early-vote releases (TX, NC, GA, NV, AZ, FL)", "D legs in TX/NC/GA Senate, Governor races", "low-medium; D-favourable so far (CNN/Axios 16-20 Oct)", "follow only via outside venues"),
    ("Oct 26-30", "Texas early-vote county releases (early voting runs to Oct 30; record Harris/Dallas turnout)", "Texas Senate / Governor", "low-medium; two-sided", "follow only via outside venues"),
    ("Oct 27 Tue 13:00/18:00", "Trump rallies, Lubbock + Waco TX", "Texas Senate / Governor", "low", "no action"),
    ("Oct 27-Nov 2 (expected)", "Final polls: Marist, Emerson, Quinnipiac, AJC, NYT/Siena battlegrounds; no dates announced (2024 precedent: fielded Oct 24-Nov 2, out Nov 3)", "GA, MI, NC, OH, TX, ME, IA, AK Senate; GA/NV/AZ/WI/MI governor", "medium; 1-3 pts on Polymarket, SIG follows <20% in 4 h", "headline/outside follow with confirmation"),
    ("Oct 30 Fri", "Early voting closes in Texas and Georgia; NC Oct 31", "TX/GA/NC races", "low", "none"),
    ("Nov 1 Sun 02:00", "US clocks fall back (ET = UTC-5)", "all", "none", "time-of-day gates shift one hour"),
    ("Nov 2 Mon (expected)", "Final network polls; last Polymarket/Kalshi repricing before results", "competitive Senate/Governor", "medium", "widen MM spreads, hold directional"),
    ("Nov 3 Tue 18:00", "Polls close IN, KY (most)", "IN House seats, Kentucky Senate", "very high; favourites to 1.0 within minutes of AP calls on Kalshi/PM, SIG DMM lags 3-20 min", "election-night follow: take favourites after the call"),
    ("Nov 3 Tue 19:00", "GA, SC, VT, VA, most of FL", "Georgia Senate/Governor, SC Senate, VA House, FL Senate/Governor", "very high", "same"),
    ("Nov 3 Tue 19:30", "NC, OH, WV", "NC Senate, Ohio Senate/Governor", "very high", "same"),
    ("Nov 3 Tue 20:00", "AL, CT, DE, DC, IL, ME, MD, MA, MS, MO, NH, NJ, OK, PA, RI, TN, rest of FL/MI/TX/KS/ND/SD", "most Senate/Governor markets", "very high", "same; ME is ranked-choice (slow)"),
    ("Nov 3 Tue 20:30-21:00", "AR; then AZ, CO, KS, LA, MI, MN, NE, NM, NY, SD, TX, WI, WY", "MI/MN/NE/TX/WI Senate, AZ/KS/MI/MN/NM/WI governor", "very high", "same; AZ counts slowly"),
    ("Nov 3 Tue 22:00-23:00", "IA, MT, NV, UT; then CA, ID, OR, WA, HI", "Iowa Senate/Governor, NV Governor, CA/OR/WA House seats", "high; NV/CA/OR/WA slow counts, often not called by Nov 4 noon", "take only AP-called races"),
    ("Nov 4 Wed 01:00", "Alaska polls close", "Alaska Senate/Governor/House", "high; ranked-choice tabulation is days later", "no position: unresolved at close"),
    ("Nov 4 Wed 12:00", "SIG trading ends; settlement 17:00 UTC", "all", "-", "flatten uncalled races before close"),
]


def election_table(top: int = 25) -> list[dict]:
    """Per race: SIG favourite leg, its ask, outside fair, edge if the race is called for the favourite, DMM depth within
    10 ticks above the ask (data/books.json). Static snapshot from disk (no reads)."""
    markets = load_markets()
    try:
        books = json.load(open(os.path.join(HERE, "data", "books.json")))
        outside = load_outside()
    except OSError:
        return []
    legs = collections.defaultdict(dict)
    for ex, (race, party, _) in markets.items():
        legs[race][party] = ex
    slow = ("Alaska", "California", "Maine", "Arizona", "Nevada", "Oregon", "Washington", "AK-", "CA-", "ME-", "AZ-", "NV-", "OR-", "WA-")
    rows = []
    for race, lg in legs.items():
        if "I" in lg or race.startswith(slow):
            continue                                   # three-way races and slow-count states: not callable by Nov 4 noon
        best = None
        for party, ex in lg.items():
            b = books.get(ex) or {}
            asks = b.get("asks") or []
            if not asks:
                continue
            ask = asks[0]["price"]
            if best is None or ask > best[1]:
                best = (party, ask, ex, asks)
        if not best or best[1] < 0.55:
            continue
        party, ask, ex, asks = best
        fair = leg_fair(outside, race, party, 1e12)
        depth = sum(a["quantity"] for a in asks if a["price"] <= ask + 10 * TICK)
        rows.append(dict(race=race, party=party, sig_ask=ask, fair=fair, edge_if_called=round(1 - ask, 3), depth_10t=int(depth),
                         gain_per_10k=round(10000 / (ask + 5 * TICK) * (1 - ask - 5 * TICK))))
    rows.sort(key=lambda r: -r["gain_per_10k"] * min(1.0, r["depth_10t"] * r["sig_ask"] / 10000))
    return rows[:top]


def print_calendar():
    print("| when (ET) | event | markets | expected volatility | action |\n|---|---|---|---|---|")
    for r in CALENDAR:
        print("| " + " | ".join(r) + " |")
    rows = election_table()
    if rows:
        print("\nElection night (books.json snapshot of 21 Oct; three-way and slow-count races excluded): the pre-call favourite leg, its SIG ask,"
              " the outside fair, the gain per share if it is called, DMM depth within 10 ticks of the ask, and the SUSQies earned per 10k of"
              " cash bought 5 ticks above today's ask after an AP call that the DMM has not yet priced in")
        print("| race | leg | SIG ask | outside | gain/share | depth 10t | per 10k |\n|---|---|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['race']} | {r['party']} | {r['sig_ask']:.3f} | {'-' if r['fair'] is None else f'{r['fair']:.3f}'} | {r['edge_if_called']:.3f} | {r['depth_10t']:,} | {r['gain_per_10k']:,} |")


def main(argv=None):
    ap = argparse.ArgumentParser(description="volatile-move strategy: paper module, backtest and events calendar")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--calendar", action="store_true")
    ap.add_argument("--since", default="2026-10-06")
    ap.add_argument("--until", default=None)
    ap.add_argument("--ticks", type=int, default=5)
    ap.add_argument("--qty", type=int, default=1000)
    ap.add_argument("--outside-pts", type=float, default=0.02)
    ap.add_argument("--md", action="store_true", help="markdown tables")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.calendar:
        print_calendar()
    if a.backtest:
        res = backtest(a.since, a.until, a.ticks, a.qty, a.outside_pts)
        if a.json:
            res["fade"].pop("rows"); res["follow"].pop("rows"); res["follow_quotes"].pop("rows", None)
            print(json.dumps(res, indent=1))
        else:
            print_backtest(res, a.md)
    if not (a.calendar or a.backtest):
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
