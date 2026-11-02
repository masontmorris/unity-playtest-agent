"""Measured primitives for the option layer (docs/design/options-layer.md, section 3).

A primitive is one offered action's measured effect, reduced to a displacement vector **per second of
hold**. Every number here comes from the calibration record's valid trials; nothing is inferred from a
name, a declaration or a genre. No game, object, control or genre word appears in this module.

**Up is Unity's world +y.** It is the engine convention for default gravity, so it is used as-is and not
renamed. Ground movement is Unity's world x/z plane; v1 is correct for world-relative movement, which is
what a side view and a top-down view measure.

Unknown stays ``None``, never zero: a control with no measurable trial yields no primitive, and a
direction the calibration cannot support is stated in ``Primitives.unsupported`` rather than guessed.

Constants below name their measured source.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from .model import OptionSpec

# --- axes and classification -------------------------------------------------------------

UP = 1                      # index of Unity world +y in a position vector: the engine's up
GROUND = (0, 2)             # indices of Unity world x and z: the plane a move primitive travels in
AXES = ("x", "y", "z")

KIND_MOVE = "move"          # a primitive whose measured vector is mostly horizontal
KIND_RISE = "rise"          # a primitive whose measured vector is mostly +y
KIND_OTHER = "other"        # a primitive that fits neither; it stays a plain action

# A classification threshold is a share of the primitive's own measured vector, so it carries no units
# and no game knowledge. 0.75 means "three quarters of the measured motion is along this axis group".
RISE_SHARE = 0.75
MOVE_SHARE = 0.75

# A trial needs a measured apply-to-release interval to give a rate at all. Below this the interval is
# a clock tick, not a hold, and dividing by it would invent a speed.
MIN_APPLIED_S = 0.01

# Two trials of one control whose measured directions differ by more than this disagree about the
# direction itself, not just its length. That happens when the vector rotates with a measured entity
# basis; such a direction is stated as unsupported rather than labelled world-relative.
BASIS_ROTATION_DEG = 15.0

# The wait durations offered as options. They are the option layer's own small fixed set, chosen so one
# wait is shorter than, comparable to, and longer than the calibration's longest requested hold.
WAIT_SECONDS = (0.25, 0.5, 1.0)


# --- small measured-vector helpers -------------------------------------------------------


def _vec(value: Any) -> tuple[float, float, float] | None:
    """A three-component finite vector, or None. A malformed reading is unknown, never zero."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    out: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        out.append(number)
    return (out[0], out[1], out[2])


def _sub(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a: Sequence[float]) -> float:
    return math.sqrt(_dot(a, a))


def _scale(a: Sequence[float], factor: float) -> tuple[float, float, float]:
    return (a[0] * factor, a[1] * factor, a[2] * factor)


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _median_vec(vectors: Sequence[Sequence[float]]) -> tuple[float, float, float] | None:
    if not vectors:
        return None
    return (
        _median([v[0] for v in vectors]) or 0.0,
        _median([v[1] for v in vectors]) or 0.0,
        _median([v[2] for v in vectors]) or 0.0,
    )


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _label(unit: Sequence[float]) -> str:
    """A direction label from a unit vector: the dominant ground axis, with its sign."""
    axis = max(GROUND, key=lambda index: abs(unit[index]))
    sign = "+" if unit[axis] >= 0 else "-"
    return f"{sign}{AXES[axis]}"


# --- primitive records --------------------------------------------------------------------


@dataclass(frozen=True)
class Primitive:
    """One offered action's measured effect, per second of hold."""

    action: str
    control: str | None
    kind: str
    vector: tuple[float, float, float]
    spread: float | None
    trials: int
    step: tuple[float, float, float] | None = None   # measured displacement of its shortest hold
    step_s: float | None = None                      # the measured applied duration of that hold

    @property
    def magnitude(self) -> float:
        return _norm(self.vector)


@dataclass(frozen=True)
class Direction:
    """One measured horizontal direction and the offered action that produces it."""

    label: str                       # "+x", "-z", ...
    action: str                      # the offered action name, verbatim
    control: str | None
    unit: tuple[float, float, float]
    speed: float                     # measured units per second of hold along `unit`
    spread: float | None
    step: float | None               # measured displacement of one short hold, along `unit`
    trials: int
    alternatives: tuple[tuple[str, str | None], ...] = ()   # other (action, control) pairs, same direction
    # "world": `unit` is a world vector. "body": `unit` is (right, 0, forward) in the entity's own measured
    # basis, because the measured motion turns with the entity; an option resolves it at its start.
    basis: str = "world"


