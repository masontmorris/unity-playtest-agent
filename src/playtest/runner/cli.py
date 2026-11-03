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
import math
import os
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from playtest.bench import call_budget, replay
from playtest.bench import record as recording
from playtest.bench.record import digest
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceKind
from playtest.frontier import media
from playtest.contracts.investigation import Budget, ModelRef
from playtest.contracts.manifest import PreflightManifest
from playtest.core.adherence import adherence_rate
from playtest.core.bridge_client import BridgeClient
from playtest.core.context import INSTRUCTIONS_POLICY_VARIANT, INSTRUCTIONS_VARIANTS
from playtest.core.run_record import build_run_record
from playtest.core.suspicion import RANGE_MIN_READINGS, ReaderWatch
from playtest.core.trace import TraceWriter
from playtest.preflight import manifest as manifest_checks
from playtest.frontier import provider as frontier_provider
from playtest.runner.directives import DirectiveTracker
from playtest.runner.frontier_help import FrontierHelp, action_effects

from .catalog import PATH_VARIABLE, SHA256_VARIABLE, verify_host_catalog
from .choices import FINISH_LABELS, ManifestChoices, build_choices
from .coverage import CoverageFacts
from .pattern import PatternFacts
from .declared import declared_configuration
from .detectors import DetectorWatch, build_detectors, detectors_config
from .game_view import BridgeView
from .output import secret_values
from . import modal_wake
from .pacing import INITIAL_ESTIMATE, TokenBudget, resolve_tokens_per_minute
from .probe import DisplacementProbe, make_snapshot
from .progress import ProgressFacts, ProgressProbe
from .readers import FieldReaders
from .request_layout import RequestLayout
from .s1 import ARM, IMAGE_TOKENS, IMAGE_TRANSPORT, SystemOneS1
from .session import MAX_AGE_S, MAX_PROVIDER_FAILURES, WALL_LIMIT_S, run_session
from .session_control import (
    HostSession,
    SessionControl,
    SessionControlError,
    control_directory,
    load_host_session,
)
from .travel import TravelFacts

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
    parser.add_argument("--discovery", type=Path, default=_env("UPA_RUN_DISCOVERY", Path, None))
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
    parser.add_argument("--s1-layout", default=_env("UPA_RUN_S1_LAYOUT", str, RequestLayout.full.value),
                        choices=[layout.value for layout in RequestLayout],
                        help="the S1 request body layout: full (default), or the lossless layout that sends each "
                             "duplicated text once (also UPA_RUN_S1_LAYOUT)")
    parser.add_argument("--s1-instructions", default=_env("UPA_RUN_S1_INSTRUCTIONS", str, INSTRUCTIONS_POLICY_VARIANT),
                        choices=sorted(INSTRUCTIONS_VARIANTS),
                        help="the S1 question text: policy/1 (default, today's paragraph) or direct/1 (one direct "
                             "question first; docs/design/s1-opt-direct-instructions.md; also "
                             "UPA_RUN_S1_INSTRUCTIONS)")
    parser.add_argument("--s1-compact-state", default=_env("UPA_RUN_S1_COMPACT_STATE", str, "off"),
                        choices=["off", "object", "format", "default", "all"],
                        help="restructure S1's state with lossless rules: off (default); object (every rule, state "
                             "kept a JSON object, about 13%% smaller); or as compact text: format, default, all "
                             "(about 29%% fewer billed tokens, but d1-ultra renders a string state differently and "
                             "changes some near-tie choices). Liquid arms only; use with --s1-layout full. See "
                             "docs/design/s1-token-budget.md (also UPA_RUN_S1_COMPACT_STATE)")
    parser.add_argument("--s1-context", default=_env("UPA_RUN_S1_CONTEXT", str, "full"),
                        choices=["full", "compact-menu"],
                        help="what S1 reads: full (default, the payload the loop built, byte for byte) or "
                             "compact-menu (the menu's own shorter state: one comparable label per choice, the "
                             "run's measured positions and the game view; requires --options for the measured "
                             "cell, and cannot be combined with --s1-layout, --s1-compact-state, a non-sorted "
                             "option order or --s1-pending-question; also UPA_RUN_S1_CONTEXT)")
    parser.add_argument("--s1-pending-question", default=_env("UPA_RUN_S1_PENDING_QUESTION", str, "follow_up"),
                        choices=["follow_up", "batched"],
                        help="ask while_pending in a second call after a help choice (follow_up, default) or beside "
                             "next_action in the same call (batched; docs/design/s1-opt-batch-pending.md; also "
                             "UPA_RUN_S1_PENDING_QUESTION)")
    parser.add_argument("--coverage-facts", action="store_true", default=_env("UPA_RUN_COVERAGE_FACTS", switch, False),
                        help="state each turn how many inputs each choice has sent this run and which offered "
                             "actions and skills have sent none (also UPA_RUN_COVERAGE_FACTS=1)")
    parser.add_argument("--detectors", action="store_true", default=_env("UPA_RUN_DETECTORS", switch, False),
                        help="raise the generic runtime detectors as suspicions over the observations already "
                             "taken (non-finite pose, a fall below the lowest settled floor, an observation "
                             "hitch, a new runtime error, a reader above a declared maximum); each states its "
                             "measured values and the bound it crossed (also UPA_RUN_DETECTORS=1)")
    parser.add_argument("--detector-max", action="append", default=None, metavar="ALIAS=VALUE",
                        help="one declared reader maximum the reader-over-max detector compares against, "
                             "repeatable (also UPA_RUN_DETECTOR_MAX as a comma-separated list)")
    parser.add_argument("--point-moves", action="store_true", default=_env("UPA_RUN_POINT_MOVES", switch, False))
    parser.add_argument("--point-turns", action="store_true", default=_env("UPA_RUN_POINT_TURNS", switch, False))
    parser.add_argument("--capture-width", type=int, default=_env("UPA_RUN_CAPTURE_WIDTH", int, 640))
    parser.add_argument("--capture-height", type=int, default=_env("UPA_RUN_CAPTURE_HEIGHT", int, 480))
    parser.add_argument("--turn-sizes", action="store_true", default=_env("UPA_RUN_TURN_SIZES", switch, False))
    parser.add_argument("--pattern-facts", action="store_true", default=_env("UPA_RUN_PATTERN_FACTS", switch, False))
    parser.add_argument("--travel-facts", action="store_true", default=_env("UPA_RUN_TRAVEL_FACTS", switch, False),
                        help="state each turn where the body actually went over the last 20 executed choices: the "
                             "total path, the position change as a vector, and each choice's mean displacement "
                             "vector (also UPA_RUN_TRAVEL_FACTS=1)")
    parser.add_argument("--frontier", action="store_true", default=_env("UPA_RUN_FRONTIER", switch, False),
                        help="enable frontier calls after S1 chooses escalate (also UPA_RUN_FRONTIER=1)")
    parser.add_argument("--max-frontier-calls", type=int,
                        default=_env("UPA_RUN_MAX_FRONTIER_CALLS", int, 0),
                        help="approved frontier call cap; requires --frontier")
    parser.add_argument("--frontier-clip-s", type=float,
                        default=_env("UPA_RUN_FRONTIER_CLIP_S", float, media.WINDOW_S),
                        help="seconds of game view in each frontier clip (also UPA_RUN_FRONTIER_CLIP_S)")
    parser.add_argument("--wall-limit-s", type=float, default=_env("UPA_RUN_WALL_LIMIT_S", float, WALL_LIMIT_S))
    parser.add_argument("--max-age-s", type=float, default=_env("UPA_RUN_MAX_AGE_S", float, MAX_AGE_S),
                        help="the oldest observation the bridge will still run an action against")
    parser.add_argument("--max-provider-failures", type=int,
                        default=_env("UPA_RUN_MAX_PROVIDER_FAILURES", int, MAX_PROVIDER_FAILURES))
    parser.add_argument("--tokens-per-minute", type=int,
                        default=_env("UPA_RUN_TOKENS_PER_MINUTE", int, None),
                        help="the most S1 tokens the runner lets one minute use; it waits before a turn to stay "
                             "under it (default: the selected arm's own rate; also UPA_RUN_TOKENS_PER_MINUTE)")
    parser.add_argument("--continuous-input", action="store_true",
                        default=_env("UPA_RUN_CONTINUOUS_INPUT", switch, False),
                        help="Decide while explicitly selected keyboard holds run (opt in)")
    parser.add_argument("--options", action="store_true", default=_env("UPA_RUN_OPTIONS", switch, False))
    parser.add_argument("--frontier-advice", action="store_true",
                        default=_env("UPA_RUN_FRONTIER_ADVICE", switch, False),
                        help="ask the frontier for gameplay advice when S1 escalates (opt in; also "
                             "UPA_RUN_FRONTIER_ADVICE)")
    parser.add_argument("--measure-air-rise", action="store_true",
                        default=_env("UPA_RUN_MEASURE_AIR_RISE", switch, False),
                        help="before play, measure the rise pressed again at its apex and offer a hop that does so "
                             "when it rose higher (needs --options; also UPA_RUN_MEASURE_AIR_RISE)")
    parser.add_argument("--measure-guards", action="store_true",
                        default=_env("UPA_RUN_MEASURE_GUARDS", switch, False),
                        help="before play, measure each verb at rest, while moving and in the air against a baseline "
                             "of each, and save the table as guards.json (the host must serve the guard chords: "
                             "playtest-export-catalog --guard-chords; also UPA_RUN_MEASURE_GUARDS)")
    parser.add_argument("--decide-during-options", action="store_true",
                        default=_env("UPA_RUN_DECIDE_DURING_OPTIONS", switch, False),
                        help="run options, actions and skills in the background so S1 decides again at once: picking "
                             "the running one again keeps it going, another choice interrupts it; a hop in flight is "
                             "waited out (opt in; also UPA_RUN_DECIDE_DURING_OPTIONS)")
    parser.add_argument("--hold-met-directive", action="store_true",
                        default=_env("UPA_RUN_HOLD_MET_DIRECTIVE", switch, False),
                        help="keep a directive whose setpoint was met in S1's context, marked met, until its own "
                             "deadline (opt in; also UPA_RUN_HOLD_MET_DIRECTIVE)")
    parser.add_argument("--keep-help-across-restart", action="store_true",
                        default=_env("UPA_RUN_KEEP_HELP_ACROSS_RESTART", switch, False),
                        help="keep S1's open help request through a restart into the same scene instead of "
                             "cancelling it (opt in; also UPA_RUN_KEEP_HELP_ACROSS_RESTART)")
    parser.add_argument("--memory-dir", type=Path, default=_env("UPA_RUN_MEMORY_DIR", Path, None),
                        help="the folder that keeps the options layer's outcome memory between runs, beside the "
                             "preflight cache; default off, so nothing is read or written and the run is "
                             "byte-identical (also UPA_RUN_MEMORY_DIR)")
    parser.add_argument("--contingent", action="store_true", default=_env("UPA_RUN_CONTINGENT", switch, False),
                        help="act on S1's own if_blocked/if_succeeded answers, which the compact menu already asks "
                             "beside next_action: when an option ends blocked or succeeded the next turn executes "
                             "that answer with no further call. Requires --s1-context compact-menu "
                             "(also UPA_RUN_CONTINGENT=1)")
    parser.add_argument("--placement", action="store_true", default=_env("UPA_RUN_PLACEMENT", switch, False))
    parser.add_argument("--pacing-burst", type=float, default=_env("UPA_RUN_PACING_BURST", float, 0.0),
                        help="the share of the per-minute token budget S1's calls may use before pacing spaces "
                             "them; 0 (default) spaces every call, 0.5 lets a session run unspaced until half the "
                             "budget is in the window (also UPA_RUN_PACING_BURST)")
    parser.add_argument("--pacing-anchor", default=_env("UPA_RUN_PACING_ANCHOR", str, "end"),
                        choices=["end", "send"],
                        help="the moment the token pacer measures its spacing from: end (default) when the "
                             "previous response arrived, or send when the previous request was posted, which "
                             "stops each call's latency adding to the next wait; requests are built the same "
                             "way (also UPA_RUN_PACING_ANCHOR)")
    parser.add_argument("--finish-label", default=_env("UPA_RUN_FINISH_LABEL", str, "plain"),
                        choices=sorted(FINISH_LABELS),
                        help="how the finish choice is labelled: plain ('Finish', default) or conditional (states "
                             "that it ends the session and when to choose it; also UPA_RUN_FINISH_LABEL)")
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


