"""The exact text of a System One request when an opt-in presentation order is on.

`canonical` (`playtest.core.context`) sorts every key, so by default the provider sees a question's
options and `state`'s top-level keys in alphabetical order. Two opt-in options choose those orders
instead: `playtest.runner.option_order` for each question's `criteria`, `playtest.runner.state_order` for
`state`'s top-level keys. Both render through `render` here, so the two can be combined and the text the
adapter hashes is the text it sends.

The rendered text parses back to exactly the object `canonical(body)` states. Only the two named key
orders can differ; every other mapping is sorted and every value is canonical JSON. Nothing is dropped,
duplicated or rewritten, and `canonical` itself is untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from playtest.core.context import canonical


def render(body: Mapping[str, Any], *, criteria: Mapping[str, Sequence[str]] | None = None,
           state: Callable[[Mapping[str, Any]], Sequence[str]] | None = None) -> str:
    """`canonical(body)`, except for each question's `criteria` order and `state`'s top-level key order.

    `criteria` maps a question name to its option ids in presentation order and must name every id of
    that question exactly once. `state` returns `state`'s top-level keys, each exactly once, in order.
    Either may be `None` to keep that part sorted. Raises `ValueError` when an order does not cover its
    keys exactly once.
    """

    def ordered(value: Mapping[str, Any], keys: Sequence[str], what: str) -> str:
        if len(keys) != len(value) or set(keys) != set(value):
            raise ValueError(f"{what} order must contain each key exactly once")
        return "{" + ",".join(f"{canonical(k)}:{canonical(value[k])}" for k in keys) + "}"

    def encode(value: Any, path: tuple[str, ...]) -> str:
        if not isinstance(value, Mapping):
            return canonical(value)
        if path == ("state",) and state is not None:
            return ordered(value, state(value), "state")
        if len(path) == 3 and path[0] == "questions" and path[2] == "criteria" and criteria is not None:
            return ordered(value, criteria.get(path[1], ()), f"{path[1]} criteria")
        if path in ((), ("questions",)) or (len(path) == 2 and path[0] == "questions"):
            return "{" + ",".join(f"{canonical(k)}:{encode(value[k], path + (k,))}" for k in sorted(value)) + "}"
        return canonical(value)

    return encode(body, ())
