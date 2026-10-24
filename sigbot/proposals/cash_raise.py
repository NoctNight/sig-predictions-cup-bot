"""Cash raise (Sizhe 24 Oct): sell holdings that cost under 1% to exit, but only while a sleeve with more edge is waiting
for that cash. Never sells just to hold cash.

What may be sold: the quantity on a leg beyond any arb set (sell-sets: the common NO on every leg; buy-sets: the common
YES), i.e. MM, tilt and directional leftovers (~30.8k walkable at ~0.4% on 24 Oct). Never: the sets themselves, races in
`exclude_races` (Rhode Island Governor), races where high-frequency arb holds lots, legs the catch-up sweep or cheap
favourites hold or have orders on, legs holding more than `hold_limit` shares.

When: a sleeve posted a cash need this cycle (Bot.post_cash_need: high-frequency arb blocked by its cash floor, cheap
favourites short of cash; shock mode can post one too). The raise is the posted shortfall plus a small buffer, capped by
what the routine may still raise in 24 h, and each clip is sized so the cycle never raises more than that.

How: cheapest first, one clip per leg per cycle at the touch (at most 80% of the best bid / ask size, never more than
the leg's eligible quantity), only when the clip's cost against the mid is below `max_cost` (1%): the limit is the touch
price, so no clip walks past the bound. Net D stays inside the partisan cap after every clip; near the cap, clips that
pull net D back toward zero go first. Pure logic here; the Bot wires books, orders and the MM pause.

Tier 2 (Sizhe 24 Oct: "holding something for 3 days vs doing multiple HF arbs with the same capital each with some EV
should usually be an easy choice"), off unless raise_sets_enabled: whole arb sets / buy-sets (Rhode Island Governor,
MN-05, Hawaii, small sets), all legs at once, all-or-none, only when (a) tier 1 cannot cover the shortfall, (b) high-
frequency arb skipped qualifying entries for cash in >= raise_set_min_skips distinct half-hours of the last 6 h (a
one-cycle burst is one), and (c) the set's exit
now costs <= raise_set_max_cost (4.5%: what the 31 Oct-3 Nov wind-down is expected to cost anyway) of its proceeds, never
in a race we plan to hold through an early call (raise_set_hold_races). Pieces sized to the touch, only as many sets as the
shortfall needs, cheapest first.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

CLIP_FRAC = 0.8                 # take at most 80% of the touch size (the order fills at once as a taker)


def beyond_sets(race_legs: dict, positions: dict) -> dict:
    """{exchange: signed quantity beyond any arb set} (YES positive, NO negative). race_legs: race -> [exchange ids]."""
    out = {}
    for race, exs in race_legs.items():
        qs = [positions.get(e, 0.0) for e in exs]
        common = 0.0
        if len(exs) >= 2 and all(q < 0 for q in qs):
            common = -min(-q for q in qs)          # sell-sets: common NO (negative)
        elif len(exs) >= 2 and all(q > 0 for q in qs):
            common = min(qs)                       # buy-sets: common YES
        for e, q in zip(exs, qs):
            r = q - common
            if abs(r) >= 1:
                out[e] = r
    return out


def eligible(race_legs: dict, positions: dict, exclude_races=(), exclude_ex=(), hold_limit: float = 4000.0) -> dict:
    """{exchange: signed quantity the raise may sell} after every exclusion."""
    skip_ex = set(exclude_ex)
    legs = {r: exs for r, exs in race_legs.items() if r not in set(exclude_races)}
    out = {}
    for e, r in beyond_sets(legs, positions).items():
        if e in skip_ex or abs(positions.get(e, 0.0)) > hold_limit:
            continue
        out[e] = int(r) if r > 0 else -int(-r)
    return out


def clip(ex: str, qty: int, book: Optional[dict], max_cost: float, min_proceeds: float = 0.0) -> Optional[dict]:
    """The clip that sells up to |qty| of a holding at the touch: a YES holding is sold by buying NO at 1 - best bid, a
    NO holding by buying YES at the best ask. None when the book is missing, the clip costs >= max_cost of the mid, or it
    could fetch less than min_proceeds (crumbs cost a cancel and an MM pause on the leg for almost no cash)."""
    if not book or not book.get("bids") or not book.get("asks") or qty == 0:
        return None
    bb, ba = book["bids"][0], book["asks"][0]
    mid = (bb["price"] + ba["price"]) / 2
    if qty > 0:                                    # long YES: sell at the bid
        pps, value, size, side, px = bb["price"], mid, bb["quantity"], "no", round(1 - bb["price"], 3)
    else:                                          # long NO: sell at the ask (proceeds 1 - ask per share)
        pps, value, size, side, px = 1 - ba["price"], 1 - mid, ba["quantity"], "yes", round(ba["price"], 3)
    if value <= 0 or pps <= 0:
        return None
    cost = (value - pps) / value
    if cost >= max_cost - 1e-12:
        return None
    q = int(min(abs(qty), CLIP_FRAC * float(size or 0)))
    if q < 1 or q * pps < min_proceeds:
        return None
    return {"exchange_id": ex, "side": side, "price": px, "qty": q, "pps": pps, "mid": mid, "cost_frac": cost,
            "holding": "YES" if qty > 0 else "NO", "sign": 1 if qty > 0 else -1}


def plan(elig: dict, books: dict, target: float, max_cost: float, net_d: Callable[[dict], float], cap: float,
         positions: dict, margin: float = 1000.0, max_clips: int = 4, min_proceeds: float = 0.0) -> list:
    """Clips to send now, raising at most `target` (cash): cheapest first; when net D is within `margin` of the cap,
    clips that move net D toward zero first; a clip that would leave |net D| above the cap (or raise it when already
    above) is skipped. net_d(positions) -> net D notional; positions are updated clip by clip."""
    if target <= 0:
        return []
    pos = dict(positions)
    cands = [c for c in (clip(e, q, books.get(e), max_cost, min_proceeds) for e, q in elig.items()) if c]
    out, left = [], float(target)
    nd = net_d(pos)
    while cands and left > 0.5 and len(out) < max_clips:
        def after(c, q):
            p2 = dict(pos)
            p2[c["exchange_id"]] = p2.get(c["exchange_id"], 0.0) - c["sign"] * q
            return net_d(p2), p2
        near = abs(nd) >= cap - margin
        scored = []
        for c in cands:
            q = min(c["qty"], max(1, math.ceil(left / c["pps"])))      # never raise more than the target
            if q * c["pps"] < min_proceeds:
                continue                                              # trimmed to a crumb: not worth a pause on the race
            d2, p2 = after(c, q)
            if abs(d2) > cap + 1e-6 and abs(d2) >= abs(nd) - 1e-6:
                continue                                              # would break (or widen a breach of) the cap
            toward = abs(d2) < abs(nd) - 1e-9
            scored.append(((0 if (toward or not near) else 1, c["cost_frac"], c["exchange_id"]), c, q, d2, p2))
        if not scored:
            break
        scored.sort(key=lambda s: s[0])
        _, c, q, d2, p2 = scored[0]
        out.append(dict(c, qty=q, proceeds=q * c["pps"], cost=q * (c["mid"] - c["pps"]) if c["sign"] > 0 else q * ((1 - c["mid"]) - c["pps"]),
                        net_d_after=d2))
        left -= q * c["pps"]
        pos, nd = p2, d2
        cands = [x for x in cands if x["exchange_id"] != c["exchange_id"]]   # one clip per leg per cycle (touch consumed)
    return out


def tier1_available(elig: dict, books: dict, max_cost: float) -> float:
    """Cash tier 1 could raise over the coming cycles: every eligible holding whose touch exit costs < max_cost, valued at
    the touch (tier 2 waits while this covers the shortfall)."""
    tot = 0.0
    for e, q in elig.items():
        c = clip(e, q, books.get(e), max_cost)
        if c:
            tot += abs(q) * c["pps"]
    return tot


def set_pieces(race_legs: dict, positions: dict, books: dict, target: float, max_cost: float, exclude=(),
               max_pieces: int = 2, min_proceeds: float = 0.0) -> list:
    """Tier 2: pieces of whole sets to unwind now, cheapest first, raising at most `target`. A sell-set (NO on every leg,
    pays legs - 1) is unwound by buying YES on every leg at the asks (proceeds legs - sum of asks per set); a buy-set (YES
    on every leg, pays 1) by buying NO on every leg at 1 - bid (proceeds = sum of bids). cost_frac = (payout - proceeds) /
    proceeds at the touch, which must be <= max_cost; a piece is at most 80% of the thinnest leg's touch and the sets held."""
    cands = []
    skip = set(exclude)
    for race, exs in race_legs.items():
        if race in skip or len(exs) < 2:
            continue
        qs = [positions.get(e, 0.0) for e in exs]
        if all(q < 0 for q in qs):
            kind, k, payout = "sell_set", int(min(-q for q in qs)), float(len(exs) - 1)
        elif all(q > 0 for q in qs):
            kind, k, payout = "buy_set", int(min(qs)), 1.0
        else:
            continue
        bks = [books.get(e) for e in exs]
        if k < 1 or any(not b or not b.get("bids") or not b.get("asks") for b in bks):
            continue
        tops = [b["asks"][0] if kind == "sell_set" else b["bids"][0] for b in bks]
        pps = (len(exs) - sum(t["price"] for t in tops)) if kind == "sell_set" else sum(t["price"] for t in tops)
        if pps <= 0:
            continue
        cost = (payout - pps) / pps
        q = int(min(k, CLIP_FRAC * min(float(t["quantity"] or 0) for t in tops)))
        if cost > max_cost + 1e-12 or q < 1 or q * pps < min_proceeds:
            continue
        cands.append({"race": race, "kind": kind, "qty": q, "held": k, "pps": pps, "payout": payout, "cost_frac": cost})
    cands.sort(key=lambda c: (c["cost_frac"], c["race"]))
    out, left = [], float(target)
    for c in cands:
        if left <= 0.5 or len(out) >= max_pieces:
            break
        q = min(c["qty"], max(1, math.ceil(left / c["pps"])))
        if q * c["pps"] < min_proceeds:
            continue                                   # trimmed to a crumb (review 4): not worth a pause on the race
        out.append(dict(c, qty=q, proceeds=q * c["pps"], cost=q * (c["payout"] - c["pps"])))
        left -= q * c["pps"]
    return out


def exit_cost_median(rows, race: str, n_legs: int, now: float, min_hours: float = 12.0) -> Optional[float]:
    """The race's trailing 24-h median exit cost per set (sum of the legs' asks - 1, i.e. payout - proceeds of unwinding a
    sell-set at the touch), from venue_quotes rows (race, party, ts, ask), each leg's last ask carried forward up to 30 min
    on a 5-min grid (dev_churn.overround_series). None with less than `min_hours` of history ("level 0" needs a median)."""
    from .dev_churn import overround_series
    ser = overround_series(rows, {race: n_legs}, now - 86400.0, now).get(race) or []
    if len(ser) < int(min_hours * 12) or now - ser[0][0] < min_hours * 3600.0 - 600.0:
        return None
    xs = sorted(v for _, v in ser)
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else 0.5 * (xs[m - 1] + xs[m])


def trim_qty(held: int, need: float, pps: float, trimmed: float, cap: float, clip_max: int) -> int:
    """Sets to unwind in one RI trim clip: at most clip_max, the sets held, what is left of the cap, and only as many as
    the cash need takes (rounded up)."""
    if pps <= 0 or need <= 0:
        return 0
    return int(max(0, min(clip_max, held, cap - trimmed, math.ceil(need / pps))))
