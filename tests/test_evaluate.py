"""Check evaluation: the shared single-check evaluator, and manifest invariants scored with it.

`playtest.roles.checks.evaluate` is the one place a check (a field, a comparator, a value) is scored; directive
setpoints use it too. Its answer is three-valued and never guesses from a missing reading. `evaluate_invariant`
(`playtest.core.evaluate`) scores a whole manifest invariant by calling it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playtest.contracts.investigation import Comparator
from playtest.contracts.manifest import ManifestInvariant
from playtest.core import evaluate as scoring
from playtest.core.evaluate import evaluate_invariant, same
from playtest.roles.checks import evaluate

EQ, NE, LT, LE, GT, GE = Comparator.eq, Comparator.ne, Comparator.lt, Comparator.le, Comparator.gt, Comparator.ge
CHANGED, UNCHANGED = Comparator.changed, Comparator.unchanged


def rule(kind: str, **fields) -> ManifestInvariant:
    base = dict(kind=kind, name="rule", source="test", catches="a test case", field=None, direction=None, min=None,
                max=None, when=None, then=None, unless=None)
    return ManifestInvariant.model_validate({**base, **fields})


def check(field: str, op: str, value=None) -> dict:
    return {"field": field, "op": op, "value": value}


KINDS = {"count": "number", "paused": "bool", "moved": "vector", "mystery": None}


# --- one check: the shared evaluator --------------------------------------------------------


@pytest.mark.parametrize("op, observed, expected, result", [
    (LT, 1, 2, True), (LT, 2, 2, False), (LE, 2, 2, True), (GT, 3, 2, True), (GT, 2, 2, False), (GE, 2, 2, True),
    (EQ, 2.0, 2, True), (NE, 2.0, 2, False), (EQ, 2.5, 2, False), (NE, 2.5, 2, True),
])
def test_numbers_are_compared_by_value(op, observed, expected, result):
    assert evaluate(observed, kind="number", op=op, expected=expected) is result


def test_a_tolerance_applies_to_numeric_equality_only():
    assert evaluate(2.04, kind="number", op=EQ, expected=2.0, tolerance=0.05) is True
    assert evaluate(2.04, kind="number", op=NE, expected=2.0, tolerance=0.05) is False
    assert evaluate(2.1, kind="number", op=EQ, expected=2.0, tolerance=0.05) is False
    assert evaluate(1.0, kind="number", op=LT, expected=2.0, tolerance=0.05) is None     # not scorable as written: unknown


def test_bool_and_string_compare_only_with_their_own_type():
    assert evaluate(True, kind="bool", op=EQ, expected=True) is True and evaluate(True, kind="bool", op=NE, expected=True) is False
    assert evaluate("a", kind="string", op=EQ, expected="a") is True and evaluate("a", kind="string", op=EQ, expected="b") is False
    assert evaluate(1, kind="bool", op=EQ, expected=True) is None                        # a number is not a bool reading
    assert evaluate(True, kind="string", op=EQ, expected="yes") is None


def test_a_missing_reading_is_unknown_never_a_failure_or_a_pass():
    for op, expected in ((LT, 1), (EQ, 1), (NE, 1), (GE, 0)):
        assert evaluate(None, kind="number", op=op, expected=expected) is None
    assert evaluate(None, kind="number", op=CHANGED, baseline=1) is None
    assert evaluate(None, kind="number", op=UNCHANGED, baseline=1) is None


def test_changed_and_unchanged_need_a_measured_baseline():
    assert evaluate(2, kind="number", op=CHANGED) is None                                # no baseline supplied
    assert evaluate(2, kind="number", op=CHANGED, baseline=None) is None                 # supplied, but nobody measured it
    assert evaluate(2, kind="number", op=CHANGED, baseline=1) is True
    assert evaluate(2, kind="number", op=UNCHANGED, baseline=2.0) is True
    assert evaluate([0, 0, 1.2], kind="vector", op=UNCHANGED, baseline=[0, 0, 1.2]) is True
    assert evaluate([0, 0, 1.2], kind="vector", op=CHANGED, baseline=[0, 0, 1.3]) is True
    assert evaluate([0, 0], kind="vector", op=CHANGED, baseline=[0, 0, 0]) is True


def test_a_vector_or_other_field_supports_only_changed_and_unchanged():
    assert evaluate([1, 2, 3], kind="vector", op=GT, expected=1) is None
    assert evaluate([1, 2, 3], kind="vector", op=EQ, expected=1) is None
    assert evaluate(1, kind="other", op=GT, expected=0) is None


def test_a_check_that_is_not_scorable_as_written_is_unknown():
    assert evaluate(3, kind="number", op=GT, expected=None) is None                      # needs a value
    assert evaluate(3, kind="number", op=CHANGED, expected=1, baseline=2) is None        # takes no value
    assert evaluate("a", kind="number", op=LT, expected=2) is None                       # a string is not a numeric reading
    assert evaluate(float("nan"), kind="number", op=LT, expected=2) is None              # nor is a number nobody could measure
    assert evaluate(float("inf"), kind="number", op=EQ, expected=2) is None


def test_same_compares_values_not_types_that_merely_equal():
    assert same(1, 1.0) and not same(1, True) and not same(True, 1) and same(True, True)
    assert not same("1", 1) and same([1, [2.0]], [1.0, [2]]) and not same([1], [1, 1])
    assert same({"a": 1}, {"a": 1.0}) and not same({"a": 1}, {"b": 1})
    assert not same(None, None)                                            # nothing measured on either side is not "same"


def test_the_core_scores_a_check_with_the_shared_evaluator_and_defines_no_comparator_of_its_own(monkeypatch):
    """One evaluator: the core's invariant scoring calls `roles.checks.evaluate`, and has no comparator logic to drift."""
    source = Path(scoring.__file__).read_text(encoding="utf-8")
    assert not hasattr(scoring, "holds") and not hasattr(scoring, "lookup")
    assert not [token for token in ("Comparator.", "ORDERING", "check_problems") if token in source]

    seen = []

    def spy(value, **kwargs):
        seen.append((value, kwargs))
        return True

    monkeypatch.setattr(scoring, "evaluate", spy)
    implies = rule("implies", when=check("paused", "==", True), then=check("moved", "unchanged"),
                   unless=check("count", ">=", 100))
    result = evaluate_invariant(implies, {"paused": True, "moved": [0, 0, 1.0], "count": 3},
                                {"paused": False, "moved": [0, 0, 0.5], "count": 2}, KINDS)
    assert result.holds is True
    assert seen[0] == (True, {"kind": "bool", "op": EQ, "expected": True, "baseline": False})
    assert seen[1] == ([0, 0, 1.0], {"kind": "vector", "op": UNCHANGED, "expected": None, "baseline": [0, 0, 0.5]})


