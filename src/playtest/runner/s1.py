"""S1 on Baseten System One: the provider adapter for `mammoth-1`.

The request is the shape proven live: one `choice` question, `next_action`, whose criteria are the
offered choices, and the context payload as `state`. The API answers one choice per question, so when
that answer names an escalate or delegate choice in real time the adapter makes ONE follow-up call in
the same shape, with the question `while_pending` over the wait choice and the executable (action and
skill) choices, and the same state. Its answer becomes the `while_pending` of S1's decision; a failed or
invalid follow-up leaves it absent and the loop applies the recorded policy default of wait.

Every call reserves a ledger row before it is sent (`playtest.bench.call_budget`), and the blocking
HTTP call runs in a worker thread so the bridge client keeps reading on the event loop. The adapter
holds no policy: it never retries, repairs, ranks or substitutes, and the returned distribution is
recorded and never used to select.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from typing import Any

from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.contracts.common import ClockMode
from playtest.core.context import INSTRUCTIONS, canonical
from playtest.core.ports import S1Answer, S1Call

ARM = "mammoth-1"
PRIMARY = "next_action"
FOLLOW_UP = "while_pending"
FOLLOW_UP_INSTRUCTIONS = (
    "Help has been requested and is pending. Choose exactly one id from the listed choices to say what "
    "happens to the game's input while it is pending. A wait choice sends no input; any other listed "
    "choice is executed exactly as chosen. Facts are measured; null means unknown, never zero."
)
PROMPTS = {PRIMARY: INSTRUCTIONS, FOLLOW_UP: FOLLOW_UP_INSTRUCTIONS}
_PROVIDERS = {"liquid-d1.1": "liquid"}
_HELP_KINDS = ("escalate", "delegate")
_PENDING_KINDS = ("wait", "action", "skill")


class _Reply:
    """One call's outcome: its `s1_call` record, the choice it named, and why there is none."""

    __slots__ = ("call", "choice", "distribution", "failure", "halt")

    def __init__(self, call: S1Call, choice: str | None = None, distribution: Any = None,
                 failure: str | None = None, halt: str | None = None) -> None:
        self.call, self.choice, self.distribution, self.failure, self.halt = call, choice, distribution, failure, halt


