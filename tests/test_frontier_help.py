from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from runner_manifest import all_modes
from test_runner_session import OBJECTIVE, connected, two_actions, harness_trace, session, closed, end_trace
from turn_trace_check import problems as trace_problems
from playtest.contracts.bridge import ExecStatus, ExecutionRecord, Observation, RejectReason
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, Directive, RunRecord, Suspicion, SuspicionBasis
from playtest.contracts.loop import ChoiceSet, TurnOutcome, TurnRecord
from playtest.contracts.trace import TraceAuthor, TraceEntry, TraceKind
from playtest.core.bridge_client import BridgeClient
from playtest.core.execution import Execution
from playtest.core.ports import Executed, S1Answer, S1Call, TurnReport
from playtest.core.trace import TraceWriter
from playtest.core.trace_sink import MemorySink
from playtest.core.trace_replay import replay as replay_trace
from playtest.frontier.media import Clip
from playtest.frontier import provider
from playtest.roles import frontier
from playtest.runner.directives import DirectiveTracker
from playtest.preflight.verify import MEASUREMENT_TOLERANCE
from playtest.runner.frontier_help import (FrontierHelp, action_effects, reader_calibration, reader_notes,
                                           recorded_suspicions, _evidence_snapshot, _finding_evidence)
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
    assert "call_budget.Ledger(run_dir, approval, args.s1_arm, concurrent_arms=True)" in Path(cli.__file__).read_text(
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


def test_guidance_compacts_only_identical_hypotheses_and_names_the_directive_by_id(tmp_path: Path):
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
    reworded = {**first_h, "statement": "A, reworded.", "contradicting_seq": [4]}
    changed = {**other, "status": "contradicted"}
    new = {**first_h, "id": "hyp-5-0", "statement": "C."}
    second = helper._guidance({"hypotheses": [reworded, changed, new], "directive": None,
                               "errors": ["one problem"], "review": [{"item": "x", "reason": "y"}]})
    assert second["hypotheses"] == [reworded, changed, new]
    assert "restated_hypotheses" not in second
    assert second["directive_id"] is None
    assert second["errors"] == ["one problem"] and second["review"] == [{"item": "x", "reason": "y"}]
    third = helper._guidance({"hypotheses": [reworded, {**changed, "status": "open"}], "directive": None})
    assert third["restated_hypotheses"] == ["hyp-1-0"]
    assert third["hypotheses"] == [{**changed, "status": "open"}]    # reopening is a status change too


def test_a_candidate_finding_is_written_citing_trace_entries_or_not_written_at_all(tmp_path: Path):
    sink = harness_trace()
    observed = sink.append("observation", {"seq": 7}, author="harness", turn=0)
    stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                    actions=two_actions().actions, skills=[])
    helper = FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                          approval=_approval(), sink=sink, tracker=DirectiveTracker(sink),
                          started_utc=datetime.now(timezone.utc), send=lambda body: _meta_reply(body),
                          capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1)
    proposed = frontier.FindingOut.model_validate({
        "actual": "The count stays at zero.", "conditions": "After the move action.", "evidence_seq": [7, 7],
        "expected": "The count reaches one.", "expected_source": "developer_assertion",
        "repro_check": {"field": "public.counter", "op": "<", "tolerance": None, "value": 1.0},
        "severity": "major", "severity_reason": "It blocks progress.", "steps": ["Move."],
        "title": "The count never rises", "uncertainty": "One sample only."})
    snapshot = _evidence_snapshot(sink)
    written = helper._record_finding(proposed, turn=3, evidence=_finding_evidence(proposed, [], snapshot))
    assert written["finding_id"] == "f-3" and written["reason"] is None
    entry = sink.entries[written["trace_seq"]]
    assert entry["kind"] == "finding" and entry["author"] == "frontier" and entry["refs"] == [observed]
    assert entry["body"]["evidence_seq"] == [observed] and entry["body"]["state"] == "candidate"
    assert entry["body"]["repro_check"]["op"] == "<" and entry["body"]["severity"] == "major"
    assert helper.findings == [{"finding_id": "f-3", "trace_seq": written["trace_seq"], "request_turn": 3,
                                "title": "The count never rises",
                                "repro_check": {"field": "public.counter", "op": "<", "value": 1.0, "tolerance": None}}]
    # A cited observation the trace never recorded: nothing is written, and the reason names the seq.
    missing = proposed.model_copy(update={"evidence_seq": [7, 99]})
    unmapped = helper._record_finding(missing, turn=4, evidence=_finding_evidence(missing, [], snapshot))
    assert unmapped["trace_seq"] is None and "[99]" in unmapped["reason"]
    assert len(sink.of_kind("finding")) == 1 and len(helper.findings) == 1
    assert helper._guidance({"hypotheses": [], "directive": None, "candidate_finding": unmapped})[
        "candidate_finding"] == {"finding_id": "f-4", "reason": unmapped["reason"]}


