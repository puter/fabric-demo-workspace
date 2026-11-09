# Deployment

Everything except four portal-only steps (marked **MANUAL**) is scripted and
idempotent. Re-running a step updates in place.

## Prerequisites

| Need | Detail |
|---|---|
| Fabric capacity | F4 or larger recommended for the full demo (always-on Eventhouse + Eventstream + hourly Spark). F2 works for the hot path alone. A trial capacity also works. Assign it to `ws-example`. |
| Workspace role | Deployer: **Contributor** or higher on `ws-example`. Report viewers: Viewer. |
| Tenant settings (Fabric admin) | *Users can create Fabric items*; *ArcGIS GeoAnalytics for Fabric Runtime* (Integration settings — tenant or capacity delegated) if using ArcGIS; for CI: *Service principals can use Fabric APIs*. For the data agent: *Users can create and share Fabric data agent item types*, *Users can use Copilot and other features powered by Azure OpenAI*, and *Allow XMLA endpoints* (read) so the agent can query the semantic model. If Azure OpenAI isn't available in the capacity's region, also allow cross-geo processing. |
| Licences | Power BI Pro / PPU for report consumers unless the capacity is F64+. ArcGIS GeoAnalytics for Fabric is **bring-your-own-licence** (Esri marketplace offer) — optional, the fallback engine needs none. |
| OHGO API key | Free: https://publicapi.ohgo.com/accounts/registration |
| Azure subscription | Only for hosting the poller (Container Apps, Key Vault, Storage). |
| Local tools | `uv` (Python 3.11 is fetched automatically), Azure CLI, Docker (to build the image). Java 17 only for the Spark test-suite. |

### Signing in (this repo)

The repo pins its own Azure CLI profile (`.claude/settings.local.json` →
`AZURE_CONFIG_DIR=~/.azure-profiles/demo-fabric-workspace`) so the demo tenant
never mixes with other work:

```bash
AZURE_CONFIG_DIR=~/.azure-profiles/demo-fabric-workspace az login --tenant <demo-tenant-id>
export AZURE_CONFIG_DIR=~/.azure-profiles/demo-fabric-workspace   # for this shell
```

## 1. Local validation (no cloud)

```bash
cd scenarios/miami-valley
uv sync --all-extras --group dev
uv run pytest -q                                    # unit + integration (mocked)
JAVA_HOME=/path/to/jdk17 uv run --group spark pytest -m spark tests/spark   # medallion on Spark/Delta
uv run mvoi replay synthetic                        # writes replay/recordings/synthetic-storm-and-crashes.jsonl
MVOI_PUBLISHER__KIND=stdout uv run mvoi replay start \
  --recording replay/recordings/synthetic-storm-and-crashes.jsonl --speed 60
OHGO_API_KEY=... uv run pytest -m live              # live OHGO + NWS smoke tests
OHGO_API_KEY=... MVOI_PUBLISHER__KIND=stdout uv run mvoi run --once
```

## 2. Fabric items

```bash
uv run python scripts/deploy_fabric.py --steps all --key-vault https://<kv>.vault.azure.net/
```

Creates, under **`Miami Valley Ops Intelligence/`** (ADR 0001/0002):

| Folder | Items |
|---|---|
| 00 - Domain Platform | `lh_mvoi`, `eh_mvoi`, `kqldb_mvoi` (schema, functions, MV, reference tables), `env_mvoi` |
| 10 - Ingestion | `es_mvoi_events` (custom endpoint `PollerEndpoint` → Eventhouse `Events`) |
| 20 - Transformation | `nb_mvoi_bronze_ingest`, `nb_mvoi_silver_build`, `nb_mvoi_gold_build`, `nb_mvoi_geospatial` |
| 30 - Data Products | `rtd_mvoi_operations`, `sm_mvoi_historical`, `da_mvoi` (data agent) |
| 40 - Orchestration | `pl_mvoi_medallion` (hourly), `act_mvoi_alerts` (step 5) |

