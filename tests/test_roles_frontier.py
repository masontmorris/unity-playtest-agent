"""Frontier answer schema, review and conversion into record contracts (#73).

Setpoints are scored from manifest field kinds, hypothesis and sequence references are checked
against the packet, and problems stay attached to the item they belong to. The schema holds only
run-stable enums, never a packet's sequence numbers or hypothesis ids.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from playtest.contracts.investigation import Comparator, ExpectedSource, FindingState
from playtest.contracts.loop import ChoiceKind, ChoiceSource
from playtest.roles import frontier, strict


@pytest.fixture
def manifest():
    return frontier.Manifest(
        fields={
            "public.item_held": "bool",
            "public.counter": "number",
            "public.pos": "vector",
            "scene": "string",
        },
        actions={"move": frontier.ActionSpec(axis=True, max_hold_s=3.0), "use": frontier.ActionSpec()},
        skills=["reset"],
    )


@pytest.fixture
def refs():
    return frontier.PacketRefs(
        seqs=frozenset({10, 11, 12}),
        hypotheses=frozenset({"h1"}),
        new_hypothesis_ids=("h2", "h3"),
        open_directive=True,
    )


def hypothesis(hid="h1", supporting=(), contradicting=()):
    return {
        "id": hid,
        "statement": "a reading of the evidence",
        "status": "open",
        "supporting_seq": list(supporting),
        "contradicting_seq": list(contradicting),
        "confirm_if": "a later sample agrees",
        "refute_if": "a later sample disagrees",
    }


def setpoint(field, op, value, tolerance=None):
    return {"field": field, "op": op, "value": value, "tolerance": tolerance}


def directive(tests="h1", expected=None, deadline_s=20.0, text="try the controlled test"):
    return {
        "text": text,
        "tests": tests,
        "target": None,
        "expected": setpoint("public.item_held", Comparator.eq, True) if expected is None else expected,
        "deadline_s": deadline_s,
    }


def add(name="move", label="move a step", hold_s=2.0, x=0.0, y=1.0):
    return {"name": name, "label": label, "hold_s": hold_s, "x": x, "y": y}


def finding(seqs=(11, 12), repro=None):
    return {
        "title": "state disagrees with the expectation",
        "severity": "major",
        "severity_reason": "it blocks progress",
        "expected": "the state changes",
        "expected_source": ExpectedSource.developer_assertion,
        "actual": "it does not",
        "conditions": "after the action",
        "steps": ["do the action"],
        "evidence_seq": list(seqs),
        "repro_check": repro if repro is not None else setpoint("public.counter", Comparator.lt, 3.0),
        "uncertainty": "one sample only",
    }


def answer(**sections):
    body = {"hypotheses": [], "directive": None, "add_choices": [], "prune_choices": [], "candidate_finding": None}
    body.update(sections)
    return frontier.FrontierAnswer.model_validate(body)


def full_answer():
    return answer(
        hypotheses=[hypothesis("h1", supporting=[10]), hypothesis("h2")],
        directive=directive(tests="h2"),
        add_choices=[add()],
        prune_choices=[{"name": "use", "reason": "not needed for the test"}],
        candidate_finding=finding(),
    )


# --- schema --------------------------------------------------------------------------------------


def test_schema_is_strict_stable_and_packet_free(manifest):
    schema = frontier.schema(manifest)
    assert strict.check(schema) == []
    assert strict.canonical(schema) == strict.canonical(frontier.schema(manifest))
    text = strict.canonical(schema)
    for token in ("10", "11", "12", "h1", "h2", "h3"):
        assert token not in text


# --- a fully valid answer ------------------------------------------------------------------------


def test_valid_answer_reviews_clean(manifest, refs):
    assert frontier.review(full_answer(), manifest, refs).clean


def test_directive_conversion_puts_the_deadline_in_game_time(manifest, refs):
    a = full_answer()
    received = frontier.to_directive(
        a.directive, directive_id="d1", parent=None, model="m", based_on_seq=12, received_game_s=40.0
    )
    assert received.deadline_game_s == 60.0
    no_clock = frontier.to_directive(
        a.directive, directive_id="d1", parent=None, model="m", based_on_seq=12, received_game_s=None
    )
    assert no_clock.deadline_game_s is None


def test_choices_instantiate_manifest_actions_and_skills(manifest, refs):
    a = full_answer()
    action = frontier.to_choices(a.add_choices, manifest, ids=["x1"], ttl_game_s=20.0)[0]
    assert action.kind is ChoiceKind.action
    assert action.action == "move"
    assert action.params == {"hold_s": 2.0, "axis": {"x": 0.0, "y": 1.0}}
    assert action.source is ChoiceSource.frontier

    reset = frontier.AddChoice.model_validate(add(name="reset", hold_s=None, x=None, y=None))
    skill = frontier.to_choices([reset], manifest, ids=["x2"], ttl_game_s=20.0)[0]
    assert skill.kind is ChoiceKind.skill
    assert skill.skill == "reset"


def test_finding_stays_a_candidate_and_keeps_its_evidence(manifest, refs):
    a = full_answer()
    record = frontier.to_finding(a.candidate_finding, finding_id="f1")
    assert record.state is FindingState.candidate
    assert record.evidence_seq == [11, 12]


# --- hypothesis and packet references ------------------------------------------------------------


@pytest.mark.parametrize(
    "sections, items",
    [
        (dict(hypotheses=[hypothesis("h9")]), ["hypotheses[0]"]),
        (dict(hypotheses=[hypothesis("h2"), hypothesis("h2")]), ["hypotheses[1]"]),
        (dict(hypotheses=[hypothesis("h1", supporting=[99])]), ["hypotheses[0]"]),
        (dict(directive=directive(tests="h9")), ["directive"]),
    ],
    ids=["unknown_id", "new_id_twice", "unknown_sequence", "directive_unknown_hypothesis"],
)
def test_references_are_checked_per_item(manifest, refs, sections, items):
    review = frontier.review(answer(**sections), manifest, refs)
    assert [p.item for p in review.problems] == items


def test_directive_may_test_a_hypothesis_created_in_the_same_answer(manifest, refs):
    review = frontier.review(answer(hypotheses=[hypothesis("h2")], directive=directive(tests="h2")), manifest, refs)
    assert review.clean


def test_directive_may_not_test_a_rejected_hypothesis(manifest, refs):
    # h2 cites a sequence number the packet does not show, so its item is rejected; a directive
    # testing it would point at a hypothesis that is never recorded.
    review = frontier.review(answer(hypotheses=[hypothesis("h2", supporting=[99])], directive=directive(tests="h2")),
                             manifest, refs)
    assert [p.item for p in review.problems] == ["hypotheses[0]", "directive"]


# --- setpoints -----------------------------------------------------------------------------------


SETPOINT_CASES = [
    (
        "ordering_on_bool",
        setpoint("public.item_held", Comparator.lt, 1.0),
        "needs a numeric field and a number",
    ),
    (
        "tolerance_on_ordering",
        setpoint("public.counter", Comparator.lt, 3.0, tolerance=0.5),
        "tolerance applies only to numeric == or !=",
    ),
    (
        "changed_with_value",
        setpoint("public.counter", Comparator.changed, 5.0),
        "takes value null",
    ),
    (
        "equality_on_vector",
        setpoint("public.pos", Comparator.eq, "x"),
        "supports only changed/unchanged",
    ),
]


@pytest.mark.parametrize(
    "expected, needle",
    [(expected, needle) for _, expected, needle in SETPOINT_CASES],
    ids=[name for name, _, _ in SETPOINT_CASES],
)
def test_setpoint_problems_flag_the_directive(manifest, refs, expected, needle):
    review = frontier.review(answer(directive=directive(tests="h1", expected=expected)), manifest, refs)
    assert [p.item for p in review.problems] == ["directive"]
    assert needle in review.problems[0].reason


def test_numeric_equality_with_tolerance_is_clean(manifest, refs):
    expected = setpoint("public.counter", Comparator.eq, 3.0, tolerance=0.5)
    assert frontier.review(answer(directive=directive(tests="h1", expected=expected)), manifest, refs).clean


# --- add and prune choices -----------------------------------------------------------------------


ADD_PRUNE_CASES = [
    ("hold_over_bound", dict(add_choices=[add(hold_s=5.0)]), "add_choices[0]", "over the manifest bound"),
    (
        "axis_on_non_axis_action",
        dict(add_choices=[add(name="use", hold_s=None, x=0.0, y=1.0)]),
        "add_choices[0]",
        "drives no axis control",
    ),
    (
        "x_without_y",
        dict(add_choices=[add(hold_s=None, x=0.0, y=None)]),
        "add_choices[0]",
        "needs both x and y",
    ),
    (
        "skill_override",
        dict(add_choices=[add(name="reset", hold_s=1.0, x=None, y=None)]),
        "add_choices[0]",
        "takes no hold or axis override",
    ),
    ("added_twice", dict(add_choices=[add(), add()]), "add_choices[1]", "added twice"),
    (
        "added_and_pruned",
        dict(
            add_choices=[add(hold_s=None, x=None, y=None)],
            prune_choices=[{"name": "move", "reason": "conflict"}],
        ),
        "prune_choices[0]",
        "both added and pruned",
    ),
    (
        "pruned_twice",
        dict(
            prune_choices=[
                {"name": "use", "reason": "first"},
                {"name": "use", "reason": "second"},
            ]
        ),
        "prune_choices[1]",
        "pruned twice",
    ),
]


@pytest.mark.parametrize(
    "sections, item, needle",
    [(sections, item, needle) for _, sections, item, needle in ADD_PRUNE_CASES],
    ids=[name for name, _, _, _ in ADD_PRUNE_CASES],
)
def test_add_and_prune_problems_are_attributed(manifest, refs, sections, item, needle):
    review = frontier.review(answer(**sections), manifest, refs)
    assert [p.item for p in review.problems] == [item]
    assert needle in review.problems[0].reason


def test_choices_are_unbounded_without_an_open_directive(manifest, refs):
    a = answer(
        add_choices=[add(hold_s=None, x=None, y=None)],
        prune_choices=[{"name": "use", "reason": "not needed"}],
    )
    closed = replace(refs, open_directive=False)
    review = frontier.review(a, manifest, closed)
    assert [p.item for p in review.problems] == ["add_choices[0]", "prune_choices[0]"]
    assert frontier.review(a, manifest, refs).clean


# --- candidate finding ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record, needle",
    [
        (finding(seqs=(99,)), "not in the packet"),
        (finding(repro=setpoint("scene", Comparator.gt, "x")), "needs a numeric field and a number"),
    ],
    ids=["unknown_sequence", "ordering_on_string"],
)
def test_candidate_finding_problems_are_reported(manifest, refs, record, needle):
    review = frontier.review(answer(candidate_finding=record), manifest, refs)
    assert [p.item for p in review.problems] == ["candidate_finding"]
    assert needle in review.problems[0].reason


def test_a_finding_without_evidence_parses_then_is_reported(manifest, refs):
    payload = full_answer().model_dump(mode="json")
    payload["candidate_finding"]["evidence_seq"] = []
    payload["candidate_finding"]["steps"] = []
    value, errors = strict.parse(frontier.FrontierAnswer, json.dumps(payload))
    assert errors == []
    assert isinstance(value, frontier.FrontierAnswer)
    reasons = [p.reason for p in frontier.review(value, manifest, refs).problems if p.item == "candidate_finding"]
    assert any("must cite evidence_seq" in reason for reason in reasons)
    assert any("steps must be non-empty" in reason for reason in reasons)


# --- value rules live in review, not in the item models ------------------------------------------


def test_bad_values_parse_and_reject_only_their_own_item(manifest, refs):
    a = answer(
        directive=directive() | {"text": "", "deadline_s": 0.0},
        add_choices=[add(hold_s=0.0, x=2.0), add(name="use", hold_s=None, x=None, y=None)],
    )
    review = frontier.review(a, manifest, refs)
    assert {p.item for p in review.problems} == {"directive", "add_choices[0]"}


EMPTY_TEXT_CASES = [
    ("directive_text", dict(directive=directive() | {"text": ""}), "directive", "text is empty"),
    ("add_label", dict(add_choices=[add() | {"label": ""}]), "add_choices[0]", "label is empty"),
    (
        "prune_reason",
        dict(prune_choices=[{"name": "use", "reason": ""}]),
        "prune_choices[0]",
        "reason is empty",
    ),
    (
        "hypothesis_statement",
        dict(hypotheses=[hypothesis() | {"statement": ""}]),
        "hypotheses[0]",
        "statement is empty",
    ),
    (
        "hypothesis_confirm_if",
        dict(hypotheses=[hypothesis() | {"confirm_if": ""}]),
        "hypotheses[0]",
        "confirm_if is empty",
    ),
    (
        "hypothesis_refute_if",
        dict(hypotheses=[hypothesis() | {"refute_if": ""}]),
        "hypotheses[0]",
        "refute_if is empty",
    ),
    (
        "finding_title",
        dict(candidate_finding=finding() | {"title": ""}),
        "candidate_finding",
        "title is empty",
    ),
    (
        "finding_expected",
        dict(candidate_finding=finding() | {"expected": ""}),
        "candidate_finding",
        "expected is empty",
    ),
    (
        "finding_actual",
        dict(candidate_finding=finding() | {"actual": ""}),
        "candidate_finding",
        "actual is empty",
    ),
    (
        "finding_severity_reason",
        dict(candidate_finding=finding() | {"severity_reason": ""}),
        "candidate_finding",
        "severity_reason is empty",
    ),
    (
        "finding_uncertainty",
        dict(candidate_finding=finding() | {"uncertainty": ""}),
        "candidate_finding",
        "uncertainty is empty",
    ),
]


@pytest.mark.parametrize(
    "sections, item, needle",
    [(sections, item, needle) for _, sections, item, needle in EMPTY_TEXT_CASES],
    ids=[name for name, _, _, _ in EMPTY_TEXT_CASES],
)
def test_empty_text_is_reported_on_its_own_item(manifest, refs, sections, item, needle):
    review = frontier.review(answer(**sections), manifest, refs)
    assert [p.item for p in review.problems] == [item]
    assert needle in review.problems[0].reason


NUMBER_CASES = [
    ("hold_zero", dict(add_choices=[add(hold_s=0.0)]), "add_choices[0]", "hold_s must be positive"),
    ("hold_negative", dict(add_choices=[add(hold_s=-1.0)]), "add_choices[0]", "hold_s must be positive"),
    ("x_above_range", dict(add_choices=[add(x=2.0)]), "add_choices[0]", "x and y must be in [-1, 1]"),
    ("y_below_range", dict(add_choices=[add(y=-2.0)]), "add_choices[0]", "x and y must be in [-1, 1]"),
    ("deadline_zero", dict(directive=directive(deadline_s=0.0)), "directive", "deadline_s must be positive"),
]


@pytest.mark.parametrize(
    "sections, item, needle",
    [(sections, item, needle) for _, sections, item, needle in NUMBER_CASES],
    ids=[name for name, _, _, _ in NUMBER_CASES],
)
def test_numeric_bounds_are_reported_on_their_own_item(manifest, refs, sections, item, needle):
    review = frontier.review(answer(**sections), manifest, refs)
    assert [p.item for p in review.problems] == [item]
    assert needle in review.problems[0].reason


def test_list_caps_remain_model_constraints():
    payload = full_answer().model_dump(mode="json")
    payload["candidate_finding"]["steps"] = [f"step {i}" for i in range(frontier.MAX_STEPS + 1)]
    payload["hypotheses"] = [hypothesis("h2") for _ in range(frontier.MAX_HYPOTHESES + 1)]
    value, errors = strict.parse(frontier.FrontierAnswer, json.dumps(payload))
    assert value is None
    assert any("steps" in error for error in errors)
    assert any("hypotheses" in error for error in errors)
