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

**Felt map.** Every probe reading (`public.probe`) is filed as what it measured, per cell, in the frame the
probe says it was taken in (`basis.plane`): a down-ray that found a surface files `floor` true at the cell of the
point it hit and the point's own height; a down-ray that found nothing inside its cast files `floor` false at
the cell of the ray itself, which is what "the floor ends here" means and is never inferred from a neighbour; a
forward cast that hit files the highest cast height that hit in the cell of its hit point, as a lower bound on
the obstruction's height. A cell holds the sample count and the last time it was measured, and the newest
reading wins per cell. In a side-view plane (`basis.plane == "2d"`) cells are along the motion axis and the floor
height; in 3-D they are the full cell. Nothing that was not measured is ever filed: no basis, no frame, so the
reading is not attributed to any cell; a cast whose ray position cannot be reconstructed (a grid row the basis
does not publish a lateral axis for) is skipped for the same reason.

**Persistence.** `save(path)` writes the whole memory as JSON: a header with the cell size, this schema's version
and the run ids that contributed, then the filed outcomes, the unplaced tally, the settled points, the felt cells
and the scene frames. `OutcomeMemory.load(path, cell)` builds a fresh memory from one file for this run's measured
step and refuses a file measured with a different step, because a different step is a different grid of places.
`merge(other)` adds another memory's facts to this one: counts add, the best advance keeps the larger value, and
the newest measured record wins per settled point and per felt cell. `autosave(path)` rewrites the file atomically
after each recorded outcome, so a crash keeps what was learned. A run only persists memory when the caller names a
file: with no path nothing here is written and the run is byte-identical to one without memory.

**Reading the map.** `map_line(scene, position, direction)` states what the map measured along the line through
one place, ahead and behind: how far the floor is known, and what ends it (a wall's lower bound, a drop, a step's
height difference). `wall_ahead` returns only the nearest measured obstruction's bound. Both answer `None` when
nothing was measured, and a column nobody measured stops the walk rather than carrying a fact across it.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
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

# --- the felt map -----------------------------------------------------------------------
PROBE = "probe"
UP = 1                 # index of Unity world +y in a position vector: the axis a floor's height is measured on
GROUND = (0, 2)        # the axes a move travels in, so the axis a place is walked along is one of them
PLANE_2D = "2d"
PLANE_3D = "3d"
SAME_SURFACE_FRACTION = 0.5  # half of one measured step: a surface this close to the last is the same floor
SPAN_LIMIT = 4096            # a loop bound, not a measurement: the longest run of cells one line may walk
FLOOR = "floor"
WALL = "wall"
DROP = "drop"
STEP = "step"
EDGE = "edge"
UNKNOWN = "unknown"
WALL_AT_LEAST = "\u2265"     # a cast height that hit is a lower bound on the obstruction's height

# --- persistence ------------------------------------------------------------------------
MEMORY_VERSION = 1
CELL_TOLERANCE = 1e-6  # a cell within one part in a million is the same measured step, and the same grid of cells


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


def _quantity(value: float) -> str:
    """One distance in world units to one decimal, with the typographic minus and no negative zero."""
    rounded = round(float(value), 1)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:.1f}".replace("-", MINUS, 1)


def _close_cell(left: float, right: float) -> bool:
    """Whether two measured steps are the same step to one part in a million."""
    return abs(left - right) <= CELL_TOLERANCE * max(abs(left), abs(right), 1.0)


def _stamp(observation: Any) -> str | None:
    """The moment one observation was taken, as text, or `None` when it carries no measured wall clock."""
    value = getattr(observation, "wall_utc", None)
    if isinstance(value, str):
        return value or None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass
