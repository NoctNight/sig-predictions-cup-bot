"""MM upgrade study (research_notes/mm_upgrade.md). Paper only: data/sigbot.sqlite read-only, no API calls.

    python3 research_notes/mm_upgrade_study.py history     # rebuild the self-tuner's MM runs hourly -> mm_upgrade_data/tuner_history.json
    python3 research_notes/mm_upgrade_study.py candidates  # replay candidate rules out of sample -> mm_upgrade_data/candidates.json
    python3 research_notes/mm_upgrade_study.py plot        # SVG figures -> research_notes/figures/

History: for each run time T (hourly), the window is [first quote, T] (the 48 h / 96 h lookbacks are longer than the data),
fit = older 70%, test = newest 30%, the MM config in force at T (data/changes.md), the shadow tuner's coordinate search,
test-block bootstrap (shadow.bootstrap_confidence) and, added here, (a) a moving-block bootstrap of hourly fit-window
blocks for the sampling distribution of the optimum, (b) a test of whether consecutive runs' optima differ, (c) hours
of data needed to resolve each parameter.
"""
from __future__ import annotations

import copy
import json
import os
import random
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sigbot import shadow as S                       # noqa: E402
from sigbot.config import BotConfig                  # noqa: E402
from sigbot.proposals import mm_upgrade as U         # noqa: E402

OUT = os.path.join(ROOT, "research_notes", "mm_upgrade_data")
FIG = os.path.join(ROOT, "research_notes", "figures")
H = 3600.0


def _t(s: str) -> float:
    return S._ts(s + "+00:00")


# MM config timeline (UTC; data/changes.md is in BST = UTC+1)
TIMELINE = [
    (_t("2026-10-21T00:00:00"), dict(level_ticks=[1, 2, 3, 5], level_size_mult=[0.5, 0.75, 1.0, 1.25], base_qty=100, max_inventory_notional=600,
                                     requote_ticks=0.5, meanrev_max_ticks=1.0, vol_gate_fade_mult=0.0, n_markets=15, capital_frac=0.20, improve_ge=2)),
    (_t("2026-10-22T00:29:00"), dict(meanrev_max_ticks=2.0, vol_gate_fade_mult=0.5, n_markets=25, capital_frac=0.35)),
    (_t("2026-10-22T00:53:00"), dict(requote_ticks=1.0)),
    (_t("2026-10-22T07:34:00"), dict(n_markets=12, capital_frac=0.15)),
    (_t("2026-10-22T10:22:00"), dict(base_qty=200, max_inventory_notional=900)),
    (_t("2026-10-22T10:40:00"), dict(n_markets=40, capital_frac=0.33)),
    (_t("2026-10-22T11:40:00"), dict(level_ticks=[1, 2], level_size_mult=[1.0, 0.5], base_qty=300, improve_ge=3)),
]

LADDERS = {     # joint (level_ticks, level_size_mult) candidates
    "live2 [1,2]x[1,.5]": ([1, 2], [1.0, 0.5]),
    "old4 [1,2,3,5]x[.5,.75,1,1.25]": ([1, 2, 3, 5], [0.5, 0.75, 1.0, 1.25]),
    "touch [1]x[1]": ([1], [1.0]),
    "flat2 [1,2]x[1,1]": ([1, 2], [1.0, 1.0]),
    "wide2 [1,3]x[1,.5]": ([1, 3], [1.0, 0.5]),
    "lit-neutral [1,2,3,5,8]x[1,1,.75,.5,.25]": ([1, 2, 3, 5, 8], [1.0, 1.0, 0.75, 0.5, 0.25]),
    "lit-benign [1,2,3,5]x[1.5,1,.5,.25]": ([1, 2, 3, 5], [1.5, 1.0, 0.5, 0.25]),
}
GRIDS = {
    "requote_ticks": [0.5, 1.0, 1.5, 2.0, 3.0],
    "vol_gate_fade_mult": [0.0, 0.25, 0.5, 0.75, 1.0],
    "meanrev_max_ticks": [0.0, 0.5, 1.0, 1.5, 2.0],
    "ladder": list(LADDERS),
    "base_qty": [100, 200, 300, 400, 600],
    "improve_ge": [2, 3, 4],
}
LITERATURE = {   # reports/Market maker quote sizing by depth.md, mapped onto the bot's knobs
    "requote_ticks": (0.5, "requote on a 0.5-tick reservation move (1 tick on the mid)"),
    "vol_gate_fade_mult": (0.5, "variance regime: all sizes x0.5, h x1.5"),
    "meanrev_max_ticks": (1.0, "fair-value shift clipped at +-h = 1 tick"),
    "ladder": ("lit-neutral [1,2,3,5,8]x[1,1,.75,.5,.25]", "5 levels at h+0,1,2,4,7, flat-to-declining (theta 0)"),
    "base_qty": (None, "S0 = Q_cap / sum(mult): a ceiling from worst-case loss, no point value"),
    "improve_ge": (4, "join while spread <= 2h+1 = 3 ticks, else sit at r -+ h"),
}


def cfg_at(T: float):
    cfg = BotConfig.load()
    d = {}
    for t, ch in TIMELINE:
        if t <= T:
            d.update(ch)
    for k, v in d.items():
        if k != "improve_ge":
            setattr(cfg.mm, k, copy.deepcopy(v))
    return cfg, d


def ladder_name(d: dict) -> str:
    for n, (lt, lm) in LADDERS.items():
        if lt == d["level_ticks"] and lm == d["level_size_mult"]:
            return n
    return "other"


def params_of(d: dict, key: str, val, fill_model: str = "bucket") -> dict:
    p = dict(requote_ticks=d["requote_ticks"], level_ticks=d["level_ticks"], level_size_mult=d["level_size_mult"],
             meanrev_max_ticks=d["meanrev_max_ticks"], vol_gate_fade_mult=d["vol_gate_fade_mult"], base_qty=d["base_qty"],
             max_inventory_notional=d["max_inventory_notional"], improve_ge=d["improve_ge"], fill_model=fill_model)
    if key == "ladder":
        p["level_ticks"], p["level_size_mult"] = LADDERS[val]
    elif key is not None:
        p[key] = val
    return p


def current_of(d: dict, key: str):
    return ladder_name(d) if key == "ladder" else d[key]


def blocks(a: float, b: float, n: int) -> list:
    w = (b - a) / n
    return [(a + i * w, a + (i + 1) * w) for i in range(n)]


def hourly(a: float, b: float) -> list:
    n = max(1, int(round((b - a) / H)))
    return blocks(a, b, n)


def boot_argmax(block_scores: dict, cur, n: int = 1000, seed: int = 11) -> dict:
    """Moving-block bootstrap (blocks = hours) of the fit-window optimum: P(each candidate is the argmax)."""
    keys = list(block_scores)
    m = len(next(iter(block_scores.values())))
    rng = random.Random(seed)
    wins = {k: 0.0 for k in keys}
    for _ in range(n):
        idx = [rng.randrange(m) for _ in range(m)]
        tot = {k: sum(block_scores[k][i] for i in idx) for k in keys}
        top = max(tot.values())
        best = cur if tot[cur] >= top - 1e-9 else max(tot, key=lambda k: tot[k])
        wins[best] += 1
    return {k: v / n for k, v in wins.items()}


def hours_needed(diffs: list, z: float = 1.2816) -> dict:
    """Hourly score differences (candidate - current) -> hours for a one-sided 90% test (z 1.28) and for the
    materiality bar (10 SUSQies) at the observed mean rate."""
    if len(diffs) < 3:
        return {"mean_per_h": None, "sd_per_h": None, "hours_90": None, "hours_material": None}
    mu, sd = statistics.mean(diffs), statistics.pstdev(diffs) or 1e-9
    h90 = (z * sd / abs(mu)) ** 2 if abs(mu) > 1e-9 else float("inf")
    hm = 10.0 / abs(mu) if abs(mu) > 1e-9 else float("inf")
    return {"mean_per_h": mu, "sd_per_h": sd, "hours_90": h90, "hours_material": hm}


