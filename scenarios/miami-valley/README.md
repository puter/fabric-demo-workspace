# Miami Valley Operations Intelligence

A real-time, geospatially enriched operations demo on **Microsoft Fabric**.
It covers Dayton, Troy, Springfield and southwestern Ohio. Public ODOT traffic
data and NWS weather alerts stream through Eventstream into Eventhouse for live
dashboards and Activator alerts. The same events land in a Bronze/Silver/Gold
Lakehouse, where Spark adds facility proximity, weather exposure,
corridor analytics and hot spots for Power BI. A built-in Python engine does
the geospatial work by default; ArcGIS GeoAnalytics is optional and needs an
Esri licence.

> Monitored sites are **real public places and employers**, centred on the
> demo host, **Sinclair Conference Center** (Sinclair Community College,
> Dayton). MVOI is **not affiliated** with any of them. Scores are **inferred
> travel-disruption exposure** from public traffic and weather data near each
> site, not a statement about its operations.

## Start here

| Doc | What's in it |
|---|---|
| [docs/overview.md](docs/overview.md) | Non-technical write-up: what the app does, how severity and disruption exposure are calculated, the architecture at a glance |
| [docs/architecture.md](docs/architecture.md) | Data-flow diagram, hot/historical paths, table refresh, replay isolation, design decisions |
| [docs/data-sources.md](docs/data-sources.md) | OHGO / NWS / TIGER / facilities, polling & freshness, severity rules, API ambiguities |
| [docs/data-contracts.md](docs/data-contracts.md) | The canonical event contract |
| [docs/deployment.md](docs/deployment.md) | Prerequisites (capacity, roles, tenant settings, licences) and step-by-step deploy |
| [docs/demo-script.md](docs/demo-script.md) | The 10-minute customer demonstration |
| [docs/power-bi.md](docs/power-bi.md) | Semantic model and report build guide |
| [docs/presenter.md](docs/presenter.md) | Local presenter app for live demos |
| [docs/arcgis.md](docs/arcgis.md) | ArcGIS GeoAnalytics usage, licensing, fallback |
| [docs/costs.md](docs/costs.md) / [docs/troubleshooting.md](docs/troubleshooting.md) | |
| [../../docs/adr/0002-scenario-source-layout-and-rti-naming.md](../../docs/adr/0002-scenario-source-layout-and-rti-naming.md) | Why the code lives here and what the RTI prefixes are |

## Quick start (local, no cloud)

```bash
cd scenarios/miami-valley
uv sync --all-extras --group dev
uv run pytest -q                                   # unit + integration tests
uv run mvoi replay synthetic                       # build the demo storyline
MVOI_PUBLISHER__KIND=stdout uv run mvoi replay start \
  --recording replay/recordings/synthetic-storm-and-crashes.jsonl --speed 60
```

With an OHGO key: `OHGO_API_KEY=… MVOI_PUBLISHER__KIND=stdout uv run mvoi run --once`.

## Layout

```
config/            geography.yaml · risk-scoring.yaml · settings.example.yaml · fabric.yaml
reference-data/    facilities.csv (public sites) · corridors.geojson (TIGER) · nws_zones.geojson (NWS)
src/mvoi/
  sources/         OHGO + NWS adapters, severity rules
  models/          canonical event contract
  services/        pipeline, lifecycle, SQLite state/outbox, health
  publishers/      Eventstream (Event Hubs protocol), file, stdout
  risk/            explainable scoring + alert state machine
  geo/             geodesy, corridor matching, Gi* hot spots
  replay/          recording format, player, synthetic storyline
  lakehouse/       DQ rules, occurrences, Spark medallion + geospatial jobs
  deploy/          Fabric REST client, KQL, notebooks, dashboard, Activator, semantic model, data agent builders
  presenter/       local app that embeds the dashboard and data agent for live demos
fabric/
  eventhouse/      kqldb_mvoi.kql (tables, mapping, policies, MV, 24 functions)
  notebooks/       bronze / silver / gold / geospatial (percent-format .py)
  semantic-model/  sm_mvoi_historical.SemanticModel (generated TMDL, Direct Lake)
  data-agent/      da_mvoi instructions + KQL few-shots (ADR 0003)
scripts/           deploy_fabric.py · build_reference_data.py
infrastructure/    main.bicep (Container Apps + Key Vault + Files)
replay/recordings/ synthetic-storm-and-crashes.jsonl
tests/             unit · integration (mocked + live) · spark
```

## Capability status

**Running** means it is deployed in our demo Fabric workspace and working on
live data. **Tested** means automated tests cover it, without a Fabric
workspace. Status as of November 2026.

| Capability | Status | Notes |
|---|---|---|
| OHGO and NWS ingestion | **Running** | Polls OHGO every 60 s and NWS every 120 s |
| Incident lifecycle, de-duplication, truncation guard | **Running** | |
| Outbox and Eventstream publisher | **Running** | Backlogs survive restarts and overnight capacity pauses |
| Poller hosting (Docker, Bicep, Container Apps) | **Running** | Includes the hourly Eventstream resume job |
| Eventhouse schema, materialized view, 24 KQL functions | **Running** | |
| Eventstream (custom endpoint to Eventhouse) | **Running** | |
| Real-Time Dashboard (27 tiles, Live/Replay) | **Deployed** | Built by the deploy script against dashboard schema v89 |
| Bronze/Silver/Gold notebooks and pipeline | **Running** | Hourly |
| Geospatial: proximity, weather overlay, corridor match, Gi* hot spots | **Running** | The ArcGIS engine needs an Esri licence; otherwise a Python fallback is used |
| Risk scoring and explanations | **Running** | |
| Direct Lake semantic model (12 tables) | **Running** | |
| Data agent (semantic model + KQL database, 12 KQL examples) | **Running** | Answers checked against the stored functions for current traffic, risk and feed health. Replay and historical answers still need checking |
| Local presenter app | Implemented | Embeds the dashboard and data agent; see [docs/presenter.md](docs/presenter.md) |
| Replay (record, synthetic, speed, start/stop/reset, isolation) | **Tested** locally | Tested end to end through the pipeline, but not yet run against Fabric |
| Alert de-duplication, hysteresis and recovery | **Tested** | Runs in the poller's scoring logic |
| Activator alerts | Implemented, **not deployed** | Needs `--steps activator` and live alert rows |
| Power BI report pages | **Manual, not built** | Build guide in [docs/power-bi.md](docs/power-bi.md) |
| CI/CD in Azure DevOps | Defined, **not set up** | `.azure-pipelines/miami-valley.yml` needs service connections `sc-example-fabric` and `sc-example-azure`, and variables `acrName`, `resourceGroup` and `nwsUserAgent` |

**Tests:** 168 unit and integration tests pass with mocked services. Spark
end-to-end tests need a local Spark and Delta install, and live API tests need
network access (plus `OHGO_API_KEY` for OHGO); both are skipped otherwise. The
KQL itself isn't run by the tests; it was checked against the deployed
Eventhouse.
