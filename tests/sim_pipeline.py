"""Offline end-to-end simulation of the whole bot against a fake venue, checked against invariants after every step.

    python3 tests/sim_pipeline.py                 # every scenario; exit status 1 on any violation
    python3 tests/sim_pipeline.py calm sweep6     # only these scenarios
    python3 tests/sim_pipeline.py --list          # scenario names
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
  arb never sends a basket while our own order is a leg's best bid [arb_vs_own_order]; rates per object over sliding
  windows (RATE_LIMITS: requotes, exits, offload exits, lag entries, directional orders, rejected baskets, inside)
  [rate_*]; no race bought and unwound alternately [arb_ping_pong]; calm scenario: marked P&L + spread paid >= -25
  [calm_pnl]. Kill-switch firings and deploy-watcher thresholds are printed as notes, not violations.

Not exercised: the news LLM path (no key, nothing fetched), the shadow tuner / allocator self-tuning (stubbed: it
replays logs in a thread), election night (off in config), thread interleaving between the scanner and the main loop
(the sim runs them in sequence).
"""
from __future__ import annotations

import collections
import json
import logging
import math
import os
import random
import shutil
import socket
import sys
import tempfile
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


def make_sandbox() -> str:
    sb = tempfile.mkdtemp(prefix="sigbot-sim-")
    shutil.copytree(os.path.join(ROOT, "sigbot"), os.path.join(sb, "sigbot"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.bak*", "*.sleeves_wip", "*.pyc", "hft_data"))
    shutil.copyfile(os.path.join(ROOT, "config.json"), os.path.join(sb, "config.json"))
    shutil.copytree(os.path.join(ROOT, "params"), os.path.join(sb, "params_master"), ignore=shutil.ignore_patterns("*.bak*"))
    os.makedirs(os.path.join(sb, "data"))
    for f in ("markets.json", "volume_cache.json", "volume_cache.json.vol"):
        src = os.path.join(ROOT, "data", f)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(sb, "data", f))   # copyfile: fresh mtime, so the volume cache counts as recent
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
        D.DirectionTrader.fetch_outside = lambda self_: v.outside_quotes()
        cfg = mods["config"].BotConfig.load(os.path.join(sb, "config.json"))
        cfg.api_key = "sim-no-key"
        cfg.db_path = "data/sim.sqlite"
        for k, val in sc.cfg.items():
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
EXEMPT_ADD = {"exit", "offload", "arb", "unw", "fre", "seed", "hedge"}
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
        self.own_guard = None                              # scenario: (race, exchange, order id) of our own top-of-book bid

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
                    self.violation("passive_order_crossed", f"{c['key']} {ex} {c['side']} x{c['qty']} @{c['price']} traded {c['traded']} on arrival "
                                   f"(DMM touch {SV.P(v.bid_t[ex])}/{SV.P(v.ask_t[ex])})")
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
                        floor = min(reserve, self.cash0)
                        if c["cash_after"] < floor - 1.0:
                            self.violation("arb_cash_reserve", f"{c['key']} {race} {len(c['legs'])} legs x{c['legs'][0]['quantity']}: cash "
                                           f"{c['cash_before']:.0f} -> {c['cash_after']:.0f}, below min(reserve {reserve:.0f}, cash at step start "
                                           f"{self.cash0:.0f})")
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
        if nd > self.max_netd:
            # the block is computed once per cycle, so one cycle's directional adds (per_poll_budget 900) may overshoot it
            if nd > capp + 1000 and not self.netd_flagged:
                self.netd_flagged = True
                self.violation("partisan_cap", f"net D notional {expo.net_d_notional:+.0f} beyond cap {capp:.0f} + 1,000 (one cycle of adds)")
            self.max_netd = nd

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
        for (cat, obj), (w, mx) in NOTE_LIMITS.items():
            n = worst(self.events.get((cat, obj), []), w)
            if n > mx:
                self.notes.append(f"{cat} {obj}: {n} in {w}s exceeds the deploy watcher's alarm ({mx}); {len(self.events[(cat, obj)])} in the run")
        for race, seq in self.unwind_seq.items():
            kinds = [k for _, k in sorted(seq)]
            flips = sum(1 for a, b in zip(kinds, kinds[1:]) if a != b)
            if kinds.count("unwind") >= 2 and flips >= 3:
                self.viol.append({"scenario": self.sim.sc.name, "step": "rate check", "t": CLOCK.t, "kind": "arb_ping_pong",
                                  "detail": f"{race}: bought and unwound alternately {flips} times ({kinds.count('unwind')} unwinds)", "trace": ""})


# ======================================================================================== scenarios
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


SCENARIOS = [Calm, Sweep6, OutsideMove, Recycle, RecycleTail, RecycleRace, RecycleOversize, OwnBid, ExitsWrongSide, Offload, SlowWrites, FeedSilent, AonReject, InsideOn, Day]


# ======================================================================================== main
def import_sandbox(sb):
    sys.path.insert(0, sb)
    import importlib
    names = ["config", "api", "universe", "store", "risk", "arb", "mm", "tilt", "direction", "allocator", "run"]
    mods = {n: importlib.import_module("sigbot." + n) for n in names}
    mods["xv_gate"] = importlib.import_module("sigbot.proposals.mm_upgrade").xv_gate
    assert os.path.dirname(os.path.dirname(os.path.abspath(mods["run"].__file__))) == sb, "sigbot was not imported from the sandbox"
    return mods


def main(argv):
    verbose = "-v" in argv
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
                exec(compile(open(a.split("=", 1)[1]).read(), a, "exec"), {"mods": mods, "__name__": "sim_patch"})
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
