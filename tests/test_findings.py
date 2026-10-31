"""Findings and suspicions in the trace: citations that exist, states that never edit, folds that match the entries."""

from __future__ import annotations

import pytest

from datetime import datetime, timezone

from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import (
    Budget,
    ExpectedSource,
    Finding,
    FindingState,
    RunRecord,
    Suspicion,
    SuspicionBasis,
)
from playtest.contracts.trace import TraceAuthor, TraceEntry, TraceKind
from playtest.core.findings import (
    FindingLog,
    findings_in,
    latest_findings,
    observation_entries,
    suspicions_in,
)
from playtest.core.trace import TraceWriter, read_trace
from playtest.core.trace_sink import MemorySink


def suspicion(n: int, evidence: list[int]) -> Suspicion:
    return Suspicion(suspicion_id=f"s{n}", basis=SuspicionBasis.range_exit, turn=0, based_on_seq=20, subject="public.r",
                     statement="public.r is 5: outside the range 1 to 2 that its 5 earlier readings showed.",
                     evidence_seq=evidence)


def finding(state: FindingState, evidence: list[int], finding_id: str = "f1") -> Finding:
    return Finding(finding_id=finding_id, state=state, title="Timer left its range", expected="Timer stays in its range",
                   expected_source=ExpectedSource.interpretation, actual="Timer fell to -1.5", evidence_seq=evidence)


def trace() -> MemorySink:
    """A run, one observation at bridge seq 1842 and one suspicion citing it."""
    sink = MemorySink()
    sink.append(TraceKind.run, {"run_id": "r"}, author=TraceAuthor.harness)
    sink.append(TraceKind.observation, {"seq": 1842, "public": {}}, author=TraceAuthor.bridge, turn=0)
    sink.append(TraceKind.suspicion, suspicion(1, [1]).model_dump(mode="json"), author=TraceAuthor.core, turn=0, refs=[1])
    return sink


def test_a_finding_is_recorded_with_each_cited_entry_as_a_ref():
    sink = trace()
    seq = FindingLog(sink).record(finding(FindingState.candidate, [2, 1]))
    entry = sink.entries[seq]
    assert entry["kind"] == "finding" and entry["refs"] == [1, 2] and entry["turn"] is None
    assert entry["body"]["evidence_seq"] == [2, 1] and entry["body"]["state"] == "candidate"
    assert entry["author"] == "core"


def test_a_citation_that_is_not_an_earlier_entry_is_refused_when_it_is_written():
    sink = trace()
    log = FindingLog(sink)
    with pytest.raises(ValueError, match="refs must cite earlier entries"):
        log.record(finding(FindingState.candidate, [1, 3]))                  # entry 3 is the finding itself: not earlier
    with pytest.raises(ValueError, match="refs must cite earlier entries"):
        log.record(finding(FindingState.candidate, [99]))
    assert len(sink.entries) == 3                                            # nothing was written


def test_a_candidate_with_no_citation_cannot_exist_and_a_suspicion_state_needs_none():
    with pytest.raises(ValueError, match="must cite trace sequence numbers"):
        finding(FindingState.candidate, [])
    sink = trace()
    seq = FindingLog(sink).record(finding(FindingState.suspicion, []), turn=0)
    assert sink.entries[seq]["refs"] == [] and sink.entries[seq]["turn"] == 0


def test_a_new_state_is_a_new_entry_and_the_fold_reads_the_newest():
    sink = trace()
    log = FindingLog(sink, author=TraceAuthor.frontier)
    first = log.record(finding(FindingState.suspicion, [1]))
    second = log.record(finding(FindingState.candidate, [1, 2]))
    other = log.record(finding(FindingState.candidate, [2], finding_id="f2"))
    history = findings_in(sink.entries)
    assert list(history) == ["f1", "f2"] and [r.trace_seq for r in history["f1"]] == [first, second]
    assert [r.finding.state for r in history["f1"]] == [FindingState.suspicion, FindingState.candidate]
    latest = latest_findings(sink.entries)
    assert latest["f1"].trace_seq == second and latest["f1"].finding.state is FindingState.candidate
    assert latest["f2"].trace_seq == other
    assert sink.entries[first]["body"]["state"] == "suspicion"               # the earlier entry was not edited
    assert sink.entries[first]["author"] == "frontier"


def test_suspicions_and_observations_fold_out_of_the_trace():
    sink = trace()
    raised = suspicions_in(sink.entries)
    assert [(r.trace_seq, r.suspicion.suspicion_id) for r in raised] == [(2, "s1")]
    assert observation_entries(sink.entries) == {1842: 1}


def test_the_folds_read_a_trace_file_the_same_as_a_memory_sink(tmp_path):
    record = RunRecord(run_id="r", game_project="g", clock_mode=ClockMode.realtime, models=[], objective="o",
                       budget=Budget(), core_version="0")
    observed = Observation(session="s", seq=1842, game_s=1.0, wall_utc=datetime(2026, 10, 29, tzinfo=timezone.utc),
                           clock_mode="realtime", scene="main", playing=True, paused=False, time_scale=1.0, frame=1)
    writer = TraceWriter(tmp_path / "trace.jsonl", record)
    writer.append(TraceKind.observation, observed.model_dump(mode="json"), author=TraceAuthor.bridge, turn=0)
    writer.append(TraceKind.suspicion, suspicion(1, [1]).model_dump(mode="json"), author=TraceAuthor.core, turn=0, refs=[1])
    FindingLog(writer).record(finding(FindingState.candidate, [1, 2]))
    writer.close()
    entries = read_trace(tmp_path / "trace.jsonl")
    assert all(isinstance(entry, TraceEntry) for entry in entries)
    assert [r.suspicion.suspicion_id for r in suspicions_in(entries)] == ["s1"]
    assert latest_findings(entries)["f1"].finding.state is FindingState.candidate
    assert latest_findings(entries)["f1"].trace_seq == 3
    assert observation_entries(entries) == {1842: 1}


def test_a_bridge_sequence_seen_in_two_sessions_is_not_citable():
    """A restart reuses the bridge counter: a `seq` two sessions both recorded names no one entry."""
    sink = MemorySink()
    sink.append(TraceKind.run, {"run_id": "r"}, author=TraceAuthor.harness)
    sink.append(TraceKind.observation, {"seq": 3, "session": "before"}, author=TraceAuthor.bridge, turn=0)
    sink.append(TraceKind.observation, {"seq": 7, "session": "before"}, author=TraceAuthor.bridge, turn=0)
    sink.append(TraceKind.observation, {"seq": 7, "session": "after"}, author=TraceAuthor.bridge, turn=0)

    # The unique pre-restart sequence stays citable; the reused one is dropped rather than guessed.
    assert observation_entries(sink.entries) == {3: 1}
