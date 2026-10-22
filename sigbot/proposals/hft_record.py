"""Read-only side-by-side recording of the SIG realtime feed and Polymarket's public CLOB market WebSocket.

    python3 -m sigbot.proposals.hft_record --minutes 60 --out sigbot/proposals/hft_data/rec_YYYYMMDD_HHMM.jsonl

One JSONL file, every line stamped with the local receive time ``rx`` (epoch s):
  {"t":"book","src":"sig","rx","ex","as_of","seq","bids":[[p,q]..6],"asks":[[p,q]..6]}   (every SIG book push)
  {"t":"trade","src":"sig","rx","ex","id","seq","price","qty","side","at"}
  {"t":"pm","rx","a":token,"bb","ba","ts"}    Polymarket best bid/ask, written only when it changes (book/price_change/best_bid_ask)
  {"t":"pmt","rx","a":token,"p","q","side","ts"}  Polymarket last_trade_price
  {"t":"map","legs":{exchange_id: {"race","party","pm_token"}}}                          (first line)
  {"t":"own","rx","orders":[...]}       our open orders at start and end (GET /orders?status=open, paged)
  {"t":"acct","rx","fills","orderUpdates"}  our account batches (to track our own resting orders in between)
Costs: one POST /realtime/token (no order); 2 x ceil(open orders / 200) REST reads for the own-order snapshots; Polymarket is public.
"""
from __future__ import annotations

import argparse, json, logging, os, threading, time

from sigbot.feed import RealtimeFeed, WebSocket, _IDLE, _s
from sigbot.universe import Universe

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
TOURNAMENT = "bda92870-621e-47b0-bc3c-3602c5c26f55"


def leg_map() -> dict:
    uni = Universe.from_markets(json.load(open(os.path.join(ROOT, "data", "markets.json"))))
    rm = json.load(open(os.path.join(ROOT, "data", "race_map.json")))["races"]
    out = {}
    for name, race in uni.races.items():
        pm_legs = ((rm.get(name) or {}).get("polymarket") or {}).get("legs") or {}
        for party, leg in race.legs.items():
            toks = pm_legs.get(party) or []
            out[leg.exchange_id] = {"race": name, "party": party, "pm_token": toks[0]["token"] if len(toks) == 1 else None}
    return out


def _lv(levels, n=6):
    out = []
    for l in (levels or [])[:n]:
        if isinstance(l, dict):
            out.append([l.get("price"), l.get("quantity", l.get("size", l.get("qty")))])
        else:
            out.append(list(l)[:2])
    return out


class Recorder:
    def __init__(self, path: str):
        self.f, self.lock, self.n = open(path, "a", buffering=1 << 16), threading.Lock(), {}

    def write(self, rec: dict):
        with self.lock:
            self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            self.n[rec["t"]] = self.n.get(rec["t"], 0) + 1


class RecordingFeed(RealtimeFeed):
    def __init__(self, rec: Recorder, *a, **kw):
        super().__init__(*a, **kw)
        self.rec = rec

    def _market_batch(self, market_id, batch, now):
        for b in batch.get("books") or []:
            a = b.get("asOf") or {}
            self.rec.write({"t": "book", "src": "sig", "rx": now, "ex": _s(b.get("exchangeId")), "as_of": a.get("at"),
                            "seq": a.get("sequence"), "bids": _lv(b.get("bids")), "asks": _lv(b.get("asks"))})
        for t in batch.get("trades") or []:
            self.rec.write({"t": "trade", "src": "sig", "rx": now, "ex": _s(t.get("exchangeId")), "id": _s(t.get("id")),
                            "seq": t.get("sequence"), "price": t.get("price"), "qty": t.get("quantity"),
                            "side": t.get("takerSide") or t.get("side"), "at": t.get("executedAt") or t.get("createdAt")})
        return super()._market_batch(market_id, batch, now)

    def _account_batch(self, body, now):
        self.rec.write({"t": "acct", "rx": now, "fills": body.get("fills") or [], "orderUpdates": body.get("orderUpdates") or []})
        return super()._account_batch(body, now)


