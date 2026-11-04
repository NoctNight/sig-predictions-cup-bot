"""Can a news model trained on past election news make positive-EV trades on SIG? (24 Oct 2026)

Stages (cached; every raw response is kept under data/research_news_history/raw and never re-fetched):
  python3 -I research_notes/news_history_study.py fetch     headlines (Wayback RSS snapshots) + outside prices (stdlib)
  python3 -I research_notes/news_history_study.py label     Claude Haiku labels with the bot's FINAL prompt (stdlib)
  <venv>/python research_notes/news_history_study.py analyse   numpy/pandas/sklearn
  <venv>/python research_notes/news_history_study.py plot      matplotlib -> research_notes/figures/news_history.png

Sources
  * Headlines: archived RSS snapshots on the Wayback Machine (CDX + id_ raw replay) of the NYT, NPR, Washington Post,
    CBS politics, Breitbart, Washington Examiner, National Review and Newsweek feeds (and Fox for 2022), up to 4 snapshots per feed per day. Each item keeps its feed pubDate, which is
    the publisher's own time to the second. GDELT DOC refused this IP ("one every 5 seconds") even after 60 s pauses, and
    Google News RSS only dates old items to the day, so neither is used.
  * 2024 prices: Polymarket CLOB prices-history, fidelity 1 (one point a minute), 7-day windows, D leg of each race.
  * 2022 prices: pre-CLOB FPMM markets, rebuilt from FPMMBuy/FPMMSell logs already cached by favourite_drift_extra.py.
    Block times are interpolated between anchors every 10,000 blocks (~6 h), so 2022 is used only at 1 h and longer.
  * SIG (Oct 2026): data/sigbot.sqlite quotes / venue_quotes / news_events, read-only.
"""
from __future__ import annotations

import bisect
import csv
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "research_news_history")
RAW = os.path.join(OUT, "raw")
FIG = os.path.join(HERE, "figures", "news_history.png")
UA = {"User-Agent": "Mozilla/5.0 (research script; contact sizhe.pan)"}

ELECTION = {"2024": "2024-11-05", "2022": "2022-11-08"}
WINDOW = {"2024": ("2024-08-15", "2024-11-05 23:00"), "2022": ("2022-09-01", "2022-11-08 23:00")}
EXTRA_FEEDS = ["feeds.feedburner.com/breitbart", "www.washingtonexaminer.com/feed", "www.nationalreview.com/feed/",
               "www.newsweek.com/rss"]
FEEDS = {"2024": ["rss.nytimes.com/services/xml/rss/nyt/Politics.xml", "feeds.npr.org/1014/rss.xml",
                  "feeds.washingtonpost.com/rss/politics", "www.cbsnews.com/latest/rss/politics"] + EXTRA_FEEDS,
         "2022": ["rss.nytimes.com/services/xml/rss/nyt/Politics.xml", "feeds.npr.org/1014/rss.xml",
                  "feeds.washingtonpost.com/rss/politics", "www.cbsnews.com/latest/rss/politics",
                  "feeds.foxnews.com/foxnews/politics", "moxie.foxnews.com/google-publisher/politics.xml"] + EXTRA_FEEDS}
OUTLET = {"nytimes": "NYT", "npr": "NPR", "washingtonpost": "WaPo", "cbsnews": "CBS", "foxnews": "Fox", "breitbart": "Breitbart",
          "washingtonexaminer": "WashExaminer", "nationalreview": "NatReview", "newsweek": "Newsweek"}

# race key -> (office, label, name patterns). Surnames are matched case-sensitively on word boundaries.
RACES_2024 = {
    "AZ-sen": ("senate", "Arizona Senate", ["Gallego", "Kari Lake"], "arizona-us-senate-election-winner"),
    "PA-sen": ("senate", "Pennsylvania Senate", ["Bob Casey", "Casey", "McCormick"], "pennsylvania-us-senate-election-winner"),
    "NV-sen": ("senate", "Nevada Senate", ["Jacky Rosen", "Rosen", "Sam Brown"], "nevada-us-senate-election-winner"),
    "OH-sen": ("senate", "Ohio Senate", ["Sherrod Brown", "Bernie Moreno", "Moreno"], "ohio-us-senate-election-winner"),
    "NE-sen": ("senate", "Nebraska Senate", ["Dan Osborn", "Osborn", "Deb Fischer"], "nebraska-us-senate-election-winner"),
    "MT-sen": ("senate", "Montana Senate", ["Tester", "Sheehy"], "montana-us-senate-election-winner"),
    "MI-sen": ("senate", "Michigan Senate", ["Slotkin", "Mike Rogers"], "michigan-us-senate-election-winner"),
    "TX-sen": ("senate", "Texas Senate", ["Allred", "Ted Cruz"], "texas-us-senate-election-winner"),
    "WI-sen": ("senate", "Wisconsin Senate", ["Tammy Baldwin", "Baldwin", "Hovde"], "wisconsin-us-senate-election-winner"),
    "FL-sen": ("senate", "Florida Senate", ["Mucarsel-Powell", "Rick Scott"], "florida-us-senate-election-winner"),
    "MD-sen": ("senate", "Maryland Senate", ["Alsobrooks", "Larry Hogan", "Hogan"], "maryland-us-senate-election-winner"),
    "NC-gov": ("governor", "North Carolina Governor", ["Josh Stein", "Mark Robinson"], "north-carolina-governor-election-winner"),
    "NH-gov": ("governor", "New Hampshire Governor", ["Ayotte", "Joyce Craig"], "new-hampshire-governor-election-winner"),
    "CA-13": ("house", "CA-13 House", ["Duarte", "Adam Gray"], "ca-13-election-gray-d-vs-duarte-r"),
    "NJ-07": ("house", "NJ-07 House", ["Altman", "Tom Kean"], "nj-07-election-altman-d-vs-kean-r"),
    "CA-45": ("house", "CA-45 House", ["Michelle Steel", "Derek Tran"], "ca-45-election-tran-d-vs-steel-r"),
    "CA-27": ("house", "CA-27 House", ["Whitesides", "Mike Garcia"], "ca-27-election-whitesides-d-vs-garcia-r"),
    "WA-03": ("house", "WA-03 House", ["Gluesenkamp Perez", "Joe Kent"], "wa-03-house-race-perez-d-vs-kent-r"),
    "AK-01": ("house", "Alaska House", ["Peltola", "Begich"], "ak-1-election-peltola-d-vs-begich-r"),
    "CA-41": ("house", "CA-41 House", ["Calvert", "Will Rollins"], "ca-13-election-rollins-d-vs-calvert-r"),
    "CA-22": ("house", "CA-22 House", ["Valadao", "Rudy Salas"], "ca-22-election-salas-d-vs-valadao-r"),
    "AZ-06": ("house", "AZ-06 House", ["Ciscomani", "Kirsten Engel"], "az-06-election-engel-d-vs-ciscomani-r"),
    "IA-01": ("house", "IA-01 House", ["Miller-Meeks", "Bohannan"], "ia-01-election-bohannan-d-vs-miller-meeks-r"),
    "CO-08": ("house", "CO-08 House", ["Caraveo", "Gabe Evans"], "co-8-election-caraveo-d-vs-evans-r"),
    "sen-control": ("control", "Senate control", ["control of the Senate", "Senate majority", "Senate control"], "which-party-will-control-the-us-senate-after-the-2024-election"),
    "house-control": ("control", "House control", ["control of the House", "House majority", "House control"], "house-control-after-2024-election"),
}
STATE_ABBR = {"Georgia": "GA", "Arizona": "AZ", "Pennsylvania": "PA", "Nevada": "NV", "Ohio": "OH", "New Hampshire": "NH",
              "Colorado": "CO", "North Carolina": "NC", "Wisconsin": "WI", "New York": "NY", "Oregon": "OR", "Kansas": "KS",
              "Michigan": "MI", "Iowa": "IA", "Florida": "FL", "Washington": "WA", "Utah": "UT", "New Mexico": "NM"}
