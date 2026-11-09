"""Collapse lifecycle events into distinct incident *occurrences*.

The poller emits created/updated/resolved/reopened events per entity. Counting
those rows, or counting every poll, would let a long-running incident dominate
hotspot and trend statistics. An occurrence is one continuous active period
of one entity:

    created|reopened  ->  ... updated ...  ->  resolved   (or still open)

Each occurrence carries its duration, so analyses can choose between
"distinct occurrences" (default) and "incident-minutes" weighting.

Pure Python: called per entity_key from Spark `applyInPandas` and from tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

SEVERITY_RANK = {"minor": 1, "moderate": 2, "major": 3, "critical": 4}

OCCURRENCE_COLUMNS = [
    "occurrence_id", "entity_key", "entity_type", "occurrence_seq", "started_at", "ended_at",
    "is_open", "duration_minutes", "latitude", "longitude", "corridor_id", "route_name",
    "category", "peak_severity", "peak_severity_rank", "had_road_closure", "update_count",
    "resolution_reason",
]


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def build_occurrences(events: Iterable[Mapping[str, Any]], as_of: datetime) -> list[dict[str, Any]]:
    """`events`: all lifecycle events of ONE entity_key (any order, duplicates allowed)."""
    seen: set[str] = set()
    rows = []
    for e in events:
        eid = e.get("event_id")
        if eid in seen:
            continue
        seen.add(eid)
        rows.append(e)
    rows.sort(key=lambda e: (_ts(e.get("observed_at")) or as_of, e.get("change_type") == "resolved"))

    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    def close(end: datetime | None, reason: str | None, still_open: bool) -> None:
        nonlocal cur
        if cur is None:
            return
        stop = as_of if still_open else (end or cur["started_at"])
        cur["ended_at"] = None if still_open else stop
        cur["is_open"] = still_open
        cur["duration_minutes"] = round(max(0.0, (stop - cur["started_at"]).total_seconds() / 60), 2)
        cur["resolution_reason"] = reason
        out.append(cur)
        cur = None

    for e in rows:
        ct = e.get("change_type")
        t = _ts(e.get("observed_at"))
        if ct in ("created", "reopened") or (cur is None and ct == "updated"):
            if cur is not None:  # a new start without a resolve: close at last sighting
                close(cur["_last_seen"], "superseded", False)
            started = _ts(e.get("first_seen_at")) if ct == "created" else t
            cur = {
                "entity_key": e["entity_key"], "entity_type": e.get("entity_type"),
                "occurrence_seq": len(out) + 1, "started_at": started or t,
                "latitude": e.get("latitude"), "longitude": e.get("longitude"),
                "corridor_id": e.get("corridor_id"), "route_name": e.get("route_name"),
                "category": e.get("category"), "peak_severity": e.get("severity"),
                "peak_severity_rank": SEVERITY_RANK.get(e.get("severity") or "", 0),
                "had_road_closure": bool(e.get("is_road_closure")), "update_count": 0,
                "_last_seen": _ts(e.get("last_seen_at")) or t,
            }
        elif ct == "updated" and cur is not None:
            cur["update_count"] += 1
            rank = SEVERITY_RANK.get(e.get("severity") or "", 0)
            if rank > cur["peak_severity_rank"]:
                cur["peak_severity_rank"], cur["peak_severity"] = rank, e.get("severity")
            cur["had_road_closure"] = cur["had_road_closure"] or bool(e.get("is_road_closure"))
            cur["corridor_id"] = e.get("corridor_id") or cur["corridor_id"]
            cur["_last_seen"] = _ts(e.get("last_seen_at")) or t
        elif ct == "resolved" and cur is not None:
            reason = (e.get("attributes") or {}).get("resolution_reason") \
                if isinstance(e.get("attributes"), Mapping) else None
            close(_ts(e.get("last_seen_at")) or cur["_last_seen"], reason, False)
    if cur is not None:
        close(None, None, True)

    for o in out:
        o.pop("_last_seen", None)
        o["occurrence_id"] = f"{o['entity_key']}#{o['occurrence_seq']}"
    return [{c: o.get(c) for c in OCCURRENCE_COLUMNS} for o in out]
