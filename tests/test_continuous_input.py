import asyncio
import json
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loop_fakes import FakeS1, choices, rig
from playtest.contracts.bridge import ExecutionRecord, ExecStatus, Observation, RejectReason
from playtest.contracts.trace import TraceEntry
from playtest.core.adherence import check_adherence
from playtest.core.trace_sink import MemorySink
from playtest.core.turn_loop import TurnLoop


async def active_rig(*answers, probe=False):
    r = await rig('w', 'c3', *answers, continuous_input=True, continuous_choices=['c3'],
                  probe=probe, game={'action_run_s': 0.08, 'lease_terminals': True}, leases={'c3': 0.08})
    await r.loop.run_turn()
    await asyncio.sleep(0.08)  # measured first turn interval establishes the next lease
    await r.loop.run_turn()
    assert r.game._held
    return r


@pytest.mark.parametrize('answer', ['w', 'f', {'choice_id': 'missing'},
    {'choice_id': 'h', 'while_pending': {'mode': 'wait'}},
    {'choice_id': 'd', 'while_pending': {'mode': 'wait'}},
    {'choice_id': 'h', 'while_pending': {'mode': 'continue', 'choice_id': 'w'}},
    {'choice_id': 'h', 'while_pending': {'mode': 'continue', 'choice_id': 'missing'}}])
def test_no_unselected_continuation(answer):
    async def run():
        r = await active_rig(answer)
        try:
            before = len(r.game.input_log)
            await r.loop.run_turn()
            assert not r.game._held
            assert not any(row['event'] == 'applied' for row in r.game.input_log[before:])
            assert not check_adherence(r.loop.turns)
            for row in r.sink.entries:
                if row["kind"] not in ("execution", "stop"):
                    continue
                TraceEntry.model_validate({"run_id": "test", **row})
        finally:
            await r.close()
    asyncio.run(run())


def test_selected_continuation_expiry_and_unknown_effect():
    async def run():
        r = await active_rig('c3', 'w', probe=True)
        try:
            first_id = r.game._held.copy()
            await r.loop.run_turn()
            assert r.game._held != first_id
            assert ('primary', 'superseded') in r.game.releases()
            assert 'action=hold' in str(r.s1.contexts[-1])
            assert 'unknown' in str(r.s1.contexts[-1])
            assert not r.loop._effects.measured
            lease = r.loop._active[0]['lease_s']
            await asyncio.sleep(lease + 0.03)
            assert not r.game._held
            assert ('primary', 'expired') in r.game.releases()
            await r.loop.run_turn()
            assert len([e for e in r.game.input_log if e['event'] == 'applied']) == 2
            assert not check_adherence(r.loop.turns)
        finally:
            await r.close()
    asyncio.run(run())


def test_stale_selection_releases_without_repair():
    async def run():
        r = await active_rig('c3')
        try:
            r.game._reject_acts = RejectReason.stale_age
            record = await r.loop.run_turn()
            assert record.outcome.value == 'rejected_stale'
            assert not r.game._held
            assert len([e for e in r.game.input_log if e['event'] == 'applied']) == 1
        finally:
            await r.close()
    asyncio.run(run())


def test_pending_continue_executes_only_named_selection():
    async def run():
        r = await active_rig({'choice_id': 'h', 'while_pending': {'mode': 'continue', 'choice_id': 'c3'}}, 'w')
        try:
            record = await r.loop.run_turn()
            assert record.executed_action == 'c3'
            assert r.game._held
            await r.loop.run_turn()
            assert not r.game._held
            assert not check_adherence(r.loop.turns)
        finally:
            await r.close()
    asyncio.run(run())


