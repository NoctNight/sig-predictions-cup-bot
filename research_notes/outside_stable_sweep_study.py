"""Outside-move-then-stable sweep: when Polymarket AND Kalshi move >= M pts within W min, then hold steady for S min
(outside fair range < R pts), and SIG has moved < 1/3 of it (gap to the outside fair >= G ticks), take SIG's stale touch in
the direction of the outside move one minute later; exit when SIG's mid has closed >= 70% of the entry gap, or at the time
stop H (1, 4, 12 h), taker at the opposite touch (T/T) or at the mid (T/mid: passive-exit upper bound).

Read-only research: opens data/sigbot.sqlite with mode=ro, reads JSON in data/, writes only data/research_outside_sweep/.
No venue calls. Reproduce:  python3 research_notes/outside_stable_sweep_study.py
One tick = 0.005. P&L in ticks per share, net of the touch spread and 0.05 tick per taker side (realised slippage over the
half spread on our own <= 3k-share taker orders, directional_impact.md section 4).

Samples (same split as directional_impact.md):
  A    7-21 Oct, SIG 5-min candle close as mid (+- leg median spread / 2), PM 5-min + Kalshi ~1-3-min D-leg history.
       IS   = A before 17 Oct 00:00 UTC (9.0 d, 57% of the days)
       OOS1 = A from 17 Oct 00:00 UTC (4.9 d)
  B    21 Oct 23:49 - 23 Oct 20:29 UTC, live bid/ask (venue_fair + quotes), PM and Kalshi per poll (venue_fair).  OOS2.
"""
import bisect
import collections
import itertools
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "research_notes"))
from sigbot.proposals.volatile_moves import load_markets, load_outside, load_candles  # noqa: E402 (pure loaders)
import directional_impact_study as DI  # noqa: E402 (pure helpers: load_books, walk, leg_liquidity, load_B, B_spreads)

DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "research_outside_sweep")
os.makedirs(OUT, exist_ok=True)
T = 0.005
SPLIT = datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()
A_START = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc).timestamp()
SLIP = 0.05             # extra ticks per taker side beyond the half spread
CAP_NOTIONAL = 600.0
MAX_Q = 2000
PARTISAN_CAP = 5000.0
AGREE = 0.04            # PM-Kalshi level agreement for a consensus
VENUE_SHARE = 0.5       # each venue must have moved >= 0.5 M in the move's direction
SIG_FRAC = 1 / 3        # SIG moved < 1/3 of the outside move
CLOSE_FRAC = 0.7        # exit when SIG's mid has closed >= 70% of the entry gap
LAT = 60                # entry latency (s)
W_MIN = 60              # move window (min), fixed


def iso(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        digits = "".join(ch for ch in tail if ch.isdigit())
        tz = tail[len(digits):]
        s = f"{head}.{digits[:6]}{tz}"
    return datetime.fromisoformat(s).timestamp()


def fmt(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %H:%M")


def clustered(rows, key):
    xs = [(r["race"], r[key]) for r in rows if r.get(key) is not None]
    n = len(xs)
    if n == 0:
        return None, None
    m = sum(x for _, x in xs) / n
    if n < 2:
        return m, None
    g = collections.defaultdict(float)
    for c, x in xs:
        g[c] += x - m
    G = len(g)
    if G < 2:
        return m, None
    var = sum(v * v for v in g.values()) / n / n * G / (G - 1)
    return m, (m / math.sqrt(var) if var > 1e-12 else None)


def drop_top(rows, key, k=3):
    tot = collections.defaultdict(float)
    for r in rows:
        if r.get(key) is not None:
            tot[r["race"]] += r[key]
    top = [r for r, _ in sorted(tot.items(), key=lambda kv: -kv[1])[:k]]
    return [r for r in rows if r["race"] not in top], top


# ----------------------------------------------------------------------------------------------- series per race
class Race:
    """Grid series for one race's traded leg (D leg). Arrays aligned on `ts` (step seconds)."""

    def __init__(self, race, ex, party, ts, pm, k, bid, ask, step):
        self.race, self.ex, self.party, self.ts, self.pm, self.k, self.bid, self.ask, self.step = race, ex, party, ts, pm, k, bid, ask, step
        self.mid = [None if b is None or a is None else 0.5 * (a + b) for b, a in zip(bid, ask)]
        self.F = [0.5 * (p + q) if (p is not None and q is not None and abs(p - q) <= AGREE + 1e-9) else None for p, q in zip(pm, k)]

    def idx(self, t):
        return int(round((t - self.ts[0]) / self.step))


def ff(ts, vals, t, max_age):
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or (max_age is not None and t - ts[i] > max_age):
        return None
    return vals[i]


def build_A():
    mk = load_markets()
    outside = load_outside()
    candles = load_candles()
    con = DI.db()
    spreads = DI.B_spreads(DI.load_B(con))
    d_leg = {race: ex for ex, (race, party, _) in mk.items() if party == "D"}
    t1 = max(max(c["ts"]) for c in candles.values()) + 300
    step = 300
    grid = [A_START + step * i for i in range(int((t1 - A_START) / step) + 1)]
    races = {}
    for race, o in outside.items():
        ex = d_leg.get(race)
        if ex is None or ex not in candles or "pm" not in o or "kalshi" not in o:
            continue
        sp = spreads.get(ex)
        sp = 1.0 if sp is None else sp
        if sp > 2.0 + 1e-9:
            continue
        c = candles[ex]
        cts = [x + 300 for x in c["ts"]]          # close stamped at bar end
        pts, pvs = o["pm"]
        kts, kvs = o["kalshi"]
        pm = [ff(pts, pvs, t, 1800) for t in grid]
        kk = [ff(kts, kvs, t, 1800) for t in grid]
        bid, ask = [], []
        for t in grid:
            m = ff(cts, c["close"], t, None)
            if m is None:
                bid.append(None), ask.append(None)
            else:
                bid.append(m - sp * T / 2), ask.append(m + sp * T / 2)
        races[race] = Race(race, ex, "D", grid, pm, kk, bid, ask, step)
    return races, grid[0], grid[-1]


def build_B(prefer="D"):
    con = sqlite3.connect(f"file:{os.path.join(DATA, 'sigbot.sqlite')}?mode=ro", uri=True)
    mk = load_markets()
    obs = collections.defaultdict(list)
    outs = collections.defaultdict(list)
    for ts, race, party, pm, k, bid, ask, ex in con.execute(
            "SELECT ts, race, party, pm, kalshi, sig_bid, sig_ask, exchange_id FROM venue_fair WHERE exchange_id IS NOT NULL ORDER BY ts"):
        ex = str(ex)
        if bid is not None and ask is not None and ask > bid:
            obs[ex].append((ts, bid, ask))
        outs[ex].append((ts, pm, k))
    for ts, ex, bid, ask in con.execute("SELECT ts, exchange_id, best_bid, best_ask FROM quotes ORDER BY ts"):
        if bid is not None and ask is not None and ask > bid:
            obs[str(ex)].append((ts, bid, ask))
    for ex in obs:
        obs[ex].sort()
    t0 = min(v[0][0] for v in outs.values())
    t1 = max(v[-1][0] for v in outs.values())
    step = 60
    grid = [t0 + step * i for i in range(int((t1 - t0) / step) + 1)]
    by_race = collections.defaultdict(dict)
    for ex, (race, party, _) in mk.items():
        if ex in outs and ex in obs and party in ("D", "R"):
            by_race[race][party] = ex
    races = {}
    for race, legs in by_race.items():
        if prefer == "D":
            party = "D" if "D" in legs else "R"
        else:
            if prefer not in legs:
                continue
            party = prefer
        ex = legs[party]
        ots = [x[0] for x in outs[ex]]
        pm = [ff(ots, [x[1] for x in outs[ex]], t, 420) for t in grid]
        kk = [ff(ots, [x[2] for x in outs[ex]], t, 420) for t in grid]
        sts = [x[0] for x in obs[ex]]
        bid = [ff(sts, [x[1] for x in obs[ex]], t, 360) for t in grid]
        ask = [ff(sts, [x[2] for x in obs[ex]], t, 360) for t in grid]
        races[race] = Race(race, ex, party, grid, pm, kk, bid, ask, step)
    return races, grid[0], grid[-1]


# netD from the bot log (local BST stamps) for the partisan-cap check in B
NETD = []
for line in open(os.path.join(DATA, "sigbot.log"), errors="ignore"):
    m_ = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*netD=([+-]?\d+)", line)
    if m_:
        t_ = datetime.strptime(m_.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1))).timestamp()
        NETD.append((t_, float(m_.group(2))))
NETD.sort()
NETD_TS = [x[0] for x in NETD]


def netd_at(t):
    i = bisect.bisect_right(NETD_TS, t) - 1
    return NETD[i][1] if i >= 0 else None


