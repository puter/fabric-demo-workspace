"""Build the public geospatial reference data committed under reference-data/.

Outputs (all public-domain US government data):
  reference-data/corridors.geojson  - US Census TIGER/Line 2025 primary &
                                      secondary roads for Ohio, filtered to the
                                      configured corridors, clipped to the AOI
                                      and simplified.
  reference-data/nws_zones.geojson  - NWS forecast + county zone polygons for
                                      the zones listed in config/geography.yaml.

Run:  uv run --group tools python scripts/build_reference_data.py
"""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

import shapefile  # pyshp
from shapely.geometry import box, mapping, shape
from shapely.ops import linemerge, unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mvoi.common.config import load_geography  # noqa: E402

TIGER_URL = "https://www2.census.gov/geo/tiger/TIGER2025/PRISECROADS/tl_2025_39_prisecroads.zip"
USER_AGENT = "MiamiValleyOpsIntelligence/0.1 (reference-data build)"

# TIGER FULLNAME -> corridor id. TIGER uses forms like "I- 75", "US Hwy 35",
# "State Rte 4".
_TIGER_PATTERNS = [
    (re.compile(r"^I-\s*(\d+)\b"), "I-{}"),
    (re.compile(r"^US Hwy\s*(\d+)\b"), "US-{}"),
    (re.compile(r"^State Rte\s*(\d+)\b"), "SR-{}"),
]


def tiger_corridor_id(fullname: str | None) -> str | None:
    if not fullname:
        return None
    for pattern, fmt in _TIGER_PATTERNS:
        m = pattern.match(fullname.strip())
        if m:
            return fmt.format(m.group(1))
    return None


def _get(url: str, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def build_corridors(geo) -> dict:
    bb = geo.bounding_box
    # Small margin so corridors run to the edge of the map.
    aoi = box(bb.west - 0.05, bb.south - 0.05, bb.east + 0.05, bb.north + 0.05)
    wanted = {c.id: c for c in geo.corridors}

    print(f"downloading {TIGER_URL}")
    z = zipfile.ZipFile(io.BytesIO(_get(TIGER_URL)))
    stem = "tl_2025_39_prisecroads"
    reader = shapefile.Reader(
        shp=io.BytesIO(z.read(f"{stem}.shp")),
        dbf=io.BytesIO(z.read(f"{stem}.dbf")),
        shx=io.BytesIO(z.read(f"{stem}.shx")),
    )
    parts: dict[str, list] = {cid: [] for cid in wanted}
    for sr in reader.iterShapeRecords():
        cid = tiger_corridor_id(sr.record["FULLNAME"])
        if cid not in wanted:
            continue
        geom = shape(sr.shape.__geo_interface__)
        if not geom.intersects(aoi):
            continue
        parts[cid].append(geom.intersection(aoi))

    features = []
    for cid, geoms in parts.items():
        if not geoms:
            print(f"  WARNING: no TIGER geometry for corridor {cid}")
            continue
        merged = linemerge(unary_union(geoms))
        # ~10 m tolerance in degrees; keeps file small, ample for 300 m snapping.
        simplified = merged.simplify(0.0001, preserve_topology=False)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "corridor_id": cid,
                    "corridor_name": wanted[cid].name,
                    "source": "US Census TIGER/Line 2025 PRISECROADS (Ohio)",
                },
                "geometry": _round(mapping(simplified)),
            }
        )
        print(f"  {cid}: {len(geoms)} segments")
    return {"type": "FeatureCollection", "features": features}


def build_nws_zones(geo) -> dict:
    features = []
    zones = {**geo.nws.forecast_zones, **geo.nws.county_zones}
    for zone_id, name in sorted(zones.items()):
        kind = "forecast" if zone_id[2] == "Z" else "county"
        url = f"https://api.weather.gov/zones/{kind}/{zone_id}"
        data = json.loads(_get(url, accept="application/geo+json"))
        geom = shape(data["geometry"]).simplify(0.0005, preserve_topology=True)
        features.append(
            {
                "type": "Feature",
                "properties": {"zone_id": zone_id, "zone_type": kind, "name": name},
                "geometry": _round(mapping(geom)),
            }
        )
        print(f"  {zone_id} {name}")
    return {"type": "FeatureCollection", "features": features}


def _round(geojson_geom: dict, ndigits: int = 5) -> dict:
    def r(c):
        if isinstance(c, (int, float)):
            return round(c, ndigits)
        return [r(x) for x in c]

    return {"type": geojson_geom["type"], "coordinates": r(geojson_geom["coordinates"])}


def main() -> None:
    geo = load_geography(ROOT / "config" / "geography.yaml")
    out = ROOT / "reference-data"
    out.mkdir(exist_ok=True)
    (out / "corridors.geojson").write_text(json.dumps(build_corridors(geo)))
    print("fetching NWS zones")
    (out / "nws_zones.geojson").write_text(json.dumps(build_nws_zones(geo)))
    print("done")


if __name__ == "__main__":
    main()
