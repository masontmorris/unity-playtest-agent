"""Directives, findings and run records (build bible 5.6, 5.8, 5.9)."""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field, model_validator

from .bridge import ExecStatus, RejectReason
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


_UNFINISHED = frozenset({ExecStatus.failed, ExecStatus.cancelled, ExecStatus.timed_out})


class DirectiveWindow(Strict):
    """What S1's inputs did between a directive first reaching S1 and its outcome. Counts, never a verdict.

    A `not_met` whose window completed no input was never tested. The window says so, and so does the
    directive's `error_class`.
    """

    first_turn: int = Field(ge=0, description="The first turn whose context stated the directive")
    first_game_s: float | None = Field(
        None, ge=0, description="Game time of that context's observation; the deadline counts from here. "
                                "Null when the game clock was unknown")
    turns: int = Field(ge=0, description="Turns completed from first_turn up to the outcome")
    inputs_sent: int = Field(ge=0)
    inputs_completed: int = Field(ge=0)
    inputs_rejected: dict[RejectReason, int] = Field(default_factory=dict, description="By the bridge's reason")
    inputs_unfinished: dict[ExecStatus, int] = Field(
        default_factory=dict, description="Failed, cancelled or timed out, by terminal status")
    inputs_unrecorded: int = Field(0, ge=0, description="Sent, but the bridge returned no record")
    waits: int = Field(0, ge=0)
    escalations: int = Field(0, ge=0)

    @model_validator(mode="after")
    def _counts_add_up(self) -> "DirectiveWindow":
        if any(count < 0 for count in (*self.inputs_rejected.values(), *self.inputs_unfinished.values())):
            raise ValueError("window counts cannot be negative")
        if not set(self.inputs_unfinished) <= _UNFINISHED:
            raise ValueError("inputs_unfinished counts only failed, cancelled or timed_out inputs")
        accounted = (self.inputs_completed + sum(self.inputs_rejected.values())
                     + sum(self.inputs_unfinished.values()) + self.inputs_unrecorded)
        if accounted != self.inputs_sent:
            raise ValueError("every input sent must be completed, rejected, unfinished or unrecorded")
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
    window: DirectiveWindow | None = Field(None, description="Set when the directive closes; null while open")

    @model_validator(mode="after")
    def _consistency(self) -> "Directive":
        if self.expected is None and self.outcome in (DirectiveOutcome.met, DirectiveOutcome.not_met):
            raise ValueError("a directive with no expected postcondition cannot be scored met or not_met")
        if self.error_class is not None and self.outcome is not DirectiveOutcome.not_met:
            raise ValueError("error_class only applies to a not_met outcome")
        if self.window is not None and self.outcome is DirectiveOutcome.open:
            raise ValueError("a window is recorded only when the directive closes")
        window = self.window
        if self.error_class is DirectiveErrorClass.no_attempt and (window is None or window.inputs_sent != 0):
            raise ValueError("no_attempt needs a window in which no input was sent")
        if self.error_class is DirectiveErrorClass.action_rejected and (
                window is None or window.inputs_completed != 0 or not sum(window.inputs_rejected.values())):
            raise ValueError("action_rejected needs a window in which inputs were rejected and none completed")
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


# --- suspicion ------------------------------------------------------------------


class SuspicionBasis(str, Enum):
    invariant = "invariant"  # a manifest invariant did not hold for the readings
    effect_mismatch = "effect_mismatch"  # an action's measured effect differs from its calibrated one beyond the recorded spread
    range_exit = "range_exit"  # a reader left the range its earlier readings in the session showed


class Suspicion(Strict):
    """A measured fact worth recording (build bible section 9, step 1).

    It is written to the trace and stated to S1 as a fact. It is evidence a later step can cite by `trace_seq`;
    it is not a finding, it never escalates, and nothing acts on it by itself. `statement` carries measured
    values only. `detail` holds the same numbers as data; its keys depend on `basis` (see
    `docs/design/suspicions.md`).
    """

    suspicion_id: str = Field(min_length=1, description="Unique within the run, in the order raised")
    basis: SuspicionBasis
    turn: int = Field(ge=0)
    based_on_seq: int = Field(ge=0, description="Bridge seq of the observation of the turn it was raised in")
    game_s: float | None = Field(None, ge=0, allow_inf_nan=False, description="Game clock of that observation; null when unknown")
    subject: str = Field(min_length=1, description="The invariant's name, the choice id, or public.<reader alias>")
    statement: str = Field(min_length=1, description="The fact S1 reads: measured values only, no advice")
    detail: dict[str, Any] = Field(default_factory=dict)
    evidence_seq: list[int] = Field(default_factory=list, description="trace_seq of the entries it rests on")

    @model_validator(mode="after")
    def _well_formed(self) -> "Suspicion":
        if not self.statement.strip():
            raise ValueError("a suspicion needs a statement")
        if any(isinstance(item, bool) or item < 0 for item in self.evidence_seq):
            raise ValueError("evidence_seq must be non-negative trace sequence numbers")
        if len(set(self.evidence_seq)) != len(self.evidence_seq):
            raise ValueError("evidence_seq cites the same entry twice")
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
    game_worktree: Literal["clean", "modified", "unknown"] = "unknown"
    game_modified_paths: list[str] = Field(default_factory=list)
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

    @model_validator(mode="after")
    def _worktree_evidence(self) -> "RunRecord":
        if self.game_worktree == "modified" and not self.game_modified_paths:
            raise ValueError("modified worktree requires modified paths")
        if self.game_worktree != "modified" and self.game_modified_paths:
            raise ValueError("modified paths require a modified worktree")
        if any(not path or path.startswith(("/", "\\")) or ":" in path or ".." in PurePosixPath(path).parts
               for path in self.game_modified_paths):
            raise ValueError("modified paths must be repository-relative")
        return self
