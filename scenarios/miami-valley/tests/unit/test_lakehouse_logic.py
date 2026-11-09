from collections import defaultdict
from datetime import timedelta

from mvoi.common.config import BoundingBox
from mvoi.geo.hotspot import find_hot_spots
from mvoi.lakehouse.dq import rules
from mvoi.lakehouse.occurrences import build_occurrences
from tests.conftest import T0


def _ev(ct, minutes, sev="moderate", eid=None, closure=False, last_seen=None, **kw):
    t = T0 + timedelta(minutes=minutes)
    return {"event_id": eid or f"{ct}-{minutes}", "entity_key": "ohgo:incident:1",
            "entity_type": "incident", "change_type": ct, "observed_at": t.isoformat(),
            "first_seen_at": T0.isoformat(), "last_seen_at": (last_seen or t).isoformat(),
            "severity": sev, "is_road_closure": closure, "latitude": 39.8, "longitude": -84.2,
            "corridor_id": "I-75", **kw}


def test_single_occurrence_with_peak_severity_and_duration():
    evs = [_ev("created", 0), _ev("updated", 5, "major", closure=True),
           _ev("updated", 20, "moderate"),
           _ev("resolved", 33, last_seen=T0 + timedelta(minutes=30),
               attributes={"resolution_reason": "absent"})]
    [o] = build_occurrences(evs, as_of=T0 + timedelta(hours=1))
    assert o["peak_severity"] == "major" and o["had_road_closure"]
    assert o["duration_minutes"] == 30 and not o["is_open"]
    assert o["update_count"] == 2 and o["resolution_reason"] == "absent"


def test_reopened_entity_counts_as_two_occurrences():
    evs = [_ev("created", 0), _ev("resolved", 10), _ev("reopened", 40), _ev("resolved", 50)]
    occ = build_occurrences(evs, as_of=T0 + timedelta(hours=2))
    assert [o["occurrence_id"] for o in occ] == ["ohgo:incident:1#1", "ohgo:incident:1#2"]


def test_open_occurrence_duration_runs_to_as_of():
    [o] = build_occurrences([_ev("created", 0)], as_of=T0 + timedelta(minutes=45))
    assert o["is_open"] and o["ended_at"] is None and o["duration_minutes"] == 45


def test_duplicate_deliveries_ignored():
    e = _ev("created", 0)
    assert len(build_occurrences([e, dict(e)], as_of=T0 + timedelta(minutes=1))) == 1


def test_repeated_polling_does_not_inflate_hotspots(make_pipeline, ohgo_adapter):
    """One incident seen for 3 hours (180 polls) must weigh the same as one seen once."""
    from mvoi.publishers.base import MemoryPublisher
    pub = MemoryPublisher()
    p = make_pipeline(publisher=pub)
    item = {"links": [], "id": "long", "latitude": 39.80, "longitude": -84.19, "location": "x",
            "description": "Crash", "category": "Crash", "direction": "NB", "routeName": "I-75",
            "roadStatus": "Closed", "roadClosureDetails": None}
    env = {"lastUpdated": None, "rejectedFilters": [], "totalResultCount": 1, "results": [item]}
    for m in range(180):
        p.process([ohgo_adapter.parse_envelope("incidents", env, T0 + timedelta(minutes=m))],
                  T0 + timedelta(minutes=m))
    lifecycle = [e for e in pub.events if e["entity_type"] == "incident"]
    assert len(lifecycle) == 1  # change detection, not 180 rows
    by_entity = defaultdict(list)
    for e in lifecycle:
        by_entity[e["entity_key"]].append(e)
    occ = [o for evs in by_entity.values() for o in build_occurrences(evs, T0 + timedelta(hours=3))]
    assert len(occ) == 1 and occ[0]["duration_minutes"] == 180


BB = BoundingBox(north=40.0, south=39.6, west=-84.4, east=-84.0)


def test_hotspot_detects_cluster():
    cluster = [(39.80 + i * 0.001, -84.20 + i * 0.001, 1.0) for i in range(25)]
    scatter = [(39.65 + (i % 5) * 0.07, -84.35 + (i // 5) * 0.07, 1.0) for i in range(25)]
    bins = find_hot_spots(cluster + scatter, BB, bin_size_m=1600, neighborhood_m=3200)
    best = max(bins, key=lambda b: b.GiZScore)
    assert best.Gi_Bin == 3 and best.GiPValue < 0.01
    assert abs(best.center_lat - 39.81) < 0.03 and abs(best.center_lon + 84.19) < 0.03
    assert sum(b.value for b in bins) == 50


def test_hotspot_uniform_or_empty_has_no_significance():
    assert all(b.Gi_Bin == 0 for b in find_hot_spots([], BB))


def test_hotspot_wkt_is_closed_polygon():
    b = find_hot_spots([(39.8, -84.2, 1)], BB)[0]
    coords = b.wkt[len("POLYGON(("):-2].split(",")
    assert coords[0] == coords[-1] and len(coords) == 5


def test_dq_rules_use_configured_aoi(geography):
    rs = {r.name: r for r in rules(geography)}
    assert "39.45" in rs["transport_in_aoi"].expression
    assert {r.severity for r in rs.values()} <= {"error", "warn"}
    assert len(rs) == len(rules(geography))
