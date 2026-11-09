"""OHGO (ODOT) Public API adapter.

Documented at https://publicapi.ohgo.com/docs/v1/swagger.json. Field names
below are exactly those in the published swagger (camelCase JSON):

  envelope : links, lastUpdated, accecptedFilters [sic], rejectedFilters,
             totalPageCount, totalResultCount, currentResultCount, results
  incident : id, latitude, longitude, location, description, category,
             direction, routeName, roadStatus, roadClosureDetails
             {closureStartLocation, closureEndLocation, polyline}  ([lon,lat])
  construction : id, latitude, longitude, location, description, category,
             direction, district, routeName, status, startDate, endDate,
             workZoneProjectId, workZones, detours
  dangerous-slowdown : id, latitude, longitude, location, description,
             normalMPH, currentMPH, routeName, direction
  travel-delay : id, latitude, longitude, location, description, direction,
             routeName, travelTime, delayTime, startMileMarker, endMileMarker,
             currentAvgSpeed, normalAvgSpeed   (per /docs/v1/travel-delays)

Auth: `Authorization: APIKEY <key>` header. Rate limit: HTTP 429 is
documented ("x requests in a timeframe") but the limit itself is not
published — the client honours Retry-After and backs off.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from mvoi.common.config import BoundingBox, OhgoSettings
from mvoi.common.http import FetchError, ResilientClient
from mvoi.geo.reference import CorridorMatcher
from mvoi.models.events import (
    ChangeType,
    EntityType,
    NormalizedEvent,
    Source,
    deterministic_source_id,
    make_entity_key,
)
from mvoi.sources import severity as sev
from mvoi.sources.base import Observation, PollResult, Recorder

log = logging.getLogger(__name__)

RESOURCE_ENTITY = {
    "incidents": EntityType.INCIDENT,
    "construction": EntityType.CONSTRUCTION,
    "dangerous-slowdowns": EntityType.DANGEROUS_SLOWDOWN,
    "travel-delays": EntityType.TRAVEL_DELAY,
}


def bbox_params(bb: BoundingBox) -> dict[str, str]:
    """OHGO map-bounds filter.

    The swagger text says "(longitude,latitude)" but every documented example
    is latitude-first (e.g. `41.011,-82.368`, which is only valid in Ohio as
    lat,lon). We send latitude,longitude, matching the examples; the live
    smoke test (tests/integration/test_live_sources.py) asserts that returned
    points fall inside the box, and `rejectedFilters` is logged if the API
    disagrees.
    """
    return {
        "map-bounds-sw": f"{bb.south},{bb.west}",
        "map-bounds-ne": f"{bb.north},{bb.east}",
        "page-all": "true",
    }


class OhgoAdapter:
    name = "ohgo"

    def __init__(self, client: ResilientClient | None, settings: OhgoSettings, api_key: str | None,
                 bbox: BoundingBox, corridors: CorridorMatcher, environment: str = "dev",
                 recorder: Recorder | None = None):
        if client is not None and not api_key:
            raise ValueError("OHGO API key is required (env OHGO_API_KEY or Key Vault)")
        self.recorder = recorder
        self._client = client
        self._settings = settings
        self._api_key = api_key
        self._bbox = bbox
        self._corridors = corridors
        self._environment = environment

    def poll(self, observed_at: datetime) -> list[PollResult]:
        return [self.poll_resource(r, observed_at) for r in self._settings.resources]

    def poll_resource(self, resource: str, observed_at: datetime) -> PollResult:
        assert self._client is not None, "adapter built without a client (replay-only)"
        entity_type = RESOURCE_ENTITY[resource]
        url = f"{self._settings.base_url.rstrip('/')}/api/v1/{resource}"
        params = bbox_params(self._bbox)
        if resource == "construction":
            # Polylines are large; points are enough for this demo and the
            # work zones can be fetched per id when needed.
            params["exclude-work-zone"] = "true"
        try:
            res = self._client.get_json(
                url, params=params,
                headers={"Authorization": f"APIKEY {self._api_key}", "Accept": "application/json"},
                use_etag=self._settings.use_etag, cache_key=resource,
            )
        except FetchError as exc:
            log.error("ohgo poll failed", extra={"resource": resource, "error": str(exc),
                                                 "status": exc.status_code})
            return PollResult(self.name, entity_type, observed_at, ok=False, error=str(exc))

        if res.not_modified:
            return PollResult(self.name, entity_type, observed_at, ok=True, not_modified=True,
                              elapsed_ms=res.elapsed_ms, attempts=res.attempts)
        if self.recorder:
            self.recorder.record(self.name, resource, observed_at, res.data)
        return self.parse_envelope(resource, res.data or {}, observed_at,
                                   elapsed_ms=res.elapsed_ms, attempts=res.attempts)

    def parse_envelope(self, resource: str, envelope: dict[str, Any], observed_at: datetime,
                       elapsed_ms: float = 0, attempts: int = 0) -> PollResult:
        """Parse one API response. Shared by live polling and replay."""
        entity_type = RESOURCE_ENTITY[resource]
        if envelope.get("rejectedFilters"):
            log.warning("ohgo rejected filters",
                        extra={"resource": resource, "rejected": envelope["rejectedFilters"]})
        result = PollResult(
            self.name, entity_type, observed_at, ok=True,
            source_updated_at=_parse_dt(envelope.get("lastUpdated")),
            total_reported=envelope.get("totalResultCount"),
            elapsed_ms=elapsed_ms, attempts=attempts, raw_envelope=envelope,
        )
        for item in envelope.get("results") or []:
            try:
                obs = self.normalize(entity_type, item, observed_at, result.source_updated_at)
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                result.rejected.append({"reason": str(exc)[:500], "raw": item})
                continue
            if obs.event.latitude is None or obs.event.longitude is None:
                result.rejected.append({"reason": "missing coordinates", "raw": item})
                continue
            if not self._bbox.contains(obs.event.latitude, obs.event.longitude):
                # Server-side filter should prevent this; keep the box authoritative.
                continue
            result.observations.append(obs)
        return result

    # ------------------------------------------------------------------ normalisation

    def normalize(self, entity_type: EntityType, item: dict[str, Any], observed_at: datetime,
                  source_updated_at: datetime | None) -> Observation:
        lat, lon = item.get("latitude"), item.get("longitude")
        source_id = str(item.get("id") or "").strip()
        if not source_id:
            source_id = deterministic_source_id(entity_type, item.get("routeName"),
                                                item.get("direction"), lat, lon,
                                                item.get("description"))
        route = item.get("routeName")
        corridor_id, corridor_method = self._corridors.match(route, lat, lon)
        common = dict(
            entity_key=make_entity_key(Source.OHGO, entity_type, source_id),
            source=Source.OHGO,
            entity_type=entity_type,
            source_id=source_id,
            change_type=ChangeType.UPDATED,  # placeholder; lifecycle decides
            environment=self._environment,
            observed_at=observed_at,
            source_updated_at=source_updated_at,
            latitude=lat,
            longitude=lon,
            route_name=route,
            corridor_id=corridor_id,
            direction=item.get("direction"),
            location=item.get("location"),
            description=item.get("description"),
            raw=item,
        )
        attrs: dict[str, Any] = {"corridor_match_method": corridor_method}

        if entity_type == EntityType.INCIDENT:
            road_status = item.get("roadStatus")
            details = item.get("roadClosureDetails") or {}
            polyline = details.get("polyline")
            ev = NormalizedEvent(
                **common,
                category=item.get("category"),
                status=road_status,
                severity=sev.incident_severity(item.get("category"), road_status,
                                               item.get("description")),
                is_road_closure=sev.road_status_is_closed(road_status),
                title=f"{item.get('category') or 'Incident'} on {route or 'unknown route'}",
                geometry_geojson=_line_geojson(polyline),
                attributes={**attrs,
                            "closure_start": details.get("closureStartLocation"),
                            "closure_end": details.get("closureEndLocation")},
            )
        elif entity_type == EntityType.CONSTRUCTION:
            status = item.get("status")
            ev = NormalizedEvent(
                **common,
                category=item.get("category"),
                status=status,
                severity=sev.construction_severity(status),
                is_road_closure=sev.road_status_is_closed(status),
                title=f"Construction on {route or 'unknown route'}",
                start_time=_parse_dt(item.get("startDate")),
                end_time=_parse_dt(item.get("endDate")),
                attributes={**attrs, "district": item.get("district"),
                            "work_zone_project_id": item.get("workZoneProjectId")},
            )
        elif entity_type == EntityType.DANGEROUS_SLOWDOWN:
            normal, current = item.get("normalMPH"), item.get("currentMPH")
            ev = NormalizedEvent(
                **common,
                category="Dangerous Slowdown",
                severity=sev.slowdown_severity(normal, current),
                title=f"Dangerous slowdown on {route or 'unknown route'}",
                attributes={**attrs, "normal_mph": normal, "speed_band": sev.speed_band(normal, current),
                            "live_current_mph": current},
            )
        elif entity_type == EntityType.TRAVEL_DELAY:
            delay = item.get("delayTime")
            ev = NormalizedEvent(
                **common,
                category="Travel Delay",
                severity=sev.travel_delay_severity(delay),
                title=f"Travel delay on {route or 'unknown route'}",
                attributes={**attrs, "delay_band": sev.delay_band(delay),
                            "start_mile_marker": item.get("startMileMarker"),
                            "end_mile_marker": item.get("endMileMarker"),
                            "normal_avg_speed": item.get("normalAvgSpeed"),
                            "live_delay_minutes": delay,
                            "live_travel_time_minutes": item.get("travelTime"),
                            "live_current_avg_speed": item.get("currentAvgSpeed")},
            )
        else:  # pragma: no cover
            raise ValueError(f"unsupported entity type {entity_type}")
        return Observation(event=ev, raw=item)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _line_geojson(polyline: list | None) -> str | None:
    """OHGO polylines are arrays of [longitude, latitude] pairs (documented)."""
    if not polyline or len(polyline) < 2:
        return None
    return json.dumps({"type": "LineString", "coordinates": polyline}, separators=(",", ":"))
