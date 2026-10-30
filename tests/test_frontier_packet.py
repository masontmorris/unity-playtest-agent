"""Frontier packet fidelity: the records S1 saw, the citations it may use, and nothing else (#33).

Every assertion here is about measured content or an explicit binding. A missing value stays null,
a fact's citations come only from its `seqs`, and S1's rationale and distribution never appear.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from playtest.contracts.bridge import Observation, ReaderError
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Comparator, Directive, DirectiveOutcome, Expected
from playtest.contracts.loop import (
    Choice,
    ChoiceKind,
    ChoiceSet,
    ChoiceSource,
    TurnOutcome,
    TurnRecord,
    WhilePending,
)
from playtest.core.context import canonical
from playtest.frontier.packet import Packet, build_packet
from playtest.roles import frontier


def observation(**overrides) -> Observation:
    values = {
        "session": "run-1",
        "seq": 10,
        "game_s": 12.5,
        "wall_utc": datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc),
        "clock_mode": ClockMode.realtime,
        "scene": "scene-a",
        "playing": True,
        "paused": False,
        "time_scale": 1.0,
        "frame": 100,
        "public": {"counter": 2, "flag": True, "position": [1.0, 2.0, 3.0]},
        "ui": {"panel": None},
        "reader_errors": [ReaderError(reader="counter", error="timed out")],
    }
    values.update(overrides)
    return Observation(**values)


def choice_set(**overrides) -> ChoiceSet:
    values = {
        "set_version": 3,
        "built_from_seq": 10,
        "choices": [
            Choice(
                id="c1",
                kind=ChoiceKind.action,
                label="Move",
                action="move",
                params={"hold_s": 1.0},
                source=ChoiceSource.manifest,
            ),
            Choice(id="c2", kind=ChoiceKind.wait, label="Wait", source=ChoiceSource.core),
            Choice(
                id="c3",
                kind=ChoiceKind.escalate,
                label="Ask for help",
                role="frontier",
                source=ChoiceSource.core,
            ),
            Choice(id="c4", kind=ChoiceKind.finish, label="Finish", source=ChoiceSource.core),
        ],
    }
    values.update(overrides)
    return ChoiceSet(**values)


def escalation(**overrides) -> TurnRecord:
    values = {
        "turn": 4,
        "set_version": 3,
        "based_on_seq": 10,
        "raw_choice": "c3",
        "effective_choice": "c3",
        "executed_action": None,
        "outcome": TurnOutcome.escalated,
        "while_pending": WhilePending(mode="wait"),
        "policy_default": False,
        "latency_ms": 8.0,
    }
    values.update(overrides)
    return TurnRecord(**values)


def directive(**overrides) -> Directive:
    values = {
        "directive_id": "d1",
        "model": "muse-spark-1.3",
        "based_on_seq": 9,
        "text": "Try the panel and say whether the flag changes.",
        "target": None,
        "expected": Expected(field="public.flag", op=Comparator.eq, value=True),
        "tolerance": None,
        "deadline_game_s": 30.0,
        "outcome": DirectiveOutcome.open,
        "evidence_seq": [9],
    }
    values.update(overrides)
    return Directive(**values)


def build(**overrides) -> Packet:
    values = {
        "objective": "Find a state that disagrees with the stated expectation.",
        "observation": observation(),
        "choices": choice_set(),
        "escalation": escalation(),
        "facts": (),
        "recent": (),
        "directive": None,
        "directive_history": (),
        "hypotheses": (),
        "new_hypothesis_ids": (),
        "age_s": 0.25,
        "clip": None,
        "clip_unavailable_reason": None,
    }
    values.update(overrides)
    return build_packet(**values)


FACTS = [
    {"text": "flag changed after the action", "seqs": [9, 10]},
    {"text": "the counter reader reported an error", "seqs": []},
]
RECENT = [
    {"seq": 9, "game_s": 11.0, "wall_utc": "2026-10-27T11:59:00Z", "public": {"flag": False}},
    {
        "seq": 11,
        "execution": {"choice_id": "c1", "status": "completed"},
        "observed_effect": [{"field": "public.flag", "delta": 1.0}],
    },
    {"seq": 12, "turn": {"turn": 3, "outcome": "executed"}},
]
HYPOTHESES = [
    {
        "id": "h1",
        "statement": "the flag change is caused by the action",
        "status": "open",
        "supporting_seq": [10],
        "contradicting_seq": [9],
        "confirm_if": "a later action changes it again",
        "refute_if": "it changes with no action",
        "evidence_seq": [3],
        "prior_seq": [97],
    },
    {
        "id": "h0",
        "statement": "an earlier interpretation",
        "status": "supported",
        "supporting_seq": [1],
        "contradicting_seq": [],
        "confirm_if": "the same state returns",
        "refute_if": "it never returns",
    },
]
CLIP = {
    "from_seq": 9,
    "to_seq": 11,
    "seconds": 2.0,
    "sha256": "ab" * 32,
    "bytes": 128,
    "mime": "video/mp4",
}


def full() -> Packet:
    return build(
        facts=FACTS,
        recent=RECENT,
        directive=directive(),
        directive_history=[
            directive(
                directive_id="d0",
                based_on_seq=7,
                outcome=DirectiveOutcome.not_met,
                evidence_seq=[8],
            )
        ],
        hypotheses=HYPOTHESES,
        new_hypothesis_ids=["h2", "h3"],
        clip=CLIP,
    )


# --- the packet ----------------------------------------------------------------------------------


def test_packet_carries_every_supplied_record_and_outcome():
    packet = full()
    payload = packet.payload

    assert payload["objective"] == "Find a state that disagrees with the stated expectation."
    assert payload["as_of"] == {"seq": 10, "game_s": 12.5, "age_s": 0.25}
    assert payload["observation"]["seq"] == 10
    assert payload["observation"]["public"] == {"counter": 2, "flag": True, "position": [1.0, 2.0, 3.0]}
    assert payload["observation"]["ui"] == {"panel": None}
    assert payload["observation"]["reader_errors"] == [{"reader": "counter", "error": "timed out"}]
    assert payload["choice_set_version"] == 3
    assert [choice["id"] for choice in payload["choices"]] == ["c1", "c2", "c3", "c4"]
    assert payload["directive"] == directive().model_dump(mode="json")
    assert payload["directive"]["outcome"] == "open"
    assert payload["directive_history"][0]["outcome"] == "not_met"
    assert payload["directive_history"][0]["evidence_seq"] == [8]
    assert payload["facts"] == FACTS
    assert payload["recent"] == RECENT
    assert payload["hypotheses"] == HYPOTHESES
    assert payload["new_hypothesis_ids"] == ["h2", "h3"]
    assert payload["clip"] == CLIP
    assert payload["clip_unavailable_reason"] is None

    assert payload["escalation"]["turn"] == 4
    assert payload["escalation"]["raw_choice"] == "c3"
    assert payload["escalation"]["effective_choice"] == "c3"
    assert payload["escalation"]["while_pending"] == {"mode": "wait"}
    assert payload["escalation"]["policy_default"] is False
    assert payload["escalation"]["outcome"] == "escalated"
    assert "reason" not in payload["escalation"]
    assert "distribution" not in payload["escalation"]


def test_refs_come_only_from_explicit_sequences_and_open_hypotheses():
    packet = full()
    # observation 10, facts 9/10, recent 9/11/12. Nested supporting_seq/evidence_seq/prior_seq
    # numbers, effect values and public readings are not citations.
    assert packet.refs.seqs == frozenset({9, 10, 11, 12})
    assert 97 not in packet.refs.seqs
    assert 3 not in packet.refs.seqs
    assert packet.refs.hypotheses == frozenset({"h1"})
    assert packet.refs.new_hypothesis_ids == ("h2", "h3")
    assert packet.refs.open_directive is True


def test_refs_carry_a_copy_of_the_request_observations_public_readings():
    public = {"counter": 2, "flag": True}
    packet = build(observation=observation(public=public))
    public["counter"] = 9
    assert packet.refs.public == {"counter": 2, "flag": True}
    assert packet.refs.public == packet.payload["observation"]["public"]


def test_review_judges_a_setpoint_against_the_packets_own_observation():
    # The same observation the frontier was shown decides whether a reach setpoint already holds (#141).
    packet = build()  # public: counter 2
    manifest = frontier.Manifest(fields={"public.counter": "number"}, actions={}, skills=[])

    def reviewed(op, value):
        expected = {"field": "public.counter", "op": op, "value": value, "tolerance": None}
        directive = {"deadline_s": 5.0, "expected": expected, "target": None, "tests": None, "text": "try it"}
        answer = frontier.FrontierAnswer.model_validate({
            "add_choices": [], "candidate_finding": None, "directive": directive, "hypotheses": [],
            "prune_choices": []})
        return frontier.review(answer, manifest, packet.refs)

    assert [p.item for p in reviewed("==", 2.0).problems] == ["directive"]
    assert [p.item for p in reviewed(">=", 1.0).problems] == ["directive"]
    assert reviewed(">", 2.0).clean
    assert reviewed("changed", None).clean


def test_canonical_text_is_stable_and_equal_for_equal_records():
    first = full()
    second = full()
    assert first.canonical() == second.canonical()
    assert first.canonical() == canonical(first.payload)


def test_records_are_deep_copied_in_both_directions():
    facts = [{"text": "a measured fact", "seqs": [9]}]
    recent = [{"seq": 11, "public": {"flag": False}}]
    hypotheses = [copy.deepcopy(HYPOTHESES[0])]
    public = {"flag": True}
    packet = build(facts=facts, recent=recent, hypotheses=hypotheses, observation=observation(public=public))

    facts[0]["text"] = "changed"
    recent[0]["public"]["flag"] = True
    hypotheses[0]["supporting_seq"].append(99)
    public["flag"] = False

    assert packet.payload["facts"][0]["text"] == "a measured fact"
    assert packet.payload["recent"][0]["public"]["flag"] is False
    assert packet.payload["hypotheses"][0]["supporting_seq"] == [10]
    assert packet.payload["observation"]["public"]["flag"] is True

    packet.payload["recent"][0]["public"]["flag"] = True
    assert recent[0]["public"]["flag"] is True


# --- missing evidence ----------------------------------------------------------------------------


def test_missing_values_stay_null_and_are_not_invented():
    packet = build(
        observation=observation(game_s=None, public={"flag": None}, ui=None, reader_errors=[]),
        age_s=None,
        recent=[{"seq": 9, "public": {"flag": None}}],
        clip=None,
        clip_unavailable_reason=None,
    )
    payload = packet.payload
    assert payload["as_of"]["game_s"] is None
    assert payload["as_of"]["age_s"] is None
    assert payload["observation"]["ui"] is None
    assert payload["observation"]["public"] == {"flag": None}
    assert "game_s" not in payload["recent"][0]
    assert payload["directive"] is None
    assert payload["directive_history"] == []
    assert payload["hypotheses"] == []
    assert payload["clip"] is None
    assert payload["clip_unavailable_reason"] is None


def test_a_missing_observation_value_is_absent_not_zero():
    packet = build(observation=observation(public={}))
    assert packet.payload["observation"]["public"] == {}
    assert "flag" not in packet.payload["observation"]["public"]


def test_clip_unavailable_reason_is_kept_when_no_clip_exists():
    packet = build(clip=None, clip_unavailable_reason="capture is disabled")
    assert packet.payload["clip"] is None
    assert packet.payload["clip_unavailable_reason"] == "capture is disabled"


def test_clip_descriptor_must_be_measured_and_path_free():
    with pytest.raises(ValueError, match="sha256"):
        build(clip={"from_seq": 9, "bytes": 128})
    with pytest.raises(ValueError, match="bytes"):
        build(clip={"from_seq": 9, "sha256": "ab" * 32})
    with pytest.raises(ValueError, match="path"):
        build(clip={**CLIP, "path": "clip.mp4"})
    with pytest.raises(ValueError, match="absolute"):
        build(clip={**CLIP, "name": "C:\\capture\\clip.mp4"})
    with pytest.raises(ValueError, match="both"):
        build(clip=CLIP, clip_unavailable_reason="also unavailable")
    with pytest.raises(ValueError, match="MP4"):
        build(clip={**CLIP, "mime": "video/webm"})


# --- bindings ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "choices, record, needle",
    [
        (choice_set(), escalation(outcome=TurnOutcome.delegated), "delegated"),
        (choice_set(), escalation(based_on_seq=9), "based on seq"),
        (choice_set(built_from_seq=9), escalation(), "bound to seq"),
        (choice_set(set_version=2), escalation(), "choice set version"),
        (choice_set(), escalation(raw_choice="c2", effective_choice="c2"), "not 'escalate'"),
        (choice_set(), escalation(raw_choice="missing", effective_choice="missing"), "not in the choice set"),
        (choice_set(), escalation(raw_choice=None, effective_choice=None), "no raw choice"),
        (choice_set(), escalation(effective_choice="c2"), "acted on"),
    ],
    ids=[
        "delegate",
        "wrong_observation",
        "unbound_choice_set",
        "wrong_version",
        "not_escalate",
        "unknown_choice",
        "no_raw_choice",
        "effective_mismatch",
    ],
)
def test_invalid_bindings_are_rejected(choices, record, needle):
    with pytest.raises(ValueError, match=needle):
        build(choices=choices, escalation=record)


def test_the_escalation_projection_has_no_rationale_or_distribution():
    record = escalation()
    packet = build(escalation=record)
    assert set(packet.payload["escalation"]) <= {
        "turn",
        "set_version",
        "based_on_seq",
        "raw_choice",
        "effective_choice",
        "executed_action",
        "outcome",
        "while_pending",
        "policy_default",
        "latency_ms",
    }
    assert "reason" not in packet.payload["escalation"]
    assert "distribution" not in packet.payload["escalation"]


@pytest.mark.parametrize(
    "recent",
    [
        [{"seq": 11, "reason": "I chose this because it looked right"}],
        [{"seq": 11, "turn": {"distribution": {"c1": 0.9}}}],
        [{"seq": 11, "note": "text", "decision": {"reason": "because"}}],
    ],
    ids=["reason", "nested_distribution", "nested_decision_reason"],
)
def test_s1_rationale_and_distribution_are_not_carried_into_recent_rows(recent):
    with pytest.raises(ValueError, match="S1"):
        build(recent=recent)


def test_a_measured_public_field_named_reason_is_not_s1_rationale():
    recent = [{"seq": 11, "public": {"reason": "the panel is closed"}}]
    packet = build(recent=recent)
    assert packet.payload["recent"] == recent


def test_recent_rows_need_an_explicit_sequence():
    with pytest.raises(ValueError, match="explicit seq"):
        build(recent=[{"public": {"flag": True}}])
    with pytest.raises(ValueError, match="non-negative integer"):
        build(recent=[{"seq": True}])


# --- facts and hypotheses ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fact, needle",
    [
        ({"text": "no citation list"}, "exactly"),
        ({"text": "bad seq", "seqs": 9}, "list"),
        ({"text": "bool seq", "seqs": [True]}, "non-negative integer"),
        ({"text": "negative seq", "seqs": [-1]}, "non-negative integer"),
        ({"text": "  ", "seqs": []}, "empty"),
        ({"text": "extra", "seqs": [], "recommended": "move"}, "exactly"),
    ],
    ids=["missing_seqs", "scalar_seqs", "bool_seq", "negative_seq", "empty_text", "extra_key"],
)
def test_facts_have_an_explicit_shape(fact, needle):
    with pytest.raises(ValueError, match=needle):
        build(facts=[fact])


def test_hypotheses_keep_every_field_and_reject_bad_statuses():
    packet = build(hypotheses=HYPOTHESES)
    assert packet.payload["hypotheses"] == HYPOTHESES
    with pytest.raises(ValueError, match="status"):
        build(hypotheses=[{"id": "h1", "status": "maybe"}])
    with pytest.raises(ValueError, match="twice"):
        build(hypotheses=[HYPOTHESES[0], HYPOTHESES[0]])


def test_only_the_live_directive_opens_the_choice_bound():
    live = build(directive=directive())
    assert live.refs.open_directive is True
    closed = build(directive=directive(outcome=DirectiveOutcome.expired))
    assert closed.refs.open_directive is False
    history_only = build(directive=None, directive_history=[directive()])
    assert history_only.refs.open_directive is False
    assert history_only.payload["directive"] is None
    assert history_only.payload["directive_history"][0]["outcome"] == "open"


def test_nonfinite_values_are_rejected_by_canonical_serialization():
    with pytest.raises(ValueError, match="finite"):
        build(observation=observation(public={"counter": float("nan")}))
    with pytest.raises(ValueError, match="finite"):
        build(age_s=float("inf"))


def test_findings_already_recorded_are_carried_and_malformed_rows_are_refused():
    check = {"field": "public.score", "op": ">=", "value": 3, "tolerance": None}
    rows = [{"finding_id": "f-1", "title": "The score jumps", "repro_check": check, "trace_seq": 9},
            {"finding_id": "f-2", "title": "A second finding", "repro_check": None}]
    packet = build(findings=rows)
    assert packet.payload["findings"] == [{"finding_id": "f-1", "title": "The score jumps", "repro_check": check},
                                          {"finding_id": "f-2", "title": "A second finding", "repro_check": None}]
    assert build().payload["findings"] == []
    for bad, needle in (([rows[0], rows[0]], "unique finding_id"), ([{**rows[0], "title": " "}], "empty title"),
                        ([{**rows[0], "repro_check": "x"}], "not an object")):
        with pytest.raises(ValueError, match=needle):
            build(findings=bad)
