"""Offline end-to-end simulation of the whole bot against a fake venue, checked against invariants after every step.

    python3 tests/sim_pipeline.py                 # every scenario; exit status 1 on any violation
    python3 tests/sim_pipeline.py calm sweep6     # only these scenarios
    python3 tests/sim_pipeline.py --list          # scenario names
    python3 tests/sim_pipeline.py --churn         # high-frequency arb (churn_enabled) on in every scenario's sandbox, incl. day
    python3 tests/sim_pipeline.py --raise         # the cash raise, both tiers, on in every scenario's sandbox, incl. day
    python3 tests/sim_pipeline.py --punt          # the punt executor on in every scenario's sandbox (3,000 stage-1 test plan)
    python3 tests/sim_pipeline.py -v              # also print per-scenario activity counts
    python3 tests/sim_pipeline.py --patch=fix.py  # exec fix.py (with `mods` = the sandbox sigbot modules) before running:
                                                  # try a candidate fix on the sandbox copy without touching sigbot/

What runs: the real `sigbot.run.Bot` (cycle) and `ArbScanner` passes (one_pass, recycle_pass, offload_pass,
free_unwind_pass, fast_requote_pass), i.e. every sleeve: arb, recycle/free unwinds, offload, MM ladder (+ fast requotes,
vol gate, retrace pause, xv gate), tilt, directional (ExplainedDirection: verdict + consensus legs), lag follower,
inside quoter (one scenario), zero-EV exits, kill switch, feed watchdog, tracker, analytics. The bot runs LIVE
(dry_run=False) against tests/sim_venue.py: SuperMarketClient -> FakeClient, RealtimeFeed -> FakeFeed, the
Polymarket/Kalshi poll -> quotes from the scenario's outside fair, the news fetcher -> nothing.

Isolation: the code, config.json and params/ are copied into a temp sandbox and imported from there, so every file the
bot writes (sqlite store, params/*.json, volatile_paper.jsonl) lands in the sandbox; the project's data/ and params/ are
only read (markets.json, books.json, volume_cache.json). Sockets and urllib are disabled for the whole process. A fake
clock drives time.time/time.sleep and datetime.now inside sigbot modules. API keys are never read (env vars cleared).

Driver: each bot cycle (30 s in fast hours, 60 s otherwise, as Bot.run) is followed by scanner passes every `pass_dt`
seconds of simulated time, with taker flow, expiries and DMM refills in between. Every cycle and every pass is one step.

Invariants checked after every step (violation kinds in brackets):
  exceptions escaping cycle()/a scanner pass [exception_escaped], or logged with a traceback/at ERROR [logged_exception];
  exits never close more than is held: a fill of a purpose-'exit' order may not start from a flat position or cross zero
  [exit_crossed_zero]; an unwind leg must close NO shares, never open YES [unwind_without_set]; every arb basket is
  all-or-none and fills every leg completely [partial_basket, non_atomic_basket]; an arb basket never takes cash below
  min(arb.cash_reserve, cash before the step) (recycle follow-throughs may spend only what their unwind freed)
  [arb_cash_reserve]; |net D notional| <= portfolio cap + one cycle of adds [partisan_cap]; no order adds to a partisan
  direction the bot itself marked blocked [adds_to_blocked_direction]; per-leg directional holding (purpose 'direction',
  net of exits) <= direction.per_market_cap [directional_leg_cap]; every order: positive integer quantity, price on tick
  in [0.005, 0.995], an expiry in the future when it arrives (all-or-none baskets excepted) [bad_quantity, bad_price,
  no_expiry, expired_on_arrival]; passive (batch) orders never execute on arrival [passive_order_crossed]; the MM never
  posts or gets filled on a side its own outside-fair gate blocks [mm_posted_on_gated_side, mm_filled_on_gated_side];
  an unwind leg never ends long YES [unwind_crossed_zero]; arb never sends a basket while our own order is a leg's best bid [arb_vs_own_order]; rates per object over sliding
  windows (RATE_LIMITS: requotes, exits, offload exits, lag entries, directional orders, rejected baskets, inside)
  [rate_*]; no race bought and unwound alternately [arb_ping_pong]; calm scenario: marked P&L + spread paid >= -25
  [calm_pnl]. Kill-switch firings and deploy-watcher thresholds are printed as notes, not violations.

Not exercised: the news LLM path (no key, nothing fetched), the shadow tuner / allocator self-tuning (stubbed: it
replays logs in a thread), election night (off in config), thread interleaving between the scanner and the main loop
(the sim runs them in sequence; churn_interleave runs the churn cycle the moment a scanner unwind releases the lock).
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import math
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)
import sim_venue as SV  # noqa: E402

T0 = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc).timestamp()   # 10:00 ET, fast hours
TID = "00000000-0000-4000-8000-00000000517d"
NET_ATTEMPTS: list = []
LAST: dict = {}          # scenario name -> finished Sim (for interactive debugging)
FORCE_CFG: dict = {}     # --churn: config overrides for every scenario (a scenario's own cfg still wins)


# ======================================================================================== isolation
def _no_net(*a, **k):
    NET_ATTEMPTS.append("".join(traceback.format_stack(limit=6)))
    raise OSError("network disabled in the simulation")


def install_network_guard():
    socket.socket.connect = _no_net
    socket.socket.connect_ex = _no_net
    socket.create_connection = _no_net
    socket.getaddrinfo = _no_net
    urllib.request.urlopen = _no_net
    import http.client
    http.client.HTTPConnection.connect = _no_net
    for k in ("SIG_API_KEY", "ANTHROPIC_API_KEY"):
        os.environ.pop(k, None)


LIVE_STATE = ("churn_state.json", "catchup_state.json", "wind_down_state.json", "strategy_kill.json", "strategy_stops_state.json",
              "strategy_monitor_state.json", "shock_state.json", "raise_state.json", "cheapfav_state.json", "punt_state.json",
              "punt_plan.json", "punt_abort")


def make_sandbox() -> str:
    sb = tempfile.mkdtemp(prefix="sigbot-sim-")
    shutil.copytree(os.path.join(ROOT, "sigbot"), os.path.join(sb, "sigbot"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.bak*", "*.sleeves_wip", "*.pyc", "hft_data"))
    shutil.copyfile(os.path.join(ROOT, "config.json"), os.path.join(sb, "config.json"))
    # the live bot's runtime state is not copied: a scenario starts from its own premise, not from tonight's live churn lots,
    # catch-up ledger, strategy pauses or punt state (25 Oct: the live strategy_stops_state.json paused arb in every sandbox,
    # so recycle_* failed on an unchanged tree and botctl's precheck refused every deploy)
    shutil.copytree(os.path.join(ROOT, "params"), os.path.join(sb, "params_master"), ignore=shutil.ignore_patterns("*.bak*", *LIVE_STATE))
    os.makedirs(os.path.join(sb, "data"))
    for f in ("markets.json", "volume_cache.json", "volume_cache.json.vol"):
        src = os.path.join(ROOT, "data", f)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(sb, "data", f))   # copyfile: fresh mtime, so the volume cache counts as recent
    tc = os.path.join(ROOT, "data", "research_contract_rules", "tc_mapping.json")
    if os.path.exists(tc):                                        # the cheap-favourites sleeve trades same-terms legs only
        os.makedirs(os.path.join(sb, "data", "research_contract_rules"))
        shutil.copyfile(tc, os.path.join(sb, "data", "research_contract_rules", "tc_mapping.json"))
    return sb


def reset_sandbox(sb: str):
    shutil.rmtree(os.path.join(sb, "params"), ignore_errors=True)
    shutil.copytree(os.path.join(sb, "params_master"), os.path.join(sb, "params"))
    for f in os.listdir(os.path.join(sb, "data")):
        if f.startswith("sim.sqlite") or f == "volatile_paper.jsonl":
            os.remove(os.path.join(sb, "data", f))


class Clock(SV.SimClock):
    pass


CLOCK = Clock(T0)
_REAL_TIME, _REAL_SLEEP = time.time, time.sleep


def make_fake_datetime(clock):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock.t, tz) if tz is not None else datetime.fromtimestamp(clock.t)

        @classmethod
        def utcnow(cls):
            return datetime.fromtimestamp(clock.t, timezone.utc).replace(tzinfo=None)
    return FakeDateTime


def install_clock(sigbot_modules):
    time.time = CLOCK.time
    time.sleep = CLOCK.sleep
    fdt = make_fake_datetime(CLOCK)
    for m in sigbot_modules:
        if getattr(m, "datetime", None) is datetime:
            m.datetime = fdt


# ======================================================================================== logging capture
class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.recent = collections.deque(maxlen=600)
        self.step_lines: list = []
        self.errors: list = []

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        line = (CLOCK.t, record.levelname, record.name, msg)
        self.recent.append(line)
        self.step_lines.append(line)
        if record.exc_info or record.levelno >= logging.ERROR:
            tb = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            self.errors.append((CLOCK.t, record.name, msg, tb))


CAP = Capture()


# ======================================================================================== the simulation
class Sim:
    def __init__(self, sc, sb, mods, verbose=False):
        self.sc, self.sb, self.M, self.verbose = sc, sb, mods, verbose
        R = mods["run"]
        self.R = R
        CLOCK.t = T0
        reset_sandbox(sb)
        R.MarketMaker.hot_until.clear()
        R.MarketMaker.hot_anchor.clear()
        self.rng = random.Random(sc.seed)
        markets = json.load(open(os.path.join(ROOT, "data", "markets.json")))
        self.universe = mods["universe"].Universe.from_markets(markets)
        try:
            vol = json.load(open(os.path.join(ROOT, "data", "volume_cache.json")))
        except Exception:
            vol = {}
        touch = initial_touch(self.universe, sc.real_books)
        self.v = SV.Venue(CLOCK, markets, touch, self.rng, mods["api"].ApiError, TID, vol, trades_per_sec=sc.trades_per_sec)
        self.v.outside = base_outside(self.universe, self.v)
        sc.venue_setup(self)
        v = self.v
        labels = iter(["main", "scan", "extra1", "extra2"])
        R.SuperMarketClient = lambda *a, **k: SV.FakeClient(v, next(labels))
        R.RealtimeFeed = lambda *a, **k: SV.FakeFeed(v)
        R.shadow_tick = lambda bot: []
        D = mods["direction"]
        smap = synthetic_map(self.universe)
        D.load_map = lambda max_age_sec=0: smap
        D.DirectionTrader.fetch_outside = lambda self_, pm=None, ks=None: v.outside_quotes()
        # shock mode's fast detector: Polymarket /midpoints from the scenario's outside fair (tokens are "pm" + exchange id)
        D.Polymarket.midpoints = lambda self_, toks: {t: round(v.outside[t[2:]], 4) for t in toks if t[2:] in v.outside}
        cfg = mods["config"].BotConfig.load(os.path.join(sb, "config.json"))
        cfg.api_key = "sim-no-key"
        cfg.db_path = "data/sim.sqlite"
        # The live config is in cash mode since 23 Oct (no new sets, no recycles). Scenarios that exist to exercise the arb
        # and recycle code switch it back on in their sandbox so that code stays regression-tested; every other scenario
        # (incl. the 24-h replay) runs the live config as deployed.
        overrides = dict(ARB_CODE_CFG) if sc.name in ARB_CODE_SCENARIOS else {}
        overrides.update(FORCE_CFG)
        if PUNT_ALL and sc.name not in PUNT_SKIP:
            overrides.update(punt_test_plan(self))
        overrides.update(sc.cfg)
        for k, val in overrides.items():
            obj = cfg
            parts = k.split(".")
            for p in parts[:-1]:
                obj = getattr(obj, p)
            setattr(obj, parts[-1], val)
        self.cfg = cfg
        self.bot = R.Bot(cfg, dry_run=False)
        b = self.bot
        if b.news is not None:
            b.news.collect = lambda prices: ([], {"sources": 0, "failed": 0, "items": 0})
        b.arb.api = b.scan_api
        b.scanner = R.ArbScanner(b)
        self.chk = Checker(self)
        self.start_value = v.value()
        self.start_cash = v.cash
        sc.bot_setup(self)
        self.start_value = v.value()
        self.v.spread_paid = 0.0
        self.chk.reset_baseline()

    # ---------------------------------------------------------------- helpers for scenarios
    def legs(self, race_name):
        return self.universe.races[race_name].exchange_ids

    def race_of(self, ex):
        r = self.universe.race_of(ex)
        return r.name if r else None

    def seed_holding(self, ex, side, qty, purpose, price=None):
        """Buy `qty` of `side` on `ex` before the bot starts (at `price` or the touch), recorded in the bot's store with
        `purpose` so its fills are attributed exactly like the bot's own."""
        v, b = self.v, self.bot
        if price is None:
            price = SV.P(v.ask_t[ex]) if side == "yes" else round(1 - SV.P(v.bid_t[ex]), 3)
        key = v.new_key("seed")
        # seeds trade at a fixed price against the house, outside the book
        o = SV.Order(id=next(v._ids), key=key, prefix="seed", ex=ex, side=side, limit=price, qty=0, orig=qty, filled=qty, expiry=None,
                     t=v.now, open=False, reason=None, tick=None, book=None)
        v.orders[o.id] = o
        v._apply_fill(o, ex, side, price, qty, maker=False)
        b.store.record_order(o.id, ex, None, side, "buy", price, qty, purpose, status="filled", response={"seed": True})
        return o.id

    def seed_set(self, race_name, qty):
        """A held sell-set (NO on every leg at 1 - bid), recorded as an executed arb basket."""
        legs = []
        for ex in self.legs(race_name):
            px = round(1 - SV.P(self.v.bid_t[ex]), 3)
            self.seed_holding(ex, "no", qty, "arb", px)
            legs.append({"exchangeId": ex, "side": "no", "action": "buy", "quantity": qty, "price": px})
        n = len(legs)
        cost = sum(l["price"] for l in legs)
        self.bot.store.record_arb(race_name, "sell_set", legs, qty, (n - 1) - cost + 0.0, cost, True, {"seed": True})

    def et_hour(self):
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(CLOCK.t, ZoneInfo("America/New_York")).hour

    # ---------------------------------------------------------------- driver
    def step(self, label, fn):
        self.chk.begin(label)
        try:
            fn()
        except Exception as e:
            self.chk.violation("exception_escaped", f"{type(e).__name__}: {e}", trace="".join(traceback.format_exc().splitlines(True)[-8:]))
        self.chk.end()

    def scanner_pass(self):
        s = self.bot.scanner
        for name in ("one_pass", "recycle_pass", "offload_pass", "free_unwind_pass", "fast_requote_pass"):
            self.chk.sub = name
            self.v.step = f"{self.chk.label} {name}"
            getattr(s, name)()
        if getattr(self.bot, "punt", None) is not None and self.sc.name != "punt":   # the executor's thread (punt_loop), in sequence
            self.chk.sub = "punt_tick"
            self.bot.punt.tick()
        if getattr(self.bot, "shock", None) is not None:   # shock mode's own thread (shock_loop), run in sequence here
            self.chk.sub = "shock_tick"
            self.v.step = f"{self.chk.label} shock_tick"
            self.bot.shock_tick()
        self.chk.sub = None

    def run(self):
        sc, v = self.sc, self.v
        t_wall = _REAL_TIME()
        for i in range(sc.cycles):
            self.cycle_i = i
            sc.before_cycle(self, i)
            if sc.interleave and i > 0:
                # the scanner thread computed its best basket and entered recycle_pass (it holds `best` in a local) when the
                # main loop's cycle took the trade lock; it resumes once the cycle is done
                self.step(f"scan {i}.pre one_pass", self.bot.scanner.one_pass)
                held_best = self.bot.scanner.best_unfunded
                self.step(f"cycle {i}", self.bot.cycle)
                self.bot.scanner.best_unfunded = held_best
                self.step(f"scan {i}.pre recycle_pass", self.bot.scanner.recycle_pass)
            else:
                self.step(f"cycle {i}", self.bot.cycle)
            sc.after_cycle(self, i)
            cyc = self.cfg.cycle_sec_fast if self.cfg.fast_hours_et[0] <= self.et_hour() < self.cfg.fast_hours_et[1] else self.cfg.cycle_sec
            n = max(1, int(round(cyc / sc.pass_dt)))
            for k in range(n):
                CLOCK.advance(sc.pass_dt)
                v.flow(sc.pass_dt)
                sc.on_tick(self)
                self.step(f"scan {i}.{k}", self.scanner_pass)
        self.wall = _REAL_TIME() - t_wall
        self.chk.finish()
        sc.final_checks(self)
        return self.chk


# ======================================================================================== market setup
def initial_touch(universe, real: bool) -> dict:
    books = json.load(open(os.path.join(ROOT, "data", "books.json")))
    out = {}
    for race in universe.races.values():
        rows = []
        for ex in race.exchange_ids:
            b = books.get(ex) or {}
            bb, ba = b.get("bestBid"), b.get("bestAsk")
            if bb is None or ba is None:
                bb, ba = 0.495, 0.505
            k = max(1, min(4, SV.T(ba) - SV.T(bb)))
            rows.append((ex, (bb + ba) / 2, k, SV.T(bb), SV.T(ba)))
        s = sum(m for _, m, _, _, _ in rows) or 1.0
        for ex, m, k, tb, ta in rows:
            if real:
                out[ex] = (tb, ta)
                continue
            n = m / s
            bt = int(math.floor((n - k * SV.TICK / 2) / SV.TICK + 1e-9))
            bt = max(1, min(198 - k, bt))
            out[ex] = (bt, bt + k)
    for m in json.load(open(os.path.join(ROOT, "data", "markets.json"))):
        if m.get("status") == "open" and m.get("exchanges"):
            ex = str(m["exchanges"][0]["id"])
            out.setdefault(ex, (99, 101))
    return out


def base_outside(universe, v) -> dict:
    """Outside fair per leg: the venue mid de-compressed (logit slope 1.3, the measured favourite-longshot tilt), normalised."""
    out = {}
    for race in universe.races.values():
        f = {}
        for ex in race.exchange_ids:
            m = min(0.99, max(0.01, v.mid(ex)))
            f[ex] = 1 / (1 + math.exp(-1.3 * math.log(m / (1 - m))))
        s = sum(f.values())
        for ex in f:
            out[ex] = round(f[ex] / s, 4)
    return out


def synthetic_map(universe) -> dict:
    races = {}
    for name, r in universe.races.items():
        races[name] = {"office": r.office, "polymarket": {"verified": True, "legs": {p: [{"token": "pm" + l.exchange_id}] for p, l in r.legs.items()}},
                       "kalshi": {"verified": True, "legs": {p: [{"ticker": "k" + l.exchange_id}] for p, l in r.legs.items()}}}
    return {"built_at": "sim", "races": races, "unmatched": []}


def two_leg_mid_races(sim, lo=0.3, hi=0.7, exclude=()):
    out = []
    for name, r in sorted(sim.universe.races.items()):
        if len(r.legs) != 2 or name in exclude or "D" not in r.legs or "R" not in r.legs:
            continue
        if all(lo <= sim.v.mid(ex) <= hi for ex in r.exchange_ids):
            out.append(name)
    return out


