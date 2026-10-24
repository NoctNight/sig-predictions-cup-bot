"""Shock mode (Sizhe 24 Oct: "exempt purpose='shock' trades from the 5,000 partisan cap; build shock mode under the usual
gates"). When Polymarket AND Kalshi both jump >= 5 pts on a race and SIG's designated market maker has not followed,
take SIG's stale side on both legs at once, walking the ladder by the size of the OUTSIDE move, and sell back within
~60 min as the DMM walks its own ladder after the outside.

Evidence (research_notes/shock_playbook.md section 0, Fable-checked): on 5+ pt both-venue moves SIG copied ~70% within
10 min with no trades on the tape; entering at the first poll after the move and selling at 60 min earned +3.2 pts/share
(+2.2 at 2 min, +1.6 at 5 min; n 5, MT-01 dominant). Single-venue moves are not copied (8% at 60 min), so both venues
must agree. Walking floor((M - 2.5) / 2) ticks (max 4), M = outside move in pts, was the best rule on a replay with real
SIG quotes (+1.4k S on six events); walking by the SIG-outside GAP (which includes SIG's static compression) lost -6.5k on
MI-07. Alaska was never followed: excluded.

Pure decision logic (no API calls) so it can be tested offline; the Bot wires it to orders (purposes 'shock' and
'shock_exit'). Positions come from the fills table, so a restart cannot lose them; entry context lives in a small state
file, and a position without context is closed at once.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone
from statistics import median
from typing import Optional

TICK = 0.005
MOVE_MIN = 0.05            # blend moved >= 5 pts against its reference
LEG_MIN = 0.03             # Polymarket and Kalshi each moved >= 3 pts the same way
AGREE = 0.04               # Polymarket and Kalshi within 4 pts at entry
DISAGREE_EXIT = 0.08       # after entry: venues > 8 pts apart = one venue is wrong, close
REF_FROM, REF_TO = 45 * 60.0, 15 * 60.0     # reference window: 45 to 15 min ago (the jump is the last 15 min)
REF_MIN_OBS = 3
SIG_FOLLOW_MAX = 1 / 3     # SIG has copied less than a third of the move
GAP_MIN = 0.03             # ... AND the outside is still >= 3 pts beyond SIG's mid our way: an outside move that only
                           # closes SIG's standing gap (a longshot rising to SIG's overpriced level) has nothing to capture
DEADBAND = 0.025           # the replay's walk rule: floor((M - 2.5 pts) / 2) ticks
WALK_MAX_TICKS = 4
SPREAD_MAX_TICKS = 4
MID_RANGE = (0.03, 0.97)
ADD_WINDOW = 30 * 60.0     # pyramid while the outside keeps moving, for 30 min after onset
ADD_STOP_COPY = 0.5        # ... and only while SIG has copied < 50% of the move
CONVERGE = 0.70            # exit once SIG's mid has copied 70% of the move
CONVERGE_WITH_MAKER = 0.85 # with the R3 maker exit resting at the 70% price, the taker waits until 85%
EXIT_AFTER = 60 * 60.0     # time stop: the edge is gone by ~60 min (and a big move can reverse within 12 h: MT-01)
HOAX_WINDOW = 30 * 60.0    # the outside gives back more than half the move within 30 min: exit everything
HOAX_GIVEBACK = 0.5
EXIT_WALK_TICKS = 2        # exits cross up to 2 ticks behind the touch (positions are many touches deep)
FLATTEN_WALK_TICKS = 10    # from 3 Nov 11:30 ET: walk 10 ticks so a 40k position is flat by 12:00 ET
RACE_GAP = 6 * 3600.0      # one shock per race per 6 h
EXCLUDE_PREFIX = ("Alaska",)
LAST_ENTRY = datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc).timestamp()    # 2 Nov 23:30 ET (EST, UTC-5)
FLATTEN_AT = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc).timestamp()   # 3 Nov 11:30 ET: close everything (flat by 12:00 ET)
HIST_KEEP = 3 * 3600.0
SHARE_PRICE_FLOOR = 0.40   # size shares as if the leg cost at least this
FAST_TRIGGER = 0.03        # fast detector: a Polymarket midpoint 3+ pts off its value 5-15 min ago -> full two-venue poll now
FAST_FROM, FAST_TO = 15 * 60.0, 5 * 60.0
FULL_MIN_GAP = 15.0        # triggered full polls at most every 15 s (shared Kalshi/Polymarket clients and breakers)


class FastDetector:
    """Cheap first look between full polls: per leg, the Polymarket midpoint sum (all mapped tokens) every few seconds.
    It never trades; it only says when to run the full Polymarket + Kalshi poll that shock mode trades on."""

    def __init__(self):
        self.hist: dict = {}        # exchange_id -> deque[(ts, pm midpoint sum)]

    def update(self, mids: dict, now: float) -> list:
        """mids: {exchange_id: Polymarket midpoint sum}. Returns legs whose midpoint is >= FAST_TRIGGER off every value
        seen 5-15 min ago (a jump, not drift)."""
        hot = []
        for ex, m in mids.items():
            d = self.hist.setdefault(str(ex), deque())
            old = [v for t, v in d if now - FAST_FROM <= t <= now - FAST_TO]
            if old and min(abs(m - v) for v in old) >= FAST_TRIGGER - 1e-9:
                hot.append(str(ex))
            d.append((now, m))
            while d and d[0][0] < now - FAST_FROM - 60:
                d.popleft()
        return hot


def walk_ticks(move_abs: float) -> int:
    """Ladder depth (ticks behind the touch) worth paying for an outside move of move_abs (price units)."""
    return max(0, min(WALK_MAX_TICKS, int(math.floor((move_abs - DEADBAND) / (2 * TICK) + 1e-9))))


class ShockMode:
    def __init__(self, per_shock: float = 20000.0, total: float = 20000.0, kill_pnl: float = -1500.0, max_races: int = 1,
                 max_order_shares: int = 60000, min_shares: int = 100):
        self.per_shock, self.total, self.kill_pnl = per_shock, total, kill_pnl
        self.max_races, self.max_order_shares, self.min_shares = max_races, max_order_shares, min_shares
        self.hist: dict = {}            # exchange_id -> deque[(ts, pm, kalshi, sig_mid)]
        self.last_shock: dict = {}      # race -> onset ts of its last shock
        self.ctx: dict = {}             # exchange_id -> {ts, race, d, f_ref, s_ref, move, spent}
        self.netted: dict = {}          # exchange_id -> YES-signed shares of the ledger the venue netted against other
        self.killed = False             # sleeves' inventory on the same leg (closing them would flip the account)

    # ------------------------------------------------------------------ history
    def push(self, rows: list[dict]):
        for r in rows:
            ex = r.get("exchange_id")
            if not ex or r.get("ts") is None:
                continue
            d = self.hist.setdefault(str(ex), deque())
            if d and float(r["ts"]) <= d[-1][0]:
                continue                                   # same poll pushed twice
            d.append((float(r["ts"]), r.get("pm"), r.get("kalshi"), r.get("sig_mid")))
            while d and d[0][0] < float(r["ts"]) - HIST_KEEP:
                d.popleft()

    def reference(self, ex: str, now: float) -> Optional[tuple]:
        """(pm, kalshi, blend, sig_mid) medians over 45-15 min ago; None with too few observations."""
        obs = [o for o in self.hist.get(str(ex), ()) if now - REF_FROM <= o[0] <= now - REF_TO
               and None not in (o[1], o[2], o[3])]
        if len(obs) < REF_MIN_OBS:
            return None
        pm, k, s = median(o[1] for o in obs), median(o[2] for o in obs), median(o[3] for o in obs)
        return pm, k, (pm + k) / 2, s

    # ------------------------------------------------------------------ entries
    @staticmethod
    def excluded(race: Optional[str]) -> bool:
        return not race or any(race.startswith(p) for p in EXCLUDE_PREFIX)

    def signals(self, rows: list[dict], now: float, open_races: set) -> list[dict]:
        """New shocks on the latest rows (call push() first): one per leg, D and R legs of a race both qualify. Each:
        exchange, race, d (+1 = outside up: buy YES; -1 = down: buy NO), move (blend change, signed), f_ref, s_ref."""
        if self.killed or now > LAST_ENTRY:
            return []
        out = []
        for r in rows:
            ex, race, party = r.get("exchange_id"), r.get("race"), r.get("party")
            if not ex or party not in ("D", "R") or self.excluded(race):
                continue
            if race not in open_races and len(open_races) >= self.max_races:
                continue
            if race not in open_races and now - self.last_shock.get(race, -1e18) < RACE_GAP:
                continue
            if str(ex) in self.ctx:
                continue                                   # already in: adds go through add_size()
            pm, k, mid, bid, ask = r.get("pm"), r.get("kalshi"), r.get("sig_mid"), r.get("sig_bid"), r.get("sig_ask")
            if None in (pm, k, mid, bid, ask) or abs(pm - k) > AGREE + 1e-9:
                continue
            if not (MID_RANGE[0] <= mid <= MID_RANGE[1]) or ask - bid > SPREAD_MAX_TICKS * TICK + 1e-9:
                continue
            ref = self.reference(ex, now)
            if ref is None:
                continue
            move = (pm + k) / 2 - ref[2]
            if abs(move) < MOVE_MIN - 1e-9:
                continue
            d = 1 if move > 0 else -1
            if (pm - ref[0]) * d < LEG_MIN - 1e-9 or (k - ref[1]) * d < LEG_MIN - 1e-9:
                continue
            if (mid - ref[3]) * d >= SIG_FOLLOW_MAX * abs(move):
                continue
            gap = ((pm + k) / 2 - mid) * d
            if gap < GAP_MIN - 1e-9:
                continue
            out.append({"exchange_id": str(ex), "race": race, "party": party, "d": d, "move": move, "f_ref": ref[2],
                        "s_ref": ref[3], "mid": mid, "bid": bid, "ask": ask, "gap": gap, "f_now": (pm + k) / 2})
        return out

    def plan(self, d: int, move_abs: float, levels: list, budget: float) -> tuple[str, float, int]:
        """(side, limit price paid per share, shares) for taking the stale side: d > 0 buys YES off the asks, d < 0 buys
        NO off the bids (NO price = 1 - bid). levels: that book side, best first, [{price, quantity}]. Walks
        walk_ticks(move) ticks behind the touch, never past budget (notional)."""
        if not levels or budget <= 0:
            return ("yes" if d > 0 else "no"), 0.0, 0
        j = walk_ticks(move_abs)
        touch = float(levels[0]["price"])
        if d > 0:
            side, lim_yes = "yes", round(touch + j * TICK, 3)
            within = [l for l in levels if float(l["price"]) <= lim_yes + 1e-9]
            px = lim_yes
        else:
            side, lim_yes = "no", round(touch - j * TICK, 3)
            within = [l for l in levels if float(l["price"]) >= lim_yes - 1e-9]
            px = round(1 - lim_yes, 3)
        if not (0.005 <= px <= 0.995):
            return side, 0.0, 0
        qty = sum(float(l["quantity"]) for l in within)
        # shares as if the leg cost >= 0.40: a cheap leg gets fewer shares, so the entry mark (walk x shares) cannot eat
        # the stop on legs <= 0.25 (review 5: 20k at 0.20 was ~90k shares, -1,264 marked at entry vs a -1,500 stop)
        q = int(min(qty, budget / max(px, SHARE_PRICE_FLOOR), self.max_order_shares))
        return side, px, (q if q >= self.min_shares else 0)

    def budget_left(self, race: str, open_notional_by_race: dict) -> float:
        """Notional still available for race: per-shock cap minus what the race holds, within the sleeve's total."""
        held_race = open_notional_by_race.get(race, 0.0)
        held_all = sum(open_notional_by_race.values())
        return max(0.0, min(self.per_shock - held_race, self.total - held_all))

    def add_move(self, ex: str, row: Optional[dict], now: float) -> Optional[tuple]:
        """For a held leg inside the add window: (full outside move, walk amount = min(move, gap still open)), both abs in
        our direction, if a pyramid add is allowed (the outside agrees, SIG has copied < 50%, the move is >= the stored
        move, a gap >= GAP_MIN is open); else None. The stored move only ever rises (review 10)."""
        c = self.ctx.get(str(ex))
        if c is None or row is None or self.killed or now - c["ts"] > ADD_WINDOW or now > LAST_ENTRY:
            return None
        pm, k, mid = row.get("pm"), row.get("kalshi"), row.get("sig_mid")
        if None in (pm, k, mid) or abs(pm - k) > AGREE + 1e-9:
            return None
        m = ((pm + k) / 2 - c["f_ref"]) * c["d"]
        if m < abs(c["move"]) - 1e-9:
            return None
        if (mid - c["s_ref"]) * c["d"] >= ADD_STOP_COPY * m:
            return None
        gap = ((pm + k) / 2 - mid) * c["d"]
        if gap < GAP_MIN - 1e-9:
            return None
        return m, min(m, gap)              # the edge left is the smaller of the move and the gap still open

    # ------------------------------------------------------------------ exits
    def exit_reason(self, ex: str, row: Optional[dict], now: float, converge: float = CONVERGE) -> Optional[str]:
        """Why an open position on ex must be closed now, or None to keep it (converge: CONVERGE_WITH_MAKER in R3 mode)."""
        if now >= FLATTEN_AT:
            return "flatten before the 3 Nov punt"
        c = self.ctx.get(str(ex))
        if c is None:
            return "no entry context (restart)"
        if self.killed:
            return "sleeve stopped"
        if now - c["ts"] >= EXIT_AFTER:
            return "60-min time stop"
        if row is None:
            return None
        pm, k, mid = row.get("pm"), row.get("kalshi"), row.get("sig_mid")
        if mid is not None and (mid - c["s_ref"]) * c["d"] >= converge * float(c.get("conv", abs(c["move"]))):
            return "SIG caught up"
        if pm is not None and k is not None:
            if abs(pm - k) > DISAGREE_EXIT:
                return "Polymarket and Kalshi disagree"
            if now - c["ts"] <= HOAX_WINDOW and ((pm + k) / 2 - c["f_ref"]) * c["d"] < (1 - HOAX_GIVEBACK) * abs(c["move"]):
                return "outside gave back half the move"
        return None

    def maker_exit(self, ex: str, q: float) -> Optional[tuple]:
        """R3 exit option: (side, price) of a resting order that closes q (YES-signed) at the price SIG reaches when it
        has copied CONVERGE of the move, i.e. where the taker exit would fire, but as a maker (the DMM's walk crosses it)."""
        c = self.ctx.get(str(ex))
        if c is None or not q:
            return None
        target = c["s_ref"] + c["d"] * CONVERGE * float(c.get("conv", abs(c["move"])))   # SIG at 70% of what it can copy
        if q > 0:                                                        # long YES: sell YES at >= target = buy NO at <= 1 - target
            yes = math.ceil(target / TICK - 1e-9) * TICK
            side, px = "no", round(1 - yes, 3)
        else:                                                            # long NO: buy YES at <= target
            side, px = "yes", round(math.floor(target / TICK + 1e-9) * TICK, 3)
        return (side, px) if 0.005 <= px <= 0.995 else None

    def check_kill(self, pnl: float) -> bool:
        if pnl <= self.kill_pnl:
            self.killed = True
        return self.killed

    # ------------------------------------------------------------------ state
    def to_json(self) -> dict:
        return {"ctx": self.ctx, "last_shock": self.last_shock, "killed": self.killed, "netted": self.netted}

    def load(self, st: dict):
        self.ctx = {str(k): v for k, v in (st.get("ctx") or {}).items() if isinstance(v, dict)}
        self.last_shock = {str(k): float(v) for k, v in (st.get("last_shock") or {}).items()}
        self.killed = bool(st.get("killed", False))
        self.netted = {str(k): float(v) for k, v in (st.get("netted") or {}).items()}
