"""Export the executable subset of a preflight manifest as a strict action catalog.

Only declarations with exact release-verified execution evidence are exported. An unproven
declaration stays in the manifest with `execution_supported: false` for a later named probe;
it is never offered as an executable choice by this exporter.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playtest.contracts.action_catalog import ActionCatalog, ActionDefinition, ActionStep, InputBinding
from playtest.contracts.manifest import ManifestAction, ManifestSkill, PreflightManifest
from playtest.preflight import manifest as manifest_module


def _binding(item) -> InputBinding:
    return InputBinding(device=item.device, control=item.control, x=item.x, y=item.y)


def _action(action: ManifestAction) -> ActionDefinition:
    hold = action.hold_s if action.hold_s is not None else action.tap_hold_s
    if hold is None:
        raise ValueError(f"action {action.name!r} has no resolved hold")
    return ActionDefinition(backend="input_system", inputs=[_binding(item) for item in action.inputs],
                            default_lease_s=hold)


def _skill(skill: ManifestSkill) -> ActionDefinition:
    steps = []
    for step in skill.steps:
        hold = step.hold_s if step.hold_s is not None else step.tap_hold_s
        if hold is None:
            raise ValueError(f"skill {skill.name!r} has an unresolved step hold")
        steps.append(ActionStep(inputs=[_binding(item) for item in step.inputs],
                                hold_s=hold, wait_s=step.wait_s))
    return ActionDefinition(backend="input_system", steps=steps, max_s=skill.max_s)


def export(value: PreflightManifest, *, turn_sizes: bool = False, point_turns: bool = False, point_moves: bool = False, capture_size=(640, 480), calibration: dict | None = None) -> ActionCatalog:
    """Build the catalog from supported declarations only; no order or content is invented."""
    if sum((turn_sizes, point_turns, point_moves)) > 1:
        raise ValueError("--point-moves, --point-turns and --turn-sizes are mutually exclusive")
    actions: dict[str, ActionDefinition] = {}
    for action in value.actions:
        if action.execution_supported:
            actions[action.name] = _action(action)
    for skill in value.skills:
        if skill.execution_supported:
            actions[skill.name] = _skill(skill)
    if turn_sizes:
        from playtest.runner.turn_sizes import measured_turns
        for choice, definition in measured_turns(value, calibration):
            if choice.action in actions:
                raise ValueError(f"turn choice id collision: {choice.id}")
            actions[choice.action] = definition
    if point_turns or point_moves:
        from playtest.runner.point_turns import pointing_turns
        from playtest.runner.point_moves import pointing_moves
        derive = pointing_moves if point_moves else pointing_turns
        for row in derive(value, calibration, capture_size):
            if row.choice.skill in actions:
                raise ValueError(f"point choice id collision: {row.choice.id}")
            actions[row.choice.skill] = row.definition
    return ActionCatalog(actions=actions)


def serialize(catalog: ActionCatalog) -> dict:
    """Write only the fields of the selected mode; x and y stay present as null on buttons."""
    actions = {}
    for name, definition in catalog.actions.items():
        row = definition.model_dump(mode="json")
        if definition.is_skill:
            row.pop("inputs", None)
            row.pop("default_lease_s", None)
        else:
            row.pop("steps", None)
            row.pop("max_s", None)
        actions[name] = row
    return {"actions": actions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new catalog JSON file")
    parser.add_argument("--turn-sizes", action="store_true")
    parser.add_argument("--point-turns", action="store_true")
    parser.add_argument("--point-moves", action="store_true")
    parser.add_argument("--capture-width", type=int, default=640)
    parser.add_argument("--capture-height", type=int, default=480)
    parser.add_argument("--calibration", type=Path)
    args = parser.parse_args(argv)
    if sum((args.turn_sizes, args.point_turns, args.point_moves)) > 1:
        print("catalog export failed: --point-moves, --point-turns and --turn-sizes are mutually exclusive", file=sys.stderr)
        return 1
    output = args.output.resolve()
    if output.exists():
        parser.error("--output already exists")
    try:
        value = PreflightManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"), strict=True)
        problems = manifest_module.validate(value)
        if problems:
            raise ValueError("the manifest is invalid: " + "; ".join(problems))
        calibration = json.loads(args.calibration.read_text(encoding="utf-8")) if args.calibration else None
        catalog = export(value, turn_sizes=args.turn_sizes, point_turns=args.point_turns, point_moves=args.point_moves,
                         capture_size=(args.capture_width, args.capture_height), calibration=calibration)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(serialize(catalog), stream, indent=2, allow_nan=False, sort_keys=True)
            stream.write("\n")
    except (OSError, ValueError, TypeError) as exc:
        print(f"catalog export failed: {exc}", file=sys.stderr)
        return 1
    print(f"catalog saved to {output}: {len(catalog.actions)} executable declarations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
