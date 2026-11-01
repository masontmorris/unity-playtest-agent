"""Measure preflight control effects, sampled field freshness, and clock behavior."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import re
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playtest.contracts import action_catalog as catalog_contract
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError
from playtest.preflight.bindings import binding_set, measured_text
from playtest.preflight.entity import _complete, _norm, _objects, _sub, _vector, candidate_controls
from playtest.contracts.scene_ids import unique_by_scene_free_id


HOLD_S = (0.05, 0.1, 0.2, 0.4, 1.2)
REPEATS = 2
FIELD_REPEATS = 4
MAX_ACTIONS = 32
EFFECT_TOLERANCE = 0.03
IDLE_TOLERANCE = 0.01
QUIET_INTERVALS = 2
FIELD_INTERVAL_S = 0.1
FIELD_SAMPLES = 6
IDLE_SAMPLES = 8
MAX_KEYBOARD_STATE_AGE_S = 0.1
# The wall-clock budget scales with the plan: the held seconds, the planned trials and a fixed part.
# It is an allowance for noticing a stuck run, not a target. Measured on FPSKit: a keyboard-only
# calibration (98 trials) took 0.60-0.68 s per trial on average beyond the requested hold and 1.28 s at
# most; the calibration of 23 controls (254 trials, 1.18 s per trial overall) took 0.65-0.67 s on average
# and 1.55 s at most. The two idle windows took 1.2-1.8 s each and the clock steps about 1 s. The per-trial
# allowance is about twice the largest overhead measured and the fixed one is many times the fixed phases.
# The record states the budget it used and the time it took, so the allowances can be checked on the next run.
TRIAL_ALLOWANCE_S = 3.0
FIXED_ALLOWANCE_S = 60.0
KEY_PATH = re.compile(r"^<Keyboard>/([a-zA-Z0-9]+)$", re.IGNORECASE)
MOUSE_PATH = re.compile(r"^<Mouse>/([a-zA-Z0-9]+)$", re.IGNORECASE)
GAMEPAD_PATH = re.compile(r"^<Gamepad>/([a-zA-Z0-9]+)$", re.IGNORECASE)
MODIFIER_KEYS = {"shift": "leftshift", "ctrl": "leftctrl", "alt": "leftalt"}
MOUSE_CONTROLS = frozenset({catalog_contract.MOUSE_DELTA}) | catalog_contract.MOUSE_BUTTONS
GAMEPAD_VECTORS = catalog_contract.GAMEPAD_VECTORS
GAMEPAD_ONE_DIMENSIONAL = catalog_contract.GAMEPAD_ONE_DIMENSIONAL
GAMEPAD_BUTTONS = catalog_contract.GAMEPAD_BUTTONS
# The calibration harness declares one reproducible direction per discovered vector control. It
# does not claim the other directions, magnitude or dead zone were measured.
DECLARED_VECTOR_X = 1.0
DECLARED_VECTOR_Y = 0.0
# Bounded evidence kept for a window's runtime errors. The counts are exact; these only limit how
# many messages and how much text the record carries.
ERROR_SAMPLE_KINDS = 8
ERROR_TEXT_CHARS = 240
ERROR_STACK_CHARS = 400
# The clock phase is read at these points, in order; each window after the first read is named for the
# read that closes it. The clock is paused through `to_resume`, resumed during `resume`, running in `running`.
CLOCK_READS = ("pre_pause", "at_pause", "while_paused", "after_step", "to_resume", "resume", "running")
CLOCK_WINDOWS = CLOCK_READS[1:]
CLOCK_WINDOW_MEANING = {
    "at_pause": "from the read before the pause request to the first read that saw the clock paused",
    "while_paused": "the 0.2 s the clock stayed paused",
    "after_step": "from the paused read through the 0.2 s game-time step to the read that followed it; paused",
    "to_resume": "from that read to the read taken just before the resume request; still paused",
    "resume": "from that read through the resume request to the first read that saw the clock running",
    "running": "0.2 s of wall time with the clock running again",
}
RUNNING_OBSERVATION_S = 0.2
# What the view measurement is, kept in the record's plan so a reader of it does not have to find this code.
VIEW_TURN_PLAN = {
    "source": "the bridge's observation `view`: the game-view camera's world yaw and pitch, in degrees",
    "before": "the observation that bound the action, taken after the entity was still",
    "after": "an observation taken after the same 0.08 s settle as the entity's read, once the input had been released",
    "yaw": "the change wrapped to [-180, 180); positive turns right",
    "pitch": "the plain change; positive looks down",
    "baseline": "none: the change is not corrected for any turn the view makes with no input",
    "unknown": "a trial whose view was not read both times has no view change; it is not a turn of zero",
    "limit": ("two readings cannot show a turn of 180 degrees or more in one apply: it wraps and reads as a smaller "
              "one, so such repeats scatter; each hold's spread_deg is how far its trials disagree"),
}


def wrap_degrees(angle: float) -> float:
    """An angle difference as the equivalent turn in [-180, 180): a heading that passes the seam turns little."""
    return (angle + 180.0) % 360.0 - 180.0


def view_reading(observation: Any) -> dict[str, Any] | None:
    """The game view an observation reported, with where it came from, or None when it reported none.

    An observation with no view (a bridge that has no game-view camera, a camera at a pole, an older bridge)
    is unknown. It is never a heading of zero, and nothing is carried over from an earlier reading.
    """
    view = getattr(observation, "view", None)
    yaw, pitch = getattr(view, "yaw_deg", None), getattr(view, "pitch_deg", None)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (yaw, pitch)):
        return None
    return {"seq": getattr(observation, "seq", None), "frame": getattr(observation, "frame", None),
            "yaw_deg": float(yaw), "pitch_deg": float(pitch)}


def view_turn(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, float] | None:
    """How far the view turned between two readings, or None unless both were measured.

    Yaw is wrapped to [-180, 180): from 179 to -179 degrees is a turn of +2, not -358. Pitch is bounded by
    the poles, so its change needs no wrapping. A positive yaw turns right and a positive pitch looks down.
    """
    if before is None or after is None:
        return None
    return {"yaw_deg": wrap_degrees(after["yaw_deg"] - before["yaw_deg"]),
            "pitch_deg": after["pitch_deg"] - before["pitch_deg"]}


def summarize_view_turn(turns: list[dict[str, float]]) -> dict[str, Any] | None:
    """The mean view change of the trials that measured one, with how far they disagree; None for no trial.

    The spread is the largest difference between any two trials along yaw or pitch. A turn of 180 degrees or
    more in one apply wraps and is read as a smaller one, so repeats of it scatter; the spread keeps that
    visible beside the mean instead of leaving a number that looks measured and is not.
    """
    if not turns:
        return None
    yaws, pitches = [turn["yaw_deg"] for turn in turns], [turn["pitch_deg"] for turn in turns]
    return {"trials": len(turns), "yaw_deg": statistics.mean(yaws), "pitch_deg": statistics.mean(pitches),
            "spread_deg": max(max(yaws) - min(yaws), max(pitches) - min(pitches))}


def trial_schedule() -> list[float]:
    """The requested hold of every trial planned for one control, in execution order."""
    return [hold for hold in HOLD_S for _ in range(FIELD_REPEATS if hold == HOLD_S[-1] else REPEATS)]


def measurement_budget(control_count: int) -> dict[str, Any]:
    """The wall-clock budget for calibrating `control_count` controls, with the numbers behind it.

    It grows with the planned trials and the seconds the planned holds take, so a calibration of
    many controls is not held to a limit chosen for a few. A control that stops early only leaves
    budget unused.
    """
    holds = trial_schedule()
    trials = control_count * len(holds)
    held_s = control_count * sum(holds)
    return {"planned_controls": control_count, "planned_trials": trials, "planned_hold_s": held_s,
            "trial_allowance_s": TRIAL_ALLOWANCE_S, "fixed_allowance_s": FIXED_ALLOWANCE_S,
            "wall_budget_s": FIXED_ALLOWANCE_S + trials * TRIAL_ALLOWANCE_S + held_s,
            "formula": "fixed_allowance_s + planned_trials * trial_allowance_s + planned_hold_s",
            "elapsed_s": None, "trials_recorded": None}


def write_plan(path: Path, plan: dict[str, Any]) -> None:
    """Publish the declared budget before the first trial so the Unity host can size its own timeout.

    The file is created whole or not at all, and never over an existing one.
    """
    budget = plan["budget"]
    document = {"schema_version": 1, "planned_controls": budget["planned_controls"],
                "planned_trials": budget["planned_trials"], "wall_budget_s": budget["wall_budget_s"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.unlink(missing_ok=True)
    with pending.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, allow_nan=False)
        stream.write("\n")
    if path.exists():
        pending.unlink()
        raise FileExistsError(f"the plan file already exists: {path}")
    os.replace(pending, path)


def _keyboard_control(key: str) -> tuple[str, str]:
    """Return the stable physical id and resolution for one keyboard key token."""
    if len(key) == 1 and key.isdigit():
        return "digit" + key, "unity_digit_key"
    if key in MODIFIER_KEYS:
        return MODIFIER_KEYS[key], "unity_modifier_left_key"
    return key, "unity_key_name"


def _control_descriptor(device: str, name: str) -> dict[str, Any] | None:
    """Resolve one supported root control to its stable id, type and declared values."""
    if device == "Keyboard":
        control, resolution = _keyboard_control(name.lower())
        return {"control": control, "control_name": control, "device": "Keyboard",
                "control_type": "Button", "x": None, "y": None, "resolution": resolution}
    if device == "Mouse":
        if name not in MOUSE_CONTROLS:
            return None
        if name == "delta":
            return {"control": "mouse_delta", "control_name": "delta", "device": "Mouse",
                    "control_type": "Delta", "x": DECLARED_VECTOR_X, "y": DECLARED_VECTOR_Y,
                    "resolution": "mouse_relative_delta"}
        return {"control": "mouse_" + name.lower(), "control_name": name, "device": "Mouse",
                "control_type": "Button", "x": None, "y": None, "resolution": "mouse_button"}
    if name in GAMEPAD_VECTORS:
        return {"control": "gamepad_" + name.lower(), "control_name": name, "device": "Gamepad",
                "control_type": "Dpad" if name == "dpad" else "Stick",
                "x": DECLARED_VECTOR_X, "y": DECLARED_VECTOR_Y, "resolution": "gamepad_root_control"}
    if name in GAMEPAD_ONE_DIMENSIONAL:
        return {"control": "gamepad_" + name.lower(), "control_name": name, "device": "Gamepad",
                "control_type": "Axis", "x": DECLARED_VECTOR_X, "y": 0.0,
                "resolution": "gamepad_one_dimensional_axis"}
    if name in GAMEPAD_BUTTONS:
        return {"control": "gamepad_" + name.lower(), "control_name": name, "device": "Gamepad",
                "control_type": "Button", "x": None, "y": None, "resolution": "gamepad_button"}
    return None


def _preflight_action_name(device: str, control: str) -> str:
    if device == "Keyboard":
        return "preflight_key_" + control
    return "preflight_" + control


def discover_actions(discovery: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group discovered paths by the physical device control the bridge can request.

    Both discovery sections are read as one binding set: a binding reached through a scene
    field and the same binding read from the project's asset are the same binding, pressed
    once, and its preferred reach is the one the calibration record names. Keyboard ids stay
    the original physical key tokens. Mouse and gamepad controls get stable canonical ids
    (mouse_delta, gamepad_leftstick, ...). A path outside the supported root controls is
    preserved as an unsupported binding with a reason; it is never remapped to a different
    device, an absolute pointer, a touch control or VR input. A binding whose effective path
    was not measured is reported as unknown for that reason; it is not pressed and its path
    is not inferred.
    """
    actions: dict[str, dict[str, Any]] = {}
    unsupported = []
    for binding in binding_set(discovery):
        if binding.get("binding_is_composite"):
            continue
        path = measured_text(binding, "binding_effective_path")
        # The descriptor keeps the shape a recorded calibration is compared by, so it carries the
        # binding's own ids and paths only; the binding set and the unsupported rows name the reach.
        descriptor = {"binding_id": binding.get("id"), "action_id": binding.get("action_id"),
                      "path": binding.get("binding_effective_path"), "part": binding.get("binding_name"),
                      "source_object_id": binding.get("source_object_id")}
        match = None
        device = None
        if path is not None:
            for candidate, pattern in (("Keyboard", KEY_PATH), ("Mouse", MOUSE_PATH),
                                       ("Gamepad", GAMEPAD_PATH)):
                found = pattern.fullmatch(path)
                if found:
                    device, match = candidate, found.group(1)
                    break
        resolved = _control_descriptor(device, match) if device is not None else None
        if resolved is None:
            if path is None:
                reason = "the binding's effective path was not measured, so it is not pressed"
                if binding["unknown"]:
                    reason += " (unmeasured: " + ", ".join(binding["unknown"]) + ")"
            elif "<Touchscreen>" in path or "<Pointer>" in path:
                reason = "binding is an absolute pointer or touch control, which is unsupported"
            elif device == "Mouse" and match in ("position", "scroll"):
                reason = ("relative scroll is not tested and remains unsupported" if match == "scroll"
                          else "absolute pointer position is unsupported")
            elif path.lower().startswith("<keyboard>/"):
                reason = "keyboard path cannot be resolved to a supported key"
            else:
                reason = "binding is not drivable by a supported input backend"
            unsupported.append({**descriptor, "provenance": binding["provenance"],
                                "unmeasured": list(binding["unknown"]),
                                "effect": None, "variance": None, "inversion": None,
                                "dead_zone": None, "minimum_hold": None, "reason": reason})
            continue
        item = actions.setdefault(resolved["control"], {
            **resolved, "action": _preflight_action_name(resolved["device"], resolved["control"]),
            "bindings": []})
        item["bindings"].append(descriptor)
    return [actions[key] for key in sorted(actions)], unsupported


