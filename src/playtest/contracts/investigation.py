"""Directives, findings and run records (build bible 5.6, 5.8, 5.9)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import ClockMode, Strict


# --- directive ------------------------------------------------------------------


class DirectiveOutcome(str, Enum):
    open = "open"
    met = "met"
    not_met = "not_met"
    expired = "expired"
    invalidated = "invalidated"
    unknown = "unknown"


class DirectiveErrorClass(str, Enum):
    wrong_target = "wrong_target"
    unreachable = "unreachable"
    precondition_false = "precondition_false"
    action_rejected = "action_rejected"
    no_attempt = "no_attempt"
    ambiguous = "ambiguous"


class Comparator(str, Enum):
    eq = "=="
    ne = "!="
    lt = "<"
    le = "<="
    gt = ">"
    ge = ">="
    changed = "changed"
    unchanged = "unchanged"


class Target(Strict):
    kind: Literal["field", "entity", "ui_element", "location"]
    ref: str = Field(min_length=1)


class Expected(Strict):
    """A checkable postcondition over a public observation field."""

    field: str = Field(min_length=1, description="Dotted path into the observation, e.g. public.<reader>")
    op: Comparator
    value: Any = None

    @model_validator(mode="after")
    def _value_needed(self) -> "Expected":
        if self.op in (Comparator.changed, Comparator.unchanged):
            if self.value is not None:
                raise ValueError(f"'{self.op.value}' takes no value")
        elif self.value is None:
            raise ValueError(f"'{self.op.value}' needs a value")
        return self


class Directive(Strict):
    directive_id: str = Field(min_length=1)
    parent: str | None = None
    source: Literal["frontier"] = "frontier"
    model: str
    based_on_seq: int = Field(ge=0)
    text: str = Field(min_length=1)
    target: Target | None = None
    expected: Expected | None = Field(None, description="Null when no checkable postcondition; outcome is then 'unknown'")
    tolerance: float | None = Field(None, ge=0)
    deadline_game_s: float | None = Field(None, gt=0)
    outcome: DirectiveOutcome = DirectiveOutcome.open
    evidence_seq: list[int] = Field(default_factory=list)
    error_class: DirectiveErrorClass | None = None

    @model_validator(mode="after")
    def _consistency(self) -> "Directive":
        if self.expected is None and self.outcome in (DirectiveOutcome.met, DirectiveOutcome.not_met):
            raise ValueError("a directive with no expected postcondition cannot be scored met or not_met")
        if self.error_class is not None and self.outcome is not DirectiveOutcome.not_met:
            raise ValueError("error_class only applies to a not_met outcome")
        return self


# --- finding --------------------------------------------------------------------


class FindingState(str, Enum):
    suspicion = "suspicion"
    candidate = "candidate"
    repro_attempted = "repro_attempted"
    reproduced = "reproduced"
    unconfirmed = "unconfirmed"
    fix_proposed = "fix_proposed"
    fix_verified = "fix_verified"


class ExpectedSource(str, Enum):
    invariant = "invariant"
    objective = "objective"
    developer_assertion = "developer_assertion"
    interpretation = "interpretation"


class Repro(Strict):
    attempts: int = Field(ge=0)
    reproduced: int = Field(ge=0)
    clock_mode: ClockMode
    reset: str = Field(min_length=1)
    tolerance: str | None = None

    @model_validator(mode="after")
    def _k_le_n(self) -> "Repro":
        if self.reproduced > self.attempts:
            raise ValueError("reproduced cannot exceed attempts")
        return self


class Finding(Strict):
    finding_id: str = Field(min_length=1)
    state: FindingState
    title: str = Field(min_length=1)
    expected: str
    expected_source: ExpectedSource
    actual: str
    conditions: str | None = None
    steps: list[str] = Field(default_factory=list)
    evidence_seq: list[int] = Field(default_factory=list)
    clips: list[str] = Field(default_factory=list)
    repro: Repro | None = None
    uncertainty: str | None = None

    @model_validator(mode="after")
    def _state_evidence(self) -> "Finding":
        if self.state is not FindingState.suspicion and not self.evidence_seq:
            raise ValueError("a finding past 'suspicion' must cite trace sequence numbers")
        if self.state is FindingState.reproduced and (self.repro is None or self.repro.reproduced == 0):
            raise ValueError("'reproduced' needs a repro result with at least one success")
        return self


# --- run record -----------------------------------------------------------------

UNKNOWN = "unknown"


class ModelRef(Strict):
    role: Literal["s1", "frontier", "preflight", "video_judge", "auditor"]
    provider: str
    model: str
    resolved_model: str = UNKNOWN
    prompt_hash: str = UNKNOWN


class Budget(Strict):
    max_wall_s: float | None = Field(None, gt=0)
    max_usd: float | None = Field(None, gt=0)
    max_frontier_calls: int | None = Field(None, ge=0)


class RunRecord(Strict):
    """Written first by every run. Values nobody measured are the string 'unknown', never guessed."""

    type: Literal["run"] = "run"
    run_id: str = Field(min_length=1)
    game_project: str
    game_commit: str = UNKNOWN
    unity_version: str = UNKNOWN
    scene: str = UNKNOWN
    manifest_fingerprint: str = UNKNOWN
    clock_mode: ClockMode
    input_mode: str = UNKNOWN
    models: list[ModelRef]
    objective: str = Field(min_length=1)
    budget: Budget
    reset_method: str = UNKNOWN
    bridge_version: str = UNKNOWN
    core_version: str
