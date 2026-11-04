"""Round-2 quick checks that need only the panel, the tape and read-only DB tables. No API calls. Run: deploy/simq.sh <python> research_notes/dir_alpha/h2_internal.py
Uses the strict trades saved by d15 (strict_fast.pkl, strict_slow.pkl: j leg, t signal bin, e fill bin, x exit bin, s side, net, gross)."""
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
for c in ["j", "t", "e", "x"]:
    E[c] = E[c].astype(int)
E["race"] = L.race[E.j]; E["party"] = L.party[E.j]; E["office"] = L.office[E.j]; E["day"] = L.day[E.t]
n, m = mid.shape
c = lambda v: round(100 * float(np.nanmean(v)), 2)
se = lambda v: round(100 * float(np.nanstd(v) / np.sqrt(max(np.isfinite(v).sum(), 1))), 2)
print("strict", len(Ef), c(Ef.net), len(Es), c(Es.net))

# leg lookup for the mirror leg of a two-leg race
col_ix = {x: i for i, x in enumerate(L.cols)}
by_race = {}
for j, r in enumerate(L.race):
    by_race.setdefault(r, []).append(j)


def mirror(j):
    legs = by_race[L.race[j]]
    if len(legs) != 2: return None
    return legs[0] if legs[1] == j else legs[1]


print("\n=== B: mirror leg as a second tranche, and choosing the less-copied leg")
rows = []
Gap = (fair - mid).values
S48 = (fair - mid) - (fair - mid).rolling(576, min_periods=288).median()
Sv = S48.values
Mv = mid.values; Fv = fair.values
for k, r in E.iterrows():
    j2 = mirror(r.j)
    if j2 is None: continue
    s2 = -r.s
    b0, a0, b1, a1 = B[r.e, j2], A[r.e, j2], B[r.x, j2], A[r.x, j2]
    if not np.isfinite([b0, a0, b1, a1]).all(): continue
    n2 = (b1 - a0) if s2 > 0 else (b0 - a1)
    W = 6 if r.rule == "fast" else 288
    t0 = max(r.t - W, 0)
    left1 = r.s * ((Fv[r.t, r.j] - Fv[t0, r.j]) - (Mv[r.t, r.j] - Mv[t0, r.j]))
    left2 = s2 * ((Fv[r.t, j2] - Fv[t0, j2]) - (Mv[r.t, j2] - Mv[t0, j2]))
    rows.append(dict(rule=r.rule, net1=r.net, net2=n2, left1=left1, left2=left2, sig1=r.s * Sv[r.t, r.j], sig2=s2 * Sv[r.t, j2], spr1=A[r.e, r.j] - B[r.e, r.j], spr2=a0 - b0, race=r.race))
Mi = pd.DataFrame(rows).dropna(subset=["net1", "net2"])
for rule, g in Mi.groupby("rule"):
    pick = np.where(g.left2.fillna(-9) > g.left1.fillna(-9), g.net2, g.net1)
    pick_s = np.where(g.sig2.fillna(-9) > g.sig1.fillna(-9), g.net2, g.net1)
    print(rule, "n", len(g), "traded leg", c(g.net1), "mirror leg", c(g.net2), "half each", c((g.net1 + g.net2) / 2), "pick larger lag left", c(pick), "pick larger S48", c(pick_s), "corr nets", round(g.net1.corr(g.net2), 2), "mirror win", round((g.net2 > 0).mean(), 2))
print("share of legs: spread 1 tick at entry", round((E.apply(lambda r: A[r.e, r.j] - B[r.e, r.j] <= .0051, axis=1)).mean(), 2), "mean spread c", c(E.apply(lambda r: A[r.e, r.j] - B[r.e, r.j], axis=1)))

