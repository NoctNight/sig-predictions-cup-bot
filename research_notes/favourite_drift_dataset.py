"""Resolved two-party election legs for the favourite-drift study, built from the Polymarket raw cache.

Input  (written by research_notes/favourite_drift_fetch.py):
    data/polymarket_history/raw/events/<id>.json       Gamma event list (one event)
    data/polymarket_history/raw/hist/<token>_d.json    daily CLOB history {"history": [{"t", "p"}]}
    data/polymarket_history/raw/hist/<token>_h.json    hourly CLOB history, last 45 days before election day
Output:
    data/polymarket_history/legs.csv          one row per leg, prices at horizons 30, 25, 20, 13, 7, 3, 1, 0 days
    data/polymarket_history/daily_paths.csv   long format, one row per leg per days_before in 30..0

A leg is one party (D or R) of one market. Multi-market events (e.g. the 2022 Senate event holds one market
per state) are keyed on market_id as well as event_id, so market_id is also written to daily_paths.csv.

Classification:
  * Yes/No party markets ("Will a Democrat win ...", or groupItemTitle Democrat/Republican): token 0.
  * Two-outcome markets: outcomes Democrat/Republican map to index 0/1; candidate-surname outcomes map to a
    party through the "Surname (D)" / "Surname (R)" label in the question. Both legs share the market volume.
  * Candidate Yes/No markets labelled "(D)"/"(R)" (2025 governors): one leg per party per event, the
    highest-volume candidate. "Person A/B/C" and "Other" placeholders and other-party markets are dropped.
  * Markets with volumeNum < MIN_VOLUME are skipped, because the fetch did not download their histories.

Prices: daily and hourly observations are merged (hourly wins on equal timestamps). For days_before d, the
price is the last observation with t <= T - d days, where T is election day 12:00 UTC. first_obs and last_obs
are over the whole merged series. n_obs_30d counts observations with T-30d <= t <= T.

Run as a script to rebuild both CSVs and print a review summary. Importable: build_legs(), write_csvs().
"""
from __future__ import annotations

import bisect
import calendar
import csv
import glob
import json
import os
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "polymarket_history")
RAW = os.path.join(DATA, "raw")
LEGS_CSV = os.path.join(DATA, "legs.csv")
PATHS_CSV = os.path.join(DATA, "daily_paths.csv")

DAY = 86400
MIN_VOLUME = 1000.0
HORIZONS = [30, 25, 20, 13, 7, 3, 1, 0]
PATH_DAYS = list(range(30, -1, -1))
ELECTION_DAY = {"2022": "2022-11-08", "2023": "2023-11-07", "2024": "2024-11-05", "2025": "2025-11-04"}
LEG_FIELDS = (["event_id", "slug", "cycle", "office", "party", "market_id", "token", "volume_usd", "outcome"]
              + [f"p{h}" for h in HORIZONS] + ["n_obs_30d", "first_obs", "last_obs"])
PATH_FIELDS = ["event_id", "slug", "cycle", "office", "party", "market_id", "days_before", "price"]

_OTHER_Q = re.compile(r"^Will a candidate from another party win", re.I)
_PLACEHOLDER_Q = re.compile(r"^Will (person [A-C]|other) win", re.I)
_PLACEHOLDER_GT = re.compile(r"^(Person [A-Z]|Other)$")
_PARTY_Q = re.compile(r"^Will a (Democrat|Republican) win", re.I)
_LABEL_END = re.compile(r"\(([DR])\)\s*$")
_PARTY_LETTER = {"democrat": "D", "republican": "R"}


def _json(path: str):
    with open(path) as f:
        return json.load(f)


def _json_field(m: dict, key: str) -> list:
    """Gamma stores outcomes, outcomePrices and clobTokenIds as JSON-encoded strings."""
    v = m.get(key)
    return json.loads(v) if isinstance(v, str) else (v or [])


def _history(token: str, kind: str):
    """(points, status) for raw/hist/<token>_<kind>.json. status is ok, empty or missing."""
    path = os.path.join(RAW, "hist", f"{token}_{kind}.json")
    if not os.path.exists(path):
        return [], "missing"
    pts = _json(path).get("history") or []
    return pts, ("ok" if pts else "empty")


def _series(token: str, rep: dict, ctx: str) -> list[tuple[int, float]]:
    """Merged daily and hourly observations, sorted by timestamp, hourly winning ties."""
    merged: dict[int, float] = {}
    for kind in ("d", "h"):
        pts, status = _history(token, kind)
        if status != "ok":
            rep["history_gaps"].append({"ctx": ctx, "token": token, "kind": kind, "status": status})
        for p in pts:
            merged[int(p["t"])] = float(p["p"])
    return sorted(merged.items())


