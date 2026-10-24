"""Wind-down (backlog item 000, Sizhe 23 Oct: RAISE_TARGET = everything): turn the late-call arb sets, the tilt/direction
legs and the market maker's inventory into cash by 18:00 ET on 3 Nov, patiently, from 31 Oct 00:00 ET.

Pure logic; Bot.wind_down_cycle wires books, orders, positions and the state file (params/wind_down_state.json).

What is sold, and when
  * Sets (NO on every leg, or YES on every leg) in LATE-call races: west coast / Hawaii / Alaska, slow-count states, a
    last poll close after 21:00 ET, Georgia (runoff), chamber races, close races (favourite < 0.85), and the races in
    force_late (Rhode Island Governor: three separate binary markets, Sizhe capped it on 23 Oct). EARLY-call sets are
    held through the call; selling them at the call is report-only until Sizhe approves it (item 000c).
  * Single legs left after the sets are taken out (tilt / direction / market-making inventory), in every race. Legs in
    markets the MM quotes are sold only above the MM's shrinking inventory cap (full size until 1 Nov 00:00 ET, then
    linear to zero at 12:00 ET 3 Nov or the MM's stop_quoting_at if earlier, when the MM stops quoting).
  * From 31 Oct 00:00 ET recycling is switched off: every set it buys would have to be sold again before 18:00 ET 3 Nov.
  * Never: the cheap-favourites sleeve's races (held through the call by design), high-frequency arb (churn) races, the
    catch-up sweep's legs (both flatten themselves on their own schedules), legs the punt executor or the election-night
    sleeve trade (HOLD_PURPOSES) and anything listed in params/wind_down_hold.json; nothing at or after 18:00 ET 3 Nov.
    Catch-up and cheap-favourite positions are taken out of the positions before sets are counted. Listed in the report.

How (research_notes/ladder_exits.md and ev_hunt.md 2c, both Fable-checked)
  * Clips of at most clip_sets sets (1,000) per race, at least gap_sec (30 min) apart per race, shrunk while walking
    the book costs more than max_walk ticks per set; a single leg sells at most 80% of its touch, fill-or-cancel.
  * Cheapest first: candidates are ranked by exit cost per unit of cash raised.
  * Timing gate on sets: until 2 Nov 00:00 ET a clip sells only while the race's touch exit cost is >= 1 tick below its
    trailing 12-h median (ev_hunt 2c: sum of asks, deferral for hours); then while it is at or below its trailing 24-h
    median ("level 0"). The gate is dropped for a race once the time left is only just enough to sell what it holds:
    deadline - clips left x gap - fallback_buffer.
  * Cost cap: a set clip's walked exit cost, as a share of its proceeds (payout - proceeds) / proceeds, may not exceed
    max(the set's locked return, schedule): the schedule rises from 0 at 31 Oct 00:00 ET to 4.5% at 2 Nov 00:00 ET and
    5.5% at 17:45 ET 3 Nov (item 000b). A leg's exit cost against the outside fair (SIG mid if none) may not exceed
    1 tick until 2 Nov, 2 ticks until 12:00 ET 3 Nov, then rises to max(3 ticks, 5.5% of the price) at 17:45 ET.
  * At most max_cost_per_hour of exit cost an hour: every set sold below payout counts as drawdown for the kill switch
    (400 an hour).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Optional
from zoneinfo import ZoneInfo

from .election_night import POLL_CLOSE_ET, SLOW_COUNT, race_abbr

TICK = 0.005
ET = ZoneInfo("America/New_York")


def et_ts(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=ET).timestamp()


START = et_ts(2026, 10, 31)              # sales start (EDT, 04:00 UTC)
MM_SHRINK = et_ts(2026, 11, 1)           # MM inventory cap starts shrinking (EDT, 04:00 UTC)
LOOSEN = et_ts(2026, 11, 2)              # set cost cap 4.5%; timing gate eases from 12-h median - 1 tick to 24-h median (EST)
MM_FLAT = et_ts(2026, 11, 3, 12)         # MM flat, stops quoting (17:00 UTC)
CAP_END = et_ts(2026, 11, 3, 17, 45)     # cost caps at their loosest (22:45 UTC)
DEADLINE = et_ts(2026, 11, 3, 18)        # everything late-call sold (23:00 UTC)
STOP = DEADLINE                          # no clip at or after 18:00 ET: the punt and the election-night sleeve trade then
                                         # (review 1, blocker: the wind-down sold and cancelled a punt leg bought at 17:30 ET)
# purposes whose legs the wind-down never sells or cancels orders on (punt executor, election-night sleeve)
HOLD_PURPOSES = ("punt", "punt_exit", "election", "election_exit")

CAP_LOOSEN = 0.045
CAP_END_FRAC = 0.055
EARLY_LAST_CLOSE = 21.0                  # a state whose last polls close after 21:00 ET is late
EARLY_FAV = 0.85                         # a race whose favourite is below this is close: late
LATE_STATES = {"GA"}                     # Georgia: runoff if nobody reaches 50%
DEFAULT_FORCE_LATE = ("Rhode Island Governor",)


def et_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%d %b %H:%M")


def _lerp(t: float, t0: float, t1: float, a: float, b: float) -> float:
    if t <= t0:
        return a
    if t >= t1:
        return b
    return a + (b - a) * (t - t0) / (t1 - t0)


def active(now: float) -> bool:
    return START <= now < STOP


def mm_flat_due(now: float, end: float = MM_FLAT) -> bool:
    return now >= end


def set_cost_cap(now: float, locked: float) -> float:
    """Largest exit cost a set clip may pay, as a share of its proceeds: never below the set's locked return (an exit at
    no realised loss is always allowed)."""
    if now < LOOSEN:
        sched = _lerp(now, START, LOOSEN, 0.0, CAP_LOOSEN)
    else:
        sched = _lerp(now, LOOSEN, CAP_END, CAP_LOOSEN, CAP_END_FRAC)
    return max(max(0.0, locked), sched)


def leg_cost_cap(now: float, price: float) -> float:
    """Largest exit cost per share of a single leg against its fair value (price units)."""
    if now < LOOSEN:
        return 1 * TICK
    if now < MM_FLAT:
        return 2 * TICK
    return _lerp(now, MM_FLAT, CAP_END, 3 * TICK, max(3 * TICK, CAP_END_FRAC * max(0.0, price)))


def mm_inventory_cap(now: float, base: float, end: float = MM_FLAT) -> float:
    """The MM's per-race inventory cap: full until 1 Nov 00:00 ET, linear to 0 at `end` (12:00 ET 3 Nov, or the MM's own
    stop_quoting_at if earlier)."""
    return _lerp(now, MM_SHRINK, max(MM_SHRINK + 1.0, end), base, 0.0)


def mm_end(stop_quoting_at: Optional[str]) -> float:
    """When the MM is flat: 12:00 ET 3 Nov, or its stop_quoting_at (ISO, UTC) if that is earlier."""
    try:
        t = datetime.fromisoformat(str(stop_quoting_at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return MM_FLAT
    return min(MM_FLAT, t)


def fallback_ts(clips_left: int, gap_sec: float, buffer_sec: float) -> float:
    """From this time on a race sells without the timing gate: just enough time is left for its clips."""
    return DEADLINE - max(0, clips_left) * gap_sec - buffer_sec


def global_fallback_ts(cost_left: float, clips_left: int, budget_per_hour: float, sec_per_clip: float, buffer_sec: float) -> float:
    """From this time on EVERY race sells without the timing gate: the exit cost still to pay at the hourly budget, or the
    clips still to send at one per sec_per_clip, needs all the time left (review 2: races falling back together shared one
    budget and stranded sets at 18:00)."""
    hours = max(0.0, cost_left) / max(1.0, budget_per_hour)
    return DEADLINE - buffer_sec - max(hours * 3600.0, max(0, clips_left) * sec_per_clip)


# ------------------------------------------------------------------------------------------------ call time
def call_class(race: str, fav: Optional[float], force_late=DEFAULT_FORCE_LATE) -> tuple[str, str]:
    """('early' | 'late', why) for a race on election night. fav = the favourite's probability (outside fair, else SIG)."""
    if race in force_late:
        return "late", "sold anyway (force_late)"
    try:
        ab = race_abbr(race)
    except Exception:
        ab = None
    if ab is None or ab not in POLL_CLOSE_ET:
        return "late", "no state poll close (chamber or unknown race)"
    last = POLL_CLOSE_ET[ab][1]
    if ab in SLOW_COUNT:
        return "late", f"{ab} counts slowly"
    if ab in LATE_STATES:
        return "late", f"{ab} runoff rules"
    if last > EARLY_LAST_CLOSE:
        return "late", f"{ab} polls close {last:g}:00 ET"
    if fav is None:
        return "late", "no price for the favourite"
    if fav < EARLY_FAV:
        return "late", f"close race (favourite {fav:.2f})"
    return "early", f"{ab} polls close by {last:g}:00 ET, favourite {fav:.2f}"