# --- manifest invariants --------------------------------------------------------------------


def test_a_range_invariant_is_violated_only_by_a_measured_value_outside_it():
    r = rule("range", field="count", min=0.0, max=5.0)
    assert evaluate_invariant(r, {"count": 3}, None, KINDS).holds is True
    assert evaluate_invariant(r, {"count": 0}, None, KINDS).holds is True and evaluate_invariant(r, {"count": 5}, None, KINDS).holds is True
    above = evaluate_invariant(r, {"count": 7}, None, KINDS)
    assert above.holds is False and above.readings == {"count": 7} and "above the maximum 5" in above.reason
    below = evaluate_invariant(r, {"count": -2.5}, None, KINDS)
    assert below.holds is False and "below the minimum 0" in below.reason
    assert evaluate_invariant(r, {"count": None}, None, KINDS).holds is None
    assert evaluate_invariant(r, {}, None, KINDS).holds is None
    assert evaluate_invariant(r, {"count": 99}, None, {"count": "string"}).holds is None   # not a numeric field
    assert evaluate_invariant(r, {"count": 99}, None, {}).holds is None                    # kind unknown: not guessed
    assert evaluate_invariant(rule("range", field="count", min=None, max=None), {"count": 9}, None, KINDS).holds is None


def test_a_one_sided_range_has_one_bound():
    assert evaluate_invariant(rule("range", field="count", min=0.0), {"count": 1e9}, None, KINDS).holds is True
    assert evaluate_invariant(rule("range", field="count", max=0.0), {"count": 1e9}, None, KINDS).holds is False


def test_a_monotonic_invariant_compares_with_the_reading_before():
    up = rule("monotonic", field="count", direction="non_decreasing")
    down = rule("monotonic", field="count", direction="non_increasing")
    assert evaluate_invariant(up, {"count": 3}, {"count": 3}, KINDS).holds is True
    fell = evaluate_invariant(up, {"count": 2}, {"count": 3}, KINDS)
    assert fell.holds is False and fell.baselines == {"count": 3} and "fell from 3 to 2" in fell.reason
    rose = evaluate_invariant(down, {"count": 4}, {"count": 3}, KINDS)
    assert rose.holds is False and "rose from 3 to 4" in rose.reason
    assert evaluate_invariant(up, {"count": 2}, None, KINDS).holds is None                  # no reading before
    assert evaluate_invariant(up, {"count": 2}, {"count": None}, KINDS).holds is None      # the one before was not measured
    assert evaluate_invariant(up, {"count": None}, {"count": 3}, KINDS).holds is None


def paused_freezes_move() -> ManifestInvariant:
    return rule("implies", when=check("paused", "==", True), then=check("moved", "unchanged"))


