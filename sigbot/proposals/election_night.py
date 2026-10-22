"""Election-night sleeve for the SIG Predictions Cup (paper module: nothing here sends an order).

From 18:00 ET on 3 Nov 2026 poll Kalshi and Polymarket every 3-5 s for every race SIG lists, detect race calls and large
outside moves after a state's polls close, and take SIG's touch in the direction of the move before the designated
market maker (DMM) reprices. Evidence and numbers: research_notes/election_night.md (2024 election night, 1-minute
Polymarket/Kalshi paths, `data/election_night/`), research_notes/volatile_moves.md section 5, bot_exploits.md 2.1
(the DMM requotes from an outside signal and follows a 1.5+ point outside move by 0% at 1-2 min, 9% at 3, 29% at 10,
44% at 30 min).

Rules (all thresholds in `Config`):
  clock     a race is watched from its state's first poll close (POLL_CLOSE_ET, ET hours on election day; >24 = next
            day). Calls are only accepted after the state's last poll close: AP does not call a state before then.
  call      party P is called when every outside venue quoting the race has P >= call_level (0.95; a single venue
            needs 0.97), or `on_call()` delivers an AP / Decision Desk call. Buy P (YES on P's leg, plus NO on the
            other leg of a two-leg race when cheaper) at prices <= min(fair - min_edge_call, max_call_price), walking
            the touch depth, until the per-race cap is used. Re-buy each poll while the DMM refills its stale touch.
            Hold to settlement.
  move      before a call: P's outside fair rose >= move_pts within move_window_sec, both venues moved the same way,
            and the SIG mid has followed less than follow_frac of it. Buy up to fair - min_edge_move, at most
            move_cap_frac of the race cap. Exit at half convergence (SIG mid has closed exit_frac of the entry gap),
            on a reversal of reverse_pts, after hold_sec, or when the race is called the other way. A call for P
            turns the position into a called hold.
  kill      race: Polymarket and Kalshi disagree by > kill_disagree on two consecutive polls -> no new entries, move
            positions exit. Called holds exit if the outside fair falls below reversal_level on every venue (a call
            reversal). Global: kill_races or more races in disagreement at once, or outside data older than stale_sec
            -> no new entries anywhere.
  end       uncalled positions are flattened from flatten_et (11:30 ET on 4 Nov); SIG trading ends at 12:00 ET.

Orders are dicts like the other proposals: {exchangeId, side, action='buy', quantity, price (worst level = limit),
race, kind ('call'|'move'|'exit'), reason, ttl_sec}. Exits buy the opposite side (as run.zero_ev_exits does).

CLI:
  python3 -m sigbot.proposals.election_night --selftest
  python3 -m sigbot.proposals.election_night --clock                 # races in poll-closing order (from data/markets.json)
  python3 -m sigbot.proposals.election_night --simulate [--scenario base|fast|slow|kappa1|pull|all] [--cap 10000]
        replays 2024 election night (data/election_night/paths_2024.csv) against a lagging-DMM model of SIG
  python3 -m sigbot.proposals.election_night --record --minutes 5 [--every 4]
        polls Polymarket + Kalshi only (zero SIG reads) and appends to data/election_night/stream_<date>.jsonl
  python3 -m sigbot.proposals.election_night --replay data/election_night/stream_20261103.jsonl [--cap 10000]
        dry run on a recorded stream; SIG books come from the stream if recorded, else from the lagging-DMM model

Live wiring (main session; feed books, no SIG REST reads):
  en = ElectionNight(Config(per_race_cap=..., total_cap=...), legs=universe_legs(universe))
  stream = OutsideStream()                                  # race_map.json tokens/tickers
  every 4 s from 18:00 ET 3 Nov: rows = stream.poll(en.watched(now)); orders = en.cycle(now, rows, feed.books())
  send orders with purpose='election', expiration = now + ttl_sec; report fills with en.on_fill(order, qty, price)
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(HERE, "data", "election_night")
TICK = 0.005
EST = timezone(timedelta(hours=-5))          # all three election days fall after the DST change
ELECTION_DAY = {2022: (2022, 11, 8), 2024: (2024, 11, 5), 2026: (2026, 11, 3)}

# Poll closing, ET hours on election day (first = most of the state, last = the latest part). Sources:
# data/election_night/poll_close_2026.json (Green Papers / 270toWin / NCSL); unchanged from 2022 and 2024.
POLL_CLOSE_ET = {
    "IN": (18.0, 19.0), "KY": (18.0, 19.0),
    "FL": (19.0, 20.0), "GA": (19.0, 19.0), "SC": (19.0, 19.0), "VA": (19.0, 19.0), "VT": (19.0, 19.0), "NH": (19.0, 20.0),
    "NC": (19.5, 19.5), "OH": (19.5, 19.5), "WV": (19.5, 19.5),
    "AL": (20.0, 20.0), "CT": (20.0, 20.0), "DE": (20.0, 20.0), "DC": (20.0, 20.0), "IL": (20.0, 20.0), "ME": (20.0, 20.0),
    "MD": (20.0, 20.0), "MA": (20.0, 20.0), "MS": (20.0, 20.0), "MO": (20.0, 20.0), "NJ": (20.0, 20.0), "OK": (20.0, 20.0),
    "PA": (20.0, 20.0), "RI": (20.0, 20.0), "TN": (20.0, 20.0), "MI": (20.0, 21.0), "TX": (20.0, 21.0), "KS": (20.0, 21.0),
    "SD": (20.0, 21.0), "ND": (20.0, 21.0), "AR": (20.5, 20.5),
    "AZ": (21.0, 21.0), "CO": (21.0, 21.0), "LA": (21.0, 21.0), "MN": (21.0, 21.0), "NE": (21.0, 21.0), "NM": (21.0, 21.0),
    "NY": (21.0, 21.0), "WI": (21.0, 21.0), "WY": (21.0, 21.0), "IA": (21.0, 21.0),
    "MT": (22.0, 22.0), "NV": (22.0, 22.0), "UT": (22.0, 22.0), "ID": (22.0, 23.0),
    "CA": (23.0, 23.0), "OR": (23.0, 23.0), "WA": (23.0, 23.0),
    "HI": (24.0, 24.0), "AK": (24.0, 25.0),
}
# Count slowly: AP calls of close races here often come days later (volatile_moves.md section 5).
SLOW_COUNT = {"AK", "CA", "ME", "AZ", "NV", "OR", "WA", "UT"}


def poll_close_ts(abbr: str, year: int = 2026, which: str = "last") -> Optional[float]:
    if abbr not in POLL_CLOSE_ET or year not in ELECTION_DAY:
        return None
    h = POLL_CLOSE_ET[abbr][0 if which == "first" else 1]
    y, m, d = ELECTION_DAY[year]
    return datetime(y, m, d, tzinfo=EST).timestamp() + h * 3600


def race_abbr(race: str) -> Optional[str]:
    """SIG race name -> postal code ('Georgia Senate' -> 'GA', 'AZ-01 House race' -> 'AZ'); chamber races -> None."""
    from ..venues.states import NAME_TO_ABBR, parse_race
    info = parse_race(race)
    if info["office"] == "house":
        return info["abbr"]
    return NAME_TO_ABBR.get(info["state"] or "")


def et_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, EST).strftime("%m-%d %H:%M:%S")


# ------------------------------------------------------------------------------------------------ config
@dataclass
class Config:
    year: int = 2026
    start_et: float = 18.0                 # ET hour on election day: nothing happens before
    flatten_et: float = 35.5               # 11:30 ET on 4 Nov: flatten uncalled positions
    pre_open_sec: float = 0.0              # watch a race this long before its first poll close
    per_race_cap: float = 5000.0           # notional (price paid) per race, all legs and kinds
    total_cap: float = 90000.0
    per_cycle_budget: float = 20000.0      # notional per cycle across races
    call_level: float = 0.95
    single_call_level: float = 0.97        # when only one venue quotes the race
    call_polls: int = 2                    # consecutive polls meeting the price rule (1-minute data hid intra-minute noise)
    max_call_price: float = 0.985
    min_edge_call: float = 0.015           # buy a called leg up to fair - this
    move_pts: float = 0.05
    move_window_sec: float = 600.0
    follow_frac: float = 0.34
    min_edge_move: float = 0.04
    move_cap_frac: float = 0.5
    exit_frac: float = 0.5
    reverse_pts: float = 0.03
    hold_sec: float = 3600.0
    reversal_level: float = 0.85
    kill_disagree: float = 0.10
    kill_polls: int = 2
    kill_races: int = 6
    kill_fresh_sec: float = 600.0
    stale_sec: float = 30.0
    min_qty: int = 50
    safe_call_frac: float = 0.3            # calls within safe_call_sec of the last poll close (safe seats) may use this share of total_cap
    safe_call_sec: float = 900.0
    recycle_gap: float = 0.01              # sell a called hold once the SIG bid is within this of the outside fair (frees cash)
    ttl_sec: float = 15.0
    skip_slow_moves: bool = True           # no move trades in slow-count states (calls there may come after 4 Nov noon)
    paper: bool = True                     # assume our orders fill at the book levels we took

    def day0(self) -> float:
        y, m, d = ELECTION_DAY[self.year]
        return datetime(y, m, d, tzinfo=EST).timestamp()


# ------------------------------------------------------------------------------------------------ books
def _levels(book: dict, side: str) -> list[tuple[float, float]]:
    lv = (book or {}).get("asks" if side == "ask" else "bids") or []
    out = [(float(l["price"]), float(l["quantity"])) for l in lv if l.get("quantity")]
    return sorted(out, key=lambda x: x[0], reverse=(side == "bid"))


def _mid(book: dict) -> Optional[float]:
    a, b = _levels(book, "ask"), _levels(book, "bid")
    if a and b:
        return 0.5 * (a[0][0] + b[0][0])
    return a[0][0] if a else (b[0][0] if b else None)


def buy_ladder(books: dict, legs: dict, party: str) -> list[tuple[float, float, str, str]]:
    """Ways to buy `party` winning, cheapest first: (cost per share, shares, exchange_id, side). YES on the party's leg;
    in a two-leg race also NO on the other leg (cost 1 - bid)."""
    out = [(p, q, legs[party], "yes") for p, q in _levels(books.get(legs[party]), "ask")]
    others = [p for p in legs if p != party]
    if len(others) == 1:
        ex = legs[others[0]]
        out += [(round(1 - p, 4), q, ex, "no") for p, q in _levels(books.get(ex), "bid")]
    return sorted(out, key=lambda x: x[0])


def take(ladder: list, limit: float, budget: float, min_qty: int = 1) -> list[dict]:
    """Walk the ladder up to `limit` and `budget` notional. Returns one order per (exchange, side) at its worst price."""
    fills: dict = {}
    for cost, q, ex, side in ladder:
        if cost > limit + 1e-9 or budget < cost * min_qty:
            break
        n = int(min(q, budget / cost))
        if n <= 0:
            break
        f = fills.setdefault((ex, side), {"exchangeId": ex, "side": side, "quantity": 0, "cost": 0.0, "price": cost})
        f["quantity"] += n
        f["cost"] += n * cost
        f["price"] = max(f["price"], cost)
        budget -= n * cost
    return [f for f in fills.values() if f["quantity"] >= min_qty]


def unwind_ladder(books: dict, ex: str, side: str) -> list[tuple[float, float, str, str]]:
    """Close a held (ex, side) by buying the opposite side: held YES -> buy NO at 1 - bid; held NO -> buy YES at ask.
    Returned as (proceeds per share, shares, ex, side_to_buy), best first."""
    if side == "yes":
        return [(p, q, ex, "no") for p, q in _levels(books.get(ex), "bid")]
    return [(round(1 - p, 4), q, ex, "yes") for p, q in _levels(books.get(ex), "ask")]


# ------------------------------------------------------------------------------------------------ sleeve
class ElectionNight:
    def __init__(self, cfg: Optional[Config] = None, legs: Optional[dict] = None):
        """legs: {race: {party: exchange_id}} for SIG's races."""
        self.cfg = cfg or Config()
        self.legs = legs or {}
        self.hist: dict = collections.defaultdict(collections.deque)      # (race, party) -> deque[(ts, fair, pm, k)]
        self.sig_hist: dict = collections.defaultdict(collections.deque)  # (race, party) -> deque[(ts, sig_mid)]
        self.called: dict = {}               # race -> (party, ts, source)
        self.ext_calls: dict = {}            # race -> (party, ts, source) from on_call()
        self.disagree_n: collections.Counter = collections.Counter()
        self.dis_since: dict = {}
        self.call_n: dict = {}
        self.paused: set = set()
        self.killed: set = set()             # races whose call reversed
        self.halted_reason: Optional[str] = None
        self.pos: dict = collections.defaultdict(dict)   # race -> {(ex, side): {qty, cost, kind, party, t0, entry_mid, fair0, target}}
        self.log: list = []
        self.peak = 0.0

    # -------------------------------------------------------------- helpers
    def close_ts(self, race: str, which: str) -> Optional[float]:
        ab = race_abbr(race)
        return poll_close_ts(ab, self.cfg.year, which) if ab else None

    def watched(self, ts: float) -> list[str]:
        """Races whose first poll close has passed (minus pre_open_sec), in closing order; chamber races always."""
        out = []
        for race in self.legs:
            c = self.close_ts(race, "first")
            if c is None or ts >= c - self.cfg.pre_open_sec:
                out.append((c or 0, race))
        return [r for _, r in sorted(out)]

    def race_cost(self, race: str) -> float:
        return sum(p["cost"] for p in self.pos[race].values())

    def total_cost(self) -> float:
        return sum(self.race_cost(r) for r in self.pos)

    def on_call(self, race: str, party: str, ts: float, source: str = "AP"):
        """External race call (AP / Decision Desk feed). Overrides the price rule except when venues disagree."""
        self.ext_calls[race] = (party, ts, source)

    def on_fill(self, order: dict, qty: int, price: float):
        """Live mode (paper=False): record an actual fill of an order this sleeve emitted."""
        self._book_fill(order, qty, price)

    def _book_fill(self, o: dict, qty: int, price: float):
        race, key = o["race"], (o["exchangeId"], o["side"])
        if o["kind"] == "exit":
            held = self.pos[race].get(o["closes"])
            if held:
                frac = min(1.0, qty / max(held["qty"], 1))
                held["qty"] -= qty
                pnl = qty * price - held["cost"] * frac                      # proceeds per share = 1 - cost of the opposite side
                held["cost"] *= (1 - frac)
                self.log.append(dict(ts=o["ts"], race=race, kind="exit", pnl=pnl, why=o["reason"]))
                if held["qty"] <= 0:
                    self.pos[race].pop(o["closes"], None)
            return
        p = self.pos[race].setdefault(key, {"qty": 0, "cost": 0.0, "kind": o["kind"], "party": o["party"], "t0": o["ts"],
                                            "entry_mid": o.get("sig_mid"), "fair0": o.get("fair"), "target": o.get("target")})
        p["qty"] += qty
        p["cost"] += qty * price
        p["safe"] = p.get("safe") or bool(o.get("safe"))
        if o["kind"] == "call":
            p["kind"] = "call"

    # ------------------------------------------------------------------ rows
    @staticmethod
    def fair_of(r: dict, cfg: Config) -> tuple[Optional[float], int, float]:
        vs = [v for v in (r.get("pm"), r.get("kalshi")) if v is not None]
        if not vs:
            return None, 0, 0.0
        dis = abs(r["pm"] - r["kalshi"]) if len(vs) == 2 else 0.0
        return sum(vs) / len(vs), len(vs), dis

    def _push(self, ts: float, race: str, party: str, fair: float, r: dict, sig_mid: Optional[float]):
        h = self.hist[(race, party)]
        h.append((ts, fair, r.get("pm"), r.get("kalshi")))
        while h and ts - h[0][0] > max(self.cfg.move_window_sec, 900) + 60:
            h.popleft()
        if sig_mid is not None:
            s = self.sig_hist[(race, party)]
            s.append((ts, sig_mid))
            while s and ts - s[0][0] > self.cfg.move_window_sec + 60:
                s.popleft()

    # ----------------------------------------------------------------- cycle
    def cycle(self, ts: float, rows: list[dict], books: dict) -> list[dict]:
        """rows: [{ts, race, party, pm, kalshi}] (outside probabilities per SIG leg, one poll); books: SIG books by exchange
        id ({bids, asks} lists as in feed.books()). Returns paper orders; with cfg.paper they are booked as filled."""
        cfg = self.cfg
        if ts < cfg.day0() + cfg.start_et * 3600:
            return []
        by_race: dict = collections.defaultdict(dict)
        newest = 0.0
        for r in rows:
            if r.get("race") in self.legs:
                by_race[r["race"]][r["party"]] = r
                newest = max(newest, r.get("ts", ts))
        stale = (ts - newest) > cfg.stale_sec if rows else True
        # ---- disagreement bookkeeping (race-level), global kill
        in_dis = 0
        for race, rp in by_race.items():
            worst = max((self.fair_of(r, cfg)[2] for r in rp.values()), default=0.0)
            self.disagree_n[race] = self.disagree_n[race] + 1 if worst > cfg.kill_disagree else 0
            if self.disagree_n[race] == 1:
                self.dis_since[race] = ts
            if self.disagree_n[race] >= cfg.kill_polls:
                self.paused.add(race)
                # only fresh disagreements count toward the global kill: a thin race that disagrees all night is paused
                # on its own, many races splitting at once means one venue's data is wrong
                in_dis += ts - self.dis_since.get(race, ts) <= cfg.kill_fresh_sec
            elif self.disagree_n[race] == 0:
                self.paused.discard(race)
        self.halted_reason = ("stale outside data" if stale else
                              f"{in_dis} races in venue disagreement" if in_dis >= cfg.kill_races else None)
        orders: list[dict] = []
        cands: list[dict] = []
        flatten = ts >= cfg.day0() + cfg.flatten_et * 3600
        for race in self.watched(ts):
            rp = by_race.get(race)
            legs = self.legs[race]
            if not rp:
                continue
            fairs = {}
            for party, r in rp.items():
                f, nv, _ = self.fair_of(r, cfg)
                if f is None or party not in legs:
                    continue
                fairs[party] = (f, nv, r)
                self._push(ts, race, party, f, r, _mid(books.get(legs[party])))
            if not fairs:
                continue
            orders += self._exits(ts, race, fairs, books, flatten)
            if self.halted_reason or flatten or race in self.paused or race in self.killed:
                continue
            cands += self._entries(ts, race, fairs, books, cfg.per_cycle_budget)[0]
        # cash is the binding constraint (about 100k against 100+ callable races): fund the best expected return per
        # SUSQie first; the last order is cut to what is left
        room = min(cfg.total_cap - self.total_cost(), cfg.per_cycle_budget)
        safe_room = cfg.safe_call_frac * cfg.total_cap - sum(p["cost"] for ps in self.pos.values() for p in ps.values() if p.get("safe"))
        for o in sorted(cands, key=lambda o: -(o["fair"] - o["price_avg"]) / o["price_avg"]):
            lim = min(room, safe_room) if o.get("safe") else room
            if lim < o["price_avg"] * cfg.min_qty:
                continue
            if o["quantity"] * o["price_avg"] > lim:
                o["quantity"] = int(lim / o["price_avg"])
            room -= o["quantity"] * o["price_avg"]
            if o.get("safe"):
                safe_room -= o["quantity"] * o["price_avg"]
            orders.append(o)
        if cfg.paper:
            for o in orders:
                self._book_fill(o, o["quantity"], o["price_avg"])
        self.peak = max(self.peak, self.total_cost())
        return orders

    # --------------------------------------------------------------- calls
    def _call_party(self, ts: float, race: str, fairs: dict) -> Optional[tuple]:
        cfg = self.cfg
        last = self.close_ts(race, "last")
        if last is not None and ts < last:
            return None
        if race in self.ext_calls:
            p, t, src = self.ext_calls[race]
            if p in fairs and fairs[p][0] >= cfg.reversal_level:
                return p, src
        for party, (f, nv, r) in fairs.items():
            vals = [v for v in (r.get("pm"), r.get("kalshi")) if v is not None]
            lvl = cfg.call_level if nv >= 2 else cfg.single_call_level
            k = (race, party)
            self.call_n[k] = self.call_n.get(k, 0) + 1 if (vals and min(vals) >= lvl) else 0
            if self.call_n[k] >= cfg.call_polls:
                return party, "price"
        return None

    def _order(self, ts, race, party, f, kind, reason, fair, sig_mid, target=None, closes=None) -> dict:
        return dict(exchangeId=f["exchangeId"], side=f["side"], action="buy", quantity=f["quantity"], price=round(f["price"], 4),
                    price_avg=f["cost"] / f["quantity"], race=race, party=party, kind=kind, reason=reason, ttl_sec=self.cfg.ttl_sec,
                    ts=ts, fair=fair, sig_mid=sig_mid, target=target, closes=closes)

    def _entries(self, ts: float, race: str, fairs: dict, books: dict, budget: float) -> tuple[list, float]:
        cfg = self.cfg
        legs = self.legs[race]
        room = min(cfg.per_race_cap - self.race_cost(race), budget)
        if room <= 0:
            return [], 0.0
        call = self._call_party(ts, race, fairs)
        if call and race not in self.called:
            self.called[race] = (call[0], ts, call[1])
        if race in self.called:
            party = self.called[race][0]
            if party not in fairs:
                return [], 0.0
            fair = fairs[party][0]
            limit = min(fair - cfg.min_edge_call, cfg.max_call_price)
            fills = take(buy_ladder(books, legs, party), limit, room, cfg.min_qty)
            sm = _mid(books.get(legs[party]))
            out = [self._order(ts, race, party, f, "call", f"called {party} ({self.called[race][2]}), fair {fair:.3f}", fair, sm) for f in fills]
            last = self.close_ts(race, "last")
            safe = last is not None and self.called[race][1] - last <= cfg.safe_call_sec
            for o in out:
                o["safe"] = safe
            return out, sum(f["cost"] for f in fills)
        # ---- move trades (pre-call)
        first = self.close_ts(race, "first")
        if first is not None and ts < first:
            return [], 0.0
        if cfg.skip_slow_moves and race_abbr(race) in SLOW_COUNT:
            return [], 0.0
        if any(p["kind"] == "move" for p in self.pos[race].values()):
            return [], 0.0
        for party, (fair, nv, r) in fairs.items():
            if self.fair_of(r, cfg)[2] > cfg.kill_disagree:
                continue                                   # disagreeing now: no entry even before the race is paused
            h = self.hist[(race, party)]
            ref = next((x for x in h if ts - x[0] <= cfg.move_window_sec), None)
            if ref is None or ref[0] >= ts:
                continue
            d = fair - ref[1]
            if d < cfg.move_pts:
                continue
            # every venue present moved the same way by at least half the threshold
            if any(v is not None and v0 is not None and (v - v0) < cfg.move_pts / 2 for v, v0 in ((r.get("pm"), ref[2]), (r.get("kalshi"), ref[3]))):
                continue
            sh = self.sig_hist[(race, party)]
            s0 = next((x[1] for x in sh if ts - x[0] <= cfg.move_window_sec), None)
            sm = _mid(books.get(legs[party]))
            if sm is None or s0 is None or (sm - s0) >= cfg.follow_frac * d:
                continue
            limit = fair - cfg.min_edge_move
            cap = min(room, cfg.move_cap_frac * cfg.per_race_cap - self.race_cost(race))
            fills = take(buy_ladder(books, legs, party), limit, cap, cfg.min_qty)
            target = sm + cfg.exit_frac * (fair - sm)
            out = [self._order(ts, race, party, f, "move", f"outside {ref[1]:.3f}->{fair:.3f}, sig {s0:.3f}->{sm:.3f}", fair, sm, target) for f in fills]
            return out, sum(f["cost"] for f in fills)
        return [], 0.0

    # --------------------------------------------------------------- exits
    def _exits(self, ts: float, race: str, fairs: dict, books: dict, flatten: bool) -> list[dict]:
        cfg = self.cfg
        out = []
        legs = self.legs[race]
        called = self.called.get(race)
        for key, p in list(self.pos[race].items()):
            ex, side = key
            party = p["party"]
            fair = fairs.get(party, (None,))[0]
            why = None
            if p["kind"] == "call":
                vals = [v for v in (fairs.get(party, (0, 0, {}))[2].get("pm"), fairs.get(party, (0, 0, {}))[2].get("kalshi")) if v is not None]
                low = bool(vals) and max(vals) < cfg.reversal_level
                p["below"] = p.get("below", 0) + 1 if low else 0
                bid = _levels(books.get(ex), "bid") if side == "yes" else [(round(1 - q, 4), n) for q, n in _levels(books.get(ex), "ask")]
                if p["below"] >= cfg.kill_polls:
                    why = f"call reversal: outside {max(vals):.3f} < {cfg.reversal_level}"
                    self.killed.add(race)
                    self.called.pop(race, None)
                elif cfg.recycle_gap > 0 and bid and fair is not None and bid[0][0] >= fair - cfg.recycle_gap and bid[0][1] >= p["qty"]:
                    why = f"recycle: sig bid {bid[0][0]:.3f} vs fair {fair:.3f}"
            else:
                sm = _mid(books.get(legs[party]))
                if called and called[0] == party:
                    p["kind"] = "call"
                    continue
                if called and called[0] != party:
                    why = f"race called for {called[0]}"
                elif flatten:
                    why = "flatten before close"
                elif race in self.paused:
                    why = "venues disagree"
                elif sm is not None and p["target"] is not None and sm >= p["target"]:
                    why = f"half convergence: sig {sm:.3f} >= {p['target']:.3f}"
                elif fair is not None and p["fair0"] is not None and fair <= p["fair0"] - cfg.reverse_pts:
                    why = f"outside reversed {p['fair0']:.3f}->{fair:.3f}"
                elif ts - p["t0"] >= cfg.hold_sec:
                    why = "time stop"
            if not why:
                continue
            # proceeds ladder: closing a YES leg buys NO at 1 - bid (we receive the bid), and vice versa
            lad = unwind_ladder(books, ex, side)
            left = p["qty"]
            fills: dict = {}
            for proceeds, q, ex2, side2 in lad:
                n = int(min(q, left))
                if n <= 0:
                    break
                f = fills.setdefault((ex2, side2), {"exchangeId": ex2, "side": side2, "quantity": 0, "cost": 0.0, "price": 1.0, "proc": 0.0})
                f["quantity"] += n
                f["proc"] += n * proceeds
                f["cost"] += n * (1 - proceeds)
                f["price"] = max(f["price"] if f["quantity"] > n else 0.0, 1 - proceeds)
                left -= n
            for f in fills.values():
                o = self._order(ts, race, party, f, "exit", why, fair, None, closes=key)
                o["price_avg"] = f["proc"] / f["quantity"]          # booked as proceeds per share of the closed position
                out.append(o)
        return out

    def settle(self, outcomes: dict) -> dict:
        """outcomes: {race: winning party}. P&L of what is still held, plus realised exits."""
        res = collections.defaultdict(float)
        for e in self.log:
            res[e["race"]] += e["pnl"]
        for race, ps in self.pos.items():
            w = outcomes.get(race)
            for (ex, side), p in ps.items():
                if p["qty"] <= 0 or w is None:
                    continue
                won = (p["party"] == w)        # YES on the party's leg or NO on the other leg: both pay iff the party wins
                res[race] += (p["qty"] if won else 0.0) - p["cost"]
        return dict(res)


