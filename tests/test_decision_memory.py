"""S1 receives bounded, independently copied history, including negative evidence."""

from __future__ import annotations

import asyncio
import copy
import json
from collections import deque
from datetime import datetime, timezone

import pytest

from playtest.contracts.bridge import Effect, ExecutionRecord, Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice
from playtest.core.context import canonical
from playtest.core.ports import EffectReport, S1Answer
from playtest.core.trace_sink import MemorySink
from playtest.core.turn_loop import TurnLoop

WALL = datetime(2026, 1, 1, tzinfo=timezone.utc)


def observation(seq: int, *, session: str = "s0", scene: str = "s0", frame: int | None = None,
                game_s: float | None = None) -> Observation:
    return Observation(session=session, seq=seq, game_s=float(seq) if game_s is None else game_s,
                       wall_utc=WALL, clock_mode=ClockMode.realtime, scene=scene, playing=True,
                       paused=False, time_scale=1.0, frame=seq if frame is None else frame,
                       public={"sample": ["large observation, not history"]})


def choices() -> list[Choice]:
    return [
        Choice(id="a", kind="action", label="A", action="op_a", source="manifest"),
        Choice(id="b", kind="action", label="B", action="op_b", source="manifest"),
        Choice(id="h", kind="escalate", label="H", role="frontier", source="core"),
        Choice(id="w", kind="wait", label="W", source="core"),
    ]


class Bridge:
    def __init__(self, observations: list[Observation], statuses: tuple[str, ...] = ()) -> None:
        self.observations = deque(observations)
        self.statuses = deque(statuses)
        self.requests: list[dict] = []

    async def observe_once(self) -> Observation:
        return self.observations.popleft()

    async def act(self, **request):
        self.requests.append(request)
        status = self.statuses.popleft() if self.statuses else "completed"
        rejected = status == "rejected"
        return [ExecutionRecord(session="s0", seq=500 + len(self.requests), game_s=None, wall_utc=WALL,
                                action_id=request["action_id"], status=status,
                                reject_reason="stale_age" if rejected else None,
                                applied_duration_s=0.25 if len(self.requests) == 1 and not rejected else None,
                                release_verified=True if len(self.requests) == 1 and not rejected else None,
                                expected_effect=[Effect(field="opaque", delta=2.0)] if len(self.requests) == 1
                                and not rejected else [])]

    async def stop(self, reason):
        return None


class S1:
    def __init__(self, steps):
        self.steps = deque(steps)
        self.contexts: list[dict] = []

    async def decide(self, context):
        # Exercise the same JSON serialization S1 receives, not an internal memory view.
        self.contexts.append(json.loads(canonical(context)))
        step = self.steps.popleft()
        answer = {"choice_id": step} if isinstance(step, str) else dict(step)
        answer["set_version"] = context["choice_set_version"]
        return S1Answer(answer=answer)


class Probe:
    def __init__(self):
        self.expected = {"metric": {"delta": 2.0}}
        self.observed = {"metric": {"delta": 0.1}}
        self.calls = 0

    async def before_turn(self, turn):
        pass

    async def measure(self, choice, execution):
        self.calls += 1
        if self.calls != 1:
            return None
        return EffectReport(expected=self.expected, observed=self.observed, fact="Observed 0.1 against 2.0.")


class Facts:
    async def facts(self, reports, observation):
        return ["Unresolved hypothesis: evidence pending."]


def make_loop(steps, observations=None, *, statuses=(), sink=None, effects=None, facts=None):
    if observations is None:
        observations = [observation(100 + i) for i in range(len(steps))]
    bridge = Bridge(observations, statuses)
    s1 = S1(steps)
    loop = TurnLoop(bridge, choices=choices(), objective="Inspect.", s1=s1, sink=sink,
                    effects=effects, facts=facts, clock=lambda: WALL)
    return loop, bridge, s1


