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
from datetime import datetime, timezone
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
SWEEP_HOLD_S = 0.2     # one press of each control a sweep tries
SWEEP_SETTLE_S = 0.6   # how long after each press a sweep watches for a change
PRESS_TEST_READ_S = 0.5   # how often a press test reads the readers through a wait: about a turn's pace

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

# --- the take-off (docs/design/borrowed-from-autopilot.md, item 1) ------------------------
# The press lands within a step of the decision, and the pose the decision read is already older than the
# decision: half an observation period for the press, plus the measured age of the pose.
PRESS_HALF_PERIODS = 0.5
# Speed has stopped rising when it is no faster than it was this long ago, within this share of it. Both
# numbers are the source's own (0.3 s, 4%), measured here from the pose stream.
PLATEAU_LOOKBACK_S = 0.3
PLATEAU_SHARE = 0.04
# ... and only speeds measured this recently are compared: the source's own half-second window, so a body
# that was slow a while ago and has since reached its run speed is judged on the speed it has now.
SPEED_WINDOW_S = 0.5
# One speed sample spans this many observations, so a reading taken across a change of direction is the
# small speed the body really had, not the sum of the two it had either side of the turn.
SPEED_SPAN_TICKS = 2
# Speed samples kept per approach: three seconds at observation rate, well past any lookback.
SPEED_SAMPLES = 64
# The body has run past its take-off point when it is inside this share of its lead without reaching plateau.
# A plateau that is slow compared with the run speed is not a run past it: it met something on the way in.
RAN_PAST_SHARE = 0.5
# Attempts inside one hop, each with a fresh run-up, when an attempt never took off at speed. A hop that
# jumped and was blocked is a measured result and is not retried.
MAX_HOP_ATTEMPTS = 3
# A hop that ended against the obstacle with its body this close to it did not pass it: the highest point
# it reached is a lower bound on the wall's height there.
WALL_CONTACT_U = 0.3

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


def _body_to_world(unit: Sequence[float], observation: Any) -> tuple[float, float, float] | None:
    """A (right, 0, forward) unit in the entity's basis as a world unit, from the observation's measured yaw.

    The yaw convention is the calibration record's: forward = (sin yaw, 0, cos yaw), right = (cos yaw, 0,
    -sin yaw). `None` when the observation carries no yaw.
    """
    view = getattr(observation, "view", None)
    yaw = getattr(view, "yaw_deg", None) if view is not None else None
    if isinstance(view, Mapping):
        yaw = view.get("yaw_deg")
    yaw = _finite(yaw)
    if yaw is None:
        return None
    radians = math.radians(yaw)
    forward = (math.sin(radians), 0.0, math.cos(radians))
    right = (math.cos(radians), 0.0, -math.sin(radians))
    x = right[0] * unit[0] + forward[0] * unit[2]
    z = right[2] * unit[0] + forward[2] * unit[2]
    length = math.hypot(x, z)
    return None if length <= 0 else (x / length, 0.0, z / length)


def _pose(observation: Any) -> tuple[float, float, float] | None:
    """The controlled entity's measured position in an observation, or None."""
    controlled = _controlled(observation) if observation is not None else None
    return _position(controlled.get("position")) if controlled else None


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


def fell_bound(primitives: Any) -> float | None:
    """The vertical drop that counts as a fall: the measured rise peak, or two measured short steps.

    Shared with the turn loop's plain actions and skills (`playtest.runner.session`), so a drop means the same
    thing whatever choice made it. `None` when neither quantity is measured.
    """
    candidates: list[float] = []
    peak = getattr(primitives, "rise_peak", None)
    if peak is not None:
        candidates.append(peak * FELL_SHARE_OF_RISE)
    step = getattr(primitives, "short_step", None)
    if step is not None:
        candidates.append(step * FELL_MIN_STEPS)
    return max(candidates) if candidates else None


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


def _vector(value: Any) -> tuple[float, ...] | None:
    """A sequence of finite numbers as a tuple of floats, else None (unmeasured, never a default)."""
    if isinstance(value, (list, tuple)) and value and all(_finite(c) is not None for c in value):
        return tuple(float(c) for c in value)
    return None


def _rays_toward(probe: Mapping[str, Any], back: Sequence[float] | None) -> list[tuple[float, Mapping[str, Any]]]:
    """The probe's centre-line down-rays that point along `back` in the world, as (distance, entry).

    The probe's rows are named for the body's own frame (`samples` ahead, `backward` behind), and in a plane
    that frame follows the body's facing: a body backing up faces the way it backs, so the row that looks where
    it is going is the one named forward. Each row is therefore placed in the world by the probe's own basis
    (the sign of the entry's offset times `basis.forward`) and kept when it points along `back`. Without a basis
    vector or a direction, the row named backward is read as it was, so a probe without a basis is unchanged.
    """
    basis = probe.get("basis") if isinstance(probe.get("basis"), Mapping) else {}
    forward = _vector(basis.get("forward")) if basis else None
    out: list[tuple[float, Mapping[str, Any]]] = []
    for name in ("samples", "backward"):
        entries = probe.get(name)
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            offset = _finite(entry.get("forward"))
            lateral = _finite(entry.get("lateral"))
            if offset is None or offset == 0 or (lateral is not None and abs(lateral) > 1e-9):
                continue
            if forward is None or back is None or len(forward) != len(back):
                if name == "backward" and offset < 0:
                    out.append((-offset, entry))
                continue
            direction = [c * (1.0 if offset > 0 else -1.0) for c in forward]
            if sum(d * b for d, b in zip(direction, back)) > 0.5:
                out.append((abs(offset), entry))
    return out