# ----------------------------------------------------------------------------------------------- the rule
def signals(R, M, S, G, Rng, H, stable=True, W=W_MIN, t_lo=None, t_hi=None, lat=LAT):
    """Return trades for one race. M pts, S min, G ticks, Rng pts, H hours. stable=False: no stabilisation filter (S=0)."""
    st = R.step
    nW, nS = int(W * 60 / st), (int(S * 60 / st) if stable else 0)
    nH = int(H * 3600 / st)
    nL = max(1, int(round(lat / st)))       # latency: next grid point (A: next 5-min close; B: +60 s)
    out = []
    i = nW + nS
    if t_lo is not None:
        i = max(i, R.idx(t_lo))
    n = len(R.ts)
    while i < n - 1:
        t = R.ts[i]
        if t_hi is not None and t > t_hi:
            break
        if t_lo is not None and t < t_lo:
            i += 1
            continue
        a, b = i - nS - nW, i - nS
        Fa, Fb, Fi = R.F[a], R.F[b], R.F[i]
        if Fa is None or Fb is None or Fi is None:
            i += 1
            continue
        dF = Fb - Fa
        if abs(dF) < M / 100 - 1e-9:
            i += 1
            continue
        d = 1 if dF > 0 else -1
        # both venues moved in the direction by >= VENUE_SHARE * M over the move window
        if (R.pm[b] - R.pm[a]) * d < VENUE_SHARE * M / 100 - 1e-9 or (R.k[b] - R.k[a]) * d < VENUE_SHARE * M / 100 - 1e-9:
            i += 1
            continue
        if stable:
            seg = R.F[b:i + 1]
            if any(x is None for x in seg) or max(seg) - min(seg) >= Rng / 100 - 1e-9:
                i += 1
                continue
        tot = (Fi - Fa) * d                      # outside move up to the check, signed
        ma, mi = R.mid[a], R.mid[i]
        if ma is None or mi is None or not (0.03 <= mi <= 0.97):
            i += 1
            continue
        if (mi - ma) * d >= SIG_FRAC * tot - 1e-9:
            i += 1
            continue
        g = (Fi - mi) * d / T
        if g < G - 1e-9:
            i += 1
            continue
        e = i + nL
        if e >= n or R.bid[e] is None or R.ask[e] is None:
            i += 1
            continue
        bid, ask = R.bid[e], R.ask[e]
        sp = (ask - bid) / T
        if sp > 2 + 1e-9:
            i += 1
            continue
        px_in = ask if d > 0 else bid
        m_in = 0.5 * (bid + ask)
        g_in = (Fi - m_in) * d / T
        if g_in <= 0:
            i += 1
            continue
        target = m_in + d * CLOSE_FRAC * g_in * T
        # exit scan
        x, how = None, None
        for j in range(e + 1, min(n, e + nH + 1)):
            mj = R.mid[j]
            if mj is not None and (mj - target) * d >= -1e-9:
                x, how = j, "close"
                break
        if x is None:
            j = e + nH
            while j < n and j <= e + nH + int(7200 / st) and R.mid[j] is None:
                j += 1                      # first quote after a data hole, at most 2 h late
            if j >= n or R.mid[j] is None:
                break                       # no outcome (data end)
            x, how = j, "time"
        bx, ax = R.bid[x], R.ask[x]
        px_out = bx if d > 0 else ax
        tt = (px_out - px_in) * d / T - 2 * SLIP
        tm = (0.5 * (bx + ax) - px_in) * d / T - SLIP
        mm = (0.5 * (bx + ax) - m_in) * d / T
        paid = px_in if d > 0 else 1 - px_in
        q = min(MAX_Q, int(CAP_NOTIONAL / max(paid, 0.02)))
        adds_d = (R.party == "D" and d > 0) or (R.party == "R" and d < 0)
        out.append(dict(race=R.race, ex=R.ex, party=R.party, t=t, t_in=R.ts[e], t_out=R.ts[x], hold_h=round((R.ts[x] - R.ts[e]) / 3600, 2),
                        dir=d, out_move=round(tot * 100, 2), sig_move=round((mi - ma) * d * 100, 2), gap=round(g_in, 1), spread=round(sp, 1),
                        exit=how, tt=round(tt, 3), tm=round(tm, 3), mm=round(mm, 3), qty=q, paid=round(paid, 4), adds_d=adds_d,
                        day=fmt(t)[:2]))
        # cooldown: next signal on this race only after the exit and after the move window has rolled past the entry
        i = max(x, e + nW + nS) + 1
    return out


def run_sample(races, M, S, G, Rng, H, stable=True, t_lo=None, t_hi=None, W=W_MIN, lat=LAT):
    rows = []
    for R in races.values():
        rows += signals(R, M, S, G, Rng, H, stable, W, t_lo, t_hi, lat)
    return rows


def summary(rows, days, key="tt"):
    xs = [r for r in rows if r.get(key) is not None]
    if not xs:
        return dict(n=0)
    m, t = clustered(xs, key)
    s_day = sum(r[key] * T * r["qty"] for r in xs) / days
    feas = [r for r in xs if not r["adds_d"]]
    s_day_r = sum(r[key] * T * r["qty"] for r in feas) / days
    mr, tr = clustered(feas, key)
    ex3, top = drop_top(xs, key)
    m3, t3 = clustered(ex3, key)
    return dict(n=len(xs), races=len({r["race"] for r in xs}), hit=round(sum(r[key] > 0 for r in xs) / len(xs), 2), mean=round(m, 2),
                t=None if t is None else round(t, 1), per_day=round(len(xs) / days, 1), s_day=round(s_day, 1),
                share_adds_d=round(sum(r["adds_d"] for r in xs) / len(xs), 2),
                r_only_n=len(feas), r_only_mean=None if mr is None else round(mr, 2), r_only_s_day=round(s_day_r, 1),
                ex3_n=len(ex3), ex3_mean=None if m3 is None else round(m3, 2), ex3_t=None if t3 is None else round(t3, 1), top3=top,
                close_exits=round(sum(r["exit"] == "close" for r in xs) / len(xs), 2), mean_hold_h=round(statistics.mean(r["hold_h"] for r in xs), 2),
                mean_gap=round(statistics.mean(r["gap"] for r in xs), 1), mid_mid=round(statistics.mean(r["mm"] for r in xs), 2),
                t_mid=round(statistics.mean(r["tm"] for r in xs), 2), med_qty=int(statistics.median(r["qty"] for r in xs)))


def fmt_s(s):
    if not s or not s.get("n"):
        return "0 | - | - | - | - | -"
    t = "" if s["t"] is None else f" ({s['t']:+.1f})"
    e3 = "-" if s["ex3_mean"] is None else f"{s['ex3_mean']:+.2f}" + ("" if s["ex3_t"] is None else f" ({s['ex3_t']:+.1f})")
    return f"{s['n']} | {s['hit']:.2f} | {s['mean']:+.2f}{t} | {e3} | {s['per_day']} | {s['s_day']:+.0f}"


