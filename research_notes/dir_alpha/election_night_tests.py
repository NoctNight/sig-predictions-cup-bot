"""Four election-night ideas (A2, A3, A6, A22) on 2022 and 2024 outside prices. Definitions: data/research_diralpha/election/prereg.md
(written before any statistic was run). Read-only: reads data/election_night/, writes data/research_diralpha/election/results.json.

    python research_notes/dir_alpha/election_night_tests.py            # from the repo root
"""
import collections, csv, json, os, re, sys
from datetime import datetime
import numpy as np, pandas as pd

R = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, R)
from sigbot.proposals.election_night import DATA, EST, POLL_CLOSE_ET, ELECTION_DAY, load_2024  # noqa: E402
from sigbot.venues.states import NAME_TO_ABBR  # noqa: E402

OUT = os.path.join(R, "data", "research_diralpha", "election")
COST = 0.5          # pts, round trip at a 1-tick spread (tick 0.5 pt)
RATES = (0.2, 0.5, None)   # SIG walk, pt/min; None = no lag
TYPES = {"LATE_MAIL_D": "PA WI MI VA".split(), "EARLY_D_FIRST": "NC OH IA TX FL GA AZ NV NM".split(),
         "SLOW_WEST": "CA OR WA AK UT ME CO MT HI ID".split()}
MIRAGE = {"LATE_MAIL_D": -1, "EARLY_D_FIRST": 1}


def ctype(st):
    return next((k for k, v in TYPES.items() if st in v), "FAST_OTHER")


def t0(year):
    y, m, d = ELECTION_DAY[year]
    return int(datetime(y, m, d, tzinfo=EST).timestamp())


def et(year, hour):
    return t0(year) + int(round(hour * 3600))


def grid(year):
    return np.arange(et(year, 16), et(year, 36) + 60, 60)


def venue_frame(series, g, limit=30):
    """Series [(ts, p or None)] -> value on the grid; a dropped (None) print stays missing; extreme prices carry on."""
    s = pd.Series({int(t): (np.nan if p is None else p) for t, p in series}, dtype=float).sort_index()
    x = s.reindex(g, method="ffill", tolerance=limit * 60)
    all_ff = s.dropna().reindex(g, method="ffill")
    keep = x.isna() & ((all_ff < 0.03) | (all_ff > 0.97))
    x[keep] = all_ff[keep]
    return x


def blend(venues):
    """Chain the mean of venue minute changes; returns (O, per-venue frame)."""
    df = pd.DataFrame(venues)
    if df.shape[1] == 1:
        o = df.iloc[:, 0].ffill()
        return o, df
    d = df.diff()
    step = d.mean(axis=1, skipna=True).fillna(0)
    first = df.dropna(how="all").index
    if len(first) == 0:
        return None, df
    start = first[0]
    o = (df.loc[start].mean() + step.loc[start:].cumsum()).reindex(df.index)
    return o.clip(0.001, 0.999), df


def load24():
    g = grid(2024)
    out = {}
    for key, d in load_2024().items():
        v = {k: venue_frame(d[k], g) for k in ("pm", "kalshi") if d.get(k)}
        v = {k: x for k, x in v.items() if x.notna().sum() > 30}
        if not v:
            continue
        o, df = blend(v)
        if o is None:
            continue
        st = key[1]
        if st not in POLL_CLOSE_ET:
            continue
        out[(2024,) + key] = dict(year=2024, office=key[0], st=st, dist=key[2], won_d=d["winner"] == "D", O=o.values,
                                  V=df, n_venues=df.shape[1], close=et(2024, POLL_CLOSE_ET[st][0]), g=g)
    return out