def _floor_behind(probe: Any, step: float, horizon: float,
                  floor_tolerance: float, back: Sequence[float] | None = None) -> tuple[bool | None, float | None]:
    """Whether the floor the body stands on continues behind it, reading every backward ray that matters.

    The stop must land before the body can leave the floor, not after: between one sample and the next the
    body travels `horizon` backwards (`step` in one observation period plus however far a probe sample can
    age), so every ray inside that distance is a place the floor could already have ended. A ray that
    misses has no floor under it; a ray that hits a surface below the current floor by more than the landing tolerance is
    standing on nothing that continues this floor; any other measured hit is floor. Rays between the next
    step and the horizon are read together, and the nearest one that ends the floor comes back with it,
    minus the horizon's travel, as the floor still available now. When no ray lies inside the horizon the
    nearest one past it is the earliest reading the probe offers, and is read alone.

    No probe, no backward row, or an unmeasured ray is ``None`` — unknown, never a guess.
    """
    if not isinstance(probe, Mapping):
        return None, None
    measured: list[tuple[float, Mapping[str, Any]]] = []
    for behind, entry in _rays_toward(probe, back):
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
            if height < -floor_tolerance:
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
    measured: dict[str, Any] = field(default_factory=dict)

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


class _Speed:
    """The body's measured speed along one direction, and whether it has stopped rising.

    A sample is the distance travelled over `SPEED_SPAN_TICKS` observations, so a reading taken across a
    change of direction reads as the small speed the body really had at the turn. The stream is restarted
    at each approach: what matters is the speed of *this* run-in, not the one the body had before it.
    """

    def __init__(self, unit: Sequence[float]) -> None:
        self.unit = tuple(unit)
        self.points: list[tuple[float, float]] = []        # (time, progress along the direction)
        self.samples: list[tuple[float, float]] = []       # (time, measured speed)

    def _along(self, position: Sequence[float]) -> float:
        return (position[0] * self.unit[0] + position[1] * self.unit[1]
                + position[2] * self.unit[2])

    def update(self, now: float, position: Sequence[float]) -> float | None:
        """One observation: the measured speed, or None until two of them span a sample."""
        self.points.append((now, self._along(position)))
        del self.points[:-SPEED_SAMPLES]
        if len(self.points) <= SPEED_SPAN_TICKS:
            return None
        t0, p0 = self.points[-1 - SPEED_SPAN_TICKS]
        t1, p1 = self.points[-1]
        if t1 <= t0:
            return None
        speed = abs(p1 - p0) / (t1 - t0)
        self.samples.append((now, speed))
        del self.samples[:-SPEED_SAMPLES]
        return speed

    def plateau(self, now: float) -> tuple[bool, float | None]:
        """Whether the measured speed has stopped rising, and the fastest speed it measured on the way.

        "Stopped rising" is no faster than it was `PLATEAU_LOOKBACK_S` ago within `PLATEAU_SHARE`. Until the
        stream is that old the comparison is against every earlier sample but the current one, which is the
        same statement read over the time actually measured. Only samples from the last `SPEED_WINDOW_S`
        count: a speed the body had a while ago, before it reached its run speed, is not evidence it is slow
        now.
        """
        current = self.samples[-1][1] if self.samples else None
        window = [(stamp, value) for stamp, value in self.samples if now - stamp <= SPEED_WINDOW_S]
        if current is None or len(window) < 2:
            return False, None
        older = [value for stamp, value in window if now - stamp > PLATEAU_LOOKBACK_S]
        reference = older or [value for _stamp, value in window[:-1]]
        best = max(reference) if reference else None
        if best is None:
            return False, None
        return current <= best * (1.0 + PLATEAU_SHARE), best


@dataclass(frozen=True)
class _Lead:
    """Where the take-off was taken, and the two terms that made up its lead.

    The aim is one apex run before the obstacle, plus the distance the body travels while the decision is
    being made: the measured age of the pose it read, and half an observation for the press to land.
    """

    distance: float
    run_to_apex: float
    latency: float
    pose_age: float | None
    period_s: float

    def statement(self) -> str:
        age = "unmeasured" if self.pose_age is None else f"{self.pose_age:.3f} s"
        return (f"lead {self.distance:.3f} = {self.run_to_apex:.3f} run to apex + "
                f"{self.latency:.3f} latency (pose age {age} + half of a {self.period_s:.3f} s period)")


