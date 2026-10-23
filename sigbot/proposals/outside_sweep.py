"""Catch-up sweep (capped trial, Sizhe 23 Oct: "sweep immediately"): when Polymarket AND Kalshi agree on a move of 2+ pts
over the last hour and SIG has not followed, take SIG's stale touch at once and close when SIG catches up.

Evidence (research_notes/outside_stable_sweep.md, Fable double-check): waiting for the outside to "stabilise" adds nothing
(optimal wait x = 0); the edge is weak, honest estimate +0.3 tick/share (range -0.5..+1.2), so the trial is small:
300 notional per leg, at most 4 open positions, the sleeve switches itself off at -50 S, and every position is closed by
12:00 ET on 3 Nov (the owner wants all cash free for the 3 Nov punt).

Pure decision logic here (no API calls) so it can be tested offline; the Bot wires it to orders (purpose 'catchup',
exits 'catchup_exit'). Positions are reconstructed from the fills table, so a restart never loses them; entry context
(gap, outside move) is kept in a small state file, and a position without context is closed at once.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

TICK = 0.005
MOVE_MIN = 0.02            # outside fair moved >= 2 pts over the window
LEG_MIN = 0.01             # Polymarket and Kalshi each moved >= 1 pt the same way
WINDOW = 3600.0            # 60 min
WINDOW_TOL = 900.0         # the "60 min ago" observation may be 45-75 min old
AGREE = 0.04               # Polymarket and Kalshi within 4 pts (now and then)
SIG_FOLLOW_MAX = 1 / 3     # SIG moved less than a third of the outside move
GAP_MIN_TICKS = 2          # (F - SIG mid) * d >= 2 ticks
SPREAD_MAX_TICKS = 2
MID_RANGE = (0.03, 0.97)
CONVERGE = 0.70            # exit once SIG mid moved 70% of the entry gap
TIME_STOP = 12 * 3600.0
REVERT = 0.30              # safety exit: outside fell back below 30% of its move
RACE_GAP = 2 * 3600.0      # one signal per race per 2 h
LAST_ENTRY = datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc).timestamp()    # 2 Nov 23:30 ET (EST, UTC-5)
FLATTEN_AT = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc).timestamp()   # 3 Nov 11:30 ET: close everything


class CatchupSweep:
    def __init__(self, notional: float = 300.0, max_positions: int = 4, kill_pnl: float = -50.0, max_shares: int = 2000,
                 min_shares: int = 100):
        self.notional, self.max_positions, self.kill_pnl = notional, max_positions, kill_pnl
        self.max_shares, self.min_shares = max_shares, min_shares
        self.hist: dict = {}            # exchange_id -> deque[(ts, pm, kalshi, sig_mid)]
        self.last_signal: dict = {}     # race -> ts of the last signal
        self.ctx: dict = {}             # exchange_id -> entry context {ts, d, f0, f_entry, mid0, gap, move}
        self.killed = False

    # ------------------------------------------------------------------ history
    def push(self, rows: list[dict]):
        for r in rows:
            ex = r.get("exchange_id")
            if not ex or r.get("ts") is None:
                continue
            d = self.hist.setdefault(ex, deque())
            d.append((float(r["ts"]), r.get("pm"), r.get("kalshi"), r.get("sig_mid")))
            while d and d[0][0] < float(r["ts"]) - (WINDOW + 2 * WINDOW_TOL):
                d.popleft()

    def _then(self, ex: str, now: float):
        """Observation closest to now - WINDOW, within the tolerance."""
        best = None
        for obs in self.hist.get(ex, ()):
            if abs(obs[0] - (now - WINDOW)) <= WINDOW_TOL and (best is None or abs(obs[0] - (now - WINDOW)) < abs(best[0] - (now - WINDOW))):
                best = obs
        return best

    @staticmethod
    def _universe_leg(r: dict, parties_by_race: dict) -> bool:
        """Trade the D leg, or the R leg where a race lists no D (one leg per race, no double counting)."""
        p = r.get("party")
        if p == "D":
            return True
        return p == "R" and "D" not in parties_by_race.get(r.get("race"), set())

    # ------------------------------------------------------------------ entries
    def signals(self, rows: list[dict], now: float) -> list[dict]:
        """Catch-up signals on the latest directional rows (call push() first). Each: exchange, race, direction d
        (+1 = outside moved up: buy YES; -1 = moved down: buy NO), outside move, entry gap."""
        if self.killed or now > LAST_ENTRY:
            return []
        parties = {}
        for r in rows:
            parties.setdefault(r.get("race"), set()).add(r.get("party"))
        out = []
        for r in rows:
            ex, race = r.get("exchange_id"), r.get("race")
            if not ex or not self._universe_leg(r, parties):
                continue
            pm, k, mid = r.get("pm"), r.get("kalshi"), r.get("sig_mid")
            bid, ask = r.get("sig_bid"), r.get("sig_ask")
            if None in (pm, k, mid, bid, ask) or abs(pm - k) > AGREE:
                continue
            if not (MID_RANGE[0] <= mid <= MID_RANGE[1]) or (ask - bid) > SPREAD_MAX_TICKS * TICK + 1e-9:
                continue
            then = self._then(ex, now)
            if then is None or None in (then[1], then[2], then[3]) or abs(then[1] - then[2]) > AGREE:
                continue
            f_now, f_then = (pm + k) / 2, (then[1] + then[2]) / 2
            move = f_now - f_then
            if abs(move) < MOVE_MIN - 1e-9:
                continue
            d = 1 if move > 0 else -1
            if (pm - then[1]) * d < LEG_MIN - 1e-9 or (k - then[2]) * d < LEG_MIN - 1e-9:
                continue
            if (mid - then[3]) * d >= SIG_FOLLOW_MAX * abs(move):
                continue
            gap = (f_now - mid) * d
            if gap < GAP_MIN_TICKS * TICK - 1e-9:
                continue
            if now - self.last_signal.get(race, 0.0) < RACE_GAP:
                continue
            out.append({"exchange_id": ex, "race": race, "party": r.get("party"), "d": d, "move": move, "f_now": f_now,
                        "f_then": f_then, "mid": mid, "gap": gap, "bid": bid, "ask": ask})
        return out

    def size(self, sig: dict, touch_qty: float) -> tuple[str, float, int]:
        """(side, price paid per share, shares) for a signal: take the touch, never past it."""
        if sig["d"] > 0:
            side, px = "yes", sig["ask"]
        else:
            side, px = "no", round(1 - sig["bid"], 3)
        q = int(min(self.notional / max(px, 0.01), 0.8 * (touch_qty or 0), self.max_shares))
        return side, px, (q if q >= self.min_shares else 0)

    def can_enter(self, open_count: int, adds: Optional[str], net_d: float, notional: float, cap: float = 5000.0,
                  soft: float = 4800.0) -> bool:
        """Position count and partisan cap: never push net D notional past the cap; near it, only reducing trades."""
        if self.killed or open_count >= self.max_positions:
            return False
        if adds == "D":
            return net_d + notional <= cap and net_d < soft
        if adds == "R":
            return net_d - notional >= -cap and net_d > -soft
        return True

    # ------------------------------------------------------------------ exits
    def exit_reason(self, ex: str, row: Optional[dict], now: float) -> Optional[str]:
        """Why an open position on ex must be closed now, or None to keep it."""
        if now >= FLATTEN_AT:
            return "flatten before the 3 Nov punt"
        c = self.ctx.get(ex)
        if c is None:
            return "no entry context (restart)"
        if self.killed:
            return "sleeve killed"
        if now - c["ts"] >= TIME_STOP:
            return "12 h time stop"
        if row is None:
            return None
        pm, k, mid = row.get("pm"), row.get("kalshi"), row.get("sig_mid")
        if mid is not None and (mid - c["mid0"]) * c["d"] >= CONVERGE * c["gap"]:
            return "SIG caught up"
        if pm is not None and k is not None:
            if abs(pm - k) > AGREE:
                return "Polymarket and Kalshi disagree"
            if ((pm + k) / 2 - c["f_then"]) * c["d"] < REVERT * abs(c["move"]):
                return "outside move reverted"
        return None

    def check_kill(self, pnl: float) -> bool:
        if pnl <= self.kill_pnl:
            self.killed = True
        return self.killed


def pnl_from_fills(fills: list[dict], marks: dict) -> tuple[float, dict]:
    """P&L of the sleeve from its fills (YES terms: quantity signed, price YES price): cash flow -q*p plus open
    positions at marks. Returns (pnl, open positions by exchange)."""
    pos, cash, last = {}, 0.0, {}
    for f in fills:
        q, p = float(f["quantity"]), float(f["price"])
        pos[f["exchange_id"]] = pos.get(f["exchange_id"], 0.0) + q
        cash -= q * p
        last[f["exchange_id"]] = p
    open_pos = {ex: q for ex, q in pos.items() if abs(q) >= 1}
    # a position with no current mark (no direction row, one-sided book) is valued at its last fill price, never at 0:
    # valuing it at 0 tripped the sleeve's kill on a missing quote (review 23 Oct)
    value = sum(q * (marks.get(ex) if marks.get(ex) is not None else last[ex]) for ex, q in open_pos.items())
    return cash + value, open_pos


def now_ts() -> float:
    return time.time()