# ----------------------------------------------------------------------------------------------- sizing
def sizing(m, sigma, risk_tol=2000.0):
    """q* per liquid ladder (books.json 21 Oct 22:03), maximising q(m - walk_in - walk_out) tick - (q sigma tick)^2 / (2 T)."""
    if m is None or m <= 0:
        return dict(q_med=0, s_trade=0.0)
    books = DI.load_books()
    con = DI.db()
    liq = DI.leg_liquidity(books, DI.B_spreads(DI.load_B(con)))
    grid = [100 * i for i in range(1, 201)] + [20000 + 1000 * i for i in range(1, 81)]
    qs, gs, ns = [], [], []
    for ex, (bids, asks) in books.items():
        if not liq.get(ex, {}).get("liquid"):
            continue
        for inn, outl, px in ((asks, bids, asks[0][0]), (bids, asks, 1 - bids[0][0])):
            best, bq = 0.0, 0
            for q in grid:
                wi, wo = DI.walk(inn, q), DI.walk(outl, q)
                if wi is None or wo is None:
                    break
                v = q * (m - wi - wo) * T - (q * sigma * T) ** 2 / (2 * risk_tol)
                if v > best:
                    best, bq = v, q
            qs.append(bq), gs.append(best), ns.append(bq * px)
    return dict(q_med=int(statistics.median(qs)), q_p25=int(sorted(qs)[len(qs) // 4]), q_p75=int(sorted(qs)[3 * len(qs) // 4]),
                notional_med=round(statistics.median(ns)), s_trade=round(statistics.median(gs), 1), s_trade_cap=round(750 * m * T, 1))


# ----------------------------------------------------------------------------------------------- main
def main():
    A, a0, a1 = build_A()
    B, b0, b1 = build_B()
    res = dict(meta=dict(A=(fmt(a0), fmt(a1), len(A)), B=(fmt(b0), fmt(b1), len(B))))
    print(f"A {fmt(a0)}..{fmt(a1)} races {len(A)};  B {fmt(b0)}..{fmt(b1)} races {len(B)}")

    def days(lo, hi, H):
        return max(0.05, (hi - lo - H * 3600) / 86400)

    samples = dict(IS=(A, a0, SPLIT), OOS1=(A, SPLIT, a1), OOS2=(B, b0, b1))
    Ms, Ss, Gs, Hs = (1.5, 2.0, 3.0), (15, 30), (2, 4), (1, 4, 12)
    RNG = 1.0
    grid_rows = []
    allrows = {}
    n_variants = 0
    for stable in (True, False):
        for M, S, G, H in itertools.product(Ms, Ss if stable else (0,), Gs, Hs):
            n_variants += 1
            key = (stable, M, S, G, H)
            rec = dict(stable=stable, M=M, S=S, G=G, H=H)
            for lab, (rs, lo, hi) in samples.items():
                rows = run_sample(rs, M, S, G, RNG, H, stable, lo, hi - H * 3600)
                allrows[(key, lab)] = rows
                rec[lab] = summary(rows, days(lo, hi, H))
            pool = allrows[(key, "OOS1")] + allrows[(key, "OOS2")]
            rec["OOS"] = summary(pool, days(SPLIT, a1, H) + days(b0, b1, H))
            grid_rows.append(rec)
    res["n_variants_main_grid"] = n_variants
    res["grid"] = grid_rows

    def show(rec):
        return f"| {'stable' if rec['stable'] else 'no filter'} | {rec['M']} | {rec['S']} | {rec['G']} | {rec['H']} | " + " | ".join(
            fmt_s(rec[l]) for l in ("IS", "OOS1", "OOS2", "OOS"))

    print("\n## Full grid (T/T ticks/share; cols per sample: n | hit | mean (t) | ex-top-3 mean (t) | signals/day | S/day at 600-notional)\n")
    print("| filter | M | S | G | H | " + " | ".join(f"{l} n | hit | mean (t) | ex3 | /day | S/day" for l in ("IS", "OOS1", "OOS2", "OOS pooled")) + " |")
    print("|" + "---|" * (5 + 24))
    for rec in grid_rows:
        print(show(rec))

    # selection on IS only: best clustered t among stable variants with n >= 15
    cand = [r for r in grid_rows if r["stable"] and r["IS"].get("n", 0) >= 15 and r["IS"].get("t") is not None]
    best = max(cand, key=lambda r: r["IS"]["t"]) if cand else None
    res["is_best"] = best
    default = next(r for r in grid_rows if r["stable"] and r["M"] == 2.0 and r["S"] == 30 and r["G"] == 4 and r["H"] == 4)
    res["default"] = default
    print("\nIS-best stable variant:", {k: best[k] for k in ("M", "S", "G", "H")} if best else None)
    for name, rec in (("IS-best", best), ("default M2 S30 G4 H4", default)):
        if rec:
            print(f"\n### {name}")
            for lab in ("IS", "OOS1", "OOS2", "OOS"):
                print(lab, rec[lab])

    # how many OOS-pooled variants pass the bar
    passes = [r for r in grid_rows if r["OOS"].get("n", 0) >= 10 and (r["OOS"].get("t") or 0) > 2 and (r["OOS"].get("ex3_mean") or -1) > 0]
    res["oos_pass"] = [{k: r[k] for k in ("stable", "M", "S", "G", "H")} | dict(OOS=r["OOS"], IS=r["IS"]) for r in passes]
    print("\nvariants passing OOS (t>2 and ex-top-3 > 0):", len(passes))
    for r in passes:
        print({k: r[k] for k in ("stable", "M", "S", "G", "H")}, r["IS"].get("mean"), r["IS"].get("t"), "|", r["OOS"])

    # stable vs no-filter, paired by (M, G, H): mean over S for stable
    print("\n## Stabilisation filter vs none (pooled OOS T/T mean, n)\n")
    comp = []
    for M, G, H in itertools.product(Ms, Gs, Hs):
        nf = next(r for r in grid_rows if not r["stable"] and r["M"] == M and r["G"] == G and r["H"] == H)
        st = [r for r in grid_rows if r["stable"] and r["M"] == M and r["G"] == G and r["H"] == H]
        comp.append(dict(M=M, G=G, H=H, none=(nf["IS"].get("n"), nf["IS"].get("mean"), nf["OOS"].get("n"), nf["OOS"].get("mean"), nf["OOS"].get("t")),
                         S15=(st[0]["IS"].get("n"), st[0]["IS"].get("mean"), st[0]["OOS"].get("n"), st[0]["OOS"].get("mean"), st[0]["OOS"].get("t")),
                         S30=(st[1]["IS"].get("n"), st[1]["IS"].get("mean"), st[1]["OOS"].get("n"), st[1]["OOS"].get("mean"), st[1]["OOS"].get("t"))))
    res["stable_vs_none"] = comp
    for c in comp:
        print(c)

    # robustness of the IS-best and default: R = 0.5 pt, the lookback's 30-min window (no filter), R leg in B
    extra = {}
    for name, rec in (("best", best), ("default", default)):
        if not rec:
            continue
        M, S, G, H = rec["M"], rec["S"], rec["G"], rec["H"]
        for lab, (rs, lo, hi) in samples.items():
            rows = run_sample(rs, M, S, G, 0.5, H, True, lo, hi - H * 3600)
            extra[f"{name}_R0.5_{lab}"] = summary(rows, days(lo, hi, H))
            n_variants += 1 if lab == "IS" else 0
    for lab, (rs, lo, hi) in samples.items():
        rows = run_sample(rs, 2.0, 0, 0, 1.0, 4, False, lo, hi - 4 * 3600, W=30)
        extra[f"lookback_like_W30_M2_G0_H4_{lab}"] = summary(rows, days(lo, hi, 4))
    n_variants += 1
    res["extra"] = extra
    res["n_variants_total"] = n_variants
    print("\n## extra")
    for k, v in extra.items():
        print(k, v)

    # by-day for the IS-best and default (all samples)
    for name, rec in (("best", best), ("default", default)):
        if not rec:
            continue
        key = (True, rec["M"], rec["S"], rec["G"], rec["H"])
        byday = collections.defaultdict(list)
        for lab in ("IS", "OOS1", "OOS2"):
            for r in allrows[(key, lab)]:
                byday[(lab, r["day"])].append(r["tt"])
        res[f"byday_{name}"] = {f"{k[0]} {k[1]} Oct": (len(v), round(statistics.mean(v), 2)) for k, v in sorted(byday.items(), key=lambda kv: kv[0][1])}
        print(f"\nby day {name}:", res[f"byday_{name}"])
        # trade list OOS2
        res[f"trades_OOS2_{name}"] = [dict(r, t=fmt(r["t"]), t_in=fmt(r["t_in"]), t_out=fmt(r["t_out"]), netD=netd_at(r["t"])) for r in allrows[(key, "OOS2")]]
        res[f"trades_OOS1_{name}"] = [dict(r, t=fmt(r["t"]), t_in=fmt(r["t_in"]), t_out=fmt(r["t_out"])) for r in allrows[(key, "OOS1")]]

    # MN-01 example (22 Oct 10:00-13:00 UTC): which variants fired on MN-01?
    mn = []
    for (key, lab), rows in allrows.items():
        if lab != "OOS2":
            continue
        for r in rows:
            if r["race"].startswith("MN-01") and datetime(2026, 10, 22, 9, tzinfo=timezone.utc).timestamp() <= r["t"] <= datetime(2026, 10, 22, 14, tzinfo=timezone.utc).timestamp():
                mn.append(dict(variant=key, t=fmt(r["t"]), gap=r["gap"], out_move=r["out_move"], sig_move=r["sig_move"], tt=r["tt"], exit=r["exit"], hold=r["hold_h"]))
    res["mn01_example"] = mn
    print("\nMN-01 firings:", len(mn))
    for x in mn[:20]:
        print(x)
    # trace MN-01 on B
    R = B.get("MN-01 House race")
    if R:
        tr = []
        for t in range(int(datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc).timestamp()), int(datetime(2026, 10, 22, 13, 0, tzinfo=timezone.utc).timestamp()), 900):
            i = R.idx(t)
            tr.append((fmt(R.ts[i]), R.party, R.pm[i], R.k[i], R.F[i] and round(R.F[i], 4), R.bid[i], R.ask[i]))
        res["mn01_trace"] = tr
        for x in tr:
            print(x)

    # sizing from the impact model at the OOS mean of the IS-best and default
    sz = {}
    for name, rec in (("best", best), ("default", default)):
        if not rec:
            continue
        key = (True, rec["M"], rec["S"], rec["G"], rec["H"])
        pool = allrows[(key, "OOS1")] + allrows[(key, "OOS2")]
        vals = [r["tt"] for r in pool]
        m = rec["OOS"].get("mean")
        sigma = statistics.pstdev(vals) if len(vals) > 1 else 3.0
        sz[name] = dict(m=m, sigma=round(sigma, 2), **sizing(m, sigma))
        ex3 = rec["OOS"].get("ex3_mean")
        sz[name + "_ex3"] = dict(m=ex3, sigma=round(sigma, 2), **sizing(ex3, sigma))
    res["sizing"] = sz
    print("\nsizing", sz)

    # ---- why waiting hurts: SIG mid path after the no-filter signal (M 2, G 2), signed toward the outside move, ticks
    paths = {}
    for lab, (rs, lo, hi) in samples.items():
        rows = run_sample(rs, 2.0, 0, 2, 1.0, 4, False, lo, hi - 4 * 3600)
        acc = collections.defaultdict(list)
        for r in rows:
            Rr = rs[r["race"]]
            i0 = Rr.idx(r["t"])
            m0 = Rr.mid[i0]
            for mins in (5, 15, 30, 60, 120, 240):
                j = Rr.idx(r["t"] + 60 * mins)
                if j < len(Rr.ts) and Rr.mid[j] is not None:
                    acc[mins].append((Rr.mid[j] - m0) * r["dir"] / T)
            fj = Rr.idx(r["t"] + 14400)
            if fj < len(Rr.ts) and Rr.F[fj] is not None and Rr.F[i0] is not None:
                acc["fair240"].append((Rr.F[fj] - Rr.F[i0]) * r["dir"] / T)
        paths[lab] = {str(k): (len(v), round(statistics.mean(v), 2)) for k, v in acc.items() if v}
    res["sig_path_after_nofilter_signal"] = paths
    print("\nSIG mid path after no-filter M2 G2 signal (n, mean ticks toward outside):", paths)

    # ---- latency sensitivity (entry delay) for the no-filter M2 G2 H4 / H12 and the stable best
    lat_res = {}
    for (st_, M, S, G, H) in ((False, 2.0, 0, 2, 4), (False, 2.0, 0, 2, 12), (True, 1.5, 30, 2, 12)):
        for lat in (60, 300, 900, 1800):
            pool = []
            for lab in ("OOS1", "OOS2"):
                rs, lo, hi = samples[lab]
                pool += run_sample(rs, M, S, G, 1.0, H, st_, lo, hi - H * 3600, lat=lat)
            s_ = summary(pool, days(SPLIT, a1, H) + days(b0, b1, H))
            isr = summary(run_sample(A, M, S, G, 1.0, H, st_, a0, SPLIT - H * 3600, lat=lat), days(a0, SPLIT, H))
            lat_res[f"{'stable' if st_ else 'nofilter'} M{M} S{S} G{G} H{H} lat{lat}"] = dict(IS=(isr.get("n"), isr.get("mean"), isr.get("t")),
                                                                                             OOS=(s_.get("n"), s_.get("mean"), s_.get("t"), s_.get("ex3_mean"), s_.get("ex3_t")))
    res["latency"] = lat_res
    print("\nlatency:")
    for k, v in lat_res.items():
        print(k, v)

    # ---- B by day and R-leg replication for the passing no-filter variants and the stable best
    BR, _, _ = build_B("R")
    rep = {}
    for (st_, M, S, G, H) in ((False, 2.0, 0, 2, 4), (False, 2.0, 0, 2, 12), (False, 2.0, 0, 4, 4), (False, 2.0, 0, 4, 12),
                              (True, 1.5, 30, 2, 12), (True, 2.0, 30, 4, 4)):
        name = f"{'stable' if st_ else 'nofilter'} M{M} S{S} G{G} H{H}"
        rowsD = run_sample(B, M, S, G, 1.0, H, st_, b0, b1 - H * 3600)
        rowsR = run_sample(BR, M, S, G, 1.0, H, st_, b0, b1 - H * 3600)
        byd = collections.defaultdict(list)
        for r in rowsD:
            byd[r["day"]].append(r["tt"])
        rep[name] = dict(B_D_byday={k: (len(v), round(statistics.mean(v), 2)) for k, v in sorted(byd.items())},
                         B_Rleg=summary(rowsR, days(b0, b1, H)), B_Dleg=summary(rowsD, days(b0, b1, H)))
    res["B_replication"] = rep
    print("\nB replication:")
    for k, v in rep.items():
        print(k, v["B_D_byday"], "| R leg:", {x: v["B_Rleg"].get(x) for x in ("n", "hit", "mean", "t", "ex3_mean", "ex3_t")})

    json.dump(res, open(os.path.join(OUT, "results.json"), "w"), indent=1, default=str)


# ----------------------------------------------------------------------------------------------- fine outside data (fetch)
RAW = os.path.join(OUT, "raw")
F_START = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc).timestamp()
F_END = datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc).timestamp()


