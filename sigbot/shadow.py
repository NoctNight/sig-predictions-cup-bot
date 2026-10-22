"""Shadow backtesting and parameter tuning on the bot's own logs. Paper only: never places orders, never calls the venue.

    python3 -m sigbot.shadow --report          # print the current table (params/shadow_report.json), running first if absent
    python3 -m sigbot.shadow --run             # force a run now and print the table
    python3 -m sigbot.shadow --run --hours 48  # lookback override

Three replays over data/sigbot.sqlite (opened read-only, URI mode=ro, so the live process is never blocked or written):

  arbs       venue_quotes (venue='sig', every 60-120 s, all 237 legs, no sizes) -> set-arb opportunities at the touch,
             re-using sigbot.backtest_arb (build_polls / opportunities / x_table / settlement_ts). Adds capital
             recycling (unwind a held set to fund a better one, recycle_min_gain) and a per-race cap on top of the
             sibling's rules; one entry per race per continuous qualifying run (no re-stacking: the conservative case
             of the sibling's report); assumed top-of-book size ARB_ASSUMED_SIZE because sizes are not logged.
             Score: locked profit of sell-sets, expected (x-adjusted) profit of buy-sets, realised unwinds.
  mm         quotes (our per-cycle touch snapshots on quoted markets) + orders/fills (our own ladder, its exposure
             time and fills) + tape (public trades) -> the ladder is rebuilt under alternative parameters from the
             logged microprice / pressure / mean-reversion skew and a simulated inventory. Fill model (tape-driven
             participation): in each snapshot interval a simulated order fills part(d) x (public tape volume that
             traded at or through its price in that interval), capped at its remaining size, where d is the order's
             CURRENT distance from mid in ticks. part(d) is calibrated on the fit window from our own orders: shares
             we were filled / tape volume at or through our price while our order rested at distance d (order life =
             placement to the next requote batch, expiry or full fill), shrunk toward the pooled rate with a prior of
             MM_PART_PRIOR_SHARES. Queue-position assumption: a simulated order at distance d gets the same share of
             the passing volume as our real orders got at distance d, i.e. it joins the back of the queue exactly as
             the live ladder does; it does NOT get the extra share a larger order would get at the same price.
             Check: the live parameters replayed over the fit window fill within a few % of what the bot actually
             filled (reported as mm_calibration). An hourly hazard by distance is the fallback for exchanges with no
             tape. Expected fills (no Monte Carlo), so results are reproducible. Score: fills marked at the
             exchange's latest mid in the window plus a 30-minute markout as a cross-check; capital = inventory
             collateral + resting-order collateral.
  direction  venue_fair (outside fair per leg per poll with the SIG touch) -> value takes on verdict legs
             (params/fair_explained.json trade_yes/trade_no, one leg per race, as ExplainedDirection does) when
             edge >= min_edge_take + 0.5 * venue disagreement (+0.01 single venue); zero-EV exits when the SIG touch is
             within exit_band of fair. Fills assumed at the logged touch (taker). Score: open legs marked at the latest
             outside fair (primary; the sleeve's own valuation) AND at the latest SIG mid (secondary); a recommendation
             needs both marks to improve.

Scoring: every sleeve reports settlement-value P&L, capital-days (capital held x time, charged to settlement for sets
that lock capital), worst drawdown of the marked P&L path, and `score` = P&L - HURDLE_PER_1K_DAY * capital-days / 1000
(capital is charged at roughly the return of the lowest arb sets: 0.1% per day).
Walk-forward: each group's lookback window is split 70/30 by time; every candidate value is scored on the fit window
(older 70%) with the other parameters at their live values, the fit-best candidate (ties keep the current value) is then
scored on the test window (newest 30%), and the reported improvement is test-window only.
Significance (arb, direction): the test window is cut into TEST_BLOCKS (8) equal blocks; per block, candidate score minus
current score; confidence = bootstrap probability (2,000 resamples of the blocks, fixed seed) that the mean difference is
> 0. The directional sleeve must pass on both marks (outside fair and SIG mid).
Significance (MM): the whole-test-window replay is split into (market, hour) cells; candidate-minus-current contributions
are summed per market and per hour and each set is bootstrapped (cluster_confidence); confidence = the smaller of the two.
Two MM scores must both improve: `score` (marked at the window end) and `score_3h` (each fill marked 3 h later, marks
never past the window end), because adverse selection from Polymarket/Kalshi leads shows at 3-6 h, not 30 min
(research_notes/mm_upgrade.md). MM materiality is per filled share: max(2, 0.1 tick x test shares filled, 10% of |score|).
A change is recommended only if: inside HARD_BOUNDS, not report-only, test gain >= material (arb/direction: max of 10,
50 a day of test window, 10% of the current score), confidence >= MIN_CONFIDENCE (0.90), test drawdown not worse than
1.5x + 25, and the SAME value was recommended by CONFIRM_RUNS (2) scheduled runs of its group whose test windows do not
overlap (a run re-testing the last confirming run's window neither counts nor resets). Ladder grids follow the live
ladder's length (LADDER_GRIDS); apply() refuses a ladder whose length differs from the live sibling key.

Schedule (SCHEDULE; per-group override in config.json shadow.schedule): arb every 4 h on 24 h, direction every 12 h on
72 h, mm_quote every 12 h on 48 h, mm_ladder every 24 h on 96 h; the allocator (sigbot/allocator.py) every 12 h on 48 h,
driven from the same tick. One group runs at a time, the most overdue first; last-run times and confirmation streaks are
kept in params/shadow_state.json so a restart does not re-run everything or forget a half-confirmed change.

Hard bounds (HARD_BOUNDS below) limit what may ever be recommended; they never loosen the kill switch (kill_drawdown,
kill_markout_ticks, kill_pause_sec), the exposure caps (mm.max_inventory_notional, mm.portfolio_max_partisan_notional
5,000, direction.per_market_cap 300 / overall 3,000), all-or-none arb execution, or the arb caps (capital_frac 70%,
max_race_frac 10%) - those are not tunable here and max_race_frac may only be tightened. mm.base_qty is never tuned
(size steps follow Sizhe's markout rule; `--sizes` replays it for information).

Applying: only when config.json has "shadow": {"auto_apply": true} (default false). Applied values are set on the live
config objects, recorded in params/auto_applied.json with the config.json value they override, and written to the audit
trail data/auto_tuning.log (sigbot/tuned.py). On the next start they are re-applied only if config.json still holds that
value; a manual edit of config.json always wins. Without auto_apply everything is logged (SHADOW lines) and written to
params/shadow_report.json only.

Wiring (owned by run.py, unchanged): `from .shadow import tick as shadow_tick` and, at the end of Bot.cycle,
`shadow_tick(self)`. No API reads; at most `cpu_budget_sec` (8) of CPU per group run, in a daemon thread.
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import math
import os
import random
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from .config import PRICE_MAX, PRICE_MIN, TICK, round_tick, ticks
from . import tuned

try:
    from . import backtest_arb as _bta
except Exception:  # the arb replay needs the sibling module; the other sleeves run without it
    _bta = None

log = logging.getLogger("sigbot.shadow")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_PATH = os.path.join(ROOT, "params", "shadow_report.json")

# ------------------------------------------------------------------------------------------------- constants
HURDLE_PER_1K_DAY = 1.0          # capital charge in SUSQies per 1,000 of capital per day (0.1%/day)
FIT_FRAC = 0.70                  # older share of the window used to pick candidates
TEST_BLOCKS = 8                  # equal time blocks of the test window for the significance test
BOOTSTRAP_N = 2000               # bootstrap resamples of the block differences
MIN_CONFIDENCE = 0.90            # P(mean block difference > 0) needed to recommend
CONFIRM_RUNS = 2                 # consecutive scheduled runs that must recommend the same value before it is applied
ARB_ASSUMED_SIZE = 500           # sets available at the touch per race per poll (venue_quotes has no sizes)
ARB_MIN_SETS = 20                # live arb.min_set_qty
ARB_TOTAL_CAP_FRAC = 0.70        # live arb.capital_frac ceiling set by Sizhe 2026-10-22 (never loosened by the tuner)
ARB_RECYCLE_MIN_NEW_RET = 0.02   # live arb.recycle_min_new_ret
MM_HAZARD_BUCKETS = [-1.0, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]   # current distance from mid, ticks
_BUCKET_LUT = {h: min(MM_HAZARD_BUCKETS, key=lambda b: abs(h / 2.0 - b)) for h in range(-2, 17)}
MM_PART_PRIOR_SHARES = 5000.0   # shrinkage weight (shares of through-volume) of each bucket's participation toward the pooled rate
MM_PRESSURE_SKEW_TICKS = 1.0   # live mm.pressure_skew_ticks (needed to undo the logged fair skew)
MM_PRESSURE_SIZE_SKEW = 0.5
MM_INV_SKEW_TICKS = 3.0
MM_SIZE_SKEW_AT_MAX = 0.8
MM_GAMMA = 0.5
MM_IMPROVE_SPREAD_GE = 2           # fallback only: the replay reads cfg.mm.improve_when_spread_ticks_ge
MM_MARK_SEC = 3 * 3600.0           # second MM score: fill P&L marked 3 h later (cross-venue adverse selection shows at 3-6 h)
MM_MATERIAL_FLOOR = 2.0            # MM materiality: max(this, MM_MATERIAL_TICKS_PER_SHARE x tick x test shares filled, 10% of |score|)
MM_MATERIAL_TICKS_PER_SHARE = 0.1
CLUSTER_BOOT_N = 2000
MM_VOL_GATE_MOVE_TICKS = 3.0
MM_VOL_GATE_COOLDOWN = 1800
MM_VOL_GATE_SPREAD_ADD = 1
MM_VOL_GATE_SIZE_MULT = 0.5      # live config (reducing side while hot)
DIR_PER_LEG_CAP = 300.0          # live direction.per_market_cap (never loosened)
DIR_TOTAL_CAP_MAX = 3000.0       # directional sleeve total cap (never loosened); live = overall_cash_frac x cash, at most this
DIR_EDGE_FULL = 0.06
DIR_MIN_QTY = 20
DIR_DISAGREE_MULT = 0.5
DIR_MAX_DISAGREE = 0.08
DIR_SINGLE_VENUE_EXTRA = 0.01

# Hard bounds: (min, max) for scalars, or a validator for lists. Anything not listed is never recommended.
def _ladder_ok_ticks(v) -> bool:
    return isinstance(v, list) and 1 <= len(v) <= 6 and all(isinstance(x, int) and 1 <= x <= 10 for x in v) and all(a < b for a, b in zip(v, v[1:]))


def _ladder_ok_mult(v) -> bool:
    return isinstance(v, list) and 1 <= len(v) <= 6 and all(0.0 <= float(x) <= 2.0 for x in v) and sum(v) <= 4.0


HARD_BOUNDS: dict[str, Any] = {
    "arb.min_ret": (0.0, 0.04),
    "arb.recycle_min_gain": (0.005, 0.05),
    "arb.max_race_frac": (0.01, 0.10),                 # may only tighten: 10% is the live cap (Sizhe, 2026-10-22)
    "mm.requote_ticks": (0.5, 3.0),
    "mm.meanrev_max_ticks": (0.0, 2.0),
    "mm.vol_gate_fade_mult": (0.0, 1.0),
    "mm.level_ticks": _ladder_ok_ticks,          # any ladder length 1-6; apply() also requires the live ladder's length
    "mm.level_size_mult": _ladder_ok_mult,
    "direction.min_edge_take": (0.02, 0.08),
    "exit_band": (0.0, 0.03),
}
REPORT_ONLY = {"arb.free_unwinds",          # a safety feature, not a tuning knob
               "mm.meanrev_max_ticks"}      # values above the live cap cannot be replayed, so a cut could never be undone

# Candidate grids. Each parameter is varied alone, the others stay at their live values (coordinate search).
GRIDS: dict[str, list] = {
    "arb.min_ret": [round(0.002 * i, 3) for i in range(0, 21)],
    "arb.recycle_min_gain": [0.005, 0.01, 0.02, 0.03, 0.05],
    "arb.max_race_frac": [0.03, 0.05, 0.07, 0.10],
    "arb.free_unwinds": [True, False],
    "mm.requote_ticks": [0.5, 1.0, 1.5, 2.0, 3.0],
    "mm.level_ticks": [[1, 2, 3, 5], [1, 2, 4, 6], [2, 3, 4, 6], [1, 3, 5, 8], [2, 4, 6, 8]],
    "mm.level_size_mult": [[0.5, 0.75, 1.0, 1.25], [1.0, 1.0, 1.0, 1.0], [1.5, 1.0, 0.5, 0.25], [0.5, 0.75, 1.0, 1.5], [0.25, 0.5, 1.0, 1.5]],
    "mm.meanrev_max_ticks": [0.0, 0.5, 1.0, 1.5, 2.0],
    "mm.vol_gate_fade_mult": [0.0, 0.25, 0.5, 0.75, 1.0],
    "direction.min_edge_take": [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08],
    "exit_band": [0.0, 0.005, 0.01, 0.015, 0.02],
}
# Ladder candidates by ladder length (the live ladder's length picks the grid, so ticks and multipliers always pair up)
LADDER_GRIDS: dict[str, dict[int, list]] = {
    "mm.level_ticks": {2: [[1, 2], [1, 3], [2, 3], [2, 4], [1, 4]], 3: [[1, 2, 3], [1, 2, 4], [1, 3, 5], [2, 3, 5]],
                       4: GRIDS["mm.level_ticks"], 5: [[1, 2, 3, 5, 8], [1, 2, 4, 6, 9], [2, 3, 4, 6, 8]]},
    "mm.level_size_mult": {2: [[1.0, 0.5], [1.0, 1.0], [1.0, 0.25], [0.5, 1.0], [1.5, 0.5]], 3: [[1.0, 0.5, 0.25], [1.0, 1.0, 1.0], [1.0, 0.75, 0.5], [0.5, 0.75, 1.0]],
                           4: GRIDS["mm.level_size_mult"], 5: [[1.0, 1.0, 0.75, 0.5, 0.25], [1.5, 1.0, 0.5, 0.25, 0.0], [0.5, 0.75, 1.0, 0.75, 0.5], [1.0, 1.0, 1.0, 1.0, 1.0]]},
}


def grid_for(key: str, current) -> list:
    """Candidate values for a key; ladder grids follow the live ladder's length."""
    if key in LADDER_GRIDS and isinstance(current, list):
        return [list(v) for v in LADDER_GRIDS[key].get(len(current), [])]
    return list(GRIDS[key])


