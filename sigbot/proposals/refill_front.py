"""Refill front-running (paper mode): after a print sweeps the touch level P, post at P before the DMM refills it,
so we stand first in the FIFO queue at the DMM's own price instead of behind its 2,000-2,500 shares.

    python3 -m sigbot.proposals.refill_front --backtest data/feed_capture_20261022.jsonl [sigbot/proposals/hft_data/rec_*.jsonl]
        [--latency 1.05 --qty 300 --max-wait 120 --max-spread 1 --min-sweep 100]

Replay rules (engine time = book asOf / trade executedAt):
  * trigger: between two pushes of one exchange a print traded at or through the best bid P (best ask P) and the next push
    shows the best bid below P (best ask above P): the level is empty. Prior spread <= --max-spread ticks.
  * our order (bid at P after a sell sweep, ask at P after a buy sweep) reaches the engine at trigger push asOf + latency
    (feed 0.7 s + REST 0.3 s + 0.05 s): queue ahead = quantity resting at P in the last push before that instant.
  * fills: prints after arrival at P consume the queue FIFO (ahead first, then us); a print through P fills us fully.
  * the order lives until filled or --max-wait s, or until the touch moves through it the other way (we are then the best
    price alone: cancelled at the next push, 1 write). 1 write to place (expirationDate = max-wait, so no cancel write).
  * markouts in ticks from our side at +1/+5/+30 min and at the last book of the file (mid), and the exit-at-touch value
    (sell at the bid / buy at the ask 5 min later), per filled share.
Paper only: `RefillFront.on_book(ex, prev_book, book, prints)` returns the order to place (no venue calls).
"""
from __future__ import annotations

import argparse, bisect, collections, statistics, sys

TICK = 0.005


class RefillFront:
    """Live-shaped signal: call on every feed push of an exchange with the prints since the previous push."""

    def __init__(self, qty=300, max_spread=1, min_sweep=100, max_wait=120, blocked=frozenset()):
        self.qty, self.max_spread, self.min_sweep, self.max_wait, self.blocked = qty, max_spread, min_sweep, max_wait, blocked

    def on_book(self, ex, prev, book, prints):
        """prev/book: {'bids': [[p, q]..], 'asks': [[p, q]..]}; prints: [(price, qty)]. Returns order dict or None."""
        if ex in self.blocked or not prev["bids"] or not prev["asks"] or not book["bids"] or not book["asks"]:
            return None
        if round((prev["asks"][0][0] - prev["bids"][0][0]) / TICK) > self.max_spread:
            return None
        pb, pa = prev["bids"][0][0], prev["asks"][0][0]
        sold = sum(q for p, q in prints if p <= pb + 1e-9)
        bought = sum(q for p, q in prints if p >= pa - 1e-9)
        if sold >= self.min_sweep and book["bids"][0][0] < pb - 1e-9 and book["asks"][0][0] > pb + 1e-9:
            return {"exchangeId": ex, "side": "yes", "action": "buy", "price": pb, "quantity": self.qty, "expiry_sec": self.max_wait}
        if bought >= self.min_sweep and book["asks"][0][0] > pa + 1e-9 and book["bids"][0][0] < pa - 1e-9:
            return {"exchangeId": ex, "side": "yes", "action": "sell", "price": pa, "quantity": self.qty, "expiry_sec": self.max_wait}
        return None


def load(paths):
    from sigbot.proposals.hft_study import load as _load
    books, trades = collections.defaultdict(list), collections.defaultdict(list)
    for kind, d in _load(paths):
        if kind == "book":
            books[d["ex"]].append((d["ts"], d["bids"], d["asks"]))
        elif kind == "trade":
            trades[d["ex"]].append((d["ts"], d["price"], d["qty"]))
    for v in trades.values():
        v.sort()
    return books, trades


def mid_at(rows, ts_list, t):
    i = bisect.bisect_right(ts_list, t) - 1
    if i < 0:
        return None
    _, b, a = rows[i]
    return (b[0][0] + a[0][0]) / 2 if b and a else None


def touch_at(rows, ts_list, t):
    i = bisect.bisect_right(ts_list, t) - 1
    if i < 0:
        return None
    _, b, a = rows[i]
    return (b[0][0] if b else None, a[0][0] if a else None)