def eval_param(md, d, key, fit, test, fill_model="bucket", metric="score"):
    cur = current_of(d, key)
    grid = list(GRIDS[key])
    if cur not in grid:
        grid = [cur] + grid
    sim = lambda v, w: U.simulate_x(md, w[0], w[1], params_of(d, key, v, fill_model))
    fit_full = {str(v): sim(v, fit) for v in grid}
    fs = {k: r[metric] for k, r in fit_full.items()}
    top = max(fs.values())
    ck = str(cur)
    best_k = ck if fs[ck] >= top - 1e-9 else max(fs, key=lambda k: fs[k])
    best = next(v for v in grid if str(v) == best_k)
    tb = blocks(test[0], test[1], S.TEST_BLOCKS)
    rc, rb = sim(cur, test), sim(best, test)
    gain = rb[metric] - rc[metric]
    diffs = [] if best_k == ck else [sim(best, b)[metric] - sim(cur, b)[metric] for b in tb]
    conf = S.bootstrap_confidence(diffs) if diffs else 0.0
    # hourly fit-window blocks for every candidate (sampling distribution of the optimum)
    hb = hourly(fit[0], fit[1])
    bs = {str(v): [sim(v, b)[metric] for b in hb] for v in grid}
    pmax = boot_argmax(bs, ck)
    # hours to resolve: hourly differences over the whole window, fit-best (or, when unchanged, the runner-up) vs current
    alt_k = best_k if best_k != ck else max((k for k in fs if k != ck), key=lambda k: fs[k])
    alt = next(v for v in grid if str(v) == alt_k)
    allh = hourly(fit[0], test[1])
    hd = [sim(alt, b)[metric] - sim(cur, b)[metric] for b in allh]
    return {"current": cur, "best": best, "grid": [str(v) for v in grid], "fit_scores": {k: round(v, 2) for k, v in fs.items()},
            "fit_edge30": {k: round(r["edge30"], 2) for k, r in fit_full.items()}, "fit_fill_qty": {k: r["fill_qty"] for k, r in fit_full.items()},
            "test_current": round(rc[metric], 2), "test_best": round(rb[metric], 2), "oos_gain": round(gain, 2), "block_diffs": [round(x, 2) for x in diffs],
            "confidence": round(conf, 3), "p_argmax": {k: round(v, 3) for k, v in pmax.items()}, "fit_hourly": {k: [round(x, 3) for x in v] for k, v in bs.items()},
            "alt": alt, "hourly_diff_alt": [round(x, 3) for x in hd], "need": hours_needed(hd)}


def tuner_as_coded(md, cfg, fit, test):
    """The shadow tuner's own mm_ladder keys with its grids and hard-coded improve 2, as the bot would run them now."""
    lp = {k.split(".", 1)[1]: v for k, v in S.live_params(cfg).items() if k.startswith("mm.")}
    out = {}
    for key in ("level_ticks", "level_size_mult"):
        try:
            for v in S.GRIDS["mm." + key]:
                S.simulate_mm(md, fit[0], fit[1], dict(lp, **{key: v}))
            out[key] = "ok"
        except Exception as e:
            out[key] = f"{type(e).__name__}: {e}"
    return out


def run_history(step_h: float = 1.0, keys=None):
    os.makedirs(OUT, exist_ok=True)
    cfg0 = BotConfig.load()
    con = S.open_ro(os.path.join(ROOT, cfg0.db_path))
    first = con.execute("SELECT MIN(ts) FROM quotes").fetchone()[0]
    last = con.execute("SELECT MAX(ts) FROM quotes").fetchone()[0]
    keys = keys or list(GRIDS)
    runs = []
    T = _t("2026-10-22T04:00:00")
    times = []
    while T <= last:
        times.append(T); T += step_h * H
    times.append(_t("2026-10-22T10:33:00"))       # the logged scheduled run (validation)
    times.append(last)
    times = sorted(set(times))
    for T in times:
        cfg, d = cfg_at(T)
        t0 = first
        fit_until = t0 + S.FIT_FRAC * (T - t0)
        c0 = time.thread_time()
        md = U.MMDataX(con, t0, T, fit_until, cfg)
        load_cpu = time.thread_time() - c0
        rec = {"T": T, "iso": S._iso(T), "config": d, "fit_until": S._iso(fit_until), "hours": round((T - t0) / H, 2),
               "calib": {"fills": md.calib.get("fills"), "orders": md.calib.get("orders")}, "params": {}}
        if not md.ok:
            rec["skipped"] = True; runs.append(rec); continue
        lp = params_of(d, None, None)
        live_fit = U.simulate_x(md, t0, fit_until, dict(lp, base_qty=md.base_qty_fit))
        rec["calib"].update(replay_fill=live_fit["fill_qty"], actual_fill=round(sum(float(f["q"]) for f in md.fills)))
        c1 = time.thread_time()
        # the tuner's own run cost: its 3 + 2 keys on the shadow model, timed separately
        ct = time.thread_time()
        tuner_cpu = 0.0
        for key in ("requote_ticks", "vol_gate_fade_mult", "meanrev_max_ticks"):
            for v in GRIDS[key]:
                U.simulate_x(md, t0, fit_until, params_of(d, key, v))
            U.simulate_x(md, fit_until, T, params_of(d, key, d[key]))
            for b in blocks(fit_until, T, S.TEST_BLOCKS):
                U.simulate_x(md, b[0], b[1], params_of(d, key, d[key]))
                U.simulate_x(md, b[0], b[1], params_of(d, key, d[key]))
        tuner_cpu = time.thread_time() - ct
        rec["cpu"] = {"load": round(load_cpu, 2), "tuner_quote_group_est": round(tuner_cpu + load_cpu, 2)}
        rec["tuner_as_coded"] = tuner_as_coded(md, cfg, (t0, fit_until), (fit_until, T))
        for key in keys:
            rec["params"][key] = eval_param(md, d, key, (t0, fit_until), (fit_until, T))
        rec["cpu"]["study_all_keys"] = round(time.thread_time() - c1, 2)
        runs.append(rec)
        print(rec["iso"], "fills", rec["calib"], "cpu", rec["cpu"], {k: (v["best"], v["oos_gain"], v["confidence"]) for k, v in rec["params"].items()},
              flush=True)
        json.dump(runs, open(os.path.join(OUT, "tuner_history.json"), "w"), indent=0, default=str)
    con.close()
    return runs


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "history"
    if cmd == "history":
        run_history()


# ================================================================================================= empirical slices
def _series(con, t0=0.0):
    """Per-exchange snapshot series from our quotes table (all quoted markets) and SIG mids from venue_quotes (all legs)."""
    q = {}
    for r in con.execute("SELECT ts, exchange_id, best_bid, best_ask, bid_qty, ask_qty, mid, microprice, fair, pressure FROM quotes "
                         "WHERE mid IS NOT NULL AND ts>=? ORDER BY ts", (t0,)):
        q.setdefault(r["exchange_id"], []).append(tuple(r))
    xv = {}
    for r in con.execute("SELECT ts, exchange_id, fair, sig_bid, sig_ask, sig_mid FROM venue_fair WHERE fair IS NOT NULL AND exchange_id IS NOT NULL ORDER BY ts"):
        xv.setdefault(r["exchange_id"], []).append(tuple(r))
    qt = {k: [x[0] for x in v] for k, v in q.items()}
    xt = {k: [x[0] for x in v] for k, v in xv.items()}
    return q, qt, xv, xt


def _asof(times, rows, ts, max_age):
    import bisect as _b
    i = _b.bisect_right(times, ts) - 1
    if i < 0 or ts - times[i] > max_age:
        return None
    return rows[i]


def _after(times, rows, ts, max_gap=900.0):
    import bisect as _b
    i = _b.bisect_left(times, ts)
    if i >= len(times) or times[i] - ts > max_gap:
        return None
    return rows[i]


