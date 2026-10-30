"""The runner end to end against the fake game: the probe, the driver's stop rules, pacing, the run output and the CLI.

The production client, loop, choices, probe, driver and trace writer run for real; only S1 (a scripted port
or a fake `send`) and the game (a protocol peer) are fakes. No network, no Unity, no key. A wait choice's
sleep and the token budget's clock and sleep are fakes too, so nothing here really waits.

Two peers stand in for the game. `TransformsGame` runs the fake's own behaviors under the declared names
(`ALIASES`), enough for the driver's stop, pacing and help rules. `CatalogGame` serves the catalog the real
exporter wrote for the manifest and runs exactly the declaration a request names: the session's host seam.
`tests/test_runner_declarations.py` runs chords, mouse and gamepad values and skills through it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from catalog_game import CatalogGame
from loop_fakes import TOKEN, FakeHelp, FakeS1, FakeTime, GameThread, Sleeps, TransformsGame, fail, pick
from runner_manifest import action, hold, manifest
from s1_fakes import Send, approval, reply
from turn_trace_check import problems

from playtest.bench import call_budget
from playtest.contracts.bridge import ExecStatus, ExecutionRecord
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, ModelRef, RunRecord
from playtest.contracts.loop import TurnOutcome
from playtest.contracts.trace import TraceAuthor, TraceKind
from playtest.core.adherence import check_adherence
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.execution import Execution
from playtest.core.ports import S1Answer, S1Call
from playtest.core.run_record import build_run_record
from playtest.core.trace import TraceWriter, read_trace
from playtest.core.trace_replay import replay
from playtest.core.trace_sink import MemorySink
from playtest.preflight import catalog as exporter
from playtest.preflight.catalog import export
from playtest.runner import cli
from playtest.runner.choices import build_choices
from playtest.runner.output import secret_values
from playtest.runner.pacing import PacingStats, TokenBudget, wait_fact
from playtest.runner.probe import DisplacementProbe, make_snapshot
from playtest.runner.s1 import ARM as DEFAULT_ARM, SystemOneS1
from s1_fakes import ARM
from playtest.runner.session import run_session

OBJECTIVE = "Try each control once, then finish."


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    """No test sees, scans for or can print a credential from the machine's own environment, or a session catalog."""
    for name in [n for n in os.environ if n.endswith("_API_KEY")]:
        monkeypatch.delenv(name)
    for name in ("UPA_SESSION_CATALOG", "UPA_SESSION_CATALOG_SHA256"):
        monkeypatch.delenv(name, raising=False)


ALIASES = {"move": "increment", "look": "hold"}   # the fake world's own behavior for each declared name


def two_actions():
    """`move` and `look`, each a proven single-key declaration held for its manifest lease."""
    return manifest(
        action("move", "w", hold_s=0.2, holds=[hold(0.2)]),
        action("look", "a", hold_s=0.25, holds=[hold(0.25, mean=0.0, effective=False)]))


def harness_trace(sink: MemorySink | None = None) -> MemorySink:
    """An in-memory sink that opens with the harness's `run` entry, the way the file writer's trace does."""
    record = RunRecord(run_id="r1", game_project="game", clock_mode=ClockMode.realtime, models=[],
                       objective=OBJECTIVE, budget=Budget(), core_version="0.0.1")
    sink = sink if sink is not None else MemorySink()
    sink.append(TraceKind.run, record.model_dump(mode="json"), author=TraceAuthor.harness)
    return sink


def end_trace(sink: MemorySink, result) -> None:
    """The harness's closing entry, as `TraceWriter.close` writes it."""
    sink.append(TraceKind.end, {"status": result.status, "error": result.error, "turns": result.turns,
                                "redactions": 0}, author=TraceAuthor.harness)


class Ordered(MemorySink):
    """A memory sink that also notes each appended kind in a shared list, so a test can see what came before what."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def append(self, kind, body, **kwargs):
        self._events.append(TraceKind(kind).value)
        return super().append(kind, body, **kwargs)


async def session(client, built, **kwargs):
    """`run_session` with a sleep that returns at once, so a wait choice's duration costs no real time."""
    kwargs.setdefault("sleep", Sleeps())
    return await run_session(client, built, **kwargs)


def ticking(step: float = 1.0):
    """A clock that advances `step` seconds each time it is read, so a wall limit is reached in a set number of turns."""
    state = {"now": -step}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


async def connected(game_class=TransformsGame, **kwargs):
    if game_class is TransformsGame:
        kwargs.setdefault("aliases", ALIASES)
    game = game_class(token=TOKEN, **kwargs)
    port = await game.start()
    client = BridgeClient("127.0.0.1", port, TOKEN)
    await client.connect()
    return game, client


async def closed(game, client) -> None:
    try:
        await client.aclose()
    finally:
        await game.stop()


# --- the effect probe ---------------------------------------------------------------------


class Readings:
    """A snapshot source that yields scripted readings (or raises), and counts how often it is read."""

    def __init__(self, *items: Any) -> None:
        self._items = list(items)
        self.reads = 0

    async def __call__(self) -> dict[str, Any]:
        self.reads += 1
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def reading(seq: int, x: float, game_s: float) -> dict[str, Any]:
    return {"inspection_seq": seq, "position": [x, 0.0, 0.0], "game_time_s": game_s}


def terminal(duration: float | None = 0.21) -> Execution:
    wall = datetime(2026, 10, 28, tzinfo=timezone.utc)
    record = ExecutionRecord(session="s", seq=3, game_s=1.0, wall_utc=wall, action_id="a", status=ExecStatus.completed,
                             applied_duration_s=duration, release_verified=True)
    return Execution(request={}, records=(record,))


def probe_for(readings: Readings, expected=None):
    return DisplacementProbe(readings, {"move": expected})


EXPECTED = {"source": "calibration", "control": "w", "requested_hold_s": 0.2, "net_displacement": 6.04,
            "sample_variance": 0.0001, "valid_trials": 2, "effective_in_all_repeats": True, "unit": "world units"}


