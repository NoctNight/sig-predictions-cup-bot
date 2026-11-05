"""Cross-race correlation model: fit from history, apply to the venue's 117 races, price the chamber markets, find arbs.

    python3 -m sigbot.correlation_model --fit        # fit from data/history/races.csv -> params/correlation_model.json
    python3 -m sigbot.correlation_model --report     # bound violations + chamber pricing + top relative-value pairs (no API reads)
    python3 -m sigbot.correlation_model --matrix     # also write data/correlation_matrix.csv (every pair of D legs)

Model
-----
For race i in year y, the Democratic margin (D% - R%) is

    margin_i = E_i + beta_office(i) * u_y + g_{division(i),y} + s_{state(i),y} + e_i

E_i is the pre-election expectation (final-21-day poll average where >= 1 poll, else a per-office fundamentals regression on
state lean, incumbency and the generic ballot), u_y the national swing (the common part of the polling / fundamentals miss),
g a Census-division shock, s a same-state shock shared by every race in that state that year, e idiosyncratic. The variance
components are estimated by moments on 2006-2024 residuals (year means -> u, division-year means -> g, products of residuals
of different races in the same state-year -> s, leftovers -> e). Chamber markets are priced by Monte Carlo of this model with
each seat's marginal probability taken from the fair values (or from the venue's own prices), so the only model input is the
correlation structure, not a view on any race.

Pairs: the latent correlation rho_ij between two races' margins turns each pair of binary legs into P(D_i and D_j) through
the bivariate normal; the implied binary correlation and the hedge ratio that neutralises u are reported per pair.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import sqlite3
import statistics
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(HERE, "data", "history")
PARAMS = os.path.join(HERE, "params", "correlation_model.json")
TICK = 0.005

STATE_NAME = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
              "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
              "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
              "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
              "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
              "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
              "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}
NAME_STATE = {v: k for k, v in STATE_NAME.items()}


# ============================================================ small numerics (stdlib only)
def phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def Phi(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def Phi_inv(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if Phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


_GL_X = [-0.9739065285, -0.8650633667, -0.6794095683, -0.4333953941, -0.1488743390, 0.1488743390, 0.4333953941, 0.6794095683, 0.8650633667, 0.9739065285]
_GL_W = [0.0666713443, 0.1494513492, 0.2190863625, 0.2692667193, 0.2955242247, 0.2955242247, 0.2692667193, 0.2190863625, 0.1494513492, 0.0666713443]


def bvn(a: float, b: float, rho: float) -> float:
    """P(X < a, Y < b) for standard bivariate normal with correlation rho (Drezner-Wesolowsky, 10-point Gauss-Legendre
    on the rho-integral; accurate to ~1e-6 for |rho| <= 0.95, which covers every pair here)."""
    rho = max(-0.999, min(0.999, rho))
    if abs(rho) < 1e-12:
        return Phi(a) * Phi(b)
    tot = 0.0
    for x, w in zip(_GL_X, _GL_W):
        r = 0.5 * rho * (x + 1)                 # r in (0, rho]
        tot += w * math.exp(-(a * a - 2 * r * a * b + b * b) / (2 * (1 - r * r))) / math.sqrt(1 - r * r)
    return Phi(a) * Phi(b) + 0.5 * rho * tot / (2 * math.pi)


def ols(X: list[list[float]], y: list[float]) -> list[float]:
    k = len(X[0])
    A = [[sum(X[n][i] * X[n][j] for n in range(len(X))) for j in range(k)] for i in range(k)]
    b = [sum(X[n][i] * y[n] for n in range(len(X))) for i in range(k)]
    for i in range(k):
        A[i][i] += 1e-9
    # gaussian elimination
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(k):
            if r != c and abs(M[c][c]) > 0:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * bb for a, bb in zip(M[r], M[c])]
    return [M[i][k] / M[i][i] if M[i][i] else 0.0 for i in range(k)]


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


# ============================================================ fit
def load_history(years=range(2006, 2025)) -> list[dict]:
    rows = []
    for r in csv.DictReader(open(os.path.join(HIST, "races.csv"), encoding="utf-8")):
        y = int(r["year"])
        if y not in years or r["contested"] != "1" or _f(r["margin"]) is None or _f(r["pvi"]) is None or _f(r["gb_poll"]) is None:
            continue
        inc = 0.0
        if r["inc_running"] == "1" and r["inc_party"] in ("D", "R"):
            inc = 1.0 if r["inc_party"] == "D" else -1.0
        held = 1.0 if r["inc_party"] == "D" else -1.0 if r["inc_party"] == "R" else 0.0
        rows.append({"year": y, "office": r["office"], "state": r["state_po"], "seat": r["seat"], "division": r["division"],
                     "margin": _f(r["margin"]), "pvi": _f(r["pvi"]), "inc": inc, "held": held, "gb": _f(r["gb_poll"]),
                     "poll": _f(r["poll_margin"]), "n_polls": int(r["n_polls"] or 0), "pres_year": r["pres_year"] == "1"})
    return rows


def fundamentals_fit(rows: list[dict]) -> dict:
    """Per office: margin ~ 1 + pvi + inc + held_open + gb (+ pres_year). Leave-one-year-out residuals for honesty."""
    out = {}
    for office in ("senate", "governor", "house"):
        sub = [r for r in rows if r["office"] == office]
        if len(sub) < 30:
            continue
        feats = lambda r: [1.0, r["pvi"], r["inc"], r["held"] * (1.0 if r["inc"] == 0 else 0.0), r["gb"], 1.0 if r["pres_year"] else 0.0]
        coef = ols([feats(r) for r in sub], [r["margin"] for r in sub])
        # leave-one-year-out prediction
        years = sorted({r["year"] for r in sub})
        for y in years:
            train = [r for r in sub if r["year"] != y]
            c = ols([feats(r) for r in train], [r["margin"] for r in train])
            for r in sub:
                if r["year"] == y:
                    r["fund"] = sum(a * b for a, b in zip(c, feats(r)))
        res = [r["margin"] - r["fund"] for r in sub]
        out[office] = {"coef": dict(zip(["const", "pvi", "inc", "open_held", "gb", "pres_year"], [round(c, 3) for c in coef])),
                       "n": len(sub), "rmse_loyo": round(math.sqrt(sum(e * e for e in res) / len(res)), 2)}
    return out


def expectation(rows: list[dict], w_poll: float = 0.75) -> None:
    """E_i = poll-weighted blend where polled (weight rises with the poll count), else fundamentals."""
    for r in rows:
        if r.get("fund") is None:
            r["E"] = None
            continue
        if r["poll"] is not None and r["n_polls"] >= 1:
            w = w_poll * min(1.0, r["n_polls"] / 4.0) ** 0.5
            r["E"] = w * r["poll"] + (1 - w) * r["fund"]
            r["polled"] = True
        else:
            r["E"] = r["fund"]
            r["polled"] = False
        r["res"] = r["margin"] - r["E"]


def _var(xs):
    return statistics.pvariance(xs) if len(xs) > 1 else 0.0


COMP_CUT = 20.0   # a race is "competitive" when the pre-election expected margin is within 20 points; used for the per-office residual-width buckets


CORE_OFFICES = ("senate", "governor")   # the common structure is estimated on statewide races; House rows (fundamentals RMSE 18, redistricting
                                         # years) would otherwise dominate the year means and read model misfit as swing. House gets its own
                                         # loading on that factor and its own residual widths.


def variance_components(rows: list[dict], comp_cut: float = COMP_CUT) -> dict:
    all_rows = [r for r in rows if r.get("res") is not None]
    comp_rows = [r for r in all_rows if abs(r["E"]) <= comp_cut]
    rows = [r for r in all_rows if r["office"] in CORE_OFFICES]     # every contested statewide race: ~60 per year keeps the year means stable
    years = sorted({r["year"] for r in rows})
    offices = CORE_OFFICES
    # 1. national factor: race-weighted year mean of residuals; leave-office-out version for the office loadings
    by_year = defaultdict(list)
    by_year_off = defaultdict(list)
    for r in rows:
        by_year[r["year"]].append(r["res"])
        by_year_off[(r["year"], r["office"])].append(r["res"])
    u = {y: statistics.mean(by_year[y]) for y in years}
    noise_u = statistics.mean(_var(by_year[y]) / len(by_year[y]) for y in years)
    var_u = max(_var(list(u.values())) - noise_u, 0.25)
    betas = {}
    for o in offices:
        pts = []
        for y in years:
            own = by_year_off.get((y, o))
            others = [x for oo in offices if oo != o for x in by_year_off.get((y, oo), [])]
            if own and len(others) >= 10:
                pts.append((statistics.mean(others), statistics.mean(own)))
        if len(pts) >= 4:
            mx = statistics.mean(p[0] for p in pts)
            my = statistics.mean(p[1] for p in pts)
            sxx = sum((p[0] - mx) ** 2 for p in pts)
            sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
            betas[o] = max(0.0, sxy / sxx) if sxx > 0 else 1.0
        else:
            betas[o] = 1.0
    # normalise so the race-weighted mean loading is 1 (u keeps the pooled scale)
    n_off = {o: sum(1 for r in rows if r["office"] == o) for o in offices}
    mean_beta = sum(betas[o] * n_off[o] for o in offices) / max(1, sum(n_off.values()))
    betas = {o: round(b / mean_beta, 3) for o, b in betas.items()}
    u = {y: v / mean_beta for y, v in u.items()}
    var_u = var_u / (mean_beta ** 2)
    # House loading: slope of its competitive-polled year means on u (House is not part of u, so no leave-out needed)
    hp = defaultdict(list)
    for r in comp_rows:
        if r["office"] == "house" and r.get("polled"):
            hp[r["year"]].append(r["res"])
    pts = [(u[y], statistics.mean(hp[y])) for y in years if len(hp.get(y, [])) >= 10]
    if len(pts) >= 4:
        mx, my = statistics.mean(p[0] for p in pts), statistics.mean(p[1] for p in pts)
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
        betas["house"] = round(max(0.5, min(1.5, sxy / sxx)), 3) if sxx > 0 else 1.0
    else:
        betas["house"] = 1.0
    for r in all_rows:
        r["r1"] = r["res"] - betas[r["office"]] * u.get(r["year"], 0.0)
    # 2. division-year shocks
    by_div = defaultdict(list)
    for r in rows:
        by_div[(r["year"], r["division"])].append(r["r1"])
    cells = [v for v in by_div.values() if len(v) >= 3]
    var_div = max(0.0, _var([statistics.mean(v) for v in cells]) - statistics.mean(_var(v) / len(v) for v in cells))
    div_mean = {k: statistics.mean(v) for k, v in by_div.items()}
    for r in rows:
        k = (r["year"], r["division"])
        n = len(by_div[k])
        shrink = var_div / (var_div + _var(by_div[k]) / n) if n > 1 and var_div > 0 else 0.0
        r["r2"] = r["r1"] - shrink * div_mean[k]
    # 3. same-state same-year shock: mean product of residuals of different races in one state-year
    by_state = defaultdict(list)
    for r in rows:
        by_state[(r["year"], r["state"])].append(r)
    prods, prods_so = [], []
    for v in by_state.values():
        for i in range(len(v)):
            for j in range(i + 1, len(v)):
                prods.append(v[i]["r2"] * v[j]["r2"])
                if {v[i]["office"], v[j]["office"]} == {"senate", "governor"}:
                    prods_so.append(v[i]["r2"] * v[j]["r2"])
    var_state = max(0.0, statistics.mean(prods)) if prods else 0.0
    var_state_sg = statistics.mean(prods_so) if prods_so else None
    # 4. idiosyncratic by office and polled flag
    idio = {}
    for o in ("senate", "governor", "house"):
        for label, sub in (("competitive", [r for r in comp_rows if r["office"] == o]),
                           ("competitive_polled", [r for r in comp_rows if r["office"] == o and r.get("polled")]),
                           ("competitive_polled3", [r for r in comp_rows if r["office"] == o and r["n_polls"] >= 3]),
                           ("safe", [r for r in all_rows if r["office"] == o and abs(r["E"]) > comp_cut])):
            if len(sub) >= 15:
                tot = _var([r["res"] for r in sub])
                idio[f"{o}:{label}"] = {
                    "n": len(sub), "sd_total": round(math.sqrt(tot), 2),
                    "sd_idio": round(math.sqrt(max(2.5 ** 2, tot - betas[o] ** 2 * var_u - var_div - var_state)), 2),
                    "mean_res": round(statistics.mean(r["res"] for r in sub), 2)}
    # 5. how much of a typical pair's covariance is the national swing
    share = {}
    for o in ("senate", "governor", "house"):
        k = f"{o}:competitive_polled3" if (idio.get(f"{o}:competitive_polled3") or {"n": 0})["n"] >= 80 else f"{o}:competitive_polled"
        if k in idio:
            tot = idio[k]["sd_total"] ** 2
            tot = max(tot, betas[o] ** 2 * var_u + var_div + var_state + 2.5 ** 2)
            share[o] = {"national": round(betas[o] ** 2 * var_u / tot, 3), "division": round(var_div / tot, 3),
                        "state": round(var_state / tot, 3), "idiosyncratic": round(1 - (betas[o] ** 2 * var_u + var_div + var_state) / tot, 3), "bucket": k}
    # 6. generic-ballot error vs the national factor (how much of u is the generic-ballot miss)
    gbe = {}
    for r in csv.DictReader(open(os.path.join(HIST, "national_environment.csv"))):
        if r["gb_error"]:
            gbe[int(r["year"])] = float(r["gb_error"])
    pts = [(gbe[y], u[y]) for y in years if y in gbe]
    gb_slope = gb_corr = None
    if len(pts) >= 4:
        mx, my = statistics.mean(p[0] for p in pts), statistics.mean(p[1] for p in pts)
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        syy = sum((p[1] - my) ** 2 for p in pts)
        sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
        gb_slope, gb_corr = round(sxy / sxx, 3), round(sxy / math.sqrt(sxx * syy), 3)
    return {"var_u": round(var_u, 3), "sd_u": round(math.sqrt(var_u), 2), "u_by_year": {y: round(u[y], 2) for y in years}, "n_competitive": len(rows), "comp_cut": comp_cut,
            "beta": betas, "var_division": round(var_div, 3), "var_state": round(var_state, 3),
            "var_state_senate_gov_only": None if var_state_sg is None else round(var_state_sg, 3), "n_state_pairs": len(prods),
            "idiosyncratic": idio, "variance_share_polled": share,
            "gb_error_by_year": gbe, "u_on_gb_error_slope": gb_slope, "u_gb_error_corr": gb_corr}


def fit(years=range(2006, 2025), write: bool = True) -> dict:
    rows = load_history(years)
    fund = fundamentals_fit(rows)
    expectation(rows)
    vc = variance_components(rows)
    # within-year residual correlation check: average pairwise correlation implied vs empirical year-effect spread
    model = {"built_at": time.time(), "years": [min(years), max(years)], "n_races": len(rows), "fundamentals": fund, **vc,
             "notes": ["margin = D% - R% of total vote; E = 0.75*sqrt(min(1,n/4)) poll blend where polled else fundamentals (LOYO)",
                       "var_u is the variance of the race-weighted year mean of residuals net of sampling noise; betas are leave-office-out slopes, normalised to mean 1",
                       "var_division from division-year means net of noise; var_state from products of residuals of different races in one state-year"]}
    if write:
        json.dump(model, open(PARAMS, "w"), indent=1)
    return model


# ============================================================ apply to the 2026 universe
def load_model() -> dict:
    return json.load(open(PARAMS))


def race_state(name: str) -> str | None:
    m = re.match(r"([A-Z]{2})-\d+ House race", name)
    if m:
        return m.group(1)
    for suffix in (" Senate", " Governor"):
        if name.endswith(suffix):
            return NAME_STATE.get(name[: -len(suffix)])
    return None


def race_office(name: str) -> str:
    if name in ("U.S. Senate", "U.S. House"):
        return "chamber"
    if "House race" in name:
        return "house"
    return "senate" if name.endswith("Senate") else "governor"


def division_of(state: str) -> str:
    from .history_build import STATE_DIV
    return STATE_DIV.get(state, "")


def race_sigma(model: dict, office: str, competitive: bool) -> float:
    """Total latent sd for a race: competitive races (expected margin within 20 points, which is every polled or rated race) use the
    competitive bucket, safe seats the safe bucket (wide, but they sit 2+ sd from the threshold so it hardly matters)."""
    I = model["idiosyncratic"]
    p3 = I.get(f"{office}:competitive_polled3")
    idio = (I.get(f"{office}:safe") if not competitive else None) or (p3 if p3 and p3["n"] >= 80 else None) or I.get(f"{office}:competitive_polled") or I.get(f"{office}:competitive") or {"sd_idio": 6.0}
    b = model["beta"].get(office, 1.0)
    return math.sqrt(b * b * model["var_u"] + model["var_division"] + model["var_state"] + idio["sd_idio"] ** 2)


def latent_cov(model: dict, a: dict, b: dict) -> float:
    c = model["beta"][a["office"]] * model["beta"][b["office"]] * model["var_u"]
    if a["division"] and a["division"] == b["division"]:
        c += model["var_division"]
    if a["state"] and a["state"] == b["state"]:
        c += model["var_state"]
    return c


def load_universe() -> dict:
    """race -> {office, state, division, polled, legs{D,R,I: exchange_id}, fair (D), fair_src, conf, outside, venue bid/ask/mid per leg}"""
    markets = json.load(open(os.path.join(HERE, "data", "markets.json")))
    fm = json.load(open(os.path.join(HERE, "params", "fair_model.json")))["races"]
    fe = json.load(open(os.path.join(HERE, "params", "fair_explained.json")))["legs"]
    U = {}
    for m in markets:
        mm = re.match(r"Will the (Democratic|Republican|Independent) Party win the (.+)\?", m["title"])
        if not mm:
            continue
        party, race = mm.group(1)[0], mm.group(2)
        u = U.setdefault(race, {"race": race, "office": race_office(race), "state": race_state(race), "legs": {}, "venue": {}})
        u["legs"][party] = m["exchanges"][0]["id"]
        u["venue"][party] = {"last": m["exchanges"][0].get("latestPrice")}
    for race, u in U.items():
        u["division"] = division_of(u["state"]) if u["state"] else ""
        info = fm.get(race, {})
        u["n_polls"] = info.get("inputs", {}).get("n_polls") or 0
        u["model_fair_d"] = (info.get("legs", {}).get("D") or {}).get("fair")
        u["sigma_model"] = info.get("sigma")
        leg = fe.get(f"{race} [D]")
        legr = fe.get(f"{race} [R]")
        if leg:
            u["fair_d"], u["fair_src"], u["conf"], u["outside_d"] = leg["believed_fair"], "explained", leg.get("confidence"), leg.get("outside_blend")
            u["verdict_d"] = leg.get("verdict")
        elif legr:
            u["fair_d"], u["fair_src"], u["conf"], u["outside_d"] = 1 - legr["believed_fair"], "explained(R)", legr.get("confidence"), None if legr.get("outside_blend") is None else 1 - legr["outside_blend"]
            u["verdict_d"] = legr.get("verdict")
        else:
            u["fair_d"], u["fair_src"], u["conf"], u["outside_d"] = u["model_fair_d"], "model", 0.3, None
            u["verdict_d"] = None
        u["polled"] = u["n_polls"] >= 1 or (u["fair_d"] is not None and 0.05 < u["fair_d"] < 0.95)   # "competitive" flag for race_sigma
    return U


def load_venue_prices(U: dict, db: str | None = None, max_age_s: float = 3 * 3600) -> float:
    """Latest SIG bid/ask per exchange from the bot's own log (read-only). Returns the age of the newest row in minutes."""
    db = db or os.path.join(HERE, "data", "sigbot.sqlite")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    newest = 0.0
    px = {}
    since = time.time() - max_age_s
    # one consistent poll first (so set/bound arithmetic compares legs quoted at the same instant), then fill gaps per instrument
    for ex, bid, ask, ts in con.execute("SELECT instrument, bid, ask, ts FROM venue_quotes WHERE venue='sig' AND ts = (SELECT MAX(ts) FROM venue_quotes WHERE venue='sig')"):
        px[str(ex)] = (bid, ask, ts)
        newest = max(newest, ts)
    for ex, bid, ask, ts in con.execute("SELECT instrument, bid, ask, MAX(ts) FROM venue_quotes WHERE venue='sig' AND ts >= ? GROUP BY instrument", (since,)):
        if str(ex) not in px:
            px[str(ex)] = (bid, ask, ts)
    for ex, bid, ask, ts in con.execute("SELECT exchange_id, best_bid, best_ask, ts FROM quotes WHERE ts >= (SELECT MAX(ts) FROM quotes) - 120"):
        if str(ex) not in px:
            px[str(ex)] = (bid, ask, ts)
    try:
        for race, party, pm, k, fair, ts in con.execute("SELECT race, party, pm, kalshi, fair, ts FROM venue_fair WHERE ts = (SELECT MAX(ts) FROM venue_fair)"):
            if race in U and party == "D":
                U[race]["outside_now"] = fair
                U[race]["pm"], U[race]["kalshi"] = pm, k
    except sqlite3.Error:
        pass
    con.close()
    for u in U.values():
        for party, ex in u["legs"].items():
            v = u["venue"].setdefault(party, {})
            if ex in px and time.time() - px[ex][2] <= max_age_s:
                v["bid"], v["ask"], v["ts"] = px[ex][0], px[ex][1], px[ex][2]
                if v["bid"] is not None and v["ask"] is not None:
                    v["mid"] = 0.5 * (v["bid"] + v["ask"])
            elif v.get("last") is not None:
                v["mid"] = v["last"]
    return (time.time() - newest) / 60 if newest else float("inf")


