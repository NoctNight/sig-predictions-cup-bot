"""News-driven sweeper: read headlines, classify them with Claude, take liquidity before the venue reprices.

    python -m sigbot.news --once --dry-run          # poll every source once, classify, print what it would do (default)
    python -m sigbot.news --once --dry-run --no-llm # feeds + pre-filter only (no ANTHROPIC_API_KEY needed)
    python -m sigbot.news --dry-run                 # loop every news.poll_sec
    python -m sigbot.news --live                    # loop and place taker orders (asks for nothing: be sure)

Pipeline, once per poll:
  1. sources   RSS/Atom feeds (urllib + xml.etree), Google News RSS queries (site: queries for outlets that block
               direct RSS, plus one query per competitive race, rotating), and the venue's own per-market "Related
               News" JSON (web route /api/markets/[id]/news?marketId=N, rotating over the universe). Items are
               deduplicated by URL in the news_items table; anything older than max_item_age_hours is dropped.
  2. lead      when a feed only gives a headline and the link is a plain article URL, fetch the page (robots.txt
               honoured, timeout, no JS) and keep the first paragraphs.
  3. prefilter cheap keyword test against the universe (state names, district codes, office words).
  4. screen    batched headlines + leads -> claude-haiku-5-5 (structured JSON): relevant? which race?
  5. final     relevant items + the race's current venue prices -> claude-sonnet-5-5: party, direction, expected
               move in points, confidence, one-line reason, whether this is new information.
  6. act       confidence >= min_confidence and move >= min_move_pts and new information: compare with the current
               touch; if the market has not already moved more than already_moved_frac of the expected move
               (reference = 5-minute candle close before publication), buy YES at the ask (up) or buy NO at
               1 - bid (down) for min(per_event_budget, free sleeve capital), expiring after order_ttl_sec, and
               pause the market maker on every leg of the race (params key news_pause, read by sigbot.mm).
  7. follow-up mid at +5 / +30 / +120 minutes written into news_events (from the bulk price snapshot while the
               loop runs; sigbot.news_report backfills from candles for anything missed).
Every decision, including dry-run ones and skips, is a news_events row so the hit rate can be measured before
sizing up. Stdlib only. Claude is called over raw HTTPS (Messages API) with ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import email.utils
import hashlib
import html
import html.parser
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .api import ApiError, SuperMarketClient, _ssl_context
from .config import BotConfig, NewsConfig, TICK, round_tick
from .store import Store
from .universe import Universe

log = logging.getLogger("sigbot.news")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ELECTION_DAY = "2026-11-03"

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
OFFICE_WORDS = re.compile(r"\b(senate|senator|governor|gubernatorial|congress(ional|man|woman)?|house (seat|race|district)|midterm|"
                          r"district|poll(s|ing)?|primary|ballot|GOP|Democrat(s|ic)?|Republican(s)?|DCCC|NRCC|DSCC|NRSC|endorse|"
                          r"drops out|withdraw|indict|scandal|debate)\b", re.I)
DISTRICT_RE = re.compile(r"\b([A-Z]{2})-(\d{2})\b")


# --------------------------------------------------------------------------- small helpers
def _key_file() -> str:
    """Fallback: `.anthropic_api_key` next to config.json (same pattern as .sig_api_key)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".anthropic_api_key")
    try:
        return open(path).read().strip() if os.path.exists(path) else ""
    except OSError:
        return ""


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = s.strip()
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None


class _TextExtractor(html.parser.HTMLParser):
    """Collects <p> paragraphs and the og:description of an article page."""

    SKIP = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form", "button"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras: list[str] = []
        self.og: Optional[str] = None
        self._buf: list[str] = []
        self._in_p = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "p" and not self._skip:
            self._in_p, self._buf = True, []
        elif tag == "meta":
            a = dict(attrs)
            if a.get("property") in ("og:description", "twitter:description") and a.get("content") and not self.og:
                self.og = a["content"].strip()

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "p" and self._in_p:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(text) >= 60:
                self.paras.append(text)
            self._in_p = False

    def handle_data(self, data):
        if self._in_p and not self._skip:
            self._buf.append(data)


def strip_html(s: Optional[str]) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_url(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(u.strip())
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "cmpid", "ocid"))]
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urllib.parse.urlencode(q), ""))
    except Exception:
        return u.strip()


@dataclass
class Item:
    source: str
    url: str
    title: str
    published: Optional[float]
    lead: str = ""
    key: str = ""
    id: Optional[int] = None
    race_hint: Optional[str] = None      # set for venue news / race queries (where the query already names the race)

    def finish(self):
        self.title = strip_html(self.title)[:300]
        self.lead = strip_html(self.lead)[:2000]
        self.key = norm_url(self.url) if self.url and "news.google.com" not in self.url else hashlib.sha1(f"{self.source}|{self.title}".encode()).hexdigest()
        return self


