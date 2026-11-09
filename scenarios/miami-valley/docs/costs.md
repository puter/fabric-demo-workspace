# Cost model (estimates — verify in the Azure pricing calculator for your region)

| Component | Sizing | Approx. monthly (US list, pay-as-you-go) | Notes |
|---|---|---|---|
| Fabric capacity | F4 (recommended); F2 hot-path-only | F2 ≈ $263, F4 ≈ $526 | Reserved pricing is roughly 40% lower. Pause the capacity between demos to stop compute charges (OneLake storage still billed). |
| Container Apps (poller) | 1 replica, 0.25 vCPU / 0.5 GiB, always on | ≈ $10–20 | Monthly free grant covers part. A laptop or any VM also works. |
| Container Apps job (Eventstream resume) | 12 short runs/day, seconds each | ≈ $0 | Inside the monthly free grant. |
| Log Analytics | poller logs, 30-day retention | < $5 | ~100–300 MB/month of JSON logs |
| Storage (Azure Files, 1 GiB) | SQLite state | < $1 | |
| Key Vault | 3–4 secrets | < $1 | |
| ArcGIS GeoAnalytics | BYOL, Esri marketplace offer | per Esri offer | Billed by compute core-hours; `geoanalytics_fabric.auth_info()` shows `available_hours`. The fallback engine costs nothing extra. |
| OHGO / NWS APIs | ≈ 5,800 + 720 requests/day | $0 | Public APIs; be a good citizen (ETag, ≥ 60 s) |

## What consumes Fabric capacity (CU)

| Workload | Driver | How to keep it small |
|---|---|---|
| Eventhouse | Always-on while data arrives; minimum compute is kept warm | Small data volumes (a few thousand events/day). 30-day retention, 7-day hot cache. For an occasional demo, pause the poller and let Eventhouse suspend. |
| Eventstream | Throughput tier `Low`, 1-day retention | The default is enough (~10 events/min) |
| Spark | Hourly pipeline: 4 notebooks, about 3–8 minutes total including start-up | Starter pools. Gold is small. Lengthen the schedule (`medallion_schedule.interval_minutes`) for cost-sensitive environments. Spark start-up (30–90 s per session) is why the hot path does not use notebooks. |
| Activator | 2 rules, KQL source every 60 s | Raise `executionIntervalInSeconds` if needed |
| Real-Time Dashboard | Queries only while open (live updates ≥ 10 s) | Each tile is a small KQL function call |
| Power BI | Direct Lake, no scheduled refresh | Reads Delta directly; nothing to refresh |
| Data agent (`da_mvoi`) | LLM tokens per question: 100 / 10 / 400 CU s per 1,000 input / cached input / output tokens ([rates](https://learn.microsoft.com/en-us/fabric/fundamentals/data-agent-consumption)), summed over several model calls per question. The prompt carries instructions, selected schema, few-shots and conversation history. Generated KQL/DAX is billed to Eventhouse / the model (negligible here). Zero when idle. | Keep `fabric/data-agent/` instructions short, select only curated tables and functions, keep few-shots to ~10, start a new conversation per test, never call it from automation. Check per-question CU in the Capacity Metrics app after deploying. |

## Demo-only operation

For a customer demo you can skip live polling entirely: deploy the Fabric
items, run `mvoi replay start` from a laptop for 10 minutes, then pause the
capacity. Cost is then dominated by the hours the capacity is running.