def _election_ts(cycle: str) -> int:
    return calendar.timegm(time.strptime(ELECTION_DAY[cycle] + " 12:00:00", "%Y-%m-%d %H:%M:%S"))


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _office(slug: str):
    s = slug.lower()
    if "senate" in s:
        return "senate"
    if "governor" in s or "gubernatorial" in s:
        return "governor"
    if "house" in s or re.match(r"^[a-z]{2}-\d{1,2}-election", s):
        return "house"
    if "presidential" in s:
        return "president"
    return None


def _classify_market(m: dict):
    """Return ("ok", [(party, outcome_index, level)]), ("skip", reason) or ("bad", reason).

    level is "party" for a market that is the party itself, "candidate" for a candidate Yes/No market.
    """
    q = (m.get("question") or "").strip()
    gt = (m.get("groupItemTitle") or "").strip()
    outcomes = [str(o).strip() for o in _json_field(m, "outcomes")]
    if outcomes == ["Yes", "No"]:
        if _OTHER_Q.match(q):
            return "skip", "other-party market"
        if _PLACEHOLDER_Q.match(q) or _PLACEHOLDER_GT.match(gt):
            return "skip", "placeholder (Person A/B/C or Other)"
        pm = _PARTY_Q.match(q)
        if pm:
            return "ok", [(_PARTY_LETTER[pm.group(1).lower()], 0, "party")]
        if gt.lower().startswith("democrat"):
            return "ok", [("D", 0, "party")]
        if gt.lower().startswith("republican"):
            return "ok", [("R", 0, "party")]
        lab = _LABEL_END.search(gt)
        if lab:
            return "ok", [(lab.group(1), 0, "candidate")]
        return "bad", "Yes/No market with no party in question or groupItemTitle"
    if outcomes == ["Democrat", "Republican"]:
        return "ok", [("D", 0, "party"), ("R", 1, "party")]
    if len(outcomes) == 2:
        legs = []
        for i, name in enumerate(outcomes):
            hit = re.search(r"\b" + re.escape(name) + r"\s*\(([DR])\)", q) if name else None
            if not hit:
                return "bad", "two-outcome market: outcome not followed by (D)/(R) in question"
            legs.append((hit.group(1), i, "party"))
        if len({p for p, _, _ in legs}) == 2:
            return "ok", legs
        return "bad", "two-outcome market: both outcomes map to the same party"
    return "bad", f"unrecognised outcomes {outcomes}"


def _describe(ev: dict, m: dict, reason: str) -> dict:
    return {"slug": ev.get("slug"), "market_id": m.get("id"), "question": m.get("question"),
            "outcomes": m.get("outcomes"), "groupItemTitle": m.get("groupItemTitle"),
            "volume": round(float(m.get("volumeNum") or 0), 2), "reason": reason}


def _make_leg(ev: dict, cycle: str, office: str, party: str, m: dict, idx: int, vol: float, rep: dict) -> dict:
    slug = ev.get("slug") or ""
    token = _json_field(m, "clobTokenIds")[idx]
    px = float(_json_field(m, "outcomePrices")[idx])
    if px not in (0.0, 1.0):
        rep["unresolved"].append(_describe(ev, m, f"outcome price {px} is not 0 or 1"))
    series = _series(token, rep, f"{slug} market {m.get('id')} {party}")
    times = [t for t, _ in series]
    vals = [p for _, p in series]
    T = _election_ts(cycle)
    path = {}
    for d in PATH_DAYS:
        i = bisect.bisect_right(times, T - d * DAY) - 1
        path[d] = round(vals[i], 6) if i >= 0 else None
    n30 = bisect.bisect_right(times, T) - bisect.bisect_left(times, T - 30 * DAY)
    return {
        "event_id": str(ev.get("id")), "slug": slug, "cycle": cycle, "office": office, "party": party,
        "market_id": str(m.get("id")), "token": token, "volume_usd": round(vol, 2),
        "outcome": round(px), **{f"p{h}": path[h] for h in HORIZONS},
        "n_obs_30d": n30, "first_obs": _date(times[0]) if times else "",
        "last_obs": _date(times[-1]) if times else "", "path": path,
    }


