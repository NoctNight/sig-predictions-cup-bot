"""Volume report: our pace against the volume target, from the fill log and two leaderboard reads.

    python3 -m sigbot.volume_report            # 2 API reads (first leaderboard page, then our row)
    python3 -m sigbot.volume_report --no-api   # fills table only, no reads
    python3 -m sigbot.volume_report --json     # machine-readable

Volume follows the leaderboard definition ("bid+ask notional volume": every fill counts quantity x price,
whichever side we were on). The fill log stores YES-denominated prices; `fill_volume_mode` in
params/volume_target.json picks how a NO fill is valued ("yes_price": quantity x YES price,
"side_cost": quantity x (1 - YES price)); the mode was calibrated against the leaderboard on 2026-10-22.
The target itself lives in params/volume_target.json (written by research_notes/volume_target.md's model).
Stdlib only. Opens the SQLite log read-only and never writes anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

from .api import ApiError, SuperMarketClient
from .config import BotConfig

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_PATH = os.path.join(HERE, "params", "volume_target.json")
TOURNAMENT_START = datetime(2026, 10, 1, tzinfo=timezone.utc)
TOURNAMENT_END = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)  # 12:00 ET on 4 Nov (EST)
SLEEVES = ("mm", "arb", "tilt", "direction", "exit", "hedge", "news", None)


def load_target() -> dict:
    if os.path.exists(TARGET_PATH):
        with open(TARGET_PATH) as f:
            return json.load(f)
    return {}


def volume_expr(mode: str) -> str:
    if mode == "side_cost":
        return "abs(quantity) * (case when side = 'no' then 1 - price else price end)"
    return "abs(quantity) * price"


def fills_summary(db_path: str, mode: str, now: float) -> dict:
    """Volume today (UTC), over the last 24 h, and since the log started, by sleeve."""
    uri = "file:%s?mode=ro" % os.path.abspath(db_path)
    conn = sqlite3.connect(uri, uri=True)
    vexpr = volume_expr(mode)
    out = {"by_sleeve": {}, "mode": mode}
    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT00:00:00")
    day_ago = datetime.fromtimestamp(now - 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    first = conn.execute("select min(filled_at) from fills").fetchone()[0]
    out["log_start"] = first
    log_hours = 0.0
    if first:
        t0 = datetime.fromisoformat(first[:19]).replace(tzinfo=timezone.utc).timestamp()
        log_hours = max(0.01, (now - t0) / 3600)
    out["log_hours"] = round(log_hours, 1)
    window_hours = min(24.0, log_hours)
    rows = conn.execute(
        f"""select coalesce(purpose, 'unknown') p,
                   sum(case when filled_at >= ? then {vexpr} else 0 end) today,
                   sum(case when filled_at >= ? then {vexpr} else 0 end) last24,
                   sum({vexpr}) total,
                   sum(case when filled_at >= ? then 1 else 0 end) n_today,
                   sum(case when filled_at >= ? then abs(quantity) else 0 end) sh_today
            from fills group by p""",
        (today, day_ago, today, today),
    ).fetchall()
    tot_today = tot_24 = 0.0
    for p, vt, v24, vtot, n_today, sh_today in rows:
        pace = (v24 or 0.0) * 24.0 / window_hours if window_hours else 0.0
        out["by_sleeve"][p] = {
            "today": round(vt or 0.0),
            "last24h": round(v24 or 0.0),
            "pace_per_day": round(pace),
            "fills_today": int(n_today or 0),
            "shares_today": round(sh_today or 0.0),
            "since_log_start": round(vtot or 0.0),
        }
        tot_today += vt or 0.0
        tot_24 += v24 or 0.0
    out["today"] = round(tot_today)
    out["last24h"] = round(tot_24)
    out["pace_per_day"] = round(tot_24 * 24.0 / window_hours) if window_hours else 0
    out["window_hours"] = round(window_hours, 1)
    # market-making fill rate per quoted market-hour (the capacity number used in the target note)
    try:
        hrs = conn.execute(
            "select count(distinct exchange_id || strftime('%Y%m%d%H', placed_at, 'unixepoch')) from orders where purpose = 'mm'"
        ).fetchone()[0]
        sh = conn.execute("select coalesce(sum(abs(quantity)), 0) from fills where purpose = 'mm'").fetchone()[0]
        out["mm_shares_per_quoted_market_hour"] = round(sh / hrs, 1) if hrs else None
        out["mm_quoted_market_hours"] = hrs
    except sqlite3.Error:
        pass
    conn.close()
    return out


def leaderboard(cfg: BotConfig, username: str, days_elapsed: float) -> dict:
    """Two reads: the first page (top accounts + our rank) and the page holding our row."""
    api = SuperMarketClient(api_key=cfg.api_key, base_url=cfg.base_url, reads_per_min=5, writes_per_min=1)
    path = f"/tournaments/{cfg.tournament_slug}/leaderboard"
    page = api.get(path, limit=100, offset=0, sort="pnl", period="all")
    rows = page.get("leaderboard", [])
    my_rank = page.get("myRank")
    out = {"total_accounts": page.get("total"), "my_rank": my_rank, "reads": 1}
    top10 = rows[:10]
    if top10:
        vols = sorted(r.get("volume", 0.0) for r in top10)
        med = vols[len(vols) // 2] if len(vols) % 2 else (vols[len(vols) // 2 - 1] + vols[len(vols) // 2]) / 2
        out["top10"] = {
            "median_volume": round(med),
            "median_volume_per_day": round(med / days_elapsed),
            "min_volume_per_day": round(vols[0] / days_elapsed),
            "max_volume_per_day": round(vols[-1] / days_elapsed),
            "median_trades_per_day": round(sorted(r.get("tradesCount", 0) for r in top10)[5] / days_elapsed),
            "pnl_range": [round(top10[-1].get("pnl", 0)), round(top10[0].get("pnl", 0))],
        }
    me = next((r for r in rows if r.get("username") == username), None)
    if me is None and my_rank:
        off = max(0, int(my_rank) - 3)
        try:
            page2 = api.get(path, limit=6, offset=off, sort="pnl", period="all")
            out["reads"] = 2
            me = next((r for r in page2.get("leaderboard", []) if r.get("username") == username), None)
        except ApiError as e:
            out["error"] = str(e)
    if me:
        out["me"] = {
            "rank": me.get("rank"),
            "pnl": me.get("pnl"),
            "volume": round(me.get("volume", 0.0)),
            "trades": me.get("tradesCount"),
            "volume_per_day_since_oct1": round(me.get("volume", 0.0) / days_elapsed),
        }
    return out


def fmt(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    if abs(n) >= 1e6:
        return f"{n / 1e6:.2f}M"
    if abs(n) >= 1e3:
        return f"{n / 1e3:.1f}k"
    return f"{n:.0f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-api", action="store_true", help="skip the two leaderboard reads")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--username", default=None, help="our leaderboard username (default: params/volume_target.json)")
    args = ap.parse_args(argv)

    cfg = BotConfig.load()
    target = load_target()
    now = time.time()
    now_dt = datetime.fromtimestamp(now, timezone.utc)
    days_elapsed = max(0.1, (now_dt - TOURNAMENT_START).total_seconds() / 86400)
    days_left = max(0.0, (TOURNAMENT_END - now_dt).total_seconds() / 86400)
    mode = target.get("fill_volume_mode", "yes_price")
    username = args.username or target.get("username") or "sunny-beacon-7397"

    report = {
        "generated_at": now_dt.isoformat(timespec="seconds"),
        "days_elapsed": round(days_elapsed, 2),
        "days_left": round(days_left, 2),
        "fills": fills_summary(os.path.join(HERE, cfg.db_path), mode, now),
        "target": target,
    }
    if not args.no_api:
        try:
            report["leaderboard"] = leaderboard(cfg, username, days_elapsed)
        except ApiError as e:
            report["leaderboard"] = {"error": str(e)}
        except Exception as e:  # network, JSON
            report["leaderboard"] = {"error": repr(e)}

    tgt = target.get("target_volume_per_day")
    pace = report["fills"]["pace_per_day"]
    # arb volume arrives in lumps when the sleeve deploys or recycles, so the steady pace excludes it
    pace_ex_arb = pace - report["fills"]["by_sleeve"].get("arb", {}).get("pace_per_day", 0)
    report["pace_ex_arb_per_day"] = round(pace_ex_arb)
    if tgt:
        report["gap_per_day"] = round(tgt - pace)
        report["pace_pct_of_target"] = round(100.0 * pace / tgt, 1)
        # judged on a full day of log only; a shorter window extrapolates lumps
        report["at_target"] = pace >= tgt and report["fills"]["window_hours"] >= 20

    if args.json:
        print(json.dumps(report, indent=1))
        return 0

    f = report["fills"]
    print(f"volume report {report['generated_at']}  (day {days_elapsed:.1f} of 34.7, {days_left:.1f} days left; volume = {mode})")
    print(f"our volume today (UTC): {fmt(f['today'])}   last {f['window_hours']:.0f}h: {fmt(f['last24h'])}   pace: {fmt(pace)}/day")
    print("  by sleeve (today / pace per day / fills today):")
    for p, s in sorted(f["by_sleeve"].items(), key=lambda kv: -kv[1]["pace_per_day"]):
        print(f"    {p:10s} {fmt(s['today']):>8s} {fmt(s['pace_per_day']):>9s}/day  {s['fills_today']:4d} fills")
    if f.get("mm_shares_per_quoted_market_hour") is not None:
        print(f"  mm fill rate: {f['mm_shares_per_quoted_market_hour']} shares per quoted market-hour over {f['mm_quoted_market_hours']} market-hours")
    lb = report.get("leaderboard")
    if lb:
        if "error" in lb:
            print("leaderboard: error", lb["error"])
        else:
            t = lb.get("top10", {})
            print(f"top-10 now: median volume {fmt(t.get('median_volume'))} = {fmt(t.get('median_volume_per_day'))}/day "
                  f"(range {fmt(t.get('min_volume_per_day'))}-{fmt(t.get('max_volume_per_day'))}/day), "
                  f"{t.get('median_trades_per_day')} trades/day, P&L {fmt(t.get('pnl_range', [0, 0])[0])}..{fmt(t.get('pnl_range', [0, 0])[1])}")
            me = lb.get("me")
            if me:
                print(f"us: rank {me['rank']} of {lb.get('total_accounts')}, P&L {fmt(me['pnl'])}, volume {fmt(me['volume'])} "
                      f"({fmt(me['volume_per_day_since_oct1'])}/day since 1 Oct), {me['trades']} trades  [{lb['reads']} reads]")
            else:
                print(f"us: rank {lb.get('my_rank')} (row not found)  [{lb['reads']} reads]")
    if tgt:
        print(f"target: {fmt(tgt)}/day ({target.get('target_basis', '')})   stretch {fmt(target.get('stretch_volume_per_day'))}/day   "
              f"ceiling {fmt(target.get('ceiling_volume_per_day'))}/day")
        print(f"gap: {fmt(report['gap_per_day'])}/day  (pace is {report['pace_pct_of_target']}% of target; "
              f"steady pace without arb lumps {fmt(report['pace_ex_arb_per_day'])}/day"
              f"{'; AT TARGET' if report['at_target'] else ''}"
              f"{'; window < 20 h, not judged' if f['window_hours'] < 20 else ''})")
    else:
        print("no params/volume_target.json: target not set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