def load22(min_usd=50.0, min_trades=5):
    g = grid(2022)
    legs, meta = collections.defaultdict(list), {}
    for r in csv.DictReader(open(os.path.join(DATA, "paths_2022.csv"))):
        if not r.get("price") or r["party"] == "R" or float(r["usd_size"] or 0) < min_usd:
            continue
        legs[r["market_id"]].append((int(float(r["ts"])), float(r["price"])))
        meta[r["market_id"]] = (r["office"], r["outcome"], r["question"])
    out = {}
    for mid, s in legs.items():
        if len(s) < min_trades:
            continue
        office, won, q = meta[mid]
        m = re.search(r"in ([A-Z][A-Za-z ]+)\?", q) or re.search(r"^([A-Z]{2})-(\d+)", q)
        st = NAME_TO_ABBR.get(m.group(1).strip()) if m and len(m.group(1)) > 2 else (m.group(1) if m else None)
        if st not in POLL_CLOSE_ET:
            continue
        dist = m.group(2) if office == "house" and m and m.lastindex and m.lastindex > 1 else ""
        x = pd.Series(dict(sorted(s)), dtype=float)
        x = x[~x.index.duplicated(keep="last")].reindex(g, method="ffill")
        out[(2022, office, st, dist)] = dict(year=2022, office=office, st=st, dist=dist, won_d=won == "1", O=x.values,
                                             V=pd.DataFrame({"pm": x}), n_venues=1, close=et(2022, POLL_CLOSE_ET[st][0]), g=g, n_trades=len(s))
    return out