def test_the_probe_reports_expected_and_observed_displacement_around_one_input():
    async def main():
        readings = Readings(reading(10, 1.0, 5.0), reading(12, 7.12, 6.5))
        probe = probe_for(readings, EXPECTED)
        move = build_choices(manifest(action("move", holds=[hold(0.2)]))).choices[0]
        await probe.before_turn(3)
        assert readings.reads == 1                     # one reading before the turn's observation...
        report = await probe.measure(move, terminal())
        assert readings.reads == 2                     # ...and one right after the terminal record, no others
        assert report.expected == EXPECTED
        assert report.observed["net_displacement"] == pytest.approx(6.12)
        assert (report.observed["from_inspection_seq"], report.observed["to_inspection_seq"]) == (10, 12)
        assert report.observed["window_game_s"] == pytest.approx(1.5) and report.observed["applied_duration_s"] == 0.21
        assert report.fact == ("move: net displacement between inspection seq 10 and 12: 6.120 world units; "
                               "calibrated for a 0.2-second hold: 6.040 (sample variance 0.000100, n=2).")

    asyncio.run(main())


def test_a_reading_that_cannot_be_taken_leaves_the_observed_value_null_not_zero():
    async def main():
        move = build_choices(manifest(action("move", holds=[hold(0.2)]))).choices[0]
        failed = Readings(ValueError("the selected entity is missing from the transform snapshot"), reading(2, 5.0, 1.0))
        probe = probe_for(failed, EXPECTED)
        await probe.before_turn(0)
        report = await probe.measure(move, terminal())
        assert report.observed is None and report.expected == EXPECTED
        assert report.fact == "move: net displacement was not measured (the selected entity is missing from the transform snapshot)."
        missing_after = probe_for(Readings(reading(1, 0.0, 0.0), ValueError("transform snapshot has no objects list")))
        await missing_after.before_turn(0)
        report = await missing_after.measure(move, terminal())
        assert report.observed is None and report.expected is None and "not measured" in report.fact

    asyncio.run(main())


def test_an_input_without_a_calibrated_value_still_reports_what_was_observed():
    async def main():
        probe = probe_for(Readings(reading(1, 0.0, 0.0), reading(2, 3.0, 1.0)), None)
        move = build_choices(manifest(action("move", holds=[hold(0.2)]))).choices[0]
        await probe.before_turn(0)
        report = await probe.measure(move, terminal(duration=None))
        assert report.expected is None and report.observed["net_displacement"] == 3.0
        assert report.observed["applied_duration_s"] is None
        assert report.fact.endswith("3.000 world units; no calibrated effect is recorded for this declaration.")

    asyncio.run(main())


def test_a_closed_bridge_is_not_swallowed_by_the_probe():
    async def main():
        probe = probe_for(Readings(BridgeError("closed", "connection is closed")))
        with pytest.raises(BridgeError):
            await probe.before_turn(0)

    asyncio.run(main())


def test_the_live_probe_measures_a_real_displacement_on_the_protocol_peer():
    async def main():
        game, client = await connected()
        try:
            snapshot = make_snapshot(client, "entity")
            before = await snapshot()
            assert before["position"] == [0.0, 0.0, 0.0] and isinstance(before["inspection_seq"], int)
            built = build_choices(two_actions())
            probe = DisplacementProbe(snapshot, built.expected)
            await probe.before_turn(0)
            observation = await client.observe_once()
            records = await client.act(action_id="a1", choice_id="move", action="increment", params={},
                                       based_on_seq=observation.seq, max_age_s=2.0, lease_s=0.2)
            report = await probe.measure(built.choices[0], Execution(request={}, records=tuple(records)))
            assert report.observed["net_displacement"] == pytest.approx(6.0)
            assert report.expected["net_displacement"] == 6.04
            assert report.observed["to_inspection_seq"] > report.observed["from_inspection_seq"]
        finally:
            await closed(game, client)

    asyncio.run(main())


# --- the session driver -------------------------------------------------------------------


