"""Parameter audit re-fits (24 Oct 2026): read-only replay of the live high-frequency arb (proposals/dev_churn.py) on
this venue's real quotes, one parameter swept at a time with every other rule at its live value.

Windows (UTC): IS = 21 Oct 23:50 - 23 Oct 21:25 (the arb_convergence.md test panel, now already seen);
OOS = 23 Oct 21:25 - the last stored poll (data no study has used). A lot's P&L (exit, or the touch at the end of the data)
counts in the window where it was entered. t-statistics: paired per-race differences vs the live value, OOS window.

No API calls; the database is copied and opened read-only. Run: nice -n 5 <venv>/bin/python research_notes/param_audit_study.py
"""
from __future__ import annotations

import bisect
import collections
import json
import math
import os
import shutil
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from sigbot.backtest_arb import open_copy_ro  # noqa: E402
from sigbot.proposals.volatile_moves import load_markets  # noqa: E402
from sigbot.proposals import dev_churn as DC  # noqa: E402

UTC = timezone.utc
T = 0.005
STEP = 300.0
B0 = datetime(2026, 10, 21, 23, 50, tzinfo=UTC).timestamp()
C0 = datetime(2026, 10, 23, 21, 25, tzinfo=UTC).timestamp()
OUT = os.path.join(HERE, "data", "research_param_audit")
os.makedirs(OUT, exist_ok=True)

LIVE = dict(dev=DC.DEV_MIN, take=DC.TAKE, max_hold=DC.MAX_HOLD, loss=DC.LOSS_EXIT, pot=4000.0, per_race=4000.0,
            clip=DC.MAX_SETS_PER_ENTRY, vol_pause=DC.VOL_PAUSE, vol_resume=DC.VOL_RESUME, kill=-200.0, skip_holding=4000.0,
            entry_gap=DC.ENTRY_GAP, reentry_gap=DC.REENTRY_GAP, fill="touch")


def load():
    con, tmp = open_copy_ro(os.path.join(HERE, "data", "sigbot.sqlite"))
    vq = con.execute("SELECT ts, race, party, instrument, bid, ask, mid FROM venue_quotes WHERE venue='sig' AND ts >= ? "
                     "ORDER BY ts", (B0 - 3 * 86400,)).fetchall()
    qs = con.execute("SELECT ts, exchange_id, best_bid, best_ask, bid_qty, ask_qty FROM quotes ORDER BY ts").fetchall()
    fills = con.execute("SELECT exchange_id, quantity FROM fills").fetchall()
    con.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return vq, qs, fills


def build(vq, qs, fills):
    mk = load_markets()
    legs = collections.defaultdict(set)
    for ex, (race, party, _) in mk.items():
        legs[race].add(ex)
    two = {r: sorted(e) for r, e in legs.items() if len(e) == 2 and r not in DC.EXCLUDE}
    polls = sorted({r[0] for r in vq})
    grid = [t for t in frange(B0, polls[-1], STEP)]
    by_inst = collections.defaultdict(list)
    for ts, race, party, inst, bid, ask, mid in vq:
        by_inst[str(inst)].append((ts, bid, ask))
    # leg touch on the grid (last quote at most 10 min old, as arb_convergence_study.panel_B)
    touch = {}
    for inst, rows in by_inst.items():
        tss = [r[0] for r in rows]
        bb, aa = [], []
        for g in grid:
            i = bisect.bisect_right(tss, g) - 1
            if i >= 0 and g - tss[i] <= 600 and rows[i][1] is not None and rows[i][2] is not None:
                bb.append(rows[i][1]); aa.append(rows[i][2])
            else:
                bb.append(None); aa.append(None)
        touch[inst] = (bb, aa)
    # live medians: dev_churn.overround_series on (race, party, ts, mid), truncated at each refresh time
    n_legs = {r: 2 for r in two}
    rows = [(race, party, ts, mid) for ts, race, party, inst, bid, ask, mid in vq if race in n_legs]
    series = DC.overround_series(rows, n_legs, polls[0], polls[-1])
    # touch sizes (quotes table: markets we quote); None = unknown
    sizes = collections.defaultdict(list)
    for ts, ex, bb_, ba_, bq, aq in qs:
        sizes[str(ex)].append((ts, bb_, ba_, bq, aq))
    pos = collections.defaultdict(float)
    for ex, q in fills:
        pos[str(ex)] += q
    return dict(grid=grid, two=two, touch=touch, series=series, sizes=sizes, pos=pos, last_poll=polls[-1])


def frange(a, b, s):
    t = a
    while t <= b:
        yield t
        t += s


def size_at(D, ex, t, side, px):
    rows = D["sizes"].get(ex)
    if not rows:
        return None
    tss = D.setdefault("size_ts", {}).get(ex)
    if tss is None:
        tss = D["size_ts"][ex] = [r[0] for r in rows]
    i = bisect.bisect_right(tss, t) - 1
    if i < 0 or t - rows[i][0] > 300:
        return None
    _, bb, ba, bq, aq = rows[i]
    if side == "bid":
        return bq if bb is not None and abs(bb - px) < 1e-6 else None
    return aq if ba is not None and abs(ba - px) < 1e-6 else None


def precompute(D):
    """Per refresh time (every 15 min, as the live loop): medians per race and the gap-movement ratio."""
    grid = D["grid"]
    med, vr = {}, {}
    trunc_cache = {}
    for i, g in enumerate(grid):
        if i % 3:
            continue
        s = {r: [x for x in xs if x[0] <= g] for r, xs in D["series"].items()}
        med[i] = DC.medians_from_series(s, g)
        vr[i] = DC.vol_ratio(s, g)
    return med, vr