def venue_d_mid(u: dict) -> float | None:
    d = u["venue"].get("D", {}).get("mid")
    r = u["venue"].get("R", {}).get("mid")
    if d is not None and r is not None:
        return 0.5 * (d + 1 - r)
    return d if d is not None else (1 - r if r is not None else None)


# ---------------------------------------------------------------- pairwise structure
def pair_stats(model: dict, a: dict, b: dict, pa: float, pb: float) -> dict:
    """Latent rho, P(both D), binary correlation, and swing sensitivities for two races with marginals pa, pb."""
    sa, sb = race_sigma(model, a["office"], a["polled"]), race_sigma(model, b["office"], b["polled"])
    rho = latent_cov(model, a, b) / (sa * sb)
    za, zb = Phi_inv(pa), Phi_inv(pb)
    both = bvn(za, zb, rho)   # P(margin_a<0 ... ) symmetric: P(D_a, D_b) with D = latent > -z... use symmetry
    # D wins when latent margin > 0, i.e. standard normal X > -z; P(X_a > -za, X_b > -zb) = P(X_a < za, X_b < zb)
    cov = both - pa * pb
    corr = cov / math.sqrt(pa * (1 - pa) * pb * (1 - pb)) if 0 < pa < 1 and 0 < pb < 1 else 0.0
    swing_a = phi(za) * model["beta"][a["office"]] / sa    # dP/du per point of national swing
    swing_b = phi(zb) * model["beta"][b["office"]] / sb
    return {"rho": rho, "p_both": both, "cov": cov, "corr": corr, "swing_a": swing_a, "swing_b": swing_b}


