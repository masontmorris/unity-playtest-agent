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

Every call that was sent also reports its tokens to an injected meter (`playtest.runner.pacing`
paces the next turn from them): the provider's `usage.input_tokens` plus `usage.output_tokens` when
it gave them, else an estimate from the request body, reported as an estimate.
"""

from __future__ import annotations

import asyncio
import base64
import math
from collections.abc import Mapping
from typing import Any, Protocol

from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.contracts.common import ClockMode
from playtest.core.context import INSTRUCTIONS, canonical
from playtest.core.ports import S1Answer, S1Call

# The default S1 arm since #100 (29 October): Liquid's d1-ultra matched mammoth-1 on both S1 benches and kept every
# live action fresh where Baseten's latency did not (docs/design/s1-model.md). `--s1-arm` picks any other arm.
ARM = "liquid-d1-ultra"
PRIMARY = "next_action"
FOLLOW_UP = "while_pending"
FOLLOW_UP_INSTRUCTIONS = (
    "Help has been requested and is pending. Choose exactly one id from the listed choices to say what "
    "happens to the game's input while it is pending. A wait choice sends no input; any other listed "
    "choice is executed exactly as chosen. Facts are measured; null means unknown, never zero. "
    "When an image is present, it is the current game view."
)
PROMPTS = {PRIMARY: INSTRUCTIONS, FOLLOW_UP: FOLLOW_UP_INSTRUCTIONS}
_HELP_KINDS = ("escalate", "delegate")
_PENDING_KINDS = ("wait", "action", "skill")
# Day 1 exercised the Baseten and d1.1 combinations. d1-ultra took the same `images` array in its
# 30 October probe: a 640x480 JPEG added 450 input tokens, the Day 1 image cost scaled by pixel count.
IMAGE_TRANSPORT = {"mammoth-1": "messages", "elephant-1": "messages", "liquid-d1.1": "images",
                   "liquid-d1-ultra": "images"}
IMAGE_TOKENS = {"messages": 1000, "images": 1400}  # Day 1 1280x720 measurements
CHARS_PER_TOKEN = 2.5  # request characters per token, for the estimate used when a reply reports no usage


class TokenMeter(Protocol):
    def record(self, tokens: int, *, estimated: bool = False) -> None:
        """Count one call's tokens; `estimated` says the provider did not report them."""
        ...


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _tokens(usage: Any) -> int | None:
    """Input plus output tokens when the provider reported input tokens; `None` when it did not."""
    if not isinstance(usage, dict):
        return None
    used = _count(usage.get("input_tokens"))
    if used is None:
        return None
    return used + (_count(usage.get("output_tokens")) or 0)


