"""Directional trading from LIVE outside prices (Polymarket + Kalshi): value taking and move following.

    python3 -m sigbot.direction --once --dry-run        # one poll, print the mispricing table, log decisions
    python3 -m sigbot.direction --loop --dry-run        # poll every 30 s (09:00-24:00 ET) / 120 s overnight
    python3 -m sigbot.direction --once --top 40         # show more rows

Parent wiring (one object, one call per bot cycle; never raises):

    from .direction import DirectionTrader
    self.direction = DirectionTrader(self.api, self.store, None, self.universe)      # None -> config.json "direction" or defaults
    summary = self.direction.cycle(prices, positions, self.cash, write_budget, self.dry_run)

Orders are placed only when BOTH dry_run is False AND `direction.allow_live` is true in config.json (default false).

Pipeline per poll
-----------------
1. Outside quotes: one POST /books to Polymarket for every mapped YES token (<= 500 per call) and GET /markets?tickers=
   to Kalshi (100 per call), in parallel threads. Every instrument whose bid/ask changed (or every 10 minutes) is
   written to `venue_quotes`, together with the SIG touch of every leg (venue 'sig'), all stamped with the same poll
   timestamp, so the three venues can be compared at exactly the same time.
2. Party probabilities per venue: sum of candidate mids per party (multi-candidate events such as Alaska governor),
   normalised over the parties when the venue's total is within [0.85, 1.15] (removes the overround).
3. Fair value per SIG leg: liquidity-weighted blend, weight = sqrt(depth within 2c of the touch, USD) / spread.
   Venue disagreement |PM - Kalshi| widens the required edge by `disagreement_mult` x the gap.
4. Signals:
   value  buy YES when SIG ask <= fair - min_edge_take; buy NO when SIG bid >= fair + min_edge_take.
   move   an outside venue moved >= move_min_pts in move_window_sec and SIG moved less than move_follow_frac of it
          in the same window: take SIG toward the new outside price (YES if up and ask < new fair - buffer,
          NO if down and bid > new fair + buffer). Predicted direction is checked at +5/+30/+120 minutes.
   Size: per-market cap x edge scale x outside-depth scale, capped per market (cost), per poll, and overall
   (`overall_cash_frac` of cash). Never adds to a partisan direction the parent has blocked (params
   `blocked_directions`, else sigbot.risk with the market maker's portfolio cap).
5. Everything goes to `direction_signals` (dry rows are the paper book). `python3 -m sigbot.direction_report` scores them.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import risk
from .universe import Universe
from .venues.http import Http, VenueError
from .venues.kalshi import Kalshi
from .venues.mapping import MAP_PATH, build as build_map, coverage, load as load_map
from .venues.polymarket import Polymarket

log = logging.getLogger("sigbot.direction")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")
TICK = 0.005

SCHEMA = """
CREATE TABLE IF NOT EXISTS venue_quotes (
  ts REAL NOT NULL,              -- poll timestamp (same for every row of one poll)
  venue TEXT NOT NULL,           -- polymarket | kalshi | sig
  race TEXT NOT NULL,
  party TEXT NOT NULL,
  instrument TEXT NOT NULL,      -- PM token id | Kalshi ticker | SIG exchange id
  bid REAL, ask REAL, mid REAL,
  bid_sz REAL, ask_sz REAL, depth_usd REAL,
  latency_ms REAL,
  PRIMARY KEY (ts, venue, instrument)
);
CREATE INDEX IF NOT EXISTS venue_quotes_key ON venue_quotes(venue, race, party, ts);

CREATE TABLE IF NOT EXISTS venue_fair (
  ts REAL NOT NULL, race TEXT NOT NULL, party TEXT NOT NULL,
  pm REAL, kalshi REAL, fair REAL, pm_w REAL, k_w REAL,
  sig_bid REAL, sig_ask REAL, sig_mid REAL, exchange_id TEXT,
  PRIMARY KEY (ts, race, party)
);
CREATE INDEX IF NOT EXISTS venue_fair_key ON venue_fair(race, party, ts);