# ------------------------------------------------------------------------------------------------ live outside poller
class OutsideStream:
    """Polymarket books + Kalshi quotes for SIG legs via data/race_map.json (the direction sleeve's mapping). One poll is
    one Polymarket POST /books and 1-3 parallel Kalshi GET /markets calls, about 0.4-1.0 s. No SIG reads."""

    def __init__(self, map_path: Optional[str] = None):
        from ..venues.http import Http
        from ..venues.kalshi import Kalshi
        from ..venues.polymarket import Polymarket
        self.map = json.load(open(map_path or os.path.join(HERE, "data", "race_map.json")))["races"]
        http = Http(user_agent="sigbot-election/0.1", timeout=4.0)
        self.pm, self.ks = Polymarket(http), Kalshi(http, os.path.join(HERE, "data"))
        self.latency: dict = {}

    def poll(self, races: Optional[list] = None) -> list[dict]:
        from concurrent.futures import ThreadPoolExecutor
        from ..direction import DirectionTrader
        races = [r for r in (races or list(self.map)) if r in self.map]
        toks = sorted({l["token"] for r in races for ls in self.map[r].get("polymarket", {}).get("legs", {}).values() for l in ls})
        tks = sorted({l["ticker"] for r in races for ls in self.map[r].get("kalshi", {}).get("legs", {}).values() for l in ls})
        t0 = time.time()

        def safe(fn, arg):
            try:
                return fn(arg) if arg else {}
            except Exception:
                return {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            fp, fk = ex.submit(safe, self.pm.books, toks), ex.submit(safe, self.ks.quotes, tks)
            pmq, kq = fp.result(), fk.result()
        ts = time.time()
        self.latency = {"ms": round((ts - t0) * 1000), "pm": len(pmq), "kalshi": len(kq)}
        rows = []
        for race in races:
            e = self.map[race]
            pmp, _ = DirectionTrader.venue_probs(e.get("polymarket", {}).get("legs", {}), pmq, "token", 0.10)
            kp, _ = DirectionTrader.venue_probs(e.get("kalshi", {}).get("legs", {}), kq, "ticker", 0.10)
            for party in set(pmp) | set(kp):
                rows.append({"ts": ts, "race": race, "party": party, "pm": pmp.get(party), "kalshi": kp.get(party)})
        return rows


def universe_legs(universe=None) -> dict:
    """{race: {party: exchange_id}} from a Universe or data/markets.json."""
    if universe is not None:
        return {name: {p: l.exchange_id for p, l in race.legs.items()} for name, race in universe.races.items()}
    out: dict = {}
    import re
    for m in json.load(open(os.path.join(HERE, "data", "markets.json"))):
        mm = re.match(r"Will the (Democratic|Republican|Independent) Party win the (.+)\?", m["title"])
        if mm:
            out.setdefault(mm.group(2), {})[mm.group(1)[0]] = str(m["exchanges"][0]["id"])
    return out


# ------------------------------------------------------------------------------------------------ lagging-DMM model of SIG
LADDER = [2000, 2000, 3000, 5000, 5000, 10000, 10000, 10000, 10000, 10000]   # shares per tick from the touch (about 67k in 10 ticks)

SCENARIOS = {
    # a: share of an outside move the DMM follows on its fast clock, d: dead time, T: fast time constant, T2: the rest;
    # kappa: SIG tracks 0.5 + kappa (fair - 0.5) (today's tail compression ~0.8); refill: a swept stale level is re-posted
    "base":   dict(a=0.45, d=120, T=600, T2=86400, kappa=0.8, refill=True, pull_pts=None),
    "fast":   dict(a=0.9, d=30, T=60, T2=3600, kappa=0.8, refill=True, pull_pts=None),
    "slow":   dict(a=0.3, d=300, T=1200, T2=86400, kappa=0.8, refill=True, pull_pts=None),
    "kappa1": dict(a=0.45, d=120, T=600, T2=86400, kappa=1.0, refill=True, pull_pts=None),
    "norefill": dict(a=0.45, d=120, T=600, T2=86400, kappa=0.8, refill=False, pull_pts=None),
    "pull":   dict(a=0.9, d=30, T=60, T2=3600, kappa=0.8, refill=False, pull_pts=0.03),
    "fast_k1": dict(a=0.9, d=30, T=60, T2=3600, kappa=1.0, refill=True, pull_pts=None),
    "instant": dict(a=1.0, d=0, T=1, T2=3600, kappa=1.0, refill=True, pull_pts=None),   # sanity: no lag, no compression
}


class LaggingVenue:
    """Synthetic SIG books for one two-leg race: the DMM's D mid follows a compressed outside fair through the lag
    kernel F(tau) = a (1 - exp(-(tau - d)/T)) + (1 - a)(1 - exp(-tau/T2)), fitted to the measured follow curve
    (0% at 1-2 min, 9% at 3, 29% at 10, 44% at 30, 53% at 60 min; base gives 4/25/43/47%). `pull_pts`: the DMM pulls its
    quotes for 10 minutes after an outside move of that size, then requotes at the new level."""

    def __init__(self, legs: dict, f0: float, ts0: float, a=0.45, d=120, T=600, T2=86400, kappa=0.8, refill=True, pull_pts=None, spread_ticks=1):
        self.legs, self.a, self.d, self.T, self.T2, self.kappa = legs, a, d, T, T2, kappa
        self.refill, self.pull_pts, self.spread = refill, pull_pts, spread_ticks
        self.c0 = self._c(f0)
        self.steps: list = []                 # (ts, delta compressed fair)
        self.last_c, self.last_f = self.c0, f0
        self.consumed: dict = {}              # (ex, side, price) -> shares taken
        self.pulled_until = 0.0
        self.ts0 = ts0

    def _c(self, f: float) -> float:
        return 0.5 + self.kappa * (f - 0.5)

    def F(self, tau: float) -> float:
        if self.d <= 0 and self.a >= 1 and tau >= 0:
            return 1.0                                     # "instant": the DMM has repriced before we can act
        if tau <= 0:
            return 0.0
        fast = self.a * (1 - math.exp(-(tau - self.d) / self.T)) if tau > self.d else 0.0
        return fast + (1 - self.a) * (1 - math.exp(-tau / self.T2))

    def update(self, ts: float, f_d: float):
        c = self._c(f_d)
        if abs(c - self.last_c) > 1e-9:
            self.steps.append((ts, c - self.last_c))
            if self.pull_pts and abs(f_d - self.last_f) >= self.pull_pts:
                self.pulled_until = ts + 600
            self.last_c = c
        self.last_f = f_d

    def mid_d(self, ts: float) -> float:
        if self.pull_pts and ts < self.pulled_until + 1:
            # after a pull the DMM requotes fully at the new level
            pass
        m = self.c0 + sum(dc * self.F(ts - t) for t, dc in self.steps)
        if self.pull_pts and self.steps and ts >= self.pulled_until and self.pulled_until > 0:
            m = self.c0 + sum(dc * (1.0 if t <= self.pulled_until - 600 else self.F(ts - t)) for t, dc in self.steps)
        return min(0.995 - TICK, max(0.005 + TICK, m))

    def books(self, ts: float) -> dict:
        if self.pull_pts and ts < self.pulled_until:
            return {ex: {"bids": [], "asks": []} for ex in self.legs.values()}
        md = self.mid_d(ts)
        out = {}
        for party, ex in self.legs.items():
            m = md if party == "D" else 1 - md
            bid = math.floor((m - self.spread * TICK / 2) / TICK + 1e-9) * TICK
            ask = bid + self.spread * TICK
            asks, bids = [], []
            for i, q in enumerate(LADDER):
                pa, pb = round(ask + i * TICK, 4), round(bid - i * TICK, 4)
                if pa < 1:
                    asks.append({"price": pa, "quantity": max(0, q - self.consumed.get((ex, "ask", pa), 0))})
                if pb > 0:
                    bids.append({"price": pb, "quantity": max(0, q - self.consumed.get((ex, "bid", pb), 0))})
            out[ex] = {"asks": [l for l in asks if l["quantity"] > 0], "bids": [l for l in bids if l["quantity"] > 0]}
        return out

    def fill(self, orders: list[dict], books: dict):
        """Consume the levels our orders took (paper). With refill the DMM re-posts them before the next poll."""
        if self.refill:
            return
        for o in orders:
            ex = o["exchangeId"]
            if o["side"] == "yes":
                lv, side = _levels(books.get(ex), "ask"), "ask"
            else:
                lv, side = [(round(1 - p, 4), q) for p, q in _levels(books.get(ex), "bid")], "bid"
            left = o["quantity"]
            for p, q in sorted(lv):
                if left <= 0 or p > o["price"] + 1e-9:
                    break
                n = min(q, left)
                price_key = p if side == "ask" else round(1 - p, 4)
                self.consumed[(ex, side, price_key)] = self.consumed.get((ex, side, price_key), 0) + n
                left -= n


# ------------------------------------------------------------------------------------------------ 2024 replay
PRES_ABBR = {"congressional-district-1st-maine": "ME", "congressional-district-2nd-maine": "ME", "congressional-district-1st-nebraska": "NE",
             "congressional-district-2nd-nebraska": "NE", "congressional-district-3rd-nebraska": "NE", "washington-dc": "DC"}


def race_key_2024(venue: str, office: str, slug: str) -> Optional[tuple]:
    """(office, ST, district or '') shared by the Polymarket slug and the Kalshi ticker of one 2024 race."""
    import re
    from ..venues.states import NAME_TO_ABBR
    if venue == "kalshi":
        m = re.match(r"^(PRESPARTY|SENATE|GOVPARTY|HOUSE)([A-Z]{2})([A-Z0-9]*)-24", slug)
        if not m:
            return None
        kind, st, rest = m.groups()
        off = {"PRESPARTY": "president", "SENATE": "senate", "GOVPARTY": "governor", "HOUSE": "house"}[kind]
        if off == "senate" and rest == "S":          # SENATENES-24: Nebraska special
            return (off, st, "special")
        if off == "house" and rest == "AL":
            rest = "1"
        return (off, st, rest.lstrip("0") if rest else "")
    s = slug
    if office == "president":
        base = s.replace("-presidential-election-winner", "").replace("-2024", "")
        if base in PRES_ABBR:
            dist = {"1st": "1", "2nd": "2", "3rd": "3"}.get(base.split("-")[2], "") if base.startswith("congressional") else ""
            return ("president", PRES_ABBR[base], dist)
        name = " ".join(w.capitalize() for w in base.split("-"))
        return ("president", NAME_TO_ABBR.get(name), "") if NAME_TO_ABBR.get(name) else None
    if office in ("senate", "governor"):
        base = s.replace("-us-senate-election-winner", "").replace("-governor-election-winner", "")
        name = " ".join(w.capitalize() for w in base.split("-"))
        return (office, NAME_TO_ABBR.get(name), "") if NAME_TO_ABBR.get(name) else None
    m = re.match(r"^([a-z]{2})-(\d+)-", s)
    return ("house", m.group(1).upper(), m.group(2).lstrip("0")) if m else None


def load_2024(path: Optional[str] = None, max_spread: float = 0.10, stale_sec: float = 3600) -> dict:
    """{race_key: {"winner": 'D'|'R', "pm": [(ts, pD)], "kalshi": [(ts, pD)]}} with pD from the D leg and 1 - R leg averaged."""
    path = path or os.path.join(DATA, "paths_2024.csv")
    raw: dict = collections.defaultdict(lambda: collections.defaultdict(dict))   # (key, venue) -> party -> {ts: p}
    winners: dict = {}
    pm_events: dict = collections.defaultdict(set)
    for r in csv.DictReader(open(path)):
        key = race_key_2024(r["venue"], r["office"], r["slug_or_ticker"] if r["venue"] == "polymarket" else r["event"])
        if not key or not r["price"] or r["party"] not in ("D", "R"):
            continue
        if r["venue"] == "kalshi" and r["bid"] and r["ask"] and float(r["ask"]) - float(r["bid"]) > max_spread and 0.03 < float(r["price"]) < 0.97:
            continue                                   # wide Kalshi books: the mid is noise (live poller drops these too)
        if r["venue"] == "polymarket":
            pm_events[key].add(r["slug_or_ticker"])
        raw[(key, r["venue"])][r["party"]][int(r["ts"])] = float(r["price"])
        if r["outcome"] in ("0", "1") and r["outcome"] == "1":
            winners[key] = r["party"]
    out: dict = {}
    for (key, venue), parties in raw.items():
        if len(pm_events.get(key, ())) > 1:
            continue                                   # two House matchups mapped to one district (CA-13 slug clash): skip
        ts_all = sorted(set().union(*[set(v) for v in parties.values()]))
        last = {}
        series = []
        for t in ts_all:
            for p in ("D", "R"):
                if t in parties.get(p, {}):
                    last[p] = parties[p][t]
            vals = []
            if "D" in last:
                vals.append(last["D"])
            if "R" in last:
                vals.append(1 - last["R"])
            if vals:
                series.append((t, sum(vals) / len(vals)))
        # stale last-trade prices (illiquid Polymarket legs sat at 0.18 for hours while Kalshi was at 0.02): a mid-range
        # value unchanged for stale_sec is dropped, as the live poller's spread/depth filter would drop the book
        clean, chg, prev = [], None, None
        for t, p in series:
            if prev is None or abs(p - prev) > 1e-9:
                chg, prev = t, p
            clean.append((t, p if (t - chg < stale_sec or not 0.03 < p < 0.97) else None))
        out.setdefault(key, {"winner": winners.get(key)})["pm" if venue == "polymarket" else "kalshi"] = clean
    return {k: v for k, v in out.items() if v.get("winner")}


def _at(series: list, t: float) -> Optional[float]:
    """Last value at or before t (series sorted by ts)."""
    import bisect
    i = bisect.bisect_right(series, t, key=lambda x: x[0]) - 1
    return series[i][1] if i >= 0 else None


def simulate(scenario: str = "base", cap: float = 10000.0, step: int = 60, offices=None, cfg_over: Optional[dict] = None,
             data: Optional[dict] = None, total: float = 1e12) -> dict:
    """Replay 2024 election night: every `step` seconds the sleeve sees both venues' D/R probabilities and SIG books from a
    LaggingVenue per race, trades, and settles on the actual winner. Positions not called are flattened at 12:00 ET the
    next day (SIG's 2026 close is 4 Nov 12:00 ET)."""
    data = data if data is not None else load_2024()
    sc = SCENARIOS[scenario]
    cfg = Config(year=2024, per_race_cap=cap, total_cap=total, per_cycle_budget=1e12, **(cfg_over or {}))
    t_start = cfg.day0() + cfg.start_et * 3600
    t_end = cfg.day0() + 36 * 3600
    races, venues = {}, {}
    for key, d in data.items():
        if offices and key[0] not in offices:
            continue
        name = f"{key[1]}{('-' + key[2]) if key[2] else ''} {key[0]}"
        legs = {"D": name + "/D", "R": name + "/R"}
        f0s = [v for v in (_at(d.get("pm", []), t_start), _at(d.get("kalshi", []), t_start)) if v is not None]
        if not f0s:
            continue
        races[name] = (key, d, legs)
        venues[name] = LaggingVenue(legs, sum(f0s) / len(f0s), t_start, **sc)
    en = ElectionNight(cfg, {n: legs for n, (_, _, legs) in races.items()})
    en.close_ts = lambda race, which: poll_close_ts(races[race][0][1], 2024, which)   # postal code from the 2024 key
    _ab = {n: k[1] for n, (k, _, _) in races.items()}
    global race_abbr
    orig = race_abbr
    race_abbr = lambda r: _ab.get(r) if r in _ab else orig(r)
    try:
        orders_all = []
        t = t_start
        while t <= t_end:
            rows, books = [], {}
            for name, (key, d, legs) in races.items():
                pm, k = _at(d.get("pm", []), t), _at(d.get("kalshi", []), t)
                if pm is None and k is None:
                    continue
                fv = [v for v in (pm, k) if v is not None]
                venues[name].update(t, sum(fv) / len(fv))
                rows.append({"ts": t, "race": name, "party": "D", "pm": pm, "kalshi": k})
                rows.append({"ts": t, "race": name, "party": "R", "pm": None if pm is None else 1 - pm, "kalshi": None if k is None else 1 - k})
                books.update(venues[name].books(t))
            orders = en.cycle(t, rows, books)
            for o in orders:
                venues[o["race"]].fill([o], books)
            orders_all += orders
            t += step
        pnl = en.settle({n: d["winner"] for n, (_, d, _) in races.items()})
    finally:
        race_abbr = orig
    by_kind = collections.defaultdict(lambda: {"orders": 0, "cost": 0.0})
    for o in orders_all:
        by_kind[o["kind"]]["orders"] += 1
        by_kind[o["kind"]]["cost"] += o["quantity"] * o["price_avg"]
    per_race = []
    for name, (key, d, legs) in races.items():
        c = en.called.get(name)
        cost = sum(o["quantity"] * o["price_avg"] for o in orders_all if o["race"] == name and o["kind"] != "exit")
        close = poll_close_ts(key[1], 2024, "last")
        fv = [v for v in (_at(d.get("pm", []), close), _at(d.get("kalshi", []), close)) if v is not None] if close else []
        pw = (sum(fv) / len(fv) if d["winner"] == "D" else 1 - sum(fv) / len(fv)) if fv else None
        cat = "no data" if pw is None else ("safe at close" if pw >= 0.95 else ("called later" if c else "not called"))
        per_race.append(dict(race=name, office=key[0], winner=d["winner"], called=c[0] if c else None, category=cat,
                             called_et=et_str(c[1]) if c else None, cost=round(cost), pnl=round(pnl.get(name, 0.0)),
                             killed=name in en.killed))
    traded = [r for r in per_race if r["cost"] > 0]
    tot = sum(r["pnl"] for r in per_race)
    return {"scenario": scenario, "params": sc, "cap": cap, "total": total, "peak_held": round(en.peak), "races": len(races), "traded": len(traded),
            "wrong_calls": sum(1 for r in per_race if r["called"] and r["called"] != r["winner"]),
            "killed": sum(1 for r in per_race if r["killed"]), "pnl": round(tot), "cost": round(sum(r["cost"] for r in per_race)),
            "worst_race": min((r["pnl"] for r in per_race), default=0), "by_kind": {k: {"orders": v["orders"], "cost": round(v["cost"])} for k, v in by_kind.items()},
            "exits": collections.Counter(" ".join(e["why"].split(":")[0].split()[:2]) for e in en.log),
            "per_race": sorted(per_race, key=lambda r: -r["pnl"])}


# ------------------------------------------------------------------------------------------------ CLI helpers
def print_clock():
    legs = universe_legs()
    rows = []
    for race in legs:
        ab = race_abbr(race)
        if not ab:
            rows.append((99, race, "-", "-", ""))
            continue
        f, l = POLL_CLOSE_ET[ab]
        rows.append((l, race, f"{int(f) % 24:02d}:{int(f % 1 * 60):02d}", f"{int(l) % 24:02d}:{int(l % 1 * 60):02d}", "slow count" if ab in SLOW_COUNT else ""))
    rows.sort()
    print("| last close ET | first close ET | race | note |\n|---|---|---|---|")
    for l, race, fs, ls, note in rows:
        print(f"| {ls} | {fs} | {race} | {note} |")


def record(minutes: float, every: float, path: Optional[str] = None):
    path = path or os.path.join(DATA, f"stream_{datetime.now(EST).strftime('%Y%m%d')}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    st = OutsideStream()
    end = time.time() + minutes * 60
    n, lat = 0, []
    with open(path, "a") as f:
        while time.time() < end:
            t0 = time.time()
            rows = st.poll()
            lat.append(st.latency["ms"])
            for r in rows:
                f.write(json.dumps(r) + "\n")
            n += 1
            time.sleep(max(0.0, every - (time.time() - t0)))
    lat.sort()
    print(f"{n} polls -> {path}; poll latency p50 {lat[len(lat) // 2]} ms, p90 {lat[int(len(lat) * 0.9)]} ms; last poll {st.latency}")


def replay(path: str, cap: float, scenario: str = "base") -> dict:
    """Dry run on a recorded outside stream. SIG books: lines with a 'sig_books' key, else the lagging-DMM model."""
    legs = universe_legs()
    polls = collections.defaultdict(list)
    sig_books: dict = {}
    for line in open(path):
        r = json.loads(line)
        if "sig_books" in r:
            sig_books[round(r["ts"])] = r["sig_books"]
        else:
            polls[round(r["ts"], 1)].append(r)
    if not polls:
        return {"polls": 0}
    ts_sorted = sorted(polls)
    # a recorded stream from before 3 Nov runs as if it were election night: shift time to 19:00 ET plus offset
    cfg = Config(per_race_cap=cap)
    shift = 0.0
    if ts_sorted[0] < cfg.day0() + cfg.start_et * 3600:
        shift = cfg.day0() + 19 * 3600 - ts_sorted[0]
    en = ElectionNight(cfg, legs)
    venues: dict = {}
    orders_all = []
    for ts in ts_sorted:
        rows = [dict(r, ts=r["ts"] + shift) for r in polls[ts]]
        books = sig_books.get(round(ts)) or {}
        if not books:
            for r in rows:
                if r["race"] in legs and r["party"] == "D" and len(legs[r["race"]]) == 2:
                    f, _, _ = ElectionNight.fair_of(r, cfg)
                    if f is None:
                        continue
                    v = venues.setdefault(r["race"], LaggingVenue(legs[r["race"]], f, ts + shift, **SCENARIOS[scenario]))
                    v.update(ts + shift, f)
                    books.update(v.books(ts + shift))
        orders_all += en.cycle(ts + shift, rows, books)
    return {"polls": len(ts_sorted), "orders": len(orders_all), "called": {r: c[0] for r, c in en.called.items()},
            "paused": sorted(en.paused), "halted": en.halted_reason, "open_cost": round(en.total_cost()),
            "sample": [{k: o[k] for k in ("race", "kind", "side", "quantity", "price", "reason")} for o in orders_all[:10]]}


# ------------------------------------------------------------------------------------------------ self-test
def selftest() -> None:
    cfg = Config(year=2026, per_race_cap=10000, total_cap=15000, safe_call_frac=1.0)
    legs = {"Georgia Senate": {"D": "g1", "R": "g2"}, "Ohio Senate": {"D": "o1", "R": "o2"}, "Kentucky Senate": {"D": "k1", "R": "k2"}}
    en = ElectionNight(cfg, legs)
    day = cfg.day0()
    ga_close = poll_close_ts("GA", 2026)
    assert ga_close == day + 19 * 3600 and poll_close_ts("AK", 2026, "last") == day + 25 * 3600
    assert race_abbr("Georgia Senate") == "GA" and race_abbr("AZ-01 House race") == "AZ" and race_abbr("U.S. Senate") is None
    assert en.watched(day + 18.5 * 3600) == ["Kentucky Senate"]
    venues = {r: LaggingVenue(l, 0.6, day + 18 * 3600, **SCENARIOS["base"]) for r, l in legs.items()}

    def step(t, fairs, kfairs=None):
        rows, books = [], {}
        for r, f in fairs.items():
            k = (kfairs or {}).get(r, f)
            venues[r].update(t, (f + k) / 2)
            books.update(venues[r].books(t))
            rows += [{"ts": t, "race": r, "party": "D", "pm": f, "kalshi": k}, {"ts": t, "race": r, "party": "R", "pm": 1 - f, "kalshi": 1 - k}]
        return en.cycle(t, rows, books)

    # nothing before 18:00 ET; nothing in GA before its polls close even if the price runs
    assert step(day + 17 * 3600, {"Georgia Senate": 0.6}) == []
    t = day + 18.9 * 3600
    o = step(t, {"Georgia Senate": 0.97, "Ohio Senate": 0.6, "Kentucky Senate": 0.6})
    assert not any(x["race"] == "Georgia Senate" and x["kind"] == "call" for x in o), "call before last poll close"
    # GA called after 19:00: buy D up to fair - 1.5 pts, within the race cap, cheapest instruments first
    t = ga_close + 60
    assert not [x for x in step(t - 30, {"Georgia Senate": 0.985, "Ohio Senate": 0.6, "Kentucky Senate": 0.6}) if x["race"] == "Georgia Senate"]
    o = step(t, {"Georgia Senate": 0.985, "Ohio Senate": 0.6, "Kentucky Senate": 0.6})   # second poll at call level
    ga = [x for x in o if x["race"] == "Georgia Senate"]
    assert ga and all(x["kind"] == "call" and x["price"] <= 0.985 - 0.015 + 1e-9 for x in ga), ga
    assert en.race_cost("Georgia Senate") <= 10000 + 1e-6 and en.called["Georgia Senate"][0] == "D"
    spent = en.race_cost("Georgia Senate")
    assert spent > 9000, spent                            # the lagging DMM still quotes ~0.69: the cap binds, not depth
    # OH: a 10-point outside move after 19:30 that SIG has not followed -> a move trade, capped at half the race cap
    t = poll_close_ts("OH", 2026) + 60
    step(t, {"Georgia Senate": 0.99, "Ohio Senate": 0.6, "Kentucky Senate": 0.6})
    o = step(t + 240, {"Georgia Senate": 0.99, "Ohio Senate": 0.72, "Kentucky Senate": 0.6})
    oh = [x for x in o if x["race"] == "Ohio Senate"]
    assert oh and oh[0]["kind"] == "move" and en.race_cost("Ohio Senate") <= 5000 + 1e-6, oh
    assert en.total_cost() <= 15000 + 1e-6               # total cap binds across races
    # half convergence exit once the DMM has caught up
    out = []
    for i in range(1, 70):
        out += step(t + 240 + 60 * i, {"Georgia Senate": 0.99, "Ohio Senate": 0.72, "Kentucky Senate": 0.6})
        if any(x["race"] == "Ohio Senate" and x["kind"] == "exit" for x in out):
            break
    ex = [x for x in out if x["race"] == "Ohio Senate" and x["kind"] == "exit"]
    assert ex and ex[0]["reason"].startswith(("half convergence", "time stop")), ex
    assert not en.pos["Ohio Senate"], en.pos["Ohio Senate"]
    # kill: KY venues disagree by 12 pts on two polls -> paused, no entries even after a call-level price on one venue
    t2 = t + 4000
    step(t2, {"Kentucky Senate": 0.97}, {"Kentucky Senate": 0.85})
    o = step(t2 + 5, {"Kentucky Senate": 0.97}, {"Kentucky Senate": 0.85})
    assert "Kentucky Senate" in en.paused and not [x for x in o if x["race"] == "Kentucky Senate"]
    # call reversal: GA outside falls below 0.85 on both venues -> exit the called hold, race killed
    step(t2 + 10, {"Georgia Senate": 0.6})
    o = step(t2 + 15, {"Georgia Senate": 0.6})
    assert any(x["race"] == "Georgia Senate" and x["kind"] == "exit" for x in o) and "Georgia Senate" in en.killed
    # stale data halts entries
    en2 = ElectionNight(cfg, legs)
    assert en2.cycle(ga_close + 100, [{"ts": ga_close, "race": "Georgia Senate", "party": "D", "pm": 0.99, "kalshi": 0.99}], {}) == []
    assert en2.halted_reason == "stale outside data"
    # safe-seat calls (within 15 min of the close) are limited to safe_call_frac of the total cap
    en4 = ElectionNight(Config(per_race_cap=10000, total_cap=15000, safe_call_frac=0.3), {"Georgia Senate": legs["Georgia Senate"]})
    v4 = LaggingVenue(legs["Georgia Senate"], 0.9, day + 18 * 3600, **SCENARIOS["base"])
    rows4 = [{"ts": ga_close + 60, "race": "Georgia Senate", "party": "D", "pm": 0.985, "kalshi": 0.985},
             {"ts": ga_close + 60, "race": "Georgia Senate", "party": "R", "pm": 0.015, "kalshi": 0.015}]
    en4.cycle(ga_close + 60, rows4, v4.books(ga_close + 60))
    en4.cycle(ga_close + 64, [dict(r, ts=ga_close + 64) for r in rows4], v4.books(ga_close + 64))
    assert 4000 < en4.race_cost("Georgia Senate") <= 4500 + 1e-6, en4.race_cost("Georgia Senate")
    # ladder helper: NO on the other leg counts as buying the party at 1 - bid
    lad = buy_ladder({"a": {"asks": [{"price": 0.7, "quantity": 10}]}, "b": {"bids": [{"price": 0.32, "quantity": 5}]}}, {"D": "a", "R": "b"}, "D")
    assert lad[0][:4] == (0.68, 5.0, "b", "no") and lad[1][:4] == (0.7, 10.0, "a", "yes")
    # settlement: NO on the loser pays like YES on the winner
    en3 = ElectionNight(cfg, {"X": {"D": "a", "R": "b"}})
    en3._book_fill(dict(race="X", exchangeId="b", side="no", kind="call", party="D", ts=0), 100, 0.68)
    assert abs(en3.settle({"X": "D"})["X"] - 32.0) < 1e-6 and abs(en3.settle({"X": "R"})["X"] + 68.0) < 1e-6
    # lag kernel matches the measured follow curve within a few points
    lv = LaggingVenue({"D": "a", "R": "b"}, 0.5, 0, **SCENARIOS["base"])
    assert lv.F(60) < 0.01 and 0.02 < lv.F(180) < 0.12 and 0.2 < lv.F(600) < 0.35 and 0.38 < lv.F(1800) < 0.5
    print("election_night selftest: OK")


def main(argv=None):
    ap = argparse.ArgumentParser(description="election-night sleeve (paper): clock, recorder, replay, 2024 simulation")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--clock", action="store_true")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--scenario", default="base")
    ap.add_argument("--cap", type=float, default=10000.0)
    ap.add_argument("--total", type=float, default=1e12, help="total cap (notional held at once); default unlimited")
    ap.add_argument("--offices", default=None, help="comma list: president,senate,governor,house")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--every", type=float, default=4.0)
    ap.add_argument("--replay", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
    if a.clock:
        print_clock()
    if a.record:
        record(a.minutes, a.every)
    if a.replay:
        print(json.dumps(replay(a.replay, a.cap, a.scenario), indent=1, default=str))
    if a.simulate:
        offices = a.offices.split(",") if a.offices else None
        data = load_2024()
        for sc in (list(SCENARIOS) if a.scenario == "all" else [a.scenario]):
            res = simulate(sc, a.cap, offices=offices, data=data, total=a.total)
            if a.json:
                print(json.dumps(res, indent=1, default=str))
                continue
            print(f"\n== scenario {sc} {res['params']}  cap {a.cap:,.0f}/race")
            print(f"peak held {res['peak_held']:,}; races {res['races']}, traded {res['traded']}, cost {res['cost']:,}, P&L {res['pnl']:+,}, worst race {res['worst_race']:+,}, "
                  f"wrong calls {res['wrong_calls']}, call reversals {res['killed']}, by kind {res['by_kind']}, exits {dict(res['exits'])}")
            off = collections.defaultdict(lambda: [0, 0, 0])
            for r in res["per_race"]:
                off[r["office"]][0] += 1 if r["cost"] else 0
                off[r["office"]][1] += r["cost"]
                off[r["office"]][2] += r["pnl"]
            print("by office: " + ", ".join(f"{k} {v[0]} traded cost {v[1]:,} pnl {v[2]:+,}" for k, v in off.items()))
            cat = collections.defaultdict(lambda: [0, 0, 0])
            for r in res["per_race"]:
                cat[r["category"]][0] += 1
                cat[r["category"]][1] += 1 if r["cost"] else 0
                cat[r["category"]][2] += r["pnl"]
            print("by state of the race at poll close: " + ", ".join(f"{k} {v[0]} races, {v[1]} traded, pnl {v[2]:+,}" for k, v in cat.items()))
            for r in res["per_race"][:8] + res["per_race"][-5:]:
                print(f"  {r['race']:22s} winner {r['winner']} called {r['called'] or '-'} at {r['called_et'] or '-':15s} cost {r['cost']:>7,} pnl {r['pnl']:>+7,}")
    if not (a.selftest or a.clock or a.record or a.replay or a.simulate):
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
