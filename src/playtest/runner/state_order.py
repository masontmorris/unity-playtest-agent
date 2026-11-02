"""An opt-in wire order for a System One request: turn-stable state first, the turn's news last.

A decision model renders `state`, then the question and its options, and scores the option labels in
one forward pass. Two independent reasons to say where each top-level key of `state` sits:

* a model reads the start and the end of a context better than its middle (Liu et al., "Lost in the
  Middle", TACL 2024), and the question comes immediately after `state`, so the values that changed
  this turn belong nearest the end;
* a provider that caches a prefix can only reuse the leading bytes that are identical from turn to
  turn, so the values that rarely change belong first.

`canonical` (`playtest.core.context`) sorts every key, so today the wire order of `state` is
alphabetical whatever order the payload was built in, and the first value that changes ends the shared
prefix. This module renders the same body with `state`'s top-level keys in the declared order, every
value still the canonical JSON it already was. It never drops, duplicates or rewrites a key or a value,
it leaves every value's own key order and nesting alone, and it does not touch `canonical`.

Nothing here decides, ranks, filters or suppresses: every value the payload carries is sent, once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from playtest.runner.wire import render

# Most turn-stable first, the turn's own news last. Each entry names why it sits where it does; the
# measured stability of every key on a real 97-turn session is in docs/design/s1-opt-state-order.md.
STATE_ORDER: tuple[str, ...] = (
    # The same bytes on every turn of every run.
    "instructions",
    # The same bytes on every turn of a run (a replan is a new objective, not a new turn).
    "objective",
    # Rebuilt only when the offered choices change; this is the largest stable block.
    "choices",
    # Stable between events: directives close, a helper's items are adopted, a run resets.
    "directive_history",
    "directive",
    "directive_provenance",
    "guidance",
    "recent_history",
    "reset_summary",
    # The turn's own news, nearest the question: which set this is, what was measured, the reading.
    "choice_set_version",
    "facts",
    "as_of",
    "game_view_status",
    "game_view_sha256",
    "game_view",
    "observation",
    "pending_help",
)


class StateOrder(str, Enum):
    """How a System One request lays out `state`'s top-level keys on the wire."""

    canonical = "canonical"  # sorted by key, as `canonical` renders it: the shipped body, byte for byte
    stable_first = "stable-first/1"


def state_keys(state: Mapping[str, Any], order: Sequence[str] = STATE_ORDER) -> tuple[str, ...]:
    """Every key of `state` exactly once: the declared order first, then any key it does not name."""
    declared = [key for key in order if key in state]
    named = set(order)
    return tuple(declared) + tuple(sorted(key for key in state if key not in named))


def ordered_text(body: Mapping[str, Any], *, order: Sequence[str] = STATE_ORDER) -> str:
    """The request text, with `state`'s top-level keys in `order` and every value still canonical.

    The text parses back to exactly the object `canonical(body)` states: same keys, same values, the
    same order inside every value. Only the order of `state`'s own keys differs, so a reader can
    rebuild both candidates from the payload alone and hash them to see which one was sent.

    A body whose `state` is not a mapping is rendered as `canonical` would render it.
    """
    return render(body, state=lambda value: state_keys(value, order))