def set_race_bids_sum(sim, race_name, total, spread_ticks=1):
    """Re-centre a race's DMM so its YES bids sum to `total` (a standing sell-set when total > 1)."""
    v = sim.v
    exs = sim.legs(race_name)
    mids = [v.mid(ex) for ex in exs]
    s = sum(mids)
    target = [m / s * total for m in mids]
    for ex, t in zip(exs, target):
        bt = int(round(t / SV.TICK))
        v.set_touch(ex, bt, bt + spread_ticks)
    # rounding: fix the last leg so the sum is exact in ticks
    want = int(round(total / SV.TICK))
    got = sum(v.bid_t[ex] for ex in exs)
    v.set_touch(exs[-1], v.bid_t[exs[-1]] + (want - got), v.ask_t[exs[-1]] + (want - got))


# ======================================================================================== invariants
EXEMPT_ADD = {"exit", "offload", "arb", "unw", "fre", "seed", "hedge", "shk", "shx", "shm", "punt"}   # shk/shx/shm: shock mode, exempt from the partisan cap (Sizhe 24 Oct); punt: only with an approved plan's cap_exempt
RATE_LIMITS = {
    # category: [(window_sec, max events per object), ...]   object = exchange or race
    "requote": [(60, 5)],            # MM/tilt/inside cancel-all per exchange: 1 per 30-s cycle + fast requotes (3/min total)
    "exit": [(60, 2), (3600, 20)],   # zero-EV/lag exits per exchange: at most one per cycle, and no endless re-sending
    "offload": [(3600, 1)],          # offload exit: each held position at most once per hour
    "lag": [(3600, 2)],              # lag entry: 30-min cooldown after its exit
    "direction": [(600, 2)],         # directional order per leg: signal cooldown 600 s
    "arb_fail": [(60, 2)],           # rejected all-or-none basket per race: 60-s cooldown
    "inside": [(60, 2)],
}
GLOBAL_LIMITS: dict = {}
# operational thresholds (deploy/watch_deploy.py stops the bot above them); reported as notes, not violations: a recycle that
# swaps a low-edge set for a better one every scanner pass is by design as long as it never buys back what it unwound
NOTE_LIMITS = {("unwind", "*"): (600, 12)}


class Checker:
    def __init__(self, sim):
        self.sim = sim
        self.v = sim.v
        self.viol: list = []
        self.label = ""
        self.sub = None
        self.ci = self.fi = self.vi = self.ei = 0
        self.events = collections.defaultdict(list)       # (category, object) -> [ts]
        self.max_netd = 0.0
        self.netd_flagged = False
        self.dir_hold: dict = {}                           # ex -> [signed qty, cost]
        self.dir_flagged = set()
        self.purpose_cache: dict = {}
        self.counts = collections.Counter()
        self.unwind_seq = collections.defaultdict(list)    # race -> [(t, 'entry'|'unwind')]
        self.gated_flagged = set()
        self.kills: list = []
        self.stops: list = []                       # per-strategy stop pauses (strategy_stops.py)
        self.own_guard = None                              # scenario: (race, exchange, order id) of our own top-of-book bid
        self.cash_mode_baskets: list = []                  # ok arb baskets in cash mode that were not high-frequency arb entries
        self.strict_unwinds = False                        # scenario: > 12 unwinds in 10 min is a violation, not a note

    def churn_entry(self, c, race) -> bool:
        """A high-frequency arb (churn) entry: a main-loop basket in a race the sleeve attempted at that moment."""
        ch = getattr(self.sim.bot, "churn", None)
        return (ch is not None and str(c.get("step", "")).startswith("cycle")
                and abs(ch.last_entry.get(race, -1e18) - c["t"]) < 60.0)

    def reset_baseline(self):
        self.ci, self.fi, self.vi = len(self.v.calls), len(self.v.fills), len(self.v.violations)
        self.ei = len(CAP.errors)

    def violation(self, kind, detail, trace=None):
        sub = f" [{self.sub}]" if self.sub else ""
        if trace is None:
            lines = CAP.step_lines[-6:]
            trace = "\n".join(f"      {datetime.fromtimestamp(t, timezone.utc).strftime('%H:%M:%S')} {lv[0]} {n}: {m[:220]}" for t, lv, n, m in lines)
        self.viol.append({"scenario": self.sim.sc.name, "step": self.label + sub, "t": CLOCK.t, "kind": kind, "detail": detail, "trace": trace})

    def purpose(self, order_id):
        if order_id is None:
            return None
        if order_id in self.purpose_cache:
            return self.purpose_cache[order_id]
        r = self.sim.bot.store.one("SELECT purpose FROM orders WHERE order_id=?", (order_id,))
        p = r["purpose"] if r else None
        if p is not None:
            self.purpose_cache[order_id] = p
        return p

    def begin(self, label):
        self.label = label
        self.sub = None
        self.v.step = label
        self.cash0 = self.v.cash
        self.xv0 = dict(getattr(self.sim.bot.mm, "xv_fair", None) or {})   # the outside fair the MM quotes against during this step
        CAP.step_lines = []

    def end(self):
        v, bot = self.v, self.sim.bot
        cfg = self.sim.cfg
        # order-level violations found at arrival
        for x in v.violations[self.vi:]:
            self.violation(x["kind"], x["detail"])
        self.vi = len(v.violations)
        # swallowed exceptions / errors logged by the bot
        for t, lv, name, msg in CAP.step_lines:
            if msg.startswith("KILL SWITCH"):
                self.kills.append(f"{datetime.fromtimestamp(t, timezone.utc).strftime('%H:%M:%S')} {msg[:90]}")
            elif msg.startswith("KILL-STRATEGY") or msg.startswith("KILL-ENTRIES"):
                self.stops.append(f"{datetime.fromtimestamp(t, timezone.utc).strftime('%H:%M:%S')} {msg[:160]}")
        for t, name, msg, tb in CAP.errors[self.ei:]:
            self.violation("logged_exception", f"{name}: {msg[:200]}", trace=tb[-1500:] if tb else None)
        self.ei = len(CAP.errors)
        blocked = set(getattr(bot.mm, "blocked", set()) or set())
        reserve = cfg.arb.cash_reserve
        for c in v.calls[self.ci:]:
            t = c["t"]
            if c["kind"] == "cancel_all" and c.get("ex"):
                self.events[("requote", c["ex"])].append(t)
            elif c["kind"] == "order":
                pre, ex = c["prefix"], c["ex"]
                self.counts["orders_" + pre] += 1
                if pre == "exit":
                    self.events[("exit", ex)].append(t)
                elif pre == "offload":
                    self.events[("offload", ex)].append(t)
                elif pre == "lag":
                    if c.get("pos_before", 0) == 0 or (c["side"] == "yes") == (c.get("pos_before", 0) > 0):
                        self.events[("lag", ex)].append(t)
                elif pre == "ord":
                    self.events[("direction", ex)].append(t)
                if c.get("ok") and pre not in EXEMPT_ADD:
                    reduces = (c["side"] == "yes" and c["pos_before"] < 0) or (c["side"] == "no" and c["pos_before"] > 0)
                    d = self.sim.M["risk"].adds(self.sim.universe, ex, c["side"])
                    if d in blocked and not reduces:
                        self.violation("adds_to_blocked_direction", f"{c['key']} {ex} buy {c['side']} x{c['qty']} adds {d} while {sorted(blocked)} is blocked")
                if pre == "bat" and c.get("ok") and self.purpose(c.get("order_id")) == "mm" and cfg.mm.xv_gate_ticks > 0:
                    xv = self.xv0.get(ex)
                    if xv:
                        ok_bid, ok_ask = self.sim.M["xv_gate"](c["mid"], xv[0], xv[1], xv[2], t - xv[3], gap_ticks=cfg.mm.xv_gate_ticks + 1)
                        if (c["side"] == "yes" and not ok_bid) or (c["side"] == "no" and not ok_ask):
                            self.violation("mm_posted_on_gated_side", f"{c['key']} {ex} MM {'bid' if c['side'] == 'yes' else 'ask'} posted while outside "
                                           f"fair {xv[0]:.3f} is {(xv[0] - c['mid']) / SV.TICK:+.1f} ticks from mid {c['mid']:.4f}")
                if pre == "bat" and c.get("ok") and c.get("traded", 0) > 0:
                    ts_ = c.get("touch_at_send")
                    # crossing only because the touch moved while the order was in flight (write latency): a market
                    # race no last look can remove; a quote that already crossed the book it was sent against is a bug
                    in_flight = ts_ is not None and ((c["side"] == "yes" and c["price"] < ts_[1] - 1e-9) or
                                                     (c["side"] == "no" and c["price"] < 1 - ts_[0] - 1e-9))
                    if in_flight:
                        self.counts["crossed_in_flight"] += 1
                    else:
                        self.violation("passive_order_crossed", f"{c['key']} {ex} {c['side']} x{c['qty']} @{c['price']} traded {c['traded']} on arrival "
                                       f"(DMM touch {SV.P(v.bid_t[ex])}/{SV.P(v.ask_t[ex])}; when sent {ts_})")
            elif c["kind"] == "multileg":
                pre = c["prefix"]
                race = self.sim.race_of(str(c["legs"][0]["exchangeId"])) if c["legs"] else None
                self.counts[f"multileg_{pre}_{'ok' if c.get('ok') else 'fail'}"] += 1
                if self.own_guard and race == self.own_guard[0] and pre == "arb":
                    o = v.orders.get(self.own_guard[2])
                    if o is not None and o.open:
                        self.violation("arb_vs_own_order", f"{c['key']} {race} basket sent while our own bid {o.limit} x{o.qty} on {o.ex} is the "
                                       f"leg's best bid ({'filled' if c.get('ok') else 'rejected: ' + str(c.get('reason'))})")
                if pre in ("unw", "fre"):
                    if c.get("ok"):
                        self.events[("unwind", race)].append(t)
                        self.events[("unwind", "*")].append(t)
                        self.unwind_seq[race].append((t, "unwind"))
                elif pre == "arb":
                    if not c.get("ok"):
                        self.events[("arb_fail", race)].append(t)
                    else:
                        self.unwind_seq[race].append((t, "entry"))
                        traded = c.get("traded") or []
                        if len(set(traded)) > 1 or any(q != c["legs"][0]["quantity"] for q in traded):
                            self.violation("partial_basket", f"{c['key']} {race} legs traded {traded}")
                        churn = self.churn_entry(c, race)            # churn keeps its own floor (churn_cash_floor, 3k)
                        res_ = float(getattr(cfg, "churn_cash_floor", 0.0)) if churn else reserve
                        if churn:
                            self.counts["churn_entries"] += 1
                        elif not cfg.arb.new_sets:
                            self.cash_mode_baskets.append(c)
                        floor = min(res_, self.cash0)
                        if c["cash_after"] < floor - 1.0:
                            self.violation("arb_cash_reserve", f"{c['key']} {race} {len(c['legs'])} legs x{c['legs'][0]['quantity']}: cash "
                                           f"{c['cash_before']:.0f} -> {c['cash_after']:.0f}, below min({'churn floor' if churn else 'reserve'} {res_:.0f}, "
                                           f"cash at step start {self.cash0:.0f})")
                if c.get("ok") is False and not c.get("aon"):
                    pass
        self.ci = len(v.calls)
        # fills: exits must never close more than held
        for f in v.fills[self.fi:]:
            m = v.fill_meta[f["id"]]
            pur = self.purpose(m["order_id"])
            if pur is None and m["prefix"] in ("exit", "offload"):
                pur = "exit"
            self.counts["fill_" + str(pur)] += 1
            if pur == "mm" and m["maker"]:
                xv = self.xv0.get(m["ex"])
                if xv and cfg.mm.xv_gate_ticks > 0:
                    # one tick of slack: the bot measures the gap from its book mid (which includes our own quotes)
                    ok_bid, ok_ask = self.sim.M["xv_gate"](m["mid"], xv[0], xv[1], xv[2], m["t"] - xv[3], gap_ticks=cfg.mm.xv_gate_ticks + 1)
                    if (m["side"] == "yes" and not ok_bid) or (m["side"] == "no" and not ok_ask):
                        self.counts["mm_gated_fill_qty"] += m["qty"]
                        if m["t"] - T0 > 300:
                            self.counts["mm_gated_fill_qty_after_5min"] += m["qty"]
                        if m["ex"] not in self.gated_flagged:
                            self.gated_flagged.add(m["ex"])
                            o = v.orders.get(m["order_id"])
                            self.violation("mm_filled_on_gated_side", f"{m['key']} {m['ex']} MM {'bid' if m['side'] == 'yes' else 'ask'} filled x{m['qty']} "
                                           f"@{m['price']} (placed {SV.iso(o.t) if o else '?'}) while the outside fair {xv[0]:.3f} is "
                                           f"{(xv[0] - m['mid']) / SV.TICK:+.1f} ticks from mid {m['mid']:.4f}: the xv gate blocks that side",
                                           trace=self._order_trace(m["ex"]))
            if pur == "exit":
                b, a = m["before"], m["after"]
                if b == 0 or (b > 0 > a) or (b < 0 < a):
                    self.violation("exit_crossed_zero", f"{m['key']} {m['ex']} buy {m['side']} x{m['qty']} @{m['price']}: position {b:+d} -> {a:+d}",
                                   trace=self._order_trace(m["ex"]))
            # an unwind buys YES on every leg of a HELD set: each leg must close NO shares, never open a position
            if m["prefix"] in ("unw", "fre") and m["before"] >= 0:
                self.violation("unwind_without_set", f"{m['key']} {m['ex']} ({self.sim.race_of(m['ex'])}) unwind leg bought YES x{m['qty']} @{m['price']} "
                               f"with no NO held: position {m['before']:+d} -> {m['after']:+d}", trace=self._order_trace(m["ex"]))
            elif m["prefix"] in ("unw", "fre") and m["after"] > 0:      # Fable 24 Oct: -1,700 -> +300 passed the check above
                self.violation("unwind_crossed_zero", f"{m['key']} {m['ex']} ({self.sim.race_of(m['ex'])}) unwind leg bought YES x{m['qty']} @{m['price']} "
                               f"through zero: position {m['before']:+d} -> {m['after']:+d}", trace=self._order_trace(m["ex"]))
            # a YES-set unwind (wind-down) buys NO on every leg of a HELD buy-set: each leg must close YES, never go short
            if m["prefix"] == "unb" and m["before"] <= 0:
                self.violation("unwind_without_set", f"{m['key']} {m['ex']} ({self.sim.race_of(m['ex'])}) YES-set unwind leg bought NO x{m['qty']} "
                               f"with no YES held: position {m['before']:+d} -> {m['after']:+d}", trace=self._order_trace(m["ex"]))
            elif m["prefix"] == "unb" and m["after"] < 0:
                self.violation("unwind_crossed_zero", f"{m['key']} {m['ex']} ({self.sim.race_of(m['ex'])}) YES-set unwind leg bought NO x{m['qty']} "
                               f"through zero: position {m['before']:+d} -> {m['after']:+d}", trace=self._order_trace(m["ex"]))
            # directional holding per leg (cost), net of exits
            if pur in ("direction", "exit"):
                h = self.dir_hold.setdefault(m["ex"], [0.0, 0.0])
                sgn = 1 if m["side"] == "yes" else -1
                if pur == "direction":
                    h[0] += sgn * m["qty"]
                    h[1] += m["qty"] * m["price"]
                elif h[0] * sgn < 0:
                    red = min(abs(h[0]), m["qty"])
                    h[1] *= (abs(h[0]) - red) / abs(h[0]) if h[0] else 0
                    h[0] += sgn * red
                cap = float(getattr(bot.direction.cfg, "per_market_cap", 600.0))
                if h[1] > cap * 1.1 + 5 and m["ex"] not in self.dir_flagged:
                    self.dir_flagged.add(m["ex"])
                    self.violation("directional_leg_cap", f"{m['ex']}: directional holding {h[0]:+.0f} shares costing {h[1]:.0f} > per-leg cap {cap:.0f}",
                                   trace=self._order_trace(m["ex"]))
        self.fi = len(v.fills)
        # partisan cap
        risk = self.sim.M["risk"]
        prices = {ex: {"bestBid": SV.P(v.bid_t[ex]), "bestAsk": SV.P(v.ask_t[ex])} for ex in v.exs}
        expo = risk.exposure(self.sim.universe, v.signed_positions(), prices)
        nd = abs(expo.net_d_notional)
        capp = cfg.mm.portfolio_max_partisan_notional
        # shock mode is exempt from the cap (Sizhe 24 Oct): the bound widens by exactly the shock sleeve's own net D, in
        # its direction; every other sleeve still sees the total (shock included) and must stay inside the rest
        sk_pos = {}                                     # the venue's own record of shk/shx fills (the bot's ledger lags a cycle)
        for m in v.fill_meta.values():
            if m.get("prefix") in ("shk", "shx", "shm", "punt"):     # (the punt scenario's plan is cap_exempt, as Sizhe's yes)
                sk_pos[m["ex"]] = sk_pos.get(m["ex"], 0.0) + (m["qty"] if m["side"] == "yes" else -m["qty"])
        sk_pos = {ex: q for ex, q in sk_pos.items() if abs(q) >= 1}
        sk_nd = risk.exposure(self.sim.universe, sk_pos, prices).net_d_notional if sk_pos else 0.0
        hi, lo = capp + 1000 + max(0.0, sk_nd), -(capp + 1000) + min(0.0, sk_nd)
        if nd > self.max_netd or not (lo <= expo.net_d_notional <= hi):
            # the block is computed once per cycle, so one cycle's directional adds (per_poll_budget 900) may overshoot it
            if not (lo <= expo.net_d_notional <= hi) and not self.netd_flagged:
                self.netd_flagged = True
                self.violation("partisan_cap", f"net D notional {expo.net_d_notional:+.0f} outside [{lo:.0f}, {hi:.0f}] (cap {capp:.0f} + 1,000 "
                                               f"+ the shock sleeve's own {sk_nd:+.0f})")
            self.max_netd = max(self.max_netd, nd)

    def _order_trace(self, ex):
        rows = [c for c in self.v.calls if c.get("ex") == ex or any(str(l.get("exchangeId")) == ex for l in c.get("legs", []))][-6:]
        out = []
        for c in rows:
            ts = datetime.fromtimestamp(c["t"], timezone.utc).strftime("%H:%M:%S")
            if c["kind"] == "order":
                out.append(f"      {ts} {c['key']} buy {c['side']} x{c['qty']} @{c['price']} pos {c.get('pos_before')} -> {c.get('pos_after')} traded {c.get('traded')}")
            elif c["kind"] == "cancel_all":
                out.append(f"      {ts} cancel-all {c['ex']} ({c.get('cancelled')})")
            else:
                out.append(f"      {ts} {c['key']} multileg ok={c.get('ok')} ({c.get('step')})")
        return "\n".join(out)

    def finish(self):
        def worst(ts, w):
            ts = sorted(ts)
            best, j = 0, 0
            for i in range(len(ts)):
                while ts[i] - ts[j] >= w:
                    j += 1
                best = max(best, i - j + 1)
            return best
        for (cat, obj), ts in self.events.items():
            lims = GLOBAL_LIMITS.get(cat, []) if obj == "*" else RATE_LIMITS.get(cat, [])
            for w, mx in lims:
                n = worst(ts, w)
                if n > mx:
                    self.label, self.sub = "end of run", None
                    self.viol.append({"scenario": self.sim.sc.name, "step": "rate check", "t": CLOCK.t, "kind": f"rate_{cat}",
                                      "detail": f"{obj}: {n} in {w}s (design allows {mx}); {len(ts)} in the run",
                                      "trace": self._order_trace(obj) if cat in ("exit", "offload", "lag", "direction", "requote") else ""})
        self.notes = []
        if self.kills:
            self.notes.append(f"kill switch fired {len(self.kills)}x, first: {self.kills[0]}")
        for s in self.stops:
            self.notes.append(f"stop: {s}")
        for (cat, obj), (w, mx) in NOTE_LIMITS.items():
            n = worst(self.events.get((cat, obj), []), w)
            if n > mx and self.strict_unwinds:
                self.viol.append({"scenario": self.sim.sc.name, "step": "rate check", "t": CLOCK.t, "kind": f"rate_{cat}_all",
                                  "detail": f"{n} {cat}s in {w}s (the deploy watcher's alarm is {mx}); {len(self.events[(cat, obj)])} in the run", "trace": ""})
            elif n > mx:
                self.notes.append(f"{cat} {obj}: {n} in {w}s exceeds the deploy watcher's alarm ({mx}); {len(self.events[(cat, obj)])} in the run")
        for race, seq in self.unwind_seq.items():
            kinds = [k for _, k in sorted(seq)]
            flips = sum(1 for a, b in zip(kinds, kinds[1:]) if a != b)
            if kinds.count("unwind") >= 2 and flips >= 3:
                self.viol.append({"scenario": self.sim.sc.name, "step": "rate check", "t": CLOCK.t, "kind": "arb_ping_pong",
                                  "detail": f"{race}: bought and unwound alternately {flips} times ({kinds.count('unwind')} unwinds)", "trace": ""})


