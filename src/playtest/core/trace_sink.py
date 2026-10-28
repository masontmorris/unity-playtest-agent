"""Where the turn loop writes its trace, and what it writes.

`TraceSink.append` adds one entry and returns its `trace_seq`. Sequence numbers start at 0 and are
contiguous. An entry never changes once appended, and `refs` cite earlier entries by `trace_seq`.
A file-backed sink (#30) adds `run_id` and opens with the `run` entry; `MemorySink` keeps entries in
a list for tests.

This vocabulary is the contract between the turn loop (#29) and the trace writer and replay (#30).
Bodies are JSON-ready (`model_dump(mode="json")`). A value nobody measured is null, never zero.

Every entry of a turn carries that turn's number. Within a turn the order is as listed, except that
`help` entries sit where their event happens:

- `observation` (bridge; refs none): the `Observation` the turn decided on.
- `choice_set` (core; refs the observation): the `ChoiceSet`; `built_from_seq` is that
  observation's `seq`.
- `context` (core; refs the observation and the choice set):
  `{"sha256": <sha256 hex of canonical(payload)>, "payload": <the exact build_context dict sent to S1>}`.
- `s1_call` (s1; refs the context): one per provider call, so a help turn may add a follow-up:
  `{"question", "provider", "model", "resolved_model", "request_sha256", "http_status",
  "latency_ms", "problem", "response", "usage"}`. `response` is the provider's parsed reply body;
  request headers and credentials are never part of an entry. `question` is null when the port
  failed before naming one.
- `decision` (core; refs the choice set and every `s1_call` of the turn):
  `{"answer": <the exact object passed to parse_decision, or null>, "clock_mode", "valid", "error",
  "policy_default", "decision": <Decision or null>}`.
- `execution` (bridge; refs the decision): zero, one or two per turn:
  `{"purpose": "choice" | "while_pending", "request": {"action_id", "choice_id", "action", "params",
  "based_on_seq", "max_age_s", "depends_on", "lease_s"}, "records": [<every ExecutionRecord the
  bridge returned, terminal last>]}`. `records` is empty only when the act raised before any came
  back; the turn then has no `turn` entry.
- `effect` (core; refs the execution and any measurement it cites): one per execution whose effect
  was measured, after that execution and before the turn entry:
  `{"choice_id", "action", "expected": <object or null>, "observed": <object or null>}`.
- `help` (core): `{"event": "opened" | "repeat" | "adopted" | "cancelled", "choice_id", "kind":
  "escalate" | "delegate", "opened_turn", "source", "result", "reason"}`. `opened` and `repeat`
  follow the decision and ref it; `adopted` comes first in the turn that adopts the result, before
  its observation; `adopted` and `cancelled` ref the `opened` entry. A cancel at stop has no turn.
- `turn` (core; refs the observation, choice set, context, decision and executions): the `TurnRecord`.

Outside a turn (turn null):

- `run` (harness): the `RunRecord`. Always `trace_seq` 0, written before the first turn.
- `stop` (core): `{"reason", "result": <StopResult or null>, "error"}`.
- `end` (harness): `{"status": "complete" | "stopped" | "inconclusive", "error", "turns",
  "redactions"}`; last. `redactions` counts secret values the writer replaced.

A replay (#30) re-derives every turn from these entries and fails on any difference:

- `trace_seq` is contiguous from 0, entry 0 is `run`, and every ref points at an earlier entry.
- The choice set is bound to its observation, and `set_version` increases from turn to turn.
- The context hash matches its payload; the payload's `as_of.seq`, `choice_set_version` and
  `choices` match the turn's observation and set; the payload has a `pending_help` key.
- `parse_decision(answer, choice_set, clock_mode)` reproduces `valid`, `error`, `policy_default`
  and `decision`.
- The turn record agrees with those entries: `set_version`, `based_on_seq`, `raw_choice` (the
  answer's `choice_id` when it is a string), `effective_choice`, `executed_action` (the choice id
  of the execution the bridge did not reject) and `outcome`.
- `check_adherence` over all turn records finds nothing.
- At most one help request is open at a time, and each is adopted or cancelled at most once.
- A turn whose entries stop before its `turn` entry is reported, not skipped.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol

from playtest.contracts.trace import TraceAuthor, TraceKind


class TraceSink(Protocol):
    def append(
        self,
        kind: TraceKind,
        body: Mapping[str, Any],
        *,
        author: TraceAuthor,
        turn: int | None = None,
        refs: Sequence[int] = (),
    ) -> int:
        """Append one entry and return its `trace_seq`."""
        ...


class MemorySink:
    """Keeps entries in a list. It checks what any sink must refuse but writes no file."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def append(
        self,
        kind: TraceKind | str,
        body: Mapping[str, Any],
        *,
        author: TraceAuthor | str,
        turn: int | None = None,
        refs: Sequence[int] = (),
    ) -> int:
        seq = len(self.entries)
        if turn is not None and (isinstance(turn, bool) or not isinstance(turn, int) or turn < 0):
            raise ValueError(f"turn must be a non-negative integer or None, not {turn!r}")
        cited = list(refs)
        if any(isinstance(r, bool) or not isinstance(r, int) or not 0 <= r < seq for r in cited):
            raise ValueError(f"refs must cite earlier entries (0..{seq - 1}), not {cited!r}")
        self.entries.append({
            "trace_seq": seq,
            "kind": TraceKind(kind).value,
            "author": TraceAuthor(author).value,
            "turn": turn,
            "refs": cited,
            "wall_utc": datetime.now(timezone.utc).isoformat(),
            "body": copy.deepcopy(dict(body)),
        })
        return seq

    def of_kind(self, kind: TraceKind | str) -> list[dict[str, Any]]:
        name = TraceKind(kind).value
        return [entry for entry in self.entries if entry["kind"] == name]
