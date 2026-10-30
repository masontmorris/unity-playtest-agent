"""Day 2 loop invariants: the production client against the fake game.

Every scenario asserts on the fake game's input log, not only on the
loop's own records. Each test covers one row of the issue's scenario
table. The decision source is scripted; no model provider is involved.
"""

from __future__ import annotations

import asyncio
from collections import deque

from playtest.contracts.loop import Choice, TurnOutcome
from playtest.core.adherence import check_adherence
from playtest.core.bridge_client import BridgeClient
from playtest.core.turn_loop import TurnLoop

from fake_game import FakeGame

TOKEN = "loop-test-token"


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


class Script:
    """Scripted decision source: pops raw answers, filling the live set version."""

    def __init__(self, answers: list) -> None:
        self._queue = deque(answers)

    def __call__(self, choice_set, observation):
        raw = self._queue.popleft()
        if isinstance(raw, dict):
            raw = dict(raw)
            raw.setdefault("set_version", choice_set.set_version)
        return raw


async def _started(**kwargs):
    game = FakeGame(token=TOKEN, **kwargs)
    port = await game.start()
    client = BridgeClient("127.0.0.1", port, TOKEN)
    await client.connect()
    return game, client


async def _closed(game, client) -> None:
    try:
        await client.aclose()
    finally:
        await game.stop()


