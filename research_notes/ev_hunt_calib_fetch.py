"""Calibration dataset: resolved US election prediction-market legs with price paths 0..90 days before the election.

Output (data/research_ev_hunt/calib/):
    legs_all.csv   one row per leg (a YES contract on one party/candidate winning one race), both venues
    paths.csv      venue, market_id, days_before (0.5, 0..90), price, stale_days, spread
    README.md      written by hand from the build report (this script prints the report)
Raw API caches live in data/research_ev_hunt/calib/raw/ (new downloads) and are read from
data/polymarket_history/raw/ (the earlier favourite-drift study) before anything is downloaded.

Conventions
    T0 = election date 12:00 UTC (about 07:00-08:00 ET on election morning).
    days_before = d means the last observation at or before T0 - d days; d = 0.5 means at or before 00:00 UTC on
    election day (election eve evening ET). Observations older than 3 days before the cutoff are not used (price blank);
    stale_days is the age of the observation that was found (also given for blank rows when an older one exists).
    All observation timestamps are floored to the minute (Polymarket daily points are stamped 00:00:0x).

Venues / sources
    clob            Polymarket CLOB prices-history: daily (interval=max, fidelity=1440) + hourly (fidelity=60) for the
                    last 5 days.
    chain           Polymarket AMM (FPMM) markets rebuilt from FPMMBuy/FPMMSell logs on Polygon (2020, 2022 markets).
    kalshi_candles  Kalshi /historical/markets/{ticker}/candlesticks, daily (1440) + hourly (60) for the last 5 days;
                    price = mid of yes_bid.close and yes_ask.close, else last trade (see kalshi_px).

Run:  python3 -I research_notes/ev_hunt_calib_fetch.py --fetch [--only pm|chain|kalshi]   (download missing, then build)
      python3 -I research_notes/ev_hunt_calib_fetch.py                                       (build from caches only)
Polite: at most ~3 requests/s, back off on 429; every response is cached and never re-fetched.
"""
from __future__ import annotations

import bisect
import calendar
import csv
import glob
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import favourite_drift_fetch as F  # noqa: E402  (TLS context)
import favourite_drift_extra as E  # noqa: E402  (keccak, FPMM topics, RPC helper, B_RACES)

OLD = os.path.join(ROOT, "data", "polymarket_history")
OLD_RAW = os.path.join(OLD, "raw")
OUT = os.path.join(ROOT, "data", "research_ev_hunt", "calib")
RAW = os.path.join(OUT, "raw")
DAY = 86400
WINDOW = 90            # days before the election kept in paths
MAX_STALE = 3 * DAY
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
MIN_VOLUME = 1000.0
F.PAUSE = 0.36         # E._rpc sleeps F.PAUSE after each call
ELECTION_DAY = {"2020": "2020-11-03", "2021": "2021-11-02", "2022": "2022-11-08", "2023": "2023-11-07",
                "2024": "2024-11-05", "2025": "2025-11-04"}

# ------------------------------------------------------------------------------------------------ small utilities
_http = {"last": 0.0, "gap": 0.36, "n": 0, "429": 0}
CTX = F._ctx()


