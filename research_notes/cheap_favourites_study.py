"""Cheap safe favourites on SIG: calibration of outside favourites, today's fast-count basket, correlated Monte Carlo.

Question (Sizhe, 24 Oct): SIG prices safe favourites below the Polymarket/Kalshi fair on ~80 legs. Should we put 10-20k
into the best of them in fast-count states and hold through the call, so the cash plus profit is back on election night
in time for the punt's stage 2?

    <venv with numpy+matplotlib>/bin/python research_notes/cheap_favourites_study.py      (~1 min; no venue calls)

Inputs (all local, read-only):
  * data/polymarket_history/{legs,extra_legs,kalshi_legs}.csv  daily favourite prices 13/7/3/1 days out (2022, 2024)
  * data/research_punt/hist/legs_hourly.csv                    hourly Kalshi/Polymarket prices 24 h out (2022, 2024)
  * data/sigbot.sqlite venue_fair (latest poll), data/books_latest.json (hourly book snapshot)
  * data/research_contract_rules/tc_mapping.json, data/election_night/poll_close_2026.json
  * data/research_cheapfav/positions.json (one GET of our positions, 24 Oct 00:00 UTC)
  * params/correlation_model.json via sigbot/correlation_model.py (national swing, division and state shocks)
Outputs: data/research_cheapfav/results.json, research_notes/figures/cheap_favourites.png.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from sigbot import correlation_model as cm          # noqa: E402
import favourite_drift_study as F                    # noqa: E402  (loaders and exact tests only; its main() is not run)

OUT = os.path.join(ROOT, "data", "research_cheapfav")
FIG = os.path.join(HERE, "figures", "cheap_favourites.png")
SEED = 20261024
N_SIM = 200_000
OFFICES = {"senate", "governor", "house"}
BANDS = [(0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.99)]
HORIZONS = [13, 7, 3, 1]
MIN_EDGE = 0.02
MIN_FAV = 0.80                       # the basket is safe favourites only (calibration band 0.80-0.99)
SLOW_STATES = {"AK", "AZ", "CA", "ME", "NV", "OR", "UT", "WA", "GA"}
LAST_CLOSE = 21.0                    # ET hour: last polls in the state closed by 21:00
# Races the punt screener names as stage-1 / stage-2 candidates (punt_strategy.md section 4); the sleeve never trades them
PUNT_RACES = {"FL-13 House race", "FL-16 House race", "Vermont Governor", "Minnesota Senate", "Nebraska Governor",
              "Oregon Governor", "Louisiana Senate", "Idaho Governor", "Wyoming Governor", "South Dakota Governor"}
HOLD_SKIP = 4000                     # Sizhe's rule: no entries in races where we hold > 4,000 shares on a leg
PARTISAN_CAP = 5000.0


# =========================================================================================== A1. calibration
def _merge_pm():
    pm = F.read_legs_csv(os.path.join(F.DATA, "legs.csv"), "polymarket")
    have = {(l["market_id"], l["party"]) for l in pm if l[20] is not None or l[13] is not None}
    for l in F.read_legs_csv(os.path.join(F.DATA, "extra_legs.csv"), "polymarket"):
        if (l["market_id"], l["party"]) not in have:
            pm = [x for x in pm if (x["market_id"], x["party"]) != (l["market_id"], l["party"])] + [l]
    return pm


def favourites():
    """[{source, cycle, office, h, p, won, party}] one favourite per race per horizon, prices 0.50-0.995."""
    out = []
    pm = [l for l in _merge_pm() if l["office"] in OFFICES and l["cycle"] in ("2022", "2024")]
    ka = [l for l in F.read_legs_csv(os.path.join(F.DATA, "kalshi_legs.csv"), "kalshi")
          if l["office"] in OFFICES and l["cycle"] in ("2022", "2024")]
    for src, legs in (("Polymarket daily", pm), ("Kalshi daily", ka)):
        for key, ls in F.races(legs).items():
            for h in HORIZONS:
                ok = [l for l in ls if l[h] is not None]
                if not ok:
                    continue
                f = max(ok, key=lambda l: l[h])
                out.append({"source": src, "race": f"{src}:{key}", "cycle": f["cycle"], "office": f["office"], "h": h,
                            "p": f[h], "won": f["outcome"], "party": f["party"]})
    # hourly Kalshi 24 h out (research_punt/hist): the only Kalshi sample with more than a handful of favourites
    by = defaultdict(list)
    for r in csv.DictReader(open(os.path.join(ROOT, "data", "research_punt", "hist", "legs_hourly.csv"))):
        if r["venue"] == "kalshi" and r["office"] in OFFICES and r["cycle"] in ("2022", "2024") and r["h-24"] not in ("", None):
            by[r["race_key"]].append(r)
    for rk, ls in by.items():
        f = max(ls, key=lambda r: float(r["h-24"]))
        out.append({"source": "Kalshi hourly", "race": f"kh:{rk}", "cycle": f["cycle"], "office": f["office"], "h": 1,
                    "p": float(f["h-24"]), "won": int(float(f["outcome"])), "party": f["party_or_outcome"]})
    return out


def corr_pvalue(rows, rho=0.3, n=100_000, seed=SEED):
    """P(wins >= observed) when each cycle shares a latent swing (latent corr rho, signed by the favourite's party):
    favourites of one party in one cycle lose together. Marginals stay at the quoted prices."""
    if not rows:
        return float("nan")
    rng = np.random.default_rng(seed)
    cycles = sorted({r["cycle"] for r in rows})
    ci = np.array([cycles.index(r["cycle"]) for r in rows])
    sgn = np.array([1.0 if r["party"] == "D" else -1.0 for r in rows])
    z = np.array([cm.Phi_inv(min(max(r["p"], 1e-6), 1 - 1e-6)) for r in rows])
    obs = sum(r["won"] for r in rows)
    hits = 0
    for _ in range(n // 20_000):
        u = rng.standard_normal((20_000, len(cycles)))[:, ci] * sgn
        e = rng.standard_normal((20_000, len(rows)))
        wins = (math.sqrt(rho) * u + math.sqrt(1 - rho) * e < z).sum(axis=1)
        hits += int((wins >= obs).sum())
    return hits / (n // 20_000 * 20_000)


def calibration(favs):
    rows = []
    for src in ("Polymarket daily", "Kalshi daily", "Kalshi hourly", "all"):
        for h in HORIZONS:
            for lo, hi in BANDS + [(0.80, 0.99)]:
                sel = [r for r in favs if (src == "all" or r["source"] == src) and r["h"] == h and lo <= r["p"] <= hi
                       and (hi == 0.99 and r["p"] <= 0.99 or r["p"] < hi)]
                if not sel:
                    continue
                n, won, exp = len(sel), sum(r["won"] for r in sel), sum(r["p"] for r in sel)
                ps = [r["p"] for r in sel]
                rows.append({"source": src, "h": h, "band": f"{lo:.2f}-{hi:.2f}", "n": n, "won": won, "expected": exp,
                             "win_rate": won / n, "implied": exp / n,
                             "p_upper": F.poisson_binomial_upper(won, ps),
                             "p_lower": 1 - F.poisson_binomial_upper(won + 1, ps),
                             "p_upper_corr": corr_pvalue(sel) if hi == 0.99 and lo == 0.80 else None})
    return rows


def pooled_races(favs):
    """Each race once (its favourite averaged over the horizons it has in 1-14 days), for the headline count."""
    by = defaultdict(list)
    for r in favs:
        by[r["race"]].append(r)
    out = []
    for k, ls in by.items():
        p = sum(r["p"] for r in ls) / len(ls)
        if 0.80 <= p <= 0.99:
            out.append({**ls[0], "p": p})
    return out


def map_fit(favs, hs=(13, 7)):
    """Calibration map q = sigmoid(a + b logit p), MAP fit (favourite_drift priors) on Polymarket favourites 0.50-0.995
    at the horizons that bracket today (10 days out)."""
    fits = {}
    for h in hs:
        sel = [r for r in favs if r["source"] == "Polymarket daily" and r["h"] == h and 0.5 <= r["p"] <= 0.995]
        xs = [F.logit(r["p"]) for r in sel]
        ys = [r["won"] for r in sel]
        fits[h] = F.map_fit(xs, ys) + (len(sel),)
    a = sum(f[0] for f in fits.values()) / len(fits)
    b = sum(f[1] for f in fits.values()) / len(fits)
    return a, b, fits


def calibrated(p, ab):
    a, b = ab
    p = min(max(p, 1e-4), 1 - 1e-4)
    return 1 / (1 + math.exp(-(a + b * math.log(p / (1 - p)))))


def ev_table(ab):
    rows = []
    for f in (0.85, 0.90, 0.95, 0.98):
        for g in (0.02, 0.05, 0.10):
            c = f - g
            rows.append({"fair": f, "gap": g, "price": c, "ev_outside": 1000 * (f - c) / c,
                         "ev_calibrated": 1000 * (calibrated(f, ab) - c) / c, "q_cal": calibrated(f, ab),
                         "loss_if_upset": -1000.0})
    return rows


# =========================================================================================== A2. basket
def close_hour(s):
    hh, mm = (int(x) for x in s.split(":"))
    h = hh + mm / 60
    return h + 24 if h < 12 else h          # times after midnight are Wed 4 Nov


def walk(levels, limit, buy_yes):
    """Levels available at an edge of at least MIN_EDGE: YES asks at <= limit, or NO bought by hitting YES bids at
    >= limit (cost 1 - bid). Returns [(cost per share, shares)] best first."""
    out = []
    for lv in levels:
        px, n = float(lv["price"]), float(lv["quantity"])
        if (buy_yes and px <= limit + 1e-9) or (not buy_yes and px >= limit - 1e-9):
            out.append((px if buy_yes else 1 - px, n))
    return sorted(out)


def fill(levels, notional):
    """Shares bought by spending `notional` walking `levels` best first (stops at the end of the edge>=2c book)."""
    q, left = 0.0, notional
    for c, n in levels:
        take = min(n, left / c)
        q += take
        left -= take * c
        if left <= 1e-9:
            break
    return q


def basket():
    con = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
    ts = con.execute("SELECT MAX(ts) FROM venue_fair").fetchone()[0]
    vf = [dict(zip(("race", "party", "pm", "kalshi", "fair", "bid", "ask", "ex"), r)) for r in con.execute(
        "SELECT race, party, pm, kalshi, fair, sig_bid, sig_ask, exchange_id FROM venue_fair WHERE ts = ?", (ts,))]
    con.close()
    tc = json.load(open(os.path.join(ROOT, "data", "research_contract_rules", "tc_mapping.json")))
    books = json.load(open(os.path.join(ROOT, "data", "books_latest.json")))
    polls = json.load(open(os.path.join(ROOT, "data", "election_night", "poll_close_2026.json")))
    pos = {p["exchangeId"]: float(p["quantity"]) for p in
           json.load(open(os.path.join(OUT, "positions.json")))["body"]["positions"] if not p.get("settled")}
    parties = defaultdict(set)
    race_ex = defaultdict(list)
    for r in vf:
        parties[r["race"]].add(r["party"])
        race_ex[r["race"]].append(str(r["ex"]))
    U = cm.load_universe()
    all_edges, cands = [], []
    funnel = defaultdict(int)
    for r in vf:
        if r["fair"] is None or r["bid"] is None or r["ask"] is None:
            continue
        m = tc.get(str(r["ex"])) or {}
        same = m.get("terms") == "same" and not any(str(d).endswith("late_settle") for d in (m.get("differences") or []))
        for side in ("yes", "no"):
            edge = (r["fair"] - r["ask"]) if side == "yes" else (r["bid"] - r["fair"])
            p_win = r["fair"] if side == "yes" else 1 - r["fair"]
            price = r["ask"] if side == "yes" else 1 - r["bid"]
            if edge < MIN_EDGE - 1e-9:
                continue
            row = {"race": r["race"], "party": r["party"], "side": side, "ex": str(r["ex"]), "fair": r["fair"],
                   "p_win": p_win, "price": price, "edge": edge, "pm": r["pm"], "kalshi": r["kalshi"], "same_terms": same}
            all_edges.append(row)
            funnel["edge >= 2c"] += 1
            if not same:
                continue
            funnel["+ same terms, no late result"] += 1
            st = cm.race_state(r["race"])
            pc = polls.get(st or "", {})
            if not st or st in SLOW_STATES or not pc or close_hour(pc["last_close_et"]) > LAST_CLOSE:
                continue
            funnel["+ fast-count state"] += 1
            if p_win < MIN_FAV:
                continue
            funnel["+ favourite (p >= 0.80)"] += 1
            if "I" in parties[r["race"]] or r["race"] in PUNT_RACES:
                continue
            funnel["+ no I leg, not a punt race"] += 1
            if max((abs(pos.get(e, 0.0)) for e in race_ex[r["race"]]), default=0) > HOLD_SKIP:
                continue
            funnel["+ we hold <= 4,000 shares"] += 1
            b = books.get(str(r["ex"])) or {}
            lv = b.get("asks" if side == "yes" else "bids") or []
            lv = sorted(lv, key=lambda x: x["price"], reverse=(side == "no"))
            touch = float(lv[0]["quantity"]) if lv else 0.0
            limit = r["fair"] - MIN_EDGE if side == "yes" else r["fair"] + MIN_EDGE
            levels = walk(lv, limit, side == "yes")
            u = U.get(r["race"], {})
            row.update({"state": st, "close_et": pc["last_close_et"], "touch_qty": touch, "levels": levels, "office": u.get("office") or cm.race_office(r["race"]),
                        "division": u.get("division") or cm.division_of(st), "polled": bool(u.get("polled", True)),
                        "win_party": r["party"] if side == "yes" else ("R" if r["party"] == "D" else "D"),
                        "hold_max": max((abs(pos.get(e, 0.0)) for e in race_ex[r["race"]]), default=0)})
            cands.append(row)
    # one route per race outcome (D-YES and R-NO are the same bet): keep the higher edge, pool both routes' depth
    best = {}
    for c in cands:
        k = (c["race"], c["win_party"])
        if k not in best:
            best[k] = dict(c, routes=[c])
        else:
            b = best[k]
            b["routes"].append(c)
            if c["edge"] > b["edge"]:
                best[k] = dict(c, routes=b["routes"])
    legs = []
    for k, b in best.items():
        b["levels"] = sorted(lv for r in b["routes"] for lv in r["levels"])
        b["depth_q"] = sum(n for _, n in b["levels"])
        b["depth_cost"] = sum(c * n for c, n in b["levels"])
        b["avg_px_3k"] = 3000 / fill(b["levels"], 3000) if b["depth_cost"] >= 3000 else (
            b["depth_cost"] / b["depth_q"] if b["depth_q"] else b["price"])
        b.pop("routes")
        legs.append(b)
    legs.sort(key=lambda x: -x["edge"])
    expo = existing_net_d(vf, pos)
    return {"ts": ts, "funnel": dict(funnel), "n_all_edges": len(all_edges), "legs": legs, "existing": expo,
            "all_edges": all_edges}


def existing_net_d(vf, pos):
    """Current net D notional with the bot's own rule (risk.exposure), marked at SIG mids from venue_fair."""
    from sigbot.risk import exposure
    from sigbot.universe import Universe
    markets = json.load(open(os.path.join(ROOT, "data", "markets.json")))
    uni = Universe.from_markets(markets)
    prices = {str(r["ex"]): {"bestBid": r["bid"], "bestAsk": r["ask"]} for r in vf if r["bid"] is not None and r["ask"] is not None}
    e = exposure(uni, pos, prices)
    return {"net_d": e.net_d_notional, "long_d": e.long_d, "short_d": e.short_d}


# =========================================================================================== A3. Monte Carlo
def simulate(legs, n=N_SIM, seed=SEED, mode="outside", ab=None, u_scale=1.0):
    """Win indicators [n, legs] under the factor model (national swing x beta, division, state, idiosyncratic), with each
    race's D marginal set from the outside fair ('outside') or the calibration map ('calibrated')."""
    model = cm.load_model()
    rng = np.random.default_rng(seed)
    sd_u = math.sqrt(model["var_u"]) * u_scale
    sd_div, sd_st = math.sqrt(model["var_division"]), math.sqrt(model["var_state"])
    divs = sorted({l["division"] for l in legs})
    sts = sorted({l["state"] for l in legs})
    mu, bu, di, si, inv, sgn = [], [], [], [], [], []
    for l in legs:
        p_fav = l["p_win"] if mode != "calibrated" else calibrated(l["p_win"], ab)
        p_d = p_fav if l["win_party"] == "D" else 1 - p_fav
        sig = cm.race_sigma(model, l["office"], l["polled"])
        b = model["beta"][l["office"]]
        idio = math.sqrt(max(1.0, sig * sig - b * b * model["var_u"] - model["var_division"] - model["var_state"]))
        mu.append(sig * cm.Phi_inv(min(max(p_d, 1e-6), 1 - 1e-6)) / idio)
        bu.append(b / idio); inv.append(1 / idio)
        di.append(divs.index(l["division"])); si.append(sts.index(l["state"]))
        sgn.append(1 if l["win_party"] == "D" else -1)
    mu, bu, inv, sgn = map(np.array, (mu, bu, inv, sgn))
    di, si = np.array(di), np.array(si)
    out = np.empty((n, len(legs)), dtype=bool)
    u_all = np.empty(n)
    step = 50_000
    for s in range(0, n, step):
        m = min(step, n - s)
        u = rng.normal(0, sd_u, m)
        g = rng.normal(0, sd_div, (m, len(divs)))[:, di]
        st = rng.normal(0, sd_st, (m, len(sts)))[:, si]
        t = mu + bu * u[:, None] + (g + st) * inv
        e = rng.standard_normal((m, len(legs)))
        d_wins = e < t                                     # P(D) = Phi(t)
        out[s:s + m] = np.where(sgn > 0, d_wins, ~d_wins)
        u_all[s:s + m] = u
    return out, u_all


def _spread(legs, idx, budget, scheme, cap):
    if scheme == "equal":
        w = np.ones(len(legs))
    elif scheme == "edge":
        w = np.array([l["edge"] for l in legs])
    else:                                                  # "edge/risk": touch edge over the upset chance
        w = np.array([l["edge"] / max(1e-3, 1 - l["p_win"]) for l in legs])
    w = np.clip(w, 0, None) * idx
    lim = np.array([min(cap, l["depth_cost"]) for l in legs]) * idx
    alloc = np.zeros(len(legs))
    for _ in range(50):
        room = lim - alloc
        act = room > 1e-6
        left = budget - alloc.sum()
        if left < 1 or not act.any():
            break
        ww = w * act
        if ww.sum() <= 0:
            break
        alloc += np.minimum(room, left * ww / ww.sum())
    return alloc


def allocate(legs, budget, scheme, cap, existing=0.0):
    """Notional per leg: weights by scheme, each leg capped at `cap` and at its edge>=2c depth. The basket is split
    into a Democrat-favourite pool and a Republican-favourite pool so the book ends inside the 5,000 partisan cap
    (net-D notional = D pool - R pool; today the book is already over the cap on the D side, so R comes first)."""
    is_d = np.array([l["win_party"] == "D" for l in legs])
    excess = max(0.0, existing - PARTISAN_CAP) - max(0.0, -existing - PARTISAN_CAP)   # >0: need net R from the basket
    r_target = max(0.0, min(budget, (budget + excess) / 2))
    a_r = _spread(legs, ~is_d, r_target, scheme, cap)
    # book after = existing + D - R must stay <= the cap (or no worse than now)
    d_target = max(0.0, min(budget - a_r.sum(), a_r.sum() + max(PARTISAN_CAP, existing) - existing))
    return a_r + _spread(legs, is_d, d_target, scheme, cap)


def pnl_stats(alloc, legs, wins):
    shares = np.array([fill(l["levels"], a) if a > 0 else 0.0 for a, l in zip(alloc, legs)])
    pnl = wins.astype(float) @ shares - alloc.sum()
    return {"notional": float(alloc.sum()), "ev": float(pnl.mean()), "p_loss": float((pnl < 0).mean()),
            "p5": float(np.percentile(pnl, 5)), "p1": float(np.percentile(pnl, 1)), "es5": float(pnl[pnl <= np.percentile(pnl, 5)].mean()),
            "worst": float(pnl.min()), "max_loss": float(-alloc.sum()), "n_legs": int((alloc > 1).sum()),
            "p_any_upset": float((~wins[:, alloc > 1]).any(axis=1).mean()) if (alloc > 1).any() else 0.0,
            "net_d": float(sum(a if l["win_party"] == "D" else -a for a, l in zip(alloc, legs)))}, pnl


def sweep(legs, existing, wins_o, wins_c, wins_s):
    res = []
    for budget in (10_000, 15_000, 20_000):
        for scheme in ("equal", "edge", "edge/risk"):
            for cap in (500, 750, 1000, 1500, 2000, 3000):
                a = allocate(legs, budget, scheme, cap, existing)
                so, _ = pnl_stats(a, legs, wins_o)
                sc, _ = pnl_stats(a, legs, wins_c)
                ss, _ = pnl_stats(a, legs, wins_s)
                res.append({"budget": budget, "scheme": scheme, "cap": cap, "outside": so, "calibrated": sc, "stress": ss,
                            "score": so["ev"] / max(1.0, -so["es5"]) if so["es5"] < 0 else so["ev"],
                            "net_d_after": existing + so["net_d"], "alloc": [round(float(x), 1) for x in a]})
    return res


# =========================================================================================== figure
def figure(cal, ab, legs, sw, pick, pnl_o, pnl_c, pnl_s, u_o, existing):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 12, "axes.titleweight": "bold", "legend.fontsize": 9.5})
    fig = plt.figure(figsize=(13, 30))
    gs = fig.add_gridspec(5, 1, height_ratios=[1.0, 1.55, 1.0, 0.95, 0.55], hspace=0.5, top=0.955, bottom=0.02, left=0.2, right=0.97)
    fig.suptitle("Buying SIG's cheap safe favourites and holding them to the call", fontsize=16, fontweight="bold", y=0.985)
    fig.text(0.5, 0.968, "Past Polymarket/Kalshi favourites, today's fast-count basket, and what 10-20k of it could earn or lose",
             ha="center", fontsize=11.5, color="#444")

    # A. calibration
    ax = fig.add_subplot(gs[0])
    cols = {"Polymarket daily": "#1f77b4", "Kalshi daily": "#ff7f0e", "Kalshi hourly": "#2ca02c"}
    mk = {13: "o", 7: "s", 3: "^", 1: "D"}
    xs = np.linspace(0.78, 1.0, 50)
    ax.plot(xs, xs, color="#555", lw=1.2, label="Price is exactly right (win rate = price)")
    ax.plot(xs, [calibrated(x, ab) for x in xs], color="#d62728", lw=2, ls="--",
            label="Calibrated win chance fitted on past favourites (used as the 'calibrated' case)")
    off = {13: -0.006, 7: -0.002, 3: 0.002, 1: 0.006}
    for r in cal:
        if r["source"] == "all" or r["band"] == "0.80-0.99" or r["n"] < 3:
            continue
        ax.errorbar(r["implied"] + off[r["h"]] + (0.0015 if r["source"] != "Polymarket daily" else 0), r["win_rate"],
                    yerr=[[r["win_rate"] - beta_lo(r["won"], r["n"])], [beta_hi(r["won"], r["n"]) - r["win_rate"]]],
                    fmt=mk[r["h"]], color=cols[r["source"]], ms=4 + min(6, r["n"] / 4), alpha=0.85, capsize=2, lw=0.8)
    for src, c in cols.items():
        ax.plot([], [], "o", color=c, label=f"{src} favourites (marker size = number of races; bars: 95% range)")
    for h, m in mk.items():
        ax.plot([], [], m, color="#777", label=f"{h} day{'s' if h > 1 else ''} before the election")
    head = [r for r in cal if r["source"] == "Polymarket daily" and r["band"] == "0.80-0.99"]
    ax.text(0.80, 0.63, "Polymarket favourites at 0.80-0.99 (Senate, governor, House):\n" + "; ".join(
        f"{r['h']} d out {r['won']} of {r['n']} won ({r['expected']:.1f} expected)" for r in head)
        + "\nNo favourite in the band lost on Polymarket at any horizon; one Kalshi favourite lost 24 h out (15 of 16)",
        fontsize=9.5, va="bottom", bbox=dict(facecolor="white", edgecolor="#bbb"))
    ax.set_xlim(0.79, 1.0); ax.set_ylim(0.6, 1.02)
    ax.set_xlabel("Average outside price of the favourite in the band"); ax.set_ylabel("Share of favourites that won")
    ax.set_title("A. Did past favourites priced 0.80-0.99 win as often as their price said? (2022 and 2024)", loc="left")
    _widen(ax)
    ax.grid(alpha=0.3)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False)

    # B. today's basket
    ax = fig.add_subplot(gs[1])
    a_pick = np.array(pick["alloc"])
    order = np.argsort([-l["edge"] for l in legs])
    labels = [f"{legs[i]['race'].replace(' House race', '')} {legs[i]['win_party']} ({legs[i]['close_et']} ET)" for i in order]
    y = np.arange(len(order))
    ax.barh(y, [legs[i]["edge"] * 100 for i in order], color=["#1f77b4" if legs[i]["win_party"] == "D" else "#d62728" for i in order],
            alpha=0.85)
    for yy, i in zip(y, order):
        ax.text(legs[i]["edge"] * 100 + 0.15, yy, f"fair {legs[i]['p_win']:.3f}, SIG {legs[i]['price']:.3f}, "
                f"{legs[i]['depth_cost']/1000:,.0f}k on offer; buy {a_pick[i]/1000:.1f}k", va="center", fontsize=9)
    ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=9); ax.invert_yaxis()
    ax.set_xlabel("Edge at SIG's best price (cents below the outside fair)")
    ax.set_xlim(0, max(l["edge"] for l in legs) * 100 * 1.9)
    ax.set_title(f"B. Today's candidates: fast-count states, favourites at 0.80+, SIG at least 2c under the outside fair ({len(legs)} legs)", loc="left")
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color="#1f77b4", label="Democrat favourite"), Patch(color="#d62728", label="Republican favourite")],
              loc="lower right", frameon=True)
    ax.grid(axis="x", alpha=0.3)

    # C. P&L distribution of the pick
    ax = fig.add_subplot(gs[2])
    bins = np.linspace(min(pnl_s.min(), pnl_o.min()), max(pnl_o.max(), pnl_c.max()) + 1, 70)
    for p, c, lab in ((pnl_o, "#1f77b4", "Outside prices are right"), (pnl_c, "#2ca02c", "Calibrated (favourites win more often, as in 2022 and 2024)"),
                      (pnl_s, "#d62728", "Stress: national polling miss 1.5x the 2006-2024 size")):
        ax.hist(p, bins=bins, histtype="step", lw=2, color=c, density=True,
                label=f"{lab}: expected {p.mean():+,.0f}, loss chance {(p < 0).mean():.0%}, 5th pct {np.percentile(p, 5):+,.0f}")
    ax.axvline(0, color="#333", lw=1)
    ax.set_yscale("log")
    ax.set_xlabel("Profit at settlement (SUSQies)"); ax.set_ylabel("Probability density (log scale)")
    _widen(ax)
    ax.set_title(f"C. Chosen basket ({pick['budget']/1000:.0f}k, {pick['scheme']} weights, at most {pick['cap']:,} per leg): profit when the races are called", loc="left")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=1, frameon=False)

    # D. expected profit vs downside across the sweep
    ax = fig.add_subplot(gs[3])
    mk2 = {"equal": "o", "edge": "s", "edge/risk": "^"}
    cb = {10_000: "#9ecae1", 15_000: "#4292c6", 20_000: "#08306b"}
    for r in sw:
        ax.scatter(-r["outside"]["es5"], r["outside"]["ev"], marker=mk2[r["scheme"]], color=cb[r["budget"]], s=20 + r["cap"] / 40, alpha=0.8)
    ax.scatter(-pick["outside"]["es5"], pick["outside"]["ev"], s=260, facecolors="none", edgecolors="k", lw=2, label="Chosen allocation")
    for b, c in cb.items():
        ax.scatter([], [], color=c, label=f"{b/1000:.0f}k in total")
    for s, m in mk2.items():
        ax.scatter([], [], marker=m, color="#777", label={"equal": "Same amount per leg", "edge": "More money where the edge is bigger",
                                                          "edge/risk": "Edge divided by the upset chance"}[s])
    ax.set_xlabel("Average loss in the worst 5% of outcomes (SUSQies)"); ax.set_ylabel("Expected profit (SUSQies), outside prices")
    _widen(ax)
    ax.set_title("D. Expected profit against the bad-case loss for every allocation tried (marker size = per-leg cap)", loc="left")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3, frameon=False)

    # glossary
    ax = fig.add_subplot(gs[4]); ax.axis("off")
    pos = ax.get_position(); ax.set_position([0.04, pos.y0, 0.93, pos.height])
    gl = [("outside fair", "the bot's blend of the Polymarket and Kalshi prices for the same contract"),
          ("edge", "outside fair minus the price we pay on SIG, in cents per share"),
          ("favourite", "the side priced 0.80 or more; it pays 1 if it wins and 0 if it loses"),
          ("fast-count state", "last polls close by 21:00 ET; not AK, AZ, CA, ME, NV, OR, UT, WA or GA"),
          ("calibrated", f"win chance from a fit to past favourites: 0.90 -> {calibrated(0.9, ab):.3f}, 0.95 -> {calibrated(0.95, ab):.3f}"),
          ("national polling miss", "one shared error that moves every race the same way (sd 3.7 points, 1.5x in the stress case)"),
          ("state shock", "a second shared error for races in the same state or region"),
          ("5th pct", "the profit that 95% of simulated election nights beat"),
          ("worst-5% loss", "the average profit over the worst 5% of simulated nights"),
          ("partisan cap", f"the bot's 5,000 limit on net Democrat exposure; ours is {existing:+,.0f} today"),
          ("SUSQies", "the competition's play money")]
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes, facecolor="#f4f4ef", edgecolor="#999", lw=1, zorder=0))
    ax.text(0.015, 0.93, "Glossary", fontweight="bold", fontsize=12, transform=ax.transAxes, va="top")
    half = (len(gl) + 1) // 2
    for col, items in enumerate((gl[:half], gl[half:])):
        for i, (k, v) in enumerate(items):
            ax.text(0.015 + 0.5 * col, 0.80 - i * 0.135, f"{k}: {v}", fontsize=9.5, transform=ax.transAxes, va="top", wrap=True)
    os.makedirs(os.path.dirname(FIG), exist_ok=True)
    fig.savefig(FIG, dpi=100)


