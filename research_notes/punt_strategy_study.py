"""Punt strategy study for 3 Nov (SIG Predictions Cup endgame). Read-only research; places nothing.

    <venv with numpy+matplotlib>/bin/python research_notes/punt_strategy_study.py [--n 20000]

Inputs (all cached; this script makes NO venue calls):
  data/research_punt/sig_reads/*.json   leaderboard reads of 23 Oct 21:58 UTC (all / 1d / 7d) via data/research_punt/sig_read.py
  data/leaderboard_snapshot.json, data/leaderboard_full.json, data/leaderboard_all_20261022.json   (22 Oct reads)
  data/candles_1d.json, data/sigbot.sqlite (venue_quotes, venue='sig'; read-only)   SIG mids for the exposure regression
  data/books.json, data/markets.json                                               SIG books (21 Oct) for slippage
  data/research_punt/hist/legs_index.csv, legs_hourly.csv                          Polymarket/Kalshi 2022-2026 history
  data/election_night/paths_2024.csv                                               1-minute 2024 election-night prices
Outputs: data/research_punt/results.json, research_notes/figures/punt_*.png
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = lambda *p: os.path.join(HERE, "data", *p)
FIG = os.path.join(HERE, "research_notes", "figures")
PRIZE = {1: 30000.0, 2: 5000.0, 3: 2500.0}          # predictionscup.com/rules ("up to"), fetched 23 Oct 2026
A_MAP, B_MAP = 0.14, 1.37                            # favourite_drift.md MAP calibration (favourite side)
A_RAW, B_RAW = -0.43, 2.59                           # favourite_drift.md raw 13-day fit (strong bias, lower bound for longshots)
ME = "sunny-beacon-7397"
RES: dict = {}


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def sigm(x):
    return 1 / (1 + np.exp(-x))


def p_true_longshot(price_outside, a=A_MAP, b=B_MAP):
    """Calibrated win probability of a longshot priced `price_outside` by the outside venues: the favourite (1-p)
    becomes logistic(a + b logit(1-p)); the longshot gets the rest. a=0,b=1 returns the price itself."""
    return 1 - sigm(a + b * logit(1 - np.asarray(price_outside, float)))


def wilson(k, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


# =====================================================================================================================
# Part 1: leaderboard panel (who is where, and how fast they move)
# =====================================================================================================================
def load_lb_reads():
    R = D("research_punt", "sig_reads")

    def rows(pfx):
        out, at = {}, None
        for f in sorted(glob.glob(os.path.join(R, pfx + "*.json"))):
            j = json.load(open(f))
            at = j["at"]
            for r in j["body"]["leaderboard"]:
                out[r["profileId"]] = r
        return out, at
    A, at = rows("lb_all_")
    tot = json.load(open(os.path.join(R, "lb_all_0.json")))["body"]["total"]
    D1, _ = rows("lb_1d_")
    D7, _ = rows("lb_7d_")
    return A, D1, D7, ts(at), tot


def leaderboard_panel():
    A, D1, D7, t_now, total = load_lb_reads()
    snaps = {}  # name -> (time, {pid: pnl})
    snaps["now"] = (t_now, {p: r["pnl"] for p, r in A.items()})
    snaps["d1_base"] = (ts("2026-10-23T00:00:00Z"), {p: A[p]["pnl"] - D1[p]["pnl"] for p in A if p in D1})
    snaps["d7_base"] = (ts("2026-10-18T00:00:00Z"), {p: A[p]["pnl"] - D7[p]["pnl"] for p in A if p in D7})
    s = json.load(open(D("leaderboard_snapshot.json")))
    snaps["oct22_a"] = (os.stat(D("leaderboard_snapshot.json")).st_mtime, {r["profileId"]: r["pnl"] for r in s})
    f = json.load(open(D("leaderboard_full.json")))
    snaps["oct22_b"] = (ts(f["pulled_at"]), {r["profileId"]: r["pnl"] for r in f["rows"]})
    g = json.load(open(D("leaderboard_all_20261022.json")))
    snaps["oct22_c"] = (ts(g["pulled_at"]), {r["profileId"]: r["pnl"] for r in g["rows"]})
    order = sorted(snaps, key=lambda k: snaps[k][0])
    top = sorted(A.values(), key=lambda r: -r["pnl"])
    me_rank = None
    for r in g["rows"]:
        if r["username"] == ME:
            me_rank = r
    panel = []
    for r in top[:60]:
        p = r["profileId"]
        row = dict(rank=r["rank"], user=r["username"], pid=p, balance_now=100000 + r["pnl"], trades=r["tradesCount"],
                   volume=r["volume"], pnl_7d=D7[p]["pnl"] if p in D7 else None, pnl_1d=D1[p]["pnl"] if p in D1 else None,
                   vol_7d=D7[p]["volume"] if p in D7 else 0.0)
        for k in order:
            row["pnl@" + k] = snaps[k][1].get(p)
        panel.append(row)
    days_7d = (t_now - snaps["d7_base"][0]) / 86400
    for row in panel:
        row["rate_per_day_7d"] = (row["pnl_7d"] or 0.0) / days_7d
    with open(D("research_punt", "leaderboard_panel.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(panel[0].keys()))
        w.writeheader()
        w.writerows(panel)
    RES["leaderboard"] = dict(read_at=datetime.fromtimestamp(t_now, timezone.utc).isoformat(), total_ranked=total,
                              snapshots={k: datetime.fromtimestamp(snaps[k][0], timezone.utc).isoformat() for k in order},
                              top12=[(x["rank"], x["user"], round(x["balance_now"]), round(x["pnl_7d"] or 0), round(x["rate_per_day_7d"]))
                                     for x in panel[:12]], days_in_7d_window=round(days_7d, 2),
                              me_22oct=me_rank)
    return panel, snaps, order


# =====================================================================================================================
# Part 2: can rivals' directional exposure be recovered from leaderboard P&L changes?  (regression on SIG mids)
# =====================================================================================================================
def sig_mids_at(times: dict) -> dict:
    """{snapshot name: {exchange_id: mid}} from the 1-day candles (18 Oct 00:00) and the bot's venue_quotes (sig rows)."""
    c1 = json.load(open(D("candles_1d.json")))
    out = {}
    con = sqlite3.connect(f"file:{D('sigbot.sqlite')}?mode=ro", uri=True)
    for name, t in times.items():
        m = {}
        for ex, v in c1.items():   # last daily close before t
            best = None
            for cd in v["candles"]:
                if ts(cd["time"]) + 86400 <= t + 1:
                    best = cd["close"]
            if best is not None:
                m[ex] = best
        row_t = con.execute("SELECT MAX(ts) FROM venue_quotes WHERE venue='sig' AND ts<=?", (t,)).fetchone()[0]
        if row_t is not None and t - row_t < 3 * 3600:
            for inst, mid, bid, ask in con.execute("SELECT instrument, mid, bid, ask FROM venue_quotes WHERE venue='sig' AND ts=?", (row_t,)):
                if mid is not None:
                    m[inst] = mid
        out[name] = m
    con.close()
    return out


