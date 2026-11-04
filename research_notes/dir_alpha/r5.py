"""Round 5: events. Sweeps across many legs, single-leg sweeps, repeated clips, news, and our own past signals."""
import sys, sqlite3, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, survivors, show, H
L = Lab()
mid, P = L.mid, L.P
ds = L.dsign
sf, vol = P["sflow"], P["vol"]
sig = {}
idx = pd.Index(L.idx)
empty = lambda: pd.DataFrame(np.nan, index=mid.index, columns=mid.columns)
# H25 multi-leg sweeps: same D direction, 5-min bin, many legs, big flow
flow_notional = sf * mid
dfl = flow_notional.mul(ds, axis=1)
for n_legs in (3, 5, 8):
    for size in (300, 1000):
        up = (dfl >= size).sum(axis=1); dn = (dfl <= -size).sum(axis=1)
        ev = (up >= n_legs).astype(float) - (dn >= n_legs).astype(float)
        hit = (dfl.abs() >= size)
        S = hit.astype(float).mul(ev, axis=0).mul(ds, axis=1).where(lambda x: x != 0)
        sig[f"H25multifade:{n_legs}:{size}"] = -S
        sig[f"H25multifollow:{n_legs}:{size}"] = S
# H26 single-leg sweeps: 5- and 15-min moves with volume
for k, kn in ((1, "5m"), (3, "15m")):
    r = L.lag(mid, k)
    v = L.rsum(vol, k) * mid
    for mv in (.01, .015, .025):
        S = r.where((r.abs() >= mv) & (v >= 2000))
        sig[f"H26sweepfade:{kn}:{mv}"] = -S
        sig[f"H26sweepfollow:{kn}:{mv}"] = S
        Sq = r.where((r.abs() >= mv) & (v < 500))
        sig[f"H26quietmove:{kn}:{mv}"] = Sq
        sig[f"H26quietfade:{kn}:{mv}"] = -Sq
# H27 repeated clips: same size, same aggressor, same leg, 3+ in 30 min
tp = pd.read_pickle("data/research_diralpha/tape.pkl")
tp = tp[tp.aggressor.isin(["buy", "sell"])].sort_values("ts")
tp["q"] = tp.quantity.abs().round(0)
tp["b"] = (tp.ts // 300).astype("int64") * 300
out = []
for (ex, ag, q), g in tp[tp.q >= 50].groupby(["exchange_id", "aggressor", "q"]):
    if len(g) < 3: continue
    t = g.ts.values
    for i in range(2, len(t)):
        if t[i] - t[i - 2] <= 1800:
            out.append((g.b.values[i], ex, 1.0 if ag == "buy" else -1.0, q))
c = pd.DataFrame(out, columns=["b", "ex", "s", "q"])
print("clip events", len(c), flush=True)
Sc = empty()
for r in c.itertuples():
    if r.ex in Sc.columns and r.b in Sc.index:
        Sc.at[r.b, r.ex] = r.s
sig["H27clipfollow"] = Sc
sig["H27clipfade"] = -Sc
# H28 news events
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
ne = pd.read_sql_query("select ts,exchange_id,race,party,direction,expected_move_pts,confidence,mid_before from news_events where exchange_id is not null", db)
print("news", len(ne), ne.direction.value_counts().to_dict(), ne.confidence.describe().round(2).to_dict(), flush=True)
ne["b"] = (ne.ts // 300).astype("int64") * 300 + 300
for cf in (0.0, .7, .85):
    Sn = empty()
    for r in ne[ne.confidence >= cf].itertuples():
        d = {"up": 1.0, "down": -1.0}.get(str(r.direction).lower(), 0.0)
        if d and r.exchange_id in Sn.columns and r.b in Sn.index:
            Sn.at[r.b, r.exchange_id] = d * max(float(r.expected_move_pts or 1), .1)
    sig[f"H28news:{cf}"] = Sn
    sig[f"H28newsfade:{cf}"] = -Sn
A_ = []
for i, (k, s) in enumerate(sig.items()):
    q = (0.0,) if ("clip" in k or "news" in k or "H25" in k or "H26" in k) else (.9,)
    d = L.test(k, s, hs=("1h", "4h", "12h", "24h"), qs=(0.0,), offices=(None, "senate", "governor", "house"), family=k.split(":")[0])
    A_.append(d)
    if i % 10 == 0:
        print(i, k, flush=True)
A = pd.concat(A_, ignore_index=True); A.to_pickle("data/research_diralpha/r5.pkl")
print("tests", L.n_tests, "signals", len(sig))
S = survivors(A, min_n=20); print("survivors", len(S)); show(S, 30)
print("\nBest test net, n >= 20:"); show(A[A.get("test_n", 0) >= 20].sort_values("test_net_c", ascending=False), 25)
print(A.groupby(["family", "h"]).test_net_c.median().unstack().round(2).to_string())
