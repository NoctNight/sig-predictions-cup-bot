"""Per-strategy stops (Sizhe 24 Oct): instead of the bot-wide kill switch, pause only the strategy that is losing.

The bot-wide switch (drawdown > 400 vs the trailing-hour peak, or 30-min MM markout < -1) paused every sleeve for 30 min.
This module replaces it (config strategy_stops_enabled). Every pause stops NEW entries only: exits, unwinds, sell-alls
and cancels of stale quotes keep running, and the process is never stopped for P&L. Pauses are written to
params/strategy_kill.json (the strategy monitor's file, read by strategy_paused() before every entry), persist across
restarts and are cleared only by a human deleting the entry; the strategy is then re-armed from that moment.

Ledgers. Every fill is attributed when the stops evaluate (not when it was ingested: a basket's fills can be ingested
before its orders are recorded) by its order's purpose in the orders table; fills of orders the table does not know
are matched to the order ids in arb_trades responses (free unwinds record no orders); the rest are "untagged".
  mm        mm, inside                      catchup   catchup, catchup_exit       cheapfav  cheapfav, cheapfav_exit
  direction direction, news, tilt           shock     shock, shock_exit           punt      punt (attributed, no stop)
  election  election (election night: attributed and gated by "all"; no P&L limit unless stop_limits sets one)
  raise     raise_set, ri_trim, wind_down (the cash raise's tier-2 set unwinds, the RI trim and the 31 Oct-3 Nov wind-down's
            set clips: deliberate sales of sets below their payout, so set arb is not charged for them; attributed, no stop
            but rule 1b's 2,000-an-hour fall, which pauses exit:raise: the raise, the RI trim and the wind-down check it;
            the wind-down's own budget keeps its costs inside 200-300 an hour). Tier-1 clips (purpose raise) close holdings
            like a zero-EV exit (direction / tilt first, MM inventory next, anything beyond them in this ledger) and a
            clip that takes the account's position on a leg through zero pauses exit:raise (raise_cycle checks it)
  The 3 Nov punt executor must record its orders with purpose "punt": an unrecorded punt would be an untagged loss.
  sets      arb, hedge, arb_trades matches: one pooled ledger for set arb, recycles and high-frequency arb (they hold
            the same fungible sets); "arb" = the pooled ledger minus high-frequency arb's own ledger, both at settlement
            basis, so a churn round trip nets to zero for arb
  exit      zero-EV exits close direction/tilt holdings first, MM inventory with the rest (as zero_ev_exits sizes them)
P&L of a strategy = its YES-equivalent cash + open positions at the touch (bid for longs, ask for shorts); complete sets
inside a ledger at their payout. High-frequency arb uses its sleeve's own P&L (realised + open at the touch, what its
own stop uses); catch-up its fills ledger at the touch (its sleeve values open positions at the direction poll's mids,
which a jump can leave stale: sim zz_swing showed -667 for a few cycles on positions worth -27 at the touch).

Rules (each pause is logged once as KILL-STRATEGY <name> or KILL-ENTRIES):
  1. stop        P&L since armed <= limit (stop_limits; high-frequency arb / catch-up: churn_kill / catchup_kill), or a
                 drop >= kill_drawdown (400) from the strategy's own trailing-hour peak (cheap favourites and shock: their
                 limit): a single-strategy leak that trips today's 400 switch trips this too.
  2. markout     30-min MM markout < kill_markout_ticks on >= 20 fills pauses the MM only.
  3. sleeve      high-frequency arb / catch-up switched themselves off (their own stops): mirrored into the kill file.
  4. crossing    an exit or unwind fill took its own ledger through zero (closed more than the strategy held): the
                 owner's entries are paused and that exit path too (exit:<path>), so a buggy exit cannot keep flipping.
  5. untagged    P&L of untagged fills <= -untagged_limit, or unattributed loss >= unattributed_limit for 3 evaluations
                 in a row: the legs where it sits are found and every strategy that traded them since the baseline is
                 paused; if none can be named, ALL new entries are paused (KILL-ENTRIES).
  6. stuck       one of our orders still rests 10 min past its expiry (2 h if it has none): cancelled, and the owner's
                 entries paused (an exit order is only cancelled).
  7. bypass      orders tagged with a paused strategy's entry purpose placed 2+ min after its pause (a mis-tagged order
                 or an ungated path): ALL new entries are paused.
  8. ping-pong   one race bought and unwound alternately >= 4 times (>= 2 unwinds) within an hour (the recycler pump,
                 live 22 Oct 18:28-19:03: sets unwound at 0.97 and bought back at 0.98): set arb / recycles and high-
                 frequency arb are paused. Churn's 30-min re-entry gap allows at most 3 alternations an hour.
  9. combined    the strategies together fall >= 800 within an hour (a bug spread over several sleeves, each under its
                 own limit; today's switch fired at 400 for the whole account): each one still trading that lost >= 100
                 in that hour is paused (a strategy paused inside the hour counts up to its pause; never all entries).
Rules 1 and 9 judge one valuation, so a first breach only HOLDS that strategy's entries (in memory, read through
strategy_paused from that cycle: as quick as today's switch); it is written as a pause only if the next valuation
breaches too (one bad snapshot never makes a pause only a human clears). Crossings, untagged / unattributed (3
evaluations), stuck orders, bypass and ping-pong are definite and pause at once.
Safety: crossings are judged only on fills ingested after the stops first started (persisted; the record's own history
is never re-judged at deploy). After a position settles the unattributed check waits 10 min and re-baselines (a payout
is not a loss). An unparseable kill file (a hand edit in progress) is never rewritten and nothing is re-armed from it;
the stops decide from its last good content. A pause that disappears (cleared by hand, whoever wrote it) re-arms that
strategy from then. If the stops fail, the last pauses stay in force and nothing takes over (the bot-wide switch was
deleted 3 Nov, as its cancel_all could pull punt orders).
Quarantine: when a strategy other than the MM is paused, the MM keeps off that strategy's legs for kill_pause_sec (its
quotes there are pulled, as during a news pause), the part of today's bot-wide switch that protects the markets where the
loss is happening (sim churn_widen: an MM quote left on a dropped market blocked churn's all-or-none exit). Only entry
purposes (PULLABLE) are ever pulled or cancelled: never exits, punt, raise, hedge or election orders.
Unattributed loss = change in account value minus the change in every ledger's value, both at the venue's own marks
(so set accounting and MM-vs-set netting cannot create it), since a baseline taken at each restart. In YES terms it is
the cash the ledgers do not explain plus the new shares they do not explain (venue position minus every ledger's),
valued now, plus the untagged ledger's P&L.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

from . import strategy_monitor as SMON

log = logging.getLogger("sigbot")

STATE_FILE = "strategy_stops_state.json"
DEFAULT_LIMITS = {"hf_arb": -200.0, "catchup": -50.0, "cheapfav": -500.0, "mm": -300.0, "arb": -300.0, "shock": -3000.0,
                  "direction": -300.0}
# purpose -> ledger; "exit" is split between direction and mm, "sets" is the pooled set ledger
PURPOSE_LEDGER = {"mm": "mm", "inside": "mm", "catchup": "catchup", "catchup_exit": "catchup", "cheapfav": "cheapfav",
                  "cheapfav_exit": "cheapfav", "direction": "direction", "news": "direction", "tilt": "direction",
                  "shock": "shock", "shock_exit": "shock", "punt": "punt", "punt_exit": "punt", "election": "election", "election_exit": "election",
                  "raise": "exit", "volatile": "direction", "wind_down": "raise",
                  "raise_set": "raise", "ri_trim": "raise",
                  "arb": "sets", "hedge": "sets",
                  "exit": "exit"}
# entry purposes per stop key (the bypass check: none of these may be placed while the key is paused)
ENTRY_PURPOSES = {"mm": ("mm", "inside"), "catchup": ("catchup",), "cheapfav": ("cheapfav",),
                  "direction": ("direction", "news", "tilt"), "shock": ("shock",)}
# exit purposes -> (exit path key, owner stop keys)
EXIT_PATHS = {"exit": ("exit:zero_ev", ("mm", "direction")), "cheapfav_exit": ("exit:cheapfav", ("cheapfav",)),
              "catchup_exit": (None, ("catchup",)), "shock_exit": (None, ())}
# shock_exit / catchup_exit: no exit path to pause (shock's exit loop also holds its 3 Nov flatten, its own stop's closing
# and its time stop; catch-up must be flat by 3 Nov; nothing else closes their legs). A catch-up exit through zero pauses
# catch-up's entries; shock exits are clamped to the venue's own same-side position and shock nets its residuals itself
# (sk.netted), so they are not judged.
EXIT_PURPOSES = frozenset(EXIT_PATHS) | {"election"}   # resting orders the quarantine never pulls (election mixes both)
LEDGER_KEYS = ("mm", "catchup", "cheapfav", "direction", "shock", "punt", "election", "raise", "sets")
PURPOSE_LEDGER_EXITS = ("exit", "raise")       # purposes booked like a zero-EV exit (they close other sleeves' holdings)
# the trailing-hour drop that pauses a strategy is today's bot-wide 400 (kill_drawdown) for every strategy, so a leak in
# one strategy is stopped at least as fast as today's switch would stop it; except the two whose larger limits Sizhe set
# for swings they are meant to ride (cheap favourites -500 held through the call, shock -3k), which use their limit
WIDE_DROP = ("cheapfav", "shock")
PINGPONG_FLIPS, PINGPONG_UNWINDS = 4, 2
# resting orders the stops may cancel (stuck) or pull (quarantine): entries of the strategies they pause, and exits
# only when stuck. Never punt, raise, hedge, election or anything else (Sizhe-approved or residual-covering orders).
PULLABLE = frozenset({"mm", "inside", "tilt", "direction", "news", "catchup", "cheapfav", "shock"})
CANCEL_STUCK = PULLABLE | {"exit", "cheapfav_exit", "catchup_exit", "shock_exit"}
# bot-wide backstop: the strategies still trading together falling this much inside an hour (a bug spread over several
# sleeves, each under its own limit; today's switch fired at 400 for the whole account) pauses each of them that lost >= 100
COMBINED_DROP, COMBINED_SHARE = 800.0, 100.0
COMBINED_EXCLUDE = ("shock", "punt", "election", "raise")
HOLD_MIN = 60.0                 # ... and is confirmed only by a breach at least this much later (fast cycles: not 10 s)
HOLD_SEC = 150.0                # a first P&L breach holds entries this long (two cycles) while it is confirmed
SETTLE_QUIET = 600.0            # after a position settles, the unattributed check waits this long and re-baselines
UNWIND_KINDS = ("unwind_set", "unwind_buy_set")
CROSS_CONFIRM = 180.0           # a crossing must still hold this long after first seen (an unread fill shifts the drift)
JUDGE_LAG = 60.0                # a fill is judged (crossings) this long after its ingest: its order is recorded by then
WIDE_FALL = 2000.0              # the no-limit ledgers' (raise, punt) fall inside an hour that stops that path
SUSTAIN_SEC = 300.0             # ... and for this long (fills of a big taker order, e.g. the punt, ingest up to ~4.5 min late)
SUSTAIN = 3                     # evaluations in a row before an unattributed loss counts
BYPASS_GRACE = 60.0             # entry orders placed this long after their strategy's entries stopped (pause or hold), at
BYPASS_TIMES = 3                # >= this many distinct times, are a bypass: at most two writes can be in flight at a pause
                                # (the main loop's MM cycle and one scanner requote; retried for minutes on REQUEST_IN_FLIGHT),
                                # each one placement; a mis-tagged or ungated path keeps placing every cycle
STUCK_PAST_EXPIRY = 600.0
STUCK_NO_EXPIRY = 7200.0
STUCK_CONFIRM = 60.0            # ... and still listed this long after first seen so (after a sleep / clock jump the venue
                                # purges expired orders on its own; a stuck one stays)


def _ts(s) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class Ledger:
    """YES-equivalent ledger: pos[ex] signed shares (+ YES, - NO), cash = -sum(q * price_yes) over its fills."""

    def __init__(self):
        self.pos: dict = {}
        self.cash = 0.0
        self.last: dict = {}            # ex -> last fill price (YES terms), a fallback mark

    def add(self, ex: str, q: float, px: float):
        self.pos[ex] = self.pos.get(ex, 0.0) + q
        self.cash -= q * px
        self.last[ex] = px


def touch_value(led: Ledger, touch: dict, races: Optional[list] = None) -> float:
    """Cash + open positions at the touch (bid for longs, ask for shorts; the last fill price when a side is missing).
    races: lists of exchange ids of exhaustive races; a complete set inside the ledger is valued at its payout (one leg
    of every set pays 1: +c for c YES sets, -c for c NO sets in YES terms)."""
    pos = dict(led.pos)
    v = led.cash
    for legs in races or []:
        qs = [pos.get(e, 0.0) for e in legs]
        if len(qs) < 2:
            continue
        if all(q >= 1 for q in qs):
            c = min(qs)                     # c YES sets: one leg pays 1 per set
        elif all(q <= -1 for q in qs):
            c = max(qs)                     # -c NO sets: short one YES per set (n - 1 payout in NO terms)
        else:
            continue
        v += c
        for e in legs:
            pos[e] = pos[e] - c
    for ex, q in pos.items():
        if abs(q) < 1e-9:
            continue
        bid, ask = (touch.get(ex) or (None, None))
        px = (bid if q > 0 else ask)
        if px is None:
            px = led.last.get(ex, 0.0)
        v += q * px
    return v


def mid_value(led: Ledger, touch: dict) -> float:
    """Cash + open positions at the touch's mid (catch-up's own stop values at mids: at the touch its 2,000-share
    positions would show the spread as a loss on entry)."""
    v = led.cash
    for ex, q in led.pos.items():
        if abs(q) < 1e-9:
            continue
        bid, ask = (touch.get(ex) or (None, None))
        v += q * ((bid + ask) / 2 if bid is not None and ask is not None else led.last.get(ex, 0.0))
    return v


def mark_value(led: Ledger, marks: dict) -> float:
    return led.cash + sum(q * marks.get(ex, led.last.get(ex, 0.0)) for ex, q in led.pos.items() if abs(q) > 1e-9)


class StrategyStops:
    def __init__(self, cfg, store, params_dir: str, races: list, now: Optional[float] = None, write: bool = True):
        self.cfg, self.store, self.params_dir, self.write = cfg, store, params_dir, write
        self._races: list = []
        self.races = races
        self.kill_path = os.path.join(params_dir, SMON.KILL_FILE)
        self.state_path = os.path.join(params_dir, STATE_FILE)
        lim = dict(DEFAULT_LIMITS)
        # high-frequency arb and catch-up keep the stops they have ("as now"): their own churn_kill / catchup_kill
        for k, a in (("hf_arb", "churn_kill"), ("catchup", "catchup_kill")):
            if isinstance(getattr(cfg, a, None), (int, float)):
                lim[k] = float(getattr(cfg, a))
        lim.update(getattr(cfg, "stop_limits", None) or {})
        self.limits = lim
        self.drop_floor = float(getattr(cfg, "kill_drawdown", 400.0))
        self.unattributed_limit = float(getattr(cfg, "unattributed_limit", 2000.0))
        self.untagged_limit = float(getattr(cfg, "untagged_limit", 200.0))
        st = SMON._read_json(self.state_path, {}) or {}
        self.armed: dict = st.get("armed", {}) if isinstance(st.get("armed"), dict) else {}
        self.ours: dict = st.get("ours", {}) if isinstance(st.get("ours"), dict) else {}   # pauses this module wrote
        self.t_start = time.time() if now is None else now
        self.base: Optional[dict] = None          # unattributed baseline (resets at every restart)
        self.hist: dict = {}                      # key -> [(t, value)] trailing hour (memory)
        self.arb_ids: dict = {}                   # order id -> arb_trades kind (incremental)
        self.arb_seen = 0
        self.bad_streak = 0
        self.bad_since: Optional[float] = None
        self.un_since: Optional[float] = None
        self.localized: dict = {}                 # trigger -> loss when its suspects were paused (escalation level)
        self.quarantine: dict = {}                # ex -> time until which the MM keeps off it
        self.quarantine_sec = float(getattr(cfg, "kill_pause_sec", 1800))
        # crossings are judged on fills ingested after this: persisted, and on a first start every fill already in the
        # store is history (review 24 Oct: 43 crossings of 22 Oct re-judged at deploy would have paused six keys)
        sf = st.get("seen_fill")
        if not isinstance(sf, (int, float)):
            r = store.one("SELECT MAX(ingested_at) m FROM fills") if store is not None else None
            sf = float(r["m"]) if r and r["m"] is not None else 0.0
        self.seen_fill = float(sf)
        self.last: dict = {}
        self.good_kills: Optional[dict] = st.get("good_kills") if isinstance(st.get("good_kills"), dict) else None   # last parsed
        self.mem_kills: dict = {}                 # dry run: our pauses, kept in memory (nothing is written)
        self.cleared: dict = {str(k): float(v) for k, v in (st.get("cleared") or {}).items()} \
            if isinstance(st.get("cleared"), dict) else {}      # key -> when a human cleared it (persisted)
        tc = st.get("touch") if isinstance(st.get("touch"), dict) else {}
        self.touch_cache: dict = {str(k): tuple(v) for k, v in tc.items() if isinstance(v, list) and len(v) == 2}   # ex -> last two-sided touch
        self.settled_seen: set = set(st.get("settled") or []) if isinstance(st.get("settled"), list) else set()
        self.quiet_until = 0.0
        self.combo_hist: list = []                # [(t, {key: value})] trailing hour, for the bot-wide backstop
        self.warned_purposes: set = set()
        self.hold: dict = {}                      # key -> until: a first breach holds entries (in memory) for a cycle
        self.hold_start: dict = {}
        self.pending: dict = st.get("pending") if isinstance(st.get("pending"), dict) else {}   # decided, not yet written
        self.cross_pending: list = [tuple(c) for c in (st.get("cross_pending") or []) if isinstance(c, list) and len(c) == 8]
        self.un_streak = 0
        self.wide_hist: dict = {}
        self.stuck_seen: dict = {}
        self.bypass_seen: dict = {str(k): int(v) for k, v in (st.get("bypass_seen") or {}).items()} \
            if isinstance(st.get("bypass_seen"), dict) else {}  # key -> highest order id already reported as a bypass                # order id -> first evaluation it was seen resting past its expiry

    @property
    def races(self) -> list:
        return self._races

    @races.setter
    def races(self, races):
        self._races = [list(r) for r in races if len(r) >= 2]           # exhaustive races (lists of exchange ids)
        self.race_of = {e: legs for legs in self._races for e in legs}

    # ------------------------------------------------------------------ kill file
    def kills(self) -> Optional[dict]:
        """The kill file's entries; {} when it does not exist; None when it exists but does not parse (a half-edited file:
        the stops then decide from the last good copy and never rewrite it, as the monitor and KillFile do)."""
        if not os.path.exists(self.kill_path):
            d = {}
        else:
            d = SMON.normalise_kills(SMON._read_json(self.kill_path, None))
        if d is not None and not self.write:
            d = dict(d)
            d.update({k: v for k, v in self.mem_kills.items() if k not in d})
        return d

    def pause(self, kills: dict, key: str, reason: str, now: float, kind: str, extra: Optional[dict] = None) -> bool:
        if key in kills:
            return False
        e = {"ts": now, "utc": _utc(now), "reason": reason, "by": "strategy_stops", "rule": kind}
        e.update(extra or {})
        kills[key] = e
        if not self.write:
            self.mem_kills[key] = e
            self.ours[key] = now
        if key == "all":
            log.warning("KILL-ENTRIES: every strategy's NEW entries paused (exits continue): %s", reason)
        elif key.startswith("exit:"):
            log.warning("KILL-STRATEGY %s: this exit path paused (it closed more than was held): %s", key, reason)
        else:
            log.warning("KILL-STRATEGY %s: new entries paused (exits continue): %s", key, reason)
        return True

    # ------------------------------------------------------------------ attribution
    def _refresh_arb_ids(self):
        for r in self.store.rows("SELECT id, kind, response FROM arb_trades WHERE id > ? AND ok=1 ORDER BY id", (self.arb_seen,)):
            self.arb_seen = r["id"]
            try:
                resp = json.loads(r["response"] or "{}")
            except ValueError:
                continue
            for x in (resp.get("results") or []) if isinstance(resp, dict) else []:
                oid = (x.get("data") or {}).get("orderId")
                if oid is not None:
                    self.arb_ids[int(oid)] = r["kind"]

    def ledgers(self):
        """Every fill attributed now. Returns (ledgers {key: Ledger}, untagged Ledger, events) where events are the
        fills ingested since seen_fill as (ingested_at, purpose-or-kind, ledger key, ex, q, before, after). before/after
        are measured the way that exit path sizes itself: zero-EV exits on the gross holding per side of the sleeves they
        close (as zero_ev_exits does), cheap-favourite / catch-up / shock exits on their own ledger, unwinds on every
        ledger together (the venue's position, which unwinds are sized on); None for fills that are not exits."""
        self._refresh_arb_ids()
        if not hasattr(self, "judge_until"):
            self.judge_until = float("inf")
        L = {k: Ledger() for k in LEDGER_KEYS}
        untagged = Ledger()
        events = []
        allpos: dict = {}                      # every ledger together (~ the venue's position)
        recent_since = (self.judge_until + JUDGE_LAG - 3600) if self.judge_until != float("inf") else float("inf")
        # fills ingested since the previous evaluation: the ones that can sit between this cycle's positions read and
        # its ledgers (the crossing tolerance); not older ones (the first of a double unwind must not excuse the second)
        fresh_since = self.prev_eval if getattr(self, "prev_eval", None) is not None else float("inf")
        self.recent_fills = {}                    # ex -> [(ingested_at, |q|)] non-exit fills since the previous evaluation
        self.recent = {k: set() for k in LEDGER_KEYS}
        gross: dict = {}                       # (ex, side held) -> shares zero-EV exits may close
        # the order's purpose, else the fill's own (shock mode tags fills of orders whose outcome was unknown, which have
        # no order row, through fills.purpose: shock_claim)
        for f in self.store.rows("SELECT f.order_id, f.exchange_id, f.quantity, f.price, f.ingested_at, "
                                 "COALESCE(o.purpose, f.purpose) purpose FROM fills f "
                                 "LEFT JOIN orders o ON o.order_id = f.order_id WHERE f.price IS NOT NULL "
                                 "ORDER BY f.filled_at, f.id"):
            oid = int(f["order_id"]) if f["order_id"] is not None else None
            p = f["purpose"]
            kind = self.arb_ids.get(oid) if oid is not None else None
            if p is None and kind is not None:
                p = "arb"
            ex, q, px = str(f["exchange_id"]), float(f["quantity"]), float(f["price"])
            key = PURPOSE_LEDGER.get(p) if p is not None else None
            if p is not None and key is None and p not in self.warned_purposes:
                self.warned_purposes.add(p)
                log.warning("STRATEGY STOPS: fills of purpose %r have no owner in PURPOSE_LEDGER: counted as untagged", p)
            ing = f["ingested_at"] or 0.0
            new = self.seen_fill < ing <= self.judge_until
            if ing >= fresh_since and p not in EXIT_PURPOSES and p not in ("exit", "raise") and kind not in UNWIND_KINDS:
                self.recent_fills.setdefault(ex, []).append((ing, abs(q)))
            if key in self.recent and ing >= recent_since:
                self.recent[key].add(ex)
            elif key == "exit" and ing >= recent_since:
                self.recent["mm"].add(ex)
            all_before = allpos.get(ex, 0.0)
            allpos[ex] = all_before + q
            if key in ("mm", "direction"):
                gk = (ex, "yes" if q > 0 else "no")
                gross[gk] = gross.get(gk, 0.0) + abs(q)
            if key is None:
                untagged.add(ex, q, px)
                if new:
                    events.append((f["ingested_at"], p, None, ex, q, None, None))
                continue
            if key == "exit":
                # a zero-EV exit / raise clip closes direction/tilt holdings first, MM inventory next; a raise clip may
                # also sell holdings no sleeve owns (beyond the sets): that part goes to the raise ledger
                d, m = L["direction"], L["mm"]
                side_of = (lambda x: max(0.0, x)) if q < 0 else (lambda x: max(0.0, -x))   # the holdings this closes
                avail = sum(side_of(l_.pos.get(ex, 0.0)) for k_, l_ in L.items() if k_ not in ("catchup", "cheapfav", "shock")) \
                    + side_of(untagged.pos.get(ex, 0.0))
                gk = (ex, "no" if q > 0 else "yes")    # buying YES closes held NO, and vice versa
                before = gross.get(gk, 0.0)
                gross[gk] = before - abs(q)
                rest = q
                for led in (d, m):
                    h = led.pos.get(ex, 0.0)
                    if abs(rest) > 1e-9 and h * rest < 0:
                        take = math.copysign(min(abs(rest), abs(h)), rest)
                        led.add(ex, take, px)
                        rest -= take
                if abs(rest) > 1e-9:
                    # the cash raise and the wind-down sell single legs beyond the sets: a set ledger's residual on the
                    # leg (beyond its complete sets) is closed there, not left as a phantom position in set arb
                    h = self._residual(L["sets"], ex)
                    if h * rest < 0:
                        take = math.copysign(min(abs(rest), abs(h)), rest)
                        L["sets"].add(ex, take, px)
                        rest -= take
                if abs(rest) > 1e-9:
                    L["raise"].add(ex, rest, px)            # beyond every sleeve: the liquidation ledger
                if new:
                    if p == "raise":                    # sized on the venue's position beyond the sets
                        events.append((f["ingested_at"], "raise", "exit", ex, q, all_before, all_before + q))
                    else:
                        # zero-EV exits, offload and the wind-down's leg clips (all purpose exit) close what the ledgers
                        # other than catch-up / cheap favourites / shock hold on that side (review 24 Oct: judged on the
                        # account net of those three, a correct exit on a leg where cheap favourites sat on the same side
                        # and the MM or a set on the other read as a crossing): crossed only if it closes more than that
                        events.append((f["ingested_at"], "exit", "exit", ex, q, avail, avail - abs(q)))
                continue
            led = L[key]
            before = led.pos.get(ex, 0.0)
            if key == "raise" and kind in UNWIND_KINDS:
                # a liquidation set clip is booked against the set ledger's holding on the leg (as a mirror in this ledger,
                # so set arb is not charged), and the part beyond it closes direction / MM inventory the venue netted in
                hs = L["sets"].pos.get(ex, 0.0)
                cover = math.copysign(min(abs(q), abs(hs)), q) if hs * q < 0 else 0.0
                rest = q - cover
                for o_ in (L["direction"], L["mm"]):
                    h = o_.pos.get(ex, 0.0)
                    if abs(rest) > 1e-9 and h * rest < 0:
                        take = math.copysign(min(abs(rest), abs(h)), rest)
                        o_.add(ex, take, px)
                        rest -= take
                led.add(ex, cover + rest, px)
            else:
                led.add(ex, q, px)
            if new:
                if p in EXIT_PATHS:
                    events.append((f["ingested_at"], p, key, ex, q, before, before + q))
                elif key in ("sets", "raise") and kind in UNWIND_KINDS:
                    # the MM's own inventory on the leg is not what the unwind closed through zero (an MM bid fill between
                    # the unwind's positions read and its fill leaves the account long by exactly that)
                    others = [l_.pos.get(ex, 0.0) for k_, l_ in L.items() if k_ not in ("sets", "raise")]
                    adj = -sum(max(0.0, x) for x in others) if q > 0 else sum(max(0.0, -x) for x in others)
                    events.append((f["ingested_at"], "unwind", key, ex, q, all_before, all_before + q + adj))
                else:
                    events.append((f["ingested_at"], p, key, ex, q, None, None))
        return L, untagged, events

    # ------------------------------------------------------------------ values
    def values(self, L: dict, touch: dict, churn: Optional[dict]) -> dict:
        v = {}
        for k in ("mm", "cheapfav", "direction", "election", "punt"):
            v[k] = touch_value(L[k], touch)
        # shock at mids (a full-size entry walks a wide book: at the touch it reads -1.5..3k at once and SIG takes minutes
        # to follow); its own -2,500 stop (venue marks) is mirrored by rule 3
        v["shock"] = mid_value(L["shock"], touch)
        both = Ledger()                                   # sets + liquidations together: a sold set nets out at payout
        for l_ in (L["sets"], L["raise"]):
            both.cash += l_.cash
            for e_, q_ in l_.pos.items():
                both.pos[e_] = both.pos.get(e_, 0.0) + q_
            both.last.update(l_.last)
        v["raise"] = touch_value(both, touch, self.races) - touch_value(L["sets"], touch, self.races)
        v["catchup"] = mid_value(L["catchup"], touch)
        pooled = touch_value(L["sets"], touch, self.races)
        if churn is not None:
            # high-frequency arb at settlement basis (its sets at payout 1, two-leg races) to split the pooled ledger;
            # its ledger is restored from its state file at start, so this holds from the first cycle; its own value
            # (open at the touch) only once its cycle has valued it (None on the first cycle after a restart)
            settle = churn["realised"] + sum(l["qty"] * (1.0 - l["cost"]) for l in churn["lots"])
            v["arb"] = pooled - settle
            if churn.get("open") is not None:
                v["hf_arb"] = churn["realised"] + churn["open"]
        else:
            v["arb"] = pooled
        return v

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, now: float, *, touch: dict, cash: Optional[float] = None, positions: Optional[dict] = None,
                 mv: Optional[dict] = None, settled: Optional[set] = None, churn: Optional[dict] = None, churn_killed: bool = False, catchup_killed: bool = False, shock_killed: bool = False, fills_ok: bool = True, markout: Optional[tuple] = None,
                 open_orders: Optional[dict] = None) -> dict:
        """One evaluation. touch {ex: (bid, ask)}; cash/positions/mv: the venue's cash, signed positions and market values
        read together (None when a read failed: the unattributed check is skipped); churn {realised, open, lots}.
        Returns {"new": [(key, reason)], "cancel": [order ids], "status": {...}}."""
        read = self.kills()
        readable = read is not None
        if not readable:
            if not self.last.get("bad_file"):
                log.warning("STRATEGY STOPS: params/%s does not parse: deciding from the last good copy, writing nothing "
                            "until it is fixed", SMON.KILL_FILE)
            self.last["bad_file"] = True
            kills = dict(self.good_kills or {})
        else:
            self.last["bad_file"] = False
            kills = read
            # a pause that disappeared (a human cleared it; ours, the monitor's or a manual one): re-arm that strategy from
            # now, so the loss that paused it cannot re-pause it at once, and the ping-pong window restarts
            prev = self.good_kills if self.good_kills is not None else {k: None for k in self.ours}
            for k in list(prev):
                w = (prev.get(k) or {}) if isinstance(prev.get(k), dict) else {}
                if k not in kills and w.get("by") == "strategy_stops" and now - float(w.get("ts") or 0) < 120 \
                        and not w.get("reasserted") and self.write:
                    e2 = dict(w, reasserted=True)
                    self.pending[k] = e2                  # in force again and written below (the pending path)
                    log.warning("STRATEGY STOPS: %s was removed from params/%s %.0f s after the stops wrote it (an editor "
                                "saving a stale copy?): put back once; delete it again to clear it", k, SMON.KILL_FILE,
                                now - float(w.get("ts") or 0))
                    continue
                if k not in kills:
                    self.ours.pop(k, None)
                    self.mem_kills.pop(k, None)
                    self.armed.pop(k, None)
                    self.hist.pop(k, None)
                    self.cleared[k] = now
                    self.wide_hist.pop({"exit:raise": "raise"}.get(k, k), None)
                    self.hold.pop(k, None)
                    self.hold_start.pop(k, None)
                    for _, s_ in self.combo_hist:
                        s_.pop(k, None)                   # the backstop does not re-pause it on the same fall
                    self.base, self.bad_streak, self.un_streak = None, 0, 0     # the unattributed check restarts too
                    self.localized.clear()
                    log.warning("STRATEGY STOPS: %s re-enabled by hand: re-armed from now", k)
            self.good_kills = dict(kills)
        new = []
        for k in [k for k in self.pending if k in kills]:
            self.pending.pop(k)                           # in the file already (written by someone else): not ours to rewrite
        for k, e_ in list(self.pending.items()):          # decided earlier, not yet written: still in force, retried below
            if k not in kills:
                kills[k] = e_
                new.append((k, e_.get("reason", "")))
        for ex, ba in (touch or {}).items():
            if ba and ba[0] is not None and ba[1] is not None:
                self.touch_cache[ex] = ba
        touch = dict(self.touch_cache)            # a leg without a touch now (settled, closed) keeps its last one
        if positions is not None:
            gone = {ex for ex, q in (self.last.get("venue_pos") or {}).items() if abs(q) >= 1 and abs(positions.get(ex, 0.0)) < 1}
            gone |= {ex for ex in self.last.get("gone_pending", set()) if abs(positions.get(ex, 0.0)) < 1}
            seen = self.last.setdefault("held_seen", {})
            if gone:
                # a fill of ours on the leg since it was last seen held closed it (not a settlement)
                since_h = min(seen.get(ex, now) for ex in gone) - 120.0
                filled = {str(r["exchange_id"]) for r in self.store.rows(
                    "SELECT DISTINCT exchange_id FROM fills WHERE filled_at > ? OR ingested_at > ?",
                    (datetime.fromtimestamp(since_h, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), since_h))}
                gone = {ex for ex in gone if ex not in filled}
            for ex, q in positions.items():
                if abs(q) >= 1:
                    seen[ex] = now
            # left the read with no fill of ours on two reads in a row (a close whose fill is still unread is not one)
            confirmed = gone & set(self.last.get("gone_pending", set()))
            self.last["gone_pending"] = gone - confirmed
            if confirmed:
                settled = set(settled or ()) | confirmed  # settled (or voided)
            self.last["venue_pos"], self.last["venue_pos_t"] = dict(positions), now
        if self.settled_seen:
            back = {str(r["exchange_id"]) for r in self.store.rows(
                "SELECT DISTINCT exchange_id FROM fills WHERE ingested_at > ?", (self.last.get("settle_check", now - 60.0),))} & self.settled_seen
            if back:
                self.settled_seen -= back                 # our own (late) fill closed it: not a settlement
                log.info("STRATEGY STOPS: %s not settled after all (a fill of ours arrived)", sorted(back))
            self.last["settle_check"] = now
        if settled and not set(settled) <= self.settled_seen:
            # a market settled: its payout reaches the cash and its position leaves the read, which the ledgers do not
            # book; the unattributed check waits and re-baselines afterwards (never a settlement taken for a loss)
            self.settled_seen |= set(settled)
            self.quiet_until = now + SETTLE_QUIET
            log.warning("STRATEGY STOPS: %d position(s) settled: unattributed check paused for %.0f min, then re-baselined",
                        len(settled), SETTLE_QUIET / 60)
        self.judge_until = now - JUDGE_LAG
        self.prev_eval_used = getattr(self, "prev_eval", None)
        L, untagged, events = self.ledgers()
        self.prev_eval = now
        vals = self.values(L, touch, churn)

        def legs_of(key) -> set:
            if key == "hf_arb":
                return set((churn or {}).get("legs") or ())
            lk = {"arb": "sets"}.get(key, key)
            led = L.get(lk)
            recent = getattr(self, "recent", {}).get(lk, set())
            # only legs it holds AND traded in the last hour (a strategy with no live orders keeps the MM off nothing)
            return {ex for ex, q in led.pos.items() if abs(q) >= 1 and ex in recent} if led is not None else set()

        def quarantine(key, kind, legs=None):
            if key != "mm" and key != "all" and not key.startswith("exit:"):
                q = (legs_of(key) if kind not in ("unattributed",) else set()) | set(legs or ())
                fresh = [ex for ex in q if self.quarantine.get(ex, 0.0) < now + self.quarantine_sec - 60]
                for ex in q:
                    self.quarantine[ex] = max(self.quarantine.get(ex, 0.0), now + self.quarantine_sec)
                if fresh:
                    log.warning("STRATEGY STOPS: the MM keeps off %d leg(s) of %s for %.0f min", len(q), key, self.quarantine_sec / 60)

        def add(key, reason, kind, extra=None, legs=None):
            if key in self.hold_start and self.hold_start[key] and self.hold.get(key, 0.0) > now:
                extra = dict(extra or {}, held_since=self.hold_start[key])   # entries stopped then (still in force)
            if self.pause(kills, key, reason, now, kind, extra):
                new.append((key, reason))
                quarantine(key, kind, legs)

        breached = set()

        def breach(key, reason, kind, extra=None):
            """A P&L rule on one valuation: the first time, entries are held for this cycle and the next (the bot reads
            hold through strategy_paused, so it is as quick as today's switch); held on the next valuation too: paused
            for good. One bad snapshot (a feed hole, a missing side) cannot make a pause only a human clears."""
            if key in kills:
                return
            if key in breached:
                return                                    # one decision per key per evaluation
            breached.add(key)
            if key in self.hold and self.hold_start.get(key, now) <= now - HOLD_MIN:
                add(key, reason, kind, extra)
            elif key not in self.hold:
                self.hold[key] = now + HOLD_SEC
                self.hold_start[key] = now
                quarantine(key, kind)
                log.warning("STRATEGY STOPS: %s entries held (first breach, paused for good if it holds next cycle): %s", key, reason)

        # 1. stops: loss since armed, or a drop from the trailing-hour peak
        for k, val in vals.items():
            lim = self.limits.get(k)
            if lim is None:
                continue
            a = self.armed.get(k)
            if a is None:
                self.armed[k] = a = {"ts": now, "value": val}
            pnl = val - a["value"]
            h = self.hist.setdefault(k, [])
            h.append((now, val))
            while h and now - h[0][0] > 3600:
                h.pop(0)
            peak = max(x for _, x in h)
            drop_lim = float("inf") if k == "shock" else (abs(lim) if k in WIDE_DROP else self.drop_floor)
            if pnl <= lim:
                breach(k, "P&L %+.0f since armed %s <= %.0f" % (pnl, _utc(a["ts"]), lim), "stop", {"pnl": round(pnl, 1)})
            elif peak - val >= drop_lim:
                breach(k, "P&L fell %.0f from its trailing-hour peak (>= %.0f)" % (peak - val, drop_lim), "drop",
                       {"drop": round(peak - val, 1)})
        # 1b. the ledgers with no limit (the liquidations, the punt): a fall of WIDE_FALL inside an hour
        # stops that path (a bug tagging its losses as one of them would otherwise run unchecked)
        # (not election night: its core trade buys stale asks while the bid stays stale for minutes, so its touch value
        # falls by design; only a stop_limits entry Sizhe sets gates it, as today's switch never did)
        for k, gate in (("raise", "exit:raise"), ("punt", None)):
            if k not in vals:
                continue
            h = self.wide_hist.setdefault(k, [])
            h.append((now, vals[k]))
            while h and now - h[0][0] > 3600:
                h.pop(0)
            fall = max(x for _, x in h) - vals[k]
            if fall >= WIDE_FALL:
                why = "%s ledger fell %.0f within the hour (>= %.0f)" % (k, fall, WIDE_FALL)
                if gate:
                    breach(gate, why, "wide")
                elif self.last.get("wide_" + k, 0) < now - 600:
                    self.last["wide_" + k] = now
                    log.warning("STRATEGY STOPS: %s (no path to pause: check it by hand)", why)
        # 2. markout (MM only)
        if now - self.cleared.get("mm", -1e18) >= 3 * 3600 and markout and markout[1] >= 20 and markout[0] is not None and \
                round(markout[0], 6) < float(getattr(self.cfg, "kill_markout_ticks", -1.0)):
            add("mm", "30-min markout %.2f ticks on %d fills < %.1f" % (markout[0], markout[1], self.cfg.kill_markout_ticks), "markout")
        # 3. the sleeves' own stops
        if churn_killed:
            add("hf_arb", "its own stop (realised + open <= churn_kill) switched it off; clearing also needs killed=false "
                          "in params/churn_state.json", "sleeve")
        if shock_killed:
            add("shock", "its own stop (P&L <= shock_kill) switched it off; clearing also needs its own state reset", "sleeve")
        if catchup_killed:
            add("catchup", "its own stop (P&L <= catchup_kill) switched it off; clearing also needs killed=false in its state "
                           "file", "sleeve")
        # 4. exits / unwinds through zero
        # the account's position = every ledger together + what no ledger holds (the drift, from this cycle's read): exits,
        # raise clips and unwinds are sized on the account's position, so they are judged against it
        if positions is not None:
            tot_now: dict = {}
            for l in list(L.values()) + [untagged]:
                for e_, q_ in l.pos.items():
                    tot_now[e_] = tot_now.get(e_, 0.0) + q_
            drift_of = lambda e_: positions.get(e_, 0.0) - tot_now.get(e_, 0.0)
        else:
            drift_of = lambda e_: 0.0
        # a crossing is a candidate first: re-judged at every evaluation with that evaluation's drift (a later fill already
        # in the positions read but not yet ingested shifts the drift; ingest lags up to ~4.5 min) and written only if it
        # still holds CROSS_CONFIRM after it was first seen (review 24 Oct: a flattening exit followed by an unread MM
        # fill read as "closed more than held")
        cand = [tuple(c) for c in self.cross_pending] + [e_ + (None,) for e_ in events]
        still = []
        for t, ev, key, ex, q, before, after, first in cand:
            if key is None or before is None or abs(q) < 1:
                continue
            led_before, led_after = before, after
            if ev == "exit":
                d_ = drift_of(ex)                         # held on the venue beyond every ledger, on the side it closes
                adj = max(0.0, d_) if q < 0 else max(0.0, -d_)
                tol = sum(a for ing_, a in self.recent_fills.get(ex, []) if ing_ > t)
                before, after = before + adj, after + adj
                if after + tol >= -0.5:
                    continue                              # closed no more than was held
            elif ev in ("raise", "unwind"):
                d_ = drift_of(ex)
                before, after = before + d_, after + d_
                # other fills on the leg ingested since the previous evaluation (between this cycle's positions read and
                # the ingest); the event's own fill only if it is one of them
                # (non-exit fills only, ingested after the event's own fill: a sibling exit never excuses a double exit)
                tol = sum(a for ing_, a in self.recent_fills.get(ex, []) if ing_ > t)
                if tol >= 1 and abs(after) <= tol:
                    continue
            if ev == "exit":
                crossed = True                            # (decided above: closed more than the ledgers held on that side)
            elif ev == "raise":
                # a raise clip may only reduce the account's position toward zero: through zero or further away from it
                # is closing what the account did not hold
                crossed = not (before * q < 0 and abs(q) <= abs(before) + 1e-9)
            elif ev == "unwind":
                # an unwind leg went through zero: it closed sets the account did not hold (a NO-set unwind buys YES, a
                # YES-set unwind buys NO)
                crossed = after >= 1 if q > 0 else after <= -1
            elif ev in EXIT_PATHS:
                # an own-ledger exit (cheap favourites, catch-up, shock) may only reduce its ledger toward zero
                crossed = not (before * q < 0 and abs(q) <= abs(before) + 1e-9)
            else:
                crossed = before * after < 0 and min(abs(before), abs(after)) >= 1
            if not crossed:
                continue
            if ev not in EXIT_PATHS and ev not in ("unwind", "raise"):
                continue                                  # an entry may legitimately flip (the MM sells more than it holds)
            if ev in EXIT_PATHS and not EXIT_PATHS[ev][0] and not EXIT_PATHS[ev][1]:
                continue                                  # not judged (shock_exit)
            if first is None or now - first < CROSS_CONFIRM or not fills_ok or positions is None:   # (a read failed: no drift)
                still.append((t, ev, key, ex, q, led_before, led_after, first if first is not None else now))
                continue
            if ev in EXIT_PATHS:
                path, owners = EXIT_PATHS[ev]
            elif ev == "unwind" and key == "raise":
                path, owners = "exit:raise", ()           # the cash raise's tier 2 / RI trim / wind-down set clips
            elif ev == "unwind":
                path, owners = "exit:unwinds", ("arb", "hf_arb")
            elif ev == "raise":
                path, owners = "exit:raise", ()
            else:
                continue                                  # an entry may legitimately flip (the MM sells more than it holds)
            why = "%s fill on %s took the %s ledger %+.0f -> %+.0f (closed more than held; still so %.0f s later)" % (
                ev, ex, key, before, after, now - first)
            if path:
                add(path, why, "crossing")
            for o in owners:
                add(o, why, "crossing", legs={ex})
        self.cross_pending = still
        # 5. untagged fills and unattributed loss
        status = {"pnl": {}, "unattributed": None, "untagged": None}
        cancel_unrecorded: list = []
        un_pnl = touch_value(untagged, touch)
        if now < self.quiet_until:
            self.base, self.bad_streak = None, 0
            self.localized.clear()
            status["unattributed"] = "paused after a settlement"
        elif cash is not None and positions is not None and mv is not None:
            marks = {}
            for ex, q in positions.items():
                m = mv.get(ex)
                if m is None or abs(q) < 1e-9:
                    continue
                marks[ex] = m / q if q > 0 else 1.0 - m / abs(q)
            for ex, (bid, ask) in touch.items():
                if ex not in marks and bid is not None and ask is not None:
                    marks[ex] = (bid + ask) / 2
            n_no = sum(-q for q in positions.values() if q < 0)
            led_cash = sum(l.cash for l in L.values()) + untagged.cash
            led_pos: dict = {}
            for l in list(L.values()) + [untagged]:
                for ex, q in l.pos.items():
                    led_pos[ex] = led_pos.get(ex, 0.0) + q
            drift = {ex: positions.get(ex, 0.0) - led_pos.get(ex, 0.0) for ex in set(positions) | set(led_pos)}
            snap = {"t": now, "cash_yes": cash + n_no, "led_cash": led_cash, "drift": drift, "untagged": un_pnl}
            if self.base is None:
                self.base = snap
            b = self.base
            new_drift = {ex: d - b["drift"].get(ex, 0.0) for ex, d in drift.items()}
            unexpl = (snap["cash_yes"] - b["cash_yes"]) - (led_cash - b["led_cash"]) + \
                sum(d * marks.get(ex, 0.0) for ex, d in new_drift.items() if abs(d) >= 1)
            un_m = mark_value(untagged, marks)
            if "untagged_m" not in b:
                b["untagged_m"] = un_m
            un_d = un_m - b["untagged_m"]
            self.un_streak = self.un_streak + 1 if un_d <= -self.untagged_limit else 0
            total = unexpl + un_d
            status["unattributed"], status["untagged"] = round(total, 1), round(un_d, 1)
            if total <= -self.unattributed_limit:
                self.bad_streak += 1
                self.bad_since = self.bad_since or now
            else:
                self.bad_streak, self.bad_since = 0, None
            trig = tkey = None
            if un_d > -self.untagged_limit:
                self.un_since = None
            elif self.un_since is None:
                self.un_since = now
            if self.un_streak >= SUSTAIN and now - (self.un_since or now) >= SUSTAIN_SEC:
                trig, tkey, loss, lim = ("untagged fills lost %.0f since %s (>= %.0f, %d evaluations)" % (
                    -un_d, _utc(b["t"]), self.untagged_limit, self.un_streak),
                                         "untagged", -un_d, self.untagged_limit)
            elif self.bad_streak >= SUSTAIN and now - (self.bad_since or now) >= SUSTAIN_SEC:
                trig, tkey, loss, lim = ("unattributed loss %.0f since %s (>= %.0f, %d evaluations)" % (
                    -total, _utc(b["t"]), self.unattributed_limit, self.bad_streak), "unattributed", -total, self.unattributed_limit)
            new_untagged = any(key_ is None for _, _, key_, _, _, _, _ in events)
            if tkey == "untagged" and new_untagged:
                self.last["untagged_new_since_loc"] = True
            if trig and tkey in self.localized and loss >= self.localized[tkey] + lim and \
                    (tkey != "untagged" or self.last.get("untagged_new_since_loc")):
                # (untagged: only if untagged fills kept coming after the localized pause, not on marks of what was held)
                # the strategies on those legs were paused and the loss kept growing by another limit: whatever causes it
                # is not one of them (an untagged path elsewhere), so every entry stops
                add("all", "%s, still growing after the strategies on its legs were paused at a loss of %.0f" % (
                    trig, self.localized[tkey]), "unattributed")
            elif trig and tkey not in self.localized:
                # once localized, the same loss only escalates (above), it is not localized again
                legs = {ex for ex, d in new_drift.items() if abs(d) >= 1}
                legs |= {ex for t_, ev, key, ex, q, _, _ in events if key is None}
                legs |= self._untagged_since(b["t"])
                if tkey == "untagged" and open_orders:
                    unrec = self._unrecorded(open_orders, legs)
                    if unrec:                             # could be Sizhe's or the punt's: logged, never cancelled here
                        log.warning("STRATEGY STOPS: %d resting order(s) no strategy recorded rest on the untagged legs: %s "
                                    "(not cancelled: check them by hand)", len(unrec), unrec[:10])
                sus = self.suspects(legs, b["t"])
                self.localized[tkey] = loss
                self.last["untagged_new_since_loc"] = False
                if sus:
                    for k in sorted(sus):
                        add(k, "%s; it traded the legs where it sits (%s)" % (trig, ", ".join(sorted(legs))[:200]), "unattributed",
                            legs=legs)
                elif tkey == "untagged":
                    # untagged fills on legs no strategy traded since the baseline: a manual trade (or an unrecorded
                    # punt) more likely than a bot path; KILL-ENTRIES only if untagged fills keep coming (escalation)
                    log.warning("STRATEGY STOPS: %s; no strategy traded the legs where it sits (%s): not paused; every entry "
                                "is paused if untagged fills keep coming (a manual trade? record it)", trig,
                                ", ".join(sorted(legs))[:200] or "none found")
                else:
                    add("all", "%s; no strategy traded the legs where it sits (%s)" % (trig, ", ".join(sorted(legs))[:200] or "none found"),
                        "unattributed")
        # 6. stuck resting orders
        cancel = []
        if open_orders:
            ids = [int(o["id"]) for lst in open_orders.values() for o in lst if o.get("id") is not None]
            meta = {}
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in self.store.rows("SELECT order_id, purpose, placed_at, expires_at FROM orders WHERE order_id IN (%s)"
                                         % ",".join("?" * len(chunk)), tuple(chunk)):
                    meta[int(r["order_id"])] = r
            for oid in ids:
                r = meta.get(oid)
                if r is None:
                    continue                              # not ours as far as the bot knows (manual): never cancelled here
                exp = _ts(r["expires_at"])
                late = (now > exp + STUCK_PAST_EXPIRY) if exp is not None else (now - (r["placed_at"] or now) > STUCK_NO_EXPIRY)
                if not late:
                    self.stuck_seen.pop(oid, None)
                    continue
                first = self.stuck_seen.setdefault(oid, now)
                if now - first < STUCK_CONFIRM:
                    continue                              # still listed a minute later: stuck (not a venue purge in flight)
                if r["purpose"] not in CANCEL_STUCK:
                    if self.last.get("stuck_skip", {}).get(oid) is None:
                        self.last.setdefault("stuck_skip", {})[oid] = now
                        log.warning("STRATEGY STOPS: order %d (%s) rests past its expiry: left alone (not a purpose the stops "
                                    "cancel)", oid, r["purpose"])
                    continue
                cancel.append(oid)
                key = PURPOSE_LEDGER.get(r["purpose"])
                if r["purpose"] in EXIT_PATHS:
                    log.warning("STRATEGY STOPS: exit order %d (%s) still rests past its expiry %s: cancelled (exits stay on)",
                                oid, r["purpose"], r["expires_at"])
                elif key:
                    add(key, "order %d (%s) still rests %s" % (oid, r["purpose"], "past its expiry %s" % r["expires_at"] if exp
                                                                else "%.0f min after placing" % ((now - r["placed_at"]) / 60)), "stuck")
        # 7. bypass: entries tagged with a paused strategy
        for k, purposes in ENTRY_PURPOSES.items():
            src = kills.get("all") or kills.get(k)
            until = float("inf")                          # orders counted up to here
            if src is None:
                # only a hold: its orders count while it was in force (a lapsed hold let entries resume legitimately)
                if not self.hold_start.get(k) or self.hold.get(k, 0.0) <= self.hold_start[k]:
                    continue
                ts, until = self.hold_start[k], self.hold[k]
            else:
                try:
                    ts = float(src.get("ts") or 0) if isinstance(src, dict) else 0.0
                    if isinstance(src, dict) and src.get("held_since"):
                        ts = min(ts, float(src["held_since"])) if ts else float(src["held_since"])
                except (TypeError, ValueError):
                    ts = 0.0                              # a hand-written entry: no time to judge a bypass against
            if not ts:
                continue
            since_b = max(ts, self.cleared.get("all", 0.0), self.cleared.get(k, 0.0)) + BYPASS_GRACE
            q = self.store.one("SELECT COUNT(*) n, MAX(order_id) o, COUNT(DISTINCT CAST(placed_at AS INTEGER)) t FROM orders "
                               "WHERE purpose IN (%s) AND placed_at > ? AND placed_at <= ? AND order_id > ?"
                               % ",".join("?" * len(purposes)), tuple(purposes) + (since_b, min(until, 1e18), self.bypass_seen.get(k, 0)))
            if q and q["n"] and q["t"] >= BYPASS_TIMES:
                if "all" in kills:
                    if self.last.get("bypass_all") != q["o"]:
                        log.warning("KILL-ENTRIES bypass: %d %s order(s) placed after every entry was paused (last %s): an "
                                     "ungated path", q["n"], "/".join(purposes), q["o"])
                        self.last["bypass_all"] = q["o"]
                else:
                    add("all", "%d order(s) tagged %s placed after %s was paused (last order %s): a mis-tagged order or an "
                               "ungated path" % (q["n"], "/".join(purposes), k, q["o"]), "bypass")
                self.bypass_seen[k] = int(q["o"] or 0)      # reported once: a clear of "all" alone does not re-fire on it
        # 8. ping-pong (the recycler pump)
        seq: dict = {}
        since = max(now - 3600, self.cleared.get("arb", 0.0), self.cleared.get("hf_arb", 0.0))
        liq = {int(r["order_id"]) for r in self.store.rows(
            "SELECT order_id FROM orders WHERE placed_at > ? AND purpose IN ('raise_set', 'ri_trim', 'wind_down')", (since - 60,))}
        for r in self.store.rows("SELECT race, kind, cost_per_set, legs, response FROM arb_trades WHERE ok=1 AND ts > ? ORDER BY ts, id", (since,)):
            if r["kind"] in UNWIND_KINDS and liq:
                try:
                    ids = {int((x.get("data") or {}).get("orderId")) for x in (json.loads(r["response"] or "{}").get("results") or [])
                           if (x.get("data") or {}).get("orderId") is not None}
                except (ValueError, TypeError, AttributeError):
                    ids = set()
                if ids & liq:
                    continue                              # a cash-raise / RI-trim / wind-down sale, not the recycler
            if r["kind"] in UNWIND_KINDS:
                try:
                    payout = max(1, len(json.loads(r["legs"] or "[]")) - 1)
                except ValueError:
                    payout = 1
                if r["kind"] == "unwind_set" and r["cost_per_set"] is not None and r["cost_per_set"] >= payout - 1e-9:
                    continue                              # a free unwind (proceeds >= payout) locks profit: not a loop
            seq.setdefault(r["race"], []).append("unwind" if r["kind"] in UNWIND_KINDS else "buy")
        for race, ks in seq.items():
            flips = sum(1 for a, b in zip(ks, ks[1:]) if a != b)
            if flips >= PINGPONG_FLIPS and ks.count("unwind") >= PINGPONG_UNWINDS:
                why = "%s bought and unwound alternately %d times in the last hour (%d unwinds): a recycle loop" % (race, flips, ks.count("unwind"))
                for k in ("arb", "hf_arb"):
                    add(k, why, "pingpong")
        # 9. bot-wide backstop: the strategies together falling COMBINED_DROP inside an hour (a bug spread over several
        # sleeves, each under its own limit) pauses each one still trading that lost >= COMBINED_SHARE. A strategy paused
        # inside the hour counts with its value at its pause (its holdings keep moving while it exits: not the bug's
        # loss); only P&L changes of a strategy valued at both moments (ledger levels are not P&L); never all entries
        active = self._active_keys(now - 3600)
        allv = {k: v for k, v in vals.items() if k not in COMBINED_EXCLUDE and k in active}
        self.combo_hist.append((now, dict(allv)))
        while self.combo_hist and now - self.combo_hist[0][0] > 3600:
            self.combo_hist.pop(0)

        def paused_at(k):
            e_ = kills.get(k)
            if e_ is None:
                return None
            try:
                return float(e_.get("ts")) if isinstance(e_, dict) and e_.get("ts") is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        def value_now(k):
            tp = paused_at(k)
            if tp is None:
                return allv[k]
            past = [s_[k] for t_, s_ in self.combo_hist if t_ <= tp + 1e-6 and k in s_]
            return past[-1] if past else None             # paused before the window: not part of this fall
        cur = {k: value_now(k) for k in allv}
        best = None
        for t_, s_ in self.combo_hist:
            common = [k for k in allv if k in s_ and cur[k] is not None and (paused_at(k) is None or paused_at(k) >= t_)]
            fall = sum(s_[k] - cur[k] for k in common)
            if best is None or fall > best[0]:
                best = (fall, s_, common)
        fall, pk, common = best
        if fall >= COMBINED_DROP:
            losers = sorted(k for k in common if paused_at(k) is None and pk[k] - cur[k] >= COMBINED_SHARE)
            why = "the strategies together fell %.0f within the hour (>= %.0f; %s)" % (
                fall, COMBINED_DROP, ", ".join("%s %+.0f" % (k, cur[k] - pk[k]) for k in sorted(common) if abs(cur[k] - pk[k]) >= 1))
            for k in losers:
                breach(k, why, "combined")
        for k in list(self.hold):
            if k not in breached and k not in self.pending:
                self.hold.pop(k, None)             # the breach did not hold on this valuation: released
                self.hold_start.pop(k, None)
        if new and self.write:
            merged = self.kills() if readable else None
            if merged is None:                            # the file stopped parsing since the read: never overwrite it
                self.pending.update({k: kills[k] for k, _ in new})
                log.warning("STRATEGY STOPS: params/%s does not parse: %d pause(s) kept in force in memory, written once "
                            "it is fixed", SMON.KILL_FILE, len(self.pending))
            else:
                merged.update({k: kills[k] for k, _ in new})
                try:
                    SMON._write_json(self.kill_path, merged)
                    for k, _ in new:
                        self.ours[k] = now
                        self.pending.pop(k, None)
                        self.hold.pop(k, None)            # written: the kill file pauses it now
                        self.hold_start.pop(k, None)
                    self.good_kills = dict(merged)
                except OSError as e:                      # not written: kept in force in memory and retried
                    self.pending.update({k: kills[k] for k, _ in new})
                    log.warning("STRATEGY STOPS: kill file write failed (%s): %d pause(s) kept in memory, retried next cycle",
                                e, len(self.pending))
        self.seen_fill = max(self.seen_fill, min(self.judge_until, max([self.seen_fill] + [e[0] for e in events if e[0] is not None])))
        if self.write:
            try:
                SMON._write_json(self.state_path, {"armed": self.armed, "ours": self.ours, "seen_fill": self.seen_fill,
                                                   "settled": sorted(self.settled_seen),
                                                   "touch": {k: list(v) for k, v in self.touch_cache.items()},
                                                   "cleared": self.cleared, "bypass_seen": self.bypass_seen,
                                                   "pending": self.pending, "cross_pending": [list(c) for c in self.cross_pending],
                                                   "good_kills": self.good_kills})
            except OSError as e:
                log.warning("strategy stops state: %s", e)
        for k in self.pending:
            self.hold[k] = float("inf")              # an unwritten pause still stops entries (read through hold)
            self.hold_start.setdefault(k, 0.0)
        status["pnl"] = {k: round(val - self.armed[k]["value"], 1) for k, val in vals.items() if k in self.armed}
        self.quarantine = {ex: u for ex, u in self.quarantine.items() if u > now}
        self.last["status"] = status
        return {"new": new, "cancel": cancel, "cancel_unrecorded": cancel_unrecorded, "status": status}

    def _active_keys(self, since: float) -> set:
        """Stop keys with entry orders since `since` (a sleeve with no live entries is not part of a bot-wide fall)."""
        out = set()
        for r in self.store.rows("SELECT DISTINCT purpose FROM orders WHERE placed_at > ?", (since,)):
            for k, ps in ENTRY_PURPOSES.items():
                if r["purpose"] in ps:
                    out.add(k)
        if self.store.one("SELECT 1 FROM arb_trades WHERE ok=1 AND ts > ? AND kind IN ('sell_set', 'buy_set') LIMIT 1", (since,)):
            out |= {"arb", "hf_arb"}
        return out

    def _residual(self, led: Ledger, ex: str) -> float:
        """led's position on ex beyond its complete sets in ex's race (all of it when ex is in no known race)."""
        legs = self.race_of.get(ex)
        q = led.pos.get(ex, 0.0)
        if not legs:
            return q
        qs = [led.pos.get(e, 0.0) for e in legs]
        if all(x >= 1 for x in qs):
            return q - min(qs)
        if all(x <= -1 for x in qs):
            return q - max(qs)
        return q

    def _unrecorded(self, open_orders: dict, legs: set) -> list:
        """Our resting orders on these legs that the orders table does not know (an unrecorded path's orders)."""
        ids = [int(o["id"]) for ex in legs for o in (open_orders.get(ex) or []) if o.get("id") is not None]
        if not ids:
            return []
        known = {int(r["order_id"]) for r in self.store.rows("SELECT order_id FROM orders WHERE order_id IN (%s)"
                                                              % ",".join("?" * len(ids)), tuple(ids))}
        return [i for i in ids if i not in known and i not in self.arb_ids]

    def _untagged_since(self, t0: float) -> set:
        out = set()
        for f in self.store.rows("SELECT f.order_id, f.exchange_id FROM fills f LEFT JOIN orders o ON o.order_id = f.order_id "
                                 "WHERE f.ingested_at >= ? AND o.order_id IS NULL AND f.purpose IS NULL", (t0,)):
            oid = f["order_id"]
            if oid is None or int(oid) not in self.arb_ids:
                out.add(str(f["exchange_id"]))
        return out

    def suspects(self, legs: set, t0: float) -> set:
        """Stop keys of every strategy that placed orders on these legs since t0."""
        if not legs:
            return set()
        out = set()
        lg = sorted(legs)
        for r in self.store.rows("SELECT DISTINCT purpose FROM orders WHERE placed_at >= ? AND exchange_id IN (%s)"
                                 % ",".join("?" * len(lg)), (t0,) + tuple(lg)):
            key = PURPOSE_LEDGER.get(r["purpose"])
            if key == "sets":
                out |= {"arb", "hf_arb"}
            elif key and key not in ("punt", "raise", "exit", "election"):
                out.add(key)                              # never an exit path or election night on suspicion alone
        for r in self.store.rows("SELECT legs FROM arb_trades WHERE ts >= ?", (t0,)):
            try:
                if {str(l.get("exchangeId")) for l in json.loads(r["legs"] or "[]")} & legs:
                    out |= {"arb", "hf_arb"}
                    break
            except (ValueError, TypeError, AttributeError):
                continue
        return out