# ======================================================================================== scenarios
ARB_CODE_SCENARIOS = {"sweep6", "recycle", "recycle_tail", "recycle_race", "own_bid", "aon_reject", "recycle_oversize", "recycle_stale_fill"}
ARB_CODE_CFG = {"arb.new_sets": True, "arb.recycle_enabled": True}


class Scenario:
    name = "base"
    cycles = 60
    pass_dt = 5.0
    seed = 1
    real_books = False
    trades_per_sec = 0.6
    interleave = False        # run cycle() between the scanner's one_pass and recycle_pass (thread interleaving)
    cfg: dict = {}
    doc = ""

    def venue_setup(self, sim):
        pass

    def bot_setup(self, sim):
        pass

    def before_cycle(self, sim, i):
        pass

    def after_cycle(self, sim, i):
        pass

    def on_tick(self, sim):
        pass

    def final_checks(self, sim):
        pass


class Calm(Scenario):
    name, cycles, seed = "calm", 120, 11
    doc = "no moves, symmetric taker flow; P&L must not be negative beyond the spread we paid"

    def final_checks(self, sim):
        pnl = sim.v.value() - sim.start_value
        sim.pnl = pnl
        if pnl + sim.v.spread_paid < -25:
            sim.chk.viol.append({"scenario": self.name, "step": "end", "t": CLOCK.t, "kind": "calm_pnl",
                                 "detail": f"marked P&L {pnl:+.1f} with {sim.v.spread_paid:.1f} of spread paid (net {pnl + sim.v.spread_paid:+.1f})", "trace": ""})


class Sweep6(Scenario):
    name, cycles, seed = "sweep6", 120, 12
    doc = "a 6-tick sweep on three quoted markets, no outside move; half reverts 25 min later"

    def before_cycle(self, sim, i):
        v = sim.v
        if i == 20:
            q = [ex for ex in sim.bot.mm.quoted if 0.25 <= v.mid(ex) <= 0.75][:3]
            self.swept = q
            for k, ex in enumerate(q):
                v.sweep(ex, 6 if k < 2 else -6)
        if i == 70:
            for k, ex in enumerate(getattr(self, "swept", [])):
                v.shift(ex, -3 if k < 2 else 3)


class OutsideMove(Scenario):
    name, cycles, seed = "outside_move", 150, 13
    doc = "Polymarket/Kalshi move 8 points on three races; the venue's DMM starts following 10 min later, 1 tick / 2 min"

    def before_cycle(self, sim, i):
        v = sim.v
        if i == 10:
            races = two_leg_mid_races(sim)[:3]
            self.moved = []
            for r in races:
                d, rr = sim.universe.races[r].legs["D"].exchange_id, sim.universe.races[r].legs["R"].exchange_id
                v.outside[d] = round(min(0.97, v.outside[d] + 0.08), 4)
                v.outside[rr] = round(max(0.03, v.outside[rr] - 0.08), 4)
                self.moved += [d, rr]
        if i >= 10:
            for ex in getattr(self, "moved", []):
                gap = v.outside[ex] - v.mid(ex)
                v.flow_bias[ex] = 0.2 if gap > 0.01 else (-0.2 if gap < -0.01 else 0.0)
                if i >= 30 and i % 4 == 0 and abs(gap) > SV.TICK:
                    v.shift(ex, 1 if gap > 0 else -1)


class Catchup(Scenario):
    name, cycles, seed = "catchup", 320, 28
    cfg = {"catchup_enabled": True, "catchup_notional": 2500.0}
    doc = "catch-up sweep: after 75 min of history, Polymarket+Kalshi move 3 pts on three races (SIG's DMM follows 10 min later, 1 tick / 2 min) and on a fourth race the outside move reverts after 10 min: enter at SIG's touch in clips, <= 5,000 notional a trade, <= 6 open, no convergence exit (held to the time stop / take-profit), exit on reversal"

    def before_cycle(self, sim, i):
        v = sim.v
        if i == 20:
            # the four test races start with the outside fair at SIG's mid, so only the scripted moves create gaps
            self.races = two_leg_mid_races(sim, 0.3, 0.7)[:4]
            for r in self.races:
                for ex in sim.legs(r):
                    v.outside[ex] = round(v.mid(ex), 4)
        if i == 150:
            self.moved, self.noise = [], []
            for k, r in enumerate(self.races):
                d, rr = sim.universe.races[r].legs["D"].exchange_id, sim.universe.races[r].legs["R"].exchange_id
                v.outside[d] = round(min(0.97, v.outside[d] + 0.03), 4)
                v.outside[rr] = round(max(0.03, v.outside[rr] - 0.03), 4)
                (self.noise if k == 3 else self.moved).extend([d, rr])
            self.t_move = v.now
        if i == 170 and getattr(self, "noise", None):
            for ex in self.noise:                                   # the noise move reverts
                v.outside[ex] = round(v.outside[ex] + (-0.03 if ex == self.noise[0] else 0.03), 4)
        if i >= 170:
            for ex in getattr(self, "moved", []):
                gap = v.outside[ex] - v.mid(ex)
                if i % 4 == 0 and abs(gap) > SV.TICK:
                    v.shift(ex, 1 if gap > 0 else -1)

    def final_checks(self, sim):
        calls = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] in ("cu", "cux")]
        entries = [c for c in calls if c["prefix"] == "cu"]
        if not entries:
            sim.chk.violation("catchup_none", "no catch-up entry although Polymarket+Kalshi moved 3 pts and SIG lagged")
        spent = {}
        for c in entries:
            spent[c["ex"]] = spent.get(c["ex"], 0.0) + c["qty"] * c["price"]
        for ex, v in spent.items():
            if v > 5000 * 1.02:
                sim.chk.violation("catchup_oversize", f"{ex}: {v:.0f} bought in clips > 5,000 for one trade")
        fills = [dict(r) for r in sim.bot.store.rows("SELECT exchange_id, quantity, filled_at FROM fills WHERE purpose IN ('catchup','catchup_exit') ORDER BY filled_at")]
        pos, peak = {}, 0
        for f in fills:
            pos[f["exchange_id"]] = pos.get(f["exchange_id"], 0.0) + float(f["quantity"])
            peak = max(peak, sum(1 for q in pos.values() if abs(q) >= 1))
        if peak > 6:
            sim.chk.violation("catchup_too_many", f"{peak} catch-up positions open at once (max 6)")
        for ex in getattr(self, "noise", []):
            if abs(pos.get(ex, 0.0)) >= 1:
                sim.chk.violation("catchup_noise_held", f"{ex}: catch-up position still open after the outside move reverted")
        self.summary = {"entries": len(entries), "exits": len(calls) - len(entries), "open_at_end": {e: q for e, q in pos.items() if abs(q) >= 1}}


class CheapFav(Scenario):
    name, cycles, seed = "cheapfav", 240, 31
    cfg = {"cheapfav_enabled": True, "cheapfav_budget": 6000.0, "cheapfav_leg_cap": 1500.0}
    doc = "cheap safe favourites: the outside fair de-compresses SIG's mids (favourites 2-5c cheap on SIG); buy fast-count favourites only (same terms, p >= 0.80, edge >= 2c, no punt or slow-count race), <= 6k in total and 1.5k per race outcome, never past 4,000 shares on a leg; at cycle 150 the outside fair of one held leg falls 4c under SIG: exit that leg only"

    def before_cycle(self, sim, i):
        v = sim.v
        if i == 150:
            held = getattr(sim.bot, "cheapfav_open", None) or {}
            self.reversed = None
            for ex, q in sorted(held.items()):
                race = sim.universe.race_of(ex)
                if race is None or len(race.legs) != 2:
                    continue
                other = [e for e in race.exchange_ids if e != ex][0]
                if q > 0:                                       # held YES: fair falls 4c under SIG's bid
                    v.outside[ex] = round(SV.P(v.bid_t[ex]) - 0.04, 4)
                else:                                           # held NO: fair rises 4c over SIG's ask
                    v.outside[ex] = round(SV.P(v.ask_t[ex]) + 0.04, 4)
                v.outside[other] = round(1 - v.outside[ex], 4)
                self.reversed, self.t_rev = ex, v.now
                break

    def final_checks(self, sim):
        CF = sim.M["cheap_favourites"]
        same = CF.same_terms_exchanges(os.path.join(sim.sb, "data", "research_contract_rules", "tc_mapping.json")) or set()
        calls = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] in ("cf", "cfx")]
        entries = [c for c in calls if c["prefix"] == "cf"]
        exits = [c for c in calls if c["prefix"] == "cfx"]
        if not entries:
            sim.chk.violation("cheapfav_none", "no cheap-favourite entry although SIG's favourites sit 2-5c under the outside fair")
        rev = getattr(self, "reversed", None)
        for c in entries:
            race = sim.universe.race_of(c["ex"])
            st = CF.race_state(race.name) if race else None
            if race is None or c["ex"] not in same or st not in CF.FAST_CLOSE_ET or race.name in CF.PUNT_RACES or "I" in race.legs:
                sim.chk.violation("cheapfav_bad_leg", f"{c['key']} {c['ex']} ({race.name if race else '?'}): not a same-terms fast-count two-party race")
                continue
            if c["ex"] == rev and c["t"] >= self.t_rev:
                sim.chk.violation("cheapfav_after_reversal", f"{c['key']} bought {c['ex']} after its edge reversed")
            if c["ex"] != rev or c["t"] < self.t_rev:
                f = sim.v.outside[c["ex"]] if c["ex"] != rev else None
                if f is not None:
                    p_win = f if c["side"] == "yes" else 1 - f
                    if p_win < 0.80 - 1e-9 or p_win - c["price"] < 0.02 - 1e-9:
                        sim.chk.violation("cheapfav_no_edge", f"{c['key']} {c['ex']} {c['side']} @{c['price']}: P(win) {p_win:.3f}, edge {p_win - c['price']:.3f}")
            after = c["pos_before"] + (c["qty"] if c["side"] == "yes" else -c["qty"])
            if abs(after) > 4000 + 1e-9:
                sim.chk.violation("cheapfav_over_4000", f"{c['key']} {c['ex']}: leg position {after:+.0f} shares > 4,000")
        fills = [dict(r) for r in sim.bot.store.rows("SELECT exchange_id, quantity, price FROM fills WHERE purpose IN ('cheapfav','cheapfav_exit') ORDER BY filled_at")]
        race_of = {ex: r for ex, r in sim.universe.by_exchange.items()}
        tot, per = 0.0, {}
        for f in fills:
            q, p = float(f["quantity"]), float(f["price"])
            if any(e["ex"] == f["exchange_id"] for e in exits) and f["exchange_id"] == rev:
                continue
            paid = q * p if q > 0 else -q * (1 - p)
            r = race_of.get(f["exchange_id"])
            win = r[1] if q > 0 else ("R" if r[1] == "D" else "D")
            per[(r[0], win)] = per.get((r[0], win), 0.0) + paid
            tot += paid
        if tot > 6000 + 1:
            sim.chk.violation("cheapfav_budget", f"sleeve bought {tot:.0f} > budget 6,000")
        for k, x in per.items():
            if x > 1500 + 1:
                sim.chk.violation("cheapfav_leg_cap", f"{k}: {x:.0f} > 1,500 per race outcome")
        for c in exits:
            if c["ex"] != rev:
                sim.chk.violation("cheapfav_exit_unwanted", f"{c['key']} exited {c['ex']} although its edge never reversed")
        if rev and not exits:
            sim.chk.violation("cheapfav_no_exit", f"{rev}: edge reversed by 4c, held position not closed")
        _op, _c, _h, dep = CF.ledger(fills, race_of)
        self.summary = {"entries": len(entries), "exits": len(exits), "deployed": round(dep), "legs": len(_op), "reversed": rev,
                        "per_outcome_max": round(max(per.values())) if per else 0}


class GapPilotSim(Scenario):
    name, cycles, seed = "gappilot", 240, 33
    cfg = {"cheapfav_enabled": True, "cheapfav_budget": 6000.0, "cheapfav_leg_cap": 1500.0, "gap_enabled": True, "gap_budget": 800.0,
           "gap_leg_cap": 300.0, "gap_order_notional": 150.0, "gap_net_max": 400.0}
    doc = "gap-trade pilot: buy the side SIG prices >= 3c under the outside fair on any same-terms two-party leg (not the chamber markets / punt races), walk the book, tiny caps; a leg whose gap closes is sold; positions ride the cheap-favourites ledger"

    def before_cycle(self, sim, i):
        v = sim.v
        if i == 150:
            legs = getattr(sim.bot.store, "get_param", lambda *a: {})("gap_legs", {}) or {}
            held = getattr(sim.bot, "cheapfav_open", None) or {}
            self.closed = None
            for ex in sorted(legs):
                q = held.get(ex)
                race = sim.universe.race_of(ex)
                if not q or race is None or len(race.legs) != 2:
                    continue
                other = [e for e in race.exchange_ids if e != ex][0]
                v.outside[ex] = round(SV.P(v.bid_t[ex]) if q > 0 else SV.P(v.ask_t[ex]), 4)      # the gap closes: fair = SIG's touch
                v.outside[other] = round(1 - v.outside[ex], 4)
                self.closed, self.t_closed = ex, v.now
                break

    def final_checks(self, sim):
        GP = sim.M["gap_pilot"]
        CF = sim.M["cheap_favourites"]
        same = CF.same_terms_exchanges(os.path.join(sim.sb, "data", "research_contract_rules", "tc_mapping.json")) or set()
        calls = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] in ("gp", "gpx")]
        entries = [c for c in calls if c["prefix"] == "gp"]
        exits = [c for c in calls if c["prefix"] == "gpx"]
        if not entries:
            sim.chk.violation("gap_none", "no gap-pilot entry although SIG sits 3c+ under the outside fair on many legs")
        closed = getattr(self, "closed", None)
        per_leg = {}
        for c in entries:
            race = sim.universe.race_of(c["ex"])
            if race is None or c["ex"] not in same or race.name in GP.CHAMBER_RACES or race.name in CF.PUNT_RACES or "I" in race.legs:
                sim.chk.violation("gap_bad_leg", f"{c['key']} {c['ex']} ({race.name if race else '?'}): not a same-terms two-party non-chamber race")
                continue
            if c["ex"] == closed and c["t"] >= self.t_closed:
                sim.chk.violation("gap_after_close", f"{c['key']} bought {c['ex']} after its gap closed")
            elif c["ex"] != closed:
                f = sim.v.outside[c["ex"]]
                p_win = f if c["side"] == "yes" else 1 - f
                if p_win - c["price"] < 0.03 - 1e-9:
                    sim.chk.violation("gap_no_edge", f"{c['key']} {c['ex']} {c['side']} limit {c['price']}: P(win) {p_win:.3f}, edge {p_win - c['price']:.3f}")
            after = c["pos_before"] + (c["qty"] if c["side"] == "yes" else -c["qty"])
            if abs(after) > 4000 + 1e-9:
                sim.chk.violation("gap_over_4000", f"{c['key']} {c['ex']}: leg position {after:+.0f} shares > 4,000")
            per_leg[c["ex"]] = per_leg.get(c["ex"], 0.0) + c["qty"] * (c["price"] if c["side"] == "yes" else c["price"])
        legs = sim.bot.store.get_param("gap_legs", {}) or {}
        fills = [dict(r) for r in sim.bot.store.rows("SELECT exchange_id, quantity, price FROM fills WHERE purpose IN ('cheapfav','cheapfav_exit') ORDER BY filled_at")]
        gap_ex = set(legs) | {c["ex"] for c in entries}
        race_of = {ex: r for ex, r in sim.universe.by_exchange.items()}
        gap_paid = 0.0                                          # peak open cost of the pilot's legs along its fills (the ledger's own measure)
        for k in range(1, len(fills) + 1):
            _o, cost_ex, _h, _t = CF.ledger([f for f in fills[:k] if f["exchange_id"] in gap_ex], race_of)
            gap_paid = max(gap_paid, sum(cost_ex.values()))
        if gap_paid > 800 + 150:
            sim.chk.violation("gap_budget", f"pilot bought {gap_paid:.0f} > budget 800 (+ one order)")
        for ex, x in per_leg.items():
            if x > 300 + 150:
                sim.chk.violation("gap_leg_cap", f"{ex}: {x:.0f} > leg cap 300 (+ one order)")
        for c in exits:
            if c["ex"] != closed and c["t"] < sim.v.now:
                f = sim.v.outside[c["ex"]]
                # an exit of an untouched leg needs a stop / time stop / gap-closed reason: the sim moves nothing else, so none expected
                sim.chk.violation("gap_exit_unwanted", f"{c['key']} exited {c['ex']} although its gap never closed")
        if closed and not exits:
            sim.chk.violation("gap_no_exit", f"{closed}: gap closed (fair = SIG touch), held position not sold")
        self.summary = {"entries": len(entries), "exits": len(exits), "gap_legs": len(legs), "paid": round(gap_paid), "closed": closed}


