"""Turn-loop contracts: choices, S1 decisions and the per-turn trace record (build bible 5.3, 5.4)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import Strict


class ChoiceKind(str, Enum):
    action = "action"
    skill = "skill"
    delegate = "delegate"
    escalate = "escalate"
    wait = "wait"
    finish = "finish"
    restart_play_mode = "restart_play_mode"  # an explicit host operation, offered only by a capable host


class ChoiceSource(str, Enum):
    manifest = "manifest"
    frontier = "frontier"
    core = "core"  # wait, escalate, delegate, finish and a capable host's restart exist for every game


class Choice(Strict):
    id: str = Field(min_length=1)
    kind: ChoiceKind
    label: str = Field(min_length=1)
    action: str | None = None
    skill: str | None = None
    role: Literal["frontier", "helper"] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    source: ChoiceSource
    ttl_game_s: float | None = Field(None, gt=0)

    @model_validator(mode="after")
    def _kind_fields(self) -> "Choice":
        need = {
            ChoiceKind.action: ("action",),
            ChoiceKind.skill: ("skill",),
            ChoiceKind.escalate: ("role",),
            ChoiceKind.delegate: ("role",),
        }.get(self.kind, ())
        for name in ("action", "skill", "role"):
            present = getattr(self, name) is not None
            if present != (name in need):
                raise ValueError(f"'{name}' is {'required' if name in need else 'not allowed'} for kind '{self.kind.value}'")
        if self.source is ChoiceSource.frontier and self.kind not in (ChoiceKind.action, ChoiceKind.skill):
            raise ValueError("the frontier may only add choices that instantiate a manifest action or skill")
        if self.kind is ChoiceKind.restart_play_mode and self.source is not ChoiceSource.core:
            raise ValueError("only the core offers a host restart choice")
        return self


class ChoiceSet(Strict):
    set_version: int = Field(ge=0)
    built_from_seq: int = Field(ge=0)
    choices: list[Choice] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "ChoiceSet":
        ids = [c.id for c in self.choices]
        if len(ids) != len(set(ids)):
            raise ValueError("choice ids must be unique within a set")
        return self

    def get(self, choice_id: str) -> Choice | None:
        return next((c for c in self.choices if c.id == choice_id), None)


class WhilePending(Strict):
    mode: Literal["wait", "continue"]
    choice_id: str | None = None

    @model_validator(mode="after")
    def _continue_has_choice(self) -> "WhilePending":
        if (self.mode == "continue") != (self.choice_id is not None):
            raise ValueError("choice_id is required exactly when mode is 'continue'")
        return self


class Decision(Strict):
    """S1's answer. `distribution` is recorded for analysis and never used to select."""

    choice_id: str = Field(min_length=1)
    set_version: int = Field(ge=0)
    reason: str | None = None
    while_pending: WhilePending | None = None
    distribution: dict[str, float] | None = None


class TurnOutcome(str, Enum):
    executed = "executed"
    rejected_invalid = "rejected_invalid"  # S1's answer did not name a valid choice; never repaired
    rejected_stale = "rejected_stale"  # the bridge refused the action as stale
    escalated = "escalated"
    delegated = "delegated"
    finished = "finished"
    restarted = "restarted"  # S1 selected the host operation; its outcome is recorded separately


class OperationStatus(str, Enum):
    ready = "ready"
    failed = "failed"
    cancelled = "cancelled"
    timeout = "timeout"


class OperationOutcome(Strict):
    """What a selected host operation did. It never stands in for an action's execution outcome.

    A ready status means every measured condition the host protocol requires held: a verified release,
    a terminal ready result for this ticket, an observed host ready state at a distinct new generation,
    a fresh bridge endpoint and a valid first observation. Any other status fails the run closed.
    """

    kind: Literal["restart_play_mode"]
    status: OperationStatus
    ticket_id: str | None = None
    from_generation: str | None = None
    to_generation: str | None = None
    reason: str | None = None


class TurnRecord(Strict):
    """One turn in the trace. raw_choice, effective_choice and executed_action must agree (law 1)."""

    type: Literal["turn"] = "turn"
    turn: int = Field(ge=0)
    set_version: int = Field(ge=0)
    based_on_seq: int = Field(ge=0)
    raw_choice: str | None = Field(description="Choice id exactly as S1 returned it; null if unparseable")
    effective_choice: str | None = Field(description="Choice id the loop acted on")
    executed_action: str | None = Field(description="Choice id whose input reached the bridge; null if none was sent")
    outcome: TurnOutcome
    while_pending: WhilePending | None = None
    policy_default: bool = Field(False, description="while_pending was missing or invalid and defaulted to wait")
    latency_ms: float | None = Field(None, ge=0)
    operation: OperationOutcome | None = Field(
        None, description="The selected host operation's explicit outcome; null for every other turn")

    @model_validator(mode="after")
    def _operation_fields(self) -> "TurnRecord":
        if (self.outcome is TurnOutcome.restarted) != (self.operation is not None):
            raise ValueError("'operation' is required exactly for a restarted turn")
        if self.outcome is TurnOutcome.restarted and self.executed_action is not None:
            raise ValueError("a restarted turn sends no gameplay input")
        return self