def exposure_regression(panel, snaps, order):
    mk = json.load(open(D("markets.json")))
    party = {}
    for x in mk:
        ex = x["exchanges"][0]["id"]
        party[ex] = "D" if "Democrat" in x["title"] else ("R" if "Republican" in x["title"] else "I")
    mids = sig_mids_at({k: snaps[k][0] for k in order})
    base = mids["d7_base"]
    sign = {ex: (1 if base.get(ex, 0.5) > 0.5 else -1) for ex in party}
    common = [ex for ex in party if all(ex in mids[k] for k in order)]
    F = {}
    for k in order:
        fav = sum(sign[ex] * mids[k][ex] for ex in common)
        dem = sum((1 if party[ex] == "D" else -1 if party[ex] == "R" else 0) * mids[k][ex] for ex in common)
        F[k] = (fav, dem)
    ints = [(order[i], order[i + 1]) for i in range(len(order) - 1)]
    dF = [(F[b][0] - F[a][0], F[b][1] - F[a][1], (snaps[b][0] - snaps[a][0]) / 86400) for a, b in ints]
    fits = []
    for row in panel[:12]:
        y, X = [], []
        for (a, b), (dfav, ddem, dt) in zip(ints, dF):
            pa, pb = row.get("pnl@" + a), row.get("pnl@" + b)
            if pa is None or pb is None:
                continue
            y.append(pb - pa)
            X.append([dt, dfav, ddem])
        if len(y) < 4:
            fits.append(dict(user=row["user"], n=len(y)))
            continue
        X, y = np.array(X), np.array(y)
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        dof = len(y) - 3
        s2 = float(resid @ resid / dof) if dof > 0 else float("nan")
        try:
            cov = s2 * np.linalg.inv(X.T @ X)
            se = np.sqrt(np.diag(cov))
        except np.linalg.LinAlgError:
            se = [float("nan")] * 3
        fits.append(dict(user=row["user"], n=len(y), dof=dof, carry_per_day=round(beta[0]), se_carry=round(se[0]),
                         per_fav_index_pt=round(beta[1]), se_fav=round(se[1]), per_dem_index_pt=round(beta[2]), se_dem=round(se[2]),
                         y=[round(v) for v in y]))
    RES["exposure_regression"] = dict(
        n_markets=len(common), intervals=[f"{a}->{b}" for a, b in ints],
        factor_changes=[dict(d_fav_index=round(a, 3), d_dem_index=round(b, 3), days=round(c, 2)) for a, b, c in dF],
        note=("fav index = sum over markets of the favourite-side SIG mid (orientation fixed at 18 Oct); dem index = sum of "
              "D-leg mids minus R-leg mids. Coefficients are SUSQies of P&L per 1.0 move of the index (i.e. per one share "
              "of every favourite / every D leg). 6 snapshots -> 5 intervals, 3 unknowns -> 2 degrees of freedom."),
        fits=fits)


# =====================================================================================================================
# Part 3: history - last-24-h longshots, favourites that lost, election-night speed (Polymarket / Kalshi)
# =====================================================================================================================
def load_hist(cycles=("2022", "2024")):
    """One row per (venue, race, leg), general elections only by default (2022 midterms, 2024 general).
    2025-26 rows are excluded: the pull mislabelled at least one outcome (Polymarket 2025 NJ governor D, five duplicate
    rows marked lost) and specials are not comparable. Kalshi hourly prices are replaced by the bid/ask mid and dropped
    when the spread exceeds 0.15 (thin House books print single trades at 0.02-0.07 with bid 0.03 / ask 0.86)."""
    idx = list(csv.DictReader(open(D("research_punt", "hist", "legs_index.csv"))))
    wide = {}
    for r in csv.DictReader(open(D("research_punt", "hist", "legs_hourly.csv"))):
        wide[(r["venue"], r["race_key"], r["party_or_outcome"])] = r
    E0 = {"2024": ts("2024-11-05T00:00:00Z"), "2022": ts("2022-11-08T00:00:00Z")}
    kq = {}
    for r in csv.DictReader(open(D("research_punt", "hist", "legs_hourly_long.csv"))):
        if r["venue"] == "kalshi" and r["bid"] and r["ask"]:
            kq[(r["race_key"], r["leg"], int(r["ts_utc"]))] = (float(r["bid"]), float(r["ask"]))
    legs, seen = [], set()
    for r in idx:
        if r["office"] == "other" or r["outcome"] not in ("0", "1") or r["cycle"] not in cycles:
            continue
        key = (r["venue"], r["race_key"], r["leg"])
        if key in seen:
            continue
        seen.add(key)
        w = wide.get(key)
        hp = {}
        if w and r["gran"] == "h":
            for k, v in w.items():
                if k.startswith("h") and v not in ("", None):
                    try:
                        h = int(k[1:])
                        price = float(v)
                    except ValueError:
                        continue
                    if r["venue"] == "kalshi":
                        ba = kq.get((r["race_key"], r["leg"], int(E0[r["cycle"]] + 3600 * h)))
                        if ba is None:
                            continue
                        if ba[1] - ba[0] > 0.15:
                            continue
                        price = (ba[0] + ba[1]) / 2
                    hp[h] = price
        f = lambda x: float(x) if x not in ("", None) else None
        legs.append(dict(venue=r["venue"], cycle=r["cycle"], office=r["office"], race=r["cycle"] + "/" + r["race_key"], leg=r["leg"],
                         won=int(r["outcome"]), p_m1d=f(r["p_24h_before"]), p_07et=f(r["p_elecday_12utc"]),
                         p_17et=hp.get(22), hp=hp, gran=r["gran"], vol=f(r["volume_usd"])))
    return legs


def boot_band(pairs, n=2000, seed=5):
    """pairs = [(race, won)]: 95% band of the leg-level win share, resampling whole races (cluster bootstrap).
    With zero wins the bootstrap band collapses to 0, so the upper end is replaced by the rule-of-three bound on races."""
    if not pairs:
        return (np.nan, np.nan)
    by = defaultdict(list)
    for rc, w in pairs:
        by[rc].append(w)
    groups = list(by.values())
    rng = np.random.default_rng(seed)
    sums = np.array([sum(g) for g in groups], float)
    lens = np.array([len(g) for g in groups], float)
    idx = rng.integers(0, len(groups), (n, len(groups)))
    fr = sums[idx].sum(axis=1) / lens[idx].sum(axis=1)
    lo, hi = float(np.percentile(fr, 2.5)), float(np.percentile(fr, 97.5))
    if sums.sum() == 0:
        hi = 1 - 0.05 ** (1 / len(groups))
    return (lo, hi)


