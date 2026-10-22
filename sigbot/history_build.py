"""Assemble the historical race dataset used by sigbot.correlation_model (stdlib only, no network).

Inputs (data/history/):
  raw/medsl_1976-2024-senate.csv      MIT Election Data + Science Lab, "U.S. Senate statewide 1976-2024" (doi:10.7910/DVN/PEJ5QU)
  raw/medsl_1976-2024-president.csv   MEDSL "U.S. President 1976-2024" (doi:10.7910/DVN/42MVDX) -> state partisan lean
  governor_results.csv                gubernatorial generals 2005-2025 from Wikipedia per-year pages (built by a side script)
  house_results.csv                   House generals 2006-2024 by district from Wikipedia per-year pages (optional)
  ../fair/raw_polls_538.csv           FiveThirtyEight pollster-ratings raw_polls (1998-2023): final-21-day race poll averages
  NATIONAL (below)                    generic-ballot final polling average and House popular vote by year (sources in the table)

Output: data/history/races.csv, one row per general-election race (Senate, Governor, House), with
  year, office, state_po, district, seat, pres_year, special, contested, dem_pct, rep_pct, margin (D - R, points of total vote),
  winner, inc_party (D/R/I/''), inc_running (0/1), open_seat, pvi (state lean from the two previous presidential elections,
  D-positive, two-party), region, division, gb_poll (generic ballot D-R final average), natl_pv (House popular vote D-R),
  pres_party (White House party), poll_margin (final-21-day race poll average D-R, '' if unpolled), n_polls.
And data/history/national_environment.csv, data/history/state_lean.csv.

Usage: python3 -m sigbot.history_build
"""
from __future__ import annotations

import csv
import os
import re
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(HERE, "data", "history")

# year: (generic ballot final-21-day average D-R [538 raw_polls House-G-US, 2024: 538 average 46.5-45.7 on 2024-11-04],
#        House popular vote D-R [538 raw_polls margin_actual; 2024 Wikipedia 47.19-49.75], White House party, presidential year)
NATIONAL = {
    1998: (1.9, -0.9, "D", 0), 2000: (2.0, -0.3, "D", 1), 2002: (0.6, -4.7, "R", 0), 2004: (2.1, -2.6, "R", 1),
    2006: (14.2, 7.9, "R", 0), 2008: (8.7, 10.5, "R", 1), 2010: (-6.8, -6.5, "D", 0), 2012: (0.3, 1.1, "D", 1),
    2014: (-2.5, -5.6, "D", 0), 2016: (2.6, -1.0, "D", 1), 2018: (7.3, 8.5, "R", 0), 2020: (7.8, 3.0, "R", 1),
    2022: (-0.3, -2.6, "D", 0), 2024: (0.8, -2.6, "D", 1), 2026: (8.7, None, "R", 0),
}
# odd-year governor races use the previous even year's environment is wrong (mid-cycle); they get their own entries where known
NATIONAL_ODD = {2005: (None, None, "R", 0), 2007: (None, None, "R", 0), 2009: (None, None, "D", 0), 2011: (None, None, "D", 0),
                2013: (None, None, "D", 0), 2015: (None, None, "D", 0), 2017: (None, None, "R", 0), 2019: (None, None, "R", 0),
                2021: (None, None, "D", 0), 2023: (None, None, "D", 0), 2025: (None, None, "R", 0)}

DIVISION = {
    "New England": ["CT", "ME", "MA", "NH", "RI", "VT"], "Mid-Atlantic": ["NJ", "NY", "PA"],
    "East North Central": ["IL", "IN", "MI", "OH", "WI"], "West North Central": ["IA", "KS", "MN", "MO", "NE", "ND", "SD"],
    "South Atlantic": ["DE", "FL", "GA", "MD", "NC", "SC", "VA", "WV", "DC"], "East South Central": ["AL", "KY", "MS", "TN"],
    "West South Central": ["AR", "LA", "OK", "TX"], "Mountain": ["AZ", "CO", "ID", "MT", "NV", "NM", "UT", "WY"],
    "Pacific": ["AK", "CA", "HI", "OR", "WA"],
}
REGION = {"New England": "Northeast", "Mid-Atlantic": "Northeast", "East North Central": "Midwest", "West North Central": "Midwest",
          "South Atlantic": "South", "East South Central": "South", "West South Central": "South", "Mountain": "West", "Pacific": "West"}
STATE_DIV = {s: d for d, ss in DIVISION.items() for s in ss}
STATE_REGION = {s: REGION[d] for s, d in STATE_DIV.items()}

