"""The adherence checker catches every way code can override S1."""

from __future__ import annotations

import pytest

from playtest.contracts.loop import TurnRecord
from playtest.core.adherence import adherence_rate, check_adherence


def turn(**kw) -> TurnRecord:
    base = {"turn": 0, "set_version": 1, "based_on_seq": 1, "raw_choice": "a", "effective_choice": "a",
            "executed_action": "a", "outcome": "executed"}
    return TurnRecord.model_validate({**base, **kw})


GOOD = [
    turn(),
    turn(outcome="rejected_invalid", raw_choice="zz", effective_choice=None, executed_action=None),
    turn(raw_choice="w", effective_choice="w", executed_action=None, outcome="finished"),
    turn(raw_choice="h", effective_choice="h", executed_action=None, outcome="escalated", while_pending={"mode": "wait"}),
    turn(raw_choice="h", effective_choice="h", executed_action="a", outcome="escalated",
         while_pending={"mode": "continue", "choice_id": "a"}),
]


@pytest.mark.parametrize("t", GOOD, ids=lambda t: t.outcome.value)
def test_faithful_turns_pass(t):
    assert check_adherence([t]) == []


BAD = {
    "substituted": turn(raw_choice="a", effective_choice="b", executed_action="b"),
    "executed_other": turn(executed_action="b"),
    "repaired_rejection": turn(outcome="rejected_invalid", raw_choice="zz", effective_choice="a", executed_action="a"),
    "converted_to_escalation": turn(raw_choice="a", effective_choice="h", executed_action=None, outcome="escalated",
                                    while_pending={"mode": "wait"}),
    "input_during_wait": turn(raw_choice="h", effective_choice="h", executed_action="a", outcome="escalated",
                              while_pending={"mode": "wait"}),
    "code_selected_continuation": turn(raw_choice="h", effective_choice="h", executed_action="b", outcome="escalated",
                                       while_pending={"mode": "continue", "choice_id": "a"}),
    "help_without_while_pending": turn(raw_choice="h", effective_choice="h", executed_action=None, outcome="escalated"),
    "executed_without_input": turn(executed_action=None),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_overrides_are_caught(name):
    assert check_adherence([BAD[name]]), name


def test_rate():
    assert adherence_rate([]) is None
    assert adherence_rate([turn(), BAD["substituted"]]) == 0.5