def calib_table(legs, key, bins):
    out = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        sel = [l for l in legs if l[key] is not None and lo <= l[key] < hi]
        races = {l["race"] for l in sel}
        k = sum(l["won"] for l in sel)
        n = len(sel)
        # effective n: distinct races (both venues quote the same race; mirror legs are the same event)
        ne = len(races)
        ke = sum(1 for rc in races if any(l["won"] for l in sel if l["race"] == rc))
        out.append(dict(lo=lo, hi=hi, n_legs=n, n_races=ne, wins_legs=k, wins_races=ke, mean_price=(sum(l[key] for l in sel) / n if n else None),
                        freq=(k / n if n else None), band=boot_band([(l["race"], l["won"]) for l in sel])))
    return out


def history_study():
    legs = load_hist()
    bins = [0.0, 0.02, 0.05, 0.10, 0.15, 0.25, 0.40, 0.60, 0.75, 0.85, 0.90, 0.95, 0.98, 1.0001]
    out = {"n_legs": len(legs), "n_races": len({l["race"] for l in legs}),
           "by_venue_cycle": {}}
    for l in legs:
        out["by_venue_cycle"][l["venue"] + " " + l["cycle"]] = out["by_venue_cycle"].get(l["venue"] + " " + l["cycle"], 0) + 1
    for key, label in (("p_m1d", "election eve 07:00 ET (Monday)"), ("p_07et", "election day 07:00 ET"), ("p_17et", "election day 17:00 ET")):
        tab = calib_table(legs, key, bins)
        ls = [l for l in legs if l[key] is not None and 0.02 <= l[key] < 0.15]
        fv = [l for l in legs if l[key] is not None and 0.85 <= l[key] < 0.98]
        exp_ls = sum(l[key] for l in ls)
        # P(0 wins | prices are fair), races treated as independent clusters: one leg per race (the cheapest)
        per_race = {}
        for l in ls:
            per_race.setdefault(l["race"], []).append(l)
        p_r = [np.mean([x[key] for x in v]) for v in per_race.values()]
        p0_fair = float(np.prod([1 - p for p in p_r]))
        p0_cal = float(np.prod([1 - p_true_longshot(p) for p in p_r]))
        fr = {}
        for l in fv:
            fr.setdefault(l["race"], []).append(l)
        out[key] = dict(label=label, table=tab,
                        longshots=dict(n_legs=len(ls), n_races=len(per_race), wins=sum(l["won"] for l in ls),
                                       expected_wins_at_price_legs=round(exp_ls, 2), expected_wins_races=round(float(sum(p_r)), 2),
                                       p_zero_wins_if_fair=round(p0_fair, 4), p_zero_wins_if_MAP_calibrated=round(p0_cal, 3),
                                       upper95_win_rate_races=round(1 - 0.05 ** (1 / max(1, len(per_race))), 4)),
                        favourites_85_98=dict(n_legs=len(fv), n_races=len(fr), lost=sum(1 - l["won"] for l in fv),
                                              expected_losses_races=round(float(sum(1 - np.mean([x[key] for x in v]) for v in fr.values())), 2)))
    # ---- election night (hourly, h = hours after 00:00 UTC of election day; polls close 23:00-06:00 UTC) ----
    night = range(24, 37)              # 19:00 ET to 07:00 ET
    hl = [l for l in legs if l["hp"] and l["cycle"] == "2024"]
    touched = []
    for l in hl:
        hrs = [h for h in night if h in l["hp"] and 0.02 <= l["hp"][h] < 0.15 and l["hp"].get(22, 0.5) >= 0.15]
        if hrs:
            touched.append(dict(race=l["race"], venue=l["venue"], leg=l["leg"], won=l["won"], first_h=hrs[0], p_first=l["hp"][hrs[0]],
                                p_17et=l["hp"].get(22)))
    winners_min = []
    for l in hl:
        if l["won"] and 22 in l["hp"]:
            mn = min([l["hp"][h] for h in night if h in l["hp"]] or [1])
            winners_min.append(dict(race=l["race"], venue=l["venue"], leg=l["leg"], p_17et=l["hp"][22], night_min=mn))
    # night calibration: every (leg, hour) after 19:00 ET, each leg weighted equally within a price bin
    nb = [0.0, 0.02, 0.05, 0.10, 0.15, 0.25, 0.40, 0.60, 0.75, 0.85, 0.95, 1.0001]
    ntab = []
    for lo, hi in zip(nb[:-1], nb[1:]):
        per_leg = {}
        for l in hl:
            ps = [l["hp"][h] for h in night if h in l["hp"] and lo <= l["hp"][h] < hi]
            if ps:
                per_leg[(l["venue"], l["race"], l["leg"])] = (np.mean(ps), l["won"], l["race"])
        n = len(per_leg)
        k = sum(v[1] for v in per_leg.values())
        races = {v[2] for v in per_leg.values()}
        ke = sum(1 for rc in races if any(v[1] for v in per_leg.values() if v[2] == rc))
        ntab.append(dict(lo=lo, hi=hi, n_legs=n, n_races=len(races), wins_legs=k,
                         mean_price=(float(np.mean([v[0] for v in per_leg.values()])) if n else None),
                         freq=(k / n if n else None), band=boot_band([(v[2], v[1]) for v in per_leg.values()])))
    out["night"] = dict(
        longshot_touch=dict(n_legs=len(touched), n_races=len({t["race"] for t in touched}), won=[t for t in touched if t["won"]],
                            n_won_legs=sum(t["won"] for t in touched)),
        winners_cheap_at_night=sorted([w for w in winners_min if w["night_min"] <= 0.30], key=lambda w: w["night_min"]),
        n_winner_legs=len(winners_min), calib=ntab)
    # ---- speed: 1-minute paths, eventual winners that were < 0.5 at 17:00 ET: minutes from 19:00 ET to cross 0.5 / 0.9,
    # and the largest 10-minute rise ----
    paths = defaultdict(list)
    with open(D("election_night", "paths_2024.csv")) as fh:
        for r in csv.DictReader(fh):
            if r["outcome"] == "1" and r["price"]:
                paths[(r["venue"], r["slug_or_ticker"], r["party"])].append((int(r["ts"]), float(r["price"])))
    t19 = ts("2024-11-06T00:00:00Z")
    speed = []
    for k, v in paths.items():
        v.sort()
        p_at = lambda t: next((p for tt, p in reversed(v) if tt <= t), None)
        p0 = p_at(t19)
        if p0 is None or p0 >= 0.5:
            continue
        t50 = next((tt for tt, p in v if tt >= t19 and p >= 0.5), None)
        t90 = next((tt for tt, p in v if tt >= t19 and p >= 0.9), None)
        mn = min(p for tt, p in v if tt >= t19)
        tmin = next(tt for tt, p in v if tt >= t19 and p == mn)
        # largest 10-minute rise after 19:00 ET
        arr = [(tt, p) for tt, p in v if tt >= t19]
        best, j = 0.0, 0
        for i in range(len(arr)):
            while arr[i][0] - arr[j][0] > 600:
                j += 1
            best = max(best, arr[i][1] - min(p for _, p in arr[j:i + 1]))
        speed.append(dict(venue=k[0], market=k[1], party=k[2], p_19et=p0, night_min=mn, min_at_et_h=round((tmin - t19) / 3600 + 19, 1),
                          minutes_to_050=(None if t50 is None else round((t50 - t19) / 60)),
                          minutes_to_090=(None if t90 is None else round((t90 - t19) / 60)), max_10min_rise=round(best, 3)))
    out["night_speed_upsets"] = speed
    RES["history"] = out
    return legs, out


