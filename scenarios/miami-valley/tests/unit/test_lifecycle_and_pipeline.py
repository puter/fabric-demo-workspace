from datetime import timedelta

from mvoi.common.config import LifecycleSettings
from mvoi.publishers.base import MemoryPublisher
from mvoi.services.lifecycle import LifecycleTracker
from mvoi.sources.base import PollResult
from tests.conftest import T0


def _item(id_, status="Closed", lat=39.809, lon=-84.204):
    return {"links": [], "id": id_, "latitude": lat, "longitude": lon, "location": "x",
            "description": "Crash", "category": "Crash", "direction": "NB", "routeName": "I-75",
            "roadStatus": status, "roadClosureDetails": None}


def _env(items):
    return {"lastUpdated": "2026-01-15T14:00:00Z", "rejectedFilters": [],
            "totalResultCount": len(items), "results": items}


def _poll(adapter, items, minutes):
    return adapter.parse_envelope("incidents", _env(items), T0 + timedelta(minutes=minutes))


def _tracker(store, **kw):
    s = LifecycleSettings(**{"resolve_after_missed_polls": 3, "resolve_after_minutes": 2, **kw})
    return LifecycleTracker(store, s, environment="test")


def test_created_then_unchanged_then_updated(store, ohgo_adapter):
    t = _tracker(store)
    out = t.apply(_poll(ohgo_adapter, [_item("1")], 0))
    assert [e.change_type for e in out.events] == ["created"]
    out = t.apply(_poll(ohgo_adapter, [_item("1")], 1))
    assert out.events == [] and out.unchanged == 1
    out = t.apply(_poll(ohgo_adapter, [_item("1", status="Partially Closed")], 2))
    assert [e.change_type for e in out.events] == ["updated"]
    assert out.events[0].first_seen_at == T0


def test_resolution_requires_missed_polls_and_minutes(store, ohgo_adapter):
    t = _tracker(store, resolve_after_missed_polls=3, resolve_after_minutes=5)
    t.apply(_poll(ohgo_adapter, [_item("1")], 0))
    for m in (1, 2, 3):  # 3 misses but only 3 minutes absent
        assert t.apply(_poll(ohgo_adapter, [], m)).events == []
    out = t.apply(_poll(ohgo_adapter, [], 6))
    assert [e.change_type for e in out.events] == ["resolved"]
    assert out.events[0].attributes["resolution_reason"] == "absent"


def test_reappearance_resets_miss_counter(store, ohgo_adapter):
    t = _tracker(store)
    t.apply(_poll(ohgo_adapter, [_item("1")], 0))
    t.apply(_poll(ohgo_adapter, [], 1))
    t.apply(_poll(ohgo_adapter, [], 2))
    t.apply(_poll(ohgo_adapter, [_item("1")], 3))  # flicker back
    assert t.apply(_poll(ohgo_adapter, [], 4)).events == []
    assert store.get("ohgo:incident:1").missed_polls == 1


def test_reopened_after_resolution(store, ohgo_adapter):
    t = _tracker(store, resolve_after_missed_polls=1, resolve_after_minutes=0)
    t.apply(_poll(ohgo_adapter, [_item("1")], 0))
    assert t.apply(_poll(ohgo_adapter, [], 1)).events[0].change_type == "resolved"
    assert t.apply(_poll(ohgo_adapter, [_item("1")], 2)).events[0].change_type == "reopened"


def test_failed_and_not_modified_polls_never_resolve(store, ohgo_adapter):
    t = _tracker(store, resolve_after_missed_polls=1, resolve_after_minutes=0)
    t.apply(_poll(ohgo_adapter, [_item("1")], 0))
    failed = PollResult("ohgo", "incident", T0 + timedelta(minutes=5), ok=False, error="HTTP 503")
    assert t.apply(failed).events == []
    nm = PollResult("ohgo", "incident", T0 + timedelta(minutes=6), ok=True, not_modified=True)
    assert t.apply(nm).events == []
    assert store.get("ohgo:incident:1").status == "active"


def test_ratio_guard_blocks_mass_resolution(store, ohgo_adapter):
    t = _tracker(store, resolve_after_missed_polls=1, resolve_after_minutes=0)
    items = [_item(str(i), lat=39.8 + i / 1000) for i in range(10)]
    t.apply(_poll(ohgo_adapter, items, 0))
    out = t.apply(_poll(ohgo_adapter, items[:2], 1))  # 2 of 10 -> suspicious
    assert out.suppressed_resolution and out.events == []
    out = t.apply(_poll(ohgo_adapter, items[:6], 2))  # 6 of 10 -> plausible
    assert len([e for e in out.events if e.change_type == "resolved"]) == 4


