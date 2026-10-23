"""Late punt screener for the SIG Predictions Cup (read-only; prints options, sends nothing).

On 3 Nov, given our cash C and the third-place balance T, list the bets whose payoff lifts us from C to at least T,
ranked by the probability of getting there. Prices are SIG's own books walked level by level, so SIG's longshot
overpricing (longshots trade 3-7 points above Polymarket/Kalshi, research_notes/favourite_drift.md) is paid in full.
Probabilities come in three views:
  fair        our believed fair (params/fair_explained.json, else the fair model's D fair)
  outside     the Polymarket/Kalshi blend (latest venue_fair row in data/sigbot.sqlite, read-only)
  calibrated  the outside blend corrected for the favourite-longshot bias: the race favourite's probability becomes
              logistic(a + b logit(p)) with the MAP fit a 0.14, b 1.37 (favourite_drift.md); longshots shrink
Joint outcomes: Monte Carlo of the cross-race factor model (params/correlation_model.json: national swing sd 3.7 pts
with office betas, division 1.9, state 2.9, idiosyncratic per office), so same-direction upsets are correlated.
Common random numbers across views.

Option families:
  single   back one outcome (YES on a leg, or NO on a leg = the other parties) with the smallest stake whose payout
           reaches T; success iff it wins
  m-of-k   back k cheap outcomes in different races with equal payouts; success iff at least m win (k 2-3)
  roll     back A in an early-closing state (polls closed by 21:00 ET), sell after A is called at `roll_recovery` x 1 per
           share (0.9 by default; SIG's DMM lags and compresses tails, so the bid may sit well below 1), put everything on B,
           which closes 60+ min later, at today's B price plus `roll_follow` (0.5) of the outside repricing P(B|A) - P(B);
           success iff A and B both win. Needs A called before B's polls close: often false for an upset A
For any bets at fair prices P(reach T) <= C/T (wealth is a martingale), so C/T is printed as the yardstick.

CLI:  python3 -m sigbot.proposals.punt_screener --report --cash 102000 --target 955000 [--top 8] [--n 40000]
      python3 -m sigbot.proposals.punt_screener --report --cash 102000 --leaderboard data/leaderboard_snapshot.json
      python3 -m sigbot.proposals.punt_screener --selftest
Inputs default to data/books.json (SIG books; pass --books with a fresh feed.books() dump on 3 Nov), data/markets.json,
params/fair_explained.json, params/fair_model.json, the venue_fair table. No SIG API reads.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import sqlite3
import sys
from array import array
from statistics import NormalDist
from typing import Optional

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
A_MAP, B_MAP = 0.14, 1.37
VIEWS = ("fair", "outside", "calibrated")
ND = NormalDist()


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def calibrate(probs: dict) -> dict:
    """Race probabilities {party: p} -> favourite-longshot corrected (the favourite rises, the others shrink pro rata)."""
    fav = max(probs, key=probs.get)
    pf = probs[fav]
    if pf <= 0.5 or pf >= 1:
        return dict(probs)
    pf2 = 1 / (1 + math.exp(-(A_MAP + B_MAP * logit(pf))))
    rest = 1 - pf
    return {p: (pf2 if p == fav else (v * (1 - pf2) / rest if rest > 0 else 0.0)) for p, v in probs.items()}


# ------------------------------------------------------------------------------------------------ inputs
def load_races(books: Optional[dict] = None, db: Optional[str] = None) -> dict:
    """race -> {office, state, division, polled, legs {party: ex}, probs {view: {party: p}}, close (ET hour), books}."""
    from .. import correlation_model as cm
    from .election_night import POLL_CLOSE_ET, race_abbr
    U = cm.load_universe()
    if books is None:                     # newest snapshot from the bot's feed (hourly), else the 21-Oct dump
        latest = os.path.join(HERE, "data", "books_latest.json")
        books = json.load(open(latest if os.path.exists(latest) else os.path.join(HERE, "data", "books.json")))
    fe = json.load(open(os.path.join(HERE, "params", "fair_explained.json")))["legs"]
    outside: dict = {}
    try:
        con = sqlite3.connect(f"file:{db or os.path.join(HERE, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
        for race, party, fair in con.execute("SELECT race, party, fair FROM venue_fair WHERE ts = (SELECT MAX(ts) FROM venue_fair)"):
            if fair is not None:
                outside.setdefault(race, {})[party] = fair
        con.close()
    except sqlite3.Error:
        pass
    out = {}
    for race, u in U.items():
        legs = u["legs"]
        fair, out_p = {}, {}
        for party in legs:
            leg = fe.get(f"{race} [{party}]")
            if leg:
                fair[party] = leg["believed_fair"]
                if leg.get("outside_blend") is not None:
                    out_p[party] = leg["outside_blend"]
        if len(fair) < len(legs) and u.get("fair_d") is not None and set(legs) == {"D", "R"}:
            fair = {"D": u["fair_d"], "R": 1 - u["fair_d"]}
        if race in outside and set(outside[race]) >= set(legs):
            out_p = {p: outside[race][p] for p in legs}
        if len(out_p) < len(legs):
            out_p = dict(fair)
        if len(fair) < len(legs):
            fair = dict(out_p)
        if len(fair) < len(legs):
            continue
        norm = lambda d: {p: max(1e-4, v) / sum(max(1e-4, x) for x in d.values()) for p, v in d.items()}
        fair, out_p = norm(fair), norm(out_p)
        ab = race_abbr(race)
        out[race] = dict(race=race, office=u["office"], state=u["state"], division=u["division"], polled=u["polled"], legs=legs,
                         probs={"fair": fair, "outside": out_p, "calibrated": calibrate(out_p)},
                         close=POLL_CLOSE_ET[ab][1] if ab in POLL_CLOSE_ET else 24.0,
                         books={p: books.get(ex) or {} for p, ex in legs.items()})
    return out


# ------------------------------------------------------------------------------------------------ outcomes and ladders
def outcomes(races: dict) -> list[dict]:
    """Backable outcomes: 'race wins party' (YES on the leg; in two-leg races also NO on the other leg) and, in three-leg
    races, 'party loses' (NO on the leg). Each with a cheapest-first ladder of (price per share, shares)."""
    out = []
    for race, r in races.items():
        legs = list(r["legs"])
        for party in legs:
            lad = [(float(l["price"]), float(l["quantity"])) for l in r["books"][party].get("asks") or [] if l.get("quantity")]
            others = [p for p in legs if p != party]
            if len(others) == 1:
                lad += [(round(1 - float(l["price"]), 4), float(l["quantity"])) for l in r["books"][others[0]].get("bids") or [] if l.get("quantity")]
            if lad:
                out.append(dict(race=race, win=frozenset([party]), label=f"{race} {party}", ladder=sorted(lad)))
            if len(legs) == 3:
                nl = [(round(1 - float(l["price"]), 4), float(l["quantity"])) for l in r["books"][party].get("bids") or [] if l.get("quantity")]
                if nl:
                    out.append(dict(race=race, win=frozenset(others), label=f"{race} not {party}", ladder=sorted(nl)))
    for o in out:
        o["best"] = o["ladder"][0][0]
        o["p"] = {v: sum(races[o["race"]]["probs"][v][p] for p in o["win"]) for v in VIEWS}
    return out


def cost_for_shares(ladder: list, shares: float) -> Optional[float]:
    """Cost of buying `shares` walking the ladder; None if the book is too thin."""
    cost, left = 0.0, shares
    for px, q in ladder:
        n = min(q, left)
        cost += n * px
        left -= n
        if left <= 1e-9:
            return cost
    return None


def stake_to_reach(ladder: list, cash: float, target: float) -> Optional[tuple]:
    """Smallest (stake, shares) with cash - stake + shares >= target, walking the ladder; None if infeasible."""
    need = target - cash
    stake = shares = 0.0
    for px, q in ladder:
        if px >= 1:
            break
        gain_per = 1 - px                      # each share adds (1 - px) to shares - stake
        n = min(q, max(0.0, (need - (shares - stake)) / gain_per))
        stake += n * px
        shares += n
        if shares - stake >= need - 1e-6:
            return (stake, shares) if stake <= cash + 1e-6 else None
        if stake > cash:
            return None
    return None


def shares_for_cash(ladder: list, cash: float) -> float:
    shares, left = 0.0, cash
    for px, q in ladder:
        n = min(q, left / px)
        shares += n
        left -= n * px
        if left <= 1e-9:
            break
    return shares


# ------------------------------------------------------------------------------------------------ joint simulation
def simulate(races: dict, outs: list, n: int = 40000, seed: int = 11) -> dict:
    """Win bitmasks per (outcome index, view) as Python ints (bit i = simulation i), from one draw of the factor model."""
    from .. import correlation_model as cm
    m = cm.load_model()
    rnd = random.Random(seed)
    sd_u = math.sqrt(m["var_u"])
    sd_div, sd_st = math.sqrt(m["var_division"]), math.sqrt(m["var_state"])
    u = array("d", (rnd.gauss(0, 1) for _ in range(n)))
    divs = {r["division"] for r in races.values() if r["division"]}
    sts = {r["state"] for r in races.values() if r["state"]}
    dv = {d: array("d", (rnd.gauss(0, 1) for _ in range(n))) for d in divs}
    sv = {s: array("d", (rnd.gauss(0, 1) for _ in range(n))) for s in sts}
    winners: dict = {}                         # race -> {view: bytearray of party codes}
    for race, r in races.items():
        legs = sorted(r["legs"])
        code = {p: i for i, p in enumerate(legs)}
        if r["office"] == "chamber":
            load_u, load_d, load_s, sig = 0.9 * sd_u, 0.0, 0.0, sd_u
        else:
            b = m["beta"].get(r["office"], 1.0)
            sig = cm.race_sigma(m, r["office"], r["polled"])
            load_u, load_d, load_s = b * sd_u, (sd_div if r["division"] else 0.0), (sd_st if r["state"] else 0.0)
        idio = math.sqrt(max(1e-6, sig * sig - load_u ** 2 - load_d ** 2 - load_s ** 2))
        dvr, svr = dv.get(r["division"]), sv.get(r["state"])
        lat = array("d", ((load_u * u[i] + (load_d * dvr[i] if dvr else 0.0) + (load_s * svr[i] if svr else 0.0) + idio * rnd.gauss(0, 1)) / sig
                          for i in range(n)))
        ind = array("d", (0.3 * u[i] + 0.954 * rnd.gauss(0, 1) for i in range(n))) if "I" in legs else None
        winners[race] = {}
        for v in VIEWS:
            pr = r["probs"][v]
            pi = pr.get("I", 0.0)
            pd = pr.get("D", 0.0) / max(1e-9, 1 - pi)
            th_i = ND.inv_cdf(min(max(pi, 1e-9), 1 - 1e-9)) if ind is not None else None
            th_d = -ND.inv_cdf(min(max(pd, 1e-9), 1 - 1e-9))          # D wins iff latent > th_d
            if "D" not in legs:
                th_d = float("inf")
            if "R" not in legs:
                th_d = -float("inf")
            wb = bytearray(n)
            cD, cR, cI = code.get("D", 0), code.get("R", 0), code.get("I", 0)
            for i in range(n):
                if ind is not None and ind[i] < th_i:
                    wb[i] = cI
                else:
                    wb[i] = cD if lat[i] > th_d else cR
            winners[race][v] = (wb, code)
    masks: dict = {}
    for k, o in enumerate(outs):
        wb_code = winners[o["race"]]
        for v in VIEWS:
            wb, code = wb_code[v]
            want = {code[p] for p in o["win"]}
            bits = bytes(48 + (x in want) for x in wb)                 # b'0'/b'1'
            masks[(k, v)] = int(bits[::-1].decode(), 2)               # bit i = simulation i
    return masks


def pct(mask: int, n: int) -> float:
    return mask.bit_count() / n


# ------------------------------------------------------------------------------------------------ screening
def screen(cash: float, target: float, races: Optional[dict] = None, n: int = 40000, top: int = 8, max_price: float = 0.6,
           roll_recovery: float = 0.9, roll_gap_h: float = 1.0, roll_follow: float = 0.5, seed: int = 11) -> dict:
    races = races if races is not None else load_races()
    outs = [o for o in outcomes(races) if o["best"] <= max_price]
    masks = simulate(races, outs, n, seed)
    P = lambda mask: {v: pct(mask[v], n) for v in VIEWS}
    res = {"cash": cash, "target": target, "bound": cash / target, "n_sims": n, "single": [], "combo": [], "roll": []}
    # ---- singles
    for k, o in enumerate(outs):
        st = stake_to_reach(o["ladder"], cash, target)
        if not st:
            continue
        stake, sh = st
        p = {v: pct(masks[(k, v)], n) for v in VIEWS}
        res["single"].append(dict(option=f"back {o['label']}", stake=stake, avg_price=stake / sh, p=p,
                                  exp_loss=stake - p["calibrated"] * sh, best=o["best"]))
    # ---- m-of-k among the cheapest outcomes in different races
    cheap = sorted([k for k, o in enumerate(outs) if o["best"] <= 0.25], key=lambda k: -outs[k]["p"]["calibrated"] / outs[k]["best"])[:22]
    for kk in (2, 3):
        for combo in itertools.combinations(cheap, kk):
            if len({outs[k]["race"] for k in combo}) < kk:
                continue
            for mreq in range(1, kk + 1):
                # equal payout s per outcome; success iff >= mreq win: mreq s - sum cost_i(s) >= target - cash, total cost <= cash
                def gap(s):
                    cs = [cost_for_shares(outs[k]["ladder"], s) for k in combo]
                    if any(c is None for c in cs):
                        return None, None
                    return mreq * s - sum(cs), sum(cs)
                lo, hi = 0.0, target * 2
                g, c = gap(hi)
                while g is None and hi > 1:
                    hi /= 1.5
                    g, c = gap(hi)
                if g is None or g < target - cash:
                    continue
                for _ in range(40):
                    mid = 0.5 * (lo + hi)
                    gm, cm_ = gap(mid)
                    if gm is not None and gm >= target - cash:
                        hi = mid
                    else:
                        lo = mid
                g, c = gap(hi)
                if c is None or c > cash:
                    continue
                p = {}
                for v in VIEWS:
                    ms = [masks[(k, v)] for k in combo]
                    acc = 0
                    for sub in itertools.combinations(ms, mreq):
                        x = sub[0]
                        for y in sub[1:]:
                            x &= y
                        acc |= x
                    p[v] = pct(acc, n)
                # expected payout under calibrated: s x expected number of winners (the bet pays s per winner)
                ev = hi * sum(outs[k]["p"]["calibrated"] for k in combo)
                res["combo"].append(dict(option=f"{mreq} of {kk}: " + " + ".join(outs[k]["label"] for k in combo),
                                         stake=c, p=p, exp_loss=c - ev))
    # ---- two-stage roll across poll-closing waves
    for a, oa in enumerate(outs):
        ca = races[oa["race"]]["close"]
        if ca > 21.0:
            continue
        sa = shares_for_cash(oa["ladder"], cash)
        w1 = roll_recovery * sa
        if w1 <= cash:
            continue
        for b, ob in enumerate(outs):
            if ob["race"] == oa["race"] or races[ob["race"]]["close"] < ca + roll_gap_h:
                continue
            # B's SIG price after A wins: the outside reprices B to P(B | A); SIG's DMM has followed roll_follow of that
            pa = pct(masks[(a, "outside")], n)
            pb_a = pct(masks[(a, "outside")] & masks[(b, "outside")], n) / pa if pa > 0 else 0.0
            shift = max(0.0, roll_follow * (pb_a - ob["p"]["outside"]))
            sb = shares_for_cash([(min(0.99, px + shift), q) for px, q in ob["ladder"]], w1)
            if sb < target:
                continue
            p = {v: pct(masks[(a, v)] & masks[(b, v)], n) for v in VIEWS}
            res["roll"].append(dict(option=f"roll {oa['label']} ({ca:g}h ET) -> {ob['label']} ({races[ob['race']]['close']:g}h)",
                                    stake=cash, p=p, exp_loss=cash - p["calibrated"] * sb, mid_wealth=w1, final=sb))
    for k in ("single", "combo", "roll"):
        res[k].sort(key=lambda r: (-r["p"]["calibrated"], r["exp_loss"]))
        res[k] = res[k][:top]
    return res


def print_report(res: dict):
    c, t = res["cash"], res["target"]
    print(f"Punt screener: cash {c:,.0f} -> target {t:,.0f} ({t / c:.1f}x). Yardstick: at fair prices no strategy reaches the "
          f"target with probability above C/T = {res['bound']:.3f}. {res['n_sims']:,} correlated simulations.")
    for k, title in (("single", "Single outcomes (smallest stake that reaches the target)"), ("combo", "m-of-k combinations, equal payouts"),
                     ("roll", "Two-stage rolls across poll-closing waves (all-in twice; A sold at recovery x 1, B bought after half the DMM repricing)")):
        print(f"\n{title}\n| option | stake | P fair | P outside | P calibrated | expected loss (calibrated) |\n|---|---|---|---|---|---|")
        if not res[k]:
            print("| none feasible | | | | | |")
        for r in res[k]:
            p = r["p"]
            print(f"| {r['option']} | {r['stake']:,.0f} | {p['fair']:.4f} | {p['outside']:.4f} | {p['calibrated']:.4f} | {r['exp_loss']:,.0f} |")


def leaderboard_target(path: str, rank: int = 3, start: float = 100000.0) -> float:
    rows = json.load(open(path))
    rows = rows if isinstance(rows, list) else rows.get("entries") or rows.get("leaderboard") or []
    r = next(x for x in rows if int(x.get("rank", 0)) == rank)
    return start + float(r["pnl"])


# ------------------------------------------------------------------------------------------------ self-test
def selftest():
    # ladders
    lad = [(0.05, 1000.0), (0.06, 1000.0), (0.10, 1e6)]
    st = stake_to_reach(lad, 100.0, 1000.0)
    assert st and abs((100 - st[0] + st[1]) - 1000) < 1e-6 and st[0] <= 100, st
    assert stake_to_reach([(0.2, 1e9)], 100.0, 1000.0) is None              # 0.2 > C/T: infeasible
    assert abs(cost_for_shares(lad, 1500) - (50 + 30)) < 1e-9 and cost_for_shares(lad[:1], 2000) is None
    assert abs(shares_for_cash([(0.5, 10), (1 / 3, 100)], 8) - (10 + 3 / (1 / 3))) < 1e-9
    # calibration: favourite up, longshot down, sums to 1
    c = calibrate({"D": 0.9, "R": 0.1})
    assert c["D"] > 0.9 and abs(sum(c.values()) - 1) < 1e-12
    # a synthetic two-race universe: probabilities and correlation come out of the simulation
    book = lambda ask, bid: {"asks": [{"price": ask, "quantity": 1e7}], "bids": [{"price": bid, "quantity": 1e7}]}
    races = {}
    for name, st_, div, pd in (("Ohio Senate", "OH", "East North Central", 0.1), ("Ohio Governor", "OH", "East North Central", 0.1),
                               ("Texas Senate", "TX", "West South Central", 0.5)):
        pr = {"D": pd, "R": 1 - pd}
        races[name] = dict(race=name, office="senate" if "Senate" in name else "governor", state=st_, division=div, polled=True,
                           legs={"D": name + "D", "R": name + "R"}, probs={"fair": pr, "outside": pr, "calibrated": calibrate(pr)},
                           close=19.5 if st_ == "OH" else 21.0,
                           books={"D": book(round(pd + 0.03, 3), round(pd + 0.02, 3)), "R": book(round(1 - pd - 0.02, 3), round(1 - pd - 0.03, 3))})
    outs = outcomes(races)
    masks = simulate(races, outs, n=20000, seed=3)
    idx = {o["label"]: k for k, o in enumerate(outs)}
    p_oh = pct(masks[(idx["Ohio Senate D"], "outside")], 20000)
    assert abs(p_oh - 0.1) < 0.01, p_oh
    both = pct(masks[(idx["Ohio Senate D"], "outside")] & masks[(idx["Ohio Governor D"], "outside")], 20000)
    assert both > 0.1 * 0.1 * 2.5, both                                      # same-state upsets are strongly correlated
    res = screen(10000, 60000, races=races, n=20000, top=3)
    assert res["single"] and all(r["avg_price"] <= 10000 / 60000 + 1e-9 for r in res["single"])
    assert res["single"][0]["option"].startswith("back Ohio") and res["single"][0]["p"]["calibrated"] < 0.1
    assert any(r["option"].startswith("roll Ohio") for r in res["roll"]) or not res["roll"]
    print("punt_screener selftest: OK")


def main(argv=None):
    ap = argparse.ArgumentParser(description="late punt screener: bets that lift cash C to target T, ranked by probability")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--cash", type=float, default=None)
    ap.add_argument("--target", type=float, default=None)
    ap.add_argument("--leaderboard", default=None, help="leaderboard JSON; target = start + pnl of --rank")
    ap.add_argument("--rank", type=int, default=3)
    ap.add_argument("--start", type=float, default=100000.0)
    ap.add_argument("--books", default=None, help="SIG books JSON {exchange_id: {bids, asks}} (default data/books.json)")
    ap.add_argument("--n", type=int, default=40000)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--recovery", type=float, default=0.9, help="roll: share of 1 recovered when selling the called first leg")
    ap.add_argument("--follow", type=float, default=0.5, help="roll: share of the outside repricing of B that SIG has made by the time we buy")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
    if a.report:
        target = a.target if a.target is not None else leaderboard_target(a.leaderboard or os.path.join(HERE, "data", "leaderboard_snapshot.json"), a.rank, a.start)
        races = load_races(json.load(open(a.books)) if a.books else None)
        res = screen(a.cash or 102000.0, target, races=races, n=a.n, top=a.top, roll_recovery=a.recovery, roll_follow=a.follow)
        print(json.dumps(res, indent=1, default=str) if a.json else "", end="")
        if not a.json:
            print_report(res)
    if not (a.selftest or a.report):
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
