"""Truncation look-ahead test, poll-level fills, corrected pipeline, tape-priced P&L, book slippage, selection grid."""
import sys, glob, json; sys.path.insert(0, "research_notes/dir_alpha")
import numpy as np, pandas as pd
import audit_common as c
pd.set_option("display.width", 220, "display.max_columns", 40, "display.max_rows", 200)
G = c.Grid(); sf = G.flow(); tp = G.tape; M = G.masks(sf); d = G.trades(M["full"]); n = len(G.idx)
def r(nm, x):
    day = x.groupby("day").net.mean()
    print(f"{nm:58s} n {len(x):4d} races {x.race.nunique():3d} net {100*x.net.mean():6.2f}c med {100*x.net.median():5.2f} win {100*(x.net>0).mean():4.0f}% days+ {(day>0).sum()}/{len(day)} worst {100*x.net.min():.1f}")
print("--- truncation test: rerun the pipeline on data cut at T; signals before T must be identical")
vf = c.raw("vf")
for T in G.idx[[1200, 1600, 2000, 2300]]:
    orig = c.raw
    c.raw = lambda k, _o=orig, _T=T: (lambda z: z[z.ts < _T + 300] if k == "vf" else z)(_o(k))
    Gt = c.Grid()
    tpc = orig("tape"); tpc = tpc[pd.to_datetime(tpc.executed_at, format="ISO8601", utc=True) < pd.Timestamp(T + 300, unit="s", tz="UTC")]
    sft = Gt.flow(tape=tpc); Mt = Gt.masks(sft); c.raw = orig
    idx = Gt.idx; I = np.searchsorted(G.idx, idx)
    # last bin's own tape is partial in both because the cut is at bin end
    a = Mt["full"][:-1] & (np.abs(Gt.S48.values[:-1]) >= c.TH); b = M["full"][I][:-1] & (np.abs(G.S48.values[I][:-1]) >= c.TH)
    Jm = [G.cols.index(x) for x in Gt.cols]
    print(pd.Timestamp(T, unit="s", tz="UTC"), "signal cells", int(a.sum()), "cells differing", int((a != b[:, Jm]).sum()), "S48 max abs diff", np.nanmax(np.abs(Gt.S48.values[:-1] - G.S48.values[I][:-1][:, Jm])))

print("\n--- poll-level fills")
L = {k: v.sort_values("ts")[["ts", "sig_bid", "sig_ask"]].values for k, v in vf.groupby("exchange_id")}
def pollfill(x, skip=1, hold_s=86400):
    """Signal at the bin's last poll. Fill at the `skip`-th later poll. Exit at first poll at or after fill + hold."""
    out = []
    for k in x.itertuples():
        a = L[k.ex]; t0 = G.P["ts"].values[k.i, k.j]
        p = np.searchsorted(a[:, 0], t0, side="left")
        e = p + skip
        if e >= len(a): out.append(np.nan); continue
        y = np.searchsorted(a[:, 0], a[e, 0] + hold_s, side="left")
        if y >= len(a): out.append(np.nan); continue
        out.append((a[y, 1] - a[e, 2]) if k.s > 0 else (a[e, 1] - a[y, 2]))
    return out
for sk in (0, 1, 2, 4):
    r(f"all 97, fill at poll +{sk}, exit first real poll >= +24 h", d.assign(net=pollfill(d, sk)).dropna(subset=["net"]))

