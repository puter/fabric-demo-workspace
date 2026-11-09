"""Builds the Fabric Data Agent definition for `da_mvoi` (ADR 0003).

Two data sources:
* `sm_mvoi_historical` (semantic_model): history and trends via the model's
  measures. Semantic model sources take no example queries.
* `kqldb_mvoi` (kusto): the current state via the stored KQL functions, plus
  few-shot question -> KQL examples.

Authored content lives in fabric/data-agent/ (instructions + few-shots) and is
rendered into the documented definition layout:

    Files/Config/data_agent.json
    Files/Config/{draft,published}/stage_config.json
    Files/Config/{draft,published}/<prefix>-<name>/datasource.json
    Files/Config/{draft,published}/kusto-kqldb_mvoi/fewshots.json
    Files/Config/publish_info.json                (published only)

Folder prefixes follow the Data Agent source-control docs (`semantic-model-`,
`kusto-`). Data source `type` values use the definition enum (`semantic_model`,
`kusto`).

KQL functions are `kusto.function` children of one `function_grouping`
element named "Functions", as the Fabric UI writes them (read back from a
UI-published agent). Top-level `kusto.function` elements are stored but
ignored: neither the UI nor the agent's query generator sees them. The
documented `kusto.functions` type is rejected as "invalid JSON". Data source
files use the full dataSource schema URL the service writes back.

Ids are deterministic (uuid5 of a name) so redeploying updates in place.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from mvoi.deploy import semantic
from mvoi.deploy.fabric_api import part

_NS = uuid.UUID("5d0f3e6a-2b7c-4c91-9a4e-8f1d2c6b7a30")
_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/dataAgent/definition"

KQL_TABLES = ("Events", "Facilities", "Corridors", "Zones")
# Element descriptions are all the agent's query generator knows about an item:
# without them it saw function names only and fell back to querying Events.
KQL_TABLE_DESCRIPTIONS = {
    "Events": "Raw change log: one row per lifecycle event (created, updated, resolved...) of every "
              "entity, live and replay. Its rows are not incidents; never count them. Use the "
              "functions, which return current state per entity.",
    "Facilities": "Monitored sites: facility_name, facility_type, city, latitude, longitude, criticality.",
    "Corridors": "Highway corridors: corridor_id (I-75, I-70, US-35...), corridor_name, geometry.",
    "Zones": "NWS county and forecast zones in the area of interest: zone_id, zone_type, name, geometry.",
}
# User-facing functions only; Core helpers and the Activator probe stay hidden.
KQL_FUNCTIONS = (
    "ActiveIncidents", "NewIncidents", "NewIncidentWindows", "IncidentCountsByCorridor",
    "IncidentsBySeverity", "ActiveWeatherAlerts", "WeatherAlertsByRegion", "CurrentFacilityRisk",
    "IncidentsNearSite", "FacilityRiskTimeline", "RegionalRiskTrend", "OpenAlerts", "IngestionFreshness",
    "DataQualitySummary",
)

_FUNCTION_RE = re.compile(r"^\.create-or-alter function[^\n]*\n(\w+)\(", re.MULTILINE)
_SIGNATURE_RE = re.compile(
    r'^\.create-or-alter function with \([^\n]*docstring = "((?:[^"\\]|\\.)*)"\)\n(\w+)\(([^)]*)\)',
    re.MULTILINE)


def _id(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


def kql_function_names(kql_path: Path) -> set[str]:
    return set(_FUNCTION_RE.findall(kql_path.read_text(encoding="utf-8")))


def kql_function_docs(kql_path: Path) -> dict[str, tuple[str, str]]:
    """name -> (parameter list, docstring), as written in the .kql file."""
    return {name: (params, doc) for doc, name, params in
            _SIGNATURE_RE.findall(kql_path.read_text(encoding="utf-8"))}


def function_description(name: str, params: str, doc: str, columns: list[str] | None = None) -> str:
    text = f"{name}({params}). {doc}."
    if "," in params:
        text += " Pass every argument except mode by name."
    if columns:
        text += " Columns: " + ", ".join(columns) + "."
    return text


def load_fewshots(path: Path) -> list[dict[str, str]]:
    return [{"id": _id(f"fewshot:{s['question']}"), "question": s["question"], "query": s["query"].strip()}
            for s in yaml.safe_load(path.read_text(encoding="utf-8"))]


def _semantic_elements() -> list[dict[str, Any]]:
    return [{"id": _id(f"sm:{t.name}"), "display_name": t.name, "type": "semantic_model.table",
             "is_selected": True, "description": t.desc} for t in semantic.TABLES]


def _kusto_elements(kql_path: Path, columns: dict[str, list[str]] | None) -> list[dict[str, Any]]:
    docs = kql_function_docs(kql_path)
    tables = [{"id": _id(f"kql:table:{n}"), "display_name": n, "type": "kusto.table", "is_selected": True,
               "description": KQL_TABLE_DESCRIPTIONS[n]} for n in KQL_TABLES]
    functions = [{"id": _id(f"kql:function:{n}"), "display_name": n, "type": "kusto.function",
                  "is_selected": True,
                  "description": function_description(n, *docs[n], (columns or {}).get(n)), "children": []}
                 for n in KQL_FUNCTIONS]
    group = {"id": _id("kql:function_grouping"), "display_name": "Functions", "type": "function_grouping",
             "is_selected": True, "description": None, "children": functions}
    return tables + [group]


def _datasource(*, artifact_id: str, workspace_id: str, name: str, ds_type: str, description: str,
                instructions: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"$schema": f"{_SCHEMA}/dataSource/1.0.0/schema.json", "artifactId": artifact_id,
            "workspaceId": workspace_id, "displayName": name, "type": ds_type, "userDescription": description,
            "dataSourceInstructions": instructions, "elements": elements}


def build_files(*, workspace_id: str, semantic_model_id: str, semantic_model_name: str,
                kql_db_id: str, kql_db_name: str, src_dir: Path, publish: bool = True,
                description: str = "",
                function_columns: dict[str, list[str]] | None = None) -> dict[str, dict[str, Any]]:
    """Definition files keyed by part path, as JSON-ready dicts.

    function_columns (name -> output columns) is read from the live database at
    deploy time; without it the function descriptions omit columns.
    """
    kql_path = src_dir.parent / "eventhouse" / f"{kql_db_name}.kql"
    read = lambda name: (src_dir / name).read_text(encoding="utf-8").strip()  # noqa: E731
    stage = {
        "stage_config.json": {"$schema": f"{_SCHEMA}/stageConfiguration/1.0.0/schema.json",
                              "aiInstructions": read("instructions.md")},
        f"semantic-model-{semantic_model_name}/datasource.json": _datasource(
            artifact_id=semantic_model_id, workspace_id=workspace_id, name=semantic_model_name,
            ds_type="semantic_model", description="Historical trends from the Gold lakehouse (hourly).",
            instructions=read(f"{semantic_model_name}.instructions.md"), elements=_semantic_elements()),
        f"kusto-{kql_db_name}/datasource.json": _datasource(
            artifact_id=kql_db_id, workspace_id=workspace_id, name=kql_db_name, ds_type="kusto",
            description="Current incidents, weather alerts, facility risk and feed health (live + replay).",
            instructions=read(f"{kql_db_name}.instructions.md"),
            elements=_kusto_elements(kql_path, function_columns)),
        f"kusto-{kql_db_name}/fewshots.json": {
            "$schema": f"{_SCHEMA}/fewShots/1.0.0/schema.json",
            "fewShots": load_fewshots(src_dir / f"{kql_db_name}.fewshots.yaml")},
    }
    files = {"Files/Config/data_agent.json": {"$schema": f"{_SCHEMA}/dataAgent/2.1.0/schema.json"}}
    files.update({f"Files/Config/draft/{k}": v for k, v in stage.items()})
    if publish:
        files.update({f"Files/Config/published/{k}": v for k, v in stage.items()})
        files["Files/Config/publish_info.json"] = {
            "$schema": f"{_SCHEMA}/publishInfo/1.0.0/schema.json", "description": description}
    return files


def definition_parts(**kwargs) -> list[dict[str, str]]:
    return [part(path, json.dumps(body, indent=2)) for path, body in build_files(**kwargs).items()]
