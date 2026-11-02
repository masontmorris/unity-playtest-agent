from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from loop_fakes import choices as base_choices
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


class InterruptiblePort:
    """An option that runs until released or interrupted, and reports which, as a measured outcome would."""

    def __init__(self):
        self.release = asyncio.Event()
        self.interrupted: list[str] = []
        self.runs = 0

    async def run(self, choice):
        self.runs += 1
        await self.release.wait()
        ended = 'interrupted' if self.interrupted else 'succeeded'
        return {'option_id': choice.option_id, 'template': choice.template, 'params': choice.params,
                'start_position': None, 'end_position': None, 'displacement': None,
                'terminated_by': ended, 'duration_s': 1.5, 'scene': None, 'receipts': []}

    def interrupt(self, reason):
        self.interrupted.append(reason)
        self.release.set()


def test_decide_during_options_keeps_going_on_a_repeat_and_interrupts_on_another_choice():
    """The option runs while S1 decides again: the same id keeps it going, another choice interrupts it."""
    async def main():
        port = InterruptiblePort()
        r = await rig('traverse+x', 'traverse+x', 'c1', 'f', choices=[*base_choices(), option()],
                      option_port=port, decide_during_options=True)
        try:
            first = await r.loop.run_turn()           # starts it and ends the turn at once
            await asyncio.sleep(0)                    # the background task takes its first step
            assert first.executed_action == 'traverse+x' and port.runs == 1
            assert r.loop.reports[-1].option_running and r.loop.reports[-1].finished_option is None
            second = await r.loop.run_turn()          # S1 chose it again: kept going, not restarted
            assert port.runs == 1 and port.interrupted == [] and second.executed_action == 'traverse+x'
            assert 'running_option' in r.sink.of_kind('context')[-1]['body']['payload']
            await r.loop.run_turn()                   # another choice: interrupted first, then executed
            assert port.interrupted == ['S1 chose c1']
            report = r.loop.reports[-1]
            assert report.finished_option[1]['terminated_by'] == 'interrupted'
            assert report.record.executed_action == 'c1' and report.option_running  # c1 runs in the background too
            option_rows = r.sink.of_kind('option')
            assert len(option_rows) == 1 and option_rows[0]['body']['outcome']['terminated_by'] == 'interrupted'
            assert option_rows[0]['refs'] == [r.sink.of_kind('decision')[0]['trace_seq']]
        finally:
            await r.loop.stop('finish')
            await r.close()
    run(main)


def test_a_background_option_that_ends_by_itself_is_collected_without_an_interrupt():
    async def main():
        port = InterruptiblePort()
        r = await rig('traverse+x', 'f', choices=[*base_choices(), option()],
                      option_port=port, decide_during_options=True)
        try:
            await r.loop.run_turn()
            port.release.set()
            for _ in range(5):
                await asyncio.sleep(0)
            await r.loop.run_turn()                   # finish: nothing is running, so nothing is interrupted
            assert port.interrupted == []
            assert r.loop.reports[-1].finished_option[1]['terminated_by'] == 'succeeded'
        finally:
            await r.loop.stop('finish')
            await r.close()
    run(main)


def test_decide_during_options_runs_a_plain_move_in_the_background_and_files_it_when_collected():
    """A plain action starts and the turn ends; the next turn collects it, so its execution is still filed."""
    async def main():
        r = await rig('c1', 'c2', 'f', choices=base_choices(), decide_during_options=True)
        try:
            first = await r.loop.run_turn()
            assert first.executed_action == 'c1' and r.loop.reports[-1].option_running
            assert r.loop.reports[-1].executed == ()            # not yet collected
            await r.loop.run_turn()                             # c2: c1 is collected (stopped if still running)
            moved = r.loop.reports[-1].finished_move
            assert moved is not None and moved[0].id == 'c1' and not moved[1].execution.rejected
            await r.loop.run_turn()                             # finish collects c2
            assert r.loop.reports[-1].finished_move[0].id == 'c2'
            executions = [e['body']['request']['choice_id'] for e in r.sink.of_kind('execution')]
            assert executions[:2] == ['c1', 'c2']
        finally:
            await r.loop.stop('finish')
            await r.close()
    run(main)
