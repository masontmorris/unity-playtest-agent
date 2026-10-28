"""Manifest to choices: what a live run puts in front of S1.

Each executable `ManifestAction` becomes one `action` choice, carrying the intent, the requested hold
and the calibrated effect for that hold exactly as the manifest records them. The four core choices
(wait, escalate, delegate, finish) come after. The only action not offered is one the current bridge
cannot execute; it is listed with the manifest field that says so, and nothing is ranked, reordered
or rewritten (S1 ownership: S1 chooses among the actions as declared).

The bridge action named on the choice is the one the manifest's offered controls carry for the
action's control. The PlayMode bench host registers one action per keyboard key under those names, so
no C# change is needed (see `docs/design/turn-loop.md`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from playtest.contracts.calibration import HoldMeasurement
from playtest.contracts.investigation import Comparator
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource
from playtest.contracts.manifest import ManifestAction, ManifestCheck, PreflightManifest

UNIT = "world units"  # the unit of the entity positions that calibration and the effect probe measure


@dataclass(frozen=True)
class ManifestChoices:
    """The choice list for a run and the data hosts need beside it."""

    choices: list[Choice]
    expected: dict[str, dict[str, Any] | None]
    leases: dict[str, float]
    not_offered: list[dict[str, str]] = field(default_factory=list)


def core_choices() -> list[Choice]:
    """The choices every game has. Their wording is the one the live smoke used."""
    return [
        Choice(id="wait", kind=ChoiceKind.wait, label="Wait", source=ChoiceSource.core),
        Choice(id="escalate", kind=ChoiceKind.escalate, label="Ask the frontier model for help",
               role="frontier", source=ChoiceSource.core),
        Choice(id="delegate", kind=ChoiceKind.delegate, label="Hand a bounded task to a helper",
               role="helper", source=ChoiceSource.core),
        Choice(id="finish", kind=ChoiceKind.finish, label="Finish", source=ChoiceSource.core),
    ]


def build_choices(manifest: PreflightManifest) -> ManifestChoices:
    core = core_choices()
    reserved = {choice.id for choice in core}
    bridge_actions = {row.get("id"): row.get("action") for row in manifest.selection.offered_controls}
    choices: list[Choice] = []
    expected: dict[str, dict[str, Any] | None] = {}
    leases: dict[str, float] = {}
    not_offered: list[dict[str, str]] = []

    for action in manifest.actions:
        if not action.execution_supported:
            not_offered.append({"kind": "action", "name": action.name, "reason": _why_unsupported(action)})
            continue
        control = action.inputs[0].control_id
        bridge_action = bridge_actions.get(control)
        if not isinstance(bridge_action, str) or not bridge_action:
            not_offered.append({"kind": "action", "name": action.name,
                                "reason": f"the manifest's offered controls name no bridge action for control {control!r}"})
            continue
        if action.name in reserved:
            raise ValueError(f"manifest action {action.name!r} collides with the core choice of the same id")
        hold = action.hold_s if action.hold_s is not None else action.tap_hold_s
        assert hold is not None  # execution_supported requires a hold
        measured = next((h for h in action.calibrations[0].holds if h.requested_hold_s == hold), None)
        choices.append(Choice(
            id=action.name,
            kind=ChoiceKind.action,
            label=_label(action, control, hold, measured),
            action=bridge_action,
            params={"hold_s": hold},
            preconditions=[_precondition(check) for check in action.preconditions],
            source=ChoiceSource.manifest,
        ))
        expected[action.name] = _expected(control, hold, measured)
        leases[action.name] = hold

    for skill in manifest.skills:
        not_offered.append({"kind": "skill", "name": skill.name,
                            "reason": "execution_supported is false: the current bridge cannot execute skills"})
    return ManifestChoices(choices=[*choices, *core], expected=expected, leases=leases, not_offered=not_offered)


def _why_unsupported(action: ManifestAction) -> str:
    if len(action.inputs) != 1:
        return f"execution_supported is false: it declares {len(action.inputs)} controls and the bridge executes one"
    if action.axis:
        return "execution_supported is false: it drives an axis and the bridge sends binary key input"
    return "execution_supported is false: it has no tested requested hold the bridge can run"


def _label(action: ManifestAction, control: str, hold: float, measured: HoldMeasurement | None) -> str:
    head = f"{action.intent}: request a {hold:g}-second hold of {control}"
    if measured is None:
        return f"{head}; no calibration at this hold."
    if measured.mean_magnitude is None:
        return f"{head}; calibrated effect unknown (no valid trial at this hold)."
    variance = f"{measured.sample_variance:.6f}" if measured.sample_variance is not None else "unknown"
    text = (f"{head}; calibrated net displacement {measured.mean_magnitude:.3f} {UNIT} "
            f"(sample variance {variance}, n={measured.valid_trials})")
    if measured.effective_in_all_repeats is False:
        text += "; not effective in every repeat"
    return text + "."


def _expected(control: str, hold: float, measured: HoldMeasurement | None) -> dict[str, Any] | None:
    """The calibrated effect for the requested hold, or None when calibration measured none."""
    if measured is None or measured.mean_magnitude is None:
        return None
    return {
        "source": "calibration",
        "control": control,
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
