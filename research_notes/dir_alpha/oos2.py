"""Out-of-sample check of the conviction count and the filters on 3-20 Oct trades. Points as frozen: no public polls, toss-up rating, leg unchanged 2 h+ (last trade here, quote there),
signal in the upper two thirds (slow 2.18c, fast 2.28c). Polls and ratings are today's values, so this is slightly flattered: a poll count from 31 Oct is applied to mid-October."""
import json, pickle, numpy as np, pandas as pd
pd.set_option("display.width", 200, "display.max_rows", 100)
O = "data/research_diralpha/oos/"
P = pickle.load(open(O + "panel.pkl", "rb")); S = P["S"]; Mv = S.values; race = P["race"]; n = Mv.shape[0]
fm = json.load(open("params/fair_model.json"))["races"]
age = np.zeros(Mv.shape)
for j in range(Mv.shape[1]):
    last = 0
    for i in range(1, n):
        if np.isfinite(Mv[i, j]) and Mv[i, j] != Mv[i - 1, j]: last = i
        age[i, j] = (i - last) / 12
def pts(E, thr):
    ip = [fm.get(x, {}).get("inputs", {}) or {} for x in race[E.j.values]]
    c1 = np.array([1 if p.get("n_polls") == 0 else 0 for p in ip]); c2 = np.array([1 if (p.get("rating_margin") is not None and abs(p["rating_margin"]) <= 2) else 0 for p in ip])
    c3 = (age[E.t.values, E.j.values] >= 2).astype(int); c4 = (E.sig.values >= thr).astype(int)
    return c1, c2, c3, c4, c1 + c2 + c3 + c4
def show(lab, E, col="net"):
    print(lab); print(E.groupby(E.score.clip(upper=3)).agg(n=(col, "size"), races=("race", "nunique"), net=(col, lambda v: 100 * v.mean()), se=(col, lambda v: 100 * v.std() / np.sqrt(len(v))), win=(col, lambda v: (v > 0).mean())).round(2).to_string())
E = pd.read_pickle(O + "slow36.pkl"); a, b, c, d, E["score"] = pts(E, .0218); E["c1"] = a; E["c2"] = b; E["c3"] = c; E["c4"] = d
print("SLOW all", len(E), "gross %.2fc net %.2fc" % (100 * E.gross.mean(), 100 * E.net.mean())); show("slow by points", E)
for k, nm in (("c1", "no polls"), ("c2", "toss-up"), ("c3", "unchanged 2 h"), ("c4", "upper two thirds")): print(nm, E.groupby(k).net.agg(n="size", net=lambda v: 100 * v.mean()).round(2).to_dict())
print("slow 2+ points, own side under 70c:", (lambda q: (len(q), round(100 * q.net.mean(), 2), round((q.net > 0).mean(), 2)))(E[(E.score >= 2) & (E.own < .7)]))
F = pd.read_pickle(O + "fast6.pkl"); x = np.minimum(F.e.values + 96, n - 1)
F["net8"] = F.s * (Mv[x, F.j.values] - Mv[F.e.values, F.j.values]) - .0075
a, b, c, d, F["score"] = pts(F, .0228); show("fast by points (4 h)", F); show("fast by points (8 h)", F.dropna(subset=["net8"]), "net8")
print("overall by week"); E["wk"] = pd.to_datetime(E.day + "-2026", format="%m-%d-%Y").dt.isocalendar().week; print(E.groupby("wk").net.agg(n="size", net=lambda v: 100 * v.mean()).round(2).to_string())
