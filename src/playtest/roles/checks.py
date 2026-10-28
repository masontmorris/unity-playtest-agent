"""Shared rules for checks: one field, one comparator, one value (build bible 5.6).

Directive setpoints, preconditions, invariants and finding predicates all compare one observed
field with a value, so code can score them. The comparator semantics live here so every role's
validation agrees:

- `changed` / `unchanged` take no value. They compare with a baseline the caller defines: the
  previous sample for a rule, the value when S1 received the directive for a setpoint.
- `<`, `<=`, `>`, `>=` need a number and a numeric field.
- `==` / `!=` need a value of the field's kind.
- a tolerance applies only to numeric `==` / `!=`.

Field kinds come from discovery's declared types (preflight) or the manifest (frontier). A kind of
"vector" or "other" supports only `changed` / `unchanged`: there is no scalar to compare.
"""

from __future__ import annotations

from typing import Any, Literal

from playtest.contracts.investigation import Comparator

ValueKind = Literal["bool", "number", "string", "vector", "other"]

_NUMBER_TYPES = frozenset({
    "int", "float", "double", "long", "short", "byte", "sbyte", "uint", "ulong", "ushort", "decimal",
    "Int32", "Int64", "Single", "Double",
})
_STRING_TYPES = frozenset({"string", "String", "char"})
_BOOL_TYPES = frozenset({"bool", "Boolean"})
_VECTOR_TYPES = frozenset({
    "Vector2", "Vector3", "Vector4", "Vector2Int", "Vector3Int", "Quaternion", "Color", "Color32", "Rect",
    "Bounds",
})
# The Editor's reflection collector emits Type.FullName, not C# aliases. Match
# only known framework types; stripping an arbitrary namespace could misclassify
# a user-defined type with the same short name.
_FULL_TYPES: dict[str, ValueKind] = {
    **{f"System.{name}": "number" for name in (
        "Int16", "Int32", "Int64", "UInt16", "UInt32", "UInt64", "Byte", "SByte",
        "Single", "Double", "Decimal")},
    "System.Boolean": "bool", "System.String": "string", "System.Char": "string",
    **{f"UnityEngine.{name}": "vector" for name in _VECTOR_TYPES},
}
ORDERING = frozenset({Comparator.lt, Comparator.le, Comparator.gt, Comparator.ge})
CHANGE = frozenset({Comparator.changed, Comparator.unchanged})


def value_kind(type_name: str | None) -> ValueKind:
    """The comparable kind of a declared field type. Unknown types are 'other', not guessed."""
    if not type_name:
        return "other"
    if type_name in _FULL_TYPES:
        return _FULL_TYPES[type_name]
    if type_name in _BOOL_TYPES or type_name == "boolean":
        return "bool"
    if type_name in _NUMBER_TYPES or type_name in ("number", "integer"):
        return "number"
    if type_name in _STRING_TYPES:
        return "string"
    if type_name in _VECTOR_TYPES or type_name == "vector":
        return "vector"
    return "other"


def check_problems(where: str, kind: ValueKind, op: Comparator, value: Any,
                   tolerance: float | None = None) -> list[str]:
    """Why a check cannot be scored as written; empty when it can."""
    problems: list[str] = []
    if op in CHANGE:
        if value is not None:
            problems.append(f"{where}: '{op.value}' takes value null")
    elif value is None:
        problems.append(f"{where}: '{op.value}' needs a value")
    elif kind in ("vector", "other"):
        problems.append(f"{where}: a {kind} field supports only changed/unchanged")
    elif op in ORDERING:
        if kind != "number" or not _is_number(value):
            problems.append(f"{where}: '{op.value}' needs a numeric field and a number")
    elif kind == "number" and not _is_number(value):
        problems.append(f"{where}: a numeric field compares with a number, not {value!r}")
    elif kind == "bool" and not isinstance(value, bool):
        problems.append(f"{where}: a bool field compares with true or false, not {value!r}")
    elif kind == "string" and not isinstance(value, str):
        problems.append(f"{where}: a string field compares with a string, not {value!r}")
    if tolerance is not None:
        if tolerance < 0:
            problems.append(f"{where}: tolerance cannot be negative")
        if kind != "number" or op not in (Comparator.eq, Comparator.ne):
            problems.append(f"{where}: a tolerance applies only to numeric == or !=")
    return problems


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
