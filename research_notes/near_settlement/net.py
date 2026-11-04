"""Shared fetch helpers for the near-settlement study: curl only, every response cached, one Kalshi rate limit across processes."""
import json, os, subprocess, time, urllib.parse
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "data", "research_near_settlement")
LOCK, STAMP = "/tmp/sig-near-settlement-kalshi.lock", "/tmp/sig-near-settlement-kalshi.stamp"
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA, CLOB = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"


def curl(url, params=None, timeout=40):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    r = subprocess.run(["curl", "-s", "-m", str(timeout), "-w", "\n%{http_code}", url], capture_output=True, text=True)
    body, _, code = r.stdout.rpartition("\n")
    try:
        return int(code or 0), (json.loads(body) if body else None)
    except ValueError:
        return int(code or 0), None


def kalshi_slot(gap=10.5):
    """Wait for this process's turn: one Kalshi call per `gap` seconds across every process (3 calls per 30 s)."""
    while True:
        try:
            os.mkdir(LOCK)
        except FileExistsError:
            time.sleep(.5)
            continue
        try:
            last = float(open(STAMP).read()) if os.path.exists(STAMP) else 0
            if time.time() - last >= gap:
                open(STAMP, "w").write(str(time.time()))
                return
        finally:
            os.rmdir(LOCK)
        time.sleep(1)


def kalshi(path, params=None, tries=4):
    for i in range(tries):
        kalshi_slot()
        code, d = curl(KBASE + path, params)
        if code == 429:
            time.sleep(30 * (i + 1))
            continue
        if code >= 500 or code == 0:
            time.sleep(10)
            continue
        return code, d
    return code, None


def cached(path, fn):
    if os.path.exists(path):
        return json.load(open(path))
    d = fn()
    if d is not None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(d, open(path, "w"))
    return d
