"""Reproduce one logged runtime error k of n times, each time after a scene reload.

This is a code-driven probe, not an S1 turn, in the family of calibration and stage 7. The caller states
a plan: one keyboard control, how long to hold it, how many scene reloads to try, how many times to press
the control after each, and a predicate naming the logged runtime error that counts as the reproduction.
The Editor harness hosts one action for that control and reloads the first enabled scene when asked.

For each attempt the client asks for reload number i through numbered handshake files, reconnects (a scene
change ends the previous bridge session), watches the idle scene, then presses the control through the
bridge's normal `act` path and reads the runtime errors the game logged in the window after each press.
Every press is sent once. Nothing here chooses, ranks, substitutes, retries or repairs an action, and
nothing here interprets a result beyond the predicate the caller wrote. The game clock is never paused,
stepped or scaled: every wait is wall-clock time.

Windows follow `calibrate.error_window`. A window is every error after the monitor's counter read the value
taken at its opening read, so its count is exact even when the monitor kept only some of the messages;
`complete` says whether the retained events account for every counted error. A monitor that is off or
inconsistent leaves the window unknown: the count is `null`, never zero. A press window is matched when any
retained event matches the predicate, unmatched when it is complete and none does, and unknown when it is
incomplete and none does. Unknown is not failure.

An attempt is valid only when its reload completed, its idle window is known and holds no error, and its
first press executed with verified release and has a window known well enough to say whether the error
appeared. Invalid attempts are listed with their reasons. They are never counted in n and never imputed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError, read_project_lock
from playtest.preflight import calibrate


SCHEMA_VERSION = 1
RESET = "scene_reload"
PLAN_KEYS = ("schema_version", "control", "hold_s", "attempts", "presses", "settle_s", "press_interval_s",
             "window_s", "predicate")
PREDICATE_KEYS = ("type", "message_contains", "stack_contains")
CONTROL_PATTERN = re.compile(r"^[a-z0-9]+$")
HOLD_S_RANGE = (0.02, 2.0)
ATTEMPTS_RANGE = (1, 20)
PRESSES_RANGE = (1, 10)
MIN_SETTLE_S = 0.5
MIN_PRESS_INTERVAL_S = 0.2
MIN_WINDOW_S = 0.1
# A wait this long is a typo, not a plan; it would also outlast the Editor host's budget for the run.
MAX_WAIT_S = 60.0
ACTION_PREFIX = "repro_"
MAX_AGE_S = 2.0
RELOAD_TIMEOUT_S = 120.0
CLIENT_TIMEOUT_S = 120.0
HANDSHAKE_POLL_S = 0.05
HOLD_TOLERANCE_S = 1e-6
# Bounds on what one window carries into the report. The counts stay exact; only the listed messages are capped.
LISTED_EVENTS = 16
STACK_LINES = 8
STACK_LINE_CHARS = 300
MESSAGE_CHARS = 1000


@dataclass(frozen=True)
class Predicate:
    """The logged error that counts as the reproduction. All three parts must hold for one event."""

    type: str
    message_contains: str
    stack_contains: str

    def as_dict(self) -> dict[str, str]:
        return {"type": self.type, "message_contains": self.message_contains,
                "stack_contains": self.stack_contains}


@dataclass(frozen=True)
class Plan:
    """A validated reproduction plan. Every value was stated by the caller; nothing is defaulted."""

    control: str
    hold_s: float
    attempts: int
    presses: int
    settle_s: float
    press_interval_s: float
    window_s: float
    predicate: Predicate

    @property
    def action(self) -> str:
        """The one action the Editor host registers for this plan's control."""
        return ACTION_PREFIX + self.control

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "control": self.control, "hold_s": self.hold_s,
                "attempts": self.attempts, "presses": self.presses, "settle_s": self.settle_s,
                "press_interval_s": self.press_interval_s, "window_s": self.window_s,
                "predicate": self.predicate.as_dict()}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


# --- plan -----------------------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        raise ValueError("the plan repeats a key: " + ", ".join(repeated))
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise ValueError(f"the plan contains {name}, which is not JSON")