def jget(url: str, params: dict | None = None, retries: int = 6, quiet404: bool = True):
    """GET json with a rate limit and 429 backoff. Returns None on 404/400 or after the retries are exhausted."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        wait = _http["gap"] - (time.time() - _http["last"])
        if wait > 0:
            time.sleep(wait)
        _http["last"] = time.time()
        _http["n"] += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sig-ev-hunt-calib/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40, context=CTX) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _http["429"] += 1
                _http["gap"] = 1.0
                time.sleep(10 * (attempt + 1))
                continue
            if e.code in (400, 404):
                return None
            time.sleep(3 * (attempt + 1))
        except Exception:  # noqa: BLE001
            time.sleep(3 * (attempt + 1))
    return None


def rj(path):
    with open(path) as f:
        return json.load(f)


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def ts_of_day(day: str, hour: int = 12) -> int:
    return calendar.timegm(time.strptime(f"{day} {hour:02d}:00:00", "%Y-%m-%d %H:%M:%S"))


def date_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def jlist(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new-hampshire": "NH", "new-jersey": "NJ",
    "new-mexico": "NM", "new-york": "NY", "north-carolina": "NC", "north-dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode-island": "RI", "south-carolina": "SC",
    "south-dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington-dc": "DC", "washington": "WA", "west-virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def state_from_name(text: str):
    t = re.sub(r"[^a-z]+", "-", text.lower()).strip("-")
    for name in sorted(STATES, key=len, reverse=True):
        if t == name or t.startswith(name + "-"):
            return STATES[name]
    return None


# ------------------------------------------------------------------------------------------------ Polymarket legs
# New events found by discovery (Gamma /public-search and an id scan 0-8000, 2026-10-24). Fetched into RAW/events.
# cfg keys: cycle, date, office, state, district, special, mode, us, parties/cands, outcome (manual outcome by party)
NEW_EVENTS = {
    # 2020 AMM markets (CLOB history empty -> rebuilt from Polygon logs). outcomePrices are 0/0 for four of them
    # (voided in Gamma), so the result is set by hand: FL R, GA D, PA D, TX R (AZ D is in outcomePrices).
    4795: dict(cycle="2020", office="president", state="US", mode="single", party="R", label="Trump wins 2020"),
    3846: dict(cycle="2020", office="president", state="FL", mode="two_party", outcome={"R": 1, "D": 0}),
    3840: dict(cycle="2020", office="president", state="GA", mode="two_party", outcome={"R": 0, "D": 1}),
    3842: dict(cycle="2020", office="president", state="PA", mode="two_party", outcome={"R": 0, "D": 1}),
    4488: dict(cycle="2020", office="president", state="TX", mode="two_party", outcome={"R": 1, "D": 0}),
    5378: dict(cycle="2020", office="president", state="AZ", mode="two_party", outcome={"R": 0, "D": 1}),
    # 2021 / 2023
    4369: dict(cycle="2021", office="governor", state="VA", mode="single", party="D", label="McAuliffe"),
    902472: dict(cycle="2023", office="governor", state="KY", mode="single", party="D", label="Democrat (Beshear)"),
    # 2022 national control
    3506: dict(cycle="2022", office="other", state="US", mode="two_party", label_race="senate_control"),
    5430: dict(cycle="2022", office="other", state="US", mode="two_party", label_race="house_control"),
    # 2024 national
    4076: dict(cycle="2024", office="president", state="US", mode="two_party", label_race="party"),
    902520: dict(cycle="2024", office="other", state="US", mode="two_party", label_race="senate_control"),
    903514: dict(cycle="2024", office="other", state="US", mode="two_party", label_race="house_control"),
    903193: dict(cycle="2024", office="president", state="US", mode="cands", label_race="winner",
                 cands={"Donald Trump": "R", "Kamala Harris": "D", "Michelle Obama": "D", "Robert F. Kennedy Jr.": "I",
                        "Joe Biden": "D"}),
    12719: dict(cycle="2024", office="senate", state="NE", special=True, mode="auto"),
    903278: dict(cycle="2024", date="2024-02-13", office="house", state="NY", district=3, special=True, mode="auto"),
    # 2025
    21308: dict(cycle="2025", date="2025-04-01", office="house", state="FL", district=1, special=True, mode="auto"),
    21311: dict(cycle="2025", date="2025-04-01", office="house", state="FL", district=6, special=True, mode="auto"),
    23245: dict(cycle="2025", office="mayor", state="NY", district="NYC", mode="single", party="D", label="Democrat wins NYC mayor"),
    23246: dict(cycle="2025", office="mayor", state="NY", district="NYC", mode="cands",
                cands={"Zohran Mamdani": "D", "Curtis Sliwa": "R", "Andrew Cuomo": "I", "Eric Adams": "I", "Jim Walden": "I"}),
    41299: dict(cycle="2025", date="2025-09-23", office="house", state="AZ", district=7, special=True, mode="cands",
                cands={"Adelita Grijalva": "D", "Daniel Butierez": "R"}),
    54928: dict(cycle="2025", office="other", state="VA", district="AG", mode="cands",
                cands={"Jay Jones": "D", "Jason Miyares": "R"}),
    48328: dict(cycle="2025", office="other", state="VA", district="HoD", mode="two_party"),
    # 2022 single-candidate yes/no markets
    713958: dict(cycle="2022", office="house", state="AK", district="AL", mode="single", party="D", label="Peltola wins AK-AL"),
    834763: dict(cycle="2022", office="senate", state="AK", mode="single", party="R", label="Murkowski wins AK-Sen"),
}
# Old-cache events that need a state/office from the market rather than the slug (one event, many races)
MULTI_STATE_EVENTS = {5808: "senate", 5863: "governor", 834675: "house"}
SKIP_OLD_EVENTS = set()

_PARTY_PREFIX = (("dem", "D"), ("rep", "R"), ("ind", "I"))
_LABEL = re.compile(r"\(([DRI])\)\s*$")
_OTHER_Q = re.compile(r"^Will a candidate from another party win", re.I)
_PLACEHOLDER = re.compile(r"^(person [a-z]|candidate [a-z]|other)$", re.I)
_PARTY_Q = re.compile(r"^Will a (Democrat|Republican) win", re.I)


def party_of_name(name: str):
    n = name.strip().lower()
    for pre, p in _PARTY_PREFIX:
        if n.startswith(pre):
            return p
    return None


def classify_market(m: dict, cfg: dict):
    """Legs of one market: list of (party, outcome_index, level, label). level party|candidate."""
    q = (m.get("question") or "").strip()
    gt = (m.get("groupItemTitle") or "").strip()
    outs = [str(o).strip() for o in jlist(m.get("outcomes"))]
    mode = cfg.get("mode", "auto")
    if mode == "single":
        return [(cfg["party"], 0, "party", cfg.get("label", q))]
    if mode == "cands":
        for name, party in cfg["cands"].items():
            if name.lower() in q.lower() or name.lower() == gt.lower():
                return [(party, 0, "party", name)]
        return []
    if outs == ["Yes", "No"]:
        if _OTHER_Q.match(q):
            return [("other", 0, "party", "another party")]
        if _PLACEHOLDER.match(gt) or re.match(r"^Will (person|candidate) [a-z] win", q, re.I):
            return []
        pm = _PARTY_Q.match(q)
        if pm:
            return [("D" if pm.group(1).lower() == "democrat" else "R", 0, "party", gt or pm.group(1))]
        if gt.lower().startswith("democrat"):
            return [("D", 0, "party", gt)]
        if gt.lower().startswith("republican"):
            return [("R", 0, "party", gt)]
        lab = _LABEL.search(gt)
        if lab:
            return [(lab.group(1), 0, "candidate", gt)]
        return None
    if len(outs) == 2:
        ps = [party_of_name(o) for o in outs]
        if None not in ps and len(set(ps)) == 2:
            return [(ps[0], 0, "party", outs[0]), (ps[1], 1, "party", outs[1])]
        legs = []
        for i, name in enumerate(outs):
            hit = re.search(r"\b" + re.escape(name) + r"\s*\(([DRI])\)", q) if name else None
            if not hit:
                return None
            legs.append((hit.group(1), i, "party", name))
        if len({p for p, _, _, _ in legs}) == 2:
            return legs
    return None


def meta_from_slug(slug: str):
    """(office, state, district) from an old-cache event slug, or None."""
    s = slug.lower()
    m = re.match(r"^([a-z]{2})-(\d{1,2})-election", s) or re.match(r"^([a-z]{2})-(\d{1,2})-house-race", s)
    if m:
        return "house", m.group(1).upper(), int(m.group(2))
    m = re.match(r"^congressional-district-(\d)(?:st|nd|rd|th)-([a-z-]+?)-presidential", s)
    if m:
        return "president", state_from_name(m.group(2)), int(m.group(1))
    if "presidential-election-winner" in s:
        return "president", state_from_name(s), None
    if "us-senate-election-winner" in s:
        return "senate", state_from_name(s), None
    if "governor-election-winner" in s or "gubernatorial-election" in s:
        return "governor", state_from_name(s), None
    return None


def mid_of(m, idx):
    """Gamma market id; two-outcome (non Yes/No) markets hold two legs, so the second gets '#1'."""
    outs = [str(o).strip() for o in jlist(m.get("outcomes"))]
    return str(m["id"]) if outs == ["Yes", "No"] or idx == 0 else f"{m['id']}#{idx}"


def race_id(cycle, office, state, district=None, special=False, tag=None):
    parts = [str(cycle), office, state or "XX"]
    if district not in (None, ""):
        parts.append(str(district))
    if tag:
        parts.append(tag)
    if special:
        parts.append("special")
    return "-".join(parts)


def pm_events():
    """Yield (event, cfg, folder) for every Polymarket event to process (old caches first, then new)."""
    seen = set()
    for folder, pattern in ((os.path.join(RAW, "events"), "*.json"), (os.path.join(OLD_RAW, "events"), "*.json")):
        for p in sorted(glob.glob(os.path.join(folder, pattern)), key=lambda p: int(os.path.basename(p)[:-5])):
            eid = int(os.path.basename(p)[:-5])
            if eid in seen:
                continue
            evs = rj(p)
            if not evs:
                continue
            seen.add(eid)
            yield evs[0], NEW_EVENTS.get(eid), folder


def build_pm_legs(rep: Counter, fetch: bool):
    """Legs from Polymarket Gamma events: dicts without series yet (series attached later)."""
    legs = []
    if fetch:
        for eid in NEW_EVENTS:
            p = os.path.join(RAW, "events", f"{eid}.json")
            if not os.path.exists(p):
                wj(p, jget(f"{GAMMA}/events", {"id": eid}) or [])
    for ev, cfg, folder in pm_events():
        eid = int(ev["id"])
        slug = ev.get("slug") or ""
        if eid in SKIP_OLD_EVENTS:
            continue
        groups = []   # (market, cfg2, office, state, district, cycle, date, special, tag)
        if cfg is None and eid in MULTI_STATE_EVENTS:
            office = MULTI_STATE_EVENTS[eid]
            for m in ev.get("markets", []):
                gt = (m.get("groupItemTitle") or "")
                if office == "house":
                    mm = re.match(r"^([A-Z]{2})-(\d+)", gt)
                    state, dist = (mm.group(1), int(mm.group(2))) if mm else (None, None)
                else:
                    state, dist = state_from_name(gt.split(" - ")[0]), None
                if not state:
                    rep["2022 market without state"] += 1
                    continue
                groups.append((m, {"mode": "auto"}, office, state, dist, "2022", None, False, None))
        else:
            if cfg is None:
                meta = meta_from_slug(slug)
                cyc = (ev.get("endDate") or "")[:4]
                if meta is None or cyc not in ELECTION_DAY or meta[1] is None:
                    if eid not in (902823,):
                        rep["old event not an election race (skipped)"] += 1
                        continue
                    meta = ("governor", "MS", None)
                    cyc = "2023"
                if eid == 12058:   # slug says ca-13 but the market is CA-41 (Rollins v Calvert)
                    meta = ("house", "CA", 41)
                cfg = {"mode": "auto", "cycle": cyc, "office": meta[0], "state": meta[1], "district": meta[2]}
            for m in ev.get("markets", []):
                groups.append((m, cfg, cfg["office"], cfg["state"], cfg.get("district"), cfg["cycle"],
                               cfg.get("date"), bool(cfg.get("special")), cfg.get("label_race")))
        cand_best = {}
        for m, c2, office, state, dist, cycle, date, special, tag in groups:
            vol = float(m.get("volumeNum") or 0)
            if vol < MIN_VOLUME:
                rep["pm market below $1000 volume"] += 1
                continue
            cl = classify_market(m, c2)
            if cl is None:
                rep["pm market unclassified"] += 1
                continue
            outs_p = jlist(m.get("outcomePrices"))
            toks = jlist(m.get("clobTokenIds"))
            for party, idx, level, label in cl:
                if idx >= len(toks) or idx >= len(outs_p):
                    rep["pm leg without token/price"] += 1
                    continue
                px = float(outs_p[idx])
                manual = (c2.get("outcome") or {}).get(party)
                if manual is not None:
                    outcome = manual
                elif round(px) in (0, 1) and abs(px - round(px)) < 0.01:
                    outcome = int(round(px))
                else:
                    rep["pm leg with unresolved outcome (dropped)"] += 1
                    continue
                d = date or c2.get("date") or ELECTION_DAY[cycle]
                leg = dict(venue="polymarket", cycle=d[:4] if c2.get("date") else cycle, election_date=d, office=office, state=state,
                           district=dist if dist is not None else "", party=party, market_id=mid_of(m, idx),
                           volume_usd=round(vol, 2), vol_unit="usd", outcome=outcome, us=1, special=int(special),
                           label=label, slug=slug, event_id=eid, token=toks[idx], addr=m.get("marketMakerAddress") or "",
                           out_idx=idx, level=level, race_id=race_id(d[:4] if c2.get("date") else cycle, office, state,
                                                                       dist if dist is not None else None, special, tag))
                if level == "candidate":
                    k = (leg["race_id"], party)
                    if k not in cand_best or vol > cand_best[k][0]:
                        cand_best[k] = (vol, leg)
                    continue
                legs.append(leg)
        for _, leg in cand_best.values():
            legs.append(leg)
    # B races (international runoffs, US specials, WI Supreme Court) from the earlier study
    for r in E.B_RACES:
        folder_ev = E._event(r["event"], False)
        if folder_ev is None:
            rep["B event missing"] += 1
            continue
        office_map = {"intl": "intl", "us-special": "house", "us-offyear": "other"}
        us = 0 if r["office"] == "intl" else 1
        label = r["label"]
        state, dist = ("", "")
        mm = re.match(r"^([A-Z]{2})-(\d+)", label)
        if mm:
            state, dist = mm.group(1), int(mm.group(2))
        elif "Wisconsin" in label:
            state, dist = "WI", "SupremeCourt"
        else:
            state = label.split()[0]
        party_by_name = {"Clayton Fuller": "R", "Shawn Harris": "D", "Matt Van Epps": "R", "Aftyn Behn": "D",
                         "Analilia Mejia": "D", "Joe Hathaway": "R", "Christian Menefee": "D", "Amanda Edwards": "D"}
        for name in r["cands"]:
            m, idx = E._match(folder_ev.get("markets", []), name)
            if m is None:
                rep["B candidate not found"] += 1
                continue
            vol = float(m.get("volumeNum") or 0)
            if vol < E.MIN_VOLUME_B:
                rep["B below volume"] += 1
                continue
            toks = jlist(m.get("clobTokenIds"))
            pxs = jlist(m.get("outcomePrices"))
            party = party_by_name.get(name, "other" if us else name.split()[-1])
            cyc = r["day"][:4]
            special = r["office"] == "us-special"
            legs.append(dict(venue="polymarket", cycle=cyc, election_date=r["day"], office=office_map[r["office"]],
                             state=state, district=dist, party=party, market_id=mid_of(m, idx), volume_usd=round(vol, 2),
                             vol_unit="usd", outcome=int(round(float(pxs[idx]))), us=us, special=int(special), label=name,
                             slug=folder_ev.get("slug") or "", event_id=r["event"], token=toks[idx], addr="",
                             out_idx=idx, level="candidate",
                             race_id=race_id(cyc, office_map[r["office"]], re.sub(r"\W+", "_", state), dist or None,
                                             special, "runoff" if "runoff" in label else None)))
    return legs


# ------------------------------------------------------------------------------------------------ Polymarket histories
def clob_daily(token: str, fetch: bool):
    for base in (RAW, OLD_RAW):
        p = os.path.join(base, "hist", f"{token}_d.json")
        if os.path.exists(p):
            return rj(p).get("history") or []
    if not fetch:
        return None
    d = jget(f"{CLOB}/prices-history", {"market": token, "interval": "max", "fidelity": 1440})
    wj(os.path.join(RAW, "hist", f"{token}_d.json"), d if d is not None else {"history": []})
    return (d or {}).get("history") or []


def clob_hourly(token: str, t0: int, fetch: bool):
    p = os.path.join(RAW, "histh", f"{token}.json")
    if os.path.exists(p):
        return rj(p).get("history") or []
    if not fetch:
        return None
    d = jget(f"{CLOB}/prices-history", {"market": token, "startTs": t0 - 5 * DAY, "endTs": t0 + DAY // 2, "fidelity": 60})
    h = (d or {}).get("history") or []
    wj(p, {"history": [[int(x["t"]), x["p"]] for x in h]})
    return [[int(x["t"]), x["p"]] for x in h]


def merge_series(*parts):
    out = {}
    for part in parts:
        for x in part:
            t, p = (x["t"], x["p"]) if isinstance(x, dict) else x
            out[(int(t) // 60) * 60] = float(p)
    return sorted(out.items())


# ------------------------------------------------------------------------------------------------ chain (FPMM)
class Chain:
    """Polygon block numbers and timestamps, FPMM trades per address; old caches are read, new data written to RAW."""

    def __init__(self, fetch: bool):
        self.fetch = fetch
        self.blocks = {}
        for p in glob.glob(os.path.join(OLD_RAW, "chain", "blocks", "*.json")):
            b = rj(p)
            if b:
                self.blocks[int(os.path.basename(p)[:-5])] = int(b["timestamp"], 16)
        self.newp = os.path.join(RAW, "chain", "blocks.json")
        if os.path.exists(self.newp):
            self.blocks.update({int(k): v for k, v in rj(self.newp).items()})
        self.dirty = False

    def ts(self, n: int) -> int:
        if n not in self.blocks:
            if not self.fetch:
                raise RuntimeError(f"block {n} timestamp not cached")
            b = rpc_retry("eth_getBlockByNumber", [hex(n), False])
            self.blocks[n] = int(b["timestamp"], 16)
            self.dirty = True
        return self.blocks[n]

    def save(self):
        if self.dirty:
            wj(self.newp, {str(k): v for k, v in self.blocks.items()})
            self.dirty = False

    def block_at(self, target: int) -> int:
        pm = os.path.join(RAW, "chain", "day_blocks.json")
        cache = rj(pm) if os.path.exists(pm) else {}
        for old in (os.path.join(OLD_RAW, "chain", "day_blocks.json"),):
            if os.path.exists(old):
                cache = {**rj(old), **cache}
        if str(target) in cache:
            return cache[str(target)]
        lo, hi = 1, 40_000_000
        while lo < hi:
            mid = (lo + hi) // 2
            if self.ts(mid) < target:
                lo = mid + 1
            else:
                hi = mid
        cache2 = rj(pm) if os.path.exists(pm) else {}
        cache2[str(target)] = lo
        wj(pm, cache2)
        self.save()
        return lo


def rpc_retry(method, params, tries=8):
    for i in range(tries):
        try:
            return E._rpc(method, params)
        except Exception as e:  # noqa: BLE001  (transient DNS/network errors)
            if i == tries - 1:
                raise
            time.sleep(15 * (i + 1))


def parse_logs(logs, addrs):
    out = defaultdict(list)
    for lg in logs:
        topics = lg.get("topics", [])
        data = bytes.fromhex(lg["data"][2:])
        if len(topics) != 3 or len(data) != 96 or topics[0] not in (E.TOPIC_BUY, E.TOPIC_SELL):
            continue
        a = lg["address"].lower()
        if a not in addrs:
            continue
        w = [int.from_bytes(data[32 * k:32 * k + 32], "big") for k in range(3)]
        if w[2] <= 0:
            continue
        price = (w[0] - w[1]) / w[2] if topics[0] == E.TOPIC_BUY else (w[0] + w[1]) / w[2]
        if not 0.0 < price < 1.0:
            continue
        out[a].append((int(lg["blockNumber"], 16), int(topics[2], 16), price))
    return out


def chain_group(chain: Chain, addresses: list, day_from: str, day_to: str, fetch: bool, rep: Counter,
                align_old: bool = False):
    """{address_lower: sorted [(ts, outcome_index, price)]} for trades between the two dates (UTC midnight)."""
    addrs = sorted({a for a in addresses})
    low = {a.lower() for a in addrs}
    key = E.keccak256(",".join(sorted(a.lower() for a in addrs)).encode()).hex()[:12]
    a_block = chain.block_at(ts_of_day(day_from, 0))
    b_block = chain.block_at(ts_of_day(day_to, 0))
    old_day = rj(os.path.join(OLD_RAW, "chain", "day_blocks.json"))
    # align to the old grid when the old cache covers this range (2022 markets), else start at a_block
    grid0 = old_day.get(str(ts_of_day("2022-09-01", 0))) if align_old else None
    if grid0 is None:
        grid0 = a_block
    first = grid0 - ((grid0 - a_block + 9999) // 10000) * 10000
    trades = defaultdict(list)
    n_chunks = 0
    lo = first
    while lo <= b_block:
        hi = lo + 9999
        n_chunks += 1
        old_p = os.path.join(OLD_RAW, "chain", "logs", f"{lo}_{hi}_{key}.json")
        new_p = os.path.join(RAW, "chain", "trades", f"{lo}_{hi}_{key}.json")
        if os.path.exists(old_p):
            got = parse_logs(rj(old_p), low)
        elif os.path.exists(new_p):
            got = {a: [tuple(t) for t in v] for a, v in rj(new_p).items()}
        elif fetch:
            logs = rpc_retry("eth_getLogs", [{"address": addrs, "fromBlock": hex(lo), "toBlock": hex(hi)}])
            got = parse_logs(logs, low)
            wj(new_p, {a: v for a, v in got.items()})
        else:
            rep["chain chunk missing"] += 1
            lo += 10000
            continue
        for a, lst in got.items():
            trades[a].extend(lst)
        lo += 10000
    rep["chain chunks"] += n_chunks
    out = {}
    for a, lst in trades.items():
        rows = []
        for blk, idx, price in lst:
            n0 = grid0 + ((blk - grid0) // 10000) * 10000
            t0 = chain.ts(n0)
            t1 = chain.ts(n0 + 10000)
            rows.append((int(t0 + (blk - n0) * (t1 - t0) / 10000), idx, price))
        out[a] = sorted(rows)
    chain.save()
    return out


def chain_series(trades, idx):
    """Series for outcome idx: own trades at their price, complementary outcome's trades at 1 - price."""
    pts = {}
    for t, j, p in trades:
        pts[t] = p if j == idx else 1.0 - p
    return sorted(pts.items())


