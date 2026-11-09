"""Direct Lake semantic model `sm_mvoi_historical` — generated TMDL.

The model is declared here as data and rendered to TMDL (tabs, `///`
descriptions, measures before columns, EntityPartitionSource/directLake),
so column-to-Gold mappings are unit-tested against the Spark job schemas.

    uv run python -m mvoi.deploy.semantic      # regenerates fabric/semantic-model/

Rendered files are committed for review with `{{WORKSPACE_ID}}` and
`{{LAKEHOUSE_ID}}` placeholders; scripts/deploy_fabric.py fills them in.

Deliberate guideline deviations (documented in docs/power-bi.md):
* latitude/longitude and Gi* statistics stay `double` — Direct Lake maps the
  Delta type as-is and geographic precision matters more than compression;
* Hot Spots is a standalone map table (grid bins have no date or site).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

MODEL_NAME = "sm_mvoi_historical"
EXPR = "DirectLake - MVOI Lakehouse"


@dataclass
class Col:
    name: str
    source: str
    dtype: str                       # int64 | string | double | dateTime | boolean
    hidden: bool = False
    summarize_none: bool = True
    category: str | None = None      # Latitude | Longitude | City
    sort_by: str | None = None
    fmt: str | None = None
    desc: str | None = None
    is_key: bool = False


@dataclass
class Measure:
    name: str
    dax: str
    fmt: str
    folder: str
    desc: str


@dataclass
class Table:
    name: str
    entity: str
    desc: str
    cols: list[Col]
    measures: list[Measure] = field(default_factory=list)
    data_category: str | None = None


H = dict(hidden=True)

TABLES: list[Table] = [
    Table("Date", "gold_date", "Calendar dimension (one row per day), conformed across all facts.", [
        Col("Date", "date", "dateTime", is_key=True, fmt="yyyy-mm-dd"),
        Col("Year", "year", "int64", fmt="0"),
        Col("Quarter", "quarter", "string"),
        Col("Month Number", "month_number", "int64", **H),
        Col("Month", "month_name", "string", sort_by="Month Number"),
        Col("Year Month", "year_month", "string"),
        Col("Day of Week Number", "day_of_week_number", "int64", **H),
        Col("Day of Week", "day_of_week", "string", sort_by="Day of Week Number"),
        Col("Week Start", "week_start", "dateTime", fmt="yyyy-mm-dd"),
    ], data_category="Time"),
    Table("Site", "silver_facilities",
          "Monitored public sites (venues, hospitals, airport, major employers). Locations are public; "
          "MVOI is not affiliated with any site (is_affiliated = false).", [
        Col("Site ID", "facility_id", "string", is_key=True),
        Col("Site", "facility_name", "string"),
        Col("Site Type", "facility_type", "string"),
        Col("City", "city", "string", category="City"),
        Col("Criticality", "criticality", "int64", desc="Site importance, 1 (low) to 5 (high) — scales the exposure score"),
        Col("Primary Corridor", "primary_corridor", "string"),
        Col("Latitude", "latitude", "double", category="Latitude"),
        Col("Longitude", "longitude", "double", category="Longitude"),
        Col("Address", "address", "string"),
        Col("Is Affiliated", "is_affiliated", "boolean"),
    ]),
    Table("Corridor", "gold_corridors", "Highway corridors (configured in geography.yaml).", [
        Col("Corridor", "corridor", "string", is_key=True),
        Col("Corridor Name", "corridor_name", "string"),
    ]),
    Table("Incidents", "gold_incident_occurrences",
          "One row per DISTINCT incident occurrence (continuous active period of one OHGO event). "
          "Repeated polling never adds rows, so counts and hot spots are not inflated.", [
        Col("Occurrence ID", "occurrence_id", "string", is_key=True, **H),
        Col("Event Type", "entity_type", "string"),
        Col("Category", "category", "string"),
        Col("Peak Severity Rank", "peak_severity_rank", "int64", **H),
        Col("Peak Severity", "peak_severity", "string", sort_by="Peak Severity Rank"),
        Col("Road Closure", "had_road_closure", "boolean"),
        Col("Is Open", "is_open", "boolean"),
        Col("Start Date", "start_date", "dateTime", **H),
        Col("Start Hour", "start_hour", "int64", desc="Hour of day (UTC) the occurrence began"),
        Col("Started At", "started_at", "dateTime", fmt="yyyy-mm-dd hh:nn"),
        Col("Duration Minutes", "duration_minutes", "double", **H),
        Col("Route", "route_name", "string"),
        Col("Corridor Key", "corridor", "string", **H),
        Col("Latitude", "latitude", "double", category="Latitude"),
        Col("Longitude", "longitude", "double", category="Longitude"),
    ], [
        Measure("Incident Occurrences", "COUNTROWS(Incidents)", "#,##0", "Incidents",
                "Distinct incident occurrences (not polls, not lifecycle updates)."),
        Measure("Road Closure Occurrences",
                "CALCULATE(COUNTROWS(Incidents), Incidents[Road Closure] = TRUE())", "#,##0", "Incidents",
                "Occurrences that fully closed the road at any point."),
        Measure("Incident Hours", "DIVIDE(SUM(Incidents[Duration Minutes]), 60)", "#,##0.0", "Incidents",
                "Total active duration of occurrences, in hours (open occurrences counted to the last run)."),
        Measure("Average Duration Minutes", "AVERAGE(Incidents[Duration Minutes])", "#,##0.0", "Incidents",
                "Mean active duration of an occurrence."),
        Measure("Weather-Related Occurrences",
                "COUNTROWS(FILTER(Incidents, NOT ISEMPTY(RELATEDTABLE('Incident Weather'))))", "#,##0",
                "Weather", "Occurrences inside an active NWS alert area during the alert window "
                           "(spatial-temporal coincidence, not proven causation)."),
        Measure("Weather-Related Share", "DIVIDE([Weather-Related Occurrences], [Incident Occurrences])",
                "0.0%", "Weather", "Share of occurrences that coincided with an NWS alert."),
        Measure("Occurrences per Feed Day", "DIVIDE([Incident Occurrences] + 0, [Feed Days])", "#,##0.0",
                "Incidents", "Occurrences per day the feed was running (days without polls are not quiet days)."),
        Measure("Usual Daily Occurrences",
                "CALCULATE([Occurrences per Feed Day], 'Date'[Date] >= TODAY() - 28 && 'Date'[Date] < TODAY())",
                "#,##0.0", "Incidents",
                "Baseline for 'than usual': occurrences per feed day over the 28 days before today."),
    ]),
    Table("Corridor Daily", "gold_corridor_disruption_daily", "Disruption metrics per corridor per day.", [
        Col("Date Key", "start_date", "dateTime", **H),
        Col("Corridor Key", "corridor", "string", **H),
        Col("Occurrences", "occurrences", "int64", **H),
        Col("Closures", "closures", "int64", **H),
        Col("Closure Minutes", "closure_minutes", "double", **H),
        Col("Median Duration Minutes", "median_duration_minutes", "double", **H),
    ], [
        Measure("Closure Hours", "DIVIDE(SUM('Corridor Daily'[Closure Minutes]), 60)", "#,##0.0",
                "Corridors", "Hours of full road closure attributed to the corridor."),
        Measure("Median Incident Duration Minutes", "MEDIAN('Corridor Daily'[Median Duration Minutes])",
                "#,##0.0", "Corridors", "Median of daily median durations (robust to outliers)."),
    ]),
    Table("Site Disruption", "gold_facility_risk_history",
          "Site travel-disruption exposure snapshots (emitted on material change). INFERRED from public traffic "
          "and weather data near the site; not a statement about the site's operations.", [
        Col("Site Key", "facility_id", "string", **H),
        Col("Date Key", "risk_date", "dateTime", **H),
        Col("Scored At", "observed_at", "dateTime", fmt="yyyy-mm-dd hh:nn"),
        Col("Score", "score", "double", **H),
        Col("Band", "band", "string"),
        Col("Incident Points", "incident_points", "double", **H),
        Col("Road Closure Points", "road_closure_points", "double", **H),
        Col("Slowdown Points", "slowdown_points", "double", **H),
        Col("Weather Points", "weather_points", "double", **H),
        Col("History Points", "history_points", "double", **H),
        Col("Explanation", "explanation", "string"),
    ], [
        Measure("Peak Exposure Score", "MAX('Site Disruption'[Score])", "0.0", "Exposure",
                "Highest disruption exposure score in context (0–100)."),
        Measure("Average Exposure Score", "AVERAGE('Site Disruption'[Score])", "0.0", "Exposure",
                "Mean of exposure snapshots in context (snapshots are change-driven, so this is indicative)."),
        Measure("High Exposure Snapshots", "CALCULATE(COUNTROWS('Site Disruption'), 'Site Disruption'[Score] >= 70)",
                "#,##0", "Exposure", "Snapshots at or above the alert threshold (70)."),
        Measure("Weather Share of Exposure",
                "DIVIDE(SUM('Site Disruption'[Weather Points]), SUM('Site Disruption'[Incident Points]) + "
                "SUM('Site Disruption'[Road Closure Points]) + SUM('Site Disruption'[Slowdown Points]) + "
                "SUM('Site Disruption'[Weather Points]) + SUM('Site Disruption'[History Points]))",
                "0.0%", "Exposure", "Share of raw exposure points contributed by weather alerts."),
    ]),
    Table("Site Proximity", "gold_facility_proximity",
          "Incident occurrences within the scoring distance of each site (geodesic).", [
        Col("Occurrence Key", "occurrence_id", "string", **H),
        Col("Site Key", "facility_id", "string", **H),
        Col("Date Key", "start_date", "dateTime", **H),
        Col("Distance Meters", "distance_m", "double", **H),
        Col("Within Full Weight", "within_full_weight", "boolean",
            desc="Inside the full-weight radius (default 1 mile)"),
        Col("Peak Severity", "peak_severity", "string"),
        Col("Road Closure", "had_road_closure", "boolean"),
        Col("Spatial Engine", "spatial_engine", "string"),
    ], [
        Measure("Nearby Incident Occurrences", "DISTINCTCOUNT('Site Proximity'[Occurrence Key])",
                "#,##0", "Proximity", "Distinct occurrences near the site in context."),
        Measure("Average Distance Miles",
                "DIVIDE(AVERAGE('Site Proximity'[Distance Meters]), 1609.344)", "#,##0.00", "Proximity",
                "Mean distance from site to nearby occurrences, in miles."),
    ]),
    Table("Weather Exposure", "gold_weather_exposure", "NWS alerts whose area covered a site.", [
        Col("Alert Key", "alert_key", "string", **H),
        Col("Site Key", "facility_id", "string", **H),
        Col("Date Key", "onset_date", "dateTime", **H),
        Col("Weather Event", "event", "string"),
        Col("CAP Severity", "cap_severity", "string",
            desc="Common Alerting Protocol severity from NWS (Extreme/Severe/Moderate/Minor)"),
        Col("Onset", "onset", "dateTime", fmt="yyyy-mm-dd hh:nn"),
        Col("Expires", "expires", "dateTime", fmt="yyyy-mm-dd hh:nn"),
    ], [
        Measure("Weather Alerts at Sites", "DISTINCTCOUNT('Weather Exposure'[Alert Key])", "#,##0",
                "Weather", "Distinct NWS alerts covering at least one site in context."),
        Measure("Sites Under Alert", "DISTINCTCOUNT('Weather Exposure'[Site Key])", "#,##0",
                "Weather", "Distinct sites covered by an NWS alert in context."),
    ]),
    Table("Incident Weather", "gold_incident_weather",
          "Bridge: occurrences located inside an NWS alert area during the alert window.", [
        Col("Occurrence Key", "occurrence_id", "string", **H),
        Col("Alert Key", "alert_key", "string", **H),
        Col("Weather Event", "event", "string"),
        Col("CAP Severity", "cap_severity", "string"),
    ]),
    Table("Hot Spots", "gold_hotspots",
          "Getis-Ord Gi* grid bins over distinct incident occurrences (ArcGIS FindHotSpots or fallback). "
          "Map table: plot Bin Latitude/Longitude coloured by Confidence.", [
        Col("Bin ID", "bin_id", "string", **H),
        Col("Bin Latitude", "center_lat", "double", category="Latitude"),
        Col("Bin Longitude", "center_lon", "double", category="Longitude"),
        Col("Bin WKT", "bin_wkt", "string", **H),
        Col("Occurrences in Bin", "value", "double", **H),
        Col("Z Score", "GiZScore", "double", fmt="0.00"),
        Col("P Value", "GiPValue", "double", fmt="0.0000"),
        Col("Confidence", "Gi_Bin", "int64",
            desc="+3/+2/+1 = hot spot at 99/95/90% confidence; negative = cold spot; 0 = not significant"),
        Col("Weighting", "weighting", "string"),
        Col("Spatial Engine", "spatial_engine", "string"),
    ], [
        Measure("Hot Spot Bins 95", "CALCULATE(COUNTROWS('Hot Spots'), 'Hot Spots'[Confidence] >= 2)",
                "#,##0", "Hot spots", "Bins that are hot spots at 95% confidence or higher."),
        Measure("Occurrences in Hot Spots",
                "CALCULATE(SUM('Hot Spots'[Occurrences in Bin]), 'Hot Spots'[Confidence] >= 2)", "#,##0",
                "Hot spots", "Distinct occurrences that fall inside 95%+ hot-spot bins."),
    ]),
    Table("Pipeline Health", "gold_pipeline_health_daily", "Poller health per source per day (from heartbeats).", [
        Col("Date Key", "date", "dateTime", **H),
        Col("Source", "source", "string"),
        Col("Polls", "polls", "int64", **H),
        Col("Failed Polls", "failed_polls", "int64", **H),
        Col("Observations", "observations", "int64", **H),
        Col("Rejected Records", "rejected", "int64", **H),
        Col("P95 Source Latency ms", "p95_source_latency_ms", "double", **H),
        Col("P95 End to End Seconds", "p95_end_to_end_s", "double", **H),
    ], [
        Measure("Poll Success Rate",
                "1 - DIVIDE(SUM('Pipeline Health'[Failed Polls]), SUM('Pipeline Health'[Polls]))", "0.00%",
                "Pipeline", "Share of source polls that succeeded."),
        Measure("Total Polls", "SUM('Pipeline Health'[Polls])", "#,##0", "Pipeline", "Source polls executed."),
        Measure("Feed Days", "COUNTROWS(FILTER(VALUES('Date'[Date]), [Total Polls] > 0))", "#,##0", "Pipeline",
                "Days in context on which the poller ran."),
        Measure("Rejected Record Rate",
                "DIVIDE(SUM('Pipeline Health'[Rejected Records]), SUM('Pipeline Health'[Observations]) + "
                "SUM('Pipeline Health'[Rejected Records]))", "0.00%", "Pipeline",
                "Source records rejected by schema/coordinate validation in the poller."),
        Measure("P95 End to End Latency Seconds", "MAX('Pipeline Health'[P95 End to End Seconds])", "#,##0.0",
                "Pipeline", "Worst daily p95 from poll to Eventhouse ingestion in context."),
    ]),
    Table("Data Quality", "silver_dq_results", "Silver data-quality rule results per run.", [
        Col("Run ID", "run_id", "string", **H),
        Col("Date Key", "run_date", "dateTime", **H),
        Col("Rule", "rule", "string"),
        Col("Rule Severity", "severity", "string"),
        Col("Rule Description", "description", "string"),
        Col("Rows Checked", "rows_checked", "int64", **H),
        Col("Rows Failed", "rows_failed", "int64", **H),
    ], [
        Measure("DQ Pass Rate",
                "1 - DIVIDE(SUM('Data Quality'[Rows Failed]), SUM('Data Quality'[Rows Checked]))", "0.00%",
                "Data quality", "Share of rule checks that passed."),
        Measure("DQ Failed Rows", "SUM('Data Quality'[Rows Failed])", "#,##0", "Data quality",
                "Rows failing a rule (error rules are quarantined; warn rules are flagged)."),
    ]),
]

# (from table, from column) -> (to table, to column)   many -> one, single direction
RELATIONSHIPS = [
    ("Incidents", "Start Date", "Date", "Date"),
    ("Incidents", "Corridor Key", "Corridor", "Corridor"),
    ("Corridor Daily", "Date Key", "Date", "Date"),
    ("Corridor Daily", "Corridor Key", "Corridor", "Corridor"),
    ("Site Disruption", "Date Key", "Date", "Date"),
    ("Site Disruption", "Site Key", "Site", "Site ID"),
    ("Site Proximity", "Date Key", "Date", "Date"),
    ("Site Proximity", "Site Key", "Site", "Site ID"),
    ("Weather Exposure", "Date Key", "Date", "Date"),
    ("Weather Exposure", "Site Key", "Site", "Site ID"),
    ("Incident Weather", "Occurrence Key", "Incidents", "Occurrence ID"),
    ("Pipeline Health", "Date Key", "Date", "Date"),
    ("Data Quality", "Date Key", "Date", "Date"),
]


def _q(name: str) -> str:
    return f"'{name}'" if any(ch in name for ch in " .=:'-") else name


def render_table(t: Table) -> str:
    lines = [f"/// {t.desc}", f"table {_q(t.name)}"]
    if t.data_category:
        lines.append(f"\tdataCategory: {t.data_category}")
    lines.append("")
    for m in t.measures:
        lines.append(f"\t/// {m.desc}")
        lines.append(f"\tmeasure {_q(m.name)} = {m.dax}")
        lines.append(f"\t\tformatString: {m.fmt}")
        lines.append(f"\t\tdisplayFolder: {m.folder}")
        lines.append("")
    for c in t.cols:
        if c.desc:
            lines.append(f"\t/// {c.desc}")
        lines.append(f"\tcolumn {_q(c.name)}")
        lines.append(f"\t\tdataType: {c.dtype}")
        if c.is_key:
            lines.append("\t\tisKey")
        if c.hidden:
            lines.append("\t\tisHidden")
        if c.fmt:
            lines.append(f"\t\tformatString: {c.fmt}")
        if c.summarize_none:
            lines.append("\t\tsummarizeBy: none")
        if c.category:
            lines.append(f"\t\tdataCategory: {c.category}")
        if c.sort_by:
            lines.append(f"\t\tsortByColumn: {_q(c.sort_by)}")
        lines.append(f"\t\tsourceColumn: {c.source}")
        lines.append("")
    lines += [f"\tpartition {_q(t.name)} = entity", "\t\tmode: directLake", "\t\tsource",
              f"\t\t\tentityName: {t.entity}", f"\t\t\texpressionSource: {_q(EXPR)}", ""]
    return "\n".join(lines)


def render() -> dict[str, str]:
    files = {
        "definition.pbism": json.dumps({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/"
                       "definitionProperties/1.0.0/schema.json",
            "version": "4.2", "settings": {"qnaEnabled": True}}, indent=2) + "\n",
        "definition/database.tmdl": f"database {MODEL_NAME}\n\tcompatibilityLevel: 1604\n"
                                    "\tcompatibilityMode: powerBI\n\tlanguage: 1033\n",
        "definition/model.tmdl": "\n".join([
            "model Model", "\tculture: en-US", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
            "\tsourceQueryCulture: en-US", "\tdiscourageImplicitMeasures", "",
            *[f"ref table {_q(t.name)}" for t in TABLES], ""]),
        "definition/expressions.tmdl": "\n".join([
            "/// OneLake source for all Direct Lake tables (ids injected at deploy time)",
            f"expression {_q(EXPR)} =", "\t\tlet",
            '\t\t    Source = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/'
            '{{WORKSPACE_ID}}/{{LAKEHOUSE_ID}}", [HierarchicalNavigation=true])',
            "\t\tin", "\t\t    Source", ""]),
        "definition/relationships.tmdl": "\n".join(
            "\n".join([f"relationship '{ft} to {tt} ({fc})'", f"\tfromColumn: {_q(ft)}.{_q(fc)}", f"\ttoColumn: {_q(tt)}.{_q(tc)}", ""])
            for ft, fc, tt, tc in RELATIONSHIPS),
    }
    for t in TABLES:
        files[f"definition/tables/{t.name}.tmdl"] = render_table(t)
    return files


def write(folder: Path) -> None:
    for rel, text in render().items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def definition_parts(folder: Path, *, workspace_id: str, lakehouse_id: str) -> list[dict[str, str]]:
    from mvoi.deploy.fabric_api import part

    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            text = p.read_text(encoding="utf-8").replace("{{WORKSPACE_ID}}", workspace_id) \
                .replace("{{LAKEHOUSE_ID}}", lakehouse_id)
            out.append(part(p.relative_to(folder).as_posix(), text))
    return out


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[3] / "fabric" / "semantic-model" / f"{MODEL_NAME}.SemanticModel"
    write(target)
    print(target)
