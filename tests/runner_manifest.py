"""Manifests for the runner tests, built from the shared `valid-basic` contract fixture.

Each helper keeps the manifest valid under `PreflightManifest`'s own cross-field rules (a tap hold must
match an effective calibrated hold, execution support follows exact release-verified evidence for the
declared inputs, values, holds and waits, and so on), so a test varies one thing at a time.

Evidence is generated from the declaration it proves, the way a perfect run would report it: one receipt
per declared input in step order, a mouse impulse's delta exactly `value * 64` pixels, a chord's inputs
applied and released together, and a skill's steps released and waited in order. The manifest validator
then accepts or rejects it by its own rules, so a helper cannot make a declaration look proven that is not.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from playtest.contracts.action_catalog import MOUSE_DELTA_PIXEL_BOUND, control_type, is_pointer_impulse
from playtest.contracts.manifest import PreflightManifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "preflight_manifest"
START_S = 1000.0


def fixture(name: str = "valid-basic.json") -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- inputs ---------------------------------------------------------------------------------


def key(control: str) -> dict[str, Any]:
    return button("Keyboard", control)


def button(device: str, control: str) -> dict[str, Any]:
    return {"device": device, "control": control, "x": None, "y": None}


def vector(device: str, control: str, x: float, y: float) -> dict[str, Any]:
    return {"device": device, "control": control, "x": x, "y": y}


def control_id(spec: dict[str, Any]) -> str:
    """The id preflight offers a control under: a key's own name, else `<device>_<control>` in lower case."""
    return spec["control"] if spec["device"] == "Keyboard" else f"{spec['device'].lower()}_{spec['control'].lower()}"


def _spec(item: str | dict[str, Any]) -> dict[str, Any]:
    return key(item) if isinstance(item, str) else dict(item)


def _manifest_input(spec: dict[str, Any]) -> dict[str, Any]:
    return {"control_id": control_id(spec), **spec}


# --- measurements and evidence --------------------------------------------------------------


def hold(requested: float, *, trials: int = 2, mean: float | None = 6.04, variance: float | None = 0.0001,
         effective: bool | None = True) -> dict[str, Any]:
    """A calibrated hold, with the unknowns a count of trials implies left null."""
    if trials == 0:
        mean = variance = effective = None
    elif trials < 2:
        variance = effective = None
    return {"requested_hold_s": requested, "valid_trials": trials,
            "mean_delta": [mean, 0.0, 0.0] if mean is not None else None, "mean_magnitude": mean,
            "sample_variance": variance, "effective_in_all_repeats": effective}


def _receipt(index: int, spec: dict[str, Any], start: float, hold_s: float) -> dict[str, Any]:
    impulse = is_pointer_impulse(spec["device"], spec["control"])
    return {
        "step_index": index, "device": spec["device"], "control": spec["control"], "x": spec["x"], "y": spec["y"],
        "input_start_wall_s": start, "input_end_wall_s": start + hold_s,
        "applied_duration_s": None if impulse else hold_s,
        "applied_delta_x": spec["x"] * MOUSE_DELTA_PIXEL_BOUND if impulse else None,
        "applied_delta_y": spec["y"] * MOUSE_DELTA_PIXEL_BOUND if impulse else None,
        "release_verified": True,
    }


def exact_evidence(name: str, declaration: dict[str, Any], steps: list[tuple[list[dict[str, Any]], float, float]]
                   ) -> dict[str, Any]:
    """A release-verified completed execution for exactly this declaration.

    `steps` is `(inputs, hold_s, wait_s)` per step (one step for an action). Step `i` is applied when step
    `i - 1` was released and its wait passed; the terminal window ends after the last step's own wait.
    """
    receipts: list[dict[str, Any]] = []
    now = START_S
    for index, (inputs, hold_s, wait_s) in enumerate(steps):
        receipts += [_receipt(index, spec, now, hold_s) for spec in inputs]
        now += hold_s + wait_s
    return {
        "action": name, "session": "runner-fixture", "status": "completed", "release_verified": True,
        "declaration": declaration, "applied_duration_s": now - START_S,
        "input_start_wall_s": START_S, "input_end_wall_s": now, "receipts": receipts,
        "wall_utc": "2026-01-01T00:00:00Z",
    }


