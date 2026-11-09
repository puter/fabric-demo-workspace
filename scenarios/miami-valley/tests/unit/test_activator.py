import json
import re
from pathlib import Path

import pytest

from mvoi.deploy.activator import (
    ALERTS_QUERY,
    HEARTBEAT_QUERY,
    build_reflex_entities,
    check_reference_integrity,
)

KQL = Path(__file__).parents[2] / "fabric" / "eventhouse" / "kqldb_mvoi.kql"


def _entities(**kw):
    args = dict(workspace_id="w" * 8, kql_database_id="d" * 8, email_recipients=["ops@example.com"],
                backfill_start="2026-10-09T00:00:00Z")
    args.update(kw)
    return build_reflex_entities(**args)


def test_reference_integrity_passes():
    assert check_reference_integrity(_entities(teams_recipients=["ops@example.com"])) == []


def test_preflight_catches_broken_reference():
    ents = _entities()
    ents = [e for e in ents if e["payload"].get("name") != "Heartbeat events"]
    problems = check_reference_integrity(ents)
    assert any("unresolved" in p for p in problems)


def test_instances_are_json_strings_with_step_ids():
    for e in _entities():
        inst = e["payload"].get("definition", {}).get("instance")
        if inst is not None:
            assert isinstance(inst, str)
            assert all(s["id"] for s in json.loads(inst)["steps"])


def test_rules_are_event_triggers_with_expected_detectors():
    rules = {e["payload"]["name"]: json.loads(e["payload"]["definition"]["instance"])
             for e in _entities() if e["payload"].get("definition", {}).get("type") == "Rule"}
    raised = rules["Operational alert raised"]
    assert raised["templateId"] == "EventTrigger"
    detect = raised["steps"][1]["rows"]
    assert detect[0]["arguments"][0]["value"] == "alert_state"
    assert detect[1]["arguments"][1]["value"] == "raised"
    stopped = rules["Ingestion stopped"]["steps"][1]["rows"][0]
    assert stopped["kind"] == "NoHeartbeat" and stopped["arguments"][0]["value"] == 300_000


def test_requires_a_recipient():
    with pytest.raises(ValueError):
        _entities(email_recipients=[], teams_recipients=[])


def test_deterministic():
    assert _entities() == _entities()


@pytest.mark.parametrize("query", [ALERTS_QUERY, HEARTBEAT_QUERY])
def test_queries_use_time_axis_and_known_columns(query):
    assert query.startswith("declare query_parameters(startTime:datetime, endTime:datetime);")
    table_cols = set(re.findall(r"^\s{4}(\w+):", KQL.read_text().split(".create-merge table Facilities")[0],
                                flags=re.M))
    for col in ("ingested_at", "run_mode", "entity_type", "status", "attributes"):
        assert col in table_cols and col in query
