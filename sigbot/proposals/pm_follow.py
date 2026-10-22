"""Polymarket-move follower (paper): take the SIG touch the moment Polymarket's book moves and SIG's has not, hold while
the DMM catches up (minutes to hours), exit at the touch. An event-time upgrade of dmm_lag_follow.py: one trade per race
per signal (the D and R legs are the same bet), entry from the Polymarket WebSocket instead of 2-minute polls, size capped
by the SIG touch, and a walk-forward test.

    python3 -m sigbot.proposals.pm_follow --backtest                       # DB polls (venue_quotes), fit first half / test second half
    python3 -m sigbot.proposals.pm_follow --replay sigbot/proposals/hft_data/rec_*.jsonl   # second-resolution replay of a recording

Signal: outside mid (Polymarket; the Polymarket/Kalshi average when both exist and agree within 4 pts) moved >= move
within window_sec, SIG mid moved < 1/3 of it, SIG spread <= max_spread_ticks. Trade the race's reference leg (D, else the first
party) in the direction of the move (buy YES at the ask on an up move, buy NO at 1 - bid on a down move), quantity
min(qty, touch size). Exit: when SIG mid has closed >= exit_frac of the entry gap, or at hold_sec, crossing the touch.
Reported per trade: points per share net of both crossings, and the mid-to-mid move (gross of spread).
`PMFollower.on_outside(ts, ex, outside_mid, sig_book)` returns a paper order; nothing is sent.
"""
from __future__ import annotations

import argparse, bisect, collections, json, os, sqlite3, statistics, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TICK = 0.005


class PMFollower:
    def __init__(self, move=0.015, window_sec=300, qty=1000, max_spread_ticks=2, cooldown_sec=1800):
        self.move, self.window_sec, self.qty, self.max_spread, self.cooldown = move, window_sec, qty, max_spread_ticks, cooldown_sec
        self.hist = collections.defaultdict(list)   # race -> [(ts, outside, sig_mid)] for the reference leg
        self.last = {}

    def on_outside(self, ts, race, outside, sig_bid, sig_ask, sig_bid_sz=None, sig_ask_sz=None):
        """Feed one observation of a race's reference leg (YES of the first party). Returns paper order or None."""
        if None in (outside, sig_bid, sig_ask):
            return None
        mid = (sig_bid + sig_ask) / 2
        h = self.hist[race]
        h.append((ts, outside, mid))
        while h and h[0][0] < ts - self.window_sec:
            h.pop(0)
        if ts - self.last.get(race, -1e18) < self.cooldown or len(h) < 2:
            return None
        d_out, d_sig = outside - h[0][1], mid - h[0][2]
        if abs(d_out) < self.move - 1e-9 or abs(d_sig) >= abs(d_out) / 3:
            return None
        if round((sig_ask - sig_bid) / TICK) > self.max_spread:
            return None
        up = d_out > 0
        if (up and sig_ask >= outside) or (not up and sig_bid <= outside):
            return None   # SIG is already at or past the outside price
        self.last[race] = ts
        size = sig_ask_sz if up else sig_bid_sz
        q = int(min(self.qty, size)) if size else self.qty
        return {"race": race, "dir": 1 if up else -1, "price": sig_ask if up else sig_bid, "quantity": q,
                "gap": outside - mid, "why": f"outside {d_out:+.3f} in {self.window_sec:.0f}s, sig {d_sig:+.3f}"}


def db_series():
    c = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
    from sigbot.universe import Universe
    uni = Universe.from_markets(json.load(open(os.path.join(ROOT, "data", "markets.json"))))
    ref = {}
    for name, r in uni.races.items():
        party = "D" if "D" in r.legs else sorted(r.legs)[0]
        ref[name] = (party, r.legs[party].exchange_id)
    out = collections.defaultdict(dict)     # race -> ts -> dict
    for ts, venue, race, party, inst, bid, ask, mid, bsz, asz in c.execute(
            "select ts, venue, race, party, instrument, bid, ask, mid, bid_sz, ask_sz from venue_quotes"):
        if race not in ref or party != ref[race][0]:
            continue
        row = out[race].setdefault(ts, {})
        if venue == "sig":
            row.update(sig_bid=bid, sig_ask=ask, bsz=bsz, asz=asz)
        elif mid is not None and ask is not None and bid is not None and ask - bid <= 0.10:
            row[venue] = mid
    return out


def outside_of(row):
    pm, k = row.get("polymarket"), row.get("kalshi")
    if pm is not None and k is not None:
        return (pm + k) / 2 if abs(pm - k) <= 0.04 else None
    return pm if pm is not None else k