def _detector_maxima(parser: argparse.ArgumentParser, args: argparse.Namespace,
                     manifest: PreflightManifest) -> dict[str, float]:
    """The reader maxima `--detector-max` declares, checked against the readers this manifest declares."""
    raw = list(args.detector_max or ())
    raw += [part for part in _env("UPA_RUN_DETECTOR_MAX", str, "").split(",") if part.strip()]
    if raw and not args.detectors:
        parser.error("--detector-max declares a detector's bound; it requires --detectors")
    declared = {reader.alias for reader in manifest.readers}
    maxima: dict[str, float] = {}
    for item in raw:
        alias, separator, value = str(item).partition("=")
        alias = alias.strip()
        if not separator or alias not in declared:
            parser.error(f"--detector-max {item!r}: name a reader the manifest declares, as ALIAS=VALUE "
                         f"(declared: {', '.join(sorted(declared)) or 'none'})")
        try:
            number = float(value)
        except ValueError:
            parser.error(f"--detector-max {item!r}: the maximum is not a number")
        if not math.isfinite(number):
            parser.error(f"--detector-max {item!r}: the maximum must be finite")
        maxima[alias] = number
    return maxima


def _detectors(args: argparse.Namespace, calibration: Mapping[str, Any], built: ManifestChoices,
               maxima: Mapping[str, float]) -> list[Any]:
    """The generic detectors this run states facts with, and the measured peak the fall bound derives from.

    The fall bound is twice the rise peak the calibration measured, taken through the same options
    primitives the options layer builds; with no measured rise the bound is unknown, the plan says so and
    that detector states nothing.
    """
    if not args.detectors:
        return []
    peak: float | None = None
    source = "unknown: the calibration measured no rise, so the fall bound has no peak"
    if calibration:
        from .options.primitives import primitives_from_calibration
        primitives = primitives_from_calibration(
            calibration,
            [choice.action for choice in built.choices if choice.kind is ChoiceKind.action and choice.action],
            catalog=built.catalog.actions)
        if primitives.rise_peak is not None:
            peak = primitives.rise_peak
            source = "the calibration's measured rise peak, through the options primitives"
    return build_detectors(rise_peak=peak, peak_source=source, maxima=maxima,
                           maxima_source="an operator declaration (`--detector-max`)")


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


