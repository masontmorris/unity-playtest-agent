"""Verify one explicitly named manifest action and its scene-reload return state.

This is a preflight measurement, not an S1 turn. The caller names one action that the
manifest already selected; this module executes exactly that action through the bridge
under a matching Editor catalog, compares its measured world-space effect to the same
control's repeated calibration hold, then asks the Editor harness for one scene reload
and compares the sampled post-reload state with the pre-action state.

Both halves are judged against calibration. The effect must match the repeated hold, and
it counts only when the bridge-measured hold reproduced the calibration trials' holds. The
reload must restore the entity position and every comparable field that calibration's
own reload trials restored each time. A field that calibration saw come back differently,
such as a frame counter, is reported but cannot decide the outcome; a field calibration
could not compare stays unknown.

Nothing here chooses, ranks, substitutes, retries or repairs an action. Missing
evidence stays unknown. A scene reload sample is not full game-state restoration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from playtest.contracts.calibration import ActionCalibration, CalibrationRecord, HoldMeasurement
from playtest.contracts.manifest import ManifestAction, PreflightManifest
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError
from playtest.preflight import calibrate
from playtest.preflight.entity import _norm, _sub, _vector


# Predeclared stage 7 measurement plan. Two idle reads plus the calibration's own
# effect threshold is 0.05 world units at the current calibration plan; the effect
# tolerance is the original calibration's sample spread plus that allowance.
MEASUREMENT_TOLERANCE = 2 * calibrate.IDLE_TOLERANCE + calibrate.EFFECT_TOLERANCE
BASIS_COSINE_MIN = 0.99
DIRECTION_COSINE_MIN = 0.95
# The same bound the stage 6 scene-reload record uses for a restored position.
POSITION_TOLERANCE = 0.01
# The bridge measures how long it actually held the key. A hold outside the matching
# calibration trials' applied holds by more than this (under one frame at 60 fps) is not
# the calibrated input, so its effect cannot confirm or refute the calibration.
APPLIED_HOLD_ALLOWANCE_S = 0.01
RELOAD_TIMEOUT_S = 120.0
SCALAR_KINDS = frozenset({"number", "integer", "boolean", "enum", "string"})
VECTOR_KINDS = frozenset({"vector2", "vector3", "vector4", "quaternion", "vector2_int", "vector3_int"})


@dataclass(frozen=True)
class SelectedAction:
    """The one manifest action and the exact calibration evidence it is compared with."""

    manifest: PreflightManifest
    calibration: CalibrationRecord
    action: ManifestAction
    summary: ActionCalibration
    hold: HoldMeasurement
    requested_hold_s: float
    control: str
    scene_id: str
    trials: tuple[dict[str, Any], ...]
    reload_fields: dict[str, dict[str, Any]]


def _reload_fields(calibration: CalibrationRecord) -> dict[str, dict[str, Any]]:
    """Calibration's per-field scene-reload counts by field id; absent evidence is empty."""
    scene_reload = calibration.reset.get("scene_reload") if isinstance(calibration.reset, dict) else None
    rows = scene_reload.get("fields") if isinstance(scene_reload, dict) else None
    if not isinstance(rows, list):
        return {}
    return {row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}


def reload_stable(row: dict[str, Any] | None) -> bool | None:
    """True when calibration's reload restored the field in every compared trial, at least twice.

    False when any compared reload left it different. A single matching trial, or no
    comparison at all, is not repeated evidence and stays unknown.
    """
    if row is None:
        return None
    compared, matched = row.get("trials_compared"), row.get("matches_initial_after_reload")
    if (isinstance(compared, bool) or not isinstance(compared, int) or compared < 1
            or isinstance(matched, bool) or not isinstance(matched, int) or not 0 <= matched <= compared):
        return None
    if matched < compared:
        return False
    return True if compared >= 2 else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _require_number(value: Any, message: str) -> float:
    number = _number(value)
    if number is None:
        raise ValueError(message)
    return number


