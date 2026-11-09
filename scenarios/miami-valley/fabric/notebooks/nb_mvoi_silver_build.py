# %% [markdown]
# # Silver — validate, deduplicate, conform
#
# * live rows only (replay/demo data never reaches Silver or Gold)
# * de-duplicated on the deterministic `event_id` (MERGE — re-runs are no-ops)
# * typed, UTC timestamps, data-quality rules from `mvoi.lakehouse.dq`
#   - `error` failures ➜ `silver_events_rejected`
#   - `warn` failures kept and listed in `dq_warnings`
# * per-run rule results ➜ `silver_dq_results`
# * latest state per entity ➜ `silver_entity_state`

# %%
# {{BOOTSTRAP}}

# %%
from mvoi.common.config import load_geography

result = sj.silver_build(spark, load_geography(), TABLES)
summary = sj.run_summary(step="silver", **result)
print(summary)

# %%
display(spark.table(TABLES.silver_dq_results)  # noqa: F821
        .where(f"run_id = '{result['run_id']}'").orderBy("severity", "rule"))

# %%
notebookutils.notebook.exit(summary)  # noqa: F821