# --------------------------------------------------------------------------- fetching
class Fetcher:
    def __init__(self, cfg: NewsConfig):
        self.cfg = cfg
        self.ctx = _ssl_context()
        self.robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    def get(self, url: str, timeout: float, accept: str = "*/*", max_bytes: int = 2_000_000, headers: Optional[dict] = None) -> tuple[int, str, bytes]:
        h = {"User-Agent": self.cfg.user_agent, "Accept": accept}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read(max_bytes)

    def allowed(self, url: str) -> bool:
        p = urllib.parse.urlsplit(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                st, _, body = self.get(host + "/robots.txt", min(5.0, self.cfg.article_timeout_sec), max_bytes=200_000)
                rp.parse(body.decode("utf-8", "replace").splitlines())
                self.robots[host] = rp
            except urllib.error.HTTPError as e:
                self.robots[host] = None if e.code in (404, 410) else rp   # no robots.txt -> allowed; other errors -> rp (empty) allows all
            except Exception:
                self.robots[host] = None
        rp = self.robots[host]
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.cfg.user_agent, url) and rp.can_fetch("*", url)
        except Exception:
            return True

    # ---- feeds
    def feed(self, name: str, url: str) -> list[Item]:
        st, ctype, body = self.get(url, 12.0, accept="application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5")
        return parse_feed(name, body)

    def google_news(self, name: str, query: str) -> list[Item]:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        items = self.feed(name, url)
        for it in items:
            # Google News titles are "Headline - Outlet"; the link is a redirect, so there is nothing to fetch
            m = re.match(r"^(.*)\s+-\s+([^-]{2,40})$", it.title)
            if m:
                it.title, it.source = m.group(1).strip(), f"{m.group(2).strip()} (via Google News)"
        return items

    def venue_news(self, market_id: str, race: str, party: str) -> list[Item]:
        url = self.cfg.venue_news_url.format(market_id=market_id)
        st, ctype, body = self.get(url, 12.0, accept="application/json")
        d = json.loads(body.decode("utf-8", "replace"))
        out = []
        for h in d.get("headlines", []) or []:
            if not h.get("url") or not h.get("title"):
                continue
            lead = " ".join(x for x in (h.get("summary"), h.get("relevanceExplanation")) if x)
            out.append(Item(source=f"venue:{h.get('source') or 'related news'}", url=h["url"], title=h["title"],
                            published=parse_date(h.get("publishedDate")), lead=lead, race_hint=race).finish())
        return out

    # ---- article lead
    def article_lead(self, url: str) -> str:
        if not url or "news.google.com" in url or not self.allowed(url):
            return ""
        try:
            st, ctype, body = self.get(url, self.cfg.article_timeout_sec, accept="text/html,application/xhtml+xml", max_bytes=600_000)
        except Exception as e:
            log.debug("article %s: %s", url, e)
            return ""
        if "html" not in (ctype or "").lower():
            return ""
        ex = _TextExtractor()
        try:
            ex.feed(body.decode("utf-8", "replace"))
        except Exception:
            pass
        out, n = [], 0
        for ptxt in ex.paras:
            out.append(ptxt)
            n += len(ptxt)
            if n >= self.cfg.article_max_chars:
                break
        text = " ".join(out)[: self.cfg.article_max_chars]
        if len(text) < 80 and ex.og:
            text = ex.og
        return text


def parse_feed(source: str, body: bytes) -> list[Item]:
    root = ET.fromstring(body)
    items: list[Item] = []
    ns_atom = "{http://www.w3.org/2005/Atom}"
    ns_content = "{http://purl.org/rss/1.0/modules/content/}"
    ns_media = "{http://search.yahoo.com/mrss/}"
    for it in root.iter("item"):                              # RSS 2.0
        title = it.findtext("title") or ""
        link = (it.findtext("link") or "").strip()
        if not link:
            g = it.find("guid")
            if g is not None and (g.get("isPermaLink", "true") != "false") and (g.text or "").startswith("http"):
                link = g.text.strip()
        desc = it.findtext("description") or it.findtext(ns_content + "encoded") or it.findtext(ns_media + "description") or ""
        pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")
        if title and link:
            items.append(Item(source=source, url=link, title=title, published=parse_date(pub), lead=desc).finish())
    for e in root.iter(ns_atom + "entry"):                     # Atom
        title = e.findtext(ns_atom + "title") or ""
        link = ""
        for l in e.findall(ns_atom + "link"):
            if l.get("rel", "alternate") == "alternate" and l.get("href"):
                link = l.get("href")
                break
        desc = e.findtext(ns_atom + "summary") or e.findtext(ns_atom + "content") or ""
        pub = e.findtext(ns_atom + "published") or e.findtext(ns_atom + "updated")
        if title and link:
            items.append(Item(source=source, url=link, title=title, published=parse_date(pub), lead=desc).finish())
    return items


