"""Opt-in pointing skills: measured view turns followed by a measured forward input."""

import math
from dataclasses import dataclass

from playtest.contracts.action_catalog import ActionDefinition, ActionStep
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource
from playtest.preflight.catalog import _action
from .point_turns import PointTurn, pointing_turns


@dataclass(frozen=True)
class ForwardStep:
    action: str
    definition: ActionDefinition
    displacement: float
    direction_deg: float
    spread: float
    trials: int


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def forward_step(manifest, calibration):
    """Compare repeated displacements in each trial's measured view frame, never world averages.

    Spread is the sample vector standard deviation in that frame (world units).
    At least two valid, fully measured repeats are required to measure spread. Each
    repeat must move within 20 degrees of both its before and after view yaw.
    effect_delta is preferred; raw_delta is used only when effect_delta is absent.
    """
    if not calibration or calibration.get("entity_id") != manifest.entity_id:
        raise ValueError("--point-moves requires calibration for the manifest entity")
    candidates = []
    for action in manifest.actions:
        if not action.execution_supported or len(action.inputs) != 1:
            continue
        definition = _action(action)
        binding = action.inputs[0]
        trials = [t for t in calibration.get("trials", [])
                  if t.get("valid") is True and t.get("control") == binding.control_id
                  and t.get("requested_hold_s") == definition.default_lease_s
                  and (t.get("declared_x"), t.get("declared_y")) == (binding.x, binding.y)]
        vectors = []
        for trial in trials:
            delta = trial.get("effect_delta", trial.get("raw_delta"))
            yaws = [(trial.get(key) or {}).get("yaw_deg") for key in ("view_before", "view_after")]
            if (not isinstance(delta, (list, tuple)) or len(delta) != 3
                    or not all(_finite(v) for v in [*delta, *yaws])):
                break
            magnitude = math.hypot(*delta)
            if magnitude <= 0:
                break
            local = []
            for yaw in yaws:
                radians = math.radians(yaw)
                x, y, z = delta
                local.append((x * math.cos(radians) - z * math.sin(radians), y,
                              x * math.sin(radians) + z * math.cos(radians)))
            if any(v[2] / magnitude < math.cos(math.radians(20)) - 1e-12 for v in local):
                break
            vectors.append(local[0])
        else:
            if len(vectors) < 2:
                continue
            mean = tuple(math.fsum(v[i] for v in vectors) / len(vectors) for i in range(3))
            spread = math.sqrt(math.fsum(math.dist(v, mean) ** 2 for v in vectors) / (len(vectors) - 1))
            candidates.append(ForwardStep(action.name, definition,
                math.fsum(math.hypot(*v) for v in vectors) / len(vectors),
                math.degrees(math.atan2(mean[0], mean[2])), spread, len(vectors)))
    if not candidates:
        raise ValueError("--point-moves requires a measured forward step at a declared hold: "
                         "at least two valid displacement/view-yaw trials within ±20° of the view")
    # Manifest order breaks a remaining exact tie. Names never participate.
    return min(candidates, key=lambda row: (row.spread, row.definition.default_lease_s))


def pointing_moves(manifest, calibration, capture_size=(640, 480)):
    step = forward_step(manifest, calibration)
    try:
        turns = pointing_turns(manifest, calibration, capture_size)
    except ValueError as exc:
        raise ValueError(str(exc).replace("--point-turns", "--point-moves")) from exc
    by_id = {row.choice.id: row for row in turns}
    result = []
    for k in range(1, 10):
        turn = by_id.get(f"point.strip{k}")
        steps = [s.model_copy(update={"wait_s": .1}) for s in turn.definition.steps] if turn else []
        steps.append(ActionStep(inputs=step.definition.inputs, hold_s=step.definition.default_lease_s, wait_s=0))
        ident = f"move.strip{k}"
        prefix = f"go toward view strip {k} of 9"
        label = (f"{prefix} (strips counted left to right across the image): "
                 + turn.choice.label.split(": ", 1)[1] + ", then " if turn else
                 f"{prefix} (straight ahead): ")
        choice = Choice(id=ident, kind=ChoiceKind.skill, skill=ident.replace(".", "_"),
                        label=label + f"step forward {step.displacement:.1f} units",
                        params={}, source=ChoiceSource.manifest)
        result.append(PointTurn(choice, ActionDefinition(backend="input_system", steps=steps,
            max_s=sum(s.hold_s + s.wait_s for s in steps) + 1),
            turn.target if turn else 0, (*turn.sequence, step.action) if turn else (step.action,),
            turn.total if turn else 0))
    return [*result, *(row for row in turns if row.choice.id.startswith("point.search_"))]