@dataclass
class _Hop:
    """What one hop's attempts share: the measured take-off geometry and the inputs to use."""

    unit: tuple[float, float, float]
    move_action: str
    chord: str
    back_action: str | None
    speed: float | None
    run_up: float | None
    launch: float
    rise_duration: float
    flight_bound: float
    floor_tolerance: float
    probe_wall: float | None = None
    air_press_s: float | None = None  # pressed again this long after take-off (`options.air_rise`), or never


class OptionExecutor:
    """Runs one chosen option's closed loop and returns what it measured.

    `dead_fields` names manifest-declared death or health readers by their `observation.public` path and
    the value that means dead (``True`` for a flag, ``0`` for a count that reaches zero). Only declared
    readers are watched: the executor invents none. `readers` (the session's field readers) is what a press
    test reads; without it a press test takes no readings, and its presses are scored as unread.
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
        wall_clock: Callable[[], datetime] | None = None,
        readers: Any = None,
    ) -> None:
        self._client = client
        self._readers = readers
        self._primitives = primitives
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        # The reader's wall clock, for the one measurement a monotonic clock cannot make: how old the pose
        # in an observation was when it arrived. The bridge stamps `wall_utc` when it takes the pose; this
        # is read the moment the observation is in hand.
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._dead_fields = dict(dead_fields or {})
        self._period_s = period_s
        # Flights measured by earlier hops in this run, shared by the caller across executors. A hop's take-off
        # lead uses the measured time to apex once one exists, instead of the symmetric-flight assumption.
        self._flight = flight if flight is not None else {}
        # Set by `interrupt`: S1 chose something else while this option ran (`--decide-during-options`).
        self._interrupted = asyncio.Event()
        self._interrupt_reason: str | None = None
        # Cleared for a whole hop, run-up and flight: a hop cut short anywhere is a jump that falls short, so an
        # interrupt takes effect when it has landed (or ended on its own measure).
        self._uncommitted = asyncio.Event()
        self._uncommitted.set()
        self._last_tick: float | None = None
        self._last_obs: Observation | None = None
        # The observation stream's own timing, measured rather than assumed: how often an observation
        # arrives, and how often a probe sample is actually a new one. A run-up's stopping distance is
        # both of them times the body's measured speed.
        self._tick_times: list[float] = []
        self._last_probe_key: Any = None
        self._probe_changed_at: float | None = None
        self._probe_intervals: list[float] = []
        # The measured age of the pose in the most recent observation: the bridge's capture stamp against
        # the reader's clock when it arrived. None until one was measured on a pair that makes sense.
        self._pose_age_s: float | None = None

    # --- the port ------------------------------------------------------------------------

    async def run(self, spec: OptionSpec) -> OptionOutcome:
        """Run one option. Always releases held input; never raises for a measured failure."""
        run = _Run(spec=spec, started=self._clock())
        self._last_tick = None
        self._tick_times = []
        self._last_probe_key = None
        self._probe_changed_at = None
        self._probe_intervals = []
        self._pose_age_s = None
        work: asyncio.Future | None = None
        try:
            work = asyncio.ensure_future(self._template(run))
            stop = asyncio.ensure_future(self._interrupted.wait())
            try:
                done, _ = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.cancel()
            if work not in done and not self._uncommitted.is_set():
                # Committed: the hop runs to its landing, or to its own measured ending, before it stops.
                landing = asyncio.ensure_future(self._uncommitted.wait())
                try:
                    await asyncio.wait({work, landing}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    landing.cancel()
                done = {work} if work.done() else set()
            if work in done:
                work.result()  # the template's own errors reach the handlers below
            else:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                run.end("interrupted", self._interrupt_reason or "interrupted")
        except BridgeError as exc:
            run.end("rejected" if exc.code != "closed" else "unmeasured",
                    f"bridge error {exc.code}: {exc.message}")
        except asyncio.TimeoutError:
            run.end("unmeasured", "no observation arrived in time")
        finally:
            if work is not None and not work.done():  # this run itself was cancelled: so is its template
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await self._release(run)
        return self._outcome(run)

    def interrupt(self, reason: str) -> None:
        """End the running option at its next await, as `interrupted`, with its inputs released and measured.

        A hop is committed from its run-up to its landing: the interrupt takes effect then (or when it ends).
        """
        self._interrupt_reason = reason
        self._interrupted.set()

    async def until_landed(self) -> None:
        """Return once no committed option (a hop or a sweep) is under way (at once when none is)."""
        await self._uncommitted.wait()

    @property
    def committed(self) -> bool:
        """Whether an interrupt would wait: a hop is under way."""
        return not self._uncommitted.is_set()

    async def _template(self, run: _Run) -> None:
        spec = run.spec
        if spec.template == "wait":
            await self._wait(run)
        elif spec.template == "place":
            await self._place(run)
        elif spec.template == "hop":
            self._uncommitted.clear()
            try:
                await self._hop(run)
            finally:
                self._uncommitted.set()
        elif spec.template in ("traverse", "retreat"):
            await self._move(run)
        elif spec.template == "sweep":
            # Committed like a hop: each press is a measurement, and a press cut short measures nothing.
            self._uncommitted.clear()
            try:
                await self._sweep(run)
            finally:
                self._uncommitted.set()
        elif spec.template == "press_test":
            # Committed like a sweep: each press is a measurement, and its window cut short measures nothing.
            self._uncommitted.clear()
            try:
                await self._press_test(run)
            finally:
                self._uncommitted.set()
        else:  # OptionSpec already rejects an unknown template; this is the fail-closed remainder.
            run.end("rejected", f"no executor for template {spec.template!r}")

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
        params = dict(run.spec.params)
        return OptionOutcome(
            option_id=run.spec.option_id,
            template=run.spec.template,
            params=params,
            start_position=start,
            end_position=end,
            displacement=displacement,
            terminated_by=run.terminated_by or "unmeasured",
            duration_s=duration,
            receipts=receipts,
            scene=run.scene,
            measured=dict(run.measured) if run.measured else {},
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
        self._pose_age_s = self._measure_pose_age(observation)
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

    def _measure_pose_age(self, observation: Observation) -> float | None:
        """How old the pose in this observation was when it arrived, or None when that is not measurable.

        The bridge stamps the pose with `wall_utc`; the reader's clock is read the moment the observation is
        in hand. An age that is negative or past the bridge's own staleness bound is a clock disagreement,
        not a reading, so it is reported as unmeasured rather than used as a number.
        """
        captured = getattr(observation, "wall_utc", None)
        if not isinstance(captured, datetime):
            return None
        try:
            age = (self._wall_clock() - captured).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(age) or age < 0.0 or age > MAX_AGE_S:
            return None
        return age

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
        for attempt in (0, 1):
            run.acts += 1
            action_id = f"{run.spec.option_id}.{run.acts}"
            # What was asked for, beside what the bridge answered: a replay needs the action, not only its receipts.
            run.receipts.append({"type": "request", "action_id": action_id, "action": action, "lease_s": lease_s})
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
            stale = (getattr(records[-1].status, "value", records[-1].status) == "rejected"
                     and getattr(records[-1].reject_reason, "value", records[-1].reject_reason) == "stale_age")
            if not stale or attempt:
                break
            # The reading aged past the bound while the bridge served other requests: the same input once more,
            # bound to a fresh reading. A second stale rejection ends the option as rejected.
            try:
                observation = await self._client.observe_once()
            except BridgeError as exc:
                run.end("rejected", f"observation failed: {exc.code}: {exc.message}")
                return False
            self._last_obs = observation
            run.note(f"{action!r} was rejected stale_age; sent once more bound to seq {observation.seq}")
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
        return fell_bound(self._primitives)

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

    async def _sweep(self, run: _Run) -> None:
        """Each listed control once, with a settle between: ends at the first one the scene or session changed on.

        A scene change, or a session that ends right after a press, is `state_changed`; motion is measured and
        noted per control. Nothing pressed changing anything measured is `blocked`. Every control pressed is
        listed in `measured["tried"]`, so the place's history counts it as tried.
        """
        actions = [a for a in (run.spec.params or {}).get("actions") or () if isinstance(a, str) and a]
        tried: list[dict[str, Any]] = []
        run.measured["tried"] = tried
        first = await self._tick(run)
        if first is None:
            return
        scene = self._last_obs.scene
        for action in actions:
            before = self._observation()
            start = _pose(before)
            if not await self._hold(run, action, before, lease_s=SWEEP_HOLD_S):
                return
            ended = None
            settle_until = self._clock() + SWEEP_SETTLE_S
            while self._clock() < settle_until:
                await self._pace()
                try:
                    observation = await self._client.observe_once()
                except BridgeError as exc:
                    ended = f"the session ended after {action!r}: {exc.code}"
                    break
                self._last_obs = observation
                position = _pose(observation)
                if position is not None:
                    run.end_position = list(position)
                if observation.scene != scene:
                    ended = f"{action!r} changed the scene from {scene!r} to {observation.scene!r}"
                    break
            if ended is None:
                # Released and collected, so the press's measured end is in its receipts, not only its start.
                await self._release(run)
            end = _pose(self._last_obs)
            tried.append({"action": action, "start": list(start) if start else None,
                          "end": list(end) if end else None})
            if ended is not None:
                run.end("state_changed", ended)
                return
        run.end("blocked", f"none of {len(tried)} controls changed the scene")

    async def _press_test(self, run: _Run) -> None:
        """One control pressed `presses` times, each press followed by `wait_s` with nothing pressed, so what
        follows each press is its own. Ends `succeeded` after the last wait, or `state_changed` at a scene change.

        Committed like a sweep: S1 is not asked again until it ends, so it reads the session's readers itself,
        into `measured["readings"]`: just before each press, once while it is held, just after its release, and
        every `PRESS_TEST_READ_S` through each wait. Every press is released and collected, so its measured start and end are in the
        receipts; `measured["pressed"]` counts the presses made.
        """
        params = run.spec.params or {}
        action, presses, wait_s = params.get("action"), params.get("presses"), _finite(params.get("wait_s"))
        if (not isinstance(action, str) or not action or not isinstance(presses, int) or presses < 1
                or wait_s is None or wait_s < 0):
            run.end("rejected", "a press test needs a control, a positive press count and a non-negative wait")
            return
        run.measured["pressed"] = 0
        readings: list[dict[str, Any]] = []
        run.measured["readings"] = readings

        async def read() -> None:
            # The session's readers, read as a press reading is (`--press-readings`): a read that fails, or no
            # readers at all, is no reading, never a reading with nothing in it.
            if self._readers is None:
                return
            try:
                sample = await self._readers.read()
            except Exception:  # noqa: BLE001 - a reading that could not be taken is unmeasured
                return
            values = getattr(sample, "values", None)
            if isinstance(values, Mapping) and values:
                readings.append({"wall_s": self._wall_clock().timestamp(), "values": dict(values)})

        if await self._tick(run) is None:
            return
        for _ in range(presses):
            await read()
            if not await self._hold(run, action, self._observation(), lease_s=SWEEP_HOLD_S):
                return
            until = self._clock() + SWEEP_HOLD_S
            await read()   # while held: an effect shorter than the reading pace (a dash's flag) is still read
            while self._clock() < until:
                if await self._tick(run) is None:
                    return
            await self._release(run)
            run.measured["pressed"] += 1
            if await self._tick(run) is None:
                return
            await read()
            until, next_read = self._clock() + wait_s, self._clock() + PRESS_TEST_READ_S
            while self._clock() < until:
                if await self._tick(run) is None:
                    return
                if self._clock() >= next_read:
                    await read()
                    next_read += PRESS_TEST_READ_S
        await read()
        run.end("succeeded")

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
        if params.get("basis") == "body":
            # Measured in the entity's own basis: resolved once against its facing now. One held move does
            # not turn the entity, so the world direction holds for the whole traverse.
            world = _body_to_world(unit, self._last_obs)
            if world is None:
                run.end("rejected", "the entity's facing is unmeasured, so a body-relative direction has no "
                                    "world direction")
                return
            unit = world
            run.measured["world_direction"] = list(world)
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
        """Run up, launch with the move held through the rise, and end on a measured landing.

        The take-off is timed the way `jump_to` times it (docs/design/borrowed-from-autopilot.md, item 1):
        the lead carries the measured age of the pose the decision read, the press waits for the measured
        speed to stop rising, an attempt that ran past its take-off point at too low a speed is retried with a
        fresh run-up, and a flight that ended against the obstacle states the height it reached as a lower
        bound on the wall's. What is already known about the wall is stated before the hop runs and never
        refuses the hop S1 chose.
        """
        params = run.spec.params or {}
        unit = _unit(params.get("direction"))
        if unit is None:
            run.end("rejected", "the option carries no measured direction")
            return
        if params.get("basis") == "body":
            # Measured in the entity's own basis: resolved once against its facing now, as a traverse is.
            if await self._tick(run) is None:
                return
            world = _body_to_world(unit, self._last_obs)
            if world is None:
                run.end("rejected", "the entity's facing is unmeasured, so a body-relative direction has no "
                                    "world direction")
                return
            unit = world
            run.measured["world_direction"] = list(world)
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
        # `launch` is the run to the apex: how far short of the hop's start to take off. Without a stated value
        # it is the distance covered to apex at measured speed.
        if launch is None:
            launch = (speed or 0.0) * rise_duration / 2.0 if speed else 0.0
        measured_apex = self._measured_apex_s()
        if measured_apex is not None and speed and launch:
            launch = speed * measured_apex
        back_action = params.get("run_up_action")
        if run_up and back_action is None:
            back = self._primitives.opposite(unit)
            back_action = back.action if back else None
            if back_action is None:
                run.end("rejected", "a run-up needs an action measured opposite this direction")
                return
        hop = _Hop(
            unit=unit,
            move_action=str(move_action),
            chord=str(chord),
            back_action=back_action,
            speed=speed,
            run_up=run_up,
            launch=launch,
            rise_duration=rise_duration,
            flight_bound=min(OPTION_MAX_S, rise_duration * FLIGHT_BOUND_FACTOR),
            floor_tolerance=self._landing_eps(),
            air_press_s=_finite(params.get("air_press_s")),
        )

        start = await self._tick(run)
        if start is None:
            return
        assert run.start_position is not None
        progress = _Progress(unit, run.start_position, BLOCK_WINDOW_S)
        hop.probe_wall = self._probe_wall_lower(self._last_obs)
        # What is already known here, against what the rise reaches: stated up front, so the next label can
        # carry it. The hop still runs — code does not refuse the choice S1 made.
        self._reach_pre_check(run, params, hop.probe_wall)

        # Phase 0: find what is to be cleared, by measurement. Advance toward the direction until the body
        # stalls: that point is the obstacle. A hop chosen pressed against it stalls at once. If nothing stalls
        # the body within the run-up distance there is no obstacle to find, and the hop is a running jump from
        # where it is, already at speed.
        obstacle, launch_at = await self._hop_seek(run, hop, tuple(start), progress)
        if run.terminated_by is not None:
            return
        if obstacle is None:
            if launch_at is None:                      # the seek ended without a measurement to jump from
                return
            await self._hop_fly(run, hop, None, launch_at, progress)
            return

        attempt = 0
        while True:
            attempt += 1
            run.note(f"attempt {attempt} of {MAX_HOP_ATTEMPTS}")
            started = self._clock()
            retry = await self._hop_attempt(run, hop, obstacle, progress, started)
            if run.terminated_by is not None or not retry:
                return
            if attempt >= MAX_HOP_ATTEMPTS:
                break
        run.end("rejected", RAN_PAST_LOW_SPEED)

    async def _hop_seek(self, run: _Run, hop: _Hop, start: tuple[float, float, float],
                        progress: _Progress) -> tuple[tuple[float, float, float] | None,
                                                     tuple[float, float, float] | None]:
        """Find the obstacle a hop is chosen for, or the point to run off.

        Returns (obstacle, launch_at): the obstacle when one stalled the body, or None with the point at
        which a running jump is taken (the floor ending, or the run-up distance covered with nothing in the
        way).
        """
        if not hop.run_up:
            return start, None
        if not await self._hold(run, hop.move_action, self._observation(),
                                lease_s=self._time_bound(hop.run_up, hop.speed)):
            return None, None
        seek = _Progress(hop.unit, start, BLOCK_WINDOW_S)
        while run.terminated_by is None:
            position = await self._tick(run)
            if position is None:
                return None, None
            now = self._clock()
            seek.update(now, position)
            progress.update(now, position)
            rate = seek.rate()
            if (rate is not None and hop.speed and seek.elapsed >= BLOCK_WINDOW_S
                    and rate < BLOCK_SHARE * hop.speed):
                return tuple(position), None
            if seek.along(position) >= hop.run_up:
                return None, tuple(position)
            # The floor ended: the body has started to drop with no rise input. A gap is cleared by taking
            # off where the floor ends, so launch now, already moving toward it.
            if position[UP] < start[UP] - self._landing_eps():
                return None, tuple(position)
            if self._fell(run) or self._timeout(run, now):
                return None, None
        return None, None

    async def _hop_attempt(self, run: _Run, hop: _Hop, obstacle: Sequence[float],
                           progress: _Progress, started: float) -> bool:
        """One run-up, approach and flight. Returns True when it never took off at speed, so it is retried."""
        unit = hop.unit
        reference = _Progress(unit, obstacle, BLOCK_WINDOW_S)
        back_from = tuple(run.end_position or obstacle)
        origin: tuple[float, float, float] = back_from

        # Phase 1: the run-up, away from the obstacle (from wherever the body is, so a retry backs up again).
        if hop.run_up:
            if not await self._hold(run, str(hop.back_action), self._observation(),
                                    lease_s=self._time_bound(hop.run_up, hop.speed)):
                return False
            back_progress = _Progress((-unit[0], -unit[1], -unit[2]), back_from, BLOCK_WINDOW_S)
            # A run-up stays on the floor it starts on: between one observation and the next the body
            # travels `step`, and a probe sample can be older than the observation that carried it, so
            # the floor behind is read from every backward down-ray inside that whole stopping distance.
            step = (hop.speed or 0.0) * self._period_s
            floor_y = back_from[UP]
            while run.terminated_by is None:
                position = await self._tick(run)
                if position is None:
                    return False
                now = self._clock()
                backed_up = max(0.0, back_progress.update(now, position))
                if backed_up >= hop.run_up:
                    break
                if self._blocked(run, back_progress, hop.speed, BLOCK_SHARE):
                    return False
                staleness, estimated = self._probe_staleness_s()
                margin = (hop.speed or 0.0) * (staleness + self._period_s)
                holds, edge = _floor_behind(_probe(self._last_obs), step, margin + step, hop.floor_tolerance,
                                            back=tuple(-c for c in hop.unit))
                if holds is False and edge is not None:
                    note = (f"run-up {backed_up:.2f} available of {hop.run_up:.2f} "
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
                if self._fell(run) or self._timeout(run, now, started):
                    return False
            if run.terminated_by is not None:
                return False
            origin = tuple(run.end_position or back_from)

        # Phase 2: run in, and take off where the lead says, at a measured plateau in speed.
        if not await self._hold(run, hop.move_action, self._observation(),
                                lease_s=self._time_bound(hop.run_up or 0.0, hop.speed)):
            return False
        approach = _Progress(unit, origin, BLOCK_WINDOW_S)
        meter = _Speed(unit)
        lead = self._take_off_lead(hop.speed, hop.launch)
        # Whether this attempt had room to build speed before its take-off point at all: from a standing start
        # already inside the lead there is nothing to wait for, and the flight measures what it got.
        room = (-reference.along(origin)) > RAN_PAST_SHARE * lead.distance
        launch_at: tuple[float, float, float] | None = None
        while run.terminated_by is None:
            position = await self._tick(run)
            if position is None:
                return False
            now = self._clock()
            approach.update(now, position)
            progress.update(now, position)
            measured = meter.update(now, position)
            take_off_speed = measured or hop.speed
            lead = self._take_off_lead(take_off_speed, hop.launch)
            plateaued, plateau_speed = meter.plateau(now)
            remaining = -reference.along(position)
            if remaining <= lead.distance and (plateaued or not room):
                launch_at = tuple(position)
                break
            if room and remaining <= RAN_PAST_SHARE * lead.distance and not plateaued:
                # The lead point arrived before the measured speed stopped rising: the body was still
                # accelerating when it should have been jumping. No press at a speed it had not reached.
                detail = f"{remaining:.2f} u short of the obstacle"
                if take_off_speed is not None:
                    detail += f" at {take_off_speed:.2f} u/s"
                detail += (", its speed unmeasured" if plateau_speed is None
                           else f", still rising past {plateau_speed:.2f} u/s")
                run.note(f"{RAN_PAST_LOW_SPEED} ({detail}); back up for a fresh run-up")
                return True
            rate = approach.rate()
            if (rate is not None and plateaued and hop.speed and approach.elapsed >= BLOCK_WINDOW_S
                    and approach.best >= self._success_margin(hop.speed)
                    and rate < LAUNCH_SHARE * hop.speed):
                # Met something on the way in: the speed it had is gone and did not come back. A body still
                # rising through its run speed is not stalled, and waits for its take-off point instead.
                launch_at = tuple(position)
                break
            if self._fell(run) or self._timeout(run, now, started):
                return False
        if run.terminated_by is not None or launch_at is None:
            return False
        await self._hop_fly(run, hop, obstacle, launch_at, progress, lead=lead)
        return False

    async def _hop_fly(self, run: _Run, hop: _Hop, obstacle: Sequence[float] | None,
                       launch_at: tuple[float, float, float], progress: _Progress,
                       lead: _Lead | None = None) -> None:
        """Phase 3: the flight. The chord holds the move and the rise inputs together for its lease."""
        if not await self._hold(run, hop.chord, self._observation(), lease_s=hop.flight_bound):
            return
        await self._hop_flight(run, hop, obstacle, launch_at, progress, lead)

    async def _hop_flight(self, run: _Run, hop: _Hop, obstacle: Sequence[float] | None,
                          launch_at: tuple[float, float, float], progress: _Progress,
                          lead: _Lead | None = None) -> None:
        flight_start = self._clock()
        landing = _Landing(hop.rise_duration, self._landing_eps())
        apex: tuple[float, float] | None = None  # (elapsed, height) of the highest sample this flight
        pressed_again = hop.air_press_s is None
        while run.terminated_by is None:
            position = await self._tick(run)
            if position is None:
                return
            now = self._clock()
            if not pressed_again and now - flight_start >= hop.air_press_s:
                # Release, then the chord again: a new press of the rise with the body in the air, as measured.
                pressed_again = True
                await self._client.stop("release")
                run.note(f"pressed the rise again {now - flight_start:.2f} s after take-off")
                if not await self._hold(run, hop.chord, self._observation(),
                                        lease_s=max(PERIOD_S, hop.flight_bound - (now - flight_start))):
                    return
            progress.update(now, position)
            if apex is None or position[UP] > apex[1]:
                apex = (now - flight_start, position[UP])
            if self._fell(run):
                return
            if landing.landed(now - flight_start, position):
                break
            if now - flight_start >= hop.flight_bound:
                run.end("timed_out", f"flight {now - flight_start:.2f} s past the bound "
                                     f"{hop.flight_bound:.2f} s")
                return

        landed_at = tuple(run.end_position or launch_at)
        self._record_apex(apex, launch_at)
        margin = self._success_margin(hop.speed)
        apex_height = None if apex is None else apex[1] - launch_at[UP]
        bound = self._fell_bound()
        if bound is not None and launch_at[UP] - landed_at[UP] > bound:
            # Landing far below the take-off is a fall off the route, however far along it came down.
            run.end("fell", f"landed {launch_at[UP] - landed_at[UP]:.3f} u below its take-off, past the measured "
                            f"bound {bound:.3f}")
            return
        if obstacle is not None:
            # Cleared means landing past the measured obstacle, not merely past the take-off point.
            reference = _Progress(hop.unit, obstacle, BLOCK_WINDOW_S)
            beyond = reference.along(landed_at)
            took_off = reference.along(launch_at)
            outcome = "succeeded" if beyond >= margin else "blocked"
            flight = ("apex unmeasured" if apex is None else
                      f"apex {apex_height:+.3f} u at {apex[0]:.2f} s after take-off")
            lead_text = f"lead {hop.launch or 0.0:.3f}" if lead is None else lead.statement()
            run.end(outcome, f"landed {beyond:.3f} u beyond the measured obstacle at "
                             f"{[round(v, 3) for v in obstacle]}, margin {margin:.3f}; took off {took_off:+.3f} u "
                             f"from it ({lead_text}); {flight}")
            if outcome == "blocked":
                self._wall_measurement(run, hop, launch_at, apex, beyond)
            return
        gained = progress.along(landed_at) - progress.along(launch_at)
        if gained >= margin:
            run.end("succeeded", f"landed {gained:.3f} u beyond the launch point, margin {margin:.3f}")
        else:
            run.end("blocked", f"landed {gained:.3f} u beyond the launch point, margin {margin:.3f}")

    # --- the take-off's measurements ------------------------------------------------------

    def _take_off_lead(self, speed: float | None, run_to_apex: float) -> _Lead:
        """The distance before the obstacle at which the press must land, as one measured lead.

        The body travels `speed` for the time to the apex, and for however long the decision it is making was
        already old: the measured age of the pose it read, plus half an observation period for the press
        itself to land. Before an observation arrives there is no age to measure, and the half period alone is
        used, stated as such in the note.
        """
        period = self._observation_interval_s()
        age = self._pose_age_s
        latency = (speed or 0.0) * (PRESS_HALF_PERIODS * period + (age or 0.0))
        return _Lead(distance=run_to_apex + latency, run_to_apex=run_to_apex, latency=latency,
                     pose_age=age, period_s=period)

    def _probe_wall_lower(self, observation: Observation | None) -> float | None:
        """A lower bound on the height of the face ahead, from the probe's forward casts.

        `blockers` are cast heights measured against the floor under the body, so a cast that hit at 0.9 says
        the face there is taller than 0.9. The tallest cast that hit is the bound; no probe or no hit is
        unknown, never zero.
        """
        probe = _probe(observation)
        if not isinstance(probe, Mapping):
            return None
        blockers = probe.get("blockers")
        if not isinstance(blockers, (list, tuple)) or not blockers:
            return None
        hits: list[float] = []
        for entry in blockers:
            if not isinstance(entry, Mapping) or entry.get("hit") is not True:
                continue
            height = _finite(entry.get("height"))
            if height is not None:
                hits.append(height)
        return max(hits) if hits else None

    def _reach_pre_check(self, run: _Run, params: Mapping[str, Any],
                         probe_wall: float | None) -> None:
        """State a wall already known to be out of reach against the measured reach, before the hop runs."""
        known = _finite((params or {}).get("wall_min_height"))
        walls = [wall for wall in (known, probe_wall) if wall is not None]
        peak = self._primitives.rise_peak
        if not walls or peak is None:
            return
        reach = peak + (self._primitives.short_step or 0.0)
        wall = max(walls)
        if wall <= reach:
            return
        source = "memory" if known is not None and known == wall else "probe"
        run.note(f"reach {reach:.1f} < wall ≥ {wall:.1f}: expected blocked ({source}; the hop runs anyway)")

    def _wall_measurement(self, run: _Run, hop: _Hop,
                          launch_at: Sequence[float], apex: tuple[float, float] | None,
                          beyond: float) -> None:
        """A flight that ended against the obstacle says how high the body got, and that it did not pass.

        The wall's top is not measured here, so what is stated is a bound: a body that ended its flight against
        it with its highest point at `A` above take-off never got that high. `params["measured"]` carries the
        same number, so the next label can compare it with what a rise reaches.
        """
        if apex is None:
            return
        if not (-WALL_CONTACT_U <= beyond <= WALL_CONTACT_U):
            return                                 # landed away from it: this says nothing about its height
        height = apex[1] - launch_at[UP]
        note = (f"wall higher than +{height:.3f} u at this spot: the body ended against it with its apex "
                f"+{height:.3f} u above take-off at {apex[0]:.2f} s and did not pass it")
        if hop.probe_wall is not None:
            note += f"; wall ≥{hop.probe_wall:g} (probe)"
        run.note(note)
        run.measured["wall_min_height"] = round(height, 6)

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

    def _timeout(self, run: _Run, now: float, started: float | None = None) -> bool:
        """End the run past the option cap, counted from `started` (one hop attempt's own start)."""
        since = run.started if started is None else started
        if now - since < OPTION_MAX_S:
            return False
        run.end("timed_out", f"{now - since:.2f} s past the cap {OPTION_MAX_S:.2f} s")
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


# The reason an attempt was given up, and the ending when every attempt did it: the body never reached its
# take-off point at speed, so the flight measured nothing about the obstacle.
RAN_PAST_LOW_SPEED = "ran past the take-off point at too low a speed"

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