def _object(value: Any, keys: tuple[str, ...], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a JSON object")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise ValueError(f"{where} has unknown keys: " + ", ".join(unknown))
    missing = [key for key in keys if key not in value]
    if missing:
        raise ValueError(f"{where} is missing: " + ", ".join(missing))
    return value


def _number(value: Any, name: str, low: float | None = None, high: float | None = None) -> float:
    number = _finite(value)
    if number is None:
        raise ValueError(f"{name} must be a finite number")
    if (low is not None and number < low) or (high is not None and number > high):
        bound = (f"at least {low:g}" if high is None else f"at most {high:g}" if low is None
                 else f"between {low:g} and {high:g}")
        raise ValueError(f"{name} must be {bound}")
    return number


def _whole_number(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def parse_plan(text: str) -> Plan:
    """Validate a plan document. Unknown or missing keys, duplicate keys and out-of-range values are errors."""
    try:
        document = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the plan is not valid JSON: {exc}") from None
    fields = _object(document, PLAN_KEYS, "the plan")
    version = fields["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be the whole number {SCHEMA_VERSION}")
    control = fields["control"]
    if not isinstance(control, str) or not CONTROL_PATTERN.fullmatch(control):
        raise ValueError("control must be a lower-case keyboard key token matching ^[a-z0-9]+$")
    if control.isdigit():
        raise ValueError(f"control {control!r} is a bare number, which the bridge rejects as a key name; "
                         "write a digit key as digitN, for example digit1")
    predicate = _object(fields["predicate"], PREDICATE_KEYS, "predicate")
    return Plan(
        control=control,
        hold_s=_number(fields["hold_s"], "hold_s", *HOLD_S_RANGE),
        attempts=_whole_number(fields["attempts"], "attempts", *ATTEMPTS_RANGE),
        presses=_whole_number(fields["presses"], "presses", *PRESSES_RANGE),
        settle_s=_number(fields["settle_s"], "settle_s", MIN_SETTLE_S, MAX_WAIT_S),
        press_interval_s=_number(fields["press_interval_s"], "press_interval_s", MIN_PRESS_INTERVAL_S, MAX_WAIT_S),
        window_s=_number(fields["window_s"], "window_s", MIN_WINDOW_S, MAX_WAIT_S),
        predicate=Predicate(type=_text(predicate["type"], "predicate.type"),
                            message_contains=_text(predicate["message_contains"], "predicate.message_contains"),
                            stack_contains=_text(predicate["stack_contains"], "predicate.stack_contains")),
    )


def read_plan(path: Path) -> tuple[Plan, str]:
    """The validated plan and the SHA-256 of the exact bytes it was read from."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("the plan is not valid UTF-8") from None
    return parse_plan(text), hashlib.sha256(raw).hexdigest()


# --- runtime-error windows ---------------------------------------------------------


def event_matches(predicate: Predicate, event: dict[str, Any]) -> bool:
    """One logged event matches when its type is equal and its message and stack contain the caller's text."""
    kind, message, stack = event.get("type"), event.get("message"), event.get("stack")
    return (isinstance(kind, str) and isinstance(message, str) and isinstance(stack, str)
            and kind == predicate.type and predicate.message_contains in message
            and predicate.stack_contains in stack)


def _event_record(event: dict[str, Any], occurrences: int, first: int, last: int, previous: int,
                  predicate: Predicate) -> dict[str, Any]:
    """One retained event as the report keeps it.

    The monitor merges consecutive identical errors into one run, and a run can begin before this window.
    `count` and the sequences are the window's own: its first error is `previous + 1`. The run's own first
    sequence is kept as `run_first_sequence`.
    """
    stack = event.get("stack")
    lines = stack.splitlines() if isinstance(stack, str) else []
    return {"type": calibrate._clip(event.get("type"), 32),
            "message": calibrate._clip(event.get("message"), MESSAGE_CHARS),
            "stack_lines": [line[:STACK_LINE_CHARS] for line in lines[:STACK_LINES]],
            "stack_lines_total": len(lines) if isinstance(stack, str) else None,
            "count": occurrences, "first_sequence": max(first, previous + 1), "last_sequence": last,
            "run_first_sequence": first, "matches": event_matches(predicate, event)}


def _window_events(monitor: dict[str, Any], previous: int, predicate: Predicate) -> list[dict[str, Any]]:
    """The retained events with an error after `previous`, each with only its occurrences inside the window."""
    records = []
    for event in monitor["events"]:
        first, last = event["first_sequence"], event["last_sequence"]
        if last <= previous:
            continue
        run = last - max(first, previous + 1) + 1
        recorded = calibrate._whole(event.get("count"))
        records.append(_event_record(event, run if recorded is None else min(run, recorded), first, last,
                                     previous, predicate))
    return records


def error_window(monitor: Any, previous: int | None, predicate: Predicate) -> dict[str, Any]:
    """The runtime errors one window saw, judged against the predicate.

    The count, `sampled` and `complete` are `calibrate.error_window`'s, so they mean exactly what they mean
    for calibration. The retained events are kept one by one, because the predicate may need an event's own
    stack. A window that is not available has no count, no events and no match.
    """
    base = calibrate.error_window(monitor, previous)
    window: dict[str, Any] = {key: base[key] for key in (
        "available", "reason", "start_observed", "end_observed", "count", "sampled", "complete",
        "monitor_limit", "note")}
    truncated = monitor.get("truncated") if isinstance(monitor, dict) else None
    window["monitor_truncated"] = truncated if isinstance(truncated, bool) else None
    events = _window_events(monitor, previous, predicate) if base["available"] else []
    matching = [event for event in events if event["matches"]]
    window.update(
        matching_events=len(matching) if base["available"] else None,
        matching_count=sum(event["count"] for event in matching) if base["available"] else None,
        non_matching_count=(window["sampled"] - sum(event["count"] for event in matching))
        if base["available"] else None,
        unretained_count=(window["count"] - window["sampled"]) if base["available"] else None,
        events_total=len(events) if base["available"] else None)
    listed = sorted(events, key=lambda event: (not event["matches"], event["first_sequence"]))[:LISTED_EVENTS]
    window["events"] = sorted(listed, key=lambda event: event["first_sequence"])
    return window


def press_match(window: dict[str, Any]) -> bool | None:
    """Whether the press window held the predicate's error: True, False, or None when that is unknown."""
    if window.get("available") is not True:
        return None
    if window["matching_events"] > 0:
        return True
    return False if window.get("complete") is True else None


@dataclass(frozen=True)
class Read:
    """One monitor read: the compact summary the report keeps and the raw monitor the windows are built from."""

    summary: dict[str, Any]
    monitor: Any
    observed: int | None


async def read_monitor(client: Any) -> Read:
    """Read the runtime-error monitor. The read is a window boundary; only the monitor is used from the reply."""
    snapshot = await client.inspect("transforms")
    data = snapshot.data if isinstance(snapshot.data, dict) else {}
    monitor = data.get("runtime_errors")
    state = monitor if isinstance(monitor, dict) else {}
    active, truncated, events = state.get("active"), state.get("truncated"), state.get("events")
    observed = calibrate._whole(state.get("observed")) if active is True else None
    summary = {
        "wall_utc": snapshot.wall_utc.isoformat(),
        "frame": calibrate._whole(data.get("frame")),
        "real_time_s": _finite(data.get("real_time_s")),
        "game_time_s": _finite(data.get("game_time_s")),
        "inspection_error_count": len(snapshot.errors) if isinstance(snapshot.errors, list) else None,
        "monitor": {"active": active if isinstance(active, bool) else None, "observed": observed,
                    "limit": calibrate._whole(state.get("limit")),
                    "truncated": truncated if isinstance(truncated, bool) else None,
                    "events_retained": len(events) if isinstance(events, list) else None},
    }
    return Read(summary, monitor, observed)


# --- reload handshake ----------------------------------------------------------------


def reload_paths(directory: Path, number: int) -> tuple[Path, Path]:
    """The request this client writes and the completion the Editor harness writes for reload `number`."""
    return directory / f"reload-{number}.request.json", directory / f"reload-{number}.done.json"


def _host_evidence(done: Any, plan: Plan, number: int) -> dict[str, Any]:
    """Require the harness to say it reloaded this request, hosting exactly this plan's action."""
    if not isinstance(done, dict):
        raise ValueError("the scene-reload evidence is not a JSON object")
    if done.get("method") != RESET or done.get("reloaded") is not True:
        raise ValueError("the Editor harness did not report a completed scene reload")
    answered = done.get("number")
    if isinstance(answered, bool) or answered != number:
        raise ValueError(f"the Editor harness answered reload {answered!r}, not reload {number}")
    host = done.get("action")
    if not isinstance(host, dict) or host.get("name") != plan.action or host.get("control") != plan.control:
        raise ValueError("the Editor host's action name or physical control does not match the plan")
    lease = _finite(host.get("default_lease_s"))
    if lease is None or abs(lease - plan.hold_s) > HOLD_TOLERANCE_S:
        raise ValueError("the Editor host's lease does not match the plan's hold")
    scene_path = done.get("scene_path")
    if not isinstance(scene_path, str) or not scene_path:
        raise ValueError("the Editor harness did not report the reloaded scene")
    return {"completed": True, "method": RESET, "number": number, "scene_path": scene_path, "action": host,
            **{key: done.get(key) for key in ("requested_at_utc", "completed_at_utc", "game_time_s",
                                              "real_time_s", "frame")}}


async def request_reload(directory: Path, plan: Plan, number: int, timeout_s: float) -> dict[str, Any]:
    """Ask the Editor harness for reload `number` and wait for its completion.

    The request is published whole or not at all, because the harness reads it from another process. The
    completion is read the same way. A request is made once; a timeout is an error, not a reason to ask again.
    """
    request_path, done_path = reload_paths(directory, number)
    request = {"schema_version": SCHEMA_VERSION, "number": number,
               "action": {"name": plan.action, "control": plan.control, "requested_hold_s": plan.hold_s},
               "requested_at_utc": _utcnow()}
    temporary = request_path.with_name(request_path.name + ".writing")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(request, stream, indent=2, allow_nan=False)
        stream.write("\n")
    if request_path.exists() or done_path.exists():
        raise ValueError(f"the scene-reload handshake files for reload {number} already exist")
    temporary.replace(request_path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not done_path.exists():
        if loop.time() >= deadline:
            raise TimeoutError(f"scene reload {number} did not complete within {timeout_s:g} s")
        await asyncio.sleep(HANDSHAKE_POLL_S)
    try:
        done = json.loads(done_path.read_text(encoding="utf-8"), parse_constant=_no_constant)
    except (OSError, ValueError) as exc:
        raise ValueError(f"the scene-reload evidence for reload {number} is not readable JSON: {exc}") from None
    return _host_evidence(done, plan, number)


# --- attempts --------------------------------------------------------------------------


def _observation_summary(observation: Any) -> dict[str, Any]:
    return {"seq": observation.seq, "scene": getattr(observation, "scene", None),
            "playing": getattr(observation, "playing", None), "paused": getattr(observation, "paused", None),
            "time_scale": getattr(observation, "time_scale", None), "frame": getattr(observation, "frame", None),
            "clock_mode": _enum_value(getattr(observation, "clock_mode", None))}


def _press_reason(press: dict[str, Any]) -> str:
    """Why a press is not a measurement, in the words of what the bridge reported."""
    status = press["status"]
    if status == "rejected":
        return (f"the bridge rejected the press ({press['reject_reason']}); it was not retried or replaced")
    if status != "completed":
        return f"the press ended {status}, not completed; it was not retried or replaced"
    return "the press completed but its input release was not verified; it was not retried or replaced"


async def _press(client: Any, plan: Plan, press: dict[str, Any], opening: Read) -> Read:
    """Send one press and close its window. `press` is filled in as the facts arrive, so a failure keeps them."""
    observation = await client.observe_once()
    press["observation"] = _observation_summary(observation)
    press["based_on_seq"] = observation.seq
    action_id = f"repro-{uuid.uuid4().hex}"
    press["action_id"] = action_id
    stamps = press["timestamps"]
    stamps["act_requested_wall_utc"] = _utcnow()
    # Exactly one act per press: a rejection or an unverified release is recorded, never retried or replaced.
    records = await client.act(action_id=action_id, choice_id=action_id, action=plan.action,
                               based_on_seq=observation.seq, max_age_s=MAX_AGE_S, lease_s=plan.hold_s)
    stamps["act_returned_wall_utc"] = _utcnow()
    terminal = records[-1]
    press.update(
        records=[record.model_dump(mode="json") for record in records],
        status=_enum_value(terminal.status),
        reject_reason=_enum_value(getattr(terminal, "reject_reason", None)),
        release_verified=terminal.release_verified,
        applied_duration_s=getattr(terminal, "applied_duration_s", None),
        input_start_wall_s=getattr(terminal, "input_start_wall_s", None),
        input_end_wall_s=getattr(terminal, "input_end_wall_s", None))
    press["executed"] = press["status"] == "completed" and terminal.release_verified is True
    await asyncio.sleep(plan.window_s)
    closing = await read_monitor(client)
    stamps["window_closed_wall_utc"] = _utcnow()
    window = error_window(closing.monitor, opening.observed, plan.predicate)
    press["window"] = window
    if not press["executed"]:
        press["reason"] = _press_reason(press)
    elif window["available"] is not True:
        press["reason"] = f"the press window is unknown: {window['reason']}"
    else:
        press.update(valid=True, matched=press_match(window),
                     reason="the press executed with verified release and its window was measured")
    return closing


def _new_press(number: int, plan: Plan) -> dict[str, Any]:
    """A press that has not happened yet: every fact is unknown until the bridge reports it."""
    return {"number": number, "valid": False, "executed": False, "matched": None,
            "reason": "the press did not finish", "action": plan.action, "control": plan.control,
            "requested_hold_s": plan.hold_s, "lease_s": plan.hold_s, "action_id": None, "based_on_seq": None,
            "observation": None, "records": [], "status": None, "reject_reason": None, "release_verified": None,
            "applied_duration_s": None, "input_start_wall_s": None, "input_end_wall_s": None, "window": None,
            "timestamps": {}}


def _idle_is_clean(idle: dict[str, Any] | None) -> bool:
    return idle is not None and idle["available"] is True and idle["count"] == 0


def _judge(attempt: dict[str, Any]) -> None:
    """Decide whether the attempt is a measurement, and say why not when it is not.

    Only the rules in the module docstring decide. A failure after the first press was measured ends the run
    but leaves that measurement standing; the error stays on the attempt and in the report.
    """
    reasons: list[str] = []
    reload = attempt["reload"]
    idle, presses = attempt["idle"], attempt["presses"]
    if not isinstance(reload, dict) or reload.get("completed") is not True:
        reasons.append("the scene reload did not complete")
    elif idle is None:
        reasons.append("the idle window was not measured")
    elif idle["available"] is not True:
        reasons.append(f"the idle window is unknown: {idle['reason']}")
    elif idle["count"] != 0:
        reasons.append(f"the game logged {idle['count']} runtime error(s) in the idle window, before any input")
    elif not presses:
        reasons.append("no press was made")
    else:
        first = presses[0]
        if first["valid"] is not True:
            reasons.append(f"the first press is not a measurement: {first['reason']}")
        elif first["matched"] is None:
            reasons.append("the first press window was incomplete and no retained error matched, so whether the "
                           "error appeared is unknown")
    if reasons and attempt["error"] is not None:
        reasons.append(f"the probe stopped: {attempt['error']}")
    attempt["reasons"] = reasons
    attempt["valid"] = not reasons
    attempt["first_press_matched"] = presses[0]["matched"] if attempt["valid"] else None


async def run_attempt(plan: Plan, number: int, reload: Callable[[int], Awaitable[dict[str, Any]]],
                      connect: Callable[[], Awaitable[Any]]) -> tuple[dict[str, Any], bool]:
    """One reload, one fresh session, one idle window and the presses. Returns the record and whether to go on.

    Any failure of the reload handshake or the bridge ends the run, and the attempts already made are kept.
    The error stays on the attempt, which is valid only if the rules in `_judge` still hold without the part
    the failure cut off.
    """
    attempt: dict[str, Any] = {
        "number": number, "valid": False, "reasons": [], "error": None, "reload": None, "session": None,
        "reads": [], "idle": None, "presses": [], "first_press_matched": None, "stop": None,
        "stop_error": None, "timestamps": {"started_wall_utc": _utcnow()}}
    stamps = attempt["timestamps"]
    client = None
    proceed = True
    try:
        stamps["reload_requested_wall_utc"] = _utcnow()
        attempt["reload"] = await reload(number)
        stamps["reload_completed_wall_utc"] = _utcnow()
        # A scene change ended the previous session, so every attempt opens its own.
        client = await connect()
        attempt["session"] = getattr(client, "session", None)
        opening = await read_monitor(client)
        attempt["reads"].append(opening.summary)
        await asyncio.sleep(plan.settle_s)
        closing = await read_monitor(client)
        attempt["reads"].append(closing.summary)
        attempt["idle"] = error_window(closing.monitor, opening.observed, plan.predicate)
        if _idle_is_clean(attempt["idle"]):
            boundary = closing
            for press_number in range(1, plan.presses + 1):
                if press_number > 1:
                    await asyncio.sleep(plan.press_interval_s)
                press = _new_press(press_number, plan)
                attempt["presses"].append(press)
                boundary = await _press(client, plan, press, boundary)
                attempt["reads"].append(boundary.summary)
                if press["valid"] is not True:
                    break
    except (BridgeError, BridgeLockError, ValueError, OSError, TimeoutError) as exc:
        attempt["error"], proceed = str(exc) or type(exc).__name__, False
    except Exception as exc:  # noqa: BLE001 - the report must be written whatever failed
        attempt["error"], proceed = f"unexpected {type(exc).__name__}: {exc}", False
    finally:
        if client is not None:
            try:
                stop = await client.aclose()
                attempt["stop"] = stop.model_dump(mode="json") if stop is not None else None
            except Exception as exc:  # noqa: BLE001 - closing must not lose the report; the stop stays unknown
                attempt["stop_error"] = f"{type(exc).__name__}: {exc}"
    stamps["finished_wall_utc"] = _utcnow()
    _judge(attempt)
    return attempt, proceed


# --- report ------------------------------------------------------------------------------


def summarize(plan: Plan, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """k of n over the valid attempts, with press-level counts and the errors that did not match."""
    valid = [attempt for attempt in attempts if attempt["valid"] is True]
    n = len(valid)
    presses = [press for attempt in valid for press in attempt["presses"]]
    counted = [press for press in presses if press["valid"] is True]
    windows = [press["window"] for press in counted]
    return {
        "planned": plan.attempts, "attempted": len(attempts), "valid": n,
        "k": sum(attempt["first_press_matched"] is True for attempt in valid) if n else None, "n": n,
        "basis": ("k is the valid attempts whose first press window held the predicate's error; n is the valid "
                  "attempts; an invalid attempt is excluded from both and never imputed"),
        "invalid_attempts": [{"number": attempt["number"], "reasons": list(attempt["reasons"])}
                             for attempt in attempts if attempt["valid"] is not True],
        "presses": {"scope": "presses in valid attempts",
                    "reported": sum(press["status"] is not None for press in presses), "valid": len(counted),
                    "matched": sum(press["matched"] is True for press in counted),
                    "unmatched": sum(press["matched"] is False for press in counted),
                    "unknown": sum(press["matched"] is None for press in counted)},
        "non_matching_errors": sum(window["non_matching_count"] for window in windows) if windows else None,
        "unretained_errors": sum(window["unretained_count"] for window in windows) if windows else None,
        "error_counts_note": ("non_matching_errors counts the retained errors in valid press windows that do not "
                              "match; unretained_errors counts errors the monitor counted but no longer retains, "
                              "which could be of either kind"),
    }


def _status(summary: dict[str, Any], error: str | None) -> tuple[str, str]:
    n, k, planned = summary["n"], summary["k"], summary["planned"]
    ended = f"; the run ended early: {error}" if error is not None else ""
    if n == 0:
        return "inconclusive", (f"none of the {summary['attempted']} attempt(s) made was a valid measurement; "
                                f"see the reasons on each attempt{ended}")
    held = f"{k} of {n} valid attempts held the predicate's error after a scene reload"
    if n == planned and error is None:
        return "measured", held
    missing = f"{planned - n} of the {planned} planned attempts are missing or invalid"
    return "measured_with_gaps", f"{held}; {missing}{ended}"


def _clock_evidence(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    seen = [press["observation"] for attempt in attempts for press in attempt["presses"] if press["observation"]]

    def distinct(key: str) -> list[Any]:
        return sorted({item[key] for item in seen if item.get(key) is not None}, key=str)

    return {"stated": {"mode": "real_time", "paused": False,
                       "statement": ("the probe never pauses, steps or scales the game clock; every wait is "
                                     "wall-clock time")},
            "observed": {"presses_observed": len(seen), "paused": distinct("paused"),
                         "time_scale": distinct("time_scale"), "clock_mode": distinct("clock_mode"),
                         "note": "what each press's observation reported; empty when no press was observed"}}


def _base_report(plan: Plan, plan_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": "scene_reload_reproduction",
        "is_s1_turn": False,
        "model_calls": 0,
        "claim_scope": ("how often the caller's predicate matched a runtime error logged in the window after the "
                        "first press of one keyboard control, each time after a scene reload; it establishes no "
                        "cause, and says nothing about any other input, reset or build"),
        "status": "inconclusive",
        "status_reason": "the probe did not finish",
        "error": None,
        "plan": plan.as_dict(),
        "plan_sha256": plan_sha256,
        "reset": RESET,
        "clock": None,
        "action": {"name": plan.action, "device": "Keyboard", "control": plan.control, "lease_s": plan.hold_s},
        "attempts": [],
        "summary": None,
        "timestamps": {"started_wall_utc": _utcnow()},
    }


async def measure(plan: Plan, plan_sha256: str, reload: Callable[[int], Awaitable[dict[str, Any]]],
                  connect: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    """Run the plan's attempts in order and return the report. It does not raise for a failed attempt."""
    report = _base_report(plan, plan_sha256)
    for number in range(1, plan.attempts + 1):
        attempt, proceed = await run_attempt(plan, number, reload, connect)
        report["attempts"].append(attempt)
        if attempt["error"] is not None and report["error"] is None:
            report["error"] = f"attempt {number}: {attempt['error']}"
        if not proceed:
            break
    report["summary"] = summarize(plan, report["attempts"])
    report["status"], report["status_reason"] = _status(report["summary"], report["error"])
    report["clock"] = _clock_evidence(report["attempts"])
    report["timestamps"]["finished_wall_utc"] = _utcnow()
    return report


async def _run(project: Path, plan: Plan, plan_sha256: str, handshake: Path,
               reload_timeout_s: float) -> dict[str, Any]:
    async def reload(number: int) -> dict[str, Any]:
        return await request_reload(handshake, plan, number, reload_timeout_s)

    async def connect() -> BridgeClient:
        return await BridgeClient.connect_from_project(project, timeout_s=CLIENT_TIMEOUT_S)

    return await measure(plan, plan_sha256, reload, connect)


# --- command line --------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Replace a non-finite number with null so the report is always valid JSON: unknown, not a guess."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, required=True, help="Unity project with a running Editor bridge")
    parser.add_argument("--plan", type=Path, required=True, help="the caller's reproduction plan (JSON)")
    parser.add_argument("--output", type=Path, required=True, help="new report JSON outside the Unity project")
    parser.add_argument("--handshake-dir", type=Path, required=True,
                        help="new or empty directory outside the Unity project for the numbered reload handshake")
    parser.add_argument("--reload-timeout", type=float, default=RELOAD_TIMEOUT_S,
                        help="maximum seconds to wait for each scene reload")
    args = parser.parse_args(argv)

    project, plan = args.project.resolve(), args.plan.resolve()
    output, handshake = args.output.resolve(), args.handshake_dir.resolve()
    if not project.is_dir():
        parser.error("--project must be an existing Unity project directory")
    if not plan.is_file():
        parser.error("--plan must be an existing file")
    if not math.isfinite(args.reload_timeout) or args.reload_timeout <= 0:
        parser.error("--reload-timeout must be finite and positive")
    for path in (output, handshake):
        if path.is_relative_to(project):
            parser.error("the report and the handshake directory must be outside the Unity project")
    if output.exists():
        parser.error("the report path must be new")
    if handshake.exists() and (not handshake.is_dir() or any(handshake.iterdir())):
        parser.error("--handshake-dir must be new or empty")
    if output.is_relative_to(handshake):
        parser.error("the report must not be inside the handshake directory")
    return args


def _write_report(path: Path, report: dict[str, Any]) -> None:
    text = json.dumps(_json_safe(report), indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def main(argv: list[str] | None = None) -> int:
    """Exit 0 once the report is written, whatever it says; exit 1 only when the run could not be set up."""
    args = _parse_args(argv)
    project, output, handshake = args.project.resolve(), args.output.resolve(), args.handshake_dir.resolve()
    try:
        plan, plan_sha256 = read_plan(args.plan.resolve())
    except (OSError, ValueError) as exc:
        print(f"reproduction plan rejected: {exc}", file=sys.stderr)
        return 1
    try:
        read_project_lock(project)
    except BridgeLockError as exc:
        print(f"no running bridge to reproduce against: {exc}", file=sys.stderr)
        return 1
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        handshake.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"could not create the output directories: {exc}", file=sys.stderr)
        return 1
    report = asyncio.run(_run(project, plan, plan_sha256, handshake, args.reload_timeout))
    report["sources"] = {"project": str(project), "plan": str(args.plan.resolve())}
    try:
        _write_report(output, report)
    except (OSError, ValueError) as exc:
        print(f"could not save the reproduction report: {exc}", file=sys.stderr)
        return 1
    summary = report["summary"]
    print(f"reproduction report saved to {output}: {report['status']} "
          f"(k={summary['k']} n={summary['n']} attempted={summary['attempted']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
