# %% [markdown]
# # Gold — analytical datasets
#
# | Table | Grain | Refresh |
# |---|---|---|
# | `silver_facilities`, `silver_corridors`, `silver_zones` | reference | overwrite from Files/mvoi/reference-data |
# | `gold_incident_occurrences` | one row per distinct incident occurrence | overwrite (full recompute; small) |
# | `gold_incident_trends_hourly` | date × hour × corridor × type × severity | overwrite |
# | `gold_corridor_disruption_daily` | date × corridor | overwrite |
# | `gold_active_incidents` | entity (active only) | overwrite |
# | `gold_facility_risk_history` | facility × risk snapshot | overwrite |
# | `gold_pipeline_health_daily` | date × source | overwrite |
# | `gold_facility_history_baseline` | facility | overwrite + JSON export for the poller |
# | `gold_date`, `gold_corridors` | conformed dimensions | overwrite |
#
# Occurrences (not polls, not lifecycle rows) are the unit of counting, so a
# long-running incident is counted once; `duration_minutes` supports
# incident-minute weighting.

# %%
# {{BOOTSTRAP}}

# %%
from datetime import UTC, datetime

from mvoi.common.config import load_geography, load_risk_config

as_of = datetime.now(UTC)
ref = f"{MVOI_FILES}/reference-data"
# Spark reads lakehouse-relative paths; Python file APIs use the /lakehouse mount.
sj.gold_reference(spark, TABLES, "Files/mvoi/reference-data/facilities.csv",
                  f"{ref}/corridors.geojson", f"{ref}/nws_zones.geojson")
occ = sj.gold_occurrences(spark, TABLES, as_of=as_of)
sj.gold_aggregates(spark, TABLES)
sj.gold_dimensions(spark, TABLES, load_geography())

risk = load_risk_config()
baseline = sj.facility_history_baseline(spark, TABLES, risk, as_of=as_of)

# Exported for the polling service's history component (see docs/architecture.md).
notebookutils.fs.put("Files/mvoi/exports/history_baseline.json",  # noqa: F821
                     sj.baseline_json(baseline, risk, as_of), True)

summary = sj.run_summary(step="gold", as_of=as_of, occurrences=occ.count(),
                         facilities_with_history=sum(1 for v in baseline.values() if v))
print(summary)

# %%
display(spark.table(TABLES.gold_corridor_disruption_daily)  # noqa: F821
        .orderBy("start_date", ascending=False).limit(50))

# %%
notebookutils.notebook.exit(summary)  # noqa: F821
