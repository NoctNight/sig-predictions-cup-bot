"""Election-night 2022 Polymarket price paths (FPMM trades on Polygon), window 2022-11-08 21:00 UTC to 2022-11-10 05:00 UTC.

The CLOB prices-history is empty for the 2022 tokens, so paths are rebuilt from FPMMBuy/FPMMSell logs, reusing the helpers
of favourite_drift_extra.py (RPC, keccak, topics). eth_getLogs for these FPMM addresses over the whole chain window were
already cached there in 10,000-block chunks (raw/chain/logs); chunks overlapping the window are reused from that cache and
only fetched (same chunking) if absent. Exact block timestamps are fetched for every block that holds a trade in the
window (cached under data/election_night/raw/chain2022/blocks; the older raw/chain/blocks cache is read first).

Price of a leg after a trade = average trade price (buy: (investment - fee)/tokens; sell: (return + fee)/tokens) of the
traded outcome, or 1 - that for the complementary outcome (same method as favourite_drift_extra.py).

Outputs (data/election_night/): paths_2022.csv, legs_2022.json.   Run: python3 research_notes/election_night_fetch_2022.py
"""
from __future__ import annotations

import csv
import glob
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import favourite_drift_extra as E  # noqa: E402

ROOT = E.ROOT
OUT = os.path.join(ROOT, "data", "election_night")
RAW = os.path.join(OUT, "raw", "chain2022")
T0, T1 = 1667941200, 1668056400          # 2022-11-08 21:00 UTC .. 2022-11-10 05:00 UTC
T_POLLS = 1667948400                      # 2022-11-08 23:00 UTC
EVENTS = [(5808, "senate"), (5863, "governor"), (834675, "house")]
CHUNK = E.RPC_CHUNK
SLEEP = 0.2


def _rd(p):
    with open(p) as f:
        return json.load(f)


def _wr(p, o):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(o, f)


def block_ts(n: int) -> int:
    for p in (os.path.join(RAW, "blocks", f"{n}.json"), os.path.join(E.CHAIN, "blocks", f"{n}.json")):
        if os.path.exists(p):
            return int(_rd(p)["timestamp"], 16)
    b = E._rpc("eth_getBlockByNumber", [hex(n), False])
    _wr(os.path.join(RAW, "blocks", f"{n}.json"), {"timestamp": b["timestamp"], "number": b["number"]})
    time.sleep(SLEEP)
    return int(b["timestamp"], 16)


def block_at(ts: int) -> int:
    """Smallest block with timestamp >= ts (bisection around a linear estimate; ~2.1 s blocks)."""
    cp = os.path.join(RAW, "window_blocks.json")
    cache = _rd(cp) if os.path.exists(cp) else {}
    if str(ts) in cache:
        return cache[str(ts)]
    ref_b, ref_t = 32534499, 1661990400        # known anchor from the earlier study
    guess = ref_b + int((ts - ref_t) / 2.1)
    lo, hi = guess - 300_000, guess + 300_000
    assert block_ts(lo) < ts <= block_ts(hi)
    while lo < hi:
        mid = (lo + hi) // 2
        if block_ts(mid) < ts:
            lo = mid + 1
        else:
            hi = mid
    cache[str(ts)] = lo
    _wr(cp, cache)
    return lo


def load_legs():
    seen = {}
    for f in ("legs.csv", "extra_legs.csv"):
        for r in csv.DictReader(open(os.path.join(ROOT, "data", "polymarket_history", f))):
            if r["cycle"] == "2022":
                seen[(r["market_id"], r["token"])] = r
    markets = {}
    for eid, office in EVENTS:
        ev = _rd(os.path.join(E.RAW_EVENTS, f"{eid}.json"))[0]
        for m in ev["markets"]:
            toks = E._jlist(m.get("clobTokenIds"))
            outs = E._jlist(m.get("outcomes"))
            if not m.get("marketMakerAddress"):
                continue
            markets[m["id"]] = dict(event=eid, office=office, slug=ev.get("slug", ""), addr=m["marketMakerAddress"].lower(),
                                    question=m.get("question", ""), toks=toks, outs=outs)
    legs = []
    for (mid, tok), r in sorted(seen.items(), key=lambda kv: (kv[1]["office"], kv[1]["market_id"], kv[1]["party"])):
        m = markets.get(mid)
        if m is None or tok not in m["toks"]:
            raise RuntimeError(f"leg without market/token: {mid} {tok}")
        legs.append(dict(market=m, market_id=mid, idx=m["toks"].index(tok), party=r["party"],
                         outcome=r["outcome"], office=r["office"], event=m["event"], slug=m["slug"],
                         question=m["question"]))
    return legs


