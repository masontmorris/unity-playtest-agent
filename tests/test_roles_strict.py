"""The strict-schema renderer, checker and parser (#73).

These pin the provider-facing contract: the exact JSON shape a model class renders into, the
run-stable byte identity that cache reuse depends on, every way `check` rejects a schema, and the
rule that `parse` reports problems instead of repairing an answer.
"""

from __future__ import annotations

import json
from typing import Literal

import pytest
from pydantic import Field

from playtest.contracts.common import Strict
from playtest.roles.strict import (
    DEFAULT_KEEP,
    MUSE_ENFORCED,
    Limits,
    canonical,
    check,
    enum_str,
    parse,
    render,
    stats,
)

ENUMS = {"names": ["alpha", "beta"]}


class InnerModel(Strict):
    flag: bool
    note: str = Field(min_length=1, description="inner note")


class SampleModel(Strict):
    kind: Literal["only"]
    ratio: float | None
    inner: InnerModel | None
    names: enum_str("names")
    alias: enum_str("names")
    tags: list[str] = Field(max_length=3)
    score: float = Field(ge=0.0, le=1.0)
    code: str = Field(pattern=r"^[a-z]+$", description="lowercase")


class BoundedModel(Strict):
    count: int = Field(ge=0, le=9)
    limited: int = Field(gt=0, lt=10)
    items: list[str] = Field(min_length=1, max_length=2)
    code: str = Field(pattern=r"^[a-z]+$", min_length=1)


