"""Correlation / relative-value sleeve (paper): swing-neutral pairs and chamber-vs-seats trades from sigbot.correlation_model.

    python3 -m sigbot.proposals.correlation_arb --backtest      # do the pair gaps mean-revert before settlement? markouts
    python3 -m sigbot.proposals.correlation_arb --paper         # one paper cycle on the latest logged prices (no API, no orders)

What it trades (all model-dependent except the set/bound arbs, which the live arb sleeve already takes):
  * pairs: long the D leg whose venue price is below fair and short the D leg (= buy the R leg) whose venue price is above
    fair, with the hedge ratio h = swing_a / swing_b from the factor model so the national-swing P&L nets to ~0;
  * chamber: U.S. Senate / U.S. House D leg versus the model's P(D control) from the fair seat marginals.
  Sizing: 300 notional per leg and 3,000 total for everything that is not a bound violation; partisan net-D notional cap 5,000
  (both legs of a swing-neutral pair count, with opposite signs, so pairs are nearly net-D flat by construction); every order
  rests at the touch (join the bid of the side we buy) with a 60 s TTL unless the main loop manages it.

Backtest (--backtest): replays the 5-minute grid of SIG candles + Polymarket/Kalshi history (data/candles_5m.json,
data/model_history.json; 14-21 Oct) and the direction loop's venue_fair rows (21-22 Oct, 1-2 min polls). For every pair the
model proposes today, the gap g_t = (sig_a - outside_a) - h (sig_b - outside_b) is the venue's relative mispricing (outside
blend as the moving fair; the model's own fair is static). It reports the AR(1) persistence of g at 1 h / 6 h / 24 h, the
pooled slope of the gap change on the gap level (half-life), and markouts of entering against the gap when |g| >= thr and
marking at the mid later. Settlement P&L cannot be backtested before Nov 3: the EV column of the report is the model's.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)
from sigbot import correlation_model as cm  # noqa: E402

TICK = 0.005
CAPS = {"per_leg_notional": 300.0, "total_notional": 3000.0, "net_d_cap": 5000.0, "worst_case_cap": 50000.0}


# ============================================================ paper cycle
class CorrelationArb:
    def __init__(self, model: dict | None = None, caps: dict | None = None, min_ev_touch: float = 0.03, min_ev_over_sd: float = 0.3,
                 max_pairs: int = 6, chamber_min_gap: float = 0.10, ttl_sec: int = 60):
        self.model = model or cm.load_model()
        self.caps = dict(CAPS, **(caps or {}))
        self.min_ev_touch, self.min_ev_over_sd, self.max_pairs, self.chamber_min_gap, self.ttl = min_ev_touch, min_ev_over_sd, max_pairs, chamber_min_gap, ttl_sec
        self.U = cm.load_universe()
        self.last_chamber = None

    # ---- exposure bookkeeping (same conventions as sigbot.risk: + = long D notional)
    def _exposure(self, prices: dict, positions: dict) -> tuple[float, dict]:
        net = 0.0
        per_race = defaultdict(float)
        for race, u in self.U.items():
            for party, ex in u["legs"].items():
                q = positions.get(ex, 0.0)
                if not q or party not in ("D", "R"):
                    continue
                p = prices.get(ex) or {}
                bb, ba = p.get("bestBid"), p.get("bestAsk")
                price = 0.5 * (bb + ba) if bb is not None and ba is not None else (p.get("latestPrice") or 0.5)
                sn = q * price if q > 0 else q * (1 - price)
                d = sn if party == "D" else -sn
                per_race[race] += d
                net += d
        return net, per_race

    def _apply_prices(self, prices: dict) -> None:
        for u in self.U.values():
            for party, ex in u["legs"].items():
                p = prices.get(ex)
                if not p:
                    continue
                v = u["venue"].setdefault(party, {})
                v["bid"], v["ask"] = p.get("bestBid"), p.get("bestAsk")
                if v["bid"] is not None and v["ask"] is not None:
                    v["mid"] = 0.5 * (v["bid"] + v["ask"])
                elif p.get("latestPrice") is not None:
                    v["mid"] = p["latestPrice"]

    def cycle(self, prices: dict, positions: dict, cash: float | None = None) -> list[dict]:
        """prices: {exchange_id: {bestBid, bestAsk, latestPrice}}; positions: {exchange_id: qty (+YES, -YES=NO)}.
        Returns paper orders [{exchangeId, side, action, quantity, price, ttl_sec, reason, riskless, notional}] within the caps."""
        self._apply_prices(prices)
        net_d, _ = self._exposure(prices, positions)
        orders: list[dict] = []
        spent = 0.0
        held_notional = sum(abs(q) * ((prices.get(ex) or {}).get("latestPrice") or 0.5) for ex, q in positions.items())
        budget = max(0.0, self.caps["total_notional"] - min(held_notional, self.caps["total_notional"]))
        # 1. riskless set / bound arbs are the live arb sleeve's job; report them as zero-size flags so the caller sees them
        for a in cm.set_arbs(self.U):
            orders.append({"exchangeId": None, "side": None, "action": "flag", "quantity": 0, "price": None, "riskless": True,
                           "reason": f"set arb {a['race']} {a['kind']} edge {a['edge']:+.3f} (live arb sleeve)"})
        for b in cm.chamber_bounds(self.U):
            if b["edge"] > 0:
                orders.append({"exchangeId": None, "side": None, "action": "flag", "quantity": 0, "price": None, "riskless": True,
                               "reason": f"chamber bound violated: {b['bound']} edge {b['edge']:+.3f}: {b['portfolio']}"})
        # 2. swing-neutral pairs
        pairs = [p for p in cm.relative_value(self.model, self.U, max_pairs=40) if p["ev_per_share_touch"] >= self.min_ev_touch and p["ev_over_sd_touch"] >= self.min_ev_over_sd]
        used_races: set = set()
        for p in pairs:
            if len(used_races) // 2 >= self.max_pairs:
                break
            if p["long_d"] in used_races or p["short_d"] in used_races:
                continue
            ua, ub = self.U[p["long_d"]], self.U[p["short_d"]]
            legs = self._pair_legs(ua, ub)
            if not legs:
                continue
            per_leg = min(self.caps["per_leg_notional"], budget - spent)
            if per_leg < 20:
                break
            qa = int(per_leg / legs[0]["price"])
            qb = int(min(qa * p["hedge_ratio"], per_leg / legs[1]["price"]))
            if qa < 1 or qb < 1:
                continue
            dd = legs[0]["d_sign"] * qa * legs[0]["price"] + legs[1]["d_sign"] * qb * legs[1]["price"]
            if abs(net_d + dd) > self.caps["net_d_cap"]:
                continue
            for leg, q in ((legs[0], qa), (legs[1], qb)):
                orders.append({"exchangeId": leg["ex"], "side": leg["side"], "action": "buy", "quantity": q, "price": leg["price"], "ttl_sec": self.ttl,
                               "riskless": False, "notional": round(q * leg["price"], 1),
                               "reason": f"pair {p['long_d']} / {p['short_d']} h {p['hedge_ratio']} rho {p['rho_latent']} EV/sh {p['ev_per_share_touch']:+.3f} sd {p['residual_sd']}"})
                spent += q * leg["price"]
            net_d += dd
            used_races.update((p["long_d"], p["short_d"]))
        # 3. chamber vs seats (one small position per chamber market)
        if budget - spent >= 100:
            for o in self._chamber_orders(net_d, min(self.caps["per_leg_notional"], budget - spent)):
                orders.append(o)
                spent += o["notional"]
        return orders

    def _pair_legs(self, ua: dict, ub: dict) -> list[dict] | None:
        """Long D in a = buy a's D YES at its bid; short D in b = buy b's R YES at its bid (both rest at the touch)."""
        exa, exb = ua["legs"].get("D"), ub["legs"].get("R")
        va, vb = ua["venue"].get("D", {}), ub["venue"].get("R", {})
        if exa is None or exb is None or va.get("bid") is None or vb.get("bid") is None:
            return None
        return [{"ex": exa, "side": "yes", "price": round(va["bid"], 3), "d_sign": +1},
                {"ex": exb, "side": "yes", "price": round(vb["bid"], 3), "d_sign": -1}]

    def _chamber_orders(self, net_d: float, per_leg: float) -> list[dict]:
        out = []
        if self.last_chamber is None or time.time() - self.last_chamber[0] > 1800:
            pf = cm.price_chambers(self.model, self.U, "fair", 4000)
            self.last_chamber = (time.time(), pf)
        pf = self.last_chamber[1]
        for name, key in (("U.S. Senate", "senate"), ("U.S. House", "house")):
            u = self.U.get(name)
            pm = pf.get(key, {}).get("p_d_model")
            if not u or pm is None:
                continue
            vd, vr = u["venue"].get("D", {}), u["venue"].get("R", {})
            if vd.get("bid") is None or vr.get("bid") is None:
                continue
            vm = cm.venue_d_mid(u)
            gap = pm - vm
            if abs(gap) < self.chamber_min_gap:
                continue
            if gap > 0:      # model says D control is underpriced: buy D at the D bid
                ex, px, side, dsign = u["legs"]["D"], vd["bid"], "yes", +1
            else:            # overpriced: buy R at the R bid
                ex, px, side, dsign = u["legs"]["R"], vr["bid"], "yes", -1
            q = int(per_leg / px)
            if q < 1 or abs(net_d + dsign * q * px) > self.caps["net_d_cap"]:
                continue
            out.append({"exchangeId": ex, "side": side, "action": "buy", "quantity": q, "price": round(px, 3), "ttl_sec": self.ttl, "riskless": False,
                        "notional": round(q * px, 1), "reason": f"{name}: model P(D) {pm:.3f} vs venue {vm:.3f} (fair seat marginals, factor model)"})
        return out


# ============================================================ backtest
def _grid_series() -> tuple[list, dict]:
    """5-minute grid: {race: {'sig': [...], 'out': [...]}} from the offline files, then the venue_fair rows appended."""
    from sigbot import direction_model as dm
    hist = json.load(open(dm.HIST_PATH))
    grid, data = dm.build_grid_offline(hist)
    series = {}
    for race, d in data.items():
        sig = d.get("sig")
        pm, ks = d.get("pm"), d.get("kalshi")
        if not sig or not (pm or ks):
            continue
        out = []
        for i in range(len(grid)):
            vals = [v[i] for v in (pm, ks) if v and v[i] is not None]
            out.append(sum(vals) / len(vals) if vals else None)
        series[race] = {"sig": list(sig), "out": out}
    con = sqlite3.connect(f"file:{os.path.join(HERE, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
    rows = con.execute("SELECT ts, race, pm, kalshi, sig_mid FROM venue_fair WHERE party='D' AND ts > ? ORDER BY ts", (grid[-1],)).fetchall()
    con.close()
    if rows:
        g2 = list(range(grid[-1] + 300, int(rows[-1][0]) + 300, 300))
        buck: dict = defaultdict(dict)
        for ts, race, pm, k, sm in rows:
            b = int(ts // 300 * 300 + 300)
            o = [v for v in (pm, k) if v is not None]
            if sm is not None and o:
                buck[race][b] = (sm, sum(o) / len(o))
        for race in series:
            last = None
            for b in g2:
                last = buck[race].get(b, last)
                series[race]["sig"].append(last[0] if last else None)
                series[race]["out"].append(last[1] if last else None)
        grid = grid + g2
    return grid, series


def _ar1(xs: list, lag: int):
    pairs = [(xs[i], xs[i + lag]) for i in range(len(xs) - lag) if xs[i] is not None and xs[i + lag] is not None]
    if len(pairs) < 20:
        return None, len(pairs)
    mx = statistics.mean(p[0] for p in pairs)
    my = statistics.mean(p[1] for p in pairs)
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    return (sxy / sxx if sxx > 0 else None), len(pairs)


def backtest(thr: float = 0.03, horizons=(12, 72, 288)) -> dict:
    model = cm.load_model()
    U = cm.load_universe()
    cm.load_venue_prices(U)
    pairs = cm.relative_value(model, U, max_pairs=15) + cm.relative_value(model, U, competitive=True, max_pairs=8)
    grid, series = _grid_series()
    out = {"grid_bars": len(grid), "days": round((grid[-1] - grid[0]) / 86400, 2), "pairs": []}
    pooled = defaultdict(list)
    markouts = defaultdict(list)
    seen = set()
    for p in pairs:
        a, b, h = p["long_d"], p["short_d"], p["hedge_ratio"]
        if (a, b) in seen or a not in series or b not in series:
            continue
        seen.add((a, b))
        sa, oa, sb, ob = series[a]["sig"], series[a]["out"], series[b]["sig"], series[b]["out"]
        g = [None if None in (sa[i], oa[i], sb[i], ob[i]) else (sa[i] - oa[i]) - h * (sb[i] - ob[i]) for i in range(len(grid))]
        vals = [x for x in g if x is not None]
        if len(vals) < 50:
            continue
        row = {"pair": f"{a} / {b}", "h": h, "n": len(vals), "gap_mean": round(statistics.mean(vals), 4), "gap_sd": round(statistics.pstdev(vals), 4),
               "gap_now": round(vals[-1], 4)}
        for lag in horizons:
            rho, _ = _ar1(g, lag)
            row[f"ar1_{lag * 5}m"] = None if rho is None else round(rho, 3)
            for i in range(len(g) - lag):
                if g[i] is not None and g[i + lag] is not None:
                    pooled[lag].append((g[i], g[i + lag] - g[i]))
                    if abs(g[i]) >= thr:
                        markouts[lag].append(-math.copysign(1.0, g[i]) * (g[i + lag] - g[i]))
        ends = [-math.copysign(1.0, g[i]) * (vals[-1] - g[i]) for i in range(len(g) - 1) if g[i] is not None and abs(g[i]) >= thr]
        row["entries_thr"] = len(ends)
        row["markout_to_end_mean"] = round(statistics.mean(ends), 4) if ends else None
        out["pairs"].append(row)
    out["pooled"] = {}
    for lag in horizons:
        pts = pooled[lag]
        if len(pts) < 50:
            continue
        mx = statistics.mean(x for x, _ in pts)
        my = statistics.mean(y for _, y in pts)
        sxx = sum((x - mx) ** 2 for x, _ in pts)
        sxy = sum((x - mx) * (y - my) for x, y in pts)
        slope = sxy / sxx if sxx > 0 else None
        mk = markouts[lag]
        out["pooled"][f"{lag * 5}m"] = {"n": len(pts), "slope_dgap_on_gap": None if slope is None else round(slope, 3),
                                         "half_life_bars": None if slope is None or slope >= 0 or slope <= -1 else round(-math.log(2) / math.log(1 + slope), 1),
                                         "markout_mean_pts": round(100 * statistics.mean(mk), 3) if mk else None,
                                         "markout_hit": round(sum(1 for m in mk if m > 0) / len(mk), 3) if mk else None, "entries": len(mk)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="correlation / relative-value sleeve (paper) and its backtest")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--paper", action="store_true")
    ap.add_argument("--thr", type=float, default=0.03)
    a = ap.parse_args(argv)
    if a.backtest:
        r = backtest(a.thr)
        print(f"grid {r['grid_bars']} bars over {r['days']} days; gap = (sig_a - outside_a) - h (sig_b - outside_b); entries at |gap| >= {a.thr}")
        print(f"  {'pair':58s} {'h':>5s} {'n':>5s} {'mean':>7s} {'sd':>6s} {'now':>7s} {'ar1 1h':>7s} {'ar1 6h':>7s} {'ar1 24h':>8s} {'to-end':>7s}")
        for p in r["pairs"]:
            print(f"  {p['pair'][:58]:58s} {p['h']:5.2f} {p['n']:5d} {p['gap_mean']:+7.3f} {p['gap_sd']:6.3f} {p['gap_now']:+7.3f} "
                  f"{str(p.get('ar1_60m')):>7s} {str(p.get('ar1_360m')):>7s} {str(p.get('ar1_1440m')):>8s} {str(p.get('markout_to_end_mean')):>7s}")
        print("pooled:", json.dumps(r["pooled"], indent=1))
        json.dump(r, open(os.path.join(HERE, "params", "correlation_arb_backtest.json"), "w"), indent=1)
    if a.paper or not a.backtest:
        s = CorrelationArb()
        cm.load_venue_prices(s.U)
        prices = {}
        for u in s.U.values():
            for party, ex in u["legs"].items():
                v = u["venue"].get(party, {})
                prices[ex] = {"bestBid": v.get("bid"), "bestAsk": v.get("ask"), "latestPrice": v.get("mid") or v.get("last")}
        orders = s.cycle(prices, {})
        tot = sum(o.get("notional", 0) for o in orders)
        print(f"paper cycle: {len(orders)} orders, {tot:.0f} notional (caps {s.caps})")
        for o in orders:
            print(f"  {str(o['exchangeId']):>5s} {str(o['side']):>4s} {o['action']:4s} q {o['quantity']:5d} @ {o['price']}  {o['reason']}")


if __name__ == "__main__":
    main()
