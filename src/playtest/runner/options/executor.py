"""The option executor: code runs the motor loop S1 chose (docs/design/options-layer.md, section 4).

S1 picks an intent (`traverse`, `retreat`, `hop`, `wait`, `place`). This class runs it as a closed loop at
observation rate over measured primitives, ends on a measured condition, and returns a measured outcome.
It never decides which option to run, and it never raises for a measured failure: every ending is one of
the pinned ``TERMINATIONS``.

**Inputs go through the bridge alone.** A held input is one `act` with a `lease_s` (see
`core/turn_loop.py` `_send` / `_collect_active` / `_release_active`), superseded by the next one and always
released, verified, before the outcome is returned.

**Holding a move through a rise needs one declared action that presses both.** The bridge applies one
declaration at a time: a second `act` supersedes the held one, so two concurrent leases do not hold two
inputs. When the manifest declares a chord over the move control and the rise control, the hop uses it
with a lease covering the flight. When it does not, `hop` ends `rejected` with the receipt reason and does
not fake the held move with repeated presses.

Every bound below is named with where it comes from. Unknown measurements stay ``None``: a bound that
needs one is skipped, never treated as zero.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from playtest.contracts.bridge import Observation
from playtest.core.bridge_client import BridgeError

from .model import OptionOutcome, OptionSpec
from .primitives import Primitives

# --- rates and windows --------------------------------------------------------------------
# The spec's "Layers and rates": the option executor runs at observation rate, about 20 Hz.
OBSERVATION_HZ = 20.0
PERIOD_S = 1.0 / OBSERVATION_HZ

# A request bound to a reading older than ten ticks is stale: the bridge rejects it rather than applying
# input to a state nobody measured. Ten observation periods.
MAX_AGE_S = 10 * PERIOD_S

# A block is called only over a window long enough for one fast tick to be noise: six observation
# periods, a bit under a third of a second.
BLOCK_WINDOW_S = 6 * PERIOD_S
# ... and only when progress over that window is under this share of what the measured speed would give.
BLOCK_SHARE = 0.25
# The same window also says the body has met something while approaching a launch: the measured rate
# falls under this share of its measured speed after having reached it.
LAUNCH_SHARE = 0.5

# --- bounds ------------------------------------------------------------------------------
# A fall is a drop equal to the measured rise peak: as far down as the body can go up in one rise.
FELL_SHARE_OF_RISE = 1.0
# With no measured rise, a fall is two measured short move steps: the smallest drop that is not one step.
FELL_MIN_STEPS = 2.0
# A distance-derived time bound is generous against measurement: three times the travel time at the
# measured speed, plus one base window for apply and settle.
TIMEOUT_TRAVEL_FACTOR = 3.0
TIMEOUT_BASE_S = 2 * BLOCK_WINDOW_S
# An open traverse (no requested distance) lasts one S1 decision: the spec's decision rate is about 1 Hz,
# and a longer hold would spend several decisions on one choice. Two periods of slack.
OPEN_TRAVERSE_S = 2.0
# No option may hold the turn longer than this, whatever its distance and speed work out to.
OPTION_MAX_S = 6.0
# The flight bound is the measured airtime with half again as much, for apply latency and a slow descent.
FLIGHT_BOUND_FACTOR = 1.5
# Landing is read from the measured y standing still over a window: four observation periods.
LANDING_WINDOW_S = 4 * PERIOD_S
# ... within a band of this share of the measured rise peak (a hovering body would not be still).
LANDING_EPS_SHARE = 0.02
# A hop counts as having cleared something when it ends at least one measured short move step beyond the
# furthest point reached before the rise input.
SUCCESS_MARGIN_STEPS = 1.0
# A rise has to be past its own apex before a landing can be read: the apex is halfway through the airtime.
APEX_SHARE = 0.5
# Placement settle: four observation periods, enough for a contact-resolved body to stop.
PLACE_SETTLE_S = 0.25

UP = 1


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _unit(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    parts = [_finite(item) for item in value]
    if any(part is None for part in parts):
        return None
    length = math.sqrt(sum(part * part for part in parts))  # type: ignore[operator]
    if length <= 0:
        return None
    return (parts[0] / length, parts[1] / length, parts[2] / length)  # type: ignore[operator]


def _position(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    parts = [_finite(item) for item in value]
    if any(part is None for part in parts):
        return None
    return (parts[0], parts[1], parts[2])  # type: ignore[index]


def _public_value(public: Mapping[str, Any], path: str) -> Any:
    """One declared reader's value from `observation.public`, by dotted path; None when it is absent."""
    value: Any = public
    for part in path.split("."):
        if isinstance(value, Mapping):
            if part not in value:
                return None
            value = value[part]
        else:
            return None
    return value


