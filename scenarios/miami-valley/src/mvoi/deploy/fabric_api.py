"""Minimal Fabric REST / Kusto / OneLake client for scripts/deploy_fabric.py.

Auth: AzureCliCredential — i.e. whatever `az login` the shell has. In this
repo `.claude/settings.local.json` points AZURE_CONFIG_DIR at a dedicated
profile, so deployments use the demo tenant, not the default account.

Handles long-running operations (202 + Location polling), 429 Retry-After,
and continuation-token pagination.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

FABRIC = "https://api.fabric.microsoft.com"
STORAGE_SCOPE = "https://storage.azure.com/.default"
KUSTO_SCOPE = "https://kusto.kusto.windows.net/.default"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
POWERBI = "https://api.powerbi.com/v1.0/myorg"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


class FabricError(RuntimeError):
    pass


def b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def part(path: str, content: bytes | str) -> dict[str, str]:
    return {"path": path, "payload": b64(content), "payloadType": "InlineBase64"}


class FabricClient:
    def __init__(self, credential=None, timeout: float = 60):
        if credential is None:
            from azure.identity import AzureCliCredential
            credential = AzureCliCredential()
        self._cred = credential
        self._tokens: dict[str, tuple[str, float]] = {}
        self._http = httpx.Client(timeout=timeout)
        extra = os.environ.get("MVOI_FABRIC_EXTRA_HEADERS")  # "k=v;k2=v2"
        self._extra = dict(kv.split("=", 1) for kv in extra.split(";") if kv) if extra else {}

    # ------------------------------------------------------------------ auth

    def token(self, scope: str) -> str:
        tok, exp = self._tokens.get(scope, ("", 0))
        if time.time() > exp - 300:
            t = self._cred.get_token(scope)
            self._tokens[scope] = (t.token, t.expires_on)
            tok = t.token
        return tok

    def _headers(self, scope: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(scope)}", **self._extra}

    # ------------------------------------------------------------------ fabric

    def request(self, method: str, path: str, body: Any = None, *, lro: bool = True,
                params: dict | None = None) -> Any:
        url = path if path.startswith("http") else f"{FABRIC}/v1{path}"
        for attempt in range(6):
            r = self._http.request(method, url, json=body, params=params,
                                   headers=self._headers(f"{FABRIC}/.default"))
            if r.status_code == 429:
                delay = float(r.headers.get("Retry-After", 2 ** attempt))
                log.warning("throttled; sleeping %.0fs", delay)
                time.sleep(min(delay, 60))
                continue
            break
        if r.status_code == 202 and lro:
            return self._wait(r)
        if r.status_code >= 400:
            raise FabricError(f"{method} {url} -> {r.status_code}: {r.text[:800]}")
        return r.json() if r.content else None

    def _wait(self, r: httpx.Response, max_wait_s: int = 900) -> Any:
        loc = r.headers.get("Location")
        if not loc:
            return None
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            time.sleep(float(r.headers.get("Retry-After", 5)))
            r = self._http.get(loc, headers=self._headers(f"{FABRIC}/.default"))
            if r.status_code >= 400:
                raise FabricError(f"LRO poll {loc} -> {r.status_code}: {r.text[:500]}")
            body = r.json() if r.content else {}
            status = body.get("status")
            if status == "Succeeded":
                res = self._http.get(f"{loc}/result", headers=self._headers(f"{FABRIC}/.default"))
                return res.json() if res.status_code == 200 and res.content else body
            if status in ("Failed", "Cancelled"):
                raise FabricError(f"LRO {status}: {json.dumps(body.get('error'))[:800]}")
            if status is None and r.status_code == 200:
                return body  # some endpoints return the resource directly
        raise FabricError(f"LRO timed out after {max_wait_s}s: {loc}")

    def list(self, path: str) -> list[dict[str, Any]]:
        out, params = [], None
        while True:
            page = self.request("GET", path, params=params)
            out.extend(page.get("value", []))
            ct = page.get("continuationToken")
            if not ct:
                return out
            params = {"continuationToken": ct}

    # ------------------------------------------------------------------ helpers

    def workspace_id(self, name: str) -> str:
        for w in self.list("/workspaces"):
            if w["displayName"] == name:
                return w["id"]
        raise FabricError(f"workspace '{name}' not found (is `az login` on the right tenant?)")

    def find_item(self, ws: str, item_type: str, name: str) -> dict[str, Any] | None:
        for it in self.list(f"/workspaces/{ws}/items?type={item_type}"):
            if it["displayName"] == name:
                return it
        return None

    def ensure_folder(self, ws: str, name: str, parent: str | None = None) -> str:
        for f in self.list(f"/workspaces/{ws}/folders"):
            if f["displayName"] == name and f.get("parentFolderId") == parent:
                return f["id"]
        body = {"displayName": name}
        if parent:
            body["parentFolderId"] = parent
        return self.request("POST", f"/workspaces/{ws}/folders", body)["id"]

    def ensure_item(self, ws: str, item_type: str, name: str, *, folder: str | None = None,
                    definition: dict | None = None, creation_payload: dict | None = None,
                    description: str = "") -> tuple[dict[str, Any], bool]:
        """Create if missing; update definition if present. Returns (item, created)."""
        existing = self.find_item(ws, item_type, name)
        if existing:
            if definition:
                self.request("POST", f"/workspaces/{ws}/items/{existing['id']}/updateDefinition",
                             {"definition": definition})
            return existing, False
        body: dict[str, Any] = {"displayName": name, "type": item_type, "description": description}
        if folder:
            body["folderId"] = folder
        if definition:
            body["definition"] = definition
        if creation_payload:
            body["creationPayload"] = creation_payload
        self.request("POST", f"/workspaces/{ws}/items", body)
        item = self.find_item(ws, item_type, name)
        if item is None:
            raise FabricError(f"created {item_type} '{name}' but cannot find it")
        return item, True

    def get_definition(self, ws: str, item_id: str, fmt: str | None = None) -> dict[str, bytes]:
        path = f"/workspaces/{ws}/items/{item_id}/getDefinition"
        if fmt:
            path += f"?format={fmt}"
        res = self.request("POST", path, {})
        return {p["path"]: base64.b64decode(p["payload"]) for p in res["definition"]["parts"]}

    # ------------------------------------------------------------------ power bi

    def refresh_semantic_model(self, ws: str, model_id: str, *, max_wait_s: int = 900,
                               poll_s: float = 5) -> dict[str, Any]:
        """Full refresh (enhanced API) and wait. For Direct Lake this reframes onto Gold."""
        url = f"{POWERBI}/groups/{ws}/datasets/{model_id}/refreshes"
        r = self._http.post(url, json={"type": "full"}, headers=self._headers(POWERBI_SCOPE))
        if r.status_code != 202:
            raise FabricError(f"semantic model refresh -> {r.status_code}: {r.text[:500]}")
        loc = r.headers["Location"]
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            time.sleep(poll_s)
            r = self._http.get(loc, headers=self._headers(POWERBI_SCOPE))
            if r.status_code >= 400:
                raise FabricError(f"refresh poll {loc} -> {r.status_code}: {r.text[:500]}")
            body = r.json()
            if body.get("status") == "Completed":
                return body
            if body.get("status") in ("Failed", "Cancelled", "Disabled"):
                detail = body.get("messages") or body.get("serviceExceptionJson")
                raise FabricError(f"semantic model refresh {body['status']}: {json.dumps(detail)[:800]}")
        raise FabricError(f"semantic model refresh timed out after {max_wait_s}s")

    # ------------------------------------------------------------------ kusto

    def kusto_mgmt(self, query_uri: str, database: str, csl: str) -> dict[str, Any]:
        r = self._http.post(f"{query_uri.rstrip('/')}/v1/rest/mgmt",
                            json={"db": database, "csl": csl},
                            headers=self._headers(KUSTO_SCOPE), timeout=300)
        if r.status_code >= 400:
            raise FabricError(f"Kusto mgmt failed {r.status_code}: {r.text[:1500]}")
        return r.json()

    def kusto_query(self, query_uri: str, database: str, csl: str) -> list[dict[str, Any]]:
        r = self._http.post(f"{query_uri.rstrip('/')}/v1/rest/query",
                            json={"db": database, "csl": csl},
                            headers=self._headers(KUSTO_SCOPE), timeout=300)
        if r.status_code >= 400:
            raise FabricError(f"Kusto query failed {r.status_code}: {r.text[:1500]}")
        table = r.json()["Tables"][0]
        cols = [c["ColumnName"] for c in table["Columns"]]
        return [dict(zip(cols, row, strict=False)) for row in table["Rows"]]

    # ------------------------------------------------------------------ onelake

    def onelake_put(self, ws: str, item_id: str, rel_path: str, data: bytes) -> None:
        url = f"{ONELAKE}/{ws}/{item_id}/{rel_path.lstrip('/')}"
        h = self._headers(STORAGE_SCOPE)
        r = self._http.put(url, params={"resource": "file", "overwrite": "true"}, headers=h)
        if r.status_code >= 400:
            raise FabricError(f"OneLake create {rel_path}: {r.status_code} {r.text[:300]}")
        if data:
            r = self._http.patch(url, params={"action": "append", "position": 0}, content=data,
                                 headers={**h, "Content-Type": "application/octet-stream"})
            if r.status_code >= 400:
                raise FabricError(f"OneLake append {rel_path}: {r.status_code} {r.text[:300]}")
        r = self._http.patch(url, params={"action": "flush", "position": len(data)}, headers=h)
        if r.status_code >= 400:
            raise FabricError(f"OneLake flush {rel_path}: {r.status_code} {r.text[:300]}")