def leg_instruments():
    """race -> dict(party, pm token, kalshi ticker) for the D leg (from venue_quotes, the bot's own mapping)."""
    con = sqlite3.connect(f"file:{os.path.join(DATA, 'sigbot.sqlite')}?mode=ro", uri=True)
    m = collections.defaultdict(dict)
    for race, party, venue, ins in con.execute("SELECT DISTINCT race, party, venue, instrument FROM venue_quotes"):
        m[(race, party)][venue] = ins
    out = {}
    for (race, party), v in m.items():
        if party == "D" and "polymarket" in v and "kalshi" in v:
            out[race] = dict(party="D", pm=v["polymarket"], kalshi=v["kalshi"], sig=v.get("sig"))
    return out


def fetch():
    """Polymarket CLOB prices-history (fidelity 1 = ~1-min mid), Polymarket data-api trades (tick level), Kalshi 1-min
    candlesticks (bid/ask close) for every D leg, 8-23 Oct. Public endpoints only; cached in data/research_outside_sweep/raw/."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    os.makedirs(RAW, exist_ok=True)

    import subprocess

    def get(url, tries=4):
        """curl (the system trust store; this Python build has no CA bundle)."""
        for k in range(tries):
            try:
                r = subprocess.run(["curl", "-s", "--max-time", "30", "-H", "Accept: application/json", url], capture_output=True, timeout=40)
                return json.loads(r.stdout.decode())
            except Exception as e:  # noqa: BLE001
                if k == tries - 1:
                    return {"_error": str(e)}
                time.sleep(1.5 * (k + 1))

    def one(item):
        race, leg = item
        fn = os.path.join(RAW, re.sub(r"[^A-Za-z0-9]+", "_", race) + ".json")
        if os.path.exists(fn):
            return race, "cached"
        rec = dict(race=race, **leg, pm_hist=[], pm_trades=[], k_candles=[])
        t = F_START
        while t < F_END:
            u = min(F_END, t + 2 * 86400)
            h = get(f"https://clob.polymarket.com/prices-history?market={leg['pm']}&startTs={int(t)}&endTs={int(u)}&fidelity=1")
            rec["pm_hist"] += [(x["t"], x["p"]) for x in (h.get("history") or [])] if isinstance(h, dict) else []
            time.sleep(0.1)
            t = u
        g = get(f"https://gamma-api.polymarket.com/markets?clob_token_ids={leg['pm']}")
        cond = g[0].get("conditionId") if isinstance(g, list) and g else None
        rec["condition"] = cond
        if cond:
            off = 0
            while off <= 9500:
                tr = get(f"https://data-api.polymarket.com/trades?market={cond}&limit=500&offset={off}")
                if not isinstance(tr, list) or not tr:
                    break
                for x in tr:
                    px = float(x["price"])
                    if x.get("asset") != leg["pm"]:
                        px = 1 - px                  # the other outcome token: convert to D-YES terms
                    rec["pm_trades"].append((int(x["timestamp"]), px, float(x["size"])))
                if min(int(x["timestamp"]) for x in tr) < F_START:
                    break
                off += 500
                time.sleep(0.1)
        series = leg["kalshi"].split("-")[0]
        t = F_START
        while t < F_END:
            u = min(F_END, t + 3 * 86400)
            c = get(f"https://api.elections.kalshi.com/trade-api/v2/series/{series}/markets/{leg['kalshi']}/candlesticks"
                    f"?start_ts={int(t)}&end_ts={int(u)}&period_interval=1")
            for x in (c.get("candlesticks") or []) if isinstance(c, dict) else []:
                b, a = x.get("yes_bid", {}).get("close_dollars"), x.get("yes_ask", {}).get("close_dollars")
                if b is not None and a is not None:
                    rec["k_candles"].append((x["end_period_ts"], float(b), float(a)))
            time.sleep(0.15)
            t = u
        json.dump(rec, open(fn, "w"))
        return race, f"pm {len(rec['pm_hist'])} trades {len(rec['pm_trades'])} k {len(rec['k_candles'])}"

    legs = leg_instruments()
    with ThreadPoolExecutor(4) as ex:
        for race, msg in ex.map(one, sorted(legs.items())):
            print(race, msg, flush=True)


# ----------------------------------------------------------------------------------------------- x-study (wait x seconds)
XS = (0, 15, 30, 60, 120, 300, 600)
XM = (1.5, 2.0, 3.0, 5.0)
XH = (4, 12)
RETAIN = 0.7            # REAL: >= 70% of the move still there at +1 h and +4 h (or SIG followed >= 50% by +4 h)
EVENT_GAP = 7200        # one outside-move event per race per 2 h (the event set is the same for every x)
X_LAT = 2               # streaming: websocket detect + order round trip (s)
K_AGE = 6 * 3600     # Kalshi 1-min candles are emitted only in minutes where the book or trades changed: forward-fill
TRADE_TOL = 0.01        # trade prints may sit a half spread (~1c) off the mid; a print counts as a reversal only beyond it


def load_fine():
    """race -> dict(pm=(ts, px) ~1-min mids, pmt=(ts, px) trades, k=(ts, mid) 1-min Kalshi bid/ask close mid)."""
    out = {}
    for fn in sorted(os.listdir(RAW)):
        if not fn.endswith(".json"):
            continue
        r = json.load(open(os.path.join(RAW, fn)))
        pm = sorted(set((float(t), float(p)) for t, p in r["pm_hist"]))
        pmt = sorted((float(t), float(p)) for t, p, _ in r["pm_trades"])
        k = sorted((float(t), 0.5 * (b + a)) for t, b, a in r["k_candles"] if a > b > 0 or (a >= b and a > 0))
        if len(pm) < 100 or len(k) < 20:
            continue
        out[r["race"]] = dict(pm=([x[0] for x in pm], [x[1] for x in pm]), pmt=([x[0] for x in pmt], [x[1] for x in pmt]),
                              k=([x[0] for x in k], [x[1] for x in k]), party=r.get("party", "D"))
    return out


def sig_paths():
    """exchange -> merged SIG path [(ts, bid, ask, max_age)]: A = 5-min candle close +- half median spread (to 21 Oct 21:30),
    B = live venue_fair + quotes from 21 Oct 23:49."""
    mk = load_markets()
    candles = load_candles()
    con = DI.db()
    spreads = DI.B_spreads(DI.load_B(con))
    con2 = sqlite3.connect(f"file:{os.path.join(DATA, 'sigbot.sqlite')}?mode=ro", uri=True)
    obs = collections.defaultdict(list)
    for ts, bid, ask, ex in con2.execute("SELECT ts, sig_bid, sig_ask, exchange_id FROM venue_fair WHERE exchange_id IS NOT NULL"):
        if bid is not None and ask is not None and ask > bid:
            obs[str(ex)].append((ts, bid, ask, 360))
    for ts, ex, bid, ask in con2.execute("SELECT ts, exchange_id, best_bid, best_ask FROM quotes"):
        if bid is not None and ask is not None and ask > bid:
            obs[str(ex)].append((ts, bid, ask, 360))
    b_start = min(v[0] for vs in obs.values() for v in vs)
    paths = {}
    for ex in set(candles) | set(obs):
        pts = []
        sp = spreads.get(ex)
        sp = 1.0 if sp is None else sp
        if ex in candles and sp <= 2.0 + 1e-9:
            c = candles[ex]
            for t, cl in zip(c["ts"], c["close"]):
                if t + 300 < b_start:
                    pts.append((t + 300, cl - sp * T / 2, cl + sp * T / 2, 6 * 3600))
        pts += obs.get(ex, [])
        pts.sort()
        if pts:
            paths[ex] = ([p[0] for p in pts], pts)
    d_leg = {race: ex for ex, (race, party, _) in mk.items() if party == "D"}
    return paths, d_leg, b_start


def q_at(path, t):
    ts, pts = path
    i = bisect.bisect_right(ts, t) - 1
    if i < 0:
        return None
    p = pts[i]
    if t - p[0] > p[3]:
        return None
    return p


def xstudy():
    fine = load_fine()
    paths, d_leg, b_start = sig_paths()
    a_end = datetime(2026, 10, 21, 21, 30, tzinfo=timezone.utc).timestamp()
    print(f"fine outside data for {len(fine)} races; SIG live quotes from {fmt(b_start)}")
    # resolution achieved
    res_pm = [b - a for f in fine.values() for a, b in zip(f["pm"][0], f["pm"][0][1:])]
    res_k = [b - a for f in fine.values() for a, b in zip(f["k"][0], f["k"][0][1:])]
    n_tr = sum(len(f["pmt"][0]) for f in fine.values())
    resinfo = dict(pm_hist_median_s=statistics.median(res_pm), pm_hist_p90_s=sorted(res_pm)[9 * len(res_pm) // 10],
                   kalshi_median_s=statistics.median(res_k), pm_trades=n_tr,
                   pm_trades_per_race_day=round(n_tr / len(fine) / ((F_END - F_START) / 86400), 1))
    print("resolution", resinfo)

    def at(series, t, age):
        ts, px = series
        i = bisect.bisect_right(ts, t) - 1
        if i < 0 or t - ts[i] > age:
            return None, None
        return px[i], ts[i]

    W = 3600
    events = collections.defaultdict(list)       # M -> events
    for race, f in fine.items():
        ex = d_leg.get(race)
        if ex is None or ex not in paths:
            continue
        P = paths[ex]
        for M in XM:
            last = -1e18
            t = F_START + W
            while t < F_END:
                if t - last < EVENT_GAP:
                    t += 60
                    continue
                pm1, tp = at(f["pm"], t, 600)
                k1, tk = at(f["k"], t, K_AGE)
                pm0, _ = at(f["pm"], t - W, 600)
                k0, _ = at(f["k"], t - W, K_AGE)
                if None in (pm1, k1, pm0, k0) or abs(pm1 - k1) > AGREE + 1e-9 or abs(pm0 - k0) > AGREE + 1e-9:
                    t += 60
                    continue
                F1, F0 = 0.5 * (pm1 + k1), 0.5 * (pm0 + k0)
                mv = F1 - F0
                d = 1 if mv > 0 else -1
                if abs(mv) < M / 100 - 1e-9 or (pm1 - pm0) * d < VENUE_SHARE * M / 100 - 1e-9 or (k1 - k0) * d < VENUE_SHARE * M / 100 - 1e-9:
                    t += 60
                    continue
                tc = max(tp, tk)                    # the moment both venues' data showing the move existed
                q0, qa = q_at(P, tc), q_at(P, tc - W)
                if q0 is None or qa is None:
                    t += 60
                    continue
                m0, ma = 0.5 * (q0[1] + q0[2]), 0.5 * (qa[1] + qa[2])
                if not (0.03 <= m0 <= 0.97) or (m0 - ma) * d >= SIG_FRAC * abs(mv) - 1e-9:
                    t += 60
                    continue
                g0 = (F1 - m0) * d / T
                if g0 < 2 - 1e-9:
                    t += 60
                    continue
                # REAL / NOISE label (uses the future: for evaluation only)
                def F_at(tt):
                    a_, _ = at(f["pm"], tt, 900)
                    b_, _ = at(f["k"], tt, K_AGE)
                    return None if a_ is None or b_ is None else 0.5 * (a_ + b_)
                f1h, f4h = F_at(tc + 3600), F_at(tc + 14400)
                q4 = q_at(P, tc + 14400)
                sig_follow = None if q4 is None else (0.5 * (q4[1] + q4[2]) - m0) * d >= 0.5 * abs(mv) - 1e-9
                keep = None if f1h is None or f4h is None else ((f1h - F0) * d >= RETAIN * abs(mv) and (f4h - F0) * d >= RETAIN * abs(mv))
                real = None if keep is None and sig_follow is None else bool(keep or sig_follow)
                events[M].append(dict(race=race, ex=ex, tc=tc, d=d, F0=F0, pm0=pm0, k0=k0, mvpm=(pm1 - pm0) * d, mvk=(k1 - k0) * d,
                                      mv=abs(mv), m0=m0, g0=g0, real=real, F1=F1))
                last = t
                t += 60
    for M in XM:
        print("M", M, "events", len(events[M]), "IS", sum(e["tc"] < SPLIT for e in events[M]))

    def stable_for(e, x, trades=False):
        f = fine[e["race"]]
        d = e["d"]
        for key, base, mv0 in (("pm", e["pm0"], e["mvpm"]), ("k", e["k0"], e["mvk"])):
            ts, px = f[key]
            lo, hi = bisect.bisect_right(ts, e["tc"]), bisect.bisect_right(ts, e["tc"] + x)
            for j in range(lo, hi):
                if (px[j] - base) * d < RETAIN * mv0 - 1e-9:
                    return False
        if trades:
            ts, px = f["pmt"]
            lo, hi = bisect.bisect_right(ts, e["tc"]), bisect.bisect_right(ts, e["tc"] + x)
            for j in range(lo, hi):
                if (px[j] - e["pm0"]) * d < RETAIN * e["mvpm"] - TRADE_TOL - 1e-9:
                    return False
        return True

    def trade(e, x, H, lat):
        P = paths[e["ex"]]
        d = e["d"]
        te = e["tc"] + x + lat
        q = q_at(P, te)
        if q is None:
            return None
        bid, ask = q[1], q[2]
        if (ask - bid) / T > 2 + 1e-9:
            return None
        m_in = 0.5 * (bid + ask)
        g_in = (e["F1"] - m_in) * d / T          # gap vs the consensus seen at detection
        if g_in < 1 - 1e-9:
            return dict(skip="caught_up", g_in=g_in)
        px_in = ask if d > 0 else bid
        target = m_in + d * CLOSE_FRAC * g_in * T
        ts, pts = P
        i = bisect.bisect_right(ts, te)
        x_pt, how = None, None
        while i < len(ts) and ts[i] <= te + H * 3600:
            p = pts[i]
            if (0.5 * (p[1] + p[2]) - target) * d >= -1e-9:
                x_pt, how = p, "close"
                break
            i += 1
        if x_pt is None:
            j = bisect.bisect_left(ts, te + H * 3600)
            if j >= len(ts) or ts[j] > te + H * 3600 + 7200:
                return None
            x_pt, how = pts[j], "time"
        px_out = x_pt[1] if d > 0 else x_pt[2]
        tt = (px_out - px_in) * d / T - 2 * SLIP
        paid = px_in if d > 0 else 1 - px_in
        m_out = 0.5 * (x_pt[1] + x_pt[2])
        return dict(race=e["race"], tt=tt, g_in=g_in, gap=g_in, qty=min(MAX_Q, int(CAP_NOTIONAL / max(paid, 0.02))), adds_d=d > 0,
                    exit=how, t=e["tc"], day=fmt(e["tc"])[:2], real=e["real"], hold_h=round((x_pt[0] - te) / 3600, 2),
                    mm=(m_out - m_in) * d / T, tm=(m_out - px_in) * d / T - SLIP)

    def sample_of(t, H):
        if t < SPLIT and t + H * 3600 < SPLIT + 86400:
            return "IS"
        return "OOS1" if t < a_end else ("OOS2" if t >= b_start else None)

    span = {"IS": (F_START + W, SPLIT), "OOS1": (SPLIT, a_end), "OOS2": (b_start, F_END)}

    def days_of(lab, H):
        lo, hi = span[lab]
        return max(0.05, (hi - lo - H * 3600) / 86400)

    table = []
    store = {}
    for M in XM:
        for H in XH:
            for x in XS:
                for lat, tr in ((X_LAT, False), (X_LAT, True), (60, False), (120, False)):
                    rows = collections.defaultdict(list)
                    stat = collections.defaultdict(lambda: collections.Counter())
                    gaps = collections.defaultdict(list)
                    for e in events[M]:
                        lab = sample_of(e["tc"], H)
                        if lab is None:
                            continue
                        if e["tc"] + x + lat + H * 3600 > F_END:
                            continue
                        stat[lab]["events"] += 1
                        stat[lab]["real"] += e["real"] is True
                        stat[lab]["labelled"] += e["real"] is not None
                        if not stable_for(e, x + lat, tr):
                            stat[lab]["unstable"] += 1
                            continue
                        stat[lab]["stable"] += 1
                        stat[lab]["stable_real"] += e["real"] is True
                        stat[lab]["stable_labelled"] += e["real"] is not None
                        r = trade(e, x, H, lat)
                        if r is None:
                            stat[lab]["no_quote"] += 1
                            continue
                        gaps[lab].append(max(0.0, r["g_in"]) / e["g0"])
                        if r.get("skip"):
                            stat[lab]["caught_up"] += 1
                            continue
                        rows[lab].append(r)
                    rec = dict(M=M, H=H, x=x, lat=lat, trades_filter=tr)
                    for lab in ("IS", "OOS1", "OOS2"):
                        s_ = summary(rows[lab], days_of(lab, H)) if rows[lab] else dict(n=0)
                        st = stat[lab]
                        s_.update(events=st["events"], p_real=None if not st["labelled"] else round(st["real"] / st["labelled"], 2),
                                  p_real_stable=None if not st["stable_labelled"] else round(st["stable_real"] / st["stable_labelled"], 2),
                                  pass_stable=None if not st["events"] else round(st["stable"] / st["events"], 2),
                                  gap_left=None if not gaps[lab] else round(statistics.mean(gaps[lab]), 2),
                                  caught_up=st["caught_up"])
                        rec[lab] = s_
                    pool = rows["OOS1"] + rows["OOS2"]
                    rec["OOS"] = summary(pool, days_of("OOS1", H) + days_of("OOS2", H)) if pool else dict(n=0)
                    for k_ in ("events", "stable", "stable_real", "stable_labelled", "real", "labelled"):
                        rec["OOS"][k_] = stat["OOS1"][k_] + stat["OOS2"][k_]
                    rec["OOS"]["p_real_stable"] = None if not rec["OOS"]["stable_labelled"] else round(rec["OOS"]["stable_real"] / rec["OOS"]["stable_labelled"], 2)
                    rec["OOS"]["p_real"] = None if not rec["OOS"]["labelled"] else round(rec["OOS"]["real"] / rec["OOS"]["labelled"], 2)
                    g_all = gaps["OOS1"] + gaps["OOS2"]
                    rec["OOS"]["gap_left"] = None if not g_all else round(statistics.mean(g_all), 2)
                    table.append(rec)
                    store[(M, H, x, lat, tr)] = rows
    out = dict(resolution=resinfo, n_events={str(M): len(events[M]) for M in XM}, table=table)

    def key_main(r):
        return r["lat"] == X_LAT and not r["trades_filter"]

    print("\n## x grid (streaming latency 2 s, mids only). Cells: n | mean (t) | ex-top-3 | S/day ; P(real|stable) ; gap left\n")
    print("| M | H | x s | IS events | IS P(real) | IS P(real given stable) | IS pass | IS n | IS mean (t) | IS S/day | OOS events | OOS P(real) | OOS P(real given stable) | OOS gap left | OOS n | **OOS mean (t)** | **OOS ex-top-3 (t)** | OOS S/day | OOS1 mean (n) | OOS2 mean (n) |")
    print("|" + "---|" * 20)
    for r in table:
        if not key_main(r):
            continue
        I, O = r["IS"], r["OOS"]

        def mt(s):
            if not s.get("n"):
                return "-"
            return f"{s['mean']:+.2f}" + ("" if s.get("t") is None else f" ({s['t']:+.1f})")
        e3 = "-" if O.get("ex3_mean") is None else f"{O['ex3_mean']:+.2f}" + ("" if O.get("ex3_t") is None else f" ({O['ex3_t']:+.1f})")
        print(f"| {r['M']} | {r['H']} | {r['x']} | {I['events']} | {I['p_real']} | {I['p_real_stable']} | {I['pass_stable']} | {I.get('n', 0)} | {mt(I)} | {I.get('s_day', '-')} | "
              f"{O['events']} | {O['p_real']} | {O['p_real_stable']} | {O['gap_left']} | {O.get('n', 0)} | {mt(O)} | {e3} | {O.get('s_day', '-')} | "
              f"{mt(r['OOS1'])} ({r['OOS1'].get('n', 0)}) | {mt(r['OOS2'])} ({r['OOS2'].get('n', 0)}) |")

    # x chosen per (M, H) on IS S/day; then the OOS of that x. And the reverse fold.
    sel = []
    for M in XM:
        for H in XH:
            rs = [r for r in table if key_main(r) and r["M"] == M and r["H"] == H]
            by_is = max(rs, key=lambda r: (r["IS"].get("s_day") if r["IS"].get("n") else -1e9))
            by_oos = max(rs, key=lambda r: (r["OOS"].get("s_day") if r["OOS"].get("n") else -1e9))
            sel.append(dict(M=M, H=H, x_is=by_is["x"], is_sday=by_is["IS"].get("s_day"), oos_at_x_is=(by_is["OOS"].get("n"), by_is["OOS"].get("mean"), by_is["OOS"].get("t"), by_is["OOS"].get("ex3_mean"), by_is["OOS"].get("ex3_t"), by_is["OOS"].get("s_day")),
                            x_oos=by_oos["x"], oos_sday=by_oos["OOS"].get("s_day"), is_at_x_oos=(by_oos["IS"].get("n"), by_oos["IS"].get("mean"), by_oos["IS"].get("t"), by_oos["IS"].get("s_day"))))
    out["selection"] = sel
    print("\n## x selection (per M, H)")
    for s_ in sel:
        print(s_)
    glob = max((r for r in table if key_main(r)), key=lambda r: (r["IS"].get("s_day") if r["IS"].get("n", 0) >= 10 else -1e9))
    out["global_is_best"] = glob
    print("\nglobal IS-best (n>=10):", {k: glob[k] for k in ("M", "H", "x")}, "IS", glob["IS"], "\nOOS", glob["OOS"])

    print("\n## latency / feed scenarios at the selected x (OOS mean (t), n)")
    lat_tab = []
    for r in table:
        if r["M"] == glob["M"] and r["H"] == glob["H"]:
            lat_tab.append(dict(x=r["x"], lat=r["lat"], trades=r["trades_filter"], IS=(r["IS"].get("n"), r["IS"].get("mean"), r["IS"].get("t")),
                                OOS=(r["OOS"].get("n"), r["OOS"].get("mean"), r["OOS"].get("t"), r["OOS"].get("ex3_mean")),
                                gap_left=r["OOS"].get("gap_left"), p_real_stable=r["OOS"].get("p_real_stable")))
    out["latency_table"] = lat_tab
    for x_ in lat_tab:
        print(x_)
    # by day for the global pick
    rows = store[(glob["M"], glob["H"], glob["x"], X_LAT, False)]
    byd = collections.defaultdict(list)
    for lab in ("IS", "OOS1", "OOS2"):
        for r in rows[lab]:
            byd[r["day"]].append(r["tt"])
    out["global_byday"] = {k: (len(v), round(statistics.mean(v), 2)) for k, v in sorted(byd.items())}
    print("by day", out["global_byday"])
    # real vs noise P&L split (OOS, x=0, M of glob)
    rows0 = store[(glob["M"], glob["H"], 0, X_LAT, False)]
    pool = rows0["OOS1"] + rows0["OOS2"] + rows0["IS"]
    for lab_, flag in (("real", True), ("noise", False)):
        xs = [r["tt"] for r in pool if r["real"] is flag]
        out[f"pnl_{lab_}_x0_all"] = (len(xs), round(statistics.mean(xs), 2) if xs else None)
    print("P&L by label at x=0 (all samples):", out["pnl_real_x0_all"], out["pnl_noise_x0_all"])
    # focus: the x = 0 fast-follow variants (detail for the spec / verdict)
    focus = {}
    for (M, H) in ((1.5, 4), (1.5, 12), (2.0, 4), (2.0, 12), (3.0, 12)):
        rows = store[(M, H, 0, X_LAT, False)]
        pool = rows["OOS1"] + rows["OOS2"]
        byd = collections.defaultdict(list)
        for lab in ("IS", "OOS1", "OOS2"):
            for r in rows[lab]:
                byd[r["day"]].append(r["tt"])
        o = next(r for r in table if key_main(r) and r["M"] == M and r["H"] == H and r["x"] == 0)["OOS"]
        vals = [r["tt"] for r in pool]
        focus[f"M{M} H{H}"] = dict(byday={k: (len(v), round(statistics.mean(v), 2)) for k, v in sorted(byd.items())},
                                   oos={k: o.get(k) for k in ("n", "mean", "t", "ex3_mean", "ex3_t", "top3", "share_adds_d", "r_only_n", "r_only_mean", "r_only_s_day", "s_day", "per_day", "close_exits", "mean_hold_h", "med_qty", "mid_mid")},
                                   sigma=round(statistics.pstdev(vals), 2) if len(vals) > 1 else None,
                                   sizing=sizing(o.get("mean"), statistics.pstdev(vals) if len(vals) > 1 else 2.5))
    out["focus_x0"] = focus
    print("\n## focus x=0")
    for k, v in focus.items():
        print(k, v)
    json.dump(out, open(os.path.join(OUT, "xstudy.json"), "w"), indent=1, default=str)
    curves(events, fine, paths, stable_for, trade, sample_of)


# ----------------------------------------------------------------------------------------------- curves A, B, C against x
CX = (0, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600)
FIG = os.path.join(HERE, "research_notes", "figures", "outside_sweep_x.png")


def wilson(k, n, z=1.96):
    if n == 0:
        return None, None, None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, c - h), min(1.0, c + h)


def curves(events, fine, paths, stable_for, trade, sample_of, H=12, lat=X_LAT, Ms=(1.5, 2.0)):
    """Same events for every x. A: P(real | stable for x). B: gap and touch depth others took by x. C: net EV per share."""
    import random
    con = sqlite3.connect(f"file:{os.path.join(DATA, 'sigbot.sqlite')}?mode=ro", uri=True)
    exs = {e["ex"] for M in Ms for e in events[M]}
    tape = collections.defaultdict(list)
    for ex, px, q, at_, ag in con.execute("SELECT exchange_id, price, quantity, executed_at, aggressor FROM tape"):
        if str(ex) in exs and ag in ("buy", "sell"):
            tape[str(ex)].append((iso(at_), px, abs(q), 1 if ag == "buy" else -1))
    ours = collections.defaultdict(list)
    for ex, px, q, at_ in con.execute("SELECT exchange_id, price, quantity, filled_at FROM fills"):
        ours[str(ex)].append((iso(at_), px, abs(q)))
    for d_ in (tape, ours):
        for k in d_:
            d_[k].sort()
    tape_start = min(v[0][0] for v in tape.values()) if tape else None
    depth = collections.defaultdict(list)
    for ts, ex, bq, aq in con.execute("SELECT ts, exchange_id, bid_qty, ask_qty FROM quotes WHERE bid_qty IS NOT NULL"):
        if str(ex) in exs:
            depth[str(ex)].append((ts, bq, aq))
    for k in depth:
        depth[k].sort()

    def is_ours(ex, t, px, q):
        for (tf, pf, qf) in ours.get(ex, []):
            if abs(tf - t) < 5 and abs(pf - px) < 1e-9 and abs(qf - q) < 1e-6:
                return True
        return False

    def touch_size(ex, t, d):
        v = depth.get(ex)
        if not v:
            return None
        i = bisect.bisect_right([x[0] for x in v], t) - 1
        if i < 0 or t - v[i][0] > 120:
            return None
        return v[i][2] if d > 0 else v[i][1]

    def kalshi_pm_start(e):
        """move start: latest minute in the hour before t_c at which F still held < 25% of the move."""
        f = fine[e["race"]]
        t = e["tc"]
        while t > e["tc"] - 3600:
            a_ = VM_at(f["pm"], t, 600)
            b_ = VM_at(f["k"], t, K_AGE)
            if a_ is not None and b_ is not None and (0.5 * (a_ + b_) - e["F0"]) * e["d"] < 0.25 * e["mv"]:
                return t
            t -= 60
        return e["tc"] - 3600

    per = []          # one row per (event, x)
    for M in Ms:
        for e in events[M]:
            lab = sample_of(e["tc"], H)
            if lab is None or e["tc"] + max(CX) + lat + H * 3600 > F_END:
                continue
            P = paths[e["ex"]]
            q0 = q_at(P, e["tc"])
            if q0 is None:
                continue
            d = e["d"]
            stale_px = q0[2] if d > 0 else q0[1]
            t_start = kalshi_pm_start(e)
            ts_ = [x[0] for x in tape.get(e["ex"], [])]
            touch0 = touch_size(e["ex"], e["tc"], d)
            for x in CX:
                te = e["tc"] + x + lat
                st = stable_for(e, x + lat)
                q = q_at(P, te)
                gap_left = None if q is None else (e["F1"] - 0.5 * (q[1] + q[2])) * d / T
                taken = None
                if tape_start is not None and t_start >= tape_start:
                    lo, hi = bisect.bisect_left(ts_, t_start), bisect.bisect_right(ts_, te)
                    taken = 0.0
                    for (tt_, px, qq, ag) in tape[e["ex"]][lo:hi]:
                        if ag == d and (px - stale_px) * d <= 1e-9 and not is_ours(e["ex"], tt_, px, qq):
                            taken += qq
                r = trade(e, x, H, lat) if st else None
                per.append(dict(M=M, race=e["race"], ex=e["ex"], tc=e["tc"], x=x, sample=lab, real=e["real"], stable=st, g0=e["g0"],
                                gap_left=gap_left, frac_left=None if gap_left is None else max(0.0, gap_left) / e["g0"],
                                taken_sh=taken, touch0=touch0, touch_x=touch_size(e["ex"], te, d),
                                tt=None if (r is None or r.get("skip")) else r["tt"], mm=None if (r is None or r.get("skip")) else r["mm"],
                                skip=None if r is None else r.get("skip")))
    out = {}
    for M in Ms:
        rowsM = [p for p in per if p["M"] == M]
        A, B, C = [], [], []
        for x in CX:
            rx = [p for p in rowsM if p["x"] == x]
            for scope, sel in (("all", rx), ("OOS", [p for p in rx if p["sample"] != "IS"]), ("IS", [p for p in rx if p["sample"] == "IS"])):
                lab_ = [p for p in sel if p["stable"] and p["real"] is not None]
                pr, lo_, hi_ = wilson(sum(p["real"] for p in lab_), len(lab_))
                A.append(dict(x=x, scope=scope, n=len(lab_), p=pr, lo=lo_, hi=hi_,
                              pass_rate=round(sum(p["stable"] for p in sel) / len(sel), 2) if sel else None))
                fl = [p["frac_left"] for p in sel if p["frac_left"] is not None]
                gl = [p["gap_left"] for p in sel if p["gap_left"] is not None]
                tk = [p["taken_sh"] for p in sel if p["taken_sh"] is not None]
                tshare = [min(1.0, p["taken_sh"] / p["touch0"]) for p in sel if p["taken_sh"] is not None and p["touch0"]]
                B.append(dict(x=x, scope=scope, n=len(fl), frac_left=round(statistics.mean(fl), 3) if fl else None,
                              frac_left_se=round(statistics.pstdev(fl) / math.sqrt(len(fl)), 3) if len(fl) > 1 else None,
                              gap_left_ticks=round(statistics.mean(gl), 2) if gl else None,
                              taken_sh_median=statistics.median(tk) if tk else None, taken_sh_mean=round(statistics.mean(tk)) if tk else None,
                              n_tape=len(tk), touch_taken_share=round(statistics.mean(tshare), 3) if tshare else None, n_touch=len(tshare),
                              touch0_median=statistics.median([p["touch0"] for p in sel if p["touch0"]]) if any(p["touch0"] for p in sel) else None))
                tr = [p for p in sel if p["tt"] is not None]
                m_, t_ = clustered(tr, "tt") if tr else (None, None)
                se = None if (m_ is None or t_ in (None, 0)) else abs(m_ / t_)
                C.append(dict(x=x, scope=scope, n=len(tr), ev=None if m_ is None else round(m_, 3), lo=None if se is None else round(m_ - 1.96 * se, 3),
                              hi=None if se is None else round(m_ + 1.96 * se, 3), t=None if t_ is None else round(t_, 2)))
        # model curve: calibrate kappa (share of available gap SIG closes for REAL events), adverse move for NOISE, cost, on IS at x=0
        is0 = [p for p in rowsM if p["x"] == 0 and p["sample"] == "IS" and p["mm"] is not None]
        real_is = [p for p in is0 if p["real"]]
        noise_is = [p for p in is0 if p["real"] is False]
        kappa = statistics.mean([p["mm"] / max(1.0, p["gap_left"]) for p in real_is]) if real_is else None
        adverse = -statistics.mean([p["mm"] for p in noise_is]) if noise_is else None
        cost = statistics.mean([p["mm"] - p["tt"] for p in is0]) if is0 else None
        model = []
        for scope in ("IS", "OOS"):
            for x in CX:
                a_ = next(a for a in A if a["x"] == x and a["scope"] == scope)
                b_ = next(b for b in B if b["x"] == x and b["scope"] == scope)
                if None in (a_["p"], b_["gap_left_ticks"], kappa, adverse, cost):
                    continue
                ev = a_["p"] * kappa * b_["gap_left_ticks"] - (1 - a_["p"]) * adverse - cost
                model.append(dict(x=x, scope=scope, ev_model=round(ev, 3)))
        # optimal x with uncertainty: race-cluster bootstrap of the empirical EV curve (stable trades), OOS and all
        boot = {}
        rnd = random.Random(7)
        for scope in ("OOS", "all"):
            sel = [p for p in rowsM if (scope == "all" or p["sample"] != "IS") and p["tt"] is not None]
            races = sorted({p["race"] for p in sel})
            if not races:
                continue
            byr = collections.defaultdict(list)
            for p in sel:
                byr[p["race"]].append(p)
            picks = []
            for _ in range(2000):
                samp = [p for r_ in (rnd.choice(races) for _ in races) for p in byr[r_]]
                evx = {}
                for x in CX:
                    v = [p["tt"] for p in samp if p["x"] == x]
                    if v:
                        evx[x] = sum(v) / len(v) * len(v)      # total ticks per share summed: EV per event-opportunity
                if evx:
                    picks.append(max(evx, key=evx.get))
            cnt = collections.Counter(picks)
            boot[scope] = {str(k): round(v / len(picks), 3) for k, v in sorted(cnt.items())}
        out[str(M)] = dict(A=A, B=B, C=C, model=model, kappa=None if kappa is None else round(kappa, 3),
                           adverse=None if adverse is None else round(adverse, 2), cost=None if cost is None else round(cost, 2),
                           n_events=len({(p["race"], p["tc"]) for p in rowsM}),
                           n_events_tape=len({(p["race"], p["tc"]) for p in rowsM if p["taken_sh"] is not None}),
                           boot_argmax=boot)
    out["per_event"] = per
    json.dump(out, open(os.path.join(OUT, "curves.json"), "w"), indent=1, default=str)
    for M in Ms:
        o = out[str(M)]
        print(f"\n## curves M={M}: events {o['n_events']} (tape-covered {o['n_events_tape']}); kappa {o['kappa']} adverse {o['adverse']} cost {o['cost']}")
        print("boot argmax (share of bootstrap draws picking each x):", o["boot_argmax"])
        for x in CX:
            a = {s: next(r for r in o["A"] if r["x"] == x and r["scope"] == s) for s in ("all", "OOS")}
            b = {s: next(r for r in o["B"] if r["x"] == x and r["scope"] == s) for s in ("all", "OOS")}
            c = {s: next(r for r in o["C"] if r["x"] == x and r["scope"] == s) for s in ("all", "OOS", "IS")}
            mo = {m["scope"]: m["ev_model"] for m in o["model"] if m["x"] == x}
            print(f"x={x:5d} | A all {a['all']['p'] and round(a['all']['p'],2)} [{a['all']['lo'] and round(a['all']['lo'],2)},{a['all']['hi'] and round(a['all']['hi'],2)}] n{a['all']['n']} pass {a['all']['pass_rate']}"
                  f" | B frac_left {b['all']['frac_left']} gap {b['all']['gap_left_ticks']} taken_med {b['all']['taken_sh_median']} (n{b['all']['n_tape']}) touch_taken {b['all']['touch_taken_share']} (n{b['all']['n_touch']})"
                  f" | C IS {c['IS']['ev']} (n{c['IS']['n']}) OOS {c['OOS']['ev']} [{c['OOS']['lo']},{c['OOS']['hi']}] n{c['OOS']['n']} | model IS {mo.get('IS')} OOS {mo.get('OOS')}")
    try:
        plot_curves(out, Ms)
    except ImportError as ex_:
        print("matplotlib not available in this interpreter; run plot with the venv:", ex_)


def VM_at(series, t, age):
    ts, px = series
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > age:
        return None
    return px[i]


def plot_curves(out, Ms=(1.5, 2.0)):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.style.use("default")
    fig, axes = plt.subplots(3, 1, figsize=(9, 12), sharex=True, facecolor="white")
    pos = lambda x: max(x, 5)                        # x = 0 drawn at 5 s on the log axis
    cols = {1.5: "#1f77b4", 2.0: "#d62728"}
    for M in Ms:
        o = out[str(M)]
        col = cols.get(M, "k")
        # A
        for scope, ls in (("all", "-"), ("OOS", "--")):
            r = [a for a in o["A"] if a["scope"] == scope and a["p"] is not None]
            xs = [pos(a["x"]) for a in r]
            axes[0].plot(xs, [a["p"] for a in r], ls, color=col, marker="o", ms=4, label=f"M {M} pts, {scope} (n {r[0]['n'] if r else 0} at x=0)")
            if scope == "all":
                axes[0].fill_between(xs, [a["lo"] for a in r], [a["hi"] for a in r], color=col, alpha=0.12)
        # B
        r = [b for b in o["B"] if b["scope"] == "all" and b["frac_left"] is not None]
        xs = [pos(b["x"]) for b in r]
        axes[1].plot(xs, [b["frac_left"] for b in r], "-", color=col, marker="o", ms=4, label=f"M {M}: share of gap still available (DMM requotes + takes)")
        axes[1].fill_between(xs, [b["frac_left"] - 1.96 * (b["frac_left_se"] or 0) for b in r], [b["frac_left"] + 1.96 * (b["frac_left_se"] or 0) for b in r], color=col, alpha=0.12)
        rt = [b for b in o["B"] if b["scope"] == "all" and b["touch_taken_share"] is not None]
        if rt:
            axes[1].plot([pos(b["x"]) for b in rt], [1 - b["touch_taken_share"] for b in rt], ":", color=col, marker="s", ms=4,
                         label=f"M {M}: share of stale touch depth not yet taken by others (tape, n {rt[0]['n_touch']})")
        # C
        for scope, ls, mk in (("IS", ":", "^"), ("OOS", "-", "o")):
            r = [c for c in o["C"] if c["scope"] == scope and c["ev"] is not None]
            xs = [pos(c["x"]) for c in r]
            axes[2].plot(xs, [c["ev"] for c in r], ls, color=col, marker=mk, ms=4, label=f"M {M}: realised EV, {scope}")
            if scope == "OOS":
                axes[2].fill_between(xs, [c["lo"] if c["lo"] is not None else c["ev"] for c in r], [c["hi"] if c["hi"] is not None else c["ev"] for c in r], color=col, alpha=0.10)
        mo = [m for m in o["model"] if m["scope"] == "OOS"]
        if mo:
            axes[2].plot([pos(m["x"]) for m in mo], [m["ev_model"] for m in mo], "-.", color=col, alpha=0.7, label=f"M {M}: model EV (IS-calibrated) on OOS P(real), gap")
        # per-event points (OOS, stable trades), jittered
        pts = [p for p in out["per_event"] if p["M"] == M and p["sample"] != "IS" and p["tt"] is not None]
        import random
        rnd = random.Random(1)
        axes[2].scatter([pos(p["x"]) * (1 + 0.08 * (rnd.random() - 0.5)) * (1.06 if M == 2.0 else 0.94) for p in pts], [p["tt"] for p in pts],
                        s=6, color=col, alpha=0.25)
    axes[0].set_ylabel("A: P(move is REAL | stable for x)")
    axes[0].set_ylim(0.4, 1.02)
    axes[0].legend(fontsize=8, loc="lower left")
    axes[0].set_title("Outside move then wait x seconds before sweeping SIG (12-h stop, 2-s latency; x = 0 drawn at 5 s)", fontsize=10)
    axes[1].set_ylabel("B: share of SIG's stale gap / depth left")
    axes[1].set_ylim(0, 1.05)
    axes[1].legend(fontsize=8, loc="lower left")
    axes[2].axhline(0, color="grey", lw=0.8)
    axes[2].set_ylabel("C: net EV per share (ticks, after spread + slip)")
    axes[2].set_ylim(-5, 8)
    axes[2].legend(fontsize=7, loc="upper right")
    axes[2].set_xscale("log")
    axes[2].set_xlabel("stabilisation wait x (seconds, log scale)")
    axes[2].set_xticks([5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600])
    axes[2].set_xticklabels(["0", "15", "30", "60", "120", "300", "600", "1200", "1800", "3600"])
    for a in axes:
        a.grid(True, alpha=0.3)
        a.set_facecolor("white")
    fig.tight_layout()
    os.makedirs(os.path.dirname(FIG), exist_ok=True)
    fig.savefig(FIG, dpi=130, facecolor="white")
    print("saved", FIG)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        fetch()
    elif len(sys.argv) > 1 and sys.argv[1] == "xstudy":
        xstudy()
    else:
        main()
