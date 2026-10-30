from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_runner_session import OBJECTIVE, connected, two_actions, harness_trace, session, closed, end_trace
from turn_trace_check import problems as trace_problems
from playtest.contracts.bridge import ExecStatus, ExecutionRecord, Observation, RejectReason
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Directive
from playtest.contracts.loop import TurnOutcome, TurnRecord
from playtest.contracts.trace import TraceEntry
from playtest.core.bridge_client import BridgeClient
from playtest.core.execution import Execution
from playtest.core.ports import Executed, S1Answer, S1Call, TurnReport
from playtest.core.trace_sink import MemorySink
from playtest.core.trace_replay import replay as replay_trace
from playtest.frontier.media import Clip
from playtest.frontier import provider
from playtest.runner.directives import DirectiveTracker
from playtest.runner.frontier_help import FrontierHelp
from playtest.runner.choices import build_choices


def _approval():
    limit = {"max_calls": 2, "max_cost_usd": 1.0, "per_call_max_usd": 0.5,
             "verified_balance_usd": 10.0, "balance_checked_at_utc": "2026-01-01T00:00:00Z",
             "price_valid_until_utc": "2099-01-01T00:00:00Z"}
    return {"max_calls": 2, "max_cost_usd": 1.0, "arms": {provider.MODEL: limit}}


def _answer_json(seq: int):
    return json.dumps({
        "add_choices": [], "candidate_finding": None,
        "directive": {"deadline_s": 10.0,
                      "expected": {"field": "public.counter", "op": ">=", "value": 1.0,
                                   "tolerance": None},
                      "target": None, "tests": "hyp-0-0", "text": "Check whether the measured count reaches one."},
        "hypotheses": [{"confirm_if": "The count reaches one.", "contradicting_seq": [],
                        "id": "hyp-0-0", "refute_if": "The count remains below one at the deadline.",
                        "statement": "The count may increase after the selected action.",
                        "status": "open", "supporting_seq": [seq]}],
        "prune_choices": [],
    })


def _meta_reply(_body):
    packet = json.loads(_body["messages"][2]["content"])
    seq = packet["observation"]["seq"]
    return 200, {"model": provider.MODEL, "choices": [{"finish_reason": "stop", "message": {"content": _answer_json(seq)}}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 5,
                           "prompt_tokens_details": {"cached_tokens": 0}}}, 1.0, None


