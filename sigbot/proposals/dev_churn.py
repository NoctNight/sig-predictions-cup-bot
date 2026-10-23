"""Deviation churn (live, Sizhe 23 Oct): buy a sell-set only when the race is unusually cheap and sell it back once the
round trip earns 2 ticks per set, or after 24 h; the cash is then reused for the next unusually cheap set.

Evidence: research_notes/arb_convergence.md (a race's gap to payout sits around its own normal level and only the part
above that level reverts, halving in 2-5 h). Fable double-check: honest estimate +0.4k to 3 Nov (range -1k..+2k), 20%
of baskets rejected in the bot's own logs; Sizhe chose to run it live. Hence the caps: 12k pot, cash floor 3k, the sleeve
stops entering at -600 (realised + open at the touch), no entries after 2 Nov 23:00 UTC and everything sold by
3 Nov 22:30 UTC (17:30 ET), before the punt.

Scope: two-leg races only (sell-set = NO on both legs, pays 1), never Rhode Island Governor (capped by Sizhe) or any
three-leg race (several legs could resolve YES). Buy-sets are excluded (the bot's unwind closes sell-sets only).

"Unusually cheap": mid overround (sum of leg mids - 1) at least 4 ticks above the race's median over the last 24 h
(this venue's own quotes, at least 12 h of history), and the set still clears the bot's arb thresholds.
Pure logic here; the Bot wires books, orders and the ledger (params/churn_state.json).
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Optional

TICK = 0.005
DEV_MIN = 0.020                 # 4 ticks above the 24-h median
TAKE = 0.010                    # exit once the round trip earns 2 ticks per set
MAX_HOLD = 24 * 3600.0
REENTRY_GAP = 30 * 60.0         # no re-entry within 30 min of an exit in the same race
ENTRY_GAP = 5 * 60.0            # at most one entry per race per 5 min
MAX_SETS_PER_ENTRY = 2000
MIN_HISTORY = 12 * 3600.0
MIN_SAMPLES = 144
EXCLUDE = {"Rhode Island Governor"}
LAST_ENTRY = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc).timestamp()     # Sizhe: stop 2-3 days before 3 Nov
VOL_RECENT = 6 * 3600.0         # volatility pause: recent window ...
VOL_BASE = 72 * 3600.0          # ... vs the 3 days before it
VOL_PAUSE = 1.5                 # pause new entries when recent gap movement >= 1.5x normal
VOL_RESUME = 1.2                # resume only below 1.2x (no flapping)
SELL_ALL_AT = datetime(2026, 11, 3, 22, 30, tzinfo=timezone.utc).timestamp()   # 17:30 ET, before the punt


GRID = 300.0                    # 5-min sampling grid (as in the study)
STALE = 1800.0                  # a leg's last quote counts for at most 30 min (the venue_quotes heartbeat is 10 min)


def overround_series(rows, n_legs: dict, start: float, end: float) -> dict:
    """rows: (race, party, ts, mid) from venue_quotes (venue 'sig'), which stores a leg only when its quote changes plus
    a 10-min heartbeat. Each leg's last mid is carried forward and the race's mid overround (sum of leg mids - 1) is
    sampled on a 5-min grid from start to end, only at points where every leg has a quote at most 30 min old."""
    by = {}
    for race, party, ts, mid in rows:
        if race in n_legs and mid is not None and ts is not None:
            by.setdefault(race, []).append((float(ts), party, float(mid)))
    out = {}
    for race, xs in by.items():
        xs.sort()
        last, i, samples = {}, 0, []
        t = start
        while t <= end:
            while i < len(xs) and xs[i][0] <= t:
                last[xs[i][1]] = (xs[i][0], xs[i][2])
                i += 1
            if len(last) == n_legs[race] and all(t - ts_ <= STALE for ts_, _ in last.values()):
                samples.append((t, sum(m for _, m in last.values()) - 1.0))
            t += GRID
        out[race] = samples
    return out


def medians_from_series(series: dict, now: float) -> dict:
    """Median overround per race over the last 24 h, where the samples span >= 12 h and number >= 144."""
    out = {}
    for race, samples in series.items():
        xs = [(t, v) for t, v in samples if t >= now - 86400.0]
        if len(xs) >= MIN_SAMPLES and now - xs[0][0] >= MIN_HISTORY:
            out[race] = median(v for _, v in xs)
    return out


def medians_from_rows(rows, n_legs: dict, now: float) -> dict:
    return medians_from_series(overround_series(rows, n_legs, now - 86400.0, now), now)


def vol_ratio(series: dict, now: float) -> Optional[float]:
    """How much the races' gaps to payout have been moving lately: per race, the standard deviation of 5-min changes in
    the overround over the last 6 h divided by the same over the 3 days before; the median across races with enough data
    (>= 30 base and >= 20 recent changes, base movement > 0). None when too few races qualify (< 10)."""
    ratios = []
    for race, samples in series.items():
        rec, base = [], []
        for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
            if t1 - t0 > GRID + 1:
                continue
            if t1 > now - VOL_RECENT:
                rec.append(v1 - v0)
            elif t1 > now - VOL_RECENT - VOL_BASE:
                base.append(v1 - v0)
        if len(base) < 30 or len(rec) < 20:
            continue
        sb = (sum(x * x for x in base) / len(base)) ** 0.5
        sr = (sum(x * x for x in rec) / len(rec)) ** 0.5
        if sb > 0:
            ratios.append(sr / sb)
    return median(ratios) if len(ratios) >= 10 else None


def big_holding(positions: dict, exchange_ids: list, own_sets: int, limit: float) -> bool:
    """True when we already hold more than `limit` shares on any leg of the race, not counting this sleeve's own sets
    (Sizhe 24 Oct: leave races with big existing positions alone for now, a mistake there costs thousands of edge)."""
    return any(abs(positions.get(e, 0.0)) - own_sets > limit for e in exchange_ids)


def mid_overround(books: dict, exchange_ids: list) -> Optional[float]:
    s = 0.0
    for e in exchange_ids:
        b = books.get(e) or {}
        if not b.get("bids") or not b.get("asks"):
            return None
        s += (b["bids"][0]["price"] + b["asks"][0]["price"]) / 2
    return s - 1.0


class DevChurn:
    def __init__(self, capital: float = 12000.0, cash_floor: float = 3000.0, kill: float = -600.0, per_race: float = 4000.0):
        self.capital, self.cash_floor, self.kill, self.per_race = capital, cash_floor, kill, per_race
        self.lots: list = []            # {race, qty, cost (per set), ts}
        self.last_entry: dict = {}      # race -> ts
        self.last_exit: dict = {}       # race -> ts
        self.realised = 0.0
        self.killed = False
        self.vol_paused = False
        self.vol_last: Optional[float] = None

    # ------------------------------------------------------------------ state
    def to_json(self) -> dict:
        return {"lots": self.lots, "last_entry": self.last_entry, "last_exit": self.last_exit, "realised": self.realised,
                "killed": self.killed, "vol_paused": self.vol_paused}

    def load(self, d: dict):
        self.lots = [dict(l) for l in d.get("lots") or [] if l.get("qty", 0) > 0]
        self.last_entry = dict(d.get("last_entry") or {})
        self.last_exit = dict(d.get("last_exit") or {})
        self.realised = float(d.get("realised", 0.0))
        self.killed = bool(d.get("killed", False))
        self.vol_paused = bool(d.get("vol_paused", False))

    def used(self, race: Optional[str] = None) -> float:
        return sum(l["qty"] * l["cost"] for l in self.lots if race is None or l["race"] == race)

    def held(self, race: str) -> int:
        return sum(int(l["qty"]) for l in self.lots if l["race"] == race)

    # ------------------------------------------------------------------ entries
    def eligible(self, race, now: float) -> bool:
        return (len(race.exchange_ids) == 2 and race.name not in EXCLUDE and not getattr(race, "has_independent", False)
                and now <= LAST_ENTRY and not self.killed and not self.vol_paused
                and now - self.last_entry.get(race.name, 0.0) >= ENTRY_GAP
                and now - self.last_exit.get(race.name, 0.0) >= REENTRY_GAP)

    def unusually_cheap(self, ovr: Optional[float], med: Optional[float]) -> bool:
        return ovr is not None and med is not None and ovr >= med + DEV_MIN - 1e-9

    def entry_qty(self, chunk, cash: float, min_qty: int) -> int:
        """Sets to buy from the best sell-set chunk: capped by the chunk, 2,000 a time, the pot, the per-race limit and the
        cash floor; 0 when below the bot's minimum set size."""
        if chunk is None or chunk.kind != "sell_set" or chunk.cost <= 0:
            return 0
        room = min(self.capital - self.used(), self.per_race - self.used(chunk.race), cash - self.cash_floor)
        q = int(min(chunk.qty, MAX_SETS_PER_ENTRY, max(0.0, room) // chunk.cost))
        return q if q >= min_qty else 0

    def add_lot(self, race: str, qty: int, cost: float, now: float):
        self.lots.append({"race": race, "qty": int(qty), "cost": float(cost), "ts": now})
        self.last_entry[race] = now

    def update_vol(self, ratio: Optional[float]) -> Optional[str]:
        """Pause new entries when recent gap movement is >= 1.5x normal; resume below 1.2x. Returns 'paused'/'resumed' on a
        change. An unknown ratio (too little data) keeps the current state."""
        self.vol_last = ratio
        if ratio is None:
            return None
        if not self.vol_paused and ratio >= VOL_PAUSE:
            self.vol_paused = True
            return "paused"
        if self.vol_paused and ratio < VOL_RESUME:
            self.vol_paused = False
            return "resumed"
        return None

    # ------------------------------------------------------------------ exits
    def exit_reason(self, lot: dict, proceeds: Optional[float], now: float) -> Optional[str]:
        """proceeds = what selling one set back fetches now (unwind_quote, walked asks)."""
        if now >= SELL_ALL_AT:
            return "sell everything before the 3 Nov punt"
        if proceeds is not None and proceeds - lot["cost"] >= TAKE - 1e-9:
            return "round trip +2 ticks"
        if now - lot["ts"] >= MAX_HOLD:
            return "24 h maximum hold"
        return None

    def reconcile(self, race: str, actual_sets: int) -> int:
        """Shrink the race's lots (oldest first) to the sets actually held (a recycle or free unwind may have closed some).
        Returns the number of sets dropped from the ledger."""
        excess = self.held(race) - max(0, int(actual_sets))
        dropped = 0
        for l in sorted((l for l in self.lots if l["race"] == race), key=lambda l: l["ts"]):
            if excess <= 0:
                break
            take = min(excess, l["qty"])
            l["qty"] -= take
            excess -= take
            dropped += take
        self.lots = [l for l in self.lots if l["qty"] > 0]
        return dropped

    def close(self, race: str, qty: int, proceeds: float, now: float) -> float:
        """Book an exit of qty sets (oldest lots first) at proceeds per set; returns realised P&L."""
        pnl, left = 0.0, int(qty)
        for l in sorted((l for l in self.lots if l["race"] == race), key=lambda l: l["ts"]):
            if left <= 0:
                break
            take = min(left, l["qty"])
            pnl += take * (proceeds - l["cost"])
            l["qty"] -= take
            left -= take
        self.lots = [l for l in self.lots if l["qty"] > 0]
        self.realised += pnl
        self.last_exit[race] = now
        return pnl

    def check_kill(self, open_value: float) -> bool:
        """open_value = sum over lots of qty * (proceeds now - cost)."""
        if self.realised + open_value <= self.kill:
            self.killed = True
        return self.killed