print("\n=== B: passive first, cross later. Bid at our side's touch from the fill bin; a fill needs a print through it (strict) or at it (loose); else cross at wait end")
tp = pd.read_pickle("data/research_diralpha/tape.pkl")[["exchange_id", "price", "quantity", "ts"]]
tp["exchange_id"] = tp.exchange_id.astype(str)
tg = {k: g.sort_values("ts") for k, g in tp.groupby("exchange_id")}
for wait_bins in (1, 3, 6, 12):
    out = []
    for k, r in E.iterrows():
        ex = str(L.cols[r.j]); t0 = L.idx[r.e]; t1 = t0 + wait_bins * 300
        p = B[r.e, r.j] if r.s > 0 else A[r.e, r.j]
        ee = min(r.e + wait_bins, n - 1)
        g = tg.get(ex)
        if g is None: continue
        w = g[(g.ts > t0) & (g.ts <= t1)]
        strict = (w.price < p - 1e-9) if r.s > 0 else (w.price > p + 1e-9)
        loose = (w.price <= p + 1e-9) if r.s > 0 else (w.price >= p - 1e-9)
        cross = A[ee, r.j] if r.s > 0 else B[ee, r.j]
        ex_px = B[r.x, r.j] if r.s > 0 else A[r.x, r.j]
        if not np.isfinite([p, cross, ex_px]).all(): continue
        pnl = lambda px: (ex_px - px) * r.s
        out.append(dict(rule=r.rule, base=r.net, f_strict=strict.any(), f_loose=loose.any(), n_strict=pnl(p) if strict.any() else pnl(cross), n_loose=pnl(p) if loose.any() else pnl(cross), n_late=pnl(cross)))
    O = pd.DataFrame(out)
    print(f"wait {wait_bins*5:3d} min:", O.groupby("rule").apply(lambda g: pd.Series(dict(base=c(g.base), filled_strict=round(g.f_strict.mean(), 2), net_strict=c(g.n_strict), filled_loose=round(g.f_loose.mean(), 2), net_loose=c(g.n_loose), cross_late=c(g.n_late)))).to_string().replace("\n", " | "))

print("\n=== C: copy speed by day. Both venues moved 1.5c+ in 30 min, one per leg per 4 h, band 15-85, no chamber. Copy = SIG move / outside move")
pmv, kv = L.pm.values, L.k.values
ev = []
nochm = L.office != "chamber"
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
        base = Mv[t - 6, j]
        row = dict(j=j, t=t, s=s, df=abs(df), c0=s * (Mv[t, j] - base), day=L.day[t], hour=L.hour[t], o240=s * (Fv[t + 48, j] - Fv[t, j]))
        for lab, h in (("c15", 3), ("c60", 12), ("c240", 48)):
            row[lab] = s * (Mv[t + h, j] - base)
        row["vol0"] = P["vol"].values[max(t - 6, 0):t + 3, j].sum()
        row["flow0"] = s * P["sflow"].values[t - 5:t + 4, j].sum()
        ev.append(row)
Ev = pd.DataFrame(ev)
Ev["wk"] = Ev.day.isin(["10-24", "10-25"])
tb = Ev.groupby("day").agg(n=("df", "size"), move=("df", lambda v: 100 * v.mean()), c0=("c0", lambda v: 100 * v.mean()), c15=("c15", lambda v: 100 * v.mean()), c60=("c60", lambda v: 100 * v.mean()), c240=("c240", lambda v: 100 * v.mean()), o240=("o240", lambda v: 100 * v.mean()), flow=("flow0", "median"))
tb["frac0"] = tb.c0 / tb.move; tb["frac60"] = tb.c60 / tb.move; tb["frac240"] = tb.c240 / tb.move
print(tb.round(2).to_string())
x = np.arange(len(Ev)); d = pd.factorize(Ev.day, sort=True)[0]
fr60 = (Ev.c60 / Ev.df).clip(-1, 2); fr15 = (Ev.c15 / Ev.df).clip(-1, 2)
print("trend per day in copied share: 15 min", round(np.polyfit(d, fr15.fillna(0), 1)[0], 3), "60 min", round(np.polyfit(d, fr60.fillna(0), 1)[0], 3), "n", len(Ev), "SE by day block 60m", round(pd.Series(fr60.values).groupby(d).mean().std() / np.sqrt(d.max() + 1), 3))
print("weekend vs week: ", Ev.groupby("wk")[["df", "c15", "c60", "c240"]].mean().mul(100).round(2).to_string().replace("\n", " | "), "n", Ev.wk.sum())
print("no-poll-gap check, events per day vs bars fresh:", P["fresh"].mean(axis=1).groupby(L.day).mean().round(2).to_dict())

