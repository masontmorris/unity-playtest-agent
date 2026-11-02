"""Opt-in view pointing: geometry bound to measured FOV and measured input compositions."""

import math
from dataclasses import dataclass
from itertools import product

from playtest.contracts.action_catalog import ActionDefinition, ActionStep
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource
from .turn_sizes import measured_turns


def measured_fov(calibration):
    """Clock checks retain full observations; turn summaries retain only yaw/pitch."""
    clock = (calibration or {}).get("clock") or {}
    values = []
    for phase in ("pause", "step", "resume"):
        reading = clock.get(phase, {}) or {}
        observation = reading.get("observation", reading)
        value = _camera_fov(observation.get("public") or {})
        if (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and 0 < value < 180):
            values.append(float(value))
    if not values:
        raise ValueError("--point-turns requires a measured camera field_of_view in calibration clock observations")
    if max(values) - min(values) > .01:
        raise ValueError("--point-turns requires a consistent measured camera field_of_view")
    return values[0]


def strip_angles(vfov, capture_size=(640, 480)):
    width, height = capture_size
    if width <= 0 or height <= 0 or not 0 < vfov < 180:
        raise ValueError("point-turns requires positive capture dimensions and 0 < field_of_view < 180")
    horizontal_tangent = math.tan(math.radians(vfov) / 2) * width / height
    return {k: math.degrees(math.atan((2 * (k - .5) / 9 - 1) * horizontal_tangent))
            for k in range(1, 10) if k != 5}


def compose(target, measured):
    """Exhaustive bounded search; row order breaks remaining ties deterministically."""
    candidates = (steps for count in range(1, 4) for steps in product(range(len(measured)), repeat=count))
    return min(candidates, key=lambda seq: (abs(target - math.fsum(measured[i][2] for i in seq)), len(seq), seq))


@dataclass(frozen=True)
class PointTurn:
    choice: Choice
    definition: ActionDefinition
    target: float
    sequence: tuple[str, ...]
    total: float


def pointing_turns(manifest, calibration, capture_size=(640, 480)):
    fov = measured_fov(calibration)
    measured = list(measured_turns(manifest, calibration, include_yaw=True))
    if not measured:
        raise ValueError("--point-turns requires measured turn inputs")
    targets = [(f"point.strip{k}", f"face view strip {k} of 9 (strips counted left to right across the image)", angle,
                compose(angle, measured)) for k, angle in strip_angles(fov, capture_size).items()]
    for sign, direction in ((-1, "left"), (1, "right")):
        candidates = [i for i, row in enumerate(measured) if 0 < sign * row[2] <= 180]
        if candidates:
            i = max(candidates, key=lambda i: abs(measured[i][2]))
            targets.append((f"point.search_{direction}", "large turn", measured[i][2], (i,)))
    result = []
    for ident, label, target, sequence in targets:
        steps = [ActionStep(inputs=measured[i][1].inputs, hold_s=measured[i][1].default_lease_s,
                            wait_s=.1 if n < len(sequence) - 1 else 0)
                 for n, i in enumerate(sequence)]
        total = math.fsum(measured[i][2] for i in sequence)
        direction = "left" if target < 0 else "right"
        choice = Choice(id=ident, kind=ChoiceKind.skill, skill=ident.replace(".", "_"),
                        label=f"{label}: turn {direction} {abs(target):.1f}° (measured inputs total {abs(total):.1f}°)",
                        params={}, source=ChoiceSource.manifest)
        # The bridge deadline also covers scheduling and verified releases. This margin
        # changes no input hold or wait; an exact-duration deadline races the last release.
        definition = ActionDefinition(backend="input_system", steps=steps,
                                      max_s=sum(s.hold_s + s.wait_s for s in steps) + 1.0)
        result.append(PointTurn(choice, definition, target, tuple(measured[i][0].id for i in sequence), total))
    return result


def _camera_fov(public):
    camera = public.get("camera")
    return camera.get("field_of_view") if isinstance(camera, dict) else None


def gate_choices(choices, observation, fov):
    value = _camera_fov(observation.public)
    matches = (not isinstance(value, bool) and isinstance(value, (int, float))
               and math.isfinite(value) and abs(value - fov) <= .01 + 1e-12)
    return [c for c in choices if matches or not (c.id.startswith("point.strip") or (c.id.startswith("move.strip") and c.id != "move.strip5"))]
