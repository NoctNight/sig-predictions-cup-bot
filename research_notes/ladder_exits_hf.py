"""Ladder exits for the high-frequency arb (deviation churn): part at +1 tick, part at +2, the rest at 24 h, vs the live
rule (all at +2 ticks per set, 6-tick stop, 24 h). Read-only; real SIG touch from venue_quotes, as in ladder_exits_study.

Entry (live rule, simplified to one 2,000-set lot per race at a time): 2-leg race (not RI Governor), mid overround
>= 4 ticks above its trailing 24-h median (>= 12 h history), sum of bids - 1 >= 1 tick, 30 min after the race's last exit.
Entry cost per set 2 - sum bids, exit proceeds 2 - sum asks, 0.1 tick slippage per leg each way. Round trip
R = sb0 - sa1 - slippage (ticks). Exits: a tier sells its share once R >= its target; any lot at R <= -6 is sold in full;
whatever is left goes at 24 h (or is marked at the last quote if the data ends first).
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ladder_exits_study import (OOS0, IS0, OUT, STEP, T, build, load, trailing_median, tstat)  # noqa: E402

SLIP = 0.1 * 2                 # ticks per set, each way (2 legs)
LOT = 2000
POLICIES = {
    "live: all at +2": [(1.0, 2)],
    "all at +1": [(1.0, 1)],
    "all at +3": [(1.0, 3)],
    "half +1, half +2": [(0.5, 1), (0.5, 2)],
    "third +1, two thirds +2": [(1 / 3, 1), (2 / 3, 2)],
    "half +1, half +3": [(0.5, 1), (0.5, 3)],
    "half +2, half +3": [(0.5, 2), (0.5, 3)],
    "thirds +1 / +2 / +3": [(1 / 3, 1), (1 / 3, 2), (1 / 3, 3)],
}


def main():
    vq, arb, _ = load()
    grid, races, leg, cost = build(vq)
    H24, GAP = int(24 * 3600 / STEP), int(1800 / STEP)
    out = {}
    lots = {name: [] for name in POLICIES}
    for r, ex in races.items():
        if len(ex) != 2 or r == "Rhode Island Governor":
            continue
        b = sum(leg[e][0] for e in ex)
        a = sum(leg[e][1] for e in ex)
        ovr = ((a + b) / 2 - 1) / T
        med = trailing_median(grid, ovr)
        sig = np.isfinite(ovr) & np.isfinite(med) & (ovr - med >= 4) & ((b - 1) / T >= 1) & (grid >= IS0)
        for name, tiers in POLICIES.items():
            i = 0
            while i < len(grid):
                if not sig[i]:
                    i += 1
                    continue
                sb0 = b[i] / T
                end = min(i + H24, len(grid) - 1)
                R = sb0 - a[i + 1:end + 1] / T - SLIP                     # round trip if sold at each later point
                ok = np.isfinite(R)
                left = [[f, tg] for f, tg in tiers]
                pnl, held, last_exit = 0.0, 0.0, i
                stop = np.flatnonzero(ok & (R <= -6))
                stop_j = stop[0] if len(stop) else None
                for f_tg in left:
                    hit = np.flatnonzero(ok & (R >= f_tg[1]))
                    j = hit[0] if len(hit) else None
                    if j is not None and (stop_j is None or j < stop_j):
                        pnl += f_tg[0] * R[j]
                        held += f_tg[0] * (j + 1)
                        last_exit = max(last_exit, i + 1 + j)
                        f_tg[0] = 0.0
                rest = sum(f for f, _ in left)
                if rest > 1e-9:
                    if stop_j is not None:
                        j = stop_j
                    else:
                        fin = np.flatnonzero(ok)
                        j = fin[-1] if len(fin) else 0
                    pnl += rest * (R[j] if len(R) and np.isfinite(R[j]) else 0.0)
                    held += rest * (j + 1)
                    last_exit = max(last_exit, i + 1 + j)
                cap = (2 - b[i]) * LOT
                lots[name].append({"race": r, "t": float(grid[i]), "oos": bool(grid[i] >= OOS0), "pnl_ticks": float(pnl),
                                   "hold_h": float(held * STEP / 3600), "cap": float(cap),
                                   "truncated": bool(i + H24 >= len(grid))})
                i = last_exit + GAP
    for name, ls in lots.items():
        for half in ("IS", "OOS"):
            x = [l for l in ls if l["oos"] == (half == "OOS")]
            p = np.array([l["pnl_ticks"] for l in x])
            hold = np.array([l["hold_h"] for l in x])
            cap = np.array([l["cap"] for l in x])
            per1k_day = (p * T * LOT).sum() / max(1e-9, (cap * hold / 24).sum()) * 1000
            out[f"{name}|{half}"] = {"n": len(x), "ticks_per_set": float(p.mean()) if len(p) else np.nan, "t": float(tstat(p)),
                                     "p5": float(np.percentile(p, 5)) if len(p) else np.nan, "hold_h": float(hold.mean()) if len(p) else np.nan,
                                     "per_1k_per_day": float(per1k_day), "truncated": int(sum(l["truncated"] for l in x))}
    # paired comparison vs live, per lot (same entries only when the live rule enters at the same time)
    live = {(l["race"], l["t"]): l for l in lots["live: all at +2"]}
    for name, ls in lots.items():
        for half in ("IS", "OOS"):
            d = [l["pnl_ticks"] - live[(l["race"], l["t"])]["pnl_ticks"] for l in ls
                 if (l["race"], l["t"]) in live and l["oos"] == (half == "OOS")]
            out[f"{name}|{half}"]["paired_vs_live"] = float(np.mean(d)) if d else np.nan
            out[f"{name}|{half}"]["paired_t"] = float(tstat(d))
            out[f"{name}|{half}"]["paired_n"] = len(d)
    for k, v in out.items():
        print(f"{k:38s}", {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()})
    json.dump(out, open(os.path.join(OUT, "hf.json"), "w"))


if __name__ == "__main__":
    main()
