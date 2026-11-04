"""Round-2 quick checks, part 2: fixes to part 1 (national drift, passive table, fill weights) plus entry state, capital use, tightening test."""
import json, sqlite3, sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
P, mid, fair = L.P, L.mid, L.fair
B, A = L.B, L.A
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 40)
Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl"); Ef["rule"] = "fast"
Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Es["rule"] = "slow"
E = pd.concat([Ef, Es], ignore_index=True)
for x in ["j", "t", "e", "x"]:
    E[x] = E[x].astype(int)
E["race"] = L.race[E.j]; E["party"] = L.party[E.j]; E["office"] = L.office[E.j]; E["day"] = L.day[E.t]
n, m = mid.shape
Mv, Fv = mid.values, fair.values
c = lambda v: round(100 * float(np.nanmean(v)), 2)

print("=== national drift (D-signed fair, legs 10-90, D and R legs only)")
legs = np.flatnonzero((L.dsign.values != 0) & (L.office != "chamber"))
ff = fair.iloc[:, legs].where((mid.iloc[:, legs] >= .1) & (mid.iloc[:, legs] <= .9))
sgn = L.dsign.values[legs]
natl = (ff * sgn).sub((ff * sgn).mean(axis=0), axis=1).mean(axis=1)       # demeaned per leg so legs dropping in and out of the band do not move it
print("end of day, c:", (100 * natl.groupby(L.day).last()).round(2).to_dict())
E["dside"] = E.s * np.where(E.party == "D", 1, np.where(E.party == "R", -1, 0))
E["drift24"] = natl.diff(288).values[E.t]
st = np.where(E.drift24.abs() < .002, "flat", np.where(np.sign(E.drift24) == E.dside, "with", "against"))
E["st"] = st
print(E.groupby(["rule", "st"]).net.agg(n="size", net=lambda v: round(100 * v.mean(), 2), win=lambda v: round((v > 0).mean(), 2)).to_string())
E["dnat_s"] = (natl.values[E.x] - natl.values[E.e]) * E.dside
print("net, national part over hold, net minus it:", E.groupby("rule").apply(lambda g: (c(g.net), c(g.dnat_s), c(g.net - g.dnat_s))).to_dict())
E["fw"] = np.where(E.dnat_s > .002, "helped", np.where(E.dnat_s < -.002, "hurt", "flat"))
print(E.groupby(["rule", "fw"]).net.agg(n="size", net=lambda v: round(100 * v.mean(), 2)).to_string())

print("\n=== net by entry state: had SIG already stepped since the move began? (fast rule: copied in the 30 min window; slow: copied in 24 h)")
rows = []
for r in E.itertuples():
    W = 6 if r.rule == "fast" else 288
    t0 = max(r.t - W, 0)
    df = r.s * (Fv[r.t, r.j] - Fv[t0, r.j]); dm = r.s * (Mv[r.t, r.j] - Mv[t0, r.j])
    v = Mv[:r.t + 1, r.j]; ch = np.flatnonzero(np.abs(np.diff(v)) > 1e-9)
    age = (r.t - ch[-1]) * 5 if len(ch) else np.nan
    rows.append(dict(rule=r.rule, net=r.net, df=df, dm=dm, frac=dm / df if df else np.nan, age=age, left=df - dm))
S = pd.DataFrame(rows)
S["nostep"] = S.dm <= 1e-9
print(S.groupby(["rule", "nostep"]).net.agg(n="size", net=lambda v: round(100 * v.mean(), 2)).to_string())
S["agebk"] = pd.cut(S.age, [-1, 5, 30, 120, 1e6], labels=["<=5m", "5-30m", "30m-2h", ">2h"])
print(S.groupby(["rule", "agebk"], observed=True).net.agg(n="size", net=lambda v: round(100 * v.mean(), 2)).to_string())
S["leftbk"] = S.groupby("rule").left.transform(lambda v: pd.qcut(v, 3, labels=False, duplicates="drop"))
print(S.groupby(["rule", "leftbk"]).agg(n=("net", "size"), left=("left", lambda v: round(100 * v.mean(), 2)), net=("net", lambda v: round(100 * v.mean(), 2))).to_string())

