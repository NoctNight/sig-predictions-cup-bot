"""Catch-up sweep (capped trial, Sizhe 23 Oct: "sweep immediately"): when Polymarket AND Kalshi agree on a move of 2+ pts
over the last hour and SIG has not followed, take SIG's stale touch at once and close when SIG catches up.

Evidence (research_notes/outside_stable_sweep.md, Fable double-check): waiting for the outside to "stabilise" adds nothing
(optimal wait x = 0); the edge is weak, honest estimate +0.3 tick/share (range -0.5..+1.2). It started as a small trial
(300 notional, 4 positions). 31 Oct (research_notes/dir_alpha/FINDINGS.md): this is the lag trade, the one directional
signal that held on unseen days (+0.4c slow, +0.6c fast after spread), so the sleeve now follows the research:
  - entries are tagged fast (both venues moved 1.5c+ in 30 min, SIG copied under half, leg 25-75c) or slow (the rest);
  - exits: no convergence exit (it cut the winners); fast sold by 8 h, slow by 20 h, slow takes profit at +3c, fast never;
    no hard stop on losers (a flat-or-down trade at 12 h still expects more); the outside-reverted and venues-disagree
    exits stay; everything is closed by 11:30 ET on 3 Nov (the owner wants all cash free for the 3 Nov punt);
  - size 2.5k a trade, tilted up to 5k on conviction points and for legs 2c+ on the right side of the market maker's line,
    built in repeat clips (each clip is at most 0.8 of the touch); slow entries with our own side at 70c+ are skipped.

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
# exits (31 Oct, dir_alpha FINDINGS "Exit rule"): fast peaks at 8 h and fades, slow is done by about 20 h; the 70% convergence exit is gone
TIME_STOP_FAST = 8 * 3600.0
TIME_STOP_SLOW = 20 * 3600.0
TIME_STOP_LEGACY = 12 * 3600.0   # a position whose context was written by the old code (no kind): its old 12 h stop
TAKE_PROFIT_SLOW = 0.03    # slow only: past +3c the expected further gain is below zero; fast never takes profit (1c+ at 8 h still pays)
REVERT = 0.30              # safety exit: outside fell back below 30% of its move
# fast tag: both venues moved 1.5c+ the same way in 30 min, SIG copied under half of that, leg priced 25-75
FAST_WINDOW = 1800.0
FAST_TOL = 600.0
FAST_LEG_MOVE = 0.015
FAST_FOLLOW_MAX = 0.5
FAST_MID_RANGE = (0.25, 0.75)
SLOW_OWN_MAX = 0.70        # slow: skip when the side we would buy costs 70c+ (13 trades -0.1c, rest +2.0c)
# conviction points, one each (nothing fitted): no public polls, toss-up rating, leg quote unchanged 2 h+, signal in the upper two thirds
UNCHANGED_SECS = 2 * 3600.0
UPPER_GAP = 0.0218         # slow: the leg's distance beyond the market maker's line (the gap less its built-in compression) in the upper two thirds
UPPER_MOVE = 0.0228        # fast: 30-min outside move in the upper two thirds of fast signals
# the market maker prices about 0.92 of the way from 50c to the outside; a leg 2c+ on the right side of that line starts better
MM_K = 0.92
LINE_RIGHT = 0.02
# size: base notional a trade, x1.4 for 2 points, x2 for 3+, x1.4 on the right side of the line, never past NOTIONAL_MAX;
# a fast signal under 2.3c (smallest third) gets 0.6 of the base
SIZE_MULT_PTS = {0: 1.0, 1: 1.0, 2: 1.4}
SIZE_MULT_PTS3 = 2.0
SIZE_MULT_RIGHT = 1.4
FAST_SMALL = 0.023
FAST_SMALL_MULT = 0.6
BUILD_WINDOW_FAST = 20 * 60.0   # clips keep coming for this long after the first (fast decays: 15 min late +1.3c, 60 min late +0.5c)
BUILD_WINDOW_SLOW = 60 * 60.0
CLIP_SLIPPAGE = 0.01       # no clip when the side costs more than 1c above the first clip
CLIP_WAIT = 90.0           # next clip waits for the last order to settle (or this long, so a lagging fill sync cannot double up)
PARTISAN_MARGIN = 200.0    # D/R-adding clips leave this much room under the partisan cap (the old soft limit of 4,800 at a 5,000 cap)
RACE_GAP = 2 * 3600.0      # one signal per race per 2 h
LAST_ENTRY = datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc).timestamp()    # 2 Nov 23:30 ET (EST, UTC-5)
FLATTEN_AT = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc).timestamp()   # 3 Nov 11:30 ET: close everything


class CatchupSweep:
    def __init__(self, notional: float = 2500.0, max_positions: int = 6, kill_pnl: float = -50.0, max_shares: int = 5000,
                 min_shares: int = 100, max_notional: float = 5000.0, max_total_shares: int = 15000,
                 max_open_notional: float = 15000.0):
        self.notional, self.max_positions, self.kill_pnl = notional, max_positions, kill_pnl
        self.max_notional = max(notional, max_notional)    # the tilt never takes a trade past this
        self.max_shares, self.min_shares = max_shares, min_shares    # per clip; one clip is also at most 0.8 of the touch
        self.max_total_shares = max_total_shares           # per trade: the edge covers cost to about 15k shares
        self.max_open_notional = max_open_notional         # cost of everything the sleeve holds or has on order
        self.hist: dict = {}            # exchange_id -> deque[(ts, pm, kalshi, sig_mid)]
        self.chg: dict = {}             # exchange_id -> [sig_mid, ts it last changed (or was first seen)]
        self.inputs: dict = {}          # race -> {"n_polls", "rating_margin"} from params/fair_model.json (conviction points)
        self.last_signal: dict = {}     # race -> ts of the last signal
        self.ctx: dict = {}             # exchange_id -> entry context {ts, d, f_then, move, mid0, gap, kind, target, ordered, ...}
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
            mid = r.get("sig_mid")
            if mid is not None:
                c = self.chg.get(ex)
                if c is None or abs(mid - c[0]) > 1e-9:
                    self.chg[ex] = [float(mid), float(r["ts"])]

    def _then(self, ex: str, now: float, window: float = WINDOW, tol: float = WINDOW_TOL):
        """Observation closest to now - window, within the tolerance."""
        best = None
        for obs in self.hist.get(ex, ()):
            if abs(obs[0] - (now - window)) <= tol and (best is None or abs(obs[0] - (now - window)) < abs(best[0] - (now - window))):
                best = obs
        return best

    def unchanged_for(self, ex: str, now: float) -> float:
        """Seconds SIG's mid on ex has not changed (since first seen when no change was seen: a short history reads short)."""
        c = self.chg.get(ex)
        return 0.0 if c is None else max(0.0, now - c[1])

    @staticmethod
    def _universe_leg(r: dict, parties_by_race: dict) -> bool:
        """Trade the D leg, or the R leg where a race lists no D (one leg per race, no double counting)."""
        p = r.get("party")
        if p == "D":
            return True
        return p == "R" and "D" not in parties_by_race.get(r.get("race"), set())

    # ------------------------------------------------------------------ entries
    def points(self, race: str, kind: str, unchanged: float, gap: float, move30: Optional[float]) -> int:
        """Conviction points, one each: no public polls, toss-up rating, SIG's quote unchanged 2 h+, signal in the upper two thirds.
        gap (slow) is the distance beyond the market maker's line, (line - mid) * d: research measured the gap against its own
        48 h norm, and the raw gap carries the maker's compression (2.4c at an outside of 80c) towards the favourite for free."""
        ip = self.inputs.get(race) or {}
        pts = 1 if ip.get("n_polls") == 0 else 0
        rm = ip.get("rating_margin")
        pts += 1 if rm is not None and abs(rm) <= 2 else 0
        pts += 1 if unchanged >= UNCHANGED_SECS else 0
        if kind == "fast":
            pts += 1 if move30 is not None and abs(move30) >= UPPER_MOVE - 1e-9 else 0
        else:
            pts += 1 if gap >= UPPER_GAP - 1e-9 else 0
        return pts

    def signals(self, rows: list[dict], now: float) -> list[dict]:
        """Catch-up signals on the latest directional rows (call push() first). Each: exchange, race, direction d
        (+1 = outside moved up: buy YES; -1 = moved down: buy NO), outside move, entry gap, kind (fast / slow), conviction
        points, whether the leg is 2c+ on the right side of the market maker's line."""
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
            # fast: both venues moved 1.5c+ in the last 30 min, SIG copied under half of that, leg 25-75
            kind, move30 = "slow", None
            t30 = self._then(ex, now, FAST_WINDOW, FAST_TOL)
            if t30 is not None and None not in (t30[1], t30[2], t30[3]) and abs(t30[1] - t30[2]) <= AGREE:
                move30 = f_now - (t30[1] + t30[2]) / 2
                if (pm - t30[1]) * d >= FAST_LEG_MOVE - 1e-9 and (k - t30[2]) * d >= FAST_LEG_MOVE - 1e-9 \
                        and FAST_MID_RANGE[0] <= mid <= FAST_MID_RANGE[1] and (mid - t30[3]) * d < FAST_FOLLOW_MAX * move30 * d:
                    kind = "fast"
            own = ask if d > 0 else 1 - bid
            if kind == "slow" and own >= SLOW_OWN_MAX - 1e-9:
                continue
            line = 0.5 + MM_K * (f_now - 0.5)
            line_dist = (line - mid) * d
            right = line_dist >= LINE_RIGHT - 1e-9
            out.append({"exchange_id": ex, "race": race, "party": r.get("party"), "d": d, "move": move, "f_now": f_now,
                        "f_then": f_then, "mid": mid, "gap": gap, "bid": bid, "ask": ask, "kind": kind, "right": right,
                        "move30": move30, "pts": self.points(race, kind, self.unchanged_for(ex, now), line_dist, move30)})
        return out

    def target_notional(self, sig: dict) -> float:
        """Trade size in dollars: the base, tilted up by conviction points and by a start 2c+ on the right side of the
        market maker's line, never past max_notional; a fast signal in the smallest third gets less."""
        pts = int(sig.get("pts") or 0)
        m = SIZE_MULT_PTS3 if pts >= 3 else SIZE_MULT_PTS.get(pts, 1.0)
        if sig.get("right"):
            m *= SIZE_MULT_RIGHT
        if sig.get("kind") == "fast" and sig.get("move30") is not None and abs(sig["move30"]) < FAST_SMALL - 1e-9:
            m *= FAST_SMALL_MULT
        return min(self.notional * m, self.max_notional)

    def size(self, sig: dict, touch_qty: float, remaining: Optional[float] = None) -> tuple[str, float, int]:
        """(side, price paid per share, shares) for one clip: take the touch, never past it (at most 0.8 of what shows). The
        first clip is sized by the trade's target; later clips by `remaining` shares."""
        if sig["d"] > 0:
            side, px = "yes", sig["ask"]
        else:
            side, px = "no", round(1 - sig["bid"], 3)
        want = self.target_shares(sig, px) if remaining is None else remaining
        q = int(min(want, 0.8 * (touch_qty or 0), self.max_shares))
        return side, px, (q if q >= self.min_shares else 0)

    def target_shares(self, sig: dict, px: float) -> int:
        return int(min(self.target_notional(sig) / max(px, 0.01), self.max_total_shares))

    @staticmethod
    def room(adds: Optional[str], net_d: float, cap: float = 5000.0, margin: float = PARTISAN_MARGIN) -> float:
        """Dollars a clip may add on the `adds` side before net D notional reaches the cap less the margin (the old soft
        limit of 4,800 at 5,000). Exits never call this: only adds are limited."""
        if adds == "D":
            return max(0.0, cap - margin - net_d)
        if adds == "R":
            return max(0.0, cap - margin + net_d)
        return float("inf")

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
    def time_stop(self, c: dict) -> float:
        k = c.get("kind")
        return TIME_STOP_FAST if k == "fast" else TIME_STOP_SLOW if k == "slow" else TIME_STOP_LEGACY

    def exit_reason(self, ex: str, row: Optional[dict], now: float, avg_yes: Optional[float] = None, qty: float = 0.0) -> Optional[str]:
        """Why an open position on ex must be closed now, or None to keep it. avg_yes: the position's average YES price and
        qty its signed YES quantity (for the slow take-profit; both unknown -> no take-profit)."""
        if now >= FLATTEN_AT:
            return "flatten before the 3 Nov punt"
        c = self.ctx.get(ex)
        if c is None:
            return "no entry context (restart)"
        if self.killed:
            return "sleeve killed"
        held = now - c["ts"]
        if held >= self.time_stop(c):
            return "%d h time stop (%s)" % (round(self.time_stop(c) / 3600), c.get("kind") or "legacy")
        if row is None:
            return None
        pm, k = row.get("pm"), row.get("kalshi")
        if c.get("kind") == "slow" and avg_yes is not None and qty:
            bid, ask = row.get("sig_bid"), row.get("sig_ask")
            val = None
            if qty > 0 and bid is not None:
                val = bid - avg_yes
            elif qty < 0 and ask is not None:
                val = avg_yes - ask
            if val is not None and val >= TAKE_PROFIT_SLOW - 1e-9:
                return "slow take-profit +%.1fc" % (100 * val)
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


