"""Suspicions and findings as trace records: written with citations that exist, folded back by id.

The trace is the only store (build bible 5.7: typed, append-only, no database). A `suspicion` entry is a
fact the loop measured; a `finding` entry is a `Finding` in some state, written by whoever judges (the
frontier proposes a candidate, a reproduction attempt moves it on). A finding is never edited: each change
of state is a new entry with the same `finding_id`, and `latest_findings` reads the newest.

A finding cites trace sequence numbers in `evidence_seq`. `FindingLog.record` makes each one a ref of the
entry, and the sink refuses a ref that is not an earlier entry, so a citation that does not exist is an
error at the time it is written, not a dangling number in a report.

Readers of a trace take entries as dicts (a `MemorySink`'s) or as `TraceEntry` models (a read file's).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from playtest.contracts.investigation import Finding, Suspicion
from playtest.contracts.trace import TraceAuthor, TraceEntry, TraceKind

from .trace_sink import TraceSink


@dataclass(frozen=True)
class RaisedSuspicion:
    """A suspicion and the trace entry it was written as (`None` when the run keeps no trace)."""

    trace_seq: int | None
    suspicion: Suspicion


@dataclass(frozen=True)
class RecordedFinding:
    """A finding in the state one entry recorded it in."""

    trace_seq: int
    finding: Finding


class FindingLog:
    """Writes finding records to a trace sink."""

    def __init__(self, sink: TraceSink, *, author: TraceAuthor = TraceAuthor.core) -> None:
        self._sink = sink
        self._author = author

    def record(self, finding: Finding, *, turn: int | None = None) -> int:
        """Append the finding's current state and return its `trace_seq`.

        Every `evidence_seq` becomes a ref. `ValueError` if one is not an earlier entry of this trace.
        """
        return self._sink.append(TraceKind.finding, finding.model_dump(mode="json"), author=self._author,
                                 turn=turn, refs=sorted(set(finding.evidence_seq)))


def _rows(entries: Iterable[TraceEntry | Mapping[str, Any]]) -> Iterable[tuple[int, str, Mapping[str, Any]]]:
    for entry in entries:
        if isinstance(entry, TraceEntry):
            yield entry.trace_seq, entry.kind.value, entry.body
        else:
            yield entry["trace_seq"], str(getattr(entry["kind"], "value", entry["kind"])), entry["body"]


def suspicions_in(entries: Iterable[TraceEntry | Mapping[str, Any]]) -> list[RaisedSuspicion]:
    """Every suspicion in the trace, in the order it was raised."""
    return [RaisedSuspicion(seq, Suspicion.model_validate(body))
            for seq, kind, body in _rows(entries) if kind == TraceKind.suspicion.value]


def findings_in(entries: Iterable[TraceEntry | Mapping[str, Any]]) -> dict[str, list[RecordedFinding]]:
    """Each finding's records in trace order, by `finding_id` (first-seen order)."""
    found: dict[str, list[RecordedFinding]] = {}
    for seq, kind, body in _rows(entries):
        if kind == TraceKind.finding.value:
            finding = Finding.model_validate(body)
            found.setdefault(finding.finding_id, []).append(RecordedFinding(seq, finding))
    return found


def latest_findings(entries: Iterable[TraceEntry | Mapping[str, Any]]) -> dict[str, RecordedFinding]:
    """Each finding in the newest state recorded for it."""
    return {finding_id: history[-1] for finding_id, history in findings_in(entries).items()}


def observation_entries(entries: Iterable[TraceEntry | Mapping[str, Any]]) -> dict[int, int]:
    """Bridge observation `seq` to the `trace_seq` of its entry: the map from what a model cites to what a finding cites.

    A restart gives the new play instance a new session, and its bridge counter may count from the same
    sequence numbers again. A `seq` seen in more than one session therefore names no one entry and is left
    out: a citation must be unambiguous. A `seq` seen in one session only stays citable, however old.
    """
    seen: dict[int, dict[str | None, int]] = {}
    for seq, kind, body in _rows(entries):
        if kind == TraceKind.observation.value:
            seen.setdefault(int(body["seq"]), {}).setdefault(body.get("session"), seq)
    return {seq: next(iter(by_session.values()))
            for seq, by_session in seen.items() if len(by_session) == 1}
