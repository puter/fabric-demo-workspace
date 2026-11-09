"""Data Agent definition: layout, and references to real KQL functions and DAX measures."""

import base64
import json
import re
from pathlib import Path

import pytest

from mvoi.deploy import dataagent, semantic

ROOT = Path(__file__).parents[2]
SRC = ROOT / "fabric" / "data-agent"
KQL = ROOT / "fabric" / "eventhouse" / "kqldb_mvoi.kql"
WS, SM, DB = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", \
    "33333333-3333-3333-3333-333333333333"
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _files(**kw):
    return dataagent.build_files(workspace_id=WS, semantic_model_id=SM,
                                 semantic_model_name="sm_mvoi_historical", kql_db_id=DB,
                                 kql_db_name="kqldb_mvoi", src_dir=SRC, **kw)


def test_layout_draft_and_published():
    files = _files()
    for stage in ("draft", "published"):
        assert f"Files/Config/{stage}/stage_config.json" in files
        assert f"Files/Config/{stage}/semantic-model-sm_mvoi_historical/datasource.json" in files
        assert f"Files/Config/{stage}/kusto-kqldb_mvoi/datasource.json" in files
        assert f"Files/Config/{stage}/kusto-kqldb_mvoi/fewshots.json" in files
        # Semantic model sources do not support example queries.
        assert f"Files/Config/{stage}/semantic-model-sm_mvoi_historical/fewshots.json" not in files
    assert "Files/Config/data_agent.json" in files and "Files/Config/publish_info.json" in files


def test_draft_only_when_not_publishing():
    files = _files(publish=False)
    assert not any("published" in p or "publish_info" in p for p in files)


def test_datasources_bound_to_arguments():
    files = _files()
    sm = files["Files/Config/draft/semantic-model-sm_mvoi_historical/datasource.json"]
    kql = files["Files/Config/draft/kusto-kqldb_mvoi/datasource.json"]
    assert (sm["type"], sm["artifactId"], sm["workspaceId"]) == ("semantic_model", SM, WS)
    assert (kql["type"], kql["artifactId"], kql["workspaceId"]) == ("kusto", DB, WS)
    assert sm["dataSourceInstructions"] and kql["dataSourceInstructions"]
    assert {e["display_name"] for e in sm["elements"]} == {t.name for t in semantic.TABLES}


def test_parts_are_base64_json():
    parts = dataagent.definition_parts(workspace_id=WS, semantic_model_id=SM,
                                       semantic_model_name="sm_mvoi_historical", kql_db_id=DB,
                                       kql_db_name="kqldb_mvoi", src_dir=SRC)
    for p in parts:
        assert p["payloadType"] == "InlineBase64"
        json.loads(base64.b64decode(p["payload"]))


def test_no_ids_except_arguments_and_deterministic_element_ids():
    a, b = _files(), _files()
    assert a == b
    text = json.dumps(a)
    element_ids = {x["id"] for f in a.values() for e in f.get("elements", [])
                   for x in [e, *e.get("children", [])]}
    fewshot_ids = {s["id"] for f in a.values() for s in f.get("fewShots", [])}
    assert set(GUID.findall(text)) <= {WS, SM, DB} | element_ids | fewshot_ids


def _kusto_leaves(files):
    out = []
    for e in files["Files/Config/draft/kusto-kqldb_mvoi/datasource.json"]["elements"]:
        out.extend(e["children"] if e["type"] == "function_grouping" else [e])
    return out


def test_functions_are_grouped_like_the_fabric_ui_writes_them():
    # Top-level kusto.function elements are stored but ignored by the service.
    elements = _files()["Files/Config/draft/kusto-kqldb_mvoi/datasource.json"]["elements"]
    tables = ["kusto.table"] * len(dataagent.KQL_TABLES)
    assert [e["type"] for e in elements] == tables + ["function_grouping"]
    group = elements[-1]
    assert group["display_name"] == "Functions" and group["is_selected"]
    assert [f["display_name"] for f in group["children"]] == list(dataagent.KQL_FUNCTIONS)
    assert {f["type"] for f in group["children"]} == {"kusto.function"}


def test_every_kql_element_is_described():
    # The agent's query generator sees only element descriptions; without them it
    # did not know the functions' parameters or columns and queried Events instead.
    elements = _kusto_leaves(_files())
    assert {e["display_name"] for e in elements} == set(dataagent.KQL_TABLES) | set(dataagent.KQL_FUNCTIONS)
    for e in elements:
        assert e["description"], e["display_name"]
        if e["type"] == "kusto.function":
            assert e["description"].startswith(e["display_name"] + "(mode: string")


def test_function_descriptions_carry_columns_and_named_argument_hint():
    files = _files(function_columns={"IncidentsNearSite": ["entity_key", "distance_mi"]})
    elements = {e["display_name"]: e for e in _kusto_leaves(files)}
    near = elements["IncidentsNearSite"]["description"]
    assert 'site: string = "Sinclair Conference Center"' in near
    assert "by name" in near and near.endswith("Columns: entity_key, distance_mi.")
    assert "by name" not in elements["CurrentFacilityRisk"]["description"]  # mode is its only argument


def test_selected_kql_functions_exist():
    assert set(dataagent.KQL_FUNCTIONS) <= dataagent.kql_function_names(KQL)


def test_kql_instructions_name_only_selected_functions():
    named = set(re.findall(r"\b([A-Z]\w+)\(\)", (SRC / "kqldb_mvoi.instructions.md").read_text()))
    assert named and named <= set(dataagent.KQL_FUNCTIONS)


@pytest.mark.parametrize("shot", dataagent.load_fewshots(SRC / "kqldb_mvoi.fewshots.yaml"),
                         ids=lambda s: s["question"][:40])
def test_fewshots_start_from_a_selected_function(shot):
    first = re.match(r"(\w+)\(", shot["query"])
    assert first and first.group(1) in dataagent.KQL_FUNCTIONS, shot["query"]


def test_semantic_instructions_name_real_measures():
    measures = {m.name for t in semantic.TABLES for m in t.measures}
    named = set(re.findall(r"\[([^\]]+)\]", (SRC / "sm_mvoi_historical.instructions.md").read_text()))
    assert named and named <= measures, named - measures
