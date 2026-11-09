# Shared first cell, inlined into every notebook by scripts/deploy_fabric.py.
# Points the mvoi package (installed from env_mvoi) at the config and reference
# data that deploy_fabric.py uploads to the default lakehouse, and loads the
# workspace-specific runtime values (no ids are committed to the repo).
import json
import os

MVOI_FILES = "/lakehouse/default/Files/mvoi"
os.environ["MVOI_CONFIG_DIR"] = f"{MVOI_FILES}/config"
os.environ["MVOI_REFERENCE_DIR"] = f"{MVOI_FILES}/reference-data"
with open(f"{MVOI_FILES}/config/fabric_runtime.json", encoding="utf-8") as _fh:
    RUNTIME = json.load(_fh)

import mvoi  # noqa: E402,F401  — fails fast if env_mvoi is not attached
from mvoi.lakehouse import spark_jobs as sj  # noqa: E402

TABLES = sj.Tables(RUNTIME.get("table_prefix", ""))
