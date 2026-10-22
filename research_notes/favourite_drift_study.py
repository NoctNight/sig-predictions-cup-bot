"""Favourite drift and calibration in resolved Polymarket two-party election markets (stdlib only).

Question: in races with a ~80/20 or ~90/10 favourite, does the favourite's price drift up or down into election day,
and is a 0.90 favourite really 0.90? Then: does that give the SIG venue's 80/20 and 90/10 legs a directional edge
large enough to beat set arbs and market making?

Pipeline (each step reads only the previous step's cache, nothing is re-downloaded):
    python3 research_notes/favourite_drift_fetch.py    -> data/polymarket_history/raw/ (Gamma events, CLOB histories)
    python3 research_notes/favourite_drift_dataset.py  -> data/polymarket_history/legs.csv, daily_paths.csv
    python3 research_notes/favourite_drift_study.py    -> every table in research_notes/favourite_drift.md
                                                          (also written to data/polymarket_history/tables.md)
The SIG section reads data/polymarket_history/sig_snapshot.json (one poll of the bot's venue_quotes table, written
read-only from data/sigbot.sqlite); pass --refresh-sig to rebuild it from the latest poll.

Unit of analysis: one favourite per race per horizon (the higher-priced of the D and R legs at the window start), so
the two complementary legs of a race are never counted twice. Prices are the last CLOB price at or before
election day 12:00 UTC minus d days.
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import sqlite3
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data", "polymarket_history")
SEED = 20261022
B = 4000                      # bootstrap draws
MIN_VOLUME = 2000.0           # USD traded over the market's life
MIN_OBS = 5                   # price observations in the last 30 days (excludes dead markets)
BUCKETS = [(0.75, 0.85, "75-85"), (0.85, 0.92, "85-92"), (0.92, 0.95, "92-95")]
WINDOWS = [(20, 13), (13, 7), (7, 1), (1, "res"), (13, 1)]
HORIZONS = [20, 13, 7, 1]


# ----------------------------------------------------------------------------------------------- distributions
def _betacf(a, b, x):
    m_max, eps, fpmin = 300, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > fpmin else fpmin)
    h = d
    for m in range(1, m_max + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > fpmin else fpmin)
        c = 1 + aa / c if abs(1 + aa / c) > fpmin else fpmin
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > fpmin else fpmin)
        c = 1 + aa / c if abs(1 + aa / c) > fpmin else fpmin
        de = d * c
        h *= de
        if abs(de - 1) < eps:
            break
    return h


def betainc(a, b, x):
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1 - math.exp(lbt) * _betacf(b, a, 1 - x) / b


def t_two_sided_p(t, df):
    if df <= 0 or not math.isfinite(t):
        return float("nan")
    return betainc(df / 2, 0.5, df / (df + t * t))


def gammainc_upper(s, x):
    """Regularised upper incomplete gamma Q(s, x)."""
    if x <= 0:
        return 1.0
    if x < s + 1:
        term = summ = 1 / s
        n = s
        for _ in range(500):
            n += 1; term *= x / n; summ += term
            if abs(term) < abs(summ) * 1e-15:
                break
        return 1 - summ * math.exp(-x + s * math.log(x) - math.lgamma(s))
    b = x + 1 - s; c = 1 / 1e-300; d = 1 / b; h = d
    for i in range(1, 500):
        an = -i * (i - s); b += 2
        d = an * d + b; d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = b + an / c; c = c if abs(c) > 1e-300 else 1e-300
        de = d * c; h *= de
        if abs(de - 1) < 1e-15:
            break
    return math.exp(-x + s * math.log(x) - math.lgamma(s)) * h


def chi2_sf(x, df):
    return gammainc_upper(df / 2, x / 2)


def norm_cdf(z):
    return 0.5 * math.erfc(-z / math.sqrt(2))


def norm_ppf(p):
    p = min(1 - 1e-12, max(1e-12, p))
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def binom_two_sided_p(k, n, p):
    """Exact two-sided binomial test (sum of outcomes no more likely than the observed one)."""
    if n == 0:
        return float("nan")
    pk = [math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(n + 1)]
    obs = pk[k]
    return min(1.0, sum(x for x in pk if x <= obs * (1 + 1e-9)))


def poisson_binomial_upper(k, probs):
    """Exact P(X >= k) for X = sum of independent Bernoulli(p_i)."""
    dist = [1.0]
    for q in probs:
        nd = [0.0] * (len(dist) + 1)
        for i, v in enumerate(dist):
            nd[i] += v * (1 - q); nd[i + 1] += v * q
        dist = nd
    return sum(dist[k:])


def poisson_binomial_z(k, probs):
    mu = sum(probs); var = sum(q * (1 - q) for q in probs)
    if var <= 0:
        return float("nan"), float("nan")
    z = (k - mu) / math.sqrt(var)
    return z, 2 * (1 - norm_cdf(abs(z)))


# --------------------------------------------------------------------------------------------------- helpers
def fnum(x):
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def t_test(xs):
    n = len(xs)
    if n < 3:
        return float("nan"), float("nan")
    s = st.stdev(xs)
    if s == 0:
        return float("nan"), float("nan")
    t = mean(xs) / (s / math.sqrt(n))
    return t, t_two_sided_p(t, n - 1)


def boot_ci(xs, rng, b=B):
    if len(xs) < 3:
        return float("nan"), float("nan")
    n = len(xs)
    ms = sorted(mean([xs[rng.randrange(n)] for _ in range(n)]) for _ in range(b))
    return ms[int(0.025 * b)], ms[int(0.975 * b) - 1]


def cluster_boot_ci(xs, keys, rng, b=B):
    """Resample whole clusters (cycle x office), so a single national polling miss cannot pass for many draws."""
    groups = {}
    for x, k in zip(xs, keys):
        groups.setdefault(k, []).append(x)
    g = list(groups.values())
    if len(g) < 3:
        return float("nan"), float("nan")
    ms = []
    for _ in range(b):
        pick = [g[rng.randrange(len(g))] for _ in range(len(g))]
        flat = [x for grp in pick for x in grp]
        ms.append(mean(flat))
    ms.sort()
    return ms[int(0.025 * b)], ms[int(0.975 * b) - 1]


def logit(p):
    p = min(0.995, max(0.005, p))
    return math.log(p / (1 - p))


def logistic_fit(xs, ys, iters=50):
    """y ~ Bernoulli(sigmoid(a + b x)); Newton-Raphson; returns a, b, se_a, se_b."""
    a, b = 0.0, 1.0
    for _ in range(iters):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            p = 1 / (1 + math.exp(-(a + b * x)))
            w = p * (1 - p)
            g0 += y - p; g1 += (y - p) * x
            h00 += w; h01 += w * x; h11 += w * x * x
        det = h00 * h11 - h01 * h01
        if det <= 1e-12:
            break
        da = (h11 * g0 - h01 * g1) / det
        db = (-h01 * g0 + h00 * g1) / det
        a += da; b += db
        if abs(da) + abs(db) < 1e-10:
            break
    det = h00 * h11 - h01 * h01
    se_a = math.sqrt(h11 / det) if det > 0 else float("nan")
    se_b = math.sqrt(h00 / det) if det > 0 else float("nan")
    return a, b, se_a, se_b


PRIOR_SD = 0.25   # prior on calibration: a ~ N(0, 0.25^2), b ~ N(1, 0.25^2); b in 1.0-1.5 spans the FLB literature


def map_fit(xs, ys, iters=60):
    """Penalised (MAP) logistic fit: priors a ~ N(0, PRIOR_SD^2), b ~ N(1, PRIOR_SD^2) added to the Newton step, so the
    strong a-b correlation of the uncentred fit is respected (separate shrinkage of a and b is not the posterior)."""
    a, b, lam = 0.0, 1.0, 1 / PRIOR_SD ** 2
    for _ in range(iters):
        g0, g1, h00, h01, h11 = -lam * a, -lam * (b - 1), lam, 0.0, lam
        for x, y in zip(xs, ys):
            q = 1 / (1 + math.exp(-(a + b * x)))
            w = q * (1 - q)
            g0 += y - q; g1 += (y - q) * x; h00 += w; h01 += w * x; h11 += w * x * x
        det = h00 * h11 - h01 * h01
        da, db = (h11 * g0 - h01 * g1) / det, (-h01 * g0 + h00 * g1) / det
        a += da; b += db
        if abs(da) + abs(db) < 1e-10:
            break
    return a, b


def fmt(x, d=3, sign=False):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return (f"{x:+.{d}f}" if sign else f"{x:.{d}f}")


def pfmt(p):
    if p is None or not math.isfinite(p):
        return "n/a"
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


# ------------------------------------------------------------------------------------------------------ data
def read_legs_csv(path, source):
    legs = []
    if not os.path.exists(path):
        return legs
    with open(path) as f:
        for r in csv.DictReader(f):
            d = {k: r[k] for k in ("event_id", "slug", "cycle", "office", "party", "market_id")}
            d["source"] = source
            d["volume"] = fnum(r.get("volume_usd")) or 0.0
            try:
                d["outcome"] = int(round(float(r["outcome"])))
            except (TypeError, ValueError):
                continue
            d["n_obs"] = int(float(r.get("n_obs_30d") or 0))
            for h in (30, 25, 20, 13, 7, 3, 1, 0):
                d[h] = fnum(r.get(f"p{h}"))
            legs.append(d)
    return legs


def load_legs():
    """Polymarket legs (main sample) and Kalshi legs (replication on the same 2024 races, never pooled)."""
    sys.path.insert(0, HERE)
    for mod in ("favourite_drift_dataset", "favourite_drift_extra", "favourite_drift_kalshi"):
        try:
            m = __import__(mod)
            if hasattr(m, "write_csvs"):
                m.write_csvs()
            elif mod == "favourite_drift_kalshi":
                m.main([])                                  # cache-only rebuild of kalshi_legs.csv
            elif hasattr(m, "main"):
                m.main()
        except Exception as e:  # noqa: BLE001
            print(f"({mod} rebuild skipped: {e}; reading the existing CSV)", file=sys.stderr)
    pm = read_legs_csv(os.path.join(DATA, "legs.csv"), "polymarket")
    have = {(l["market_id"], l["party"]) for l in pm if l[20] is not None or l[13] is not None}
    for l in read_legs_csv(os.path.join(DATA, "extra_legs.csv"), "polymarket"):
        if (l["market_id"], l["party"]) not in have:        # extra file may re-supply 2022 legs that had no prices
            pm = [x for x in pm if (x["market_id"], x["party"]) != (l["market_id"], l["party"])] + [l]
    ka = read_legs_csv(os.path.join(DATA, "kalshi_legs.csv"), "kalshi")
    return pm, ka


def races(legs, min_volume=MIN_VOLUME, min_obs=MIN_OBS):
    """{race key: [legs]}. An event normally is one race; events that hold several races (the 2022 Polymarket
    events, one two-outcome market per state) are split by market id."""
    by_event = {}
    for l in legs:
        if l["volume"] < min_volume or l["n_obs"] < min_obs:
            continue
        by_event.setdefault((l["source"], l["event_id"]), []).append(l)
    out = {}
    for key, ls in by_event.items():
        parties = [l["party"] for l in ls]
        if len(parties) != len(set(parties)):
            for l in ls:
                out.setdefault((key, l["market_id"]), []).append(l)
        else:
            out[key] = ls
    return out


def favourites_at(race_map, h):
    """One favourite per race at horizon h: the leg with the highest price, kept if that price is in [0.75, 0.95]."""
    favs = []
    for key, ls in race_map.items():
        ok = [l for l in ls if l[h] is not None]
        if not ok:
            continue
        fav = max(ok, key=lambda l: l[h])
        if 0.75 <= fav[h] <= 0.95:
            favs.append(fav)
    return favs


def bucket_of(p):
    for lo, hi, name in BUCKETS:
        if lo <= p < hi or (name == "92-95" and p == hi):
            return name
    return None


# ------------------------------------------------------------- martingale null for "fraction rose" (probit model)
def fit_k(pairs):
    """Latent margin M is Brownian; price p(tau) = Phi(M / sqrt(1 + k tau)) with tau in days to the election and the
    election-night polling error normalised to variance 1. That price is a martingale. Fit k (pre-election variance per
    day relative to election-night variance) by matching the mean squared probit change across all windows."""
    obs = [(norm_ppf(min(0.995, max(0.005, a))), norm_ppf(min(0.995, max(0.005, b))), ta, tb) for a, b, ta, tb in pairs]
    target = mean([(zb - za) ** 2 for za, zb, _, _ in obs])
    best = None
    for i in range(1, 400):
        k = 10 ** (-4 + 4 * i / 400)      # 1e-4 .. 1
        sse = 0.0
        pred = []
        for za, _, ta, tb in obs:
            sa, sb = math.sqrt(1 + k * ta), math.sqrt(1 + k * tb)
            ma = za * sa
            pred.append(ma * ma * (1 / sb - 1 / sa) ** 2 + k * (ta - tb) / sb ** 2)
        sse = (mean(pred) - target) ** 2
        if best is None or sse < best[0]:
            best = (sse, k)
    return best[1], target


def p_rise_martingale(p, ta, tb, k):
    za = norm_ppf(min(0.995, max(0.005, p)))
    sa, sb = math.sqrt(1 + k * ta), math.sqrt(1 + k * tb)
    ma = za * sa
    r = sb / sa
    sd = math.sqrt(k * (ta - tb))
    return norm_cdf(ma * (1 - r) / sd) if sd > 0 else 0.5


# ------------------------------------------------------------------------------------------------- the study
def window_rows(favs_by_h, k, rng, label_filter=None):
    rows = []
    for a, b in WINDOWS:
        favs = favs_by_h[a]
        for lo, hi, name in BUCKETS + [(0.75, 0.95, "all 75-95")]:
            sel = [l for l in favs if (bucket_of(l[a]) == name if name != "all 75-95" else True)]
            if label_filter:
                sel = [l for l in sel if label_filter(l)]
            if b == "res":
                sel = [l for l in sel]
                d = [l["outcome"] - l[a] for l in sel]
            else:
                sel = [l for l in sel if l[b] is not None]
                d = [l[b] - l[a] for l in sel]
            if not d:
                rows.append([f"{a}d->{b if b == 'res' else str(b) + 'd'}", name, 0] + ["n/a"] * 9)
                continue
            up = sum(1 for x in d if x > 1e-9); down = sum(1 for x in d if x < -1e-9)
            p_sign = binom_two_sided_p(up, up + down, 0.5) if up + down else float("nan")
            if b == "res":
                exp_up = [l[a] for l in sel]             # martingale: P(rise to 1) = price
            else:
                exp_up = [p_rise_martingale(l[a], a, b, k) for l in sel]
            moved = [(x, e) for x, e in zip(d, exp_up) if abs(x) > 1e-9]
            z, p_mart = poisson_binomial_z(up, [e for _, e in moved]) if moved else (float("nan"), float("nan"))
            t, p_t = t_test(d)
            lo_ci, hi_ci = boot_ci(d, rng)
            clo, chi = cluster_boot_ci(d, [(l["cycle"], l["office"]) for l in sel], rng)
            rows.append([f"{a}d->{b if b == 'res' else str(b) + 'd'}", name, len(d), fmt(mean(d), 3, True), fmt(st.median(d), 3, True),
                         f"{up}/{up + down}", pfmt(p_sign), f"{mean([e for _, e in moved]):.2f}" if moved else "n/a", pfmt(p_mart),
                         f"{fmt(t, 2, True)} / {pfmt(p_t)}", f"[{fmt(lo_ci, 3, True)}, {fmt(hi_ci, 3, True)}]",
                         f"[{fmt(clo, 3, True)}, {fmt(chi, 3, True)}]"])
    return rows


def calibration_rows(favs_by_h, rng):
    rows, hl = [], []
    for h in HORIZONS:
        favs = favs_by_h[h]
        stat, groups = 0.0, 0
        for lo, hi, name in BUCKETS + [(0.75, 0.95, "all 75-95")]:
            sel = favs if name == "all 75-95" else [l for l in favs if bucket_of(l[h]) == name]
            n = len(sel)
            if n == 0:
                rows.append([f"{h}d", name, 0] + ["n/a"] * 7)
                continue
            wins = sum(l["outcome"] for l in sel)
            pbar = mean([l[h] for l in sel])
            buy = [1000 * (l["outcome"] - l[h]) / l[h] for l in sel]
            sell = [1000 * (l[h] - l["outcome"]) / (1 - l[h]) for l in sel]
            # exact test of wins against the Poisson-binomial mean, approximated by a binomial at the mean price
            p_bin = poisson_binomial_upper(wins, [l[h] for l in sel])     # one-sided: favourites win this often or more
            losses = n - wins
            upset_hi = 3 / n if losses == 0 else None
            blo, bhi = boot_ci(buy, rng)
            rows.append([f"{h}d", name, n, fmt(pbar), f"{wins}/{n} = {wins / n:.3f}", pfmt(p_bin),
                         f"<= {upset_hi:.3f}" if upset_hi is not None else f"{losses / n:.3f}",
                         f"{mean(buy):+.0f} [{blo:+.0f}, {bhi:+.0f}]", f"{mean(sell):+.0f}",
                         f"{wins / n - pbar:+.3f}"])
            if name != "all 75-95":
                e = sum(l[h] for l in sel)
                v = sum(l[h] * (1 - l[h]) for l in sel)
                if v > 0:
                    stat += (wins - e) ** 2 / v; groups += 1
        hl.append((h, stat, groups, chi2_sf(stat, groups) if groups else float("nan")))
    return rows, hl


def slope_rows(race_map):
    """Logistic calibration over ALL races with a 0.5-0.99 favourite at each horizon: logit P(win) = a + b logit(price).
    b > 1 means favourites are underpriced (prices compressed toward 50%), b < 1 means they are overpriced."""
    rows = []
    for h in HORIZONS:
        xs, ys = [], []
        for ls in race_map.values():
            ok = [l for l in ls if l[h] is not None]
            if not ok:
                continue
            fav = max(ok, key=lambda l: l[h])
            if 0.5 <= fav[h] <= 0.99:
                xs.append(logit(fav[h])); ys.append(fav["outcome"])
        if len(xs) < 10:
            continue
        a, b, sa, sb = logistic_fit(xs, ys)
        ma, mb = map_fit(xs, ys)
        rows.append((h, len(xs), a, b, sa, sb, sum(ys), ma, mb))
    return rows


def robustness_rows(favs_by_h, rng):
    rows = []
    for key in ("cycle", "office"):
        vals = sorted({l[key] for h in HORIZONS for l in favs_by_h[h]})
        for v in vals:
            f20 = [l for l in favs_by_h[20] if l[key] == v and l[1] is not None]
            d = [l[1] - l[20] for l in f20]
            f13 = [l for l in favs_by_h[13] if l[key] == v]
            wins = sum(l["outcome"] for l in f13)
            pbar = mean([l[13] for l in f13]) if f13 else float("nan")
            buy = [1000 * (l["outcome"] - l[13]) / l[13] for l in f13]
            rows.append([f"{key}={v}", len(d), fmt(mean(d), 3, True) if d else "n/a", pfmt(t_test(d)[1]) if d else "n/a",
                         len(f13), fmt(pbar), f"{wins / len(f13):.3f}" if f13 else "n/a", f"{mean(buy):+.0f}" if buy else "n/a"])
    return rows


# ------------------------------------------------------------------------------------------------- SIG mapping
def refresh_sig_snapshot():
    db = os.path.join(ROOT, "data", "sigbot.sqlite")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    ts = cur.execute("select ts from venue_quotes group by ts having count(*) > 500 order by ts desc limit 1").fetchone()[0]
    d = {}
    for venue, race, party, inst, bid, ask, mid, bsz, asz in cur.execute(
            "select venue, race, party, instrument, bid, ask, mid, bid_sz, ask_sz from venue_quotes where ts=?", (ts,)):
        d.setdefault((race, party), {})[venue] = dict(bid=bid, ask=ask, mid=mid, bid_sz=bsz, ask_sz=asz, inst=inst)
    legs = []
    for (race, party), v in d.items():
        s = v.get("sig")
        if not s or s["mid"] is None:
            continue
        outs = [x for x in (v.get("polymarket", {}).get("mid"), v.get("kalshi", {}).get("mid")) if x is not None]
        legs.append(dict(race=race, party=party, sig_bid=s["bid"], sig_ask=s["ask"], sig_mid=s["mid"],
                         pm=v.get("polymarket", {}).get("mid"), kalshi=v.get("kalshi", {}).get("mid"),
                         outside=sum(outs) / len(outs) if outs else None, sig_ex=s["inst"]))
    with open(os.path.join(DATA, "sig_snapshot.json"), "w") as f:
        json.dump({"ts": ts, "legs": legs}, f, indent=1)


def sig_rows(fit_ab, shrunk_ab):
    """EV per 1,000 bought at the SIG ask and held to settlement, under three views of the true probability:
    (1) the outside blend (Polymarket/Kalshi) is calibrated; (2) the outside blend corrected by our calibration fit
    shrunk toward no bias; (3) corrected by the raw fit (an upper bound: one cycle, wide standard errors)."""
    snap = json.load(open(os.path.join(DATA, "sig_snapshot.json")))
    cal = lambda ab, p: 1 / (1 + math.exp(-(ab[0] + ab[1] * logit(p))))  # noqa: E731
    rows, per_leg = [], []
    for o in snap["legs"]:
        ask, out = o.get("sig_ask"), o.get("outside")
        if ask is None or out is None or not (0.75 <= ask <= 0.95):
            continue
        q_s, q_f = cal(shrunk_ab, out), cal(fit_ab, out)
        per_leg.append(dict(o, q_out=out, q_shr=q_s, q_fit=q_f, ev_out=1000 * (out - ask) / ask,
                            ev_shr=1000 * (q_s - ask) / ask, ev_fit=1000 * (q_f - ask) / ask))
    for lo, hi, name in BUCKETS + [(0.75, 0.95, "all 75-95")]:
        sel = [x for x in per_leg if (bucket_of(x["sig_ask"]) == name if name != "all 75-95" else True)]
        if not sel:
            continue
        rows.append([name, len(sel), fmt(mean([x["sig_ask"] for x in sel])), fmt(mean([x["q_out"] for x in sel])),
                     f"{mean([x['ev_out'] for x in sel]):+.0f}", f"{mean([x['ev_shr'] for x in sel]):+.0f}",
                     f"{mean([x['ev_fit'] for x in sel]):+.0f}", sum(1 for x in sel if x["ev_out"] >= 50)])
    per_leg.sort(key=lambda x: -x["ev_out"])
    return snap["ts"], rows, per_leg


# ------------------------------------------------------------------------------------------------------- main
def main():
    if "--refresh-sig" in sys.argv:
        refresh_sig_snapshot()
    rng = random.Random(SEED)
    legs, kalshi = load_legs()
    rmap = races(legs)
    favs_by_h = {h: favourites_at(rmap, h) for h in HORIZONS}
    out = []
    # sample
    by = {}
    for ls in rmap.values():
        by.setdefault((ls[0]["cycle"], ls[0]["office"]), 0)
        by[(ls[0]["cycle"], ls[0]["office"])] += 1
    sample = [[c, o, n, sum(1 for l in favs_by_h[20] if l["cycle"] == c and l["office"] == o),
               sum(1 for l in favs_by_h[13] if l["cycle"] == c and l["office"] == o),
               sum(1 for l in favs_by_h[7] if l["cycle"] == c and l["office"] == o)] for (c, o), n in sorted(by.items())]
    sample.append(["all", "", len(rmap), len(favs_by_h[20]), len(favs_by_h[13]), len(favs_by_h[7])])
    out.append("### Sample (races with volume >= %d USD and >= %d price points in the last 30 days)\n" % (MIN_VOLUME, MIN_OBS))
    out.append(table(["cycle", "office", "races", "fav in 75-95 at 20d", "at 13d", "at 7d"], sample))
    # martingale null
    pairs = []
    for a, b in WINDOWS:
        if b == "res":
            continue
        for l in favs_by_h[a]:
            if l[b] is not None:
                pairs.append((l[a], l[b], a, b))
    k, msq = fit_k(pairs)
    out.append(f"\nMartingale null for 'rose' (probit Brownian model, fitted k = {k:.4f} per day, i.e. {1 / k:.0f} days of "
               f"pre-election news = one election-night polling error; mean squared probit change {msq:.3f}).\n")
    # drift
    out.append("### Drift of the favourite's price (window start bucket)\n")
    out.append(table(["window", "bucket", "n", "mean chg", "median chg", "rose", "sign p (vs 50%)", "E[rose] if martingale",
                      "sign p (vs martingale)", "t / p", "bootstrap 95% CI", "cluster CI (cycle x office)"],
                     window_rows(favs_by_h, k, rng)))
    # path-conditioned subset requested in the brief
    inband = lambda l: all(l[h] is not None and 0.75 <= l[h] <= 0.95 for h in (20, 13, 7))  # noqa: E731
    sub = [r for r in window_rows(favs_by_h, k, rng, inband) if r[1] == "all 75-95"]
    out.append("\n### Same, restricted to favourites inside 75-95 at 20d, 13d AND 7d (conditions on the future path: biased toward zero drift)\n")
    out.append(table(["window", "bucket", "n", "mean chg", "median chg", "rose", "sign p (vs 50%)", "E[rose] if martingale",
                      "sign p (vs martingale)", "t / p", "bootstrap 95% CI", "cluster CI (cycle x office)"], sub))
    # calibration
    cal, hl = calibration_rows(favs_by_h, rng)
    out.append("\n### Calibration: favourite's win rate vs price, and P&L per 1,000 held to resolution\n")
    out.append(table(["horizon", "bucket", "n", "mean price", "won", "P(wins >= obs) exact", "upset rate (rule of 3 if 0)",
                      "buy fav per 1k [price-only CI]", "sell fav per 1k", "win - price"], cal))
    out.append("\nHosmer-Lemeshow over the three buckets (df = groups, prices are not fitted): " +
               "; ".join(f"{h}d chi2 {s:.2f} df {g} p {pfmt(p)}" for h, s, g, p in hl))
    sl = slope_rows(rmap)
    out.append("\n### Logistic calibration slope, all races with a 50-99 favourite: logit P(win) = a + b logit(price)\n")
    out.append(table(["horizon", "n", "fav won", "a (se)", "b (se)", "b = 1 test p", "implied P(win) at 0.80 / 0.90 / 0.95", "MAP a / b"],
                     [[f"{h}d", n, w, f"{a:+.2f} ({sa:.2f})", f"{b:.2f} ({sb:.2f})", pfmt(2 * (1 - norm_cdf(abs((b - 1) / sb)))),
                       " / ".join(f"{1 / (1 + math.exp(-(a + b * logit(p)))):.3f}" for p in (0.8, 0.9, 0.95)),
                       f"{_ma:+.2f} / {_mb:.2f}"]
                      for h, n, a, b, sa, sb, w, _ma, _mb in sl]))
    # robustness
    out.append("\n### Robustness: drift 20d->1d and calibration at 13d by cycle and office\n")
    out.append(table(["subset", "n drift", "mean chg 20d->1d", "t p", "n at 13d", "mean price", "win rate", "buy per 1k"],
                     robustness_rows(favs_by_h, rng)))
    # Kalshi replication (same 2024 races, different venue: a check on the drift, not new outcomes)
    kmap = races(kalshi, min_volume=1000, min_obs=3)
    kfav = {h: favourites_at(kmap, h) for h in HORIZONS}
    krows = [r for r in window_rows(kfav, k, rng) if r[1] == "all 75-95"]
    kcal, khl = calibration_rows(kfav, rng)
    out.append("\n### Kalshi replication, 2024 (markets opened 7-31 Oct 2024, so 20d and 13d samples are thin)\n")
    out.append(table(["window", "bucket", "n", "mean chg", "median chg", "rose", "sign p (vs 50%)", "E[rose] if martingale",
                      "sign p (vs martingale)", "t / p", "bootstrap 95% CI", "cluster CI (cycle x office)"], krows))
    out.append("")
    out.append(table(["horizon", "bucket", "n", "mean price", "won", "P(wins >= obs) exact", "upset rate (rule of 3 if 0)",
                      "buy fav per 1k [price-only CI]", "sell fav per 1k", "win - price"], [r for r in kcal if r[1] == "all 75-95"]))
    # SIG
    s13 = [r for r in sl if r[0] == 13] or sl
    h, n, a, b, sa, sb, w, ma, mb = s13[0]
    fit_ab, shrunk_ab = (a, b), (ma, mb)
    out.append(f"\nCalibration used for SIG (13d fit): raw a {a:+.2f} b {b:.2f}; MAP with priors a~N(0,{PRIOR_SD}^2), b~N(1,{PRIOR_SD}^2): "
               f"a {shrunk_ab[0]:+.2f} b {shrunk_ab[1]:.2f}.")
    ts, srows, per_leg = sig_rows(fit_ab, shrunk_ab)
    import time as _t
    out.append(f"\n### SIG legs with ask in 75-95 (snapshot {_t.strftime('%Y-%m-%d %H:%M UTC', _t.gmtime(ts))}): EV per 1,000 bought at the ask, held to 4 Nov\n")
    out.append(table(["bucket", "legs", "mean SIG ask", "outside blend", "EV/1k outside calibrated", "EV/1k MAP FLB",
                      "EV/1k raw fit (upper)", "legs >= +50/1k (outside)"], srows))
    out.append("\nTop legs by EV per 1,000 at the outside blend (buy YES at ask):\n")
    out.append(table(["leg", "SIG bid/ask", "Poly", "Kalshi", "outside", "EV/1k outside", "EV/1k MAP"],
                     [[f"{x['race']} [{x['party']}]", f"{x['sig_bid']:.3f}/{x['sig_ask']:.3f}", fmt(x['pm']), fmt(x['kalshi']),
                       fmt(x['q_out']), f"{x['ev_out']:+.0f}", f"{x['ev_shr']:+.0f}"] for x in per_leg[:12]]))
    text = "\n".join(out) + "\n"
    with open(os.path.join(DATA, "tables.md"), "w") as f:
        f.write(text)
    json.dump({"k": k, "calibration_13d_fit": fit_ab, "calibration_13d_shrunk": shrunk_ab, "slopes": sl, "hl": hl,
               "sig_legs": [{kk: x[kk] for kk in ("race", "party", "sig_ask", "outside", "q_shr", "ev_out", "ev_shr", "ev_fit")} for x in per_leg]},
              open(os.path.join(DATA, "study_summary.json"), "w"), indent=1, default=str)
    print(text)


if __name__ == "__main__":
    main()