class DevChurnSim(Scenario):
    name, cycles, seed = "churn", 200, 29
    cfg = {"churn_enabled": True}
    doc = "deviation churn: 24 h of normal quotes preloaded; one race's set gap to payout jumps 6 ticks above its median, reverts 30 min later: buy once (all-or-none), sell at +2 ticks or better, ledger empty and profit at the end; a second race spikes but is Rhode Island-style excluded by a 3-leg check"

    def bot_setup(self, sim):
        v = sim.v
        self.race = two_leg_mid_races(sim, 0.3, 0.7)[0]
        legs = sim.universe.races[self.race].legs
        self.base = sum(SV.P(v.bid_t[l.exchange_id]) for l in legs.values())
        rows = []
        for k in range(150):                       # 25 h of 10-min heartbeats at today's quotes
            t = v.now - 90000 + 600 * k
            for party, l in legs.items():
                b, a = SV.P(v.bid_t[l.exchange_id]), SV.P(v.ask_t[l.exchange_id])
                rows.append((t, "sig", self.race, party, l.exchange_id, b, a, (b + a) / 2, None, None, None, None))
        sim.bot.store.conn.executemany("INSERT OR REPLACE INTO venue_quotes(ts,venue,race,party,instrument,bid,ask,mid,bid_sz,ask_sz,depth_usd,latency_ms) "
                                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        sim.bot.store.conn.commit()

    def before_cycle(self, sim, i):
        if i == 30:
            set_race_bids_sum(sim, self.race, self.base + 0.03)
        if i == 90:
            set_race_bids_sum(sim, self.race, self.base)

    def final_checks(self, sim):
        calls = [c for c in sim.v.calls if c["kind"] == "multileg" and c.get("ok")]
        ent = [c for c in calls if c["prefix"] == "arb" and sim.race_of(str(c["legs"][0]["exchangeId"])) == self.race]
        ext = [c for c in calls if c["prefix"] == "unw" and sim.race_of(str(c["legs"][0]["exchangeId"])) == self.race]
        ch = sim.bot.churn
        if not ent:
            sim.chk.violation("churn_none", f"no churn entry although {self.race}'s set gap jumped 6 ticks above its 24-h median")
        if ent and not ext:
            sim.chk.violation("churn_no_exit", f"{self.race}: bought but never sold after the gap reverted")
        if ch.lots:
            sim.chk.violation("churn_ledger_left", f"churn ledger still holds {ch.lots}")
        if ent and ch.realised <= 0:
            sim.chk.violation("churn_loss", f"realised {ch.realised:+.1f} on a +6-tick spike that fully reverted")
        if sum(c["cash_before"] - c["cash_after"] for c in ent) > 12000 * 1.01:
            sim.chk.violation("churn_over_pot", "churn bought more than its 12k pot")
        self.summary = {"entries": len(ent), "exits": len(ext), "realised": round(ch.realised, 1)}


# ---------------------------------------------------------------- high-frequency arb (churn) stress: Fable 24 Oct, ported
VQ_INS = ("INSERT OR REPLACE INTO venue_quotes(ts,venue,race,party,instrument,bid,ask,mid,bid_sz,ask_sz,depth_usd,latency_ms) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")


def DCm():
    return sys.modules["sigbot.proposals.dev_churn"]


def churn_races(sim, n):
    return two_leg_mid_races(sim, 0.3, 0.7, exclude=("Rhode Island Governor",))[:n]


def preload_quotes(sim, races, hours=25.0, noise_p=0.0, rng=None):
    """10-min 'sig' heartbeat rows for `hours` back at today's touch; with noise_p, leg 0 sits +-1 tick off with that
    probability (gives the volatility ratio a non-zero base)."""
    v, rows = sim.v, []
    for race in races:
        legs = list(sim.universe.races[race].legs.items())
        for k in range(int(hours * 6)):
            t = v.now - hours * 3600 + 600 * k
            for i, (party, l) in enumerate(legs):
                bt, at = v.bid_t[l.exchange_id], v.ask_t[l.exchange_id]
                if i == 0 and rng is not None and rng.random() < noise_p:
                    d = rng.choice([-1, 1])
                    bt, at = bt + d, at + d
                b, a = SV.P(bt), SV.P(at)
                rows.append((t, "sig", race, party, l.exchange_id, b, a, (b + a) / 2, None, None, None, None))
    sim.bot.store.conn.executemany(VQ_INS, rows)
    sim.bot.store.conn.commit()


def bids_sum(sim, race):
    return sum(SV.P(sim.v.bid_t[l.exchange_id]) for l in sim.universe.races[race].legs.values())


def venue_sets(sim, race):
    pos = sim.v.signed_positions()
    qs = [pos.get(e, 0.0) for e in sim.legs(race)]
    return int(min(-q for q in qs)) if qs and all(q < 0 for q in qs) else 0


def churn_sets(sim, race):
    """Venue sets in `race` beyond the ones the market maker's own fills (purposes mm / inside) would form by themselves: the
    venue's sets less the MM-only sets, never below 0. A fill with no purpose still counts, so a real churn set on top of MM
    inventory is seen. 31 Oct: by the sell-all cycle the MM's quotes had collected -19 and -740 on the two legs of IA-02 (purpose
    mm only, no churn fill), and the old reading, which counted that as a set, failed an unchanged main. (Not 'venue position
    less MM fills': an MM long on one leg would then make a flat race look like a set.)"""
    legs = list(sim.legs(race))
    sets = venue_sets(sim, race)
    if not sets:
        return 0
    mm = {str(r["exchange_id"]): float(r["q"] or 0.0) for r in sim.bot.store.rows(
        "SELECT exchange_id, SUM(quantity) q FROM fills WHERE purpose IN ('mm','inside') GROUP BY exchange_id")}
    qs = [mm.get(e, 0.0) for e in legs]
    mm_sets = int(min(-q for q in qs)) if qs and all(q < 0 for q in qs) else 0
    return max(0, sets - mm_sets)


def is_main(c):
    return str(c.get("step", "")).startswith("cycle")       # main-loop step = churn; scanner steps = recycle / free unwind


class ChurnBase(Scenario):
    """Stress at the sleeve's design maximum (12k pot, -600 stop), recycling on as in the live cash-mode config. Checked at
    every step boundary: the venue's sets in these races (priced at their entry cost) stay within the pot and the per-race
    cap at any moment (concurrent, not cumulative turnover); the ledger never claims more than the venue holds."""
    cfg = {"churn_enabled": True, "churn_capital": 12000.0, "churn_kill": -600.0}
    pass_dt = 30.0
    n_races = 1

    def bot_setup(self, sim):
        self.races = churn_races(sim, self.n_races)
        preload_quotes(sim, self.races)
        self.common_setup(sim)

    def common_setup(self, sim):
        self.base = {r: bids_sum(sim, r) for r in self.races}
        self.kill_t = self.pause_t = None
        self.max_tot, self.max_race, self.cost, self._ci = 0.0, {}, {}, 0
        sim.chk.strict_unwinds = True

    def track(self, sim, ledger=False):
        ch = sim.bot.churn
        for c in sim.v.calls[self._ci:]:
            if c["kind"] == "multileg" and c["prefix"] == "arb" and c.get("ok") and c["legs"]:
                q = c["legs"][0]["quantity"]
                self.cost[sim.race_of(str(c["legs"][0]["exchangeId"]))] = (c["cash_before"] - c["cash_after"]) / q if q else 1.0
        self._ci = len(sim.v.calls)
        per = {r: venue_sets(sim, r) * self.cost.get(r, 1.0) for r in self.races}
        tot = sum(per.values())
        if tot > self.max_tot:
            self.max_tot = tot
            if tot > ch.capital * 1.01 and not getattr(self, "pot_flagged", False):
                self.pot_flagged = True
                sim.chk.violation("churn_over_pot", f"venue sets in churn races worth {tot:.0f} at entry cost > pot {ch.capital:.0f} at one moment")
        for r, x in per.items():
            if x > self.max_race.get(r, 0.0):
                self.max_race[r] = x
                if x > ch.per_race * 1.01 and r not in getattr(self, "race_flagged", set()):
                    self.race_flagged = getattr(self, "race_flagged", set()) | {r}
                    sim.chk.violation("churn_over_race_cap", f"{r}: venue sets worth {x:.0f} > per-race cap {ch.per_race:.0f} at one moment")
            if ledger and ch.held(r) > venue_sets(sim, r):     # after a cycle: churn reconciled taker fills on MM quotes
                sim.chk.violation("churn_ledger_overstates", f"{r}: ledger {ch.held(r)} > venue sets {venue_sets(sim, r)}")
        if ch.killed and self.kill_t is None:
            self.kill_t = CLOCK.t
        if ch.vol_paused and self.pause_t is None:
            self.pause_t = CLOCK.t

    def after_cycle(self, sim, i):
        self.track(sim, ledger=True)

    def on_tick(self, sim):
        self.track(sim)

    def calls(self, sim, prefixes, ok=True, main=None):
        return [c for c in sim.v.calls if c["kind"] == "multileg" and c["prefix"] in prefixes and bool(c.get("ok")) == ok and c["legs"]
                and sim.race_of(str(c["legs"][0]["exchangeId"])) in self.races and (main is None or is_main(c) == main)]

    def entries_after(self, sim, t, race=None):
        return [c for c in self.calls(sim, ("arb",), main=True) if c["t"] > t
                and (race is None or sim.race_of(str(c["legs"][0]["exchangeId"])) == race)]

    def report(self, sim):
        self.track(sim)
        ch = sim.bot.churn
        ent, ext = self.calls(sim, ("arb",), main=True), self.calls(sim, ("unw", "fre"), main=True)
        try:
            st = json.load(open(os.path.join(sim.sb, "params", "churn_state.json")))
        except Exception:
            st = None
        return {"entries": len(ent), "rejected": len(self.calls(sim, ("arb",), ok=False)), "exits": len(ext),
                "recycle_buys": len(self.calls(sim, ("arb",), main=False)), "scanner_unwinds": len(self.calls(sim, ("unw", "fre"), main=False)),
                "turnover": round(sum(c["cash_before"] - c["cash_after"] for c in ent)), "max_concurrent": round(self.max_tot),
                "max_race": round(max(self.max_race.values(), default=0.0)), "realised": round(ch.realised, 1), "killed": ch.killed,
                "vol_paused": ch.vol_paused, "lots_left": len(ch.lots), "venue_sets_left": sum(venue_sets(sim, r) for r in self.races),
                "state_file_lots": None if st is None else len(st.get("lots", [])), "venue_pnl": round(sim.v.value() - sim.start_value, 1)}

    def final_checks(self, sim):
        self.summary = self.report(sim)
        if self.summary["recycle_buys"]:
            sim.chk.violation("churn_recycled_into", f"{self.summary['recycle_buys']} scanner baskets bought into churn races (recycling)")


class ChurnWiden(ChurnBase):
    name, cycles, seed, n_races = "churn_widen", 420, 31, 3
    doc = "3 races: +6 ticks at cycle 20, then +2 ticks every 10 min up to +20: each race is sold at the touch at a 9-tick loss or by the stop, never re-bought, loss bounded by the stop"

    def bot_setup(self, sim):
        super().bot_setup(sim)
        DCm().MAX_HOLD = 2 * 3600.0

    def before_cycle(self, sim, i):
        if i >= 20 and (i - 20) % 20 == 0:
            k = min((i - 20) // 20, 7)
            for r in self.races:
                set_race_bids_sum(sim, r, self.base[r] + 0.03 + 0.01 * k)

    def final_checks(self, sim):
        super().final_checks(sim)
        rep, ch = self.summary, sim.bot.churn
        DCm().MAX_HOLD = 24 * 3600.0
        if rep["entries"] == 0:
            sim.chk.violation("churn_none", "no entry although every gap jumped 6 ticks")
        if rep["lots_left"]:
            sim.chk.violation("churn_held_widening", f"ledger still holds {sim.bot.churn.lots} while the gap kept widening")
        for r in self.races:
            ex = [c["t"] for c in self.calls(sim, ("unw",), main=True) if sim.race_of(str(c["legs"][0]["exchangeId"])) == r]
            if ex and self.entries_after(sim, max(ex), r):
                sim.chk.violation("churn_rebought_after_loss", f"{r}: bought again after its loss exit")
        if ch.realised < ch.kill - 0.02 * ch.capital:
            sim.chk.violation("churn_loss_unbounded", f"realised {ch.realised:.0f} far beyond the {ch.kill:.0f} stop")
        if self.kill_t and self.entries_after(sim, self.kill_t):
            sim.chk.violation("churn_entry_after_kill", "entries after the stop")


class ChurnReject(ChurnBase):
    name, cycles, seed, n_races = "churn_reject", 120, 32, 3
    doc = "3 races +6 ticks at cycle 20, every all-or-none basket rejected: <= 1 attempt / race / 5 min, no ledger entries"

    def bot_setup(self, sim):
        super().bot_setup(sim)
        sim.v.reject_aon = lambda legs, prefix: prefix == "arb"

    def before_cycle(self, sim, i):
        if i == 20:
            for r in self.races:
                set_race_bids_sum(sim, r, self.base[r] + 0.03)

    def final_checks(self, sim):
        super().final_checks(sim)
        by = {}
        for c in self.calls(sim, ("arb",), ok=False):
            by.setdefault(sim.race_of(str(c["legs"][0]["exchangeId"])), []).append(c["t"])
        worst = sum(1 for ts in by.values() for a, b in zip(sorted(ts), sorted(ts)[1:]) if b - a < 300 - 1)
        if worst:
            sim.chk.violation("churn_hammering", f"{worst} re-attempts within 5 min of a rejection")
        if not by:
            sim.chk.violation("churn_none", "no attempt at all")
        if self.summary["lots_left"] or self.summary["state_file_lots"]:
            sim.chk.violation("churn_phantom_lot", f"ledger lots without a fill: {sim.bot.churn.lots}")


class ChurnRestart(ChurnBase):
    name, cycles, seed, n_races = "churn_restart", 120, 33, 1
    doc = "+6 ticks at cycle 20 (entry), a new Bot over the same DB and state file at cycle 40, gap reverts at 60: ledger reloads, one exit, nothing left"

    def before_cycle(self, sim, i):
        r = self.races[0]
        if i == 20:
            set_race_bids_sum(sim, r, self.base[r] + 0.03)
        if i == 40:
            old = sim.bot
            self.lots_before = [dict(l) for l in old.churn.lots]
            old.stop = True
            try:
                old.store.conn.close()
            except Exception:
                pass
            new = sim.R.Bot(sim.cfg, dry_run=False)
            if new.news is not None:
                new.news.collect = lambda prices: ([], {"sources": 0, "failed": 0, "items": 0})
            new.arb.api = new.scan_api
            new.scanner = sim.R.ArbScanner(new)
            sim.bot = new
            self.lots_after = [dict(l) for l in new.churn.lots]
        if i == 60:
            set_race_bids_sum(sim, r, self.base[r])

    def final_checks(self, sim):
        super().final_checks(sim)
        rep = self.summary
        lb, la = getattr(self, "lots_before", None), getattr(self, "lots_after", None)
        if not lb:
            sim.chk.violation("churn_none", "no lot before the restart")
        elif lb != la:
            sim.chk.violation("churn_reload", f"ledger before {lb} != after reload {la}")
        if rep["exits"] != 1:
            sim.chk.violation("churn_exit_count", f"{rep['exits']} exits (want exactly 1)")
        if rep["lots_left"] or rep["venue_sets_left"]:
            sim.chk.violation("churn_left_over", f"lots {rep['lots_left']} venue sets {rep['venue_sets_left']}")


class ChurnFreeFirst(ChurnBase):
    name, cycles, seed, n_races = "churn_free_first", 90, 34, 1
    doc = "entry at +6 ticks; between cycles the asks drop to the payout: the scanner's free unwind sells first, churn books it and never sells again"

    def before_cycle(self, sim, i):
        if i == 20:
            set_race_bids_sum(sim, self.races[0], self.base[self.races[0]] + 0.03)

    def on_tick(self, sim):
        super().on_tick(sim)
        if sim.cycle_i == 40 and not getattr(self, "dropped", False):
            self.dropped = True
            set_race_bids_sum(sim, self.races[0], 0.99)      # asks sum 1.00: proceeds 1.0 >= payout 1

    def final_checks(self, sim):
        super().final_checks(sim)
        ok = [c for c in sim.v.calls if c["kind"] == "multileg" and c.get("ok") and c["legs"] and sim.race_of(str(c["legs"][0]["exchangeId"])) in self.races]
        bought = sum(c["legs"][0]["quantity"] for c in ok if c["prefix"] == "arb")
        sold = sum(c["legs"][0]["quantity"] for c in ok if c["prefix"] in ("unw", "fre"))
        self.summary.update(sets_bought=bought, sets_sold=sold)
        if not bought:
            sim.chk.violation("churn_none", "no entry")
        if sold != bought:
            sim.chk.violation("churn_oversold" if sold > bought else "churn_left_over", f"bought {bought} sets, unwound {sold}")
        if self.summary["lots_left"]:
            sim.chk.violation("churn_left_over", f"ledger still {sim.bot.churn.lots}")
        if bought and sim.bot.churn.realised <= 0:
            sim.chk.violation("churn_exit_unbooked", f"realised {sim.bot.churn.realised:+.1f}: the free unwind at the payout was not booked")


class ChurnStaleFill(ChurnBase):
    name, cycles, seed, n_races = "churn_stale_fill", 40, 41, 1
    cfg = {"churn_enabled": True}            # the live config's pot / stop (4k / -200)
    doc = "Fable review 2: entry at +6 ticks (cycle 20); at cycle 30 the gap reverts and, after that cycle's positions read, a taker hits our MM bid on leg 1 for 300 YES before churn's exit: the exit sells only the sets held then, no leg crosses zero"
    fill = 300

    def bot_setup(self, sim):
        super().bot_setup(sim)
        b = sim.bot
        real = b.churn_cycle
        self.hit = False

        def wrapped(positions, killed, cycle_id=None):
            if sim.cycle_i == 30 and not self.hit:
                self.hit = True                   # the sim applies taker flow only between steps; this lands inside the cycle
                sim.seed_holding(sim.legs(self.races[0])[0], "yes", self.fill, "mm")
            return real(positions, killed, cycle_id)
        b.churn_cycle = wrapped

    def before_cycle(self, sim, i):
        r = self.races[0]
        if i == 20:
            set_race_bids_sum(sim, r, self.base[r] + 0.03)
        if i == 30:
            set_race_bids_sum(sim, r, self.base[r])

    def final_checks(self, sim):
        super().final_checks(sim)
        pos = sim.v.signed_positions()
        self.summary.update(hit=self.hit, pos_end={e: pos.get(e, 0.0) for e in sim.legs(self.races[0])}, ledger=sim.bot.churn.lots)
        if not self.hit:
            sim.chk.violation("stale_fill_not_exercised", "the mid-cycle fill was never injected")
        if not self.summary["exits"]:
            sim.chk.violation("churn_no_exit", "no exit after the gap reverted")


class CashRaise(ChurnBase):
    name, cycles, seed, n_races = "cash_raise", 80, 42, 12
    # high-frequency arb as the only cash user (cheap favourites have their own scenario, cash_raise_cheapfav)
    cfg = {"churn_enabled": True, "churn_capital": 12000.0, "churn_kill": -600.0, "raise_enabled": True, "cheapfav_enabled": False}
    doc = ("cash raise: 12 races' gaps +6 ticks at cycle 20 with 9k cash, so high-frequency arb is blocked by its 3k floor; "
           "MM leftovers on 6 other races (one a 4,500-share leg), an arb set and a buy-set: the raise sells only the "
           "shortfall + buffer, only < 1% of mid, never a set or the big leg, never through zero, the MM stays off the "
           "legs, and the arb then enters with the cash; nothing is sold before the need or once it is met")

    def bot_setup(self, sim):
        super().bot_setup(sim)
        self.others = [r for r in two_leg_mid_races(sim, 0.3, 0.7, exclude=("Rhode Island Governor",)) if r not in self.races][:9]
        self.big_leg = sim.legs(self.others[5])[1]
        sim.seed_holding(self.big_leg, "yes", 4500, "mm")
        self.set_race, self.buyset_race = self.others[6], self.others[7]
        sim.seed_set(self.set_race, 800)
        for ex in sim.legs(self.buyset_race):
            sim.seed_holding(ex, "yes", 500, "arb")
        self.protected_legs = set(sim.legs(self.set_race)) | set(sim.legs(self.buyset_race)) | {self.big_leg}
        seed_cost = 0.0                # the cycle-19 leftovers below, priced as seed_holding prices them
        for k, r in enumerate(self.others[:5]):
            ex = sim.legs(r)[k % 2]
            seed_cost += 900 * ((1 - SV.P(sim.v.bid_t[ex])) if k == 1 else SV.P(sim.v.ask_t[ex]))
        sim.v.cash = 9000.0 + seed_cost  # ~9k after them: the 3k floor binds once 12 gaps open
        sim.start_value = sim.v.value()
        self.per_cycle = []

    def before_cycle(self, sim, i):
        if i == 19:                    # MM / tilt leftovers the moment before the need (the MM unwinds them over time)
            for k, r in enumerate(self.others[:5]):
                sim.seed_holding(sim.legs(r)[k % 2], "no" if k == 1 else "yes", 900, "mm")
        if i == 20:
            for r in self.races:
                set_race_bids_sum(sim, r, self.base[r] + 0.03)

    def after_cycle(self, sim, i):
        super().after_cycle(sim, i)
        needs = dict(sim.bot.cash_needs or {})
        clips = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "raise" and c.get("step") == f"cycle {i}"]
        got = sum(c["qty"] * (1 - c["price"]) for c in clips)
        self.per_cycle.append((i, sum(a for a, _ in needs.values()), got, len(clips)))
        if clips and not needs:
            sim.chk.violation("raise_without_need", f"cycle {i}: {len(clips)} raise clips with no posted need")
        if clips and got > sum(a for a, _ in needs.values()) + sim.cfg.raise_buffer + 1.0:
            sim.chk.violation("raise_over_need", f"cycle {i}: raised {got:.0f} for needs {needs} + buffer {sim.cfg.raise_buffer:.0f}")
        for c in clips:
            mid, px, ex = c["mid"], c["price"], c["ex"]
            cost = (mid - (1 - px)) / mid if c["side"] == "no" else (px - mid) / (1 - mid)
            if cost >= sim.cfg.raise_max_cost - 1e-9:
                sim.chk.violation("raise_over_cost", f"{c['key']} {ex} {c['side']} x{c['qty']} @{px}: {100 * cost:.2f}% of mid {mid:.4f}")
            if ex in self.protected_legs or sim.chk.sim.bot.churn.held(sim.race_of(ex)) > 0:   # (raise runs after churn's entries)
                sim.chk.violation("raise_sold_excluded", f"{c['key']} {ex}: a set, a > 4,000-share leg or a race high-frequency arb holds")
            b, a = c["pos_before"], c.get("pos_after", c["pos_before"])
            if b == 0 or (b > 0 > a) or (b < 0 < a):
                sim.chk.violation("raise_crossed_zero", f"{c['key']} {ex}: {b:+d} -> {a:+d}")

    def final_checks(self, sim):
        super().final_checks(sim)
        clips = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "raise"]
        pause_from, idx = {}, {id(c): i for i, c in enumerate(sim.v.calls)}
        for c in clips:
            pause_from.setdefault(c["ex"], (idx[id(c)], c["t"]))
        # after a leg's first clip (call order: the sim clock does not move inside a step) and within the pause
        mm_on_raised = [c for i, c in enumerate(sim.v.calls) if c["kind"] == "order" and c["prefix"] == "bat" and c["ex"] in pause_from
                        and i > pause_from[c["ex"]][0] and c["t"] < pause_from[c["ex"]][1] + sim.cfg.raise_pause_sec]
        # MM quotes resting on a leg when its clip is sent must have been pulled first (no self-match, no buy-back)
        unpulled = []
        for c in clips:
            i = idx[id(c)]
            prior = [j for j, x in enumerate(sim.v.calls[:i]) if x["kind"] == "order" and x["prefix"] == "bat" and x["ex"] == c["ex"]
                     and x.get("step") == c.get("step")]
            if prior and not any(x["kind"] == "cancel_all" and x.get("ex") == c["ex"] for x in sim.v.calls[prior[-1]:i]):
                unpulled.append(c["key"])
        if unpulled:
            sim.chk.violation("raise_mm_not_pulled", f"clips sent while this cycle's MM quotes still rested on the leg: {unpulled[:3]}")
        ent_after = [c for c in self.calls(sim, ("arb",), main=True) if clips and c["t"] >= clips[0]["t"]]
        raised = sum(c["qty"] * (1 - c["price"]) for c in clips)
        self.summary.update(raise_clips=len(clips), raised=round(raised), legs_sold=sorted(pause_from),
                            needs_cycles=sum(1 for _, n, _, _ in self.per_cycle if n > 0), entries_after_raise=len(ent_after))
        if not clips:
            sim.chk.violation("raise_none", "high-frequency arb was blocked by its floor but nothing was raised")
        if mm_on_raised:
            sim.chk.violation("raise_mm_quoted", f"{len(mm_on_raised)} MM orders on a leg during its raise pause, first {mm_on_raised[0]['key']} {mm_on_raised[0]['ex']}")
        if clips and not ent_after:
            sim.chk.violation("raise_unused", "the raised cash never funded an entry")
        before = [c for c in clips if c["t"] < T0 + 20 * self.pass_dt]
        if before:
            sim.chk.violation("raise_before_need", f"{len(before)} clips before any sleeve was short")
        sold = {c["ex"] for c in clips}
        hit = sold & self.protected_legs
        if hit:
            sim.chk.violation("raise_touched_set", f"an arb set, buy-set or > 4,000-share leg was sold: {sorted(hit)}")