def test_live_help_port_offline_end_to_end_escalates_then_adopts_hypothesis_and_scores_directive(tmp_path: Path):
    async def main():
        game, client = await connected()
        sink = harness_trace()
        frontier_release = threading.Event()
        suspicion_fact = "Suspicion fact (test fixture): a reader trend is under review."

        def fake_send(body):
            packet = json.loads(body["messages"][2]["content"])
            assert suspicion_fact in [fact["text"] for fact in packet["facts"]]
            assert frontier_release.wait(3), "the test did not release the fake frontier"
            result = _meta_reply(body)
            return result

        class S1:
            contexts = []
            directive_seen = False
            def answer(self, context, choice, while_pending=None):
                value = {"choice_id": choice, "set_version": context["choice_set_version"]}
                if while_pending is not None:
                    value["while_pending"] = while_pending
                return S1Answer(answer=value, calls=(S1Call(question="next_action", provider="fake", model="fake",
                                                            http_status=200, latency_ms=1.0),))
            async def decide(self, context):
                self.contexts.append(context)
                self.directive_seen = self.directive_seen or any(
                    "Frontier directive" in line for line in context["facts"])
                if len(self.contexts) == 1:
                    return self.answer(context, "escalate", {"mode": "continue", "choice_id": "move"})
                guidance = context.get("guidance", [])
                frontier_result = next((row.get("result") for row in guidance
                                        if row.get("source") == "frontier"), None)
                if not frontier_result:
                    frontier_release.set()
                    return self.answer(context, "wait")
                assert self.directive_seen
                assert suspicion_fact in context["facts"]
                assert frontier_result["hypotheses"][0]["confirm_if"] == "The count reaches one."
                return self.answer(context, "finish")

        tracker = DirectiveTracker(sink)
        class StubSuspicionFacts:
            async def facts(self, reports, observation):
                return [*await tracker.facts(reports, observation), suspicion_fact]

        stub_manifest = SimpleNamespace(
            readers=[SimpleNamespace(alias="counter", kind="number")],
            actions=two_actions().actions, skills=[],
        )
        helper = FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                              approval=_approval(), sink=sink, tracker=tracker,
                              started_utc=datetime.now(timezone.utc), send=fake_send,
                              capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1)
        try:
            fake_s1 = S1()
            result = await session(client, build_choices(two_actions()), s1=fake_s1, objective=OBJECTIVE,
                                   help=helper, facts=StubSuspicionFacts(), sink=sink, wall_limit_s=10.0)
            assert result.status == "complete", result.error
            assert helper.calls == 1
            assert helper.ledger.rows[0]["status"] == "finished"
            assert helper.ledger.rows[0]["cost_usd"] is not None
            entries = sink.of_kind("investigation")
            assert [row["body"]["event"] for row in entries] == ["hypothesis_directive", "directive", "outcome"]
            assert entries[0]["body"]["hypothesis"][0]["id"] == "hyp-0-0"
            assert entries[-1]["body"]["outcome"] == "met"
            assert entries[-1]["body"]["evidence_trace_seq"] in entries[-1]["refs"]
            assert entries[-1]["body"]["request_turn"] == 0
            assert entries[-1]["body"]["based_on_seq"] == entries[0]["body"]["based_on_seq"]
            assert result.loop.turns[0].outcome.value == "escalated"
            end_trace(sink, result)
            validated = [TraceEntry.model_validate({"run_id": "r1", **row}) for row in sink.entries]
            assert replay_trace(validated) == []
            assert trace_problems(sink.entries) == []
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_both_live_ledgers_let_s1_keep_choosing_while_a_help_request_is_pending(tmp_path: Path):
    # The Day 5 gate run halted when S1's next call met the frontier's in-flight reservation.
    stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                    actions=two_actions().actions, skills=[])
    helper = FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                          approval=_approval(), sink=harness_trace(), tracker=DirectiveTracker(harness_trace()),
                          started_utc=datetime.now(timezone.utc), send=lambda body: _meta_reply(body),
                          capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1)
    assert helper.ledger.concurrent_arms is True
    from playtest.runner import cli
    assert "call_budget.Ledger(run_dir, approval, ARM, concurrent_arms=True)" in Path(cli.__file__).read_text(
        encoding="utf-8")


@pytest.mark.parametrize("status,game_s,public,scene", [
    ("met", 0.5, {"counter": 1}, "scene"),
    ("not_met", 2, {"counter": 0}, "scene"),
    ("expired", 2, {}, "scene"),
    ("invalidated", 0.5, {"counter": 0}, "new-scene"),
    ("unknown", 0.5, {}, "scene"),
])
def test_directive_tracker_closes_each_terminal_outcome(status, game_s, public, scene):
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        deadline = 1 if status in {"not_met", "expired"} else 10
        tracker.adopt({"directive_id": "d", "text": "Reach the setpoint.",
                       "expected": {"field": "public.counter", "op": ">=", "value": 1},
                       "kind": "number", "baseline": 0, "deadline_game_s": deadline,
                       "scene": "scene", "last_frame": 0, "last_game_s": 0,
                       "directive": {"directive_id": "d", "text": "Reach the setpoint.",
                                     "expected": {"field": "public.counter", "op": ">=", "value": 1}}},
                      request_turn=0, based_on_seq=0)
        obs = Observation(session="test", seq=1, game_s=game_s, wall_utc=datetime.now(timezone.utc),
                          clock_mode=ClockMode.realtime, scene=scene, playing=True, paused=False,
                          time_scale=1, frame=1, public=public)
        await tracker.facts([], obs)
        if status == "unknown":
            tracker.finish(turn=1, based_on_seq=1)
        assert sink.of_kind("investigation")[-1]["body"]["outcome"] == status

    asyncio.run(main())


