"""Run trace vocabulary: the kinds of entry an append-only trace holds and who writes them (build bible 5.7).

`playtest.core.trace_sink` documents each kind's body. Issue #30 adds the entry model here.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from .common import ClockMode, Strict
from .bridge import ExecutionRecord, Observation, StopResult
from .investigation import Finding, RunRecord, Suspicion
from .loop import ChoiceSet, Decision, TurnRecord


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
    restart = "restart"
    investigation = "investigation"
    turn = "turn"
    stop = "stop"
    end = "end"
    suspicion = "suspicion"
    finding = "finding"


class TraceAuthor(str, Enum):
    harness = "harness"  # the process that opened the trace: run record and end of run
    core = "core"
    bridge = "bridge"
    s1 = "s1"
    frontier = "frontier"
    helper = "helper"


class TraceExecutionRequest(Strict):
    """The bridge request shape recorded inside an execution trace entry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action_id: str = Field(min_length=1)
    choice_id: str
    action: str = Field(min_length=1)
    params: dict[str, Any]
    based_on_seq: int = Field(ge=0)
    max_age_s: float = Field(gt=0)
    depends_on: list[str]
    lease_s: float | None = Field(None, gt=0)


_KEEP_REQUEST_FIELDS = {"turn", "seq", "game_s"}
_KEEP_FIELDS = {"directive_id", "requested", "deadline_game_s", "expires_in_game_s", "adopted_turn",
                "closed_at_game_s", "closed_by_deadline", "request_before_deadline"}


def _finite_number(value: Any, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"a keep row's {name} must be a finite number or null")