print("\n--- corrected pipeline")
G0 = c.Grid(ffill=0)
def strict(mask_extra=None, Gx=G0, dedupe=True, sk=1, full48=False, thr=c.TH):
    S = Gx.S48; sg = np.sign(S)
    lag = lambda x: x - x.ffill(limit=3).shift(c.HOLD)
    both = ((lag(Gx.P["pm"]) * sg >= .01) & (lag(Gx.P["kalshi"]) * sg >= .01)).values
    flow = (sf.rolling(c.HOLD, min_periods=1).sum() * sg >= -5000).values
    band = ((Gx.mid >= .15) & (Gx.mid <= .85)).values & ~Gx.chamber[None, :]
    m = both & flow & band
    if mask_extra is not None: m = m & mask_extra
    if full48:
        m = m & (Gx.gap.notna().rolling(c.WIN, min_periods=1).sum().values >= 500)
    e = Gx.trades(m, need_exit=False, th=thr).sort_values("x").reset_index(drop=True)
    if dedupe:
        keep, last = [], {}
        for k in e.itertuples():
            if k.race in last and k.x - last[k.race] < c.HOLD: continue
            keep.append(k.Index); last[k.race] = k.x
        e = e.loc[keep]
    # pollfill needs G.P ts: use Gx
    out = []
    for k in e.itertuples():
        a = L[k.ex]; t0 = Gx.P["ts"].values[k.i, k.j]
        p = np.searchsorted(a[:, 0], t0, side="left"); f_ = p + sk
        if f_ >= len(a): out.append(np.nan); continue
        y = np.searchsorted(a[:, 0], a[f_, 0] + 86400, side="left")
        if y >= len(a): out.append(np.nan); continue
        out.append((a[y, 1] - a[f_, 2]) if k.s > 0 else (a[f_, 1] - a[y, 2]))
    e = e.assign(net=out).dropna(subset=["net"])
    return e