def rows_from_venue_quotes(vq_rows, leg_of: dict) -> list[dict]:
    """Rebuild direction-style rows from stored quotes so the 60-min history survives a restart. vq_rows: (ts, venue,
    race, party, mid) with venue in polymarket / kalshi / sig, written on change plus a 10-min heartbeat; leg_of:
    (race, party) -> SIG exchange id. Each (race, party, venue) value is carried forward to every later poll time."""
    by_ts: dict = {}
    for ts, venue, race, party, mid in vq_rows:
        if (race, party) in leg_of and mid is not None:
            by_ts.setdefault(float(ts), []).append((venue, race, party, float(mid)))
    last: dict = {}
    out = []
    for ts in sorted(by_ts):
        for venue, race, party, mid in by_ts[ts]:
            last[(race, party, venue)] = (ts, mid)
        for (race, party), ex in leg_of.items():
            v = {}
            for venue in ("polymarket", "kalshi", "sig"):
                tv = last.get((race, party, venue))
                v[venue] = tv[1] if tv is not None and ts - tv[0] <= 1800 else None
            if v["polymarket"] is None and v["kalshi"] is None and v["sig"] is None:
                continue
            out.append({"ts": ts, "exchange_id": ex, "race": race, "party": party, "pm": v["polymarket"], "kalshi": v["kalshi"],
                        "sig_mid": v["sig"]})
    return out


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


