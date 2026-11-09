from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from mvoi.common.config import load_settings
from mvoi.models.events import (
    ChangeType,
    EntityType,
    NormalizedEvent,
    Source,
    deterministic_source_id,
    make_entity_key,
)

T = datetime(2026, 1, 15, 14, 0, tzinfo=UTC)


def _ev(**kw):
    base = dict(entity_key=make_entity_key("ohgo", "incident", "1"), source=Source.OHGO,
                entity_type=EntityType.INCIDENT, source_id="1", change_type=ChangeType.CREATED,
                observed_at=T, latitude=39.75, longitude=-84.19, severity="major", title="Crash")
    base.update(kw)
    return NormalizedEvent(**base).finalize()


def test_event_id_is_deterministic():
    assert _ev().event_id == _ev().event_id
    assert _ev().event_id != _ev(change_type=ChangeType.UPDATED).event_id
    assert _ev().event_id != _ev(run_mode="replay", replay_session_id="s1").event_id


def test_poll_time_does_not_change_content_or_id():
    a, b = _ev(), _ev(observed_at=T + timedelta(minutes=5))
    assert a.content_hash == b.content_hash
    assert a.event_id == b.event_id


def test_live_attributes_excluded_from_content_hash():
    a = _ev(attributes={"speed_band": "<25%", "live_current_mph": 11})
    b = _ev(attributes={"speed_band": "<25%", "live_current_mph": 14})
    c = _ev(attributes={"speed_band": "25-50%", "live_current_mph": 14})
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


def test_timestamps_normalised_to_utc_and_serialised_with_z():
    est = timezone(timedelta(hours=-5))
    e = _ev(observed_at=datetime(2026, 1, 15, 9, 0, tzinfo=est))
    assert e.observed_at == T
    assert e.to_wire()["observed_at"] == "2026-01-15T14:00:00Z"


def test_naive_timestamps_assumed_utc():
    e = _ev(observed_at=datetime(2026, 1, 15, 14, 0))
    assert e.observed_at.tzinfo is not None and e.observed_at == T


@pytest.mark.parametrize("lat,lon", [(91, 0), (0, -181)])
def test_coordinate_validation(lat, lon):
    with pytest.raises(ValidationError):
        _ev(latitude=lat, longitude=lon)


def test_severity_rank_filled():
    assert _ev(severity="critical").severity_rank == 4


def test_deterministic_source_id_stable():
    assert deterministic_source_id("a", 1.0) == deterministic_source_id("a", 1.0)
    assert deterministic_source_id("a", 1.0).startswith("h-")


def test_settings_env_override():
    s = load_settings(env={"MVOI_PUBLISHER__KIND": "file", "MVOI_POLLING__INTERVAL_SECONDS": "90"})
    assert s.publisher.kind == "file"
    assert s.polling.interval_seconds == 90


def test_poll_interval_floor():
    with pytest.raises(ValidationError):
        load_settings(env={"MVOI_POLLING__INTERVAL_SECONDS": "5"})


def test_risk_band_lookup(risk_cfg):
    assert risk_cfg.band_for(0) == "low"
    assert risk_cfg.band_for(49.9) == "guarded"
    assert risk_cfg.band_for(70) == "high"
    assert risk_cfg.band_for(100) == "severe"