def select_action(manifest: PreflightManifest, calibration: CalibrationRecord, name: str) -> SelectedAction:
    """Resolve the explicitly named action and its matching repeated measured hold.

    Selection is exact: no default action, no first action, no fallback calibration and
    no action whose control, entity or hold does not match its manifest entry.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("--action must name exactly one manifest action")
    found = [action for action in manifest.actions if action.name == name]
    if len(found) != 1:
        raise ValueError(f"the requested action {name!r} is not exactly one manifest action")
    action = found[0]
    if len(action.inputs) != 1:
        raise ValueError("stage 7 verifies exactly one button; this action does not have exactly one input")
    control = action.inputs[0].control_id
    if action.inputs[0].x is not None or action.inputs[0].y is not None:
        raise ValueError("the selected action carries analog parameters that the keyboard bridge does not send")
    if action.axis:
        raise ValueError("the selected action is an axis binding, not a single button")
    if action.execution_supported is not True and action.execution_evidence is not None:
        raise ValueError("stored execution evidence disagrees with the declaration's support flag")
    requested = action.hold_s if action.hold_s is not None else action.tap_hold_s
    if requested is None:
        raise ValueError("the selected action has no tested requested hold")
    requested = _require_number(requested, "the selected action's requested hold is unknown")

    if calibration.status != "measured_with_gaps":
        raise ValueError("the calibration record is not a completed measurement with gaps")
    if calibration.entity_id != manifest.entity_id:
        raise ValueError("the calibration record is for a different entity")
    if calibration.entity != manifest.entity:
        raise ValueError("the calibration record's entity details differ from the manifest")
    if manifest.entity.get("id") != manifest.entity_id:
        raise ValueError("the manifest entity does not match its entity id")
    scene_id = manifest.entity.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id:
        raise ValueError("the manifest entity has no measured scene id")
    if calibration.entity.get("scene_id") != scene_id:
        raise ValueError("the calibration entity has a different scene id")

    offered = {row.get("id"): row for row in manifest.selection.offered_controls}
    if control not in offered:
        raise ValueError("the selected control is not an offered control")
    if offered[control].get("control_type") != "Button":
        raise ValueError("the selected control is not a measured button")

    summaries = [summary for summary in calibration.action_summaries if summary.control == control]
    if len(summaries) != 1:
        raise ValueError("the calibration record has no unique summary for the selected control")
    summary = summaries[0]
    if action.calibrations[0].model_dump(mode="json") != summary.model_dump(mode="json"):
        raise ValueError("the action's calibration differs from the calibration record")

    holds = [hold for hold in summary.holds if hold.requested_hold_s == requested]
    if len(holds) != 1:
        raise ValueError("the calibration record has no unique hold matching the action's requested hold")
    hold = holds[0]
    if action.hold_effect is not True:
        raise ValueError("the manifest does not record a repeated effective calibration hold for this action")
    if (hold.effective_in_all_repeats is not True or hold.valid_trials < 2
            or hold.sample_variance is None or hold.mean_delta is None or hold.mean_magnitude is None):
        raise ValueError("the matching hold has no repeated effective measured trials")
    trials = tuple(
        trial for trial in calibration.trials
        if trial.get("control") == control and trial.get("requested_hold_s") == requested
        and trial.get("valid") is True
    )
    if len(trials) < 2 or len(trials) != hold.valid_trials:
        raise ValueError("the matching hold's repeated valid trials do not agree with its summary")
    if any(_vector(trial.get("effect_delta")) is None for trial in trials):
        raise ValueError("a matching calibration trial has no measured effect vector")
    return SelectedAction(manifest, calibration, action, summary, hold, requested, control, scene_id, trials,
                          _reload_fields(calibration))


def effect_tolerance(hold: HoldMeasurement, plan: dict[str, Any]) -> dict[str, Any]:
    """Predeclare the world-space effect tolerance before the action is sent.

    The tolerance is the calibration's measured sample spread (sample standard
    deviation of the repeated effect magnitudes) plus a fixed measurement allowance
    for two fresh reads, timestamp skew and the idle-baseline subtraction.
    """
    variance = hold.sample_variance
    if variance is None:
        raise ValueError("the matching hold has no measured sample variance")
    spread = math.sqrt(variance)
    idle_tolerance = _number(plan.get("idle_tolerance"))
    effect_tolerance = _number(plan.get("effect_tolerance"))
    if idle_tolerance is not None and idle_tolerance >= 0 and effect_tolerance is not None and effect_tolerance >= 0:
        measurement = 2 * idle_tolerance + effect_tolerance
    else:
        measurement = MEASUREMENT_TOLERANCE
    return {
        "formula": "sqrt(calibration sample variance) + (2 * calibration idle tolerance + calibration effect tolerance)",
        "calibration_spread": spread,
        "measurement_tolerance": measurement,
        "measurement_tolerance_basis": {"idle_tolerance": idle_tolerance,
                                        "effect_tolerance": effect_tolerance,
                                        "fallback": MEASUREMENT_TOLERANCE},
        "effect_tolerance": spread + measurement,
        "direction_cosine_min": DIRECTION_COSINE_MIN,
        "basis_cosine_min": BASIS_COSINE_MIN,
        "position_tolerance": POSITION_TOLERANCE,
        "applied_hold_allowance_s": APPLIED_HOLD_ALLOWANCE_S,
        "units": "Unity world units",
    }


def _applied_hold(execution: Any) -> float | None:
    """The bridge-measured hold of an action's terminal execution record, if it reported one."""
    if not isinstance(execution, list) or not execution or not isinstance(execution[-1], dict):
        return None
    return _number(execution[-1].get("applied_duration_s"))


def input_reproduction(selected: SelectedAction, trial: dict[str, Any]) -> dict[str, Any]:
    """Whether the bridge applied the same hold as the calibration trials it is compared with."""
    applied = _applied_hold(trial.get("execution"))
    calibrated = [_applied_hold(item.get("execution")) for item in selected.trials]
    known = [value for value in calibrated if value is not None]
    if applied is None or len(known) != len(calibrated):
        reproduced = None
        low = high = None
    else:
        low, high = min(known), max(known)
        reproduced = low - APPLIED_HOLD_ALLOWANCE_S <= applied <= high + APPLIED_HOLD_ALLOWANCE_S
    return {"applied_hold_s": applied, "calibration_applied_holds_s": calibrated,
            "calibration_range_s": [low, high] if low is not None else None,
            "allowance_s": APPLIED_HOLD_ALLOWANCE_S, "reproduced": reproduced}