class CashRaiseSets(ChurnBase):
    name, cycles, seed, n_races = "cash_raise_sets", 200, 43, 12
    cfg = {"churn_enabled": True, "churn_capital": 12000.0, "churn_kill": -600.0, "raise_enabled": True, "raise_sets_enabled": True,
           "cheapfav_enabled": False}
    doc = ("cash raise tier 2: 12 gaps +6 ticks at cycle 20 with ~9k cash and no tier-1 leftovers; held: a cheap sell-set, an "
           "expensive one (6%+ to exit), a buy-set and a cheap set in a race held through an early call: sets are unwound "
           "(all-or-none, pieces <= 80% of the touch, never more than the need) only once high-frequency arb has skipped "
           "entries for cash in >= 3 half-hours (not on the first burst), never the expensive set or the held race")

    def bot_setup(self, sim):
        super().bot_setup(sim)
        others = [r for r in two_leg_mid_races(sim, 0.3, 0.7, exclude=("Rhode Island Governor",)) if r not in self.races][:4]
        self.cheap, self.dear, self.buyset, self.held_race = others
        set_race_bids_sum(sim, self.dear, 1.06)            # asks ~1.07: unwinding fetches ~0.93/set, 7% under its payout
        for r in (self.cheap, self.dear, self.held_race):
            sim.seed_set(r, 1500)
        for ex in sim.legs(self.buyset):
            sim.seed_holding(ex, "yes", 1500, "arb")
        sim.cfg.raise_set_hold_races = [self.held_race]
        sim.v.cash = 9000.0 + 0.0
        sim.start_value = sim.v.value()
        self.per_cycle, self.skips_at = [], {}

    def before_cycle(self, sim, i):
        if i == 20:
            for r in self.races:
                set_race_bids_sum(sim, r, self.base[r] + 0.03)

    def set_calls(self, sim, step=None):
        # the raise's baskets only: the bot records each leg with purpose 'raise_set'. Other set unwinds share the key
        # prefix, e.g. high-frequency arb's own loss exit of a lot from the copied params/churn_state.json (24 Oct precheck)
        own = {self.cheap, self.dear, self.buyset, self.held_race}
        mine = {int(r["order_id"]) for r in sim.bot.store.rows("SELECT order_id FROM orders WHERE purpose='raise_set' AND order_id IS NOT NULL")}
        return [c for c in sim.v.calls if c["kind"] == "multileg" and c.get("ok") and c["prefix"] in ("unw", "unb") and c["legs"]
                and any(int(x) in mine for x in c.get("order_ids") or [])
                and sim.race_of(str(c["legs"][0]["exchangeId"])) in own and (step is None or c.get("step") == step)]

    def after_cycle(self, sim, i):
        super().after_cycle(sim, i)
        needs = dict(sim.bot.cash_needs or {})
        skips = sim.bot.churn_skip_windows_6h(sim.cfg.raise_set_skip_window)
        pieces = self.set_calls(sim, f"cycle {i}")
        clips = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "raise" and c.get("step") == f"cycle {i}"]
        got = sum(c["cash_after"] - c["cash_before"] for c in pieces) + sum(c["qty"] * (1 - c["price"]) for c in clips)
        self.per_cycle.append((i, round(sum(a for a, _ in needs.values())), skips, len(pieces), round(got)))
        if pieces and skips < sim.cfg.raise_set_min_skips:
            sim.chk.violation("raise_set_early", f"cycle {i}: set unwound with cash skips in only {skips} half-hours of the last 6 h")
        if pieces and not needs:
            sim.chk.violation("raise_without_need", f"cycle {i}: set unwound with no posted need")
        if got > sum(a for a, _ in needs.values()) + sim.cfg.raise_buffer + 2.0:
            sim.chk.violation("raise_over_need", f"cycle {i}: raised {got:.0f} for needs {needs}")
        for c in pieces:
            r = sim.race_of(str(c["legs"][0]["exchangeId"]))
            if r in (self.dear, self.held_race):
                sim.chk.violation("raise_set_excluded", f"{c['key']} {r}: the expensive set or a race held through the call")
            if c["prefix"] == "unb":
                pos = sim.v.signed_positions()
                if any(pos.get(e, 0.0) < 0 for e in sim.legs(r)):
                    sim.chk.violation("raise_buyset_crossed", f"{c['key']} {r}: a buy-set leg went short")

    def final_checks(self, sim):
        super().final_checks(sim)
        pieces = self.set_calls(sim)
        self.summary.update(set_pieces=len(pieces), set_races=sorted({sim.race_of(str(c["legs"][0]["exchangeId"])) for c in pieces}),
                            raised_sets=round(sum(c["cash_after"] - c["cash_before"] for c in pieces)),
                            first_set_cycle=next((x for x in self.per_cycle if x[3]), None), skips_end=sim.bot.churn_skips_6h(),
                            skip_windows_end=sim.bot.churn_skip_windows_6h(sim.cfg.raise_set_skip_window))
        first = next((x for x in self.per_cycle if x[1]), None)
        if first and self.summary["first_set_cycle"] and self.summary["first_set_cycle"][0] <= first[0]:
            sim.chk.violation("raise_set_on_burst", "a set was unwound in the first cycle of the need")
        if not pieces:
            sim.chk.violation("raise_set_none", "tier 1 was empty and the arb kept skipping for cash, but no set was unwound")
        if not self.calls(sim, ("arb",), main=True):
            sim.chk.violation("raise_unused", "no high-frequency arb entry at all")


class CashRaiseCheapfav(CheapFav):
    name, cycles, seed = "cash_raise_cheapfav", 240, 44
    cfg = {"cheapfav_enabled": True, "cheapfav_budget": 6000.0, "cheapfav_leg_cap": 1500.0, "raise_enabled": True, "raise_sets_enabled": False}
    doc = ("cash raise for cheap favourites (the launch use): favourites 2-5c cheap but cash ~500 above the sleeve's 1k floor, "
           "MM leftovers of 2,000 shares on 4 other legs: the raise sells (< 1% of mid, never through zero) only in cycles "
           "where cheap favourites posted a need, never more than need + buffer, and the sleeve then buys with the cash")

    def bot_setup(self, sim):
        self.left = []
        for r in two_leg_mid_races(sim, 0.3, 0.7, exclude=("Rhode Island Governor",))[:4]:
            ex = sim.legs(r)[0]
            sim.seed_holding(ex, "yes", 2000, "mm")
            self.left.append(ex)
        sim.v.cash = 1500.0
        sim.start_value = sim.v.value()
        self.per_cycle = []

    def after_cycle(self, sim, i):
        needs = dict(sim.bot.cash_needs or {})
        clips = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "raise" and c.get("step") == f"cycle {i}"]
        got = sum(c["qty"] * (1 - c["price"]) for c in clips)
        self.per_cycle.append((i, round(sum(a for a, _ in needs.values())), len(clips), round(got)))
        if clips and "cheapfav" not in needs:
            sim.chk.violation("raise_without_need", f"cycle {i}: {len(clips)} raise clips without a cheap-favourites need ({needs})")
        if got > sum(a for a, _ in needs.values()) + sim.cfg.raise_buffer + 1.0:
            sim.chk.violation("raise_over_need", f"cycle {i}: raised {got:.0f} for {needs}")
        for c in clips:
            mid, px = c["mid"], c["price"]
            cost = (mid - (1 - px)) / mid if c["side"] == "no" else (px - mid) / (1 - mid)
            if cost >= sim.cfg.raise_max_cost - 1e-9:
                sim.chk.violation("raise_over_cost", f"{c['key']} {c['ex']}: {100 * cost:.2f}% of mid")
            b, a = c["pos_before"], c.get("pos_after", c["pos_before"])
            if b == 0 or (b > 0 > a) or (b < 0 < a):
                sim.chk.violation("raise_crossed_zero", f"{c['key']} {c['ex']}: {b:+d} -> {a:+d}")

    def final_checks(self, sim):
        super().final_checks(sim)
        clips = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "raise"]
        cf_after = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "cf" and clips and c["t"] > clips[0]["t"]]
        self.summary.update(raise_clips=len(clips), raised=round(sum(c["qty"] * (1 - c["price"]) for c in clips)),
                            need_cycles=sum(1 for x in self.per_cycle if x[1]), cf_entries_after_raise=len(cf_after))
        if not clips:
            sim.chk.violation("raise_none", "cheap favourites were short of cash but nothing was raised")
        elif not cf_after:
            sim.chk.violation("raise_unused", "no cheap-favourite entry after the raise")


class ShockReserve(CheapFav):
    name, cycles, seed = "shock_reserve", 240, 45
    cfg = {"cheapfav_enabled": True, "cheapfav_budget": 6000.0, "cheapfav_leg_cap": 1500.0, "raise_enabled": True,
           "raise_sets_enabled": False, "shock_enabled": True, "shock_reserve": 15000.0}
    doc = ("shock reserve (shock mode stubbed on): cheap favourites trade on 30k cash; at cycle 120 a shock buys enough to leave "
           "~12k free, under the 15k reserve: the raise refills it from MM leftovers (tier 1, < 1%, never the shock position, "
           "never more than deficit + buffer per cycle), and no cheap-favourite entry takes cash under its 1k floor + 15k")

    def bot_setup(self, sim):
        self.left = []
        sim.v.cash = 30000.0
        sim.start_value = sim.v.value()
        self.per_cycle, self.t_shock, self.shock_ex = [], None, set()

    def before_cycle(self, sim, i):
        super().before_cycle(sim, i)
        races = two_leg_mid_races(sim, 0.3, 0.7, exclude=("Rhode Island Governor",))
        if i == 119:                   # MM leftovers just before the shock (the MM unwinds them over time)
            for k, r in enumerate(races[:4]):
                ex = sim.universe.races[r].legs["D" if k % 2 else "R"].exchange_id
                sim.seed_holding(ex, "yes", 1500, "mm")
                self.left.append(ex)
        if i == 120:                   # the shock buys D YES in one race and R YES in another (partisan-neutral, as the
            spend = max(0.0, sim.v.cash - 12000.0)     # checker does not know shock mode is exempt from the cap)
            for r, party in ((races[8], "D"), (races[9], "R")):
                ex = sim.universe.races[r].legs[party].exchange_id
                q = int(spend / 2 / SV.P(sim.v.ask_t[ex]))
                sim.seed_holding(ex, "yes", q, "shock")   # recorded as a shock order: the reserve's only legitimate spender
                self.shock_ex.add(ex)
            self.t_shock = sim.v.now

    def after_cycle(self, sim, i):
        needs = dict(sim.bot.cash_needs or {})
        clips = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "raise" and c.get("step") == f"cycle {i}"]
        got = sum(c["qty"] * (1 - c["price"]) for c in clips)
        self.per_cycle.append((i, {k: round(a) for k, (a, _) in needs.items()}, len(clips), round(got), round(sim.v.cash)))
        if clips and not needs:
            sim.chk.violation("raise_without_need", f"cycle {i}: {len(clips)} clips without a need or a reserve deficit")
        if clips and self.t_shock is None and "cheapfav" not in needs:
            sim.chk.violation("raise_before_shock", f"cycle {i}: raised before any shock spend without a cheap-favourites need")
        if got > sum(a for a, _ in needs.values()) + sim.cfg.raise_buffer + 1.0:
            sim.chk.violation("raise_over_need", f"cycle {i}: raised {got:.0f} for {needs}")
        for c in clips:
            if c["ex"] in self.shock_ex:
                sim.chk.violation("raise_sold_shock", f"{c['key']}: sold the shock position")
            mid, px = c["mid"], c["price"]
            cost = (mid - (1 - px)) / mid if c["side"] == "no" else (px - mid) / (1 - mid)
            if cost >= sim.cfg.raise_max_cost - 1e-9:
                sim.chk.violation("raise_over_cost", f"{c['key']}: {100 * cost:.2f}% of mid")
            b, a = c["pos_before"], c.get("pos_after", c["pos_before"])
            if b == 0 or (b > 0 > a) or (b < 0 < a):
                sim.chk.violation("raise_crossed_zero", f"{c['key']} {c['ex']}: {b:+d} -> {a:+d}")
        for c in [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "cf" and c.get("step") == f"cycle {i}"]:
            if c["cash_before"] - c["qty"] * c["price"] < 1000.0 + 15000.0 - 1.0:
                sim.chk.violation("reserve_spent_by_cheapfav", f"{c['key']}: cash {c['cash_before']:.0f} -> "
                                  f"{c['cash_before'] - c['qty'] * c['price']:.0f}, under the 1k floor + 15k reserve")

    def final_checks(self, sim):
        super().final_checks(sim)
        clips = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "raise"]
        after = [c for c in clips if self.t_shock is not None and c["t"] >= self.t_shock]
        cash_end = sim.v.cash
        self.summary.update(raise_clips=len(clips), after_shock=len(after), raised_after=round(sum(c["qty"] * (1 - c["price"]) for c in after)),
                            cash_at_end=round(cash_end), reserve_cycles=sum(1 for x in self.per_cycle if "shock_reserve" in x[1]))
        if self.t_shock is None:
            sim.chk.violation("shock_not_simulated", "no shock spend")
        elif not after:
            sim.chk.violation("reserve_not_refilled", "free cash fell under the reserve but nothing was raised")
        elif cash_end < 15000.0 - 1.0:
            sim.chk.violation("reserve_still_short", f"free cash {cash_end:.0f} still under the 15k reserve at the end")


