import asyncio
import json

import pytest

from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.core.context import canonical
from playtest.runner.option_order import OptionOrder, ordered_ids, serialize
from playtest.runner.s1 import SystemOneS1
from s1_fakes import approval, reply
from test_runner_s1 import context


def test_serialization_and_shuffle():
    criteria = {f'c{i}': str(i) for i in range(20, 0, -1)}
    body = {'state': {'z': 1, 'a': [1, 2]}, 'questions': {'q': {'criteria': criteria}}, 'model': 'x'}
    sorted_ids = ordered_ids(criteria, OptionOrder.sorted, None, 1, 0, 'q')
    assert serialize(body, {'q': sorted_ids}) == canonical(body)
    shuffled = ordered_ids(criteria, OptionOrder.shuffled, 42, 1, 0, 'q')
    assert shuffled == ordered_ids(criteria, OptionOrder.shuffled, 42, 1, 0, 'q')
    assert shuffled != ordered_ids(criteria, OptionOrder.shuffled, 42, 1, 1, 'q')
    assert json.loads(serialize(body, {'q': shuffled})) == json.loads(canonical(body))


@pytest.mark.parametrize('mode', ['sorted', 'manifest', 'shuffled'])
def test_adapter_order_and_selection(tmp_path, mode):
    ledger = call_budget.Ledger(tmp_path, approval(), 'mammoth-1')
    seen = []
    def send(arm, body, **kwargs):
        seen.append((body, kwargs.get('wire_text', canonical(body))))
        return reply('next_action', 'c1', favoured='c2')(body)
    port = SystemOneS1(ledger, max_calls=3, arm='mammoth-1', send=send, option_order=mode, order_seed=7)
    ctx = context()
    ctx['choices'] = list(reversed(ctx['choices']))
    result = asyncio.run(port.decide(ctx))
    body, wire = seen[0]
    row = ledger.arm_rows()[0]
    assert result.answer['choice_id'] == 'c1'
    assert result.answer['distribution']['c2'] == 0.9
    assert digest(wire) == result.calls[0].request_sha256 == row['request_sha256']
    if mode == 'sorted':
        assert wire == canonical(body) and 'option_order' not in row
    else:
        assert wire == serialize(body, {'next_action': row['presented_ids']})
        if mode == 'manifest':
            assert row['presented_ids'] == [c['id'] for c in ctx['choices']]


def test_invalid_modes(tmp_path):
    ledger = call_budget.Ledger(tmp_path, approval(), 'mammoth-1')
    with pytest.raises(ValueError, match='integer seed'):
        SystemOneS1(ledger, max_calls=1, option_order='shuffled')
    with pytest.raises(ValueError, match='Meta'):
        SystemOneS1(ledger, max_calls=1, arm='meta-muse-spark-s1', option_order='manifest')
    with pytest.raises(ValueError, match='full request layout'):
        SystemOneS1(ledger, max_calls=1, option_order='manifest', layout='dup-state-ref/1')