def test_ratio_guard_gives_up_when_low_count_persists(store, ohgo_adapter):
    t = _tracker(store, resolve_after_missed_polls=1, resolve_after_minutes=0,
                 max_resolution_suppression_minutes=15)
    items = [_item(str(i), lat=39.8 + i / 1000) for i in range(10)]
    t.apply(_poll(ohgo_adapter, items, 0))
    for m in (1, 8, 15):  # guard holds for the first 14 minutes of the streak
        assert t.apply(_poll(ohgo_adapter, items[:1], m)).suppressed_resolution
    out = t.apply(_poll(ohgo_adapter, items[:1], 16))
    assert not out.suppressed_resolution
    assert len([e for e in out.events if e.change_type == "resolved"]) == 9


def test_ratio_guard_streak_resets_on_a_normal_poll(store, ohgo_adapter):
    t = _tracker(store, resolve_after_missed_polls=1, resolve_after_minutes=0,
                 max_resolution_suppression_minutes=15)
    items = [_item(str(i), lat=39.8 + i / 1000) for i in range(10)]
    t.apply(_poll(ohgo_adapter, items, 0))
    t.apply(_poll(ohgo_adapter, items[:1], 1))
    t.apply(_poll(ohgo_adapter, items, 10))  # full response ends the streak
    assert t.apply(_poll(ohgo_adapter, items[:1], 20)).suppressed_resolution


def test_duplicate_in_one_response_counted_once(store, ohgo_adapter):
    out = _tracker(store).apply(_poll(ohgo_adapter, [_item("1"), _item("1")], 0))
    assert len(out.events) == 1


def test_weather_expiry_resolves(store, nws_adapter):
    import copy

    from tests.conftest import load_fixture
    f = copy.deepcopy(load_fixture("nws_alerts_active_sample.json")["features"][0])
    f["geometry"] = {"type": "Polygon", "coordinates": [[[-84.3, 39.7], [-84.1, 39.7], [-84.1, 39.9],
                                                          [-84.3, 39.9], [-84.3, 39.7]]]}
    f["properties"]["ends"] = None
    f["properties"]["expires"] = (T0 + timedelta(minutes=10)).isoformat()
    t = _tracker(store)
    assert t.apply(nws_adapter.parse_envelope("alerts", {"features": [f]}, T0)).events
    later = nws_adapter.parse_envelope("alerts", {"features": [f]}, T0 + timedelta(minutes=11))
    out = t.apply(later)
    assert [e.attributes["resolution_reason"] for e in out.events] == ["expired"]


# ------------------------------------------------------------------- pipeline / outbox


class _Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def test_outbox_retains_events_across_publish_failure(make_pipeline, ohgo_adapter):
    pub = MemoryPublisher()
    p = make_pipeline(publisher=pub)
    p.clock = clock = _Clock()
    pub.fail_next = 1
    report = p.process([_poll(ohgo_adapter, [_item("1")], 0)], T0)
    assert report.publish_error and pub.events == []
    assert p.store.outbox_depth() > 0
    clock.advance(30)
    published, err = p.flush()
    assert err is None and published > 0 and p.store.outbox_depth() == 0
    assert any(e["entity_key"] == "ohgo:incident:1" for e in pub.events)
    assert all(e["ingested_at"] for e in pub.events)


def test_publish_retries_back_off_while_failing_and_reset_on_success(make_pipeline, ohgo_adapter):
    pub = MemoryPublisher()
    p = make_pipeline(publisher=pub)
    p.clock = clock = _Clock()
    attempts = []
    publish = pub.publish
    pub.publish = lambda payloads: attempts.append(clock.now) or publish(payloads)

    pub.fail_next = 3
    p.process([_poll(ohgo_adapter, [_item("1")], 0)], T0)  # attempt 1 fails -> retry in 30 s
    for _ in range(5):                                         # ticks inside the backoff: no attempts
        clock.advance(5)
        assert p.flush() == (0, p._publish_error) and len(attempts) == 1
    clock.advance(5)
    p.flush()                                                  # t=30: attempt 2 fails -> retry in 60 s
    clock.advance(59)
    p.flush()
    assert len(attempts) == 2
    clock.advance(1)
    p.flush()                                                  # t=90: attempt 3 fails -> retry in 120 s
    assert [(a - T0).total_seconds() for a in attempts] == [0, 30, 90]
    assert p.metrics.publish_retry_at == T0 + timedelta(seconds=210)

    clock.advance(120)
    published, err = p.flush()                                 # t=210: succeeds, backoff resets
    assert err is None and published > 0 and p.metrics.publish_retry_at is None
    pub.fail_next = 1
    p.flush()
    p.process([_poll(ohgo_adapter, [_item("1"), _item("2")], 4)], clock.now)
    assert p.metrics.publish_retry_at == clock.now + timedelta(seconds=30)