def test_a_calibrated_displacement_becomes_an_effect_field_with_the_effect_probes_allowance():
    effects = action_effects(build_choices(two_actions()).expected)
    assert set(effects) == {"move", "look"}
    assert effects["move"].expected == 6.04 and effects["move"].unit == "world units"
    assert effects["move"].allowance == pytest.approx(0.01 + MEASUREMENT_TOLERANCE)       # sqrt(0.0001) + the stage 7 allowance
    assert effects["look"].expected == 0.0                                                # measured, and measured to be nothing
    # Only a declaration calibration measured exactly has one: a chord, a skill or an unmeasured hold has none.
    assert set(action_effects(build_choices(all_modes()).expected)) == {"key_hold", "stick_value"}


def test_an_action_without_a_usable_calibrated_displacement_is_never_offered_as_an_effect_field():
    assert action_effects({"a": None, "b": {}, "c": {"net_displacement": None}, "d": {"net_displacement": float("nan")},
                           "e": {"net_displacement": True}, "f": {"net_displacement": "far"}}) == {}
    one_trial = action_effects({"g": {"net_displacement": 2.0, "sample_variance": None, "unit": "world units"}})
    assert one_trial == {"g": frontier.Effect(expected=2.0, allowance=None)}      # no recorded spread: allowance unknown


def test_the_helper_hands_the_frontier_every_effect_even_when_a_choice_is_hidden(tmp_path: Path):
    built = build_choices(two_actions())
    stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                    actions=two_actions().actions, skills=[])
    def helper(**kwargs):
        return FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                            approval=_approval(), sink=harness_trace(), tracker=DirectiveTracker(harness_trace()),
                            started_utc=datetime.now(timezone.utc), send=lambda body: _meta_reply(body),
                            capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1, **kwargs)
    hidden = ChoiceSet(set_version=1, built_from_seq=1, choices=[c for c in built.choices if c.id != "move"])
    role = helper(effects=action_effects(built.expected))._manifest_for(hidden)
    assert "move" not in role.actions                                         # hidden from S1 for now
    assert set(role.effects) == {"move", "look"}                              # still an action a check may name
    assert role.enums()["check_fields"] == ["public.counter", "effect.look", "effect.move"]
    assert helper()._manifest_for(hidden).effects == {}                       # no effects given: none offered


def test_a_candidate_finding_with_an_effect_check_is_written_as_proposed(tmp_path: Path):
    sink = harness_trace()
    observed = sink.append("observation", {"seq": 7}, author="harness", turn=0)
    stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                    actions=two_actions().actions, skills=[])
    helper = FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                          approval=_approval(), sink=sink, tracker=DirectiveTracker(sink),
                          started_utc=datetime.now(timezone.utc), send=lambda body: _meta_reply(body),
                          capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1)
    proposed = frontier.FindingOut.model_validate({
        "actual": "The entity ended the move where it started.", "conditions": "After the move action.",
        "evidence_seq": [7], "expected": "The move carries the entity about as far as calibration measured.",
        "expected_source": "interpretation",
        "repro_check": {"field": "effect.move", "op": "<", "tolerance": None, "value": 1.5},
        "severity": "major", "severity_reason": "The control stops working.", "steps": ["Move."],
        "title": "The move no longer carries the entity", "uncertainty": "One sample only."})
    written = helper._record_finding(proposed, turn=5,
                                     evidence=_finding_evidence(proposed, [], _evidence_snapshot(sink)))
    assert written["reason"] is None
    entry = sink.entries[written["trace_seq"]]
    assert entry["body"]["repro_check"] == {"field": "effect.move", "op": "<", "value": 1.5, "tolerance": None}
    assert entry["body"]["evidence_seq"] == [observed] and entry["body"]["state"] == "candidate"
    assert helper.findings[0]["repro_check"] == entry["body"]["repro_check"]


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


# --- how a setpoint is scored (#141) ---------------------------------------------------------------------------


def _watch(directive_id: str, deadline: float, op: str = "unchanged", baseline=0) -> dict:
    """A changed/unchanged directive on the counter; `baseline` is the reading at the escalation."""
    spec = {"directive_id": directive_id, "model": provider.MODEL, "based_on_seq": 0,
            "text": f"Watch the counter ({directive_id}).",
            "expected": {"field": "public.counter", "op": op, "value": None}, "deadline_game_s": deadline}
    return {**spec, "kind": "number", "baseline": baseline, "scene": "scene", "last_frame": 0, "last_game_s": 0,
            "directive": dict(spec)}


