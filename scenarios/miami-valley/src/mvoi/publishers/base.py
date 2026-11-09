"""Event publishers.

Publishers take wire-format dicts (from the outbox), stamp `ingested_at`, and
deliver them. `EventHubPublisher` targets a Fabric Eventstream *custom
endpoint* source, which exposes an Event Hubs-compatible connection string
(SAS key) — see
https://learn.microsoft.com/fabric/real-time-intelligence/event-streams/add-source-custom-app
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, TextIO

from mvoi.models.events import utcnow

log = logging.getLogger(__name__)

Payload = dict[str, Any]


class Publisher(Protocol):
    def publish(self, payloads: Sequence[Payload]) -> int:
        """Deliver all payloads or raise. Returns the number delivered."""
        ...

    def close(self) -> None: ...


def stamp(payloads: Sequence[Payload]) -> list[Payload]:
    now = utcnow().isoformat().replace("+00:00", "Z")
    return [{**p, "ingested_at": now} for p in payloads]


def dumps(p: Payload) -> str:
    return json.dumps(p, separators=(",", ":"), default=str)


class StdoutPublisher:
    def __init__(self, stream: TextIO | None = None):
        self._stream = stream or sys.stdout

    def publish(self, payloads: Sequence[Payload]) -> int:
        for p in stamp(payloads):
            self._stream.write(dumps(p) + "\n")
        self._stream.flush()
        return len(payloads)

    def close(self) -> None:
        pass


class FilePublisher:
    """Appends JSON Lines (handy for local runs and for building replay sets)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115

    def publish(self, payloads: Sequence[Payload]) -> int:
        for p in stamp(payloads):
            self._fh.write(dumps(p) + "\n")
        self._fh.flush()
        return len(payloads)

    def close(self) -> None:
        self._fh.close()


class MemoryPublisher:
    """Test double; set `fail_next` to simulate an outage."""

    def __init__(self):
        self.events: list[Payload] = []
        self.fail_next = 0

    def publish(self, payloads: Sequence[Payload]) -> int:
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("simulated publish failure")
        self.events.extend(stamp(payloads))
        return len(payloads)

    def close(self) -> None:
        pass


class EventHubPublisher:
    """Publishes to a Fabric Eventstream custom endpoint (Event Hubs protocol).

    * Ordering across partitions is NOT relied upon: every downstream table
      takes the latest state per entity_key by `observed_at`.
    * Application properties carry entity_type / run_mode / environment so
      Eventstream filters can route without parsing the body.
    * An event too large for a batch (e.g. a huge alert polygon) is sent
      without its lossless `raw` copy rather than dropped; `raw_omitted`
      marks it.
    """

    def __init__(self, connection_string: str, max_events_per_batch: int = 200):
        from azure.eventhub import EventHubProducerClient  # optional dependency

        self._client = EventHubProducerClient.from_connection_string(connection_string)
        self._max = max_events_per_batch

    def _event_data(self, p: Payload):
        from azure.eventhub import EventData

        d = EventData(dumps(p))
        d.properties = {"entity_type": p["entity_type"], "run_mode": p["run_mode"],
                        "environment": p["environment"], "change_type": p["change_type"]}
        return d

    def publish(self, payloads: Sequence[Payload]) -> int:
        sent = 0
        batch = self._client.create_batch()
        count = 0
        for p in stamp(payloads):
            data = self._event_data(p)
            try:
                batch.add(data)
                count += 1
            except ValueError:  # batch full
                if count:
                    self._client.send_batch(batch)
                    sent += count
                batch = self._client.create_batch()
                count = 0
                try:
                    batch.add(data)
                except ValueError:
                    slim = {**p, "raw": None,
                            "attributes": {**(p.get("attributes") or {}), "raw_omitted": True}}
                    log.warning("event too large; publishing without raw",
                                extra={"entity_key": p["entity_key"]})
                    batch.add(self._event_data(slim))
                count = 1
            if count >= self._max:
                self._client.send_batch(batch)
                sent += count
                batch = self._client.create_batch()
                count = 0
        if count:
            self._client.send_batch(batch)
            sent += count
        return sent

    def close(self) -> None:
        self._client.close()