def correlation_matrix(model: dict, U: dict, path: str) -> int:
    races = [u for u in U.values() if u["office"] != "chamber" and u.get("fair_d") is not None]
    n = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["race_a", "race_b", "p_a", "p_b", "rho_latent", "corr_binary", "p_both_d", "same_state", "same_division"])
        for i in range(len(races)):
            for j in range(i + 1, len(races)):
                a, b = races[i], races[j]
                s = pair_stats(model, a, b, a["fair_d"], b["fair_d"])
                w.writerow([a["race"], b["race"], round(a["fair_d"], 4), round(b["fair_d"], 4), round(s["rho"], 4), round(s["corr"], 4),
                            round(s["p_both"], 4), int(a["state"] == b["state"]), int(a["division"] == b["division"])])
                n += 1
    return n


# ---------------------------------------------------------------- chamber pricing
SENATE_NOT_UP_D = 34       # Democratic caucus seats not up in 2026 (32 D + 2 I); 47 held, 13 up
SENATE_NEED_D = 51         # VP Vance (R) breaks ties
HOUSE_NEED_D = 218
OHIO_SENATE_D = 0.42       # special election not listed on the venue (Brown vs Husted); outside markets ~0.40-0.45 in Oct 2026


def house_seats(U: dict, model: dict) -> list[dict]:
    """All 435 districts: venue races use their fair, rated seats the rating margin (sigma 8), the rest the incumbent party."""
    from . import fairmodel as fmod
    w_c = json.load(open(os.path.join(HERE, "data", "fair", "wiki_2026_United_States_House_of_Representatives_elections.json")))
    w_r = json.load(open(os.path.join(HERE, "data", "fair", "wiki_2026_United_States_House_of_Representatives_election_ratings.json")))
    cands = fmod.parse_house_candidates(w_c)
    ratings = fmod.parse_pred_table(w_r, r"!\{\{ushr\|([A-Z]{2}\|\d+)\|X\}\}")
    venue = {f"{u['state']}-{int(u['race'].split('-')[1].split()[0]):02d}": u for u in U.values() if u["office"] == "house"}
    seats = []
    for key, info in cands.items():
        st, dd = key.split("-")
        dist = f"{st}-{int(dd):02d}"
        src = "incumbent"
        if dist in venue and venue[dist].get("fair_d") is not None:
            p, src = venue[dist]["fair_d"], "venue-fair"
        else:
            rk = f"{st}|{int(dd)}"
            rm = fmod.rating_margin(ratings[rk]["ratings"]) if rk in ratings else None
            if rm is not None:
                p, src = Phi(rm / 8.0), "rating"
            elif info.get("inc_party") == "D":
                p = 0.985
            elif info.get("inc_party") == "R":
                p = 0.015
            elif info.get("pvi") is not None:
                p, src = Phi(info["pvi"] / 8.0), "pvi"
            else:
                p, src = 0.5, "unknown"
        seats.append({"race": dist, "office": "house", "state": st, "division": division_of(st), "polled": src != "incumbent", "p": p, "src": src})
    return seats


