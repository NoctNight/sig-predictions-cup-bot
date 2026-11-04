"""Q5: has SIG's gap to the outside fair on favourites started closing faster as 3 Nov approaches? Reads venue_fair read-only.
Entries as in favourite_gap_exit_study.py, entry one 30-min bar after the signal. Gap is measured on the mid so the spread does not count as closure."""
import sqlite3, numpy as np, pandas as pd, json
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
v = pd.read_sql_query("select ts,exchange_id,race,sig_bid,sig_ask,pm,kalshi,fair from venue_fair where exchange_id is not null and sig_bid is not null and sig_ask is not null and fair is not null order by ts", db)
v["b"] = (v.ts // 1800).astype("int64") * 1800
g = v.groupby(["b", "exchange_id"]).last().reset_index()
g = g[(g.sig_ask - g.sig_bid) < .10]
T_END = g.b.max()
print("span", pd.Timestamp(g.b.min(), unit="s"), pd.Timestamp(T_END, unit="s"), "legs", g.exchange_id.nunique())
rows = []
H = [12, 24, 48, 66]
for ex, d in g.groupby("exchange_id"):
    d = d.sort_values("b").set_index("b")
    for side in ("yes", "no"):
        ask = d.sig_ask if side == "yes" else 1 - d.sig_bid
        bid = d.sig_bid if side == "yes" else 1 - d.sig_ask
        mid = (ask + bid) / 2
        f = d.fair if side == "yes" else 1 - d.fair
        for name, mask in (("fav", (ask >= .88) & (ask <= .93) & (f - ask >= .04)),
                           ("fav_loose", (ask >= .80) & (ask <= .95) & (f - mid >= .02)),
                           ("mid", (ask >= .35) & (ask <= .65) & (f - mid >= .02))):
            last = -1e18
            for t in d.index[mask.values]:
                if t - last < 86400: continue
                last = t
                t1 = t + 1800                       # enter one bar later: the signal bar's own noise is not closure
                if t1 not in d.index: continue
                gap0 = f.loc[t1] - mid.loc[t1]
                r = dict(ex=ex, side=side, kind=name, t=t1, race=d.race.loc[t], px=ask.loc[t1], f=f.loc[t1], gap0=gap0, buy=ask.loc[t1], f_sig=f.loc[t], ask_sig=ask.loc[t])
                for h in H:
                    tt = t1 + h * 3600
                    if tt in d.index:
                        r[f"gap{h}"] = f.loc[tt] - mid.loc[tt]
                        r[f"dmid{h}"] = mid.loc[tt] - mid.loc[t1]
                        r[f"df{h}"] = f.loc[tt] - f.loc[t1]
                        r[f"net{h}"] = bid.loc[tt] - ask.loc[t1]
                rows.append(r)
E = pd.DataFrame(rows)
E["day"] = pd.to_datetime(E.t, unit="s").dt.floor("D")
E["days_left"] = (pd.Timestamp("2026-11-03 23:00") - pd.to_datetime(E.t, unit="s")).dt.total_seconds() / 86400
E.to_pickle("data/research_near_settlement/sig_entries.pkl")
def cluster_ols(x, y, cl):
    x = np.column_stack([np.ones(len(x)), x]); b = np.linalg.lstsq(x, y, rcond=None)[0]; e = y - x @ b
    XtXi = np.linalg.inv(x.T @ x); M = np.zeros((2, 2))
    for c in np.unique(cl):
        i = cl == c; s = x[i].T @ e[i]; M += np.outer(s, s)
    G = len(np.unique(cl)); V = XtXi @ M @ XtXi * G / max(1, G - 1)
    return b, np.sqrt(np.diag(V)), G
out = {}
for kind in ("fav", "fav_loose", "mid"):
    q = E[E.kind == kind]
    print("\n==", kind, len(q), "entries", q.race.nunique(), "races, mean gap %.2fc" % (100 * q.gap0.mean()))
    for h in H:
        s = q.dropna(subset=[f"gap{h}"])
        s = s[s.gap0.abs() > .005]
        tab = []
        for lab, a, b in (("21-23 Oct", "2026-10-21", "2026-10-24"), ("24-25", "2026-10-24", "2026-10-26"), ("26-27", "2026-10-26", "2026-10-28"), ("28-29", "2026-10-28", "2026-10-30"), ("30-31", "2026-10-30", "2026-11-01")):
            w = s[(s.day >= a) & (s.day < b)]
            if len(w) < 5: continue
            cl = 1 - w[f"gap{h}"].mean() / w.gap0.mean()
            race = w.groupby("race").apply(lambda z: pd.Series(dict(g0=z.gap0.sum(), g1=z[f"gap{h}"].sum())), include_groups=False)
            bs = []
            rng = np.random.default_rng(1)
            for _ in range(500):
                i = rng.integers(0, len(race), len(race)); bs.append(1 - race.g1.values[i].sum() / race.g0.values[i].sum())
            tab.append(dict(h=h, window=lab, n=len(w), races=len(race), gap0=100 * w.gap0.mean(), close=100 * cl, lo=100 * np.percentile(bs, 16), hi=100 * np.percentile(bs, 84), sig_move=100 * w[f"dmid{h}"].mean(), fair_move=100 * w[f"df{h}"].mean(), net=100 * w[f"net{h}"].mean()))
        t = pd.DataFrame(tab)
        print(t.round(2).to_string(index=False))
        # trend: fraction of each entry's gap closed, on days left, clustered by race
        y = (1 - s[f"gap{h}"] / s.gap0).clip(-2, 2).values
        b, se, G = cluster_ols(s.days_left.values, y, s.race.values)
        print(f"  trend h={h}: closure per day nearer 3 Nov {-100*b[1]:+.2f} pts (se {100*se[1]:.2f}, {G} races, t {-b[1]/se[1]:+.2f})")
        out[f"{kind}_{h}"] = dict(table=tab, slope=-100 * b[1], se=100 * se[1], races=G)
json.dump(out, open("data/research_near_settlement/sig_closure.json", "w"), indent=1)
# the live trade's horizons: from now (T-3) to 2 Nov 18:00 ET = 48 h, to 3 Nov noon ET = 66 h. Late entries only, and a wide-gap cut (the live book's mean is 6c+)
q = E[E.kind == "fav"]
tab = []
for name, w in (("all entries", q), ("from 24 Oct", q[q.day >= "2026-10-24"]), ("from 24 Oct, gap 6c+", q[(q.day >= "2026-10-24") & (q.gap0 >= .06)])):
    for h in (24, 48, 66):
        s = w.dropna(subset=[f"gap{h}"])
        if len(s) < 5: continue
        race = s.groupby("race").agg(g0=("gap0", "sum"), g1=(f"gap{h}", "sum"), net=(f"net{h}", "sum"), n=("gap0", "size"), dm=(f"dmid{h}", "sum"), df=(f"df{h}", "sum"))
        rng = np.random.default_rng(2); bc, bn = [], []
        for _ in range(500):
            i = rng.integers(0, len(race), len(race)); r = race.iloc[i]
            bc.append(1 - r.g1.sum() / r.g0.sum()); bn.append(r.net.sum() / r.n.sum())
        tab.append(dict(sample=name, h=h, n=len(s), races=len(race), gap0=100 * s.gap0.mean(), close=100 * (1 - race.g1.sum() / race.g0.sum()), lo=100 * np.percentile(bc, 16), hi=100 * np.percentile(bc, 84), sig_up=100 * s[f"dmid{h}"].mean(), fair=100 * s[f"df{h}"].mean(), net=100 * s[f"net{h}"].mean(), net_lo=100 * np.percentile(bn, 16), net_hi=100 * np.percentile(bn, 84)))
H_ = pd.DataFrame(tab); H_.to_pickle("data/research_near_settlement/sig_horizons.pkl")
print("\nHorizons of the live trade"); print(H_.round(2).to_string(index=False))
