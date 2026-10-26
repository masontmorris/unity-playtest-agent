"""Render answer models into the provider's strict JSON Schema subset, and check the result.

Muse strict mode, from the provider docs (read 26 Oct 2026) and the Day-1 probes:

- The root is a plain object. `anyOf` is allowed below the root; `oneOf`, `allOf` and `not` are
  not allowed anywhere.
- Every object sets `additionalProperties: false` and lists every property in `required`, so an
  optional value is a nullable one. Free-form maps are impossible.
- Recursion is rejected. Limits: nesting depth 10, 5,000 properties, 120,000 characters of
  names and enum/const values, 1,000 enum values, a single enum over 250 values capped at 15,000
  characters, and 200,000 nodes once `$ref`s are inlined.
- Decoding is constrained by the schema, and the schema's text is billed as prompt tokens.

Measured on 26 Oct 2026 (`scripts/probes/probe_schemas.py --phase features`, #73):

- The server canonicalizes a schema before using it. `$ref`s are expanded, so a shared `$defs`
  enum bills the same prompt tokens as repeating it inline (about 3 tokens per enum value per use).
- Objects are emitted in sorted key order, not schema order. In an `anyOf` of objects the first
  sorted key picks the branch: asked for a box (keys h, kind, w), the model wrote `kind` first and
  so could only finish the circle branch (kind, radius), 5 of 5 times. `check` rejects unions of
  objects; use one flat object or nullable parts.
- Every constraint keyword tried was enforced while decoding, by coercion rather than rejection:
  `minLength` padded "hi" with dots, `maximum` turned 500 into 10, `minItems` invented items. A
  coerced value is a repair nobody sees, so `DEFAULT_KEEP` sends only `maxItems` (the model's own
  list, cut at the cap) and `pattern` (identifier spelling). Other bounds are checked after
  decoding, where a violation stays visible.
- Nesting deeper than 10 objects is rejected; `oneOf` is rejected. One enum of 1,001 values and two
  enums of 600 each were accepted, so the documented 1,000-value cap was not reached.

A model class cannot know a project's discovered ids or a run's manifest names. A field declares
`enum_str("<name>")` and `render` substitutes the values. Rendering follows model field order and
sorts nothing else, so the same model and enums always give byte-identical JSON. That property is
what lets one schema serve a whole chain of calls without breaking provider cache reuse (#70).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, Field, ValidationError

ENUM_MARK = "x-enum"

# Keywords that only constrain values. They are stripped from a provider schema unless listed in
# `keep`; the answer models still enforce them when code validates the decoded answer.
CONSTRAINT_KEYWORDS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern", "format",
})

# Constraint keywords Muse accepted and enforced while decoding (#73 feature probe, 26 Oct 2026).
MUSE_ENFORCED = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems", "minLength", "maxLength",
    "pattern",
})
# The ones a provider schema carries by default: enforcing them cannot silently change a value's meaning.
DEFAULT_KEEP = frozenset({"maxItems", "pattern"})

_STRUCTURE = frozenset({
    "type", "description", "enum", "properties", "required", "additionalProperties", "items", "anyOf",
    "$ref", "$defs",
})
_DROP = frozenset({"title", "default", "examples", "discriminator"})
_KEY_ORDER = ("type", "description", "enum", "properties", "required", "additionalProperties", "items",
              "anyOf", "$ref", "$defs")


def enum_str(name: str) -> Any:
    """A string type whose allowed values are supplied to `render` under `name`."""
    return Annotated[str, Field(json_schema_extra={ENUM_MARK: name})]


@dataclass(frozen=True)
class Limits:
    """Provider limits. `enum_counting` says whether a shared enum counts once or at every use."""

    depth: int = 10
    properties: int = 5_000
    string_chars: int = 120_000
    enum_values: int = 1_000
    big_enum: int = 250
    big_enum_chars: int = 15_000
    expanded_nodes: int = 200_000
    enum_counting: str = "definitions"  # or "expanded"; 2,094 expanded and 1,001 unique values were accepted


MUSE_LIMITS = Limits()


def render(
    model: type[BaseModel],
    enums: Mapping[str, Sequence[str]] | None = None,
    *,
    keep: frozenset[str] = DEFAULT_KEEP,
    descriptions: bool = True,
    shared_enums: bool = True,
    inline_refs: bool = False,
    limits: Limits | None = MUSE_LIMITS,
) -> dict[str, Any]:
    """The strict provider schema for `model`, with run-stable enums filled in.

    `shared_enums` puts each injected enum in `$defs` once; otherwise its values are repeated at
    every use. `inline_refs` removes `$defs` entirely. Raises ValueError if the result breaks the
    strict subset or `limits` (pass `limits=None` to skip the limit check, e.g. to probe a limit).
    """
    enums = dict(enums or {})
    raw = model.model_json_schema(mode="validation")
    raw_defs = raw.pop("$defs", {})
    used: dict[str, list[str]] = {}

    def values_for(name: str) -> list[str]:
        values = [str(v) for v in enums.get(name) or ()]
        if not values:
            raise ValueError(f"no values supplied for enum {name!r}")
        if len(set(values)) != len(values):
            raise ValueError(f"enum {name!r} has duplicate values")
        used[name] = values
        return values

    def conv(node: Any) -> Any:
        if isinstance(node, list):
            return [conv(n) for n in node]
        if not isinstance(node, dict):
            return node
        if ENUM_MARK in node:
            values = values_for(node[ENUM_MARK])
            if shared_enums:
                return {"$ref": f"#/$defs/{node[ENUM_MARK]}"}
            node = {k: v for k, v in node.items() if k != ENUM_MARK} | {"enum": values}
        if "allOf" in node and len(node["allOf"]) == 1:  # Pydantic's wrapper for a described $ref
            node = {k: v for k, v in node.items() if k != "allOf"} | node["allOf"][0]
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DROP or (key == "description" and not descriptions):
                continue
            if key in CONSTRAINT_KEYWORDS and key not in keep:
                continue
            if key == "const":
                out["enum"] = [value]
            elif key == "oneOf":
                out["anyOf"] = conv(value)
            elif key == "properties":
                out[key] = {name: conv(sub) for name, sub in value.items()}
            else:
                out[key] = conv(value)
        if "$ref" in out:  # a sibling next to $ref is not portable; keep the reference alone
            return {"$ref": out["$ref"]}
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}))
        if "anyOf" in out:
            out = _compress_primitives(out)
        return _ordered(out)

    root = conv(raw)
    defs = {name: conv(sub) for name, sub in raw_defs.items()}
    for name, values in used.items():
        if shared_enums:
            defs[name] = {"type": "string", "enum": values}
    if inline_refs:
        root = _inline(root, defs, ())
    else:
        referenced = sorted(_refs(root, defs))
        if referenced:
            root["$defs"] = {name: defs[name] for name in referenced}
    if limits is not None:
        problems = check(root, limits, keep=keep)
        if problems:
            raise ValueError("schema breaks the strict subset:\n" + "\n".join(problems))
    return root


def response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The Chat Completions `response_format` body for a rendered schema."""
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


