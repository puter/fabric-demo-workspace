"""Asks the published data agent (`da_mvoi`) a question over its MCP endpoint.

A published Fabric data agent is an MCP server with a single tool,
`DataAgent_<name>(userQuestion)`, at

    https://api.fabric.microsoft.com/v1/mcp/workspaces/{ws}/dataagents/{id}/agent

(The OpenAI Assistants route in older samples was retired on 2026-08-26.)
Each call is stateless: the agent sees one question, not the conversation.
Calls run as the signed-in `az` user and consume Copilot capacity.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import httpx

from mvoi.deploy.fabric_api import FABRIC

SCOPE = f"{FABRIC}/.default"


class AgentError(RuntimeError):
    pass


class DataAgentClient:
    def __init__(self, workspace_id: str, agent_id: str, agent_name: str,
                 token: Callable[[str], str], http: httpx.Client | None = None):
        self.url = f"{FABRIC}/v1/mcp/workspaces/{workspace_id}/dataagents/{agent_id}/agent"
        self.tool = f"DataAgent_{agent_name}"
        self._token = token
        self._http = http or httpx.Client(timeout=240)
        self._ids = itertools.count(1)

    def ask(self, question: str) -> str:
        result = self._rpc("tools/call", {"name": self.tool, "arguments": {"userQuestion": question}})
        text = "\n\n".join(c["text"] for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise AgentError(text or "the data agent returned an error")
        return text

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        r = self._http.post(self.url, json={"jsonrpc": "2.0", "id": next(self._ids),
                                            "method": method, "params": params},
                            headers={"Authorization": f"Bearer {self._token(SCOPE)}",
                                     "Accept": "application/json, text/event-stream"})
        if r.status_code >= 400:
            raise AgentError(f"data agent HTTP {r.status_code}: {r.text[:500]}")
        body = r.json()
        if "error" in body:
            raise AgentError(body["error"].get("message", str(body["error"])))
        return body["result"]