CAUCUS_D = {"SANDERS:B", "KING:A"}   # independents who caucus with the Democrats: Sanders (VT), King (ME); matched on last name + initial


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _key(name: str) -> str:
    """Last name + first initial, for incumbent matching across files."""
    n = re.sub(r"[^A-Z ]", "", name.upper().replace(",", " "))
    parts = [p for p in n.split() if p not in ("JR", "SR", "II", "III", "IV")]
    if not parts:
        return ""
    return parts[-1] + ":" + parts[0][:1]


def state_lean() -> dict:
    """(state_po, election_year) -> two-party D lean vs nation, averaged over the two preceding presidential elections."""
    path = os.path.join(HIST, "raw", "medsl_1976-2024-president.csv")
    dv = defaultdict(float)
    rv = defaultdict(float)
    for r in csv.DictReader(open(path, encoding="utf-8")):
        y, s, p = int(r["year"]), r["state_po"], r["party_simplified"]
        v = _f(r["candidatevotes"]) or 0.0
        if p == "DEMOCRAT":
            dv[(y, s)] += v
        elif p == "REPUBLICAN":
            rv[(y, s)] += v
    years = sorted({y for y, _ in dv})
    lean = {}
    for y in years:
        nd = sum(v for (yy, _), v in dv.items() if yy == y)
        nr = sum(v for (yy, _), v in rv.items() if yy == y)
        nat = 100.0 * (nd - nr) / (nd + nr)
        for s in {s for (yy, s) in dv if yy == y}:
            d, r = dv[(y, s)], rv[(y, s)]
            if d + r > 0:
                lean[(y, s)] = 100.0 * (d - r) / (d + r) - nat
    out = {}
    for ey in range(1978, 2027):
        prev = [y for y in years if y < ey][-2:]
        if not prev:
            continue
        for s in {s for (_, s) in lean}:
            vals = [lean[(y, s)] for y in prev if (y, s) in lean]
            if vals:
                out[(s, ey)] = sum(vals) / len(vals)
    return out


