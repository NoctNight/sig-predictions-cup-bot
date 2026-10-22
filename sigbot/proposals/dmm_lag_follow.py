"""Follow Polymarket/Kalshi moves on SIG before the designated market maker (DMM) requotes.

Evidence (research_notes/bot_exploits.md, 2026-10-22): the DMM moves its touch 1-2 ticks in bursts across many
markets, 70% of its moves have no SIG print in the previous 10 s (it requotes from an outside signal), and the SIG mid
follows a 1-minute Polymarket/Kalshi move of >= 1.5 points by 0% after 1-2 min, 9% at 3, 29% at 10, 44% at 30 and
53% at 60 min (43 events, 218 legs, 21-22 Oct). Pooled lead-lag: corr(dSIG_t, dPM_(t-3min)) = 0.41 conditional on a
>= 1-point Polymarket move, ~0 at other lags. No participant trades on SIG within 3 min of an outside move. So taking
the DMM's touch in the direction of an outside move and exiting once SIG has converged is an uncontested trade whose
edge is the DMM's own lag, not a view on the race.

Rules (paper by default; `cycle()` returns intended orders, nothing is sent):
  * signal: Polymarket (or Kalshi when Polymarket is missing) mid moved >= move_pts within window_sec and the SIG mid
    moved less than follow_frac of it; both venues present and disagreeing by more than max_disagree -> skip;
  * entry: buy YES at the SIG ask (up move) or NO at 1 - bid (down move), size = min(qty, DMM touch size);
  * exit: when the SIG mid has moved >= exit_frac of the outside move, or after hold_sec, at the touch; the main
    loop's zero-EV exit (run.py) handles the remainder if the sleeve is wired with purpose 'direction'.

Backtest (`python3 -m sigbot.proposals.dmm_lag_follow --backtest`): replays `venue_fair` (one row per poll per leg
with pm, kalshi, sig_bid/ask/mid). Entry at the first poll after the move at the logged SIG touch, exit at the first
poll where the convergence condition holds or at hold_sec, crossing the logged spread. Reports events per day,
hit rate, mean points per share, and SUSQies per day at the default size. Caveat: polls are 60-120 s apart, so the
backtest cannot see the first minute; the realtime feed plus a 3-5 s Polymarket poll would enter earlier than the
backtest assumes, never later.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import statistics
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TICK = 0.005


class LagFollower:
    def __init__(self, move_pts: float = 0.015, window_sec: float = 300, follow_frac: float = 0.34, exit_frac: float = 0.5,
                 hold_sec: float = 3600, qty: int = 1000, max_disagree: float = 0.04, cooldown_sec: float = 1800):
        self.move_pts, self.window_sec, self.follow_frac, self.exit_frac = move_pts, window_sec, follow_frac, exit_frac
        self.hold_sec, self.qty, self.max_disagree, self.cooldown_sec = hold_sec, qty, max_disagree, cooldown_sec
        self.hist: dict[str, list] = collections.defaultdict(list)   # ex -> [(ts, outside, sig_mid)]
        self.open: dict[str, dict] = {}
        self.cooldown: dict[str, float] = {}

    @staticmethod
    def outside(pm, kalshi):
        if pm is not None and kalshi is not None:
            return 0.5 * (pm + kalshi) if abs(pm - kalshi) <= 0.04 else None
        return pm if pm is not None else kalshi

    def cycle(self, ts: float, legs: list[dict], dmm_touch_size: dict | None = None) -> list[dict]:
        """legs: [{exchange_id, pm, kalshi, sig_bid, sig_ask, sig_mid}] (the direction sleeve's rows). Paper orders."""
        out = []
        for r in legs:
            ex = r["exchange_id"]
            o = self.outside(r.get("pm"), r.get("kalshi"))
            if o is None or r.get("sig_mid") is None:
                continue
            h = self.hist[ex]
            h.append((ts, o, r["sig_mid"]))
            del h[:-200]
            if ex in self.open:
                pos = self.open[ex]
                moved = (r["sig_mid"] - pos["sig0"]) / pos["move"]
                if moved >= self.exit_frac or ts - pos["t0"] >= self.hold_sec:
                    px = r["sig_bid"] if pos["dir"] > 0 else r["sig_ask"]
                    out.append(dict(exchangeId=ex, side="no" if pos["dir"] > 0 else "yes", action="buy", quantity=pos["qty"],
                                    price=round(1 - px, 3) if pos["dir"] > 0 else px, reason="exit", pnl_pts=(px - pos["entry"]) * pos["dir"]))
                    self.open.pop(ex); self.cooldown[ex] = ts + self.cooldown_sec
                continue
            if self.cooldown.get(ex, 0) > ts:
                continue
            ref = [x for x in h if ts - x[0] <= self.window_sec]
            if len(ref) < 2:
                continue
            d = o - ref[0][1]
            if abs(d) < self.move_pts:
                continue
            if abs(r["sig_mid"] - ref[0][2]) >= self.follow_frac * abs(d):
                continue
            direction = 1 if d > 0 else -1
            px = r["sig_ask"] if direction > 0 else r["sig_bid"]
            if px is None:
                continue
            qty = min(self.qty, int((dmm_touch_size or {}).get(ex, self.qty)))
            self.open[ex] = dict(t0=ts, dir=direction, entry=px, sig0=r["sig_mid"], move=d, qty=qty)
            out.append(dict(exchangeId=ex, side="yes" if direction > 0 else "no", action="buy", quantity=qty,
                            price=px if direction > 0 else round(1 - px, 3), reason=f"outside moved {d:+.3f}, sig {ref[0][2]:.3f}->{r['sig_mid']:.3f}"))
        return out


def backtest(db: str, qty: int, move_pts: float, exit_frac: float, hold_sec: float, follow_frac: float) -> dict:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute("SELECT ts, exchange_id, pm, kalshi, sig_bid, sig_ask, sig_mid FROM venue_fair WHERE exchange_id IS NOT NULL ORDER BY ts").fetchall()
    polls = collections.defaultdict(list)
    for r in rows:
        polls[r[0]].append(dict(exchange_id=r[1], pm=r[2], kalshi=r[3], sig_bid=r[4], sig_ask=r[5], sig_mid=r[6]))
    lf = LagFollower(move_pts=move_pts, exit_frac=exit_frac, hold_sec=hold_sec, qty=qty, follow_frac=follow_frac)
    trades, exits = [], []
    for ts in sorted(polls):
        for o in lf.cycle(ts, polls[ts]):
            (exits if o["reason"] == "exit" else trades).append(dict(o, ts=ts))
    # force-close whatever is still open at the last poll's touch
    last = sorted(polls)[-1]
    lastrows = {r["exchange_id"]: r for r in polls[last]}
    for ex, pos in list(lf.open.items()):
        r = lastrows.get(ex)
        if r and r["sig_bid"] is not None and r["sig_ask"] is not None:
            px = r["sig_bid"] if pos["dir"] > 0 else r["sig_ask"]
            exits.append(dict(exchangeId=ex, quantity=pos["qty"], pnl_pts=(px - pos["entry"]) * pos["dir"], reason="exit(forced)", ts=last))
    days = (sorted(polls)[-1] - sorted(polls)[0]) / 86400
    pnl = [e["pnl_pts"] for e in exits]
    out = {"polls": len(polls), "days": round(days, 2), "entries": len(trades), "exits": len(exits), "entries_per_day": round(len(trades) / max(days, 1e-9), 1)}
    if pnl:
        out["hit_rate"] = round(sum(1 for p in pnl if p > 0) / len(pnl), 2)
        out["mean_pts_per_share"] = round(statistics.mean(pnl), 4)
        out["median_pts_per_share"] = round(statistics.median(pnl), 4)
        out["susqies_per_day_at_qty"] = round(sum(p * e["quantity"] for p, e in zip(pnl, exits)) / max(days, 1e-9), 1)
        out["worst_pts"] = round(min(pnl), 4)
        out["exit_reasons"] = dict(collections.Counter(e["reason"] for e in exits))
    out["sample_entries"] = [dict(ex=t["exchangeId"], side=t["side"], px=t["price"], q=t["quantity"], why=t["reason"]) for t in trades[:5]]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="follow outside moves ahead of the DMM's requote lag (paper) and backtest")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--db", default=os.path.join(HERE, "data", "sigbot.sqlite"))
    ap.add_argument("--qty", type=int, default=1000)
    ap.add_argument("--move-pts", type=float, default=0.015)
    ap.add_argument("--exit-frac", type=float, default=0.5)
    ap.add_argument("--hold-sec", type=float, default=3600)
    ap.add_argument("--follow-frac", type=float, default=0.34)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.backtest:
        res = backtest(a.db, a.qty, a.move_pts, a.exit_frac, a.hold_sec, a.follow_frac)
        print(json.dumps(res, indent=1) if a.json else "\n".join(f"{k}: {v}" for k, v in res.items()))
        return
    print("paper module: wire LagFollower.cycle(ts, direction.rows) after the direction sleeve; see --backtest")


if __name__ == "__main__":
    main(sys.argv[1:])
