"""Drill-down 17: small upgrades on the strict trades.
 A cheaper side: the same bet can be bought as D YES or as R NO, take whichever costs less, in and out
 B bigger signal, bigger edge? scaling size to the signal
 C weekend days, the coming regime
 D size in shares rather than dollars
 E big prints (H03), the one idea not yet run directly."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, survivors, show, H
L = Lab()
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl")
Bv, Av = L.B, L.A
mate = {}
for r, g in L.meta.reset_index().groupby("race"):
    if len(g) == 2:
        a, b = list(g.exchange_id if "exchange_id" in g else g.index)
        ia, ib = L.cols.index(a) if a in L.cols else None, L.cols.index(b) if b in L.cols else None
        if ia is not None and ib is not None: mate[ia] = ib; mate[ib] = ia
for name, E in (("slow", Es), ("fast", Ef)):
    a, b, c = [], [], []
    for r in E.itertuples():
        k = mate.get(r.j)
        own_in = Av[r.e, r.j] if r.s > 0 else 1 - Bv[r.e, r.j]          # price of the side we buy on this leg
        own_out = Bv[r.x, r.j] if r.s > 0 else 1 - Av[r.x, r.j]
        a.append(own_out - own_in)
        if k is None: b.append(own_out - own_in); c.append(0); continue
        alt_in = (1 - Bv[r.e, k]) if r.s > 0 else Av[r.e, k]             # same bet through the other leg
        alt_out = (1 - Av[r.x, k]) if r.s > 0 else Bv[r.x, k]
        i_ = np.nanmin([own_in, alt_in]); o_ = np.nanmax([own_out, alt_out])
        b.append(o_ - i_); c.append(own_in - i_)
    print(f"A {name}: same-leg net {100*np.nanmean(a):.2f}c, cheaper side both ways {100*np.nanmean(b):.2f}c, entry saving {100*np.nanmean(c):.2f}c, entries improved {100*np.mean(np.array(c)>0):.0f}%")
gap = L.fair - L.mid
S48 = (gap - gap.rolling(576, min_periods=288).median()).values
df = L.lag(L.fair, 6).values
for name, E, sv in (("slow", Es, S48), ("fast", Ef, df)):
    E = E.copy(); E["sig"] = np.abs(sv[E.t.values, E.j.values]) * 100
    b = pd.qcut(E.sig, 3, duplicates="drop")
    print(f"\nB {name} by signal size"); print(E.groupby(b, observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
    E["wk"] = pd.to_datetime(L.idx[E.t.values], unit="s", utc=True).dayofweek >= 5
    print("C weekend"); print(E.groupby("wk").net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
H_ = .004
for shares in (2000, 5000, 8000, 10000, 15000, 20000, 30000):
    tot = 0; n = 0; cap = 0
    for E in (Es, Ef):
        for r in E.itertuples():
            tot += (r.net - H_ - 2 * .02 * shares / 1e5) * shares; n += 1
    print(f"D {shares:6d} shares a trade, {n} trades, no capital limit: to 3 Nov after haircut ${tot/8.5*3.9:8.0f}")
print("E big prints")
tp = pd.read_pickle("data/research_diralpha/tape.pkl")
tp = tp[tp.aggressor.isin(["buy", "sell"]) & (tp.ts >= L.idx[0])]
tp["n"] = (tp.quantity.abs() * tp.price); tp["sq"] = np.where(tp.aggressor == "buy", 1, -1) * tp.quantity.abs()
tp["b"] = (tp.ts // 300).astype("int64") * 300
sig = {}
for th in (2000, 5000, 15000):
    x = tp[tp.n >= th]
    S = x.groupby(["b", "exchange_id"]).sq.sum().unstack().reindex(L.idx).reindex(columns=L.cols)
    sig[f"H03big:{th}"] = S; sig[f"H03bigfade:{th}"] = -S
A = pd.concat([L.test(k, s, hs=("1h", "4h", "12h", "24h"), qs=(0.0,), offices=(None, "senate", "governor", "house"), family=k.split(":")[0]) for k, s in sig.items()], ignore_index=True)
print("survivors", len(survivors(A, min_n=15))); show(A[A.get("test_n", 0) >= 15].sort_values("test_net_c", ascending=False), 6)
print(A.groupby(["family", "h"]).test_net_c.median().unstack().round(2).to_string())
