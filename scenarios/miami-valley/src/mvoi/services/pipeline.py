"""The hot-path pipeline shared by live polling and replay.

    poll results ─► lifecycle (dedup, created/updated/resolved)
                 ─► facility risk (snapshot on material change)
                 ─► operational alerts (raise/clear transitions)
                 ─► heartbeat (freshness + per-source health)
                 ─► outbox (SQLite, same process)  ─► publisher (Eventstream)

The outbox gives at-least-once delivery across publisher outages and process
restarts; event_ids are deterministic so downstream de-duplication is exact.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mvoi.common.config import LifecycleSettings, RiskConfig, RiskSettings
from mvoi.geo.reference import Facility
from mvoi.models.events import (
    TRANSPORT_TYPES,
    ChangeType,
    EntityType,
    NormalizedEvent,
    RunMode,
    Severity,
    Source,
    make_entity_key,
    utcnow,
)
from mvoi.publishers.base import Publisher
from mvoi.risk.alerts import AlertManager
from mvoi.risk.scoring import ActiveEvent, FacilityRisk, score_all
from mvoi.services.health import Metrics
from mvoi.services.lifecycle import LifecycleTracker
from mvoi.services.state import StateStore
from mvoi.sources.base import PollResult, SourceAdapter

log = logging.getLogger(__name__)

_BAND_SEVERITY = {"severe": Severity.CRITICAL, "high": Severity.MAJOR,
                  "elevated": Severity.MODERATE}
_KV_RISK = "risk.last"
_KV_SOURCE_HEALTH = "sources.health"


@dataclass
class CycleReport:
    observed_at: datetime
    polls: list[PollResult] = field(default_factory=list)
    lifecycle_events: int = 0
    risk_events: int = 0
    alert_events: int = 0
    published: int = 0
    publish_error: str | None = None


class HistoryBaseline:
    """Per-facility count of distinct historical incidents near the facility.

    Produced by nb_mvoi_gold_build (`gold_facility_history_baseline`) and
    exported to OneLake as Files/mvoi/exports/history_baseline.json.
    `source` may be a local path or an https://onelake.dfs.fabric.microsoft.com
    URL (read with the service's managed identity / az login). Missing or
    unreadable -> zeros, which only removes the history component; scoring
    never fails because of it.
    """

    REFRESH_SECONDS = 3600

    def __init__(self, source: str | Path | None):
        self.source = str(source) if source else None
        self._data: dict[str, int] = {}
        self._loaded_at = 0.0

    def get(self) -> Mapping[str, int]:
        import time

        if not self.source or time.time() - self._loaded_at < self.REFRESH_SECONDS and self._data:
            return self._data
        try:
            raw = json.loads(self._read())
            self._data = {k: int(v) for k, v in (raw.get("facilities") or raw).items()}
            self._loaded_at = time.time()
        except Exception as exc:  # noqa: BLE001
            log.warning("history baseline unavailable; history component = 0",
                        extra={"source": self.source, "error": exc.__class__.__name__})
            self._loaded_at = time.time()  # don't hammer a missing source
        return self._data

    def _read(self) -> str:
        assert self.source
        if self.source.startswith("https://"):
            import httpx
            from azure.identity import DefaultAzureCredential

            token = DefaultAzureCredential().get_token("https://storage.azure.com/.default").token
            r = httpx.get(self.source, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            r.raise_for_status()
            return r.text
        return Path(self.source).read_text(encoding="utf-8")


class Pipeline:
    def __init__(
        self,
        *,
        adapters: Mapping[str, SourceAdapter],
        intervals: Mapping[str, int],
        store: StateStore,
        lifecycle: LifecycleSettings,
        publisher: Publisher,
        facilities: list[Facility],
        risk_cfg: RiskConfig,
        risk_settings: RiskSettings,
        environment: str,
        run_mode: RunMode = RunMode.LIVE,
        replay_session_id: str | None = None,
        history: HistoryBaseline | None = None,
        metrics: Metrics | None = None,
        batch_max_events: int = 200,
        publish_retry_initial_seconds: float = 30,
        publish_retry_max_seconds: float = 300,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.adapters = dict(adapters)
        self.intervals = dict(intervals)
        self.store = store
        self.tracker = LifecycleTracker(store, lifecycle, environment=environment,
                                        run_mode=run_mode, replay_session_id=replay_session_id)
        self.publisher = publisher
        self.facilities = facilities
        self.risk_cfg = risk_cfg
        self.risk_settings = risk_settings
        self.environment = environment
        self.run_mode = RunMode(run_mode)
        self.replay_session_id = replay_session_id
        self.history = history or HistoryBaseline(None)
        self.metrics = metrics or Metrics()
        self.batch_max = batch_max_events
        self.clock = clock
        self.alerts = AlertManager(store, risk_cfg, environment, str(self.run_mode), replay_session_id)
        self._last_polled: dict[str, datetime] = {}
        # While publishing fails, the outbox is retried on a backoff rather than on
        # every tick: each failed attempt blocks for the SDK's retries (~40 s).
        self._retry_initial = publish_retry_initial_seconds
        self._retry_max = publish_retry_max_seconds
        self._retry_delay = 0.0
        self._retry_at: datetime | None = None
        self._publish_error: str | None = None

    # ------------------------------------------------------------------ live loop

    def due_adapters(self, now: datetime) -> list[str]:
        due = []
        for name in self.adapters:
            last = self._last_polled.get(name)
            if last is None or (now - last).total_seconds() >= self.intervals.get(name, 60) - 1:
                due.append(name)
        return due

    def run_cycle(self) -> CycleReport:
        now = self.clock()
        polls: list[PollResult] = []
        for name in self.due_adapters(now):
            polls.extend(self.adapters[name].poll(now))
            self._last_polled[name] = now
        return self.process(polls, now)

    def run_forever(self, stop: threading.Event, tick_seconds: float = 5) -> None:
        log.info("pipeline started", extra={"run_mode": str(self.run_mode),
                                            "adapters": list(self.adapters)})
        while not stop.is_set():
            if self.due_adapters(self.clock()):
                try:
                    self.run_cycle()
                except Exception:  # noqa: BLE001 — keep the service alive
                    log.exception("cycle failed")
                    with self.metrics.lock:
                        self.metrics.cycle_failures += 1
                        self.metrics.last_cycle_ok = False
            else:
                self.flush()  # retry any backlog between polls
            stop.wait(tick_seconds)
        self.flush()
        log.info("pipeline stopped")

    # ------------------------------------------------------------------ processing

    def process(self, polls: list[PollResult], now: datetime) -> CycleReport:
        """Process a set of poll results observed at `now` (live or replay)."""
        report = CycleReport(observed_at=now, polls=polls)
        events: list[NormalizedEvent] = []
        per_poll: list[dict[str, Any]] = []
        for poll in polls:
            outcome = self.tracker.apply(poll)
            events.extend(outcome.events)
            per_poll.append(self._poll_stats(poll, len(outcome.events), outcome.unchanged,
                                             outcome.suppressed_resolution))
        report.lifecycle_events = len(events)

        transport_changed = any(e.entity_type in {t.value for t in TRANSPORT_TYPES}
                                or e.entity_type == EntityType.WEATHER_ALERT.value for e in events)
        if self.risk_settings.enabled and (polls or transport_changed):
            active = [ActiveEvent.from_event(s.last_event) for s in self.store.active()
                      if s.entity_type != EntityType.OPERATIONAL_ALERT.value]
            risks = score_all(self.facilities, active, self.risk_cfg, now, self.history.get())
            risk_events = self._risk_events(risks, now)
            alert_events = self.alerts.evaluate(now, self.facilities, risks, active)
            report.risk_events, report.alert_events = len(risk_events), len(alert_events)
            events.extend(risk_events)
            events.extend(alert_events)

        events.append(self._heartbeat(now, per_poll))
        with self.store.transaction():
            self.store.enqueue([e.to_wire() for e in events], now)
        report.published, report.publish_error = self.flush()

        with self.metrics.lock:
            self.metrics.cycles += 1
            self.metrics.last_cycle_at = now
            self.metrics.last_cycle_ok = True
            self.metrics.last_publish_ok = report.publish_error is None
            for s in per_poll:
                self.metrics.sources[f"{s['source']}:{s['entity_type']}"] = s
        log.info("cycle complete", extra={
            "observed_at": now.isoformat(), "polls": len(polls),
            "lifecycle_events": report.lifecycle_events, "risk_events": report.risk_events,
            "alert_events": report.alert_events, "published": report.published,
            "outbox_depth": self.metrics.outbox_depth})
        return report

    def flush(self) -> tuple[int, str | None]:
        total = 0
        error = None
        now = self.clock()
        if self._retry_at is not None and now < self._retry_at:
            with self.metrics.lock:
                self.metrics.outbox_depth = self.store.outbox_depth()
            return 0, self._publish_error
        while True:
            batch = self.store.peek_outbox(self.batch_max)
            if not batch:
                break
            seqs = [s for s, _ in batch]
            try:
                self.publisher.publish([p for _, p in batch])
            except Exception as exc:  # noqa: BLE001
                self.store.nack_outbox(seqs)
                error = f"{exc.__class__.__name__}: {exc}"
                self._retry_delay = min(self._retry_max, self._retry_delay * 2 or self._retry_initial)
                self._retry_at = now + timedelta(seconds=self._retry_delay)
                self._publish_error = error
                log.error("publish failed; events retained in outbox",
                          extra={"count": len(seqs), "error": error, "retry_in_s": self._retry_delay})
                with self.metrics.lock:
                    self.metrics.publish_failures += 1
                break
            self.store.ack_outbox(seqs)
            total += len(seqs)
            self._retry_delay, self._retry_at, self._publish_error = 0.0, None, None
        with self.metrics.lock:
            self.metrics.events_published += total
            self.metrics.outbox_depth = self.store.outbox_depth()
            self.metrics.publish_retry_at = self._retry_at
        return total, error

    # ------------------------------------------------------------------ derived events

    def _risk_events(self, risks: list[FacilityRisk], now: datetime) -> list[NormalizedEvent]:
        last: dict[str, dict[str, Any]] = self.store.get_kv(_KV_RISK, {})
        out = []
        fac = {f.facility_id: f for f in self.facilities}
        for r in risks:
            prev = last.get(r.facility_id)
            changed = (prev is None or prev["band"] != r.band
                       or abs(prev["score"] - r.score) >= self.risk_settings.min_score_delta)
            if not (changed or self.risk_settings.emit_every_cycle):
                continue
            f = fac[r.facility_id]
            d = r.to_dict()
            d["contributors"] = d["contributors"][:5]
            d["previous_score"] = prev["score"] if prev else None
            d["previous_band"] = prev["band"] if prev else None
            out.append(NormalizedEvent(
                entity_key=make_entity_key(Source.MVOI, EntityType.FACILITY_RISK, r.facility_id),
                source=Source.MVOI, entity_type=EntityType.FACILITY_RISK, source_id=r.facility_id,
                change_type=ChangeType.SNAPSHOT, environment=self.environment,
                run_mode=self.run_mode, replay_session_id=self.replay_session_id,
                observed_at=now, latitude=f.latitude, longitude=f.longitude,
                corridor_id=f.primary_corridor, location=f.city, category=f.facility_type,
                status=r.band, severity=_BAND_SEVERITY.get(r.band, Severity.MINOR),
                title=f.facility_name, description=r.explanation, attributes=d,
            ).finalize())
            last[r.facility_id] = {"score": r.score, "band": r.band}
        self.store.set_kv(_KV_RISK, last)
        return out

    def _poll_stats(self, poll: PollResult, emitted: int, unchanged: int,
                    suppressed: bool) -> dict[str, Any]:
        health: dict[str, Any] = self.store.get_kv(_KV_SOURCE_HEALTH, {})
        key = f"{poll.source}:{poll.entity_type}"
        h = health.get(key, {})
        if poll.ok:
            h["last_success_at"] = poll.observed_at.isoformat()
            h["consecutive_failures"] = 0
        else:
            h["last_failure_at"] = poll.observed_at.isoformat()
            h["consecutive_failures"] = h.get("consecutive_failures", 0) + 1
        health[key] = h
        self.store.set_kv(_KV_SOURCE_HEALTH, health)
        return {
            "source": poll.source, "entity_type": str(poll.entity_type), "ok": poll.ok,
            "not_modified": poll.not_modified, "observations": len(poll.observations),
            "rejected": len(poll.rejected), "events_emitted": emitted, "unchanged": unchanged,
            "resolution_suppressed": suppressed, "total_reported": poll.total_reported,
            "elapsed_ms": poll.elapsed_ms, "attempts": poll.attempts, "error": poll.error,
            "source_updated_at": poll.source_updated_at.isoformat() if poll.source_updated_at else None,
            "last_success_at": h.get("last_success_at"),
            "consecutive_failures": h.get("consecutive_failures", 0),
        }

    def _heartbeat(self, now: datetime, per_poll: list[dict[str, Any]]) -> NormalizedEvent:
        healthy = all(p["ok"] for p in per_poll)
        sid = "poller" if self.run_mode == RunMode.LIVE else f"replay-{self.replay_session_id}"
        active_counts: dict[str, int] = {}
        # Every tracked type is listed, even with no active keys, so KQL can hide
        # rows the poller no longer tracks (ADR 0005).
        active_keys: dict[str, list[str]] = {t: [] for t in self.store.entity_types()}
        for s in self.store.active():
            active_counts[s.entity_type] = active_counts.get(s.entity_type, 0) + 1
            active_keys.setdefault(s.entity_type, []).append(s.entity_key)
        for keys in active_keys.values():
            keys.sort()
        return NormalizedEvent(
            entity_key=make_entity_key(Source.MVOI, EntityType.INGESTION_HEARTBEAT, sid),
            source=Source.MVOI, entity_type=EntityType.INGESTION_HEARTBEAT, source_id=sid,
            change_type=ChangeType.SNAPSHOT, environment=self.environment,
            run_mode=self.run_mode, replay_session_id=self.replay_session_id,
            observed_at=now, status="ok" if healthy else "degraded",
            title="ingestion heartbeat",
            attributes={"polls": per_poll, "active_counts": active_counts, "active_keys": active_keys,
                        "outbox_depth": self.store.outbox_depth(),
                        "service_started_at": self.metrics.started_at.isoformat()},
        ).finalize()
