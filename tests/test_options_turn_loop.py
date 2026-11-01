from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from loop_fakes import rig, run
from turn_trace_check import problems
from playtest.contracts.loop import Choice
from playtest.core.adherence import check_adherence
from playtest.contracts.trace import TraceEntry
from playtest.core.trace_replay import replay
from playtest.runner.choices import options_choices


def option():
    return Choice(id='traverse+x', kind='option', label='Traverse', option_id='traverse+x',
                  template='traverse', params={'direction': [1, 0, 0]}, source='manifest')


class Port:
    async def run(self, choice):
        return {'option_id': choice.option_id, 'template': choice.template, 'params': choice.params,
                'start_position': None, 'end_position': None, 'displacement': None,
                'terminated_by': 'unmeasured', 'duration_s': None, 'scene': None, 'receipts': []}


def test_option_selected_and_traced():
    async def main():
        r = await rig('traverse+x', choices=[option()], option_port=Port())
        try:
            await r.turns(1)
            assert r.loop.turns[0].executed_action == 'traverse+x'
            assert not check_adherence(r.loop.turns)
            row = r.sink.of_kind('option')[0]
            assert row['refs'] == [r.sink.of_kind('decision')[0]['trace_seq']]
            await r.loop.stop('finish')
            assert not problems(r.sink.entries)
            run_body = json.loads(Path('fixtures/contracts/trace_entry/valid-run.json').read_text())['body']
            run_body['run_id'] = 'test'
            rows = [{**row, 'run_id': 'test'} for row in r.sink.entries]
            rows[0]['body'] = run_body
            assert not replay([TraceEntry.model_validate(row) for row in rows])
        finally:
            await r.close()
    run(main)


def test_option_labels_and_placement(monkeypatch):
    from playtest.runner.options import primitives
    from playtest.runner.options.model import OptionSpec
    monkeypatch.setattr(primitives, 'option_specs', lambda _: [OptionSpec('traverse+x', 'traverse', {}, 'move')])
    class Memory:
        def observe(self, observation): pass
        def suffix(self, scene, position, option_id): return str(position)
        def settled_points(self, scene): return [[1, 2, 3]]
    p = SimpleNamespace(classified=[SimpleNamespace(action='walk', kind='move')])
    choices = [Choice(id='walk', kind='action', action='walk', label='Walk', source='manifest'),
               Choice(id='use', kind='action', action='use', label='Use', source='manifest')]
    a = SimpleNamespace(scene='scene', public={'controlled': {'position': [0, 0, 0]}})
    b = SimpleNamespace(scene='scene', public={'controlled': {'position': [1, 0, 0]}})
    first = options_choices(choices, p, Memory(), a, placement=True)
    second = options_choices(choices, p, Memory(), b, placement=True)
    assert [c.id for c in first] == ['use', 'traverse+x', 'place:0']
    assert first[1].label != second[1].label


def test_stop_cancels_running_option():
    async def main():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        class BlockingPort:
            async def run(self, choice):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
        r = await rig('traverse+x', choices=[option()], option_port=BlockingPort())
        try:
            task = asyncio.create_task(r.loop.run_turn())
            await started.wait()
            result = await r.loop.stop('finish')
            assert cancelled.is_set() and result.release_verified
            await asyncio.gather(task, return_exceptions=True)
        finally:
            await r.close()
    run(main)