def _host_session(project: Path) -> HostSession | None:
    """The validated Editor-owned host session this runner was started under, or `None` for a bench run."""
    directory = control_directory()
    if directory is None:
        return None
    try:
        return load_host_session(directory, project=project)
    except SessionControlError as exc:
        raise ValueError(f"the session control directory is not usable: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if sum((args.turn_sizes, args.point_turns, args.point_moves)) > 1:
        parser.error("--point-moves, --point-turns and --turn-sizes are mutually exclusive")
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
    if not 0 < args.frontier_clip_s <= media.MAX_WINDOW_S:
        parser.error(f"--frontier-clip-s must be above 0 and at most {media.MAX_WINDOW_S:g}")
    if not 0 <= args.pacing_burst <= 1:
        parser.error("--pacing-burst (or UPA_RUN_PACING_BURST) must be between 0 and 1")
    if args.pacing_anchor not in ("end", "send"):
        parser.error("--pacing-anchor (or UPA_RUN_PACING_ANCHOR) must be 'end' or 'send'")
    if args.tokens_per_minute < 1:
        parser.error("--tokens-per-minute (or UPA_RUN_TOKENS_PER_MINUTE) must be at least 1")
    if output.is_relative_to(project) or output.exists():
        parser.error("--output must be a new file outside the Unity project")
    if args.s1_game_view and args.s1_arm not in IMAGE_TRANSPORT:
        parser.error(f"--s1-game-view: S1 arm {args.s1_arm!r} has no verified image transport")
    if args.s1_context == "compact-menu":
        if not args.options:
            parser.error("--s1-context compact-menu requires --options: the menu's measured cell comes from the "
                         "options layer's memory")
        if args.s1_layout != RequestLayout.full.value:
            parser.error("--s1-context compact-menu sends its own state; it cannot be combined with --s1-layout")
        if args.s1_compact_state != "off":
            parser.error("--s1-context compact-menu sends its own state; it cannot be combined with "
                         "--s1-compact-state")
        if args.s1_pending_question != "follow_up":
            parser.error("--s1-context compact-menu asks its own pending question beside next_action; it cannot be "
                         "combined with --s1-pending-question")
        if args.s1_game_view and IMAGE_TRANSPORT[args.s1_arm] != "images":
            parser.error(f"--s1-context compact-menu: S1 arm {args.s1_arm!r} does not take a plain images array, "
                         f"so the menu cannot attach its game view")
    elif args.contingent:
        parser.error("--contingent requires --s1-context compact-menu: the branch answers it acts on are that "
                     "request's own if_blocked/if_succeeded questions")
    for variable in replay.credential_variables(args.s1_arm):
        if not os.environ.get(variable):
            parser.error(f"{variable} is not set in the environment")
    if args.memory_dir is not None:
        if not args.options:
            parser.error("--memory-dir requires --options: it holds the options layer's measured outcomes")
        directory = args.memory_dir.resolve()
        if directory == project or directory.is_relative_to(project):
            parser.error("--memory-dir must be outside the Unity project, beside the preflight cache")
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
        discovery_bytes = args.discovery.read_bytes() if args.discovery else None
        declared = declared_configuration(json.loads(discovery_bytes), manifest.entity_id) if discovery_bytes else None
        approval, approval_sha = replay._load(args.approval)
        arms = [args.s1_arm, frontier_provider.MODEL] if args.frontier else [args.s1_arm]
        replay.validate_approval(approval, arms)
        _fits(approval, args.max_calls, args.max_frontier_calls if args.frontier else 0, args.s1_arm)
        if args.s1_arm in replay.MODAL_ARMS:
            # A scaled-to-zero server takes minutes to start; wake it before the game is touched, so the
            # first turn is not spent waiting on a cold GPU.
            summary["s1_server_wake_s"] = modal_wake.wake(args.s1_arm)
        session_host = _host_session(project)
        built = build_choices(manifest, restart_play_mode=session_host is not None
                              and session_host.advertises_restart,
                              finish_label=FINISH_LABELS[args.finish_label],
                              turn_sizes=args.turn_sizes, point_turns=args.point_turns, point_moves=args.point_moves,
                              capture_size=(args.capture_width, args.capture_height),
                              help_kinds=FrontierHelp.supported_kinds if args.frontier else frozenset(),
                              calibration=calibration if args.turn_sizes or args.point_turns or args.point_moves else None)
        host = verify_host_catalog(manifest, args.catalog, args.catalog_sha256,
                                   turn_sizes=args.turn_sizes, point_turns=args.point_turns, point_moves=args.point_moves,
                                   capture_size=(args.capture_width, args.capture_height),
                                   calibration=calibration if (args.turn_sizes or args.point_turns or args.point_moves
                                                               or args.measure_guards) else None,
                                   guard_chords=args.measure_guards)  # before any call or contact with the bridge
        if session_host is not None:
            # The host must serve the very catalog this run verified, or its scene is a different session.
            if (Path(session_host.config.catalog_path).resolve() != args.catalog.resolve()
                    or session_host.config.catalog_sha256 != host.sha256):
                raise ValueError("the session control directory names a different catalog than this run serves")
        if not any(c.source.value == "manifest" for c in built.choices):
            raise ValueError("the manifest offers no executable action")
        detector_maxima = _detector_maxima(parser, args, manifest)
        detectors = _detectors(args, calibration, built, detector_maxima)
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
        if discovery_bytes is not None:
            plan["discovery_sha256"] = digest(discovery_bytes)
            plan["declared_configuration_count"] = len(declared["values"]) if declared else 0
        plan["s1_game_view"] = args.s1_game_view
        plan["s1_layout"] = args.s1_layout
        if args.s1_instructions != INSTRUCTIONS_POLICY_VARIANT:
            plan["s1_instructions"] = args.s1_instructions  # only a non-default variant adds bytes
        if args.s1_compact_state != "off":
            plan["s1_compact_state"] = args.s1_compact_state
        if args.s1_pending_question != "follow_up":
            plan["s1_pending_question"] = args.s1_pending_question
        if args.s1_context != "full":
            plan["s1_context"] = args.s1_context
        if args.continuous_input:
            plan["continuous_input"] = True
        if args.options:
            plan["options"] = True
        if args.keep_help_across_restart:
            plan["keep_help_across_restart"] = True
        if args.frontier_advice:
            plan["frontier_advice"] = True
        if args.hold_met_directive:
            plan["hold_met_directive"] = True
        if args.decide_during_options:
            plan["decide_during_options"] = True
        if args.measure_air_rise:
            plan["measure_air_rise"] = True
        if args.measure_guards:
            plan["measure_guards"] = True
        if args.contingent:
            plan["contingent"] = True
        if args.placement:
            plan["placement"] = True
        if args.pacing_burst:
            plan["pacing_burst"] = args.pacing_burst
        if args.pacing_anchor != "end":
            plan["pacing_anchor"] = "send"
        if args.finish_label != "plain":
            plan["finish_label"] = args.finish_label
        plan["coverage_facts"] = args.coverage_facts
        if args.point_moves:
            plan["point_moves"] = True
        if args.point_turns:
            plan["point_turns"] = True
        if args.turn_sizes:
            plan["turn_sizes"] = True
        if args.pattern_facts:
            plan["pattern_facts"] = True
        if args.travel_facts:
            plan["travel_facts"] = True
        if args.detectors:
            # On the run's plan: which detectors ran, and the bound or constant each measures against.
            plan["detectors"] = detectors_config(detectors)
        plan["frontier"] = {"enabled": args.frontier,
                             "max_calls": args.max_frontier_calls if args.frontier else 0,
                             "model": frontier_provider.MODEL if args.frontier else None}
        plan["session_control"] = None if session_host is None else {
            "run_id": session_host.run_id, "generation": session_host.generation,
            "capabilities": list(session_host.capabilities),
            "advertises_restart": session_host.advertises_restart,
            "restart_offered": any(c.id == "restart_play_mode" for c in built.choices),
        }
        replay._write(run_dir / "plan.json", plan, exclusive=True)
        # The frontier helper shares this folder and runs while S1 keeps choosing, so in-flight calls block per arm.
        ledger = call_budget.Ledger(run_dir, approval, args.s1_arm, concurrent_arms=True)
        before = recording._game_worktree(project)
        summary = asyncio.run(_run(project, manifest, built, objective, ledger, approval, args, manifest_sha, host.sha256,
                                   calibration=calibration, session_host=session_host, declared=declared,
                                   detectors=detectors))
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


def _plan_memory(run_dir: Path, facts: Mapping[str, Any]) -> None:
    """Record on the run's plan what the outcome memory loaded and saved: a count of facts, not a verdict.

    The plan is written before the run; this rewrites it atomically so a finished folder says which memory file
    contributed what, without a second file to find. A plan that cannot be read or rewritten is left alone:
    a fact about the disk must not end a run that is already measuring.
    """
    path = run_dir / "plan.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(plan, dict):
        return
    plan["memory"] = dict(facts)
    try:
        replay._save(path, plan)
    except OSError:
        return


async def scene_after_close(connect: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    """The scene a fresh bridge connection observes after the run's session closed, or the error that stopped it."""
    try:
        client = await connect()
    except Exception as exc:  # noqa: BLE001 - nothing to observe is a recorded fact, not a failure of the run
        return {"scene": None, "error": f"{type(exc).__name__}: {exc}"}
    try:
        observation = await client.observe_once()
        return {"scene": observation.scene, "seq": observation.seq,
                "wall_utc": observation.wall_utc.isoformat() if observation.wall_utc else None}
    except Exception as exc:  # noqa: BLE001
        return {"scene": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await client.stop("finish")
        except Exception:  # noqa: BLE001
            pass
        await client.aclose()


async def _run(project: Path, manifest: PreflightManifest, built: ManifestChoices, objective: str,
               ledger: call_budget.Ledger, approval: dict[str, Any], args: argparse.Namespace,
               manifest_sha: str, catalog_sha: str, calibration: dict[str, Any] | None = None,
               session_host: HostSession | None = None, declared: Mapping[str, Any] | None = None,
               detectors: list[Any] | None = None) -> dict[str, Any]:
    run_dir = args.output.resolve().parent
    started_utc = dt.datetime.now(dt.timezone.utc)
    generation = session_host.generation if session_host is not None else None
    restart = (SessionControl(session_host, project=project)
               if session_host is not None and session_host.advertises_restart else None)
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    try:
        primitives = memory = None
        if args.placement and not args.options:
            raise ValueError("--placement requires --options")
        if args.options:
            from .options.primitives import primitives_from_calibration
            from .options.memory import OutcomeMemory
            primitives = primitives_from_calibration(calibration or {},
                [c.action for c in built.choices if c.kind is ChoiceKind.action and c.action], catalog=built.catalog.actions)
            steps = [d.step for d in primitives.directions if d.step is not None and d.step > 0]
            if not steps:
                raise ValueError("options require a measured short movement step for outcome memory")
            memory = OutcomeMemory(cell=min(steps))
        snapshot = make_snapshot(client, manifest.entity_id)
        await snapshot()  # before any paid call: the manifest's entity must be in the running game
        scene = (await client.observe_once()).scene
        if primitives is not None and args.measure_air_rise:
            from .options.air_rise import measure_air_rise, with_air_rise

            # The rise measured once more in the state it creates (airborne), before any paid call; a hop that
            # presses it again is offered only when that measurably rose higher. The record stays in the run.
            air = await measure_air_rise(client, primitives)
            replay._save(run_dir / "air-rise.json", air.to_json() if air is not None else {"measured": None})
            primitives = with_air_rise(primitives, air)
        memory_facts: dict[str, Any] | None = None
        if memory is not None and args.memory_dir is not None:
            from .options.memory import memory_file

            # The memory lives beside the preflight cache, keyed by the game and the scene rather than by the
            # cache's fingerprint: that fingerprint covers every authored file, so one edited script would orphan
            # what earlier runs measured, while the cells are world coordinates in one scene and stay valid.
            memory_path = memory_file(args.memory_dir, project.name, scene)
            present = memory_path.is_file()
            if present:
                memory = OutcomeMemory.load(memory_path, cell=memory.cell)  # before the first paid call
            memory.autosave(memory_path)
            memory_facts = {"dir": str(args.memory_dir.resolve()), "file": memory_path.name,
                            "loaded": memory.stats() if present else None, "saved": None, "error": None}
            _plan_memory(run_dir, memory_facts)
        readers = FieldReaders(client, manifest.readers) if manifest.readers else None
        if readers is not None:
            await readers.install()  # before any paid call: the manifest's readers must resolve in the running game
        if args.measure_guards:
            from playtest.preflight.catalog import export
            from .guards import PREFIX, measure_guards
            from .options.primitives import primitives_from_calibration

            # Each verb at rest, while moving and in the air, before any paid call, from the catalog this run verified
            # the host serves. A measurement for the review and the frontier; it adds, removes or ranks no choice.
            served = export(manifest, turn_sizes=args.turn_sizes, point_turns=args.point_turns,
                            point_moves=args.point_moves, capture_size=(args.capture_width, args.capture_height),
                            calibration=calibration, guard_chords=True).actions
            guard_primitives = primitives_from_calibration(
                calibration or {}, [n for n, d in served.items() if d.inputs and not n.startswith(PREFIX)],
                catalog={n: d for n, d in served.items() if not n.startswith(PREFIX)})
            guards = await measure_guards(client, readers, served, guard_primitives)
            replay._save(run_dir / "guards.json", guards)
        guard_lines: list[str] = []
        if args.measure_guards:
            from .guards import summary_lines
            guard_lines = summary_lines(guards)
        watch = ReaderWatch({reader.alias: reader.kind for reader in manifest.readers}, manifest.invariants)
        # Detectors are opt-in: without them the loop reports to the readers' watch itself, so a trace is
        # unchanged. With them, the same watch plus the detectors is one watch, numbered in one sequence.
        reader_watch = watch
        if detectors:
            watch = DetectorWatch(reader_watch, detectors)
        extra = IMAGE_TOKENS[IMAGE_TRANSPORT[args.s1_arm]] if args.s1_game_view else 0
        per_minute, first_call = args.tokens_per_minute, INITIAL_ESTIMATE + extra   # the image counts from the first call
        budget = TokenBudget(per_minute, initial_estimate=first_call, spread=True, burst=args.pacing_burst,
                             anchor=args.pacing_anchor)
        instructions = INSTRUCTIONS_VARIANTS[args.s1_instructions]
        s1 = SystemOneS1(ledger, max_calls=args.max_calls, arm=args.s1_arm, budget=budget,
                          game_view=args.s1_game_view, layout=args.s1_layout, instructions=instructions,
                          compact_state=args.s1_compact_state, pending_question=args.s1_pending_question,
                          s1_context=args.s1_context, menu_memory=memory, contingent=args.contingent,
                          requests_dir=run_dir / "requests" / "s1", hold_met_directive=args.hold_met_directive)
        status = client.status
        view = (BridgeView(project, status, started_utc=started_utc, generation=generation)
                if args.s1_game_view else None)
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
            reset_method="restart_play_mode" if session_host is not None else "none")  # this runner only resets through the host
        writer = TraceWriter(run_dir / TRACE_FILE, record, secrets=secret_values())
        if memory is not None and memory_facts is not None:
            memory.note_run(record.run_id)  # the file says which runs contributed, once it is written again
        progress_probe = ProgressProbe(manifest.reach_regions)
        tracker = DirectiveTracker(writer, derived=progress_probe.derived if progress_probe.regions else None)
        facts = ProgressFacts(tracker, progress_probe) if progress_probe.regions else tracker
        if args.coverage_facts:
            facts = CoverageFacts(facts, [c.id for c in built.choices if c.kind in (ChoiceKind.action, ChoiceKind.skill)])
        if args.pattern_facts:
            facts = PatternFacts(facts, [reader.alias for reader in manifest.readers])
        if args.travel_facts:
            facts = TravelFacts(facts)
        help_port = (FrontierHelp(project=project, status=status, manifest=manifest, directory=run_dir,
                                  approval=approval, sink=writer, tracker=tracker,
                                  started_utc=dt.datetime.now(dt.timezone.utc), calibration=calibration,
                                  effects=action_effects(built.expected), generation=generation,
                                  session=getattr(client, "session", None), progress=progress_probe,
                                  clip_window_s=args.frontier_clip_s, declared=declared, guards=guard_lines,
                                  advice=args.frontier_advice)
                     if args.frontier else None)
        closing: tuple[str, str | None] = ("inconclusive", None)
        try:
            result = await run_session(
                client, built, s1=s1, objective=objective, view=view,
                probe=DisplacementProbe(snapshot, built.expected,
                                        snapshot_for=lambda new: make_snapshot(new, manifest.entity_id)),
                readers=readers, watch=watch, sink=writer, max_age_s=args.max_age_s, wall_limit_s=args.wall_limit_s,
                max_provider_failures=args.max_provider_failures, budget=budget, help=help_port,
                facts=facts, s1_call_cap=args.max_calls, restart=restart, generation=generation,
                continuous_input=args.continuous_input, instructions=instructions,
                option_primitives=primitives, option_memory=memory, placement=args.placement,
                reach_regions=manifest.reach_regions,
                keep_help_across_restart=args.keep_help_across_restart,
                decide_during_options=args.decide_during_options)
            closing = (result.status, result.error)
            after_close = None
            if result.error is not None and "closed" in result.error:
                # The bridge closes the session when the active scene changes. One fresh connection reads which
                # scene is running now; a different one is the run's measured ending, never assumed.
                after_close = await scene_after_close(
                    lambda: BridgeClient.connect_from_project(project, timeout_s=10.0))
                if after_close.get("scene") is not None and after_close["scene"] != scene:
                    closing = ("complete", None)
            turns = result.loop.turns
            summary = {
                "version": 1, "status": result.status, "ending": result.ending, "error": result.error,
                "turns": result.turns, "outcomes": result.outcomes, "provider_failures": result.provider_failures,
                "adherence": {"rate": adherence_rate(turns), "violations": [v.__dict__ for v in result.adherence]},
                "stop": result.stop.model_dump(mode="json") if result.stop is not None else None,
                "s1": {"provider": s1.provider, "model": s1.model, "prompt_sha256": s1.prompt_sha256,
                       "calls": s1.calls_reserved, "call_cap": args.max_calls,
                       **({"instructions": args.s1_instructions}
                          if args.s1_instructions != INSTRUCTIONS_POLICY_VARIANT else {}),
                       **({"compact_state": s1.compact_state} if s1.compact_state is not None else {}),
                       **({"pending_question": args.s1_pending_question}
                          if args.s1_pending_question != "follow_up" else {}),
                       **({"context": args.s1_context} if args.s1_context != "full" else {})},
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
                "suspicions": _suspicions(result.loop.suspicions, reader_watch.min_readings,
                                          reader_watch.drift_readings),
                "settings": {"max_age_s": args.max_age_s, "wall_limit_s": args.wall_limit_s,
                             "max_provider_failures": args.max_provider_failures,
                             "tokens_per_minute": args.tokens_per_minute},
                "pacing": {**dataclasses.asdict(result.pacing), "calls_counted": budget.recorded,
                           "calls_estimated": budget.estimated},
                "session_control": None if session_host is None else {
                    "run_id": session_host.run_id,
                    "generation": result.loop.generation,
                    "capabilities": list(session_host.capabilities),
                    "advertises_restart": session_host.advertises_restart,
                    "restarts": [operation.model_dump(mode="json")
                                 for operation in result.restart_history],
                },
                "trace": TRACE_FILE,
            }
            if after_close is not None:
                summary["scene_after_close"] = after_close
                if after_close.get("scene") is not None and after_close["scene"] != scene:
                    summary.update(status="complete", ending="scene_changed", error=None,
                                   scene_changed={"from": scene, "to": after_close["scene"],
                                                  "closed_with": result.error})
        except BaseException as exc:  # the loop has stopped and released its inputs; the trace still gets its end
            closing = ("inconclusive", _describe(exc))
            raise
        finally:
            writer.close(*closing)
            if memory is not None and memory_facts is not None:
                # The file already has every outcome this run recorded; this last write also carries the run id,
                # and the plan states what was saved even when a write failed along the way.
                try:
                    memory.save()
                except OSError as exc:
                    memory_facts["error"] = f"{type(exc).__name__}: {exc}"
                memory_facts["saved"] = memory.stats()
                _plan_memory(run_dir, memory_facts)
        summary["redactions"] = writer.redactions
        return summary
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(main())
