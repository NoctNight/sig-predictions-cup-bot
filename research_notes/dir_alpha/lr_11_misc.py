from lr_common import *
L, D, tp = load(); Z = pd.read_pickle("research_notes/dir_alpha/lr_sets.pkl"); dA, dB = D["A"], D["B"]; qa = book_ctx(L, dB); C, Rc, okc = clean(L, dB, qa); b0, a0 = dB["book"]; mB = (b0 + a0) / 2; Ra, oka = residual(dA); mA = dA["S"].values
print("== truncation: Res rebuilt on data cut at T equals full-sample Res up to T")
for nm, d, R0 in (("A", dA, Ra), ("B last", dB, residual(dB)[0])):
    for fr in (.4, .7):
        T = int(R0.shape[0] * fr); dd = dict(d); dd["S"] = d["S"].iloc[:T]; dd["P"] = d["P"].iloc[:T]; dd["K"] = d["K"].iloc[:T]
        r, _ = residual(dd); a = r; b = R0[:T]; m = np.isfinite(a) & np.isfinite(b); print(nm, fr, "cells", m.sum(), "max abs diff", np.abs(a[m] - b[m]).max(), "nan mismatch", (np.isfinite(a) != np.isfinite(b)).sum())
print("== dropped exits (no print in the 6 h exit window)")
for nm, d, R_, kw in (("A", dA, Ra, {}), ("B", dB, residual(dB)[0], {})):
    n = R_.shape[0]; R, ok = R_, residual(d)[1]; Rv = d["R"].values; S = d["S"].values; nx = next_real(Rv); ra = d["R"].rolling(12, min_periods=1).max().fillna(0).values > 0
    # count: signal, enterable, exit window inside the sample, but no print
    drop = []; 
    for j in range(R.shape[1]):
        last = -10**9
        for t in np.flatnonzero((np.abs(R[:, j]) >= .03) & (S[:, j] >= .1) & (S[:, j] <= .9) & ok[:, j]):
            if t - last < 864 or not ra[t, j]: continue
            e = nx[t, j]
            if e < 0 or e - t > 24: continue
            last = t; s = -1. if R[t, j] > 0 else 1.
            if e + 936 >= n: continue
            if not Rv[e + 864:e + 936, j].any():
                u = np.flatnonzero(Rv[e + 936:, j]); xp = e + 936 + u[0] if len(u) else -1; drop.append(s * (S[min(e + 864, n - 1), j] - S[e, j]) - .0075 if xp < 0 else s * (S[xp, j] - S[e, j]) - .0075)
    print(nm, "dropped inside sample", len(drop), "mean net at next print or stale %.2fc" % (100 * np.mean(drop) if drop else np.nan))
print("== fill delay, clean B")
for dl in (1, 3, 6, 12, 24):
    c, _, _ = clean(L, dB, qa, delay=dl, Rmid=(Rc, okc)); print(dl * 5, "min", st(c))
print("== old distance vs new, snapshots every 6 h, all legs 3c+")
def snapq(R_, ok_, Mx, d, fn, band=(.1, .9)):
    rows = []; n = R_.shape[0]
    for T in range(288, n - 870, 72):
        sel = np.flatnonzero((np.abs(R_[T]) >= .03) & (Mx[T] >= band[0]) & (Mx[T] <= band[1]) & ok_[T])
        if len(sel) == 0: continue
        E = fn(T)
        if len(E) == 0: continue
        for r in E.itertuples():
            sgn = 1 if r.s < 0 else -1; o24 = sgn * R_[T - 288, r.j]; o48 = sgn * R_[max(T - 576, 0), r.j]
            rows.append(dict(T=T, j=r.j, n=r.n, race=r.race, day=r.day, o24=o24, o48=o48, res=r.res, s=r.s, party=r.party))
    return pd.DataFrame(rows)
def snapA(T):
    Mc = np.zeros(Ra.shape, bool); Mc[T] = (np.abs(Ra[T]) >= .03) & (mA[T] >= .1) & (mA[T] <= .9) & oka[T]; return engine(dA, Ra, oka, Mcustom=Mc)
def snapB(T):
    ex = np.zeros(Rc.shape, bool); ex[T] = True; return clean(L, dB, qa, extra=ex, Rmid=(Rc, okc))[0]