def evidence(name: str, control: str, requested: float) -> dict[str, Any]:
    """A release-verified completed execution for exactly this one-button declaration."""
    declaration = {"backend": "input_system", "inputs": [key(control)], "default_lease_s": requested}
    return exact_evidence(name, declaration, [([key(control)], requested, 0.0)])


def calibration(control: str, holds: list[dict[str, Any]], lowest: float | None = None,
                spec: dict[str, Any] | None = None, calibrated: tuple[float | None, float | None] | None = None
                ) -> dict[str, Any]:
    """One control's calibration summary. A key is the keyboard-only shape; any other control records its device,
    type and the one declared value calibration measured (`calibrated`, default the declaration's own)."""
    row = {"control": control, "bindings": [], "resolution": "unity_key_name", "holds": holds,
           "lowest_tested_effective_requested_hold_s": lowest, "shortest_observed_effective_hold_s": lowest,
           "minimum_hold_scope": "unknown", "inverted_relative_to_entity_basis": None,
           "signed_basis_projections": [], "dead_zone": None, "dead_zone_reason": "not measured"}
    if spec is not None and spec["device"] != "Keyboard":
        x, y = calibrated if calibrated is not None else (spec["x"], spec["y"])
        row.update(device=spec["device"], control_name=spec["control"],
                   control_type=control_type(spec["device"], spec["control"]), declared_x=x, declared_y=y)
    return row


# --- declarations ---------------------------------------------------------------------------


def action(name: str, controls: str | dict[str, Any] | list[str | dict[str, Any]] = "w", *,
           hold_s: float | None = 0.2, tap_hold_s: float | None = None,
           holds: list[dict[str, Any]] | None = None, preconditions: list[dict[str, Any]] | None = None,
           intent: str | None = None, axis: bool = False, supported: bool | None = None,
           calibrated: tuple[float | None, float | None] | None = None) -> dict[str, Any]:
    """One manifest action. `controls` is a key name, an input spec (`button`, `vector`) or a list of them.

    It is supported (carries exact evidence for what it declares) unless `supported` says otherwise or it
    has no resolved hold. `calibrated` overrides the value calibration measured for a single non-key input.
    """
    items = controls if isinstance(controls, list) else [controls]
    specs = [_spec(item) for item in items]
    ids = [control_id(spec) for spec in specs]
    measured = holds if holds is not None else []
    lowest = tap_hold_s if tap_hold_s is not None else None
    requested = hold_s if hold_s is not None else tap_hold_s
    # As the contract requires: a hold effect only for the exact input value calibration measured.
    single = specs[0] if len(ids) == 1 else None
    same_value = single is not None and (
        single.get("device") == "Keyboard"
        or (single.get("x"), single.get("y")) == (calibrated if calibrated is not None else (single.get("x"), single.get("y"))))
    matching = [h["effective_in_all_repeats"] for h in measured if h["requested_hold_s"] == requested] if same_value else []
    proven = requested is not None if supported is None else supported and requested is not None
    declaration = {"backend": "input_system", "inputs": specs, "default_lease_s": requested}
    return {
        "name": name, "intent": intent or name,
        "inputs": [_manifest_input(spec) for spec in specs],
        "hold_s": hold_s, "tap_hold_s": tap_hold_s, "preconditions": preconditions or [],
        "calibrations": [calibration(cid, measured, lowest, spec, calibrated) for cid, spec in zip(ids, specs)],
        "axis": axis, "max_hold_s": None,
        "hold_effect": matching[0] if len(matching) == 1 else None,
        "execution_evidence": exact_evidence(name, declaration, [(specs, requested, 0.0)]) if proven else None,
        "execution_supported": proven,
    }


def skill(name: str, steps: list[tuple[list[str | dict[str, Any]], float, float]], max_s: float, *,
          purpose: str | None = None, supported: bool = True) -> dict[str, Any]:
    """One manifest skill. `steps` is `(inputs, hold_s, wait_s)` in order; every control gets an empty calibration."""
    declared = [([_spec(item) for item in inputs], hold_s, wait_s) for inputs, hold_s, wait_s in steps]
    specs = {control_id(spec): spec for inputs, _, _ in declared for spec in inputs}
    declaration = {"backend": "input_system", "max_s": max_s, "steps": [
        {"inputs": inputs, "hold_s": hold_s, "wait_s": wait_s} for inputs, hold_s, wait_s in declared]}
    return {
        "name": name, "purpose": purpose or name,
        "steps": [{"inputs": [_manifest_input(spec) for spec in inputs], "hold_s": hold_s, "tap_hold_s": None,
                   "wait_s": wait_s} for inputs, hold_s, wait_s in declared],
        "max_s": max_s,
        "calibrations": [calibration(cid, [], None, spec) for cid, spec in specs.items()],
        "execution_evidence": exact_evidence(name, declaration, declared) if supported else None,
        "execution_supported": supported,
    }


