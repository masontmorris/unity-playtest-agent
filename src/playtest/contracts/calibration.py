"""Local preflight calibration record; measurements and unknowns remain distinct."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .bridge import InspectionCap, StopResult
from .common import Strict


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

    @model_validator(mode="after")
    def _requires_observations(self) -> "HoldMeasurement":
        if self.valid_trials == 0 and (self.mean_delta is not None or self.mean_magnitude is not None):
            raise ValueError("a mean needs at least one valid trial")
        if self.valid_trials > 0 and (self.mean_delta is None or self.mean_magnitude is None):
            raise ValueError("valid trials require measured means")
        if self.valid_trials < 2 and (self.sample_variance is not None or self.effective_in_all_repeats is not None):
            raise ValueError("variance and repeat effectiveness need at least two trials")
        return self


class ActionCalibration(Strict):
    control: str = Field(min_length=1)
    bindings: list[dict[str, Any]]
    resolution: str
    holds: list[HoldMeasurement]
    lowest_tested_effective_requested_hold_s: float | None = Field(ge=0)
    shortest_observed_effective_hold_s: float | None = Field(ge=0)
    minimum_hold_scope: str
    inverted_relative_to_entity_basis: bool | None
    signed_basis_projections: list[float]
    dead_zone: float | None = Field(ge=0)
    dead_zone_reason: str


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
    trials: list[dict[str, Any]]
    action_summaries: list[ActionCalibration]
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
