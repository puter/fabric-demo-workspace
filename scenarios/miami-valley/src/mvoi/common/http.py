"""Resilient HTTP GET with retry, exponential backoff + jitter, Retry-After
handling, and ETag conditional requests."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class FetchResult:
    status_code: int
    data: Any | None            # parsed JSON; None on 304
    not_modified: bool
    etag: str | None
    elapsed_ms: float
    attempts: int
    headers: dict[str, str] = field(default_factory=dict)


class ResilientClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        backoff_max: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = client
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self._etags: dict[str, str] = {}

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_etag: bool = False,
        cache_key: str | None = None,
    ) -> FetchResult:
        key = cache_key or url
        req_headers = dict(headers or {})
        if use_etag and key in self._etags:
            req_headers["If-None-Match"] = self._etags[key]

        attempt = 0
        start = time.monotonic()
        while True:
            attempt += 1
            try:
                resp = self._client.get(url, params=params, headers=req_headers)
            except httpx.TransportError as exc:
                if attempt > self.max_retries:
                    raise FetchError(f"transport error after {attempt} attempts: {exc}") from exc
                self._backoff(attempt, None, url, f"transport: {exc.__class__.__name__}")
                continue

            if resp.status_code == 304:
                return FetchResult(304, None, True, self._etags.get(key),
                                   _ms(start), attempt, dict(resp.headers))
            if resp.status_code in RETRYABLE_STATUS:
                if attempt > self.max_retries:
                    raise FetchError(f"HTTP {resp.status_code} after {attempt} attempts",
                                     resp.status_code)
                self._backoff(attempt, resp.headers.get("Retry-After"), url,
                              f"HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                # 400/401/403/404 are not retryable: configuration or auth problem.
                raise FetchError(f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code)

            etag = resp.headers.get("ETag")
            if use_etag and etag:
                self._etags[key] = etag
            return FetchResult(resp.status_code, resp.json(), False, etag,
                               _ms(start), attempt, dict(resp.headers))

    def _backoff(self, attempt: int, retry_after: str | None, url: str, reason: str) -> None:
        delay = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = min(self.backoff_max, self.backoff_base * 2 ** (attempt - 1))
            delay = delay * (0.5 + random.random() / 2)  # jitter
        delay = min(delay, self.backoff_max)
        log.warning("retrying request", extra={"url": url, "attempt": attempt,
                                               "reason": reason, "delay_s": round(delay, 2)})
        self._sleep(delay)


def _ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 1)
