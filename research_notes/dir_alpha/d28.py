"""Drill-down 28 (round 3, ideas 7, 16 and new ones): cuts on the frozen strict trades.
 7 outside spread and depth at trigger. 16 open seat, partisan lean, ratings disagreement.
 s legs that were slow in the first half predict edge in the second half.
 v gap still widening or already narrowing at trigger. y the last signal in this race won or lost.
 t our own MM quote is the touch we would lift."""
import sys, json, sqlite3, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); P = L.P
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
Es = pd.read_pickle("data/research_diralpha/r3_slow.pkl").reset_index(drop=True); Ef = pd.read_pickle("data/research_diralpha/r3_fast.pkl").reset_index(drop=True)
fm = json.load(open("params/fair_model.json"))["races"]
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
gap = L.gap; S48 = gap - gap.rolling(576, min_periods=288).median(); Sv = S48.values; Mv = L.mid.values
Fv = L.fair.values; ST = (S48 - S48.shift(12)).values
# s: slowness in the first half: share of the 24 h outside move copied, by leg
half = np.searchsorted(L.idx, L.SPLIT)
dF, dM = (L.fair - L.fair.shift(288)), (L.mid - L.mid.shift(288))
slowb = {}
for c, j in zip(L.cols, range(len(L.cols))):
    x, y = dF.iloc[:half, j].values[::36], dM.iloc[:half, j].values[::36]
    ok = np.isfinite(x) & np.isfinite(y) & (np.abs(x) >= .01)
    slowb[j] = np.polyfit(x[ok], y[ok], 1)[0] if ok.sum() >= 8 else np.nan


def cut(nm, E, col, bins=None, q=None, labels=None):
    x = E[col]
    g = pd.qcut(x, q, duplicates="drop") if q else (pd.cut(x, bins, labels=labels) if bins is not None else x)
    ts = L.idx[E.t.values]
    o = E.groupby(g, observed=True).agg(n=("net", "size"), net=("net", lambda v: 100 * v.mean()), win=("net", lambda v: (v > 0).mean()))
    o["train"] = E[ts < L.SPLIT].groupby(g, observed=True).net.mean() * 100; o["test"] = E[ts >= L.SPLIT].groupby(g, observed=True).net.mean() * 100
    print(f"\n{nm} by {col}"); print(o.round(2).to_string())


for nm, E in (("slow", Es), ("fast", Ef)):
    j, t = E.j.values, E.t.values
    rows = []
    for r in E.itertuples():
        T = float(L.idx[r.t]); race, party = str(L.race[r.j]), str(L.party[r.j])
        q = pd.read_sql_query("select venue,bid,ask,depth_usd from venue_quotes where race=? and party=? and venue in ('polymarket','kalshi') and ts between ? and ?", db, params=(race, party, T - 900, T + 300))
        if q.empty: rows.append((np.nan, np.nan, np.nan, np.nan)); continue
        g = q.groupby("venue").median(numeric_only=True); sp = (g.ask - g.bid) * 100
        rows.append((sp.get("polymarket", np.nan), sp.get("kalshi", np.nan), g.depth_usd.get("polymarket", np.nan), g.depth_usd.get("kalshi", np.nan)))
    Q = pd.DataFrame(rows, columns=["pm_sp", "k_sp", "pm_dep", "k_dep"]); E = E.join(Q)
    E["wide"] = E[["pm_sp", "k_sp"]].max(axis=1); E["thin"] = E[["pm_dep", "k_dep"]].min(axis=1)
    print(nm, "venue coverage", int(E.wide.notna().sum()), "of", len(E))
    cut(nm, E, "wide", q=3); cut(nm, E, "thin", q=3)
    ip = [fm.get(x, {}).get("inputs", {}) or {} for x in L.race[j]]
    E["open_seat"] = [str(p.get("open_seat")) for p in ip]; cut(nm, E, "open_seat")
    E["lean"] = [abs(p.get("pvi")) if p.get("pvi") is not None else np.nan for p in ip]; cut(nm, E, "lean", q=3)
    def disag(p):
        v = p.get("ratings") or {}
        m = {"Solid D": 3, "Safe D": 3, "Likely D": 2, "Lean D": 1, "Tilt D": .5, "Tossup": 0, "Toss-up": 0, "Tilt R": -.5, "Lean R": -1, "Likely R": -2, "Solid R": -3, "Safe R": -3}
        x = [m[k] for k in v.values() if k in m]
        return np.std(x) if len(x) >= 3 else np.nan
    E["disagree"] = [disag(p) for p in ip]; cut(nm, E, "disagree", q=3)
    E["slowness"] = [slowb.get(q_, np.nan) for q_ in j]
    if nm == "slow":
        Te = E[L.idx[t] >= L.SPLIT].copy(); print("second-half trades only, by first-half copy beta of the leg"); print(Te.groupby(pd.qcut(Te.slowness, 3, duplicates="drop"), observed=True).net.agg(n="size", net=lambda v: 100 * v.mean(), win=lambda v: (v > 0).mean()).round(2).to_string())
    E["widen"] = (ST[t, j] * E.s) * 100; cut(nm, E, "widen", [-100, -.5, 0, .5, 1, 100])
    # y previous signal in the same race
    prev = []
    for i, r in enumerate(E.itertuples()):
        o = E[(L.race[E.j.values] == L.race[r.j]) & (E.t < r.t)]
        prev.append(np.nan if o.empty else float(o.iloc[-1].net > 0) * (1 if o.iloc[-1].s * L.dsign.values[o.iloc[-1].j] == r.s * L.dsign.values[r.j] else -1))
    E["prev"] = prev; print(f"\n{nm} by previous signal in the race (1 same way won, 0 same or other lost, -1 other way won)"); print(E.groupby("prev", dropna=False).net.agg(n="size", net=lambda v: 100 * v.mean()).round(2).to_string())
    # t own quote at touch at entry
    own = []
    for r in E.itertuples():
        T = float(L.idx[r.e]) + 150; ex = L.cols[r.j]; yes_side = r.s > 0
        # we lift the ask (buy yes) or hit the bid (sell yes, the same as buy no at 1-bid)
        px = L.A[r.e, r.j] if r.s > 0 else L.B[r.e, r.j]
        q = pd.read_sql_query("select side,action,price,quantity,placed_at,status from orders where exchange_id=? and purpose='mm' and placed_at between ? and ?", db, params=(str(ex), T - 900, T))
        own.append(len(q))
    E["own_orders_15m"] = own
    print(f"{nm}: MM orders placed in this leg in the 15 min before entry: {np.mean(np.array(own)>0)*100:.0f}% of trades")
    cut(nm, E, "own_orders_15m", [-1, 0, 4, 1000], labels=["none", "1-4", "5+"])
    E.to_pickle(f"data/research_diralpha/r3b_{nm}.pkl")
# combined filters on slow: own-side price >= .70 out, against-wave out, on wave events' loose counts already in r3 cluster
E = Es.copy(); ts = L.idx[E.t.values]
for lab, m_ in (("all", np.ones(len(E), bool)), ("own price < .70", E.band.values < .70), ("cluster >= -2", E.cluster.values > -2), ("both", (E.band.values < .70) & (E.cluster.values > -2))):
    e = E[m_]; tt = ts[m_]; print(f"slow {lab}: n {len(e)} net {100*e.net.mean():.2f}c win {(e.net>0).mean():.2f} train {100*e[tt<L.SPLIT].net.mean():.2f} test {100*e[tt>=L.SPLIT].net.mean():.2f}")