def manifest(*actions: dict[str, Any], skills: tuple[dict[str, Any], ...] = (),
             controls: dict[str, str | None] | None = None) -> PreflightManifest:
    """A valid manifest with these actions and skills.

    The offered controls are exactly those the declarations use. `controls` may name a legacy per-key bridge
    action for a control (`preflight_key_w`); it rides in the row as discovery wrote it and nothing reads it.
    """
    data = copy.deepcopy(fixture())
    data["actions"] = list(actions)
    data["skills"] = list(skills)
    wanted = controls if controls is not None else {}
    rows: dict[str, dict[str, Any]] = {}
    declared = [item for entry in actions for item in entry["inputs"]]
    declared += [item for entry in skills for step in entry["steps"] for item in step["inputs"]]
    for item in declared:
        row = {"id": item["control_id"], "control": item["control"], "device": item["device"],
               "control_type": control_type(item["device"], item["control"]),
               "bindings": [], "resolution": "unity_key_name"}
        legacy = wanted.get(item["control_id"], f"preflight_key_{item['control']}" if item["device"] == "Keyboard" else None)
        if legacy is not None:
            row["action"] = legacy
        rows[item["control_id"]] = row
    data["selection"]["offered_controls"] = list(rows.values())
    data["selection"]["action_cap_reached"] = len(actions) == 14
    data["selection"]["skill_cap_reached"] = len(skills) == 6
    return PreflightManifest.model_validate(data)


def manifest_json(*actions: dict[str, Any], skills: tuple[dict[str, Any], ...] = (),
                  controls: dict[str, str | None] | None = None) -> str:
    return manifest(*actions, skills=skills, controls=controls).model_dump_json()


def all_modes() -> PreflightManifest:
    """One proven declaration of every mode the catalog can hold, with unproven ones interleaved.

    Supported: a key, a keyboard chord, a three-device chord, a mouse impulse, a stick, a trigger, a dpad,
    a gamepad button, a mouse button, and two ordered skills (one with a mouse impulse step). Unproven, so
    never exported: a chord and a skill. Each value is distinct so a swapped or reduced declaration shows.
    """
    return manifest(
        action("key_hold", "w", hold_s=0.2, holds=[hold(0.2)]),
        action("key_chord", ["w", "a"], hold_s=0.2),
        action("unproven_chord", ["w", "d"], supported=False),
        action("device_chord", [key("w"), vector("Gamepad", "rightTrigger", 1.0, 0.0), button("Mouse", "leftButton")], hold_s=0.3),
        action("pointer_impulse", vector("Mouse", "delta", 0.5, -0.25), hold_s=0.1),
        action("stick_value", vector("Gamepad", "leftStick", 0.3, 1.0), hold_s=0.5, holds=[hold(0.5, mean=2.0)]),
        action("trigger_value", vector("Gamepad", "leftTrigger", 0.5, 0.0), hold_s=0.3),
        action("dpad_value", vector("Gamepad", "dpad", 0.0, 1.0), hold_s=0.2),
        action("pad_button", button("Gamepad", "buttonSouth"), hold_s=0.1),
        action("mouse_button", button("Mouse", "leftButton"), hold_s=0.1),
        skills=(
            skill("key_sequence", [(["w"], 0.2, 0.1), (["a", "space"], 0.2, 0.0)], 1.0, purpose="two keys in order"),
            skill("unproven_skill", [(["d"], 0.2, 0.0)], 1.0, supported=False),
            skill("pointer_sequence", [([vector("Mouse", "delta", 0.5, -0.25), button("Mouse", "leftButton")], 0.05, 0.1),
                            ([vector("Gamepad", "leftStick", 0.0, 1.0)], 0.2, 0.0)], 0.5, purpose="a pointer step then a stick step"),
        ))
