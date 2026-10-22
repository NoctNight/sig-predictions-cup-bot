"""Live dashboard: one self-refreshing HTML page on http://127.0.0.1:8765.

    python3 -m sigbot.dashboard                 # serve; recompute every 45 s, 2 venue reads a minute at most
    python3 -m sigbot.dashboard --once          # write data/dashboard.html once and exit
    python3 -m sigbot.dashboard --once --no-api # same, with no venue reads (account value from the cycles table)
    python3 -m sigbot.dashboard --json          # print the computed numbers instead of HTML

Reads data/sigbot.sqlite read-only, data/markets.json, data/live.log, data/auto_tuning.log, config.json and
params/volume_target.json. The only venue reads are /portfolio/positions and /portfolio/pnl (the same two reads as
sigbot.review), at most once every --venue-every seconds (default 60). Never writes anything except --once's file.

Definitions (each section repeats its own in a footnote)
  marked value      the venue's account value: cash + every position at the venue's marks
  settlement value  marked value with arb sets (NO on every leg of a race, the common quantity k) valued at their
                    locked payoff (legs - 1) x k instead of at marks; equals sigbot.review's settlement_value.
                    The difference is k x (sum of the legs' YES marks - 1).
  P&L over a window settlement value now minus settlement value at the window start; the past value is the cycles
                    table's total_value at the last cycle before the start plus the arb-set adjustment rebuilt from
                    positions at that time (positions now minus later fills) and marks then (last quote mid, else
                    last public print). Before the bot's first cycle the account was 100,000 cash.
  volume            leaderboard definition: quantity x price paid on the side bought (YES: price, NO: 1 - price)
  venue volume      the same definition summed over both counterparties of every public print = print quantity
  markout           mid N minutes after a fill minus the fill price, signed in our favour, in ticks (0.005), with
                    the same reference as sigbot.review: first quote snapshot at or after fill + N, else the first
                    public print at or after it
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(HERE, "data", "sigbot.sqlite")
TICK = 0.005
START_VALUE = 100_000.0
FEED_START = "2026-10-22T10:06"          # realtime tape feed began; earlier tape is per-exchange REST backfill
TITLE_RE = re.compile(r"Will the (Democratic|Republican|Independent) Party win the (.+)\?")
MARKOUT_SLEEVES = ("mm", "inside", "tilt", "direction")
NO_RANK_SLEEVES = ("arb", "hedge")       # set legs: per-leg marks are meaningless, see the positions section
EVENT_KEYS = (("KILL SWITCH", "KILL SWITCH"), ("RETRACE PAUSE", "RETRACE PAUSE"), ("APPLIED", " APPLIED "),
              ("OFFLOAD", "OFFLOAD"), ("LAG", "sigbot: LAG "))
HEALTH_RE = re.compile(r"markout30=(\S+)\(n=(\d+)\) drawdown=(-?\d+) netD=([+-]?\d+) killed=(\w+) wave=(\w+) hot=(\d+) feed=\[([^\]]*)\]")


def _ts(s: str) -> float:
    """ISO UTC string (Z or +00:00, any number of fractional digits) -> epoch seconds."""
    t = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    if len(s) > 20 and s[19] == ".":
        d = re.match(r"\d+", s[20:])
        if d:
            t += float("0." + d.group(0))
    return t


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


# --------------------------------------------------------------------------------------------- venue
class Venue:
    """Positions + pnl, refreshed at most once every `every` seconds (2 reads)."""

    def __init__(self, every: float, enabled: bool = True):
        self.every, self.enabled = every, enabled
        self.at = 0.0
        self.positions: dict = {}
        self.pnl: dict = {}
        self.error = None
        self.reads = 0
        self.client = None

    def refresh(self, now: float):
        if not self.enabled or now - self.at < self.every:
            return
        try:
            if self.client is None:
                from .api import SuperMarketClient
                from .config import BotConfig
                cfg = BotConfig.load()
                self.slug = cfg.tournament_slug
                self.client = SuperMarketClient(api_key=cfg.api_key, base_url=cfg.base_url, reads_per_min=4,
                                                writes_per_min=1, timeout=15, max_retries=0)
            base = f"/tournaments/{self.slug}/portfolio"
            pos = self.client.get(base + "/positions")
            pnl = self.client.get(base + "/pnl", period="all")
            self.reads += 2
            self.positions = {str(p["exchangeId"]): p for p in pos.get("positions", []) if not p.get("settled")}
            self.pnl = pnl
            self.at, self.error = now, None
        except Exception as e:      # keep the last good read; the page shows its age
            self.at, self.error = now, f"{type(e).__name__}: {str(e)[:160]}"


# ------------------------------------------------------------------------------------------ log tail
class LogTail:
    """Incrementally parses data/live.log (and data/auto_tuning.log) for HEALTH and event lines."""

    def __init__(self):
        self.files = {"live": [os.path.join(HERE, "data", "live.log"), 0, b""],
                      "tune": [os.path.join(HERE, "data", "auto_tuning.log"), 0, b""]}
        self.events = {k: deque(maxlen=12) for k, _ in EVENT_KEYS}
        self.health = None
        self.health_line = None

    def _lines(self, key):
        path, off, rest = self.files[key]
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        if size < off:                     # truncated or replaced
            off, rest = 0, b""
        if size == off:
            return []
        with open(path, "rb") as f:
            f.seek(off)
            chunk = rest + f.read(size - off)
        lines = chunk.split(b"\n")
        self.files[key][1], self.files[key][2] = size, lines[-1]
        return [l.decode("utf-8", "replace") for l in lines[:-1]]

    def update(self):
        for line in self._lines("live"):
            if "HEALTH " in line:
                m = HEALTH_RE.search(line)
                if m:
                    self.health_line = line
                    self.health = {"time": line[:19], "markout30": m.group(1), "n_markout": int(m.group(2)),
                                   "drawdown": int(m.group(3)), "netD": int(m.group(4)), "killed": m.group(5) == "True",
                                   "wave": m.group(6) == "True", "hot": int(m.group(7)), "feed": m.group(8)}
                continue
            for k, needle in EVENT_KEYS:
                if needle in line and not ("report-only" in line and k == "OFFLOAD" and False):
                    txt = line[24:].split(": ", 1)[-1].strip()
                    for pre in (k + ": ", k + " "):
                        if txt.startswith(pre):
                            txt = txt[len(pre):]
                    self.events[k].append((line[:19], txt))
                    break
        for line in self._lines("tune"):
            if line.split(" ", 2)[1:2] and ("APPLIED by=" in line or "DROPPED by=" in line):
                self.events["APPLIED"].append((line[:19], line[20:].strip()))


# ------------------------------------------------------------------------------------- computation
class Dashboard:
    def __init__(self, venue: Venue):
        self.venue = venue
        self.logs = LogTail()
        self.markouts: dict = {}          # (fill_id, horizon) -> ticks   (only resolved ones are cached)
        self.mid_at_fill: dict = {}       # fill_id -> mid at fill time (or None)
        self.mark_cache: dict = {}        # (exchange, t) -> mark, for aligned chart points
        self.legs_mtime = None
        self.data: dict = {}
        self.html = "<p>starting…</p>"
        self.compute_ms = 0.0

    # ------------------------------------------------------------------ helpers
    def _db(self):
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def _legs(self):
        p = os.path.join(HERE, "data", "markets.json")
        mt = os.path.getmtime(p)
        if mt != self.legs_mtime:
            self.leg, self.race_legs = {}, defaultdict(list)
            for m in _load_json(p, []):
                mt_ = TITLE_RE.match(m["title"])
                if mt_:
                    ex = str(m["exchanges"][0]["id"])
                    self.leg[ex] = (mt_.group(2), mt_.group(1)[0])
                    self.race_legs[mt_.group(2)].append(ex)
            self.legs_mtime = mt
        return self.leg, self.race_legs

    def _mark_at(self, db, ex: str, t: float):
        """Mark at time t: last quote mid within 15 min before t, else last public print before t."""
        q = db.execute("SELECT mid, ts FROM quotes WHERE exchange_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (ex, t)).fetchone()
        if q and q["mid"] is not None and q["ts"] >= t - 900:
            return q["mid"]
        r = db.execute("SELECT price FROM tape WHERE exchange_id=? AND executed_at<=? ORDER BY executed_at DESC LIMIT 1",
                       (ex, _iso(t) + ".999")).fetchone()
        if r:
            return r["price"]
        return q["mid"] if q and q["mid"] is not None else None

    def _arb_adj(self, db, pos: dict, race_legs: dict, t: float, marks: dict | None = None, cache: bool = False):
        """Settlement minus marked value of held arb sets at time t: sum over races of k x (sum YES marks - 1)."""
        adj, sets = 0.0, []
        for race, legs in race_legs.items():
            if len(legs) < 2:
                continue
            qs = [pos.get(ex, 0.0) for ex in legs]
            if not all(q < -1e-9 for q in qs):
                continue
            k = min(-q for q in qs)
            ms = []
            for ex in legs:
                if marks is not None and ex in marks:
                    m = marks[ex]
                elif cache:
                    key = (ex, t)
                    if key not in self.mark_cache:
                        self.mark_cache[key] = self._mark_at(db, ex, t)
                    m = self.mark_cache[key]
                else:
                    m = self._mark_at(db, ex, t)
                ms.append(m)
            if any(m is None for m in ms):
                continue
            adj += k * (sum(ms) - 1.0)
            sets.append((race, k, sum(ms)))
        return adj, sets

    def _markout(self, db, f, h: str, secs: int, now: float):
        key = (f["id"], h)
        if key in self.markouts:
            return self.markouts[key]
        if now - f["t"] < secs:
            return None
        t = f["t"] + secs
        q = db.execute("SELECT mid FROM quotes WHERE exchange_id=? AND ts>=? ORDER BY ts ASC LIMIT 1", (f["ex"], t)).fetchone()
        ref = q["mid"] if q and q["mid"] is not None else None
        if ref is None:
            tr = db.execute("SELECT price FROM tape WHERE exchange_id=? AND executed_at>=? ORDER BY executed_at ASC LIMIT 1",
                            (f["ex"], _iso(t))).fetchone()
            ref = tr["price"] if tr else None
        if ref is None:
            return None
        e = (ref - f["price"]) if f["side"] == "yes" else (f["price"] - ref)
        self.markouts[key] = e / TICK
        return self.markouts[key]

    def _entry_mid(self, db, f):
        if f["id"] in self.mid_at_fill:
            return self.mid_at_fill[f["id"]]
        q = db.execute("SELECT mid, ts FROM quotes WHERE exchange_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (f["ex"], f["t"])).fetchone()
        mid = q["mid"] if q and q["mid"] is not None and q["ts"] >= f["t"] - 600 else None
        if mid is None:
            mid = f["mid_at_place"]
        if mid is None:
            r = db.execute("SELECT price FROM tape WHERE exchange_id=? AND executed_at<? AND trade_id<>? ORDER BY executed_at DESC LIMIT 1",
                           (f["ex"], f["iso"], str(f["id"]))).fetchone()
            mid = r["price"] if r else None
        self.mid_at_fill[f["id"]] = mid
        return mid

    # ------------------------------------------------------------------ main
    def compute(self) -> dict:
        now = time.time()
        self.venue.refresh(now)
        t0 = time.process_time()
        self.logs.update()
        leg, race_legs = self._legs()
        cfg = _load_json(os.path.join(HERE, "config.json"), {})
        target = _load_json(os.path.join(HERE, "params", "volume_target.json"), {})
        cap = cfg.get("risk", {}).get("portfolio_max_partisan_notional") or _find(cfg, "portfolio_max_partisan_notional") or 5000
        kill_pause = cfg.get("kill_pause_sec", 1800)
        db = self._db()
        D: dict = {"now": now, "warnings": [], "discrepancies": []}

        # ---- fills
        fills = []
        for r in db.execute("SELECT id, exchange_id, side, price, quantity, filled_at, COALESCE(purpose,'?') purpose, mid_at_place "
                            "FROM fills WHERE price IS NOT NULL ORDER BY filled_at"):
            p = r["price"]
            fills.append({"id": r["id"], "ex": r["exchange_id"], "side": r["side"], "price": p, "q": r["quantity"],
                          "iso": r["filled_at"], "t": _ts(r["filled_at"]), "purpose": r["purpose"], "mid_at_place": r["mid_at_place"],
                          "vol": abs(r["quantity"]) * (p if r["side"] == "yes" else 1 - p)})
        sl = defaultdict(lambda: defaultdict(float))
        for f in fills:
            sl[f["ex"]][f["purpose"]] += abs(f["q"])
        sleeve_of = {ex: max(v, key=v.get) for ex, v in sl.items()}

        # ---- positions and marks
        fill_pos = defaultdict(float)
        for f in fills:
            fill_pos[f["ex"]] += f["q"]
        V = self.venue
        have_venue = bool(V.pnl) and V.at > 0
        if have_venue:
            pos = {ex: float(p["quantity"]) for ex, p in V.positions.items() if p.get("quantity")}
            mism = [(ex, round(fill_pos.get(ex, 0.0)), round(q)) for ex, q in pos.items() if abs(fill_pos.get(ex, 0.0) - q) > 0.5]
            mism += [(ex, round(q), 0) for ex, q in fill_pos.items() if abs(q) > 0.5 and ex not in pos]
            if mism:
                D["discrepancies"].append(f"positions: {len(mism)} markets where the sum of logged fills differs from the venue "
                                          f"position (e.g. {', '.join(f'{e}: fills {a} vs venue {b}' for e, a, b in mism[:4])})")
        else:
            pos = {ex: q for ex, q in fill_pos.items() if abs(q) > 1e-9}
        last_q = {r["exchange_id"]: r["mid"] for r in db.execute(
            "SELECT exchange_id, mid, MAX(ts) FROM quotes WHERE ts>=? AND mid IS NOT NULL GROUP BY exchange_id", (now - 900,))}
        last_t = {r["exchange_id"]: r["price"] for r in db.execute(
            "SELECT exchange_id, price, MAX(executed_at) FROM tape GROUP BY exchange_id")}
        mark, mark_src = {}, {}
        for ex in set(pos) | set(sleeve_of):
            vp = V.positions.get(ex) if have_venue else None
            if vp and vp.get("quantity"):
                q = float(vp["quantity"])
                side_px = float(vp["marketValue"]) / abs(q)
                mark[ex], mark_src[ex] = (side_px if q > 0 else 1 - side_px), "venue"
            elif ex in last_q:
                mark[ex], mark_src[ex] = last_q[ex], "quote"
            elif ex in last_t:
                mark[ex], mark_src[ex] = last_t[ex], "print"
        my_mark = {ex: last_q.get(ex, last_t.get(ex)) for ex in mark}

        # ---- headline
        cyc = db.execute("SELECT ts, cash, positions_value, total_value FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()
        first_cycle = db.execute("SELECT MIN(ts) FROM cycles").fetchone()[0]
        tv_now = float(V.pnl.get("totalAccountValue")) if have_venue and V.pnl.get("totalAccountValue") is not None else (cyc["total_value"] if cyc else START_VALUE)
        adj_v, sets_v = self._arb_adj(db, pos, race_legs, now, marks=mark)
        adj_m, _ = self._arb_adj(db, pos, race_legs, now, marks={ex: m for ex, m in my_mark.items() if m is not None})
        settle_now = tv_now + adj_v
        H = {"marked": tv_now, "settlement": settle_now, "arb_adj": adj_v, "pnl_start": settle_now - START_VALUE,
             "marked_pnl_start": tv_now - START_VALUE, "source": "venue" if have_venue else "cycles table (no venue read)",
             "venue_age": now - V.at if have_venue else None, "venue_error": V.error, "venue_reads": V.reads,
             "cycle_total": cyc["total_value"] if cyc else None}
        if have_venue and cyc and abs(cyc["total_value"] - tv_now) > 25 and now - cyc["ts"] < 120:
            D["discrepancies"].append(f"account value: venue {tv_now:,.0f} vs last cycle {cyc['total_value']:,.0f}")
        if abs(adj_v - adj_m) > 20:
            D["discrepancies"].append(f"arb-set adjustment at venue marks {adj_v:,.0f} vs at our quote mids {adj_m:,.0f} (the venue marks are "
                                      "neither mids nor last prints); past adjustments carry each race's current difference")

        fills_after = sorted(fills, key=lambda f: f["t"])

        def pos_at(t):
            p = dict(pos)
            for f in fills_after:
                if f["t"] > t:
                    p[f["ex"]] = p.get(f["ex"], 0.0) - f["q"]
            return p

        def tv_at(t):
            if first_cycle is None or t < first_cycle:
                return START_VALUE
            r = db.execute("SELECT total_value FROM cycles WHERE ts<=? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
            return r["total_value"]

        # the venue's marks are neither quote mids nor last prints, so past adjustments (rebuilt at quote mids) carry each
        # race's current venue-minus-mid difference, per set; races no longer held get none
        bias = {race: sum(mark[ex] - (my_mark.get(ex) if my_mark.get(ex) is not None else mark[ex]) for ex in race_legs[race])
                for race, _, _ in sets_v}

        def settle_at(t, cache=False):
            if first_cycle is None or t < first_cycle:
                return START_VALUE, START_VALUE
            tv = tv_at(t)
            a, sets = self._arb_adj(db, pos_at(t), race_legs, t, cache=cache)
            return tv + a + sum(k * bias.get(race, 0.0) for race, k, _ in sets), tv

        win = {}
        for label, secs in (("1h", 3600), ("6h", 21600), ("24h", 86400)):
            s_then, tv_then = settle_at(now - secs)
            win[label] = {"settle": settle_now - s_then, "marked": tv_now - tv_then, "start_value": s_then}
        H["windows"] = win
        D["headline"] = H

        # account-value chart: cycles every 15 min plus reconstructed settlement value on a 30-min grid
        series = [(r["ts"], r["total_value"]) for r in db.execute(
            "SELECT ts, total_value FROM cycles WHERE ts>=? AND cycle_id % 10 = 0 ORDER BY ts", (now - 86400,))]
        grid0 = (int(now) // 1800) * 1800
        sgrid = []
        for i in range(47, -1, -1):
            t = grid0 - i * 1800
            if first_cycle is not None and t >= first_cycle:
                sgrid.append((t, settle_at(t, cache=True)[0]))
        sgrid.append((now, settle_now))
        D["chart"] = {"marked": series + [(now, tv_now)], "settle": sgrid}

        # ---- status
        st = {"cycle_age": now - cyc["ts"] if cyc else None, "health": self.logs.health}
        try:
            out = subprocess.run(["pgrep", "-f", "sigbot.run --live"], capture_output=True, text=True, timeout=5).stdout.split()
            st["procs"] = len(out)
        except Exception:
            st["procs"] = None
        try:
            lp = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.sizhe.sigbot"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"state = (\w+)", lp)
            st["launchd"] = m.group(1) if m else "not loaded"
        except Exception:
            st["launchd"] = "?"
        ks = self.logs.events["KILL SWITCH"]
        st["last_kill"] = ks[-1][0] if ks else None
        if ks:
            kt = time.mktime(time.strptime(ks[-1][0], "%Y-%m-%d %H:%M:%S"))
            st["kill_active"] = now - kt < kill_pause
            st["kill_until"] = kt + kill_pause
        else:
            st["kill_active"] = False
        if self.logs.health and self.logs.health["killed"]:
            st["kill_active"] = True
        D["status"] = st
        if st["cycle_age"] is not None and st["cycle_age"] > 180:
            D["warnings"].append(f"last bot cycle {st['cycle_age'] / 60:.0f} min ago")

        # ---- volume
        bot_down = []
        prev = None
        for (ts,) in db.execute("SELECT ts FROM cycles WHERE ts>=? ORDER BY ts", (now - 86400 - 600,)):
            if prev is not None and ts - prev > 300:
                bot_down.append((prev, ts))
            prev = ts
        if prev is not None and now - prev > 300:
            bot_down.append((prev, now))
        vol = {}
        for label, secs in (("1h", 3600), ("24h", 86400)):
            since = now - secs
            iso = _iso(since)
            ours = [f for f in fills if f["t"] >= since]
            by = defaultdict(float)
            for f in ours:
                by[f["purpose"]] += f["vol"]
            v_ours = sum(by.values())
            sh_ours = sum(abs(f["q"]) for f in ours)
            r = db.execute("SELECT COALESCE(SUM(quantity),0) q, COUNT(*) n, COALESCE(SUM(quantity*price),0) yn FROM tape WHERE executed_at>=?", (iso,)).fetchone()
            minutes = {x[0] for x in db.execute("SELECT DISTINCT substr(executed_at,1,16) FROM tape WHERE executed_at>=?", (iso,))}
            gaps, run, longest, tm = 0, 0, 0, int(since // 60) * 60 + 60
            while tm < now - 60:
                if _iso(tm)[:16] in minutes:
                    run = 0
                else:
                    run += 1
                    gaps += 1
                    longest = max(longest, run)
                tm += 60
            ids = [str(f["id"]) for f in ours if f["iso"] >= FEED_START]
            matched = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                matched += db.execute(f"SELECT COUNT(*) FROM tape WHERE trade_id IN ({','.join('?' * len(chunk))})", chunk).fetchone()[0]
            down = sum(min(b, now) - max(a, since) for a, b in bot_down if b > since)
            flags = []
            if ids and matched / len(ids) < 0.97:
                flags.append(f"only {matched}/{len(ids)} of our fills appear in the tape")
            if longest >= 10:
                flags.append(f"longest stretch with no public print {longest} min")
            if down > 300:
                flags.append(f"bot (and its tape feed) was down {down / 60:.0f} min of the window; that stretch is REST backfill only")
            if iso < FEED_START:
                flags.append("window starts before the realtime feed (10:06 UTC 22 Oct); earlier tape is REST backfill")
            hours = min(secs, max(60.0, now - (fills[0]["t"] if fills else now))) / 3600
            vol[label] = {"ours": v_ours, "by_sleeve": dict(by), "shares": sh_ours, "n_fills": len(ours),
                          "venue": r["q"], "venue_prints": r["n"], "venue_yes_notional": r["yn"],
                          "share": v_ours / r["q"] if r["q"] else None, "share_qty": sh_ours / r["q"] if r["q"] else None,
                          "pace": v_ours * 24 / hours if hours else 0, "hours": hours, "empty_minutes": gaps,
                          "longest_gap": longest, "fills_in_tape": (matched, len(ids)), "bot_down_min": down / 60, "flags": flags}
        hourly = []
        for i in range(23, -1, -1):
            a = (int(now) // 3600) * 3600 - i * 3600
            b = a + 3600
            v = sum(f["vol"] for f in fills if a <= f["t"] < b)
            tv_ = db.execute("SELECT COALESCE(SUM(quantity),0) FROM tape WHERE executed_at>=? AND executed_at<?", (_iso(a), _iso(b))).fetchone()[0]
            hourly.append((a, v, tv_))
        D["volume"] = {"windows": vol, "target": target.get("target_volume_per_day", 100000), "hourly": hourly}

        # ---- recent fills
        recent = []
        for f in sorted(fills, key=lambda f: -f["t"])[:30]:
            race, party = leg.get(f["ex"], (f["ex"], "?"))
            recent.append({"t": f["t"], "race": race, "party": party, "ex": f["ex"], "sleeve": f["purpose"], "side": f["side"],
                           "price": f["price"], "q": abs(f["q"]), "vol": f["vol"],
                           "mk5": self._markout(db, f, "5m", 300, now), "mk30": self._markout(db, f, "30m", 1800, now)})
        D["recent"] = recent

        # ---- P&L by sleeve and market (marked basis = sum over fills of q x (mark - price))
        def blank():
            return {"pnl": 0.0, "edge": 0.0, "move": 0.0, "n": 0, "vol": 0.0, "unexplained": 0.0}
        sleeves_all, sleeves_24 = defaultdict(blank), defaultdict(blank)
        mkt_all, mkt_24 = defaultdict(blank), defaultdict(blank)
        since24 = now - 86400
        for f in fills:
            m = mark.get(f["ex"])
            if m is None:
                continue
            pnl = f["q"] * (m - f["price"])
            mid = self._entry_mid(db, f)
            for agg, key, ok in ((sleeves_all, f["purpose"], True), (mkt_all, f["ex"], True),
                                 (sleeves_24, f["purpose"], f["t"] >= since24), (mkt_24, f["ex"], f["t"] >= since24)):
                if not ok:
                    continue
                a = agg[key]
                a["pnl"] += pnl
                a["n"] += 1
                a["vol"] += f["vol"]
                if mid is not None:
                    a["edge"] += f["q"] * (mid - f["price"])
                    a["move"] += f["q"] * (m - mid)
                else:
                    a["unexplained"] += pnl
        # 24 h per market also carries the position held at the window start
        p24 = pos_at(since24)
        for ex, q0 in p24.items():
            if abs(q0) < 1e-9 or ex not in mark:
                continue
            m0 = self._mark_at(db, ex, since24)
            if m0 is not None:
                mkt_24[ex]["pnl"] += q0 * (mark[ex] - m0)
                mkt_24[ex]["move"] += q0 * (mark[ex] - m0)
        if "arb" in sleeves_all:
            sleeves_all["arb"]["settle_extra"] = adj_v
        D["pnl_sleeves"] = {"all": dict(sleeves_all), "24h": dict(sleeves_24)}
        D["sum_fill_pnl"] = sum(v["pnl"] for v in sleeves_all.values())

        def rows(agg):
            out = []
            for ex, a in agg.items():
                if sleeve_of.get(ex) in NO_RANK_SLEEVES:
                    continue
                race, party = leg.get(ex, (ex, "?"))
                out.append(dict(a, ex=ex, race=race, party=party, sleeve=sleeve_of.get(ex, "?"), pos=pos.get(ex, 0.0)))
            return sorted(out, key=lambda r: r["pnl"])
        ra, r24 = rows(mkt_all), rows(mkt_24)
        D["markets"] = {"all_best": ra[::-1][:10], "all_worst": ra[:10], "24_best": r24[::-1][:10], "24_worst": r24[:10]}

        # ---- markouts
        mk = defaultdict(list)
        per_mkt = defaultdict(lambda: {"n": 0, "neg": 0, "ticks": 0.0, "usd": defaultdict(float)})
        for f in fills:
            for h, secs in (("5m", 300), ("30m", 1800)):
                if f["purpose"] not in MARKOUT_SLEEVES and not (h == "5m" and f["purpose"] not in NO_RANK_SLEEVES):
                    continue
                v = self._markout(db, f, h, secs, now)
                if v is None:
                    continue
                if f["purpose"] in MARKOUT_SLEEVES:
                    for w, ws in (("1h", 3600), ("24h", 86400), ("all", 1e12)):
                        if now - f["t"] <= ws:
                            mk[(f["purpose"], h, w)].append(v)
                if h == "5m" and f["purpose"] not in NO_RANK_SLEEVES:
                    pm = per_mkt[f["ex"]]
                    pm["n"] += 1
                    pm["neg"] += v < 0
                    pm["ticks"] += v
                    pm["usd"][f["purpose"]] += v * TICK * abs(f["q"])
        D["markouts"] = {f"{p}|{h}|{w}": {"n": len(v), "mean": sum(v) / len(v)} for (p, h, w), v in mk.items()}
        losing = []
        for ex, pm in per_mkt.items():
            if pm["n"] >= 10 and pm["neg"] / pm["n"] >= 0.6:
                race, party = leg.get(ex, (ex, "?"))
                worst = min(pm["usd"], key=pm["usd"].get)
                losing.append({"ex": ex, "race": race, "party": party, "n": pm["n"], "neg_pct": pm["neg"] / pm["n"],
                               "mean": pm["ticks"] / pm["n"], "usd": sum(pm["usd"].values()), "sleeve": worst,
                               "pnl": mkt_all.get(ex, {}).get("pnl")})
        D["losing"] = sorted(losing, key=lambda r: r["usd"])
        D["losing_near"] = sorted(({"ex": ex, "n": pm["n"], "neg_pct": pm["neg"] / pm["n"]} for ex, pm in per_mkt.items()
                                   if pm["n"] >= 5), key=lambda r: -r["neg_pct"])[:3]

        # ---- positions
        plist = []
        set_k = {}
        for race, k, _ in sets_v:
            for ex in race_legs[race]:
                set_k[ex] = k
        for ex, q in pos.items():
            vp = V.positions.get(ex, {}) if have_venue else {}
            m = mark.get(ex)
            mv = float(vp["marketValue"]) if vp.get("marketValue") is not None else (abs(q) * (m if q > 0 else 1 - m) if m is not None else None)
            cost = float(vp["costBasis"]) if vp.get("costBasis") is not None else None
            race, party = leg.get(ex, (ex, "?"))
            plist.append({"ex": ex, "race": race, "party": party, "q": q, "mv": mv, "cost": cost, "mark": m,
                          "sleeve": sleeve_of.get(ex, "?"), "in_set": set_k.get(ex, 0.0)})
        plist.sort(key=lambda r: -(r["mv"] or 0))
        sets_out = []
        for race, k, msum in sets_v:
            legs = race_legs[race]
            cost_set = 0.0
            ok = have_venue
            for ex in legs:
                vp = V.positions.get(ex, {})
                if not vp.get("costBasis") or not vp.get("quantity"):
                    ok = False
                    break
                cost_set += float(vp["costBasis"]) * k / abs(float(vp["quantity"]))
            sets_out.append({"race": race, "legs": len(legs), "k": k, "overround": msum - 1.0,
                             "cost": cost_set if ok else None, "locked": ((len(legs) - 1) * k - cost_set) if ok else None,
                             "marked_pnl": (k * (len(legs) - msum) - cost_set) if ok else None})
        sets_out.sort(key=lambda s: -(s["locked"] or 0))
        # net partisan D-notional, as sigbot/risk.py: arb sets netted out, D +, R -, I ignored, at quote mids
        net = dict(pos)
        for race, legs in race_legs.items():
            qs = [net.get(ex, 0.0) for ex in legs]
            if len(qs) >= 2 and all(q < 0 for q in qs):
                common = max(qs)
                for ex in legs:
                    net[ex] = net.get(ex, 0.0) - common
        netd = 0.0
        for ex, q in net.items():
            if abs(q) < 1e-9 or ex not in leg or leg[ex][1] == "I":
                continue
            m = my_mark.get(ex) if my_mark.get(ex) is not None else mark.get(ex)
            if m is None:
                continue
            sn = q * m if q > 0 else q * (1 - m)
            netd += sn if leg[ex][1] == "D" else -sn
        D["positions"] = {"top": plist[:15], "n": len(plist), "sets": sets_out, "net_d": netd, "cap": cap,
                          "health_netD": self.logs.health["netD"] if self.logs.health else None,
                          "gross": sum(r["mv"] or 0 for r in plist)}

        # ---- events
        ev = []
        for k, _ in EVENT_KEYS:
            for t, txt in self.logs.events[k]:
                ev.append((t, k, txt))
        ev.sort(reverse=True)
        D["events"] = ev[:40]
        D["event_last"] = {k: (self.logs.events[k][-1][0] if self.logs.events[k] else None) for k, _ in EVENT_KEYS}
        db.close()
        self.compute_ms = (time.process_time() - t0) * 1000
        D["compute_ms"] = self.compute_ms
        self.data = D
        self.html = render(D)
        return D


def _find(d, key):
    if isinstance(d, dict):
        for k, v in d.items():
            if k == key:
                return v
            r = _find(v, key)
            if r is not None:
                return r
    return None


# ------------------------------------------------------------------------------------------ render
def _f(x, nd=0, sign=False):
    if x is None:
        return "–"
    if abs(x) < 0.5 * 10 ** -nd:
        x = 0.0
    s = f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"
    return s.replace("-", "−")


def _cls(x):
    return "" if x is None or abs(x) < 1e-9 else ("pos" if x > 0 else "neg")


def _lt(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def _ago(s):
    if s is None:
        return "–"
    return f"{s:.0f} s" if s < 120 else (f"{s / 60:.0f} min" if s < 7200 else f"{s / 3600:.1f} h")


def _e(s):
    return html.escape(str(s))


def _svg_lines(series: list[list[tuple]], colors: list[str], w=560, h=150, labels=None):
    pts = [p for s in series for p in s]
    if len(pts) < 2:
        return ""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    pad = max(1.0, (y1 - y0) * 0.08)
    y0, y1 = y0 - pad, y1 + pad
    sx = lambda x: 40 + (x - x0) / max(1, x1 - x0) * (w - 50)
    sy = lambda y: 8 + (y1 - y) / max(1e-9, y1 - y0) * (h - 28)
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">']
    for frac in (0, 0.5, 1):
        yv = y0 + frac * (y1 - y0)
        out.append(f'<line x1="40" x2="{w - 10}" y1="{sy(yv):.1f}" y2="{sy(yv):.1f}" class="grid"/>'
                   f'<text x="36" y="{sy(yv) + 4:.1f}" class="ax" text-anchor="end">{yv / 1000:,.1f}k</text>')
    for t in range(int(x0 // 21600 + 1) * 21600, int(x1) + 1, 21600):
        out.append(f'<text x="{sx(t):.1f}" y="{h - 4}" class="ax" text-anchor="middle">{time.strftime("%H:%M", time.localtime(t))}</text>')
    for s, c in zip(series, colors):
        if len(s) >= 2:
            d = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in s)
            out.append(f'<polyline points="{d}" fill="none" stroke="{c}" stroke-width="1.8"/>')
    out.append("</svg>")
    return "".join(out)


def _svg_bars(hourly, w=560, h=120):
    if not hourly:
        return ""
    mx = max(v for _, v, _ in hourly) or 1
    bw = (w - 50) / len(hourly)
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">',
           f'<text x="36" y="14" class="ax" text-anchor="end">{mx / 1000:,.1f}k</text>',
           f'<line x1="40" x2="{w - 10}" y1="{h - 18}" y2="{h - 18}" class="grid"/>']
    for i, (a, v, tv) in enumerate(hourly):
        bh = v / mx * (h - 30)
        x = 40 + i * bw
        share = f"{100 * v / tv:.1f}% of venue" if tv else "no tape"
        out.append(f'<rect x="{x + 1:.1f}" y="{h - 18 - bh:.1f}" width="{bw - 2:.1f}" height="{bh:.1f}" class="bar">'
                   f'<title>{time.strftime("%H:00", time.localtime(a))}: ours {v:,.0f}, venue {tv:,.0f} ({share})</title></rect>')
        if i % 6 == 0:
            out.append(f'<text x="{x:.1f}" y="{h - 4}" class="ax">{time.strftime("%H:00", time.localtime(a))}</text>')
    out.append("</svg>")
    return "".join(out)


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mut:#6b6b70;--line:#e3e3e0;--pos:#16794c;--neg:#c0362c;--acc:#2b6cb0;--acc2:#b7791f;--warn:#fff4d6;--warnb:#e0b84d;}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#141416;--card:#1d1d20;--fg:#e8e8ea;--mut:#9a9aa2;--line:#2d2d32;--pos:#4cc38a;--neg:#ff6b5e;--acc:#6aa8ff;--acc2:#e3b04b;--warn:#3a3014;--warnb:#8a6d1f;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",Helvetica,Arial,sans-serif}
header{padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px 18px;align-items:baseline;border-bottom:1px solid var(--line)}
header h1{font-size:15px;margin:0}main{padding:12px 16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,560px),1fr));gap:12px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;min-width:0;overflow-x:auto}
section.wide{grid-column:1/-1}h2{font-size:13px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
h3{font-size:12px;margin:10px 0 4px;color:var(--mut)}
.kpis{display:flex;flex-wrap:wrap;gap:8px 22px}.kpi .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}.kpi .l{color:var(--mut);font-size:11px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:3px 6px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:11px}td.l,th.l{text-align:left}td.race{max-width:230px;overflow:hidden;text-overflow:ellipsis}
.pos{color:var(--pos)}.neg{color:var(--neg)}.mut{color:var(--mut)}.foot{color:var(--mut);font-size:11px;margin-top:8px}
.flag{background:var(--warn);border:1px solid var(--warnb);border-radius:6px;padding:4px 8px;margin:4px 0;font-size:12px}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;border:1px solid var(--line)}.ok{color:var(--pos);border-color:var(--pos)}.bad{color:var(--neg);border-color:var(--neg)}
.chart{width:100%;height:auto;display:block}.chart .grid{stroke:var(--line)}.chart .ax{fill:var(--mut);font-size:10px}.chart .bar{fill:var(--acc)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}.legend span{margin-right:12px}.sw{display:inline-block;width:10px;height:3px;vertical-align:middle;margin-right:4px}
"""


