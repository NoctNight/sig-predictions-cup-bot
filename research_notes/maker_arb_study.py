"""Resting-order (maker-side) high-frequency arb: enter and/or exit the sell-set with resting orders at the touch
instead of crossing the spread. Read-only (copies data/sigbot.sqlite), 0 venue reads.

Fill model (queue at the back of the touch, price-time priority):
  a resting YES offer of q at price p posted at t0 fills from public buy-aggressor prints at p after the queue ahead A0
  (the touch size at t0) is consumed; any buy print above p clears the level and fills it fully. A resting YES bid is
  the mirror image (sell-aggressor prints at or below p). Our own fills are removed from the tape (our own taker prints are removed). Unknown-aggressor
  prints are ignored. A0 = the `quotes` table touch size (fresh <= 3 min, same price), else the leg's median touch size,
  else the global median.
Selling YES at p = buying NO at 1 - p, so a sell-set entered with resting offers costs sum(asks) instead of sum(bids).

Stages: 0 calibration on our own level-1 MM orders; 1 fill rates and markouts at every touch; 2 backtest of the
high-frequency arb rule (deviation >= 4 ticks, +2-tick exit, 24 h) with taker or resting entry/exit and wait W.
Training = signals on 22 Oct UTC, test = 23 Oct.
Usage: python research_notes/maker_arb_study.py [--figures]
"""
from __future__ import annotations

import collections
import os
import pickle
import sys
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "research_notes"))
import arb_convergence_study as acs  # noqa: E402
import skewed_arb_study as sks  # noqa: E402
from sigbot.backtest_arb import open_copy_ro  # noqa: E402

OUT = os.path.join(HERE, "data", "research_maker_arb")
FIG = os.path.join(HERE, "research_notes", "figures")
os.makedirs(OUT, exist_ok=True)
T = 0.005
EPS = 1e-6
UTC = timezone.utc
WAITS = [0, 2, 5, 15, 30, 60]                     # minutes a resting order waits before we cross the spread
DEADLINES = [5, 15, 30, 60, 240]
RNG = np.random.default_rng(11)
D22 = datetime(2026, 10, 22, tzinfo=UTC).timestamp()
D23 = datetime(2026, 10, 23, tzinfo=UTC).timestamp()