a1 = strict(dedupe=False, sk=0); r("no ffill, same-poll fill, exit next real poll", a1)
a2 = strict(dedupe=False, sk=1); r("+ fill one poll later", a2)
a3 = strict(dedupe=True, sk=1); r("+ one per race per 24 h", a3)
a4 = strict(dedupe=True, sk=1, full48=True); r("+ full 48 h median window only", a4)
a5 = strict(dedupe=True, sk=2); r("(a3 with fill two polls later)", a5)
for nm, f in (("drop top 2 race episodes (IA-02, MT-01 type)", lambda z: z.drop(z.net.nlargest(2).index)), ("drop 10-29 entries", lambda z: z[z.day != "10-29"]), ("10-29 entries only", lambda z: z[z.day == "10-29"]), ("first half", lambda z: z[z.x < n // 2]), ("second half", lambda z: z[z.x >= n // 2]), ("house", lambda z: z[z.race.str.contains("House")]), ("non-house", lambda z: z[~z.race.str.contains("House")]), ("D-side bets", lambda z: z[(z.s * np.where(z.party == "D", 1, -1)) > 0]), ("R-side bets", lambda z: z[(z.s * np.where(z.party == "D", 1, -1)) < 0])):
    r("  a3 " + nm, f(a3))
print(a3.groupby("day").net.agg(["size", lambda x: 100 * x.mean()]).round(2).T.to_string())
print("trim 5% each side net c", round(100 * a3.net.sort_values().iloc[int(.05 * len(a3)):int(.95 * len(a3))].mean(), 2))
rng = np.random.default_rng(1); ds = a3.day.unique(); bs = [np.concatenate([a3.net.values[a3.day.values == q] for q in rng.choice(ds, len(ds))]).mean() for _ in range(4000)]
print("a3 day bootstrap se", round(100 * np.std(bs), 2), "95%", np.round(100 * np.quantile(bs, [.025, .975]), 2))
a3.to_pickle(c.CACHE + "/a3.pkl")

print("\n--- base rule (no both/flow), same corrections")
sG = G0.S48
b0 = ((G0.mid >= .15) & (G0.mid <= .85)).values & ~G0.chamber[None, :]
e = G0.trades(b0, need_exit=False).sort_values("x").reset_index(drop=True)
keep, last = [], {}
for k in e.itertuples():
    if k.race in last and k.x - last[k.race] < c.HOLD: continue
    keep.append(k.Index); last[k.race] = k.x
e = e.loc[keep]
r("base, no ffill, one poll later, one per race", e.assign(net=[(lambda a, f_, y: (a[y, 1] - a[f_, 2]) if k.s > 0 else (a[f_, 1] - a[y, 2]))(L[k.ex], (lambda a: np.searchsorted(a[:, 0], G0.P["ts"].values[k.i, k.j]) + 1)(L[k.ex]), (lambda a: min(len(a) - 1, np.searchsorted(a[:, 0], a[min(len(a) - 1, np.searchsorted(a[:, 0], G0.P["ts"].values[k.i, k.j]) + 1), 0] + 86400)))(L[k.ex])) for k in e.itertuples()]))

print("\n--- tape-priced P&L for the 97: same-side taker print nearest the entry and exit, within +/-30 min")
by = {k: v.sort_values("ts") for k, v in tp.groupby("exchange_id")}
rows = []
for k in d.itertuples():
    a = by.get(k.ex)
    if a is None: rows.append((np.nan, np.nan)); continue
    def nearest(t, side):
        m = a[(a.aggressor == side) & (a.ts >= t - 1800) & (a.ts <= t + 1800)]
        return np.nan if not len(m) else m.price.iloc[(m.ts - t).abs().argmin()]
    t0, t1 = G.idx[k.x] + 300, G.idx[k.y] + 300
    if k.s > 0: rows.append((nearest(t0, "buy"), nearest(t1, "sell")))
    else: rows.append((nearest(t0, "sell"), nearest(t1, "buy")))
T = pd.DataFrame(rows, columns=["e", "x"], index=d.index); T["tn"] = d.s * (T.x - T.e)
ok = T.tn.notna(); print("priced on both ends:", int(ok.sum()), "tape net c", round(100 * T.tn[ok].mean(), 2), "touch net same trades", round(100 * d.net[ok].mean(), 2), " rest touch net", round(100 * d.net[~ok].mean(), 2))

print("\n--- book walk from snapshots")
files = sorted(glob.glob("data/books_history/books_*.json")); tradedx = sorted(set(a3.ex))
out = []
for fn in files:
    B = json.load(open(fn))
    for ex in tradedx:
        b = B.get(str(ex))
        if not b or not b.get("bids") or not b.get("asks"): continue
        for side, lv in (("ask", b["asks"]), ("bid", b["bids"])):
            r0 = {"side": side, "touch": lv[0]["quantity"], "ex": ex}
            p0 = lv[0]["price"]
            for Q in (500, 2000, 5000, 20000, 60000):
                need, cost = Q, 0
                for l in lv:
                    t_ = min(need, l["quantity"]); cost += t_ * abs(l["price"] - p0); need -= t_
                    if need <= 0: break
                r0[f"s{Q}"] = 100 * cost / (Q - need) if need < Q else np.nan
                r0[f"lvl{Q}"] = need > 0
            out.append(r0)
Z = pd.DataFrame(out)
print("snapshot rows", len(Z), "touch qty quantiles", Z.touch.quantile([.1, .25, .5, .75, .9]).round(0).to_dict())
print("slippage beyond touch, one side, c, mean / median:", {q: (round(Z[f"s{q}"].mean(), 3), round(Z[f"s{q}"].median(), 3)) for q in (500, 2000, 5000, 20000, 60000)})
print("book snapshot depth (50 levels) ran out share:", {q: round(Z[f"lvl{q}"].mean(), 3) for q in (500, 2000, 5000, 20000, 60000)})

print("\n--- selection grid (d4's own grid, rebuilt)")
Sg = G.S48; s = np.sign(Sg)
fair, mid = G.fair, G.mid
vol = tp.groupby(["b", "exchange_id"]).q.sum().unstack().reindex(index=G.idx, columns=G.cols).fillna(0).rolling(c.HOLD, min_periods=1).sum()
dm = mid - mid.shift(288); df_ = fair - fair.shift(288)
both, flowok = M["both"], M["flowok"]; thin = (vol < 60000).values
notyet = ((dm * s) <= .3 * (df_ * s).clip(lower=.005)).values
base = M["base"]
R = {"base": base, "both": base & both, "flow": base & flowok, "both+flow": base & both & flowok, "both+thin": base & both & thin, "both+notyet": base & both & notyet, "both+flow+notyet": base & both & flowok & notyet, "both+flow+thin": base & both & flowok & thin, "flow+thin": base & flowok & thin}
ofc = np.array(["house" if "House race" in x else "senate" if x.endswith("Senate") else "governor" if x.endswith("Governor") else "other" for x in G.race])
rows = []
for th in (.015, .02, .03):
    for nm, m in R.items():
        for off in (None, "house", "governor", "senate"):
            for h in (144, 288, 576):
                mm = m.copy()
                if off: mm[:, ofc != off] = False
                e_ = G.trades(mm, th=th, hold=h)
                if len(e_) < 12: continue
                a_, b_ = e_[e_.x < n // 2], e_[e_.x >= n // 2]
                rows.append(dict(th=th, rule=nm, off=off or "all", h=h, n=len(e_), net=100 * e_.net.mean(), n1=len(a_), n2=len(b_), n1net=100 * a_.net.mean() if len(a_) else np.nan, n2net=100 * b_.net.mean() if len(b_) else np.nan))
A = pd.DataFrame(rows); A.to_pickle(c.CACHE + "/grid.pkl")
print("configs", len(A), "mean net", round(A.net.mean(), 2), "sd", round(A.net.std(), 2), "max", round(A.net.max(), 2), " rank of the audited rule by net:", int((A.net > A[(A.rule == "both+flow") & (A.th == .02) & (A.off == "all") & (A.h == 288)].net.iloc[0]).sum()) + 1)
ok = A[(A.n1 >= 10) & (A.n2 >= 10)]
print("usable", len(ok), "spearman 1st-half vs 2nd-half net:", round(ok.n1net.corr(ok.n2net, method="spearman"), 2))
top = ok.nlargest(5, "n1net"); print("best by first half -> second half:"); print(top.round(2).to_string(index=False))
top = ok.nlargest(5, "n2net"); print("best by second half -> first half:"); print(top.round(2).to_string(index=False))
print("mean 2nd-half net of top-10% by first half:", round(ok[ok.n1net >= ok.n1net.quantile(.9)].n2net.mean(), 2), "  of bottom-10%:", round(ok[ok.n1net <= ok.n1net.quantile(.1)].n2net.mean(), 2))
led = pd.read_json("data/research_diralpha/ledger.jsonl", lines=True)
print("ledger rows", len(led), "families", led.family.nunique(), "names", led.name.nunique())
surv = led[(led.train_net_c > 0) & (led.test_net_c > 0) & (led.get("test_t_race", 0) >= 2) & (led.get("test_t_day", 0) >= 2)]
print("passing train>0,test>0,t>=2 both:", len(surv), "by family", surv.family.value_counts().head(8).to_dict())
d2 = pd.read_pickle("data/research_diralpha/d2.pkl"); print("d2 rows", len(d2), "d2 best net", round(d2.net.max(), 2))
g3 = pd.read_pickle("data/research_diralpha/exit_grid.pkl"); print("d3 exit grid rows", len(g3)); d4 = pd.read_pickle("data/research_diralpha/d4.pkl"); print("d4 rows", len(d4))
b = base; rej = b & ~(both & flowok)
e_all = G.trades(b); 
print("filter uplift: base trades flagged by the filters at their own entry bin")
fl = (both & flowok)[e_all.i, e_all.j]; e_all["pass"] = fl
print(e_all.groupby("pass").net.agg(["size", lambda x: 100 * x.mean()]).round(2).to_string())
print(e_all.assign(half=e_all.x >= n // 2).groupby(["half", "pass"]).net.agg(["size", lambda x: 100 * x.mean()]).round(2).to_string())
