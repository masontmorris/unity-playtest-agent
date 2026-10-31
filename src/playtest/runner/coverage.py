"""Coverage: how often each executable choice has been sent so far in the run, stated as one fact.

S1 picks from the offered set every turn but sees only its last few turns, so it cannot tell which choices it
has tried and which it never has. `CoverageFacts` counts the inputs the bridge accepted, by choice id, over
every turn report of the run, and names the offered actions and skills that have none. It is a count, never a
recommendation: it does not rank, order by need or say which to pick, and S1 decides what to do with it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.core.ports import DirectiveView, FactsSource, TurnReport


def coverage_line(reports: Sequence[TurnReport], offered: Sequence[str]) -> str:
    """Accepted inputs by choice id, most sent first (ties by id), then the offered ids with none."""
    sent = Counter(item.choice.id for report in reports for item in report.executed if not item.execution.rejected)
    counts = ", ".join(f"{choice_id} {n}" for choice_id, n in sorted(sent.items(), key=lambda kv: (-kv[1], kv[0])))
    never = [choice_id for choice_id in offered if choice_id not in sent]
    return (f"Inputs sent so far this run, by choice: {counts or 'none'}. "
            f"Offered actions and skills not sent yet this run: {', '.join(never) if never else 'none'}.")


class CoverageFacts:
    """Append the coverage line to the inner source's facts; forward everything else unchanged."""

    def __init__(self, inner: FactsSource, offered: Sequence[str]) -> None:
        self._inner, self._offered = inner, list(offered)

    async def facts(self, reports: Sequence[TurnReport], observation: Observation) -> list[str]:
        lines = list(await self._inner.facts(reports, observation))
        lines.append(coverage_line(reports, self._offered))
        return lines

    def directive_view(self, observation: Observation) -> DirectiveView | None:
        view = getattr(self._inner, "directive_view", None)
        return view(observation) if view is not None else None

    def finish(self, **values: Any) -> None:
        finish = getattr(self._inner, "finish", None)
        if finish is not None:
            finish(**values)

    def rebind(self, **values: Any) -> None:
        rebind = getattr(self._inner, "rebind", None)
        if rebind is not None:
            rebind(**values)
