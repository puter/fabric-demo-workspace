"""Operational alert state machine with de-duplication and recovery.

Each alert has a stable `alert_key`. The manager only emits on transitions:

  facility_risk_high      raise when score >= raise_threshold; clear when
                          score < clear_threshold (hysteresis). A re-raise
                          inside `min_reraise_minutes` of the last clear is
                          suppressed (flapping guard).
  incident_near_facility  raise once per (facility, incident) when a MAJOR or
                          CRITICAL incident is within the full-weight
                          distance; clear when the incident resolves.
  weather_at_facility     raise once per (facility, NWS alert) whose polygon
                          or zones cover the site; clear on expiry/resolution.

Ingestion staleness ("no successful ingestion in N minutes") cannot be
raised by the service that has stopped, so it is implemented in Activator /
KQL from the heartbeat stream (see fabric/activator/).

Activator rules key on alert_state == "raised", so each condition notifies
once, and once more when it clears.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from mvoi.common.config import RiskConfig
from mvoi.geo.core import haversine_m
from mvoi.geo.reference import Facility
from mvoi.models.events import ChangeType, EntityType, NormalizedEvent, Source, make_entity_key
from mvoi.risk.scoring import INCIDENT_TYPES, ActiveEvent, FacilityRisk
from mvoi.services.state import StateStore

_KV = "alerts.open"          # alert_key -> {raised_at, facility_id, alert_type, ...}
_KV_LAST_CLEAR = "alerts.last_clear"


class AlertManager:
    def __init__(self, store: StateStore, cfg: RiskConfig, environment: str = "dev",
                 run_mode: str = "live", replay_session_id: str | None = None):
        self.store = store
        self.cfg = cfg
        self.environment = environment
        self.run_mode = run_mode
        self.replay_session_id = replay_session_id

    def evaluate(self, now: datetime, facilities: Iterable[Facility],
                 risks: Iterable[FacilityRisk], active: Iterable[ActiveEvent]) -> list[NormalizedEvent]:
        open_alerts: dict[str, dict[str, Any]] = self.store.get_kv(_KV, {})
        last_clear: dict[str, str] = self.store.get_kv(_KV_LAST_CLEAR, {})
        fac = {f.facility_id: f for f in facilities}
        active = list(active)
        desired: dict[str, dict[str, Any]] = {}

        a = self.cfg.alerting
        for r in risks:
            key = f"facility_risk_high:{r.facility_id}"
            is_open = key in open_alerts
            if r.score >= a.raise_threshold or (is_open and r.score >= a.clear_threshold):
                desired[key] = {"alert_type": "facility_risk_high", "facility_id": r.facility_id,
                                "score": r.score, "band": r.band, "message": r.explanation}

        full = self.cfg.distance_bands_m.full
        for e in active:
            if e.entity_type in INCIDENT_TYPES and e.severity in ("major", "critical") \
                    and e.latitude is not None and e.longitude is not None:
                for f in fac.values():
                    d = haversine_m(f.latitude, f.longitude, e.latitude, e.longitude)
                    if d <= full:
                        key = f"incident_near_facility:{f.facility_id}:{e.entity_key}"
                        desired[key] = {"alert_type": "incident_near_facility",
                                        "facility_id": f.facility_id, "related_entity_key": e.entity_key,
                                        "distance_m": round(d), "severity": e.severity,
                                        "message": f"{e.severity.title()} {e.title or 'incident'} "
                                                   f"{d / 1609.344:.1f} mi from {f.facility_name}"}
        for r in risks:
            for c in r.contributors:
                if c.component == "weather":
                    key = f"weather_at_facility:{r.facility_id}:{c.entity_key}"
                    desired[key] = {"alert_type": "weather_at_facility", "facility_id": r.facility_id,
                                    "related_entity_key": c.entity_key,
                                    "message": f"{c.label} — {r.facility_name}"}

        out: list[NormalizedEvent] = []
        for key, info in desired.items():
            if key in open_alerts:
                continue
            lc = last_clear.get(key)
            if lc and info["alert_type"] == "facility_risk_high" and \
                    now - datetime.fromisoformat(lc) < timedelta(minutes=a.min_reraise_minutes):
                continue  # flapping guard
            open_alerts[key] = {**info, "raised_at": now.isoformat()}
            out.append(self._event(key, "raised", info, now, fac.get(info["facility_id"])))
        for key in list(open_alerts):
            if key not in desired:
                info = open_alerts.pop(key)
                last_clear[key] = now.isoformat()
                out.append(self._event(key, "cleared", info, now, fac.get(info["facility_id"])))

        self.store.set_kv(_KV, open_alerts)
        self.store.set_kv(_KV_LAST_CLEAR, last_clear)
        return out

    def _event(self, key: str, state: str, info: dict[str, Any], now: datetime,
               facility: Facility | None) -> NormalizedEvent:
        return NormalizedEvent(
            entity_key=make_entity_key(Source.MVOI, EntityType.OPERATIONAL_ALERT, key),
            source=Source.MVOI, entity_type=EntityType.OPERATIONAL_ALERT, source_id=key,
            change_type=ChangeType.CREATED if state == "raised" else ChangeType.RESOLVED,
            environment=self.environment, run_mode=self.run_mode,
            replay_session_id=self.replay_session_id,
            observed_at=now,
            latitude=facility.latitude if facility else None,
            longitude=facility.longitude if facility else None,
            category=info["alert_type"], status=state,
            title=info.get("message") if state == "raised" else f"Cleared: {info.get('message')}",
            attributes={**{k: v for k, v in info.items() if k != "message"},
                        "alert_key": key, "alert_state": state,
                        "facility_name": facility.facility_name if facility else None},
        ).finalize()