print("\n=== capital use. Open positions over time with exit at 8 h (fast) and 36 h (slow), return per day of capital")
E2 = E.copy()
hold = E2.x - E2.e
print("mean hold h", (hold * 5 / 60).groupby(E2.rule).mean().round(1).to_dict(), "net per trade c", E2.groupby("rule").net.mean().mul(100).round(2).to_dict())
print("net c per 24 h of capital held:", {k: round(100 * g.net.sum() / ((g.x - g.e).sum() * 5 / 1440), 2) for k, g in E2.groupby("rule")})
for f_h, s_h in ((4, 24), (8, 36)):
    op = np.zeros(n)
    for r in E2.itertuples():
        hb = (f_h if r.rule == "fast" else s_h) * 12
        op[r.e:min(r.e + hb, n)] += 1
    live = op[L.day >= "10-23"]
    print(f"hold fast {f_h} h slow {s_h} h: open positions mean {live.mean():.1f} p90 {np.quantile(live, .9):.0f} max {live.max():.0f}; at $2.5k a trade p90 capital ${2500*np.quantile(live,.9):,.0f}")

print("\n=== tightening test: copied share at 60 min, first days vs last days, both-venue 1.5c moves in 30 min")
pmv, kv = L.pm.values, L.k.values
nochm = L.office != "chamber"
ev = []
for j in np.flatnonzero(nochm):
    last = -999
    for t in range(6, n - 48):
        if t - last < 48: continue
        dp, dk = pmv[t, j] - pmv[t - 6, j], kv[t, j] - kv[t - 6, j]
        if not (np.isfinite(dp) and np.isfinite(dk)) or np.sign(dp) != np.sign(dk) or min(abs(dp), abs(dk)) < .015: continue
        if not (.15 <= Mv[t, j] <= .85): continue
        s = np.sign(dp); df = Fv[t, j] - Fv[t - 6, j]
        if not np.isfinite(df) or df * s <= 0: continue
        last = t
        base = Mv[t, j]
        ev.append(dict(j=j, t=t, s=s, day=L.day[t], df=abs(df), pre=s * (base - Mv[t - 6, j]), f15=s * (Mv[t + 3, j] - base), f60=s * (Mv[t + 12, j] - base), f240=s * (Mv[t + 48, j] - base), o60=s * (Fv[t + 12, j] - Fv[t, j]), o240=s * (Fv[t + 48, j] - Fv[t, j]), net240=(B[t + 48, j] - A[t + 1, j]) if s > 0 else (B[t + 1, j] - A[t + 48, j])))
V = pd.DataFrame(ev)
V["rem"] = V.df - V.pre
V["per"] = np.where(V.day <= "10-26", "22-26 Oct", "27-30 Oct")
g = V.groupby("per")
print(g.agg(n=("df", "size"), df=("df", lambda v: 100 * v.mean()), pre=("pre", lambda v: 100 * v.mean()), rem=("rem", lambda v: 100 * v.mean()), f15=("f15", lambda v: 100 * v.mean()), f60=("f60", lambda v: 100 * v.mean()), f240=("f240", lambda v: 100 * v.mean()), o240=("o240", lambda v: 100 * v.mean()), net240=("net240", lambda v: 100 * v.mean())).round(2).to_string())
dd = V.groupby("day").agg(pre=("pre", "mean"), df=("df", "mean"), f60=("f60", "mean"), net=("net240", "mean"), rem=("rem", "mean"))
dd["prefrac"] = dd.pre / dd.df; dd["f60_of_rem"] = dd.f60 / dd.rem
print(dd[["prefrac", "f60_of_rem", "net"]].round(3).T.to_string())
early, late = dd[dd.index <= "10-26"], dd[dd.index > "10-26"]
print("day-mean prefrac", round(early.prefrac.mean(), 3), round(late.prefrac.mean(), 3), "diff SE", round(np.sqrt(early.prefrac.var() / len(early) + late.prefrac.var() / len(late)), 3))
print("day-mean f60 of remaining", round(early.f60_of_rem.mean(), 3), round(late.f60_of_rem.mean(), 3), "diff SE", round(np.sqrt(early.f60_of_rem.var() / len(early) + late.f60_of_rem.var() / len(late)), 3))
print("day-mean net at 4 h c", round(100 * early.net.mean(), 2), round(100 * late.net.mean(), 2))

