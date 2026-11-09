"""Builds the Activator (Reflex) definition: ReflexEntities.json.

Two KQL-sourced, event-triggered rules over kqldb_mvoi (live data only):

1. "Operational alert raised"  — fires once per operational_alert event with
   alert_state == "raised". De-duplication, hysteresis and flap suppression
   already happen in the polling service (mvoi.risk.alerts), so each
   condition notifies exactly once; clears are visible on the dashboard.
2. "Ingestion stopped"         — NoHeartbeat: fires when no live
   ingestion_heartbeat event has arrived for `stale_after_minutes`.
   This is the one alert the poller cannot raise itself.

Shapes follow the documented ReflexEntities schema: kqlSource-v1 with
time-axis query parameters, SourceEvent views, EventTrigger templates
(FieldsDefaultsStep -> EventDetectStep -> ActStep), template version 1.2.4,
and `definition.instance` serialised as a JSON *string*.

Deploy-time gate (scripts/deploy_fabric.py): the Activator is only created
after the alert/heartbeat queries return real rows, per the Activator source
validation rule — never against an empty source.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

TEMPLATE_VERSION = "1.2.4"
_NS = uuid.UUID("a3c1f1d2-77b4-4f4e-8d0c-4b7e6f0a9e55")

ALERTS_QUERY = """declare query_parameters(startTime:datetime, endTime:datetime);
Events
| where ingested_at between (startTime .. endTime)
| where run_mode == "live" and entity_type == "operational_alert"
| project ingested_at, alert_key = source_id, alert_state = status, alert_type = category,
          facility_name = tostring(attributes.facility_name), message = ['title']"""

HEARTBEAT_QUERY = """declare query_parameters(startTime:datetime, endTime:datetime);
Events
| where ingested_at between (startTime .. endTime)
| where run_mode == "live" and entity_type == "ingestion_heartbeat"
| project ingested_at, service_status = status, outbox_depth = toint(attributes.outbox_depth)"""


def _id(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


def _s(value: str) -> dict[str, Any]:
    return {"type": "string", "value": value}


def _email(recipients: list[str], subject: str, headline: str, message: str,
           fields: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "name": "EmailBinding",
        "kind": "EmailMessage",
        "arguments": [
            {"name": "messageLocale", "type": "string", "value": "en-us"},
            {"name": "sentTo", "type": "array", "values": [_s(r) for r in recipients]},
            {"name": "copyTo", "type": "array", "values": []},
            {"name": "bCCTo", "type": "array", "values": []},
            {"name": "subject", "type": "array", "values": [_s(subject)]},
            {"name": "headline", "type": "array", "values": [_s(headline)]},
            {"name": "optionalMessage", "type": "array", "values": [_s(message)]},
            {"name": "additionalInformation", "type": "array", "values": [
                {"kind": "NameReferencePair", "type": "complex", "arguments": [
                    {"name": "name", "type": "string", "value": label},
                    {"kind": "EventFieldReference", "type": "complexReference", "name": "reference",
                     "arguments": [{"name": "fieldName", "type": "string", "value": field}]},
                ]} for label, field in fields
            ]},
        ],
    }


def _teams(recipients: list[str], headline: str, message: str,
           fields: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "name": "TeamsBinding",
        "kind": "TeamsMessage",
        "arguments": [
            {"name": "messageLocale", "type": "string", "value": ""},
            {"name": "recipients", "type": "array", "values": [_s(r) for r in recipients]},
            {"name": "headline", "type": "array", "values": [_s(headline)]},
            {"name": "optionalMessage", "type": "array", "values": [_s(message)]},
            {"name": "additionalInformation", "type": "array", "values": [
                {"kind": "NameReferencePair", "type": "complex", "arguments": [
                    {"name": "name", "type": "string", "value": label},
                    {"kind": "EventFieldReference", "type": "complexReference", "name": "reference",
                     "arguments": [{"name": "fieldName", "type": "string", "value": field}]},
                ]} for label, field in fields
            ]},
        ],
    }


def _kql_source(key: str, name: str, query: str, container: str, workspace_id: str,
                kql_database_id: str, backfill_start: str, interval_s: int) -> dict[str, Any]:
    return {
        "uniqueIdentifier": _id(f"source-{key}"),
        "payload": {
            "name": name,
            "runSettings": {"executionIntervalInSeconds": interval_s},
            "query": {"queryString": query},
            "eventhouseItem": {"itemId": kql_database_id, "workspaceId": workspace_id,
                               "itemType": "KustoDatabase"},
            "queryParameters": [
                {"name": "startTime", "type": "DURATION_START", "value": backfill_start},
                {"name": "endTime", "type": "DURATION_END", "value": backfill_start},
            ],
            "eventTimeSettings": {"timeFieldName": "ingested_at", "ingestionDelayInSeconds": 60,
                                  "timeZone": "UTC"},
            "metadata": {"workspaceId": workspace_id, "measureName": "", "querySetId": "",
                         "queryId": ""},
            "parentContainer": {"targetUniqueIdentifier": container},
        },
        "type": "kqlSource-v1",
    }


def _source_event(key: str, name: str, container: str) -> dict[str, Any]:
    instance = {"templateId": "SourceEvent", "templateVersion": TEMPLATE_VERSION, "steps": [{
        "name": "SourceEventStep", "id": _id(f"step-sourceevent-{key}"),
        "rows": [{"name": "SourceSelector", "kind": "SourceReference",
                  "arguments": [{"name": "entityId", "type": "string",
                                 "value": _id(f"source-{key}")}]}],
    }]}
    return {
        "uniqueIdentifier": _id(f"event-{key}"),
        "payload": {"name": name, "parentContainer": {"targetUniqueIdentifier": container},
                    "definition": {"type": "Event",
                                   "instance": json.dumps(instance, separators=(",", ":"))}},
        "type": "timeSeriesView-v1",
    }


def _event_rule(key: str, name: str, container: str, event_key: str,
                detect_rows: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    instance = {"templateId": "EventTrigger", "templateVersion": TEMPLATE_VERSION, "steps": [
        {"name": "FieldsDefaultsStep", "id": _id(f"step-fields-{key}"), "rows": [{
            "name": "EventSelector", "kind": "Event",
            "arguments": [{"name": "event", "kind": "EventReference", "type": "complex",
                           "arguments": [{"name": "entityId", "type": "string",
                                          "value": _id(f"event-{event_key}")}]}],
        }]},
        {"name": "EventDetectStep", "id": _id(f"step-detect-{key}"), "rows": detect_rows},
        {"name": "ActStep", "id": _id(f"step-act-{key}"), "rows": actions},
    ]}
    return {
        "uniqueIdentifier": _id(f"rule-{key}"),
        "payload": {
            "name": name,
            "description": "Miami Valley Ops Intelligence — managed by scripts/deploy_fabric.py",
            "parentContainer": {"targetUniqueIdentifier": container},
            "definition": {"type": "Rule", "instance": json.dumps(instance, separators=(",", ":")),
                           "settings": {"shouldRun": True, "shouldApplyRuleOnUpdate": False}},
        },
        "type": "timeSeriesView-v1",
    }


def build_reflex_entities(*, workspace_id: str, kql_database_id: str, email_recipients: list[str],
                          teams_recipients: list[str] | None = None, stale_after_minutes: int = 5,
                          backfill_start: str, dashboard_url: str = "") -> list[dict[str, Any]]:
    if not email_recipients and not teams_recipients:
        raise ValueError("at least one email or Teams recipient is required")
    container = _id("container")
    see = f" Dashboard: {dashboard_url}" if dashboard_url else ""

    alert_fields = [("Alert", "alert_type"), ("Site", "facility_name"), ("Detail", "message"),
                    ("Alert key", "alert_key")]
    alert_actions = []
    if email_recipients:
        alert_actions.append(_email(
            email_recipients, "MVOI: operational alert raised",
            "Miami Valley Ops Intelligence raised an operational alert",
            "Travel disruption near a monitored public site. Exposure is inferred from public "
            "traffic and weather data; MVOI is not affiliated with the site." + see, alert_fields))
    if teams_recipients:
        alert_actions.append(_teams(teams_recipients, "MVOI operational alert raised",
                                    "Inferred exposure — not a confirmed disruption." + see,
                                    alert_fields))

    stale_actions = []
    if email_recipients:
        stale_actions.append(_email(
            email_recipients, "MVOI: live ingestion has stopped",
            f"No live ingestion heartbeat for {stale_after_minutes} minutes",
            "Check the poller (Container App logs, /healthz) and OHGO/NWS availability. "
            "Dashboards are showing stale data." + see, [("Last status", "service_status")]))
    if teams_recipients:
        stale_actions.append(_teams(teams_recipients, "MVOI live ingestion has stopped",
                                    f"No heartbeat for {stale_after_minutes} minutes." + see,
                                    [("Last status", "service_status")]))

    return [
        {"uniqueIdentifier": container,
         "payload": {"name": "Miami Valley Ops Intelligence", "type": "kqlQueries"},
         "type": "container-v1"},
        _kql_source("alerts", "Operational alerts (live)", ALERTS_QUERY, container, workspace_id,
                    kql_database_id, backfill_start, 60),
        _source_event("alerts", "Operational alert events", container),
        _kql_source("heartbeat", "Ingestion heartbeat (live)", HEARTBEAT_QUERY, container,
                    workspace_id, kql_database_id, backfill_start, 60),
        _source_event("heartbeat", "Heartbeat events", container),
        _event_rule("alert-raised", "Operational alert raised", container, "alerts",
                    [{"name": "EventFieldSelector", "kind": "EventField",
                      "arguments": [{"name": "fieldName", "type": "string", "value": "alert_state"}]},
                     {"name": "TextValueCondition", "kind": "TextValueCondition",
                      "arguments": [{"name": "op", "type": "string", "value": "IsEqualTo"},
                                    {"name": "value", "type": "string", "value": "raised"}]}],
                    alert_actions),
        _event_rule("ingestion-stopped", "Ingestion stopped", container, "heartbeat",
                    [{"name": "NoHeartbeat", "kind": "NoHeartbeat",
                      "arguments": [{"name": "duration", "type": "timeSpan",
                                     "value": stale_after_minutes * 60_000}]}],
                    stale_actions),
    ]


def check_reference_integrity(entities: list[dict[str, Any]]) -> list[str]:
    """Activator 'Reference Integrity Preflight'. Returns a list of problems (empty = ok)."""
    by_id = {e["uniqueIdentifier"]: e for e in entities}
    problems = []
    if len(by_id) != len(entities):
        problems.append("duplicate uniqueIdentifier")

    def need(ref: str, expected_type: str, where: str):
        target = by_id.get(ref)
        if target is None:
            problems.append(f"{where}: unresolved reference {ref}")
        elif target["type"] != expected_type:
            problems.append(f"{where}: {ref} is {target['type']}, expected {expected_type}")

    def walk(node: Any, where: str):
        if isinstance(node, dict):
            if node.get("kind") in ("SourceReference", "EventReference", "AttributeReference"):
                for a in node.get("arguments", []):
                    if a.get("name") == "entityId":
                        kind = "kqlSource-v1" if node["kind"] == "SourceReference" else "timeSeriesView-v1"
                        need(a["value"], kind, where)
            for v in node.values():
                walk(v, where)
        elif isinstance(node, list):
            for v in node:
                walk(v, where)

    for e in entities:
        p = e["payload"]
        for key in ("parentContainer", "parentObject"):
            if key in p:
                need(p[key]["targetUniqueIdentifier"],
                     "container-v1" if key == "parentContainer" else "timeSeriesView-v1",
                     f"{e['uniqueIdentifier']}.{key}")
        inst = (p.get("definition") or {}).get("instance")
        if inst is not None:
            if not isinstance(inst, str):
                problems.append(f"{e['uniqueIdentifier']}: definition.instance must be a JSON string")
                continue
            parsed = json.loads(inst)
            for step in parsed.get("steps", []):
                if not step.get("id"):
                    problems.append(f"{e['uniqueIdentifier']}: step {step.get('name')} missing id")
            walk(parsed, e["payload"].get("name", e["uniqueIdentifier"]))
    return problems