@dataclass(frozen=True)
class Primitives:
    """Everything the option layer may offer, measured once per run from the calibration record."""

    directions: tuple[Direction, ...] = ()
    # Moves whose measured direction turns with the entity, measured in its own basis (`Direction.basis`
    # "body"). Kept apart from `directions`, which every world-frame lookup reads, so a world game sees none.
    body_directions: tuple[Direction, ...] = ()
    rise: Primitive | None = None
    rise_duration_s: float | None = None    # measured airtime: hold time whose rise returns to zero
    rise_peak: float | None = None          # measured peak rise: the largest +y displacement sampled
    # The rise pressed again at its measured apex (`options.air_rise`): set only when that measurably rose
    # higher than a rise from rest. Its peak, when after take-off it was pressed, and its measured airtime.
    air_rise_peak: float | None = None
    air_press_s: float | None = None
    air_flight_s: float | None = None
    # The same probe's flight from rest: airtime and peak of an actual rise, the basis both hop labels share.
    rest_rise_peak: float | None = None
    rest_flight_s: float | None = None
    classified: tuple[Primitive, ...] = ()
    chords: tuple[tuple[str, str, str], ...] = ()   # (move action, rise action, chord action)
    notes: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    basis: str = "world"

    # --- lookups -------------------------------------------------------------------------

    def direction(self, label: str) -> Direction | None:
        for item in self.directions:
            if item.label == label:
                return item
        return None

    def aligned(self, unit: Sequence[float]) -> Direction | None:
        """The measured direction best aligned with `unit`, if any points the same way."""
        best: Direction | None = None
        best_dot = 0.0
        for item in self.directions:
            value = _dot(item.unit, unit)
            if value > best_dot:
                best, best_dot = item, value
        return best

    def opposite(self, unit: Sequence[float]) -> Direction | None:
        return self.aligned(_scale(unit, -1.0))

    def chord_for(self, move_action: str) -> str | None:
        """The declared action that applies this move input and the rise input together, if offered."""
        if self.rise is None:
            return None
        for move, rise, chord in self.chords:
            if move == move_action and rise == self.rise.action:
                return chord
        return None

    def chorded_move(self, direction: Direction) -> tuple[str, str] | None:
        """(move action, chord action) for a hop along `direction`, if any offered move has a chord.

        The fastest action measuring the direction is preferred, but a slower one that the catalog
        declares together with the rise input is usable and is preferred over no hop at all.
        """
        for action, _control in ((direction.action, direction.control), *direction.alternatives):
            chord = self.chord_for(action)
            if chord is not None:
                return (action, chord)
        return None

    # --- derived parameters --------------------------------------------------------------

    @property
    def short_step(self) -> float | None:
        """The shortest measured move step: the smallest distance a measured move is known to cover."""
        steps = [item.step for item in self.directions if item.step is not None]
        return min(steps) if steps else None

    def run_up(self, direction: Direction | None = None) -> float | None:
        """Run-up distance: measured speed times measured rise time (spec section 4).

        With no direction given it uses the fastest measured direction, so a summary has one number.
        """
        if self.rise_duration_s is None:
            return None
        if direction is None:
            speeds = [item.speed for item in self.directions]
            if not speeds:
                return None
            return max(speeds) * self.rise_duration_s
        return direction.speed * self.rise_duration_s

    def launch_distance(self, direction: Direction | None = None) -> float | None:
        """How far short of the hop's start to take off: measured speed times the time to apex.

        A hop is chosen facing what it should clear, so the obstacle is at the start. Taking off this far short
        of it puts the body at full height as it arrives. The time to apex is half the measured airtime, which
        assumes a symmetric flight: stated, not measured separately.
        """
        if self.rise_duration_s is None:
            return None
        if direction is None:
            speeds = [item.speed for item in self.directions]
            if not speeds:
                return None
            return max(speeds) * self.rise_duration_s / 2.0
        return direction.speed * self.rise_duration_s / 2.0

    @property
    def can_hop(self) -> bool:
        """A hop needs a measured rise time, a measured move direction and a declared chord."""
        if self.rise is None or self.rise_duration_s is None or self.rise_peak is None:
            return False
        moves = (*self.directions, *self.body_directions)
        if not moves:
            return False
        return any(self.chorded_move(item) is not None for item in moves)

    # --- chords --------------------------------------------------------------------------

    def with_catalog(self, catalog: Any) -> "Primitives":
        """Return these primitives knowing which declared actions apply two inputs at once.

        `catalog` is the manifest's exported action list (or a name -> declaration mapping): the only
        place that says a single declared action presses the move control and the rise control
        together. Nothing in the calibration record does.
        """
        if not catalog:
            return self
        move_controls: dict[str, str] = {}
        for item in (*self.directions, *self.body_directions):
            if item.control is not None:
                move_controls.setdefault(item.control, item.action)
            for action, control in item.alternatives:
                if control is not None:
                    move_controls.setdefault(control, action)
        rise_control = self.rise.control if self.rise is not None else None
        rise_action = self.rise.action if self.rise is not None else None
        if rise_control is None or rise_action is None:
            return self
        found: list[tuple[str, str, str]] = []
        for name, controls in _catalog_inputs(catalog).items():
            if len(controls) != 2:
                continue
            if rise_control not in controls:
                continue
            for control in controls:
                if control != rise_control and control in move_controls:
                    found.append((move_controls[control], rise_action, name))
        if not found:
            return replace(
                self,
                notes=self.notes + (
                    "no declared action applies a measured move control and the rise control together; "
                    "hop is not offered",
                ),
            )
        known = {(move, rise, chord) for move, rise, chord in self.chords}
        added = tuple(item for item in found if item not in known)
        return replace(self, chords=self.chords + added)

    # --- reporting -----------------------------------------------------------------------

    def statement(self) -> str:
        """A printable, quantity-only description (no advice, no game words)."""
        lines = [f"basis {self.basis}"]
        for item in self.directions:
            extra = "".join(f", alt {name}" for name, _control in item.alternatives)
            lines.append(
                f"move {item.label}: action {item.action} "
                f"{item.speed:.3f} u/s spread {_text(item.spread)} "
                f"step {_text(item.step)} over {item.trials} trials{extra}"
            )
        if self.rise is not None:
            lines.append(
                f"rise: action {self.rise.action} {self.rise.magnitude:.3f} u/s "
                f"airtime {_text(self.rise_duration_s)} peak {_text(self.rise_peak)}"
            )
        else:
            lines.append("rise: none measured")
        for move, rise, chord in self.chords:
            lines.append(f"chord {chord}: {move} + {rise}")
        for item in self.unsupported:
            lines.append(f"unsupported: {item}")
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)


