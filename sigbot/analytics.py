"""Turn logged fills, tape and quote snapshots into market-making parameters.

Writes params/mm_overrides.json:
{
  "generated_at": ..., "lookback_hours": ...,
  "exchanges": {
     "<exchange_id>": {
        "pressure": -1..1,            # + = buyers lifting offers (net buying), - = sellers hitting bids
        "n_fills": int, "n_tape": int,
        "markout": {"5m": {...}, "30m": {...}},   # per side:level, mean post-fill mid move in our favour (ticks)
        "fill_rate": {level: fills per quoted hour},
        "level_size_mult": [..],      # recommended ladder multipliers (kept increasing with depth)
        "spread_add_ticks": int,      # widen when the touch is toxic
        "size_scale": float           # overall shrink/grow for this market
     }, ...
  },
  "global": {"markout_by_level": ...}
}

Rules (shrunk toward priors when data is sparse, weight n/(n+n0)):
  * pressure  = EWMA of signed aggressor volume on the tape (half-life `pressure_half_life_h`) blended with the
                sign of our own fills (our bid filled => seller aggression).
  * markout   = for each of our fills: (mid after h) - fill price, signed so + means we kept edge, in ticks.
                A level whose 30m markout is negative gets its multiplier cut; clearly positive, raised.
  * fill rate = fills per hour while quoted at that level (diagnostic; used to pull in dead levels by hand).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from .config import TICK, BotConfig
from .store import Store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def mid_at(store: Store, ex: str, ts: float):
    r = store.one("SELECT mid FROM quotes WHERE exchange_id=? AND ts>=? ORDER BY ts ASC LIMIT 1", (ex, ts))
    return r["mid"] if r else None


def compute(store: Store, cfg: BotConfig, lookback_h: float = 48.0, pressure_half_life_h: float = 6.0, n0: float = 20.0) -> dict:
    now = time.time()
    since = now - lookback_h * 3600
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "lookback_hours": lookback_h, "exchanges": {}, "global": {}}
    levels = cfg.mm.levels
    base_mult = list(cfg.mm.level_size_mult)[:levels]

    # pressure from the public tape
    tape = store.rows("SELECT exchange_id, price, quantity, executed_at, aggressor FROM tape WHERE ingested_at>=?", (since,))
    buy = defaultdict(float); sell = defaultdict(float); ntape = defaultdict(int)
    for t in tape:
        if t["aggressor"] not in ("buy", "sell"):
            continue
        age_h = (now - _ts(t["executed_at"])) / 3600
        w = 0.5 ** (age_h / pressure_half_life_h) * t["quantity"]
        (buy if t["aggressor"] == "buy" else sell)[t["exchange_id"]] += w
        ntape[t["exchange_id"]] += 1

    # our own fills: pressure sign and markouts
    fills = store.rows("SELECT * FROM fills WHERE purpose='mm' AND ingested_at>=?", (since,))
    fbuy = defaultdict(float); fsell = defaultdict(float)
    mark = defaultdict(lambda: defaultdict(list))   # ex -> (h, side, level) -> ticks
    gmark = defaultdict(list)                        # (h, level) -> ticks
    nfills = defaultdict(int)
    for f in fills:
        ex = f["exchange_id"]; nfills[ex] += 1
        ts = _ts(f["filled_at"])
        w = 0.5 ** (((now - ts) / 3600) / pressure_half_life_h) * abs(f["quantity"])
        if f["side"] == "yes":   # our YES bid was hit => seller aggression
            fsell[ex] += w
        else:                    # our NO buy (YES ask) was lifted => buyer aggression
            fbuy[ex] += w
        for h, secs in (("5m", 300), ("30m", 1800)):
            m = mid_at(store, ex, ts + secs)
            if m is None or f["price"] is None:
                continue
            edge = (m - f["price"]) if f["side"] == "yes" else (f["price"] - m)
            mark[ex][(h, f["side"], int(f["level"] or 0))].append(edge / TICK)
            gmark[(h, int(f["level"] or 0))].append(edge / TICK)

    # quoted hours per level (for fill rates)
    orders = store.rows("SELECT exchange_id, level, placed_at, expires_at FROM orders WHERE purpose='mm' AND placed_at>=?", (since,))
    quoted_h = defaultdict(lambda: defaultdict(float))
    for o in orders:
        if o["level"] is None:
            continue
        end = min(now, _ts(o["expires_at"])) if o["expires_at"] else now
        quoted_h[o["exchange_id"]][int(o["level"])] += max(0.0, end - o["placed_at"]) / 3600 / 2
    fills_by_level = defaultdict(lambda: defaultdict(int))
    for f in fills:
        if f["level"] is not None:
            fills_by_level[f["exchange_id"]][int(f["level"])] += 1

    for ex in set(buy) | set(sell) | set(nfills) | set(quoted_h):
        tb, ts_ = buy.get(ex, 0.0), sell.get(ex, 0.0)
        p_tape = (tb - ts_) / (tb + ts_) if (tb + ts_) > 0 else 0.0
        fb, fs = fbuy.get(ex, 0.0), fsell.get(ex, 0.0)
        p_fill = (fb - fs) / (fb + fs) if (fb + fs) > 0 else 0.0
        n_t, n_f = ntape.get(ex, 0), nfills.get(ex, 0)
        pressure = max(-1.0, min(1.0, 0.6 * (n_t / (n_t + n0)) * p_tape + 0.4 * (n_f / (n_f + n0)) * p_fill))

        rec_mult = list(base_mult); spread_add = 0; size_scale = 1.0
        mk: dict = {}
        for (h, side, lvl), vals in mark[ex].items():
            mk.setdefault(h, {})[f"{side}:{lvl}"] = {"n": len(vals), "mean_ticks": round(sum(vals) / len(vals), 3)}
        # regime from the touch's 30m markout: positive -> touch-heavy (calm), negative -> pulled off the touch (toxic)
        touch = [v for (h, s, l), vs in mark[ex].items() if h == "30m" and l == 1 for v in vs]
        shape = "default"
        if touch:
            n = len(touch); mean = sum(touch) / n; w = n / (n + n0)
            if w > 0.3 and mean > 0.3:
                rec_mult, shape = list(cfg.mm.calm_level_size_mult), "calm"
            elif w > 0.3 and mean < -0.3:
                rec_mult, shape = list(cfg.mm.toxic_level_size_mult), "toxic"
                if mean < -1.0:
                    spread_add = 1
        # per-level fine-tuning from each level's own 30m markout, shrunk by sample size
        for lvl in range(1, levels + 1):
            vals = [v for (h, s, l), vs in mark[ex].items() if h == "30m" and l == lvl for v in vs]
            if not vals:
                continue
            n = len(vals); mean = sum(vals) / n; w = n / (n + n0)
            rec_mult[lvl - 1] = rec_mult[lvl - 1] * (1.0 + w * max(-0.6, min(0.6, mean / 2.0)))
        scale = base_mult[0] / rec_mult[0] if rec_mult[0] > 0 else 1.0
        rec_mult = [round(max(0.05, m * scale), 3) for m in rec_mult]
        all30 = [v for (h, s, l), vs in mark[ex].items() if h == "30m" for v in vs]
        if all30:
            n = len(all30); mean = sum(all30) / n; w = n / (n + n0)
            size_scale = round(max(0.3, min(1.5, 1.0 + w * mean / 3.0)), 3)
        fr = {str(l): round(fills_by_level[ex][l] / quoted_h[ex][l], 3) for l in quoted_h[ex] if quoted_h[ex][l] > 0}
        out["exchanges"][ex] = {"pressure": round(pressure, 3), "n_fills": n_f, "n_tape": n_t, "p_tape": round(p_tape, 3),
                                "p_fill": round(p_fill, 3), "markout": mk, "fill_rate": fr, "level_size_mult": rec_mult, "shape": shape,
                                "spread_add_ticks": spread_add, "size_scale": size_scale}
    out["global"]["markout_by_level"] = {f"{h}:{l}": {"n": len(v), "mean_ticks": round(sum(v) / len(v), 3)} for (h, l), v in gmark.items()}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--lookback-hours", type=float, default=48.0)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    cfg = BotConfig.load(args.config)
    store = Store(os.path.join(HERE, cfg.db_path))
    out = compute(store, cfg, args.lookback_hours)
    path = os.path.join(HERE, cfg.mm.overrides_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(out, f, indent=1)
    os.replace(path + ".tmp", path)
    store.set_param("mm_overrides", out)
    print(f"wrote {path}: {len(out['exchanges'])} exchanges")
    if args.print:
        print(json.dumps(out, indent=1)[:6000])


if __name__ == "__main__":
    main()
