import copy

import pytest

from mvoi.replay.synthetic import build_storyline
from mvoi.sources import severity as sev
from mvoi.sources.ohgo import bbox_params
from tests.conftest import T0, load_fixture

# ----------------------------------------------------------------- OHGO contract


def _swagger_props(schema: str) -> set[str]:
    sw = load_fixture("ohgo_swagger_v1.json")
    return set(sw["components"]["schemas"][schema]["properties"])


@pytest.mark.parametrize("schema,fields", [
    ("Incident", {"id", "latitude", "longitude", "location", "description", "category", "direction",
                  "routeName", "roadStatus", "roadClosureDetails"}),
    ("Construction", {"id", "latitude", "longitude", "location", "description", "category",
                      "direction", "district", "routeName", "status", "startDate", "endDate",
                      "workZoneProjectId"}),
    ("DangerousSlowdown", {"id", "latitude", "longitude", "location", "description", "normalMPH",
                           "currentMPH", "routeName", "direction"}),
    ("IncidentApiResult", {"lastUpdated", "rejectedFilters", "totalResultCount", "results"}),
    ("RoadClosureDetails", {"closureStartLocation", "closureEndLocation", "polyline"}),
])
def test_fields_we_read_exist_in_published_swagger(schema, fields):
    assert fields <= _swagger_props(schema)


def test_synthetic_envelopes_conform_to_swagger():
    """Synthetic replay data must use only documented field names."""
    allowed = {
        "incidents": _swagger_props("Incident"),
        "construction": _swagger_props("Construction"),
        "dangerous-slowdowns": _swagger_props("DangerousSlowdown"),
        # travel-delay model documented on /docs/v1/travel-delays (swagger reuses Incident)
        "travel-delays": {"links", "id", "latitude", "longitude", "location", "description",
                          "direction", "routeName", "travelTime", "delayTime", "startMileMarker",
                          "endMileMarker", "currentAvgSpeed", "normalAvgSpeed"},
    }
    envelope_fields = _swagger_props("IncidentApiResult")
    for _, source, resource, env in build_storyline(T0):
        if source != "ohgo":
            continue
        assert set(env) <= envelope_fields
        for item in env["results"]:
            assert set(item) <= allowed[resource], (resource, set(item) - allowed[resource])


def test_bbox_params(geography):
    p = bbox_params(geography.bounding_box)
    assert p["map-bounds-sw"] == "39.45,-84.6"
    assert p["map-bounds-ne"] == "40.25,-83.65"


# ----------------------------------------------------------------- OHGO normalisation


def _incident(**kw):
    item = {"links": [], "id": "123", "latitude": 39.809, "longitude": -84.204,
            "location": "I-75 NB at Needmore Rd", "description": "Crash", "category": "Crash",
            "direction": "NB", "routeName": "I-75", "roadStatus": "Closed",
            "roadClosureDetails": {"closureStartLocation": [-84.204, 39.809],
                                   "closureEndLocation": [-84.204, 39.815],
                                   "polyline": [[-84.204, 39.809], [-84.204, 39.815]]}}
    item.update(kw)
    return item


def _env(results):
    return {"links": [], "lastUpdated": "2026-01-15T14:00:00Z", "accecptedFilters": [],
            "rejectedFilters": [], "totalPageCount": 1, "totalResultCount": len(results),
            "currentResultCount": len(results), "results": results}


def test_incident_normalisation(ohgo_adapter):
    r = ohgo_adapter.parse_envelope("incidents", _env([_incident()]), T0)
    assert r.ok and len(r.observations) == 1 and not r.rejected
    e = r.observations[0].event
    assert e.entity_key == "ohgo:incident:123"
    assert e.corridor_id == "I-75" and e.attributes["corridor_match_method"] == "attribute"
    assert e.severity == "major" and e.is_road_closure
    assert '"LineString"' in e.geometry_geojson
    assert e.raw["roadStatus"] == "Closed"  # lossless copy retained


def test_missing_id_gets_deterministic_identity(ohgo_adapter):
    a = ohgo_adapter.parse_envelope("incidents", _env([_incident(id=None)]), T0)
    b = ohgo_adapter.parse_envelope("incidents", _env([_incident(id="")]), T0)
    ka, kb = a.observations[0].event.entity_key, b.observations[0].event.entity_key
    assert ka == kb and ka.startswith("ohgo:incident:h-")


