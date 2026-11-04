"""Drill-down 1: the lag family (outside moved, SIG has not). Is it one event, one side, one party? Does it hold on every day?"""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, show, H
L = Lab()
mid, gap = L.mid, L.gap
S = {"fresh48": gap - gap.rolling(576, min_periods=288).median(), "fresh24": gap - gap.rolling(288, min_periods=144).median(), "lag24": L.lag(L.fair, 288) - L.lag(mid, 288), "lag12": L.lag(L.fair, 144) - L.lag(mid, 144)}
ho = np.ones((len(L.idx), len(L.cols)), bool); ho[:, L.office != "house"] = False
pd.set_option("display.width", 200, "display.max_rows", 300)
for nm, s in S.items():
    for off, M in (("house", ho), ("all", None)):
        for hn in ("12h", "24h"):
            for q in (0.9, 0.95, 0.98):
                tr = L.trades(s, H[hn], q=q, mask=M)
                if tr is None: continue
                d = pd.DataFrame(tr, columns=["j", "i", "s", "gross", "net"])
                d["day"] = L.day[d.i.astype(int)]; d["race"] = L.race[d.j.astype(int)]; d["party"] = L.party[d.j.astype(int)]
                d["dside"] = d.s * np.where(d.party == "D", 1, -1)
                d["p0"] = L.mid.values[d.i.astype(int), d.j.astype(int)]
                if (nm, off, hn, q) in (("fresh48", "house", "24h", 0.95), ("lag24", "house", "24h", 0.95), ("lag24", "all", "24h", 0.95)):
                    print(f"\n=== {nm} {off} hold {hn} q{q}: n {len(d)} net {100*d.net.mean():.2f}c win {100*(d.net>0).mean():.0f}%")
                    print("by day"); print(d.groupby("day").agg(n=("net", "size"), net_c=("net", lambda x: 100 * x.mean()), gross_c=("gross", lambda x: 100 * x.mean()), dside=("dside", "mean")).round(2).to_string())
                    print("by D side", d.groupby("dside").net.agg(["size", lambda x: 100 * x.mean()]).round(2).to_string())
                    print("by signal side", d.groupby("s").net.agg(["size", lambda x: 100 * x.mean()]).round(2).to_string())
                    print("price bucket", d.groupby(pd.cut(d.p0, [0, .1, .25, .5, .75, .9, 1])).net.agg(["size", lambda x: 100 * x.mean()]).round(2).to_string())
                    t = d.groupby("race").net.agg(["size", "sum"]).sort_values("sum"); print("worst", (100*t["sum"]).head(4).round(1).to_dict()); print("best", (100*t["sum"]).tail(6).round(1).to_dict()); print("top 5 races' share of total", round(t["sum"].tail(5).sum() / t["sum"].sum(), 2))
                print(nm, off, hn, q, "n", len(d), "net %.2f" % (100 * d.net.mean()), "win %.0f" % (100 * (d.net > 0).mean()), "days+", int((d.groupby("day").net.mean() > 0).sum()), "/", d.day.nunique())
print("\nPermutation (circular shift of the signal in time, 200 draws):")
for nm, off, hn, q, M in (("fresh48", "house", "24h", .95, None), ("lag24", "house", "24h", .95, None), ("lag12", "house", "24h", .95, None), ("lag24", None, "24h", .95, None), ("fresh48", None, "24h", .95, None)):
    print(nm, off, hn, q, L.perm_p(S[nm], hn, q, office=off, n=200), flush=True)
