"""Kalshi 2024 US general-election legs for the favourite-drift study.

Stdlib only. Rebuilds data/polymarket_history/kalshi_legs.csv from the raw cache
(data/polymarket_history/raw/kalshi/). With --fetch it first downloads what is missing
(polite: at most ~3 requests per second; after any 429 it paces at one request per 5 s).
Every response is cached as one JSON file and never re-fetched.

Run:
    python3 -I research_notes/favourite_drift_kalshi.py --list-only   (discovery and party diagnostics)
    python3 -I research_notes/favourite_drift_kalshi.py --fetch       (discovery + candlesticks, then rebuild)
    python3 -I research_notes/favourite_drift_kalshi.py               (rebuild the CSV from the cache only)

Endpoints (base https://api.elections.kalshi.com/trade-api/v2, no key needed):
  GET /series?category=Elections   and   GET /series?category=Politics
        series catalogue; race series are picked by ticker prefix (see office_of_series)
  GET /historical/markets?series_ticker=<S>&limit=1000[&cursor=...]
        settled (finalized) markets of one series; 2024 markets are those whose event_ticker ends in -24
  GET /historical/markets/<TICKER>/candlesticks?start_ts&end_ts&period_interval=1440
        daily candles for one archived market, window 2024-10-01 to 2024-11-07 UTC
Tried, not used: GET /events?series_ticker=PRES&status=settled (only PRES-2024 comes back, as a
national event); GET /markets?event_ticker=PRESPARTYAR-24 (empty: 2024 markets are archived);
GET /series/<S>/markets/<T>/candlesticks (404 for archived markets).

Leg rule: one row per market (one party or candidate yes/no), settled with result yes/no, volume_fp
(contracts) >= 1000. Daily price = mid of yes_bid.close and yes_ask.close when both are present,
else price.close (last trade). For days_before d, the price is the last daily candle whose
end_period_ts is <= election day 12:00 UTC minus d days. first_obs and last_obs are the UTC dates of
the first and last candle end timestamps in the window. n_obs_30d counts candles ending in
[T-30d, T]. Output columns match data/polymarket_history/legs.csv.
"""
from __future__ import annotations

import argparse
import bisect
import calendar
import csv
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "polymarket_history")
RAW = os.path.join(DATA, "raw", "kalshi")
OUT_CSV = os.path.join(DATA, "kalshi_legs.csv")
BASE = "https://api.elections.kalshi.com/trade-api/v2"
USER_AGENT = "sig-favourite-drift-study/0.1"
GAP = 0.36       # seconds between requests (under 3 per second)
SLOW_GAP = 5.0   # after any 429, pace at about one request per 5 s for the rest of the run
DAY = 86400
MIN_VOLUME = 1000.0
CYCLE = "2024"
ELECTION_TS = calendar.timegm((2024, 11, 5, 12, 0, 0))
WINDOW_START = calendar.timegm((2024, 10, 1, 0, 0, 0))
WINDOW_END = calendar.timegm((2024, 11, 8, 0, 0, 0)) - 1
HORIZONS = [30, 25, 20, 13, 7, 3, 1, 0]
LEG_FIELDS = (["event_id", "slug", "cycle", "office", "party", "market_id", "token", "volume_usd", "outcome"]
              + [f"p{h}" for h in HORIZONS] + ["n_obs_30d", "first_obs", "last_obs"])
OFFICE_ORDER = {"president": 0, "senate": 1, "governor": 2, "house": 3}

_state = {"last": 0.0, "gap": GAP, "requests": 0, "fetched": 0, "rate_limited": 0, "errors": []}
FETCH = False
_CTX = None


def _tls_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        for p in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(p):
                return ssl.create_default_context(cafile=p)
        return ssl.create_default_context()


def _cache_file(path: str, params: dict | None) -> str:
    key = path + "?" + urllib.parse.urlencode(sorted((params or {}).items()))
    slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[:90]
    return os.path.join(RAW, f"{slug}__{hashlib.sha1(key.encode()).hexdigest()[:10]}.json")