def summarize_action(action: dict[str, Any], trials: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize measured displacements; untested or insufficient values remain null."""
    groups = []
    minimum = None
    observed_minimum = None
    for hold in HOLD_S:
        selected = [t for t in trials if t.get("requested_hold_s") == hold and t.get("valid") is True]
        magnitudes = [t["effect_magnitude"] for t in selected]
        vectors = [t["effect_delta"] for t in selected]
        mean_vector = [statistics.mean(v[i] for v in vectors) for i in range(3)] if vectors else None
        mean = statistics.mean(magnitudes) if magnitudes else None
        variance = statistics.variance(magnitudes) if len(magnitudes) >= 2 else None
        effective = len(magnitudes) >= REPEATS and all(v >= EFFECT_TOLERANCE for v in magnitudes)
        mean_view_turn = summarize_view_turn([t["view_delta"] for t in selected
                                              if isinstance(t.get("view_delta"), dict)])
        if minimum is None and effective:
            minimum = hold
            durations = [t.get("execution", [{}])[-1].get("applied_duration_s") for t in selected
                         if isinstance(t.get("execution"), list) and t["execution"]]
            if len(durations) == len(selected) and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                       and math.isfinite(v) and v >= 0 for v in durations):
                observed_minimum = min(durations)
        groups.append({"requested_hold_s": hold, "valid_trials": len(magnitudes),
                       "mean_delta": mean_vector, "mean_magnitude": mean,
                       "sample_variance": variance, "effective_in_all_repeats": effective if len(magnitudes) >= REPEATS else None,
                       "mean_view_turn": mean_view_turn})

    parts = {b.get("part", "").lower() for b in action["bindings"]
             if isinstance(b.get("part"), str) and b["part"].lower() in {"up", "down", "left", "right"}}
    inversion = None
    projections = []
    if len(parts) == 1:
        part = next(iter(parts))
        for trial in trials:
            if trial.get("valid") is not True or trial.get("effect_magnitude", 0) < EFFECT_TOLERANCE:
                continue
            basis = trial.get("before", {}).get("world_forward" if part in {"up", "down"} else "world_right")
            vec = _vector(basis)
            if vec is None:
                continue
            sign = 1 if part in {"up", "right"} else -1
            projections.append(sign * sum(a * b for a, b in zip(trial["effect_delta"], vec)))
        if len(projections) >= REPEATS and all(p > EFFECT_TOLERANCE for p in projections):
            inversion = False
        elif len(projections) >= REPEATS and all(p < -EFFECT_TOLERANCE for p in projections):
            inversion = True
    return {"control": action["control"], "bindings": action["bindings"], "resolution": action["resolution"],
            "device": action.get("device"), "control_name": action.get("control_name"),
            "control_type": action.get("control_type"),
            "declared_x": action.get("x"), "declared_y": action.get("y"),
            "holds": groups, "lowest_tested_effective_requested_hold_s": minimum,
            "shortest_observed_effective_hold_s": observed_minimum,
            "minimum_hold_scope": "shortest measured applied duration in the first requested group with responses in both repetitions; not a global minimum",
            "inverted_relative_to_entity_basis": inversion, "signed_basis_projections": projections,
            "dead_zone": None, "dead_zone_reason": "dead zone is unknown: the backend sends declared values only",
            "sensitivity": None,
            "sensitivity_reason": "sensitivity is unknown: one declared value per control was measured, not a response curve"}


def _entity_snapshot(response: Any, entity_id: str) -> dict[str, Any]:
    data = _complete(response, "transforms")
    rows = _objects(data)
    row = rows.get(entity_id)
    if row is None:
        row = unique_by_scene_free_id(rows, entity_id)  # the same object under another loaded-scene index
    if row is None:
        raise ValueError("the selected entity is missing from the transform snapshot")
    forward = _vector(row.get("world_forward"))
    basis = row.get("basis_state")
    yaw = pitch = None
    if basis == "present" and forward is not None and _norm(forward) > 0:
        yaw = math.degrees(math.atan2(forward[0], forward[2]))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, -forward[1]))))
    return {"frame": data["frame"], "real_time_s": data["real_time_s"], "game_time_s": data["game_time_s"],
            "wall_utc": response.wall_utc.isoformat(), "position": row["position"],
            "world_forward": row.get("world_forward"), "world_right": row.get("world_right"),
            "basis_state": row.get("basis_state"),
            "yaw_deg": yaw, "pitch_deg": pitch,
            "basis_convention": ("Unity yaw = atan2(forward.x, forward.z) and pitch = asin(-forward.y) in degrees; "
                                 "derived from the measured basis, not a game-specific camera convention"),
            "runtime_errors": data.get("runtime_errors"), "coverage": data["inspection_coverage"]}


def _input_state(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return generic per-device input state, falling back to the keyboard-only snapshot.

    The bridge keeps `keyboard_state` with its `keys` alias for the older harness and may add
    `input_state` with per-device controls. Missing state is unknown, never "released".
    """
    for key in ("input_state", "keyboard_state"):
        state = data.get(key)
        if isinstance(state, dict):
            return state
    return None


def _control_name(value: Any) -> str:
    return value.rsplit("/", 1)[-1].lower() if isinstance(value, str) else ""


def _active_value(value: Any) -> bool | None:
    """Whether one device control is active, or None when the value was not measured.

    A missing, non-finite or wholly null value is unknown. It is never reported as released:
    an all-null vector and a NaN axis are unmeasured, not a measured zero.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return abs(number) > 0 if math.isfinite(number) else None
    if isinstance(value, dict):
        if isinstance(value.get("active"), bool):
            return value["active"]
        if "x" in value or "y" in value:
            parts = [value.get("x"), value.get("y")]
            present = [part for part in parts if part is not None]
            if not present:
                return None
            numbers = []
            for part in present:
                if isinstance(part, bool) or not isinstance(part, (int, float)) or not math.isfinite(float(part)):
                    return None
                numbers.append(float(part))
            if any(abs(number) > 0 for number in numbers):
                return True
            return False if len(present) == len(parts) else None
        if "value" in value:
            return _active_value(value["value"])
        return None
    if isinstance(value, (list, tuple)) and 1 <= len(value) <= 2:
        if all(part is None for part in value):
            return None
        return _active_value({"x": value[0], "y": value[1] if len(value) > 1 else None})
    return None


def _state_controls(state: dict[str, Any], device: str) -> dict[str, Any]:
    """Collect a device's control map from the supported generic state shapes."""
    result: dict[str, Any] = {}
    containers = [state.get("controls")]
    devices = state.get("devices")
    if isinstance(devices, dict):
        containers.append(devices.get(device))
    elif isinstance(devices, list):
        containers.extend(item for item in devices if isinstance(item, dict) and item.get("device") == device)
    for container in containers:
        if isinstance(container, dict):
            if isinstance(container.get("controls"), dict):
                container = container["controls"]
            elif isinstance(container.get("values"), dict):
                container = container["values"]
            for key, value in container.items():
                if isinstance(key, str):
                    result[_control_name(key)] = value
        elif isinstance(container, list):
            for row in container:
                if isinstance(row, dict) and isinstance(row.get("control"), str):
                    if row.get("device") in (None, device):
                        result[_control_name(row["control"])] = row
    return result


def _control_held(state: dict[str, Any], device: str, control: str) -> bool | None:
    for key, value in _state_controls(state, device).items():
        if key == _control_name(control):
            return _active_value(value)
    devices = state.get("devices")
    device_state = devices.get(device) if isinstance(devices, dict) else None
    if isinstance(device_state, dict):
        pressed = device_state.get("pressed")
        if (catalog_contract.control_type(device, control) == "Button"
                and isinstance(pressed, list) and all(isinstance(name, str) for name in pressed)):
            return any(_control_name(name) == _control_name(control) for name in pressed)
    if device == "Keyboard":
        keys = state.get("keys")
        if isinstance(keys, list):
            return any(_control_name(key) == _control_name(control) for key in keys)
    return None


def _field_snapshot(response: Any, scene_id: str) -> dict[str, Any]:
    if response.stage != "fields" or response.errors:
        raise ValueError("field inspection failed or returned the wrong stage")
    for name in ("game_objects", "fields"):
        cap = response.caps.get(name)
        if cap is None or cap.truncated is not False or cap.observed is None:
            raise ValueError(f"field inspection has incomplete {name} coverage")
    data = response.data
    for name in ("real_time_s", "game_time_s"):
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"field inspection has unknown {name}")
    if data.get("time_unit") != "seconds" or not isinstance(data.get("fields"), list):
        raise ValueError("field inspection has unknown units or field list")
    state = _input_state(data)
    if not isinstance(state, dict):
        raise ValueError("field inspection has unknown input-state evidence")
    held = state.get("keys")
    if held is not None and (not isinstance(held, list) or any(not isinstance(key, str) for key in held)):
        raise ValueError("field inspection has invalid keyboard-state evidence")
    keys = ("id", "value_state", "value_kind", "value", "is_null", "value_truncated", "read_error")
    fields = [{k: field.get(k) for k in keys} for field in data["fields"] if field.get("scene_id") == scene_id]
    return {"frame": data.get("frame"), "real_time_s": data["real_time_s"], "game_time_s": data["game_time_s"],
            "wall_utc": response.wall_utc.isoformat(), "sample_wall_s": data.get("sample_wall_s"),
            "input_state": state, "keyboard_state": data.get("keyboard_state"), "fields": fields,
            "coverage": {name: response.caps[name].model_dump(mode="json") for name in ("game_objects", "fields")}}