def _directive(directive_id: str, deadline: float) -> dict:
    spec = {"directive_id": directive_id, "model": provider.MODEL, "based_on_seq": 0,
            "text": f"Reach the setpoint ({directive_id}).",
            "expected": {"field": "public.counter", "op": ">=", "value": 1}, "deadline_game_s": deadline}
    return {**spec, "kind": "number", "baseline": 0, "scene": "scene", "last_frame": 0, "last_game_s": 0,
            "directive": dict(spec)}


def _observation(seq: int, game_s: float, counter: int) -> Observation:
    return Observation(session="test", seq=seq, game_s=game_s, wall_utc=datetime.now(timezone.utc),
                       clock_mode=ClockMode.realtime, scene="scene", playing=True, paused=False,
                       time_scale=1, frame=seq, public={"counter": counter})


def test_every_directive_and_its_outcome_stays_in_the_facts_after_it_closes():
    # Before #132 a closed directive's outcome was stated on its closing turn only.
    async def main():
        tracker = DirectiveTracker(MemorySink())
        tracker.adopt(_directive("d-1", 5), request_turn=0, based_on_seq=0)
        closing = await tracker.facts([], _observation(1, 1.0, 1))
        later = await tracker.facts([], _observation(2, 2.0, 0))
        assert closing == later and len(later) == 1
        assert "d-1" in later[0] and "status met (the measured reader value satisfied the setpoint" in later[0]
        tracker.adopt(_directive("d-2", 9), request_turn=2, based_on_seq=2)
        both = await tracker.facts([], _observation(3, 3.0, 0))
        assert [("d-1" in line, "d-2" in line) for line in both] == [(True, False), (False, True)]
        assert both[0] == later[0] and both[1].endswith("status open.")       # the open one is the last line
        expired = await tracker.facts([], _observation(4, 9.5, 0))
        assert "d-2" in expired[1] and "status not_met (the deadline passed" in expired[1]

    asyncio.run(main())


def _report(turn: int, kind: str, *, status: ExecStatus | None = None) -> TurnReport:
    """One finished turn: an action with the bridge's terminal status, a wait or an escalation."""
    choices = {c.kind.value: c for c in build_choices(two_actions()).choices}
    obs = _observation(turn + 1, float(turn), 0)
    selected = choices["action" if kind == "action" else kind]
    sent: tuple[Executed, ...] = ()
    if kind == "action":
        envelope = {"session": "s", "seq": turn + 1, "game_s": float(turn), "wall_utc": obs.wall_utc}
        terminal = ExecutionRecord(action_id=f"a{turn}", status=status, **envelope,
                                   reject_reason=RejectReason.stale_age if status is ExecStatus.rejected else None)
        sent = (Executed("choice", selected, Execution(request={}, records=(terminal,))),)
    outcome = {"wait": TurnOutcome.finished, "escalate": TurnOutcome.escalated}.get(
        kind, TurnOutcome.rejected_stale if status is ExecStatus.rejected else TurnOutcome.executed)
    record = TurnRecord(turn=turn, set_version=0, based_on_seq=turn + 1, raw_choice=selected.id,
                        effective_choice=selected.id,
                        executed_action=selected.id if outcome is TurnOutcome.executed else None, outcome=outcome)
    return TurnReport(record=record, observation=obs, selected=selected, problem=None, failure=None,
                      help_event=None, executed=sent)


