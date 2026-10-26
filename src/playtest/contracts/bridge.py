"""Bridge wire protocol (build bible 5.1, 5.2, 5.5).

Five verbs: hello/status, observe, act, stop, clock. Requests flow core -> bridge;
observations and execution records flow back.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

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
    params: dict[str, Any] = Field(default_factory=dict)
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
    expected_effect: list[Effect] = Field(default_factory=list)
    observed_effect: list[Effect] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_has_reason(self) -> "ExecutionRecord":
        if (self.status is ExecStatus.rejected) != (self.reject_reason is not None):
            raise ValueError("reject_reason is required exactly when status is 'rejected'")
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


class BridgeError(Envelope):
    type: Literal["error"] = "error"
    code: Literal["auth", "busy", "protocol", "invalid", "internal"]
    message: str
    in_reply_to: int | None = None