def senate_rows() -> list:
    path = os.path.join(HIST, "raw", "medsl_1976-2024-senate.csv")
    by = defaultdict(lambda: defaultdict(lambda: {"votes": 0.0, "parties": set(), "writein": False}))
    total = {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        if r["mode"].lower() != "total":
            continue
        stage = r["stage"].lower()
        if stage.startswith("pri") or stage == "pre":
            continue
        yr = int(r["year"])
        if "runoff" in stage and yr % 2 == 1:
            yr -= 1                      # January runoff (Georgia 2021) belongs to the previous November's race
        key = (yr, r["state_po"], r["special"].lower() == "true", "runoff" if "runoff" in stage else "gen")
        c = by[key][r["candidate"].strip().upper()]
        c["votes"] += _f(r["candidatevotes"]) or 0.0
        c["parties"].add(r["party_simplified"])
        if r["party_detailed"] in ("DEMOCRAT", "REPUBLICAN"):
            c["parties"].add(r["party_detailed"])
        total[key] = _f(r["totalvotes"]) or 0.0
    # runoff supersedes the general of the same race
    races = {}
    for (y, s, sp, stage), cands in by.items():
        k = (y, s, sp)
        if k in races and stage == "gen":
            continue
        races[k] = (stage, cands, total[(y, s, sp, stage)])
    rows = []
    for (y, s, sp), (stage, cands, tot) in sorted(races.items()):
        rows.append(_race_row(y, "senate", s, "statewide" if not sp else "special", sp, cands, tot, stage))
    _senate_incumbency(rows)
    return rows


def _party_of(cand: str, parties: set) -> str:
    if _key(cand) in CAUCUS_D:
        return "D"
    if "DEMOCRAT" in parties:
        return "D"
    if "REPUBLICAN" in parties:
        return "R"
    return "I"


def _race_row(y, office, s, district, special, cands, tot, stage) -> dict:
    best = {}
    for name, c in cands.items():
        p = _party_of(name, c["parties"])
        if p not in best or c["votes"] > best[p][1]:
            best[p] = (name, c["votes"])
    tot = tot or sum(c["votes"] for c in cands.values()) or 1.0
    ranked = sorted(cands.items(), key=lambda kv: -kv[1]["votes"])
    winner_name = ranked[0][0] if ranked else ""
    winner = _party_of(winner_name, cands[winner_name]["parties"]) if winner_name else ""
    d = best.get("D", ("", 0.0))[1]
    r = best.get("R", ("", 0.0))[1]
    contested = int("D" in best and "R" in best and d > 0 and r > 0 and tot > 1 and winner in ("D", "R"))
    return {"year": y, "office": office, "state_po": s, "district": district, "seat": f"{s}-{district}", "special": int(special),
            "contested": contested, "dem_pct": round(100 * d / tot, 2) if "D" in best else "", "rep_pct": round(100 * r / tot, 2) if "R" in best else "",
            "margin": round(100 * (d - r) / tot, 2) if contested else "", "winner": winner, "winner_name": winner_name,
            "dem_name": best.get("D", ("",))[0], "rep_name": best.get("R", ("",))[0], "stage": stage,
            "inc_party": "", "inc_running": "", "open_seat": ""}


def _senate_incumbency(rows: list) -> None:
    """Regular election: incumbent party = party of the last general winner in that state of the same class (6 years earlier,
    or a later special whose winner is a current candidate). Running = that winner's name is on the ballot."""
    wins = defaultdict(list)   # state -> [(year, special, winner_key, winner_party)]
    for r in rows:
        if r["winner"]:
            wins[r["state_po"]].append((r["year"], r["special"], _key(r["winner_name"]), r["winner"]))
    for r in rows:
        y, s = r["year"], r["state_po"]
        cand_keys = {_key(n) for n in (r["dem_name"], r["rep_name"], r["winner_name"]) if n}
        prev = None
        for (py, psp, pk, pp) in wins[s]:
            if py >= y:
                continue
            if not r["special"] and not psp and (y - py) % 6 == 0 and y - py <= 6:
                prev = (py, pk, pp) if prev is None or py > prev[0] else prev
        # a special winner who is on this ballot is the incumbent (appointed-then-elected or special-then-regular)
        for (py, psp, pk, pp) in wins[s]:
            if y - 6 < py < y and pk in cand_keys:
                prev = (py, pk, pp)
        if prev is None and r["special"]:
            r["inc_party"], r["inc_running"], r["open_seat"] = "", 0, 1
            continue
        if prev is None:
            continue
        r["inc_party"] = prev[2]
        r["inc_running"] = int(prev[1] in cand_keys)
        r["open_seat"] = 1 - r["inc_running"]


def governor_rows() -> list:
    path = os.path.join(HIST, "governor_results.csv")
    if not os.path.exists(path):
        return []
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        y, s = int(r["year"]), r["state_po"].strip().upper()
        d, rp = _f(r["dem_pct"]), _f(r["rep_pct"])
        w = r["winner_party"].strip().upper()[:1]
        contested = int(d is not None and rp is not None and d > 0 and rp > 0 and w in ("D", "R"))
        rows.append({"year": y, "office": "governor", "state_po": s, "district": "statewide", "seat": f"{s}-gov", "special": 0,
                     "contested": contested, "dem_pct": d if d is not None else "", "rep_pct": rp if rp is not None else "",
                     "margin": round(d - rp, 2) if contested else "", "winner": w, "winner_name": "", "dem_name": r.get("dem_candidate", ""),
                     "rep_name": r.get("rep_candidate", ""), "stage": "gen", "inc_party": r["incumbent_party"].strip().upper()[:1],
                     "inc_running": int(_f(r["incumbent_running"]) or 0), "open_seat": int(_f(r["open_seat"]) or 0)})
    return rows


def house_rows() -> list:
    path = os.path.join(HIST, "house_results.csv")
    if not os.path.exists(path):
        return []
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        y, s = int(r["year"]), r["state_po"].strip().upper()
        try:
            dist = int(r["district"])
        except ValueError:
            continue
        d, rp = _f(r["dem_pct"]), _f(r["rep_pct"])
        w = r["winner_party"].strip().upper()[:1]
        note = (r.get("notes") or "").lower()
        contested = int(d is not None and rp is not None and d > 0 and rp > 0 and w in ("D", "R") and "same-party" not in note)
        rows.append({"year": y, "office": "house", "state_po": s, "district": dist, "seat": f"{s}-{dist:02d}", "special": 0,
                     "contested": contested, "dem_pct": d if d is not None else "", "rep_pct": rp if rp is not None else "",
                     "margin": round(d - rp, 2) if contested else "", "winner": w, "winner_name": "", "dem_name": "", "rep_name": "",
                     "stage": "gen", "inc_party": r["incumbent_party"].strip().upper()[:1],
                     "inc_running": int(_f(r["incumbent_running"]) or 0), "open_seat": int(_f(r["open_seat"]) or 0)})
    return rows


def race_polls() -> dict:
    """(cycle, office, state_po, district) -> (final-21-day average D-R margin, n polls); ambiguous same-state double races by name."""
    path = os.path.join(HERE, "data", "fair", "raw_polls_538.csv")
    if not os.path.exists(path):
        return {}
    acc = defaultdict(list)
    for r in csv.DictReader(open(path, encoding="utf-8")):
        t = r["type_simple"]
        if t not in ("Sen-G", "Gov-G", "House-G"):
            continue
        try:
            tte = int(r["time_to_election"])
        except ValueError:
            continue
        if tte > 21 or tte < 0:
            continue
        p1, p2 = r["cand1_party"], r["cand2_party"]
        m = _f(r["margin_poll"])
        if m is None:
            continue
        if p1 == "DEM" and p2 == "REP":
            dm = m
        elif p1 == "REP" and p2 == "DEM":
            dm = -m
        else:
            continue
        office = {"Sen-G": "senate", "Gov-G": "governor", "House-G": "house"}[t]
        loc = r["location"]
        dist = "statewide"
        if office == "house":
            st, _, dd = loc.partition("-")
            loc, dist = st, int(dd) if dd.isdigit() else 1
        acc[(int(r["cycle"]), office, loc, dist, r["race_id"])].append((dm, r["cand1_name"], r["cand2_name"]))
    out = {}
    for (cy, off, loc, dist, rid), v in acc.items():
        out.setdefault((cy, off, loc, dist), []).append((rid, sum(x[0] for x in v) / len(v), len(v), {_key(x[1]) for x in v} | {_key(x[2]) for x in v}))
    return out


def build() -> list:
    lean = state_lean()
    polls = race_polls()
    rows = senate_rows() + governor_rows() + house_rows()
    for r in rows:
        y, s = r["year"], r["state_po"]
        r["pvi"] = round(lean.get((s, y), lean.get((s, y - 1), 0.0)), 2) if (s, y) in lean or (s, y - 1) in lean else ""
        r["region"] = STATE_REGION.get(s, "")
        r["division"] = STATE_DIV.get(s, "")
        nat = NATIONAL.get(y) or NATIONAL_ODD.get(y) or (None, None, "", 0)
        r["gb_poll"] = "" if nat[0] is None else nat[0]
        r["natl_pv"] = "" if nat[1] is None else nat[1]
        r["pres_party"] = nat[2]
        r["pres_year"] = nat[3]
        dist = r["district"] if r["office"] == "house" else "statewide"
        cands = polls.get((y, r["office"], s, dist), [])
        r["poll_margin"], r["n_polls"] = "", 0
        if cands:
            pick = cands[0]
            if len(cands) > 1:
                keys = {_key(n) for n in (r["dem_name"], r["rep_name"]) if n}
                scored = sorted(cands, key=lambda c: -len(c[3] & keys))
                pick = scored[0] if len(scored[0][3] & keys) > 0 else (None if r["special"] else cands[0])
            if pick:
                r["poll_margin"], r["n_polls"] = round(pick[1], 2), pick[2]
    cols = ["year", "office", "state_po", "district", "seat", "pres_year", "special", "contested", "dem_pct", "rep_pct", "margin", "winner",
            "inc_party", "inc_running", "open_seat", "pvi", "region", "division", "gb_poll", "natl_pv", "pres_party", "poll_margin", "n_polls",
            "winner_name", "dem_name", "rep_name", "stage"]
    rows.sort(key=lambda r: (r["year"], r["office"], r["state_po"], str(r["district"])))
    with open(os.path.join(HIST, "races.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(HIST, "national_environment.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["year", "gb_poll_final", "house_pv_margin", "pres_party", "pres_year", "gb_error"])
        for y, (gb, pv, pp, py) in sorted(NATIONAL.items()):
            w.writerow([y, gb, "" if pv is None else pv, pp, py, "" if (pv is None or gb is None) else round(pv - gb, 2)])
    with open(os.path.join(HIST, "state_lean.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["state_po", "election_year", "lean_2pres"])
        for (s, y), v in sorted(lean.items()):
            if y >= 1998:
                w.writerow([s, y, round(v, 2)])
    return rows


if __name__ == "__main__":
    rows = build()
    from collections import Counter
    c = Counter((r["year"], r["office"]) for r in rows if r["year"] >= 2006)
    for k in sorted(c):
        n_con = sum(1 for r in rows if (r["year"], r["office"]) == k and r["contested"])
        n_pol = sum(1 for r in rows if (r["year"], r["office"]) == k and r["poll_margin"] != "")
        n_inc = sum(1 for r in rows if (r["year"], r["office"]) == k and r["inc_party"] != "")
        print(f"{k[0]} {k[1]:9s} races {c[k]:4d} contested {n_con:4d} polled {n_pol:4d} inc_known {n_inc:4d}")
    print("wrote", os.path.join(HIST, "races.csv"), len(rows), "rows")