def _keep_entry(keep: Any) -> None:
    """Validate a keep row: one adopted answer's measured request beside a directive, unmeasured parts null."""
    if keep is None:
        return
    if not isinstance(keep, dict) or set(keep) != _KEEP_FIELDS:
        raise ValueError(f"investigation keep keys must be exactly {sorted(_KEEP_FIELDS)}")
    if not isinstance(keep["directive_id"], str) or not keep["directive_id"]:
        raise ValueError("a keep row needs the directive id it was asked for")
    requested = keep["requested"]
    if not isinstance(requested, dict) or set(requested) != _KEEP_REQUEST_FIELDS:
        raise ValueError(f"a keep row's requested keys must be exactly {sorted(_KEEP_REQUEST_FIELDS)}")
    for name in ("turn", "seq"):
        value = requested[name]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"a keep row's requested {name} must be a non-negative integer or null")
    _finite_number(requested["game_s"], "requested game_s")
    for name in ("deadline_game_s", "expires_in_game_s", "closed_at_game_s"):
        _finite_number(keep[name], name)
    adopted = keep["adopted_turn"]
    if isinstance(adopted, bool) or not isinstance(adopted, int) or adopted < 0:
        raise ValueError("a keep row's adopted_turn must be a non-negative integer")
    for name in ("closed_by_deadline", "request_before_deadline"):
        if keep[name] is not None and not isinstance(keep[name], bool):
            raise ValueError(f"a keep row's {name} must be a boolean or null")


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
        if self.kind is TraceKind.investigation:
            null_turn = True  # helper results can be appended after their originating turn closes
        if self.kind is TraceKind.help:
            null_turn = self.body.get("event") == "cancelled" and self.turn is None
        # A finding sits in the turn that wrote it, or outside any: a helper's answer can arrive after later turns
        # (as investigation entries do), and the harness writes findings after the run.
        if self.kind is not TraceKind.finding and (self.turn is None) != null_turn:
            raise ValueError("turn is null exactly for run, stop, end, or a stop cancel")
        typed = {TraceKind.run: RunRecord, TraceKind.observation: Observation,
                 TraceKind.choice_set: ChoiceSet, TraceKind.turn: TurnRecord,
                 TraceKind.suspicion: Suspicion, TraceKind.finding: Finding}
        if self.kind in typed:
            record = typed[self.kind].model_validate(self.body)
            if self.kind is TraceKind.suspicion:
                if record.turn != self.turn:
                    raise ValueError("suspicion turn must equal the entry's turn")
                if not set(record.evidence_seq) <= set(self.refs):
                    raise ValueError("a suspicion's evidence_seq must be among the entry's refs")
            if self.kind is TraceKind.finding and not set(record.evidence_seq) <= set(self.refs):
                raise ValueError("a finding's evidence_seq must be among the entry's refs")
        else:
            keys = {
                TraceKind.context: {"sha256", "payload"},
                TraceKind.s1_call: {"question", "provider", "model", "resolved_model", "request_sha256", "http_status", "latency_ms", "problem", "response", "usage"},
                TraceKind.decision: {"answer", "clock_mode", "valid", "error", "policy_default", "decision"},
                TraceKind.execution: {"purpose", "request", "records"},
                TraceKind.effect: {"choice_id", "action", "expected", "observed"},
                TraceKind.help: {"event", "choice_id", "kind", "opened_turn", "source", "result", "reason"},
                TraceKind.restart: {"event", "ticket_id", "from_generation", "to_generation", "status",
                                    "reason", "release_verified", "checks", "waited_s"},
                TraceKind.investigation: {"event", "request_turn", "based_on_seq", "hypothesis", "directive",
                                         "keep", "outcome", "outcome_seq", "evidence_trace_seq", "reason"},
                TraceKind.stop: {"reason", "result", "error"},
                TraceKind.end: {"status", "error", "turns", "redactions"},
            }[self.kind]
            if (self.kind is TraceKind.investigation and "keep" not in self.body
                    and self.body.get("event") != "keep"):
                keys = keys - {"keep"}  # traces written before keep rows existed stay readable
            if set(self.body) != keys:
                raise ValueError(f"{self.kind.value} body keys must be exactly {sorted(keys)}")
            if self.kind is TraceKind.execution and self.body["purpose"] not in ("choice", "while_pending"):
                raise ValueError("execution purpose must be choice or while_pending")
            if self.kind is TraceKind.execution:
                TraceExecutionRequest.model_validate(self.body["request"])
                records = self.body["records"]
                if not isinstance(records, list):
                    raise ValueError("execution records must be a list")
                # Empty only when the act raised after the request may have reached the bridge.
                typed_records = [ExecutionRecord.model_validate(item) for item in records]
                terminal = {"completed", "failed", "cancelled", "timed_out", "rejected"}
                if typed_records and typed_records[-1].status.value not in terminal:
                    raise ValueError("last execution record must have a terminal status")
            if self.kind is TraceKind.decision:
                body = self.body
                if not isinstance(body["valid"], bool) or not isinstance(body["policy_default"], bool):
                    raise ValueError("decision valid and policy_default must be booleans")
                ClockMode(body["clock_mode"])
                decision = Decision.model_validate(body["decision"]) if body["decision"] is not None else None
                if body["valid"] != (decision is not None):
                    raise ValueError("decision valid must be true exactly when decision is non-null")
            if self.kind is TraceKind.s1_call:
                body = self.body
                # Null when the port failed before naming a question.
                question = body["question"]
                if question is not None and (not isinstance(question, str) or not question.strip()):
                    raise ValueError("s1_call question must be a non-empty string or null")
                if body["http_status"] is not None and (isinstance(body["http_status"], bool) or not isinstance(body["http_status"], int)):
                    raise ValueError("s1_call http_status must be an integer or null")
                latency = body["latency_ms"]
                if latency is not None and (isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency < 0):
                    raise ValueError("s1_call latency_ms must be non-negative or null")
            if self.kind is TraceKind.stop and self.body["result"] is not None:
                StopResult.model_validate(self.body["result"])
            if self.kind is TraceKind.help and (isinstance(self.body["opened_turn"], bool) or not isinstance(self.body["opened_turn"], int) or self.body["opened_turn"] < 0):
                raise ValueError("help opened_turn must be a non-negative integer")
            if self.kind is TraceKind.end:
                for key in ("turns", "redactions"):
                    value = self.body[key]
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ValueError(f"end {key} must be a non-negative integer")
            if self.kind is TraceKind.restart:
                from .session_control import is_generation

                body = self.body
                if body["event"] not in ("request", "completed", "failed"):
                    raise ValueError("unknown restart event")
                if not isinstance(body["ticket_id"], str) or not body["ticket_id"].strip():
                    raise ValueError("restart ticket_id must be a non-empty string")
                for name in ("from_generation", "to_generation"):
                    if body[name] is not None and not is_generation(body[name]):
                        raise ValueError(f"restart {name} must be a canonical generation or null")
                if body["status"] not in ("requested", "ready", "failed", "cancelled", "timeout"):
                    raise ValueError("unknown restart status")
                if body["event"] == "request" and body["status"] != "requested":
                    raise ValueError("a restart request must be recorded as requested")
                if body["event"] == "completed" and body["status"] != "ready":
                    raise ValueError("a completed restart must be ready")
                if body["event"] == "failed" and body["status"] == "ready":
                    raise ValueError("a failed restart cannot be ready")
                if body["status"] == "ready" and (body["to_generation"] is None
                                                  or body["to_generation"] == body["from_generation"]):
                    raise ValueError("a ready restart needs a distinct new generation")
                if body["status"] in ("failed", "cancelled", "timeout") and not (
                        isinstance(body["reason"], str) and body["reason"].strip()):
                    raise ValueError("a failed restart needs a non-empty reason")
                release = body["release_verified"]
                if release is not None and not isinstance(release, bool):
                    raise ValueError("restart release_verified must be a boolean or null")
                checks = body["checks"]
                if isinstance(checks, bool) or not isinstance(checks, int) or checks < 0:
                    raise ValueError("restart checks must be a non-negative integer")
                waited = body["waited_s"]
                if waited is not None and (isinstance(waited, bool) or not isinstance(waited, (int, float))
                                           or waited < 0):
                    raise ValueError("restart waited_s must be a non-negative number or null")
            if self.kind is TraceKind.help:
                if self.body["event"] not in ("opened", "repeat", "adopted", "cancelled"):
                    raise ValueError("unknown help event")
                if self.body["kind"] not in ("escalate", "delegate"):
                    raise ValueError("unknown help kind")
            if self.kind is TraceKind.investigation:
                if self.body["event"] not in ("not_called", "invalid_answer", "hypothesis_directive", "directive",
                                              "outcome", "keep"):
                    raise ValueError("unknown investigation event")
                if (isinstance(self.body["request_turn"], bool) or not isinstance(self.body["request_turn"], int)
                        or self.body["request_turn"] < 0):
                    raise ValueError("investigation request_turn must be a non-negative turn number")
                for name in ("based_on_seq", "outcome_seq"):
                    if (isinstance(self.body[name], bool) or not isinstance(self.body[name], int)
                            or self.body[name] < 0):
                        raise ValueError(f"investigation {name} must be a non-negative sequence number")
                if self.body["outcome"] not in ("open", "met", "not_met", "expired", "invalidated", "unknown"):
                    raise ValueError("unknown investigation outcome")
                evidence = self.body["evidence_trace_seq"]
                if evidence is not None and (isinstance(evidence, bool) or not isinstance(evidence, int)
                                             or evidence not in self.refs):
                    raise ValueError("evidence_trace_seq must be null or an earlier cited trace sequence")
                if self.body["reason"] is not None and not isinstance(self.body["reason"], str):
                    raise ValueError("investigation reason must be a string or null")
                _keep_entry(self.body.get("keep"))
            if self.kind is TraceKind.end and self.body["status"] not in ("complete", "stopped", "inconclusive"):
                raise ValueError("unknown end status")
        return self