def own_orders(rec: Recorder, key: str, log):
    """Snapshot of our resting orders (a few paged REST reads) so the study can take them out of the books."""
    from sigbot.api import SuperMarketClient
    api = SuperMarketClient(key, "https://sig.thesuper.market/api/v1", TOURNAMENT)
    rows = api.open_orders()
    keep = [{k: o.get(k) for k in ("orderId", "exchangeId", "side", "action", "price", "remainingQuantity", "quantity",
                                    "createdAt", "expirationDate")} for o in rows]
    rec.write({"t": "own", "rx": time.time(), "orders": keep})
    log.info("own open orders snapshot: %d", len(keep))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pm_event(rec: Recorder, x: dict, now: float, best: dict):
    et, ts = x.get("event_type"), x.get("timestamp")
    if et == "book":
        bids, asks = [_f(l["price"]) for l in x.get("bids") or []], [_f(l["price"]) for l in x.get("asks") or []]
        items = [(x.get("asset_id"), max(bids) if bids else None, min(asks) if asks else None)]
    elif et == "price_change":
        items = [(c.get("asset_id"), _f(c.get("best_bid")), _f(c.get("best_ask"))) for c in x.get("price_changes") or []]
    elif et == "best_bid_ask":
        items = [(x.get("asset_id"), _f(x.get("best_bid")), _f(x.get("best_ask")))]
    elif et == "last_trade_price":
        rec.write({"t": "pmt", "rx": now, "a": x.get("asset_id"), "p": _f(x.get("price")), "q": _f(x.get("size")),
                   "side": x.get("side"), "ts": ts})
        return
    else:
        return
    for a, bb, ba in items:
        if a in best and best[a] == (bb, ba):
            continue
        best[a] = (bb, ba)
        rec.write({"t": "pm", "rx": now, "a": a, "bb": bb, "ba": ba, "ts": ts})


def pm_loop(rec: Recorder, tokens: list, stop: threading.Event, log):
    while not stop.is_set():
        try:
            ws = WebSocket(PM_WS)
            ws.send_text(json.dumps({"assets_ids": tokens, "type": "market", "custom_feature_enabled": True}))
            log.info("polymarket subscribed to %d tokens", len(tokens))
            last_ping, best = time.time(), {}
            while not stop.is_set():
                if time.time() - last_ping > 9:
                    ws.send_text("PING")
                    last_ping = time.time()
                m = ws.recv()
                if m is _IDLE:
                    continue
                if m is None:
                    raise ConnectionError("closed")
                now = time.time()
                txt = m.decode(errors="replace")
                if txt in ("PONG", ""):
                    continue
                try:
                    msg = json.loads(txt)
                except ValueError:
                    rec.write({"t": "pm_raw", "rx": now, "txt": txt[:200]})
                    continue
                for x in (msg if isinstance(msg, list) else [msg]):
                    _pm_event(rec, x, now, best)
        except Exception as e:
            log.warning("polymarket ws: %s: %s; reconnecting", type(e).__name__, e)
            stop.wait(2)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-sig", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("hft_record")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    rec = Recorder(a.out)
    legs = leg_map()
    rec.write({"t": "map", "rx": time.time(), "legs": legs})
    stop = threading.Event()
    toks = sorted({v["pm_token"] for v in legs.values() if v["pm_token"]})
    th = threading.Thread(target=pm_loop, args=(rec, toks, stop, log), daemon=True)
    th.start()
    feed = None
    if not a.no_sig:
        key = open(os.path.join(ROOT, ".sig_api_key")).read().strip()
        feed = RecordingFeed(rec, key, "https://sig.thesuper.market/api/v1", TOURNAMENT)
        feed.start()
        own_orders(rec, key, log)
    t0 = time.time()
    while time.time() - t0 < a.minutes * 60:
        time.sleep(30)
        log.info("t+%.0fs counts %s", time.time() - t0, rec.n)
    if feed:
        own_orders(rec, key, log)
    stop.set()
    if feed:
        feed.stop()
    rec.f.flush()


if __name__ == "__main__":
    main()
