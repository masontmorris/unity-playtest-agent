"""The rise measured in the state it creates: pressed again at its measured apex, with fakes only."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from playtest.runner.options.air_rise import GAIN_SHARE, measure_air_rise, with_air_rise
from playtest.runner.options.primitives import Primitive, Primitives, option_specs
from playtest.runner.options.primitives import Direction

GRAVITY = 20.0
RISE_SPEED = 8.0     # peak 1.6 from rest
DT = 0.02


class Body:
    """A body at rest that rises when the rise is newly pressed, and once more in the air when `air_presses`."""

    def __init__(self, air_presses: int) -> None:
        self.t = 0.0
        self.y = 0.0
        self.vy = 0.0
        self.air_left = air_presses
        self.air_presses = air_presses
        self.held = False
        self.seq = 0
        self.stops: list[str] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self._advance(seconds)

    def _advance(self, seconds: float) -> None:
        steps = max(1, int(round(seconds / DT)))
        for _ in range(steps):
            self.t += DT
            if self.y > 0 or self.vy > 0:
                self.vy -= GRAVITY * DT
                self.y = max(0.0, self.y + self.vy * DT)
                if self.y == 0.0:
                    self.vy = 0.0
                    self.air_left = self.air_presses

    async def observe_once(self):
        self._advance(DT)
        self.seq += 1
        return SimpleNamespace(seq=self.seq, public={"controlled": {"position": [0.0, self.y, 0.0]}})

    async def start_act(self, **request):
        if self.held:
            return []
        self.held = True
        if self.y == 0.0:
            self.vy = RISE_SPEED
        elif self.air_left > 0:
            self.air_left -= 1
            self.vy = RISE_SPEED
        return []

    async def stop(self, reason: str):
        self.held = False
        self.stops.append(reason)
        return SimpleNamespace(release_verified=True)

    async def collect_act(self, action_id: str):
        return []


def primitives() -> Primitives:
    rise = Primitive(action="rise_up", control="space", kind="rise", vector=(0.0, 1.0, 0.0), spread=None, trials=2)
    right = Direction(label="+x", action="move_right", control="d", unit=(1.0, 0.0, 0.0), speed=9.0,
                      spread=None, step=0.47, trials=3)
    return Primitives(directions=(right,), rise=rise, rise_duration_s=0.8, rise_peak=1.6,
                      chords=(("move_right", "rise_up", "chord_right"),))


def measured(air_presses: int):
    body = Body(air_presses)
    return asyncio.run(measure_air_rise(body, primitives(), clock=body.clock, sleep=body.sleep)), body


def test_a_second_press_that_rises_higher_is_measured_as_gained():
    air, body = measured(air_presses=1)
    assert air is not None and air.gained
    assert air.pressed_peak - air.rest_peak > GAIN_SHARE * air.rest_peak
    assert 0.3 < air.press_s < 0.5                        # pressed at the apex the rise from rest measured
    assert all(f.second_press_s is not None for f in air.pressed)
    assert air.pressed_airtime_s > max(f.airtime_s for f in air.rest)
    assert body.stops and set(body.stops) == {"release"}  # every press is released


def test_a_second_press_that_does_nothing_is_not_gained_and_adds_no_option():
    air, _ = measured(air_presses=0)
    assert air is not None and not air.gained
    derived = with_air_rise(primitives(), air)
    assert derived.air_rise_peak is None
    assert not any(spec.option_id.startswith("hop2") for spec in option_specs(derived))


def test_a_gained_rise_offers_a_hop_that_presses_again_with_its_own_measured_numbers():
    air, _ = measured(air_presses=1)
    derived = with_air_rise(primitives(), air)
    specs = {spec.option_id: spec for spec in option_specs(derived)}
    assert "hop+x" in specs and "hop2+x" in specs
    double = specs["hop2+x"].params
    assert double["air_press_s"] == air.press_s and double["rise_peak"] == air.pressed_peak
    assert double["flight_s"] == air.pressed_airtime_s
    assert "air_press_s" not in specs["hop+x"].params


def test_without_a_measured_rise_nothing_is_measured():
    assert asyncio.run(measure_air_rise(Body(1), replace(primitives(), rise=None))) is None


def test_both_hops_are_labelled_on_the_same_measured_flight_basis():
    from playtest.contracts.loop import Choice
    from playtest.runner import menu
    from playtest.runner.options.memory import OutcomeMemory

    air, _ = measured(air_presses=1)
    specs = {spec.option_id: spec for spec in option_specs(with_air_rise(primitives(), air))}
    labels = {}
    for key in ("hop+x", "hop2+x"):
        spec = specs[key]
        choice = Choice(id=key, kind="option", option_id=key, template="hop", params=spec.params,
                        label=spec.intent, source="manifest")
        labels[key] = menu.option_label(choice, OutcomeMemory(cell=0.47), "s", (0.0, 0.0, 0.0))
    assert f"air {air.rest_airtime_s:.2f} s, peak {air.rest_peak:.2f}" in labels["hop+x"]
    assert f"air {air.pressed_airtime_s:.2f} s, peak {air.pressed_peak:.2f}" in labels["hop2+x"]


def test_a_rise_that_gains_nothing_still_labels_the_plain_hop_with_its_measured_flight():
    air, _ = measured(air_presses=0)
    derived = with_air_rise(primitives(), air)
    assert derived.rest_flight_s == air.rest_airtime_s and derived.air_rise_peak is None


def test_a_measured_flight_supplies_the_rise_airtime_calibration_left_unknown_and_enables_a_body_hop():
    """No airtime from calibration: the probe's flights from rest supply it, and a body-relative chord hops."""
    from playtest.runner.options.primitives import Direction
    forward = Direction(label="forward", action="move_forward", control="w", unit=(0.0, 0.0, 1.0), speed=5.0,
                        spread=None, step=0.4, trials=2, basis="body")
    back = Direction(label="back", action="move_back", control="s", unit=(0.0, 0.0, -1.0), speed=5.0,
                     spread=None, step=0.4, trials=2, basis="body")
    rise = Primitive(action="jump", control="space", kind="rise", vector=(0.0, 1.0, 0.0), spread=None, trials=2)
    base = Primitives(body_directions=(forward, back), rise=rise, rise_duration_s=None, rise_peak=1.9,
                      chords=(("move_forward", "jump", "running_jump"),))
    assert not base.can_hop
    body = Body(air_presses=0)
    air = asyncio.run(measure_air_rise(body, base, clock=body.clock, sleep=body.sleep))
    derived = with_air_rise(base, air)
    assert derived.rise_duration_s == air.rest_airtime_s and derived.can_hop
    specs = {spec.option_id: spec for spec in option_specs(derived)}
    hop = specs["hop:forward"].params
    assert hop["basis"] == "body" and hop["chord"] == "running_jump" and hop["run_up_action"] == "move_back"
