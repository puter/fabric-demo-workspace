"""End-to-end replay through the real pipeline (no network, no Fabric)."""

from datetime import timedelta

import pytest

from mvoi.common.config import LifecycleSettings
from mvoi.models.events import RunMode
from mvoi.publishers.base import MemoryPublisher
from mvoi.replay import player as rp
from mvoi.replay.recording import read_recording
from mvoi.replay.synthetic import write_storyline
from mvoi.services.state import StateStore
from tests.conftest import T0


class FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)
        return False  # not stopped


@pytest.fixture
def adapters(ohgo_adapter, nws_adapter):
    return {"ohgo": ohgo_adapter, "nws": nws_adapter}


@pytest.fixture
def recording(tmp_path):
    return write_storyline(tmp_path / "rec.jsonl", start=T0)


def _play(recording, make_pipeline, adapters, speed=4.0, session="s1"):
    clock = FakeClock(T0 + timedelta(days=30))
    pub = MemoryPublisher()
    store = StateStore(":memory:")
    base = LifecycleSettings(resolve_after_missed_polls=3, resolve_after_minutes=5)

    def factory(sid, spd):
        p = make_pipeline(publisher=pub, lifecycle=rp.scaled_lifecycle(base, spd),
                          run_mode=RunMode.REPLAY, session=sid, st=store)
        p.clock = clock
        return p

    player = rp.ReplayPlayer(recording, factory, adapters, speed=speed,
                             session_id=session, clock=clock, sleep=clock.sleep)
    cycles = player.run()
    return player, pub, store, clock, cycles


def test_recording_roundtrip(recording):
    manifest, frames = read_recording(recording)
    assert manifest.synthetic is True
    assert frames[0].t == T0 and len(frames) > 100


def test_replay_storyline_produces_expected_alerts(recording, make_pipeline, adapters):
    player, pub, store, clock, cycles = _play(recording, make_pipeline, adapters)
    assert cycles == 37
    ev = pub.events
    assert all(e["run_mode"] == "replay" and e["replay_session_id"] == "s1" for e in ev)
    assert all(e["environment"] == "test" for e in ev)

    raised = {e["attributes"]["alert_key"] for e in ev
              if e["entity_type"] == "operational_alert" and e["status"] == "raised"}
    assert "facility_risk_high:FAC-001" in raised
    assert "incident_near_facility:FAC-002:ohgo:incident:SYN-I-040" in raised
    assert any(k.startswith("weather_at_facility:FAC-001") for k in raised)
    cleared = {e["attributes"]["alert_key"] for e in ev
               if e["entity_type"] == "operational_alert" and e["status"] == "cleared"}
    assert raised == cleared  # every condition recovers by the end of the storyline

    # Synthetic marker flows through to every source event.
    src = [e for e in ev if e["source"] in ("ohgo", "nws")]
    assert src and all(e["attributes"].get("synthetic") is True for e in src)

    # Accelerated: 36 recorded minutes play in 9 wall minutes.
    assert clock.t - (T0 + timedelta(days=30)) == timedelta(minutes=9)

    # Timestamps inside payloads were remapped onto the replay timeline.
    wx = [e for e in ev if e["entity_type"] == "weather_alert" and e["change_type"] == "created"][0]
    assert wx["end_time"].startswith((T0 + timedelta(days=30, minutes=6)).date().isoformat())


def test_replay_reset_resolves_everything(recording, make_pipeline, adapters):
    player, pub, store, clock, _ = _play(recording, make_pipeline, adapters, session="s2")
    # Construction zones are still active at the end of the storyline.
    assert store.active()
    n = rp.reset_session(store, player.pipeline)
    assert n >= 2 and store.active() == []
    resets = [e for e in pub.events if e["attributes"].get("resolution_reason") == "replay_reset"]
    assert len(resets) == n and all(e["change_type"] == "resolved" for e in resets)


def test_time_map_and_shift():
    tm = rp.TimeMap(T0, T0 + timedelta(hours=1), speed=2)
    assert tm(T0 + timedelta(minutes=10)) == T0 + timedelta(hours=1, minutes=5)
    shifted = rp.shift_times({"expires": "2026-01-15T14:10:00Z", "other": "2026-01-15T14:10:00Z"}, tm)
    assert shifted["expires"] == "2026-01-15T15:05:00Z"
    assert shifted["other"] == "2026-01-15T14:10:00Z"