def canonical(obj: Any) -> str:
    """The exact JSON text a client sends for `obj`; equal text means a byte-identical request part."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def parse(model: type[BaseModel], content: str | None) -> tuple[BaseModel | None, list[str]]:
    """Decode an answer into `model`. Returns the answer or exact error strings, never a repair."""
    if not isinstance(content, str) or not content.strip():
        return None, ["empty answer"]
    try:
        data = json.loads(content)
    except ValueError as exc:
        return None, [f"answer is not JSON: {exc}"]
    try:
        return model.model_validate(data), []
    except ValidationError as exc:
        return None, [f"{_loc(e['loc'])}: {e['msg']}" for e in exc.errors()]


# --- checking ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stats:
    depth: int
    properties: int
    enum_values_definitions: int
    enum_values_expanded: int
    largest_enum: int
    largest_enum_chars: int
    string_chars: int
    expanded_nodes: int


def stats(schema: dict[str, Any]) -> Stats:
    defs = schema.get("$defs", {})
    definition_enums = sum(len(n.get("enum", ())) for _, n in _walk(schema))
    expanded = _inline({k: v for k, v in schema.items() if k != "$defs"}, defs, ())
    nodes = list(_walk(expanded))
    enums = [n["enum"] for _, n in nodes if "enum" in n]
    chars = sum(len(name) for _, n in nodes for name in n.get("properties", {}))
    chars += sum(len(name) for name in defs)
    chars += sum(len(str(v)) for e in enums for v in e)
    return Stats(
        depth=_depth(expanded),
        properties=sum(len(n.get("properties", {})) for _, n in nodes),
        enum_values_definitions=definition_enums,
        enum_values_expanded=sum(len(e) for e in enums),
        largest_enum=max((len(e) for e in enums), default=0),
        largest_enum_chars=max((sum(len(str(v)) for v in e) for e in enums), default=0),
        string_chars=chars,
        expanded_nodes=len(nodes),
    )


def check(schema: dict[str, Any], limits: Limits | None = MUSE_LIMITS, *,
          keep: frozenset[str] = DEFAULT_KEEP) -> list[str]:
    """Every way `schema` breaks the strict subset or the provider limits; empty when it is valid."""
    problems: list[str] = []
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        problems.append("$: the root must be an object with properties")
    for key in ("anyOf", "oneOf", "allOf", "enum", "not"):
        if key in schema:
            problems.append(f"$: '{key}' is not allowed at the root")
    defs = schema.get("$defs", {})
    allowed = _STRUCTURE | keep
    for path, node in _walk(schema):
        for key in node:
            if key not in allowed:
                problems.append(f"{path}: keyword '{key}' is not in the supported subset")
        if "$ref" in node:
            name = node["$ref"].removeprefix("#/$defs/")
            if name not in defs:
                problems.append(f"{path}: unresolved $ref {node['$ref']}")
        if "anyOf" in node:
            branches = [_inline(b, defs, ()) if "$ref" in b and b["$ref"].removeprefix("#/$defs/") in defs else b
                        for b in node["anyOf"]]
            if sum(1 for b in branches if isinstance(b, dict) and b.get("type") == "object") > 1:
                problems.append(f"{path}: an anyOf of objects decodes by the first sorted key; use one object")
        if node.get("type") == "object" or "properties" in node:
            props = node.get("properties")
            if not isinstance(props, dict) or not props:
                problems.append(f"{path}: an object needs named properties (no free-form maps)")
                props = {}
            if node.get("additionalProperties") is not False:
                problems.append(f"{path}: additionalProperties must be false")
            if sorted(node.get("required", ())) != sorted(props):
                problems.append(f"{path}: required must list every property")
    try:
        _inline({k: v for k, v in schema.items() if k != "$defs"}, defs, ())
    except ValueError as exc:
        problems.append(f"$: {exc}")
        return problems
    if limits is not None:
        s = stats(schema)
        enum_count = s.enum_values_expanded if limits.enum_counting == "expanded" else s.enum_values_definitions
        for value, limit, what in (
            (s.depth, limits.depth, "nesting depth"),
            (s.properties, limits.properties, "properties"),
            (s.string_chars, limits.string_chars, "characters of names and enum values"),
            (enum_count, limits.enum_values, f"enum values ({limits.enum_counting})"),
            (s.expanded_nodes, limits.expanded_nodes, "expanded nodes"),
        ):
            if value > limit:
                problems.append(f"$: {what} {value} exceeds the limit of {limit}")
        if s.largest_enum > limits.big_enum and s.largest_enum_chars > limits.big_enum_chars:
            problems.append(f"$: an enum of {s.largest_enum} values has {s.largest_enum_chars} characters, "
                            f"over {limits.big_enum_chars}")
    return problems


# --- helpers -------------------------------------------------------------------------------------


def _ordered(node: dict[str, Any]) -> dict[str, Any]:
    head = {k: node[k] for k in _KEY_ORDER if k in node}
    return head | {k: v for k, v in node.items() if k not in head}


def _compress_primitives(node: dict[str, Any]) -> dict[str, Any]:
    """anyOf over bare primitive types becomes one `type` list: fewer schema tokens, same meaning."""
    branches = node["anyOf"]
    if all(isinstance(b, dict) and set(b) == {"type"} and isinstance(b["type"], str) for b in branches):
        out = {k: v for k, v in node.items() if k != "anyOf"}
        out["type"] = [b["type"] for b in branches]
        return out
    return node


def _walk(node: Any, path: str = "$") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for name, sub in value.items():
                    yield from _walk(sub, f"{path}.{name}" if key == "properties" else f"{path}.$defs.{name}")
            elif key == "items":
                yield from _walk(value, f"{path}[]")
            elif key == "anyOf":
                for i, sub in enumerate(value):
                    yield from _walk(sub, f"{path}|{i}")


def _refs(node: Any, defs: dict[str, Any], seen: tuple[str, ...] = ()) -> set[str]:
    found: set[str] = set()
    for _, sub in _walk(node):
        if "$ref" in sub:
            name = sub["$ref"].removeprefix("#/$defs/")
            if name not in found and name not in seen and name in defs:
                found.add(name)
                found |= _refs(defs[name], defs, (*seen, name))
    return found


def _inline(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_inline(n, defs, stack) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].removeprefix("#/$defs/")
        if name in stack:
            raise ValueError(f"recursive $ref through {' -> '.join((*stack, name))}")
        if name not in defs:
            raise ValueError(f"unresolved $ref {node['$ref']}")
        return _inline(defs[name], defs, (*stack, name))
    return {k: _inline(v, defs, stack) for k, v in node.items() if k != "$defs"}


def _depth(node: Any) -> int:
    if not isinstance(node, dict):
        return 0
    here = 1 if node.get("type") in ("object", "array") or "properties" in node or "items" in node else 0
    children = [*node.get("properties", {}).values(), *node.get("anyOf", ())]
    if "items" in node:
        children.append(node["items"])
    return here + max((_depth(c) for c in children), default=0)


def _loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "answer"