# ------------------------------------------------------------------------------------------------ holdings
def decompose(exs: list, positions: dict) -> tuple[Optional[str], int, dict]:
    """(kind, sets, residual legs) of one race: kind 'sell_set' (NO on every leg; positions negative), 'buy_set' (YES on
    every leg) or None; residual = what is left on each leg once the sets are taken out (signed, + = YES)."""
    qs = [float(positions.get(e, 0.0)) for e in exs]
    kind, k = None, 0
    if len(exs) >= 2 and all(q < 0 for q in qs):
        kind, k = "sell_set", int(min(-q for q in qs))
    elif len(exs) >= 2 and all(q > 0 for q in qs):
        kind, k = "buy_set", int(min(qs))
    res = {}
    for e, q in zip(exs, qs):
        r = q + k if kind == "sell_set" else q - k if kind == "buy_set" else q
        if abs(r) >= 1:
            res[e] = r
    return kind, k, res


def touch_cost(kind: str, books: dict, exs: list) -> Optional[float]:
    """Exit cost of one set at the touch (price units): sum of asks - 1 for NO sets, 1 - sum of bids for YES sets."""
    side = "asks" if kind == "sell_set" else "bids"
    px = []
    for e in exs:
        lv = (books.get(e) or {}).get(side) or []
        if not lv:
            return None
        px.append(float(lv[0]["price"]))
    return sum(px) - 1.0 if kind == "sell_set" else 1.0 - sum(px)


