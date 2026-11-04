"""Round 7: getting ahead of the signal, and splitting what it is made of.
 B1 both-venue momentum after SIG has caught up   B2 one venue moved, the other has not (which leads)
 B3 SIG-only move: fade or follow                 B4 one-venue move that SIG copied: fade
 B5 inactivity plus gap                            B6 pairs: directional pairs flow (A13)     B7 skilled signature screen (A12)
 B8 our own model against SIG (H30)                B9 gap source: outside-driven or SIG-driven """
import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, survivors, show, H
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
pm, k_ = L.pm, L.k
sig = {}
S48 = gap - gap.rolling(576, min_periods=288).median()
for k, n in ((12, "1h"), (48, "4h"), (144, "12h"), (288, "24h")):
    dp, dk, dm, df = L.lag(pm, k), L.lag(k_, k), L.lag(mid, k), L.lag(fair, k)
    s = np.sign(dp)
    same = (np.sign(dk) == s) & (dp.abs() >= .015) & (dk.abs() >= .015)
    sig[f"B1boththrough:{n}"] = df.where(same & ((dm * s) >= .8 * (df * s)))             # SIG already caught up
    sig[f"B1bothlag:{n}"] = df.where(same & ((dm * s) < .5 * (df * s)))
    sig[f"B2pmfirst:{n}"] = dp.where((dp.abs() >= .02) & (dk.abs() < .005))
    sig[f"B2kfirst:{n}"] = dk.where((dk.abs() >= .02) & (dp.abs() < .005))
    sigonly = (dm.abs() >= .015) & (df.abs() < .005)
    sig[f"B3sigonlyfade:{n}"] = (-dm).where(sigonly)
    sig[f"B3sigonlyfollow:{n}"] = dm.where(sigonly)
    onev = ((dp.abs() >= .02) & (dk.abs() < .005)) | ((dk.abs() >= .02) & (dp.abs() < .005))
    sig[f"B4onevfade:{n}"] = (-dm).where(onev & (np.sign(dm) == np.sign(df)) & (dm.abs() >= .01))
ntr = P["ntr"]
quiet = L.rsum(ntr, 72) == 0
sig["B5quietgap"] = S48.where(quiet)
sig["B5busygap"] = S48.where(L.rsum(ntr, 72) >= 20)
df24, dm24 = L.lag(fair, 288), L.lag(mid, 288)
sig["B9outsidedriven"] = S48.where(df24.abs() >= 2 * dm24.abs())
sig["B9sigdriven"] = S48.where(dm24.abs() >= 2 * df24.abs())
# B8 own model
fm = json.load(open("params/fair_model.json"))
print("fair model keys", list(fm)[:6], str(fm.get("generated_at"))[:30])
tbl = fm.get("races") or fm.get("table") or fm
rows = []
if isinstance(tbl, dict):
    for r, v in tbl.items():
        if isinstance(v, dict):
            for pa in ("D", "R"):
                p = v.get(f"p_{pa}") or v.get(pa) or (v.get("probs", {}) or {}).get(pa)
                if isinstance(p, (int, float)): rows.append((r, pa, p))
print("model rows", len(rows), rows[:3])
if rows:
    mm = pd.DataFrame(rows, columns=["race", "party", "p"])
    key = {(a, b): e for e, a, b in zip(L.meta.index, L.meta.race, L.meta.party)}
    Sm = pd.DataFrame(np.nan, index=mid.index, columns=mid.columns)
    for r in mm.itertuples():
        e = key.get((r.race, r.party))
        if e in Sm.columns: Sm[e] = r.p - mid[e]
    sig["B8modelgap"] = Sm
    sig["B8modelvsfair"] = Sm.where(np.sign(Sm) == np.sign(gap))
# B6 directional pairs
tp = pd.read_pickle("data/research_diralpha/tape.pkl")
tp = tp[tp.aggressor.isin(["buy", "sell"]) & (tp.ts >= L.idx[0])].sort_values("ts")
tp["q"] = tp.quantity.abs(); tp["sg"] = np.where(tp.aggressor == "buy", 1, -1)
exs = set(L.cols); tp = tp[tp.exchange_id.isin(exs)]
tp["race"] = tp.exchange_id.map(dict(zip(L.cols, L.race))); tp["party"] = tp.exchange_id.map(dict(zip(L.cols, L.party)))
pair = []
for r, g in tp[tp.party.isin(["D", "R"])].groupby("race"):
    t, q, s, pa, ex = g.ts.values, g.q.values, g.sg.values, g.party.values, g.exchange_id.values
    j = 0
    for i in range(len(t)):
        while t[i] - t[j] > 3: j += 1
        for m_ in range(j, i):
            if pa[m_] != pa[i] and abs(q[m_] - q[i]) <= .1 * q[i]:
                dd = s[i] * (1 if pa[i] == "D" else -1), s[m_] * (1 if pa[m_] == "D" else -1)
                if dd[0] == dd[1]:                                # buy D with sell R (or the reverse): a view
                    pair.append((t[i], r, dd[0] * q[i]))
                break