def _closed(sink: MemorySink) -> dict:
    """The last outcome entry's body, checked to still be a valid `Directive` record."""
    body = sink.of_kind("investigation")[-1]["body"]
    assert body["event"] == "outcome"
    Directive.model_validate(body["directive"])
    return body


def test_an_unchanged_directive_is_a_hold_met_only_by_lasting_to_the_deadline():
    # Its first reading used to meet it, so a hold could never be tested.
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        tracker.adopt(_watch("d-1", 5.0), request_turn=0, based_on_seq=0)
        await tracker.facts([], _observation(1, 1.0, 0))               # first stated to S1; equal to the baseline
        assert tracker.active is not None
        done = [_report(0, "action", status=ExecStatus.completed)]
        await tracker.facts(done, _observation(2, 3.0, 0))
        await tracker.facts(done, _observation(3, 4.0, None))          # unreadable: skipped, neither held nor changed
        assert tracker.active is not None                              # 5.0 is not yet reached
        lines = await tracker.facts(done, _observation(4, 5.5, 0))     # the deadline has passed with the value held
        outcome = _closed(sink)
        assert outcome["outcome"] == "met" and outcome["reason"] == "the value stayed unchanged through the deadline"
        assert outcome["directive"]["outcome"] == "met" and outcome["directive"]["error_class"] is None
        assert "setpoint public.counter unchanged until game_s 5.0" in lines[-1]
        assert "status met (the value stayed unchanged through the deadline)" in lines[-1]

    asyncio.run(main())


def test_a_hold_is_missed_by_the_first_scored_reading_that_changes_the_value():
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        tracker.adopt(_watch("d-1", 5.0), request_turn=0, based_on_seq=0)
        await tracker.facts([], _observation(1, 1.0, 0))
        done = [_report(0, "action", status=ExecStatus.completed)]
        await tracker.facts(done, _observation(2, 2.0, 1))             # changed, with 3 s still to go
        outcome = _closed(sink)
        assert outcome["outcome"] == "not_met" and outcome["reason"] == "the value changed before the deadline"
        assert outcome["directive"]["window"]["inputs_completed"] == 1 and outcome["directive"]["error_class"] is None
        # The window logic of #133 still runs: a change nobody caused leaves a test that never ran.
        tracker.adopt(_watch("d-2", 9.0), request_turn=1, based_on_seq=2)
        await tracker.facts(done, _observation(3, 3.0, 1))             # first stated at 1, so that is the baseline
        await tracker.facts(done + [_report(1, "wait")], _observation(4, 4.0, 2))
        untried = _closed(sink)
        assert untried["outcome"] == "not_met" and untried["directive"]["error_class"] == "no_attempt"
        assert untried["reason"].startswith("the value changed before the deadline; no input completed")

    asyncio.run(main())


def test_a_hold_nobody_could_score_expires_rather_than_passing():
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        tracker.adopt(_watch("d-1", 3.0), request_turn=0, based_on_seq=0)
        await tracker.facts([], _observation(1, 1.0, None))
        await tracker.facts([], _observation(2, 2.0, None))
        assert tracker.active is not None
        await tracker.facts([], _observation(3, 3.5, None))
        outcome = _closed(sink)
        assert outcome["outcome"] == "expired"
        assert outcome["reason"].startswith("the deadline passed without a scoreable reader value")

    asyncio.run(main())


def test_a_hold_with_no_readings_at_all_is_unknown_when_the_session_ends():
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        tracker.adopt(_watch("d-1", 3.0), request_turn=0, based_on_seq=0)
        await tracker.facts([], _observation(1, 1.0, 0))
        tracker.finish(turn=1, based_on_seq=1)                          # a hold still running is not a pass
        assert _closed(sink)["outcome"] == "unknown"

    asyncio.run(main())


