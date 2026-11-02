"""What the rise does while the body is airborne: a second press at the measured apex of the first.

Calibration measures every action from rest. An action's effect can depend on the body's state, and the one
state the rise itself creates is airborne, so this measures the rise once more in that state, with the same bridge
inputs an option uses: a rise from rest, then a rise pressed again at the apex the first one measured. It reports
heights and airtimes only. Nothing here reads a game, object, control or level name, and it chooses nothing: the
options layer offers a hop that presses again in the air only when this measured the second press rising higher.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

UP = 1
TRIALS = 2                # rises of each kind; the smaller measured gain is the one reported
GAIN_SHARE = 0.25         # a second press counts when it adds over a quarter of a rise from rest
MAX_FLIGHT_S = 4.0        # a flight that has not come down by then is reported unmeasured
MIN_AIRTIME_S = 0.15      # nothing before this counts as a landing
SETTLE_S = 2.0            # longest wait for the body to come to rest between rises
SETTLED_READINGS = 3      # consecutive still readings that count as at rest
MAX_AGE_S = 1.0
PROBE_ID = "air-rise"
UNMEASURED_RISE_LEASE_S = 0.5  # the press held when calibration measured no airtime for the rise


@dataclass(frozen=True)
class Flight:
    """One measured rise: the highest point above its start, when it was reached, and when the body came down."""

    peak: float
    apex_s: float
    airtime_s: float | None   # None: it had not come back down within the bound
    second_press_s: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"peak": self.peak, "apex_s": self.apex_s, "airtime_s": self.airtime_s,
                "second_press_s": self.second_press_s}


@dataclass(frozen=True)
class AirRise:
    """Rises from rest and rises pressed again at their apex, and what the second press measurably added."""

    rest: tuple[Flight, ...]
    pressed: tuple[Flight, ...]
    press_s: float | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rest_peak(self) -> float | None:
        return min((f.peak for f in self.rest), default=None)

    @property
    def pressed_peak(self) -> float | None:
        return min((f.peak for f in self.pressed), default=None)

    @property
    def rest_airtime_s(self) -> float | None:
        times = [f.airtime_s for f in self.rest]
        return None if not times or any(t is None for t in times) else min(times)

    @property
    def pressed_airtime_s(self) -> float | None:
        times = [f.airtime_s for f in self.pressed]
        return None if not times or any(t is None for t in times) else min(times)

    @property
    def gained(self) -> bool:
        """Whether every second press rose higher than every rise from rest, by more than `GAIN_SHARE`."""
        rest, pressed = self.rest_peak, self.pressed_peak
        if rest is None or pressed is None or rest <= 0 or self.pressed_airtime_s is None:
            return False
        return pressed - max(f.peak for f in self.rest) > GAIN_SHARE * rest

    def to_json(self) -> dict[str, Any]:
        return {"rest": [f.to_json() for f in self.rest], "pressed": [f.to_json() for f in self.pressed],
                "press_s": self.press_s, "rest_peak": self.rest_peak, "pressed_peak": self.pressed_peak,
                "pressed_airtime_s": self.pressed_airtime_s, "gained": self.gained, "notes": list(self.notes),
                "gain_share": GAIN_SHARE}


def _position(observation: Any) -> tuple[float, ...] | None:
    public = getattr(observation, "public", None) or {}
    controlled = public.get("controlled") if isinstance(public, dict) else None
    value = controlled.get("position") if isinstance(controlled, dict) else None
    if not isinstance(value, (list, tuple)) or len(value) <= UP:
        return None
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None


class _Probe:
    def __init__(self, client: Any, action: str, lease_s: float, eps: float,
                 clock: Callable[[], float], sleep: Callable[[float], Awaitable[Any]]) -> None:
        self.client, self.action, self.lease_s, self.eps = client, action, lease_s, eps
        self.clock, self.sleep = clock, sleep
        self.acts = 0
        self.started: list[str] = []

    async def settle(self) -> Any:
        """Observe until the body reads still `SETTLED_READINGS` times running, or `SETTLE_S` passes."""
        started = self.clock()
        last, still = None, 0
        while True:
            observation = await self.client.observe_once()
            position = _position(observation)
            if position is not None and last is not None and abs(position[UP] - last[UP]) <= self.eps:
                still += 1
            else:
                still = 0
            last = position
            if still >= SETTLED_READINGS or self.clock() - started >= SETTLE_S:
                return observation
            await self.sleep(0.05)

    async def press(self, observation: Any) -> None:
        self.acts += 1
        action_id = f"{PROBE_ID}.{self.acts}"
        self.started.append(action_id)
        await self.client.start_act(action_id=action_id, choice_id=PROBE_ID, action=self.action,
                                    params={}, based_on_seq=observation.seq, max_age_s=MAX_AGE_S,
                                    lease_s=self.lease_s)

    async def flight(self, second_press_s: float | None) -> Flight | None:
        observation = await self.settle()
        start = _position(observation)
        if start is None:
            return None
        await self.press(observation)
        began = self.clock()
        peak, apex, pressed_at, airtime = 0.0, 0.0, None, None
        try:
            while self.clock() - began < MAX_FLIGHT_S:
                observation = await self.client.observe_once()
                elapsed = self.clock() - began
                position = _position(observation)
                if position is None:
                    continue
                height = position[UP] - start[UP]
                if height > peak:
                    peak, apex = height, elapsed
                if second_press_s is not None and pressed_at is None and elapsed >= second_press_s:
                    # Release first, so the next press is a new one, then press again with the body in the air.
                    await self.client.stop("release")
                    await self.press(observation)
                    pressed_at = elapsed
                if elapsed >= MIN_AIRTIME_S and peak > self.eps and height <= self.eps:
                    airtime = elapsed
                    break
        finally:
            await self.client.stop("release")
            while self.started:  # every started input is collected after its release, as the client requires
                try:
                    await self.client.collect_act(self.started.pop())
                except Exception:  # noqa: BLE001 - a receipt that never came is not a measurement
                    pass
        return Flight(peak=peak, apex_s=apex, airtime_s=airtime, second_press_s=pressed_at)


async def measure_air_rise(client: Any, primitives: Any, *, clock: Callable[[], float] = time.monotonic,
                           sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> AirRise | None:
    """Measure rises from rest, then rises pressed again at their measured apex. `None` without a measured rise."""
    rise = getattr(primitives, "rise", None)
    # The rise is held through its measured airtime; with none measured, through a bounded press, and this
    # probe then measures the airtime itself.
    lease = getattr(primitives, "rise_duration_s", None) or UNMEASURED_RISE_LEASE_S
    peak = getattr(primitives, "rise_peak", None)
    if rise is None or not peak:
        return None
    eps = max(1e-3, 0.02 * float(peak))
    probe = _Probe(client, rise.action, float(lease), eps, clock, sleep)
    notes: list[str] = []
    rest = [f for f in [await probe.flight(None) for _ in range(TRIALS)] if f is not None]
    if len(rest) < TRIALS:
        notes.append("a rise from rest had no measured position")
    apexes = [f.apex_s for f in rest if f.peak > eps]
    if not apexes:
        return AirRise(rest=tuple(rest), pressed=(), press_s=None, notes=tuple(notes + ["no rise was measured"]))
    press_s = statistics.median(apexes)
    pressed = [f for f in [await probe.flight(press_s) for _ in range(TRIALS)] if f is not None]
    if len(pressed) < TRIALS:
        notes.append("a pressed rise had no measured position")
    return AirRise(rest=tuple(rest), pressed=tuple(pressed), press_s=press_s, notes=tuple(notes))


def with_air_rise(primitives: Any, air: AirRise | None) -> Any:
    """These primitives, plus the measured flight from rest, and the airborne rise when it measurably gained.

    The flight from rest is kept whatever the second press did: it puts every hop's label on the same measured
    basis (airtime and peak of an actual flight) as a hop that presses again.
    """
    import dataclasses

    if air is None:
        return primitives
    rest = dataclasses.replace(primitives, rest_rise_peak=air.rest_peak, rest_flight_s=air.rest_airtime_s)
    if rest.rise_duration_s is None and air.rest_airtime_s is not None:
        # Calibration measured no airtime for the rise; this probe's flights from rest did.
        rest = dataclasses.replace(rest, rise_duration_s=air.rest_airtime_s)
    if not air.gained:
        return rest
    return dataclasses.replace(rest, air_rise_peak=air.pressed_peak, air_press_s=air.press_s,
                               air_flight_s=air.pressed_airtime_s)

