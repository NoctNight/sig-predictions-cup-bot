"""Tilt harvest: sell overpriced longshots, buy underpriced favourites, hold to settlement.

Why: prices on this venue are compressed toward 50% relative to Polymarket/Kalshi (favourite-longshot bias with a
position cap, the PredictIt regime). A 2%-elsewhere candidate trades at 10-15c here; a 97% candidate at 85-88c.
Published participant data (research_notes/public_prediction_market_alpha.md) shows ~15% per unit of cash from a
resting "harvest ladder" on <=10% longshots and >=90% favourites with >= 8% edge per unit of collateral.

Fair value without external data: logit recalibration p_fair = sigmoid(b * logit(p_mid)), b > 1 (Kalshi/Polymarket
politics slopes 1.3-1.8). Edge per unit of collateral:
    longshot (buy NO at 1 - p):   ((1 - p_fair) - (1 - p)) / (1 - p) = (p - p_fair) / (1 - p)
    favourite (buy YES at p):     (p_fair - p) / p
Orders rest at the touch and one/two ticks away from it (giving liquidity, never taking), expire after `order_ttl_sec`,
and are re-posted when the touch moves. Positions are held to settlement; cash is the binding constraint, so the
sleeve has its own capital budget and a per-market cap, and skips any exchange the market maker is quoting.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .api import ApiError, SuperMarketClient
from .config import TICK, TiltConfig, round_tick
from .store import Store
from .universe import Universe
from . import risk

log = logging.getLogger("sigbot.tilt")


def logit(p: float) -> float:
    p = min(0.999, max(0.001, p))
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


@dataclass
class TiltState:
    last_touch: Optional[float] = None
    last_ts: float = 0.0


class TiltHarvester:
    def __init__(self, api: SuperMarketClient, store: Store, cfg: TiltConfig, universe: Universe, starting_cash: float,
                 dry_run: bool = True):
        self.api, self.store, self.cfg, self.u = api, store, cfg, universe
        self.starting_cash, self.dry_run = starting_cash, dry_run
        self.state: dict[str, TiltState] = {}
        self.blocked: set = set()

    def fair(self, p: float) -> float:
        """Two models, take the one implying LESS edge (conservative):
        mixture:  p_venue = (1 - s) p_true + s/2  ->  p_true = (p - s/2) / (1 - s)   (measured tilt, s ~ 0.09-0.13)
        logit:    p_true = sigmoid(b * logit(p))                                       (calibration slope b ~ 1.3-1.8)"""
        s = self.cfg.tilt_s
        mix = min(0.999, max(0.001, (p - s / 2) / (1 - s)))
        lg = sigmoid(self.cfg.calibration_slope * logit(p))
        return max(mix, lg) if p < 0.5 else min(mix, lg)

    def candidates(self, prices: dict[str, dict], exclude: set[str]) -> list[tuple[str, str, float, float]]:
        """Returns (exchange, kind, mid, edge_per_collateral) for markets in the longshot/favourite bands."""
        out = []
        for ex in self.u.all_exchanges():
            if ex in exclude:
                continue
            p = prices.get(ex)
            if not p or p["bestBid"] is None or p["bestAsk"] is None:
                continue
            mid = 0.5 * (p["bestBid"] + p["bestAsk"])
            pf = self.fair(mid)
            if mid <= self.cfg.longshot_max:
                edge = (mid - pf) / max(1e-6, 1 - mid)
                if edge >= self.cfg.min_edge:
                    out.append((ex, "longshot", mid, edge))
            elif mid >= self.cfg.favourite_min:
                edge = (pf - mid) / max(1e-6, mid)
                if edge >= self.cfg.min_edge:
                    out.append((ex, "favourite", mid, edge))
        out.sort(key=lambda r: -r[3])
        return out

    def invested(self, ex: str, positions: dict[str, float], mid: float) -> float:
        """Collateral this sleeve has locked in one market (from its own fills)."""
        if self.store is None:
            return 0.0
        tot = 0.0
        for r in self.store.rows("SELECT side, price, quantity FROM fills WHERE purpose='tilt' AND exchange_id=?", (ex,)):
            if r["price"] is not None:
                tot += abs(r["quantity"]) * (r["price"] if r["side"] == "yes" else 1 - r["price"])
        return tot

    def cycle(self, prices: dict[str, dict], positions: dict[str, float], open_orders: dict[str, list], exclude: set[str],
              cash: float, cycle_id: int, write_budget: int) -> dict:
        summary = {"candidates": 0, "requoted": [], "orders": 0, "writes": 0}
        cap_total = self.cfg.capital_frac * self.starting_cash
        # capital committed by this sleeve = collateral of the tilt positions still HELD: per market, tilt fills net of
        # exit fills, clamped to the actual position (it used to sum every tilt fill ever, which starved the sleeve once
        # positions had been exited)
        committed = 0.0
        if self.store is not None:
            net, cost, qty = {}, {}, {}
            for r in self.store.rows("SELECT exchange_id, side, price, quantity, purpose FROM fills WHERE purpose IN ('tilt','exit') AND price IS NOT NULL ORDER BY filled_at"):
                ex = r["exchange_id"]
                s = 1.0 if r["side"] == "yes" else -1.0
                net[ex] = net.get(ex, 0.0) + s * abs(r["quantity"])
                if r["purpose"] == "tilt":
                    cost[ex] = cost.get(ex, 0.0) + abs(r["quantity"]) * (r["price"] if r["side"] == "yes" else 1 - r["price"])
                    qty[ex] = qty.get(ex, 0.0) + abs(r["quantity"])
            for ex, n in net.items():
                if ex not in qty or not qty[ex]:
                    continue
                pos = positions.get(ex, 0.0)
                held = min(abs(n), abs(pos)) if n * pos > 0 else 0.0
                committed += held * cost[ex] / qty[ex]
        self.committed = committed
        budget_total = cap_total
        # resting tilt orders also count against the budget (worst case = everything fills)
        resting_by_ex: dict[str, float] = {}
        if self.store is not None:
            mine = {r["order_id"] for r in self.store.rows("SELECT order_id FROM orders WHERE purpose='tilt' AND status='open'")}
            for ex, lst in open_orders.items():
                for o in lst:
                    if o["id"] in mine:
                        resting_by_ex[ex] = resting_by_ex.get(ex, 0.0) + float(o.get("priceLimit") or 0.0) * float(o["quantity"])
        resting = sum(resting_by_ex.values())
        free = max(0.0, min(budget_total - committed - resting, cash * 0.9 - resting))
        self.free_last = free
        self.unfunded = []                    # candidates this cycle could not fund (offload may free arb capital for them)
        self.unfunded_ts = time.time()
        cands = self.candidates(prices, exclude)
        summary["candidates"] = len(cands)
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=self.cfg.order_ttl_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to_place: list[dict] = []
        writes = 0
        per_market_cap = self.cfg.max_market_frac * self.starting_cash
        for ex, kind, mid, edge in cands[: self.cfg.max_markets]:
            p = prices[ex]
            if risk.adds(self.u, ex, "no" if kind == "longshot" else "yes") in self.blocked:
                continue
            if p.get("spread") is not None and p["spread"] > 0.03:
                continue                                     # wide, thin tail books are where tilt orders get baited
            touch = p["bestAsk"] if kind == "longshot" else p["bestBid"]
            st = self.state.setdefault(ex, TiltState())
            oo = open_orders.get(ex, [])
            moved = st.last_touch is None or abs(touch - st.last_touch) >= TICK - 1e-9
            stale = time.time() - st.last_ts > self.cfg.refresh_sec
            if oo and not moved and not stale:
                continue
            # requoting this market releases its own resting notional first
            free_here = free + resting_by_ex.get(ex, 0.0)
            want = per_market_cap - self.invested(ex, positions, mid)
            room = min(free_here, want)
            if room < 5:
                if want >= 50 and free_here < 5:
                    self.unfunded.append({"exchange_id": ex, "kind": kind, "edge": edge, "touch": touch, "capital": want})
                continue
            if writes + 1 + math.ceil((len(to_place) + 3) / 15) > write_budget:
                break
            if oo:
                if self.dry_run:
                    log.info("[DRY] tilt cancel-all %s (%d orders)", ex, len(oo))
                else:
                    try:
                        self.api.cancel_all(exchange_id=ex)
                        self.store.mark_orders([o["id"] for o in oo], "cancelled")
                    except ApiError as e:
                        log.warning("tilt cancel-all %s: %s", ex, e)
                        continue
                writes += 1
            st.last_touch, st.last_ts = touch, time.time()
            # ladder: at the touch, +1 tick, +2 ticks (better for us), with sizes increasing away from the touch
            levels = []
            for k, (dt, w) in enumerate(zip(self.cfg.level_ticks, self.cfg.level_weights)):
                if kind == "longshot":
                    yes_px = round_tick(touch + dt * TICK)          # we sell YES at yes_px = buy NO at 1 - yes_px
                    send = dict(side="no", price=round_tick(1 - yes_px), collateral=1 - yes_px)
                else:
                    yes_px = round_tick(touch - dt * TICK)          # we buy YES at yes_px
                    send = dict(side="yes", price=yes_px, collateral=yes_px)
                levels.append((k + 1, yes_px, w, send))
            wsum = sum(w for _, _, w, _ in levels) or 1.0
            for lvl, yes_px, w, send in levels:
                notional = room * w / wsum
                qty = int(notional / max(1e-6, send["collateral"]))
                if qty < 1:
                    continue
                to_place.append(dict(exchangeId=ex, side=send["side"], action="buy", quantity=qty, price=send["price"], expirationDate=ttl,
                                     tournamentId=self.api.tournament_id, _meta=dict(kind=kind, lvl=lvl, yes_px=yes_px, mid=mid, edge=edge)))
            free = free_here - room
            resting_by_ex[ex] = room
            summary["requoted"].append(ex)
        for i in range(0, len(to_place), 15):
            chunk = to_place[i : i + 15]
            orders = [{k: v for k, v in o.items() if k != "_meta"} for o in chunk]
            if self.dry_run:
                for o in chunk:
                    m = o["_meta"]
                    log.info("[DRY] tilt %s %s %s px=%.3f q=%d yes_px=%.3f mid=%.3f edge/collateral=%.1f%% lvl=%d", m["kind"], o["exchangeId"],
                             "BUY-NO" if o["side"] == "no" else "BUY-YES", o["price"], o["quantity"], m["yes_px"], m["mid"], 100 * m["edge"], m["lvl"])
                summary["orders"] += len(chunk)
                continue
            try:
                resp = self.api.place_batch(orders)
            except ApiError as e:
                log.warning("tilt batch failed: %s", e)
                continue
            writes += 1
            for r in resp.get("results", []):
                o = chunk[r["index"]]; d = r.get("data") or {}; m = o["_meta"]
                if r.get("ok") and d.get("orderId"):
                    self.store.record_order(d["orderId"], o["exchangeId"], None, o["side"], "buy", o["price"], o["quantity"], "tilt", m["lvl"],
                                            (m["yes_px"] - m["mid"]) / TICK, self.fair(m["mid"]), m["mid"], None, positions.get(o["exchangeId"], 0.0),
                                            cycle_id, ttl, "open" if d.get("open") else "filled", d)
                    summary["orders"] += 1
        summary["writes"] = writes
        return summary
