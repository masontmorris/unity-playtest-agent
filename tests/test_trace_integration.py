"""The turn loop's own traces, written by the file writer, pass the replay (#29 with #30).

The loop and the replay were built in parallel against `core/trace_sink.py`. These tests run the
production loop on the fake game, write through `TraceWriter`, read the file back and replay it, so
any drift between what the loop writes and what the replay checks fails here.
"""

from __future__ import annotations

import asyncio

from fake_game import FakeGame
from loop_fakes import TOKEN, FakeHelp, FakeProbe, FakeS1, Raw, choices, pick
from playtest.contracts.bridge import RejectReason
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, RunRecord
from playtest.contracts.loop import TurnOutcome
from playtest.core.bridge_client import BridgeClient
from playtest.core.ports import S1Answer, S1Call
from playtest.core.trace import TraceWriter, read_trace
from playtest.core.trace_replay import replay
from playtest.core.turn_loop import TurnLoop


def _run_record() -> RunRecord:
    return RunRecord(run_id="integration", game_project="fake", clock_mode=ClockMode.realtime, models=[],
                     objective="Exercise the controls.", budget=Budget(), core_version="0.0.1")


async def _loop(path, *steps, game=None, help=None, probe=None):
    game = FakeGame(token=TOKEN) if game is None else game
    port = await game.start()
    client = BridgeClient("127.0.0.1", port, TOKEN)
    await client.connect()
    writer = TraceWriter(path, _run_record())
    loop = TurnLoop(client, choices=choices(), objective="Exercise the controls.", s1=FakeS1(*steps),
                    help=help, effects=probe, sink=writer)
    return game, client, writer, loop


async def _close(game, client) -> None:
    try:
        await client.aclose()
    finally:
        await game.stop()


def test_a_mixed_run_replays_clean(tmp_path):
    path = tmp_path / "trace.jsonl"

    async def main():
        game = FakeGame(token=TOKEN)
        helper = FakeHelp(result={"advice": "carry on"})

        def stale_once(_context):
            game.reject_acts = RejectReason.stale_age
            return pick("c1")

        def fresh_again(_context):
            game.reject_acts = None
            return "c2"

        no_answer = S1Answer(answer=None, failure="HTTP error (HTTP 429)", calls=(
            S1Call(question="next_action", provider="fake", model="fake-1", http_status=429, latency_ms=3.0,
                   problem="HTTP error"),))
        game, client, writer, loop = await _loop(
            path, "c1", "w", pick("h", while_pending={"mode": "continue", "choice_id": "c2"}), pick("d"),
            pick("zz"), Raw("not-an-object"), no_answer, stale_once, fresh_again, "w", pick("h"), pick("f"),
            game=game, help=helper, probe=FakeProbe(None))
        try:
            for _ in range(4):
                await loop.run_turn()
            helper.release.set()
            await asyncio.sleep(0.02)
            while len(loop.turns) < 12:
                await loop.run_turn()
            result = await loop.stop("finish")
            assert result is not None and result.release_verified is True
            writer.close("complete")
        finally:
            await _close(game, client)
        assert [t.outcome for t in loop.turns] == [
            TurnOutcome.executed, TurnOutcome.finished, TurnOutcome.escalated, TurnOutcome.delegated,
            TurnOutcome.rejected_invalid, TurnOutcome.rejected_invalid, TurnOutcome.rejected_invalid,
            TurnOutcome.rejected_stale, TurnOutcome.executed, TurnOutcome.finished, TurnOutcome.escalated,
            TurnOutcome.finished]

    asyncio.run(main())
    entries = read_trace(path)
    events = [e.body["event"] for e in entries if e.kind.value == "help"]
    assert events == ["opened", "repeat", "adopted", "opened", "cancelled"]
    assert replay(entries) == []


def test_a_port_that_raises_is_written_and_replays_clean(tmp_path):
    path = tmp_path / "trace.jsonl"

    def broken(_context):
        raise RuntimeError("provider down")

    async def main():
        game, client, writer, loop = await _loop(path, broken, pick("f"))
        try:
            await loop.run_turn()
            await loop.run_turn()
            await loop.stop("finish")
            writer.close("complete")
        finally:
            await _close(game, client)
        assert [t.outcome for t in loop.turns] == [TurnOutcome.rejected_invalid, TurnOutcome.finished]

    asyncio.run(main())
    entries = read_trace(path)
    calls = [e.body for e in entries if e.kind.value == "s1_call"]
    assert calls[0]["question"] is None and calls[0]["problem"] == "port error: RuntimeError"
    assert replay(entries) == []


def test_an_act_that_raises_leaves_its_request_and_a_reported_truncated_turn(tmp_path):
    path = tmp_path / "trace.jsonl"

    async def main():
        game, client, writer, loop = await _loop(path, "c1")

        async def act(**_kwargs):
            raise ConnectionError("bridge closed")

        client.act = act
        try:
            try:
                await loop.run_turn()
            except ConnectionError:
                pass
            else:
                raise AssertionError("the act's failure must reach the caller")
            writer.close("inconclusive", "bridge closed")
        finally:
            await _close(game, client)

    asyncio.run(main())
    entries = read_trace(path)
    executions = [e.body for e in entries if e.kind.value == "execution"]
    assert len(executions) == 1 and executions[0]["records"] == []
    messages = [p.message for p in replay(entries)]
    assert "turn entries stop before a turn record" in messages
