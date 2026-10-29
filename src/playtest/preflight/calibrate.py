"""Measure preflight control effects, sampled field freshness, and clock behavior."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from playtest.contracts import action_catalog as catalog_contract
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError
from playtest.preflight.entity import _complete, _norm, _objects, _sub, _vector, candidate_controls


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
MEASUREMENT_BUDGET_S = 300.0
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

    Keyboard ids stay the original physical key tokens. Mouse and gamepad controls get stable
    canonical ids (mouse_delta, gamepad_leftstick, ...). A path outside the supported root
    controls is preserved as an unsupported binding with a reason; it is never remapped to a
    different device, an absolute pointer, a touch control or VR input.
    """
    actions: dict[str, dict[str, Any]] = {}
    unsupported = []
    for section in ("bindings", "input_asset_bindings"):
        for binding in discovery.get(section, []):
            if binding.get("binding_is_composite"):
                continue
            path = binding.get("binding_effective_path")
            descriptor = {"binding_id": binding.get("id"), "action_id": binding.get("action_id"),
                          "path": path, "part": binding.get("binding_name"),
                          "source_object_id": binding.get("source_object_id")}
            match = None
            device = None
            if isinstance(path, str):
                for candidate, pattern in (("Keyboard", KEY_PATH), ("Mouse", MOUSE_PATH),
                                           ("Gamepad", GAMEPAD_PATH)):
                    found = pattern.fullmatch(path)
                    if found:
                        device, match = candidate, found.group(1)
                        break
            resolved = _control_descriptor(device, match) if device is not None else None
            if resolved is None:
                if isinstance(path, str) and ("<Touchscreen>" in path or "<Pointer>" in path):
                    reason = "binding is an absolute pointer or touch control, which is unsupported"
                elif isinstance(path, str) and device == "Mouse" and match in ("position", "scroll"):
                    reason = ("relative scroll is not tested and remains unsupported" if match == "scroll"
                              else "absolute pointer position is unsupported")
                elif isinstance(path, str) and path.lower().startswith("<keyboard>/"):
                    reason = "keyboard path cannot be resolved to a supported key"
                else:
                    reason = "binding is not drivable by a supported input backend"
                unsupported.append({**descriptor, "effect": None, "variance": None, "inversion": None,
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
        if minimum is None and effective:
            minimum = hold
            durations = [t.get("execution", [{}])[-1].get("applied_duration_s") for t in selected
                         if isinstance(t.get("execution"), list) and t["execution"]]
            if len(durations) == len(selected) and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                       and math.isfinite(v) and v >= 0 for v in durations):
                observed_minimum = min(durations)
        groups.append({"requested_hold_s": hold, "valid_trials": len(magnitudes),
                       "mean_delta": mean_vector, "mean_magnitude": mean,
                       "sample_variance": variance, "effective_in_all_repeats": effective if len(magnitudes) >= REPEATS else None})

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
    if entity_id not in rows:
        raise ValueError("the selected entity is missing from the transform snapshot")
    row = rows[entity_id]
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


async def _observe_paused(client: BridgeClient, paused: bool, *, min_game_s: float | None = None) -> Any:
    deadline = asyncio.get_running_loop().time() + 5.0
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await client.observe_once()
        if last.paused is paused and (min_game_s is None or (last.game_s is not None and last.game_s >= min_game_s)):
            return last
        await asyncio.sleep(0.05)
    raise ValueError(f"clock did not reach the requested paused={paused} state")


async def measure_clock(client: BridgeClient, entity_id: str, scene_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"stepped_mode_supported": None, "reason":
        "animation, physics and game-specific timer behavior are not fully measured",
        "pause": None, "step": None, "resume": None, "error": None}
    try:
        start = await _read_entity(client, entity_id)
        await client.clock("pause")
        paused = await _observe_paused(client, True)
        paused_before = await _read_entity(client, entity_id)
        paused_fields = [await _read_fields(client, scene_id)]
        await asyncio.sleep(0.2)
        paused_after = await _read_entity(client, entity_id)
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
        after_step = await _read_entity(client, entity_id)
        step_fields = await _read_fields(client, scene_id)
        result["step"] = {"requested_game_s": requested, "observation": stepped.model_dump(mode="json"),
                          "after": after_step, "observed_game_s": after_step["game_time_s"] - paused_after["game_time_s"],
                          "field_sample": step_fields}
    except (BridgeError, ValueError) as exc:
        result["error"] = str(exc)
    finally:
        try:
            if not client.closed:
                await client.clock("resume")
                resumed = await _observe_paused(client, False)
                result["resume"] = resumed.model_dump(mode="json")
        except (BridgeError, ValueError) as exc:
            result["error"] = (result["error"] + "; " if result["error"] else "") + "resume failed: " + str(exc)
    return result


async def measure(client: BridgeClient, selected: dict[str, Any]) -> dict[str, Any]:
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
                 "wall_budget_s": MEASUREMENT_BUDGET_S},
        "action_cap": cap, "actions": actions[:MAX_ACTIONS], "unsupported_bindings": unsupported,
        "discovery_coverage": discovery["inspection_coverage"], "idle_fields": [],
        "later_idle_windows": [], "input_field_segments": [], "field_sampling_gap": None,
        "trials": [], "action_summaries": [],
        "freshness": None, "clock": None, "reset": None, "runtime_errors": None}
    if cap["truncated"] or not actions:
        result["error"] = "action candidates are incomplete" if cap["truncated"] else "no drivable input controls"
        return result
    try:
        async with asyncio.timeout(MEASUREMENT_BUDGET_S):
            result["idle_fields"] = await _idle_window(client, scene_id)
            initial_monitor = await _read_entity(client, entity_id)
            seen_errors, initial_errors = _new_runtime_errors(initial_monitor, 0)
            result["runtime_errors"] = initial_monitor["runtime_errors"]
            if initial_errors:
                raise ValueError("the game logged errors before the first calibration action")
            unsupported_controls: set[str] = set()
            previous_trial: dict[str, Any] | None = None
            for hold in HOLD_S:
                for repeat in range(FIELD_REPEATS if hold == HOLD_S[-1] else REPEATS):
                    for action in actions:
                        if action["control"] in unsupported_controls:
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
                        if action["control"] in unsupported_controls:
                            continue
                        observation = await client.observe_once()
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
                            result["trials"][-1].update(valid=False, error="the bridge rejected this control",
                                execution=[r.model_dump(mode="json") for r in records])
                            previous_trial = None
                            continue
                        if terminal.status.value != "completed" or terminal.release_verified is not True:
                            raise ValueError(f"{action_id} did not complete with verified release")
                        await asyncio.sleep(0.08)
                        after = await _read_entity(client, entity_id)
                        observed, game_errors = _new_runtime_errors(after, seen_errors)
                        seen_errors = observed
                        result["runtime_errors"] = after["runtime_errors"]
                        trial = _trial_measure(before, after, idle_start, records, hold)
                        trial.update(control=action["control"], action_id=action_id, observation_seq=observation.seq,
                                     game_errors=game_errors, error=None)
                        if game_errors:
                            trial["valid"] = False
                            trial["error"] = "game errors were logged during or shortly after this input"
                            unsupported_controls.add(action["control"])
                        result["trials"][-1] = trial
                        previous_trial = trial
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
            segments = result["input_field_segments"]
            result["freshness"] = analyze_freshness(
                result["idle_fields"], [segment["samples"] for segment in segments],
                input_controls=[segment["control"] for segment in segments],
                later_idle_windows=result["later_idle_windows"])
            result["action_summaries"] = [summarize_action(action,
                [trial for trial in result["trials"] if trial["control"] == action["control"]]) for action in actions]
            result["clock"] = await measure_clock(client, entity_id, scene_id)
            final_monitor = await _read_entity(client, entity_id)
            observed, late_errors = _new_runtime_errors(final_monitor, seen_errors)
            result["runtime_errors"] = final_monitor["runtime_errors"]
            if late_errors:
                raise ValueError("game errors were logged outside a completed action window")
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
    except (BridgeError, ValueError) as exc:
        result["error"] = str(exc)
    except TimeoutError:
        result["error"] = "the calibration exceeded its wall-clock budget"
    return result


async def _run(project: Path, selected: dict[str, Any]) -> dict[str, Any]:
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    try:
        result = await measure(client, selected)
    finally:
        stop = await client.aclose()
    result["stop"] = stop.model_dump(mode="json") if stop is not None else None
    if stop is None or stop.release_verified is not True:
        result.update(status="inconclusive", error="bridge shutdown did not verify input release")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--entity", type=Path, required=True, help="a completed stage 2 entity record")
    parser.add_argument("--output", type=Path, required=True, help="new file outside the Unity project")
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    if output.is_relative_to(project):
        parser.error("--output must be outside the Unity project")
    if output.exists():
        parser.error("--output already exists")
    try:
        selected = json.loads(args.entity.read_text(encoding="utf-8"))
        result = asyncio.run(_run(project, selected))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (BridgeError, BridgeLockError, OSError, ValueError) as exc:
        print(f"calibration failed: {exc}", file=sys.stderr)
        return 1
    print(f"calibration saved to {output}: {result['status']}")
    return 0 if result["status"] == "measured_with_gaps" else 1


if __name__ == "__main__":
    raise SystemExit(main())
