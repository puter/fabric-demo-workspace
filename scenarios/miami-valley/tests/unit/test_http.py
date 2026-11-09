import httpx
import pytest
import respx

from mvoi.common.http import FetchError, ResilientClient

URL = "https://publicapi.ohgo.com/api/v1/incidents"


def _client(**kw):
    sleeps: list[float] = []
    c = ResilientClient(httpx.Client(), max_retries=kw.get("max_retries", 3), backoff_base=1,
                        backoff_max=10, sleep=sleeps.append)
    return c, sleeps


@respx.mock
def test_retries_then_succeeds_honouring_retry_after():
    route = respx.get(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.Response(200, json={"results": []}),
    ])
    c, sleeps = _client()
    r = c.get_json(URL)
    assert r.status_code == 200 and r.attempts == 3 and route.call_count == 3
    assert sleeps[0] == 7  # Retry-After honoured
    assert 0 < sleeps[1] <= 10


@respx.mock
def test_gives_up_after_max_retries():
    respx.get(URL).mock(return_value=httpx.Response(500))
    c, sleeps = _client(max_retries=2)
    with pytest.raises(FetchError) as ei:
        c.get_json(URL)
    assert ei.value.status_code == 500 and len(sleeps) == 2


@respx.mock
def test_auth_error_not_retried():
    route = respx.get(URL).mock(return_value=httpx.Response(401, text="bad key"))
    c, sleeps = _client()
    with pytest.raises(FetchError) as ei:
        c.get_json(URL)
    assert ei.value.status_code == 401 and route.call_count == 1 and sleeps == []


@respx.mock
def test_transport_errors_retried():
    respx.get(URL).mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})])
    c, _ = _client()
    assert c.get_json(URL).attempts == 2


@respx.mock
def test_etag_conditional_request():
    route = respx.get(URL).mock(side_effect=[
        httpx.Response(200, json={"results": [1]}, headers={"ETag": '"v1"'}),
        httpx.Response(304),
    ])
    c, _ = _client()
    first = c.get_json(URL, use_etag=True, cache_key="incidents")
    second = c.get_json(URL, use_etag=True, cache_key="incidents")
    assert first.data == {"results": [1]}
    assert second.not_modified and second.data is None
    assert route.calls[1].request.headers["If-None-Match"] == '"v1"'


@respx.mock
def test_ohgo_adapter_sends_documented_auth_header_and_bbox(geography, corridors):
    from mvoi.common.config import OhgoSettings
    from mvoi.sources.ohgo import OhgoAdapter
    from tests.conftest import T0

    route = respx.get(URL).mock(return_value=httpx.Response(200, json={
        "lastUpdated": "2026-01-15T14:00:00Z", "rejectedFilters": [], "totalResultCount": 0,
        "results": []}))
    c, _ = _client()
    a = OhgoAdapter(c, OhgoSettings(resources=["incidents"]), "k123", geography.bounding_box,
                    corridors)
    [r] = a.poll(T0)
    req = route.calls[0].request
    assert req.headers["Authorization"] == "APIKEY k123"
    assert req.url.params["map-bounds-sw"] == "39.45,-84.6"
    assert req.url.params["page-all"] == "true"
    assert r.ok


@respx.mock
def test_ohgo_adapter_reports_failure_without_raising(geography, corridors):
    from mvoi.common.config import OhgoSettings
    from mvoi.sources.ohgo import OhgoAdapter
    from tests.conftest import T0

    respx.get(URL).mock(return_value=httpx.Response(401))
    c, _ = _client()
    a = OhgoAdapter(c, OhgoSettings(resources=["incidents"]), "bad", geography.bounding_box, corridors)
    [r] = a.poll(T0)
    assert not r.ok and "401" in r.error