def render(D: dict) -> str:
    H, st, V = D["headline"], D["status"], D["volume"]
    hl = st.get("health") or {}
    tzname = time.strftime("%Z")
    o = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
         f'<meta http-equiv="refresh" content="30"><title>SIG Bot Dashboard</title><style>{CSS}</style></head><body>']
    running = (st.get("procs") or 0) > 0
    feed = hl.get("feed", "?")
    o.append(f'<header><h1>SIG Predictions Cup bot</h1>'
             f'<span class="pill {"ok" if running else "bad"}">{"running" if running else "NOT RUNNING"}</span>'
             f'<span title="launchd job com.sizhe.sigbot">launchd: {_e(st.get("launchd"))}</span>'
             f'<span>last cycle {_ago(st.get("cycle_age"))} ago</span>'
             f'<span title="feed state from the last HEALTH line">feed: {_e(feed)}</span>'
             f'<span class="pill {"bad" if st.get("kill_active") else "ok"}" title="kill switch: drawdown &gt; kill_drawdown or 30-min markout &lt; -1 tick; pauses MM/tilt for kill_pause_sec">'
             f'kill switch {"ON until " + _lt(st["kill_until"]) if st.get("kill_active") and st.get("kill_until") else ("ON" if st.get("kill_active") else "off")}</span>'
             f'<span class="mut">last kill {_e(st.get("last_kill") or "never")}</span>'
             f'<span class="mut">updated {_lt(D["now"])} {tzname} · {D["compute_ms"]:.0f} ms CPU · venue read {_ago(H["venue_age"])} ago</span></header><main>')
    for w in D["warnings"]:
        o.append(f'<div class="flag" style="grid-column:1/-1">{_e(w)}</div>')
    if H.get("venue_error"):
        o.append(f'<div class="flag" style="grid-column:1/-1">venue read failed: {_e(H["venue_error"])} (showing last good read)</div>')

    # 1 headline
    w = H["windows"]
    o.append('<section class="wide"><h2>1 · Account</h2><div class="kpis">')
    kp = [("settlement value", _f(H["settlement"]), "", "marked value with arb sets valued at their locked payoff"),
          ("marked value", _f(H["marked"]), "", "venue account value: cash + positions at venue marks"),
          ("P&L since start", _f(H["pnl_start"], sign=True), _cls(H["pnl_start"]), "settlement value - 100,000"),
          ("last 1 h", _f(w["1h"]["settle"], sign=True), _cls(w["1h"]["settle"]), f"settlement basis; marked basis {_f(w['1h']['marked'], sign=True)}"),
          ("last 6 h", _f(w["6h"]["settle"], sign=True), _cls(w["6h"]["settle"]), f"settlement basis; marked basis {_f(w['6h']['marked'], sign=True)}"),
          ("last 24 h", _f(w["24h"]["settle"], sign=True), _cls(w["24h"]["settle"]), f"settlement basis; marked basis {_f(w['24h']['marked'], sign=True)}"),
          ("drawdown (bot)", _f(hl.get("drawdown")), "", "drawdown as the bot measures it, from the last HEALTH line"),
          ("markout30 (bot)", f'{_e(hl.get("markout30", "–"))} (n={hl.get("n_markout", "–")})', "", "the kill-switch markout from the last HEALTH line")]
    for l, v, c, tip in kp:
        o.append(f'<div class="kpi" title="{_e(tip)}"><div class="v {c}">{v}</div><div class="l">{_e(l)}</div></div>')
    o.append('</div>')
    o.append(_svg_lines([D["chart"]["marked"], D["chart"]["settle"]], ["var(--acc)", "var(--acc2)"]))
    o.append('<div class="legend foot"><span><i class="sw" style="background:var(--acc)"></i>marked value (cycles table)</span>'
             '<span><i class="sw" style="background:var(--acc2)"></i>settlement value (rebuilt every 30 min)</span></div>')
    o.append(f'<div class="foot">Source: {_e(H["source"])}. Settlement value = marked value + Σ over arb sets k × (Σ legs\' YES marks − 1) '
             f'= {_f(H["marked"])} + {_f(H["arb_adj"], sign=True)} (same as <code>sigbot.review</code>). Window P&amp;L = marked change '
             '(venue now vs the cycles table at the window start) + change of the arb-set adjustment (positions then rebuilt from later fills, '
             'marks then = last quote mid, else last print, plus each race\'s current venue-minus-mid difference). Before the first cycle (22:42 UTC 21 Oct) the account was 100,000 cash.</div></section>')

    # 2 volume
    o.append('<section><h2>2 · Volume</h2><table><tr><th class="l">window</th><th>ours</th><th>fills</th><th>venue</th><th title="our volume / venue volume">share</th>'
             '<th title="our shares / venue shares">qty share</th><th>pace/day</th><th>vs target</th></tr>')
    for label in ("1h", "24h"):
        v = V["windows"][label]
        warn = " ⚠" if v["flags"] else ""
        o.append(f'<tr><td class="l">last {label}</td><td>{_f(v["ours"])}</td><td>{v["n_fills"]}</td><td>{_f(v["venue"])}</td>'
                 f'<td>{_f(100 * v["share"], 2) + "%" if v["share"] is not None else "–"}{warn}</td>'
                 f'<td>{_f(100 * v["share_qty"], 2) + "%" if v["share_qty"] is not None else "–"}</td>'
                 f'<td>{_f(v["pace"])}</td><td>{_f(100 * v["pace"] / V["target"], 0)}%</td></tr>')
    o.append('</table><h3>by sleeve</h3><table><tr><th class="l">sleeve</th><th>last 1 h</th><th>last 24 h</th><th>24 h share of ours</th></tr>')
    sl = sorted(set(V["windows"]["1h"]["by_sleeve"]) | set(V["windows"]["24h"]["by_sleeve"]), key=lambda s: -V["windows"]["24h"]["by_sleeve"].get(s, 0))
    for s in sl:
        a, b = V["windows"]["1h"]["by_sleeve"].get(s, 0), V["windows"]["24h"]["by_sleeve"].get(s, 0)
        o.append(f'<tr><td class="l">{_e(s)}</td><td>{_f(a)}</td><td>{_f(b)}</td><td>{_f(100 * b / (V["windows"]["24h"]["ours"] or 1), 0)}%</td></tr>')
    o.append('</table>')
    for label in ("1h", "24h"):
        fl = V["windows"][label]["flags"]
        if fl:
            o.append(f'<div class="flag">⚠ tape may be incomplete over the last {label} ({_e("; ".join(fl))}): venue volume is then understated and our share overstated</div>')
    w24 = V["windows"]["24h"]
    o.append(_svg_bars(V["hourly"]))
    o.append(f'<div class="foot">Our volume = Σ quantity × price paid on the side bought (YES fill: price, NO fill: 1 − price), the leaderboard definition '
             f'(<code>fill_volume_mode=side_cost</code>). Venue volume = the same summed over both counterparties of every print in <code>tape</code> '
             f'= Σ print quantity. Qty share = our shares / venue shares. Pace = window volume × 24 / window hours (24 h window capped at the {w24["hours"]:.1f} h since our first fill); '
             f'target {_f(V["target"])}/day. Tape check: {w24["fills_in_tape"][0]}/{w24["fills_in_tape"][1]} of our fills since the realtime feed started are in the tape; '
             f'{w24["empty_minutes"]} minutes in 24 h had no print (longest {w24["longest_gap"]} min). Bars: our volume per hour (hover for the venue share).</div></section>')

    # 3 recent fills
    o.append(f'<section><h2>3 · Most recent fills</h2><table><tr><th class="l">time {tzname}</th><th class="l">market</th><th class="l">sleeve</th><th class="l">side</th>'
             '<th>price</th><th>qty</th><th>volume</th><th title="ticks in our favour">mk 5m</th><th>mk 30m</th></tr>')
    for r in D["recent"]:
        o.append(f'<tr><td class="l">{_lt(r["t"])}</td><td class="l race" title="{_e(r["race"])} ({r["ex"]})">{_e(r["party"])} · {_e(r["race"])}</td>'
                 f'<td class="l">{_e(r["sleeve"])}</td><td class="l">{_e(r["side"])}</td><td>{r["price"]:.3f}</td><td>{_f(r["q"])}</td><td>{_f(r["vol"])}</td>'
                 f'<td class="{_cls(r["mk5"])}">{_f(r["mk5"], 1, True) if r["mk5"] is not None else "<span class=mut>pending</span>"}</td>'
                 f'<td class="{_cls(r["mk30"])}">{_f(r["mk30"], 1, True) if r["mk30"] is not None else "<span class=mut>pending</span>"}</td></tr>')
    o.append('</table><div class="foot">Price is YES-denominated; side is the position side bought (NO = sold YES). Markout = reference mid N minutes after the fill '
             'minus the fill price, signed in our favour, in ticks of 0.005; reference = first quote snapshot at or after fill + N, else the first public print '
             '(as <code>sigbot.review</code>).</div></section>')

    # 4 P&L by sleeve and market
    o.append('<section class="wide"><h2>4 · P&amp;L by sleeve and market</h2><div class="two"><div><h3>by sleeve (marked: realised + open at marks)</h3>'
             '<table><tr><th class="l">sleeve</th><th>fills</th><th>P&amp;L</th><th title="Σ q × (mid at fill − price)">entry edge</th>'
             '<th title="Σ q × (mark now − mid at fill)">move since</th><th>24 h P&amp;L</th><th>24 h edge</th><th>24 h move</th></tr>')
    PA, P24 = D["pnl_sleeves"]["all"], D["pnl_sleeves"]["24h"]
    for s in sorted(PA, key=lambda s: PA[s]["pnl"]):
        a, b = PA[s], P24.get(s, {"pnl": 0, "edge": 0, "move": 0})
        extra = f' <span class="mut" title="arb sets at locked value">(settle {_f(a["pnl"] + a["settle_extra"], 0, True)})</span>' if "settle_extra" in a else ""
        o.append(f'<tr><td class="l">{_e(s)}</td><td>{a["n"]}</td><td class="{_cls(a["pnl"])}">{_f(a["pnl"], 0, True)}{extra}</td>'
                 f'<td class="{_cls(a["edge"])}">{_f(a["edge"], 0, True)}</td><td class="{_cls(a["move"])}">{_f(a["move"], 0, True)}</td>'
                 f'<td class="{_cls(b["pnl"])}">{_f(b["pnl"], 0, True)}</td><td>{_f(b["edge"], 0, True)}</td><td>{_f(b["move"], 0, True)}</td></tr>')
    tot = sum(a["pnl"] for a in PA.values())
    o.append(f'<tr><td class="l"><b>total</b></td><td></td><td class="{_cls(tot)}"><b>{_f(tot, 0, True)}</b></td><td colspan=5 class="l mut">'
             f'vs marked P&amp;L since start {_f(H["marked_pnl_start"], 0, True)}</td></tr></table></div>')

    def mtable(title, rows_):
        t = [f'<div><h3>{title}</h3><table><tr><th class="l">market</th><th class="l">sleeve</th><th>fills</th><th>P&amp;L</th><th>edge</th><th>move</th><th>position</th></tr>']
        for r in rows_:
            t.append(f'<tr><td class="l race" title="{_e(r["race"])} ({r["ex"]})">{_e(r["party"])} · {_e(r["race"])}</td><td class="l">{_e(r["sleeve"])}</td><td>{r["n"]}</td>'
                     f'<td class="{_cls(r["pnl"])}">{_f(r["pnl"], 0, True)}</td><td>{_f(r["edge"], 0, True)}</td><td>{_f(r["move"], 0, True)}</td><td>{_f(r["pos"])}</td></tr>')
        t.append('</table></div>')
        return "".join(t)
    M = D["markets"]
    o.append(mtable("worst 10 · 24 h", M["24_worst"]) + mtable("best 10 · 24 h", M["24_best"]) + '</div><div class="two">'
             + mtable("worst 10 · since start", M["all_worst"]) + mtable("best 10 · since start", M["all_best"]))
    o.append('</div><h3>consistently losing (≥ 10 fills, ≥ 60 % with negative 5-min markout)</h3>')
    if D["losing"]:
        o.append('<table><tr><th class="l">market</th><th class="l">sleeve responsible</th><th>fills</th><th>% negative</th><th>mean mk 5m</th><th>markout $</th><th>P&amp;L since start</th></tr>')
        for r in D["losing"]:
            o.append(f'<tr><td class="l race">{_e(r["party"])} · {_e(r["race"])}</td><td class="l">{_e(r["sleeve"])}</td><td>{r["n"]}</td><td>{100 * r["neg_pct"]:.0f}%</td>'
                     f'<td class="neg">{_f(r["mean"], 1, True)}</td><td class="{_cls(r["usd"])}">{_f(r["usd"], 0, True)}</td><td class="{_cls(r["pnl"])}">{_f(r["pnl"], 0, True)}</td></tr>')
        o.append('</table>')
    else:
        near = ", ".join(f'{x["ex"]} ({x["n"]} fills, {100 * x["neg_pct"]:.0f}% negative)' for x in D["losing_near"])
        o.append(f'<p class="mut">none yet. Closest markets with ≥ 5 fills: {_e(near or "–")}</p>')
    o.append('<div class="foot">Per fill, P&amp;L = q × (mark − price) with q the signed YES quantity (NO fills negative); summed over a market this is realised plus '
             'open P&amp;L. It splits into entry edge = q × (mid at fill − price) and move since = q × (mark − mid at fill); mid at fill = last quote snapshot '
             'within 10 min before the fill, else the bot\'s mid at placement, else the previous public print. Mark = venue mark for held positions, else last quote '
             'mid, else last print. 24 h market P&amp;L adds the position held 24 h ago × (mark now − mark then). Arb and hedge markets (set legs) are left out of '
             'the rankings because per-leg marks say nothing about the locked set value (see section 6). Sleeve = the sleeve with most shares filled in that market; '
             '"sleeve responsible" = the sleeve with the most negative markout $ there (ticks × 0.005 × quantity). Losing list uses every non-arb, non-hedge fill.</div></section>')

    # 5 markouts
    o.append('<section><h2>5 · Markouts (ticks)</h2><table><tr><th class="l">sleeve</th><th>5m · 1 h</th><th>5m · 24 h</th><th>5m · all</th><th>30m · 1 h</th><th>30m · 24 h</th><th>30m · all</th></tr>')
    MK = D["markouts"]
    for s in MARKOUT_SLEEVES:
        cells = []
        for h in ("5m", "30m"):
            for wn in ("1h", "24h", "all"):
                x = MK.get(f"{s}|{h}|{wn}")
                cells.append(f'<td class="{_cls(x["mean"]) if x else ""}">{_f(x["mean"], 2, True) if x else "–"}<span class="mut"> {"n=" + str(x["n"]) if x else ""}</span></td>')
        o.append(f'<tr><td class="l">{s}</td>{"".join(cells)}</tr>')
    o.append('</table><div class="foot">Mean markout per fill (not size-weighted) in ticks of 0.005, signed in our favour; window = fill time within the last 1 h / 24 h; '
             'a 30-min markout exists only for fills at least 30 min old, so the 1 h column covers fills 30–60 min old. "all" matches <code>sigbot.review</code>\'s markouts.</div></section>')

    # 6 positions
    P = D["positions"]
    capu = abs(P["net_d"]) / P["cap"] if P["cap"] else 0
    o.append(f'<section><h2>6 · Positions</h2><div class="kpis"><div class="kpi" title="net D-notional ex arb sets, as sigbot/risk.py"><div class="v {"neg" if capu > 0.9 else ""}">{_f(P["net_d"], 0, True)}</div>'
             f'<div class="l">net partisan (D +) · cap ±{_f(P["cap"])} · {100 * capu:.0f}% used · bot says {_f(P["health_netD"], 0, True)}</div></div>'
             f'<div class="kpi"><div class="v">{P["n"]}</div><div class="l">open positions · gross {_f(P["gross"])}</div></div></div>')
    o.append('<h3>largest holdings by notional</h3><table><tr><th class="l">market</th><th class="l">sleeve</th><th>qty</th><th>mark</th><th>value</th><th>cost</th><th>open P&amp;L</th></tr>')
    for r in P["top"]:
        u = (r["mv"] - r["cost"]) if r["mv"] is not None and r["cost"] is not None else None
        tag = f' <span class="mut">(set ×{r["in_set"]:.0f})</span>' if r["in_set"] else ""
        o.append(f'<tr><td class="l race" title="{_e(r["race"])} ({r["ex"]})">{_e(r["party"])} · {_e(r["race"])}</td><td class="l">{_e(r["sleeve"])}{tag}</td>'
                 f'<td>{_f(r["q"])} {"YES" if r["q"] > 0 else "NO"}</td><td>{_f(r["mark"], 3)}</td><td>{_f(r["mv"])}</td><td>{_f(r["cost"])}</td><td class="{_cls(u)}">{_f(u, 0, True)}</td></tr>')
    o.append('</table><h3>arb sets</h3><table><tr><th class="l">race</th><th>legs</th><th>sets k</th><th>cost</th><th>locked profit</th><th>marked P&amp;L</th><th title="Σ YES marks − 1">overround</th></tr>')
    tl = 0.0
    for s in P["sets"]:
        tl += s["locked"] or 0
        o.append(f'<tr><td class="l race">{_e(s["race"])}</td><td>{s["legs"]}</td><td>{_f(s["k"])}</td><td>{_f(s["cost"])}</td><td class="{_cls(s["locked"])}">{_f(s["locked"], 0, True)}</td>'
                 f'<td class="{_cls(s["marked_pnl"])}">{_f(s["marked_pnl"], 0, True)}</td><td>{_f(s["overround"], 3, True)}</td></tr>')
    o.append(f'<tr><td class="l"><b>total</b></td><td></td><td></td><td></td><td><b>{_f(tl, 0, True)}</b></td><td></td><td></td></tr></table>'
             '<div class="foot">Value = venue market value; mark = YES-denominated. An arb set = NO on every leg of a race; k = the common NO quantity; locked profit = '
             '(legs − 1) × k − cost of k sets (pro rata of each leg\'s cost basis), as <code>sigbot.review</code>. Net partisan = Σ D-legs signed notional − Σ R-legs '
             '(long YES q × mid, NO q × (1 − mid)) after removing arb sets, Independent legs ignored; the cap is <code>portfolio_max_partisan_notional</code>.</div></section>')

    # 7 events
    o.append('<section><h2>7 · Events</h2><p class="mut">last seen: ' + " · ".join(f'{k} {_e((v or "never")[11:] if v else "never")}' for k, v in D["event_last"].items()) + '</p>'
             '<table><tr><th class="l">time (local)</th><th class="l">event</th><th class="l">detail</th></tr>')
    for t, k, txt in D["events"]:
        o.append(f'<tr><td class="l">{_e(t[5:])}</td><td class="l">{_e(k)}</td><td class="l" style="white-space:normal">{_e(txt[:220])}</td></tr>')
    o.append('</table><div class="foot">From <code>data/live.log</code> (local time) and <code>data/auto_tuning.log</code>: KILL SWITCH, RETRACE PAUSE, APPLIED '
             '(self-tuner, allocator and shadow changes, plus DROPPED), OFFLOAD (capital recycling), LAG (lag follower enters and exits). Last 12 of each kind.</div></section>')

    if D["discrepancies"]:
        o.append('<section class="wide"><h2>Data-source checks</h2>' + "".join(f'<div class="flag">{_e(x)}</div>' for x in D["discrepancies"]) + '</section>')
    o.append('</main></body></html>')
    return "".join(o)


