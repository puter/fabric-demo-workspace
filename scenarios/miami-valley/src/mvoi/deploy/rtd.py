"""Builds the Real-Time Dashboard definition (RealTimeDashboard.json).

Format: Real-Time Dashboard / ADX dashboard schema v89
(https://dataexplorer.azure.com/static/d/schema/89/dashboard.json), validated
offline in tests/unit/test_rtd.py against the vendored schema files.

Every tile queries a KQL function from fabric/eventhouse/kqldb_mvoi.kql, so
dashboard logic lives in version-controlled KQL rather than in tile text.
The `_mode` parameter switches all tiles between live data and the most
recent replay session.

Branding: the Operations and Pipeline health pages open with a Markdown header carrying the SD Labs badge
as an embedded data URI. The tile renders only Markdown (HTML is stripped)
and external image URLs are not shown, so the image must be inline.

Ids are deterministic (uuid5 of a name) so redeploying updates in place.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 89
LOGO = Path(__file__).parents[3] / "fabric" / "dashboards" / "sd-labs-badge.png"
HEADER_H = 2  # data tiles sit below the header row on every page
_NS = uuid.UUID("0b8f8a54-3f4e-4d47-9c35-6a0ad3c2f7e1")


def _id(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


@dataclass
class Tile:
    key: str
    title: str
    page: str
    query: str
    visual: str
    x: int
    y: int
    w: int
    h: int
    options: dict[str, Any] = field(default_factory=dict)
    description: str | None = None


def _sev_color_rules(column: str) -> list[dict[str, Any]]:
    """Severity colouring for tables (critical/major red, moderate amber)."""
    rules = []
    for value, color in (("critical", "red"), ("major", "red"), ("moderate", "yellow")):
        rules.append({
            "id": _id(f"rule-{column}-{value}"),
            "ruleType": "colorByCondition",
            "applyToColumn": column,
            "hideText": False,
            "applyTo": "cells",
            "conditions": [{"operator": "==", "column": column, "values": [value]}],
            "chainingOperator": "and",
            "colorStyle": "bold",
            "color": color,
            "tag": "",
            "icon": None,
            "ruleName": f"{column} {value}",
            "visualType": "table",
        })
    return rules


def _score_color_rules() -> list[dict[str, Any]]:
    out = []
    for lo, color in ((85, "red"), (70, "red"), (50, "yellow")):
        out.append({
            "id": _id(f"rule-score-{lo}"), "ruleType": "colorByCondition", "applyToColumn": "score",
            "hideText": False, "applyTo": "cells",
            "conditions": [{"operator": ">=", "column": "score", "values": [str(lo)]}],
            "chainingOperator": "and", "colorStyle": "bold", "color": color, "tag": "", "icon": None,
            "ruleName": f"score >= {lo}", "visualType": "table",
        })
    return out


# Brand palette (from the SD Labs badge) for neutral series. Severity and
# site-band colours stay red/purple so alerts read correctly.
BRAND_GREEN = "rgba(91, 177, 49, 1)"         # #5BB131
BRAND_LIGHT_GREEN = "rgba(142, 211, 106, 1)"  # #8ED36A
BRAND_GREY = "rgba(130, 132, 140, 1)"         # #82848C

# Presenter comes first: it is the page Fabric Embed opens (the SDK embeds a
# whole dashboard, not a page or tile). It has no header; the presenter app
# supplies its own.
PAGES = {"presenter": "Presenter", "ops": "Operations", "health": "Pipeline health"}
HEADER_PAGES = ("ops", "health")

M = "_mode"  # dashboard parameter variable
S = "_site"  # Presenter page site picker
# Picker options: Sinclair (the venue) first, so it is the default.
SITE_PICKER_QUERY = ('Facilities | extend o = iff(facility_id == "FAC-001", 0, 1) '
                     "| order by o asc, facility_name asc | project facility_name")

# The live map is shared by the Operations and Presenter pages: one query.
# Traffic incidents only, one series per severity, with the legend shown.
# The RTD map ignores seriesColors and colours each label from its palette in
# order of appearance, so rows are sorted by severity to keep the order
# stable, and the legend keeps colour readable if it still shifts. Size is
# fixed: the map scales size between the smallest and largest value present
# (rows without coordinates plot at 0,0), so the worst current incident would
# always be drawn at full size.
#
# The Presenter map shows only incidents within SITE_MAP_RADIUS_MI of the
# picked site, so the map auto-centres and zooms on that area (it fits the
# points it is given); the Operations map is the whole region. The site
# incidents table keeps the 5-mile scoring radius.
MAP_TITLE = "Live map · active traffic incidents by severity"
SITE_MAP_RADIUS_MI = 3
SITE_MAP_TITLE = (f"Live map · traffic incidents within {SITE_MAP_RADIUS_MI} miles of the site, "
                  "by severity")


def _map_query(incidents: str) -> str:
    return f"""{incidents}
