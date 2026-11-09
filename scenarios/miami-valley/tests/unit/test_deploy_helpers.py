import ast
import json
import re
from pathlib import Path

import httpx
import pytest

from mvoi.deploy import kql, notebooks
from mvoi.deploy.fabric_api import FabricClient, FabricError
from mvoi.geo.reference import REFERENCE_DIR

ROOT = Path(__file__).parents[2]
KQL = ROOT / "fabric" / "eventhouse" / "kqldb_mvoi.kql"
NB = ROOT / "fabric" / "notebooks"


def test_kql_split_into_commands():
    cmds = kql.schema_commands(KQL)
    assert all(c.startswith(".") for c in cmds)
    assert not any("//" in c.split("\n")[0] for c in cmds)
    kinds = [c.split()[0] for c in cmds]
    assert kinds.count(".create-or-alter") >= 20  # functions + mapping
    mv = [c for c in cmds if "materialized-view" in c][0]
    assert "arg_max(observed_at, *)" in mv and mv.rstrip().endswith("}")
    mapping = [c for c in cmds if "ingestion json mapping" in c][0]
    body = mapping.split("```")[1]
    cols = [m["column"] for m in json.loads(body)]
    assert "event_id" in cols and "raw" in cols


def test_kql_functions_have_balanced_braces():
    for c in kql.schema_commands(KQL):
        if "function" in c.split("\n")[0]:
            assert c.count("{") == c.count("}"), c[:80]
            assert c.count("(") == c.count(")"), c[:80]


def test_kql_real_parameter_defaults_are_real_literals():
    # `x: real = 8000` is rejected by Kusto (int default for a real parameter).
    # The schema script stops there, after Facilities was dropped but before it
    # was reloaded, which left the table empty.
    bad = re.findall(r"\w+:\s*real\s*=\s*-?\d+(?![.\d])", KQL.read_text())
    assert not bad, bad


def test_reference_commands_escape_and_cover_all_rows():
    fac, zones, cors = kql.reference_commands(REFERENCE_DIR)
    assert fac.count("'FAC-") == 20
    assert "datatable(" in zones and "dynamic(" in zones
    # Quotes inside values are escaped
    assert kql._q("O'Hare") == "'O\\'Hare'"


def test_notebooks_convert_and_compile():
    bootstrap, sources = notebooks.load_notebook_sources(NB)
    assert set(sources) == {"nb_mvoi_bronze_ingest", "nb_mvoi_silver_build", "nb_mvoi_gold_build",
                            "nb_mvoi_geospatial"}
    for name, src in sources.items():
        nb = notebooks.to_ipynb(src, bootstrap, lakehouse_id="L", lakehouse_name="lh_mvoi",
                                workspace_id="W", environment_id="E")
        code = [c for c in nb["cells"] if c["cell_type"] == "code"]
        assert any("MVOI_CONFIG_DIR" in "".join(c["source"]) for c in code), name
        assert nb["metadata"]["dependencies"]["lakehouse"]["default_lakehouse_name"] == "lh_mvoi"
        for c in code:
            text = "".join(c["source"])
            ast.parse(text)  # valid Python (no magics used)
        assert nb["cells"][0]["cell_type"] == "markdown"


def test_parameters_cell_tagged():
    src = (NB / "nb_mvoi_geospatial.py").read_text()
    cells = notebooks.parse_cells(src)
    params = [c for c in cells if "parameters" in c["tags"]]
    assert len(params) == 1 and any("engine" in ln for ln in params[0]["lines"])


def test_no_ids_committed_in_fabric_sources():
    guid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
    for p in (ROOT / "fabric").rglob("*"):
        if p.is_file() and p.suffix in {".py", ".kql", ".json", ".tmdl", ".yaml"}:
            assert not guid.search(p.read_text(encoding="utf-8")), p


class _Cred:
    def get_token(self, scope):
        return type("T", (), {"token": "t", "expires_on": 4_102_444_800})()


def _refresh_client(statuses):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        if req.method == "POST":
            return httpx.Response(202, headers={"Location": "https://api.powerbi.com/refresh/1"})
        return httpx.Response(200, json={"status": statuses.pop(0)})

    c = FabricClient(credential=_Cred())
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c, seen


def test_semantic_refresh_waits_for_completion():
    c, seen = _refresh_client(["Unknown", "Completed"])
    assert c.refresh_semantic_model("ws", "sm", poll_s=0)["status"] == "Completed"
    assert seen[0] == ("POST", "/v1.0/myorg/groups/ws/datasets/sm/refreshes") and len(seen) == 3


def test_semantic_refresh_failure_raises():
    c, _ = _refresh_client(["Failed"])
    with pytest.raises(FabricError, match="Failed"):
        c.refresh_semantic_model("ws", "sm", poll_s=0)
