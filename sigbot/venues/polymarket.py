"""Polymarket: Gamma API for discovery, CLOB for live books and price history. Public, no keys.

Discovery: GET https://gamma-api.polymarket.com/events?tag_slug=midterms&closed=false&limit=100&offset=N (about 1,250
events: 35 "<state>-senate-election-winner", 35 "<state>-governor-winner-2026", 443 "<st>-<dd>-house-election-winner",
the chamber events 32224 / 32225 and derivatives). Each event carries its markets with `groupItemTitle` ("Jon Ossoff (D)",
"Democratic Party"), `question`, `clobTokenIds` (JSON list; first = YES token), `bestBid`/`bestAsk`, `liquidity`.
Live: POST https://clob.polymarket.com/books with [{token_id}] (<= 500 per call, ~0.2 s) -> bids ascending / asks
descending (best level LAST in both lists), sizes in shares. History: GET /prices-history?market=<token>&startTs&endTs&
fidelity=1|5|10 (minutes; 1-minute data for up to ~4 days per call, 5-minute for ~7 days, 10-minute for 30 days).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from .http import Http, VenueError, VenueState

log = logging.getLogger("sigbot.venues.polymarket")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
CHAMBER_EVENTS = {"senate": "which-party-will-win-the-senate-in-2026", "house": "which-party-will-win-the-house-in-2026"}
EXTRA_EVENT_SLUGS = ["california-governor-election-2026"]      # not tagged midterms

# Candidate-level events (no party in the title). Only the names that matter at the prices seen in Oct 2026.
CANDIDATE_PARTY = {
    "Mary Peltola": "D", "Dan Sullivan": "R", "Sen. Dan Sullivan": "R", "Daniel J. Sullivan Jr.": "R",
    "Jonathan Kreiss-Tomkins": "D", "Tom Begich": "D", "Bernadette Wilson": "R", "Click Bishop": "R", "Nancy Dahlstrom": "R",
    "Treg Taylor": "R", "Shelley Hughes": "R", "Adam Crum": "R", "David Bronson": "R", "Lisa Murkowski": "R",
    "Katie Porter": "D", "Xavier Becerra": "D", "Antonio Villaraigosa": "D", "Betty Yee": "D", "Tony Thurmond": "D",
    "Eric Swalwell": "D", "Chad Bianco": "R", "Steve Hilton": "R", "Ian Calderon": "D", "Toni Atkins": "D", "Stephen Cloobeck": "D",
    "Rick Caruso": "D", "Tom Steyer": "D", "Alex Padilla": "D", "Kamala Harris": "D",
}


def party_of(market: dict) -> Optional[str]:
    g = (market.get("groupItemTitle") or "").strip()
    q = market.get("question") or ""
    m = re.search(r"\((D|R|I|L|G)\)\s*$", g)
    if m:
        return {"D": "D", "R": "R"}.get(m.group(1), "I")
    if g in CANDIDATE_PARTY:
        return CANDIDATE_PARTY[g]
    low = (g + " " + q).lower()
    if re.search(r"democrat", low):
        return "D"
    if re.search(r"republican", low):
        return "R"
    if re.search(r"\bindependent\b", low):
        return "I"
    return None


def yes_token(market: dict) -> Optional[str]:
    try:
        toks = json.loads(market.get("clobTokenIds") or "[]")
        return str(toks[0]) if toks else None
    except Exception:
        return None


class Polymarket:
    def __init__(self, http: Http):
        self.http = http
        self.state = VenueState("polymarket")

    # ------------------------------------------------------------ discovery
    def discover(self) -> list[dict]:
        """All open midterms events (plus a few extra slugs), each with its markets."""
        events: list[dict] = []
        off = 0
        while True:
            page, lat = self.http.get(f"{GAMMA}/events", {"closed": "false", "limit": 100, "offset": off, "tag_slug": "midterms"}, retries=2, timeout=20)
            self.state.ok(lat)
            events.extend(page or [])
            if not page or len(page) < 100:
                break
            off += 100
        have = {e["slug"] for e in events}
        for slug in EXTRA_EVENT_SLUGS:
            if slug in have:
                continue
            try:
                page, lat = self.http.get(f"{GAMMA}/events", {"slug": slug}, retries=1, timeout=15)
                self.state.ok(lat)
                events.extend(page or [])
            except VenueError as e:
                log.warning("extra event %s: %s", slug, e)
        return events

    @staticmethod
    def party_legs(event: dict) -> dict:
        """{party: [{market_id, token, title, liquidity}]} over the active markets of one event."""
        out: dict = {}
        for m in event.get("markets", []):
            if not m.get("active") or m.get("closed") or not m.get("enableOrderBook", True):
                continue
            tok = yes_token(m)
            p = party_of(m)
            if not tok or not p:
                continue
            out.setdefault(p, []).append({"market_id": str(m["id"]), "token": tok, "title": m.get("groupItemTitle") or m.get("question"),
                                          "liquidity": float(m.get("liquidityNum") or m.get("liquidity") or 0)})
        return out

    # ------------------------------------------------------------ live books
    def books(self, tokens: list[str]) -> dict[str, dict]:
        """{token: {bid, ask, bid_sz, ask_sz, depth_usd, last, ts}} from the batch books endpoint (chunks of 500)."""
        out: dict[str, dict] = {}
        if not self.state.available():
            raise VenueError("polymarket circuit open")
        for i in range(0, len(tokens), 500):
            chunk = tokens[i : i + 500]
            try:
                res, lat = self.http.post(f"{CLOB}/books", [{"token_id": t} for t in chunk], retries=1, timeout=10)
            except VenueError as e:
                self.state.fail(e)
                raise
            self.state.ok(lat)
            for b in res or []:
                tok = str(b.get("asset_id"))
                bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
                asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
                bb = max(bids, key=lambda x: x[0]) if bids else None
                ba = min(asks, key=lambda x: x[0]) if asks else None
                # depth in USD within 2 cents of the touch, both sides
                dep = 0.0
                if bb:
                    dep += sum(p * s for p, s in bids if p >= bb[0] - 0.02)
                if ba:
                    dep += sum(p * s for p, s in asks if p <= ba[0] + 0.02)
                out[tok] = {"bid": bb[0] if bb else None, "ask": ba[0] if ba else None, "bid_sz": bb[1] if bb else 0.0,
                            "ask_sz": ba[1] if ba else 0.0, "depth_usd": dep, "last": _f(b.get("last_trade_price")),
                            "ts": (float(b["timestamp"]) / 1000.0) if b.get("timestamp") else time.time(), "latency_ms": lat}
        return out

    def midpoints(self, tokens: list[str]) -> dict[str, float]:
        """{token: midpoint} from the batch midpoints endpoint (chunks of 500): a light poll for shock detection (no depth)."""
        out: dict[str, float] = {}
        if not self.state.available():
            raise VenueError("polymarket circuit open")
        for i in range(0, len(tokens), 500):
            chunk = tokens[i : i + 500]
            try:
                res, lat = self.http.post(f"{CLOB}/midpoints", [{"token_id": t} for t in chunk], retries=0, timeout=5)
            except VenueError as e:
                self.state.fail(e)
                raise
            self.state.ok(lat)
            for tok, mid in (res or {}).items():
                m = _f(mid)
                if m is not None:
                    out[str(tok)] = m
        return out

    # -------------------------------------------------------------- history
    def history(self, token: str, start_ts: int, end_ts: int, fidelity: int = 5) -> list[tuple[float, float]]:
        """[(ts, price)] from prices-history; fidelity in minutes."""
        res, lat = self.http.get(f"{CLOB}/prices-history", {"market": token, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity}, retries=2, timeout=20)
        self.state.ok(lat)
        return [(float(h["t"]), float(h["p"])) for h in (res or {}).get("history", [])]


def _f(x):
    try:
        return float(x) if x is not None else None
    except Exception:
        return None
