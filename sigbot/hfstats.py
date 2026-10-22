"""High-frequency mean-reversion statistics on the tournament's own data (stdlib only).

    python3 -m sigbot.hfstats --fetch-1m            # pull 1-minute candles for the 60 highest-volume markets
                                                    # (one read each, 8 s apart; the live bot shares the account)
    python3 -m sigbot.hfstats                       # run every analysis, write research_notes/hf_mean_reversion.md
    python3 -m sigbot.hfstats --only ac,tod,bounce  # subset: ac, tod, bounce, event, sweep

Data: data/candles_1m.json, data/candles_5m.json, data/hist.json (1h), data/tapes.json, data/markets.json.
Candles exist only for intervals that traded (no zero-volume bars), so "one bar" is one traded interval; where it
matters the tables also give the contiguous-bar subset (next bar exactly one interval later).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
TICK = 0.005
BASE = "https://sig.thesuper.market/api/v1"
TOURNAMENT = "bda92870-621e-47b0-bc3c-3602c5c26f55"
ET = ZoneInfo("America/New_York")
BUCKETS = (("overnight 00-07", 0, 7), ("morning 07-12", 7, 12), ("afternoon 12-17", 12, 17), ("evening 17-24", 17, 24))


# ----------------------------------------------------------------------------------------------- data access
def api_key() -> str:
    k = os.environ.get("SIG_API_KEY")
    if not k:
        with open(os.path.join(HERE, ".sig_api_key")) as f:
            k = f.read().strip()
    return k


def curl_get(path: str, **params) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = f"{BASE}{path}?{qs}"
    out = subprocess.run(
        ["curl", "-sS", "--max-time", "30", "-H", f"Authorization: Bearer {api_key()}", "-H", "Accept: application/json", url],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def top_volume_exchanges(n: int = 60) -> list[str]:
    hist = json.load(open(os.path.join(DATA, "hist.json")))
    vol = {ex: sum(c.get("volume", 0) or 0 for c in h.get("candles", [])) for ex, h in hist.items()}
    return [ex for ex, _ in sorted(vol.items(), key=lambda kv: -kv[1])[:n]]


def fetch_1m(n: int = 60, sleep_s: float = 8.0, out_path: str | None = None) -> None:
    out_path = out_path or os.path.join(DATA, "candles_1m.json")
    done = json.load(open(out_path)) if os.path.exists(out_path) else {}
    todo = [ex for ex in top_volume_exchanges(n) if ex not in done]
    print(f"fetching 1m candles for {len(todo)} exchanges ({len(done)} already on disk), {sleep_s:.0f}s apart", flush=True)
    for i, ex in enumerate(todo):
        t0 = time.time()
        try:
            res = curl_get(f"/exchanges/{ex}/price-history", resolution="1m", limit=1000, tournamentId=TOURNAMENT)
            if "candles" not in res:
                print(f"  {ex}: unexpected response {str(res)[:200]}", flush=True)
            else:
                done[ex] = res
                print(f"  {i + 1}/{len(todo)} {ex}: {len(res['candles'])} bars {res['candles'][0]['time'] if res['candles'] else ''}", flush=True)
                with open(out_path, "w") as f:
                    json.dump(done, f)
        except Exception as e:  # noqa: BLE001
            print(f"  {ex}: error {e}", flush=True)
        if i + 1 < len(todo):
            time.sleep(max(0.0, sleep_s - (time.time() - t0)))
    print(f"saved {len(done)} series to {out_path}", flush=True)



# ----------------------------------------------------------------------------------------------- helpers
def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def et_bucket(ts: datetime) -> str:
    h = ts.astimezone(ET).hour
    for name, lo, hi in BUCKETS:
        if lo <= h < hi:
            return name
    return BUCKETS[-1][0]


def et_hour(ts: datetime) -> int:
    return ts.astimezone(ET).hour


def is_weekend(ts: datetime) -> bool:
    return ts.astimezone(ET).weekday() >= 5


def load_candles(name: str) -> dict[str, list[dict]]:
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return {}
    raw = json.load(open(path))
    out = {}
    for ex, h in raw.items():
        c = [x for x in h.get("candles", []) if x.get("close") is not None]
        c.sort(key=lambda x: x["time"])
        if len(c) >= 30:
            for x in c:
                x["_ts"] = parse_ts(x["time"])
            out[ex] = c
    return out


def calendar_grid(bars: list[dict], step_min: int, field: str = "close") -> tuple[list[datetime], list[float]]:
    """Forward-filled series on a calendar grid from the first to the last bar."""
    t0, t1 = bars[0]["_ts"], bars[-1]["_ts"]
    n = int((t1 - t0).total_seconds() // (60 * step_min)) + 1
    times, vals = [], []
    j, last = 0, bars[0][field]
    for i in range(n):
        t = t0 + i * __import__("datetime").timedelta(minutes=step_min)
        while j < len(bars) and bars[j]["_ts"] <= t:
            last = bars[j][field]
            j += 1
        times.append(t)
        vals.append(last)
    return times, vals


def fmt(x, d=3):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    return f"{x:.{d}f}"


class Pairs:
    """Pooled (x, y) regression sample made of per-market contiguous segments, for HAC inference."""

    def __init__(self):
        self.segments: list[tuple[list[float], list[float]]] = []

    def add(self, xs, ys):
        if len(xs) >= 3:
            self.segments.append((list(xs), list(ys)))

    @property
    def n(self):
        return sum(len(s[0]) for s in self.segments)

    def ols_hac(self, lags: int = 5):
        """Slope of y on x, Newey-West SE with Bartlett weights, autocovariances summed within segments."""
        n = self.n
        if n < 10:
            return dict(n=n, slope=float("nan"), se=float("nan"), t=float("nan"), rho=float("nan"))
        sx = sum(x for s in self.segments for x in s[0])
        sy = sum(y for s in self.segments for y in s[1])
        mx, my = sx / n, sy / n
        sxx = sum((x - mx) ** 2 for s in self.segments for x in s[0])
        syy = sum((y - my) ** 2 for s in self.segments for y in s[1])
        sxy = sum((x - mx) * (y - my) for s in self.segments for x, y in zip(s[0], s[1]))
        if sxx <= 0 or syy <= 0:
            return dict(n=n, slope=float("nan"), se=float("nan"), t=float("nan"), rho=float("nan"))
        b = sxy / sxx
        a = my - b * mx
        v = 0.0
        for xs, ys in self.segments:
            sc = [(x - mx) * (y - a - b * x) for x, y in zip(xs, ys)]
            v += sum(s * s for s in sc)
            for j in range(1, min(lags, len(sc) - 1) + 1):
                w = 1 - j / (lags + 1)
                v += 2 * w * sum(sc[i] * sc[i - j] for i in range(j, len(sc)))
        se = math.sqrt(max(v, 0.0)) / sxx
        return dict(n=n, slope=b, se=se, t=(b / se if se > 0 else float("nan")), rho=sxy / math.sqrt(sxx * syy))


def variance_ratio(segments: list[list[float]], q: int):
    """Lo-MacKinlay VR(q) on pooled one-period changes with heteroskedasticity-robust z (overlapping q-sums)."""
    n = sum(len(s) for s in segments)
    if n < 10 * q:
        return float("nan"), float("nan"), n
    mu = sum(x for s in segments for x in s) / n
    s2 = sum((x - mu) ** 2 for s in segments for x in s) / n
    if s2 <= 0:
        return float("nan"), float("nan"), n
    num, cnt = 0.0, 0
    for s in segments:
        for i in range(q, len(s) + 1):
            num += (sum(s[i - q:i]) - q * mu) ** 2
            cnt += 1
    if cnt < 100:
        return float("nan"), float("nan"), n
    vr = (num / cnt) / (q * s2)
    # heteroskedasticity-consistent theta: sum_j (2(q-j)/q)^2 * delta_j
    e2 = [[(x - mu) ** 2 for x in s] for s in segments]
    den = sum(sum(e) for e in e2) ** 2
    theta = 0.0
    for j in range(1, q):
        dj = sum(sum(e[i] * e[i - j] for i in range(j, len(e))) for e in e2) / den if den > 0 else 0.0
        theta += (2 * (q - j) / q) ** 2 * dj
    z = (vr - 1) / math.sqrt(theta) if theta > 0 else float("nan")
    return vr, z, n


# ----------------------------------------------------------------------------------------------- samples
HORIZONS = [  # (label, candle file, base resolution minutes, steps of base per horizon)
    ("1m", "candles_1m.json", 1, 1),
    ("5m", "candles_5m.json", 5, 1),
    ("15m", "candles_5m.json", 5, 3),
    ("30m", "candles_5m.json", 5, 6),
    ("1h", "hist.json", 60, 1),
    ("4h", "hist.json", 60, 4),
]


def horizon_samples(bars_by_ex, step_min, k, lo=0.05, hi=0.95, field="close", mode="calendar"):
    """Non-overlapping horizon changes per market.

    Returns a list of segments; each segment is a list of (t, d_prev, d_next, price_at_t). mode="calendar" uses a
    forward-filled grid (a horizon with no trades is a zero change); mode="bars" uses consecutive traded bars.
    """
    segs = []
    for ex, bars in bars_by_ex.items():
        if mode == "calendar":
            times, closes = calendar_grid(bars, step_min, field)
        else:
            times, closes = [b["_ts"] for b in bars], [b[field] for b in bars]
        pts = [(times[i], closes[i]) for i in range(0, len(times), k)]
        if len(pts) < 4:
            continue
        seg = []
        for i in range(1, len(pts) - 1):
            p0, p1, p2 = pts[i - 1][1], pts[i][1], pts[i + 1][1]
            if lo <= p1 <= hi:
                seg.append((pts[i][0], p1 - p0, p2 - p1, p1))
            elif seg:  # a filtered point breaks the segment so neighbours stay adjacent in time
                if len(seg) >= 3:
                    segs.append(seg)
                seg = []
        if len(seg) >= 3:
            segs.append(seg)
    return segs


def segs_to_pairs(segs, pred=None):
    pr = Pairs()
    for seg in segs:
        xs, ys = [], []
        for t, d0, d1, p in seg:
            if pred is None or pred(t):
                xs.append(d0)
                ys.append(d1)
        pr.add(xs, ys)
    return pr


WINSOR = 4 * TICK


def segs_to_pairs_w(segs, pred=None, cap=WINSOR):
    """Same as segs_to_pairs with both changes winsorized at +/- cap (4 ticks): robust to fat-finger prints."""
    pr = Pairs()
    w = lambda v: max(-cap, min(cap, v))
    for seg in segs:
        xs, ys = [], []
        for t, d0, d1, p in seg:
            if pred is None or pred(t):
                xs.append(w(d0))
                ys.append(w(d1))
        pr.add(xs, ys)
    return pr


def segs_to_changes(segs, pred=None):
    """One-period change series per segment (the d_next of every point, contiguous only when pred is None)."""
    out = []
    for seg in segs:
        cur = []
        for t, d0, d1, p in seg:
            if pred is None or pred(t):
                cur.append(d1)
            elif cur:
                out.append(cur)
                cur = []
        if cur:
            out.append(cur)
    return out


# ----------------------------------------------------------------------------------------------- 1. autocorr by horizon
def analysis_autocorr(cand):
    rows, key = [], {}
    md = ["## 1. Autocorrelation of price changes by horizon (pooled, mid in [0.05, 0.95])", "",
          "Two clocks. *Calendar*: forward-filled close on a fixed grid, non-overlapping changes (an interval without "
          "trades is a zero change). *Bar*: consecutive traded bars (the clock the bot's earlier -0.45 figure used). "
          "AR(1) is the OLS slope of the next change on the last change; t is Newey-West (Bartlett, 5 lags, "
          "autocovariances summed within market). The HAC t-statistics are small relative to the sample because the "
          "changes have a kurtosis in the thousands (a handful of fat-finger prints that reverse dominate the "
          "covariance), so the *wins* columns repeat the regression with changes winsorized at 4 ticks; that is the "
          "estimate that describes the typical bar. VR(k) is the Lo-MacKinlay variance ratio of k-horizon to "
          "1-horizon changes on the calendar clock with the heteroskedasticity-robust z; VR < 1 means reversion.", "",
          "| horizon | markets | n cal | AR(1) cal | t | AR(1) cal wins | t | n bar | AR(1) bar | t | AR(1) bar wins | t | VR(2) z | VR(4) z | VR(8) z | VR(16) z |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, fname, step, k in HORIZONS:
        bars = cand.get(fname) or {}
        if not bars:
            md.append(f"| {label} | - | (no {fname}) |")
            continue
        segc = horizon_samples(bars, step, k, mode="calendar")
        segb = horizon_samples(bars, step, k, mode="bars")
        rc = segs_to_pairs(segc).ols_hac()
        rb = segs_to_pairs(segb).ols_hac()
        rcw = segs_to_pairs_w(segc).ols_hac()
        rbw = segs_to_pairs_w(segb).ols_hac()
        ch = segs_to_changes(segc)
        vrs = [variance_ratio(ch, q) for q in (2, 4, 8, 16)]
        key[label] = dict(cal=rc, bar=rb, calw=rcw, barw=rbw, vr=vrs)
        md.append(f"| {label} | {len(bars)} | {rc['n']} | {fmt(rc['slope'])} | {fmt(rc['t'],1)} | {fmt(rcw['slope'])} | {fmt(rcw['t'],1)} | "
                  f"{rb['n']} | {fmt(rb['slope'])} | {fmt(rb['t'],1)} | {fmt(rbw['slope'])} | {fmt(rbw['t'],1)} | "
                  + " | ".join(f"{fmt(v,2)} ({fmt(z,1)})" for v, z, _ in vrs) + " |")
    md.append("")
    return "\n".join(md), key


# ----------------------------------------------------------------------------------------------- 2. time of day
def analysis_tod(cand):
    md = ["## 2. Reversion by US Eastern time of day and weekday/weekend", "",
          "Calendar clock, non-overlapping changes; the bucket is the ET time at which the lagged change ends (the "
          "moment a fade would be placed). AR(1) and t are on raw changes (HAC); *wins* winsorizes changes at 4 ticks. "
          "diff z compares the bucket's winsorized slope with the winsorized slope on all other observations "
          "(independent HAC standard errors); the Wald chi-square (3 df) tests equality of the winsorized slopes "
          "across the four buckets. VR(2) and VR(4) are on raw changes within the bucket (windows never cross a bucket "
          "boundary) with their z-statistics.", ""]
    key = {}
    for label, fname, step, k in HORIZONS:
        if label == "4h":
            continue
        bars = cand.get(fname) or {}
        if not bars:
            continue
        segc = horizon_samples(bars, step, k, mode="calendar")
        md += [f"### {label}", "", "| bucket | n | AR(1) | t | AR(1) wins | t | diff vs rest z | VR(2) z | VR(4) z |", "|---|---|---|---|---|---|---|---|---|"]
        ests = []
        for name, lo, hi in BUCKETS:
            inb = lambda t, lo=lo, hi=hi: lo <= et_hour(t) < hi
            r = segs_to_pairs(segc, inb).ols_hac()
            rw = segs_to_pairs_w(segc, inb).ols_hac()
            row = segs_to_pairs_w(segc, lambda t, f=inb: not f(t)).ols_hac()
            ch = segs_to_changes(segc, inb)
            v2, z2, _ = variance_ratio(ch, 2)
            v8, z8, _ = variance_ratio(ch, 4)
            dz = (rw["slope"] - row["slope"]) / math.sqrt(rw["se"] ** 2 + row["se"] ** 2) if rw["se"] > 0 and row["se"] > 0 else float("nan")
            ests.append(rw)
            key[(label, name)] = dict(raw=r, wins=rw, dz=dz, vr2=(v2, z2), vr8=(v8, z8))
            md.append(f"| {name} | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(rw['slope'])} | {fmt(rw['t'],1)} | {fmt(dz,1)} | {fmt(v2,2)} ({fmt(z2,1)}) | {fmt(v8,2)} ({fmt(z8,1)}) |")
        good = [e for e in ests if e["se"] > 0 and not math.isnan(e["slope"])]
        if len(good) >= 2:
            w = [1 / e["se"] ** 2 for e in good]
            bw = sum(wi * e["slope"] for wi, e in zip(w, good)) / sum(w)
            chi = sum(wi * (e["slope"] - bw) ** 2 for wi, e in zip(w, good))
            key[(label, "wald")] = chi
            md.append(f"| Wald equality (df={len(good)-1}) | | | | | | chi2={chi:.1f} | | |")
        for name, pred in (("weekday", lambda t: not is_weekend(t)), ("weekend", is_weekend)):
            r = segs_to_pairs(segc, pred).ols_hac()
            rw = segs_to_pairs_w(segc, pred).ols_hac()
            row = segs_to_pairs_w(segc, lambda t, f=pred: not f(t)).ols_hac()
            ch = segs_to_changes(segc, pred)
            v2, z2, _ = variance_ratio(ch, 2)
            v8, z8, _ = variance_ratio(ch, 4)
            dz = (rw["slope"] - row["slope"]) / math.sqrt(rw["se"] ** 2 + row["se"] ** 2) if rw["se"] > 0 and row["se"] > 0 else float("nan")
            key[(label, name)] = dict(raw=r, wins=rw, dz=dz, vr2=(v2, z2), vr8=(v8, z8))
            md.append(f"| {name} | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(rw['slope'])} | {fmt(rw['t'],1)} | {fmt(dz,1)} | {fmt(v2,2)} ({fmt(z2,1)}) | {fmt(v8,2)} ({fmt(z8,1)}) |")
        md.append("")
    return "\n".join(md), key


# ----------------------------------------------------------------------------------------------- 3. bounce
def load_tapes():
    raw = json.load(open(os.path.join(DATA, "tapes.json")))
    out = {}
    for ex, h in raw.items():
        tr = [dict(t=parse_ts(x["createdAt"]), p=float(x["price"]), q=float(x["size"])) for x in h.get("data", [])]
        tr.sort(key=lambda x: x["t"])
        if len(tr) >= 20:
            out[ex] = tr
    return out


def autocorr_pooled(series_list, lo=0.05, hi=0.95):
    """Pooled lag-1 autocorrelation of within-segment consecutive changes; returns (rho, n, se-based t)."""
    pr = Pairs()
    for s in series_list:
        xs, ys = [], []
        for i in range(1, len(s) - 1):
            if lo <= s[i][1] <= hi:
                xs.append(s[i][1] - s[i - 1][1])
                ys.append(s[i + 1][1] - s[i][1])
        pr.add(xs, ys)
    return pr.ols_hac()


def analysis_bounce(cand):
    tapes = load_tapes()
    md = ["## 3. Bid-ask bounce versus real reversion", "",
          "Roll (1984): a price that alternates between bid and ask with a fixed mid has cov(dp_t, dp_t-1) = -s^2/4. "
          "Trade-to-trade changes isolate that mechanics; 5-minute vwap-to-vwap changes average the bounce out "
          "(at the cost of a Working-type positive bias from averaging); collapsing consecutive bars at the same "
          "close removes the zero changes created by sparse trading (which *strengthens* the negative autocorrelation "
          "of a pure bounce process, so it is a discreteness adjustment, not a bounce removal).", "",
          "| series | n | AR(1) slope | t | rho | note |", "|---|---|---|---|---|---|"]
    key = {}
    # trade level
    tr_all = [[(x["t"], x["p"]) for x in v] for v in tapes.values()]
    r = autocorr_pooled(tr_all)
    key["trade"] = r
    md.append(f"| trade-to-trade, all trades | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(r['rho'])} | tapes.json, {len(tapes)} markets |")
    tr_nz = []
    for v in tapes.values():
        s = [(v[0]["t"], v[0]["p"])]
        for x in v[1:]:
            if x["p"] != s[-1][1]:
                s.append((x["t"], x["p"]))
        tr_nz.append(s)
    r = autocorr_pooled(tr_nz)
    key["trade_nz"] = r
    md.append(f"| trade-to-trade, price-changing trades only | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(r['rho'])} | zero changes dropped |")
    # Roll spread from trade changes
    cov_num, cov_n, var_num = 0.0, 0, 0.0
    for v in tapes.values():
        d = [v[i]["p"] - v[i - 1]["p"] for i in range(1, len(v)) if 0.05 <= v[i]["p"] <= 0.95]
        for i in range(1, len(d)):
            cov_num += d[i] * d[i - 1]
            cov_n += 1
        var_num += sum(x * x for x in d)
    cov1 = cov_num / max(1, cov_n)
    roll_s = 2 * math.sqrt(-cov1) if cov1 < 0 else float("nan")
    key["roll_s"] = roll_s
    key["trade_var"] = var_num / max(1, cov_n)
    md.append(f"| Roll implied spread from trades | {cov_n} | | | | s = 2 sqrt(-cov1) = {fmt(roll_s,4)} ({fmt(roll_s/TICK,2)} ticks); var(dp) = {key['trade_var']:.2e} |")
    # aggressor-conditioned (sqlite): ask-to-ask changes among buyer-initiated trades carry no bounce
    try:
        import sqlite3
        con = sqlite3.connect(os.path.join(DATA, "sigbot.sqlite"))
        rows = con.execute("select exchange_id, executed_at, price, aggressor from tape where aggressor in ('buy','sell') order by exchange_id, executed_at").fetchall()
        con.close()
        by = defaultdict(lambda: {"buy": [], "sell": []})
        for ex, ts, p, ag in rows:
            by[ex][ag].append((parse_ts(ts), float(p)))
        for side in ("buy", "sell"):
            r = autocorr_pooled([v[side] for v in by.values()])
            key[f"agg_{side}"] = r
            md.append(f"| same-aggressor trades only ({side}) | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(r['rho'])} | sigbot.sqlite, bot-classified aggressor; quote-to-quote, no bounce |")
    except Exception as e:  # noqa: BLE001
        md.append(f"| same-aggressor trades | - | | | | sqlite unavailable: {e} |")
    # 5-minute bars
    bars5 = cand.get("candles_5m.json") or {}
    for field, lab in (("close", "5m close-to-close (bar clock)"), ("vwap", "5m vwap-to-vwap (bar clock)")):
        r = segs_to_pairs(horizon_samples(bars5, 5, 1, field=field, mode="bars")).ols_hac()
        key[f"5m_{field}"] = r
        md.append(f"| {lab} | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(r['rho'])} | |")
    r = segs_to_pairs_w(horizon_samples(bars5, 5, 1, mode="bars")).ols_hac(); key["5m_close_w"] = r
    md.append(f"| 5m close-to-close, changes winsorized at 4 ticks | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(r['rho'])} | robust to fat-finger prints |")
    r = segs_to_pairs_w(horizon_samples(bars5, 5, 1, field="vwap", mode="bars")).ols_hac(); key["5m_vwap_w"] = r
    md.append(f"| 5m vwap-to-vwap, winsorized at 4 ticks | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(r['rho'])} | |")
    for field, lab in (("close", "5m close, contiguous bars only"),):
        pr = Pairs()
        for ex, bars in bars5.items():
            xs, ys = [], []
            for i in range(1, len(bars) - 1):
                if (bars[i]["_ts"] - bars[i - 1]["_ts"]).total_seconds() == 300 and (bars[i + 1]["_ts"] - bars[i]["_ts"]).total_seconds() == 300 and 0.05 <= bars[i]["close"] <= 0.95:
                    xs.append(bars[i]["close"] - bars[i - 1]["close"]); ys.append(bars[i + 1]["close"] - bars[i]["close"])
            pr.add(xs, ys)
        r = pr.ols_hac(); key["5m_contig"] = r
        md.append(f"| {lab} | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(r['rho'])} | both neighbours exactly 5 min away |")
    # collapsed same-close bars
    coll = []
    for ex, bars in bars5.items():
        s = [(bars[0]["_ts"], bars[0]["close"])]
        for b in bars[1:]:
            if b["close"] != s[-1][1]:
                s.append((b["_ts"], b["close"]))
        coll.append(s)
    r = autocorr_pooled(coll); key["5m_collapsed"] = r
    md.append(f"| 5m close, consecutive same-close bars collapsed | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(r['rho'])} | every change non-zero |")
    coll = []
    for ex, bars in bars5.items():
        s = [(bars[0]["_ts"], bars[0]["vwap"])]
        for b in bars[1:]:
            if abs(b["vwap"] - s[-1][1]) >= TICK / 2:
                s.append((b["_ts"], b["vwap"]))
        coll.append(s)
    r = autocorr_pooled(coll); key["5m_vwap_collapsed"] = r
    md.append(f"| 5m vwap, bars within half a tick of the previous vwap collapsed | {r['n']} | {fmt(r['slope'])} | {fmt(r['t'],1)} | {fmt(r['rho'])} | |")
    # Bounce share: Roll covariance over the 5m close-change variance
    var5 = 0.0; n5 = 0
    for ex, bars in bars5.items():
        for i in range(1, len(bars)):
            if 0.05 <= bars[i]["close"] <= 0.95:
                var5 += (bars[i]["close"] - bars[i - 1]["close"]) ** 2; n5 += 1
    var5 /= max(1, n5)
    bounce_rho = (-(roll_s ** 2) / 4) / var5 if var5 > 0 and not math.isnan(roll_s) else float("nan")
    tick_rho = (-(TICK ** 2) / 4) / var5 if var5 > 0 else float("nan")
    key["bounce_rho_roll"] = bounce_rho; key["bounce_rho_tick"] = tick_rho; key["var5"] = var5
    c = key["5m_close"]["rho"]; v = key["5m_vwap"]["rho"]
    md += ["",
           f"Variance of a 5-minute close change (mid in [0.05, 0.95]) is {var5:.2e} (sd {math.sqrt(var5)/TICK:.2f} ticks). "
           f"A pure bounce across a one-tick spread contributes an autocovariance of -s^2/4 = {-(TICK**2)/4:.2e}, i.e. "
           f"rho = {fmt(tick_rho)} of the 5-minute AR(1); with the Roll spread estimated from the tape ({fmt(roll_s/TICK,2)} ticks) it is {fmt(bounce_rho)}. ",
           f"The observed 5-minute close-to-close rho is {fmt(c)}; the vwap-to-vwap rho is {fmt(v)}. "
           f"Bounce therefore explains about {fmt(100*bounce_rho/c if c else float('nan'),0)}% of the close-to-close coefficient by the Roll "
           f"decomposition, and the close-minus-vwap gap ({fmt(c - v)}) says {fmt(100*(c-v)/c if c else float('nan'),0)}% of it disappears when the bounce is averaged out. "
           f"The remainder, rho of roughly {fmt(v)} on vwap (and the same-aggressor quote-to-quote estimates above), is reversion of the quote itself: "
           "the order flow is mean-reverting, not just the trade print.", ""]
    return "\n".join(md), key


# ----------------------------------------------------------------------------------------------- 4. event study
def analysis_events(cand, fname="candles_5m.json", label="5m", window=48, horizons=(1, 3, 6, 12)):
    bars_by_ex = cand.get(fname) or {}
    md = [f"## 4. Event study: large {label} moves (sigma from the trailing {window} bar changes, bar clock)", "",
          "R_k = cumulative change over the next k bars signed against the move (positive = reversion), in ticks. "
          "Passive fade P&L per share = R_k + half a tick (resting at the post-move close improved by one half tick, "
          "exit at the close k bars later); taker/passive = R_k - half a tick (cross the spread to enter, leave passively); "
          "taker fade P&L = R_k - one tick (crossing a 0.005 spread to enter, exiting at the close, i.e. paying the "
          "spread once in full). The post-move close is itself a print at the far quote, so about half the Roll spread "
          "(0.4 ticks) of R_k on close is bounce; the vwap tables are the cleaner measure. Events can overlap (one shock produces several bars), so the t-statistics are optimistic.", ""]
    key = {}
    for field in ("close", "vwap"):
        events = []  # (t, price, sigma_units, sign, outcomes{k: R_k})
        for ex, bars in bars_by_ex.items():
            px = [b[field] for b in bars]
            d = [px[i] - px[i - 1] for i in range(1, len(px))]
            for i in range(window + 1, len(px) - 1):
                if not (0.05 <= px[i] <= 0.95):
                    continue
                hist = d[i - 1 - window:i - 1]
                m = sum(hist) / len(hist)
                sig = math.sqrt(sum((x - m) ** 2 for x in hist) / (len(hist) - 1))
                mv = d[i - 1]
                if sig <= 0 or abs(mv) < TICK or abs(mv) < 2 * sig:
                    continue
                out = {k: -math.copysign(1, mv) * (px[i + k] - px[i]) for k in horizons if i + k < len(px)}
                events.append((bars[i]["_ts"], px[i], abs(mv) / sig, abs(mv), out))
        for thr in (2, 3):
            ev = [e for e in events if e[2] >= thr]
            md += [f"### {field}, |move| >= {thr} sigma ({len(ev)} events, mean move {fmt(sum(e[3] for e in ev)/max(1,len(ev))/TICK,2)} ticks)", "",
                   "| bucket | n | k | mean R_k (ticks) | t | median | hit rate | passive P&L (ticks) | taker in / passive out | taker P&L (ticks) |", "|---|---|---|---|---|---|---|---|---|---|"]
            groups = [("all", lambda t: True)] + [(name, (lambda t, lo=lo, hi=hi: lo <= et_hour(t) < hi)) for name, lo, hi in BUCKETS] \
                     + [("weekday", lambda t: not is_weekend(t)), ("weekend", is_weekend)]
            for gname, pred in groups:
                g = [e for e in ev if pred(e[0])]
                for k in horizons:
                    vals = [e[4][k] for e in g if k in e[4]]
                    n = len(vals)
                    if n < 5:
                        md.append(f"| {gname} | {n} | {k} | - | - | - | - | - | - | - |")
                        continue
                    m = sum(vals) / n
                    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
                    t = m / (sd / math.sqrt(n)) if sd > 0 else float("nan")
                    hit = sum(1 for v in vals if v > 0) / n
                    med = sorted(vals)[n // 2]
                    key[(field, thr, gname, k)] = dict(n=n, mean=m, t=t, hit=hit, median=med, passive=m + TICK / 2, half=m - TICK / 2, taker=m - TICK)
                    md.append(f"| {gname} | {n} | {k} | {fmt(m/TICK,2)} | {fmt(t,1)} | {fmt(med/TICK,2)} | {fmt(hit,2)} | {fmt((m+TICK/2)/TICK,2)} | {fmt((m-TICK/2)/TICK,2)} | {fmt((m-TICK)/TICK,2)} |")
            md.append("")
    return "\n".join(md), key


# ----------------------------------------------------------------------------------------------- 5. sweeps
def analysis_sweeps(cand, gap_s=60, min_ticks=2):
    tapes = load_tapes()
    bars5 = cand.get("candles_5m.json") or {}
    md = ["## 5. Sweeps on the tape", "",
          f"A sweep is a run of consecutive trades, each within {gap_s} s of the previous one, whose prices never move "
          f"against the run and which carries the price by at least {min_ticks} ticks from the first to the last trade. "
          "The post-sweep price at +5, +15 and +60 min is the last known price at or before that time, from the tape "
          "where it extends that far and otherwise from 5-minute candle closes (stamped at bar end). Reversion is "
          "signed against the sweep (positive = price came back), in ticks. The sweep's last print is at the quote the "
          "sweep was hitting and the later print is on average near the mid, so about half the Roll spread (~0.4 "
          "ticks, section 3) of the measured reversion is bounce; a passive fader who posts at the sweep's end price "
          "earns the measured reversion, a taker crossing the spread earns it minus one tick.", ""]
    from datetime import timedelta
    sweeps = []
    for ex, tr in tapes.items():
        ref = [(x["t"], x["p"]) for x in tr]
        ref += [(b["_ts"] + timedelta(minutes=5), b["close"]) for b in bars5.get(ex, [])]
        ref.sort()
        rt = [r[0] for r in ref]
        import bisect
        def price_at(t):
            i = bisect.bisect_right(rt, t) - 1
            return ref[i][1] if i >= 0 else None
        last_known = rt[-1]
        i = 0
        while i < len(tr) - 1:
            j, direction = i, 0
            while j + 1 < len(tr) and (tr[j + 1]["t"] - tr[j]["t"]).total_seconds() <= gap_s:
                dp = tr[j + 1]["p"] - tr[j]["p"]
                if dp == 0:
                    j += 1; continue
                s = 1 if dp > 0 else -1
                if direction == 0:
                    direction = s
                elif s != direction:
                    break
                j += 1
            move = tr[j]["p"] - tr[i]["p"]
            if direction != 0 and abs(move) >= min_ticks * TICK - 1e-9 and 0.05 <= tr[j]["p"] <= 0.95:
                t_end = tr[j]["t"]
                out = {}
                for h in (5, 15, 60):
                    th = t_end + timedelta(minutes=h)
                    if th <= last_known:
                        out[h] = -direction * (price_at(th) - tr[j]["p"])
                sweeps.append(dict(ex=ex, t=t_end, move=abs(move), n=j - i + 1, vol=sum(x["q"] for x in tr[i:j + 1]), out=out))
                i = j + 1
            else:
                i += 1
    key = {}
    md += [f"{len(sweeps)} sweeps in {len(tapes)} markets; mean size {fmt(sum(s['move'] for s in sweeps)/max(1,len(sweeps))/TICK,2)} ticks, "
           f"mean {fmt(sum(s['n'] for s in sweeps)/max(1,len(sweeps)),1)} trades, median shares {fmt(sorted(s['vol'] for s in sweeps)[len(sweeps)//2] if sweeps else float('nan'),0)}.", "",
           "| bucket | n | +5 min (ticks) | t | +15 min | t | +60 min | t | median +15 | hit +15 |", "|---|---|---|---|---|---|---|---|---|---|"]
    groups = [("all", lambda t: True)] + [(name, (lambda t, lo=lo, hi=hi: lo <= et_hour(t) < hi)) for name, lo, hi in BUCKETS] \
             + [("weekday", lambda t: not is_weekend(t)), ("weekend", is_weekend)]
    def stats(vals):
        n = len(vals)
        if n < 5:
            return n, float("nan"), float("nan"), float("nan"), float("nan")
        m = sum(vals) / n
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
        return n, m, (m / (sd / math.sqrt(n)) if sd > 0 else float("nan")), sum(1 for v in vals if v > 0) / n, sorted(vals)[n // 2]
    for gname, pred in groups:
        g = [s for s in sweeps if pred(s["t"])]
        cells = []
        hit15 = med15 = float("nan")
        for h in (5, 15, 60):
            n, m, t, hit, med = stats([s["out"][h] for s in g if h in s["out"]])
            key[(gname, h)] = dict(n=n, mean=m, t=t, hit=hit, median=med)
            cells.append(f"{fmt(m/TICK if not math.isnan(m) else m,2)} | {fmt(t,1)}")
            if h == 15:
                hit15, med15 = hit, med
        md.append(f"| {gname} | {len(g)} | " + " | ".join(cells) + f" | {fmt(med15/TICK if not math.isnan(med15) else med15,2)} | {fmt(hit15,2)} |")
    md += ["", "By ET hour (reversion at +15 min):", "", "| ET hour | n | mean sweep (ticks) | +15 min reversion (ticks) | t | hit |", "|---|---|---|---|---|---|"]
    for h in range(24):
        g = [s for s in sweeps if et_hour(s["t"]) == h and 15 in s["out"]]
        n, m, t, hit, _ = stats([s["out"][15] for s in g])
        md.append(f"| {h:02d} | {n} | {fmt(sum(s['move'] for s in g)/n/TICK if n else float('nan'),2)} | {fmt(m/TICK if not math.isnan(m) else m,2)} | {fmt(t,1)} | {fmt(hit,2)} |")
    md.append("")
    return "\n".join(md), key


# ----------------------------------------------------------------------------------------------- 6. opportunities
def analysis_opportunities(k1, k2, k3, k4, k5, cand, k4m=None):
    md = ["## 6. Opportunities", ""]
    T = lambda v: v / TICK
    md += ["**Horizon.** Reversion is present at every horizon from one minute to four hours and is strongest in the "
           "bar clock at 1-5 minutes (winsorized AR(1): " + ", ".join(f"{l} {fmt(k1[l]['barw']['slope'])}" for l in k1) +
           "); on the calendar clock the slope is about -0.2 from 1 minute to 30 minutes and -0.17 at 1 hour, with "
           "variance ratios falling monotonically with k (VR(16) of " + fmt(k1['5m']['vr'][3][0], 2) + " at 5 minutes, " +
           fmt(k1['1h']['vr'][3][0], 2) + " at 1 hour), so the reversion is not a one-bar artefact: a good part of any "
           "5-minute move is given back over the next hour or two.", ""]
    md += ["**Fading large 5-minute moves (section 4), what survives the spread:**", ""]
    md.append("| sample | n | R_3 close (ticks) | passive | taker in / passive out | taker round trip | R_3 vwap | verdict |")
    md.append("|---|---|---|---|---|---|---|---|")
    for thr in (2, 3):
        for gname in ["all"] + [b[0] for b in BUCKETS] + ["weekday", "weekend"]:
            r = k4.get(("close", thr, gname, 3)); rv = k4.get(("vwap", thr, gname, 3))
            if not r:
                continue
            if r["taker"] > 0 and r["t"] > 2:
                verdict = "taker round trip positive"
            elif r["half"] > 0 and r["t"] > 2:
                verdict = "pays only if one side is passive"
            elif r["passive"] > 0:
                verdict = "passive only"
            else:
                verdict = "nothing"
            md.append(f"| {thr} sigma, {gname} | {r['n']} | {fmt(T(r['mean']),2)} (t {fmt(r['t'],1)}) | {fmt(T(r['passive']),2)} | "
                      f"{fmt(T(r['half']),2)} | {fmt(T(r['taker']),2)} | {fmt(T(rv['mean']),2) if rv else '-'} | {verdict} |")
    md += ["", "Fading a 2-3 sigma 5-minute move as a round-trip taker loses in every bucket (about -0.1 to -0.3 ticks per "
           "share); crossing once and leaving passively nets +0.3 to +0.7 ticks, and that is the bot's earlier \"+0.33 cents\" "
           "number (it charged the spread once). The clean gain is passive: a resting quote filled by the move earns "
           "1.2-1.7 ticks per share gross on these events, of which roughly 0.4 ticks is the bounce of the print back to "
           "the mid and 0.6-1.0 ticks is reversion of the quote itself (vwap column). Fill probability is not modelled: "
           "a resting order is filled only when the move continues through it, so this is an upper bound on the ladder's "
           "edge per fill, not a strategy return.", ""]
    walds = {k[0]: v for k, v in k2.items() if k[1] == "wald"}
    sig = [l for l, v in walds.items() if v > 7.81]
    insig = [l for l, v in walds.items() if v <= 7.81]
    wald_txt = ("winsorized Wald chi-square on 3 df: " + ", ".join(f"{l} {walds[l]:.1f}" for l in walds) +
                "; 5% critical value 7.8")
    if sig:
        wald_txt += ("; only " + ", ".join(sig) + " crosses it, and there the spread between buckets is small (" +
                     ", ".join(f"{b[0].split()[0]} {fmt(k2[(sig[0], b[0])]['wins']['slope'])}" for b in BUCKETS) + ")")
    md += ["**Hours.** The AR(1) slopes barely differ between the four ET buckets (" + wald_txt + "; see section 2), "
           "and the event-study reversion is 0.7-1.0 ticks in every bucket. The differences that do show up are in the "
           "sweeps (section 5):", ""]
    for gname in ["all"] + [b[0] for b in BUCKETS] + ["weekday", "weekend"]:
        r = k5.get((gname, 15))
        if not r or math.isnan(r["mean"]):
            continue
        md.append(f"- {gname}: n {r['n']}, +15 min reversion {fmt(T(r['mean']),2)} ticks (t {fmt(r['t'],1)}, median {fmt(T(r['median']),1)}, hit {fmt(r['hit'],2)}); "
                  f"passive at the sweep's end price {fmt(T(r['mean']),2)}, taker {fmt(T(r['mean'])-1,2)}.")
    md += ["", "Overnight (00-07 ET) sweeps revert least (median zero, hit rate below one half, weakest at 03-05 ET); evening "
           "(17-24 ET) and the 19-21 ET hours revert most, 1.4-2 ticks, but on a few dozen sweeps. Even the evening figure "
           "only just survives a round-trip taker fade (about +0.4 ticks before any adverse selection), so sweeps are a "
           "signal for where to rest quotes and when to pull the adding side, not a taker trade. Weekend events revert "
           "somewhat more than weekday ones (3-sigma 5-minute moves: " + fmt(T(k4[('close',3,'weekend',3)]['mean']),2) + " vs " +
           fmt(T(k4[('close',3,'weekday',3)]['mean']),2) + " ticks at k = 3), consistent with thinner books and the DMM ladder being "
           "the only liquidity.", ""]
    md += ["**Does not survive:** any taker fade of a single-bar move at any hour at 2 sigma; taker fades of 3-sigma moves "
           "on weekdays; taker fades of overnight sweeps. **Survives:** passive fills on the far side of 2-3 sigma moves "
           "and sweeps at any hour (1.2-1.5 ticks gross per share before fill probability), cross-once-leave-passive on "
           "3-sigma moves (+0.4 ticks), and evening sweeps if the fill is near the sweep's end price.", ""]
    md += ["**Caveats.**", "",
           "- Seven to twenty days of 5-minute candles per market and 1,000 one-minute bars (one to twenty days, depending on activity) on the 60 most traded markets; every estimate is in-sample on one campaign stretch, and the election-week regime will differ.",
           "- Event windows overlap (one shock creates several qualifying bars and several sweeps), so the event-study and sweep t-statistics overstate independence; the HAC regressions handle it, the event tables do not.",
           "- Changes have a kurtosis in the thousands: a handful of fat-finger prints that reverse (5-minute changes up to 0.8) contribute almost half of the raw covariance, which is why raw HAC t-statistics are around 5-7 despite 200k-1M observations; the winsorized columns describe the typical bar.",
           "- Candles exist only for intervals that traded; the bar clock stretches overnight (a 5-minute bar can span an hour), and the overnight bucket has fewer traded bars and sweeps, so its estimates are noisier and partly measure the morning's first trades.",
           "- The post-move close and the sweep's last print sit at the far quote; about 0.4 ticks (half the Roll spread) of the measured reversion on close is bounce, and the collapse of same-price bars strengthens rather than removes that effect.",
           "- Passive P&L assumes a fill at the post-move price and an exit at a later print; it ignores queue position, the chance of being filled only when the move continues (adverse selection), and the 15-minute order expiry.",
           ""]
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-1m", action="store_true")
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--sleep", type=float, default=8.0)
    ap.add_argument("--only", default="ac,tod,bounce,event,sweep")
    ap.add_argument("--out", default=os.path.join(HERE, "research_notes", "hf_mean_reversion.md"))
    args = ap.parse_args()
    if args.fetch_1m:
        fetch_1m(args.top, args.sleep)
        return
    only = set(args.only.split(","))
    cand = {f: load_candles(f) for f in ("candles_1m.json", "candles_5m.json", "hist.json")}
    sections, keys = [], {}
    spans = []
    for f, bars in cand.items():
        if bars:
            t0 = min(b[0]["_ts"] for b in bars.values()); t1 = max(b[-1]["_ts"] for b in bars.values())
            med = sorted(len(b) for b in bars.values())[len(bars) // 2]
            spans.append(f"{f}: {len(bars)} markets, median {med} bars, {t0:%Y-%m-%d %H:%M} to {t1:%Y-%m-%d %H:%M} UTC")
    header = ["# High-frequency mean reversion on the Predictions Cup tape", "",
              f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `python3 -m sigbot.hfstats`. Prices are YES prices; one tick = 0.005. "
              "Candles exist only for intervals that traded, so bar clocks are event clocks; calendar clocks forward-fill.", "",
              "Data: " + "; ".join(spans) + ".", ""]
    t = time.time()
    if "ac" in only:
        s, keys["ac"] = analysis_autocorr(cand); sections.append(s); print(f"ac done {time.time()-t:.0f}s", flush=True)
    if "tod" in only:
        s, keys["tod"] = analysis_tod(cand); sections.append(s); print(f"tod done {time.time()-t:.0f}s", flush=True)
    if "bounce" in only:
        s, keys["bounce"] = analysis_bounce(cand); sections.append(s); print(f"bounce done {time.time()-t:.0f}s", flush=True)
    if "event" in only:
        s, keys["event"] = analysis_events(cand); sections.append(s)
        if cand.get("candles_1m.json"):
            s1, keys["event1m"] = analysis_events(cand, "candles_1m.json", "1m", horizons=(1, 3, 6, 12)); sections.append(s1.replace("## 4.", "## 4b."))
        print(f"event done {time.time()-t:.0f}s", flush=True)
    if "sweep" in only:
        s, keys["sweep"] = analysis_sweeps(cand); sections.append(s); print(f"sweep done {time.time()-t:.0f}s", flush=True)
    if only >= {"ac", "tod", "bounce", "event", "sweep"}:
        sections.append(analysis_opportunities(keys["ac"], keys["tod"], keys["bounce"], keys["event"], keys["sweep"], cand))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write("\n".join(header + sections))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
