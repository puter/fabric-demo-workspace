import json

import pytest

from mvoi.geo.core import haversine_m, point_in_polygon, point_line_distance_m, polygon_centroid
from mvoi.geo.reference import normalize_route

SQUARE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[-84.4, 39.6], [-84.0, 39.6], [-84.0, 40.0], [-84.4, 40.0], [-84.4, 39.6]],
        [[-84.25, 39.75], [-84.15, 39.75], [-84.15, 39.85], [-84.25, 39.85], [-84.25, 39.75]],
    ],
}


def test_haversine_dayton_to_springfield():
    # Dayton (39.7589,-84.1916) to Springfield (39.9242,-83.8088) ≈ 37 km
    d = haversine_m(39.7589, -84.1916, 39.9242, -83.8088)
    assert 36_000 < d < 38_500


def test_point_in_polygon_respects_holes():
    assert point_in_polygon(39.65, -84.3, SQUARE_WITH_HOLE)
    assert not point_in_polygon(39.80, -84.20, SQUARE_WITH_HOLE)  # in hole
    assert not point_in_polygon(40.10, -84.20, SQUARE_WITH_HOLE)


def test_multipolygon():
    mp = {"type": "MultiPolygon", "coordinates": [SQUARE_WITH_HOLE["coordinates"]]}
    assert point_in_polygon(39.65, -84.3, mp)


def test_point_line_distance():
    line = {"type": "LineString", "coordinates": [[-84.2, 39.7], [-84.2, 39.9]]}
    on = point_line_distance_m(39.8, -84.2, line)
    off = point_line_distance_m(39.8, -84.19, line)  # ~0.01 deg lon east ≈ 855 m
    assert on < 1
    assert 800 < off < 900


def test_centroid_of_square():
    lat, lon = polygon_centroid({"type": "Polygon", "coordinates": [SQUARE_WITH_HOLE["coordinates"][0]]})
    assert lat == pytest.approx(39.8, abs=1e-6) and lon == pytest.approx(-84.2, abs=1e-6)


@pytest.mark.parametrize("route,expected", [
    ("I-75", "I-75"), ("IR-75 NB", "I-75"), ("I-675", "I-675"), ("I 70", "I-70"),
    ("US-35", "US-35"), ("SR-4", "SR-4"), ("SR-444", "SR-444"), ("SR-41", None), ("I-7", None),
])
def test_corridor_alias_matching(corridors, route, expected):
    assert corridors.by_route_name(route) == expected


def test_corridor_spatial_fallback(corridors):
    # Point ~50 m off I-75 (TIGER alignment) with an unhelpful route name.
    cid, method = corridors.match("Ramp", 39.80629, -84.1882)
    assert (cid, method) == ("I-75", "spatial")
    cid, method = corridors.match(None, 39.55, -84.55)  # rural, far from corridors
    assert method == "none"


def test_normalize_route():
    assert normalize_route("IR-75 n/b") == "IR75NB"


def test_reference_corridors_cover_priority_routes():
    from mvoi.geo.reference import REFERENCE_DIR

    fc = json.loads((REFERENCE_DIR / "corridors.geojson").read_text())
    ids = {f["properties"]["corridor_id"] for f in fc["features"]}
    assert {"I-75", "I-70", "US-35", "I-675"} <= ids


def test_sites_are_unaffiliated_and_inside_aoi(facilities, geography):
    assert len(facilities) == 20
    assert facilities[0].facility_name == "Sinclair Conference Center"  # demo host site
    for f in facilities:
        assert not f.is_affiliated and f.address
        assert geography.bounding_box.contains(f.latitude, f.longitude), f.facility_id


def test_zone_index_contains_dayton(zones):
    assert "OHZ061" in zones.zones_containing(39.7589, -84.1916)  # Montgomery
