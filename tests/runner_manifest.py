"""Manifests for the runner tests, built from the shared `valid-basic` contract fixture.

Each helper keeps the manifest valid under `PreflightManifest`'s own cross-field rules (a tap hold must
match an effective calibrated hold, execution support follows the declared hold, and so on), so a test
varies one thing at a time.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from playtest.contracts.manifest import PreflightManifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "preflight_manifest"


def fixture(name: str = "valid-basic.json") -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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


def calibration(control: str, holds: list[dict[str, Any]], lowest: float | None = None) -> dict[str, Any]:
    return {"control": control, "bindings": [], "resolution": "unity_key_name", "holds": holds,
            "lowest_tested_effective_requested_hold_s": lowest, "shortest_observed_effective_hold_s": lowest,
            "minimum_hold_scope": "unknown", "inverted_relative_to_entity_basis": None,
            "signed_basis_projections": [], "dead_zone": None, "dead_zone_reason": "not measured"}


def action(name: str, controls: str | list[str] = "w", *, hold_s: float | None = 0.2, tap_hold_s: float | None = None,
           holds: list[dict[str, Any]] | None = None, preconditions: list[dict[str, Any]] | None = None,
           intent: str | None = None, axis: bool = False) -> dict[str, Any]:
    ids = [controls] if isinstance(controls, str) else controls
    measured = holds if holds is not None else []
    lowest = tap_hold_s if tap_hold_s is not None else None
    requested = hold_s if hold_s is not None else tap_hold_s
    matching = [h["effective_in_all_repeats"] for h in measured if h["requested_hold_s"] == requested] if len(ids) == 1 else []
    return {
        "name": name, "intent": intent or name,
        "inputs": [{"control_id": c, "x": None, "y": None} for c in ids],
        "hold_s": hold_s, "tap_hold_s": tap_hold_s, "preconditions": preconditions or [],
        "calibrations": [calibration(c, measured, lowest) for c in ids],
        "axis": axis, "max_hold_s": None,
        "hold_effect": matching[0] if len(matching) == 1 else None,
        "execution_supported": len(ids) == 1 and not axis and requested is not None and requested <= 3600,
    }


def manifest(*actions: dict[str, Any], controls: dict[str, str | None] | None = None) -> PreflightManifest:
    """A valid manifest with these actions; `controls` maps each offered control to its bridge action name."""
    data = copy.deepcopy(fixture())
    data["actions"] = list(actions)
    wanted = controls if controls is not None else {"w": "preflight_key_w"}
    rows = []
    for control, bridge_action in wanted.items():
        row = {"id": control, "control": control, "control_type": "Button", "bindings": [],
               "resolution": "unity_key_name"}
        if bridge_action is not None:
            row["action"] = bridge_action
        rows.append(row)
    data["selection"]["offered_controls"] = rows
    data["selection"]["action_cap_reached"] = len(actions) == 14
    return PreflightManifest.model_validate(data)


def manifest_json(*actions: dict[str, Any], controls: dict[str, str | None] | None = None) -> str:
    return manifest(*actions, controls=controls).model_dump_json()
