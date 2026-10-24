"""Cheap safe favourites (Sizhe 24 Oct): buy favourites SIG prices >= 2c under the Polymarket/Kalshi fair in fast-count
states and hold them through the call, so the cash plus profit is back on election night for the punt's stage 2.

Evidence (research_notes/cheap_favourites.md): outside favourites at 0.80-0.99 won 52/52 at 13 days (48.2 expected, p 0.02),
so the outside fair is, if anything, too low for favourites; a 15k basket of today's 32 legs, edge / (1 - p) weights, at most
2,000 per leg: EV +840 at outside prices (+1,166 calibrated), P(loss) 11%, 5th percentile -664, worst of 200k nights -6.8k.

Rules:
  * legs: contract terms the same as Polymarket/Kalshi and no late-result flag (tc_mapping.json); the state's last polls
    close by 21:00 ET and it is not AK AZ CA ME NV OR UT WA GA; the bought side's fair is >= 0.80; edge >= min_edge
    (+1c when only one outside venue prices it); Polymarket and Kalshi within 4 pts; no Independent leg in the race;
    never the punt's candidate races; never a race where we hold > 4,000 shares on a leg, and no order that would take
    a leg past 4,000 (Sizhe's rule, churn_skip_holding).
  * one route per race outcome (D-YES or R-NO, whichever is cheaper), never a route that trades against a holding we
    already have on that leg (SIG nets YES against NO, so that would close an arb set or another sleeve's position), and
    never a NO next to a NO on another leg of the race (NO on every leg reads as an arb set to the recycler, the free
    unwinds and the high-frequency arb's sell-all; the Bot also marks cheap-favourite races protected from those paths).
  * size: the budget spread by edge / (1 - p) over today's candidates, each leg at most leg_cap; orders of at most
    order_notional at the touch, <= 80% of the touch quantity.
  * partisan: never adds to a direction the bot blocked this cycle; the book (fresh positions read, plus unfilled orders
    of this and the directional sleeves counted one-sided) stays within +-(cap - headroom) = +-4,000 in the direction of
    each add, and the sleeve's own net D within +-net_max (2,000) whatever the book, so the shared partisan room stays
    free for the catch-up sweep and the others (precheck 24 Oct). Races the catch-up sweep is in are skipped.
  * last entry 3 Nov 17:30 ET, before the first polls close (IN/KY 18:00 ET).
  * no exits before the call, except when SIG prices the held side >= 2c ABOVE the fair (edge reversed): selling then beats
    holding in expectation and frees the cash before the punt. Never after the state's polls close.

Pure decision logic (no API calls) so it can be tested offline; the Bot wires it to orders (purpose 'cheapfav', exits
'cheapfav_exit'). The ledger is the fills table, so a restart never loses a position.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from ..correlation_model import race_state

TICK = 0.005
LAST_ENTRY = datetime(2026, 11, 3, 22, 30, tzinfo=timezone.utc).timestamp()     # 3 Nov 17:30 ET (EST, UTC-5)
# last poll close (ET, 3 Nov) of each fast-count state, from data/election_night/poll_close_2026.json
FAST_CLOSE_ET = {"AL": 20.0, "AR": 20.5, "CO": 21.0, "CT": 20.0, "DE": 20.0, "FL": 20.0, "IA": 21.0, "IL": 20.0, "IN": 19.0,
                 "KS": 21.0, "KY": 19.0, "LA": 21.0, "MA": 20.0, "MD": 20.0, "MI": 21.0, "MN": 21.0, "MO": 20.0, "MS": 20.0,
                 "NC": 19.5, "ND": 21.0, "NE": 21.0, "NH": 20.0, "NJ": 20.0, "NM": 21.0, "NY": 21.0, "OH": 19.5, "OK": 20.0,
                 "PA": 20.0, "RI": 20.0, "SC": 19.0, "SD": 21.0, "TN": 20.0, "TX": 21.0, "VA": 19.0, "VT": 19.0, "WI": 21.0,
                 "WV": 19.5, "WY": 21.0}
# races the punt screener names as stage-1 / stage-2 candidates (research_notes/punt_strategy.md section 4)
PUNT_RACES = ("FL-13 House race", "FL-16 House race", "Vermont Governor", "Minnesota Senate", "Nebraska Governor",
              "Oregon Governor", "Louisiana Senate", "Idaho Governor", "Wyoming Governor", "South Dakota Governor")
ELECTION_DAY_UTC = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc).timestamp()   # 3 Nov 00:00 ET
AGREE = 0.04
SINGLE_VENUE_EXTRA = 0.01
REVERSE = 0.02


def same_terms_exchanges(path: str) -> Optional[set]:
    """Exchange ids whose SIG contract matches Polymarket/Kalshi with no late-result flag; None if the file is missing."""
    try:
        m = json.load(open(path))
    except (OSError, ValueError):
        return None
    out = set()
    for ex, v in m.items():
        if ex.startswith("_") or not isinstance(v, dict):
            continue
        if v.get("terms") == "same" and not any(str(d).endswith("late_settle") for d in (v.get("differences") or [])):
            out.add(str(ex))
    return out


def poll_close_ts(state: str) -> Optional[float]:
    h = FAST_CLOSE_ET.get(state)
    return None if h is None else ELECTION_DAY_UTC + h * 3600


class CheapFavourites:
    def __init__(self, budget: float = 15000.0, min_edge: float = 0.02, leg_cap: float = 2000.0, min_fav: float = 0.80,
                 order_notional: float = 500.0, hold_limit: float = 4000.0, cash_floor: float = 1000.0,
                 same_terms: Optional[set] = None, exclude_races=PUNT_RACES, states: Optional[dict] = None,
                 last_entry: float = LAST_ENTRY, min_shares: int = 20, net_max: float = 2000.0, headroom: float = 1000.0):
        self.budget, self.min_edge, self.leg_cap, self.min_fav = budget, min_edge, leg_cap, min_fav
        self.order_notional, self.hold_limit, self.cash_floor = order_notional, hold_limit, cash_floor
        self.same_terms = same_terms
        self.exclude = set(exclude_races or ())
        self.states = dict(states or FAST_CLOSE_ET)
        self.last_entry, self.min_shares = last_entry, min_shares
        # partisan budget (precheck 24 Oct: the sleeve filled the book to the 5,000 cap and starved the catch-up sweep): its
        # own net D cost stays within +-net_max whatever the book, and the book stays within +-(cap - headroom) in the
        # direction of each add
        self.net_max, self.headroom = net_max, headroom

    # ------------------------------------------------------------------ candidates
    def candidates(self, rows: list[dict], positions: dict, race_legs: Optional[dict] = None) -> list[dict]:
        """Best route per race outcome on the latest directional rows: {race, win_party, exchange_id, side, price, p_win,
        edge, need, weight}. `positions`: venue positions (YES-signed shares) to apply the 4,000-share and no-crossing rules;
        `race_legs`: race -> every exchange id of the race (so a leg without a directional row is still checked)."""
        if self.same_terms is None:
            return []                                          # no contract map: never guess which legs are safe
        by_race: dict = {}
        for r in rows:
            by_race.setdefault(r.get("race"), []).append(r)
        out = []
        for race, rs in by_race.items():
            if not race or race in self.exclude or any(r.get("party") not in ("D", "R") for r in rs):
                continue                                       # Independent leg: D-NO is not an R bet
            st = race_state(race)
            if st not in self.states:
                continue
            exs = {str(r.get("exchange_id")) for r in rs} | set((race_legs or {}).get(race, ()))
            if any(abs(positions.get(e, 0.0)) > self.hold_limit for e in exs):
                continue
            best: dict = {}
            for r in rs:
                ex = str(r.get("exchange_id") or "")
                fair, bid, ask = r.get("fair"), r.get("sig_bid"), r.get("sig_ask")
                if not ex or ex not in self.same_terms or fair is None:
                    continue
                pm, k = r.get("pm"), r.get("kalshi")
                if pm is not None and k is not None and abs(pm - k) > AGREE:
                    continue
                need = self.min_edge + (SINGLE_VENUE_EXTRA if (pm is None or k is None) else 0.0)
                pos = positions.get(ex, 0.0)
                routes = []
                if ask is not None and pos >= 0:              # buy YES (never against a held NO on this leg)
                    routes.append(("yes", ask, fair, r["party"]))
                no_elsewhere = any(positions.get(e, 0.0) < 0 for e in exs if e != ex)
                if bid is not None and pos <= 0 and not no_elsewhere:   # buy NO at 1 - bid (never against a held YES, and
                    # never next to a NO on another leg: NO on every leg reads as an arb set to every set path; review 24 Oct)
                    routes.append(("no", round(1 - bid, 3), 1 - fair, "R" if r["party"] == "D" else "D"))
                for side, px, p_win, win in routes:
                    edge = p_win - px
                    if p_win < self.min_fav or edge < need - 1e-9:
                        continue
                    c = {"race": race, "state": st, "win_party": win, "exchange_id": ex, "party": r["party"], "side": side,
                         "price": px, "p_win": p_win, "edge": edge, "need": need, "fair": fair,
                         "weight": edge / max(1e-3, 1 - p_win)}
                    if win not in best or edge > best[win]["edge"]:
                        best[win] = c
            out.extend(best.values())
        return sorted(out, key=lambda c: -c["weight"])

    def targets(self, cands: list[dict]) -> dict:
        """(race, win_party) -> target notional: the budget spread by weight, each leg capped, the excess re-spread."""
        tgt = {(c["race"], c["win_party"]): 0.0 for c in cands}
        w = {(c["race"], c["win_party"]): c["weight"] for c in cands}
        for _ in range(20):
            open_ = [k for k in tgt if tgt[k] < self.leg_cap - 1e-6]
            left = self.budget - sum(tgt.values())
            sw = sum(w[k] for k in open_)
            if left < 1 or not open_ or sw <= 0:
                break
            for k in open_:
                tgt[k] = min(self.leg_cap, tgt[k] + left * w[k] / sw)
        return tgt

    # ------------------------------------------------------------------ entries
    def entries(self, cands: list[dict], held: dict, deployed: float, cash: float, now: float, touch: dict,
                positions: dict, net_d: float, cap: float = 5000.0, max_orders: int = 3, blocked=(),
                pend: tuple = (0.0, 0.0), own: Optional[tuple] = None) -> list[dict]:
        """Orders to send now. held: (race, win_party) -> cost already in the sleeve (fills plus orders not yet filled);
        deployed: total of the same; touch: exchange_id -> (best price for our side, quantity there) from a fresh book;
        positions: venue positions (empty = the positions read failed: no entries, every holding guard would be blind).
        Partisan (review 24 Oct; every term only ever tightens the check it applies to):
          net_d   the book's net D notional from a fresh positions read (filled only);
          pend    (D, R) notional of unfilled orders that could still fill, this sleeve's and the directional sleeves',
                  counted one-sided: pending D only against D adds, pending R only against R adds, so an order that
                  expired unfilled can never make room;
          own     (filled net D cost of this sleeve, its pending D, its pending R); default: held as filled;
          blocked directions the bot itself blocked this cycle (risk.blocked_directions): never added to.
        A D add needs net_d + pend_D + n <= cap - headroom and own_net + own_pend_D + n <= net_max; an R add the mirror."""
        if now > self.last_entry or not positions:
            return []
        pend_d, pend_r = pend
        if own is None:
            own = (sum(x if k[1] == "D" else -x for k, x in held.items()), 0.0, 0.0)
        own_net, own_pd, own_pr = own
        lim = cap - self.headroom
        tgt = self.targets(cands)
        out = []
        room = min(self.budget - deployed, cash - self.cash_floor)
        for c in cands:
            if len(out) >= max_orders or room < 1:
                break
            k = (c["race"], c["win_party"])
            want = min(tgt.get(k, 0.0) - held.get(k, 0.0), self.order_notional, room)
            t = touch.get(c["exchange_id"])
            if want < 1 or not t:
                continue
            px, tq = t
            if px is None or c["p_win"] - px < c.get("need", self.min_edge) - 1e-9:
                continue                                       # the fresh book no longer offers the edge
            q = int(min(want / px, 0.8 * (tq or 0)))
            pos = positions.get(c["exchange_id"], 0.0)
            q = min(q, int(self.hold_limit - abs(pos)))      # never take a leg past 4,000 shares
            if q < self.min_shares:
                continue
            notional = q * px
            adds = c["win_party"]                             # buying a D favourite adds D, an R favourite adds R
            if adds in blocked:
                continue
            if adds == "D":
                if net_d + pend_d + notional > lim + 1e-9 or own_net + own_pd + notional > self.net_max + 1e-9:
                    continue
                pend_d += notional
                own_pd += notional
            else:
                if net_d - pend_r - notional < -lim - 1e-9 or own_net - own_pr - notional < -self.net_max - 1e-9:
                    continue
                pend_r += notional
                own_pr += notional
            room -= notional
            out.append(dict(c, qty=q, price=px, notional=notional))
        return out

    # ------------------------------------------------------------------ exits
    def exit_reason(self, ex: str, qty: float, row: Optional[dict], race: str, now: float) -> Optional[str]:
        """Close a held position only when SIG now prices the held side >= 2c above the outside fair (edge reversed:
        selling beats holding in expectation), with both venues agreeing, and only before the state's polls close."""
        if row is None or row.get("fair") is None:
            return None
        close = poll_close_ts(race_state(race) or "")
        if close is not None and now >= close:
            return None
        pm, k = row.get("pm"), row.get("kalshi")
        if pm is None or k is None or abs(pm - k) > AGREE:
            return None
        fair, bid, ask = row["fair"], row.get("sig_bid"), row.get("sig_ask")
        if qty > 0 and bid is not None and bid - fair >= REVERSE - 1e-9:
            return f"edge reversed: SIG bid {bid:.3f} >= fair {fair:.3f} + 2c"
        if qty < 0 and ask is not None and fair - ask >= REVERSE - 1e-9:
            return f"edge reversed: SIG ask {ask:.3f} <= fair {fair:.3f} - 2c"
        return None