def _trial_measure(before: dict[str, Any], after: dict[str, Any], idle_start: dict[str, Any],
                   records: list[Any], hold: float) -> dict[str, Any]:
    p0, p1, pi = _vector(before.get("position")), _vector(after.get("position")), _vector(idle_start.get("position"))
    if p0 is None or p1 is None or pi is None:
        raise ValueError("an entity position is unknown")
    idle_dt = before["real_time_s"] - idle_start["real_time_s"]
    action_dt = after["real_time_s"] - before["real_time_s"]
    if idle_dt <= 0 or action_dt <= 0:
        raise ValueError("measurement timestamps did not advance")
    idle_delta, raw_delta = _sub(p0, pi), _sub(p1, p0)
    baseline = tuple(v * action_dt / idle_dt for v in idle_delta)
    effect = _sub(raw_delta, baseline)
    terminal = records[-1]
    valid = terminal.status.value == "completed" and terminal.release_verified is True and _norm(idle_delta) <= IDLE_TOLERANCE
    return {"requested_hold_s": hold, "valid": valid, "before": before, "after": after,
            "idle_start": idle_start, "idle_delta": idle_delta, "raw_delta": raw_delta,
            "effect_delta": effect, "effect_magnitude": _norm(effect),
            "execution": [record.model_dump(mode="json") for record in records]}


async def _read_entity(client: BridgeClient, entity_id: str) -> dict[str, Any]:
    return _entity_snapshot(await client.inspect("transforms"), entity_id)


async def _read_fields(client: BridgeClient, scene_id: str) -> dict[str, Any]:
    return _field_snapshot(await client.inspect("fields"), scene_id)


async def _idle_window(client: BridgeClient, scene_id: str) -> list[dict[str, Any]]:
    """Settle briefly, then sample the fields with no input running."""
    await asyncio.sleep(0.75)
    window = []
    for i in range(IDLE_SAMPLES):
        if i: await asyncio.sleep(FIELD_INTERVAL_S)
        window.append(await _read_fields(client, scene_id))
    return window


async def _quiet_window(client: BridgeClient, entity_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return the last two reads once the entity has held still for QUIET_INTERVALS intervals in a row.

    One still interval is not enough: motion that returns to its start, like a jump apex, nets to
    zero between two reads.
    """
    deadline = asyncio.get_running_loop().time() + 3.0
    previous = await _read_entity(client, entity_id)
    still = 0
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.12)
        current = await _read_entity(client, entity_id)
        a, b = _vector(previous["position"]), _vector(current["position"])
        if a is None or b is None:
            raise ValueError("an entity position is unknown")
        still = still + 1 if _norm(_sub(b, a)) <= IDLE_TOLERANCE else 0
        if still >= QUIET_INTERVALS:
            return previous, current
        previous = current
    return None


# Every control is assumed to be safe to press: the game keeps running and the displacement measured
# across the trial is the input's effect. A control can break that assumption (a pause or menu action, a
# scene change), and the trial's own readings say so: game time stops advancing while real time does not.
# The trial window is short, so the game must advance game time through it at close to the wall rate.
# Across the saved calibration records the windows that ran advanced at 0.43-1.06 game seconds per wall
# second, and the windows in which the game stopped its own clock advanced at 0.23 or less, most of them
# at exactly 0. A window below a quarter of the wall rate did not run through the window: the clock
# stopped in it, and the displacement measured across it is not the input's effect.
CLOCK_STOP_RATE = 0.25
# Under one frame of game time a window is indistinguishable from a stopped clock, and a window this
# short of real time cannot tell a stop from a sample that was simply brief.
CLOCK_STOP_S = 0.05
CLOCK_MIN_WINDOW_S = 0.1
# What one generic restore may take: the Editor's play-mode restart, its readiness check included.
RESTORE_OPERATION_S = 120.0
# What the trial window's clock and scene evidence means, kept in the record so a reader of it does not
# have to find this code.
CLOCK_WINDOW_BASIS = (
    "the trial's own two reads: game time and real time in seconds, and the scene, pause state and clock "
    "mode each observation reported. A trial whose game time advanced at less than a quarter of its real "
    f"time, or at most {CLOCK_STOP_S} s, over at least {CLOCK_MIN_WINDOW_S} s of real time did not run "
    "through the window; one whose scene changed did not measure the same game on both sides")
STATE_CHANGE_NOT_PRESSED_AGAIN = (
    "not pressed again: this control changed the game's state, and each further trial would measure a "
    "stopped or replaced game rather than the input's effect")
# A session does not only change its state under a press. The measured body drifts while the controls are
# pressed, and a long enough session walks it into a hazard, a trigger, a conversation or the end of the level:
# the game reacts to where the body has got to, not to any one control. The next trial then opens on a game
# that stopped its own clock or replaced its scene, and the control about to be pressed is the one the old
# attribution would have blamed. The state changed between two trials, with no input, so the change belongs to
# no control: it is a session event, its cause is unknown, and the pass ends instead of pressing a control on a
# game that is no longer the one it started in.
BETWEEN_TRIAL_CHANGES = frozenset({"stopped", "scene_changed", "unknown"})
SESSION_EVENT_KIND = "session_event"
SESSION_EVENT_CAUSE = "unknown"
SESSION_EVENT_BASIS = (
    "the fact is a change in the game between two trials of one session, measured from the previous trial's "
    "closing read to the read that opens the next trial, after that trial's input was released and before "
    "anything else was pressed. No control was active, so no control is blamed: the cause is unknown. It is "
    "evidence about the session, not a measurement of any control's effect")
# The keys a prior file holds: the state changes earlier passes measured, and the sessions whose controls they
# finished measuring, each with the idle baseline that session's trials were measured against.
PRIOR_STATE_CHANGES = "state_changes"
PRIOR_SESSIONS = "sessions"
PRIOR_SCHEMA_VERSION = 1
PRIOR_KEYS = frozenset({"schema_version", PRIOR_STATE_CHANGES, PRIOR_SESSIONS})
PLANNED_PASS = "pass"


def _window_reading(snapshot: Any, observation: Any = None) -> dict[str, Any]:
    """One end of a trial window: the clock it read, and the scene and pause state an observation reported.

    A value the sample did not carry stays null. It is never read as zero, false or success.
    """
    def number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return None
        return float(value)

    sample = snapshot if isinstance(snapshot, dict) else {}
    paused = getattr(observation, "paused", None)
    clock_mode = getattr(observation, "clock_mode", None)
    clock_mode = getattr(clock_mode, "value", clock_mode)
    scene = getattr(observation, "scene", None)
    return {
        "frame": _whole(sample.get("frame")),
        "game_time_s": number(sample.get("game_time_s")),
        "real_time_s": number(sample.get("real_time_s")),
        "scene": scene if isinstance(scene, str) and scene else None,
        "paused": paused if isinstance(paused, bool) else None,
        "time_scale": number(getattr(observation, "time_scale", None)),
        "clock_mode": clock_mode if isinstance(clock_mode, str) and clock_mode else None,
    }


def clock_window(before: dict[str, Any], after: dict[str, Any], *,
                 bridge_paused: bool | None = None) -> dict[str, Any]:
    """What a trial's own clock and scene evidence says about the game it ran on.

    `before` and `after` are `_window_reading`s of the reads that bound the trial. The verdict is one of
    `running` (the clock advanced and the scene held), `stopped` (game time did not advance while real
    time did, and the bridge did not pause it), `scene_changed`, `bridge_paused` (the bridge paused the
    clock itself, so nothing about the game changed) or `unknown` (the evidence cannot say).

    `bridge_paused` is what the caller knows about its own clock requests. The bridge's own report in
    `clock_mode` is measured evidence and takes precedence over it: `stepped` is the mode the bridge
    sets when it pauses or steps the clock, and `realtime` when it has not. A stopped window the caller
    cannot attribute, with no `clock_mode` to settle it, is unknown rather than valid.

    Only `running` and `bridge_paused` keep the trial's measurement. Every other verdict says the
    displacement measured across the window is not the input's effect, and states the evidence why.
    """
    first_game, second_game = before.get("game_time_s"), after.get("game_time_s")
    first_real, second_real = before.get("real_time_s"), after.get("real_time_s")
    game_delta = second_game - first_game if first_game is not None and second_game is not None else None
    real_delta = second_real - first_real if first_real is not None and second_real is not None else None
    first_scene, second_scene = before.get("scene"), after.get("scene")
    scene_changed = first_scene != second_scene if first_scene and second_scene else None
    modes = [mode for mode in (before.get("clock_mode"), after.get("clock_mode")) if isinstance(mode, str)]
    observed_pause = True if "stepped" in modes else (False if modes else None)
    paused = observed_pause if observed_pause is not None else bridge_paused
    rate = game_delta / real_delta if game_delta is not None and real_delta is not None and real_delta > 0 else None
    evidence: dict[str, Any] = {
        "game_time_s": {"before": first_game, "after": second_game, "delta_s": game_delta},
        "real_time_s": {"before": first_real, "after": second_real, "delta_s": real_delta},
        "game_rate": rate,
        "scene": {"before": first_scene, "after": second_scene, "changed": scene_changed},
        "paused": {"before": before.get("paused"), "after": after.get("paused")},
        "time_scale": {"before": before.get("time_scale"), "after": after.get("time_scale")},
        "clock_mode": {"before": before.get("clock_mode"), "after": after.get("clock_mode")},
        "bridge_paused": paused, "verdict": "unknown", "effect": None, "valid": False,
        "state_change": False, "reason": None, "basis": CLOCK_WINDOW_BASIS}
    stopped: bool | None = None
    reason: str | None = None

    def seconds(value: float | None) -> str:
        return "unmeasured" if value is None else f"{value:.4f} s"

    if game_delta is None or real_delta is None:
        reason = "the trial's game time and real time were not both read at both ends of the window"
    elif game_delta < 0 or real_delta < 0:
        reason = (f"the trial's clock went backwards: game time advanced {game_delta:.4f} s and real time "
                  f"{real_delta:.4f} s")
    elif real_delta < CLOCK_MIN_WINDOW_S:
        reason = (f"the trial's window was only {real_delta:.4f} s of real time, too short to tell a stopped "
                  "game clock from a brief sample")
    else:
        stopped = game_delta <= max(CLOCK_STOP_S, CLOCK_STOP_RATE * real_delta)
    if scene_changed is True:
        evidence.update(verdict="scene_changed", effect="scene changed", valid=False, state_change=True,
                        reason=(f"the scene changed from {first_scene!r} to {second_scene!r} across the trial "
                                f"(game time advanced {seconds(game_delta)}, real time {seconds(real_delta)})"))
    elif stopped is True and paused is True:
        evidence.update(verdict="bridge_paused", effect=None, valid=True, state_change=False,
                        reason=("the bridge paused the clock itself, which is not a game state change: game "
                                f"time advanced {game_delta:.4f} s while real time advanced {real_delta:.4f} s"))
    elif stopped is True and paused is False:
        evidence.update(verdict="stopped", effect="game clock stopped", valid=False, state_change=True,
                        reason=(f"the game clock stopped: game time advanced {game_delta:.4f} s while real time "
                                f"advanced {real_delta:.4f} s across the trial"))
    elif stopped is True:
        reason = (f"game time advanced {game_delta:.4f} s while real time advanced {real_delta:.4f} s, but "
                  "neither the bridge nor the trial says who paused the clock")
    elif stopped is False:
        evidence.update(verdict="running", effect=None, valid=True, state_change=False, reason=None)
    if reason is not None:
        evidence["reason"] = reason
    return evidence


def state_change_record(action: dict[str, Any], action_id: str, hold: float, window: dict[str, Any],
                        trial: int) -> dict[str, Any]:
    """The fact that one control changed the game's state, with the readings that measured it.

    The control is kept: it is what a later stage has to expose, not something to drop. The record
    carries the effect it produced, the game and real time and the scene before and after it, and the
    generic restore that put a running game back before the next trial.
    """
    return {
        "control": action["control"], "action_id": action_id, "requested_hold_s": hold,
        "device": action.get("device"), "control_name": action.get("control_name"),
        "effect": window["effect"], "trial": trial,
        "before": {"game_time_s": window["game_time_s"]["before"], "real_time_s": window["real_time_s"]["before"],
                   "scene": window["scene"]["before"]},
        "after": {"game_time_s": window["game_time_s"]["after"], "real_time_s": window["real_time_s"]["after"],
                  "scene": window["scene"]["after"]},
        "further_trials": STATE_CHANGE_NOT_PRESSED_AGAIN, "restore": None, "evidence": window,
    }


def _seconds(value: Any) -> str:
    """A measured interval, or the fact that it was not measured; never zero for unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return "unmeasured"
    return f"{float(value):.4f} s"


def between_trial_window(previous: dict[str, Any], previous_observation: Any,
                         current: dict[str, Any], observation: Any) -> dict[str, Any] | None:
    """Whether the game still ran between the previous trial's close and this trial's start.

    `previous` is the trial this one follows, and its `after` read is the last good evidence of the game
    before this trial's baseline; `current` is the read that opens this trial. The two observations carry
    the scene, pause state and clock mode each read reported. The window is decided by the same thresholds
    as a trial's own window (`clock_window`), with the bridge's clock known to be realtime here: this loop
    sends no clock request of its own.

    A previous trial whose closing read is not a measured snapshot gives no window. Nothing was compared,
    which is not the same as a measured running game, and it is not evidence that the game changed.
    """
    closing = previous.get("after") if isinstance(previous, dict) else None
    if not isinstance(closing, dict) or not isinstance(current, dict):
        return None
    return clock_window(_window_reading(closing, previous_observation),
                        _window_reading(current, observation), bridge_paused=False)


def session_event(window: dict[str, Any], last_trial: dict[str, Any], trial_index: int,
                  after_trial: int) -> dict[str, Any]:
    """The record of a game state change between trials that no control produced, with its readings.

    It is filed among the trials because that is where the evidence it interrupts lives, but it is not a
    trial: `unattributed` says so, `control` is null, `valid` is null because nothing was pressed, and the
    cause is `unknown`. What it carries is measured: the game and real time and the scene on both sides of
    the change, the body's position at the last good read, and the control that was released before it and
    when that release completed. It says which entry it sits after, so a reader can find the two trials it
    separates.
    """
    execution = last_trial.get("execution") if isinstance(last_trial, dict) else None
    released = (execution[-1] if isinstance(execution, list) and execution
                and isinstance(execution[-1], dict) else None)
    after = last_trial.get("after") if isinstance(last_trial, dict) else None
    return {
        "control": None, "unattributed": True, "kind": SESSION_EVENT_KIND,
        "cause": SESSION_EVENT_CAUSE, "effect": window["effect"], "valid": None, "error": None,
        "trial": trial_index, "after_trial": after_trial,
        "last_control": last_trial.get("control") if isinstance(last_trial, dict) else None,
        "last_action_id": last_trial.get("action_id") if isinstance(last_trial, dict) else None,
        "last_release_wall_s": released.get("input_end_wall_s") if isinstance(released, dict) else None,
        "position": after.get("position") if isinstance(after, dict) else None,
        "clock": window, "reason": session_event_reason(window), "basis": SESSION_EVENT_BASIS,
    }


def session_event_reason(window: dict[str, Any]) -> str:
    """What the window measured, in a sentence, with the values it measured it from."""
    game, real, scene = window["game_time_s"], window["real_time_s"], window["scene"]
    if window["verdict"] == "scene_changed":
        return (f"the scene changed from {scene['before']!r} to {scene['after']!r} between two trials, with no "
                f"input pressed (game time advanced {_seconds(game['delta_s'])}, real time "
                f"{_seconds(real['delta_s'])})")
    if window["verdict"] == "stopped":
        return (f"the game clock stopped between two trials, with no input pressed: game time advanced "
                f"{_seconds(game['delta_s'])} while real time advanced {_seconds(real['delta_s'])}")
    return ("the game's clock and scene could not be read between two trials, so whether it still runs is "
            "unknown: " + (window.get("reason") or "no reason was measured"))


class SessionStopped(Exception):
    """The game changed its own state between two trials; the pass ends and no control is blamed."""

    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__(event["reason"])
        self.event = event


def _prior_trial(entry: Any, index: int) -> dict[str, Any]:
    """One carried trial, checked for the evidence that a state change has to bring with it."""
    if not isinstance(entry, dict):
        raise ValueError(f"prior state change {index} is not a trial record")
    control = entry.get("control")
    change = entry.get("state_change")
    if not isinstance(control, str) or not control:
        raise ValueError(f"prior state change {index} names no control")
    if not isinstance(change, dict):
        raise ValueError(f"prior state change {index} for {control!r} carries no state_change entry")
    if change.get("control") != control:
        raise ValueError(f"prior state change {index} is listed as {control!r} but its state_change entry "
                         f"names {change.get('control')!r}")
    _prior_pass(entry, f"prior state change {index}")
    return entry


def _prior_pass(entry: dict[str, Any], what: str) -> int | None:
    """The pass number a carried record names, or None when the file predates the numbering."""
    value = entry.get(PLANNED_PASS)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what} names the pass {value!r}, which is not a positive pass number")
    return value


