"""Value of the lag book to 3 Nov on the unseen-day edge. Edges: all signals slow +0.6c, fast +0.8c at 8 h; 2+ points and under 70c about +1.2c (fast +1.0c-ish, thin). Cost 0.02c per 1,000 shares each way. Signals from the strict list, scaled to 3.9 days."""
import pandas as pd
A = pd.concat([pd.read_pickle("data/research_diralpha/book.pkl")]) if False else None
days = 3.9 / 8.5
for lab, n, e in (("all signals", 86 * days, .65), ("2+ points", 39 * days, 1.2)):
    for sh in (2500, 5000, 10000, 15000, 20000):
        c = 2 * .02 * sh / 1000
        print(f"{lab}: {n:.0f} trades at {sh} shares, edge {e}c, cost {c:.2f}c: ${n*sh*(e-c)/100:.0f}")
