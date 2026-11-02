"""Pattern facts: measured alternation in executed choices, and how declared readers moved across it.

S1 decides each turn without memory, so it cannot see that its last turns went A, B, A, B and that the
readers came back to where they were. `PatternFacts` states that, as a measurement: which two choices
alternated, over how many executed turns, and how each declared scalar reader moved across them, with its
net change. It never says what to do: it does not rank, recommend, block or delay a choice, and S1 decides
what to make of it (docs/design/oscillation.md).

Readings are rounded to 0.001 before they are compared or written. Float noise such as a heading of
3.4e-06 after turning back reads as 0.0, so an exact two-value cycle is stated as one.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

_PLACES = 3  # readings are compared and written at this many decimal places
_MIN_RUN = 4  # executed choices alternating before the fact is stated
_MAX_READERS = 3


def _q(value: float) -> float:
    rounded = round(value, _PLACES)
    return 0.0 if rounded == 0 else rounded  # no "-0.0"


def _fmt(value: float) -> str:
    text = f"{value:.4g}"
    return text if "." in text or "e" in text else text + ".0"


class PatternFacts:
    def __init__(self, inner, readers: Sequence[str]) -> None:
        self._inner, self._readers = inner, list(readers)
        self._history: list[dict[str, float]] = []
        self._offset: int | None = 0

    async def facts(self, reports, observation) -> list[str]:
        lines = list(await self._inner.facts(reports, observation))
        values = {name: _q(value) for name in self._readers
                  if isinstance((value := observation.public.get(name)), (int, float))
                  and not isinstance(value, bool) and math.isfinite(value)}
        if self._offset is None:
            self._offset = len(reports)
        reports = reports[self._offset:]
        # Reports are indexed by completed turns; the current observation precedes the next turn.
        self._history = self._history[:len(reports)]
        self._history.append(values)
        executed = [(i, item.choice.id) for i, report in enumerate(reports)
                    for item in report.executed if not item.execution.rejected]
        if len(executed) < _MIN_RUN or executed[-1][1] == executed[-2][1]:
            return lines
        start = len(executed) - 2
        while start > 0 and executed[start - 1][1] == executed[start + 1][1]:
            start -= 1
        run = executed[start:]
        if len(run) < _MIN_RUN:
            return lines
        line = f"Your last {len(run)} executed choices alternated between {run[0][1]} and {run[1][1]}."
        candidates = []
        indices = [i for i, _ in run] + [len(reports)]
        for name in self._readers:
            if any(i >= len(self._history) or name not in self._history[i] for i in indices):
                continue
            readings = [self._history[i][name] for i in indices]
            change = max(readings) - min(readings)
            if change:
                candidates.append((-change, name, readings))
        for _, name, readings in sorted(candidates)[:_MAX_READERS]:
            previous, now = readings[:-1], readings[-1]
            if (len(previous) >= 6 and len(set(previous)) == 2
                    and all(previous[i] == previous[i % 2] for i in range(len(previous)))):
                description = f"alternated {_fmt(previous[0])} and {_fmt(previous[1])} ({len(previous)} readings)"
            elif len(previous) > 8:  # a long, irregular sequence is stated as its range
                description = f"ranged {_fmt(min(previous))} to {_fmt(max(previous))} over {len(previous)} readings"
            else:
                description = "read " + ", ".join(map(_fmt, previous))
            line += (f" Over those turns public.{name} {description} and now {_fmt(now)} "
                     f"(net change {_fmt(_q(now - previous[0]))}).")
        lines.append(line)
        return lines

    def directive_view(self, observation):
        view = getattr(self._inner, "directive_view", None)
        return view(observation) if view is not None else None

    def finish(self, **values: Any) -> None:
        finish = getattr(self._inner, "finish", None)
        if finish is not None:
            finish(**values)

    def rebind(self, **values: Any) -> None:
        self._history.clear()
        self._offset = None
        rebind = getattr(self._inner, "rebind", None)
        if rebind is not None:
            rebind(**values)