def _basis(snapshot: dict[str, Any]) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    if snapshot.get("basis_state") != "present":
        return None
    forward, right = _vector(snapshot.get("world_forward")), _vector(snapshot.get("world_right"))
    if forward is None or right is None or _norm(forward) == 0 or _norm(right) == 0:
        return None
    return forward, right


def _cosine(a: tuple[float, float, float], b: tuple[float, float, float]) -> float | None:
    magnitude = _norm(a) * _norm(b)
    if magnitude == 0:
        return None
    return sum(x * y for x, y in zip(a, b)) / magnitude


def compare_basis(snapshot: dict[str, Any], trials: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Compare the live entity basis with every calibration trial that supplies the hold.

    A world delta is only interpreted as a vector when both bases were measured and
    agree. A measured but rotated basis makes the vector comparison inconclusive;
    missing basis evidence also stays unknown rather than being assumed.
    """
    live = _basis(snapshot)
    if live is None:
        return {"compatible": None, "minimum_cosine": BASIS_COSINE_MIN, "trials": [],
                "reason": "the live entity basis was not measured"}
    rows = []
    for index, trial in enumerate(trials):
        before = trial.get("before")
        measured = _basis(before) if isinstance(before, dict) else None
        if measured is None:
            return {"compatible": None, "minimum_cosine": BASIS_COSINE_MIN, "trials": rows,
                    "reason": f"calibration trial {index + 1} has no measured entity basis"}
        forward, right = _cosine(live[0], measured[0]), _cosine(live[1], measured[1])
        if forward is None or right is None:
            return {"compatible": None, "minimum_cosine": BASIS_COSINE_MIN, "trials": rows,
                    "reason": f"calibration trial {index + 1} has a degenerate entity basis"}
        rows.append({"trial": index + 1, "forward_cosine": forward, "right_cosine": right})
    compatible = all(
        row["forward_cosine"] >= BASIS_COSINE_MIN and row["right_cosine"] >= BASIS_COSINE_MIN
        for row in rows
    )
    reason = ("the live entity basis matches the calibration trials" if compatible
              else "the live entity basis is rotated relative to the calibration trials")
    return {"compatible": compatible, "minimum_cosine": BASIS_COSINE_MIN, "trials": rows, "reason": reason}


def _comparable(field: dict[str, Any]) -> bool:
    """Match the stage 6 field-comparability rule: only readable, non-truncated values."""
    state, kind, value = field.get("value_state"), field.get("value_kind"), field.get("value")
    if state == "null" and field.get("is_null") is True:
        return True
    if (state != "present" or value is None or field.get("value_truncated") is True
            or field.get("read_error") is not None):
        return False
    if kind in SCALAR_KINDS:
        if isinstance(value, bool) or isinstance(value, str):
            return True
        number = _number(value)
        return number is not None
    if kind in VECTOR_KINDS:
        return (isinstance(value, (list, tuple)) and 2 <= len(value) <= 4
                and all(_number(component) is not None for component in value))
    return False


def _field_row(field: dict[str, Any] | None, source: str) -> dict[str, Any]:
    if field is None:
        return {"id": None, "source": source, "value_state": None, "value_kind": None,
                "value": None, "comparable": False}
    return {"id": field.get("id"), "source": source, "value_state": field.get("value_state"),
            "value_kind": field.get("value_kind"), "value": field.get("value"),
            "comparable": _comparable(field)}


def _index_fields(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = snapshot.get("fields")
    if not isinstance(rows, list):
        raise ValueError("field snapshot has no fields list")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in indexed:
            raise ValueError("field snapshot contains an invalid or duplicate field id")
        indexed[row["id"]] = row
    return indexed


def compare_reload(
    pre_action: dict[str, Any],
    before_reload: dict[str, Any],
    after_reload: dict[str, Any],
    pre_fields: dict[str, Any],
    after_reload_fields: dict[str, Any],
    calibrated: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare the post-reload entity position and comparable sampled fields with the
    state sampled before the action. Unknowns stay null; this is not full-state proof.

    Only fields that calibration's reload trials restored every time decide the field
    outcome; every other field is still compared and reported."""
    calibrated = calibrated or {}
    start, moved, restored = (_vector(snapshot.get("position")) for snapshot in
                              (pre_action, before_reload, after_reload))
    distance = _norm(_sub(moved, start)) if start is not None and moved is not None else None
    restored_distance = _norm(_sub(restored, start)) if start is not None and restored is not None else None
    position = {
        "pre_action_position": list(start) if start is not None else None,
        "before_reload_position": list(moved) if moved is not None else None,
        "after_reload_position": list(restored) if restored is not None else None,
        "displacement_before_reload": distance,
        "distance_to_pre_action": restored_distance,
        "tolerance": POSITION_TOLERANCE,
        "restored": restored_distance <= POSITION_TOLERANCE if restored_distance is not None else None,
        "pre_action_timestamps": {key: pre_action.get(key) for key in ("frame", "real_time_s", "game_time_s", "wall_utc")},
        "before_reload_timestamps": {key: before_reload.get(key) for key in ("frame", "real_time_s", "game_time_s", "wall_utc")},
        "after_reload_timestamps": {key: after_reload.get(key) for key in ("frame", "real_time_s", "game_time_s", "wall_utc")},
    }
    first, last = _index_fields(pre_fields), _index_fields(after_reload_fields)
    fields = []
    for field_id in sorted(set(first) | set(last)):
        a, b = first.get(field_id), last.get(field_id)
        if a is not None and b is not None and _comparable(a) and _comparable(b):
            matches = (a.get("value_state"), a.get("value_kind"), a.get("value")) == (
                b.get("value_state"), b.get("value_kind"), b.get("value"))
        else:
            matches = None
        expectation = calibrated.get(field_id)
        stable = reload_stable(expectation)
        fields.append({
            "id": field_id,
            "pre_action": _field_row(a, "pre_action") if a is not None else None,
            "post_reload": _field_row(b, "post_reload") if b is not None else None,
            "matches_pre_action": matches,
            "calibration": ({key: expectation.get(key) for key in ("trials_compared", "matches_initial_after_reload")}
                            if expectation is not None else None),
            "calibrated_reload_stable": stable,
            "counted": stable is True and matches is not None,
        })
    compared = [row for row in fields if row["counted"]]
    if any(row["matches_pre_action"] is False for row in compared):
        outcome = "different"
    elif compared:
        outcome = "matched"
    else:
        outcome = None
    uncounted = [row for row in fields if not row["counted"] and row["matches_pre_action"] is not None]
    return {
        "method": "scene_reload",
        "requested": True,
        "completed": True,
        "entity_position": position,
        "fields": fields,
        "fields_outcome": outcome,
        "fields_outcome_basis": "comparable fields that calibration's scene reloads restored in every compared trial",
        "fields_compared": len(compared),
        "fields_total": len(fields),
        "fields_matched": sum(row["matches_pre_action"] is True for row in compared),
        "uncounted_comparable": {
            "not_reload_stable_in_calibration": sum(row["calibrated_reload_stable"] is False for row in uncounted),
            "no_repeated_calibration_evidence": sum(row["calibrated_reload_stable"] is None for row in uncounted),
            "differed": sum(row["matches_pre_action"] is False for row in uncounted),
        },
        "fields_sampled_at": {
            "pre_action": {key: pre_fields.get(key) for key in
                           ("frame", "real_time_s", "game_time_s", "wall_utc", "sample_wall_s")},
            "post_reload": {key: after_reload_fields.get(key) for key in
                            ("frame", "real_time_s", "game_time_s", "wall_utc", "sample_wall_s")},
        },
        "runtime_errors": after_reload.get("runtime_errors"),
        "full_state_restored": None,
        "reason": ("the sample covers the entity position and selected readable fields only; "
                   "it does not establish complete game-state restoration"),
        "save_load": {"works": None, "reason": "no save/load capability was discovered or exercised"},
        "checkpoint": {"works": None, "reason": "no checkpoint capability was discovered or exercised"},
    }


def compare_effect(
    selected: SelectedAction,
    trial: dict[str, Any],
    tolerance: dict[str, Any],
    basis: dict[str, Any],
    runtime_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare the one stage 7 effect to the matching calibration hold."""
    expected_vector = _vector(selected.hold.mean_delta)
    observed_vector = _vector(trial.get("effect_delta"))
    expected_magnitude = _number(selected.hold.mean_magnitude)
    observed_magnitude = _number(trial.get("effect_magnitude"))
    magnitude_difference = (abs(observed_magnitude - expected_magnitude)
                            if observed_magnitude is not None and expected_magnitude is not None else None)
    tolerance_value = tolerance["effect_tolerance"]
    magnitude_within = magnitude_difference <= tolerance_value if magnitude_difference is not None else None

    vector_distance = None
    direction_cosine = None
    vector_within = None
    if basis["compatible"] is True and expected_vector is not None and observed_vector is not None:
        vector_distance = _norm(_sub(observed_vector, expected_vector))
        vector_within = vector_distance <= tolerance_value
        direction_cosine = _cosine(observed_vector, expected_vector)

    reasons = []
    if runtime_errors:
        reasons.append("runtime errors were logged during or after the action")
    if trial.get("valid") is not True:
        reasons.append("the trial did not complete with verified release and a quiet baseline")
    if expected_vector is None or expected_magnitude is None or observed_vector is None or observed_magnitude is None:
        reasons.append("the expected or observed effect is unknown")
    valid = not reasons
    reproduction = input_reproduction(selected, trial)

    if not valid:
        comparison = "inconclusive"
    elif reproduction["reproduced"] is not True:
        comparison = "inconclusive"
        if reproduction["reproduced"] is None:
            reasons.append("the applied hold of the action or of a calibration trial is unknown")
        else:
            low, high = reproduction["calibration_range_s"]
            reasons.append(f"the bridge held the key {reproduction['applied_hold_s']:.4f} s, outside the calibration "
                           f"trials' {low:.4f}-{high:.4f} s by more than {APPLIED_HOLD_ALLOWANCE_S:g} s, so the "
                           "calibrated input was not reproduced")
    elif magnitude_within is None:
        comparison = "inconclusive"
        reasons.append("the magnitude difference is unknown")
    elif magnitude_within is False:
        comparison = "different"
        reasons.append("the observed effect magnitude is outside the predeclared tolerance")
    elif basis["compatible"] is not True:
        comparison = "inconclusive"
        reasons.append(basis["reason"])
    elif vector_distance is None or direction_cosine is None:
        comparison = "inconclusive"
        reasons.append("a world-vector comparison is not available")
    elif vector_within is True and direction_cosine >= DIRECTION_COSINE_MIN:
        comparison = "matched"
        reasons.append("the observed effect is within tolerance of the calibration in magnitude and direction")
    else:
        comparison = "different"
        reasons.append("the observed effect vector is outside the predeclared magnitude or direction tolerance")

    return {
        "valid": valid,
        "comparison": comparison,
        "reason": "; ".join(reasons) if reasons else "the effect comparison is available",
        "expected": {
            "effect_delta": list(expected_vector) if expected_vector is not None else None,
            "effect_magnitude": expected_magnitude,
            "sample_variance": selected.hold.sample_variance,
            "valid_trials": selected.hold.valid_trials,
            "requested_hold_s": selected.hold.requested_hold_s,
        },
        "observed": {
            "effect_delta": list(observed_vector) if observed_vector is not None else None,
            "effect_magnitude": observed_magnitude,
            "requested_hold_s": trial.get("requested_hold_s"),
            "valid": trial.get("valid"),
        },
        "magnitude_difference": magnitude_difference,
        "magnitude_within_tolerance": magnitude_within,
        "vector_distance": vector_distance,
        "vector_within_tolerance": vector_within,
        "direction_cosine": direction_cosine,
        "direction_cosine_min": DIRECTION_COSINE_MIN,
        "input_reproduction": reproduction,
        "basis": basis,
        "runtime_errors": runtime_errors,
        "tolerance": tolerance,
    }


def _summarize(effect: dict[str, Any] | None, reload: dict[str, Any]) -> tuple[str, str]:
    if effect is None:
        return "inconclusive", "the action effect was not measured"
    if effect["comparison"] == "different":
        return "different", effect["reason"]
    position = reload.get("entity_position", {}).get("restored")
    fields = reload.get("fields_outcome")
    if position is False:
        return "different", "the entity position after reload differs from its pre-action position"
    if fields == "different":
        return "different", "one or more reload-stable sampled fields differ after reload"
    if (effect["comparison"] == "matched" and position is True and fields in ("matched", None)
            and reload.get("completed") is True):
        if fields is None:
            return "verified", ("the action produced its calibrated effect and the sampled entity position "
                                "returned after the scene reload; no comparable field had repeated calibration "
                                "evidence that a reload restores it")
        return "verified", ("the action produced its calibrated effect and the sampled entity position and every "
                            "comparable field that calibration's reloads restored returned after the scene reload")
    return "inconclusive", "the action effect or the sampled return to the pre-action state is not established"


def _close_report(report: dict[str, Any]) -> dict[str, Any]:
    """Record attempt counts and a finish time on every path that returns a report."""
    execution = report["execution"]
    execution["attempt_count"] = len(execution["attempts"])
    execution["invalid_attempt_count"] = sum(item.get("valid") is not True for item in execution["attempts"])
    report["timestamps"].setdefault("finished_wall_utc", _utcnow().isoformat())
    return report


def _base_report(selected: SelectedAction, tolerance: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "stage": "stage_7_preflight_verification",
        "is_s1_turn": False,
        "model_calls": 0,
        "claim_scope": ("one manifest action's measured effect and a scene reload's sampled return to the "
                        "pre-action state, for the entity position and the fields calibration's reloads "
                        "restored; not complete game-state restoration"),
        "status": "inconclusive",
        "status_reason": "the verification did not complete",
        "error": None,
        "action": {
            "name": selected.action.name,
            "intent": selected.action.intent,
            "control": selected.control,
            "requested_hold_s": selected.requested_hold_s,
            "lease_s": selected.requested_hold_s,
            "hold_effect": selected.action.hold_effect,
            "execution_supported": selected.action.execution_supported,
            "inputs": [item.model_dump(mode="json") for item in selected.action.inputs],
            "preconditions": [item.model_dump(mode="json") for item in selected.action.preconditions],
        },
        "calibration": {
            "source_entity_id": selected.calibration.entity_id,
            "entity": selected.calibration.entity,
            "control": selected.control,
            "hold": selected.hold.model_dump(mode="json"),
            "summary": selected.summary.model_dump(mode="json"),
            "matching_trial_count": len(selected.trials),
            "matching_trials": [{
                "action_id": trial.get("action_id"),
                "requested_hold_s": trial.get("requested_hold_s"),
                "effect_delta": trial.get("effect_delta"),
                "effect_magnitude": trial.get("effect_magnitude"),
                "before_timestamps": {key: (trial.get("before") or {}).get(key) for key in
                                      ("frame", "real_time_s", "game_time_s", "wall_utc")},
                "applied_duration_s": (trial.get("execution") or [{}])[-1].get("applied_duration_s")
                if isinstance(trial.get("execution"), list) and trial["execution"] else None,
            } for trial in selected.trials],
        },
        "tolerance": tolerance,
        "pre_action": None,
        "expected": {
            "effect_delta": list(_vector(selected.hold.mean_delta)) if _vector(selected.hold.mean_delta) else None,
            "effect_magnitude": selected.hold.mean_magnitude,
            "sample_variance": selected.hold.sample_variance,
            "valid_trials": selected.hold.valid_trials,
            "requested_hold_s": selected.hold.requested_hold_s,
        },
        "observed": None,
        "execution": {
            "attempts": [],
            "terminal_status": None,
            "release_verified": None,
            "client_stop": None,
            "runtime_errors": {"initial": None, "before_action": None, "after_action": None,
                               "new_during_action": [], "after_action_monitor": None},
        },
        "effect": None,
        "reload": {"requested": False, "completed": False,
                   "reason": "no scene-reload harness was supplied", "host": None},
        "reset_bindings": {"selected": selected.manifest.reset_bindings,
                           "reason": selected.manifest.reset_bindings_reason},
        "save_load": {"works": None, "reason": "no save/load capability was discovered or exercised"},
        "checkpoint": {"works": None, "reason": "no checkpoint capability was discovered or exercised"},
        "timestamps": {"started_wall_utc": _utcnow().isoformat()},
    }


async def measure(
    client: BridgeClient,
    selected: SelectedAction,
    reload: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Execute exactly one named action once, measure it, and compare one reload.

    A rejected or unsafe action is recorded and never retried or replaced. The reload
    is requested only after an action completed with verified release. All bridge
    failures leave a report with `status=inconclusive` rather than a guessed value.
    """
    tolerance = effect_tolerance(selected.hold, selected.calibration.plan)
    report = _base_report(selected, tolerance)
    execution = report["execution"]
    runtime = execution["runtime_errors"]
    attempt: dict[str, Any] | None = None
    try:
        discovery = calibrate._complete(await client.inspect("discovery"), "discovery")
        objects = discovery.get("game_objects")
        if not isinstance(objects, list):
            raise ValueError("discovery has no object list")
        indexed: dict[str, dict[str, Any]] = {}
        for row in objects:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in indexed:
                raise ValueError("discovery has an invalid or duplicate object id")
            indexed[row["id"]] = row
        current = indexed.get(selected.manifest.entity_id)
        if current is None:
            raise ValueError("the manifest entity does not resolve in the current discovery snapshot")
        for key in ("scene_id", "scene_path", "path", "parent_id"):
            if current.get(key) != selected.manifest.entity.get(key):
                raise ValueError(f"the manifest entity's {key} differs from discovery")

        initial = await calibrate._read_entity(client, selected.manifest.entity_id)
        seen, initial_errors = calibrate._new_runtime_errors(initial, 0)
        runtime["initial"] = {"observed": seen, "events": initial_errors}

        quiet = await calibrate._quiet_window(client, selected.manifest.entity_id)
        if quiet is None:
            attempt = {"number": 1, "valid": False, "reason": "no quiet pre-action baseline was observed",
                       "action_id": None, "records": []}
            execution["attempts"].append(attempt)
            raise ValueError("no quiet pre-action baseline was observed")
        idle_start, before = quiet
        observed, before_errors = calibrate._new_runtime_errors(before, seen)
        seen = observed
        runtime["before_action"] = {"observed": seen, "events": before_errors}
        if before_errors:
            attempt = {"number": 1, "valid": False, "reason": "game errors were logged before the action",
                       "action_id": None, "records": []}
            execution["attempts"].append(attempt)
            raise ValueError("the game logged errors before the selected action")

        pre_fields = await calibrate._read_fields(client, selected.scene_id)
        report["pre_action"] = {
            "entity": before,
            "fields": pre_fields,
            "timestamps": {key: before.get(key) for key in ("frame", "real_time_s", "game_time_s", "wall_utc")},
        }

        observation = await client.observe_once()
        action_id = f"stage7-{uuid.uuid4().hex}"
        attempt = {
            "number": 1,
            "action_id": action_id,
            "action": selected.action.name,
            "control": selected.control,
            "requested_hold_s": selected.requested_hold_s,
            "lease_s": selected.requested_hold_s,
            "based_on_seq": observation.seq,
            "valid": None,
            "reason": None,
            "release_verified": None,
            "records": [],
        }
        execution["attempts"].append(attempt)
        report["timestamps"]["action_requested_wall_utc"] = _utcnow().isoformat()
        records = await client.act(
            action_id=action_id,
            choice_id=action_id,
            action=selected.action.name,
            based_on_seq=observation.seq,
            max_age_s=2.0,
            lease_s=selected.requested_hold_s,
        )
        attempt["records"] = [record.model_dump(mode="json") for record in records]
        await asyncio.sleep(0.08)
        after = await calibrate._read_entity(client, selected.manifest.entity_id)
        terminal = records[-1]
        execution["terminal_status"] = terminal.status.value
        execution["release_verified"] = terminal.release_verified
        attempt["terminal_status"] = terminal.status.value
        attempt["release_verified"] = terminal.release_verified

        if terminal.status.value == "rejected":
            attempt.update(valid=False, reason="the bridge rejected the selected action; it was not retried or replaced")
            report["status"] = "inconclusive"
            report["status_reason"] = attempt["reason"]
            return _close_report(report)
        if terminal.status.value != "completed" or terminal.release_verified is not True:
            attempt.update(valid=False, reason="the action did not complete with verified input release")
            observed, late_errors = calibrate._new_runtime_errors(after, seen)
            execution["runtime_errors"]["after_action"] = {"observed": observed, "events": late_errors}
            execution["runtime_errors"]["new_during_action"] = late_errors
            execution["runtime_errors"]["after_action_monitor"] = after.get("runtime_errors")
            report["observed"] = {"effect_delta": None, "effect_magnitude": None,
                                  "requested_hold_s": selected.requested_hold_s, "valid": False,
                                  "before": before, "after": after, "idle_start": idle_start,
                                  "raw_delta": None, "idle_delta": None}
            report["status"] = "inconclusive"
            report["status_reason"] = attempt["reason"]
            return _close_report(report)

        observed, new_errors = calibrate._new_runtime_errors(after, seen)
        runtime["after_action"] = {"observed": observed, "events": new_errors}
        runtime["new_during_action"] = new_errors
        runtime["after_action_monitor"] = after.get("runtime_errors")
        trial = calibrate._trial_measure(before, after, idle_start, records, selected.requested_hold_s)
        trial.update(control=selected.control, action_id=action_id)
        report["observed"] = {
            "effect_delta": trial["effect_delta"],
            "effect_magnitude": trial["effect_magnitude"],
            "raw_delta": trial["raw_delta"],
            "idle_delta": trial["idle_delta"],
            "requested_hold_s": selected.requested_hold_s,
            "valid": trial["valid"],
            "before": before,
            "after": after,
            "idle_start": idle_start,
            "execution": trial["execution"],
        }
        attempt["valid"] = trial["valid"] is True and not new_errors
        if not trial["valid"]:
            attempt["reason"] = "the trial did not complete with verified release and a quiet baseline"
        elif new_errors:
            attempt["reason"] = "runtime errors were logged during or after the action"
        else:
            attempt["reason"] = "the action completed and its input release was verified"

        basis = compare_basis(before, selected.trials)
        effect = compare_effect(selected, trial, tolerance, basis, new_errors)
        report["effect"] = effect

        if reload is None:
            report["reload"] = {"requested": False, "completed": False,
                                "reason": "no scene-reload harness was supplied", "host": None}
        else:
            report["reload"] = {"requested": True, "completed": False,
                                "reason": "the scene reload was requested", "host": None}
            report["timestamps"]["reload_requested_wall_utc"] = _utcnow().isoformat()
            evidence = await reload()
            if not isinstance(evidence, dict):
                raise ValueError("the scene-reload harness returned no evidence object")
            harness = evidence.get("evidence")
            report["reload"]["harness"] = harness
            host = _host_evidence(harness, selected)
            report["timestamps"]["reload_completed_wall_utc"] = _utcnow().isoformat()
            compared = compare_reload(before, after, evidence.get("entity") or {},
                                      pre_fields, evidence.get("fields") or {}, selected.reload_fields)
            compared.update(
                requested=True, completed=True, host=host,
                host_matches_action=True,
                post_reload_client_stop=evidence.get("stop"),
                harness_timestamps=harness,
            )
            report["reload"] = compared
        report["status"], report["status_reason"] = _summarize(report["effect"], report["reload"])
        return _close_report(report)
    except (BridgeError, ValueError, OSError, TimeoutError) as exc:
        report["error"] = str(exc)
        report["status"] = "inconclusive"
        report["status_reason"] = str(exc)
        if execution["attempts"]:
            last = execution["attempts"][-1]
            if last.get("valid") is None:
                last["valid"] = False
                last["reason"] = "the verification stopped before this attempt completed"
        return _close_report(report)


def _host_evidence(evidence: Any, selected: SelectedAction) -> dict[str, Any]:
    """Require the Editor harness to name the exact hosted action, control and lease."""
    if not isinstance(evidence, dict):
        raise ValueError("the scene-reload harness returned no evidence object")
    if evidence.get("method") != "scene_reload" or evidence.get("reloaded") is not True:
        raise ValueError("the scene-reload harness did not report a completed scene reload")
    host = evidence.get("action")
    if not isinstance(host, dict):
        raise ValueError("the scene-reload harness did not report its hosted action")
    if host.get("name") != selected.action.name or host.get("control") != selected.control:
        raise ValueError("the Editor host action name or physical control does not match the manifest action")
    lease = _number(host.get("default_lease_s"))
    if lease is None or abs(lease - selected.requested_hold_s) > 1e-6:
        raise ValueError("the Editor host lease does not match the manifest action's requested hold")
    scene_path = evidence.get("scene_path")
    expected = selected.manifest.entity.get("scene_path")
    if isinstance(scene_path, str) and isinstance(expected, str) and (
            scene_path.replace("\\", "/").casefold() != expected.replace("\\", "/").casefold()):
        raise ValueError("the reloaded scene path does not match the manifest entity's scene")
    return {
        "method": "scene_reload",
        "reloaded": True,
        "scene_path": scene_path,
        "action": host,
        "timestamps": {key: evidence.get(key) for key in
                       ("requested_at_utc", "completed_at_utc", "game_time_s", "real_time_s", "frame")},
    }


async def _reload_from_files(
    project: Path,
    selected: SelectedAction,
    request_path: Path,
    done_path: Path,
    timeout_s: float,
) -> dict[str, Any]:
    """Ask the Editor harness for one reload, then sample the reloaded state ourselves.

    The scene change ends the bridge's active session by design. The post-reload
    sample opens a new connection after the harness reports completion; the action is
    not replayed and no request is retried.
    """
    request = {
        "stage": "stage_7_preflight_verification",
        "action": {"name": selected.action.name, "control": selected.control,
                   "requested_hold_s": selected.requested_hold_s},
        "requested_at_utc": _utcnow().isoformat(),
    }
    # Publish the handshake only when it is complete; the harness reads it from another process.
    temporary = request_path.with_name(request_path.name + ".writing")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(request, stream, indent=2, allow_nan=False)
        stream.write("\n")
    if request_path.exists():
        raise ValueError("the scene-reload request path appeared while the request was being written")
    temporary.replace(request_path)
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        if done_path.exists():
            try:
                evidence = json.loads(done_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"the scene-reload evidence is not readable JSON: {exc}") from None
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise ValueError("the scene reload did not complete within its wall-clock limit")
        await asyncio.sleep(0.05)
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    try:
        entity = await calibrate._read_entity(client, selected.manifest.entity_id)
        fields = await calibrate._read_fields(client, selected.scene_id)
    finally:
        stop = await client.aclose()
    return {"entity": entity, "fields": fields, "evidence": evidence,
            "stop": stop.model_dump(mode="json") if stop is not None else None}


async def _run(
    project: Path,
    selected: SelectedAction,
    request_path: Path,
    done_path: Path,
    timeout_s: float,
) -> dict[str, Any]:
    # The request timeout must cover the action's own lease plus release and the samples.
    client_timeout_s = max(120.0, min(selected.requested_hold_s + 60.0, 3660.0))
    client = await BridgeClient.connect_from_project(project, timeout_s=client_timeout_s)
    try:
        report = await measure(
            client, selected,
            reload=lambda: _reload_from_files(project, selected, request_path, done_path, timeout_s),
        )
    finally:
        stop = await client.aclose()
    report["execution"]["client_stop"] = stop.model_dump(mode="json") if stop is not None else None
    return report


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="Unity project with a running Editor bridge")
    parser.add_argument("--manifest", type=Path, required=True, help="the saved preflight manifest")
    parser.add_argument("--calibration", type=Path, required=True, help="the saved calibration record")
    parser.add_argument("--action", required=True, help="the exact manifest action name to verify")
    parser.add_argument("--output", type=Path, required=True, help="new verification JSON outside the Unity project")
    parser.add_argument("--reload-request", type=Path, required=True,
                        help="new handshake file the Editor harness watches for")
    parser.add_argument("--reload-done", type=Path, required=True,
                        help="new handshake file the Editor harness writes after reloading")
    parser.add_argument("--reload-timeout", type=float, default=RELOAD_TIMEOUT_S,
                        help="maximum seconds to wait for the Editor harness's reload")
    args = parser.parse_args(argv)

    project = args.project.resolve()
    output = args.output.resolve()
    request = args.reload_request.resolve()
    done = args.reload_done.resolve()
    writing = (request.with_name(request.name + ".writing"), done.with_name(done.name + ".writing"))
    if not project.is_dir():
        parser.error("--project must be an existing Unity project directory")
    if not math.isfinite(args.reload_timeout) or args.reload_timeout <= 0:
        parser.error("--reload-timeout must be finite and positive")
    if not isinstance(args.action, str) or not args.action:
        parser.error("--action must name exactly one manifest action")
    paths = (output, request, done, *writing)
    for path in paths:
        if path.is_relative_to(project):
            parser.error("verification and handshake files must be outside the Unity project")
    if len(set(paths)) != len(paths):
        parser.error("verification and handshake files must be different paths")
    if any(path.exists() for path in paths):
        parser.error("verification and handshake files must be new")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    request, done = args.reload_request.resolve(), args.reload_done.resolve()
    try:
        manifest = PreflightManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"), strict=True)
        calibration = CalibrationRecord.model_validate_json(args.calibration.read_text(encoding="utf-8"), strict=True)
        selected = select_action(manifest, calibration, args.action)
    except (OSError, ValueError, TypeError) as exc:
        print(f"verification selection failed: {exc}", file=sys.stderr)
        return 1
    try:
        for path in (output, request, done):
            path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"could not create the verification output directory: {exc}", file=sys.stderr)
        return 1
    try:
        report = asyncio.run(_run(project, selected, request, done, args.reload_timeout))
    except (BridgeError, BridgeLockError, OSError, ValueError, TimeoutError) as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    report["sources"] = {
        "project": str(project),
        "manifest": str(args.manifest.resolve()),
        "calibration": str(args.calibration.resolve()),
    }
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError) as exc:
        print(f"could not save the verification report: {exc}", file=sys.stderr)
        return 1
    print(f"stage 7 verification saved to {output}: {report['status']}")
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
