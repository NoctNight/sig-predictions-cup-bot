"""3 Nov punt plan study (25 Oct 2026). Read-only research; places nothing, makes NO venue calls.

    <venv with numpy+matplotlib>/bin/python research_notes/punt_plan_study.py [--n 20000]

Inputs (cached):
  data/research_punt/sig_reads/lb_all_1025.json, lb_7d_1025.json   leaderboard reads of 25 Oct 22:59 UTC (2 reads)
  data/research_punt/sig_reads/lb_all_0.json, lb_7d_0.json         leaderboard reads of 23 Oct 21:58 UTC (for carry rates)
  data/books_latest.json (25 Oct 22:23 UTC SIG books), params/wind_down_report.json, data/sigbot.sqlite (read-only)
  sigbot/proposals/punt_screener.py (races, ladders, calibrated probabilities, correlated simulation; imported read-only)
  research_notes/punt_strategy_study.py (rival field model: project_field / finals / prize_given_W; imported read-only)
Outputs: data/research_punt/plan_results.json, research_notes/figures/punt_plan_decision.png
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "research_notes"))
from sigbot.proposals import punt_screener as ps  # noqa: E402
import punt_strategy_study as pss  # noqa: E402

RD = os.path.join(HERE, "data", "research_punt", "sig_reads")
OUT = os.path.join(HERE, "data", "research_punt", "plan_results.json")
FIG = os.path.join(HERE, "research_notes", "figures", "punt_plan_decision.png")
ME = "sunny-beacon-7397"
DAYS_LEFT = 8.8                      # 25 Oct 23:00 UTC -> 3 Nov 23:00 UTC (18:00 ET)
FAST_COUNT_BAD = {"OR", "NV", "AK", "AZ", "CA", "WA", "ME", "UT", "GA"}   # BACKLOG 0000 addendum: no stage legs here
# SIG ask -> outside fair per S bought (shock_playbook section 8, venue_fair 212 legs)
SIG_MARKUP = [(0.05, 0.42), (0.10, 0.67), (0.15, 0.88), (0.25, 0.88), (0.35, 0.94), (0.50, 0.95), (0.90, 1.01), (1.0, 1.03)]
# overnight outside calibration, won/price (punt_strategy.md section 6, 2024), shrunk half-way to 1 because 2022 was calibrated
NIGHT_CAL = [(0.15, 0.5 * (0.069 / 0.123) + 0.5), (0.25, 0.5 * (0.11 / 0.19) + 0.5), (0.40, 0.5 * (0.26 / 0.33) + 0.5),
             (0.60, 0.5 * (0.46 / 0.49) + 0.5), (1.0, 1.0)]
RES: dict = {}


def band(table, x):
    for hi, v in table:
        if x <= hi:
            return v
    return table[-1][1]


# ------------------------------------------------------------------------------------------------ 1. cash
def projected_cash():
    wd = json.load(open(os.path.join(HERE, "params", "wind_down_report.json")))
    t = wd["totals"]
    rows = wd["rows"]
    cheap = [r for r in rows if r.get("sleeve") == "cheap favourites"]
    churn = [r for r in rows if r.get("sleeve") == "high-frequency arb"]
    # exempt legs are valued at today's SIG bid (YES held) / 1 - ask (NO held) from the latest books
    books = json.load(open(os.path.join(HERE, "data", "books_latest.json")))

    def sale(legs):
        v = 0.0
        for ex, q in legs.items():
            b = books.get(str(ex)) or {}
            if q > 0 and b.get("bids"):
                v += q * float(b["bids"][0]["price"])
            elif q < 0 and b.get("asks"):
                v += -q * (1 - float(b["asks"][0]["price"]))
        return v
    cheap_v = sum(sale(r["legs"]) for r in cheap)
    churn_v = sum(sale(r["legs"]) for r in churn)
    base = t["projected_cash_1800_et"]
    c = dict(report_et=wd["et"], cash_now=t["cash_now"], late_sets=t["late_sets_sale"], legs=t["legs_sale"],
             wind_down_1800=base, mn05_sale_before=t["early_sets_sale_today"], hf_arb=churn_v, cheap_fav=cheap_v)
    c["stage1_cash_hold_cheapfav"] = base + c["mn05_sale_before"] + churn_v
    c["stage1_cash_all_sold"] = c["stage1_cash_hold_cheapfav"] + cheap_v
    RES["cash"] = {k: (round(v) if isinstance(v, float) else v) for k, v in c.items()}
    return c


# ------------------------------------------------------------------------------------------------ 2. field and bar
def panel_now():
    a = json.load(open(os.path.join(RD, "lb_all_1025.json")))
    a0 = json.load(open(os.path.join(RD, "lb_all_0.json")))
    w0 = json.load(open(os.path.join(RD, "lb_7d_0.json")))
    t1, t0 = pss.ts(a["at"]), pss.ts(a0["at"])
    old = {r["profileId"]: r["pnl"] for r in a0["body"]["leaderboard"]}
    wk = {r["profileId"]: r["pnl"] for r in w0["body"]["leaderboard"]}
    d2 = (t1 - t0) / 86400
    d7 = (t0 - pss.ts("2026-10-18T00:00:00Z")) / 86400
    panel = []
    for r in a["body"]["leaderboard"]:
        p = r["profileId"]
        r2 = (r["pnl"] - old[p]) / d2 if p in old else 0.0
        r7 = wk.get(p, 0.0) / d7
        panel.append(dict(rank=r["rank"], user=r["username"], balance_now=100000 + r["pnl"], rate_per_day_7d=0.5 * (r2 + r7),
                          rate_2d=r2, rate_7d=r7))
    RES["leaderboard"] = dict(read_at=a["at"], total=a["body"]["total"], my_rank=a["body"]["myRank"],
                              top8=[(x["rank"], x["user"], round(x["balance_now"]), round(x["rate_2d"]), round(x["rate_7d"])) for x in panel[:8]])
    return panel


SCEN_USE = ["base case", "few rivals punt", "most rivals punt", "rivals copy our bet"]


def prize_curves(panel, rng, n):
    field = pss.project_field(panel, rng, n, days_left=DAYS_LEFT)
    Wg = np.arange(0.05e6, 2.501e6, 0.005e6)
    cur = {"W": Wg}
    for nm in SCEN_USE:
        fin = pss.finals(field, pss.SCEN[nm], 0.03, rng)
        ev, p1, p3 = pss.prize_given_W(fin, Wg)
        srt = -np.sort(-fin, axis=1)
        cur[nm] = dict(ev=ev, p1=p1, p3=p3)
        RES.setdefault("bar", {})[nm] = dict(first=[round(float(np.percentile(srt[:, 0], q)) / 1e3) for q in (10, 50, 90)],
                                            third=[round(float(np.percentile(srt[:, 2], q)) / 1e3) for q in (10, 50, 90)])
    return cur


def at(cur, nm, key, W):
    W = np.asarray(W, float)
    return np.interp(W, cur["W"], cur[nm][key], left=0.0, right=cur[nm][key][-1])


# ------------------------------------------------------------------------------------------------ 3. structures
def static_eval(cur, W_dist):
    """W_dist: array of final balances over simulations -> {scenario: (P top3, E prize, P 1st)}, E balance."""
    out = {}
    for nm in SCEN_USE:
        out[nm] = (float(at(cur, nm, "p3", W_dist).mean()), float(at(cur, nm, "ev", W_dist).mean()), float(at(cur, nm, "p1", W_dist).mean()))
    return out, float(np.mean(W_dist))


def mask_to_np(mask, n):
    s = bin(mask)[2:].zfill(n)[::-1]
    return np.frombuffer(s.encode(), dtype=np.uint8) == ord("1")


def singles(cur, races, outs, masks, cash, n):
    rows = []
    for k, o in enumerate(outs):
        if o["best"] > 0.30:
            continue
        for f in (0.5, 0.75, 1.0):
            sh = ps.shares_for_cash(o["ladder"], f * cash)
            win = mask_to_np(masks[(k, "calibrated")], n)
            W = np.where(win, cash * (1 - f) + sh, cash * (1 - f))
            ev, eW = static_eval(cur, W)
            rows.append(dict(kind="single", label=o["label"], frac=f, stake=f * cash, touch=o["best"], avg=f * cash / sh, payout=cash * (1 - f) + sh,
                             p=o["p"]["calibrated"], p_out=o["p"]["outside"], ev=ev, EW=eW))
    rows.sort(key=lambda r: -r["ev"]["base case"][1])
    return rows


def baskets(cur, races, outs, masks, cash, n, kmax=3):
    """Equal-cash split across k cheap outcomes in different races. Same-direction (all D upsets or all R upsets) = the
    correlated 'parlay-like' basket; mixed directions = the laddered spread."""
    cheap = sorted([k for k, o in enumerate(outs) if o["best"] <= 0.12 and races[o["race"]]["office"] != "chamber"],
                   key=lambda k: -outs[k]["p"]["calibrated"] / outs[k]["best"])[:16]
    wins = {k: mask_to_np(masks[(k, "calibrated")], n) for k in cheap}
    rows = []
    for kk in (2, 3):
        for combo in itertools.combinations(cheap, kk):
            if len({outs[k]["race"] for k in combo}) < kk:
                continue
            parties = {next(iter(outs[k]["win"])) for k in combo}
            W = np.zeros(n)
            for k in combo:
                W += np.where(wins[k], ps.shares_for_cash(outs[k]["ladder"], cash / kk), 0.0)
            ev, eW = static_eval(cur, W)
            rows.append(dict(kind="basket same direction" if len(parties) == 1 else "ladder mixed",
                             label=" + ".join(outs[k]["label"] for k in combo), k=kk, ev=ev, EW=eW,
                             p_all=float(np.mean(np.all([wins[k] for k in combo], axis=0))), max_payout=float(W.max())))
    best = {}
    for r in rows:
        if r["kind"] not in best or r["ev"]["base case"][1] > best[r["kind"]]["ev"]["base case"][1]:
            best[r["kind"]] = r
    return best


def stage2_walk(books, notional, x=0.3):
    """Median ratio (average fill / touch) of buying `notional` into legs whose ask today is within 0.6x-1.6x of x."""
    ratios = []
    for b in books.values():
        asks = [(float(l["price"]), float(l["quantity"])) for l in b.get("asks") or [] if l.get("quantity")]
        if not asks or not 0.6 * x <= asks[0][0] <= 1.6 * x:
            continue
        sh = ps.shares_for_cash(asks, notional)
        spent = notional if sh else 0
        tot = sum(p * q for p, q in asks)
        if tot < notional:
            continue
        ratios.append((spent / sh) / asks[0][0])
    return float(np.median(ratios)) if ratios else 1.1, len(ratios)


def rolls(cur, races, outs, cash, books, recovery=0.85, sellable=0.85):
    """Two-stage roll with the sellability finding built in: stage 1 is a 22-40c leg bought before a 19:00-20:00 ET
    close in a fast-count state; it is sold at `recovery` once called (P = `sellable`, else held: no prize); stage 2 is a
    plain fair-price bet bought late at the SIG price x2 = W1 / T2, discounted by SIG's markup and the overnight
    outside calibration (no correlated edge, no DMM lag). Returns per stage-1 leg the best T2 and the T2 curve."""
    s1 = []
    for o in outs:
        r = races[o["race"]]
        if not 0.22 <= o["best"] <= 0.40 or r["close"] > 20.0 or r["state"] in FAST_COUNT_BAD or r["office"] == "chamber":
            continue
        cap = o["best"] + 0.05                           # executor max price: never walk more than 10 ticks
        lad = [(p, q) for p, q in o["ladder"] if p <= cap]
        sh = ps.shares_for_cash(lad, cash)
        spent = cash
        if sum(p * q for p, q in lad) < cash:            # the capped ladder is too thin: buy what fits
            sh = sum(q for p, q in lad)
            spent = sum(p * q for p, q in lad)
        s1.append(dict(label=o["label"], state=r["state"], close=r["close"], touch=o["best"], avg=spent / sh, spent=spent, shares=sh,
                       # no longshot haircut at 22-40c (ev_hunt.md part 3: 0.15-0.30 legs won 17% at 22%, CI -17..+11 pts;
                       # punt_strategy Fable fix 3); the SIG markup is already in the walked price
                       p1=o["p"]["outside"], p1_cal=o["p"]["calibrated"]))
    T2g = np.arange(0.9e6, 2.2e6, 0.05e6)
    rows = []
    for s in s1:
        W1 = recovery * s["shares"] + (cash - s["spent"])
        best = None
        curve = {}
        walks = {}
        for T2 in T2g:
            x2_avg = W1 / T2
            band_key = round(x2_avg, 2)
            if band_key not in walks:
                walks[band_key] = stage2_walk(books, W1, x2_avg)[0]
            walk = walks[band_key]
            x2 = x2_avg / walk
            if x2 > 0.85 or x2 < 0.05:
                continue
            p2 = min(1.0, x2 * band(SIG_MARKUP, x2) * band(NIGHT_CAL, x2))
            pw = s["p1"] * sellable * p2
            ev = {nm: (pw * float(at(cur, nm, "p3", T2)), pw * float(at(cur, nm, "ev", T2)), pw * float(at(cur, nm, "p1", T2))) for nm in SCEN_USE}
            EW = pw * T2 + s["p1"] * (1 - sellable) * s["shares"] + (cash - s["spent"]) * (1 - s["p1"])
            curve[float(T2)] = dict(P=pw, x2=x2, walk2=walk, ev=ev, EW=EW)
            if best is None or ev["base case"][1] > best[1]["ev"]["base case"][1]:
                best = (float(T2), curve[float(T2)])
        if best:
            T13 = min(curve, key=lambda t: abs(t - 1.3e6))
            best[1]["at_1p3M"] = {k: v for k, v in curve[T13].items()}
            Tp3 = max(curve, key=lambda t: curve[t]["ev"]["base case"][0])
            best[1]["best_p3"] = dict(T2=Tp3, **{k: v for k, v in curve[Tp3].items() if k != "at_1p3M"})
            rows.append(dict(kind="roll", stage1=s, W1=W1, T2=best[0], **best[1], curve=curve))
    rows.sort(key=lambda r: -r["ev"]["base case"][1])
    return rows


def run(args):
    rng = np.random.default_rng(args.seed)
    cash = projected_cash()
    panel = panel_now()
    cur = prize_curves([p for p in panel if p["user"] != ME], rng, args.n)
    books = json.load(open(os.path.join(HERE, "data", "books_latest.json")))
    races = ps.load_races(books)
    outs = [o for o in ps.outcomes(races) if o["best"] <= 0.6]
    masks = ps.simulate(races, outs, args.sn, seed=11)
    RES["structures"] = {}
    for cname, C in (("all_sold", cash["stage1_cash_all_sold"]), ("hold_cheapfav", cash["stage1_cash_hold_cheapfav"])):
        sg = singles(cur, races, outs, masks, C, args.sn)
        bk = baskets(cur, races, outs, masks, C, args.sn)
        rl = rolls(cur, races, outs, C, books)
        rl_lo = rolls(cur, races, outs, C, books, recovery=0.80, sellable=0.60)
        RES["structures"][cname] = dict(cash=round(C), single=sg[:5], basket=bk, roll=[{k: v for k, v in r.items() if k != "curve"} for r in rl[:6]],
                                        roll_low=[{k: v for k, v in r.items() if k != "curve"} for r in rl_lo[:3]])
        if cname == "all_sold":
            keep = dict(single=sg, basket=bk, roll=rl, roll_low=rl_lo, C=C)
    # roll value vs cash (best stage-1 leg)
    cash_sweep = []
    for C in (40e3, 50e3, 60e3, 72.6e3, 80e3, 87e3, 95e3, 105e3):
        rl = rolls(cur, races, outs, C, books)
        sg = singles(cur, races, outs, masks, C, args.sn)
        cash_sweep.append(dict(C=C, roll=rl[0]["ev"] if rl else None, single=sg[0]["ev"] if sg else None))
    RES["cash_sweep"] = [dict(C=x["C"], roll_base=x["roll"]["base case"] if x["roll"] else None,
                              single_base=x["single"]["base case"] if x["single"] else None) for x in cash_sweep]
    json.dump(RES, open(OUT, "w"), indent=1, default=float)
    if not args.no_fig:
        draw(cur, keep, cash_sweep)
    return RES


# ------------------------------------------------------------------------------------------------ figure
def draw(cur, keep, cash_sweep):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(13, 29))
    gs = fig.add_gridspec(4, 1, height_ratios=[1, 1, 1, 0.42], hspace=0.62, top=0.95, bottom=0.03, left=0.08, right=0.97)
    fig.suptitle("The 3 Nov punt: which structure gives the best chance of a top-3 finish", fontsize=17, fontweight="bold", y=0.99)
    fig.text(0.5, 0.973, f"Cash at stage 1 (18:00 ET) about {keep['C'] / 1e3:.0f}k if every holding is sold; rivals projected from the 25 Oct leaderboard",
             ha="center", fontsize=11, color="#444")
    col = {"base case": "#1f77b4", "few rivals punt": "#2ca02c", "most rivals punt": "#d62728", "rivals copy our bet": "#9467bd"}
    # A: what final balance is needed
    ax = fig.add_subplot(gs[0])
    for nm in SCEN_USE:
        ax.plot(cur["W"] / 1e6, cur[nm]["p3"], color=col[nm], lw=2, label=f"{nm}: chance of top 3")
        ax.plot(cur["W"] / 1e6, cur[nm]["p1"], color=col[nm], lw=1.5, ls="--", label=f"{nm}: chance of 1st")
    ax.set_xlim(0.8, 2.0)
    ax.set_title("A. What final balance does a top-3 finish need?", loc="left", fontweight="bold")
    ax.set_xlabel("Our final balance (millions of SUSQies)")
    ax.set_ylabel("Chance of finishing there\nif our balance is W")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False, fontsize=9)
    # B: P(top3) per structure (stacked bars: 1st / 2nd-3rd)
    ax = fig.add_subplot(gs[1])
    items = []
    s0 = keep["single"][0]
    items.append((f"Single longshot\n{s0['label']}\n(all-in before 18:00)", s0))
    for kind in ("basket same direction", "ladder mixed"):
        if kind in keep["basket"]:
            b = keep["basket"][kind]
            items.append((("Correlated basket\n" if kind.startswith("basket") else "Laddered spread\n") + b["label"].replace(" + ", "\n+ "), b))
    for r in keep["roll"][:3]:
        items.append((f"Roll {r['stage1']['label']}\n-> late fair-price leg\nto {r['T2'] / 1e6:.2f}M", r))
    if keep["roll_low"]:
        r = keep["roll_low"][0]
        items.append((f"Roll {r['stage1']['label']}\npessimistic sale\n(60% sellable, 0.80)", r))
    x = np.arange(len(items))
    for j, nm in enumerate(SCEN_USE):
        p3 = np.array([it[1]["ev"][nm][0] for it in items])
        p1 = np.array([it[1]["ev"][nm][2] for it in items])
        ax.bar(x + (j - 1.5) * 0.2, p1 * 100, 0.2, color=col[nm], label=f"{nm}: 1st place" if True else None)
        ax.bar(x + (j - 1.5) * 0.2, (p3 - p1) * 100, 0.2, bottom=p1 * 100, color=col[nm], alpha=0.35,
               label=f"{nm}: 2nd or 3rd")
    for i, it in enumerate(items):
        ax.text(i, it[1]["ev"]["base case"][0] * 100 + 0.08, f"${it[1]['ev']['base case'][1]:,.0f}", ha="center", fontsize=9, color="#1f77b4")
    ax.set_xticks(x)
    ax.set_xticklabels([it[0] for it in items], fontsize=8)
    ax.set_ylabel("Chance of a top-3 finish (%)")
    ax.set_title("B. Chance of top 3 by punt structure (solid = 1st, pale = 2nd/3rd; blue numbers = expected prize, base case)", loc="left", fontweight="bold")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.24), ncol=4, frameon=False, fontsize=9)
    # C: roll target and cash
    ax = fig.add_subplot(gs[2])
    r = keep["roll"][0]
    T = sorted(r["curve"])
    for nm in SCEN_USE:
        ax.plot(np.array(T) / 1e6, [r["curve"][t]["ev"][nm][0] * 100 for t in T], color=col[nm], lw=2,
                label=f"{nm}: best roll, chance of top 3 vs stage-2 target")
    ax2 = ax.twiny()
    Cs = [c["C"] / 1e3 for c in cash_sweep]
    ax2.plot(Cs, [c["roll"]["base case"][0] * 100 if c["roll"] else 0 for c in cash_sweep], color="k", lw=1.5, ls=":", marker="o",
             label="base case: best roll, chance of top 3 vs cash at 18:00 ET (top axis)")
    ax2.plot(Cs, [c["single"]["base case"][0] * 100 if c["single"] else 0 for c in cash_sweep], color="grey", lw=1.5, ls="-.", marker="s",
             label="base case: best single, chance of top 3 vs cash (top axis)")
    ax2.set_xlabel("Cash at 18:00 ET (k SUSQies)")
    ax.set_xlabel("Stage-2 target: our balance if both stages win (millions)")
    ax.set_ylabel("Chance of a top-3 finish (%)")
    ax.set_title(f"C. How big should the roll aim, and what is extra cash worth? (stage 1 = {r['stage1']['label']})", loc="left", fontweight="bold", y=1.09)
    ax.grid(alpha=0.3)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False, fontsize=9)
    # glossary
    ax = fig.add_subplot(gs[3])
    ax.axis("off")
    g = ("Glossary\n"
         "top 3: final settled balance above the 3rd-best rival; prizes $30k / $5k / $2.5k for 1st / 2nd / 3rd\n"
         "rivals: the top 60 accounts of 25 Oct 22:59 UTC, carried at their 23-25 Oct and 18-23 Oct P&L rates to 3 Nov, plus settlement noise;\n"
         "    'rivals punt' = chasers ranked 5-30 make their own all-in bet (base 35%, few 10%, most 70%); 'copy' = their bets win when ours does (corr 0.6)\n"
         "single longshot: all cash into one outcome before 18:00 ET, walked through SIG's 25 Oct book\n"
         "correlated basket: cash split equally over 2-3 cheap upsets of the same party (national swing links them)\n"
         "laddered spread: the same split over upsets of both parties (more chance one hits, smaller payout each)\n"
         "roll: stage 1 = a 22-40c leg bought before its 19:00-20:00 ET poll close in a fast-count state; sold at 0.85 once called\n"
         "    (85% of the time in time; else held, no prize); stage 2 = everything into a late fair-price leg sized to the target\n"
         "win chances: Polymarket/Kalshi blend corrected for the favourite-longshot bias (ev_hunt.md part 3); stage 2 also pays\n"
         "    SIG's markup and the overnight longshot bias (half-way between 2024 and calibrated 2022)\n"
         "expected prize: chance of each rank x its prize, averaged over the bet's outcomes")
    ax.text(0.0, 1.0, g, va="top", ha="left", fontsize=9.5, family="DejaVu Sans",
            bbox=dict(boxstyle="round", facecolor="#f4f4f0", edgecolor="#999"))
    fig.savefig(FIG, dpi=100)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000, help="field simulations")
    ap.add_argument("--sn", type=int, default=20000, help="race simulations")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-fig", action="store_true")
    res = run(ap.parse_args())
    print(json.dumps({k: res[k] for k in ("cash", "leaderboard", "bar", "cash_sweep")}, indent=1, default=float))
