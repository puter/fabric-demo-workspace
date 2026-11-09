"""Deploy Miami Valley Operations Intelligence to a Fabric workspace.

Idempotent: re-running updates existing items in place. Each step can be run
alone with --steps. Uses the current `az login` (AZURE_CONFIG_DIR-aware).

    uv run python scripts/deploy_fabric.py --steps all
    uv run python scripts/deploy_fabric.py --steps eventstream --store-connection-in-keyvault https://kv-x.vault.azure.net/
    uv run python scripts/deploy_fabric.py --steps activator --alert-email ops@example.com

Steps (dependency order):
  folders      ADR folder tree under the scenario folder
  lakehouse    lh_mvoi
  eventhouse   eh_mvoi + kqldb_mvoi, KQL schema, reference tables
  environment  env_mvoi (mvoi wheel + pydantic), published
  files        config + reference data + fabric_runtime.json -> lh_mvoi/Files/mvoi
  notebooks    four medallion/geospatial notebooks bound to lh_mvoi + env_mvoi
  pipeline     pl_mvoi_medallion (bronze->silver->gold->geospatial) + hourly schedule
  eventstream  es_mvoi_events: custom endpoint -> Eventhouse (ProcessedIngestion)
  dashboard    rtd_mvoi_operations (Real-Time Dashboard)
  semantic     sm_mvoi_historical (Direct Lake semantic model over Gold), refreshed
  dataagent    da_mvoi (Data Agent over sm_mvoi_historical + kqldb_mvoi), published
  activator    act_mvoi_alerts — only after live alert/heartbeat rows exist (validation gate)
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mvoi.deploy import activator, kql, notebooks, rtd  # noqa: E402
from mvoi.deploy.fabric_api import FabricClient, FabricError, part  # noqa: E402

log = logging.getLogger("deploy")
STEPS = ["folders", "lakehouse", "eventhouse", "environment", "files", "notebooks", "pipeline",
         "eventstream", "dashboard", "semantic", "dataagent", "activator"]
NOTEBOOK_ORDER = ["nb_mvoi_bronze_ingest", "nb_mvoi_silver_build", "nb_mvoi_gold_build",
                  "nb_mvoi_geospatial"]


class Deployer:
    def __init__(self, cfg: dict, client: FabricClient, args):
        self.cfg = cfg
        self.c = client
        self.args = args
        self.names = cfg["items"]
        self.ws = client.workspace_id(cfg["workspace_name"])
        self.folders: dict[str, str] = {}
        log.info("workspace %s -> %s", cfg["workspace_name"], self.ws)

    # ------------------------------------------------------------------ lookups

    def item(self, item_type: str, key: str) -> dict:
        it = self.c.find_item(self.ws, item_type, self.names[key])
        if not it:
            raise FabricError(f"{item_type} '{self.names[key]}' missing — run its step first")
        return it

    def folder(self, key: str) -> str:
        if not self.folders:
            self.step_folders()
        return self.folders[key]

    def kql_db(self) -> dict:
        db = self.item("KQLDatabase", "kql_database")
        return self.c.request("GET", f"/workspaces/{self.ws}/kqlDatabases/{db['id']}")

    # ------------------------------------------------------------------ steps

    def step_folders(self) -> None:
        root = self.c.ensure_folder(self.ws, self.cfg["scenario_folder"])
        for key, name in self.cfg["folders"].items():
            self.folders[key] = self.c.ensure_folder(self.ws, name, root)
        log.info("folders ready: %s", ", ".join(self.cfg["folders"].values()))

    def step_lakehouse(self) -> None:
        it, created = self.c.ensure_item(self.ws, "Lakehouse", self.names["lakehouse"],
                                         folder=self.folder("platform"),
                                         description="MVOI medallion lakehouse (bronze/silver/gold)")
        log.info("lakehouse %s %s", it["displayName"], "created" if created else "exists")

    def step_eventhouse(self) -> None:
        eh, _ = self.c.ensure_item(self.ws, "Eventhouse", self.names["eventhouse"],
                                   folder=self.folder("platform"),
                                   description="MVOI operational analytics (hot path)")
        db, _ = self.c.ensure_item(self.ws, "KQLDatabase", self.names["kql_database"],
                                   folder=self.folder("platform"),
                                   creation_payload={"databaseType": "ReadWrite",
                                                     "parentEventhouseItemId": eh["id"]})
        props = self.kql_db()["properties"]
        uri, name = props["queryServiceUri"], self.names["kql_database"]
        cmds = kql.schema_commands(ROOT / "fabric" / "eventhouse" / "kqldb_mvoi.kql")
        for i, cmd in enumerate(cmds, 1):
            try:
                self.c.kusto_mgmt(uri, name, cmd)
            except FabricError as exc:
                raise FabricError(f"KQL command {i}/{len(cmds)} failed:\n{cmd[:300]}\n{exc}") from exc
        for cmd in kql.reference_commands(ROOT / "reference-data"):
            self.c.kusto_mgmt(uri, name, cmd)
        n = self.c.kusto_query(uri, name, "Facilities | count")[0]["Count"]
        log.info("KQL schema applied (%d commands); Facilities=%d; query uri %s", len(cmds), n, uri)

    def step_environment(self) -> None:
        dist = ROOT / "dist"
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(dist)], cwd=ROOT, check=True)
        wheel = sorted(dist.glob("mvoi-*.whl"))[-1]
        env_yml = "dependencies:\n  - pip:\n      - pydantic>=2.7,<3\n      - httpx>=0.27\n      - pyyaml>=6.0\n"
        # Fabric rejects a partial Sparkcompute.yml: driver/executor sizing is required.
        # Smallest node size (4 cores / 28g) and one executor; fits an F2 and the data is small.
        compute = (
            "enable_native_execution_engine: false\n"
            "driver_cores: 4\n"
            "driver_memory: 28g\n"
            "executor_cores: 4\n"
            "executor_memory: 28g\n"
            "dynamic_executor_allocation:\n"
            "  enabled: false\n"
            "  min_executors: 1\n"
            "  max_executors: 1\n"
            "runtime_version: \"1.3\"\n"
        )
        definition = {"parts": [
            part(f"Libraries/CustomLibraries/{wheel.name}", wheel.read_bytes()),
            part("Libraries/PublicLibraries/environment.yml", env_yml),
            part("Setting/Sparkcompute.yml", compute),
        ]}
        env, _ = self.c.ensure_item(self.ws, "Environment", self.names["environment"],
                                    folder=self.folder("platform"), definition=definition,
                                    description="Spark environment with the mvoi package")
        log.info("publishing environment (can take several minutes)…")
        self.c.request("POST", f"/workspaces/{self.ws}/environments/{env['id']}/staging/publish")
        log.info("environment %s published with %s", env["displayName"], wheel.name)

    def runtime_json(self) -> dict:
        props = self.kql_db()["properties"]
        return {"workspace_id": self.ws, "kql_query_uri": props["queryServiceUri"],
                "kql_database": self.names["kql_database"], "lakehouse": self.names["lakehouse"],
                "key_vault_url": self.args.key_vault, "deployed_at": datetime.now(UTC).isoformat()}

    def step_files(self) -> None:
        lh = self.item("Lakehouse", "lakehouse")
        uploads = {f"Files/mvoi/config/{p.name}": p.read_bytes()
                   for p in (ROOT / "config").glob("*.yaml") if p.name != "settings.yaml"}
        uploads.update({f"Files/mvoi/reference-data/{p.name}": p.read_bytes()
                        for p in (ROOT / "reference-data").iterdir() if p.is_file()})
        uploads["Files/mvoi/config/fabric_runtime.json"] = json.dumps(self.runtime_json(), indent=1).encode()
        for path, data in uploads.items():
            self.c.onelake_put(self.ws, lh["id"], path, data)
        log.info("uploaded %d files to %s/Files/mvoi", len(uploads), lh["displayName"])

    def step_notebooks(self) -> None:
        lh = self.item("Lakehouse", "lakehouse")
        env = self.c.find_item(self.ws, "Environment", self.names["environment"])
        bootstrap, sources = notebooks.load_notebook_sources(ROOT / "fabric" / "notebooks")
        for name in NOTEBOOK_ORDER:
            nb = notebooks.to_ipynb(sources[name], bootstrap, lakehouse_id=lh["id"],
                                    lakehouse_name=lh["displayName"], workspace_id=self.ws,
                                    environment_id=env["id"] if env else None)
            definition = {"format": "ipynb",
                          "parts": [part("notebook-content.ipynb", notebooks.ipynb_bytes(nb))]}
            it, created = self.c.ensure_item(self.ws, "Notebook", name, folder=self.folder("transformation"),
                                             definition=definition)
            log.info("notebook %s %s", name, "created" if created else "updated")

    def step_pipeline(self) -> None:
        activities, prev = [], None
        for name in NOTEBOOK_ORDER:
            nb = self.c.find_item(self.ws, "Notebook", name)
            act = {"name": name.replace("nb_mvoi_", "").title().replace("_", ""),
                   "type": "TridentNotebook",
                   "dependsOn": [{"activity": prev, "dependencyConditions": ["Succeeded"]}] if prev else [],
                   "policy": {"timeout": "0.01:00:00", "retry": 1, "retryIntervalInSeconds": 60},
                   "typeProperties": {"notebookId": nb["id"], "workspaceId": self.ws}}
            activities.append(act)
            prev = act["name"]
        content = {"properties": {"description": "MVOI medallion: bronze -> silver -> gold -> geospatial",
                                  "activities": activities}}
        pl, _ = self.c.ensure_item(self.ws, "DataPipeline", self.names["pipeline"],
                                   folder=self.folder("orchestration"),
                                   definition={"parts": [part("pipeline-content.json", json.dumps(content))]})
        sched = self.cfg["medallion_schedule"]
        existing = self.c.list(f"/workspaces/{self.ws}/items/{pl['id']}/jobs/Pipeline/schedules")
        if not existing:
            now = datetime.now(UTC)
            self.c.request("POST", f"/workspaces/{self.ws}/items/{pl['id']}/jobs/Pipeline/schedules", {
                "enabled": True,
                "configuration": {"type": "Cron", "interval": sched["interval_minutes"],
                                  "startDateTime": now.strftime("%Y-%m-%dT%H:%M:00"),
                                  "endDateTime": (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:00"),
                                  "localTimeZoneId": sched["time_zone"]}})
        log.info("pipeline %s ready; schedule every %d min", pl["displayName"], sched["interval_minutes"])

    def step_eventstream(self) -> None:
        es_cfg = self.cfg["eventstream"]
        db = self.item("KQLDatabase", "kql_database")
        name = self.names["eventstream"]
        topology = {
            "compatibilityLevel": "1.0",
            "sources": [{"name": es_cfg["source_node"], "type": "CustomEndpoint", "properties": {}}],
            "streams": [{"name": f"{name}-stream", "type": "DefaultStream", "properties": {},
                         "inputNodes": [{"name": es_cfg["source_node"]}]}],
            "operators": [],
            "destinations": [{
                "name": es_cfg["destination_node"], "type": "Eventhouse",
                "properties": {"dataIngestionMode": "ProcessedIngestion", "workspaceId": self.ws,
                               "itemId": db["id"], "databaseName": self.names["kql_database"],
                               "tableName": "Events",
                               "inputSerialization": {"type": "Json", "properties": {"encoding": "UTF8"}}},
                "inputNodes": [{"name": f"{name}-stream"}]}],
        }
        props = {"retentionTimeInDays": es_cfg["retention_days"], "eventThroughputLevel": es_cfg["throughput"]}
        definition = {"parts": [part("eventstream.json", json.dumps(topology)),
                                part("eventstreamProperties.json", json.dumps(props))]}
        es, created = self.c.ensure_item(self.ws, "Eventstream", name, folder=self.folder("ingestion"),
                                         definition=definition)
        log.info("eventstream %s %s", name, "created" if created else "updated")
        if self.args.store_connection_in_keyvault:
            self._store_connection(es["id"])
        else:
            log.info("connection string NOT retrieved. Re-run with --store-connection-in-keyvault <url> "
                     "to write it to Key Vault secret 'eventstream-connection-string', or copy it from "
                     "the Eventstream Details pane (see docs/deployment.md).")

    def _store_connection(self, es_id: str) -> None:
        # updateDefinition re-provisions the custom endpoint; its access keys 404
        # (AuthorizationRuleNotFound) until the source leaves "Creating".
        deadline = time.monotonic() + 600
        while True:
            topo = self.c.request("GET", f"/workspaces/{self.ws}/eventstreams/{es_id}/topology")
            src = next(s for s in topo["sources"] if s["name"] == self.cfg["eventstream"]["source_node"])
            if src.get("status") == "Running":
                break
            if time.monotonic() > deadline:
                raise FabricError(f"eventstream source still '{src.get('status')}' after 10 min")
            log.info("waiting for eventstream source (status %s)…", src.get("status"))
            time.sleep(15)
        conn =self.c.request("GET", f"/workspaces/{self.ws}/eventstreams/{es_id}/sources/{src['id']}/connection")
        from azure.identity import AzureCliCredential
        from azure.keyvault.secrets import SecretClient
        SecretClient(self.args.store_connection_in_keyvault, AzureCliCredential()).set_secret(
            "eventstream-connection-string", conn["accessKeys"]["primaryConnectionString"])
        log.info("stored Eventstream connection string in Key Vault (value not printed)")

    def step_dashboard(self) -> None:
        props = self.kql_db()["properties"]
        db = self.item("KQLDatabase", "kql_database")
        dash = rtd.build_dashboard(title="Miami Valley Operations Intelligence",
                                   cluster_uri=props["queryServiceUri"], database_id=db["id"],
                                   workspace_id=self.ws, database_name=self.names["kql_database"])
        it, created = self.c.ensure_item(self.ws, "KQLDashboard", self.names["dashboard"],
                                         folder=self.folder("products"),
                                         definition={"parts": [part("RealTimeDashboard.json", json.dumps(dash))]})
        log.info("dashboard %s %s", it["displayName"], "created" if created else "updated")

    def step_semantic(self) -> None:
        from mvoi.deploy import semantic
        lh = self.item("Lakehouse", "lakehouse")
        folder = ROOT / "fabric" / "semantic-model" / f"{semantic.MODEL_NAME}.SemanticModel"
        parts = semantic.definition_parts(folder, workspace_id=self.ws, lakehouse_id=lh["id"])
        it, created = self.c.ensure_item(self.ws, "SemanticModel", self.names["semantic_model"],
                                         folder=self.folder("products"),
                                         definition={"format": "TMDL", "parts": parts})
        log.info("semantic model %s %s (Direct Lake over %s)", it["displayName"],
                 "created" if created else "updated", lh["displayName"])
        # Uploading a definition does not frame a Direct Lake model; until it is
        # refreshed, every query fails with "Cannot find table".
        try:
            self.c.refresh_semantic_model(self.ws, it["id"])
            log.info("semantic model %s refreshed", it["displayName"])
        except FabricError as e:
            log.warning("semantic model refresh failed (Gold not built yet? run the pipeline, then "
                        "--steps semantic): %s", e)

    def step_dataagent(self) -> None:
        from mvoi.deploy import dataagent
        sm = self.item("SemanticModel", "semantic_model")
        db = self.item("KQLDatabase", "kql_database")
        uri, name = self.kql_db()["properties"]["queryServiceUri"], self.names["kql_database"]
        columns = {fn: [r["ColumnName"] for r in self.c.kusto_query(uri, name, f"{fn}() | getschema")]
                   for fn in dataagent.KQL_FUNCTIONS}
        parts = dataagent.definition_parts(
            workspace_id=self.ws, semantic_model_id=sm["id"], semantic_model_name=sm["displayName"],
            kql_db_id=db["id"], kql_db_name=name, src_dir=ROOT / "fabric" / "data-agent",
            description=f"Deployed by deploy_fabric.py at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
            function_columns=columns)
        it, created = self.c.ensure_item(self.ws, "DataAgent", self.names["data_agent"],
                                         folder=self.folder("products"), definition={"parts": parts})
        back = self.c.get_definition(self.ws, it["id"])
        sources = sorted({p.split("/")[3] for p in back if p.startswith("Files/Config/draft/") and p.count("/") == 4})
        log.info("data agent %s %s; readback sources=%s", it["displayName"],
                 "created" if created else "updated", ", ".join(sources) or "none")

    def step_activator(self) -> None:
        if not (self.args.alert_email or self.args.alert_teams):
            raise FabricError("activator step needs --alert-email and/or --alert-teams")
        props = self.kql_db()["properties"]
        db = self.item("KQLDatabase", "kql_database")
        uri, name = props["queryServiceUri"], self.names["kql_database"]
        # Source validation gate: both signals must have real live rows.
        counts = self.c.kusto_query(uri, name, """Events | where run_mode == 'live'
            | summarize alerts = countif(entity_type == 'operational_alert'),
                        heartbeats = countif(entity_type == 'ingestion_heartbeat')""")[0]
        if counts["heartbeats"] == 0 or (counts["alerts"] == 0 and not self.args.allow_no_alert_rows):
            raise FabricError(
                f"activator gate: live rows found {counts}. Run the poller first so heartbeats (and at "
                "least one operational alert) exist, or pass --allow-no-alert-rows to author the alert "
                "rule against the not-yet-emitting alert stream (heartbeats are still required).")
        start = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        entities = activator.build_reflex_entities(
            workspace_id=self.ws, kql_database_id=db["id"], email_recipients=self.args.alert_email,
            teams_recipients=self.args.alert_teams,
            stale_after_minutes=self.cfg["alerts"]["stale_after_minutes"], backfill_start=start)
        problems = activator.check_reference_integrity(entities)
        if problems:
            raise FabricError("activator preflight failed: " + "; ".join(problems))
        it, created = self.c.ensure_item(self.ws, "Reflex", self.names["activator"],
                                         folder=self.folder("orchestration"),
                                         definition={"parts": [part("ReflexEntities.json", json.dumps(entities))]})
        back = self.c.get_definition(self.ws, it["id"])
        n = len(json.loads(back.get("ReflexEntities.json", b"[]")))
        log.info("activator %s %s; readback entities=%d", it["displayName"],
                 "created" if created else "updated", n)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(ROOT / "config" / "fabric.yaml"))
    p.add_argument("--steps", default="all", help=f"comma list or 'all' ({','.join(STEPS)})")
    p.add_argument("--key-vault", help="Key Vault URL notebooks read secrets from (ArcGIS key)")
    p.add_argument("--store-connection-in-keyvault", metavar="KV_URL",
                   help="retrieve the Eventstream custom-endpoint connection string into Key Vault")
    p.add_argument("--alert-email", action="append", default=[])
    p.add_argument("--alert-teams", action="append", default=[])
    p.add_argument("--allow-no-alert-rows", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(args.config).read_text())
    steps = STEPS[:-1] if args.steps == "all" else [s.strip() for s in args.steps.split(",")]
    unknown = set(steps) - set(STEPS)
    if unknown:
        p.error(f"unknown steps: {unknown}")
    d = Deployer(cfg, FabricClient(), args)
    for s in steps:
        log.info("=== %s", s)
        getattr(d, f"step_{s}")()
    log.info("done: %s", ", ".join(steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
