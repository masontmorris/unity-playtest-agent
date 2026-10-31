"""The S1-selected host restart in the real turn loop and session driver.

Fakes only: the bridge is `tests/fake_game.py`, the S1 is scripted, and the host operation is a port that
returns exactly the outcome a case needs. The tests check the S1 ownership rules: the restart is a primary
choice, no input is sent on its turn, no S1 call is made while the host operation waits, a failure ends the
run closed without replaying the next answer, and the restart keeps the run's own deadlines and budgets.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from fake_game import FakeGame
from loop_fakes import TOKEN, FakeHelp, FakeS1, FakeTime, Rig, choices, rig
from playtest.contracts.bridge import StopResult
from playtest.contracts.loop import Choice, OperationStatus, TurnOutcome
from playtest.contracts.trace import TraceEntry
from playtest.core.bridge_client import BridgeClient
from playtest.core.ports import RestartOutcome
from playtest.core.run_record import build_run_record
from playtest.core.trace_replay import replay
from playtest.core.trace_sink import MemorySink
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget
from playtest.runner.choices import ManifestChoices, RESTART_ID
from playtest.runner.pacing import TokenBudget
from playtest.runner.session import run_session
from turn_trace_check import problems as trace_problems

G0 = str(uuid.uuid4())
G1 = str(uuid.uuid4())
G2 = str(uuid.uuid4())


def restart_choice() -> Choice:
    return Choice(id=RESTART_ID, kind="restart_play_mode",
                  label="Restart Play Mode", source="core")


class FakeRestart:
    """The `RestartPort`: it records exactly what the loop asked and returns one explicit outcome."""

    def __init__(self, *, client: BridgeClient | None = None, observation: Any = None,
                 status: OperationStatus = OperationStatus.ready, reason: str | None = None) -> None:
        self.client, self.observation, self.status, self.reason = client, observation, status, reason
        self.calls: list[dict[str, Any]] = []

    async def restart(self, *, ticket_id: str, from_generation: str, previous_endpoint: Any,
                      deadline_s: float, release_verified: bool,
                      previous_session: str | None = None) -> RestartOutcome:
        self.calls.append({"ticket_id": ticket_id, "from_generation": from_generation,
                           "previous_endpoint": previous_endpoint, "deadline_s": deadline_s,
                           "release_verified": release_verified,
                           "previous_session": previous_session})
        if self.status is OperationStatus.ready:
            return RestartOutcome(status=OperationStatus.ready, ticket_id=ticket_id,
                                  from_generation=from_generation, to_generation=G1, checks=3,
                                  waited_s=0.5, release_verified=release_verified,
                                  client=self.client, observation=self.observation, scene_path="main")
        return RestartOutcome(status=self.status, ticket_id=ticket_id, from_generation=from_generation,
                              reason=self.reason or "the host failed", checks=3, waited_s=0.5,
                              release_verified=release_verified)


class SequentialRestart:
    """A `RestartPort` that answers each explicit ticket with the next verified cycle, in order."""

    def __init__(self, cycles: list[tuple[str, Any, Any]]) -> None:
        self._cycles = list(cycles)
        self.calls: list[dict[str, Any]] = []

    async def restart(self, *, ticket_id: str, from_generation: str, previous_endpoint: Any,
                      deadline_s: float, release_verified: bool,
                      previous_session: str | None = None) -> RestartOutcome:
        self.calls.append({"ticket_id": ticket_id, "from_generation": from_generation,
                           "previous_endpoint": previous_endpoint, "deadline_s": deadline_s,
                           "release_verified": release_verified, "previous_session": previous_session})
        to_generation, client, observation = self._cycles.pop(0)
        return RestartOutcome(status=OperationStatus.ready, ticket_id=ticket_id,
                              from_generation=from_generation, to_generation=to_generation, checks=3,
                              waited_s=0.5, release_verified=release_verified, client=client,
                              observation=observation, scene_path="main")


async def new_session() -> tuple[FakeGame, BridgeClient, Any]:
    """A second peer and client: the host's new play session, distinct from the loop's pre-restart one."""
    game = FakeGame(token=TOKEN)
    port = await game.start()
    client = BridgeClient("127.0.0.1", port, TOKEN)
    await client.connect()
    return game, client, await client.observe_once()


async def run_two_turns(restart: FakeRestart, **loop_kwargs: Any) -> tuple[Rig, FakeGame]:
    game, client, observation = await new_session()
    restart.client, restart.observation = client, observation
    r = await rig(RESTART_ID, "finish", choices=[*choices(), restart_choice()],
                  restart=restart, generation=G0, **loop_kwargs)
    await r.loop.run_turn()
    await r.loop.run_turn()
    return r, game


def test_a_restart_turn_sends_no_input_and_traces_one_request_and_one_terminal_entry(tmp_path):
    async def scenario() -> None:
        restart = FakeRestart()
        r, game = await run_two_turns(restart)
        try:
            first, second = r.loop.turns
            assert first.outcome is TurnOutcome.restarted and first.executed_action is None
            assert second.raw_choice == "finish"
            assert first.operation is not None and first.operation.status is OperationStatus.ready
            assert second.operation is None

            entries = [entry for entry in r.sink.entries if entry["turn"] == 0]
            kinds = [entry["kind"] for entry in entries]
            assert kinds.count("restart") == 2 and "execution" not in kinds and "effect" not in kinds
            request, terminal = [entry for entry in entries if entry["kind"] == "restart"]
            assert (request["body"]["event"], terminal["body"]["event"]) == ("request", "completed")
            assert request["refs"] == [next(e["trace_seq"] for e in entries if e["kind"] == "decision")]
            assert terminal["refs"] == [request["trace_seq"]]
            assert request["body"]["from_generation"] == G0
            assert terminal["body"]["to_generation"] == G1 and terminal["body"]["status"] == "ready"
            assert first.raw_choice == RESTART_ID

            # The restarted session is a new client and a new observation, and the loop acts through it.
            assert r.loop.client is restart.client and r.client.closed
            assert second.based_on_seq == r.loop.store.latest().seq > restart.observation.seq
            assert r.loop.generation == G1
            assert "guidance" not in r.s1.contexts[1]
            assert (r.s1.contexts[1]["reset_summary"]["from_generation"],
                    r.s1.contexts[1]["reset_summary"]["to_generation"]) == (G0, G1)

            # The checker and the replay both accept the trace, including the restart pair and its operation.
            record = build_run_record(game_project=tmp_path, status=None, scene=None, manifest_fingerprint=None,
                                      clock_mode=ClockMode.realtime, input_mode=None, models=[],
                                      objective="restart", budget=Budget(), reset_method="restart_play_mode")
            r.sink.entries[0]["body"] = record.model_dump(mode="json")
            for entry in r.sink.entries:            # MemorySink keeps no run_id; the file writer adds it
                entry["run_id"] = record.run_id
            assert trace_problems(r.sink.entries) == []
            parsed = [TraceEntry.model_validate(entry) for entry in r.sink.entries]
            assert replay(parsed) == []
        finally:
            await r.loop.stop("finish")
            await r.close()
            await game.stop()

    asyncio.run(scenario())


def test_no_s1_call_is_made_while_the_host_operation_waits():
    async def scenario() -> None:
        restart = FakeRestart()
        r, game = await run_two_turns(restart)
        try:
            # One S1 call per decision turn, and the restart's wait produced no turn, context or call.
            assert len(r.s1.contexts) == 2
            assert restart.calls[0]["release_verified"] is True
            assert restart.calls[0]["from_generation"] == G0
            # The restart is a primary choice; no synthetic wait choice was added to the set.
            offered = [c["id"] for c in r.s1.contexts[0]["choices"]]
            assert offered == [c.id for c in [*choices(), restart_choice()]]
        finally:
            await r.loop.stop("finish")
            await r.close()
            await game.stop()

    asyncio.run(scenario())


def test_the_restart_does_not_reset_the_runs_wall_deadline():
    async def scenario() -> None:
        fake = FakeTime(start=1000.0)

        def spend_four_seconds(context: dict[str, Any]) -> str:
            fake.now += 4.0
            return RESTART_ID

        restart = FakeRestart(status=OperationStatus.failed)
        r = await rig(spend_four_seconds, choices=[*choices(), restart_choice()], restart=restart,
                      generation=G0)
        built = ManifestChoices(choices=[*choices(), restart_choice()], expected={}, leases={})
        try:
            result = await run_session(
                r.client, built, s1=r.s1, objective="Exercise the controls.", restart=restart,
                generation=G0, wall_limit_s=10.0, clock=fake.clock, sleep=fake.sleep)
            # The operation got the remaining six seconds, not a fresh sixty or a fresh wall limit.
            assert 5.9 <= restart.calls[0]["deadline_s"] <= 6.0
            assert (result.ending, result.status) == ("error", "inconclusive")
        finally:
            await r.close()

    asyncio.run(scenario())


def test_a_failed_restart_ends_the_run_closed_and_never_replays_the_next_answer():
    async def scenario() -> None:
        restart = FakeRestart(status=OperationStatus.failed, reason="the scene did not load")
        r = await rig(RESTART_ID, "c1", choices=[*choices(), restart_choice()], restart=restart,
                      generation=G0)
        built = ManifestChoices(choices=[*choices(), restart_choice()], expected={}, leases={})
        fake = FakeTime()
        budget = TokenBudget(1_000_000, clock=fake.clock, sleep=fake.sleep)
        try:
            result = await run_session(
                r.client, built, s1=r.s1, objective="Exercise the controls.", restart=restart,
                generation=G0, wall_limit_s=60.0, budget=budget, sink=r.sink)
            assert result.ending == "error" and result.status == "inconclusive"
            assert result.turns == 1 and result.outcomes == {"restarted": 1}
            assert "restart_play_mode failed" in result.error and "scene did not load" in result.error
            assert len(r.s1.contexts) == 1                         # the second scripted answer was never asked for
            assert r.sink.of_kind("execution") == []               # no action was replayed or substituted
            assert r.sink.entries and r.sink.of_kind("restart")[-1]["body"]["event"] == "failed"
            assert result.restart_history[0].status is OperationStatus.failed
            assert budget.recorded == 0                            # nothing was re-estimated or reset
        finally:
            await r.close()

    asyncio.run(scenario())


def test_an_unverified_release_never_calls_the_restart_port():
    async def scenario() -> None:
        restart = FakeRestart()
        r = await rig(RESTART_ID, choices=[*choices(), restart_choice()], restart=restart, generation=G0)

        async def unverified(reason: str = "finish") -> StopResult:
            return StopResult(session="runner", seq=1, game_s=None,
                              wall_utc=datetime.now(timezone.utc), released=[], release_verified=False)

        r.client.stop = unverified                          # type: ignore[method-assign]
        try:
            record = await r.loop.run_turn()
            assert record.outcome is TurnOutcome.restarted
            assert record.operation is not None and record.operation.status is OperationStatus.failed
            assert "release was not verified" in (record.operation.reason or "")
            assert restart.calls == []                     # the host never saw a ticket
            assert r.sink.of_kind("restart")[-1]["body"]["release_verified"] is False
        finally:
            await r.close()

    asyncio.run(scenario())


def test_the_restart_adopts_a_new_observation_epoch_and_clears_old_guidance():
    async def scenario() -> None:
        restart = FakeRestart()
        r, game = await run_two_turns(restart)
        try:
            second = r.loop.turns[1]
            assert r.loop.store.latest() is not None
            # The adopted first observation began the new epoch; the next turn observed it again through the new client.
            assert r.loop.store.latest().seq == second.based_on_seq > restart.observation.seq
            assert set(r.loop.observation_entries) == {second.based_on_seq}
            assert r.loop.generation == G1
        finally:
            await r.loop.stop("finish")
            await r.close()
            await game.stop()

    asyncio.run(scenario())


def test_two_s1_selections_are_two_verified_cycles_and_keep_one_budget():
    """Two explicit restarts through the session driver: distinct tickets, a boundary between epochs,
    pending help invalidated, the pre-reset reading dropped, and the run's budget left as it was."""
    async def scenario() -> None:
        first_game, first_client, first_observation = await new_session()
        second_game, second_client, second_observation = await new_session()
        restart = SequentialRestart([(G1, first_client, first_observation),
                                     (G2, second_client, second_observation)])
        help_port = FakeHelp()
        # restart, a delegate whose helper never answers, restart again (it cancels that helper), finish
        r = await rig(RESTART_ID, "d", RESTART_ID, "f",
                      choices=[*choices(), restart_choice()], restart=restart, generation=G0,
                      help=help_port)
        built = ManifestChoices(choices=[*choices(), restart_choice()], expected={}, leases={})
        fake = FakeTime()
        budget = TokenBudget(1_000_000, clock=fake.clock, sleep=fake.sleep)
        budget.record(100)                                  # a call that already spent budget before the restarts
        try:
            result = await run_session(
                r.client, built, s1=r.s1, objective="Exercise the controls.", help=help_port,
                restart=restart, generation=G0, wall_limit_s=60.0, budget=budget, sink=r.sink,
                clock=fake.clock, sleep=fake.sleep)

            # Two distinct tickets, each dispatched from the generation the previous one produced.
            assert [call["from_generation"] for call in restart.calls] == [G0, G1]
            assert len({call["ticket_id"] for call in restart.calls}) == 2
            assert restart.calls[0]["previous_session"] == r.client.session
            assert restart.calls[1]["previous_session"] == first_client.session
            assert [op.status for op in result.restart_history] == [OperationStatus.ready, OperationStatus.ready]
            assert result.outcomes == {"restarted": 2, "delegated": 1, "finished": 1}

            # Each cycle adopted its own client: the first adopted one was closed when the second replaced it.
            assert result.loop.generation == G2 and first_client.closed

            # The helper opened before the second restart was invalidated at the boundary, never adopted.
            assert help_port.cancelled is True
            help_events = [entry["body"]["event"] for entry in r.sink.of_kind("help")]
            assert "cancelled" in help_events and "adopted" not in help_events

            # No budget refill: the call counted before the restarts is still the only one counted, and the
            # second epoch's context names the boundary it crossed.
            assert budget.recorded == 1 and budget.in_window == 100
            assert r.s1.contexts[1]["reset_summary"]["from_generation"] == G0
            assert r.s1.contexts[1]["reset_summary"]["to_generation"] == G1
            assert r.s1.contexts[3]["reset_summary"]["from_generation"] == G1
            assert r.s1.contexts[3]["reset_summary"]["to_generation"] == G2

            # Only the current epoch's reading is citable; the pre-reset sequences are not current frame.
            assert len(result.loop.observation_entries) == 1
            assert set(result.loop.observation_entries) == {result.loop.store.latest().seq}
            assert first_observation.seq not in result.loop.observation_entries
        finally:
            for game in (r.game, first_game, second_game):
                await game.stop()

    asyncio.run(scenario())