MIN_VOL_2022 = 10000.0

TYPES = [  # first match wins
    ("results", r"\b(projected|projects|AP calls|calls the race|race call|concedes?|conceded|declared (the )?winner|wins (re-?election|the race|seat)|defeats|victory speech)\b"),
    ("dropout", r"(drops? out|dropping out|withdraws?|suspends? (his|her) campaign|ends? (his|her) (campaign|bid)|exits? the race|quits the race)"),
    ("scandal", r"\b(scandal|indict\w*|charged|allegation\w*|accus\w*|investigation|porn|lied|misconduct|abuse|harass\w*|controvers\w*|nude|racist)\b"),
    ("court", r"\b(court|judge|ruling|ruled|lawsuit|sues|sued|appeals)\b"),
    ("debate", r"\bdebat\w*"),
    ("endorsement", r"\bendors\w*"),
    ("poll", r"\b(poll|polls|polling|survey|leads|trails|lead over|neck and neck|dead heat|toss-?up)\b"),
    ("fundraising", r"\b(raised?|raising|fundrais\w*|ad buy|ads?|spending|spends?|super PAC|million)\b"),
]
HORIZONS = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400}


# --------------------------------------------------------------------------- helpers
def ts_of(s: str) -> int:
    fmt = "%Y-%m-%d %H:%M" if " " in s else "%Y-%m-%d"
    return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())


_CTX = None


def _ctx():
    """The bot's SSL context (this machine's proxy needs it); read-only import."""
    global _CTX
    if _CTX is None:
        sys.path.insert(0, ROOT)
        from sigbot.api import _ssl_context
        _CTX = _ssl_context()
    return _CTX


def get(url: str, path: str, sleep: float = 1.0, tries: int = 4):
    """GET with on-disk cache. Returns bytes or None."""
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60, context=_ctx()) as r:
                body = r.read()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(body)
            time.sleep(sleep)
            return body
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            if code in (404, 400):
                return None
            print(f"    retry {k + 1} {url[:90]}: {e!r}"[:200], flush=True)
            time.sleep(5 * (k + 1))
    return None


def jload(p):
    with open(p) as f:
        return json.load(f)


def jsave(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(obj, f)


# --------------------------------------------------------------------------- headlines
def parse_rss(body: bytes):
    s = body.decode("utf-8", "replace")
    out = []
    for it in re.findall(r"<item\b.*?</item>", s, re.S):
        def tag(t):
            m = re.search(rf"<{t}\b[^>]*>(.*?)</{t}>", it, re.S)
            if not m:
                return ""
            v = m.group(1).strip()
            v = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v, flags=re.S)
            v = re.sub(r"<[^>]+>", " ", v)
            return re.sub(r"\s+", " ", v.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
                          .replace("&#8217;", "'").replace("&#x27;", "'").replace("&lt;", "<").replace("&gt;", ">")).strip()
        title, link, pub = tag("title"), tag("link"), tag("pubDate")
        if not title or not pub:
            continue
        try:
            t = int(parsedate_to_datetime(pub).timestamp())
        except Exception:  # noqa: BLE001
            continue
        out.append({"title": title, "url": link, "ts": t, "desc": tag("description")[:600]})
    return out


