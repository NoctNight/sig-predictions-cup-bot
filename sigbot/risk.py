"""Portfolio exposure limits.

Directional risk on this venue is partisan and correlated: being long "D wins" in many races (or short "R wins",
which is the same bet) is one national-swing position. We measure it as net D-notional at risk:

    for a D leg:  +q * price  if long YES (q > 0),   q * (1 - price) if short (q < 0, i.e. long NO)   -> signed
    for an R leg: the opposite sign
    I legs:       ignored on the partisan axis (they still count in the per-race cap)

Arb sets (NO on every leg of a race) are neutral and are netted out first: the common NO quantity is removed from
each leg before the exposure is computed. The remainder is market-making, tilt and hedge inventory.

`blocked_directions(...)` returns which partisan direction may no longer be added to; `adds(ex, side)` says which
direction an order on exchange `ex` buying `side` adds to, so the quoting sleeves can drop those orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .universe import Universe


@dataclass
class Exposure:
    net_d_notional: float          # + = net long Democrats (or short Republicans)
    long_d: float
    short_d: float
    per_race: dict                 # race -> signed D-notional


def _signed_notional(q: float, price: float) -> float:
    if q > 0:
        return q * price
    return q * (1 - price)        # negative: short YES = long NO, at risk (1-p) per share


def net_positions_ex_arb(universe: Universe, positions: dict[str, float]) -> dict[str, float]:
    """Remove held arb sets: the common NO quantity on every leg of a race (sell-sets) and the common YES quantity on
    every leg (buy-sets). Either pays a fixed amount whoever wins, so neither is partisan exposure (a buy-set used to
    count as up to 0.4 x qty of exposure toward the underdog: sim_pipeline sweep6, 22 Oct)."""
    out = dict(positions)
    for race in universe.races.values():
        qs = [out.get(l.exchange_id, 0.0) for l in race.legs.values()]
        if len(qs) >= 2 and all(q < 0 for q in qs):
            common = max(qs)       # least negative = the common NO quantity
        elif len(qs) >= 2 and all(q > 0 for q in qs):
            common = min(qs)       # the common YES quantity
        else:
            continue
        for l in race.legs.values():
            out[l.exchange_id] = out.get(l.exchange_id, 0.0) - common
    return out


def exposure(universe: Universe, positions: dict[str, float], prices: dict[str, dict]) -> Exposure:
    net = net_positions_ex_arb(universe, positions)
    per_race: dict[str, float] = {}
    long_d = short_d = 0.0
    for ex, q in net.items():
        if not q:
            continue
        r = universe.by_exchange.get(ex)
        if not r:
            continue
        race, party = r
        p = prices.get(ex) or {}
        bb, ba = p.get("bestBid"), p.get("bestAsk")
        price = 0.5 * (bb + ba) if bb is not None and ba is not None else (p.get("latestPrice") or 0.5)
        sn = _signed_notional(q, price)
        if party == "D":
            d = sn
        elif party == "R":
            d = -sn
        else:
            continue
        per_race[race] = per_race.get(race, 0.0) + d
        if d > 0:
            long_d += d
        else:
            short_d += -d
    return Exposure(long_d - short_d, long_d, short_d, per_race)


def adds(universe: Universe, ex: str, side: str) -> Optional[str]:
    """Direction ('D' or 'R') an order buying `side` on `ex` adds exposure to; None for Independent legs."""
    r = universe.by_exchange.get(ex)
    if not r:
        return None
    party = r[1]
    if party == "D":
        return "D" if side == "yes" else "R"
    if party == "R":
        return "R" if side == "yes" else "D"
    return None


def blocked_directions(expo: Exposure, cap: float) -> set:
    """Directions that may not be added to. Over the cap on the long-D side blocks 'D' (no more D-yes / R-no)."""
    out = set()
    if expo.net_d_notional >= cap:
        out.add("D")
    if expo.net_d_notional <= -cap:
        out.add("R")
    return out
