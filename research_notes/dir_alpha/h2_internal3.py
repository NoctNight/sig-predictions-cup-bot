"""Round-2 quick checks, part 3: leg staleness as a filter on the slow rule, and whether holds can shorten now that SIG copies sooner."""
import sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
Mv = L.mid.values; B, A = L.B, L.A
n = Mv.shape[0]
Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl"); Ef["rule"] = "fast"
Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Es["rule"] = "slow"
E = pd.concat([Ef, Es], ignore_index=True)
for x in ["j", "t", "e", "x"]:
    E[x] = E[x].astype(int)
E["race"] = L.race[E.j]; E["day"] = L.day[E.t]; E["train"] = L.idx[E.t] < L.SPLIT


def age(j, t):
    ch = np.flatnonzero(np.abs(np.diff(Mv[:t + 1, j])) > 1e-9)
    return (t - ch[-1]) * 5 if len(ch) else np.nan


E["age"] = [age(j, t) for j, t in zip(E.j, E.t)]
s = E[E.rule == "slow"].copy()
s["stale"] = s.age > 120
print(s.groupby("stale").net.agg(n="size", net=lambda v: round(100 * v.mean(), 2), win=lambda v: round((v > 0).mean(), 2)).to_string())
print(s.groupby(["train", "stale"]).net.agg(n="size", net=lambda v: round(100 * v.mean(), 2)).to_string())
print("day means:", s.groupby(["day", "stale"]).net.mean().mul(100).round(1).unstack().to_string())
print("races stale", s[s.stale].race.nunique(), "office", pd.Series(L.office[s.j[s.stale]]).value_counts().to_dict(), "| not stale office", pd.Series(L.office[s.j[~s.stale]]).value_counts().to_dict())
print("house only:", s[L.office[s.j] == "house"].groupby("stale").net.agg(["size", lambda v: round(100 * v.mean(), 2)]).to_string())
for cut in (30, 60, 120, 240):
    a, b = s[s.age > cut], s[s.age <= cut]
    print(f"cut {cut}m: above {len(a)} {100*a.net.mean():.2f}c, below {len(b)} {100*b.net.mean():.2f}c")
print("\nnet by hold and period (strict entries, touch both ways)")
E["per"] = np.where(E.day <= "10-26", "to 26 Oct", "27 Oct on")
rows = []
for rule, hs in (("fast", (1, 2, 4, 8)), ("slow", (4, 8, 12, 24))):
    for hh in hs:
        k = hh * 12
        for per, g in E[E.rule == rule].groupby("per"):
            v = []
            for r in g.itertuples():
                x = r.e + k
                if x >= n: continue
                b1, a1 = B[x, r.j], A[x, r.j]
                v.append((b1 - A[r.e, r.j]) if r.s > 0 else (B[r.e, r.j] - a1))
            rows.append(dict(rule=rule, hold=hh, per=per, n=len(v), net=round(100 * np.nanmean(v), 2)))
print(pd.DataFrame(rows).pivot_table(index=["rule", "hold"], columns="per", values=["net", "n"]).to_string())