class _FeltCell:
    """What one cell's probe readings measured: a floor, an obstruction, or neither."""

    floor: bool | None = None       # true: a surface was found; false: the ray past it found nothing
    height: float | None = None     # the measured world height of the surface a down-ray found in this cell
    wall: float | None = None       # the highest cast height that hit, a lower bound on the obstruction's height
    samples: int = 0                # readings filed in this cell
    last_seen: str | None = None    # when it was last measured; the newest reading of a cell wins



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
        # The scene's own frame for the felt map: ("2d", motion axis) or ("3d", 0). The first frame measured in a
        # scene keys its cells, and a later probe taken in another frame is not filed under a key that would mix them.
        self._planes: dict[str | None, tuple[str, int]] = {}
        self._felt: dict[str | None, dict[tuple[int, ...], _FeltCell]] = {}
        self._runs: set[str] = set()
        self._save_path: Path | None = None
        self.save_error: str | None = None  # the last autosave failure, or None; a failed write never ends a run

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
            self._autosave()
            return
        key = (outcome.scene, self.cell_of(start), outcome.option_id)
        history = self._history.get(key)
        if history is None:
            history = self._history[key] = OptionHistory(option_id=outcome.option_id)
        history.tries += 1
        history.count(outcome.terminated_by)
        # A body-relative option measured its world direction when it started; its params stay the spec's.
        direction = _vector((outcome.measured or {}).get("world_direction") or outcome.params.get("direction"))
        displacement = _vector(outcome.displacement)
        # A fall's ground covered is not an advance the body holds: it ended below the route it was on, so a fall
        # never sets `best` (a hop that always falls would otherwise read as the biggest gain).
        if direction is not None and displacement is not None and outcome.terminated_by != "fell":
            advance = _dot(displacement, direction)
            if advance is not None and (history.best_advance is None or advance > history.best_advance):
                history.best_advance = advance
        end = _vector(outcome.end_position)
        if end is not None:
            history.last_end = list(end)
        self._autosave()

    # --- observations ------------------------------------------------------------

    def observe(self, observation: Any) -> None:
        """Read one observation: its probe into the felt map, and its pose into a settled point."""
        public = _public(observation)
        scene = getattr(observation, "scene", None)
        self._read_felt(scene, public.get(PROBE), _stamp(observation))
        declared = public.get(ALIVE_READERS)
        if isinstance(declared, (list, tuple)) and all(isinstance(alias, str) for alias in declared):
            self._alive_aliases = tuple(declared)
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

    # --- the felt map ---------------------------------------------------------------

    def _read_felt(self, scene: str | None, probe: Any, stamp: str | None) -> None:
        """File one probe reading: every down-ray and forward cast where it landed, or where it found nothing.

        Only a reading whose frame the probe states can be filed: without `basis` there is no axis the samples
        are ahead on, so the reading is not attributed to a cell. A grid row whose lateral offset is not zero
        cannot be placed either, because the basis publishes the forward axis and not the lateral one.
        """
        reading = _mapping(probe)
        basis = _mapping(reading.get("basis"))
        plane = basis.get("plane")
        forward = _vector(basis.get("forward"))
        anchor = _vector(_mapping(reading.get("anchor")).get("position"))
        if plane not in (PLANE_2D, PLANE_3D) or forward is None or anchor is None:
            return
        axis = _motion_axis(forward)
        if plane == PLANE_2D and not self._frame(scene, plane, axis):
            return
        if plane == PLANE_3D and not self._frame(scene, plane, 0):
            return
        rays = [reading.get("under")]
        for name in ("samples", "backward"):
            rows = reading.get(name)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                rays.extend(row for row in rows if isinstance(row, Mapping))
        for entry in rays:
            if not isinstance(entry, Mapping):
                continue
            point = _vector(entry.get("point"))
            hit = entry.get("hit")
            if hit is True and point is not None:
                self._file_felt(scene, point, floor=True, height=point[UP], stamp=stamp)
                continue
            if hit is not False:
                continue  # a reading that is neither a hit nor a miss measured nothing
            lateral = _number(entry.get("lateral"))
            offset = _number(entry.get("forward"))
            if offset is None or lateral is None or lateral != 0:
                continue
            ray = [value + direction * offset for value, direction in zip(anchor, forward)]
            self._file_felt(scene, ray, floor=False, height=None, stamp=stamp)
        blockers = reading.get("blockers")
        for cast in blockers if isinstance(blockers, Sequence) and not isinstance(blockers, (str, bytes)) else ():
            if not isinstance(cast, Mapping) or cast.get("hit") is not True:
                continue
            point, height = _vector(cast.get("point")), _number(cast.get("height"))
            if point is None or height is None:
                continue
            self._file_felt(scene, point, floor=None, height=None, stamp=stamp, wall=height)

    def _frame(self, scene: str | None, plane: str, axis: int) -> bool:
        """Whether this scene's cells are keyed in this frame; the first frame measured in a scene keys them."""
        known = self._planes.get(scene)
        if known is None:
            self._planes[scene] = (plane, axis)
            return True
        return known == (plane, axis)

    def _file_felt(self, scene: str | None, point: Sequence[float], *, floor: bool | None,
                   height: float | None, stamp: str | None, wall: float | None = None) -> None:
        """File one reading under the cell of the point it measured; the newest reading of a cell wins."""
        cell = self._felt_cell(scene, point)
        filed = self._felt.setdefault(scene, {}).get(cell)
        if filed is None:
            filed = self._felt[scene][cell] = _FeltCell()
        filed.samples += 1
        filed.last_seen = stamp
        if floor is not None:
            filed.floor, filed.height = floor, height
        if wall is not None:
            filed.wall = wall if filed.wall is None else max(filed.wall, wall)

    def _felt_cell(self, scene: str | None, point: Sequence[float]) -> tuple[int, ...]:
        """The cell one measured point is filed under, in the scene's own frame."""
        plane, axis = self._planes.get(scene, (PLANE_3D, 0))
        if plane == PLANE_2D:
            if len(point) <= max(axis, UP):
                raise ValueError(f"a planar cell needs a position with a motion axis and a height, not {point!r}")
            return (math.floor(point[axis] / self.cell), math.floor(point[UP] / self.cell))
        return tuple(math.floor(value / self.cell) for value in point)

    def map_line(self, scene: str | None, position: Sequence[float] | None,
                 direction: Sequence[float] | None) -> str | None:
        """What the felt map measured along this direction at this place, as quantities: `None` when nothing is known.

        `direction` is any vector along the line to describe; its sign says which side is ahead. The line walks
        one cell at a time from the place's own column, over cells that were measured, and stops at the first
        column whose floor ends (a down-ray found nothing), whose cast hit an obstruction, whose floor is at a
        different height, or that nobody measured. Behind is described by the same rule in the other direction.
        """
        frame = self._walk_frame(scene, position, direction)
        if frame is None:
            return None
        scene_cells = self._felt.get(scene, {})
        start_height = self._start_height(scene_cells, frame)
        if start_height is None:
            return None
        sides = []
        for sign, word in ((frame["sign"], "ahead"), (-frame["sign"], "behind")):
            known, stop, wall, step = self._span(scene_cells, frame, sign, start_height)
            text = _side_text(known, stop, wall, step, word, self.cell)
            if text:
                sides.append(text)
        return "; ".join(sides) if sides else None

    def wall_ahead(self, scene: str | None, position: Sequence[float] | None,
                   direction: Sequence[float] | None) -> float | None:
        """A lower bound on the height of the nearest obstruction measured ahead, or `None` when none was.

        The bound is the highest cast height that hit in that column: a wall at least that tall stands there.
        A column nobody measured between the place and a later wall stops the search, because the nearest
        obstruction is then unknown rather than the one further on.
        """
        frame = self._walk_frame(scene, position, direction)
        if frame is None:
            return None
        cells = self._felt.get(scene, {})
        column = frame["start"]
        for _ in range(SPAN_LIMIT):
            column += frame["sign"]
            row = self._column_cells(cells, frame, column)
            if not row:
                return None
            walls = [filed.wall for filed in row.values() if filed.wall is not None]
            if walls:
                return max(walls)
        return None

    def _walk_frame(self, scene: str | None, position: Sequence[float] | None,
                    direction: Sequence[float] | None) -> dict[str, Any] | None:
        """The axis, sign and column a line at this place and direction walks along; `None` when unmeasured."""
        measured, vector = _vector(position), _vector(direction)
        if measured is None or vector is None:
            return None
        plane, plane_axis = self._planes.get(scene, (PLANE_3D, 0))
        if plane == PLANE_2D:
            if plane_axis >= len(vector) or plane_axis >= len(measured) or vector[plane_axis] == 0:
                return None
            axis, fixed, sign = plane_axis, {}, 1 if vector[plane_axis] > 0 else -1
        else:
            ground = [index for index in GROUND if index < len(vector)]
            if not ground:
                return None
            axis = max(ground, key=lambda index: abs(vector[index]))
            if vector[axis] == 0 or axis >= len(measured):
                return None
            sign = 1 if vector[axis] > 0 else -1
            fixed = {index: math.floor(measured[index] / self.cell)
                     for index in ground if index != axis and index < len(measured)}
        return {"axis": axis, "sign": sign, "fixed": fixed,
                "start": math.floor(measured[axis] / self.cell), "position": measured}

    def _column_cells(self, cells: Mapping[tuple[int, ...], _FeltCell], frame: Mapping[str, Any],
                      column: int) -> dict[tuple[int, ...], _FeltCell]:
        """Every measured cell of one column: the cells at this column and the frame's other fixed coordinates."""
        found = {}
        for cell, filed in cells.items():
            if len(cell) != len(frame["fixed"]) + 2 or cell[frame["axis"]] != column:
                continue
            if any(cell[index] != value for index, value in frame["fixed"].items()):
                continue
            found[cell] = filed
        return found

    def _start_height(self, cells: Mapping[tuple[int, ...], _FeltCell],
                      frame: Mapping[str, Any]) -> float | None:
        """The measured floor this place stands on: the highest surface in its own column at or below it."""
        measured = frame["position"]
        here = measured[UP] if len(measured) > UP else None
        if here is None:
            return None
        floors = [filed.height for filed in self._column_cells(cells, frame, frame["start"]).values()
                  if filed.floor is True and filed.height is not None]
        below = [height for height in floors if height <= here + SAME_SURFACE_FRACTION * self.cell]
        if below:
            return max(below)
        return min(floors) if floors else None

    def _span(self, cells: Mapping[tuple[int, ...], _FeltCell], frame: Mapping[str, Any], sign: int,
              height: float) -> tuple[int, str, float | None, float | None]:
        """Walk one side of a place: (cells of floor, what stopped it, wall bound, step height difference)."""
        known, column, current = 0, frame["start"], height
        for _ in range(SPAN_LIMIT):
            column += sign
            row = self._column_cells(cells, frame, column)
            if not row:
                return known, UNKNOWN, None, None
            walls = [filed.wall for filed in row.values() if filed.wall is not None]
            if walls:  # a cast that hit is at this column, so the span ends at an obstruction, not at floor
                return known, WALL, max(walls), None
            floors = [filed.height for filed in row.values()
                      if filed.floor is True and filed.height is not None]
            if floors:
                nearest = min(floors, key=lambda value: abs(value - current))
                if abs(nearest - current) > SAME_SURFACE_FRACTION * self.cell:
                    return known, STEP, None, nearest - current
                current, known = nearest, known + 1
                continue
            if any(filed.floor is False for filed in row.values()):
                return known, DROP, None, None
            return known, UNKNOWN, None, None
        return known, EDGE, None, None

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

    def place_summary(self, scene: str | None, position: Sequence[float] | None) -> dict[str, int] | None:
        """Each choice tried at this place, by id, with how many times it ended `succeeded` here."""
        measured = _vector(position) if position is not None else None
        if measured is None:
            return None
        cell = self.cell_of(measured)
        return {option_id: history.terminations.get("succeeded", 0)
                for (other_scene, other_cell, option_id), history in self._history.items()
                if other_scene == scene and other_cell == cell and history.tries}

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

    # --- persistence ---------------------------------------------------------------

    def note_run(self, run_id: str | None) -> None:
        """Name one run that contributed to this memory; the header keeps them, so a file says where it came from."""
        if isinstance(run_id, str) and run_id:
            self._runs.add(run_id)

    def stats(self) -> dict[str, Any]:
        """What this memory holds, as counts: what a plan or a summary states about it."""
        return {"run_ids": sorted(self._runs),
                "entries": len(self._history) + len(self._unplaced),
                "outcomes": sum(history.tries for history in self._history.values())
                            + sum(sum(counts.values()) for counts in self._unplaced.values()),
                "settled_points": sum(len(points) for points in self._settled.values()),
                "felt_cells": sum(len(cells) for cells in self._felt.values())}

    def autosave(self, path: Path | None) -> None:
        """Keep this memory in one file from now on: one atomic write after each recorded outcome."""
        self._save_path = None if path is None else Path(path)

    def _autosave(self) -> None:
        if self._save_path is None:
            return
        try:
            self.save()
        except OSError as exc:  # a file that cannot be written is a fact about the disk, not a reason to stop
            self.save_error = f"{type(exc).__name__}: {exc}"

    def save(self, path: Path | None = None) -> Path:
        """Write the whole memory as JSON, atomically: a temp file in the same folder replaces the target."""
        target = Path(path) if path is not None else self._save_path
        if target is None:
            raise ValueError("this memory has no file: pass a path, or name one once with autosave")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_json(), sort_keys=True, indent=2, ensure_ascii=False,
                             allow_nan=False) + "\n"
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        self.save_error = None
        return target

    def to_json(self) -> dict[str, Any]:
        """This memory as JSON-ready data: a header, then every filed fact in a fixed order."""
        planes = [{"scene": scene, "plane": plane, "axis": axis}
                  for scene, (plane, axis) in sorted(self._planes.items(), key=lambda item: str(item[0]))]
        history = [{"scene": scene, "cell": list(cell), "option_id": option_id,
                    "tries": row.tries,
                    "terminations": {term: row.terminations[term] for term in TERMINATIONS
                                     if row.terminations.get(term)},
                    "best_advance": row.best_advance, "last_end": row.last_end}
                   for (scene, cell, option_id), row in sorted(
                       self._history.items(), key=lambda item: (str(item[0][0]), item[0][1], item[0][2]))]
        unplaced = [{"scene": scene, "option_id": option_id, "terminations": dict(counts)}
                    for (scene, option_id), counts in sorted(
                        self._unplaced.items(), key=lambda item: (str(item[0][0]), item[0][1]))]
        settled = [{"scene": scene, "cell": list(cell), "position": list(point)}
                   for scene, points in sorted(self._settled.items(), key=lambda item: str(item[0]))
                   for cell, point in points.items()]
        felt = [{"scene": scene, "cell": list(cell), "floor": filed.floor, "height": filed.height,
                 "wall": filed.wall, "samples": filed.samples, "last_seen": filed.last_seen}
                for scene, cells in sorted(self._felt.items(), key=lambda item: str(item[0]))
                for cell, filed in sorted(cells.items())]
        return {"version": MEMORY_VERSION, "cell": self.cell, "runs": sorted(self._runs),
                "planes": planes, "history": history, "unplaced": unplaced, "settled": settled,
                "felt": felt}

    @classmethod
    def load(cls, path: Path, cell: float) -> "OutcomeMemory":
        """Read one saved memory for this run's measured step; a file measured with another step is refused."""
        path = Path(path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"the outcome memory {path.name} could not be read: {exc}") from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"the outcome memory {path.name} is not JSON: {exc}") from None
        return cls._from_json(data, cell, path.name)

    @classmethod
    def _from_json(cls, data: Any, cell: float, label: str) -> "OutcomeMemory":
        if not isinstance(data, dict):
            raise ValueError(f"the outcome memory {label} is not an object")
        if data.get("version") != MEMORY_VERSION:
            raise ValueError(f"the outcome memory {label} is version {data.get('version')!r}; "
                             f"this build reads version {MEMORY_VERSION}")
        stored = _number(data.get("cell"))
        if stored is None:
            raise ValueError(f"the outcome memory {label} states no cell size")
        if not _close_cell(stored, float(cell)):
            raise ValueError(
                f"the outcome memory {label} was measured with a cell of {stored:.4f} world units and this run "
                f"measured {float(cell):.4f}: a different step is a different grid of places, so it is not loaded")
        memory = cls(cell)
        for run_id in data.get("runs") or ():
            memory.note_run(run_id)
        for row in _rows(data.get("planes"), label, "planes"):
            scene, plane, axis = row.get("scene"), row.get("plane"), row.get("axis")
            if plane not in (PLANE_2D, PLANE_3D) or isinstance(axis, bool) or not isinstance(axis, int):
                raise ValueError(f"the outcome memory {label} has a malformed scene frame")
            memory._planes[scene] = (plane, axis)
        for row in _rows(data.get("history"), label, "history"):
            scene, option_id = row.get("scene"), row.get("option_id")
            if not isinstance(option_id, str) or not option_id:
                raise ValueError(f"the outcome memory {label} has an entry without an option id")
            filed = OptionHistory(option_id=option_id, tries=_count(row.get("tries"), label),
                                  best_advance=_number(row.get("best_advance")),
                                  last_end=_points(row.get("last_end")))
            for term, count in _terminations(row.get("terminations"), label).items():
                filed.terminations[term] = count
            memory._history[(scene, _cell(row.get("cell"), label), option_id)] = filed
        for row in _rows(data.get("unplaced"), label, "unplaced"):
            option_id = row.get("option_id")
            if not isinstance(option_id, str) or not option_id:
                raise ValueError(f"the outcome memory {label} has an unplaced tally without an option id")
            memory._unplaced[(row.get("scene"), option_id)] = _terminations(row.get("terminations"), label)
        for row in _rows(data.get("settled"), label, "settled"):
            point = _points(row.get("position"))
            if point is None:
                raise ValueError(f"the outcome memory {label} has a settled point without a position")
            memory._settled.setdefault(row.get("scene"), {})[_cell(row.get("cell"), label)] = tuple(point)
        for row in _rows(data.get("felt"), label, "felt"):
            floor = row.get("floor")
            if floor not in (True, False, None):
                raise ValueError(f"the outcome memory {label} has a malformed floor reading")
            samples = row.get("samples")
            if isinstance(samples, bool) or not isinstance(samples, int) or samples < 0:
                raise ValueError(f"the outcome memory {label} has a malformed sample count")
            last_seen = row.get("last_seen")
            if last_seen is not None and not isinstance(last_seen, str):
                raise ValueError(f"the outcome memory {label} has a malformed last-seen time")
            memory._felt.setdefault(row.get("scene"), {})[_cell(row.get("cell"), label)] = _FeltCell(
                floor=floor, height=_number(row.get("height")), wall=_number(row.get("wall")),
                samples=samples, last_seen=last_seen)
        return memory

    def merge(self, other: "OutcomeMemory") -> None:
        """Add another memory's measured facts to this one: counts add, and the newest record wins per cell.

        `best_advance` keeps the larger measured advance; a settled point replaces the one in the same cell; a
        felt cell takes the reading with the later `last_seen`, and a reading with a known time beats one whose
        time is unknown. Nothing is averaged and nothing is inferred from the other memory's absence.
        """
        if not isinstance(other, OutcomeMemory):
            raise ValueError(f"only another outcome memory merges into this one, not {other!r}")
        if not _close_cell(other.cell, self.cell):
            raise ValueError(f"an outcome memory measured with a {other.cell:.4f} cell cannot merge into a "
                             f"{self.cell:.4f} one")
        self._runs.update(other._runs)
        for scene, (plane, axis) in other._planes.items():
            self._planes.setdefault(scene, (plane, axis))
        for key, theirs in other._history.items():
            mine = self._history.get(key)
            if mine is None:
                mine = self._history[key] = OptionHistory(option_id=theirs.option_id)
            mine.tries += theirs.tries
            for term, count in theirs.terminations.items():
                mine.terminations[term] = mine.terminations.get(term, 0) + count
            if theirs.best_advance is not None and (mine.best_advance is None
                                                    or theirs.best_advance > mine.best_advance):
                mine.best_advance = theirs.best_advance
            if theirs.last_end is not None:
                mine.last_end = list(theirs.last_end)
        for key, counts in other._unplaced.items():
            mine = self._unplaced.setdefault(key, {})
            for term, count in counts.items():
                mine[term] = mine.get(term, 0) + count
        for scene, points in other._settled.items():
            self._settled.setdefault(scene, {}).update(points)
        for scene, cells in other._felt.items():
            mine = self._felt.setdefault(scene, {})
            for cell, theirs in cells.items():
                held = mine.get(cell)
                if held is None or _newer(theirs.last_seen, held.last_seen):
                    mine[cell] = _FeltCell(floor=theirs.floor, height=theirs.height, wall=theirs.wall,
                                           samples=held.samples + theirs.samples if held else theirs.samples,
                                           last_seen=theirs.last_seen or (held.last_seen if held else None))
                else:
                    held.samples += theirs.samples
                    if held.wall is None or (theirs.wall is not None and theirs.wall > held.wall):
                        held.wall = theirs.wall


