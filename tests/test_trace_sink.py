"""The in-memory trace sink: contiguous sequence numbers, backward refs, frozen bodies."""

from __future__ import annotations

import pytest

from playtest.contracts.trace import TraceAuthor, TraceKind
from playtest.core.trace_sink import MemorySink


def test_sequence_is_contiguous_and_refs_point_back():
    sink = MemorySink()
    first = sink.append(TraceKind.observation, {"seq": 4}, author=TraceAuthor.bridge, turn=0)
    second = sink.append("choice_set", {"built_from_seq": 4}, author="core", turn=0, refs=[first])
    assert (first, second) == (0, 1)
    assert [e["trace_seq"] for e in sink.entries] == [0, 1]
    assert sink.entries[1]["kind"] == "choice_set" and sink.entries[1]["refs"] == [0]
    assert [e["trace_seq"] for e in sink.of_kind("observation")] == [0]


def test_rejects_forward_refs_and_unknown_names():
    sink = MemorySink()
    with pytest.raises(ValueError):
        sink.append(TraceKind.turn, {}, author=TraceAuthor.core, refs=[0])
    with pytest.raises(ValueError):
        sink.append("not_a_kind", {}, author=TraceAuthor.core)
    with pytest.raises(ValueError):
        sink.append(TraceKind.turn, {}, author="nobody")
    with pytest.raises(ValueError):
        sink.append(TraceKind.turn, {}, author=TraceAuthor.core, turn=-1)
    assert sink.entries == []


def test_body_is_copied_on_append():
    sink = MemorySink()
    body = {"nested": {"value": 1}}
    sink.append(TraceKind.stop, body, author=TraceAuthor.core)
    body["nested"]["value"] = 2
    assert sink.entries[0]["body"] == {"nested": {"value": 1}}
