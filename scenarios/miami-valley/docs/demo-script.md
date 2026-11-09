# 10-minute customer demonstration

**Audience:** IT directors, data-engineering leaders, architects.
**Message:** One Fabric platform covers streaming ingestion, real-time
analytics, alerting, a governed lakehouse, geospatial analytics and BI, built
as code and repeatable.

## Before the meeting (T−15 min)

1. Capacity running; open in browser tabs: `rtd_mvoi_operations`, `da_mvoi`, the
   `Miami Valley Ops Intelligence` workspace folder, `nb_mvoi_geospatial`
   (last run output), the Power BI report, and the Activator item.
2. Reset any previous demo session, then confirm the poller is healthy:
   ```bash
   uv run mvoi replay reset --session-id <last-session>     # if one exists
   ```
3. Set the dashboard **Data** parameter to *Live*. Check *Minutes since last
   ingestion* < 2.
4. Have a terminal ready (do not start yet):
   ```bash
   uv run mvoi replay start --recording replay/recordings/synthetic-storm-and-crashes.jsonl --speed 4
   ```
   The 36-minute storyline plays in about 9 minutes at speed 4.

## Script

| Time | Show | Say |
|---|---|---|
| 0:00 | Workspace folder tree | "Everything here was deployed by one script from Git: lakehouse, Eventhouse, Eventstream, notebooks, pipeline, dashboard, semantic model, alerts. Folders and names follow our standards." |
| 0:45 | Dashboard, **Live** | "Live ODOT incidents, construction and NWS warnings for the Miami Valley, polled every minute. Freshness is in the corner. The purple points are public sites around us, starting with this building, Sinclair Conference Center. We're not affiliated with any of them; we score travel disruption near them." |
| 1:30 | Terminal: start replay; dashboard Data = **Demo replay** | "Live roads may be quiet right now, so we'll replay a recorded storyline through the same pipeline: same API parsing, same Eventstream, same Eventhouse. It is tagged as demo data and never reaches our analytics or pages anyone." |
| 2:30 | Map + weather table (storm warning arrives) | "NWS issues a Severe Thunderstorm Warning over downtown, including Sinclair and the airport. Weather adds to each site's exposure, and the explanation says exactly why." |
| 3:30 | Incidents table + map (I-75 crash, then closure) | "A crash on I-75 northbound, right outside this building, then a full closure. The slowdown and travel delay behind it arrive from ODOT's sensors." |
| 4:30 | Exposure table: Sinclair Conference Center > 85; Open alerts | "Sinclair's disruption exposure crosses 70: inferred from proximity, the closure and the storm, scaled by site importance. Downtown neighbours follow. We say *inferred*: a nearby crash is not proof anyone's late. Each condition raises one alert, not one per minute." |
| 5:30 | US-40 crash near Dayton International Airport | "A second corridor, by the airport — anyone flying in for today would feel this. Each site's score and explanation update independently." |
| 6:15 | Pipeline-health page | "Every minute there is a heartbeat: source latency, rejected records, outbox depth. If ingestion stops, Activator alerts on the silence itself." |
| 7:00 | Notebook `nb_mvoi_geospatial` output map | "Every hour, Spark enriches the history with a built-in Python engine, and ArcGIS GeoAnalytics can be switched on with an Esri licence: site proximity, weather polygon overlays, corridor matching, and Getis-Ord hot spots. We count distinct incidents, not polls, so a four-hour closure doesn't look like 240 crashes." |
| 8:00 | Power BI report (Direct Lake) | "The same Gold tables feed Power BI with no import refresh: corridor disruption, hour-of-day patterns, weather-related share, site exposure history, data quality." |
| 8:30 | Data agent `da_mvoi` (replay still running) | Ask: *"During the demo replay, which sites are exposed?"* then *"Which corridor had the most closure hours this month?"* "Same governed data, in plain English. It picked the live Eventhouse for the first question and the Power BI model for the second, using the functions and measures we already built." |
| 9:00 | Back to dashboard: storyline resolving; alerts clear | "Incidents clear, the warning expires, scores fall, alerts clear. That's the whole lifecycle, end to end." |
| 9:30 | Close | "Built on public data in days. The same pattern applies to fleet telemetry, IoT, supply chain or field service." |

## If something goes wrong

| Symptom | Recovery |
|---|---|
| Replay tiles empty | Data parameter = *Demo replay*; the dashboard shows the **latest** session only. Wait ~30 s for Eventstream ProcessedIngestion. |
| Need to restart | Ctrl-C the replay, `mvoi replay reset --session-id <id>` (it resolves everything so dashboards clear), start again. |
| Network flaky | `--speed 8` shortens the story; the outbox buffers and catches up when connectivity returns. |
| Data agent slow or throttled | Skip it. The dashboard and report make the same point. Scale the capacity up before the demo, because agent questions use Copilot CUs. |
| Live tab looks stale | Talk to the freshness tile ("this is exactly what the staleness alert is for"), switch to replay. |