def test_an_implies_invariant_needs_its_condition_true_and_its_consequence_false_to_be_violated():
    r = paused_freezes_move()
    moving = {"paused": True, "moved": [0, 0, 1.2]}
    still = {"paused": True, "moved": [0, 0, 1.0]}
    violated = evaluate_invariant(r, still, moving, KINDS)
    assert violated.holds is False and violated.readings == {"paused": True, "moved": [0, 0, 1.0]}
    assert violated.baselines == {"moved": [0, 0, 1.2]} and "paused == True held" in violated.reason
    assert evaluate_invariant(r, moving, moving, KINDS).holds is True                      # the consequence holds
    assert evaluate_invariant(r, {"paused": False, "moved": [9, 9, 9]}, moving, KINDS).holds is True   # the condition is false


def test_an_implies_invariant_is_unknown_when_a_reading_it_needs_is_unknown():
    r = paused_freezes_move()
    assert evaluate_invariant(r, {"paused": None, "moved": [1, 2, 3]}, {"moved": [1, 2, 3]}, KINDS).holds is None
    assert evaluate_invariant(r, {"paused": True, "moved": None}, {"moved": [1, 2, 3]}, KINDS).holds is None
    assert evaluate_invariant(r, {"paused": True, "moved": [1, 2, 3]}, None, KINDS).holds is None   # first turn: no baseline
    assert evaluate_invariant(r, {"paused": True, "moved": [1, 2, 3]}, {"moved": None}, KINDS).holds is None


def test_an_exception_exempts_a_violation_and_an_unknown_one_leaves_it_unknown():
    r = rule("implies", when=check("paused", "==", True), then=check("count", "<", 3),
             unless=check("count", ">=", 100))
    assert evaluate_invariant(r, {"paused": True, "count": 4}, None, KINDS).holds is False
    assert evaluate_invariant(r, {"paused": True, "count": 400}, None, KINDS).holds is True       # excepted
    assert evaluate_invariant(r, {"paused": True, "count": 2}, None, KINDS).holds is True
    unsure = ManifestInvariant.model_validate(r.model_dump(mode="json") | {"unless": check("mystery", ">=", 1)})
    assert evaluate_invariant(unsure, {"paused": True, "count": 4, "mystery": 5}, None, KINDS).holds is None   # kind unknown
    assert evaluate_invariant(r, {"paused": True, "count": None}, None, KINDS).holds is None


def test_an_exception_excuses_a_range_violation_and_an_unknown_one_leaves_it_unknown():
    r = rule("range", field="count", min=0, max=10, unless=check("paused", "==", True))
    assert evaluate_invariant(r, {"count": 20, "paused": False}, None, KINDS).holds is False
    excused = evaluate_invariant(r, {"count": 20, "paused": True}, None, KINDS)
    assert excused.holds is True and excused.readings == {"count": 20, "paused": True}
    assert evaluate_invariant(r, {"count": 5, "paused": None}, None, KINDS).holds is True    # holds: not consulted
    unsure = evaluate_invariant(r, {"count": 20, "paused": None}, None, KINDS)
    assert unsure.holds is None and unsure.reason == "the exception on paused cannot be scored"
    violated = evaluate_invariant(r, {"count": -1, "paused": False}, None, KINDS)
    assert violated.reason == "count is -1, below the minimum 0" and violated.readings["paused"] is False


def test_an_exception_excuses_a_monotonic_violation_against_the_previous_sample():
    m = rule("monotonic", field="count", direction="non_decreasing", unless=check("moved", "changed"))
    before = {"count": 5, "moved": [0, 0, 0]}
    assert evaluate_invariant(m, {"count": 2, "moved": [0, 0, 0]}, before, KINDS).holds is False
    reset = evaluate_invariant(m, {"count": 2, "moved": [1, 0, 0]}, before, KINDS)
    assert reset.holds is True and reset.baselines == {"count": 5, "moved": [0, 0, 0]}
    assert evaluate_invariant(m, {"count": 2, "moved": None}, before, KINDS).holds is None
    assert evaluate_invariant(m, {"count": 7, "moved": None}, before, KINDS).holds is True   # holds: not consulted
    ranged = rule("range", field="count", max=10, unless=check("moved", "changed"))
    assert evaluate_invariant(ranged, {"count": 20, "moved": [1, 0, 0]}, before, KINDS).holds is True
    assert evaluate_invariant(ranged, {"count": 20, "moved": [1, 0, 0]}, None, KINDS).holds is None  # no baseline


def test_a_rule_missing_a_part_is_unknown():
    assert evaluate_invariant(rule("implies", when=check("paused", "==", True)), {"paused": True}, None, KINDS).holds is None
    assert evaluate_invariant(rule("monotonic", field="count"), {"count": 1}, {"count": 2}, KINDS).holds is None