def _widen(ax):
    pos = ax.get_position()
    ax.set_position([0.09, pos.y0, 0.88, pos.height])


def beta_lo(k, n):
    return 0.0 if k == 0 else _beta_q(0.025, k, n - k + 1)


def beta_hi(k, n):
    return 1.0 if k == n else _beta_q(0.975, k + 1, n - k)


def _beta_q(q, a, b):
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if F.betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# =========================================================================================== main
def main():
    os.makedirs(OUT, exist_ok=True)
    favs = favourites()
    cal = calibration(favs)
    pooled = pooled_races([r for r in favs if r["source"] == "Polymarket daily"])
    pooled_all = pooled_races(favs)
    a, b, fits = map_fit(favs)
    ab = (a, b)
    print("A1 calibration (favourites 0.80-0.99):")
    for r in cal:
        if r["band"] == "0.80-0.99" or r["source"] == "all":
            print(f"  {r['source']:16s} {r['h']:>2}d {r['band']}  n {r['n']:3d}  won {r['won']:3d}  exp {r['expected']:6.1f}  "
                  f"rate {r['win_rate']:.3f} vs {r['implied']:.3f}  P(>=) {r['p_upper']:.3f}  P(<=) {r['p_lower']:.3f}"
                  + (f"  P(>=|cycle corr 0.3) {r['p_upper_corr']:.3f}" if r.get("p_upper_corr") is not None else ""))
    pr = pooled
    print(f"  Polymarket races once (mean 1-14 d price in 0.80-0.99): n {len(pr)} won {sum(r['won'] for r in pr)} "
          f"exp {sum(r['p'] for r in pr):.1f} p {F.poisson_binomial_upper(sum(r['won'] for r in pr), [r['p'] for r in pr]):.4f} "
          f"corr p {corr_pvalue(pr):.3f}")
    pa = pooled_all
    print(f"  all sources, each race-venue once: n {len(pa)} won {sum(r['won'] for r in pa)} exp {sum(r['p'] for r in pa):.1f}")
    print(f"  MAP calibration a {a:+.3f} b {b:.3f} (fits {fits}); 0.85->{calibrated(.85, ab):.3f} 0.90->{calibrated(.9, ab):.3f} "
          f"0.95->{calibrated(.95, ab):.3f} 0.98->{calibrated(.98, ab):.4f}")
    evt = ev_table(ab)

    bk = basket()
    legs = bk["legs"]
    existing = bk["existing"]["net_d"]
    print(f"\nA2 basket: venue_fair ts {bk['ts']:.0f}, funnel {bk['funnel']}, existing net D {existing:+,.0f}")
    for l in legs:
        print(f"  {l['race']:28s} {l['win_party']} via {l['party']}-{l['side']:3s} fair {l['p_win']:.3f} SIG {l['price']:.3f} "
              f"edge {l['edge']*100:4.1f}c depth {l['depth_q']:8.0f} sh / {l['depth_cost']:8.0f}, 3k fills @ {l['avg_px_3k']:.3f} "
              f"touch {l['touch_qty']:6.0f} close {l['close_et']} pm {l['pm']} k {l['kalshi']} hold {l['hold_max']:.0f}")
    wins_o, u_o = simulate(legs, mode="outside")
    wins_c, _ = simulate(legs, mode="calibrated", ab=ab, seed=SEED + 1)
    wins_s, _ = simulate(legs, mode="outside", u_scale=1.5, seed=SEED + 2)
    sw = sweep(legs, existing, wins_o, wins_c, wins_s)
    best = {}
    for r in sw:
        k = r["budget"]
        if k not in best or r["score"] > best[k]["score"]:
            best[k] = r
    print("\nA3 Monte Carlo (n %d), best allocation per budget by EV / worst-5%% loss:" % N_SIM)
    for k, r in sorted(best.items()):
        for mode in ("outside", "calibrated", "stress"):
            s = r[mode]
            print(f"  {k:>6} {r['scheme']:9s} cap {r['cap']:5d} {mode:10s} notional {s['notional']:7.0f} legs {s['n_legs']:2d} "
                  f"EV {s['ev']:+7.0f} P(loss) {s['p_loss']:.3f} p5 {s['p5']:+7.0f} es5 {s['es5']:+7.0f} p1 {s['p1']:+7.0f} "
                  f"worst {s['worst']:+7.0f} P(any upset) {s['p_any_upset']:.3f}")
        print(f"         basket net D {r['outside']['net_d']:+,.0f} -> book {r['net_d_after']:+,.0f}")
    # the score (EV per unit of worst-5% loss) is flat in the budget (10k/15k/20k within ~6%), so the budget is set by
    # cash, not by the ratio: 15k is today's free cash and the middle of Sizhe's 10-20k range
    pick = best[15_000]
    _, pnl_o = pnl_stats(np.array(pick["alloc"]), legs, wins_o)
    _, pnl_c = pnl_stats(np.array(pick["alloc"]), legs, wins_c)
    _, pnl_s = pnl_stats(np.array(pick["alloc"]), legs, wins_s)
    # loss conditional on a big national miss against the basket's net direction
    nd_sign = 1 if pick["outside"]["net_d"] >= 0 else -1
    bad = (u_o * nd_sign) < np.percentile(u_o * nd_sign, 5)
    print(f"  pick: {pick['budget']} {pick['scheme']} cap {pick['cap']}; mean P&L when the national swing is in its worst 5% for the "
          f"basket: {pnl_o[bad].mean():+.0f}")
    figure(cal, ab, legs, sw, pick, pnl_o, pnl_c, pnl_s, u_o, existing)
    json.dump({"calibration": cal, "map_ab": ab, "map_fits": {str(k): v for k, v in fits.items()}, "ev_table": evt,
               "pooled_polymarket": {"n": len(pr), "won": sum(r["won"] for r in pr), "expected": sum(r["p"] for r in pr)},
               "basket": {k: v for k, v in bk.items() if k != "all_edges"}, "sweep": sw, "best": best, "pick": pick,
               "pick_bad_swing_mean": float(pnl_o[bad].mean())},
              open(os.path.join(OUT, "results.json"), "w"), indent=1, default=float)
    print("\nEV per 1,000 bought at fair - gap:")
    for r in evt:
        print(f"  fair {r['fair']:.2f} gap {r['gap']:.2f}: outside {r['ev_outside']:+6.1f}  calibrated {r['ev_calibrated']:+6.1f} (q {r['q_cal']:.4f})")
    print("figure:", FIG)


if __name__ == "__main__":
    main()