# ------------------------------------------------------------------------------------------------ Kalshi
KRAW_OLD = os.path.join(OLD_RAW, "kalshi")
KRAW = os.path.join(RAW, "kalshi")


def kcache_name(path, params):
    key = path + "?" + urllib.parse.urlencode(sorted((params or {}).items()))
    slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[:90]
    return f"{slug}__{hashlib.sha1(key.encode()).hexdigest()[:10]}.json"


def kget(path, params, fetch):
    name = kcache_name(path, params)
    for base in (KRAW, KRAW_OLD):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return rj(p).get("response")
    if not fetch:
        return None
    d = jget(KALSHI + path, params)
    wj(os.path.join(KRAW, name), {"status": 200 if d is not None else 404, "response": d})
    return d


def kalshi_series(fetch):
    seen = {}
    for cat in ("Elections", "Politics"):
        r = kget("/series", {"category": cat}, fetch)
        for s in (r or {}).get("series") or []:
            seen[s["ticker"]] = s
    return seen


def office_of_series(s: str):
    if s.startswith("PRESPARTY") and s != "PRESPARTYFULL":
        return "president"
    if s.startswith("GOVPARTY"):
        return "governor"
    if s.startswith("SENATE") and s != "SENATE" and "MOV" not in s:
        return "senate"
    if s.startswith("HOUSE") and s != "HOUSE" and "MOV" not in s:
        return "house"
    return None


