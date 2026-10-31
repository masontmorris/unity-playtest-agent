"""Layout of the frontier's adopted guidance for S1's context (#170).

An adopted frontier answer stays in S1's context for the rest of the run, with every hypothesis it lists in
full, so the same hypotheses recur once per answer. `render` lays the same adopted items out once per fact:

- `current_hypotheses`: the latest complete row of every hypothesis id, with the `requested_turn` of the answer
  that stated it.
- `earlier_versions`: every older row of those ids, newest first. A row states only the fields that differ from
  the next newer version, and `other_fields_same_as_requested_turn: N` says that every field it does not state
  equals the version stated at request turn N.
- `help_adoptions`: the chronological ledger. Every item keeps its source, kind, choice id, turns, `age_s`,
  `waited_s` and result as adopted; an item whose rows moved to the first two blocks names them in
  `hypothesis_ids`, in the order the frontier listed them.

Nothing is dropped, ranked, summarised or reworded. An old statement, condition, citation or negative stays in
S1's view. Values are compared as canonical JSON, so `1`, `1.0` and `true` differ, and `null`, an empty list and
a missing field are three different things: a field is unchanged only where the marker says so, and a null is
never carried by the marker, so it is stated wherever it occurs. Items from any other source, and frontier
results this module does not recognise (an error, a cap, a replacement string, an unexpected row shape), are
carried into the ledger byte for byte and never join a hypothesis chain.

`restore` is the exact inverse. The turn loop sends a rendering only after `restore` has given back the list it
was built from, so a fault here can make a context larger, never lose information.

This module sees only the frontier's own answer vocabulary: no game, field name or genre word.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from playtest.core.context import canonical

SOURCE = "frontier"
FIELDS = ("status", "statement", "confirm_if", "refute_if", "supporting_seq", "contradicting_seq", "what_one_would_expect")
ROW_KEYS = frozenset(("id", *FIELDS))
LEDGER_KEY = "hypothesis_ids"  # on a lifted ledger entry only; an adopted item never has it
CURRENT, EARLIER, LEDGER = "current_hypotheses", "earlier_versions", "help_adoptions"
REF_KEY = "other_fields_same_as_requested_turn"  # on an earlier version only; an adopted row never has it


class CodecError(ValueError):
    """The input cannot be carried exactly, or the rendering is not well formed."""


def same(a: Any, b: Any) -> bool:
    """Type-exact JSON equality: 1, 1.0 and true differ; key order does not matter."""
    return canonical(a) == canonical(b)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _row_ok(row: Any) -> bool:
    return isinstance(row, dict) and row.keys() == ROW_KEYS and isinstance(row["id"], str)


def liftable(item: Any) -> bool:
    """A frontier adoption whose hypothesis rows are all well formed, with distinct ids, so they can be carried by id."""
    if not isinstance(item, dict) or item.get("source") != SOURCE or LEDGER_KEY in item:
        return False
    result = item.get("result")
    if not isinstance(result, dict) or not _is_int(item.get("requested_turn")):
        return False
    rows = result.get("hypotheses")
    if not isinstance(rows, list) or not rows or not all(_row_ok(row) for row in rows):
        return False
    return len({row["id"] for row in rows}) == len(rows)


def _check_originals(items: Sequence[Any]) -> None:
    """Refuse an input whose own keys would be mistaken for this layout's."""
    for item in items:
        if isinstance(item, Mapping) and LEDGER_KEY in item:
            raise CodecError("an adopted item already uses the ledger key")
        result = item.get("result") if isinstance(item, Mapping) else None
        rows = result.get("hypotheses") if isinstance(result, Mapping) else None
        if isinstance(rows, list) and any(isinstance(r, Mapping) and REF_KEY in r for r in rows):
            raise CodecError("an adopted row already uses the reference marker")
    turns = [item["requested_turn"] for item in items if liftable(item)]
    if len(set(turns)) != len(turns):  # a version is named by (id, requested_turn)
        raise CodecError("two lifted adoptions share a requested_turn")


def render(items: Sequence[Any]) -> dict[str, Any]:
    """The three blocks for the adopted guidance `items`; every value is a copy, `items` is not touched."""
    _check_originals(items)
    chains: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for item in items:
        if liftable(item):
            for row in item["result"]["hypotheses"]:
                chains.setdefault(row["id"], []).append((item["requested_turn"], row))
    current: list[dict[str, Any]] = []
    earlier: list[dict[str, Any]] = []
    for hid, chain in chains.items():
        turn, newest = chain[-1]
        current.append({"id": hid, "requested_turn": turn, **{f: copy.deepcopy(newest[f]) for f in FIELDS}})
        newer, newer_turn = newest, turn
        for turn, row in reversed(chain[:-1]):
            carried = [f for f in FIELDS if row[f] is not None and same(row[f], newer[f])]
            old = {"id": hid, "requested_turn": turn, **{f: copy.deepcopy(row[f]) for f in FIELDS if f not in carried}}
            if carried:
                old[REF_KEY] = newer_turn
            earlier.append(old)
            newer, newer_turn = row, turn
    ledger = []
    for item in items:
        if liftable(item):
            result = {k: copy.deepcopy(v) for k, v in item["result"].items() if k != "hypotheses"}
            ledger.append({**{k: copy.deepcopy(v) for k, v in item.items() if k != "result"},
                           LEDGER_KEY: [row["id"] for row in item["result"]["hypotheses"]], "result": result})
        else:
            ledger.append(copy.deepcopy(item))
    return {CURRENT: current, EARLIER: earlier, LEDGER: ledger}


