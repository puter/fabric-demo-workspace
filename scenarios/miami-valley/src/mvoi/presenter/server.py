"""Local presenter app: the live Fabric dashboard plus a data agent chat (ADR 0004).

    uv run --extra fabric mvoi-presenter        # then open http://localhost:8000

The browser signs in with MSAL against the "MVOI Presenter (local)" Entra app
and embeds `rtd_mvoi_operations` with the Fabric Embed SDK, so the map is the
real Fabric dashboard, not a copy. Questions go browser -> this server -> the
data agent's MCP endpoint, as the `az` user.

Runs on localhost only (the app registration allows no other redirect URI).
Every id is resolved at startup by name; nothing is committed.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import logging
import mimetypes
import os
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from mvoi.presenter.agent import AgentError, DataAgentClient

log = logging.getLogger(__name__)

ROOT = Path(__file__).parents[3]
STATIC = Path(__file__).parent / "static"
LOGO = ROOT / "fabric" / "dashboards" / "sd-labs-badge.png"
APP_NAME = "MVOI Presenter (local)"
GRAPH = "https://graph.microsoft.com"
MAX_QUESTION = 500


def _claim(token: str, name: str) -> Any:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))[name]


def resolve_config(client, config_path: Path) -> tuple[dict[str, str], DataAgentClient]:
    """Looks up the workspace, dashboard, agent and Entra app by name."""
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    names = cfg["items"]
    ws = client.workspace_id(cfg["workspace_name"])
    found = {}
    for key, item_type in (("dashboard", "KQLDashboard"), ("data_agent", "DataAgent")):
        found[key] = client.find_item(ws, item_type, names[key])
        if found[key] is None:
            raise SystemExit(f"{item_type} '{names[key]}' not found; run scripts/deploy_fabric.py first")
    graph_token = client.token(f"{GRAPH}/.default")
    client_id = os.environ.get("MVOI_PRESENTER_CLIENT_ID") or _app_client_id(client, graph_token)
    browser = {"clientId": client_id, "tenantId": _claim(graph_token, "tid"), "workspaceId": ws,
               "dashboardId": found["dashboard"]["id"], "dashboardName": names["dashboard"],
               "agentName": names["data_agent"]}
    agent = DataAgentClient(ws, found["data_agent"]["id"], names["data_agent"], client.token)
    return browser, agent


def _app_client_id(client, graph_token: str) -> str:
    r = client._http.get(f"{GRAPH}/v1.0/applications",
                         params={"$filter": f"displayName eq '{APP_NAME}'", "$select": "appId"},
                         headers={"Authorization": f"Bearer {graph_token}"})
    r.raise_for_status()
    apps = r.json()["value"]
    if len(apps) != 1:
        raise SystemExit(f"expected one Entra app named '{APP_NAME}', found {len(apps)} "
                         "(see docs/presenter.md, or set MVOI_PRESENTER_CLIENT_ID)")
    return apps[0]["appId"]


def make_handler(browser_config: dict[str, str], ask: Callable[[str], str]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/api/config":
                self._json(200, browser_config)
            elif path == "/logo.png":
                self._file(LOGO)
            elif path in ("/", "/index.html"):
                self._file(STATIC / "index.html")
            elif (STATIC / path.lstrip("/")).resolve().parent == STATIC.resolve():
                self._file(STATIC / path.lstrip("/"))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/api/ask":
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid JSON"})
            question = str(body.get("question", "")).strip()
            if not question or len(question) > MAX_QUESTION:
                return self._json(400, {"error": f"question must be 1-{MAX_QUESTION} characters"})
            try:
                self._json(200, {"answer": ask(question)})
            except AgentError as exc:
                log.warning("data agent failed: %s", exc)
                self._json(502, {"error": str(exc)})

        def _file(self, path: Path) -> None:
            if not path.is_file():
                return self._json(404, {"error": "not found"})
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8000,
                   help="must match the app registration's redirect URI (default 8000)")
    p.add_argument("--config", default=str(ROOT / "config" / "fabric.yaml"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from mvoi.deploy.fabric_api import FabricClient
    browser, agent = resolve_config(FabricClient(), Path(args.config))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(browser, agent.ask))
    log.info("presenter on http://localhost:%d (dashboard %s, agent %s)",
             args.port, browser["dashboardName"], browser["agentName"])
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
