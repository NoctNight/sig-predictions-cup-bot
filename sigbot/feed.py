"""Realtime market-data feed: Supabase Realtime (Phoenix channels over WebSocket), stdlib only.

Protocol
--------
1. ``POST {base}/realtime/token`` (bearer API key) -> ``{token, expiresAt, supabaseUrl, anonKey, channels.user}``.
   The token lives 3 hours; ``channels.user`` is our ``user:{profile_id}`` channel.
2. Connect ``wss://{host of supabaseUrl}/realtime/v1/websocket?apikey={anonKey}&vsn=1.0.0``. RFC 6455 client
   framing is implemented below (masked client frames, fragmentation, ping/pong, close; batches reach ~250 KB).
3. Phoenix messages are JSON objects ``{topic, event, payload, ref[, join_ref]}``; the topic is
   ``"realtime:" + channel``. Join with ``phx_join`` carrying ``{"config": {..., "private": true},
   "access_token": token}``; the reply is ``phx_reply`` with ``payload.status`` ``ok`` or ``error``
   (``Unauthorized`` / ``Too many channels`` is permanent for that token: no retry until a fresh one).
   Heartbeat ``{"topic": "phoenix", "event": "heartbeat"}`` every 25 s; no reply within 30 s -> reconnect.
   Server pushes arrive as event ``broadcast`` with ``payload = {type, event: <name>, payload: {...}}``.
4. Before expiry the token is minted again and pushed with the ``access_token`` event on every joined channel;
   a channel the server closed (``phx_close`` / ``phx_error``) or refused is re-joined.

Channels: ``tournament:{tournament_id}:markets`` -> ``markets_batch`` ``{markets: [{marketId, batch}]}`` where each
``batch`` is ``{trades, bookDirty, marketSettled, books, delivery, resyncRequired?}``; ``user:{profile_id}`` ->
``account_batch`` ``{fills, orderUpdates, settlements, refunds, delivery}``.

Resync rules (data/realtime_docs.md)
------------------------------------
* One last accepted ``delivery.revision`` per market (markets topic) and one for the user topic.
* ``books`` apply whenever their ``asOf`` (sequence, then at) is newer than the held book, even on a duplicate
  revision; a held book with a null ``asOf`` is replaced by any pushed book.
* A duplicate revision (``revision <= last``) has its arrays ignored. A gap (``previousRevision > last``), a
  ``resyncRequired`` flag, a trade with a null ``sequence``, ``book_dirty`` / ``market_settled`` hints, the initial
  join, every reconnect and every token refresh put the affected exchange ids into ``needs_resync()``; the bot
  refetches those books over REST and calls ``mark_resynced``. A market's exchanges are learned from its batches
  or given up front with ``set_universe``.
* The topic carries only markets that changed, so ``book()`` is None until an exchange's first push: seed books
  over REST at start (``needs_resync`` lists every known exchange after the first join).
"""
from __future__ import annotations

import argparse, base64, collections, hashlib, json, logging, os, random, socket, ssl, statistics, struct, sys
import threading, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

HEARTBEAT_SEC, HEARTBEAT_TIMEOUT_SEC, TOKEN_REFRESH_MARGIN_SEC, MAX_BACKOFF_SEC = 25.0, 30.0, 15 * 60, 60.0
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_IDLE = object()


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _s(v: Any) -> Optional[str]:
    """Ids as strings (the feed sends numeric exchangeIds in books, REST sends strings)."""
    return None if v is None else str(v)


def _iso_ts(s: Any) -> Optional[float]:
    """ISO-8601 -> epoch seconds; tolerates 'Z' and the engine clock's 7 fractional digits."""
    if not isinstance(s, str):
        return None
    try:
        s = s.replace("Z", "+00:00")
        head, dot, frac = s.partition(".")
        if dot:
            tz = frac[frac.find("+"):] if "+" in frac else ""
            s = f"{head}.{frac[: len(frac) - len(tz)][:6]}{tz}"
        return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def _version_key(as_of: Any) -> tuple:
    return (as_of.get("sequence") or -1, _iso_ts(as_of.get("at")) or -1.0) if isinstance(as_of, dict) else (-1, -1.0)


