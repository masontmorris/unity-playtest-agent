"""An opt-in, lossless compact rendering of S1's `state`: the same payload in far fewer billed tokens.

A System One decision server renders an object `state` itself. Liquid's open d1 code writes it with
`json.dumps(state, indent=2)`, and the input tokens hosted `d1-ultra` reported for every call of the 31
October night sessions match that indented rendering (slope 1.001 against the d1 tokenizer; the compact
bytes we post fit at 1.30). A string `state` is written verbatim. So the indentation is paid on every
call, and so is every key name repeated in a list of records. This module renders the state as one
string instead: a header naming the rules applied, then compact JSON. The measured effect is in
`docs/design/s1-token-budget.md`.

Every rule is lossless: `decode` rebuilds the exact payload from the sent text and the question it was
asked with, and reads nothing else (no file, no trace, no original context). A rule that could not be
undone exactly for this payload is not applied, and the header lists only the rules that were applied:

* `instructions-ref`: `state.instructions` is omitted when it is the primary question's instructions.
* `labels-ref`: a choice's `label` is omitted when it is that question's `criteria` text for its id
  (off by default: whether the hosted renderer shows criteria descriptions is unverified).
* `history-tables`: `recent_history.turns.items`, `recent_history.action_attempts.items` and the probe's
  `samples` are written as `{"columns": [...], "rows": [[...], ...]}`, when every row has the same keys.
* `same-as-raw`: in the history tables, `"="` stands for the row's `raw_choice` in `effective_choice`,
  `executed_action` and `choice_id`.
* `effect-ref`: an `effect_fact` that begins with the displacement sentence the effect probe writes from
  the row's own `choice_id`, `expected_effect` and `observed_effect` keeps only its remainder, after `"@"`.
* `empty-omitted`: a choice's empty `params` and empty `preconditions` are omitted.

Nothing here drops, ranks, filters or rewrites a fact or a choice, and nothing here chooses. The traced
context payload is untouched: the adapter encodes a copy at the request boundary, and the request hash
covers the text it sends.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from types import SimpleNamespace
from typing import Any

from playtest.runner.probe import DisplacementProbe

VERSION = "compact-state/1"


class Rule(str, Enum):
    instructions = "instructions-ref"
    labels = "labels-ref"
    tables = "history-tables"
    same = "same-as-raw"
    effect = "effect-ref"
    empty = "empty-omitted"


DEFAULT_RULES: tuple[Rule, ...] = (Rule.instructions, Rule.tables, Rule.same, Rule.effect, Rule.empty)
ALL_RULES: tuple[Rule, ...] = tuple(Rule)

# What S1 reads in the header for each rule that changes how the JSON below it is read. Rules that only
# remove a second copy (instructions, labels) need a pointer, not an explanation.
_LEGEND = {
    Rule.instructions: "state.instructions is the question's instructions.",
    Rule.labels: "A choice with no label is described by its option in the question.",
    Rule.tables: 'A table is {"columns": [...], "rows": [[...], ...]}: one row per record, values in column order.',
    Rule.same: '"=" in effective_choice, executed_action or choice_id means the same as that row\'s raw_choice.',
    Rule.effect: ('An effect_fact starting "@" begins with that row\'s displacement sentence: its choice, '
                  'inspection seqs, measured and calibrated displacement, hold, sample variance and n.'),
    Rule.empty: "A choice with no params has params {}; with no preconditions, preconditions [].",
}
_SAME_COLUMNS = ("effective_choice", "executed_action", "choice_id")
_HISTORY_BLOCKS = ("turns", "action_attempts")


class CodecError(ValueError):
    """The text is not a compact state this version can restore exactly."""


# --------------------------------------------------------------------------- encode


def encode(state: Mapping[str, Any], *, instructions: str | None, criteria: Mapping[str, Any] | None,
           rules: Sequence[Rule | str] = DEFAULT_RULES, key_order: Sequence[str] | None = None) -> tuple[str, list[str]]:
    """The state text to send and the rules applied to it.

    `instructions` and `criteria` are the primary question's own, which `instructions-ref` and `labels-ref`
    point at. `key_order` lays out the state's top-level keys (the rest follow, in the payload's order);
    `None` keeps the payload's order. Raises `CodecError` for a state that is not finite JSON.
    """
    wanted = [Rule(rule) for rule in rules]
    state = copy.deepcopy(dict(state))
    applied: list[Rule] = []
    for rule in ALL_RULES:  # a fixed order, so the header and the decoder agree whatever the caller passed
        if rule in wanted and _APPLY[rule](state, instructions, criteria):
            applied.append(rule)
    order = [k for k in (key_order or ()) if k in state] + [k for k in state if k not in (key_order or ())]
    try:
        body = json.dumps({k: state[k] for k in order}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CodecError(f"the state is not finite JSON: {exc}") from None
    names = [rule.value for rule in applied]
    legend = " ".join(_LEGEND[rule] for rule in applied)
    header = f"{VERSION} rules={','.join(names) or 'none'}." + (f" {legend}" if legend else "")
    return f"{header}\n{body}", names


def _instructions(state: dict, instructions: str | None, _criteria: Any) -> bool:
    if instructions is None or state.get("instructions") != instructions:
        return False
    del state["instructions"]
    return True


def _labels(state: dict, _instructions: Any, criteria: Mapping[str, Any] | None) -> bool:
    choices = state.get("choices")
    if not isinstance(criteria, Mapping) or not isinstance(choices, list) or not all(
            isinstance(c, dict) and isinstance(c.get("label"), str) for c in choices):
        return False  # decoding restores a label wherever one is missing, so every choice must have had one
    done = False
    for choice in choices:
        if criteria.get(choice.get("id")) == choice["label"]:
            del choice["label"]
            done = True
    return done


def _tables(state: dict, _instructions: Any, _criteria: Any) -> bool:
    slots = _table_slots(state)
    if any(isinstance(parent[key], dict) and set(parent[key]) == {"columns", "rows"} for parent, key in slots):
        return False  # a slot that already looks like a table would be read back as one
    done = False
    for parent, key in slots:
        rows = parent[key]
        if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
            continue
        columns = list(rows[0])
        if any(list(r) != columns for r in rows):
            continue  # differing keys: a table could not say which were absent
        parent[key] = {"columns": columns, "rows": [[r[c] for c in columns] for r in rows]}
        done = True
    return done


def _table_slots(state: dict) -> list[tuple[dict, str]]:
    slots = []
    history = state.get("recent_history")
    if isinstance(history, dict):
        for block in _HISTORY_BLOCKS:
            if isinstance(history.get(block), dict) and "items" in history[block]:
                slots.append((history[block], "items"))
    observation = state.get("observation")
    public = observation.get("public") if isinstance(observation, dict) else None
    probe = public.get("probe") if isinstance(public, dict) else None
    if isinstance(probe, dict) and "samples" in probe:
        slots.append((probe, "samples"))
    return slots


def _history_tables(state: dict) -> list[dict]:
    history = state.get("recent_history")
    if not isinstance(history, dict):
        return []
    out = []
    for block in _HISTORY_BLOCKS:
        table = (history.get(block) or {}).get("items") if isinstance(history.get(block), dict) else None
        if isinstance(table, dict) and "columns" in table:
            out.append(table)
    return out


def _same(state: dict, _instructions: Any, _criteria: Any) -> bool:
    tables = _history_tables(state)
    # Exact only if no cell already holds "=": otherwise decoding could not tell the two apart.
    for table in tables:
        cols = table["columns"]
        if "raw_choice" not in cols:
            continue
        for row in table["rows"]:
            if row[cols.index("raw_choice")] == "=" or any(row[cols.index(c)] == "=" for c in _SAME_COLUMNS if c in cols):
                return False
    done = False
    for table in tables:
        cols = table["columns"]
        if "raw_choice" not in cols:
            continue
        raw = cols.index("raw_choice")
        for row in table["rows"]:
            for name in _SAME_COLUMNS:
                if name in cols and row[raw] is not None and row[cols.index(name)] == row[raw]:
                    row[cols.index(name)] = "="
                    done = True
    return done


def _sentence(choice_id: Any, expected: Any, observed: Any) -> str | None:
    """The effect probe's own displacement sentence for a row, or None when the row cannot give one."""
    if not isinstance(choice_id, str) or not isinstance(observed, Mapping):
        return None
    try:
        if observed.get("net_displacement") is None:
            return None
        return DisplacementProbe._displacement_fact(SimpleNamespace(id=choice_id), expected, observed)
    except (KeyError, TypeError, ValueError):
        return None


