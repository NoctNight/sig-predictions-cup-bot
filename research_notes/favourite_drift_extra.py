"""Extra election legs for the favourite-drift study: 2022 US midterm AMM markets and resolved two-candidate races.

Writes data/polymarket_history/extra_legs.csv with the legs.csv header (cycle = election year, outcome 1/0 from
outcomePrices, p30..p0 = last price at or before election day 12:00 UTC minus d days).

Stdlib only. Polite: at most 4 requests/s, every raw response cached and never re-fetched.

Sources
  A. 2022 midterms (events 5808 Senate, 5863 governor, 834675 House). These were pre-CLOB FPMM markets, so the CLOB
     prices-history is empty. Prices are rebuilt from FPMM trades on Polygon:
       * Gamma event files (raw/events/<id>.json) give conditionId, marketMakerAddress (the FPMM) and clobTokenIds.
       * Polygon JSON-RPC (key-free public endpoints: polygon.gateway.tenderly.co, rpc-mainnet.matic.quiknode.pro), eth_getLogs over the FPMM addresses in 10,000-block
         chunks (the node's maximum range), topic FPMMBuy / FPMMSell (keccak256 of the event signature, computed here).
       * Block timestamps: eth_getBlockByNumber every 10,000 blocks, linear interpolation between them (about 6 hours).
       * Trade price for outcome i: Buy (investment - fee) / tokensBought, Sell (return + fee) / tokensSold.
       * Each trade on outcome j gives the other outcome 1 - price. The leg price is the last trade at or before t.
     The Goldsky FPMM subgraph endpoints are no longer usable (Goldsky docs: deprecated after the 2026-04-28 v2
     migration), and data-api.polymarket.com/trades returns nothing for these conditionIds.
  B. Resolved two-candidate races, 2023-2026. Gamma event files (raw/events_extra/<id>.json, kept apart from
     raw/events so favourite_drift_dataset.py does not pick them up) and CLOB daily history
     https://clob.polymarket.com/prices-history?market=<token>&interval=max&fidelity=1440 into raw/hist/.
     Event ids were found with https://gamma-api.polymarket.com/public-search?q=...&events_status=resolved
     (queries: poland/romania/ecuador/portugal/argentina/finland/slovakia presidential, wisconsin supreme court 2025,
     ga-14 / tn-7 / nj-11 / tx-18 special election, chile / colombia / peru / bolivia runoff) and fixed here.
     Not included: Kentucky 2023 and Louisiana 2023 governor (not in the search index); Chile 2025, Bolivia 2025,
     Peru 2026 and Colombia 2026 runoffs (the final-round winner market was not identified); Hungary 2026 (parliament,
     not a candidate race); Czech 2023 (no market above the volume floor).

Run:  python3 research_notes/favourite_drift_extra.py --fetch   (downloads what is missing from the cache, then builds)
      python3 research_notes/favourite_drift_extra.py           (builds from the cache only)
Importable: build_rows(), write_csv().
"""
from __future__ import annotations

import bisect
import calendar
import csv
import json
import os
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import favourite_drift_fetch as F  # noqa: E402  (shared TLS context, GET helper and CLOB history cache)

ROOT = F.ROOT
DATA = os.path.join(ROOT, "data", "polymarket_history")
RAW = F.RAW
RAW_EVENTS = os.path.join(RAW, "events")
EXTRA_EVENTS = os.path.join(RAW, "events_extra")
CHAIN = os.path.join(RAW, "chain")
OUT_CSV = os.path.join(DATA, "extra_legs.csv")

RPCS = ["https://polygon.gateway.tenderly.co", "https://rpc-mainnet.matic.quiknode.pro",
        "https://polygon-bor-rpc.publicnode.com"]   # key-free public Polygon endpoints, tried in order
DAY = 86400
MIN_VOLUME_A = 1000.0   # same rule as legs.csv
MIN_VOLUME_B = 5000.0   # spec for the new races
HORIZONS = [30, 25, 20, 13, 7, 3, 1, 0]
LEG_FIELDS = (["event_id", "slug", "cycle", "office", "party", "market_id", "token", "volume_usd", "outcome"]
              + [f"p{h}" for h in HORIZONS] + ["n_obs_30d", "first_obs", "last_obs"])
