"""Effect probe: the calibrated entity's displacement around each input, against its calibrated value.

The reading before an input is taken at the top of the turn, before the turn's observation; the
reading after it right after the input's terminal record. Neither is ever taken between observing and
acting, so a reading adds nothing to the age of the observation an act is bound to. The probe reports
the expected (calibrated) and observed displacement and a fact line stating both, and, when the calibration
recorded a spread, the allowance around the calibrated value; it judges nothing (the watch compares them).
A reading that cannot be taken leaves the observed value `None`, never zero.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from playtest.contracts.loop import Choice
from playtest.core.bridge_client import BridgeClient
from playtest.core.execution import Execution
from playtest.core.ports import EffectComparison, EffectReport
from playtest.preflight.calibrate import _entity_snapshot
from playtest.preflight.entity import _norm, _sub, _vector
from playtest.preflight.verify import MEASUREMENT_TOLERANCE

from .choices import UNIT

METRIC = "net displacement"

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


async def read_snapshot(snapshot: Snapshot) -> tuple[dict[str, Any] | None, str | None]:
    """One reading, or why there is none: an incomplete reading or a missing entity is unmeasured, not zero."""
    try:
        return await snapshot(), None
    except ValueError as exc:
        return None, str(exc)


class DisplacementProbe:
    def __init__(self, snapshot: Snapshot, expected: Mapping[str, Mapping[str, Any] | None],
                 *, snapshot_for: Callable[[BridgeClient], Snapshot] | None = None) -> None:
        self._snapshot = snapshot
        self._expected = expected
        self._snapshot_for = snapshot_for
        self._before: dict[str, Any] | None = None
        self._before_problem: str | None = None

    def rebind(self, *, client: BridgeClient | None = None, observation: Any = None,
               generation: str | None = None) -> None:
        """Point at the restarted play instance; no pre-restart reading may be the 'before' of a new input."""
        if client is not None and self._snapshot_for is not None:
            self._snapshot = self._snapshot_for(client)
        self._before, self._before_problem = None, None

    async def before_turn(self, turn: int) -> None:
        self._before, self._before_problem = await self._read()

    async def measure(self, choice: Choice, execution: Execution) -> EffectReport:
        before, before_problem = self._before, self._before_problem
        after, after_problem = await self._read()
        expected = self._expected.get(choice.id)
        problem = before_problem or after_problem
        observed = displacement_between(before, after)
        if observed is not None:
            problem = problem or (None if observed["net_displacement"] is not None else "a position was unknown")
            observed["applied_duration_s"] = execution.terminal.applied_duration_s
        return EffectReport(
            expected=dict(expected) if expected is not None else None,
            observed=observed,
            fact=self._fact(choice, expected, observed, problem),
            comparison=_comparison(expected, observed),
        )

    async def _read(self) -> tuple[dict[str, Any] | None, str | None]:
        return await read_snapshot(self._snapshot)

    @staticmethod
    def _fact(choice: Choice, expected: Mapping[str, Any] | None, observed: Mapping[str, Any] | None,
              problem: str | None) -> str:
        if observed is None or observed["net_displacement"] is None:
            return f"{choice.id}: net displacement was not measured ({problem or 'no reading'})."
        text = (f"{choice.id}: net displacement between inspection seq {observed['from_inspection_seq']} "
                f"and {observed['to_inspection_seq']}: {observed['net_displacement']:.3f} {UNIT}")
        if expected is None:
            return text + "; no calibrated effect is recorded for this declaration."
        variance = expected["sample_variance"]
        return (f"{text}; calibrated for a {expected['requested_hold_s']:g}-second hold: "
                f"{expected['net_displacement']:.3f} (sample variance "
                f"{f'{variance:.6f}' if variance is not None else 'unknown'}, n={expected['valid_trials']}).")


def displacement_between(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The net displacement between two snapshots of the entity, or None when either snapshot was not taken.

    A snapshot whose position is unknown gives a `net_displacement` of None: unmeasured, never zero.
    """
    if before is None or after is None:
        return None
    start, end = _vector(before.get("position")), _vector(after.get("position"))
    return {
        "net_displacement": _norm(_sub(end, start)) if start is not None and end is not None else None,
        "unit": UNIT,
        "from_inspection_seq": before.get("inspection_seq"),
        "to_inspection_seq": after.get("inspection_seq"),
        "window_game_s": _gap(before.get("game_time_s"), after.get("game_time_s")),
    }


def allowance(expected: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """A calibrated displacement's spread and its allowance: how far a measured displacement may be from the
    calibrated one before the calibration's recorded spread no longer accounts for it.

    The allowance is the stage 7 formula (`playtest.preflight.verify.effect_tolerance`), fixed before any run: the
    square root of the calibration's sample variance plus its measurement allowance (two idle reads and the effect
    threshold). None when nothing is calibrated, or the calibration recorded no variance (one valid trial): the
    allowance is then unknown, not zero.
    """
    if expected is None:
        return None
    variance, calibrated = expected.get("sample_variance"), expected.get("net_displacement")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (variance, calibrated)) or variance < 0:
        return None
    spread = math.sqrt(variance)
    return spread, spread + MEASUREMENT_TOLERANCE


def _comparison(expected: Mapping[str, Any] | None, observed: Mapping[str, Any] | None) -> EffectComparison | None:
    """The observed displacement against the calibrated one, with how far apart they may be before the
    calibration's recorded spread no longer accounts for it (`allowance`).

    A calibration that recorded no variance (one valid trial) or an effect that was not measured
    has no comparison: that is unknown, not a match and not a mismatch.
    """
    if expected is None or observed is None or observed.get("net_displacement") is None:
        return None
    bounds = allowance(expected)
    seen = observed["net_displacement"]
    if bounds is None or isinstance(seen, bool) or not isinstance(seen, (int, float)) or not math.isfinite(seen):
        return None
    spread, allowed = bounds
    return EffectComparison(
        metric=METRIC, unit=UNIT, expected=float(expected["net_displacement"]), observed=float(seen),
        spread=spread, allowed=allowed,
        basis=f"the calibration's spread {spread:.4g} plus a measurement allowance of {MEASUREMENT_TOLERANCE:g}")


def _gap(start: Any, end: Any) -> float | None:
    numbers = [v for v in (start, end) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return end - start if len(numbers) == 2 and end >= start else None
