"""The live frontier gets S1's recorded attempts, including failures across intervening waits."""

from __future__ import annotations

import pytest

from playtest.runner.frontier_history import recent_records


def context():
    return {"as_of": {"seq": 900}, "recent_history": {
        "epoch": 1, "boundary": {"trace_seq": 300, "observation_seq": 700},
        "turns": {"limit": 8, "total": 10, "omitted": 2,
                  "items": [{"turn": 12, "observation_seq": 800, "trace_seq": 400,
                             "outcome": "finished", "raw_choice": "wait"}]},
        "action_attempts": {"limit": 8, "total": 2, "omitted": 0,
                            "items": [{"turn": 3, "observation_seq": 710, "trace_seq": 305,
                                       "choice_id": "a", "terminal_status": "rejected",
                                       "reject_reason": "stale_age", "observed_effect": None},
                                      {"turn": 4, "observation_seq": 720, "trace_seq": 315,
                                       "choice_id": "b", "terminal_status": "completed",
                                       "observed_effect": {"net_displacement": 0.1},
                                       "expected_effect": {"net_displacement": 2.0},
                                       "effect_trace_seq": 316}]}}}


def test_exact_attempts_and_unknowns_survive_forwarding_with_correct_sequence_kind():
    original = context()
    records = recent_records(original)
    summary, rejected, completed, wait = records
    assert summary["seq"] == 900 and summary["windows"]["turns"]["omitted"] == 2
    assert summary["boundary"] == original["recent_history"]["boundary"]
    assert [row["turn"] for row in records[1:]] == [3, 4, 12]
    assert rejected["seq"] == 710 and rejected["trace_seq"] == 305
    assert rejected["observed_effect"] is None and rejected["reject_reason"] == "stale_age"
    assert completed["seq"] == 720 and completed["effect_trace_seq"] == 316
    assert completed["observed_effect"] == {"net_displacement": 0.1}
    assert wait["raw_choice"] == "wait"
    completed["observed_effect"].clear()
    summary["boundary"].clear()
    assert original == context()  # the request owns its snapshot
    assert recent_records({"as_of": {"seq": 900}}) == []


def test_missing_observation_reference_is_never_replaced_with_a_trace_reference():
    value = context()
    value["recent_history"]["action_attempts"]["items"][0]["observation_seq"] = None
    with pytest.raises(ValueError, match="bridge observation sequence"):
        recent_records(value)