def test_a_session_runs_manifest_actions_with_their_leases_and_finishes():
    async def main():
        value = two_actions()
        game, client = await connected(CatalogGame, catalog=export(value), effects={"move": (6.0, 0.0, 0.0)})
        built = build_choices(value)
        sink = harness_trace()
        try:
            s1 = FakeS1("move", "look", "wait", "finish")
            result = await session(
                client, built, s1=s1, objective=OBJECTIVE, probe=DisplacementProbe(make_snapshot(client, "entity"), built.expected),
                sink=sink, max_age_s=2.0, wall_limit_s=60.0)
            assert (result.status, result.ending, result.error) == ("complete", "finish", None)
            assert result.turns == 4 and result.outcomes == {"executed": 2, "finished": 2}
            assert result.stop is not None and result.stop.release_verified is True and result.adherence == []
            # The host ran the declarations S1 chose, under their own names.
            assert game.applied() == ["move", "look"]
            # Each input ran under the lease its manifest action declares, not the bridge's default.
            leases = {e["body"]["request"]["choice_id"]: e["body"]["request"]["lease_s"] for e in sink.of_kind("execution")}
            assert leases == {"move": 0.2, "look": 0.25}
            # The bridge applies exactly its catalog declaration; declared-value overrides are rejected.
            assert all(e["body"]["request"]["params"] == {} for e in sink.of_kind("execution"))
            assert [e["body"]["request"]["action"] for e in sink.of_kind("execution")] == ["move", "look"]
            effects = {e["body"]["choice_id"]: e["body"] for e in sink.of_kind("effect")}
            assert effects["move"]["observed"]["net_displacement"] == pytest.approx(6.0)
            assert effects["move"]["expected"]["net_displacement"] == 6.04
            assert effects["look"]["observed"]["net_displacement"] == pytest.approx(0.0)
            assert any(f.startswith("move: net displacement between inspection seq") for f in s1.contexts[1]["facts"])
            assert s1.contexts[0]["objective"] == OBJECTIVE
            assert [c["id"] for c in s1.contexts[0]["choices"]] == ["move", "look", "wait", "escalate", "delegate", "finish"]
            end_trace(sink, result)
            assert problems(sink.entries) == []
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_the_wall_clock_limit_ends_a_run_normally():
    async def main():
        game, client = await connected()
        try:
            result = await session(client, build_choices(two_actions()), s1=FakeS1(*["wait"] * 50),
                                       objective=OBJECTIVE, wall_limit_s=3.0, clock=ticking())
            assert (result.status, result.ending) == ("complete", "wall_limit") and result.turns == 2
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_consecutive_provider_failures_stop_the_run_and_a_good_turn_resets_the_count():
    async def main():
        game, client = await connected()
        try:
            steps = [fail("transport error"), fail("transport error"), "wait", fail("transport error"),
                     fail("HTTP error (HTTP 500)"), fail("transport error"), "wait"]
            sink = harness_trace()
            result = await session(client, build_choices(two_actions()), s1=FakeS1(*steps), objective=OBJECTIVE,
                                       sink=sink, wall_limit_s=60.0)
            assert (result.status, result.ending) == ("stopped", "provider_failures")
            assert result.turns == 6 and result.provider_failures == 5      # the wait at turn 2 reset the streak
            assert result.error == "3 consecutive provider failures"
            assert game.applied() == [] and result.stop.release_verified is True
            stop = sink.of_kind("stop")[0]["body"]
            assert stop["reason"] == "error" and stop["error"] == "3 consecutive provider failures"
            assert [t.outcome for t in result.loop.turns].count(TurnOutcome.rejected_invalid) == 5
        finally:
            await closed(game, client)

        game, client = await connected()
        try:
            steps = [fail("transport error"), fail("transport error"), "wait", fail("transport error"),
                     fail("transport error"), "finish"]
            result = await session(client, build_choices(two_actions()), s1=FakeS1(*steps), objective=OBJECTIVE,
                                       wall_limit_s=60.0)
            assert (result.status, result.ending, result.turns) == ("complete", "finish", 6)
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_a_failure_limit_of_one_stops_at_the_first_failure():
    async def main():
        game, client = await connected()
        try:
            result = await session(client, build_choices(two_actions()), s1=FakeS1(fail("x"), "wait"),
                                       objective=OBJECTIVE, max_provider_failures=1)
            assert (result.ending, result.turns) == ("provider_failures", 1)
            with pytest.raises(ValueError):
                await session(client, build_choices(two_actions()), s1=FakeS1(), objective=OBJECTIVE,
                                  max_provider_failures=0)
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_a_port_that_asks_to_halt_stops_the_run_after_recording_its_turn():
    async def main():
        game, client = await connected()
        try:
            halt = S1Answer(answer=None, calls=(S1Call(problem="the run's call cap was reached"),),
                            failure="the run's call cap was reached", halt="the run's call cap was reached")
            result = await session(client, build_choices(two_actions()), s1=FakeS1("wait", halt, "wait"),
                                       objective=OBJECTIVE, wall_limit_s=60.0)
            assert (result.status, result.ending, result.turns) == ("stopped", "halt", 2)
            assert result.error == "the run's call cap was reached"
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_a_lost_bridge_ends_the_run_inconclusive_with_the_cause_and_still_stops():
    async def main():
        game, client = await connected()
        sink = harness_trace()
        try:
            def cut(_context):
                client._writer.close()          # an abrupt disconnect: no stop is sent
                return "wait"

            result = await session(client, build_choices(two_actions()), s1=FakeS1("look", cut, "wait"),
                                       objective=OBJECTIVE, sink=sink, max_age_s=2.0, wall_limit_s=60.0)
            assert (result.status, result.ending) == ("inconclusive", "error")
            assert "BridgeError" in result.error and "input release was not verified" in result.error
            assert result.stop is None
            stop = sink.of_kind("stop")[0]["body"]
            assert stop["reason"] == "error" and stop["result"] is None and "BridgeError" in stop["error"]
            for _ in range(200):                # the bridge releases what was held when the connection drops
                if ("held", "disconnect") in game.releases():
                    break
                await asyncio.sleep(0.01)
            assert ("held", "disconnect") in game.releases()
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_an_input_whose_record_never_arrives_is_traced_then_stopped_and_released():
    async def main():
        game = TransformsGame(token=TOKEN, action_run_s=5.0, action_stall=True, aliases=ALIASES)    # accepts, never completes
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN, timeout_s=0.3)
        await client.connect()
        sink = harness_trace()
        try:
            result = await session(client, build_choices(two_actions()), s1=FakeS1("look", "wait"),
                                       objective=OBJECTIVE, sink=sink, max_age_s=2.0, wall_limit_s=60.0)
            assert (result.status, result.ending) == ("inconclusive", "error") and result.turns == 0
            assert "BridgeError" in result.error and "timeout" in result.error
            # The request that may have reached the bridge stays in the trace, with no records to show.
            sent = sink.of_kind("execution")[0]["body"]
            assert sent["request"]["choice_id"] == "look" and sent["records"] == []
            # The stop released and verified the input, but a run that lost an input's record is not complete.
            assert result.stop is not None and result.stop.release_verified is True
            assert sink.of_kind("stop")[0]["body"]["result"]["release_verified"] is True
            assert [t for t in problems(sink.entries) if "stops before its turn entry" in t]    # reported, not skipped
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_an_escalation_with_no_helper_stays_pending_until_the_run_stops_and_is_cancelled_there():
    async def main():
        game, client = await connected()
        sink = harness_trace()
        try:
            result = await session(client, build_choices(two_actions()),
                                       s1=FakeS1(pick("escalate"), "wait", "wait", "finish"), objective=OBJECTIVE, sink=sink,
                                       wall_limit_s=60.0)
            assert (result.status, result.ending) == ("complete", "finish")
            assert [e["body"]["event"] for e in sink.of_kind("help")] == ["opened", "cancelled"]
            assert result.loop.helper_count == 0 and result.loop.cancelled_helpers == 1
            end_trace(sink, result)
            assert problems(sink.entries) == []
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_help_that_finishes_is_adopted_and_shown_on_a_session():
    async def main():
        game, client = await connected()
        helper = FakeHelp(result={"advice": "try look"})
        helper.release.set()
        try:
            s1 = FakeS1(pick("escalate"), "wait", "wait", "finish")
            result = await session(client, build_choices(two_actions()), s1=s1, objective=OBJECTIVE, help=helper,
                                       wall_limit_s=60.0)
            assert result.status == "complete" and len(helper.requests) == 1
            assert result.loop.adoptions == [{"advice": "try look"}]
            # The helper finishes while turn 1 awaits the bridge, so it is adopted at the top of turn 2.
            assert s1.contexts[1]["pending_help"] is not None and "guidance" not in s1.contexts[1]
            assert s1.contexts[2]["pending_help"] is None and s1.contexts[2]["guidance"][0]["source"] == "fake-helper"
        finally:
            await closed(game, client)

    asyncio.run(main())


