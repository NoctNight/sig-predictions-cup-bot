"""Cumulative results of each strategy since its launch, for Sizhe's check-ins.

    python3 deploy/strategy_report.py [--out data/strategy_report.md] [--no-api]

Read-only. Uses data/sigbot.sqlite, data/live.log and (unless --no-api) one positions + one pnl read via sigbot.review.
Open positions are valued at SIG's latest best price for closing them (bid for longs, ask for shorts), so "open" is what
the position would fetch at the touch now, not its payout. Add a strategy to STRATEGIES when it goes live.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LON = ZoneInfo("Europe/London")

# name, fill purposes, launch (BST), what it does in one line
STRATEGIES = [
    ("High-frequency arb", None, "2026-10-24 10:58",
     "buys two-leg sets when their gap is unusually wide vs the last 24 h, sells after +1c/set or 24 h (4k pot, -200 stop)"),
    ("Catch-up sweep", ("catchup", "catchup_exit"), "2026-10-23 22:59",
     "takes SIG's stale price when Polymarket and Kalshi have both moved 2+ pts (300/position, max 4, -50 stop)"),
    ("Cheap favourites", ("cheapfav", "cheapfav_exit"), None,
     "buys fast-count favourites SIG prices below the outside fair, held through the call (15k)"),
    ("Shock mode", ("shock", "shock_exit"), None,
     "trades SIG's lag after a 10-20 pt outside jump on both Polymarket and Kalshi"),
    ("Cash raise", ("raise",), None,
     "sells cheap-to-exit holdings only when a higher-edge strategy is short of cash"),
]

CYCLE_RE = re.compile(r"^(\S+ \S+),\d+ INFO sigbot: cycle \d+ done .* cash=(-?\d+) pos_value=(-?\d+) total=(-?\d+) netD=([+-]?\d+)")
CH_ENTER = re.compile(r"^(\S+ \S+),\d+ INFO sigbot: CHURN enter (.+?) x(\d+) at cost ([\d.]+)/set")
CH_EXIT = re.compile(r"^(\S+ \S+),\d+ INFO sigbot: CHURN exit (.+?) x(\d+) of \d+ at ([\d.]+)/set \((.*?)\): ([+-][\d.]+), realised so far ([+-][\d.]+)")
CH_GONE = re.compile(r"^(\S+ \S+),\d+ INFO sigbot: CHURN (.+?): (\d+) sets no longer held .*?: ([+-][\d.]+), realised so far ([+-][\d.]+)")


def bst(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=LON).timestamp()


_LOG: list = []


def log_lines():
    """live.log lines from 23 Oct on (read once)."""
    if not _LOG:
        with open(os.path.join(HERE, "data/live.log"), errors="replace") as f:
            _LOG.extend(ln for ln in f if ln[:10] >= "2026-10-23")
    return _LOG


def latest_quotes(db) -> dict:
    """exchange id -> (bid, ask) from SIG's latest stored quote (written on change plus a 10-min heartbeat)."""
    out = {}
    for ex, bid, ask, _ in db.execute(
            "SELECT instrument, bid, ask, MAX(ts) FROM venue_quotes WHERE venue='sig' AND ts > ? GROUP BY instrument",
            (time.time() - 3 * 3600,)):
        out[str(ex)] = (bid, ask)
    return out


def close_value(q: float, quote) -> float | None:
    """What q shares (YES terms, + long YES) fetch at the touch now."""
    if not quote or quote[0] is None or quote[1] is None:
        return None
    return q * quote[0] if q > 0 else -q * (1 - quote[1])


def churn_section(db, quotes, since):
    entries, exits, realised, pause = [], [], 0.0, None
    for ln in log_lines():
        if ln[:16] < datetime.fromtimestamp(since, LON).strftime("%Y-%m-%d %H:%M"):
            continue
        if m := CH_ENTER.match(ln):
            entries.append((m[2], int(m[3]), float(m[4])))
        elif m := CH_EXIT.match(ln):
            exits.append((m[1], m[2], int(m[3]), m[5], float(m[6])))
            realised = float(m[7])
        elif m := CH_GONE.match(ln):
            exits.append((m[1], m[2], int(m[3]), "closed elsewhere", float(m[4])))
            realised = float(m[5])
        elif "CHURN: gap movement" in ln:
            pause = ln[:16] + " " + ln.split("new entries ")[-1].split(" (")[0]
        elif "CHURN: realised" in ln:
            pause = ln[:16] + " STOP hit"
    # open sets per race = entered - exited; value at the touch via the race's legs in arb_trades
    held = {}
    for race, q, cost in entries:
        h = held.setdefault(race, [0, 0.0]); h[0] += q; h[1] += q * cost
    for _, race, q, _, _ in exits:
        if race in held:
            h = held[race]; avg = h[1] / h[0] if h[0] else 0
            h[0] -= q; h[1] -= q * avg
    open_val, open_cost, rows = 0.0, 0.0, []
    for race, (q, c) in held.items():
        if q <= 0:
            continue
        r = db.execute("SELECT legs FROM arb_trades WHERE race=? AND kind='sell_set' ORDER BY id DESC LIMIT 1", (race,)).fetchone()
        val = None
        if r:
            vals = [close_value(-q, quotes.get(str(l["exchangeId"]))) for l in json.loads(r[0])]
            val = None if None in vals else sum(vals)
        rows.append((race, q, c, val))
        open_cost += c
        open_val += val if val is not None else c
    return {"entries": len(entries), "exits": exits, "realised": realised, "open": rows, "open_pnl": open_val - open_cost,
            "pot_used": open_cost, "pause": pause}