def test_a_not_met_whose_every_attempt_was_rejected_says_its_test_never_ran():
    # Day 5 gate run-02: d-38 was scored not_met while all three attempts in its window went stale (#133).
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        tracker.adopt(_directive("d-38", 3.5), request_turn=0, based_on_seq=0)
        reports: list[TurnReport] = [_report(0, "escalate")]
        await tracker.facts(reports, _observation(1, 1.0, 0))           # first stated to S1 on turn 1
        reports += [_report(1, "action", status=ExecStatus.rejected), _report(2, "wait"),
                    _report(3, "action", status=ExecStatus.rejected)]
        lines = await tracker.facts(reports, _observation(5, 4.0, 0))   # past the deadline: not_met
        outcome = sink.of_kind("investigation")[-1]["body"]
        directive = Directive.model_validate(outcome["directive"])      # the record the frontier's history uses
        assert (outcome["outcome"], directive.error_class.value) == ("not_met", "action_rejected")
        assert directive.window.model_dump(mode="json") == {
            "first_turn": 1, "first_game_s": 1.0, "turns": 3, "inputs_sent": 2, "inputs_completed": 0,
            "inputs_rejected": {"stale_age": 2}, "inputs_unfinished": {}, "inputs_unrecorded": 0,
            "waits": 1, "escalations": 0}
        assert "no input completed in its window" in outcome["reason"]
        assert ("window: 3 turns from turn 1, 2 inputs sent, 0 completed, 2 rejected (stale_age 2), 1 waits"
                in lines[-1]) and lines[-1].endswith("error class action_rejected.")

    asyncio.run(main())


def test_the_deadline_counts_from_when_s1_first_sees_the_directive():
    # #132 validation run: 11 of 14 deadlines passed while the answer was still on its way to S1.
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        item = _directive("d-9", 4.0)                          # asked at game 1.0 with a 3 s deadline
        item["requested_game_s"] = 1.0
        tracker.adopt(item, request_turn=0, based_on_seq=0)
        first = await tracker.facts([], _observation(1, 10.0, 0))   # S1 first sees it at game 10.0
        assert "deadline game_s 13.0" in first[-1] and first[-1].endswith("status open.")
        await tracker.facts([_report(0, "action", status=ExecStatus.completed)], _observation(2, 12.5, 0))
        assert tracker.active is not None                       # 13.0 not yet reached
        await tracker.facts([_report(0, "action", status=ExecStatus.completed), _report(1, "wait")],
                            _observation(3, 13.5, 0))
        outcome = sink.of_kind("investigation")[-1]["body"]
        assert outcome["outcome"] == "not_met" and outcome["directive"]["deadline_game_s"] == 13.0
        assert outcome["directive"]["window"]["first_game_s"] == 10.0
        assert outcome["directive"]["window"]["inputs_completed"] == 1 and outcome["directive"]["error_class"] is None

    asyncio.run(main())


def test_without_both_game_times_the_deadline_stays_as_given():
    async def main():
        tracker = DirectiveTracker(MemorySink())
        tracker.adopt(_directive("d-3", 4.0), request_turn=0, based_on_seq=0)   # no requested_game_s
        lines = await tracker.facts([], _observation(1, 2.0, 0))
        assert "deadline game_s 4.0" in lines[-1]

    asyncio.run(main())


def test_a_tested_not_met_and_an_untried_one_are_told_apart():
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        tracker.adopt(_directive("d-1", 2.5), request_turn=0, based_on_seq=0)
        await tracker.facts([], _observation(1, 1.0, 0))
        await tracker.facts([_report(0, "action", status=ExecStatus.completed)], _observation(2, 3.0, 0))
        tested = sink.of_kind("investigation")[-1]["body"]
        assert tested["outcome"] == "not_met" and tested["directive"]["error_class"] is None
        assert tested["directive"]["window"]["inputs_completed"] == 1
        assert "no input completed" not in tested["reason"]
        tracker.adopt(_directive("d-2", 5.5), request_turn=1, based_on_seq=2)
        reports = [_report(0, "action", status=ExecStatus.completed)]
        await tracker.facts(reports, _observation(3, 4.0, 0))
        await tracker.facts(reports + [_report(1, "wait"), _report(2, "wait")], _observation(4, 6.0, 0))
        untried = sink.of_kind("investigation")[-1]["body"]
        assert untried["outcome"] == "not_met" and untried["directive"]["error_class"] == "no_attempt"
        assert untried["directive"]["window"]["inputs_sent"] == 0 and untried["directive"]["window"]["waits"] == 2
        for body in (tested, untried):
            Directive.model_validate(body["directive"])

    asyncio.run(main())