# explicit extra series (national and 2025-26 specials / off-year)
KALSHI_EXTRA = {
    "CONTROLH": ("other", "US", None, "house_control"), "CONTROLS": ("other", "US", None, "senate_control"),
    "POPVOTE": ("president", "US", None, "popvote"), "PRES": ("president", "US", None, "winner"),
    "KXMAYORNYCPARTY": ("mayor", "NY", "NYC", None), "KXATTYGENVA": ("other", "VA", "AG", None),
    "KXLTGOVVA": ("other", "VA", "LtGov", None),
    "KXUSELECTIONARIZONA7": ("house", "AZ", 7, None), "KXUSELECTIONTEXAS18": ("house", "TX", 18, None),
    "KXHOUSENJ11SPECIAL": ("house", "NJ", 11, None),
}
KALSHI_DATES = {"HOUSETN7S-25": "2025-12-02", "HOUSEVA11S-25": "2025-09-09", "KXUSELECTIONARIZONA7-25": "2025-09-23",
                "KXUSELECTIONTEXAS18-25": "2026-01-31", "KXHOUSENJ11SPECIAL-26": "2026-04-16",
                "HOUSEFL1-25": "2025-04-01", "HOUSEFL6-25": "2025-04-01"}
KALSHI_SPECIAL = {"HOUSETN7S", "HOUSEVA11S", "HOUSEAZ7S", "HOUSETX18S", "HOUSEFL1", "HOUSEFL6",
                  "KXUSELECTIONARIZONA7", "KXUSELECTIONTEXAS18", "KXHOUSENJ11SPECIAL"}
