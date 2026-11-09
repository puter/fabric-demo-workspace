"""Silver-layer data-quality rules.

Each rule is a Spark SQL boolean expression that must be TRUE for a valid
row. `severity="error"` rows are quarantined (silver_events_rejected);
`"warn"` rows are kept and flagged. Results per run land in
silver_dq_results and feed the Power BI pipeline-health page.

The thresholds come from config/geography.yaml so the AOI is never
duplicated.
"""

from __future__ import annotations

from dataclasses import dataclass

from mvoi.common.config import Geography

ENTITY_TYPES = ("incident", "construction", "dangerous_slowdown", "travel_delay", "weather_alert",
                "facility_risk", "operational_alert", "ingestion_heartbeat")
CHANGE_TYPES = ("created", "updated", "resolved", "reopened", "snapshot")


@dataclass(frozen=True)
class Rule:
    name: str
    expression: str
    severity: str  # error | warn
    description: str


def rules(geo: Geography) -> list[Rule]:
    bb = geo.bounding_box
    types = ", ".join(f"'{t}'" for t in ENTITY_TYPES)
    changes = ", ".join(f"'{c}'" for c in CHANGE_TYPES)
    transport = "entity_type IN ('incident','construction','dangerous_slowdown','travel_delay')"
    return [
        Rule("event_id_present", "event_id IS NOT NULL AND length(event_id) = 36", "error",
             "Deterministic UUID event id present"),
        Rule("entity_key_present", "entity_key IS NOT NULL AND entity_key <> ''", "error",
             "Entity key present"),
        Rule("known_entity_type", f"entity_type IN ({types})", "error", "Entity type in contract"),
        Rule("known_change_type", f"change_type IN ({changes})", "error", "Change type in contract"),
        Rule("observed_at_present", "observed_at IS NOT NULL", "error", "Observation time present"),
        Rule("lat_lon_valid",
             "(latitude IS NULL AND longitude IS NULL) OR "
             "(latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)", "error",
             "Coordinates in WGS84 range"),
        Rule("transport_has_location",
             f"NOT ({transport}) OR (latitude IS NOT NULL AND longitude IS NOT NULL)", "error",
             "Transportation events are located"),
        Rule("transport_in_aoi",
             f"NOT ({transport}) OR (latitude BETWEEN {bb.south} AND {bb.north} "
             f"AND longitude BETWEEN {bb.west} AND {bb.east})", "warn",
             "Transportation events inside the configured bounding box"),
        Rule("not_future", "observed_at <= ingested_at + INTERVAL 5 MINUTES", "warn",
             "Observation not after publish (clock skew guard)"),
        Rule("publish_latency_reasonable",
             "ingested_at IS NULL OR ingested_at <= observed_at + INTERVAL 1 HOUR", "warn",
             "Published within an hour of observation (outbox backlog guard)"),
        Rule("severity_known",
             "severity IS NULL OR severity IN ('minor','moderate','major','critical')", "warn",
             "Severity in normalised set"),
        Rule("weather_has_geometry",
             "entity_type <> 'weather_alert' OR geometry_geojson IS NOT NULL", "warn",
             "Weather alerts carry polygon or zone geometry"),
    ]