# --- pacing and the wait choice -----------------------------------------------------------


def test_a_wait_choice_sleeps_its_duration_and_sends_no_input():
    async def main():
        game, client = await connected()
        sleeps = Sleeps()
        try:
            s1 = FakeS1("wait", "move", "wait", "finish")
            result = await session(client, build_choices(two_actions()), s1=s1, objective=OBJECTIVE,
                                   max_age_s=2.0, wall_limit_s=60.0, sleep=sleeps)
            assert (result.status, result.ending, result.turns) == ("complete", "finish", 4)
            assert sleeps.calls == [0.5, 0.5]                    # one half second per wait turn, none for the rest
            assert game.applied() == ["increment"] and [t.executed_action for t in result.loop.turns] == [None, "move", None, None]
            wait = next(c for c in s1.contexts[0]["choices"] if c["id"] == "wait")
            assert wait["label"] == "Wait 0.5 s with no input" and wait["params"] == {"duration_game_s": 0.5}
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_the_session_paces_before_each_turn_observes_and_tells_s1_about_a_real_wait_once():
    async def main():
        game, client = await connected()
        events: list[str] = []
        fake = FakeTime()

        async def sleep(seconds):                    # the budget's wait, noted among the trace entries
            events.append("paced")
            await fake.sleep(seconds)

        budget = TokenBudget(10_000, clock=fake.clock, sleep=sleep)

        class Probe:
            async def before_turn(self, turn):
                events.append("probe")

            async def measure(self, choice, execution):
                return None

        def spend(tokens, answer):
            def step(_context):
                events.append("s1")
                budget.record(tokens)                # what a call reports to the budget
                return answer
            return step

        sink = harness_trace(Ordered(events))
        try:
            s1 = FakeS1(spend(6000, "look"), spend(1000, "move"), spend(1000, "finish"))
            result = await session(client, build_choices(two_actions()), s1=s1, objective=OBJECTIVE, probe=Probe(),
                                   sink=sink, max_age_s=2.0, wall_limit_s=60.0, budget=budget)
            assert (result.status, result.ending, result.turns) == ("complete", "finish", 3)
            # Turn 1 would put 6,000 + 6,000 into a 10,000 minute, so the session waited for turn 0's call to leave it.
            assert fake.slept == [60.0] and events.count("paced") == 1
            at = events.index("paced")
            assert events[at - 1] == "turn"                       # the previous turn had closed...
            assert events[at + 1:at + 3] == ["probe", "observation"]   # ...and the next one began, and observed, after it
            observing = False                                     # no wait ever falls between an observation and its turn,
            for event in events:                                  # so pacing never ages the observation an action is bound to
                observing = True if event == "observation" else False if event == "turn" else observing
                assert not (observing and event == "paced")
            assert result.pacing == PacingStats(waited_s=60.0, longest_wait_s=60.0, turns_waited=1)
            # S1 reads the measured wait on that turn's context, and on no other.
            line = wait_fact(60.0)
            assert line == "Before this observation the harness waited 60.00 s to stay under the provider's token rate limit."
            assert [line in c["facts"] for c in s1.contexts] == [False, True, False]
            assert s1.contexts[1]["facts"][-1] == line
            traced = [e["body"]["payload"]["facts"] for e in sink.of_kind("context")]
            assert [line in facts for facts in traced] == [False, True, False]
            # The wait changed nothing S1 chose or what ran.
            assert [(t.raw_choice, t.effective_choice, t.executed_action) for t in result.loop.turns] == [
                ("look", "look", "look"), ("move", "move", "move"), ("finish", "finish", None)]
            assert game.applied() == ["hold", "increment"] and check_adherence(result.loop.turns) == []
            end_trace(sink, result)
            assert problems(sink.entries) == []
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_pacing_is_a_delay_only_it_never_opens_help_or_changes_what_s1_chose():
    """A budget that makes every later turn wait: no helper is asked, nothing is substituted, suppressed or escalated."""
    async def main():
        game, client = await connected()
        fake = FakeTime()
        budget = TokenBudget(5000, clock=fake.clock, sleep=fake.sleep)
        helper = FakeHelp()
        sink = harness_trace()

        def spend(answer):
            def step(_context):
                budget.record(6000)                  # a call over the limit by itself, so every later turn waits a minute
                return answer
            return step

        try:
            s1 = FakeS1(spend("move"), spend("wait"), spend("look"), spend("wait"), spend("finish"))
            result = await session(client, build_choices(two_actions()), s1=s1, objective=OBJECTIVE, help=helper,
                                   sink=sink, max_age_s=2.0, wall_limit_s=600.0, budget=budget)
            assert (result.status, result.ending, result.turns) == ("complete", "finish", 5)
            assert result.pacing == PacingStats(waited_s=240.0, longest_wait_s=60.0, turns_waited=4)
            assert helper.requests == [] and sink.of_kind("help") == [] and result.loop.helper_count == 0
            assert all(c["pending_help"] is None for c in s1.contexts)
            assert [wait_fact(60.0) in c["facts"] for c in s1.contexts] == [False, True, True, True, True]
            assert [(t.raw_choice, t.effective_choice, t.executed_action) for t in result.loop.turns] == [
                ("move", "move", "move"), ("wait", "wait", None), ("look", "look", "look"), ("wait", "wait", None),
                ("finish", "finish", None)]
            assert game.applied() == ["increment", "hold"] and check_adherence(result.loop.turns) == []
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_a_session_without_a_budget_does_not_pace_and_says_nothing_extra():
    async def main():
        game, client = await connected()
        try:
            s1 = FakeS1("wait", "finish")
            result = await session(client, build_choices(two_actions()), s1=s1, objective=OBJECTIVE, wall_limit_s=60.0)
            assert result.pacing is None
            assert all("harness waited" not in fact for c in s1.contexts for fact in c["facts"])
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_the_wall_limit_is_checked_again_after_a_pacing_wait():
    async def main():
        game, client = await connected()
        fake = FakeTime()
        budget = TokenBudget(10_000, clock=fake.clock, sleep=fake.sleep)
        budget.record(9000)                              # the minute is already full, so the first turn waits
        times = iter([0.0, 1.0, 100.0])                  # the deadline is set; the loop starts; the wait used the time up
        try:
            result = await session(client, build_choices(two_actions()), s1=FakeS1("wait"), objective=OBJECTIVE,
                                   wall_limit_s=10.0, clock=lambda: next(times), budget=budget)
            assert (result.status, result.ending, result.turns) == ("complete", "wall_limit", 0)
            assert fake.slept == [60.0] and result.pacing == PacingStats(60.0, 60.0, 1)
            assert game.applied() == [] and result.stop is not None and result.stop.release_verified is True
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_a_rate_limited_turn_is_rejected_without_a_retry_and_three_in_a_row_end_the_run(tmp_path: Path):
    async def main():
        game, client = await connected()
        limited = (429, None, 5.0, "HTTP error")
        send = Send(tmp_path, limited, limited, limited)
        fake = FakeTime()
        budget = TokenBudget(85_000, clock=fake.clock, sleep=fake.sleep)
        s1 = SystemOneS1(call_budget.Ledger(tmp_path, approval(12), ARM), max_calls=10, send=send, arm=ARM, budget=budget)
        sink = harness_trace()
        try:
            result = await session(client, build_choices(two_actions()), s1=s1, objective=OBJECTIVE, sink=sink,
                                   wall_limit_s=60.0, budget=budget)
            assert (result.status, result.ending, result.turns) == ("stopped", "provider_failures", 3)
            assert result.provider_failures == 3 and len(send.bodies) == 3     # one request a turn: nothing was retried
            assert [t.outcome for t in result.loop.turns] == [TurnOutcome.rejected_invalid] * 3
            assert [e["body"]["http_status"] for e in sink.of_kind("s1_call")] == [429, 429, 429]
            assert game.applied() == [] and result.stop.release_verified is True
            assert (budget.recorded, budget.estimated) == (3, 3)               # a 429 has no usage: counted as estimates
            assert fake.slept == [] and result.pacing == PacingStats(0.0, 0.0, 0)
            end_trace(sink, result)
            assert problems(sink.entries) == []
        finally:
            await closed(game, client)

    asyncio.run(main())


