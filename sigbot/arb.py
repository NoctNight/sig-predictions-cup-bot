"""Set arbitrage across the party markets of one race.

For a race with legs L (D, R and maybe I), each leg a YES contract paying 1 if that party wins:

  sell-set  : buy NO on every leg at (1 - bid_i).  Cost = |L| - sum(bid_i).  Pays |L| - 1 if one listed
              party wins (always), |L| if nobody listed wins.  Riskless edge = sum(bid_i) - 1.
  buy-set   : buy YES on every leg at ask_i.  Cost = sum(ask_i).  Pays 1 if a listed party wins,
              0 otherwise (prob x).  Expected edge = 1 - sum(ask_i) - x.

Execution is atomic (POST /orders/multi-leg, executionMode=allOrNone): either every leg fills its
whole quantity at or better than its limit, or nothing trades. That is what keeps the book delta-neutral:
each executed chunk has equal quantity on every leg. We still verify positions afterwards and flatten any
residual that a 502 ORDER_STATUS_UNKNOWN or external netting could leave.

Capital is allocated greedily by marginal return (edge / cost) across all races and book levels, subject
to a global arb budget and a per-race cap.
"""
from __future__ import annotations

import json
import logging
import os
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from .api import ApiError, SuperMarketClient
from .config import ArbConfig, round_tick
from .store import Store
from .universe import Race, Universe

log = logging.getLogger("sigbot.arb")


@dataclass
class Chunk:
    race: str
    kind: str                     # sell_set | buy_set
    qty: int
    edge: float                   # per set
    cost: float                   # per set (capital tied up)
    limits: dict                  # exchange_id -> limit price for the order we send (NO price for sell_set, YES price for buy_set)

    @property
    def ret(self) -> float:
        return self.edge / self.cost if self.cost > 0 else 0.0


def _walk(books: dict, exs: list[str], side: str, edge_fn, min_edge: float) -> list[tuple[int, float, float, dict]]:
    """Walk the books of all legs in lockstep. side='bids' for sell_set, 'asks' for buy_set.
    Returns chunks (qty, edge, cost, level_prices)."""
    ptr = {e: 0 for e in exs}
    lv = {e: books[e][side] for e in exs}
    if any(not lv[e] for e in exs):
        return []
    rem = {e: lv[e][0]["quantity"] for e in exs}
    out = []
    while True:
        try:
            prices = {e: lv[e][ptr[e]]["price"] for e in exs}
        except IndexError:
            break
        edge, cost = edge_fn(prices)
        if edge <= min_edge:
            break
        q = int(math.floor(min(rem[e] for e in exs)))
        if q <= 0:
            break
        out.append((q, edge, cost, prices))
        for e in exs:
            rem[e] -= q
            if rem[e] <= 0:
                ptr[e] += 1
                if ptr[e] < len(lv[e]):
                    rem[e] = lv[e][ptr[e]]["quantity"]
    return out