def comeback_jumps(lo=0.06, hi=0.12, window_s=600):
    """2024 1-minute paths (all legs, both venues), after 19:00 ET: a leg at <= lo that trades >= hi within 10 minutes.
    These are the moments a lagging SIG book could still be bought at <= lo while the outside says >= hi."""
    paths = defaultdict(list)
    meta = {}
    with open(D("election_night", "paths_2024.csv")) as fh:
        for r in csv.DictReader(fh):
            if r["price"]:
                k = (r["venue"], r["slug_or_ticker"], r["party"])
                paths[k].append((int(r["ts"]), float(r["price"])))
                meta[k] = (r["office"], r["outcome"])
    t19 = ts("2024-11-06T00:00:00Z")
    ev = []
    for k, v in paths.items():
        v.sort()
        v = [x for x in v if x[0] >= t19]
        j = 0
        for i in range(len(v)):
            if v[i][1] <= lo:
                j = i
                while j < len(v) and v[j][0] - v[i][0] <= window_s:
                    if v[j][1] >= hi:
                        ev.append(dict(venue=k[0], market=k[1], party=k[2], office=meta[k][0], won=int(meta[k][1]),
                                       et_hour=round((v[i][0] - t19) / 3600 + 19, 2), p_from=v[i][1], p_to=v[j][1],
                                       p_plus60=next((p for tt, p in v if tt >= v[i][0] + 3600), None)))
                        break
                    j += 1
                if ev and ev[-1]["market"] == k[1] and ev[-1]["party"] == k[2]:
                    break   # first jump per leg only
    return ev


# =====================================================================================================================
# Part 4: SIG execution and the candidate tickets (21 Oct books walked level by level, 23 Oct outside prices)
# =====================================================================================================================
def shares_for(ladder, cash):
    sh, left = 0.0, cash
    for p, q in ladder:
        take = min(q, left / p)
        sh += take
        left -= take * p
        if left <= 1e-6:
            break
    return sh if left <= 1 else None


def candidate_singles(cash):
    """Every 'back one outcome' ticket with SIG touch <= 0.30, using punt_screener's race universe (read-only import):
    ladder = YES asks of the leg plus NO via the other leg's bids in two-leg races. Probabilities: outside blend,
    MAP-calibrated (punt_screener.calibrate), and the raw 13-day fit (stronger longshot haircut)."""
    from sigbot.proposals import punt_screener as ps
    races = ps.load_races()
    outs = ps.outcomes(races)
    rows = []
    for o in outs:
        if o["best"] > 0.30:
            continue
        sh = shares_for(o["ladder"], cash)
        if sh is None:
            continue
        po = o["p"]["outside"]
        rows.append(dict(label=o["label"], touch=o["best"], payout=sh, eff=cash / sh, p_outside=po, p_map=o["p"]["calibrated"],
                         p_raw=float(p_true_longshot(po, A_RAW, B_RAW)) if po < 0.5 else po, p_fair=o["p"]["fair"]))
    return rows, races


def candidate_rolls(cash, races, floor=900000.0, n=20000):
    from sigbot.proposals import punt_screener as ps
    res = ps.screen(cash, floor, races=races, n=n, top=12)
    return [dict(label=r["option"], payout=r["final"], eff=cash / r["final"], p_outside=r["p"]["outside"], p_map=r["p"]["calibrated"],
                 p_fair=r["p"]["fair"], mid_wealth=r["mid_wealth"]) for r in res["roll"]]


# =====================================================================================================================
# Part 5: the bar and the prize EV (Monte Carlo of the top 60 rivals on 4 Nov)
# =====================================================================================================================
SCEN = {
    # plain-word name: (share of chasers ranked 5-30 who punt, share of the top 4 who punt, correlation of rival punts with ours,
    #                   leaders' book move on the night our ticket wins, chance each top-8 account is disqualified)
    "base case": dict(q_chase=0.35, q_lead=0.05, corr=0.15, lead_hit=0.0, dq=0.0),
    "few rivals punt": dict(q_chase=0.10, q_lead=0.0, corr=0.0, lead_hit=0.0, dq=0.0),
    "most rivals punt": dict(q_chase=0.70, q_lead=0.10, corr=0.15, lead_hit=0.0, dq=0.0),
    "rivals copy our bet": dict(q_chase=0.35, q_lead=0.05, corr=0.60, lead_hit=0.0, dq=0.0),
    "our win hurts leaders": dict(q_chase=0.35, q_lead=0.05, corr=0.15, lead_hit=-0.06, dq=0.0),
    "our win lifts leaders": dict(q_chase=0.35, q_lead=0.05, corr=0.15, lead_hit=+0.06, dq=0.0),
    "some leaders banned": dict(q_chase=0.35, q_lead=0.05, corr=0.15, lead_hit=0.0, dq=0.15),
}


