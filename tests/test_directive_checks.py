from playtest.contracts.investigation import Comparator
from playtest.roles.checks import evaluate


def test_numeric_directive_checks_and_tolerance():
    assert evaluate(3.0, kind="number", op=Comparator.ge, expected=2.0) is True
    assert evaluate(1.0, kind="number", op=Comparator.ge, expected=2.0) is False
    assert evaluate(2.05, kind="number", op=Comparator.eq, expected=2.0, tolerance=0.1) is True


def test_missing_or_unscorable_directive_evidence_stays_unknown():
    assert evaluate(None, kind="number", op=Comparator.ge, expected=0.0) is None
    assert evaluate("3", kind="number", op=Comparator.ge, expected=0.0) is None
    assert evaluate(3.0, kind="number", op=Comparator.changed, baseline=None) is None


def test_change_checks_use_the_received_baseline():
    assert evaluate(4, kind="number", op=Comparator.changed, baseline=3) is True
    assert evaluate(3, kind="number", op=Comparator.unchanged, baseline=3) is True
