"""Replay player: drives recorded source responses through the real pipeline.

Time mapping: every recorded instant t maps to
    wall(t) = session_start + (t - recording_start) / speed
That covers poll times *and* timestamps inside payloads (OHGO
lastUpdated/startDate/endDate, NWS sent/effective/onset/expires/ends), so
"last 15 minutes" tiles, expiry, and lifecycle resolution all behave as they
did when recorded, just faster. Lifecycle time thresholds are divided by
`speed` for the same reason.

Isolation: events carry run_mode="replay" and replay_session_id; each session
has its own state DB, so replay never touches live lifecycle state. Silver and
Gold exclude run_mode="replay" (see fabric/notebooks), and dashboards filter
on run_mode.
"""

from __future__ import annotations

import copy
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mvoi.common.config import LifecycleSettings
from mvoi.models.events import ChangeType, NormalizedEvent, RunMode, utcnow
from mvoi.replay.recording import Manifest, group_frames, read_recording
from mvoi.services.pipeline import Pipeline
from mvoi.services.state import StateStore
from mvoi.sources.base import PollResult, SourceAdapter

log = logging.getLogger(__name__)

TIME_KEYS = {"lastUpdated", "startDate", "endDate", "updated", "sent", "effective", "onset",
             "expires", "ends"}


def new_session_id() -> str:
    return utcnow().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def session_state_path(state_dir: str | Path, session_id: str) -> Path:
    return Path(state_dir) / f"replay-{session_id}.sqlite"


def scaled_lifecycle(base: LifecycleSettings, speed: float) -> LifecycleSettings:
    return base.model_copy(update={"resolve_after_minutes": base.resolve_after_minutes / speed})


@dataclass
class TimeMap:
    recording_start: datetime
    session_start: datetime
    speed: float

    def __call__(self, t: datetime) -> datetime:
        return self.session_start + (t - self.recording_start) / self.speed


def shift_times(obj: Any, tmap: TimeMap) -> Any:
    """Deep-copy `obj`, remapping ISO timestamps under known keys."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in TIME_KEYS and isinstance(v, str):
                out[k] = _shift_str(v, tmap)
            else:
                out[k] = shift_times(v, tmap)
        return out
    if isinstance(obj, list):
        return [shift_times(v, tmap) for v in obj]
    return obj


def _shift_str(value: str, tmap: TimeMap) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        from datetime import UTC
        dt = dt.replace(tzinfo=UTC)
    return tmap(dt).isoformat().replace("+00:00", "Z")


class ReplayPlayer:
    def __init__(
        self,
        recording_path: str | Path,
        pipeline_factory: Callable[[str, float], Pipeline],
        adapters: dict[str, SourceAdapter],
        speed: float = 10.0,
        session_id: str | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], bool] | None = None,
    ):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.manifest, self.frames = read_recording(recording_path)
        if not self.frames:
            raise ValueError("recording has no frames")
        self.speed = speed
        self.session_id = session_id or new_session_id()
        self.adapters = adapters
        self.clock = clock
        self.stop_event = threading.Event()
        self._sleep = sleep or (lambda s: self.stop_event.wait(s))
        self.pipeline = pipeline_factory(self.session_id, speed)

    @property
    def info(self) -> Manifest:
        return self.manifest

    def run(self) -> int:
        """Play to the end (or until stopped). Returns number of cycles processed."""
        start = self.clock()
        tmap = TimeMap(self.frames[0].t, start, self.speed)
        log.info("replay started", extra={"session_id": self.session_id, "speed": self.speed,
                                          "recording": self.manifest.name,
                                          "synthetic": self.manifest.synthetic,
                                          "frames": len(self.frames)})
        cycles = 0
        for t, frames in group_frames(self.frames):
            target = tmap(t)
            wait = (target - self.clock()).total_seconds()
            if wait > 0 and self._sleep(wait):
                break  # stop requested
            if self.stop_event.is_set():
                break
            polls = [self._parse(f.source, f.resource, shift_times(f.envelope, tmap), target)
                     for f in frames]
            self.pipeline.process([p for p in polls if p is not None], target)
            cycles += 1
        self.pipeline.flush()
        log.info("replay finished", extra={"session_id": self.session_id, "cycles": cycles})
        return cycles

    def stop(self) -> None:
        self.stop_event.set()

    def _parse(self, source: str, resource: str, envelope: dict[str, Any],
               observed_at: datetime) -> PollResult | None:
        adapter = self.adapters.get(source)
        if adapter is None:
            log.warning("no adapter for recorded source", extra={"source": source})
            return None
        result = adapter.parse_envelope(resource, copy.deepcopy(envelope), observed_at)
        for obs in result.observations:
            obs.event.attributes["synthetic"] = self.manifest.synthetic
        return result


def reset_session(store: StateStore, pipeline: Pipeline) -> int:
    """Resolve everything still active in a replay session, then wipe its state.

    Publishing explicit `resolved` events (rather than silently deleting)
    clears the session from the live dashboards through the normal path.
    """
    now = pipeline.clock()
    events: list[NormalizedEvent] = []
    for s in store.active():
        data = dict(s.last_event)
        data.update(change_type=ChangeType.RESOLVED, observed_at=now, event_id="",
                    run_mode=RunMode.REPLAY, replay_session_id=pipeline.replay_session_id)
        data["attributes"] = {**(data.get("attributes") or {}), "resolution_reason": "replay_reset"}
        if data.get("entity_type") == "operational_alert":
            data["status"] = "cleared"
            data["attributes"]["alert_state"] = "cleared"
        events.append(NormalizedEvent.model_validate(data).finalize())
    with store.transaction():
        store.enqueue([e.to_wire() for e in events], now)
    _, error = pipeline.flush()
    if error or store.outbox_depth():
        raise RuntimeError(f"reset aborted: could not publish resolution events ({error}); "
                           "state kept so the reset can be retried")
    store.reset()
    return len(events)