CREATE TABLE IF NOT EXISTS direction_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,            -- value | move
  race TEXT NOT NULL, party TEXT NOT NULL, exchange_id TEXT NOT NULL,
  side TEXT NOT NULL,            -- yes | no (what we buy)
  direction TEXT NOT NULL,       -- up | down: predicted move of the SIG YES price
  sig_bid REAL, sig_ask REAL, sig_mid REAL,
  pm REAL, kalshi REAL, fair REAL,
  edge REAL,                     -- in probability points (0.03 = 3 points), from our side
  outside_move REAL, sig_move REAL, venue_moved TEXT,
  price REAL,                    -- price paid per share in the bought side's terms
  quantity INTEGER, cost REAL,
  action TEXT NOT NULL,          -- dry | traded | skipped_blocked | skipped_cap | skipped_budget | error
  dry_run INTEGER NOT NULL,
  order_id INTEGER, filled_qty REAL,
  mid_5 REAL, mid_30 REAL, mid_120 REAL,
  fair_5 REAL, fair_30 REAL, fair_120 REAL,
  reason TEXT, response TEXT
);
CREATE INDEX IF NOT EXISTS direction_signals_ts ON direction_signals(ts);
CREATE INDEX IF NOT EXISTS direction_signals_ex ON direction_signals(exchange_id, ts);
"""


@dataclass
class DirectionConfig:
    enabled: bool = True
    allow_live: bool = False             # second switch: orders only when the parent runs live AND this is true
    poll_sec_day: float = 30.0           # 09:00-24:00 ET
    poll_sec_night: float = 120.0
    day_hours_et: list = field(default_factory=lambda: [9, 24])
    min_edge_take: float = 0.02          # value taking threshold (probability points)
    disagreement_mult: float = 0.5       # extra edge required per point of |PM - Kalshi|
    max_disagreement: float = 0.08       # ignore a leg when the venues disagree by more than this
    max_outside_spread: float = 0.12     # an outside instrument with a wider spread is not used (except near-zero legs)
    min_outside_depth_usd: float = 200.0 # a venue with less depth near the touch than this gets no weight
    single_venue_extra_edge: float = 0.01  # extra edge when only one venue prices the leg
    move_min_pts: float = 0.02           # outside move that triggers a move signal
    move_window_sec: float = 300.0
    move_follow_frac: float = 0.34       # SIG "has not followed" when it moved less than this fraction of the outside move
    move_buffer: float = 0.005           # take only while SIG is still at least this far from the new outside price
    move_size_frac: float = 0.5          # move trades use this fraction of the per-market cap
    per_market_cap: float = 1000.0       # SUSQies of cost per SIG market (value + move combined)
    overall_cash_frac: float = 0.10      # total cost of this sleeve as a share of cash
    per_poll_budget: float = 3000.0      # SUSQies of new cost per poll
    depth_full_usd: float = 20000.0      # outside depth at which size is not scaled down
    edge_full: float = 0.06              # edge at which size reaches the per-market cap
    min_qty: int = 20
    signal_cooldown_sec: float = 600.0   # re-log an actionable value signal on the same leg only after this long or a 1-point change
    skip_log_cooldown_sec: float = 3600.0  # a standing signal that cannot be sized (caps) is re-logged at most this often
    move_reserve_frac: float = 0.30      # share of the sleeve cap kept free for move trades (value takes cannot use it)
    order_ttl_sec: int = 60              # unfilled remainder of a taker order dies after this
    quote_heartbeat_sec: float = 600.0   # write unchanged quotes to venue_quotes at least this often
    map_max_age_hours: float = 24.0
    followup_minutes: list = field(default_factory=lambda: [5, 30, 120])
    portfolio_cap: Optional[float] = None  # partisan cap; None -> config.json mm.portfolio_max_partisan_notional (5000)

    @staticmethod
    def load(path: Optional[str] = None, override: Optional[dict] = None) -> "DirectionConfig":
        cfg = DirectionConfig()
        path = path or os.path.join(HERE, "config.json")
        raw = {}
        if os.path.exists(path):
            try:
                raw = json.load(open(path))
            except Exception as e:
                log.warning("config.json unreadable: %s", e)
        names = {f.name for f in fields(DirectionConfig)}
        for k, v in (raw.get("direction") or {}).items():
            if k in names:
                setattr(cfg, k, v)
        for k, v in (override or {}).items():
            if k in names:
                setattr(cfg, k, v)
        if cfg.portfolio_cap is None:
            cfg.portfolio_cap = float((raw.get("mm") or {}).get("portfolio_max_partisan_notional", 5000.0))
        return cfg


def _now() -> float:
    return time.time()


def _mid(q: dict) -> Optional[float]:
    b, a = q.get("bid"), q.get("ask")
    if b is not None and a is not None:
        return (b + a) / 2
    if a is not None and a <= 0.03:          # near-zero leg with no bid: half the ask
        return a / 2
    if b is not None and b >= 0.97:
        return (b + 1) / 2
    return None


def _round_tick(p: float) -> float:
    return min(0.995, max(0.005, round(round(p / TICK) * TICK, 3)))


class DirectionTrader:
    def __init__(self, api, store, cfg=None, universe: Optional[Universe] = None):
        self.api, self.store, self.u = api, store, universe
        if isinstance(cfg, DirectionConfig):
            self.cfg = cfg
        else:
            self.cfg = DirectionConfig.load(override=cfg if isinstance(cfg, dict) else None)
        if self.store is not None:
            with self.store.lock:
                self.store.conn.executescript(SCHEMA)
        self.http = Http()
        self.pm = Polymarket(self.http)
        self.ks = Kalshi(self.http, os.path.join(HERE, "data"))
        self.map: dict = {}
        self.map_loaded = 0.0
        self.last_poll = 0.0
        self.last_stored: dict = {}                 # (venue, instrument) -> (bid, ask, ts)
        self.hist: dict = {}                        # (race, party, venue) -> deque[(ts, prob)]; venue in pm|kalshi|sig
        self.last_signal: dict = {}                 # (exchange_id, kind) -> (ts, edge)
        self.rows: list[dict] = []                  # last mispricing table
        self.last_exec: dict = {}                   # exchange_id -> (action, qty, cost) of its latest sized signal
        self.held: dict = {}                        # exchange_id -> cost held by this sleeve (paper in dry runs)
        self.latency: dict = {}
        self.lock = threading.Lock()
        self._ensure_map()
        self._load_last_signals()

    # ------------------------------------------------------------------ map
    def _ensure_map(self):
        if self.map and _now() - self.map_loaded < self.cfg.map_max_age_hours * 3600:
            return
        try:
            self.map = load_map(self.cfg.map_max_age_hours * 3600)
        except Exception as e:                     # keep the old map if the rebuild fails
            log.warning("race map rebuild failed: %s", e)
            if not self.map and os.path.exists(MAP_PATH):
                self.map = json.load(open(MAP_PATH))
        self.map_loaded = _now()
        self.pm_tokens = sorted({l["token"] for e in self.map.get("races", {}).values() for ls in e.get("polymarket", {}).get("legs", {}).values() for l in ls})
        self.k_tickers = sorted({l["ticker"] for e in self.map.get("races", {}).values() for ls in e.get("kalshi", {}).get("legs", {}).values() for l in ls})

    # ---------------------------------------------------------------- poll
    def poll_interval(self) -> float:
        h = datetime.now(ET).hour
        a, b = self.cfg.day_hours_et
        return self.cfg.poll_sec_day if a <= h < b else self.cfg.poll_sec_night

    def fetch_outside(self) -> tuple[dict, dict]:
        """Both venues in parallel. Returns ({token: quote}, {ticker: quote}); a failed venue returns {}."""
        def pmf():
            t0 = _now()
            try:
                return self.pm.books(self.pm_tokens), (_now() - t0) * 1000
            except VenueError as e:
                log.warning("polymarket poll failed: %s", e)
                return {}, None

        def ksf():
            t0 = _now()
            try:
                return self.ks.quotes(self.k_tickers), (_now() - t0) * 1000
            except VenueError as e:
                log.warning("kalshi poll failed: %s", e)
                return {}, None

        with ThreadPoolExecutor(max_workers=2) as ex:
            fp, fk = ex.submit(pmf), ex.submit(ksf)
            (pmq, pm_ms), (kq, k_ms) = fp.result(), fk.result()
        self.latency = {"polymarket_ms": round(pm_ms) if pm_ms else None, "kalshi_ms": round(k_ms) if k_ms else None,
                        "polymarket": self.pm.state.stats(), "kalshi": self.ks.state.stats()}
        return pmq, kq

    @staticmethod
    def venue_probs(legs: dict, quotes: dict, key: str, max_spread: float) -> tuple[dict, dict]:
        """Per-party probability and liquidity weight for one venue. legs: {party: [{token|ticker}]}."""
        probs, depth, spreads = {}, {}, {}
        for party, ls in legs.items():
            tot, dep, sp, have = 0.0, 0.0, [], False
            for l in ls:
                q = quotes.get(l[key])
                if not q:
                    continue
                m = _mid(q)
                if m is None:
                    continue
                if q.get("bid") is not None and q.get("ask") is not None:
                    s = q["ask"] - q["bid"]
                    if s > max_spread and m > 0.03:
                        continue
                    sp.append(max(s, 0.001))
                tot += m
                dep += q.get("depth_usd") or 0.0
                have = True
            if have:
                probs[party], depth[party] = tot, dep
                spreads[party] = min(sp) if sp else 0.01
        s = sum(probs.values())
        if probs and 0.85 <= s <= 1.15 and len(probs) >= 2:
            probs = {p: v / s for p, v in probs.items()}
        w = {p: math.sqrt(max(0.0, depth[p])) / max(spreads[p], 0.005) for p in probs}
        return probs, {"w": w, "depth": depth, "spread": spreads}

    def compute(self, prices: dict, pmq: dict, kq: dict, ts: float) -> list[dict]:
        """One row per SIG leg with outside probabilities, fair, edges."""
        rows = []
        cfg = self.cfg
        for race_name, race in (self.u.races.items() if self.u else []):
            e = self.map.get("races", {}).get(race_name)
            if not e:
                continue
            pmp, pmi = self.venue_probs(e.get("polymarket", {}).get("legs", {}), pmq, "token", cfg.max_outside_spread)
            kp, ki = self.venue_probs(e.get("kalshi", {}).get("legs", {}), kq, "ticker", cfg.max_outside_spread)
            for party, leg in race.legs.items():
                p = prices.get(leg.exchange_id) or {}
                bb, ba = p.get("bestBid"), p.get("bestAsk")
                pm_v, k_v = pmp.get(party), kp.get(party)
                # a party the venue does not list is ~0 when the listed parties already sum to ~1
                if pm_v is None and pmp and sum(pmp.values()) >= 0.97 and len(pmp) >= 2:
                    pm_v = 0.0
                if k_v is None and kp and sum(kp.values()) >= 0.97 and len(kp) >= 2:
                    k_v = 0.0
                pm_dep = pmi["depth"].get(party, 0.0) if pm_v else 0.0
                k_dep = ki["depth"].get(party, 0.0) if k_v else 0.0
                pm_w = pmi["w"].get(party, 0.0) if pm_v is not None and pmi["depth"].get(party, 0.0) >= cfg.min_outside_depth_usd else 0.0
                k_w = ki["w"].get(party, 0.0) if k_v is not None and ki["depth"].get(party, 0.0) >= cfg.min_outside_depth_usd else 0.0
                if pm_v == 0.0 and pm_w == 0.0 and pm_v is not None:
                    pm_w = 1.0            # implied-zero legs get a nominal weight
                if k_v == 0.0 and k_w == 0.0 and k_v is not None:
                    k_w = 1.0
                fair = None
                if pm_w + k_w > 0:
                    fair = ((pm_v or 0.0) * pm_w + (k_v or 0.0) * k_w) / (pm_w + k_w)
                n_venues = int(pm_w > 0) + int(k_w > 0)
                disagree = abs(pm_v - k_v) if (pm_v is not None and k_v is not None and pm_w > 0 and k_w > 0) else 0.0
                need = cfg.min_edge_take + cfg.disagreement_mult * disagree + (cfg.single_venue_extra_edge if n_venues == 1 else 0.0)
                edge_yes = (fair - ba) if (fair is not None and ba is not None) else None
                edge_no = (bb - fair) if (fair is not None and bb is not None) else None
                sig_mid = (bb + ba) / 2 if bb is not None and ba is not None else None
                rows.append({"ts": ts, "race": race_name, "party": party, "exchange_id": leg.exchange_id, "office": e.get("office"),
                             "sig_bid": bb, "sig_ask": ba, "sig_mid": sig_mid, "pm": pm_v, "kalshi": k_v, "fair": fair,
                             "pm_w": pm_w, "k_w": k_w, "outside_depth": pm_dep + k_dep, "disagree": disagree, "need": need,
                             "n_venues": n_venues, "edge_yes": edge_yes, "edge_no": edge_no})
        return rows

    # ------------------------------------------------------------ storage
    def store_quotes(self, ts: float, prices: dict, pmq: dict, kq: dict):
        if self.store is None:
            return
        out = []
        hb = self.cfg.quote_heartbeat_sec

        def add(venue, race, party, inst, q):
            key = (venue, inst)
            prev = self.last_stored.get(key)
            sig = (q.get("bid"), q.get("ask"))
            if prev and prev[0] == sig and ts - prev[1] < hb:
                return
            self.last_stored[key] = (sig, ts)
            out.append((ts, venue, race, party, inst, q.get("bid"), q.get("ask"), _mid(q), q.get("bid_sz"), q.get("ask_sz"),
                        q.get("depth_usd"), q.get("latency_ms")))

        for race, e in self.map.get("races", {}).items():
            for party, ls in e.get("polymarket", {}).get("legs", {}).items():
                for l in ls:
                    if l["token"] in pmq:
                        add("polymarket", race, party, l["token"], pmq[l["token"]])
            for party, ls in e.get("kalshi", {}).get("legs", {}).items():
                for l in ls:
                    if l["ticker"] in kq:
                        add("kalshi", race, party, l["ticker"], kq[l["ticker"]])
        if self.u:
            for race_name, race in self.u.races.items():
                for party, leg in race.legs.items():
                    p = prices.get(leg.exchange_id)
                    if p:
                        add("sig", race_name, party, leg.exchange_id, {"bid": p.get("bestBid"), "ask": p.get("bestAsk")})
        with self.store.lock:
            self.store.conn.execute("BEGIN")
            self.store.conn.executemany("INSERT OR REPLACE INTO venue_quotes(ts,venue,race,party,instrument,bid,ask,mid,bid_sz,ask_sz,depth_usd,latency_ms) "
                                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", out)
            self.store.conn.execute("COMMIT")

    def store_fair(self, rows: list[dict]):
        if self.store is None or not rows:
            return
        vals = [(r["ts"], r["race"], r["party"], r["pm"], r["kalshi"], r["fair"], r["pm_w"], r["k_w"], r["sig_bid"], r["sig_ask"],
                 r["sig_mid"], r["exchange_id"]) for r in rows if r["fair"] is not None]
        with self.store.lock:
            self.store.conn.execute("BEGIN")
            self.store.conn.executemany("INSERT OR REPLACE INTO venue_fair(ts,race,party,pm,kalshi,fair,pm_w,k_w,sig_bid,sig_ask,sig_mid,exchange_id) "
                                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", vals)
            self.store.conn.execute("COMMIT")

    def _push_hist(self, rows: list[dict]):
        horizon = max(self.cfg.move_window_sec * 3, 1800)
        for r in rows:
            for venue, v in (("pm", r["pm"] if r["pm_w"] > 0 else None), ("kalshi", r["kalshi"] if r["k_w"] > 0 else None), ("sig", r["sig_mid"])):
                if v is None:
                    continue
                d = self.hist.setdefault((r["race"], r["party"], venue), deque())
                d.append((r["ts"], v))
                while d and d[0][0] < r["ts"] - horizon:
                    d.popleft()

    def _value_at(self, race, party, venue, ts) -> Optional[float]:
        """Most recent value at or before ts (None if no observation within 2.5 windows before ts)."""
        d = self.hist.get((race, party, venue))
        if not d:
            return None
        best = None
        for t, v in d:
            if t <= ts:
                best = (t, v)
            else:
                break
        if best is None or ts - best[0] > self.cfg.move_window_sec * 1.5:
            return None
        return best[1]

    # ------------------------------------------------------------ signals
    def blocked(self, positions: dict, prices: dict) -> set:
        if self.store is not None:
            b = self.store.get_param("blocked_directions")
            if isinstance(b, list):
                return set(b)
        if not self.u:
            return set()
        try:
            return risk.blocked_directions(risk.exposure(self.u, positions, prices), self.cfg.portfolio_cap)
        except Exception:
            return set()

    def sleeve_cost(self, dry_run: bool) -> tuple[dict, dict]:
        """Cost committed by this sleeve per kind and per exchange (dry rows in dry runs, traded rows when live)."""
        if self.store is None:
            return {}, {}
        act = "dry" if dry_run else "traded"
        per: dict = {}
        kinds: dict = {}
        for r in self.store.rows("SELECT exchange_id, kind, SUM(cost) AS c FROM direction_signals WHERE action=? GROUP BY 1,2", (act,)):
            per[r["exchange_id"]] = per.get(r["exchange_id"], 0.0) + float(r["c"] or 0.0)
            kinds[r["kind"]] = kinds.get(r["kind"], 0.0) + float(r["c"] or 0.0)
        return kinds, per

    def _load_last_signals(self):
        """Seed the re-log cooldown from the table so a restart does not re-log every standing signal."""
        if self.store is None:
            return
        for r in self.store.rows("SELECT exchange_id, kind, side, edge, action, MAX(ts) AS ts FROM direction_signals WHERE ts>=? GROUP BY exchange_id, kind",
                                 (_now() - 3 * 3600,)):
            self.last_signal[(r["exchange_id"], r["kind"])] = (r["ts"], r["edge"], r["side"], r["action"])

    def signals(self, rows: list[dict], ts: float) -> list[dict]:
        cfg = self.cfg
        out = []
        for r in rows:
            if r["fair"] is None or r["sig_bid"] is None or r["sig_ask"] is None:
                r["action"] = ""
                continue
            if r["disagree"] > cfg.max_disagreement:
                r["action"] = "venues disagree"
                continue
            act = ""
            # ---- value
            if r["edge_yes"] is not None and r["edge_yes"] >= r["need"]:
                out.append(dict(r, kind="value", side="yes", direction="up", edge=r["edge_yes"], price=r["sig_ask"]))
                act = f"BUY YES @{r['sig_ask']:.3f}"
            elif r["edge_no"] is not None and r["edge_no"] >= r["need"]:
                out.append(dict(r, kind="value", side="no", direction="down", edge=r["edge_no"], price=round(1 - r["sig_bid"], 3)))
                act = f"BUY NO @{1 - r['sig_bid']:.3f}"
            # ---- move: an outside venue moved, SIG did not follow
            for venue in ("pm", "kalshi"):
                now_v = r["pm"] if venue == "pm" else r["kalshi"]
                w = r["pm_w"] if venue == "pm" else r["k_w"]
                if now_v is None or w <= 0:
                    continue
                then_v = self._value_at(r["race"], r["party"], venue, ts - cfg.move_window_sec)
                sig_then = self._value_at(r["race"], r["party"], "sig", ts - cfg.move_window_sec)
                if then_v is None or sig_then is None or r["sig_mid"] is None:
                    continue
                mv = now_v - then_v
                if abs(mv) < cfg.move_min_pts:
                    continue
                smv = r["sig_mid"] - sig_then
                if smv * math.copysign(1, mv) >= cfg.move_follow_frac * abs(mv):
                    continue                       # SIG already followed
                target = r["fair"]
                if mv > 0 and r["sig_ask"] <= target - cfg.move_buffer:
                    out.append(dict(r, kind="move", side="yes", direction="up", edge=target - r["sig_ask"], price=r["sig_ask"],
                                    outside_move=mv, sig_move=smv, venue_moved=venue))
                    act = (act + "; " if act else "") + f"MOVE {venue} {mv * 100:+.1f}pt -> BUY YES"
                elif mv < 0 and r["sig_bid"] >= target + cfg.move_buffer:
                    out.append(dict(r, kind="move", side="no", direction="down", edge=r["sig_bid"] - target, price=round(1 - r["sig_bid"], 3),
                                    outside_move=mv, sig_move=smv, venue_moved=venue))
                    act = (act + "; " if act else "") + f"MOVE {venue} {mv * 100:+.1f}pt -> BUY NO"
                break
            r["action"] = act
        # moves first (time-critical), then by return on the capital locked until settlement
        out.sort(key=lambda s: (s["kind"] != "move", -s["edge"] / max(s["price"], 0.01)))
        return out

    def size(self, s: dict, room_market: float, room_total: float) -> int:
        cfg = self.cfg
        edge_scale = min(1.0, max(0.0, s["edge"] / cfg.edge_full))
        # depth measures how far to trust the outside price, so scale gently: sqrt, floored at 25%
        depth_scale = min(1.0, max(0.25, math.sqrt((s.get("outside_depth") or 0.0) / cfg.depth_full_usd)))
        frac = cfg.move_size_frac if s["kind"] == "move" else 1.0
        cost = min(cfg.per_market_cap * frac * edge_scale * depth_scale, room_market, room_total)
        return int(cost / max(s["price"], 0.005))

    def execute(self, sigs: list[dict], positions: dict, prices: dict, cash: float, write_budget: int, dry_run: bool) -> dict:
        cfg = self.cfg
        live = (not dry_run) and cfg.allow_live and self.api is not None
        blocked = self.blocked(positions, prices)
        kinds, per = self.sleeve_cost(not live)
        cap = cfg.overall_cash_frac * cash
        used = sum(kinds.values())
        poll_left = cfg.per_poll_budget
        room = {"value": max(0.0, cap * (1 - cfg.move_reserve_frac) - kinds.get("value", 0.0)),
                "move": max(0.0, cap - used)}
        writes = 0
        summ = {"signals": len(sigs), "logged": 0, "dry": 0, "traded": 0, "skipped": 0, "writes": 0, "blocked": sorted(blocked)}
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=cfg.order_ttl_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        seen_ex = set()
        for s in sigs:
            ex = s["exchange_id"]
            if ex in seen_ex:
                continue
            seen_ex.add(ex)
            key = (ex, s["kind"])
            prev = self.last_signal.get(key)
            room_m = max(0.0, cfg.per_market_cap - per.get(ex, 0.0))
            room_k = min(room[s["kind"]], poll_left, room["move"])
            sizable = room_m >= 5 and room_k >= 5 and risk.adds(self.u, ex, s["side"]) not in blocked
            if s["kind"] == "value" and prev and prev[2] == s["side"] and abs(s["edge"] - prev[1]) < 0.01:
                wait = cfg.signal_cooldown_sec if sizable else cfg.skip_log_cooldown_sec
                retry = sizable and str(prev[3] if len(prev) > 3 else "").startswith("skipped")   # skipped last time, room now
                if not retry and _now() - prev[0] < wait:
                    continue                        # same standing mispricing; do not re-log every poll
            action, qty, cost, oid, filled, resp = None, 0, 0.0, None, None, None
            if risk.adds(self.u, ex, s["side"]) in blocked:
                action = "skipped_blocked"
            else:
                qty = self.size(s, room_m, room_k)
                if qty < cfg.min_qty:
                    action, qty = ("skipped_cap" if room_m < 5 or room_k < 5 else "skipped_small"), 0
                elif live and writes >= write_budget:
                    action, qty = "skipped_budget", 0
                else:
                    cost = qty * s["price"]
                    if live:
                        try:
                            resp = self.api.place_order(ex, s["side"], "buy", qty, s["price"], expiration=ttl)
                            writes += 1
                            oid = resp.get("orderId") or resp.get("id")
                            filled = resp.get("filledQuantity")
                            action = "traded"
                            if oid and hasattr(self.store, "record_order"):
                                self.store.record_order(oid, ex, None, s["side"], "buy", s["price"], qty, "direction", None, None,
                                                        s["fair"], s["sig_mid"], None, positions.get(ex, 0.0), None, ttl, "open", resp)
                        except Exception as e:
                            action, resp = "error", str(e)
                            log.warning("direction order %s %s failed: %s", ex, s["side"], e)
                    else:
                        action = "dry"
                    if action in ("dry", "traded"):
                        per[ex] = per.get(ex, 0.0) + cost
                        room[s["kind"]] -= cost
                        room["move"] -= cost if s["kind"] == "value" else 0.0
                        poll_left -= cost
            self._record(s, action, qty, cost, not live, oid, filled, resp)
            self.last_signal[key] = (_now(), s["edge"], s["side"], action)
            if action in ("dry", "traded"):
                self.last_exec[ex] = (action, qty, cost)
            elif action != "skipped_cap" or ex not in self.last_exec:
                self.last_exec[ex] = (action, 0, 0.0)
            summ["logged"] += 1
            if action in ("dry", "traded"):
                summ[action] += 1
            else:
                summ["skipped"] += 1
        summ["writes"] = writes
        self.held = per
        return summ

    def _record(self, s, action, qty, cost, dry, oid, filled, resp):
        if self.store is None:
            return
        reason = (f"{s['kind']}: fair {s['fair']:.3f} (pm {s['pm'] if s['pm'] is not None else '-'}, k {s['kalshi'] if s['kalshi'] is not None else '-'}) "
                  f"vs SIG {s['sig_bid']}/{s['sig_ask']}, need {s['need']:.3f}")
        self.store.exec(
            "INSERT INTO direction_signals(ts,kind,race,party,exchange_id,side,direction,sig_bid,sig_ask,sig_mid,pm,kalshi,fair,edge,outside_move,sig_move,"
            "venue_moved,price,quantity,cost,action,dry_run,order_id,filled_qty,reason,response) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["ts"], s["kind"], s["race"], s["party"], s["exchange_id"], s["side"], s["direction"], s["sig_bid"], s["sig_ask"], s["sig_mid"],
             s["pm"], s["kalshi"], s["fair"], s["edge"], s.get("outside_move"), s.get("sig_move"), s.get("venue_moved"), s["price"], qty, cost,
             action, int(dry), oid, filled, reason, json.dumps(resp)[:5000] if resp is not None else None))

    def followups(self, rows: list[dict]):
        """Fill mid_N / fair_N on signals that are N minutes old, from this poll's SIG mids and fairs."""
        if self.store is None:
            return
        by_ex = {r["exchange_id"]: r for r in rows}
        for m in self.cfg.followup_minutes:
            if m not in (5, 30, 120):
                continue
            for s in self.store.rows(f"SELECT id, exchange_id, ts FROM direction_signals WHERE mid_{m} IS NULL AND ts<=? AND ts>=?",
                                     (_now() - m * 60, _now() - m * 60 - 1800)):
                r = by_ex.get(s["exchange_id"])
                if r and r["sig_mid"] is not None:
                    self.store.exec(f"UPDATE direction_signals SET mid_{m}=?, fair_{m}=? WHERE id=?", (r["sig_mid"], r["fair"], s["id"]))

    # ---------------------------------------------------------------- cycle
    def cycle(self, prices: dict, positions: dict, cash: float, write_budget: int = 0, dry_run: bool = True, force: bool = False) -> dict:
        """Called by the parent each bot cycle. Polls outside venues when due; never raises."""
        summary: dict[str, Any] = {"polled": False}
        if not self.cfg.enabled:
            return summary
        try:
            if not force and _now() - self.last_poll < self.poll_interval() - 1:
                return summary
            with self.lock:
                self.last_poll = _now()
                self._ensure_map()
                t0 = _now()
                pmq, kq = self.fetch_outside()
                ts = _now()
                rows = self.compute(prices, pmq, kq, ts)
                self.store_quotes(ts, prices, pmq, kq)
                self.store_fair(rows)
                sigs = self.signals(rows, ts)
                self._push_hist(rows)
                ex_summary = self.execute(sigs, positions or {}, prices, cash, write_budget, dry_run)
                self.followups(rows)
                self.rows = rows
                summary.update(polled=True, poll_sec=round(_now() - t0, 2), pm_quotes=len(pmq), kalshi_quotes=len(kq),
                               priced=sum(1 for r in rows if r["fair"] is not None), latency=self.latency, **ex_summary)
        except Exception as e:                       # the sleeve must never kill the parent loop
            log.exception("direction cycle failed: %s", e)
            summary["error"] = str(e)
        return summary

    # ---------------------------------------------------------------- table
    def table(self, top: int = 25) -> str:
        rows = [r for r in self.rows if r["fair"] is not None and r["sig_bid"] is not None]

        def best_edge(r):
            return max(r["edge_yes"] or -1, r["edge_no"] or -1)
        rows.sort(key=lambda r: -best_edge(r))
        f = lambda x: f"{x:.3f}" if isinstance(x, (int, float)) and x is not None else "  -  "
        lines = [f"{'race':26s} {'pty':3s} {'SIG bid/ask':>13s} {'Poly':>6s} {'Kalshi':>6s} {'fair':>6s} {'edge':>6s} {'need':>5s} {'last qty':>8s} {'held':>6s}  action"]
        for r in rows[:top]:
            e = best_edge(r)
            act, qty, cost = self.last_exec.get(r["exchange_id"], ("", 0, 0.0)) if r.get("action") else ("", 0, 0.0)
            held = self.held.get(r["exchange_id"], 0.0)
            tag = r.get("action", "") + (f"  [{act}]" if act and act not in ("dry", "traded") else "")
            lines.append(f"{r['race'][:26]:26s} {r['party']:3s} {f(r['sig_bid']):>6s}/{f(r['sig_ask']):<6s} {f(r['pm']):>6s} {f(r['kalshi']):>6s} "
                         f"{f(r['fair']):>6s} {e * 100:+5.1f}  {r['need'] * 100:4.1f} {qty:8d} {held:6.0f}  {tag}")
        return "\n".join(lines)


