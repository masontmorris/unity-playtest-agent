"""The loop with readers and a watch: reader values in the observation, suspicions in the trace, facts for S1.

The S1-ownership half is the point: a suspicion is a fact. However many the watch raises, S1's selection
executes exactly as chosen, no helper is asked, and nothing S1 may choose changes.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from loop_fakes import TOKEN, FakeHelp, FakeS1, TransformsGame, choices, pick, rig, run
from turn_trace_check import problems

from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, RunRecord, SuspicionBasis
from playtest.contracts.manifest import ManifestInvariant
from playtest.contracts.trace import TraceAuthor, TraceKind
from playtest.core.adherence import check_adherence
from playtest.core.bridge_client import BridgeClient
from playtest.core.execution import Execution
from playtest.core.findings import FindingLog, observation_entries, suspicions_in
from playtest.core.ports import EffectComparison, EffectReport, ReaderSample
from playtest.core.suspicion import ReaderWatch
from playtest.core.trace import TraceWriter, read_trace
from playtest.core.trace_replay import replay
from playtest.core.turn_loop import TurnLoop


class ScriptedReaders:
    """A reader port that returns one scripted value per read and records when it was read."""

    def __init__(self, *values: Any, alias: str = "r", events: list[str] | None = None) -> None:
        self.alias = alias
        self.values = list(values)
        self.events = events
        self.reads = 0

    async def read(self) -> ReaderSample:
        self.reads += 1
        if self.events is not None:
            self.events.append("readers")
        value = self.values.pop(0)
        errors = {} if value is not None else {self.alias: "scripted: no value"}
        return ReaderSample(values={self.alias: value}, errors=errors, inspection_seq=self.reads, game_s=float(self.reads))


class ComparingProbe:
    """An effect probe whose report carries a comparison: the calibrated displacement 2 against what it is told was observed."""

    def __init__(self, *observed: float, events: list[str] | None = None) -> None:
        self.observed = list(observed)
        self.events = events

    async def before_turn(self, turn: int) -> None:
        if self.events is not None:
            self.events.append("probe")

    async def measure(self, choice, execution: Execution) -> EffectReport:
        value = self.observed.pop(0)
        comparison = EffectComparison(metric="net displacement", unit="world units", expected=2.0, observed=value,
                                      spread=0.01, allowed=0.06, basis="a test allowance")
        return EffectReport(expected={"net_displacement": 2.0}, observed={"net_displacement": value},
                            fact=f"{choice.id}: net displacement {value}.", comparison=comparison)


def watch(*, invariants: list[ManifestInvariant] | None = None, min_readings: int = 3) -> ReaderWatch:
    return ReaderWatch({"r": "number"}, invariants or [], min_readings=min_readings)


def sane(maximum: float = 5.0) -> ManifestInvariant:
    return ManifestInvariant.model_validate(dict(kind="range", name="r_sane", source="test", catches="a test", field="r",
                                                 direction=None, min=0.0, max=maximum, when=None, then=None, unless=None))


def finish(r) -> None:
    r.sink.append(TraceKind.end, {"status": "complete", "error": None, "turns": len(r.loop.turns), "redactions": 0},
                  author=TraceAuthor.harness)
    assert problems(r.sink.entries) == []


async def watched(*steps: Any, readers: ScriptedReaders | None, watch_: ReaderWatch | None, **kwargs: Any):
    return await rig(*steps, game_class=TransformsGame, readers=readers, watch=watch_, **kwargs)


# --- readers in the observation ---------------------------------------------------------------


def test_reader_values_are_in_every_observation_the_trace_and_the_context():
    async def main():
        readers = ScriptedReaders(1.0, 2.0, None, 4.0)
        r = await watched("w", "w", "w", "w", readers=readers, watch_=watch())
        try:
            await r.turns(4)
            observed = [e["body"] for e in r.sink.of_kind("observation")]
            assert [o["public"]["r"] for o in observed] == [1.0, 2.0, None, 4.0]
            assert [o["reader_errors"] for o in observed] == [[], [], [{"reader": "r", "error": "scripted: no value"}], []]
            assert [c["observation"]["public"]["r"] for c in r.s1.contexts] == [1.0, 2.0, None, 4.0]
            assert r.s1.contexts[2]["observation"]["reader_errors"] == [{"reader": "r", "error": "scripted: no value"}]
            assert [e["body"]["payload"]["observation"]["public"]["r"] for e in r.sink.of_kind("context")] == [1.0, 2.0, None, 4.0]
            await r.loop.stop("finish")
            finish(r)
        finally:
            await r.close()

    run(main)


def test_the_readers_are_read_before_the_observation_after_the_probe_and_never_between_it_and_the_act():
    async def main():
        events: list[str] = []
        readers = ScriptedReaders(1.0, 2.0, events=events)
        probe = ComparingProbe(2.0, events=events)
        r = await watched("c2", "w", readers=readers, watch_=watch(), effects=probe)
        original = r.client.observe_once

        async def observe_once(**kwargs):
            events.append("observe")
            return await original(**kwargs)

        r.client.observe_once = observe_once                                      # type: ignore[method-assign]
        try:
            await r.turns(2)
            assert events[:3] == ["probe", "readers", "observe"]
            assert events[3:] == ["probe", "readers", "observe"]
            assert [e["body"]["request"]["based_on_seq"] for e in r.sink.of_kind("execution")] == [
                r.sink.of_kind("observation")[0]["body"]["seq"]]                  # the act is bound to the observation, not a reading
            await r.loop.stop("finish")
        finally:
            await r.close()

    run(main)


def test_without_readers_or_a_watch_the_loop_writes_what_it_always_did():
    async def main():
        r = await rig("c2", "w", game_class=TransformsGame)
        try:
            await r.turns(2)
            assert r.sink.of_kind("suspicion") == [] and r.loop.suspicions == []
            assert all("r" not in e["body"]["public"] and e["body"]["reader_errors"] == [] for e in r.sink.of_kind("observation"))
            assert all(c["facts"] == [] or c["facts"][0].startswith("Turn ") for c in r.s1.contexts)
            await r.loop.stop("finish")
            finish(r)
        finally:
            await r.close()

    run(main)


# --- suspicions in the trace and in S1's context ------------------------------------------------


def test_a_range_exit_is_written_before_the_context_that_states_it_with_its_trace_seq():
    async def main():
        readers = ScriptedReaders(1, 2, 1, 2, 1, 5, 5)
        r = await watched(*["w"] * 7, readers=readers, watch_=watch())
        try:
            await r.turns(7)
            entries = r.sink.entries
            (entry,) = r.sink.of_kind("suspicion")
            assert entry["turn"] == 5 and entry["author"] == "core"
            kinds = [e["kind"] for e in entries if e["turn"] == 5]
            assert kinds[:4] == ["observation", "choice_set", "suspicion", "context"]
            observation_seq = next(e["trace_seq"] for e in entries if e["turn"] == 5 and e["kind"] == "observation")
            assert entry["refs"] == entry["body"]["evidence_seq"] and observation_seq in entry["refs"]
            line = r.s1.contexts[5]["facts"][-1]
            assert line == f"Suspicion s1 (range_exit, trace_seq {entry['trace_seq']}): " + entry["body"]["statement"]
            assert "Suspicion" not in " ".join(r.s1.contexts[6]["facts"])         # stated once, in the context of the turn it began
            raised = r.loop.suspicions
            assert [(x.trace_seq, x.suspicion.suspicion_id) for x in raised] == [(entry["trace_seq"], "s1")]
            assert r.loop.observation_entries[entry["body"]["based_on_seq"]] == observation_seq
            await r.loop.stop("finish")
            finish(r)
        finally:
            await r.close()

    run(main)


def test_the_reader_lines_are_facts_in_every_context_after_the_turns_own_facts():
    async def main():
        readers = ScriptedReaders(1.0, 2.0, 3.0)
        r = await watched("c2", "w", "w", readers=readers, watch_=watch(), probe=True)
        try:
            await r.turns(3)
            first, second, third = (c["facts"] for c in r.s1.contexts)
            assert first[0].startswith("The readers were read at game time") and first[1] == (
                "public.r: now 1; previous unknown; earlier readings: none.")
            assert second[0].startswith("Turn 0: c2 was executed.") and second[-1] == (
                "public.r: now 2; previous 1; earlier range 1 to 1 (1 earlier reading).")
            assert third[-1] == "public.r: now 3; previous 2; earlier range 1 to 2 (2 earlier readings)."
            await r.loop.stop("finish")
            finish(r)
        finally:
            await r.close()

    run(main)


def test_an_effect_suspicion_follows_its_effect_entry_and_is_stated_in_the_next_context_only():
    async def main():
        probe = ComparingProbe(0.0, 2.0)
        r = await watched("c2", "c2", "w", readers=None, watch_=watch(), effects=probe)
        try:
            await r.turns(3)
            (entry,) = r.sink.of_kind("suspicion")
            assert entry["turn"] == 0 and entry["body"]["basis"] == "effect_mismatch" and entry["body"]["subject"] == "c2"
            turn0 = [e for e in r.sink.entries if e["turn"] == 0]
            kinds = [e["kind"] for e in turn0]
            assert kinds == ["observation", "choice_set", "context", "s1_call", "decision", "execution", "effect",
                             "suspicion", "turn"]
            execution = next(e for e in turn0 if e["kind"] == "execution")
            effect = next(e for e in turn0 if e["kind"] == "effect")
            assert entry["refs"] == [execution["trace_seq"], effect["trace_seq"]] == entry["body"]["evidence_seq"]
            assert not any(f.startswith("Suspicion") for f in r.s1.contexts[0]["facts"])
            assert r.s1.contexts[1]["facts"][-1] == (
                f"Suspicion s1 (effect_mismatch, trace_seq {entry['trace_seq']}): " + entry["body"]["statement"])
            assert not any(f.startswith("Suspicion") for f in r.s1.contexts[2]["facts"])    # the second effect was inside the allowance
            await r.loop.stop("finish")
            finish(r)
        finally:
            await r.close()

    run(main)


def test_an_invariant_is_stated_when_it_begins_and_restated_while_it_lasts():
    async def main():
        readers = ScriptedReaders(3, 7, 8, 4)
        r = await watched("w", "w", "w", "w", readers=readers, watch_=watch(invariants=[sane()]))
        try:
            await r.turns(4)
            lines = [[f for f in c["facts"] if "r_sane" in f] for c in r.s1.contexts]
            assert lines[0] == []
            assert lines[1][0].startswith("Suspicion s1 (invariant, trace_seq ")
            assert lines[2] == ["Invariant r_sane still does not hold (since turn 1, suspicion s1): "
                                "r is 8, above the maximum 5 (r is 8)."]
            assert lines[3] == []
            (entry,) = r.sink.of_kind("suspicion")
            assert entry["body"]["basis"] == "invariant" and len(entry["refs"]) == 2    # the observation before, and this one
            await r.loop.stop("finish")
            finish(r)
        finally:
            await r.close()

    run(main)


def test_a_scripted_turn_records_its_suspicions_and_leaves_unstated_ones_for_the_next_context():
    async def main():
        probe = ComparingProbe(0.0)
        r = await watched("c2", "w", readers=None, watch_=watch(), effects=probe)
        try:
            await r.loop.run_turn()                                   # S1 turn: c2 executes, its effect is a suspicion
            await r.loop.run_turn(lambda choice_set, observation: {"choice_id": "w", "set_version": choice_set.set_version})
            assert len(r.sink.of_kind("suspicion")) == 1
            assert len(r.s1.contexts) == 1                            # the scripted turn built no context
            await r.loop.run_turn()
            assert any(f.startswith("Suspicion s1 (effect_mismatch") for f in r.s1.contexts[1]["facts"])
            await r.loop.stop("finish")
        finally:
            await r.close()

    run(main)


def test_the_trace_a_file_writer_keeps_replays_clean_with_suspicions_and_a_finding_in_it(tmp_path: Path):
    async def main():
        game = TransformsGame(token=TOKEN)
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN)
        await client.connect()
        record = RunRecord(run_id="r1", game_project="game", clock_mode=ClockMode.realtime, models=[],
                           objective="Exercise the controls.", budget=Budget(), core_version="0.0.1")
        writer = TraceWriter(tmp_path / "trace.jsonl", record)
        s1 = FakeS1("c2", "w", "w", "w", "w", "w", "w")
        loop = TurnLoop(client, choices=choices(), objective="Exercise the controls.", s1=s1, sink=writer, effects=ComparingProbe(0.0),
                        readers=ScriptedReaders(1, 2, 1, 2, 1, 5, 5), watch=watch(invariants=[sane(4)]))
        try:
            for _ in range(7):
                await loop.run_turn()
            await loop.stop("finish")
            raised = loop.suspicions
            from playtest.contracts.investigation import ExpectedSource, Finding, FindingState
            FindingLog(writer, author=TraceAuthor.frontier).record(Finding(
                finding_id="f1", state=FindingState.candidate, title="A reader left its range", expected="It stays in range",
                expected_source=ExpectedSource.interpretation, actual="It reached 5",
                evidence_seq=[raised[-1].trace_seq, *raised[-1].suspicion.evidence_seq]))
            writer.close("complete", None)
        finally:
            await client.aclose()
            await game.stop()
        return raised

    raised = asyncio.run(main())
    entries = read_trace(tmp_path / "trace.jsonl")
    assert replay(entries) == []
    assert problems([e.model_dump(mode="json") for e in entries]) == []
    assert {s.suspicion.basis for s in suspicions_in(entries)} == {SuspicionBasis.effect_mismatch, SuspicionBasis.range_exit,
                                                                  SuspicionBasis.invariant}
    assert [(s.trace_seq, s.suspicion.suspicion_id) for s in suspicions_in(entries)] == [
        (x.trace_seq, x.suspicion.suspicion_id) for x in raised]
    assert len(observation_entries(entries)) == 7 and entries[-2].kind is TraceKind.finding


# --- a suspicion is a fact: S1 still owns the actuator and escalation -----------------------------


def test_a_flood_of_suspicions_never_opens_help_and_every_selection_executes_exactly_as_chosen():
    async def main():
        help_port = FakeHelp()
        readers = ScriptedReaders(*range(1, 13))                       # a new high every turn: a departure from turn 3 on
        steps = ["c1", "c2", "w", "c1", "c3", "c2", "w", "c1", "c2", "c1", "w", "c2"]
        r = await watched(*steps, readers=readers, watch_=watch(invariants=[sane(2)]), help=help_port,
                          effects=ComparingProbe(*[0.0] * 12))
        try:
            await r.turns(len(steps))
            await asyncio.sleep(0)
            assert len(r.sink.of_kind("suspicion")) >= 8 and r.loop.suspicions
            assert help_port.requests == [] and r.loop.helper_count == 0 and r.sink.of_kind("help") == []
            assert all(c["pending_help"] is None for c in r.s1.contexts)
            turns = r.loop.turns
            assert [t.raw_choice for t in turns] == steps and [t.effective_choice for t in turns] == steps
            assert [t.executed_action for t in turns] == [s if s != "w" else None for s in steps]
            assert r.game.applied() == ["shift", "increment", "shift", "hold", "increment", "shift", "increment", "shift",
                                        "increment"]
            assert all(t.outcome.value in ("executed", "finished") for t in turns) and check_adherence(turns) == []
            await r.loop.stop("finish")
            finish(r)
        finally:
            await r.close()

    run(main)


def test_the_choices_s1_is_offered_do_not_depend_on_what_the_watch_found():
    async def main():
        quiet = await watched("w", "w", "w", "w", "w", "w", readers=ScriptedReaders(1, 1, 1, 1, 1, 1), watch_=watch())
        loud = await watched("w", "w", "w", "w", "w", "w", readers=ScriptedReaders(1, 2, 3, 4, 5, 6),
                             watch_=watch(invariants=[sane(2)]))
        try:
            await quiet.turns(6)
            await loud.turns(6)
            assert not quiet.sink.of_kind("suspicion") and loud.sink.of_kind("suspicion")
            offered = lambda r: [[c["id"] for c in ctx["choices"]] for ctx in r.s1.contexts]   # noqa: E731
            assert offered(quiet) == offered(loud) == [[c.id for c in choices()]] * 6
            assert [ctx["choice_set_version"] for ctx in quiet.s1.contexts] == [ctx["choice_set_version"] for ctx in loud.s1.contexts]
        finally:
            await quiet.close()
            await loud.close()

    run(main)


def test_help_opens_only_when_s1_chooses_it_whatever_the_watch_raised():
    async def main():
        help_port = FakeHelp()
        readers = ScriptedReaders(*range(1, 6))
        r = await watched("w", "w", pick("h"), "w", "w", readers=readers, watch_=watch(invariants=[sane(1)]), help=help_port)
        try:
            await r.turns(5)
            await asyncio.sleep(0)
            assert [e["turn"] for e in r.sink.of_kind("help")] == [2] and len(help_port.requests) == 1
            assert [t.outcome.value for t in r.loop.turns] == ["finished", "finished", "escalated", "finished", "finished"]
            await r.loop.stop("finish")
        finally:
            await r.close()

    run(main)


def test_a_watch_that_fails_ends_the_turn_loudly_instead_of_being_skipped():
    class Broken:
        def observe(self, *args, **kwargs):
            raise RuntimeError("the watch broke")

        def effect(self, *args, **kwargs):
            return []

    async def main():
        r = await watched("w", readers=None, watch_=Broken())            # type: ignore[arg-type]
        try:
            with pytest.raises(RuntimeError, match="the watch broke"):
                await r.loop.run_turn()
            assert r.game.applied() == [] and r.sink.of_kind("turn") == []
        finally:
            await r.close()

    run(main)


def test_no_reader_or_watch_module_can_reach_help_or_pick_a_choice():
    """Grep half of the property the build bible asks for: these modules do not name help, escalation or an actuator."""
    root = Path(__file__).resolve().parents[1] / "src" / "playtest"
    forbidden = re.compile(r"HelpPort|HelpRequest|escalate|delegate|resolve_helper|_help_request|ExecutionController|"
                           r"ChoiceKind|\.act\(|run_turn|ActRequest")
    for relative in ("core/suspicion.py", "core/evaluate.py", "core/readers.py", "core/findings.py", "runner/readers.py"):
        text = (root / relative).read_text(encoding="utf-8")
        assert not forbidden.findall(text), f"{relative} names {forbidden.findall(text)}"