def project_field(panel, rng, n, days_left=11.0):
    """Scenario-independent draws for the top-60 rivals (we are excluded). See punt_strategy.md section 2 for the model."""
    rivals = [r for r in panel if r["user"] != ME][:60]
    k = len(rivals)
    B = np.array([r["balance_now"] for r in rivals])
    rate = np.array([max(0.0, r["rate_per_day_7d"]) for r in rivals])
    B3 = B[None, :] + days_left * rate[None, :] * rng.uniform(0.3, 1.0, (n, k))
    h = rng.uniform(0.3, 0.9, (n, k))
    s = rng.normal(0, 1, (1, k))
    z_nat = rng.normal(0, 1, (n, 1))
    ret = 0.04 + 0.05 * s * z_nat + rng.normal(0, 0.08, (n, k))
    # a punting rival aims to clear first place with a margin: target ~ lognormal around 1.25M (sd 15%), at least 1.05M,
    # at most 10x its balance (the most a single SIG book pays, see slippage)
    target = np.clip(np.exp(rng.normal(math.log(1.25e6), 0.15, (n, k))), 1.05e6, 10 * B3)
    e = np.minimum(B3 / target, 0.97)
    p_win = np.where(e >= 0.3, e * 0.97, p_true_longshot(e) * 0.97)
    from statistics import NormalDist
    nd = NormalDist()
    zt = np.vectorize(lambda p: nd.inv_cdf(min(max(p, 1e-9), 1 - 1e-9)))(p_win)
    return dict(B3=B3, hold=B3 * (1 + h * ret), h=h, target=target, p_win=p_win, zt=zt, eps=rng.normal(size=(n, k)),
                u_punt=rng.uniform(size=(n, k)), u_dq=rng.uniform(size=(n, k)), rank=np.arange(1, k + 1),
                users=[r["user"] for r in rivals])


def finals(field, sc, p_us, rng):
    """Rivals' final balances in the worlds where OUR ticket (win probability p_us) wins."""
    from statistics import NormalDist
    nd = NormalDist()
    n, k = field["B3"].shape
    u01 = rng.uniform(1e-12, max(p_us, 1e-9), n)
    U = np.array([nd.inv_cdf(x) for x in u01])[:, None]
    c = sc["corr"]
    rank = field["rank"][None, :]
    q = np.where(rank <= 4, sc["q_lead"], np.where(rank <= 30, sc["q_chase"], sc["q_chase"] / 2))
    punts = field["u_punt"] < q
    rival_win = (c * U + math.sqrt(1 - c * c) * field["eps"]) < field["zt"]
    punt_final = np.where(rival_win, field["target"], field["B3"] * 0.075)
    hold_final = field["hold"] * (1 + field["h"] * sc["lead_hit"])
    fin = np.where(punts, punt_final, hold_final)
    dq = field["u_dq"] < np.where(rank <= 8, sc["dq"], 0.0)
    return np.where(dq, 0.0, fin)


def rank_given_W(fin, Ws):
    top = -np.sort(-fin, axis=1)[:, :3]
    Ws = np.asarray(Ws, float)[:, None]
    p1 = (Ws > top[None, :, 0]).mean(axis=1) if False else (Ws > top[:, 0][None, :]).mean(axis=1)
    p2 = ((Ws <= top[:, 0][None, :]) & (Ws > top[:, 1][None, :])).mean(axis=1)
    p3 = ((Ws <= top[:, 1][None, :]) & (Ws > top[:, 2][None, :])).mean(axis=1)
    return p1, p2, p3


def prize_given_W(fin, Ws):
    p1, p2, p3 = rank_given_W(fin, Ws)
    return PRIZE[1] * p1 + PRIZE[2] * p2 + PRIZE[3] * p3, p1, p1 + p2 + p3


# =====================================================================================================================
# Part 6: figures (plain-word labels, one question per panel, glossary box on each PNG)
# =====================================================================================================================
GLOSS = {
    "longshot": "longshot = an outcome priced cheaply (here 2-15 cents), i.e. the market thinks it is unlikely",
    "favourite": "favourite = the outcome the market expects (priced 85-98 cents)",
    "calibration": "calibration = does a 5-cent outcome really win 5% of the time? (realised win rate vs price)",
    "bias": "longshot bias = cheap outcomes win less often than their price says; correction fitted on past Polymarket/Kalshi races",
    "roll": "two-stage roll = all cash on an early-closing race; if it wins, sell and put everything on a later-closing race",
    "bar": "bar = the final balance needed to finish 1st / 3rd (rivals' balances after election night, simulated)",
    "payout": "payout = what our ticket is worth if it wins (shares bought x 1 SUSQie); SIG's order books cap one ticket at ~1.0-1.2M",
    "susq": "SUSQies = the contest's play money; prizes are real dollars: 1st $30,000, 2nd $5,000, 3rd $2,500",
    "rivals": "rivals = the 60 accounts above us; 'punt' = a rival bets its whole balance to jump to ~1.25M",
    "wilson": "band = 95% range from resampling whole races (two venues quoting one race are one event); 0 wins: upper end = 3/n rule",
    "overnight": "overnight = 19:00 ET election day to 07:00 ET next morning, hourly prices",
    "tick": "tick = 0.005, the smallest price step on SIG",
}


def gloss_box(fig, keys, y=0.005):
    txt = "Glossary:  " + "\n".join(GLOSS[k] for k in keys)
    fig.text(0.01, y, txt, fontsize=9, va="bottom", ha="left", family="DejaVu Sans",
             bbox=dict(boxstyle="round,pad=0.4", fc="#f6f6f2", ec="#999999"))


