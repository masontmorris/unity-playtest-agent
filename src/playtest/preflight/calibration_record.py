"""Combine a calibration with its separately measured scene-reload evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from playtest.contracts.calibration import CalibrationRecord
from playtest.preflight.entity import _norm, _sub, _vector


RESET_POSITION_TOLERANCE = 0.01
MIN_PRE_RESET_MOVEMENT = 1.0


def _index_fields(trial: dict[str, Any], name: str) -> dict[str, dict[str, Any]]:
    rows = trial.get(name)
    if not isinstance(rows, list):
        raise ValueError(f"reset evidence is missing {name}")
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in found:
            raise ValueError("reset evidence has invalid or duplicate field ids")
        found[row["id"]] = row
    return found


def _reload_trial(trial: dict[str, Any]) -> dict[str, Any]:
    """One scene reload after a held key: where the entity went, and what came back."""
    if not isinstance(trial, dict) or trial.get("release_verified") is not True:
        raise ValueError("a reset trial did not verify the key release")
    start = _vector(trial.get("initial_position"))
    before = _vector(trial.get("before_reload_position"))
    after = _vector(trial.get("after_reload_position"))
    if start is None or before is None or after is None:
        raise ValueError("reset positions are unknown")
    before_distance = _norm(_sub(before, start))
    after_distance = _norm(_sub(after, start))
    moved = before_distance >= MIN_PRE_RESET_MOVEMENT

    first, changed, reloaded = (_index_fields(trial, name) for name in
                                ("initial_fields", "before_reload_fields", "after_reload_fields"))
    fields = []
    for field_id in sorted(set(first) | set(changed) | set(reloaded)):
        a, b, c = (rows.get(field_id) for rows in (first, changed, reloaded))
        if all(isinstance(row, dict) and row.get("comparable") is True for row in (a, b, c)):
            def value(row: dict[str, Any]) -> tuple[Any, Any, Any]:
                return row.get("state"), row.get("kind"), row.get("value")
            fields.append({"id": field_id, "changed_before_reload": value(a) != value(b),
                           "matches_initial_after_reload": value(a) == value(c)})
        else:
            fields.append({"id": field_id, "changed_before_reload": None, "matches_initial_after_reload": None})
    return {"key": trial.get("key"), "requested_hold_s": trial.get("requested_hold_s"),
            "applied_hold_s": trial.get("applied_hold_s"), "release_verified": True,
            "entity_moved_before_reload": moved,
            "entity_position_restored": after_distance <= RESET_POSITION_TOLERANCE if moved else None,
            "distance_before_reload": before_distance, "distance_after_reload": after_distance,
            "fields": fields}


def _field_counts(moved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-field counts over the trials where the entity moved and the field was comparable throughout."""
    counts: dict[str, dict[str, Any]] = {}
    for trial in moved:
        for row in trial["fields"]:
            entry = counts.setdefault(row["id"], {"id": row["id"], "trials_compared": 0, "changed_before_reload": 0,
                                                  "restored_after_change": 0, "matches_initial_after_reload": 0})
            if row["changed_before_reload"] is None:
                continue
            entry["trials_compared"] += 1
            entry["changed_before_reload"] += row["changed_before_reload"]
            entry["restored_after_change"] += row["changed_before_reload"] and row["matches_initial_after_reload"]
            entry["matches_initial_after_reload"] += row["matches_initial_after_reload"]
    rows = []
    for entry in sorted(counts.values(), key=lambda item: item["id"]):
        if entry["trials_compared"] == 0:
            entry = {"id": entry["id"], "trials_compared": 0, "changed_before_reload": None,
                     "restored_after_change": None, "matches_initial_after_reload": None}
        rows.append(entry)
    return rows


def combine(calibration: dict[str, Any], reset: dict[str, Any]) -> dict[str, Any]:
    stop = calibration.get("stop")
    if calibration.get("status") != "measured_with_gaps" or not isinstance(stop, dict) or stop.get("release_verified") is not True:
        raise ValueError("calibration did not finish with verified input release")
    if reset.get("method") != "scene_reload" or reset.get("entity_id") != calibration.get("entity_id"):
        raise ValueError("reset evidence does not match the calibrated entity")
    raw_trials = reset.get("trials")
    if not isinstance(raw_trials, list) or not raw_trials:
        raise ValueError("reset evidence has no trials")
    trials = [_reload_trial(trial) for trial in raw_trials]
    moved = [trial for trial in trials if trial["entity_moved_before_reload"]]
    restored = sum(trial["entity_position_restored"] is True for trial in moved)
    result = dict(calibration)
    result["reset"] = {
        "scene_reload": {"trials": trials, "trial_count": len(trials), "moved_trial_count": len(moved),
            "position_restored": {"restored": restored, "of": len(moved)} if moved else None,
            "fields": _field_counts(moved), "position_tolerance": RESET_POSITION_TOLERANCE,
            "minimum_pre_reset_movement": MIN_PRE_RESET_MOVEMENT, "full_state_restored": None,
            "reason": "sampled fields and entity position do not establish complete game-state restoration",
            "evidence": reset},
        "save_load": {"works": None, "reason": "no generic save/load operation was exercised"},
        "checkpoint": {"works": None, "reason": "no generic checkpoint operation was exercised"},
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--reset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    if output.is_relative_to(project):
        parser.error("--output must be outside the Unity project")
    if output.exists():
        parser.error("--output already exists")
    try:
        calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
        reset = json.loads(args.reset.read_text(encoding="utf-8"))
        result = combine(calibration, reset)
        validated = CalibrationRecord.model_validate(result)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(validated.model_dump(mode="json"), stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError) as exc:
        print(f"could not combine calibration evidence: {exc}")
        return 1
    print(f"calibration record saved to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
