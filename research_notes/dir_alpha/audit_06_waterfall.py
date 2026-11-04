"""Waterfall from 2.02c to a corrected number, what the 24 h P&L is made of, independence of the corrected set, selection optimism."""
import sys, sqlite3; sys.path.insert(0, "research_notes/dir_alpha")
import numpy as np, pandas as pd
import audit_common as c
pd.set_option("display.width", 220, "display.max_columns", 40, "display.max_rows", 200)
G = c.Grid(); G0 = c.Grid(ffill=0); sf = G.flow(); M = G.masks(sf); n = len(G.idx); vf = c.raw("vf")
L = {k: v.sort_values("ts")[["ts", "sig_bid", "sig_ask"]].values for k, v in vf.groupby("exchange_id")}
fresh = G.fresh.values
def r(nm, x):
    day = x.groupby("day").net.mean()
    print(f"{nm:60s} n {len(x):4d} races {x.race.nunique():3d} net {100*x.net.mean():6.2f}c med {100*x.net.median():5.2f} win {100*(x.net>0).mean():4.0f}% days+ {(day>0).sum()}/{len(day)} se(day) {100*boot(x):.2f}")
def boot(x):
    rng = np.random.default_rng(1); ds = x.day.unique()
    return np.std([np.concatenate([x.net.values[x.day.values == q] for q in rng.choice(ds, len(ds))]).mean() for _ in range(1000)])
def fills(Gx, e, sk):
    out = []; ft = []
    for k in e.itertuples():
        a = L[k.ex]; t0 = Gx.P["ts"].values[k.i, k.j]
        f_ = np.searchsorted(a[:, 0], t0, side="left") + sk
        if f_ >= len(a): out.append(np.nan); continue
        y = np.searchsorted(a[:, 0], a[f_, 0] + 86400, side="left")
        if y >= len(a): out.append(np.nan); continue
        out.append((a[y, 1] - a[f_, 2]) if k.s > 0 else (a[f_, 1] - a[y, 2]))
    return e.assign(net=out).dropna(subset=["net"])
def dedupe(e):
    e = e.sort_values("x").reset_index(drop=True); keep, last = [], {}
    for k in e.itertuples():
        if k.race in last and k.x - last[k.race] < c.HOLD: continue
        keep.append(k.Index); last[k.race] = k.x
    return e.loc[keep]
s0 = G.trades(M["full"]); r("S0 reported", s0)
s1 = fills(G, G.trades(M["full"] & fresh), 0); r("S1 entry on a real poll, exit first real poll >= 24 h", s1)
s2 = fills(G, G.trades(M["full"] & fresh), 1); r("S2 + fill at the next poll", s2)
s3 = dedupe(s2); r("S3 + one trade per race per 24 h", s3)
S = G0.S48; sg = np.sign(S); lag = lambda x: x - x.ffill(limit=3).shift(c.HOLD)
both = ((lag(G0.P["pm"]) * sg >= .01) & (lag(G0.P["kalshi"]) * sg >= .01)).values
flow = (sf.rolling(c.HOLD, min_periods=1).sum() * sg >= -5000).values
band = ((G0.mid >= .15) & (G0.mid <= .85)).values & ~G0.chamber[None, :]
s4 = fills(G0, dedupe(G0.trades(both & flow & band, need_exit=False)), 1); r("S4 + no ffill in median or lags (strict)", s4)
s4b = s3[s3.t >= pd.Timestamp("2026-10-23 12:00", tz="UTC")]; r("S3 from 23 Oct 12:00 (after first-valid-bin batch)", s4b)
print("first-valid batch 10-22 23:45-10-23 04:00 in S0:", int((s0.t < pd.Timestamp("2026-10-23 04:00", tz="UTC")).sum()), "net", round(100 * s0.net[s0.t < pd.Timestamp("2026-10-23 04:00", tz="UTC")].mean(), 2))

