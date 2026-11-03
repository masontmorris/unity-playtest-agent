"""A rise's net displacement is whatever it was read at: it is never compared with its calibrated value.

On clean DragonBlaze (2026-11-03) six in-play jumps measured 2.26 to 2.52 against a calibrated 2.47 with an
allowance of 0.05, falling as the reading after each landed later in the arc, and the frontier filed that as a
finding. Calibration's own holds show a rise falling as the hold lengthens (2.47 at 0.2 s, 1.22 at 0.4 s).
"""

from __future__ import annotations

from types import SimpleNamespace

from playtest.runner import choices
from playtest.runner.frontier_help import action_effects
from playtest.runner.probe import _comparison


def action(*holds):
    return SimpleNamespace(calibrations=[SimpleNamespace(holds=[
        SimpleNamespace(requested_hold_s=h, mean_magnitude=m) for h, m in holds])])


def test_a_displacement_that_falls_at_a_longer_hold_is_timing_sensitive_and_noise_is_not():
    assert choices._timing_sensitive(action((0.2, 2.47), (0.4, 1.22), (1.2, 0.1)))      # a rise
    assert not choices._timing_sensitive(action((0.2, 1.89), (0.4, 3.78), (1.2, 9.32)))  # a walk
    assert not choices._timing_sensitive(action((0.2, 0.004), (0.4, 0.0), (1.2, 0.001)))  # a body that hardly moved
    assert not choices._timing_sensitive(SimpleNamespace(calibrations=[]))


def test_a_timing_sensitive_effect_is_compared_with_nothing_and_offered_to_no_replay():
    rise = {"net_displacement": 2.47, "sample_variance": 6e-6, "unit": "world units", "timing_sensitive": True}
    walk = {"net_displacement": 3.78, "sample_variance": 1e-6, "unit": "world units"}
    observed = {"net_displacement": 2.26}
    assert _comparison(rise, observed) is None                     # unknown, never a mismatch
    assert _comparison(walk, {"net_displacement": 3.0}) is not None
    assert set(action_effects({"jump": rise, "move": walk})) == {"move"}