def test_serialized_history_keeps_action_across_waits_and_preserves_rejections_and_pending():
    async def scenario():
        steps = ["a", {"choice_id": "h", "while_pending": {"mode": "continue", "choice_id": "b"}},
                 "not_offered", "a", *["w"] * 9]
        sink, probe = MemorySink(), Probe()
        loop, bridge, s1 = make_loop(steps, statuses=("completed", "completed", "rejected"),
                                     sink=sink, effects=probe, facts=Facts())
        await loop.run_turn()
        await loop.run_turn()
        assert loop.resolve_helper({"hypothesis": "still unresolved"}, source="test")
        await loop.run_turn()
        early = copy.deepcopy(s1.contexts[2]["recent_history"])
        # Both the probe's objects and a previously delivered payload are mutable.
        probe.expected["metric"]["delta"] = 99
        s1.contexts[2]["recent_history"]["action_attempts"]["items"][0]["observed_effect"].clear()
        for _ in range(10):
            await loop.run_turn()

        assert "recent_history" not in s1.contexts[0]
        current = s1.contexts[-1]
        history = current["recent_history"]
        turns, attempts = history["turns"], history["action_attempts"]
        assert (turns["limit"], turns["total"], turns["omitted"]) == (8, 12, 4)
        assert [row["turn"] for row in turns["items"]] == list(range(4, 12))
        assert (attempts["limit"], attempts["total"], attempts["omitted"]) == (8, 3, 0)
        assert [row["turn"] for row in attempts["items"]] == [0, 1, 3]
        first, pending, rejected = attempts["items"]
        assert (first["choice_id"], first["purpose"], first["terminal_status"]) == ("a", "choice", "completed")
        assert first["expected_effect"] == {"metric": {"delta": 2.0}}
        assert first["observed_effect"] == {"metric": {"delta": 0.1}}
        assert first["effect_fact"] == "Observed 0.1 against 2.0."
        assert first["applied_duration_s"] == 0.25 and first["release_verified"] is True
        assert first["bridge_expected_effects"][0]["field"] == "opaque"
        assert first["bridge_observed_effects"] is None
        assert (pending["raw_choice"], pending["effective_choice"], pending["executed_action"],
                pending["choice_id"], pending["purpose"], pending["outcome"], pending["help_event"]) == (
                    "h", "h", "b", "b", "while_pending", "escalated", "opened")
        assert pending["expected_effect"] is None and pending["observed_effect"] is None
        assert pending["effect_fact"] is None and pending["effect_trace_seq"] is None
        assert pending["applied_duration_s"] is None and pending["release_verified"] is None
        assert rejected["terminal_status"] == "rejected" and rejected["reject_reason"] == "stale_age"
        assert rejected["executed_action"] is None and rejected["outcome"] == "rejected_stale"
        assert rejected["expected_effect"] is None and rejected["release_verified"] is None
        invalid = early["turns"]["items"]  # the invalid turn has not yet completed at this context
        assert [row["turn"] for row in invalid] == [0, 1]
        invalid_row = s1.contexts[3]["recent_history"]["turns"]["items"][-1]
        assert (invalid_row["raw_choice"], invalid_row["effective_choice"], invalid_row["executed_action"],
                invalid_row["outcome"]) == ("not_offered", None, None, "rejected_invalid")
        assert early["action_attempts"]["items"][0]["observed_effect"] == {"metric": {"delta": 0.1}}
        assert current["facts"] == ["Unresolved hypothesis: evidence pending."]
        assert current["guidance"][0]["result"] == {"hypothesis": "still unresolved"}
        assert current["pending_help"] is None
        assert [req["choice_id"] for req in bridge.requests] == ["a", "b", "a"]
        assert [r.raw_choice for r in loop.turns] == [step if isinstance(step, str) else step["choice_id"]
                                                    for step in steps]
        assert len(sink.of_kind("help")) == 2  # opened and adopted, never generated by the memory
        for item in attempts["items"]:
            assert sink.entries[item["trace_seq"]]["kind"] == "execution"
            assert sink.entries[item["trace_seq"]]["turn"] == item["turn"]
            obs = sink.entries[item["observation_trace_seq"]]
            assert obs["kind"] == "observation" and obs["body"]["seq"] == item["observation_seq"]
        assert sink.entries[first["effect_trace_seq"]]["kind"] == "effect"
        for item in turns["items"]:
            assert sink.entries[item["trace_seq"]]["kind"] == "turn"
            assert sink.entries[item["trace_seq"]]["turn"] == item["turn"]
        assert "public" not in json.dumps(history) and "large observation" not in json.dumps(history)

    asyncio.run(scenario())