def _version(row: Mapping[str, Any], base: Mapping[str, Any], base_turn: int) -> dict[str, Any]:
    """The fields of an earlier version: what its row states, the rest from the version it refers to."""
    present = {f: row[f] for f in FIELDS if f in row}
    omitted = [f for f in FIELDS if f not in present]
    if not omitted:
        if REF_KEY in row:
            raise CodecError("a reference marker on a row that omits nothing")
    elif REF_KEY not in row:
        raise CodecError("a row that omits fields needs the reference marker")
    elif not _is_int(row[REF_KEY]) or row[REF_KEY] != base_turn:
        raise CodecError("the marker refers to a version that is not the one it follows")
    elif any(base[f] is None for f in omitted):
        raise CodecError("the marker would stand for a null")
    return {f: copy.deepcopy(present[f] if f in present else base[f]) for f in FIELDS}


def _row(hid: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """A restored row with its keys sorted, as `canonical()` and the producer's own model dump order them."""
    return dict(sorted({"id": hid, **fields}.items()))


def _put(versions: dict[tuple[str, int], dict[str, Any]], key: tuple[str, int], row: dict[str, Any]) -> None:
    if key in versions:
        raise CodecError("two versions of one id share a requested_turn")
    versions[key] = row


def restore(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The adopted items `render` was given, rebuilt exactly. Raises `CodecError` on a rendering it cannot read."""
    if not isinstance(view, Mapping) or set(view) != {CURRENT, EARLIER, LEDGER}:
        raise CodecError("not a rendering of adopted guidance")
    if not all(isinstance(view[block], list) for block in view):
        raise CodecError("a block is not a list")
    versions: dict[tuple[str, int], dict[str, Any]] = {}
    chain: dict[str, tuple[dict[str, Any], int]] = {}  # per id: the version the next earlier row is measured against
    for row in view[CURRENT]:
        if not isinstance(row, Mapping) or set(row) != {"id", "requested_turn", *FIELDS}:
            raise CodecError("a current row is not complete")
        if not isinstance(row["id"], str) or not _is_int(row["requested_turn"]) or row["id"] in chain:
            raise CodecError("a current row has no usable id and turn, or an id has two current rows")
        full = _row(row["id"], {f: copy.deepcopy(row[f]) for f in FIELDS})
        _put(versions, (row["id"], row["requested_turn"]), full)
        chain[row["id"]] = (full, row["requested_turn"])
    for old in view[EARLIER]:
        if not isinstance(old, Mapping) or not {"id", "requested_turn"} <= set(old) or set(old) - {
                "id", "requested_turn", REF_KEY, *FIELDS}:
            raise CodecError("an earlier version has an unknown or missing key")
        if not isinstance(old["id"], str) or not _is_int(old["requested_turn"]):
            raise CodecError("an earlier version has no usable id and turn")
        if old["id"] not in chain:
            raise CodecError("an earlier version has no current row to refer to")
        base, base_turn = chain[old["id"]]
        full = _row(old["id"], _version(old, base, base_turn))
        _put(versions, (old["id"], old["requested_turn"]), full)
        chain[old["id"]] = (full, old["requested_turn"])
    used: set[tuple[str, int]] = set()
    items: list[Any] = []
    for entry in view[LEDGER]:
        if not (isinstance(entry, Mapping) and LEDGER_KEY in entry):
            items.append(copy.deepcopy(entry))
            continue
        ids, result, turn = entry[LEDGER_KEY], entry.get("result"), entry.get("requested_turn")
        if not isinstance(ids, list) or not isinstance(result, Mapping) or "hypotheses" in result or not _is_int(turn):
            raise CodecError("a ledger entry does not carry its rows by id")
        rows = []
        for hid in ids:
            key = (hid, turn)
            if not isinstance(hid, str) or key not in versions or key in used:
                raise CodecError("a ledger entry names a version that is missing or already used")
            used.add(key)
            rows.append(copy.deepcopy(versions[key]))
        items.append({**{k: copy.deepcopy(v) for k, v in entry.items() if k not in (LEDGER_KEY, "result")},
                      "result": {"hypotheses": rows, **copy.deepcopy(dict(result))}})
    if used != set(versions):
        raise CodecError("a version is named by no ledger entry")
    return items
