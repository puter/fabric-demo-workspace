"""Loaders for committed reference data (facilities, corridors, NWS zones)."""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from mvoi.common.config import Geography
from mvoi.geo.core import geometry_bbox, point_in_polygon, point_line_distance_m

REFERENCE_DIR = Path(os.environ.get("MVOI_REFERENCE_DIR")
                     or Path(__file__).resolve().parents[3] / "reference-data")


@dataclass(frozen=True)
class Facility:
    facility_id: str
    facility_name: str
    facility_type: str
    latitude: float
    longitude: float
    city: str
    criticality: int
    primary_corridor: str
    address: str
    description: str
    is_affiliated: bool


def load_facilities(path: Path | None = None) -> list[Facility]:
    path = path or REFERENCE_DIR / "facilities.csv"
    out = []
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            f = Facility(
                facility_id=row["facility_id"],
                facility_name=row["facility_name"],
                facility_type=row["facility_type"],
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                city=row["city"],
                criticality=int(row["criticality"]),
                primary_corridor=row["primary_corridor"],
                address=row["address"],
                description=row["description"],
                is_affiliated=row["is_affiliated"].strip().lower() == "true",
            )
            if not 1 <= f.criticality <= 5:
                raise ValueError(f"{f.facility_id}: criticality must be 1..5")
            out.append(f)
    if len({f.facility_id for f in out}) != len(out):
        raise ValueError("duplicate facility_id in facilities.csv")
    return out


@lru_cache(maxsize=4)
def _load_geojson(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class ZoneIndex:
    """NWS zone polygons, for alerts that list affected zones but no polygon."""

    def __init__(self, path: Path | None = None):
        fc = _load_geojson(str(path or REFERENCE_DIR / "nws_zones.geojson"))
        self.zones = {f["properties"]["zone_id"]: f for f in fc["features"]}

    def geometry(self, zone_id: str) -> dict[str, Any] | None:
        f = self.zones.get(zone_id)
        return f["geometry"] if f else None

    def zones_containing(self, lat: float, lon: float) -> list[str]:
        return [zid for zid, f in self.zones.items() if point_in_polygon(lat, lon, f["geometry"])]


_ROUTE_CLEAN = re.compile(r"[^A-Z0-9]+")


def normalize_route(name: str | None) -> str:
    """'IR-75 NB' -> 'IR75NB'; used for alias matching only."""
    return _ROUTE_CLEAN.sub("", (name or "").upper())


class CorridorMatcher:
    """Assigns events to configured corridors.

    1. Attribute match: OHGO `routeName` against configured aliases (exact,
       cheapest, and what ODOT itself says).
    2. Spatial fallback: nearest corridor polyline within
       `corridor_snap_distance_m` of the point.
    """

    def __init__(self, geography: Geography, path: Path | None = None):
        self.snap_m = geography.corridor_snap_distance_m
        self._aliases: list[tuple[str, str]] = []
        for c in geography.corridors:
            for alias in [c.id, *c.aliases]:
                self._aliases.append((normalize_route(alias), c.id))
        # Longest alias first so "I675" is tried before "I67"-style prefixes.
        self._aliases.sort(key=lambda a: -len(a[0]))
        fc = _load_geojson(str(path or REFERENCE_DIR / "corridors.geojson"))
        self._geoms = [(f["properties"]["corridor_id"], f["geometry"], geometry_bbox(f["geometry"]))
                       for f in fc["features"]]

    def by_route_name(self, route_name: str | None) -> str | None:
        norm = normalize_route(route_name)
        if not norm:
            return None
        for alias, cid in self._aliases:
            # Prefix match tolerates direction suffixes ("IR75NB"); the next char
            # must not be a digit so "I-7" never matches "I-75".
            if norm.startswith(alias) and not norm[len(alias):len(alias) + 1].isdigit():
                return cid
        return None

    def nearest(self, lat: float, lon: float) -> tuple[str | None, float | None]:
        best_id, best_d = None, None
        pad = self.snap_m / 80_000  # degrees, generous
        for cid, geom, (x0, y0, x1, y1) in self._geoms:
            if not (x0 - pad <= lon <= x1 + pad and y0 - pad <= lat <= y1 + pad):
                continue
            d = point_line_distance_m(lat, lon, geom)
            if best_d is None or d < best_d:
                best_id, best_d = cid, d
        return best_id, best_d

    def match(self, route_name: str | None, lat: float | None, lon: float | None
              ) -> tuple[str | None, str]:
        """Returns (corridor_id, method) where method is attribute|spatial|none."""
        cid = self.by_route_name(route_name)
        if cid:
            return cid, "attribute"
        if lat is not None and lon is not None:
            cid, d = self.nearest(lat, lon)
            if cid is not None and d is not None and d <= self.snap_m:
                return cid, "spatial"
        return None, "none"
