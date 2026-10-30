"""`playtest-run`: put S1 in the production turn loop on a wired game, driven by a preflight manifest.

It runs under the PlayMode bench host the way `playtest.bench.live` does (`scripts/unity/run-session.ps1`
starts the host, which starts this module with `--project --entity --calibration --output`). In a session
the host registers exactly the catalog the manifest exports (`playtest-export-catalog`), under each
declaration's own name, so a supported chord, stick or mouse value, or skill runs as declared. That file
and its pinned SHA-256 arrive as `--catalog` and `--catalog-sha256` (`UPA_SESSION_CATALOG` and
`UPA_SESSION_CATALOG_SHA256`, the same variables the host reads); they are required, and the run ends before
any call or contact with the bridge unless the file is exactly the manifest's export (`playtest.runner.catalog`).
The manifest, approval and limits arrive as flags or `UPA_RUN_*` variables, because the host's command line
is fixed.

Budget before spend: no paid call starts without an approval file and a call cap, every call reserves
its row in `<output dir>/calls` before it is sent, and the cap and the approved envelope are checked
before the bridge is even contacted. The key is only checked for by name and is never printed.

The run record and the trace go through the #30 writer (`playtest.core.trace.TraceWriter`): the trace
is opened before the first turn and closed exactly once, with the session's status and error, after the
loop's `stop` entry, even when an exception escapes. S1's calls are paced to `--tokens-per-minute` or that
arm's own default (see `playtest.runner.pacing`); the pacing only delays when a turn begins.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from playtest.bench import call_budget, replay
from playtest.bench import record as recording
from playtest.bench.record import digest
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, ModelRef
from playtest.contracts.manifest import PreflightManifest
from playtest.core.adherence import adherence_rate
from playtest.core.bridge_client import BridgeClient
from playtest.core.run_record import build_run_record
from playtest.core.suspicion import RANGE_MIN_READINGS, ReaderWatch
from playtest.core.trace import TraceWriter
from playtest.preflight import manifest as manifest_checks
from playtest.frontier import provider as frontier_provider
from playtest.runner.directives import DirectiveTracker
from playtest.runner.frontier_help import FrontierHelp, action_effects

from .catalog import PATH_VARIABLE, SHA256_VARIABLE, verify_host_catalog
from .choices import ManifestChoices, build_choices
from .game_view import BridgeView
from .output import secret_values
from .pacing import INITIAL_ESTIMATE, TokenBudget, resolve_tokens_per_minute
from .probe import DisplacementProbe, make_snapshot
from .readers import FieldReaders
from .s1 import ARM, IMAGE_TOKENS, IMAGE_TRANSPORT, SystemOneS1
from .session import MAX_AGE_S, MAX_PROVIDER_FAILURES, WALL_LIMIT_S, run_session

DEFAULT_OBJECTIVE = (
    "Play the game for the time allowed, using only the offered choices. Try the available controls and "
    "observe their effects. Finish when you judge there is nothing more to try."
)
TRACE_FILE = "trace.jsonl"


def _env(name: str, cast: Any, default: Any) -> Any:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        raise SystemExit(f"playtest-run: {name} is not a valid {cast.__name__}") from None


def switch(raw: str) -> bool:
    """An on/off environment value: exactly `1`/`true` or `0`/`false`, so a typo is an error, not off."""
    value = raw.strip().lower()
    if value in ("1", "true"):
        return True
    if value in ("0", "false"):
        return False
    raise ValueError(raw)


def _describe(exc: BaseException) -> str:
    """`Name: message`, or just the name when the exception carries no message."""
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="playtest-run", description=__doc__)
    for name in ("project", "entity", "calibration", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=_env("UPA_RUN_MANIFEST", Path, None))
    parser.add_argument("--manifest-origin", default=_env("UPA_RUN_MANIFEST_ORIGIN", str, None),
                        help="free text saying where the manifest came from, recorded in the plan and the summary "
                             "(for example that it was made on another build and is reused; also UPA_RUN_MANIFEST_ORIGIN)")
    parser.add_argument("--catalog", type=Path, default=_env(PATH_VARIABLE, Path, None),
                        help=f"the catalog file the host serves, exported from the manifest (also {PATH_VARIABLE})")
    parser.add_argument("--catalog-sha256", default=_env(SHA256_VARIABLE, str, None),
                        help=f"the SHA-256 the catalog file was pinned to (also {SHA256_VARIABLE})")
    parser.add_argument("--approval", type=Path, default=_env("UPA_RUN_APPROVAL", Path, None),
                        help="the approval file naming the call and cost caps (also UPA_RUN_APPROVAL)")
    parser.add_argument("--max-calls", type=int, default=_env("UPA_RUN_MAX_CALLS", int, None),
                        help="the most paid calls this run may make (also UPA_RUN_MAX_CALLS)")
    parser.add_argument("--s1-arm", default=_env("UPA_RUN_S1_ARM", str, ARM),
                        help=f"the S1 model arm, one of replay.ARMS (default {ARM}; also UPA_RUN_S1_ARM)")
    parser.add_argument("--s1-game-view", action="store_true", default=_env("UPA_RUN_S1_GAME_VIEW", switch, False),
                        help="send a fresh bridge frame to a verified image-capable S1 arm (also UPA_RUN_S1_GAME_VIEW=1)")
    parser.add_argument("--frontier", action="store_true", default=_env("UPA_RUN_FRONTIER", switch, False),
                        help="enable frontier calls after S1 chooses escalate (also UPA_RUN_FRONTIER=1)")
    parser.add_argument("--max-frontier-calls", type=int,
                        default=_env("UPA_RUN_MAX_FRONTIER_CALLS", int, 0),
                        help="approved frontier call cap; requires --frontier")
    parser.add_argument("--wall-limit-s", type=float, default=_env("UPA_RUN_WALL_LIMIT_S", float, WALL_LIMIT_S))
    parser.add_argument("--max-age-s", type=float, default=_env("UPA_RUN_MAX_AGE_S", float, MAX_AGE_S),
                        help="the oldest observation the bridge will still run an action against")
    parser.add_argument("--max-provider-failures", type=int,
                        default=_env("UPA_RUN_MAX_PROVIDER_FAILURES", int, MAX_PROVIDER_FAILURES))
    parser.add_argument("--tokens-per-minute", type=int,
                        default=_env("UPA_RUN_TOKENS_PER_MINUTE", int, None),
                        help="the most S1 tokens the runner lets one minute use; it waits before a turn to stay "
                             "under it (default: the selected arm's own rate; also UPA_RUN_TOKENS_PER_MINUTE)")
    parser.add_argument("--objective", default=None)
    parser.add_argument("--objective-file", type=Path, default=_env("UPA_RUN_OBJECTIVE_FILE", Path, None))
    return parser


def _objective(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    if args.objective is not None and args.objective_file is not None:
        parser.error("give --objective or --objective-file, not both")
    if args.objective_file is not None:
        try:
            text = args.objective_file.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read the objective file: {exc}")
    else:
        text = args.objective if args.objective is not None else DEFAULT_OBJECTIVE
    if not text.strip():
        parser.error("the objective is empty")
    return text.strip()


def _fits(approval: dict[str, Any], calls: int, frontier_calls: int = 0, arm: str = ARM) -> None:
    """The approved envelope must cover the most the run may spend."""
    limit = approval["arms"][arm]
    bound = replay._decimal(limit["per_call_max_usd"], "per-call bound")
    total_bound = bound * calls
    if frontier_calls:
        f_limit = approval["arms"][frontier_provider.MODEL]
        f_bound = replay._decimal(f_limit["per_call_max_usd"], "frontier per-call bound")
        total_bound += f_bound * frontier_calls
        if (frontier_calls > f_limit["max_calls"]
                or f_bound * frontier_calls > replay._decimal(f_limit["max_cost_usd"], "frontier arm limit")):
            raise ValueError("the approved frontier call or cost envelope is smaller than its call cap")
    if (calls + frontier_calls > approval["max_calls"] or calls > limit["max_calls"]
            or total_bound > replay._decimal(approval["max_cost_usd"], "overall limit")
            or bound * calls > replay._decimal(limit["max_cost_usd"], "arm limit")):
        raise ValueError("the approved call or cost envelope is smaller than the call cap")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    if args.approval is None:
        parser.error("--approval (or UPA_RUN_APPROVAL) is required: no paid call starts without one")
    if args.max_calls is None or args.max_calls < 1:
        parser.error("--max-calls (or UPA_RUN_MAX_CALLS) is required and must be at least 1")
    if args.frontier and args.max_frontier_calls < 1:
        parser.error("--frontier requires --max-frontier-calls of at least 1")
    if not args.frontier and args.max_frontier_calls:
        parser.error("--max-frontier-calls requires --frontier")
    if args.manifest is None:
        parser.error("--manifest (or UPA_RUN_MANIFEST) is required")
    if args.catalog is None or args.catalog_sha256 is None:
        parser.error(f"--catalog and --catalog-sha256 (or {PATH_VARIABLE} and {SHA256_VARIABLE}) are required: "
                     "a session runs only against the catalog the manifest exports, never a default one")
    if args.wall_limit_s <= 0 or args.max_age_s <= 0 or args.max_provider_failures < 1:
        parser.error("the wall limit and maximum age must be positive and the failure limit at least 1")
    if args.s1_arm not in replay.ARMS:
        parser.error(f"--s1-arm must be one of {sorted(replay.ARMS)}")
    # Neither the flag nor the environment named a rate, so the selected arm's own default applies. Resolving
    # before the plan and the summary is written is what makes both record the rate this run actually used.
    args.tokens_per_minute = resolve_tokens_per_minute(args.s1_arm, args.tokens_per_minute)
    if args.tokens_per_minute < 1:
        parser.error("--tokens-per-minute (or UPA_RUN_TOKENS_PER_MINUTE) must be at least 1")
    if output.is_relative_to(project) or output.exists():
        parser.error("--output must be a new file outside the Unity project")
    if args.s1_game_view and args.s1_arm not in IMAGE_TRANSPORT:
        parser.error(f"--s1-game-view: S1 arm {args.s1_arm!r} has no verified image transport")
    for variable in replay.ARMS[args.s1_arm][1:]:
        if variable and not os.environ.get(variable):
            parser.error(f"{variable} is not set in the environment")
    if args.frontier and not os.environ.get("META_API_KEY"):
        parser.error("META_API_KEY is required when --frontier is enabled")
    objective = _objective(parser, args)

    run_dir = output.parent
    summary: dict[str, Any] = {"version": 1, "status": "inconclusive", "error": None,
                               "s1_game_view": {"enabled": args.s1_game_view, "sent": None, "omitted": None, "stale": None}}
    try:
        manifest_bytes = args.manifest.read_bytes()
        manifest = PreflightManifest.model_validate_json(manifest_bytes, strict=True)
        problems = manifest_checks.validate(manifest)
        if problems:
            raise ValueError("the manifest does not validate: " + "; ".join(problems))
        entity = json.loads(args.entity.read_bytes())
        calibration = json.loads(args.calibration.read_bytes())
        if entity.get("entity_id") != manifest.entity_id or calibration.get("entity_id") != manifest.entity_id:
            raise ValueError("the manifest, the entity record and the calibration record name different entities")
        approval, approval_sha = replay._load(args.approval)
        arms = [args.s1_arm, frontier_provider.MODEL] if args.frontier else [args.s1_arm]
        replay.validate_approval(approval, arms)
        _fits(approval, args.max_calls, args.max_frontier_calls if args.frontier else 0, args.s1_arm)
        built = build_choices(manifest)
        host = verify_host_catalog(manifest, args.catalog, args.catalog_sha256)  # before any call or contact with the bridge
        if not any(c.source.value == "manifest" for c in built.choices):
            raise ValueError("the manifest offers no executable action")
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_sha = digest(manifest_bytes)
        plan = {"version": 1, "kind": "session", "arm": args.s1_arm, "approval_sha256": approval_sha,
                "manifest_sha256": manifest_sha, "catalog_sha256": host.sha256, "max_calls": args.max_calls,
                "wall_limit_s": args.wall_limit_s, "max_age_s": args.max_age_s,
                "max_provider_failures": args.max_provider_failures, "tokens_per_minute": args.tokens_per_minute,
                "objective_sha256": digest(objective), "manifest_origin": args.manifest_origin,
                "readers": [reader.alias for reader in manifest.readers],
                "invariants": [rule.name for rule in manifest.invariants],
                "suspicions": {"min_range_readings": RANGE_MIN_READINGS,
                               "effect_allowance": "square root of the calibration sample variance plus the stage 7 "
                                                   "measurement allowance (playtest.preflight.verify.MEASUREMENT_TOLERANCE)"},
                "offered": [c.id for c in built.choices], "not_offered": built.not_offered,
                "budget_assumptions": {"balance_override": approval.get("unverified_balance_override")}}
        plan["s1_game_view"] = args.s1_game_view
        plan["frontier"] = {"enabled": args.frontier,
                             "max_calls": args.max_frontier_calls if args.frontier else 0,
                             "model": frontier_provider.MODEL if args.frontier else None}
        replay._write(run_dir / "plan.json", plan, exclusive=True)
        # The frontier helper shares this folder and runs while S1 keeps choosing, so in-flight calls block per arm.
        ledger = call_budget.Ledger(run_dir, approval, args.s1_arm, concurrent_arms=True)
        before = recording._game_worktree(project)
        summary = asyncio.run(_run(project, manifest, built, objective, ledger, approval, args, manifest_sha, host.sha256,
                                   calibration=calibration))
        after = recording._game_worktree(project)
        summary["game_worktree_changed"] = None if before is None or after is None else before != after
        if summary["game_worktree_changed"] is True and summary["status"] == "complete":
            summary.update(status="inconclusive", error="the game worktree changed during the run")
    except Exception as exc:  # noqa: BLE001 - fail closed: _run released the bridge; leave the cause in the summary
        summary.update(status="inconclusive", error=f"{type(exc).__name__}: {exc}")
        print(f"playtest-run: {type(exc).__name__}: {exc}", file=sys.stderr)
    except BaseException as exc:  # an interrupt: _run stopped the loop and closed the trace; say so, then let it go on
        summary.update(status="inconclusive", error=_describe(exc))
        raise
    finally:
        if run_dir.is_dir():
            try:
                replay._write(output, summary, exclusive=True)
            except OSError as exc:
                print(f"playtest-run: could not write the summary: {exc}", file=sys.stderr)
    print(f"run {summary['status']}: turns={summary.get('turns')} ending={summary.get('ending')}")
    return 0 if summary["status"] == "complete" else 1


def _suspicions(raised: list[Any], min_readings: int, drift_readings: int | None = None) -> dict[str, Any]:
    """How many suspicions the run raised and of what kind: a count of facts, not a verdict."""
    return {"total": len(raised), "min_range_readings": min_readings, "min_drift_readings": drift_readings,
            "by_basis": dict(Counter(item.suspicion.basis.value for item in raised)),
            "by_subject": dict(Counter(item.suspicion.subject for item in raised))}


async def _run(project: Path, manifest: PreflightManifest, built: ManifestChoices, objective: str,
               ledger: call_budget.Ledger, approval: dict[str, Any], args: argparse.Namespace,
               manifest_sha: str, catalog_sha: str, calibration: dict[str, Any] | None = None) -> dict[str, Any]:
    run_dir = args.output.resolve().parent
    started_utc = dt.datetime.now(dt.timezone.utc)
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    try:
        snapshot = make_snapshot(client, manifest.entity_id)
        await snapshot()  # before any paid call: the manifest's entity must be in the running game
        scene = (await client.observe_once()).scene
        readers = FieldReaders(client, manifest.readers) if manifest.readers else None
        if readers is not None:
            await readers.install()  # before any paid call: the manifest's readers must resolve in the running game
        watch = ReaderWatch({reader.alias: reader.kind for reader in manifest.readers}, manifest.invariants)
        extra = IMAGE_TOKENS[IMAGE_TRANSPORT[args.s1_arm]] if args.s1_game_view else 0
        per_minute, first_call = args.tokens_per_minute, INITIAL_ESTIMATE + extra   # the image counts from the first call
        budget = TokenBudget(per_minute, initial_estimate=first_call)
        s1 = SystemOneS1(ledger, max_calls=args.max_calls, arm=args.s1_arm, budget=budget,
                          game_view=args.s1_game_view)
        status = client.status
        view = BridgeView(project, status, started_utc=started_utc) if args.s1_game_view else None
        record = await asyncio.to_thread(  # it runs git; keep the bridge connection serviced meanwhile
            build_run_record,
            game_project=project, status=status, scene=scene, manifest_fingerprint=manifest_sha,
            clock_mode=ClockMode.realtime,
            input_mode=status.input_backends[0].value if status is not None and status.input_backends else None,
            models=[ModelRef(role="s1", provider=s1.provider, model=s1.model, prompt_hash=s1.prompt_sha256)]
                   + ([ModelRef(role="frontier", provider="meta", model=frontier_provider.MODEL)]
                      if args.frontier else []),
            objective=objective,
            budget=Budget(max_wall_s=args.wall_limit_s, max_usd=float(approval["max_cost_usd"]),
                          max_frontier_calls=args.max_frontier_calls if args.frontier else 0),
            reset_method="none")  # this runner never resets the game
        writer = TraceWriter(run_dir / TRACE_FILE, record, secrets=secret_values())
        tracker = DirectiveTracker(writer)
        help_port = (FrontierHelp(project=project, status=status, manifest=manifest, directory=run_dir,
                                  approval=approval, sink=writer, tracker=tracker,
                                  started_utc=dt.datetime.now(dt.timezone.utc), calibration=calibration,
                                  effects=action_effects(built.expected))
                     if args.frontier else None)
        closing: tuple[str, str | None] = ("inconclusive", None)
        try:
            result = await run_session(
                client, built, s1=s1, objective=objective, view=view, probe=DisplacementProbe(snapshot, built.expected),
                readers=readers, watch=watch, sink=writer, max_age_s=args.max_age_s, wall_limit_s=args.wall_limit_s,
                max_provider_failures=args.max_provider_failures, budget=budget, help=help_port,
                facts=tracker, s1_call_cap=args.max_calls)
            closing = (result.status, result.error)
            turns = result.loop.turns
            summary = {
                "version": 1, "status": result.status, "ending": result.ending, "error": result.error,
                "turns": result.turns, "outcomes": result.outcomes, "provider_failures": result.provider_failures,
                "adherence": {"rate": adherence_rate(turns), "violations": [v.__dict__ for v in result.adherence]},
                "stop": result.stop.model_dump(mode="json") if result.stop is not None else None,
                "s1": {"provider": s1.provider, "model": s1.model, "prompt_sha256": s1.prompt_sha256,
                       "calls": s1.calls_reserved, "call_cap": args.max_calls},
                "s1_game_view": s1.view_counts,
                "frontier": {"enabled": args.frontier, "model": frontier_provider.MODEL if args.frontier else None,
                             "calls": help_port.calls if help_port is not None else 0,
                             "call_cap": args.max_frontier_calls if args.frontier else 0},
                "manifest": {"sha256": manifest_sha, "catalog_sha256": catalog_sha, "entity_id": manifest.entity_id,
                             "origin": args.manifest_origin,
                             "offered": [c.id for c in built.choices], "not_offered": built.not_offered},
                "readers": None if readers is None else {
                    "declared": readers.aliases,
                    "installed": readers.installed.summary() if readers.installed is not None else None,
                    **readers.stats.summary()},
                "suspicions": _suspicions(result.loop.suspicions, watch.min_readings, watch.drift_readings),
                "settings": {"max_age_s": args.max_age_s, "wall_limit_s": args.wall_limit_s,
                             "max_provider_failures": args.max_provider_failures,
                             "tokens_per_minute": args.tokens_per_minute},
                "pacing": {**dataclasses.asdict(result.pacing), "calls_counted": budget.recorded,
                           "calls_estimated": budget.estimated},
                "trace": TRACE_FILE,
            }
        except BaseException as exc:  # the loop has stopped and released its inputs; the trace still gets its end
            closing = ("inconclusive", _describe(exc))
            raise
        finally:
            writer.close(*closing)
        summary["redactions"] = writer.redactions
        return summary
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(main())