def _text(value: float | None, digits: int = 3) -> str:
    return "unknown" if value is None else f"{value:.{digits}f}"


# --- reading the calibration record -------------------------------------------------------


def _controls(calibration: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """control id -> its calibration entry, from either of the record's two listings."""
    out: dict[str, Mapping[str, Any]] = {}
    for key in ("action_summaries", "actions"):
        for entry in calibration.get(key) or ():
            if not isinstance(entry, Mapping):
                continue
            control = entry.get("control") or entry.get("control_name")
            if isinstance(control, str) and control not in out:
                out[control] = entry
    return out


def _catalog_inputs(catalog: Any) -> dict[str, tuple[str, ...]]:
    """declared action name -> its control ids, from a manifest action list or mapping."""
    out: dict[str, tuple[str, ...]] = {}
    if isinstance(catalog, Mapping):
        items: Iterable[tuple[Any, Any]] = catalog.items()
    else:
        items = (
            ((entry.get("name") if isinstance(entry, Mapping) else getattr(entry, "name", None)), entry)
            for entry in catalog or ()
        )
    for name, entry in items:
        if not isinstance(name, str):
            continue
        if isinstance(entry, Mapping):
            inputs = entry.get("inputs") or []
        else:
            inputs = getattr(entry, "inputs", None) or []
        controls: list[str] = []
        for item in inputs:
            control = None
            if isinstance(item, Mapping):
                control = item.get("control_id") or item.get("control")
            else:
                control = getattr(item, "control_id", None) or getattr(item, "control", None)
            if isinstance(control, str):
                controls.append(control)
        if controls:
            out[name] = tuple(controls)
    return out


def _resolve_actions(
    offered_actions: Sequence[Any],
    controls: Mapping[str, Mapping[str, Any]],
    catalog: Any,
    notes: list[str],
) -> tuple[dict[str, tuple[str, ...]], Any]:
    """offered action name -> the control ids it presses, as far as anything measured says.

    An offered action may be a name or the manifest declaration itself; a declaration carries its
    inputs, so passing the exported actions is enough to resolve and to find chords.
    """
    names: list[str] = []
    embedded: dict[str, Any] = {}
    for entry in offered_actions:
        if isinstance(entry, str):
            names.append(entry)
            continue
        name = entry.get("name") if isinstance(entry, Mapping) else getattr(entry, "name", None)
        if isinstance(name, str):
            names.append(name)
            embedded[name] = entry
    declared = _catalog_inputs(catalog) if catalog else {}
    resolved: dict[str, tuple[str, ...]] = {}
    for action in names:
        if action in declared:
            resolved[action] = declared[action]
            continue
        if action in controls:
            resolved[action] = (action,)
            continue
        named = [
            control for control, entry in controls.items()
            if entry.get("action") == action or entry.get("action_name") == action
        ]
        if len(named) == 1:
            resolved[action] = (named[0],)
            continue
        if action in embedded:
            resolved[action] = _catalog_inputs(embedded)[action]
            continue
        notes.append(f"{action}: no calibrated control is named by this offered action")
    combined = {**embedded, **(catalog if isinstance(catalog, Mapping) else {})}
    if catalog and not isinstance(catalog, Mapping):
        combined = {**embedded, **{item: entry for item, entry in _catalog_entries(catalog)}}
    return resolved, (combined or None)


def _catalog_entries(catalog: Any) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for entry in catalog or ():
        name = entry.get("name") if isinstance(entry, Mapping) else getattr(entry, "name", None)
        if isinstance(name, str):
            out.append((name, entry))
    return out


def _applied_s(trial: Mapping[str, Any]) -> float | None:
    """The measured apply-to-release interval of a trial, from its own execution records."""
    records = trial.get("execution") or ()
    for record in reversed(list(records)):
        if isinstance(record, Mapping):
            value = _finite(record.get("applied_duration_s"))
            if value is not None and value > 0:
                return value
    return None


def _trial_delta(trial: Mapping[str, Any]) -> tuple[float, float, float] | None:
    """The measured displacement of one trial: drift-corrected when the record carries it."""
    corrected = _vec(trial.get("effect_delta"))
    if corrected is not None:
        return corrected
    before = trial.get("before") or {}
    after = trial.get("after") or {}
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return None
    start, end = _vec(before.get("position")), _vec(after.get("position"))
    if start is None or end is None:
        return None
    return _sub(end, start)


@dataclass(frozen=True)
class _Sample:
    delta: tuple[float, float, float]
    applied_s: float
    hold_s: float
    yaw_deg: float | None
    right: tuple[float, float, float] | None = None
    forward: tuple[float, float, float] | None = None


def _samples(trials: Sequence[Mapping[str, Any]], control: str) -> list[_Sample]:
    out: list[_Sample] = []
    for trial in trials:
        if not isinstance(trial, Mapping) or trial.get("control") != control:
            continue
        if not trial.get("valid"):
            continue
        delta = _trial_delta(trial)
        if delta is None:
            continue
        applied = _applied_s(trial)
        if applied is None or applied < MIN_APPLIED_S:
            continue
        hold = _finite(trial.get("requested_hold_s"))
        before = trial.get("before") or {}
        yaw = _finite(before.get("yaw_deg")) if isinstance(before, Mapping) else None
        right = _vec(before.get("world_right")) if isinstance(before, Mapping) else None
        forward = _vec(before.get("world_forward")) if isinstance(before, Mapping) else None
        out.append(_Sample(delta=delta, applied_s=applied,
                           hold_s=hold if hold is not None else applied, yaw_deg=yaw,
                           right=right, forward=forward))
    return out


def _kind(vector: Sequence[float]) -> str:
    magnitude = _norm(vector)
    if magnitude <= 0:
        return KIND_OTHER
    vertical = vector[UP] / magnitude
    horizontal = math.hypot(vector[0], vector[2]) / magnitude
    if vertical >= RISE_SHARE:
        return KIND_RISE
    if horizontal >= MOVE_SHARE:
        return KIND_MOVE
    return KIND_OTHER


def _primitive(action: str, control: str, samples: Sequence[_Sample]) -> Primitive | None:
    rates = [_scale(sample.delta, 1.0 / sample.applied_s) for sample in samples]
    vector = _median_vec(rates)
    if vector is None:
        return None
    deviations = [_norm(_sub(rate, vector)) for rate in rates]
    spread = _median(deviations)
    shortest = min(samples, key=lambda sample: (sample.hold_s, sample.applied_s))
    same_hold = [sample.delta for sample in samples if sample.hold_s == shortest.hold_s]
    step = _median_vec(same_hold)
    return Primitive(action=action, control=control, kind=_kind(vector), vector=vector,
                     spread=spread, trials=len(samples), step=step, step_s=shortest.applied_s)


def _direction(primitive: Primitive, notes: list[str],
               unsupported: list[str]) -> Direction | None:
    horizontal = (primitive.vector[0], 0.0, primitive.vector[2])
    length = _norm(horizontal)
    if length <= 0:
        unsupported.append(f"{primitive.action}: measured motion is vertical only")
        return None
    unit = _scale(horizontal, 1.0 / length)
    speed = _dot(primitive.vector, unit)
    if speed <= 0:
        unsupported.append(f"{primitive.action}: measured motion has no positive component along its own axis")
        return None
    step = _dot(primitive.step, unit) if primitive.step is not None else None
    return Direction(label=_label(unit), action=primitive.action, control=primitive.control,
                     unit=unit, speed=speed, spread=primitive.spread, step=step,
                     trials=primitive.trials)


BODY_LABELS = {(2, 1): "forward", (2, -1): "back", (0, 1): "right", (0, -1): "left"}


def _body_direction(primitive: Primitive, samples: Sequence[_Sample]) -> Direction | None:
    """The move measured in the entity's own basis: each trial's motion on its recorded right and forward.

    The unit is (right, 0, forward); its label names the dominant body axis. `None` when no trial recorded the
    basis or the motion in it has no horizontal part.
    """
    rates, shortest = [], None
    for sample in samples:
        if sample.right is None or sample.forward is None or sample.applied_s <= 0:
            continue
        local = (_dot(sample.delta, sample.right), 0.0, _dot(sample.delta, sample.forward))
        rates.append(_scale(local, 1.0 / sample.applied_s))
        if shortest is None or (sample.hold_s, sample.applied_s) < (shortest[0].hold_s, shortest[0].applied_s):
            shortest = (sample, local)
    vector = _median_vec(rates)
    if vector is None:
        return None
    length = _norm(vector)
    if length <= 0:
        return None
    unit = _scale(vector, 1.0 / length)
    index = 2 if abs(unit[2]) >= abs(unit[0]) else 0
    label = BODY_LABELS[(index, 1 if unit[index] > 0 else -1)]
    step = _dot(shortest[1], unit) if shortest is not None else None
    spread = _median([_norm(_sub(rate, vector)) for rate in rates])
    return Direction(label=label, action=primitive.action, control=primitive.control, unit=unit,
                     speed=length, spread=spread, step=step, trials=len(rates), basis="body")


def _pair_spread(vectors: Sequence[Sequence[float]]) -> float:
    """The largest angle between any two of these horizontal directions, in degrees."""
    directions = []
    for vector in vectors:
        horizontal = (vector[0], 0.0, vector[2])
        length = _norm(horizontal)
        if length > 0:
            directions.append(_scale(horizontal, 1.0 / length))
    worst = 0.0
    for index, first in enumerate(directions):
        for second in directions[index + 1:]:
            cosine = max(-1.0, min(1.0, _dot(first, second)))
            worst = max(worst, math.degrees(math.acos(cosine)))
    return worst


def _basis_relative(samples: Sequence[_Sample]) -> tuple[bool, float]:
    """Whether the measured directions rotate with the entity basis the trials recorded.

    A world direction that turns with the entity's yaw is basis-relative, and calling it world-relative
    would silently mislabel it. This compares the spread of the measured directions in world space with
    their spread in each trial's own recorded basis: agreeing in the basis while disagreeing in the world
    is exactly that rotation.
    """
    yaws = {round(sample.yaw_deg, 3) for sample in samples if sample.yaw_deg is not None}
    if len(yaws) < 2:
        return (False, 0.0)
    world = [sample.delta for sample in samples]
    in_basis = [
        (_dot(sample.delta, sample.right), 0.0, _dot(sample.delta, sample.forward))
        for sample in samples
        if sample.right is not None and sample.forward is not None
    ]
    if len(in_basis) < 2:
        return (False, 0.0)
    world_spread = _pair_spread(world)
    basis_spread = _pair_spread(in_basis)
    return (world_spread > BASIS_ROTATION_DEG and basis_spread <= BASIS_ROTATION_DEG, world_spread)


def _rotation_spread(samples: Sequence[_Sample], unit: Sequence[float]) -> tuple[int, int, float]:
    """How much the trials' measured directions disagree with `unit`: (disagreeing, total, worst deg).

    One trial can be cut short by an obstacle or a state change, so a single outlier is not a
    disagreement about the direction. A majority that disagrees is: that is what a vector rotating with
    a moving entity basis looks like, and it must not be labelled world-relative.
    """
    worst = 0.0
    disagreeing = 0
    total = 0
    for sample in samples:
        horizontal = (sample.delta[0], 0.0, sample.delta[2])
        length = _norm(horizontal)
        if length <= 0:
            continue
        total += 1
        cosine = max(-1.0, min(1.0, _dot(_scale(horizontal, 1.0 / length), unit)))
        angle = math.degrees(math.acos(cosine))
        worst = max(worst, angle)
        if angle > BASIS_ROTATION_DEG:
            disagreeing += 1
    return (disagreeing, total, worst)


# A linear continuation of the measured descent can only be trusted a little past the last sample; past
# that, the rise is stated as unknown rather than given a bound nothing measured.
AIRTIME_EXTRAPOLATION_CAP = 2.0


def _rise_shape(samples: Sequence[_Sample]) -> tuple[float | None, float | None, list[str]]:
    """Measured (airtime, peak) of a rise primitive, and any notes about how they were read.

    Peak is the largest +y displacement any calibrated hold measured: a measured lower bound of the
    apex. Airtime is where that displacement returns to zero: interpolated between the two holds that
    straddle it, extrapolated from the measured descent when the sampled holds stop just short of it,
    and unknown when the samples never come back down.
    """
    notes: list[str] = []
    by_hold: dict[float, list[float]] = {}
    for sample in samples:
        by_hold.setdefault(round(sample.hold_s, 4), []).append(sample.delta[UP])
    points = sorted((hold, _median(values) or 0.0) for hold, values in by_hold.items())
    if not points or max(value for _, value in points) <= 0:
        return (None, None, ["the rise control never moved the entity up in a valid trial"])
    peak_hold, peak = max(points, key=lambda point: point[1])
    later = [point for point in points if point[0] > peak_hold]
    for (h0, v0), (h1, v1) in zip(later, later[1:]):
        if v0 > 0 >= v1:
            span = v0 - v1
            airtime = h0 + (h1 - h0) * (v0 / span) if span > 0 else h1
            notes.append(
                f"airtime interpolated between holds {h0:g} s ({v0:.3f} u) and {h1:g} s ({v1:.3f} u)"
            )
            return (airtime, peak, notes)
    if len(later) >= 2:
        (h0, v0), (h1, v1) = later[-2], later[-1]
        slope = (v1 - v0) / (h1 - h0) if h1 > h0 else 0.0
        if v1 > 0 and slope < 0:
            airtime = h1 + v1 / -slope
            if airtime <= h1 * AIRTIME_EXTRAPOLATION_CAP:
                notes.append(
                    f"airtime extrapolated from the descending holds {h0:g} s ({v0:.3f} u) and "
                    f"{h1:g} s ({v1:.3f} u)"
                )
                return (airtime, peak, notes)
            notes.append(
                f"airtime unknown: the measured descent would reach zero at {airtime:.2f} s, past the "
                f"{AIRTIME_EXTRAPOLATION_CAP:g}x cap on extrapolation"
            )
            return (None, peak, notes)
    notes.append("the rise never returns to zero within the calibrated holds: airtime is unknown")
    return (None, peak, notes)


def primitives_from_calibration(calibration: Mapping[str, Any], offered_actions: Sequence[str],
                                *, catalog: Any = None) -> Primitives:
    """Build the measured primitives once per run, from the calibration record.

    `offered_actions` are the action names the runner offers (the manifest's names, or the calibration's
    own action names). `catalog`, when given, is the manifest's action list: it is the only thing that
    says one declared action presses two controls at once, which is what makes a hop expressible.
    """
    notes: list[str] = []
    unsupported: list[str] = []
    controls = _controls(calibration)
    trials = [trial for trial in calibration.get("trials") or () if isinstance(trial, Mapping)]
    offered = list(offered_actions)
    resolved, combined = _resolve_actions(offered, controls, catalog, notes)
    if catalog is None:
        catalog = combined

    classified: list[Primitive] = []
    for action in offered:
        name = action if isinstance(action, str) else getattr(action, "name", None) or (
            action.get("name") if isinstance(action, Mapping) else None)
        if not isinstance(name, str):
            continue
        action = name
        ids = resolved.get(action) or ()
        if len(ids) != 1:
            if len(ids) > 1:
                notes.append(f"{action}: declared over {len(ids)} controls; it is not a single primitive")
            continue
        control = ids[0]
        samples = _samples(trials, control)
        if not samples:
            notes.append(f"{action}: no valid calibrated trial with a measured applied duration")
            continue
        primitive = _primitive(action, control, samples)
        if primitive is not None:
            classified.append(primitive)

    moves: list[Primitive] = []
    rises: list[tuple[Primitive, Sequence[_Sample]]] = []
    body_directions: dict[str, Direction] = {}
    for primitive in classified:
        if primitive.kind == KIND_MOVE:
            samples = _samples(trials, primitive.control or "")
            rotates, spread_deg = _basis_relative(samples)
            if rotates:
                yaws = sorted({round(sample.yaw_deg, 3) for sample in samples
                               if sample.yaw_deg is not None})
                body = _body_direction(primitive, samples)
                if body is None:
                    unsupported.append(
                        f"{primitive.action}: its measured direction turns with the entity basis "
                        f"({spread_deg:.1f} deg across trials at yaw {yaws}) and has no measured motion in "
                        "that basis, so it is not offered as a direction"
                    )
                    continue
                notes.append(
                    f"{primitive.action}: its measured direction turns with the entity basis "
                    f"({spread_deg:.1f} deg across trials at yaw {yaws}); offered as {body.label} in the "
                    f"entity's own basis at {body.speed:.3f} u/s"
                )
                existing = body_directions.get(body.label)
                if existing is None or body.speed > existing.speed:
                    body_directions[body.label] = body
                continue
            horizontal = (primitive.vector[0], 0.0, primitive.vector[2])
            length = _norm(horizontal)
            if length > 0:
                disagreeing, total, worst = _rotation_spread(
                    samples, _scale(horizontal, 1.0 / length)
                )
                if total and disagreeing * 2 > total:
                    yaws = sorted({round(sample.yaw_deg, 3) for sample in samples
                                   if sample.yaw_deg is not None})
                    if len(yaws) > 1:
                        unsupported.append(
                            f"{primitive.action}: {disagreeing} of {total} trials disagree with its "
                            f"measured direction by up to {worst:.1f} deg at yaw {yaws}; "
                            "basis-relative directions are not supported yet, so it is not offered "
                            "as a direction"
                        )
                    else:
                        unsupported.append(
                            f"{primitive.action}: {disagreeing} of {total} trials disagree with its "
                            f"measured direction by up to {worst:.1f} deg; not offered as a direction"
                        )
                    continue
            moves.append(primitive)
        elif primitive.kind == KIND_RISE:
            rises.append((primitive, _samples(trials, primitive.control or "")))

    directions: dict[str, Direction] = {}
    for primitive in moves:
        direction = _direction(primitive, notes, unsupported)
        if direction is None:
            continue
        existing = directions.get(direction.label)
        if existing is not None:
            notes.append(
                f"{direction.label}: {existing.action} ({existing.speed:.3f} u/s) and "
                f"{direction.action} ({direction.speed:.3f} u/s) both measure this direction; "
                f"the faster is offered, and the other stays usable for a chord"
            )
            if direction.speed <= existing.speed:
                directions[direction.label] = replace(
                    existing,
                    alternatives=existing.alternatives + ((direction.action, direction.control),),
                )
                continue
            direction = replace(
                direction,
                alternatives=((existing.action, existing.control), *existing.alternatives),
            )
        directions[direction.label] = direction

    rise: Primitive | None = None
    rise_duration: float | None = None
    rise_peak: float | None = None
    if rises:
        # The rise with the largest measured peak is the one that can leave the ground highest.
        scored: list[tuple[float, Primitive, float | None, float | None]] = []
        for primitive, samples in rises:
            airtime, peak, shape_notes = _rise_shape(samples)
            scored.append((peak or 0.0, primitive, airtime, peak))
            notes.extend(f"{primitive.action}: {note}" for note in shape_notes)
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) > 1:
            notes.append(
                f"{len(scored)} measured rise actions; {scored[0][1].action} has the largest peak"
            )
        rise = scored[0][1]
        rise_duration = scored[0][2]
        rise_peak = scored[0][3]

    primitives = Primitives(
        directions=tuple(directions[label] for label in sorted(directions)),
        body_directions=tuple(body_directions[label] for label in sorted(body_directions)),
        rise=rise,
        rise_duration_s=rise_duration,
        rise_peak=rise_peak,
        classified=tuple(classified),
        notes=tuple(notes),
        unsupported=tuple(unsupported),
    )
    return primitives.with_catalog(catalog)