def _checked_state_changes(entries: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise ValueError(f"{what} must be a list of trial records")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        _prior_trial(entry, index)
        if entry["control"] in seen:
            raise ValueError(f"{what} names the control {entry['control']!r} twice")
        seen.add(entry["control"])
    return entries


_SESSION_KEYS = frozenset({PLANNED_PASS, "idle_fields", "later_idle_windows", "controls", "session_events"})
_CONTROL_KEYS = frozenset({"control", "trials", "input_field_segments"})


def _prior_control(entry: Any, number: int, index: int) -> dict[str, Any]:
    """One completed control of a carried session, checked for the evidence it has to bring with it."""
    if not isinstance(entry, dict) or set(entry) != _CONTROL_KEYS:
        raise ValueError(f"prior session {number} control {index} is a mapping holding exactly "
                         f"{sorted(_CONTROL_KEYS)}")
    control = entry["control"]
    if not isinstance(control, str) or not control:
        raise ValueError(f"prior session {number} control {index} names no control")
    trials = entry["trials"]
    if not isinstance(trials, list) or not trials:
        raise ValueError(f"prior session {number} control {control!r} carries no measured trial")
    for trial_index, trial in enumerate(trials):
        if not isinstance(trial, dict) or trial.get("control") != control:
            raise ValueError(f"prior session {number} control {control!r} trial {trial_index} is not a trial "
                             "of that control")
        if isinstance(trial.get("state_change"), dict):
            raise ValueError(f"prior session {number} control {control!r} carries a state change; a control that "
                             "changed the game's state is filed as one, not as a measurement")
        if trial.get("valid") is None:
            raise ValueError(f"prior session {number} control {control!r} trial {trial_index} never finished, so "
                             "the control is not completed")
    segments = entry["input_field_segments"]
    if not isinstance(segments, list):
        raise ValueError(f"prior session {number} control {control!r} field segments are not a list")
    for segment_index, segment in enumerate(segments):
        if not isinstance(segment, dict) or segment.get("control") != control:
            raise ValueError(f"prior session {number} control {control!r} field segment {segment_index} is not "
                             "that control's")
        samples = segment.get("samples")
        if not isinstance(samples, list) or any(not isinstance(sample, dict) for sample in samples):
            raise ValueError(f"prior session {number} control {control!r} field segment {segment_index} has no "
                             "sample list")
    return entry


def _prior_session_event(entry: Any, number: int, index: int) -> dict[str, Any]:
    """One carried session event, checked to be unattributed: a carried event never blames a control."""
    if not isinstance(entry, dict) or entry.get("unattributed") is not True:
        raise ValueError(f"prior session {number} event {index} is not an unattributed event")
    if entry.get("control") is not None:
        raise ValueError(f"prior session {number} event {index} blames the control {entry.get('control')!r}")
    if entry.get("cause") != SESSION_EVENT_CAUSE:
        raise ValueError(f"prior session {number} event {index} does not state the cause {SESSION_EVENT_CAUSE!r}")
    return entry


def _prior_session(entry: Any, index: int) -> dict[str, Any]:
    """One passed session, checked for the baseline and the controls it has to bring with it."""
    if not isinstance(entry, dict) or set(entry) != _SESSION_KEYS:
        raise ValueError(f"prior session {index} is a mapping holding exactly {sorted(_SESSION_KEYS)}")
    number = entry[PLANNED_PASS]
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"prior session {index} names the pass {number!r}, which is not a positive pass number")
    idle = entry["idle_fields"]
    if not isinstance(idle, list) or not idle or any(not isinstance(sample, dict) for sample in idle):
        raise ValueError(f"prior session {number} carries no idle baseline to measure its trials against")
    windows = entry["later_idle_windows"]
    if not isinstance(windows, list) or any(not isinstance(window, list) or
                                           any(not isinstance(sample, dict) for sample in window)
                                           for window in windows):
        raise ValueError(f"prior session {number} carries malformed later idle windows")
    controls, events = entry["controls"], entry["session_events"]
    if not isinstance(controls, list) or not isinstance(events, list):
        raise ValueError(f"prior session {number} controls and session events are lists")
    if not controls and not events:
        raise ValueError(f"prior session {number} measured nothing to carry")
    for control_index, control in enumerate(controls):
        _prior_control(control, number, control_index)
    for event_index, event in enumerate(events):
        _prior_session_event(event, number, event_index)
    return entry


def _prior_document(prior: Any) -> dict[str, Any]:
    """A prior file checked whole, as `{schema_version, state_changes, sessions}`.

    Everything a earlier pass settled appears once: a control is either a state change or a completed
    measurement, never both and never twice, because a later pass would otherwise carry one fact as two.
    """
    if not isinstance(prior, dict) or PRIOR_STATE_CHANGES not in prior:
        raise ValueError(f"prior measurements are a mapping holding {PRIOR_STATE_CHANGES!r}, and optionally "
                         f"{PRIOR_SESSIONS!r}")
    unknown = set(prior) - PRIOR_KEYS
    if unknown:
        raise ValueError(f"prior measurements hold keys this format does not define: {sorted(unknown)}")
    version = prior.get("schema_version", PRIOR_SCHEMA_VERSION)
    if version != PRIOR_SCHEMA_VERSION:
        raise ValueError(f"prior measurements are schema version {version!r}, not {PRIOR_SCHEMA_VERSION}")
    changes = _checked_state_changes(prior[PRIOR_STATE_CHANGES], "prior state_changes")
    sessions = prior.get(PRIOR_SESSIONS, [])
    if not isinstance(sessions, list):
        raise ValueError(f"prior {PRIOR_SESSIONS} must be a list of the passes whose controls were completed")
    checked = [_prior_session(entry, index) for index, entry in enumerate(sessions)]
    numbers = [session[PLANNED_PASS] for session in checked]
    if len(set(numbers)) != len(numbers):
        raise ValueError(f"prior {PRIOR_SESSIONS} numbers the same pass twice")
    known = {entry["control"] for entry in changes}
    for session in checked:
        for control in session["controls"]:
            name = control["control"]
            if name in known:
                raise ValueError(f"prior measurements name the control {name!r} twice")
            known.add(name)
    return {"schema_version": PRIOR_SCHEMA_VERSION, PRIOR_STATE_CHANGES: changes, PRIOR_SESSIONS: checked}


def _prior_entries(prior: Any) -> list[dict[str, Any]]:
    return _prior_document(prior)[PRIOR_STATE_CHANGES]


def _prior_sessions(prior: Any) -> list[dict[str, Any]]:
    return _prior_document(prior)[PRIOR_SESSIONS]


def load_prior_state_changes(path: Path) -> dict[str, Any]:
    """Read what earlier passes measured, handed to a fresh calibration run as `--prior-state-changes`.

    The file is `{"schema_version": 1, "state_changes": [...], "sessions": [...]}`. `state_changes` holds one
    trial per control an earlier pass showed to change the game's state, exactly as that pass wrote it, its
    `state_change` entry included. `sessions` holds one entry per pass whose controls were finished, each with
    that pass's own idle baseline, the trials and field segments its measurement produced, and the session
    events it saw. Anything else is an error: a prior file that silently dropped, duplicated or reshaped one of
    these facts would let the run press a control whose effect is already known, or carry a measurement into a
    record whose baseline is another session's. It is read and checked before the session is touched.
    """
    return copy.deepcopy(_prior_document(json.loads(Path(path).read_text(encoding="utf-8"))))


