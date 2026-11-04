from lr_common import *
import glob, os, datetime as dt
L, D, tp = load(); Z = pd.read_pickle("research_notes/dir_alpha/lr_sets.pkl"); dB = D["B"]; dA = D["A"]; qa = book_ctx(L, dB); C, Rc, okc = clean(L, dB, qa); b0, a0 = dB["book"]; mB = (b0 + a0) / 2
cols = list(dB["S"].columns); idx = L.idx
def walk(lv, q, sgn):
    lv = sorted(lv, key=lambda z: sgn * z["price"]); left = q; cost = 0.
    for l in lv:
        t = min(left, l["quantity"]); cost += t * l["price"]; left -= t
        if left <= 0: break
    if left > 0: return np.nan, 0
    return cost / q, 1
rows = []
for f in sorted(glob.glob("data/books_history/books_*.json")):
    ts = dt.datetime.strptime(os.path.basename(f)[6:-5], "%Y%m%dT%H%MZ").replace(tzinfo=dt.timezone.utc).timestamp()
    i = int(np.searchsorted(idx, ts))
    if i >= len(idx): i = len(idx) - 1
    bk = json.load(open(f))
    for j, c in enumerate(cols):
        r = Rc[i, j]; m = mB[i, j]; 
        if not np.isfinite(r) or not okc[i, j] or not (.1 <= m <= .9): continue
        B = bk.get(str(c)); 
        if not B or not B.get("bids") or not B.get("asks"): continue
        ba = min(l["price"] for l in B["asks"]); bb = max(l["price"] for l in B["bids"]); mid = (ba + bb) / 2
        d = dict(f=os.path.basename(f), j=j, res=abs(r), rich=r > 0, sig=abs(r) >= .03, spread=ba - bb)
        for q in (1000, 2000, 5000, 10000, 20000):
            pa, _ = walk(B["asks"], q, 1); pb, _ = walk(B["bids"], q, -1)
            # our entry side then exit on the other side
            e_ = (mid - pb) if r > 0 else (pa - mid); x_ = (pa - mid) if r > 0 else (mid - pb)
            d[f"in{q}"] = e_; d[f"out{q}"] = x_
        rows.append(d)
W = pd.DataFrame(rows); print("snapshots", W.f.nunique(), "leg-snaps", len(W), "signal", int(W.sig.sum()))
S_ = W[W.sig]
print("signal legs: spread %.2fc; cost from mid, one way, entry side (mean, median), round trip:" % (100 * S_.spread.mean()))
for q in (1000, 2000, 5000, 10000, 20000):
    print(f"  {q}: in mean {100*S_[f'in{q}'].mean():.2f}c med {100*S_[f'in{q}'].median():.2f}c | out mean {100*S_[f'out{q}'].mean():.2f}c | RT mean {100*(S_[f'in{q}']+S_[f'out{q}']).mean():.2f}c med {100*(S_[f'in{q}']+S_[f'out{q}']).median():.2f}c p90 {100*(S_[f'in{q}']+S_[f'out{q}']).quantile(.9):.2f}c | depth missing {S_[f'in{q}'].isna().mean():.2f}")
print("  touch-only RT (spread) mean %.2fc; extra at 5k against touch %.2fc" % (100 * S_.spread.mean(), 100 * ((S_["in5000"] + S_["out5000"]).mean() - S_.spread.mean())))
# latest book against line_now
Zn = pd.read_pickle(O + "line_now.pkl"); bl = json.load(open("data/books_latest.json")); key = {(r, p): c for c, r, p in zip(cols, dB["race"], dB["party"])}
rr = []
for r in Zn.itertuples():
    p, sd = r.leg.split(); c = key.get((r.race, p)); B = bl.get(str(c))
    if not B: continue
    ba = min(l["price"] for l in B["asks"]); bb = max(l["price"] for l in B["bids"]); mid = (ba + bb) / 2
    pa, _ = walk(B["asks"], 5000, 1); pb, _ = walk(B["bids"], 5000, -1)
    rr.append(dict(race=r.race, leg=r.leg, side=r.side, d=r.dist_c, spr=100 * (ba - bb), inc=100 * ((pa - mid) if sd == "YES" else (mid - pb)), outc=100 * ((mid - pb) if sd == "YES" else (pa - mid)), size1=r.size_1c, price=r.price))
RR = pd.DataFrame(rr); RR["rt"] = RR.inc + RR.outc; print(RR.sort_values("d", ascending=False).round(2).to_string(index=False)); print("line_now 5k RT mean %.2fc median %.2fc" % (RR.rt.mean(), RR.rt.median()))
# (b) own flow
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
fl = pd.read_sql_query("select exchange_id ex, side, price, quantity q, filled_at, purpose from fills", db); fl["ts"] = (pd.to_datetime(fl.filled_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
print(fl.groupby("side").q.agg(["size", "sum", "min"]).to_string())
# signed q in YES: 'side' yes/no is the resulting position side per schema; quantity sign tells direction?
print(fl.head(6).to_string())
print("tape volume in B, shares", round(tp.quantity.abs().sum()), "own fills", round(fl.q.abs().sum()), "share %.3f" % (fl.q.abs().sum() / tp.quantity.abs().sum()))
