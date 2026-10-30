"""Exercise actual helper publication and history forwarding at live-loop await boundaries."""

from __future__ import annotations

import asyncio
import gc
import json
import threading

import pytest

from playtest.core.adherence import check_adherence
from playtest.core.turn_loop import TurnLoop
from test_decision_memory import Bridge, Probe, S1, WALL, choices, observation
from test_frontier_publication import _helper, _send


def live_choices():
    offered = choices()
    offered[0] = offered[0].model_copy(update={"action": "move"})
    offered[1] = offered[1].model_copy(update={"action": "look"})
    return offered


@pytest.mark.parametrize("discard", [False, True])
def test_help_completing_during_observation_cannot_publish_until_adoption(tmp_path, discard):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def send(body):
            entered.set()
            assert release.wait(5), "test did not release the fake transport"
            return _send(body)

        helper, tracker, sink = _helper(tmp_path, send)
        observations = [observation(100 + i).model_copy(update={"public": {"counter": 0}}) for i in range(3)]
        bridge = Bridge(observations)
        s1 = S1(["h", "w", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=helper,
                        facts=tracker, sink=sink, clock=lambda: WALL)
        try:
            await loop.run_turn()
            pending_task = loop._pending.task
            assert await asyncio.to_thread(entered.wait, 5)
            original_observe = bridge.observe_once

            async def observe_after_completion():
                release.set()
                await pending_task
                return await original_observe()

            bridge.observe_once = observe_after_completion
            await loop.run_turn()  # completion is after this turn captured eligibility
            assert s1.contexts[1]["pending_help"] is not None
            assert "guidance" not in s1.contexts[1]
            assert not any("Frontier directive" in line for line in s1.contexts[1]["facts"])
            assert tracker.history == [] and helper.hypotheses == {}
            if discard:
                await loop.stop("scene_change")
                assert helper._staged == {} and helper._shown == {} and tracker.history == []
                assert helper.adopt_result(0, pending_task.result()) is pending_task.result()
            else:
                await loop.run_turn()
                assert s1.contexts[2]["pending_help"] is None
                assert s1.contexts[2]["guidance"][0]["result"]["directive_id"] == "d-0"
                assert any("Frontier directive d-0" in line for line in s1.contexts[2]["facts"])
                assert len(tracker.history) == 1 and tracker.history[0]["first_turn"] == 2
                assert [entry["turn"] for entry in sink.of_kind("help")
                        if entry["body"]["event"] == "adopted"] == [2]
            assert bridge.requests == []  # publication never chooses an action
        finally:
            release.set()
            await loop.stop("user")

    asyncio.run(scenario())


@pytest.mark.parametrize(("change", "reason"), [
    ({"frame": 5}, "frame_regression"),
    ({"game_s": 5.0}, "game_time_regression"),
    ({"scene": "s1"}, "scene_change"),
    ({"session": "s1"}, "session_change"),
])
@pytest.mark.parametrize("completion", ["before_entry", "during_observation"])
def test_boundary_discards_unadopted_frontier_result_and_allows_new_help(tmp_path, change, reason, completion):
    async def scenario():
        packets = []

        def send(body):
            packets.append(json.loads(body["messages"][2]["content"]))
            return _send(body)

        helper, tracker, sink = _helper(tmp_path, send)
        first = observation(100).model_copy(update={"public": {"counter": 0}})
        second = observation(101).model_copy(update={**change, "public": {"counter": 0}})
        third = second.model_copy(update={"seq": 102, "frame": second.frame + 1,
                                          "game_s": second.game_s + 1})
        fourth = third.model_copy(update={"seq": 103, "frame": third.frame + 1,
                                         "game_s": third.game_s + 1})
        bridge = Bridge([first, second, third, fourth])
        s1 = S1(["h", "a", "h", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=helper,
                        facts=tracker, sink=sink, clock=lambda: WALL)
        try:
            await loop.run_turn()
            old_task = loop._pending.task
            if completion == "before_entry":
                await old_task
                assert 0 in helper._staged
            else:
                assert not old_task.done()
                original_observe = bridge.observe_once

                async def complete_before_returning_observation():
                    await old_task
                    return await original_observe()

                bridge.observe_once = complete_before_returning_observation

            record = await loop.run_turn()
            assert record.raw_choice == record.effective_choice == record.executed_action == "a"
            assert [(r["choice_id"], r["based_on_seq"]) for r in bridge.requests] == [("a", 101)]
            context = s1.contexts[1]
            assert context["as_of"]["seq"] == 101 and context["pending_help"] is None
            assert context["recent_history"]["boundary"]["reasons"] == [reason]
            assert "guidance" not in context
            assert not any("Frontier directive" in line for line in context["facts"])
            assert helper._staged == {} and helper.hypotheses == {} and tracker.history == []
            assert [e["body"]["event"] for e in sink.of_kind("help")] == ["opened", "cancelled"]
            assert not [e for e in sink.of_kind("investigation") if e["body"]["event"] == "directive"]
            assert loop.adoptions == [] and loop.cancelled_helpers == 1

            await loop.run_turn()  # S1 explicitly asks for new help in the new epoch
            await loop._pending.task
            await loop.run_turn()
            assert packets[1]["hypotheses"] == [] and packets[1]["directive"] is None
            assert s1.contexts[3]["guidance"][0]["result"]["directive_id"] == "d-2"
            assert [item["directive_id"] for item in tracker.history] == ["d-2"]
            assert tracker.history[0]["first_turn"] == 3
            assert [e["turn"] for e in sink.of_kind("help") if e["body"]["event"] == "adopted"] == [3]
            entries = [e["kind"] for e in sink.entries if e["turn"] == 3]
            assert entries.index("observation") < entries.index("help") < entries.index("context")
            action_entries = [e["kind"] for e in sink.entries if e["turn"] == 1]
            assert action_entries.index("context") < action_entries.index("decision") < action_entries.index("execution")
            assert len(bridge.requests) == 1
            assert check_adherence(loop.turns) == []
        finally:
            await loop.stop("user")

    asyncio.run(scenario())


@pytest.mark.parametrize("late_failure", [False, True])
def test_boundary_cancels_resistant_helper_and_late_completion_cannot_poison_new_help(tmp_path, late_failure):
    async def scenario():
        helper, tracker, sink = _helper(tmp_path, _send)
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        unhandled = []
        event_loop = asyncio.get_running_loop()
        previous_handler = event_loop.get_exception_handler()
        event_loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        class ResistantHelp:
            async def run(self, request):
                if request.turn == 0:
                    started.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        await release.wait()
                    if late_failure:
                        raise RuntimeError("late helper failure")
                return await helper.run(request)

            def discard_result(self, turn):
                helper.discard_result(turn)

            def adopt_result(self, turn, result):
                return helper.adopt_result(turn, result)

        first = observation(100).model_copy(update={"public": {"counter": 0}})
        second = observation(101, scene="s1").model_copy(update={"public": {"counter": 0}})
        observations = [first, second, observation(102, scene="s1"), observation(103, scene="s1")]
        observations = [o.model_copy(update={"public": {"counter": 0}}) for o in observations]
        bridge = Bridge(observations)
        s1 = S1(["h", "a", "h", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=ResistantHelp(),
                        facts=tracker, sink=sink, clock=lambda: WALL)
        old_task = None
        try:
            await loop.run_turn()
            old_task = loop._pending.task
            await started.wait()
            await loop.run_turn()  # must not wait for the cancellation-resistant helper
            await cancelled.wait()
            assert s1.contexts[1]["pending_help"] is None and "guidance" not in s1.contexts[1]
            assert tracker.history == [] and loop.adoptions == []
            assert 0 in helper._discarded and not old_task.done()
            finished = asyncio.Event()
            old_task.add_done_callback(lambda _task: finished.set())
            release.set()
            await finished.wait()
            await asyncio.sleep(0)  # let the loop's completion callback consume exceptions
            assert loop._retired_helpers == set() and helper._staged == {}
            assert tracker.history == [] and helper.hypotheses == {}
            old_task = None
            gc.collect()
            await asyncio.sleep(0)
            assert unhandled == []

            await loop.run_turn()
            await loop._pending.task
            await loop.run_turn()
            assert [item["directive_id"] for item in tracker.history] == ["d-2"]
            assert s1.contexts[3]["guidance"][0]["result"]["directive_id"] == "d-2"
            assert [(r["choice_id"], r["based_on_seq"]) for r in bridge.requests] == [("a", 101)]
            assert [e["body"]["event"] for e in sink.of_kind("help")] == [
                "opened", "cancelled", "opened", "adopted"]
        finally:
            release.set()
            if old_task is not None and not old_task.done():
                await asyncio.gather(old_task, return_exceptions=True)
            await loop.stop("user")
            event_loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())


def test_boundary_invalidates_pending_help_without_attached_helper():
    async def scenario():
        bridge = Bridge([observation(100), observation(101, scene="s1")])
        s1 = S1(["h", "a"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, clock=lambda: WALL)
        await loop.run_turn()
        assert loop.pending_helper is not None
        record = await loop.run_turn()
        assert loop.pending_helper is None and loop.cancelled_helpers == 1
        assert s1.contexts[1]["pending_help"] is None and "guidance" not in s1.contexts[1]
        assert record.raw_choice == record.effective_choice == record.executed_action == "a"
        assert [(r["choice_id"], r["based_on_seq"]) for r in bridge.requests] == [("a", 101)]
        assert not loop.resolve_helper("late")
        await loop.stop("user")

    asyncio.run(scenario())


@pytest.mark.parametrize("completion", ["before_entry", "during_observation"])
@pytest.mark.parametrize("boundary", [False, True])
def test_external_resolution_is_staged_until_a_matching_observation(completion, boundary):
    async def scenario():
        scene = "s1" if boundary else "s0"
        bridge = Bridge([observation(100), observation(101, scene=scene), observation(102, scene=scene)])
        s1 = S1(["h", "w", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, clock=lambda: WALL)
        await loop.run_turn()

        def resolve():
            assert loop.resolve_helper({"advice": "external"}, source="operator")
            assert not loop.resolve_helper({"advice": "duplicate"}, source="operator")
            assert loop.adoptions == []  # resolution does not publish before the epoch is checked

        if completion == "before_entry":
            resolve()
        else:
            original_observe = bridge.observe_once

            async def resolve_during_observation():
                bridge.observe_once = original_observe
                resolve()
                return await original_observe()

            bridge.observe_once = resolve_during_observation

        await loop.run_turn()
        context = s1.contexts[1]
        if boundary:
            assert context["pending_help"] is None and "guidance" not in context
            assert loop.adoptions == [] and loop.cancelled_helpers == 1
        elif completion == "before_entry":
            assert context["pending_help"] is None
            assert context["guidance"][0]["result"] == {"advice": "external"}
        else:
            assert context["pending_help"] is not None and "guidance" not in context
            assert loop.adoptions == []
        await loop.run_turn()
        if boundary:
            assert "guidance" not in s1.contexts[2] and loop.adoptions == []
        else:
            assert s1.contexts[2]["guidance"][0]["result"] == {"advice": "external"}
            assert loop.adoptions == [{"advice": "external"}]
        assert bridge.requests == []
        await loop.stop("user")

    asyncio.run(scenario())


def test_external_resolution_during_observation_cannot_replace_entry_ready_result(tmp_path):
    async def scenario():
        helper, tracker, sink = _helper(tmp_path, _send)
        bridge = Bridge([observation(i).model_copy(update={"public": {"counter": 0}})
                         for i in (100, 101, 102)])
        s1 = S1(["h", "w", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=helper,
                        facts=tracker, sink=sink, clock=lambda: WALL)
        try:
            await loop.run_turn()
            await loop._pending.task  # the original result was ready at turn entry
            original_observe = bridge.observe_once

            async def replace_during_observation():
                bridge.observe_once = original_observe
                assert loop.resolve_helper("replacement", source="operator")
                return await original_observe()

            bridge.observe_once = replace_during_observation
            await loop.run_turn()
            assert s1.contexts[1]["pending_help"] is not None and "guidance" not in s1.contexts[1]
            assert tracker.history == [] and helper._staged == {} and loop.adoptions == []
            await loop.run_turn()
            assert s1.contexts[2]["guidance"][0]["result"] == "replacement"
            assert tracker.history == [] and loop.adoptions == ["replacement"]
            assert bridge.requests == [] and check_adherence(loop.turns) == []
        finally:
            await loop.stop("user")

    asyncio.run(scenario())


def test_frontier_receives_actual_action_shortfall_after_nine_waits(tmp_path):
    async def scenario():
        packets = []

        def send(body):
            packets.append(json.loads(body["messages"][2]["content"]))
            return _send(body)

        helper, tracker, sink = _helper(tmp_path, send)
        steps = ["a", *["w"] * 9, "h", "w"]
        bridge = Bridge([observation(100 + i).model_copy(update={"public": {"counter": 0}})
                         for i in range(len(steps))])
        s1 = S1(steps)
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=helper,
                        facts=tracker, effects=Probe(), sink=sink, clock=lambda: WALL)
        try:
            for _ in range(11):
                await loop.run_turn()
            await loop._pending.task
            await loop.run_turn()
            assert helper.calls == 1 and len(packets) == 1
            recent = packets[0]["recent"]
            assert recent[0]["windows"]["turns"]["omitted"] == 2
            attempt = next(row for row in recent if row["record_kind"] == "action_attempts")
            assert attempt["turn"] == 0 and attempt["seq"] == 100
            assert attempt["expected_effect"] == {"metric": {"delta": 2.0}}
            assert attempt["observed_effect"] == {"metric": {"delta": 0.1}}
            assert sink.entries[attempt["trace_seq"]]["kind"] == "execution"
            assert sink.entries[attempt["observation_trace_seq"]]["body"]["seq"] == attempt["seq"]
            assert len(bridge.requests) == 1 and bridge.requests[0]["choice_id"] == "a"
            assert s1.contexts[-1]["guidance"][0]["result"]["directive_id"] == "d-10"
        finally:
            await loop.stop("user")

    asyncio.run(scenario())
