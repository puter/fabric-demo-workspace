"""Dependency-free geodesy helpers (WGS84 lon/lat).

These run in the polling service, in unit tests, and as the *fallback* spatial
engine inside Fabric Spark when ArcGIS GeoAnalytics is not licensed. Accuracy
is ample for a ~100 km region: haversine distance (sphere, <0.5% error) and
local equirectangular projection for point-to-segment distances.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from typing import Any

EARTH_RADIUS_M = 6_371_008.8

Coord = Sequence[float]  # (lon, lat) — GeoJSON order


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _project(lon: float, lat: float, lat0: float) -> tuple[float, float]:
    """Local equirectangular projection to metres around latitude lat0."""
    x = math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat) * EARTH_RADIUS_M
    return x, y


def point_segment_distance_m(lat: float, lon: float, a: Coord, b: Coord) -> float:
    px, py = _project(lon, lat, lat)
    ax, ay = _project(a[0], a[1], lat)
    bx, by = _project(b[0], b[1], lat)
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def point_line_distance_m(lat: float, lon: float, geometry: dict[str, Any]) -> float:
    """Distance from a point to a (Multi)LineString GeoJSON geometry."""
    best = math.inf
    for line in _lines(geometry):
        for a, b in zip(line, line[1:], strict=False):
            best = min(best, point_segment_distance_m(lat, lon, a, b))
    return best


def point_in_polygon(lat: float, lon: float, geometry: dict[str, Any]) -> bool:
    """Ray-casting test against a (Multi)Polygon GeoJSON geometry, holes respected."""
    for polygon in _polygons(geometry):
        if not polygon:
            continue
        if _in_ring(lon, lat, polygon[0]) and not any(_in_ring(lon, lat, h) for h in polygon[1:]):
            return True
    return False


def _in_ring(x: float, y: float, ring: Sequence[Coord]) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def geometry_bbox(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat)."""
    xs: list[float] = []
    ys: list[float] = []
    for x, y in _all_coords(geometry["coordinates"]):
        xs.append(x)
        ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def polygon_centroid(geometry: dict[str, Any]) -> tuple[float, float]:
    """Area-weighted centroid (lat, lon) of the outer rings — for map labels only."""
    total_a = cx = cy = 0.0
    for polygon in _polygons(geometry):
        ring = polygon[0]
        for (x0, y0), (x1, y1) in zip(ring, ring[1:], strict=False):
            cross = x0 * y1 - x1 * y0
            total_a += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
    if total_a == 0:
        min_x, min_y, max_x, max_y = geometry_bbox(geometry)
        return (min_y + max_y) / 2, (min_x + max_x) / 2
    return cy / (3 * total_a), cx / (3 * total_a)


def parse_geometry(value: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    return json.loads(value) if isinstance(value, str) else value


def _polygons(geometry: dict[str, Any]) -> Iterable[Sequence[Sequence[Coord]]]:
    t = geometry.get("type")
    if t == "Polygon":
        yield geometry["coordinates"]
    elif t == "MultiPolygon":
        yield from geometry["coordinates"]
    elif t == "GeometryCollection":
        for g in geometry.get("geometries", []):
            yield from _polygons(g)


def _lines(geometry: dict[str, Any]) -> Iterable[Sequence[Coord]]:
    t = geometry.get("type")
    if t == "LineString":
        yield geometry["coordinates"]
    elif t == "MultiLineString":
        yield from geometry["coordinates"]
    elif t == "GeometryCollection":
        for g in geometry.get("geometries", []):
            yield from _lines(g)


def _all_coords(coords: Any) -> Iterable[tuple[float, float]]:
    if coords and isinstance(coords[0], (int, float)):
        yield float(coords[0]), float(coords[1])
    else:
        for c in coords:
            yield from _all_coords(c)
