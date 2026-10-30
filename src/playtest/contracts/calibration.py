"""Local preflight calibration record; measurements and unknowns remain distinct."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .bridge import InputExecutionEvidence, InspectionCap, StopResult
from .common import Strict


class ViewTurn(Strict):
    """How far the game view turned: the mean change between the view read just before an input and
    the read just after it, over the valid trials that measured it both times.

    Yaw is wrapped to [-180, 180) per trial before averaging, so a turn across the +/-180 degree seam
    is a small change, not a near-full circle. A positive yaw turns right and a positive pitch looks down.
    The same wrap means a turn of 180 degrees or more in one apply cannot be read from two readings: it
    shows as a smaller turn, and repeats that differ by a little then differ by a lot. `spread_deg` is how
    far the trials disagree, so a mean that cannot be trusted says so.
    """

    trials: int = Field(ge=1, description="Valid trials that read the view before and after the input")
    yaw_deg: float = Field(ge=-180, le=180, allow_inf_nan=False, description="Mean yaw change")
    pitch_deg: float = Field(ge=-180, le=180, allow_inf_nan=False, description="Mean pitch change")
    spread_deg: float = Field(
        ge=0, allow_inf_nan=False,
        description="The largest difference between any two of these trials' changes, along yaw or pitch, "
                    "in degrees; 0 for a single trial, which cannot show whether the turn repeats")


class HoldMeasurement(Strict):
    requested_hold_s: float = Field(gt=0, description="Probe setting, not the applied duration")
    valid_trials: int = Field(ge=0)
    mean_delta: tuple[float, float, float] | None = Field(
        description="Mean net entity displacement over the valid trials: the change between the read before "
                    "the input and the read just after it, minus the idle baseline")
    mean_magnitude: float | None = Field(
        ge=0, description="Mean of each valid trial's net displacement magnitude, with the same net meaning")
    sample_variance: float | None = Field(
        ge=0, description="Sample variance of the per-trial net displacement magnitudes")
    effective_in_all_repeats: bool | None = Field(
        description="True when every valid repeat's net displacement met the plan's effect tolerance. "
                    "False is not proof the input had no effect: motion that returns to its start nets to zero")
    mean_view_turn: ViewTurn | None = Field(
        None, description="Mean change of the game view over the valid trials that measured it, or null when no "
                          "valid trial did. A view that was not read is unknown, not a turn of zero degrees; "
                          "null on records from before the view was measured")

    @model_validator(mode="after")
    def _requires_observations(self) -> "HoldMeasurement":
        if self.valid_trials == 0 and (self.mean_delta is not None or self.mean_magnitude is not None):
            raise ValueError("a mean needs at least one valid trial")
        if self.valid_trials > 0 and (self.mean_delta is None or self.mean_magnitude is None):
            raise ValueError("valid trials require measured means")
        if self.valid_trials < 2 and (self.sample_variance is not None or self.effective_in_all_repeats is not None):
            raise ValueError("variance and repeat effectiveness need at least two trials")
        if self.mean_view_turn is not None and self.mean_view_turn.trials > self.valid_trials:
            raise ValueError("a view turn cannot average more trials than were valid")
        return self


class ActionCalibration(Strict):
    control: str = Field(min_length=1)
    bindings: list[dict[str, Any]]
    resolution: str
    device: Literal["Keyboard", "Mouse", "Gamepad"] | None = Field(
        None, description="Physical device measured for this control; null on records from the keyboard-only snapshot")
    control_name: str | None = Field(None, description="Physical root control name, without a device prefix")
    control_type: str | None = Field(None, description="Declared Input System control type, e.g. Button, Axis or Stick")
    declared_x: float | None = Field(None, ge=-1, le=1, allow_inf_nan=False,
                                     description="Declared normalized value measured for this control")
    declared_y: float | None = Field(None, ge=-1, le=1, allow_inf_nan=False,
                                     description="Declared second value measured for this control")
    holds: list[HoldMeasurement]
    lowest_tested_effective_requested_hold_s: float | None = Field(ge=0)
    shortest_observed_effective_hold_s: float | None = Field(ge=0)
    minimum_hold_scope: str
    inverted_relative_to_entity_basis: bool | None
    signed_basis_projections: list[float]
    dead_zone: float | None = Field(ge=0)
    dead_zone_reason: str
    sensitivity: float | None = Field(None, ge=0, allow_inf_nan=False,
                                      description="Measured response slope, or null when not measured")
    sensitivity_reason: str = Field(
        "sensitivity is unknown: one declared value per control was measured, not a response curve")


class CalibrationRecord(Strict):
    schema_version: Literal[1]
    status: Literal["measured_with_gaps", "inconclusive"]
    error: str | None
    entity_id: str = Field(min_length=1)
    entity: dict[str, Any]
    coordinate_frame: str
    units: str
    physical_unit: str | None
    plan: dict[str, Any]
    action_cap: InspectionCap
    actions: list[dict[str, Any]]
    unsupported_bindings: list[dict[str, Any]]
    discovery_coverage: dict[str, InspectionCap]
    idle_fields: list[dict[str, Any]]
    later_idle_windows: list[list[dict[str, Any]]] = Field(
        default_factory=list, description="Idle field windows sampled after the key trials; each is its own window")
    input_field_segments: list[dict[str, Any]]
    field_sampling_gap: str | None = Field(
        None, description="Why no held-input field window was usable, or null when none was needed")
    trials: list[dict[str, Any]]
    action_summaries: list[ActionCalibration]
    execution_evidence: list[InputExecutionEvidence] = Field(
        default_factory=list,
        description="Release-verified receipts for exact declarations, used only when a declaration matches")
    freshness: dict[str, Any] | None
    clock: dict[str, Any] | None
    reset: dict[str, Any] | None
    runtime_errors: dict[str, Any] | None
    stop: StopResult | None

    @model_validator(mode="after")
    def _measured_requires_release(self) -> "CalibrationRecord":
        if self.status == "measured_with_gaps" and (self.stop is None or self.stop.release_verified is not True):
            raise ValueError("a measured calibration needs verified input release")
        if self.status == "measured_with_gaps" and (
            not any(trial.get("valid") is True for trial in self.trials)
            or not self.action_summaries or self.freshness is None or self.clock is None or self.reset is None
        ):
            raise ValueError("a measured calibration needs action, field, clock and reset evidence")
        return self
