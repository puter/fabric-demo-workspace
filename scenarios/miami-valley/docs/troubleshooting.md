# Troubleshooting

## Poller

| Symptom | Likely cause | Fix |
|---|---|---|
| `HTTP 401` from OHGO (not retried) | Missing or wrong key | Check `OHGO_API_KEY` / Key Vault `ohgo-api-key`; the header is `Authorization: APIKEY <key>` |
| `HTTP 429` in logs, heartbeat `attempts > 1` | Undocumented OHGO rate limit | Raise `MVOI_POLLING__INTERVAL_SECONDS` (e.g. 120). `Retry-After` is honoured automatically. |
| Warning `ohgo rejected filters` | Bounding-box parameter format disagrees with the API | See data-sources.md; flip the coordinate order in `bbox_params` and re-run the live test |
| Warning `suppressing resolution: result count dropped sharply` | Upstream returned a truncated list | Expected protection; incidents are not mass-resolved. After 15 min of low counts (`lifecycle.max_resolution_suppression_minutes`) the count is accepted as real and stale entities resolve. Persists past that → check OHGO status. |
| `publish failed; events retained in outbox` | Eventstream unreachable (expected every night while the capacity is suspended: `Name or service not known`) or bad connection string | Events stay in SQLite and are retried on a backoff (30 s doubling to 5 min, `publisher.retry_*`); polling continues and the poller stays healthy. `/metrics` → `outbox_depth`, `last_publish_ok`, `publish_retry_at`. Fix the secret if it persists in daytime; no data is lost. |
| `/healthz` 503 | No completed cycle in 5× the poll interval (the loop is stuck or crashing; publish failures alone don't cause it) | `az containerapp logs show -n ca-example-poller -g rg-example --follow` |
| NWS 403 | Missing / generic User-Agent | Set `MVOI_NWS__USER_AGENT` with a contact |
| Map or counts show incidents that cleared long ago, or miss ones the poller reports | Events lost between Eventstream and the Eventhouse (e.g. destination paused by the nightly suspend) | `ActiveSetDrift()`: `hidden_rows` are stale rows `LatestFor` already hides ([ADR 0005](../../../docs/adr/0005-heartbeat-active-set-reconciliation.md)); `missing_keys` have no row at all and need a backfill from the poller's state |
| Lost state volume | Container recreated without Azure Files | Safe: active entities are re-emitted as `created`; downstream takes the latest per entity |

## Fabric

| Symptom | Cause | Fix |
|---|---|---|
| `deploy_fabric.py`: workspace not found | Signed in to the wrong tenant | `AZURE_CONFIG_DIR=~/.azure-profiles/demo-fabric-workspace az login` |
| KQL command N failed | Schema drift / syntax | The script prints the failing command. Fix `fabric/eventhouse/kqldb_mvoi.kql` and re-run `--steps eventhouse` (idempotent). |
| Eventstream destination in error | Table schema mismatch | `Events` must exist **before** the Eventstream (run `eventhouse` first). It includes the ProcessedIngestion columns `EventProcessedUtcTime`, `PartitionId`, `EventEnqueuedUtcTime`. |
| `Events` empty but poller healthy | Eventhouse destination *Paused*, usually after the nightly capacity suspend | `caj-example-resume-stream` fixes this hourly. To fix it now: `uv run --extra fabric mvoi resume-stream`. If the job's logs show `workspace not found`, the poller identity is missing Contributor on the workspace (docs/deployment.md §3) |
| Dashboard replay tiles empty | Parameter on *Live*, or a newer session exists | Select *Demo replay*; only the latest session is shown |
| Notebook: `ModuleNotFoundError: mvoi` | `env_mvoi` not published or not attached | Re-run `--steps environment,notebooks`; publishing takes several minutes |
| Notebook: `fabric_runtime.json` missing | `files` step not run | `--steps files` |
| Geospatial uses `fallback-python` unexpectedly | ArcGIS tenant setting off or not authorised | Check the tenant setting and the Key Vault `arcgis-api-key` access of the pipeline owner; run with `engine="arcgis"` to see the error |
| Activator step refuses to run | Validation gate: no live heartbeat / alert rows yet | Run the poller first, or `--allow-no-alert-rows` |
| Data agent answers wrong, or says a function "is not available" | Its query generator only sees functions nested under the `Functions` group (`function_grouping`) of the KQL data source, with element descriptions; otherwise it falls back to raw `Events` | Redeploy `--steps dataagent`. See exactly what it ran with the Eventhouse query log (below) |
| Semantic model queries (or the data agent) fail with *Cannot find table* | Direct Lake model never refreshed after deploy | Redeploy `--steps semantic`, which refreshes it; if the refresh warns, run the pipeline first |
| Semantic model visuals error after a Gold change | Column renamed in Spark | `uv run python -m mvoi.deploy.semantic` and the Spark contract test catch this; redeploy `--steps semantic` |

## Useful KQL

The data agent's own queries, with failures (it queries as `AISkillWorkload`; needs database admin):

```kusto
.show queries
| where StartedOn > ago(1h) and Application == "AISkillWorkload"
| project StartedOn, State, FailureReason, Text
```

```kusto
IngestionFreshness()                 // per-source last success
DataQualitySummary("live", 24h)      // rejections, failed polls
Events | where ingested_at > ago(10m) | summarize count() by entity_type, change_type
EntityLatest | where run_mode == "replay" | summarize count() by replay_session_id
```