def draw_all(R, singles, rolls_by_floor, curves, scen_names, hist_legs, hist, jumps, cash_rows, cash):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "legend.fontsize": 9})
    os.makedirs(FIG, exist_ok=True)
    colors = {"base case": "#1f77b4", "few rivals punt": "#2ca02c", "most rivals punt": "#d62728", "rivals copy our bet": "#9467bd",
              "our win hurts leaders": "#8c564b", "our win lifts leaders": "#e377c2", "some leaders banned": "#7f7f7f"}
    Wg = curves["W"]

    # ---------------- figure 1: chance, finish, prize value vs payout ----------------
    fig, ax = plt.subplots(3, 1, figsize=(10, 14), sharex=True)
    fig.subplots_adjust(top=0.95, bottom=0.15, hspace=0.12, left=0.11, right=0.97)
    fig.suptitle(f"All {cash/1e3:.1f}k of cash in one punt: chance it wins, where it lands, prize value", fontsize=12)
    Wk = np.array([s["payout"] for s in singles]) / 1e3
    a = ax[0]
    a.plot(Wg / 1e3, cash / Wg, color="black", lw=1.2, ls="--", label="ceiling at fair prices (cash / payout); nothing beats it")
    a.scatter(Wk, [s["p_outside"] for s in singles], s=14, color="#ff7f0e", alpha=0.35, label="one longshot, chance at Polymarket/Kalshi prices")
    a.scatter(Wk, [s["p_map"] for s in singles], s=14, color="#1f77b4", alpha=0.6, label="one longshot, chance after longshot-bias correction")
    rf = [(r["payout"] / 1e3, r["p_map"], r["p_outside"]) for fl, rs in rolls_by_floor.items() for r in rs[:3]]
    a.scatter([x[0] for x in rf], [x[2] for x in rf], marker="^", s=40, color="#ff7f0e", alpha=0.8, label="two-stage roll, chance at outside prices")
    a.scatter([x[0] for x in rf], [x[1] for x in rf], marker="^", s=40, color="#1f77b4", label="two-stage roll, chance after bias correction")
    a.scatter([x[0] for x in rf], [0.5 * x[1] for x in rf], marker="v", s=30, color="#1f77b4", alpha=0.45, label="same roll if the first sale misses its window half the time")
    a.set_yscale("log")
    a.set_ylim(3e-4, 2.0)
    a.set_ylabel("A: chance the punt wins\n(log scale)")
    a.legend(loc="upper right", fontsize=9, framealpha=0.9)
    a.grid(alpha=0.3)
    b = ax[1]
    for nm in scen_names:
        lw = 2.2 if nm == "base case" else 1.1
        b.plot(Wg / 1e3, curves[nm]["p3"], color=colors[nm], lw=lw, label=f"top 3 - {nm}")
    b.plot(Wg / 1e3, curves["base case"]["p1"], color="black", lw=2, ls=":", label="1st place - base case")
    lo1 = np.min([curves[nm]["p1"] for nm in scen_names], axis=0)
    hi1 = np.max([curves[nm]["p1"] for nm in scen_names], axis=0)
    b.fill_between(Wg / 1e3, lo1, hi1, color="black", alpha=0.08, label="1st place - range over all scenarios")
    b.set_ylabel("B: chance of a prize place\nif the punt wins")
    b.set_ylim(0, 1.02)
    b.legend(loc="upper left", fontsize=9, ncol=1, framealpha=0.9)
    b.grid(alpha=0.3)
    c = ax[2]
    for nm in scen_names:
        lw = 2.2 if nm == "base case" else 1.1
        c.plot(Wg / 1e3, curves[nm]["ev_ceiling"], color=colors[nm], lw=lw, alpha=0.9)
    evs = [s["p_map"] * np.interp(s["payout"], Wg, curves["base case"]["ev"]) for s in singles]
    c.scatter(Wk, evs, s=14, color="#1f77b4", alpha=0.6, label="one longshot (bias-corrected chance), base case")
    evr = [(r[0], r[1] * np.interp(r[0] * 1e3, Wg, curves["base case"]["ev"])) for r in rf]
    c.scatter([x[0] for x in evr], [x[1] for x in evr], marker="^", s=45, color="#1f77b4", label="two-stage roll (bias-corrected chance), base case")
    c.plot([], [], color="#1f77b4", lw=2.2, label="ceiling: fair-price chance x prize value, per scenario (colours as in B)")
    c.set_yscale("log")
    c.set_ylim(1, 2e4)
    c.set_ylabel("C: expected prize money\n(US dollars, log scale)")
    c.set_xlabel("payout if the punt wins (thousand SUSQies)")
    c.legend(loc="upper left", fontsize=9, framealpha=0.9)
    c.grid(alpha=0.3)
    c.set_xlim(600, 2000)
    gloss_box(fig, ["payout", "longshot", "bias", "roll", "bar", "rivals", "susq"])
    fig.savefig(os.path.join(FIG, "punt_ev.png"), dpi=110)
    plt.close(fig)

    # ---------------- figure 2: the bar ----------------
    fig, ax = plt.subplots(3, 1, figsize=(10, 12.5), sharex=True)
    fig.subplots_adjust(top=0.95, bottom=0.1, hspace=0.12, left=0.11, right=0.97)
    fig.suptitle("Where 1st and 3rd place are likely to finish on 4 Nov (top-60 rivals simulated)", fontsize=12)
    bins = np.arange(700, 2001, 20)
    for i, (key, lab) in enumerate((("third", "A: 3rd-best rival balance"), ("first", "B: best rival balance"))):
        a = ax[i]
        for nm in scen_names:
            hst, e = np.histogram(curves[nm][key] / 1e3, bins=bins, density=True)
            a.plot((e[1:] + e[:-1]) / 2, hst * 20, color=colors[nm], lw=2.2 if nm == "base case" else 1.1, label=nm)
        a.set_ylabel(lab + "\n(share of simulations per 20k)")
        a.grid(alpha=0.3)
        if i == 0:
            a.axvline(R["leaderboard"]["top12"][2][2] / 1e3, color="black", ls="--", lw=1)
            a.text(R["leaderboard"]["top12"][2][2] / 1e3 + 5, a.get_ylim()[1] * 0.9, "today's 3rd place", fontsize=9)
            a.legend(loc="upper right", fontsize=9)
        else:
            a.axvline(R["leaderboard"]["top12"][0][2] / 1e3, color="black", ls="--", lw=1)
            a.text(R["leaderboard"]["top12"][0][2] / 1e3 + 5, a.get_ylim()[1] * 0.9, "today's 1st place", fontsize=9)
    c = ax[2]
    for nm in scen_names:
        c.plot(Wg / 1e3, curves[nm]["ev"], color=colors[nm], lw=2.2 if nm == "base case" else 1.1, label=nm)
    c.set_ylabel("C: expected prize if we finish\nwith this balance (US dollars)")
    c.set_xlabel("our final balance (thousand SUSQies)")
    c.grid(alpha=0.3)
    c.set_xlim(700, 2000)
    gloss_box(fig, ["bar", "rivals", "susq"])
    fig.savefig(os.path.join(FIG, "punt_bar.png"), dpi=110)
    plt.close(fig)

    # ---------------- figure 3: calibration of past election longshots ----------------
    fig, ax = plt.subplots(3, 1, figsize=(10, 13), sharex=True)
    fig.subplots_adjust(top=0.89, bottom=0.12, hspace=0.12, left=0.11, right=0.97)
    fig.suptitle("Past US election markets (Polymarket 2022/24, Kalshi 2024): how often cheap outcomes won", fontsize=12)
    rng = np.random.default_rng(3)
    for i, (key, lab, tab) in enumerate((("p_07et", "A: priced at 07:00 ET on election day", hist["p_07et"]["table"]),
                                         (None, "B: priced overnight, after polls closed", hist["night"]["calib"]))):
        a = ax[i]
        a.plot([0, 1], [0, 1], color="black", lw=1, ls="--", label="perfectly calibrated (wins as often as the price says)")
        if key:
            pts = [(l[key], l["won"]) for l in hist_legs if l[key] is not None]
        else:
            pts = []
            for l in hist_legs:
                ps = [l["hp"][h] for h in range(24, 37) if h in l["hp"]]
                if ps:
                    pts.append((float(np.mean(ps)), l["won"]))
        xs = np.array([p for p, _ in pts])
        ys = np.array([w for _, w in pts]) + rng.uniform(-0.025, 0.025, len(pts))
        a.scatter(xs, ys, s=7, alpha=0.18, color="#555555", label="one market (won = top row, lost = bottom row)")
        mx = [t["mean_price"] for t in tab if t["mean_price"] is not None]
        fq = [t["freq"] for t in tab if t["mean_price"] is not None]
        lo = [t["band"][0] for t in tab if t["mean_price"] is not None]
        hi = [t["band"][1] for t in tab if t["mean_price"] is not None]
        a.fill_between(mx, lo, hi, color="#1f77b4", alpha=0.2, label="95% band (resampling whole races)")
        a.plot(mx, fq, "o-", color="#1f77b4", label="share that won, by price bucket")
        xx = np.linspace(0.005, 0.995, 200)
        cal = np.where(xx < 0.5, p_true_longshot(xx), 1 - p_true_longshot(1 - xx))
        a.plot(xx, cal, color="#d62728", lw=1.2, label="longshot-bias correction used in this study")
        a.set_ylabel(lab + "\n(share that won)")
        a.set_ylim(-0.05, 1.05)
        a.grid(alpha=0.3)
        if i == 0:
            fig.legend(*a.get_legend_handles_labels(), loc="upper center", bbox_to_anchor=(0.5, 0.955), ncol=2, fontsize=9, frameon=False)
        ia = a.inset_axes([0.62, 0.08, 0.36, 0.38])
        ia.plot([0, 0.2], [0, 0.2], color="black", lw=1, ls="--")
        sel = [t for t in tab if t["mean_price"] is not None and t["hi"] <= 0.25]
        ia.fill_between([t["mean_price"] for t in sel], [t["band"][0] for t in sel], [t["band"][1] for t in sel], color="#1f77b4", alpha=0.2)
        ia.plot([t["mean_price"] for t in sel], [t["freq"] for t in sel], "o-", color="#1f77b4")
        ia.plot(xx[xx < 0.2], cal[xx < 0.2], color="#d62728", lw=1.2)
        ia.set_xlim(0, 0.22)
        ia.set_ylim(0, 0.3)
        ia.set_title("zoom: prices under 25 cents", fontsize=9)
        ia.tick_params(labelsize=9)
    c = ax[2]
    t1 = hist["p_07et"]["table"]
    t2 = hist["night"]["calib"]
    c.bar([t["mean_price"] or (t["lo"] + t["hi"]) / 2 for t in t1], [t["n_races"] for t in t1], width=0.018, color="#1f77b4", alpha=0.6, label="races, 07:00 ET election day")
    c.bar([(t["mean_price"] or (t["lo"] + t["hi"]) / 2) + 0.018 for t in t2], [t["n_races"] for t in t2], width=0.018, color="#ff7f0e", alpha=0.6, label="races, overnight")
    c.set_ylabel("C: number of races\nper price bucket")
    c.set_xlabel("market price of the outcome (cents per 1 SUSQie / dollar of payout)")
    c.legend(loc="upper center", fontsize=9)
    c.grid(alpha=0.3)
    gloss_box(fig, ["longshot", "calibration", "bias", "wilson", "overnight"])
    fig.savefig(os.path.join(FIG, "punt_calibration.png"), dpi=110)
    plt.close(fig)

    # ---------------- figure 4: how fast prices move overnight ----------------
    fig, ax = plt.subplots(2, 1, figsize=(10, 10), sharex=True)
    fig.subplots_adjust(top=0.94, bottom=0.14, hspace=0.12, left=0.11, right=0.97)
    fig.suptitle("Election night 2024: eventual winners that traded cheap overnight, and how fast they recovered", fontsize=12)
    a = ax[0]
    shown = 0
    for l in hist_legs:
        if l["cycle"] != "2024" or not l["won"] or not l["hp"]:
            continue
        hrs = [h for h in range(20, 50) if h in l["hp"]]
        if not hrs or min(l["hp"][h] for h in hrs if h >= 24) > 0.30 if [h for h in hrs if h >= 24] else True:
            continue
        a.plot([h - 24 + 19 for h in hrs], [l["hp"][h] for h in hrs], color="#1f77b4" if l["venue"] == "polymarket" else "#ff7f0e", alpha=0.5, lw=1)
        shown += 1
    a.plot([], [], color="#1f77b4", label="Polymarket, one eventual winner")
    a.plot([], [], color="#ff7f0e", label="Kalshi (bid/ask mid), one eventual winner")
    a.set_ylabel("A: price of an eventual winner\nthat fell to 30 cents or less")
    a.legend(loc="upper left", fontsize=9)
    a.grid(alpha=0.3)
    b = ax[1]
    jx = [e["et_hour"] for e in jumps]
    b.scatter([e["et_hour"] for e in jumps if not e["won"]], [e["p_to"] for e in jumps if not e["won"]], s=22, color="#999999", alpha=0.6, label="cheap leg jumped from <=6 to >=12 cents in 10 min, later LOST")
    b.scatter([e["et_hour"] for e in jumps if e["won"]], [e["p_to"] for e in jumps if e["won"]], s=40, color="#d62728", label="same jump, later WON")
    b.set_ylabel("B: price 10 minutes after the jump\n(the leg was <= 6 cents just before)")
    b.set_xlabel("hour (ET) from 19:00 on election day; 24 = midnight, 30 = 06:00 next morning")
    b.legend(loc="upper right", fontsize=9)
    b.grid(alpha=0.3)
    b.set_xlim(18.5, 43)
    gloss_box(fig, ["overnight", "longshot", "tick"])
    fig.savefig(os.path.join(FIG, "punt_night.png"), dpi=110)
    plt.close(fig)

    # ---------------- figure 5: how the best punt changes with our cash ----------------
    fig, ax = plt.subplots(2, 1, figsize=(10, 9.5), sharex=True)
    fig.subplots_adjust(top=0.93, bottom=0.17, hspace=0.12, left=0.12, right=0.97)
    fig.suptitle("How the best punt changes with the cash we have on 3 Nov (base case for rivals)", fontsize=12)
    xs = [r["cash"] / 1e3 for r in cash_rows]
    a = ax[0]
    a.plot(xs, [r["single_W"] / 1e3 for r in cash_rows], "o-", color="#1f77b4", label="best single longshot: payout if it wins")
    a.plot(xs, [r["roll_W"] / 1e3 for r in cash_rows], "^-", color="#2ca02c", label="best two-stage roll: payout if it wins")
    a.axhspan(*np.percentile(curves["base case"]["third"], [25, 75]) / 1e3, color="#999999", alpha=0.25, label="3rd-place bar, middle half of simulations")
    a.axhspan(*np.percentile(curves["base case"]["first"], [25, 75]) / 1e3, color="#d62728", alpha=0.15, label="1st-place bar, middle half of simulations")
    a.set_ylabel("A: payout if the punt wins\n(thousand SUSQies)")
    a.legend(loc="lower right", fontsize=9)
    a.grid(alpha=0.3)
    b = ax[1]
    b.plot(xs, [r["single_ev"] for r in cash_rows], "o-", color="#1f77b4", label="best single longshot (bias-corrected chance)")
    b.plot(xs, [r["roll_ev"] for r in cash_rows], "^-", color="#2ca02c", label="best two-stage roll (bias-corrected chance)")
    b.fill_between(xs, [r["roll_ev_lo"] for r in cash_rows], [r["roll_ev_hi"] for r in cash_rows], color="#2ca02c", alpha=0.15,
                   label="roll: from 'sale misses half the time' to 'outside prices are right'")
    b.set_yscale("log")
    b.set_ylim(1, 5000)
    b.set_ylabel("B: expected prize money\n(US dollars, log scale)")
    b.set_xlabel("our cash on 3 Nov evening (thousand SUSQies)")
    b.legend(loc="upper left", fontsize=9)
    b.grid(alpha=0.3)
    gloss_box(fig, ["roll", "payout", "bias", "bar", "susq"])
    fig.savefig(os.path.join(FIG, "punt_cash.png"), dpi=110)
    plt.close(fig)


