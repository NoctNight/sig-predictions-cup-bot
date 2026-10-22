"""Kalshi public market data (api.elections.kalshi.com/trade-api/v2, no auth for reads).

Discovery: GET /series?limit=5000 (15k series, 2.8 MB; cached for a day) -> Elections-category series whose title names
a state and Senate/Governor (tickers like SENATEAR, SENATEPARTY-AZ, GOVPARTYMI, KXGOVAK); GET /markets?series_ticker=X&
status=open gives the markets, party legs are the ones titled "Will Democrats/Republicans win ..." in the "-26" event.
House: one series KXHOUSERACE, 707 markets, events KXHOUSERACE-<ST><DD>-26, tickers ...-26-D / -26-R.
Chamber control: no direct market; derived from KXBALANCEPOWERCOMBO-27FEB-{DD,DR,RD,RR} (House letter first):
    P(D Senate) = DD + RD,  P(R Senate) = RR + DR,  P(D House) = DD + DR.
Live: GET /markets?tickers=A,B,... (100 per call, ~0.2 s) -> yes_bid_dollars / yes_ask_dollars and *_size_fp (contracts).
History: GET /series/{series}/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval=1|60|1440 (<= 5000 candles).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

from .http import Http, VenueError, VenueState
from .states import STATES

log = logging.getLogger("sigbot.venues.kalshi")

BASE = "https://api.elections.kalshi.com/trade-api/v2"
COMBO_SERIES = "KXBALANCEPOWERCOMBO"
HOUSE_SERIES = "KXHOUSERACE"
EXCLUDE = re.compile(r"nominee|primary|margin|combo|turnout|state senate|state house|lieutenant|county|within|closest|sworn|"
                     r"outperform|runoff|matchup|run for|announce|resign|retire|special election winner sworn", re.I)


def party_of(m: dict) -> Optional[str]:
    t = (m.get("title") or "") + " " + (m.get("subtitle") or "")
    tk = m.get("ticker", "")
    if tk.endswith("-D") or re.search(r"democrat", t, re.I):
        return "D"
    if tk.endswith("-R") or re.search(r"republican", t, re.I):
        return "R"
    if re.search(r"independent", t, re.I):
        return "I"
    return None


class Kalshi:
    def __init__(self, http: Http, cache_dir: str = "data"):
        self.http = http
        self.state = VenueState("kalshi")
        self.cache_dir = cache_dir

    # ------------------------------------------------------------ discovery
    def series_list(self) -> list[dict]:
        path = os.path.join(self.cache_dir, "kalshi_series.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
            return json.load(open(path))
        res, lat = self.http.get(f"{BASE}/series", {"limit": 5000}, retries=2, timeout=30)
        self.state.ok(lat)
        series = (res or {}).get("series", [])
        os.makedirs(self.cache_dir, exist_ok=True)
        json.dump(series, open(path, "w"))
        return series

    def race_series(self) -> dict:
        """{(state_name, office): [series_ticker, ...]} for Senate and Governor races from the series list."""
        out: dict = {}
        names = sorted(STATES.values(), key=len, reverse=True)
        for s in self.series_list():
            if s.get("category") != "Elections":
                continue
            title = s.get("title") or ""
            if EXCLUDE.search(title):
                continue
            tm = re.search(r"^(?:KX)?(SENATEPARTY|SENATE|GOVPARTY|GOV)-?([A-Z]{2})$", s.get("ticker", ""))
            office = "senate" if re.search(r"senat", title, re.I) else ("governor" if re.search(r"governor", title, re.I) else None)
            if not office and tm:
                office = "senate" if tm.group(1).startswith("SENATE") else "governor"
            if not office:
                continue
            st = next((n for n in names if re.search(r"\b" + re.escape(n) + r"\b", title)), None)
            if not st:
                # tickers like SENATEPARTY-AZ / SENATEAZ / GOVPARTYMI carry the postal code
                st = STATES.get(tm.group(2)) if tm else None
            if not st:
                continue
            out.setdefault((st, office), []).append(s["ticker"])
        return out

    def markets_of_series(self, series_ticker: str, status: str = "open") -> list[dict]:
        out, cursor = [], None
        while True:
            res, lat = self.http.get(f"{BASE}/markets", {"series_ticker": series_ticker, "status": status, "limit": 1000, "cursor": cursor}, retries=2, timeout=20)
            self.state.ok(lat)
            out.extend((res or {}).get("markets", []))
            cursor = (res or {}).get("cursor")
            if not cursor:
                return out

    @staticmethod
    def current_event(markets: list[dict]) -> Optional[str]:
        """Kalshi tags events by inconsistent years (SENATEAR-26, GOVPARTYKS-27, GOVPARTYNH-28 are all Nov 2026 races; SENATEAR-28
        is 2028). Pick the event whose markets are expected to expire soonest after 1 Nov 2026 (and before July 2027)."""
        best = None
        for m in markets:
            if m.get("status") not in ("active", "open", "initialized"):
                continue
            exp = m.get("expected_expiration_time") or m.get("close_time") or ""
            if not ("2026-11" <= exp[:7] <= "2027-06"):
                continue
            if best is None or exp < best[0]:
                best = (exp, m["event_ticker"])
        return best[1] if best else None

    @staticmethod
    def party_legs(markets: list[dict], event: Optional[str] = None) -> dict:
        """{party: [{ticker, title, candidate}]} for the party-level markets of the current-cycle event."""
        event = event or Kalshi.current_event(markets)
        out: dict = {}
        for m in markets:
            if m.get("event_ticker") != event or m.get("status") not in ("active", "open", "initialized"):
                continue
            p = party_of(m)
            if not p:
                continue
            out.setdefault(p, []).append({"ticker": m["ticker"], "title": m.get("title"), "candidate": m.get("yes_sub_title")})
        return out

    # ------------------------------------------------------------ live quotes
    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        """{ticker: {bid, ask, bid_sz, ask_sz, depth_usd, last, ts}} via the batch tickers parameter (100 per call)."""
        out: dict[str, dict] = {}
        if not self.state.available():
            raise VenueError("kalshi circuit open")
        chunks = [tickers[i : i + 100] for i in range(0, len(tickers), 100)]

        def one(chunk):
            return self.http.get(f"{BASE}/markets", {"tickers": ",".join(chunk), "limit": 200}, retries=1, timeout=10)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, len(chunks))) as ex:
            futs = [ex.submit(one, c) for c in chunks]
            results = []
            for f in futs:
                try:
                    results.append(f.result())
                except VenueError as e:
                    self.state.fail(e)
                    raise
        for res, lat in results:
            self.state.ok(lat)
            for m in (res or {}).get("markets", []):
                bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
                bs, as_ = _f(m.get("yes_bid_size_fp")) or 0.0, _f(m.get("yes_ask_size_fp")) or 0.0
                if bid is not None and bid <= 0:
                    bid = None
                if ask is not None and ask >= 1:
                    ask = None
                dep = (bid or 0) * bs + (ask or 0) * as_
                out[m["ticker"]] = {"bid": bid, "ask": ask, "bid_sz": bs, "ask_sz": as_, "depth_usd": dep, "last": _f(m.get("last_price_dollars")),
                                    "ts": time.time(), "latency_ms": lat, "status": m.get("status")}
        return out

    def orderbook(self, ticker: str, depth: int = 10) -> dict:
        res, lat = self.http.get(f"{BASE}/markets/{ticker}/orderbook", {"depth": depth}, retries=1, timeout=10)
        self.state.ok(lat)
        ob = (res or {}).get("orderbook_fp") or {}
        yes = [(float(p), float(s)) for p, s in ob.get("yes_dollars") or []]
        no = [(float(p), float(s)) for p, s in ob.get("no_dollars") or []]
        bb = max(yes, key=lambda x: x[0]) if yes else None
        bn = max(no, key=lambda x: x[0]) if no else None
        return {"bid": bb[0] if bb else None, "bid_sz": bb[1] if bb else 0.0, "ask": (1 - bn[0]) if bn else None, "ask_sz": bn[1] if bn else 0.0}

    # --------------------------------------------------------------- history
    def candles(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, interval_min: int = 1) -> list[dict]:
        """[{ts, bid, ask, mid, close}] from candlesticks; the API caps a call at 5000 candles, so we chunk."""
        out: list[dict] = []
        step = 5000 * 60 * interval_min
        t = start_ts
        while t < end_ts:
            e = min(end_ts, t + step - 60)
            res, lat = self.http.get(f"{BASE}/series/{series_ticker}/markets/{ticker}/candlesticks",
                                     {"start_ts": t, "end_ts": e, "period_interval": interval_min}, retries=2, timeout=20)
            self.state.ok(lat)
            for c in (res or {}).get("candlesticks", []):
                bid = _f(((c.get("yes_bid") or {}).get("close_dollars")))
                ask = _f(((c.get("yes_ask") or {}).get("close_dollars")))
                close = _f((c.get("price") or {}).get("close_dollars")) or _f((c.get("price") or {}).get("previous_dollars"))
                mid = (bid + ask) / 2 if bid is not None and ask is not None else close
                out.append({"ts": float(c["end_period_ts"]), "bid": bid, "ask": ask, "mid": mid, "close": close, "volume": _f(c.get("volume_fp"))})
            t = e + 60
        return out


def _f(x):
    try:
        return float(x) if x is not None and x != "" else None
    except Exception:
        return None
