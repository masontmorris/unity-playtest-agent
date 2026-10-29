"""Fakes for the turn loop tests: scripted ports and the choice list every scenario shares.

The fake game is a protocol peer (`fake_game.py`); everything here stands in for a provider, a helper
or a probe. No network, no Unity, no key.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any, Callable

from fake_game import FakeGame
from playtest.contracts.bridge import ExecStatus, InspectionCap, InspectResult
from playtest.contracts.loop import Choice, ChoiceSet
from playtest.contracts.trace import TraceAuthor, TraceKind
from playtest.core.bridge_client import BridgeClient
from playtest.core.execution import Execution
from playtest.core.ports import EffectReport, HelpRequest, HelpResult, S1Answer, S1Call
from playtest.core.trace_sink import MemorySink
from playtest.core.turn_loop import TurnLoop

TOKEN = "turn-loop-test-token"


def choices() -> list[Choice]:
    return [
        Choice(id="c1", kind="action", label="Shift", action="shift",
               params={"delta": [1.0, 0.0, 0.0]}, source="manifest"),
        Choice(id="c2", kind="action", label="Increment", action="increment", source="manifest"),
        Choice(id="c3", kind="action", label="Hold", action="hold",
               params={"tag": "primary"}, source="manifest"),
        Choice(id="h", kind="escalate", label="Escalate", role="frontier", source="core"),
        Choice(id="d", kind="delegate", label="Delegate", role="helper", source="core"),
        Choice(id="w", kind="wait", label="Wait", source="core"),
        Choice(id="f", kind="finish", label="Finish", source="core"),
    ]


class Raw:
    """An answer the provider returned that is passed to the loop exactly as given."""

    def __init__(self, value: Any) -> None:
        self.value = value


def pick(choice_id: str, **extra: Any) -> dict[str, Any]:
    return {"choice_id": choice_id, **extra}


class Sleeps:
    """A `sleep` that records what it was asked for and returns at once, so no test really waits."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FakeTime:
    """A clock the test moves. Its `sleep` records the request and moves the clock by exactly that much."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def fail(problem: str) -> S1Answer:
    return S1Answer(answer=None, calls=(S1Call(question="next_action", provider="fake", problem=problem),),
                    failure=problem)


class FakeS1:
    """A scripted S1 port. Each step is a choice id, an answer dict, `Raw`, an `S1Answer`, or a callable of the context."""

    def __init__(self, *steps: Any) -> None:
        self._steps = deque(steps)
        self.contexts: list[dict[str, Any]] = []

    async def decide(self, context: dict[str, Any]) -> S1Answer:
        self.contexts.append(context)
        step = self._steps.popleft()
        value = step(context) if callable(step) else step
        if isinstance(value, S1Answer):
            return value
        if isinstance(value, Raw):
            answer = value.value
        elif isinstance(value, str):
            answer = {"choice_id": value, "set_version": context["choice_set_version"]}
        else:
            answer = dict(value)
            answer.setdefault("set_version", context["choice_set_version"])
        call = S1Call(question="next_action", provider="fake", model="fake-1", resolved_model="fake-1.0",
                      request_sha256="0" * 64, http_status=200, latency_ms=1.5, problem=None,
                      response={"echo": answer}, usage=None)
        return S1Answer(answer=answer, calls=(call,))


class FakeHelp:
    """A helper that answers when the test releases it, and notes if it was cancelled."""

    def __init__(self, result: Any = None, source: str | None = "fake-helper") -> None:
        self.result = {"advice": "try the other control"} if result is None else result
        self.source = source
        self.requests: list[HelpRequest] = []
        self.release = asyncio.Event()
        self.cancelled = False

    async def run(self, request: HelpRequest) -> HelpResult:
        self.requests.append(request)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return HelpResult(source=self.source, result=self.result)


class FakeProbe:
    """An effect probe that reports a fixed effect for every executed input."""

    def __init__(self, sink: MemorySink | None = None) -> None:
        self._sink = sink
        self.turns: list[int] = []
        self.entries_before_turn: list[int] = []
        self.measured: list[str] = []

    async def before_turn(self, turn: int) -> None:
        self.turns.append(turn)
        self.entries_before_turn.append(len(self._sink.entries) if self._sink is not None else -1)

    async def measure(self, choice: Choice, execution: Execution) -> EffectReport:
        self.measured.append(choice.id)
        return EffectReport(expected={"net": 6.0}, observed={"net": 6.1},
                            fact=f"{choice.id}: net 6.1 against a calibrated 6.0.")


class Rig:
    """A fake game, a connected client, a sink that starts with the harness `run` entry, and a loop on them."""

    def __init__(self, game: FakeGame, client: BridgeClient, sink: MemorySink, loop: TurnLoop, s1: FakeS1) -> None:
        self.game, self.client, self.sink, self.loop, self.s1 = game, client, sink, loop, s1

    async def turns(self, count: int) -> None:
        for _ in range(count):
            await self.loop.run_turn()

    async def close(self) -> None:
        try:
            await self.client.aclose()
        finally:
            await self.game.stop()

    def kinds(self, turn: int) -> list[str]:
        return [e["kind"] for e in self.sink.entries if e["turn"] == turn]

    def helps(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.sink.of_kind("help") if e["body"]["event"] == event]


async def rig(*steps: Any, game: dict[str, Any] | None = None, probe: bool = False,
              game_class: type[FakeGame] = FakeGame, **loop_kwargs: Any) -> Rig:
    fake = game_class(token=TOKEN, **(game or {}))
    port = await fake.start()
    client = BridgeClient("127.0.0.1", port, TOKEN)
    await client.connect()
    sink = MemorySink()
    sink.append(TraceKind.run, {"run_id": "test"}, author=TraceAuthor.harness)
    s1 = FakeS1(*steps)
    if probe:
        loop_kwargs["effects"] = FakeProbe(sink)
    loop = TurnLoop(client, choices=loop_kwargs.pop("choices", choices()), objective="Exercise the controls.",
                    s1=s1, sink=sink, **loop_kwargs)
    return Rig(fake, client, sink, loop, s1)


def run(coro_fn: Callable[[], Any]) -> None:
    asyncio.run(coro_fn())


def choice_set_of(entry: dict[str, Any]) -> ChoiceSet:
    return ChoiceSet.model_validate(entry["body"])


class TransformsGame(FakeGame):
    """A fake game that also answers `inspect("transforms")` with the entity's position.

    Its `increment` action moves the entity 6.0 units along x, so a probe reading before and after
    measures a displacement the way the live smoke did.
    """

    def field_rows(self) -> list[dict[str, Any]]:
        """The field rows `inspect("fields")` answers with: the one reader the shared manifest fixture declares,
        valued at the entity's x so it changes when the entity moves. Tests replace this to script other values."""
        return [{"id": "field", "scene_id": "scene", "field_type": "System.Single", "value_kind": "number",
                 "value_state": "present", "value": self._pos[0], "is_null": False, "value_truncated": None,
                 "read_error": None}]

    def _inspection(self, request, seq, session):
        if request.stage == "fields":
            rows = self.field_rows()
            return InspectResult(
                **self._envelope(seq, session), type="inspection", stage="fields", in_reply_to=request.seq,
                data={"time_unit": "seconds", "frame": self._frame, "real_time_s": time.monotonic(),
                      "game_time_s": self._game_s(), "fields": rows},
                caps={"fields": InspectionCap(limit=64, observed=len(rows), returned=len(rows), truncated=False),
                      "game_objects": InspectionCap(limit=64, observed=1, returned=1, truncated=False)}, errors=[])
        if request.stage != "transforms":
            return super()._inspection(request, seq, session)
        return InspectResult(
            **self._envelope(seq, session), type="inspection", stage="transforms", in_reply_to=request.seq,
            data={"position_space": "world", "position_unit": "unity_world_unit", "position_order": ["x", "y", "z"],
                  "time_unit": "seconds", "frame": self._frame, "real_time_s": time.monotonic(),
                  "game_time_s": self._game_s(),
                  "game_objects": [{"id": "entity", "parent_id": None, "position": list(self._pos)}]},
            caps={"game_objects": InspectionCap(limit=64, observed=1, returned=1, truncated=False)}, errors=[])

    def _apply(self, request, seq, session):
        record = super()._apply(request, seq, session)
        if request.action == "increment" and record.status is ExecStatus.completed:
            self._pos[0] += 6.0
        return record


class GameThread:
    """A fake game on its own event loop in a thread, so synchronous code (the CLI) can connect to it."""

    def __init__(self, game_class: type[FakeGame] = FakeGame, **kwargs: Any) -> None:
        self._game = game_class(token=TOKEN, **kwargs)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self.port = 0

    @property
    def game(self) -> FakeGame:
        return self._game

    def __enter__(self) -> "GameThread":
        self._thread.start()
        self.port = asyncio.run_coroutine_threadsafe(self._game.start(), self._loop).result(10)
        return self

    def __exit__(self, *exc_info: object) -> None:
        asyncio.run_coroutine_threadsafe(self._game.stop(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
        self._loop.close()
