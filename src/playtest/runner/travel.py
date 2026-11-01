"""Travel facts: where the body actually went over the last executed choices.

S1 decides each turn without memory, so it cannot see that the body has not changed position along one axis
across hundreds of turns, and the live effect fact states only a displacement magnitude: up and back down reads
the same as forward. `TravelFacts` states the measurement: the total path the executed choices covered, the
body's actual position change across the window as a vector, and each choice's mean displacement vector. It never
says what to do: it does not rank, recommend, block, delay or open help, and S1 decides what to make of it.

The window is the last `_WINDOW` executed choices. That constant is a reporting window, not a threshold: it
selects what the fact is about and gates nothing but its own statement. A choice whose displacement was not
measured is counted as unmeasured, and left out of the totals and means: unknown, never zero. If the position a
choice started or ended at was not recorded (an older trace has only the magnitude), that part of the statement
reads unmeasured rather than leaving it out.

Readings are rounded to 0.001 before they are written, as `PatternFacts` does, so float noise such as a landing
0.0000015 above the ledge reads as 0.0. Positions from different play sessions are never compared: a rebind
(play-mode restart) starts the window over.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.core.ports import FactsSource, TurnReport
from playtest.preflight.entity import _sub, _vector

from .choices import UNIT

_PLACES = 3  # readings are compared and written at this many decimal places
_WINDOW = 20  # the executed choices the fact reports over; a reporting window, not a threshold


def _q(value: float) -> float:
    rounded = round(value, _PLACES)
    return 0.0 if rounded == 0 else rounded  # no "-0.0"


def _fmt(value: float) -> str:
    text = f"{value:.4g}"
    return text if "." in text or "e" in text else text + ".0"


def _num(value: Any) -> float | None:
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            else None)


def _signed(vector: tuple[float, float, float]) -> str:
    """A vector with a sign on every component, so a direction is never read as a distance."""
    return "(" + ", ".join(("+" if _q(v) >= 0 else "-") + _fmt(abs(_q(v))) for v in vector) + ")"


@dataclass(frozen=True)
class _Entry:
    """One executed choice in the window: what it was and what was measured of where the body went."""

    choice_id: str
    distance: float | None      # the measured net displacement, the path this choice covered
    step: tuple[float, float, float] | None      # its measured signed displacement
    start: tuple[float, float, float] | None     # the position it was measured from
    end: tuple[float, float, float] | None       # the position it was measured to

    @property
    def measured(self) -> bool:
        return self.distance is not None


def _entry(item: Any) -> _Entry:
    """One executed choice as the probe recorded it. Anything the report does not carry is unmeasured."""
    effect = getattr(item, "effect", None)
    observed = getattr(effect, "observed", None)
    observed = observed if isinstance(observed, Mapping) else {}
    return _Entry(choice_id=str(getattr(getattr(item, "choice", None), "id", "")),
                  distance=_num(observed.get("net_displacement")),
                  step=_vector(observed.get("displacement")),
                  start=_vector(observed.get("from_position")),
                  end=_vector(observed.get("to_position")))


def travel_line(entries: Sequence[_Entry]) -> str | None:
    """The window's measured travel as one line, or None when nothing in it was measured.

    Quantities only: the total path, the position change from the first measured start to the last measured end,
    and each choice's mean displacement vector, most executed first (ties by id). No recommendation, no rank, no
    word about what the measurements mean.
    """
    measured = [entry for entry in entries if entry.measured]
    if not measured:
        return None
    total = sum(entry.distance for entry in measured)
    first = next((entry for entry in measured if entry.start is not None), None)
    last = next((entry for entry in reversed(measured) if entry.end is not None), None)
    change = _sub(last.end, first.start) if first is not None and last is not None else None
    line = f"Over your last {len(entries)} executed choices the body moved {_fmt(_q(total))} {UNIT} in total; "
    line += (f"its position changed by {_signed(change)} {UNIT}." if change is not None
             else "its position change was not measured.")
    counts = Counter(entry.choice_id for entry in entries)
    steps: dict[str, list[tuple[float, float, float]]] = {}
    for entry in measured:
        if entry.step is not None:
            steps.setdefault(entry.choice_id, []).append(entry.step)
    rows = []
    for choice_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        vectors = steps.get(choice_id, [])
        label = f"{choice_id} x{count}"
        if not vectors:
            rows.append(f"{label}: mean displacement vector not measured")
            continue
        if len(vectors) < count:
            label += f" ({len(vectors)} with a measured vector)"
        mean = tuple(_q(sum(vector[axis] for vector in vectors) / len(vectors)) for axis in range(3))
        rows.append(f"{label}: mean {_signed(mean)}")
    line += " " + "; ".join(rows) + "."
    unmeasured = len(entries) - len(measured)
    if unmeasured:
        line += f" {unmeasured} of those choices {'was' if unmeasured == 1 else 'were'} unmeasured."
    return line


class TravelFacts:
    """Append the travel line to the inner source's facts; forward everything else unchanged."""

    def __init__(self, inner: FactsSource) -> None:
        self._inner = inner
        self._offset: int | None = 0

    async def facts(self, reports: Sequence[TurnReport], observation: Observation) -> list[str]:
        lines = list(await self._inner.facts(reports, observation))
        if self._offset is None or self._offset > len(reports):
            # A restart, or a history shorter than what was already counted: nothing before this point is
            # compared with what comes after it.
            self._offset = len(reports)
        entries = [_entry(item) for report in reports[self._offset:]
                   for item in report.executed if not item.execution.rejected]
        if sum(entry.measured for entry in entries) < _WINDOW:
            return lines  # fewer measured choices than the window: nothing is stated yet
        line = travel_line(entries[-_WINDOW:])
        if line is not None:
            lines.append(line)
        return lines

    def directive_view(self, observation: Observation):
        view = getattr(self._inner, "directive_view", None)
        return view(observation) if view is not None else None

    def finish(self, **values: Any) -> None:
        finish = getattr(self._inner, "finish", None)
        if finish is not None:
            finish(**values)

    def rebind(self, **values: Any) -> None:
        self._offset = None
        rebind = getattr(self._inner, "rebind", None)
        if rebind is not None:
            rebind(**values)