def simulate(D, PRE, **kw):
    p = dict(LIVE); p.update(kw)
    med_at, vr_at = PRE
    grid, two, touch = D["grid"], D["two"], D["touch"]
    big = {r for r, exs in two.items() if p["skip_holding"] > 0 and any(abs(D["pos"].get(e, 0.0)) > p["skip_holding"] for e in exs)}
    lots, closed = [], []
    last_entry, last_exit, last_loss = {}, {}, {}
    realised, killed, paused = 0.0, False, False
    med, vr = {}, None
    vr_trace = []
    for i, g in enumerate(grid):
        if i in med_at:
            med, vr = med_at[i], vr_at[i]
            if vr is not None and p["vol_pause"] is not None:
                if not paused and vr >= p["vol_pause"]:
                    paused = True
                elif paused and vr < p["vol_resume"]:
                    paused = False
            vr_trace.append((g, vr, paused))
        quote = {}
        for r, exs in two.items():
            b = [touch.get(e, ([None] * len(grid),) * 2)[0][i] for e in exs]
            a = [touch.get(e, ([None] * len(grid),) * 2)[1][i] for e in exs]
            if None not in b and None not in a:
                quote[r] = (sum(b), sum(a), b, a)
        # exits
        open_val = 0.0
        for lot in lots:
            r = lot["race"]
            if r not in quote or lot["qty"] <= 0:
                continue
            sb, sa, _, a = quote[r]
            R = (2 - sa) - lot["cost"]
            why = None
            if killed:
                why = "stop"
            elif p["loss"] is not None and R <= -p["loss"] + 1e-9:
                why = "loss"
            elif R >= p["take"] - 1e-9:
                why = "take"
            elif g - lot["ts"] >= p["max_hold"]:
                why = "max_hold"
            if why is None:
                continue
            cap = lot["qty"]
            if p["fill"] == "touch":
                szs = [size_at(D, e, g, "ask", px) for e, px in zip(two[r], a)]
                cap = min(cap, int(min(s if s is not None else 600 for s in szs)))
            q = max(0, min(lot["qty"], cap))
            if q <= 0:
                continue
            lot["qty"] -= q
            realised += q * R
            closed.append(dict(race=r, qty=q, pnl=q * R, t_in=lot["ts"], t_out=g, why=why))
            last_exit[r] = g
            if why == "loss":
                last_loss[r] = g
        lots = [l for l in lots if l["qty"] > 0]
        for lot in lots:
            if lot["race"] in quote:
                lot["mark"] = (2 - quote[lot["race"]][1]) - lot["cost"]
            open_val += lot["qty"] * lot.get("mark", 0.0)
        if not killed and realised + open_val <= p["kill"]:
            killed = True
        if killed or paused:
            continue
        used = sum(l["qty"] * l["cost"] for l in lots)
        for r, (sb, sa, b, a) in quote.items():
            if r in big or r not in med:
                continue
            if g - last_entry.get(r, -1e18) < p["entry_gap"] or g - last_exit.get(r, -1e18) < p["reentry_gap"]:
                continue
            if g - last_loss.get(r, -1e18) < DC.LOSS_REENTRY_GAP:
                continue
            mid_ovr = sum((x + y) / 2 for x, y in zip(b, a)) - 1
            if mid_ovr < med[r] + p["dev"] - 1e-9:
                continue
            edge = sb - 1
            cost = 2 - sb
            if edge < 0.005 - 1e-9 or edge / cost < 0.005 - 1e-9:
                continue
            room = min(p["pot"] - used, p["per_race"] - sum(l["qty"] * l["cost"] for l in lots if l["race"] == r))
            depth = p["clip"]
            if p["fill"] == "touch":
                szs = [size_at(D, e, g, "bid", px) for e, px in zip(two[r], b)]
                depth = min(depth, int(min(s if s is not None else 600 for s in szs)))
            q = int(min(depth, p["clip"], max(0.0, room) // cost))
            last_entry[r] = g
            if q < 20:
                continue
            lots.append(dict(race=r, qty=q, cost=cost, ts=g, mark=(2 - sa) - cost))
            used += q * cost
    for lot in lots:      # end of data: open lots at the touch (cash objective)
        closed.append(dict(race=lot["race"], qty=lot["qty"], pnl=lot["qty"] * lot.get("mark", 0.0), t_in=lot["ts"],
                           t_out=None, why="open at end (touch)"))
    return dict(closed=closed, killed=killed, vr_trace=vr_trace)


def window_stats(res, t0, t1):
    xs = [c for c in res["closed"] if t0 <= c["t_in"] < t1]
    by = collections.defaultdict(float)
    for c in xs:
        by[c["race"]] += c["pnl"]
    return dict(pnl=sum(c["pnl"] for c in xs), sets=sum(c["qty"] for c in xs), n=len({(c["race"], c["t_in"]) for c in xs}),
                by_race=dict(by), whys=collections.Counter(c["why"] for c in xs))


def paired_t(a, b):
    races = set(a) | set(b)
    d = [a.get(r, 0.0) - b.get(r, 0.0) for r in races]
    d = [x for x in d if abs(x) > 1e-9] or [0.0]
    n = len(d)
    m = sum(d) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1)) if n > 1 else float("nan")
    se = sd / math.sqrt(n) if n > 1 else float("nan")
    return dict(diff=sum(d), mean=m, se_total=(se * n if se == se else float("nan")), t=(m / se if se and se == se and se > 0 else float("nan")), n_races=n)


SWEEPS = {
    "dev": [0.010, 0.015, 0.020, 0.025, 0.030, 0.040],
    "take": [0.005, 0.010, 0.015, 0.020, 0.030],
    "max_hold": [6 * 3600.0, 12 * 3600.0, 24 * 3600.0, 48 * 3600.0],
    "loss": [0.020, 0.030, 0.045, None],
    "pot": [2000.0, 4000.0, 8000.0, 12000.0, 20000.0, 32000.0],
    "per_race": [2000.0, 4000.0, 8000.0],
    "vol_pause": [1.0, 1.2, 1.5, None],
}