print("\n--- what is the P&L made of (S3)")
Fa, Mi = G.fair.values, G.mid.values
u = s3.copy()
yy = np.minimum(u.y.values, n - 1)
u["dfair"] = u.s * (Fa[yy, u.j] - Fa[u.x, u.j]); u = u.dropna(subset=["dfair"])
u["ds"] = u.s * np.where(u.party == "D", 1, np.where(u.party == "R", -1, 0))
X = np.c_[np.ones(len(u)), u.dfair.values]; b = np.linalg.lstsq(X, u.net.values, rcond=None)[0]
print("net = a + b * further outside move: a %.2fc, b %.2f" % (100 * b[0], b[1]), " mean further outside move %.2fc" % (100 * u.dfair.mean()), "median %.2fc" % (100 * u.dfair.median()))
print(u.groupby("ds")[["net", "dfair"]].agg(["size", "mean"]).round(4).to_string())
# unconditional outside continuation on all band legs: does fair keep going after a 24h move >= 1c on both venues?
fw = (G.fair.shift(-288) - G.fair); bk = (G.fair - G.fair.shift(288)); dd = np.sign(bk)
mask = M["base"] & (np.abs(bk.values) >= .01)
print("outside continuation all band legs after a 24 h fair move >= 1c: mean signed next 24 h fair move c", round(100 * np.nanmean((fw * dd).values[mask]), 2))

print("\n--- independence (S3)")
u = u.sort_values("x"); u["hit"] = u.net > 0
ep, last, cur, k_ = [], {}, {}, 0
for t in u.itertuples():
    if t.x - last.get(t.ds, -10**9) > 144: k_ += 1; cur[t.ds] = k_
    last[t.ds] = t.x; ep.append(cur[t.ds])
u["ep"] = ep; E = u.groupby("ep").agg(n=("net", "size"), net=("net", "mean"), a=("t", "min"), b=("t", "max"), ds=("ds", "first"))
print(E.round(3).to_string())
print("clusters", len(E), "t", round(E.net.mean() / (E.net.std() / np.sqrt(len(E))), 2), "mean net", round(100 * E.net.mean(), 2))
ch = (G.mid * np.where(G.party == "D", 1.0, np.where(G.party == "R", -1.0, 0.0))).diff(288).iloc[::12]
Cj = ch.iloc[:, u.j.values].corr().fillna(0).values; xs, ys, sg_ = u.x.values, u.y.values, u.ds.values
ov = np.array([np.clip(np.minimum(ys[a], ys) - np.maximum(xs[a], xs), 0, None) / 288 for a in range(len(u))])
Q = ov * Cj * np.outer(sg_, sg_); np.fill_diagonal(Q, 1); Q2 = ov * np.abs(Cj); np.fill_diagonal(Q2, 1)
print("effective bets: signed corr", round(len(u) ** 2 / Q.sum(), 1), " unsigned corr (worst case)", round(len(u) ** 2 / Q2.sum(), 1), " n", len(u), " states", u.race.str.replace(r"-\d\d House race| Senate| Governor", "", regex=True).nunique())

print("\n--- selection optimism: day-block bootstrap of the d4 grid, pick best in-bag, score out-of-bag")
tape = G.tape; s_ = np.sign(G.S48)
vol = tape.groupby(["b", "exchange_id"]).q.sum().unstack().reindex(index=G.idx, columns=G.cols).fillna(0).rolling(c.HOLD, min_periods=1).sum()
dm = G.mid - G.mid.shift(288); df_ = G.fair - G.fair.shift(288)
bo, fl = M["both"], M["flowok"]; thin = (vol < 60000).values; ny = ((dm * s_) <= .3 * (df_ * s_).clip(lower=.005)).values; base = M["base"]
R = {"base": base, "both": base & bo, "flow": base & fl, "both+flow": base & bo & fl, "both+thin": base & bo & thin, "both+notyet": base & bo & ny, "both+flow+notyet": base & bo & fl & ny, "both+flow+thin": base & bo & fl & thin, "flow+thin": base & fl & thin}
ofc = np.array(["house" if "House race" in x else "senate" if x.endswith("Senate") else "governor" if x.endswith("Governor") else "other" for x in G.race])
days = sorted(set(G.t.strftime("%m-%d"))); recs = []; nm = []
for th in (.015, .02, .03):
    for k, m in R.items():
        for off in (None, "house", "governor", "senate"):
            for h in (144, 288, 576):
                mm = m.copy()
                if off: mm[:, ofc != off] = False
                e = G.trades(mm, th=th, hold=h)
                if len(e) < 12: continue
                g = e.groupby("day").net.agg(["sum", "size"]).reindex(days).fillna(0)
                recs.append(g.values); nm.append((th, k, off, h))
A = np.array(recs)                                              # config, day, (sum, count)
rng = np.random.default_rng(5); opt = []; oob = []; pick = []
for _ in range(3000):
    w = rng.multinomial(len(days), np.ones(len(days)) / len(days))
    inb = (A[:, :, 0] * w).sum(1) / np.maximum((A[:, :, 1] * w).sum(1), 1); cnt = (A[:, :, 1] * w).sum(1)
    inb = np.where(cnt >= 12, inb, -9); b_ = inb.argmax()
    o = w == 0; oo = A[b_, o, 0].sum() / max(A[b_, o, 1].sum(), 1)
    if A[b_, o, 1].sum() >= 5:
        opt.append(inb[b_] - oo); oob.append(oo); pick.append(nm[b_])
