"""Build data/race_map.json: every SIG race -> matching Polymarket and Kalshi markets, per party.

    python3 -m sigbot.venues.mapping            # rebuild (about 1-2 minutes of public outside reads, zero SIG reads)

Matching is on office + state (+ district for House) through the outside venues' own naming conventions, then verified
by title: the outside title must contain the state name (or the district code) and the office word. Each race entry:

    {"office", "state", "district",
     "polymarket": {"event_id", "slug", "title", "verified", "legs": {party: [{market_id, token, title, liquidity}]}},
     "kalshi":     {"series", "event", "title", "verified", "legs": {party: [{ticker, title, candidate}]}},
     "unmatched": [venue, ...]}

Chamber races ("U.S. Senate", "U.S. House") map to Polymarket's chamber events and to Kalshi's balance-of-power combo
(derived; see kalshi.py). Unmatched races are listed under "unmatched" in the file and printed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

from .http import Http, VenueError
from .kalshi import COMBO_SERIES, HOUSE_SERIES, Kalshi
from .polymarket import CHAMBER_EVENTS, Polymarket
from .states import STATES, parse_race, slug

log = logging.getLogger("sigbot.venues.mapping")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAP_PATH = os.path.join(HERE, "data", "race_map.json")


def load_race_names() -> list[str]:
    """SIG race names from the cached markets file (no SIG reads)."""
    from ..universe import Universe
    rows = json.load(open(os.path.join(HERE, "data", "markets.json")))
    return sorted(Universe.from_markets(rows).races.keys())


def _pm_event_for(info: dict, by_slug: dict) -> Optional[dict]:
    off, st = info["office"], info["state"]
    if off == "chamber":
        return by_slug.get(CHAMBER_EVENTS[info["chamber"]])
    if off == "senate":
        pat = re.compile(r"^" + re.escape(slug(st)) + r"-senate-election-winner(-\d+)?$")
    elif off == "governor":
        pat = re.compile(r"^" + re.escape(slug(st)) + r"-governor-(winner-2026|election-winner|election-2026)$")
    elif off == "house":
        code = f"{info['abbr'].lower()}-{info['district']:02d}"
        pat = re.compile(r"^(" + code + r"-house-election-winner|which-party-will-win-the-house-race-for-the-" + code + r"-seat)$")
    else:
        return None
    cands = [e for s, e in by_slug.items() if pat.match(s)]
    if not cands:
        return None
    return max(cands, key=lambda e: float(e.get("liquidity") or 0))


def _pm_verify(info: dict, ev: dict) -> bool:
    t = (ev.get("title") or "") + " " + " ".join(m.get("question") or "" for m in ev.get("markets", [])[:3])
    if info["office"] == "chamber":
        return info["chamber"].lower() in t.lower()
    if info["office"] == "house":
        code = f"{info['abbr']}-{info['district']:02d}"
        return code.lower() in t.lower() or f"{info['abbr']}-{info['district']}".lower() in t.lower()
    return info["state"].lower() in t.lower() and info["office"] in t.lower()


def build(save: bool = True) -> dict:
    http = Http()
    pm, ks = Polymarket(http), Kalshi(http, os.path.join(HERE, "data"))
    t0 = time.time()
    races = load_race_names()
    events = pm.discover()
    by_slug = {e["slug"]: e for e in events}
    log.info("polymarket: %d events in %.1fs", len(events), time.time() - t0)

    # Kalshi: Senate/Governor series by (state, office); House from one series; chamber from the combo.
    t1 = time.time()
    rs = ks.race_series()
    house_mk = ks.markets_of_series(HOUSE_SERIES)
    house_by_event: dict = {}
    for m in house_mk:
        house_by_event.setdefault(m["event_ticker"], []).append(m)
    series_cache: dict = {}

    out: dict = {"built_at": time.time(), "races": {}, "unmatched": {"polymarket": [], "kalshi": []}}
    for name in races:
        info = parse_race(name)
        entry: dict = {"office": info["office"], "state": info["state"], "district": info["district"], "unmatched": []}
        # ---------------- Polymarket
        ev = _pm_event_for(info, by_slug)
        if ev:
            legs = pm.party_legs(ev)
            entry["polymarket"] = {"event_id": str(ev["id"]), "slug": ev["slug"], "title": ev.get("title"),
                                   "verified": _pm_verify(info, ev), "legs": legs}
            if not legs:
                entry["unmatched"].append("polymarket")
        else:
            entry["unmatched"].append("polymarket")
        # ---------------- Kalshi
        k = None
        if info["office"] == "chamber":
            combo = {m["ticker"]: m for m in ks.markets_of_series(COMBO_SERIES)} if "combo" not in series_cache else series_cache["combo"]
            series_cache["combo"] = combo
            tick = {c: f"{COMBO_SERIES}-27FEB-{c}" for c in ("DD", "DR", "RD", "RR")}
            if all(t in combo for t in tick.values()):
                # ticker suffix: House letter first, Senate letter second
                if info["chamber"] == "senate":
                    legs = {"D": [{"ticker": tick["DD"]}, {"ticker": tick["RD"]}], "R": [{"ticker": tick["RR"]}, {"ticker": tick["DR"]}]}
                else:
                    legs = {"D": [{"ticker": tick["DD"]}, {"ticker": tick["DR"]}], "R": [{"ticker": tick["RR"]}, {"ticker": tick["RD"]}]}
                k = {"series": COMBO_SERIES, "event": f"{COMBO_SERIES}-27FEB", "title": "Congress balance of power (derived)",
                     "verified": True, "derived": True, "legs": legs}
        elif info["office"] == "house":
            # competitive districts have their own series (HOUSEAZ1, HOUSENE2, ...); the rest live in KXHOUSERACE
            code_pad, code = f"{info['abbr']}-{info['district']:02d}", f"{info['abbr']}-{info['district']}"
            for st, mk in ((f"HOUSE{info['abbr']}{info['district']}", None), (HOUSE_SERIES, house_by_event.get(f"{HOUSE_SERIES}-{info['abbr']}{info['district']:02d}-26", []))):
                if mk is None:
                    if st not in series_cache:
                        try:
                            series_cache[st] = ks.markets_of_series(st)
                        except VenueError as e:
                            log.warning("kalshi series %s: %s", st, e)
                            series_cache[st] = []
                    mk = series_cache[st]
                ev_k = Kalshi.current_event(mk)
                legs = Kalshi.party_legs(mk, ev_k)
                if legs:
                    t = next((m.get("title") for m in mk if m.get("event_ticker") == ev_k), "") or ""
                    k = {"series": st, "event": ev_k, "title": t, "verified": (code_pad in t or code + "?" in t or code + " " in t), "legs": legs}
                    break
        elif info["office"] in ("senate", "governor"):
            best = None
            for st in rs.get((info["state"], info["office"]), []):
                if st not in series_cache:
                    try:
                        series_cache[st] = ks.markets_of_series(st)
                    except VenueError as e:
                        log.warning("kalshi series %s: %s", st, e)
                        series_cache[st] = []
                mk = series_cache[st]
                ev_k = Kalshi.current_event(mk)
                legs = Kalshi.party_legs(mk, ev_k)
                if not legs:
                    continue
                vol = sum(float(m.get("volume_fp") or 0) for m in mk if m.get("event_ticker") == ev_k)
                t = next((m.get("title") for m in mk if m.get("event_ticker") == ev_k), "") or ""
                verified = info["state"].lower() in t.lower() and (info["office"] in t.lower() or (info["office"] == "governor" and "governorship" in t.lower()))
                cand = {"series": st, "event": ev_k, "title": t, "verified": verified, "legs": legs, "volume": vol}
                if best is None or (cand["verified"], vol) > (best["verified"], best["volume"]):
                    best = cand
            k = best
        if k:
            entry["kalshi"] = k
        else:
            entry["unmatched"].append("kalshi")
        for v in entry["unmatched"]:
            out["unmatched"][v].append(name)
        out["races"][name] = entry
    log.info("kalshi mapping in %.1fs (%d series fetched)", time.time() - t1, len(series_cache))
    if save:
        os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
        json.dump(out, open(MAP_PATH, "w"), indent=1)
    return out


def load(max_age_sec: float = 7 * 86400) -> dict:
    if os.path.exists(MAP_PATH) and time.time() - os.path.getmtime(MAP_PATH) < max_age_sec:
        return json.load(open(MAP_PATH))
    return build()


def coverage(m: dict) -> dict:
    by: dict = {}
    for name, e in m["races"].items():
        o = e["office"]
        c = by.setdefault(o, {"races": 0, "polymarket": 0, "kalshi": 0, "either": 0, "both": 0, "unverified": []})
        c["races"] += 1
        p = "polymarket" in e and e["polymarket"]["legs"]
        k = "kalshi" in e and e["kalshi"]["legs"]
        c["polymarket"] += bool(p)
        c["kalshi"] += bool(k)
        c["either"] += bool(p or k)
        c["both"] += bool(p and k)
        for v in ("polymarket", "kalshi"):
            if v in e and not e[v].get("verified"):
                c["unverified"].append(f"{name} [{v}: {e[v].get('title')}]")
    return by


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    m = build()
    for office, c in coverage(m).items():
        print(f"{office:9s} races={c['races']:3d} polymarket={c['polymarket']:3d} kalshi={c['kalshi']:3d} either={c['either']:3d} both={c['both']:3d}")
        for u in c["unverified"]:
            print("   unverified:", u)
    print("unmatched polymarket:", m["unmatched"]["polymarket"])
    print("unmatched kalshi:", m["unmatched"]["kalshi"])
