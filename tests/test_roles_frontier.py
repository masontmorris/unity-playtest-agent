"""Frontier answer schema, review and conversion into record contracts (#73).

Setpoints are scored from manifest field kinds, hypothesis and sequence references are checked
against the packet, and problems stay attached to the item they belong to. The schema holds only
run-stable enums, never a packet's sequence numbers or hypothesis ids.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import get_args

import jsonschema
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
    # Severity and the replay check now have contract fields; a trace writer passes trace seqs instead.
    assert record.severity == a.candidate_finding.severity
    check = a.candidate_finding.repro_check
    assert (record.repro_check is None) == (check is None)
    if check is not None:
        assert (record.repro_check.field, record.repro_check.op) == (check.field, check.op)
    assert frontier.to_finding(a.candidate_finding, finding_id="f1", evidence_seq=[40, 52]).evidence_seq == [40, 52]


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


# --- how a setpoint is scored (#141) -------------------------------------------------------------


@pytest.fixture
def seen(refs):
    """The same packet, whose observation reads counter 3 and item_held true."""
    return replace(refs, public={"counter": 3, "item_held": True, "pos": [0.0, 1.0, 2.0]})


def reviewed(manifest, refs, expected, **kwargs):
    return frontier.review(answer(directive=directive(tests="h1", expected=expected, **kwargs)), manifest, refs)


def test_an_unchanged_setpoint_needs_a_deadline(manifest, refs):
    hold = setpoint("public.counter", Comparator.unchanged, None)
    review = reviewed(manifest, refs, hold, deadline_s=None)
    assert [p.item for p in review.problems] == ["directive"]
    assert "unchanged" in review.problems[0].reason and "needs deadline_s" in review.problems[0].reason
    assert reviewed(manifest, refs, hold, deadline_s=20.0).clean
    # Only a hold needs one: a change and a reach are met by a reading, whenever it comes.
    for other in (setpoint("public.counter", Comparator.changed, None), setpoint("public.counter", Comparator.gt, 5.0)):
        assert reviewed(manifest, refs, other, deadline_s=None).clean


ALREADY_HOLDS = [
    ("equal", setpoint("public.counter", Comparator.eq, 3.0)),
    ("equal_within_tolerance", setpoint("public.counter", Comparator.eq, 3.2, tolerance=0.5)),
    ("at_least", setpoint("public.counter", Comparator.ge, 3.0)),
    ("below_a_higher_value", setpoint("public.counter", Comparator.lt, 10.0)),
    ("not_equal", setpoint("public.counter", Comparator.ne, 7.0)),
    ("bool_equal", setpoint("public.item_held", Comparator.eq, True)),
    ("bool_not_equal", setpoint("public.item_held", Comparator.ne, False)),
]


@pytest.mark.parametrize("expected", [e for _, e in ALREADY_HOLDS], ids=[n for n, _ in ALREADY_HOLDS])
def test_a_reach_setpoint_the_request_observation_already_satisfies_is_rejected(manifest, seen, expected):
    review = reviewed(manifest, seen, expected)
    assert [p.item for p in review.problems] == ["directive"]
    assert review.problems[0].reason == "the setpoint already holds at the request observation, so it tests nothing"


NOT_YET = [
    ("above", setpoint("public.counter", Comparator.gt, 3.0)),
    ("at_least_more", setpoint("public.counter", Comparator.ge, 4.0)),
    ("below", setpoint("public.counter", Comparator.lt, 3.0)),
    ("equal_other", setpoint("public.counter", Comparator.eq, 4.0)),
    ("equal_outside_tolerance", setpoint("public.counter", Comparator.eq, 4.0, tolerance=0.5)),
    ("bool_other", setpoint("public.item_held", Comparator.eq, False)),
    ("changed", setpoint("public.counter", Comparator.changed, None)),   # compared with S1's value, not this one
    ("unchanged_vector", setpoint("public.pos", Comparator.unchanged, None)),
]


@pytest.mark.parametrize("expected", [e for _, e in NOT_YET], ids=[n for n, _ in NOT_YET])
def test_a_setpoint_still_to_be_reached_is_accepted(manifest, seen, expected):
    assert reviewed(manifest, seen, expected).clean


@pytest.mark.parametrize("public", [None, {}, {"counter": None}, {"counter": "three"}, {"counter": float("nan")}],
                         ids=["no_readings", "field_absent", "null", "wrong_type", "not_finite"])
def test_an_unknown_reading_is_not_a_reason_to_reject(manifest, refs, public):
    would_hold = setpoint("public.counter", Comparator.ge, 0.0)
    assert reviewed(manifest, replace(refs, public=public), would_hold).clean


def test_a_vacuous_directive_rejects_only_itself(manifest, seen):
    a = answer(hypotheses=[hypothesis("h2")],
               directive=directive(tests="h2", expected=setpoint("public.counter", Comparator.eq, 3.0)))
    review = frontier.review(a, manifest, seen)
    assert [p.item for p in review.problems] == ["directive"]


def test_a_findings_repro_check_may_already_hold(manifest, seen):
    # A replay check says what a reproduction must show; a finding is often true right now.
    assert frontier.review(answer(candidate_finding=finding(repro=setpoint("public.counter", Comparator.ge, 3.0))),
                           manifest, seen).clean


def test_the_prompt_and_schema_say_how_each_setpoint_is_scored(manifest):
    in_schema = strict.canonical(frontier.schema(manifest))
    for needle in ("must not already hold", "first differs from its value when S1 receives the directive",
                   "stays as it is until the deadline", "required for an unchanged setpoint"):
        assert needle in in_schema
    for needle in ("must not already hold", "changed is met when the value first differs",
                   "unchanged is met only if the value stays as it is until the deadline", "it needs deadline_s"):
        assert needle in frontier.SYSTEM


def test_the_prompt_weighs_calibration_against_a_fields_note_and_asks_for_every_suspicion_to_be_answered():
    prompt = frontier.SYSTEM
    for needle in ("Calibration says whether a field changed on this build, not whether the change is right",
                   "The packet's suspicions are every suspicion raised so far in this run"):
        assert needle in prompt
    # Every source and severity the prompt tells the model to give, or not to give, is one the finding accepts.
    for source in (ExpectedSource.interpretation, ExpectedSource.invariant, ExpectedSource.developer_assertion):
        assert source.value in prompt
    assert "unknown" in get_args(frontier.FindingOut.model_fields["severity"].annotation)
    # The answer fields it points to for telling a note's reading from a departure by the game are real ones.
    for name in ("confirm_if", "refute_if"):
        assert name in frontier.HypothesisOut.model_fields and name in prompt
    # A note says why the preflight model chose a field, not what the game intends, and calibration does not
    # excuse a reading; neither earlier claim may come back.
    assert "meant to be" not in prompt
    assert "was there before S1 played" not in prompt
    # The statuses the prompt offers for answering a suspicion are statuses an answer can carry.
    named = prompt.split("account for each in a hypothesis (")[1].split(")")[0].replace(" or ", ", ").split(", ")
    assert named == ["open", "supported", "contradicted"]
    assert set(named) <= set(get_args(frontier.HypothesisOut.model_fields["status"].annotation))


# --- effect checks (#154) ------------------------------------------------------------------------


@pytest.fixture
def effect_manifest(manifest):
    """The same manifest, with a calibrated expected displacement for `move` only."""
    return replace(manifest, effects={"move": frontier.Effect(expected=6.04, allowance=0.06)})


def effect_check(op=Comparator.lt, value=1.5, tolerance=None, action="move"):
    return setpoint(f"effect.{action}", op, value, tolerance)


def test_the_schema_offers_an_effect_check_to_a_finding_and_never_to_a_directive(effect_manifest):
    schema = frontier.schema(effect_manifest)
    assert strict.check(schema) == []
    defs = schema["$defs"]
    assert "effect.move" in defs["check_fields"]["enum"] and "effect.use" not in defs["check_fields"]["enum"]
    assert set(defs["fields"]["enum"]) <= set(defs["check_fields"]["enum"])       # every reader is still a check field
    assert not any(name.startswith("effect.") for name in defs["fields"]["enum"])   # a setpoint's enum stays readers only
    payload = full_answer().model_dump(mode="json")
    payload["candidate_finding"]["repro_check"] = effect_check()
    jsonschema.Draft202012Validator(schema).validate(payload)
    payload["candidate_finding"]["repro_check"] = effect_check(action="use")        # no calibrated displacement
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(payload)
    payload = full_answer().model_dump(mode="json")
    payload["directive"]["expected"] = effect_check()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_a_manifest_without_effects_offers_a_finding_the_same_fields_as_a_directive(manifest):
    defs = frontier.schema(manifest)["$defs"]
    assert defs["check_fields"]["enum"] == defs["fields"]["enum"]
    assert manifest.effects == {}


def test_effect_check_fields_are_listed_in_a_stable_order_after_the_readers(manifest):
    effects = {"use": frontier.Effect(expected=1.0), "move": frontier.Effect(expected=6.04, allowance=None)}
    listed = replace(manifest, effects=effects).enums()["check_fields"]
    assert listed == [*manifest.fields, "effect.move", "effect.use"]
    assert strict.canonical(frontier.schema(replace(manifest, effects=effects))) == strict.canonical(
        frontier.schema(replace(manifest, effects=dict(reversed(list(effects.items()))))))


@pytest.mark.parametrize("op, value", [(Comparator.lt, 1.5), (Comparator.le, 1.5), (Comparator.gt, 9.0),
                                       (Comparator.ge, 9)])
def test_an_effect_check_on_a_calibrated_action_reviews_clean(effect_manifest, refs, op, value):
    review = frontier.review(answer(candidate_finding=finding(repro=effect_check(op, value))), effect_manifest, refs)
    assert review.clean


EFFECT_CASES = [
    ("equal", effect_check(Comparator.eq, 0.0), "compares with <, <=, > or >=, not '=='"),
    ("not_equal", effect_check(Comparator.ne, 0.0), "compares with <, <=, > or >=, not '!='"),
    ("changed", effect_check(Comparator.changed, None), "compares with <, <=, > or >=, not 'changed'"),
    ("unchanged", effect_check(Comparator.unchanged, None), "compares with <, <=, > or >=, not 'unchanged'"),
    ("equal_with_tolerance", effect_check(Comparator.eq, 0.0, tolerance=0.1), "compares with <, <=, > or >=, not '=='"),
    ("text_value", effect_check(Comparator.lt, "far"), "needs a numeric field and a number"),
    ("no_value", effect_check(Comparator.lt, None), "'<' needs a value"),
    ("bool_value", effect_check(Comparator.lt, True), "needs a numeric field and a number"),
    ("infinite_value", effect_check(Comparator.lt, float("inf")), "compares with a finite number"),
    ("tolerance_on_a_bound", effect_check(Comparator.lt, 1.5, tolerance=0.1), "tolerance applies only to numeric == or !="),
    ("action_without_a_calibrated_displacement", effect_check(action="use"), "has no calibrated expected displacement"),
    ("action_not_in_the_manifest", effect_check(action="missing"), "has no calibrated expected displacement"),
]


@pytest.mark.parametrize("repro, needle", [(repro, needle) for _, repro, needle in EFFECT_CASES],
                         ids=[name for name, _, _ in EFFECT_CASES])
def test_a_bad_effect_check_rejects_only_its_finding(effect_manifest, refs, repro, needle):
    review = frontier.review(answer(candidate_finding=finding(repro=repro)), effect_manifest, refs)
    assert [p.item for p in review.problems] == ["candidate_finding"]
    assert any(needle in p.reason for p in review.problems)


def test_a_manifest_without_effects_rejects_every_effect_check(manifest, refs):
    review = frontier.review(answer(candidate_finding=finding(repro=effect_check())), manifest, refs)
    assert [p.item for p in review.problems] == ["candidate_finding"]
    assert "has no calibrated expected displacement" in review.problems[0].reason


def test_a_directive_may_not_name_an_effect_field(effect_manifest, refs):
    review = frontier.review(answer(directive=directive(tests="h1", expected=effect_check())), effect_manifest, refs)
    assert [p.item for p in review.problems] == ["directive"]
    assert "'effect.move' is not a manifest field" in review.problems[0].reason


def test_an_effect_check_that_holds_for_the_calibrated_displacement_is_left_to_the_replay_to_score(effect_manifest, refs):
    # Review does not judge a repro_check against the calibration (a finding is often true now); `reproduce` scores
    # a check the calibrated displacement satisfies as unknown, and says why.
    assert frontier.review(answer(candidate_finding=finding(repro=effect_check(Comparator.gt, 1.0))),
                           effect_manifest, refs).clean


def test_an_effect_check_survives_conversion_into_the_finding_record(effect_manifest):
    record = frontier.to_finding(answer(candidate_finding=finding(repro=effect_check())).candidate_finding,
                                 finding_id="f1", evidence_seq=[3])
    assert (record.repro_check.field, record.repro_check.op, record.repro_check.value) == ("effect.move", Comparator.lt, 1.5)


def test_the_prompt_and_schema_say_when_a_check_may_name_an_effect_and_what_it_must_not_satisfy(effect_manifest):
    for needle in ("When an action stops having its calibrated effect", "may name effect.<action>",
                   "net displacement of one execution of that action", "with <, <=, > or >= and a number",
                   "must not hold for the expected displacement listed there"):
        assert needle in frontier.SYSTEM
    in_schema = strict.canonical(frontier.schema(effect_manifest))
    assert "must not hold for the expected displacement the manifest lists for it" in in_schema


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


# --- exploration targets (#179) ------------------------------------------------------------------


def test_system_prompt_invites_exploration_targets():
    """The frontier may point S1 at parts of the level its clip shows but no recorded action tried.

    The provider serializes SYSTEM verbatim as the request's system message, so this also keeps
    the instruction in every frontier request.
    """
    assert "- exploration targets:" in frontier.SYSTEM
    for needle in (
        "do not appear to have reached or used",
        "never replaces accounting for each suspicion",
        "target (kind location)",
        "propose none",
        "supports no candidate_finding by itself",
    ):
        assert needle in frontier.SYSTEM


def test_system_prompt_keeps_a_target_on_reachable_surface():
    """Guidance must not send S1 off a level edge, nor spend an approach it already made.

    Session 7's frontier directives walked S1 along open ground past the scene's floor edge (z=61.5,
    no boundary) until it fell out of the world, and an earlier directive had it back away from the
    structure it had just reached. The clip is a view, not the level's extent, so the prompt says so.
    """
    assert "- navigation:" in frontier.SYSTEM
    for needle in (
        "shows what is in view, not how far the level goes",
        "never send S1 toward, across or out over ground",
        "loses the run",
        "rather than back away",
        "which spends the approach",
    ):
        assert needle in frontier.SYSTEM


def test_system_prompt_lines_s1_up_on_a_part_while_moving():
    """Session 8 reached 0.98 units from the stairs but walked past them, 10° off the line.

    The fix is guidance, not control: line up on the part, then turn back onto it as it drifts
    rather than commit to the first heading. The provider serializes SYSTEM verbatim, so this ships.
    """
    for needle in (
        "Line up before moving",
        "turn until the part sits in the centre of the view",
        "after each move turn back onto it",
        "a heading that only passes beside the part spends the approach",
    ):
        assert needle in frontier.SYSTEM


def test_exploration_answer_passes_review(manifest, refs):
    """A hypothesis about an untried part plus a go-try directive needs no contract change."""
    body = answer(
        hypotheses=[{**hypothesis("h2"), "statement": "an untried part of the level may respond to input"}],
        directive={
            "text": "turn toward the part with move, approach it and use it",
            "tests": "h2",
            "target": {"kind": "location", "ref": "in view, left of centre"},
            "expected": None,
            "deadline_s": 60.0,
        },
    )
    assert frontier.review(body, manifest, refs).clean
