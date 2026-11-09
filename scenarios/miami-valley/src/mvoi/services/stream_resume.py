"""Resumes Eventstream destinations left paused by the nightly capacity suspend.

When the Fabric capacity suspends, the Eventhouse destination of
`es_mvoi_events` drops to Paused and stays paused after the capacity resumes,
so events pile up in the Eventstream and never reach `Events`. The poller and
the custom-endpoint source recover on their own; the destination does not.

`mvoi resume-stream` runs as a scheduled Container Apps job (main.bicep). It
resumes every paused destination from where it stopped (`WhenLastStopped`), so
the queued events backfill, and exits quickly when everything is Running.
While the capacity is still suspended the Fabric calls fail; the command
retries until `wait_minutes` runs out.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

RUNNING = "Running"
TRANSITIONAL = {"Resuming", "Starting", "Pausing"}


def destination_statuses(client, ws: str, es_id: str) -> dict[str, dict]:
    topo = client.request("GET", f"/workspaces/{ws}/eventstreams/{es_id}/topology")
    return {d["name"]: d for d in topo.get("destinations", [])}


def resume_paused(client, ws: str, es_id: str) -> list[str]:
    """Resumes paused destinations. Returns the names of destinations not yet Running."""
    pending = []
    for name, d in destination_statuses(client, ws, es_id).items():
        status = d.get("status")
        if status == RUNNING:
            continue
        pending.append(name)
        if status in TRANSITIONAL:
            log.info("destination %s is %s; waiting", name, status)
            continue
        log.warning("destination %s is %s; resuming from where it stopped", name, status)
        client.request("POST", f"/workspaces/{ws}/eventstreams/{es_id}/destinations/{d['id']}/resume",
                       {"startType": "WhenLastStopped"})
    return pending


def run(client, config_path: Path, *, wait_minutes: float = 30, poll_seconds: float = 30,
        sleep=time.sleep, clock=time.monotonic) -> bool:
    """Returns True once every destination is Running, False if it ran out of time."""
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    deadline = clock() + wait_minutes * 60
    while True:
        try:
            ws = client.workspace_id(cfg["workspace_name"])
            es = client.find_item(ws, "Eventstream", cfg["items"]["eventstream"])
            if es is None:
                raise RuntimeError(f"Eventstream '{cfg['items']['eventstream']}' not found")
            pending = resume_paused(client, ws, es["id"])
            if not pending:
                log.info("all Eventstream destinations running")
                return True
        except Exception as exc:  # capacity still suspended, throttling, token refresh, ...
            log.warning("Eventstream check failed (capacity may be suspended): %s", exc)
        if clock() >= deadline:
            log.error("gave up after %.0f min; destinations still not running", wait_minutes)
            return False
        sleep(poll_seconds)
