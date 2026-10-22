"""Mean-reversion prediction model on 5-minute candles (stdlib only).

    python3 -m sigbot.meanrev --train --candles data/candles_5m.json     # fit, walk-forward validation report, save params
    python3 -m sigbot.meanrev --report --candles data/candles_5m.json    # 20 markets with the largest current predicted move

Model
  Ridge regression (z-scored features, intercept unpenalised) of the next 1-bar and next 6-bar close change of a
  market's YES price on features built from that market's own candle window plus the other legs of its race.
  Features (see FEATURES):
    d1, d3, d6, d12        close change over the last 1/3/6/12 bars
    dev_ewma12, dev_ewma36 close minus its 12- and 36-bar EWMA
    range_vol              (high - low) of the current bar / rolling 36-bar stdev of 1-bar changes
    vol_z, tc_z            volume and tradeCount z-scores against the previous 36 bars
    comp_dev               sum of the race's leg closes (this leg + other legs as-of the same timestamp) - 1
    dist_half              close - 0.5
    hour_sin, hour_cos     UTC hour of day on the unit circle
    gap_log                log(minutes since the previous bar / 5): candles exist only for bars with trades,
                           so "1 bar" is 5 minutes only about half the time
  Validation is walk-forward in time: rows whose target is fully realised before the 70 % time cutoff train the
  model (ridge strength picked on the last 30 % of the training window), rows at or after the cutoff are the test
  set. Nothing from the test window touches the fit (feature scaling is from the training rows too).

Programmatic use (what run.py / mm.py will call)
  params = load()                                   # params/meanrev.json
  f = build_features(candles, others=[other_leg_candles, ...])   # dict of the features at the last bar
  dp = predict(f, horizon=1, params=params)         # expected change in the YES price over the next bar
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, Sequence

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARAMS_PATH = os.path.join(HERE, "params", "meanrev.json")
TICK = 0.005
HALF_SPREAD = 0.0025          # one-tick spread: taker pays 0.005 round trip, a passive fill earns a half tick
MIN_PRICE, MAX_PRICE = 0.05, 0.95
HORIZONS = (1, 6)
WARMUP = 12                   # bars needed before the first feature row
ZWIN = 36                     # window for the volume / tradeCount z-scores and the range/vol ratio
ZCLIP = 6.0                   # z-scored features are clipped here (train and predict)
FEATURES = ["d1", "d3", "d6", "d12", "dev_ewma12", "dev_ewma36", "range_vol", "vol_z", "tc_z",
            "comp_dev", "dist_half", "hour_sin", "hour_cos", "gap_log"]
THRESHOLDS = (0.0, 0.0025, 0.005, 0.01, 0.015)
LAMBDAS = (1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0, 10.0)


# --------------------------------------------------------------------------- time helpers
def parse_time(s) -> float:
    if isinstance(s, (int, float)):
        return float(s) / (1000.0 if s > 1e11 else 1.0)
    s = str(s).replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


# --------------------------------------------------------------------------- one market's candle window
class Series:
    """Candles of one market, ascending in time, with the rolling quantities precomputed in O(n)."""

    def __init__(self, candles: Sequence[dict]):
        c = [x for x in candles if x.get("close") is not None]
        self.t = [parse_time(x["time"]) for x in c]
        self.close = [float(x["close"]) for x in c]
        self.high = [float(x.get("high", x["close"])) for x in c]
        self.low = [float(x.get("low", x["close"])) for x in c]
        self.vol = [float(x.get("volume") or 0.0) for x in c]
        self.tc = [float(x.get("tradeCount") or 0.0) for x in c]
        n = len(c)
        self.n = n
        self.ewma12, self.ewma36 = [0.0] * n, [0.0] * n
        a12, a36 = 2.0 / 13.0, 2.0 / 37.0
        for i, p in enumerate(self.close):
            self.ewma12[i] = p if i == 0 else self.ewma12[i - 1] + a12 * (p - self.ewma12[i - 1])
            self.ewma36[i] = p if i == 0 else self.ewma36[i - 1] + a36 * (p - self.ewma36[i - 1])

    def asof_close(self, ts: float) -> Optional[float]:
        """Last close at or before ts (None if the series starts later)."""
        k = bisect.bisect_right(self.t, ts)
        return self.close[k - 1] if k > 0 else None

    def _z(self, arr: list[float], i: int) -> float:
        w = arr[max(0, i - ZWIN):i]
        if len(w) < 6:
            return 0.0
        m = sum(w) / len(w)
        sd = math.sqrt(sum((v - m) ** 2 for v in w) / len(w))
        return (arr[i] - m) / sd if sd > 1e-9 else 0.0

    def features(self, i: int, others: Sequence["Series"] = ()) -> Optional[dict]:
        if i < WARMUP or i >= self.n:
            return None
        c, p = self.close, self.close[i]
        chg = [c[k] - c[k - 1] for k in range(max(1, i - ZWIN + 1), i + 1)]
        sig = math.sqrt(sum(v * v for v in chg) / len(chg)) if chg else 0.0   # zero-mean stdev of 1-bar changes
        sig = max(sig, TICK / 2)
        comp = 0.0
        if others:
            tot, ok = p, True
            for o in others:
                q = o.asof_close(self.t[i])
                if q is None:
                    ok = False
                    break
                tot += q
            comp = (tot - 1.0) if ok else 0.0
        hour = (self.t[i] % 86400.0) / 3600.0
        gap_min = max(5.0, (self.t[i] - self.t[i - 1]) / 60.0)
        return {
            "d1": p - c[i - 1], "d3": p - c[i - 3], "d6": p - c[i - 6], "d12": p - c[i - 12],
            "dev_ewma12": p - self.ewma12[i], "dev_ewma36": p - self.ewma36[i],
            "range_vol": (self.high[i] - self.low[i]) / sig,
            "vol_z": self._z(self.vol, i), "tc_z": self._z(self.tc, i),
            "comp_dev": comp, "dist_half": p - 0.5,
            "hour_sin": math.sin(2 * math.pi * hour / 24.0), "hour_cos": math.cos(2 * math.pi * hour / 24.0),
            "gap_log": math.log(gap_min / 5.0),
        }


def build_features(candles: Sequence[dict], others: Sequence[Sequence[dict]] = (), i: Optional[int] = None) -> Optional[dict]:
    """Features for one market at bar i (default: the last bar). `others` are the candle lists of the race's
    other legs (used for the complement sum, as-of the bar's timestamp). None if fewer than WARMUP+1 bars."""
    s = Series(candles)
    return s.features(s.n - 1 if i is None else i, [Series(o) for o in others])


# --------------------------------------------------------------------------- small linear algebra
def solve(A: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; A is modified."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        d = M[col][col]
        if abs(d) < 1e-14:
            d = 1e-14
        for r in range(col + 1, n):
            f = M[r][col] / d
            if f:
                rowc, rowr = M[col], M[r]
                for k in range(col, n + 1):
                    rowr[k] -= f * rowc[k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))
        x[r] = s / (M[r][r] if abs(M[r][r]) > 1e-14 else 1e-14)
    return x


def inverse(A: list[list[float]]) -> list[list[float]]:
    n = len(A)
    cols = [solve(A, [1.0 if r == j else 0.0 for r in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def matmul(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(len(B))) for j in range(len(B[0]))] for i in range(len(A))]


class Ridge:
    """Ridge on z-scored features. Fit from the cross-product matrices so that drop-one refits are free."""

    def __init__(self, names: list[str], mean: list[float], std: list[float]):
        self.names, self.mean, self.std = names, mean, std
        self.coef: list[float] = [0.0] * len(names)
        self.intercept = 0.0
        self.tstats: list[float] = [0.0] * len(names)
        self.lam = 0.0

    def zrow(self, f) -> list[float]:
        v = [f[n] for n in self.names] if isinstance(f, dict) else list(f)
        return [max(-ZCLIP, min(ZCLIP, (x - m) / s)) for x, m, s in zip(v, self.mean, self.std)]

    @staticmethod
    def crossprod(Z: list[list[float]], y: list[float]):
        k = len(Z[0])
        ZZ = [[0.0] * k for _ in range(k)]
        Zy = [0.0] * k
        for z, t in zip(Z, y):
            for a in range(k):
                za = z[a]
                if za == 0.0:
                    continue
                Zy[a] += za * t
                row = ZZ[a]
                for b in range(a, k):
                    row[b] += za * z[b]
        for a in range(k):
            for b in range(a):
                ZZ[a][b] = ZZ[b][a]
        return ZZ, Zy

    def fit_from(self, ZZ, Zy, n: int, ymean: float, zmean: list[float], lam: float, keep: Optional[list[int]] = None,
                 yy: Optional[float] = None):
        """Solve (Zc'Zc + lam*n I) b = Zc'yc on centred data. ZZ/Zy are uncentred cross products, so we centre
        them here (Zc'Zc = Z'Z - n m m', Zc'yc = Z'y - n m ybar)."""
        idx = keep if keep is not None else list(range(len(self.names)))
        k = len(idx)
        A = [[ZZ[a][b] - n * zmean[a] * zmean[b] + (lam * n if a == b else 0.0) for b in idx] for a in idx]
        rhs = [Zy[a] - n * zmean[a] * ymean for a in idx]
        beta = solve(A, rhs)
        coef = [0.0] * len(self.names)
        for j, a in enumerate(idx):
            coef[a] = beta[j]
        self.coef, self.lam = coef, lam
        self.intercept = ymean - sum(c * m for c, m in zip(coef, zmean))
        if yy is not None and n > k + 1:
            # sandwich variance of ridge: sigma^2 A^-1 (Zc'Zc) A^-1
            Ainv = inverse([row[:] for row in A])
            S = [[ZZ[a][b] - n * zmean[a] * zmean[b] for b in idx] for a in idx]
            V = matmul(matmul(Ainv, S), Ainv)
            sse = (yy - n * ymean * ymean) - sum(beta[j] * rhs[j] for j in range(k))
            s2 = max(sse, 1e-18) / (n - k - 1)
            ts = [0.0] * len(self.names)
            for j, a in enumerate(idx):
                se = math.sqrt(max(V[j][j] * s2, 1e-30))
                ts[a] = beta[j] / se
            self.tstats = ts
        return self

    def predict_z(self, z: list[float]) -> float:
        return self.intercept + sum(c * v for c, v in zip(self.coef, z))

    def to_json(self) -> dict:
        return {"coef": self.coef, "intercept": self.intercept, "lambda": self.lam, "tstats": self.tstats}


# --------------------------------------------------------------------------- data loading
def load_candles(path: str) -> dict[str, Series]:
    raw = json.load(open(path))
    out = {}
    for ex, h in raw.items():
        s = Series(h.get("candles", []) if isinstance(h, dict) else h)
        if s.n > WARMUP + max(HORIZONS):
            out[str(ex)] = s
    return out


def load_universe(path: str):
    from .universe import Universe
    return Universe.from_markets(json.load(open(path)))


def race_legs(series: dict[str, Series], universe) -> dict[str, list[Series]]:
    """exchange id -> Series of the other legs of its race (those with candles)."""
    out = {}
    for ex in series:
        race = universe.race_of(ex)
        if race is None:
            out[ex] = []
            continue
        out[ex] = [series[o] for o in race.exchange_ids if o != ex and o in series]
    return out


# --------------------------------------------------------------------------- training
def build_rows(series: dict[str, Series], others: dict[str, list[Series]]):
    """One row per (market, bar) with price inside [MIN_PRICE, MAX_PRICE] and all horizons available."""
    H = max(HORIZONS)
    rows = []
    for ex, s in series.items():
        oth = others.get(ex, [])
        for i in range(WARMUP, s.n - H):
            p = s.close[i]
            if not (MIN_PRICE <= p <= MAX_PRICE):
                continue
            f = s.features(i, oth)
            if f is None:
                continue
            rows.append({
                "ex": ex, "i": i, "t": s.t[i], "x": [f[n] for n in FEATURES],
                "y": {h: s.close[i + h] - p for h in HORIZONS},
                "t_end": {h: s.t[i + h] for h in HORIZONS},
                # would a passive order one tick inside the close have been touched during the next bar?
                "touch_dn": s.low[i + 1] <= p - HALF_SPREAD, "touch_up": s.high[i + 1] >= p + HALF_SPREAD,
            })
    rows.sort(key=lambda r: r["t"])
    return rows


def quantile(vals: list[float], q: float) -> float:
    v = sorted(vals)
    return v[min(len(v) - 1, int(q * len(v)))]


def scaling(rows, idx_train) -> tuple[list[float], list[float]]:
    k = len(FEATURES)
    mean = [0.0] * k
    for i in idx_train:
        x = rows[i]["x"]
        for j in range(k):
            mean[j] += x[j]
    n = max(1, len(idx_train))
    mean = [m / n for m in mean]
    var = [0.0] * k
    for i in idx_train:
        x = rows[i]["x"]
        for j in range(k):
            var[j] += (x[j] - mean[j]) ** 2
    std = [math.sqrt(v / n) if v / n > 1e-18 else 1.0 for v in var]
    return mean, std


def r2(y: list[float], yhat: list[float]) -> tuple[float, float]:
    """(R2 around the mean of y, R2 against predicting zero)."""
    n = len(y)
    if n < 2:
        return float("nan"), float("nan")
    m = sum(y) / n
    sse = sum((a - b) ** 2 for a, b in zip(y, yhat))
    sst = sum((a - m) ** 2 for a in y)
    ss0 = sum(a * a for a in y)
    return (1 - sse / sst if sst > 0 else float("nan")), (1 - sse / ss0 if ss0 > 0 else float("nan"))


def evaluate(rows, idx_test, Z, model: Ridge, h: int) -> dict:
    y = [rows[i]["y"][h] for i in idx_test]
    yhat = [model.predict_z(Z[i]) for i in idx_test]
    r2m, r20 = r2(y, yhat)
    corr = float("nan")
    if len(y) > 2 and statistics.pstdev(yhat) > 0 and statistics.pstdev(y) > 0:
        corr = statistics.correlation(y, yhat)
    out = {"n": len(y), "r2": r2m, "r2_vs_zero": r20, "corr": corr, "pred_sd": statistics.pstdev(yhat) if yhat else 0.0,
           "y_sd": statistics.pstdev(y) if y else 0.0, "by_threshold": []}
    for thr in THRESHOLDS:
        sel = [(k, i) for k, i in enumerate(idx_test) if abs(yhat[k]) >= thr and (thr > 0 or yhat[k] != 0)]
        if not sel:
            out["by_threshold"].append({"thr": thr, "n": 0})
            continue
        wins = losses = flat = 0
        edge, fills, pnl_fill = [], 0, []
        for k, i in sel:
            r = rows[i]
            sgn = 1.0 if yhat[k] > 0 else -1.0
            e = sgn * r["y"][h]
            edge.append(e)
            if e > 0:
                wins += 1
            elif e < 0:
                losses += 1
            else:
                flat += 1
            touched = r["touch_dn"] if sgn > 0 else r["touch_up"]
            if touched:
                fills += 1
                pnl_fill.append(e + HALF_SPREAD)   # bought one tick below (sold one tick above) the close, exit at close[i+h]
        me = statistics.mean(edge)
        out["by_threshold"].append({
            "thr": thr, "n": len(sel), "share": len(sel) / len(y),
            "hit": wins / max(1, wins + losses), "wins": wins, "losses": losses, "flat": flat,
            "mean_edge": me,
            "taker": me - 2 * HALF_SPREAD,                 # cross the spread in and out
            "passive": me + HALF_SPREAD,                   # filled at mid -/+ half spread, exit at the later close
            "passive_fill_rate": fills / len(sel),
            "passive_if_touched": statistics.mean(pnl_fill) if pnl_fill else float("nan"),
        })
    return out


def train(series: dict[str, Series], universe, split: float = 0.7, lam: Optional[float] = None, verbose: bool = True) -> dict:
    others = race_legs(series, universe)
    rows = build_rows(series, others)
    if not rows:
        raise SystemExit("no training rows")
    cutoff = quantile([r["t"] for r in rows], split)
    idx_test = [i for i, r in enumerate(rows) if r["t"] >= cutoff]
    out = {"version": 1, "trained_at": iso(datetime.now(timezone.utc).timestamp()), "features": FEATURES,
           "cutoff": iso(cutoff), "n_rows": len(rows), "n_markets": len(series), "horizons": {}}
    if verbose:
        print(f"rows: {len(rows)} from {len(series)} markets, {iso(rows[0]['t'])} .. {iso(rows[-1]['t'])}; "
              f"test from {iso(cutoff)} ({len(idx_test)} rows)")
    for h in HORIZONS:
        # training rows: target fully realised before the cutoff (no leakage across the split)
        idx_train = [i for i, r in enumerate(rows) if r["t_end"][h] < cutoff]
        mean, std = scaling(rows, idx_train)
        model = Ridge(FEATURES, mean, std)
        Z = [model.zrow(r["x"]) for r in rows]
        k = len(FEATURES)
        # pick lambda on the last 30 % (in time) of the training window, then refit on all of it
        inner_cut = quantile([rows[i]["t"] for i in idx_train], 0.7)
        inner_tr = [i for i in idx_train if rows[i]["t_end"][h] < inner_cut]
        inner_va = [i for i in idx_train if rows[i]["t"] >= inner_cut]
        chosen = lam
        lam_table = []
        if chosen is None and inner_tr and inner_va:
            ZZ, Zy = Ridge.crossprod([Z[i] for i in inner_tr], [rows[i]["y"][h] for i in inner_tr])
            n_in = len(inner_tr)
            zm = [sum(Z[i][j] for i in inner_tr) / n_in for j in range(k)]
            ym = sum(rows[i]["y"][h] for i in inner_tr) / n_in
            best = None
            for L in LAMBDAS:
                m = Ridge(FEATURES, mean, std).fit_from(ZZ, Zy, n_in, ym, zm, L)
                rr = r2([rows[i]["y"][h] for i in inner_va], [m.predict_z(Z[i]) for i in inner_va])[0]
                lam_table.append((L, rr))
                if best is None or rr > best[1]:
                    best = (L, rr)
            chosen = best[0]
        chosen = chosen if chosen is not None else 1e-3
        ZZ, Zy = Ridge.crossprod([Z[i] for i in idx_train], [rows[i]["y"][h] for i in idx_train])
        n_tr = len(idx_train)
        zm = [sum(Z[i][j] for i in idx_train) / n_tr for j in range(k)]
        ym = sum(rows[i]["y"][h] for i in idx_train) / n_tr
        yy = sum(rows[i]["y"][h] ** 2 for i in idx_train)
        model.fit_from(ZZ, Zy, n_tr, ym, zm, chosen, yy=yy)
        ev = evaluate(rows, idx_test, Z, model, h)
        # drop-one ablation (same lambda, refit on the training cross products, scored out of sample)
        ablation = {}
        for j, name in enumerate(FEATURES):
            keep = [a for a in range(k) if a != j]
            m = Ridge(FEATURES, mean, std).fit_from(ZZ, Zy, n_tr, ym, zm, chosen, keep=keep)
            ablation[name] = ev["r2"] - r2([rows[i]["y"][h] for i in idx_test], [m.predict_z(Z[i]) for i in idx_test])[0]
        out["horizons"][str(h)] = {
            "n_train": n_tr, "n_test": len(idx_test), "lambda_grid": lam_table,
            "mean": mean, "std": std, **model.to_json(),
            "validation": ev, "ablation_delta_r2": ablation,
        }
        if verbose:
            print_report(h, out["horizons"][str(h)])
    return out


def print_report(h: int, m: dict):
    ev = m["validation"]
    print(f"\n=== horizon {h} bar(s): train n={m['n_train']}  test n={m['n_test']}  lambda={m['lambda']:g}"
          + (f"  (grid: {', '.join(f'{L:g}:{r:+.4f}' for L, r in m['lambda_grid'])})" if m["lambda_grid"] else ""))
    print(f"out-of-sample R2={ev['r2']:+.4f} (vs zero {ev['r2_vs_zero']:+.4f}) corr={ev['corr']:+.3f}  "
          f"pred sd={ev['pred_sd']*100:.3f}c  target sd={ev['y_sd']*100:.3f}c")
    print(f"{'|pred|>=':>9} {'n':>7} {'share':>6} {'hit':>6} {'W/L/flat':>18} {'edge':>8} {'taker':>8} {'passive':>8} {'fill%':>6} {'pass@fill':>9}")
    for t in ev["by_threshold"]:
        if not t.get("n"):
            print(f"{t['thr']*100:>8.2f}c {0:>7}")
            continue
        print(f"{t['thr']*100:>8.2f}c {t['n']:>7} {t['share']*100:>5.1f}% {t['hit']*100:>5.1f}% "
              f"{t['wins']:>6}/{t['losses']:>5}/{t['flat']:>5} {t['mean_edge']*100:>+7.3f}c {t['taker']*100:>+7.3f}c "
              f"{t['passive']*100:>+7.3f}c {t['passive_fill_rate']*100:>5.1f}% {t['passive_if_touched']*100:>+8.3f}c")
    print("  edge = mean(sign(pred) * realised change) per share; taker = edge - 0.005 (cross the 1-tick spread both ways);")
    print("  passive = edge + 0.0025 (fill at mid -/+ half spread, exit at the later close, fill assumed);")
    print("  fill% / pass@fill = share of next bars that traded through that passive level and the P&L on those only")
    order = sorted(range(len(FEATURES)), key=lambda j: -abs(m["tstats"][j]))
    print(f"{'feature':>12} {'coef/sd(c)':>11} {'t':>8} {'drop dR2':>9}   (coef per 1 sd of the feature, in cents)")
    for j in order:
        print(f"{FEATURES[j]:>12} {m['coef'][j]*100:>+11.4f} {m['tstats'][j]:>+8.1f} {m['ablation_delta_r2'][FEATURES[j]]:>+9.5f}")


# --------------------------------------------------------------------------- params / predict
def save(params: dict, path: str = PARAMS_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(params, f, indent=1)
    os.replace(tmp, path)
    return path


def load(path: str = PARAMS_PATH) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


_CACHE: dict = {}


def predict(features, horizon: int = 1, params: Optional[dict] = None) -> float:
    """Expected change in the YES price over the next `horizon` bars (1 or 6) given a feature dict from
    build_features. Returns 0.0 when no parameters are available."""
    if params is None:
        params = _CACHE.get("p") or load()
        _CACHE["p"] = params
    if not params or features is None:
        return 0.0
    m = params["horizons"].get(str(horizon))
    if not m:
        return 0.0
    names = params.get("features", FEATURES)
    v = [features[n] for n in names] if isinstance(features, dict) else list(features)
    z = [max(-ZCLIP, min(ZCLIP, (x - mu) / sd)) for x, mu, sd in zip(v, m["mean"], m["std"])]
    return m["intercept"] + sum(c * q for c, q in zip(m["coef"], z))


def predict_market(candles: Sequence[dict], others: Sequence[Sequence[dict]] = (), horizon: int = 1, params=None) -> float:
    return predict(build_features(candles, others), horizon, params)


# --------------------------------------------------------------------------- CLI
def report(series: dict[str, Series], universe, params: dict, top: int = 20):
    others = race_legs(series, universe)
    now = datetime.now(timezone.utc).timestamp()
    rows = []
    for ex, s in series.items():
        f = s.features(s.n - 1, others.get(ex, []))
        if f is None or not (MIN_PRICE <= s.close[-1] <= MAX_PRICE):
            continue
        p1, p6 = predict(f, 1, params), predict(f, 6, params)
        rp = universe.by_exchange.get(ex, ("?", "?"))
        rows.append((ex, f"{rp[1]} {rp[0]}", s.close[-1], s.t[-1], f, p1, p6))
    rows.sort(key=lambda r: -abs(r[6]))
    print(f"model trained {params.get('trained_at')} (cutoff {params.get('cutoff')}); {len(rows)} markets inside "
          f"[{MIN_PRICE}, {MAX_PRICE}]; sorted by |6-bar prediction|")
    print(f"{'exch':>5} {'market':<34} {'close':>6} {'last bar':>17} {'age':>6} {'pred1':>7} {'pred6':>7} {'d1':>6} {'d6':>6} {'comp':>6} {'ewm36':>6}")
    for ex, name, close, t, f, p1, p6 in rows[:top]:
        age = (now - t) / 60
        age_s = f"{age:.0f}m" if age < 180 else f"{age/60:.1f}h"
        print(f"{ex:>5} {name[:34]:<34} {close:>6.3f} {iso(t):>17} {age_s:>6} {p1*100:>+6.2f}c {p6*100:>+6.2f}c "
              f"{f['d1']*100:>+5.1f}c {f['d6']*100:>+5.1f}c {f['comp_dev']*100:>+5.1f}c {f['dev_ewma36']*100:>+5.1f}c")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--train", action="store_true", help="fit on the first 70%% of time, validate on the rest, save params")
    ap.add_argument("--report", action="store_true", help="list the markets with the largest current predicted move")
    ap.add_argument("--candles", default="data/candles_5m.json")
    ap.add_argument("--markets", default="data/markets.json")
    ap.add_argument("--params", default=PARAMS_PATH)
    ap.add_argument("--split", type=float, default=0.7)
    ap.add_argument("--lambda", dest="lam", type=float, default=None, help="ridge strength (relative to n); default: chosen on an inner validation window")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args(argv)
    if not (args.train or args.report):
        ap.error("pass --train and/or --report")
    path = lambda p: p if os.path.isabs(p) else os.path.join(HERE, p)
    series = load_candles(path(args.candles))
    universe = load_universe(path(args.markets))
    params = None
    if args.train:
        params = train(series, universe, split=args.split, lam=args.lam)
        if not args.no_save:
            print(f"\nsaved {save(params, path(args.params))}")
    if args.report:
        params = params or load(path(args.params))
        if not params:
            raise SystemExit(f"no params at {args.params}; run --train first")
        print()
        report(series, universe, params, top=args.top)


if __name__ == "__main__":
    main()
