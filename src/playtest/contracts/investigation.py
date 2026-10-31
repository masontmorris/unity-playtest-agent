"""Directives, findings and run records (build bible 5.6, 5.8, 5.9)."""

from __future__ import annotations

import math
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


MAX_OBJECTIVE_QUOTE = 200


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
    # The frontier's own attribution of the advice to the run objective, and the conditions it authored for
    # continuing or replanning. All three are null on a directive recorded before they existed: absence is
    # unknown provenance, not a claim that the frontier authored conditions. Nothing in code reads them to
    # select an action, help, a wait, a restart or a finish.
    objective_quote: str | None = Field(
        None, max_length=MAX_OBJECTIVE_QUOTE,
        description="A verbatim substring of the run objective this advice serves, as the frontier quoted it")
    continue_if: list[str] | None = Field(
        None, description="What the frontier says to inspect to keep pursuing, authored as advice; null when unknown")
    replan_if: list[str] | None = Field(
        None, description="What the frontier says to inspect to replan, authored as advice; null when unknown")
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

    @model_validator(mode="after")
    def _objective_attribution(self) -> "Directive":
        """The frontier's attribution is all-or-none, and never blank.

        A record written before these fields existed has all three null, which stays readable: its provenance
        is unknown, not an empty quote or invented conditions. A record that has any one of them must have all
        three, non-empty, so a half-written attribution cannot pass as authored advice.
        """
        written = (self.objective_quote, self.continue_if, self.replan_if)
        if all(value is None for value in written):
            return self
        if any(value is None for value in written):
            raise ValueError("objective attribution is all-or-none: objective_quote, continue_if and replan_if")
        if not self.objective_quote.strip():
            raise ValueError("objective_quote is empty")
        for name, conditions in (("continue_if", self.continue_if), ("replan_if", self.replan_if)):
            if not conditions:
                raise ValueError(f"{name} is empty; say unknown explicitly instead")
            if any(not condition.strip() for condition in conditions):
                raise ValueError(f"{name} has an empty condition")
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


EFFECT_PREFIX = "effect."
_ORDERING = (Comparator.lt, Comparator.le, Comparator.gt, Comparator.ge)


def effect_action(field: str) -> str | None:
    """The action an `effect.<action>` repro-check field names; None for any other field."""
    return field[len(EFFECT_PREFIX):] if field.startswith(EFFECT_PREFIX) else None


class ReproCheck(Strict):
    """What a replay must show to reproduce a finding: one field, one comparator, one value.

    A reader field (`public.<reader>`) is scored with the shared check evaluator against the readers a replay
    reads. `changed` and `unchanged` take no value and compare with the reading when the replay starts.

    `effect.<action>` is the net displacement, in world units, of one execution of that action, measured between
    two position snapshots. It shows an action that stops having its effect, which leaves the readers as they are
    at rest. It takes `<`, `<=`, `>` or `>=` and a finite number, and no tolerance; the other comparators have
    nothing to compare a displacement with.
    """

    field: str = Field(min_length=1, description="Dotted path into the observation, e.g. public.<reader>; or "
                                                 "effect.<action>, the net displacement of one execution of an action")
    op: Comparator
    value: Any = None
    tolerance: float | None = Field(None, ge=0, description="Only for numeric == or !=")

    @model_validator(mode="after")
    def _value_needed(self) -> "ReproCheck":
        if self.op in (Comparator.changed, Comparator.unchanged):
            if self.value is not None:
                raise ValueError(f"'{self.op.value}' takes no value")
        elif self.value is None:
            raise ValueError(f"'{self.op.value}' needs a value")
        return self

    @model_validator(mode="after")
    def _effect_is_a_bound(self) -> "ReproCheck":
        action = effect_action(self.field)
        if action is None:
            return self
        if not action.strip():
            raise ValueError("an effect field names an action: effect.<action>")
        if self.op not in _ORDERING:
            raise ValueError(f"an effect check compares with <, <=, > or >=, not '{self.op.value}'")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value):
            raise ValueError("an effect check compares with a finite number")
        if self.tolerance is not None:
            raise ValueError("an effect check takes no tolerance")
        return self


Severity = Literal["blocker", "major", "minor", "cosmetic", "unknown"]


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
    repro_check: ReproCheck | None = Field(None, description="What a replay must show; null when none was given")
    severity: Severity | None = None
    severity_reason: str | None = None

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
    drift = "drift"  # a reader set a new low (or high) at each of many consecutive readings


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
