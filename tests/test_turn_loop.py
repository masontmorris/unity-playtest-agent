"""The real-time turn loop (#29): observation with its age, facts, a versioned choice set, then S1.

Every scenario runs the production client against the fake game and asserts on the game's input log,
not only on the loop's records. S1, the helper and the effect probe are scripted fakes. Each trace the
loop writes is checked against the rules in `playtest.core.trace_sink`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from loop_fakes import (
    FakeHelp, FakeProbe, FakeS1, Raw, Sleeps, choices, fail, pick, rig, run,
)
from turn_trace_check import problems

from playtest.contracts.bridge import ExecStatus, ExecutionRecord, Observation, RejectReason
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceSet, TurnOutcome, TurnRecord
from playtest.contracts.trace import TraceAuthor, TraceKind
from playtest.core.adherence import check_adherence
from playtest.core.context import build_context
from playtest.core.execution import Execution
from playtest.core.facts import RecordFacts
from playtest.core.ports import Executed, S1Answer, S1Call, TurnReport
from playtest.core.turn_loop import TurnLoop


def finish_trace(r) -> None:
    """Stop the loop and close the trace the way a harness does, then require the trace to be clean."""
    r.sink.append(TraceKind.end, {"status": "complete", "error": None, "turns": len(r.loop.turns), "redactions": 0},
                  author=TraceAuthor.harness)
    assert problems(r.sink.entries) == []


# --- S1 owns the actuator ---------------------------------------------------------------------


def test_a_valid_choice_executes_as_selected():
    async def main():
        r = await rig("c1", "c2", "c1")
        try:
            await r.turns(3)
            records = r.loop.turns
            assert [(t.raw_choice, t.effective_choice, t.executed_action) for t in records] == [
                ("c1", "c1", "c1"), ("c2", "c2", "c2"), ("c1", "c1", "c1")]
            assert all(t.outcome is TurnOutcome.executed for t in records)
            assert r.game.applied() == ["shift", "increment", "shift"]
            assert [e["choice_id"] for e in r.game.input_log if e["event"] == "applied"] == ["c1", "c2", "c1"]
            sent = r.sink.of_kind("execution")[0]["body"]
            seq = r.sink.of_kind("observation")[0]["body"]["seq"]
            assert sent["purpose"] == "choice" and sent["request"]["choice_id"] == "c1"
            assert sent["request"]["based_on_seq"] == seq and sent["records"][-1]["status"] == "completed"
            assert check_adherence(r.loop.turns) == []
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_an_explicit_wait_sends_no_input():
    async def main():
        r = await rig("w", "w")
        try:
            await r.turns(2)
            assert [t.effective_choice for t in r.loop.turns] == ["w", "w"]
            assert all(t.executed_action is None for t in r.loop.turns)
            assert r.game.applied() == [] and r.game.input_log == []
            assert r.sink.of_kind("execution") == []
            assert r.kinds(0) == ["observation", "choice_set", "context", "s1_call", "decision", "turn"]
            assert r.s1.contexts[1]["facts"] == ["Turn 0: w was chosen; no input was sent."]
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def timed_wait(duration, choice_id: str = "w5") -> Choice:
    return Choice(id=choice_id, kind="wait", label=f"Wait {duration} s with no input",
                  params={"duration_game_s": duration}, source="core")


def test_a_wait_with_a_duration_sleeps_that_long_after_the_decision_and_sends_no_input():
    async def main():
        holder = {}
        seen = []

        async def spy(seconds):
            entries = holder["rig"].sink.entries
            seen.append((seconds, entries[-1]["kind"], sum(e["kind"] == "turn" for e in entries)))

        r = await rig("w5", "w5", choices=[*choices(), timed_wait(0.5)], sleep=spy)
        holder["rig"] = r
        try:
            await r.turns(2)
            # It slept the game seconds at time scale 1, with the decision written and the turn not yet closed.
            assert seen == [(0.5, "decision", 0), (0.5, "decision", 1)]
            assert [t.outcome for t in r.loop.turns] == [TurnOutcome.finished] * 2
            assert [(t.raw_choice, t.effective_choice, t.executed_action) for t in r.loop.turns] == [("w5", "w5", None)] * 2
            assert r.game.applied() == [] and r.game.input_log == [] and r.sink.of_kind("execution") == []
            assert r.kinds(0) == ["observation", "choice_set", "context", "s1_call", "decision", "turn"]
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_a_wait_without_a_duration_does_not_sleep_and_neither_does_anything_else():
    async def main():
        sleeps = Sleeps()
        # An untimed wait, an invalid answer, a finish, an action, and a help turn that waits while pending.
        r = await rig("w", pick("zz"), "f", "c1", pick("h"), choices=[*choices(), timed_wait(0.5)], sleep=sleeps)
        try:
            await r.turns(5)
            assert sleeps.calls == []
            assert r.game.applied() == ["shift"]
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_a_duration_that_is_not_a_positive_finite_number_is_not_slept():
    async def main():
        sleeps = Sleeps()
        bad = [timed_wait(value, f"b{i}") for i, value in enumerate((0, -1.0, "0.5", True, None))]
        r = await rig(*[c.id for c in bad], "w5", choices=[*choices(), *bad, timed_wait(2)], sleep=sleeps)
        try:
            await r.turns(6)
            assert [t.outcome for t in r.loop.turns] == [TurnOutcome.finished] * 6
            assert sleeps.calls == [2]                       # only the last, valid, one slept
            await r.loop.stop("finish")
        finally:
            await r.close()

    run(main)


def test_the_wait_is_game_seconds_so_the_sleep_follows_the_time_scale():
    async def main():
        sleeps = Sleeps()
        r = await rig("w5", "w5", "w5", "w5", choices=[*choices(), timed_wait(0.5)], sleep=sleeps)
        try:
            await r.turns(1)
            await r.client.clock("set_scale", time_scale=2.0)
            await r.turns(1)
            await r.client.clock("set_scale", time_scale=0.25)
            await r.turns(1)
            await r.client.clock("set_scale", time_scale=0.0)       # a game that is not running: nothing to wait for
            await r.turns(1)
            assert sleeps.calls == [0.5, 0.25, 2.0]
            await r.loop.stop("finish")
        finally:
            await r.close()

    run(main)


def test_a_stepped_clock_never_sleeps_for_a_wait():
    async def main():
        sleeps = Sleeps()
        r = await rig("w5", choices=[*choices(), timed_wait(0.5)], sleep=sleeps, clock_mode=ClockMode.stepped)
        try:
            await r.turns(1)
            assert sleeps.calls == [] and r.loop.turns[0].outcome is TurnOutcome.finished
            await r.loop.stop("finish")
        finally:
            await r.close()

    run(main)


def test_invalid_answers_send_no_input_and_are_recorded_never_repaired():
    async def main():
        r = await rig(
            pick("zz"),                      # an id that is not offered
            pick("c1", set_version=999),     # the wrong set version
            Raw("not-an-object"),            # not an object
            fail("transport error"),         # the provider failed
            pick("c2"),                      # then a valid answer: the only input of the run
        )
        try:
            await r.turns(5)
            records = r.loop.turns
            assert [t.outcome for t in records[:4]] == [TurnOutcome.rejected_invalid] * 4
            assert [t.raw_choice for t in records[:4]] == ["zz", "c1", None, None]
            assert all(t.effective_choice is None and t.executed_action is None for t in records[:4])
            assert r.game.applied() == ["increment"]   # nothing was sent, repaired or substituted before turn 4
            decisions = [e["body"] for e in r.sink.of_kind("decision")]
            assert [d["valid"] for d in decisions] == [False] * 4 + [True]
            assert decisions[0]["error"] == "unknown choice_id 'zz'"
            assert decisions[1]["error"] == "set_version 999 != current 1"
            assert decisions[2]["error"] == decisions[3]["error"] == "answer is not an object"
            assert decisions[2]["answer"] == "not-an-object" and decisions[3]["answer"] is None
            failure = [e for e in r.sink.of_kind("s1_call") if e["body"]["problem"]]
            assert [e["body"]["problem"] for e in failure] == ["transport error"]
            assert [rep.provider_failed for rep in r.loop.reports] == [False, False, False, True, False]
            assert r.s1.contexts[1]["facts"] == ["Turn 0: the answer was rejected (unknown choice_id 'zz'); no input was sent."]
            assert r.s1.contexts[3]["facts"] == ["Turn 2: the answer was rejected (answer is not an object); no input was sent."]
            assert r.s1.contexts[4]["facts"] == [
                "Turn 3: the provider gave no usable answer (transport error); no input was sent."]
            assert check_adherence(records) == []
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_a_port_that_raises_or_halts_rejects_the_turn_and_sends_nothing():
    async def main():
        def explode(_context):
            raise RuntimeError("adapter bug")

        stop = S1Answer(answer=None, calls=(S1Call(question="next_action", problem="the call cap was reached"),),
                        failure="the call cap was reached", halt="the call cap was reached")
        r = await rig(explode, stop)
        try:
            await r.turns(2)
            assert [t.outcome for t in r.loop.turns] == [TurnOutcome.rejected_invalid] * 2
            calls = [e["body"]["problem"] for e in r.sink.of_kind("s1_call")]
            assert calls == ["port error: RuntimeError", "the call cap was reached"]
            assert r.loop.halt_reason == "the call cap was reached"
            assert r.game.applied() == []
            await r.loop.stop("error", error="the call cap was reached")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_a_bridge_rejection_is_recorded_with_its_reason_and_nothing_replaces_it():
    async def main():
        r = await rig("c1", "w", game={"reject_acts": RejectReason.stale_age})
        try:
            await r.turns(2)
            first = r.loop.turns[0]
            assert first.outcome is TurnOutcome.rejected_stale
            assert first.effective_choice == "c1" and first.executed_action is None
            assert r.game.applied() == []
            execution = r.sink.of_kind("execution")[0]["body"]
            assert execution["records"][-1]["status"] == "rejected"
            assert execution["records"][-1]["reject_reason"] == "stale_age"
            assert r.s1.contexts[1]["facts"] == ["Turn 0: the bridge rejected c1 (stale_age); no input was sent."]
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


# --- pending help -----------------------------------------------------------------------------


def test_while_pending_continue_runs_exactly_that_choice_and_ordinary_selections_still_execute():
    async def main():
        help_port = FakeHelp()
        r = await rig(pick("h", while_pending={"mode": "continue", "choice_id": "c2"}), "c1", "w", "w", help=help_port)
        try:
            await r.turns(4)
            first = r.loop.turns[0]
            assert first.outcome is TurnOutcome.escalated and first.effective_choice == "h"
            assert first.executed_action == "c2" and not first.policy_default
            assert first.while_pending is not None and first.while_pending.choice_id == "c2"
            assert r.game.applied() == ["increment", "shift"]   # c2 from the pending turn, then S1's own c1
            executions = [e["body"] for e in r.sink.of_kind("execution")]
            assert [(x["purpose"], x["request"]["choice_id"]) for x in executions] == [
                ("while_pending", "c2"), ("choice", "c1")]
            # An explicit wait while help is pending issues no input.
            assert r.loop.turns[2].executed_action is None and r.loop.turns[3].executed_action is None
            assert r.game.applied() == ["increment", "shift"]
            assert r.s1.contexts[1]["facts"][0] == (
                "Turn 0: h was chosen; a help request was opened; while help is pending: continue with c2.")
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_a_missing_or_invalid_while_pending_means_wait_and_policy_default():
    async def main():
        r = await rig(
            pick("h"),                                                                  # missing
            pick("d", while_pending={"mode": "continue", "choice_id": "zz"}),           # not offered
            pick("d", while_pending="nonsense"),                                        # not an object
            pick("h", while_pending={"mode": "continue", "choice_id": "f"}),            # finish is not a continuation
            pick("h", while_pending={"mode": "continue", "choice_id": "d"}),            # help is not a continuation
        )
        try:
            await r.turns(5)
            for record in r.loop.turns:
                assert record.outcome in (TurnOutcome.escalated, TurnOutcome.delegated)
                assert record.while_pending is not None and record.while_pending.mode == "wait"
                assert record.policy_default is True and record.executed_action is None
            assert r.game.applied() == [] and r.sink.of_kind("execution") == []
            assert all(e["body"]["policy_default"] for e in r.sink.of_kind("decision"))
            assert check_adherence(r.loop.turns) == []
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_one_helper_request_at_most_and_a_repeat_creates_no_new_call():
    async def main():
        help_port = FakeHelp()
        r = await rig(pick("h"), pick("d"), pick("h"), "w", help=help_port)
        try:
            await r.turns(4)
            await asyncio.sleep(0)
            assert len(help_port.requests) == 1 and r.loop.helper_count == 1
            events = [e["body"]["event"] for e in r.sink.of_kind("help")]
            assert events == ["opened", "repeat", "repeat"]
            repeat = r.helps("repeat")[0]["body"]
            assert (repeat["kind"], repeat["choice_id"], repeat["opened_turn"]) == ("delegate", "d", 0)
            assert [t.outcome for t in r.loop.turns[:3]] == [
                TurnOutcome.escalated, TurnOutcome.delegated, TurnOutcome.escalated]
            request = help_port.requests[0]
            assert (request.kind, request.choice_id, request.role, request.turn) == ("escalate", "h", "frontier", 0)
            assert request.context == r.s1.contexts[0] and request.based_on_seq == r.loop.turns[0].based_on_seq
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_a_result_is_adopted_once_and_shown_with_its_source_and_age():
    async def main():
        help_port = FakeHelp(result={"advice": "try c1"})
        r = await rig(pick("h"), "w", "w", "w", pick("h"), help=help_port)
        try:
            await r.turns(1)
            help_port.release.set()
            await asyncio.sleep(0.02)                   # the helper finishes between turns
            await r.turns(3)
            adopted = r.helps("adopted")
            assert len(adopted) == 1 and r.loop.adoptions == [{"advice": "try c1"}]
            assert adopted[0]["turn"] == 1 and adopted[0]["refs"] == [r.helps("opened")[0]["trace_seq"]]
            assert adopted[0]["body"]["source"] == "fake-helper"
            assert adopted[0]["body"]["result"] == {"advice": "try c1"} and adopted[0]["body"]["opened_turn"] == 0
            first_of_turn = [e for e in r.sink.entries if e["turn"] == 1][0]
            assert first_of_turn["kind"] == "observation"  # validate the epoch before adopting old help
            assert [e["kind"] for e in r.sink.entries if e["turn"] == 1][:3] == ["observation", "help", "choice_set"]
            assert r.loop.helper_count == 0
            # The next context carries it with its source and age; the request is no longer open.
            context = r.s1.contexts[1]
            assert context["pending_help"] is None
            item = context["guidance"][0]
            assert item["source"] == "fake-helper" and item["result"] == {"advice": "try c1"}
            assert (item["kind"], item["requested_turn"], item["adopted_turn"]) == ("escalate", 0, 1)
            assert isinstance(item["age_s"], float) and 0 <= item["age_s"] < 5
            # It stays visible, still adopted once, with a growing age.
            assert r.s1.contexts[2]["guidance"][0]["age_s"] >= item["age_s"]
            assert len(r.helps("adopted")) == 1
            await r.turns(1)                            # a new escalation opens a new request, and only one
            await asyncio.sleep(0)
            assert len(help_port.requests) == 2 and r.loop.helper_count == 1
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_pending_help_is_in_every_context_and_null_exactly_while_a_request_is_open():
    async def main():
        help_port = FakeHelp()
        r = await rig("w", pick("h"), "w", "w", "w", help=help_port)
        try:
            await r.turns(1)
            await r.turns(2)
            help_port.release.set()
            await asyncio.sleep(0.02)
            await r.turns(2)
            assert all("pending_help" in c for c in r.s1.contexts)
            opened = [c["pending_help"] is not None for c in r.s1.contexts]
            assert opened == [False, False, True, False, False]
            pending = r.s1.contexts[2]["pending_help"]
            assert (pending["kind"], pending["choice_id"], pending["opened_turn"]) == ("escalate", "h", 1)
            assert pending["label"] == "Escalate" and pending["waiting_on"] == "frontier"
            assert pending["helper_attached"] is True and pending["reason"] is None
            assert isinstance(pending["age_wall_s"], float) and pending["age_wall_s"] >= 0
            assert isinstance(pending["age_game_s"], float) and pending["age_game_s"] >= 0
            assert pending["since_game_s"] == r.sink.of_kind("observation")[1]["body"]["game_s"]
            assert "guidance" not in r.s1.contexts[2] and "guidance" in r.s1.contexts[3]
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_with_no_helper_attached_an_escalation_stays_pending_and_visible_until_stop():
    async def main():
        r = await rig(pick("h"), "w", "w", "w")
        try:
            await r.turns(4)
            assert r.loop.helper_count == 1 and r.helps("adopted") == []
            for context in r.s1.contexts[1:]:
                assert context["pending_help"]["helper_attached"] is False
                assert context["pending_help"]["choice_id"] == "h"
            await r.loop.stop("user")
            cancelled = r.helps("cancelled")
            assert len(cancelled) == 1 and cancelled[0]["turn"] is None and cancelled[0]["body"]["reason"] == "user"
            assert r.loop.helper_count == 0 and r.loop.cancelled_helpers == 1
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_stop_cancels_a_pending_helper_and_releases_a_held_lease():
    async def main():
        help_port = FakeHelp()
        r = await rig("c3", pick("h"), help=help_port, lease_s=30.0)
        try:
            await r.turns(2)
            await asyncio.sleep(0)
            assert r.game.applied() == ["hold"] and r.game.releases() == [] and r.loop.helper_count == 1
            result = await r.loop.stop("user")
            assert result is not None and result.release_verified is True
            assert ("primary", "stop") in r.game.releases()
            assert help_port.cancelled and r.loop.helper_count == 0 and r.loop.cancelled_helpers == 1
            cancelled = r.helps("cancelled")[0]
            assert cancelled["refs"] == [r.helps("opened")[0]["trace_seq"]] and cancelled["turn"] is None
            stop = r.sink.of_kind("stop")[0]["body"]
            assert stop["reason"] == "user" and stop["result"]["release_verified"] is True
            assert await r.loop.stop("user") is result and len(r.sink.of_kind("stop")) == 1   # runs once
            with pytest.raises(ValueError):
                await r.loop.stop("whenever")
            assert r.loop.resolve_helper("late") is False
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_resolve_helper_adopts_exactly_once_after_the_next_observation_epoch_check():
    async def main():
        r = await rig(pick("h"), "w")
        try:
            await r.turns(1)
            assert r.loop.resolve_helper({"advice": "x"}, source="operator") is True
            assert r.loop.resolve_helper({"advice": "y"}) is False
            await r.turns(1)
            adopted = r.helps("adopted")
            assert len(adopted) == 1 and adopted[0]["turn"] == 1 and adopted[0]["body"]["source"] == "operator"
            assert [e["kind"] for e in r.sink.entries if e["turn"] == 1][:2] == ["observation", "help"]
            assert r.s1.contexts[1]["guidance"][0]["source"] == "operator"
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


# --- the context ------------------------------------------------------------------------------


def test_age_and_sample_gap_are_measured_from_the_observations():
    async def main():
        r = await rig("w", "w", "w")
        try:
            for _ in range(3):
                await r.turns(1)
                await asyncio.sleep(0.05)       # longer than the wall clock's tick, so a gap is not 0.0 by chance
            first, second, third = (c["as_of"] for c in r.s1.contexts)
            assert first["sample_gap_s"] is None            # nothing before the first observation
            assert isinstance(first["age_s"], float) and 0 <= first["age_s"] < 5
            walls = [datetime.fromisoformat(e["body"]["wall_utc"]) for e in r.sink.of_kind("observation")]
            assert second["sample_gap_s"] == pytest.approx((walls[1] - walls[0]).total_seconds())
            assert third["sample_gap_s"] == pytest.approx((walls[2] - walls[1]).total_seconds())
            assert second["sample_gap_s"] > 0 and third["sample_gap_s"] > 0
            assert [c["as_of"]["seq"] for c in r.s1.contexts] == [
                e["body"]["seq"] for e in r.sink.of_kind("observation")]
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_an_age_the_clock_cannot_measure_is_null_not_zero():
    async def main():
        long_ago = datetime(2000, 1, 1, tzinfo=timezone.utc)
        r = await rig(pick("h"), "w", clock=lambda: long_ago)
        try:
            await r.turns(2)
            assert all(c["as_of"]["age_s"] is None for c in r.s1.contexts)       # the clock is before the reading
            assert r.s1.contexts[1]["as_of"]["sample_gap_s"] is not None        # both readings share the bridge's clock
            await r.loop.stop("finish")
        finally:
            await r.close()

    run(main)


def test_the_context_adds_guidance_only_when_there_is_some():
    obs = Observation(session="s", seq=3, game_s=1.0, wall_utc=datetime(2026, 10, 28, tzinfo=timezone.utc),
                      clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False, time_scale=1.0, frame=1)
    options = ChoiceSet(set_version=0, built_from_seq=3, choices=choices())
    plain = build_context(objective="o", observation=obs, choices=options)
    assert "guidance" not in plain and plain["pending_help"] is None
    shown = build_context(objective="o", observation=obs, choices=options, guidance=[{"source": "x", "age_s": 1.0}])
    assert shown["guidance"] == [{"source": "x", "age_s": 1.0}]


def test_the_s1_port_is_awaited_without_blocking_the_event_loop():
    async def main():
        class Slow(FakeS1):
            async def decide(self, context):
                await asyncio.sleep(0.15)
                return await super().decide(context)

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        r = await rig()
        r.loop._s1 = Slow("w")
        task = asyncio.create_task(ticker())
        try:
            await r.loop.run_turn()
            assert ticks >= 5            # the event loop kept running while S1 decided
            await r.loop.stop("finish")
        finally:
            task.cancel()
            await r.close()

    run(main)


# --- facts and effects ------------------------------------------------------------------------


def test_the_effect_probe_reads_before_the_observation_and_its_report_is_traced_and_shown():
    async def main():
        r = await rig("c1", "w", probe=True)
        try:
            await r.turns(2)
            probe = r.loop._effects
            assert isinstance(probe, FakeProbe)
            assert probe.turns == [0, 1] and probe.measured == ["c1"]
            # `before_turn` ran before the turn's observation was appended, never between observing and acting.
            for turn, count in zip(probe.turns, probe.entries_before_turn):
                assert r.sink.entries[count]["kind"] == "observation" and r.sink.entries[count]["turn"] == turn
            assert r.kinds(0) == ["observation", "choice_set", "context", "s1_call", "decision", "execution",
                                  "effect", "turn"]
            effect = r.sink.of_kind("effect")[0]
            execution = r.sink.of_kind("execution")[0]
            assert effect["refs"] == [execution["trace_seq"]] and effect["author"] == "core"
            assert effect["body"] == {"choice_id": "c1", "action": "shift", "expected": {"net": 6.0},
                                      "observed": {"net": 6.1}}
            assert r.s1.contexts[1]["facts"] == [
                "Turn 0: c1 was executed.",
                "c1 ended completed; applied duration 0.000 s; release verified true.",
                "c1: net 6.1 against a calibrated 6.0.",
            ]
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_a_rejected_input_is_not_measured():
    async def main():
        r = await rig("c1", "w", probe=True, game={"reject_acts": RejectReason.stale_age})
        try:
            await r.turns(2)
            assert r.loop._effects.measured == [] and r.sink.of_kind("effect") == []
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)


def test_unmeasured_duration_and_release_read_unknown_not_zero():
    async def main():
        wall = datetime(2026, 10, 28, tzinfo=timezone.utc)
        envelope = {"session": "s", "seq": 1, "game_s": 1.0, "wall_utc": wall}
        observation = Observation(clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False,
                                  time_scale=1.0, frame=1, **envelope)
        terminal = ExecutionRecord(action_id="a", status=ExecStatus.failed, **envelope)   # nothing else was measured
        selected = choices()[0]
        record = TurnRecord(turn=0, set_version=0, based_on_seq=1, raw_choice="c1", effective_choice="c1",
                            executed_action="c1", outcome=TurnOutcome.executed)
        sent = Executed("choice", selected, Execution(request={}, records=(terminal,)))
        report = TurnReport(record=record, observation=observation, selected=selected, problem=None, failure=None,
                            help_event=None, executed=(sent,))
        assert await RecordFacts().facts([report], observation) == [
            "Turn 0: c1 was executed.",
            "c1 ended failed; applied duration unknown; release verified unknown.",
        ]
        assert await RecordFacts().facts([], observation) == []

    run(main)


# --- the whole trace --------------------------------------------------------------------------


def test_a_mixed_run_writes_a_trace_that_passes_every_replay_check():
    async def main():
        help_port = FakeHelp(result={"advice": "carry on"})

        def stale_once(_context):
            r.game.reject_acts = RejectReason.stale_age
            return pick("c1")

        def fresh_again(_context):
            r.game.reject_acts = None
            return "c2"

        r = await rig(
            "c1", "w", pick("h", while_pending={"mode": "continue", "choice_id": "c2"}), pick("d"),
            pick("zz"), stale_once, fresh_again, "w", pick("f"), help=help_port, probe=True,
        )
        try:
            await r.turns(4)
            help_port.release.set()
            await asyncio.sleep(0.02)
            while len(r.loop.turns) < 9:
                await r.loop.run_turn()
            records = r.loop.turns
            assert [t.outcome for t in records] == [
                TurnOutcome.executed, TurnOutcome.finished, TurnOutcome.escalated, TurnOutcome.delegated,
                TurnOutcome.rejected_invalid, TurnOutcome.rejected_stale, TurnOutcome.executed,
                TurnOutcome.finished, TurnOutcome.finished]
            assert r.game.applied() == ["shift", "increment", "increment"]
            assert [e["body"]["event"] for e in r.sink.of_kind("help")] == ["opened", "repeat", "adopted"]
            assert [t.set_version for t in records] == list(range(9))
            result = await r.loop.stop("finish")
            assert result is not None and result.release_verified is True
            finish_trace(r)
            # Every entry of a turn carries its number; only run, stop and a stop-time cancel sit outside one.
            outside = [e["kind"] for e in r.sink.entries if e["turn"] is None]
            assert outside == ["run", "stop", "end"]
        finally:
            await r.close()

    run(main)


def test_duplicate_choice_ids_fail_when_the_loop_is_built():
    async def main():
        r = await rig()
        try:
            with pytest.raises(ValueError):
                TurnLoop(r.client, choices=[*choices(), choices()[0]])
            with pytest.raises(ValueError, match="objective"):
                TurnLoop(r.client, choices=choices(), s1=FakeS1())
            with pytest.raises(ValueError, match="no decision source"):
                await TurnLoop(r.client, choices=choices()).run_turn()
        finally:
            await r.close()

    run(main)


# --- the checks themselves, and the negative cases --------------------------------------------


def test_the_trace_checker_catches_a_tampered_trace():
    """A checker that cannot fail would prove nothing: break each rule once and expect it to notice."""
    import copy

    async def main():
        help_port = FakeHelp()
        r = await rig("c1", pick("h", while_pending={"mode": "continue", "choice_id": "c2"}), pick("zz"), "w",
                      help=help_port)
        try:
            await r.turns(4)
            await r.loop.stop("finish")
            entries = r.sink.entries
            assert problems(entries) == []

            def broken(edit):
                changed = copy.deepcopy(entries)
                edit(changed)
                return problems(changed)

            def kind_at(changed, kind, nth=0):
                return [e for e in changed if e["kind"] == kind][nth]

            assert any("stops before its turn entry" in p for p in broken(lambda c: c.remove(c[-3])))
            assert any("context hash" in p for p in broken(lambda c: kind_at(c, "context")["body"]["payload"]["facts"].append("x")))
            assert any("parse_decision" in p for p in broken(lambda c: kind_at(c, "decision", 2)["body"].update(valid=True)))
            assert any("executed_action" in p for p in broken(lambda c: kind_at(c, "turn", 0)["body"].update(executed_action="c2")))
            assert any("effective_choice" in p for p in broken(lambda c: kind_at(c, "turn", 2)["body"].update(effective_choice="w")))
            assert any("outcome" in p for p in broken(lambda c: kind_at(c, "turn", 0)["body"].update(outcome="finished")))
            assert any("pending_help" in p for p in broken(lambda c: kind_at(c, "context")["body"]["payload"].pop("pending_help")))
            assert any("not bound" in p for p in broken(lambda c: kind_at(c, "choice_set")["body"].update(built_from_seq=999)))
            assert any("set_version" in p for p in broken(lambda c: kind_at(c, "choice_set", 1)["body"].update(set_version=0)))
            assert any("refs" in p for p in broken(lambda c: kind_at(c, "execution")["refs"].append(0)))
            assert any("body keys" in p for p in broken(lambda c: kind_at(c, "decision")["body"].pop("error")))
            def reopen(c):
                opened = next(e for e in c if e["kind"] == "help" and e["body"]["event"] == "opened")
                c.insert(c.index(opened) + 1, copy.deepcopy(opened))
                for number, entry in enumerate(c):
                    entry["trace_seq"] = number

            assert any("opens a request while one is open" in p for p in broken(reopen))
            assert any("adherence" in p for p in broken(lambda c: [
                kind_at(c, "turn", 2)["body"].update(effective_choice=None, outcome="rejected_invalid") or
                kind_at(c, "turn", 3)["body"].update(executed_action="c1")]))
        finally:
            await r.close()

    run(main)


def test_only_s1_opens_help_whatever_the_loop_measures():
    """Stale rejections, failures, repeated waits and a long run are facts; none of them opens a request."""
    async def main():
        help_port = FakeHelp()
        steps = [pick("c1"), pick("c1"), "w", "w", fail("transport error"), fail("transport error"),
                 fail("transport error"), pick("zz"), "w", "w", "w", "w"]
        r = await rig(*steps, help=help_port, game={"reject_acts": RejectReason.stale_age})
        try:
            await r.turns(len(steps))
            await asyncio.sleep(0)
            assert help_port.requests == [] and r.loop.helper_count == 0
            assert r.sink.of_kind("help") == []
            assert all(t.outcome in (TurnOutcome.rejected_stale, TurnOutcome.finished, TurnOutcome.rejected_invalid)
                       for t in r.loop.turns)
            assert all(c["pending_help"] is None for c in r.s1.contexts)
            await r.loop.stop("finish")
        finally:
            await r.close()

    run(main)


def test_nothing_overrides_what_s1_chose_while_help_is_pending():
    """A pending request never suppresses, delays or replaces S1's own selection."""
    async def main():
        help_port = FakeHelp()
        r = await rig(pick("h"), "c1", "c2", pick("d"), "c2", "w", help=help_port)
        try:
            await r.turns(6)
            assert r.game.applied() == ["shift", "increment", "increment"]
            assert [t.executed_action for t in r.loop.turns] == [None, "c1", "c2", None, "c2", None]
            assert r.loop.helper_count == 1 and len(help_port.requests) == 1
            assert check_adherence(r.loop.turns) == []
            await r.loop.stop("finish")
            finish_trace(r)
        finally:
            await r.close()

    run(main)