KALSHI_PRES_CANDS = {"DJT": "R", "KH": "D", "RFK": "I", "JB": "D", "MO": "D", "NH": "R"}


def kalshi_party(m):
    suf = (m.get("ticker") or "").rsplit("-", 1)[-1]
    if suf in ("D", "R", "DEM", "REP", "GOP"):
        return {"DEM": "D", "REP": "R", "GOP": "R"}.get(suf, suf)
    cs = m.get("custom_strike") or {}
    raw = ""
    if isinstance(cs, dict):
        raw = (cs.get("Party") or cs.get("Candidate/Party") or "").strip().lower()
    if raw.startswith("democrat"):
        return "D"
    if raw.startswith("republic"):
        return "R"
    return None


def kalshi_meta(series, event):
    """(office, state, district, tag, special) from the series/event ticker."""
    if series in KALSHI_EXTRA:
        o, st, d, tag = KALSHI_EXTRA[series]
        return o, st, d, tag, series in KALSHI_SPECIAL
    off = office_of_series(series)
    special = series in KALSHI_SPECIAL
    if off == "president":
        mm = re.match(r"^PRESPARTY([A-Z]{2})(\d)?$", series)
        return off, mm.group(1), (int(mm.group(2)) if mm and mm.group(2) else None), None, False
    if off == "governor":
        return off, re.sub(r"^GOVPARTY-?", "", series)[:2], None, None, False
    if off == "senate":
        mm = re.match(r"^SENATE(?:PARTY)?([A-Z]{2})(S|ES)?$", series)
        st = mm.group(1) if mm else series[-2:]
        return off, st, None, None, bool(mm and mm.group(2))
    if off == "house":
        mm = re.match(r"^HOUSE([A-Z]{2})([A-Z]{2}|\d+)(S)?$", series)
        if mm:
            d = mm.group(2)
            return off, mm.group(1), (int(d) if d.isdigit() else d), None, special or bool(mm.group(3))
        return None
    return None