print("\n=== C: venue state by day (touch spread, volume, legs 15-85 no chamber)")
ok = pd.DataFrame(np.broadcast_to(nochm, mid.shape), index=mid.index, columns=mid.columns) & (mid >= .15) & (mid <= .85)
spr = L.spr.where(ok)
vol = P["notional"].where(pd.DataFrame(np.broadcast_to(nochm, mid.shape), index=mid.index, columns=mid.columns))
d = pd.DataFrame(dict(spread_c=100 * spr.mean(axis=1), onetick=(spr <= .0051).sum(axis=1) / ok.sum(axis=1), touch=P["bid_qty"].where(ok).median(axis=1), notional=vol.sum(axis=1), absdfair_c=100 * (fair.diff(12).abs().where(ok)).mean(axis=1))).groupby(L.day)
print(d.agg(dict(spread_c="mean", onetick="mean", touch="median", notional="sum", absdfair_c="mean")).round(3).to_string())

print("\n=== C: what closes the gap? Over each strict hold, signed SIG mid change split by bins with prints and bins without")
rows = []
for k, r in E.iterrows():
    dm = np.diff(Mv[r.e:r.x + 1, r.j]) * r.s
    v = P["vol"].values[r.e + 1:r.x + 1, r.j]; sf = P["sflow"].values[r.e + 1:r.x + 1, r.j] * r.s
    if not len(dm): continue
    dm = np.nan_to_num(dm)
    rows.append(dict(rule=r.rule, tot=dm.sum(), noprint=dm[v == 0].sum(), withprint=dm[v > 0].sum(), flowwith=dm[(v > 0) & (sf > 0)].sum(), flowagainst=dm[(v > 0) & (sf <= 0)].sum(), bins_noprint=(v == 0).mean()))
print(pd.DataFrame(rows).groupby("rule").mean().mul(100).round(2).to_string())

print("\n=== C: count of strict signals by day and national drift")
dsign = L.dsign.values
good = (mid >= .1) & (mid <= .9)
natl = (fair.where(good).mul(L.dsign, axis=1).where(L.dsign != 0).mean(axis=1))
natm = (mid.where(good).mul(L.dsign, axis=1).where(L.dsign != 0).mean(axis=1))
print(pd.crosstab(E.day, E.rule).T.to_string())
print("national D-signed fair by day end, c:", (100 * natl.groupby(L.day).last()).round(2).to_dict())
E["dside"] = E.s * np.where(E.party == "D", 1, np.where(E.party == "R", -1, 0))
E["drift24"] = natl.diff(288).values[E.t]
E["with_drift"] = np.sign(E.drift24) == E.dside
E["flat"] = E.drift24.abs() < .002
print("net by trade against prior 24 h national drift")
print(E.groupby(["rule", "flat", "with_drift"]).net.agg(["size", lambda v: round(100 * v.mean(), 2), lambda v: round((v > 0).mean(), 2)]).to_string())
E["dnat"] = natl.values[E.x] - natl.values[E.e]
E["dnat_s"] = E.dnat * E.dside
print("net minus the national move in the trade's favour over the hold:", E.groupby("rule").apply(lambda g: (c(g.net), c(g.net - g.dnat_s), c(g.dnat_s))).to_dict())
E["fwd_dir"] = np.where(E.dnat_s > .002, "nat helped", np.where(E.dnat_s < -.002, "nat hurt", "nat flat"))
print(E.groupby(["rule", "fwd_dir"]).net.agg(["size", lambda v: round(100 * v.mean(), 2)]).to_string())

