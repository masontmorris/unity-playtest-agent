import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'probes'))
import probe_s1_tolerance_wording as probe

TEXT = 'is in the centre of the view; rise straight ahead of you'
HALF = 'is ahead of you, anywhere in the middle half of the view; rise ahead of you, anywhere in the middle half of the view'


def test_substitution():
    assert probe.objective_for(TEXT, 'centre') == TEXT
    for arm, band in probe.BANDS.items():
        assert probe.objective_for(TEXT, arm) == f'is ahead of you, anywhere in {band}; rise ahead of you, anywhere in {band}'
    assert probe.objective_for(TEXT, 'half_approach') == 'is ahead of you, anywhere in the middle half of the view; rise straight ahead of you'
    for bad in ('missing', TEXT + '; rise straight ahead of you', TEXT + '; is in the centre of the view'):
        with pytest.raises(ValueError): probe.objective_for(bad, 'half')
    with pytest.raises(ValueError): probe.objective_for(TEXT, 'quarter')


def test_a_recorded_half_band_objective_maps_back_to_every_arm():
    assert probe.objective_for(TEXT, 'half') == HALF
    assert probe.recorded_arm(HALF) == 'half' and probe.recorded_arm(TEXT) == 'centre'
    for arm in probe.ARMS:
        assert probe.objective_for(HALF, arm) == probe.objective_for(TEXT, arm)


def test_classifier():
    def public(yaw, x=3.58, z=-16.25, y=0.08): return {'yaw': yaw, 'probe': {'anchor': {'position': [x, y, z]}}}
    assert probe.classify(public(38)) == 'far'
    assert probe.classify(public(359)) == 'near'
    assert probe.classify(public(0)) == 'near'
    assert probe.classify(public(20)) is None
    assert probe.classify(public(38, 4.1)) is None
    assert probe.classify(public(38, 7.85, -10.87)) == 'foot'
    assert probe.classify(public(38, 7.85, -10.87, y=1.0)) is None   # already up the steps
    assert probe.classify(public(0, 7.85, -10.87)) is None           # already on the flight's axis
    assert probe.classify({}) is None


def _rows(units, foot_left):
    rs = []
    for u in units:
        for arm in probe.arms_for(u):
            for repeat in range(probe.REPEATS):
                changed = arm != 'centre'
                if u['class'] == 'far':
                    d = {'mouse_turn_left': .2 if changed else .8, 'mouse_turn_right': .1, 'move_forward': .4 if changed else .1}
                elif u['class'] == 'near':
                    d = {'mouse_turn_left': .1, 'mouse_turn_right': .8, 'move_forward': .1}
                else:
                    d = {'mouse_turn_left': foot_left[arm], 'mouse_turn_right': .1, 'move_forward': .3, 'running_jump': .1}
                choice = max(d, key=d.get)
                rs.append({'key': u['key'], 'arm': arm, 'repeat': repeat, 'status': 200, 'choice': choice, 'distribution': d})
    return rs


def _write(path, rs):
    (path / 'results.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rs))


def test_readout(tmp_path):
    units = [{'key': str(i), 'class': 'far' if i < 8 else 'near' if i < 16 else 'foot'} for i in range(20)]
    _write(tmp_path, _rows(units, {'centre': .5, 'half': .3, 'half_approach': .5}))
    result = probe.summarize(tmp_path, units)
    assert result['band_reading'] == 'third'
    assert result['arms']['third']['settles'] and not result['arms']['third']['over_widens']
    assert result['line_up_gain'] == pytest.approx(.2)
    assert result['reading'] == 'half_approach'
    assert result['arms']['half']['foot']['p_align'] == pytest.approx(.3)
    assert 'foot' not in result['arms']['third']                     # foot units are not asked under band arms

    _write(tmp_path, _rows(units, {'centre': .5, 'half': .3, 'half_approach': .35}))
    result = probe.summarize(tmp_path, units)
    assert result['line_up_gain'] == pytest.approx(.05) and result['reading'] == 'third'

    rs = _rows(units, {'centre': .5, 'half': .3, 'half_approach': .5}); rs.pop(); _write(tmp_path, rs)
    assert probe.summarize(tmp_path, units)['reading'] == 'inconclusive: incomplete data'