def _effect(state: dict, _instructions: Any, _criteria: Any) -> bool:
    history = state.get("recent_history")
    block = history.get("action_attempts") if isinstance(history, dict) else None
    table = block.get("items") if isinstance(block, dict) else None
    if not isinstance(table, dict) or "columns" not in table:
        return False  # the rule reads rows of the attempts table
    cols = table["columns"]
    if not {"effect_fact", "choice_id", "raw_choice", "expected_effect", "observed_effect"} <= set(cols):
        return False
    fact = cols.index("effect_fact")
    if any(isinstance(row[fact], str) and row[fact].startswith("@") for row in table["rows"]):
        return False  # an original "@" would be read back as a reference
    done = False
    for row in table["rows"]:
        text = row[fact]
        sentence = _sentence(_choice_id(cols, row), row[cols.index("expected_effect")], row[cols.index("observed_effect")])
        if isinstance(text, str) and sentence and text.startswith(sentence):
            row[fact] = "@" + text[len(sentence):]
            done = True
    return done


def _choice_id(cols: list[str], row: list[Any]) -> Any:
    value = row[cols.index("choice_id")]
    return row[cols.index("raw_choice")] if value == "=" else value


def _empty(state: dict, _instructions: Any, _criteria: Any) -> bool:
    choices = state.get("choices")
    if not isinstance(choices, list) or not choices or not all(
            isinstance(c, dict) and "params" in c and "preconditions" in c for c in choices):
        return False  # restoring needs every choice to have had both keys
    done = False
    for choice in choices:
        if choice["params"] == {}:
            del choice["params"]
            done = True
        if choice["preconditions"] == []:
            del choice["preconditions"]
            done = True
    return done


