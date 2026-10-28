"""Run the three model-selection stages without repairing their answers.

The caller supplies the collaborator and persists each attempt. The runner neither performs
transport retries nor creates actions: it accepts a complete, validated stage answer as given,
or records why it was rejected. The same rendered schema is used for the whole conversation.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from playtest.preflight.packet import Packet
from playtest.roles import preflight, strict


StageName = Literal["readers", "actions", "progress"]
STAGES: tuple[StageName, ...] = ("readers", "actions", "progress")
MAX_RETRIES = 3


@dataclass(frozen=True)
class CallResult:
    """A single collaborator call. A transport failure has an error and no content.

    `metadata` is the caller's per-call ledger data, including unknown usage as null. It is
    passed unchanged to the attempt recorder; this module does not estimate missing usage.
    """

    content: str | None
    error: str | None
    metadata: Mapping[str, Any]
    error_kind: Literal["transport", "provider", "refusal", "truncated"] | None = None


@dataclass(frozen=True)
class Attempt:
    stage: StageName
    number: int
    content: str | None
    problems: tuple[str, ...]
    metadata: Mapping[str, Any]
    transport_error: str | None
    failure_kind: Literal["transport", "provider", "refusal", "truncated"] | None = None
    failure: str | None = None


@dataclass(frozen=True)
class StageResult:
    answer: preflight.StageAnswer | None
    attempts: tuple[Attempt, ...]
    skipped_reason: str | None = None


@dataclass(frozen=True)
class RunResult:
    readers: StageResult
    actions: StageResult
    progress: StageResult


def _validate(content: str, stage: StageName, discovery: preflight.Discovery,
              aliases: Mapping[str, str]) -> tuple[preflight.StageAnswer | None, list[str]]:
    parsed, errors = strict.parse(preflight.StageAnswer, content)
    if parsed is None:
        return None, errors
    # The sent schema specifies types. Pydantic's ordinary parser may coerce them, so enforce
    # those types again before semantic review, as the frontier provider does.
    try:
        answer = preflight.StageAnswer.model_validate_json(content, strict=True)
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
    try:
        json.dumps(answer.model_dump(mode="json"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        return None, [f"the answer is not finite JSON: {exc}"]
    problems = preflight.review(answer, stage, discovery, readers=aliases)
    return (answer if not problems else None), problems


def run(
    packet: Packet,
    call: Callable[[list[dict[str, str]], dict[str, Any]], CallResult],
    record: Callable[[Attempt], None],
    *,
    max_retries: int = MAX_RETRIES,
    extra_review: Callable[[preflight.StageAnswer, StageName], list[str]] | None = None,
) -> RunResult:
    """Call readers, actions, and progress with up to three validation retries per stage.

    The recorder is called before another request is made. If it fails, the run stops rather
    than spend without a ledger. A transport error stops the chain; it is not a validation
    error and cannot trigger an automatic retry. Progress may still run after exhausted action
    validation because it depends on accepted readers, not on the action answer.
    """
    if not isinstance(packet, Packet) or not packet.text.strip():
        raise ValueError("a non-empty preflight Packet is required")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= MAX_RETRIES:
        raise ValueError("max_retries must be an integer between 0 and 3")
    discovery = packet.discovery
    schema = preflight.schema(discovery)
    messages = [{"role": "system", "content": preflight.SYSTEM},
                {"role": "user", "content": preflight.first_turn(packet.text, "readers")}]
    results: dict[StageName, StageResult] = {}
    aliases: dict[str, str] = {}
    stop_reason: str | None = None

    for stage in STAGES:
        if stop_reason is not None:
            results[stage] = StageResult(None, (), stop_reason)
            continue
        if stage != "readers":
            instruction = packet.action_prompt if stage == "actions" else preflight.STAGE_PROMPTS[stage]
            messages.append({"role": "user", "content": instruction})
        attempts: list[Attempt] = []
        accepted: preflight.StageAnswer | None = None
        for index in range(max_retries + 1):
            # A collaborator may mutate its arguments; it cannot alter the next request or schema.
            reply = call(copy.deepcopy(messages), copy.deepcopy(schema))
            if not isinstance(reply, CallResult):
                raise TypeError("the collaborator must return CallResult")
            if reply.error is not None:
                if reply.content is not None:
                    raise ValueError("a transport error cannot also contain an answer")
                problems: list[str] = []
            else:
                content = reply.content if isinstance(reply.content, str) else ""
                accepted, problems = _validate(content, stage, discovery, aliases)
                if accepted is not None:
                    problems = packet.review_bridge(accepted, stage)
                    if extra_review is not None:
                        problems += extra_review(accepted, stage)
                    if problems:
                        accepted = None
            failure_kind = (reply.error_kind or "transport") if reply.error is not None else None
            attempt = Attempt(stage, index + 1, reply.content, tuple(problems), copy.deepcopy(reply.metadata),
                              reply.error if failure_kind == "transport" else None, failure_kind, reply.error)
            record(attempt)
            attempts.append(attempt)
            if reply.error is not None:
                stop_reason = f"{stage} {failure_kind} failed"
                break
            # Keep the exact received answer, including rejected answers, in the conversation.
            messages.append({"role": "assistant", "content": reply.content if reply.content is not None else ""})
            if accepted is not None:
                break
            if index < max_retries:
                messages.append({"role": "user", "content": preflight.retry_turn(stage, problems)})
        results[stage] = StageResult(accepted, tuple(attempts))
        if stage == "readers":
            if accepted is None:
                stop_reason = stop_reason or "readers were not accepted"
            else:
                aliases = accepted.aliases()
        elif stage == "actions" and accepted is None and stop_reason is None:
            messages.append({"role": "user", "content": "The actions stage failed validation; no actions were accepted."})
    return RunResult(results["readers"], results["actions"], results["progress"])