def test_execution_window_counts_all_attempts_and_has_null_trace_references_without_sink():
    async def scenario():
        steps = ["a"] * 10 + ["w"]
        loop, bridge, s1 = make_loop(steps, statuses=("rejected", "failed") + ("completed",) * 8)
        for _ in steps:
            await loop.run_turn()
        history = s1.contexts[-1]["recent_history"]
        assert (history["turns"]["total"], history["turns"]["omitted"]) == (10, 2)
        assert (history["action_attempts"]["total"], history["action_attempts"]["omitted"]) == (10, 2)
        assert [row["turn"] for row in history["action_attempts"]["items"]] == list(range(2, 10))
        first, second = s1.contexts[2]["recent_history"]["action_attempts"]["items"]
        assert (first["terminal_status"], first["reject_reason"], first["executed_action"]) == (
            "rejected", "stale_age", None)
        assert (second["terminal_status"], second["reject_reason"]) == ("failed", None)
        for window in (history["turns"], history["action_attempts"]):
            assert all(row["trace_seq"] is None and row["observation_trace_seq"] is None
                       for row in window["items"])
        assert all(row["effect_trace_seq"] is None for row in history["action_attempts"]["items"])
        assert len(bridge.requests) == 10

    asyncio.run(scenario())


def test_rejected_pending_continuation_is_an_attempt_not_an_executed_action():
    async def scenario():
        pending = {"choice_id": "h", "while_pending": {"mode": "continue", "choice_id": "b"}}
        loop, bridge, s1 = make_loop([pending, "w"], statuses=("rejected",))
        await loop.run_turn()
        await loop.run_turn()
        history = s1.contexts[1]["recent_history"]
        turn = history["turns"]["items"][0]
        attempt = history["action_attempts"]["items"][0]
        assert (turn["raw_choice"], turn["effective_choice"], turn["executed_action"], turn["outcome"],
                turn["help_event"]) == ("h", "h", None, "escalated", "opened")
        assert (attempt["choice_id"], attempt["purpose"], attempt["terminal_status"],
                attempt["reject_reason"], attempt["executed_action"]) == (
                    "b", "while_pending", "rejected", "stale_age", None)
        assert attempt["applied_duration_s"] is None and attempt["release_verified"] is None
        assert [request["choice_id"] for request in bridge.requests] == ["b"]
        assert loop.helper_count == 1

    asyncio.run(scenario())


