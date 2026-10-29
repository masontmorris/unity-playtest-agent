"""Bridge wire protocol (build bible 5.1, 5.2, 5.5).

Six verbs: hello/status, observe, act, stop, clock and inspect. Requests flow core -> bridge;
observations, execution records and inspection results flow back. `inspect` exists only in the
Editor and is the only path that runs discovery; it never writes to the project.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from .action_catalog import ActionDefinition
from .common import ClockMode, Envelope, Strict


# --- hello / status -------------------------------------------------------------


class InputBackend(str, Enum):
    input_system = "input_system"
    legacy = "legacy"


class Hello(Envelope):
    type: Literal["hello"] = "hello"
    token: str = Field(min_length=1)
    core_version: str


class LockState(Strict):
    held: bool
    owner_pid: int | None = None
    port: int | None = None


class Status(Envelope):
    type: Literal["status"] = "status"
    bridge_version: str
    unity_version: str
    project: str
    input_backends: list[InputBackend]
    clock_control: bool
    capture: bool
    capture_self_test: Literal["passed", "failed", "unknown"]
    lock: LockState


# --- observe --------------------------------------------------------------------


class ObserveRequest(Envelope):
    type: Literal["observe"] = "observe"
    mode: Literal["subscribe", "once", "unsubscribe"]
    with_frame: bool = False
    rate_hz: float | None = Field(None, gt=0, le=60)


class ReaderError(Strict):
    reader: str
    error: str


class Observation(Envelope):
    """One sample of the game. Missing readers are null plus a reader_errors entry."""

    type: Literal["observation"] = "observation"
    clock_mode: ClockMode
    scene: str
    playing: bool
    paused: bool
    time_scale: float = Field(ge=0)
    frame: int = Field(ge=0)
    public: dict[str, Any] = Field(default_factory=dict)
    ui: dict[str, Any] | None = None
    frame_ref: str | None = None
    reader_errors: list[ReaderError] = Field(default_factory=list)


# --- act ------------------------------------------------------------------------


class ActRequest(Envelope):
    """Start one bounded action. The bridge rejects it atomically if it is stale."""

    type: Literal["act"] = "act"
    action_id: str = Field(min_length=1, description="Unique per request; the execution record echoes it")
    choice_id: str
    action: str = Field(min_length=1, description="Manifest action name")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Declared-value overrides are unsupported; a bridge rejects nonempty params before applying input")
    based_on_seq: int = Field(ge=0, description="Observation the choice was built from")
    max_age_s: float = Field(gt=0)
    depends_on: list[str] = Field(
        default_factory=list,
        description="Public fields that must be unchanged since based_on_seq; from preflight's measured freshness classes",
    )
    lease_s: float | None = Field(None, gt=0, description="Held input runs until superseded, expired or stopped")


class ExecStatus(str, Enum):
    requested = "requested"
    validated = "validated"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"
    rejected = "rejected"


TERMINAL_STATUSES = frozenset(
    {ExecStatus.completed, ExecStatus.failed, ExecStatus.cancelled, ExecStatus.timed_out, ExecStatus.rejected}
)


class RejectReason(str, Enum):
    stale_age = "stale_age"
    stale_fields = "stale_fields"
    unknown_action = "unknown_action"
    invalid_params = "invalid_params"
    not_playing = "not_playing"
    busy = "busy"


class Effect(Strict):
    """A measured or calibrated effect on one field. Unknown values stay null."""

    field: str
    delta: float | None = None
    spread: float | None = Field(None, ge=0, description="Calibrated +/- around delta")
    unit: str | None = None


class AppliedInput(Strict):
    """One physical input the bridge applied, as measured at apply and release time.

    `applied_duration_s` is the measured interval from device apply acknowledgement to
    observed release. It is never copied from configured hold time. A relative pointer
    impulse may report a null duration when its neutral-reset instant was not measured,
    but it always reports the actual pixel delta it produced.
    """

    step_index: int = Field(ge=0, description="0 for an action; the ordered step index for a skill")
    device: Literal["Keyboard", "Mouse", "Gamepad"]
    control: str = Field(min_length=1, description="Physical root control, without a device prefix")
    x: float | None = Field(None, ge=-1, le=1, allow_inf_nan=False,
                            description="Declared normalized value; null for a button")
    y: float | None = Field(None, ge=-1, le=1, allow_inf_nan=False,
                            description="Declared second value; 0 for a one-dimensional axis; null for a button")
    input_start_wall_s: float | None = Field(None, allow_inf_nan=False,
                                             description="Device apply acknowledgement, wall seconds")
    input_end_wall_s: float | None = Field(None, allow_inf_nan=False,
                                           description="Observed release, wall seconds")
    applied_duration_s: float | None = Field(None, ge=0, allow_inf_nan=False,
                                             description="Measured apply-to-release seconds")
    applied_delta_x: float | None = Field(None, allow_inf_nan=False,
                                          description="Actual mouse pixel delta, not a configured value")
    applied_delta_y: float | None = Field(None, allow_inf_nan=False,
                                          description="Actual mouse pixel delta, not a configured value")
    release_verified: bool | None = Field(
        None, description="Device-state read confirmed this input is no longer held")

    @model_validator(mode="after")
    def _value_shape(self) -> "AppliedInput":
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must both be present for a vector input or both null for a button")
        if self.device == "Keyboard" and self.x is not None:
            raise ValueError("a keyboard input is a button; x and y must be null")
        if (self.input_start_wall_s is not None and self.input_end_wall_s is not None
                and self.input_end_wall_s < self.input_start_wall_s):
            raise ValueError("input_end_wall_s must not be before input_start_wall_s")
        return self


class InputExecutionEvidence(Strict):
    """Release-verified receipts for one exact declaration, kept as local evidence.

    The declaration itself is recorded here rather than inferred from receipts or a label:
    support requires exact inputs, values, holds, waits and `max_s`, before any measured
    timing allowance. Historical bare evidence without a declaration fails closed. This
    record proves only that the bridge completed that declaration and applied/released the
    same physical inputs; it is not a claim about game effects.
    """

    action: str = Field(min_length=1, description="The action name this execution was requested under")
    session: str = Field(min_length=1)
    status: ExecStatus
    release_verified: bool
    declaration: ActionDefinition = Field(
        description="The exact requested declaration this evidence proves; required, never guessed")
    applied_duration_s: float | None = Field(None, ge=0, allow_inf_nan=False)
    input_start_wall_s: float | None = Field(None, allow_inf_nan=False)
    input_end_wall_s: float | None = Field(None, allow_inf_nan=False)
    receipts: list[AppliedInput] = Field(default_factory=list)
    wall_utc: datetime | None = None

    @model_validator(mode="after")
    def _completed_has_receipts(self) -> "InputExecutionEvidence":
        value = self.status.value if isinstance(self.status, ExecStatus) else self.status
        if value == "completed" and not self.receipts:
            raise ValueError("a completed execution evidence record needs applied-input receipts")
        if value == "rejected" and self.receipts:
            raise ValueError("a rejected execution applied no input and cannot have receipts")
        if (self.input_start_wall_s is not None and self.input_end_wall_s is not None
                and self.input_end_wall_s < self.input_start_wall_s):
            raise ValueError("input_end_wall_s must not be before input_start_wall_s")
        return self


class ExecutionRecord(Envelope):
    type: Literal["execution"] = "execution"
    action_id: str
    status: ExecStatus
    validated_against_seq: int | None = None
    reject_reason: RejectReason | None = None
    input_start_game_s: float | None = None
    input_end_game_s: float | None = None
    input_start_wall_s: float | None = None
    input_end_wall_s: float | None = None
    applied_duration_s: float | None = Field(None, ge=0, description="Measured, never taken from configuration")
    release_verified: bool | None = None
    applied_inputs: list[AppliedInput] = Field(
        default_factory=list,
        description="Per-input receipts for a chord or ordered skill; empty for a stale rejection")
    expected_effect: list[Effect] = Field(default_factory=list)
    observed_effect: list[Effect] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_has_reason(self) -> "ExecutionRecord":
        if (self.status is ExecStatus.rejected) != (self.reject_reason is not None):
            raise ValueError("reject_reason is required exactly when status is 'rejected'")
        return self

    @model_validator(mode="after")
    def _receipt_order(self) -> "ExecutionRecord":
        if self.status is ExecStatus.rejected and self.applied_inputs:
            raise ValueError("a rejected execution applied no input and cannot have receipts")
        indices = [item.step_index for item in self.applied_inputs]
        if indices != sorted(indices):
            raise ValueError("applied-input step indices must be in execution order")
        return self


# --- stop / clock ---------------------------------------------------------------


class StopRequest(Envelope):
    type: Literal["stop"] = "stop"
    reason: Literal["user", "reset", "scene_change", "disconnect", "error", "finish"]


class StopResult(Envelope):
    type: Literal["stopped"] = "stopped"
    released: list[str] = Field(description="Inputs released")
    release_verified: bool


class ClockRequest(Envelope):
    type: Literal["clock"] = "clock"
    op: Literal["pause", "resume", "step", "set_scale"]
    step_game_s: float | None = Field(None, gt=0)
    time_scale: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _op_args(self) -> "ClockRequest":
        if (self.op == "step") != (self.step_game_s is not None):
            raise ValueError("step_game_s is required exactly when op is 'step'")
        if (self.op == "set_scale") != (self.time_scale is not None):
            raise ValueError("time_scale is required exactly when op is 'set_scale'")
        return self


# --- inspect (Editor only) ------------------------------------------------------


class InspectRequest(Envelope):
    type: Literal["inspect"] = "inspect"
    stage: Literal["inventory", "discovery", "transforms", "fields"]


class InspectionCap(Strict):
    """How completely one collection was sampled.

    An uncounted total stays null; it is never guessed from the returned sample. A known
    observed count must agree with `truncated`: true means some items were left out, false
    means the returned sample is the whole collection.
    """

    limit: int = Field(gt=0)
    observed: int | None = Field(ge=0, description="Items seen, or null when the total is unknown")
    returned: int = Field(ge=0)
    truncated: bool | None = Field(description="Whether the sample was capped, or null when unknown")

    @model_validator(mode="after")
    def _consistent(self) -> "InspectionCap":
        if self.returned > self.limit:
            raise ValueError("returned must not exceed limit")
        if self.observed is not None:
            if self.observed < self.returned:
                raise ValueError("observed must be at least returned")
            if self.truncated is True and self.returned >= self.observed:
                raise ValueError("truncated is true but returned covers observed")
            if self.truncated is False and self.returned != self.observed:
                raise ValueError("truncated is false but returned differs from observed")
        return self


class InspectResult(Envelope):
    """One Editor inspection snapshot.

    `data` is open because each stage carries game-defined reflection descriptors. `caps` is
    mandatory even when empty so a capped sample can never be mistaken for the whole project.
    """

    type: Literal["inspection"] = "inspection"
    stage: Literal["inventory", "discovery", "transforms", "fields"]
    in_reply_to: int = Field(ge=0)
    data: dict[str, Any]
    caps: dict[str, InspectionCap]
    errors: list[str]


class BridgeError(Envelope):
    type: Literal["error"] = "error"
    code: Literal["auth", "busy", "protocol", "invalid", "internal"]
    message: str
    in_reply_to: int | None = None
