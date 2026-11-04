"""Drill-down 23 (round 3, ideas 1 and 2): use the lag signal to time sales we must make anyway. Current positions, every strict event of the backtest.
 Against: we hold the side the signal says will fall. Sell now rather than later: saves the mid move, no spread paid twice, no new capital, no cap trouble.
 Leg-out: we hold a set in the race. Sell the falling leg now, the rising one after the move. Leaves a naked leg for the hold, moves net D.
 Rich first: among legs we hold, does SIG above outside predict a fall? (the level, not the change)."""
import sys, sqlite3, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
pos = pd.read_sql_query("select exchange_id ex, sum(quantity) q from fills group by 1", db).set_index("ex").q
pos.index = pos.index.astype(str)
p = pos.reindex(L.cols).fillna(0).values
print("legs held", int((np.abs(p) >= 1).sum()), "gross shares", int(np.abs(p).sum()))
mate = {}
for r, g in pd.Series(range(len(L.cols))).groupby(L.race):
    if len(g) == 2: a, b = g.values; mate[a] = b; mate[b] = a
mid = L.mid.values
Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl")
Hh = {"slow": 288, "fast": 96}
span = (L.idx[-1] - L.idx[0]) / 86400
for nm, E in (("slow", Es), ("fast", Ef)):
    h = Hh[nm]; rows = []
    for r in E.itertuples():
        j = r.j; k = mate.get(j); e = r.e; x = min(e + h, mid.shape[0] - 1)
        mj = r.s * (mid[x, j] - mid[e, j])
        if not np.isfinite(mj): continue
        mk = -r.s * (mid[x, k] - mid[e, k]) if k is not None else np.nan
        pj, pk = p[j], (p[k] if k is not None else 0.0)
        ag = max(0, -pj * r.s) * mj + (max(0, pk * r.s) * mk if k is not None and np.isfinite(mk) else 0)
        sh = max(0, -pj * r.s) + max(0, pk * r.s)
        wi = max(0, pj * r.s) + max(0, -pk * r.s)
        st = 0.0
        if k is not None and pj * pk > 0: st = min(abs(pj), abs(pk))
        rows.append(dict(race=L.race[j], m=mj, ag=ag, sh=sh, wi=wi, setq=st, lo=st * mj))
    Q = pd.DataFrame(rows)
    print(f"\n{nm}: events {len(Q)} mid move {100*Q.m.mean():.2f}c; events where we hold against {int((Q.sh>=1).sum())}, shares {Q.sh.sum():.0f}, saved ${Q.ag.sum():.0f}; with the signal {int((Q.wi>=1).sum())}, shares {Q.wi.sum():.0f}; events on a set {int((Q.setq>=1).sum())}, sets {Q.setq.sum():.0f}, leg-out gain ${Q.lo.sum():.0f}")
    print(f"  per remaining 3.5 days: sell-against ${Q.ag.sum()/span*3.5:.0f}, leg-out ${Q.lo.sum()/span*3.5:.0f}; after 0.4c haircut sell-against ${(Q.ag.sum()-.004*Q.sh.sum())/span*3.5:.0f}")
    print(Q[Q.sh >= 50].sort_values("sh", ascending=False).head(8)[["race", "m", "sh", "ag"]].round(3).to_string(index=False))
    print(Q[Q.setq >= 50].sort_values("setq", ascending=False).head(8)[["race", "m", "setq", "lo"]].round(3).to_string(index=False))
# rich first: for legs we hold now, fall over the next 24/48 h against SIG minus outside (level), last 4 days
gap = (L.mid - L.fair)
idx = np.abs(p) >= 100
t0 = np.searchsorted(L.idx, L.idx[-1] - 4 * 86400)
for h in (288, 576):
    rows = []
    for t in range(t0, mid.shape[0] - h, 72):
        g = gap.values[t, :]; f = mid[t + h, :] - mid[t, :]
        sg = np.sign(p)
        ok = idx & np.isfinite(g) & np.isfinite(f)
        rows.append(pd.DataFrame(dict(rich=g[ok] * sg[ok], fwd=f[ok] * sg[ok], q=np.abs(p[ok]))))
    R = pd.concat(rows)
    b = pd.cut(R.rich, [-1, -.02, -.01, 0, .01, .02, 1])
    print(f"\nlegs we hold, side value change over {h//12} h by how rich our side is on SIG against outside"); print(R.groupby(b, observed=True).fwd.agg(n="size", move=lambda v: 100 * v.mean()).round(2).to_string())
