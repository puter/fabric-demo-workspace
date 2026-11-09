from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from mvoi.models.events import EntityType, NormalizedEvent


@dataclass
class Observation:
    """One entity as seen in one poll, before lifecycle (created/updated/...) is decided."""

    event: NormalizedEvent          # change_type is a placeholder until lifecycle runs
    raw: dict[str, Any]


@dataclass
class PollResult:
    source: str
    entity_type: EntityType
    observed_at: datetime
    ok: bool
    not_modified: bool = False
    observations: list[Observation] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)  # failed validation
    source_updated_at: datetime | None = None
    total_reported: int | None = None
    elapsed_ms: float = 0
    attempts: int = 0
    error: str | None = None
    raw_envelope: dict[str, Any] | None = None  # for sample capture / bronze


class Recorder(Protocol):
    """Captures raw API responses for replay (see mvoi.replay.recording)."""

    def record(self, source: str, resource: str, observed_at: datetime,
               envelope: dict[str, Any] | None) -> None: ...


class SourceAdapter(Protocol):
    name: str

    def poll(self, observed_at: datetime) -> list[PollResult]: ...

    def parse_envelope(self, resource: str, envelope: dict[str, Any], observed_at: datetime,
                       elapsed_ms: float = 0, attempts: int = 0) -> PollResult: ...