print("configs", len(nm), " in-bag best minus out-of-bag same config, mean c", round(100 * np.mean(opt), 2), " mean oob c", round(100 * np.mean(oob), 2), " median oob", round(100 * np.median(oob), 2))
print("most-picked:", pd.Series([str(p) for p in pick]).value_counts().head(4).to_dict())
# same but fix hold 24 h and office all, only choosing filter and threshold (the real last step of the search)
ix = [i for i, p in enumerate(nm) if p[3] == 288 and p[2] is None]
sub = A[ix]; opt2 = []; oob2 = []
for _ in range(3000):
    w = rng.multinomial(len(days), np.ones(len(days)) / len(days)); cnt = (sub[:, :, 1] * w).sum(1)
    inb = np.where(cnt >= 12, (sub[:, :, 0] * w).sum(1) / np.maximum(cnt, 1), -9); b_ = inb.argmax(); o = w == 0
    if sub[b_, o, 1].sum() >= 5:
        oo = sub[b_, o, 0].sum() / sub[b_, o, 1].sum(); opt2.append(inb[b_] - oo); oob2.append(oo)
print("hold 24 h, all offices, choose filter+threshold: optimism c", round(100 * np.mean(opt2), 2), " mean oob", round(100 * np.mean(oob2), 2))
# empirical-Bayes shrink against the whole ledger: all configs ever run, 24h, broad
led = pd.read_json("data/research_diralpha/ledger.jsonl", lines=True); print("ledger rows now", len(led))
l24 = led[(led.h == "24h") & (led.office == "all")].dropna(subset=["all_net_c"])
fam = l24.groupby("family").all_net_c.max()
print("24 h all-office: configs", len(l24), "families", len(fam), " family-best net c quantiles", fam.quantile([.5, .9, .99, 1]).round(2).to_dict())
m_, t2 = fam.mean(), max(fam.var() - .36 ** 2, 0)
x_ = 100 * s3.net.mean(); se = 100 * boot(s3)
print(f"EB shrink of S3 ({x_:.2f}c, se {se:.2f}) with prior N({m_:.2f}, {t2:.2f}): {(t2 * x_ + se**2 * m_) / (t2 + se**2):.2f}c")
con = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
print("venue_fair rows out of ts order by rowid:", con.execute("select count(*) from (select ts, lag(ts) over (order by rowid) p from venue_fair) where ts<p").fetchone()[0])
print("venue_fair total rows, min rowid, max rowid:", con.execute("select count(*),min(rowid),max(rowid) from venue_fair").fetchone())
# last-24h signals: S3 style with whatever hold exists, versus same elapsed hold on earlier trades
Bq, Aq = G.bid.values, G.ask.values
v = G.S48.values; late = []
m = M["full"] & fresh
seen = {}
for j in range(v.shape[1]):
    d_ = s0[s0.j == j]; nxt = (d_.i.max() + c.HOLD) if len(d_) else 0
    for i in np.flatnonzero((np.abs(v[:, j]) >= c.TH) & m[:, j]):
        if i < nxt or i + c.HOLD < n: continue
        y = n - 1
        while y > i and not (fresh[y, j] and np.isfinite(Bq[y, j])): y -= 1
        s = np.sign(v[i, j]); late.append(dict(race=G.race[j], i=i, h=(y - i) / 12, net=(Bq[y, j] - Aq[i, j]) if s > 0 else (Bq[i, j] - Aq[y, j]))); nxt = i + c.HOLD
Lt = pd.DataFrame(late).sort_values("i"); Lt = Lt.groupby("race").head(1)
# matched: earlier trades valued at the same elapsed hours
vals = []
for t in s0.itertuples():
    for h in Lt.h.values:
        y = t.x + int(h * 12)
        if y < n and np.isfinite(Bq[y, t.j]): vals.append((Bq[y, t.j] - Aq[t.x, t.j]) if t.s > 0 else (Bq[t.x, t.j] - Aq[y, t.j]))
print(f"held-out last-day signals, one per race: n {len(Lt)}, mean hold {Lt.h.mean():.1f} h, net so far {100*Lt.net.mean():.2f}c, win {(Lt.net>0).mean():.2f}; earlier trades at same elapsed hours {100*np.mean(vals):.2f}c")