class _HookLock:
    """The trade lock, but whoever waits for it runs the moment it is released: models the main thread's churn cycle
    taking the lock right after the scanner's unwind released it (real thread order, which the sequential sim hides)."""
    def __init__(self):
        self.inner, self.pending, self.running = threading.Lock(), None, False

    def __enter__(self):
        self.inner.acquire()
        return self

    def __exit__(self, *a):
        self.inner.release()
        h, self.pending = self.pending, None
        if h and not self.running:
            self.running = True
            try:
                h()
            finally:
                self.running = False


class ChurnInterleave(ChurnFreeFirst):
    name, seed = "churn_interleave", 35
    doc = "as churn_free_first, but the main loop's churn cycle runs the moment the scanner's free unwind releases the trade lock (real thread order)"

    def bot_setup(self, sim):
        super().bot_setup(sim)
        b, lk = sim.bot, _HookLock()
        b.trade_lock = lk
        real_fu = b.arb.free_unwinds

        def churn_now():
            v = sim.v
            label, v.step = v.step, "cycle (interleaved churn)"
            try:
                b.churn_cycle(b.positions_snapshot, False, b.last_cycle_id)
            finally:
                v.step = label

        def fu(*a, **k):
            out = real_fu(*a, **k)
            if out and sim.chk.sub == "free_unwind_pass":
                lk.pending = churn_now
                self.hooked = getattr(self, "hooked", 0) + 1
            return out
        b.arb.free_unwinds = fu

    def final_checks(self, sim):
        super().final_checks(sim)
        if not getattr(self, "hooked", 0):
            sim.chk.violation("churn_interleave_idle", "the scanner never free-unwound: the interleaving was not exercised")


class ChurnBlowout(ChurnBase):
    name, cycles, seed, n_races = "churn_blowout", 200, 36, 14
    doc = "14 races, 80 h of noisy history, recycling on: all gaps +6 ticks at cycle 20 and +-2 ticks every cycle: entries stop on the volatility pause; 7 races revert at 120, the sell-all at cycle 150 leaves no set in any of them"

    def bot_setup(self, sim):
        self.races = churn_races(sim, self.n_races)
        self.rng = random.Random(99)
        preload_quotes(sim, self.races, hours=80.0, noise_p=0.3, rng=self.rng)
        self.common_setup(sim)
        self.sell_all = T0 + 150 * self.pass_dt
        self.saved = (DCm().SELL_ALL_AT, DCm().LAST_ENTRY, sim.R.CHURN_LAST_ENTRY)
        DCm().SELL_ALL_AT = self.sell_all
        DCm().LAST_ENTRY = self.sell_all - 1
        sim.R.CHURN_LAST_ENTRY = self.sell_all - 1

    def after_cycle(self, sim, i):
        super().after_cycle(sim, i)
        if CLOCK.t >= self.sell_all and getattr(self, "left_after", None) is None:
            # Only the sell-all cycle itself (the first at or past the deadline). The old check re-read every later cycle, so it
            # judged the end of the run: sim taker flow keeps hitting the MM's quotes after the sell-all, building 'sets' (NO on
            # every leg of a two-leg race) that are MM inventory, not churn's (29 Oct: IA-02, 75 sets at t+93 min, none at t+75).
            # And MM inventory present AT the sell-all cycle (31 Oct: IA-02, 19) is not churn's either: churn_sets() leaves it out.
            self.left_after = {r: churn_sets(sim, r) for r in self.races if churn_sets(sim, r)}

    def before_cycle(self, sim, i):
        if i < 20:
            return
        for k, r in enumerate(self.races):
            if i >= 120 and k < 7:
                set_race_bids_sum(sim, r, self.base[r])
            else:
                set_race_bids_sum(sim, r, self.base[r] + 0.03 + self.rng.choice([-0.01, 0.0, 0.01]))

    def final_checks(self, sim):
        DCm().SELL_ALL_AT, DCm().LAST_ENTRY, sim.R.CHURN_LAST_ENTRY = self.saved
        super().final_checks(sim)
        rep, ch = self.summary, sim.bot.churn
        if not rep["entries"]:
            sim.chk.violation("churn_none", "no entry")
        if self.pause_t is None:
            sim.chk.violation("churn_no_pause", "gap movement rose on every race but entries never paused")
        elif self.entries_after(sim, self.pause_t):
            sim.chk.violation("churn_entry_while_paused", f"{len(self.entries_after(sim, self.pause_t))} entries after the volatility pause")
        if rep["lots_left"]:
            sim.chk.violation("churn_sell_all_incomplete", f"after the sell-all: ledger {ch.lots}")
        left = getattr(self, "left_after", None)
        if left is None or left:
            sim.chk.violation("sets_left_at_sell_all", f"venue sets in churn races right after the sell-all cycle: {left}")
        if self.entries_after(sim, self.sell_all):
            sim.chk.violation("churn_entry_after_sell_all", "bought again after the sell-all")
        rep.update(pause_after_min=None if self.pause_t is None else round((self.pause_t - T0) / 60),
                   vol_last=None if ch.vol_last is None else round(ch.vol_last, 2))


class ChurnMany(ChurnBase):
    name, cycles, seed, n_races = "churn_many", 60, 37, 20
    doc = "20 races +6 ticks at once with 100k cash: the 12k pot binds"
    cash = None

    def bot_setup(self, sim):
        super().bot_setup(sim)
        if self.cash is not None:
            sim.v.cash = float(self.cash)
            sim.start_value = sim.v.value()

    def before_cycle(self, sim, i):
        if i == 20:
            for r in self.races:
                set_race_bids_sum(sim, r, self.base[r] + 0.03)

    def final_checks(self, sim):
        super().final_checks(sim)
        ent = self.calls(sim, ("arb",), main=True)
        if not ent:
            sim.chk.violation("churn_none", "no entry")
        lo = min((c["cash_after"] for c in ent), default=None)
        self.summary["min_cash_after_entry"] = None if lo is None else round(lo)
        if lo is not None and lo < sim.bot.churn.cash_floor - 1:
            sim.chk.violation("churn_below_floor", f"cash after an entry {lo:.0f} < {sim.bot.churn.cash_floor:.0f} floor")


class ChurnManyLowCash(ChurnMany):
    name, seed, cash = "churn_many_lowcash", 38, 14000.0
    doc = "20 races +6 ticks at once with 14k cash: the 3k cash floor binds (churn keeps its own floor, not the 20k arb reserve)"


class Recycle(Scenario):
    name, cycles, seed = "recycle", 80, 14
    doc = "held low-edge sets lock ~77k, cash ~23k near the 20k arb reserve; 4 races show standing 6% sell-sets"

    def venue_setup(self, sim):
        races = two_leg_mid_races(sim, 0.2, 0.8)
        self.held, self.targets = races[:6], races[6:10]
        for r in self.held:
            set_race_bids_sum(sim, r, 1.015)
        for r in self.targets:
            set_race_bids_sum(sim, r, 1.06)

    def bot_setup(self, sim):
        per = (sim.v.cash - 23000.0) / len(self.held)
        for r in self.held:
            cost = sum(1 - SV.P(sim.v.bid_t[ex]) for ex in sim.legs(r))
            sim.seed_set(r, int(per / cost))


class RecycleTail(Recycle):
    name, cycles, seed = "recycle_tail", 30, 23
    doc = "as recycle, but the held sets are small (300 each) so one unwind closes a whole set; cash pinned at 21k"

    def bot_setup(self, sim):
        for r in self.held:
            sim.seed_set(r, 300)
        sim.v.cash = 21000.0          # the rest of the capital is tied up elsewhere


class RecycleStaleFill(RecycleTail):
    name, cycles, seed = "recycle_stale_fill", 30, 25
    doc = ("Fable review 3: as recycle_tail (300-set holdings), but right before the first recycle a taker hits our MM bid for "
           "100 YES on one leg of every held set, after the positions snapshot the recycle is sized from: the unwind is "
           "re-sized to the 200 sets held then, no leg crosses zero")

    def bot_setup(self, sim):
        super().bot_setup(sim)
        arb = sim.bot.arb
        real = arb.recycle
        self.hit = 0

        def wrapped(best, held_books, positions, cash, cycle_id=None, **kw):
            if not self.hit:
                self.hit = 1                      # lands between the snapshot and the unwind (the sim applies flow only between steps)
                for r in self.held:
                    sim.seed_holding(sim.legs(r)[0], "yes", 100, "mm")
            return real(best, held_books, positions, cash, cycle_id, **kw)
        arb.recycle = wrapped

    def final_checks(self, sim):
        unw = [c for c in sim.v.calls if c["kind"] == "multileg" and c["prefix"] == "unw" and c.get("ok")]
        self.summary = {"hit": self.hit, "unwinds": len(unw), "first_qty": unw[0]["legs"][0]["quantity"] if unw else None}
        if not self.hit:
            sim.chk.violation("stale_fill_not_exercised", "no recycle was attempted")
        if not unw:
            sim.chk.violation("recycle_none", "no recycle unwind after the fill (the scenario must still recycle)")


class RecycleRace(RecycleTail):
    name, cycles, seed = "recycle_race", 30, 24
    interleave = True
    doc = "small held sets in 16 races, threads interleaved: the main loop's backstop recycle runs while the scanner is inside recycle_pass"

    def venue_setup(self, sim):
        races = two_leg_mid_races(sim, 0.2, 0.8)
        self.held, self.targets = races[:16], races[16:18]
        for r in self.held:
            set_race_bids_sum(sim, r, 1.015)
        for r in self.targets:
            set_race_bids_sum(sim, r, 1.06)


class OwnBid(Scenario):
    name, cycles, seed = "own_bid", 40, 15
    doc = "our own resting bid is the top of one leg and makes the race look like a sell-set; arb must not trade against it"

    def venue_setup(self, sim):
        v = sim.v
        r = two_leg_mid_races(sim, 0.3, 0.7)[-1]
        a, b = sim.legs(r)
        v.set_touch(a, 118, 124)          # 0.590 / 0.620
        v.set_touch(b, 81, 82)            # 0.405 / 0.410  -> bids sum 0.995, no arb without us
        self.race, self.leg = r, a

    def after_cycle(self, sim, i):
        # after the cycle has read its open orders (as tilt/MM orders placed during a cycle are), rest our bid at 0.605
        g = sim.chk.own_guard
        o = sim.v.orders.get(g[2]) if g else None
        if o is None or not o.open:
            resp = sim.v.place(self.leg, "yes", "buy", 300, 0.605, SV.iso(CLOCK.t + 3600), sim.v.new_key("bat"))
            sim.bot.store.record_order(resp["orderId"], self.leg, None, "yes", "buy", 0.605, 300, "tilt", status="open")
            sim.chk.own_guard = (self.race, self.leg, resp["orderId"])


class ExitsWrongSide(Scenario):
    name, cycles, seed = "exits", 80, 16
    doc = "directional/tilt holdings whose edge is gone and MM inventory on the wrong side of the outside consensus; one thin book"

    def venue_setup(self, sim):
        self.races = two_leg_mid_races(sim, 0.3, 0.7)[:6]

    def bot_setup(self, sim):
        v = sim.v
        u = sim.universe
        dlegs = [u.races[r].legs["D"].exchange_id for r in self.races[:3]]
        mlegs = [u.races[r].legs["R"].exchange_id for r in self.races[3:5]]
        for ex in dlegs:                       # directional YES 500: edge gone (outside = mid)
            sim.seed_holding(ex, "yes", 500, "direction")
            v.outside[ex] = round(v.mid(ex), 4)
            other = [e for e in sim.legs(sim.race_of(ex)) if e != ex][0]
            v.outside[other] = round(1 - v.outside[ex], 4)
        for ex in mlegs:                       # MM inventory YES 800, outside consensus 3 points lower
            sim.seed_holding(ex, "yes", 800, "mm")
            v.outside[ex] = round(v.mid(ex) - 0.03, 4)
            other = [e for e in sim.legs(sim.race_of(ex)) if e != ex][0]
            v.outside[other] = round(1 - v.outside[ex], 4)
        fav = [ex for ex in v.exs if 0.885 <= v.mid(ex) <= 0.95][:1]
        for ex in fav:                         # tilt favourite YES 600, outside agrees with the venue
            sim.seed_holding(ex, "yes", 600, "tilt")
            v.outside[ex] = round(v.mid(ex), 4)
        thin = dlegs[0]                        # thin bid: the exit fills partly and rests (must not be re-sent on top)
        sim.v.dmm[thin]["bid"] = {}
        self.thin = thin

    def on_tick(self, sim):
        ex = self.thin
        d = sim.v.dmm[ex]["bid"]
        d.clear()
        d[sim.v.bid_t[ex]] = 120
        d[sim.v.bid_t[ex] - 2] = 2000
        sim.v._touch(ex)


class Offload(Scenario):
    name, cycles, seed = "offload", 60, 22
    doc = "a directional holding with little edge left while four new directional signals arrive at once (two are skipped for the write budget)"

    def venue_setup(self, sim):
        self.races = two_leg_mid_races(sim, 0.35, 0.65)[:5]

    def bot_setup(self, sim):
        v, u = sim.v, sim.universe
        a = u.races[self.races[0]].legs["D"].exchange_id
        sim.seed_holding(a, "yes", 600, "direction")
        v.outside[a] = round(SV.P(v.bid_t[a]) + 0.012, 4)      # 1.2 points of edge left: outside the exit band, cheap to give up
        other = [e for e in sim.legs(self.races[0]) if e != a][0]
        v.outside[other] = round(1 - v.outside[a], 4)
        self.held = a

    def before_cycle(self, sim, i):
        v, u = sim.v, sim.universe
        if i == 3:
            for r in self.races[1:]:
                d, rr = u.races[r].legs["D"].exchange_id, u.races[r].legs["R"].exchange_id
                v.outside[d] = round(min(0.95, v.mid(d) + 0.10), 4)
                v.outside[rr] = round(1 - v.outside[d], 4)

    def final_checks(self, sim):
        if not any(c["kind"] == "order" and c["prefix"] == "offload" for c in sim.v.calls):
            sim.chk.notes.append("offload exit path was not reached")


class SlowWrites(OutsideMove):
    name, cycles, seed = "slow_writes", 40, 17
    doc = "every write takes 12 s (throttled write path) while outside moves make the directional/lag sleeves trade"

    def bot_setup(self, sim):
        sim.v.write_latency = 12.0

    def before_cycle(self, sim, i):
        v = sim.v
        if i == 2:
            races = two_leg_mid_races(sim)[:6]
            self.moved = []
            for r in races:
                d, rr = sim.universe.races[r].legs["D"].exchange_id, sim.universe.races[r].legs["R"].exchange_id
                v.outside[d] = round(min(0.97, v.outside[d] + 0.08), 4)
                v.outside[rr] = round(max(0.03, v.outside[rr] - 0.08), 4)
                self.moved += [d, rr]


class FeedSilent(Scenario):
    name, cycles, seed = "feed_silent", 90, 18
    doc = "the feed goes silent for 10 min (books frozen, then empty after the watchdog restarts it) while 4 quoted touches move 3 ticks"

    def before_cycle(self, sim, i):
        v = sim.v
        if i == 20:
            v.begin_silence(600)
        if i == 22:
            for ex in [ex for ex in sim.bot.mm.quoted if 0.2 <= v.mid(ex) <= 0.8][:4]:
                v.shift(ex, -3)


class MMOutageLastLook(Scenario):
    name, cycles, seed = "mm_outage_lastlook", 90, 32
    doc = "MM last look through a 10-min feed outage (sim 'day' 24 Oct): the feed goes silent while quoted touches keep moving; " \
          "no MM quote may be sent for a market whose last look found no touch (it used to be sent unchecked)"

    def bot_setup(self, sim):
        b = sim.bot
        self.last = {}
        self.unchecked = []
        orig = b.mm.touch_fn

        def look(ex):
            r = orig(ex)
            self.last[str(ex)] = r
            return r
        b.mm.touch_fn = look
        api = b.mm.api
        orig_pb = api.place_batch

        self.sent_in_outage = 0

        def pb(orders, *a, **k):
            for o in orders:
                ex = str(o["exchangeId"])
                if ex in self.last and self.last[ex] is None:
                    self.unchecked.append((sim.v.now, ex, o["side"], o["price"]))
            if sim.v.silent():
                self.sent_in_outage += len(orders)
            return orig_pb(orders, *a, **k)
        api.place_batch = pb

    def before_cycle(self, sim, i):
        v = sim.v
        if i == 20:
            v.begin_silence(600)
        if i in (22, 30, 38):
            for ex in [ex for ex in sim.bot.mm.quoted if 0.2 <= v.mid(ex) <= 0.8][:4]:
                v.shift(ex, -1 if i != 30 else 1)

    def final_checks(self, sim):
        for t, ex, side, px in self.unchecked:
            sim.chk.violation("mm_quote_unchecked", f"{ex} {side} @{px} sent with no known touch (feed outage, t+{(t - T0) / 60:.1f} min)")
        if self.sent_in_outage == 0:
            sim.chk.violation("mm_dark_in_outage", "the MM sent no quote at all during the 10-min feed outage (checked quotes are expected)")
        self.summary = {"unchecked": len(self.unchecked), "sent_in_outage": self.sent_in_outage}


class AonReject(Scenario):
    name, cycles, seed = "aon_reject", 60, 19
    doc = "standing 4% sell-sets in four races; baskets on two of them are always rejected (someone else takes the size first)"

    def venue_setup(self, sim):
        races = two_leg_mid_races(sim, 0.2, 0.8)[:4]
        for r in races:
            set_race_bids_sum(sim, r, 1.04)
        bait = set(races[:2])
        self.bait = bait
        sim.v.reject_aon = lambda legs, prefix: prefix == "arb" and sim.race_of(str(legs[0]["exchangeId"])) in bait


