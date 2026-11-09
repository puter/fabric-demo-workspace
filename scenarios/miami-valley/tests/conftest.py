from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mvoi.common.config import LifecycleSettings, RiskSettings, load_geography, load_risk_config
from mvoi.geo.reference import CorridorMatcher, ZoneIndex, load_facilities
from mvoi.models.events import RunMode
from mvoi.publishers.base import MemoryPublisher
from mvoi.services.pipeline import Pipeline
from mvoi.services.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 1, 15, 14, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def geography():
    return load_geography()


@pytest.fixture(scope="session")
def corridors(geography):
    return CorridorMatcher(geography)


@pytest.fixture(scope="session")
def zones():
    return ZoneIndex()


@pytest.fixture(scope="session")
def risk_cfg():
    return load_risk_config()


@pytest.fixture(scope="session")
def facilities():
    return load_facilities()


@pytest.fixture
def store():
    s = StateStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def ohgo_adapter(geography, corridors):
    from mvoi.common.config import OhgoSettings
    from mvoi.sources.ohgo import OhgoAdapter

    return OhgoAdapter(None, OhgoSettings(), None, geography.bounding_box, corridors, "test")


@pytest.fixture
def nws_adapter(geography, zones):
    from mvoi.common.config import NwsSettings
    from mvoi.sources.nws import NwsAdapter

    return NwsAdapter(None, NwsSettings(), geography, zones, "test")


@pytest.fixture
def make_pipeline(store, facilities, risk_cfg, ohgo_adapter, nws_adapter):
    def _make(publisher=None, lifecycle=None, run_mode=RunMode.LIVE, session=None, st=None):
        return Pipeline(
            adapters={"ohgo": ohgo_adapter, "nws": nws_adapter},
            intervals={"ohgo": 60, "nws": 120},
            store=st or store,
            lifecycle=lifecycle or LifecycleSettings(resolve_after_missed_polls=3,
                                                     resolve_after_minutes=2),
            publisher=publisher or MemoryPublisher(),
            facilities=facilities, risk_cfg=risk_cfg, risk_settings=RiskSettings(),
            environment="test", run_mode=run_mode, replay_session_id=session,
        )
    return _make


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())
