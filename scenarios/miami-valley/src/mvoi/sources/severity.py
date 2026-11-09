"""Severity normalisation.

Neither OHGO resource carries a severity field, so severity is *derived* from
documented fields with transparent rules. NWS alerts carry CAP `severity`,
which maps directly. Rules are deliberately simple so they can be explained
in a customer demo; tune them here and in docs/data-sources.md together.
"""

from __future__ import annotations

import re

from mvoi.models.events import Severity

_CRITICAL_WORDS = re.compile(r"\b(fatal|fatality|hazmat|hazardous material|wrong[- ]way|"
                             r"bridge (strike|collapse)|tanker)\b", re.I)
_CRASH_WORDS = re.compile(r"\b(crash|accident|collision|rollover|disabled|debris|"
                          r"vehicle fire|injur)", re.I)


def road_status_is_closed(road_status: str | None) -> bool:
    return bool(road_status) and "closed" in road_status.lower() and "partial" not in road_status.lower()


def road_status_is_partial(road_status: str | None) -> bool:
    return bool(road_status) and "partial" in road_status.lower()


def incident_severity(category: str | None, road_status: str | None,
                      description: str | None) -> Severity:
    text = f"{category or ''} {description or ''}"
    if _CRITICAL_WORDS.search(text):
        return Severity.CRITICAL
    if road_status_is_closed(road_status):
        return Severity.MAJOR
    if road_status_is_partial(road_status) or _CRASH_WORDS.search(text):
        return Severity.MODERATE
    return Severity.MINOR


def construction_severity(status: str | None) -> Severity:
    s = (status or "").lower()
    if "closed" in s:
        return Severity.MODERATE
    return Severity.MINOR


def slowdown_severity(normal_mph: float | None, current_mph: float | None) -> Severity:
    if not normal_mph or current_mph is None or normal_mph <= 0:
        return Severity.MODERATE  # a dangerous slowdown is by definition notable
    drop = max(0.0, (normal_mph - current_mph) / normal_mph)
    if drop >= 0.6:
        return Severity.MAJOR
    if drop >= 0.4:
        return Severity.MODERATE
    return Severity.MINOR


def travel_delay_severity(delay_minutes: float | None) -> Severity:
    d = delay_minutes or 0
    if d >= 20:
        return Severity.MAJOR
    if d >= 10:
        return Severity.MODERATE
    return Severity.MINOR


_NWS_MAP = {
    "extreme": Severity.CRITICAL,
    "severe": Severity.MAJOR,
    "moderate": Severity.MODERATE,
    "minor": Severity.MINOR,
}


def nws_severity(cap_severity: str | None) -> Severity:
    return _NWS_MAP.get((cap_severity or "").lower(), Severity.MINOR)


def speed_band(normal_mph: float | None, current_mph: float | None) -> str:
    """Coarse band so a few mph of jitter is not an 'update'."""
    if not normal_mph or current_mph is None:
        return "unknown"
    ratio = current_mph / normal_mph
    for edge, label in ((0.25, "<25%"), (0.5, "25-50%"), (0.75, "50-75%")):
        if ratio < edge:
            return label
    return ">=75%"


def delay_band(delay_minutes: float | None) -> str:
    d = delay_minutes or 0
    for edge, label in ((5, "<5m"), (10, "5-10m"), (20, "10-20m"), (40, "20-40m")):
        if d < edge:
            return label
    return ">=40m"