class InsideOn(Scenario):
    name, cycles, seed = "inside_on", 40, 20
    doc = "calm market with the inside quoter switched on and 3-5 tick DMM spreads on 20 mid-band markets"
    cfg = {"inside_enabled": True}

    def venue_setup(self, sim):
        v = sim.v
        n = 0
        for ex in v.exs:
            if 0.2 <= v.mid(ex) <= 0.8 and n < 20:
                k = 3 + n % 3
                v.set_touch(ex, v.bid_t[ex] - (k - 1) // 2, v.bid_t[ex] - (k - 1) // 2 + k)
                n += 1


class RecycleOversize(Scenario):
    name, cycles, seed = "recycle_oversize", 40, 25
    cfg = {"arb.max_race_frac": 0.02, "arb.high_ret_min": 0.99}
    doc = "23 Oct 06:14: one 30,000-set held at low edge, cash 5k (below the 20k reserve), a deep 6% target the per-race cap limits to 2,000: an unwind may free only what the follow-through buys"

    def venue_setup(self, sim):
        races = two_leg_mid_races(sim, 0.3, 0.7)
        self.held, self.target = races[0], races[1]
        set_race_bids_sum(sim, self.held, 1.012)
        set_race_bids_sum(sim, self.target, 1.06)

    def bot_setup(self, sim):
        sim.seed_set(self.held, 30000)
        sim.v.cash = 5000.0

    def final_checks(self, sim):
        calls = [c for c in sim.v.calls if c["kind"] == "multileg" and c.get("ok")]
        unwinds = [c for c in calls if c["prefix"] == "unw"]
        if not unwinds:
            sim.chk.violation("recycle_none", "the scenario never recycled: the held set was not unwound for the 6% target")
        for u in unwinds:
            freed = u["cash_after"] - u["cash_before"]
            buys = [c for c in calls if c["prefix"] == "arb" and u["t"] <= c["t"] <= u["t"] + 90]
            deployed = sum(c["cash_before"] - c["cash_after"] for c in buys)
            if deployed < 0.9 * freed - 1.0:
                sim.chk.violation("recycle_idle", f"{u['key']} freed {freed:.0f} but the follow-through bought only {deployed:.0f} within 90 s "
                                                  f"({len(buys)} basket(s)); the rest sits idle and its locked profit is gone")


class RecycleCash(Scenario):
    name, cycles, seed = "recycle_cash", 60, 26
    cfg = {"arb.new_sets": False, "arb.recycle_enabled": True}
    doc = "cash mode (live config since 23 Oct): held low-edge sets lock ~40k, cash 60k ABOVE the 20k reserve, 6% sets on offer: free cash must never buy a set, recycles still swap into the better set, spending only what each unwind freed"

    def venue_setup(self, sim):
        races = two_leg_mid_races(sim, 0.2, 0.8)
        self.held, self.targets = races[:4], races[4:7]
        for r in self.held:
            set_race_bids_sum(sim, r, 1.012)
        for r in self.targets:
            set_race_bids_sum(sim, r, 1.06)

    def bot_setup(self, sim):
        per = 40000.0 / len(self.held)
        for r in self.held:
            cost = sum(1 - SV.P(sim.v.bid_t[ex]) for ex in sim.legs(r))
            sim.seed_set(r, int(per / cost))
        sim.v.cash = 60000.0

    def final_checks(self, sim):
        calls = sorted((c for c in sim.v.calls if c["kind"] == "multileg" and c.get("ok")), key=lambda c: c["t"])
        unwinds = [c for c in calls if c["prefix"] == "unw"]
        if not unwinds:
            sim.chk.violation("recycle_none", "cash mode never recycled although 6% sets were on offer and held sets earn ~1.2%")
        freed = spent = 0.0
        for c in calls:
            if c["prefix"] in ("unw", "fre"):
                freed += c["cash_after"] - c["cash_before"]
            elif c["prefix"] == "arb":
                spent += c["cash_before"] - c["cash_after"]
                if spent > freed + 1.0:
                    sim.chk.violation("cash_mode_locked_more", f"{c['key']}: arb baskets have spent {spent:.0f} but unwinds freed only "
                                                               f"{freed:.0f}; in cash mode free cash must never buy a set")
                    break
        biggest = max((c["cash_after"] - c["cash_before"] for c in unwinds), default=0.0)
        if freed - spent > 1.05 * biggest + 1.0:
            sim.chk.violation("recycle_idle_pile", f"unwinds freed {freed:.0f} but follow-throughs spent {spent:.0f}: more than one "
                                                   f"recycle's worth ({biggest:.0f}) of locked profit was given up for idle cash")


class RecycleCashGone(RecycleCash):
    name, cycles, seed = "recycle_cash_gone", 60, 27
    doc = "cash mode, review 23 Oct: the 6% target keeps showing but every basket on it is rejected (taken first): at most one held set may be unwound for it, never one per pass"

    def venue_setup(self, sim):
        super().venue_setup(sim)
        tg = set(self.targets)
        sim.v.reject_aon = lambda legs, prefix: prefix == "arb" and sim.race_of(str(legs[0]["exchangeId"])) in tg

    def final_checks(self, sim):
        unw = [c for c in sim.v.calls if c["kind"] == "multileg" and c.get("ok") and c["prefix"] == "unw"]
        if len(unw) > 1:
            sim.chk.violation("recycle_unwind_loop", f"{len(unw)} held sets unwound for targets whose baskets are always rejected "
                                                     f"(at most 1 allowed: the freed cash must be re-spent before another unwind)")


class Day(Scenario):
    name, cycles, pass_dt, seed = "day", 2400, 15.0, 21
    real_books = True
    doc = "24 h on the real 21-Oct books (standing overrounds): drifting outside fairs, a jump every ~2 h, sweeps, one feed outage"

    def before_cycle(self, sim, i):
        v, rng = sim.v, sim.rng
        if CLOCK.t - getattr(self, "t_end", T0) >= 86400 - 1:
            return
        if i % 20 == 0:                     # outside drift
            for r in rng.sample(sorted(sim.universe.races), 10):
                exs = sim.legs(r)
                if len(exs) != 2:
                    continue
                d = rng.gauss(0, 0.01)
                v.outside[exs[0]] = round(min(0.98, max(0.02, v.outside[exs[0]] + d)), 4)
                v.outside[exs[1]] = round(1 - v.outside[exs[0]], 4)
        if i % 240 == 120:                  # a jump the venue follows with a lag
            r = rng.choice(two_leg_mid_races(sim, 0.15, 0.85) or sorted(sim.universe.races))
            exs = sim.legs(r)
            d = rng.choice([-1, 1]) * 0.06
            v.outside[exs[0]] = round(min(0.97, max(0.03, v.outside[exs[0]] + d)), 4)
            v.outside[exs[1]] = round(1 - v.outside[exs[0]], 4)
            self.follow = getattr(self, "follow", []) + exs
        if i % 300 == 200 and sim.bot.mm.quoted:
            ex = rng.choice(sim.bot.mm.quoted)
            v.sweep(ex, rng.choice([-5, 5]))
        if i == 1500:
            v.begin_silence(400)
        if i % 4 == 0:
            for ex in getattr(self, "follow", []):
                gap = v.outside[ex] - v.mid(ex)
                if abs(gap) > 2 * SV.TICK:
                    v.shift(ex, 1 if gap > 0 else -1)

    def bot_setup(self, sim):
        self.t_end = T0
        sim.chk.strict_unwinds = sim.bot.churn is not None

    def final_checks(self, sim):
        ch = sim.bot.churn
        if ch is None:
            return
        # with high-frequency arb on in cash mode, the only arb baskets are its own entries (Fable 24 Oct: the recycler
        # bought Rhode Island Governor 43 times with cash from unwound churn sets)
        out = sim.chk.cash_mode_baskets
        if out:
            c = out[0]
            sim.chk.violation("cash_mode_basket_outside_churn", f"{len(out)} arb baskets outside high-frequency arb, first {c['key']} "
                                                                f"{sim.race_of(str(c['legs'][0]['exchangeId']))} ({c.get('step')})")
        self.summary = {"churn_entries": sim.chk.counts.get("churn_entries", 0), "realised": round(ch.realised, 1), "lots": len(ch.lots),
                        "killed": ch.killed, "vol_paused": ch.vol_paused}


# ---------------------------------------------------------------- wind-down (backlog 000), 30 Oct - 3 Nov on a fake clock
class WindDown(Scenario):
    name, cycles, seed = "wind_down", 175, 41
    cfg = {"wind_down_enabled": True}
    doc = ("wind-down on the live config: the clock jumps through 30 Oct 23:00, 31 Oct 22:00, 2 Nov 01:00, 3 Nov 11:30 and 15:00-18:40 ET "
           "(3 extra min a cycle); late-call NO sets (Oregon Governor at 2 ticks, Rhode Island Governor 3-leg at 2.8%) and a YES set "
           "(California Governor) are sold by the wind-down itself (not free unwinds) in clips <= 1,000, >= 30 min apart per race, nothing before 31 Oct 00:00 ET, all by 18:00 ET 3 Nov; an early-call set is "
           "held; a directional leg is sold; a punt leg is never touched; nothing is sold from 18:00 ET; no MM quote is sent once the MM is "
           "flat (its stop_quoting_at, 07:00 ET 3 Nov live)")
    WINDOWS = [(0, (2026, 10, 30, 23, 0)), (12, (2026, 10, 31, 22, 0)), (45, (2026, 11, 2, 1, 0)), (80, (2026, 11, 3, 11, 30)),
               (105, (2026, 11, 3, 15, 0))]

    def venue_setup(self, sim):
        WD = sys.modules["sigbot.proposals.wind_down"]
        CLOCK.t = WD.et_ts(*self.WINDOWS[0][1])

    def bot_setup(self, sim):
        WD = sys.modules["sigbot.proposals.wind_down"]
        v, u = sim.v, sim.universe
        self.late_sell = ["Oregon Governor", "Rhode Island Governor"]
        self.late_buy = ["California Governor"]
        early = []
        for name, r in sorted(u.races.items()):
            if len(r.legs) != 2 or name in self.late_sell + self.late_buy:
                continue
            fav = max(v.mid(e) for e in r.exchange_ids)
            if WD.call_class(name, fav)[0] == "early" and fav <= 0.93:
                early.append(name)
        self.early = early[0]
        mids = [x for x in two_leg_mid_races(sim, 0.35, 0.65, exclude=tuple(self.late_sell + self.late_buy + [self.early]))]
        self.dir_race, self.punt_race = mids[0], mids[1]
        self.dir_ex = sim.legs(self.dir_race)[0]
        self.punt_ex = sim.legs(self.punt_race)[0]
        self.sizes = {"Oregon Governor": 2500, "Rhode Island Governor": 2200, "California Governor": 1800, self.early: 3000}
        # real exit costs, not free unwinds: Oregon's asks sum to 1.01 (2 ticks), Rhode Island's 3 legs to 1.055 (2.8% of proceeds)
        set_race_bids_sum(sim, "Oregon Governor", 1.0)
        set_race_bids_sum(sim, "Rhode Island Governor", 1.04)
        for race in self.late_sell + [self.early]:
            sim.seed_set(race, self.sizes[race])
        for race in self.late_buy:
            legs = []
            for ex in sim.legs(race):
                px = SV.P(v.ask_t[ex])
                sim.seed_holding(ex, "yes", self.sizes[race], "arb", px)
                legs.append({"exchangeId": ex, "side": "yes", "action": "buy", "quantity": self.sizes[race], "price": px})
            sim.bot.store.record_arb(race, "buy_set", legs, self.sizes[race], 1 - sum(l["price"] for l in legs), sum(l["price"] for l in legs), True, {"seed": True})
        sim.seed_holding(self.dir_ex, "yes", 600, "direction")
        sim.seed_holding(self.punt_ex, "yes", 800, "punt")          # a punt leg: never sold, its orders never cancelled
        self.preload(sim)
        self.mm_after_flat = []

    def preload(self, sim):
        """25 h of 'sig' heartbeats with each race's exit cost 2 ticks above today's: today's touch is cheap vs its medians."""
        v, rows = sim.v, []
        for race in self.late_sell + self.late_buy + [self.early]:
            legs = list(sim.universe.races[race].legs.items())
            for k in range(150):
                t = v.now - 25 * 3600 + 600 * k
                for i, (party, l) in enumerate(legs):
                    bt, at = v.bid_t[l.exchange_id], v.ask_t[l.exchange_id]
                    if i == 0:
                        bt, at = bt - 2, at + 2
                    b, a = SV.P(bt), SV.P(at)
                    rows.append((t, "sig", race, party, l.exchange_id, b, a, (b + a) / 2, None, None, None, None))
        sim.bot.store.conn.executemany(VQ_INS, rows)
        sim.bot.store.conn.commit()
        sim.bot.wd_med_ts = 0.0

    def before_cycle(self, sim, i):
        WD = sys.modules["sigbot.proposals.wind_down"]
        for at, ymdhm in self.WINDOWS[1:]:
            if i == at:
                CLOCK.t = max(CLOCK.t, WD.et_ts(*ymdhm))
                self.preload(sim)
                # a jump of hours or days skips every outside-fair poll in between: the bot's last fair is then a day old,
                # which never happens while it runs (it polls every cycle), and the MM would quote one cycle with the xv
                # gate open (stale fairs never gate, by design) on whichever markets it quotes. Model the jump like the
                # restart it stands for: the MM waits for a fresh outside fair first (mm_gate_ready's restart path).
                # (24 Oct: failed on 3f51a2b when the volume cache made the MM quote a market 11 ticks off its fair.)
                sim.bot.mm_need_xv = True

    def after_cycle(self, sim, i):
        CLOCK.advance(180.0)
        WD = sys.modules["sigbot.proposals.wind_down"]
        end = sim.bot.wd_mm_end
        if CLOCK.t > end + 120:
            self.mm_after_flat.extend(c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] == "bat" and c["t"] > end + 120
                                      and c not in self.mm_after_flat)

    def final_checks(self, sim):
        WD = sys.modules["sigbot.proposals.wind_down"]
        b, v = sim.bot, sim.v
        sold = list(b.wind_down.sold)
        pos = v.signed_positions()
        if not sold:
            sim.chk.violation("wind_down_none", "the wind-down sold nothing")
        early = [s for s in sold if s[0] < WD.START]
        if early:
            sim.chk.violation("wind_down_before_start", f"{len(early)} sales before 31 Oct 00:00 ET: {early[:3]}")
        late = [s for s in sold if s[0] >= WD.DEADLINE]
        if late:
            sim.chk.violation("wind_down_after_deadline", f"{len(late)} sales at or after 18:00 ET 3 Nov: {late[:3]}")
        wd_calls = [c for c in v.calls if c.get("prefix") in ("unw", "unb", "wdx") and c["t"] >= WD.DEADLINE]
        if wd_calls:
            sim.chk.violation("wind_down_after_deadline", f"{len(wd_calls)} unwind/exit orders at or after 18:00 ET 3 Nov, first {wd_calls[0].get('key')}")
        if pos.get(self.punt_ex, 0.0) != 800 or any(self.punt_ex in s[1] for s in sold):
            sim.chk.violation("wind_down_sold_punt", f"punt leg {self.punt_ex}: {pos.get(self.punt_ex)} held (seeded 800)")
        by_race = collections.defaultdict(list)
        for t, what, q, px, cost in sold:
            if q > 1000:
                sim.chk.violation("wind_down_clip_size", f"{what}: {q} in one clip (> 1,000)")
            if what.startswith(("sell_set", "buy_set")):
                by_race[what.split(" ", 1)[1]].append(t)
        for race, ts in by_race.items():
            ts.sort()
            if any(b_ - a_ < 1800 - 1 for a_, b_ in zip(ts, ts[1:])):
                sim.chk.violation("wind_down_spacing", f"{race}: clips less than 30 min apart: {[WD.et_str(x) for x in ts]}")
        for race in self.late_sell + self.late_buy:
            if race not in by_race:
                sim.chk.violation("wind_down_not_its_sale", f"{race}: the wind-down never sold a clip (another path took the sets?)")
        for race in self.late_sell:
            n = venue_sets(sim, race)
            if n:
                sim.chk.violation("wind_down_left", f"{race}: {n} late-call NO sets still held after 18:00 ET 3 Nov")
        for race in self.late_buy:
            qs = [pos.get(e, 0.0) for e in sim.legs(race)]
            if all(q > 0 for q in qs):
                sim.chk.violation("wind_down_left", f"{race}: {int(min(qs))} late-call YES sets still held after 18:00 ET 3 Nov")
        if venue_sets(sim, self.early) < self.sizes[self.early] and self.early not in b.wind_down_exempt()[0]:
            sim.chk.violation("wind_down_sold_early_call", f"{self.early}: an early-call set was sold ({venue_sets(sim, self.early)} left)")
        if any(self.early in s[1] for s in sold):
            sim.chk.violation("wind_down_sold_early_call", f"{self.early}: the wind-down sold an early-call set")
        if pos.get(self.dir_ex, 0.0) > 0:
            sim.chk.violation("wind_down_left", f"directional leg {self.dir_ex}: {pos.get(self.dir_ex)} YES still held")
        if self.mm_after_flat:
            sim.chk.violation("wind_down_mm_after_flat", f"{len(self.mm_after_flat)} MM batch orders after 12:00 ET 3 Nov, first {self.mm_after_flat[0]['key']}")
        self.summary = {"sales": len(sold), "first": WD.et_str(sold[0][0]) if sold else None, "last": WD.et_str(sold[-1][0]) if sold else None,
                        "cost": round(sum(s[4] for s in sold), 1), "early_race": self.early, "dir_leg": self.dir_ex,
                        "by_race": {r: len(t) for r, t in by_race.items()}}



