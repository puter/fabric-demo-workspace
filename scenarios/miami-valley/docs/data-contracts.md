# Data contract — `NormalizedEvent` v1.0

One JSON envelope for every source and derived signal
(`src/mvoi/models/events.py`). The field names are also the column names in
the Eventhouse `Events` table and in the Lakehouse.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | `"1.0"` |
| `event_id` | uuid string | **Deterministic** UUIDv5 of entity_key + change_type + content_hash + run_mode + session (+ observed_at for snapshots). The same event published twice has the same id, and downstream de-duplicates on it. |
| `entity_key` | string | `<source>:<entity_type>:<source_id>` — stable identity of the thing |
| `source` | enum | `ohgo`, `nws`, `mvoi` (platform-generated) |
| `entity_type` | enum | `incident`, `construction`, `dangerous_slowdown`, `travel_delay`, `weather_alert`, `facility_risk`, `operational_alert`, `ingestion_heartbeat` |
| `source_id` | string | Source id; if missing, `h-<sha256>` of route, direction, coordinates and description |
| `change_type` | enum | `created`, `updated`, `resolved`, `reopened`, `snapshot` |
| `environment` | string | `dev` / `demo` / `prod` |
| `run_mode` | enum | `live` / `replay` |
| `replay_session_id` | string? | Set for replay |
| `observed_at` | UTC datetime | When the poll that saw this ran (replay: mapped onto the wall clock) |
| `source_updated_at` | UTC datetime? | OHGO `lastUpdated` / NWS `sent` |
| `first_seen_at`, `last_seen_at` | UTC datetime? | From lifecycle tracking |
| `start_time`, `end_time` | UTC datetime? | Construction start/end; NWS onset/ends (expires) |
| `ingested_at` | UTC datetime | Stamped by the publisher |
| `latitude`, `longitude` | double? | WGS84, validated. Weather: polygon centroid (for map labels only) |
| `geometry_geojson` | string? | Line (incident closure) or (Multi)Polygon (weather) |
| `route_name`, `corridor_id`, `direction`, `location` | string? | `corridor_id` from configured corridors |
| `category`, `status` | string? | Source category / road status / NWS event / risk band / alert state |
| `severity`, `severity_rank` | enum, int | `minor`=1, `moderate`=2, `major`=3, `critical`=4 |
| `is_road_closure` | bool | |
| `title`, `description` | string? | |
| `attributes` | object | Type-specific fields (below) |
| `content_hash` | sha256 | Of the content fields only; volatile `live_*` attributes are excluded |
| `raw` | object? | Lossless original source record (Bronze keeps it as `raw_json`) |

### Type-specific `attributes`

| entity_type | Keys |
|---|---|
| incident | corridor_match_method, closure_start, closure_end, synthetic? |
| construction | corridor_match_method, district, work_zone_project_id |
| dangerous_slowdown | normal_mph, speed_band, live_current_mph |
| travel_delay | delay_band, start/end_mile_marker, normal_avg_speed, live_delay_minutes, live_travel_time_minutes, live_current_avg_speed |
| weather_alert | cap_severity, certainty, urgency, response, sender_name, instruction, expires, zones, aoi_zones, references, geometry_source |
| facility_risk | score, band, raw_points, criticality, criticality_multiplier, incident/road_closure/slowdown/weather/history_points, nearby counts, nearest_incident_m, explanation, exposure_type=`inferred`, model_version, contributors[≤5], previous_score, previous_band |
| operational_alert | alert_type, alert_key, alert_state (`raised`/`cleared`), facility_id, facility_name, related_entity_key, score, distance_m |
| ingestion_heartbeat | polls[] (per source: ok, not_modified, observations, rejected, events_emitted, elapsed_ms, attempts, error, last_success_at, consecutive_failures), active_counts, active_keys (every tracked entity type → sorted active entity keys, `[]` when none; KQL hides rows not listed, [ADR 0005](../../../docs/adr/0005-heartbeat-active-set-reconciliation.md)), outbox_depth, service_started_at |
| any (resolved) | resolution_reason: `absent`, `expired`, `replay_reset` |

Compatibility rule: add fields only. A breaking change bumps `schema_version`,
and the KQL mapping and Silver handle both versions.
