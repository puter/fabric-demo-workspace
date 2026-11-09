"""KQL deployment helpers: schema script + reference-data commands."""

from __future__ import annotations

import json
import re
from pathlib import Path

from mvoi.geo.reference import load_facilities


def schema_commands(kql_path: Path) -> list[str]:
    """Split kqldb_mvoi.kql into individual control commands (comments removed).

    Commands start with a line beginning with '.'; everything until the next
    such line belongs to the command (function bodies, mapping literals).
    """
    commands: list[str] = []
    cur: list[str] = []
    for line in kql_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("//"):
            continue
        if line.startswith("."):
            if cur:
                commands.append("\n".join(cur).strip())
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        commands.append("\n".join(cur).strip())
    return [c for c in commands if c]


def database_script(commands: list[str]) -> str:
    """One `.execute database script` — atomic-ish, single round trip."""
    return ".execute database script with (ContinueOnErrors = false) <|\n" + "\n\n".join(commands)


def _q(s: str | None) -> str:
    if s is None:
        return "''"
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dyn(obj) -> str:
    # dynamic() literal; JSON is valid KQL dynamic syntax.
    return "dynamic(" + json.dumps(obj, separators=(",", ":")) + ")"


def facilities_command(csv_path: Path | None = None) -> str:
    rows = []
    for f in load_facilities(csv_path):
        rows.append(", ".join([
            _q(f.facility_id), _q(f.facility_name), _q(f.facility_type), repr(f.latitude),
            repr(f.longitude), _q(f.city), str(f.criticality), _q(f.primary_corridor),
            _q(f.address), _q(f.description), "true" if f.is_affiliated else "false"]))
    return (".set-or-replace Facilities <|\n"
            "datatable(facility_id:string, facility_name:string, facility_type:string, latitude:real, "
            "longitude:real, city:string, criticality:int, primary_corridor:string, address:string, "
            "description:string, is_affiliated:bool)[\n  " + ",\n  ".join(rows) + "\n]")


def geojson_command(table: str, geojson_path: Path, id_prop: str, extra: list[str]) -> str:
    fc = json.loads(geojson_path.read_text(encoding="utf-8"))
    schema = ", ".join([f"{id_prop}:string", *[f"{e}:string" for e in extra], "geometry:dynamic"])
    rows = []
    for f in fc["features"]:
        p = f["properties"]
        rows.append(", ".join([_q(p[id_prop]), *[_q(p.get(e)) for e in extra], _dyn(f["geometry"])]))
    return f".set-or-replace {table} <|\ndatatable({schema})[\n  " + ",\n  ".join(rows) + "\n]"


def reference_commands(reference_dir: Path) -> list[str]:
    return [
        facilities_command(reference_dir / "facilities.csv"),
        geojson_command("Zones", reference_dir / "nws_zones.geojson", "zone_id", ["zone_type", "name"]),
        geojson_command("Corridors", reference_dir / "corridors.geojson", "corridor_id", ["corridor_name"]),
    ]


def function_names(kql_path: Path) -> set[str]:
    return set(re.findall(r"^(\w+)\(", kql_path.read_text(encoding="utf-8"), flags=re.M))
