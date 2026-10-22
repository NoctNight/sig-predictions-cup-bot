"""Tiny HTTP helper for the outside venues (stdlib urllib, TLS via certifi when available).

Every call returns (payload, latency_ms). Retries once on network errors / 5xx with a short backoff; 4xx raise
immediately. Each venue keeps its own circuit breaker (see VenueState): after `max_failures` consecutive failures the
venue is skipped for `cooloff_sec` so a dead endpoint cannot stall the bot cycle.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger("sigbot.venues")


class VenueError(Exception):
    pass


def _ctx() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover
        return ssl.create_default_context()


class Http:
    def __init__(self, user_agent: str = "sigbot-direction/0.1", timeout: float = 8.0):
        self.ua, self.timeout, self.ctx = user_agent, timeout, _ctx()

    def _do(self, method: str, url: str, body: Optional[bytes], retries: int, timeout: Optional[float]) -> tuple[Any, float]:
        headers = {"User-Agent": self.ua, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            t0 = time.time()
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self.ctx) as resp:
                    raw = resp.read()
                lat = (time.time() - t0) * 1000
                return (json.loads(raw) if raw else None), lat
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < max(retries, 4):          # rate limited: back off harder, always retry
                    attempt += 1
                    ra = e.headers.get("Retry-After")
                    time.sleep(float(ra) if ra and ra.replace(".", "", 1).isdigit() else 2.0 * (2 ** attempt))
                    continue
                if e.code in (500, 502, 503, 504) and attempt < retries:
                    attempt += 1
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise VenueError(f"{method} {url[:120]} -> HTTP {e.code}: {e.read()[:200]!r}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt < retries:
                    attempt += 1
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise VenueError(f"{method} {url[:120]} -> {e}")

    def get(self, url: str, params: Optional[dict] = None, retries: int = 1, timeout: Optional[float] = None) -> tuple[Any, float]:
        if params:
            params = {k: v for k, v in params.items() if v is not None}
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return self._do("GET", url, None, retries, timeout)

    def post(self, url: str, body: Any, retries: int = 1, timeout: Optional[float] = None) -> tuple[Any, float]:
        return self._do("POST", url, json.dumps(body).encode(), retries, timeout)


class VenueState:
    """Circuit breaker + latency bookkeeping for one venue."""

    def __init__(self, name: str, max_failures: int = 3, cooloff_sec: float = 300.0):
        self.name, self.max_failures, self.cooloff_sec = name, max_failures, cooloff_sec
        self.failures = 0
        self.skip_until = 0.0
        self.last_ok = 0.0
        self.last_latency_ms: Optional[float] = None
        self.latencies: list[float] = []
        self.calls = 0
        self.errors = 0

    def available(self) -> bool:
        return time.time() >= self.skip_until

    def ok(self, latency_ms: float):
        self.failures = 0
        self.last_ok = time.time()
        self.last_latency_ms = latency_ms
        self.calls += 1
        self.latencies.append(latency_ms)
        if len(self.latencies) > 200:
            self.latencies = self.latencies[-200:]

    def fail(self, err: Exception):
        self.failures += 1
        self.errors += 1
        self.calls += 1
        log.warning("%s: %s (failure %d)", self.name, err, self.failures)
        if self.failures >= self.max_failures:
            self.skip_until = time.time() + self.cooloff_sec
            log.warning("%s: %d consecutive failures, skipping for %.0fs", self.name, self.failures, self.cooloff_sec)

    def stats(self) -> dict:
        lat = sorted(self.latencies)
        return {"calls": self.calls, "errors": self.errors, "last_ms": round(self.last_latency_ms or 0),
                "p50_ms": round(lat[len(lat) // 2]) if lat else None, "p90_ms": round(lat[int(len(lat) * 0.9)]) if lat else None,
                "age_sec": round(time.time() - self.last_ok) if self.last_ok else None}