def purpose_section(db, purposes, quotes, since):
    q_marks = ",".join("?" * len(purposes))
    fills = db.execute(f"SELECT exchange_id, quantity, price, filled_at FROM fills WHERE purpose IN ({q_marks})", purposes).fetchall()
    fills = [f for f in fills if datetime.fromisoformat(f[3].replace("Z", "+00:00")).timestamp() >= since]
    pos, cash, last = {}, 0.0, {}
    for ex, q, p, _ in fills:
        pos[ex] = pos.get(ex, 0.0) + q; cash -= q * p; last[ex] = p
    value = 0.0
    for ex, q in pos.items():
        if abs(q) < 1:
            continue
        v = close_value(q, quotes.get(str(ex)))
        value += v if v is not None else (q * last[ex] if q > 0 else -q * (1 - last[ex]))
    opens = sum(1 for q in pos.values() if abs(q) >= 1)
    return {"fills": len(fills), "open_positions": opens, "pnl": cash + value}


def account_series():
    pts = []
    for ln in log_lines():
        if m := CYCLE_RE.match(ln):
            pts.append((m[1][:16], int(m[2]), int(m[4]), int(m[5])))
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "data/strategy_report.md"))
    ap.add_argument("--no-api", action="store_true")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{os.path.join(HERE, 'data/sigbot.sqlite')}?mode=ro", uri=True)
    quotes = latest_quotes(db)
    now = datetime.now(LON)
    L = [f"# Strategy results since launch ({now:%a %d %b %H:%M} BST)", ""]

    series = account_series()
    day0 = next((p for p in series if p[0] >= now.strftime("%Y-%m-%d 00:00")), None)
    if series:
        t, c, tot, nd = series[-1]
        L.append(f"**Account:** cash {c:,}, marked total {tot:,}, net D {nd:+,}"
                 + (f" (since 00:00: cash {c - day0[1]:+,}, total {tot - day0[2]:+,})" if day0 else ""))
    if not a.no_api:
        try:
            r = subprocess.run([sys.executable, "-m", "sigbot.review", "--minutes", "60", "--json"], cwd=HERE,
                               capture_output=True, text=True, timeout=120)
            sv = json.loads(r.stdout).get("settlement_value")
            L.append(f"**Settlement value:** {sv:,.0f}")
        except Exception as e:
            L.append(f"(settlement value unavailable: {e})")
    killed = [ln[:16] for ln in log_lines() if "KILL" in ln and "killed=False" not in ln][-1:]
    L += [f"**Kill switch:** {'TRIPPED at ' + killed[0] if killed else 'not tripped'}", ""]

    L += ["| Strategy | Live since | Trades | Open | Result so far |", "|---|---|---|---|---|"]
    details = []
    for name, purposes, launch, what in STRATEGIES:
        if purposes is None:
            s = churn_section(db, quotes, bst(launch))
            tot = s["realised"] + s["open_pnl"]
            L.append(f"| {name} | {launch[5:]} | {s['entries']} buys, {len(s['exits'])} sells | "
                     f"{len(s['open'])} races, {s['pot_used']:,.0f} of the pot | **{tot:+,.0f}** "
                     f"(realised {s['realised']:+,.0f}, open {s['open_pnl']:+,.0f} at the touch) |")
            d = [f"**{name}:** {what}."]
            for t, race, q, why, pnl in s["exits"]:
                d.append(f"- sold {race} x{q} at {t[11:]} ({why}): {pnl:+.0f}")
            for race, q, c, v in s["open"]:
                d.append(f"- holding {race} x{q}, cost {c:,.0f}" + (f", worth {v:,.0f} at the touch now" if v is not None else "")
                         + f" (pays {q:,} at settlement: {q - c:+,.0f}; the gap to the touch is the spread it waits to win back)")
            if s["pause"]:
                d.append(f"- last pause/stop event: {s['pause']}")
            details += d + [""]
            continue
        first = db.execute(f"SELECT MIN(filled_at) FROM fills WHERE purpose IN ({','.join('?' * len(purposes))})", purposes).fetchone()[0]
        if launch is None and first is None:
            L.append(f"| {name} | not live yet | | | |")
            continue
        since = bst(launch) if launch else datetime.fromisoformat(first.replace("Z", "+00:00")).timestamp()
        s = purpose_section(db, purposes, quotes, since)
        live = launch[5:] if launch else datetime.fromtimestamp(since, LON).strftime("%m-%d %H:%M")
        L.append(f"| {name} | {live} | {s['fills']} fills | {s['open_positions']} positions | **{s['pnl']:+,.0f}** |")
        details.append(f"**{name}:** {what}.")
        details.append("")
    L += ["", "Result = realised P&L plus open positions valued at what they would sell for at SIG's best price now.", ""]
    L += details
    if series:
        L += ["**Account through the day (hourly):**", "", "| Time | Cash | Marked total | Net D |", "|---|---|---|---|"]
        seen = set()
        for t, c, tot, nd in series:
            if t >= now.strftime("%Y-%m-%d 00:00") and t[:13] not in seen:
                seen.add(t[:13]); L.append(f"| {t[11:16]} | {c:,} | {tot:,} | {nd:+,} |")
    open(a.out, "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