def simulate(paths, latency=1.05, qty=300, max_wait=120, max_spread=1, min_sweep=100):
    books, trades = load(paths)
    sig = RefillFront(qty, max_spread, min_sweep, max_wait)
    res = []
    t_end = max(r[-1][0] for r in books.values())
    t_beg = min(r[0][0] for r in books.values())
    for ex, rows in books.items():
        ts_list = [r[0] for r in rows]
        tts = [t[0] for t in trades.get(ex, [])]
        busy_until = 0.0
        for i in range(1, len(rows)):
            a, b = rows[i - 1], rows[i]
            j0, j1 = bisect.bisect_left(tts, a[0] - 0.05), bisect.bisect_right(tts, b[0] + 0.05)
            if j1 <= j0 or b[0] < busy_until:
                continue
            o = sig.on_book(ex, {"bids": a[1], "asks": a[2]}, {"bids": b[1], "asks": b[2]},
                            [(p, q) for _, p, q in trades[ex][j0:j1]])
            if not o:
                continue
            P, buy = o["price"], o["action"] == "buy"
            t_arr = b[0] + latency
            k = bisect.bisect_right(ts_list, t_arr) - 1
            lv = rows[k][1] if buy else rows[k][2]
            ahead = sum(q for p, q in lv if abs(p - P) < 1e-9)
            # does the level still trade on our side? if the opposite touch came down to P we would cross: skip
            tb, ta = (rows[k][1][0][0] if rows[k][1] else None), (rows[k][2][0][0] if rows[k][2] else None)
            if (buy and ta is not None and ta <= P + 1e-9) or (not buy and tb is not None and tb >= P - 1e-9):
                res.append({"ex": ex, "t": b[0], "status": "crossed", "ahead": ahead})
                continue
            filled, t_fill, t_stop = 0.0, None, min(t_arr + max_wait, t_end)
            # the DMM requoting through our price fills us too: first push after arrival with the opposite touch at/through P
            t_cross = next((r[0] for r in rows[k + 1:] if r[0] <= t_stop and ((buy and r[2] and r[2][0][0] <= P + 1e-9)
                            or (not buy and r[1] and r[1][0][0] >= P - 1e-9))), None)
            for (tt, p, q) in trades[ex][bisect.bisect_left(tts, t_arr):]:
                if t_cross is not None and tt >= t_cross:
                    break
                if tt > t_stop:
                    break
                at = abs(p - P) < 1e-9
                through = (p < P - 1e-9) if buy else (p > P + 1e-9)
                if not (at or through):
                    continue
                if through:
                    take = qty - filled
                else:
                    use = min(ahead, q)
                    ahead -= use
                    take = min(qty - filled, q - use)
                if take > 0:
                    filled += take
                    t_fill = t_fill or tt
                if filled >= qty - 1e-9:
                    break
            if t_cross is not None and filled < qty - 1e-9 and (t_fill is None or t_fill <= t_cross):
                filled, t_fill = qty, t_fill or t_cross
            busy_until = (t_fill if filled >= qty - 1e-9 else t_stop)
            r = {"ex": ex, "t": b[0], "P": P, "buy": buy, "ahead0": ahead, "filled": filled, "t_fill": t_fill,
                 "status": "filled" if filled > 0 else "unfilled"}
            if filled > 0:
                sgn = 1 if buy else -1
                for h, lab in ((60, "m1"), (300, "m5"), (1800, "m30")):
                    if t_fill + h <= t_end:
                        m = mid_at(rows, ts_list, t_fill + h)
                        r[lab] = sgn * (m - P) / TICK if m is not None else None
                m = mid_at(rows, ts_list, t_end)
                r["mend"] = sgn * (m - P) / TICK
                if t_fill + 300 <= t_end:
                    bb, aa = touch_at(rows, ts_list, t_fill + 300)
                    r["x5"] = ((bb - P) if buy else (P - aa)) / TICK
            res.append(r)
    return res, (t_end - t_beg) / 3600


def summarise(res, hours, qty, label=""):
    f = [r for r in res if r["status"] == "filled"]
    print(f"{label} {hours:.2f} h: triggers {len(res)} ({len(res)/hours:.0f}/h), crossed {sum(r['status']=='crossed' for r in res)}, "
          f"filled {len(f)} ({len(f)/hours:.1f}/h), shares {sum(r['filled'] for r in f):.0f} "
          f"({sum(r['filled'] for r in f)/hours:.0f}/h); queue ahead at arrival = 0 in "
          f"{sum(1 for r in res if r.get('ahead0', 1) == 0)} of {len(res)}")
    out = {}
    for lab in ("m1", "m5", "m30", "mend", "x5"):
        xs = [(r[lab], r["filled"]) for r in f if r.get(lab) is not None]
        if xs:
            w = sum(q for _, q in xs)
            mean = sum(v * q for v, q in xs) / w
            out[lab] = mean
            print(f"   {lab:5s} n {len(xs):4d}  share-weighted {mean:+.2f} ticks  median {statistics.median(v for v, _ in xs):+.2f}"
                  f"  hit {sum(v > 0 for v, _ in xs)}/{sum(v < 0 for v, _ in xs)}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", nargs="+", required=True)
    ap.add_argument("--latency", type=float, default=1.05)
    ap.add_argument("--qty", type=int, default=300)
    ap.add_argument("--max-wait", type=float, default=120)
    ap.add_argument("--max-spread", type=int, default=1)
    ap.add_argument("--min-sweep", type=int, default=100)
    a = ap.parse_args(argv)
    for p in a.backtest:
        res, hours = simulate([p], a.latency, a.qty, a.max_wait, a.max_spread, a.min_sweep)
        summarise(res, hours, a.qty, label=p.rsplit("/", 1)[-1])


if __name__ == "__main__":
    main()