def run(series, t_lo, t_hi, move, window, hold, exit_frac, qty=1000):
    f = PMFollower(move=move, window_sec=window, qty=qty)
    trades = []
    for race, rows in series.items():
        ts_list = sorted(rows)
        for i, ts in enumerate(ts_list):
            r = rows[ts]
            o = f.on_outside(ts, race, outside_of(r), r.get("sig_bid"), r.get("sig_ask"), r.get("bsz"), r.get("asz"))
            if not o or not (t_lo <= ts < t_hi):
                continue
            entry_mid = (r["sig_bid"] + r["sig_ask"]) / 2
            ex_px = ex_mid = None
            for ts2 in ts_list[i + 1:]:
                r2 = rows[ts2]
                if r2.get("sig_bid") is None or r2.get("sig_ask") is None:
                    continue
                m2 = (r2["sig_bid"] + r2["sig_ask"]) / 2
                closed = (m2 - entry_mid) / o["gap"] if o["gap"] else 0
                if closed >= exit_frac or ts2 - ts >= hold:
                    ex_px = r2["sig_bid"] if o["dir"] > 0 else r2["sig_ask"]
                    ex_mid = m2
                    break
            if ex_px is None:
                continue
            trades.append(dict(o, ts=ts, net=(ex_px - o["price"]) * o["dir"], gross=(ex_mid - entry_mid) * o["dir"]))
    return trades


def summary(tr, days):
    if not tr:
        return "n 0"
    net = [t["net"] for t in tr]
    s = sum(t["net"] * t["quantity"] for t in tr)
    return (f"n {len(tr):3d} ({len(tr) / days:5.1f}/day) net {statistics.mean(net) * 100:+.2f} pts/share (hit {sum(x > 0 for x in net)}/{len(net)}), "
            f"gross mid move {statistics.mean(t['gross'] for t in tr) * 100:+.2f} pts, SUSQies {s:+.0f} ({s / days:+.0f}/day), "
            f"median qty {statistics.median(t['quantity'] for t in tr):.0f}")


def backtest():
    ser = db_series()
    allts = sorted({ts for v in ser.values() for ts in v})
    t0, t1 = allts[0], allts[-1]
    mid_t = t0 + (t1 - t0) / 2
    d_half = (t1 - t0) / 2 / 86400
    print(f"polls {len(allts)}, {(t1 - t0) / 3600:.1f} h, races {len(ser)}; fit = first half, test = second half")
    grid = [(m, w, h, x) for m in (0.01, 0.015, 0.02, 0.03) for w in (300, 900) for h in (1800, 3600, 10800) for x in (0.5, 1.0)]
    res = []
    for m, w, h, x in grid:
        fit = run(ser, t0, mid_t, m, w, h, x)
        s = sum(t["net"] * t["quantity"] for t in fit)
        res.append((s, (m, w, h, x), fit))
    res.sort(key=lambda r: -r[0])
    print("top fit configs (move, window s, hold s, exit frac):")
    for s, p, fit in res[:5]:
        print(f"  {p}: fit {summary(fit, d_half)}")
    best = res[0][1]
    test = run(ser, mid_t, t1 + 1, *best)
    print(f"TEST with fit-best {best}: {summary(test, d_half)}")
    for p in ((0.015, 300, 3600, 0.5), (0.02, 300, 3600, 0.5)):
        print(f"  fixed {p} (dmm_lag_follow defaults) fit: {summary(run(ser, t0, mid_t, *p), d_half)}")
        print(f"  fixed {p} test: {summary(run(ser, mid_t, t1 + 1, *p), d_half)}")
    # count fit configs whose test result is positive (robustness)
    pos = sum(1 for s, p, _ in res if sum(t["net"] * t["quantity"] for t in run(ser, mid_t, t1 + 1, *p)) > 0)
    print(f"configs positive out of sample: {pos} of {len(res)}")


def replay(paths):
    """Second-resolution: how fast does SIG respond after a Polymarket move, and what would entry at +1 s earn?"""
    from sigbot.proposals.hft_study import touches, series, at
    tb, tr, pm, pmt, legs = touches(paths)
    ser = series(tb, pm, legs)
    t_end = max(r[-1][0] for r in tb.values())
    out = []
    for ex, (s, p) in ser.items():
        f = PMFollower(move=0.01, window_sec=300, qty=1000, cooldown_sec=600)
        for (t, m, bb, ba) in p:
            k = bisect.bisect_right([r[0] for r in s], t + 1.0) - 1
            if k < 0:
                continue
            _, smid, sb, sa = s[k]
            o = f.on_outside(t, ex, m, sb, sa)
            if not o:
                continue
            row = {"ex": ex, "t": t, "dir": o["dir"], "px": o["price"]}
            for h in (10, 60, 300, 1800):
                if t + h <= t_end:
                    v = at(s, t + h, 1)
                    row[h] = (v - smid) * o["dir"] / TICK
            out.append(row)
    print(f"replay: {len(out)} Polymarket-move signals on {len({r['ex'] for r in out})} legs")
    for h in (10, 60, 300, 1800):
        xs = [r[h] for r in out if h in r]
        if xs:
            print(f"  SIG mid move after {h:5d} s in the signal direction: mean {statistics.mean(xs):+.2f} ticks, n {len(xs)}, "
                  f">0: {sum(x > 0 for x in xs)}, <0: {sum(x < 0 for x in xs)}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--replay", nargs="*")
    a = ap.parse_args()
    if a.backtest:
        backtest()
    if a.replay:
        replay(a.replay)
