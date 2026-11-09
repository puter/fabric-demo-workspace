"""Getis-Ord Gi* hot-spot analysis on a square grid (fallback engine).

Mirrors the inputs and output columns of ArcGIS GeoAnalytics `FindHotSpots`
(value, GiZScore, GiPValue, Gi_Bin) so the Gold table and the Power BI
report are identical whichever engine ran. ArcGIS is preferred when
licensed; this exists so the demo never depends on a third-party licence.

* Grid: square bins of `bin_size_m` over the AOI in a local equirectangular
  projection (adequate for a ~100 km region).
* Weights: binary, all bins whose centres lie within `neighborhood_m`
  (including the bin itself — the "star" in Gi*).
* Values: distinct incident occurrences per bin by default, or any weight
  (e.g. incident-minutes) — never raw poll counts.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from mvoi.common.config import BoundingBox
from mvoi.geo.core import EARTH_RADIUS_M


@dataclass
class HotspotBin:
    bin_id: str
    row: int
    col: int
    center_lat: float
    center_lon: float
    wkt: str
    value: float
    GiZScore: float
    GiPValue: float
    Gi_Bin: int


def _norm_sf(z: float) -> float:
    """Two-sided p-value for a standard normal z."""
    return math.erfc(abs(z) / math.sqrt(2))


def _confidence_bin(z: float, p: float) -> int:
    level = 3 if p < 0.01 else 2 if p < 0.05 else 1 if p < 0.10 else 0
    return level if z > 0 else -level


def find_hot_spots(points: Iterable[tuple[float, float, float]], bbox: BoundingBox,
                   bin_size_m: float = 1600, neighborhood_m: float = 4800) -> list[HotspotBin]:
    """`points`: (lat, lon, weight). Returns one row per grid bin (zeros included)."""
    lat0 = (bbox.north + bbox.south) / 2
    m_per_deg_lat = math.pi * EARTH_RADIUS_M / 180
    m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(lat0))
    dlat = bin_size_m / m_per_deg_lat
    dlon = bin_size_m / m_per_deg_lon
    n_rows = max(1, math.ceil((bbox.north - bbox.south) / dlat))
    n_cols = max(1, math.ceil((bbox.east - bbox.west) / dlon))

    values: dict[tuple[int, int], float] = defaultdict(float)
    for lat, lon, w in points:
        if not bbox.contains(lat, lon):
            continue
        r = min(n_rows - 1, int((lat - bbox.south) / dlat))
        c = min(n_cols - 1, int((lon - bbox.west) / dlon))
        values[(r, c)] += w

    n = n_rows * n_cols
    total = sum(values.values())
    mean = total / n
    var = sum(v * v for v in values.values()) / n - mean * mean
    s = math.sqrt(max(var, 0.0))
    k = int(neighborhood_m // bin_size_m)
    rad2 = (neighborhood_m / bin_size_m) ** 2

    offsets = [(dr, dc) for dr in range(-k, k + 1) for dc in range(-k, k + 1) if dr * dr + dc * dc <= rad2]
    out: list[HotspotBin] = []
    for r in range(n_rows):
        for c in range(n_cols):
            wsum = 0
            xsum = 0.0
            for dr, dc in offsets:
                rr, cc = r + dr, c + dc
                if 0 <= rr < n_rows and 0 <= cc < n_cols:
                    wsum += 1
                    xsum += values.get((rr, cc), 0.0)
            if s == 0 or n <= 1:
                z = 0.0
            else:
                denom = s * math.sqrt((n * wsum - wsum * wsum) / (n - 1))
                z = 0.0 if denom == 0 else (xsum - mean * wsum) / denom
            p = _norm_sf(z)
            south = bbox.south + r * dlat
            west = bbox.west + c * dlon
            north, east = south + dlat, west + dlon
            wkt = (f"POLYGON(({west:.6f} {south:.6f},{east:.6f} {south:.6f},{east:.6f} {north:.6f},"
                   f"{west:.6f} {north:.6f},{west:.6f} {south:.6f}))")
            out.append(HotspotBin(f"r{r}c{c}", r, c, south + dlat / 2, west + dlon / 2, wkt,
                                  values.get((r, c), 0.0), round(z, 4), round(p, 6),
                                  _confidence_bin(z, p)))
    return out