# =====================================================================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--screen-n", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cash", type=float, default=97500.0)
    ap.add_argument("--no-figs", action="store_true")
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    panel, snaps, order = leaderboard_panel()
    exposure_regression(panel, snaps, order)
    hist_legs, hist = history_study()
    jumps = comeback_jumps()
    RES["history"]["comeback_jumps"] = dict(n=len(jumps), won=sum(e["won"] for e in jumps),
                                            still_12c_after_60min=sum(1 for e in jumps if (e["p_plus60"] or 0) >= 0.12),
                                            won_if_still_12c=sum(e["won"] for e in jumps if (e["p_plus60"] or 0) >= 0.12), events=jumps)
    cash = args.cash
    singles, races = candidate_singles(cash)
    floors = (1.0e6, 1.2e6, 1.3e6, 1.5e6, 1.8e6)
    rolls_by_floor = {fl: candidate_rolls(cash, races, floor=fl, n=args.screen_n) for fl in floors}
    field = project_field(panel, rng, args.n)
    Wg = np.arange(0.60e6, 2.001e6, 0.01e6)
    curves = {"W": Wg}
    for nm, sc in SCEN.items():
        fin = finals(field, sc, 0.02, rng)
        ev, p1, p3 = prize_given_W(fin, Wg)
        srt = -np.sort(-fin, axis=1)
        curves[nm] = dict(ev=ev, p1=p1, p3=p3, ev_ceiling=(cash / Wg) * ev, first=srt[:, 0], third=srt[:, 2])
    # candidate table (base case EV and range over scenarios)
    def ev_row(label, W, p_map, p_out, kind):
        evs = {nm: float(np.interp(W, Wg, curves[nm]["ev"])) for nm in SCEN}
        return dict(kind=kind, label=label, payout=round(W), p_map=round(p_map, 4), p_outside=round(p_out, 4),
                    p1_given_win_base=round(float(np.interp(W, Wg, curves["base case"]["p1"])), 3),
                    p3_given_win_base=round(float(np.interp(W, Wg, curves["base case"]["p3"])), 3),
                    ev_base=round(p_map * evs["base case"]), ev_min=round(min(p_map * v for v in evs.values())),
                    ev_max=round(max(p_map * v for v in evs.values())), ev_base_outside=round(p_out * evs["base case"]))
    cands = [ev_row(s["label"], s["payout"], s["p_map"], s["p_outside"], "single") for s in singles]
    for fl, rs in rolls_by_floor.items():
        cands += [ev_row(r["label"], r["payout"], r["p_map"], r["p_outside"], f"roll>= {fl/1e6:.1f}M") for r in rs[:6]]
    cands.sort(key=lambda r: -r["ev_base"])
    RES["candidates"] = cands[:40]
    RES["bar_quantiles"] = {nm: dict(first=[round(float(np.percentile(curves[nm]["first"], q))) for q in (10, 25, 50, 75, 90)],
                                     third=[round(float(np.percentile(curves[nm]["third"], q))) for q in (10, 25, 50, 75, 90)])
                            for nm in SCEN}
    RES["prize_given_W"] = {nm: {f"{w/1e6:.2f}M": dict(p1=round(float(np.interp(w, Wg, curves[nm]["p1"])), 3),
                                                       p_top3=round(float(np.interp(w, Wg, curves[nm]["p3"])), 3),
                                                       ev=round(float(np.interp(w, Wg, curves[nm]["ev"]))))
                                 for w in (0.9e6, 1.0e6, 1.1e6, 1.2e6, 1.3e6, 1.4e6, 1.5e6, 1.7e6, 2.0e6)} for nm in SCEN}
    RES["scenarios"] = SCEN
    # optimal punt vs our cash (base case)
    cash_rows = []
    for C in (60000.0, 80000.0, 97500.0, 120000.0, 150000.0, 200000.0):
        sg, _ = candidate_singles(C)
        bs = max(sg, key=lambda s: s["p_map"] * np.interp(s["payout"], Wg, curves["base case"]["ev"]))
        best_r, best_v = None, -1
        for fl in (1.0e6, 1.2e6, 1.3e6, 1.5e6, 1.8e6):
            for r in candidate_rolls(C, races, floor=fl, n=args.screen_n)[:6]:
                v = r["p_map"] * np.interp(r["payout"], Wg, curves["base case"]["ev"])
                if v > best_v:
                    best_r, best_v = r, v
        cash_rows.append(dict(cash=C, single=bs["label"], single_W=bs["payout"], single_p=bs["p_map"],
                              single_ev=bs["p_map"] * float(np.interp(bs["payout"], Wg, curves["base case"]["ev"])),
                              roll=(best_r or {}).get("label"), roll_W=(best_r or {}).get("payout", np.nan), roll_p=(best_r or {}).get("p_map"),
                              roll_ev=best_v, roll_ev_lo=0.5 * best_v,
                              roll_ev_hi=((best_r or {}).get("p_outside", 0) * float(np.interp((best_r or {}).get("payout", 0), Wg, curves["base case"]["ev"])))))
    RES["optimal_vs_cash"] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in cash_rows]
    json.dump(RES, open(D("research_punt", "results.json"), "w"), indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else float(o))
    if not args.no_figs:
        draw_all(RES, singles, rolls_by_floor, curves, list(SCEN), hist_legs, hist, jumps, cash_rows, cash)
    print("candidates (top 12 by base-case prize EV):")
    for r in RES["candidates"][:12]:
        print(" ", r)
    print("bar quantiles:", json.dumps(RES["bar_quantiles"]))
    print("optimal vs cash:", json.dumps(RES["optimal_vs_cash"], default=float)[:3000])


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HERE)
    main()