def main():
    vq, qs, fills = load()
    D = build(vq, qs, fills)
    PRE = precompute(D)
    end = D["last_poll"] + 1
    days_is, days_oos = (C0 - B0) / 86400, (end - C0) / 86400
    out = dict(windows=dict(IS=[B0, C0], OOS=[C0, end], days_is=days_is, days_oos=days_oos), sweeps={})
    for fill in ("touch", "deep"):
        base = simulate(D, PRE, fill=fill)
        b_is, b_oos = window_stats(base, B0, C0), window_stats(base, C0, end)
        out.setdefault("vr_trace", base["vr_trace"])
        for name, vals in SWEEPS.items():
            rows = []
            for v in vals:
                res = simulate(D, PRE, fill=fill, **{name: v})
                s_is, s_oos = window_stats(res, B0, C0), window_stats(res, C0, end)
                rows.append(dict(value=v, is_pnl=s_is["pnl"], is_per_day=s_is["pnl"] / days_is, is_n=s_is["n"], is_sets=s_is["sets"],
                                 oos_pnl=s_oos["pnl"], oos_per_day=s_oos["pnl"] / days_oos, oos_n=s_oos["n"], oos_sets=s_oos["sets"],
                                 t_is=paired_t(s_is["by_race"], b_is["by_race"]), t_oos=paired_t(s_oos["by_race"], b_oos["by_race"]),
                                 whys_oos=dict(s_oos["whys"]), whys_is=dict(s_is["whys"]),
                                 is_race_sd=race_sd(s_is["by_race"]), oos_race_sd=race_sd(s_oos["by_race"]), killed=res["killed"]))
            out["sweeps"][f"{fill}:{name}"] = rows
            print(fill, name)
            for r in rows:
                print(f"   {r['value']!s:>8}  IS {r['is_per_day']:+7.1f}/d (n {r['is_n']:3d})  OOS {r['oos_per_day']:+7.1f}/d (n {r['oos_n']:3d})"
                      f"  dOOS {r['t_oos']['diff']:+7.1f} t {r['t_oos']['t']:+.2f} ({r['t_oos']['n_races']} races)  whys {r['whys_oos']}")
    json.dump(out, open(os.path.join(OUT, "churn_sweeps.json"), "w"), indent=1, default=str)


def race_sd(by):
    """Standard error of the window total from per-race contributions (races as independent clusters)."""
    xs = list(by.values())
    n = len(xs)
    if n < 2:
        return float("nan")
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) * math.sqrt(n)


if __name__ == "__main__" and len(sys.argv) == 1:
    main()


# ------------------------------------------------------------------------------------------- event study (no pot)
def events(D, PRE, dev=LIVE["dev"], take=LIVE["take"], max_hold=LIVE["max_hold"], loss=LIVE["loss"], reentry=LIVE["reentry_gap"]):
    """One virtual set per signal, per race at most one open at a time (the live per-race spacing), no pot or depth limit.
    Returns entries: race, t_in, R (per set, at exit or at the touch at the end of data), hold seconds, why."""
    med_at, _ = PRE
    grid, two, touch = D["grid"], D["two"], D["touch"]
    big = {r for r, exs in two.items() if any(abs(D["pos"].get(e, 0.0)) > LIVE["skip_holding"] for e in exs)}
    out = []
    open_, last_exit, last_loss, med = {}, {}, {}, {}
    for i, g in enumerate(grid):
        if i in med_at:
            med = med_at[i]
        for r, exs in two.items():
            if r in big:
                continue
            b = [touch[e][0][i] if e in touch else None for e in exs]
            a = [touch[e][1][i] if e in touch else None for e in exs]
            if None in b or None in a:
                continue
            sb, sa = sum(b), sum(a)
            lot = open_.get(r)
            if lot is not None:
                R = (2 - sa) - lot["cost"]
                lot["mark"] = R
                why = ("loss" if loss is not None and R <= -loss + 1e-9 else "take" if R >= take - 1e-9
                       else "max_hold" if g - lot["t_in"] >= max_hold else None)
                if why:
                    out.append(dict(race=r, t_in=lot["t_in"], R=R, hold=g - lot["t_in"], why=why, dx=lot["dx"], edge=lot["edge"]))
                    del open_[r]
                    last_exit[r] = g
                    if why == "loss":
                        last_loss[r] = g
                continue
            if r not in med or g - last_exit.get(r, -1e18) < reentry or g - last_loss.get(r, -1e18) < DC.LOSS_REENTRY_GAP:
                continue
            dx = sum((x + y) / 2 for x, y in zip(b, a)) - 1 - med[r]
            if dx < dev - 1e-9:
                continue
            if sb - 1 < 0.005 - 1e-9 or (sb - 1) / (2 - sb) < 0.005 - 1e-9:
                continue
            open_[r] = dict(t_in=g, cost=2 - sb, mark=(2 - sa) - (2 - sb), dx=dx, edge=sb - 1)
    for r, lot in open_.items():
        out.append(dict(race=r, t_in=lot["t_in"], R=lot["mark"], hold=grid[-1] - lot["t_in"], why="open at end", dx=lot["dx"],
                        edge=lot["edge"]))
    return out


def ev_summary(evs, t0, t1, rng=None, B=2000):
    xs = [e for e in evs if t0 <= e["t_in"] < t1]
    by = collections.defaultdict(lambda: [0.0, 0.0, 0])
    for e in xs:
        by[e["race"]][0] += e["R"] / T
        by[e["race"]][1] += max(e["hold"], STEP) / 86400
        by[e["race"]][2] += 1
    n = len(xs)
    tot_R = sum(v[0] for v in by.values())
    tot_d = sum(v[1] for v in by.values())
    return dict(n=n, races=len(by), mean_R=(tot_R / n if n else float("nan")), eff=(tot_R / tot_d if tot_d else float("nan")),
                hold_h=(tot_d * 24 / n if n else float("nan")), by=dict(by),
                censored=sum(e["why"] == "open at end" for e in xs), whys=dict(collections.Counter(e["why"] for e in xs)))