def fill_table(con):
    """Every MM fill with its state at the fill and 5 / 30 / 60-minute markouts in ticks (mid from our snapshots)."""
    q, qt, xv, xt = _series(con)
    out = []
    for f in con.execute("SELECT f.exchange_id ex, f.side, f.price, ABS(f.quantity) qty, f.filled_at, f.level, f.purpose, "
                         "o.placed_at FROM fills f LEFT JOIN orders o ON o.order_id=f.order_id WHERE f.purpose IN ('mm','inside')"):
        ex = f["ex"]
        if ex not in q and ex in xv:          # inside-quoter markets are not in our quotes table: use venue_fair SIG polls
            q[ex] = [(x[0], None, x[3], x[4], 0, 0, x[5], None, None, None) for x in xv[ex] if x[3] is not None and x[4] is not None]
            qt[ex] = [x[0] for x in q[ex]]
        if ex not in q:
            continue
        ts = S._ts(f["filled_at"])
        yes_px = f["price"]                    # fills are stored in YES terms (orders store the price sent)
        sgn = 1.0 if f["side"] == "yes" else -1.0          # +1 we bought YES
        s0 = _asof(qt[ex], q[ex], ts, 300.0)
        if s0 is None:
            continue
        mid0, bb, ba, bq, aq = s0[6], s0[2], s0[3], s0[4], s0[5]
        mo = {}
        for h in (300, 1800, 3600):
            s = _after(qt[ex], q[ex], ts + h)
            mo[h] = None if s is None else sgn * (s[6] - yes_px) / S.TICK
        sp_place = None
        if f["placed_at"]:
            sp = _asof(qt[ex], q[ex], f["placed_at"], 300.0)
            sp_place = None if sp is None else U.ticks(sp[3]) - U.ticks(sp[2])
        x0 = _asof(xt.get(ex, []), xv.get(ex, []), ts, 600.0)
        x10 = _asof(xt.get(ex, []), xv.get(ex, []), ts - 600, 900.0)
        m10 = _asof(qt[ex], q[ex], ts - 600, 600.0)
        xdev = None if x0 is None else (x0[2] - mid0) / S.TICK                        # outside fair minus SIG mid, ticks
        xmove = None if (x0 is None or x10 is None) else (x0[2] - x10[2]) / S.TICK     # outside move over 10 min
        smove = None if m10 is None else (mid0 - m10[6]) / S.TICK
        mr = (s0[8] - s0[7] - (s0[9] or 0.0) * S.MM_PRESSURE_SKEW_TICKS * S.TICK) / S.TICK if s0[8] is not None and s0[7] is not None else None
        out.append(dict(ts=ts, ex=ex, purpose=f["purpose"], side=f["side"], sgn=sgn, px=yes_px, qty=float(f["qty"]), level=f["level"], mid=mid0,
                        spread=U.ticks(ba) - U.ticks(bb), spread_place=sp_place, touch_q=(bq if f["side"] == "yes" else aq),
                        conc=sgn * (mid0 - yes_px) / S.TICK, mo5=mo[300], mo30=mo[1800], mo60=mo[3600],
                        xdev_signed=None if xdev is None else sgn * xdev, xmove_signed=None if xmove is None else sgn * xmove,
                        smove_signed=None if smove is None else sgn * smove, mr_signed=None if mr is None else sgn * mr))
    return out


def tape_table(con, t0: float):
    """Public prints since t0 seen from the PASSIVE side (maker bought if the aggressor sold), with the SIG state from
    venue_fair polls (all legs, ~2 min) and markouts from the next polls."""
    q, qt, xv, xt = _series(con)
    out = []
    lo = S._iso(t0).replace(" ", "T")
    for r in con.execute("SELECT exchange_id ex, price, quantity, executed_at, aggressor FROM tape WHERE executed_at>=? AND aggressor IN ('buy','sell')", (lo,)):
        ex = r["ex"]
        if ex not in xv:
            continue
        ts = S._ts(r["executed_at"])
        sgn = 1.0 if r["aggressor"] == "sell" else -1.0     # maker bought YES when the taker sold
        x0 = _asof(xt[ex], xv[ex], ts, 300.0)
        if x0 is None or x0[3] is None or x0[4] is None:
            continue
        mid0 = x0[5]
        mo = {}
        for h in (300, 1800):
            s = _after(xt[ex], xv[ex], ts + h, 600.0)
            mo[h] = None if s is None else sgn * (s[5] - r["price"]) / S.TICK
        x10 = _asof(xt[ex], xv[ex], ts - 600, 600.0)
        out.append(dict(ts=ts, ex=ex, sgn=sgn, px=r["price"], qty=abs(float(r["quantity"])), mid=mid0, spread=U.ticks(x0[4]) - U.ticks(x0[3]),
                        conc=sgn * (mid0 - r["price"]) / S.TICK, mo5=mo[300], mo30=mo[1800], xdev_signed=sgn * (x0[2] - mid0) / S.TICK,
                        xmove_signed=None if x10 is None else sgn * (x0[2] - x10[2]) / S.TICK,
                        smove_signed=None if x10 is None else sgn * (mid0 - x10[5]) / S.TICK))
    return out


def wmean(rows, key, w="qty"):
    v = [(r[key], r[w]) for r in rows if r.get(key) is not None]
    sw = sum(b for _, b in v)
    return (sum(a * b for a, b in v) / sw if sw > 0 else None), len(v), sw


