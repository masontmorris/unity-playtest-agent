"""Measured session limits for S1; availability facts never select or suppress choices."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.core.facts import RecordFacts
from playtest.core.ports import FactsSource, TurnReport

PREFIX = "Session resources: "


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _calls(used: Any, cap: Any, *, configured: bool = True) -> dict[str, Any]:
    used, cap = _count(used), _count(cap)
    remaining = max(0, cap - used) if cap is not None and used is not None else None
    state = ("not_configured" if not configured else "unknown" if remaining is None else
             "call_cap_exhausted" if remaining == 0 else "within_call_cap")
    return {"reserved": used, "cap": cap, "remaining": remaining, "state": state}


class SessionFacts:
    """Expose wall time and actual reservation counts alongside the existing facts.

    A remaining call slot is only a count: it does not assert a balance, cost allowance,
    provider availability or a pending result's readiness. Missing counters stay null.
    No elapsed time, count or availability state here opens help or ends a session.
    """

    def __init__(self, *, started: float, wall_limit_s: float, clock: Callable[[], float],
                 s1: Any, s1_call_cap: int | None, help: Any = None,
                 inner: FactsSource | None = None) -> None:
        self._started, self._wall_limit_s, self._clock = started, wall_limit_s, clock
        self._s1, self._s1_cap, self._help = s1, s1_call_cap, help
        self._inner = inner if inner is not None else RecordFacts()

    def snapshot(self) -> dict[str, Any]:
        elapsed = self._clock() - self._started
        elapsed = max(0.0, elapsed) if math.isfinite(elapsed) else None
        return {
            "wall_s": {"elapsed": elapsed, "limit": self._wall_limit_s,
                       "remaining": max(0.0, self._wall_limit_s - elapsed) if elapsed is not None else None},
            "s1_calls": _calls(getattr(self._s1, "calls_reserved", None), self._s1_cap),
            "frontier_calls": (_calls(0, 0, configured=False) if self._help is None else
                               _calls(getattr(self._help, "calls", None), getattr(self._help, "max_calls", None))),
        }

    async def facts(self, reports: Sequence[TurnReport], observation: Observation) -> list[str]:
        lines = list(await self._inner.facts(reports, observation))
        lines.append(PREFIX + json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"), allow_nan=False))
        return lines