def test_changed_and_unchanged_take_their_baseline_when_s1_first_sees_the_directive():
    # The escalation's observation is older by the frontier call and the next turn's pacing (as the deadline was, #133).
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        tracker.adopt(_watch("d-1", 9.0, op="changed", baseline=0), request_turn=0, based_on_seq=0)
        await tracker.facts([], _observation(1, 1.0, 4))               # the counter moved from 0 to 4 meanwhile
        item = tracker.history[0]
        assert (item["baseline"], item["baseline_source"]) == (4, "first_statement")
        assert tracker.active is not None                              # 4 against 0 is not a change S1 caused
        await tracker.facts([], _observation(2, 2.0, 4))
        assert tracker.active is not None
        await tracker.facts([], _observation(3, 3.0, 5))
        assert _closed(sink)["outcome"] == "met"
        tracker.adopt(_watch("d-2", 9.0, op="unchanged", baseline=0), request_turn=1, based_on_seq=3)
        await tracker.facts([], _observation(4, 4.0, 4))
        assert tracker.history[1]["baseline"] == 4 and tracker.history[1]["baseline_source"] == "first_statement"
        await tracker.facts([], _observation(5, 5.0, 4))
        assert tracker.active is not None                              # held at 4, not missed against 0
        await tracker.facts([], _observation(6, 9.5, 4))
        assert _closed(sink)["outcome"] == "met"

    asyncio.run(main())


def test_the_escalations_value_is_only_a_fallback_and_the_record_says_which_was_used():
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        tracker.adopt(_watch("d-1", 3.0, baseline=2), request_turn=0, based_on_seq=0)
        await tracker.facts([], _observation(1, 1.0, None))            # the field cannot be read at first statement
        assert (tracker.active["baseline"], tracker.active["baseline_source"]) == (2, "escalation")
        await tracker.facts([], _observation(2, 2.0, 2))
        await tracker.facts([], _observation(3, 3.5, 2))
        outcome = _closed(sink)
        assert outcome["outcome"] == "met" and "baseline from the escalation" in outcome["reason"]
        tracker.adopt(_watch("d-2", 9.0, baseline=None), request_turn=1, based_on_seq=3)
        await tracker.facts([], _observation(4, 4.0, None))            # and not at the escalation either
        assert (tracker.active["baseline"], tracker.active["baseline_source"]) == (None, None)
        await tracker.facts([], _observation(5, 5.0, 3))               # later readings do not invent one
        assert tracker.active is not None and tracker.active["baseline"] is None
        tracker.adopt(_directive("d-3", 9), request_turn=2, based_on_seq=5)
        await tracker.facts([], _observation(6, 6.0, 0))               # a reach setpoint has no baseline to take
        assert "baseline_source" not in tracker.history[2]

    asyncio.run(main())


def test_the_fact_line_states_a_hold_as_a_hold_and_leaves_the_other_lines_alone():
    async def main():
        tracker = DirectiveTracker(MemorySink())
        tracker.adopt(_watch("d-1", 5.0), request_turn=0, based_on_seq=0)
        assert (await tracker.facts([], _observation(1, 1.0, 0)))[-1] == (
            "Frontier directive d-1: goal Watch the counter (d-1).; setpoint public.counter unchanged until game_s 5.0; "
            "status open.")
        tracker.adopt(_watch("d-2", 6.0, op="changed"), request_turn=1, based_on_seq=1)
        assert (await tracker.facts([], _observation(2, 2.0, 0)))[-1] == (
            "Frontier directive d-2: goal Watch the counter (d-2).; setpoint public.counter changed null; "
            "deadline game_s 6.0; status open.")
        tracker.adopt(_directive("d-3", 7.0), request_turn=2, based_on_seq=2)
        assert (await tracker.facts([], _observation(3, 3.0, 0)))[-1] == (
            "Frontier directive d-3: goal Reach the setpoint (d-3).; setpoint public.counter >= 1; "
            "deadline game_s 7.0; status open.")

    asyncio.run(main())


def _escalation_request(turn: int, counter: int) -> SimpleNamespace:
    """What `FrontierHelp.run` reads when S1 escalates: the context S1 saw, with `counter` as the reading."""
    offered = build_choices(two_actions()).choices
    escalate = next(c for c in offered if c.kind.value == "escalate")
    context = {"as_of": {"seq": 7, "game_s": 2.0, "age_s": 0.1},
               "observation": {"scene": "scene", "playing": True, "paused": False, "time_scale": 1,
                               "public": {"counter": counter}},
               "choice_set_version": 0, "choices": [c.model_dump(mode="json") for c in offered],
               "objective": OBJECTIVE, "facts": []}
    return SimpleNamespace(turn=turn, choice_id=escalate.id, context=context)


def _frontier_asks(expected: dict, deadline_s: float | None):
    """A fake frontier whose one directive carries this setpoint."""
    def send(body):
        packet = json.loads(body["messages"][2]["content"])
        answer = json.loads(_answer_json(packet["observation"]["seq"]))
        answer["directive"].update(expected=expected, deadline_s=deadline_s)
        return 200, {"model": provider.MODEL,
                     "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 5,
                               "prompt_tokens_details": {"cached_tokens": 0}}}, 1.0, None
    return send


