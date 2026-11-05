"""Independent review of the line trade (lr_*). Read-only. Rebuilds both panels and a general version of the oos8 engine."""
import pickle, sys, json, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
pd.set_option("display.width", 220, "display.max_rows", 200, "display.max_columns", 40)
O = "data/research_diralpha/oos/"
def load():
    L = Lab(); A = pickle.load(open(O + "panel.pkl", "rb"))
    idx = pd.to_datetime(L.idx, unit="s", utc=True)
    tp = pd.read_pickle("data/research_diralpha/tape.pkl"); tp = tp[tp.ts >= L.idx[0]].sort_values("ts"); tp["b"] = (tp.ts // 300).astype("int64") * 300; tp["ex"] = tp.exchange_id.astype(str)
    last = tp.groupby(["b", "ex"]).price.last().unstack().reindex(index=L.idx, columns=L.cols)
    D = {}
    D["A"] = dict(S=A["S"], R=A["R"], P=A["P"], K=A["K"], race=np.asarray(A["race"], object), office=np.asarray(A["office"], object), party=np.asarray(A["party"], object), book=None)
    D["B"] = dict(S=last.ffill(limit=288).set_axis(idx), R=last.notna().set_axis(idx), P=L.pm.set_axis(idx), K=L.k.set_axis(idx), race=np.asarray(L.race, object), office=np.asarray(L.office, object), party=np.asarray(L.party, object), book=(L.bid.ffill(limit=12).values, L.ask.ffill(limit=12).values))
    return L, D, tp
def fit_k(Mv, Fv, ok, mode="ols", office=None):
    """k series, past data only, same smoothing as oos8. Modes: ols (oos8), median (median of ratio), wfit (weight by |F-.5|**.. equals ols w/ extra weight)."""
    n = Mv.shape[0]; k = np.full(n, np.nan)
    for t in range(0, n, 12):
        o = ok[t]
        if o.sum() > 60:
            x = Fv[t, o] - .5; y = Mv[t, o] - .5
            if mode == "ols": k[t] = np.sum(y * x) / np.sum(x * x)
            elif mode == "median":
                u = np.abs(x) >= .1; k[t] = np.median(y[u] / x[u])
            elif mode == "wfit":
                w = np.minimum(1 / (.25 * 0 + np.maximum(np.abs(x), .05)), 20) * 0 + 1 / (.01 + np.abs(y - .93 * x)); k[t] = np.sum(w * y * x) / np.sum(w * x * x)
    return pd.Series(k).ffill().rolling(72, min_periods=1).mean().shift(1).values
def residual(D, Mv=None, mode="ols", by_office=False, Fv=None):
    S = D["S"]; P_, K_ = D["P"], D["K"]
    Mv = S.values if Mv is None else Mv
    Fv = ((P_ + K_) / 2).values if Fv is None else Fv
    office = D["office"]
    ok = np.isfinite(Mv) & np.isfinite(Fv) & (office != "chamber")[None, :] & (Fv > .03) & (Fv < .97) & (np.abs(P_.values - K_.values) <= .04)
    if by_office:
        Res = np.full(Mv.shape, np.nan)
        for off in ("house", "senate", "governor"):
            c = office == off
            o2 = ok.copy(); o2[:, ~c] = False
            # office fits need a lower floor of legs
            n = Mv.shape[0]; k = np.full(n, np.nan)
            for t in range(0, n, 12):
                o = o2[t]
                if o.sum() > 8: k[t] = np.sum((Mv[t, o] - .5) * (Fv[t, o] - .5)) / np.sum((Fv[t, o] - .5) ** 2)
            k = pd.Series(k).ffill().rolling(72, min_periods=1).mean().shift(1).values
            Res[:, c] = (Mv - (.5 + k[:, None] * (Fv - .5)))[:, c]
        return Res, ok
    k = fit_k(Mv, Fv, ok, mode)
    return Mv - (.5 + k[:, None] * (Fv - .5)), ok
def next_real(Rv):
    n, m = Rv.shape; nx = np.full((n, m), -1, int)
    for j in range(m):
        z = -1
        for i in range(n - 1, -1, -1):
            nx[i, j] = z
            if Rv[i, j]: z = i
    return nx
def engine(D, Res, ok, trig=.02, hold=864, cool=864, lo=.1, hi=.9, cost=.0075, extra=None, nx=None, ra=None, per_race=False, Mcustom=None):
    """oos8 engine. trig is the mask threshold; oos8 uses .02 and then cuts res>=.03 afterwards. Returns all trades with fields."""
    S, R = D["S"], D["R"]; Mv = S.values; Rv = R.values; n, m = Mv.shape; race = D["race"]; book = D["book"]
    ra = R.rolling(12, min_periods=1).max().fillna(0).values > 0 if ra is None else ra
    nx = next_real(Rv) if nx is None else nx
    M = (np.abs(Res) >= trig) & (Mv >= lo) & (Mv <= hi) & ok
    if extra is not None: M &= extra
    if Mcustom is not None: M = Mcustom
    days = np.asarray(S.index.strftime("%m-%d"))
    out = []
    lastrace = {}
    for j in range(m):
        last = -10**9
        for t in np.flatnonzero(M[:, j]):
            if t - last < cool or not ra[t, j]: continue
            e = nx[t, j]
            if e < 0 or e - t > 24: continue
            s = -1.0 if Res[t, j] > 0 else 1.0; x = -1
            for i in range(e + hold, min(e + hold + 72, n)):
                if Rv[i, j]: x = i; break
            r = dict(j=j, t=t, e=e, x=x, res=abs(Res[t, j]), rich=Res[t, j] > 0, s=s, own=Mv[e, j] if s > 0 else 1 - Mv[e, j], race=race[j], party=D["party"][j], day=days[t], fav=(s * (Mv[e, j] - .5)) > 0)
            if x < 0: r["n"] = np.nan; r["lt"] = np.nan
            else:
                r["lt"] = s * (Mv[x, j] - Mv[e, j]) - cost
                if book is None: r["n"] = r["lt"]
                else:
                    b, a = book; r["n"] = (b[x, j] - a[e, j]) if s > 0 else (b[e, j] - a[x, j])
            out.append(r); last = t
    E = pd.DataFrame(out)
    return E.dropna(subset=["n"]) if len(E) else E
def st(q, c="n"):
    if len(q) < 4: return dict(n=len(q), net=round(100 * q[c].mean(), 2) if len(q) else None)
    r = q.groupby("race")[c].mean(); d = q.groupby("day")[c].mean()
    return dict(n=len(q), races=q.race.nunique(), days=q.day.nunique(), net=round(100 * q[c].mean(), 2), win=round((q[c] > 0).mean(), 2), t_race=round(r.mean() / (r.std() / np.sqrt(len(r))), 1), t_day=round(d.mean() / (d.std() / np.sqrt(len(d))), 1))
def candles(D):
    C = json.load(open("data/candles_5m.json")); S = D["S"]; v, q, nt = {}, {}, {}
    for c in S.columns:
        d = pd.DataFrame(C[str(c)]["candles"]); ix = pd.to_datetime(d.time, utc=True)
        for tgt, col in ((v, "vwap"), (q, "volume"), (nt, "tradeCount")):
            x = pd.Series(d[col].values, index=ix); x = x[~x.index.duplicated(keep="last")].reindex(S.index); tgt[c] = x
    return pd.DataFrame(v), pd.DataFrame(q).fillna(0), pd.DataFrame(nt).fillna(0)
def age_since_change(F):
    v = F.values; ch = np.vstack([np.zeros((1, v.shape[1]), bool), (v[1:] != v[:-1]) & np.isfinite(v[1:]) & np.isfinite(v[:-1])])
    a = np.zeros(v.shape, int)
    for j in range(v.shape[1]):
        c = 10**6
        for i in range(v.shape[0]):
            c = 0 if ch[i, j] else c + 1; a[i, j] = c
    return a
def book_ctx(L, d):
    fresh = L.P["fresh"].values; n, m = fresh.shape; qa = np.zeros((n, m), int)
    for j in range(m):
        c = 10**6
        for i in range(n):
            c = 0 if fresh[i, j] else c + 1; qa[i, j] = c
    return qa
def clean(L, d, qa, trig=.03, hold=864, lo=.1, hi=.9, cool=864, delay=1, mode="ols", by_office=False, cost_extra=0.0, extra=None, Rmid=None, lim=True):
    """Live-style B rule: signal on the book mid, needs a fresh poll, entry next fresh poll, exit first fresh poll at +hold, touch on both ends. No print needed."""
    b, a = d["book"]; rb, ra_ = L.bid.values, L.ask.values; m0 = (b + a) / 2
    if Rmid is None: R2, ok2 = residual(d, Mv=m0, mode=mode, by_office=by_office)
    else: R2, ok2 = Rmid
    M = (np.abs(R2) >= trig) & (m0 >= lo) & (m0 <= hi) & ok2 & (qa == 0)
    if extra is not None: M &= extra
    days = np.asarray(d["S"].index.strftime("%m-%d")); out = []; n, mm = m0.shape
    for j in range(mm):
        last = -10**9
        for t in np.flatnonzero(M[:, j]):
            if t - last < cool: continue
            e = t + delay
            while e < n and e < t + 12 and qa[e, j] != 0: e += 1
            if e >= n or qa[e, j] != 0: continue
            x = e + hold
            while x < n and x < e + hold + 72 and qa[x, j] != 0: x += 1
            if x >= n or qa[x, j] != 0: continue
            s = -1.0 if R2[t, j] > 0 else 1.0
            nt = (rb[x, j] - ra_[e, j]) if s > 0 else (rb[e, j] - ra_[x, j])
            if not np.isfinite(nt): continue
            out.append(dict(j=j, t=t, e=e, x=x, s=s, res=abs(R2[t, j]), rich=R2[t, j] > 0, race=d["race"][j], party=d["party"][j], day=days[t], n=nt, g=s * (m0[x, j] - m0[e, j]), own=ra_[e, j] if s > 0 else 1 - rb[e, j])); last = t
    return pd.DataFrame(out), R2, ok2
def dside(E):
    return np.where(E.party.values == "D", E.s.values, np.where(E.party.values == "R", -E.s.values, 0.0))
def race_bets(E, cl="n", span=864):
    """One bet per race and direction: legs of one race on the same partisan side within 72 h count once (mean)."""
    E = E.copy(); E["ds"] = dside(E); E = E.sort_values("t"); rows = []
    for (r, ds), g in E.groupby(["race", "ds"]):
        st_ = None; cur = []
        for row in g.itertuples():
            if st_ is None or row.t - st_ >= span:
                if cur: rows.append(cur)
                st_ = row.t; cur = [row]
            else: cur.append(row)
        rows.append(cur)
    out = [dict(race=c[0].race, day=c[0].day, ds=c[0].ds, t=c[0].t, legs=len(c), n=float(np.mean([getattr(x, cl) for x in c]))) for c in rows]
    return pd.DataFrame(out)
def twoway_t(q, c="n"):
    v = q[c].values; v = v - v.mean(); N = len(v)
    def V(g):
        s = pd.Series(v).groupby(np.asarray(g)).sum(); return (s ** 2).sum()
    vr, vd, vb = V(q.race.values), V(q.day.values), V(np.char.add(q.race.values.astype(str), q.day.values.astype(str)))
    var = max(vr + vd - vb, max(vr, vd)) / N ** 2   # conservative: never below the larger one-way variance
    return q[c].mean() / np.sqrt(var), np.sqrt(var)
def summary(q, c="n"):
    r = st(q, c)
    if len(q) < 6: return r
    t2, se = twoway_t(q, c); r["t2way"] = round(t2, 1)
    br = q.groupby("race")[c].sum().sort_values(ascending=False); bd = q.groupby("day")[c].sum().sort_values(ascending=False)
    r["drop3races"] = round(100 * q[~q.race.isin(br.index[:3])][c].mean(), 2); r["drop2days"] = round(100 * q[~q.day.isin(bd.index[:2])][c].mean(), 2)
    return r
