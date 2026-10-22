"""Download resolved Polymarket two-party election markets (2022, 2023, 2024, 2025) and their price histories.

Stdlib only. Polite: ~4 requests/s, every response cached under data/polymarket_history/raw/ and never re-fetched.
Sources: Gamma API https://gamma-api.polymarket.com (events, markets) and CLOB https://clob.polymarket.com/prices-history.
Discovery (done once by hand on 2026-10-22 with /public-search, queries listed in QUERIES below): event ids are
hard-coded so the dataset is reproducible even if the search index changes. The presidential-state events of 2024 live
in a contiguous id block (903630-903690) which is scanned.

Run:  python3 research_notes/favourite_drift_fetch.py          (rebuilds only what is missing from the cache)
Then: python3 research_notes/favourite_drift_study.py          (rebuilds the CSV and every table from the cache)
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "polymarket_history", "raw")
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PAUSE = 0.25

QUERIES = ["senate 2022", "governor 2022", "2022 house", "us-senate-election-winner", "presidential-election-winner",
           "governor-election-winner", "house-race", "d-vs", "mississippi governor 2023", "virginia governor 2025"]

EVENT_IDS = [
    # 2022 midterms (one event per office, two-outcome markets)
    5808, 5863, 834675,
    # 2023 off-year
    902823,
    # 2024 US Senate (<state>-us-senate-election-winner)
    10018, 10019, 10020, 10021, 10022, 10214, 10218, 10219, 10231, 10232, 10234, 10235, 10236, 10238, 10240, 10241,
    10242, 10243, 10244, 10245, 10246, 10247, 10249, 10250, 10251, 10253, 10254, 10255, 10257, 10258, 10260, 10261, 903799,
    # 2024 governors
    903757, 903759, 903761, 903763, 903764, 903765, 903766, 903768, 903769, 10606, 10607,
    # 2024 House ("<st>-<dd>-election-<dem>-d-vs-<rep>-r")
    11924, 12029, 12030, 12031, 12032, 12056, 12057, 12058, 12059, 12061, 12063, 12246, 12250, 12476, 12682, 12683,
    13461, 13908, 13909, 14095, 14102, 14267,
    # 2024 presidential, states outside the contiguous block + district electors
    10166, 10169, 10170, 10382, 10164, 10172, 10173, 10174, 10175,
    # 2025 governors
    26507, 28830,
]
PRES_BLOCK = range(903630, 903691)


def _ctx() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        for p in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(p):
                return ssl.create_default_context(cafile=p)
        return ssl.create_default_context()


CTX = _ctx()


def get(url: str, params: dict | None = None, retries: int = 3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sig-favourite-drift-study/0.1"})
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                data = json.loads(r.read().decode())
            time.sleep(PAUSE)
            return data
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))
    return None


def cached(path: str, fetch):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    data = fetch()
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def fetch_event(eid: int):
    p = os.path.join(RAW, "events", f"{eid}.json")
    return cached(p, lambda: get(f"{GAMMA}/events", {"id": eid}))


def fetch_history(token: str, kind: str, start: int | None = None, end: int | None = None):
    p = os.path.join(RAW, "hist", f"{token}_{kind}.json")
    if kind == "d":
        params = {"market": token, "interval": "max", "fidelity": 1440}
    else:
        params = {"market": token, "startTs": start, "endTs": end, "fidelity": 60}
    return cached(p, lambda: get(f"{CLOB}/prices-history", params))


def election_day(end_date: str) -> str:
    y = (end_date or "")[:4]
    return {"2022": "2022-11-08", "2023": "2023-11-07", "2024": "2024-11-05", "2025": "2025-11-04"}.get(y, (end_date or "")[:10])


def main():
    os.makedirs(os.path.join(RAW, "events"), exist_ok=True)
    os.makedirs(os.path.join(RAW, "hist"), exist_ok=True)
    ids = list(EVENT_IDS)
    for eid in PRES_BLOCK:
        ids.append(eid)
    events = []
    for eid in ids:
        try:
            ev = fetch_event(eid)
        except Exception as e:  # noqa: BLE001
            print("event", eid, "failed:", e, file=sys.stderr)
            continue
        if not ev:
            continue
        ev = ev[0]
        slug = ev.get("slug") or ""
        if eid in PRES_BLOCK and not slug.endswith("presidential-election-winner"):
            continue
        events.append(ev)
    print("events", len(events))
    # tokens to pull: every market's YES/first token and, for two-outcome markets, the second token too
    n = 0
    for ev in events:
        eday = election_day(ev.get("endDate") or "")
        t_el = int(time.mktime(time.strptime(eday + " 12:00:00 UTC", "%Y-%m-%d %H:%M:%S %Z")))
        for m in ev.get("markets", []):
            try:
                toks = json.loads(m.get("clobTokenIds") or "[]")
            except Exception:  # noqa: BLE001
                toks = []
            outcomes = json.loads(m.get("outcomes") or "[]")
            if float(m.get("volumeNum") or 0) < 1000:
                continue
            want = toks[:2] if outcomes[:2] not in (["Yes", "No"],) else toks[:1]
            for tok in want:
                try:
                    fetch_history(tok, "d")
                    fetch_history(tok, "h", t_el - 45 * 86400, t_el + 3 * 86400)
                    n += 1
                except Exception as e:  # noqa: BLE001
                    print("hist", tok[:12], "failed:", e, file=sys.stderr)
    with open(os.path.join(RAW, "..", "sources.json"), "w") as f:
        json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "gamma": GAMMA + "/events?id=<id>",
                   "clob": CLOB + "/prices-history?market=<token>&interval=max&fidelity=1440 and &fidelity=60 for the last 45 days",
                   "discovery_queries": QUERIES, "event_ids": [e["id"] for e in events], "tokens": n}, f, indent=1)
    print("histories", n)


if __name__ == "__main__":
    main()