def _without_image(value: Any, image: bytes | None) -> Any:
    """Do not retain an echoed data URL or encoded image in a provider response trace."""
    if image is None:
        return value
    encoded = base64.b64encode(image).decode("ascii")

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return item.replace("data:image/jpeg;base64," + encoded, "[image redacted]").replace(encoded, "[image redacted]")
        if isinstance(item, list):
            return [clean(part) for part in item]
        if isinstance(item, dict):
            return {clean(key): clean(part) for key, part in item.items()}
        return item

    return clean(value)


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
        budget: TokenMeter | None = None,
        game_view: bool = False,
    ) -> None:
        if arm not in replay.ARMS:
            raise ValueError(f"unknown S1 arm {arm!r}")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError("the call cap must be a positive integer")
        if game_view and arm not in IMAGE_TRANSPORT:
            raise ValueError(f"S1 arm {arm!r} has no verified image transport")
        self._game_view = game_view
        self._view_sent = self._view_omitted = self._view_stale = 0
        self._ledger = ledger
        self._max_calls = max_calls
        self._send = send
        self._arm = arm
        self._clock_mode = clock_mode
        self._episode = episode
        self._budget = budget
        self._turn = 0
        self._reserved = 0

    @property
    def provider(self) -> str:
        return replay.provider_of(self._arm)

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

    @property
    def view_counts(self) -> dict[str, int | bool]:
        """Count calls attempted with or without a view; stale is a subset of omitted."""
        return {"enabled": self._game_view, "sent": self._view_sent,
                "omitted": self._view_omitted, "stale": self._view_stale}

    async def decide(self, context: Mapping[str, Any], *, image: bytes | None = None) -> S1Answer:
        if image is not None and (not self._game_view or digest(image) != context.get("game_view_sha256")
                                  or context.get("game_view_status") != "sent"):
            raise ValueError("image does not match the measured view")
        if self._game_view and ((image is None) != (context.get("game_view_status") != "sent")):
            raise ValueError("image presence does not match the measured view")
        turn, self._turn = self._turn, self._turn + 1
        choices = context["choices"]
        kinds = {item["id"]: item["kind"] for item in choices}
        first = await self._call(PRIMARY, context["instructions"], {item["id"]: item["label"] for item in choices},
                                 context, turn, image)
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
                second = await self._call(FOLLOW_UP, FOLLOW_UP_INSTRUCTIONS, criteria, context, turn, image)
                calls.append(second.call)
                halt = second.halt
                if second.choice is not None:
                    answer["while_pending"] = ({"mode": "wait"} if kinds[second.choice] == "wait"
                                               else {"mode": "continue", "choice_id": second.choice})
        return S1Answer(answer=answer, calls=tuple(calls), halt=halt)

    async def _call(self, question: str, instructions: str, criteria: dict[str, str],
                    context: Mapping[str, Any], turn: int, image: bytes | None) -> _Reply:
        model = self.model
        body = {"model": model,
                "questions": {question: {"type": "choice", "instructions": instructions, "criteria": criteria}},
                "state": context}
        try:
            estimate_text = canonical(body)  # excludes base64; image tokens are counted separately
            if image is not None:
                url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
                if IMAGE_TRANSPORT[self._arm] == "messages":
                    body["state"] = {"messages": [{"role": "user", "content": [
                        {"type": "text", "text": canonical(context)},
                        {"type": "image_url", "image_url": {"url": url}}]}]}
                else:
                    body["images"] = [url]
            request_text = canonical(body)
            request_sha = digest(request_text)
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
        if self._game_view:
            if image is not None:
                self._view_sent += 1
            else:
                self._view_omitted += 1
                self._view_stale += int(context.get("game_view_status") == "stale")
        try:
            status, data, latency_ms, problem = await asyncio.to_thread(self._send, self._arm, body)
        except Exception as exc:  # noqa: BLE001 - keep the reservation: an unknown outcome is never retried
            note = f"adapter error: {type(exc).__name__}"
            self._count_tokens(None, estimate_text, image is not None)  # the request may have reached the provider
            reservation.finish(http_status=None, choice=None, distribution=None, problem=note, latency_ms=None,
                               resolved_model=None, usage=None, cost_usd=None)
            return _Reply(self._record(question, request_sha, None, None, None, note, None), failure=note)
        choice, distribution, answer_problem = replay.parse_answer(
            self._as_next_action(data if status == 200 else None, question), set(criteria))
        usage = _without_image((data or {}).get("usage"), image)
        self._count_tokens(usage, estimate_text, image is not None)
        raw_cost = usage.get("cost") if isinstance(usage, dict) else None
        cost = (float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                and math.isfinite(raw_cost) and raw_cost >= 0 else None)
        note = _without_image(problem or answer_problem, image)
        resolved = _without_image((data or {}).get("model"), image)
        row = reservation.finish(http_status=status, choice=choice, distribution=distribution, problem=note,
                                 latency_ms=latency_ms, resolved_model=resolved, usage=usage, cost_usd=cost)
        call = self._record(question, request_sha, status, latency_ms, resolved, note,
                            _without_image(data, image) if isinstance(data, dict) else None, usage)
        failure = None
        if choice is None:
            failure = (note or "no usable answer") + (f" (HTTP {status})" if status not in (None, 200) else "")
        halt = "a provider-reported cost exceeded the per-call bound" if row["cost_bound_exceeded"] else None
        return _Reply(call, choice, distribution, failure, halt)

    def _count_tokens(self, usage: Any, request_text: str, has_image: bool) -> None:
        """Report what a sent call used: the provider's own count, else an estimate marked as one."""
        if self._budget is None:
            return
        used = _tokens(usage)
        if used is not None:
            self._budget.record(used)
        else:
            image_tokens = IMAGE_TOKENS[IMAGE_TRANSPORT[self._arm]] if has_image else 0
            self._budget.record(math.ceil(len(request_text) / CHARS_PER_TOKEN) + image_tokens, estimated=True)
        reserve = getattr(self._budget, "reserve_next_image", None)
        if reserve is not None and self._game_view:
            reserve(0 if has_image else IMAGE_TOKENS[IMAGE_TRANSPORT[self._arm]])

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
