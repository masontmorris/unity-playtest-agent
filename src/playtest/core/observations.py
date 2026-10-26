"""Observation store keyed by `seq`.

Missing evidence stays `None`: an empty store reports no latest reading,
no age and no clock, and unknown readers or fields read as `None`, never
zero, success or failure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from playtest.contracts.bridge import Observation

_MISSING = object()


class ObservationStore:
    def __init__(self) -> None:
        self._by_seq: dict[int, Observation] = {}
        self._latest: Observation | None = None

    def add(self, observation: Observation) -> Observation:
        self._by_seq[observation.seq] = observation
        if self._latest is None or observation.seq >= self._latest.seq:
            self._latest = observation
        return observation

    def get(self, seq: int) -> Observation | None:
        return self._by_seq.get(seq)

    def latest(self) -> Observation | None:
        return self._latest

    @property
    def age_s(self) -> float | None:
        """Wall-clock age of the latest reading, or `None` when empty."""
        if self._latest is None:
            return None
        wall = self._latest.wall_utc
        if wall.tzinfo is None:
            wall = wall.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - wall).total_seconds())

    @property
    def game_s(self) -> float | None:
        if self._latest is None:
            return None
        return self._latest.game_s

    @property
    def wall_utc(self) -> datetime | None:
        if self._latest is None:
            return None
        return self._latest.wall_utc

    def reader(self, name: str) -> Any | None:
        """One entry of the latest `public` payload, or `None` when the
        reader is missing."""
        if self._latest is None:
            return None
        return self._latest.public.get(name)

    def field(self, dotted: str) -> Any | None:
        """Dotted lookup from `public` (for example `entity.pos`), or
        `None` when anything along the path is missing."""
        if self._latest is None:
            return None
        node: Any = self._latest.public
        for part in dotted.split("."):
            if not isinstance(node, dict) or node.get(part, _MISSING) is _MISSING:
                return None
            node = node[part]
        return node

    def __len__(self) -> int:
        return len(self._by_seq)