def window_logs(addresses, b0, b1):
    """All FPMM logs in [b0, b1], from the 10,000-block chunk cache (fetched with the same chunking if missing)."""
    out = []
    start = 32534499                           # chunk grid of favourite_drift_extra (a_block + k*10,000)
    lo = start + ((b0 - start) // CHUNK) * CHUNK
    while lo <= b1:
        hi = lo + CHUNK - 1
        logs = E._logs(addresses, lo, hi, fetch=False)
        if logs is None:                        # not in the older cache: fetch with the same call, cache in the old format
            logs = E._logs(addresses, lo, hi, fetch=True)
            time.sleep(SLEEP)
        out.extend(lg for lg in logs if b0 <= int(lg["blockNumber"], 16) <= b1)
        lo += CHUNK
    return out


def main():
    os.makedirs(RAW, exist_ok=True)
    legs = load_legs()
    addresses = sorted({l["market"]["addr"] for l in legs})
    print("legs", len(legs), "markets", len(addresses))
    # the older cache's key covers every FPMM with volume >= 1000 in the three events: use that exact address list
    evs = [_rd(os.path.join(E.RAW_EVENTS, f"{eid}.json"))[0] for eid, _ in EVENTS]
    all_addr = sorted({m["marketMakerAddress"] for _, m, _ in E._fpmm_markets(evs)})
    b0, b1 = block_at(T0), block_at(T1)
    print("window blocks", b0, b1, b1 - b0)
    logs = window_logs(all_addr, b0, b1)
    print("logs in window", len(logs))
    by_addr = {}
    for lg in logs:
        topics = lg.get("topics", [])
        data = bytes.fromhex(lg["data"][2:])
        if len(topics) != 3 or len(data) != 96 or topics[0] not in (E.TOPIC_BUY, E.TOPIC_SELL):
            continue
        w = [int.from_bytes(data[32 * k:32 * k + 32], "big") for k in range(3)]
        if w[2] <= 0:
            continue
        buy = topics[0] == E.TOPIC_BUY
        price = (w[0] - w[1]) / w[2] if buy else (w[0] + w[1]) / w[2]
        if not 0.0 < price < 1.0:
            continue
        by_addr.setdefault(lg["address"].lower(), []).append(
            dict(block=int(lg["blockNumber"], 16), li=int(lg["logIndex"], 16), idx=int(topics[2], 16), price=price,
                 usd=w[0] / 1e6, buy=buy))
    blocks = sorted({t["block"] for a in addresses for t in by_addr.get(a, [])})
    print("distinct trade blocks to timestamp", len(blocks))
    ts = {}
    for i, n in enumerate(blocks):
        ts[n] = block_ts(n)
        if i % 200 == 0:
            print("  block ts", i, len(blocks), flush=True)

    # last pre-window price needs trades before T0 too; not requested (task asks for price before 23:00 UTC within window)
    rows, summary = [], []
    for l in legs:
        trs = sorted(by_addr.get(l["market"]["addr"], []), key=lambda t: (t["block"], t["li"]))
        path = []
        for t in trs:
            tt = ts[t["block"]]
            if not T0 <= tt <= T1:
                continue
            p = t["price"] if t["idx"] == l["idx"] else 1.0 - t["price"]
            path.append((tt, round(p, 6), round(t["usd"], 2)))
            rows.append(["polymarket", l["office"], l["event"], l["slug"], l["party"], l["outcome"], tt, round(p, 6),
                         round(t["usd"], 2), l["market_id"], l["question"]])
        pre = [p for tt, p, _ in path if tt <= T_POLLS]
        summary.append(dict(office=l["office"], event=l["event"], slug=l["slug"], market_id=l["market_id"], question=l["question"],
                            party=l["party"], outcome=int(float(l["outcome"])), n_trades=len(path),
                            first_ts=path[0][0] if path else None, last_ts=path[-1][0] if path else None,
                            last_price_before_2300utc=pre[-1] if pre else None,
                            last_price_in_window=path[-1][1] if path else None,
                            usd_volume=round(sum(u for _, _, u in path), 2)))
    rows.sort(key=lambda r: (r[1], r[4 + 0], str(r[5]), r[6]))
    with open(os.path.join(OUT, "paths_2022.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["venue", "office", "event", "slug", "party", "outcome", "ts", "price", "usd_size", "market_id", "question"])
        w.writerows(rows)
    _wr(os.path.join(OUT, "legs_2022.json"), summary)
    ns = [s["n_trades"] for s in summary]
    print("legs", len(summary), "trade rows", len(rows), "median/min/max trades per leg", statistics.median(ns), min(ns), max(ns))
    print("zero-trade legs", sum(n == 0 for n in ns))


if __name__ == "__main__":
    main()
