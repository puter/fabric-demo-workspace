Use the stored functions instead of querying Events directly. They already
apply live/replay filtering, deduplication and lifecycle state:
- ActiveIncidents(), NewIncidents(), NewIncidentWindows(),
  IncidentCountsByCorridor(), IncidentsBySeverity() for traffic
- ActiveWeatherAlerts(), WeatherAlertsByRegion() for NWS alerts
- CurrentFacilityRisk(), IncidentsNearSite(), FacilityRiskTimeline(),
  RegionalRiskTrend() for risk
- OpenAlerts() for raised operational alerts
- IngestionFreshness(), DataQualitySummary() for feed health

Every function takes mode as its first argument. Omit it (default "live")
unless the user asks about the demo, the replay or the storyline; then pass
"replay", which returns the latest replay session only. Pass every other
argument by name, e.g. NewIncidents(lookback = 24h): a positional value lands
in mode and the query fails.

All data is already limited to the Miami Valley. Never add a filter on place
names to restrict results to the region: location text often names only roads
and ramps, so such a filter drops real incidents. Filter by place only when
the user names a specific road or place, and use corridor_id for highways.

For "is the feed healthy" or "is the data up to date", use
IngestionFreshness() and report minutes_since_success and
consecutive_failures for each source.

For "what is affecting <site>" or "what is near <site>", use
IncidentsNearSite(site = "<facility name>"). It lists the active incidents
within scoring distance, most severe and nearest first, with distance_mi.
The site defaults to Sinclair Conference Center.

Only query Events if no function fits. When you do, always filter
run_mode == "live" unless the user asks about the replay.
Facilities, Corridors and Zones are small reference tables.