RPC_CHUNK = 10000
CHAIN_FROM, CHAIN_TO = "2022-09-01", "2022-11-11"   # window of FPMM trades read (election 2022-11-08)

# ---- election dates used ----------------------------------------------------------------------------------------
# A: 2022 midterms (FPMM). Election day 2022-11-08.
A_ELECTIONS = [
    {"event": 5808, "office": "senate", "cycle": "2022", "day": "2022-11-08"},
    {"event": 5863, "office": "governor", "cycle": "2022", "day": "2022-11-08"},
    {"event": 834675, "office": "house", "cycle": "2022", "day": "2022-11-08"},
]
# B: two-candidate finals. day = election day, or the runoff date for a runoff.
B_RACES = [
    {"event": 14874, "office": "intl", "day": "2025-06-01", "label": "Poland presidential runoff",
     "cands": ["Karol Nawrocki", "Rafał Trzaskowski"]},
    {"event": 16868, "office": "intl", "day": "2025-05-18", "label": "Romania presidential runoff",
     "cands": ["George Simion", "Nicușor Dan"]},
    {"event": 14882, "office": "intl", "day": "2025-04-13", "label": "Ecuador presidential runoff",
     "cands": ["Daniel Noboa", "Luisa González"]},
    {"event": 34587, "office": "intl", "day": "2026-02-08", "label": "Portugal presidential runoff",
     "cands": ["António José Seguro", "André Ventura"]},
    {"event": 901930, "office": "intl", "day": "2023-11-19", "label": "Argentina presidential runoff",
     "cands": ["Javier Milei", "Sergio Massa"]},
    {"event": 903250, "office": "intl", "day": "2024-02-11", "label": "Finland presidential runoff",
     "cands": ["Alexander Stubb", "Pekka Haavisto"]},
    {"event": 903525, "office": "intl", "day": "2024-04-06", "label": "Slovakia presidential runoff",
     "cands": ["Peter Pellegrini", "Ivan Korčok"]},
    {"event": 20810, "office": "us-offyear", "day": "2025-04-01", "label": "Wisconsin Supreme Court 2025",
     "cands": ["Susan Crawford", "Brad Schimel"]},
    {"event": 91200, "office": "us-special", "day": "2026-04-07", "label": "GA-14 special runoff",
     "cands": ["Clayton Fuller", "Shawn Harris"]},
    {"event": 41300, "office": "us-special", "day": "2025-12-02", "label": "TN-7 special",
     "cands": ["Matt Van Epps", "Aftyn Behn"]},
    {"event": 232533, "office": "us-special", "day": "2026-04-16", "label": "NJ-11 special",
     "cands": ["Analilia Mejia", "Joe Hathaway"]},
    {"event": 41298, "office": "us-special", "day": "2026-01-31", "label": "TX-18 special runoff",
     "cands": ["Christian Menefee", "Amanda Edwards"]},
]
PARTY_NAME = {"democrat": "D", "republican": "R", "independent": "I"}


# ---- helpers ----------------------------------------------------------------------------------------------------
def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _ts(day: str, hour: int = 0) -> int:
    return calendar.timegm(time.strptime(f"{day} {hour:02d}:00:00", "%Y-%m-%d %H:%M:%S"))


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _jlist(v) -> list:
    return json.loads(v) if isinstance(v, str) else (v or [])


def _read(path):
    with open(path) as f:
        return json.load(f)


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def _event(eid: int, fetch: bool, folder: str = EXTRA_EVENTS):
    p = os.path.join(folder, f"{eid}.json")
    if not os.path.exists(p):
        if not fetch:
            return None
        data = F.get(f"{F.GAMMA}/events", {"id": eid})
        _write(p, data)
    evs = _read(p)
    return evs[0] if evs else None