# --------------------------------------------------------------------------- Claude (raw Messages API)
class ClaudeClient:
    def __init__(self, api_key: str, timeout: float = 90.0):
        self.api_key, self.timeout, self.ctx = api_key, timeout, _ssl_context()
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}

    def json_call(self, model: str, system: str, user: str, schema: dict, max_tokens: int = 4096, effort: str = "medium",
                  fallbacks: bool = False) -> Optional[dict]:
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system,
                                "messages": [{"role": "user", "content": user}],
                                "output_config": {"format": {"type": "json_schema", "schema": schema}, "effort": effort}}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        if fallbacks:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        data = json.dumps(body).encode()
        for attempt in range(4):
            self.usage["calls"] += 1
            try:
                req = urllib.request.Request(ANTHROPIC_URL, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                    out = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")[:500]
                if e.code in (429, 500, 502, 503, 529, 408) and attempt < 3:
                    ra = e.headers.get("retry-after")
                    wait = float(ra) if ra and ra.isdigit() else 3.0 * (attempt + 1)
                    log.warning("claude %s -> %s; retry in %.0fs: %s", model, e.code, wait, raw)
                    time.sleep(wait)
                    continue
                self.usage["errors"] += 1
                log.error("claude %s -> %s: %s", model, e.code, raw)
                return None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 3:
                    time.sleep(3.0 * (attempt + 1))
                    continue
                self.usage["errors"] += 1
                log.error("claude %s network error: %s", model, e)
                return None
        else:
            return None
        u = out.get("usage", {}) or {}
        self.usage["input_tokens"] += int(u.get("input_tokens") or 0)
        self.usage["output_tokens"] += int(u.get("output_tokens") or 0)
        if out.get("stop_reason") == "refusal":
            log.warning("claude %s refused: %s", model, (out.get("stop_details") or {}).get("category"))
            return None
        if out.get("stop_reason") == "max_tokens":
            log.warning("claude %s hit max_tokens; output may be truncated", model)
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        try:
            return json.loads(text)
        except Exception:
            log.error("claude %s returned non-JSON: %s", model, text[:300])
            self.usage["errors"] += 1
            return None


SCREEN_SYSTEM = """You screen political news headlines for a prediction-market desk trading the 2026 U.S. midterm elections.
The tradable markets are "Will the Democratic/Republican/Independent Party win <race>?" for the races listed.
For each item say whether it is RELEVANT: it reports something that could change the probability that one party wins a
specific listed race (a poll, a candidate dropping out, an endorsement, a scandal, a court ruling on the race, a major
ad buy, a debate verdict, a death or health event, a ballot-access decision). National mood pieces, retrospectives,
explainers, odds pages, fundraising roundups and opinion columns are NOT relevant unless they carry a concrete new fact
about a listed race. Name the race exactly as listed. If an item concerns several listed races, pick the most affected one."""

FINAL_SYSTEM = """You are the final judge for a prediction-market desk that trades "Will the D/R/I Party win <race>?" markets for
the 2026 U.S. midterms on a small play-money venue whose prices lag the news by minutes to hours. Prices are the
probability (0-1) that the party wins the race. Election day is {election_day}; today is {today} UTC.

For each item decide whether it is NEW information that should move a listed race's price, and by how much:
  * race: exactly one of the listed race names.
  * party: D, R or I - the party whose YES market you are calling.
  * direction: up or down, for that party's YES price.
  * expected_move_pts: how many percentage points the venue's price should move from the current mid once the news is
    priced in (e.g. a 55% -> 62% move is 7). Be calibrated: an ordinary poll inside the existing range is 0-2; a
    candidate withdrawing, a major scandal or a shock poll in a close race is 5-20; a race already priced below 5% or
    above 95% rarely moves more than 1-2 points on anything short of a withdrawal.
  * confidence: probability (0-1) that the price moves in that direction by at least a third of expected_move_pts
    within two hours. Most headlines deserve well under 0.5. Only use >= 0.8 for unambiguous, fresh, verified facts
    from a reputable outlet about a close or mispriced race.
  * new_information: false if the item is analysis of already-known facts, an odds/forecast page, a stale story
    (published more than a few hours ago relative to today), or already reflected in the current price.
  * reason: one line.
Use the current venue prices given with each item: if the price already reflects the news, lower expected_move_pts
and set new_information to false."""

SCREEN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["id", "relevant", "race", "note"],
    "properties": {"id": {"type": "integer"}, "relevant": {"type": "boolean"}, "race": {"anyOf": [{"type": "string"}, {"type": "null"}]}, "note": {"type": "string"}}}}}}

