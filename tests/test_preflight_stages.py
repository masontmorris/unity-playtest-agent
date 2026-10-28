"""The preflight chain never repairs a model answer or spends past its retry budget."""

from __future__ import annotations

import json

import pytest

from playtest.preflight import stages
from playtest.preflight.packet import Packet
from playtest.roles import preflight, strict


DISCOVERY = preflight.Discovery({"field": "int"}, {"key": "Button"})
def fake_packet(text):
    return Packet(text, DISCOVERY, {"field": "field"}, frozenset({"key"}), None)
READERS = {"stage_4": {"readers": [{"field_id": "field", "alias": "counter", "why": "state"}],
                       "unobservable": ["missing state"]}, "stage_5": None, "stage_6": None}
ACTIONS = {"stage_4": None, "stage_5": {"actions": [{"name": "press", "intent": "act",
           "hold_s": None, "inputs": [{"control_id": "key", "x": None, "y": None}],
           "preconditions": []}], "skills": [], "uncontrollable": ["missing control"]}, "stage_6": None}
PROGRESS = {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [],
            "unjudgeable": ["missing objective"]}}


class Collaborator:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def __call__(self, messages, schema):
        self.requests.append((messages, strict.canonical(schema)))
        value = next(self.replies)
        if isinstance(value, stages.CallResult):
            return value
        return stages.CallResult(json.dumps(value) if isinstance(value, dict) else value, None,
                                 {"model": "test", "usage": None})


def test_chain_preserves_answers_and_uses_one_schema():
    client = Collaborator([READERS, ACTIONS, PROGRESS])
    ledger = []
    result = stages.run(fake_packet("discovery packet"), client, ledger.append)
    assert result.readers.answer.stage_4.model_dump() == READERS["stage_4"]
    assert result.actions.answer.stage_5.model_dump() == ACTIONS["stage_5"]
    assert result.progress.answer.stage_6.model_dump() == PROGRESS["stage_6"]
    assert [a.stage for a in ledger] == list(stages.STAGES)
    assert len({schema for _, schema in client.requests}) == 1
    assert client.requests[1][0][-2]["content"] == json.dumps(READERS)


def test_validation_retry_sends_exact_error_and_keeps_rejected_answer():
    invalid = json.loads(json.dumps(ACTIONS))
    invalid["stage_5"]["actions"][0]["inputs"][0]["control_id"] = "invented"
    client = Collaborator([READERS, invalid, ACTIONS, PROGRESS])
    ledger = []
    result = stages.run(fake_packet("packet"), client, ledger.append)
    rejected = result.actions.attempts[0]
    assert rejected.content == json.dumps(invalid)
    assert rejected.problems == ("stage_5.actions[0].inputs[0]: 'invented' is not a discovered control",)
    retry_messages = client.requests[2][0]
    assert retry_messages[-2]["content"] == rejected.content
    assert retry_messages[-1]["content"] == preflight.retry_turn("actions", rejected.problems)
    assert result.actions.answer.stage_5.model_dump() == ACTIONS["stage_5"]


def test_exhausted_actions_do_not_generate_an_action_and_progress_can_run():
    client = Collaborator([READERS, "{}", "{}", "{}", "{}", PROGRESS])
    ledger = []
    result = stages.run(fake_packet("packet"), client, ledger.append)
    assert result.actions.answer is None
    assert len(result.actions.attempts) == 4
    assert result.progress.answer is not None
    assert "no actions were accepted" in client.requests[-1][0][-2]["content"]
    assert len(ledger) == 6


def test_rejected_readers_stop_dependent_stages():
    client = Collaborator(["{}"] * 4)
    result = stages.run(fake_packet("packet"), client, lambda _: None)
    assert result.readers.answer is None
    assert result.actions.skipped_reason == result.progress.skipped_reason == "readers were not accepted"
    assert len(client.requests) == 4


def test_transport_failure_is_recorded_and_never_retried():
    client = Collaborator([READERS, stages.CallResult(None, "timeout", {"usage": None})])
    ledger = []
    result = stages.run(fake_packet("packet"), client, ledger.append)
    assert len(client.requests) == 2
    assert ledger[-1].transport_error == "timeout"
    assert result.actions.answer is None
    assert result.progress.skipped_reason == "actions transport failed"


def test_refusal_is_not_misreported_as_transport_failure():
    client = Collaborator([READERS, stages.CallResult(None, "refused", {}, "refusal")])
    ledger = []
    result = stages.run(fake_packet("packet"), client, ledger.append)
    assert ledger[-1].failure_kind == "refusal" and ledger[-1].transport_error is None
    assert result.progress.skipped_reason == "actions refusal failed"


def test_ledger_failure_stops_before_next_spend():
    client = Collaborator([READERS, ACTIONS])
    def fail(_):
        raise OSError("ledger unavailable")
    with pytest.raises(OSError, match="ledger unavailable"):
        stages.run(fake_packet("packet"), client, fail)
    assert len(client.requests) == 1


def test_strict_types_are_not_coerced():
    invalid = json.loads(json.dumps(ACTIONS))
    invalid["stage_5"]["actions"][0]["hold_s"] = "1"
    client = Collaborator([READERS, invalid, ACTIONS, PROGRESS])
    result = stages.run(fake_packet("packet"), client, lambda _: None)
    assert result.actions.attempts[0].problems
    assert result.actions.answer.stage_5.actions[0].hold_s is None


def test_execution_review_rejects_and_retries_without_dropping_choices():
    client = Collaborator([READERS, ACTIONS, ACTIONS, PROGRESS])
    count = 0
    def execution_review(answer, stage):
        nonlocal count
        if stage == "actions":
            count += 1
            return ["action is unavailable"] if count == 1 else []
        return []
    result = stages.run(fake_packet("packet"), client, lambda _: None, extra_review=execution_review)
    assert result.actions.attempts[0].problems == ("action is unavailable",)
    assert client.requests[2][0][-1]["content"] == preflight.retry_turn("actions", ["action is unavailable"])
    assert result.actions.answer.stage_5.model_dump() == ACTIONS["stage_5"]


@pytest.mark.parametrize("retries", [-1, 4, True, 1.5])
def test_invalid_retry_budget_is_rejected_before_a_call(retries):
    client = Collaborator([])
    with pytest.raises(ValueError):
        stages.run(fake_packet("packet"), client, lambda _: None, max_retries=retries)
    assert not client.requests