The `files` step uploads `config/`, `reference-data/` and a generated
`fabric_runtime.json` (query URI, database, Key Vault URL) to
`lh_mvoi/Files/mvoi/`. Notebooks read workspace-specific values from there, so
**no workspace or item ids are committed**.

## 3. Poller hosting (Azure Container Apps)

Normally done by the **DeployPoller** stage of the Azure DevOps pipeline (below). By hand:

```bash
az group create -n rg-example -l eastus2
az acr create -g rg-example -n <acr> --sku Basic          # once
az acr build -r <acr> -t mvoi-poller:<tag> .                # builds in Azure; no local Docker needed
az deployment group create -g rg-example -f infrastructure/main.bicep \
  -p image=<acr>.azurecr.io/mvoi-poller:<tag> acrName=<acr> \
     nwsUserAgent='MiamiValleyOpsIntelligence/0.1 (ops@example.com)'
```

The Container App pulls from ACR with its managed identity (`AcrPull`), so no
registry credentials are stored.

Then put the secrets in the vault (output `keyVaultName`). Your own identity
needs *Key Vault Secrets Officer*:

```bash
az keyvault secret set --vault-name <kv> -n ohgo-api-key --value <key>
uv run python scripts/deploy_fabric.py --steps eventstream --store-connection-in-keyvault https://<kv>.vault.azure.net/
az containerapp revision restart ...   # or let the next revision pick the secrets up
```

`--store-connection-in-keyvault` reads the custom-endpoint connection string
through the Eventstream Topology API and writes it straight to the vault. It is
never printed. **MANUAL alternative:** Eventstream → *PollerEndpoint* →
Details → Event Hub → SAS Key Authentication → copy *Connection string-primary
key* into the secret.

### Eventstream resume job

The template also creates `caj-example-resume-stream`, a scheduled job that runs
`mvoi resume-stream` hourly from 11:05 to 22:05 UTC (`resumeStreamSchedule`).
When the capacity suspends overnight, the Eventstream's Eventhouse destination
goes to *Paused*, and it stays paused after the capacity resumes. The job
resumes it from where it stopped, so the queued events backfill. When
everything is already *Running* it exits in seconds.

The job runs as the poller's managed identity (output
`pollerIdentityPrincipalId`), which needs **Contributor** on the workspace
(*Manage access* → add the identity `id-example-poller`). To check it, run
`az containerapp job start -n caj-example-resume-stream -g rg-example`,
then read the execution's logs. Locally, `uv run --extra fabric mvoi
resume-stream` does the same as your `az login` user.

Optional: with that role in place, set `historyBaselineUrl` to
`https://onelake.dfs.fabric.microsoft.com/<ws>/<lh>/Files/mvoi/exports/history_baseline.json`
so the history component of risk scoring is live.

Running the poller without Azure: `docker run -e OHGO_API_KEY=… -e
MVOI_EVENTSTREAM_CONNECTION_STRING=… -e MVOI_PUBLISHER__KIND=eventhub -v
$PWD/.state:/state mvoi-poller` on any always-on host.

## 4. First data and Gold

Once the poller has been running for a few minutes:

1. Check the Real-Time Dashboard's **Minutes since last ingestion** tile shows < 2.
2. Run `pl_mvoi_medallion` once by hand (it then runs hourly) to create the
   Bronze/Silver/Gold tables.
3. ArcGIS (optional): store the Esri API key as Key Vault secret
   `arcgis-api-key`. The identity running the notebooks (the pipeline owner)
   needs *Key Vault Secrets User*. `nb_mvoi_geospatial` then reports
   `spatial engine: arcgis-geoanalytics`.

## 5. Activator (validation-gated)

```bash
uv run python scripts/deploy_fabric.py --steps activator --alert-email ops@example.com [--alert-teams ops@example.com]
```

