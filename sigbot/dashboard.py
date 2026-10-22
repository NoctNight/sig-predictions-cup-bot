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
RECYCLE_RE = re.compile(r"RECYCLE (.+?): unwind (\d+) sets at proceeds ([\d.]+)/set \(forgone ret (-?[\d.]+)%\) to fund (.+?) (sell_set|buy_set) "
                        r"at (-?[\d.]+)% \(gain (-?[\d.]+)%\)")
FREE_RE = re.compile(r"FREE UNWIND (.+?): (\d+) sets at proceeds ([\d.]+)/set")
OFFLOAD_RE = re.compile(r"OFFLOAD( \(report-only\))?: exit (.+?) (\S+) x(\d+) \(gives up (-?[\d.]+)%\) to fund (.+?) \((-?[\d.]+)% net\), gain (-?[\d.]+)%")
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
        self.recycle: list = []            # parsed RECYCLE / FREE UNWIND / OFFLOAD lines (epoch from local log time)
        self.basket_fail_lines = 0         # "not filled" lines for all-or-none baskets (cross-check of arb_trades ok=0)

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

    def _recycle_line(self, line: str):
        try:
            t = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return
        m = RECYCLE_RE.search(line)
        if m:
            self.recycle.append({"kind": "RECYCLE", "t": t, "key": m.group(1), "qty": int(m.group(2)), "proceeds": float(m.group(3)),
                                 "forgone": float(m.group(4)) / 100, "fund": m.group(5), "fund_kind": m.group(6),
                                 "new": float(m.group(7)) / 100, "gain": float(m.group(8)) / 100})
            return
        m = FREE_RE.search(line)
        if m:
            self.recycle.append({"kind": "FREE", "t": t, "key": m.group(1), "qty": int(m.group(2)), "proceeds": float(m.group(3))})
            return
        m = OFFLOAD_RE.search(line)
        if m and not m.group(1):
            self.recycle.append({"kind": "OFFLOAD", "t": t, "key": m.group(2), "side": m.group(3), "qty": int(m.group(4)),
                                 "forgone": float(m.group(5)) / 100, "fund": m.group(6), "new": float(m.group(7)) / 100,
                                 "gain": float(m.group(8)) / 100})

    def update(self):
        for line in self._lines("live"):
            if "RECYCLE " in line or "FREE UNWIND " in line or "OFFLOAD" in line:
                self._recycle_line(line)
            if "not filled atomically" in line or "recycle " in line and " not filled:" in line or "free unwind " in line and " not filled:" in line:
                self.basket_fail_lines += 1
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

    def _recycle(self, db, now, cfg, pos, race_legs, leg, have_venue) -> dict:
        """Arb entries, recycle unwinds, free unwinds and offload exits, matched to their log lines for the percentages."""
        lines = self.logs.recycle
        used = set()

        def match(kind, key, qty, t, tol=15):
            for i in range(len(lines) - 1, -1, -1):
                l = lines[i]
                if i not in used and l["kind"] == kind and l["key"] == key and l["qty"] == qty and abs(l["t"] - t) <= tol:
                    used.add(i)
                    return l
            return None

        arb = [dict(r) for r in db.execute("SELECT id, ts, race, kind, quantity, edge_per_set, cost_per_set, ok, "
                                           "(response LIKE '%dry_run%') dry FROM arb_trades ORDER BY ts")]
        arb = [a for a in arb if not a["dry"]]
        entries = [a for a in arb if a["ok"] and a["kind"] in ("sell_set", "buy_set")]
        ev = []
        for a in arb:
            if a["kind"] != "unwind_set":
                continue
            q, cap_ = a["quantity"], a["quantity"] * a["cost_per_set"]
            l = match("RECYCLE", a["race"], q, a["ts"])
            if l:
                follow = next((e for e in entries if e["race"] == l["fund"] and 0 <= e["ts"] - a["ts"] <= 180), None)
                ev.append({"t": a["ts"], "type": "recycle", "ok": bool(a["ok"]), "what": f'{a["race"]} → {l["fund"]}', "qty": q,
                           "capital": cap_, "given_up": -a["edge_per_set"] * q, "new_edge": cap_ * l["new"], "net": cap_ * l["gain"],
                           "forgone_pct": l["forgone"], "new_pct": l["new"], "gain_pct": l["gain"],
                           "follow": (follow["quantity"], follow["quantity"] * follow["edge_per_set"]) if follow else None})
                continue
            free = match("FREE", a["race"], q, a["ts"])
            ev.append({"t": a["ts"], "type": "free unwind" if free else "unwind (no log line)", "ok": bool(a["ok"]), "what": a["race"],
                       "qty": q, "capital": cap_, "given_up": -a["edge_per_set"] * q, "new_edge": None, "net": None})
        exits = [dict(r) for r in db.execute("SELECT order_id, exchange_id, side, quantity, placed_at FROM orders WHERE purpose='exit'")]
        filled = defaultdict(lambda: [0.0, 0.0])
        for r in db.execute("SELECT order_id, side, price, quantity FROM fills WHERE purpose='exit' AND price IS NOT NULL"):
            filled[r["order_id"]][0] += abs(r["quantity"])
            filled[r["order_id"]][1] += abs(r["quantity"]) * (r["price"] if r["side"] == "no" else 1 - r["price"])
        n_other = 0
        for o in exits:
            l = match("OFFLOAD", o["exchange_id"], o["quantity"], o["placed_at"])
            if not l:
                n_other += 1
                continue
            fq, proceeds = filled.get(o["order_id"], (0.0, 0.0))
            race, party = leg.get(o["exchange_id"], (o["exchange_id"], "?"))
            ev.append({"t": o["placed_at"], "type": "offload exit", "ok": fq > 0, "what": f'{party} · {race} → {_name(leg, l["fund"])}', "qty": fq,
                       "ordered": o["quantity"], "capital": proceeds, "given_up": proceeds * l["forgone"], "new_edge": proceeds * l["new"],
                       "net": proceeds * l["gain"], "forgone_pct": l["forgone"], "new_pct": l["new"], "gain_pct": l["gain"]})
        ev.sort(key=lambda e: e["t"])

        def summ(since):
            out = {"entries": sum(1 for e in entries if e["ts"] >= since),
                   "entry_capital": sum(e["quantity"] * e["cost_per_set"] for e in entries if e["ts"] >= since),
                   "entry_edge": sum(e["quantity"] * e["edge_per_set"] for e in entries if e["ts"] >= since)}
            for typ in ("recycle", "free unwind", "offload exit", "unwind (no log line)"):
                xs = [e for e in ev if e["type"] == typ and e["ok"] and e["t"] >= since]
                out[typ] = {"n": len(xs), "capital": sum(e["capital"] for e in xs), "given_up": sum(e["given_up"] for e in xs),
                            "new_edge": sum(e["new_edge"] or 0 for e in xs), "net": sum(e["net"] or 0 for e in xs)}
            out["failed"] = sum(1 for a in arb if not a["ok"] and a["ts"] >= since)
            return out
        windows = {"1h": summ(now - 3600), "24h": summ(now - 86400), "all": summ(0)}
        hours = []
        h0 = (int(now) // 3600) * 3600
        for i in range(23, -1, -1):
            a_ = h0 - i * 3600
            hours.append((a_, sum(1 for a in arb if not a["ok"] and a_ <= a["ts"] < a_ + 3600)))
        # capital locked, as the bot counts it (run.py): every leg's cost basis in races where we hold NO on all legs
        ac = cfg.get("arb", {})
        start = cfg.get("starting_cash", 100000.0)
        ceiling = ac.get("capital_frac", 0.9) * start
        race_max = ac.get("max_race_frac", 0.15) * start
        alloc = _load_json(os.path.join(HERE, "params", "allocation.json"), {}).get("races") or {}
        caps = {k: float(v) for k, v in (alloc.get("caps") or {}).items()}
        default = float(alloc.get("default_cap", 2000.0))
        V = self.venue
        locked = []
        for race, legs in race_legs.items():
            qs = [pos.get(ex, 0.0) for ex in legs]
            if not legs or not all(q < 0 for q in qs):
                continue
            committed = sum(float(V.positions.get(ex, {}).get("costBasis") or 0.0) for ex in legs) if have_venue else None
            lim = min(race_max, max(caps.get(race, default), default)) if caps else race_max
            locked.append({"race": race, "committed": committed, "cap": caps.get(race), "limit": lim, "k": min(-q for q in qs)})
        locked.sort(key=lambda r: -(r["committed"] or 0))
        tot = sum(r["committed"] or 0 for r in locked) if have_venue else None
        return {"windows": windows, "events": ev[::-1][:10], "fail_hours": hours, "locked": locked, "locked_total": tot,
                "ceiling": ceiling, "race_max": race_max, "default_cap": default, "caps_loaded": bool(caps),
                "other_exits": n_other, "log_fail_lines": self.logs.basket_fail_lines,
                "unmatched_log": sum(1 for i, l in enumerate(lines) if i not in used and l["kind"] in ("RECYCLE", "FREE"))}

    # ------------------------------------------------------------------ main
    def compute(self) -> dict:
        now = time.time()
        self.venue.refresh(now)
        t0 = time.process_time()
        for key in [k for k in self.mark_cache if k[1] < now - 90000]:
            del self.mark_cache[key]
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
            D["discrepancies"].append(f"arb sets are worth {adj_v:,.0f} more than at market at the venue's marks and {adj_m:,.0f} at our quote mids "
                                      "(the venue's marks are neither mids nor last prints)")

        fills_after = sorted(fills, key=lambda f: (f["t"], f["id"]))

        def pos_at(t):
            """Positions at t from the fills log alone (so a venue snapshot taken mid-trade cannot skew the past)."""
            p = defaultdict(float)
            for f in fills_after:
                if f["t"] > t:
                    break
                p[f["ex"]] += f["q"]
            return dict(p)

        def tv_at(t):
            if first_cycle is None or t < first_cycle:
                return START_VALUE
            r = db.execute("SELECT total_value FROM cycles WHERE ts<=? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
            return r["total_value"]

        def cash_at(t):
            """Cash at t: 100,000 plus every fill's cash flow up to t. A fill against an opposite-side holding nets:
            each closed pair redeems for 1. Reproduces the venue's cash (account value - holdings value) to the cent."""
            p, cash = defaultdict(float), START_VALUE
            for f in fills_after:
                if f["t"] > t:
                    break
                q, before = f["q"], p[f["ex"]]
                if q > 0:
                    cash += -q * f["price"] + (min(q, -before) if before < 0 else 0.0)
                else:
                    cash += q * (1 - f["price"]) + (min(-q, before) if before > 0 else 0.0)
                p[f["ex"]] = before + q
            return cash

        if have_venue and V.pnl.get("totalHoldingsValue") is not None:
            gap = cash_at(now) - (tv_now - float(V.pnl["totalHoldingsValue"]))
            if abs(gap) > 1:
                D["discrepancies"].append(f"cash rebuilt from the fills log differs from the venue's by {gap:,.2f} "
                                          "(fills not yet logged); past values use the fills log")

        def settle_at(t, cache=False):
            """Settlement value at t rebuilt from cash and positions at t: arb sets at their payoff, everything else at
            our marks then (last quote mid <= 15 min old, else last print). Does not depend on a fresh cycle row, so a
            window that starts while the bot was down is still valued at t, not at the last cycle before the outage."""
            if first_cycle is None or t < first_cycle:
                return START_VALUE, START_VALUE
            p = {ex: q for ex, q in pos_at(t).items() if abs(q) > 1e-9}
            for race, legs in race_legs.items():
                qs = [p.get(ex, 0.0) for ex in legs]
                if len(legs) >= 2 and all(q < 0 for q in qs):
                    k = min(-q for q in qs)
                    for ex in legs:
                        p[ex] += k
                    p[("set", race)] = (len(legs) - 1) * k
            val = cash_at(t)
            for ex, q in p.items():
                if isinstance(ex, tuple):
                    val += q
                    continue
                if abs(q) < 1e-9:
                    continue
                key = (ex, t)
                if key not in self.mark_cache:
                    self.mark_cache[key] = self._mark_at(db, ex, t)
                m = self.mark_cache[key]
                if m is None:
                    m = mark.get(ex, 0.5)
                val += q * m if q > 0 else -q * (1 - m)
            if not cache:
                for key in [k for k in self.mark_cache if k[1] == t]:
                    del self.mark_cache[key]
            return val, tv_at(t)

        cycle_gaps, prev = [], None
        for (ts,) in db.execute("SELECT ts FROM cycles WHERE ts>=? ORDER BY ts", (now - 86400 - 600,)):
            if prev is not None and ts - prev > 300:
                cycle_gaps.append((prev, ts))
            prev = ts
        win = {}
        for label, secs in (("1h", 3600), ("6h", 21600), ("24h", 86400)):
            s_then, tv_then = settle_at(now - secs)
            down = [(a, b) for a, b in cycle_gaps if b > now - secs]
            win[label] = {"settle": settle_now - s_then, "marked": tv_now - tv_then, "start_value": s_then,
                          "bot_down_min": sum(min(b, now) - max(a, now - secs) for a, b in down) / 60}
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
            ids = [str(f["id"]) for f in ours]
            matched = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                matched += db.execute(f"SELECT COUNT(*) FROM tape WHERE trade_id IN ({','.join('?' * len(chunk))})", chunk).fetchone()[0]
            down = sum(min(b, now) - max(a, since) for a, b in bot_down if b > since)
            flags = []
            if ids and matched / len(ids) < 0.97:
                flags.append(f"only {matched}/{len(ids)} of our fills in the window appear in the tape")
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
        plist_all = plist
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
        D["positions"] = {"top": plist[:15], "all": plist_all, "n": len(plist), "sets": sets_out, "net_d": netd, "cap": cap,
                          "health_netD": self.logs.health["netD"] if self.logs.health else None,
                          "gross": sum(r["mv"] or 0 for r in plist)}

        # ---- arb recycling
        D["recycle"] = self._recycle(db, now, cfg, pos, race_legs, leg, have_venue)

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


def _name(leg, key):
    if key in leg:
        return f"{leg[key][1]} · {leg[key][0]}"
    return key


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


SLEEVE_NAMES = {"mm": "Market making", "arb": "Arb sets", "tilt": "Tilt", "direction": "Direction", "inside": "Inside quotes",
                "exit": "Exits", "hedge": "Hedges", "?": "Unknown"}
EVENT_NAMES = {"KILL SWITCH": "Kill switch", "RETRACE PAUSE": "Quote side paused", "APPLIED": "Setting changed",
               "OFFLOAD": "Capital moved", "LAG": "Lag-follower trade"}


def _sl(s):
    return SLEEVE_NAMES.get(s, s)


def _mkt(party, race):
    return f'<span class="pty p{_e(party)}">{_e(party)}</span> {_e(race)}'


def _chart_value(D, w=760, h=220):
    """Account value over the last 24 h: two lines, crosshair tooltip on hover."""
    settle, marked = D["chart"]["settle"], D["chart"]["marked"]
    pts = settle + marked
    if len(settle) < 2:
        return ""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    lo, hi = min(ys), max(ys)
    step = max(250, round((hi - lo) / 4 / 250) * 250)
    y0, y1 = (lo // step) * step, (hi // step + 1) * step
    L, R, T, B = 58, 92, 12, 26
    sx = lambda x: L + (x - x0) / max(1, x1 - x0) * (w - L - R)
    sy = lambda y: T + (y1 - y) / max(1e-9, y1 - y0) * (h - T - B)
    o = [f'<div class="chart" data-chart><svg viewBox="0 0 {w} {h}" role="img" aria-label="Account value over the last 24 hours">']
    y = y0
    while y <= y1 + 1e-9:
        o.append(f'<line class="gridl" x1="{L}" x2="{w - R}" y1="{sy(y):.1f}" y2="{sy(y):.1f}"/>'
                 f'<text class="ax" x="{L - 8}" y="{sy(y) + 4:.1f}" text-anchor="end">{y / 1000:,.1f}k</text>')
        y += step
    for t in range(int(x0 // 21600 + 1) * 21600, int(x1) + 1, 21600):
        o.append(f'<text class="ax" x="{sx(t):.1f}" y="{h - 6}" text-anchor="middle">{time.strftime("%H:%M", time.localtime(t))}</text>')
    for s, cls in ((marked, "ln-m"), (settle, "ln-s")):
        d = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in s)
        o.append(f'<polyline class="{cls}" points="{d}"/>')
        ex, ey = s[-1]
        o.append(f'<circle class="dot {cls}" cx="{sx(ex):.1f}" cy="{sy(ey):.1f}" r="4"/>'
                 f'<text class="endlab" x="{sx(ex) + 9:.1f}" y="{sy(ey) + 4:.1f}">{ey:,.0f}</text>')
    # hover columns: one per settlement point, nearest marked value alongside
    for i, (t, v) in enumerate(settle):
        a = sx(settle[i - 1][0]) if i else L
        b = sx(settle[i + 1][0]) if i + 1 < len(settle) else w - R
        lft, rgt = (a + sx(t)) / 2 if i else L, (sx(t) + b) / 2 if i + 1 < len(settle) else w - R
        mv = min(marked, key=lambda p: abs(p[0] - t))[1]
        tip = f'{time.strftime("%a %H:%M", time.localtime(t))}|If locked arbs pay out|{v:,.0f}|At market prices|{mv:,.0f}'
        o.append(f'<rect class="hit" x="{lft:.1f}" y="{T}" width="{max(1, rgt - lft):.1f}" height="{h - T - B}" data-tip="{_e(tip)}" data-x="{sx(t):.1f}"/>')
    o.append(f'<line class="xhair" x1="0" x2="0" y1="{T}" y2="{h - B}" visibility="hidden"/></svg></div>')
    return "".join(o)


def _chart_cols(rows, w=760, h=170, cls="b-in", fmt=lambda v: f"{v:,.0f}", tipf=None, label=""):
    """Hourly columns with a per-column tooltip. rows: [(epoch_hour, value, extra)]."""
    if not rows:
        return ""
    mx = max(v for _, v, *_ in rows) or 1
    L, R, T, B = 46, 8, 14, 24
    bw = (w - L - R) / len(rows)
    cw = min(24, bw - 2)
    o = [f'<div class="chart" data-chart><svg viewBox="0 0 {w} {h}" role="img" aria-label="{_e(label)}">',
         f'<line class="gridl" x1="{L}" x2="{w - R}" y1="{T}" y2="{T}"/><text class="ax" x="{L - 6}" y="{T + 4}" text-anchor="end">{fmt(mx)}</text>',
         f'<line class="basel" x1="{L}" x2="{w - R}" y1="{h - B}" y2="{h - B}"/><text class="ax" x="{L - 6}" y="{h - B + 4}" text-anchor="end">0</text>']
    for i, r in enumerate(rows):
        a, v = r[0], r[1]
        bh = v / mx * (h - T - B)
        x = L + i * bw + (bw - cw) / 2
        if bh > 0:
            rr = min(4, bh)
            o.append(f'<path class="{cls}" d="M{x:.1f},{h - B} v{-(bh - rr):.1f} q0,{-rr:.1f} {rr:.1f},{-rr:.1f} h{cw - 2 * rr:.1f} '
                     f'q{rr:.1f},0 {rr:.1f},{rr:.1f} v{bh - rr:.1f} z"/>')
        tip = tipf(r) if tipf else f'{time.strftime("%H:00", time.localtime(a))}|value|{fmt(v)}'
        o.append(f'<rect class="hit" x="{L + i * bw:.1f}" y="{T}" width="{bw:.1f}" height="{h - T - B}" data-tip="{_e(tip)}"/>')
        if i % 6 == 0:
            o.append(f'<text class="ax" x="{x + cw / 2:.1f}" y="{h - 6}" text-anchor="middle">{time.strftime("%H:00", time.localtime(a))}</text>')
    o.append("</svg></div>")
    return "".join(o)


def _bars(rows, total=None, signed=True):
    """Ranked horizontal bars: rows of (label_html, value, note). Diverging around zero when signed."""
    if not rows:
        return '<p class="muted">Nothing yet.</p>'
    mx = max(abs(v) for _, v, _ in rows) or 1
    o = ['<div class="bars">']
    for lab, v, note in rows:
        pct = abs(v) / mx * (50 if signed else 100)
        if signed:
            fill = (f'<span class="fill {"gain" if v >= 0 else "loss"}" style="{"left:50%" if v >= 0 else "right:50%"};width:{pct:.1f}%"></span>'
                    '<span class="mid"></span>')
        else:
            fill = f'<span class="fill b-in" style="left:0;width:{pct:.1f}%"></span>'
        o.append(f'<div class="brow"><span class="nm">{lab}</span><span class="track">{fill}</span>'
                 f'<span class="val">{_f(v, 0, signed)}{f" <span class=pc>{note}</span>" if note else ""}</span></div>')
    if total is not None:
        o.append(f'<div class="total"><span>Total</span><span class="val">{_f(total, 0, signed)}</span></div>')
    o.append("</div>")
    return "".join(o)


def _tile(label, value, sub="", cls="", tip=""):
    t = f' title="{_e(tip)}"' if tip else ""
    return (f'<div class="tile"{t}><div class="lb">{label}</div><div class="v {cls}">{value}</div>'
            + (f'<div class="d">{sub}</div>' if sub else "") + "</div>")


def _meter(frac, warn=0.8, bad=1.0):
    f = max(0.0, min(frac, 1.0))
    c = "loss" if frac >= bad else ("warn" if frac >= warn else "ok")
    return f'<div class="meter"><span class="{c}" style="width:{100 * f:.1f}%"></span></div>'


def _pct(x, nd=1):
    return "–" if x is None else f"{100 * x:.{nd}f}%"


CSS = """
:root{
  --paper:#f1f4ef;--sheet:#fcfdfb;--sheet-2:#f4f7f2;--rule:#dfe5db;--rule-2:#c8d0c3;
  --ink:#141a16;--ink-2:#4a544d;--ink-3:#6f7972;
  --accent:#17694a;--accent-wash:#e2eee6;--in:#2a78d6;--set:#0f8a5a;
  --gain:#0b7a3b;--loss:#c23a32;--warn:#8f6200;--warn-wash:#f8efd6;--loss-wash:#f9e3e1;
  --grid:#e5e9e2;--tip-bg:#141a16;--tip-ink:#f1f4ef;
  --pD:#2a78d6;--pR:#d24a3c;--pI:#7c867f;
  --f-display:"Schibsted Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  --f-body:"Instrument Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  --f-fig:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --paper:#0e1210;--sheet:#161b18;--sheet-2:#1b211d;--rule:#27302a;--rule-2:#36403a;
  --ink:#edf1ee;--ink-2:#a9b3ac;--ink-3:#8b958e;
  --accent:#4fc08d;--accent-wash:#183024;--in:#3987e5;--set:#3daa78;
  --gain:#45c274;--loss:#f07b72;--warn:#e2b64d;--warn-wash:#2a2414;--loss-wash:#2e1a18;
  --grid:#222a25;--tip-bg:#edf1ee;--tip-ink:#0e1210;--pD:#3987e5;--pR:#e66767;--pI:#8b958e;color-scheme:dark}}
:root[data-theme="dark"]{
  --paper:#0e1210;--sheet:#161b18;--sheet-2:#1b211d;--rule:#27302a;--rule-2:#36403a;
  --ink:#edf1ee;--ink-2:#a9b3ac;--ink-3:#8b958e;
  --accent:#4fc08d;--accent-wash:#183024;--in:#3987e5;--set:#3daa78;
  --gain:#45c274;--loss:#f07b72;--warn:#e2b64d;--warn-wash:#2a2414;--loss-wash:#2e1a18;
  --grid:#222a25;--tip-bg:#edf1ee;--tip-ink:#0e1210;--pD:#3987e5;--pR:#e66767;--pI:#8b958e;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 var(--f-body);-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:22px 16px 80px}
@media (min-width:760px){.wrap{padding-inline:28px}}
h1,h2,h3{text-wrap:balance}
.mast{display:flex;align-items:flex-end;justify-content:space-between;gap:12px 20px;flex-wrap:wrap}
.mast h1{font:700 30px/1.05 var(--f-display);letter-spacing:-.025em;margin:0}
.mast p{margin:6px 0 0;color:var(--ink-2);font-size:14px}
.status{display:flex;gap:8px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:7px;font:600 13px/1 var(--f-body);padding:8px 12px;border-radius:999px;border:1px solid var(--rule-2);background:var(--sheet);color:var(--ink-2)}
.pill::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--gain)}
.pill.bad{background:var(--loss-wash);border-color:transparent;color:var(--loss)}.pill.bad::before{background:var(--loss)}
.pill.warn{background:var(--warn-wash);border-color:transparent;color:var(--warn)}.pill.warn::before{background:var(--warn)}
.banner{margin-top:14px;padding:11px 15px;border-radius:10px;background:var(--warn-wash);font-size:14px}
.banner.bad{background:var(--loss-wash)}
.panel{display:grid;gap:18px;margin-top:20px}
.grid2{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(min(100%,500px),1fr))}
.card{background:var(--sheet);border:1px solid var(--rule);border-radius:14px;padding:20px 22px;min-width:0}
.card h2{font:600 19px/1.3 var(--f-display);margin:0;letter-spacing:-.01em}
.card h3{font:600 15px/1.3 var(--f-display);margin:20px 0 10px}
.hd{display:flex;align-items:baseline;justify-content:space-between;gap:4px 14px;flex-wrap:wrap;margin-bottom:16px}
.hd .sub{color:var(--ink-3);font-size:13.5px}
.hero{display:grid;gap:24px;grid-template-columns:minmax(0,1fr) minmax(0,1.5fr);align-items:end}
.hero .tiles{grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:900px){.hero{grid-template-columns:1fr}}
@media (max-width:520px){.hero .tiles{grid-template-columns:1fr}}
.hero .big .lb{font-size:15px;color:var(--ink-2);font-weight:500}
.hero .big .v{font:700 60px/1.05 var(--f-display);letter-spacing:-.03em;margin-top:6px}
.hero .big .d{font-size:14px;color:var(--ink-2);margin-top:8px;max-width:46ch}
.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr))}
.tile{background:var(--sheet-2);border:1px solid var(--rule);border-radius:12px;padding:14px 16px;min-width:0}
.tile .lb{font-size:13.5px;color:var(--ink-2);font-weight:500}
.tile .v{font:600 30px/1.15 var(--f-display);letter-spacing:-.02em;margin-top:6px;overflow-wrap:anywhere}
.tile .d{font-size:13px;line-height:1.4;margin-top:6px;color:var(--ink-3)}
.tile .tag{white-space:normal;line-height:1.3;margin-top:6px}
.up,.gain{color:var(--gain)}.down,.loss{color:var(--loss)}.muted{color:var(--ink-3)}
.legend{display:flex;gap:18px;font-size:13.5px;color:var(--ink-2);flex-wrap:wrap;margin:14px 0 4px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:7px;vertical-align:4px}
.chart{position:relative;width:100%;margin-top:6px}
.chart svg{display:block;width:100%;height:auto;overflow:visible}
.ax{font:12px var(--f-fig);fill:var(--ink-3)}
.gridl{stroke:var(--grid);stroke-width:1}.basel{stroke:var(--rule-2);stroke-width:1}
.ln-s{fill:none;stroke:var(--set);stroke-width:2.2;stroke-linejoin:round;stroke-linecap:round}
.ln-m{fill:none;stroke:var(--in);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
circle.ln-s{fill:var(--set);stroke:var(--sheet);stroke-width:2}circle.ln-m{fill:var(--in);stroke:var(--sheet);stroke-width:2}
.endlab{font:500 12.5px var(--f-fig);fill:var(--ink)}
.b-in{fill:var(--in)}.b-loss{fill:var(--loss)}
.hit{fill:transparent;cursor:crosshair}.hit:hover{fill:var(--ink);fill-opacity:.04}
.xhair{stroke:var(--ink-3);stroke-width:1}
.tip{position:absolute;pointer-events:none;background:var(--tip-bg);color:var(--tip-ink);font-size:13px;line-height:1.5;padding:8px 11px;border-radius:8px;white-space:nowrap;z-index:5;box-shadow:0 6px 20px rgba(0,0,0,.18)}
.tip .t{font-weight:600;margin-bottom:2px}.tip .r{display:flex;justify-content:space-between;gap:18px;font-family:var(--f-fig);font-size:12.5px}
.bars{display:grid;gap:2px}
.brow{display:grid;grid-template-columns:minmax(110px,190px) minmax(60px,1fr) minmax(70px,auto);gap:14px;align-items:center;padding:7px 0}
.brow .nm{font-size:14.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brow .track{height:12px;position:relative;background:var(--sheet-2);border-radius:4px}
.brow .fill{position:absolute;top:0;bottom:0;min-width:2px;border-radius:4px}
.brow .fill.gain{background:var(--gain)}.brow .fill.loss{background:var(--loss)}.brow .fill.b-in{background:var(--in)}
.brow .mid{position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--rule-2)}
.brow .val{font:500 14px var(--f-fig);text-align:right;white-space:nowrap}
.brow .pc{color:var(--ink-3);font-size:12.5px}
.total{display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;padding-top:9px;border-top:1px solid var(--rule-2);font-weight:600}
.total .val{font:600 14.5px var(--f-fig);border-bottom:3px double var(--ink-2)}
.meter{height:10px;border-radius:5px;background:var(--sheet);border:1px solid var(--rule);overflow:hidden;margin-top:9px}
.meter span{display:block;height:100%;border-radius:5px}.meter .ok{background:var(--set)}.meter .warn{background:var(--warn)}.meter .loss{background:var(--loss)}
.tw{overflow-x:auto;margin-inline:-22px;padding-inline:22px}
table.t{width:100%;border-collapse:collapse;font-size:14.5px}
.t th{font:600 11.5px/1.25 var(--f-body);text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);text-align:left;padding:9px 8px;border-bottom:1px solid var(--rule-2);white-space:nowrap;vertical-align:bottom}
.t td{padding:8px;border-bottom:1px solid var(--rule);vertical-align:middle}
.t tr:last-child td{border-bottom:0}.t tr.tot td{font-weight:600;border-top:1px solid var(--rule-2)}
.t .num{text-align:right;font-family:var(--f-fig);font-variant-numeric:tabular-nums;white-space:nowrap;font-size:14px}
.t th.num{font-family:var(--f-body)}
.t .dt{font:13.5px var(--f-fig);color:var(--ink-2);white-space:nowrap}
.t .mk{max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pty{display:inline-block;width:20px;text-align:center;font:700 11px/18px var(--f-body);border-radius:4px;color:#fff;margin-right:4px}
.pD{background:var(--pD)}.pR{background:var(--pR)}.pI{background:var(--pI)}
.tag{display:inline-block;font:600 11px/1 var(--f-body);text-transform:uppercase;letter-spacing:.05em;padding:4px 7px;border-radius:5px;background:var(--sheet-2);color:var(--ink-2);white-space:nowrap}
.tag.warn{background:var(--warn-wash);color:var(--warn)}.tag.bad{background:var(--loss-wash);color:var(--loss)}.tag.ok{background:var(--accent-wash);color:var(--accent)}
.note{font-size:13.5px;color:var(--ink-3);margin:10px 0 0}
details.defs{margin-top:22px}
details.defs summary{cursor:pointer;font:600 15px var(--f-display);color:var(--ink-2)}
details.defs dl{display:grid;grid-template-columns:minmax(150px,220px) 1fr;gap:8px 18px;font-size:13.5px;color:var(--ink-2);margin:14px 0 0}
details.defs dt{font-weight:600;color:var(--ink)}details.defs dd{margin:0}
@media (max-width:600px){details.defs dl{grid-template-columns:1fr}.hero .big .v{font-size:46px}.brow{grid-template-columns:minmax(90px,130px) 1fr auto}}
"""

JS = """
(function(){
  var tip=document.createElement('div');tip.className='tip';tip.hidden=true;document.body.appendChild(tip);
  var busy=0;
  document.querySelectorAll('[data-chart]').forEach(function(box){
    var svg=box.querySelector('svg'),xh=svg.querySelector('.xhair');
    box.addEventListener('mousemove',function(ev){
      var h=ev.target.closest('.hit');busy=Date.now();
      if(!h){tip.hidden=true;if(xh)xh.setAttribute('visibility','hidden');return}
      var p=h.getAttribute('data-tip').split('|'),html='<div class="t">'+p[0]+'</div>';
      for(var i=1;i<p.length;i+=2)html+='<div class="r"><span>'+p[i]+'</span><b>'+p[i+1]+'</b></div>';
      tip.innerHTML=html;tip.hidden=false;
      var x=ev.pageX+14,y=ev.pageY+14;if(x+tip.offsetWidth>window.scrollX+innerWidth-8)x=ev.pageX-tip.offsetWidth-14;
      tip.style.left=x+'px';tip.style.top=y+'px';
      if(xh&&h.dataset.x){xh.setAttribute('x1',h.dataset.x);xh.setAttribute('x2',h.dataset.x);xh.setAttribute('visibility','visible')}
    });
    box.addEventListener('mouseleave',function(){tip.hidden=true;if(xh)xh.setAttribute('visibility','hidden')});
  });
  // reload every 45 s, but not while a chart is being read
  setInterval(function(){if(Date.now()-busy>8000&&!document.querySelector('details.defs[open]'))location.reload()},45000);
})();
"""


def render(D: dict) -> str:
    H, st, V, P, R = D["headline"], D["status"], D["volume"], D["positions"], D["recycle"]
    hl = st.get("health") or {}
    w = H["windows"]
    tz = time.strftime("%Z")
    under_24h = abs(w["24h"]["start_value"] - START_VALUE) < 1e-6
    o = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
         '<title>SIG Bot Dashboard</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Instrument+Sans:wght@400;500;600&family=Schibsted+Grotesk:wght@600;700&display=swap">'
         f'<style>{CSS}</style></head><body><div class="wrap">']

    # ---- masthead and status
    running = (st.get("procs") or 0) > 0
    age = st.get("cycle_age")
    feed_ok = str(hl.get("feed", "")).startswith("ok")
    pills = [("Bot running" if running else "Bot NOT running", "" if running else "bad"),
             (f"Last cycle {_ago(age)} ago", "" if age is not None and age < 180 else "bad"),
             ("Market feed OK" if feed_ok else f"Feed: {hl.get('feed', '?')}", "" if feed_ok else "warn"),
             ((f"Kill switch ON until {_lt(st['kill_until'])}" if st.get("kill_until") else "Kill switch ON") if st.get("kill_active") else "Kill switch off",
              "bad" if st.get("kill_active") else "")]
    o.append(f'<header class="mast"><div><h1>SIG trading bot</h1><p>Updated {_lt(D["now"])} {tz} · refreshes every 45 s · '
             f'account read {_ago(H["venue_age"])} ago</p></div><div class="status">'
             + "".join(f'<span class="pill {c}">{_e(t)}</span>' for t, c in pills) + '</div></header>')
    for wmsg in D["warnings"]:
        o.append(f'<div class="banner bad">{_e(wmsg)}</div>')
    if H.get("venue_error"):
        o.append(f'<div class="banner">Could not read the account just now ({_e(H["venue_error"])}); showing the last good read.</div>')
    o.append('<div class="panel">')

    # ---- 1 profit
    dn = [k for k in ("1h", "6h", "24h") if w[k]["bot_down_min"] > 5]
    o.append('<section class="card"><div class="hero"><div class="big">'
             f'<div class="lb">Profit since the start (100,000)</div><div class="v {_cls(H["pnl_start"])}">{_f(H["pnl_start"], 0, True)}</div>'
             f'<div class="d">The account is worth <b>{_f(H["settlement"])}</b> counting locked arb sets at their guaranteed payout, '
             f'and <b>{_f(H["marked"])}</b> at today\'s market prices.</div></div><div class="tiles">'
             + _tile("Profit, last hour", _f(w["1h"]["settle"], 0, True), "", _cls(w["1h"]["settle"]))
             + _tile("Profit, last 6 hours", _f(w["6h"]["settle"], 0, True), "", _cls(w["6h"]["settle"]))
             + _tile("Profit, last 24 hours", _f(w["24h"]["settle"], 0, True), "same as since start: the bot is under a day old" if under_24h else "",
                     _cls(w["24h"]["settle"]))
             + '</div></div>')
    if dn:
        o.append(f'<div class="banner">The bot was offline during part of the last {", ".join(dn)}. Profit for those windows is approximate (about ±50).</div>')
    o.append('<h3>Account value, last 24 hours</h3><div class="legend"><span><i style="background:var(--set)"></i>If locked arb sets pay out</span>'
             '<span><i style="background:var(--in)"></i>At market prices</span></div>' + _chart_value(D) + '</section>')

    # ---- 2 volume
    v1, v24 = V["windows"]["1h"], V["windows"]["24h"]
    pace_frac = v24["pace"] / V["target"] if V["target"] else 0
    incomplete = bool(v24["flags"])
    o.append('<div class="grid2"><section class="card"><div class="hd"><h2>Trading volume</h2><span class="sub">target 100k a day</span></div><div class="tiles">'
             + _tile("Our volume, last hour", _f(v1["ours"]), f'{v1["n_fills"]} fills')
             + _tile("Our volume, last 24 hours", _f(v24["ours"]), f'{v24["n_fills"]} fills')
             + _tile("Daily pace vs target", _pct(pace_frac, 0), _meter(pace_frac, 2, 9) + f'<div style="margin-top:6px">{_f(v24["pace"])} a day</div>')
             + _tile("Our share of all venue trading", _pct(v24["share"], 2),
                     f'last 24 h · last hour {_pct(v1["share"], 2)}' + ('<br><span class="tag warn">likely lower: venue data incomplete</span>' if incomplete else ""))
             + '</div><h3>Volume by strategy, last 24 hours</h3>')
    bs = sorted(v24["by_sleeve"].items(), key=lambda kv: -kv[1])
    o.append(_bars([(_e(_sl(s)), v, _pct(v / (v24["ours"] or 1), 0)) for s, v in bs], signed=False))
    o.append('<h3>Our volume per hour</h3>' + _chart_cols(
        V["hourly"], label="Our volume per hour",
        tipf=lambda r: f'{time.strftime("%a %H:00", time.localtime(r[0]))}|Our volume|{r[1]:,.0f}|All venue|{r[2]:,.0f}|Our share|{_pct(r[1] / r[2], 2) if r[2] else "–"}')
             + '</section>')

    # ---- 3 strategies P&L
    PA = D["pnl_sleeves"]["all"]
    rows = []
    for s in sorted(PA, key=lambda s: -PA[s]["pnl"]):
        a = PA[s]
        v = a["pnl"] + a.get("settle_extra", 0.0)
        rows.append((_e(_sl(s)), v, f'{a["n"]} fills'))
    o.append('<section class="card"><div class="hd"><h2>Profit by strategy</h2><span class="sub">since the start</span></div>'
             + _bars(rows, total=sum(r[1] for r in rows)))
    o.append('<h3>Where the profit came from</h3><div class="tw"><table class="t"><tr><th>Strategy</th><th class="num">Profit</th>'
             '<th class="num">Good entry prices</th><th class="num">Price moves since</th></tr>')
    for s in sorted(PA, key=lambda s: -PA[s]["pnl"]):
        a = PA[s]
        o.append(f'<tr><td>{_e(_sl(s))}</td><td class="num {_cls(a["pnl"])}">{_f(a["pnl"], 0, True)}</td>'
                 f'<td class="num">{_f(a["edge"], 0, True)}</td><td class="num">{_f(a["move"], 0, True)}</td></tr>')
    o.append('</table></div><p class="note">Profit here is at market prices. Arb sets in the chart above also count the extra profit already locked in '
             f'({_f(PA.get("arb", {}).get("settle_extra"), 0, True)}).</p></section></div>')

    # ---- 4 markets
    M = D["markets"]

    def mtable(rows_):
        t = ['<div class="tw"><table class="t"><tr><th>Market</th><th>Strategy</th><th class="num">Profit</th><th class="num">Fills</th><th class="num">Holding</th></tr>']
        for r in rows_:
            t.append(f'<tr><td class="mk" title="{_e(r["race"])}">{_mkt(r["party"], r["race"])}</td><td>{_e(_sl(r["sleeve"]))}</td>'
                     f'<td class="num {_cls(r["pnl"])}">{_f(r["pnl"], 0, True)}</td><td class="num">{r["n"]}</td>'
                     f'<td class="num">{_f(abs(r["pos"]))} {"YES" if r["pos"] > 0 else ("NO" if r["pos"] < 0 else "")}</td></tr>')
        t.append("</table></div>")
        return "".join(t)
    best = [r for r in M["all_best"] if r["pnl"] > 0][:8]
    worst = [r for r in M["all_worst"] if r["pnl"] < 0][:8]
    span = "since the start" if under_24h else "since the start"
    o.append(f'<div class="grid2"><section class="card"><div class="hd"><h2>Best markets</h2><span class="sub">{span}</span></div>{mtable(best)}</section>'
             f'<section class="card"><div class="hd"><h2>Worst markets</h2><span class="sub">{span}</span></div>{mtable(worst)}</section></div>')
    if not under_24h:
        b24 = [r for r in M["24_best"] if r["pnl"] > 0][:8]
        w24 = [r for r in M["24_worst"] if r["pnl"] < 0][:8]
        o.append(f'<div class="grid2"><section class="card"><div class="hd"><h2>Best markets</h2><span class="sub">last 24 hours</span></div>{mtable(b24)}</section>'
                 f'<section class="card"><div class="hd"><h2>Worst markets</h2><span class="sub">last 24 hours</span></div>{mtable(w24)}</section></div>')

    # ---- 5 fill quality
    MK = D["markouts"]
    o.append('<div class="grid2"><section class="card"><div class="hd"><h2>Are we getting good prices?</h2>'
             '<span class="sub">average price move after our fills, in ticks (½¢)</span></div>'
             '<div class="tw"><table class="t"><tr><th>Strategy</th><th class="num">5 min later,<br>last hour</th><th class="num">5 min later,<br>last 24 h</th>'
             '<th class="num">30 min later,<br>last 24 h</th><th class="num">Fills</th></tr>')
    for s in MARKOUT_SLEEVES:
        cells = []
        for key in (f"{s}|5m|1h", f"{s}|5m|24h", f"{s}|30m|24h"):
            x = MK.get(key)
            cells.append(f'<td class="num {_cls(x["mean"]) if x else ""}">{_f(x["mean"], 2, True) if x else "–"}</td>')
        n = (MK.get(f"{s}|5m|24h") or {}).get("n", 0)
        o.append(f'<tr><td>{_e(_sl(s))}</td>{"".join(cells)}<td class="num muted">{n}</td></tr>')
    o.append('</table></div><p class="note">Positive means the price moved our way after we traded. A strategy that stays negative is paying '
             'the spread to better-informed traders.</p>')
    o.append('<h3>Markets where we keep getting picked off</h3>')
    if D["losing"]:
        o.append('<div class="tw"><table class="t"><tr><th>Market</th><th>Strategy to blame</th><th class="num">Bad fills</th><th class="num">Cost</th></tr>')
        for r in D["losing"]:
            o.append(f'<tr><td class="mk">{_mkt(r["party"], r["race"])}</td><td>{_e(_sl(r["sleeve"]))}</td>'
                     f'<td class="num">{100 * r["neg_pct"]:.0f}% of {r["n"]}</td><td class="num {_cls(r["usd"])}">{_f(r["usd"], 0, True)}</td></tr>')
        o.append('</table></div><p class="note">Markets with at least 10 fills where at least 60% moved against us within 5 minutes.</p>')
    else:
        o.append('<p class="muted">None: no market has 10+ fills with 60%+ of them moving against us.</p>')
    o.append('</section>')

    # ---- 6 positions
    cap_frac = abs(P["net_d"]) / P["cap"] if P["cap"] else 0
    lean = "toward Democrats" if P["net_d"] > 0 else "toward Republicans"
    locked_total = sum(s["locked"] or 0 for s in P["sets"])
    o.append('<section class="card"><div class="hd"><h2>What we hold</h2></div><div class="tiles">'
             + _tile("Partisan lean", f'{_f(abs(P["net_d"]))}', f'{lean} · {_pct(cap_frac, 0)} of the {_f(P["cap"])} limit' + _meter(cap_frac))
             + _tile("Open positions", str(P["n"]), f'worth {_f(P["gross"])} at market prices')
             + _tile("Profit locked in arb sets", _f(locked_total, 0, True), f'{len(P["sets"])} races, paid out whoever wins', "gain" if locked_total > 0 else "")
             + '</div><h3>Largest holdings outside arb sets</h3><div class="tw"><table class="t"><tr><th>Market</th><th>Strategy</th><th class="num">Shares</th>'
               '<th class="num">Worth</th><th class="num">Gain</th></tr>')
    for r in [r for r in P["all"] if not r["in_set"]][:10]:
        u = (r["mv"] - r["cost"]) if r["mv"] is not None and r["cost"] is not None else None
        o.append(f'<tr><td class="mk" title="{_e(r["race"])}">{_mkt(r["party"], r["race"])}</td><td>{_e(_sl(r["sleeve"]))}</td>'
                 f'<td class="num">{_f(abs(r["q"]))} {"YES" if r["q"] > 0 else "NO"}</td><td class="num">{_f(r["mv"])}</td>'
                 f'<td class="num {_cls(u)}">{_f(u, 0, True)}</td></tr>')
    o.append('</table></div></section></div>')

    # ---- 7 arb sets and recycling
    W = R["windows"]
    lt = R["locked_total"]
    lfrac = lt / R["ceiling"] if lt is not None and R["ceiling"] else None
    w24r = W["24h"]
    net24 = w24r["recycle"]["net"] + w24r["offload exit"]["net"]
    recycled24 = sum(w24r[k]["capital"] for k in ("recycle", "free unwind", "offload exit", "unwind (no log line)"))
    o.append('<div class="grid2"><section class="card"><div class="hd"><h2>Arb sets</h2><span class="sub">NO on every candidate in a race, so all but one pay out whoever wins</span></div><div class="tiles">'
             + _tile("Money tied up in arb sets", _f(lt) if lt is not None else "–",
                     (f'{_pct(lfrac, 0)} of the {_f(R["ceiling"])} limit' + _meter(lfrac, 0.85, 1.0)) if lfrac is not None else "needs an account read")
             + _tile("Sets bought, last 24 hours", str(w24r["entries"]), f'{_f(w24r["entry_capital"])} spent · {_f(w24r["entry_edge"], 0, True)} profit locked')
             + _tile("Failed attempts, last 24 hours", str(w24r["failed"]), "the book moved before every leg could fill", "loss" if w24r["failed"] else "")
             + '</div><h3>Every arb set</h3><div class="tw"><table class="t"><tr><th>Race</th><th class="num">Sets</th><th class="num">Cost</th>'
               '<th class="num" title="value of the sets at the venue\'s market prices now, minus what they cost">Live P&amp;L<br>at market</th>'
               '<th class="num" title="what the sets pay out whoever wins, minus what they cost">Locked profit<br>at payout</th>'
               '<th class="num" title="money in the race against its per-race limit">Of its<br>limit</th></tr>')
    lim = {r["race"]: r for r in R["locked"]}
    live_total = cost_total = 0.0
    for s in P["sets"]:
        lr = lim.get(s["race"], {})
        u = lr["committed"] / lr["limit"] if lr.get("committed") is not None and lr.get("limit") else None
        live_total += s["marked_pnl"] or 0.0
        cost_total += s["cost"] or 0.0
        o.append(f'<tr><td class="mk">{_e(s["race"])}</td><td class="num">{_f(s["k"])}</td><td class="num">{_f(s["cost"])}</td>'
                 f'<td class="num {_cls(s["marked_pnl"])}">{_f(s["marked_pnl"], 0, True)}</td><td class="num {_cls(s["locked"])}">{_f(s["locked"], 0, True)}</td>'
                 f'<td class="num {"loss" if u is not None and u > 1.001 else ""}">{_pct(u, 0)}</td></tr>')
    o.append(f'<tr class="tot"><td>All {len(P["sets"])} races</td><td></td><td class="num">{_f(cost_total)}</td><td class="num {_cls(live_total)}">{_f(live_total, 0, True)}</td>'
             f'<td class="num">{_f(locked_total, 0, True)}</td><td></td></tr></table></div>'
             '<p class="note">Live P&amp;L moves with market prices; locked profit is what the set pays when the race settles, whoever wins.</p>')
    o.append('<h3>Failed attempts per hour</h3>' + _chart_cols(
        [(a, c) for a, c in R["fail_hours"]], h=120, cls="b-loss", label="Failed arb attempts per hour",
        tipf=lambda r: f'{time.strftime("%a %H:00", time.localtime(r[0]))}|Failed attempts|{r[1]}') + '</section>')

    o.append('<section class="card"><div class="hd"><h2>Recycling money into better trades</h2><span class="sub">selling a weaker holding to fund a better one</span></div><div class="tiles">'
             + _tile("Money recycled, last 24 hours", _f(recycled24), f'{w24r["recycle"]["n"]} arb sets · {w24r["offload exit"]["n"]} other holdings')
             + _tile("Net gain from recycling, 24 h", _f(net24, 0, True), f'gave up {_f(w24r["recycle"]["given_up"] + w24r["offload exit"]["given_up"], 0)} of expected profit '
                     f'for {_f(w24r["recycle"]["new_edge"] + w24r["offload exit"]["new_edge"], 0)}', _cls(net24))
             + '</div><h3>Latest recycling trades</h3><div class="tw"><table class="t"><tr><th>Time</th><th>Sold → to fund</th><th class="num">Money</th>'
               '<th class="num">Net gain</th><th></th></tr>')
    for e in R["events"]:
        tag = '<span class="tag bad">not filled</span>' if not e["ok"] else ""
        if "follow" in e and e["ok"] and not e["follow"]:
            tag = '<span class="tag warn">new set never bought</span>'
        kind = "Arb set" if e["type"] == "recycle" else ("Free unwind" if e["type"].startswith("free") else "Holding")
        o.append(f'<tr><td class="dt">{_lt(e["t"])}</td><td class="mk" title="{_e(e["what"])}"><span class="tag">{kind}</span> {_e(e["what"])}</td>'
                 f'<td class="num">{_f(e["capital"])}</td><td class="num {_cls(e["net"])}">{_f(e["net"], 1, True)}</td><td>{tag}</td></tr>')
    o.append('</table></div></section></div>')

    # ---- 8 latest fills
    o.append(f'<section class="card"><div class="hd"><h2>Latest trades</h2><span class="sub">times in {tz}</span></div><div class="tw"><table class="t">'
             '<tr><th>Time</th><th>Market</th><th>Strategy</th><th>Bought</th><th class="num">Price</th><th class="num">Shares</th>'
             '<th class="num">Volume</th><th class="num">Moved our way<br>after 5 min</th></tr>')
    for r in D["recent"][:20]:
        px = r["price"] if r["side"] == "yes" else 1 - r["price"]
        mk = f'<td class="num {_cls(r["mk5"])}">{_f(r["mk5"], 1, True)}</td>' if r["mk5"] is not None else '<td class="num muted">pending</td>'
        o.append(f'<tr><td class="dt">{_lt(r["t"])}</td><td class="mk" title="{_e(r["race"])}">{_mkt(r["party"], r["race"])}</td>'
                 f'<td>{_e(_sl(r["sleeve"]))}</td><td>{r["side"].upper()}</td><td class="num">{100 * px:.1f}¢</td><td class="num">{_f(r["q"])}</td>'
                 f'<td class="num">{_f(r["vol"])}</td>{mk}</tr>')
    o.append('</table></div></section>')

    # ---- 9 events
    o.append(f'<section class="card"><div class="hd"><h2>Bot events</h2><span class="sub">times in {tz}</span></div><div class="tw"><table class="t">'
             '<tr><th>Time</th><th>Event</th><th>Detail</th></tr>')
    seen, rows_ev = {}, []
    for t, k, txt in D["events"]:
        key = (k, txt)
        if key in seen:
            seen[key][3] += 1
            continue
        seen[key] = [t, k, txt, 1]
        rows_ev.append(seen[key])
    for t, k, txt, n in rows_ev[:12]:
        if n > 1:
            txt = f"{txt}  (×{n}, latest shown)"
        cls = "bad" if k == "KILL SWITCH" else ("warn" if k in ("RETRACE PAUSE", "APPLIED") else "")
        o.append(f'<tr><td class="dt">{_e(t[11:16])}</td><td><span class="tag {cls}">{_e(EVENT_NAMES.get(k, k))}</span></td>'
                 f'<td style="font-size:13.5px;color:var(--ink-2)">{_e(txt[:160])}</td></tr>')
    o.append('</table></div></section>')

    # ---- definitions
    vt = v24["fills_in_tape"]
    o.append('<details class="defs"><summary>How these numbers are computed</summary><dl>'
             '<dt>Profit since the start</dt><dd>Account value minus the 100,000 starting cash, with arb sets valued at their guaranteed payout '
             '(legs − 1 per set) instead of at market prices. Matches <code>python3 -m sigbot.review</code>.</dd>'
             '<dt>Profit over a window</dt><dd>Value now minus value at the window start. The past value is rebuilt from the fills log: cash (100,000 plus every '
             'fill up to then) and positions then, arb sets at payout, the rest at the last quote mid or trade price then.</dd>'
             '<dt>At market prices</dt><dd>The venue\'s own account value: cash plus every position at the venue\'s marks.</dd>'
             '<dt>Volume</dt><dd>Shares × price paid on the side bought (YES at the price, NO at 1 − price), the leaderboard definition.</dd>'
             '<dt>Share of venue trading</dt><dd>Our volume ÷ all public trades\' volume over the same window (both sides of each trade, i.e. its share count). '
             f'{vt[0]} of our {vt[1]} fills in the last 24 h appear in the recorded public trades; '
             + ("; ".join(_e(x) for x in v24["flags"]) + ". The share is therefore an upper bound." if v24["flags"] else "the record looks complete.") + '</dd>'
             '<dt>Daily pace</dt><dd>Last-24 h volume scaled to a full day (over the hours since our first fill while the bot is under a day old).</dd>'
             '<dt>Good entry prices / price moves</dt><dd>Each fill\'s profit = shares × (price now − fill price), split into (mid at fill − fill price) and (price now − mid at fill).</dd>'
             '<dt>Price move after our fills</dt><dd>Mid price 5 or 30 minutes after a fill minus the fill price, signed so positive is in our favour, in ticks of 0.5¢. '
             'Same as <code>sigbot.review</code>.</dd>'
             '<dt>Partisan lean</dt><dd>Net money on Democrats winning (YES on D, NO on R) minus the reverse, arb sets excluded, against '
             '<code>portfolio_max_partisan_notional</code>.</dd>'
             '<dt>Arb set limits</dt><dd>Money in a race = cost of every leg where we hold NO on all legs. Limit per race = min(15,000, max(allocator cap, 2,000)); '
             'overall limit = 90% of 100,000. Caps never force a sale.</dd>'
             '<dt>Recycling</dt><dd>Gain = money moved × (return of the new trade − return given up), from the bot\'s RECYCLE and OFFLOAD log lines. '
             '"New set never bought" means the unwind happened but the trade it was meant to fund did not fill within 3 minutes.</dd>'
             f'<dt>Refresh</dt><dd>Recomputed every 45 s from <code>data/sigbot.sqlite</code> and <code>data/live.log</code> ({D["compute_ms"]:.0f} ms of CPU); '
             'the account is read from the venue at most once a minute (2 reads).</dd>'
             + "".join(f'<dt>Data check</dt><dd>{_e(x)}</dd>' for x in D["discrepancies"]) + '</dl></details>')
    o.append(f'</div></div><script>{JS}</script></body></html>')
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
