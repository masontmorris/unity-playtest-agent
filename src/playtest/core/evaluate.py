"""Score a manifest invariant over measured readings (build bible 5.6).

One check (a field, a comparator, a value) is scored in one place: `playtest.roles.checks.evaluate`. Directive
setpoints, preconditions, invariants and finding predicates all go through it, so every role scores a check the
same way. Its answer is three-valued: `True`, `False`, or `None` for unknown. A reading nobody measured, a
baseline nobody measured and a check that cannot be scored as written are unknown, never a pass and never a
failure (measured, not inferred). Nothing here decides what to do about an answer.

This module holds only what that function does not: `evaluate_invariant` scores a whole manifest invariant
(`range`, `monotonic`, `implies`) over a reading and the one before it, and two small helpers for the watch.
It defines no comparator of its own.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from playtest.contracts.manifest import ManifestCheck, ManifestInvariant
from playtest.roles.checks import CHANGE, ValueKind, evaluate


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def same(a: Any, b: Any) -> bool:
    """Whether two readings are the same value: numbers by value, booleans only with booleans, lists element by element.

    The watch uses it to tell whether a flag or a string was seen before; it is not how a check scores `changed`.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if is_number(a) and is_number(b):
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(same(a[key], b[key]) for key in a)
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return False


# --- invariants ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class InvariantResult:
    """`holds` is `False` when the rule is violated by these readings, `None` when they cannot say.

    `readings` are the current values of the fields the rule names and `baselines` the earlier values it
    compared them with; a value nobody measured is `None`. `reason` says why a rule is unknown or what was
    measured when it is violated.
    """

    holds: bool | None
    reason: str | None = None
    readings: dict[str, Any] = field(default_factory=dict)
    baselines: dict[str, Any] = field(default_factory=dict)


def _kind(kinds: Mapping[str, ValueKind | None], alias: str) -> ValueKind:
    return kinds.get(alias) or "other"


def evaluate_invariant(rule: ManifestInvariant, now: Mapping[str, Any], before: Mapping[str, Any] | None,
                       kinds: Mapping[str, ValueKind | None]) -> InvariantResult:
    """Score one manifest invariant over the readings `now` (by reader alias) and the ones `before` them.

    `before` is the previous observation's readings, or `None` on the first. A rule that names a reading
    nobody measured is unknown; it is violated only when every reading it needs was measured and breaks it.
    """
    if rule.kind == "range":
        return _range(rule, now, kinds)
    if rule.kind == "monotonic":
        return _monotonic(rule, now, before, kinds)
    return _implies(rule, now, before, kinds)


def _range(rule: ManifestInvariant, now: Mapping[str, Any], kinds: Mapping[str, ValueKind | None]) -> InvariantResult:
    name = rule.field
    if name is None or (rule.min is None and rule.max is None):
        return InvariantResult(None, "the rule names no field or no bound")
    value = now.get(name)
    readings = {name: value}
    if _kind(kinds, name) != "number":
        return InvariantResult(None, f"{name} is not a numeric field", readings)
    if not is_number(value):
        return InvariantResult(None, f"{name} has no measured value", readings)
    below = rule.min is not None and value < rule.min
    above = rule.max is not None and value > rule.max
    if below or above:
        return InvariantResult(False, f"{name} is {value:.6g}, {'below the minimum' if below else 'above the maximum'} "
                                      f"{(rule.min if below else rule.max):.6g}", readings)
    return InvariantResult(True, None, readings)


def _monotonic(rule: ManifestInvariant, now: Mapping[str, Any], before: Mapping[str, Any] | None,
               kinds: Mapping[str, ValueKind | None]) -> InvariantResult:
    name = rule.field
    if name is None or rule.direction is None:
        return InvariantResult(None, "the rule names no field or no direction")
    value, earlier = now.get(name), (before.get(name) if before is not None else None)
    readings, baselines = {name: value}, {name: earlier}
    if _kind(kinds, name) != "number":
        return InvariantResult(None, f"{name} is not a numeric field", readings, baselines)
    if not is_number(value) or not is_number(earlier):
        return InvariantResult(None, f"{name} has no measured value now or before", readings, baselines)
    broken = value < earlier if rule.direction == "non_decreasing" else value > earlier
    if broken:
        way = "fell" if rule.direction == "non_decreasing" else "rose"
        return InvariantResult(False, f"{name} {way} from {earlier:.6g} to {value:.6g}", readings, baselines)
    return InvariantResult(True, None, readings, baselines)


def _implies(rule: ManifestInvariant, now: Mapping[str, Any], before: Mapping[str, Any] | None,
             kinds: Mapping[str, ValueKind | None]) -> InvariantResult:
    if rule.when is None or rule.then is None:
        return InvariantResult(None, "the rule lacks a condition or a consequence")
    checks = [("when", rule.when), ("then", rule.then)] + ([("unless", rule.unless)] if rule.unless is not None else [])
    readings = {check.field: now.get(check.field) for _, check in checks}
    baselines = {check.field: before.get(check.field) if before is not None else None
                 for _, check in checks if check.op in CHANGE}

    def score(check: ManifestCheck) -> bool | None:
        baseline = before.get(check.field) if before is not None else None
        return evaluate(now.get(check.field), kind=_kind(kinds, check.field), op=check.op, expected=check.value,
                        baseline=baseline)

    antecedent = score(rule.when)
    if antecedent is None:
        return InvariantResult(None, f"the condition on {rule.when.field} cannot be scored", readings, baselines)
    if antecedent is False:
        return InvariantResult(True, None, readings, baselines)
    consequence = score(rule.then)
    if consequence is True:
        return InvariantResult(True, None, readings, baselines)
    if consequence is None:
        return InvariantResult(None, f"the consequence on {rule.then.field} cannot be scored", readings, baselines)
    exempt = score(rule.unless) if rule.unless is not None else False
    if exempt is None:
        return InvariantResult(None, f"the exception on {rule.unless.field} cannot be scored", readings, baselines)
    if exempt:
        return InvariantResult(True, None, readings, baselines)
    return InvariantResult(False, f"{_describe(rule.when)} held, and {_describe(rule.then)} did not", readings, baselines)


def _describe(check: ManifestCheck) -> str:
    if check.op in CHANGE:
        return f"{check.field} {check.op.value}"
    return f"{check.field} {check.op.value} {check.value!r}"
