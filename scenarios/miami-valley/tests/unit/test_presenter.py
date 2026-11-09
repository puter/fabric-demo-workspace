"""Presenter app: data agent MCP client and the local HTTP server."""

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest
import respx

from mvoi.presenter.agent import AgentError, DataAgentClient
from mvoi.presenter.server import MAX_QUESTION, make_handler

URL = "https://api.fabric.microsoft.com/v1/mcp/workspaces/ws1/dataagents/ag1/agent"


def _agent():
    return DataAgentClient("ws1", "ag1", "da_mvoi", token=lambda scope: "tok")


@respx.mock
def test_ask_calls_the_agent_tool_and_joins_text():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": "Sinclair"}, {"type": "text", "text": "95.7"}],
                   "isError": False}}))
    assert _agent().ask("Top site?") == "Sinclair\n\n95.7"
    sent = json.loads(route.calls[0].request.content)
    assert sent["method"] == "tools/call"
    assert sent["params"] == {"name": "DataAgent_da_mvoi", "arguments": {"userQuestion": "Top site?"}}
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(401, text="unauthorized"),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "boom"}}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                              "result": {"content": [{"type": "text", "text": "bad"}], "isError": True}}),
])
def test_agent_failures_raise(response):
    respx.post(URL).mock(return_value=response)
    with pytest.raises(AgentError):
        _agent().ask("q")


@pytest.fixture
def server():
    asked = []

    def ask(q):
        if q == "fail":
            raise AgentError("agent down")
        asked.append(q)
        return f"answer to {q}"

    cfg = {"clientId": "c", "tenantId": "t", "workspaceId": "w", "dashboardId": "d",
           "dashboardName": "rtd", "agentName": "da"}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, ask))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", asked
    srv.shutdown()


def test_serves_page_config_and_logo(server):
    base, _ = server
    assert httpx.get(f"{base}/").headers["content-type"].startswith("text/html")
    assert httpx.get(f"{base}/app.js").status_code == 200
    assert httpx.get(f"{base}/logo.png").headers["content-type"] == "image/png"
    assert httpx.get(f"{base}/api/config").json()["dashboardId"] == "d"


def test_static_files_cannot_escape_the_folder(server):
    base, _ = server
    assert httpx.get(f"{base}/../server.py").status_code == 404
    assert httpx.get(f"{base}/%2e%2e/server.py").status_code == 404


def test_ask_endpoint(server):
    base, asked = server
    r = httpx.post(f"{base}/api/ask", json={"question": "  Which site?  "})
    assert r.json() == {"answer": "answer to Which site?"} and asked == ["Which site?"]
    assert httpx.post(f"{base}/api/ask", json={"question": ""}).status_code == 400
    assert httpx.post(f"{base}/api/ask", json={"question": "x" * (MAX_QUESTION + 1)}).status_code == 400
    r = httpx.post(f"{base}/api/ask", json={"question": "fail"})
    assert r.status_code == 502 and "agent down" in r.json()["error"]
