import json
import sys
from datetime import datetime, timezone

import pytest

from playtest.runner import review

T0 = 1_000_000.0  # bridge wall clock, epoch seconds


def item(**updates):
    base = dict(title='test', expected='expected', expected_source='contrast', actual='actual', evidence_rows=[0],
                contrast_rows=[1], uncertainty='', repro_check={'reader': 'x', 'op': '>', 'value': 1, 'after_action': None})
    return base | updates


def _iso(offset_s):
    return datetime.fromtimestamp(T0 + offset_s, timezone.utc).isoformat()


def obs(seq, turn, at, **public):
    return {'kind': 'observation', 'turn': turn, 'trace_seq': seq, 'body': {'wall_utc': _iso(at), 'public': public}}


def execution(seq, turn, action, at, held, status='completed', reason=None):
    record = {'status': status, 'input_start_wall_s': T0 + at if status != 'rejected' else None,
              'applied_duration_s': held if status != 'rejected' else None, 'reject_reason': reason,
              'release_verified': True if status != 'rejected' else None}
    return {'kind': 'execution', 'turn': turn, 'trace_seq': seq,
            'body': {'purpose': 'choice', 'request': {'action': action, 'choice_id': action}, 'records': [record]}}


def rows():
    """`go` raises x from 0 to 2; `aim` holds a zoom that is gone after release; `look` was rejected stale."""
    return [obs(10, 0, 0.0, x=0, zoom=0), execution(11, 0, 'go', 0.1, 0.4),
            obs(20, 1, 1.0, x=2, zoom=0), execution(21, 1, 'aim', 1.1, 0.4),
            obs(25, 1, 1.3, x=2, zoom=1),                                  # taken while aim is still held
            obs(30, 2, 2.0, x=2, zoom=0), execution(31, 2, 'look', None, None, status='rejected', reason='stale_age'),
            obs(40, 3, 3.0, x=2, zoom=0)]


def test_each_row_is_one_input_measured_over_its_own_window_and_unrun_inputs_are_only_counted(monkeypatch):
    monkeypatch.setattr(review, 'declared_configuration', lambda *args: [])
    packet, windows = review.build_review_packet(rows(), {'actions': [], 'skills': [], 'entity_id': 'e'}, {})
    assert [(r['row'], r['choice'], r['action']) for r in packet['actions']] == [(0, 'go', 'go'), (1, 'aim', 'aim')]
    assert packet['inputs_not_run'] == {'rejected stale_age': 1}
    assert packet['actions'][0]['changed'] == {'x': [0, 2]}
    aim = packet['actions'][1]
    assert aim['changed'] == {} and aim['changed_while_held'] == {'zoom': [0, 1]}
    assert windows[0][:2] == (10, 20) and windows[1][:2] == (20, 30)
    assert packet['actions'][0]['surroundings_before'] == review.describe_surroundings(None)
    assert review.request_body(packet)['response_format']['json_schema']['strict'] is True
    assert '0.08' not in review.SYSTEM


def test_an_option_is_one_row_on_the_turn_that_started_it_with_how_it_ended(monkeypatch):
    monkeypatch.setattr(review, 'declared_configuration', lambda *args: [])
    anchor = {'anchor': {'position': [0.0, -6.0, 0.0]}}
    receipts = [{'type': 'request', 'action_id': 'hop+x.1', 'action': 'right', 'lease_s': 3.0},
                {'type': 'execution', 'action_id': 'hop+x.1', 'status': 'running', 'input_start_wall_s': T0 + 0.1},
                {'type': 'execution', 'action_id': 'hop+x.2', 'status': 'cancelled', 'input_start_wall_s': T0 + 0.5,
                 'applied_duration_s': 0.7, 'release_verified': True}]
    trace = [obs(10, 1, 0.0, x=0, probe=anchor), {'kind': 'decision', 'turn': 1, 'trace_seq': 12, 'body': {}},
             obs(20, 2, 1.5, x=3, probe=anchor),
             {'kind': 'option', 'turn': 2, 'trace_seq': 22, 'refs': [12], 'wall_utc': _iso(1.3),
              'body': {'option_id': 'hop+x', 'receipts': receipts,
                       'outcome': {'terminated_by': 'fell', 'displacement': [3.0, -2.0, 0.0], 'duration_s': 1.25}}}]
    packet, windows = review.build_review_packet(trace, {'actions': [], 'skills': [], 'entity_id': 'e'}, {})
    (row,) = packet['actions']
    assert (row['turn'], row['choice'], row['kind'], row['option_ended']) == (1, 'hop+x', 'option', 'fell')
    assert row['displacement'] == [3.0, -2.0, 0.0] and 'action' not in row
    assert packet['start_height'] == -6.0 and windows[0][:2] == (10, 20)


def test_conversion_cites_each_rows_two_observations_and_checks_the_input_condition(monkeypatch):
    monkeypatch.setattr(review, 'declared_configuration', lambda *args: [])
    manifest = {'actions': [], 'skills': [], 'entity_id': 'e',
                'readers': [{'alias': 'x', 'kind': 'number'}, {'alias': 'zoom', 'kind': 'number'}]}
    _, windows = review.build_review_packet(rows(), manifest, {})
    unchanged = {'reader': 'zoom', 'op': 'unchanged', 'value': None, 'after_action': 'aim'}
    findings, rejected = review.findings_from_answer({'findings': [
        item(),
        item(evidence_rows=[1], repro_check=unchanged),
        item(evidence_rows=[1], repro_check=unchanged | {'after_action': 'look'}),   # never ran: no check
        item(repro_check={'reader': 'zoom', 'op': 'unchanged', 'value': None, 'after_action': None}),
        item(repro_check={'reader': 'missing', 'op': '>', 'value': 1, 'after_action': None}),
        item(evidence_rows=[8]),
    ]}, windows, manifest)
    assert findings[0].evidence_seq == [10, 20] and findings[0].repro_check.field == 'public.x'
    assert findings[0].steps == ['"go"'] and findings[0].conditions == 'Contrast rows: 1'
    assert (findings[1].repro_check.op.value, findings[1].repro_check.after_action) == ('unchanged', 'aim')
    for finding in findings[2:]:
        assert finding.repro_check is None and 'rejected' in finding.uncertainty
    assert len(findings) == 5 and len(rejected) == 1


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


def test_an_input_conditioned_check_needs_a_contrast_row_where_its_reader_changed(monkeypatch):
    monkeypatch.setattr(review, 'declared_configuration', lambda *args: [])
    manifest = {'actions': [], 'skills': [], 'entity_id': 'e',
                'readers': [{'alias': 'x', 'kind': 'number'}, {'alias': 'zoom', 'kind': 'number'}]}
    _, windows = review.build_review_packet(rows(), manifest, {})
    unchanged = {'reader': 'zoom', 'op': 'unchanged', 'value': None, 'after_action': 'go'}
    findings, _ = review.findings_from_answer({'findings': [
        item(evidence_rows=[0], contrast_rows=[1], repro_check=unchanged),   # aim held: zoom changed there
        item(evidence_rows=[0], contrast_rows=[], repro_check=unchanged),    # no contrast at all
        item(evidence_rows=[1], contrast_rows=[0], repro_check=unchanged),   # go never changed zoom
    ]}, windows, manifest)
    assert findings[0].repro_check is not None and findings[0].repro_check.after_action == 'go'
    for finding in findings[1:]:
        assert finding.repro_check is None and 'no contrast row shows that reader changing' in finding.uncertainty
    assert 'contrast row' in review.SYSTEM