for lab, q in (("A", snapq(Ra, oka, mA, dA, snapA)), ("B", snapq(Rc, okc, mB, dB, snapB))):
    q = q.dropna(subset=["o24"]); print(lab, "snapshot rows", len(q), "unique leg-weeks", q.groupby(["j", q["T"] // 864]).ngroups)
    print(q.groupby(pd.cut(q.o24, [-1, .02, .03, 1]), observed=True).n.agg(rows="size", net=lambda v: round(100 * v.mean(), 2), win=lambda v: round((v > 0).mean(), 2)).T.to_string())
    for g, m in (("new (under 2c a day ago)", q.o24 < .02), ("old (3c+ a day ago)", q.o24 >= .03)):
        x = q[m]; x2 = x.groupby(["race", x["T"] // 864]).n.mean().reset_index(); dmean = x.groupby(x["T"] // 288).n.mean(); print("  ", g, "race-blocks", len(x2), "net %.2fc, day-mean t %.1f over %d days" % (100 * x2.n.mean(), dmean.mean() / (dmean.std() / np.sqrt(len(dmean))), len(dmean)))
print("== own flow in B")
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True); fl = pd.read_sql_query("select exchange_id ex, price, quantity q, filled_at, purpose from fills", db); fl["ts"] = (pd.to_datetime(fl.filled_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds(); fl["b"] = (fl.ts // 300).astype("int64") * 300
cols = list(dB["S"].columns); ci = {str(c): i for i, c in enumerate(cols)}; fl["j"] = fl.ex.map(ci); fl = fl.dropna(subset=["j"]); fl["i"] = np.searchsorted(L.idx, fl.b.values)
tp["i"] = np.searchsorted(L.idx, tp.b.values); tp["j"] = tp.ex.map(ci)
for lab, E in (("B clean", Z["C"]), ("B oos8", Z["B1"]), ("B stated", Z["B2"])):
    own, vol, pre = [], [], []
    for r in E.itertuples():
        f = fl[(fl.j == r.j) & (fl.i >= r.e) & (fl.i <= r.x)]; own.append(r.s * f.q.sum()); pf = fl[(fl.j == r.j) & (fl.i >= r.t - 72) & (fl.i < r.e)]; pre.append(r.s * pf.q.sum())
        v = tp[(tp.j == r.j) & (tp.i >= r.e) & (tp.i <= r.x)]; vol.append(v.quantity.abs().sum())
    own, vol, pre = np.array(own), np.array(vol), np.array(pre); shr = own / np.maximum(vol, 1)
    print(lab, "own net flow in trade direction during hold: mean %.0f shares, median %.0f, tape vol median %.0f, share mean %.3f; p95 %.3f; net where own helped >2%%: %s | else %s; own in prior 6 h mean %.0f" % (own.mean(), np.median(own), np.median(vol), shr.mean(), np.quantile(shr, .95), st(E[shr > .02]), st(E[shr <= .02]), pre.mean()))
    # own print at the entry bin
    k = fl.groupby(["i", "j"]).q.sum(); print("   entries with an own fill in the bin %.3f, exits %.3f" % (np.mean([(a, b) in k.index for a, b in zip(E.e, E.j)]), np.mean([(a, b) in k.index for a, b in zip(E.x, E.j)])))
or_ = pd.read_sql_query("select exchange_id ex, side, action, price, quantity q, placed_at ts, purpose from orders where purpose in ('mm','tilt','inside')", db); or_["j"] = or_.ex.map(ci); or_ = or_.dropna(subset=["j"])
# MM rests: side yes buy = bid at price; side no buy = ask at 1-price or YES-denominated price
print("order price sample", or_.groupby("side").price.describe()[["mean"]].T.to_string())
E = Z["C"]; hit = []; 
bid, ask = L.bid.values, L.ask.values
for r in E.itertuples():
    t1 = L.idx[r.e] + 300; o = or_[(or_.j == r.j) & (or_.ts <= t1) & (or_.ts >= t1 - 600)]
    if r.s > 0: h = o[(o.side == "no") & (np.abs(o.price - ask[r.e, r.j]) < .001)]
    else: h = o[(o.side == "yes") & (np.abs(o.price - bid[r.e, r.j]) < .001)]
    hit.append(h.q.abs().max() if len(h) else 0)
hit = np.array(hit, float); print("B clean: own resting order placed in the 10 min before entry at the touch we would hit: share %.2f, median size where present %.0f, net with %.2fc, without %.2fc" % ((hit > 0).mean(), np.median(hit[hit > 0]) if (hit > 0).any() else 0, 100 * E.n.values[hit > 0].mean() if (hit > 0).any() else np.nan, 100 * E.n.values[hit == 0].mean()))
print("== cap sim, 5,000 shares a bet, block adds at |net|>=5,000 as risk.blocked_directions, marks at entry price")
def cap(E, Mx, start, q=5000, hold=864):
    E = E.sort_values("e"); opn = []; net = start; tk = []; peak = 0; cash_peak = 0
    for r in E.itertuples():
        for o in list(opn):
            if o[0] <= r.e: net -= o[1]; opn.remove(o)
        p = Mx[r.e, r.j]; ds = (1 if r.party == "D" else -1 if r.party == "R" else 0) * r.s; 
        n = (p if r.s > 0 else 1 - p) * q * ds
        blocked = (ds > 0 and net >= 5000) or (ds < 0 and net <= -5000)
        tk.append(not blocked)
        if not blocked:
            net += n; opn.append((r.e + hold, n)); peak = max(peak, abs(net)); cash_peak = max(cash_peak, sum((Mx[o[0] - hold, 0] * 0 + abs(o[1])) for o in opn))
    tk = np.array(tk); return tk.mean(), 100 * E.n.values[tk].mean(), 100 * E.n.values[~tk].mean() if (~tk).any() else np.nan, peak, cash_peak
for lab, E, Mx in (("A stated", Z["A2"], mA), ("B clean", Z["C"], mB), ("B oos8", Z["B1"], dB["S"].values), ("A oos8", Z["A1"], mA)):
    for st_ in (0, 4700):
        t, a, b, pk, cp = cap(E, Mx, st_); print(lab, "start", st_, f"taken {t:.2f}, net taken {a:.2f}c, blocked {b:.2f}c, peak |net| {pk:.0f}, peak notional at risk {cp:.0f}")
Zn = pd.read_pickle(O + "line_now.pkl"); net = 4700; tk = 0; pos = 0
Zn2 = Zn[~Zn.race.isin(["U.S. House", "U.S. Senate"])]
for r in Zn2.itertuples():
    ds = 1 if r.side == "D" else -1; n = ds * r.price * 5000
    if (ds > 0 and net >= 5000) or (ds < 0 and net <= -5000): continue
    net += n; tk += 1
print("live list, chamber out", len(Zn2), "sides, D", (Zn2.side == "D").sum(), "R", (Zn2.side == "R").sum(), "taken at 5k in distance order from +4,700:", tk, "end net", round(net), "cash needed all", round((Zn2.price * 5000).sum()))
print("== pooled claim B: within-sample-day difference, all four sets")
