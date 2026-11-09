"""Spark medallion jobs (Bronze -> Silver -> Gold) for lh_mvoi.

Notebooks in fabric/notebooks/ are thin wrappers around these functions so
the same code runs in Fabric and in the local/CI Spark test-suite
(tests/spark/). All writes are idempotent:

  bronze_events               append, watermark on Eventhouse ingestion_time()
  silver_events               MERGE on event_id (dedup), live rows only
  silver_events_rejected      append (error-severity DQ failures)
  silver_dq_results           append, one row per rule per run
  silver_entity_state         overwrite: latest state per entity
  gold_incident_occurrences   overwrite (derived from the full silver history)
  gold_*                      overwrite (small, recomputed each run)

Replay isolation: Bronze keeps everything (lossless, including replay rows,
for audit); Silver and Gold filter run_mode = 'live'.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

from mvoi.common.config import Geography, RiskConfig
from mvoi.lakehouse import dq
from mvoi.lakehouse.occurrences import OCCURRENCE_COLUMNS, build_occurrences

TRANSPORT = ["incident", "construction", "dangerous_slowdown", "travel_delay"]

# Column order of the canonical contract (Eventhouse `Events` minus system columns).
EVENT_COLUMNS = [
    "schema_version", "event_id", "entity_key", "source", "entity_type", "source_id", "change_type",
    "environment", "run_mode", "replay_session_id", "observed_at", "source_updated_at",
    "first_seen_at", "last_seen_at", "start_time", "end_time", "ingested_at", "latitude",
    "longitude", "geometry_geojson", "route_name", "corridor_id", "direction", "location",
    "category", "status", "severity", "severity_rank", "is_road_closure", "title", "description",
    "attributes_json", "content_hash", "raw_json",
]


class Tables:
    """Physical table naming; `prefix` lets tests use a scratch database."""

    def __init__(self, prefix: str = ""):
        self.p = prefix

    def __getattr__(self, name: str) -> str:
        return f"{self.p}{name}"


def _exists(spark: SparkSession, table: str) -> bool:
    return spark.catalog.tableExists(table)


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- bronze

def bronze_kusto_query(watermark: datetime | None, lag_seconds: int = 60) -> str:
    """KQL that returns new Events rows since the watermark, dynamics as JSON text."""
    since = f"datetime({watermark.isoformat()})" if watermark else "datetime(1970-01-01)"
    # Bracket every name: some columns (e.g. title) are reserved words in KQL.
    cols = ", ".join(f"['{c}']" for c in EVENT_COLUMNS if c not in ("attributes_json", "raw_json"))
    return (
        "Events\n"
        f"| extend kusto_ingestion_time = ingestion_time()\n"
        f"| where kusto_ingestion_time > {since} and kusto_ingestion_time <= ago({lag_seconds}s)\n"
        f"| project {cols}, attributes_json = tostring(attributes), raw_json = tostring(raw),\n"
        "          kusto_ingestion_time"
    )


def bronze_append(spark: SparkSession, batch: DataFrame, t: Tables) -> int:
    """Append a batch pulled from Eventhouse. Lossless: every source column is kept."""
    if batch.rdd.isEmpty():
        return 0
    out = (batch
           .withColumn("bronze_loaded_at", F.current_timestamp())
           .withColumn("event_date", F.to_date("observed_at")))
    n = out.count()
    out.write.format("delta").mode("append").partitionBy("event_date").saveAsTable(t.bronze_events)
    return n


def bronze_watermark(spark: SparkSession, t: Tables) -> datetime | None:
    if not _exists(spark, t.bronze_events):
        return None
    v = spark.table(t.bronze_events).agg(F.max("kusto_ingestion_time")).first()[0]
    return v.replace(tzinfo=UTC) if v is not None and v.tzinfo is None else v


# --------------------------------------------------------------------------- silver

def silver_build(spark: SparkSession, geo: Geography, t: Tables, run_id: str | None = None) -> dict:
    """Bronze -> silver_events (dedup MERGE), rejects, DQ results, entity state."""
    run_id = run_id or _now().strftime("%Y%m%dT%H%M%S")
    bronze = spark.table(t.bronze_events)
    since = None
    if _exists(spark, t.silver_events):
        since = spark.table(t.silver_events).agg(F.max("bronze_loaded_at")).first()[0]
    if since is not None:
        bronze = bronze.where(F.col("bronze_loaded_at") > F.lit(since))

    live = bronze.where(F.col("run_mode") == "live")
    # Typed + UTC: Kusto datetimes arrive as UTC timestamps; enforce numeric types.
    typed = (live
             .withColumn("latitude", F.col("latitude").cast("double"))
             .withColumn("longitude", F.col("longitude").cast("double"))
             .withColumn("severity_rank", F.col("severity_rank").cast("int"))
             .withColumn("is_road_closure", F.coalesce(F.col("is_road_closure").cast("boolean"),
                                                       F.lit(False))))
    # Dedup inside the batch (at-least-once delivery => same event_id twice).
    w = Window.partitionBy("event_id").orderBy(F.col("kusto_ingestion_time").asc())
    typed = typed.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")

    rules = dq.rules(geo)
    for r in rules:
        typed = typed.withColumn(f"_dq_{r.name}", F.coalesce(F.expr(r.expression), F.lit(False)))
    errors = [f"_dq_{r.name}" for r in rules if r.severity == "error"]
    warns = [f"_dq_{r.name}" for r in rules if r.severity == "warn"]
    typed = (typed
             .withColumn("dq_error", ~F.expr(" AND ".join(f"`{c}`" for c in errors)))
             .withColumn("dq_warnings", F.array_compact(F.array(*[
                 F.when(~F.col(c), F.lit(c[4:])) for c in warns]))))
    typed.cache()

    total = typed.count()
    dq_rows = typed.select([F.sum(F.when(~F.col(f"_dq_{r.name}"), 1).otherwise(0)).alias(r.name)
                            for r in rules]).first().asDict() if total else {r.name: 0 for r in rules}
    dq_df = spark.createDataFrame(
        [(run_id, _now(), r.name, r.severity, r.description, total, int(dq_rows[r.name] or 0))
         for r in rules],
        "run_id string, run_at timestamp, rule string, severity string, description string, "
        "rows_checked long, rows_failed long")
    dq_df = dq_df.withColumn("run_date", F.to_date("run_at"))
    dq_df.write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable(t.silver_dq_results)

    drop = [c for c in typed.columns if c.startswith("_dq_")]
    rejected = typed.where("dq_error").drop(*drop)
    good = typed.where("NOT dq_error").drop(*drop, "dq_error")
    n_rej = rejected.count()
    if n_rej:
        rejected.withColumn("rejected_run_id", F.lit(run_id)) \
            .write.format("delta").mode("append").saveAsTable(t.silver_events_rejected)

    n_good = good.count()
    if not _exists(spark, t.silver_events):
        good.write.format("delta").partitionBy("event_date").saveAsTable(t.silver_events)
    elif n_good:
        good.createOrReplaceTempView("_silver_batch")
        spark.sql(f"""
            MERGE INTO {t.silver_events} AS s
            USING _silver_batch AS b
            ON s.event_id = b.event_id
            WHEN NOT MATCHED THEN INSERT *
        """)
    typed.unpersist()

    _entity_state(spark, t)
    return {"run_id": run_id, "checked": total, "accepted": n_good, "rejected": n_rej}


def _entity_state(spark: SparkSession, t: Tables) -> None:
    ev = spark.table(t.silver_events)
    w = Window.partitionBy("entity_key").orderBy(F.col("observed_at").desc(),
                                                 F.col("kusto_ingestion_time").desc())
    latest = (ev.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")
              .withColumn("is_active", F.col("change_type") != "resolved"))
    latest.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
        .saveAsTable(t.silver_entity_state)


# --------------------------------------------------------------------------- gold

_OCC_SCHEMA = T.StructType([
    T.StructField("occurrence_id", T.StringType()), T.StructField("entity_key", T.StringType()),
    T.StructField("entity_type", T.StringType()), T.StructField("occurrence_seq", T.IntegerType()),
    T.StructField("started_at", T.TimestampType()), T.StructField("ended_at", T.TimestampType()),
    T.StructField("is_open", T.BooleanType()), T.StructField("duration_minutes", T.DoubleType()),
    T.StructField("latitude", T.DoubleType()), T.StructField("longitude", T.DoubleType()),
    T.StructField("corridor_id", T.StringType()), T.StructField("route_name", T.StringType()),
    T.StructField("category", T.StringType()), T.StructField("peak_severity", T.StringType()),
    T.StructField("peak_severity_rank", T.IntegerType()),
    T.StructField("had_road_closure", T.BooleanType()), T.StructField("update_count", T.IntegerType()),
    T.StructField("resolution_reason", T.StringType()),
])
assert [f.name for f in _OCC_SCHEMA.fields] == OCCURRENCE_COLUMNS


def gold_occurrences(spark: SparkSession, t: Tables, as_of: datetime | None = None) -> DataFrame:
    """Distinct incident occurrences via the shared pure-Python builder (applyInPandas)."""
    as_of = as_of or _now()
    ev = (spark.table(t.silver_events)
          .where(F.col("entity_type").isin(TRANSPORT))
          .select("event_id", "entity_key", "entity_type", "change_type", "observed_at",
                  "first_seen_at", "last_seen_at", "severity", "is_road_closure", "latitude",
                  "longitude", "corridor_id", "route_name", "category",
                  F.get_json_object("attributes_json", "$.resolution_reason")
                  .alias("resolution_reason")))

    def per_entity(pdf):
        import pandas as pd
        recs = []
        for r in pdf.to_dict("records"):
            for k in ("observed_at", "first_seen_at", "last_seen_at"):
                v = r.get(k)
                r[k] = None if v is None or pd.isna(v) else pd.Timestamp(v).tz_localize(None) \
                    .tz_localize("UTC").to_pydatetime()
            r["attributes"] = {"resolution_reason": r.pop("resolution_reason")}
            recs.append(r)
        occ = build_occurrences(recs, as_of)
        df = pd.DataFrame(occ, columns=OCCURRENCE_COLUMNS)
        for k in ("started_at", "ended_at"):
            df[k] = pd.to_datetime(df[k], utc=True).dt.tz_localize(None)
        return df

    occ = ev.groupBy("entity_key").applyInPandas(per_entity, schema=_OCC_SCHEMA)
    occ = (occ.withColumn("start_date", F.to_date("started_at"))
              .withColumn("start_hour", F.hour("started_at"))
              .withColumn("start_dow", F.date_format("started_at", "E"))
              .withColumn("corridor", F.coalesce("corridor_id", F.lit("Other / unmatched"))))
    occ.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
        .saveAsTable(t.gold_incident_occurrences)
    return spark.table(t.gold_incident_occurrences)


def gold_aggregates(spark: SparkSession, t: Tables) -> None:
    occ = spark.table(t.gold_incident_occurrences)

    (occ.groupBy("start_date", "start_hour", "start_dow", "corridor", "entity_type", "peak_severity")
        .agg(F.count("*").alias("occurrences"),
             F.sum("duration_minutes").alias("incident_minutes"),
             F.sum(F.col("had_road_closure").cast("int")).alias("closures"))
        .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
        .saveAsTable(t.gold_incident_trends_hourly))

    (occ.groupBy("start_date", "corridor")
        .agg(F.count("*").alias("occurrences"),
             F.sum(F.col("had_road_closure").cast("int")).alias("closures"),
             F.sum(F.when(F.col("had_road_closure"), F.col("duration_minutes"))).alias("closure_minutes"),
             F.sum("duration_minutes").alias("incident_minutes"),
             F.expr("percentile_approx(duration_minutes, 0.5)").alias("median_duration_minutes"),
             F.max("peak_severity_rank").alias("max_severity_rank"))
        .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
        .saveAsTable(t.gold_corridor_disruption_daily))

    state = spark.table(t.silver_entity_state)
    (state.where(F.col("entity_type").isin(TRANSPORT) & F.col("is_active"))
          .select("entity_key", "entity_type", "category", "severity", "severity_rank",
                  "is_road_closure", "corridor_id", "route_name", "direction", "location", "title",
                  "latitude", "longitude", "first_seen_at", "last_seen_at")
          .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
          .saveAsTable(t.gold_active_incidents))

    ev = spark.table(t.silver_events)
    risk = (ev.where("entity_type = 'facility_risk'")
              .select(F.col("source_id").alias("facility_id"), F.col("title").alias("facility_name"),
                      "observed_at",
                      F.get_json_object("attributes_json", "$.score").cast("double").alias("score"),
                      F.col("status").alias("band"),
                      *[F.get_json_object("attributes_json", f"$.{c}").cast("double").alias(c)
                        for c in ("incident_points", "road_closure_points", "slowdown_points",
                                  "weather_points", "history_points")],
                      F.col("description").alias("explanation"))
              .withColumn("risk_date", F.to_date("observed_at")))
    risk.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
        .saveAsTable(t.gold_facility_risk_history)

    hb = (ev.where("entity_type = 'ingestion_heartbeat'")
            .select("observed_at", "ingested_at", "kusto_ingestion_time", "status",
                    F.from_json("attributes_json", "polls array<struct<source:string, entity_type:string,"
                                " ok:boolean, not_modified:boolean, observations:int, rejected:int,"
                                " elapsed_ms:double, attempts:int>>").alias("a"))
            .select("*", F.explode_outer("a.polls").alias("p")))
    (hb.groupBy(F.to_date("observed_at").alias("date"),
                F.concat_ws(":", "p.source", "p.entity_type").alias("source"))
       .agg(F.count("*").alias("polls"),
            F.sum(F.when(~F.col("p.ok"), 1).otherwise(0)).alias("failed_polls"),
            F.sum("p.observations").alias("observations"),
            F.sum("p.rejected").alias("rejected"),
            F.expr("percentile_approx(p.elapsed_ms, 0.95)").cast("double").alias("p95_source_latency_ms"),
            F.expr("percentile_approx(unix_timestamp(kusto_ingestion_time) - unix_timestamp(observed_at),"
                   " 0.95)").cast("double").alias("p95_end_to_end_s"))
       .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
       .saveAsTable(t.gold_pipeline_health_daily))


def gold_reference(spark: SparkSession, t: Tables, facilities_csv: str, corridors_geojson: str,
                   zones_geojson: str) -> None:
    """Reference tables from the files deployed to Files/mvoi/reference-data/."""
    fac = spark.read.option("header", True).option("inferSchema", True).csv(facilities_csv)
    fac.write.format("delta").mode("overwrite").option("overwriteSchema", "true") \
        .saveAsTable(t.silver_facilities)
    for path, table, key, name in ((corridors_geojson, t.silver_corridors, "corridor_id", "corridor_name"),
                                   (zones_geojson, t.silver_zones, "zone_id", "name")):
        with open(path, encoding="utf-8") as fh:
            fc = json.load(fh)
        rows = [(f["properties"][key], f["properties"].get(name), json.dumps(f["geometry"]))
                for f in fc["features"]]
        spark.createDataFrame(rows, f"{key} string, name string, geometry_geojson string") \
            .write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table)


def gold_dimensions(spark: SparkSession, t: Tables, geo: Geography, days_ahead: int = 7) -> None:
    """Conformed Date and Corridor dimensions for the Direct Lake semantic model."""
    occ = spark.table(t.gold_incident_occurrences)
    lo = occ.agg(F.min("start_date")).first()[0] or _now().date()
    hi = _now().date() + timedelta(days=days_ahead)
    lo = min(lo, hi - timedelta(days=365))
    dates = (spark.sql(f"SELECT explode(sequence(DATE'{lo.isoformat()}', DATE'{hi.isoformat()}')) AS date")
             .withColumn("year", F.year("date"))
             .withColumn("quarter", F.concat(F.lit("Q"), F.quarter("date")))
             .withColumn("month_number", F.month("date"))
             .withColumn("month_name", F.date_format("date", "MMM"))
             .withColumn("year_month", F.date_format("date", "yyyy-MM"))
             .withColumn("day_of_week_number", F.dayofweek("date"))
             .withColumn("day_of_week", F.date_format("date", "EEE"))
             .withColumn("week_start", F.date_sub("date", (F.dayofweek("date") + 5) % 7)))
    _overwrite(dates, t.gold_date)
    rows = [(c.id, c.name) for c in geo.corridors] + [("Other / unmatched", "Other / unmatched")]
    _overwrite(spark.createDataFrame(rows, "corridor string, corridor_name string"), t.gold_corridors)


def _overwrite(df: DataFrame, table: str) -> None:
    df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table)


def facility_history_baseline(spark: SparkSession, t: Tables, risk: RiskConfig,
                              as_of: datetime | None = None) -> dict[str, int]:
    """Distinct incident occurrences near each facility in the trailing window."""
    from mvoi.geo.core import haversine_m

    as_of = as_of or _now()
    start = as_of.replace(tzinfo=None) - timedelta(days=risk.history.window_days)
    occ = (spark.table(t.gold_incident_occurrences)
           .where((F.col("entity_type") == "incident") & (F.col("started_at") >= F.lit(start)))
           .select("latitude", "longitude").collect())
    fac = spark.table(t.silver_facilities).select("facility_id", "latitude", "longitude").collect()
    out = {}
    for f in fac:
        out[f.facility_id] = sum(
            1 for o in occ if o.latitude is not None and
            haversine_m(f.latitude, f.longitude, o.latitude, o.longitude) <= risk.history.radius_m)
    spark.createDataFrame([(k, v, as_of) for k, v in out.items()],
                          "facility_id string, occurrences int, as_of timestamp") \
        .write.format("delta").mode("overwrite").saveAsTable(t.gold_facility_history_baseline)
    return out


def baseline_json(baseline: dict[str, int], risk: RiskConfig, as_of: datetime) -> str:
    return json.dumps({"as_of": as_of.isoformat(), "window_days": risk.history.window_days,
                       "radius_m": risk.history.radius_m, "facilities": baseline}, indent=1)


def run_summary(**kw: Any) -> str:
    return json.dumps({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in kw.items()},
                      default=str)
