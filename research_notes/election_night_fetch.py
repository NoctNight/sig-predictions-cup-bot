#!/usr/bin/env python3
"""Fetch 1-min election-night 2024 price paths from Polymarket and Kalshi (cached raw JSON)."""
import csv, json, os, ssl, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import certifi

R = "/Users/sizhework/Library/Application Support/Claude/scratch-workspaces/62f47635-ebde-4c1d-bfe7-883a5dbae4be/3a53c166-2070-4578-aee2-0aaccd642218/scratch-2026-10-21-e81342"
OUT = R + "/data/election_night"
START, END = 1730840400, 1730955600
T_POLLS, T_END = 1730847600, 1730955600
UA = {"User-Agent": "Mozilla/5.0 (research; election-night-fetch)"}
CTX = ssl.create_default_context(cafile=certifi.where())
errors = []

def http_json(url, retries=6):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45, context=CTX) as r:
                return 200, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                ra = e.headers.get("Retry-After")
                time.sleep(float(ra) if ra and ra.replace(".", "").isdigit() else 2 ** (i + 1))
                continue
            return e.code, None
        except Exception as e:
            time.sleep(2 ** (i + 1))
            last = repr(e)
    errors.append(("giveup", url))
    return 0, None

def cached(path, fn):
    if os.path.exists(path):
        return json.load(open(path))
    d = fn()
    if d is not None:
        json.dump(d, open(path, "w"))
    return d

def fetch_poly(tok):
    def go():
        pts = {}
        mid = START + 16 * 3600
        for a, b in ((START, mid), (mid, END)):
            code, d = http_json(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={a}&endTs={b}&fidelity=1")
            if d is None:
                errors.append((f"poly HTTP {code}", tok[:12])); return None
            for h in d.get("history", []):
                pts[h["t"]] = h["p"]
        return sorted(pts.items())
    return cached(f"{OUT}/raw/polymarket/{tok}.json", go)

def fetch_kalshi(tk):
    def go():
        q = f"start_ts={START}&end_ts={END}&period_interval=1"
        code, d = http_json(f"https://api.elections.kalshi.com/trade-api/v2/historical/markets/{tk}/candlesticks?{q}")
        if d is None and code == 404:
            ser = tk.split("-")[0]
            time.sleep(0.35)
            code, d = http_json(f"https://api.elections.kalshi.com/trade-api/v2/series/{ser}/markets/{tk}/candlesticks?{q}")
        time.sleep(0.35)
        if d is None:
            errors.append((f"kalshi HTTP {code}", tk)); return None
        return d
    return cached(f"{OUT}/raw/kalshi/{tk}.json", go)

def fl(x):
    try:
        return None if x is None else float(x)
    except Exception:
        return None

def kalshi_rows(d):
    rows = []
    for c in (d or {}).get("candlesticks", []):
        ts = c["end_period_ts"]
        bid = fl((c.get("yes_bid") or {}).get("close")); ask = fl((c.get("yes_ask") or {}).get("close"))
        pr = fl((c.get("price") or {}).get("close"))
        if pr is None: pr = fl((c.get("price") or {}).get("previous"))
        if bid is not None and ask is not None and bid > 0 and ask < 1:
            p = (bid + ask) / 2
        else:
            p = pr
        rows.append((ts, p, bid, ask, fl(c.get("volume"))))
    return sorted(rows)

def at(rows, t):
    v = None
    for r in rows:
        if r[0] <= t and r[1] is not None: v = r[1]
        elif r[0] > t: break
    return v

def main():
    pm = [r for r in csv.DictReader(open(R + "/data/polymarket_history/legs.csv")) if r["cycle"] == "2024"]
    ka = [r for r in csv.DictReader(open(R + "/data/polymarket_history/kalshi_legs.csv")) if r["cycle"] == "2024"]
    legs, out = [], []
    pm = [r for r in pm if r["token"]]
    with ThreadPoolExecutor(4) as ex:
        pres = list(ex.map(lambda r: fetch_poly(r["token"]), pm))
    for r, d in zip(pm, pres):
        rows = [(t, p, None, None, None) for t, p in (d or [])]
        legs.append(dict(venue="polymarket", office=r["office"], event=r["event_id"], slug_or_ticker=r["slug"], market_id=r["market_id"],
                         party=r["party"], outcome=r["outcome"], fetched=d is not None, rows=rows))
    for r in ka:
        d = fetch_kalshi(r["market_id"])
        legs.append(dict(venue="kalshi", office=r["office"], event=r["event_id"], slug_or_ticker=r["market_id"], market_id=r["market_id"],
                         party=r["party"], outcome=r["outcome"], fetched=d is not None, rows=kalshi_rows(d)))
    with open(OUT + "/paths_2024.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow("venue,office,event,slug_or_ticker,party,outcome,ts,price,bid,ask,volume".split(","))
        for L in legs:
            for ts, p, b, a, v in L["rows"]:
                w.writerow([L["venue"], L["office"], L["event"], L["slug_or_ticker"], L["party"], L["outcome"], ts,
                            "" if p is None else p, "" if b is None else b, "" if a is None else a, "" if v is None else v])
    meta = []
    for L in legs:
        rs = L["rows"]
        meta.append(dict(venue=L["venue"], office=L["office"], event=L["event"], slug_or_ticker=L["slug_or_ticker"], party=L["party"],
                         outcome=L["outcome"], fetched=L["fetched"], n_points=len(rs), first_ts=rs[0][0] if rs else None,
                         last_ts=rs[-1][0] if rs else None, price_at_1730847600=at(rs, T_POLLS), price_at_1730955600=at(rs, T_END)))
    json.dump(meta, open(OUT + "/legs_2024.json", "w"), indent=1)
    print("errors", errors)
    from collections import Counter
    print(Counter((m["venue"], m["office"]) for m in meta))
    print("no data", Counter((m["venue"], m["office"]) for m in meta if m["n_points"] == 0))
    print("not fetched", [(m["venue"], m["slug_or_ticker"][:40]) for m in meta if not m["fetched"]])
    print("rows", sum(len(L["rows"]) for L in legs))

main()