def build_legs(report: dict | None = None) -> list[dict]:
    """Return the sorted list of leg dicts. If `report` is passed, it is filled with review lists."""
    rep = report if report is not None else {}
    rep.update(events=0, unclassified=[], dropped=Counter(), skipped_volume=0, history_gaps=[], unresolved=[])
    legs = []
    files = sorted(glob.glob(os.path.join(RAW, "events", "*.json")),
                   key=lambda p: int(os.path.splitext(os.path.basename(p))[0]))
    for path in files:
        evs = _json(path)
        if not evs:
            rep["unclassified"].append({"slug": os.path.basename(path), "reason": "empty event file"})
            continue
        ev = evs[0]
        rep["events"] += 1
        slug = ev.get("slug") or ""
        cycle = (ev.get("endDate") or "")[:4]
        office = _office(slug)
        if office is None or cycle not in ELECTION_DAY:
            rep["unclassified"].append({"slug": slug, "reason": f"event office/cycle not recognised ({office}, {cycle})"})
            continue
        cands = []  # (party, idx, level, volume, market)
        for m in ev.get("markets", []):
            vol = float(m.get("volumeNum") or 0)
            if vol < MIN_VOLUME:
                rep["skipped_volume"] += 1
                continue
            status, payload = _classify_market(m)
            if status == "skip":
                rep["dropped"][payload] += 1
            elif status == "bad":
                rep["unclassified"].append(_describe(ev, m, payload))
            else:
                for party, idx, level in payload:
                    cands.append((party, idx, level, vol, m))
        best: dict[str, tuple] = {}
        for party, idx, level, vol, m in cands:
            if level == "party":
                legs.append(_make_leg(ev, cycle, office, party, m, idx, vol, rep))
            elif party not in best or vol > best[party][3]:
                if party in best:
                    rep["dropped"]["lower-volume candidate in same party"] += 1
                best[party] = (party, idx, level, vol, m)
            else:
                rep["dropped"]["lower-volume candidate in same party"] += 1
        for party, idx, _, vol, m in best.values():
            legs.append(_make_leg(ev, cycle, office, party, m, idx, vol, rep))
    legs.sort(key=lambda l: (l["cycle"], l["office"], l["slug"], l["party"], l["market_id"]))
    return legs


def _fmt(v):
    return "" if v is None else v


def write_csvs(legs: list[dict] | None = None) -> tuple[str, str]:
    """Write legs.csv and daily_paths.csv. Builds the legs from the cache if none are passed."""
    if legs is None:
        legs = build_legs()
    os.makedirs(DATA, exist_ok=True)
    with open(LEGS_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(LEG_FIELDS)
        for leg in legs:
            w.writerow([_fmt(leg[k]) for k in LEG_FIELDS])
    with open(PATHS_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(PATH_FIELDS)
        for leg in legs:
            for d in PATH_DAYS:
                w.writerow([leg["event_id"], leg["slug"], leg["cycle"], leg["office"], leg["party"],
                            leg["market_id"], d, _fmt(leg["path"][d])])
    return LEGS_CSV, PATHS_CSV


def main():
    rep: dict = {}
    legs = build_legs(rep)
    write_csvs(legs)
    print(f"events read {rep['events']}, legs {len(legs)}, markets skipped for volume < {MIN_VOLUME:.0f}: "
          f"{rep['skipped_volume']}")
    print("legs by cycle x office:")
    for (c, o), n in sorted(Counter((l["cycle"], l["office"]) for l in legs).items()):
        print(f"  {c} {o:<10} {n}")
    print("legs by party:", dict(Counter(l["party"] for l in legs)))
    in20 = sum(1 for l in legs if l["p20"] is not None and 0.75 <= l["p20"] <= 0.95)
    in13 = sum(1 for l in legs if l["p13"] is not None and 0.75 <= l["p13"] <= 0.95)
    print(f"p20 in [0.75, 0.95]: {in20}   p13 in [0.75, 0.95]: {in13}")
    vols = sorted(l["volume_usd"] for l in legs)
    if vols:
        print(f"volume_usd min {vols[0]:,.0f}  median {statistics.median(vols):,.0f}  max {vols[-1]:,.0f}")
    print("dropped by rule:", dict(rep["dropped"]))
    print(f"unclassified markets: {len(rep['unclassified'])}")
    for u in rep["unclassified"]:
        print("  ", u)
    print(f"unresolved outcome prices: {len(rep['unresolved'])}")
    for u in rep["unresolved"]:
        print("  ", u)
    gaps = Counter((g["kind"], g["status"]) for g in rep["history_gaps"])
    print("history files not ok (kind, status):", dict(gaps))
    for g in rep["history_gaps"]:
        print("  ", g)
    if legs:
        print("example rows:")
        for i in sorted({0, len(legs) // 2, len(legs) - 1}):
            print("  ", {k: legs[i][k] for k in LEG_FIELDS})
    print("wrote", LEGS_CSV, "and", PATHS_CSV)


if __name__ == "__main__":
    main()
