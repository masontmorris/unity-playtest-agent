"""Refresh existing manifest measurements offline, preserving model selections and execution evidence."""
import argparse
import json
from pathlib import Path
from playtest.contracts.calibration import CalibrationRecord
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight.manifest import calibration_for, validate


def update(manifest: PreflightManifest, calibration: CalibrationRecord) -> PreflightManifest:
    if calibration.status != "measured_with_gaps" or calibration.entity_id != manifest.entity_id or calibration.entity != manifest.entity:
        raise ValueError("a completed calibration of the exact manifest entity is required")
    rows = [r.model_dump(mode="json") for r in calibration.action_summaries]
    data = manifest.model_dump(mode="json")
    for action in data["actions"]:
        action["calibrations"] = [calibration_for(rows, i["control_id"], i["x"], i["y"]) for i in action["inputs"]]
        if len(action["inputs"]) == 1:
            i, row = action["inputs"][0], action["calibrations"][0]
            hold = action["hold_s"] if action["hold_s"] is not None else action["tap_hold_s"]
            matched = [h for h in row["holds"] if h["requested_hold_s"] == hold] if (i["x"], i["y"]) == (row["declared_x"], row["declared_y"]) else []
            action["hold_effect"] = matched[0]["effective_in_all_repeats"] if len(matched) == 1 else None
    for skill in data["skills"]:
        inputs = [i for step in skill["steps"] for i in step["inputs"]]
        skill["calibrations"] = [calibration_for(rows, old["control"],
            next(i for i in inputs if i["control_id"] == old["control"])["x"],
            next(i for i in inputs if i["control_id"] == old["control"])["y"]) for old in skill["calibrations"]]
    result = PreflightManifest.model_validate(data)
    problems = validate(result)
    if problems:
        raise ValueError("updated manifest is invalid: " + "; ".join(problems))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = update(PreflightManifest.model_validate_json(args.manifest.read_bytes()),
                    CalibrationRecord.model_validate_json(args.calibration.read_bytes()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result.model_dump(mode="json"), stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