def boot_diff(s_var, s_base, B=4000, seed=7):
    """Race-cluster bootstrap of capital efficiency (ticks per set-day held) and mean R per entry: variant, base, diff."""
    import random
    rnd = random.Random(seed)
    races = sorted(set(s_var["by"]) | set(s_base["by"]))
    if len(races) < 3:
        return dict(t_eff=float("nan"), t_R=float("nan"), lo=float("nan"), hi=float("nan"))
    z = [0.0, 0.0, 0]

    def stat(sample, s):
        R = sum(s["by"].get(r, z)[0] for r in sample)
        d = sum(s["by"].get(r, z)[1] for r in sample)
        n = sum(s["by"].get(r, z)[2] for r in sample)
        return (R / d if d else 0.0), (R / n if n else 0.0)

    de, dr, ve = [], [], []
    for _ in range(B):
        smp = [rnd.choice(races) for _ in races]
        a, b = stat(smp, s_var), stat(smp, s_base)
        de.append(a[0] - b[0]); dr.append(a[1] - b[1]); ve.append(a[0])
    sd = lambda v: (sum((x - sum(v) / len(v)) ** 2 for x in v) / (len(v) - 1)) ** 0.5
    pt_e = stat(races, s_var)[0] - stat(races, s_base)[0]
    pt_r = stat(races, s_var)[1] - stat(races, s_base)[1]
    ve.sort()
    return dict(d_eff=pt_e, t_eff=(pt_e / sd(de) if sd(de) > 0 else float("nan")), d_R=pt_r,
                t_R=(pt_r / sd(dr) if sd(dr) > 0 else float("nan")), lo=ve[int(0.025 * B)], hi=ve[int(0.975 * B)])


EV_SWEEPS = {
    "dev": [0.010, 0.015, 0.020, 0.025, 0.030, 0.040],
    "take": [0.005, 0.010, 0.015, 0.020, 0.030],
    "max_hold": [3 * 3600.0, 6 * 3600.0, 12 * 3600.0, 24 * 3600.0, 48 * 3600.0],
    "loss": [0.015, 0.020, 0.030, 0.045, None],
}


def main_events():
    vq, qs, fills = load()
    D = build(vq, qs, fills)
    PRE = precompute(D)
    end = D["last_poll"] + 1
    vrs = [v for _, v in PRE[1].items() if v is not None]
    print("gap-movement ratio over the data: min %.2f max %.2f" % (min(vrs), max(vrs)) if vrs else "no ratio")
    base_ev = events(D, PRE)
    bI, bO = ev_summary(base_ev, B0, C0), ev_summary(base_ev, C0, end)
    out = dict(windows=dict(IS=[B0, C0], OOS=[C0, end]), vr=[(k, v) for k, v in PRE[1].items()], ev={})
    for name, vals in EV_SWEEPS.items():
        rows = []
        for v in vals:
            evs = events(D, PRE, **{name: v})
            sI, sO = ev_summary(evs, B0, C0), ev_summary(evs, C0, end)
            row = dict(value=v, IS={k: sI[k] for k in sI if k != "by"}, OOS={k: sO[k] for k in sO if k != "by"},
                       bIS=boot_diff(sI, bI), bOOS=boot_diff(sO, bO))
            rows.append(row)
            print(f"{name:8s} {v!s:>8}  IS n {sI['n']:3d} R {sI['mean_R']:+.2f} eff {sI['eff']:+.2f} [{row['bIS']['lo']:+.2f},{row['bIS']['hi']:+.2f}]"
                  f" hold {sI['hold_h']:.1f}h | OOS n {sO['n']:3d} R {sO['mean_R']:+.2f} eff {sO['eff']:+.2f} [{row['bOOS']['lo']:+.2f},{row['bOOS']['hi']:+.2f}]"
                  f" cens {sO['censored']} | dEff t IS {row['bIS']['t_eff']:+.2f} OOS {row['bOOS']['t_eff']:+.2f}")
        out["ev"][name] = rows
    # pot sweep on the full replay, stop scaled with the pot (the planned 12k / -600 ratio)
    pots = []
    for pot in [2000.0, 4000.0, 8000.0, 12000.0, 20000.0]:
        for fill in ("touch", "deep"):
            res = simulate(D, PRE, pot=pot, kill=-pot / 20, fill=fill)
            sI, sO = window_stats(res, B0, C0), window_stats(res, C0, end)
            allw = window_stats(res, B0, end)
            pots.append(dict(pot=pot, fill=fill, is_pnl=sI["pnl"], oos_pnl=sO["pnl"], all_pnl=allw["pnl"], all_se=race_sd(allw["by_race"]),
                             is_se=race_sd(sI["by_race"]), oos_se=race_sd(sO["by_race"]), n_is=sI["n"], n_oos=sO["n"], killed=res["killed"]))
            print(f"pot {pot:7.0f} {fill:5s} IS {sI['pnl']:+7.1f} ({sI['n']}) OOS {sO['pnl']:+7.1f} ({sO['n']}) all {allw['pnl']:+7.1f} se {race_sd(allw['by_race']):.1f} killed {res['killed']}")
    out["pots"] = pots
    json.dump(out, open(os.path.join(OUT, "churn_events.json"), "w"), indent=1, default=str)


if __name__ == "__main__" and "events" in sys.argv:
    main_events()


# ------------------------------------------------------------------------------- MM outside-fair gate (xv_gate_ticks)
GATE_ON = datetime(2026, 10, 22, 19, 18, tzinfo=UTC).timestamp()     # 20:18 BST: gate wired
GATE_SPLIT = datetime(2026, 10, 23, 12, 0, tzinfo=UTC).timestamp()