def _newer(left: str | None, right: str | None) -> bool:
    """Whether one measured time is later than another; a known time is later than an unknown one."""
    if left is None:
        return False
    return True if right is None else left > right


def _rows(value: Any, label: str, name: str) -> list[Mapping[str, Any]]:
    """One saved section as a list of mappings, or a refusal: a malformed file is never read as partial facts."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"the outcome memory {label} has a malformed {name} section")
    return value


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"the outcome memory {label} has a malformed count")
    return value


def _terminations(value: Any, label: str) -> dict[str, int]:
    """One saved termination tally, keeping `TERMINATIONS` order and refusing a word it cannot render."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"the outcome memory {label} has a malformed termination tally")
    counts = {}
    for term, count in value.items():
        if term not in TERMINATIONS:
            raise ValueError(f"the outcome memory {label} names an unknown termination {term!r}")
        counts[term] = _count(count, label)
    return {term: counts[term] for term in TERMINATIONS if counts.get(term)}


def _cell(value: Any, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value or any(
            isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"the outcome memory {label} has a malformed cell")
    return tuple(value)


def _points(value: Any) -> list[float] | None:
    """One saved position as a list of finite floats, or `None` when it was not measured."""
    measured = _vector(value)
    return None if measured is None else list(measured)


def _motion_axis(forward: Sequence[float]) -> int:
    """The ground axis a probe's own forward direction mostly points along: the axis its samples advance on."""
    axes = [index for index in GROUND if index < len(forward)]
    return max(axes, key=lambda index: abs(forward[index])) if axes else 0


def _side_text(known: int, stop: str, wall: float | None, step: float | None, word: str,
               cell: float) -> str | None:
    """One side of a map line: how far the measured floor reaches, and what the span ends on.

    Quantities only: a distance in world units from cells times the measured cell, a wall's measured lower
    bound, a step's measured height difference. A side nobody measured says nothing.
    """
    span = f"floor known {_quantity(known * cell)} {word}" if known else ""
    if stop == WALL and wall is not None:
        tail = f"wall {WALL_AT_LEAST}{_quantity(wall)} high"
    elif stop == DROP:
        tail = DROP
    elif stop == STEP and step is not None:
        tail = f"{STEP} {_signed(step)}"
    else:
        return span or None  # the span simply ends, or nothing was measured on this side
    if span:
        return f"{span}, then {tail}"
    return f"{tail} {_quantity((known + 1) * cell)} {word}"


def memory_file(directory: Path, game: str | None, scene: str | None) -> Path:
    """The memory file for one game and scene inside `directory`: one file per place, never a mixed one.

    The identity is the game project's own directory name (what the run record calls `game_project`) and the
    scene id. It is deliberately not the preflight fingerprint: that covers every authored file, so editing a
    script would orphan the memory, while the cells are world coordinates in one scene and stay valid across
    manifest revisions.
    """
    return Path(directory) / f"{_slug(game)}--{_slug(scene)}.json"


def _slug(value: str | None) -> str:
    """A filename-safe form of a measured name: letters, digits, dot, dash and underscore kept, the rest `-`."""
    text = "".join(char if (char.isalnum() or char in "._-") else "-" for char in (value or ""))
    return text.strip("-") or "unknown"


def _public(observation: Any) -> Mapping[str, Any]:
    """The observation's published readers, or none when it carries no usable mapping."""
    public = getattr(observation, "public", None)
    if isinstance(public, Mapping):
        return public
    return {}
