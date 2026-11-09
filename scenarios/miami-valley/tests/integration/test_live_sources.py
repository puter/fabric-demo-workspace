"""Live smoke tests against the real public APIs.

    uv run pytest -m live tests/integration/test_live_sources.py

NWS needs only network access. OHGO needs OHGO_API_KEY (free registration at
https://publicapi.ohgo.com/accounts/registration) and is skipped without it.
These verify assumptions the docs leave ambiguous, notably the map-bounds
coordinate order.
"""

from __future__ import annotations

import os

import httpx
import pytest

from mvoi.common.config import NwsSettings, OhgoSettings
from mvoi.common.http import ResilientClient
from mvoi.models.events import utcnow
from mvoi.sources.nws import NwsAdapter
from mvoi.sources.ohgo import OhgoAdapter

pytestmark = pytest.mark.live


@pytest.fixture
def client():
    c = ResilientClient(httpx.Client(timeout=30), max_retries=2, backoff_base=1)
    yield c


def test_nws_active_alerts_parse(client, geography, zones):
    ua = os.environ.get("MVOI_NWS__USER_AGENT", "MiamiValleyOpsIntelligence/0.1 (live test)")
    a = NwsAdapter(client, NwsSettings(user_agent=ua), geography, zones, "test")
    [r] = a.poll(utcnow())
    assert r.ok, r.error
    assert r.total_reported is not None
    assert not r.rejected, r.rejected[:1]
    for o in r.observations:  # whatever is active in Ohio right now must be in our AOI
        assert o.event.attributes["aoi_zones"] or o.event.attributes["geometry_source"] == "alert_polygon"


@pytest.mark.skipif(not os.environ.get("OHGO_API_KEY"), reason="OHGO_API_KEY not set")
def test_ohgo_resources_parse_and_respect_bbox(client, geography, corridors):
    a = OhgoAdapter(client, OhgoSettings(use_etag=False), os.environ["OHGO_API_KEY"],
                    geography.bounding_box, corridors, "test")
    results = a.poll(utcnow())
    for r in results:
        assert r.ok, (r.entity_type, r.error)
        env = r.raw_envelope or {}
        assert not env.get("rejectedFilters"), f"OHGO rejected filters: {env.get('rejectedFilters')}"
        # Server-side map-bounds must already confine results to the AOI; if the
        # coordinate order were wrong the API would return points elsewhere.
        raw = env.get("results") or []
        inside = [x for x in raw if geography.bounding_box.contains(x["latitude"], x["longitude"])]
        assert len(inside) == len(raw), f"{r.entity_type}: {len(raw) - len(inside)} results outside bbox"
        assert len(r.rejected) == 0, r.rejected[:1]
