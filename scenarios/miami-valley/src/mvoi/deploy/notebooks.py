"""Converts fabric/notebooks/*.py (percent format) into Fabric .ipynb definitions.

Source notebooks stay reviewable Python; workspace-specific bindings (default
lakehouse, environment) are injected at deploy time, so no workspace or item
ids are committed (CLAUDE.md hard rule).

Cell markers:  `# %%`, `# %% [markdown]`, `# %% tags=["parameters"]`.
`# {{BOOTSTRAP}}` is replaced by fabric/notebooks/_bootstrap.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_MARKER = re.compile(r"^# %%(?P<md> \[markdown\])?(?P<tags> tags=\[(?P<taglist>[^\]]*)\])?\s*$")


def parse_cells(source: str) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in source.splitlines():
        m = _MARKER.match(line)
        if m:
            if cur:
                cells.append(cur)
            tags = [t.strip().strip('"\'') for t in (m.group("taglist") or "").split(",") if t.strip()]
            cur = {"type": "markdown" if m.group("md") else "code", "tags": tags, "lines": []}
            continue
        if cur is None:
            continue  # preamble before the first marker is ignored
        cur["lines"].append(line)
    if cur:
        cells.append(cur)
    for c in cells:
        while c["lines"] and not c["lines"][-1].strip():
            c["lines"].pop()
        while c["lines"] and not c["lines"][0].strip():
            c["lines"].pop(0)
        if c["type"] == "markdown":
            c["lines"] = [ln[2:] if ln.startswith("# ") else ln.lstrip("#") for ln in c["lines"]]
    return [c for c in cells if c["lines"]]


def to_ipynb(source: str, bootstrap: str, *, lakehouse_id: str, lakehouse_name: str, workspace_id: str,
             environment_id: str | None = None) -> dict[str, Any]:
    cells = []
    for c in parse_cells(source):
        text = "\n".join(c["lines"])
        if text.strip() == "# {{BOOTSTRAP}}":
            text = bootstrap.strip()
        lines = text.split("\n")
        src = [ln + "\n" for ln in lines[:-1]] + [lines[-1]]
        if c["type"] == "markdown":
            cells.append({"cell_type": "markdown", "metadata": {}, "source": src})
        else:
            meta: dict[str, Any] = {}
            if c["tags"]:
                meta["tags"] = c["tags"]
            cells.append({"cell_type": "code", "metadata": meta, "source": src,
                          "execution_count": None, "outputs": []})
    deps: dict[str, Any] = {"lakehouse": {
        "default_lakehouse": lakehouse_id,
        "default_lakehouse_name": lakehouse_name,
        "default_lakehouse_workspace_id": workspace_id,
        "known_lakehouses": [{"id": lakehouse_id}],
    }}
    if environment_id:
        deps["environment"] = {"environmentId": environment_id, "workspaceId": workspace_id}
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "cells": cells,
        "metadata": {
            "kernel_info": {"name": "synapse_pyspark"},
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "language_info": {"name": "python"},
            "dependencies": deps,
        },
    }


def load_notebook_sources(folder: Path) -> tuple[str, dict[str, str]]:
    bootstrap = (folder / "_bootstrap.py").read_text(encoding="utf-8")
    sources = {p.stem: p.read_text(encoding="utf-8") for p in sorted(folder.glob("nb_*.py"))}
    return bootstrap, sources


def ipynb_bytes(nb: dict[str, Any]) -> bytes:
    return json.dumps(nb, indent=1).encode("utf-8")