class SystemOneS1:
    def __init__(
        self,
        ledger: call_budget.Ledger,
        *,
        max_calls: int,
        send: replay.Send = replay.send_systemone,
        arm: str = ARM,
        clock_mode: ClockMode = ClockMode.realtime,
        episode: int = 1,
    ) -> None:
        if arm not in replay.ARMS:
            raise ValueError(f"unknown S1 arm {arm!r}")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError("the call cap must be a positive integer")
        self._ledger = ledger
        self._max_calls = max_calls
        self._send = send
        self._arm = arm
        self._clock_mode = clock_mode
        self._episode = episode
        self._turn = 0
        self._reserved = 0

    @property
    def provider(self) -> str:
        return _PROVIDERS.get(self._arm, "baseten")

    @property
    def model(self) -> str:
        return replay.ARMS[self._arm][0]

    @property
    def prompt_sha256(self) -> str:
        """SHA-256 of every prompt text this adapter can send: the context instructions and the follow-up wording."""
        return digest(canonical(PROMPTS))

    @property
    def calls_reserved(self) -> int:
        return self._reserved

    async def decide(self, context: Mapping[str, Any]) -> S1Answer:
        turn, self._turn = self._turn, self._turn + 1
        choices = context["choices"]
        kinds = {item["id"]: item["kind"] for item in choices}
        first = await self._call(PRIMARY, context["instructions"], {item["id"]: item["label"] for item in choices},
                                 context, turn)
        calls = [first.call]
        if first.choice is None:
            return S1Answer(answer=None, calls=tuple(calls), failure=first.failure, halt=first.halt)
        answer: dict[str, Any] = {"choice_id": first.choice, "set_version": context["choice_set_version"]}
        if first.distribution is not None:
            answer["distribution"] = first.distribution
        halt = first.halt
        if kinds[first.choice] in _HELP_KINDS and self._clock_mode is ClockMode.realtime and halt is None:
            criteria = {item["id"]: item["label"] for item in choices if item["kind"] in _PENDING_KINDS}
            if criteria:
                second = await self._call(FOLLOW_UP, FOLLOW_UP_INSTRUCTIONS, criteria, context, turn)
                calls.append(second.call)
                halt = second.halt
                if second.choice is not None:
                    answer["while_pending"] = ({"mode": "wait"} if kinds[second.choice] == "wait"
                                               else {"mode": "continue", "choice_id": second.choice})
        return S1Answer(answer=answer, calls=tuple(calls), halt=halt)

    async def _call(self, question: str, instructions: str, criteria: dict[str, str],
                    context: Mapping[str, Any], turn: int) -> _Reply:
        model = self.model
        body = {"model": model,
                "questions": {question: {"type": "choice", "instructions": instructions, "criteria": criteria}},
                "state": context}
        try:
            request_sha = digest(canonical(body))
        except ValueError:
            return self._refused(question, None, "the request is not finite JSON; no call was made", halt=None)
        if self._reserved >= self._max_calls:
            return self._refused(question, request_sha, "the run's call cap was reached; no call was made",
                                 halt="the run's call cap was reached")
        try:
            reservation = self._ledger.reserve(
                episode=self._episode, turn=turn, based_on_seq=context["as_of"]["seq"],
                request_sha256=request_sha, requested_model=model, extra={"question": question})
        except call_budget.BudgetStop as exc:
            return self._refused(question, request_sha, f"{exc}; no call was made", halt=str(exc))
        self._reserved += 1
        try:
            status, data, latency_ms, problem = await asyncio.to_thread(self._send, self._arm, body)
        except Exception as exc:  # noqa: BLE001 - keep the reservation: an unknown outcome is never retried
            note = f"adapter error: {type(exc).__name__}"
            reservation.finish(http_status=None, choice=None, distribution=None, problem=note, latency_ms=None,
                               resolved_model=None, usage=None, cost_usd=None)
            return _Reply(self._record(question, request_sha, None, None, None, note, None), failure=note)
        choice, distribution, answer_problem = replay.parse_answer(
            self._as_next_action(data if status == 200 else None, question), set(criteria))
        usage = (data or {}).get("usage")
        raw_cost = usage.get("cost") if isinstance(usage, dict) else None
        cost = (float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                and math.isfinite(raw_cost) and raw_cost >= 0 else None)
        note = problem or answer_problem
        resolved = (data or {}).get("model")
        row = reservation.finish(http_status=status, choice=choice, distribution=distribution, problem=note,
                                 latency_ms=latency_ms, resolved_model=resolved, usage=usage, cost_usd=cost)
        call = self._record(question, request_sha, status, latency_ms, resolved, note,
                            data if isinstance(data, dict) else None, usage)
        failure = None
        if choice is None:
            failure = (note or "no usable answer") + (f" (HTTP {status})" if status not in (None, 200) else "")
        halt = "a provider-reported cost exceeded the per-call bound" if row["cost_bound_exceeded"] else None
        return _Reply(call, choice, distribution, failure, halt)

    @staticmethod
    def _as_next_action(data: dict[str, Any] | None, question: str) -> dict[str, Any] | None:
        """`replay.parse_answer` reads the `next_action` answer; present another question's answer under that name."""
        if question == PRIMARY:
            return data
        answers = (data or {}).get("answers")
        return {"answers": {PRIMARY: answers.get(question) if isinstance(answers, dict) else None}}

    def _refused(self, question: str, request_sha: str | None, note: str, *, halt: str | None) -> _Reply:
        return _Reply(self._record(question, request_sha, None, None, None, note, None), failure=note, halt=halt)

    def _record(self, question: str, request_sha: str | None, status: int | None, latency_ms: float | None,
                resolved: Any, problem: str | None, response: Any, usage: Any = None) -> S1Call:
        return S1Call(question=question, provider=self.provider, model=self.model,
                      resolved_model=resolved if isinstance(resolved, str) else None, request_sha256=request_sha,
                      http_status=status, latency_ms=latency_ms, problem=problem, response=response, usage=usage)