FINAL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["calls"], "properties": {"calls": {"type": "array", "items": {
    "type": "object", "additionalProperties": False,
    "required": ["id", "race", "party", "direction", "expected_move_pts", "confidence", "new_information", "reason"],
    "properties": {"id": {"type": "integer"}, "race": {"type": "string"}, "party": {"type": "string", "enum": ["D", "R", "I"]},
                   "direction": {"type": "string", "enum": ["up", "down"]}, "expected_move_pts": {"type": "number"},
                   "confidence": {"type": "number"}, "new_information": {"type": "boolean"}, "reason": {"type": "string"}}}}}}


# --------------------------------------------------------------------------- the sweeper
@dataclass
class Decision:
    item: dict
    race: str
    party: str
    exchange_id: str
    direction: str
    expected: float
    confidence: float
    reason: str
    new_information: bool
    action: str = ""
    detail: dict = field(default_factory=dict)


class NewsSweeper:
    def __init__(self, api: SuperMarketClient, store: Store, cfg: NewsConfig, universe: Universe, starting_cash: float,
                 dry_run: bool = True, use_llm: bool = True, anthropic_key: Optional[str] = None):
        self.api, self.store, self.cfg, self.u = api, store, cfg, universe
        self.starting_cash = starting_cash
        self.dry_run = dry_run or not cfg.allow_live        # two switches must both be on before a news order goes out
        if not dry_run and not cfg.allow_live:
            log.info("news: bot is live but news.allow_live is false -> sweeper runs dry (logs only)")
        self.fetch = Fetcher(cfg)
        key = anthropic_key or os.environ.get("ANTHROPIC_API_KEY", "") or _key_file()
        self.llm = ClaudeClient(key, cfg.llm_timeout_sec) if (use_llm and key) else None
        if use_llm and not key:
            log.warning("ANTHROPIC_API_KEY not set and no .anthropic_api_key file: headlines are collected and pre-filtered but not classified")
        self.last_poll = 0.0
        self.last: dict = {}
        self.race_cursor = 0
        self.venue_cursor = 0
        self.vocab = self._vocab()

    # ---- vocabulary / prefilter
    def _vocab(self) -> list[re.Pattern]:
        words = set()
        for name in self.u.races:
            m = DISTRICT_RE.match(name)
            if m:
                st = STATES.get(m.group(1))
                words.add(re.escape(name.split(" ")[0]))                     # "AZ-01"
                if st:
                    n = int(m.group(2))
                    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
                    words.add(rf"{re.escape(st)}'?s? {n}{suffix}")           # "Arizona's 1st"
                    words.add(rf"{re.escape(st)} (?:House|congressional)")
            else:
                for st in STATES.values():
                    if name.startswith(st):
                        words.add(re.escape(st))
        words.update([r"U\.S\. House", r"U\.S\. Senate", "House majority", "Senate majority", "control of the (House|Senate)"])
        return [re.compile(r"\b(" + "|".join(sorted(words, key=len, reverse=True)) + r")\b", re.I), OFFICE_WORDS]

    def prefilter(self, it: Item) -> bool:
        if it.race_hint:
            return True
        text = f"{it.title} {it.lead}"
        return bool(self.vocab[0].search(text)) and bool(self.vocab[1].search(text))

    def match_race(self, name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        if name in self.u.races:
            return name
        low = name.strip().lower()
        for r in self.u.races:
            if r.lower() == low:
                return r
        for r in self.u.races:                      # "Rhode Island Senate race" / "RI Senate"
            if r.lower() in low or low in r.lower():
                return r
        return None

    # ---- sources
    def competitive_races(self, prices: dict[str, dict]) -> list[str]:
        out = []
        for name, race in self.u.races.items():
            mids = []
            for leg in race.legs.values():
                p = prices.get(leg.exchange_id)
                if p and p.get("bestBid") is not None and p.get("bestAsk") is not None:
                    mids.append((p["bestBid"] + p["bestAsk"]) / 2)
            if mids and any(self.cfg.race_query_min_mid <= m <= 1 - self.cfg.race_query_min_mid for m in mids):
                out.append(name)
        return out

    def collect(self, prices: dict[str, dict]) -> tuple[list[Item], dict]:
        jobs: list[tuple[str, Any]] = []
        for name, url in self.cfg.feeds.items():
            jobs.append((name, lambda n=name, u=url: self.fetch.feed(n, u)))
        for site in self.cfg.google_news_sites:
            q = f"site:{site} ({self.cfg.google_news_site_terms})"
            jobs.append((f"gnews:{site}", lambda s=site, q=q: self.fetch.google_news(s, q)))
        if self.cfg.race_queries:
            races = self.competitive_races(prices) or list(self.u.races)
            k = min(self.cfg.race_queries_per_poll, len(races))
            for i in range(k):
                r = races[(self.race_cursor + i) % len(races)]
                q = f'"{r.replace(" House race", "")}"' + (" congressional" if "House race" in r else " election")
                jobs.append((f"gnews:{r}", lambda r=r, q=q: [it for it in self.fetch.google_news("gnews", q) if setattr(it, "race_hint", r) or True]))
            self.race_cursor = (self.race_cursor + k) % max(1, len(races))
        if self.cfg.venue_news:
            legs = [(race.name, party, leg.market_id) for race in self.u.races.values() for party, leg in race.legs.items()]
            legs.sort()
            k = min(self.cfg.venue_news_per_poll, len(legs))
            for i in range(k):
                race, party, mid = legs[(self.venue_cursor + i) % len(legs)]
                jobs.append((f"venue:{race}", lambda m=mid, r=race, p=party: self.fetch.venue_news(m, r, p)))
            self.venue_cursor = (self.venue_cursor + k) % max(1, len(legs))
        items: list[Item] = []
        stats = {"sources": len(jobs), "failed": 0, "items": 0}
        # Never block the trading loop on a slow source: a DNS lookup has no timeout, so a `with` block (which waits for
        # every worker on exit) could hang the cycle for minutes on a flaky network. Collect what finishes within 30 s,
        # count the rest as failed, and let the stragglers finish in the background.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        futs = {pool.submit(fn): name for name, fn in jobs}
        try:
            for f in concurrent.futures.as_completed(futs, timeout=30):
                name = futs[f]
                try:
                    got = f.result()
                    items.extend(got)
                    log.debug("source %s: %d items", name, len(got))
                except Exception as e:
                    stats["failed"] += 1
                    log.warning("source %s failed: %s", name, str(e)[:160])
        except concurrent.futures.TimeoutError:
            late = [n for f, n in futs.items() if not f.done()]
            stats["failed"] += len(late)
            log.warning("news: %d sources still pending after 30 s, skipped this round: %s", len(late), late[:5])
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        stats["items"] = len(items)
        return items, stats

    def ingest(self, items: list[Item]) -> list[Item]:
        """Dedupe against the store, drop old items, fetch leads for the ones that pass the pre-filter. Returns new items."""
        now = _now()
        cutoff = now - self.cfg.max_item_age_hours * 3600
        fresh: list[Item] = []
        seen_keys = set()
        for it in items:
            if not it.key or it.key in seen_keys or self.store.news_item_exists(it.key):
                continue
            seen_keys.add(it.key)
            if it.published is not None and it.published < cutoff:
                continue
            keep = self.prefilter(it)
            if keep and self.cfg.fetch_article and len(it.lead) < 200:
                lead = self.fetch.article_lead(it.url)
                if lead:
                    it.lead = (it.lead + " " + lead).strip()[:2000]
            it.id = self.store.add_news_item(it.key, it.source, it.url, it.title, _iso(it.published) if it.published else None, it.lead, keep)
            if keep:
                fresh.append(it)
        return fresh

    # ---- classification
    def screen(self, rows: list) -> list[dict]:
        """rows: news_items rows pending screening. Returns dicts of relevant items with their race."""
        relevant: list[dict] = []
        if not rows:
            return relevant
        if self.llm is None:
            return relevant
        race_list = "\n".join(sorted(self.u.races))
        for i in range(0, len(rows), self.cfg.screen_batch):
            batch = rows[i : i + self.cfg.screen_batch]
            lines = []
            for r in batch:
                lines.append(f"[{r['id']}] ({r['source']}; {r['published'] or 'date unknown'}) {r['title']}\n    lead: {(r['lead'] or '')[:600]}")
            user = f"Listed races:\n{race_list}\n\nItems:\n" + "\n".join(lines) + "\n\nReturn one entry per item id."
            out = self.llm.json_call(self.cfg.screen_model, SCREEN_SYSTEM, user, SCREEN_SCHEMA, max_tokens=4096, effort="low")
            got = {x["id"]: x for x in (out or {}).get("items", []) if isinstance(x, dict) and "id" in x}
            for r in batch:
                x = got.get(r["id"])
                if x is None:
                    self.store.set_news_screen(r["id"], None, {"error": "no answer"}, status=1 if out is not None else 0)
                    continue
                race = self.match_race(x.get("race")) or self.match_race(r["screen_race"] if "screen_race" in r.keys() else None)
                self.store.set_news_screen(r["id"], race, x)
                if x.get("relevant") and race:
                    relevant.append({"id": r["id"], "source": r["source"], "url": r["url"], "title": r["title"], "published": r["published"],
                                     "lead": r["lead"] or "", "race": race, "note": x.get("note", "")})
        return relevant

    def race_prices_text(self, race: str, prices: dict[str, dict]) -> str:
        parts = []
        for party, leg in self.u.races[race].legs.items():
            p = prices.get(leg.exchange_id) or {}
            bb, ba = p.get("bestBid"), p.get("bestAsk")
            if bb is not None and ba is not None:
                parts.append(f"{party} {100 * (bb + ba) / 2:.1f}% (bid {bb:.3f} / ask {ba:.3f})")
            else:
                parts.append(f"{party} last {p.get('latestPrice')}")
        return "; ".join(parts)

    def final(self, relevant: list[dict], prices: dict[str, dict]) -> list[Decision]:
        if not relevant or self.llm is None:
            return []
        lines = []
        for r in relevant:
            lines.append(f"[{r['id']}] race: {r['race']} | current venue prices: {self.race_prices_text(r['race'], prices)}\n"
                         f"    source: {r['source']} | published: {r['published'] or 'unknown'}\n    headline: {r['title']}\n"
                         f"    lead: {r['lead'][:1200]}\n    screener note: {r['note']}")
        user = "Items:\n" + "\n".join(lines) + "\n\nReturn one call per item id (every id must appear)."
        system = FINAL_SYSTEM.format(election_day=ELECTION_DAY, today=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
        out = self.llm.json_call(self.cfg.final_model, system, user, FINAL_SCHEMA, max_tokens=8192, effort="medium", fallbacks=True)
        calls = {x["id"]: x for x in (out or {}).get("calls", []) if isinstance(x, dict) and "id" in x}
        decisions: list[Decision] = []
        for r in relevant:
            x = calls.get(r["id"])
            if x is None:
                continue
            self.store.set_news_final(r["id"], x)
            race = self.match_race(x.get("race")) or r["race"]
            party = x.get("party")
            if race not in self.u.races or party not in self.u.races[race].legs:
                log.info("news: call on unknown leg %s/%s for item %s", race, party, r["id"])
                continue
            decisions.append(Decision(item=r, race=race, party=party, exchange_id=self.u.races[race].legs[party].exchange_id,
                                      direction=x.get("direction", "up"), expected=float(x.get("expected_move_pts") or 0.0),
                                      confidence=float(x.get("confidence") or 0.0), reason=str(x.get("reason", ""))[:400],
                                      new_information=bool(x.get("new_information"))))
        return decisions

    # ---- reference mid before the news
    def reference_mid(self, ex: str, published: Optional[str], first_seen: Optional[float]) -> Optional[float]:
        ts = parse_date(published) if published else None
        ts = ts or first_seen
        if ts is None:
            return None
        if _now() - ts < 6 * 3600:
            try:
                h = self.api.price_history(ex, "5m", 80)
                best = None
                for c in h.get("candles", []):
                    ct = parse_date(c.get("time") or c.get("timestamp") or c.get("t") or c.get("startTime"))
                    if ct is not None and ct <= ts - 300 and c.get("close") is not None:
                        best = c["close"]
                if best is not None:
                    return float(best)
            except (ApiError, Exception) as e:
                log.debug("price history %s: %s", ex, e)
        q = self.store.last_quote(ex, ts)
        return float(q["mid"]) if q and q["mid"] is not None else None

    # ---- execution
    def act(self, d: Decision, prices: dict[str, dict], cash: float, cycle_id: Optional[int]) -> dict:
        ex = d.exchange_id
        it = d.item
        base = dict(item_id=it["id"], race=d.race, party=d.party, exchange_id=ex, direction=d.direction, expected_move_pts=d.expected,
                    confidence=d.confidence, reason=d.reason, model=self.cfg.final_model, headline=it["title"], source=it["source"],
                    url=it["url"], published=it["published"], dry_run=int(self.dry_run))
        p = prices.get(ex) or {}
        bb, ba = p.get("bestBid"), p.get("bestAsk")
        mid = (bb + ba) / 2 if bb is not None and ba is not None else p.get("latestPrice")
        base.update(bid_before=bb, ask_before=ba, mid_before=mid)
        if not d.new_information or d.confidence < self.cfg.min_confidence or d.expected < self.cfg.min_move_pts:
            base.update(action="skipped_threshold")
            self.store.record_news_event(**base)
            return base
        if bb is None or ba is None:
            base.update(action="error", response={"error": "no touch"})
            self.store.record_news_event(**base)
            return base
        row = self.store.one("SELECT first_seen FROM news_items WHERE id=?", (it["id"],))
        ref = self.reference_mid(ex, it["published"], row["first_seen"] if row else None)
        sign = 1.0 if d.direction == "up" else -1.0
        moved = (mid - ref) * 100 * sign if ref is not None else 0.0
        base.update(ref_mid=ref, moved_pts=moved)
        if moved > self.cfg.already_moved_frac * d.expected:
            base.update(action="skipped_moved")
            self.store.record_news_event(**base)
            return base
        day_ago = _now() - 86400
        if self.store.news_events_for_race_since(d.race, day_ago) >= self.cfg.max_events_per_race_per_day:
            base.update(action="skipped_race_cap")
            self.store.record_news_event(**base)
            return base
        budget_total = min(self.cfg.capital_frac * cash, self.cfg.capital_frac * self.starting_cash)
        free = budget_total - self.store.news_open_cost()
        budget = min(self.cfg.per_event_budget, free)
        if budget < 20:
            base.update(action="skipped_budget", response={"free": free})
            self.store.record_news_event(**base)
            return base
        # fresh touch right before sending (the bulk snapshot can be a cycle old)
        try:
            book = self.api.orderbook(ex, 5)
            if book.get("bids") and book.get("asks"):
                bb, ba = book["bids"][0]["price"], book["asks"][0]["price"]
                base.update(bid_before=bb, ask_before=ba, mid_before=(bb + ba) / 2)
        except ApiError as e:
            log.warning("news: book %s: %s", ex, e)
        if d.direction == "up":
            side, price, collateral = "yes", round_tick(ba), ba                       # buy YES at the ask
        else:
            side, price, collateral = "no", round_tick(1 - bb), 1 - bb                # buy NO at 1 - bid = sell YES at the bid
        qty = int(budget / max(collateral, TICK))
        if qty < 1:
            base.update(action="skipped_budget")
            self.store.record_news_event(**base)
            return base
        cost = qty * collateral
        base.update(side=side, price=price, quantity=qty, cost=cost)
        if self.dry_run:
            base.update(action="dry")
            eid = self.store.record_news_event(**base)
            log.info("[DRY] NEWS %s %s %s -> %s BUY-%s px=%.3f q=%d cost=%.0f (exp %+.1f pts, conf %.2f, moved %+.1f) %s; would pause the MM on %s",
                     d.race, d.party, d.direction.upper(), ex, side.upper(), price, qty, cost, d.expected, d.confidence, moved, d.reason, d.race)
            return dict(base, event_id=eid)
        ttl = (datetime.now(timezone.utc) + timedelta(seconds=self.cfg.order_ttl_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            resp = self.api.place_order(ex, side, "buy", qty, price, expiration=ttl)
        except ApiError as e:
            base.update(action="error", response={"error": str(e), "details": e.details})
            self.store.record_news_event(**base)
            log.error("news order failed %s: %s", ex, e)
            return base
        oid = resp.get("orderId") or resp.get("id")
        self.store.record_order(oid, ex, None, side, "buy", price, qty, "news", None, (price - mid) / TICK if mid else None, None, mid,
                                None, None, cycle_id, ttl, "open" if resp.get("open") else "filled", resp)
        base.update(action="traded", order_id=oid, filled_qty=resp.get("filledQuantity"), response=resp)
        eid = self.store.record_news_event(**base)
        log.info("NEWS TRADE %s %s %s -> %s BUY-%s px=%.3f q=%d cost=%.0f order=%s (exp %+.1f pts, conf %.2f, moved %+.1f) %s", d.race, d.party,
                 d.direction.upper(), ex, side.upper(), price, qty, cost, oid, d.expected, d.confidence, moved, d.reason)
        self.pause_race(d.race)
        return dict(base, event_id=eid)

    def pause_race(self, race: str):
        until = _now() + self.cfg.mm_pause_sec
        with self.store.lock:                        # read-modify-write shared with the MM and shock mode
            cur = self.store.get_param("news_pause", {}) or {}
            for leg in self.u.races[race].legs.values():
                cur[leg.exchange_id] = max(float(cur.get(leg.exchange_id, 0)), until)
            self.store.set_param("news_pause", cur)
        log.info("news: market maker paused on %s until %s", race, _iso(until))

    def followups(self, prices: dict[str, dict]):
        for minutes in self.cfg.followup_minutes:
            col = f"mid_{minutes}"
            if col not in ("mid_5", "mid_30", "mid_120"):
                continue
            for ev in self.store.news_events_pending_followup(col, minutes * 60):
                p = prices.get(ev["exchange_id"]) or {}
                bb, ba = p.get("bestBid"), p.get("bestAsk")
                if bb is not None and ba is not None:
                    self.store.set_news_followup(ev["id"], col, (bb + ba) / 2)

    # ---- one pass
    def cycle(self, prices: dict[str, dict], cash: float, cycle_id: Optional[int] = None, force: bool = False) -> dict:
        summary: dict[str, Any] = {"polled": False}
        self.followups(prices)
        if not force and _now() - self.last_poll < self.cfg.poll_sec:
            return summary
        self.last_poll = _now()
        t0 = _now()
        items, stats = self.collect(prices)
        fresh = self.ingest(items)
        summary.update(polled=True, sources=stats["sources"], failed=stats["failed"], seen=stats["items"], new=len(fresh), fetch_sec=round(_now() - t0, 1))
        # anything still unscreened after max_item_age_hours (e.g. collected while no key was set) is stale: drop it
        self.store.exec("UPDATE news_items SET screened=-1 WHERE screened=0 AND first_seen<?", (_now() - self.cfg.max_item_age_hours * 3600,))
        pending = self.store.pending_news_items()
        relevant = self.screen(pending)
        decisions = self.final(relevant, prices)
        acted = []
        for d in decisions:
            try:
                acted.append(self.act(d, prices, cash, cycle_id))
            except Exception as e:                        # never let a news bug kill the trading loop
                log.exception("news act failed: %s", e)
        summary.update(screened=len(pending), relevant=len(relevant), calls=len(decisions),
                       actions={a["action"]: sum(1 for b in acted if b["action"] == a["action"]) for a in acted})
        if self.llm:
            summary["llm"] = dict(self.llm.usage)
        self.last = dict(fresh=fresh, relevant=relevant, decisions=decisions, acted=acted)
        return summary


# --------------------------------------------------------------------------- standalone entry point
def main():
    ap = argparse.ArgumentParser(description="news sweeper (dry-run by default)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    ap.add_argument("--dry-run", action="store_true", help="default: classify and log, place nothing")
    ap.add_argument("--live", action="store_true", help="place taker orders (also needs news.allow_live=true in config)")
    ap.add_argument("--no-llm", action="store_true", help="skip Claude; show feeds and the pre-filter only")
    ap.add_argument("--hours", type=float, default=None, help="max item age in hours (overrides config)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    cfg = BotConfig.load(args.config)
    if not cfg.api_key:
        raise SystemExit("no SIG API key: set SIG_API_KEY or write it to .sig_api_key next to config.json")
    if args.hours is not None:
        cfg.news.max_item_age_hours = args.hours
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(os.path.join(here, os.path.dirname(cfg.log_path)), exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(here, cfg.log_path))])
    dry = not args.live
    api = SuperMarketClient(cfg.api_key, cfg.base_url)
    t = api.tournament(cfg.tournament_slug)
    api.tournament_id = t["id"]
    cash = float(t.get("myBalance") or 0.0)
    try:
        cash = float(api.account().get("balance") or cash)
    except ApiError:
        pass
    universe = Universe.from_markets(api.markets())
    store = Store(os.path.join(here, cfg.db_path))
    sw = NewsSweeper(api, store, cfg.news, universe, cfg.starting_cash, dry_run=dry, use_llm=not args.no_llm)
    log.info("news sweeper dry_run=%s llm=%s races=%d cash=%.0f", dry, bool(sw.llm), len(universe.races), cash)
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    while not stop["flag"]:
        t0 = _now()
        prices = api.prices(universe.all_exchanges())
        summary = sw.cycle(prices, cash, None, force=True)
        fresh, relevant, decisions, acted = (sw.last.get(k, []) for k in ("fresh", "relevant", "decisions", "acted"))
        print(f"\n== poll {_iso(t0)}  sources={summary.get('sources')} failed={summary.get('failed')} items={summary.get('seen')} "
              f"new={summary.get('new')} fetch={summary.get('fetch_sec')}s")
        for it in fresh[:60]:
            print(f"  + [{it.id}] {it.source[:28]:28s} {(_iso(it.published) if it.published else '----'):20s} {it.title[:110]}")
        if len(fresh) > 60:
            print(f"  ... {len(fresh) - 60} more")
        if sw.llm is None:
            print(f"  (no classification: {'--no-llm' if args.no_llm else 'ANTHROPIC_API_KEY not set'}; {summary.get('screened', 0)} items pending)")
        else:
            print(f"  screened={summary.get('screened')} relevant={len(relevant)} calls={len(decisions)} llm={summary.get('llm')}")
            for r in relevant:
                print(f"  ~ [{r['id']}] {r['race']:28s} {r['title'][:90]}  ({r['note'][:80]})")
            for d, a in zip(decisions, acted):
                flag = "ACT" if a["action"] in ("traded", "dry") else a["action"]
                print(f"  > [{d.item['id']}] {flag:18s} {d.race} {d.party} {d.direction:4s} exp={d.expected:+.1f} conf={d.confidence:.2f} "
                      f"new={d.new_information} mid={a.get('mid_before')} ref={a.get('ref_mid')} moved={a.get('moved_pts') if a.get('moved_pts') is None else round(a['moved_pts'], 1)} "
                      f"| {d.reason[:120]}")
        if args.once:
            break
        sys.stdout.flush()
        time.sleep(max(5.0, cfg.news.poll_sec - (_now() - t0)))


if __name__ == "__main__":
    main()