print("\n=== C: polls. Strict entries with a poll of that race created in the prior 24 h vs none")
import re
rm = json.load(open("data/race_map.json"))["races"]
polls = []
for pt, suffix in (("governor", " Governor"), ("us-senator", " Senate"), ("us-representative", " House race")):
    dd = json.load(open(f"data/research_diralpha/polls/{pt}.json")); dd = dd.get("data", dd) if isinstance(dd, dict) else dd
    for p_ in dd:
        polls.append(dict(race=(p_.get("subject") or "").replace("2026 ", "") + suffix, ts=pd.Timestamp(p_["created_at"]).timestamp()))
Pl = pd.DataFrame(polls)
print("poll rows", len(Pl), "matched names", Pl.race.isin(set(L.race)).mean().round(2))
pt = {r: np.sort(g.ts.values) for r, g in Pl.groupby("race")}
def recent(r, ts, h=24):
    a = pt.get(r)
    return a is not None and ((a <= ts) & (a > ts - h * 3600)).any()
E["poll24"] = [recent(r, L.idx[t]) for r, t in zip(E.race, E.t)]
Ev["poll24"] = [recent(L.race[j], L.idx[t]) for j, t in zip(Ev.j, Ev.t)]
print(E.groupby(["rule", "poll24"]).net.agg(["size", lambda v: round(100 * v.mean(), 2)]).to_string())
print("all both-venue moves: retained outside 4 h after and SIG copy at 4 h by poll24"); print(Ev.groupby("poll24")[["df", "o240", "c240"]].mean().mul(100).round(2).assign(n=Ev.groupby("poll24").size()).to_string())
Ev["late"] = Ev.day >= "10-28"
print(Ev.groupby("late")[["df", "o240", "c240"]].mean().mul(100).round(2).assign(n=Ev.groupby("late").size()).to_string())

print("\n=== A: overround by day (sum of mids - 1) in two-leg races, and mean gap by band")
two = [g for g in by_race.values() if len(g) == 2]
ov = pd.DataFrame({i: Mv[:, a] + Mv[:, b] - 1 for i, (a, b) in enumerate(two) if L.office[a] != "chamber"}, index=mid.index)
gap = fair - mid
tb = pd.DataFrame(dict(overround_c=100 * ov.mean(axis=1), gap_lo=100 * gap.where((fair > .15) & (fair < .35)).mean(axis=1), gap_mid=100 * gap.where((fair > .35) & (fair < .65)).mean(axis=1), gap_hi=100 * gap.where(fair > .85).mean(axis=1))).groupby(L.day).mean()
print(tb.round(2).to_string())

print("\n=== B: wind-down overlay. A sale is happening anyway, so no spread: mid move after a signal cell, legs 15-85, no chamber")
okv = ok.values
sg = {}
for thr in (.01, .015, .02):
    both = ((L.lag(L.pm, 288) * np.sign(S48) >= .01) & (L.lag(L.k, 288) * np.sign(S48) >= .01)).values
    M = (np.abs(np.nan_to_num(Sv)) >= thr) & okv & both
    for hb, lab in ((48, "4h"), (96, "8h"), (144, "12h"), (288, "24h")):
        fw = (mid.shift(-hb) - mid).values * np.sign(Sv)
        v = fw[M]
        sg[(thr, lab)] = (c(v), int(np.isfinite(v).sum()))
    print("threshold", thr, "share of leg-time active", round(M.sum() / okv.sum(), 3), {k[1]: v for k, v in sg.items() if k[0] == thr})