class Shock(Scenario):
    name, cycles, seed = "shock", 420, 41
    post_only_dmm = False
    cfg = {"shock_enabled": True}
    doc = ("shock mode: 45+ min of flat history; then (a) Polymarket jumps +8 pts on one race while Kalshi moves +2 "
           "(venues disagree: no entry); (b) both venues jump +14 pts on a second race, SIG's DMM lags 2 min then walks "
           "1 tick / 30 s: enter both legs (walk <= 4 ticks, <= 20k notional), exit within ~60 min; (c) later both venues "
           "jump +8 pts on a third race and revert 8 min later, SIG never moves: enter, then the hoax guard closes it; flat "
           "at the end, partisan cap exempt for shock only")

    def before_cycle(self, sim, i):
        v = sim.v
        if i == 3:
            self.races = [r for r in two_leg_mid_races(sim, 0.3, 0.7) if not r.startswith("Alaska")][:3]
            for r in self.races:
                for ex in sim.legs(r):
                    v.outside[ex] = round(v.mid(ex), 4)
            self.t0 = v.now
            self.events = {}
        if not getattr(self, "races", None) or len(self.races) < 3:
            return
        t = v.now - self.t0
        rD = lambda r: sim.universe.races[r].legs["D"].exchange_id
        rR = lambda r: sim.universe.races[r].legs["R"].exchange_id
        if "split" not in self.events and t >= 50 * 60:                 # (a) Polymarket only
            r = self.races[0]
            for ex, sgn in ((rD(r), 1), (rR(r), -1)):
                v.outside[ex] = round(v.outside[ex] + sgn * 0.08, 4)
                v.kalshi_offset[ex] = -sgn * 0.06
            self.events["split"] = v.now
        if "shock" not in self.events and t >= 60 * 60:                 # (b) a real shock
            r = self.races[1]
            v.outside[rD(r)] = round(min(0.97, v.outside[rD(r)] + 0.14), 4)
            v.outside[rR(r)] = round(max(0.03, v.outside[rR(r)] - 0.14), 4)
            self.events["shock"] = v.now
        if "hoax" not in self.events and t >= 140 * 60:                 # (c) a hoax
            r = self.races[2]
            v.outside[rD(r)] = round(v.outside[rD(r)] + 0.08, 4)
            v.outside[rR(r)] = round(v.outside[rR(r)] - 0.08, 4)
            self.events["hoax"] = v.now
        if "hoax" in self.events and "revert" not in self.events and v.now - self.events["hoax"] >= 8 * 60:
            r = self.races[2]
            v.outside[rD(r)] = round(v.outside[rD(r)] - 0.08, 4)
            v.outside[rR(r)] = round(v.outside[rR(r)] + 0.08, 4)
            self.events["revert"] = v.now

    def on_tick(self, sim):
        v = sim.v
        if getattr(self, "events", None) and "shock" in self.events and v.now - self.events["shock"] >= 120:
            r = self.races[1]
            last = getattr(self, "last_walk", 0.0)
            if v.now - last >= 30:                                      # the DMM walks 1 tick / 30 s toward the outside
                self.last_walk = v.now
                for ex in sim.legs(r):
                    gap = v.outside[ex] - v.mid(ex)
                    if abs(gap) > SV.TICK:
                        step = 1 if gap > 0 else -1
                        if self.post_only_dmm and any((o.book == "ask" and step > 0 and o.tick <= v.bid_t[ex] + 1) or
                                                      (o.book == "bid" and step < 0 and o.tick >= v.ask_t[ex] - 1) for o in v.ours[ex]):
                            continue                                   # a post-only DMM stops one tick short of our order
                        v.shift(ex, step)

    def final_checks(self, sim):
        b = sim.bot
        calls = [c for c in sim.v.calls if c["kind"] == "order" and c["prefix"] in ("shk", "shx", "shm")]
        if not getattr(self, "races", None) or len(self.races) < 3:
            sim.chk.violation("shock_setup", "fewer than 3 two-leg mid races")
            return
        legs = {r: set(sim.legs(r)) for r in self.races}
        ent = [c for c in calls if c["prefix"] == "shk"]
        by_race = {r: [c for c in ent if c["ex"] in legs[r]] for r in self.races}
        if by_race[self.races[0]]:
            sim.chk.violation("shock_on_split", f"entered {self.races[0]} although Kalshi did not confirm Polymarket's jump")
        if {c["ex"] for c in by_race[self.races[1]]} != legs[self.races[1]]:
            sim.chk.violation("shock_missed", f"real shock on {self.races[1]}: entries on {sorted({c['ex'] for c in by_race[self.races[1]]})}, want both legs")
        if not by_race[self.races[2]]:
            sim.chk.violation("shock_hoax_none", f"no entry on the hoax race {self.races[2]} (expected an entry, then the hoax exit)")
        for r in self.races:
            notional = sum(c["qty"] * c["price"] for c in by_race[r])
            if notional > b.cfg.shock_per_shock * 1.01:
                sim.chk.violation("shock_oversize", f"{r}: {notional:.0f} entered > per-shock cap {b.cfg.shock_per_shock:.0f}")
        fills = [dict(x) for x in b.store.rows("SELECT exchange_id, quantity, price, filled_at FROM fills WHERE purpose IN ('shock','shock_exit') ORDER BY filled_at")]
        pos = {}
        for f in fills:
            pos[f["exchange_id"]] = pos.get(f["exchange_id"], 0.0) + float(f["quantity"])
        net = b.shock.netted if b.shock else {}
        still = {e: q for e, q in pos.items() if abs(q - net.get(e, 0.0)) >= 1}
        vpos = sim.v.signed_positions()
        if any(abs(net.get(e, 0.0)) >= 1 and vpos.get(e, 0.0) * net[e] > 0 for e in net):
            sim.chk.violation("shock_netted_wrong", f"netted {net} but the venue still holds that side: {vpos}")
        if still:
            sim.chk.violation("shock_not_flat", f"shock positions still open at the end: {still}")
        cash = -sum(float(f["quantity"]) * float(f["price"]) for f in fills)
        real = {r: -sum(float(f["quantity"]) * float(f["price"]) for f in fills if f["exchange_id"] in legs[r]) for r in self.races}
        if real[self.races[1]] <= 0:
            sim.chk.violation("shock_real_lost", f"the real shock lost {real[self.races[1]]:+.0f} with the DMM lagging")
        self.summary = {"entries": len(ent), "exits": len(calls) - len(ent), "pnl_by_race": {r: round(x) for r, x in real.items()},
                        "pnl": round(cash), "killed": b.shock.killed if b.shock else None,
                        "events_utc": {k: datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M") for k, t in self.events.items()}}


class ShockMaker(Shock):
    name = "shock_maker"
    cfg = {"shock_enabled": True, "shock_exit_maker": True}
    doc = "the shock scenario with the R3 exit: rest the exit at the 70%-convergence price (the DMM's walk fills it), taker only on a stop"


class ShockMakerPostOnly(ShockMaker):
    name = "shock_maker_postonly"
    post_only_dmm = True
    doc = "R3 worst case: the DMM quotes post-only, so its walk stops one tick short of our resting exit and never fills it"

# ---------------------------------------------------------------- 3 Nov punt executor (BACKLOG 0000, research_notes/punt_plan.md)
class Punt(Scenario):
    name, cycles, seed = "punt", 110, 53
    doc = ("punt executor ON with the live config otherwise: a hash-matched plan buys stage 1 (17:30-17:45 ET) in capped chunks, "
           "the clock jumps to 19:50 ET with the outside at 0.97 and SIG repriced so stage 1 is sold at >= 0.85 after 3 polls, then to "
           "22:05 ET for stage 2; no punt order above its cap or outside its window, stage-1 cost <= budget, the sale never flips "
           "the position, and no other sleeve sends an order on any punt race")

    def venue_setup(self, sim):
        WD = sys.modules["sigbot.proposals.wind_down"]
        CLOCK.t = WD.et_ts(2026, 11, 3, 17, 25)
        mids = two_leg_mid_races(sim, 0.25, 0.75)
        self.r1, self.r2 = mids[0], mids[1]
        self.ex1 = min(sim.legs(self.r1), key=lambda e: sim.v.mid(e))      # the cheaper leg (~0.25-0.45)
        self.ex2 = min(sim.legs(self.r2), key=lambda e: sim.v.mid(e))
        self.cap1 = round(SV.P(sim.v.ask_t[self.ex1]) + 0.02, 3)
        self.budget = 6000.0
        plan = {"plan_id": "SIM", "budget": self.budget, "cap_exempt": True, "steps": [
            {"id": "s1", "kind": "buy", "exchange_id": self.ex1, "side": "yes", "max_price": self.cap1, "max_cash": self.budget,
             "chunk_cash": 1500, "start": "2026-11-03T17:30:00-05:00", "stop": "2026-11-03T17:45:00-05:00", "min_gap_sec": 20},
            {"id": "s1_sell", "kind": "sell", "of_step": "s1", "min_price": 0.85, "chunk_shares": 5000, "outside_min": 0.95,
             "outside_polls": 3, "single_venue_min": 0.97, "start": "2026-11-03T19:45:00-05:00", "stop": "2026-11-03T21:30:00-05:00",
             "min_gap_sec": 20},
            {"id": "s2", "kind": "target_buy", "after_step": "s1_sell", "min_sold_frac": 0.7, "target_min": 1.0, "target_k_leader": 0.0,
             "target_fallback": 1.0, "max_cash": 3000, "chunk_cash": 1500,
             "candidates": [{"exchange_id": self.ex2, "side": "yes", "max_price": 0.6, "min_price": 0.05}],
             "start": "2026-11-03T22:00:00-05:00", "stop": "2026-11-03T23:00:00-05:00", "min_gap_sec": 20}]}
        os.makedirs(os.path.join(sim.sb, "params"), exist_ok=True)
        path = os.path.join(sim.sb, "params", "punt_plan.json")
        open(path, "w").write(json.dumps(plan))
        for f in ("punt_state.json", "punt_abort"):
            try:
                os.remove(os.path.join(sim.sb, "params", f))
            except FileNotFoundError:
                pass
        self.plan = plan
        self.cfg = {"punt_enabled": True, "punt_plan_path": "params/punt_plan.json",
                    "punt_plan_sha256": hashlib.sha256(open(path, "rb").read()).hexdigest()}

    def bot_setup(self, sim):
        b = sim.bot
        if b.punt is None or b.punt.refused:
            sim.chk.violation("punt_not_enabled", str(getattr(b.punt, "refused", "no executor")))
        self.legs = set(sim.legs(self.r1)) | set(sim.legs(self.r2))
        self.t_start = CLOCK.t

    def before_cycle(self, sim, i):
        WD = sys.modules["sigbot.proposals.wind_down"]
        v = sim.v
        if i == 40:                                  # stage 1 wins: the outside calls it, SIG reprices the leg to 0.90/0.91
            CLOCK.t = max(CLOCK.t, WD.et_ts(2026, 11, 3, 19, 50))
            v.outside[self.ex1] = 0.97
            other = [e for e in sim.legs(self.r1) if e != self.ex1][0]
            v.outside[other] = 0.03
            v.set_touch(self.ex1, int(round(0.90 / SV.TICK)), int(round(0.91 / SV.TICK)))
            v.set_touch(other, int(round(0.08 / SV.TICK)), int(round(0.09 / SV.TICK)))
            sim.bot.mm_need_xv = True
        if i == 75:
            CLOCK.t = max(CLOCK.t, WD.et_ts(2026, 11, 3, 22, 5))
            sim.bot.mm_need_xv = True

    def on_tick(self, sim):
        if sim.bot.punt is not None:
            sim.step("punt tick", sim.bot.punt.tick)
            q = sim.v.signed_positions().get(self.ex1, 0.0)
            if q < -0.5 and not getattr(self, "flip", False):
                self.flip = True
                sim.chk.violation("punt_flip", f"stage-1 leg {self.ex1} went short ({q:+.0f}): the sale sold more than held")

    def final_checks(self, sim):
        v, st = sim.v, sim.bot.punt.state if sim.bot.punt else {}
        WD = sys.modules["sigbot.proposals.wind_down"]
        win = {s["id"]: (WD_ts(s["start"]), WD_ts(s["stop"])) for s in self.plan["steps"]}
        bad, other, cost1 = [], [], 0.0
        for o in v.orders.values():
            if o.ex not in self.legs:
                continue
            if o.prefix == "punt":
                sid = o.key.split("-")[2]
                lo, hi = win[sid]
                if not lo <= o.t <= hi:
                    bad.append(f"{o.key} at {o.t - lo:+.0f}s outside its window")
                if sid == "s1":
                    if o.limit > self.cap1 + 1e-9:
                        bad.append(f"{o.key} limit {o.limit} > cap {self.cap1}")
                    cost1 += o.filled * o.limit
                if sid == "s1_sell" and not (o.side == "no" and o.limit <= 0.15 + 1e-9):
                    bad.append(f"{o.key} sells below 0.85 ({o.side} @{o.limit})")
            elif o.prefix != "seed" and o.t >= self.t_start:
                other.append(f"{o.key} {o.ex} {o.side} @{o.limit}")
        def V(kind, detail):
            sim.chk.viol.append({"scenario": self.name, "step": "end", "t": CLOCK.t, "kind": kind, "detail": detail, "trace": ""})
        if bad:
            V("punt_order", "; ".join(bad[:4]))
        if other:
            V("punt_race_touched", f"{len(other)} non-punt orders on punt races: " + "; ".join(other[:4]))
        if cost1 > self.budget + 1e-6:
            V("punt_budget", f"stage 1 cost (at limits) {cost1:.0f} > budget {self.budget:.0f}")
        steps = st.get("steps", {})
        n = {}
        for o in v.orders.values():
            if o.prefix == "punt":
                k = o.key.split("-")[2]
                n[k] = (n.get(k, (0, 0))[0] + 1, n.get(k, (0, 0))[1] + o.filled)
        sim.chk.notes.append("punt orders (count, filled) " + json.dumps(n) + "; stage-1 cost at limits %.0f of %.0f" % (cost1, self.budget))
        if not (steps.get("s1", {}).get("filled", 0) > 0 and steps.get("s1_sell", {}).get("sold", 0) > 0 and steps.get("s2", {}).get("filled", 0) > 0):
            V("punt_incomplete", json.dumps({k: {kk: steps[k].get(kk) for kk in ("status", "filled", "sold", "why")} for k in steps}))


PUNT_ALL = False          # --punt: the executor ON in every scenario's sandbox with a small test plan (stage 1 only)
# --punt leaves out the scenarios built on a cash shortfall (the test plan's 3,000 buy changes their premise, the same reason
# shock mode is off in them) and the punt scenario itself (it has its own plan)
PUNT_SKIP = {"punt", "churn_many_lowcash", "cash_raise", "cash_raise_sets", "cash_raise_cheapfav", "recycle_oversize", "shock_reserve"}


def punt_test_plan(sim) -> dict:
    """--punt: a 3,000 stage-1 buy on the cheaper leg of a 0.25-0.75 race, from 60 s into the run for an hour, hash-matched."""
    mids = two_leg_mid_races(sim, 0.25, 0.75)          # the last such race: scenarios build their premises on the first ones
    ex = min(sim.legs(mids[-1]), key=lambda e: sim.v.mid(e))
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    plan = {"plan_id": "SIMALL", "budget": 3000, "cap_exempt": True, "steps": [
        {"id": "s1", "kind": "buy", "exchange_id": ex, "side": "yes", "max_price": round(SV.P(sim.v.ask_t[ex]) + 0.02, 3),
         "max_cash": 3000, "chunk_cash": 1000, "start": iso(CLOCK.t + 60), "stop": iso(CLOCK.t + 3600), "min_gap_sec": 20}]}
    path = os.path.join(sim.sb, "params", "punt_plan.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(json.dumps(plan))
    try:
        os.remove(os.path.join(sim.sb, "params", "punt_state.json"))
    except FileNotFoundError:
        pass
    return {"punt_enabled": True, "punt_plan_path": "params/punt_plan.json",
            "punt_plan_sha256": hashlib.sha256(open(path, "rb").read()).hexdigest()}


def WD_ts(s):
    return datetime.fromisoformat(s).timestamp()


# scenarios built on a cash shortfall: with the live config's shock_enabled true, the 15k shock reserve would take their
# cash before their own premise (low cash) is reached; shock with the reserve has its own scenarios (shock*, shock_reserve)
for _c in (ChurnManyLowCash, CashRaise, CashRaiseSets, CashRaiseCheapfav, RecycleOversize):
    _c.cfg = {**(getattr(_c, "cfg", None) or {}), "shock_enabled": False}

SCENARIOS = [Calm, Sweep6, OutsideMove, Catchup, Shock, ShockMaker, ShockMakerPostOnly, CheapFav, GapPilotSim, DevChurnSim, ChurnWiden, ChurnReject, ChurnRestart, ChurnFreeFirst, ChurnInterleave, ChurnStaleFill, ChurnBlowout,
             ChurnMany, ChurnManyLowCash, CashRaise, CashRaiseSets, CashRaiseCheapfav, ShockReserve, Recycle, RecycleTail, RecycleStaleFill, RecycleRace, RecycleOversize, RecycleCash, RecycleCashGone, OwnBid, ExitsWrongSide,
             Offload, SlowWrites, FeedSilent, MMOutageLastLook, AonReject, InsideOn, WindDown, Punt, Day]


# ======================================================================================== main
def import_sandbox(sb):
    sys.path.insert(0, sb)
    import importlib
    names = ["config", "api", "universe", "store", "risk", "arb", "mm", "tilt", "direction", "allocator", "run"]
    mods = {n: importlib.import_module("sigbot." + n) for n in names}
    mods["xv_gate"] = importlib.import_module("sigbot.proposals.mm_upgrade").xv_gate
    mods["cheap_favourites"] = importlib.import_module("sigbot.proposals.cheap_favourites")
    mods["gap_pilot"] = importlib.import_module("sigbot.proposals.gap_pilot")
    assert os.path.dirname(os.path.dirname(os.path.abspath(mods["run"].__file__))) == sb, "sigbot was not imported from the sandbox"
    return mods


def main(argv):
    verbose = "-v" in argv
    if "--churn" in argv:
        FORCE_CFG["churn_enabled"] = True
    if "--raise" in argv:
        FORCE_CFG["raise_enabled"] = True
        FORCE_CFG["raise_sets_enabled"] = True
    if "--wind" in argv:                     # the wind-down on in every scenario's sandbox (it acts only from 31 Oct)
        FORCE_CFG["wind_down_enabled"] = True
    if "--punt" in argv:                     # the punt executor on in every scenario's sandbox with a small test plan
        global PUNT_ALL
        PUNT_ALL = True
    names = [a for a in argv if not a.startswith("-")]
    if "--list" in argv:
        for s in SCENARIOS:
            print(f"{s.name:14s} {s.doc}")
        return 0
    chosen = [s for s in SCENARIOS if not names or s.name in names]
    install_network_guard()
    sb = make_sandbox()
    try:
        mods = import_sandbox(sb)
        for a in argv:
            if a.startswith("--patch="):
                exec(compile(open(a.split("=", 1)[1]).read(), a, "exec"), {"mods": mods, "__name__": "sim_patch",
                                                                       "__file__": os.path.abspath(a.split("=", 1)[1])})
        lg = logging.getLogger("sigbot")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        lg.addHandler(CAP)
        install_clock([m for n, m in sys.modules.items() if n == "sigbot" or n.startswith("sigbot.")])
        allv = []
        t_all = _REAL_TIME()
        for S in chosen:
            sc = S()
            try:
                sim = Sim(sc, sb, mods, verbose)
                chk = sim.run()
            except Exception:
                print(f"HARNESS ERROR in {sc.name}:\n{traceback.format_exc()}")
                return 2
            finally:
                try:
                    sim.bot.store.conn.close()
                except Exception:
                    pass
            v = sim.v
            pnl = v.value() - sim.start_value
            arb_ok = sum(1 for c in v.calls if c["kind"] == "multileg" and c["prefix"] == "arb" and c.get("ok"))
            arb_bad = sum(1 for c in v.calls if c["kind"] == "multileg" and c["prefix"] == "arb" and not c.get("ok"))
            unw = sum(1 for c in v.calls if c["kind"] == "multileg" and c["prefix"] in ("unw", "fre") and c.get("ok"))
            kinds = collections.Counter(x["kind"] for x in chk.viol)
            print(f"{'FAIL' if chk.viol else 'ok  '} {sc.name:13s} {sc.cycles:5d} cycles {(CLOCK.t - T0) / 3600:5.1f} h sim {sim.wall:6.1f} s wall | "
                  f"P&L {pnl:+8.1f} (spread paid {v.spread_paid:7.1f}) cash {v.cash:8.0f} | fills {len(v.fills):5d} arb {arb_ok}/{arb_ok + arb_bad} "
                  f"unwinds {unw} | violations {len(chk.viol)} {dict(kinds) if kinds else ''}")
            for n_ in getattr(chk, "notes", []):
                print("      note:", n_)
            if getattr(sc, "summary", None) and (verbose or sc.name.startswith("churn") or FORCE_CFG):
                print("      summary:", sc.summary)
            if verbose:
                print("      activity:", dict(sorted(chk.counts.items())), "max |netD|", round(chk.max_netd))
            allv.extend(chk.viol)
            LAST[sc.name] = sim
        if NET_ATTEMPTS:
            print(f"HARNESS: {len(NET_ATTEMPTS)} network attempts were blocked; first from:\n{NET_ATTEMPTS[0]}")
        print(f"\n{len(chosen)} scenario(s) in {_REAL_TIME() - t_all:.0f} s, {len(allv)} violation(s)")
        seen = collections.OrderedDict()
        for x in allv:
            seen.setdefault((x["scenario"], x["kind"]), []).append(x)
        for (scn, kind), xs in seen.items():
            x = xs[0]
            mins = (x["t"] - T0) / 60
            print(f"\n[{scn}] {kind} x{len(xs)} - first at {x['step']} (t+{mins:.1f} min): {x['detail']}")
            for y in xs[1:3]:
                print(f"    also: {y['step']}: {y['detail'][:200]}")
            if x.get("trace"):
                print(x["trace"].rstrip())
        return 1 if allv or NET_ATTEMPTS else 0
    finally:
        time.time, time.sleep = _REAL_TIME, _REAL_SLEEP
        shutil.rmtree(sb, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