def _setpoint(op: str, value, field: str = "public.counter") -> dict:
    return {"field": field, "op": op, "value": value, "tolerance": None}


@pytest.mark.parametrize("expected,deadline_s,problem", [
    (_setpoint("==", 3.0), 10.0, "the setpoint already holds at the request observation, so it tests nothing"),
    (_setpoint(">=", 2.0), 10.0, "the setpoint already holds at the request observation, so it tests nothing"),
    (_setpoint("unchanged", None), None, "needs deadline_s"),
    (_setpoint(">", 3.0), 10.0, None),
    (_setpoint("unchanged", None), 10.0, None),
], ids=["vacuous_equal", "vacuous_at_least", "hold_without_deadline", "reach_not_yet_made", "hold_with_deadline"])
def test_a_vacuous_or_deadline_less_directive_is_not_adopted_and_the_entry_says_why(tmp_path, expected, deadline_s,
                                                                                    problem):
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                        actions=two_actions().actions, skills=[])
        helper = FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                              approval=_approval(), sink=sink, tracker=tracker,
                              started_utc=datetime.now(timezone.utc), send=_frontier_asks(expected, deadline_s),
                              capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1)
        finished = await helper.run(_escalation_request(0, counter=3))  # the counter reads 3 at the request
        result = helper.adopt_result(0, finished).result
        entry = next(row["body"] for row in sink.of_kind("investigation")
                     if row["body"]["event"] == "hypothesis_directive")
        assert [h["id"] for h in result["hypotheses"]] == ["hyp-0-0"]           # only the directive item is rejected
        if problem is None:
            assert result["directive_id"] == "d-0" and "review" not in result
            assert entry["outcome"] == "open" and entry["reason"] is None
            assert tracker.active is not None and tracker.active["baseline"] == 3   # the fallback, as read at the request
        else:
            assert result["directive_id"] is None
            assert [p["item"] for p in result["review"]] == ["directive"] and problem in result["review"][0]["reason"]
            assert entry["directive"] is None and entry["outcome"] == "unknown"
            assert entry["reason"].startswith("the directive was rejected: ") and problem in entry["reason"]
            assert tracker.active is None and tracker.history == []

    asyncio.run(main())


def test_the_frontier_sees_each_fields_calibration_and_the_findings_already_on_record(tmp_path: Path):
    async def main():
        stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number", field_id="f:counter"),
                                                 SimpleNamespace(alias="flag", kind="bool", field_id="f:flag")],
                                        actions=two_actions().actions, skills=[])
        calibration = {"freshness": {"f:counter": {"classification": "input"},
                                     "f:flag": {"classification": "unknown"}}}   # unknown stays unlabelled
        sent = []

        def send(body):
            sent.append(body)
            return _meta_reply(body)

        helper = FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                              approval=_approval(), sink=MemorySink(), tracker=DirectiveTracker(MemorySink()),
                              started_utc=datetime.now(timezone.utc), send=send,
                              capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1,
                              calibration=calibration)
        helper.findings.append({"finding_id": "f-0", "trace_seq": 1, "request_turn": 0,
                                "title": "The count never rises", "repro_check": None})
        await helper.run(_escalation_request(1, counter=3))
        fields = json.loads(sent[0]["messages"][1]["content"])["fields"]
        assert {f["path"]: f.get("calibration") for f in fields} == {
            "public.counter": "changed only while a control was held", "public.flag": None}
        packet = json.loads(sent[0]["messages"][2]["content"])
        assert packet["findings"] == [{"finding_id": "f-0", "title": "The count never rises", "repro_check": None}]

    asyncio.run(main())


# --- the suspicions the frontier is shown ----------------------------------------------------------------------


def _raised(n: int, *, turn: int, basis: SuspicionBasis = SuspicionBasis.drift, subject: str = "public.counter") -> dict:
    """The body of a `suspicion` trace entry, as the loop writes it."""
    return Suspicion(suspicion_id=f"s{n}", basis=basis, turn=turn, based_on_seq=7, subject=subject,
                     statement=f"{subject} set a new low at each of {20 + n} consecutive readings."
                     ).model_dump(mode="json")


def _stated(body: dict) -> dict:
    """What the packet says of a recorded suspicion: five keys, each as recorded."""
    return {key: body[key] for key in ("suspicion_id", "basis", "subject", "turn", "statement")}