def _walk(node):
    """Every dict and list nested anywhere in a schema."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _objects(schema):
    return [node for node in _walk(schema) if "properties" in node]


def test_root_and_every_object_are_closed():
    schema = render(SampleModel, ENUMS)
    assert schema["type"] == "object"
    objects = _objects(schema)
    assert len(objects) >= 2  # the root and InnerModel
    for node in objects:
        assert node["additionalProperties"] is False
        assert sorted(node["required"]) == sorted(node["properties"])
    for node in _walk(schema):
        assert "title" not in node
        assert "default" not in node


def test_single_literal_renders_as_enum_without_const():
    schema = render(SampleModel, ENUMS)
    assert schema["properties"]["kind"] == {"type": "string", "enum": ["only"]}
    for node in _walk(schema):
        assert "const" not in node


def test_nullable_float_and_nested_model_shapes():
    schema = render(SampleModel, ENUMS)
    assert schema["properties"]["ratio"] == {"type": ["number", "null"]}
    assert schema["properties"]["inner"] == {
        "anyOf": [{"$ref": "#/$defs/InnerModel"}, {"type": "null"}],
    }


def test_shared_enums_put_values_in_defs_and_reference_them():
    schema = render(SampleModel, ENUMS)
    assert schema["$defs"]["names"] == {"type": "string", "enum": ["alpha", "beta"]}
    for field in ("names", "alias"):
        assert schema["properties"][field] == {"$ref": "#/$defs/names"}


def test_shared_enums_false_repeats_values_at_each_use():
    schema = render(SampleModel, ENUMS, shared_enums=False)
    assert "names" not in schema.get("$defs", {})
    for field in ("names", "alias"):
        assert schema["properties"][field] == {"type": "string", "enum": ["alpha", "beta"]}


def test_inline_refs_leaves_no_ref_or_defs():
    schema = render(SampleModel, ENUMS, inline_refs=True)
    for node in _walk(schema):
        assert "$ref" not in node
        assert "$defs" not in node
    for field in ("names", "alias"):
        assert schema["properties"][field] == {"type": "string", "enum": ["alpha", "beta"]}


@pytest.mark.parametrize(
    "enums",
    [{}, {"names": []}, {"names": ["alpha", "alpha"]}],
    ids=["missing", "empty", "duplicate"],
)
def test_render_rejects_unusable_enum_values(enums):
    with pytest.raises(ValueError):
        render(SampleModel, enums)


def test_default_keep_is_max_items_and_pattern():
    assert DEFAULT_KEEP == frozenset({"maxItems", "pattern"})


def test_muse_enforced_records_the_measured_keywords():
    assert {"maxItems", "pattern", "minimum"} <= MUSE_ENFORCED


def test_default_keep_keeps_list_caps_and_patterns_but_strips_bounds():
    props = render(BoundedModel)["properties"]
    assert props["items"]["maxItems"] == 2
    assert props["code"]["pattern"] == "^[a-z]+$"
    for field, keyword in [
        ("count", "minimum"),
        ("count", "maximum"),
        ("limited", "exclusiveMinimum"),
        ("limited", "exclusiveMaximum"),
        ("items", "minItems"),
        ("code", "minLength"),
    ]:
        assert keyword not in props[field]


def test_bounds_are_present_only_when_kept():
    keep = frozenset({"maxItems", "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                      "minItems", "minLength"})
    props = render(BoundedModel, keep=keep)["properties"]
    assert props["count"]["minimum"] == 0
    assert props["count"]["maximum"] == 9
    assert props["limited"]["exclusiveMinimum"] == 0
    assert props["limited"]["exclusiveMaximum"] == 10
    assert props["items"]["minItems"] == 1
    assert props["items"]["maxItems"] == 2
    assert props["code"]["minLength"] == 1
    assert props["code"]["pattern"] == "^[a-z]+$"


def test_constraint_keywords_are_kept_when_listed():
    keep = frozenset({"maxItems", "minimum", "maximum", "pattern", "minLength"})
    schema = render(SampleModel, ENUMS, keep=keep)
    props = schema["properties"]
    assert props["tags"]["maxItems"] == 3
    assert props["score"]["minimum"] == 0
    assert props["score"]["maximum"] == 1
    assert props["code"]["pattern"] == "^[a-z]+$"
    assert schema["$defs"]["InnerModel"]["properties"]["note"]["minLength"] == 1


def test_descriptions_can_be_removed_everywhere():
    with_descriptions = render(SampleModel, ENUMS)
    assert any("description" in node for node in _walk(with_descriptions))
    without = render(SampleModel, ENUMS, descriptions=False)
    for node in _walk(without):
        assert "description" not in node


def test_canonical_text_is_byte_identical_and_tracks_enum_values():
    first = canonical(render(SampleModel, ENUMS))
    second = canonical(render(SampleModel, dict(ENUMS)))
    changed = canonical(render(SampleModel, {"names": ["alpha", "gamma"]}))
    assert first == second
    assert changed != first


def _root(properties, **extra):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
        **extra,
    }


VALID = _root({"a": {"type": "string"}})

BOUNDED = _root({"a": {"type": "number", "minimum": 0}})


def _object(name):
    return {
        "type": "object",
        "properties": {name: {"type": "string"}},
        "required": [name],
        "additionalProperties": False,
    }


OBJECT_UNION = _root({"a": {"anyOf": [_object("x"), _object("y")]}})
REF_UNION = _root(
    {"a": {"anyOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}]}},
    **{"$defs": {"A": _object("x"), "B": _object("y")}},
)
OBJECT_OR_NULL = _root(
    {"a": {"anyOf": [{"$ref": "#/$defs/A"}, {"type": "null"}]}},
    **{"$defs": {"A": _object("x")}},
)

ENUM_SCHEMA = _root(
    {f"p{i}": {"$ref": "#/$defs/e"} for i in range(3)},
    **{"$defs": {"e": {"type": "string", "enum": ["a", "b", "c"]}}},
)

DEEP = _root(
    {
        "a": _root(
            {
                "b": _root({"c": {"type": "string"}}),
            }
        )
    }
)

RECURSIVE = _root(
    {"a": {"$ref": "#/$defs/A"}},
    **{
        "$defs": {
            "A": _root({"self": {"$ref": "#/$defs/A"}}),
        }
    },
)

INVALID_SCHEMAS = [
    ("root_anyOf", dict(VALID, anyOf=[{"type": "null"}]), "anyOf"),
    (
        "object_without_additionalProperties",
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        "additionalProperties must be false",
    ),
    (
        "required_missing_property",
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": [], "additionalProperties": False},
        "required must list every property",
    ),
    (
        "free_form_map",
        _root({"m": {"type": "object", "additionalProperties": False}}),
        "free-form maps",
    ),
    ("oneOf_nested", _root({"a": {"oneOf": [{"type": "string"}]}}), "oneOf"),
    ("allOf_nested", _root({"a": {"allOf": [{"type": "string"}]}}), "allOf"),
    (
        "unresolved_ref",
        _root({"a": {"$ref": "#/$defs/missing"}}),
        "unresolved $ref",
    ),
    ("recursive_ref", RECURSIVE, "recursive $ref"),
]


def test_check_accepts_a_valid_schema():
    assert check(VALID) == []


def test_check_strips_value_bounds_by_default():
    assert any("minimum" in p for p in check(BOUNDED))
    assert check(BOUNDED, keep=frozenset({"minimum"})) == []


@pytest.mark.parametrize("schema", [OBJECT_UNION, REF_UNION], ids=["inline", "refs"])
def test_check_rejects_an_any_of_of_objects(schema):
    problems = check(schema)
    assert any("anyOf of objects" in p for p in problems)


def test_check_allows_an_object_or_null_any_of():
    assert check(OBJECT_OR_NULL) == []


@pytest.mark.parametrize(
    "schema, needle",
    [(schema, needle) for _, schema, needle in INVALID_SCHEMAS],
    ids=[name for name, _, _ in INVALID_SCHEMAS],
)
def test_check_reports_each_invalid_shape(schema, needle):
    problems = check(schema)
    assert problems
    assert any(needle in p for p in problems)


def test_enum_limit_defaults_to_counting_definitions():
    assert Limits().enum_counting == "definitions"
    expanded = check(ENUM_SCHEMA, Limits(enum_values=5, enum_counting="expanded"))
    assert any("enum values (expanded)" in p for p in expanded)
    assert check(ENUM_SCHEMA, Limits(enum_values=5)) == []


def test_nesting_depth_limit():
    assert check(DEEP, Limits(depth=2))
    assert any("nesting depth" in p for p in check(DEEP, Limits(depth=2)))


def test_stats_counts_shared_enum_at_definitions_and_uses():
    counts = stats(ENUM_SCHEMA)
    assert counts.enum_values_definitions == 3
    assert counts.enum_values_expanded == 9
    assert counts.largest_enum == 3


@pytest.mark.parametrize(
    "content",
    [None, "", "   ", "not json", '{"kind": "wrong"}'],
    ids=["none", "empty", "blank", "not_json", "wrong_model"],
)
def test_parse_reports_instead_of_raising(content):
    value, errors = parse(SampleModel, content)
    assert value is None
    assert errors
    assert all(isinstance(e, str) for e in errors)


def test_parse_returns_the_model_for_a_valid_answer():
    body = {
        "kind": "only",
        "ratio": None,
        "inner": None,
        "names": "alpha",
        "alias": "beta",
        "tags": [],
        "score": 0.5,
        "code": "abc",
    }
    value, errors = parse(SampleModel, json.dumps(body))
    assert errors == []
    assert isinstance(value, SampleModel)
    assert value.names == "alpha"