def simulate_count(model: dict, seats: list[dict], n: int = 10000, seed: int = 7, u_scale: float = 1.0) -> list[int]:
    """Monte Carlo of D wins under the factor model with each seat's marginal p preserved exactly (u_scale rescales sd_u
    for sensitivity; marginals are then no longer exactly preserved). Common shocks are drawn, the idiosyncratic part is
    integrated out per seat (win iff uniform < Phi((mu + common)/sd_idio)), which is ~5x faster than drawing it."""
    rng = random.Random(seed)
    sd_u, sd_div, sd_st = math.sqrt(model["var_u"]) * u_scale, math.sqrt(model["var_division"]), math.sqrt(model["var_state"])
    divs = sorted({s["division"] for s in seats})
    sts = sorted({s["state"] for s in seats})
    di, si = {d: i for i, d in enumerate(divs)}, {s: i for i, s in enumerate(sts)}
    pre = []
    for s in seats:
        sig = race_sigma(model, s["office"], s["polled"])
        idio = math.sqrt(max(1.0, sig * sig - (model["beta"][s["office"]] ** 2) * model["var_u"] - model["var_division"] - model["var_state"]))
        pre.append((sig * Phi_inv(s["p"]) / idio, model["beta"][s["office"]] / idio, di[s["division"]], si[s["state"]], 1.0 / idio))
    counts = []
    erfc, sq2, rnd, gauss = math.erfc, math.sqrt(2), rng.random, rng.gauss
    for _ in range(n):
        u = gauss(0, sd_u)
        g = [gauss(0, sd_div) for _ in divs]
        st = [gauss(0, sd_st) for _ in sts]
        c = 0
        for mu, bu, d, s, inv in pre:
            t = mu + bu * u + (g[d] + st[s]) * inv
            if rnd() < 0.5 * erfc(-t / sq2):
                c += 1
        counts.append(c)
    return counts


