import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'probes'))
import probe_s1_pointing as probe


def frame(yaw, x=3.58, z=-16.25, fov=65.0):
    return {'yaw': yaw, 'camera': {'position': [x, 1.78, z], 'field_of_view': fov}}


def test_the_foot_is_centred_when_the_view_faces_it_and_moves_right_as_the_view_turns_left():
    straight = probe.truth(frame(41.05))
    assert straight['strip'] == 5 and abs(straight['rel_deg']) < .1
    assert probe.truth(frame(20.0))['strip'] == 7       # about 21 degrees right of centre
    assert probe.truth(frame(57.0))['strip'] == 3       # about 16 degrees left of centre


def test_edge_frames_are_left_out_and_out_of_view_frames_have_no_strip():
    assert probe.truth(frame(0.0)) is None              # the foot sits within 3 degrees of the right edge
    assert probe.truth(frame(120.0))['strip'] is None   # behind the left edge
    assert probe.truth({'yaw': 0.0}) is None            # no camera reading


def test_strip_answers_parse():
    assert probe.strip_of('strip_7') == 7 and probe.strip_of('not_visible') is None
