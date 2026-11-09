"""Command-line entry point.

  mvoi run [--once] [--record FILE]          live polling service
  mvoi replay start --recording FILE [--speed N] [--session-id ID]
  mvoi replay reset --session-id ID
  mvoi replay synthetic --out FILE            write the synthetic demo storyline
  mvoi capture --out DIR                      save one live response per resource (fixtures)
  mvoi resume-stream [--wait-minutes N]       resume Eventstream destinations paused by a capacity suspend
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from datetime import timedelta
from pathlib import Path

import httpx

from mvoi.common import logging as mlog
from mvoi.common.config import CONFIG_DIR, Settings, load_geography, load_risk_config, load_settings
from mvoi.common.http import ResilientClient
from mvoi.common.secrets import SecretResolver
from mvoi.geo.reference import CorridorMatcher, ZoneIndex, load_facilities
from mvoi.models.events import RunMode
from mvoi.publishers.base import EventHubPublisher, FilePublisher, Publisher, StdoutPublisher
from mvoi.services import health
from mvoi.services.pipeline import HistoryBaseline, Pipeline
from mvoi.services.state import StateStore
from mvoi.sources.nws import NwsAdapter
from mvoi.sources.ohgo import OhgoAdapter

log = logging.getLogger("mvoi")


def build_publisher(settings: Settings, secrets: SecretResolver, replay: bool = False) -> Publisher:
    kind = settings.publisher.kind
    if kind == "stdout":
        return StdoutPublisher()
    if kind == "file":
        return FilePublisher(settings.publisher.file_path)
    conn = None
    if replay:
        # Prefer a dedicated demo Eventstream; fall back to the main one (events are tagged).
        conn = secrets.get("replay_eventstream_connection_string", required=False)
    conn = conn or secrets.get("eventstream_connection_string")
    return EventHubPublisher(conn, settings.publisher.batch_max_events)


def build_http(settings: Settings) -> ResilientClient:
    client = httpx.Client(timeout=settings.polling.http_timeout_seconds, follow_redirects=True)
    return ResilientClient(client, max_retries=settings.polling.max_retries,
                           backoff_base=settings.polling.backoff_base_seconds,
                           backoff_max=settings.polling.backoff_max_seconds)


def build_adapters(settings: Settings, secrets: SecretResolver | None, *, live: bool,
                   recorder=None) -> dict:
    geo = load_geography()
    corridors = CorridorMatcher(geo)
    http = build_http(settings) if live else None
    api_key = secrets.get("ohgo_api_key") if (live and secrets) else None
    return {
        "ohgo": OhgoAdapter(http, settings.ohgo, api_key, geo.bounding_box, corridors,
                            settings.environment, recorder=recorder),
        "nws": NwsAdapter(http, settings.nws, geo, ZoneIndex(), settings.environment,
                          recorder=recorder),
    }


def build_pipeline(settings: Settings, adapters: dict, publisher: Publisher, store: StateStore, *,
                   run_mode: RunMode = RunMode.LIVE, replay_session_id: str | None = None,
                   lifecycle=None, metrics: health.Metrics | None = None) -> Pipeline:
    return Pipeline(
        adapters=adapters,
        intervals={"ohgo": settings.polling.interval_seconds,
                   "nws": settings.polling.nws_interval_seconds},
        store=store,
        lifecycle=lifecycle or settings.lifecycle,
        publisher=publisher,
        facilities=load_facilities(),
        risk_cfg=load_risk_config(),
        risk_settings=settings.risk,
        environment=settings.environment,
        run_mode=run_mode,
        replay_session_id=replay_session_id,
        history=HistoryBaseline(settings.risk.history_baseline_path),
        metrics=metrics,
        batch_max_events=settings.publisher.batch_max_events,
        publish_retry_initial_seconds=settings.publisher.retry_initial_seconds,
        publish_retry_max_seconds=settings.publisher.retry_max_seconds,
    )


def cmd_run(args, settings: Settings) -> int:
    secrets = SecretResolver(settings.key_vault.url)
    recorder = None
    if args.record:
        from mvoi.replay.recording import RecordingWriter
        recorder = RecordingWriter(args.record, name=Path(args.record).stem,
                                   description="Captured live OHGO/NWS responses", synthetic=False)
    adapters = build_adapters(settings, secrets, live=True, recorder=recorder)
    store = StateStore(settings.state.path)
    metrics = health.Metrics()
    publisher = build_publisher(settings, secrets)
    pipeline = build_pipeline(settings, adapters, publisher, store, metrics=metrics)
    try:
        if args.once:
            report = pipeline.run_cycle()
            return 0 if report.publish_error is None and all(p.ok for p in report.polls) else 1
        if settings.health.port:
            health.serve(metrics, settings.health.port,
                         max_age=timedelta(seconds=settings.polling.interval_seconds * 5))
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        pipeline.run_forever(stop)
        return 0
    finally:
        publisher.close()
        store.close()
        if recorder:
            recorder.close()


def cmd_replay(args, settings: Settings) -> int:
    from mvoi.replay import player as rp

    state_dir = Path(settings.state.path).parent
    if args.replay_cmd == "synthetic":
        from mvoi.replay.synthetic import write_storyline
        print(write_storyline(args.out))
        return 0

    secrets = SecretResolver(settings.key_vault.url)
    settings = settings.model_copy(update={"environment": args.environment or "demo"})
    adapters = build_adapters(settings, None, live=False)

    if args.replay_cmd == "reset":
        path = rp.session_state_path(state_dir, args.session_id)
        if not path.exists():
            print(f"no state for session {args.session_id}", file=sys.stderr)
            return 1
        store = StateStore(path)
        publisher = build_publisher(settings, secrets, replay=True)
        pipeline = build_pipeline(settings, adapters, publisher, store, run_mode=RunMode.REPLAY,
                                  replay_session_id=args.session_id)
        n = rp.reset_session(store, pipeline)
        publisher.close()
        store.close()
        path.unlink(missing_ok=True)
        print(json.dumps({"session_id": args.session_id, "resolved_events": n}))
        return 0

    # start
    publisher = build_publisher(settings, secrets, replay=True)
    stores: list[StateStore] = []

    def factory(session_id: str, speed: float) -> Pipeline:
        store = StateStore(rp.session_state_path(state_dir, session_id))
        stores.append(store)
        return build_pipeline(settings, adapters, publisher, store, run_mode=RunMode.REPLAY,
                              replay_session_id=session_id,
                              lifecycle=rp.scaled_lifecycle(settings.lifecycle, speed))

    player = rp.ReplayPlayer(args.recording, factory, adapters, speed=args.speed,
                             session_id=args.session_id)
    print(json.dumps({"session_id": player.session_id, "recording": player.info.name,
                      "synthetic": player.info.synthetic, "speed": args.speed}), flush=True)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: player.stop())
    try:
        player.run()
    finally:
        publisher.close()
        for s in stores:
            s.close()
    return 0


def cmd_capture(args, settings: Settings) -> int:
    """One live poll per resource; writes raw responses as test fixtures."""
    from mvoi.models.events import utcnow

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    class Saver:
        def record(self, source, resource, observed_at, envelope):
            (out / f"{source}_{resource}.json").write_text(json.dumps(envelope, indent=1))

    secrets = SecretResolver(settings.key_vault.url)
    adapters = build_adapters(settings, secrets, live=True, recorder=Saver())
    now = utcnow()
    summary = {}
    for name, a in adapters.items():
        for r in a.poll(now):
            summary[f"{name}:{r.entity_type}"] = {"ok": r.ok, "observations": len(r.observations),
                                                  "rejected": len(r.rejected), "error": r.error}
    print(json.dumps(summary, indent=1))
    return 0 if all(v["ok"] for v in summary.values()) else 1


def cmd_resume_stream(args) -> int:
    # Managed identity in the Container Apps job (AZURE_CLIENT_ID), `az login` locally.
    from azure.identity import DefaultAzureCredential

    from mvoi.deploy.fabric_api import FabricClient
    from mvoi.services import stream_resume
    ok = stream_resume.run(FabricClient(DefaultAzureCredential()), CONFIG_DIR / "fabric.yaml",
                           wait_minutes=args.wait_minutes)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mvoi", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--settings", help="settings YAML (default config/settings.yaml)")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="live polling service")
    r.add_argument("--once", action="store_true", help="single cycle then exit")
    r.add_argument("--record", help="also append raw responses to this recording file")

    rp = sub.add_parser("replay", help="demo replay")
    rsub = rp.add_subparsers(dest="replay_cmd", required=True)
    s = rsub.add_parser("start")
    s.add_argument("--recording", required=True)
    s.add_argument("--speed", type=float, default=4.0)
    s.add_argument("--session-id")
    s.add_argument("--environment", default="demo")
    rs = rsub.add_parser("reset")
    rs.add_argument("--session-id", required=True)
    rs.add_argument("--environment", default="demo")
    sy = rsub.add_parser("synthetic")
    sy.add_argument("--out", default="replay/recordings/synthetic-storm-and-crashes.jsonl")

    c = sub.add_parser("capture", help="save live responses as fixtures")
    c.add_argument("--out", default="tests/fixtures/live")

    rsm = sub.add_parser("resume-stream", help="resume Eventstream destinations paused by a capacity suspend")
    rsm.add_argument("--wait-minutes", type=float, default=30,
                     help="keep retrying this long while the capacity is still suspended")

    args = p.parse_args(argv)
    mlog.configure(args.log_level)
    settings = load_settings(Path(args.settings) if args.settings else None)
    if args.cmd == "run":
        return cmd_run(args, settings)
    if args.cmd == "replay":
        return cmd_replay(args, settings)
    if args.cmd == "resume-stream":
        return cmd_resume_stream(args)
    return cmd_capture(args, settings)


if __name__ == "__main__":
    sys.exit(main())