The step refuses to run until real live heartbeat rows (and at least one
operational alert) exist in Eventhouse. This is the Activator source-validation
rule: never author against an empty source. Use `--allow-no-alert-rows` to
author the alert rule before a real alert has occurred; heartbeats are still
required.

Rules created (both on live data only):

| Rule | Source | Condition | Action |
|---|---|---|---|
| Operational alert raised | KQL, 60 s, operational_alert events | `alert_state` = `raised` | Email / Teams with alert type, facility, detail |
| Ingestion stopped | KQL, 60 s, heartbeat events | `NoHeartbeat` 5 min | Email / Teams |

De-duplication: the poller emits `raised` once per condition, applies
hysteresis (clears below 60), and suppresses re-raise within 30 min.
`NoHeartbeat` fires once per gap. **MANUAL:** recipients outside your tenant
may need to be allowed by the Teams/Exchange admin.

## 6. Power BI historical report (MANUAL — build in the service)

The Direct Lake semantic model `sm_mvoi_historical` is deployed and refreshed
by the script. The refresh frames the model onto the Gold tables. Without it,
every query (including the data agent's) fails with *Cannot find table*. On a
first deploy the Gold tables may not exist yet, so the refresh only logs a
warning: run the pipeline once, then `--steps semantic` again.
The report pages are built in the browser following [power-bi.md](power-bi.md).
Report-definition (PBIR) generation is not automated here: a hand-written PBIR
cannot be validated without a live service, and the brief asks not to present
untested artifacts as working.

## 6a. Data agent

`da_mvoi` is created by `--steps all` (or `--steps dataagent`) over
`sm_mvoi_historical` and `kqldb_mvoi`, and published. Its instructions and
example queries are authored in `fabric/data-agent/` (ADR 0003). Edit those
files and re-run the step. Changes made in the Fabric UI are overwritten.

It answers useful questions only after step 4: Gold must be populated for the
semantic model, and Events must have rows for the KQL functions. Every question
uses Copilot capacity (see [costs.md](costs.md)). Tune it in short sessions,
starting a new conversation for each test.

## 7. Git sync to `/workspace` (MANUAL, once)

Workspace settings → Git integration → this repo, branch, folder `/workspace`
→ **Commit**. Fabric writes its native serialization there (ADR 0002). Future
changes go through `scenarios/miami-valley` and the deploy script, then commit
from Fabric.

## 8. Azure DevOps pipeline

`.azure-pipelines/miami-valley.yml` runs on pushes and PRs to `main` that touch
the scenario.

| Stage | Runs | What it does |
|---|---|---|
| Test | always | ruff, pytest (+ JUnit results), TMDL-drift check; Spark/Delta suite on Java 17; Bicep compile; image build |
| DeployFabric | `deployFabric` parameter = true | `deploy_fabric.py --steps all` via service connection `sc-example-fabric` |
| DeployPoller | `deployPoller` parameter = true | `az acr build` + Bicep deployment via `sc-example-azure` |

One-time ADO setup (**MANUAL**):
1. Pipelines → New pipeline → Azure Repos Git → this repo → *Existing YAML* → `/.azure-pipelines/miami-valley.yml`.
2. Project settings → Service connections → *Azure Resource Manager* → *Workload identity federation*:
   - `sc-example-fabric`: add its service principal to `ws-example` as **Contributor**. Requires the tenant setting *Service principals can use Fabric APIs*.
   - `sc-example-azure`: **Contributor** on `rg-example` and **AcrPush** on the registry.
3. Pipeline variables: `acrName`, `resourceGroup`, `nwsUserAgent`.
4. Pipelines → Environments → `example-env` → add an **Approval** check, so deploys need a human.

The Activator step stays out of the pipeline: it needs recipients and passes the
live-data validation gate only after the poller is running.

## Teardown

Delete the scenario folder's items in the workspace and `az group delete -n
rg-example`. Eventhouse data is purged with the item; Lakehouse Delta tables
go with `lh_mvoi`.
