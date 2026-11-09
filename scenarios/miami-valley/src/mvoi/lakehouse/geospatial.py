"""Geospatial enrichment for Gold, with two interchangeable engines.

* ArcGISEngine   — ArcGIS GeoAnalytics for Microsoft Fabric (`geoanalytics_fabric`):
                   ST_Point/ST_SRID geometry, geodesic ST_DWithin + ST_GeodesicDistance
                   proximity, ST_Intersects weather overlay, FindHotSpots (Gi*).
                   Requires the tenant setting AND an Esri licence (BYOL); see
                   docs/arcgis.md.
* FallbackEngine — pure Python (mvoi.geo) on the driver. Data volumes here
                   are small (thousands of occurrences, 20 facilities, tens of
                   alerts), so collecting is cheaper than shuffling.

Both write identical schemas plus a `spatial_engine` column, so Power BI and
the Real-Time Dashboard never care which one ran.

Outputs:
  gold_facility_proximity       occurrence x facility within max distance
  gold_weather_exposure         NWS alert x facility intersections
  gold_incident_weather         occurrence inside an alert polygon during its window
  gold_corridor_match           occurrences re-matched to corridors spatially
  gold_hotspots                 Gi* bins (distinct occurrences, optional minutes weighting)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from mvoi.common.config import Geography
from mvoi.geo.core import haversine_m, parse_geometry, point_in_polygon, point_line_distance_m
from mvoi.geo.hotspot import find_hot_spots
from mvoi.lakehouse.spark_jobs import Tables

log = logging.getLogger(__name__)

PROXIMITY_SCHEMA = ("occurrence_id string, facility_id string, facility_name string, distance_m double, "
                    "within_full_weight boolean, started_at timestamp, peak_severity string, "
                    "had_road_closure boolean, corridor string, spatial_engine string")
WEATHER_FAC_SCHEMA = ("alert_key string, event string, cap_severity string, onset timestamp, "
                      "expires timestamp, facility_id string, facility_name string, spatial_engine string")
WEATHER_INC_SCHEMA = ("occurrence_id string, alert_key string, event string, cap_severity string, "
                      "spatial_engine string")
CORRIDOR_SCHEMA = ("occurrence_id string, attribute_corridor string, spatial_corridor string, "
                   "spatial_distance_m double, final_corridor string, match_method string, "
                   "spatial_engine string")
HOTSPOT_SCHEMA = ("bin_id string, center_lat double, center_lon double, bin_wkt string, value double, "
                  "GiZScore double, GiPValue double, Gi_Bin int, weighting string, bin_size_m double, "
                  "neighborhood_m double, spatial_engine string")


def _write(df: DataFrame, table: str) -> None:
    # Date keys for the semantic model's Date dimension.
    for src, dst in (("started_at", "start_date"), ("onset", "onset_date")):
        if src in df.columns and dst not in df.columns:
            df = df.withColumn(dst, F.to_date(src))
    df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table)


def _alerts(spark: SparkSession, t: Tables) -> list[dict[str, Any]]:
    """Distinct weather alerts (latest record per alert) with geometry and window."""
    rows = (spark.table(t.silver_events).where("entity_type = 'weather_alert'")
            .where("geometry_geojson IS NOT NULL")
            .groupBy("entity_key")
            .agg(F.max_by(F.struct("category", "geometry_geojson", "start_time", "end_time",
                                   "attributes_json", "first_seen_at"), "observed_at").alias("a"),
                 F.max(F.when(F.col("change_type") == "resolved", F.col("observed_at"))).alias("resolved_at"))
            .collect())
    out = []
    for r in rows:
        a = r.a
        attrs = json.loads(a.attributes_json or "{}")
        out.append({"alert_key": r.entity_key, "event": a.category,
                    "cap_severity": attrs.get("cap_severity"),
                    "onset": a.start_time or a.first_seen_at, "expires": a.end_time or r.resolved_at,
                    "geometry_geojson": a.geometry_geojson})
    return out


# =============================================================================== fallback


class FallbackEngine:
    name = "fallback-python"

    def __init__(self, spark: SparkSession, geo: Geography):
        self.spark = spark
        self.geo = geo

    def proximity(self, t: Tables, max_m: float, full_m: float) -> None:
        occ = self.spark.table(t.gold_incident_occurrences).where("latitude IS NOT NULL").collect()
        fac = self.spark.table(t.silver_facilities).collect()
        rows = []
        for o in occ:
            for f in fac:
                d = haversine_m(f.latitude, f.longitude, o.latitude, o.longitude)
                if d <= max_m:
                    rows.append((o.occurrence_id, f.facility_id, f.facility_name, round(d, 1),
                                 d <= full_m, o.started_at, o.peak_severity, o.had_road_closure,
                                 o.corridor, self.name))
        _write(self.spark.createDataFrame(rows, PROXIMITY_SCHEMA), t.gold_facility_proximity)

    def weather(self, t: Tables) -> None:
        alerts = _alerts(self.spark, t)
        fac = self.spark.table(t.silver_facilities).collect()
        occ = self.spark.table(t.gold_incident_occurrences).where("latitude IS NOT NULL").collect()
        fac_rows, inc_rows = [], []
        for a in alerts:
            g = parse_geometry(a["geometry_geojson"])
            for f in fac:
                if point_in_polygon(f.latitude, f.longitude, g):
                    fac_rows.append((a["alert_key"], a["event"], a["cap_severity"], a["onset"],
                                     a["expires"], f.facility_id, f.facility_name, self.name))
            for o in occ:
                overlaps = (a["onset"] is None or o.ended_at is None or o.ended_at >= a["onset"]) and \
                           (a["expires"] is None or o.started_at <= a["expires"])
                if overlaps and point_in_polygon(o.latitude, o.longitude, g):
                    inc_rows.append((o.occurrence_id, a["alert_key"], a["event"], a["cap_severity"],
                                     self.name))
        _write(self.spark.createDataFrame(fac_rows, WEATHER_FAC_SCHEMA), t.gold_weather_exposure)
        _write(self.spark.createDataFrame(inc_rows, WEATHER_INC_SCHEMA), t.gold_incident_weather)

    def corridors(self, t: Tables) -> None:
        cor = [(r.corridor_id, parse_geometry(r.geometry_geojson))
               for r in self.spark.table(t.silver_corridors).collect()]
        snap = self.geo.corridor_snap_distance_m
        rows = []
        for o in self.spark.table(t.gold_incident_occurrences).where("latitude IS NOT NULL").collect():
            best, best_d = None, None
            for cid, g in cor:
                d = point_line_distance_m(o.latitude, o.longitude, g)
                if best_d is None or d < best_d:
                    best, best_d = cid, d
            spatial = best if best_d is not None and best_d <= snap else None
            final = o.corridor_id or spatial
            method = "attribute" if o.corridor_id else ("spatial" if spatial else "none")
            rows.append((o.occurrence_id, o.corridor_id, spatial,
                         round(best_d, 1) if best_d is not None else None, final, method, self.name))
        _write(self.spark.createDataFrame(rows, CORRIDOR_SCHEMA), t.gold_corridor_match)

    def hotspots(self, t: Tables, bin_m: float, neigh_m: float, weighting: str) -> None:
        occ = (self.spark.table(t.gold_incident_occurrences)
               .where("entity_type = 'incident' AND latitude IS NOT NULL")
               .select("latitude", "longitude", "duration_minutes").collect())
        pts = [(o.latitude, o.longitude, 1.0 if weighting == "occurrences"
                else float(o.duration_minutes or 0)) for o in occ]
        bins = find_hot_spots(pts, self.geo.bounding_box, bin_m, neigh_m)
        rows = [(b.bin_id, b.center_lat, b.center_lon, b.wkt, b.value, b.GiZScore, b.GiPValue,
                 b.Gi_Bin, weighting, float(bin_m), float(neigh_m), self.name) for b in bins]
        _write(self.spark.createDataFrame(rows, HOTSPOT_SCHEMA), t.gold_hotspots)


# =============================================================================== ArcGIS


class ArcGISEngine:
    """ArcGIS GeoAnalytics for Fabric. Function names per
    https://developers.arcgis.com/geoanalytics-fabric/sql-functions/ ."""

    name = "arcgis-geoanalytics"

    def __init__(self, spark: SparkSession, geo: Geography):
        from geoanalytics_fabric.sql import functions as ST  # noqa: N812 (Esri convention)

        self.ST = ST
        self.spark = spark
        self.geo = geo

    def _points(self, df: DataFrame) -> DataFrame:
        ST = self.ST
        return df.withColumn("geom", ST.point("longitude", "latitude", sr=4326))

    def proximity(self, t: Tables, max_m: float, full_m: float) -> None:
        ST = self.ST
        occ = self._points(self.spark.table(t.gold_incident_occurrences).where("latitude IS NOT NULL")) \
            .alias("o")
        fac = self._points(self.spark.table(t.silver_facilities)).alias("f")
        j = (occ.join(fac, ST.dwithin(F.col("o.geom"), F.col("f.geom"), max_m, geodesic=True))
             .select("o.occurrence_id", "f.facility_id", "f.facility_name",
                     ST.geodesic_distance(F.col("o.geom"), F.col("f.geom")).alias("distance_m"),
                     "o.started_at", "o.peak_severity", "o.had_road_closure", "o.corridor"))
        out = (j.withColumn("within_full_weight", F.col("distance_m") <= full_m)
                .withColumn("spatial_engine", F.lit(self.name))
                .select("occurrence_id", "facility_id", "facility_name", F.round("distance_m", 1)
                        .alias("distance_m"), "within_full_weight", "started_at", "peak_severity",
                        "had_road_closure", "corridor", "spatial_engine"))
        _write(out, t.gold_facility_proximity)

    def weather(self, t: Tables) -> None:
        ST = self.ST
        alerts = self.spark.createDataFrame(
            [(a["alert_key"], a["event"], a["cap_severity"], a["onset"], a["expires"], a["geometry_geojson"])
             for a in _alerts(self.spark, t)],
            "alert_key string, event string, cap_severity string, onset timestamp, expires timestamp, "
            "geometry_geojson string")
        # GeoJSON-embedded SRs are ignored by GeoAnalytics; set 4326 explicitly.
        alerts = alerts.withColumn("poly", ST.geom_from_geojson("geometry_geojson", sr=4326)).alias("a")
        fac = self._points(self.spark.table(t.silver_facilities)).alias("f")
        wf = (alerts.join(fac, ST.intersects(F.col("a.poly"), F.col("f.geom")))
              .select("a.alert_key", "a.event", "a.cap_severity", "a.onset", "a.expires",
                      "f.facility_id", "f.facility_name")
              .withColumn("spatial_engine", F.lit(self.name)))
        _write(wf, t.gold_weather_exposure)
        occ = self._points(self.spark.table(t.gold_incident_occurrences).where("latitude IS NOT NULL")) \
            .alias("o")
        overlap = ((F.col("a.onset").isNull() | F.col("o.ended_at").isNull()
                    | (F.col("o.ended_at") >= F.col("a.onset")))
                   & (F.col("a.expires").isNull() | (F.col("o.started_at") <= F.col("a.expires"))))
        wi = (alerts.join(occ, ST.intersects(F.col("a.poly"), F.col("o.geom")) & overlap)
              .select("o.occurrence_id", "a.alert_key", "a.event", "a.cap_severity")
              .withColumn("spatial_engine", F.lit(self.name)))
        _write(wi, t.gold_incident_weather)

    def corridors(self, t: Tables) -> None:
        ST = self.ST
        snap = self.geo.corridor_snap_distance_m
        cor = (self.spark.table(t.silver_corridors)
               .withColumn("line", ST.geom_from_geojson("geometry_geojson", sr=4326))
               .select(F.col("corridor_id").alias("c_id"), "line"))
        occ = self._points(self.spark.table(t.gold_incident_occurrences).where("latitude IS NOT NULL"))
        cand = (occ.join(cor, ST.dwithin(F.col("geom"), F.col("line"), snap, geodesic=True), "left")
                .withColumn("d", ST.geodesic_distance(F.col("geom"), F.col("line"))))
        best = (cand.groupBy("occurrence_id", "corridor_id")
                .agg(F.min_by("c_id", "d").alias("spatial_corridor"), F.min("d").alias("spatial_distance_m")))
        out = (best.withColumn("final_corridor", F.coalesce("corridor_id", "spatial_corridor"))
               .withColumn("match_method", F.when(F.col("corridor_id").isNotNull(), "attribute")
                           .when(F.col("spatial_corridor").isNotNull(), "spatial").otherwise("none"))
               .select("occurrence_id", F.col("corridor_id").alias("attribute_corridor"),
                       "spatial_corridor", F.round("spatial_distance_m", 1).alias("spatial_distance_m"),
                       "final_corridor", "match_method")
               .withColumn("spatial_engine", F.lit(self.name)))
        _write(out, t.gold_corridor_match)

    def hotspots(self, t: Tables, bin_m: float, neigh_m: float, weighting: str) -> None:
        from geoanalytics_fabric.tools import FindHotSpots

        ST = self.ST
        occ = (self.spark.table(t.gold_incident_occurrences)
               .where("entity_type = 'incident' AND latitude IS NOT NULL"))
        pts = self._points(occ)
        if weighting != "occurrences":
            # FindHotSpots counts points per bin; approximate minute-weighting by
            # replicating each point once per started hour of duration (capped).
            pts = pts.withColumn("_rep", F.explode(F.sequence(F.lit(1), F.least(
                F.greatest(F.ceil(F.col("duration_minutes") / 60), F.lit(1)), F.lit(48)).cast("int"))))
        res = (FindHotSpots()
               .setBins(bin_size=bin_m, bin_size_unit="Meters")
               .setNeighborhood(distance=neigh_m, distance_unit="Meters")
               .run(dataframe=pts.select("geom")))
        wgs = ST.transform("bin_geometry", 4326)
        out = (res.withColumn("bin_wgs", wgs)
               .withColumn("centroid", ST.centroid("bin_wgs"))
               .select(F.monotonically_increasing_id().cast("string").alias("bin_id"),
                       ST.y("centroid").alias("center_lat"), ST.x("centroid").alias("center_lon"),
                       ST.as_text("bin_wgs").alias("bin_wkt"), F.col("value").cast("double"),
                       F.col("GiZScore").cast("double"), F.col("GiPValue").cast("double"),
                       F.col("Gi_Bin").cast("int"))
               .withColumn("weighting", F.lit(weighting))
               .withColumn("bin_size_m", F.lit(float(bin_m)))
               .withColumn("neighborhood_m", F.lit(float(neigh_m)))
               .withColumn("spatial_engine", F.lit(self.name)))
        _write(out, t.gold_hotspots)


def select_engine(spark: SparkSession, geo: Geography, prefer: str = "auto",
                  arcgis_auth: dict[str, str] | None = None):
    """'auto' -> ArcGIS when importable and authorised, else fallback. Never fails the run."""
    if prefer == "fallback":
        return FallbackEngine(spark, geo)
    try:
        import geoanalytics_fabric

        if arcgis_auth:
            geoanalytics_fabric.auth(**arcgis_auth)
        info = {r["name"]: r["value"] for r in geoanalytics_fabric.auth_info().collect()}
        if not info:
            raise RuntimeError("ArcGIS GeoAnalytics is not authorised (no licence/API key)")
        log.info("using ArcGIS GeoAnalytics", extra={"billing_type": info.get("billing_type"),
                                                     "available_hours": info.get("available_hours")})
        return ArcGISEngine(spark, geo)
    except Exception as exc:  # noqa: BLE001 — ImportError, AuthError, tenant setting off
        if prefer == "arcgis":
            raise
        log.warning("ArcGIS unavailable, using fallback engine: %s", exc)
        return FallbackEngine(spark, geo)