_APPLY = {Rule.instructions: _instructions, Rule.labels: _labels, Rule.tables: _tables, Rule.same: _same,
          Rule.effect: _effect, Rule.empty: _empty}


# --------------------------------------------------------------------------- decode


def decode(text: str, *, instructions: str | None, criteria: Mapping[str, Any] | None) -> dict[str, Any]:
    """The exact payload a compact state was encoded from, given the primary question it was sent with."""
    head, sep, body = text.partition("\n")
    prefix = f"{VERSION} rules="
    if not sep or not head.startswith(prefix):
        raise CodecError(f"not a {VERSION} state")
    names = head[len(prefix):].split(".", 1)[0]
    try:
        applied = [] if names == "none" else [Rule(name) for name in names.split(",")]
        state = json.loads(body)
    except ValueError as exc:
        raise CodecError(f"unreadable {VERSION} state: {exc}") from None
    if not isinstance(state, dict):
        raise CodecError("the state is not a JSON object")
    for rule in reversed(ALL_RULES):  # undo in the reverse of the order applied
        if rule in applied:
            _UNDO[rule](state, instructions, criteria)
    return state


def _undo_instructions(state: dict, instructions: str | None, _criteria: Any) -> None:
    if instructions is None or "instructions" in state:
        raise CodecError("instructions-ref needs the question's instructions and no state.instructions")
    state["instructions"] = instructions


def _undo_labels(state: dict, _instructions: Any, criteria: Mapping[str, Any] | None) -> None:
    if not isinstance(criteria, Mapping):
        raise CodecError("labels-ref needs the question's criteria")
    for choice in state.get("choices") or []:
        if isinstance(choice, dict) and "label" not in choice:
            if choice.get("id") not in criteria:
                raise CodecError(f"no criteria text for choice {choice.get('id')!r}")
            choice["label"] = criteria[choice["id"]]


def _undo_tables(state: dict, _instructions: Any, _criteria: Any) -> None:
    for parent, key in _table_slots(state):
        table = parent[key]
        if isinstance(table, dict) and set(table) == {"columns", "rows"}:
            cols = table["columns"]
            if any(len(row) != len(cols) for row in table["rows"]):
                raise CodecError("a table row does not match its columns")
            parent[key] = [dict(zip(cols, row)) for row in table["rows"]]


def _undo_same(state: dict, _instructions: Any, _criteria: Any) -> None:
    for table in _history_tables(state):
        cols = table["columns"]
        if "raw_choice" not in cols:
            continue
        raw = cols.index("raw_choice")
        for row in table["rows"]:
            for name in _SAME_COLUMNS:
                if name in cols and row[cols.index(name)] == "=":
                    row[cols.index(name)] = row[raw]


def _undo_effect(state: dict, _instructions: Any, _criteria: Any) -> None:
    history = state.get("recent_history") or {}
    table = (history.get("action_attempts") or {}).get("items")
    if not isinstance(table, dict) or "columns" not in table:
        raise CodecError("effect-ref needs the attempts table")
    cols = table["columns"]
    fact = cols.index("effect_fact")
    for row in table["rows"]:
        text = row[fact]
        if isinstance(text, str) and text.startswith("@"):
            # Read before same-as-raw is undone, so a "=" choice id is resolved from the row here.
            sentence = _sentence(_choice_id(cols, row), row[cols.index("expected_effect")],
                                 row[cols.index("observed_effect")])
            if sentence is None:
                raise CodecError("an effect reference whose row gives no sentence")
            row[fact] = sentence + text[1:]


def _undo_empty(state: dict, _instructions: Any, _criteria: Any) -> None:
    for choice in state.get("choices") or []:
        if isinstance(choice, dict):
            choice.setdefault("params", {})
            choice.setdefault("preconditions", [])


_UNDO = {Rule.instructions: _undo_instructions, Rule.labels: _undo_labels, Rule.tables: _undo_tables,
         Rule.same: _undo_same, Rule.effect: _undo_effect, Rule.empty: _undo_empty}