| order by severity_rank asc
| project latitude, longitude,
          severity = strcat(toupper(substring(severity, 0, 1)), substring(severity, 1))"""


MAP_QUERY = _map_query(f"ActiveIncidents({M})")
SITE_MAP_QUERY = _map_query(f"IncidentsNearSite({M}, {S}, {SITE_MAP_RADIUS_MI * 1609.344})")
MAP_OPTIONS = {"map__type": "bubble", "map__geoType": "numeric",
               "map__latitudeColumn": "latitude", "map__longitudeColumn": "longitude",
               "map__labelColumn": "severity", "map__sizeColumn": None,
               "map__sizeDisabled": True, "map__bubbleFormat": "dot",
               "hideLegend": False, "legendLocation": "bottom"}
MAP_DESCRIPTION = ("Map of active traffic incidents, coloured by severity (see the legend; "
                   "click a point for its severity). Incident details are in the Active "
                   "incidents table.")

TILES: list[Tile] = [
    # ---- KPI row
    Tile("kpi-active", "Active transportation events", "ops",
         f"ActiveIncidents({M}) | count", "card", 0, 0, 4, 3),
    Tile("kpi-new-hour", "New in last hour", "ops",
         f"NewIncidentWindows({M}) | project last_60m", "card", 4, 0, 4, 3),
    Tile("kpi-closures", "Active road closures", "ops",
         f"ActiveIncidents({M}) | where is_road_closure | count", "card", 8, 0, 4, 3),
    Tile("kpi-elevated", "Sites with elevated+ disruption", "ops",
         f"CurrentFacilityRisk({M}) | where score >= 50 | count", "card", 12, 0, 4, 3),
    Tile("kpi-alerts", "Open operational alerts", "ops",
         f"OpenAlerts({M}) | count", "card", 16, 0, 4, 3),
    Tile("kpi-fresh", "Minutes since last ingestion", "ops",
         f"IngestionFreshness({M}) | summarize minutes = round(min(minutes_since_heartbeat), 1)",
         "card", 20, 0, 4, 3,
         description="Heartbeat from the poller (live) or replay session; > 5 means stale."),
    # ---- Map + risk
    Tile("map", MAP_TITLE, "ops", MAP_QUERY, "map", 0, 3, 14, 12,
         options=MAP_OPTIONS, description=MAP_DESCRIPTION),
    Tile("risk-table", "Site disruption exposure (inferred)", "ops",
         f"CurrentFacilityRisk({M}) | where score > 0 "
         "| project facility_name, city, score, band, explanation, scored_at",
         "table", 14, 3, 10, 6, options={"colorRules": _score_color_rules(),
                                         "colorRulesDisabled": False}),
    Tile("alerts", "Open alerts", "ops",
         f"OpenAlerts({M}) | project raised_at, alert_type, facility_name, message",
         "table", 14, 9, 10, 6),
    # ---- Detail row
    Tile("incidents", "Active incidents", "ops",
         f"ActiveIncidents({M}) | order by severity_rank desc, first_seen_at desc "
         "| project severity, entity_type, corridor_id, route_name, direction, location, ['title'], "
         "minutes_active, is_road_closure, synthetic",
         "table", 0, 15, 14, 7, options={"colorRules": _sev_color_rules("severity"),
                                         "colorRulesDisabled": False}),
    Tile("by-corridor", "Active events by corridor", "ops",
         f"IncidentCountsByCorridor({M}) | project corridor, active, closures",
         "bar", 14, 15, 10, 7,
         options={"seriesColors": {"active": BRAND_GREEN, "closures": BRAND_LIGHT_GREEN}}),
    Tile("weather", "Active weather alerts", "ops",
         f"ActiveWeatherAlerts({M}) | project event, cap_severity, area, onset, expires, geometry_source",
         "table", 0, 22, 12, 6),
    Tile("risk-trend", "Regional risk index", "ops",
         f"RegionalRiskTrend({M}, 6h, 5m)", "timechart", 12, 22, 12, 6,
         options={"seriesColors": {"mean_score": BRAND_GREEN, "max_score": BRAND_LIGHT_GREEN,
                                   "elevated_facilities": BRAND_GREY}}),
    Tile("activity", "Recent activity", "ops",
         f"RecentActivity({M}, 40)", "table", 0, 28, 24, 7,
         options={"colorRules": _sev_color_rules("severity"), "colorRulesDisabled": False}),
    # ---- Presenter page (embedded by the local presenter app)
    Tile("p-kpi-active", "Active transportation events", "presenter",
         f"ActiveIncidents({M}) | count", "card", 0, 0, 5, 3),
    Tile("p-kpi-closures", "Active road closures", "presenter",
         f"ActiveIncidents({M}) | where is_road_closure | count", "card", 5, 0, 5, 3),
    Tile("p-kpi-elevated", "Sites with elevated+ disruption", "presenter",
         f"CurrentFacilityRisk({M}) | where score >= 50 | count", "card", 10, 0, 5, 3),
    Tile("p-kpi-alerts", "Open operational alerts", "presenter",
         f"OpenAlerts({M}) | count", "card", 15, 0, 5, 3),
    Tile("p-kpi-fresh", "Minutes since last update", "presenter",
         f"IngestionFreshness({M}) | summarize minutes = round(min(minutes_since_heartbeat), 1)",
         "card", 20, 0, 4, 3,
         description="Heartbeat from the poller (live) or replay session; > 5 means stale."),
    Tile("p-map", SITE_MAP_TITLE, "presenter", SITE_MAP_QUERY, "map", 0, 3, 14, 14,
         options=MAP_OPTIONS, description=MAP_DESCRIPTION),
    Tile("p-site", "Site spotlight (pick a site above)", "presenter",
         f"CurrentFacilityRisk({M}) | where facility_name == {S} "
         "| project score, band, explanation, scored_at",
         "table", 14, 3, 10, 4, options={"colorRules": _score_color_rules(),
                                         "colorRulesDisabled": False}),
    Tile("p-site-incidents", "Active incidents within 5 miles of the site", "presenter",
         f"IncidentsNearSite({M}, {S}) "
         "| project severity, distance_mi, route_name, direction, ['title'], minutes_active, "
         "is_road_closure",
         "table", 14, 7, 10, 5, options={"colorRules": _sev_color_rules("severity"),
                                         "colorRulesDisabled": False}),
    Tile("p-sites", "All sites by disruption exposure (inferred)", "presenter",
         f"CurrentFacilityRisk({M}) | project facility_name, city, score, band, explanation",
         "table", 14, 12, 10, 5, options={"colorRules": _score_color_rules(),
                                          "colorRulesDisabled": False}),
    # ---- Pipeline health page
    Tile("freshness", "Source freshness", "health",
         f"IngestionFreshness({M})", "table", 0, 0, 24, 6),
    Tile("arrival", "Event arrival rate", "health",
         f"EventArrivalRate({M}, 6h, 5m)", "timechart", 0, 6, 12, 7),
    Tile("latency", "Ingestion latency (s)", "health",
         f"IngestionLatency({M}, 6h)", "timechart", 12, 6, 12, 7,
         options={"seriesColors": {"p50_poll_to_publish_s": BRAND_GREEN,
                                   "p95_poll_to_publish_s": BRAND_LIGHT_GREEN,
                                   "p50_publish_to_eventhouse_s": BRAND_GREY,
                                   "p95_publish_to_eventhouse_s": "rgba(27, 32, 46, 1)"}}),
    Tile("dq", "Data quality (24h)", "health",
         f"DataQualitySummary({M}, 24h)", "table", 0, 13, 24, 6),
]


def header_markdown(page_name: str) -> str:
    logo = base64.b64encode(LOGO.read_bytes()).decode()
    return (f"## ![SD Labs](data:image/png;base64,{logo}) Miami Valley Operations Intelligence"
            f" · {page_name}\n\n"
            "Live public traffic and weather around **Sinclair Conference Center**, Dayton. "
            "Sites are real public places; SD Labs is not affiliated with them.")


def build_dashboard(*, title: str, cluster_uri: str, database_id: str, workspace_id: str,
                    database_name: str = "kqldb_mvoi") -> dict[str, Any]:
    ds_id = _id("datasource")
    param_id = _id("param-mode")
    site_query_id = _id("query-site-picker")
    pages = [{"id": _id(f"page-{k}"), "name": v} for k, v in PAGES.items()]
    queries, tiles = [], []
    for key in HEADER_PAGES:
        name = PAGES[key]
        tiles.append({
            "id": _id(f"tile-header-{key}"),
            "title": "",
            "layout": {"x": 0, "y": 0, "width": 24, "height": HEADER_H},
            "pageId": _id(f"page-{key}"),
            "visualType": "markdownCard",
            "markdownText": header_markdown(name),
            "visualOptions": {},
        })
    for t in TILES:
        qid = _id(f"query-{t.key}")
        queries.append({
            "id": qid,
            "dataSource": {"kind": "inline", "dataSourceId": ds_id},
            "text": t.query,
            "usedVariables": [v for v in (M, S) if v in t.query],
        })
        tile: dict[str, Any] = {
            "id": _id(f"tile-{t.key}"),
            "title": t.title,
            "queryRef": {"kind": "query", "queryId": qid},
            "layout": {"x": t.x, "y": t.y + (HEADER_H if t.page in HEADER_PAGES else 0),
                       "width": t.w, "height": t.h},
            "pageId": _id(f"page-{t.page}"),
            "visualType": t.visual,
            "visualOptions": dict(t.options),
        }
        if t.description:
            tile["description"] = t.description
        tiles.append(tile)

    return {
        "$schema": f"https://dataexplorer.azure.com/static/d/schema/{SCHEMA_VERSION}/dashboard.json",
        "schema_version": SCHEMA_VERSION,
        "title": title,
        "autoRefresh": {"enabled": True, "defaultInterval": "30s", "minInterval": "10s"},
        # Live updates: tiles re-query when the Eventhouse table changes,
        # no faster than every 10 s; falls back to 30 s polling.
        "changeDetection": {"kind": "liveUpdates", "minRefreshRate": "10s",
                            "fallbackRefreshRate": "30s"},
        "baseQueries": [],
        "embeddedApps": [],
        "dataSources": [{
            "id": ds_id, "name": database_name, "kind": "kusto-trident",
            "clusterUri": cluster_uri, "database": database_id,
            "databaseArtifactId": database_id, "workspace": workspace_id,
        }],
        "pages": pages,
        "parameters": [{
            "kind": "string",
            "id": param_id,
            "displayName": "Data",
            "description": "Live public data, or the most recent demo replay session",
            "variableName": M,
            "selectionType": "scalar",
            "includeAllOption": False,
            "defaultValue": {"kind": "value", "value": "live"},
            "dataSource": {"kind": "static", "values": [
                {"displayText": "Live", "value": "live"},
                {"displayText": "Demo replay (latest session)", "value": "replay"},
            ]},
            "showOnPages": {"kind": "all"},
        }, {
            "kind": "string",
            "id": _id("param-site"),
            "displayName": "Site",
            "description": "Site shown in the Presenter page spotlight",
            "variableName": S,
            "selectionType": "scalar",
            "includeAllOption": False,
            "defaultValue": {"kind": "query-result"},
            "dataSource": {"kind": "query", "queryRef": {"kind": "query", "queryId": site_query_id},
                           "columns": {"value": "facility_name"}},
            "showOnPages": {"kind": "selection", "pageIds": [_id("page-presenter")]},
        }],
        "queries": queries + [{
            "id": site_query_id,
            "dataSource": {"kind": "inline", "dataSourceId": ds_id},
            "text": SITE_PICKER_QUERY,
            "usedVariables": [],
        }],
        "tiles": tiles,
    }