def parse_iso(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        dg = "".join(ch for ch in tail if ch.isdigit())
        s = f"{head}.{dg[:6]}{tail[len(dg):]}"
    return datetime.fromisoformat(s).timestamp()


def main_gate():
    con, tmp = open_copy_ro(os.path.join(HERE, "data", "sigbot.sqlite"))
    fills = con.execute("SELECT exchange_id, price, quantity, filled_at FROM fills WHERE purpose='mm'").fetchall()
    vf = con.execute("SELECT ts, exchange_id, pm, kalshi, fair, sig_mid FROM venue_fair WHERE exchange_id IS NOT NULL ORDER BY ts").fetchall()
    vq = con.execute("SELECT ts, instrument, mid FROM venue_quotes WHERE venue='sig' ORDER BY ts").fetchall()
    con.close(); shutil.rmtree(tmp, ignore_errors=True)
    fair = collections.defaultdict(list)
    for ts, ex, pm, k, f, m in vf:
        fair[str(ex)].append((ts, pm, k, f, m))
    mids = collections.defaultdict(list)
    for ts, ex, m in vq:
        if m is not None:
            mids[str(ex)].append((ts, m))
    fts = {e: [r[0] for r in v] for e, v in fair.items()}
    mts = {e: [r[0] for r in v] for e, v in mids.items()}
    last_poll = max(r[0] for r in vq)
    rows = []
    for ex, px, q, at in fills:
        ex, t = str(ex), parse_iso(at)
        if ex not in fair or ex not in mids or abs(q) < 1:
            continue
        i = bisect.bisect_right(fts[ex], t) - 1
        if i < 0 or t - fair[ex][i][0] > 600:
            continue
        _, pm, k, f, m = fair[ex][i]
        if None in (pm, k, f, m) or abs(pm - k) > 0.08:
            continue
        s = 1 if q > 0 else -1
        g = (f - m) / T * s                         # outside fair minus mid, signed toward our side (negative = against us)
        mk = {}
        for lab, h in (("30m", 1800), ("3h", 10800)):
            if t + h > last_poll:
                continue
            j = bisect.bisect_right(mts[ex], t + h) - 1
            if j >= 0 and t + h - mids[ex][j][0] <= 1800:
                mk[lab] = (mids[ex][j][1] - px) / T * s
        rows.append(dict(t=t, ex=ex, qty=abs(q), g=g, **mk))
    out = {}
    for lab in ("30m", "3h"):
        res = []
        for win, (a, b) in (("pre-gate (fit data of mm_upgrade)", (0, GATE_ON)), ("gate live, IS", (GATE_ON, GATE_SPLIT)),
                            ("gate live, OOS", (GATE_SPLIT, 1e12))):
            xs = [r for r in rows if a <= r["t"] < b and lab in r]
            for G in (1, 2, 3, 4, 5, 6, None):
                kept = [r for r in xs if G is None or r["g"] > -G + 1e-9]
                drop = [r for r in xs if not (G is None or r["g"] > -G + 1e-9)]
                pnl_k = sum(r[lab] * r["qty"] * T for r in kept)
                # effect of the gate = minus the P&L of the dropped fills; t clustered by market (fills on one market share later mids)
                by = collections.defaultdict(float)
                for r in drop:
                    by[r["ex"]] += r[lab] * r["qty"] * T
                v = list(by.values())
                n = len(v)
                se = (math.sqrt(sum((x - sum(v) / n) ** 2 for x in v) / (n - 1)) * math.sqrt(n)) if n > 1 else float("nan")
                gain = -sum(v)
                res.append(dict(window=win, G=G, n_fills=len(xs), shares=sum(r["qty"] for r in xs), n_drop=len(drop),
                                shares_drop=sum(r["qty"] for r in drop), pnl_kept=pnl_k, gate_gain=gain, se=se,
                                t=(gain / se if se == se and se > 0 else float("nan")), markets_drop=n))
                print(f"{lab} {win:34s} G {G!s:>4}: fills {len(xs):3d} drop {len(drop):3d} ({sum(r['qty'] for r in drop):6.0f} sh, {n} mkts)"
                      f" kept P&L {pnl_k:+7.1f}  gate gain {gain:+7.1f} t {res[-1]['t']:+.2f}")
        out[lab] = res
    hist = collections.Counter(int(math.floor(r["g"])) for r in rows if r["t"] >= GATE_ON)
    print("signed gap histogram after the gate (ticks):", sorted(hist.items()))
    json.dump(dict(rows=rows, res=out), open(os.path.join(OUT, "mm_gate.json"), "w"), indent=1, default=str)


if __name__ == "__main__" and "gate" in sys.argv:
    main_gate()


# ------------------------------------------------------------------------------- catch-up sweep signal counts
def main_catchup():
    from sigbot.proposals import outside_sweep as OS
    con, tmp = open_copy_ro(os.path.join(HERE, "data", "sigbot.sqlite"))
    vf = con.execute("SELECT ts, race, party, exchange_id, pm, kalshi, sig_mid, sig_bid, sig_ask FROM venue_fair "
                     "WHERE ts >= ? ORDER BY ts", (B0,)).fetchall()
    con.close(); shutil.rmtree(tmp, ignore_errors=True)
    by_ts = collections.defaultdict(list)
    for ts, race, party, ex, pm, k, m, b, a in vf:
        by_ts[ts].append(dict(ts=ts, race=race, party=party, exchange_id=str(ex) if ex else None, pm=pm, kalshi=k, sig_mid=m,
                              sig_bid=b, sig_ask=a))
    res = {}
    for move in (0.01, 0.015, 0.02, 0.03):
        for gap in (1, 2, 3):
            OS.MOVE_MIN, OS.GAP_MIN_TICKS = move, gap
            cs = OS.CatchupSweep()
            n_is = n_oos = 0
            for ts in sorted(by_ts):
                rows = by_ts[ts]
                cs.push(rows)
                for s in cs.signals(rows, ts):
                    cs.last_signal[s["race"]] = ts
                    if ts < C0:
                        n_is += 1
                    else:
                        n_oos += 1
            res[f"{move}:{gap}"] = (n_is, n_oos)
            print(f"MOVE_MIN {move:.3f} GAP {gap}: signals IS {n_is} ({n_is / ((C0 - B0) / 86400):.1f}/day)  OOS {n_oos}")
    json.dump(res, open(os.path.join(OUT, "catchup_counts.json"), "w"), indent=1)


if __name__ == "__main__" and "catchup" in sys.argv:
    main_catchup()


# ------------------------------------------------------------------------------------------------------ figure
def mean_R_ci(evs, t0, t1, base=None, B=3000, seed=11):
    """Mean round trip per entry (ticks) with a race-cluster bootstrap 95% range; with `base`, the paired t of the
    difference (same race resamples for both)."""
    import random
    rnd = random.Random(seed)
    def agg(xs):
        by = collections.defaultdict(lambda: [0.0, 0])
        for e in xs:
            if t0 <= e["t_in"] < t1:
                by[e["race"]][0] += e["R"] / T
                by[e["race"]][1] += 1
        return by
    a = agg(evs)
    b = agg(base) if base is not None else None
    races = sorted(set(a) | set(b or {}))
    z = [0.0, 0]
    def m(by, smp):
        s = sum(by.get(r, z)[0] for r in smp); n = sum(by.get(r, z)[1] for r in smp)
        return s / n if n else float("nan")
    n = sum(v[1] for v in a.values())
    if not races:
        return dict(n=0, mean=float("nan"), lo=float("nan"), hi=float("nan"), t=float("nan"))
    vals, diffs = [], []
    for _ in range(B):
        smp = [rnd.choice(races) for _ in races]
        x = m(a, smp)
        if x == x:
            vals.append(x)
        if b is not None:
            y = m(b, smp)
            if x == x and y == y:
                diffs.append(x - y)
    vals.sort()
    pt = m(a, races)
    t = float("nan")
    if b is not None and len(diffs) > 30:
        mu = sum(diffs) / len(diffs)
        sd = (sum((d - mu) ** 2 for d in diffs) / (len(diffs) - 1)) ** 0.5
        d0 = pt - m(b, races)
        t = d0 / sd if sd > 1e-9 else float("nan")
    return dict(n=n, mean=pt, lo=vals[int(0.025 * len(vals))] if vals else float("nan"),
                hi=vals[int(0.975 * len(vals)) - 1] if vals else float("nan"), t=t)


def main_figure():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    vq, qs, fills = load()
    D = build(vq, qs, fills)
    PRE = precompute(D)
    end = D["last_poll"] + 1
    base = events(D, PRE)
    sweeps = {"dev": [0.010, 0.015, 0.020, 0.025, 0.030, 0.040], "take": [0.005, 0.010, 0.015, 0.020, 0.030],
              "max_hold": [3 * 3600.0, 6 * 3600.0, 12 * 3600.0, 24 * 3600.0, 48 * 3600.0], "loss": [0.015, 0.020, 0.030, 0.045, None]}
    res = {}
    for name, vals in sweeps.items():
        res[name] = []
        for v in vals:
            ev = events(D, PRE, **{name: v})
            res[name].append(dict(value=v, IS=mean_R_ci(ev, B0, C0, base), OOS=mean_R_ci(ev, C0, end, base)))
    ce = json.load(open(os.path.join(OUT, "churn_events.json")))
    gate = json.load(open(os.path.join(OUT, "mm_gate.json")))["res"]
    json.dump(res, open(os.path.join(OUT, "churn_meanR.json"), "w"), indent=1, default=str)
    for name, rows in res.items():
        for r in rows:
            print(name, r["value"], "IS", {k: round(v, 2) for k, v in r["IS"].items()}, "OOS", {k: round(v, 2) for k, v in r["OOS"].items()})

    IS_C, OOS_C, LIVE_C = "#1f77b4", "#d62728", "#444444"
    fig, axes = plt.subplots(7, 1, figsize=(10, 35))
    fig.suptitle("Re-fitting the bot's least-tested settings on the newest data", fontsize=15, fontweight="bold", y=0.997)
    fig.text(0.5, 0.981, "Each panel varies one setting; everything else stays at today's live value.\nDotted = days the setting "
             "was already judged on; solid = the newest data, never used before.", ha="center", fontsize=9.5, color="#444")
    is_lab = "dotted: 22-23 Oct (real quotes the 23 Oct study already tested on)"
    oos_lab = "solid: 23 Oct 21:25 - " + datetime.fromtimestamp(D["last_poll"], UTC).strftime("%d Oct %H:%M") + " UTC (new data)"

    def churn_panel(ax, name, xs, xlab, title, xt):
        rows = res[name]
        for key, col, ls, lab in (("IS", IS_C, ":", is_lab), ("OOS", OOS_C, "-", oos_lab)):
            ys = [r[key]["mean"] for r in rows]
            lo = [r[key]["lo"] for r in rows]
            hi = [r[key]["hi"] for r in rows]
            ax.plot(xs, ys, ls=ls, color=col, marker="o", label=lab)
            ax.fill_between(xs, lo, hi, color=col, alpha=0.12)
            for x, r in zip(xs, rows):
                tt = r[key]["t"]
                ax.annotate(f"n {r[key]['n']}" + (f"\nt {tt:+.1f}" if tt == tt else ""), (x, r[key]["mean"]),
                            textcoords="offset points", xytext=(6, 4 if key == "IS" else -22), fontsize=7.5, color=col)
        live_x = xs[[r["value"] for r in rows].index(LIVE[name])]
        ax.axvline(live_x, color=LIVE_C, lw=1, ls="--", label="today's live value")
        ax.axhline(0, color="grey", lw=0.8)
        ax.set_xticks(xs); ax.set_xticklabels(xt)
        ax.set_xlabel(xlab); ax.set_ylabel("profit per set per trade\n(ticks, after the spread)")
        ax.set_title(title, loc="left", fontweight="bold")
        ax.set_ylim(-12, 8)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="lower right")

    churn_panel(axes[0], "dev", [1, 2, 3, 4, 5, 6], "how much wider than its usual level a race's gap must be before we buy (ticks)",
                "A. High-frequency arb: when to buy (DEV_MIN)", ["2", "3", "4", "5", "6", "8"])
    churn_panel(axes[1], "take", [1, 2, 3, 4, 5], "profit per set at which we sell back (ticks)",
                "B. High-frequency arb: when to take profit (TAKE)", ["1", "2", "3", "4", "6"])
    churn_panel(axes[2], "max_hold", [1, 2, 3, 4, 5], "longest we hold a set before selling at whatever price (hours)",
                "C. High-frequency arb: maximum hold (MAX_HOLD)", ["3", "6", "12", "24", "48"])
    churn_panel(axes[3], "loss", [1, 2, 3, 4, 5], "loss per set at which we sell at once (ticks)",
                "D. High-frequency arb: loss exit (LOSS_EXIT)", ["3", "4", "6", "9", "never"])

    ax = axes[4]
    pots = [p for p in ce["pots"] if p["fill"] == "touch"]
    xs = [p["pot"] / 1000 for p in pots]
    for key, se, col, ls, lab in (("is_pnl", "is_se", IS_C, ":", is_lab), ("oos_pnl", "oos_se", OOS_C, "-", oos_lab)):
        ys = [p[key] for p in pots]
        ax.plot(xs, ys, ls=ls, color=col, marker="o", label=lab)
        ax.fill_between(xs, [y - 1.96 * p[se] for y, p in zip(ys, pots)], [y + 1.96 * p[se] for y, p in zip(ys, pots)], color=col, alpha=0.12)
    ax.axvline(4, color=LIVE_C, lw=1, ls="--", label="today's live pot (4k)")
    ax.axvline(12, color="#9467bd", lw=1, ls="-.", label="planned step after 3 clean exits (12k)")
    ax.axhline(0, color="grey", lw=0.8)
    ax.set_xlabel("money the sleeve may tie up at once (thousands of SUSQies; stop loss scaled to 1/20 of it)")
    ax.set_ylabel("profit over the window\n(SUSQies, open sets valued\nat what selling now fetches)")
    ax.set_title("E. High-frequency arb: size of the pot (churn_capital)", loc="left", fontweight="bold")
    ax.grid(alpha=0.3); ax.legend(fontsize=8, loc="lower left")

    ax = axes[5]
    Gs = [1, 2, 3, 4, 5, 6]
    for lab, mk in (("30m", "o"), ("3h", "s")):
        for win, col, ls in (("pre-gate (fit data of mm_upgrade)", IS_C, ":"), ("gate live, OOS", OOS_C, "-")):
            rows = [r for r in gate[lab] if r["window"] == win and r["G"] is not None]
            ys = [r["gate_gain"] for r in rows]
            se = [r["se"] if r["se"] == r["se"] else 0 for r in rows]
            name = ("22 Oct, before the gate (the data it was chosen on)" if col == IS_C else "23 Oct 12:00 - 24 Oct (new fills)")
            ax.plot(Gs, ys, ls=ls, color=col, marker=mk, alpha=1 if lab == "3h" else 0.55,
                    label=f"{name}, valued {'30 minutes' if lab == '30m' else '3 hours'} after the fill")
            if lab == "3h":
                ax.fill_between(Gs, [y - 1.96 * s for y, s in zip(ys, se)], [y + 1.96 * s for y, s in zip(ys, se)], color=col, alpha=0.1)
    ax.axvline(3, color=LIVE_C, lw=1, ls="--", label="today's live value (3 ticks)")
    ax.axhline(0, color="grey", lw=0.8)
    ax.set_xlabel("how far Polymarket/Kalshi must be from SIG's price before the market maker stops quoting that side (ticks)")
    ax.set_ylabel("money the rule saves vs\nno rule (SUSQies)")
    ax.set_title("F. Market maker: outside-price gate (xv_gate_ticks)", loc="left", fontweight="bold")
    ax.grid(alpha=0.3); ax.legend(fontsize=7.5, loc="lower right")

    ax = axes[6]
    sz = json.load(open(os.path.join(OUT, "sizing.json")))
    neu = json.load(open(os.path.join(OUT, "mm_sizing_neutral.json")))
    labels, is_v, oos_v, is_t, oos_t = [], [], [], [], []
    for r in sz["churn_dev0.02"][1:]:
        labels.append("HF arb: " + ("size up with the gap" if r["rule"].startswith("prop") else "size by fitted profit"))
        is_v.append((r["IS"]["pnl_ticks"] - r["IS"]["flat_ticks"]) / max(1, r["IS"]["n"]))
        oos_v.append((r["OOS"]["pnl_ticks"] - r["OOS"]["flat_ticks"]) / max(1, r["OOS"]["n"]))
        is_t.append(r["IS"]["t"]); oos_t.append(r["OOS"]["t"])
    for lab in ("30m", "3h"):
        a, b = neu[f"{lab}:1000.0:IS"], neu[f"{lab}:1000.0:OOS"]
        labels.append(f"MM: size by past markout\n(valued {'30 min' if lab == '30m' else '3 h'} after the fill)")
        is_v.append(a["gain"]); oos_v.append(b["gain"]); is_t.append(a["t"]); oos_t.append(b["t"])
    ax2 = ax.twinx()
    for i in range(len(labels)):
        a_ = ax if i < 2 else ax2
        a_.bar(i - 0.18, is_v[i], width=0.36, color=IS_C, alpha=0.45, hatch="..", label=is_lab if i == 0 else None)
        a_.bar(i + 0.18, oos_v[i], width=0.36, color=OOS_C, alpha=0.8, label=oos_lab if i == 0 else None)
        a_.annotate(f"{is_v[i]:+.1f}\nt {is_t[i]:+.1f}", (i - 0.18, is_v[i]), ha="center", va="bottom" if is_v[i] >= 0 else "top", fontsize=7.5, color=IS_C)
        a_.annotate(f"{oos_v[i]:+.1f}\nt {oos_t[i]:+.1f}", (i + 0.18, oos_v[i]), ha="center", va="bottom" if oos_v[i] >= 0 else "top", fontsize=7.5, color=OOS_C)
    ax.axvline(1.5, color="grey", lw=1)
    ax.set_ylim(-4, 4); ax2.set_ylim(-30, 30)
    ax.axhline(0, color="grey", lw=0.8)
    ax.set_xticks(range(len(labels))); ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylabel("HF arb (left two pairs):\ngain per trade vs today (ticks per set)")
    ax2.set_ylabel("MM (right two pairs): gain vs today\n(SUSQies, same total shares)")
    ax.set_title("G. Sizing by edge instead of fixed sizes (HF arb at the live 4-tick trigger)", loc="left", fontweight="bold")
    ax.grid(alpha=0.3, axis="y"); ax.legend(fontsize=8, loc="lower left")

    gl = ("Glossary\n"
          "high-frequency arb: buys a NO on both sides of a two-candidate race when the pair is unusually cheap, sells it back after a small gain\n"
          "gap / overround: how far the two prices add up above 1; a set pays exactly 1 at settlement\n"
          "tick: 0.005, the smallest price step.   set: one NO share on each side of a race\n"
          "profit per trade: what selling the set back fetches minus what it cost, after crossing the spread both ways; a trade still\n"
          "    open at the end of the data is valued at what selling it right then would fetch (the cash objective)\n"
          "n: trades in the window (one set each, no pot limit).   t: paired difference against today's value, races resampled together\n"
          "shaded bands: 95% range from resampling races (A-E) or markets (F)\n"
          "gate (F): the market maker posts no bid when the outside price is this many ticks below SIG's, no offer when above;\n"
          "    'money saved' = minus the profit of fills the rule would have blocked. Fills it blocked after 22 Oct cannot be seen,\n"
          "    so values above 3 ticks are only testable on the 22 Oct data\n"
          "sizing by edge (G): HF arb trades weighted by the gap beyond normal, or by a profit line fitted on 22-23 Oct; MM fills\n"
          "    weighted by 1 + that market's earlier markout (ticks, shrunk toward 0), rescaled to the same total shares")
    fig.text(0.07, 0.006, gl, fontsize=8.2, va="bottom", ha="left", family="DejaVu Sans",
             bbox=dict(boxstyle="round", facecolor="#f4f4f0", edgecolor="#999"))
    fig.tight_layout(rect=(0, 0.07, 1, 0.975))
    path = os.path.join(HERE, "research_notes", "figures", "param_audit_refits.png")
    fig.savefig(path, dpi=110)
    print("wrote", path)


