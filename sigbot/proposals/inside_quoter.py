"""Inside-spread quoting on markets where the designated market maker's (DMM) spread is 3+ ticks.

Evidence (research_notes/bot_exploits.md, 2026-10-22): the DMM rests 1,500-2,500 shares at its touch on every market
and, in the 22 Oct realtime capture, 184 of 237 markets had a DMM spread of 2+ ticks (99 at 2, 57 at 3, 28 at 4-6).
Other bots' orders placed INSIDE the DMM spread were filled 44% of the time within a median 12 s (123 of 282 such
orders in 25 min), and every aggressive-flow family on the tape has a zero or negative 30-minute markout (the takers
are uninformed: periodic 5/10/20-share loops, 1,000-lot clips, one-share probes). So a resting order one tick inside
the DMM, on a market whose DMM spread is at least 3 ticks, is first in queue at a price the DMM does not occupy and
earns (spread - 2) ticks per round trip from flow that would otherwise pay the DMM.

Rules (paper mode: `cycle()` only returns the intended orders; `--live` is deliberately not implemented here):
  * eligible market: DMM spread >= min_dmm_spread_ticks (3), DMM mid inside [min_price, max_price], not in the
    market maker's own list (pass `exclude`), partisan direction not blocked (pass `blocked` + universe);
  * bid at dmm_bid + 1 tick, ask at dmm_ask - 1 tick, size `qty` each, 120 s TTL, re-posted when the DMM touch moves;
  * per-market net inventory capped at inv_cap shares: the side that would add beyond the cap is dropped;
  * race consistency: our bid plus the other legs' best bids must stay below 1 (same guard as mm.py).

Backtest (`python3 -m sigbot.proposals.inside_quoter --backtest`): replays data/sigbot.sqlite `quotes` snapshots
(the market maker's 40 quoted markets, one row per cycle) joined to the public tape. A snapshot qualifies when both
touch sizes are DMM-sized (>= 1,500) and the spread is >= 3 ticks; a hypothetical bid/ask one tick inside is filled
by any print at or through its price before the next snapshot (we are alone at an improving price, so the whole
print up to `qty` is ours); markouts use the next prints at +5/+30 min. It also prints the live calibration: the
market maker's own level-1 fills split into improving (inside the DMM touch) and joining, with their markouts.
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
from datetime import datetime

TICK = 0.005
DMM_MIN = 1500.0
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ts(s: str) -> float:
    s = s.replace("Z", "+00:00")
    head, dot, frac = s.partition(".")
    if dot:
        tz = frac[frac.find("+"):] if "+" in frac else ""
        s = f"{head}.{frac[: len(frac) - len(tz)][:6]}{tz}"
    return datetime.fromisoformat(s).timestamp()


def _rt(p: float) -> float:
    return round(round(p / TICK) * TICK, 3)


def dmm_touch(book: dict, dmm_min: float = DMM_MIN):
    """(dmm_bid, dmm_ask) = first level on each side with at least dmm_min shares; None when absent."""
    bb = next((l["price"] for l in book.get("bids") or [] if l["quantity"] >= dmm_min), None)
    ba = next((l["price"] for l in book.get("asks") or [] if l["quantity"] >= dmm_min), None)
    return bb, ba


class InsideQuoter:
    def __init__(self, universe=None, qty: int = 100, min_dmm_spread_ticks: int = 3, min_price: float = 0.10,
                 max_price: float = 0.90, inv_cap: int = 300, ttl_sec: int = 120, max_markets: int = 60):
        self.u, self.qty, self.min_spread = universe, qty, min_dmm_spread_ticks
        self.min_price, self.max_price, self.inv_cap, self.ttl_sec, self.max_markets = min_price, max_price, inv_cap, ttl_sec, max_markets
        self.last: dict[str, tuple] = {}      # ex -> (dmm_bid, dmm_ask) last quoted against

    def cycle(self, books: dict, positions: dict, exclude: set = frozenset(), blocked: set = frozenset(),
              prices: dict | None = None) -> list[dict]:
        """Paper: returns the orders that would be sent (exchangeId, side, price, quantity, yes_price, reason)."""
        out = []
        for ex, book in books.items():
            if ex in exclude:
                continue
            bb, ba = dmm_touch(book)
            if bb is None or ba is None:
                continue
            spread = int(round((ba - bb) / TICK))
            mid = 0.5 * (bb + ba)
            if spread < self.min_spread or not (self.min_price <= mid <= self.max_price):
                self.last.pop(ex, None)
                continue
            if self.last.get(ex) == (bb, ba):
                continue                      # touch unchanged: keep queue position
            self.last[ex] = (bb, ba)
            bid_px, ask_px = _rt(bb + TICK), _rt(ba - TICK)
            if prices and self.u is not None:  # race consistency, same rule as mm.py
                race = self.u.race_of(ex)
                if race is not None:
                    others = [l.exchange_id for l in race.legs.values() if l.exchange_id != ex]
                    ob = [(prices.get(o) or {}).get("bestBid") for o in others]
                    oa = [(prices.get(o) or {}).get("bestAsk") for o in others]
                    if others and all(v is not None for v in ob):
                        bid_px = min(bid_px, _rt(1.0 - TICK - sum(ob)))
                    if others and all(v is not None for v in oa):
                        ask_px = max(ask_px, _rt(1.0 + TICK - sum(oa)))
            inv = positions.get(ex, 0.0)
            adds_yes = adds_no = None
            if self.u is not None:
                from .. import risk
                adds_yes, adds_no = risk.adds(self.u, ex, "yes"), risk.adds(self.u, ex, "no")
            if inv < self.inv_cap and adds_yes not in blocked and bid_px > bb:
                out.append(dict(exchangeId=ex, side="yes", action="buy", price=bid_px, quantity=self.qty, yes_price=bid_px,
                                reason=f"inside bid, dmm {bb}/{ba} spread {spread}t"))
            if inv > -self.inv_cap and adds_no not in blocked and ask_px < ba:
                out.append(dict(exchangeId=ex, side="no", action="buy", price=_rt(1 - ask_px), quantity=self.qty, yes_price=ask_px,
                                reason=f"inside ask, dmm {bb}/{ba} spread {spread}t"))
            if len(out) >= 2 * self.max_markets:
                break
        return out


# ----------------------------------------------------------------------------------------------- backtest
def _load_tape(db: str) -> dict:
    byx: dict = collections.defaultdict(list)
    path = os.path.join(HERE, "data", "tapes.json")
    seen = set()
    if os.path.exists(path):
        for ex, v in json.load(open(path)).items():
            for t in v.get("data", []):
                seen.add(str(t["id"]))
                # tapes.json `side`: YES = aggressor bought YES (hit the ask), NO = aggressor bought NO (hit the bid)
                byx[ex].append((_ts(t["createdAt"]), float(t["price"]), float(t["size"]), 1 if t.get("side") == "YES" else -1))
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for tid, ex, p, q, at, agg in c.execute("SELECT trade_id, exchange_id, price, quantity, executed_at, aggressor FROM tape"):
        if tid in seen:
            continue
        byx[ex].append((_ts(at), float(p), float(q), 1 if agg == "buy" else (-1 if agg == "sell" else 0)))
    for ex in byx:
        byx[ex].sort()
    return byx


def backtest(db: str, qty: int, min_spread: int, hold_sec: int = 1800, min_price: float = 0.10, max_price: float = 0.90) -> dict:
    tape = _load_tape(db)
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute("SELECT ts, exchange_id, best_bid, best_ask, bid_qty, ask_qty FROM quotes ORDER BY exchange_id, ts").fetchall()
    by: dict = collections.defaultdict(list)
    for r in rows:
        by[r[1]].append(r)
    fills = []        # (ex, ts, side, price, qty, mo5, mo30)
    snaps = quals = 0
    for ex, qs in by.items():
        trs = tape.get(ex, [])
        times = [t[0] for t in trs]
        for a, b in zip(qs, qs[1:]):
            snaps += 1
            ts, bb, ba, qb, qa = a[0], a[2], a[3], a[4] or 0, a[5] or 0
            if None in (bb, ba) or qb < DMM_MIN or qa < DMM_MIN:
                continue
            spread = int(round((ba - bb) / TICK))
            mid = 0.5 * (bb + ba)
            if spread < min_spread or not (min_price <= mid <= max_price):
                continue
            quals += 1
            end = min(b[0], ts + 120)
            bid_px, ask_px = _rt(bb + TICK), _rt(ba - TICK)
            i, j = bisect.bisect_left(times, ts), bisect.bisect_right(times, end)
            rem_b = rem_a = qty
            for (tt, p, q, agg) in trs[i:j]:
                if rem_b > 0 and p <= bid_px + 1e-9 and agg <= 0:      # seller hit the bids at or through our price
                    take = min(rem_b, q); rem_b -= take
                    fills.append([ex, tt, "yes", bid_px, take])
                if rem_a > 0 and p >= ask_px - 1e-9 and agg >= 0:      # buyer lifted the asks at or through our price
                    take = min(rem_a, q); rem_a -= take
                    fills.append([ex, tt, "no", ask_px, take])
        # markouts
    for f in fills:
        ex, tt, side, px, q = f
        trs = tape[ex]; times = [t[0] for t in trs]
        mos = []
        for h in (300, 1800):
            k = bisect.bisect_right(times, tt + h) - 1
            p = trs[k][1] if k >= 0 else px
            mos.append(((p - px) if side == "yes" else (px - p)) / TICK)
        f.extend(mos)
    days = max(1e-9, (max(r[0] for r in rows) - min(r[0] for r in rows)) / 86400) if rows else 1
    out = {"snapshots": snaps, "qualifying_snapshots": quals, "fills": len(fills), "shares": sum(f[4] for f in fills), "days": round(days, 2)}
    if fills:
        out["markout_5m_ticks"] = round(statistics.mean(f[5] for f in fills), 2)
        out["markout_30m_ticks"] = round(statistics.mean(f[6] for f in fills), 2)
        out["shares_per_day"] = round(out["shares"] / days)
        out["susqies_per_day_at_30m_markout"] = round(out["shares"] / days * out["markout_30m_ticks"] * TICK, 1)
        out["markets_with_fills"] = len({f[0] for f in fills})
        bys = collections.defaultdict(list)
        for f in fills:
            bys[f[2]].append(f[6])
        out["by_side_30m"] = {s: (round(statistics.mean(v), 2), len(v)) for s, v in bys.items()}
    # live calibration: the market maker's own level-1 fills, improving vs joining
    cal = collections.defaultdict(list)
    for ex, side, price, qn, at, mid_at_place, ticks in c.execute(
            "SELECT exchange_id, side, price, quantity, filled_at, mid_at_place, ticks_from_mid FROM fills WHERE purpose='mm' AND level=1 AND price IS NOT NULL"):
        trs = tape.get(ex, [])
        if not trs or mid_at_place is None:
            continue
        times = [t[0] for t in trs]
        tt = _ts(at)
        k = bisect.bisect_right(times, tt + 1800) - 1
        p = trs[k][1] if k >= 0 else price
        mo = ((p - price) if side == "yes" else (price - p)) / TICK
        improving = ticks is not None and ticks < 0.99       # strictly inside the touch (half-spread > 1 tick)
        cal["improving" if improving else "joining"].append((mo, abs(qn)))
    out["live_level1_calibration"] = {k: {"fills": len(v), "shares": round(sum(q for _, q in v)), "markout_30m_ticks": round(statistics.mean(m for m, _ in v), 2)}
                                      for k, v in cal.items()}
    return out


def backtest_capture(path: str, qty: int, min_spread: int, min_price: float = 0.10, max_price: float = 0.90) -> dict:
    """Replay a realtime capture (data/feed_capture_*.jsonl: every book change for all 237 markets plus every public
    print). The DMM touch is read from the book itself (first level with >= 1,500 shares per side), so our own and
    other bots' inside orders do not contaminate it. A hypothetical bid/ask one tick inside the DMM touch, re-posted
    whenever the DMM touch moves, is filled by any print at or through its price (we are alone at an improving price
    until another bot joins; prints by other inside orders at our price are counted as ours up to `qty`, which is
    optimistic by their share of the queue). Markouts at +5 min use later prints in the capture."""
    books: dict = collections.defaultdict(list); trades: dict = collections.defaultdict(list)
    for line in open(path):
        r = json.loads(line)
        if r["t"] == "book":
            books[r["ex"]].append(r)
        else:
            trades[r["ex"]].append((_ts(r["at"]) if r.get("at") else r["rx"], float(r["price"]), float(r["qty"])))
    t0 = min(r["rx"] for v in books.values() for r in v); t1 = max(r["rx"] for v in books.values() for r in v)
    fills = []; eligible = set(); inside_prints = 0; inside_shares = 0.0
    for ex, snaps in books.items():
        trs = sorted(trades.get(ex, [])); times = [t[0] for t in trs]
        for a, b in zip(snaps, snaps[1:] + [None]):
            bb, ba = dmm_touch({"bids": [{"price": p, "quantity": q} for p, q in a["bids"]], "asks": [{"price": p, "quantity": q} for p, q in a["asks"]]})
            if bb is None or ba is None:
                continue
            spread = int(round((ba - bb) / TICK)); mid = 0.5 * (bb + ba)
            if spread < min_spread or not (min_price <= mid <= max_price):
                continue
            eligible.add(ex)
            end = b["rx"] if b else t1
            bid_px, ask_px = _rt(bb + TICK), _rt(ba - TICK)
            i, j = bisect.bisect_left(times, a["rx"]), bisect.bisect_right(times, end)
            rem_b = rem_a = qty
            for (tt, p, q) in trs[i:j]:
                if bb + 1e-9 < p < ba - 1e-9:
                    inside_prints += 1; inside_shares += q
                if rem_b > 0 and p <= bid_px + 1e-9 and p > bb + 1e-9:   # a print at our improving bid (sellers hitting it)
                    take = min(rem_b, q); rem_b -= take; fills.append([ex, tt, "yes", bid_px, take])
                elif rem_a > 0 and p >= ask_px - 1e-9 and p < ba - 1e-9:
                    take = min(rem_a, q); rem_a -= take; fills.append([ex, tt, "no", ask_px, take])
    for f in fills:
        trs = sorted(trades[f[0]]); times = [t[0] for t in trs]
        k = bisect.bisect_right(times, f[1] + 300) - 1
        p = trs[k][1] if k >= 0 else f[3]
        f.append(((p - f[3]) if f[2] == "yes" else (f[3] - p)) / TICK)
    days = (t1 - t0) / 86400
    out = {"window_min": round((t1 - t0) / 60, 1), "eligible_markets": len(eligible), "inside_prints_on_eligible": inside_prints,
           "inside_shares_on_eligible": round(inside_shares), "fills": len(fills), "shares": round(sum(f[4] for f in fills))}
    if fills:
        out["markout_5m_ticks"] = round(statistics.mean(f[5] for f in fills), 2)
        out["shares_per_day"] = round(out["shares"] / days)
        out["susqies_per_day_at_5m_markout"] = round(out["shares"] / days * out["markout_5m_ticks"] * TICK, 1)
        out["half_spread_captured_per_day_if_flat"] = round(sum(f[4] for f in fills) / days * 0.5 * TICK * 1, 1)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="inside-spread quoter (paper) and tape backtest")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--db", default=os.path.join(HERE, "data", "sigbot.sqlite"))
    ap.add_argument("--qty", type=int, default=100)
    ap.add_argument("--min-spread", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--capture", default=None, help="replay a realtime capture (data/feed_capture_*.jsonl) instead of the quotes table")
    a = ap.parse_args(argv)
    if a.backtest:
        res = backtest_capture(a.capture, a.qty, a.min_spread) if a.capture else backtest(a.db, a.qty, a.min_spread)
        print(json.dumps(res, indent=1) if a.json else "\n".join(f"{k}: {v}" for k, v in res.items()))
        return
    # paper cycle from the latest feed books is the main session's call; show eligibility from data/books.json
    books = json.load(open(os.path.join(HERE, "data", "books.json")))
    q = InsideQuoter(None, a.qty, a.min_spread)
    orders = q.cycle(books, {})
    print(f"eligible markets in data/books.json snapshot: {len({o['exchangeId'] for o in orders})}; orders: {len(orders)}")
    for o in orders[:20]:
        print("  ", o)


if __name__ == "__main__":
    main(sys.argv[1:])
