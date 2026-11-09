# ArcGIS GeoAnalytics for Microsoft Fabric

Reference: https://learn.microsoft.com/fabric/data-engineering/spark-arcgis-geoanalytics
and https://developers.arcgis.com/geoanalytics-fabric/

## Availability and licensing (verified 2026-10)

* Pre-installed in the Fabric Spark runtime. A Fabric admin enables it with
  the tenant setting **ArcGIS GeoAnalytics for Fabric Runtime** (OneLake
  catalog → Govern → Tenant settings). A capacity admin can override it per
  capacity.
* **Bring your own licence.** Every function and tool fails with
  `com.esri.geoanalytics.internal.AuthError: Not authorized` until you call
  `geoanalytics_fabric.auth(...)` with GeoAnalytics-for-Fabric credentials or an
  Esri API key. Usage is metered in core-hours (`auth_info()` →
  `available_hours`, `session_usage`).
* Not supported when workspace **Outbound Access Protection** is on, because
  auth calls Esri services.
* Delta writes store geometry as WKB. We write WKT/lat-lon columns explicitly,
  so Power BI and the fallback engine read the same tables.

## How MVOI uses it (`src/mvoi/lakehouse/geospatial.py`)

| Analysis | GeoAnalytics API | Fallback (pure Python) |
|---|---|---|
| Points | `ST.point(lon, lat, sr=4326)` | haversine on lat/lon |
| Incident → facility proximity | `ST.dwithin(..., geodesic=True)` join, `ST.geodesic_distance` | haversine |
| Weather exposure | `ST.geom_from_geojson(..., sr=4326)`, `ST.intersects` (+ alert window overlap) | ray-casting point-in-polygon |
| Corridor matching | geodesic `ST.dwithin` to TIGER lines, nearest by `ST.geodesic_distance` | point-to-segment distance |
| Hot spots | `FindHotSpots().setBins(1600, "Meters").setNeighborhood(4800, "Meters")` (projects to SRID 54034) | Getis-Ord Gi* on a square grid, same output columns (`value`, `GiZScore`, `GiPValue`, `Gi_Bin`) |
| Map | `df.st.plot(basemap="light", ...)` | matplotlib scatter |

`nb_mvoi_geospatial` parameter `engine`:
* `auto` (default): ArcGIS if importable **and** authorised, otherwise fallback.
* `arcgis`: fail rather than fall back (use when demonstrating ArcGIS).
* `fallback`: never touch ArcGIS.

Every output row records `spatial_engine`.

The API key is read from Key Vault secret `arcgis-api-key` (Key Vault URL from
`fabric_runtime.json`), never from code.

## Test status

| Path | Status |
|---|---|
| Fallback engine | **Tested** on local Spark 3.5 + Delta 3.2 (`tests/spark/test_medallion.py`) with the synthetic storyline |
| ArcGIS engine | **Requires deployment + Esri licence.** Function names and signatures were checked against Esri's documentation (`point(x, y, sr)`, `geom_from_geojson(json, sr)`, `dwithin(g1, g2, distance, geodesic)`, `FindHotSpots` output fields). Not executed: no licence was available in this environment. |