def kalshi_collect(fetch, rep):
    cat = kalshi_series(fetch)
    series = sorted({t for t in cat if office_of_series(t)} | set(KALSHI_EXTRA))
    markets = {}
    for i, s in enumerate(series, 1):
        cursor = None
        while True:
            params = {"series_ticker": s, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = kget("/historical/markets", params, fetch)
            if r is None:
                break
            for m in r.get("markets") or []:
                markets[m["ticker"]] = {**m, "_series": s}
            cursor = r.get("cursor") or None
            if not cursor:
                break
    rep["kalshi series listed"] = len(series)
    rep["kalshi markets listed"] = len(markets)
    return markets


def kalshi_election_date(event):
    if event in KALSHI_DATES:
        return KALSHI_DATES[event]
    suf = event.rsplit("-", 1)[-1]
    y = {"22": "2022", "24": "2024", "25": "2025", "2024": "2024", "2022": "2022"}.get(suf)
    return ELECTION_DAY.get(y) if y else None


def build_kalshi_legs(fetch, rep):
    markets = kalshi_collect(fetch, rep)
    by_event = defaultdict(list)
    for m in markets.values():
        by_event[m["event_ticker"]].append(m)
    legs = []
    for ev, ms in sorted(by_event.items()):
        s = ms[0]["_series"]
        d = kalshi_election_date(ev)
        if d is None:
            rep[f"kalshi event year not covered: {ev.rsplit('-', 1)[-1]}"] += 1
            continue
        if d > "2026-10-24":
            continue
        meta = kalshi_meta(s, ev)
        if meta is None:
            continue
        office, state, dist, tag, special = meta
        cand_mode = s in ("PRES", "KXMAYORNYCPARTY")
        partyset = {kalshi_party(m) for m in ms if kalshi_party(m)}
        if not cand_mode and partyset != {"D", "R"}:
            rep["kalshi race without both D and R markets (dropped)"] += 1
            continue
        for m in ms:
            if m.get("result") not in ("yes", "no"):
                rep["kalshi market unresolved/void (dropped)"] += 1
                continue
            vol = float(m.get("volume_fp") or 0)
            if vol < MIN_VOLUME:
                rep["kalshi market below 1000 contracts (dropped)"] += 1
                continue
            suf = m["ticker"].rsplit("-", 1)[-1]
            if s == "PRES":
                party = KALSHI_PRES_CANDS.get(suf)
                if party is None or vol < 1e6:
                    continue
            elif s == "KXMAYORNYCPARTY":
                party = {"D": "D", "R": "R"}.get(suf, "I")
            else:
                party = kalshi_party(m)
                if party is None:
                    party = "I" if suf in ("I", "IND") else "other"
            cyc = d[:4]
            legs.append(dict(venue="kalshi", cycle=cyc, election_date=d, office=office, state=state,
                             district=dist if dist is not None else "", party=party, market_id=m["ticker"],
                             volume_usd=round(vol, 2), vol_unit="contracts", outcome=1 if m["result"] == "yes" else 0,
                             us=1, special=int(special), label=m.get("yes_sub_title") or "", slug=ev, event_id=ev,
                             token="", addr="", out_idx=0, level="party",
                             race_id=race_id(cyc, office, state, dist, special, tag)))
    return legs


def kalshi_px(c):
    """(price, spread) of one candle: mid of yes_bid/yes_ask close if the book is two-sided and not empty, else last."""
    bid = (c.get("yes_bid") or {}).get("close")
    ask = (c.get("yes_ask") or {}).get("close")
    last = (c.get("price") or {}).get("close")
    if bid is not None and ask is not None:
        b, a = float(bid), float(ask)
        if a - b < 0.5 or (last is None and a - b < 0.9):
            return (a + b) / 2, a - b
    if last is not None:
        return float(last), None
    return None, None


def kalshi_series_for(leg, fetch):
    t0 = ts_of_day(leg["election_date"], 12)
    tk = leg["market_id"]
    pts = {}
    spr = {}
    ok = False
    for interval, start in ((1440, t0 - (WINDOW + 5) * DAY), (60, t0 - 5 * DAY)):
        path = f"/historical/markets/{tk}/candlesticks"
        params = {"start_ts": start, "end_ts": t0 + DAY // 2, "period_interval": interval}
        name = kcache_name(path, params)
        p = os.path.join(KRAW, "c_" + name)
        if os.path.exists(p):
            cs = rj(p)
        elif fetch:
            r = jget(KALSHI + path, params)
            cs = [[int(c["end_period_ts"]), (c.get("yes_bid") or {}).get("close"), (c.get("yes_ask") or {}).get("close"),
                   (c.get("price") or {}).get("close")] for c in (r or {}).get("candlesticks") or []] if r is not None else None
            wj(p, cs if cs is not None else [])
        else:
            cs = None
        if cs is None:
            continue
        ok = True
        for ts, bid, ask, last in cs:
            px, sp = kalshi_px({"yes_bid": {"close": bid}, "yes_ask": {"close": ask}, "price": {"close": last}})
            if px is None:
                continue
            k = (ts // 60) * 60
            pts[k] = px
            spr[k] = sp
    return sorted(pts.items()), spr, ok


# ------------------------------------------------------------------------------------------------ paths and output
DAYS_BEFORE = [0.5] + list(range(0, WINDOW + 1))


def path_rows(series, t0):
    times = [t for t, _ in series]
    vals = [p for _, p in series]
    rows = []
    for d in DAYS_BEFORE:
        cutoff = t0 - int(d * DAY)
        i = bisect.bisect_right(times, cutoff) - 1
        if i < 0:
            rows.append((d, None, None, None))
            continue
        age = cutoff - times[i]
        rows.append((d, vals[i] if age <= MAX_STALE else None, round(age / DAY, 3), times[i]))
    return rows


def main():
    fetch = "--fetch" in sys.argv
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    os.makedirs(RAW, exist_ok=True)
    rep: Counter = Counter()
    legs = []
    if only in (None, "pm", "chain"):
        pm = build_pm_legs(rep, fetch)
        legs += pm
    if only in (None, "kalshi"):
        legs += build_kalshi_legs(fetch, rep)
    print("legs before series:", len(legs), dict(Counter((l["venue"], l["cycle"]) for l in legs)))

    # --- Polymarket FPMM groups (chain)
    chain = Chain(fetch)
    chain_trades = {}
    groups = defaultdict(list)
    for l in legs:
        if l["venue"] == "polymarket" and l["addr"] and l["cycle"] in ("2020", "2021", "2022"):
            groups[(l["cycle"], l["event_id"] in MULTI_STATE_EVENTS)].append(l["addr"])
    if only in (None, "chain"):
        for (cyc, base), addrs in groups.items():
            d = ELECTION_DAY[cyc]
            t0 = ts_of_day(d, 12)
            day_from = date_of(t0 - (WINDOW + 8) * DAY)
            day_to = date_of(t0 + 3 * DAY)
            chain_trades.update(chain_group(chain, addrs, day_from, day_to, fetch, rep, align_old=base))
            print("chain", cyc, "addresses", len(set(addrs)), "with trades", len([a for a in set(addrs) if a.lower() in chain_trades]), flush=True)

    out_legs, out_paths = [], []
    for l in legs:
        t0 = ts_of_day(l["election_date"], 12)
        series, spreads, source = [], {}, None
        if l["venue"] == "polymarket":
            if only in (None, "pm", "chain"):
                d = clob_daily(l["token"], fetch and only in (None, "pm"))
                h = clob_hourly(l["token"], t0, fetch and only in (None, "pm")) if d else None
                series = merge_series(d or [], h or [])
                source = "clob"
                if not any(t0 - WINDOW * DAY <= t <= t0 for t, _ in series) and l["addr"]:
                    tr = chain_trades.get(l["addr"].lower())
                    if tr is not None:
                        series = chain_series(tr, l["out_idx"])
                        source = "chain"
        else:
            series, spreads, ok = kalshi_series_for(l, fetch and only in (None, "kalshi"))
            source = "kalshi_candles"
            if not ok:
                rep["kalshi candles missing"] += 1
        n = sum(1 for t, _ in series if t0 - WINDOW * DAY <= t <= t0)
        if n == 0:
            rep[f"{l['venue']} leg with no observations in the 90 days (dropped)"] += 1
            continue
        l = dict(l, source=source, n_points=n)
        out_legs.append(l)
        for d, p, age, tobs in path_rows(series, t0):
            sp = spreads.get(tobs) if (tobs and p is not None) else None
            out_paths.append((l["venue"], l["market_id"], d, "" if p is None else round(p, 5),
                              "" if age is None else age, "" if sp is None else round(sp, 4)))

    out_legs.sort(key=lambda l: (l["venue"], l["cycle"], l["race_id"], l["party"], l["market_id"]))
    cols = ["venue", "cycle", "election_date", "race_id", "office", "state", "party", "market_id", "volume_usd",
            "outcome", "us", "source", "n_points", "district", "special", "vol_unit", "label", "event_id", "slug"]
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "legs_all.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for l in out_legs:
            w.writerow([l.get(c, "") for c in cols])
    keep = {(l["venue"], l["market_id"]) for l in out_legs}
    with open(os.path.join(OUT, "paths.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["venue", "market_id", "days_before", "price", "stale_days", "spread"])
        for r in out_paths:
            if (r[0], r[1]) in keep:
                w.writerow(r)
    print("legs written", len(out_legs), "paths rows", len(out_paths))
    tab = Counter((l["venue"], l["cycle"], l["office"], l["us"]) for l in out_legs)
    for k, v in sorted(tab.items()):
        print("  ", k, v)
    races = {(l["venue"], l["cycle"], l["race_id"]) for l in out_legs}
    print("races:", len(races), dict(Counter((v, c) for v, c, _ in races)))
    print("sources:", dict(Counter((l["venue"], l["source"]) for l in out_legs)))
    print("report:")
    for k, v in sorted(rep.items()):
        print("  ", k, v)
    print("http requests this run:", _http["n"], "429s:", _http["429"])


if __name__ == "__main__":
    main()
