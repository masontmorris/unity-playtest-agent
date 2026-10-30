"""Manifest to choices: what a live run puts in front of S1.

`playtest.preflight.catalog.export` decides what can run. It keeps exactly the declarations whose
`execution_supported` is true (exact, release-verified execution evidence) and writes each one as the strict
`ActionDefinition` the session host registers under the declaration's own name. Every such declaration
becomes one choice, in the manifest's order (its actions, then its skills), named for that declaration: an
action is an `action` choice and a skill is a `skill` choice, and the request names the declaration. So a
chord, a stick or mouse value, or an ordered skill reaches the bridge as declared or is rejected by it;
nothing is reduced to its first input, ranked, reordered, rewritten or inferred (S1 ownership: S1 chooses
among the declarations as declared). The four core choices (wait, escalate, delegate, finish) come after.
A declaration with `execution_supported` false is not offered and is listed with the reason. The wait
choice names its duration (`duration_game_s`), which the turn loop honors in real time.

An action's requested hold is its choice's lease, read from the exported declaration. A skill carries no
lease: its steps and `max_s` belong to the declaration, and no host lease for an action replaces them.

Calibration measures one control at a time, so an expected effect exists only where calibration measured
exactly the declared thing: one input, at the value calibration used, held for a measured hold. Any other
declaration (a chord, a skill, a value calibration did not measure) has no expected effect: it is `None` and
its label says so. The effect of a single control is never reported as the effect of a combination.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from playtest.contracts.action_catalog import (
    MOUSE_DELTA_PIXEL_BOUND,
    ActionCatalog,
    ActionDefinition,
    ActionStep,
    InputBinding,
    is_pointer_impulse,
)
from playtest.contracts.calibration import HoldMeasurement
from playtest.contracts.investigation import Comparator
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource
from playtest.contracts.manifest import ManifestAction, ManifestCheck, ManifestSkill, PreflightManifest
from playtest.preflight.catalog import export

UNIT = "world units"  # the unit of the entity positions that calibration and the effect probe measure
WAIT_GAME_S = 0.5  # what the wait choice waits: game seconds with no input


@dataclass(frozen=True)
class ManifestChoices:
    """The choice list for a run and the data hosts need beside it.

    `catalog` is the exported declarations behind the supported choices: what the session host must serve.
    """

    choices: list[Choice]
    expected: dict[str, dict[str, Any] | None]
    leases: dict[str, float]
    not_offered: list[dict[str, str]] = field(default_factory=list)
    catalog: ActionCatalog = field(default_factory=ActionCatalog)


def core_choices() -> list[Choice]:
    """The choices every game has.

    Wait names the game seconds the loop waits (build bible 5.3); the rest keep the live smoke's wording.
    """
    return [
        Choice(id="wait", kind=ChoiceKind.wait, label=f"Wait {WAIT_GAME_S:g} s with no input",
               params={"duration_game_s": WAIT_GAME_S}, source=ChoiceSource.core),
        Choice(id="escalate", kind=ChoiceKind.escalate, label="Ask the frontier model for help",
               role="frontier", source=ChoiceSource.core),
        Choice(id="delegate", kind=ChoiceKind.delegate, label="Hand a bounded task to a helper",
               role="helper", source=ChoiceSource.core),
        Choice(id="finish", kind=ChoiceKind.finish, label="Finish", source=ChoiceSource.core),
    ]


def build_choices(manifest: PreflightManifest) -> ManifestChoices:
    core = core_choices()
    reserved = {choice.id for choice in core}
    catalog = export(manifest)
    choices: list[Choice] = []
    expected: dict[str, dict[str, Any] | None] = {}
    leases: dict[str, float] = {}
    not_offered: list[dict[str, str]] = []

    for action in manifest.actions:
        if not action.execution_supported:
            not_offered.append({"kind": "action", "name": action.name, "reason": _why_unsupported(action)})
            continue
        _reserve(action.name, reserved)
        declared = _declared(catalog, action.name, skill=False)
        hold = declared.default_lease_s
        assert hold is not None and declared.inputs is not None  # an exported action has both
        measured, note = _calibrated(action, declared)
        choices.append(Choice(
            id=action.name,
            kind=ChoiceKind.action,
            label=_action_label(action, declared, hold, measured, note),
            action=action.name,
            params={},
            preconditions=[_precondition(check) for check in action.preconditions],
            source=ChoiceSource.manifest,
        ))
        expected[action.name] = _expected(action, hold, measured)
        leases[action.name] = hold

    for skill in manifest.skills:
        if not skill.execution_supported:
            not_offered.append({"kind": "skill", "name": skill.name, "reason": _why_unsupported(skill)})
            continue
        _reserve(skill.name, reserved)
        declared = _declared(catalog, skill.name, skill=True)
        choices.append(Choice(
            id=skill.name,
            kind=ChoiceKind.skill,
            label=_skill_label(skill, declared),
            skill=skill.name,
            params={},
            source=ChoiceSource.manifest,
        ))
        expected[skill.name] = None  # calibration measured no ordered sequence; nothing stands in for one
        # No lease: the declaration's own steps and max_s bound a skill.
    return ManifestChoices(choices=[*choices, *core], expected=expected, leases=leases,
                           not_offered=not_offered, catalog=catalog)


def _reserve(name: str, reserved: set[str]) -> None:
    if name in reserved:
        raise ValueError(f"manifest declaration {name!r} collides with the core choice of the same id")


def _declared(catalog: ActionCatalog, name: str, *, skill: bool) -> ActionDefinition:
    """The exported declaration for a supported name. A supported declaration is never dropped or guessed."""
    kind = "skill" if skill else "action"
    declared = catalog.actions.get(name)
    if declared is None:
        raise ValueError(f"manifest {kind} {name!r} is execution_supported but the exported catalog has no declaration for it")
    if declared.is_skill != skill:
        raise ValueError(f"manifest {kind} {name!r} is exported as a {'skill' if declared.is_skill else 'action'}")
    return declared


def _why_unsupported(declaration: ManifestAction | ManifestSkill) -> str:
    if isinstance(declaration, ManifestSkill):
        if any(step.hold_s is None and step.tap_hold_s is None for step in declaration.steps):
            return "execution_supported is false: a step has no resolved positive hold, so there is no exact declaration to prove"
    elif declaration.hold_s is None and declaration.tap_hold_s is None:
        return "execution_supported is false: it has no resolved positive hold, so there is no exact declaration to prove"
    return "execution_supported is false: no exact release-verified execution evidence"


def _calibrated(action: ManifestAction, declared: ActionDefinition) -> tuple[HoldMeasurement | None, str | None]:
    """The calibration row measured for exactly this declaration, or why there is none.

    Calibration measures one control at a time, at one declared value, over tested holds. It speaks for an
    action only when the action is that one control at that value and its hold is a tested one.
    """
    if len(action.inputs) != 1:
        return None, "no calibrated effect is recorded for this combination of inputs"
    binding, summary = declared.inputs[0], action.calibrations[0]  # type: ignore[index]
    if (summary.declared_x, summary.declared_y) != (binding.x, binding.y):
        return None, ("no calibrated effect is recorded at this declared value "
                      f"(calibration measured {_pair(summary.declared_x, summary.declared_y)})")
    row = next((h for h in summary.holds if h.requested_hold_s == declared.default_lease_s), None)
    if row is None:
        return None, "no calibration at this hold"
    return row, None


def _num(value: float) -> str:
    """A declared number exactly as it is, without a trailing `.0`: a label never rounds what it states."""
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


def _pair(x: float | None, y: float | None) -> str:
    if x is None and y is None:
        return "no declared value"
    return ", ".join(f"{axis}={'unknown' if value is None else _num(value)}" for axis, value in (("x", x), ("y", y)))


def _action_label(action: ManifestAction, declared: ActionDefinition, hold: float,
                  measured: HoldMeasurement | None, note: str | None) -> str:
    head = f"{action.intent}: request {_request_text(declared.inputs or [], hold)}"
    if measured is None:
        return f"{head}; {note}."
    if measured.mean_magnitude is None:
        return f"{head}; calibrated effect unknown (no valid trial at this hold)."
    variance = f"{measured.sample_variance:.6f}" if measured.sample_variance is not None else "unknown"
    text = (f"{head}; calibrated net displacement {measured.mean_magnitude:.3f} {UNIT} "
            f"(sample variance {variance}, n={measured.valid_trials})")
    if measured.effective_in_all_repeats is False:
        text += "; net-displacement criterion was not met in every repeat"
    if measured.mean_view_turn is not None:
        view = measured.mean_view_turn
        text += (f"; calibrated view turn yaw {view.yaw_deg:.3f} degrees, pitch {view.pitch_deg:.3f} degrees "
                 f"(spread {view.spread_deg:.3f} degrees, n={view.trials})")
    return text + "."


def _skill_label(skill: ManifestSkill, declared: ActionDefinition) -> str:
    steps = declared.steps or []
    count = f"{len(steps)} step" + ("" if len(steps) == 1 else "s")
    ordered = "; ".join(f"step {index}: {_step_text(step)}" for index, step in enumerate(steps, start=1))
    return (f"{skill.purpose}: request this ordered sequence of {count}, at most {_num(declared.max_s)} s in total: "
            f"{ordered}; no calibrated effect is recorded for this sequence.")


def _step_text(step: ActionStep) -> str:
    inputs = _inputs_text(step.inputs)
    text = (f"apply {inputs} within a {_num(step.hold_s)}-second lease" if _has_impulse(step.inputs)
            else f"hold {inputs} for {_num(step.hold_s)} s")
    return text + (f", then wait {_num(step.wait_s)} s" if step.wait_s > 0 else "")


def _request_text(inputs: list[InputBinding], hold: float) -> str:
    text = _inputs_text(inputs)
    if _has_impulse(inputs):
        return f"{text} within a {_num(hold)}-second lease"
    return f"a {_num(hold)}-second hold of {text}"


def _has_impulse(inputs: list[InputBinding]) -> bool:
    return any(is_pointer_impulse(item.device, item.control) for item in inputs)


def _inputs_text(inputs: list[InputBinding]) -> str:
    return " + ".join(_input_text(item) for item in inputs)


def _input_text(item: InputBinding) -> str:
    """One declared input as written in the declaration: its device, control and values, none inferred."""
    name = item.control if item.device == "Keyboard" else f"{item.device} {item.control}"
    if item.declared_type == "Button":
        return name
    values = f"x={_num(item.x)}, y={_num(item.y)}"
    if is_pointer_impulse(item.device, item.control):
        pixels = _num(MOUSE_DELTA_PIXEL_BOUND)
        return f"{name} ({values}; one relative impulse of x*{pixels}, y*{pixels} pixels)"
    return f"{name} ({values})"


def _expected(action: ManifestAction, hold: float, measured: HoldMeasurement | None) -> dict[str, Any] | None:
    """The calibrated effect for exactly this declaration, or None when calibration measured none."""
    if measured is None or measured.mean_magnitude is None:
        return None
    return {
        "source": "calibration",
        "control": action.inputs[0].control_id,
        "requested_hold_s": hold,
        "net_displacement": measured.mean_magnitude,
        "sample_variance": measured.sample_variance,
        "valid_trials": measured.valid_trials,
        "effective_in_all_repeats": measured.effective_in_all_repeats,
        "unit": UNIT,
    }


def _precondition(check: ManifestCheck) -> str:
    if check.op in (Comparator.changed, Comparator.unchanged):
        return f"{check.field} {check.op.value}"
    return f"{check.field} {check.op.value} {json.dumps(check.value)}"