# ---- keccak-256 (Ethereum), stdlib only -------------------------------------------------------------------------
def keccak256(data: bytes) -> bytes:
    rate = 136
    rc = []
    def rc_bit(t):
        if t % 255 == 0:
            return 1
        r = 1
        for _ in range(t % 255):
            r <<= 1
            if r & 0x100:
                r ^= 0x171
        return r & 1
    for i in range(24):
        v = 0
        for j in range(7):
            if rc_bit(j + 7 * i):
                v |= 1 << ((1 << j) - 1)
        rc.append(v)
    rot = [[0] * 5 for _ in range(5)]
    x, y = 1, 0
    for t in range(24):
        rot[x][y] = ((t + 1) * (t + 2) // 2) % 64
        x, y = y, (2 * x + 3 * y) % 5
    M = (1 << 64) - 1

    def rol(v, n):
        return ((v << n) | (v >> (64 - n))) & M if n else v

    def permute(A):
        for rnd in range(24):
            C = [A[i][0] ^ A[i][1] ^ A[i][2] ^ A[i][3] ^ A[i][4] for i in range(5)]
            D = [C[(i - 1) % 5] ^ rol(C[(i + 1) % 5], 1) for i in range(5)]
            A = [[A[i][j] ^ D[i] for j in range(5)] for i in range(5)]
            B = [[0] * 5 for _ in range(5)]
            for i in range(5):
                for j in range(5):
                    B[j][(2 * i + 3 * j) % 5] = rol(A[i][j], rot[i][j])
            A = [[B[i][j] ^ ((~B[(i + 1) % 5][j] & M) & B[(i + 2) % 5][j]) for j in range(5)] for i in range(5)]
            A[0][0] ^= rc[rnd]
        return A

    msg = bytearray(data) + b"\x01"
    while len(msg) % rate:
        msg.append(0)
    msg[-1] |= 0x80
    A = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            A[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        A = permute(A)
    out = b""
    for i in range(4):
        out += A[i % 5][i // 5].to_bytes(8, "little")
    return out


assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
assert keccak256(b"Transfer(address,address,uint256)").hex() == \
    "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_BUY = "0x" + keccak256(b"FPMMBuy(address,uint256,uint256,uint256,uint256)").hex()
TOPIC_SELL = "0x" + keccak256(b"FPMMSell(address,uint256,uint256,uint256,uint256)").hex()


# ---- Polygon RPC (cached) -----------------------------------------------------------------------------------------
def _rpc(method: str, params: list):
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for attempt in range(3):
        for url in RPCS:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                                  "User-Agent": "sig-favourite-drift-study/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=60, context=F.CTX) as r:
                    j = json.loads(r.read())
                time.sleep(F.PAUSE)
                if "error" in j:
                    raise RuntimeError(f"rpc {method} at {url}: {j['error']}")
                return j["result"]
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.5)
        time.sleep(2.0 * (attempt + 1))
    raise last


def _block(n: int, fetch: bool):
    p = os.path.join(CHAIN, "blocks", f"{n}.json")
    if not os.path.exists(p):
        if not fetch:
            return None
        _write(p, _rpc("eth_getBlockByNumber", [hex(n), False]))
    return _read(p)


def _block_ts(n: int, fetch: bool) -> int:
    b = _block(n, fetch)
    return int(b["timestamp"], 16)


def _block_at(ts_target: int, fetch: bool) -> int:
    """Smallest block whose timestamp >= ts_target (bisection, every probe cached)."""
    cache_path = os.path.join(CHAIN, "day_blocks.json")
    key = str(ts_target)
    cache = _read(cache_path) if os.path.exists(cache_path) else {}
    if key in cache:
        return cache[key]
    if not fetch:
        raise RuntimeError("block for day not cached; run with --fetch")
    lo, hi = 25_000_000, int(_rpc("eth_blockNumber", []), 16)
    while lo < hi:
        mid = (lo + hi) // 2
        if _block_ts(mid, True) < ts_target:
            lo = mid + 1
        else:
            hi = mid
    cache[key] = lo
    _write(cache_path, cache)
    return lo


def _anchors(fetch: bool):
    """(block, timestamp) every RPC_CHUNK blocks over the chain window."""
    a = _block_at(_ts(CHAIN_FROM), fetch)
    b = _block_at(_ts(CHAIN_TO), fetch)
    blocks = list(range(a, b + RPC_CHUNK, RPC_CHUNK))
    return a, b, [(n, _block_ts(n, fetch)) for n in blocks]


def _logs(addresses: list, lo: int, hi: int, fetch: bool):
    key = keccak256(",".join(sorted(a.lower() for a in addresses)).encode()).hex()[:12]
    p = os.path.join(CHAIN, "logs", f"{lo}_{hi}_{key}.json")
    if not os.path.exists(p):
        if not fetch:
            return None
        res = _rpc("eth_getLogs", [{"address": addresses, "fromBlock": hex(lo), "toBlock": hex(hi)}])
        _write(p, res)
    return _read(p)


# ---- A: FPMM trades -> prices -----------------------------------------------------------------------------------
def _fpmm_markets(ev_list):
    out = []
    for ev in ev_list:
        for m in ev.get("markets", []):
            vol = float(m.get("volumeNum") or 0)
            if vol < MIN_VOLUME_A or not m.get("marketMakerAddress"):
                continue
            out.append((ev, m, vol))
    return out


def _outcome_party(name: str):
    return PARTY_NAME.get(name.strip().lower(), name.strip()[:1].upper())


def _legs_from_series(ev, m, vol, cycle, office, party_list, series_by_idx, day, token_list, px_list, slug):
    T = _ts(day, 12)
    rows = []
    for idx, party in enumerate(party_list):
        series = sorted(series_by_idx.get(idx, {}).items())
        times = [t for t, _ in series]
        vals = [p for _, p in series]
        path = {}
        for d in HORIZONS:
            i = bisect.bisect_right(times, T - d * DAY) - 1
            path[d] = round(vals[i], 6) if i >= 0 else None
        n30 = bisect.bisect_right(times, T) - bisect.bisect_left(times, T - 30 * DAY)
        rows.append({
            "event_id": str(ev.get("id")), "slug": slug, "cycle": cycle, "office": office, "party": party,
            "market_id": str(m.get("id")), "token": token_list[idx], "volume_usd": round(vol, 2),
            "outcome": round(float(px_list[idx])), **{f"p{h}": path[h] for h in HORIZONS},
            "n_obs_30d": n30, "first_obs": _date(times[0]) if times else "",
            "last_obs": _date(times[-1]) if times else "",
        })
    return rows


def build_a(fetch: bool, status: Counter):
    cfg = {e["event"]: e for e in A_ELECTIONS}
    evs = [_event(e["event"], fetch, RAW_EVENTS) for e in A_ELECTIONS]
    if any(ev is None for ev in evs):
        raise RuntimeError("A events missing from raw/events; they should already be cached")
    markets = _fpmm_markets(evs)
    addresses = sorted({m["marketMakerAddress"] for _, m, _ in markets})
    by_addr = {m["marketMakerAddress"].lower(): (ev, m, vol) for ev, m, vol in markets}
    a_block, b_block, anchors = _anchors(fetch)
    a_blocks = [n for n, _ in anchors]
    a_ts = [t for _, t in anchors]

    def ts_of(n):
        i = bisect.bisect_right(a_blocks, n) - 1
        i = min(max(i, 0), len(anchors) - 2)
        (n0, t0), (n1, t1) = anchors[i], anchors[i + 1]
        return int(t0 + (n - n0) * (t1 - t0) / (n1 - n0))

    # trade prices per (market, outcome index): {ts: price}
    trades = {}
    n_logs = 0
    for lo in range(a_block, b_block + 1, RPC_CHUNK):
        hi = min(lo + RPC_CHUNK - 1, b_block)
        logs = _logs(addresses, lo, hi, fetch)
        if logs is None:
            status["chunks missing"] += 1
            continue
        for lg in logs:
            n_logs += 1
            topics = lg.get("topics", [])
            data = bytes.fromhex(lg["data"][2:])
            if len(topics) != 3 or len(data) != 96 or topics[0] not in (TOPIC_BUY, TOPIC_SELL):
                continue
            hit = by_addr.get(lg["address"].lower())
            if hit is None:
                continue
            ev, m, _ = hit
            w = [int.from_bytes(data[32 * k:32 * k + 32], "big") for k in range(3)]
            idx = int(topics[2], 16)
            if w[2] <= 0:
                continue
            if topics[0] == TOPIC_BUY:
                price = (w[0] - w[1]) / w[2]
            else:
                price = (w[0] + w[1]) / w[2]
            if not 0.0 < price < 1.0:
                status["trade price outside (0,1)"] += 1
                continue
            t = ts_of(int(lg["blockNumber"], 16))
            trades.setdefault((m["id"], idx), {})[t] = price
    status["fpmm logs read"] += n_logs

    rows = []
    for ev, m, vol in markets:
        eid = ev.get("id")
        e = cfg[int(eid)] if int(eid) in cfg else None
        if e is None:
            continue
        outcomes = [str(o) for o in _jlist(m.get("outcomes"))]
        toks = _jlist(m.get("clobTokenIds"))
        pxs = _jlist(m.get("outcomePrices"))
        if len(outcomes) != 2 or len(toks) != 2 or len(pxs) != 2:
            status["A market not binary"] += 1
            continue
        parties = [_outcome_party(o) for o in outcomes]
        series = {0: {}, 1: {}}
        for idx in (0, 1):
            for t, p in trades.get((m["id"], idx), {}).items():
                series[idx][t] = p                     # own outcome price
                series[1 - idx][t] = 1.0 - p           # complementary outcome, same trade
        slug = ev.get("slug") or ""
        if not series[0] and not series[1]:
            status["A markets with no FPMM trades"] += 1
        rows.extend(_legs_from_series(ev, m, vol, e["cycle"], e["office"], parties, series, e["day"], toks, pxs, slug))
    return rows


# ---- B: CLOB daily histories -------------------------------------------------------------------------------------
def _match(markets, name: str):
    key = _ascii(name)
    for m in markets:
        q = _ascii(m.get("question") or "")
        if f"will {key} " in q and _jlist(m.get("outcomes")) == ["Yes", "No"]:
            return m, 0
    for m in markets:
        outs = [_ascii(str(o)) for o in _jlist(m.get("outcomes"))]
        sur = _ascii(name.split()[-1])
        if len(outs) == 2 and sur in outs:
            return m, outs.index(sur)
    return None, None


def build_b(fetch: bool, status: Counter):
    rows = []
    for r in B_RACES:
        ev = _event(r["event"], fetch)
        if ev is None:
            status[f"B event missing {r['event']}"] += 1
            continue
        T = _ts(r["day"], 12)
        markets = ev.get("markets", [])
        cycle = r["day"][:4]
        for name in r["cands"]:
            m, idx = _match(markets, name)
            if m is None:
                status[f"B candidate not found: {name} ({r['event']})"] += 1
                continue
            vol = float(m.get("volumeNum") or 0)
            if vol < MIN_VOLUME_B:
                status[f"B below volume: {name}"] += 1
                continue
            toks = _jlist(m.get("clobTokenIds"))
            pxs = _jlist(m.get("outcomePrices"))
            token = toks[idx]
            if fetch:
                try:
                    F.fetch_history(token, "d")
                except Exception as e:  # noqa: BLE001
                    status[f"B history failed {name}: {str(e)[:60]}"] += 1
                    continue
            p = os.path.join(RAW, "hist", f"{token}_d.json")
            if not os.path.exists(p):
                status[f"B history missing {name}"] += 1
                continue
            pts = _read(p).get("history") or []
            if not pts:
                status[f"B history empty {name}"] += 1
            series = {int(x["t"]): float(x["p"]) for x in pts}
            party = name.split()[-1]
            rows.extend(_legs_from_series(ev, m, vol, cycle, r["office"], [party], {0: series}, r["day"],
                                          [token], [pxs[idx]], ev.get("slug") or ""))
            status[f"B legs {r['label']}"] += 1
    return rows


# ---- output -------------------------------------------------------------------------------------------------------
def build_rows(fetch: bool = False):
    status: Counter = Counter()
    rows = build_a(fetch, status) + build_b(fetch, status)
    rows.sort(key=lambda r: (r["cycle"], r["office"], r["slug"], r["party"], r["market_id"]))
    return rows, status


def write_csv(rows):
    os.makedirs(DATA, exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(LEG_FIELDS)
        for r in rows:
            w.writerow(["" if r[k] is None else r[k] for k in LEG_FIELDS])
    return OUT_CSV


def main():
    fetch = "--fetch" in sys.argv
    rows, status = build_rows(fetch)
    write_csv(rows)
    a = [r for r in rows if r["cycle"] == "2022"]
    print(f"legs {len(rows)}; 2022 legs {len(a)}, with p20 {sum(r['p20'] is not None for r in a)}")
    print("by race:", dict(Counter((r["cycle"], r["office"], r["slug"][:45]) for r in rows)))
    in_band = sum(1 for r in rows if r["p20"] is not None and 0.75 <= r["p20"] <= 0.95)
    print(f"p20 in [0.75, 0.95]: {in_band}")
    for k, v in sorted(status.items()):
        print("  status:", k, v)
    print("wrote", OUT_CSV)


if __name__ == "__main__":
    main()
