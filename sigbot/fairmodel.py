"""Our own fair value per SIG race from public data, independent of any prediction market.

    python3 -m sigbot.fairmodel                 # scrape (cached 6 h), fit, print the table, write params/fair_model.json
    python3 -m sigbot.fairmodel --refresh       # ignore caches

Inputs (all public, no keys; cached under data/fair/):
  * Race polls: VoteHub API (api.votehub.com/polls?poll_type=us-senator|governor|us-representative): every published
    poll with candidate names and percentages, dates, sample, population, internal/partisan flags.
  * Ratings: the prediction tables on Wikipedia's "2026 United States Senate elections", "... gubernatorial elections"
    and "... House of Representatives election ratings" pages (Cook, Inside Elections, Sabato, DDHQ, Economist, Fox,
    NBC, RCP, Silver Bulletin, Split Ticket, FiftyPlusOne), with the 2026 Cook PVI, incumbent, party and open-seat
    status of every race.
  * Fundamentals: Cook PVI (partisanship), the generic-ballot aggregate (Wikipedia GenericBallotAgg section, else
    VoteHub generic-ballot polls), incumbency, the 2024 presidential margin by state (Wikipedia results table, used
    for explanations), days to the election.
  * Calibration: FiveThirtyEight's raw_polls.csv (2016-2022 Senate, governor and House general-election polls with
    actual margins): the error of a final-three-week polling average has standard deviation 5.9 (Senate), 6.1 (governor)
    and 7.0 (House) points within a cycle, and the cycle-wide bias moved between -0.4 and -6.8 points (2018, 2022 vs
    2020), so a 3-point bias term is added in quadrature. A logistic/probit sigma is also fitted by maximum likelihood
    on the win/loss outcomes and reported next to the error SD (the two agree within about half a point).

Model (documented, deliberately simple)
  margin_hat (D minus R, points) = w_p * poll_margin + (1 - w_p) * prior,  prior = rating_margin + clamp(0.3 * (fund_margin - rating_margin), -4, 4)
    (ratings already fold in fundamentals and candidate quality, so fundamentals only nudge them; fund_margin alone when unrated)
    poll_margin   recency-weighted (half-life 14 d) average of non-internal polls in the last 45 days, partisan polls at
                  half weight, sample weight sqrt(n/600) capped at 1.5; margin = D top candidate - R top candidate
    w_p           n_eff / (n_eff + 1.5): three fresh independent polls -> 0.67, none -> 0 (prior only)
    rating_margin mean over raters of {Safe/Solid 14, Likely 8, Lean 4, Tilt 2, Tossup 0} signed toward the favoured party
    fund_margin   PVI (D positive) + 0.5 * generic ballot (D - R) + 3 * incumbency (incumbent party running, 0 if open)
  sigma = sqrt(sd_office^2 + 3^2) for polled races (>= 2 polls, n_eff >= 1), 8.0 when ratings carry the race (documented prior:
          Lean -> ~70%, Likely -> ~85%, Solid -> ~96% are the historical hit rates of the raters)
  P(D wins) = Phi(margin_hat / sigma) capped to [0.5%, 99.5%]; P(R) = 1 - P(D) unless a third candidate polls >= 10%, in which case candidates
  are treated pairwise against the strongest other candidate and the probabilities are normalised.

Output params/fair_model.json: per race -> per party {fair, lo, hi (margin +/- 1 sigma), inputs}, plus the calibration
table, the generic ballot used and the parse status of every source.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import time
from datetime import date
from typing import Optional

from .venues.http import Http
from .venues.states import parse_race

log = logging.getLogger("sigbot.fairmodel")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(HERE, "data", "fair")
OUT = os.path.join(HERE, "params", "fair_model.json")
ELECTION = date(2026, 11, 3)
WIKI_API = "https://en.wikipedia.org/w/api.php"
VOTEHUB = "https://api.votehub.com/polls"
RAW538 = "https://raw.githubusercontent.com/fivethirtyeight/data/master/pollster-ratings/raw_polls.csv"
RATING_PTS = {"Safe": 14.0, "Solid": 14.0, "Likely": 8.0, "Lean": 4.0, "Tilt": 2.0, "Tossup": 0.0, "Toss-up": 0.0}
PAGES = {
    "senate": "2026_United_States_Senate_elections",
    "governor": "2026_United_States_gubernatorial_elections",
    "house": "2026_United_States_House_of_Representatives_elections",
    "house_ratings": "2026_United_States_House_of_Representatives_election_ratings",
    "pres2024": "2024_United_States_presidential_election",
}


# ------------------------------------------------------------------ fetch / cache
def _cached(name: str, max_age: float, fetch, refresh: bool = False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if not refresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age:
        return json.load(open(path))
    data = fetch()
    json.dump(data, open(path, "w"))
    return data


def wikitext(http: Http, page: str, refresh: bool = False) -> str:
    def f():
        res, _ = http.get(WIKI_API, {"action": "parse", "page": page, "prop": "wikitext", "format": "json", "formatversion": 2}, retries=2, timeout=30)
        return res["parse"]["wikitext"]
    return _cached(f"wiki_{page}.json", 6 * 3600, f, refresh)


def votehub(http: Http, poll_type: str, refresh: bool = False) -> list:
    def f():
        res, _ = http.get(VOTEHUB, {"poll_type": poll_type}, retries=2, timeout=40)
        return res or []
    return _cached(f"votehub_{poll_type}.json", 3600, f, refresh)


def raw538(http: Http) -> list[dict]:
    path = os.path.join(CACHE, "raw_polls_538.csv")
    if not os.path.exists(path):
        import urllib.request
        os.makedirs(CACHE, exist_ok=True)
        urllib.request.urlretrieve(RAW538, path)
    return list(csv.DictReader(open(path, encoding="utf-8", errors="replace")))


# ------------------------------------------------------------------ wiki parsing
def _clean(s: str) -> str:
    s = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", s)
    return re.sub(r"<[^>]+>|'''", "", s).strip()


def parse_pred_table(w: str, key_re: str) -> dict:
    """Prediction table rows -> {key: {pvi, inc, inc_party, open, last, ratings{rater: (cat, party)}}}."""
    i = w.find("== Predictions")
    if i < 0:
        i = w.find("==Predictions")
    if i < 0:
        i = w.find("Latest published ratings")
    t = w.find("{|", i)
    body = w[t: w.find("\n|}", t)]
    out = {}
    chunks = body.split("\n|-")
    hdr = "\n".join(chunks[:3])
    hdr_names = [n.strip() for n in re.findall(r"!\s*(?:'')?\[\[[^|\]]*\|([^\]]+)\]\](?:'')?<br", hdr)]
    skip = {"PVI", "CPVI", "District", "State"}
    hdr_names = [n for n in hdr_names if n not in skip and "Partisan" not in n]
    for r in chunks[1:]:
        m = re.search(key_re, r)
        if not m:
            continue
        key = m.group(1).strip()
        pvi = re.search(r"\{\{[Ss]hading PVI\|(D|R|EVEN)\|?([\d.]+)?", r)
        inc = re.search(r"\{\{Party shading/(Democratic|Republican|Independent|DFL)\}\}\s*\|\s*\{\{sortname\|([^|}]+)\|([^|}]+)", r)
        openseat = bool(re.search(r"retiring|term-limited|resign|appointed|vacant|died|new seat|running for", r, re.I))
        last = re.search(r'data-sort-value="(-?[\d.]+)"\s*\|\s*([\d.]+)% (D|R|I)', r)
        rats = re.findall(r"\{\{USRaceRating\|([A-Za-z\-]+)(?:\|([DRI]))?(?:\|[Ff]lip)?\}\}", r)
        names = re.findall(r"<!--\s*([A-Za-z]+)\s*-->", r)
        ratings = {}
        for k, (cat, party) in enumerate(rats):
            name = names[k] if k < len(names) else (hdr_names[k] if k < len(hdr_names) else f"rater{k + 1}")
            ratings[name] = (cat, party or None)
        pv = None
        if pvi:
            pv = 0.0 if pvi.group(1) == "EVEN" else (1 if pvi.group(1) == "D" else -1) * float(pvi.group(2) or 0)
        out[key] = {"pvi": pv, "inc": (inc.group(2) + " " + inc.group(3)).strip() if inc else None,
                    "inc_party": {"Democratic": "D", "DFL": "D", "Republican": "R", "Independent": "I"}.get(inc.group(1)) if inc else None,
                    "open": openseat, "last": (float(last.group(2)), last.group(3)) if last else None, "ratings": ratings}
    return out


def parse_house_candidates(w: str) -> dict:
    """Per-district rows of the House page -> {"AZ-01": {pvi, inc, inc_party, open, candidates: [(name, party)]}}."""
    out = {}
    for m in re.finditer(r"!\{\{ushr\|([A-Z]{2})\|(\d+|AL)\|X\}\}(.*?)(?=\n\|-|\n\|\})", w, re.S):
        st, d, body = m.group(1), m.group(2), m.group(3)
        code = f"{st}-{int(d):02d}" if d != "AL" else f"{st}-01"
        if "plainlist" not in body:
            continue
        pvi = re.search(r"\{\{[Ss]hading PVI\|(D|R|EVEN)\|?([\d.]+)?", body)
        inc = re.search(r"\{\{sortname\|([^|}]+)\|([^|}]+)", body)
        party = re.search(r"\|\s*(Democratic|Republican|Independent)\s*\n", body)
        cands = re.findall(r"\{\{Party stripe\|([^}]+)\}\}\s*(?:'''|\[\[)?([^\]|'\n(]+)", body)
        cl = []
        for p, n in cands:
            pp = "D" if "Democratic" in p else ("R" if "Republican" in p else "I")
            cl.append((_clean(n).strip(), pp))
        pv = None
        if pvi:
            pv = 0.0 if pvi.group(1) == "EVEN" else (1 if pvi.group(1) == "D" else -1) * float(pvi.group(2) or 0)
        out[code] = {"pvi": pv, "inc": (inc.group(1) + " " + inc.group(2)).strip() if inc else None,
                     "inc_party": party.group(1)[0] if party else None,
                     "open": bool(re.search(r"retiring|new seat|resign|died|running for|lost renomination|term-limited", body, re.I)),
                     "candidates": cl}
    return out


def parse_generic_ballot(w: str) -> Optional[dict]:
    """GenericBallotAgg section: aggregate rows with D%, R% -> mean D - R."""
    i = w.find("GenericBallotAgg")
    if i < 0:
        return None
    t = w.find("{|", i)
    body = w[t: w.find("\n|}", t)]
    vals = []
    for r in body.split("\n|-")[1:]:
        cells = [_clean(c) for c in re.split(r"\n\|", r)[1:]]
        src = cells[0] if cells else ""
        m = re.search(r"(Democrats|Republicans)\s*\+\s*([\d.]+)%", r)
        if m and not src.lower().startswith("average") and "Average" not in src:
            vals.append((src, float(m.group(2)) * (1 if m.group(1) == "Democrats" else -1)))
    if not vals:
        return None
    return {"sources": [v[0] for v in vals], "margins": [v[1] for v in vals], "d_minus_r": sum(v[1] for v in vals) / len(vals)}


def parse_pres2024(w: str) -> dict:
    i = w.find("Results by state")
    t = w.find('{| class="wikitable sortable"', i)
    out = {}
    for r in w[t: w.find("\n|}", t)].split("\n|-"):
        m = re.search(r"\[\[2024 United States presidential election in ([^|\]]+)\|([^\]]+)\]\]", r)
        if not m:
            continue
        pcts = re.findall(r"\|\s*'{0,3}(\d{1,2}\.\d{2})%'{0,3}\s*\n", r)
        if len(pcts) >= 2:
            out[m.group(2).strip()] = round(float(pcts[1]) - float(pcts[0]), 2)     # Harris - Trump
    return out


# ------------------------------------------------------------------ calibration
def calibration(rows: list[dict]) -> dict:
    out = {}
    for off, name in (("Sen-G", "senate"), ("Gov-G", "governor"), ("House-G", "house")):
        by: dict = {}
        actual: dict = {}
        for r in rows:
            if r["type_simple"] != off or int(r["cycle"]) < 2016 or r.get("partisan") not in ("NA", "", None):
                continue
            try:
                d, mp, ma = int(r["time_to_election"]), float(r["margin_poll"]), float(r["margin_actual"])
            except Exception:
                continue
            if 0 <= d <= 21:
                by.setdefault((r["cycle"], r["race"]), []).append(mp)
                actual[(r["cycle"], r["race"])] = ma
        errs, cyc, pairs = [], {}, []
        for k, ms in by.items():
            avg = sum(ms) / len(ms)
            errs.append(actual[k] - avg)
            cyc.setdefault(k[0], []).append(actual[k] - avg)
            pairs.append((avg, actual[k] > 0))
        n = len(errs)
        mean = sum(errs) / n
        sd = math.sqrt(sum((e - mean) ** 2 for e in errs) / n)
        # within-cycle SD (bias removed per cycle) and the SD of the cycle means
        within = math.sqrt(sum((e - sum(v) / len(v)) ** 2 for v in cyc.values() for e in v) / n)
        cmeans = [sum(v) / len(v) for v in cyc.values()]
        bias_sd = math.sqrt(sum((c - sum(cmeans) / len(cmeans)) ** 2 for c in cmeans) / max(1, len(cmeans) - 1))
        # MLE probit sigma on outcomes
        best = None
        for s10 in range(20, 150):
            s = s10 / 10
            ll = sum(math.log(max(1e-9, _phi(a / s) if w else 1 - _phi(a / s))) for a, w in pairs)
            if best is None or ll > best[1]:
                best = (s, ll)
        leader = sum(1 for a, w in pairs if a != 0 and (a > 0) == w) / max(1, sum(1 for a, _ in pairs if a != 0))
        out[name] = {"races": n, "mean_error": round(mean, 2), "sd_error": round(sd, 2), "sd_within_cycle": round(within, 2),
                     "bias_sd_across_cycles": round(bias_sd, 2), "cycle_means": {c: round(sum(v) / len(v), 2) for c, v in cyc.items()},
                     "mle_sigma": best[0], "leader_won": round(leader, 3)}
    return out


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ------------------------------------------------------------------ polls
def _name_key(n: str) -> str:
    n = re.sub(r"\(.*?\)|Sen\.|Rep\.|Gov\.|Jr\.|Sr\.|III|II", "", n).strip()
    parts = n.split()
    return parts[-1].lower() if parts else n.lower()


def party_map_for(race: str, info: dict, racemap: dict) -> dict:
    """last-name -> party from Kalshi/Polymarket legs, the Wikipedia incumbent and House candidate lists."""
    m: dict = {}
    e = racemap.get("races", {}).get(race, {})
    for p, ls in e.get("kalshi", {}).get("legs", {}).items():
        for l in ls:
            if l.get("candidate") and not re.search(r"party", l["candidate"], re.I):
                m[_name_key(l["candidate"])] = p
    for p, ls in e.get("polymarket", {}).get("legs", {}).items():
        for l in ls:
            t = l.get("title") or ""
            if not re.search(r"^(Democrat|Republican|Independent|Other)", t):
                m.setdefault(_name_key(t), p)
    if info.get("inc") and info.get("inc_party") and not info.get("open"):
        m.setdefault(_name_key(info["inc"]), info["inc_party"])
    for n, p in info.get("candidates", []):
        m.setdefault(_name_key(n), p)
    return m


def _lookup(pmap: dict, nm: str) -> Optional[str]:
    import difflib
    key = _name_key(nm)
    if key in pmap:
        return pmap[key]
    close = difflib.get_close_matches(key, list(pmap), n=1, cutoff=0.78)
    return pmap[close[0]] if close else None


def race_polls(polls: list, subject: str, pmap: dict, today: date, days: int = 45) -> list[dict]:
    out = []
    for p in polls:
        if p.get("subject") != subject:
            continue
        try:
            end = date.fromisoformat(p["end_date"])
        except Exception:
            continue
        if (today - end).days > days:
            continue
        by_party: dict = {}
        unknown = []
        for a in p.get("answers", []):
            nm = a.get("choice") or ""
            party = _lookup(pmap, nm)
            if party is None:
                mm = re.search(r"\((D|R|I)\)", nm)
                party = mm.group(1) if mm else ("D" if re.search(r"democrat", nm, re.I) else ("R" if re.search(r"republican", nm, re.I) else None))
            if party is None:
                unknown.append((nm, float(a.get("pct") or 0)))
                continue
            by_party[party] = max(by_party.get(party, 0.0), float(a.get("pct") or 0))
        # a two-way poll with one unmapped name: the unmapped candidate is the other major party
        if len(unknown) == 1 and len(p.get("answers", [])) == 2 and len(by_party) == 1 and list(by_party)[0] in ("D", "R"):
            other = "D" if list(by_party)[0] == "R" else "R"
            by_party[other] = unknown[0][1]
            unknown = []
        unknown = [u[0] for u in unknown]
        if "D" not in by_party and "R" not in by_party:
            continue
        out.append({"end": end.isoformat(), "pollster": p.get("pollster"), "n": p.get("sample_size") or 500, "pop": p.get("population"),
                    "internal": bool(p.get("internal")), "partisan": p.get("partisan"), "by_party": by_party, "unknown": unknown})
    return out


def poll_average(ps: list[dict], today: date) -> dict:
    wsum = 0.0
    acc: dict = {}
    n_eff = 0.0
    for p in ps:
        if p["internal"]:
            continue
        age = (today - date.fromisoformat(p["end"])).days
        w = 0.5 ** (age / 14.0) * min(1.5, math.sqrt(float(p["n"]) / 600.0)) * (0.5 if p["partisan"] else 1.0)
        for party, pct in p["by_party"].items():
            acc[party] = acc.get(party, 0.0) + w * pct
        wsum += w
        n_eff += w
    if wsum == 0:
        return {"n": 0, "n_eff": 0.0}
    shares = {k: v / wsum for k, v in acc.items()}
    return {"n": sum(1 for p in ps if not p["internal"]), "n_eff": round(n_eff, 2), "shares": {k: round(v, 1) for k, v in shares.items()},
            "margin": round(shares.get("D", 0.0) - shares.get("R", 0.0), 1), "latest": max(p["end"] for p in ps)}


# ------------------------------------------------------------------ model
def rating_margin(ratings: dict) -> Optional[float]:
    vals = []
    for cat, party in ratings.values():
        pts = RATING_PTS.get(cat)
        if pts is None:
            continue
        vals.append(pts * (1 if party == "D" else -1 if party == "R" else 0))
    return round(sum(vals) / len(vals), 2) if vals else None


def race_fair(office: str, info: dict, polls: dict, generic: float, calib: dict, today: date, legs: list[str]) -> dict:
    sd_off = calib.get(office, {}).get("sd_within_cycle", 6.0)
    sigma_polled = math.sqrt(sd_off ** 2 + 3.0 ** 2)
    rm = rating_margin(info.get("ratings", {}))
    pvi = info.get("pvi")
    inc_bonus = 0.0
    if info.get("inc_party") in ("D", "R") and not info.get("open"):
        inc_bonus = 3.0 * (1 if info["inc_party"] == "D" else -1)
    fund = (pvi if pvi is not None else 0.0) + 0.5 * generic + inc_bonus
    # ratings already fold in fundamentals and candidate quality; fundamentals only nudge them (at most 4 points),
    # so a popular cross-partisan incumbent (Vermont's Phil Scott) is not dragged toward the state's PVI
    prior = (rm + max(-4.0, min(4.0, 0.3 * (fund - rm)))) if rm is not None else fund
    n_eff = polls.get("n_eff", 0.0)
    n_polls = polls.get("n", 0)
    w_p = n_eff / (n_eff + 1.5)
    pm = polls.get("margin")
    margin = w_p * pm + (1 - w_p) * prior if pm is not None else prior
    sigma = sigma_polled if (n_polls >= 2 and n_eff >= 1.0) else 8.0
    out = {"margin_hat": round(margin, 2), "sigma": round(sigma, 2), "w_poll": round(w_p, 2),
           "inputs": {"poll_margin": pm, "n_polls": polls.get("n", 0), "n_eff": n_eff, "poll_shares": polls.get("shares"),
                      "latest_poll": polls.get("latest"), "rating_margin": rm, "ratings": {k: f"{c} {p or ''}".strip() for k, (c, p) in info.get("ratings", {}).items()},
                      "pvi": pvi, "generic_ballot": generic, "incumbency": inc_bonus, "fund_margin": round(fund, 2), "prior_margin": round(prior, 2),
                      "incumbent": info.get("inc"), "open_seat": info.get("open"), "days_to_election": (ELECTION - today).days}}
    shares = polls.get("shares") or {}
    third = {p: s for p, s in shares.items() if p not in ("D", "R") and s >= 10.0}
    probs: dict = {}
    if third and n_polls >= 1:
        # pairwise against the strongest other candidate; the rating prior says how far the favoured party beats the field
        cands = {p: shares.get(p, 0.0) for p in set(list(shares) + ["D", "R"])}
        fav = "D" if (rm or 0) > 0 else "R"
        raw = {}
        for p, s in cands.items():
            best_other = max(v for q, v in cands.items() if q != p) if len(cands) > 1 else 0.0
            m_poll = s - best_other
            m_prior = abs(rm or 0.0) if p == fav else -abs(rm or 0.0)
            raw[p] = _phi((w_p * m_poll + (1 - w_p) * m_prior) / sigma)
        z = sum(raw.values()) or 1.0
        probs = {p: v / z for p, v in raw.items()}
        out["mode"] = "multi-candidate"
    else:
        pd = min(0.995, max(0.005, _phi(margin / sigma)))      # fat tails: nothing is more certain than 99.5%
        probs = {"D": pd, "R": 1 - pd}
        out["mode"] = "two-party"
    lo, hi = _phi((margin - sigma) / sigma), _phi((margin + sigma) / sigma)
    out["legs"] = {}
    for p in legs:
        f = probs.get(p, 0.0)
        out["legs"][p] = {"fair": round(f, 4), "lo": round(lo if p == "D" else (1 - hi if p == "R" else 0.0), 4),
                          "hi": round(hi if p == "D" else (1 - lo if p == "R" else min(0.3, f * 3)), 4)}
    return out


def build(refresh: bool = False) -> dict:
    http = Http(timeout=30.0)
    today = date.today()
    from .venues.mapping import load as load_map
    racemap = load_map()
    races = racemap["races"]
    status = {}
    W = {}
    for k, pg in PAGES.items():
        try:
            W[k] = wikitext(http, pg, refresh)
            status[k] = "ok"
        except Exception as e:
            W[k] = ""
            status[k] = f"error: {e}"
    sen = parse_pred_table(W["senate"], r"!\s*\[\[2026 United States Senate (?:special )?election in ([^|\]]+)\|")
    gov = parse_pred_table(W["governor"], r"!\s*\[\[2026 ([A-Za-z ]+?) gubernatorial election\|")
    hr = parse_pred_table(W["house_ratings"], r"!\{\{ushr\|([A-Z]{2}\|\d+)\|X\}\}")
    hr = {f"{k.split('|')[0]}-{int(k.split('|')[1]):02d}": v for k, v in hr.items()}
    hc = parse_house_candidates(W["house"])
    gb = parse_generic_ballot(W["house"])
    P = {}
    for t in ("us-senator", "governor", "us-representative", "generic-ballot"):
        try:
            P[t] = votehub(http, t, refresh)
            status[f"votehub_{t}"] = f"ok ({len(P[t])} polls)"
        except Exception as e:
            P[t] = []
            status[f"votehub_{t}"] = f"error: {e}"
    if gb is None:
        ms = []
        for p in P["generic-ballot"]:
            if p.get("subject") == "2026" and (today - date.fromisoformat(p["end_date"])).days <= 45:
                a = {x["choice"]: x["pct"] for x in p["answers"]}
                if "Dem" in a and "Rep" in a:
                    ms.append(a["Dem"] - a["Rep"])
        gb = {"sources": ["votehub polls"], "margins": ms, "d_minus_r": sum(ms) / len(ms) if ms else 3.0}
    generic = round(gb["d_minus_r"], 2)
    pres = parse_pres2024(W["pres2024"])
    calib = calibration(raw538(http))
    status["senate_rows"], status["governor_rows"], status["house_rating_rows"], status["house_candidate_rows"] = len(sen), len(gov), len(hr), len(hc)

    out = {"built_at": time.time(), "date": today.isoformat(), "generic_ballot": gb, "calibration": calib, "sources": status, "races": {}}
    for race, e in races.items():
        info_r = parse_race(race)
        office = info_r["office"]
        legs = sorted(set(p for v in ("polymarket", "kalshi") for p in e.get(v, {}).get("legs", {})) | {"D", "R"})
        if office == "senate":
            key = info_r["state"] if info_r["state"] != "Florida" else "Florida"
            info = sen.get(key) or next((v for k, v in sen.items() if k.startswith(info_r["state"])), {})
            subj = f"2026 {info_r['state']}"
            polls = race_polls(P["us-senator"], subj, party_map_for(race, info, racemap), today)
        elif office == "governor":
            info = gov.get(info_r["state"], {})
            subj = f"2026 {info_r['state']}"
            polls = race_polls(P["governor"], subj, party_map_for(race, info, racemap), today)
        elif office == "house":
            code = f"{info_r['abbr']}-{info_r['district']:02d}"
            info = dict(hc.get(code, {}))
            info.update({k: v for k, v in hr.get(code, {}).items() if v not in (None, {}, [])} if code in hr else {})
            if "ratings" not in info:
                info["ratings"] = {}
            subj = f"2026 {code}"
            polls = race_polls(P["us-representative"], subj, party_map_for(race, info, racemap), today)
        else:
            continue
        pa = poll_average(polls, today)
        fr = race_fair(office, info, pa, generic, calib, today, legs)
        fr["office"], fr["state"] = office, info_r["state"]
        fr["inputs"]["pres2024_margin"] = pres.get(info_r["state"]) if info_r["state"] else None
        fr["inputs"]["polls_used"] = [{"end": p["end"], "pollster": p["pollster"], "n": p["n"], "by_party": p["by_party"], "partisan": p["partisan"]} for p in polls if not p["internal"]][:12]
        fr["inputs"]["unmapped_poll_names"] = sorted({u for p in polls for u in p["unknown"]})[:6]
        out["races"][race] = fr
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=1)
    return out


def table(out: dict, top: Optional[int] = None) -> str:
    lines = [f"{'race':26s} {'off':8s} {'D fair':>7s} {'R fair':>7s} {'margin':>7s} {'sigma':>5s} {'polls':>5s} {'poll m':>7s} {'rating':>7s} {'fund':>6s}  ratings"]
    rows = sorted(out["races"].items(), key=lambda kv: abs(kv[1]["margin_hat"]))
    for race, r in rows[:top] if top else rows:
        i = r["inputs"]
        rs = ", ".join(f"{k}:{v}" for k, v in list(i["ratings"].items())[:3])
        lines.append(f"{race[:26]:26s} {r['office']:8s} {r['legs'].get('D', {}).get('fair', 0):7.3f} {r['legs'].get('R', {}).get('fair', 0):7.3f} "
                     f"{r['margin_hat']:+7.1f} {r['sigma']:5.1f} {i['n_polls']:5d} {(i['poll_margin'] if i['poll_margin'] is not None else float('nan')):+7.1f} "
                     f"{(i['rating_margin'] if i['rating_margin'] is not None else float('nan')):+7.1f} {i['fund_margin']:+6.1f}  {rs}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--top", type=int, default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    out = build(args.refresh)
    print("sources:", json.dumps(out["sources"]))
    print(f"generic ballot D-R: {out['generic_ballot']['d_minus_r']:+.1f} from {out['generic_ballot']['sources']}")
    print("calibration:", json.dumps(out["calibration"]))
    print(table(out, args.top))
    print(f"\nsaved {OUT} ({len(out['races'])} races)")


if __name__ == "__main__":
    main()