def avg_yes_from_fills(fills: list[dict]) -> dict:
    """Average YES price of each open position, from the fills in order (YES terms, as pnl_from_fills): adds weight the price,
    reductions keep the average, a flip restarts it at the flipping fill's price."""
    pos, cost = {}, {}
    for f in fills:
        ex, q, p = f["exchange_id"], float(f["quantity"]), float(f["price"])
        cur = pos.get(ex, 0.0)
        if cur == 0 or (cur > 0) == (q > 0):
            cost[ex] = cost.get(ex, 0.0) + q * p
            pos[ex] = cur + q
        elif abs(q) <= abs(cur):
            cost[ex] = cost[ex] * (1 - abs(q) / abs(cur))
            pos[ex] = cur + q
        else:                                      # flipped through zero
            pos[ex] = cur + q
            cost[ex] = pos[ex] * p
    return {ex: cost[ex] / q for ex, q in pos.items() if abs(q) >= 1}


def inputs_from_model(model: dict) -> dict:
    """race -> {"n_polls", "rating_margin"} from params/fair_model.json (its "races" mapping), for the conviction points."""
    out = {}
    for race, v in ((model or {}).get("races") or {}).items():
        ip = (v or {}).get("inputs") or {}
        out[race] = {"n_polls": ip.get("n_polls"), "rating_margin": ip.get("rating_margin")}
    return out


def now_ts() -> float:
    return time.time()