def pending(orders: list[dict], filled_by_order: dict, race_of: dict) -> tuple[dict, float]:
    """Entry orders sent in the last minutes whose fills are not (yet) in the fills table: (race, win_party) -> notional,
    and the total. Counted as held so a lagging fill ingest cannot make the sleeve buy the same leg twice."""
    by_outcome: dict = {}
    for o in orders:
        ex, side = str(o["exchange_id"]), o["side"]
        left = float(o["quantity"]) - float(filled_by_order.get(str(o.get("order_id")), 0.0))
        rp = race_of.get(ex)
        if left < 1 or not rp:
            continue
        race, party = rp
        win = party if side == "yes" else ("R" if party == "D" else "D")
        by_outcome[(race, win)] = by_outcome.get((race, win), 0.0) + left * float(o["price"])   # price paid for `side`
    return by_outcome, sum(by_outcome.values())


def ledger(fills: list[dict], race_of: dict) -> tuple[dict, dict, dict, float]:
    """The sleeve's book from its fills (purposes cheapfav / cheapfav_exit; quantity YES-signed, price YES price).
    Returns (open shares by exchange, cost by exchange, cost by (race, win_party), total cost deployed). Cost is the cash
    paid for the shares still held (average cost, reduced pro rata by exits)."""
    pos, cost = {}, {}
    for f in fills:
        ex, q, p = str(f["exchange_id"]), float(f["quantity"]), float(f["price"])
        paid = q * p if q > 0 else -q * (1 - p)                # YES bought at p, or NO bought at 1 - p
        old = pos.get(ex, 0.0)
        if old == 0 or (old > 0) == (q > 0):
            pos[ex] = old + q
            cost[ex] = cost.get(ex, 0.0) + paid
        else:                                                  # an exit: release cost pro rata
            closed = min(abs(q), abs(old))
            cost[ex] = cost.get(ex, 0.0) * (1 - closed / abs(old))
            pos[ex] = old + q
            if abs(pos[ex]) < 1e-9:
                pos[ex], cost[ex] = 0.0, 0.0
            elif (pos[ex] > 0) != (old > 0):                   # flipped through zero: the remainder is a new position
                cost[ex] = abs(pos[ex]) * (p if pos[ex] > 0 else 1 - p)
    open_pos = {ex: q for ex, q in pos.items() if abs(q) >= 1}
    by_outcome: dict = {}
    for ex, q in open_pos.items():
        rp = race_of.get(ex)
        if not rp:
            continue
        race, party = rp
        win = party if q > 0 else ("R" if party == "D" else "D")
        by_outcome[(race, win)] = by_outcome.get((race, win), 0.0) + cost.get(ex, 0.0)
    return open_pos, {ex: cost.get(ex, 0.0) for ex in open_pos}, by_outcome, sum(cost.get(ex, 0.0) for ex in open_pos)
