"""Outcome memory for the options layer (docs/design/options-layer.md, sections 5 and 7).

An option is one closed-loop routine. What it measurably did at a place is remembered and written into that
option's label, because S1 scores labels and keeps nothing between turns.

**Place.** A cell is `floor(position / cell)` per axis, and `cell` is the measured displacement of one short
move step (the caller derives it from one calibrated step). The scene id is part of every key: positions in one
scene are world coordinates, so they stay comparable across play-mode restarts, and two scenes never share a
cell. An outcome is filed under the cell of its `start_position`, because "here" means where the option was
started. An outcome with no measured start position is counted in the unplaced tally and never filed under a
guessed cell.

**Memory.** Per (scene, cell, option_id): the try count, counts by termination, the best `displacement .
direction` over the tries whose `params["direction"]` is a unit vector, and the last measured end position. The
best value stays unknown (never zero) when no try carried a direction or a displacement.

**Settled points.** A position where the controlled entity stayed within `settle_radius(cell)` for
`SETTLE_OBSERVATIONS` consecutive observations, at most `SETTLE_SPEED_CELLS_PER_S * cell` of measured speed,
alive, in one scene. A quarter of a measured step is the radius and one step per second the speed bound; both
are fractions of the one measured quantity this memory is given. A measured zero speed means still, a missing
velocity is unknown and does not disqualify. Aliveness reads the readers the observation declares as alive
readers, and an observation that declares none, or declares one whose value is unmeasured, disqualifies
nothing: unknown is not dead. At most one settled point per cell is kept, the latest, and they come back in
first-seen order.

**Rendering.** Suffixes and the cell fact are quantities only and are rendered from memory alone, in
`TERMINATIONS` order with fixed rounding, so the same memory always renders the same text. Negative numbers use
`MINUS` (U+2212), the typographic minus, in both.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from playtest.runner.options.model import TERMINATIONS, OptionOutcome

# The one measured quantity this memory is given is a step, `cell`. These are fractions of it, never constants
# in world units, so the same memory works at any scale.
SETTLE_RADIUS_FRACTION = 0.25  # a quarter of one step: inside one cell, well above measurement noise
SETTLE_SPEED_FRACTION = 1.0  # at most one step per second of measured speed
SETTLE_OBSERVATIONS = 3  # consecutive still observations that make one settled point

CONTROLLED = "controlled"
ALIVE_READERS = "alive_readers"  # a declared list of reader aliases whose dead or zero value is not alive
MINUS = "\u2212"
UNMEASURED_POSITION = "here: position unmeasured"
UNTRIED = "here: untried"


def _vector(value: Any) -> tuple[float, ...] | None:
    """A measured position or vector as finite floats, or `None` when it is not one (unknown is never zero)."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    if not value:
        return None
    out: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        out.append(number)
    return tuple(out)


def _dot(left: Sequence[float], right: Sequence[float]) -> float | None:
    """The dot product of two measured vectors, or `None` when their lengths differ (never a partial sum)."""
    if len(left) != len(right):
        return None
    return sum(a * b for a, b in zip(left, right))