def test_publish_failure_keeps_poller_live_but_reports_it(make_pipeline, ohgo_adapter):
    from mvoi.models.events import utcnow
    from mvoi.services.health import Metrics

    pub = MemoryPublisher()
    m = Metrics()
    p = make_pipeline(publisher=pub)
    p.metrics = m
    pub.fail_next = 1
    p.process([_poll(ohgo_adapter, [_item("1")], 0)], utcnow())  # healthy() reads the real clock
    assert m.healthy(timedelta(minutes=5))  # a restart cannot fix an Eventstream outage
    assert m.last_publish_ok is False and m.snapshot()["publish_retry_at"]


def test_pipeline_emits_heartbeat_and_risk(make_pipeline, ohgo_adapter):
    pub = MemoryPublisher()
    p = make_pipeline(publisher=pub)
    p.process([_poll(ohgo_adapter, [_item("1")], 0)], T0)
    types = {e["entity_type"] for e in pub.events}
    assert {"incident", "facility_risk", "ingestion_heartbeat"} <= types
    hb = [e for e in pub.events if e["entity_type"] == "ingestion_heartbeat"][0]
    assert hb["attributes"]["polls"][0]["ok"] is True
    assert hb["attributes"]["active_counts"]["incident"] == 1

    # Second identical cycle: no lifecycle or risk events, only heartbeat.
    pub.events.clear()
    p.process([_poll(ohgo_adapter, [_item("1")], 1)], T0 + timedelta(minutes=1))
    assert {e["entity_type"] for e in pub.events} == {"ingestion_heartbeat"}


def test_heartbeat_lists_active_keys_and_keeps_emptied_types(make_pipeline, ohgo_adapter):
    pub = MemoryPublisher()
    p = make_pipeline(publisher=pub)
    p.process([_poll(ohgo_adapter, [_item("1"), _item("2")], 0)], T0)
    hb = [e for e in pub.events if e["entity_type"] == "ingestion_heartbeat"][-1]
    keys = sorted(e["entity_key"] for e in pub.events if e["entity_type"] == "incident")
    assert hb["attributes"]["active_keys"] == {"incident": keys}

    # Both resolve: the type stays listed with no keys, so KQL can hide stale rows.
    for minute in (1, 2, 3):
        p.process([_poll(ohgo_adapter, [], minute)], T0 + timedelta(minutes=minute))
    hb = [e for e in pub.events if e["entity_type"] == "ingestion_heartbeat"][-1]
    assert hb["attributes"]["active_keys"] == {"incident": []}
    assert hb["attributes"]["active_counts"] == {}


def test_event_ids_unique_and_outbox_idempotent(make_pipeline, ohgo_adapter, store):
    pub = MemoryPublisher()
    p = make_pipeline(publisher=pub)
    p.process([_poll(ohgo_adapter, [_item("1")], 0)], T0)
    ids = [e["event_id"] for e in pub.events]
    assert len(ids) == len(set(ids))

    # While pending, enqueueing the same event_id twice is a no-op (exactly-once
    # into the outbox); after delivery, downstream dedups on event_id.
    store.enqueue([{"event_id": "dup"}], T0)
    assert store.enqueue([{"event_id": "dup"}], T0) == 0


def test_history_baseline_local_and_missing(tmp_path):
    import json as _json

    from mvoi.services.pipeline import HistoryBaseline

    f = tmp_path / "b.json"
    f.write_text(_json.dumps({"facilities": {"FAC-001": 4}}))
    assert HistoryBaseline(f).get() == {"FAC-001": 4}
    assert HistoryBaseline(tmp_path / "missing.json").get() == {}
    assert HistoryBaseline(None).get() == {}