# --- run output ---------------------------------------------------------------------------


def test_the_trace_file_opens_with_the_run_record_closes_with_end_and_never_holds_a_secret(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-secret-value-1234")
    monkeypatch.setenv("SHORT_API_KEY", "short")        # too short to be a credential worth scanning for
    assert secret_values() == ["sk-test-secret-value-1234"]
    path = tmp_path / "trace.jsonl"

    async def main():
        game, client = await connected()
        record = build_run_record(
            game_project=tmp_path / "game", status=client.status, scene="main", manifest_fingerprint="f" * 64,
            clock_mode=ClockMode.realtime, input_mode="input_system",
            models=[ModelRef(role="s1", provider="fake", model="fake-1", prompt_hash="0" * 64)], objective=OBJECTIVE,
            budget=Budget(max_wall_s=60.0), reset_method="none")
        writer = TraceWriter(path, record, secrets=secret_values())
        try:
            echo = S1Answer(answer={"choice_id": "move", "set_version": 0},
                            calls=(S1Call(question="next_action", response={"echoed": "sk-test-secret-value-1234"}),))
            result = await session(client, build_choices(two_actions()), s1=FakeS1(lambda _c: echo, "finish"),
                                   objective=OBJECTIVE, sink=writer, max_age_s=2.0, wall_limit_s=60.0)
            writer.close(result.status, result.error)
            return writer, result
        finally:
            await closed(game, client)

    writer, result = asyncio.run(main())
    text = path.read_text(encoding="utf-8")
    assert "sk-test-secret-value-1234" not in text and "[redacted]" in text
    entries = read_trace(path)
    assert entries[0].kind is TraceKind.run and entries[-1].kind is TraceKind.end
    assert entries[-1].body == {"status": "complete", "error": None, "turns": 2, "redactions": 1} and writer.redactions == 1
    assert [e.kind for e in entries].count(TraceKind.end) == 1 and entries[-2].kind is TraceKind.stop
    assert [e.trace_seq for e in entries] == list(range(len(entries)))
    assert replay(entries) == []
    with pytest.raises(FileExistsError):
        TraceWriter(path, RunRecord.model_validate(entries[0].body))      # a trace is never overwritten
    assert result.status == "complete"


# --- the command line ---------------------------------------------------------------------


class Project:
    """A scratch Unity project directory whose bridge lock advertises the fake game, plus every input file."""

    def __init__(self, tmp_path: Path, port: int, *, actions=None, entity_id: str = "entity", calls: int = 12,
                 git: bool = False) -> None:
        self.root = tmp_path
        self.project = tmp_path / "game"
        (self.project / "Temp").mkdir(parents=True)
        (self.project / "Temp" / "playtest-bridge.lock").write_text(json.dumps({
            "protocol": "upa/1", "pid": 4242, "port": port, "token": TOKEN, "started_utc": "2026-10-28T00:00:00Z"}),
            encoding="utf-8")
        if git:    # a committed project whose `Temp/` is ignored, as a Unity project's is: the worktree is clean
            run = dict(check=True, capture_output=True)
            subprocess.run(["git", "init", str(self.project)], **run)
            (self.project / ".gitignore").write_text("Temp/\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(self.project), "add", ".gitignore"], **run)
            subprocess.run(["git", "-C", str(self.project), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                            "-c", "commit.gpgsign=false", "commit", "-m", "initial"], **run)
            self.commit = subprocess.run(["git", "-C", str(self.project), "rev-parse", "HEAD"], text=True,
                                         **run).stdout.strip()
        built = actions if actions is not None else two_actions()
        self.manifest = tmp_path / "manifest.json"
        self.manifest.write_text(built.model_dump_json(), encoding="utf-8")
        # The session's catalog, written by the same exporter `run-session.ps1` runs, and the digest it pins.
        self.catalog = tmp_path / "catalog.json"
        assert exporter.main(["--manifest", str(self.manifest), "--output", str(self.catalog)]) == 0
        self.catalog_sha = hashlib.sha256(self.catalog.read_bytes()).hexdigest()
        self.entity = tmp_path / "entity.json"
        self.entity.write_text(json.dumps({"status": "identified", "entity_id": entity_id}), encoding="utf-8")
        self.calibration = tmp_path / "calibration.json"
        self.calibration.write_text(json.dumps({"entity_id": entity_id}), encoding="utf-8")
        self.approval = tmp_path / "approval.json"
        self.approval.write_text(json.dumps(approval(calls)), encoding="utf-8")
        self.out = tmp_path / "out"
        self.summary = self.out / "run.json"

    def args(self, **overrides: Any) -> list[str]:
        values: dict[str, Any] = {
            "project": self.project, "entity": self.entity, "calibration": self.calibration, "output": self.summary,
            "manifest": self.manifest, "catalog": self.catalog, "catalog-sha256": self.catalog_sha,
            "approval": self.approval, "max-calls": 6, "wall-limit-s": 60, "max-age-s": 2.0, "objective": OBJECTIVE,
            "s1-arm": ARM}  # the Baseten arm these runs are written for; the default arm is checked separately
        values.update({key.replace("_", "-"): value for key, value in overrides.items()})
        flags: list[str] = []
        for key, value in values.items():
            if value is not None:
                flags += [f"--{key}", str(value)]
        return flags


def with_fake_send(monkeypatch, send):
    real = cli.SystemOneS1
    monkeypatch.setattr(cli, "SystemOneS1", lambda ledger, **kwargs: real(ledger, send=send, **kwargs))


def test_the_cli_runs_a_manifest_session_end_to_end_and_leaves_a_clean_record(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0001")
    with GameThread(CatalogGame, catalog=export(two_actions()), effects={"move": (6.0, 0.0, 0.0)}) as thread:
        files = Project(tmp_path, thread.port, git=True)
        send = Send(files.out, reply("next_action", "move"), reply("next_action", "look"), reply("next_action", "finish"))
        with_fake_send(monkeypatch, send)
        assert cli.main(files.args(tokens_per_minute=60000)) == 0
        assert thread.game.applied() == ["move", "look"]            # the host ran the declarations S1 named
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    assert (summary["status"], summary["ending"], summary["turns"]) == ("complete", "finish", 3)
    assert summary["adherence"] == {"rate": 1.0, "violations": []} and summary["stop"]["release_verified"] is True
    assert summary["s1"]["calls"] == 3 and summary["s1"]["call_cap"] == 6 and summary["manifest"]["not_offered"] == []
    assert summary["manifest"]["offered"] == ["move", "look", "wait", "escalate", "delegate", "finish"]
    assert summary["manifest"]["catalog_sha256"] == files.catalog_sha     # the run says which catalog the host served
    assert summary["game_worktree_changed"] is False and summary["provider_failures"] == 0
    # The trace is the file writer's: read back strictly and replayed, it has nothing to report.
    path = files.out / "trace.jsonl"
    entries = read_trace(path)
    assert replay(entries) == []
    assert problems([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]) == []
    assert entries[0].kind is TraceKind.run and [e.kind for e in entries].count(TraceKind.end) == 1
    assert [e.kind for e in entries[-2:]] == [TraceKind.stop, TraceKind.end]       # the end follows the loop's stop
    assert entries[-1].body == {"status": "complete", "error": None, "turns": 3, "redactions": 0}
    assert summary["trace"] == "trace.jsonl" and summary["redactions"] == 0
    # The run record is built from what this process measured.
    run_record = RunRecord.model_validate(entries[0].body)
    assert run_record.models[0] == ModelRef(role="s1", provider="baseten", model="decision-models/baseten-mammoth-27b-latest",
                                            prompt_hash=summary["s1"]["prompt_sha256"])
    assert run_record.objective == OBJECTIVE and run_record.clock_mode is ClockMode.realtime
    assert (run_record.game_project, run_record.game_commit, run_record.game_worktree) == ("game", files.commit, "clean")
    assert (run_record.scene, run_record.input_mode, run_record.reset_method) == ("main", "input_system", "none")
    assert (run_record.unity_version, run_record.bridge_version) == ("6000.0.0f1", "0.0.1-fake")
    assert run_record.budget == Budget(max_wall_s=60.0, max_usd=0.01, max_frontier_calls=0)
    # The fingerprint is the manifest file's own SHA-256, not the preflight cache's authored-structure fingerprint.
    assert run_record.manifest_fingerprint == hashlib.sha256(files.manifest.read_bytes()).hexdigest()
    assert run_record.manifest_fingerprint == summary["manifest"]["sha256"]
    assert str(tmp_path) not in entries[0].model_dump_json()                       # no absolute path in the record
    assert len(list((files.out / "calls").glob("*.json"))) == 3 and (files.out / "plan.json").is_file()
    plan = json.loads((files.out / "plan.json").read_text(encoding="utf-8"))
    assert plan["max_calls"] == 6 and plan["offered"][:2] == ["move", "look"] and plan["catalog_sha256"] == files.catalog_sha
    # Pacing: the rate is in the plan and the summary, and a short run never waited. The fake replies carry
    # no input token count, so every call was counted as an estimate.
    assert plan["tokens_per_minute"] == summary["settings"]["tokens_per_minute"] == 60000
    assert summary["pacing"] == {"waited_s": 0.0, "longest_wait_s": 0.0, "turns_waited": 0,
                                 "calls_counted": 3, "calls_estimated": 3}
    everything = "".join(p.read_text(encoding="utf-8") for p in files.out.rglob("*") if p.is_file())
    assert "sk-test-cli-key-0001" not in everything and "sk-test-cli-key-0001" not in capsys.readouterr().out


def test_the_cli_stops_after_consecutive_provider_failures_and_exits_non_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0002")
    with GameThread(CatalogGame, catalog=export(two_actions())) as thread:
        files = Project(tmp_path, thread.port)
        limited = (429, None, 5.0, "HTTP error")
        send = Send(files.out, limited, limited, limited)
        with_fake_send(monkeypatch, send)
        assert cli.main(files.args(**{"max_provider_failures": 3})) == 1
        assert thread.game.applied() == []
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    assert (summary["status"], summary["ending"], summary["turns"]) == ("stopped", "provider_failures", 3)
    assert summary["stop"]["release_verified"] is True and summary["s1"]["calls"] == 3 and len(send.bodies) == 3
    assert summary["pacing"]["calls_counted"] == 3 == summary["pacing"]["calls_estimated"]
    entries = read_trace(files.out / "trace.jsonl")
    assert replay(entries) == [] and entries[-1].body["status"] == "stopped"
    # Nothing measured the project's commit or worktree (it is not a repository), so the record says unknown.
    record = RunRecord.model_validate(entries[0].body)
    assert (record.game_commit, record.game_worktree) == ("unknown", "unknown")
    assert summary["game_worktree_changed"] is None


def test_an_interrupt_still_stops_the_loop_and_closes_the_trace_once(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0007")

    class Interrupted(BaseException):                   # not an Exception, as an interrupt is not
        pass

    def interrupt(body):
        raise Interrupted("stop now")

    with GameThread(CatalogGame, catalog=export(two_actions())) as thread:
        files = Project(tmp_path, thread.port)
        with_fake_send(monkeypatch, Send(files.out, reply("next_action", "move"), interrupt))
        with pytest.raises(Interrupted):
            cli.main(files.args())
        assert thread.game.applied() == ["move"]
    entries = read_trace(files.out / "trace.jsonl")
    assert [e.kind for e in entries].count(TraceKind.end) == 1 and [e.kind for e in entries[-2:]] == [TraceKind.stop, TraceKind.end]
    assert entries[-2].body["reason"] == "error" and entries[-2].body["error"] == "interrupted"
    assert entries[-2].body["result"]["release_verified"] is True            # every held input was released first
    assert entries[-1].body == {"status": "inconclusive", "error": "Interrupted: stop now", "turns": 1, "redactions": 0}
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    assert (summary["status"], summary["error"]) == ("inconclusive", "Interrupted: stop now")


def test_the_token_rate_defaults_to_85000_and_takes_the_environment_then_the_flag(monkeypatch):
    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    monkeypatch.delenv("UPA_RUN_TOKENS_PER_MINUTE", raising=False)
    assert cli._parser().parse_args(required).tokens_per_minute == 85_000
    monkeypatch.setenv("UPA_RUN_TOKENS_PER_MINUTE", "70000")
    assert cli._parser().parse_args(required).tokens_per_minute == 70_000
    assert cli._parser().parse_args([*required, "--tokens-per-minute", "50000"]).tokens_per_minute == 50_000
    monkeypatch.setenv("UPA_RUN_TOKENS_PER_MINUTE", "lots")
    with pytest.raises(SystemExit):
        cli._parser()


def test_the_age_limit_defaults_to_the_measured_decision_latency_budget(monkeypatch):
    # 0.5 s sat below S1's measured decision latency and left it unable to act (#132).
    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    monkeypatch.delenv("UPA_RUN_MAX_AGE_S", raising=False)
    assert cli._parser().parse_args(required).max_age_s == 0.8
    script = (Path(cli.__file__).resolve().parents[3] / "scripts/unity/run-session.ps1").read_text(encoding="utf-8")
    assert "[double]$MaxAgeSeconds = 0.8," in script


def test_the_frontier_switch_takes_the_environment_and_rejects_anything_but_on_or_off(monkeypatch):
    # The PlayMode host passes only paths on the command line, so a hosted session turns the frontier on this way.
    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    monkeypatch.delenv("UPA_RUN_FRONTIER", raising=False)
    assert cli._parser().parse_args(required).frontier is False
    assert cli._parser().parse_args([*required, "--frontier"]).frontier is True
    for raw, expected in (("1", True), ("true", True), ("0", False), ("false", False)):
        monkeypatch.setenv("UPA_RUN_FRONTIER", raw)
        assert cli._parser().parse_args(required).frontier is expected
    monkeypatch.setenv("UPA_RUN_FRONTIER", "yes")
    with pytest.raises(SystemExit):
        cli._parser()


def test_the_cli_refuses_to_start_without_the_approval_the_cap_or_the_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0003")
    files = Project(tmp_path, 1)
    for overrides in ({"approval": None}, {"max_calls": None}, {"manifest": None}, {"tokens_per_minute": 0},
                      {"catalog": None}, {"catalog_sha256": None}):
        with pytest.raises(SystemExit) as refused:
            cli.main(files.args(**overrides))
        assert refused.value.code == 2
    monkeypatch.delenv("BASETEN_API_KEY")
    with pytest.raises(SystemExit) as no_key:
        cli.main(files.args())
    assert no_key.value.code == 2
    assert not files.out.exists()                      # nothing was created, nothing was contacted


def test_the_s1_arm_defaults_to_mammoth_takes_the_environment_and_refuses_an_unknown_one(tmp_path: Path, monkeypatch):
    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    monkeypatch.delenv("UPA_RUN_S1_ARM", raising=False)
    assert cli._parser().parse_args(required).s1_arm == DEFAULT_ARM == "liquid-d1-ultra"   # #100
    monkeypatch.setenv("UPA_RUN_S1_ARM", "liquid-d1-ultra")
    assert cli._parser().parse_args(required).s1_arm == "liquid-d1-ultra"
    assert cli._parser().parse_args([*required, "--s1-arm", "elephant-1"]).s1_arm == "elephant-1"
    monkeypatch.delenv("UPA_RUN_S1_ARM")
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0010")
    files = Project(tmp_path, 1)
    with pytest.raises(SystemExit) as unknown:
        cli.main([*files.args(), "--s1-arm", "no-such-model"])
    assert unknown.value.code == 2 and not files.out.exists()


def test_a_liquid_arm_needs_its_own_key_and_endpoint_not_basetens(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0011")
    monkeypatch.delenv("LIQUID_API_KEY", raising=False)
    monkeypatch.setenv("LIQUID_D1.1_URL", "https://example.invalid/decide")
    files = Project(tmp_path, 1)
    with pytest.raises(SystemExit) as refused:
        cli.main([*files.args(), "--s1-arm", "liquid-d1-ultra"])
    assert refused.value.code == 2 and "LIQUID_API_KEY is not set" in capsys.readouterr().err
    assert not files.out.exists()                      # nothing was created, nothing was contacted


def test_the_cli_refuses_an_output_inside_the_project_or_over_existing_evidence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0004")
    files = Project(tmp_path, 1)
    with pytest.raises(SystemExit) as inside:
        cli.main(files.args(output=files.project / "run.json"))
    assert inside.value.code == 2
    files.out.mkdir()
    files.summary.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as existing:
        cli.main(files.args())
    assert existing.value.code == 2 and files.summary.read_text(encoding="utf-8") == "{}"


def test_the_cli_refuses_inputs_that_do_not_agree_before_any_call(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0005")
    files = Project(tmp_path / "a", 1, entity_id="someone-else")
    assert cli.main(files.args()) == 1                 # the manifest, entity and calibration disagree
    small = Project(tmp_path / "b", 1, calls=2)
    assert cli.main(small.args(max_calls=5)) == 1       # the approved envelope is smaller than the cap
    no_action = Project(tmp_path / "c", 1, actions=manifest(action("look", "a", supported=False, holds=[hold(0.2)])))
    assert cli.main(no_action.args()) == 1              # nothing the bridge can execute
    for case in (files, small, no_action):
        assert not (case.out / "plan.json").exists() and not (case.out / "calls").exists()


def test_the_cli_refuses_a_missing_altered_invalid_or_different_catalog_before_any_call(tmp_path: Path, monkeypatch, capsys):
    """Every way the host's catalog can fail to be the manifest's export ends the run before a plan, a call or a contact."""
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0008")
    strafe = manifest(action("strafe", ["w", "a"], hold_s=0.2, holds=[]))
    first_key_only = manifest(action("strafe", "w", hold_s=0.2, holds=[]))
    other = manifest(action("other", "s", holds=[hold(0.2)]))

    def repin(files: Project, text: str) -> None:
        files.catalog.write_text(text, encoding="utf-8")
        files.catalog_sha = hashlib.sha256(files.catalog.read_bytes()).hexdigest()

    def exported_text(value, where: Path) -> str:
        source, path = where / "m.json", where / "c.json"
        where.mkdir(parents=True)
        source.write_text(value.model_dump_json(), encoding="utf-8")
        assert exporter.main(["--manifest", str(source), "--output", str(path)]) == 0
        return path.read_text(encoding="utf-8")

    cases = {
        "missing": (strafe, lambda f: f.catalog.unlink(), "cannot read the session catalog"),
        "altered": (strafe, lambda f: f.catalog.write_text(f.catalog.read_text(encoding="utf-8") + " ", encoding="utf-8"),
                    "not the file that was exported"),
        "invalid": (strafe, lambda f: repin(f, "{ not json"), "not a valid action catalog"),
        "legacy": (strafe, lambda f: repin(f, json.dumps({"actions": {"preflight_key_w": {
            "backend": "input_system", "device": "Keyboard", "control": "w", "default_lease_s": 0.4}}})),
                   "not a valid action catalog"),
        "another manifest": (strafe, lambda f: repin(f, exported_text(other, tmp_path / "other-export")),
                             "'strafe' is supported by the manifest but the host does not serve it"),
        "chord cut to its first key": (strafe, lambda f: repin(f, exported_text(first_key_only, tmp_path / "cut-export")),
                                       "'strafe' differs from the manifest's declaration"),
    }
    for name, (built, change, phrase) in cases.items():
        files = Project(tmp_path / name.replace(" ", "-"), 1, actions=built)      # port 1: nothing listens
        change(files)
        capsys.readouterr()
        assert cli.main(files.args()) == 1, name
        assert phrase in capsys.readouterr().err, name
        assert not (files.out / "plan.json").exists() and not (files.out / "calls").exists(), name


def test_the_catalog_and_its_pin_take_the_session_variables_then_the_flags(monkeypatch):
    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    args = cli._parser().parse_args(required)
    assert args.catalog is None and args.catalog_sha256 is None          # no default catalog exists
    monkeypatch.setenv("UPA_SESSION_CATALOG", "from-env.json")
    monkeypatch.setenv("UPA_SESSION_CATALOG_SHA256", "a" * 64)
    args = cli._parser().parse_args(required)
    assert (args.catalog, args.catalog_sha256) == (Path("from-env.json"), "a" * 64)
    args = cli._parser().parse_args([*required, "--catalog", "flag.json", "--catalog-sha256", "b" * 64])
    assert (args.catalog, args.catalog_sha256) == (Path("flag.json"), "b" * 64)


def test_a_frontier_session_builds_its_helper_with_the_calibration_record(tmp_path: Path, monkeypatch):
    from playtest.frontier import provider as frontier_provider
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0001")
    monkeypatch.setenv("META_API_KEY", "sk-test-meta-key-0001")
    built: dict[str, Any] = {}
    real = cli.FrontierHelp

    def capture(**kwargs):
        built.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(cli, "FrontierHelp", capture)
    with GameThread(CatalogGame, catalog=export(two_actions()), effects={"move": (6.0, 0.0, 0.0)}) as thread:
        files = Project(tmp_path, thread.port, git=True)
        both = approval(12)
        both["arms"][frontier_provider.MODEL] = {**both["arms"][ARM], "max_calls": 1, "per_call_max_usd": 0.001}
        files.approval.write_text(json.dumps(both), encoding="utf-8")
        with_fake_send(monkeypatch, Send(files.out, reply("next_action", "finish")))
        assert cli.main([*files.args(tokens_per_minute=60000, max_frontier_calls=1), "--frontier"]) == 0
    assert built["calibration"] == json.loads(files.calibration.read_text(encoding="utf-8"))