# =============================================================== standalone CLI
def _sig_client(reads_per_min: int):
    from .api import SuperMarketClient
    key = os.environ.get("SIG_API_KEY") or (open(os.path.join(HERE, ".sig_api_key")).read().strip() if os.path.exists(os.path.join(HERE, ".sig_api_key")) else "")
    if not key:
        raise SystemExit("no SIG API key")
    api = SuperMarketClient(key, reads_per_min=reads_per_min, writes_per_min=4)
    return api


def main():
    ap = argparse.ArgumentParser(description="Directional trader from live Polymarket/Kalshi prices (dry run by default)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="default; kept for symmetry with sigbot.run")
    ap.add_argument("--live", action="store_true", help="place orders (also needs direction.allow_live=true in config.json)")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--rebuild-map", action="store_true")
    ap.add_argument("--reads-per-min", type=int, default=8, help="SIG read budget for this process (the live bot shares the account limit)")
    ap.add_argument("--slug", default="midterm-elections")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(HERE, "data", "direction.log"))])
    if args.rebuild_map:
        m = build_map()
        for office, c in coverage(m).items():
            print(f"{office:9s} races={c['races']:3d} polymarket={c['polymarket']:3d} kalshi={c['kalshi']:3d} both={c['both']:3d}")
    from .store import Store
    store = Store(os.path.join(HERE, "data", "sigbot.sqlite"))
    api = _sig_client(args.reads_per_min)
    t = api.tournament(args.slug)
    api.tournament_id = t["id"]
    u = Universe.from_markets(api.markets())
    dt = DirectionTrader(api, store, None, u)
    dry = not args.live
    cash = float(t.get("myBalance") or 0.0)
    positions: dict = {}
    last_acct = 0.0
    while True:
        start = _now()
        if _now() - last_acct > 600:
            try:
                cash = float(api.account().get("balance") or cash)
                p = api.positions(args.slug)
                positions = {str(x["exchangeId"]): float(x["quantity"]) for x in p.get("positions", []) if not x.get("settled")}
            except Exception as e:
                log.warning("account/positions: %s", e)
            last_acct = _now()
        prices = api.prices(u.all_exchanges())
        s = dt.cycle(prices, positions, cash, write_budget=2, dry_run=dry, force=True)
        print(f"\n=== {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}  priced {s.get('priced')} legs  pm {s.get('pm_quotes')} quotes "
              f"({s.get('latency', {}).get('polymarket_ms')} ms)  kalshi {s.get('kalshi_quotes')} quotes ({s.get('latency', {}).get('kalshi_ms')} ms)  "
              f"signals {s.get('signals')} logged {s.get('logged')} dry {s.get('dry')} skipped {s.get('skipped')} blocked {s.get('blocked')}")
        print(dt.table(args.top))
        if args.once or not args.loop:
            break
        time.sleep(max(1.0, dt.poll_interval() - (_now() - start)))


if __name__ == "__main__":
    main()
