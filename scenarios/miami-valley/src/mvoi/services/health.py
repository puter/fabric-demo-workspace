"""Health and metrics endpoint (stdlib only).

GET /healthz  -> 200 when the last cycle completed recently, else 503
                (used by the Container Apps liveness/readiness probes).
                A failed publish does not fail it; see /metrics last_publish_ok.
GET /metrics  -> JSON counters and per-source freshness
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mvoi.models.events import utcnow


@dataclass
class Metrics:
    started_at: datetime = field(default_factory=utcnow)
    cycles: int = 0
    cycle_failures: int = 0
    events_published: int = 0
    publish_failures: int = 0
    outbox_depth: int = 0
    last_cycle_at: datetime | None = None
    last_cycle_ok: bool = False
    # Publishing is tracked apart from liveness: an Eventstream outage (e.g. the
    # nightly capacity suspend) is handled by the outbox, and a restart cannot fix it.
    last_publish_ok: bool = True
    publish_retry_at: datetime | None = None
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "started_at": self.started_at.isoformat(),
                "cycles": self.cycles,
                "cycle_failures": self.cycle_failures,
                "events_published": self.events_published,
                "publish_failures": self.publish_failures,
                "outbox_depth": self.outbox_depth,
                "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
                "last_cycle_ok": self.last_cycle_ok,
                "last_publish_ok": self.last_publish_ok,
                "publish_retry_at": self.publish_retry_at.isoformat() if self.publish_retry_at else None,
                "sources": self.sources,
            }

    def healthy(self, max_age: timedelta) -> bool:
        with self.lock:
            if self.last_cycle_at is None:
                return utcnow() - self.started_at < max_age  # still starting
            return self.last_cycle_ok and utcnow() - self.last_cycle_at < max_age


def serve(metrics: Metrics, port: int, max_age: timedelta) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/healthz"):
                ok = metrics.healthy(max_age)
                self._send(200 if ok else 503, {"healthy": ok})
            elif self.path.startswith("/metrics"):
                self._send(200, metrics.snapshot())
            else:
                self._send(404, {"error": "not found"})

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # silence default access log
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    return server
