Use the model's measures rather than counting or summing columns yourself:
- [Incident Occurrences], [Road Closure Occurrences], [Incident Hours],
  [Average Duration Minutes] for incident volume and duration
- [Closure Hours], [Median Incident Duration Minutes] for corridor disruption
- [Weather-Related Occurrences], [Weather-Related Share],
  [Sites Under Alert] for weather
- [Peak Exposure Score], [Average Exposure Score], [High Exposure Snapshots]
  for site disruption. Never COUNTROWS 'Site Disruption' as a number of
  incidents.
- [Nearby Incident Occurrences] for incidents near a site
- [Hot Spot Bins 95], [Occurrences in Hot Spots] for hot spots
- [Poll Success Rate], [DQ Pass Rate] for pipeline health and data quality

Slice by 'Date' for time, Corridor for highways and Site for sites.

For "than usual" questions, use [Usual Daily Occurrences], sliced by Corridor
if needed. It already covers the 28 days before today and divides by the
days the feed was running, so don't add a date filter or divide it yourself.
Say how many days it is based on with [Feed Days] over the same window. For
a per-day rate over another period, use [Occurrences per Feed Day].
This model contains live data only. Replay/demo data is never in it.
Data refreshes hourly, so it can lag the KQL database by up to an hour.