def block_ci(rows, key, w="qty", block=3600.0, n=2000, seed=5):
    """Quantity-weighted mean with a 90% CI from a bootstrap over hour blocks (fills cluster in sweeps)."""
    v = [r for r in rows if r.get(key) is not None]
    if len(v) < 5:
        return None
    by = {}
    for r in v:
        by.setdefault(int(r["ts"] // block), []).append((r[key] * r[w], r[w]))
    bl = [(sum(a for a, _ in x), sum(b for _, b in x)) for x in by.values()]
    rng = random.Random(seed)
    ms = []
    for _ in range(n):
        s = [bl[rng.randrange(len(bl))] for _ in bl]
        tw = sum(b for _, b in s)
        if tw > 0:
            ms.append(sum(a for a, _ in s) / tw)
    ms.sort()
    m = sum(a for a, _ in bl) / sum(b for _, b in bl)
    return dict(mean=round(m, 3), lo=round(ms[int(0.05 * len(ms))], 3), hi=round(ms[int(0.95 * len(ms))], 3), n=len(v),
                shares=round(sum(r[w] for r in v)), blocks=len(bl))


def slice_report(rows, conds: dict, keys=("mo5", "mo30")) -> dict:
    out = {}
    for name, fn in conds.items():
        sub = [r for r in rows if fn(r)]
        out[name] = {k: block_ci(sub, k) for k in keys}
    return out


def run_slices():
    cfg0 = BotConfig.load()
    con = S.open_ro(os.path.join(ROOT, cfg0.db_path))
    fixes = _t("2026-10-22T11:40:00")
    ft = fill_table(con)
    mm = [r for r in ft if r["purpose"] == "mm"]
    conds = {
        "all mm": lambda r: True,
        "after 11:40 fixes": lambda r: r["ts"] >= fixes,
        "spread 1 at fill": lambda r: r["spread"] == 1,
        "spread 2 at fill": lambda r: r["spread"] == 2,
        "spread 3+ at fill": lambda r: r["spread"] >= 3,
        "level 1": lambda r: r["level"] == 1,
        "level 2+": lambda r: (r["level"] or 1) >= 2,
        "touch queue >= 1500": lambda r: (r["touch_q"] or 0) >= 1500,
        "touch queue < 1500": lambda r: (r["touch_q"] or 0) < 1500,
        "outside fair with us >= 1 tick": lambda r: r["xdev_signed"] is not None and r["xdev_signed"] >= 1,
        "outside fair against us >= 1 tick": lambda r: r["xdev_signed"] is not None and r["xdev_signed"] <= -1,
        "outside fair within 1 tick": lambda r: r["xdev_signed"] is not None and abs(r["xdev_signed"]) < 1,
        "outside moved against us >= 2 ticks / 10 min": lambda r: r["xmove_signed"] is not None and r["xmove_signed"] <= -2,
        "outside moved with us >= 2 ticks / 10 min": lambda r: r["xmove_signed"] is not None and r["xmove_signed"] >= 2,
        "SIG moved against us >= 2 ticks / 10 min": lambda r: r["smove_signed"] is not None and r["smove_signed"] <= -2,
        "meanrev with us (> 0.25 tick)": lambda r: r["mr_signed"] is not None and r["mr_signed"] > 0.25,
        "meanrev against us (< -0.25 tick)": lambda r: r["mr_signed"] is not None and r["mr_signed"] < -0.25,
        "mid 0.12-0.25 or 0.75-0.88": lambda r: r["mid"] < 0.25 or r["mid"] > 0.75,
        "mid 0.25-0.75": lambda r: 0.25 <= r["mid"] <= 0.75,
    }
    res = {"mm_fills": slice_report(mm, conds), "inside_fills": slice_report([r for r in ft if r["purpose"] == "inside"], {"all inside": lambda r: True})}
    tp = tape_table(con, _t("2026-10-22T00:00:00"))
    tconds = {
        "all prints (maker side)": lambda r: True,
        "spread 1": lambda r: r["spread"] == 1, "spread 2": lambda r: r["spread"] == 2, "spread 3+": lambda r: r["spread"] >= 3,
        "outside fair with maker >= 2 ticks": lambda r: r["xdev_signed"] >= 2,
        "outside fair against maker >= 2 ticks": lambda r: r["xdev_signed"] <= -2,
        "outside within 2 ticks": lambda r: abs(r["xdev_signed"]) < 2,
        "outside moved against maker >= 2 ticks / 10 min, SIG lagging": lambda r: r["xmove_signed"] is not None and r["xmove_signed"] <= -2 and r["smove_signed"] is not None and abs(r["smove_signed"]) < 0.5 * abs(r["xmove_signed"]),
        "outside moved with maker >= 2 ticks / 10 min": lambda r: r["xmove_signed"] is not None and r["xmove_signed"] >= 2,
        "size <= 50": lambda r: r["qty"] <= 50, "size 51-999": lambda r: 50 < r["qty"] < 1000, "size >= 1000": lambda r: r["qty"] >= 1000,
        "mid < 0.12 or > 0.88": lambda r: r["mid"] < 0.12 or r["mid"] > 0.88,
        "mid 0.12-0.25 or 0.75-0.88": lambda r: 0.12 <= r["mid"] < 0.25 or 0.75 < r["mid"] <= 0.88,
        "mid 0.25-0.75": lambda r: 0.25 <= r["mid"] <= 0.75,
    }
    res["tape_maker"] = slice_report(tp, tconds)
    res["n"] = {"mm_fills": len(mm), "tape_prints": len(tp)}
    json.dump(res, open(os.path.join(OUT, "slices.json"), "w"), indent=1)
    json.dump({"mm": mm}, open(os.path.join(OUT, "fill_table.json"), "w"))
    for grp, d in res.items():
        if grp == "n":
            continue
        print("==", grp)
        for k, v in d.items():
            print(f"  {k:62s}", "  ".join(f"{h}: {x['mean']:+.2f} [{x['lo']:+.2f},{x['hi']:+.2f}] n={x['n']} sh={x['shares']}" if x else f"{h}: -" for h, x in v.items()))
    print(res["n"])


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "slices":
    os.makedirs(OUT, exist_ok=True)
    run_slices()


# ================================================================================================= candidate rules
CANDIDATES = {   # family -> {name: extra params}; every family also contains the live baseline (empty dict)
    "xv_pull": {"pull L1 vs outside fair >= 1 tick": dict(xv_pull=1.0), ">= 2 ticks": dict(xv_pull=2.0), ">= 3 ticks": dict(xv_pull=3.0)},
    "xv_fair": {"reserve += 0.5 x outside gap (cap 2)": dict(xv_w=0.5, xv_cap=2.0), "1.0 x gap (cap 2)": dict(xv_w=1.0, xv_cap=2.0),
                "1.0 x gap (cap 4)": dict(xv_w=1.0, xv_cap=4.0)},
    "xv_lead": {"pull side on outside move >= 2 ticks / 10 min": dict(xv_lead=(2.0, 600.0)), ">= 3 ticks / 10 min": dict(xv_lead=(3.0, 600.0)),
                ">= 2 ticks / 5 min": dict(xv_lead=(2.0, 300.0))},
    "dmm_queue": {"no L1 on 1-tick spreads": dict(dmm="wide_only"), "no quotes on 1-tick spreads": dict(dmm="wide_all"),
                  "no L1 behind >= 1500 at touch": dict(dmm=("queue_max", 1500)), "no L1 behind >= 3000": dict(dmm=("queue_max", 3000))},
    "meanrev": {"meanrev as size skew (k 0.5)": dict(mr_mode="size", mr_size_k=0.5), "size skew (k 1.0)": dict(mr_mode="size", mr_size_k=1.0),
                "no meanrev": dict(mr_mode="none")},
    "improve": {"improve on 2+": dict(improve_ge=2), "improve on 4+": dict(improve_ge=4)},
    "band": {"quote mid 0.20-0.80 only": dict(band=(0.20, 0.80)), "0.25-0.75 only": dict(band=(0.25, 0.75))},
    "combo": {"xv_pull 1 + xv_lead 2/600": dict(xv_pull=1.0, xv_lead=(2.0, 600.0)), "xv_pull 2 + no meanrev": dict(xv_pull=2.0, mr_mode="none"),
              "xv_fair 1.0 cap 2 + xv_lead 2/600": dict(xv_w=1.0, xv_cap=2.0, xv_lead=(2.0, 600.0))},
}


def boot_ci(diffs: list, n: int = 4000, seed: int = 3) -> tuple:
    rng = random.Random(seed)
    k = len(diffs)
    ms = sorted(sum(diffs[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return ms[int(0.05 * n)], ms[int(0.95 * n)]


def run_candidates(fit_frac: float = S.FIT_FRAC, fill_models=("bucket", "spread"), t_end: float = None, tag: str = ""):
    """Walk-forward per family: pick the fit-window best candidate on edge30 (markout P&L at 30 min, the quantity that
    adverse selection moves) and on the tuner score; report the test-window gain with the tuner's 8-block bootstrap
    confidence, an hour-block bootstrap 90% CI of the gain, per 1,000 filled shares and per day."""
    cfg0 = BotConfig.load()
    con = S.open_ro(os.path.join(ROOT, cfg0.db_path))
    t0 = con.execute("SELECT MIN(ts) FROM quotes").fetchone()[0]
    t1 = t_end or con.execute("SELECT MAX(ts) FROM quotes").fetchone()[0]
    fit_until = t0 + fit_frac * (t1 - t0)
    cfg, d = cfg_at(t1)
    md = U.MMDataX(con, t0, t1, fit_until, cfg)
    res = {"window": {"from": S._iso(t0), "fit_until": S._iso(fit_until), "to": S._iso(t1)}, "calib_spread": md.calib.get("participation_by_spread"),
           "families": {}}
    tb = blocks(fit_until, t1, S.TEST_BLOCKS)
    th = hourly(fit_until, t1)
    test_days = (t1 - fit_until) / 86400.0
    for fm in fill_models:
        base = params_of(d, None, None, fm)
        sim = lambda extra, w: U.simulate_x(md, w[0], w[1], dict(base, **extra))
        rb_fit, rb_test = sim({}, (t0, fit_until)), sim({}, (fit_until, t1))
        res.setdefault("baseline", {})[fm] = {"fit": {k: rb_fit[k] for k in ("edge5", "edge30", "edge180", "score", "fill_qty", "pnl")},
                                              "test": {k: rb_test[k] for k in ("edge5", "edge30", "edge180", "score", "fill_qty", "pnl")}}
        for fam, cands in CANDIDATES.items():
            rows = {}
            for name, extra in cands.items():
                rf, rt = sim(extra, (t0, fit_until)), sim(extra, (fit_until, t1))
                rows[name] = {"fit_edge30": rf["edge30"], "fit_edge180": rf["edge180"], "test_edge180": rt["edge180"], "fit_score": rf["score"], "fit_fill": rf["fill_qty"], "test_edge30": rt["edge30"],
                              "test_edge5": rt["edge5"], "test_score": rt["score"], "test_fill": rt["fill_qty"], "test_dd": rt["drawdown"]}
            out = {"candidates": rows}
            for metric, fk, tk in (("edge30", "fit_edge30", "test_edge30"), ("edge180", "fit_edge180", "test_edge180"), ("score", "fit_score", "test_score")):
                bfit = rb_fit[metric]
                best = max(rows, key=lambda n: rows[n][fk])
                if rows[best][fk] <= bfit + 1e-9:
                    out[metric] = {"pick": "baseline", "reason": "no candidate beats the live rule on the fit window"}
                    continue
                extra = cands[best]
                gain = rows[best][tk] - rb_test[metric]
                bd = [sim(extra, b)[metric] - sim({}, b)[metric] for b in tb]
                hd = [sim(extra, b)[metric] - sim({}, b)[metric] for b in th]
                lo, hi = boot_ci(hd) if len(hd) >= 3 else (None, None)
                fills = max(1.0, rb_test["fill_qty"])
                material = max(10.0, 50.0 * test_days, 0.10 * abs(rb_test[metric]))
                out[metric] = {"pick": best, "fit_gain": round(rows[best][fk] - bfit, 2), "oos_gain": round(gain, 2),
                               "conf_8block": S.bootstrap_confidence(bd), "block_diffs": [round(x, 2) for x in bd],
                               "hour_ci90_per_h": [None if lo is None else round(lo, 3), None if hi is None else round(hi, 3)],
                               "hour_mean_per_h": round(statistics.mean(hd), 3) if hd else None,
                               "per_1k_filled_shares": round(1000 * gain / fills, 2), "per_day": round(gain / max(1e-6, test_days), 1),
                               "test_fill_change": rows[best]["test_fill"] - rb_test["fill_qty"], "material": round(material, 1),
                               "clears_tuner_bar": bool(gain >= material and S.bootstrap_confidence(bd) >= 0.9)}
            res["families"].setdefault(fam, {})[fm] = out
            print(fm, fam, {m: (out[m].get("pick"), out[m].get("oos_gain"), out[m].get("conf_8block"), out[m].get("hour_ci90_per_h")) for m in ("edge30", "edge180", "score")},
                  flush=True)
    res["test_days"] = test_days
    json.dump(res, open(os.path.join(OUT, f"candidates{tag}.json"), "w"), indent=1, default=str)
    return res


def realised_counterfactual():
    """No fill model: remove the ACTUAL fills a rule would have prevented (state at the fill) and re-sum the realised
    30-minute markout P&L. Fit/test split as above; hour-block bootstrap CI on the test window."""
    rows = json.load(open(os.path.join(OUT, "fill_table.json")))["mm"]
    rows.sort(key=lambda r: r["ts"])
    t0, t1 = rows[0]["ts"], rows[-1]["ts"]
    fu = t0 + S.FIT_FRAC * (t1 - t0)
    rules = {
        "xv_pull >= 1": lambda r: r["xdev_signed"] is not None and r["xdev_signed"] <= -1,
        "xv_pull >= 2": lambda r: r["xdev_signed"] is not None and r["xdev_signed"] <= -2,
        "xv_pull >= 3": lambda r: r["xdev_signed"] is not None and r["xdev_signed"] <= -3,
        "xv_lead 2/600": lambda r: r["xmove_signed"] is not None and r["xmove_signed"] <= -2,
        "no L1 behind < 1500 (thin touch)": lambda r: r["level"] == 1 and (r["touch_q"] or 0) < 1500,
        "no L1 behind >= 1500 (DMM touch)": lambda r: r["level"] == 1 and (r["touch_q"] or 0) >= 1500,
        "no meanrev-aided fills (mr with us > 0.25)": lambda r: r["mr_signed"] is not None and r["mr_signed"] > 0.25,
        "band 0.25-0.75": lambda r: not (0.25 <= r["mid"] <= 0.75),
    }
    out = {}
    for name, drop in rules.items():
        res = {}
        for h in ("mo5", "mo30"):
            for part, (a, b) in (("fit", (t0, fu)), ("test", (fu, t1 + 1))):
                sub = [r for r in rows if a <= r["ts"] < b and r[h] is not None]
                dropped = [r for r in sub if drop(r)]
                gain = -sum(r[h] * r["qty"] * S.TICK for r in dropped)      # SUSQies
                byh = {}
                for r in dropped:
                    byh[int(r["ts"] // H)] = byh.get(int(r["ts"] // H), 0.0) - r[h] * r["qty"] * S.TICK
                hrs = [byh.get(k, 0.0) for k in range(int(a // H), int(b // H) + 1)]
                lo, hi = boot_ci(hrs) if len(hrs) >= 3 else (None, None)
                res[f"{part}_{h}"] = {"gain": round(gain, 2), "dropped_fills": len(dropped), "dropped_shares": round(sum(r["qty"] for r in dropped)),
                                      "kept_shares": round(sum(r["qty"] for r in sub) - sum(r["qty"] for r in dropped)),
                                      "per_1k_shares": round(1000 * gain / max(1.0, sum(r["qty"] for r in sub)), 2),
                                      "hour_ci90_per_h": [lo and round(lo, 3), hi and round(hi, 3)], "hours": len(hrs)}
        out[name] = res
        print(f"{name:44s}", {k: (v['gain'], v['dropped_fills'], v['per_1k_shares'], v['hour_ci90_per_h']) for k, v in res.items() if k.endswith('mo30')})
    json.dump(out, open(os.path.join(OUT, "realised_counterfactual.json"), "w"), indent=1)
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "candidates":
    run_candidates()
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "counterfactual":
    realised_counterfactual()


def flow_slice():
    """Bartlett-O'Hara style toxicity: share of the last 10 minutes' public volume on the market that traded INTO our side
    (taker sells when we bid), excluding our own fills' prints is not possible from the tape, so they are included."""
    import bisect as _b
    cfg0 = BotConfig.load()
    con = S.open_ro(os.path.join(ROOT, cfg0.db_path))
    tape = {}
    for r in con.execute("SELECT exchange_id ex, quantity, executed_at, aggressor FROM tape WHERE aggressor IN ('buy','sell') ORDER BY executed_at"):
        tape.setdefault(r["ex"], []).append((S._ts(r["executed_at"]), abs(float(r["quantity"])), 1.0 if r["aggressor"] == "buy" else -1.0))
    rows = json.load(open(os.path.join(OUT, "fill_table.json")))["mm"]
    for r in rows:
        tp = tape.get(r["ex"], [])
        tt = [x[0] for x in tp]
        i, j = _b.bisect_left(tt, r["ts"] - 600), _b.bisect_left(tt, r["ts"] - 1)
        win = tp[i:j]
        tot = sum(x[1] for x in win)
        # flow into our side: taker sells (-1) hit our bid (sgn +1)
        into = sum(x[1] for x in win if x[2] == -r["sgn"])
        r["into_share"] = into / tot if tot > 0 else None
        r["flow_vol"] = tot
    conds = {"into-our-side share >= 0.8 (one-sided flow)": lambda r: r["into_share"] is not None and r["into_share"] >= 0.8,
             "share 0.5-0.8": lambda r: r["into_share"] is not None and 0.5 <= r["into_share"] < 0.8,
             "share < 0.5": lambda r: r["into_share"] is not None and r["into_share"] < 0.5,
             "no prior flow in 10 min": lambda r: r["into_share"] is None}
    res = slice_report(rows, conds)
    for k, v in res.items():
        print(f"  {k:50s}", "  ".join(f"{h}: {x['mean']:+.2f} [{x['lo']:+.2f},{x['hi']:+.2f}] n={x['n']}" if x else f"{h}: -" for h, x in v.items()))
    d = json.load(open(os.path.join(OUT, "slices.json")))
    d["mm_fills_flow"] = res
    json.dump(d, open(os.path.join(OUT, "slices.json"), "w"), indent=1)


def market_cluster_ci(key="mo30", rule=None, n=4000, seed=9):
    """Bootstrap over MARKETS (not hours) of the realised markout P&L a rule would remove: checks concentration."""
    rows = [r for r in json.load(open(os.path.join(OUT, "fill_table.json")))["mm"] if r[key] is not None]
    by = {}
    for r in rows:
        if rule(r):
            by[r["ex"]] = by.get(r["ex"], 0.0) - r[key] * r["qty"] * S.TICK
    allex = sorted({r["ex"] for r in rows})
    vals = [by.get(e, 0.0) for e in allex]
    rng = random.Random(seed)
    ms = sorted(sum(vals[rng.randrange(len(vals))] for _ in vals) for _ in range(n))
    return {"total": round(sum(vals), 2), "markets": len(allex), "markets_hit": len(by), "ci90": [round(ms[int(.05 * n)], 2), round(ms[int(.95 * n)], 2)],
            "top3": sorted(((round(v, 1), e) for e, v in by.items()), reverse=True)[:3]}


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "flow":
    flow_slice()
    for name, fn in {"xv_pull>=1": lambda r: r["xdev_signed"] is not None and r["xdev_signed"] <= -1,
                     "xv_pull>=2": lambda r: r["xdev_signed"] is not None and r["xdev_signed"] <= -2,
                     "xv_pull>=3": lambda r: r["xdev_signed"] is not None and r["xdev_signed"] <= -3,
                     "meanrev-aided": lambda r: r["mr_signed"] is not None and r["mr_signed"] > 0.25,
                     "thin touch L1": lambda r: r["level"] == 1 and (r["touch_q"] or 0) < 1500}.items():
        print(name, "mo30", market_cluster_ci("mo30", fn), "mo5", market_cluster_ci("mo5", fn))


RULES = {
    "xv_pull>=2": lambda r: r["xdev_signed"] is not None and r["xdev_signed"] <= -2,
    "meanrev-aided (mr with us > 0.25)": lambda r: r["mr_signed"] is not None and r["mr_signed"] > 0.25,
    "thin touch L1 (< 1500)": lambda r: r["level"] == 1 and (r["touch_q"] or 0) < 1500,
    "one-sided flow into us >= 0.8": lambda r: r.get("into_share") is not None and r["into_share"] >= 0.8,
    "band 0.25-0.75": lambda r: not (0.25 <= r["mid"] <= 0.75),
}


def robustness():
    """Per rule: realised P&L removed (30 / 5 min marks), fit vs test halves, market-cluster CI, and leave-out of the two
    largest-contributing markets."""
    import bisect as _b
    flow_rows = json.load(open(os.path.join(OUT, "fill_table.json")))["mm"]
    # recompute into_share (flow_slice does not persist it)
    cfg0 = BotConfig.load()
    con = S.open_ro(os.path.join(ROOT, cfg0.db_path))
    tape = {}
    for r in con.execute("SELECT exchange_id ex, quantity, executed_at, aggressor FROM tape WHERE aggressor IN ('buy','sell') ORDER BY executed_at"):
        tape.setdefault(r["ex"], []).append((S._ts(r["executed_at"]), abs(float(r["quantity"])), 1.0 if r["aggressor"] == "buy" else -1.0))
    for r in flow_rows:
        tp = tape.get(r["ex"], []); tt = [x[0] for x in tp]
        win = tp[_b.bisect_left(tt, r["ts"] - 600):_b.bisect_left(tt, r["ts"] - 1)]
        tot = sum(x[1] for x in win)
        r["into_share"] = sum(x[1] for x in win if x[2] == -r["sgn"]) / tot if tot > 0 else None
    rows = sorted(flow_rows, key=lambda r: r["ts"])
    t0, t1 = rows[0]["ts"], rows[-1]["ts"]; fu = t0 + S.FIT_FRAC * (t1 - t0)
    total = {h: -sum(r[h] * r["qty"] * S.TICK for r in rows if r[h] is not None) for h in ("mo5", "mo30")}
    out = {"baseline_realised": {"mo5_pnl": round(-total["mo5"], 2), "mo30_pnl": round(-total["mo30"], 2),
                                 "shares": round(sum(r["qty"] for r in rows)), "fills": len(rows), "hours": round((t1 - t0) / H, 1)}}
    for name, fn in RULES.items():
        res = {}
        for h in ("mo5", "mo30"):
            v = [r for r in rows if r[h] is not None]
            g = lambda sub: -sum(r[h] * r["qty"] * S.TICK for r in sub if fn(r))
            by = {}
            for r in v:
                if fn(r):
                    by[r["ex"]] = by.get(r["ex"], 0.0) - r[h] * r["qty"] * S.TICK
            top2 = [e for _, e in sorted(((x, e) for e, x in by.items()), reverse=True)[:2]]
            res[h] = {"fit": round(g([r for r in v if r["ts"] < fu]), 2), "test": round(g([r for r in v if r["ts"] >= fu]), 2),
                      "all": round(g(v), 2), "without_top2_markets": round(g([r for r in v if r["ex"] not in top2]), 2), "top2": top2,
                      "market_ci90": market_cluster_ci(h, fn)["ci90"] if "into_share" not in name else None,
                      "dropped_share_of_volume": round(sum(r["qty"] for r in v if fn(r)) / max(1.0, sum(r["qty"] for r in v)), 2)}
            if "flow" in name:   # market-cluster CI needs the recomputed field
                vals = {}
                for r in v:
                    if fn(r):
                        vals[r["ex"]] = vals.get(r["ex"], 0.0) - r[h] * r["qty"] * S.TICK
                allex = sorted({r["ex"] for r in v}); arr = [vals.get(e, 0.0) for e in allex]
                rng = random.Random(9); ms = sorted(sum(arr[rng.randrange(len(arr))] for _ in arr) for _ in range(4000))
                res[h]["market_ci90"] = [round(ms[200], 2), round(ms[3800], 2)]
        out[name] = res
        print(f"{name:36s}", json.dumps(res))
    json.dump(out, open(os.path.join(OUT, "robustness.json"), "w"), indent=1)
    json.dump({"mm": rows}, open(os.path.join(OUT, "fill_table.json"), "w"))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "robust":
    robustness()


# ================================================================================================= history analysis + figures
def analyze_history():
    runs = [r for r in json.load(open(os.path.join(OUT, "tuner_history.json"))) if not r.get("skipped")]
    out = {"runs": len(runs), "params": {}, "cpu": {}}
    cpu = [r["cpu"]["tuner_quote_group_est"] for r in runs if "cpu" in r]
    out["cpu"] = {"quote_group_est_sec": [round(min(cpu), 1), round(statistics.median(cpu), 1), round(max(cpu), 1)],
                  "load_sec_median": round(statistics.median(r["cpu"]["load"] for r in runs), 2),
                  "all_study_keys_sec_median": round(statistics.median(r["cpu"]["study_all_keys"] for r in runs), 1)}
    out["tuner_as_coded_last"] = runs[-1].get("tuner_as_coded")
    for key in GRIDS:
        seq = [(r["iso"], r["params"][key]) for r in runs if key in r["params"]]
        changes, sig_changes, tests = 0, 0, []
        for (ia, a), (ib, b) in zip(seq, seq[1:]):
            if str(a["best"]) == str(b["best"]):
                continue
            changes += 1
            fh = b["fit_hourly"]
            if str(a["best"]) in fh and str(b["best"]) in fh:
                d = [x - y for x, y in zip(fh[str(b["best"])], fh[str(a["best"])])]
                lo, hi = boot_ci(d)
                sig = lo > 0 or hi < 0
                sig_changes += int(sig)
                tests.append({"from_run": ia, "to_run": ib, "old_best": a["best"], "new_best": b["best"], "per_h_ci90": [round(lo, 3), round(hi, 3)], "significant": sig})
        last = seq[-1][1]
        lit = LITERATURE[key][0]
        lit_gap = None
        if lit is not None and str(lit) in last["fit_hourly"] and str(lit) != str(last["current"]):
            d = [x - y for x, y in zip(last["fit_hourly"][str(lit)], last["fit_hourly"][str(last["current"])])]
            lo, hi = boot_ci(d)
            lit_gap = {"fit_score_lit_minus_live": round(last["fit_scores"][str(lit)] - last["fit_scores"][str(last["current"])], 2),
                       "per_h_ci90": [round(lo, 3), round(hi, 3)], "need": hours_needed(d)}
        gains = [x["oos_gain"] for _, x in seq]
        ac = None
        if len(gains) > 3 and statistics.pstdev(gains) > 0:
            m = statistics.mean(gains)
            ac = sum((x - m) * (y - m) for x, y in zip(gains, gains[1:])) / sum((x - m) ** 2 for x in gains)
        out["params"][key] = {
            "best_by_run": [(i, x["best"], x["oos_gain"], x["confidence"], round(x["p_argmax"].get(str(x["best"]), 0), 2)) for i, x in seq],
            "live_last": last["current"], "literature": LITERATURE[key], "changes": changes, "significant_changes": sig_changes,
            "change_tests": tests, "runs_conf_ge_0.9_and_gain_ge_10": sum(1 for _, x in seq if x["confidence"] >= 0.9 and x["oos_gain"] >= 10),
            "last_need": last["need"], "last_alt": last["alt"], "lit_gap_last": lit_gap, "oos_gain_lag1_autocorr": None if ac is None else round(ac, 2),
            "modal_p_argmax_last": max(last["p_argmax"].values())}
    json.dump(out, open(os.path.join(OUT, "history_analysis.json"), "w"), indent=1, default=str)
    for k, v in out["params"].items():
        print(k, "changes", v["changes"], "sig", v["significant_changes"], "need", {a: (round(b, 1) if isinstance(b, float) else b) for a, b in v["last_need"].items()},
              "lit", v["lit_gap_last"], "ac", v["oos_gain_lag1_autocorr"])
    print(out["cpu"], out["tuner_as_coded_last"])
    return out


C_BLUE, C_ORANGE, C_AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED, GRID, SURF = "#0b0b0b", "#52514e", "#8a8984", "#e4e3df", "#fcfcfb"


def _svg(w, h, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="Helvetica, Arial, sans-serif">'
            f'<rect width="{w}" height="{h}" fill="{SURF}"/><text x="16" y="24" font-size="15" font-weight="600" fill="{INK}">{title}</text>{body}</svg>')


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fig_history():
    runs = [r for r in json.load(open(os.path.join(OUT, "tuner_history.json"))) if not r.get("skipped")]
    keys = list(GRIDS)
    pw, ph, cols = 470, 210, 2
    W, Hh = 16 + cols * pw, 70 + ((len(keys) + 1) // 2) * ph + 30
    body = []
    t0, t1 = runs[0]["T"], runs[-1]["T"]
    body.append(f'<text x="16" y="44" font-size="12" fill="{INK2}">Dot = value the tuner\'s fit window picks at each hourly run (larger = more often the optimum in a block bootstrap); '
                f'orange = live value; green dashed = literature.</text>')
    body.append(f'<g transform="translate(16,52)"><circle cx="6" cy="0" r="5" fill="{C_BLUE}"/><text x="16" y="4" font-size="11" fill="{INK2}">fit-best</text>'
                f'<line x1="80" y1="0" x2="100" y2="0" stroke="{C_ORANGE}" stroke-width="2"/><text x="106" y="4" font-size="11" fill="{INK2}">live</text>'
                f'<line x1="150" y1="0" x2="170" y2="0" stroke="{C_AQUA}" stroke-width="2" stroke-dasharray="4 3"/><text x="176" y="4" font-size="11" fill="{INK2}">literature</text>'
                f'<circle cx="250" cy="0" r="5" fill="none" stroke="{INK}" stroke-width="1.5"/><text x="260" y="4" font-size="11" fill="{INK2}">ring: OOS conf &gt;= 0.9</text></g>')
    for n, key in enumerate(keys):
        ox, oy = 16 + (n % cols) * pw, 70 + (n // cols) * ph
        grid = [str(v) for v in GRIDS[key]]
        cats = list(dict.fromkeys(grid + [str(r["params"][key]["current"]) for r in runs if key in r["params"]]))
        L, R, T, B = 150, pw - 20, 22, ph - 34
        yv = lambda c: T + (B - T) * (cats.index(c) + 0.5) / len(cats)
        xv = lambda t: L + (R - L) * (t - t0) / max(1.0, t1 - t0)
        body.append(f'<text x="{ox}" y="{oy + 14}" font-size="13" font-weight="600" fill="{INK}">{_esc(key)}</text>')
        for c in cats:
            lab = c if len(c) <= 22 else c[:21] + "…"
            body.append(f'<line x1="{ox + L}" x2="{ox + R}" y1="{oy + yv(c):.1f}" y2="{oy + yv(c):.1f}" stroke="{GRID}" stroke-width="1"/>'
                        f'<text x="{ox + L - 6}" y="{oy + yv(c) + 4:.1f}" font-size="10" text-anchor="end" fill="{INK2}">{_esc(lab)}</text>')
        lit = LITERATURE[key][0]
        if lit is not None and str(lit) in cats:
            body.append(f'<line x1="{ox + L}" x2="{ox + R}" y1="{oy + yv(str(lit)):.1f}" y2="{oy + yv(str(lit)):.1f}" stroke="{C_AQUA}" stroke-width="2" stroke-dasharray="4 3"/>')
        pts = [(xv(r["T"]), yv(str(r["params"][key]["current"]))) for r in runs if key in r["params"]]
        body.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' % (C_ORANGE, " ".join(f"{ox + x:.1f},{oy + y:.1f}" for x, y in pts)))
        for r in runs:
            p = r["params"].get(key)
            if not p:
                continue
            pr = p["p_argmax"].get(str(p["best"]), 0.0)
            x, y = ox + xv(r["T"]), oy + yv(str(p["best"]))
            body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{3 + 5 * pr:.1f}" fill="{C_BLUE}" fill-opacity="0.85" stroke="{SURF}" stroke-width="2">'
                        f'<title>{r["iso"]} best {_esc(p["best"])} oos {p["oos_gain"]} conf {p["confidence"]} p_argmax {pr}</title></circle>')
            if p["confidence"] >= 0.9 and str(p["best"]) != str(p["current"]):
                body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{6 + 5 * pr:.1f}" fill="none" stroke="{INK}" stroke-width="1.5"/>')
        for t in range(int(t0 // (3 * H)) * 3, int(t1 // H) + 1, 3):
            tt = t * H if t * H >= t0 else None
            if tt is None or tt > t1:
                continue
            body.append(f'<text x="{ox + xv(tt):.1f}" y="{oy + B + 16}" font-size="10" text-anchor="middle" fill="{MUTED}">{S._iso(tt)[11:16]}</text>')
    body.append(f'<text x="16" y="{Hh - 10}" font-size="10" fill="{MUTED}">Run time, UTC, 22 Oct 2026. Window = first log (21 Oct 22:43) to the run time; fit = older 70 %, test = newest 30 %. Source: research_notes/mm_upgrade_study.py</text>')
    open(os.path.join(FIG, "mm_tuner_history.svg"), "w").write(_svg(W, Hh, "".join(body), "Self-tuner MM optimum by hourly run (rebuilt)"))


def fig_gains():
    runs = [r for r in json.load(open(os.path.join(OUT, "tuner_history.json"))) if not r.get("skipped")]
    keys = list(GRIDS)
    W, Hh = 940, 360
    L, R, T, B = 70, W - 180, 50, Hh - 50
    vals = [r["params"][k]["oos_gain"] for r in runs for k in keys if k in r["params"]]
    lo, hi = min(min(vals), -10.5), max(max(vals), 10.5)
    t0, t1 = runs[0]["T"], runs[-1]["T"]
    xv = lambda t: L + (R - L) * (t - t0) / max(1.0, t1 - t0)
    yv = lambda v: B - (B - T) * (v - lo) / (hi - lo)
    pal = [C_BLUE, C_ORANGE, C_AQUA, "#eda100", "#e87ba4", "#008300"]
    body = [f'<line x1="{L}" x2="{R}" y1="{yv(0):.1f}" y2="{yv(0):.1f}" stroke="{INK2}" stroke-width="1"/>']
    for v in (10, -10):
        body.append(f'<line x1="{L}" x2="{R}" y1="{yv(v):.1f}" y2="{yv(v):.1f}" stroke="{GRID}" stroke-width="1" stroke-dasharray="3 3"/>'
                    f'<text x="{L - 6}" y="{yv(v) + 4:.1f}" font-size="10" text-anchor="end" fill="{INK2}">{v:+d}</text>')
    body.append(f'<text x="{L - 6}" y="{yv(0) + 4:.1f}" font-size="10" text-anchor="end" fill="{INK2}">0</text>')
    for i, k in enumerate(keys):
        pts = [(xv(r["T"]), yv(r["params"][k]["oos_gain"])) for r in runs if k in r["params"]]
        body.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' % (pal[i], " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)))
        for r in runs:
            p = r["params"].get(k)
            if p:
                body.append(f'<circle cx="{xv(r["T"]):.1f}" cy="{yv(p["oos_gain"]):.1f}" r="4" fill="{pal[i]}" stroke="{SURF}" stroke-width="2"><title>{k} {r["iso"]}: '
                            f'{_esc(p["best"])} oos {p["oos_gain"]} conf {p["confidence"]}</title></circle>')
        body.append(f'<line x1="{R + 16}" x2="{R + 36}" y1="{T + 18 * i}" y2="{T + 18 * i}" stroke="{pal[i]}" stroke-width="2"/>'
                    f'<text x="{R + 42}" y="{T + 18 * i + 4}" font-size="11" fill="{INK2}">{k}</text>')
    for t in range(int(t0 // (3 * H)) * 3, int(t1 // H) + 1, 3):
        if t0 <= t * H <= t1:
            body.append(f'<text x="{xv(t * H):.1f}" y="{B + 16}" font-size="10" text-anchor="middle" fill="{MUTED}">{S._iso(t * H)[11:16]}</text>')
    body.append(f'<text x="16" y="42" font-size="12" fill="{INK2}">Test-window gain of the fit-best value over the live one (tuner score, SUSQies). Dashed = the materiality bar (10).</text>')
    open(os.path.join(FIG, "mm_tuner_oos_gain.svg"), "w").write(_svg(W, Hh, "".join(body), "Out-of-sample gain of each run's optimum"))


def fig_candidates():
    c = json.load(open(os.path.join(OUT, "candidates.json")))
    rows = []
    for fam, v in c["families"].items():
        o = v["bucket"]["edge30"]
        if o.get("pick") == "baseline":
            rows.append((fam, "no fit-window winner", None, None, None, None, None)); continue
        ci = o["hour_ci90_per_h"]
        rows.append((fam, o["pick"], 24 * o["hour_mean_per_h"], 24 * ci[0], 24 * ci[1], o["oos_gain"] / c["test_days"], o["conf_8block"]))
    W, rh = 900, 34
    Hh = 96 + rh * len(rows) + 44
    L, R = 330, W - 150
    xs = [x for r in rows for x in r[2:6] if x is not None] + [0]
    lo, hi = min(xs) - 5, max(xs) + 5
    xv = lambda v: L + (R - L) * (v - lo) / (hi - lo)
    body = [f'<text x="16" y="44" font-size="12" fill="{INK2}">Fit-window winner per family, replayed on the newest 30 % ({c["window"]["fit_until"][11:]} to {c["window"]["to"][11:]} UTC, 22 Oct); fill P&amp;L marked at +30 min.</text>',
            f'<text x="16" y="60" font-size="12" fill="{INK2}">Dot = mean hourly gain x 24 with its 90 % hour-block CI; ring = whole-test-window gain per day (differs when the gain comes from inventory carried across hours).</text>',
            f'<line x1="{xv(0):.1f}" x2="{xv(0):.1f}" y1="76" y2="{Hh - 44}" stroke="{INK2}" stroke-width="1"/>']
    for i, (fam, pick, g, a, b, full, conf) in enumerate(rows):
        y = 92 + rh * i
        body.append(f'<text x="{L - 10}" y="{y + 4}" font-size="11" text-anchor="end" fill="{INK}">{_esc(fam)}: {_esc(pick[:34])}</text>')
        if g is None:
            continue
        body.append(f'<line x1="{xv(a):.1f}" x2="{xv(b):.1f}" y1="{y}" y2="{y}" stroke="{C_BLUE}" stroke-width="2" stroke-linecap="round" stroke-opacity="0.5"/>'
                    f'<circle cx="{xv(full):.1f}" cy="{y}" r="6" fill="none" stroke="{INK2}" stroke-width="1.5"/>'
                    f'<circle cx="{xv(g):.1f}" cy="{y}" r="5" fill="{C_BLUE}" stroke="{SURF}" stroke-width="2"/>'
                    f'<text x="{R + 10:.1f}" y="{y + 4}" font-size="10" fill="{INK2}">{g:+.0f} [{a:+.0f}, {b:+.0f}] /day</text>')
    for v in sorted({round(lo), 0, round(hi)}):
        body.append(f'<text x="{xv(v):.1f}" y="{Hh - 28}" font-size="10" text-anchor="middle" fill="{MUTED}">{v:+d}</text>')
    body.append(f'<text x="{(L + R) / 2:.0f}" y="{Hh - 12}" font-size="10" text-anchor="middle" fill="{MUTED}">SUSQies per day vs the live rule (live MM fill P&amp;L at +30 min in this window: {24 * c["baseline"]["bucket"]["test"]["edge30"] / (24 * c["test_days"]):+.0f}/day)</text>')
    open(os.path.join(FIG, "mm_candidates_oos.svg"), "w").write(_svg(W, Hh, "".join(body), "Candidate MM rules out of sample"))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "plot":
    os.makedirs(FIG, exist_ok=True)
    analyze_history(); fig_history(); fig_gains(); fig_candidates()


def settlement_value():
    """Value of MM fills at the outside-venue fair (Polymarket/Kalshi blend) instead of a 30-minute SIG mark: at the fair
    at the fill and at the LATEST fair in the logs (closest proxy for settlement value). For gap thresholds G, the value
    removed by not quoting the side that trades against the outside fair by >= G ticks; fit/test halves, market-cluster CI."""
    cfg0 = BotConfig.load()
    con = S.open_ro(os.path.join(ROOT, cfg0.db_path))
    latest = {r["exchange_id"]: r["fair"] for r in con.execute(
        "SELECT exchange_id, fair FROM venue_fair v WHERE fair IS NOT NULL AND ts=(SELECT MAX(ts) FROM venue_fair w WHERE w.exchange_id=v.exchange_id)")}
    rows = sorted((r for r in json.load(open(os.path.join(OUT, "fill_table.json")))["mm"] if r["xdev_signed"] is not None), key=lambda r: r["ts"])
    for r in rows:
        r["v_fill"] = r["qty"] * (r["xdev_signed"] + r["conc"]) * S.TICK                         # at the fair at the fill
        lf = latest.get(r["ex"])
        r["v_last"] = None if lf is None else r["qty"] * r["sgn"] * (lf - r["px"])                # at the latest fair
        r["v_sig_last"] = None
    t0, t1 = rows[0]["ts"], rows[-1]["ts"]; fu = t0 + S.FIT_FRAC * (t1 - t0)
    out = {"fills": len(rows), "shares": round(sum(r["qty"] for r in rows)), "hours": round((t1 - t0) / H, 1),
           "total_v_fill": round(sum(r["v_fill"] for r in rows), 1), "total_v_last": round(sum(r["v_last"] or 0 for r in rows), 1),
           "total_mo30": round(sum((r["mo30"] or 0) * r["qty"] * S.TICK for r in rows), 1), "rules": {}}
    for G in (2, 3, 5, 8):
        drop = lambda r: r["xdev_signed"] <= -G
        res = {}
        for vk in ("v_fill", "v_last"):
            g = lambda sub: -sum(r[vk] for r in sub if drop(r) and r[vk] is not None)
            by = {}
            for r in rows:
                if drop(r) and r[vk] is not None:
                    by[r["ex"]] = by.get(r["ex"], 0.0) - r[vk]
            allex = sorted({r["ex"] for r in rows}); arr = [by.get(e, 0.0) for e in allex]
            rng = random.Random(9); ms = sorted(sum(arr[rng.randrange(len(arr))] for _ in arr) for _ in range(4000))
            top2 = [e for _, e in sorted(((x, e) for e, x in by.items()), reverse=True)[:2]]
            res[vk] = {"all": round(g(rows), 1), "fit": round(g([r for r in rows if r["ts"] < fu]), 1), "test": round(g([r for r in rows if r["ts"] >= fu]), 1),
                       "market_ci90": [round(ms[200], 1), round(ms[3800], 1)], "without_top2": round(g([r for r in rows if r["ex"] not in top2]), 1),
                       "markets_hit": len(by), "markets_positive": sum(1 for x in by.values() if x > 0)}
        sh = sum(r["qty"] for r in rows if drop(r))
        res["dropped_shares"] = round(sh); res["dropped_frac"] = round(sh / sum(r["qty"] for r in rows), 2)
        res["mo30_given_up"] = round(sum((r["mo30"] or 0) * r["qty"] * S.TICK for r in rows if drop(r)), 1)
        out["rules"][f"gap>={G}"] = res
        print(G, json.dumps(res))
    print({k: v for k, v in out.items() if k != "rules"})
    json.dump(out, open(os.path.join(OUT, "settlement_value.json"), "w"), indent=1)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "settle":
    settlement_value()


def convergence():
    """Non-tautological check: after fills that traded against the outside fair by >= G ticks, did the SIG mid move toward
    the outside fair? Markouts at 30 / 60 min and at the LATEST SIG mid (venue_fair polls), by gap bucket."""
    cfg0 = BotConfig.load()
    con = S.open_ro(os.path.join(ROOT, cfg0.db_path))
    q, qt, xv, xt = _series(con)
    rows = [r for r in json.load(open(os.path.join(OUT, "fill_table.json")))["mm"] if r["xdev_signed"] is not None]
    out = {}
    for lo, hi in ((-99, -5), (-5, -2), (-2, 2), (2, 99), (-99, -3), (3, 99)):
        sub = [r for r in rows if lo <= r["xdev_signed"] < hi]
        res = {"fills": len(sub), "shares": round(sum(r["qty"] for r in sub))}
        for h in (1800, 3 * 3600, 6 * 3600):
            vals = []
            for r in sub:
                s = _after(xt[r["ex"]], xv[r["ex"]], r["ts"] + h, 1800.0)
                if s is not None and s[5] is not None:
                    vals.append(dict(ts=r["ts"], ex=r["ex"], qty=r["qty"], m=r["sgn"] * (s[5] - r["px"]) / S.TICK))
            ci = block_ci(vals, "m") if len(vals) >= 5 else None
            # market-cluster CI
            if vals:
                by = {}
                for v in vals:
                    a = by.setdefault(v["ex"], [0.0, 0.0]); a[0] += v["m"] * v["qty"]; a[1] += v["qty"]
                bl = list(by.values()); rng = random.Random(4); ms = []
                for _ in range(3000):
                    smp = [bl[rng.randrange(len(bl))] for _ in bl]; w = sum(b for _, b in smp)
                    ms.append(sum(a for a, _ in smp) / w)
                ms.sort()
                ci = dict(ci or {}, market_ci90=[round(ms[150], 2), round(ms[2850], 2)], markets=len(bl))
            res[f"+{h // 60}min"] = ci
        out[f"gap {lo}..{hi}"] = res
        print(lo, hi, json.dumps(res))
    json.dump(out, open(os.path.join(OUT, "convergence.json"), "w"), indent=1)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "converge":
    convergence()


def convergence_halves():
    """Gap-gate effect at +3 h / +6 h SIG marks, separately on the older 70 % and newest 30 % of fills."""
    cfg0 = BotConfig.load()
    con = S.open_ro(os.path.join(ROOT, cfg0.db_path))
    q, qt, xv, xt = _series(con)
    rows = sorted((r for r in json.load(open(os.path.join(OUT, "fill_table.json")))["mm"] if r["xdev_signed"] is not None), key=lambda r: r["ts"])
    t0, t1 = rows[0]["ts"], rows[-1]["ts"]; fu = t0 + S.FIT_FRAC * (t1 - t0)
    out = {}
    for G in (2, 3, 5):
        res = {}
        for h in (1800, 3 * 3600, 6 * 3600):
            for part, (a, b) in (("fit", (t0, fu)), ("test", (fu, t1 + 1))):
                gain = n = sh = 0.0
                for r in rows:
                    if not (a <= r["ts"] < b) or r["xdev_signed"] > -G:
                        continue
                    s = _after(xt[r["ex"]], xv[r["ex"]], r["ts"] + h, 1800.0)
                    if s is None or s[5] is None:
                        continue
                    gain -= r["qty"] * r["sgn"] * (s[5] - r["px"]); n += 1; sh += r["qty"]
                res[f"{part}+{h // 60}m"] = {"gain": round(gain, 1), "fills": int(n), "shares": round(sh), "ticks_per_share": round(gain / S.TICK / sh, 2) if sh else None}
        out[f"gap>={G}"] = res
        print(G, json.dumps(res))
    d = json.load(open(os.path.join(OUT, "convergence.json"))); d["halves"] = out
    json.dump(d, open(os.path.join(OUT, "convergence.json"), "w"), indent=1)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "converge":
    convergence_halves()