def test_default_off_payload_and_trace_bytes_identical(monkeypatch):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    obs = Observation(session='s', seq=1, wall_utc=stamp, clock_mode='realtime',
                      scene='scene', playing=True, paused=False, time_scale=1, frame=1)
    class FixedDateTime:
        @staticmethod
        def now(tz):
            return stamp
    monkeypatch.setattr('playtest.core.trace_sink.datetime', FixedDateTime)
    monkeypatch.setattr('playtest.core.turn_loop.time.perf_counter', lambda: 1.0)
    monkeypatch.setattr('playtest.core.execution.uuid.uuid4', lambda: SimpleNamespace(hex='fixed'))

    async def capture(options):
        async def act(**request):
            return [ExecutionRecord(session='s', seq=2, wall_utc=stamp,
                action_id=request['action_id'], status=ExecStatus.completed)]
        client = SimpleNamespace(observe_once=AsyncMock(return_value=obs), act=act)
        sink, s1 = MemorySink(), FakeS1('c3', 'w')
        loop = TurnLoop(client, choices=choices(), objective='Exercise controls.', s1=s1,
                        sink=sink, clock=lambda: stamp, leases={'c3': 0.4}, **options)
        await loop.run_turn()
        await loop.run_turn()
        return json.dumps({'contexts': s1.contexts, 'trace': sink.entries}, sort_keys=True)
    async def run():
        default = await capture({})
        assert default == await capture({'continuous_input': False, 'continuous_choices': ['c3']})
        # Captured independently using HEAD's original TurnLoop with the same fixed inputs/clocks.
        assert hashlib.sha256(default.encode()).hexdigest() == (
            'f009778a6b4373af9a4f404cc7c0c15f7f792e816e073ece40f882a5382ff1c6')
    asyncio.run(run())


def test_different_selection_releases_previous_and_executes_exactly_once():
    async def run():
        r = await active_rig('c2')
        try:
            await r.loop.run_turn()
            assert not r.game._held
            assert r.game.applied() == ['hold', 'increment']
            assert not check_adherence(r.loop.turns)
        finally:
            await r.close()
    asyncio.run(run())


def test_stop_releases_and_collects_terminal_receipt():
    async def run():
        r = await active_rig()
        try:
            result = await r.loop.stop('finish')
            assert result.release_verified
            assert not r.game._held
            assert r.client.closed
            rows = r.sink.of_kind('execution')
            assert len(rows) == 1
            assert rows[0]['body']['records'][-1]['status'] == 'cancelled'
        finally:
            await r.close()
    asyncio.run(run())


def test_disconnect_releases_and_never_replays():
    async def run():
        r = await active_rig('c3')
        try:
            await r.client.aclose()
            assert not r.game._held
            with pytest.raises(Exception):
                await r.loop.run_turn()
            assert r.game.applied() == ['hold']
        finally:
            await r.close()
    asyncio.run(run())


def test_only_calibrated_sustained_movement_holds_are_continuous():
    """A tap, a toggle or an input that moved nothing keeps its short hold; only measured movement continues."""
    from types import SimpleNamespace
    from playtest.runner.session import continuous_choice_ids

    key = SimpleNamespace(device="Keyboard")
    mouse = SimpleNamespace(device="Mouse")
    actions = {
        "walk": SimpleNamespace(is_skill=False, inputs=[key]),
        "hop": SimpleNamespace(is_skill=False, inputs=[key]),        # a 0.1 s tap
        "toggle": SimpleNamespace(is_skill=False, inputs=[key]),     # moved nothing
        "uncalibrated": SimpleNamespace(is_skill=False, inputs=[key]),
        "look": SimpleNamespace(is_skill=False, inputs=[mouse]),
        "routine": SimpleNamespace(is_skill=True, inputs=[key]),
    }
    expected = {"walk": {"net_displacement": 2.0, "requested_hold_s": 0.4},
                "hop": {"net_displacement": 1.4, "requested_hold_s": 0.1},
                "toggle": {"net_displacement": 0.0, "requested_hold_s": 0.4},
                "look": {"net_displacement": 0.5, "requested_hold_s": 0.4},
                "routine": {"net_displacement": 2.0, "requested_hold_s": 0.4}}
    built = SimpleNamespace(catalog=SimpleNamespace(actions=actions), expected=expected)
    assert continuous_choice_ids(built) == ["walk"]