SLEEVE_OF = {k: k.split(".")[0] if "." in k else "direction" for k in GRIDS}
TUNABLE_KEYS = [k for k in GRIDS if k not in REPORT_ONLY]

# Per-group schedule. Arbs: dense data (every leg every 60-120 s) and fast-changing opportunities -> every 4 h on the last
# 24 h. Directional and MM quoting: twice a day on 72 h / 48 h. Ladder shape: once a day on 96 h, because MM fills are
# sparse (~15 an hour) and the shape is structural. Override per group in config.json shadow.schedule.
SCHEDULE: dict[str, dict] = {
    "arb": {"keys": ["arb.min_ret", "arb.recycle_min_gain", "arb.max_race_frac", "arb.free_unwinds"], "every_h": 4.0, "lookback_h": 24.0},
    "direction": {"keys": ["direction.min_edge_take", "exit_band"], "every_h": 12.0, "lookback_h": 72.0},
    "mm_quote": {"keys": ["mm.requote_ticks", "mm.vol_gate_fade_mult", "mm.meanrev_max_ticks"], "every_h": 12.0, "lookback_h": 48.0},
    "mm_ladder": {"keys": ["mm.level_ticks", "mm.level_size_mult"], "every_h": 24.0, "lookback_h": 96.0},
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def within_bounds(key: str, value: Any) -> bool:
    b = HARD_BOUNDS.get(key)
    if b is None:
        return False
    if callable(b):
        try:
            return bool(b(value))
        except Exception:
            return False
    lo, hi = b
    try:
        return lo - 1e-12 <= float(value) <= hi + 1e-12
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------------------------------------- data access
def open_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def live_params(cfg, bot=None) -> dict:
    """Current value of every tunable: the live objects when a bot is given (they include automatic changes), else the
    BotConfig plus config.json's direction section."""
    out = {
        "arb.min_ret": float(cfg.arb.min_ret), "arb.recycle_min_gain": float(cfg.arb.recycle_min_gain),
        "arb.max_race_frac": float(cfg.arb.max_race_frac), "arb.free_unwinds": bool(cfg.free_unwind_every_sec < 86400),
        "mm.requote_ticks": float(cfg.mm.requote_ticks), "mm.level_ticks": list(cfg.mm.level_ticks)[:4],
        "mm.level_size_mult": list(cfg.mm.level_size_mult)[:4], "mm.meanrev_max_ticks": float(cfg.mm.meanrev_max_ticks),
        "mm.vol_gate_fade_mult": float(cfg.mm.vol_gate_fade_mult), "exit_band": float(cfg.exit_band),
        "direction.min_edge_take": 0.02, "direction.overall_cash_frac": 0.04,
    }
    dcfg = getattr(getattr(bot, "direction", None), "cfg", None) if bot is not None else None
    if dcfg is not None:
        out["direction.min_edge_take"] = float(dcfg.min_edge_take)
        out["direction.overall_cash_frac"] = float(dcfg.overall_cash_frac)
    else:
        try:
            d = json.load(open(os.path.join(ROOT, "config.json"))).get("direction") or {}
            out["direction.min_edge_take"] = float(d.get("min_edge_take", 0.02))
            out["direction.overall_cash_frac"] = float(d.get("overall_cash_frac", 0.04))
        except Exception:
            pass
    return out


def _score(pnl: float, cap_days: float) -> float:
    return pnl - HURDLE_PER_1K_DAY * cap_days / 1000.0


# ================================================================================================= ARB REPLAY
class ArbData:
    def __init__(self, con: sqlite3.Connection, t0: float, t1: float):
        self.ok = _bta is not None
        self.polls, self.races, self.xs, self.settle = [], {}, {}, None
        if not self.ok:
            return
        rows = con.execute("SELECT ts, race, party, instrument, bid, ask, bid_sz, ask_sz FROM venue_quotes WHERE venue='sig' AND ts>=? AND ts<=? ORDER BY ts",
                           (t0, t1)).fetchall()
        # legs quoted before the window are forward-filled from their last row before t0
        prior = con.execute("SELECT ts, race, party, instrument, bid, ask, bid_sz, ask_sz FROM venue_quotes WHERE venue='sig' AND ts<? "
                            "AND ts >= (SELECT MAX(ts) FROM venue_quotes WHERE venue='sig' AND ts<?) - 86400 ORDER BY ts", (t0, t0)).fetchall()
        seen = {}
        for r in prior:
            seen[r["instrument"]] = tuple(r)
        pre = [(t0 - 1.0,) + tuple(v[1:]) for v in seen.values()]
        allrows = pre + [tuple(r) for r in rows]
        if not allrows:
            self.ok = False
            return
        self.polls, self.races, _ = _bta.build_polls(allrows)
        self.polls = [p for p in self.polls if p[0] >= t0]
        self.xs = _bta.x_table(self.races)
        self.settle = _bta.settlement_ts() or (t1 + 13 * 86400)
        # opportunities per poll computed once: (ts, snap, [(ret, race, kind, edge, cost, ssum)]) sorted by return
        self.opps: list[tuple] = []
        for ts, snap in self.polls:
            ops = []
            for race, srow in snap.items():
                for kind, edge, cost, ret, ssum in _bta.opportunities(srow, self.xs[race]):
                    if edge > 0.005 - 1e-12:                       # live also needs 0.5c of absolute edge
                        ops.append((ret, race, kind, edge, cost, ssum))
            ops.sort(key=lambda c: -c[0])
            self.opps.append((ts, snap, ops))
        # the sets the bot actually held, to start each replay window from the real book (capital already locked)
        self.actual = con.execute("SELECT ts, race, kind, quantity, edge_per_set, cost_per_set FROM arb_trades WHERE ok=1 AND ts<=? ORDER BY ts",
                                  (t1,)).fetchall()
        self.ok = len(self.polls) >= 5

    def start_lots(self, t: float) -> list:
        """Held sets at time t from arb_trades (entries minus FIFO unwinds before t)."""
        lots: list[dict] = []
        for r in self.actual:
            if r["ts"] >= t:
                break
            if r["kind"] in ("sell_set", "buy_set"):
                lots.append(dict(race=r["race"], kind=r["kind"], qty=int(r["quantity"]), edge=float(r["edge_per_set"]), cost=float(r["cost_per_set"]),
                                 sumbids=(1.0 + float(r["edge_per_set"])) if r["kind"] == "sell_set" else 0.0, start=True))
            elif r["kind"] == "unwind_set":
                q = int(r["quantity"])
                for lot in lots:
                    if q <= 0:
                        break
                    if lot["race"] == r["race"] and lot["qty"] > 0:
                        take = min(q, lot["qty"]); lot["qty"] -= take; q -= take
        return [l for l in lots if l["qty"] > 0]


def simulate_arb(data: ArbData, polls: list, min_ret: float, race_frac: float, unwinds: bool, recycle_gain: Optional[float],
                 starting_cash: float = 100000.0, capital_frac: float = ARB_TOTAL_CAP_FRAC) -> dict:
    """Paper replay of the set-arb rules over `polls` (subset of data.polls), starting from the sets the bot actually held
    at the first poll (their capital is already locked; they can be free-unwound or recycled). One entry per race per
    continuous qualifying run; capital recycling when the total budget is exhausted; free unwinds at sum(asks) <= 1.
    P&L is the change in settlement value over the window (so held sets count only through what happens to them);
    capital-days are charged inside the window for everything held, and to settlement only for sets entered here."""
    settle = data.settle
    total_cap = min(capital_frac, ARB_TOTAL_CAP_FRAC) * starting_cash
    race_cap = race_frac * starting_cash
    size = ARB_ASSUMED_SIZE
    cap_hours, realised, peak_val, worst_dd = 0.0, 0.0, 0.0, 0.0
    cap_race: dict[str, float] = defaultdict(float)
    entries = unw = rec = 0
    active: set = set()
    if not polls:
        return dict(pnl=0.0, cap_days=0.0, drawdown=0.0, score=0.0, entries=0, unwinds=0, recycles=0, sets=0)
    lots: list[dict] = data.start_lots(polls[0][0]) if hasattr(data, "start_lots") else []
    cap_tot = sum(l["qty"] * l["cost"] for l in lots)
    for l in lots:
        cap_race[l["race"]] += l["qty"] * l["cost"]
    start_value = sum(l["qty"] * l["edge"] for l in lots)
    start_cap = cap_tot
    prev = polls[0][0]
    sets = 0

    def value_now():   # settlement value of the book: realised + locked/expected edge of held lots, relative to the start
        return realised + sum(l["qty"] * l["edge"] for l in lots) - start_value

    for ts, snap, ops in polls:
        cap_hours += cap_tot * (ts - prev) / 3600.0
        prev = ts
        if unwinds:
            for race in {l["race"] for l in lots if l["kind"] == "sell_set"}:
                s = snap.get(race)
                if not s or any(a is None for a in s[1]) or sum(s[1]) > 1.0 + 1e-12:
                    continue
                sa = sum(s[1]); room = size
                for lot in lots:
                    if room <= 0:
                        break
                    if lot["race"] != race or lot["kind"] != "sell_set":
                        continue
                    q = min(lot["qty"], room)
                    realised += q * (lot["sumbids"] - sa)
                    cap_tot -= q * lot["cost"]; cap_race[race] -= q * lot["cost"]
                    lot["qty"] -= q; room -= q; unw += 1
                lots = [l for l in lots if l["qty"] > 0]
        cands, live = [], set()
        for op in ops:
            if op[0] + 1e-12 < min_ret:
                break                                               # sorted by return
            live.add((op[1], op[2]))
            if (op[1], op[2]) not in active:
                cands.append(op)
        active = live
        recycled_this_poll = False
        for ret, race, kind, edge, cost, ssum in cands:
            room = min(total_cap - cap_tot, race_cap - cap_race[race])
            q = min(size, int(room // cost))
            if q < ARB_MIN_SETS and recycle_gain is not None and not recycled_this_poll and ret >= ARB_RECYCLE_MIN_NEW_RET \
                    and total_cap - cap_tot < ARB_MIN_SETS * cost and race_cap - cap_race[race] >= ARB_MIN_SETS * cost:
                # unwind the held sell-set whose remaining return we forgo the least, if the new return beats it by the gain
                best = None
                for lot in lots:
                    if lot["kind"] != "sell_set" or lot["race"] == race:
                        continue
                    s = snap.get(lot["race"])
                    if not s or any(a is None for a in s[1]):
                        continue
                    sa = sum(s[1]); n = len(s[1])
                    proceeds = n - sa
                    if proceeds <= 0:
                        continue
                    forgone_ret = ((n - 1) - proceeds) / proceeds
                    gain = ret - forgone_ret
                    if gain >= recycle_gain and (best is None or gain > best[0]):
                        best = (gain, lot, sa)
                if best is not None:
                    gain, lot, sa = best
                    uq = min(lot["qty"], size)
                    realised += uq * (lot["sumbids"] - sa)
                    cap_tot -= uq * lot["cost"]; cap_race[lot["race"]] -= uq * lot["cost"]
                    lot["qty"] -= uq; rec += 1; recycled_this_poll = True
                    lots = [l for l in lots if l["qty"] > 0]
                    room = min(total_cap - cap_tot, race_cap - cap_race[race])
                    q = min(size, int(room // cost))
            if q < ARB_MIN_SETS:
                continue
            lots.append(dict(race=race, kind=kind, qty=q, edge=edge, cost=cost, sumbids=ssum if kind == "sell_set" else 0.0))
            cap_tot += q * cost; cap_race[race] += q * cost; entries += 1; sets += q
        v = value_now()
        peak_val = max(peak_val, v); worst_dd = max(worst_dd, peak_val - v)
    t1 = polls[-1][0]
    held_cap = sum(l["qty"] * l["cost"] for l in lots)
    new_cap = sum(l["qty"] * l["cost"] for l in lots if not l.get("start"))
    cap_days = (cap_hours + new_cap * max(0.0, settle - t1) / 3600.0) / 24.0
    pnl = value_now()
    return dict(pnl=pnl, cap_days=cap_days, drawdown=worst_dd, score=_score(pnl, cap_days), entries=entries, unwinds=unw,
                recycles=rec, sets=sets, held_cap=held_cap, new_cap=new_cap, start_cap=start_cap,
                realised=realised)


# ================================================================================================= MM REPLAY
class MMData:
    """Quotes snapshots, our orders/fills, the tape, and the fill-hazard calibration."""

    def __init__(self, con: sqlite3.Connection, t0: float, t1: float, fit_until: float, cfg):
        self.cfg = cfg
        self.snaps: dict[str, list] = defaultdict(list)
        for r in con.execute("SELECT ts, exchange_id, best_bid, best_ask, bid_qty, ask_qty, mid, microprice, fair, pressure FROM quotes "
                             "WHERE ts>=? AND ts<=? AND mid IS NOT NULL AND microprice IS NOT NULL AND fair IS NOT NULL ORDER BY ts", (t0, t1)):
            self.snaps[r["exchange_id"]].append((r["ts"], r["best_bid"], r["best_ask"], r["bid_qty"], r["ask_qty"], r["mid"], r["microprice"],
                                                 r["fair"], r["pressure"] or 0.0))
        self.stimes = {ex: [s[0] for s in v] for ex, v in self.snaps.items()}
        self.tape: dict[str, list] = defaultdict(list)
        lo, hi = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), datetime.fromtimestamp(t1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        for r in con.execute("SELECT exchange_id, price, quantity, executed_at FROM tape WHERE executed_at>=? AND executed_at<=? ORDER BY executed_at", (lo, hi)):
            self.tape[r["exchange_id"]].append((_ts(r["executed_at"]), r["price"], abs(r["quantity"])))
        self.tape_t = {ex: [x[0] for x in v] for ex, v in self.tape.items()}
        self.part: dict[float, float] = {}
        self.qref: dict[float, float] = {}
        self.orders = con.execute("SELECT order_id, exchange_id, side, price, quantity, level, placed_at, expires_at FROM orders WHERE purpose='mm' "
                                  "AND placed_at>=? AND placed_at<=? ORDER BY exchange_id, placed_at", (t0, fit_until)).fetchall()
        self.fills = con.execute("SELECT order_id, exchange_id, side, price, ABS(quantity) q, filled_at FROM fills WHERE purpose='mm' AND filled_at>=? AND filled_at<=?",
                                 (lo, datetime.fromtimestamp(fit_until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))).fetchall()
        self.vol: dict[str, float] = {}
        try:
            self.vol = {k: float(v) for k, v in json.load(open(os.path.join(ROOT, "data", "volume_cache.json.vol"))).items()}
        except Exception:
            pass
        self.stop_at = _ts(getattr(cfg.mm, "stop_quoting_at", "2026-11-03T12:00:00Z"))
        self.hazard, self.fillfrac, self.calib = self._calibrate(fit_until)
        # base_qty the live ladder used in the fit window (median of size / level multiplier), for the calibration check
        mult = list(cfg.mm.level_size_mult)
        ests = sorted(o["quantity"] / mult[o["level"] - 1] for o in self.orders if o["level"] and 1 <= o["level"] <= len(mult) and mult[o["level"] - 1] > 0)
        self.base_qty_fit = round(ests[len(ests) // 2]) if ests else float(cfg.mm.base_qty)
        self.calib["base_qty_fit_estimate"] = self.base_qty_fit
        self.n_snaps = sum(len(v) for v in self.snaps.values())
        # per-exchange index of the snapshot 15 minutes back (vol gate) and 30 minutes ahead (markout), computed once
        self.idx15: dict[str, list] = {}
        self.idx30: dict[str, list] = {}
        for ex, snaps in self.snaps.items():
            back, fwd, j, k = [], [], -1, 0
            for i, s in enumerate(snaps):
                while j + 1 < i and snaps[j + 1][0] <= s[0] - 900:
                    j += 1
                back.append(j if j >= 0 and snaps[j][0] <= s[0] - 900 else -1)
                k = max(k, i)
                while k < len(snaps) and snaps[k][0] < s[0] + 1800:
                    k += 1
                fwd.append(k if k < len(snaps) else -1)
            self.idx15[ex], self.idx30[ex] = back, fwd
        self.ok = self.n_snaps >= 50 and self.calib.get("fills", 0) >= 5

    @staticmethod
    def bucket(d: float) -> float:
        """Nearest hazard bucket to a distance in ticks (half-tick resolution, clamped to [-1, 8])."""
        return _BUCKET_LUT[max(-2, min(16, int(round(d * 2.0))))]

    def _calibrate(self, fit_until: float):
        """Fills per resting-order-hour by current distance from mid (ticks), on the fit window only."""
        exp_h: dict[float, float] = defaultdict(float)
        through_v: dict[float, float] = defaultdict(float)
        filled_v: dict[float, float] = defaultdict(float)
        qexp: dict[float, float] = defaultdict(float)      # order size x exposure hours (for the size-proportional model)
        events: dict[float, int] = defaultdict(int)
        fracs: dict[float, list] = defaultdict(list)
        by_ex: dict[str, list] = defaultdict(list)
        for o in self.orders:
            by_ex[o["exchange_id"]].append(o)
        fills_by_order: dict[int, list] = defaultdict(list)
        for f in self.fills:
            if f["order_id"] is not None:
                fills_by_order[int(f["order_id"])].append((_ts(f["filled_at"]), float(f["q"])))
        n_orders = n_fill_events = 0
        for ex, orders in by_ex.items():
            snaps = self.snaps.get(ex) or []
            if len(snaps) < 2:
                continue
            tape = self.tape.get(ex)
            # a requote is cancel-all + a batch of orders placed within a few seconds: cluster placements with gaps > 3 s,
            # an order lives until the next cluster starts (the cancel precedes it), its expiry, or its full fill
            times = sorted(o["placed_at"] for o in orders)
            clusters: list[float] = []
            for t in times:
                if not clusters or t - clusters[-1][1] > 3.0:
                    clusters.append([t, t])
                else:
                    clusters[-1][1] = t
            cluster_end: dict[int, float] = {}
            for ci, (a, b) in enumerate(clusters):
                cluster_end[ci] = clusters[ci + 1][0] if ci + 1 < len(clusters) else fit_until
            def cluster_of(t: float) -> int:
                lo, hi = 0, len(clusters) - 1
                while lo < hi:
                    m = (lo + hi + 1) // 2
                    if clusters[m][0] <= t:
                        lo = m
                    else:
                        hi = m - 1
                return lo
            stimes = [s[0] for s in snaps]
            j = 0
            for o in orders:
                n_orders += 1
                start = o["placed_at"]
                end = min(cluster_end[cluster_of(start)], fit_until)
                if o["expires_at"]:
                    end = min(end, _ts(o["expires_at"]))
                ofills = sorted(fills_by_order.get(int(o["order_id"]), []))
                filled = sum(q for _, q in ofills)
                if ofills and filled >= o["quantity"] - 0.5:
                    end = min(end, ofills[-1][0] + 1.0)      # +1 s so the closing fill lies inside the half-open interval
                if end <= start:
                    continue
                yes_px = o["price"] if o["side"] == "yes" else round(1 - o["price"], 3)
                # walk snapshot intervals overlapping [start, end)
                while j > 0 and stimes[j] > start:
                    j -= 1
                while j + 1 < len(stimes) and stimes[j + 1] <= start:
                    j += 1
                k = j
                while k < len(stimes) and stimes[k] < end:
                    a = max(start, stimes[k]); b = min(end, stimes[k + 1] if k + 1 < len(stimes) else end)
                    if b > a:
                        mid = snaps[k][5]
                        d = (mid - yes_px) / TICK if o["side"] == "yes" else (yes_px - mid) / TICK
                        bk = self.bucket(d)
                        exp_h[bk] += (b - a) / 3600.0
                        qexp[bk] += o["quantity"] * (b - a) / 3600.0
                        if tape:
                            through_v[bk] += self.through(ex, a, b, o["side"], yes_px)
                        for ft, fq in ofills:
                            if a <= ft < b:
                                events[bk] += 1; n_fill_events += 1
                                fracs[bk].append(min(1.0, fq / max(1.0, o["quantity"])))
                                if tape:
                                    filled_v[bk] += fq
                    k += 1
        # monotone (non-increasing in distance) hazard via pool-adjacent-violators, weighted by exposure
        order = sorted(MM_HAZARD_BUCKETS)
        blocks = [[b, exp_h.get(b, 0.0), float(events.get(b, 0))] for b in order]
        merged: list[list] = []
        for blk in blocks:
            merged.append(blk)
            while len(merged) >= 2:
                p, q = merged[-2], merged[-1]
                rp = p[2] / p[1] if p[1] > 0 else None
                rq = q[2] / q[1] if q[1] > 0 else None
                if rp is None or rq is None or rp >= rq:
                    break
                merged[-2:] = [[p[0], p[1] + q[1], p[2] + q[2]]]
        hazard: dict[float, float] = {}
        observed = [(blk[0], blk[2] / blk[1]) for blk in merged if blk[1] > 0]
        for b in order:
            # rate of the pooled block this bucket belongs to; buckets without exposure take the nearest observed block
            # toward the mid (a higher or equal rate by monotonicity, so unobserved deep levels are not assumed to fill less)
            rate = None
            for b0, r in observed:
                if b0 <= b:
                    rate = r
            if rate is None:
                rate = observed[0][1] if observed else 0.0
            hazard[b] = rate
        # fill fraction per bucket, pooled fallback
        allf = [x for v in fracs.values() for x in v]
        pooled = sum(allf) / len(allf) if allf else 0.85
        fillfrac = {b: (sum(fracs[b]) / len(fracs[b]) if len(fracs[b]) >= 5 else pooled) for b in order}
        # participation: our filled shares per share of public volume that traded at or through our price while the order
        # rested at that distance. Shrunk toward the pooled rate with a prior of MM_PART_PRIOR_SHARES of volume, capped at 1.
        tot_f, tot_v = sum(filled_v.values()), sum(through_v.values())
        pooled_part = tot_f / tot_v if tot_v > 0 else 0.0
        part = {b: min(1.0, (filled_v.get(b, 0.0) + pooled_part * MM_PART_PRIOR_SHARES) / (through_v.get(b, 0.0) + MM_PART_PRIOR_SHARES)) for b in order}
        calib = {"orders": n_orders, "fills": n_fill_events, "exposure_hours": {str(b): round(exp_h.get(b, 0.0), 2) for b in order},
                 "events": {str(b): events.get(b, 0) for b in order}, "hazard_per_hour": {str(b): round(hazard[b], 3) for b in order},
                 "fill_frac": {str(b): round(fillfrac[b], 2) for b in order}, "pooled_fill_frac": round(pooled, 3),
                 "through_volume": {str(b): round(through_v.get(b, 0.0)) for b in order}, "filled_volume": {str(b): round(filled_v.get(b, 0.0)) for b in order},
                 "participation": {str(b): round(part[b], 4) for b in order}, "pooled_participation": round(pooled_part, 4)}
        self.part = part
        pooled_q = sum(qexp.values()) / sum(exp_h.values()) if sum(exp_h.values()) > 0 else 100.0
        self.qref = {b: (qexp[b] / exp_h[b] if exp_h.get(b, 0.0) > 0.5 else pooled_q) for b in order}
        calib["mean_order_qty"] = {str(b): round(self.qref[b]) for b in order}
        return hazard, fillfrac, calib

    def mid_at(self, ex: str, ts: float) -> Optional[float]:
        """First snapshot mid at or after ts (None past the end of the loaded series)."""
        tt = self.stimes.get(ex)
        if not tt:
            return None
        i = bisect.bisect_left(tt, ts)
        return self.snaps[ex][i][5] if i < len(tt) else None

    def through(self, ex: str, a: float, b: float, side: str, yes_px: float) -> float:
        """Public tape volume in [a, b) at or through a resting YES price: at or below it for a bid, at or above for an ask."""
        tt = self.tape_t.get(ex)
        if not tt:
            return 0.0
        tp = self.tape[ex]
        i, j = bisect.bisect_left(tt, a), bisect.bisect_left(tt, b)
        if side == "yes":
            return sum(tp[k][2] for k in range(i, j) if tp[k][1] <= yes_px + 1e-9)
        return sum(tp[k][2] for k in range(i, j) if tp[k][1] >= yes_px - 1e-9)


def simulate_mm(data: MMData, t_from: float, t_to: float, p: dict) -> dict:
    """Rebuild the ladder per snapshot under parameters p (requote_ticks, level_ticks, level_size_mult, meanrev_max_ticks,
    vol_gate_fade_mult; optional base_qty, max_inventory_notional, capital_frac, size_model) and fill it with the tape
    participation model. Starts flat on every exchange.
    size_model 'fixed' (default): an order gets the calibrated share of passing volume whatever its size (a lower bound for
    larger orders). 'prop': the share scales with order size relative to the calibration orders at that distance
    (pro-rata; an upper bound under price-time priority)."""
    cfg = data.cfg.mm
    live_mr_cap = float(cfg.meanrev_max_ticks) * TICK
    mr_cap = min(float(p["meanrev_max_ticks"]), float(cfg.meanrev_max_ticks)) * TICK
    lticks, lmult = list(p["level_ticks"]), list(p["level_size_mult"])
    nlev = min(len(lticks), len(lmult))          # a ticks/multiplier length mismatch quotes the common levels only
    lticks, lmult = lticks[:nlev], lmult[:nlev]
    improve_ge = int(p.get("improve_ge", getattr(cfg, "improve_when_spread_ticks_ge", MM_IMPROVE_SPREAD_GE)))
    want_cells = bool(p.get("cells"))
    cells: dict[tuple, list] = defaultdict(lambda: [0.0, 0.0, 0.0])   # (exchange, hour) -> [marked pnl change, collateral-hours, 3-h-mark fill pnl]
    edge3h = 0.0
    requote = float(p["requote_ticks"]) * TICK
    fade = float(p["vol_gate_fade_mult"])
    cap_frac = float(p.get("capital_frac", cfg.capital_frac))
    per_market = min(cap_frac * data.cfg.starting_cash, 0.9 * data.cfg.starting_cash) / max(1, cfg.n_markets)
    half = 0.5 * per_market
    base_qty = float(p.get("base_qty", cfg.base_qty))
    max_inv = float(p.get("max_inventory_notional", cfg.max_inventory_notional))
    prop = p.get("size_model", "fixed") == "prop"
    lvl_fill: dict[int, float] = defaultdict(float)
    lvl_mo: dict[int, list] = defaultdict(lambda: [0.0, 0])
    max_inv_shares = max_inv_notional = max_coll_total = 0.0
    coll_by_ex: dict[str, float] = {}
    refresh = float(cfg.refresh_sec)
    pnl_total = cap_hours = 0.0
    n_fill_events = fill_qty = 0.0
    peak = dd = 0.0
    path: dict[float, float] = {}        # ts -> aggregate marked pnl (summed across exchanges at snapshot times)
    markouts: list[float] = []
    mo_w = [0.0, 0.0]                    # quantity-weighted 30-minute markout
    for ex, snaps in data.snaps.items():
        i0 = next((i for i, s in enumerate(snaps) if s[0] >= t_from), len(snaps))
        i1 = next((i for i in range(len(snaps) - 1, i0 - 1, -1) if snaps[i][0] <= t_to), i0 - 1) + 1
        rows = snaps[i0:i1]
        if len(rows) < 3:
            continue
        idx15, idx30 = data.idx15[ex], data.idx30[ex]
        tape = data.tape.get(ex) or []
        tape_i = 0
        while tape_i < len(tape) and tape[tape_i][0] < t_from:
            tape_i += 1
        sigma = data.vol.get(ex, 0.01)
        inv = cash = 0.0
        orders: list[list] = []          # [side, yes_px, qty, level]
        last_reserve, last_quote_ts, fill_since = None, 0.0, False
        hot_until, anchor = 0.0, None
        ex_pnl_prev = 0.0
        for i, s in enumerate(rows):
            ts, bb, ba, bq, aq, mid, micro, fair_logged, pressure = s
            gi = i0 + i                  # index into the full per-exchange series
            if i > 0 and ts - rows[i - 1][0] > 900:     # gap: the live bot was not quoting, drop the ladder
                orders = []; last_reserve = None
            # 1. fair under the alternative mean-reversion cap (recovered from the logged fair)
            mr_logged = max(-live_mr_cap, min(live_mr_cap, fair_logged - micro - pressure * MM_PRESSURE_SKEW_TICKS * TICK))
            mr = max(-mr_cap, min(mr_cap, mr_logged))
            fair = micro + pressure * MM_PRESSURE_SKEW_TICKS * TICK + mr
            # 2. reservation price from simulated inventory
            inv_notional = inv * (mid if inv > 0 else (1 - mid))
            inv_frac = max(-1.0, min(1.0, inv_notional / max_inv))
            T = min(24.0, max(0.0, (data.stop_at - ts) / 3600.0))
            shift = max(-MM_INV_SKEW_TICKS, min(MM_INV_SKEW_TICKS, inv_frac * MM_INV_SKEW_TICKS + MM_GAMMA * inv_frac * (sigma / TICK) ** 2 * T / 100.0))
            reserve = fair - shift * TICK
            # 2b. volatility gate (15-minute move rule only; the variance rule needs candle history)
            jb = idx15[gi]
            ref = snaps[jb][5] if jb >= i0 else rows[0][5]
            if abs(mid - ref) >= MM_VOL_GATE_MOVE_TICKS * TICK - 1e-9:
                hot_until = ts + MM_VOL_GATE_COOLDOWN
                if anchor is None:
                    anchor = ref
            hot = hot_until > ts
            if not hot:
                anchor = None
            spread_add = MM_VOL_GATE_SPREAD_ADD if hot else 0
            if hot and anchor is not None:
                if inv > 0:
                    reserve = max(reserve, anchor)
                elif inv < 0:
                    reserve = min(reserve, anchor)
            # 3. requote?
            need = (last_reserve is None or abs(reserve - last_reserve) >= requote - 1e-9 or fill_since or ts - last_quote_ts > refresh
                    or len(orders) < 2 * nlev)
            if need:
                bbid = [base_qty * m for m in lmult]; bask = list(bbid)
                if inv_frac > 0:
                    bbid = [q * (1 - MM_SIZE_SKEW_AT_MAX * inv_frac) for q in bbid]; bask = [q * (1 + 0.5 * MM_SIZE_SKEW_AT_MAX * inv_frac) for q in bask]
                elif inv_frac < 0:
                    bask = [q * (1 - MM_SIZE_SKEW_AT_MAX * -inv_frac) for q in bask]; bbid = [q * (1 + 0.5 * MM_SIZE_SKEW_AT_MAX * -inv_frac) for q in bbid]
                if pressure > 0:
                    bask = [q * (1 - MM_PRESSURE_SIZE_SKEW * min(1.0, pressure)) for q in bask]
                elif pressure < 0:
                    bbid = [q * (1 - MM_PRESSURE_SIZE_SKEW * min(1.0, -pressure)) for q in bbid]
                if inv_frac >= 1.0:
                    bbid = [0.0] * nlev
                elif inv_frac <= -1.0:
                    bask = [0.0] * nlev
                if hot:
                    if inv > 0:
                        bbid = [q * fade for q in bbid]; bask = [q * MM_VOL_GATE_SIZE_MULT for q in bask]
                    elif inv < 0:
                        bask = [q * fade for q in bask]; bbid = [q * MM_VOL_GATE_SIZE_MULT for q in bbid]
                    else:
                        bbid = [q * MM_VOL_GATE_SIZE_MULT for q in bbid]; bask = [q * MM_VOL_GATE_SIZE_MULT for q in bask]
                sb = min(1.0, half / (sum(q * mid for q in bbid) or 1.0)); sa = min(1.0, half / (sum(q * (1 - mid) for q in bask) or 1.0))
                spread_ticks = ticks(ba) - ticks(bb)
                new: dict[tuple, list] = {}
                for k, dt in enumerate(lticks):
                    dt += spread_add
                    improve = TICK if (k == 0 and spread_ticks >= improve_ge) else 0.0
                    bpx = min(round_tick(reserve - dt * TICK), round_tick(bb + improve), round_tick(ba - TICK))
                    qb = int(bbid[k] * sb)
                    if qb >= 1 and PRICE_MIN <= bpx <= PRICE_MAX:
                        key = ("yes", bpx)
                        new.setdefault(key, ["yes", bpx, 0, k + 1])[2] += qb
                    apx = max(round_tick(reserve + dt * TICK), round_tick(ba - improve), round_tick(bb + TICK))
                    qa = int(bask[k] * sa)
                    if qa >= 1 and PRICE_MIN <= apx <= PRICE_MAX:
                        key = ("no", apx)
                        new.setdefault(key, ["no", apx, 0, k + 1])[2] += qa
                orders = list(new.values())
                last_reserve, last_quote_ts, fill_since = reserve, ts, False
            # 4. fills over the interval to the next snapshot
            if i + 1 < len(rows):
                t_next = rows[i + 1][0]
                dt_h = (t_next - ts) / 3600.0
                # tape volume in [ts, t_next)
                seg = []
                while tape_i < len(tape) and tape[tape_i][0] < t_next:
                    if tape[tape_i][0] >= ts:
                        seg.append(tape[tape_i])
                    tape_i += 1
                has_tape = bool(tape)
                for o in orders:
                    side, px, q = o[0], o[1], o[2]
                    if q < 1:
                        continue
                    d = (mid - px) / TICK if side == "yes" else (px - mid) / TICK
                    bk = MMData.bucket(d)
                    if has_tape:
                        # tape-driven: our participation share of the volume that traded at or through our price
                        thr = sum(tq for tt, tp, tq in seg if (tp <= px + 1e-9 if side == "yes" else tp >= px - 1e-9)) if seg else 0.0
                        share = data.part.get(bk, 0.0)
                        if prop:
                            share = min(1.0, share * q / max(1.0, data.qref.get(bk, q)))
                        eq = min(q, share * thr)
                        pfill = 1.0 if eq > 0 else 0.0
                    else:
                        # no tape on this exchange: hourly hazard fallback
                        lam = data.hazard.get(bk, 0.0)
                        pfill = 1.0 - math.exp(-lam * dt_h)
                        eq = pfill * q * data.fillfrac.get(bk, 0.85)
                    if eq <= 0:
                        continue
                    if side == "yes":
                        inv += eq; cash -= eq * px
                    else:
                        inv -= eq; cash += eq * px
                    o[2] = q - eq
                    n_fill_events += pfill; fill_qty += eq; fill_since = True
                    lvl_fill[o[3]] += eq
                    # 30-minute markout cross-check
                    k30 = idx30[gi]
                    if 0 <= k30 < i1:
                        m30 = snaps[k30][5]
                        mo = ((m30 - px) if side == "yes" else (px - m30)) / TICK
                        markouts.append(mo)
                        mo_w[0] += mo * eq; mo_w[1] += eq
                        lvl_mo[o[3]][0] += mo * eq; lvl_mo[o[3]][1] += eq
                    # 3-h mark (purged: never a price after t_to, so fit-window scores carry no test-window prices)
                    tf = 0.5 * (ts + t_next)
                    m3 = data.mid_at(ex, tf + MM_MARK_SEC) if tf + MM_MARK_SEC <= t_to else None
                    if m3 is not None:
                        v3 = eq * ((m3 - px) if side == "yes" else (px - m3))
                        edge3h += v3
                        if want_cells:
                            cells[(ex, int(tf // 3600))][2] += v3
                orders = [o for o in orders if o[2] >= 1]
                inv_coll = abs(inv) * (mid if inv > 0 else (1 - mid))
                collateral = inv_coll + sum(o[2] * (o[1] if o[0] == "yes" else 1 - o[1]) for o in orders)
                cap_hours += collateral * dt_h
                if want_cells:
                    cells[(ex, int(ts // 3600))][1] += collateral * dt_h
                max_inv_shares = max(max_inv_shares, abs(inv)); max_inv_notional = max(max_inv_notional, inv_coll)
                coll_by_ex[ex] = inv_coll
                max_coll_total = max(max_coll_total, sum(coll_by_ex.values()))
            ex_pnl = cash + inv * mid
            path[ts] = path.get(ts, 0.0) + (ex_pnl - ex_pnl_prev)
            if want_cells:
                cells[(ex, int(ts // 3600))][0] += ex_pnl - ex_pnl_prev
            ex_pnl_prev = ex_pnl
        pnl_total += ex_pnl_prev
    # drawdown of the aggregate marked path (per-exchange increments summed in time order)
    cum = 0.0
    for t in sorted(path):
        cum += path[t]
        peak = max(peak, cum); dd = max(dd, peak - cum)
    cap_days = cap_hours / 24.0
    extra = {"_cells": dict(cells)} if want_cells else {}
    return dict(pnl=pnl_total, cap_days=cap_days, drawdown=dd, score=_score(pnl_total, cap_days), edge3h=edge3h,
                score_3h=_score(edge3h, cap_days), **extra, fill_events=round(n_fill_events, 1),
                fill_qty=round(fill_qty), markout30_ticks=(mo_w[0] / mo_w[1] if mo_w[1] > 0 else None), n_markout=len(markouts),
                by_level={lv: {"fill_qty": round(lvl_fill[lv]), "markout30_ticks": (round(lvl_mo[lv][0] / lvl_mo[lv][1], 2) if lvl_mo[lv][1] else None)}
                          for lv in sorted(lvl_fill)},
                max_inv_shares=round(max_inv_shares), max_inv_notional=round(max_inv_notional), max_inv_collateral_total=round(max_coll_total))


# ================================================================================================= DIRECTION REPLAY
class DirData:
    def __init__(self, con: sqlite3.Connection, t0: float, t1: float):
        self.polls: list[tuple[float, list]] = []
        by_ts: dict[float, list] = defaultdict(list)
        for r in con.execute("SELECT ts, race, party, exchange_id, pm, kalshi, fair, pm_w, k_w, sig_bid, sig_ask, sig_mid FROM venue_fair "
                             "WHERE ts>=? AND ts<=? AND fair IS NOT NULL AND sig_bid IS NOT NULL AND sig_ask IS NOT NULL ORDER BY ts", (t0, t1)):
            by_ts[r["ts"]].append(tuple(r))
        self.polls = [(t, by_ts[t]) for t in sorted(by_ts)]
        self.allow: dict[tuple, str] = {}
        try:
            legs = json.load(open(os.path.join(ROOT, "params", "fair_explained.json"))).get("legs", {})
            for key, v in legs.items():
                if v.get("verdict") in ("trade_yes", "trade_no"):
                    self.allow[(key.rsplit(" [", 1)[0], v.get("party"))] = "yes" if v["verdict"] == "trade_yes" else "no"
        except Exception:
            pass
        self.ok = len(self.polls) >= 5 and bool(self.allow)


def simulate_direction(data: DirData, polls: list, min_edge: float, band: float, total_cap: float = DIR_TOTAL_CAP_MAX) -> dict:
    """Value takes on verdict legs, one leg per race, zero-EV exits within `band` of fair. Starts flat.
    Live behaviour mirrored: the sleeve's cumulative entry cost counts against the caps and is NOT credited back on exit."""
    held: dict[str, dict] = {}       # exchange -> dict(side, qty, px, race)
    spent_total, realised = 0.0, 0.0
    spent_leg: dict[str, float] = defaultdict(float)
    cap_hours = 0.0
    peak_f = dd_f = peak_m = dd_m = 0.0
    entries = exits = 0
    last_fair: dict[str, float] = {}
    last_mid: dict[str, float] = {}
    if not polls:
        return dict(pnl=0.0, pnl_mid=0.0, cap_days=0.0, drawdown=0.0, drawdown_mid=0.0, score=0.0, entries=0, exits=0)
    prev = polls[0][0]
    for ts, rows in polls:
        cap_hours += sum(h["qty"] * h["px"] for h in held.values()) * (ts - prev) / 3600.0
        prev = ts
        races_done = {h["race"] for h in held.values()}
        for (t, race, party, ex, pm, kal, fair, pm_w, k_w, bid, ask, mid) in rows:
            last_fair[ex], last_mid[ex] = fair, mid
            # exits first
            h = held.get(ex)
            if h is not None:
                if h["side"] == "no" and ask <= fair + band:            # held NO (short YES at px): buy YES at ask
                    realised += h["qty"] * (h["px"] - ask); exits += 1; del held[ex]; continue
                if h["side"] == "yes" and bid >= fair - band:           # held YES: sell at bid
                    realised += h["qty"] * (bid - h["px"]); exits += 1; del held[ex]; continue
                continue
            side_ok = data.allow.get((race, party))
            if side_ok is None or race in races_done:
                continue
            disagree = abs(pm - kal) if (pm is not None and kal is not None and (pm_w or 0) > 0 and (k_w or 0) > 0) else 0.0
            if disagree > DIR_MAX_DISAGREE:
                continue
            n_venues = int((pm_w or 0) > 0) + int((k_w or 0) > 0)
            need = min_edge + DIR_DISAGREE_MULT * disagree + (DIR_SINGLE_VENUE_EXTRA if n_venues == 1 else 0.0)
            if side_ok == "yes" and fair - ask >= need:
                edge, price = fair - ask, ask
            elif side_ok == "no" and bid - fair >= need:
                edge, price = bid - fair, round(1 - bid, 3)
            else:
                continue
            room = min(DIR_PER_LEG_CAP - spent_leg[ex], total_cap - spent_total)
            cost = min(DIR_PER_LEG_CAP * min(1.0, edge / DIR_EDGE_FULL), room)
            qty = int(cost / max(price, 0.005))
            if qty < DIR_MIN_QTY:
                continue
            # position in YES terms: YES held at ask, or NO held = short YES at bid
            held[ex] = dict(side=side_ok, qty=qty, px=(ask if side_ok == "yes" else bid), race=race, cost=qty * price)
            spent_leg[ex] += qty * price; spent_total += qty * price; entries += 1; races_done.add(race)
        mark_f = realised + sum(h["qty"] * ((last_fair[e] - h["px"]) if h["side"] == "yes" else (h["px"] - last_fair[e])) for e, h in held.items())
        mark_m = realised + sum(h["qty"] * ((last_mid[e] - h["px"]) if h["side"] == "yes" else (h["px"] - last_mid[e])) for e, h in held.items())
        peak_f = max(peak_f, mark_f); dd_f = max(dd_f, peak_f - mark_f)
        peak_m = max(peak_m, mark_m); dd_m = max(dd_m, peak_m - mark_m)
    cap_days = cap_hours / 24.0
    return dict(pnl=mark_f, pnl_mid=mark_m, cap_days=cap_days, drawdown=dd_f, drawdown_mid=dd_m, score=_score(mark_f, cap_days),
                score_mid=_score(mark_m, cap_days), entries=entries, exits=exits, open_legs=len(held))


# ================================================================================================= TUNER
class ShadowTuner:
    """Scheduled walk-forward replays per parameter group; applies only out-of-sample-validated changes inside HARD_BOUNDS,
    and only when shadow.auto_apply is true. One group runs at a time, in a daemon thread; tick() never blocks the cycle."""

    def __init__(self, db_path: str, cfg, root: str = ROOT, cpu_budget_sec: float = 8.0, auto_apply: bool = False,
                 material_per_day: float = 50.0, material_frac: float = 0.10, min_confidence: float = MIN_CONFIDENCE,
                 confirm_runs: int = CONFIRM_RUNS, enabled: bool = True, schedule: Optional[dict] = None, run_allocator: bool = True):
        self.db_path, self.cfg, self.root = db_path, cfg, root
        self.cpu_budget_sec, self.auto_apply = cpu_budget_sec, auto_apply
        self.material_per_day, self.material_frac, self.min_confidence = material_per_day, material_frac, min_confidence
        self.confirm_runs, self.enabled, self.run_allocator = max(1, int(confirm_runs)), enabled, run_allocator
        self.schedule = {g: dict(v) for g, v in SCHEDULE.items()}
        for g, v in (schedule or {}).items():
            if g in self.schedule and isinstance(v, dict):
                self.schedule[g].update({k: float(x) for k, x in v.items() if k in ("every_h", "lookback_h")})
        self.last_run: dict[str, float] = {}
        self.streak: dict[str, tuple] = {}          # key -> (json value, confirming runs, end of the last counted test window)
        self.bot = None
        self.startup_values: Optional[dict] = None
        self.report: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None
        self._pending: list = []
        self._lock = threading.Lock()
        self._load_schedule_state()

    @staticmethod
    def from_config(cfg, root: str = ROOT) -> "ShadowTuner":
        raw = {}
        try:
            raw = json.load(open(os.path.join(root, "config.json"))).get("shadow") or {}
        except Exception:
            pass
        return ShadowTuner(os.path.join(root, cfg.db_path), cfg, root, cpu_budget_sec=float(raw.get("cpu_budget_sec", 8.0)),
                           auto_apply=bool(raw.get("auto_apply", False)), material_per_day=float(raw.get("material_per_day", 50.0)),
                           material_frac=float(raw.get("material_frac", 0.10)), min_confidence=float(raw.get("min_confidence", MIN_CONFIDENCE)),
                           confirm_runs=int(raw.get("confirm_runs", CONFIRM_RUNS)), enabled=bool(raw.get("enabled", True)),
                           schedule=raw.get("schedule"), run_allocator=bool(raw.get("run_allocator", True)))

    # ----------------------------------------------------------------------------------------- schedule state
    def _state_path(self) -> str:
        return os.path.join(self.root, "params", "shadow_state.json")

    def _load_schedule_state(self):
        """Last run time and confirmation streaks survive restarts, so a restart neither re-runs every group at once nor
        forgets a half-confirmed recommendation."""
        try:
            st = json.load(open(self._state_path()))
            self.last_run = {g: float(t) for g, t in (st.get("last_run") or {}).items() if g in self.schedule}
            self.streak = {k: (v[0], int(v[1]), float(v[2]) if len(v) > 2 else 0.0) for k, v in (st.get("streak") or {}).items()}
        except (OSError, ValueError, TypeError, IndexError):
            pass

    def _save_schedule_state(self):
        try:
            tmp = self._state_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"last_run": dict(self.last_run), "streak": {k: list(v) for k, v in dict(self.streak).items()}}, f, indent=1)
            os.replace(tmp, self._state_path())
        except OSError as e:
            log.warning("shadow state not saved: %s", e)

    def due_groups(self, now: Optional[float] = None) -> list:
        """Groups whose interval has elapsed, most overdue first."""
        now = time.time() if now is None else now
        due = []
        for g, v in self.schedule.items():
            if v.get("every_h", 0) <= 0:
                continue
            late = now - self.last_run.get(g, 0.0) - 3600.0 * v["every_h"]
            if late >= 0:
                due.append((late, g))
        return [g for _, g in sorted(due, reverse=True)]

    # ----------------------------------------------------------------------------------------- core run
    def run(self, hours: Optional[float] = None, groups: Optional[list] = None) -> dict:
        """Evaluate the given groups (default: all) and merge the result into params/shadow_report.json."""
        groups = list(groups or self.schedule)
        cpu0, wall0 = time.thread_time(), time.time()
        current = live_params(self.cfg, self.bot)
        try:
            rep = json.load(open(os.path.join(self.root, "params", "shadow_report.json")))
            if "groups" not in rep:
                rep = {}
        except (OSError, ValueError):
            rep = {}
        rep.setdefault("groups", {})
        recs = []
        con = open_ro(self.db_path)
        try:
            for g in groups:
                gcpu = time.thread_time()
                try:
                    grep_ = self._run_group(con, g, current, hours)
                except Exception as e:      # one broken group must not stop the others
                    log.exception("shadow group %s failed: %s", g, e)
                    grep_ = {"error": str(e)}
                grep_["cpu_sec"] = round(time.thread_time() - gcpu, 2)
                grep_["ran_at"] = _iso(time.time())
                rep["groups"][g] = grep_
                recs.extend(grep_.get("recommendations", []))
        finally:
            con.close()
        rep["generated_at"] = _iso(time.time())
        rep["cpu_sec_last_run"] = round(time.thread_time() - cpu0, 2)
        rep["wall_sec_last_run"] = round(time.time() - wall0, 2)
        rep["groups_last_run"] = groups
        rep["auto_apply"] = self.auto_apply
        rep["rules"] = {"fit_frac": FIT_FRAC, "test_blocks": TEST_BLOCKS, "min_confidence": self.min_confidence, "confirm_runs": self.confirm_runs,
                        "material": f"max(10, {self.material_per_day}/day of test window, {self.material_frac:.0%} of |current score|); "
                                    f"MM: max({MM_MATERIAL_FLOOR}, {MM_MATERIAL_TICKS_PER_SHARE} tick x test shares filled, {self.material_frac:.0%} of |score|)",
                        "confirmations": "same value, non-overlapping test windows",
                        "hurdle_per_1k_day": HURDLE_PER_1K_DAY, "schedule": self.schedule}
        rep["limitations"] = LIMITATIONS
        rep["recommendations"] = [r for gg in rep["groups"].values() for r in gg.get("recommendations", [])]
        self.report = rep
        self._write(rep)
        rep["_new_recommendations"] = recs
        return rep

    def _window(self, con, hours: float) -> tuple:
        t1 = time.time()
        t0 = t1 - 3600.0 * hours
        first = con.execute("SELECT MIN(ts) FROM quotes").fetchone()[0] or t1
        t0 = max(t0, float(first))
        return t0, t0 + FIT_FRAC * (t1 - t0), t1

    def _run_group(self, con, g: str, current: dict, hours: Optional[float]) -> dict:
        cpu0 = time.thread_time()
        keys = self.schedule[g]["keys"]
        t0, fit_until, t1 = self._window(con, hours or self.schedule[g]["lookback_h"])
        out: dict[str, Any] = {"window": {"from": _iso(t0), "fit_until": _iso(fit_until), "to": _iso(t1), "hours": round((t1 - t0) / 3600, 1),
                                          "test_hours": round((t1 - fit_until) / 3600, 1)}, "params": {}, "notes": [], "skipped": []}
        blocks = self._blocks(fit_until, t1)

        def budget_left():
            return time.thread_time() - cpu0 < self.cpu_budget_sec

        if g == "arb":
            ad = ArbData(con, t0, t1)
            if not ad.ok:
                out["notes"].append("arb replay skipped: no venue_quotes data in window" + ("" if _bta else " (sigbot.backtest_arb missing)"))
                return out
            fitp = [p for p in ad.opps if p[0] < fit_until]; testp = [p for p in ad.opps if p[0] >= fit_until]
            out["data"] = {"polls": len(ad.polls), "fit_polls": len(fitp), "test_polls": len(testp), "races": len(ad.races), "assumed_size": ARB_ASSUMED_SIZE}

            def runner(params, polls):
                return simulate_arb(ad, polls, params["arb.min_ret"], params["arb.max_race_frac"], params["arb.free_unwinds"],
                                    params["arb.recycle_min_gain"], self.cfg.starting_cash, float(self.cfg.arb.capital_frac))
            pick = lambda a, b: [p for p in ad.opps if a <= p[0] < b]
            both = False
        elif g == "direction":
            dd = DirData(con, t0, t1)
            if not dd.ok:
                out["notes"].append("direction replay skipped: no venue_fair rows or no verdict legs in window")
                return out
            fitp = [p for p in dd.polls if p[0] < fit_until]; testp = [p for p in dd.polls if p[0] >= fit_until]
            out["data"] = {"polls": len(dd.polls), "fit_polls": len(fitp), "test_polls": len(testp), "verdict_legs": len(dd.allow)}
            cash = float(getattr(self.bot, "cash", 0.0) or 0.0) or self.cfg.starting_cash
            total_cap = min(DIR_TOTAL_CAP_MAX, current.get("direction.overall_cash_frac", 0.04) * cash)

            def runner(params, polls):
                return simulate_direction(dd, polls, params["direction.min_edge_take"], params["exit_band"], total_cap)
            pick = lambda a, b: [p for p in dd.polls if a <= p[0] < b]
            both = True
        else:   # mm_quote / mm_ladder
            md = MMData(con, t0, t1, fit_until, self.cfg)
            out["calibration"] = md.calib
            if not md.ok:
                out["notes"].append(f"mm replay skipped: {md.n_snaps} snapshots, {md.calib.get('fills', 0)} fills in the fit window (need >= 50 / 5)")
                return out
            out["data"] = {"snapshots": md.n_snaps, "exchanges": len(md.snaps), "tape_rows": sum(len(v) for v in md.tape.values())}
            mmp = lambda params: {k.split(".", 1)[1]: v for k, v in params.items() if k.startswith("mm.")}
            live_sim = simulate_mm(md, t0, fit_until, dict(mmp(current), base_qty=md.base_qty_fit))
            out["calibration"]["replay_fit_fill_qty"] = live_sim["fill_qty"]
            out["calibration"]["actual_fit_fill_qty"] = round(sum(float(f["q"]) for f in md.fills))
            fitp, testp = (t0, fit_until), (fit_until, t1)

            def runner(params, window):
                return simulate_mm(md, window[0], window[1], dict(mmp(params), cells=True))
            pick = lambda a, b: (a, b)
            both = "mm"
            if "mm.meanrev_max_ticks" in keys:
                out["notes"].append(f"mm.meanrev_max_ticks is report-only: values above the live cap {current['mm.meanrev_max_ticks']} cannot be "
                                    "evaluated (the logged fair carries only the capped skew), so lowering it could never be undone by the tuner")
        recs = []
        for key in keys:
            if not budget_left():
                out["skipped"].append(key); continue
            res = self._evaluate(key, current, runner, fitp, testp, blocks, pick, both_marks=both)
            # confirmation streak: the same value must be recommended by `confirm_runs` runs of this group whose TEST windows
            # do not overlap (a run that re-tests the previous confirming run's window neither counts nor resets the streak)
            if res["recommend"]:
                v = json.dumps(res["best"])
                prev = self.streak.get(key)
                if prev and prev[0] == v and fit_until < prev[2] - 1e-6:
                    n = prev[1]
                    self.streak[key] = (v, n, prev[2])
                    res["confirmations"] = n
                    res["reason"] = f"recommended; test window overlaps the last confirming run's ({n}/{self.confirm_runs})"
                    out["params"][key] = res
                    continue
                n = prev[1] + 1 if prev and prev[0] == v else 1
                self.streak[key] = (v, n, t1)
                res["confirmations"] = n
                if n >= self.confirm_runs:
                    recs.append({"key": key, "from": res["current"], "to": res["best"], "oos_gain": res["oos_gain"], "confidence": res["confidence"],
                                 "group": g, "confirmations": n})
                else:
                    res["reason"] = f"recommended, awaiting confirmation ({n}/{self.confirm_runs})"
            else:
                self.streak.pop(key, None)
                res["confirmations"] = 0
            out["params"][key] = res
        out["recommendations"] = recs
        return out

    @staticmethod
    def _blocks(a: float, b: float) -> list:
        w = (b - a) / TEST_BLOCKS
        return [(a + i * w, a + (i + 1) * w) for i in range(TEST_BLOCKS)]

    def _evaluate(self, key: str, current: dict, runner, fit, test, blocks, pick, both_marks: bool = False) -> dict:
        """Coordinate search for one parameter: pick the fit-window best (ties keep the current value), score it on the test
        window, and test the per-block score differences with a bootstrap (confidence = P(mean difference > 0))."""
        cur_val = current[key]
        grid = grid_for(key, cur_val)
        if not any(json.dumps(v) == json.dumps(cur_val) for v in grid):
            grid = [cur_val] + grid
        fit_scores = {}
        for v in grid:
            params = dict(current); params[key] = v
            fit_scores[json.dumps(v)] = runner(params, fit)["score"]
        top = max(fit_scores.values())
        cur_key = json.dumps(cur_val)
        best_key = cur_key if fit_scores[cur_key] >= top - 1e-9 else max(fit_scores, key=lambda k: fit_scores[k])
        best_val = json.loads(best_key)
        cur_params = dict(current)
        best_params = dict(current); best_params[key] = best_val
        r_cur, r_best = runner(cur_params, test), runner(best_params, test)
        mm = both_marks == "mm"
        mark = "score_3h" if mm else "score_mid"
        gain = r_best["score"] - r_cur["score"]
        gain_mid = (r_best.get(mark, r_best["score"]) - r_cur.get(mark, r_cur["score"])) if both_marks else gain
        same = best_key == cur_key
        diffs, diffs_mid = [], []
        cluster = None
        if not same and mm:
            # MM: test the WHOLE-window gain itself, clustered by market and by hour (flat-start block re-simulations measure a
            # different path and were found not to agree with the reported gain); both scores must pass
            cluster = {m: cluster_confidence(r_best["_cells"], r_cur["_cells"], m) for m in ("score", "score_3h")}
        elif not same:
            for (a, b) in blocks:
                sub = pick(a, b)
                rc, rb = runner(cur_params, sub), runner(best_params, sub)
                diffs.append(rb["score"] - rc["score"])
                diffs_mid.append(rb.get("score_mid", rb["score"]) - rc.get("score_mid", rc["score"]))
        confidence = bootstrap_confidence(diffs) if diffs else 0.0
        if both_marks and diffs:
            confidence = min(confidence, bootstrap_confidence(diffs_mid))
        wins = (sum(1.0 if d > 1e-9 else 0.5 if abs(d) <= 1e-9 else 0.0 for d in diffs) / len(diffs)) if diffs else 0.0
        if cluster is not None:
            confidence = min(c["confidence"] for c in cluster.values())
            wins = cluster["score"]["market_win_share"]
        test_days = max(1e-6, (blocks[-1][1] - blocks[0][0]) / 86400.0)
        if mm:   # per filled share: a day-sized floor is unreachable for a sleeve that fills ~1k shares an hour
            material = max(MM_MATERIAL_FLOOR, MM_MATERIAL_TICKS_PER_SHARE * TICK * max(r_cur.get("fill_qty", 0), r_best.get("fill_qty", 0)),
                           self.material_frac * abs(r_cur["score"]))
        else:
            material = max(10.0, self.material_per_day * test_days, self.material_frac * abs(r_cur["score"]))
        dd_ok = r_best["drawdown"] <= 1.5 * r_cur["drawdown"] + 25.0
        bounded = within_bounds(key, best_val)
        recommend = (not same and key not in REPORT_ONLY and bounded and gain >= material and gain_mid >= material
                     and confidence >= self.min_confidence and dd_ok)
        reason = ("unchanged" if same else "report-only" if key in REPORT_ONLY else "outside hard bounds" if not bounded
                  else "worse out of sample" if gain < 0 or gain_mid < 0 else "gain not material" if gain < material or gain_mid < material
                  else "not significant" if confidence < self.min_confidence else "drawdown worse" if not dd_ok else "recommended")
        out = {"current": cur_val, "best": best_val, "fit_scores": {k: round(v, 1) for k, v in fit_scores.items()},
               "test_current": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r_cur.items() if not k.startswith("_")},
               "test_best": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r_best.items() if not k.startswith("_")},
               "oos_gain": round(gain, 1), "material_threshold": round(material, 1), "confidence": round(confidence, 3),
               "block_wins": round(wins, 2), "block_diffs": [round(d, 1) for d in diffs], "recommend": recommend, "reason": reason}
        if mm:
            out["oos_gain_3h_mark"] = round(gain_mid, 1)
            out["cluster"] = cluster
        elif both_marks:
            out["oos_gain_mid_mark"] = round(gain_mid, 1)
        return out

    def _write(self, rep: dict):
        path = os.path.join(self.root, "params", "shadow_report.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: v for k, v in rep.items() if not k.startswith("_")}, f, indent=1)
        os.replace(tmp, path)

    # ----------------------------------------------------------------------------------------- bot integration
    def tick(self, bot=None) -> list:
        """Call once per bot cycle. On the first call, re-applies remembered automatic changes (auto_apply only). Starts the
        most overdue group in a daemon thread; on a later call logs its result and, when auto_apply is set, applies the
        confirmed out-of-sample recommendations. Also drives the allocator on its own schedule. Never blocks the cycle."""
        if not self.enabled:
            return []
        if bot is not None:
            self.bot = bot
            if self.startup_values is None:
                self.startup_values = {k: json.loads(json.dumps(tuned.get_value(bot, k))) for k in TUNABLE_KEYS if _has(bot, k)}
                if self.auto_apply:
                    done = tuned.reapply_on_start(bot, self.startup_values, within_bounds, "shadow")
                    if done:
                        log.warning("SHADOW re-applied remembered values for %s", done)
        applied: list = []
        with self._lock:
            pending, self._pending = self._pending, []
        for rep in pending:
            recs = rep.get("_new_recommendations", [])
            log.info("SHADOW %s done in %.1fs CPU: %d confirmed recommendation(s) %s", rep.get("groups_last_run"), rep.get("cpu_sec_last_run", 0),
                     len(recs), [(r["key"], r["from"], r["to"], r["oos_gain"], r["confidence"]) for r in recs])
            if recs and self.auto_apply and bot is not None:
                applied += self.apply(recs, bot)
            elif recs:
                log.info("SHADOW auto_apply is off: recommendations logged only")
        if self._thread is None or not self._thread.is_alive():
            due = self.due_groups()
            if due:
                g = due[0]
                self.last_run[g] = time.time()
                self._thread = threading.Thread(target=self._bg, args=([g],), name=f"shadow-{g}", daemon=True)
                self._thread.start()
        if self.run_allocator and bot is not None:
            try:
                from .allocator import tick as allocator_tick
                allocator_tick(bot, self.root)
            except Exception as e:  # the allocator must never affect the loop either
                log.warning("allocator tick: %s", e)
        return applied

    def _bg(self, groups: list):
        try:
            rep = self.run(groups=groups)
        except Exception as e:  # the tuner must never affect the loop
            log.exception("shadow run failed: %s", e)
            return
        finally:
            self._save_schedule_state()
        with self._lock:
            self._pending.append(rep)

    def apply(self, recs: list, bot) -> list:
        """Set confirmed recommendations on the live bot (inside bounds only) and remember them for restarts."""
        applied = []
        for r in recs:
            key, val = r["key"], r["to"]
            if key in REPORT_ONLY or key not in TUNABLE_KEYS or not within_bounds(key, val):
                continue
            if key in LADDER_GRIDS:
                sib = "mm.level_size_mult" if key == "mm.level_ticks" else "mm.level_ticks"
                try:
                    if len(val) != len(tuned.get_value(bot, sib)):
                        log.warning("SHADOW skip %s: %s does not match the live %s length", key, val, sib)
                        continue
                except Exception:
                    continue
            try:
                if json.dumps(tuned.get_value(bot, key)) != json.dumps(r["from"]):
                    log.warning("SHADOW skip %s: live value %s changed since the replay used %s", key, tuned.get_value(bot, key), r["from"])
                    continue
                tuned.record(bot, key, val, "shadow", {"oos_gain": r["oos_gain"], "confidence": r["confidence"], "group": r.get("group"),
                                                       "confirmations": r.get("confirmations")},
                             (self.startup_values or {}).get(key, r["from"]))
                self.streak.pop(key, None)
                applied.append(key)
                log.warning("SHADOW APPLIED %s: %s -> %s (oos gain %.0f, confidence %.2f)", key, r["from"], val, r["oos_gain"], r["confidence"])
            except Exception as e:
                log.warning("shadow apply %s failed: %s", key, e)
        if applied:
            self._save_schedule_state()
        return applied


def _has(bot, key: str) -> bool:
    try:
        tuned.get_value(bot, key)
        return True
    except Exception:
        return False


def bootstrap_confidence(diffs: list, n: int = BOOTSTRAP_N, seed: int = 7) -> float:
    """P(mean of the block differences > 0) under a bootstrap of the blocks (fixed seed, so reproducible).
    All-zero differences give 0.5; a single block gives 1 or 0."""
    if not diffs:
        return 0.0
    if all(abs(d) <= 1e-9 for d in diffs):
        return 0.5
    rng = random.Random(seed)
    k = len(diffs)
    pos = 0.0
    for _ in range(n):
        m = sum(diffs[rng.randrange(k)] for _ in range(k))
        pos += 1.0 if m > 1e-9 else (0.5 if abs(m) <= 1e-9 else 0.0)
    return pos / n


def cluster_confidence(cells_best: dict, cells_cur: dict, metric: str = "score", n: int = CLUSTER_BOOT_N, seed: int = 7) -> dict:
    """Confidence that a whole-window MM gain is real: per (market, hour) cell, candidate minus current contribution to
    `metric` ('score' = marked P&L change, 'score_3h' = 3-h-mark fill P&L, both minus the capital charge); the cell
    differences are summed per market and per hour and each set of cluster sums is bootstrapped (P(resampled total > 0));
    the reported confidence is the smaller of the two (two-way-conservative)."""
    def val(c):
        base = c[0] if metric == "score" else c[2]
        return base - HURDLE_PER_1K_DAY * (c[1] / 24.0) / 1000.0
    keys = set(cells_best) | set(cells_cur)
    zero = [0.0, 0.0, 0.0]
    by_m: dict = defaultdict(float)
    by_h: dict = defaultdict(float)
    for k in keys:
        d = val(cells_best.get(k, zero)) - val(cells_cur.get(k, zero))
        by_m[k[0]] += d
        by_h[k[1]] += d
    out = {"total": round(sum(by_m.values()), 2), "markets": len(by_m), "hours": len(by_h),
           "market_win_share": round(sum(1.0 for x in by_m.values() if x > 1e-9) / max(1, sum(1 for x in by_m.values() if abs(x) > 1e-9)), 2)}
    for name, d in (("market", by_m), ("hour", by_h)):
        out[f"{name}_conf"] = bootstrap_confidence(list(d.values()), n=n, seed=seed) if d else 0.0
    out["confidence"] = min(out["market_conf"], out["hour_conf"])
    return out


def tick(bot) -> list:
    """One-call wiring for run.py: creates the tuner on first use (config.json 'shadow' section), then ticks it."""
    t = getattr(bot, "_shadow_tuner", None)
    if t is None:
        t = ShadowTuner.from_config(bot.cfg, ROOT)
        bot._shadow_tuner = t
    return t.tick(bot)


LIMITATIONS = [
    "arbs: venue_quotes has no sizes, so every opportunity is assumed to offer ARB_ASSUMED_SIZE sets at the touch; our own fills do not move later polls; "
    "no allOrNone rejections or latency; polls are 60-120 s apart so intra-poll opportunities are invisible; buy-set profit is expected (x risk), not locked.",
    "mm: fills = our calibrated share of the public tape volume at or through our price, per distance-from-mid bucket, from OUR OWN orders in the fit "
    "window (same queue position as live, no size effect: a bigger order does not get a bigger share); the tape includes the bot's real fills, so "
    "volume that only traded because we were there is counted; distances never quoted live (5+ ticks) fall back to the pooled share; "
    "race-consistency caps, same-side pauses, waves, news pauses and analytics overrides "
    "are not replayed; meanrev caps above the live cap are indistinguishable from it; the vol gate uses only the 15-minute move rule; "
    "simulated inventory starts flat on every exchange; P&L is marked at the latest mid in the window (inventory risk shows in drawdown, not in P&L).",
    "direction: taker fills assumed at the logged SIG touch regardless of size; outside depth scaling not replayed; entries limited to the verdict legs "
    "in params/fair_explained.json as the live sleeve is; cumulative entry cost is never credited back on exit (live behaviour); P&L marked at the latest "
    "outside fair (primary) and the latest SIG mid (secondary).",
    "all: one window of hours, walk-forward 70/30 by time; arb/direction confidence is block consistency on the test window; MM confidence is a "
    "market- and hour-clustered bootstrap of the whole-window gain (cells share inventory paths, so it is approximate).",
]


# ================================================================================================= CLI
def _fmt_val(v) -> str:
    if isinstance(v, list):
        return "[" + ",".join(str(x) for x in v) + "]"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def format_report(rep: dict) -> str:
    r = rep.get("rules", {})
    L = [f"shadow report {rep.get('generated_at')} UTC  last run {rep.get('groups_last_run')} cpu {rep.get('cpu_sec_last_run')} s  "
         f"auto_apply {'ON' if rep.get('auto_apply') else 'off'}  test: {r.get('test_blocks')} blocks, bootstrap conf >= {r.get('min_confidence')}, "
         f"{r.get('confirm_runs')} confirming runs"]
    L.append(f"{'parameter':26s} {'current':>16s} {'best(fit)':>16s} {'test cur':>9s} {'test best':>9s} {'oos gain':>9s} {'need':>6s} {'conf':>5s} "
             f"{'dd cur/best':>12s}  verdict")
    for g, gr in rep.get("groups", {}).items():
        w = gr.get("window", {})
        sch = r.get("schedule", {}).get(g, {})
        L.append(f"-- {g}: ran {gr.get('ran_at')} UTC, every {sch.get('every_h')} h on {w.get('hours')} h (test {w.get('test_hours')} h), cpu {gr.get('cpu_sec')} s"
                 + (f"  ERROR {gr['error']}" if gr.get("error") else ""))
        for key, p in gr.get("params", {}).items():
            tc, tb = p["test_current"], p["test_best"]
            L.append(f"{key:26s} {_fmt_val(p['current']):>16s} {_fmt_val(p['best']):>16s} {tc['score']:9.1f} {tb['score']:9.1f} {p['oos_gain']:+9.1f} "
                     f"{p['material_threshold']:6.0f} {p['confidence']:5.2f} {tc['drawdown']:5.0f}/{tb['drawdown']:<6.0f} {p['reason']}"
                     + (f" [mid-mark gain {p['oos_gain_mid_mark']:+.1f}]" if "oos_gain_mid_mark" in p else "")
                     + (f" [3h-mark gain {p['oos_gain_3h_mark']:+.1f}]" if "oos_gain_3h_mark" in p else ""))
        c = gr.get("calibration")
        if c:
            L.append(f"   mm fill model: replay of live params over the fit window filled {c.get('replay_fit_fill_qty')} shares vs actual "
                     f"{c.get('actual_fit_fill_qty')}; participation by ticks from mid {c.get('participation')}")
        for n in gr.get("notes", []):
            L.append("   note: " + n)
        if gr.get("skipped"):
            L.append(f"   skipped (cpu budget): {gr['skipped']}")
    if rep.get("recommendations"):
        L.append("CONFIRMED: " + "; ".join(f"{x['key']} {_fmt_val(x['from'])} -> {_fmt_val(x['to'])} (+{x['oos_gain']:.0f}, conf {x['confidence']:.2f})"
                                          for x in rep["recommendations"]))
    else:
        L.append("CONFIRMED: nothing (no change clears bounds, out-of-sample gain, significance, drawdown and confirmation)")
    return "\n".join(L)


def size_replay(tuner: ShadowTuner, sizes=(100, 200, 400), caps=None, hours: float = 72.0, current: int = 200) -> dict:
    """MM base_qty replay over the lookback with both size models; OOS test of each size against `current`. Report only:
    base_qty is never tuned automatically (size steps follow Sizhe's markout rule)."""
    caps = caps or {100: 600.0, 200: 900.0, 400: 1500.0}
    con = open_ro(tuner.db_path)
    try:
        t0, fit_until, t1 = tuner._window(con, hours)
        md = MMData(con, t0, t1, fit_until, tuner.cfg)
    finally:
        con.close()
    base = {k.split(".", 1)[1]: v for k, v in live_params(tuner.cfg, tuner.bot).items() if k.startswith("mm.")}
    out = {"window": {"from": _iso(t0), "fit_until": _iso(fit_until), "to": _iso(t1)}, "calibration_mean_order_qty": md.calib.get("mean_order_qty"),
           "capital_frac": float(tuner.cfg.mm.capital_frac), "n_markets": tuner.cfg.mm.n_markets, "models": {}}
    blocks = tuner._blocks(fit_until, t1)
    for model in ("fixed", "prop"):
        res = {}
        for q in sizes:
            p = dict(base, base_qty=q, max_inventory_notional=caps.get(q, base.get("max_inventory_notional", 900)), size_model=model)
            full, test = simulate_mm(md, t0, t1, p), simulate_mm(md, fit_until, t1, p)
            res[q] = {"full": full, "test": test, "params": p}
        for q in sizes:
            if q == current:
                continue
            pc, pq = res[current]["params"], res[q]["params"]
            diffs = [simulate_mm(md, a, b, pq)["score"] - simulate_mm(md, a, b, pc)["score"] for a, b in blocks]
            gain = res[q]["test"]["score"] - res[current]["test"]["score"]
            test_days = (t1 - fit_until) / 86400.0
            material = max(10.0, tuner.material_per_day * test_days, tuner.material_frac * abs(res[current]["test"]["score"]))
            conf = bootstrap_confidence(diffs)
            dd_ok = res[q]["test"]["drawdown"] <= 1.5 * res[current]["test"]["drawdown"] + 25.0
            res[q]["vs_current"] = {"oos_gain": round(gain, 1), "material": round(material, 1), "confidence": round(conf, 3), "drawdown_ok": dd_ok,
                                    "block_diffs": [round(d, 1) for d in diffs],
                                    "passes": gain >= material and conf >= tuner.min_confidence and dd_ok}
        out["models"][model] = res
    return out


def format_size_replay(r: dict) -> str:
    L = [f"MM size replay {r['window']['from']} .. {r['window']['to']} UTC (test from {r['window']['fit_until']}), capital_frac {r['capital_frac']}, "
         f"{r['n_markets']} markets; calibration orders averaged {r['calibration_mean_order_qty']} shares by ticks from mid"]
    for model, res in r["models"].items():
        L.append(f"-- size model '{model}' ({'share of passing volume independent of size: lower bound' if model == 'fixed' else 'share scales with size: upper bound'})")
        L.append(f"{'base':>5s} {'inv cap':>7s} {'fills':>7s} {'pnl':>7s} {'mo30':>6s} {'max inv sh':>10s} {'max inv $':>9s} {'tot inv $':>9s}  "
                 f"{'per level fills / mo30 (ticks)':40s}  test pnl  vs current")
        for q, x in res.items():
            f = x["full"]
            lv = " ".join(f"L{k}:{v['fill_qty']}/{v['markout30_ticks'] if v['markout30_ticks'] is not None else '-'}" for k, v in f["by_level"].items())
            vs = x.get("vs_current")
            vs_s = (f"{vs['oos_gain']:+.1f} (need {vs['material']:.0f}, conf {vs['confidence']:.2f}) {'PASS' if vs['passes'] else 'fail'}") if vs else "current"
            L.append(f"{q:5d} {x['params']['max_inventory_notional']:7.0f} {f['fill_qty']:7d} {f['pnl']:+7.1f} "
                     f"{(f['markout30_ticks'] or 0):+6.2f} {f['max_inv_shares']:10d} {f['max_inv_notional']:9d} {f['max_inv_collateral_total']:9d}  "
                     f"{lv:40s}  {x['test']['pnl']:+7.1f}  {vs_s}")
    return "\n".join(L)


def main():
    from .config import BotConfig
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="print the current report (runs all groups first if none exists)")
    ap.add_argument("--run", action="store_true", help="run the replays now")
    ap.add_argument("--groups", default=None, help="comma-separated groups to run (default all): " + ",".join(SCHEDULE))
    ap.add_argument("--hours", type=float, default=None, help="lookback override for every group")
    ap.add_argument("--cpu", type=float, default=None, help="cpu budget override per group, seconds")
    ap.add_argument("--sizes", default=None, help="MM base_qty replay, e.g. 100,200,400 (report only)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = BotConfig.load()
    tuner = ShadowTuner.from_config(cfg, ROOT)
    if a.cpu:
        tuner.cpu_budget_sec = a.cpu
    if a.sizes:
        r = size_replay(tuner, tuple(int(x) for x in a.sizes.split(",")), hours=a.hours or 72.0, current=int(cfg.mm.base_qty))
        print(json.dumps(r, indent=1, default=str) if a.json else format_size_replay(r))
        return
    rep = None
    if not a.run and os.path.exists(REPORT_PATH):
        rep = json.load(open(REPORT_PATH))
        if "groups" not in rep:
            rep = None          # report from the pre-schedule version
    if rep is None or a.run:
        rep = tuner.run(hours=a.hours, groups=a.groups.split(",") if a.groups else None)
    print(json.dumps({k: v for k, v in rep.items() if not k.startswith("_")}, indent=1) if a.json else format_report(rep))


if __name__ == "__main__":
    main()
