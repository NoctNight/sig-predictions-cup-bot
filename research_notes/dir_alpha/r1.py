"""Round 1 and 2: signals from the leg itself and from the outside venues."""
import sys, time
import numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, survivors, show

L = Lab()
print("panel", L.mid.shape, "split", pd.Timestamp(L.SPLIT, unit="s"), "last", pd.Timestamp(L.LAST, unit="s"), flush=True)
P = L.P
mid, gap = L.mid, L.gap
sf, vol = P["sflow"], P["vol"]
LB = {"15m": 3, "1h": 12, "4h": 48, "12h": 144, "24h": 288, "48h": 576}
sig = {}
for n, k in LB.items():
    r = L.lag(mid, k)
    sig[f"H01mom:{n}"] = r
    sig[f"H01rev:{n}"] = -r
for n in ("1h", "4h", "12h", "24h"):
    k = LB[n]
    fl = L.rsum(sf, k)
    v = L.rsum(vol, k)
    sig[f"H02flow:{n}"] = fl
    sig[f"H02flowfrac:{n}"] = (fl / v.where(v > 0)).where(v >= 200)
    sig[f"H02fade:{n}"] = -fl
    # flow against price
    r = L.lag(mid, k)
    sig[f"H04absorb:{n}"] = (-fl).where(np.sign(fl) != np.sign(r)).where(r != 0)
    sig[f"H05volshock:{n}"] = (np.sign(r) * (v / (L.rsum(vol, 576) / 576 * k).where(lambda x: x > 0))).where(v >= 500)
    sig[f"H05volshockfade:{n}"] = -sig[f"H05volshock:{n}"]
inv = L.rsum(sf, 864)
sig["H06inv3d"] = inv
sig["H06inv3dfade"] = -inv
for n in ("1h", "4h", "12h", "24h"):
    k = LB[n]
    df, dm = L.lag(L.fair, k), L.lag(mid, k)
    sig[f"H08lag:{n}"] = df - dm
    sig[f"H11trend:{n}"] = df
    dp, dk = L.lag(L.pm, k), L.lag(L.k, k)
    sig[f"H09pm:{n}"] = (dp - dm)
    sig[f"H09k:{n}"] = (dk - dm)
    sig[f"H09pmonly:{n}"] = (dp - dk)
for n in ("48h", "72h"):
    sig[f"H11trend:{n}"] = L.lag(L.fair, {"48h": 576, "72h": 864}[n])
g = gap
sig["H07gap"] = g
for n, k in (("4h", 48), ("24h", 288), ("48h", 576)):
    sig[f"H07fresh:{n}"] = g - g.rolling(k, min_periods=k // 2).median()
sig["H12agree"] = g.where((L.pm - L.k).abs() <= 0.02)
sig["H12disagree"] = g.where((L.pm - L.k).abs() > 0.02)
fl4 = L.rsum(sf, 48)
sig["H13gapflow"] = g.where(np.sign(g) == np.sign(fl4)).where(fl4.abs() >= 200)
bs, az = P["pm_bid_sz"], P["pm_ask_sz"]
sig["H10pmimb"] = ((bs - az) / (bs + az).where(lambda x: x > 0))
bq, aq = P["bid_qty"], P["ask_qty"]
sig["H24sigimb"] = ((bq - aq) / (bq + aq).where(lambda x: x > 0))
all_, t0 = [], time.time()
for i, (k, s) in enumerate(sig.items()):
    d = L.test(k, s, family=k.split(":")[0])
    all_.append(d)
    if i % 20 == 0:
        print(i, k, round(time.time() - t0), flush=True)
A = pd.concat(all_, ignore_index=True)
A.to_pickle("data/research_diralpha/r1.pkl")
print("tests", L.n_tests, "signals", len(sig))
S = survivors(A)
print("survivors", len(S)); show(S, 40)
print("\nBest test-day net, regardless of t:"); show(A[A.get("test_n", 0) >= 30].sort_values("test_net_c", ascending=False), 25)
print("\nBest by test t_race and train agrees:")
B = A[(A.train_net_c > 0) & (A.test_net_c > 0) & (A.test_n >= 30)].sort_values("test_t_day", ascending=False)
show(B, 25)
# by family: median test net at q=.95 and h=4h/12h
print(A[A.q == 0.95].groupby(["family", "h"]).test_net_c.median().unstack().round(2).to_string())