def _helper_sending_to(sent: list, tmp_path: Path, sink) -> FrontierHelp:
    """A helper over `sink` whose fake frontier keeps each packet it is sent."""
    def send(body):
        sent.append(json.loads(body["messages"][2]["content"]))
        return _meta_reply(body)

    stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                    actions=two_actions().actions, skills=[])
    return FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                        approval=_approval(), sink=sink, tracker=DirectiveTracker(sink),
                        started_utc=datetime.now(timezone.utc), send=send,
                        capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=2)


def test_the_frontier_sees_every_suspicion_already_in_the_trace(tmp_path: Path):
    async def main():
        sink, sent = harness_trace(), []
        helper = _helper_sending_to(sent, tmp_path, sink)
        await helper.run(_escalation_request(1, counter=3))
        assert sent[0]["suspicions"] == [] and sent[0]["suspicions_omitted"] == 0   # none raised yet: stated as none

        # Every basis, appended out of id order: all are carried, none ranked, in the order they were recorded.
        bodies = [_raised(2, turn=0, basis=SuspicionBasis.range_exit),
                  _raised(1, turn=1),
                  _raised(3, turn=1, basis=SuspicionBasis.invariant, subject="bounded"),
                  _raised(4, turn=2, basis=SuspicionBasis.effect_mismatch, subject="c1")]
        for body in bodies:
            sink.append(TraceKind.suspicion, body, author=TraceAuthor.core, turn=body["turn"])
        sink.append(TraceKind.help, {"event": "opened", "choice_id": "c", "kind": "escalate", "opened_turn": 2,
                                     "source": None, "result": None, "reason": None},
                    author=TraceAuthor.core, turn=2)                                    # other entries are not suspicions
        await helper.run(_escalation_request(2, counter=3))
        assert sent[1]["suspicions"] == [_stated(body) for body in bodies]
        assert sent[1]["suspicions_omitted"] == 0

    asyncio.run(main())


def test_the_frontier_is_shown_suspicions_read_back_from_the_live_trace_file(tmp_path: Path):
    # The live runner's sink is a TraceWriter, which keeps no entries in memory. Its file is read while the writer is
    # still open, so a suspicion recorded between two requests reaches the second one.
    async def main():
        record = RunRecord(run_id="r1", game_project="game", clock_mode=ClockMode.realtime, models=[],
                           objective=OBJECTIVE, budget=Budget(), core_version="0.0.1")
        observed = Observation(session="s", seq=7, game_s=2.0, wall_utc=datetime(2026, 10, 30, tzinfo=timezone.utc),
                               clock_mode="realtime", scene="scene", playing=True, paused=False, time_scale=1.0, frame=1)
        writer = TraceWriter(tmp_path / "trace.jsonl", record)
        sent = []
        try:
            writer.append(TraceKind.observation, observed.model_dump(mode="json"), author=TraceAuthor.bridge, turn=0)
            first = _raised(1, turn=0)
            writer.append(TraceKind.suspicion, first, author=TraceAuthor.core, turn=0)
            helper = _helper_sending_to(sent, tmp_path, writer)
            await helper.run(_escalation_request(1, counter=3))
            second = _raised(2, turn=1, basis=SuspicionBasis.range_exit)
            writer.append(TraceKind.suspicion, second, author=TraceAuthor.core, turn=1)
            await helper.run(_escalation_request(2, counter=3))
        finally:
            writer.close()
        assert sent[0]["suspicions"] == [_stated(first)]
        assert sent[1]["suspicions"] == [_stated(first), _stated(second)]
        assert sent[1]["suspicions_omitted"] == 0

    asyncio.run(main())


def test_a_trace_file_that_cannot_be_read_is_an_error_never_a_missing_suspicion(tmp_path: Path):
    record = RunRecord(run_id="r1", game_project="game", clock_mode=ClockMode.realtime, models=[],
                       objective=OBJECTIVE, budget=Budget(), core_version="0.0.1")
    writer = TraceWriter(tmp_path / "trace.jsonl", record)
    try:
        writer.append(TraceKind.suspicion, _raised(1, turn=0), author=TraceAuthor.core, turn=0)
        assert [row["suspicion_id"] for row in recorded_suspicions(writer)] == ["s1"]
        with writer.path.open("ab") as damage:
            damage.write(b'{"trace_seq":9,')
        with pytest.raises(ValueError, match="final line"):
            recorded_suspicions(writer)
    finally:
        writer.close()


def test_a_sink_that_cannot_be_read_back_is_refused_before_any_call(tmp_path: Path):
    stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                    actions=two_actions().actions, skills=[])
    write_only = SimpleNamespace(append=lambda *args, **kwargs: 0)
    with pytest.raises(ValueError, match="of_kind"):
        FrontierHelp(project=tmp_path, status=None, manifest=stub_manifest, directory=tmp_path / "run",
                     approval=_approval(), sink=write_only, tracker=DirectiveTracker(MemorySink()),
                     started_utc=datetime.now(timezone.utc), send=lambda body: _meta_reply(body),
                     capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1)
    assert not (tmp_path / "run").exists()                                          # nothing was set up for a paid call