# ------------------------------------------------------------------------------------------ server
def serve(dash: Dashboard, host: str, port: int, every: float):
    lock = threading.Lock()

    def loop():
        while True:
            try:
                with lock:
                    dash.compute()
            except Exception as e:  # keep serving the last good page
                dash.html = dash.html.replace("</main>", f'<div class="flag">refresh failed: {_e(repr(e))}</div></main>', 1)
                print(time.strftime("%H:%M:%S"), "refresh failed:", repr(e), file=sys.stderr, flush=True)
            time.sleep(every)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/json"):
                body, ctype = json.dumps(dash.data, default=str).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = dash.html.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), H)
    print(f"dashboard on http://{host}:{port}  (refresh every {every:.0f} s)", flush=True)
    srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="write data/dashboard.html and exit")
    ap.add_argument("--json", action="store_true", help="print the computed numbers as JSON and exit")
    ap.add_argument("--no-api", action="store_true", help="no venue reads (account value from the cycles table)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--every", type=float, default=45, help="seconds between recomputes (default 45)")
    ap.add_argument("--venue-every", type=float, default=60, help="seconds between venue reads (2 reads each; default 60)")
    args = ap.parse_args(argv)
    try:
        os.nice(10)
    except OSError:
        pass
    dash = Dashboard(Venue(args.venue_every, enabled=not args.no_api))
    if args.once or args.json:
        D = dash.compute()
        if args.json:
            print(json.dumps(D, default=str, indent=1))
        else:
            out = os.path.join(HERE, "data", "dashboard.html")
            with open(out, "w") as f:
                f.write(dash.html)
            print(f"wrote {out} ({dash.compute_ms:.0f} ms CPU, {dash.venue.reads} venue reads)")
        return 0
    serve(dash, args.host, args.port, args.every)
    return 0


if __name__ == "__main__":
    sys.exit(main())
