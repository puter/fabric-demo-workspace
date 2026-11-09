"""National Weather Service active-alerts adapter.

GET https://api.weather.gov/alerts/active?area=OH returns a GeoJSON
FeatureCollection of CAP alerts. Properties used (exact names): id, areaDesc,
geocode{SAME,UGC}, affectedZones, references, sent, effective, onset,
expires, ends, status, messageType, category, severity, certainty, urgency,
event, senderName, headline, description, instruction, response.

Many alerts (e.g. winter, flood, heat) carry *zones* but `geometry: null`.
For those we build the geometry from the committed NWS zone polygons so that
facility/incident intersection still works; `attributes.geometry_source`
records which happened.

No API key. NWS requires a descriptive User-Agent and asks clients to avoid
polling faster than needed; responses are cacheable (max-age is a few
seconds), so 120 s polling is well within policy.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from mvoi.common.config import BoundingBox, Geography, NwsSettings
from mvoi.common.http import FetchError, ResilientClient
from mvoi.geo.core import bbox_intersects, geometry_bbox, polygon_centroid
from mvoi.geo.reference import ZoneIndex
from mvoi.models.events import ChangeType, EntityType, NormalizedEvent, Source, make_entity_key
from mvoi.sources import severity as sev
from mvoi.sources.base import Observation, PollResult, Recorder

log = logging.getLogger(__name__)


class NwsAdapter:
    name = "nws"

    def __init__(self, client: ResilientClient | None, settings: NwsSettings, geography: Geography,
                 zones: ZoneIndex, environment: str = "dev", recorder: Recorder | None = None):
        self.recorder = recorder
        self._client = client
        self._settings = settings
        self._geo = geography
        self._zones = zones
        self._environment = environment

    def poll(self, observed_at: datetime) -> list[PollResult]:
        url = f"{self._settings.base_url.rstrip('/')}/alerts/active"
        try:
            res = self._client.get_json(
                url, params={"area": self._geo.nws.area, "status": "actual"},
                headers={"User-Agent": self._settings.user_agent, "Accept": "application/geo+json"},
            )
        except FetchError as exc:
            log.error("nws poll failed", extra={"error": str(exc), "status": exc.status_code})
            return [PollResult(self.name, EntityType.WEATHER_ALERT, observed_at, ok=False,
                               error=str(exc))]
        if self.recorder:
            self.recorder.record(self.name, "alerts", observed_at, res.data)
        return [self.parse_envelope("alerts", res.data or {}, observed_at,
                                    elapsed_ms=res.elapsed_ms, attempts=res.attempts)]

    def parse_envelope(self, resource: str, fc: dict[str, Any], observed_at: datetime,
                       elapsed_ms: float = 0, attempts: int = 0) -> PollResult:
        """Parse one API response. Shared by live polling and replay."""
        result = PollResult(self.name, EntityType.WEATHER_ALERT, observed_at, ok=True,
                            source_updated_at=_parse_dt(fc.get("updated")),
                            total_reported=len(fc.get("features") or []),
                            elapsed_ms=elapsed_ms, attempts=attempts, raw_envelope=fc)
        for feature in fc.get("features") or []:
            try:
                obs = self.normalize(feature, observed_at, result.source_updated_at)
            except (ValueError, TypeError, KeyError) as exc:
                result.rejected.append({"reason": str(exc)[:500], "raw": feature})
                continue
            if obs is not None:
                result.observations.append(obs)
        return result

    def normalize(self, feature: dict[str, Any], observed_at: datetime,
                  source_updated_at: datetime | None) -> Observation | None:
        """Returns None when the alert does not touch the area of interest."""
        p = feature["properties"]
        zone_ids = _zone_ids(p)
        relevant_zones = sorted(zone_ids & self._geo.nws.all_zone_ids)

        geometry = feature.get("geometry")
        geometry_source = "alert_polygon"
        if geometry:
            if not bbox_intersects(geometry_bbox(geometry), _bb_tuple(self._geo.bounding_box)):
                return None
        else:
            if not relevant_zones:
                return None
            polys = [g for z in relevant_zones if (g := self._zones.geometry(z))]
            geometry = _merge_polygons(polys) if polys else None
            geometry_source = "nws_zones" if geometry else "none"

        alert_id = p["id"]
        cap_sev = p.get("severity")
        lat = lon = None
        if geometry:
            lat, lon = polygon_centroid(geometry)
        ev = NormalizedEvent(
            entity_key=make_entity_key(Source.NWS, EntityType.WEATHER_ALERT, alert_id),
            source=Source.NWS,
            entity_type=EntityType.WEATHER_ALERT,
            source_id=alert_id,
            change_type=ChangeType.UPDATED,
            environment=self._environment,
            observed_at=observed_at,
            source_updated_at=_parse_dt(p.get("sent")) or source_updated_at,
            start_time=_parse_dt(p.get("onset")) or _parse_dt(p.get("effective")),
            end_time=_parse_dt(p.get("ends")) or _parse_dt(p.get("expires")),
            latitude=lat,
            longitude=lon,
            geometry_geojson=json.dumps(geometry, separators=(",", ":")) if geometry else None,
            location=p.get("areaDesc"),
            category=p.get("event"),
            status=p.get("messageType"),
            severity=sev.nws_severity(cap_sev),
            title=p.get("headline") or p.get("event"),
            description=p.get("description"),
            attributes={
                "cap_severity": cap_sev,
                "certainty": p.get("certainty"),
                "urgency": p.get("urgency"),
                "response": p.get("response"),
                "sender_name": p.get("senderName"),
                "instruction": p.get("instruction"),
                "expires": p.get("expires"),
                "zones": sorted(zone_ids),
                "aoi_zones": relevant_zones,
                "references": [r.get("identifier") for r in p.get("references") or []],
                "geometry_source": geometry_source,
            },
            raw=feature,
        )
        return Observation(event=ev, raw=feature)


def _zone_ids(p: dict[str, Any]) -> set[str]:
    ids = set((p.get("geocode") or {}).get("UGC") or [])
    for url in p.get("affectedZones") or []:
        ids.add(url.rstrip("/").rsplit("/", 1)[-1])
    return ids


def _merge_polygons(geoms: list[dict[str, Any]]) -> dict[str, Any]:
    coords: list = []
    for g in geoms:
        if g["type"] == "Polygon":
            coords.append(g["coordinates"])
        elif g["type"] == "MultiPolygon":
            coords.extend(g["coordinates"])
    return {"type": "MultiPolygon", "coordinates": coords}


def _bb_tuple(bb: BoundingBox) -> tuple[float, float, float, float]:
    return bb.west, bb.south, bb.east, bb.north


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
