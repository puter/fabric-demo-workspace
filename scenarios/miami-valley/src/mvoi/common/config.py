"""Typed configuration loaded from YAML, with environment-variable overrides.

Precedence (highest first): MVOI_* environment variables, settings.yaml,
settings.example.yaml defaults. Secrets are resolved separately by
`mvoi.common.secrets` and never live in these models' YAML sources.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Source checkout by default; Fabric notebooks point this at
# /lakehouse/default/Files/mvoi/config (uploaded by scripts/deploy_fabric.py).
CONFIG_DIR = Path(os.environ.get("MVOI_CONFIG_DIR") or Path(__file__).resolve().parents[3] / "config")


# --------------------------------------------------------------------------- geography


class BoundingBox(BaseModel):
    north: float
    south: float
    west: float
    east: float

    @model_validator(mode="after")
    def _check(self) -> BoundingBox:
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError("bounding_box: require -90 <= south < north <= 90")
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("bounding_box: require -180 <= west < east <= 180")
        return self

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east


class MapCenter(BaseModel):
    latitude: float
    longitude: float
    zoom: int = 9


class NwsGeography(BaseModel):
    area: str = "OH"
    forecast_zones: dict[str, str] = Field(default_factory=dict)
    county_zones: dict[str, str] = Field(default_factory=dict)

    @property
    def all_zone_ids(self) -> set[str]:
        return set(self.forecast_zones) | set(self.county_zones)


class Corridor(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)


class Geography(BaseModel):
    region_name: str
    bounding_box: BoundingBox
    map_center: MapCenter
    nws: NwsGeography
    corridors: list[Corridor]
    corridor_snap_distance_m: float = 300


def load_geography(path: Path | None = None) -> Geography:
    return Geography.model_validate(_read_yaml(path or CONFIG_DIR / "geography.yaml"))


# --------------------------------------------------------------------------- risk


class DistanceBands(BaseModel):
    full: float
    max: float

    @model_validator(mode="after")
    def _check(self) -> DistanceBands:
        if not 0 <= self.full < self.max:
            raise ValueError("distance_bands_m: require 0 <= full < max")
        return self


class SeverityPoints(BaseModel):
    points_by_severity: dict[str, float]
    cap: float


class FlatPoints(BaseModel):
    points: float
    cap: float


class HistoryConfig(BaseModel):
    radius_m: float
    window_days: int
    points_per_occurrence: float
    cap: float


class AlertingConfig(BaseModel):
    raise_threshold: float = 70
    clear_threshold: float = 60
    min_reraise_minutes: int = 30

    @model_validator(mode="after")
    def _check(self) -> AlertingConfig:
        if self.clear_threshold > self.raise_threshold:
            raise ValueError("alerting: clear_threshold must be <= raise_threshold")
        return self


class RiskConfig(BaseModel):
    version: int
    distance_bands_m: DistanceBands
    incident: SeverityPoints
    road_closure: FlatPoints
    slowdown: FlatPoints
    weather: SeverityPoints
    history: HistoryConfig
    criticality_multiplier: dict[int, float]
    bands: dict[str, float]
    alerting: AlertingConfig

    def band_for(self, score: float) -> str:
        band = "low"
        for name, lower in sorted(self.bands.items(), key=lambda kv: kv[1]):
            if score >= lower:
                band = name
        return band


def load_risk_config(path: Path | None = None) -> RiskConfig:
    return RiskConfig.model_validate(_read_yaml(path or CONFIG_DIR / "risk-scoring.yaml"))


# --------------------------------------------------------------------------- service settings


class PollingSettings(BaseModel):
    interval_seconds: int = 60
    nws_interval_seconds: int = 120
    http_timeout_seconds: float = 20
    max_retries: int = 4
    backoff_base_seconds: float = 2
    backoff_max_seconds: float = 60

    @field_validator("interval_seconds", "nws_interval_seconds")
    @classmethod
    def _min_interval(cls, v: int) -> int:
        # Floor protects the public APIs from accidental hammering.
        if v < 30:
            raise ValueError("poll intervals below 30s are not permitted")
        return v


OhgoResource = Literal["incidents", "construction", "dangerous-slowdowns", "travel-delays"]


class OhgoSettings(BaseModel):
    base_url: str = "https://publicapi.ohgo.com"
    resources: list[OhgoResource] = ["incidents", "construction", "dangerous-slowdowns", "travel-delays"]
    use_etag: bool = True


class NwsSettings(BaseModel):
    base_url: str = "https://api.weather.gov"
    user_agent: str = "MiamiValleyOpsIntelligence/0.1"


class LifecycleSettings(BaseModel):
    resolve_after_missed_polls: int = 3
    resolve_after_minutes: float = 5
    min_result_ratio_for_resolution: float = 0.5
    max_resolution_suppression_minutes: float = 15


class StateSettings(BaseModel):
    path: str = ".state/mvoi_state.sqlite"


class PublisherSettings(BaseModel):
    kind: Literal["stdout", "file", "eventhub"] = "stdout"
    file_path: str = ".state/events.jsonl"
    batch_max_events: int = 200
    # Outbox retry backoff while publishing fails (doubles from initial up to max).
    retry_initial_seconds: float = 30
    retry_max_seconds: float = 300


class RiskSettings(BaseModel):
    enabled: bool = True
    emit_every_cycle: bool = False
    min_score_delta: float = 5
    history_baseline_path: str | None = None


class KeyVaultSettings(BaseModel):
    url: str | None = None


class HealthSettings(BaseModel):
    port: int = 8080


class Settings(BaseModel):
    environment: str = "dev"
    run_mode: Literal["live", "replay"] = "live"
    polling: PollingSettings = PollingSettings()
    ohgo: OhgoSettings = OhgoSettings()
    nws: NwsSettings = NwsSettings()
    lifecycle: LifecycleSettings = LifecycleSettings()
    state: StateSettings = StateSettings()
    publisher: PublisherSettings = PublisherSettings()
    risk: RiskSettings = RiskSettings()
    key_vault: KeyVaultSettings = KeyVaultSettings()
    health: HealthSettings = HealthSettings()


# Flat env overrides: MVOI_<SECTION>__<KEY>=value, e.g. MVOI_PUBLISHER__KIND=eventhub.
ENV_PREFIX = "MVOI_"


def load_settings(path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    if path is None:
        candidate = Path(env.get("MVOI_SETTINGS_FILE", CONFIG_DIR / "settings.yaml"))
        path = candidate if candidate.exists() else CONFIG_DIR / "settings.example.yaml"
    data = _read_yaml(path) if path.exists() else {}
    for key, value in env.items():
        if not key.startswith(ENV_PREFIX) or key == "MVOI_SETTINGS_FILE":
            continue
        parts = key[len(ENV_PREFIX) :].lower().split("__")
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                break
        else:
            node[parts[-1]] = yaml.safe_load(value) if value else value
    return Settings.model_validate(data)


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