def _dead(value: Any, marker: Any) -> bool:
    """Whether a declared death or health reader has crossed to its dead value."""
    if value is None:
        return False
    if isinstance(value, bool) or isinstance(marker, bool):
        return bool(value) is True if marker is True else not bool(value)
    number = _finite(value)
    if number is None:
        return value == marker
    bound = _finite(marker)
    if bound is None:
        return False
    if bound <= 0:
        return number <= 0.0
    return number >= bound


def _dump(record: Any) -> dict[str, Any]:
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="json")
    if isinstance(record, Mapping):
        return dict(record)
    return {"type": "unknown", "value": repr(record)}


def _controlled(observation: Observation) -> Mapping[str, Any] | None:
    public = getattr(observation, "public", None)
    if not isinstance(public, Mapping):
        return None
    value = public.get("controlled")
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, Mapping) else None
    return None


def _probe(observation: Observation | None) -> Mapping[str, Any] | None:
    """The measured geometry published with that observation, or None when this tick carried none."""
    public = getattr(observation, "public", None) if observation is not None else None
    if not isinstance(public, Mapping):
        return None
    value = public.get("probe")
    return value if isinstance(value, Mapping) else None


def _probe_key(observation: Observation | None) -> tuple[str, tuple[float, ...]] | None:
    """What one probe sample is identified by: the point measured under the body, else the observation's `seq`.

    A repeated probe is the same sample: its `under.point` has not moved, so the geometry around the body
    was not measured again. That is what dates a sample, and a body can travel a long way while it ages.
    """
    probe = _probe(observation)
    if probe is None:
        return None
    under = probe.get("under")
    point = under.get("point") if isinstance(under, Mapping) else None
    if isinstance(point, (list, tuple)) and point:
        measured = tuple(number for number in (_finite(item) for item in point) if number is not None)
        if len(measured) == len(point):
            return ("under.point", measured)
    seq = getattr(observation, "seq", None)
    if isinstance(seq, int) and not isinstance(seq, bool):
        return ("seq", (float(seq),))
    return None


def _floor_behind(probe: Any, step: float, horizon: float,
                  fell: float | None) -> tuple[bool | None, float | None]:
    """Whether the floor the body stands on continues behind it, reading every backward ray that matters.

    The stop must land before the body can leave the floor, not after: between one sample and the next the
    body travels `horizon` backwards (`step` in one observation period plus however far a probe sample can
    age), so every ray inside that distance is a place the floor could already have ended. A ray that
    misses has no floor under it; a ray that hits a surface more than the fell bound below the anchor is
    standing on nothing that continues this floor; any other measured hit is floor. Rays between the next
    step and the horizon are read together, and the nearest one that ends the floor comes back with it,
    minus the horizon's travel, as the floor still available now. When no ray lies inside the horizon the
    nearest one past it is the earliest reading the probe offers, and is read alone.

    No probe, no backward row, or an unmeasured ray is ``None`` — unknown, never a guess.
    """
    if not isinstance(probe, Mapping):
        return None, None
    rays = probe.get("backward")
    if not isinstance(rays, (list, tuple)) or not rays:
        return None, None
    measured: list[tuple[float, Mapping[str, Any]]] = []
    for entry in rays:
        if not isinstance(entry, Mapping):
            continue
        offset = _finite(entry.get("forward"))
        if offset is None or offset >= 0:
            continue
        behind = -offset
        if behind + 1e-9 < step:
            continue
        measured.append((behind, entry))
    if not measured:
        return None, None
    measured.sort(key=lambda pair: pair[0])
    inside = [pair for pair in measured if pair[0] <= horizon + 1e-9]
    window = inside or measured[:1]
    unknown = False
    for behind, entry in window:
        hit = entry.get("hit")
        if hit is True:
            height = _finite(entry.get("delta_y"))
            if height is None:                  # inside geometry: an obstruction, not a floor to run up on
                unknown = True
                continue
            if fell is not None and height < -fell:
                return False, behind
            continue
        if hit is False:
            return False, behind
        unknown = True
    return (None if unknown else True), None


