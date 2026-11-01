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


@pytest.mark.parametrize("pending", [False, True])
def test_step_after_held_releases_rebaselines_and_measures_only_step(pending):
    async def run():
        options = choices()
        step = next(c for c in options if c.id == 'c3')
        options.append(step.model_copy(update={'id': 'c3.held'}))
        answer = ({'choice_id': 'h', 'while_pending': {'mode': 'continue', 'choice_id': 'c3'}}
                  if pending else 'c3')
        r = await rig('c3.held', answer, choices=options, continuous_input=True,
                      continuous_choices=['c3.held'], leases={'c3': 0.08, 'c3.held': 0.5},
                      probe=True, game={'action_run_s': 0.08, 'lease_terminals': True})
        try:
            first = await r.loop.run_turn()  # held selection works even without an interval
            assert first.executed_action == 'c3.held'
            assert r.game._held
            assert not r.loop._effects.measured
            second = await r.loop.run_turn()
            assert second.executed_action == 'c3'
            assert not r.game._held
            assert r.loop._effects.measured == ['c3']
            assert r.loop._effects.turns == [0, 1, 1]  # fresh baseline after release
            requests = [row['body']['request'] for row in r.sink.of_kind('execution')]
            assert [(q['choice_id'], q['action'], q['lease_s']) for q in requests] == [
                ('c3.held', 'hold', 0.5), ('c3', 'hold', 0.08)]
            assert not check_adherence(r.loop.turns)
        finally:
            await r.close()
    asyncio.run(run())


def test_runner_held_variants_preserve_steps_and_report_calibrated_product():
    from runner_manifest import action, hold, manifest
    from playtest.runner.choices import build_choices, with_held_choices, held_choice_labels
    from playtest.runner.session import continuous_choice_ids
    built = build_choices(manifest(action('move', 'w', hold_s=0.4, holds=[hold(0.4, mean=2.03)])))
    original = [c.model_dump_json() for c in built.choices]
    configured = with_held_choices(built, continuous_choice_ids(built))
    assert [c.model_dump_json() for c in configured.choices[:-1]] == original
    assert [c.model_dump_json() for c in built.choices] == original
    assert configured.choices[-1].id == 'move.held'
    assert configured.choices[-1].action == 'move'
    assert configured.choices[-1].params == {}
    assert configured.leases == {'move': 0.4, 'move.held': 0.4}
    initial = held_choice_labels(configured, ['move.held'], None, None)[-1]
    assert 'distance/decision unknown' in initial.label
    assert '5.07 world units/s' in initial.label
    measured = held_choice_labels(configured, ['move.held'], 1.7, 3.4)[-1]
    assert '5.07 world units/s * 1.70 s = 8.63 world units' in measured.label
    assert 'lease 3.40 s' in measured.label
    with pytest.raises(ValueError, match='collision'):
        with_held_choices(configured, ['move'])


@pytest.mark.parametrize('enabled', [False, True])
def test_session_offers_both_forms_only_when_enabled(enabled):
    from runner_manifest import action, hold, manifest
    from playtest.runner.choices import build_choices
    from playtest.runner.session import run_session
    async def run():
        built = build_choices(manifest(action('hold', 'w', hold_s=0.4, holds=[hold(0.4)])))
        r = await rig(game={'action_run_s': 0.08, 'lease_terminals': True})
        s1 = FakeS1(*(['hold', 'hold.held', 'finish'] if enabled else ['hold', 'finish']))
        try:
            result = await run_session(r.client, built, s1=s1, objective='Exercise controls.',
                                       sink=r.sink, continuous_input=enabled)
            assert result.status == 'complete', result.error
            sets = [row['body']['choices'] for row in r.sink.of_kind('choice_set')]
            for offered in sets:
                assert offered[:len(built.choices)] == [c.model_dump(mode='json') for c in built.choices]
                assert len(offered) == len(built.choices) + int(enabled)
            if enabled:
                assert 'distance/decision unknown' in sets[0][-1]['label']
                assert 'distance/decision unknown' not in sets[1][-1]['label']
            executions = [row['body']['request'] for row in r.sink.of_kind('execution')]
            assert executions[0]['choice_id'] == 'hold'
            assert executions[0]['lease_s'] == 0.4
            if enabled:
                assert executions[1]['choice_id'] == 'hold.held'
                assert executions[1]['action'] == 'hold'
                assert executions[1]['lease_s'] != 0.4
            assert not result.adherence
        finally:
            await r.close()
    asyncio.run(run())


def _replay_problems(rows):
    from playtest.contracts.common import ClockMode
    from playtest.contracts.investigation import Budget, RunRecord
    from playtest.core.trace_replay import replay
    run = RunRecord(run_id='test', game_project='Synthetic', clock_mode=ClockMode.realtime, models=[],
                    objective='Exercise controls.', budget=Budget(), core_version='0.0.1')
    rows = [dict(row) for row in rows]
    rows[0] = {**rows[0], 'body': run.model_dump(mode='json')}
    return [p.message for p in replay([TraceEntry.model_validate({'run_id': 'test', **row}) for row in rows])]


async def _held_then_superseded_rows():
    r = await active_rig('c3', 'w')
    try:
        await r.loop.run_turn()
        await r.loop.run_turn()
        await r.loop.stop('finish')
        return [dict(row) for row in r.sink.entries]
    finally:
        await r.close()


def test_held_lease_receipts_replay_clean():
    """A held lease's receipt is written when it closes, inside the next turn's span; replay accepts that."""
    rows = asyncio.run(_held_then_superseded_rows())
    held = [row for row in rows if row['kind'] == 'execution' and row['body']['records']
            and row['body']['records'][0]['status'] == 'running']
    turn_seq = {row['turn']: row['trace_seq'] for row in rows if row['kind'] == 'turn'}
    assert any(row['trace_seq'] > turn_seq[row['turn']] for row in held)  # the overlap really occurs
    assert _replay_problems(rows) == []


def test_held_receipt_closing_after_its_superseding_turn_is_flagged():
    rows = asyncio.run(_held_then_superseded_rows())
    turn_seq = {row['turn']: row['trace_seq'] for row in rows if row['kind'] == 'turn'}
    late = next(row for row in rows if row['kind'] == 'execution' and row['turn'] is not None
                and row['trace_seq'] > turn_seq[row['turn']])
    late['turn'] -= 1  # now it claims a turn two turns before the span it sits in
    assert 'a held execution closed after the turn that superseded it' in _replay_problems(rows)


def test_a_receipt_that_was_never_a_running_lease_gets_no_exemption():
    rows = asyncio.run(_held_then_superseded_rows())
    turn_seq = {row['turn']: row['trace_seq'] for row in rows if row['kind'] == 'turn'}
    late = next(row for row in rows if row['kind'] == 'execution' and row['turn'] is not None
                and row['trace_seq'] > turn_seq[row['turn']])
    late['body'] = {**late['body'], 'records': late['body']['records'][1:]}  # drop the running receipt
    problems = _replay_problems(rows)
    assert 'turn entries are interleaved' in problems
    assert 'turn entries are out of documented order' in problems
