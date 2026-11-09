"""Validates the generated Real-Time Dashboard against the official schema v89."""

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from mvoi.deploy.rtd import HEADER_H, HEADER_PAGES, MAP_QUERY, PAGES, SITE_MAP_QUERY, TILES, build_dashboard

SCHEMA_DIR = Path(__file__).parents[1] / "fixtures" / "rtd_schema_89"
KQL = Path(__file__).parents[2] / "fabric" / "eventhouse" / "kqldb_mvoi.kql"


@pytest.fixture(scope="module")
def validator():
    resources = []
    for f in SCHEMA_DIR.glob("*.json"):
        doc = json.loads(f.read_text())
        res = Resource.from_contents(doc)
        resources.append((doc["$id"], res))
        resources.append((f.name, res))
    registry = Registry().with_resources(resources)
    root = json.loads((SCHEMA_DIR / "dashboard.json").read_text())
    return Draft202012Validator(root, registry=registry)


def _dash():
    return build_dashboard(title="rtd_mvoi_operations",
                           cluster_uri="https://trd-example.z0.kusto.fabric.microsoft.com",
                           database_id="11111111-1111-1111-1111-111111111111",
                           workspace_id="22222222-2222-2222-2222-222222222222")


def test_dashboard_validates_against_schema(validator):
    errors = sorted(validator.iter_errors(_dash()), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message[:200]}" for e in errors[:10])


def test_ids_are_deterministic_and_unique():
    a, b = _dash(), _dash()
    assert a == b
    ids = [t["id"] for t in a["tiles"]] + [q["id"] for q in a["queries"]]
    assert len(ids) == len(set(ids))


def test_every_tile_query_uses_a_defined_kql_function():
    defined = set(re.findall(r"^(\w+)\(", KQL.read_text(), flags=re.M))
    for t in TILES:
        called = set(re.findall(r"\b([A-Z]\w+)\(", t.query))
        assert called <= defined, (t.key, called - defined)
        assert "_mode" in t.query, f"tile {t.key} ignores the live/replay switch"


def test_tiles_do_not_overlap():
    by_page: dict[str, list] = {}
    for t in TILES:
        for (x, y, w, h) in by_page.get(t.page, []):
            assert t.x >= x + w or x >= t.x + t.w or t.y >= y + h or y >= t.y + t.h, t.key
        by_page.setdefault(t.page, []).append((t.x, t.y, t.w, t.h))
        assert t.x + t.w <= 24


def test_header_pages_open_with_the_branded_header():
    tiles = _dash()["tiles"]
    headers = [t for t in tiles if t["visualType"] == "markdownCard"]
    assert len(headers) == len(HEADER_PAGES)
    for h in headers:
        assert h["layout"] == {"x": 0, "y": 0, "width": 24, "height": HEADER_H}
        # Only inline images render in Markdown tiles; external URLs are blocked.
        assert "](data:image/png;base64," in h["markdownText"]
        assert "<" not in h["markdownText"], "HTML is stripped by the Markdown tile"
    header_page_ids = {h["pageId"] for h in headers}
    assert all(t["layout"]["y"] >= HEADER_H for t in tiles
               if t["pageId"] in header_page_ids and t["visualType"] != "markdownCard")


def test_presenter_page_is_first_and_its_map_follows_the_site():
    dash = _dash()
    assert list(PAGES)[0] == "presenter" and dash["pages"][0]["name"] == "Presenter"
    maps = {t.page: t for t in TILES if t.visual == "map"}
    assert set(maps) == {"ops", "presenter"}
    assert maps["ops"].query == MAP_QUERY
    assert maps["presenter"].query == SITE_MAP_QUERY
    assert "IncidentsNearSite(_mode, _site, 4828.032)" in SITE_MAP_QUERY


def test_map_plots_incidents_by_severity_with_a_legend():
    # The RTD map ignores seriesColors: sorted series plus the legend carry severity.
    assert "ActiveIncidents" in MAP_QUERY and "order by severity_rank asc" in MAP_QUERY
    assert "CurrentFacilityRisk" not in MAP_QUERY and "ActiveWeatherAlerts" not in MAP_QUERY
    assert "order by severity_rank asc" in SITE_MAP_QUERY
    for t in (t for t in TILES if t.visual == "map"):
        assert t.options["map__labelColumn"] == "severity" and not t.options["hideLegend"]
        assert t.options["map__sizeDisabled"] and "seriesColors" not in t.options


def test_site_picker_drives_the_presenter_spotlight():
    dash = _dash()
    site = next(p for p in dash["parameters"] if p["variableName"] == "_site")
    presenter_page = dash["pages"][0]["id"]
    assert site["showOnPages"] == {"kind": "selection", "pageIds": [presenter_page]}
    picker = next(q for q in dash["queries"] if q["id"] == site["dataSource"]["queryRef"]["queryId"])
    assert picker["text"].startswith("Facilities")
    users = [q for q in dash["queries"] if "_site" in q["usedVariables"]]
    assert len(users) == 3 and all("_site" in q["text"] for q in users)
    page_of = {t["queryRef"]["queryId"]: t["pageId"] for t in dash["tiles"] if "queryRef" in t}
    assert all(page_of[q["id"]] == presenter_page for q in users)