def _dead(value: Any) -> bool:
    """Whether one declared alive reader reads dead: a false flag, or a number at zero or below."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value <= 0
    return False


def _signed(value: float) -> str:
    """A quantity signed to one decimal, with no negative zero."""
    rounded = round(value, 1)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:+.1f}".replace("-", MINUS, 1) if rounded < 0 else f"{rounded:+.1f}"


def _coordinate(value: int) -> str:
    """One cell coordinate; negative coordinates use the typographic minus."""
    return str(value).replace("-", MINUS, 1)


@dataclass
class OptionHistory:
    """What one option measurably did at one place."""

    option_id: str
    tries: int = 0
    terminations: dict[str, int] = field(default_factory=dict)
    best_advance: float | None = None  # max of displacement . direction; unknown stays unknown
    last_end: list[float] | None = None

    def count(self, terminated_by: str) -> None:
        self.terminations[terminated_by] = self.terminations.get(terminated_by, 0) + 1

    def termination_text(self) -> str:
        """`blocked 3, fell 1` in `TERMINATIONS` order: the terms with a nonzero count, and nothing else."""
        return ", ".join(f"{term} {self.terminations[term]}"
                         for term in TERMINATIONS if self.terminations.get(term))


@dataclass
class _Still:
    """One run of consecutive still observations."""

    scene: str | None
    anchor: tuple[float, ...]
    count: int


class OutcomeMemory:
    """Measured outcomes per (scene, cell, option), and the settled points visited per scene."""

    def __init__(self, cell: float) -> None:
        if isinstance(cell, bool) or not isinstance(cell, (int, float)):
            raise ValueError(f"the cell size must be a measured step in world units, not {cell!r}")
        if not math.isfinite(cell) or cell <= 0:
            raise ValueError(f"the cell size must be a positive measured step, not {cell!r}")
        self.cell = float(cell)
        self._history: dict[tuple[str | None, tuple[int, ...], str], OptionHistory] = {}
        self._unplaced: dict[tuple[str | None, str], dict[str, int]] = {}
        self._settled: dict[str | None, dict[tuple[int, ...], tuple[float, ...]]] = {}
        self._still: _Still | None = None
        self._alive_aliases: tuple[str, ...] = ()

    # --- places ------------------------------------------------------------------

    def cell_of(self, position: Sequence[float]) -> tuple[int, ...]:
        """The cell a measured position is in: `floor(position / cell)` per axis."""
        measured = _vector(position)
        if measured is None:
            raise ValueError(f"a cell needs a measured position, not {position!r}")
        return tuple(math.floor(value / self.cell) for value in measured)

    @property
    def unplaced(self) -> dict[tuple[str | None, str], dict[str, int]]:
        """Outcomes with no measured start position, by (scene, option_id) and termination: never filed here."""
        return {(scene, option_id): {term: counts[term] for term in TERMINATIONS if counts.get(term)}
                for (scene, option_id), counts in sorted(self._unplaced.items(),
                                                        key=lambda item: (str(item[0][0]), item[0][1]))}

    # --- outcomes ----------------------------------------------------------------

    def record(self, outcome: OptionOutcome) -> None:
        """File one measured outcome under the cell of its start position, or in the unplaced tally."""
        start = _vector(outcome.start_position)
        if start is None:
            counts = self._unplaced.setdefault((outcome.scene, outcome.option_id), {})
            counts[outcome.terminated_by] = counts.get(outcome.terminated_by, 0) + 1
            return
        key = (outcome.scene, self.cell_of(start), outcome.option_id)
        history = self._history.get(key)
        if history is None:
            history = self._history[key] = OptionHistory(option_id=outcome.option_id)
        history.tries += 1
        history.count(outcome.terminated_by)
        direction = _vector(outcome.params.get("direction"))
        displacement = _vector(outcome.displacement)
        if direction is not None and displacement is not None:
            advance = _dot(displacement, direction)
            if advance is not None and (history.best_advance is None or advance > history.best_advance):
                history.best_advance = advance
        end = _vector(outcome.end_position)
        if end is not None:
            history.last_end = list(end)

    # --- observations ------------------------------------------------------------

    def observe(self, observation: Any) -> None:
        """Read one observation for a settled point: still, alive, and in one scene."""
        public = _public(observation)
        declared = public.get(ALIVE_READERS)
        if isinstance(declared, (list, tuple)) and all(isinstance(alias, str) for alias in declared):
            self._alive_aliases = tuple(declared)
        scene = getattr(observation, "scene", None)
        controlled = public.get(CONTROLLED)
        position = _vector(controlled.get("position")) if isinstance(controlled, Mapping) else None
        if position is None:  # no pose: the run of consecutive observations is broken
            self._still = None
            return
        if (not self._clock_running(observation) or not self._alive(public)
                or not self._still_velocity(controlled, position)):
            self._still = None
            return
        run = self._still
        if run is None or run.scene != scene or math.dist(run.anchor, position) > self._settle_radius():
            self._still = _Still(scene=scene, anchor=position, count=1)
            return
        run.count += 1
        if run.count >= SETTLE_OBSERVATIONS:
            self._settled.setdefault(scene, {})[self.cell_of(position)] = position

    @staticmethod
    def _clock_running(observation: Any) -> bool:
        """A body only settles in a running game. Death, pause, menus and dialogue stop the game clock in many
        games, and a body frozen there is not a safe place: a measured stopped clock disqualifies; unknown does not.
        """
        if getattr(observation, "paused", None) is True:
            return False
        scale = getattr(observation, "time_scale", None)
        return not (isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale <= 0)

    def _alive(self, public: Mapping[str, Any]) -> bool:
        """No declared alive reader reads dead. Unknown, undeclared or unreadable values disqualify nothing."""
        for alias in self._alive_aliases:
            if _dead(public.get(alias)):
                return False
        return True

    def _still_velocity(self, controlled: Mapping[str, Any], position: Sequence[float]) -> bool:
        """Whether the measured velocity is small enough to count as still; unknown velocity does not decide."""
        velocity = _vector(controlled.get("velocity"))
        if velocity is None or len(velocity) != len(position):
            return True
        return math.dist((0.0,) * len(velocity), velocity) <= SETTLE_SPEED_FRACTION * self.cell

    def _settle_radius(self) -> float:
        """A quarter of one step: one radius for every axis, derived from the one measured quantity given."""
        return SETTLE_RADIUS_FRACTION * self.cell

    def settled_points(self, scene: str | None) -> list[list[float]]:
        """The settled points of one scene, one per cell, in first-seen order."""
        return [list(point) for point in self._settled.get(scene, {}).values()]

    # --- rendering ---------------------------------------------------------------

    def suffix(self, scene: str | None, position: list[float] | None,
               option_id: str) -> str:
        """This option's measured history at this place, as a label suffix: quantities only."""
        if position is None:
            return UNMEASURED_POSITION
        measured = _vector(position)
        if measured is None:
            return UNMEASURED_POSITION
        history = self._history.get((scene, self.cell_of(measured), option_id))
        if history is None or not history.tries:
            return UNTRIED
        parts = [f"{history.tries} tried"]
        terms = history.termination_text()
        if terms:
            parts.append(terms)
        if history.best_advance is not None:
            parts.append(f"best {_signed(history.best_advance)}")
        head, tail = parts[0], parts[1:]
        if not tail:
            return f"here: {head}"
        return f"here: {head} \u2014 {', '.join(tail)}"

    def cell_line(self, scene: str | None, position: list[float] | None) -> str | None:
        """One fact line for this place across every option, or `None` when this place has no history."""
        if position is None:
            return None
        measured = _vector(position)
        if measured is None:
            return None
        cell = self.cell_of(measured)
        rows = [(option_id, history) for (other_scene, other_cell, option_id), history
                in self._history.items()
                if other_scene == scene and other_cell == cell and history.tries]
        if not rows:
            return None
        segments = []
        for option_id, history in sorted(rows, key=lambda row: row[0]):
            terms = history.termination_text()
            segments.append(f"{option_id} {history.tries} tried" + (f", {terms}" if terms else ""))
        where = ", ".join(_coordinate(value) for value in cell)
        return f"This place (cell ({where})): {'; '.join(segments)}."


def _public(observation: Any) -> Mapping[str, Any]:
    """The observation's published readers, or none when it carries no usable mapping."""
    public = getattr(observation, "public", None)
    if isinstance(public, Mapping):
        return public
    return {}