print("\n=== passive first: base vs post then cross. Filled = print through our price (strict) or at it (loose)")
tp = pd.read_pickle("data/research_diralpha/tape.pkl")[["exchange_id", "price", "ts"]]
tp["exchange_id"] = tp.exchange_id.astype(str)
tg = {k: g.sort_values("ts") for k, g in tp.groupby("exchange_id")}
rows = []
for wait in (1, 3, 6, 12):
    out = []
    for r in E.itertuples():
        g_ = tg.get(str(L.cols[r.j]))
        if g_ is None: continue
        t0 = L.idx[r.e]; t1 = t0 + wait * 300
        p = B[r.e, r.j] if r.s > 0 else A[r.e, r.j]
        ee = min(r.e + wait, n - 1)
        w = g_[(g_.ts > t0) & (g_.ts <= t1)]
        tight = (w.price < p - 1e-9) if r.s > 0 else (w.price > p + 1e-9)
        loose = (w.price <= p + 1e-9) if r.s > 0 else (w.price >= p - 1e-9)
        cross = A[ee, r.j] if r.s > 0 else B[ee, r.j]
        ex_px = B[r.x, r.j] if r.s > 0 else A[r.x, r.j]
        if not np.isfinite([p, cross, ex_px]).all(): continue
        pl = lambda px: (ex_px - px) * r.s
        out.append(dict(rule=r.rule, base=r.net, ft=tight.any(), fl=loose.any(), nt=pl(p) if tight.any() else pl(cross), nl=pl(p) if loose.any() else pl(cross), late=pl(cross)))
    O = pd.DataFrame(out)
    for rule, g in O.groupby("rule"):
        rows.append(dict(wait=wait * 5, rule=rule, n=len(g), base=c(g.base), fill_t=round(g.ft.mean(), 2), net_t=c(g.nt), fill_l=round(g.fl.mean(), 2), net_l=c(g.nl), cross_late=c(g.late)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n=== our MM fills, by state of the signal (both-venue move in the last 2 h on that leg); quantity-weighted markout from fill price, YES terms")
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
fl = pd.read_sql_query("select exchange_id,side,price,quantity,filled_at from fills where purpose='mm'", db)
db.close()
fl["ts"] = (pd.to_datetime(fl.filled_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
fl["j"] = fl.exchange_id.astype(str).map({str(x): i for i, x in enumerate(L.cols)})
fl = fl[fl.j.notna() & (fl.ts >= L.idx[0]) & (fl.ts < L.idx[-1] - 4 * 3600)].copy(); fl["j"] = fl.j.astype(int)
fl["b"] = ((fl.ts - L.idx[0]) // 300).astype(int)
fl["sgn"] = np.sign(fl.quantity); fl["q"] = fl.quantity.abs()
act = np.zeros_like(Mv)
for r in V.itertuples():
    act[r.t - 6:min(r.t + 24, n), r.j] = r.s
fl["rel"] = fl.sgn * act[fl.b.values, fl.j.values]
for lab, h in (("m1", 12), ("m4", 48)):
    fl[lab] = fl.sgn * (Mv[np.minimum(fl.b.values + h, n - 1), fl.j.values] - fl.price)
fl = fl.dropna(subset=["m1", "m4"])
gr = fl.assign(state=np.where(fl.rel > 0, "with", np.where(fl.rel < 0, "against", "none"))).groupby("state")
print(pd.DataFrame(dict(n=gr.size(), shares=gr.q.sum(), mk1h=gr.apply(lambda g: 100 * np.average(g.m1, weights=g.q)), mk4h=gr.apply(lambda g: 100 * np.average(g.m4, weights=g.q)), pnl4h=gr.apply(lambda g: (g.m4 * g.q).sum()))).round(2).to_string())

print("\n=== set clips as free lag exposure: strict trades in races with a set we must sell; sets qty, gross per set from selling the loser now and the winner at exit")
wr = pd.DataFrame(json.load(open("params/wind_down_report.json"))["rows"])
sets = wr[wr.kind.isin(["sell_set", "buy_set"])].set_index("holding").qty
hit = E[E.race.isin(sets.index)]
print("races with sets", len(sets), "strict trades there", len(hit), "of", len(E), "mean sets in hit race", round(hit.race.map(sets).mean()), "gross c on the traded leg", c(hit.gross))
print("value if all sets in hit races were legged once: $", round(float((hit.race.map(sets) * hit.gross).sum())))