@dataclass
class _Run:
    """One option's live state. Never shared between runs."""

    spec: OptionSpec
    started: float
    receipts: list[dict[str, Any]] = field(default_factory=list)
    start_position: list[float] | None = None
    end_position: list[float] | None = None
    scene: str | None = None
    terminated_by: str | None = None
    reason: str | None = None
    held: str | None = None
    acts: int = 0
    notes: list[str] = field(default_factory=list)

    def end(self, terminated_by: str, reason: str | None = None) -> None:
        if self.terminated_by is None:
            self.terminated_by = terminated_by
            self.reason = reason

    def note(self, text: str) -> None:
        """Keep one measured remark for the outcome, whatever ending the run reaches."""
        self.notes.append(text)


class _Progress:
    """Progress along one unit direction, and its rate over the trailing window."""

    def __init__(self, unit: Sequence[float], start: Sequence[float], window_s: float) -> None:
        self.unit = tuple(unit)
        self.start = tuple(start)
        self.window_s = window_s
        self.points: list[tuple[float, float]] = []
        self.best = 0.0

    def along(self, position: Sequence[float]) -> float:
        delta = (
            position[0] - self.start[0],
            position[1] - self.start[1],
            position[2] - self.start[2],
        )
        value = delta[0] * self.unit[0] + delta[1] * self.unit[1] + delta[2] * self.unit[2]
        return value

    def update(self, now: float, position: Sequence[float]) -> float:
        value = self.along(position)
        self.points.append((now, value))
        horizon = now - self.window_s
        # Keep the newest point at or before the horizon, so the measured span covers the whole window. Dropping
        # every point older than the horizon left a span always just short of it under a real, jittery clock, so
        # a check that needs a full window (a stall) could never fire.
        while len(self.points) > 1 and self.points[1][0] <= horizon:
            self.points.pop(0)
        self.best = max(self.best, value)
        return value

    @property
    def elapsed(self) -> float:
        return self.points[-1][0] - self.points[0][0] if len(self.points) >= 2 else 0.0

    def rate(self) -> float | None:
        """Measured progress rate over the trailing window, or None with too few samples."""
        if len(self.points) < 2:
            return None
        (t0, p0), (t1, p1) = self.points[0], self.points[-1]
        if t1 <= t0:
            return None
        return (p1 - p0) / (t1 - t0)