def fetch_news(cycle: str):
    a, b = WINDOW[cycle]
    items = {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(len(FEEDS[cycle])) as pool:          # one polite sequential stream per feed
        for got in pool.map(lambda f: _fetch_feed(cycle, f, a, b), FEEDS[cycle]):
            for k, x in got.items():
                if k not in items or x["ts"] < items[k]["ts"]:
                    items[k] = x
    rows = [v for v in items.values() if ts_of(a) <= v["ts"] <= ts_of(b)]
    jsave(os.path.join(OUT, f"headlines_{cycle}.json"), rows)
    print(f"  {cycle}: {len(rows)} unique headlines in window", flush=True)


def _fetch_feed(cycle, feed, a, b):
    items = {}
    if True:
        fkey = re.sub(r"[^a-z0-9]+", "_", feed.lower())[:60]
        q = urllib.parse.urlencode({"url": feed, "from": a.replace("-", "")[:8], "to": b.replace("-", "")[:8],
                                    "output": "json", "fl": "timestamp", "filter": "statuscode:200"})
        body = get(f"https://web.archive.org/cdx/search/cdx?{q}", os.path.join(RAW, "cdx", f"{cycle}_{fkey}.json"), 1.5)
        snaps = [r[0] for r in (json.loads(body)[1:] if body else [])]
        # up to 4 a day: first snapshot in each 6-hour block
        keep, seen = [], set()
        for s in sorted(snaps):
            blk = (s[:8], int(s[8:10]) // 6)
            if blk not in seen:
                seen.add(blk)
                keep.append(s)
        outlet = next(v for k, v in OUTLET.items() if k in feed)
        n_ok = 0
        for s in keep:
            body = get(f"https://web.archive.org/web/{s}id_/{feed}", os.path.join(RAW, "rss", cycle, fkey, f"{s}.xml"), 1.2)
            if not body:
                continue
            n_ok += 1
            for x in parse_rss(body):
                k = (x["title"].lower(), outlet)
                if k not in items or x["ts"] < items[k]["ts"]:
                    items[k] = {**x, "outlet": outlet, "cycle": cycle}
        print(f"  {cycle} {outlet:5s} {feed[:50]:50s} snapshots {len(snaps)} used {n_ok}", flush=True)
    return items


# --------------------------------------------------------------------------- outside prices
def races(cycle: str) -> dict:
    """race key -> {office, label, pats, series: [[t, p_D], ...]} (series loaded separately)."""
    if cycle == "2024":
        return {k: {"office": o, "label": lab, "pats": pats, "slug": slug} for k, (o, lab, pats, slug) in RACES_2024.items()}
    out = {}
    for eid, office in ((5808, "senate"), (5863, "governor")):
        ev = jload(os.path.join(ROOT, "data", "polymarket_history", "raw", "events", f"{eid}.json"))
        ev = ev[0] if isinstance(ev, list) else ev
        for m in ev["markets"]:
            if float(m.get("volumeNum") or 0) < MIN_VOL_2022:
                continue
            g = m.get("groupItemTitle") or ""
            state, _, names = g.partition(" - ")
            cands = [n.strip() for n in names.split(" vs. ")]
            pats = cands + [c.split()[-1] for c in cands if len(c.split()[-1]) > 4]
            key = f"{STATE_ABBR.get(state, state[:2].upper())}-{office[:3]}"
            out[key] = {"office": office, "label": f"{state} {office.title()}", "pats": pats, "market": m,
                        "outcomes": json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]}
    return out


def fetch_prices_2024():
    legs = {r["slug"]: r for r in csv.DictReader(open(os.path.join(ROOT, "data", "polymarket_history", "legs.csv")))
            if r["cycle"] == "2024" and r["party"] == "D"}
    a, b = WINDOW["2024"]
    for k, r in races("2024").items():
        if r["slug"] in legs:
            tok = legs[r["slug"]]["token"]
        else:  # control markets: Gamma event lookup
            body = get(f"https://gamma-api.polymarket.com/events?slug={r['slug']}", os.path.join(RAW, "gamma", f"{r['slug']}.json"), 0.5)
            ev = json.loads(body)[0]
            m = ev["markets"][0]                                   # one market, outcomes [Democrats, Republicans]
            tok = json.loads(m["clobTokenIds"])[0]
        series = {}
        for t0 in range(ts_of(a), ts_of(b), 7 * 86400):
            t1 = min(t0 + 7 * 86400, ts_of(b) + 86400)
            body = get(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={t0}&endTs={t1}&fidelity=1",
                       os.path.join(RAW, "clob", k, f"{t0}.json"), 0.25)
            for x in (json.loads(body).get("history", []) if body else []):
                series[int(x["t"])] = float(x["p"])
        jsave(os.path.join(OUT, "prices", f"2024_{k}.json"), sorted(series.items()))
        print(f"  2024 {k:13s} token ..{tok[-6:]} points {len(series)}", flush=True)


def fetch_prices_2022():
    sys.path.insert(0, HERE)
    import favourite_drift_extra as E  # noqa: E402  (reads its cache only)
    rr = races("2022")
    by_addr = {r["market"]["marketMakerAddress"].lower(): k for k, r in rr.items()}
    addrs_all = None
    a_block, b_block, anchors = E._anchors(False)
    ab, at = [n for n, _ in anchors], [t for _, t in anchors]

    def t_of(n):
        i = min(max(bisect.bisect_right(ab, n) - 1, 0), len(anchors) - 2)
        (n0, t0), (n1, t1) = anchors[i], anchors[i + 1]
        return int(t0 + (n - n0) * (t1 - t0) / (n1 - n0))

    # the cached chunks were fetched for all A markets together; reuse that address set
    evs = [E._event(e["event"], False, E.RAW_EVENTS) for e in E.A_ELECTIONS]
    addrs_all = sorted({m["marketMakerAddress"] for _, m, _ in E._fpmm_markets(evs)})
    series = {k: {} for k in rr}
    for lo in range(a_block, b_block + 1, E.RPC_CHUNK):
        logs = E._logs(addrs_all, lo, min(lo + E.RPC_CHUNK - 1, b_block), False) or []
        for lg in logs:
            k = by_addr.get(lg["address"].lower())
            tp = lg.get("topics", [])
            if k is None or len(tp) != 3 or tp[0] not in (E.TOPIC_BUY, E.TOPIC_SELL):
                continue
            d = bytes.fromhex(lg["data"][2:])
            w = [int.from_bytes(d[32 * j:32 * j + 32], "big") for j in range(3)]
            if w[2] <= 0:
                continue
            p = (w[0] - w[1]) / w[2] if tp[0] == E.TOPIC_BUY else (w[0] + w[1]) / w[2]
            if not 0 < p < 1:
                continue
            idx = int(tp[2], 16)
            d_idx = 0 if rr[k]["outcomes"][0] in ("Democrat", "Independent") else 1
            series[k][t_of(int(lg["blockNumber"], 16))] = p if idx == d_idx else 1 - p
    for k, s in series.items():
        jsave(os.path.join(OUT, "prices", f"2022_{k}.json"), sorted(s.items()))
        print(f"  2022 {k:8s} trades {len(s)}", flush=True)


def fetch():
    fetch_prices_2024()
    fetch_prices_2022()
    for c in ("2024", "2022"):
        fetch_news(c)


# --------------------------------------------------------------------------- matching + moves
def story_type(text: str) -> str:
    for name, pat in TYPES:
        if re.search(pat, text, re.I):
            return name
    return "other"


def load_series(cycle, k):
    p = os.path.join(OUT, "prices", f"{cycle}_{k}.json")
    s = jload(p) if os.path.exists(p) else []
    return [int(t) for t, _ in s], [float(v) for _, v in s]


def price_at(T, P, t, max_age):
    i = bisect.bisect_right(T, t) - 1
    if i < 0 or t - T[i] > max_age:
        return None
    return P[i]


def build_events():
    """One row per (headline, matched race), with outside moves. Writes events.json and baseline.json."""
    rows, base = [], []
    rng = random.Random(7)
    for cycle in ("2024", "2022"):
        rr = races(cycle)
        heads = jload(os.path.join(OUT, f"headlines_{cycle}.json"))
        el = ts_of(ELECTION[cycle]) + 23 * 3600            # ~ polls closing
        max_age = 900 if cycle == "2024" else 6 * 3600     # 2024: minute data; 2022: last FPMM trade within 6 h
        ser = {k: load_series(cycle, k) for k in rr}
        pats = {k: [re.compile(r"(?<![A-Za-z])" + re.escape(p) + r"(?![A-Za-z])", 0 if r["office"] != "control" else re.I)
                    for p in r["pats"]] for k, r in rr.items()}
        for h in heads:
            text = h["title"] + " || " + h.get("desc", "")
            hit = [k for k in rr if any(p.search(h["title"]) or p.search(h.get("desc", "")) for p in pats[k])]
            if not hit or len(hit) > 3:
                continue
            for k in hit:
                T, P = ser[k]
                if not T:
                    continue
                t0 = h["ts"]
                p0 = price_at(T, P, t0, max_age)
                if p0 is None or t0 > el:
                    continue
                row = {"cycle": cycle, "race": k, "label": rr[k]["label"], "office": rr[k]["office"], "ts": t0,
                       "outlet": h["outlet"], "title": h["title"], "desc": h.get("desc", "")[:400],
                       "type": story_type(text), "p0": p0, "days_to_el": (el - t0) / 86400}
                for hn, sec in HORIZONS.items():
                    p1 = price_at(T, P, min(t0 + sec, el), max_age if cycle == "2024" else 2 * 86400)
                    row[f"d_{hn}"] = None if p1 is None else 100 * (p1 - p0)
                pm = price_at(T, P, t0 - 3600, max_age if cycle == "2024" else 2 * 86400)
                row["pre_1h"] = None if pm is None else 100 * (p0 - pm)
                rows.append(row)
        # baseline: random times per race inside the window
        a, b = WINDOW[cycle]
        for k in rr:
            T, P = ser[k]
            if not T:
                continue
            for _ in range(600):
                t0 = rng.randint(max(ts_of(a), T[0]), min(el - 3600, T[-1]))
                p0 = price_at(T, P, t0, max_age)
                if p0 is None:
                    continue
                r = {"cycle": cycle, "race": k, "ts": t0, "p0": p0}
                for hn, sec in HORIZONS.items():
                    p1 = price_at(T, P, min(t0 + sec, el), max_age if cycle == "2024" else 2 * 86400)
                    r[f"d_{hn}"] = None if p1 is None else 100 * (p1 - p0)
                base.append(r)
    jsave(os.path.join(OUT, "events.json"), rows)
    jsave(os.path.join(OUT, "baseline.json"), base)
    print(f"events {len(rows)} (2024 {sum(r['cycle'] == '2024' for r in rows)}, 2022 {sum(r['cycle'] == '2022' for r in rows)}); baseline {len(base)}")
    return rows


# --------------------------------------------------------------------------- LLM labels (bot's FINAL prompt, Haiku)
LABEL_MODEL = "claude-haiku-5-5"
BATCH = 20
MAX_LABELS = 2600


def _client():
    sys.path.insert(0, ROOT)
    from sigbot import news as N  # read-only import of the bot's prompt, schema and client
    key = os.environ.get("ANTHROPIC_API_KEY") or N._key_file()
    if not key:
        raise SystemExit("no Anthropic key")
    return N, N.ClaudeClient(key)


def _h(items):
    """Cache key from the batch's content, so adding headlines never reuses another batch's answers."""
    import hashlib
    return hashlib.sha1("|".join(f"{x['label']}~{x['title']}~{x['published']}" for x in items).encode()).hexdigest()[:12]


def _label_batch(N, cl, items, election_day, today, cache_path):
    if os.path.exists(cache_path):
        return jload(cache_path)
    lines = []
    for i, it in enumerate(items):
        lines.append(f"[{i}] race: {it['label']} | current venue prices: {it['prices']}\n"
                     f"    source: {it['outlet']} | published: {it['published']}\n    headline: {it['title']}\n"
                     f"    lead: {it['desc'][:600]}\n    screener note: ")
    user = "Items:\n" + "\n".join(lines) + "\n\nReturn one call per item id (every id must appear)."
    system = N.FINAL_SYSTEM.format(election_day=election_day, today=today)
    out = cl.json_call(LABEL_MODEL, system, user, N.FINAL_SCHEMA, max_tokens=6000, effort="low")
    if out is None:
        return None
    jsave(cache_path, out)
    return out


def label():
    N, cl = _client()
    ev = build_events()
    # historical sample: dedupe (title, race), stratify by cycle; keep every non-'other' item first
    seen, pool = set(), []
    for r in ev:
        k = (r["title"].lower(), r["race"])
        if k not in seen:
            seen.add(k)
            pool.append(r)
    rng = random.Random(11)
    rng.shuffle(pool)
    pool.sort(key=lambda r: r["type"] == "other")
    live = live_headlines()
    hist = pool[: max(0, MAX_LABELS - len(live))]
    print(f"labelling {len(hist)} historical + {len(live)} live headlines with {LABEL_MODEL}")
    labels = {}
    groups = {}
    for r in hist:
        groups.setdefault(("hist", r["cycle"], datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d")), []).append(r)
    for r in live:
        groups.setdefault(("live", "2026", datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d")), []).append(r)
    for (kind, cycle, day), rs in sorted(groups.items()):
        rs.sort(key=lambda r: r["ts"])
        for j in range(0, len(rs), BATCH):
            b = rs[j:j + BATCH]
            items = []
            for r in b:
                if kind == "hist":
                    pD = 100 * r["p0"]
                    prices = f"D {pD:.1f}%; R {100 - pD:.1f}%"
                else:
                    prices = r["prices"]
                items.append({**r, "prices": prices,
                              "published": datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})
            today = datetime.fromtimestamp(b[-1]["ts"] + 600, timezone.utc).strftime("%Y-%m-%d %H:%M")
            eday = {"2024": "2024-11-05", "2022": "2022-11-08", "2026": "2026-11-03"}[cycle]
            out = _label_batch(N, cl, items, eday, today, os.path.join(RAW, "labels", f"{kind}_{cycle}_{day}_{_h(items)}.json"))
            calls = {x["id"]: x for x in (out or {}).get("calls", []) if isinstance(x, dict) and "id" in x}
            for i, r in enumerate(b):
                if i in calls:
                    labels[r["uid"] if kind == "live" else f"{r['cycle']}|{r['race']}|{r['ts']}|{r['title'][:80]}"] = calls[i]
        print(f"  {kind} {cycle} {day}: {len(rs)} items, labels so far {len(labels)}, usage {cl.usage}", flush=True)
    jsave(os.path.join(OUT, "labels.json"), labels)
    print("usage", cl.usage)


# --------------------------------------------------------------------------- SIG live (Oct 2026)
DB = os.path.join(ROOT, "data", "sigbot.sqlite")


def _db():
    import sqlite3
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def live_headlines():
    """The bot's own news_events (one per decision), with the SIG prices the bot saw."""
    con = _db()
    cur = con.execute("select id, ts, race, party, exchange_id, direction, expected_move_pts, confidence, headline, source, "
                      "published, bid_before, ask_before, mid_before, model, response from news_events order by ts")
    rows = []
    for (i, ts, race, party, ex, d, e, c, h, src, pub, bb, ba, mb, model, resp) in cur.fetchall():
        rows.append({"uid": f"live|{i}", "id": i, "ts": ts, "race": race, "label": race, "party": party, "exchange_id": ex,
                     "bot_dir": d, "bot_exp": e, "bot_conf": c, "title": h or "", "desc": "", "outlet": src or "",
                     "published_raw": pub, "prices": f"{party} {100 * mb:.1f}% (bid {bb:.3f} / ask {ba:.3f})" if mb else "unknown",
                     "model": model})
    return rows


# --------------------------------------------------------------------------- analysis (needs numpy, pandas, sklearn)
SPLIT = {"2024": "2024-10-10", "2022": "2022-10-10"}   # test = the last ~4 weeks of each campaign
SIG_SPLIT = "2026-10-23"                               # SIG: train 21-22 Oct, test 23 Oct on
PM_COST = 1.0      # Polymarket round trip in points (1c spread on these markets; 0.5 c would halve it)
TICK = 0.5         # SIG tick in points


def _cluster_t(y, groups):
    """Mean, SE and t of y clustered by group."""
    import numpy as np
    y, g = np.asarray(y, float), np.asarray(groups)
    ok = ~np.isnan(y)
    y, g = y[ok], g[ok]
    if len(y) < 3:
        return float("nan"), float("nan"), float("nan"), len(y)
    m = y.mean()
    u = {}
    for yi, gi in zip(y - m, g):
        u[gi] = u.get(gi, 0.0) + yi
    G = len(u)
    se = (sum(v * v for v in u.values()) ** 0.5) / len(y) * (G / max(G - 1, 1)) ** 0.5
    return float(m), float(se), float(m / se) if se > 0 else float("nan"), len(y)


def _frames():
    import numpy as np
    import pandas as pd
    ev = pd.DataFrame(jload(os.path.join(OUT, "events.json")))
    base = pd.DataFrame(jload(os.path.join(OUT, "baseline.json")))
    ev["day"] = pd.to_datetime(ev["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    ev["test"] = [d >= SPLIT[c] for d, c in zip(ev["day"], ev["cycle"])]
    ev = ev.drop_duplicates(["title", "race"]).reset_index(drop=True)
    for h in HORIZONS:
        ev[f"a_{h}"] = ev[f"d_{h}"].abs()
        base[f"a_{h}"] = base[f"d_{h}"].abs()
        bm = base.groupby(["cycle", "race"])[f"a_{h}"].mean().rename(f"b_{h}")
        ev = ev.join(bm, on=["cycle", "race"])
        ev[f"x_{h}"] = ev[f"a_{h}"] - ev[f"b_{h}"]          # excess absolute move over the race's random-time average
    ev["close"] = (ev["p0"] - 0.5).abs() < 0.2
    lab = jload(os.path.join(OUT, "labels.json")) if os.path.exists(os.path.join(OUT, "labels.json")) else {}
    keys = [f"{c}|{r}|{t}|{ti[:80]}" for c, r, t, ti in zip(ev["cycle"], ev["race"], ev["ts"], ev["title"])]
    L = [lab.get(k) for k in keys]
    ev["labelled"] = [x is not None for x in L]
    ev["party"] = [x.get("party") if x else None for x in L]
    ev["dir"] = [x.get("direction") if x else None for x in L]
    ev["exp"] = [float(x.get("expected_move_pts") or 0) if x else np.nan for x in L]
    ev["conf"] = [float(x.get("confidence") or 0) if x else np.nan for x in L]
    ev["newinfo"] = [bool(x.get("new_information")) if x else False for x in L]
    # sign of the call on the D-leg series (I counts as the D slot in 2022 Utah; control: D leg)
    ev["sgn"] = [np.nan if d is None else (1 if d == "up" else -1) * (-1 if p == "R" else 1) for d, p in zip(ev["dir"], ev["party"])]
    ev["raw"] = ev["exp"] * ev["conf"]
    for h in HORIZONS:
        ev[f"r_{h}"] = ev["sgn"] * ev[f"d_{h}"]               # realised move in the called direction, points
    return ev, base, lab


def _sig_panel(lab):
    """Live SIG news events. Entry = first SIG quote at or after the decision (within 15 min); SIG mid at entry + h from
    the quote nearest that time (within 10 min). SIG quotes merge the bot's quotes table and venue_quotes (venue sig).
    Outside move = average over Polymarket/Kalshi of each venue's own change (only venues quoted at both times), so a
    venue dropping in or out cannot fake a move. Gap = outside fair - SIG mid, fair needs both venues within 4 points
    or one venue alone."""
    import numpy as np
    import pandas as pd
    con = _db()
    q = pd.read_sql_query("select ts, exchange_id as ex, best_bid as bid, best_ask as ask from quotes where best_bid > 0 and best_ask > 0", con)
    vq = pd.read_sql_query("select ts, venue, race, party, instrument, bid, ask, mid from venue_quotes where mid is not null", con)
    sq = vq[(vq.venue == "sig") & (vq.bid > 0) & (vq.ask > 0)].rename(columns={"instrument": "ex"})[["ts", "ex", "bid", "ask"]]
    q = pd.concat([q, sq]).sort_values("ts")
    q["mid"] = (q.bid + q.ask) / 2
    qs = {ex: (g["ts"].values, g["mid"].values, (g["ask"] - g["bid"]).values) for ex, g in q.groupby("ex")}
    vs = {}
    for (v, r, p), g in vq[vq.venue != "sig"].sort_values("ts").groupby(["venue", "race", "party"]):
        vs[(v, r, p)] = (g["ts"].values, g["mid"].values)
    live = pd.DataFrame(live_headlines())

    def near(T, t, tol):
        i = np.searchsorted(T, t)
        c = [k for k in (i - 1, i) if 0 <= k < len(T) and abs(T[k] - t) <= tol]
        return min(c, key=lambda k: abs(T[k] - t)) if c else None

    def after(T, t, tol):
        i = np.searchsorted(T, t)
        return i if i < len(T) and T[i] - t <= tol else None

    def vmid(r, p, t):
        out = {}
        for v in ("polymarket", "kalshi"):
            if (v, r, p) in vs:
                T, V = vs[(v, r, p)]
                k = near(T, t, 600)
                if k is not None:
                    out[v] = V[k]
        return out

    def fair_of(m):
        if len(m) == 2 and abs(m["polymarket"] - m["kalshi"]) > 0.04:
            return np.nan
        return float(np.mean(list(m.values()))) if m else np.nan

    def leg(ex, r, p, t0, with_title=None):
        T, M, S = qs[ex]
        k = after(T, t0, 900)
        if k is None:
            return None
        te, m0, sp0 = T[k], M[k], S[k]
        v0 = vmid(r, p, te)
        row = {"te": te, "m0": m0, "spread": sp0, "f0": fair_of(v0), "gap": 100 * (fair_of(v0) - m0)}
        for h, sec in HORIZONS.items():
            k1 = near(T, te + sec, 600)
            row[f"s_{h}"] = 100 * (M[k1] - m0) if k1 is not None else np.nan
            v1 = vmid(r, p, te + sec)
            ch = [v1[v] - v0[v] for v in v0 if v in v1]
            row[f"o_{h}"] = 100 * float(np.mean(ch)) if ch else np.nan
        return row

    rows = []
    for _, e in live.iterrows():
        if e["exchange_id"] not in qs:
            continue
        base = leg(e["exchange_id"], e["race"], e["party"], e["ts"])
        if base is None:
            continue
        x = lab.get(e["uid"]) or {}
        rows.append({**base, "uid": e["uid"], "ts": e["ts"], "race": e["race"], "party": e["party"], "ex": e["exchange_id"],
                     "title": e["title"], "bot_sgn": 1 if e["bot_dir"] == "up" else -1,
                     "bot_raw": (e["bot_exp"] or 0) * (e["bot_conf"] or 0),
                     "h_sgn": np.nan if not x else (1 if x.get("direction") == "up" else -1) * (1 if x.get("party", e["party"]) == e["party"] else -1),
                     "h_raw": np.nan if not x else float(x.get("expected_move_pts") or 0) * float(x.get("confidence") or 0),
                     "h_new": bool(x.get("new_information")) if x else False})
    sp = pd.DataFrame(rows)
    sp["day"] = pd.to_datetime(sp["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    sp["test"] = sp["day"] >= SIG_SPLIT
    # catch-up baseline: every leg the bot made a news call on, every 10 minutes (no news conditioning)
    grid = []
    legs = live.drop_duplicates("exchange_id")
    for _, e in legs.iterrows():
        if e["exchange_id"] not in qs:
            continue
        T = qs[e["exchange_id"]][0]
        for t0 in np.arange(T[0], T[-1], 600.0):
            r = leg(e["exchange_id"], e["race"], e["party"], t0)
            if r is not None and r["gap"] == r["gap"]:
                grid.append({"ts": t0, **{k: r[k] for k in ["gap"] + [f"s_{h}" for h in HORIZONS]}})
    grid = pd.DataFrame(grid)
    grid["test"] = pd.to_datetime(grid["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d") >= SIG_SPLIT
    return sp, grid


CAL_H = "6h"                                   # calibration / trade horizon (2022 prices are too sparse for 1 h)
RAW_EDGES = [-0.01, 0.05, 0.15, 0.3, 0.6, 100]   # bins of expected_move x confidence (points)


def calibrator(raw_tr, y_tr):
    """Binned calibration: predicted move = training-day mean realised move in the call direction, per score bin."""
    import numpy as np
    raw_tr, y_tr = np.asarray(raw_tr, float), np.asarray(y_tr, float)
    means = []
    for lo, hi in zip(RAW_EDGES[:-1], RAW_EDGES[1:]):
        sel = (raw_tr > lo) & (raw_tr <= hi) & ~np.isnan(y_tr)
        means.append(float(y_tr[sel].mean()) if sel.sum() >= 5 else float(np.nanmean(y_tr)))
    return lambda x: np.array([means[min(max(np.searchsorted(RAW_EDGES, v, side="left") - 1, 0), len(means) - 1)]
                               for v in np.nan_to_num(np.asarray(x, float))]), means


def analyse():
    import numpy as np
    import pandas as pd
    from sklearn.isotonic import IsotonicRegression
    from scipy import stats
    ev, base, lab = _frames()
    res, md = {}, []
    P = md.append
    P(f"events (headline x race, deduped): {len(ev)}; 2024 {int((ev.cycle == '2024').sum())}, 2022 {int((ev.cycle == '2022').sum())}; "
      f"labelled {int(ev.labelled.sum())}")

    # ---- (2) what moves outside prices: excess absolute move over each race's random-time average
    P("\n## 2. Excess absolute outside move after a headline (points; >0 = bigger than a random moment in that race)\n")
    P("| type | n train | 1h train (t) | n test | 15m test (t) | 1h test (t) | 6h test (t) | 24h test (t) | pre-1h test |")
    P("|---|---|---|---|---|---|---|---|---|")
    out = {}
    for ty in ["all"] + [t for t, _ in TYPES] + ["other"]:
        d = ev if ty == "all" else ev[ev["type"] == ty]
        tr, te = d[~d.test], d[d.test]
        cells = {}
        for nm, dd in (("train", tr), ("test", te)):
            for h in HORIZONS:
                src = dd if (h != "15m") else dd[dd.cycle == "2024"]
                cells[(nm, h)] = _cluster_t(src[f"x_{h}"], src["day"])
        pre = _cluster_t(te["pre_1h"].abs() - te["b_1h"], te["day"])
        out[ty] = {f"{k[0]}_{k[1]}": v for k, v in cells.items()}
        f = lambda c: f"{c[0]:+.2f} ({c[2]:+.1f})" if c[3] >= 3 else "-"
        P(f"| {ty} | {len(tr)} | {f(cells[('train', '1h')])} | {len(te)} | {f(cells[('test', '15m')])} | {f(cells[('test', '1h')])} | "
          f"{f(cells[('test', '6h')])} | {f(cells[('test', '24h')])} | {f(pre)} |")
    res["type_effects"] = out
    # regression of excess |move 1h| on features, fit on train, scored on test
    X = pd.get_dummies(ev[["type", "outlet", "office"]].astype(str), drop_first=True).astype(float)
    X["close"] = ev["close"].astype(float)
    X["days_to_el"] = ev["days_to_el"]
    X["c2022"] = (ev["cycle"] == "2022").astype(float)
    X.insert(0, "const", 1.0)
    for h in ("1h", "6h"):
        y = ev[f"x_{h}"]
        ok = y.notna()
        tr, te = ok & ~ev.test, ok & ev.test
        beta, *_ = np.linalg.lstsq(X[tr].values, y[tr].values, rcond=None)
        pred = X[te].values @ beta
        r2 = 1 - ((y[te] - pred) ** 2).sum() / ((y[te] - y[tr].mean()) ** 2).sum()
        rho = stats.spearmanr(pred, y[te]).correlation
        res[f"feature_model_{h}"] = {"oos_r2": float(r2), "oos_spearman": float(rho), "n_test": int(te.sum()),
                                      "coef": dict(zip(X.columns, map(float, beta)))}
        P(f"\nFeature model for excess |move| {h}: out-of-sample R^2 {r2:+.3f}, Spearman {rho:+.3f} (n test {int(te.sum())})")
        P("coefficients (train): " + ", ".join(f"{k} {v:+.2f}" for k, v in zip(X.columns, beta)))

    # ---- (3) calibrate the bot's prompt (Haiku) on the realised outside move in the called direction
    L = ev[ev.labelled & ev["sgn"].notna()].copy()
    P(f"\n## 3. Bot prompt (Haiku) calls vs realised outside move ({len(L)} labelled; new_information true {int(L.newinfo.sum())})\n")
    P(f"confidence: median {L.conf.median():.2f}, share >= 0.5 {np.mean(L.conf >= 0.5):.1%}, >= 0.8 {np.mean(L.conf >= 0.8):.1%}; "
      f"expected move median {L.exp.median():.1f} pts, share >= 3 {np.mean(L.exp >= 3):.1%}")
    cal = {}
    for h in ("15m", "1h", "6h", "24h"):
        y = L[f"r_{h}"]
        ok = y.notna() & ((L.cycle == "2024") | (h != "15m"))
        tr, te = ok & ~L.test, ok & L.test
        iso = IsotonicRegression(out_of_bounds="clip").fit(L.raw[tr], y[tr])
        L[f"cal_{h}"] = iso.predict(L.raw)
        rho_tr = stats.spearmanr(L.raw[tr], y[tr]).correlation
        rho_te = stats.spearmanr(L.raw[te], y[te]).correlation
        hit = np.mean(y[te] > 0) if te.sum() else np.nan
        nz = te & (y != 0)
        hit_nz = np.mean(y[nz] > 0) if nz.sum() else np.nan
        # Polymarket trade rule: act when calibrated prediction > threshold chosen on train
        best = None
        for th in [0, 0.25, 0.5, 0.75, 1, 1.5, 2, 3]:
            sel = tr & (L[f"cal_{h}"] > th)
            if sel.sum() >= 10:
                v = (y[sel] - PM_COST).mean()
                if best is None or v > best[1]:
                    best = (th, v, int(sel.sum()))
        sel_te = te & (L[f"cal_{h}"] > (best[0] if best else 0))
        pnl = _cluster_t(y[sel_te] - PM_COST, L.day[sel_te])
        g = _cluster_t(y[te], L.day[te])
        cal[h] = {"spearman_train": float(rho_tr), "spearman_test": float(rho_te), "mean_signed_test": g, "hit_test": float(hit),
                  "hit_test_nonzero": float(hit_nz), "threshold_train": best, "pm_pnl_test": pnl,
                  "iso_x": list(map(float, iso.X_thresholds_)), "iso_y": list(map(float, iso.y_thresholds_))}
        P(f"{h}: Spearman(raw, realised) train {rho_tr:+.3f} / test {rho_te:+.3f}; test mean realised move in called direction "
          f"{g[0]:+.2f} pts (t {g[2]:+.1f}, n {g[3]}); hit {hit:.1%} (non-zero moves {hit_nz:.1%}); "
          f"PM rule cal>{best[0] if best else '-'} (train {best[1]:+.2f} pts, n {best[2]}) -> test net {pnl[0]:+.2f} pts (t {pnl[2]:+.1f}, n {pnl[3]})"
          if best else f"{h}: no threshold with n>=10")
    res["calibration"] = cal
    # by confidence bucket, test, 1h and 6h
    P("\n| confidence (test) | n | 1h move in call direction | 6h | 24h |")
    P("|---|---|---|---|---|")
    for lo, hi in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)):
        d = L[L.test & (L.conf >= lo) & (L.conf < hi)]
        cs = [_cluster_t(d[f"r_{h}"], d.day) for h in ("1h", "6h", "24h")]
        P(f"| {lo:.1f}-{hi:.1f} | {len(d)} | " + " | ".join(f"{c[0]:+.2f} (t {c[2]:+.1f})" if c[3] >= 3 else "-" for c in cs) + " |")
    # what the bot's gate would have done
    gate = L[(L.conf >= 0.8) & (L.exp >= 3) & L.newinfo]
    P(f"\nBot gate (conf >= 0.8, move >= 3, new info): {len(gate)} of {len(L)} historical calls; "
      + ", ".join(f"{h} {_cluster_t(gate[f'r_{h}'], gate.day)[0]:+.2f}" for h in ("1h", "6h", "24h")))
    res["gate_n"] = int(len(gate))
    # confound check: 2022 test days drifted Republican and Haiku leaned R
    for c, d in L[L.test].groupby("cycle"):
        P(f"{c} test: share of calls on the D side {np.mean(d.sgn > 0):.0%}; 24h move in call direction {d.r_24h.mean():+.2f}; "
          f"always back R {-d.d_24h.mean():+.2f}; always back D {d.d_24h.mean():+.2f} (points)")
    ok = L[f"r_{CAL_H}"].notna()
    cal_fn, cal_means = calibrator(L.raw[ok & ~L.test], L[f"r_{CAL_H}"][ok & ~L.test])
    L["cal"] = cal_fn(L.raw)
    res["cal_bins"] = {"edges": RAW_EDGES, "train_means": cal_means}
    P(f"\nBinned calibration ({CAL_H}, training days): score bins {RAW_EDGES[1:-1]} -> predicted move "
      + ", ".join(f"{m:+.2f}" for m in cal_means))
    for nm, d in (("train", L[~L.test]), ("test", L[L.test])):
        y = d[f"r_{CAL_H}"]
        P(f"  {nm}: Spearman(cal, realised {CAL_H}) {stats.spearmanr(d.cal, y, nan_policy='omit').correlation:+.3f}; "
          + "; ".join(f"cal {m:+.2f}: realised {y[d.cal == m].mean():+.2f} (n {int((d.cal == m).sum())})" for m in sorted(set(cal_means))))
    L.to_pickle(os.path.join(OUT, "labelled.pkl"))

    # ---- (4) SIG: does news add to the catch-up gap?
    sp, grid = _sig_panel(lab)
    P(f"\n## 4. SIG (live news_events {len(sp)}; with fair and SIG mid {int(sp[['gap', 's_1h']].notna().all(axis=1).sum())})\n")
    sp["cal"] = cal_fn(sp["h_raw"].fillna(0))
    sp["cal_bot"] = cal_fn(sp["bot_raw"].fillna(0))
    sp["alaska"] = sp["race"].str.contains("Alaska")
    sig = {}
    for h in ("15m", "1h", "6h"):
        gtr = grid[~grid.test & grid[f"s_{h}"].notna()]
        a, b = np.polyfit(gtr["gap"], gtr[f"s_{h}"], 1)[::-1] if len(gtr) > 20 else (0.0, 0.0)
        d = sp[sp[[f"s_{h}", "gap"]].notna().all(axis=1) & sp["h_sgn"].notna()].copy()
        d["resid"] = d[f"s_{h}"] - (a + b * d["gap"])
        d["r_sig"] = d["h_sgn"] * d["resid"]                         # SIG move beyond the catch-up, in the call's direction
        d["r_raw"] = d["h_sgn"] * d[f"s_{h}"]
        d["o_call"] = d["h_sgn"] * d[f"o_{h}"]
        d["pnl"] = d["r_raw"] - d["spread"] * 100                    # cross the spread in, exit at mid later: ~1 spread
        d["pnl_ticks"] = d["pnl"] / TICK
        cell = {"a": float(a), "b": float(b), "n_grid": int(len(gtr))}
        for nm, dd in (("train", d[~d.test]), ("test", d[d.test])):
            cell[nm] = {"n": int(len(dd)),
                        "rho_cal_resid": float(stats.spearmanr(dd["cal"], dd["r_sig"]).correlation) if len(dd) > 5 else None,
                        "resid_call": _cluster_t(dd["r_sig"], dd["race"]), "outside_call": _cluster_t(dd["o_call"], dd["race"]),
                        "pnl_all": _cluster_t(dd["pnl_ticks"], dd["race"]),
                        "pnl_cal_pos": _cluster_t(dd.loc[dd.cal > 0.25, "pnl_ticks"], dd.loc[dd.cal > 0.25, "race"]),
                        "outside_call_ex_alaska": _cluster_t(dd.loc[~dd.alaska, "o_call"], dd.loc[~dd.alaska, "race"]),
                        "outside_bot_dir": _cluster_t(dd["bot_sgn"] * dd[f"o_{h}"], dd["race"]),
                        "sig_bot_dir": _cluster_t(dd["bot_sgn"] * dd[f"s_{h}"], dd["race"]),
                        "median_abs_sig": float(dd[f"s_{h}"].abs().median()), "share_sig_moved": float((dd[f"s_{h}"] != 0).mean())}
            # incremental value: OLS s_h ~ gap + h_sgn*cal
            if len(dd) > 10:
                Z = np.column_stack([np.ones(len(dd)), dd["gap"], dd["h_sgn"] * dd["cal"]])
                beta, *_ = np.linalg.lstsq(Z, dd[f"s_{h}"].values, rcond=None)
                cell[nm]["ols_gap_news"] = list(map(float, beta))
        sig[h] = cell
        d.to_pickle(os.path.join(OUT, f"sig_{h}.pkl"))
        tr, te = cell["train"], cell["test"]
        P(f"{h}: catch-up fit on {cell['n_grid']} grid points: SIG move = {a:+.2f} + {b:.3f} x gap. "
          f"News events train n {tr['n']} / test n {te['n']}. SIG move beyond catch-up, in Haiku call direction: "
          f"train {tr['resid_call'][0]:+.2f} (t {tr['resid_call'][2]:+.1f}), test {te['resid_call'][0]:+.2f} (t {te['resid_call'][2]:+.1f}); "
          f"Spearman(cal, that) test {te['rho_cal_resid']}; outside move in call direction test {te['outside_call'][0]:+.2f}; "
          f"net P&L ticks/share test all {te['pnl_all'][0]:+.2f} (t {te['pnl_all'][2]:+.1f}), cal>0.25 {te['pnl_cal_pos'][0]:+.2f} (n {te['pnl_cal_pos'][3]}); "
          f"outside in call dir ex-Alaska {te['outside_call_ex_alaska'][0]:+.2f} (t {te['outside_call_ex_alaska'][2]:+.1f}); "
          f"bot's own (Opus) direction: outside {te['outside_bot_dir'][0]:+.2f}, SIG {te['sig_bot_dir'][0]:+.2f}; "
          f"SIG moved at all in {te['share_sig_moved']:.0%} of test events")
    res["sig"] = sig
    jsave(os.path.join(OUT, "results.json"), res)
    open(os.path.join(OUT, "out.md"), "w").write("\n".join(md) + "\n")
    print("\n".join(md))


def plot():
    import numpy as np
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10.5})
    L = pd.read_pickle(os.path.join(OUT, "labelled.pkl"))
    S = pd.read_pickle(os.path.join(OUT, f"sig_{CAL_H}.pkl"))
    S1 = pd.read_pickle(os.path.join(OUT, "sig_1h.pkl"))

    def grouped(x, y, g):
        """Mean of y per distinct calibrated value (0.32 and 0.33 pooled), clustered SE."""
        xr = np.round(np.asarray(x, float), 1)
        out = []
        for v in sorted(set(xr)):
            sel = (xr == v) & y.notna().values
            mu, se, t, n = _cluster_t(y[sel], g[sel])
            if n >= 3:
                out.append((v, mu, se, n))
        return out

    def draw(ax, data, col, label, mk="o"):
        for nm, ls, m_ in (("train", ":", "^"), ("test", "-", mk)):
            pts = data.get(nm)
            if not pts:
                continue
            m, mu, se, n = map(np.array, zip(*pts))
            ax.plot(m, mu, ls=ls, marker=m_, ms=7, color=col, lw=2.2 if nm == "test" else 1.6,
                    label=f"{label}, {'training' if nm == 'train' else 'test'} days ({int(n.sum())} headlines)")
            if nm == "test":
                ax.fill_between(m, mu - 1.96 * se, mu + 1.96 * se, color=col, alpha=0.13)

    def split(df, x, y, g):
        return {nm: grouped(df[x][df.test == (nm == "test")], df[y][df.test == (nm == "test")], df[g][df.test == (nm == "test")])
                for nm in ("train", "test")}

    fig, axs = plt.subplots(3, 1, figsize=(13, 27), gridspec_kw={"hspace": 0.68})
    fig.suptitle("Does a news model trained on past elections predict prices?", fontsize=17, weight="bold", y=0.991)
    fig.text(0.5, 0.980, "The bot's own news prompt scored 1,060 headlines from 2022 and 2024 and 751 live SIG headlines (21-24 Oct 2026); "
             "x = its predicted 6-hour move", ha="center", fontsize=11, color="#444")
    xs = np.linspace(-0.15, 0.45, 2)

    ax = axs[0]
    draw(ax, split(L, "cal", f"r_{CAL_H}", "day"), "#d62728", "Polymarket move 6 h after the headline, 2022 + 2024")
    L24 = L[L.cycle == "2024"]
    draw(ax, split(L24, "cal", "r_1h", "day"), "#1f77b4", "Polymarket move 1 h after the headline, 2024 only", "s")
    ax.plot(xs, xs, color="black", lw=1, ls="--", label="perfect prediction (realised = predicted)")
    ax.axhline(PM_COST, color="#777", lw=1.2, ls="-.", label="cost of a Polymarket round trip (1 point): profit needs a line above this")
    ax.set_title("A. Do the model's calls predict the Polymarket move? (2022 and 2024 campaigns)", loc="left", weight="bold")
    ax.set_ylabel("Outside move in the direction\nthe model called (points)")
    ax.set_ylim(-1.0, 1.6)

    ax = axs[1]
    draw(ax, split(S, "cal", "r_sig", "race"), "#2ca02c", "SIG move 6 h later beyond what the gap to the outside price predicts")
    draw(ax, split(S, "cal", "o_call", "race"), "#9467bd", "Polymarket/Kalshi move 6 h later on the same headlines", "D")
    ax.plot(xs, xs, color="black", lw=1, ls="--", label="perfect prediction")
    ax.set_title("B. On SIG, does the headline add anything once the outside price is known?", loc="left", weight="bold")
    ax.set_ylabel("Move in the direction\nthe model called (points)")
    ax.set_ylim(-3, 4)

    ax = axs[2]
    rng = np.random.default_rng(1)
    for nm, colr in (("train", "#aaa"), ("test", "#1f77b4")):
        d = S[S.test == (nm == "test")]
        ax.scatter(np.round(d.cal, 1) + rng.uniform(-0.012, 0.012, len(d)), d.pnl_ticks, s=10, color=colr, alpha=0.3)
    draw(ax, split(S, "cal", "pnl_ticks", "race"), "#1f77b4", "Buy SIG in the called direction, value at the mid 6 h later")
    draw(ax, split(S1, "cal", "pnl_ticks", "race"), "#ff7f0e", "Same, 1 h later", "s")
    ax.set_title("C. Profit per share on SIG after costs", loc="left", weight="bold")
    ax.set_ylabel("Profit per share after costs (ticks)")
    ax.set_ylim(-8, 8)
    ax.set_xlabel("Predicted move: the model's expected move x confidence, turned into points by the average 6-hour Polymarket move\n"
                  "that the same score earned on the 2022/2024 training days (the model's scores bunch into 4 values)")
    for a in axs:
        a.axhline(0, color="grey", lw=1)
        a.grid(alpha=0.3)
        a.set_xlim(-0.18, 0.42)
        a.legend(loc="upper center", bbox_to_anchor=(0.5, -0.09 if a is not axs[2] else -0.13), ncol=1, frameon=False, fontsize=10)
    gl = ("Glossary\n"
          "model: the bot's own news prompt (FINAL_SYSTEM in sigbot/news.py), run with Claude Haiku on each headline; it gives a side,\n"
          "    an expected move and a confidence. Its confidence never reached 0.6 on any of the 1,060 past headlines.\n"
          "point: 1 percentage point of probability = 0.01 in price;  tick: 0.005, the smallest SIG price step\n"
          "move in the called direction: positive when the price moved the way the model said\n"
          "outside price: Polymarket and Kalshi mids for the same party (2022/2024: Polymarket only)\n"
          "beyond what the gap predicts: SIG's move minus the catch-up toward the outside price that SIG makes anyway\n"
          "    (fitted on all legs every 10 min on the training days: 2% of the gap closes in 1 h, 10% in 6 h)\n"
          "dotted = training days, solid = test days (2022/2024: from 10 Oct; SIG: training 21-22 Oct, test 23-24 Oct);  bands: 95% range on test days\n"
          "costs: Polymarket 1 point round trip; SIG the bid-ask spread at entry (usually 1 tick)")
    fig.text(0.07, 0.008, gl, fontsize=10, va="bottom", ha="left", bbox=dict(boxstyle="round", fc="#f4f4f0", ec="#999"))
    fig.subplots_adjust(top=0.965, bottom=0.17, left=0.1, right=0.97)
    fig.savefig(FIG, dpi=100)
    print("wrote", FIG)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    {"fetch": fetch, "events": build_events, "label": label, "analyse": analyse, "plot": plot}[cmd]()
