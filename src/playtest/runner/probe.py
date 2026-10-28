"""Effect probe: the calibrated entity's displacement around each input, against its calibrated value.

The reading before an input is taken at the top of the turn, before the turn's observation; the
reading after it right after the input's terminal record. Neither is ever taken between observing and
acting, so a reading adds nothing to the age of the observation an act is bound to. The probe reports
the expected (calibrated) and observed displacement and a fact line stating both; it judges nothing.
A reading that cannot be taken leaves the observed value `None`, never zero.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from playtest.contracts.loop import Choice
from playtest.core.bridge_client import BridgeClient
from playtest.core.execution import Execution
from playtest.core.ports import EffectReport
from playtest.preflight.calibrate import _entity_snapshot
from playtest.preflight.entity import _norm, _sub, _vector

from .choices import UNIT

Snapshot = Callable[[], Awaitable[dict[str, Any]]]


def make_snapshot(client: BridgeClient, entity_id: str) -> Snapshot:
    """One `inspect("transforms")` reading of the entity, stamped with the inspection's `seq`.

    Raises `ValueError` when the reading is incomplete or the entity is not in it; a closed bridge raises `BridgeError`.
    """

    async def snapshot() -> dict[str, Any]:
        response = await client.inspect("transforms")
        reading = _entity_snapshot(response, entity_id)
        reading["inspection_seq"] = response.seq
        return reading

    return snapshot


class DisplacementProbe:
    def __init__(self, snapshot: Snapshot, expected: Mapping[str, Mapping[str, Any] | None]) -> None:
        self._snapshot = snapshot
        self._expected = expected
        self._before: dict[str, Any] | None = None
        self._before_problem: str | None = None

    async def before_turn(self, turn: int) -> None:
        self._before, self._before_problem = await self._read()

    async def measure(self, choice: Choice, execution: Execution) -> EffectReport:
        before, before_problem = self._before, self._before_problem
        after, after_problem = await self._read()
        expected = self._expected.get(choice.id)
        problem = before_problem or after_problem
        distance = None
        if before is not None and after is not None:
            start, end = _vector(before.get("position")), _vector(after.get("position"))
            distance = _norm(_sub(end, start)) if start is not None and end is not None else None
            problem = problem or (None if distance is not None else "a position was unknown")
        observed = None
        if before is not None and after is not None:
            observed = {
                "net_displacement": distance,
                "unit": UNIT,
                "from_inspection_seq": before.get("inspection_seq"),
                "to_inspection_seq": after.get("inspection_seq"),
                "window_game_s": _gap(before.get("game_time_s"), after.get("game_time_s")),
                "applied_duration_s": execution.terminal.applied_duration_s,
            }
        return EffectReport(
            expected=dict(expected) if expected is not None else None,
            observed=observed,
            fact=self._fact(choice, expected, observed, problem),
        )

    async def _read(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            return await self._snapshot(), None
        except ValueError as exc:  # an incomplete reading or a missing entity is unmeasured, not zero
            return None, str(exc)

    @staticmethod
    def _fact(choice: Choice, expected: Mapping[str, Any] | None, observed: Mapping[str, Any] | None,
              problem: str | None) -> str:
        if observed is None or observed["net_displacement"] is None:
            return f"{choice.id}: net displacement was not measured ({problem or 'no reading'})."
        text = (f"{choice.id}: net displacement between inspection seq {observed['from_inspection_seq']} "
                f"and {observed['to_inspection_seq']}: {observed['net_displacement']:.3f} {UNIT}")
        if expected is None:
            return text + "; no calibrated value for this hold."
        variance = expected["sample_variance"]
        return (f"{text}; calibrated for a {expected['requested_hold_s']:g}-second hold: "
                f"{expected['net_displacement']:.3f} (sample variance "
                f"{f'{variance:.6f}' if variance is not None else 'unknown'}, n={expected['valid_trials']}).")


def _gap(start: Any, end: Any) -> float | None:
    numbers = [v for v in (start, end) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return end - start if len(numbers) == 2 and end >= start else None
