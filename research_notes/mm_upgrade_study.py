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
import math
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
    import bisect as _b
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
        if ex not in q:
            continue
        ts = S._ts(f["filled_at"])
        yes_px = f["price"] if f["side"] == "yes" else round(1 - f["price"], 3)
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