def test_invalid_decision_rejected_no_input():
    async def main():
        game, client = await _started()
        try:
            loop = TurnLoop(client, choices=choices())
            script = Script([
                {"choice_id": "zz", "set_version": 0},       # unknown id
                {"choice_id": "c1", "set_version": 999},     # wrong set_version
                "not-an-object",                             # unparseable
            ])
            records = [await loop.run_turn(script) for _ in range(3)]
            assert [r.outcome for r in records] == [TurnOutcome.rejected_invalid] * 3
            assert [r.raw_choice for r in records] == ["zz", "c1", None]
            assert all(r.effective_choice is None and r.executed_action is None for r in records)
            assert game.applied() == []
            assert check_adherence(loop.turns) == []
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_wait_sends_no_input():
    async def main():
        game, client = await _started()
        try:
            loop = TurnLoop(client, choices=choices())
            record = await loop.run_turn(Script([{"choice_id": "w"}]))
            assert record.outcome is TurnOutcome.finished
            assert record.effective_choice == "w" and record.executed_action is None
            assert game.applied() == []
            assert check_adherence(loop.turns) == []
            latest = loop.store.latest()
            assert latest is not None and loop.store.age_s is not None and loop.store.age_s >= 0
            assert loop.store.game_s == latest.game_s and loop.store.wall_utc == latest.wall_utc
            assert loop.store.reader("entity")["pos"] == [0.0, 0.0, 0.0]
            assert loop.store.reader("missing") is None
            assert loop.store.field("entity.pos") == [0.0, 0.0, 0.0]
            assert loop.store.field("entity.absent") is None
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_help_continue_runs_only_pending_choice():
    async def main():
        game, client = await _started()
        try:
            loop = TurnLoop(client, choices=choices())
            first = await loop.run_turn(Script([
                {"choice_id": "h", "while_pending": {"mode": "continue", "choice_id": "c1"}},
            ]))
            assert first.outcome is TurnOutcome.escalated
            assert first.effective_choice == "h" and first.executed_action == "c1"
            assert not first.policy_default
            assert game.applied() == ["shift"]
            assert loop.helper_count == 1
            assert loop.resolve_helper("helped") is True
            assert loop.helper_count == 1  # externally resolved, awaiting a fresh observation's epoch check
            second = await loop.run_turn(Script([
                {"choice_id": "d", "while_pending": {"mode": "continue", "choice_id": "c2"}},
            ]))
            assert second.outcome is TurnOutcome.delegated
            assert second.effective_choice == "d" and second.executed_action == "c2"
            assert game.applied() == ["shift", "increment"]
            assert loop.helper_count == 1
            assert check_adherence(loop.turns) == []
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_help_missing_while_pending_defaults_to_wait():
    async def main():
        game, client = await _started()
        try:
            loop = TurnLoop(client, choices=choices())
            script = Script([
                {"choice_id": "h"},  # missing secondary
                {"choice_id": "d", "while_pending": {"mode": "continue", "choice_id": "zz"}},  # invalid target
            ])
            first = await loop.run_turn(script)
            assert first.outcome is TurnOutcome.escalated
            assert first.effective_choice == "h" and first.executed_action is None
            assert first.policy_default is True
            assert first.while_pending is not None and first.while_pending.mode == "wait"
            second = await loop.run_turn(script)
            assert second.outcome is TurnOutcome.delegated
            assert second.effective_choice == "d" and second.executed_action is None
            assert second.policy_default is True
            assert game.applied() == []
            assert check_adherence(loop.turns) == []
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_second_help_while_helper_pending():
    async def main():
        game, client = await _started()
        try:
            loop = TurnLoop(client, choices=choices())
            first = await loop.run_turn(Script([
                {"choice_id": "h", "while_pending": {"mode": "continue", "choice_id": "c1"}},
            ]))
            assert loop.helper_count == 1
            second = await loop.run_turn(Script([
                {"choice_id": "d", "while_pending": {"mode": "continue", "choice_id": "c1"}},
            ]))
            assert second.outcome is TurnOutcome.delegated
            assert second.executed_action == "c1"
            assert loop.helper_count == 1, "no second helper while one is pending"
            third = await loop.run_turn(Script([{"choice_id": "h2"}]))
            assert third.outcome is TurnOutcome.rejected_invalid
            assert third.executed_action is None
            assert game.applied() == ["shift", "shift"]
            assert check_adherence(loop.turns) == []
            assert first.outcome is TurnOutcome.escalated
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_helper_resolves_exactly_once():
    async def main():
        game, client = await _started()
        try:
            loop = TurnLoop(client, choices=choices())
            await loop.run_turn(Script([
                {"choice_id": "h", "while_pending": {"mode": "continue", "choice_id": "c1"}},
            ]))
            assert loop.helper_count == 1
            assert loop.resolve_helper("resolution") is True
            assert loop.adoptions == []
            assert loop.helper_count == 1
            assert loop.resolve_helper("resolution-again") is False
            await loop.run_turn(Script([{"choice_id": "w"}]))
            assert loop.adoptions == ["resolution"]
            assert loop.helper_count == 0
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_stale_act_rejected_no_substitute():
    from playtest.contracts.bridge import RejectReason

    async def main():
        game, client = await _started(reject_acts=RejectReason.stale_age)
        try:
            loop = TurnLoop(client, choices=choices())
            record = await loop.run_turn(Script([{"choice_id": "c1"}]))
            assert record.outcome is TurnOutcome.rejected_stale
            assert record.effective_choice == "c1" and record.executed_action is None
            assert game.applied() == []
            assert check_adherence(loop.turns) == []
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_stop_with_held_lease_releases():
    async def main():
        game, client = await _started()
        try:
            loop = TurnLoop(client, choices=choices())
            loop.lease_s = 30.0
            held = await loop.run_turn(Script([{"choice_id": "c3"}]))
            assert held.outcome is TurnOutcome.executed
            assert game.applied() == ["hold"]
            assert game.releases() == []
            await loop.run_turn(Script([
                {"choice_id": "h", "while_pending": {"mode": "continue", "choice_id": "c1"}},
            ]))
            assert loop.helper_count == 1
            result = await loop.stop(reason="user")
            assert result is not None and result.release_verified is True
            assert ("primary", "stop") in game.releases()
            assert loop.helper_count == 0
            assert loop.cancelled_helpers == 1
            assert loop.resolve_helper("late") is False
        finally:
            await _closed(game, client)

        game2, client2 = await _started()
        try:
            loop2 = TurnLoop(client2, choices=choices())
            loop2.lease_s = 30.0
            await loop2.run_turn(Script([{"choice_id": "c3"}]))
            writer = client2._writer
            assert writer is not None
            writer.close()  # abrupt disconnect: no stop is sent
            for _ in range(200):
                if game2.releases():
                    break
                await asyncio.sleep(0.01)
            assert ("primary", "disconnect") in game2.releases()
        finally:
            await _closed(game2, client2)

    asyncio.run(main())


def test_full_scripted_run():
    async def main():
        game, client = await _started()
        try:
            loop = TurnLoop(client, choices=choices())
            script = Script([
                {"choice_id": "c1"},
                {"choice_id": "w"},
                {"choice_id": "h", "while_pending": {"mode": "continue", "choice_id": "c2"}},
                {"choice_id": "d", "while_pending": {"mode": "wait"}},
                {"choice_id": "c2"},
                {"choice_id": "f"},
            ])
            records = await loop.run(script)
            assert [r.outcome for r in records] == [
                TurnOutcome.executed,
                TurnOutcome.finished,
                TurnOutcome.escalated,
                TurnOutcome.delegated,
                TurnOutcome.executed,
                TurnOutcome.finished,
            ]
            assert loop.resolve_helper("done") is True
            assert game.applied() == ["shift", "increment", "increment"]
            assert loop.store.reader("entity")["pos"] == [1.0, 0.0, 0.0]
            assert loop.store.reader("counter") == 2
            assert check_adherence(loop.turns) == []
            result = await loop.stop(reason="finish")
            assert result is not None and result.release_verified is True
        finally:
            await _closed(game, client)

    asyncio.run(main())
