"""Exported strict catalog for programmed input declarations.

One named act is either one held action (1-3 simultaneous device inputs) or one bounded skill
(1-8 ordered hold/wait steps). The device and physical control are explicit; keyboard and
device buttons carry null x and y, vector controls carry both values in [-1, 1], and a
one-dimensional axis uses x with y = 0. Mouse delta is one relative impulse per apply:
x and y are normalized displacement and the runtime applies x * 64, y * 64 pixels.

The catalog is a local declaration contract, not a selection policy. S1 still chooses the
name; code executes exactly that declaration or rejects it. Unsupported control paths are
rejected here rather than being reinterpreted as another device or control.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from pydantic import Field, model_validator

from .common import Strict


NAME = r"^[a-z][a-z0-9_]{0,39}$"
Device = Literal["Keyboard", "Mouse", "Gamepad"]

MAX_INPUTS = 3
MAX_STEPS = 8
MAX_HOLD_S = 3600.0
# One normalized unit of mouse delta is this many pixels per component; one relative impulse
# is applied per act, never repeated each update.
MOUSE_DELTA_PIXEL_BOUND = 64.0

# Stable canonical control names. Keyboard names are the physical key tokens already used by
# discovery; mouse and gamepad names are Unity root control names without a device prefix.
MOUSE_DELTA = "delta"
MOUSE_BUTTONS = frozenset({"leftButton", "rightButton", "middleButton"})
GAMEPAD_VECTORS = frozenset({"leftStick", "rightStick", "dpad"})
GAMEPAD_ONE_DIMENSIONAL = frozenset({"leftTrigger", "rightTrigger"})
GAMEPAD_BUTTONS = frozenset({
    "buttonSouth", "buttonEast", "buttonWest", "buttonNorth",
    "leftShoulder", "rightShoulder", "leftStickPress", "rightStickPress",
    "start", "select",
})

CONTROL_TYPES: dict[tuple[str, str], str] = {
    ("Mouse", MOUSE_DELTA): "Delta",
    **{("Mouse", name): "Button" for name in MOUSE_BUTTONS},
    **{("Gamepad", name): "Stick" for name in ("leftStick", "rightStick")},
    ("Gamepad", "dpad"): "Dpad",
    **{("Gamepad", name): "Axis" for name in GAMEPAD_ONE_DIMENSIONAL},
    **{("Gamepad", name): "Button" for name in GAMEPAD_BUTTONS},
}

# A mouse impulse is measured by its reported delta, not by a configured hold. Every held
# keyboard or gamepad input needs a measured duration. This allowance compares measured
# application with the declared hold; it is not a configured value copied into the record.
HOLD_TOLERANCE_S = 0.05
HOLD_TOLERANCE_FRACTION = 0.25

# Declared allowances for measured timing. They absorb timestamp granularity, never a
# changed declaration: the declaration itself must match exactly before any of these apply.
# A chord's inputs must acknowledge and release within this window of each other.
CHORD_WINDOW_TOLERANCE_S = 0.01
# A measured release-to-next-apply wait and the measured total may fall this far short of the
# declared wait because the two wall timestamps are sampled independently.
WAIT_TOLERANCE_S = 0.01
# Float transport only: the measured pixel delta must equal x * 64 / y * 64. This is far
# below one pixel and is never used to clamp, repair or infer a declared direction.
MOUSE_DELTA_TOLERANCE_PX = 0.001


def control_type(device: str, control: str) -> str | None:
    """The declared control type, or None for an unsupported device/control pair."""
    if device == "Keyboard":
        return "Button" if _keyboard_control(control) else None
    return CONTROL_TYPES.get((device, control))


def _keyboard_control(control: str) -> bool:
    return (isinstance(control, str) and 1 <= len(control) <= 40
            and control.replace("_", "").isalnum() and control.lower() == control)


def is_pointer_impulse(device: str, control: str) -> bool:
    return device == "Mouse" and control == MOUSE_DELTA


def hold_tolerance(declared: float) -> float:
    return max(HOLD_TOLERANCE_S, abs(declared) * HOLD_TOLERANCE_FRACTION)


class InputBinding(Strict):
    """One physical input and its declared normalized value."""

    device: Device
    control: str = Field(min_length=1, max_length=40)
    x: float | None = Field(None, ge=-1, le=1, allow_inf_nan=False,
                            description="Normalized value in [-1, 1]; null for a button")
    y: float | None = Field(None, ge=-1, le=1, allow_inf_nan=False,
                            description="Second normalized value; 0 for a one-dimensional axis; null for a button")

    @model_validator(mode="after")
    def _supported_control(self) -> "InputBinding":
        kind = control_type(self.device, self.control)
        if kind is None:
            raise ValueError(f"{self.device} control {self.control!r} is not a supported root control")
        if kind == "Button":
            if self.x is not None or self.y is not None:
                raise ValueError(f"{self.device} {self.control!r} is a button; x and y must be null")
            return self
        if self.device == "Mouse":
            if self.x is None or self.y is None:
                raise ValueError(f"{self.device} {self.control!r} is a relative vector; x and y are required")
            return self
        if self.device == "Gamepad" and self.control in GAMEPAD_ONE_DIMENSIONAL:
            if self.x is None or self.y is None:
                raise ValueError(f"gamepad {self.control!r} is a one-dimensional axis; x and y are required")
            if self.y != 0:
                raise ValueError(f"gamepad {self.control!r} is one-dimensional; y must be 0")
            if self.x < 0:
                raise ValueError(f"gamepad {self.control!r} is a trigger; x must be between 0 and 1")
            return self
        if self.x is None or self.y is None:
            raise ValueError(f"gamepad {self.control!r} is a vector control; x and y are required")
        if self.control == "dpad" and (self.x not in (-1, 0, 1) or self.y not in (-1, 0, 1)):
            raise ValueError("gamepad dpad is digital; x and y must each be -1, 0 or 1")
        return self

    @property
    def declared_type(self) -> str:
        kind = control_type(self.device, self.control)
        assert kind is not None
        return kind


def _duplicate_binding(inputs: Sequence[InputBinding]) -> str | None:
    seen = set()
    for item in inputs:
        key = (item.device, item.control)
        if key in seen:
            return f"{item.device} control {item.control!r} appears twice"
        seen.add(key)
    return None


class ActionStep(Strict):
    """One ordered skill step: hold its inputs, release and verify them, then wait."""

    inputs: list[InputBinding] = Field(min_length=1, max_length=MAX_INPUTS,
                                       description="Controls held together for this step")
    hold_s: float = Field(gt=0, le=MAX_HOLD_S, allow_inf_nan=False,
                          description="Declared hold in seconds; a tap must be resolved to a tested positive hold")
    wait_s: float = Field(ge=0, le=MAX_HOLD_S, allow_inf_nan=False,
                          description="Seconds to wait after this step's inputs are released and verified")

    @model_validator(mode="after")
    def _no_duplicate_control(self) -> "ActionStep":
        duplicate = _duplicate_binding(self.inputs)
        if duplicate:
            raise ValueError(duplicate)
        return self


class ActionDefinition(Strict):
    """One action (inputs + default_lease_s) or one skill (steps + max_s), never both."""

    backend: Literal["input_system"]
    inputs: list[InputBinding] | None = Field(None, min_length=1, max_length=MAX_INPUTS,
                                              description="Controls held together for one action")
    default_lease_s: float | None = Field(
        None, gt=0, le=MAX_HOLD_S, allow_inf_nan=False,
        description="Declared hold and lease for this action; the measured applied duration is reported separately")
    steps: list[ActionStep] | None = Field(None, min_length=1, max_length=MAX_STEPS,
                                           description="Ordered bounded skill steps")
    max_s: float | None = Field(None, gt=0, le=MAX_HOLD_S, allow_inf_nan=False,
                                description="Upper bound on the whole skill, including waits")

    @model_validator(mode="after")
    def _one_mode(self) -> "ActionDefinition":
        action = self.inputs is not None or self.default_lease_s is not None
        skill = self.steps is not None or self.max_s is not None
        if action and skill:
            raise ValueError("a definition is either inputs+default_lease_s or steps+max_s, never both")
        if not action and not skill:
            raise ValueError("a definition needs inputs+default_lease_s or steps+max_s")
        if action:
            if self.inputs is None:
                raise ValueError("an action requires inputs")
            if self.default_lease_s is None:
                raise ValueError("an action requires default_lease_s")
            duplicate = _duplicate_binding(self.inputs)
            if duplicate:
                raise ValueError(duplicate)
            return self
        if self.steps is None:
            raise ValueError("a skill requires ordered steps")
        if self.max_s is None:
            raise ValueError("a skill requires max_s")
        declared = sum(step.hold_s + step.wait_s for step in self.steps)
        if declared > self.max_s:
            raise ValueError(f"declared step holds and waits take {declared:g} s, more than max_s {self.max_s:g}")
        return self

    @property
    def is_skill(self) -> bool:
        return self.steps is not None


class ActionCatalog(Strict):
    """The exported catalog: modern shape {"actions": {name: definition}}."""

    actions: dict[str, ActionDefinition] = Field(
        default_factory=dict,
        description="Named executable declarations; the key is the act name S1 may select")

    @model_validator(mode="after")
    def _names(self) -> "ActionCatalog":
        import re

        pattern = re.compile(NAME)
        for name in self.actions:
            if not pattern.fullmatch(name):
                raise ValueError(f"action name {name!r} is not a lower_snake_case identifier")
        return self

    def declaration(self, name: str) -> ActionDefinition:
        try:
            return self.actions[name]
        except KeyError:
            raise ValueError(f"{name!r} is not a catalog action") from None


# --- receipt matching ------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptRequirement:
    """One receipt position an exact declaration requires."""

    step_index: int
    device: str
    control: str
    x: float | None
    y: float | None
    declared_hold_s: float
    pointer_impulse: bool
    wait_s: float = 0.0


def binding_requirements(inputs: Sequence[InputBinding], hold_s: float, step_index: int = 0,
                         wait_s: float = 0.0) -> tuple[ReceiptRequirement, ...]:
    return tuple(ReceiptRequirement(step_index, item.device, item.control, item.x, item.y, hold_s,
                                   is_pointer_impulse(item.device, item.control), wait_s)
                 for item in inputs)


def step_requirements(steps: Sequence[ActionStep]) -> tuple[ReceiptRequirement, ...]:
    return tuple(requirement for index, step in enumerate(steps)
                 for requirement in binding_requirements(step.inputs, step.hold_s, index, step.wait_s))


def declaration_requirements(declaration: ActionDefinition) -> tuple[ReceiptRequirement, ...]:
    """The exact receipt positions one action definition requires, in declared order."""
    if declaration.is_skill:
        return step_requirements(declaration.steps or ())
    hold = declaration.default_lease_s
    return () if hold is None else binding_requirements(declaration.inputs or (), hold)


def declaration_totals(declaration: ActionDefinition) -> tuple[float | None, float | None]:
    """The declared hold-plus-wait total and, for a skill, its bound."""
    if declaration.is_skill:
        return sum(step.hold_s + step.wait_s for step in declaration.steps or ()), declaration.max_s
    return declaration.default_lease_s, None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _same_value(declared: float | None, observed: float | None) -> bool:
    if declared is None or observed is None:
        return declared is None and observed is None
    return declared == observed


def _binding_declaration_problems(expected: InputBinding, observed: InputBinding | None,
                                  where: str) -> list[str]:
    if observed is None:
        return [f"{where} is missing"]
    if (expected.device, expected.control) != (observed.device, observed.control):
        return [f"{where} is {observed.device}/{observed.control}, "
                f"expected {expected.device}/{expected.control}"]
    if not _same_value(expected.x, observed.x) or not _same_value(expected.y, observed.y):
        return [f"{where} has x={observed.x!r}, y={observed.y!r}, "
                f"expected x={expected.x!r}, y={expected.y!r}"]
    return []


def _definition_problems(declared: ActionDefinition, observed: ActionDefinition) -> list[str]:
    problems: list[str] = []
    if observed.backend != declared.backend:
        problems.append(f"the recorded declaration backend is {observed.backend!r}, expected {declared.backend!r}")
    if declared.is_skill != observed.is_skill:
        return problems + [f"the recorded declaration is a {'skill' if observed.is_skill else 'action'}, "
                           f"expected a {'skill' if declared.is_skill else 'action'}"]
    if declared.is_skill:
        expected_steps, observed_steps = declared.steps or [], observed.steps or []
        if len(expected_steps) != len(observed_steps):
            return problems + [f"the recorded declaration has {len(observed_steps)} steps, "
                               f"expected {len(expected_steps)}"]
        for index, (expected, actual) in enumerate(zip(expected_steps, observed_steps)):
            where = f"declared step {index}"
            if len(expected.inputs) != len(actual.inputs):
                problems.append(f"{where} has {len(actual.inputs)} inputs, expected {len(expected.inputs)}")
                continue
            for position, (expected_input, actual_input) in enumerate(zip(expected.inputs, actual.inputs)):
                problems += _binding_declaration_problems(expected_input, actual_input,
                                                          f"{where} input {position}")
            if expected.hold_s != actual.hold_s:
                problems.append(f"{where} hold is {actual.hold_s!r} s, expected {expected.hold_s!r} s")
            if expected.wait_s != actual.wait_s:
                problems.append(f"{where} wait is {actual.wait_s!r} s, expected {expected.wait_s!r} s")
        if declared.max_s != observed.max_s:
            problems.append(f"the recorded declaration max_s is {observed.max_s!r} s, "
                            f"expected {declared.max_s!r} s")
        return problems
    expected_inputs, observed_inputs = declared.inputs or [], observed.inputs or []
    if len(expected_inputs) != len(observed_inputs):
        return problems + [f"the recorded declaration has {len(observed_inputs)} inputs, "
                           f"expected {len(expected_inputs)}"]
    for index, (expected, actual) in enumerate(zip(expected_inputs, observed_inputs)):
        problems += _binding_declaration_problems(expected, actual, f"declared input {index}")
    if declared.default_lease_s != observed.default_lease_s:
        problems.append(f"the recorded declaration hold is {observed.default_lease_s!r} s, "
                        f"expected {declared.default_lease_s!r} s")
    return problems


def declaration_problems(declared: ActionDefinition, recorded: Any) -> list[str]:
    """Return every way a recorded declaration differs from the exact requested one.

    This is exact equality of inputs, values, holds, waits and `max_s`, before any measured
    timing allowance. A missing or historical bare declaration fails closed; it is never
    guessed from receipts. The evidence label (`action`) is deliberately not compared: an S1
    label and the calibration harness name may legitimately differ for the same declaration.
    """
    if recorded is None:
        return ["no exact declaration was recorded"]
    if isinstance(recorded, dict):
        try:
            recorded = ActionDefinition.model_validate(recorded)
        except ValueError:
            return ["the recorded declaration is not a valid action definition"]
    if not isinstance(recorded, ActionDefinition):
        return ["the recorded declaration is not an action definition"]
    return _definition_problems(declared, recorded)


def receipt_problems(requirements: Sequence[ReceiptRequirement], receipts: Sequence[Any], *,
                     declared: ActionDefinition, applied_total_s: float | None = None,
                     terminal_start_s: float | None = None,
                     terminal_end_s: float | None = None) -> list[str]:
    """Return every way the receipts fail to prove the exact declared input sequence.

    Receipts are duck-typed (`step_index`, `device`, `control`, `x`, `y`,
    `applied_duration_s`, `applied_delta_x`, `applied_delta_y`, `input_start_wall_s`,
    `input_end_wall_s`, `release_verified`) so this module stays the single matcher for
    manifest assembly, cache replay, verification and the offline probe.

    A chord must acknowledge and release within `CHORD_WINDOW_TOLERANCE_S`; a skill must
    release every step before the next apply, wait each declared `wait_s` (including the final
    wait before the measured terminal end), and report a measured total at least as long as
    the declared holds and waits. Nothing here reads a configured value as a measurement.
    """
    problems: list[str] = []
    if len(receipts) != len(requirements):
        problems.append(f"expected {len(requirements)} applied-input receipts, got {len(receipts)}")
    group_sizes: dict[int, int] = {}
    for item in requirements:
        group_sizes[item.step_index] = group_sizes.get(item.step_index, 0) + 1
    rows: list[tuple[ReceiptRequirement, Any, float | None, float | None]] = []
    for index, (expected, receipt) in enumerate(zip(requirements, receipts)):
        where = f"receipts[{index}]"
        if getattr(receipt, "step_index", None) != expected.step_index:
            problems.append(f"{where}: step_index is {getattr(receipt, 'step_index', None)!r}, "
                            f"expected {expected.step_index}")
        if getattr(receipt, "device", None) != expected.device:
            problems.append(f"{where}: device is {getattr(receipt, 'device', None)!r}, expected {expected.device!r}")
        if getattr(receipt, "control", None) != expected.control:
            problems.append(f"{where}: control is {getattr(receipt, 'control', None)!r}, expected {expected.control!r}")
        if not _same_value(expected.x, _number(getattr(receipt, "x", None))):
            problems.append(f"{where}: x is {getattr(receipt, 'x', None)!r}, expected {expected.x!r}")
        if not _same_value(expected.y, _number(getattr(receipt, "y", None))):
            problems.append(f"{where}: y is {getattr(receipt, 'y', None)!r}, expected {expected.y!r}")
        if getattr(receipt, "release_verified", None) is not True:
            problems.append(f"{where}: release was not verified")
        duration = _number(getattr(receipt, "applied_duration_s", None))
        if expected.pointer_impulse:
            delta_x = _number(getattr(receipt, "applied_delta_x", None))
            delta_y = _number(getattr(receipt, "applied_delta_y", None))
            if delta_x is None or delta_y is None:
                problems.append(f"{where}: the pointer impulse has no measured applied delta")
            else:
                for axis, observed_delta, declared_value in (("x", delta_x, expected.x),
                                                             ("y", delta_y, expected.y)):
                    if declared_value is None:
                        problems.append(f"{where}: the pointer impulse declares no {axis} value")
                        continue
                    target = declared_value * MOUSE_DELTA_PIXEL_BOUND
                    if abs(observed_delta - target) > MOUSE_DELTA_TOLERANCE_PX:
                        problems.append(
                            f"{where}: applied delta {axis} {observed_delta:g} px does not match the "
                            f"declared {target:g} px ({declared_value:g} * {MOUSE_DELTA_PIXEL_BOUND:g})")
            if getattr(receipt, "applied_duration_s", None) is not None and duration is None:
                problems.append(f"{where}: applied duration is not a measured finite number")
        else:
            if duration is None:
                problems.append(f"{where}: the held input has no measured applied duration")
            elif abs(duration - expected.declared_hold_s) > hold_tolerance(expected.declared_hold_s):
                problems.append(f"{where}: applied duration {duration:g} s does not match the declared "
                                f"{expected.declared_hold_s:g} s hold")
        start = _number(getattr(receipt, "input_start_wall_s", None))
        end = _number(getattr(receipt, "input_end_wall_s", None))
        needs_times = (declared.is_skill or not expected.pointer_impulse
                       or group_sizes.get(expected.step_index, 1) > 1)
        if needs_times and (start is None or end is None):
            problems.append(f"{where}: no measured start and end time")
        if start is not None and end is not None and end < start:
            problems.append(f"{where}: input_end_wall_s is before input_start_wall_s")
        rows.append((expected, receipt, start, end))

    groups: list[list[tuple[ReceiptRequirement, Any, float | None, float | None]]] = []
    for row in rows:
        if not groups or groups[-1][0][0].step_index != row[0].step_index:
            groups.append([])
        groups[-1].append(row)
    windows: dict[int, tuple[float, float]] = {}
    step_order: list[int] = []
    for group in groups:
        step_index = group[0][0].step_index
        step_order.append(step_index)
        starts = [row[2] for row in group]
        ends = [row[3] for row in group]
        if any(value is None for value in starts) or any(value is None for value in ends):
            continue
        present_starts = [value for value in starts if value is not None]
        present_ends = [value for value in ends if value is not None]
        windows[step_index] = (min(present_starts), max(present_ends))
        if len(group) > 1:
            start_span = max(present_starts) - min(present_starts)
            if start_span > CHORD_WINDOW_TOLERANCE_S:
                problems.append(f"step {step_index}: the inputs were not applied together; starts span "
                                f"{start_span:.3f} s, more than the {CHORD_WINDOW_TOLERANCE_S:g} s allowance")
            end_span = max(present_ends) - min(present_ends)
            if end_span > CHORD_WINDOW_TOLERANCE_S:
                problems.append(f"step {step_index}: the inputs were not released together; ends span "
                                f"{end_span:.3f} s, more than the {CHORD_WINDOW_TOLERANCE_S:g} s allowance")
    if len(step_order) != len(set(step_order)):
        problems.append("the receipts are not grouped by step in execution order")

    for previous, current in zip(groups, groups[1:]):
        previous_step, current_step = previous[0][0].step_index, current[0][0].step_index
        if current_step <= previous_step:
            problems.append(f"step {current_step} receipts follow step {previous_step} out of order")
            continue
        previous_window, current_window = windows.get(previous_step), windows.get(current_step)
        if previous_window is None or current_window is None:
            continue
        previous_end, current_start = previous_window[1], current_window[0]
        if current_start < previous_end - CHORD_WINDOW_TOLERANCE_S:
            problems.append(f"step {current_step} was applied before step {previous_step} was released")
        observed_wait = current_start - previous_end
        declared_wait = previous[0][0].wait_s
        if observed_wait < declared_wait - WAIT_TOLERANCE_S:
            problems.append(f"step {current_step} was applied {observed_wait:.3f} s after step "
                            f"{previous_step} was released, less than the declared {declared_wait:g} s wait")

    declared_total, max_s = declaration_totals(declared)
    if declared.is_skill:
        if applied_total_s is None:
            problems.append("the skill has no measured total applied duration")
        if terminal_start_s is None or terminal_end_s is None:
            problems.append("the skill has no measured execution start and end")
        last_wait = groups[-1][0][0].wait_s if groups else 0.0
        last_window = windows.get(step_order[-1]) if step_order else None
        if terminal_end_s is None:
            problems.append("the skill has no measured terminal end to prove its final wait")
        elif last_window is not None:
            observed_final = terminal_end_s - last_window[1]
            if observed_final < last_wait - WAIT_TOLERANCE_S:
                problems.append(f"the skill ended {observed_final:.3f} s after its last release, less than "
                                f"the declared {last_wait:g} s final wait")
        if (applied_total_s is not None and terminal_start_s is not None
                and terminal_end_s is not None):
            measured_window = terminal_end_s - terminal_start_s
            if abs(measured_window - applied_total_s) > WAIT_TOLERANCE_S:
                problems.append(f"the reported total {applied_total_s:g} s disagrees with the measured "
                                f"execution window {measured_window:g} s")
            if len(windows) == len(step_order) and step_order:
                measured_floor = (sum(windows[step][1] - windows[step][0] for step in step_order)
                                  + sum(group[0][0].wait_s for group in groups))
                if applied_total_s + WAIT_TOLERANCE_S < measured_floor:
                    problems.append(f"the measured total {applied_total_s:g} s is shorter than the measured "
                                    f"step holds plus declared waits ({measured_floor:g} s)")
            if max_s is not None and applied_total_s > max_s + WAIT_TOLERANCE_S:
                problems.append(f"the measured total {applied_total_s:g} s exceeds max_s {max_s:g} s beyond "
                                f"the {WAIT_TOLERANCE_S:g} s allowance")
            first_window = windows.get(step_order[0]) if step_order else None
            if first_window is not None and terminal_start_s > first_window[0] + WAIT_TOLERANCE_S:
                problems.append("the measured execution window starts after the first input was applied")
            if last_window is not None and terminal_end_s < last_window[1] - WAIT_TOLERANCE_S:
                problems.append("the measured execution window ends before the last input was released")
    elif applied_total_s is not None and declared_total is not None and applied_total_s + HOLD_TOLERANCE_S < declared_total:
        problems.append(f"the measured total {applied_total_s:g} s is shorter than the declared {declared_total:g} s")
    return problems


def evidence_receipt_problems(evidence: Any, declared: ActionDefinition) -> list[str]:
    """Declaration equality and receipts, without a terminal status or release claim."""
    if evidence is None:
        return ["no execution evidence was supplied"]
    problems = declaration_problems(declared, getattr(evidence, "declaration", None))
    if problems:
        return problems
    requirements = declaration_requirements(declared)
    if not requirements:
        return ["the declaration has no resolved positive hold"]
    return receipt_problems(requirements, getattr(evidence, "receipts", ()), declared=declared,
                            applied_total_s=_number(getattr(evidence, "applied_duration_s", None)),
                            terminal_start_s=_number(getattr(evidence, "input_start_wall_s", None)),
                            terminal_end_s=_number(getattr(evidence, "input_end_wall_s", None)))


def evidence_problems(evidence: Any, declared: ActionDefinition) -> list[str]:
    """Problems that stop one execution evidence record proving its exact declaration."""
    if evidence is None:
        return ["no execution evidence was supplied"]
    problems = declaration_problems(declared, getattr(evidence, "declaration", None))
    if problems:
        return problems
    status = getattr(evidence, "status", None)
    status_value = getattr(status, "value", status)
    if status_value != "completed":
        return [f"the execution status is {status_value!r}, not 'completed'"]
    if getattr(evidence, "release_verified", None) is not True:
        return ["the terminal execution did not verify input release"]
    return evidence_receipt_problems(evidence, declared)