def test_invalid_and_out_of_area_records(ohgo_adapter):
    items = [_incident(id="bad", latitude=123.0), _incident(id="nocoord", latitude=None),
             _incident(id="far", latitude=41.5, longitude=-81.7), _incident(id="ok")]
    r = ohgo_adapter.parse_envelope("incidents", _env(items), T0)
    assert [o.event.source_id for o in r.observations] == ["ok"]
    assert len(r.rejected) == 2  # invalid lat + missing coords; out-of-area silently dropped


def test_slowdown_speed_jitter_is_not_an_update(ohgo_adapter):
    item = {"links": [], "id": "s1", "latitude": 39.8, "longitude": -84.2, "location": "x",
            "description": "slow", "normalMPH": 65, "currentMPH": 12, "routeName": "I-75",
            "direction": "NB"}
    a = ohgo_adapter.parse_envelope("dangerous-slowdowns", _env([item]), T0).observations[0].event
    item2 = {**item, "currentMPH": 14}
    b = ohgo_adapter.parse_envelope("dangerous-slowdowns", _env([item2]), T0).observations[0].event
    a.finalize()
    b.finalize()
    assert a.content_hash == b.content_hash


# ----------------------------------------------------------------- NWS


def test_nws_out_of_area_alerts_dropped(nws_adapter):
    fc = load_fixture("nws_alerts_active_sample.json")  # real alerts from FL/MS/AS/MT
    r = nws_adapter.parse_envelope("alerts", fc, T0)
    assert r.ok and r.observations == [] and not r.rejected


def test_nws_polygon_alert_in_area(nws_adapter):
    fc = load_fixture("nws_alerts_active_sample.json")
    f = copy.deepcopy(fc["features"][0])
    f["geometry"] = {"type": "Polygon", "coordinates": [[[-84.3, 39.7], [-84.1, 39.7], [-84.1, 39.9],
                                                          [-84.3, 39.9], [-84.3, 39.7]]]}
    r = nws_adapter.parse_envelope("alerts", {"features": [f]}, T0)
    e = r.observations[0].event
    assert e.entity_type == "weather_alert"
    assert e.attributes["geometry_source"] == "alert_polygon"
    assert e.end_time is not None and e.severity in {"minor", "moderate", "major", "critical"}


def test_nws_zone_only_alert_gets_zone_geometry(nws_adapter):
    fc = load_fixture("nws_alerts_active_sample.json")
    f = copy.deepcopy([x for x in fc["features"] if not x.get("geometry")][0])
    f["properties"]["geocode"]["UGC"] = ["OHZ061"]
    f["properties"]["affectedZones"] = ["https://api.weather.gov/zones/forecast/OHZ061"]
    r = nws_adapter.parse_envelope("alerts", {"features": [f]}, T0)
    e = r.observations[0].event
    assert e.attributes["geometry_source"] == "nws_zones"
    assert e.attributes["aoi_zones"] == ["OHZ061"]
    assert e.geometry_geojson and e.latitude is not None


# ----------------------------------------------------------------- severity rules


@pytest.mark.parametrize("cat,status,desc,expected", [
    ("Crash", "Open", "", "moderate"),
    ("Crash", "Closed", "", "major"),
    ("Crash", "Partially Closed", "", "moderate"),
    ("Debris", "Open", "", "moderate"),
    ("Crash", "Closed", "Fatal crash", "critical"),
    ("Roadwork", "Open", "", "minor"),
])
def test_incident_severity(cat, status, desc, expected):
    assert sev.incident_severity(cat, status, desc) == expected


def test_closure_detection():
    assert sev.road_status_is_closed("Closed")
    assert not sev.road_status_is_closed("Partially Closed")
    assert sev.road_status_is_partial("Partially Closed")


@pytest.mark.parametrize("normal,current,expected", [(65, 10, "major"), (65, 35, "moderate"),
                                                     (65, 50, "minor"), (None, 10, "moderate")])
def test_slowdown_severity(normal, current, expected):
    assert sev.slowdown_severity(normal, current) == expected


def test_nws_severity_map():
    assert sev.nws_severity("Extreme") == "critical"
    assert sev.nws_severity("Unknown") == "minor"
