"""Generic runtime detectors: engine-level facts every game shows, raised as suspicions with evidence.

The readers' watch states facts about the values a manifest selected. These detectors state facts about
the run itself, from the generic fields the observation already carries and nothing else:

| basis | measure | bound |
|---|---|---|
| `non_finite_pose` | a component of `public.controlled.position` or `.velocity` that is NaN or infinite | a finite number, against the finite range this run's readings of that component showed |
| `fell_out` | `public.controlled.position` y below the lowest floor the body was measured standing on | the floor minus `FALL_PEAK_FACTOR` times the calibration's measured rise peak |
| `hitch` | the wall interval between two observations | `HITCH_K` times the median of the last `HITCH_WINDOW` intervals, with the game clock advancing less than a `HITCH_K`th of the game time the interval corresponds to at the observation's declared time scale |
| `runtime_exception` | a new entry in the runtime error monitor (`public.runtime_errors`) | the monitor's own `first_sequence`: an entry stated once |
| `reader_over_max` | a declared reader's value above a maximum declared for it | that declared maximum |

Every statement carries the measured value and the bound it crossed, and a bound is derived from something
this run measured: the finite readings of the same component, the floor the body stood on, the intervals
before this one, the monitor's sequence, the declared maximum. A detector with no measurement for its bound
states nothing at all; nothing here estimates, and nothing here asks for help. A suspicion is a fact S1 and
the frontier read, exactly as `playtest.core.suspicion` states them (S1 ownership rules 3 and 4).

Nothing here knows a game's names: the fields are the bridge's generic ones, and a reader is read only
against a maximum the run's own detector configuration declares (`--detector-max`). No manifest reader
declares a maximum today (`ManifestReader` carries a kind, a type and a descriptor, no range), so the
manifest needs a field before that detector has anything to compare with; it stays silent without one.

**Off by default.** `--detectors` (`UPA_RUN_DETECTORS=1`) builds these; without it the runner builds the
readers' watch alone, so a trace is byte for byte what it was before.

**Raised once per episode.** Each detector raises when a state begins and is silent while it lasts: a fall
that stays out of its bound is one fact, and it is raised again only after the body came back inside and
crossed again. An unmeasured observation (no pose, no floor, no peak, no monitor entry) neither raises nor
ends an episode. A restart resets every baseline, because the measurements belonged to the previous
generation.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import Suspicion, SuspicionBasis
from playtest.contracts.loop import Choice
from playtest.core.ports import EffectReport, Noticed, ReaderSample
from playtest.core.suspicion import ReaderWatch, fmt

# Reporting constants, fixed ahead of any run. They decide when a measurement is worth stating, and they
# only gate a fact: nothing here changes what S1 may choose, opens help or ranks anything.
HITCH_K = 5  # a wall interval this many times the median of the earlier ones is a spike
HITCH_WINDOW = 40  # how many earlier intervals that median is taken over
HITCH_MIN_INTERVALS = 8  # fewer earlier intervals than this is not yet a pace to call a spike against
FALL_PEAK_FACTOR = 2.0  # the fall bound, in multiples of the measured rise peak
MESSAGE_HEAD_CHARS = 160  # how much of an error message a statement quotes

# What the bridge's runtime error monitor records: an error, an exception or a failed assertion.
ERROR_TYPES = frozenset({"Exception", "Error", "Assert"})
_ERROR_WORDS = re.compile(r"exception|error", re.IGNORECASE)

_POSITION = "public.controlled.position"
_VELOCITY = "public.controlled.velocity"


# --- what a detector may read -------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """A measured number, or `None`: a bool, a string and a missing value are not measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _vector(value: Any) -> tuple[float, ...] | None:
    """One published vector exactly as measured, non-finite components included; `None` when it is not a
    vector of numbers (a missing or malformed one is unmeasured, never zero)."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    components: list[float] = []
    for item in value:
        number = _number(item)
        if number is None:
            return None
        components.append(number)
    return tuple(components)


@dataclass(frozen=True)
class View:
    """One observation as the detectors may read it: the bridge's generic fields, `None` where unmeasured.

    `public` is the observation's own public mapping, so a detector reads a reader value only under a name
    its own configuration declared. `monitor` is the runtime error monitor when the bridge published one.
    """

    turn: int
    trace_seq: int | None
    seq: int
    frame: int
    game_s: float | None
    wall_utc: datetime
    time_scale: float
    position: tuple[float, ...] | None
    velocity: tuple[float, ...] | None
    surface_y: float | None  # the probe's down-ray hit, in world y
    settled: bool  # the body was at rest: every velocity component zero
    public: Mapping[str, Any]
    monitor: Mapping[str, Any] | None

    @property
    def standing(self) -> bool:
        """Whether this observation measured the body at rest on a surface: the only state that measures a floor."""
        return self.settled and self.surface_y is not None


def view_of(observation: Observation, *, turn: int, trace_seq: int | None) -> View:
    """Read one observation into the generic view the detectors share."""
    public: Mapping[str, Any] = observation.public if isinstance(observation.public, Mapping) else {}
    controlled = public.get("controlled")
    controlled = controlled if isinstance(controlled, Mapping) else {}
    velocity = _vector(controlled.get("velocity"))
    surface_y: float | None = None
    probe = public.get("probe")
    if isinstance(probe, Mapping):
        under = probe.get("under")
        if isinstance(under, Mapping) and under.get("hit") is True:
            point = _vector(under.get("point"))
            if point is not None and len(point) >= 2 and math.isfinite(point[1]):
                surface_y = point[1]
    monitor = public.get("runtime_errors")
    return View(
        turn=turn, trace_seq=trace_seq, seq=observation.seq, frame=observation.frame,
        game_s=observation.game_s, wall_utc=observation.wall_utc, time_scale=observation.time_scale,
        position=_vector(controlled.get("position")), velocity=velocity, surface_y=surface_y,
        settled=velocity is not None and all(component == 0.0 for component in velocity),
        public=public, monitor=monitor if isinstance(monitor, Mapping) else None,
    )


@dataclass(frozen=True)
class Detection:
    """What one detector measured about one observation, before the watch numbers it.

    `evidence` are the observations it rests on, as trace sequence numbers; the watch turns them into the
    suspicion's `evidence_seq`, so the reproduction path can cite them.
    """

    basis: SuspicionBasis
    subject: str
    statement: str
    detail: dict[str, Any]
    evidence: tuple[int, ...] = ()


class Detector(Protocol):
    """One generic measure. `config` is what the run plan records about it; `observe` is the measurement."""

    name: str

    def config(self) -> dict[str, Any]:
        ...

    def observe(self, view: View) -> tuple[Detection, ...]:
        ...

    def rebind(self) -> None:
        """Drop every baseline: the scene, the generation or the session it measured is gone."""
        ...


# --- the detectors ------------------------------------------------------------------------------


class NonFinitePose:
    """A pose or velocity component that is not a finite number.

    NaN and infinity are never a place to stand, and every step after the observation rests on the pose:
    the probe's rays, an option's termination, a replay's reproduction. The bound is a finite number on
    every component; the statement names the component and the range this run's finite readings of that
    same component showed, so it says what is unusual without a threshold of its own.
    """

    name = "non_finite_pose"

    def __init__(self) -> None:
        # (field, component) -> [low, high, readings] over the finite readings this run saw
        self._finite: dict[tuple[str, int], list[float]] = {}
        self._open: dict[str, bool] = {}

    def config(self) -> dict[str, Any]:
        return {"fields": [_POSITION, _VELOCITY], "bound": "every component of the pose and velocity is a finite number"}

    def rebind(self) -> None:
        self._finite.clear()
        self._open.clear()

    def observe(self, view: View) -> tuple[Detection, ...]:
        found: list[Detection] = []
        for field, values in ((_POSITION, view.position), (_VELOCITY, view.velocity)):
            if values is None:  # unmeasured: it neither raises nor ends an episode
                continue
            bad = [(index, value) for index, value in enumerate(values) if not math.isfinite(value)]
            for index, value in enumerate(values):
                if not math.isfinite(value):
                    continue
                known = self._finite.setdefault((field, index), [value, value, 0.0])
                known[0], known[1], known[2] = min(known[0], value), max(known[1], value), known[2] + 1
            if bad and not self._open.get(field, False):
                found.append(self._detection(field, values, bad, view))
            self._open[field] = bool(bad)
        return tuple(found)

    def _detection(self, field: str, values: Sequence[float], bad: list[tuple[int, float]],
                   view: View) -> Detection:
        parts: list[str] = []
        measured: list[dict[str, Any]] = []
        for index, value in bad:
            known = self._finite.get((field, index))
            if known is None:
                earlier = "no finite reading of it so far"
            else:
                noun = "reading" if known[2] == 1 else "readings"
                earlier = f"its finite readings so far were {fmt(known[0])} to {fmt(known[1])} ({fmt(known[2])} {noun})"
            parts.append(f"component {index} is {fmt(value)}, which is not a finite number; {earlier}")
            measured.append({"component": index, "value": value,
                             "finite_min": None if known is None else known[0],
                             "finite_max": None if known is None else known[1],
                             "readings": None if known is None else int(known[2])})
        statement = f"{field} has a component that is not a finite number: " + "; ".join(parts) + "."
        detail = {"field": field, "value": list(values),
                  "components": [index for index, _ in bad], "measured": measured}
        return Detection(SuspicionBasis.non_finite_pose, field, statement, detail,
                         tuple(seq for seq in (view.trace_seq,) if seq is not None))


class FellOut:
    """The body below the lowest floor it was measured standing on, by more than twice the measured rise peak.

    A floor is measured, never assumed: an observation measures one when the body's velocity was zero on
    every axis (it had come to rest) and the probe's down-ray hit a surface. The lowest body y of those
    observations is the floor, and it is compared with the body's y, so both numbers are the same
    measurement of the same entity. A pit the body comes to rest in is a floor too, which is why a game
    floor is not a fall.

    The bound is `FALL_PEAK_FACTOR` times the rise peak the calibration measured, taken through the
    options primitives: a fall this run could not have climbed back out of with its own jump. With no
    measured rise peak there is no bound, and this detector states nothing.
    """

    name = "fell_out"

    def __init__(self, rise_peak: float | None, *, peak_source: str,
                 factor: float = FALL_PEAK_FACTOR) -> None:
        peak = _number(rise_peak)
        if peak is not None and (not math.isfinite(peak) or peak <= 0):
            raise ValueError("rise_peak must be a positive finite number, or None when no rise was measured")
        factor_number = _number(factor)
        if factor_number is None or not math.isfinite(factor_number) or factor_number <= 0:
            raise ValueError("factor must be a positive finite number")
        self._peak = peak
        self._peak_source = peak_source
        self._factor = factor_number
        self._floor: tuple[float, int, int | None] | None = None  # y, turn, trace_seq
        self._open = False

    def config(self) -> dict[str, Any]:
        return {"rise_peak": "unknown" if self._peak is None else self._peak,
                "peak_source": self._peak_source,
                "factor": self._factor,
                "floor": "the body y of an observation whose velocity was zero on every axis and whose probe "
                         "down-ray hit a surface",
                "silent": None if self._peak is not None else "no measured rise peak, so the bound is unknown"}

    def rebind(self) -> None:
        self._floor = None
        self._open = False

    def observe(self, view: View) -> tuple[Detection, ...]:
        if view.position is None or len(view.position) < 2:
            return ()
        y = view.position[1]
        if not math.isfinite(y):
            return ()
        if view.standing and (self._floor is None or y < self._floor[0]):
            self._floor = (y, view.turn, view.trace_seq)
        if self._peak is None or self._floor is None:
            return ()  # no bound: nothing measured a peak, or no floor yet
        bound = self._floor[0] - self._factor * self._peak
        outside = y < bound
        if outside and not self._open:
            self._open = True
            return (self._detection(y, bound, view),)
        self._open = outside
        return ()

    def _detection(self, y: float, bound: float, view: View) -> Detection:
        assert self._floor is not None and self._peak is not None
        floor, turn, seq = self._floor
        depth = floor - y
        statement = (
            f"{_POSITION} y is {fmt(y)}, below the lowest floor measured for it so far "
            f"({fmt(floor)}, turn {turn}"
            + ("" if seq is None else f", trace_seq {seq}")
            + f"), by {fmt(depth)}; the bound is {fmt(self._factor)} times the measured rise peak "
              f"{fmt(self._peak)} below that floor: {fmt(bound)}."
        )
        detail = {"field": _POSITION, "y": y, "floor_y": floor, "floor_turn": turn, "floor_seq": seq,
                  "depth": depth, "rise_peak": self._peak, "peak_source": self._peak_source,
                  "factor": self._factor, "bound_y": bound, "surface_y": view.surface_y}
        return Detection(SuspicionBasis.fell_out, _POSITION, statement, detail,
                         tuple(item for item in (seq, view.trace_seq) if item is not None))


class Hitch:
    """An observation interval far longer than this run's recent intervals, where the game clock lost the time.

    The wall interval between two observations is compared with the median of the `HITCH_WINDOW` intervals
    before it, and only once `HITCH_MIN_INTERVALS` of them exist. A long turn in which the game clock
    advanced as much as the wall clock corresponds to is a slow turn, not a hitch: the simulation kept pace.
    So the game clock has to lose the interval: it advanced less than a `HITCH_K`th of the game time the wall
    interval corresponds to at the observation's own declared time scale, a stall included. A clock that went
    backwards is a reset (a load), not an interval this measurement can read.
    """

    name = "hitch"

    def __init__(self, *, k: int = HITCH_K, window: int = HITCH_WINDOW,
                 min_intervals: int = HITCH_MIN_INTERVALS) -> None:
        for label, value in (("k", k), ("window", window), ("min_intervals", min_intervals)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if min_intervals > window:
            raise ValueError("min_intervals cannot exceed the window")
        self._k, self._window, self._min = k, window, min_intervals
        self._intervals: deque[tuple[float, float | None]] = deque()
        self._previous: View | None = None
        self._open = False

    def config(self) -> dict[str, Any]:
        return {"k": self._k, "window": self._window, "min_intervals": self._min,
                "measure": "the wall interval between two observations",
                "game_clock": "it must also advance less than a kth of the game time the wall interval "
                              "corresponds to at the observation's declared time scale"}

    def rebind(self) -> None:
        self._intervals.clear()
        self._previous = None
        self._open = False

    def observe(self, view: View) -> tuple[Detection, ...]:
        previous, self._previous = self._previous, view
        if previous is None:
            return ()
        wall = (view.wall_utc - previous.wall_utc).total_seconds()
        if not math.isfinite(wall) or wall <= 0:
            return ()  # a wall clock that stood still or went backwards is not this measurement
        game: float | None = None
        if view.game_s is not None and previous.game_s is not None:
            delta = view.game_s - previous.game_s
            # A clock that went backwards reset (a load): the interval measures no pace, so it is unmeasured.
            game = delta if math.isfinite(delta) and delta >= 0 else None
        due = view.time_scale * wall
        due = due if math.isfinite(due) and due > 0 else None
        earlier = list(self._intervals)
        wall_median = self._median([item[0] for item in earlier])
        outside = False
        found: tuple[Detection, ...] = ()
        if wall_median is not None and wall > self._k * wall_median and game is not None and due is not None:
            least = due / self._k
            if game < least:
                outside = True
                if not self._open:
                    found = (self._detection(previous, view, wall, game, wall_median, len(earlier), due, least),)
        self._open = outside
        self._intervals.append((wall, game))
        while len(self._intervals) > self._window:
            self._intervals.popleft()
        return found

    def _median(self, values: Sequence[float]) -> float | None:
        return statistics.median(values) if len(values) >= self._min else None

    def _detection(self, previous: View, view: View, wall: float, game: float, wall_median: float,
                   intervals: int, due: float, least: float) -> Detection:
        bound = self._k * wall_median
        stalled = game == 0.0
        pace = (f"it advanced {fmt(game)} s, below the {fmt(least)} s it had to advance to keep pace with the "
                f"wall clock at time scale {fmt(view.time_scale)} ({fmt(due)} s of game time was due)")
        if stalled:
            pace = "the game clock stalled in that interval: " + pace
        statement = (
            f"The wall clock advanced {fmt(wall)} s between observation seq {previous.seq} (frame {previous.frame}, "
            f"game {fmt(previous.game_s)} s) and seq {view.seq} (frame {view.frame}, game {fmt(view.game_s)} s), "
            f"more than {fmt(self._k)} times the median {fmt(wall_median)} s of the last {intervals} intervals "
            f"({fmt(bound)} s); {pace}."
        )
        detail = {"field": "observation interval", "wall_s": wall, "game_s": game,
                  "median_wall_s": wall_median, "intervals": intervals, "time_scale": view.time_scale,
                  "game_due_s": due, "min_game_s": least,
                  "k": self._k, "window": self._window, "min_intervals": self._min, "bound_s": bound,
                  "stalled": stalled,
                  "from_seq": previous.seq, "to_seq": view.seq,
                  "from_frame": previous.frame, "to_frame": view.frame,
                  "from_game_s": previous.game_s, "to_game_s": view.game_s}
        return Detection(SuspicionBasis.hitch, "observation interval", statement, detail,
                         tuple(item for item in (previous.trace_seq, view.trace_seq) if item is not None))


class RuntimeException:
    """A new entry in the runtime error monitor, quoted briefly, when its text names an error or an exception.

    The monitor is the bridge's own bounded capture of the engine's error log, published in
    `public.runtime_errors`: it counts every error and keeps a bounded sample of them. An entry is new when
    its `first_sequence` is past the highest one this run already stated, which is the monitor's own
    episode boundary: the same message recorded again later is a new entry with a new sequence and is
    stated again, a repeat folded into the same entry is not. With no monitor in the observation nothing
    is measured and nothing is stated.
    """

    name = "runtime_exception"

    def __init__(self, *, head_chars: int = MESSAGE_HEAD_CHARS) -> None:
        if isinstance(head_chars, bool) or not isinstance(head_chars, int) or head_chars < 1:
            raise ValueError("head_chars must be a positive integer")
        self._head = head_chars
        self._through: float | None = None  # the highest monitor sequence this run has stated past
        self._unsequenced: dict[str, bool] = {}  # entries without a sequence, by text

    def config(self) -> dict[str, Any]:
        return {"source": "public.runtime_errors", "head_chars": self._head,
                "types": sorted(ERROR_TYPES),
                "measure": "an entry whose first_sequence is past the highest one already stated"}

    def rebind(self) -> None:
        self._through = None
        self._unsequenced.clear()

    def observe(self, view: View) -> tuple[Detection, ...]:
        if view.monitor is None:
            return ()
        events = view.monitor.get("events")
        if not isinstance(events, (list, tuple)):
            return ()
        found: list[Detection] = []
        highest = self._through
        for event in events:
            if not isinstance(event, Mapping):
                continue
            sequence = _number(event.get("first_sequence"))
            if sequence is not None and not math.isfinite(sequence):
                sequence = None
            if sequence is not None and highest is not None and sequence <= highest:
                continue
            if sequence is not None:
                highest = sequence if highest is None else max(highest, sequence)
            text = event.get("message")
            text = text if isinstance(text, str) else ""
            kind = event.get("type")
            names = (isinstance(kind, str) and kind in ERROR_TYPES) or bool(_ERROR_WORDS.search(text))
            if not names:
                continue
            key = text if sequence is None else None
            if key is not None:
                if self._unsequenced.get(key):
                    continue
                self._unsequenced[key] = True
            found.append(self._detection(event, text, kind, sequence, view))
        if highest is not None:
            self._through = highest
        return tuple(found)

    def _detection(self, event: Mapping[str, Any], text: str, kind: Any, sequence: float | None,
                   view: View) -> Detection:
        head = text if len(text) <= self._head else text[:self._head - 1] + "…"
        count = _number(event.get("count"))
        observed = _number(view.monitor.get("observed")) if view.monitor is not None else None
        context: list[str] = []
        if sequence is not None:
            context.append(f"first recorded at monitor sequence {fmt(sequence)}")
        if count is not None:
            context.append(f"{fmt(count)} occurrence" + ("" if count == 1 else "s"))
        if observed is not None:
            context.append(f"the monitor has counted {fmt(observed)} error message" + ("" if observed == 1 else "s")
                           + " this run")
        # The quoted head is what the message named; the parenthetical is what the monitor measured about it.
        aside = f" ({'; '.join(context)})" if context else ""
        label = kind if isinstance(kind, str) and kind else "entry"
        statement = f"A new runtime error entry was recorded during play: type {label}, message {repr_head(head)}{aside}."
        detail = {"type": kind, "message_head": head, "message": text,
                  "first_sequence": sequence, "last_sequence": _number(event.get("last_sequence")),
                  "count": count, "observed": observed,
                  "truncated": view.monitor.get("truncated") if view.monitor is not None else None,
                  "event": dict(event)}
        return Detection(SuspicionBasis.runtime_exception, f"public.runtime_errors[{label}]", statement, detail,
                         tuple(item for item in (view.trace_seq,) if item is not None))


class ReaderOverMax:
    """A declared reader's value above the maximum declared for it.

    No manifest reader declares a maximum today: `ManifestReader` carries a kind, a declared type and a
    descriptor, and no range or limit (`ManifestInvariant` has one, a reader does not). So the maximum is an
    operator declaration held in this run's detector configuration, and the detector states the reading
    against that declared maximum in a configuration the run plan records. A reader with no declared
    maximum is never compared, and the detector needs the manifest's reader field before it can be a
    measurement of the manifest at all.
    """

    name = "reader_over_max"

    def __init__(self, maxima: Mapping[str, float], *, source: str) -> None:
        declared: dict[str, float] = {}
        for alias, value in maxima.items():
            number = _number(value)
            if number is None or not math.isfinite(number):
                raise ValueError(f"the declared maximum for {alias!r} must be a finite number")
            declared[alias] = number
        self._maxima = declared
        self._source = source
        self._open: dict[str, bool] = {}

    def config(self) -> dict[str, Any]:
        return {"maxima": dict(self._maxima), "source": self._source,
                "measure": "a declared reader's reading above its declared maximum",
                "silent": None if self._maxima else "no reader maximum is declared, and no manifest reader "
                                                    "declares one: the manifest needs a declared maximum"}

    def rebind(self) -> None:
        self._open.clear()

    def observe(self, view: View) -> tuple[Detection, ...]:
        found: list[Detection] = []
        for alias, maximum in self._maxima.items():
            value = _number(view.public.get(alias))
            if value is None or not math.isfinite(value):
                continue  # unmeasured: it neither raises nor ends an episode
            outside = value > maximum
            if outside and not self._open.get(alias, False):
                found.append(self._detection(alias, value, maximum, view))
            self._open[alias] = outside
        return tuple(found)

    def _detection(self, alias: str, value: float, maximum: float, view: View) -> Detection:
        statement = (f"public.{alias} is {fmt(value)}, above the declared maximum {fmt(maximum)} ({self._source}).")
        detail = {"field": f"public.{alias}", "value": value, "declared_max": maximum, "source": self._source}
        return Detection(SuspicionBasis.reader_over_max, f"public.{alias}", statement, detail,
                         tuple(item for item in (view.trace_seq,) if item is not None))


def repr_head(text: str) -> str:
    """A message as a statement quotes it: in quotes, on one line."""
    return '"' + " ".join(text.split()) + '"'


def build_detectors(*, rise_peak: float | None, peak_source: str, maxima: Mapping[str, float] | None = None,
                    maxima_source: str = "an operator declaration") -> list[Detector]:
    """The five detectors, in the order their facts are stated.

    The fall detector is included even when no rise was measured: its configuration then records that its
    bound is unknown and it states nothing, which is a fact the plan can be read for.
    """
    return [NonFinitePose(), FellOut(rise_peak, peak_source=peak_source), Hitch(), RuntimeException(),
            ReaderOverMax(maxima or {}, source=maxima_source)]


def detectors_config(detectors: Sequence[Detector]) -> dict[str, Any]:
    """What the run plan records: every detector on, and the bounds or constants each measures against."""
    return {"enabled": True, "names": [detector.name for detector in detectors],
            **{detector.name: detector.config() for detector in detectors}}


class DetectorWatch:
    """The readers' watch with the generic detectors beside it.

    It is one `Watch` to the loop: the readers' lines and suspicions come out exactly as they did, and the
    detectors' suspicions follow them in the same observation, numbered after the ones the readers' watch
    raised so a run's suspicion ids stay unique and in the order raised. Detector suspicions carry no line
    of their own: the loop states every new suspicion it is given.
    """

    def __init__(self, inner: ReaderWatch, detectors: Sequence[Detector]) -> None:
        self._inner = inner
        self._detectors = list(detectors)

    @property
    def detectors(self) -> tuple[Detector, ...]:
        return tuple(self._detectors)

    def config(self) -> dict[str, Any]:
        return detectors_config(self._detectors)

    def observe(self, observation: Observation, sample: ReaderSample | None, *, turn: int,
                trace_seq: int | None) -> Noticed:
        noticed = self._inner.observe(observation, sample, turn=turn, trace_seq=trace_seq)
        view = view_of(observation, turn=turn, trace_seq=trace_seq)
        found = [detection for detector in self._detectors for detection in detector.observe(view)]
        if not found:
            return noticed
        ids = self._inner.reserve(len(found))
        return Noticed(lines=noticed.lines,
                       suspicions=(*noticed.suspicions,
                                   *(self._suspicion(suspicion_id, detection, view)
                                     for suspicion_id, detection in zip(ids, found, strict=True))))

    def effect(self, choice: Choice, report: EffectReport, *, turn: int,
               trace_seqs: Sequence[int]) -> Sequence[Suspicion]:
        return self._inner.effect(choice, report, turn=turn, trace_seqs=trace_seqs)

    def rebind(self, *, client: Any = None, observation: Observation | None = None,
               generation: str | None = None) -> None:
        """A restart drops the readers' baselines and every detector's: they measured the previous generation."""
        self._inner.rebind(client=client, observation=observation, generation=generation)
        for detector in self._detectors:
            detector.rebind()

    @staticmethod
    def _suspicion(suspicion_id: str, detection: Detection, view: View) -> Suspicion:
        return Suspicion(
            suspicion_id=suspicion_id, basis=detection.basis, turn=view.turn, based_on_seq=view.seq,
            game_s=view.game_s, subject=detection.subject, statement=detection.statement,
            detail=detection.detail,
            evidence_seq=list(dict.fromkeys(seq for seq in detection.evidence if seq is not None)),
        )