def test_equal_or_unavailable_clock_values_do_not_create_a_boundary():
    async def scenario():
        first = observation(100).model_copy(update={"game_s": None})
        second = observation(101, frame=100, game_s=100.0)
        third = observation(102, frame=100).model_copy(update={"game_s": None})
        loop, _, s1 = make_loop(["a", "w", "w"], [first, second, third])
        for _ in range(3):
            await loop.run_turn()
        history = s1.contexts[2]["recent_history"]
        assert history["epoch"] == 0 and history["boundary"] is None
        assert history["turns"]["total"] == 2 and history["action_attempts"]["total"] == 1
        assert history["action_attempts"]["items"][0]["game_s"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_point", ["send", "measure"])
def test_interrupted_attempt_survives_if_loop_is_called_again(failure_point):
    async def scenario():
        sink = MemorySink()
        loop, bridge, s1 = make_loop(["a", "w"], sink=sink)
        if failure_point == "send":
            async def fail_send(**request):
                bridge.requests.append(request)
                raise ConnectionError("no terminal record")
            bridge.act = fail_send
        else:
            class FailingProbe:
                async def before_turn(self, turn):
                    pass

                async def measure(self, choice, execution):
                    raise RuntimeError("measurement unavailable")
            loop._effects = FailingProbe()

        with pytest.raises((ConnectionError, RuntimeError)):
            await loop.run_turn()
        assert loop.turns == []
        await loop.run_turn()
        history = s1.contexts[1]["recent_history"]
        assert history["turns"]["total"] == 0
        assert history["action_attempts"]["total"] == 1
        item = history["action_attempts"]["items"][0]
        assert (item["turn"], item["raw_choice"], item["effective_choice"], item["choice_id"],
                item["executed_action"], item["outcome"]) == (0, "a", "a", "a", None, None)
        assert item["terminal_status"] == (None if failure_point == "send" else "completed")
        assert item["expected_effect"] is item["observed_effect"] is item["effect_fact"] is None
        assert sink.entries[item["trace_seq"]]["kind"] == "execution"
        assert len(bridge.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(("second", "reason"), [
    (observation(101, scene="s1"), "scene_change"),
    (observation(101, session="s1"), "session_change"),
    (observation(101, frame=5), "frame_regression"),
    (observation(101, game_s=5.0), "game_time_regression"),
])
def test_boundary_discards_prior_epoch_before_context(second, reason):
    async def scenario():
        sink = MemorySink()
        loop, _, s1 = make_loop(["a", "w", "w"],
                                [observation(100), second, second.model_copy(update={"seq": 102})], sink=sink)
        for _ in range(3):
            await loop.run_turn()
        history = s1.contexts[1]["recent_history"]
        assert history["epoch"] == 1
        assert history["boundary"] == {"reasons": [reason], "turn": 1, "observation_seq": 101,
                                       "trace_seq": sink.of_kind("observation")[1]["trace_seq"],
                                       "discarded_turns": 1, "discarded_action_attempts": 1}
        assert history["turns"] == {"limit": 8, "total": 0, "omitted": 0, "items": []}
        assert history["action_attempts"] == {"limit": 8, "total": 0, "omitted": 0, "items": []}
        later = s1.contexts[2]["recent_history"]
        assert later["boundary"] == history["boundary"]
        assert [row["turn"] for row in later["turns"]["items"]] == [1]
        assert later["action_attempts"]["items"] == []

    asyncio.run(scenario())


def test_explicit_reset_and_multiple_boundaries_count_all_discarded_including_window_omissions():
    async def scenario():
        first = [observation(100 + i) for i in range(10)]
        after_reset = observation(110)
        after_scene = observation(111, scene="s1")
        loop, _, s1 = make_loop(["a"] * 9 + ["w", "w", "w"],
                                [*first, after_reset, after_scene])
        for _ in first:
            await loop.run_turn()
        await loop.stop("reset")
        await loop.run_turn()
        reset = s1.contexts[10]["recent_history"]
        assert reset["boundary"]["reasons"] == ["reset"]
        assert reset["boundary"]["discarded_turns"] == 10
        assert reset["boundary"]["discarded_action_attempts"] == 9
        assert reset["boundary"]["trace_seq"] is None
        await loop.run_turn()
        history = s1.contexts[11]["recent_history"]
        assert history["epoch"] == 2
        assert history["boundary"]["reasons"] == ["scene_change"]
        assert history["boundary"]["discarded_turns"] == 11
        assert history["boundary"]["discarded_action_attempts"] == 9
        assert history["turns"]["total"] == history["action_attempts"]["total"] == 0
        assert reset["boundary"]["discarded_turns"] == 10  # a later epoch cannot mutate an earlier payload

    asyncio.run(scenario())
