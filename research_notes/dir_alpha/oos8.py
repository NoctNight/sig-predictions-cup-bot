"""Distance from the compression line. Each 5 minutes fit SIG - .5 = k (outside - .5) across legs (rolling 6 h of cross-sections, k from past data only). Residual = SIG minus that line.
Trade: residual -3c or lower, buy the leg; residual +3c or higher, sell it (buy NO). Any price 10-90c, chamber out, one bet per leg per 72 h, hold 72 h. Both periods, same reporting as oos7. Both directions shown apart."""
import pickle, sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
pd.set_option("display.width", 200, "display.max_rows", 100)
L = Lab()
A = pickle.load(open("data/research_diralpha/oos/panel.pkl", "rb"))
def go(lab, S, R, P_, K_, race, office, book=None):
    Mv = S.values; Rv = R.values; n, m = Mv.shape; Fv = ((P_ + K_) / 2).values; race = np.asarray(race, dtype=object); office = np.asarray(office, dtype=object)
    ok = np.isfinite(Mv) & np.isfinite(Fv) & (office != "chamber")[None, :] & (Fv > .03) & (Fv < .97) & (np.abs(P_.values - K_.values) <= .04)
    k = np.full(n, np.nan)
    for t in range(0, n, 12):
        o = ok[t]
        if o.sum() > 60: k[t] = np.sum((Mv[t, o] - .5) * (Fv[t, o] - .5)) / np.sum((Fv[t, o] - .5) ** 2)
    k = pd.Series(k).ffill().rolling(72, min_periods=1).mean().shift(1).values
    Res = Mv - (.5 + k[:, None] * (Fv - .5))
    print(lab, "k mean %.3f, range %.3f-%.3f, residual sd %.2fc" % (np.nanmean(k), np.nanmin(k), np.nanmax(k), 100 * np.nanstd(Res[ok])))
    ra = R.rolling(12, min_periods=1).max().fillna(0).values > 0
    nx = np.full(Mv.shape, -1, int)
    for j in range(m):
        z = -1
        for i in range(n - 1, -1, -1):
            nx[i, j] = z
            if Rv[i, j]: z = i
    out = []
    M = (np.abs(Res) >= .02) & (Mv >= .1) & (Mv <= .9) & ok
    for j in range(m):
        last = -10**9
        for t in np.flatnonzero(M[:, j]):
            if t - last < 864 or not ra[t, j]: continue
            e = nx[t, j]
            if e < 0 or e - t > 24: continue
            s = -1.0 if Res[t, j] > 0 else 1.0; x = -1
            for i in range(e + 864, min(e + 936, n)):
                if Rv[i, j]: x = i; break
            r = dict(j=j, t=t, res=abs(Res[t, j]), rich=Res[t, j] > 0, own=Mv[e, j] if s > 0 else 1 - Mv[e, j], race=race[j], day=S.index[t].strftime("%m-%d"), fav=(s * (Mv[e, j] - .5)) > 0)
            if x < 0: r["n"] = np.nan
            elif book is None: r["n"] = s * (Mv[x, j] - Mv[e, j]) - .0075
            else:
                b, a = book; r["n"] = (b[x, j] - a[e, j]) if s > 0 else (b[e, j] - a[x, j])
            out.append(r); last = t
    E = pd.DataFrame(out).dropna(subset=["n"])
    def st(q):
        if len(q) < 4: return dict(n=len(q))
        r = q.groupby("race").n.mean(); d = q.groupby("day").n.mean()
        return dict(n=len(q), races=q.race.nunique(), net=round(100 * q.n.mean(), 2), win=round((q.n > 0).mean(), 2), t_race=round(r.mean() / (r.std() / np.sqrt(len(r))), 1), t_day=round(d.mean() / (d.std() / np.sqrt(len(d))), 1))
    for a, b in ((.02, .03), (.03, .045), (.045, .07), (.07, 1)):
        q = E[(E.res >= a) & (E.res < b)]
        print(f"  dist {a*100:.1f}-{b*100:.1f}c  all {st(q)}")
        print(f"      SIG cheap, buy {st(q[~q.rich])}"); print(f"      SIG rich, sell {st(q[q.rich])}")
    q = E[E.res >= .03]; print("  3c+, buying the favourite side", st(q[q.fav])); print("  3c+, buying the underdog side", st(q[~q.fav])); print("  3c+ all", st(q)); print("  3c+ cheap", st(q[~q.rich])); print("  3c+ rich", st(q[q.rich]))
    return E
idx = pd.to_datetime(L.idx, unit="s", utc=True)
tp = pd.read_pickle("data/research_diralpha/tape.pkl"); tp = tp[tp.ts >= L.idx[0]].sort_values("ts"); tp["b"] = (tp.ts // 300).astype("int64") * 300; tp["ex"] = tp.exchange_id.astype(str)
last = tp.groupby(["b", "ex"]).price.last().unstack().reindex(index=L.idx, columns=L.cols)
EA = go("A 3-20 Oct", A["S"], A["R"], A["P"], A["K"], A["race"], A["office"])
EB = go("B 21-30 Oct true book", last.ffill(limit=288).set_axis(idx), last.notna().set_axis(idx), L.pm.set_axis(idx), L.k.set_axis(idx), L.race, L.office, book=(L.bid.ffill(limit=12).values, L.ask.ffill(limit=12).values))

EA.to_pickle("data/research_diralpha/oos/line_a.pkl"); EB.to_pickle("data/research_diralpha/oos/line_b.pkl")
import json, sqlite3
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
t = db.execute("select max(ts) from venue_fair").fetchone()[0]
v = pd.read_sql_query("select race,party,exchange_id ex,pm,kalshi,fair,sig_bid,sig_ask from venue_fair where ts=? and exchange_id is not null", db, params=(t,))
v["m"] = (v.sig_bid + v.sig_ask) / 2; x = v[np.isfinite(v.fair) & np.isfinite(v.m) & (v.fair > .03) & (v.fair < .97) & ~v.race.isin(["U.S. House", "U.S. Senate"]) & ((v.pm - v.kalshi).abs() <= .04)]
k = np.sum((x.m - .5) * (x.fair - .5)) / np.sum((x.fair - .5) ** 2); print("k now %.3f" % k)
v["res"] = v.m - (.5 + k * (v.fair - .5))
b = json.load(open("data/books_latest.json")); pos = pd.read_sql_query("select exchange_id ex, sum(quantity) q from fills group by 1", db).set_index("ex").q
rows = []
for r in v[(v.res.abs() >= .03) & (v.m >= .1) & (v.m <= .9) & ((v.pm - v.kalshi).abs() <= .04)].itertuples():
    buy = r.res < 0; bk = b.get(r.ex) or {}; lv = sorted(bk.get("asks", []), key=lambda z: z["price"]) if buy else sorted(bk.get("bids", []), key=lambda z: -z["price"])
    sz = sum(l["quantity"] for l in lv if abs(l["price"] - lv[0]["price"]) <= .0101) if lv else 0
    rows.append(dict(race=r.race, leg=f"{r.party} {'YES' if buy else 'NO'}", side="D" if (r.party == "D") == buy else "R", price=round(r.sig_ask if buy else 1 - r.sig_bid, 3), outside=round(r.fair if buy else 1 - r.fair, 3), dist_c=round(100 * abs(r.res), 1), size_1c=int(sz), held=int(pos.get(r.ex, 0))))
Z = pd.DataFrame(rows).sort_values("dist_c", ascending=False); print(Z.to_string(index=False)); print(Z.side.value_counts().to_dict(), "legs", len(Z)); Z.to_pickle("data/research_diralpha/oos/line_now.pkl")