pair = pd.DataFrame(pair, columns=["t", "race", "dq"])
print("directional pairs", len(pair), flush=True)
pair["b"] = (pair.t // 300).astype("int64") * 300
flow = pair.groupby(["b", "race"]).dq.sum().unstack().reindex(L.idx).fillna(0.0)
for k, n in ((72, "6h"), (288, "24h")):
    rs = flow.rolling(k, min_periods=1).sum()
    S_ = pd.DataFrame(np.nan, index=mid.index, columns=mid.columns)
    for e in L.cols:
        r = L.meta.race[e]
        if r in rs.columns and L.dsign[e] != 0: S_[e] = rs[r] * L.dsign[e]
    S_ = S_.where(S_ != 0)
    sig[f"B6pairflow:{n}"] = S_
    sig[f"B6pairfade:{n}"] = -S_
# B7 signatures: size bucket + second of minute; rank by 6 h markout on the first half, follow the best on the second
tp["qb"] = np.round(np.log2(tp.q.clip(lower=1)) * 2) / 2
tp["sec"] = (tp.ts % 60).astype(int)
tp["cl"] = tp.qb.astype(str) + "_" + (tp.sec // 5).astype(str)
tp["b"] = (tp.ts // 300).astype("int64") * 300
pos = pd.Series(range(len(L.idx)), index=L.idx)
tp["ib"] = tp.b.map(pos)
tp = tp[tp.ib.notna()]
tp["ib"] = tp.ib.astype(int)
tp["j"] = tp.exchange_id.map({e: i for i, e in enumerate(L.cols)})
mv = L.mid.values
h = 72
ok = tp.ib.values + h < mv.shape[0]
fw = np.full(len(tp), np.nan)
fw[ok] = tp.sg.values[ok] * (mv[tp.ib.values[ok] + h, tp.j.values[ok]] - mv[tp.ib.values[ok], tp.j.values[ok]])
tp["fw"] = fw
tr_ = tp[tp.ts < L.SPLIT]
c = tr_.groupby("cl").fw.agg(["size", "mean", "std"])
c = c[c["size"] >= 80]
c["t"] = c["mean"] / (c["std"] / np.sqrt(c["size"]))
top = c.sort_values("t", ascending=False).head(max(5, len(c) // 20)).index
print("clusters", len(c), "top t", c.t.max().round(1), "same clusters in test, mean markout c", round(100 * tp[(tp.ts >= L.SPLIT) & tp.cl.isin(top)].fw.mean(), 3), "n", int(((tp.ts >= L.SPLIT) & tp.cl.isin(top)).sum()), "all test", round(100 * tp[tp.ts >= L.SPLIT].fw.mean(), 3), flush=True)
x = tp[tp.cl.isin(top)]
F = pd.DataFrame(0.0, index=L.idx, columns=L.cols)
ag = (x.sg * x.q).groupby([x.b, x.exchange_id]).sum().unstack().reindex(L.idx).reindex(columns=L.cols).fillna(0.0)
sig["B7skill:6h"] = ag.rolling(72, min_periods=1).sum().where(lambda v: v != 0)
rows = []
for i, (k, s) in enumerate(sig.items()):
    rows.append(L.test(k, s, hs=("1h", "4h", "12h", "24h"), qs=(0.0, .8), offices=(None, "house"), family=k.split(":")[0]))
    if i % 10 == 0: print(i, k, flush=True)
A = pd.concat(rows, ignore_index=True); A.to_pickle("data/research_diralpha/r7.pkl")
print("tests", L.n_tests, "signals", len(sig))
Sv = survivors(A, min_n=15); print("survivors", len(Sv)); show(Sv, 40)
print("\nBest test net, n >= 15:"); show(A[A.get("test_n", 0) >= 15].sort_values("test_net_c", ascending=False), 20)
print(A.groupby(["family", "h"]).test_net_c.median().unstack().round(2).to_string())
