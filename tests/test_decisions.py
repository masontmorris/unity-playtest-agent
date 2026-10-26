"""S1 answers are validated, never repaired (law 1)."""

from __future__ import annotations

import pytest

from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceSet
from playtest.core.decisions import parse_decision

SET = ChoiceSet.model_validate(
    {
        "set_version": 5,
        "built_from_seq": 100,
        "choices": [
            {"id": "a", "kind": "action", "label": "A", "action": "act_a", "source": "manifest"},
            {"id": "h", "kind": "escalate", "label": "Help", "role": "frontier", "source": "core"},
            {"id": "w", "kind": "wait", "label": "Wait", "source": "core"},
            {"id": "f", "kind": "finish", "label": "Finish", "source": "core"},
        ],
    }
)
RT = ClockMode.realtime


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "a",
        {"set_version": 5},
        {"choice_id": "zz", "set_version": 5},
        {"choice_id": "a", "set_version": 4},
    ],
)
def test_invalid_answers_are_rejected_not_repaired(raw):
    p = parse_decision(raw, SET, RT)
    assert not p.valid and p.decision is None and p.error


def test_distribution_is_kept_but_does_not_select():
    p = parse_decision({"choice_id": "w", "set_version": 5, "distribution": {"a": 0.9, "w": 0.1}}, SET, RT)
    assert p.decision.choice_id == "w"


def test_valid_continue_is_kept():
    raw = {"choice_id": "h", "set_version": 5, "while_pending": {"mode": "continue", "choice_id": "a"}}
    p = parse_decision(raw, SET, RT)
    assert p.decision.while_pending.choice_id == "a" and not p.policy_default


@pytest.mark.parametrize(
    "secondary",
    [None, "go", {"mode": "continue"}, {"mode": "continue", "choice_id": "zz"}, {"mode": "continue", "choice_id": "h"},
     {"mode": "continue", "choice_id": "f"}],
)
def test_bad_while_pending_defaults_to_wait_without_invalidating_primary(secondary):
    p = parse_decision({"choice_id": "h", "set_version": 5, "while_pending": secondary}, SET, RT)
    assert p.valid and p.decision.choice_id == "h"
    assert p.decision.while_pending.mode == "wait" and p.policy_default


def test_while_pending_ignored_in_stepped_mode_and_on_non_help_turns():
    assert parse_decision({"choice_id": "h", "set_version": 5}, SET, ClockMode.stepped).decision.while_pending is None
    p = parse_decision({"choice_id": "a", "set_version": 5, "while_pending": {"mode": "wait"}}, SET, RT)
    assert p.decision.while_pending is None and not p.policy_default