def test_guidance_lists_each_hypothesis_once_and_names_the_directive_by_id(tmp_path: Path):
    stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                    actions=two_actions().actions, skills=[])
    helper = FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                          approval=_approval(), sink=harness_trace(), tracker=DirectiveTracker(harness_trace()),
                          started_utc=datetime.now(timezone.utc), send=lambda body: _meta_reply(body),
                          capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1)
    first_h = {"id": "hyp-1-0", "statement": "A.", "status": "open", "confirm_if": "c", "refute_if": "r",
               "supporting_seq": [3], "contradicting_seq": []}
    other = {**first_h, "id": "hyp-1-1", "statement": "B."}
    first = helper._guidance({"hypotheses": [first_h, other], "directive": {"directive_id": "d-1", "text": "t"},
                              "errors": [], "review": []})
    assert first == {"hypotheses": [first_h, other], "directive_id": "d-1"}  # empty lists add nothing
    reworded = {**first_h, "statement": "A, reworded."}       # the frontier restates open hypotheses in its own words
    changed = {**other, "status": "refuted"}
    new = {**first_h, "id": "hyp-5-0", "statement": "C."}
    second = helper._guidance({"hypotheses": [reworded, changed, new], "directive": None,
                               "errors": ["one problem"], "review": [{"item": "x", "reason": "y"}]})
    assert second["hypotheses"] == [changed, new]            # a status change and a new id in full
    assert second["restated_hypotheses"] == ["hyp-1-0"]      # a same-status restatement by id: listed, never hidden
    assert second["directive_id"] is None
    assert second["errors"] == ["one problem"] and second["review"] == [{"item": "x", "reason": "y"}]
    third = helper._guidance({"hypotheses": [{**changed, "status": "open"}], "directive": None})
    assert third["hypotheses"] == [{**changed, "status": "open"}]    # reopening is a status change too


def test_invalid_frontier_answer_is_recorded_without_synthesizing_hypothesis_or_directive(tmp_path: Path):
    async def main():
        game, client = await connected()
        sink = harness_trace()
        frontier_release = threading.Event()
        tracker = DirectiveTracker(sink)
        stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                        actions=two_actions().actions, skills=[])
        def invalid_send(_body):
            assert frontier_release.wait(3), "the test did not release the fake frontier"
            return 200, {"model": provider.MODEL, "choices": [{"finish_reason": "stop",
                    "message": {"content": "not-json"}}]}, 1.0, None
        helper = FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                              approval=_approval(), sink=sink, tracker=tracker,
                              started_utc=datetime.now(timezone.utc), send=invalid_send,
                              capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1)
        class S1:
            contexts = []
            async def decide(self, context):
                self.contexts.append(context)
                if len(self.contexts) == 1:
                    return S1Answer(answer={"choice_id": "escalate", "set_version": context["choice_set_version"],
                                            "while_pending": {"mode": "wait"}})
                guidance = context.get("guidance", [])
                if not guidance:
                    frontier_release.set()
                    return S1Answer(answer={"choice_id": "wait", "set_version": context["choice_set_version"]})
                assert guidance[-1]["result"]["directive"] is None
                return S1Answer(answer={"choice_id": "finish", "set_version": context["choice_set_version"]})
        try:
            result = await session(client, build_choices(two_actions()), s1=S1(), objective=OBJECTIVE,
                                   help=helper, facts=tracker, sink=sink, wall_limit_s=10.0)
            assert result.status == "complete"
            entry = sink.of_kind("investigation")[-1]["body"]
            assert entry["event"] == "invalid_answer"
            assert entry["hypothesis"] is None and entry["directive"] is None
            assert tracker.active is None
        finally:
            await closed(game, client)
    asyncio.run(main())
