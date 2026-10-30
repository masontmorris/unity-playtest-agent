"""Local preflight manifest: selected choices and their evidence, not a wire message."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .action_catalog import (
    ActionDefinition,
    ActionStep,
    Device,
    InputBinding,
    evidence_problems,
)
from .bridge import InputExecutionEvidence, InspectionCap
from .calibration import ActionCalibration
from .common import Strict
from .investigation import Comparator


NAME = r"^[a-z][a-z0-9_]{0,39}$"


class ManifestCheck(Strict):
    field: str = Field(pattern=NAME)
    op: Comparator
    value: float | str | bool | None


class ManifestReader(Strict):
    alias: str = Field(pattern=NAME)
    selection_id: str = Field(min_length=1, description="The field token selected by the model")
    field_id: str = Field(min_length=1)
    why: str = Field(min_length=1)
    declared_type: str | None
    kind: Literal["bool", "number", "string", "vector", "other"] | None
    descriptor: dict[str, Any]
    freshness: None
    freshness_reason: str = Field(min_length=1)
    freshness_evidence: dict[str, Any] | None


class ManifestInput(Strict):
    control_id: str = Field(min_length=1, description="Stable canonical control id offered by preflight")
    device: Device
    control: str = Field(min_length=1, description="Physical root control, without a device prefix")
    x: float | None = Field(ge=-1, le=1, allow_inf_nan=False)
    y: float | None = Field(ge=-1, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def _declared_value_shape(self) -> "ManifestInput":
        InputBinding(device=self.device, control=self.control, x=self.x, y=self.y)
        return self


def _binding(input_value: ManifestInput) -> InputBinding:
    return InputBinding(device=input_value.device, control=input_value.control,
                        x=input_value.x, y=input_value.y)


def _calibrated_bindings(inputs: list[ManifestInput], calibrations: list[ActionCalibration]) -> None:
    by_id = {item.control: item for item in calibrations}
    for item in inputs:
        calibration = by_id[item.control_id]
        if (item.device != (calibration.device or "Keyboard")
                or item.control != (calibration.control_name or calibration.control)):
            raise ValueError("physical input must match its calibrated control binding")


def _step_hold(step: "ManifestSkillStep") -> float | None:
    return step.hold_s if step.hold_s is not None else step.tap_hold_s


def _action_hold(action: "ManifestAction") -> float | None:
    return action.hold_s if action.hold_s is not None else action.tap_hold_s


def _action_definition(action: "ManifestAction") -> ActionDefinition | None:
    """The exact catalog definition this manifest action declares, or null if unresolved."""
    hold = _action_hold(action)
    if hold is None:
        return None
    return ActionDefinition(backend="input_system",
                            inputs=[_binding(item) for item in action.inputs],
                            default_lease_s=hold)


def _skill_definition(skill: "ManifestSkill") -> ActionDefinition | None:
    """The exact catalog definition this manifest skill declares, or null if unresolved."""
    if any(_step_hold(step) is None for step in skill.steps):
        return None
    return ActionDefinition(backend="input_system", steps=[
        ActionStep(inputs=[_binding(item) for item in step.inputs],
                   hold_s=_step_hold(step), wait_s=step.wait_s) for step in skill.steps],
        max_s=skill.max_s)


def _support(definition: ActionDefinition | None, evidence: InputExecutionEvidence | None) -> bool:
    if evidence is None or definition is None:
        return False
    return not evidence_problems(evidence, definition)


class ManifestAction(Strict):
    name: str = Field(pattern=NAME)
    intent: str = Field(min_length=1)
    inputs: list[ManifestInput] = Field(min_length=1, max_length=3)
    hold_s: float | None = Field(gt=0, le=3600, allow_inf_nan=False)
    tap_hold_s: float | None = Field(gt=0, le=3600, allow_inf_nan=False, description="Tested requested hold, not observed applied duration")
    preconditions: list[ManifestCheck] = Field(max_length=3)
    calibrations: list[ActionCalibration] = Field(min_length=1, max_length=3)
    axis: bool
    max_hold_s: None = Field(description="No per-action override limit has been measured")
    hold_effect: bool | None = Field(description="Entity-displacement response in repeated trials for the exact input value and hold, or null if not established")
    execution_evidence: InputExecutionEvidence | None = Field(
        None, description="Release-verified receipts for this exact declaration, or null when unproven")
    execution_supported: bool = Field(
        description="True only when execution_evidence completed with verified release for this exact declaration")

    @model_validator(mode="after")
    def _bound_to_calibration(self) -> "ManifestAction":
        ids = [item.control_id for item in self.inputs]
        if len(ids) != len(set(ids)) or [c.control for c in self.calibrations] != ids:
            raise ValueError("action calibrations must match the selected controls in order")
        _calibrated_bindings(self.inputs, self.calibrations)
        pairs = [(item.device, item.control) for item in self.inputs]
        if len(pairs) != len(set(pairs)):
            raise ValueError("an action cannot repeat the same physical input")
        if self.hold_s is None and self.tap_hold_s is not None and (
            len(ids) != 1 or self.tap_hold_s != self.calibrations[0].lowest_tested_effective_requested_hold_s
        ):
            raise ValueError("a tap hold must be the tested requested hold for its one control")
        if self.hold_s is not None and self.tap_hold_s is not None:
            raise ValueError("an explicit hold does not use a tap hold")
        requested = self.hold_s if self.hold_s is not None else self.tap_hold_s
        same_value = len(ids) == 1 and (self.inputs[0].x, self.inputs[0].y) == (
            self.calibrations[0].declared_x, self.calibrations[0].declared_y)
        matching = [item.effective_in_all_repeats for item in self.calibrations[0].holds
                    if requested is not None and item.requested_hold_s == requested] if same_value else []
        if self.tap_hold_s is not None and (len(matching) != 1 or matching[0] is not True):
            raise ValueError("a tap hold requires matching effective repeated trials")
        expected = matching[0] if len(matching) == 1 else None
        if self.hold_effect != expected:
            raise ValueError("hold effect must reflect the matching calibration trials")
        if requested is not None and requested > 3600:
            raise ValueError("an action hold cannot exceed 3600 seconds")
        definition = _action_definition(self)
        if self.execution_evidence is not None and definition is None:
            raise ValueError("execution evidence needs a resolved positive hold")
        supported = _support(definition, self.execution_evidence)
        if self.execution_supported != supported:
            detail = "; ".join(evidence_problems(self.execution_evidence, definition)) if (
                self.execution_evidence is not None and definition is not None) else ""
            raise ValueError("execution support must reflect exact release-verified execution evidence"
                             + (f": {detail}" if detail else ""))
        return self


class ManifestSkillStep(Strict):
    inputs: list[ManifestInput] = Field(min_length=1, max_length=3)
    hold_s: float | None = Field(gt=0, le=3600, allow_inf_nan=False)
    tap_hold_s: float | None = Field(gt=0, le=3600, allow_inf_nan=False,
                                     description="Tested requested hold for a one-input step, or null")
    wait_s: float = Field(ge=0, le=3600, allow_inf_nan=False)

    @model_validator(mode="after")
    def _one_hold(self) -> "ManifestSkillStep":
        pairs = [(item.device, item.control) for item in self.inputs]
        if len(pairs) != len(set(pairs)):
            raise ValueError("a skill step cannot repeat the same physical input")
        if self.hold_s is not None and self.tap_hold_s is not None:
            raise ValueError("a skill step with an explicit hold does not use a tap hold")
        if self.tap_hold_s is not None and len(self.inputs) != 1:
            raise ValueError("a skill step tap hold needs exactly one input")
        return self


class ManifestSkill(Strict):
    name: str = Field(pattern=NAME)
    purpose: str = Field(min_length=1)
    steps: list[ManifestSkillStep] = Field(min_length=1, max_length=8)
    max_s: float = Field(gt=0, le=3600, allow_inf_nan=False)
    calibrations: list[ActionCalibration]
    execution_evidence: InputExecutionEvidence | None = Field(
        None, description="Release-verified ordered receipts for this exact skill, or null when unproven")
    execution_supported: bool

    @model_validator(mode="after")
    def _controls(self) -> "ManifestSkill":
        ids = {item.control_id for step in self.steps for item in step.inputs}
        supplied = [c.control for c in self.calibrations]
        if len(supplied) != len(set(supplied)) or set(supplied) != ids:
            raise ValueError("skill calibrations must cover its selected controls")
        for step in self.steps:
            _calibrated_bindings(step.inputs, self.calibrations)
            controls = [item.control_id for item in step.inputs]
            if len(controls) != len(set(controls)):
                raise ValueError("a skill step cannot repeat a control")
            if step.tap_hold_s is not None:
                summary = next((c for c in self.calibrations if c.control == step.inputs[0].control_id), None)
                if summary is None or summary.lowest_tested_effective_requested_hold_s != step.tap_hold_s:
                    raise ValueError("a skill step tap hold must be the tested requested hold for its control")
                matching = [h for h in summary.holds if h.requested_hold_s == step.tap_hold_s]
                if len(matching) != 1 or matching[0].effective_in_all_repeats is not True:
                    raise ValueError("a skill step tap hold requires matching effective repeated trials")
        known_duration = sum(step.wait_s + (_step_hold(step) or 0) for step in self.steps)
        if known_duration > self.max_s:
            raise ValueError("known skill step durations exceed max_s")
        if any(_step_hold(step) is None for step in self.steps):
            if self.execution_evidence is not None:
                raise ValueError("execution evidence needs every skill step hold resolved")
        definition = _skill_definition(self)
        supported = _support(definition, self.execution_evidence)
        if self.execution_supported != supported:
            detail = "; ".join(evidence_problems(self.execution_evidence, definition)) if (
                self.execution_evidence is not None and definition is not None) else ""
            raise ValueError("skill execution support must reflect exact release-verified execution evidence"
                             + (f": {detail}" if detail else ""))
        return self


class ManifestProgress(Strict):
    field: str = Field(pattern=NAME)
    direction: Literal["up", "down"]
    why: str = Field(min_length=1)


class ManifestInvariant(Strict):
    kind: Literal["range", "monotonic", "implies"]
    name: str = Field(pattern=NAME)
    source: str = Field(min_length=1, description="Model-cited source; not independently verified")
    catches: str = Field(min_length=1, description="Model-proposed failure meaning; not a verified finding")
    field: str | None = Field(pattern=NAME)
    direction: Literal["non_decreasing", "non_increasing"] | None
    min: float | None = Field(allow_inf_nan=False)
    max: float | None = Field(allow_inf_nan=False)
    when: ManifestCheck | None
    then: ManifestCheck | None
    unless: ManifestCheck | None


class ManifestGaps(Strict):
    scope: Literal["model_report_relative_to_offered_and_selected_evidence"]
    unobservable: list[str] = Field(max_length=8, description="Model-reported gaps relative to the offered and selected evidence")
    uncontrollable: list[str] = Field(max_length=8, description="Model-reported gaps; harness limitations are recorded separately")
    unjudgeable: list[str] = Field(max_length=8, description="Model-reported gaps relative to the selected readers")
    coverage: list[str]


class ManifestImage(Strict):
    mime: Literal["image/png", "image/jpeg"]
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: str = Field(min_length=1)
    same_session: bool | None
    capture_game_s: float | None = Field(ge=0, allow_inf_nan=False)


class SelectionCoverage(Strict):
    offered_fields: list[dict[str, Any]]
    unselected_field_ids: list[str]
    unoffered_fields: list[dict[str, Any]]
    offered_controls: list[dict[str, Any]]
    unoffered_controls: list[dict[str, Any]]
    unsupported_bindings: list[dict[str, Any]]
    reader_cap: Literal[16]
    reader_cap_reached: bool
    action_cap: Literal[14]
    action_cap_reached: bool
    skill_cap: Literal[6]
    skill_cap_reached: bool
    gap_caps_reached: dict[str, bool]


class PreflightManifest(Strict):
    schema_version: Literal[2]
    packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entity_id: str = Field(min_length=1)
    entity: dict[str, Any]
    inventory_caps: dict[str, InspectionCap]
    discovery_caps: dict[str, InspectionCap]
    image: ManifestImage | None
    readers: list[ManifestReader] = Field(max_length=16)
    actions: list[ManifestAction] = Field(max_length=14)
    skills: list[ManifestSkill] = Field(max_length=6)
    progress: list[ManifestProgress] = Field(max_length=6)
    invariants: list[ManifestInvariant] = Field(max_length=12)
    gaps: ManifestGaps
    selection: SelectionCoverage
    reset_bindings: None
    reset_bindings_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _references(self) -> "PreflightManifest":
        if not self.readers and not self.gaps.unobservable:
            raise ValueError("an empty reader selection requires an unobservable gap")
        if not self.actions and not self.gaps.uncontrollable:
            raise ValueError("an empty action selection requires an uncontrollable gap")
        aliases = [item.alias for item in self.readers]
        selections = [item.selection_id for item in self.readers]
        fields = [item.field_id for item in self.readers]
        names = [item.name for item in self.actions] + [item.name for item in self.skills]
        if (len(aliases) != len(set(aliases)) or len(fields) != len(set(fields)) or
                len(selections) != len(set(selections))):
            raise ValueError("reader aliases, selection ids and field ids must be unique")
        if len(names) != len(set(names)):
            raise ValueError("action and skill names must be unique")
        offered = [f.get("id") for f in self.selection.offered_fields]
        if (any(not isinstance(x, str) or not x for x in offered) or
                len(offered) != len(set(offered)) or not set(selections) <= set(offered)):
            raise ValueError("selected fields must be unique members of the offered fields")
        if self.selection.unselected_field_ids != [f for f in offered if f not in set(selections)]:
            raise ValueError("unselected fields must match the offered and selected fields")
        if (self.selection.reader_cap_reached != (len(self.readers) == self.selection.reader_cap) or
                self.selection.action_cap_reached != (len(self.actions) == self.selection.action_cap) or
                self.selection.skill_cap_reached != (len(self.skills) == self.selection.skill_cap)):
            raise ValueError("selection cap facts must match the selected counts")
        controls = [c.get("id") for c in self.selection.offered_controls]
        if any(not isinstance(c, str) or not c for c in controls) or len(controls) != len(set(controls)):
            raise ValueError("offered control ids must be unique strings")
        offered_rows = {row["id"]: row for row in self.selection.offered_controls}
        declared_inputs = [item for action in self.actions for item in action.inputs]
        declared_inputs += [item for skill in self.skills for step in skill.steps for item in step.inputs]
        selected_controls = [item.control_id for item in declared_inputs]
        if not set(selected_controls) <= set(controls):
            raise ValueError("selected controls must be offered")
        for item in declared_inputs:
            row = offered_rows[item.control_id]
            if row.get("device") != item.device or row.get("control") != item.control:
                raise ValueError("selected control descriptors must match the offered physical controls")
        gap_flags = {"unobservable": len(self.gaps.unobservable) == 8,
                     "uncontrollable": len(self.gaps.uncontrollable) == 8,
                     "unjudgeable": len(self.gaps.unjudgeable) == 8}
        if self.selection.gap_caps_reached != gap_flags:
            raise ValueError("gap cap facts must match the selected counts")
        known = set(aliases)
        references = [check.field for action in self.actions for check in action.preconditions]
        references += [item.field for item in self.progress]
        for invariant in self.invariants:
            if invariant.field is not None:
                references.append(invariant.field)
            references.extend(check.field for check in (invariant.when, invariant.then, invariant.unless)
                              if check is not None)
        if not set(references) <= known:
            raise ValueError("checks and progress must reference selected reader aliases")
        return self