# --- offered options ----------------------------------------------------------------------


def option_specs(primitives: Primitives) -> list[OptionSpec]:
    """The options these primitives support, in a deterministic order.

    One `traverse` and one `hop` per measured direction, one `retreat` per direction that has a
    measured opposite, and a fixed small set of waits. Ids follow the spec's examples: `traverse+x`,
    `hop-x`, `retreat-x:2.0`, `wait:0.5`. Intent text holds quantities only.
    """
    specs: list[OptionSpec] = []
    for direction in primitives.directions:
        specs.append(OptionSpec(
            option_id=f"traverse{direction.label}",
            template="traverse",
            params={
                "direction": list(direction.unit),
                "distance": None,
                "action": direction.action,
                "speed": direction.speed,
                "step": direction.step,
            },
            intent=(f"traverse {direction.label}: {direction.speed:.2f} u/s, "
                    f"step {_text(direction.step, 2)}"),
        ))

    if primitives.can_hop:
        for direction in primitives.directions:
            chorded = primitives.chorded_move(direction)
            if chorded is None:
                continue
            move_action, chord = chorded
            run_up = primitives.run_up(direction)
            launch = primitives.launch_distance(direction)
            specs.append(OptionSpec(
                option_id=f"hop{direction.label}",
                template="hop",
                params={
                    "direction": list(direction.unit),
                    "action": move_action,
                    "rise_action": primitives.rise.action if primitives.rise else None,
                    "chord": chord,
                    "run_up": run_up,
                    "launch_distance": launch,
                    "flight_s": primitives.rise_duration_s,
                    "rise_peak": primitives.rise_peak,
                    "speed": direction.speed,
                    "step": direction.step,
                    **({"air_s": primitives.rest_flight_s, "air_peak": primitives.rest_rise_peak}
                       if primitives.rest_flight_s is not None and primitives.rest_rise_peak is not None else {}),
                },
                intent=(f"hop {direction.label}: run-up {_text(run_up, 2)}, "
                        f"rise {_text(primitives.rise_duration_s, 2)} s, "
                        f"peak {_text(primitives.rise_peak, 2)}"),
            ))
            if primitives.air_rise_peak is not None and primitives.air_press_s and primitives.air_flight_s:
                # The same hop, pressing the rise again at the measured apex: its own measured peak and airtime.
                specs.append(OptionSpec(
                    option_id=f"hop2{direction.label}",
                    template="hop",
                    params={
                        "direction": list(direction.unit),
                        "action": move_action,
                        "rise_action": primitives.rise.action if primitives.rise else None,
                        "chord": chord,
                        "run_up": run_up,
                        "launch_distance": launch,
                        "flight_s": primitives.air_flight_s,
                        "rise_peak": primitives.air_rise_peak,
                        "air_press_s": primitives.air_press_s,
                        "speed": direction.speed,
                        "step": direction.step,
                        "air_s": primitives.air_flight_s,
                        "air_peak": primitives.air_rise_peak,
                    },
                    intent=(f"hop ×2 {direction.label}: run-up {_text(run_up, 2)}, "
                            f"rise {_text(primitives.air_flight_s, 2)} s, "
                            f"peak {_text(primitives.air_rise_peak, 2)}"),
                ))

    if primitives.can_hop:
        by_label = {d.label: d for d in primitives.body_directions}
        back_of = {"forward": "back", "back": "forward", "left": "right", "right": "left"}
        for direction in primitives.body_directions:
            # A hop measured in the entity's own basis: the chord that presses its move with the rise, and the
            # run-up on the opposite body move. Resolved to a world direction when the option starts.
            chorded = primitives.chorded_move(direction)
            back = by_label.get(back_of[direction.label])
            if chorded is None or back is None:
                continue
            move_action, chord = chorded
            run_up = primitives.run_up(direction)
            body = {"direction": list(direction.unit), "basis": "body", "label": direction.label,
                    "action": move_action, "rise_action": primitives.rise.action if primitives.rise else None,
                    "chord": chord, "run_up": run_up, "run_up_action": back.action,
                    "launch_distance": primitives.launch_distance(direction),
                    "speed": direction.speed, "step": direction.step}
            specs.append(OptionSpec(
                option_id=f"hop:{direction.label}", template="hop",
                params={**body, "flight_s": primitives.rise_duration_s, "rise_peak": primitives.rise_peak,
                        **({"air_s": primitives.rest_flight_s, "air_peak": primitives.rest_rise_peak}
                           if primitives.rest_flight_s is not None and primitives.rest_rise_peak is not None
                           else {})},
                intent=(f"hop {direction.label}: run-up {_text(run_up, 2)}, "
                        f"rise {_text(primitives.rise_duration_s, 2)} s, peak {_text(primitives.rise_peak, 2)}"),
            ))
            if primitives.air_rise_peak is not None and primitives.air_press_s and primitives.air_flight_s:
                specs.append(OptionSpec(
                    option_id=f"hop2:{direction.label}", template="hop",
                    params={**body, "flight_s": primitives.air_flight_s, "rise_peak": primitives.air_rise_peak,
                            "air_press_s": primitives.air_press_s, "air_s": primitives.air_flight_s,
                            "air_peak": primitives.air_rise_peak},
                    intent=(f"hop \u00d72 {direction.label}: run-up {_text(run_up, 2)}, "
                            f"rise {_text(primitives.air_flight_s, 2)} s, peak {_text(primitives.air_rise_peak, 2)}"),
                ))

    for direction in primitives.body_directions:
        # A move measured in the entity's own basis: resolved to a world direction when the option starts.
        specs.append(OptionSpec(
            option_id=f"traverse:{direction.label}",
            template="traverse",
            params={
                "direction": list(direction.unit),
                "basis": "body",
                "label": direction.label,
                "distance": None,
                "action": direction.action,
                "speed": direction.speed,
                "step": direction.step,
            },
            intent=(f"traverse {direction.label}: {direction.speed:.2f} u/s, step {_text(direction.step, 2)}"),
        ))

    for direction in primitives.directions:
        back = primitives.opposite(direction.unit)
        if back is None:
            continue
        run_up = primitives.run_up(direction)
        if run_up is None:
            continue
        specs.append(OptionSpec(
            option_id=f"retreat{direction.label}:{run_up:.1f}",
            template="retreat",
            params={
                "direction": list(back.unit),
                "distance": run_up,
                "action": back.action,
                "speed": back.speed,
                "step": back.step,
                "from": direction.label,
            },
            intent=(f"retreat from {direction.label}: {run_up:.2f} at {back.speed:.2f} u/s"),
        ))

    for seconds in WAIT_SECONDS:
        specs.append(OptionSpec(
            option_id=f"wait:{seconds:g}",
            template="wait",
            params={"seconds": seconds},
            intent=f"wait {seconds:g} s",
        ))
    return specs
