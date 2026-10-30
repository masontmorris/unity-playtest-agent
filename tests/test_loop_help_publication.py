"""Exercise actual helper publication and history forwarding at live-loop await boundaries."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

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
            await loop.run_turn()  # completion is after this turn's adoption point
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
