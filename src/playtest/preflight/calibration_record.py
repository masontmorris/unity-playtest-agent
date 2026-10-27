"""Combine a calibration with its separately measured scene-reload evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from playtest.contracts.calibration import CalibrationRecord
from playtest.preflight.entity import _norm, _sub, _vector


RESET_POSITION_TOLERANCE = 0.01
MIN_PRE_RESET_MOVEMENT = 0.03


def combine(calibration: dict[str, Any], reset: dict[str, Any]) -> dict[str, Any]:
    stop = calibration.get("stop")
    if calibration.get("status") != "measured_with_gaps" or not isinstance(stop, dict) or stop.get("release_verified") is not True:
        raise ValueError("calibration did not finish with verified input release")
    if reset.get("method") != "scene_reload" or reset.get("entity_id") != calibration.get("entity_id"):
        raise ValueError("reset evidence does not match the calibrated entity")
    start = _vector(reset.get("initial_position"))
    before = _vector(reset.get("before_reload_position"))
    after = _vector(reset.get("after_reload_position"))
    if start is None or before is None or after is None:
        raise ValueError("reset positions are unknown")
    before_distance = _norm(_sub(before, start))
    after_distance = _norm(_sub(after, start))
    restored = after_distance <= RESET_POSITION_TOLERANCE if before_distance >= MIN_PRE_RESET_MOVEMENT else None

    def index(name: str) -> dict[str, dict[str, Any]]:
        rows = reset.get(name)
        if not isinstance(rows, list):
            raise ValueError(f"reset evidence is missing {name}")
        found = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in found:
                raise ValueError("reset evidence has invalid or duplicate field ids")
            found[row["id"]] = row
        return found

    first, changed, reloaded = (index(name) for name in
                                ("initial_fields", "before_reload_fields", "after_reload_fields"))
    fields = []
    for field_id in sorted(set(first) | set(changed) | set(reloaded)):
        a, b, c = (rows.get(field_id) for rows in (first, changed, reloaded))
        comparable = all(isinstance(row, dict) and row.get("comparable") is True for row in (a, b, c))
        if comparable:
            def value(row: dict[str, Any]) -> tuple[Any, Any, Any]:
                return row.get("state"), row.get("kind"), row.get("value")
            changed_before = value(a) != value(b)
            matches_after = value(a) == value(c)
        else:
            changed_before = None
            matches_after = None
        fields.append({"id": field_id, "changed_before_reload": changed_before,
                       "matches_initial_after_reload": matches_after})
    result = dict(calibration)
    result["reset"] = {
        "scene_reload": {"entity_moved_before_reload": before_distance >= MIN_PRE_RESET_MOVEMENT,
            "entity_position_restored": restored, "distance_before_reload": before_distance,
            "distance_after_reload": after_distance, "position_tolerance": RESET_POSITION_TOLERANCE,
            "minimum_pre_reset_movement": MIN_PRE_RESET_MOVEMENT,
            "fields": fields, "full_state_restored": None,
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
