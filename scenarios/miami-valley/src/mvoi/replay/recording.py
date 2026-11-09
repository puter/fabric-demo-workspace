"""Recording format for replay.

A recording is a JSON Lines file (optionally .gz) of *raw source responses*:

    {"t": "2026-10-09T18:00:00Z", "source": "ohgo", "resource": "incidents",
     "envelope": {...exact API response...}}

preceded by one manifest line:

    {"manifest": {"name": ..., "description": ..., "synthetic": true|false,
                  "captured_from": ..., "captured_to": ..., "schema": 1}}

Recording raw responses (not normalised events) means replay exercises the
real normalisation, lifecycle, risk and alert code, not a canned output.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

RECORDING_SCHEMA = 1


@dataclass
class Frame:
    t: datetime
    source: str
    resource: str
    envelope: dict[str, Any]


@dataclass
class Manifest:
    name: str
    description: str
    synthetic: bool
    captured_from: str | None = None
    captured_to: str | None = None
    schema: int = RECORDING_SCHEMA


def _open(path: Path, mode: str) -> IO[str]:
    if path.suffix == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")  # type: ignore[return-value]
    return open(path, mode, encoding="utf-8")  # noqa: SIM115


def _parse_t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class RecordingWriter:
    """Implements the `Recorder` protocol; attach to live adapters with --record."""

    def __init__(self, path: str | Path, name: str, description: str = "", synthetic: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self._fh = _open(self.path, "a")
        if new:
            m = Manifest(name=name, description=description, synthetic=synthetic)
            self._fh.write(json.dumps({"manifest": m.__dict__}) + "\n")

    def record(self, source: str, resource: str, observed_at: datetime,
               envelope: dict[str, Any] | None) -> None:
        if envelope is None:
            return
        self._fh.write(json.dumps({"t": observed_at.isoformat().replace("+00:00", "Z"),
                                   "source": source, "resource": resource,
                                   "envelope": envelope}, separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def read_recording(path: str | Path) -> tuple[Manifest, list[Frame]]:
    path = Path(path)
    manifest: Manifest | None = None
    frames: list[Frame] = []
    with _open(path, "r") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            if "manifest" in rec:
                manifest = Manifest(**rec["manifest"])
                continue
            frames.append(Frame(_parse_t(rec["t"]), rec["source"], rec["resource"], rec["envelope"]))
    if manifest is None:
        raise ValueError(f"{path}: missing manifest line")
    if manifest.schema != RECORDING_SCHEMA:
        raise ValueError(f"{path}: unsupported recording schema {manifest.schema}")
    frames.sort(key=lambda f: f.t)
    return manifest, frames


def group_frames(frames: list[Frame]) -> Iterator[tuple[datetime, list[Frame]]]:
    """Frames observed in the same poll cycle (same timestamp) are processed together."""
    current: list[Frame] = []
    for f in frames:
        if current and f.t != current[0].t:
            yield current[0].t, current
            current = []
        current.append(f)
    if current:
        yield current[0].t, current