def idx(L, ts):
    return int((ts - L["g"][0]) // 60)


def at(L, ts):
    i = idx(L, ts)
    return L["O"][i] if 0 <= i < len(L["O"]) else np.nan


def walk(L, r):
    """SIG mid on the grid; r pt/min, None = equal to outside."""
    O = L["O"]
    if r is None:
        return O.copy()
    S = np.full(len(O), np.nan)
    j = np.where(~np.isnan(O))[0]
    if len(j) == 0:
        return S
    S[j[0]] = O[j[0]]
    for i in range(j[0] + 1, len(O)):
        tgt = O[i] if not np.isnan(O[i]) else S[i - 1]
        S[i] = S[i - 1] + np.clip(tgt - S[i - 1], -r / 100, r / 100)
    return S


def logit(p):
    p = np.clip(p, 0.01, 0.99)
    return np.log(p / (1 - p))


CLEAN = [False]     # post-hoc robustness: 2024 legs must have both venues priced, within 5 pts, at the window start


def comp(L, ts):
    p = at(L, ts)
    if np.isnan(p) or not 0.05 <= p <= 0.95:
        return False
    if CLEAN[0] and L["year"] == 2024:
        v = L["V"].iloc[idx(L, ts)]
        return L["n_venues"] == 2 and v.notna().all() and abs(v.iloc[0] - v.iloc[1]) <= 0.05
    return True



NIGHTS = (2022, 2024)


def sel(A, y, pres):
    return [L for L in A.values() if L["year"] == y and (pres or L["office"] != "president")]


def mean(x):
    x = [v for v in x if v is not None and not np.isnan(v)]
    return float(np.mean(x)) if x else float("nan")


def trade(L, s, ten, tex, r, memo={}):
    """Pts per share, s = +1 buy D side. Enter at S(ten), exit at S(tex), half a tick each way (0.5 pt total)."""
    k = (id(L), r)
    if k not in memo:
        memo[k] = walk(L, r)
    S = memo[k]
    i, j = idx(L, ten), min(idx(L, tex), len(S) - 1)
    if np.isnan(S[i]) or np.isnan(S[j]):
        return None
    return s * (S[j] - S[i]) * 100 - COST


# ---------------------------------------------------------------------------------------------- A2
def a2(A, pres):
    out = {}
    for y in NIGHTS:
        Ls = sel(A, y, pres)
        e = [L for L in Ls if L["close"] <= et(y, 19.5) and comp(L, et(y, 19))]
        l = [L for L in Ls if L["close"] >= et(y, 21) and comp(L, et(y, 19.5))]
        ch = lambda L, a, b: (at(L, et(y, b)) - at(L, et(y, a))) * 100
        dl = lambda L, a, b: logit(at(L, et(y, b))) - logit(at(L, et(y, a)))
        x = mean([ch(L, 19, 19.5) for L in e])
        xl = mean([dl(L, 19, 19.5) for L in e])
        ys = np.array([ch(L, 19.5, 21) for L in l])
        yl = np.array([dl(L, 19.5, 21) for L in l])
        same = np.nanmean(np.sign(ys) * np.sign(x) > 0)
        res = dict(n_early=len(e), n_late=len(l), x_pts=x, x_logit=xl, median_early=float(np.nanmedian([ch(L, 19, 19.5) for L in e])),
                   y_pts=float(np.nanmean(ys)), y_median=float(np.nanmedian(ys)), y_logit=float(np.nanmean(yl)), share_same=float(same),
                   beta_logit=float(np.nanmean(yl) / xl) if xl else None,
                   late_same_window=mean([ch(L, 19, 19.5) for L in l]), early_window_detail=sorted([(L["office"][0] + ":" + L["st"] + L["dist"], round(float(ch(L, 19, 19.5)), 1)) for L in e], key=lambda z: z[1]))
        # later exits, all to each leg's close
        res["y_to_close"] = mean([(at(L, L["close"]) - at(L, et(y, 19.5))) * 100 for L in l])
        tr = {}
        s = int(np.sign(x)) if abs(x) >= 0.5 else 0
        for r in RATES:
            p = [trade(L, s, et(y, 19.5), L["close"], r) for L in l] if s else []
            tr[str(r)] = dict(pts=mean(p), n=len([v for v in p if v is not None]), win=float(np.mean([v > 0 for v in p if v is not None])) if p else None)
        res["trade"] = tr
        res["side"] = s
        # time series
        T = np.arange(et(y, 19.5), et(y, 22) + 1, 300)
        X, Y, Y0 = [], [], []
        for t in T:
            eL = [L for L in Ls if L["close"] <= t - 1800 and comp(L, t - 1800)]
            lL = [L for L in Ls if L["close"] >= t + 3600 and comp(L, t)]
            X.append(mean([(at(L, t) - at(L, t - 1800)) * 100 for L in eL]))
            Y.append(mean([(at(L, t + 3600) - at(L, t)) * 100 for L in lL]))
            Y0.append(mean([(at(L, t) - at(L, t - 1800)) * 100 for L in lL]))
        X, Y, Y0 = map(np.array, (X, Y, Y0))
        ok = ~(np.isnan(X) | np.isnan(Y))
        b = float(np.polyfit(X[ok], Y[ok], 1)[0]) if ok.sum() > 5 else None
        c = float(np.corrcoef(X[ok], Y[ok])[0, 1]) if ok.sum() > 5 else None
        c0 = float(np.corrcoef(X[ok], Y0[ok])[0, 1]) if ok.sum() > 5 else None
        b0 = float(np.polyfit(X[ok], Y0[ok], 1)[0]) if ok.sum() > 5 else None
        plc = None
        if c is not None:
            xs, ys2 = X[ok], Y[ok]
            sh = [np.corrcoef(np.roll(xs, k), ys2)[0, 1] for k in range(1, len(xs))]
            plc = float(np.mean(np.array(sh) >= c))
        res["ts"] = dict(n=int(ok.sum()), independent_hours=round(ok.sum() * 5 / 60, 1), slope_next60=b, corr_next60=c, slope_same30=b0, corr_same30=c0, shift_p=plc)
        res["ts_series"] = dict(t=[(int(v) - t0(y)) / 3600 for v in T], x=X.tolist(), y=Y.tolist(), y0=Y0.tolist())
        res["leg_detail"] = sorted([(L["office"][0] + ":" + L["st"] + L["dist"], round(float(v), 1)) for L, v in zip(l, ys)], key=lambda z: z[1])
        out[y] = res
    return out


# ---------------------------------------------------------------------------------------------- A3
def calls_all():
    c = {}
    for r in csv.DictReader(open(os.path.join(DATA, "race_calls.csv"))):
        if not r["call_time_utc"] or "UNVERIFIED" in r["note"] or "default" in r["note"].lower():
            continue
        try:
            ts = datetime.fromisoformat(r["call_time_utc"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        dist = r["district"] or ""
        dist = "special" if "special" in dist.lower() else "".join(ch for ch in dist if ch.isdigit()).lstrip("0")
        k = (int(r["cycle"]), r["office"].lower(), r["state"], dist)
        c[k] = min(c.get(k, 1e13), ts)
    return c


def a3(A, pres):
    C = calls_all()
    out = {}
    specs = [("all", k) for k in (3, 5, 10, 20)] + [("competitive", k) for k in (1, 3, 5)]
    for y in NIGHTS:
        Ls = sel(A, y, pres)
        keyed = {(L["year"], L["office"], L["st"], L["dist"]): L for L in Ls}
        seq = []
        for k, L in keyed.items():
            if k in C:
                pre = at(L, L["close"] - 300)
                if not np.isnan(pre) and C[k] >= et(y, 18):
                    seq.append((C[k], k, L, pre))
        seq.sort(key=lambda z: z[0])
        out[y] = {"n_calls_with_price": len(seq), "first_calls": [(datetime.fromtimestamp(c, EST).strftime("%H:%M"), "/".join(str(v) for v in k[1:] if v), round(float(p), 3), int(L["won_d"])) for c, k, L, p in seq[:12]], "rows": []}
        for kind, kk in specs:
            q = [z for z in seq if kind == "all" or 0.05 <= z[3] <= 0.95]
            if len(q) < kk:
                continue
            tk = q[kk - 1][0]
            used = [z for z in q if z[0] <= tk]
            s = 100 * mean([z[2]["won_d"] - z[3] for z in used])
            tg = []
            for k, L in keyed.items():
                if C.get(k, 1e13) > tk and comp(L, tk):
                    tg.append(L)
            sg = np.sign(s)
            yy = dict(h2=[], noon=[], out=[])
            for L in tg:
                p = at(L, tk + 60)
                yy["h2"].append((at(L, tk + 7260) - p) * 100)
                yy["noon"].append((at(L, et(y, 36)) - p) * 100)
                yy["out"].append((float(L["won_d"]) - p) * 100)
            row = dict(kind=kind, k=kk, n_used=len(used), t_k=datetime.fromtimestamp(tk, EST).strftime("%H:%M"), surprise=s, n_targets=len(tg),
                       **{k: mean(v) for k, v in yy.items()}, share_same=float(np.mean(np.sign(yy["out"]) == sg)) if tg else None)
            for r in RATES:
                p = [trade(L, int(sg), tk + 60, min(max(tk + 7200, L["close"]), et(y, 36)), r) for L in tg]
                row[f"trade_{r}"] = mean(p)
            row["targets"] = [(L, at(L, tk + 60)) for L in tg]
            out[y]["rows"].append(row)
    # leave-one-night-out Brier
    bri = []
    for kind, kk in specs:
        r22 = next((r for r in out[2022]["rows"] if r["kind"] == kind and r["k"] == kk), None)
        r24 = next((r for r in out[2024]["rows"] if r["kind"] == kind and r["k"] == kk), None)
        if not r22 or not r24:
            continue

        def fit(row):
            bs = np.linspace(-0.5, 0.5, 1001)
            p = np.array([v[1] for v in row["targets"]])
            w = np.array([float(v[0]["won_d"]) for v in row["targets"]])
            ll = [np.mean(-w * np.log(q) - (1 - w) * np.log(1 - q)) for q in (1 / (1 + np.exp(-(logit(p) + b * row["surprise"]))) for b in bs)]
            return bs[int(np.argmin(ll))]

        def score(row, b):
            p = np.array([v[1] for v in row["targets"]])
            p = np.where(np.isnan(p), 0.5, p)
            w = np.array([float(v[0]["won_d"]) for v in row["targets"]])
            q = 1 / (1 + np.exp(-(logit(p) + b * row["surprise"])))
            return float(np.mean((p - w) ** 2)), float(np.mean((q - w) ** 2))
        b22, b24 = fit(r22), fit(r24)
        bri.append(dict(kind=kind, k=kk, b_fit_2022=float(b22), b_fit_2024=float(b24), test2024=score(r24, b22), test2022=score(r22, b24)))
    for y in NIGHTS:
        for r in out[y]["rows"]:
            r.pop("targets")
    out["brier"] = bri
    return out


# ---------------------------------------------------------------------------------------------- A6
def a6(A):
    ev = []
    for k, L in A.items():
        c = L["close"]
        if not comp(L, c):
            continue
        p0 = at(L, c)
        i0 = idx(L, c)
        O = L["O"]
        hit = None
        for i in range(i0 + 1, i0 + 61):
            if not np.isnan(O[i]) and abs(O[i] - p0) >= 0.03:
                hit = i
                break
        if hit is None:
            continue
        s = np.sign(O[hit] - p0)
        m = (O[hit] - p0) * 100
        ts = int(L["g"][hit])
        Rh = {h: -s * (at(L, ts + h * 60) - O[hit]) * 100 for h in (60, 120, 180)}
        maxrev = np.nanmax(-s * (O[hit:hit + 181] - O[hit])) * 100
        v2 = False
        if L["n_venues"] == 2:
            V = L["V"]
            vc = (V.iloc[hit] - V.iloc[i0]) * 100
            v2 = bool((s * vc >= 1.5).sum() == 2)
        t = ctype(L["st"])
        mir = MIRAGE.get(t)
        row = dict(year=L["year"], office=L["office"], st=L["st"], dist=L["dist"], type=t, s=int(s), m=float(m), mins=hit - i0, close_h=(c - t0(L["year"])) / 3600,
                   R60=float(Rh[60]), R120=float(Rh[120]), R180=float(Rh[180]), maxrev=float(maxrev), reversed=bool(Rh[180] >= abs(m) / 2), both=v2,
                   mirage_dir=(None if mir is None else bool(s == mir)), toward_winner=bool((s > 0) == L["won_d"]), p0=float(p0))
        for r in (0.2, 0.5):
            S = walk(L, r)
            tgt = S[i0] + s * abs(m) / 200
            j = next((j for j in range(hit, hit + 61) if s * (S[j] - tgt) >= 0), None)
            row[f"tr_{r}"] = None if j is None else float(-s * (S[j + 150] - S[j]) * 100 - COST)
        row["tr_None"] = float(-s * (O[hit + 150] - O[hit]) * 100 - COST)
        ev.append(row)
    return pd.DataFrame(ev)


def summ(d, label):
    if d.empty:
        return dict(group=label, n=0)
    sy = d.groupby(["year", "st"]).agg(R180=("R180", "mean"), R60=("R60", "mean"), tr02=("tr_0.2", "mean"), tr05=("tr_0.5", "mean"), m=("m", lambda x: np.mean(np.abs(x)))).reset_index()
    w = d.groupby(["year", "close_h"]).R180.mean()
    tt = lambda x: float(np.mean(x) / (np.std(x, ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and np.std(x, ddof=1) > 0 else None
    res = dict(group=label, legs=len(d), state_years=len(sy), waves=len(w), abs_move=float(sy.m.mean()), R60=float(sy.R60.mean()), R180=float(sy.R180.mean()), t_state=tt(sy.R180),
               t_wave=tt(w), reversed_share=float(d.reversed.mean()), maxrev=float(d.maxrev.mean()), toward_winner=float(d.toward_winner.mean()), tr02=float(sy.tr02.mean()), tr05=float(sy.tr05.mean()),
               tr_nolag=float(d.tr_None.mean()), by_year={int(y): dict(legs=len(g), states=g.st.nunique(), R180=float(g.groupby("st").R180.mean().mean()), tr02=float(g.groupby("st")["tr_0.2"].mean().mean()), tr05=float(g.groupby("st")["tr_0.5"].mean().mean()), rev=float(g.reversed.mean())) for y, g in d.groupby("year")})
    return res


# ---------------------------------------------------------------------------------------------- A22
def a22(A, pres):
    out = {}
    for y in NIGHTS:
        Ls = [L for L in sel(A, y, pres) if comp(L, et(y, 17)) and L["close"] >= et(y, 19)]
        rows = []
        for L in Ls:
            m = (at(L, et(y, 19)) - at(L, et(y, 17))) * 100
            n1 = (at(L, et(y, 20)) - at(L, et(y, 19))) * 100 if L["close"] >= et(y, 20) else np.nan
            n2 = (at(L, et(y, 21)) - at(L, et(y, 19))) * 100 if L["close"] >= et(y, 21) else np.nan
            rec = dict(st=L["st"], name=L["office"][0] + ":" + L["st"] + L["dist"], m=m, n1=n1, n2=n2, nv=L["n_venues"])
            s = -int(np.sign(m))
            if abs(m) >= 2:
                for r in RATES:
                    rec[f"t1_{r}"] = trade(L, s, et(y, 19), et(y, 20), r) if L["close"] >= et(y, 20) else None
                    rec[f"t2_{r}"] = trade(L, s, et(y, 19), et(y, 21), r) if L["close"] >= et(y, 21) else None
            rows.append(rec)
        d = pd.DataFrame(rows)
        res = dict(n=len(d), m_mean=float(d.m.mean()), m_abs=float(d.m.abs().mean()), n1_mean=float(d.n1.mean()), n2_mean=float(d.n2.mean()))

        def slope(col, demean):
            g = d.dropna(subset=["m", col]).copy()
            if len(g) < 4:
                return None
            if demean:
                g["m"] -= g.m.mean(); g[col] -= g[col].mean()
            b = float((g.m * g[col]).sum() / (g.m ** 2).sum())
            e = g[col] - b * g.m
            cl = (g.m * e).groupby(g.st).sum()
            se = float(np.sqrt((cl ** 2).sum()) / (g.m ** 2).sum())
            return dict(n=len(g), b=b, se=se, t=b / se if se else None, states=int(g.st.nunique()))
        res["slope"] = {c + ("_dm" if dm else ""): slope(c, dm) for c in ("n1", "n2") for dm in (False, True)}
        e = d[d.m.abs() >= 2].copy()
        res["events"] = dict(n=len(e), states=int(e.st.nunique()), abs=float(e.m.abs().mean()) if len(e) else None,
                             rev1=float((-np.sign(e.m) * e.n1).mean()) if len(e) else None, n_rev1=int(e.n1.notna().sum()) if len(e) else 0,
                             rev2=float((-np.sign(e.m) * e.n2).mean()) if len(e) else None, n_rev2=int(e.n2.notna().sum()) if len(e) else 0,
                             up=int((e.m > 0).sum()) if len(e) else 0, down=int((e.m < 0).sum()) if len(e) else 0)
        if len(e):
            for r in RATES:
                res["events"][f"t1_{r}"] = float(pd.to_numeric(e[f"t1_{r}"]).mean())
                res["events"][f"t2_{r}"] = float(pd.to_numeric(e[f"t2_{r}"]).mean())
        res["moves"] = sorted([(a, round(float(b), 1)) for a, b in zip(e.name, e.m)], key=lambda z: z[1]) if len(e) else []
        out[y] = res
    return out


def figure(A, e):
    """Stacked panels on one axis: net points per share on the SIG model. Faint dots are single legs, at a 0.5 pt/min walk."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def a2_legs(y, pres, r):
        Ls = sel(A, y, pres)
        x = mean([(at(L, et(y, 19.5)) - at(L, et(y, 19))) * 100 for L in Ls if L["close"] <= et(y, 19.5) and comp(L, et(y, 19))])
        return [trade(L, int(np.sign(x)), et(y, 19.5), L["close"], r) for L in Ls if L["close"] >= et(y, 21) and comp(L, et(y, 19.5))]

    def a3_legs(y, pres, r, k=5):
        C = calls_all()
        Ls = {(L["year"], L["office"], L["st"], L["dist"]): L for L in sel(A, y, pres)}
        seq = sorted((C[q], q, L, at(L, L["close"] - 300)) for q, L in Ls.items() if q in C and C[q] >= et(y, 18) and not np.isnan(at(L, L["close"] - 300)))
        tk = seq[k - 1][0]
        sg = int(np.sign(mean([z[2]["won_d"] - z[3] for z in seq if z[0] <= tk])))
        return [trade(L, sg, tk + 60, min(max(tk + 7200, L["close"]), et(y, 36)), r) for q, L in Ls.items() if C.get(q, 1e13) > tk and comp(L, tk)]

    def a22_legs(y, pres, r):
        o = []
        for L in sel(A, y, pres):
            if comp(L, et(y, 17)) and L["close"] >= et(y, 21):
                m = (at(L, et(y, 19)) - at(L, et(y, 17))) * 100
                if abs(m) >= 2:
                    o.append(trade(L, -int(np.sign(m)), et(y, 19), et(y, 21), r))
        return o

    def a6_legs(y, r):
        return e[e.year == y][f"tr_{r}"].dropna().tolist()

    panels = [("Idea 1: early closers read through to legs still voting", [("2022, signal is one trade", lambda r: a2_legs(2022, False, r)), ("2024, no president legs", lambda r: a2_legs(2024, False, r)), ("2024, with president legs", lambda r: a2_legs(2024, True, r))]),
              ("Idea 2: first five calls, buy the surprise side", [("2022, calls from 22:46 only", lambda r: a3_legs(2022, False, r)), ("2024, no president legs", lambda r: a3_legs(2024, False, r)), ("2024, with president legs", lambda r: a3_legs(2024, True, r))]),
              ("Idea 3: fade the first 3-point move after a state closes", [("2022", lambda r: a6_legs(2022, r)), ("2024", lambda r: a6_legs(2024, r))]),
              ("Idea 4: fade 17:00-19:00 moves, exit 21:00", [("2022", lambda r: a22_legs(2022, False, r)), ("2024, no president legs", lambda r: a22_legs(2024, False, r)), ("2024, with president legs", lambda r: a22_legs(2024, True, r))])]
    ink, muted, grid = "#1f1e1d", "#6b6a68", "#e4e2dc"
    slow, fast = "#2f6fb0", "#c8553d"
    fig, axs = plt.subplots(4, 1, figsize=(10, 10.6), sharex=True, gridspec_kw=dict(height_ratios=[len(p[1]) for p in panels], hspace=0.28))
    rng = np.random.default_rng(1)
    for ax, (title, rows) in zip(axs, panels):
        for i, (lab, fn) in enumerate(rows):
            yy = len(rows) - 1 - i
            v5 = np.array([v for v in fn(0.5) if v is not None and not np.isnan(v)])
            v2 = np.array([v for v in fn(0.2) if v is not None and not np.isnan(v)])
            ax.scatter(np.clip(v5, -45, 45), yy + rng.uniform(-0.2, 0.2, len(v5)), s=11, color=muted, alpha=0.28, linewidths=0, zorder=2)
            ax.scatter([v2.mean()], [yy], s=75, color=slow, edgecolor="white", linewidth=1.5, zorder=4, marker="o")
            ax.scatter([v5.mean()], [yy], s=75, color=fast, edgecolor="white", linewidth=1.5, zorder=5, marker="D")
            ax.text(46.5, yy, f"{v2.mean():+.1f} / {v5.mean():+.1f}  (n {len(v5)})", va="center", fontsize=9, color=ink)
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels([r[0] for r in rows][::-1], fontsize=9.5, color=ink)
        ax.set_ylim(-0.6, len(rows) - 0.4)
        ax.axvline(0, color=ink, lw=1)
        ax.set_title(title, loc="left", fontsize=10.5, color=ink, fontweight="bold")
        ax.grid(axis="x", color=grid, lw=0.7)
        ax.set_axisbelow(True)
        for sp in ("top", "right", "left"):
            ax.spines[sp].set_visible(False)
        ax.tick_params(length=0, colors=muted)
    axs[0].scatter([], [], s=75, color=slow, label="Average, market maker walks 0.2 pt/min")
    axs[0].scatter([], [], s=75, color=fast, marker="D", label="Average, market maker walks 0.5 pt/min")
    axs[0].scatter([], [], s=14, color=muted, alpha=0.5, label="One leg, 0.5 pt/min walk (clipped at 45)")
    axs[0].legend(loc="lower center", bbox_to_anchor=(0.5, 1.2), ncol=3, frameon=False, fontsize=9)
    axs[-1].set_xlim(-46, 46)
    axs[-1].set_xlabel("Net profit on the SIG model, points per share, after a 0.5-point round trip", fontsize=10, color=ink)
    fig.suptitle("Two election nights: none of the four ideas is proven", x=0.02, ha="left", fontsize=14, color=ink, fontweight="bold", y=0.975)
    fig.text(0.02, 0.012, "Point: 1 percentage point of price, two ticks. Leg: one race's D price. Walk: the market maker moves toward the outside price at a capped speed.\n"
             "Right-hand numbers are the slow / fast walk averages. A night is one national event, so each row is about one observation, however many dots it has.",
             fontsize=8.5, color=muted, va="bottom")
    fig.subplots_adjust(left=0.2, right=0.84, top=0.89, bottom=0.11)
    fig.savefig(os.path.join(R, "research_notes", "figures", "election_night_tests.png"), dpi=150, facecolor="white")


def pj(o):
    return json.dumps(o, indent=1, default=lambda x: x.item() if hasattr(x, "item") else str(x))


if __name__ == "__main__":
    A = {**load24(), **load22()}
    res = {}
    res["a2"] = {"no_pres": a2(A, False), "with_pres": a2(A, True)}
    res["a3"] = {"no_pres": a3(A, False), "with_pres": a3(A, True)}
    e = a6(A)
    groups = [("all", e), ("all no pres", e[e.office != "president"]), ("both venues moved", e[e.both]), ("mirage direction", e[e.mirage_dir == True]), ("not mirage direction", e[e.mirage_dir == False])]  # noqa: E712
    for t in ("LATE_MAIL_D", "EARLY_D_FIRST", "SLOW_WEST", "FAST_OTHER"):
        groups.append((t, e[e.type == t]))
    for t in MIRAGE:
        groups.append((t + " mirage dir", e[(e.type == t) & (e.mirage_dir == True)]))  # noqa: E712
        groups.append((t + " other dir", e[(e.type == t) & (e.mirage_dir == False)]))  # noqa: E712
    res["a6"] = [summ(g, l) for l, g in groups]
    e.to_csv(os.path.join(OUT, "a6_events.csv"), index=False)
    res["a22"] = {"no_pres": a22(A, False), "with_pres": a22(A, True)}
    CLEAN[0] = True
    res["clean"] = {"a2": {"no_pres": a2(A, False), "with_pres": a2(A, True)}, "a3": {"no_pres": a3(A, False), "with_pres": a3(A, True)},
                    "a22": {"no_pres": a22(A, False), "with_pres": a22(A, True)}}
    ec = a6(A)
    ec.to_csv(os.path.join(OUT, "a6_events_clean.csv"), index=False)
    res["clean"]["a6"] = [summ(g, l) for l, g in [("all", ec), ("no pres", ec[ec.office != "president"]), ("mirage direction", ec[ec.mirage_dir == True]), ("not mirage direction", ec[ec.mirage_dir == False]),  # noqa: E712
                                                  ("LATE_MAIL_D", ec[ec.type == "LATE_MAIL_D"]), ("EARLY_D_FIRST", ec[ec.type == "EARLY_D_FIRST"])]]
    CLEAN[0] = False
    # post hoc: longer signal window for A2 (early legs 19:00-20:00, late legs 20:00-21:00), to see whether the sign rests on one trade
    ph = {}
    for y in NIGHTS:
        for pres in (False, True):
            Ls = sel(A, y, pres)
            e_ = [L for L in Ls if L["close"] <= et(y, 19.5) and comp(L, et(y, 19))]
            l_ = [L for L in Ls if L["close"] >= et(y, 21) and comp(L, et(y, 20))]
            ph[f"{y}_{'pres' if pres else 'nopres'}"] = dict(x=mean([(at(L, et(y, 20)) - at(L, et(y, 19))) * 100 for L in e_]), y=mean([(at(L, et(y, 21)) - at(L, et(y, 20))) * 100 for L in l_]),
                                                             y_same=mean([(at(L, et(y, 20)) - at(L, et(y, 19))) * 100 for L in l_]), n_e=len(e_), n_l=len(l_))
    res["a2_posthoc"] = ph
    json.dump(res, open(os.path.join(OUT, "results.json"), "w"), indent=1, default=lambda x: x.item() if hasattr(x, "item") else str(x))
    figure(A, e)
    print(pj(res))
