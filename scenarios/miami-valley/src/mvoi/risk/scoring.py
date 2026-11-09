"""Explainable site travel-disruption exposure scoring.

    raw   = incident + road_closure + slowdown + weather + history   (each capped)
    score = min(100, raw * criticality_multiplier[facility.criticality])

Each proximity contribution is weighted by distance: 1.0 within
`distance_bands_m.full`, falling linearly to 0 at `distance_bands_m.max`.

The score is INFERRED EXPOSURE: something disruptive is near the facility,
or a warning covers it. It does not establish that a route or delivery is
actually affected; explanations say so explicitly.

Pure Python on purpose: the polling service (hot path) and the Gold notebook
(historical path) import this same module, so both paths score identically.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from mvoi.common.config import RiskConfig
from mvoi.geo.core import haversine_m, parse_geometry, point_in_polygon
from mvoi.geo.reference import Facility

INCIDENT_TYPES = {"incident", "construction"}
SLOWDOWN_TYPES = {"dangerous_slowdown", "travel_delay"}
METERS_PER_MILE = 1609.344


@dataclass(frozen=True)
class ActiveEvent:
    """Minimal view of an active event needed for scoring."""

    entity_key: str
    entity_type: str
    severity: str | None
    latitude: float | None
    longitude: float | None
    is_road_closure: bool = False
    geometry_geojson: str | None = None
    category: str | None = None
    cap_severity: str | None = None   # weather only
    title: str | None = None

    @classmethod
    def from_event(cls, e: Mapping[str, Any]) -> ActiveEvent:
        attrs = e.get("attributes") or {}
        return cls(
            entity_key=e["entity_key"], entity_type=e["entity_type"], severity=e.get("severity"),
            latitude=e.get("latitude"), longitude=e.get("longitude"),
            is_road_closure=bool(e.get("is_road_closure")),
            geometry_geojson=e.get("geometry_geojson"), category=e.get("category"),
            cap_severity=attrs.get("cap_severity"), title=e.get("title"),
        )


@dataclass
class Contributor:
    entity_key: str
    entity_type: str
    component: str
    points: float
    distance_m: float | None
    label: str


@dataclass
class FacilityRisk:
    facility_id: str
    facility_name: str
    scored_at: datetime
    score: float
    band: str
    raw_points: float
    criticality: int
    criticality_multiplier: float
    incident_points: float
    road_closure_points: float
    slowdown_points: float
    weather_points: float
    history_points: float
    nearby_incident_count: int
    nearby_closure_count: int
    nearby_slowdown_count: int
    weather_alert_count: int
    history_occurrences: int
    nearest_incident_m: float | None
    explanation: str
    exposure_type: str = "inferred"   # never "confirmed" — see module docstring
    model_version: int = 1
    contributors: list[Contributor] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scored_at"] = self.scored_at.isoformat().replace("+00:00", "Z")
        return d


def distance_weight(distance_m: float, cfg: RiskConfig) -> float:
    full, max_ = cfg.distance_bands_m.full, cfg.distance_bands_m.max
    if distance_m <= full:
        return 1.0
    if distance_m >= max_:
        return 0.0
    return 1.0 - (distance_m - full) / (max_ - full)


def score_facility(
    facility: Facility,
    events: Iterable[ActiveEvent],
    cfg: RiskConfig,
    scored_at: datetime,
    history_occurrences: int = 0,
) -> FacilityRisk:
    incident_pts = closure_pts = slowdown_pts = weather_pts = 0.0
    n_inc = n_closure = n_slow = n_wx = 0
    nearest: float | None = None
    contributors: list[Contributor] = []
    weather_titles: list[str] = []

    for e in events:
        if e.entity_type == "weather_alert":
            geom = parse_geometry(e.geometry_geojson)
            if geom and point_in_polygon(facility.latitude, facility.longitude, geom):
                pts = cfg.weather.points_by_severity.get(e.cap_severity or "Unknown",
                                                         cfg.weather.points_by_severity.get("Unknown", 0))
                weather_pts += pts
                n_wx += 1
                weather_titles.append(e.category or "weather alert")
                contributors.append(Contributor(e.entity_key, e.entity_type, "weather", pts, 0.0,
                                                f"{e.category or 'Weather alert'} covers site"))
            continue
        if e.latitude is None or e.longitude is None:
            continue
        d = haversine_m(facility.latitude, facility.longitude, e.latitude, e.longitude)
        w = distance_weight(d, cfg)
        if w <= 0:
            continue
        if e.entity_type in INCIDENT_TYPES:
            nearest = d if nearest is None else min(nearest, d)
            pts = cfg.incident.points_by_severity.get(e.severity or "minor", 0) * w
            incident_pts += pts
            n_inc += 1
            contributors.append(Contributor(e.entity_key, e.entity_type, "incident", pts, d,
                                            e.title or e.entity_type))
            if e.is_road_closure:
                cpts = cfg.road_closure.points * w
                closure_pts += cpts
                n_closure += 1
                contributors.append(Contributor(e.entity_key, e.entity_type, "road_closure", cpts, d,
                                                "road closure"))
        elif e.entity_type in SLOWDOWN_TYPES:
            pts = cfg.slowdown.points * w
            slowdown_pts += pts
            n_slow += 1
            contributors.append(Contributor(e.entity_key, e.entity_type, "slowdown", pts, d,
                                            e.title or e.entity_type))

    incident_pts = min(incident_pts, cfg.incident.cap)
    closure_pts = min(closure_pts, cfg.road_closure.cap)
    slowdown_pts = min(slowdown_pts, cfg.slowdown.cap)
    weather_pts = min(weather_pts, cfg.weather.cap)
    history_pts = min(history_occurrences * cfg.history.points_per_occurrence, cfg.history.cap)

    raw = incident_pts + closure_pts + slowdown_pts + weather_pts + history_pts
    mult = cfg.criticality_multiplier.get(facility.criticality, 1.0)
    score = round(min(100.0, raw * mult), 1)
    band = cfg.band_for(score)

    risk = FacilityRisk(
        facility_id=facility.facility_id, facility_name=facility.facility_name,
        scored_at=scored_at, score=score, band=band, raw_points=round(raw, 2),
        criticality=facility.criticality, criticality_multiplier=mult,
        incident_points=round(incident_pts, 2), road_closure_points=round(closure_pts, 2),
        slowdown_points=round(slowdown_pts, 2), weather_points=round(weather_pts, 2),
        history_points=round(history_pts, 2),
        nearby_incident_count=n_inc, nearby_closure_count=n_closure,
        nearby_slowdown_count=n_slow, weather_alert_count=n_wx,
        history_occurrences=history_occurrences,
        nearest_incident_m=round(nearest, 0) if nearest is not None else None,
        explanation="", model_version=cfg.version,
        contributors=sorted(contributors, key=lambda c: -c.points),
    )
    risk.explanation = explain(risk, weather_titles)
    return risk


def explain(r: FacilityRisk, weather_titles: list[str]) -> str:
    """Human-readable, deliberately hedged explanation of the score."""
    parts: list[str] = []
    if r.nearby_incident_count:
        n = r.nearby_incident_count
        s = f"{n} nearby roadway incident{'s' if n != 1 else ''}"
        if r.nearest_incident_m is not None:
            s += f" (nearest {r.nearest_incident_m / METERS_PER_MILE:.1f} mi)"
        if r.nearby_closure_count:
            s += f", {r.nearby_closure_count} with road closure"
        parts.append(s)
    if r.nearby_slowdown_count:
        n = r.nearby_slowdown_count
        parts.append(f"{n} nearby slowdown{'s' if n != 1 else ''}/travel delay{'s' if n != 1 else ''}")
    if weather_titles:
        uniq = sorted(set(weather_titles))
        parts.append("active " + ", ".join(uniq) + " covering the site")
    if r.history_points >= 5:
        parts.append(f"elevated recent incident history ({r.history_occurrences} occurrences nearby)")

    if not parts:
        return (f"Disruption exposure {r.band} ({r.score:.0f}): "
                "no active transportation or weather exposure detected.")
    lead = f"Disruption exposure {r.band} ({r.score:.0f}) due to " + _join(parts)
    if r.criticality_multiplier > 1:
        lead += f"; site importance {r.criticality} amplifies the score"
    return lead + (". Inferred from public traffic and weather data near the site; "
                   "not a statement about the site's operations.")


def _join(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def score_all(facilities: Iterable[Facility], events: Iterable[ActiveEvent], cfg: RiskConfig,
              scored_at: datetime, history: Mapping[str, int] | None = None) -> list[FacilityRisk]:
    evs = list(events)
    history = history or {}
    return [score_facility(f, evs, cfg, scored_at, history.get(f.facility_id, 0)) for f in facilities]