class OptionExecutor:
    """Runs one chosen option's closed loop and returns what it measured.

    `dead_fields` names manifest-declared death or health readers by their `observation.public` path and
    the value that means dead (``True`` for a flag, ``0`` for a count that reaches zero). Only declared
    readers are watched: the executor invents none.
    """

    def __init__(
        self,
        client: Any,
        primitives: Primitives,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        dead_fields: Mapping[str, Any] | None = None,
        period_s: float = PERIOD_S,
        flight: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._primitives = primitives
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._dead_fields = dict(dead_fields or {})
        self._period_s = period_s
        # Flights measured by earlier hops in this run, shared by the caller across executors. A hop's take-off
        # lead uses the measured time to apex once one exists, instead of the symmetric-flight assumption.
        self._flight = flight if flight is not None else {}
        self._last_tick: float | None = None
        self._last_obs: Observation | None = None
        # The observation stream's own timing, measured rather than assumed: how often an observation
        # arrives, and how often a probe sample is actually a new one. A run-up's stopping distance is
        # both of them times the body's measured speed.
        self._tick_times: list[float] = []
        self._last_probe_key: Any = None
        self._probe_changed_at: float | None = None
        self._probe_intervals: list[float] = []

    # --- the port ------------------------------------------------------------------------

    async def run(self, spec: OptionSpec) -> OptionOutcome:
        """Run one option. Always releases held input; never raises for a measured failure."""
        run = _Run(spec=spec, started=self._clock())
        self._last_tick = None
        self._tick_times = []
        self._last_probe_key = None
        self._probe_changed_at = None
        self._probe_intervals = []
        try:
            if spec.template == "wait":
                await self._wait(run)
            elif spec.template == "place":
                await self._place(run)
            elif spec.template == "hop":
                await self._hop(run)
            elif spec.template in ("traverse", "retreat"):
                await self._move(run)
            else:  # OptionSpec already rejects an unknown template; this is the fail-closed remainder.
                run.end("rejected", f"no executor for template {spec.template!r}")
        except BridgeError as exc:
            run.end("rejected" if exc.code != "closed" else "unmeasured",
                    f"bridge error {exc.code}: {exc.message}")
        except asyncio.TimeoutError:
            run.end("unmeasured", "no observation arrived in time")
        finally:
            await self._release(run)
        return self._outcome(run)

    # --- outcome -------------------------------------------------------------------------

    def _outcome(self, run: _Run) -> OptionOutcome:
        start, end = run.start_position, run.end_position
        displacement = None
        if start is not None and end is not None:
            displacement = [end[0] - start[0], end[1] - start[1], end[2] - start[2]]
        receipts = list(run.receipts)
        if run.reason is not None:
            receipts.append({
                "type": "option_note",
                "option_id": run.spec.option_id,
                "terminated_by": run.terminated_by,
                "reason": run.reason,
            })
        for note in run.notes:
            # Facts measured on the way to the ending, kept beside it: what the option did before it ended is
            # part of what it measured, not a replacement for why it ended.
            receipts.append({
                "type": "option_note",
                "option_id": run.spec.option_id,
                "terminated_by": run.terminated_by,
                "reason": note,
            })
        duration: float | None = None
        if run.terminated_by is not None:
            duration = max(0.0, self._clock() - run.started)
        return OptionOutcome(
            option_id=run.spec.option_id,
            template=run.spec.template,
            params=dict(run.spec.params),
            start_position=start,
            end_position=end,
            displacement=displacement,
            terminated_by=run.terminated_by or "unmeasured",
            duration_s=duration,
            receipts=receipts,
            scene=run.scene,
        )

    # --- observation and input ------------------------------------------------------------

    async def _pace(self) -> None:
        """Hold the loop at observation rate: sleep only the part of the period the bridge left over."""
        now = self._clock()
        if self._last_tick is not None:
            wait = self._period_s - (now - self._last_tick)
            if wait > 0:
                await self._sleep(wait)
        self._last_tick = self._clock()

    async def _tick(self, run: _Run) -> tuple[float, float, float] | None:
        """One observation at observation rate: the measured pose, or None once the run has ended."""
        await self._pace()
        try:
            observation = await self._client.observe_once()
        except BridgeError as exc:
            run.end("unmeasured" if exc.code in ("timeout", "closed") else "rejected",
                    f"observation failed: {exc.code}: {exc.message}")
            return None
        self._last_obs = observation
        self._note_stream(observation)
        run.scene = run.scene or observation.scene
        reason = self._state_change(run, observation)
        if reason is not None:
            run.end("state_changed", reason)
            return None
        controlled = _controlled(observation)
        position = _position(controlled.get("position")) if controlled is not None else None
        if position is None:
            run.end("state_changed" if run.start_position is not None else "unmeasured",
                    "no measured controlled pose in this observation")
            return None
        if run.start_position is None:
            run.start_position = list(position)
        run.end_position = list(position)
        return position

    def _note_stream(self, observation: Observation) -> None:
        """Time the observation stream: one arrival stamp, and one stamp per genuinely new probe sample.

        A probe that is republished unchanged is not a new measurement, so counting observations instead of
        sample changes would call a stale sample fresh and let the body outrun the geometry it read.
        """
        now = self._clock()
        self._tick_times = [*self._tick_times, now][-6:]
        key = _probe_key(observation)
        if key is None:
            return
        if self._last_probe_key is None:
            self._probe_changed_at = now
        elif key != self._last_probe_key:
            if self._probe_changed_at is not None:
                gap = now - self._probe_changed_at
                if gap > 0:
                    self._probe_intervals = [*self._probe_intervals, gap][-5:]
            self._probe_changed_at = now
        self._last_probe_key = key

    def _observation_interval_s(self) -> float:
        """How long one observation takes here: the median gap between the last few, else the configured period."""
        gaps = sorted(b - a for a, b in zip(self._tick_times, self._tick_times[1:]) if b > a)
        return gaps[len(gaps) // 2] if gaps else self._period_s

    def _probe_staleness_s(self) -> tuple[float, bool]:
        """How old a probe sample may be at the moment the body acts on it, and whether that was estimated.

        Measured as the interval between changes of the probe's `under.point` (or its observation `seq`) in
        this run's own stream. Until two samples have changed there is nothing to measure, and the bound is
        twice the observed observation interval — stated as an estimate, never as a reading.
        """
        if self._probe_intervals:
            ordered = sorted(self._probe_intervals)
            return ordered[len(ordered) // 2], False
        return 2.0 * self._observation_interval_s(), True

    def _state_change(self, run: _Run, observation: Observation) -> str | None:
        if run.scene is not None and observation.scene != run.scene:
            return f"scene changed from {run.scene} to {observation.scene}"
        if not observation.playing:
            return "the game stopped playing"
        if observation.paused:
            return "the game paused"
        if _finite(observation.time_scale) == 0.0:
            return "the game clock stopped"
        public = observation.public if isinstance(observation.public, Mapping) else {}
        for path, marker in self._dead_fields.items():
            value = _public_value(public, path)
            if _dead(value, marker):
                return f"declared reader {path} reads {value!r}"
        return None

    async def _hold(self, run: _Run, action: str, observation: Observation, lease_s: float) -> bool:
        """Start one held input with a lease, and keep its receipts."""
        run.acts += 1
        action_id = f"{run.spec.option_id}.{run.acts}"
        try:
            records = await self._client.start_act(
                action_id=action_id,
                choice_id=run.spec.option_id,
                action=action,
                params={},
                based_on_seq=observation.seq,
                max_age_s=MAX_AGE_S,
                lease_s=lease_s,
            )
        except BridgeError as exc:
            run.end("rejected", f"the bridge refused {action!r}: {exc.code}: {exc.message}")
            return False
        for record in records:
            run.receipts.append(_dump(record))
        status = records[-1].status
        if getattr(status, "value", status) == "rejected":
            run.end("rejected", f"{action!r} was rejected: {records[-1].reject_reason}")
            return False
        run.held = action_id
        return True

    async def _release(self, run: _Run) -> None:
        """Release every held input, verified, whatever the run's ending was."""
        held, run.held = run.held, None
        try:
            result = await self._client.stop("release")
        except BridgeError as exc:
            run.receipts.append({"type": "stopped", "released": [], "release_verified": False,
                                 "error": f"{exc.code}: {exc.message}"})
            return
        if result is None:
            run.receipts.append({"type": "stopped", "released": [], "release_verified": False})
            return
        run.receipts.append(_dump(result))
        if held is not None:
            try:
                for record in await self._client.collect_act(held):
                    run.receipts.append(_dump(record))
            except (BridgeError, KeyError):
                pass

    # --- bounds --------------------------------------------------------------------------

    def _time_bound(self, distance: float | None, speed: float | None) -> float:
        if distance is not None and speed:
            bound = distance / speed * TIMEOUT_TRAVEL_FACTOR + TIMEOUT_BASE_S
        else:
            bound = OPEN_TRAVERSE_S
        return min(OPTION_MAX_S, bound)

    def _measured_apex_s(self) -> float | None:
        """The median time to apex measured by earlier hops' flights in this run, or None before the first."""
        times = sorted(self._flight.get("time_to_apex_s", []))
        return times[len(times) // 2] if times else None

    def _record_apex(self, apex: tuple[float, float] | None, launch_at: Sequence[float] | None) -> None:
        """Keep this flight's measured time to apex when it really rose: its highest sample is above take-off
        by more than the landing tolerance and came before the flight ended."""
        if apex is None or launch_at is None:
            return
        elapsed, height = apex
        if elapsed > 0 and height - launch_at[UP] > self._landing_eps():
            self._flight.setdefault("time_to_apex_s", []).append(elapsed)

    def _fell_bound(self) -> float | None:
        candidates: list[float] = []
        if self._primitives.rise_peak is not None:
            candidates.append(self._primitives.rise_peak * FELL_SHARE_OF_RISE)
        if self._primitives.short_step is not None:
            candidates.append(self._primitives.short_step * FELL_MIN_STEPS)
        return max(candidates) if candidates else None

    def _success_margin(self, speed: float | None) -> float:
        step = self._primitives.short_step
        if step is not None:
            return step * SUCCESS_MARGIN_STEPS
        return (speed or 0.0) * self._period_s

    def _landing_eps(self) -> float:
        peak = self._primitives.rise_peak
        if peak is None:
            return 1e-6
        return max(1e-6, peak * LANDING_EPS_SHARE)

    # --- the templates -------------------------------------------------------------------

    async def _wait(self, run: _Run) -> None:
        """Hold nothing for the requested seconds. Always succeeds, by the spec's own table."""
        seconds = _finite((run.spec.params or {}).get("seconds"))
        if seconds is None or seconds < 0:
            run.end("rejected", "wait needs a non-negative measured duration")
            return
        deadline = run.started + seconds
        while True:
            position = await self._tick(run)
            if run.terminated_by is not None:
                return
            if position is not None and self._clock() >= deadline:
                run.end("succeeded")
                return

    async def _move(self, run: _Run) -> None:
        """Hold one measured direction until a measured condition ends it (traverse, retreat)."""
        params = run.spec.params or {}
        unit = _unit(params.get("direction"))
        if unit is None:
            run.end("rejected", "the option carries no measured direction")
            return
        direction = self._primitives.aligned(unit)
        action = params.get("action") or (direction.action if direction else None)
        if not action:
            run.end("rejected", "no offered action is measured along this direction")
            return
        speed = _finite(params.get("speed")) or (direction.speed if direction else None)
        distance = _finite(params.get("distance"))
        bound = self._time_bound(distance, speed)

        position = await self._tick(run)
        if position is None:
            return
        assert run.start_position is not None
        progress = _Progress(unit, run.start_position, BLOCK_WINDOW_S)
        if not await self._hold(run, str(action), self._observation(), lease_s=bound):
            return

        while run.terminated_by is None:
            position = await self._tick(run)
            if position is None:
                return
            now = self._clock()
            value = progress.update(now, position)
            if distance is not None and value >= distance:
                run.end("succeeded", f"progress {value:.3f} reached {distance:.3f}")
                return
            if self._blocked(run, progress, speed, BLOCK_SHARE):
                return
            if self._fell(run):
                return
            if now - run.started >= bound:
                run.end("timed_out", f"{now - run.started:.2f} s past the bound {bound:.2f} s")
                return

    async def _hop(self, run: _Run) -> None:
        """Run up, launch with the move held through the rise, and end on a measured landing."""
        params = run.spec.params or {}
        unit = _unit(params.get("direction"))
        if unit is None:
            run.end("rejected", "the option carries no measured direction")
            return
        direction = self._primitives.aligned(unit)
        move_action = params.get("action") or (direction.action if direction else None)
        if not move_action:
            run.end("rejected", "no offered action is measured along this direction")
            return
        # A spec that names its chord is taken at its word: a declared absence is a statement about the
        # catalog, not a gap to fill. Only a spec that says nothing about chords falls back to the
        # measured catalog, so the executor never invents a chord the caller ruled out.
        chord = params["chord"] if "chord" in params else self._primitives.chord_for(str(move_action))
        if not chord:
            run.end("rejected", HOP_NEEDS_CHORD)
            return
        rise_duration = _finite(params.get("flight_s")) or self._primitives.rise_duration_s
        if rise_duration is None:
            run.end("rejected", "the rise has no measured airtime, so a flight has no bound")
            return
        speed = _finite(params.get("speed")) or (direction.speed if direction else None)
        run_up = _finite(params.get("run_up"))
        launch = _finite(params.get("launch_distance"))
        # `launch` is how far short of the hop's start to take off; the obstacle a hop is chosen for is at its
        # start. Without a stated value it is the distance covered to apex at measured speed.
        if launch is None:
            launch = (speed or 0.0) * rise_duration / 2.0 if speed else 0.0
        measured_apex = self._measured_apex_s()
        if measured_apex is not None and speed and launch:
            launch = speed * measured_apex
        flight_bound = min(OPTION_MAX_S, rise_duration * FLIGHT_BOUND_FACTOR)
        fell = self._fell_bound()
        back_action = params.get("run_up_action")
        if run_up and back_action is None:
            back = self._primitives.opposite(unit)
            back_action = back.action if back else None
            if back_action is None:
                run.end("rejected", "a run-up needs an action measured opposite this direction")
                return

        start = await self._tick(run)
        if start is None:
            return
        assert run.start_position is not None
        progress = _Progress(unit, run.start_position, BLOCK_WINDOW_S)

        # Phase 0: find what is to be cleared, by measurement. Advance toward the direction until the body
        # stalls: that point is the obstacle. A hop chosen pressed against it stalls at once. If nothing stalls
        # the body within the run-up distance there is no obstacle to find, and the hop is a running jump from
        # where it is, already at speed.
        obstacle: tuple[float, float, float] | None = tuple(start)
        launch_at: tuple[float, float, float] | None = None
        if run_up:
            if not await self._hold(run, str(move_action), self._observation(),
                                    lease_s=self._time_bound(run_up, speed)):
                return
            seek = _Progress(unit, start, BLOCK_WINDOW_S)
            while run.terminated_by is None:
                position = await self._tick(run)
                if position is None:
                    return
                now = self._clock()
                seek.update(now, position)
                progress.update(now, position)
                rate = seek.rate()
                if (rate is not None and speed and seek.elapsed >= BLOCK_WINDOW_S
                        and rate < BLOCK_SHARE * speed):
                    obstacle = tuple(position)
                    break
                if seek.along(position) >= run_up:
                    obstacle, launch_at = None, tuple(position)
                    break
                # The floor ended: the body has started to drop with no rise input. A gap is cleared by taking
                # off where the floor ends, so launch now, already moving toward it.
                if position[1] < start[1] - self._landing_eps():
                    obstacle, launch_at = None, tuple(position)
                    break
                if self._fell(run) or self._timeout(run, now):
                    return
            if run.terminated_by is not None:
                return

        if obstacle is not None:
            reference = _Progress(unit, obstacle, BLOCK_WINDOW_S)
            # Phase 1: the run-up, away from the obstacle.
            origin = obstacle
            if run_up:
                if not await self._hold(run, str(back_action), self._observation(),
                                        lease_s=self._time_bound(run_up, speed)):
                    return
                back_progress = _Progress((-unit[0], -unit[1], -unit[2]), obstacle, BLOCK_WINDOW_S)
                # A run-up stays on the floor it starts on: between one observation and the next the body
                # travels `step`, and a probe sample can be older than the observation that carried it, so
                # the floor behind is read from every backward down-ray inside that whole stopping distance.
                step = (speed or 0.0) * self._period_s
                floor_y = obstacle[UP]
                while run.terminated_by is None:
                    position = await self._tick(run)
                    if position is None:
                        return
                    now = self._clock()
                    backed_up = max(0.0, back_progress.update(now, position))
                    if backed_up >= run_up:
                        break
                    if self._blocked(run, back_progress, speed, BLOCK_SHARE):
                        return
                    staleness, estimated = self._probe_staleness_s()
                    margin = (speed or 0.0) * (staleness + self._period_s)
                    holds, edge = _floor_behind(_probe(self._last_obs), step, margin + step, fell)
                    if holds is False and edge is not None:
                        note = (f"run-up {backed_up:.2f} available of {run_up:.2f} "
                                f"(floor edge at {edge - margin:.2f} behind")
                        if estimated:
                            note += f"; staleness {staleness:.2f} s estimated"
                        run.note(note + ")")
                        break
                    # Without a probe behind, the body walking off the edge is the measurement: it has already
                    # left the floor the run-up started on, so stop there and keep everything afterwards honest.
                    if position[UP] < floor_y - self._landing_eps():
                        run.note(f"run-up truncated: floor ended after {backed_up:.2f}")
                        break
                    if self._fell(run) or self._timeout(run, now):
                        return
                if run.terminated_by is not None:
                    return
                origin = tuple(position)

            # Phase 2: run in, and take off `launch` short of the obstacle, or at a measured stall.
            if not await self._hold(run, str(move_action), self._observation(),
                                    lease_s=self._time_bound(run_up or 0.0, speed)):
                return
            approach = _Progress(unit, origin, BLOCK_WINDOW_S)
            while run.terminated_by is None:
                position = await self._tick(run)
                if position is None:
                    return
                now = self._clock()
                approach.update(now, position)
                progress.update(now, position)
                if reference.along(position) >= -(launch or 0.0):
                    launch_at = tuple(position)
                    break
                rate = approach.rate()
                if (rate is not None and speed and approach.elapsed >= BLOCK_WINDOW_S
                        and approach.best >= self._success_margin(speed)
                        and rate < LAUNCH_SHARE * speed):
                    launch_at = tuple(position)
                    break
                if self._fell(run) or self._timeout(run, now):
                    return
            if run.terminated_by is not None:
                return
        assert launch_at is not None
        launch_progress = progress.along(launch_at)

        # Phase 3: the flight. The chord holds the move and the rise inputs together for its lease.
        if not await self._hold(run, str(chord), self._observation(), lease_s=flight_bound):
            return
        flight_start = self._clock()
        landing = _Landing(rise_duration, self._landing_eps())
        apex: tuple[float, float] | None = None  # (elapsed, height) of the highest sample this flight
        while run.terminated_by is None:
            position = await self._tick(run)
            if position is None:
                return
            now = self._clock()
            progress.update(now, position)
            if apex is None or position[UP] > apex[1]:
                apex = (now - flight_start, position[UP])
            if self._fell(run):
                return
            if landing.landed(now - flight_start, position):
                break
            if now - flight_start >= flight_bound:
                run.end("timed_out", f"flight {now - flight_start:.2f} s past the bound "
                                     f"{flight_bound:.2f} s")
                return

        landed_at = tuple(run.end_position or launch_at)
        self._record_apex(apex, launch_at)
        margin = self._success_margin(speed)
        if obstacle is not None:
            # Cleared means landing past the measured obstacle, not merely past the take-off point.
            beyond = _Progress(unit, obstacle, BLOCK_WINDOW_S).along(landed_at)
            outcome = "succeeded" if beyond >= margin else "blocked"
            took_off = _Progress(unit, obstacle, BLOCK_WINDOW_S).along(launch_at)
            flight = ("apex unmeasured" if apex is None else
                      f"apex {apex[1] - launch_at[UP]:+.3f} u at {apex[0]:.2f} s after take-off")
            run.end(outcome, f"landed {beyond:.3f} u beyond the measured obstacle at "
                             f"{[round(v, 3) for v in obstacle]}, margin {margin:.3f}; took off {took_off:+.3f} u "
                             f"from it (lead {launch or 0.0:.3f}); {flight}")
            return
        gained = progress.along(landed_at) - launch_progress
        if gained >= margin:
            run.end("succeeded", f"landed {gained:.3f} u beyond the launch point, margin {margin:.3f}")
        else:
            run.end("blocked", f"landed {gained:.3f} u beyond the launch point, margin {margin:.3f}")

    def _blocked(self, run: _Run, progress: _Progress, speed: float | None, share: float) -> bool:
        """End the run when progress over the window is under its share of the measured speed."""
        rate = progress.rate()
        if rate is None or not speed or progress.elapsed < BLOCK_WINDOW_S:
            return False
        if rate >= share * speed:
            return False
        run.end("blocked", f"progress rate {rate:.3f} u/s over {progress.elapsed:.2f} s, "
                           f"against a measured {speed:.3f} u/s")
        return True

    def _fell(self, run: _Run) -> bool:
        """End the run on a vertical drop past the primitive-derived bound."""
        bound = self._fell_bound()
        position = run.end_position
        if bound is None or position is None or run.start_position is None:
            return False
        drop = run.start_position[UP] - position[UP]
        if drop <= bound:
            return False
        run.end("fell", f"dropped {drop:.3f} u, past the measured bound {bound:.3f}")
        return True

    def _timeout(self, run: _Run, now: float) -> bool:
        if now - run.started < OPTION_MAX_S:
            return False
        run.end("timed_out", f"{now - run.started:.2f} s past the cap {OPTION_MAX_S:.2f} s")
        return True

    def _observation(self) -> Observation:
        """The observation that most recently bound the loop.

        Every input is bound to the tick it was chosen from (`based_on_seq`), which is what the bridge
        validates against, so the act names exactly the reading the option was running on.
        """
        return self._last_obs

    # --- placement -----------------------------------------------------------------------

    async def _place(self, run: _Run) -> None:
        """Put the controlled entity at a measured position and report what the physics did."""
        params = run.spec.params or {}
        position = _position(params.get("position"))
        if position is None:
            run.end("rejected", "place needs a measured position")
            return
        settle = _finite(params.get("settle_s"))
        settle = PLACE_SETTLE_S if settle is None else settle
        place = getattr(self._client, "place", None)
        if place is None:
            run.end("rejected", "this bridge client has no place verb")
            return
        try:
            result = await place(list(position), settle)
        except BridgeError as exc:
            run.end("rejected", f"place failed: {exc.code}: {exc.message}")
            return
        record = _dump(result)
        record.setdefault("type", "place")
        run.receipts.append(record)
        after = _position(record.get("position_after_settle"))
        run.end_position = list(after) if after is not None else None
        if not record.get("placed"):
            run.end("rejected", f"placement was not applied: {record.get('reason')}")
            return
        run.end("succeeded", None)


# The reason a hop cannot run, stated so the bridge work it needs is unambiguous.
HOP_NEEDS_CHORD = (
    "no declared action presses the move control and the rise control together. The bridge applies one "
    "declaration at a time: a second act supersedes the held one, so a move cannot be held across a "
    "separate rise press with two leases. Needed: a manifest chord whose inputs are the measured move "
    "control and the measured rise control (applied as one declaration, held for its lease), or a bridge "
    "verb that adds an input without releasing the held one. Alternating presses are not a substitute."
)


@dataclass
class _Landing:
    """Read a landing from measured y standing still after the measured apex."""

    airtime: float
    eps: float
    points: list[tuple[float, float]] = field(default_factory=list)

    def landed(self, elapsed: float, position: Sequence[float]) -> bool:
        self.points.append((elapsed, position[UP]))
        if elapsed < self.airtime * APEX_SHARE:
            return False
        horizon = elapsed - LANDING_WINDOW_S
        while len(self.points) > 2 and self.points[0][0] < horizon:
            self.points.pop(0)
        heights = [height for _t, height in self.points]
        if len(heights) < 2:
            return False
        span = self.points[-1][0] - self.points[0][0]
        if span < LANDING_WINDOW_S * 0.75:
            return False
        return max(heights) - min(heights) <= self.eps