def price_chambers(model: dict, U: dict, marginals: str = "fair", n: int = 10000, u_scale: float = 1.0) -> dict:
    """P(D controls Senate / House) under the factor model; marginals = 'fair' (believed fair) or 'venue' (SIG D mids)."""
    def p_of(u):
        if marginals == "venue":
            return venue_d_mid(u)
        return u.get("fair_d")
    sen = []
    for u in U.values():
        if u["office"] == "senate" and p_of(u) is not None:
            sen.append({"race": u["race"], "office": "senate", "state": u["state"], "division": u["division"], "polled": u["polled"], "p": p_of(u)})
    sen.append({"race": "Ohio Senate (not on venue)", "office": "senate", "state": "OH", "division": division_of("OH"), "polled": True, "p": OHIO_SENATE_D})
    cs = simulate_count(model, sen, n, u_scale=u_scale)
    need = SENATE_NEED_D - SENATE_NOT_UP_D
    p_sen = sum(1 for c in cs if c >= need) / len(cs)
    p_sen_indep = _indep_tail([s["p"] for s in sen], need)
    out = {"senate": {"races": len(sen), "need_wins": need, "expected_wins": round(sum(s["p"] for s in sen), 2), "p_d_model": round(p_sen, 4),
                      "p_d_independent": round(p_sen_indep, 4), "mc_se": round(math.sqrt(p_sen * (1 - p_sen) / len(cs)), 4),
                      "wins_p10_p50_p90": [sorted(cs)[int(len(cs) * q)] for q in (0.1, 0.5, 0.9)]}}
    try:
        hs = house_seats(U, model)
        if marginals == "venue":
            for s in hs:
                dist = s["race"]
                for u in U.values():
                    if u["office"] == "house" and f"{u['state']}-{int(u['race'].split('-')[1].split()[0]):02d}" == dist and venue_d_mid(u) is not None:
                        s["p"] = venue_d_mid(u)
        ch = simulate_count(model, hs, max(2000, n // 3), u_scale=u_scale)
        p_h = sum(1 for c in ch if c >= HOUSE_NEED_D) / len(ch)
        src = defaultdict(int)
        for s in hs:
            src[s["src"]] += 1
        out["house"] = {"seats": len(hs), "need_wins": HOUSE_NEED_D, "expected_wins": round(sum(s["p"] for s in hs), 1), "p_d_model": round(p_h, 4),
                        "p_d_independent": round(_indep_tail([s["p"] for s in hs], HOUSE_NEED_D), 4), "mc_se": round(math.sqrt(p_h * (1 - p_h) / len(ch)), 4),
                        "wins_p10_p50_p90": [sorted(ch)[int(len(ch) * q)] for q in (0.1, 0.5, 0.9)], "seat_sources": dict(src)}
    except Exception as e:  # noqa: BLE001
        out["house"] = {"error": repr(e)}
    return out


def _indep_tail(ps: list[float], k: int) -> float:
    dist = [1.0]
    for p in ps:
        nd = [0.0] * (len(dist) + 1)
        for i, v in enumerate(dist):
            nd[i] += v * (1 - p)
            nd[i + 1] += v * p
        dist = nd
    return sum(dist[k:])


# ---------------------------------------------------------------- strict bounds (riskless regardless of the model)
def chamber_bounds(U: dict) -> list[dict]:
    """Boole/Frechet bounds between the Senate-control legs and the 34 listed Senate seats, at the venue's touches.
    Each row is a portfolio with a payoff >= 0 in every outcome; `edge` > 0 would be a riskless profit per unit."""
    out = []
    sen = [u for u in U.values() if u["office"] == "senate"]
    ch = U.get("U.S. Senate")
    if not ch or not sen:
        return out
    need = SENATE_NEED_D - SENATE_NOT_UP_D          # 17 of 35 (Ohio unlisted: bounds below ignore it, which makes them looser, still valid)
    n_listed = len(sen)
    def bid(u, p):
        return u["venue"].get(p, {}).get("bid")
    def ask(u, p):
        return u["venue"].get(p, {}).get("ask")
    d_asks = sorted([ask(u, "D") for u in sen if ask(u, "D") is not None])
    d_bids = sorted([bid(u, "D") for u in sen if bid(u, "D") is not None], reverse=True)
    r_asks = sorted([ask(u, "R") for u in sen if ask(u, "R") is not None])
    cD_bid, cD_ask = bid(ch, "D"), ask(ch, "D")
    cR_bid, cR_ask = bid(ch, "R"), ask(ch, "R")
    if cD_bid is not None and len(d_asks) >= need:
        # 1{X>=k} <= (1/k) sum D_i : sell chamber D at bid, buy 1/k of every listed D at ask (listed sum <= full sum, still valid upper bound only if
        # all 35 are listed; with Ohio missing use the k cheapest of the listed plus Ohio counted at 1 -> conservative: add 1/k)
        cost = sum(d_asks) / need + (1.0 / need if n_listed < 35 else 0.0)
        out.append({"bound": "chamberD <= sum(D_i)/k", "portfolio": f"sell U.S. Senate D @ {cD_bid:.3f}, buy 1/{need} of each D leg at ask", "edge": round(cD_bid - cost, 4), "riskless": True})
    if cD_ask is not None and len(d_bids) >= need:
        # 1{X>=k} >= 1 - sum_{S,|S|=k} (1 - D_i): buy chamber D at ask + buy NO on the k most likely D seats; payoff >= 1 always
        cost = cD_ask + sum(1 - b for b in d_bids[:need])
        out.append({"bound": "chamberD >= 1 - sum_S (1-D_i)", "portfolio": f"buy U.S. Senate D @ {cD_ask:.3f} + NO on the {need} strongest D seats", "edge": round(1 - cost, 4), "riskless": True})
    if cD_bid is not None and len(d_asks) >= n_listed - need + 1:
        # X>=k implies at least one D win among any n-k+1 seats: chamberD <= sum_{T} D_i, |T| = n-k+1 (use the 19 cheapest listed D asks; Ohio unlisted
        # means T must have 19 of the 35, so pick 19 listed: valid)
        m = 35 - need + 1
        cost = sum(d_asks[:m])
        out.append({"bound": "chamberD <= sum_T D_i, |T|=n-k+1", "portfolio": f"sell U.S. Senate D @ {cD_bid:.3f}, buy the {m} cheapest D legs at ask", "edge": round(cD_bid - cost, 4), "riskless": True})
    if cR_bid is not None and len(r_asks) >= need:
        # R controls iff D wins <= 16 iff R wins >= 19 of 35: chamberR <= (1/19) sum R_i
        kr = 35 - need + 1
        cost = sum(r_asks) / kr + (1.0 / kr if n_listed < 35 else 0.0)
        out.append({"bound": "chamberR <= sum(R_i)/(n-k+1)", "portfolio": f"sell U.S. Senate R @ {cR_bid:.3f}, buy 1/{kr} of each R leg at ask", "edge": round(cR_bid - cost, 4), "riskless": True})
    # set arbs inside every race (the live arb sleeve's own bounds), reported for completeness
    return out


def set_arbs(U: dict, x: dict | None = None) -> list[dict]:
    out = []
    for race, u in U.items():
        bids = [u["venue"].get(p, {}).get("bid") for p in u["legs"]]
        asks = [u["venue"].get(p, {}).get("ask") for p in u["legs"]]
        tss = [u["venue"].get(p, {}).get("ts") for p in u["legs"]]
        if any(b is None for b in bids) or any(a is None for a in asks) or any(t is None for t in tss):
            continue
        if max(tss) - min(tss) > 120:        # legs quoted at different times: a stale sum is not an arb
            continue
        xr = (x or {}).get(race, 0.003)
        if sum(bids) > 1.0 + 1e-9:
            out.append({"race": race, "kind": "sell-set", "edge": round(sum(bids) - 1.0, 4), "riskless": True})
        if sum(asks) < 1.0 - xr - 1e-9:
            out.append({"race": race, "kind": "buy-set", "edge": round(1.0 - xr - sum(asks), 4), "riskless": True, "x": xr})
    return out


# ---------------------------------------------------------------- relative value
def relative_value(model: dict, U: dict, min_conf: float = 0.5, min_gap: float = 0.02, max_pairs: int = 25, cross_office_only: bool = False, competitive: bool = False) -> list[dict]:
    """Pairs (long D in a, short D in b) whose venue mispricings have opposite signs; hedge ratio neutralises the national swing.
    Mispricing m = fair_d - venue_d_mid. EV per share of a (held to settlement) = m_a + h*(-m_b). Residual sd from the model's
    pairwise binary covariance. Entry at the touch is reported separately (crossing the spread on both legs)."""
    legs = []
    for u in U.values():
        if u["office"] == "chamber" or u.get("fair_d") is None or (u.get("conf") or 0) < min_conf:
            continue
        vm = venue_d_mid(u)
        if vm is None or (competitive and not 0.15 < u["fair_d"] < 0.85):
            continue
        legs.append((u, u["fair_d"] - vm, vm))
    longs = [(u, m, vm) for u, m, vm in legs if m >= min_gap]
    shorts = [(u, m, vm) for u, m, vm in legs if m <= -min_gap]
    pairs = []
    for ua, ma, va in longs:
        for ub, mb, vb in shorts:
            if ua["race"] == ub["race"]:
                continue
            if cross_office_only and ua["state"] != ub["state"]:
                continue
            s = pair_stats(model, ua, ub, ua["fair_d"], ub["fair_d"])
            if s["swing_b"] <= 1e-6:
                continue
            h = s["swing_a"] / s["swing_b"]          # shares of b per share of a so that d(P&L)/du = 0
            h = max(0.1, min(10.0, h))
            ev = ma + h * (-mb)
            var = ua["fair_d"] * (1 - ua["fair_d"]) + h * h * ub["fair_d"] * (1 - ub["fair_d"]) - 2 * h * s["cov"]
            sd = math.sqrt(max(var, 1e-9))
            # execution at the touch: long D_a = buy D YES at ask (or sell R at bid); short D_b = buy R YES at ask / sell D at bid
            a_ask = ua["venue"].get("D", {}).get("ask")
            b_r_ask = ub["venue"].get("R", {}).get("ask")
            touch_cost = 0.0
            if a_ask is not None:
                touch_cost += a_ask - va
            if b_r_ask is not None:
                touch_cost += h * (b_r_ask - (1 - vb))
            pairs.append({"long_d": ua["race"], "short_d": ub["race"], "p_long": round(ua["fair_d"], 3), "venue_long": round(va, 3), "m_long": round(ma, 3),
                          "p_short": round(ub["fair_d"], 3), "venue_short": round(vb, 3), "m_short": round(mb, 3), "hedge_ratio": round(h, 2),
                          "rho_latent": round(s["rho"], 3), "corr_binary": round(s["corr"], 3), "ev_per_share_mid": round(ev, 4),
                          "ev_per_share_touch": round(ev - touch_cost, 4), "residual_sd": round(sd, 3), "ev_over_sd": round(ev / sd, 3),
                          "ev_over_sd_touch": round((ev - touch_cost) / sd, 3), "same_state": ua["state"] == ub["state"],
                          "conf": round(min(ua.get("conf") or 0, ub.get("conf") or 0), 2), "riskless": False})
    pairs.sort(key=lambda p: -p["ev_over_sd_touch"])
    return pairs[:max_pairs]


def chamber_relative_value(model: dict, U: dict, pricing_fair: dict, pricing_venue: dict) -> list[dict]:
    out = []
    for name, key in (("U.S. Senate", "senate"), ("U.S. House", "house")):
        u = U.get(name)
        if not u or "p_d_model" not in pricing_fair.get(key, {}):
            continue
        vm = venue_d_mid(u)
        pf = pricing_fair[key]["p_d_model"]
        pv = pricing_venue.get(key, {}).get("p_d_model")
        out.append({"market": name, "venue_d_mid": vm, "model_from_fair_marginals": pf, "model_from_venue_marginals": pv,
                    "independent_from_fair": pricing_fair[key]["p_d_independent"], "gap_vs_fair_marginals": None if vm is None else round(pf - vm, 4),
                    "gap_vs_venue_marginals": None if (vm is None or pv is None) else round(pv - vm, 4), "riskless": False,
                    "note": "a gap vs venue-marginals means the chamber price is inconsistent with the venue's own seat prices under the model; a gap vs fair-marginals is the usual tail tilt"})
    return out


# ---------------------------------------------------------------- report
def report(write_matrix: bool = False, n_sim: int = 10000) -> dict:
    model = load_model()
    U = load_universe()
    age = load_venue_prices(U)
    x = {}
    try:
        x = {k: v["x"] for k, v in json.load(open(os.path.join(HERE, "params", "x_live.json")))["by_race"].items()}
    except Exception:  # noqa: BLE001
        pass
    lines = []
    lines.append(f"correlation model fit {model['years']} on {model['n_races']} races ({model.get('n_competitive')} competitive statewide for the factor structure): sd_u {model['sd_u']} pts, beta {model['beta']}, "
                 f"sd_division {math.sqrt(model['var_division']):.2f}, sd_state {math.sqrt(model['var_state']):.2f}; venue prices {age:.0f} min old")
    for k, v in model["variance_share_polled"].items():
        lines.append(f"  variance share ({k}, {v.get('bucket', '')}): national {v['national']:.0%} division {v['division']:.0%} state {v['state']:.0%} idiosyncratic {v['idiosyncratic']:.0%}")
    sa = set_arbs(U, x)
    cb = chamber_bounds(U)
    lines.append(f"strict set arbs at the touch now: {len(sa)}" + ("" if not sa else "  " + ", ".join(f"{a['race']} {a['kind']} {a['edge']:+.3f}" for a in sa[:8])))
    lines.append("chamber bounds (riskless portfolios; edge > 0 would be an arb):")
    for b in cb:
        lines.append(f"  {b['bound']:36s} edge {b['edge']:+.3f}   {b['portfolio']}")
    pf = price_chambers(model, U, "fair", n_sim)
    pv = price_chambers(model, U, "venue", n_sim)
    crv = chamber_relative_value(model, U, pf, pv)
    for c in crv:
        lines.append(f"{c['market']}: venue D {c['venue_d_mid']}, model P(D) from fair marginals {c['model_from_fair_marginals']} (independent {c['independent_from_fair']}), "
                     f"from the venue's own seat prices {c['model_from_venue_marginals']}")
    lines.append(f"  senate: need {pf['senate']['need_wins']} of 35, expected wins {pf['senate']['expected_wins']} (fair) / {pv['senate']['expected_wins']} (venue), "
                 f"wins p10/p50/p90 {pf['senate']['wins_p10_p50_p90']}, MC se {pf['senate']['mc_se']}")
    if "p_d_model" in pf.get("house", {}):
        lines.append(f"  house: 435 seats from {pf['house']['seat_sources']}, expected D wins {pf['house']['expected_wins']} (fair), p10/p50/p90 {pf['house']['wins_p10_p50_p90']}")
    rv = relative_value(model, U)
    lines.append("top relative-value pairs (long D in a / short D in b, h = shares of b per share of a; EV per share of a to settlement; model-dependent):")
    lines.append(f"  {'long D':26s} {'short D':26s} {'pL':>5s} {'vL':>5s} {'pS':>5s} {'vS':>5s} {'h':>5s} {'rho':>5s} {'EVmid':>6s} {'EVtch':>6s} {'sd':>5s} {'EV/sd':>6s}")
    for p in rv[:15]:
        lines.append(f"  {p['long_d'][:26]:26s} {p['short_d'][:26]:26s} {p['p_long']:5.3f} {p['venue_long']:5.3f} {p['p_short']:5.3f} {p['venue_short']:5.3f} "
                     f"{p['hedge_ratio']:5.2f} {p['rho_latent']:5.2f} {p['ev_per_share_mid']:+6.3f} {p['ev_per_share_touch']:+6.3f} {p['residual_sd']:5.3f} {p['ev_over_sd_touch']:6.2f}")
    rv_comp = relative_value(model, U, competitive=True, max_pairs=10)
    lines.append("competitive-race pairs only (0.15 < fair < 0.85 on both legs; where the correlation actually matters):")
    for p in rv_comp[:8]:
        lines.append(f"  {p['long_d'][:26]:26s} {p['short_d'][:26]:26s} {p['p_long']:5.3f} {p['venue_long']:5.3f} {p['p_short']:5.3f} {p['venue_short']:5.3f} "
                     f"{p['hedge_ratio']:5.2f} {p['rho_latent']:5.2f} {p['ev_per_share_mid']:+6.3f} {p['ev_per_share_touch']:+6.3f} {p['residual_sd']:5.3f} {p['ev_over_sd_touch']:6.2f}")
    sens = {}
    for sc in (0.5, 1.5):
        ps = price_chambers(model, U, "fair", n_sim // 2, u_scale=sc)
        sens[sc] = {"senate": ps["senate"]["p_d_model"], "house": ps.get("house", {}).get("p_d_model")}
    lines.append(f"  sensitivity to the national-swing sd (x0.5 / x1.5): senate {sens[0.5]['senate']} / {sens[1.5]['senate']}, house {sens[0.5]['house']} / {sens[1.5]['house']}")
    rv_state = relative_value(model, U, cross_office_only=True, max_pairs=10)
    if rv_state:
        lines.append("same-state pairs (highest correlation):")
        for p in rv_state[:6]:
            lines.append(f"  {p['long_d'][:26]:26s} {p['short_d'][:26]:26s} h {p['hedge_ratio']:5.2f} rho {p['rho_latent']:5.2f} EVmid {p['ev_per_share_mid']:+.3f} EVtouch {p['ev_per_share_touch']:+.3f} sd {p['residual_sd']:.3f}")
    out = {"generated_at": time.time(), "price_age_min": age, "set_arbs": sa, "chamber_bounds": cb, "chamber_fair": pf, "chamber_venue": pv,
           "chamber_relative_value": crv, "relative_value": rv, "competitive_pairs": rv_comp, "same_state_pairs": rv_state, "sd_u_sensitivity": sens, "lines": lines}
    if write_matrix:
        n = correlation_matrix(model, U, os.path.join(HERE, "data", "correlation_matrix.csv"))
        lines.append(f"wrote data/correlation_matrix.csv ({n} pairs)")
    json.dump(out, open(os.path.join(HERE, "params", "correlation_report.json"), "w"), indent=1, default=str)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="cross-race correlation model (fit / report)")
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--matrix", action="store_true")
    ap.add_argument("--years", default="2006-2024")
    ap.add_argument("--sims", type=int, default=10000)
    a = ap.parse_args(argv)
    if a.fit or not os.path.exists(PARAMS):
        y0, y1 = (int(v) for v in a.years.split("-"))
        m = fit(range(y0, y1 + 1))
        print(json.dumps({k: m[k] for k in ("years", "n_races", "sd_u", "beta", "var_division", "var_state", "var_state_senate_gov_only", "n_state_pairs",
                                            "u_on_gb_error_slope", "u_gb_error_corr")}, indent=1))
        print(json.dumps(m["fundamentals"], indent=1))
        print(json.dumps(m["idiosyncratic"], indent=1))
        print(json.dumps(m["variance_share_polled"], indent=1))
        print("u by year:", m["u_by_year"])
    if a.report or a.matrix:
        out = report(a.matrix, a.sims)
        print("\n".join(out["lines"]))


if __name__ == "__main__":
    main()