def walk_quote(kind: str, books: dict, exs: list, qty: int) -> Optional[dict]:
    """Walk `qty` sets: NO sets buy YES at the asks, YES sets sell YES at the bids (sent as NO buys at 1 - bid).
    Returns proceeds per set, payout, forgone (payout - proceeds), cost_frac (forgone / proceeds), walk (extra per set
    beyond the touch) and the leg limits (YES price for NO sets; NO price for YES sets)."""
    if qty <= 0:
        return None
    side = "asks" if kind == "sell_set" else "bids"
    n = len(exs)
    total, limits, touch = 0.0, {}, 0.0
    for e in exs:
        lv = (books.get(e) or {}).get(side) or []
        need, cost, worst = qty, 0.0, None
        for x in lv:
            take = min(need, int(x["quantity"]))
            if take <= 0:
                continue
            cost += take * float(x["price"])
            need -= take
            worst = float(x["price"])
            if need <= 0:
                break
        if need > 0 or worst is None:
            return None
        total += cost
        touch += float(lv[0]["price"])
        limits[e] = round(worst, 3) if kind == "sell_set" else round(1.0 - worst, 3)
    avg = total / qty
    if kind == "sell_set":
        proceeds, payout, walk = n - avg, n - 1.0, avg - touch
    else:
        proceeds, payout, walk = avg, 1.0, touch - avg
    forgone = payout - proceeds
    return {"proceeds": proceeds, "payout": payout, "forgone": forgone, "walk": walk,
            "cost_frac": forgone / proceeds if proceeds > 0 else 9.0, "limits": limits}


