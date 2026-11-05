"""Were entry and exit prices real? Compare touch with tape prints, own orders, recorded top-of-book size, book snapshots."""
import sys, json, glob, re; sys.path.insert(0, "research_notes/dir_alpha")
import numpy as np, pandas as pd
import audit_common as c
G = c.Grid(); sf = G.flow(); tp = G.tape
d = pd.read_pickle(c.CACHE + "/d_stale.pkl")
f = c.raw("fills"); o = c.raw("orders"); q = c.raw("quotes")
f["ts"] = (pd.to_datetime(f.filled_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
print("order example: mm side/price vs quote"); 
ex = o[o.purpose == "mm"].tail(6)[["exchange_id", "side", "action", "price", "quantity", "placed_at", "status", "expires_at"]]
print(ex.to_string())
# tape is ours? match fills on (ex, price, |qty|, within 10 s)
fk = f.assign(q=f.quantity.abs()).sort_values("ts")
tp["ours"] = False
print("tape rows", len(tp), "fills", len(f), "fills qty", fk.q.sum(), "tape qty", tp.q.sum())
byf = {k: v for k, v in fk.groupby("exchange_id")}
tp2 = tp.sort_values("ts")
by = {k: v for k, v in tp2.groupby("exchange_id")}
polls = G.P["ts"].values
qq = {k: v.sort_values("ts") for k, v in q.groupby("exchange_id")}
rows = []
for k in d.itertuples():
    for leg, b, sd in (("entry", k.x, k.s), ("exit", k.y, -k.s)):
        # sd=+1: we buy YES at ask, sd=-1: we sell YES at bid
        t0 = polls[b, k.j]; p = (G.ask if sd > 0 else G.bid).values[b, k.j]; other = (G.bid if sd > 0 else G.ask).values[b, k.j]
        a = by.get(k.ex)
        r = dict(i=k.Index, leg=leg, sd=sd, t0=t0, p=p, other=other, spr=abs(p - other))
        if a is not None:
            for w in (300, 1800):
                m = a[(a.ts >= t0 - w) & (a.ts <= t0 + w)]
                good = m[(m.price <= p + 1e-9)] if sd > 0 else m[(m.price >= p - 1e-9)]       # prints at our price or better
                r[f"n{w}"] = len(m); r[f"vol{w}"] = m.q.sum(); r[f"at{w}"] = len(good); r[f"atvol{w}"] = good.q.sum()
                if len(m): r[f"vwap{w}"] = (m.price * m.q).sum() / m.q.sum()
            m = a[(a.ts >= t0 - 300) & (a.ts <= t0 + 300)]
            agg = m[m.aggressor == ("buy" if sd > 0 else "sell")]
            r["aggr_px"] = (agg.price * agg.q).sum() / agg.q.sum() if len(agg) else np.nan   # what same-side takers actually paid
            r["aggr_vol"] = agg.q.sum()
            lastp = a[a.ts <= t0].tail(1)
            r["last_age_m"] = (t0 - lastp.ts.iloc[0]) / 60 if len(lastp) else np.nan
            r["last_dev"] = (lastp.price.iloc[0] - p) if len(lastp) else np.nan
        z = qq.get(k.ex)
        if z is not None:
            zz = z[(z.ts <= t0 + 60) & (z.ts >= t0 - 300)].tail(1)
            if len(zz):
                y = zz.iloc[0]
                r["q_age"] = t0 - y.ts; r["q_bid"] = y.best_bid; r["q_ask"] = y.best_ask
                r["q_sz"] = y.ask_qty if sd > 0 else y.bid_qty
                r["q_px"] = y.best_ask if sd > 0 else y.best_bid
        # own resting order that would BE the touch: mm/inside. To be the YES ask, we 'buy NO' at 1-ask or 'sell YES' at ask
        w = o[(o.exchange_id == k.ex) & (o.placed_at <= t0) & (o.placed_at >= t0 - 150)]
        if sd > 0:
            m_ = w[((w.side == "no") & ((1 - w.price - p).abs() < 1e-6)) | ((w.side == "yes") & (w.action == "sell") & ((w.price - p).abs() < 1e-6))]
        else:
            m_ = w[((w.side == "yes") & (w.action == "buy") & ((w.price - p).abs() < 1e-6)) | ((w.side == "no") & (w.action == "sell") & ((1 - w.price - p).abs() < 1e-6))]
        r["own_at_touch"] = m_.quantity.sum()
        rows.append(r)
R = pd.DataFrame(rows); R = R.merge(d[["net", "race", "t", "party", "s"]], left_on="i", right_index=True)
R.to_pickle(c.CACHE + "/prices.pkl")
pd.set_option("display.width", 220, "display.max_columns", 40)
for leg, x in R.groupby("leg"):
    print("\n==", leg, len(x))
    print("spread at fill c: ", (100 * x.spr).describe()[["mean", "50%", "max"]].round(2).to_dict(), " share 0.5c", round((x.spr < .0051).mean(), 2), " 1c", round((x.spr.between(.0051, .0101)).mean(), 2))
    print("any print +/-5m", round((x.n300.fillna(0) > 0).mean(), 2), "+/-30m", round((x.n1800.fillna(0) > 0).mean(), 2))
    print("print at our px or better +/-5m", round((x.at300.fillna(0) > 0).mean(), 2), "+/-30m", round((x.at1800.fillna(0) > 0).mean(), 2))
    print("median prints vol +/-5m", x.vol300.median(), "at-or-better vol +/-5m", x.atvol300.median(), "+/-30m", x.atvol1800.median())
    print("recorded touch size: n", int(x.q_sz.notna().sum()), x.q_sz.describe(percentiles=[.1, .25, .5, .75]).round(0).to_dict())
    print("q_px == venue_fair touch", round(((x.q_px - x.p).abs() < 1e-6)[x.q_px.notna()].mean(), 3), "mean abs diff c", round(100 * (x.q_px - x.p).abs().mean(), 3))
    print("adverse excess of same-side taker px vs touch, c (positive = worse than touch):", round(100 * (x.sd * (x.aggr_px - x.p)).mean(), 3), "n", int(x.aggr_px.notna().sum()))
    print("last print age min", x.last_age_m.describe()[["50%", "max"]].round(1).to_dict(), " sd*(last - p) c", round(100 * (x.sd * x.last_dev).mean(), 2))
    print("own order at touch share", round((x.own_at_touch > 0).mean(), 3))
    print(" net by own-at-touch:", x.groupby(x.own_at_touch > 0).net.agg(["size", lambda v: 100 * v.mean()]).round(2).to_dict("index"))
    print(" net by print at px +/-5m:", x.groupby(x.at300.fillna(0) > 0).net.agg(["size", lambda v: 100 * v.mean()]).round(2).to_dict("index"))
E=R[R.leg=="entry"].set_index("i"); X=R[R.leg=="exit"].set_index("i")
ok = (E.at1800.fillna(0)>0) & (X.at1800.fillna(0)>0)
print("\nboth prices printed at or better within 30 m:", ok.sum(), "net", round(100*d.net[ok[ok].index].mean(),2), " neither/rest net", round(100*d.net[ok[~ok].index].mean(),2))
ok5 = (E.at300.fillna(0)>0) & (X.at300.fillna(0)>0)
print("both within 5 m:", ok5.sum(), "net", round(100*d.net[ok5[ok5].index].mean(),2), " rest net", round(100*d.net[ok5[~ok5].index].mean(),2))
# trade-only pnl: buy at same-side taker VWAP at entry, sell at same-side taker VWAP at exit (+/- 30 min), where both exist
def px(R_, w):
    return R_
# books snapshots
files = sorted(glob.glob("data/books_history/books_*.json"))
tm = np.array([pd.Timestamp(re.search(r"books_(\d+T\d+Z)", x).group(1), tz="UTC").timestamp() for x in files])
print("\nbook snapshots", len(files), "from", files[0][-21:], "to", files[-1][-21:])
near = []
for r in R.itertuples():
    k = np.argmin(np.abs(tm - r.t0))
    if abs(tm[k] - r.t0) <= 1800:
        near.append((r.Index, k))
print("legs with a snapshot within 30 m:", len(near))
cache = {}
sz = []
for ix, k in near:
    r = R.loc[ix]
    ex = d.ex[r.i]
    B = cache.setdefault(k, json.load(open(files[k])))
    b = B.get(str(ex))
    if not b: continue
    lv = b["asks"] if r.sd > 0 else b["bids"]
    if not lv: continue
    bestp = lv[0]["price"]; 
    q0 = lv[0]["quantity"]
    w2 = sum(l["quantity"] for l in lv if abs(l["price"] - bestp) <= .0101)
    sz.append(dict(leg=r.leg, touch_qty=q0, w2=w2, bestp=bestp, p=r.p, dt=(tm[k]-r.t0)/60))
Z = pd.DataFrame(sz)
if len(Z):
    print(Z.groupby("leg")[["touch_qty", "w2"]].describe(percentiles=[.5]).round(0).T.to_string())
    print("snapshot best vs touch abs diff c", round(100*(Z.bestp-Z.p).abs().mean(),2))