class WebSocket:
    """Minimal RFC 6455 client over an ssl-wrapped socket (1 s socket timeout = idle tick for the caller)."""

    def __init__(self, url: str, connect_timeout: float = 15.0):
        u = urllib.parse.urlsplit(url)
        host, port = u.hostname, u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock = _ssl_context().wrap_socket(raw, server_hostname=host) if u.scheme == "wss" else raw
        self._send_lock, self._pending = threading.Lock(), b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            buf += chunk
        head, _, self._pending = buf.partition(b"\r\n\r\n")
        lines = head.decode(errors="replace").split("\r\n")
        hdrs = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        accept = base64.b64encode(hashlib.sha1(key.encode() + _WS_GUID).digest()).decode()
        if " 101 " not in lines[0] or hdrs.get("sec-websocket-accept") != accept:
            raise ConnectionError(f"websocket handshake failed: {lines[0]}")
        self.sock.settimeout(1.0)

    def send_frame(self, opcode: int, payload: bytes = b""):
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                         if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        with self._send_lock:
            self.sock.sendall(head + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def send_text(self, text: str):
        self.send_frame(0x1, text.encode())

    def close(self, code: int = 1000):
        for fn in (lambda: self.send_frame(0x8, struct.pack(">H", code)), self.sock.close):
            try:
                fn()
            except Exception:
                pass

    def _read_exact(self, n: int, first: bool = False) -> bytes:
        """Read n bytes. With first=True a timeout before any byte arrives returns b'' (idle tick)."""
        out, self._pending = self._pending[:n], self._pending[n:]
        deadline = time.monotonic() + 60.0
        while len(out) < n:
            try:
                chunk = self.sock.recv(min(65536, n - len(out)))
            except (socket.timeout, TimeoutError):
                if first and not out:
                    return b""
                if time.monotonic() > deadline:
                    raise ConnectionError("websocket: stalled mid-frame")
                continue
            if not chunk:
                raise ConnectionError("websocket: connection closed")
            out += chunk
        return out

    def recv(self) -> Any:
        """One complete text/binary message (bytes), _IDLE on an idle tick, or None when the server closed."""
        message = b""
        while True:
            h = self._read_exact(2, first=True)
            if not h:
                return _IDLE
            fin, opcode, masked, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x80, h[1] & 0x7F
            if n >= 126:
                n = struct.unpack(">H" if n == 126 else ">Q", self._read_exact(2 if n == 126 else 8))[0]
            mask = self._read_exact(4) if masked else None
            data = self._read_exact(n) if n else b""
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if opcode == 0x8:
                self.close()
                return None
            if opcode == 0x9:
                self.send_frame(0xA, data)
            elif opcode in (0x1, 0x2, 0x0):
                message += data
                if fin:
                    return message
            elif opcode != 0xA:
                raise ConnectionError(f"websocket: unexpected opcode {opcode}")


class RealtimeFeed(threading.Thread):
    """Background realtime feed. Every public getter is thread-safe (one lock)."""

    def __init__(self, api_key: str, base_url: str, tournament_id: str, store=None, log=None):
        super().__init__(name="sigbot-feed", daemon=True)
        self.api_key, self.base_url, self.tournament_id = api_key, base_url.rstrip("/"), str(tournament_id)
        self.store = store  # kept for the caller; the feed never writes to it
        self.log = log or logging.getLogger("sigbot.feed")
        self.lock, self._stop = threading.Lock(), threading.Event()
        self.updated = threading.Event()   # set whenever a market batch is applied (the arb scanner wakes on it)
        self.keep_samples = False  # probe only: keep one raw markets_batch, message sizes and trade latencies
        self._token, self._token_exp, self._ws, self._ref, self._sessions = {}, 0.0, None, 0, 0
        self._connected_since: Optional[float] = None
        self._channels: dict[str, dict] = {}  # topic -> {status, join_ref, refused}
        self._books: dict[str, dict] = {}
        self._last_rev: dict[str, int] = {}  # market id (or "user") -> last accepted revision
        self._ex_of_market: dict[str, set] = {}
        self._known_exchanges, self._resync = set(), set()
        self._fills, self._trades = [], []
        self.counts, self.rev_deltas = collections.Counter(), collections.Counter()
        self.sizes, self.latencies, self.sample, self.join_results = [], [], None, {}
        self._st = {"reconnects": 0, "batches": 0, "trades": 0, "fills": 0, "last_message": 0.0, "last_batch": 0.0,
                    "token_refreshes": 0}

    # --------------------------------------------------------------- public
    def start(self):
        super().start()

    def stop(self):
        self._stop.set()
        if self._ws:
            self._ws.close()

    def set_universe(self, exchanges_by_market: dict):
        """{market_id: [exchange_id, ...]}: lets gaps and the initial join name every exchange of a market."""
        with self.lock:
            for mid, exs in exchanges_by_market.items():
                exs = {_s(e) for e in exs}
                self._ex_of_market.setdefault(_s(mid), set()).update(exs)
                self._known_exchanges.update(exs)

    def book(self, exchange_id) -> Optional[dict]:
        with self.lock:
            b = self._books.get(_s(exchange_id))
            return json.loads(json.dumps(b)) if b else None

    def books(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self._books))

    def best(self, exchange_id) -> Optional[tuple]:
        with self.lock:
            b = self._books.get(_s(exchange_id))
            return (b["bestBid"], b["bestAsk"]) if b else None

    def bests(self) -> dict:
        """{exchange_id: (bestBid, bestAsk, feed_ts)} for every held book, under one lock."""
        with self.lock:
            return {ex: (b.get("bestBid"), b.get("bestAsk"), b.get("feed_ts", 0.0)) for ex, b in self._books.items()}

    def drain_fills(self) -> list:
        with self.lock:
            out, self._fills = self._fills, []
        return out

    def drain_trades(self) -> list:
        with self.lock:
            out, self._trades = self._trades, []
        return out

    def needs_resync(self) -> set:
        with self.lock:
            return set(self._resync)

    def mark_resynced(self, exchange_ids):
        with self.lock:
            self._resync.difference_update(_s(e) for e in exchange_ids)

    def stats(self) -> dict:
        now = time.time()
        with self.lock:
            st = self._st
            return {"connected": self._ws is not None and self._connected_since is not None,
                    "connected_since": self._connected_since, "reconnects": st["reconnects"],
                    "batches": st["batches"], "trades": st["trades"], "fills": st["fills"],
                    "books_held": len(self._books), "resync_pending": len(self._resync),
                    "last_message_age_sec": (now - st["last_message"]) if st["last_message"] else None,
                    "last_batch_age_sec": (now - st["last_batch"]) if st["last_batch"] else None,
                    "join_status": {t.removeprefix("realtime:"): c["status"] for t, c in self._channels.items()},
                    "token_expires_in_sec": (self._token_exp - now) if self._token_exp else None,
                    "token_refreshes": st["token_refreshes"], "events": dict(self.counts)}

    def healthy(self, max_silence_sec: float = 120) -> bool:
        s = self.stats()
        return bool(s["connected"] and s["join_status"] and all(v == "joined" for v in s["join_status"].values())
                    and s["last_message_age_sec"] is not None and s["last_message_age_sec"] < max_silence_sec)

    # ------------------------------------------------------------ main loop
    def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            started = time.time()
            try:
                self._session()
            except Exception as e:
                if not self._stop.is_set():
                    self.log.warning("feed session ended: %s: %s", type(e).__name__, e)
            ws, self._ws = self._ws, None
            if ws:
                ws.close()
            with self.lock:
                self._connected_since = None
                self._last_rev.clear()   # a reconnect refetches everything anyway; the first batches must not log a gap per market
                for c in self._channels.values():
                    c["status"] = "closed" if c["status"] == "joined" else c["status"]
            if self._stop.is_set():
                break
            backoff = 1.0 if time.time() - started > 120 else min(MAX_BACKOFF_SEC, backoff * 2)
            wait = backoff * (0.5 + random.random())
            self.log.info("feed reconnecting in %.1fs", wait)
            self._stop.wait(wait)

    def _mint_token(self):
        req = urllib.request.Request(self.base_url + "/realtime/token", method="POST", data=b"",
                                     headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
            tok = json.loads(resp.read())
        if not tok.get("token"):
            raise RuntimeError("realtime token response carried no token")
        exp = _iso_ts(tok.get("expiresAt")) or (time.time() + 3 * 3600)
        with self.lock:
            self._token, self._token_exp = tok, exp
        self.log.info("realtime token minted (expires in %.0f min, user channel %s)", (exp - time.time()) / 60,
                      tok.get("channels", {}).get("user"))

    def _send(self, topic: str, event: str, payload: dict) -> str:
        self._ref += 1
        msg = {"topic": topic, "event": event, "payload": payload, "ref": str(self._ref)}
        if event == "phx_join":
            msg["join_ref"] = msg["ref"]
        self._ws.send_text(json.dumps(msg))
        return msg["ref"]

    def _join(self, topic: str):
        payload = {"config": {"broadcast": {"ack": False, "self": False}, "presence": {"key": ""},
                              "postgres_changes": [], "private": True}, "access_token": self._token["token"]}
        ref = self._send(topic, "phx_join", payload)
        with self.lock:
            self._channels[topic] = {"status": "joining", "join_ref": ref, "refused": False}

    def _session(self):
        if not self._token or self._token_exp - time.time() < TOKEN_REFRESH_MARGIN_SEC:
            self._mint_token()  # a token with >15 min left is reused across reconnects
        host = urllib.parse.urlsplit(self._token["supabaseUrl"]).netloc
        q = urllib.parse.urlencode({"apikey": self._token["anonKey"], "vsn": "1.0.0"})
        self._ws = WebSocket(f"wss://{host}/realtime/v1/websocket?{q}")
        with self.lock:
            self._connected_since = self._st["last_message"] = time.time()
            self._st["reconnects"] += 1 if self._sessions else 0
            self._sessions += 1
            self._channels.clear()
        self.log.info("feed connected to %s", host)
        for ch in (f"tournament:{self.tournament_id}:markets", self._token["channels"]["user"]):
            self._join("realtime:" + ch)  # Phoenix topic = "realtime:" + channel name
        self._mark_all_resync("connect")
        hb_ref, hb_sent, next_hb = None, 0.0, time.time() + HEARTBEAT_SEC
        while not self._stop.is_set():
            raw = self._ws.recv()
            if raw is None:
                raise ConnectionError("server closed the websocket")
            now = time.time()
            if raw is not _IDLE:
                with self.lock:
                    self._st["last_message"] = now
                msg = json.loads(raw)
                if msg.get("topic") == "phoenix" and msg.get("ref") == hb_ref:
                    hb_ref = None
                else:
                    self._handle(msg, len(raw), now)
            if hb_ref and now - hb_sent > HEARTBEAT_TIMEOUT_SEC:
                raise ConnectionError("heartbeat reply timed out")
            if now >= next_hb:
                hb_ref, hb_sent, next_hb = self._send("phoenix", "heartbeat", {}), now, now + HEARTBEAT_SEC
            if self._token_exp - now < TOKEN_REFRESH_MARGIN_SEC:
                self._refresh_token()
            for t, c in list(self._channels.items()):
                if c["status"] == "closed" and not c["refused"]:
                    self.log.info("re-joining %s", t)
                    self._join(t)

    def _refresh_token(self):
        self._mint_token()
        with self.lock:
            self._st["token_refreshes"] += 1
            joined = [t for t, c in self._channels.items() if c["status"] == "joined"]
            for c in self._channels.values():  # refused channels get another join with the fresh token
                c["refused"], c["status"] = False, ("closed" if c["status"] == "refused" else c["status"])
        for t in joined:
            self._send(t, "access_token", {"access_token": self._token["token"]})
        self._mark_all_resync("token refresh")
        self.log.info("realtime token refreshed on %d channels", len(joined))

    def _mark_all_resync(self, why: str):
        with self.lock:
            self._resync.update(self._known_exchanges, self._books.keys())
            n = len(self._resync)
        self.log.warning("resync required for %d exchanges (%s)", n, why)

    def _mark(self, exchanges, why: str):
        exchanges = {e for e in exchanges if e}
        if exchanges:
            with self.lock:
                self._resync.update(exchanges)
            self.log.warning("resync required for %s (%s)", sorted(exchanges)[:8], why)

    # ------------------------------------------------------------ messages
    def _handle(self, msg: dict, size: int, now: float):
        topic, event, payload = msg.get("topic"), msg.get("event"), msg.get("payload") or {}
        ch = self._channels.get(topic)
        if event == "phx_reply":
            if ch and msg.get("ref") == ch["join_ref"]:
                ok = payload.get("status") == "ok"
                resp = payload.get("response") or {}
                reason = "" if ok else (resp.get("reason") or json.dumps(resp)[:200])
                with self.lock:
                    ch["status"], ch["refused"] = ("joined" if ok else "refused"), not ok
                    self.join_results[topic.removeprefix("realtime:")] = "ok" if ok else f"error: {reason}"
                (self.log.info if ok else self.log.warning)("join %s -> %s %s", topic, payload.get("status"), reason)
            return
        if event in ("phx_close", "phx_error"):
            self.log.warning("channel %s %s: %s", topic, event, json.dumps(payload)[:200])
            with self.lock:
                if ch:
                    ch["status"] = "closed"
            return
        if event != "broadcast":
            with self.lock:
                self.counts[f"_{event}"] += 1
            return
        bev, body = payload.get("event"), payload.get("payload") or {}
        with self.lock:
            self.counts[bev] += 1
            if self.keep_samples:
                self.sizes.append(size)
                self.sample = self.sample or (body if bev == "markets_batch" else None)
        if bev == "markets_batch":
            for entry in body.get("markets") or []:
                self._market_batch(_s(entry.get("marketId")), entry.get("batch") or {}, now)
        elif bev == "market_batch":
            self._market_batch(_s(body.get("marketId")) or "?", body, now)
        elif bev == "account_batch":
            self._account_batch(body, now)
        elif bev == "book_dirty":
            self._mark({_s(body.get("exchangeId"))}, "book_dirty")
        elif bev in ("market_settled", "position_settled", "refund"):
            with self.lock:
                exs = set(self._ex_of_market.get(_s(body.get("marketId")), ()))
            self._mark(exs | {_s(body.get("exchangeId"))}, bev)

    def _apply_book(self, market_id: Optional[str], b: dict, now: float) -> Optional[str]:
        """Caller holds the lock. Replaces the held book when the pushed asOf is newer; returns the exchange id."""
        ex = _s(b.get("exchangeId"))
        held = self._books.get(ex) if ex else None
        if ex is None or (held and held.get("asOf") is not None and _version_key(held["asOf"]) >= _version_key(b.get("asOf"))):
            return ex
        bids, asks = b.get("bids") or [], b.get("asks") or []
        bb, ba = (bids[0]["price"] if bids else None), (asks[0]["price"] if asks else None)
        self._books[ex] = {"exchangeId": ex, "marketId": market_id or _s(b.get("marketId")), "asOf": b.get("asOf"),
                           "depth": max(len(bids), len(asks)), "bids": bids, "asks": asks, "bestBid": bb, "bestAsk": ba,
                           "spread": (ba - bb) if bb is not None and ba is not None else None,
                           "nextExpiryAt": b.get("nextExpiryAt"), "feed_ts": now}
        return ex

    def wait_update(self, timeout: float) -> bool:
        """Block until a new market batch arrives (or timeout); clears the flag. True if a batch arrived."""
        got = self.updated.wait(timeout)
        self.updated.clear()
        return got

    def _market_batch(self, market_id: str, batch: dict, now: float):
        self.updated.set()
        delivery = batch.get("delivery") or {}
        rev, prev = delivery.get("revision"), delivery.get("previousRevision")
        trades = batch.get("trades") or []
        with self.lock:
            self._st["batches"], self._st["last_batch"] = self._st["batches"] + 1, now
            named = {self._apply_book(market_id, b, now) for b in batch.get("books") or []}
            named |= {_s(i["exchangeId"]) for i in trades + (batch.get("bookDirty") or []) if i.get("exchangeId") is not None}
            named.discard(None)
            self._ex_of_market.setdefault(market_id, set()).update(named)
            self._known_exchanges.update(named)
            affected = self._ex_of_market[market_id] or {f"market:{market_id}"}
            last = self._last_rev.get(market_id)
            if isinstance(rev, int) and isinstance(prev, int):
                self.rev_deltas[rev - prev] += 1
            if rev is not None and (last is None or rev > last):
                self._last_rev[market_id] = rev
        if batch.get("resyncRequired"):  # books (if any) already applied above
            self._mark(affected, "resyncRequired")
            return
        if last is not None and rev is not None and rev <= last:
            with self.lock:
                self.counts["_duplicate_revision"] += 1
            return
        if last is not None and prev is not None and prev > last:
            self.log.warning("revision gap on market %s: last %s, batch covers %s..%s", market_id, last, prev, rev)
            self._mark(affected, "revision gap")
        if any(t.get("sequence") is None for t in trades):
            self._mark(affected, "trade without sequence")
        with self.lock:
            # a full book in a complete batch is the authoritative state at its asOf: nothing is left to refetch for it
            # (a gap only loses trades, which the bot backfills from the REST tape)
            self._resync.difference_update(_s(b.get("exchangeId")) for b in batch.get("books") or [])
            for t in trades:
                self._trades.append(dict(t, exchangeId=_s(t.get("exchangeId")), marketId=_s(t.get("marketId")) or market_id,
                                         feed_ts=now))
                ts = _iso_ts(t.get("executedAt") or t.get("createdAt")) if self.keep_samples else None
                if ts:
                    self.latencies.append(now - ts)
            self._st["trades"] += len(trades)
            del self._trades[:-50000]
        for m in batch.get("marketSettled") or []:
            self._mark(affected, f"marketSettled={m.get('settledWith')}")

    def _account_batch(self, body: dict, now: float):
        delivery = body.get("delivery") or {}
        rev, prev = delivery.get("revision"), delivery.get("previousRevision")
        with self.lock:
            self._st["batches"], self._st["last_batch"] = self._st["batches"] + 1, now
            last = self._last_rev.get("user")
            if isinstance(rev, int) and isinstance(prev, int):
                self.rev_deltas[rev - prev] += 1
            if rev is not None and (last is None or rev > last):
                self._last_rev["user"] = rev
        if last is not None and rev is not None and rev <= last:
            with self.lock:
                self.counts["_duplicate_revision"] += 1
            return
        if last is not None and prev is not None and prev > last:
            self.log.warning("revision gap on user topic: last %s, batch covers %s..%s (refetch fills over REST)",
                             last, prev, rev)
        with self.lock:
            self.counts["_user_gap"] += int(last is not None and prev is not None and prev > last)
            for f in body.get("fills") or []:
                q, p = f.get("quantity"), f.get("price")  # realtime price is YES-denominated; REST is side-denominated
                side = f.get("side") or ("no" if isinstance(q, (int, float)) and q < 0 else "yes")
                self._fills.append({"id": f.get("id", f.get("tradeId", f.get("sequence"))), "orderId": f.get("orderId"),
                                    "exchangeId": _s(f.get("exchangeId")), "marketId": _s(f.get("marketId")),
                                    "price": (round(1 - p, 4) if side == "no" and p is not None else p), "price_yes": p,
                                    "quantity": q, "side": side, "filledAt": f.get("executedAt") or f.get("filledAt"),
                                    "tournamentId": _s(f.get("tournamentId")), "feed_ts": now})
            self._st["fills"] += len(body.get("fills") or [])
            self.counts["_orderUpdates"] += len(body.get("orderUpdates") or [])
            self.counts["_settlements"] += len(body.get("settlements") or [])


# ------------------------------------------------------------------------- CLI
def _trim(obj: Any, keep: int = 2) -> Any:
    """Shorten arrays for a readable sample and drop anything token-like."""
    if isinstance(obj, dict):
        return {k: _trim(v, keep) for k, v in obj.items() if "token" not in k.lower()}
    if isinstance(obj, list):
        return [_trim(v, keep) for v in obj[:keep]] + ([f"... {len(obj) - keep} more"] if len(obj) > keep else [])
    return obj


def _probe(seconds: int, base: str, tournament: str, rest: bool = True):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    key = open(os.path.join(root, ".sig_api_key")).read().strip()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from sigbot.universe import Universe
    uni = Universe.from_markets(json.load(open(os.path.join(root, "data", "markets.json"))))
    feed = RealtimeFeed(key, base, tournament)
    feed.keep_samples = True
    feed.set_universe({l.market_id: [l.exchange_id] for r in uni.races.values() for l in r.legs.values()})
    feed.start()
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(5)
        s = feed.stats()
        print(f"  t+{time.time() - t0:4.0f}s connected={s['connected']} joined={sum(v == 'joined' for v in s['join_status'].values())}"
              f"/{len(s['join_status'])} batches={s['batches']} books={s['books_held']} trades={s['trades']} fills={s['fills']}",
              flush=True)
    s, exs = feed.stats(), uni.all_exchanges()
    held = [e for e in exs if feed.book(e)]
    print("\n== join results:", json.dumps(feed.join_results))
    print("== message counts by event:", dict(feed.counts))
    print(f"== books held: {len(held)} of {len(exs)} universe exchanges ({s['books_held']} total held)")
    if feed.sizes:
        print(f"== message size bytes: median {statistics.median(feed.sizes):.0f} max {max(feed.sizes)} n={len(feed.sizes)}")
    print("== revision - previousRevision distribution:", dict(sorted(feed.rev_deltas.items())))
    print("== resync pending:", len(feed.needs_resync()))
    if feed.latencies:
        print(f"== trade latency (receive - executedAt) sec: median {statistics.median(feed.latencies):.2f} "
              f"min {min(feed.latencies):.2f} max {max(feed.latencies):.2f} n={len(feed.latencies)}")
    ages = [b["feed_ts"] - (_iso_ts(b["asOf"]["at"]) or b["feed_ts"]) for b in feed.books().values() if b.get("asOf")]
    if ages:
        print(f"== book asOf.at -> receive sec: median {statistics.median(ages):.2f} max {max(ages):.2f} n={len(ages)}")
    if feed.sample:
        sample = dict(feed.sample, markets=feed.sample.get("markets", [])[:1])
        print("== sample markets_batch (first market entry, arrays trimmed):")
        print("\n".join(json.dumps(_trim(sample), indent=1).splitlines()[:48]))
    fills, trades = feed.drain_fills(), feed.drain_trades()
    print("== sample trade:", json.dumps(trades[0]) if trades else "none received")
    print("== sample fill (ours):", json.dumps(fills[0]) if fills else "none received")
    if rest:
        from sigbot.api import SuperMarketClient
        api = SuperMarketClient(key, base, tournament)
        for ex in held[:3]:
            fb, rb = feed.book(ex), api.orderbook(ex, depth=10)
            print(f"== REST compare {ex}: feed bid/ask {fb['bestBid']}/{fb['bestAsk']} asOf {fb['asOf']} "
                  f"(age {time.time() - fb['feed_ts']:.0f}s) | REST bid/ask {rb.get('bestBid')}/{rb.get('bestAsk')} asOf {rb.get('asOf')}")
    print("== stats:", json.dumps(feed.stats(), default=str))
    feed.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(description="sigbot realtime feed probe")
    ap.add_argument("--probe", type=int, default=90, help="seconds to stay connected")
    ap.add_argument("--base", default="https://sig.thesuper.market/api/v1")
    ap.add_argument("--tournament", default="bda92870-621e-47b0-bc3c-3602c5c26f55")
    ap.add_argument("--no-rest", action="store_true", help="skip the REST orderbook comparison (saves 3 reads)")
    a = ap.parse_args(argv)
    _probe(a.probe, a.base, a.tournament, rest=not a.no_rest)


if __name__ == "__main__":
    main(sys.argv[1:])