def parse_ts(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        digits = "".join(ch for ch in tail if ch.isdigit())
        s = f"{head}.{digits[:6]}{tail[len(digits):]}"
    return datetime.fromisoformat(s).timestamp()


# ------------------------------------------------------------------------------------------------ data
def load():
    con, tmp = open_copy_ro(os.path.join(HERE, "data", "sigbot.sqlite"))
    tape = con.execute("SELECT exchange_id, price, quantity, executed_at, aggressor FROM tape "
                       "WHERE executed_at >= '2026-10-21T23'").fetchall()
    # our own TAKER prints only (arb sets, exits, direction, hedges): resting MM/tilt/inside fills are other traders' flow
    own = con.execute("SELECT exchange_id, quantity, filled_at FROM fills WHERE filled_at >= '2026-10-21T23' "
                      "AND purpose IN ('arb', 'exit', 'direction', 'hedge')").fetchall()
    qs = con.execute("SELECT ts, exchange_id, best_bid, best_ask, bid_qty, ask_qty FROM quotes ORDER BY ts").fetchall()
    mm = con.execute("SELECT o.order_id, o.exchange_id, o.side, o.price, o.quantity, o.placed_at, o.expires_at, "
                     "COALESCE((SELECT SUM(ABS(f.quantity)) FROM fills f WHERE f.order_id = o.order_id), 0) "
                     "FROM orders o WHERE o.purpose = 'mm' AND o.level = 1 ORDER BY o.placed_at").fetchall()
    con.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    ownk = collections.defaultdict(list)
    for ex, qty, at in own:
        ownk[(str(ex), round(abs(qty)))].append(parse_ts(at))
    by = collections.defaultdict(list)
    dropped = 0
    for ex, px, qty, at, ag in tape:
        t = parse_ts(at)
        if any(abs(t - u) <= 3 for u in ownk.get((str(ex), round(qty)), ())):
            dropped += 1
            continue
        by[str(ex)].append((t, px, qty, 1 if ag == "buy" else (-1 if ag == "sell" else 0)))
    TP = {}
    for ex, rows in by.items():
        rows.sort()
        a = np.array(rows)
        TP[ex] = (a[:, 0], a[:, 1], a[:, 2], a[:, 3])
    QS = collections.defaultdict(list)
    for ts, ex, b, a, bq, aq in qs:
        QS[str(ex)].append((ts, b if b is not None else np.nan, a if a is not None else np.nan,
                            bq if bq is not None else np.nan, aq if aq is not None else np.nan))
    QS = {ex: np.array(v) for ex, v in QS.items()}
    return TP, QS, mm, dropped


class Book:
    def __init__(self, TP, QS):
        self.TP, self.QS = TP, QS
        self.med = {ex: (float(np.nanmedian(v[:, 3])), float(np.nanmedian(v[:, 4]))) for ex, v in QS.items()}
        allb = np.concatenate([v[:, 3] for v in QS.values()])
        alla = np.concatenate([v[:, 4] for v in QS.values()])
        self.gmed = (float(np.nanmedian(allb)), float(np.nanmedian(alla)))

    def queue(self, ex, t0, side, p):
        """Shares ahead of a new order joining price p (side +1 = YES offer, -1 = YES bid)."""
        k = 4 if side > 0 else 3
        v = self.QS.get(ex)
        if v is not None:
            i = np.searchsorted(v[:, 0], t0, side="right") - 1
            if i >= 0 and t0 - v[i, 0] <= 180:
                touch = v[i, 2] if side > 0 else v[i, 1]
                if np.isfinite(touch) and np.isfinite(v[i, k]):
                    if abs(touch - p) < EPS:
                        return float(v[i, k])
                    if (side > 0 and p < touch - EPS) or (side < 0 and p > touch + EPS):
                        return 0.0                              # we would be the new best price
            m = self.med[ex][1 if side > 0 else 0]
            if np.isfinite(m):
                return m
        return self.gmed[1 if side > 0 else 0]

    def fill(self, ex, t0, side, p, q, horizon_s, A0=None):
        """Filled fraction at each of `horizon_s` (seconds after t0) and the full-fill time (inf if never)."""
        if A0 is None:
            A0 = self.queue(ex, t0, side, p)
        tp = self.TP.get(ex)
        hs = np.atleast_1d(horizon_s)
        if tp is None:
            return np.zeros(len(hs)), np.inf
        ts, px, qty, ag = tp
        i0 = np.searchsorted(ts, t0, side="right")
        i1 = np.searchsorted(ts, t0 + hs.max(), side="right")
        ts, px, qty, ag = ts[i0:i1], px[i0:i1], qty[i0:i1], ag[i0:i1]
        if side > 0:
            hit = ag > 0
            at, thru = hit & (np.abs(px - p) < EPS), hit & (px > p + EPS)
        else:
            hit = ag < 0
            at, thru = hit & (np.abs(px - p) < EPS), hit & (px < p - EPS)
        f = np.clip(np.cumsum(qty * at) - A0, 0, q)
        f = np.where(np.maximum.accumulate(thru) if len(thru) else thru, q, f)
        out = np.array([f[np.searchsorted(ts, t0 + h, side="right") - 1] / q if np.searchsorted(ts, t0 + h, side="right") > 0 else 0.0
                        for h in hs])
        full = np.where(f >= q - EPS)[0]
        return out, (float(ts[full[0]]) if len(full) else np.inf)


# ------------------------------------------------------------------------------------------------ panel and deviation
def panels():
    A, B = sks.build_panels()
    for r, d in B.r.items():
        oB = d[0]["o"]
        if r in A.r:
            tt = np.concatenate([A.grid, B.grid])
            oo = np.concatenate([A.r[r][0]["o"], oB])
        else:
            tt, oo = B.grid, oB
        med = np.full(len(B.grid), np.nan)
        for k, t in enumerate(B.grid):
            lo = np.searchsorted(tt, t - 86400, side="right")
            hi = np.searchsorted(tt, t, side="right")
            w = oo[lo:hi]
            w = w[np.isfinite(w)]
            if len(w) >= 72:                                    # >= 6 h of samples (proxy days seed the median)
                med[k] = np.median(w)
        d[0]["dev2"] = oB - med
    return B


def mid_at(d, x, i):
    return d[x]["mid"][i]


# ------------------------------------------------------------------------------------------------ stage 0: calibration
def stage0(bk, mm, B):
    """Our own level-1 MM orders: model-predicted vs actual filled shares over each order's life."""
    legs = {d[x]["ex"] for d in B.r.values() for x in (0, 1)}
    by = collections.defaultdict(list)
    for o in mm:
        by[(o[1], o[2])].append(o)
    pred = act = 0.0
    n = nf_a = 0
    rows = []
    for (ex, side), os_ in by.items():
        if ex not in legs:
            continue
        for k, (oid, _, sd, price, qty, placed, exp, filled) in enumerate(os_):
            end = parse_ts(exp) if exp else placed + 120
            if k + 1 < len(os_):
                end = min(end, os_[k + 1][5])
            life = max(end - placed, 1.0)
            if sd == "yes":                                     # buy YES = resting YES bid at price
                s, p = -1, price
            else:                                               # buy NO at price = resting YES offer at 1 - price
                s, p = +1, round(1 - price, 4)
            f, _ = bk.fill(ex, placed, s, p, qty, [life])
            pred += f[0] * qty
            act += min(filled, qty)
            n += 1
            nf_a += filled > 0
            rows.append((f[0] * qty, min(filled, qty)))
    rows = np.array(rows)
    return dict(orders=n, pred_shares=pred, act_shares=act, ratio=act / pred if pred else np.nan,
                p_any_pred=float(np.mean(rows[:, 0] > 0)), p_any_act=float(np.mean(rows[:, 1] > 0)))


# ------------------------------------------------------------------------------------------------ stage 1: fill rates
def stage1(bk, B, q=1000):
    out = []
    for r, d in B.r.items():
        dev = d[0]["dev2"]
        for i in range(0, len(B.grid), 3):
            t0 = B.grid[i]
            for x in (0, 1):
                ex = d[x]["ex"]
                b, a = d[x]["bid"][i], d[x]["ask"][i]
                if not (np.isfinite(b) and np.isfinite(a)):
                    continue
                for side, p in ((+1, a), (-1, b)):
                    f, tf = bk.fill(ex, t0, side, p, q, np.array(DEADLINES) * 60.0)
                    mk = np.nan
                    if np.isfinite(tf):
                        j = min(np.searchsorted(B.grid, tf + 1800), len(B.grid) - 1)
                        m1 = d[x]["mid"][j]
                        if np.isfinite(m1) and B.grid[j] - tf < 2400:
                            mk = side * (p - m1) / T                # realised edge vs the mid 30 min after the fill
                    j0 = min(i + 6, len(B.grid) - 1)
                    m0 = d[x]["mid"][j0]
                    tk = side * ((b if side > 0 else a) - m0) / T if np.isfinite(m0) else np.nan   # taker at t0, same side
                    out.append(dict(race=r, ex=ex, side=side, day=sks.day_of(t0), dev=dev[i] / T if np.isfinite(dev[i]) else np.nan,
                                    f=f, mk=mk, tk=tk, spread=(a - b) / T))
    return out


def summ1(rows, cond):
    s = [r for r in rows if cond(r)]
    if not s:
        return None
    F = np.array([r["f"] for r in s])
    mk = np.array([r["mk"] for r in s])
    tk = np.array([r["tk"] for r in s])
    return dict(n=len(s), p_full=(F >= 1 - EPS).mean(0).tolist(), mean_frac=F.mean(0).tolist(),
                mk=float(np.nanmean(mk)), mk_n=int(np.isfinite(mk).sum()), tk=float(np.nanmean(tk)),
                mk_t=sks.cl_t(mk[np.isfinite(mk)], [r["race"] for r, m in zip(s, mk) if np.isfinite(m)])["t"])


# ------------------------------------------------------------------------------------------------ stage 2: backtest
def legs_entry(bk, d, B, i, W, q, slip):
    """Sell-set entry at grid i. Returns (YES prices sold per leg, taker share per leg, time the set is complete) or None."""
    t0 = B.grid[i]
    b = [d[x]["bid"][i] for x in (0, 1)]
    a = [d[x]["ask"][i] for x in (0, 1)]
    if W == 0:
        return [b[0] - slip * T, b[1] - slip * T], [1.0, 1.0], t0, 0.0
    fr, tf = [], []
    for x in (0, 1):
        f, t_ = bk.fill(d[x]["ex"], t0, +1, a[x], q, [W * 60.0])
        fr.append(f[0])
        tf.append(t_)
    top = max(fr)
    if top <= 0:
        return None
    j = min(np.searchsorted(B.grid, t0 + W * 60.0), len(B.grid) - 1)
    e, tk = [], []
    for x in (0, 1):
        if fr[x] >= top - EPS:
            e.append(a[x])
            tk.append(0.0)
        else:                                                   # complete the lagging leg at its bid when the wait ends
            bj = d[x]["bid"][j]
            if not np.isfinite(bj):
                return None
            w = fr[x] / top
            e.append(w * a[x] + (1 - w) * (bj - slip * T))
            tk.append(1 - w)
    done = max(tf) if min(fr) >= 1 - EPS else t0 + W * 60.0
    unhedged = abs(fr[0] - fr[1]) * q * W / 2                   # share-minutes on one leg (rough: half the wait)
    return e, tk, done, unhedged


def legs_exit(bk, d, B, j, W, q, slip):
    a = [d[x]["ask"][j] for x in (0, 1)]
    b = [d[x]["bid"][j] for x in (0, 1)]
    if W == 0:
        return [a[0] + slip * T, a[1] + slip * T], [1.0, 1.0]
    t0 = B.grid[j]
    k = min(np.searchsorted(B.grid, t0 + W * 60.0), len(B.grid) - 1)
    out, tk = [], []
    for x in (0, 1):
        f, _ = bk.fill(d[x]["ex"], t0, -1, b[x], q, [W * 60.0])
        ak = d[x]["ask"][k] if np.isfinite(d[x]["ask"][k]) else a[x]
        out.append(f[0] * b[x] + (1 - f[0]) * (ak + slip * T))
        tk.append(1 - f[0])
    return out, tk


def backtest(bk, B, Win, Wout, q=2000, slip=0.1, thr=4, k=2, cooldown=6):
    res = []
    for r, d in B.r.items():
        sb, dev = d[0]["sb"], d[0]["dev2"]
        i, n, nxt = 0, len(B.grid), 0
        while i < n:
            ok = (i >= nxt and np.isfinite(sb[i]) and np.isfinite(dev[i]) and sb[i] - 1 >= 0.005 - 1e-9
                  and (sb[i] - 1) / (2 - sb[i]) >= 0.005 - 1e-9 and dev[i] >= thr * T - 1e-9
                  and all(np.isfinite(d[x]["ask"][i]) for x in (0, 1)))
            if not ok:
                i += 1
                continue
            en = legs_entry(bk, d, B, i, Win, q, slip)
            if en is None:
                res.append(dict(race=r, day=sks.day_of(B.grid[i]), t0=B.grid[i], traded=False))
                nxt = i + 1                                     # a missed resting entry may retry at the next step
                i += 1
                continue
            e, tke, done, unh = en
            es = sum(e)
            js = max(np.searchsorted(B.grid, done), i + 1)
            jx, how = None, "eop"
            for jj in range(js, n):
                aa = [d[x]["ask"][jj] for x in (0, 1)]
                bb = [d[x]["bid"][jj] for x in (0, 1)]
                if not all(np.isfinite(aa + bb)):
                    continue
                lvl = sum(aa) if Wout == 0 else sum(bb)        # the price level we would exit at
                if es - lvl >= k * T - 1e-9 or sum(aa) <= 1 + 1e-9:
                    jx, how = jj, "k"
                    break
                if B.grid[jj] - B.grid[i] >= 86400:
                    jx, how = jj, "24h"
                    break
                jx = jj
            if jx is None:
                break
            if how == "k":
                x_, tkx = legs_exit(bk, d, B, jx, Wout, q, slip)
            else:
                x_, tkx = legs_exit(bk, d, B, jx, 0, q, slip)
            rt = (es - sum(x_)) / T
            res.append(dict(race=r, day=sks.day_of(B.grid[i]), t0=B.grid[i], traded=True, rt=rt, how=how,
                            taker_legs=sum(tke) + sum(tkx), unhedged=unh, hold_h=(B.grid[jx] - done) / 3600,
                            maker_entry_full=(Win > 0 and sum(tke) == 0)))
            nxt = jx + cooldown
            i = jx + 1
    return res


def eval_bt(res, taker_entry, rej=0.2, nboot=500):
    """Per signal and per traded set; 20% basket rejections drop taker entries (resting legs are single orders)."""
    tr = [x for x in res if x["traded"]]
    if not tr:
        return None
    rt = np.array([x["rt"] for x in tr])
    races = np.array([x["race"] for x in tr])
    keep_p = (1 - rej) if taker_entry else 1.0
    ur = sorted(set(races))
    idx = {u: np.where(races == u)[0] for u in ur}
    bs = []
    for _ in range(nboot):
        pick = np.concatenate([idx[u] for u in RNG.choice(ur, len(ur))])
        bs.append(rt[pick].mean())
    return dict(signals=len(res), traded=len(tr), fill_rate=len(tr) / len(res), rt=float(rt.mean()),
                ci=(float(np.percentile(bs, 5)), float(np.percentile(bs, 95))), t=sks.cl_t(rt, races)["t"],
                per_signal=float(rt.sum() * keep_p / len(res)), sd=float(rt.std()),
                taker_legs=float(np.mean([x["taker_legs"] for x in tr])), unhedged=float(np.mean([x["unhedged"] for x in tr])),
                hows=dict(collections.Counter(x["how"] for x in tr)), hold_h=float(np.median([x["hold_h"] for x in tr])),
                both_rest=float(np.mean([x["maker_entry_full"] for x in tr])))


def stage2(bk, B):
    out = {}
    combos = [("taker in, taker out", 0, 0)]
    for W in WAITS[1:]:
        combos += [("taker in, resting out", 0, W), ("resting in, taker out", W, 0), ("resting in, resting out", W, W)]
    for name, wi, wo in combos:
        for q in (500, 2000):
            for slip in (0.1, 0.25):
                res = backtest(bk, B, wi, wo, q=q, slip=slip)
                for lab, cond in (("train", lambda x: x["t0"] < D23), ("test", lambda x: x["t0"] >= D23), ("all", lambda x: True)):
                    out[(name, wi, wo, q, slip, lab)] = eval_bt([x for x in res if cond(x)], taker_entry=(wi == 0))
    return out


# ------------------------------------------------------------------------------------------------ report
def md(c0, s1, s2, dropped):
    L = [f"Own prints removed from the tape: {dropped}.\n",
         "## Stage 0: queue model vs our own level-1 MM orders (2-leg race legs)\n",
         f"Orders {c0['orders']}; predicted filled shares {c0['pred_shares']:,.0f}, actual {c0['act_shares']:,.0f} "
         f"(actual / predicted = {c0['ratio']:.2f}); share of orders with any fill: predicted {c0['p_any_pred']:.1%}, actual {c0['p_any_act']:.1%}.\n",
         "## Stage 1: a 1,000-share order joining the touch (every 2-leg race leg, every 15 min)\n",
         "| order | when | n | P(full fill) 5 / 15 / 30 / 60 / 240 min | mean filled share at 60 min | realised edge vs mid 30 min after fill (t) | taker same side vs mid 30 min later |",
         "|---|---|---|---|---|---|---|"]
    for key, v in s1.items():
        if v:
            L.append(f"| {key[0]} | {key[1]} | {v['n']} | " + " / ".join(f"{p:.0%}" for p in v["p_full"]) +
                     f" | {v['mean_frac'][3]:.0%} | {v['mk']:+.2f} (n {v['mk_n']}, t {v['mk_t']:+.1f}) | {v['tk']:+.2f} |")
    L += ["\n## Stage 2: high-frequency arb rule, ticks per set (deviation >= 4, +2-tick exit, 24 h)\n",
          "| variant | wait in / out (min) | size | slip | panel | signals | traded | mean per set [90% CI] (t) | per signal | taker legs per set | one-leg share-min | exits |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for (name, wi, wo, q, slip, lab), v in s2.items():
        if v and slip == 0.1 and lab != "all":
            L.append(f"| {name} | {wi} / {wo} | {q} | {slip} | {lab} | {v['signals']} | {v['traded']} | {v['rt']:+.2f} "
                     f"[{v['ci'][0]:+.2f}, {v['ci'][1]:+.2f}] ({v['t']:+.1f}) | {v['per_signal']:+.2f} | {v['taker_legs']:.2f} | "
                     f"{v['unhedged']:,.0f} | {v['hows']} |")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ stage 3: leg-in / leg-out
LEG_WAITS = [5, 15, 30, 60, 240]


def taker_exit_scan(d, B, j0, es, i_sig, k=2):
    """First grid point from j0 where the taker exit rule fires; returns (j, how)."""
    jx, how = None, "eop"
    for jj in range(j0, len(B.grid)):
        aa = [d[x]["ask"][jj] for x in (0, 1)]
        if not all(np.isfinite(aa)):
            continue
        if es - sum(aa) >= k * T - 1e-9 or sum(aa) <= 1 + 1e-9:
            return jj, "k"
        if B.grid[jj] - B.grid[i_sig] >= 86400:
            return jj, "24h"
        jx = jj
    return jx, how


def leg_in(bk, d, B, i0, W, q, slip):
    """Rest a YES offer at each leg's ask (re-posted every 5 min while the completed set would still have a sell edge
    >= 1 tick); whenever one leg fills, sell the other leg's YES at its bid at the next grid point."""
    r, tr = 1.0, []
    j = i0
    while j < len(B.grid) - 1 and B.grid[j] < B.grid[i0] + W * 60 and r > 1e-6:
        a = [d[x]["ask"][j] for x in (0, 1)]
        b1 = [d[x]["bid"][j + 1] for x in (0, 1)]
        if all(np.isfinite(a + b1)):
            ff = []
            for L in (0, 1):
                if a[L] + d[1 - L]["bid"][j] - 1 >= T - 1e-9:
                    f, _ = bk.fill(d[L]["ex"], B.grid[j], +1, a[L], r * q, [300.0])
                    ff.append(f[0] * r)
                else:
                    ff.append(0.0)
            tot = sum(ff)
            if tot > r:
                ff = [x * r / tot for x in ff]
            for L in (0, 1):
                if ff[L] > 0:
                    tr.append((ff[L], a[L] + b1[1 - L] - slip * T))
                    r -= ff[L]
        j += 1
    F = sum(x[0] for x in tr)
    if F <= 0:
        return None
    es = sum(x[0] * x[1] for x in tr) / F
    return F, es, j


def leg_out(bk, d, B, j0, es, i_sig, q, slip, k=2):
    """Exit: the taker rule as usual, plus a resting YES bid on a leg whenever buying it at the bid and the other leg at
    its ask already locks k ticks; on a fill the other leg is bought at its ask at the next grid point."""
    r, px = 1.0, 0.0
    for jj in range(j0, len(B.grid) - 1):
        aa = [d[x]["ask"][jj] for x in (0, 1)]
        bb = [d[x]["bid"][jj] for x in (0, 1)]
        a1 = [d[x]["ask"][jj + 1] for x in (0, 1)]
        if not all(np.isfinite(aa + bb + a1)):
            continue
        if es - sum(aa) >= k * T - 1e-9 or sum(aa) <= 1 + 1e-9 or B.grid[jj] - B.grid[i_sig] >= 86400:
            px += r * (sum(aa) + 2 * slip * T)
            return px, 0.0, "k" if B.grid[jj] - B.grid[i_sig] < 86400 else "24h"
        ff = []
        for L in (0, 1):
            if es - (bb[L] + aa[1 - L]) >= k * T - 1e-9:
                f, _ = bk.fill(d[L]["ex"], B.grid[jj], -1, bb[L], r * q, [300.0])
                ff.append(f[0] * r)
            else:
                ff.append(0.0)
        tot = sum(ff)
        if tot > r:
            ff = [x * r / tot for x in ff]
        for L in (0, 1):
            if ff[L] > 0:
                px += ff[L] * (bb[L] + a1[1 - L] + slip * T)
                r -= ff[L]
        if r <= 1e-6:
            return px, 0.0, "rest"
    jl = len(B.grid) - 1
    aa = [d[x]["ask"][jl] for x in (0, 1)]
    return px + r * (sum(aa) + 2 * slip * T), r, "eop"


def stage3(bk, B, q=2000, slip=0.1):
    base = backtest(bk, B, 0, 0, q=q, slip=slip)
    sig = [(x["race"], int(np.searchsorted(B.grid, x["t0"])), x) for x in base if x["traded"]]
    out = collections.defaultdict(list)
    for r, i0, bx in sig:
        d = B.r[r]
        es_t = d[0]["sb"][i0] - 2 * slip * T
        j1, how = taker_exit_scan(d, B, i0 + 1, es_t, i0)
        lo_px, _, how_lo = leg_out(bk, d, B, i0 + 1, es_t, i0, q, slip)
        row = dict(race=r, day=sks.day_of(B.grid[i0]), test=B.grid[i0] >= D23, taker=bx["rt"],
                   taker_legout=(es_t - lo_px) / T, how_lo=how_lo)
        for W in LEG_WAITS:
            li = leg_in(bk, d, B, i0, W, q, slip)
            if li is None:
                row[W] = (0.0, np.nan, np.nan)
                continue
            F, es, jend = li
            jx, _ = taker_exit_scan(d, B, jend, es, i0)
            if jx is None:
                row[W] = (0.0, np.nan, np.nan)
                continue
            sa = sum(d[x]["ask"][jx] for x in (0, 1)) + 2 * slip * T
            lo_px2, _, _ = leg_out(bk, d, B, jend, es, i0, q, slip)
            row[W] = (F, (es - sa) / T, (es - lo_px2) / T)
        out["rows"].append(row)
    # front-of-queue capacity: buy-aggressor shares printed at the ask on both legs while the gap is >= +4
    cap = collections.defaultdict(float)
    for r, d in B.r.items():
        dev = d[0]["dev2"]
        for i in range(len(B.grid) - 1):
            if not (np.isfinite(dev[i]) and dev[i] >= 4 * T):
                continue
            for x in (0, 1):
                tp = bk.TP.get(d[x]["ex"])
                a = d[x]["ask"][i]
                if tp is None or not np.isfinite(a):
                    continue
                ts, px, qty, ag = tp
                s0, s1 = np.searchsorted(ts, B.grid[i]), np.searchsorted(ts, B.grid[i + 1])
                m = (ag[s0:s1] > 0) & (px[s0:s1] >= a - EPS)
                cap[sks.day_of(B.grid[i])] += float(qty[s0:s1][m].sum())
    out["capacity"] = dict(cap)
    return out


def summ3(s3, q=2000):
    R = s3["rows"]
    res = {}
    for lab, cond in (("train", lambda x: not x["test"]), ("test", lambda x: x["test"]), ("all", lambda x: True)):
        rows = [x for x in R if cond(x)]
        if not rows:
            continue
        races = [x["race"] for x in rows]
        tk = np.array([x["taker"] for x in rows])
        tlo = np.array([x["taker_legout"] for x in rows])
        v = dict(n=len(rows), taker=float(tk.mean()), taker_t=sks.cl_t(tk, races)["t"],
                 taker_legout=float(tlo.mean()), legout_gain=float((tlo - tk).mean()), legout_gain_t=sks.cl_t(tlo - tk, races)["t"])
        for W in LEG_WAITS:
            F = np.array([x[W][0] for x in rows])
            rt = np.array([x[W][1] for x in rows])
            rlo = np.array([x[W][2] for x in rows])
            m = F > 0
            # money per signal in ticks x sets: replacing the taker entry, or adding the rested sets on top of it
            money_li = np.where(m, F * q * np.nan_to_num(rt), 0)
            v[W] = dict(fill=float(m.mean()), F=float(F.mean()), rt=float(np.nanmean(rt[m])) if m.any() else np.nan,
                        rt_lo=float(np.nanmean(rlo[m])) if m.any() else np.nan,
                        gain_vs_taker=float(np.nanmean(rt[m] - tk[m])) if m.any() else np.nan,
                        gain_t=sks.cl_t((rt[m] - tk[m]), np.array(races)[m])["t"] if m.sum() >= 3 else np.nan,
                        ticksets_li=float(money_li.mean()), ticksets_taker=float((0.8 * q * tk).mean()))
        res[lab] = v
    return res


def md3(s3s):
    L = ["\n## Stage 3: leg-in entry / leg-out exit vs the taker rule, same signals (2,000 sets, slippage 0.1 tick)\n",
         "| panel | signals | taker per set (t) | taker in + leg-out exit per set | leg-out gain (t) | wait W | share of signals with any rested fill | mean rested size (share of 2,000) | leg-in per set | leg-in + leg-out per set | gain vs taker on the same signals (t) | ticks x sets per signal: leg-in / taker |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for lab, v in s3s.items():
        for W in LEG_WAITS:
            w = v[W]
            L.append(f"| {lab} | {v['n']} | {v['taker']:+.2f} ({v['taker_t']:+.1f}) | {v['taker_legout']:+.2f} | {v['legout_gain']:+.2f} ({v['legout_gain_t']:+.1f}) | "
                     f"{W} min | {w['fill']:.0%} | {w['F']:.0%} | {w['rt']:+.2f} | {w['rt_lo']:+.2f} | {w['gain_vs_taker']:+.2f} ({w['gain_t']:+.1f}) | "
                     f"{w['ticksets_li']:,.0f} / {w['ticksets_taker']:,.0f} |")
    return "\n".join(L) + "\n"


def figure(d):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    R, s1 = d["s3"]["rows"], d["s1"]
    xs = np.arange(len(LEG_WAITS) + 1)
    xl = ["0\n(cross now)"] + [str(w) for w in LEG_WAITS]
    per_day = {}
    for lab, cond in (("train", lambda x: not x["test"]), ("test", lambda x: x["test"])):
        per_day[lab] = len([x for x in R if cond(x)]) / (1.0 if lab == "train" else 23 / 24)
    fig = plt.figure(figsize=(10, 16.5), constrained_layout=True)
    gs = fig.add_gridspec(6, 1, height_ratios=[0.5, 3.2, 3.2, 2.8, 0.1, 2.0])
    acs._header(fig.add_subplot(gs[0]), "Resting orders instead of crossing the spread",
                "High-frequency arb: wait W minutes for a resting order to fill on one leg, then sell the other leg at once")
    styles = (("train", ":", "^", "training (dotted)"), ("test", "-", "o", "test (solid)"))
    ax = fig.add_subplot(gs[1])
    for lab, ls, mk, nm in styles:
        rows = [x for x in R if (x["test"] if lab == "test" else not x["test"])]
        anyf = [np.nan] + [np.mean([x[W][0] > 0 for x in rows]) * 100 for W in LEG_WAITS]
        size = [np.nan] + [np.mean([x[W][0] for x in rows]) * 100 for W in LEG_WAITS]
        ax.plot(xs, anyf, ls=ls, marker=mk, color="C0", lw=4.5, ms=9, alpha=0.6, label=f"signals with any resting fill, {nm}")
        ax.plot(xs, size, ls=ls, marker=mk, color="C2", lw=1.8, label=f"average size filled (% of 2,000 sets), {nm}")
    ref = s1[("YES offer at the ask (= NO entry)", "gap >= +4")]
    ax.plot(xs[1:], np.array(ref["p_full"]) * 100, color="0.5", lw=1.5, ls="--", marker="s",
            label="one 1,000-share order fully filled, every wide-gap moment (both days)")
    ax.set_ylabel("Percent", fontsize=10)
    ax.set_ylim(0, 100)
    ax.set_title("A. How often a resting order fills", loc="left", fontweight="bold", fontsize=11)
    ax.set_xticks(xs, xl)
    ax.legend(fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=1, frameon=False)
    ax.grid(alpha=0.3)
    ax = fig.add_subplot(gs[2])
    for lab, ls, mk, nm in styles:
        rows = [x for x in R if (x["test"] if lab == "test" else not x["test"])]
        tk = np.mean([x["taker"] for x in rows])
        ys, lo, hi, ns = [tk], [np.nan], [np.nan], [len(rows)]
        for W in LEG_WAITS:
            sub = [x for x in rows if x[W][0] > 0]
            v = np.array([x[W][1] for x in sub])
            ys.append(v.mean() if len(v) else np.nan)
            ns.append(len(v))
            if len(v) >= 3:
                bs = [RNG.choice(v, len(v)).mean() for _ in range(1000)]
                lo.append(np.percentile(bs, 5))
                hi.append(np.percentile(bs, 95))
            else:
                lo.append(np.nan)
                hi.append(np.nan)
        ax.plot(xs, ys, ls=ls, marker=mk, color="C3", lw=2, label=f"profit per set, {nm}")
        if lab == "train":
            ax.fill_between(xs, lo, hi, color="C3", alpha=0.12, label="90% range, training (3+ filled signals)")
        else:
            for k_ in range(len(xs)):
                if np.isfinite(lo[k_]):
                    ax.errorbar(xs[k_] + 0.06, ys[k_], yerr=[[ys[k_] - lo[k_]], [hi[k_] - ys[k_]]], color="C3", capsize=4, lw=1.2)
            ax.plot([], [], color="C3", marker="|", ls="", ms=10, label="90% range, test (bar; 3+ filled signals)")
        for k_, n_ in enumerate(ns):
            if np.isfinite(ys[k_]):
                ax.annotate(f"n {n_}", (xs[k_], ys[k_]), textcoords="offset points", xytext=(8, 8 if lab == "test" else -16),
                            fontsize=9, color="0.3")
    ax.axhline(0, color="0.4", lw=1)
    ax.set_ylabel("Net profit per set (ticks)", fontsize=10)
    ax.set_title("B. Profit per set on the signals that filled (W = 0 is today's rule: cross the spread)", loc="left",
                 fontweight="bold", fontsize=11)
    ax.set_xticks(xs, xl)
    ax.legend(fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False)
    ax.grid(alpha=0.3)
    ax = fig.add_subplot(gs[3])
    for lab, ls, mk, nm in styles:
        rows = [x for x in R if (x["test"] if lab == "test" else not x["test"])]
        add = [0.0] + [np.mean([x[W][0] * 2000 * (x[W][1] if np.isfinite(x[W][1]) else 0) for x in rows]) * T * per_day[lab]
                       for W in LEG_WAITS]
        base = np.mean([0.8 * 2000 * x["taker"] for x in rows]) * T * per_day[lab]
        ax.plot(xs, add, ls=ls, marker=mk, color="C4", lw=2, label=f"extra from resting orders on top of today's rule, {nm}")
        ax.axhline(base, ls=ls, color="0.4", lw=1.5, label=f"today's rule alone at 2,000 sets per signal, {nm}")
    ax.axhline(0, color="0.6", lw=0.8)
    ax.set_ylabel("Susqies per day", fontsize=10)
    ax.set_xlabel("Minutes a resting order waits before we give up (W)", fontsize=10)
    ax.set_title("C. What it would add per day", loc="left", fontweight="bold", fontsize=11)
    ax.set_xticks(xs, xl)
    ax.legend(fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=1, frameon=False)
    ax.grid(alpha=0.3)
    fig.add_subplot(gs[4]).axis("off")
    acs._glossary(fig.add_subplot(gs[5]), [
        acs.GLOSS_TICK,
        ("high-frequency arb", "buy NO on both legs of a race when its gap to payout is 4+ ticks above its 24-h median; sell at +2 ticks per set or after 24 h"),
        ("cross the spread", "trade at once against the best price on offer (today's rule)"),
        ("resting order", "an order that joins the best price and waits behind the orders already there until a buyer arrives"),
        ("leg-in", "when one leg's resting order fills, the other leg is sold at once, so we never hold one leg alone for long"),
        ("signal", "a moment the high-frequency arb would buy; 24 on training, 10 on test"),
        ("net profit", "after the spread, 0.1 tick slippage per contract crossed, and 20% rejected baskets for crossed entries"),
        ("training / test", "training: 22 Oct; test: 23 Oct (real quotes and every public trade both days)"),
    ], ncol=2, width=56, fontsize=9)
    fig.savefig(os.path.join(FIG, "maker_arb_decision.png"), dpi=130, facecolor="white")


def main():
    TP, QS, mm, dropped = load()
    bk = Book(TP, QS)
    B = panels()
    if len(sys.argv) > 1 and sys.argv[1] == "--stage3":
        s3 = stage3(bk, B)
        s3s = summ3(s3)
        print(md3(s3s), "capacity (front of queue, shares/day at the ask while gap >= +4):", s3["capacity"])
        old = pickle.load(open(os.path.join(OUT, "results.pkl"), "rb"))
        old.update(s3=s3, s3s=s3s)
        pickle.dump(old, open(os.path.join(OUT, "results.pkl"), "wb"))
        with open(os.path.join(OUT, "tables.md"), "a") as fh:
            fh.write(md3(s3s) + f"\nFront-of-queue capacity (buy-aggressor shares at the ask, both legs, gap >= +4): {s3['capacity']}\n")
        return
    c0 = stage0(bk, mm, B)
    print("stage0", c0)
    rows = stage1(bk, B)
    s1 = {}
    for side, nm in ((+1, "YES offer at the ask (= NO entry)"), (-1, "YES bid at the bid (= NO exit)")):
        for lab, cond in (("all times", lambda r: True), ("gap >= +2", lambda r: r["dev"] >= 2),
                          ("gap >= +4", lambda r: r["dev"] >= 4)):
            s1[(nm, lab)] = summ1(rows, lambda r, s=side, c=cond: r["side"] == s and c(r))
    s2 = stage2(bk, B)
    txt = md(c0, s1, s2, dropped)
    open(os.path.join(OUT, "tables.md"), "w").write(txt)
    print(txt)
    pickle.dump(dict(c0=c0, s1=s1, s2=s2), open(os.path.join(OUT, "results.pkl"), "wb"))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--figures":
        figure(pickle.load(open(os.path.join(OUT, "results.pkl"), "rb")))
    else:
        main()
