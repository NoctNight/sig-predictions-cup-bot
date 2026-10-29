"""Gap-trade pilot (Sizhe 27 Oct: "try tiny live pilot"): buy the side SIG prices >= 3c under the Polymarket/Kalshi fair on any
same-terms leg, hold at most a few days, sell before the punt needs the cash. TINY by design: the study (research_notes/
gap_trades.md) found no out-of-sample edge, so this pilot exists to get live evidence, not because the edge is proven.

Rules:
  * legs: same contract terms (tc_mapping.json), two-party races only, PM and Kalshi within 4 pts (+1c when one venue prices
    it), not the chamber markets ("U.S. House": settles manually, terms unchecked), not the punt's races, no leg held > 4,000
    shares, never against a holding on the leg, never a NO next to a NO in the race (reads as an arb set to the set paths),
    never a leg the cheap-favourites sleeve holds (its book stays its own).
  * entry: edge at the touch >= min_edge (buy YES at the ask, or NO at 1 - bid); levels are walked while each marginal share
    still has >= min_edge; ranked by edge; at most order_notional per order and 80% of a level; leg_cap cost per leg; budget
    total (ledger cost of the pilot's legs).
  * partisan: the pilot's own net D cost stays within +-net_max whatever the book, and the book stays within the cap less
    headroom in the direction of each add (the cheap-favourites arithmetic, only tighter): pro-D and pro-R trades pair up.
  * exits (all at the touch): the gap closed (SIG within take_edge of the fair on the held side), the edge reversed by 2c,
    a stop (held side's exit price 4c under cost), or a time stop (max_hold_h after the leg's first entry, never later than
    hard_exit_ts). The pilot's positions are in the cheap-favourites ledger (purposes cheapfav / cheapfav_exit), so every
    protection and the wind-down exemption apply; the hard exit comes before the wind-down starts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..correlation_model import race_state
from .cheap_favourites import AGREE, PUNT_RACES, SINGLE_VENUE_EXTRA

CHAMBER_RACES = ("U.S. House", "U.S. Senate")      # chamber-control markets: Kalshi combo-derived fair, manual settlement; unchecked
LAST_ENTRY = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc).timestamp()      # 29 Oct 16:00 ET
HARD_EXIT = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc).timestamp()        # 30 Oct 20:00 ET, before the wind-down (31 Oct ET)


class GapPilot:
    def __init__(self, budget: float = 900.0, min_edge: float = 0.03, leg_cap: float = 300.0, order_notional: float = 150.0,
                 hold_limit: float = 4000.0, same_terms: Optional[set] = None, exclude_races=PUNT_RACES + CHAMBER_RACES,
                 min_shares: int = 20, net_max: float = 500.0, clean_shares: float = 100.0, headroom: float = 1000.0, max_hold_h: float = 72.0,
                 stop: float = 0.04, take_edge: float = 0.005, last_entry: float = LAST_ENTRY, hard_exit_ts: float = HARD_EXIT):
        self.budget, self.min_edge, self.leg_cap, self.order_notional = budget, min_edge, leg_cap, order_notional
        self.hold_limit, self.same_terms, self.exclude = hold_limit, same_terms, set(exclude_races or ())
        self.min_shares, self.net_max, self.headroom = min_shares, net_max, headroom
        self.clean_shares = clean_shares
        self.max_hold_h, self.stop, self.take_edge = max_hold_h, stop, take_edge
        self.last_entry, self.hard_exit_ts = last_entry, hard_exit_ts

    # ------------------------------------------------------------------ candidates
    def candidates(self, rows: list, positions: dict, race_legs: Optional[dict] = None, avoid_ex=(), cooldown_ex=(), own_ex=()) -> list:
        """Best route per leg on the latest directional rows: {race, exchange_id, side, party, win_party, price, p_win, edge,
        need, fair}. `avoid_ex`: legs the cheap-favourites sleeve holds; `cooldown_ex`: legs the pilot exited (stop, close or time) lately:
        no re-entry (review 28 Oct: stop-outs refreshed the caps and the leg re-entered every few minutes)."""
        if self.same_terms is None:
            return []                                        # no contract map: never guess which legs are safe
        by_race: dict = {}
        for r in rows:
            by_race.setdefault(r.get("race"), []).append(r)
        out = []
        outcome_best: dict = {}
        for race, rs in by_race.items():
            if not race or race in self.exclude or any(r.get("party") not in ("D", "R") for r in rs):
                continue
            exs = {str(r.get("exchange_id")) for r in rs} | set((race_legs or {}).get(race, ()))
            if any(abs(positions.get(e, 0.0)) > self.hold_limit for e in exs) or any(e in set(avoid_ex) | set(cooldown_ex) for e in exs):
                continue
            if any(abs(positions.get(e, 0.0)) > self.clean_shares for e in exs if e not in set(own_ex)):
                continue          # a race where another sleeve holds inventory: SIG nets YES against NO on a leg, and the cash raise
                #                  (the shock reserve's refill) could no longer sell from it (sim shock_reserve, 28 Oct)
            for r in rs:
                ex = str(r.get("exchange_id") or "")
                fair, bid, ask = r.get("fair"), r.get("sig_bid"), r.get("sig_ask")
                if not ex or ex not in self.same_terms or fair is None:
                    continue
                if bid is None or ask is None or ask - bid > self.stop - 0.01 + 1e-9:
                    continue                                      # a spread of the stop's size would stop the leg out at once
                pm, k = r.get("pm"), r.get("kalshi")
                if pm is None and k is None:
                    continue
                if pm is not None and k is not None and abs(pm - k) > AGREE:
                    continue
                need = self.min_edge + (SINGLE_VENUE_EXTRA if (pm is None or k is None) else 0.0)
                pos = positions.get(ex, 0.0)
                no_elsewhere = any(positions.get(e, 0.0) < 0 for e in exs if e != ex)
                routes = []
                if ask is not None and pos >= 0:
                    routes.append(("yes", ask, fair, r["party"]))
                if bid is not None and pos <= 0 and not no_elsewhere:
                    routes.append(("no", round(1 - bid, 3), 1 - fair, "R" if r["party"] == "D" else "D"))
                best = None
                for side, px, p_win, win in routes:
                    edge = p_win - px
                    if edge >= need - 1e-9 and (best is None or edge > best["edge"]):
                        best = {"race": race, "state": race_state(race), "exchange_id": ex, "party": r["party"], "side": side,
                                "win_party": win, "price": px, "p_win": p_win, "edge": edge, "need": need, "fair": fair}
                if best and (race, best["win_party"]) not in outcome_best or (
                        best and best["edge"] > outcome_best[(race, best["win_party"])]["edge"]):
                    outcome_best[(race, best["win_party"])] = best     # one route per race outcome: D-YES and R-NO are the same bet
            out.extend(v for k, v in outcome_best.items() if k[0] == race)
        return sorted(out, key=lambda c: -c["edge"])

    # ------------------------------------------------------------------ entries
    def entries(self, cands: list, levels: dict, cost_by_ex: dict, deployed: float, cash: float, now: float, positions: dict,
                net_d: float, own_net: float, cap: float = 5000.0, max_orders: int = 3, blocked=(), pend: tuple = (0.0, 0.0),
                own_pend: tuple = (0.0, 0.0)) -> list:
        """Orders to send now. levels: exchange_id -> [(price for our side, quantity), ...] best first, from a fresh book;
        cost_by_ex: the pilot's cost per leg (fills plus unfilled orders); deployed: its total; own_net: its filled net D
        cost; own_pend: (D, R) notional of the pilot's own unfilled orders, one-sided (an expired R order never makes room
        for D); pend: (D, R) notional of unfilled orders of any sleeve, one-sided. Each order walks the levels while the
        marginal share keeps >= need edge; D adds need net_d + pend_D + n <= cap - headroom and own_net + n <= net_max, an
        R add the mirror."""
        if now > self.last_entry or not positions:
            return []
        pend_d, pend_r = pend
        own_pd, own_pr = own_pend
        lim = cap - self.headroom
        out, room = [], min(self.budget - deployed, cash)
        for c in cands:                                       # best edge first
            if len(out) >= max_orders or room < 1:
                break
            ex = c["exchange_id"]
            if any(o["race"] == c["race"] for o in out):
                continue                                      # one order per race per cycle: no NO+NO / YES+YES pair before the fills show
            want = min(self.order_notional, room, self.leg_cap - cost_by_ex.get(ex, 0.0))
            if want < 1 or c["win_party"] in blocked:
                continue
            qty, notional, worst = 0, 0.0, None
            for px, lq in levels.get(ex) or ():
                if px is None or c["p_win"] - px < c["need"] - 1e-9:
                    break                                     # the marginal share no longer has the edge
                q = int(min((want - notional) / px, 0.8 * (lq or 0)))
                q = min(q, int(self.hold_limit - abs(positions.get(ex, 0.0))) - qty)
                if q < 1:
                    continue
                qty, notional, worst = qty + q, notional + q * px, px
                if want - notional < px:
                    break
            if qty < self.min_shares:
                continue
            adds, px_avg = c["win_party"], notional / qty
            notional = qty * worst                            # worst case (every share at the limit) for the caps below
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
            room -= qty * worst                               # the order may fill at its limit: cash and caps count the worst case
            cost_by_ex[ex] = cost_by_ex.get(ex, 0.0) + qty * worst
            out.append(dict(c, qty=qty, price=worst, notional=qty * worst, avg_price=px_avg))
        return out

    # ------------------------------------------------------------------ exits
    def exit_reason(self, ex: str, qty: float, row: Optional[dict], book: Optional[dict], cost_px: float, entry_ts: float,
                    now: float) -> Optional[str]:
        """Why a held pilot leg should be sold now (at the touch), or None. cost_px: average price paid for the held side.
        The time stop and the stop-loss need no outside price; the closing / reversal rules need a fair."""
        held_side_exit = None                                 # what the touch pays us for one share of the held side
        if book:
            if qty > 0 and book.get("bids"):
                held_side_exit = book["bids"][0]["price"]
            elif qty < 0 and book.get("asks"):
                held_side_exit = 1 - book["asks"][0]["price"]
        age_h = (now - entry_ts) / 3600.0 if entry_ts else 0.0
        if now >= self.hard_exit_ts:
            return "hard exit before the wind-down"
        if entry_ts and age_h >= self.max_hold_h:
            return f"time stop after {age_h:.0f} h"
        if book and book.get("bids") and book.get("asks") and book["asks"][0]["price"] - book["bids"][0]["price"] <= 1.5 * self.stop:
            # the stop reads the mid of a normal book (spread <= 6c): a stray bid in a thin book is not a price (review 28 Oct)
            mid = (book["bids"][0]["price"] + book["asks"][0]["price"]) / 2
            held_mid = mid if qty > 0 else 1 - mid
            if held_mid <= cost_px - self.stop + 1e-9:
                return f"stop: mid {held_mid:.3f} <= cost {cost_px:.3f} - {self.stop:.2f}"
        if row is not None and row.get("fair") is not None and held_side_exit is not None:
            fair = row["fair"] if qty > 0 else 1 - row["fair"]
            if fair - held_side_exit <= self.take_edge + 1e-9:
                return f"gap closed: exit {held_side_exit:.3f} within {self.take_edge:.3f} of fair {fair:.3f}"
        return None
