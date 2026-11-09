# Data sources

## 1. ODOT OHGO Public API (primary)

* **Docs:** https://publicapi.ohgo.com/ (Swagger: `/docs/v1/swagger.json`,
  vendored at `tests/fixtures/ohgo_swagger_v1.json`)
* **Auth:** free API key from registration, sent as `Authorization: APIKEY <key>`.
  Key source: env `OHGO_API_KEY` or Key Vault secret `ohgo-api-key`.
* **Resources used:**

| Resource | Endpoint | Fields read (exact names from swagger) | `entity_type` |
|---|---|---|---|
| Incidents | `/api/v1/incidents` | id, latitude, longitude, location, description, category, direction, routeName, roadStatus, roadClosureDetails{closureStartLocation, closureEndLocation, polyline} | `incident` |
| Construction | `/api/v1/construction` (`exclude-work-zone`) | id, latitude, longitude, location, description, category, direction, district, routeName, status, startDate, endDate, workZoneProjectId | `construction` |
| Dangerous slowdowns | `/api/v1/dangerous-slowdowns` | id, latitude, longitude, location, description, normalMPH, currentMPH, routeName, direction | `dangerous_slowdown` |
| Travel delays | `/api/v1/travel-delays` | id, latitude, longitude, location, description, direction, routeName, travelTime, delayTime, startMileMarker, endMileMarker, currentAvgSpeed, normalAvgSpeed | `travel_delay` |

Envelope: `lastUpdated`, `rejectedFilters`, `totalResultCount`, `results`
(and `accecptedFilters`, misspelled in the API itself).

**Geographic filter:** `map-bounds-sw` / `map-bounds-ne` from
`config/geography.yaml` (default N 40.25, S 39.45, W −84.60, E −83.65) plus
`page-all=true`. The bounding box is also enforced client-side.

**Known documentation ambiguities (verify with the live smoke test once a key is available):**

| Ambiguity | Our handling |
|---|---|
| `map-bounds-*` text says "(longitude,latitude)" but every example is latitude-first (`41.011,-82.368` is only valid in Ohio as lat,lon) | We send `lat,lon` per the examples. `test_ohgo_resources_parse_and_respect_bbox` asserts all returned points fall inside the box and that `rejectedFilters` is empty. The poller logs `rejectedFilters`. |
| Swagger maps `/travel-delays` to the Incident schema; the HTML docs describe a separate Travel Delay model | We follow the HTML Travel Delay model; unknown fields are ignored |
| No timestamps on incidents/slowdowns/delays | `first_seen_at` / `last_seen_at` come from our own observations; `source_updated_at` = envelope `lastUpdated` |
| No severity field | Derived by explicit rules (below) |
| Category / roadStatus value lists are not enumerated | Severity rules use case-insensitive keyword matching, not fixed enums |
| Rate limit: HTTP 429 is documented ("x requests in a timeframe"), but the limit is not | `Retry-After` honoured, exponential backoff with jitter, ETag `If-None-Match` to avoid re-downloading unchanged data, 30 s minimum interval enforced in config |

**Polling and freshness:** 60 s for OHGO (4 requests/min, about 5,800/day).
With no published limit, 60 s is the target from the brief. If 429s appear in
the heartbeat (`attempts > 1`), raise `polling.interval_seconds`. Freshness is
the poll interval plus pipeline latency (see architecture.md). The upstream
refresh cadence is not published.

### Severity rules (`mvoi/sources/severity.py`)

| Entity | Critical | Major | Moderate | Minor |
|---|---|---|---|---|
| Incident | text mentions fatal, hazmat, wrong-way, bridge strike/collapse, tanker | `roadStatus` closed | partially closed, or crash / accident / collision / debris / disabled | otherwise |
| Construction | — | — | status closed | otherwise |
| Dangerous slowdown | — | speed ≥ 60% below normal | 40–60% below | < 40% below |
| Travel delay | — | delay ≥ 20 min | 10–20 min | < 10 min |
| NWS alert | CAP Extreme | Severe | Moderate | Minor / Unknown |

Volatile readings (current mph, minutes of delay) are banded before change
detection, so 2 mph of jitter is not an "update". Raw values stay in
`attributes.live_*`.

## 2. National Weather Service (secondary)

* **Docs:** https://www.weather.gov/documentation/services-web-api
* **Endpoint:** `GET /alerts/active?area=OH&status=actual` (GeoJSON, CAP
  properties). No key; a descriptive `User-Agent` with a contact is required
  (`MVOI_NWS__USER_AGENT`). Polled every 120 s. Responses are cacheable for a
  few seconds.
* **Fields:** id, areaDesc, geocode.UGC, affectedZones, references, sent,
  effective, onset, expires, ends, status, messageType, category, severity,
  certainty, urgency, event, senderName, headline, description, instruction,
  response.
* **Area of interest:** keep an alert when its polygon's extent overlaps the
  bounding box, **or** (no polygon) any of its zones is one of the 28 forecast
  and county zones in `geography.yaml`.
* **Zone-only alerts** (most winter, flood and heat products have
  `geometry: null`) get geometry built from the committed NWS zone polygons
  (`reference-data/nws_zones.geojson`). `attributes.geometry_source` records
  `alert_polygon` or `nws_zones`.
* **Resolution:** an alert resolves when it disappears for the configured
  polls, **or** when `ends` / `expires` passes (`resolution_reason = "expired"`).

## 3. Monitored sites (public)

`reference-data/facilities.csv`: 20 real, publicly listed sites, centred on the
demo host site, **Sinclair Conference Center** (FAC-001, Sinclair Community
College, 444 W Third St, Dayton). The others are downtown venues, the airport,
hospitals, a retail district and major regional employers across Dayton,
Vandalia, Kettering, Moraine, Miamisburg, Beavercreek, Troy, Enon and
Springfield.

* **Coordinates** come from the [US Census Geocoder](https://geocoding.geo.census.gov/)
  (Public_AR_Current benchmark) for each street address. These are interpolated
  along the street, so accuracy is roughly street level (tens of metres).
* **Not affiliated:** every row has `is_affiliated = false`. MVOI scores
  *travel-disruption exposure near* a site from public traffic and weather data.
  It says nothing about a site's operations, and no site endorses the demo.
* **Criticality** (1–5) is our own site-importance weight for the exposure
  score. It is not a rating of the organisation.
* Before an event, check that each business still operates at its listed
  address.

Columns: id, name, type (venue, airport, hospital, landmark, retail,
employer), lat/lon, city, criticality, primary corridor, address, description,
is_affiliated.

## 4. Highway geography (public domain)

`reference-data/corridors.geojson` is built by
`scripts/build_reference_data.py` from **US Census TIGER/Line 2025 Primary &
Secondary Roads, Ohio** (`tl_2025_39_prisecroads`). It covers I-75, I-70, I-675,
US-35, SR-4, US-40, SR-444, US-36, SR-48 and US-68, clipped to the area of
interest and simplified to about 10 m. Corridor matching works in two steps:
first the OHGO `routeName` against configured aliases (`I-75`, `IR-75`, …),
then the nearest corridor within 300 m. The method is recorded per event.

`reference-data/nws_zones.geojson` holds the NWS forecast and county zone
polygons from `api.weather.gov/zones`, also public domain.
