"""Bronze -> Silver -> Gold -> geospatial on local Spark + Delta.

Runs the synthetic storyline through the real hot-path pipeline (as live
data), lands the events as a Bronze batch shaped exactly like the Eventhouse
pull, and executes the same functions the Fabric notebooks call. Requires
Java (JAVA_HOME) — marked `spark`, run with:  pytest -m spark
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest

pyspark = pytest.importorskip("pyspark")
pytestmark = pytest.mark.spark

from mvoi.common.config import LifecycleSettings  # noqa: E402
from mvoi.geo.reference import REFERENCE_DIR  # noqa: E402
from mvoi.lakehouse import spark_jobs as sj  # noqa: E402
from mvoi.lakehouse.geospatial import FallbackEngine, select_engine  # noqa: E402
from mvoi.models.events import RunMode  # noqa: E402
from mvoi.publishers.base import MemoryPublisher  # noqa: E402
from mvoi.replay import player as rp  # noqa: E402
from mvoi.replay.synthetic import write_storyline  # noqa: E402
from mvoi.services.state import StateStore  # noqa: E402
from tests.conftest import T0  # noqa: E402

AS_OF = T0 + timedelta(hours=1)


@pytest.fixture(scope="module")
def spark(tmp_path_factory):
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    wh = tmp_path_factory.mktemp("warehouse")
    builder = (SparkSession.builder.master("local[2]").appName("mvoi-tests")
               .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
               .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
               .config("spark.sql.warehouse.dir", str(wh))
               .config("spark.sql.session.timeZone", "UTC")
               .config("spark.sql.shuffle.partitions", "4")
               .config("spark.ui.enabled", "false"))
    s = configure_spark_with_delta_pip(builder).getOrCreate()
    yield s
    s.stop()
    shutil.rmtree(wh, ignore_errors=True)


def _events(tmp_path, make_pipeline, ohgo_adapter, nws_adapter):
    """Synthetic storyline processed as LIVE through the real pipeline."""
    rec = write_storyline(tmp_path / "rec.jsonl", start=T0)
    pub = MemoryPublisher()
    store = StateStore(":memory:")
    clock = {"t": T0}

    def factory(sid, speed):
        p = make_pipeline(publisher=pub, st=store, run_mode=RunMode.LIVE,
                          lifecycle=rp.scaled_lifecycle(LifecycleSettings(resolve_after_minutes=5), speed))
        p.clock = lambda: clock["t"]
        return p

    def sleep(s):
        clock["t"] += timedelta(seconds=s)
        return False

    player = rp.ReplayPlayer(rec, factory, {"ohgo": ohgo_adapter, "nws": nws_adapter}, speed=1.0,
                             clock=lambda: clock["t"], sleep=sleep)
    player.run()
    return pub.events


def _bronze_batch(spark, events):
    rows = []
    for i, e in enumerate(events):
        r = {c: e.get(c) for c in sj.EVENT_COLUMNS if c not in ("attributes_json", "raw_json")}
        r["attributes_json"] = json.dumps(e.get("attributes") or {})
        r["raw_json"] = json.dumps(e.get("raw")) if e.get("raw") is not None else None
        for k in ("observed_at", "source_updated_at", "first_seen_at", "last_seen_at", "start_time",
                  "end_time", "ingested_at"):
            v = r.get(k)
            r[k] = datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None) if v else None
        r["kusto_ingestion_time"] = (T0 + timedelta(seconds=i)).replace(tzinfo=None)
        rows.append(r)
    schema = ("schema_version string, event_id string, entity_key string, source string, entity_type string, "
              "source_id string, change_type string, environment string, run_mode string, "
              "replay_session_id string, observed_at timestamp, source_updated_at timestamp, "
              "first_seen_at timestamp, last_seen_at timestamp, start_time timestamp, end_time timestamp, "
              "ingested_at timestamp, latitude double, longitude double, geometry_geojson string, "
              "route_name string, corridor_id string, direction string, location string, category string, "
              "status string, severity string, severity_rank int, is_road_closure boolean, title string, "
              "description string, content_hash string, attributes_json string, raw_json string, "
              "kusto_ingestion_time timestamp")
    cols = [c.split()[0] for c in schema.split(", ")]
    return spark.createDataFrame([tuple(r.get(c) for c in cols) for r in rows], schema)


def test_medallion_end_to_end(spark, tmp_path, make_pipeline, ohgo_adapter, nws_adapter, geography,
                              risk_cfg):
    t = sj.Tables("t_")
    events = _events(tmp_path, make_pipeline, ohgo_adapter, nws_adapter)
    # Add contamination that Silver must remove: a replay row and a duplicate delivery.
    replay_row = {**events[0], "event_id": "00000000-0000-0000-0000-000000000001", "run_mode": "replay",
                  "replay_session_id": "demo"}
    batch = _bronze_batch(spark, [*events, events[0], replay_row])

    # ---- bronze
    assert sj.bronze_watermark(spark, t) is None
    n = sj.bronze_append(spark, batch, t)
    assert n == len(events) + 2
    assert sj.bronze_watermark(spark, t) is not None
    assert spark.table(t.bronze_events).where("raw_json IS NOT NULL").count() > 0  # lossless

    # ---- silver
    res = sj.silver_build(spark, geography, t, run_id="r1")
    assert res["accepted"] == len({e["event_id"] for e in events})
    assert spark.table(t.silver_events).where("run_mode <> 'live'").count() == 0
    dq = {r.rule: r.rows_failed for r in spark.table(t.silver_dq_results).collect()}
    assert dq["event_id_present"] == 0 and dq["lat_lon_valid"] == 0
    # Idempotent: rerun without new bronze rows adds nothing.
    sj.silver_build(spark, geography, t, run_id="r2")
    assert spark.table(t.silver_events).count() == res["accepted"]

    # ---- gold
    sj.gold_reference(spark, t, str(REFERENCE_DIR / "facilities.csv"),
                      str(REFERENCE_DIR / "corridors.geojson"), str(REFERENCE_DIR / "nws_zones.geojson"))
    occ = sj.gold_occurrences(spark, t, as_of=AS_OF)
    transport_entities = {e["entity_key"] for e in events
                          if e["entity_type"] in sj.TRANSPORT}
    assert occ.count() == len(transport_entities)
    i75 = occ.where("entity_key = 'ohgo:incident:SYN-I-075'").first()
    assert i75.peak_severity == "major" and i75.had_road_closure and not i75.is_open
    assert 15 < i75.duration_minutes < 30
    sj.gold_aggregates(spark, t)
    corr = {r.corridor: r.occurrences for r in spark.table(t.gold_corridor_disruption_daily).collect()}
    assert corr["I-75"] >= 3 and corr["US-40"] == 2
    assert spark.table(t.gold_facility_risk_history).where("score >= 70").count() > 0
    assert spark.table(t.gold_pipeline_health_daily).count() > 0
    sj.gold_dimensions(spark, t, geography)
    assert spark.table(t.gold_date).count() >= 365
    assert spark.table(t.gold_corridors).where("corridor = 'I-75'").count() == 1
    baseline = sj.facility_history_baseline(spark, t, risk_cfg, as_of=AS_OF)
    assert baseline["FAC-001"] >= 1

    # ---- geospatial (fallback engine; ArcGIS needs Fabric + licence)
    eng = select_engine(spark, geography, prefer="auto")
    assert isinstance(eng, FallbackEngine)
    eng.proximity(t, max_m=risk_cfg.distance_bands_m.max, full_m=risk_cfg.distance_bands_m.full)
    prox = spark.table(t.gold_facility_proximity)
    assert prox.where("facility_id = 'FAC-001' AND occurrence_id = 'ohgo:incident:SYN-I-075#1' "
                      "AND within_full_weight").count() == 1
    eng.weather(t)
    exposed = {r.facility_id for r in spark.table(t.gold_weather_exposure).collect()}
    assert {"FAC-001", "FAC-002"} <= exposed
    wx_inc = {r.occurrence_id for r in spark.table(t.gold_incident_weather).collect()}
    assert "ohgo:incident:SYN-I-075#1" in wx_inc
    eng.corridors(t)
    assert spark.table(t.gold_corridor_match).where("final_corridor IS NULL").count() == 0
    eng.hotspots(t, bin_m=1600, neigh_m=4800, weighting="occurrences")
    hs = spark.table(t.gold_hotspots)
    assert hs.agg({"value": "sum"}).first()[0] == occ.where("entity_type = 'incident'").count()
    assert {"GiZScore", "GiPValue", "Gi_Bin", "bin_wkt"} <= set(hs.columns)

    # ---- semantic model contract: every Direct Lake column exists in Gold with a compatible type
    from mvoi.deploy.semantic import TABLES as SM_TABLES
    spark_to_tmdl = {"string": "string", "int": "int64", "bigint": "int64", "double": "double",
                     "timestamp": "dateTime", "date": "dateTime", "boolean": "boolean"}
    for tbl in SM_TABLES:
        df = spark.table(getattr(t, tbl.entity))
        types = dict(df.dtypes)
        for c in tbl.cols:
            assert c.source in types, f"{tbl.name}.{c.name}: {c.source} missing from {tbl.entity}"
            assert spark_to_tmdl[types[c.source]] == c.dtype, (tbl.name, c.name, types[c.source], c.dtype)


def test_bronze_query_shape():
    q = sj.bronze_kusto_query(datetime(2026, 1, 1, tzinfo=UTC))
    assert "ingestion_time()" in q and "tostring(raw)" in q and "datetime(2026-01-01T00:00:00+00:00)" in q
    assert "['title']" in q  # reserved word in KQL; bare `title` is a syntax error
