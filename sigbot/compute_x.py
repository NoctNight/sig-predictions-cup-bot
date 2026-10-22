"""Compute x = P(neither major party wins a race) from history, exponentially weighted toward recent years.

x_office(h) = sum_y w_y * wins_{office,y} / sum_y w_y * races_{office,y},   w_y = 0.5 ** ((ref_year - y) / h)

Inputs (data/history/): third_party_wins.csv (year,office,state,...), races_per_year.csv (year,office,num_races).
Writes params/x.json with x by office for the chosen half-life plus a sensitivity table, and (if a DB is given)
stores it under params key "x".

Usage: python -m sigbot.compute_x [--half-life 16] [--ref-year 2026] [--db data/sigbot.sqlite]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    wins = defaultdict(lambda: defaultdict(int))     # office -> year -> wins
    wins_state = defaultdict(lambda: defaultdict(int))  # office -> state -> wins
    with open(os.path.join(HERE, "data/history/third_party_wins.csv")) as f:
        for r in csv.DictReader(f):
            wins[r["office"].strip().lower()][int(r["year"])] += 1
            wins_state[r["office"].strip().lower()][r["state"].strip()] += 1
    races = defaultdict(lambda: defaultdict(int))
    with open(os.path.join(HERE, "data/history/races_per_year.csv")) as f:
        for r in csv.DictReader(f):
            races[r["office"].strip().lower()][int(r["year"])] += int(r["num_races"])
    return wins, races, wins_state


def x_for(wins, races, office, half_life, ref_year, min_year=1970):
    num = den = 0.0
    for y, n in races[office].items():
        if y < min_year:
            continue
        w = 0.5 ** ((ref_year - y) / half_life)
        num += w * wins[office].get(y, 0)
        den += w * n
    return num / den if den else 0.0


STATES = ["AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS",
          "MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"]
NAME = {"AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado","CT":"Connecticut","DE":"Delaware",
        "FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky",
        "LA":"Louisiana","ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri",
        "MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico","NY":"New York",
        "NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island",
        "SC":"South Carolina","SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington",
        "WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming"}
# governors with 2-year terms over the sample hold about twice as many elections
GOV_TERM_FACTOR = {"NH": 2.0, "VT": 2.0, "RI": 1.45, "AR": 1.3}


def per_state(office, wins_rows, races, half_life, ref_year, floor):
    """Empirical-Bayes (beta-binomial, method of moments) third-party rate per state for one office.
    W_s = weighted wins in state s; N_s = weighted races in s (national races per year x the state's share)."""
    fac = {s: (GOV_TERM_FACTOR.get(s, 1.0) if office == "governor" else 1.0) for s in STATES}
    tot = sum(fac.values())
    W = {s: 0.0 for s in STATES}; N = {s: 0.0 for s in STATES}
    for y, n in races[office].items():
        w = 0.5 ** ((ref_year - y) / half_life)
        for s in STATES:
            N[s] += w * n * fac[s] / tot
    for (y, st) in wins_rows:
        if st in W:
            W[st] += 0.5 ** ((ref_year - y) / half_life)
    p = [W[s] / N[s] for s in STATES if N[s] > 0]
    nbar = sum(N.values()) / len(STATES)
    m = sum(W.values()) / sum(N.values())                       # pooled mean
    var = sum((x - m) ** 2 for x in p) / max(1, len(p) - 1)
    # beta prior strength k from the between-state variance net of binomial noise
    between = max(1e-9, var - m * (1 - m) / max(nbar, 1e-9))
    k = max(1.0, m * (1 - m) / between - 1)
    out = {}
    for s in STATES:
        post = (W[s] + k * m) / (N[s] + k)
        out[s] = {"x": round(max(floor, post), 5), "wins_w": round(W[s], 3), "races_w": round(N[s], 2)}
    return out, {"pooled_mean": round(m, 5), "prior_strength_k": round(k, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--half-life", type=float, default=16.0, help="years; weight halves every this many years")
    ap.add_argument("--ref-year", type=int, default=2026)
    ap.add_argument("--db", default=None)
    ap.add_argument("--floor", type=float, default=0.0025, help="minimum per-race rate (write-ins, freak results)")
    args = ap.parse_args()
    wins, races, wins_state = load()
    offices = ["senate", "governor", "house"]
    table = {}
    for h in (8, 12, 16, 24, 1e9):
        table[("inf" if h > 1e8 else h)] = {o: round(x_for(wins, races, o, h, args.ref_year), 5) for o in offices}
    chosen = {o: x_for(wins, races, o, args.half_life, args.ref_year) for o in offices}
    chosen["chamber"] = 0.0
    # pooled senate+governor base rate as a cross-check
    out = {
        "half_life_years": args.half_life,
        "ref_year": args.ref_year,
        "x": {k: round(v, 5) for k, v in chosen.items()},
        "sensitivity": {str(k): v for k, v in table.items()},
        "raw_counts": {o: {"wins": sum(wins[o].values()), "races": sum(races[o].values())} for o in offices},
        "wins_by_state": {o: dict(wins_state[o]) for o in offices},
        "notes": "x = P(non-D/R winner). Independent-win history concentrates in ME, AK, VT, CT, MN, RI; races with an "
                 "explicit Independent market use the 3-way set instead of x.",
    }
    # per-state rates (statewide offices), shrunk toward the office mean by how much history each state has
    import csv as _csv
    rows_by_office = {"senate": [], "governor": []}
    with open(os.path.join(HERE, "data/history/third_party_wins.csv")) as f:
        for r in _csv.DictReader(f):
            o = r["office"].strip().lower()
            if o in rows_by_office:
                rows_by_office[o].append((int(r["year"]), r["state"].strip()))
    by_state, prior = {}, {}
    for o in ("senate", "governor"):
        by_state[o], prior[o] = per_state(o, rows_by_office[o], races, args.half_life, args.ref_year, args.floor)
    by_race = {}
    for o, label in (("senate", "Senate"), ("governor", "Governor")):
        for st, v in by_state[o].items():
            by_race[f"{NAME[st]} {label}"] = v["x"]
    out["by_state"] = by_state
    out["by_race"] = by_race
    out["state_prior"] = prior
    os.makedirs(os.path.join(HERE, "params"), exist_ok=True)
    with open(os.path.join(HERE, "params/x.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: out[k] for k in ("x", "state_prior")}, indent=1))
    hi = sorted(out["by_race"].items(), key=lambda kv: -kv[1])[:14]
    print("highest per-race rates:", [(r, round(100 * v, 2)) for r, v in hi])
    print("floor-level races:", sum(1 for v in out["by_race"].values() if v <= args.floor + 1e-9), "of", len(out["by_race"]))
    if args.db:
        from .store import Store

        Store(os.path.join(HERE, args.db)).set_param("x", out)


if __name__ == "__main__":
    main()
