# %% [markdown]
# # Geospatial intelligence — Python engine, optional ArcGIS GeoAnalytics
#
# | Analysis | ArcGIS GeoAnalytics, if licensed | Output |
# |---|---|---|
# | Incident ➜ facility proximity | `ST_Point`, geodesic `ST_DWithin`, `ST_GeodesicDistance` | `gold_facility_proximity` |
# | Weather exposure | `ST_GeomFromGeoJSON`, `ST_Intersects` (+ time overlap) | `gold_weather_exposure`, `gold_incident_weather` |
# | Corridor matching | geodesic `ST_DWithin` to TIGER corridor lines | `gold_corridor_match` |
# | Historical hot spots | `FindHotSpots` (Getis-Ord Gi*) on **distinct occurrences** | `gold_hotspots` |
#
# **Licensing:** GeoAnalytics is bring-your-own-licence (Esri). With
# `engine = "auto"` the notebook uses ArcGIS when the tenant setting is on and
# credentials authorise, otherwise the pure-Python fallback produces the same
# tables (column `spatial_engine` records which ran). Set `engine = "arcgis"`
# to fail instead of falling back.

# %% tags=["parameters"]
engine = "auto"                 # auto | arcgis | fallback
hotspot_bin_m = 1600            # ~1 mile bins
hotspot_neighborhood_m = 4800   # ~3 mile neighbourhood
hotspot_weighting = "occurrences"   # occurrences | minutes

# %%
# {{BOOTSTRAP}}

# %%
from mvoi.common.config import load_geography, load_risk_config
from mvoi.lakehouse.geospatial import select_engine

geo, risk = load_geography(), load_risk_config()

auth = None
kv = RUNTIME.get("key_vault_url")
if kv and engine != "fallback":
    try:  # Esri API key kept in Key Vault, never in code
        auth = {"api_key": notebookutils.credentials.getSecret(kv, "arcgis-api-key")}  # noqa: F821
    except Exception as exc:  # noqa: BLE001
        print(f"No ArcGIS key in Key Vault ({exc.__class__.__name__}); trying ambient auth")

eng = select_engine(spark, geo, prefer=engine, arcgis_auth=auth)
print("spatial engine:", eng.name)

eng.proximity(TABLES, max_m=risk.distance_bands_m.max, full_m=risk.distance_bands_m.full)
eng.weather(TABLES)
eng.corridors(TABLES)
eng.hotspots(TABLES, hotspot_bin_m, hotspot_neighborhood_m, hotspot_weighting)

# %% [markdown]
# ## Map
# ArcGIS: `st.plot` renders the hot-spot bins and facilities on a basemap
# inside the notebook (static image — refreshed when the notebook runs).
# Interactive, auto-refreshing maps are in Power BI (Azure Maps / ArcGIS for
# Power BI over these Gold tables) and the Real-Time Dashboard (live points).

# %%
hot = spark.table(TABLES.gold_hotspots).where("Gi_Bin <> 0")
fac = spark.table(TABLES.silver_facilities)
if eng.name.startswith("arcgis"):
    from geoanalytics_fabric.sql import functions as ST

    bins = hot.withColumn("geom", ST.geom_from_text("bin_wkt", sr=4326))
    ax = bins.where("Gi_Bin > 0").st.plot(basemap="light", geometry="geom", facecolor="red",
                                          edgecolor="darkred", alpha=0.5)
    bins.where("Gi_Bin < 0").st.plot(ax=ax, geometry="geom", facecolor="blue", edgecolor="navy",
                                     alpha=0.4)
    (fac.withColumn("geom", ST.point("longitude", "latitude", sr=4326))
        .st.plot(ax=ax, geometry="geom", facecolor="black", edgecolor="white"))
else:
    import matplotlib.pyplot as plt

    hp, fp = hot.toPandas(), fac.toPandas()
    fig, ax = plt.subplots(figsize=(10, 8))
    sc = ax.scatter(hp.center_lon, hp.center_lat, c=hp.GiZScore, cmap="RdBu_r", s=60, marker="s")
    ax.scatter(fp.longitude, fp.latitude, c="black", s=25, label="Monitored sites")
    for _, r in fp.iterrows():
        ax.annotate(r.facility_id, (r.longitude, r.latitude), fontsize=7)
    fig.colorbar(sc, label="Gi* z-score")
    ax.set_title("Incident hot spots (distinct occurrences) — fallback engine")
    ax.legend()
    plt.show()

# %%
summary = sj.run_summary(step="geospatial", engine=eng.name,
                         hotspot_bins=spark.table(TABLES.gold_hotspots).where("Gi_Bin >= 2").count(),
                         proximity_pairs=spark.table(TABLES.gold_facility_proximity).count())
print(summary)
notebookutils.notebook.exit(summary)  # noqa: F821
