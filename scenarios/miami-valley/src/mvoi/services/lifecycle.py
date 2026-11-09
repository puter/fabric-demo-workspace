"""Turns raw poll observations into lifecycle events.

Rules (configurable via `lifecycle` settings):

* new entity_key                         -> created
* previously resolved, seen again        -> reopened
* active, content_hash changed           -> updated
* active, unchanged                      -> nothing published (dedup)
* active, absent from a successful poll  -> missed_polls += 1; resolved only when
  missed_polls >= resolve_after_missed_polls AND absent >= resolve_after_minutes
* weather alert past its end/expires     -> resolved (source-declared expiry)

Absence never counts when the poll failed, returned 304 Not Modified, or
returned suspiciously few results (< min_result_ratio_for_resolution of the
current active count) — a truncated upstream response must not mass-resolve.
A low count that persists for max_resolution_suppression_minutes is accepted
as real (traffic genuinely cleared), so stale entities do not stay active forever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mvoi.common.config import LifecycleSettings
from mvoi.models.events import ChangeType, EntityType, NormalizedEvent, RunMode
from mvoi.services.state import EntityState, StateStore
from mvoi.sources.base import PollResult

log = logging.getLogger(__name__)

# Below this many active entities the ratio guard is meaningless (1 -> 0 is a
# legitimate "last incident cleared").
_RATIO_GUARD_MIN_ACTIVE = 5


@dataclass
class LifecycleOutcome:
    events: list[NormalizedEvent] = field(default_factory=list)
    unchanged: int = 0
    suppressed_resolution: bool = False


class LifecycleTracker:
    def __init__(self, store: StateStore, settings: LifecycleSettings, *,
                 environment: str | None = None, run_mode: RunMode | str = RunMode.LIVE,
                 replay_session_id: str | None = None):
        self.store = store
        self.settings = settings
        self.environment = environment
        self.run_mode = RunMode(run_mode)
        self.replay_session_id = replay_session_id
        # (source, entity_type) -> when the ratio guard first tripped in the current streak.
        self._suppressed_since: dict[tuple[str, str], datetime] = {}

    def apply(self, poll: PollResult) -> LifecycleOutcome:
        out = LifecycleOutcome()
        if not poll.ok:
            return out
        etype = str(poll.entity_type)
        if poll.not_modified:
            self.store.touch_all_active(etype, poll.source, poll.observed_at)
            return out

        with self.store.transaction():
            previously_active = self.store.active(entity_type=etype, source=poll.source)
            seen: set[str] = set()
            for obs in poll.observations:
                ev = obs.event
                if ev.entity_key in seen:
                    continue  # duplicate within one response
                seen.add(ev.entity_key)
                published = self._observe(ev, poll.observed_at)
                if published is None:
                    out.unchanged += 1
                else:
                    out.events.append(published)

            prev_count = len(previously_active)
            guard = (prev_count >= _RATIO_GUARD_MIN_ACTIVE
                     and len(seen) < self.settings.min_result_ratio_for_resolution * prev_count)
            guard = self._guard_still_applies(guard, poll.source, etype, poll.observed_at)
            if guard:
                out.suppressed_resolution = True
                log.warning("suppressing resolution: result count dropped sharply",
                            extra={"source": poll.source, "entity_type": etype,
                                   "seen": len(seen), "previously_active": prev_count})

            for s in previously_active:
                if guard or s.entity_key in seen:
                    continue
                s.missed_polls += 1
                absent_for = poll.observed_at - s.last_seen_at
                if (s.missed_polls >= self.settings.resolve_after_missed_polls
                        and absent_for >= timedelta(minutes=self.settings.resolve_after_minutes)):
                    out.events.append(self._resolve(s, poll.observed_at, reason="absent"))
                else:
                    self.store.upsert(s)

            # Source-declared expiry (NWS `ends`/`expires`).
            if poll.entity_type == EntityType.WEATHER_ALERT:
                for s in self.store.active(entity_type=etype, source=poll.source):
                    end = s.last_event.get("end_time")
                    if end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= poll.observed_at:
                        out.events.append(self._resolve(s, poll.observed_at, reason="expired"))
        return out

    def _guard_still_applies(self, tripped: bool, source: str, etype: str, now: datetime) -> bool:
        key = (source, etype)
        if not tripped:
            self._suppressed_since.pop(key, None)
            return False
        since = self._suppressed_since.setdefault(key, now)
        limit = timedelta(minutes=self.settings.max_resolution_suppression_minutes)
        if now - since < limit:
            return True
        log.info("accepting low result count: persisted past the suppression limit",
                 extra={"source": source, "entity_type": etype, "since": since.isoformat()})
        return False

    def _observe(self, ev: NormalizedEvent, now: datetime) -> NormalizedEvent | None:
        self._tag(ev)
        ev.finalize()
        prior = self.store.get(ev.entity_key)
        if prior is None:
            change, first_seen = ChangeType.CREATED, now
        elif prior.status == "resolved":
            change, first_seen = ChangeType.REOPENED, prior.first_seen_at
        elif prior.content_hash != ev.content_hash:
            change, first_seen = ChangeType.UPDATED, prior.first_seen_at
        else:
            prior.last_seen_at = now
            prior.missed_polls = 0
            self.store.upsert(prior)
            return None

        ev.change_type = change
        ev.first_seen_at = first_seen
        ev.last_seen_at = now
        ev.event_id = ""
        ev.finalize()
        self.store.upsert(EntityState(
            entity_key=ev.entity_key, source=str(ev.source), entity_type=str(ev.entity_type),
            status="active", content_hash=ev.content_hash, first_seen_at=first_seen,
            last_seen_at=now, missed_polls=0, resolved_at=None,
            last_event=ev.model_dump(mode="json", exclude={"raw"}),
        ))
        return ev

    def _tag(self, ev: NormalizedEvent) -> None:
        if self.environment:
            ev.environment = self.environment
        ev.run_mode = self.run_mode
        ev.replay_session_id = self.replay_session_id

    def _resolve(self, s: EntityState, now: datetime, reason: str) -> NormalizedEvent:
        data = dict(s.last_event)
        data.update(change_type=ChangeType.RESOLVED, observed_at=now, event_id="",
                    first_seen_at=s.first_seen_at, last_seen_at=s.last_seen_at)
        data["attributes"] = {**(data.get("attributes") or {}), "resolution_reason": reason}
        ev = NormalizedEvent.model_validate(data)
        self._tag(ev)
        ev.finalize()
        s.status = "resolved"
        s.resolved_at = now
        s.last_event = ev.model_dump(mode="json", exclude={"raw"})
        self.store.upsert(s)
        return ev