def clip_quote(kind: str, books: dict, exs: list, want: int, max_walk: float, min_clip: int) -> tuple[int, Optional[dict]]:
    """The largest clip <= want (halving) whose walk costs <= max_walk per set; at min_clip whatever the walk (the cost
    cap still applies). (0, None) when even min_clip has no depth."""
    q = int(want)
    while q >= max(1, min_clip):
        wq = walk_quote(kind, books, exs, q)
        if wq is not None and (wq["walk"] <= max_walk + 1e-9 or q // 2 < min_clip):
            return q, wq
        q //= 2
    q = min(int(want), max(1, min_clip))
    wq = walk_quote(kind, books, exs, q)
    return (q, wq) if wq else (0, None)


def locked_return(kind: str, cost_basis: dict, positions: dict, exs: list, sets: int) -> float:
    """The set's locked return from the venue's cost basis (payout - cost) / cost, in [0, CAP_END_FRAC]. Only when every
    leg holds exactly the sets (a leg's basis then is the set's); 0 otherwise (review 1: residual NO bought at other
    prices inflated it to 22% and a 5.3% exit was taken on 31 Oct)."""
    try:
        per = 0.0
        for e in exs:
            q = abs(float(positions.get(e, 0.0)))
            b = float(cost_basis.get(e, 0.0))
            if q <= 0 or abs(q - sets) > 0.5 or b <= 0:
                return 0.0                       # a leg without its own basis: unknown, never the 5.5% clamp (review 2)
            per += b / q
        payout = len(exs) - 1.0 if kind == "sell_set" else 1.0
        return min(CAP_END_FRAC, max(0.0, (payout - per) / per)) if per > 0 else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


# ------------------------------------------------------------------------------------------------ timing gate
def cost_medians(rows, legs_of: dict, now: float, grid: float = 300.0, stale: float = 1800.0) -> dict:
    """rows: (race, party, ts, bid, ask) from venue_quotes (venue 'sig'), a leg stored when its quote changes plus a
    10-min heartbeat. legs_of: race -> parties. Each leg's last quote is carried forward on a 5-min grid over the last
    24 h; a grid point counts when every leg has a quote <= 30 min old. Returns race -> {'sell_set': (m12, m24),
    'buy_set': (m12, m24)} of the touch exit cost (sum of asks - 1, 1 - sum of bids); a median is None without >= 6 h
    (12-h) or 12 h (24-h) of points."""
    by: dict = {}
    for race, party, ts, bid, ask in rows:
        if race in legs_of and ts is not None:
            by.setdefault(race, []).append((float(ts), party, bid, ask))
    out = {}
    start = now - 86400.0
    for race, xs in by.items():
        xs.sort(key=lambda r: r[0])
        parties = set(legs_of[race])
        last, i = {}, 0
        pts = []
        t = start
        while t <= now:
            while i < len(xs) and xs[i][0] <= t:
                last[xs[i][1]] = xs[i]
                i += 1
            if parties <= set(last) and all(t - last[p][0] <= stale and last[p][2] is not None and last[p][3] is not None for p in parties):
                sa = sum(float(last[p][3]) for p in parties)
                sb = sum(float(last[p][2]) for p in parties)
                pts.append((t, sa - 1.0, 1.0 - sb))
            t += grid
        res = {}
        for j, kind in ((1, "sell_set"), (2, "buy_set")):
            p12 = [p[j] for p in pts if p[0] >= now - 43200.0]
            p24 = [p[j] for p in pts]
            m12 = median(p12) if len(p12) * grid >= 6 * 3600 else None
            m24 = median(p24) if len(p24) * grid >= 12 * 3600 else None
            res[kind] = (m12, m24)
        out[race] = res
    return out


def gate_ok(now: float, cost: Optional[float], m12: Optional[float], m24: Optional[float], fallback: float) -> tuple[bool, str]:
    """Timing gate for a set clip at touch exit cost `cost` (price units)."""
    if now >= fallback:
        return True, "deadline near: no timing gate"
    if cost is None:
        return False, "no touch"
    if now < LOOSEN:
        if m12 is None:
            return False, "no 12-h median yet"
        return (cost <= m12 - TICK + 1e-9), f"cost {cost / TICK:+.1f} vs 12-h median {m12 / TICK:+.1f} - 1 tick"
    if m24 is None:
        return True, "no 24-h median: level gate off"
    return (cost <= m24 + 1e-9), f"cost {cost / TICK:+.1f} vs 24-h median {m24 / TICK:+.1f}"


# ------------------------------------------------------------------------------------------------ state
@dataclass
class WindDownState:
    last_race: dict = field(default_factory=dict)       # race -> time of our last set clip there
    last_leg: dict = field(default_factory=dict)        # exchange -> time of our last leg clip there
    pulled: dict = field(default_factory=dict)          # exchange -> until: a leg whose fill-or-cancel could not be confirmed
    cost_log: list = field(default_factory=list)        # [(ts, exit cost in S)], last 2 h
    sold: list = field(default_factory=list)            # [(ts, what, qty, proceeds per unit, cost)], newest 500

    def to_json(self) -> dict:
        return {"last_race": self.last_race, "last_leg": self.last_leg, "pulled": self.pulled,
                "cost_log": self.cost_log[-500:], "sold": self.sold[-500:]}

    def load(self, d: dict):
        self.last_race = {str(k): float(v) for k, v in (d.get("last_race") or {}).items()}
        self.last_leg = {str(k): float(v) for k, v in (d.get("last_leg") or {}).items()}
        self.pulled = {str(k): float(v) for k, v in (d.get("pulled") or {}).items()}
        self.cost_log = [tuple(x) for x in (d.get("cost_log") or []) if isinstance(x, (list, tuple)) and len(x) == 2]
        self.sold = [list(x) for x in (d.get("sold") or [])][-500:]

    def cost_last_hour(self, now: float) -> float:
        self.cost_log = [(t, c) for t, c in self.cost_log if now - t < 7200.0]
        return sum(c for t, c in self.cost_log if now - t < 3600.0)

    def book(self, now: float, what: str, qty: int, px: float, cost: float):
        self.cost_log.append((now, max(0.0, cost)))
        self.sold.append([now, what, int(qty), round(px, 4), round(cost, 2)])
        self.sold = self.sold[-500:]


def clips_left(qty: int, clip: int) -> int:
    return int(math.ceil(max(0, qty) / max(1, clip)))


# ------------------------------------------------------------------------------------------------ projection
def project(races: dict, positions: dict, books: dict, fav_of, cash: float, clip: int = 1000, gap_sec: float = 1800.0,
            max_walk: float = TICK, min_clip: int = 100, force_late=DEFAULT_FORCE_LATE, exempt_races: Optional[dict] = None,
            exempt_legs: Optional[dict] = None, mm_quoted=()) -> dict:
    """Projected liquid cash at 18:00 ET 3 Nov (backlog 000e): every holding at its sale value in clips at today's
    books. races: name -> exchange ids; fav_of(race) -> favourite probability or None; exempt_races / exempt_legs:
    name or exchange -> sleeve that sells it on its own schedule. Sale value of a set clip = walked proceeds of a clip
    (<= clip sets, walk <= max_walk per set), applied to the whole holding (the book refills between 30-min clips)."""
    exempt_races, exempt_legs = exempt_races or {}, exempt_legs or {}
    rows = []
    for name, exs in sorted(races.items()):
        held = {e: positions.get(e, 0.0) for e in exs if abs(positions.get(e, 0.0)) >= 1}
        if not held:
            continue
        if name in exempt_races:
            rows.append({"holding": name, "kind": "exempt", "sleeve": exempt_races[name], "legs": {e: round(q) for e, q in held.items()},
                         "when": f"{exempt_races[name]}: own schedule"})
            continue
        kind, k, res = decompose(exs, positions)
        if kind and k > 0:
            payout = (len(exs) - 1.0) if kind == "sell_set" else 1.0
            cls, why = call_class(name, fav_of(name), force_late)
            q, wq = clip_quote(kind, books, exs, min(k, clip), max_walk, min_clip)
            tc = touch_cost(kind, books, exs)
            per = wq["proceeds"] if wq else None
            n_clips = clips_left(k, q or clip)
            rows.append({"holding": name, "kind": kind, "qty": k, "call": cls, "why": why, "payout": round(k * payout, 1),
                         "touch_cost_ticks": None if tc is None else round(tc / TICK, 1), "clip": q,
                         "sale_per_set": None if per is None else round(per, 4),
                         "sale_value": None if per is None else round(k * per, 1),
                         "exit_cost": None if per is None else round(k * (payout - per), 1),
                         "clips": n_clips, "hours_min": round(n_clips * gap_sec / 3600.0, 1),
                         "when": ("31 Oct-3 Nov 18:00 ET, clips every 30 min" if cls == "late"
                                  else "held through the call (selling at the call is report-only)")})
        for e, r in res.items():
            if e in exempt_legs:
                rows.append({"holding": f"{name} leg {e}", "kind": "exempt", "sleeve": exempt_legs[e], "legs": {e: round(r)},
                             "when": f"{exempt_legs[e]}: own schedule"})
                continue
            b = books.get(e) or {}
            if r > 0:
                lv = b.get("bids") or []
                px = float(lv[0]["price"]) if lv else None              # sell YES at the bid
            else:
                lv = b.get("asks") or []
                px = 1.0 - float(lv[0]["price"]) if lv else None        # NO held: buy YES at the ask, get 1 - ask
            mm = e in mm_quoted
            rows.append({"holding": f"{name} leg {e}", "kind": "mm_leg" if mm else "leg", "qty": round(abs(r)), "side": "yes" if r > 0 else "no",
                         "sale_per_share": None if px is None else round(px, 4),
                         "sale_value": None if px is None else round(abs(r) * px, 1),
                         "when": "1 Nov-3 Nov 12:00 ET (MM shrinks to flat)" if mm else "31 Oct-3 Nov, within 1-3 ticks of fair"})
    tot = {"cash_now": round(cash, 1)}
    late = sum(x["sale_value"] or 0.0 for x in rows if x.get("call") == "late")
    early_sale = sum(x["sale_value"] or 0.0 for x in rows if x.get("call") == "early")
    early_payout = sum(x["payout"] for x in rows if x.get("call") == "early")
    legs = sum(x["sale_value"] or 0.0 for x in rows if x["kind"] in ("leg", "mm_leg"))
    tot.update({"late_sets_sale": round(late, 1), "legs_sale": round(legs, 1), "early_sets_sale_today": round(early_sale, 1),
                "early_sets_payout": round(early_payout, 1),
                "projected_cash_1800_et": round(cash + late + legs, 1),
                "projected_cash_if_early_sold_too": round(cash + late + legs + early_sale, 1),
                "exempt": sorted({x["sleeve"] for x in rows if x["kind"] == "exempt"})})
    return {"rows": rows, "totals": tot}