wd = json.load(open("params/wind_down_report.json"))
wr = pd.DataFrame(wd["rows"])
print("wind-down report totals", wd["totals"])
print(wr.groupby("kind").agg(n=("qty", "size"), qty=("qty", "sum"), sale=("sale_value", "sum")).round(0).to_string())

print("\n=== C: wind-down collision. Current paper signals on legs the wind-down is selling")
st = json.load(open("data/research_diralpha/paper_status.json"))
live = pd.DataFrame(st["live"])
wr2 = wr[wr.kind.isin(["leg", "mm_leg"])].copy()
wr2["race"] = wr2.holding.str.replace(r" leg \d+$", "", regex=True)
wr2["ex"] = wr2.holding.str.extract(r"leg (\d+)$")
sellraces = set(wr[wr.kind.isin(["leg", "mm_leg", "sell_set"])].holding.str.replace(r" leg \d+$", "", regex=True))
print("live signals", len(live), "races in wind-down sell list", live.race.isin(sellraces).sum(), "exempt races", live.race.isin(set(wr[wr.kind == "exempt"].holding)).sum())
print("strict trades (all history) in those sell-list races:", E.race.isin(sellraces).mean().round(2), " net in/out:", E.groupby(E.race.isin(sellraces)).net.mean().mul(100).round(2).to_dict())

print("\n=== B: our MM fills against the fast or slow signal: markout of fills taken in the 4 h after a strict signal in the same race side")
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
fl = pd.read_sql_query("select exchange_id,side,price,quantity,filled_at,purpose,ticks_from_mid from fills where purpose='mm'", db)
fl["ts"] = (pd.to_datetime(fl.filled_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
print(fl.head(2).to_string()); print("side counts", fl.side.value_counts().to_dict())
fl["j"] = fl.exchange_id.astype(str).map({str(x): i for i, x in enumerate(L.cols)})
fl = fl[fl.j.notna() & (fl.ts >= L.idx[0]) & (fl.ts < L.idx[-1] - 4 * 3600)].copy(); fl["j"] = fl.j.astype(int)
fl["b"] = ((fl.ts - L.idx[0]) // 300).astype(int)
# YES-denominated: side yes = we bought YES, side no = we sold YES (store converts price to YES terms)
fl["sgn"] = np.where(fl.side == "yes", 1.0, -1.0)
# active signal state per leg and bin: both-venue move event in last 2 h, direction s; use Ev (all moves) and the slow mask
act = np.zeros_like(Mv)
for r in Ev.itertuples():
    act[r.t:min(r.t + 24, n), r.j] = r.s
fl["act"] = act[fl.b.values, fl.j.values]
fl["rel"] = fl.sgn * fl["act"]
fl["m0"] = Mv[fl.b.values, fl.j.values]
for lab, h in (("mk1h", 12), ("mk4h", 48)):
    fl[lab] = fl.sgn * (Mv[np.minimum(fl.b.values + h, n - 1), fl.j.values] - fl.price)
gr = fl.assign(state=np.where(fl.rel > 0, "with signal", np.where(fl.rel < 0, "against signal", "no signal"))).groupby("state")
print(pd.DataFrame(dict(n=gr.size(), shares=gr.quantity.sum(), mk1h_c=gr.apply(lambda g: 100 * np.average(g.mk1h.fillna(0), weights=g.quantity)), mk4h_c=gr.apply(lambda g: 100 * np.average(g.mk4h.fillna(0), weights=g.quantity)))).round(3).to_string())

print("\n=== Rate left: strict entries per day over last 3 full days, and fresh entries from the forward paper file")
print(E[E.day >= "10-28"].groupby("rule").size().div(3).round(1).to_dict())
try:
    pj = pd.read_json("data/research_diralpha/paper.jsonl", lines=True); print("paper rows", len(pj), "new (not pre-existing)", int((~pj.pre_existing).sum()))
except Exception as ex:
    print(ex)
E.to_pickle("data/research_diralpha/h2_strict.pkl") if False else None
