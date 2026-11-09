"""Synthetic demo storyline, written as raw OHGO / NWS API responses.

Everything here is CLEARLY SYNTHETIC: ids start with "SYN-", descriptions
start with "[SYNTHETIC DEMO]", the manifest says synthetic=true, and replay
tags every event run_mode="replay". Envelopes follow the documented OHGO
swagger and NWS CAP/GeoJSON schemas so the real parsers process them.

Storyline (recorded minutes; play at --speed 4 for a ~9 minute demo):

  t+0   baseline: two construction zones, one minor disabled vehicle
  t+3   NWS Severe Thunderstorm Warning covers downtown Dayton / Vandalia / Tipp City
  t+5   crash on I-75 NB beside Sinclair Conference Center (downtown), partially closed
  t+7   dangerous slowdown builds behind the crash
  t+9   I-75 crash escalates to full closure  -> Sinclair exposure > 70, alert raised
  t+10  travel delay on I-75 NB (22 min)
  t+12  crash with closure on US-40 EB near Dayton International Airport -> alert
  t+20  I-75 reopens to one lane (partially closed); slowdown eases
  t+24  thunderstorm warning expires
  t+26  I-75 crash cleared (resolves after missed polls)
  t+30  US-40 crash cleared; slowdowns and delays gone
  t+36  end — back to baseline, alerts cleared
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mvoi.replay.recording import RecordingWriter

TAG = "[SYNTHETIC DEMO] "

# Severe Thunderstorm Warning polygon (lon, lat) — downtown Dayton (Sinclair) to Tipp City.
STORM_POLYGON = [[-84.33, 39.72], [-84.05, 39.72], [-84.05, 40.00], [-84.33, 40.00], [-84.33, 39.72]]


def _ohgo_envelope(resource: str, results: list[dict[str, Any]], at: datetime) -> dict[str, Any]:
    return {
        "links": [{"href": f"https://publicapi.ohgo.com/api/v1/{resource}", "rel": "self"}],
        "lastUpdated": at.isoformat().replace("+00:00", "Z"),
        "accecptedFilters": [], "rejectedFilters": [],
        "totalPageCount": 1, "totalResultCount": len(results), "currentResultCount": len(results),
        "results": results,
    }


def _incident(id_: str, lat: float, lon: float, route: str, direction: str, category: str,
              road_status: str, desc: str, location: str) -> dict[str, Any]:
    return {"links": [], "id": id_, "latitude": lat, "longitude": lon, "location": location,
            "description": TAG + desc, "category": category, "direction": direction,
            "routeName": route, "roadStatus": road_status, "roadClosureDetails": None}


def _construction(id_: str, lat: float, lon: float, route: str, status: str, desc: str,
                  start: datetime, end: datetime) -> dict[str, Any]:
    return {"links": [], "id": id_, "latitude": lat, "longitude": lon, "location": route,
            "description": TAG + desc, "category": "Planned", "direction": "Both",
            "district": "07", "routeName": route, "status": status,
            "startDate": start.isoformat().replace("+00:00", "Z"),
            "endDate": end.isoformat().replace("+00:00", "Z"),
            "workZoneProjectId": None, "workZones": None, "detours": None}


def _slowdown(id_: str, lat: float, lon: float, route: str, direction: str, normal: int,
              current: int) -> dict[str, Any]:
    return {"links": [], "id": id_, "latitude": lat, "longitude": lon,
            "location": f"{route} {direction}", "description": TAG + "Traffic slowing quickly ahead",
            "normalMPH": normal, "currentMPH": current, "routeName": route, "direction": direction}


def _delay(id_: str, lat: float, lon: float, route: str, direction: str, delay: float) -> dict[str, Any]:
    return {"links": [], "id": id_, "latitude": lat, "longitude": lon,
            "location": f"{route} {direction}", "description": TAG + f"{delay:.0f} minute delay",
            "direction": direction, "routeName": route, "travelTime": 9 + delay, "delayTime": delay,
            "startMileMarker": 54.0, "endMileMarker": 61.0, "currentAvgSpeed": 21.0,
            "normalAvgSpeed": 65.0}


def _nws_fc(features: list[dict[str, Any]], at: datetime) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features, "title": "Current watches, warnings, "
            "and advisories for Ohio", "updated": at.isoformat().replace("+00:00", "Z")}


def _storm(at0: datetime) -> dict[str, Any]:
    iso = lambda m: (at0 + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")  # noqa: E731
    return {
        "id": "https://api.weather.gov/alerts/urn:oid:SYN-2.49.0.1.840.0.demo.001.1",
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [STORM_POLYGON]},
        "properties": {
            "id": "urn:oid:SYN-2.49.0.1.840.0.demo.001.1",
            "areaDesc": "Miami, OH; Montgomery, OH",
            "geocode": {"SAME": ["039109", "039113"], "UGC": ["OHC109", "OHC113"]},
            "affectedZones": ["https://api.weather.gov/zones/county/OHC109",
                              "https://api.weather.gov/zones/county/OHC113"],
            "references": [], "sent": iso(3), "effective": iso(3), "onset": iso(3),
            "expires": iso(24), "ends": iso(24),
            "status": "Actual", "messageType": "Alert", "category": "Met",
            "severity": "Severe", "certainty": "Observed", "urgency": "Immediate",
            "event": "Severe Thunderstorm Warning", "senderName": "NWS Wilmington OH",
            "headline": TAG + "Severe Thunderstorm Warning for Miami and Montgomery counties",
            "description": TAG + "60 mph wind gusts and quarter size hail.",
            "instruction": TAG + "Move to an interior room on the lowest floor of a building.",
            "response": "Shelter",
        },
    }


def build_storyline(start: datetime | None = None) -> list[tuple[datetime, str, str, dict[str, Any]]]:
    """Returns frames (t, source, resource, envelope)."""
    t0 = (start or datetime(2026, 1, 15, 14, 0, tzinfo=UTC)).astimezone(UTC)
    frames = []
    cons = [
        _construction("SYN-C-001", 39.71141, -84.22757, "I-75", "Restricted",
                      "Lane restriction, bridge deck repair",
                      t0 - timedelta(days=3), t0 + timedelta(days=20)),
        _construction("SYN-C-002", 39.66422, -83.95218, "US-35", "Closed",
                      "Ramp closed for resurfacing", t0 - timedelta(days=1), t0 + timedelta(days=5)),
    ]
    disabled = _incident("SYN-I-001", 39.93051, -83.78912, "SR-4", "NB", "Disabled Vehicle", "Open",
                         "Disabled vehicle on shoulder", "SR-4 at Bechtle Ave")

    def i75(status: str) -> dict[str, Any]:
        return _incident("SYN-I-075", 39.75881, -84.20133, "I-75", "NB", "Crash", status,
                         "Multi-vehicle crash", "I-75 NB in downtown Dayton near Sinclair College")

    us40 = _incident("SYN-I-040", 39.88867, -84.22415, "US-40", "EB", "Crash", "Closed",
                     "Tractor-trailer crash, all lanes blocked", "US-40 EB near Dayton International Airport")

    for m in range(0, 37):
        at = t0 + timedelta(minutes=m)
        incidents = [disabled] if m < 18 else []
        if 5 <= m < 9:
            incidents.append(i75("Partially Closed"))
        elif 9 <= m < 20:
            incidents.append(i75("Closed"))
        elif 20 <= m < 26:
            incidents.append(i75("Partially Closed"))
        if 12 <= m < 30:
            incidents.append(us40)

        slowdowns = []
        if 7 <= m < 20:
            mph = 10 if m >= 9 else 25
            slowdowns.append(_slowdown("SYN-S-075", 39.75181, -84.20501, "I-75", "NB", 65, mph))
        elif 20 <= m < 26:
            slowdowns.append(_slowdown("SYN-S-075", 39.75181, -84.20501, "I-75", "NB", 65, 40))
        if 13 <= m < 30:
            slowdowns.append(_slowdown("SYN-S-040", 39.88776, -84.23509, "US-40", "EB", 55, 8))

        delays = [_delay("SYN-D-075", 39.74387, -84.20487, "I-75", "NB", 22)] if 10 <= m < 24 else []

        for resource, results in (("incidents", incidents), ("construction", cons),
                                  ("dangerous-slowdowns", slowdowns), ("travel-delays", delays)):
            frames.append((at, "ohgo", resource, _ohgo_envelope(resource, results, at)))
        if m % 2 == 1 or m == 0:
            wx = [_storm(t0)] if 3 <= m < 24 else []
            frames.append((at, "nws", "alerts", _nws_fc(wx, at)))
    return frames


def write_storyline(path: str | Path, start: datetime | None = None) -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    w = RecordingWriter(path, name="miami-valley-storm-and-crashes",
                        description="Synthetic 36-minute storyline: thunderstorm warning, I-75 and US-40 "
                                    "crashes with closures near monitored sites.",
                        synthetic=True)
    for at, source, resource, env in build_storyline(start):
        w.record(source, resource, at, env)
    w.close()
    return path
