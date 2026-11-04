"""Drill-down 20: do the promising splits survive strict mechanics? No-poll seats, toss-ups, Kalshi-led moves, plus outside momentum after SIG has caught up, and the mirror leg."""
import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
pd.set_option("display.width", 200, "display.max_rows", 100)
fm = json.load(open("params/fair_model.json"))["races"]
ratef = lambda r: (fm.get(r, {}).get("inputs", {}) or {}).get("rating_margin")
pollf = lambda r: (fm.get(r, {}).get("inputs", {}) or {}).get("n_polls")
for name, f in (("slow", "strict_slow"), ("fast", "strict_fast")):
    E = pd.read_pickle(f"data/research_diralpha/{f}.pkl")
    E["race"] = L.race[E.j.values]; ts = L.idx[E.t.values]; E["train"] = ts < L.SPLIT
    E["polls"] = E.race.map(pollf); E["rate"] = E.race.map(ratef); E["office"] = L.office[E.j.values]
    E["kdom"] = np.abs(L.lag(L.k, 12).values[E.t.values, E.j.values]) - np.abs(L.lag(L.pm, 12).values[E.t.values, E.j.values])
    print("===", name, len(E))
    for lab, m in (("no public polls", E.polls == 0), ("1+ polls", E.polls > 0), ("toss-up rating", E.rate.abs() <= 2), ("rated lean or more", E.rate.abs() > 2), ("Kalshi moved more", E.kdom > 0), ("Polymarket moved more", E.kdom <= 0)):
        x = E[m]
        print(f"{lab:24s} n {len(x):3d} net {100*x.net.mean():5.2f}c win {100*(x.net>0).mean():3.0f}% train {100*x[x.train].net.mean():5.2f} ({int(x.train.sum())}) test {100*x[~x.train].net.mean():5.2f} ({int((~x.train).sum())}) races {x.race.nunique()}")