class ArbEngine:
    def __init__(self, api: SuperMarketClient, store: Store, cfg: ArbConfig, universe: Universe, slug: str,
                 starting_cash: float, dry_run: bool = True):
        self.api, self.store, self.cfg, self.u, self.slug = api, store, cfg, universe, slug
        self.starting_cash = starting_cash
        self.dry_run = dry_run
        self.committed: dict[str, float] = {}      # race -> capital committed so far (this process)
        self.cooldown: dict[str, float] = {}       # race -> time until which it is skipped (after an all-or-none rejection)
        self.hostile: set = set()                  # races where a basket filled partly: never arb again this session
        self.x_table: dict = store.get_param("x", {}).get("x", {}) or {}
        self._x_live: dict = {}
        self._x_live_mtime = 0.0
        self._x_hist: dict = {}
        try:
            self._x_hist = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "params", "x.json"))).get("by_race", {})
        except Exception:
            self._x_hist = {}

    # ------------------------------------------------------------- x per race
    def x_for(self, race: Race) -> float:
        if race.has_independent:
            return 0.0  # the Independent leg is in the set; residual "other" probability is negligible
        if race.name in self.cfg.x_overrides:
            return float(self.cfg.x_overrides[race.name])
        live = self.x_live().get(race.name)
        if live is not None:
            return float(live["x"])                      # outside venues' price of non-major candidates in this race
        if race.name in self._x_hist:
            return float(self._x_hist[race.name])        # per-state historical rate (empirical Bayes)
        return float(self.x_table.get(race.office, self.cfg.x_default.get(race.office, 0.02)))

    def x_live(self) -> dict:
        """params/x_live.json (built by sigbot.xlive), reloaded when the file changes."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "params", "x_live.json")
        try:
            m = os.path.getmtime(path)
            if m != self._x_live_mtime:
                self._x_live = json.load(open(path)).get("by_race", {})
                self._x_live_mtime = m
        except (OSError, ValueError):
            pass
        return self._x_live

    # --------------------------------------------------------------- scanning
    def candidates_from_prices(self, prices: dict[str, dict]) -> list[Race]:
        """Cheap pre-filter using the bulk price snapshot (3 reads for the whole universe)."""
        out = []
        for race in self.u.races.values():
            exs = race.exchange_ids
            if any(e not in prices for e in exs):
                continue
            bids = [prices[e]["bestBid"] for e in exs]
            asks = [prices[e]["bestAsk"] for e in exs]
            if all(b is not None for b in bids) and sum(bids) - 1 > self.cfg.min_edge_sell:
                out.append(race)
            elif all(a is not None for a in asks) and 1 - sum(asks) - self.x_for(race) > self.cfg.min_edge_buy:
                out.append(race)
        return out

    def chunks_for_race(self, race: Race, books: dict[str, dict]) -> list[Chunk]:
        exs = race.exchange_ids
        n = len(exs)
        chunks: list[Chunk] = []
        for q, edge, cost, prices in _walk(books, exs, "bids", lambda p: (sum(p.values()) - 1.0, n - sum(p.values())), self.cfg.min_edge_sell):
            chunks.append(Chunk(race.name, "sell_set", q, edge, cost, {e: round_tick(1 - p) for e, p in prices.items()}))
        x = self.x_for(race)
        for q, edge, cost, prices in _walk(books, exs, "asks", lambda p: (1.0 - sum(p.values()) - x, sum(p.values())), self.cfg.min_edge_buy):
            chunks.append(Chunk(race.name, "buy_set", q, edge, cost, {e: round_tick(p) for e, p in prices.items()}))
        return chunks

    # -------------------------------------------------------------- execution
    def budget_left(self, cash: float) -> float:
        total_cap = self.cfg.capital_frac * self.starting_cash
        used = sum(self.committed.values())
        return max(0.0, min(total_cap - used, cash * 0.95 - self.cfg.cash_reserve))

    def race_budget_left(self, race: str) -> float:
        """Capital still allowed in `race`: the per-race ceiling (max_race_frac), tightened by the allocator's edge-based
        cap when one is loaded (`race_caps`, floored at `race_cap_floor` so a new opportunity can still be taken small)."""
        lim = self.cfg.max_race_frac * self.starting_cash
        caps = getattr(self, "race_caps", None)
        if caps:
            cap = caps.get(race, getattr(self, "race_cap_default", lim))
            lim = min(lim, max(float(cap), getattr(self, "race_cap_floor", 0.0)))
        return max(0.0, lim - self.committed.get(race, 0.0))

    def plan(self, candidate_books: dict[str, dict[str, dict]], cash: float) -> list[Chunk]:
        """candidate_books: race name -> {exchange_id: orderbook}. Returns chunks to execute in order."""
        allc: list[Chunk] = []
        for rname, books in candidate_books.items():
            race = self.u.races[rname]
            allc.extend(self.chunks_for_race(race, books))
        allc.sort(key=lambda c: -c.ret)
        budget = self.budget_left(cash)
        plan: list[Chunk] = []
        race_spent: dict[str, float] = {}
        for c in allc:
            if budget <= 0:
                break
            floor = self.cfg.min_ret_sell if (c.kind == "sell_set" and self.cfg.min_ret_sell >= 0) else self.cfg.min_ret
            if c.ret < floor or c.race in self.hostile or self.cooldown.get(c.race, 0) > time.time():
                continue
            rb = self.race_budget_left(c.race) - race_spent.get(c.race, 0.0)
            cap = min(budget, rb)
            q = min(c.qty, int(cap // c.cost))
            if q < self.cfg.min_set_qty:
                continue
            plan.append(Chunk(c.race, c.kind, q, c.edge, c.cost, c.limits))
            budget -= q * c.cost
            race_spent[c.race] = race_spent.get(c.race, 0.0) + q * c.cost
        return plan

    def legs_for(self, chunk: Chunk) -> list[dict]:
        legs = []
        for ex, limit in chunk.limits.items():
            if chunk.kind == "sell_set":
                legs.append({"exchangeId": ex, "side": "no", "action": "buy", "quantity": chunk.qty, "price": limit,
                             "tournamentId": self.api.tournament_id})
            else:
                legs.append({"exchangeId": ex, "side": "yes", "action": "buy", "quantity": chunk.qty, "price": limit,
                             "tournamentId": self.api.tournament_id})
        return legs

    def execute(self, plan: list[Chunk], cycle_id: Optional[int] = None) -> list[dict]:
        results = []
        for chunk in plan:
            legs = self.legs_for(chunk)
            if self.dry_run:
                log.info("[DRY] %s %s qty=%d edge=%.4f cost=%.4f ret=%.2f%% legs=%s", chunk.kind, chunk.race, chunk.qty,
                         chunk.edge, chunk.cost, 100 * chunk.ret, legs)
                self.store.record_arb(chunk.race, chunk.kind, legs, chunk.qty, chunk.edge, chunk.cost, False, {"dry_run": True})
                results.append({"chunk": chunk, "ok": False, "dry": True})
                continue
            key = self.api.new_key("arb")
            ok, resp = False, None
            try:
                resp = self.api.place_multi_leg(legs, all_or_none=True, key=key)
                ok = True
            except ApiError as e:
                resp = {"error": e.code, "message": e.message, "details": e.details}
                if e.status == 502:   # ORDER_STATUS_UNKNOWN: retry same key is safe
                    try:
                        resp = self.api.place_multi_leg(legs, all_or_none=True, key=key)
                        ok = True
                    except ApiError as e2:
                        resp = {"error": e2.code, "message": e2.message, "details": e2.details}
                elif e.code == "VALIDATION_ERROR" and isinstance(e.details, dict) and e.details.get("rejectionReasonCode") == "AllOrNoneNotFilled":
                    log.info("arb %s %s not filled atomically (book moved); leg=%s; race cooled down 10 min", chunk.kind, chunk.race, e.details.get("leg"))
                else:
                    log.warning("arb %s %s failed: %s", chunk.kind, chunk.race, resp)
            if ok:
                filled = all(r["data"].get("quantityTraded", 0) >= chunk.qty for r in resp.get("results", []))
                if not filled:
                    self.hostile.add(chunk.race)
                    log.error("ARB PARTIAL FILL %s %s: legs %s; race marked hostile for this session, residual check will flag it",
                              chunk.kind, chunk.race, [(r["data"].get("exchangeId"), r["data"].get("quantityTraded")) for r in resp.get("results", [])])
                self.committed[chunk.race] = self.committed.get(chunk.race, 0.0) + chunk.qty * chunk.cost
                for r in resp.get("results", []):
                    d = r["data"]
                    self.store.record_order(d.get("orderId"), d.get("exchangeId"), None, d.get("side", "no"), d.get("action", "buy"),
                                            d.get("price"), chunk.qty, "arb", cycle_id=cycle_id, status="filled" if not d.get("open") else "open",
                                            response=d)
                log.info("ARB %s %s qty=%d edge=%.4f cost=%.4f ret=%.2f%% filled=%s", chunk.kind, chunk.race, chunk.qty, chunk.edge,
                         chunk.cost, 100 * chunk.ret, filled)
            self.store.record_arb(chunk.race, chunk.kind, legs, chunk.qty, chunk.edge, chunk.cost, ok, resp)
            results.append({"chunk": chunk, "ok": ok, "resp": resp})
            if not ok:
                self.cooldown[chunk.race] = time.time() + 60    # skip this race for 60 s (the book moved), keep going with the others
        return results

    # ---------------------------------------------------------- capital recycling
    def unwind_quote(self, race: Race, books: dict[str, dict], qty: int) -> Optional[dict]:
        """Cost of unwinding `qty` held sets of `race` now: buy YES on every leg at the asks (walked). Returns
        proceeds per set, the edge the set still has to earn if held (payout - proceeds), and the leg limits."""
        exs = race.exchange_ids
        n = len(exs)
        remaining, spent, limits = qty, 0.0, {}
        for e in exs:
            asks = books[e].get("asks") or []
            need, cost, worst = qty, 0.0, None
            for lvl in asks:
                take = min(need, int(lvl["quantity"]))
                if take <= 0:
                    continue
                cost += take * lvl["price"]; need -= take; worst = lvl["price"]
                if need <= 0:
                    break
            if need > 0 or worst is None:
                return None                                  # not enough depth to unwind atomically
            spent += cost; limits[e] = round_tick(worst)
        proceeds = n - spent / qty                           # per set: we receive 1 per NO share sold = 1 - ask paid for YES
        payout = n - 1
        return {"proceeds": proceeds, "forgone_edge": payout - proceeds, "forgone_ret": (payout - proceeds) / proceeds if proceeds > 0 else 9.0,
                "limits": limits}

    def recycle(self, best_new: Optional[dict], held_books: dict[str, dict[str, dict]], positions: dict[str, float], cash: float,
                cycle_id: Optional[int] = None) -> Optional[dict]:
        """If the arb budget is exhausted and `best_new` (race, kind, ret, cost, qty) beats a held set's remaining
        return by recycle_min_gain, unwind that set (allOrNone, all legs) so the scanner can buy the new one."""
        if not self.cfg.recycle_enabled or not best_new or best_new["ret"] < self.cfg.recycle_min_new_ret:
            return None
        if self.budget_left(cash) >= best_new["cost"] * max(self.cfg.min_set_qty, 1):
            return None                                      # not capital-locked
        if best_new.get("failed") or best_new["race"] in self.hostile or self.cooldown.get(best_new["race"], 0) > time.time():
            return None                                      # the opportunity was not executable a moment ago: do not unwind for it
        fresh = best_new.get("fresh_books")
        if fresh:
            again = [c for c in self.chunks_for_race(self.u.races[best_new["race"]], fresh) if c.qty >= self.cfg.min_set_qty]
            if not again or max(c.ret for c in again) < self.cfg.recycle_min_new_ret:
                return None                                  # gone on a fresh read: it was bait or it closed
        real_sets = {r["race"] for r in self.store.rows("SELECT DISTINCT race FROM arb_trades WHERE ok=1 AND kind IN ('sell_set','buy_set')")}
        candidates = []
        for rname, books in held_books.items():
            race = self.u.races[rname]
            if rname == best_new["race"] or rname not in real_sets:
                continue
            qs = [positions.get(l.exchange_id, 0.0) for l in race.legs.values()]
            if not qs or not all(q < 0 for q in qs):
                continue
            held = int(min(-q for q in qs))
            # unwind only what the new opportunity can use
            q = min(held, int(best_new["qty"]))
            if q < self.cfg.min_set_qty:
                continue
            uq = self.unwind_quote(race, books, q)
            if not uq:
                continue
            gain = best_new["ret"] - uq["forgone_ret"]
            candidates.append((gain, rname, q, uq))
        candidates.sort(key=lambda c: -c[0])
        if not candidates or candidates[0][0] < self.cfg.recycle_min_gain:
            return None
        gain, rname, q, uq = candidates[0]
        race = self.u.races[rname]
        legs = [{"exchangeId": e, "side": "yes", "action": "buy", "quantity": q, "price": lim, "tournamentId": self.api.tournament_id}
                for e, lim in uq["limits"].items()]
        log.info("RECYCLE %s: unwind %d sets at proceeds %.4f/set (forgone ret %.2f%%) to fund %s %s at %.2f%% (gain %.2f%%)", rname, q,
                 uq["proceeds"], 100 * uq["forgone_ret"], best_new["race"], best_new["kind"], 100 * best_new["ret"], 100 * gain)
        if self.dry_run:
            self.store.record_arb(rname, "unwind_set", legs, q, -uq["forgone_edge"], uq["proceeds"], False, {"dry_run": True, "fund": best_new})
            return {"race": rname, "qty": q, "dry": True}
        try:
            resp = self.api.place_multi_leg(legs, all_or_none=True, key=self.api.new_key("unw"))
        except ApiError as e:
            self.store.record_arb(rname, "unwind_set", legs, q, -uq["forgone_edge"], uq["proceeds"], False, {"error": e.code, "details": e.details})
            log.info("recycle %s not filled: %s", rname, e.code)
            return None
        self.store.record_arb(rname, "unwind_set", legs, q, -uq["forgone_edge"], uq["proceeds"], True, resp)
        self.committed[rname] = max(0.0, self.committed.get(rname, 0.0) - q * uq["proceeds"])
        for r in resp.get("results", []):
            d = r["data"]
            self.store.record_order(d.get("orderId"), d.get("exchangeId"), None, "yes", "buy", d.get("price"), q, "arb", cycle_id=cycle_id,
                                    status="filled" if not d.get("open") else "open", response=d)
        return {"race": rname, "qty": q, "proceeds": uq["proceeds"], "fund": best_new}

    def unwind_set(self, rname: str, books: dict, qty: int, why: str = "offload") -> Optional[dict]:
        """Unwind `qty` held sets of `rname` now, all legs or none, at limits from walking the asks."""
        race = self.u.races[rname]
        uq = self.unwind_quote(race, books, qty)
        if not uq or qty < self.cfg.min_set_qty:
            return None
        legs = [{"exchangeId": e, "side": "yes", "action": "buy", "quantity": qty, "price": lim, "tournamentId": self.api.tournament_id}
                for e, lim in uq["limits"].items()]
        log.info("UNWIND (%s) %s: %d sets at proceeds %.4f/set, gives up %.2f%%", why, rname, qty, uq["proceeds"], 100 * uq["forgone_ret"])
        if self.dry_run:
            return {"race": rname, "qty": qty, "dry": True}
        try:
            resp = self.api.place_multi_leg(legs, all_or_none=True, key=self.api.new_key("unw"))
        except ApiError as e:
            self.store.record_arb(rname, "unwind_set", legs, qty, -uq["forgone_edge"], uq["proceeds"], False, {"error": e.code, "why": why})
            log.info("unwind %s not filled: %s", rname, e.code)
            return None
        self.store.record_arb(rname, "unwind_set", legs, qty, -uq["forgone_edge"], uq["proceeds"], True, resp)
        self.committed[rname] = max(0.0, self.committed.get(rname, 0.0) - qty * uq["proceeds"])
        return {"race": rname, "qty": qty, "proceeds": uq["proceeds"]}

    def free_unwinds(self, held_books: dict[str, dict[str, dict]], positions: dict[str, float], cycle_id=None) -> list[dict]:
        """Unwind held arb sets whenever the legs' asks sum low enough that unwinding returns at least the settlement
        payout (n - 1 per set): the locked profit is taken now at no cost and the capital is freed. Atomic, all legs."""
        out = []
        real_sets = {r["race"] for r in self.store.rows("SELECT DISTINCT race FROM arb_trades WHERE ok=1 AND kind IN ('sell_set','buy_set')")}
        for rname, books in held_books.items():
            if rname not in real_sets:
                continue
            race = self.u.races[rname]
            qs = [positions.get(l.exchange_id, 0.0) for l in race.legs.values()]
            if not qs or not all(q < 0 for q in qs):
                continue
            held = int(min(-q for q in qs))
            q, uq = held, None
            while q >= self.cfg.min_set_qty:
                uq = self.unwind_quote(race, books, q)
                if uq and uq["forgone_edge"] <= 1e-9:
                    break
                q = q // 2
                uq = None
            if not uq:
                continue
            legs = [{"exchangeId": e, "side": "yes", "action": "buy", "quantity": q, "price": lim, "tournamentId": self.api.tournament_id}
                    for e, lim in uq["limits"].items()]
            log.info("FREE UNWIND %s: %d sets at proceeds %.4f/set >= payout %d (locks profit now, frees %.0f)", rname, q, uq["proceeds"],
                     len(race.legs) - 1, q * uq["proceeds"])
            if self.dry_run:
                out.append({"race": rname, "qty": q, "dry": True}); continue
            try:
                resp = self.api.place_multi_leg(legs, all_or_none=True, key=self.api.new_key("fre"))
                self.store.record_arb(rname, "unwind_set", legs, q, -uq["forgone_edge"], uq["proceeds"], True, resp)
                self.committed[rname] = max(0.0, self.committed.get(rname, 0.0) - q * uq["proceeds"])
                out.append({"race": rname, "qty": q, "proceeds": uq["proceeds"]})
            except ApiError as e:
                self.store.record_arb(rname, "unwind_set", legs, q, -uq["forgone_edge"], uq["proceeds"], False, {"error": e.code})
                log.info("free unwind %s not filled: %s", rname, e.code)
        return out

    # -------------------------------------------------------------- delta check
    def race_delta(self, race: Race, positions: dict[str, float]) -> dict:
        """positions: exchange_id -> signed quantity (+YES / -NO).
        A set position should hold the same NO (or YES) quantity on every leg. Report the residual."""
        qs = {p: positions.get(l.exchange_id, 0.0) for p, l in race.legs.items()}
        if not qs:
            return {"residual": 0.0, "legs": qs}
        # a hedged set is the common part; residual is the deviation of each leg from the common value
        common = max(qs.values(), key=lambda v: 0) if False else _median(list(qs.values()))
        residual = {p: q - common for p, q in qs.items()}
        return {"common": common, "legs": qs, "residual": residual,
                "max_abs_residual": max(abs(v) for v in residual.values())}

    def flatten_residuals(self, positions: dict[str, float], prices: dict[str, dict]) -> list[dict]:
        """If legs of a race are unequal beyond tolerance, bring the odd leg back in line with a limit order at the
        touch (never a market order). Only acts when the fix is still cheap (within 2 ticks of the touch)."""
        actions = []
        for race in self.u.races.values():
            if race.name not in self.committed:      # only races where we hold an arb set; MM inventory is not a residual
                continue
            info = self.race_delta(race, positions)
            if info.get("max_abs_residual", 0) <= self.cfg.delta_tolerance:
                continue
            for party, res in info["residual"].items():
                if abs(res) <= self.cfg.delta_tolerance:
                    continue
                ex = race.legs[party].exchange_id
                p = prices.get(ex) or {}
                qty = int(abs(res))
                if res > 0:   # too many YES (or too few NO) on this leg: buy NO at the bid-equivalent
                    px = p.get("bestBid")
                    order = dict(exchangeId=ex, side="no", action="buy", quantity=qty, price=round_tick(1 - px) if px else None)
                else:         # too many NO: buy YES
                    px = p.get("bestAsk")
                    order = dict(exchangeId=ex, side="yes", action="buy", quantity=qty, price=round_tick(px) if px else None)
                actions.append({"race": race.name, "party": party, "order": order})
                if not self.dry_run and self.cfg.hedge_residual and order["price"] is not None:
                    try:
                        resp = self.api.place_order(ex, order["side"], "buy", qty, order["price"], key=self.api.new_key("hedge"))
                        self.store.record_order(resp.get("orderId"), ex, race.legs[party].market_id, order["side"], "buy", order["price"], qty, "hedge", response=resp)
                    except ApiError as e:
                        log.warning("hedge failed %s: %s", order, e)
                else:
                    log.debug("[DRY] residual hedge %s", order)
        return actions


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])
