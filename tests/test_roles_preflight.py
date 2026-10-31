"""Preflight stage schemas, prompts and answer review (#73).

The fixtures are tiny hand-built discoveries so each test exercises one rule: one schema serves
every stage and check names a stage-4 alias, a check is scored from the declared field type, and a
bad answer is reported, never repaired or dropped.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from playtest.contracts.investigation import Comparator
from playtest.roles import preflight
from playtest.roles.strict import canonical, check


@pytest.fixture
def discovery():
    return preflight.Discovery(
        field_types={"f1": "bool", "f2": "float", "f3": "int", "f4": "Vector3", "f5": "string", "f6": "CustomType"},
        control_types={"c1": "Button", "c2": "Vector2", "c3": "Axis"},
    )


READER_ALIASES = {"item_held": "f1", "speed": "f2", "counter": "f3", "pos": "f4", "text": "f5"}


def readers_part(readers=(), unobservable=()):
    return {"readers": list(readers), "unobservable": list(unobservable)}


def actions_part(actions=(), skills=(), uncontrollable=()):
    return {"actions": list(actions), "skills": list(skills), "uncontrollable": list(uncontrollable)}


def progress_part(progress=(), invariants=(), unjudgeable=()):
    return {"progress": list(progress), "invariants": list(invariants), "unjudgeable": list(unjudgeable)}


def answer(*, stage_4=None, stage_5=None, stage_6=None):
    return preflight.StageAnswer(stage_4=stage_4, stage_5=stage_5, stage_6=stage_6)


def reader(field_id="f1", alias="item_held"):
    return {"field_id": field_id, "alias": alias, "why": "changes during play"}


def control_input(control_id, x=None, y=None):
    return {"control_id": control_id, "x": x, "y": y}


def action(name="move", inputs=None, hold_s=None, preconditions=None):
    return {
        "name": name,
        "intent": "change the state",
        "inputs": inputs if inputs is not None else [control_input("c1")],
        "hold_s": hold_s,
        "preconditions": preconditions or [],
    }


def step(inputs=None, hold_s=None, wait_s=0.0):
    return {"inputs": inputs if inputs is not None else [control_input("c1")], "hold_s": hold_s, "wait_s": wait_s}


def skill(name="reset", steps=None, max_s=2.0):
    return {"name": name, "purpose": "bounded sequence", "steps": steps or [step()], "max_s": max_s}


def check_item(alias, op, value):
    return {"field": alias, "op": op, "value": value}


def progress(field="counter", direction="up"):
    return {"field": field, "direction": direction, "why": "tracks progress"}


def rule(name, kind, field=None, direction=None, lo=None, hi=None, when=None, then=None, unless=None):
    return {
        "kind": kind,
        "name": name,
        "source": "S.F",
        "catches": "a bug",
        "field": field,
        "direction": direction,
        "min": lo,
        "max": hi,
        "when": when,
        "then": then,
        "unless": unless,
    }


def range_rule(name="r", field="speed", lo=0.0, hi=1.0, unless=None):
    return rule(name, "range", field=field, lo=lo, hi=hi, unless=unless)


def monotonic_rule(name="mono", field="counter", direction="non_decreasing"):
    return rule(name, "monotonic", field=field, direction=direction)


def implies_rule(name="implies", when=None, then=None):
    return rule(
        name,
        "implies",
        when=when if when is not None else check_item("item_held", Comparator.eq, True),
        then=then if then is not None else check_item("counter", Comparator.unchanged, None),
    )


# --- schema --------------------------------------------------------------------------------------


def test_schema_is_strict_and_has_three_nullable_parts(discovery):
    rendered = preflight.schema(discovery)
    assert check(rendered) == []
    assert list(rendered["properties"]) == ["stage_4", "stage_5", "stage_6"]
    assert canonical(rendered) == canonical(preflight.schema(discovery))


def test_schema_renders_the_field_enum_once(discovery):
    text = canonical(preflight.schema(discovery))
    assert text.count('"$ref":"#/$defs/field_ids"') == 1


# --- readers -------------------------------------------------------------------------------------


def test_valid_readers_answer_is_clean(discovery):
    a = answer(stage_4=readers_part([reader("f1", "item_held"), reader("f2", "speed")],
                                    ["nothing covers the rest"]))
    assert preflight.review(a, "readers", discovery) == []


READER_CASES = [
    ("no_readers", [], "choose at least one field"),
    ("duplicate_field", [reader("f1", "a"), reader("f1", "b")], "is chosen twice"),
    ("duplicate_alias", [reader("f1", "a"), reader("f2", "a")], "is used twice"),
    ("unknown_field", [reader("f9", "a")], "is not a discovered field"),
]


@pytest.mark.parametrize(
    "readers, needle",
    [(readers, needle) for _, readers, needle in READER_CASES],
    ids=[name for name, _, _ in READER_CASES],
)
def test_readers_problems_are_reported(discovery, readers, needle):
    problems = preflight.review(answer(stage_4=readers_part(readers)), "readers", discovery)
    assert len(problems) == 1
    assert needle in problems[0]


def test_missing_part_is_reported(discovery):
    problems = preflight.review(answer(), "readers", discovery)
    assert len(problems) == 1
    assert "fill this part" in problems[0]


def test_another_part_must_be_null(discovery):
    a = answer(stage_4=readers_part([reader()]), stage_5=actions_part([action()]))
    problems = preflight.review(a, "readers", discovery)
    assert len(problems) == 1
    assert "must be null when answering" in problems[0]


def test_alias_must_be_snake_case():
    with pytest.raises(ValidationError):
        answer(stage_4=readers_part([reader("f1", "ItemHeld")]))


def test_at_most_sixteen_readers():
    with pytest.raises(ValidationError):
        answer(stage_4=readers_part([reader("f1", f"r{i}") for i in range(17)]))


# --- actions -------------------------------------------------------------------------------------


def test_valid_actions_answer_is_clean(discovery):
    a = answer(
        stage_5=actions_part(
            [
                action(name="use", inputs=[control_input("c1")], hold_s=None),
                action(name="move", inputs=[control_input("c2", 0.0, 1.0)], hold_s=1.0),
            ]
        )
    )
    assert preflight.review(a, "actions", discovery) == []


ACTION_CASES = [
    ("button_with_axis", [action(inputs=[control_input("c1", 0.5, None)])], [], "x and y must be null"),
    (
        "vector_without_axes",
        [action(inputs=[control_input("c2")], hold_s=1.0)],
        [],
        "needs x and y",
    ),
    (
        "one_dimensional_y",
        [action(inputs=[control_input("c3", 1.0, 0.5)], hold_s=1.0)],
        [],
        "one-dimensional",
    ),
    (
        "axis_without_hold",
        [action(inputs=[control_input("c2", 0.0, 1.0)], hold_s=None)],
        [],
        "an axis input needs a hold time",
    ),
    (
        "control_twice",
        [action(inputs=[control_input("c1"), control_input("c1")])],
        [],
        "appears twice",
    ),
    ("unknown_control", [action(inputs=[control_input("c9")])], [], "is not a discovered control"),
    ("duplicate_action_name", [action(), action()], [], "is used twice"),
    ("skill_name_matches_action", [action()], [skill(name="move")], "is already an action or skill"),
    (
        "skill_over_time_bound",
        [action()],
        [skill(steps=[step(hold_s=1.0, wait_s=1.0)], max_s=1.5)],
        "more than max_s",
    ),
    (
        "precondition_not_a_reader",
        [action(preconditions=[check_item("unknown_reader", Comparator.ge, 1.0)])],
        [],
        "is not a stage-4 reader alias",
    ),
]


@pytest.mark.parametrize(
    "actions, skills, needle",
    [(actions, skills, needle) for _, actions, skills, needle in ACTION_CASES],
    ids=[name for name, _, _, _ in ACTION_CASES],
)
def test_action_problems_are_reported_one_per_case(discovery, actions, skills, needle):
    a = answer(stage_5=actions_part(actions, skills))
    problems = preflight.review(a, "actions", discovery, readers=READER_ALIASES)
    assert len(problems) == 1
    assert needle in problems[0]


def test_check_field_must_be_snake_case():
    with pytest.raises(ValidationError):
        preflight.Check.model_validate({"field": "ItemHeld", "op": Comparator.eq, "value": True})


# --- progress and invariants ---------------------------------------------------------------------


def test_valid_progress_answer_is_clean(discovery):
    a = answer(
        stage_6=progress_part(
            progress=[progress("counter"), progress("item_held", "down")],
            invariants=[range_rule(), monotonic_rule(), implies_rule()],
        )
    )
    assert preflight.review(a, "progress", discovery, readers=READER_ALIASES) == []


PROGRESS_CASES = [
    ("string_progress", [progress("text")], [], "progress needs a numeric or bool reader"),
    ("duplicate_progress", [progress("counter"), progress("counter")], [], "is listed twice"),
]


@pytest.mark.parametrize(
    "progress_fields, invariants, needle",
    [(fields, rules, needle) for _, fields, rules, needle in PROGRESS_CASES],
    ids=[name for name, _, _, _ in PROGRESS_CASES],
)
def test_progress_fields_problems_are_reported(discovery, progress_fields, invariants, needle):
    a = answer(stage_6=progress_part(progress_fields, invariants))
    problems = preflight.review(a, "progress", discovery, readers=READER_ALIASES)
    assert len(problems) == 1
    assert needle in problems[0]


INVARIANT_CASES = [
    ("range_on_bool", [range_rule(field="item_held", lo=0.0)], "needs a numeric reader"),
    ("range_without_bounds", [range_rule(lo=None, hi=None)], "a range needs min, max or both"),
    ("range_inverted", [range_rule(lo=2.0, hi=1.0)], "is above max"),
    (
        "range_with_direction",
        [range_rule(lo=0.0, hi=1.0) | {"direction": "non_decreasing"}],
        "a range rule does not use direction; set it to null",
    ),
    (
        "implies_with_unless",
        [rule("imp", "implies", when={"field": "item_held", "op": "==", "value": True},
              then={"field": "counter", "op": "changed", "value": None},
              unless={"field": "item_held", "op": "==", "value": False})],
        "an implies rule does not use unless; set it to null",
    ),
    (
        "monotonic_without_direction",
        [rule("mono", "monotonic", field="counter", direction=None)],
        "a monotonic rule needs a direction",
    ),
    (
        "implies_without_when",
        [rule("implies", "implies", then=check_item("counter", Comparator.unchanged, None))],
        "an implies rule needs when",
    ),
    (
        "implies_identical_checks",
        [
            implies_rule(
                when=check_item("item_held", Comparator.eq, True),
                then=check_item("item_held", Comparator.eq, True),
            )
        ],
        "when and then are the same check",
    ),
    (
        "implies_ordering_on_bool",
        [implies_rule(when=check_item("item_held", Comparator.lt, 1.0))],
        "needs a numeric field and a number",
    ),
    (
        "implies_changed_with_value",
        [implies_rule(then=check_item("counter", Comparator.changed, 5.0))],
        "takes value null",
    ),
    (
        "implies_equality_on_vector",
        [implies_rule(when=check_item("pos", Comparator.eq, "x"))],
        "supports only changed/unchanged",
    ),
    (
        "rule_on_unknown_alias",
        [range_rule(field="unknown_reader", lo=0.0)],
        "is not a stage-4 reader alias",
    ),
    (
        "duplicate_rule_name",
        [range_rule(name="x"), range_rule(name="x", field="counter", lo=0.0)],
        "is used twice",
    ),
]


@pytest.mark.parametrize(
    "invariants, needle",
    [(invariants, needle) for _, invariants, needle in INVARIANT_CASES],
    ids=[name for name, _, _ in INVARIANT_CASES],
)
def test_invariant_problems_are_reported(discovery, invariants, needle):
    a = answer(stage_6=progress_part(invariants=invariants))
    problems = preflight.review(a, "progress", discovery, readers=READER_ALIASES)
    assert len(problems) == 1
    assert needle in problems[0]


# --- stage all -----------------------------------------------------------------------------------


def test_all_stage_uses_the_readers_in_the_same_answer(discovery):
    a = answer(
        stage_4=readers_part([reader("f1", "item_held")]),
        stage_5=actions_part([action(preconditions=[check_item("item_held", Comparator.eq, True)])]),
        stage_6=progress_part(),
    )
    assert preflight.review(a, "all", discovery) == []


# --- prompts -------------------------------------------------------------------------------------


def test_action_selection_prompt_asks_for_useful_steps_and_opposite_axis_directions():
    for stage in ("actions", "all"):
        text = preflight.STAGE_PROMPTS[stage]
        assert "per-hold displacement numbers" in text
        assert "useful exploration step" in text
        assert "not merely the shortest hold" in text
        assert "a few units per step can let a session cover the level" in text
        assert "opposite direction on that same control: left and right, up and down" in text
        assert "One-way controls such as triggers do not need an opposite" in text


def test_action_selection_prompt_asks_for_a_coarse_turn_in_each_direction_on_one_look_device():
    for stage in ("actions", "all"):
        text = preflight.STAGE_PROMPTS[stage]
        assert "mean_view_turn" in text and "how many degrees the game view turned per apply" in text
        assert "(yaw positive turns right, pitch positive looks down)" in text
        assert "with its trial count and spread_deg, how far its trials disagree" in text
        assert "A turn of 180 degrees or more in one apply wraps and is read as a smaller one" in text
        assert "a large spread_deg means the mean is not a turn to rely on" in text
        assert "at least one coarse turn step, roughly 30 to 60 degrees, in each direction of each look axis on one look device" in text
        assert "(a vector control such as mouse delta or a stick whose mean_view_turn shows the view turning)" in text
        assert "fine steps may stay beside it" in text
        assert "A held control's turn can be scaled through its value or hold" in text
        assert "a relative impulse such as mouse delta cannot exceed 1" in text
        assert "repeating the impulse as ordered skill steps" in text
        assert "your estimate, not a measurement; never present it as measured" in text
    assert "mean_view_turn" not in preflight.STAGE_PROMPTS["readers"]
    assert "mean_view_turn" not in preflight.STAGE_PROMPTS["progress"]


def test_action_prompt_and_schema_say_inputs_with_x_and_y_need_a_hold(discovery):
    for stage in ("actions", "all"):
        text = preflight.STAGE_PROMPTS[stage]
        assert ("An action or skill step with any input that takes x and y (a stick, dpad, trigger, "
                "one-dimensional axis or mouse delta) needs a hold time") in text
        assert "even a single mouse-delta impulse: use a hold calibration tested for that control" in text
        assert "Only an action or step whose inputs are all buttons may give a null hold" in text
    rendered = preflight.schema(discovery)["$defs"]
    for model in ("Action", "SkillStep"):
        assert rendered[model]["properties"]["hold_s"]["description"] == preflight.HOLD_DESCRIPTION
    assert "Required when any input takes x and y" in preflight.HOLD_DESCRIPTION
    # The wording matches what review enforces: a null hold on an axis input is rejected.
    a = answer(stage_5=actions_part([action(inputs=[control_input("c2", 0.0, 1.0)], hold_s=None)]))
    assert any("an axis input needs a hold time" in p for p in preflight.review(a, "actions", discovery))


def test_retry_turn_lists_each_problem_on_its_own_line():
    text = preflight.retry_turn("actions", ["p1", "p2"])
    assert "actions" in text
    assert "- p1\n- p2" in text


def test_first_turn_puts_the_stage_instructions_last():
    text = preflight.first_turn("PACKET", "readers")
    assert text.startswith("PACKET")
    assert text.endswith(preflight.STAGE_PROMPTS["readers"])