def test_reader_calibration_keeps_only_measured_classes():
    readers = [SimpleNamespace(alias="a", field_id="f:a"), SimpleNamespace(alias="b", field_id="f:b"),
               SimpleNamespace(alias="c", field_id="f:c")]
    record = {"freshness": {"f:a": {"classification": "no_change_observed"}, "f:b": {"classification": "unknown"}}}
    assert reader_calibration(record, readers) == {"public.a": "no_change_observed"}
    assert reader_calibration(None, readers) == {} and reader_calibration({"freshness": None}, readers) == {}


def test_the_frontier_sees_what_the_manifest_says_each_field_reads():
    readers = [SimpleNamespace(alias="counter", kind="number", field_id="f:counter", why="Counts collected items",
                               descriptor={"declaring_type": "Game.Score", "name": "m_Count"}),
               SimpleNamespace(alias="flag", kind="bool", field_id="f:flag")]   # a reader with no notes gets none
    notes = reader_notes(readers)
    # The adapter records the two facts it has; saying what the note is belongs to the message that sends it.
    assert notes == {"public.counter": {"source": "Game.Score.m_Count", "preflight_note": "Counts collected items"}}
    manifest = frontier.Manifest(fields={"public.counter": "number", "public.flag": "bool"}, actions={}, skills=[],
                                 field_notes=notes)
    rows = {row["path"]: row for row in json.loads(provider.manifest_message(manifest))["fields"]}
    assert rows["public.counter"] == {
        "path": "public.counter", "kind": "number", "source": "Game.Score.m_Count",
        "preflight_note": "Counts collected items", "preflight_note_origin": "preflight_model_inference",
        "preflight_note_verification": "unverified"}
    assert rows["public.flag"] == {"path": "public.flag", "kind": "bool"}


@pytest.mark.parametrize("descriptor, why, expected", [
    ({"declaring_type": "Owner.A", "name": "m_A"}, "Reason R", {"source": "Owner.A.m_A", "preflight_note": "Reason R"}),
    ({"declaring_type": "Owner.A", "name": "m_A"}, None, {"source": "Owner.A.m_A"}),
    ({"declaring_type": "Owner.A"}, "Reason R", {"preflight_note": "Reason R"}),      # half an identity is not one
    ({"name": "m_A"}, "Reason R", {"preflight_note": "Reason R"}),
    ({"declaring_type": "", "name": "m_A"}, "Reason R", {"preflight_note": "Reason R"}),
    ({"declaring_type": "Owner.A", "name": 7}, "Reason R", {"preflight_note": "Reason R"}),
    ({}, "Reason R", {"preflight_note": "Reason R"}),
    (None, "Reason R", {"preflight_note": "Reason R"}),
    ("not a mapping", "Reason R", {"preflight_note": "Reason R"}),
    ({"declaring_type": "Owner.A", "name": "m_A"}, "", {"source": "Owner.A.m_A"}),
    ({"declaring_type": "Owner.A", "name": "m_A"}, " \n ", {"source": "Owner.A.m_A"}),
    ({"declaring_type": "Owner.A", "name": "m_A"}, 5, {"source": "Owner.A.m_A"}),
    (None, None, None),
], ids=["both", "identity_only", "no_name", "no_type", "empty_type", "name_not_text", "empty_descriptor",
        "no_descriptor", "descriptor_not_a_mapping", "empty_note", "blank_note", "note_not_text", "nothing"])
def test_a_reader_note_holds_only_what_the_manifest_recorded(descriptor, why, expected):
    reader = SimpleNamespace(alias="alpha", kind="number", field_id="f:alpha", why=why, descriptor=descriptor)
    assert reader_notes([reader]) == ({"public.alpha": expected} if expected else {})


def test_a_readers_note_is_kept_exactly_as_the_preflight_model_wrote_it():
    # Nothing trims, rewords, judges or drops a note, so a note is never lost to what it happens to say.
    texts = ["Counts down to the next event", "  Elapsed time since the last event \n", "Fraction used \u2014 \"0 to 1\"",
             "Set while the effect is active", "Reason R"]
    readers = [SimpleNamespace(alias=f"r{i}", kind="number", field_id=f"f:{i}", why=text) for i, text in enumerate(texts)]
    assert {path: note["preflight_note"] for path, note in reader_notes(readers).items()} == {
        f"public.r{i}": text for i, text in enumerate(texts)}