def _carried(entry: dict[str, Any], number: int | None) -> dict[str, Any]:
    """A carried entry as this record's own, with the pass that measured it and no bookkeeping key left over."""
    carried = {key: value for key, value in copy.deepcopy(entry).items() if key != PLANNED_PASS}
    carried["prior_pass"] = number if number is not None else True
    return carried


def carried_trials(prior: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every trial an earlier pass measured, as this record's own, marked with the pass that measured it.

    That is the state changes an earlier pass found and the trials of the controls it finished measuring. A
    carried entry keeps the evidence its own session produced and nothing is pressed again. `prior_pass` is
    the originating pass number, or `True` for a prior file written before the passes were numbered.
    """
    if prior is None:
        return []
    entries = [_carried(entry, _prior_pass(entry, "prior state change")) for entry in _prior_entries(prior)]
    for session in _prior_sessions(prior):
        number = session[PLANNED_PASS]
        for control in session["controls"]:
            entries.extend(_carried(trial, number) for trial in control["trials"])
        entries.extend(_carried(event, number) for event in session["session_events"])
    return entries


def carried_field_segments(prior: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The held-input field windows of the controls an earlier pass finished, with the baseline they belong to.

    `prior_pass` and `baseline_pass` are the session that produced the window and the idle baseline it was
    sampled against. A carried window is analysed only inside itself, against windows of the same session.
    """
    if prior is None:
        return []
    segments = []
    for session in _prior_sessions(prior):
        number = session[PLANNED_PASS]
        for control in session["controls"]:
            for segment in control["input_field_segments"]:
                segments.append(dict(copy.deepcopy(segment), prior_pass=number, baseline_pass=number))
    return segments


def carried_idle_windows(prior: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Each carried session's own idle windows, in pass order, with the controls they belong to.

    The first window of each is that pass's `idle_fields`: the baseline its trials and held windows were
    measured against. No window is ever merged with another; they are separate windows in one record.
    """
    if prior is None:
        return []
    carried = []
    for session in _prior_sessions(prior):
        windows = [copy.deepcopy(session["idle_fields"])]
        windows.extend(copy.deepcopy(window) for window in session["later_idle_windows"])
        carried.append({"pass": session[PLANNED_PASS],
                        "controls": [control["control"] for control in session["controls"]],
                        "windows": windows})
    return carried


def baseline_index(baselines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Where each carried session's idle windows sit in a record's `later_idle_windows`, and whose they are.

    A session's own `idle_fields` comes first in its windows: that is the baseline its trials and held windows
    were measured against. Every later window of the session follows it. The indexes let a reader of the final
    record tell which pass sampled a window and which controls it served, without merging the windows.
    """
    rows, start = [], 0
    for baseline in baselines:
        rows.append({"pass": baseline["pass"], "controls": baseline["controls"], "idle_fields_window": start,
                     "later_idle_windows": list(range(start, start + len(baseline["windows"])))})
        start += len(baseline["windows"])
    return rows


def carried_controls(prior: dict[str, Any] | None) -> dict[str, int | None]:
    """Every control an earlier pass settled, with the pass that measured it (None when it was not numbered).

    Settled means an earlier pass either finished that control's full trial set or showed that it changes the
    game's state. Neither is pressed again: a measurement belongs to the session that produced it.
    """
    if prior is None:
        return {}
    controls: dict[str, int | None] = {
        entry["control"]: _prior_pass(entry, "prior state change") for entry in _prior_entries(prior)}
    for session in _prior_sessions(prior):
        for control in session["controls"]:
            controls[control["control"]] = session[PLANNED_PASS]
    return controls


def planned_holds() -> dict[float, int]:
    """How many trials of each requested hold one control's full measurement takes: every hold and repeat."""
    planned: dict[float, int] = {}
    for hold in trial_schedule():
        planned[hold] = planned.get(hold, 0) + 1
    return planned


def completed_controls(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The controls whose full planned trial set finished in this record's own session.

    A control is completed when every requested hold has as many finished trials as the plan gives it, so a
    control interrupted partway is not completed: a later pass measures it whole, in one session, and never
    mixes two sessions' trials into one measurement. A control that changed the game's state, that the bridge
    rejected, or whose press was sent but never finished is not a completed measurement. Each result carries
    the trials and field segments of its own session, and nothing from another.
    """
    if not isinstance(record, dict) or not isinstance(record.get("trials"), list):
        raise ValueError("a calibration record holds a trials list")
    trials = record["trials"]
    planned = planned_holds()
    segments = record.get("input_field_segments") or []
    counted: dict[str, dict[float, int]] = {}
    indexes: dict[str, list[int]] = {}
    refused: set[str] = set()
    for index, trial in enumerate(trials):
        if not isinstance(trial, dict):
            raise ValueError("a calibration record holds trial records")
        control = trial.get("control")
        if not isinstance(control, str):
            continue  # a session event belongs to no control
        if trial.get("prior_pass") is not None:
            continue  # measured by an earlier pass, and carried from it
        indexes.setdefault(control, []).append(index)
        if isinstance(trial.get("state_change"), dict) or trial.get("rejected") is True:
            refused.add(control)
            continue
        hold = trial.get("requested_hold_s")
        if trial.get("valid") is None or isinstance(hold, bool) or not isinstance(hold, (int, float)):
            continue  # a press that was sent but never finished is not a finished trial
        held = counted.setdefault(control, {})
        held[float(hold)] = held.get(float(hold), 0) + 1
    completed = []
    for control in sorted(counted):
        if control in refused:
            continue
        if any(counted[control].get(hold, 0) != count for hold, count in planned.items()):
            continue
        completed.append({
            "control": control,
            "trials": [copy.deepcopy(trials[index]) for index in indexes[control]],
            "input_field_segments": [copy.deepcopy(segment) for segment in segments
                                     if isinstance(segment, dict) and segment.get("control") == control]})
    return completed


def unattributed_events(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The session events a record holds: state changes between trials that no control produced."""
    if not isinstance(record, dict) or not isinstance(record.get("trials"), list):
        raise ValueError("a calibration record holds a trials list")
    return [trial for trial in record["trials"]
            if isinstance(trial, dict) and trial.get("unattributed") is True]


def recorded_session_events(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The session events this record's own session saw, not the ones an earlier pass carried in.

    An event belongs to the session that measured it. A pass carries an earlier event into its record for
    the final report, but filing it again with this pass would duplicate one fact, so a later merge takes
    only the events whose `prior_pass` is absent.
    """
    return [event for event in unattributed_events(record) if event.get("prior_pass") is None]


def remaining_controls(record: dict[str, Any] | None, prior: dict[str, Any] | None = None) -> list[str]:
    """The offered controls a later pass still has to measure, in the order this record offers them.

    Structural, never read from a message: a control is accounted for when a trial of it carries a state
    change, when the bridge rejected it, when its full trial set finished in this record, or when the prior
    file already holds it as a state change or a completed measurement. Every control not accounted for has to
    be measured in a fresh pass, because its measurement belongs to one session and this one did not finish
    it. A pass that ends with an empty list has every offered control accounted for.
    """
    if not isinstance(record, dict) or not isinstance(record.get("actions"), list):
        raise ValueError("a calibration record holds an actions list")
    accounted = set(carried_controls(prior))
    for trial in record.get("trials") or []:
        if not isinstance(trial, dict) or not isinstance(trial.get("control"), str):
            continue
        if isinstance(trial.get("state_change"), dict) or trial.get("rejected") is True:
            accounted.add(trial["control"])
    accounted.update(control["control"] for control in completed_controls(record))
    return [action["control"] for action in record["actions"]
            if isinstance(action, dict) and action.get("control") not in accounted]


def new_state_changes(record: dict[str, Any], prior: dict[str, Any]) -> list[dict[str, Any]]:
    """The state changes a pass recorded that its prior file did not already know.

    Read structurally, never from an error message: a trial carrying a `state_change` entry whose
    control the prior file does not list is new. A control is one fact, so it is reported once; a
    trial the record carried from an earlier pass is known by its control and is not new.
    """
    known = set(carried_controls(prior))
    trials = record.get("trials") if isinstance(record, dict) else None
    if not isinstance(trials, list):
        raise ValueError("a calibration record holds a trials list")
    found: list[dict[str, Any]] = []
    for trial in trials:
        if not isinstance(trial, dict) or not isinstance(trial.get("state_change"), dict):
            continue
        control = trial.get("control")
        if not isinstance(control, str) or control in known:
            continue
        _prior_trial(trial, len(found))
        known.add(control)
        found.append(trial)
    return found


def merge_prior(prior: dict[str, Any], record: dict[str, Any] | None, pass_number: int) -> dict[str, Any]:
    """The prior file for the next pass: everything earlier passes settled, plus what this pass did.

    `record` is this pass's own record (None when the pass produced none) and `pass_number` is which pass it
    was. The state changes it found are kept whole and numbered. The controls it finished measuring are filed
    as one session with this pass's own idle baseline, so a later pass carries their measurements without
    pressing them again and without measuring them against another session's baseline. The session events this
    pass saw are filed with it — not the ones it carried from an earlier pass, which stay with the session
    that measured them — so the final record can still show where the game changed on its own.
    """
    if isinstance(pass_number, bool) or not isinstance(pass_number, int) or pass_number < 1:
        raise ValueError("a pass number is a positive integer")
    document = _prior_document(prior)
    if any(session[PLANNED_PASS] == pass_number for session in document[PRIOR_SESSIONS]):
        raise ValueError(f"the prior file already holds the pass {pass_number}")
    changes = [copy.deepcopy(entry) for entry in document[PRIOR_STATE_CHANGES]]
    if record is not None:
        for entry in new_state_changes(record, document):
            changes.append(dict(copy.deepcopy(entry), **{PLANNED_PASS: pass_number}))
    controls = completed_controls(record) if record is not None else []
    events = recorded_session_events(record) if record is not None else []
    sessions = [copy.deepcopy(session) for session in document[PRIOR_SESSIONS]]
    if controls or events:
        source = record or {}
        sessions.append({
            PLANNED_PASS: pass_number,
            "idle_fields": copy.deepcopy(source.get("idle_fields") or []),
            "later_idle_windows": copy.deepcopy(source.get("later_idle_windows") or []),
            "controls": controls,
            "session_events": copy.deepcopy(events)})
    return {"schema_version": PRIOR_SCHEMA_VERSION, PRIOR_STATE_CHANGES: changes,
            PRIOR_SESSIONS: sessions}


async def _running_window(client: BridgeClient, entity_id: str) -> dict[str, Any]:
    """Whether the game's clock runs now, from two reads of the entity `RUNNING_OBSERVATION_S` apart."""
    first = await _read_entity(client, entity_id)
    await asyncio.sleep(RUNNING_OBSERVATION_S)
    second = await _read_entity(client, entity_id)
    return clock_window(_window_reading(first), _window_reading(second), bridge_paused=False)


async def restore_running_game(restore: Any, *, client: BridgeClient, entity_id: str, trial: int,
                               evidence: dict[str, Any]) -> tuple[BridgeClient, dict[str, Any]]:
    """Run the calibration's generic restore and require that the game runs again before the next trial.

    `restore` is the generic mechanism, called as `restore(trial=, evidence=, client=)`; it returns a
    record of what it did, with `status` `restored` when the game runs again and, when it produced a new
    connection, that connection under `client`. There is no per-control unpause: a control that paused,
    opened a menu or changed the scene is restored by the same generic reset every other state is, and a
    calibration that has no generic restore stops here instead of measuring a stopped game.
    """
    if restore is None:
        raise ValueError(
            "the game changed its own state during a trial and this calibration has no generic restore, "
            "so the remaining controls cannot be measured on a running game")
    outcome = await restore(trial=trial, evidence=evidence, client=client)
    if not isinstance(outcome, dict):
        raise ValueError("the generic restore returned no record of what it did")
    record = {key: value for key, value in outcome.items() if key != "client"}
    record.setdefault("method", None)
    record.setdefault("status", None)
    adopted = outcome.get("client")
    if record["status"] != "restored":
        detail = record.get("error") or f"status {record['status']!r}"
        record["running_after_restore"] = None
        raise ValueError(f"the generic restore did not restore the game ({detail})")
    if adopted is not None and adopted is not client:
        await client.aclose()
        client = adopted
    running = await _running_window(client, entity_id)
    record["running_after_restore"] = running
    if running["verdict"] != "running":
        raise ValueError("the game clock was still not running after the generic restore: "
                         + (running["reason"] or "the evidence does not say it ran"))
    return client, record


def session_restore(project: Path, adopted: list[BridgeClient] | None = None) -> Any:
    """The calibration's generic restore as a callable, or None when this session has none.

    It is the Editor-owned play-mode restart `playtest-restart-smoke` exercises: the host restarts the
    play session, verifies its own input release and reports a ready state at a new generation, and the
    client that comes with it is the one the calibration continues on. A session with no control
    directory, or a host that does not advertise the restart, has no generic restore: the calibration
    then stops when one is needed rather than pressing keys nobody measured.
    """
    from playtest.runner.session_control import (
        SessionControlError, SessionControl, control_directory, load_host_session,
    )

    directory = control_directory()
    if directory is None:
        return None
    try:
        host = load_host_session(directory, project=project)
    except (SessionControlError, OSError, ValueError):
        return None
    if not host.advertises_restart:
        return None
    control = SessionControl(host, project=project)

    async def restore(*, trial: int, evidence: dict[str, Any], client: BridgeClient | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {"method": "restart_play_mode", "status": "failed", "error": None,
                                  "trial": trial, "effect": evidence.get("effect"), "evidence": None}
        generation = control.generation
        if not isinstance(generation, str) or not generation:
            record["error"] = "the host reports no play generation, so a restart cannot be proven"
            return record
        operation = await control.restart(
            ticket_id=f"calibration-restore-{trial + 1}", from_generation=generation,
            previous_endpoint=getattr(client, "endpoint", None) if client is not None else None,
            previous_session=getattr(client, "session", None) if client is not None else None,
            deadline_s=RESTORE_OPERATION_S, release_verified=True)
        record["evidence"] = {"status": operation.status.value, "ticket_id": operation.ticket_id,
                              "from_generation": operation.from_generation, "to_generation": operation.to_generation,
                              "reason": operation.reason, "waited_s": operation.waited_s}
        if operation.status.value != "ready" or operation.client is None:
            record["error"] = operation.reason or "the restarted session produced no new connection"
            return record
        record["status"] = "restored"
        record["client"] = operation.client
        if adopted is not None:
            adopted.append(operation.client)
        return record

    return restore


def _within_execution(response: Any, record: Any) -> bool:
    start, end = record.input_start_wall_s, record.input_end_wall_s
    if start is None or end is None or end < start:
        return False
    wall = response.data.get("sample_wall_s")
    return isinstance(wall, (int, float)) and not isinstance(wall, bool) and math.isfinite(wall) and start <= wall <= end


def _held_during_sample(response: Any, record: Any, control: str, device: str = "Keyboard") -> bool:
    state = _input_state(response.data)
    if not isinstance(state, dict) or state.get("active") is not True or state.get("error") is not None:
        return False
    if state.get("update_type") not in ("dynamic", "fixed"):
        return False
    if _control_held(state, device, control) is not True:
        return False
    captured_real, sample_real, captured_wall = state.get("real_time_s"), response.data.get("real_time_s"), state.get("wall_s")
    values = (captured_real, sample_real, captured_wall, record.input_start_wall_s, record.input_end_wall_s)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        return False
    return (0 <= sample_real - captured_real <= MAX_KEYBOARD_STATE_AGE_S
            and record.input_start_wall_s <= captured_wall <= record.input_end_wall_s
            and _within_execution(response, record))


def _new_runtime_errors(snapshot: dict[str, Any], previous: int) -> tuple[int, list[dict[str, Any]]]:
    monitor = snapshot.get("runtime_errors")
    if not isinstance(monitor, dict) or monitor.get("active") is not True or monitor.get("truncated") is not False:
        raise ValueError("runtime error monitoring is unavailable or truncated")
    observed, events = monitor.get("observed"), monitor.get("events")
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < previous or not isinstance(events, list):
        raise ValueError("runtime error monitoring has invalid coverage")
    if any(not isinstance(event, dict) or not isinstance(event.get("last_sequence"), int) for event in events):
        raise ValueError("runtime error monitoring has invalid events")
    new = [event for event in events if event["last_sequence"] > previous]
    if observed > previous and not new:
        raise ValueError("runtime errors were observed but are missing from the sample")
    return observed, new


def _whole(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _clip(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def error_window(monitor: Any, previous: int | None) -> dict[str, Any]:
    """The runtime errors one window saw, from the monitor snapshot that closes it.

    The window is every error after the monitor's counter read `previous`. The counter covers every
    error the monitor saw, so the window's `count` is exact even when the monitor kept only some of
    the messages. The retained events are a bounded sample: `complete` is true only when they
    account for every error counted in the window, and false (never silently true) when some were
    evicted. A monitor that is off, or whose counters do not hold together, gives no count at all:
    the window is unknown, not zero.
    """
    window: dict[str, Any] = {"available": False, "reason": None, "start_observed": previous,
                              "end_observed": None, "count": None, "sampled": None, "complete": None,
                              "monitor_limit": None, "kinds_total": None, "kinds": [], "note": None}
    if not isinstance(monitor, dict) or monitor.get("active") is not True:
        window["reason"] = "runtime error monitoring is unavailable"
        return window
    window["monitor_limit"] = _whole(monitor.get("limit"))
    observed, events = _whole(monitor.get("observed")), monitor.get("events")
    if previous is None:
        window["reason"] = "the window has no earlier monitor reading to start from"
        return window
    if observed is None or observed < previous or not isinstance(events, list):
        window["reason"] = "runtime error monitoring has invalid coverage"
        return window
    window["end_observed"] = observed
    covered = []
    for event in events:
        first = _whole(event.get("first_sequence")) if isinstance(event, dict) else None
        last = _whole(event.get("last_sequence")) if isinstance(event, dict) else None
        if first is None or last is None or first < 1 or last < first:
            window["reason"] = "runtime error monitoring has invalid events"
            return window
        if last <= previous:
            continue
        run = last - max(first, previous + 1) + 1
        recorded = _whole(event.get("count"))
        covered.append((event, first, last, run if recorded is None else min(run, recorded)))
    count, sampled = observed - previous, sum(item[3] for item in covered)
    if sampled > count:
        window["reason"] = "runtime error monitoring has inconsistent counts"
        return window
    kinds: dict[tuple[str, str], dict[str, Any]] = {}
    for event, first, last, occurrences in covered:
        key = (str(event.get("type")), str(event.get("message")))
        kind = kinds.setdefault(key, {
            "type": _clip(event.get("type"), 32), "message": _clip(event.get("message"), ERROR_TEXT_CHARS),
            "stack": _clip(event.get("stack"), ERROR_STACK_CHARS), "occurrences_in_sample": 0,
            "first_sequence": first, "last_sequence": last})
        kind["occurrences_in_sample"] += occurrences
        kind["first_sequence"], kind["last_sequence"] = min(kind["first_sequence"], first), max(kind["last_sequence"], last)
    ordered = sorted(kinds.values(), key=lambda kind: kind["first_sequence"])
    window.update(available=True, count=count, sampled=sampled, complete=sampled == count,
                  kinds_total=len(ordered), kinds=ordered[:ERROR_SAMPLE_KINDS])
    if sampled < count:
        limit = window["monitor_limit"]
        window["note"] = (f"at least {sampled} of {count} errors have a retained message; the monitor keeps "
                          f"{'at most ' + str(limit) if limit is not None else 'a bounded number of'} "
                          "distinct events, so this window's sample is truncated")
    return window


def clock_error_evidence(reads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Each clock step's own error window, from the snapshots read between the steps.

    `reads` maps the names in CLOCK_READS to the entity snapshots taken there. A window is the errors
    between two consecutive reads, so it is only measured when both were taken: a step that was never
    reached has no window, and a window the monitor could not measure has no count. Both are gaps,
    not zeros. The phase's verdict stays inside the clock evidence; it does not change any other
    measurement.
    """
    windows: dict[str, Any] = {}
    missing = []
    for before, name in zip(CLOCK_READS, CLOCK_READS[1:]):
        if name not in reads:
            missing.append(name)
            continue
        opening = reads.get(before)
        if opening is None:
            windows[name] = error_window(None, None)
            windows[name]["reason"] = f"the {before} read that opens this window was not taken"
            continue
        monitor = reads[name].get("runtime_errors") if isinstance(reads[name], dict) else None
        earlier = opening.get("runtime_errors") if isinstance(opening, dict) else None
        previous = _whole(earlier.get("observed")) if isinstance(earlier, dict) and earlier.get("active") is True else None
        windows[name] = error_window(monitor, previous)
    for name, window in windows.items():
        window["meaning"] = CLOCK_WINDOW_MEANING[name]
    unavailable = {name: window["reason"] for name, window in windows.items() if not window["available"]}
    measured = [window for window in windows.values() if window["available"]]
    incomplete = [window for window in measured if window["complete"] is not True]
    reasons = []
    if missing:
        reasons.append("the clock phase did not reach: " + ", ".join(missing))
    if unavailable:
        reasons.append("; ".join(f"{name}: {reason}" for name, reason in unavailable.items()))
    if incomplete:
        sampled = sum(window["sampled"] for window in incomplete)
        counted = sum(window["count"] for window in incomplete)
        reasons.append(f"in {len(incomplete)} window(s) at least {sampled} of {counted} errors have a retained "
                       "message, so the sample is truncated there")
    exact = not missing and not unavailable
    return {
        "windows": windows,
        "total_count": sum(window["count"] for window in measured) if measured and exact else None,
        "errors_observed": True if any(window["count"] for window in measured) else (False if exact else None),
        "sample_complete": False if incomplete else (True if exact else None),
        "gap": "; ".join(reasons) if reasons else None,
        "basis": ("counts are the monitor's counter differences between consecutive reads and are exact; "
                  "kinds are a bounded sample of the events the monitor retained"),
    }


async def _observe_paused(client: BridgeClient, paused: bool, *, min_game_s: float | None = None) -> Any:
    deadline = asyncio.get_running_loop().time() + 5.0
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await client.observe_once()
        if last.paused is paused and (min_game_s is None or (last.game_s is not None and last.game_s >= min_game_s)):
            return last
        await asyncio.sleep(0.05)
    raise ValueError(f"clock did not reach the requested paused={paused} state")


UNKNOWN_STEPPED_REASON = "animation, physics and game-specific timer behavior are not fully measured"


async def measure_clock(client: BridgeClient, entity_id: str, scene_id: str, *,
                        start: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pause, step and resume the clock, recording what the game logged during each step.

    `start` is the entity read that closed the previous phase; passing it leaves no gap between that
    phase's error window and this one's. Whether stepped mode is supported stays unknown. Errors the
    game logs while the clock is paused or stepped are evidence about that phase only: they are
    counted per step and never turn the calibration inconclusive.
    """
    result: dict[str, Any] = {"stepped_mode_supported": None, "reason": UNKNOWN_STEPPED_REASON,
        "pause": None, "step": None, "resume": None, "after_resume": None, "running_after_resume": None,
        "error": None, "runtime_errors": None}
    reads: dict[str, dict[str, Any]] = {}

    def note(message: str) -> None:
        result["error"] = (result["error"] + "; " if result["error"] else "") + message

    try:
        if start is None:
            start = await _read_entity(client, entity_id)
        reads["pre_pause"] = start
        await client.clock("pause")
        paused = await _observe_paused(client, True)
        paused_before = reads["at_pause"] = await _read_entity(client, entity_id)
        paused_fields = [await _read_fields(client, scene_id)]
        await asyncio.sleep(0.2)
        paused_after = reads["while_paused"] = await _read_entity(client, entity_id)
        paused_fields.append(await _read_fields(client, scene_id))
        p0, p1 = _vector(paused_before["position"]), _vector(paused_after["position"])
        if p0 is None or p1 is None:
            raise ValueError("paused entity position is unknown")
        result["pause"] = {"observation": paused.model_dump(mode="json"), "before": paused_before,
            "after": paused_after, "entity_displacement": _norm(_sub(p1, p0)),
            "game_time_delta_s": paused_after["game_time_s"] - paused_before["game_time_s"],
            "wall_time_delta_s": paused_after["real_time_s"] - paused_before["real_time_s"],
            "field_samples": paused_fields, "pre_pause": start}
        requested = 0.2
        await client.clock("step", step_game_s=requested)
        target = paused_after["game_time_s"] + requested * 0.75
        stepped = await _observe_paused(client, True, min_game_s=target)
        after_step = reads["after_step"] = await _read_entity(client, entity_id)
        step_fields = await _read_fields(client, scene_id)
        result["step"] = {"requested_game_s": requested, "observation": stepped.model_dump(mode="json"),
                          "after": after_step, "observed_game_s": after_step["game_time_s"] - paused_after["game_time_s"],
                          "field_sample": step_fields}
    except (BridgeError, ValueError) as exc:
        result["error"] = str(exc)
    finally:
        try:
            if not client.closed:
                # The last read with the clock still paused: errors after it belong to the resume. A failed
                # read must not keep the clock from being resumed.
                try:
                    reads["to_resume"] = await _read_entity(client, entity_id)
                except (BridgeError, ValueError) as exc:
                    note("read before resume failed: " + str(exc))
                await client.clock("resume")
                resumed = await _observe_paused(client, False)
                result["resume"] = resumed.model_dump(mode="json")
        except (BridgeError, ValueError) as exc:
            note("resume failed: " + str(exc))
        else:
            try:
                if not client.closed:
                    result["after_resume"] = reads["resume"] = await _read_entity(client, entity_id)
                    # A short window with the clock running is the control for everything above it.
                    await asyncio.sleep(RUNNING_OBSERVATION_S)
                    result["running_after_resume"] = reads["running"] = await _read_entity(client, entity_id)
            except (BridgeError, ValueError) as exc:
                note("read after resume failed: " + str(exc))
    evidence = clock_error_evidence(reads)
    result["runtime_errors"] = evidence
    if evidence["errors_observed"] is True:
        error_windows = ", ".join(name for name, window in evidence["windows"].items() if window["count"])
        result["reason"] = ("stepped-mode support is unknown: the game logged runtime errors in clock windows: "
                            + error_windows + "; see runtime_errors; " + UNKNOWN_STEPPED_REASON)
    elif evidence["gap"] is not None:
        result["reason"] = ("stepped-mode support is unknown: the runtime errors logged while the clock was paused or "
                            "stepped were not fully measured (" + evidence["gap"] + "); " + UNKNOWN_STEPPED_REASON)
    return result


def _summarize(result: dict[str, Any], actions: list[dict[str, Any]]) -> None:
    """The field-freshness and per-control summaries of everything this record holds, carried or new.

    A carried session's windows are windows of their own: `later_idle_windows` holds one window per pass's
    baseline, and freshness is only ever measured between adjacent samples inside one window, so no
    measurement is set against another session's idle baseline and no two sessions' values are mixed. Each
    control's held windows are summarised per control, so a carried control is summarised from its own
    session's windows and a control measured here from this session's.
    """
    from playtest.preflight.freshness import analyze_freshness

    segments = result["input_field_segments"]
    result["freshness"] = analyze_freshness(
        result["idle_fields"], [segment["samples"] for segment in segments],
        input_controls=[segment["control"] for segment in segments],
        later_idle_windows=result["later_idle_windows"])
    result["action_summaries"] = [
        summarize_action(action, [trial for trial in result["trials"]
                                  if trial.get("control") == action["control"]]) for action in actions]


async def measure(client: BridgeClient, selected: dict[str, Any], *,
                  on_plan: Callable[[dict[str, Any]], None] | None = None,
                  restore: Any = None, prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Calibrate the selected entity's controls and return the local record.

    `on_plan` is called with the plan, budget included, once the controls are known and before the
    first trial, so a host can size its own limits from the same numbers the record keeps. `restore` is
    the calibration's generic restore, called as `restore(trial=, evidence=, client=)` when a trial
    measured the game changing its own state; without one, such a trial stops the calibration instead of
    leaving it to measure a stopped game.

    `prior` is what earlier passes of the same session measured, shaped as `load_prior_state_changes`
    returns it. Within one play session no reset that keeps the measurement alive is a genuine reset, so a
    new pass starts from a fresh play session and is told what earlier passes settled: the controls that
    changed the game's state, and the controls whose whole trial set an earlier pass finished. Neither is
    pressed again, and their trials, field windows and idle baselines are carried into this record with
    `prior_pass` set, so the record still holds every fact with the evidence of the session that measured it.
    Only the controls still outstanding are measured, and each control's measurement comes from one session.

    A game can also change its own state between two trials, with no input: the body the trials have been
    moving reaches a hazard, a trigger, a conversation or the end of the level, and the game stops its clock
    or replaces its scene. That is not any control's effect: it is recorded as an unattributed session event
    in this record's trials, with its readings, and the pass ends without pressing another control.
    """
    if selected.get("status") != "identified" or not isinstance(selected.get("entity_id"), str):
        raise ValueError("a completed stage 2 entity record is required")
    if not isinstance(selected.get("stop"), dict) or selected["stop"].get("release_verified") is not True:
        raise ValueError("the stage 2 record has no verified input release")
    if not isinstance(selected.get("candidate_cap"), dict) or selected["candidate_cap"].get("truncated") is not False:
        raise ValueError("the stage 2 candidate list is incomplete")
    entity_id = selected["entity_id"]
    discovery = _complete(await client.inspect("discovery"), "discovery")
    if any(discovery["inspection_coverage"].get(name, {}).get("truncated") is not False
           for name in ("game_objects", "components", "bindings", "input_asset_bindings")):
        raise ValueError("the current discovery snapshot is incomplete")
    found = [obj for obj in discovery.get("game_objects", []) if obj.get("id") == entity_id]
    if len(found) != 1 or not isinstance(selected.get("entity"), dict):
        raise ValueError("the selected entity no longer resolves uniquely")
    entity = found[0]
    if any(entity.get(k) != selected["entity"].get(k) for k in ("scene_id", "scene_path", "path")):
        raise ValueError("the selected entity differs from the stage 2 record")
    previous_candidates = selected.get("candidates")
    if not isinstance(previous_candidates, list):
        raise ValueError("the stage 2 record has no candidate bindings")
    current_candidates, _ = candidate_controls(discovery)
    def candidate_ids(candidates: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
        result = {}
        for item in candidates:
            if not isinstance(item, dict) or not isinstance(item.get("control"), str) or not isinstance(item.get("binding_ids"), list):
                raise ValueError("candidate binding identities are malformed")
            if item["control"] in result or any(not isinstance(value, str) for value in item["binding_ids"]):
                raise ValueError("candidate binding identities are ambiguous")
            result[item["control"]] = tuple(sorted(item["binding_ids"]))
        return result
    if candidate_ids(previous_candidates) != candidate_ids(current_candidates):
        raise ValueError("discovered movement bindings differ from the stage 2 record")
    scene_id = entity["scene_id"]
    actions, unsupported = discover_actions(discovery)
    cap = {"limit": MAX_ACTIONS, "observed": len(actions), "returned": min(len(actions), MAX_ACTIONS),
           "truncated": len(actions) > MAX_ACTIONS}
    budget = measurement_budget(cap["returned"])
    carried = carried_trials(prior)
    carried_count = len(carried)
    baselines = carried_idle_windows(prior)
    result: dict[str, Any] = {"schema_version": 1, "status": "inconclusive", "error": None,
        "entity_id": entity_id, "entity": entity, "coordinate_frame": "Unity world space",
        "units": "Unity world units", "physical_unit": None,
        "plan": {"requested_holds_s": list(HOLD_S), "repeats": REPEATS,
                 "effect_tolerance": EFFECT_TOLERANCE, "idle_tolerance": IDLE_TOLERANCE,
                 "field_repeats": FIELD_REPEATS, "quiet_intervals": QUIET_INTERVALS,
                 "requested_field_interval_s": FIELD_INTERVAL_S, "field_samples": FIELD_SAMPLES,
                 "idle_samples": IDLE_SAMPLES,
                 "max_keyboard_state_age_s": MAX_KEYBOARD_STATE_AGE_S,
                 "max_input_state_age_s": MAX_KEYBOARD_STATE_AGE_S,
                 "view_turn": VIEW_TURN_PLAN,
                 "wall_budget_s": budget["wall_budget_s"], "budget": budget},
        "action_cap": cap, "actions": actions[:MAX_ACTIONS], "unsupported_bindings": unsupported,
        "discovery_coverage": discovery["inspection_coverage"], "idle_fields": [],
        "later_idle_windows": [], "input_field_segments": [], "field_sampling_gap": None,
        "trials": list(carried), "action_summaries": [],
        "freshness": None, "clock": None, "reset": None, "runtime_errors": None}
    if cap["truncated"] or not actions:
        result["error"] = "action candidates are incomplete" if cap["truncated"] else "no drivable input controls"
        return result
    # Each carried session keeps its own idle windows, in pass order. A carried measurement is analysed only
    # against the baseline its own session sampled: the windows stay separate windows of one record, and no
    # interval is ever compared across a window edge, so nothing is measured against another session's idle.
    result["later_idle_windows"] = [window for baseline in baselines for window in baseline["windows"]]
    result["input_field_segments"] = carried_field_segments(prior)
    if baselines:
        result["plan"]["prior_sessions"] = baseline_index(baselines)
    if on_plan is not None:
        on_plan(result["plan"])
    started = time.perf_counter()
    try:
        async with asyncio.timeout(budget["wall_budget_s"]):
            result["idle_fields"] = await _idle_window(client, scene_id)
            initial_monitor = await _read_entity(client, entity_id)
            seen_errors, initial_errors = _new_runtime_errors(initial_monitor, 0)
            result["runtime_errors"] = initial_monitor["runtime_errors"]
            if initial_errors:
                raise ValueError("the game logged errors before the first calibration action")
            unsupported_controls: set[str] = set()
            # Controls this pass does not press. A control the measured clock and scene evidence showed to
            # change the game's state stays in the record as a fact and is not pressed again, because every
            # further trial would measure a stopped or replaced game instead of the input's effect. A control
            # an earlier pass settled — it changed the game's state, or that pass finished its whole trial set
            # — is not pressed either: its measurement belongs to the session that produced it, and this one
            # is not that session.
            settled_controls: set[str] = {trial["control"] for trial in carried}
            previous_trial: dict[str, Any] | None = None
            previous_settled: Any = None
            after_restore = False
            for hold in HOLD_S:
                for repeat in range(FIELD_REPEATS if hold == HOLD_S[-1] else REPEATS):
                    for action in actions:
                        if action["control"] in unsupported_controls or action["control"] in settled_controls:
                            continue
                        quiet = await _quiet_window(client, entity_id)
                        if quiet is None:
                            result["trials"].append({"control": action["control"], "requested_hold_s": hold,
                                                     "valid": False, "error": "no quiet baseline was observed"})
                            continue
                        idle_start, before = quiet
                        observed, late_errors = _new_runtime_errors(before, seen_errors)
                        seen_errors = observed
                        result["runtime_errors"] = before["runtime_errors"]
                        if late_errors:
                            if previous_trial is None:
                                raise ValueError("the game logged errors before the first calibration action")
                            previous_trial["valid"] = False
                            previous_trial["error"] = "game errors were logged shortly after this input"
                            previous_trial.setdefault("game_errors", []).extend(late_errors)
                            unsupported_controls.add(previous_trial["control"])
                            for segment in result["input_field_segments"]:
                                if segment.get("action_id") == previous_trial.get("action_id"):
                                    segment["samples"] = []
                                    segment["excluded_reason"] = "game errors were logged after this input"
                        if action["control"] in unsupported_controls or action["control"] in settled_controls:
                            continue
                        observation = await client.observe_once()
                        # Before this press, did the game still run? The body the earlier trials moved has had
                        # time to reach something in the world, and a game that stopped its clock or changed
                        # its scene between then and now did so with no input: the control about to be pressed
                        # is not its cause. A restore is a new play session, so the trial after one is not
                        # compared with the trial before it.
                        if previous_trial is not None and not after_restore:
                            between = between_trial_window(previous_trial, previous_settled, before, observation)
                            if between is not None and between["verdict"] in BETWEEN_TRIAL_CHANGES:
                                event = session_event(between, previous_trial, len(result["trials"]),
                                                      len(result["trials"]) - 1)
                                result["trials"].append(event)
                                raise SessionStopped(event)
                        action_id = f"calibration-{action['control']}-{hold}-{repeat + 1}"
                        kwargs = {"action_id": action_id, "choice_id": action_id, "action": action["action"],
                                  "based_on_seq": observation.seq, "max_age_s": 2.0, "lease_s": hold}
                        result["trials"].append({"control": action["control"], "requested_hold_s": hold,
                                                 "valid": None, "observation_seq": observation.seq,
                                                 "error": "action sent; terminal result not yet received"})
                        if hold == HOLD_S[-1]:
                            records, responses = await client.act_with_inspections(
                                **kwargs, inspection_stage="fields", interval_s=FIELD_INTERVAL_S, max_samples=FIELD_SAMPLES)
                        else:
                            records, responses = await client.act(**kwargs), []
                        terminal = records[-1]
                        if terminal.status.value == "rejected":
                            unsupported_controls.add(action["control"])
                            result["trials"][-1].update(valid=False, rejected=True,
                                error="the bridge rejected this control",
                                execution=[r.model_dump(mode="json") for r in records])
                            previous_trial = None
                            continue
                        if terminal.status.value != "completed" or terminal.release_verified is not True:
                            raise ValueError(f"{action_id} did not complete with verified release")
                        await asyncio.sleep(0.08)
                        after = await _read_entity(client, entity_id)
                        # The view is read from observations, as the entity is read from transforms: the one
                        # that bound the action and one taken after the same settle as the entity read. The
                        # second observation also reports the scene and pause state the input left behind.
                        settled = await client.observe_once()
                        view_before, view_after = view_reading(observation), view_reading(settled)
                        observed, game_errors = _new_runtime_errors(after, seen_errors)
                        seen_errors = observed
                        result["runtime_errors"] = after["runtime_errors"]
                        trial = _trial_measure(before, after, idle_start, records, hold)
                        trial.update(control=action["control"], action_id=action_id, observation_seq=observation.seq,
                                     game_errors=game_errors, error=None, view_before=view_before,
                                     view_after=view_after, view_delta=view_turn(view_before, view_after))
                        # What the trial's own readings say about the game: a clock that stopped advancing, or
                        # a scene that changed, makes the displacement across it meaningless. This loop sends
                        # no clock request, so the bridge's clock is realtime here unless an observation says
                        # otherwise, and a bridge-initiated pause is not a game state change.
                        window = clock_window(_window_reading(before, observation),
                                              _window_reading(after, settled), bridge_paused=False)
                        trial.update(clock=window, state_change=None)
                        if window["valid"] is not True:
                            note = window["reason"] or "the trial's clock and scene were not measured"
                            trial["valid"] = False
                            trial["error"] = f"{trial['error']}; {note}" if trial["error"] else note
                        if game_errors:
                            trial["valid"] = False
                            trial["error"] = "game errors were logged during or shortly after this input"
                            unsupported_controls.add(action["control"])
                        result["trials"][-1] = trial
                        previous_trial, previous_settled, after_restore = trial, settled, False
                        if window["verdict"] == "unknown":
                            # The readings cannot say whether the game still runs. Measuring on is a guess, and
                            # the guess would be recorded as a measurement, so the calibration stops here.
                            raise ValueError(
                                "the trial's clock and scene evidence is unmeasured, so the state of the game "
                                "cannot be trusted: " + (window["reason"] or "no reason was measured"))
                        if window["state_change"]:
                            change = state_change_record(action, action_id, hold, window, len(result["trials"]))
                            trial["state_change"] = change
                            settled_controls.add(action["control"])
                            # Restore a running game before the next trial, and require that it runs. A
                            # calibration with no generic restore stops here instead of measuring a stopped game.
                            client, change["restore"] = await restore_running_game(
                                restore, client=client, entity_id=entity_id,
                                trial=len(result["trials"]) - 1, evidence=window)
                            after_restore = True
                        if hold == HOLD_S[-1]:
                            inspection_evidence = [{"wall_utc": response.wall_utc.isoformat(),
                                "real_time_s": response.data.get("real_time_s"),
                                "sample_wall_s": response.data.get("sample_wall_s"),
                                "keyboard_state": response.data.get("keyboard_state"),
                                "input_state": _input_state(response.data),
                                "within_execution": _within_execution(response, terminal),
                                "held_during_sample": _held_during_sample(
                                    response, terminal, action["control_name"], action["device"])} for response in responses]
                            samples = [_field_snapshot(response, scene_id) for response in responses
                                       if _held_during_sample(response, terminal, action["control_name"],
                                                              action["device"])
                                       and not game_errors]
                            result["input_field_segments"].append({"control": action["control"],
                                "action_id": action_id, "requested_hold_s": hold, "repeat": repeat + 1, "samples": samples,
                                "inspection_replies": len(responses),
                                "inspection_evidence": inspection_evidence,
                                "execution_start_wall_s": terminal.input_start_wall_s,
                                "execution_end_wall_s": terminal.input_end_wall_s})
                        await asyncio.sleep(0.2)
            from playtest.preflight.freshness import analyze_freshness
            result["later_idle_windows"].append(await _idle_window(client, scene_id))
            # This read closes the later idle window and opens the clock phase. The window is a kept
            # measurement, so a game error in it, or a monitor that cannot vouch for it, stops here.
            boundary = await _read_entity(client, entity_id)
            seen_errors, late_errors = _new_runtime_errors(boundary, seen_errors)
            result["runtime_errors"] = boundary["runtime_errors"]
            if late_errors:
                raise ValueError("game errors were logged outside a completed action window")
            # The clock phase has its own windows. What the game logs while paused is evidence about
            # that phase; a flood there cannot hide, or be mistaken for, an error in any other phase.
            result["clock"] = await measure_clock(client, entity_id, scene_id, start=boundary)
            latest = result["clock"].get("running_after_resume") or result["clock"].get("after_resume") or boundary
            result["runtime_errors"] = latest["runtime_errors"]
            _summarize(result, actions)
            if not any(trial.get("valid") is True for trial in result["trials"]):
                result["error"] = "no action produced a valid measurement"
            elif not any(len(segment["samples"]) >= 3 for segment in result["input_field_segments"]):
                if any(action.get("device") == "Keyboard" for action in actions):
                    result["error"] = "no held-input window has enough field samples"
                else:
                    result["field_sampling_gap"] = (
                        "no held-input field window was usable for the offered non-keyboard controls; "
                        "freshness and held-window evidence remain unknown for those controls")
                    result["status"] = "measured_with_gaps"
            else:
                result["status"] = "measured_with_gaps"
    except SessionStopped as stop:
        # The event is already in this record's trials, with the readings that measured it. Everything the
        # pass did finish is still summarised over all carried and new measurements, so a later pass can carry
        # the completed controls forward and the final record still holds their evidence.
        result["error"] = ("the game changed its own state between two trials, after this pass's inputs had been "
                           f"released: {stop}; the pass ended without pressing another control")
        try:
            _summarize(result, actions)
        except ValueError as exc:
            result["field_sampling_gap"] = f"the measured windows could not be summarised: {exc}"
    except (BridgeError, ValueError) as exc:
        result["error"] = str(exc)
    except TimeoutError:
        result["error"] = f"the calibration exceeded its wall-clock budget of {budget['wall_budget_s']:.0f} s"
    finally:
        budget["elapsed_s"] = time.perf_counter() - started
        # What this pass recorded, not what it carried in from earlier passes. A session event is a recorded
        # fact about the session, not a trial, so it is not counted as one.
        budget["trials_recorded"] = sum(1 for trial in result["trials"][carried_count:]
                                        if isinstance(trial, dict) and not trial.get("unattributed"))
    return result


async def _run(project: Path, selected: dict[str, Any], plan_output: Path | None = None,
               prior: dict[str, Any] | None = None) -> dict[str, Any]:
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    # A generic restore ends this session and connects a new one; the last connection is the one whose
    # verified release closes the calibration.
    adopted: list[BridgeClient] = []
    try:
        publish = None if plan_output is None else (lambda plan: write_plan(plan_output, plan))
        result = await measure(client, selected, on_plan=publish, restore=session_restore(project, adopted),
                               prior=prior)
    finally:
        stop = await (adopted[-1] if adopted else client).aclose()
    result["stop"] = stop.model_dump(mode="json") if stop is not None else None
    if stop is None or stop.release_verified is not True:
        result.update(status="inconclusive", error="bridge shutdown did not verify input release")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--entity", type=Path, required=True, help="a completed stage 2 entity record")
    parser.add_argument("--output", type=Path, required=True, help="new file outside the Unity project")
    parser.add_argument("--plan-output", type=Path,
                        help="new file outside the Unity project that receives the wall-clock plan before the first "
                             "trial, so the hosting Editor can size its own timeout from it")
    parser.add_argument("--prior-state-changes", type=Path,
                        help="a JSON file of what earlier passes measured, shaped "
                             '{"schema_version": 1, "state_changes": [<trial record>, ...], "sessions": [...]}: '
                             "the controls that changed the game's state are not pressed again and their trials "
                             "are carried into this record, and the controls earlier passes finished measuring are "
                             "carried with their own trials, field windows and idle baseline instead of being "
                             "measured again")
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    plan_output = args.plan_output.resolve() if args.plan_output is not None else None
    if output.is_relative_to(project):
        parser.error("--output must be outside the Unity project")
    if output.exists():
        parser.error("--output already exists")
    if plan_output is not None and (plan_output.is_relative_to(project) or plan_output.exists()
                                    or plan_output == output):
        parser.error("--plan-output must be a new file outside the Unity project, different from --output")
    try:
        selected = json.loads(args.entity.read_text(encoding="utf-8"))
        # Read before the session is touched: a prior file that cannot be trusted stops here, rather than
        # after a session has been spent pressing controls whose effects may already be known.
        prior = (load_prior_state_changes(args.prior_state_changes)
                 if args.prior_state_changes is not None else None)
        result = asyncio.run(_run(project, selected, plan_output, prior))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (BridgeError, BridgeLockError, OSError, ValueError) as exc:
        print(f"calibration failed: {exc}", file=sys.stderr)
        return 1
    print(f"calibration saved to {output}: {result['status']}")
    if result["status"] == "measured_with_gaps":
        return 0
    return EXIT_PASS_ENDED_BY_STATE_CHANGE if pass_ended_by_state_change(result) else 1


# A pass that this session's own evidence ended, by a control that changed the game's state or by a change the
# game made between trials, saved a complete record by design; a fresh pass continues from it. The exit code says
# so structurally, so a host need not reinterpret the record (or an error message) to tell it from a failure.
EXIT_PASS_ENDED_BY_STATE_CHANGE = 3


def pass_ended_by_state_change(record: dict[str, Any]) -> bool:
    """Whether this record's own session, not a carried earlier one, ended on a measured state change."""
    for entry in record.get("trials") or []:
        if not isinstance(entry, dict) or entry.get("prior_pass") is not None:
            continue
        if entry.get("unattributed") is True:
            return True
        change = entry.get("state_change")
        if isinstance(change, dict) and change.get("control"):
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