def _save(fp: str, url: str, status: int, data) -> None:
    os.makedirs(RAW, exist_ok=True)
    with open(fp, "w") as f:
        json.dump({"request": {"url": url, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                   "status": status, "response": data}, f)


def get(path: str, params: dict | None = None):
    """Cached GET. Returns the parsed body, or None for a 404 or a failed request.

    With FETCH off, a file that is not cached returns None and is counted as missing by the caller.
    """
    global _CTX
    fp = _cache_file(path, params)
    if os.path.exists(fp):
        with open(fp) as f:
            return json.load(f)["response"]
    if not FETCH:
        return None
    if _CTX is None:
        _CTX = _tls_context()
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(6):
        wait = _state["gap"] - (time.time() - _state["last"])
        if wait > 0:
            time.sleep(wait)
        _state["last"] = time.time()
        _state["requests"] += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _state["rate_limited"] += 1
                _state["gap"] = SLOW_GAP
                time.sleep(15 * (attempt + 1))
                continue
            if e.code == 404:
                _save(fp, url, 404, None)
                return None
            if e.code >= 500 and attempt < 5:
                time.sleep(3 * (attempt + 1))
                continue
            _state["errors"].append((e.code, url))
            return None
        except Exception as e:  # noqa: BLE001
            if attempt < 5:
                time.sleep(3 * (attempt + 1))
                continue
            _state["errors"].append((str(e), url))
            return None
        _save(fp, url, 200, data)
        _state["fetched"] += 1
        return data
    _state["errors"].append(("429 retries exhausted", url))
    return None


# ---------------------------------------------------------------- discovery

def office_of_series(s: str) -> str | None:
    """Race series by ticker prefix: PRESPARTY<ST> state presidential, SENATE<ST>/SENATEPARTY<ST>,
    GOVPARTY<ST>, HOUSE<ST><n>. Excludes national control series, primaries and margin series."""
    if s.startswith("PRESPARTY") and s != "PRESPARTYFULL":
        return "president"
    if s.startswith("GOVPARTY"):
        return "governor"
    if s.startswith("SENATE") and s != "SENATE" and "MOV" not in s:
        return "senate"
    if s.startswith("HOUSE") and s != "HOUSE" and "MOV" not in s:
        return "house"
    return None


def discover_series() -> list[str]:
    seen: dict[str, dict] = {}
    for cat in ("Elections", "Politics"):
        r = get("/series", {"category": cat})
        for s in (r or {}).get("series") or []:
            seen[s["ticker"]] = s
    return sorted(t for t in seen if office_of_series(t))


def list_historical(series: str) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    while True:
        params = {"series_ticker": series, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = get("/historical/markets", params)
        if r is None:
            return rows
        rows.extend(r.get("markets") or [])
        cursor = r.get("cursor") or None
        if not cursor:
            return rows


def party_of(m: dict) -> str | None:
    """'D' or 'R' from the ticker suffix (-D / -R, which agrees with custom_strike.Party in every
    market here); otherwise from custom_strike.Party; None for Libertarian, Independent and other legs."""
    suf = (m.get("ticker") or "").rsplit("-", 1)[-1]
    if suf in ("D", "R"):
        return suf
    cs = m.get("custom_strike") or {}
    raw = (cs.get("Party") or "").strip().lower() if isinstance(cs, dict) else ""
    if raw.startswith("democrat"):
        return "D"
    if raw.startswith("republican") or raw.startswith("republicn"):
        return "R"
    return None


def collect_markets(series_list: list[str]) -> dict[str, dict]:
    """Settled 2024 markets from every race series, keyed by market ticker."""
    found: dict[str, dict] = {}
    for i, s in enumerate(series_list, 1):
        for m in list_historical(s):
            if not str(m.get("event_ticker", "")).endswith("-24"):
                continue
            found[m["ticker"]] = {**m, "_series": s, "_office": office_of_series(s)}
        if i % 50 == 0:
            print(f"  {i}/{len(series_list)} series listed, {len(found)} 2024 markets so far", file=sys.stderr)
    return found


# ---------------------------------------------------------------- prices

def daily_points(resp) -> list[tuple[int, float]]:
    pts: dict[int, float] = {}
    for c in (resp or {}).get("candlesticks") or []:
        ts = int(c["end_period_ts"])
        if ts > WINDOW_END:
            continue
        bid = (c.get("yes_bid") or {}).get("close")
        ask = (c.get("yes_ask") or {}).get("close")
        if bid is not None and ask is not None:
            px = (float(bid) + float(ask)) / 2
        else:
            last = (c.get("price") or {}).get("close")
            if last is None:
                continue
            px = float(last)
        pts[ts] = px
    return sorted(pts.items())


def _value_at(times: list[int], vals: list[float], cutoff: int):
    i = bisect.bisect_right(times, cutoff) - 1
    return vals[i] if i >= 0 else None


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def build_leg(m: dict, party: str, rep: dict) -> dict | None:
    ticker = m["ticker"]
    resp = get(f"/historical/markets/{ticker}/candlesticks",
               {"start_ts": WINDOW_START, "end_ts": WINDOW_END, "period_interval": 1440})
    if resp is None:
        rep["missing_candles"].append(ticker)
        return None
    series = daily_points(resp)
    times = [t for t, _ in series]
    vals = [p for _, p in series]
    if not series:
        rep["no_prices"].append(ticker)
    prices = {}
    for h in HORIZONS:
        v = _value_at(times, vals, ELECTION_TS - h * DAY)
        prices[f"p{h}"] = "" if v is None else round(v, 6)
    n30 = bisect.bisect_right(times, ELECTION_TS) - bisect.bisect_left(times, ELECTION_TS - 30 * DAY)
    return {
        "event_id": m["event_ticker"], "slug": ticker, "cycle": CYCLE, "office": m["_office"],
        "party": party, "market_id": ticker, "token": "", "volume_usd": round(float(m["volume_fp"]), 2),
        "outcome": 1 if m.get("result") == "yes" else 0, **prices, "n_obs_30d": n30,
        "first_obs": _date(times[0]) if times else "", "last_obs": _date(times[-1]) if times else "",
    }


def build_rows(markets: dict[str, dict], rep: dict) -> list[dict]:
    """Two-party races first (both a D and an R market listed; third-party legs do not count),
    then per market: resolved, volume_fp >= 1000, D or R. Candles are fetched only for kept legs."""
    parties: dict[str, set] = defaultdict(set)
    for m in markets.values():
        p = party_of(m)
        if p:
            parties[m["event_ticker"]].add(p)
    keep: dict[tuple[str, str], dict] = {}
    for t, m in sorted(markets.items()):
        rep["seen"] += 1
        ev = m["event_ticker"]
        if parties[ev] != {"D", "R"}:
            rep["dropped"]["market in a race without both a D and an R market"] += 1
            rep["not_two_party"].add(ev)
            continue
        party = party_of(m)
        if party is None:
            rep["dropped"]["third-party leg (Libertarian, Independent)"] += 1
            continue
        if m.get("result") not in ("yes", "no"):
            rep["dropped"]["unresolved or void"] += 1
            continue
        vol = float(m.get("volume_fp") or 0)
        if vol < MIN_VOLUME:
            rep["dropped"]["volume below 1000 contracts"] += 1
            continue
        key = (ev, party)
        if key in keep:
            rep["dropped"]["duplicate leg in event"] += 1
            if float(keep[key]["volume_fp"]) >= vol:
                continue
        keep[key] = m
    rep["races_kept"] = len({ev for ev, _ in keep})
    rows = []
    for (ev, party), m in keep.items():
        leg = build_leg(m, party, rep)
        if leg is not None:
            rows.append(leg)
    rows.sort(key=lambda r: (OFFICE_ORDER[r["office"]], r["event_id"], r["party"]))
    return rows


def _fmt(v):
    return "" if v is None else v


def write_csv(rows: list[dict]) -> None:
    os.makedirs(DATA, exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(LEG_FIELDS)
        for r in rows:
            w.writerow([_fmt(r[k]) for k in LEG_FIELDS])


def report(rows: list[dict], rep: dict) -> None:
    print("requests sent:", _state["requests"], "fetched new:", _state["fetched"],
          "rate limited (429):", _state["rate_limited"], "errors:", len(_state["errors"]))
    for e in _state["errors"][:10]:
        print("  error", e)
    print(f"2024 markets seen {rep['seen']}; legs written {len(rows)}")
    print("dropped:", dict(rep["dropped"]))
    print("legs by office:", dict(Counter(r["office"] for r in rows)))
    print("legs by party:", dict(Counter(r["party"] for r in rows)))
    by_event = defaultdict(set)
    for r in rows:
        by_event[r["event_id"]].add(r["party"])
    print("races with both D and R legs:", sum(1 for v in by_event.values() if v == {"D", "R"}),
          "| races with one leg:", sum(1 for v in by_event.values() if len(v) == 1))
    in20 = sum(1 for r in rows if r["p20"] != "" and 0.75 <= r["p20"] <= 0.95)
    print(f"p20 in [0.75, 0.95]: {in20} of {sum(1 for r in rows if r['p20'] != '')} legs with a p20")
    print("no candles returned:", len(rep["missing_candles"]), "| candles with no price:", len(rep["no_prices"]))
    print("races kept (two-party, at least one leg):", rep["races_kept"])
    print("races excluded as not two-party:", sorted(rep["not_two_party"]))
    print("wrote", OUT_CSV)


def main(argv=None) -> None:
    global FETCH
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="download missing responses (otherwise cache only)")
    ap.add_argument("--list-only", action="store_true", help="discovery and diagnostics only, no candles")
    args = ap.parse_args(argv)
    FETCH = args.fetch or args.list_only
    series = discover_series()
    print(f"race series: {len(series)}")
    markets = collect_markets(series)
    print(f"2024 markets across race series: {len(markets)}")
    by_series = Counter(m["_series"] for m in markets.values())
    print("series with 2024 markets:", len(by_series))
    if args.list_only:
        print("sample rows (first per office):")
        seen_off = set()
        for t, m in sorted(markets.items()):
            if m["_office"] in seen_off:
                continue
            seen_off.add(m["_office"])
            print(json.dumps({k: m.get(k) for k in ("ticker", "event_ticker", "title", "subtitle",
                                                       "yes_sub_title", "custom_strike", "result",
                                                       "volume_fp", "status")}))
        return
    rep = _new_rep()
    rows = build_rows(markets, rep)
    write_csv(rows)
    report(rows, rep)


def _new_rep() -> dict:
    return {"seen": 0, "dropped": Counter(), "not_two_party": set(), "unclassified": [],
            "missing_candles": [], "no_prices": [], "races_kept": 0}


if __name__ == "__main__":
    main()
