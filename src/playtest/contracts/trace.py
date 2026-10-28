"""Run trace vocabulary: the kinds of entry an append-only trace holds and who writes them (build bible 5.7).

`playtest.core.trace_sink` documents each kind's body. Issue #30 adds the entry model here.
"""

from __future__ import annotations

from enum import Enum
from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from .common import Strict
from .bridge import Observation
from .investigation import RunRecord
from .loop import ChoiceSet, TurnRecord


class TraceKind(str, Enum):
    run = "run"
    observation = "observation"
    choice_set = "choice_set"
    context = "context"
    s1_call = "s1_call"
    decision = "decision"
    execution = "execution"
    effect = "effect"
    help = "help"
    turn = "turn"
    stop = "stop"
    end = "end"


class TraceAuthor(str, Enum):
    harness = "harness"  # the process that opened the trace: run record and end of run
    core = "core"
    bridge = "bridge"
    s1 = "s1"
    frontier = "frontier"
    helper = "helper"


class TraceEntry(Strict):
    """One immutable, locally validated append in a run trace (build bible 5.7)."""

    trace_seq: int = Field(ge=0)
    run_id: str = Field(min_length=1)
    kind: TraceKind
    author: TraceAuthor
    turn: int | None = Field(None, ge=0)
    refs: list[int]
    wall_utc: datetime
    body: dict[str, Any]

    @model_validator(mode="after")
    def _entry_consistency(self) -> "TraceEntry":
        if any(ref >= self.trace_seq for ref in self.refs):
            raise ValueError("refs must point to earlier entries")
        if any(isinstance(ref, bool) for ref in self.refs):
            raise ValueError("refs must be integer sequence ids")
        null_turn = self.kind in (TraceKind.run, TraceKind.stop, TraceKind.end)
        if self.kind is TraceKind.help:
            null_turn = self.body.get("event") == "cancelled" and self.turn is None
        if (self.turn is None) != null_turn:
            raise ValueError("turn is null exactly for run, stop, end, or a stop cancel")
        typed = {TraceKind.run: RunRecord, TraceKind.observation: Observation,
                 TraceKind.choice_set: ChoiceSet, TraceKind.turn: TurnRecord}
        if self.kind in typed:
            typed[self.kind].model_validate(self.body)
        else:
            keys = {
                TraceKind.context: {"sha256", "payload"},
                TraceKind.s1_call: {"question", "provider", "model", "resolved_model", "request_sha256", "http_status", "latency_ms", "problem", "response", "usage"},
                TraceKind.decision: {"answer", "clock_mode", "valid", "error", "policy_default", "decision"},
                TraceKind.execution: {"purpose", "request", "records"},
                TraceKind.effect: {"choice_id", "action", "expected", "observed"},
                TraceKind.help: {"event", "choice_id", "kind", "opened_turn", "source", "result", "reason"},
                TraceKind.stop: {"reason", "result", "error"},
                TraceKind.end: {"status", "error", "turns", "redactions"},
            }[self.kind]
            if set(self.body) != keys:
                raise ValueError(f"{self.kind.value} body keys must be exactly {sorted(keys)}")
            if self.kind is TraceKind.execution and self.body["purpose"] not in ("choice", "while_pending"):
                raise ValueError("execution purpose must be choice or while_pending")
            if self.kind is TraceKind.help:
                if self.body["event"] not in ("opened", "repeat", "adopted", "cancelled"):
                    raise ValueError("unknown help event")
                if self.body["kind"] not in ("escalate", "delegate"):
                    raise ValueError("unknown help kind")
            if self.kind is TraceKind.end and self.body["status"] not in ("complete", "stopped", "inconclusive"):
                raise ValueError("unknown end status")
        return self