if __name__ == "__main__" and "figure" in sys.argv:
    main_figure()


# ------------------------------------------------------------------------------------------------ sizing re-fits
def cluster_t(by):
    v = list(by.values())
    n = len(v)
    if n < 2:
        return float("nan"), n
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return (m / (sd / math.sqrt(n)) if sd > 0 else float("nan")), n


def main_sizing():
    out = {}
    # 1. high-frequency arb: size by how far the gap is beyond its usual level (the trade's edge signal)
    vq, qs, fills = load()
    D = build(vq, qs, fills)
    PRE = precompute(D)
    end = D["last_poll"] + 1
    for dev in (0.010, 0.020):
        ev = events(D, PRE, dev=dev)
        IS = [e for e in ev if e["t_in"] < C0]
        OOS = [e for e in ev if e["t_in"] >= C0]
        xs = [e["dx"] / T for e in IS]; ys = [e["R"] / T for e in IS]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / max(1e-12, sum((x - mx) ** 2 for x in xs))
        a = my - b * mx
        rules = {
            "flat (today)": lambda e: 1.0,
            "proportional to the gap beyond normal": lambda e, m=mx: (e["dx"] / T) / m,
            "fitted on 22-23 Oct: expected profit, 0 if negative": lambda e, a=a, b=b: max(0.0, a + b * e["dx"] / T),
        }
        norm = {k: (sum(f(e) for e in IS) / len(IS)) or 1.0 for k, f in rules.items()}
        res = []
        for k, f in rules.items():
            row = dict(rule=k, slope=b, intercept=a)
            for lab, xs_ in (("IS", IS), ("OOS", OOS)):
                w = [f(e) / norm[k] for e in xs_]
                pnl = sum(wi * e["R"] / T for wi, e in zip(w, xs_))
                d = collections.defaultdict(float)
                for wi, e in zip(w, xs_):
                    d[e["race"]] += (wi - 1.0) * e["R"] / T
                t, nr = cluster_t(d)
                row[lab] = dict(n=len(xs_), sets=sum(w), pnl_ticks=pnl, flat_ticks=sum(e["R"] / T for e in xs_), t=t, races=nr)
            res.append(row)
            print(f"churn dev {dev}: {k:55s} IS {row['IS']['pnl_ticks']:+7.1f} vs {row['IS']['flat_ticks']:+7.1f} t {row['IS']['t']:+.2f} |"
                  f" OOS {row['OOS']['pnl_ticks']:+7.1f} vs {row['OOS']['flat_ticks']:+7.1f} t {row['OOS']['t']:+.2f} (n {row['OOS']['n']})")
        print(f"   slope of profit on the gap beyond normal (IS): {b:+.3f} tick per tick, intercept {a:+.2f}")
        out[f"churn_dev{dev}"] = res
    # 2. market maker: size by each market's own past markouts (walk-forward, shrunk toward 0)
    rows = sorted(json.load(open(os.path.join(OUT, "mm_gate.json")))["rows"], key=lambda r: r["t"])
    for lab in ("30m", "3h"):
        hist = collections.defaultdict(lambda: [0.0, 0.0])
        Q0 = 1000.0
        rules = {"flat (today)": lambda m: 1.0, "1 + past markout (ticks), clipped 0-2": lambda m: min(2.0, max(0.0, 1.0 + m)),
                 "stop where past markout < -0.25 tick": lambda m: 0.0 if m < -0.25 else 1.0}
        acc = {k: {w: dict(pnl=0.0, flat=0.0, sh=0.0, by=collections.defaultdict(float)) for w in ("IS", "OOS")} for k in rules}
        for r in rows:
            if lab not in r:
                continue
            h = hist[r["ex"]]
            m = h[0] / (h[1] + Q0)
            win = "OOS" if r["t"] >= GATE_SPLIT else "IS"
            for k, f in rules.items():
                w = f(m)
                a_ = acc[k][win]
                a_["pnl"] += w * r[lab] * r["qty"] * T
                a_["flat"] += r[lab] * r["qty"] * T
                a_["sh"] += w * r["qty"]
                a_["by"][r["ex"]] += (w - 1) * r[lab] * r["qty"] * T
            h[0] += r[lab] * r["qty"]; h[1] += r["qty"]
        res = []
        for k in rules:
            row = dict(rule=k)
            for win in ("IS", "OOS"):
                a_ = acc[k][win]
                t, nm = cluster_t(a_["by"])
                row[win] = dict(pnl=a_["pnl"], flat=a_["flat"], shares=a_["sh"], t=t, markets=nm)
            res.append(row)
            print(f"MM {lab}: {k:40s} IS {row['IS']['pnl']:+7.1f} vs {row['IS']['flat']:+7.1f} t {row['IS']['t']:+.2f} |"
                  f" OOS {row['OOS']['pnl']:+7.1f} vs {row['OOS']['flat']:+7.1f} t {row['OOS']['t']:+.2f} shares {row['OOS']['shares']:.0f}")
        out[f"mm_{lab}"] = res
    json.dump(out, open(os.path.join(OUT, "sizing.json"), "w"), indent=1, default=str)


if __name__ == "__main__" and "sizing" in sys.argv:
    main_sizing()
