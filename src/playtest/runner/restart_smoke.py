"""`playtest-restart-smoke`: a free, local check of the Editor-owned restart path.

It drives the production turn loop and bridge client under a live Play Mode session host, with a
scripted S1 that makes no provider call, spends no money and reads no credential. The script picks the
host's `restart_play_mode` choice once, then `finish`; every other choice comes from the manifest, so the
smoke exercises the real gate (offered only when the host advertises it), the verified input release, the
mailbox ticket, the readiness check and the rebind to the new play generation.

Inputs are the ones a session already has: `--project --entity --calibration --manifest --output`, plus
the control directory the wrapper exported as `UPA_SESSION_CONTROL_DIRECTORY`. The catalog the host
serves is the one its `config.json` names; the file's SHA-256 must match the pinned one. Nothing here
starts Unity or writes control records other than the runner's own request, and no result is ever
invented: a host that does not answer ends the smoke inconclusive.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from playtest.bench.record import digest
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, ModelRef
from playtest.contracts.manifest import PreflightManifest
from playtest.core.bridge_client import BridgeClient
from playtest.core.ports import S1Answer, S1Call
from playtest.core.run_record import build_run_record
from playtest.core.trace import TraceWriter
from playtest.preflight import manifest as manifest_checks

from .catalog import verify_host_catalog
from .choices import RESTART_ID, build_choices
from .output import secret_values
from .probe import make_snapshot
from .session import run_session
from .session_control import (
    HostSession,
    SessionControl,
    SessionControlError,
    control_directory,
    load_host_session,
)

TRACE_FILE = "trace.jsonl"
OBJECTIVE = ("Restart the play session once through the host, then finish. The scripted S1 never asks a "
             "model for anything.")
SCRIPT = "restart_play_mode then finish; scripted locally, no provider call"


class ScriptedS1:
    """One restart, then finish. It answers from a list, makes no network call and needs no credential."""

    def __init__(self) -> None:
        self._restarted = False

    async def decide(self, context: dict[str, Any], *, image: bytes | None = None) -> S1Answer:
        choice = "finish" if self._restarted else RESTART_ID
        self._restarted = True
        answer = {"choice_id": choice, "set_version": context["choice_set_version"],
                  "reason": "scripted restart smoke"}
        call = S1Call(question="scripted_restart_smoke", provider="scripted", model="none",
                      resolved_model=None, problem=None, response={"choice_id": choice})
        return S1Answer(answer=answer, calls=(call,))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="playtest-restart-smoke", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("project", "entity", "calibration", "manifest", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--wall-limit-s", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    if not project.is_dir():
        parser.error("--project must be an existing directory")
    if output.is_relative_to(project) or output.exists():
        parser.error("--output must be a new file outside the Unity project")
    if not args.wall_limit_s > 0:
        parser.error("--wall-limit-s must be positive")
    directory = control_directory()
    if directory is None:
        parser.error(f"{'UPA_SESSION_CONTROL_DIRECTORY'} is not set: this smoke runs only under an "
                     "Editor-owned session host")
    summary: dict[str, Any] = {"version": 1, "status": "inconclusive", "error": None}
    try:
        summary = asyncio.run(_run(project, args, directory))
    except SessionControlError as exc:
        summary.update(status="inconclusive", error=f"SessionControlError: {exc}")
        print(f"playtest-restart-smoke: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - the smoke fails closed and says why
        summary.update(status="inconclusive", error=f"{type(exc).__name__}: {exc}")
        print(f"playtest-restart-smoke: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                              encoding="utf-8")
        except OSError as exc:
            print(f"playtest-restart-smoke: could not write the summary: {exc}", file=sys.stderr)
    print(f"restart smoke {summary['status']}: turns={summary.get('turns')} "
          f"ending={summary.get('ending')} restarts={summary.get('restarts')}")
    return 0 if summary["status"] == "complete" else 1


async def _run(project: Path, args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    manifest_bytes = args.manifest.read_bytes()
    manifest = PreflightManifest.model_validate_json(manifest_bytes, strict=True)
    problems = manifest_checks.validate(manifest)
    if problems:
        raise ValueError("the manifest does not validate: " + "; ".join(problems))
    entity = json.loads(args.entity.read_bytes())
    calibration = json.loads(args.calibration.read_bytes())
    if entity.get("entity_id") != manifest.entity_id or calibration.get("entity_id") != manifest.entity_id:
        raise ValueError("the manifest, the entity record and the calibration record name different entities")
    host: HostSession = load_host_session(directory, project=project)
    if not host.advertises_restart:
        raise ValueError("the host does not advertise restart_play_mode, so there is nothing to smoke")
    host_catalog = verify_host_catalog(manifest, Path(host.config.catalog_path), host.config.catalog_sha256)
    built = build_choices(manifest, restart_play_mode=True)
    if not any(choice.id == RESTART_ID for choice in built.choices):
        raise ValueError("the restart choice is not offered despite the host's capability")
    if not any(choice.source.value == "manifest" for choice in built.choices):
        raise ValueError("the manifest offers no executable action")

    run_dir = args.output.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    started_utc = dt.datetime.now(dt.timezone.utc)
    generation = host.generation
    control = SessionControl(host, project=project)
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    try:
        snapshot = make_snapshot(client, manifest.entity_id)
        await snapshot()  # the running game must still hold the manifest's entity before the restart
        await client.observe_once()
        record = await asyncio.to_thread(
            build_run_record,
            game_project=project, status=client.status, scene=(await client.observe_once()).scene,
            manifest_fingerprint=digest(manifest_bytes), clock_mode=ClockMode.realtime,
            input_mode=(client.status.input_backends[0].value
                        if client.status is not None and client.status.input_backends else None),
            models=[ModelRef(role="s1", provider="scripted", model="none", prompt_hash=digest(SCRIPT))],
            objective=OBJECTIVE,
            budget=Budget(max_wall_s=args.wall_limit_s),
            reset_method="restart_play_mode")
        writer = TraceWriter(run_dir / TRACE_FILE, record, secrets=secret_values())
        closing: tuple[str, str | None] = ("inconclusive", None)
        try:
            result = await run_session(
                client, built, s1=ScriptedS1(), objective=OBJECTIVE, sink=writer,
                wall_limit_s=args.wall_limit_s, max_provider_failures=1,
                restart=control, generation=generation)
            closing = (result.status, result.error)
            return {
                "version": 1, "status": result.status, "ending": result.ending, "error": result.error,
                "turns": result.turns, "outcomes": result.outcomes,
                "stop": result.stop.model_dump(mode="json") if result.stop is not None else None,
                "restarts": [operation.model_dump(mode="json") for operation in result.restart_history],
                "session_control": {"run_id": host.run_id, "start_generation": generation,
                                    "end_generation": result.loop.generation,
                                    "capabilities": list(host.capabilities)},
                "manifest": {"sha256": digest(manifest_bytes), "catalog_sha256": host_catalog.sha256,
                             "entity_id": manifest.entity_id},
                "offered": [choice.id for choice in built.choices],
                "s1": {"provider": "scripted", "model": "none", "calls": 0, "script": SCRIPT},
                "settings": {"wall_limit_s": args.wall_limit_s},
                "trace": TRACE_FILE,
            }
        finally:
            writer.close(*closing)
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(main())
