import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from playtest.runner import review


def item(**updates):
    base = dict(title='test', expected='expected', expected_source='contrast', actual='actual', evidence_turns=[1], contrast_turns=[2], uncertainty='', repro_check={'reader': 'x', 'op': '>', 'value': 1})
    return base | updates


def rows():
    return [{'kind':'observation','turn':1,'trace_seq':10,'body':{'public':{'x':0}}}, {'kind':'observation','turn':2,'trace_seq':20,'body':{'public':{'x':2}}}, {'kind':'turn','body':{'turn':1,'outcome':'executed','executed_action':'go','effective_choice':None}}]


def test_conversion():
    manifest = {'readers':[{'alias':'x','kind':'number'}]}
    findings, rejected = review.findings_from_answer({'findings':[item(),item(evidence_turns=[8]),item(repro_check={'reader':'missing','op':'>','value':1})]}, rows(), manifest)
    assert findings[0].evidence_seq == [10,20]
    assert findings[0].repro_check.field == 'public.x'
    assert findings[1].repro_check is None and 'rejected' in findings[1].uncertainty
    assert len(rejected) == 1


def test_packet_surroundings(monkeypatch):
    monkeypatch.setattr(review, 'declared_configuration', lambda *args: [])
    packet = review.build_review_packet(rows(), {'actions':[], 'skills':[], 'entity_id':'e'}, {})
    assert packet['actions'][0]['surroundings_before'] == review.describe_surroundings(None)
    assert review.request_body(packet)['response_format']['json_schema']['strict'] is True


def test_cli_refuses_existing_and_missing_approval(tmp_path, monkeypatch):
    def run(output, approval):
        monkeypatch.setattr(sys, 'argv', ['playtest-review','--session',str(tmp_path),'--manifest',str(tmp_path/'m'),'--discovery',str(tmp_path/'d'),'--approval',str(approval),'--output',str(output)])
        review.main()
    with pytest.raises(SystemExit): run(tmp_path, tmp_path/'missing')
    with pytest.raises(FileNotFoundError): run(tmp_path/'new', tmp_path/'missing')


def test_cli_one_reservation(tmp_path, monkeypatch):
    (tmp_path/'trace.jsonl').write_text('\n'.join(json.dumps(r) for r in rows()))
    (tmp_path/'m').write_text(json.dumps({'actions':[], 'skills':[], 'entity_id':'e', 'readers':[{'alias':'x','kind':'number'}]}))
    (tmp_path/'d').write_text('{}')
    (tmp_path/'a').write_text('{}')
    monkeypatch.setattr(review.replay, 'validate_approval', lambda *a: None)
    monkeypatch.setattr(review, 'declared_configuration', lambda *a: [])
    monkeypatch.setenv('META_API_KEY','fake')
    calls=[]
    class Reservation:
        def finish(self, **kw): calls.append('finish')
    class Ledger:
        def __init__(self, *a): pass
        def reserve(self, **kw): calls.append('reserve'); return Reservation()
    monkeypatch.setattr(review.call_budget, 'Ledger', Ledger)
    monkeypatch.setattr(review.provider, 'send_meta', lambda body: (200, {'choices':[{'message':{'content':json.dumps({'findings':[item()]})}}]}, 1, None))
    monkeypatch.setattr(sys, 'argv', ['playtest-review','--session',str(tmp_path),'--manifest',str(tmp_path/'m'),'--discovery',str(tmp_path/'d'),'--approval',str(tmp_path/'a'),'--output',str(tmp_path/'out')])
    review.main()
    assert calls == ['reserve','finish']
    assert (tmp_path/'out/finding-r-1.json').exists()


def test_packet_carries_an_options_outcome_on_the_turn_that_started_it_and_the_start_height(monkeypatch):
    monkeypatch.setattr(review, 'declared_configuration', lambda *args: [])
    anchor = lambda y: {'anchor': {'position': [0.0, y, 0.0]}}
    trace = [
        {'kind': 'observation', 'turn': 1, 'trace_seq': 10, 'body': {'public': {'x': 0, 'probe': anchor(-6.0)}}},
        {'kind': 'decision', 'turn': 1, 'trace_seq': 12, 'body': {}},
        {'kind': 'turn', 'turn': 1, 'trace_seq': 13, 'body': {'turn': 1, 'outcome': 'executed',
                                                              'executed_action': 'hop+x', 'effective_choice': 'hop+x'}},
        {'kind': 'observation', 'turn': 2, 'trace_seq': 20, 'body': {'public': {'x': 1, 'probe': anchor(-6.0)}}},
        {'kind': 'option', 'turn': 2, 'trace_seq': 22, 'refs': [12],
         'body': {'option_id': 'hop+x', 'outcome': {'terminated_by': 'fell', 'displacement': [3.0, -2.0, 0.0],
                                                    'duration_s': 1.25}}},
        {'kind': 'turn', 'turn': 2, 'trace_seq': 23, 'body': {'turn': 2, 'outcome': 'executed',
                                                              'executed_action': 'go', 'effective_choice': 'go'}},
        {'kind': 'observation', 'turn': 3, 'trace_seq': 30, 'body': {'public': {'x': 2}}},
    ]
    packet = review.build_review_packet(trace, {'actions': [], 'skills': [], 'entity_id': 'e'}, {})
    rows_by_turn = {row['turn']: row for row in packet['actions']}
    assert packet['start_height'] == -6.0
    assert rows_by_turn[1]['option_outcome'] == {'terminated_by': 'fell', 'displacement': [3.0, -2.0, 0.0],
                                                 'duration_s': 1.25}
    assert 'option_outcome' not in rows_by_turn[2]
    assert '0.08' not in review.SYSTEM