def test_the_request_sent_labels_a_readers_note_an_unverified_inference_beside_its_measured_and_reflected_facts(
        tmp_path: Path):
    note = "Reason R: tracks quantity Q \u2014 a ratio, \"0 to 1\" "          # opaque; exact text must reach the model
    full = [SimpleNamespace(alias="alpha", kind="number", field_id="f:alpha", why=note,
                            descriptor={"declaring_type": "Owner.A", "name": "m_Alpha", "field_type": "System.Single"}),
            SimpleNamespace(alias="beta", kind="number", field_id="f:beta",             # reflected, with no note
                            descriptor={"declaring_type": "Owner.B", "name": "m_Beta"}),
            SimpleNamespace(alias="gamma", kind="bool", field_id="f:gamma", why="Reason G"),   # a note, nothing reflected
            SimpleNamespace(alias="delta", kind="number", field_id="f:delta"),                # nothing recorded
            SimpleNamespace(alias="epsilon", kind="number", field_id="f:epsilon", why="  ",   # a blank note and half an
                            descriptor={"declaring_type": "Owner.E"})]                        # identity are nothing
    bare = [SimpleNamespace(alias=r.alias, kind=r.kind, field_id=r.field_id) for r in full]  # the same fields, no notes
    calibration = {"freshness": {"f:alpha": {"classification": "idle"}, "f:beta": {"classification": "unknown"}}}

    nothing = json.dumps({"add_choices": [], "candidate_finding": None, "directive": None, "hypotheses": [],
                          "prune_choices": []})

    async def send_to(readers, calibration_record, name):
        sent = []

        def send(body):
            sent.append(body)
            return 200, {"model": provider.MODEL, "choices": [{"finish_reason": "stop", "message": {"content": nothing}}],
                         "usage": {"prompt_tokens": 5, "completion_tokens": 5,
                                   "prompt_tokens_details": {"cached_tokens": 0}}}, 1.0, None

        stub = SimpleNamespace(readers=readers, actions=two_actions().actions, skills=[])
        sink = MemorySink()
        helper = FrontierHelp(project=tmp_path, status=None, manifest=stub, directory=tmp_path / name,
                              approval=_approval(), sink=sink, tracker=DirectiveTracker(sink),
                              started_utc=datetime.now(timezone.utc), send=send,
                              capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1,
                              calibration=calibration_record)
        result = await helper.run(_escalation_request(1, counter=3))
        return sent[0], helper, result, sink

    async def main():
        body, helper, result, sink = await send_to(full, calibration, "noted")
        rows = {row["path"]: row for row in json.loads(body["messages"][1]["content"])["fields"]}
        label = {"preflight_note_origin": "preflight_model_inference", "preflight_note_verification": "unverified"}
        assert rows["public.alpha"] == {
            "path": "public.alpha", "kind": "number",
            "calibration": provider.CALIBRATION_LABELS["idle"],       # measured by preflight calibration
            "source": "Owner.A.m_Alpha",                              # reflected from the game
            "preflight_note": note, **label}                          # a model's words, as written, and labelled so
        assert rows["public.beta"] == {"path": "public.beta", "kind": "number", "source": "Owner.B.m_Beta"}
        assert rows["public.gamma"] == {"path": "public.gamma", "kind": "bool", "preflight_note": "Reason G", **label}
        assert rows["public.delta"] == {"path": "public.delta", "kind": "number"}
        assert rows["public.epsilon"] == {"path": "public.epsilon", "kind": "number"}

        # The notes change what the frontier is told about a field, and nothing it may answer or choose: the same
        # system prompt, the same packet (so the same choices), the same closed answer schema, and the same result.
        plain, _, plain_result, plain_sink = await send_to(bare, None, "bare")
        assert body["messages"][1] != plain["messages"][1]
        assert body["messages"][0] == plain["messages"][0]
        assert body["messages"][2] == plain["messages"][2]
        assert body["response_format"] == plain["response_format"]
        assert {k: v for k, v in body.items() if k != "messages"} == {k: v for k, v in plain.items() if k != "messages"}
        packet = json.loads(body["messages"][2]["content"])
        offered = _escalation_request(1, counter=3).context["choices"]
        assert [c["id"] for c in packet["choices"]] == [c["id"] for c in offered]
        assert result.result == plain_result.result                    # the same answer is reviewed the same way
        assert [e["body"] for e in sink.of_kind("investigation")] == [e["body"] for e in plain_sink.of_kind("investigation")]
        assert helper.calls == 1

    asyncio.run(main())
