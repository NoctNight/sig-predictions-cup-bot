"""Thin client for the Super Market API (sig.thesuper.market/api/v1).

Stdlib only (urllib + sqlite). Handles:
  * bearer auth, TLS via certifi when available
  * per-account rate limits: 100 reads/min, 30 writes/min (we budget 90/27)
  * retries with Retry-After / exponential backoff on 429 / 503 / 502
  * idempotency keys on every order write
"""
from __future__ import annotations

import json
import logging
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.client
import io
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("sigbot.api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None, retry_after: Optional[float] = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.details, self.retry_after = status, code, message, details, retry_after


class TokenBucket:
    """Simple per-minute budget. Blocks until a token is available."""

    def __init__(self, per_minute: int):
        self.capacity = per_minute
        self.tokens = float(per_minute)
        self.rate = per_minute / 60.0
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover
        return ssl.create_default_context()


@dataclass
class SuperMarketClient:
    api_key: str
    base_url: str = "https://sig.thesuper.market/api/v1"
    tournament_id: Optional[str] = None
    reads_per_min: int = 80
    writes_per_min: int = 27
    timeout: float = 30.0
    max_retries: int = 5
    _reads: TokenBucket = field(init=False)
    _writes: TokenBucket = field(init=False)
    _ctx: ssl.SSLContext = field(init=False)
    stats: dict = field(default_factory=lambda: {"reads": 0, "writes": 0, "retries": 0, "errors": 0})

    def __post_init__(self):
        self._reads = TokenBucket(self.reads_per_min)
        self._writes = TokenBucket(self.writes_per_min)
        self._ctx = _ssl_context()
        self._local = threading.local()     # one kept-alive HTTPS connection per thread (http.client is not thread-safe)

    def _conn(self, host: str, fresh: bool = False) -> http.client.HTTPSConnection:
        c = getattr(self._local, "conn", None)
        if fresh or c is None or getattr(self._local, "host", None) != host:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
            c = http.client.HTTPSConnection(host, context=self._ctx, timeout=self.timeout)
            self._local.conn, self._local.host = c, host
        return c

    def _send(self, method: str, url: str, data: Optional[bytes], headers: dict) -> bytes:
        """One HTTP exchange on the thread's kept-alive connection (saves the TCP+TLS handshake, ~50 ms here). A stale
        connection (closed by the server while idle) is retried once at once on a fresh one; every request is safe to
        resend (orders carry idempotency keys). HTTP errors are raised as urllib HTTPError so the retry logic is unchanged."""
        parts = urllib.parse.urlsplit(url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        for attempt in (0, 1):
            c = self._conn(parts.netloc, fresh=attempt > 0)
            try:
                c.request(method, path, body=data, headers=dict(headers, Connection="keep-alive"))
                resp = c.getresponse()
                raw = resp.read()
                if resp.getheader("Connection", "").lower() == "close":
                    self._conn(parts.netloc, fresh=True)
                if resp.status >= 400:
                    raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, io.BytesIO(raw))
                return raw
            except urllib.error.HTTPError:
                raise
            except (http.client.RemoteDisconnected, http.client.BadStatusLine, BrokenPipeError, ConnectionResetError,
                    http.client.CannotSendRequest, http.client.ResponseNotReady) as e:
                self._conn(parts.netloc, fresh=True)
                if attempt == 0:
                    continue                    # stale kept-alive connection: resend once on a new one
                raise ConnectionError(str(e))
            except socket.timeout as e:
                self._conn(parts.netloc, fresh=True)
                raise TimeoutError(str(e))
            except OSError as e:                # DNS failure, connection refused, TLS errors
                self._conn(parts.netloc, fresh=True)
                raise urllib.error.URLError(e)
        raise ConnectionError("unreachable")

    # ------------------------------------------------------------------ core
    def _request(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None) -> Any:
        url = self.base_url + path
        if params:
            params = {k: v for k, v in params.items() if v is not None}
            if params:
                url += "?" + urllib.parse.urlencode(params)
        is_write = method not in ("GET", "HEAD")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            (self._writes if is_write else self._reads).take()
            self.stats["writes" if is_write else "reads"] += 1
            try:
                raw = self._send(method, url, data, headers)
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    payload = json.loads(raw) if raw else {}
                except Exception:
                    payload = {"raw": raw.decode(errors="replace")}
                err = payload.get("error") if isinstance(payload, dict) else None
                code = (err or {}).get("code", "HTTP_ERROR") if isinstance(err, dict) else "HTTP_ERROR"
                msg = (err or {}).get("message", str(payload)[:300]) if isinstance(err, dict) else str(payload)[:300]
                details = (err or {}).get("details") if isinstance(err, dict) else None
                ra = e.headers.get("Retry-After")
                retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
                # 207 is success-with-failures for batch; urllib raises only for >=400
                retryable = e.code in (429, 502, 503) or code in ("REQUEST_IN_FLIGHT",)
                if e.code == 207:
                    return payload
                if retryable and attempt < self.max_retries:
                    attempt += 1
                    self.stats["retries"] += 1
                    wait = retry_after if retry_after is not None else min(60, (2 ** attempt) + random.random())
                    if code == "REQUEST_IN_FLIGHT":
                        wait = max(wait, 90)
                    log.warning("%s %s -> %s %s; retry %d in %.1fs", method, path, e.code, code, attempt, wait)
                    time.sleep(wait)
                    continue
                self.stats["errors"] += 1
                # batch endpoints return 207/422/429/503 with per-item results; surface payload
                raise ApiError(e.code, code, msg, details if details is not None else payload, retry_after)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.max_retries:
                    attempt += 1
                    self.stats["retries"] += 1
                    wait = min(60, (2 ** attempt) + random.random())
                    log.warning("%s %s network error %s; retry %d in %.1fs", method, path, e, attempt, wait)
                    time.sleep(wait)
                    continue
                self.stats["errors"] += 1
                raise

    def get(self, path: str, **params) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: Optional[dict] = None, **params) -> Any:
        return self._request("POST", path, params=params, body=body)

    def delete(self, path: str, **params) -> Any:
        return self._request("DELETE", path, params=params)

    # --------------------------------------------------------------- helpers
    def _tid(self, tournament_id: Optional[str]) -> Optional[str]:
        return tournament_id or self.tournament_id

    def account(self, tournament_id: Optional[str] = None) -> dict:
        return self.get("/account", tournamentId=self._tid(tournament_id))

    def tournaments(self) -> list:
        return self.get("/tournaments", limit=100)["data"]

    def tournament(self, slug: str) -> dict:
        return self.get(f"/tournaments/{slug}")

    def markets(self, tournament_id: Optional[str] = None, status: str = "any") -> list:
        out, cursor = [], None
        while True:
            page = self.get("/markets", limit=100, cursor=cursor, tournamentId=self._tid(tournament_id), status=status)
            out.extend(page["data"])
            if not page["pagination"].get("hasMore"):
                return out
            cursor = page["pagination"]["nextCursor"]

    def market(self, market_id: str, tournament_id: Optional[str] = None) -> dict:
        return self.get(f"/markets/{market_id}", tournamentId=self._tid(tournament_id))

    def prices(self, exchange_ids: list[str], tournament_id: Optional[str] = None) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(exchange_ids), 100):
            chunk = exchange_ids[i : i + 100]
            res = self.get("/exchanges/prices", ids=",".join(chunk), tournamentId=self._tid(tournament_id))
            for row in res["data"]:
                out[row["exchangeId"]] = row
        return out

    def orderbook(self, exchange_id: str, depth: int = 50, tournament_id: Optional[str] = None) -> dict:
        return self.get(f"/exchanges/{exchange_id}/orderbook", depth=depth, tournamentId=self._tid(tournament_id))

    def trades(self, exchange_id: str, limit: int = 200, cursor: Optional[str] = None, since: Optional[str] = None,
               tournament_id: Optional[str] = None) -> dict:
        return self.get(f"/exchanges/{exchange_id}/trades", limit=limit, cursor=cursor, tournamentId=self._tid(tournament_id),
                        **({"from": since} if since else {}))

    def price_history(self, exchange_id: str, resolution: str = "1h", limit: int = 200, tournament_id: Optional[str] = None) -> dict:
        return self.get(f"/exchanges/{exchange_id}/price-history", resolution=resolution, limit=limit,
                        tournamentId=self._tid(tournament_id))

    def positions(self, slug: str) -> dict:
        return self.get(f"/tournaments/{slug}/portfolio/positions")

    def pnl(self, slug: str, period: str = "all") -> dict:
        return self.get(f"/tournaments/{slug}/portfolio/pnl", period=period)

    def fills(self, slug: str, limit: int = 200, cursor: Optional[str] = None) -> dict:
        return self.get(f"/tournaments/{slug}/portfolio/fills", limit=limit, cursor=cursor)

    def open_orders(self, tournament_id: Optional[str] = None, exchange_id: Optional[str] = None) -> list:
        out, cursor = [], None
        while True:
            page = self.get("/orders", status="open", limit=200, cursor=cursor, tournamentId=self._tid(tournament_id),
                            exchangeId=exchange_id)
            out.extend(page["data"])
            if not page["pagination"].get("hasMore"):
                return out
            cursor = page["pagination"]["nextCursor"]

    # ---------------------------------------------------------------- writes
    @staticmethod
    def new_key(prefix: str = "sb") -> str:
        return f"{prefix}-{uuid.uuid4()}"

    def place_order(self, exchange_id: str, side: str, action: str, quantity: int, price: Optional[float],
                    expiration: Optional[str] = None, tournament_id: Optional[str] = None, key: Optional[str] = None) -> dict:
        body = {"idempotencyKey": key or self.new_key("ord"), "exchangeId": str(exchange_id), "side": side, "action": action,
                "quantity": int(quantity), "tournamentId": self._tid(tournament_id)}
        if price is not None:
            body["price"] = round(price, 3)
        if expiration:
            body["expirationDate"] = expiration
        return self.post("/orders", body)

    def place_batch(self, orders: list[dict], key: Optional[str] = None, max_rounds: int = 6) -> dict:
        """orders: dicts with exchangeId, side, action, quantity, price?, expirationDate?, tournamentId?
        The engine throttles serial placement inside a batch (per-item 429 RATE_LIMITED / 503). Retrying the same
        idempotency key replays terminal items and resumes only the throttled ones, so we loop until no item is
        transient, honouring Retry-After."""
        key = key or self.new_key("bat")
        body = {"idempotencyKey": key, "orders": orders[:50]}
        last: dict = {"results": []}
        for rnd in range(max_rounds):
            try:
                resp = self.post("/orders/batch", body)
                status, retry_after = 200, None
            except ApiError as e:
                if not (isinstance(e.details, dict) and "results" in e.details):
                    raise
                resp = {"results": e.details["results"]}
                status, retry_after = e.status, e.retry_after
            last = resp
            transient = [r for r in resp.get("results", []) if not r.get("ok") and (r.get("status") in (429, 503) or
                         (isinstance(r.get("data"), dict) and r["data"].get("code") in ("RATE_LIMITED", "SERVICE_UNAVAILABLE")))]
            if not transient or status in (200, 422):
                return resp
            wait = (retry_after if retry_after else 2.0) * (1 + 0.5 * rnd) + random.random()
            log.info("batch %s: %d throttled items, retrying same key in %.1fs", key[-8:], len(transient), wait)
            time.sleep(wait)
        return last

    def place_multi_leg(self, legs: list[dict], all_or_none: bool = True, key: Optional[str] = None) -> dict:
        body = {"idempotencyKey": key or self.new_key("ml"), "legs": legs[:10]}
        if all_or_none:
            body["executionMode"] = "allOrNone"
        return self.post("/orders/multi-leg", body)

    def cancel_order(self, order_id: int) -> dict:
        return self.delete(f"/orders/{order_id}")

    def cancel_all(self, exchange_id: Optional[str] = None, market_id: Optional[str] = None,
                   tournament_id: Optional[str] = None) -> dict:
        body: dict = {"tournamentId": self._tid(tournament_id)}
        if exchange_id:
            body["exchangeId"] = str(exchange_id)
        elif market_id:
            body["marketId"] = str(market_id)
        for attempt in range(6):
            try:
                return self.post("/orders/cancel-all", body)
            except ApiError as e:
                if e.status == 503 and attempt < 5:
                    time.sleep(e.retry_after or (2 ** attempt))
                    continue
                raise
        return {"cancelled": 0}
