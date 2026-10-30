from __future__ import annotations

import asyncio
import copy
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from playtest.core.ports import HelpResult
from playtest.core.trace import TraceWriter, read_trace
from playtest.core.trace_sink import MemorySink
from playtest.core.turn_loop import TurnLoop
from playtest.contracts.investigation import Budget, RunRecord
from playtest.frontier import provider
from playtest.frontier.media import Clip
from playtest.roles.frontier import Effect
from playtest.runner.directives import DirectiveTracker
from playtest.runner.frontier_help import FrontierHelp
from playtest.runner import report, reproduce
from test_decision_memory import Bridge, Probe, S1, WALL, choices, observation
from test_frontier_help import _answer_json, _approval, _escalation_request, _observation, _report
from test_runner_session import two_actions


def _reply(answer):
    return 200, {"model": provider.MODEL,
                 "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 5,
                           "prompt_tokens_details": {"cached_tokens": 0}}}, 1.0, None


def _helper(tmp_path, send, *, max_calls=4, sink=None, effects=None):
    sink = sink if sink is not None else MemorySink()
    tracker = DirectiveTracker(sink)
    approval = _approval()
    approval["max_calls"] = approval["arms"][provider.MODEL]["max_calls"] = max_calls
    approval["max_cost_usd"] = approval["arms"][provider.MODEL]["max_cost_usd"] = max_calls
    manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                               actions=two_actions().actions, skills=[])
    helper = FrontierHelp(project=tmp_path, status=None, manifest=manifest, directory=tmp_path / "run",
                          approval=approval, sink=sink, tracker=tracker, started_utc=datetime.now(timezone.utc),
                          send=send, capture=lambda **_kwargs: Clip(None, None, "offline fake", None),
                          max_calls=max_calls, effects=effects)
    return helper, tracker, sink


def _answer_for(packet):
    answer = json.loads(_answer_json(packet["observation"]["seq"]))
    hypothesis_id = packet["new_hypothesis_ids"][0]
    answer["hypotheses"][0]["id"] = hypothesis_id
    answer["directive"]["tests"] = hypothesis_id
    return answer


def _send(body):
    return _reply(_answer_for(json.loads(body["messages"][2]["content"])))


def _candidate(seq, *, effect=True):
    return {"actual": "The measured result is small.", "conditions": "During the attempt.",
            "evidence_seq": [seq], "expected": "The calibrated result is larger.",
            "expected_source": "interpretation", "repro_check": (
                {"field": "effect.move", "op": "<", "value": 1.0, "tolerance": None} if effect else None),
            "severity": "major", "severity_reason": "The action may not work.", "steps": ["Try the action."],
            "title": "Small result", "uncertainty": "One observation."}


def _candidate_send(packets, *, cited=100, effect=True):
    def send(body):
        packet = json.loads(body["messages"][2]["content"])
        packets.append(packet)
        answer = _answer_for(packet)
        answer["candidate_finding"] = _candidate(cited, effect=effect)
        return _reply(answer)
    return send


def _send_candidate_current(body):
    packet = json.loads(body["messages"][2]["content"])
    answer = _answer_for(packet)
    answer["candidate_finding"] = _candidate(packet["observation"]["seq"], effect=False)
    return _reply(answer)


async def _history_request(sink, *, replayable=False):
    """Generate the attempt through the real loop, including an alias whose action is 'move'."""
    catalog = choices()
    catalog[0] = catalog[0].model_copy(update={"action": "move"})
    class TimedBridge(Bridge):
        async def act(self, **request):
            records = await super().act(**request)
            return [record.model_copy(update={"input_start_wall_s": 1000.0}) for record in records]

    bridge = TimedBridge([observation(100)]) if replayable else Bridge([observation(100)])
    probe = Probe()
    probe.observed.update(from_inspection_seq=610, to_inspection_seq=611)
    loop = TurnLoop(bridge, choices=catalog, objective="Inspect.", s1=S1(["a"]),
                    sink=sink, effects=probe, clock=lambda: WALL)
    await loop.run_turn()
    request = _escalation_request(1, counter=0)
    request.context["recent_history"] = loop._memory.snapshot()
    sink.append("observation", observation(7).model_dump(mode="json"), author="bridge", turn=1)
    return request


def test_finding_is_private_until_matching_adoption_and_keeps_actual_effect_evidence(tmp_path):
    async def main():
        sink, packets = MemorySink(), []
        request = await _history_request(sink)
        helper, _, _ = _helper(tmp_path, _candidate_send(packets), sink=sink,
                                effects={"move": Effect(expected=2.0)})
        attempt = request.context["recent_history"]["action_attempts"]["items"][0]
        finished = await helper.run(request)
        assert sink.of_kind("finding") == [] and helper.findings == []
        assert "candidate_finding" not in finished.result
        second = await helper.run(_escalation_request(2, counter=0))
        assert packets[1]["findings"] == []
        helper.discard_result(2)
        assert helper.adopt_result(2, second) is second
        published = helper.adopt_result(1, finished)
        assert published.result["candidate_finding"] == {"finding_id": "f-1", "reason": None}
        finding = sink.of_kind("finding")[0]
        expected = sorted([attempt["observation_trace_seq"], attempt["trace_seq"], attempt["effect_trace_seq"]])
        assert finding["refs"] == finding["body"]["evidence_seq"] == expected
        assert sink.entries[attempt["effect_trace_seq"]]["body"]["observed"]["to_inspection_seq"] == 611
        assert 610 not in finding["refs"] and 611 not in finding["refs"]
        assert finding["body"]["state"] == "candidate" and finding["body"]["repro"] is None
        assert sink.entries[attempt["trace_seq"]]["body"]["request"]["choice_id"] == "a"
        assert sink.entries[attempt["trace_seq"]]["body"]["request"]["action"] == "move"
        assert helper.adopt_result(1, finished) is published
        assert helper.adopt_result(1, published) is published
        assert len(sink.of_kind("finding")) == len(helper.findings) == 1
        await helper.run(_escalation_request(3, counter=0))
        assert [row["finding_id"] for row in packets[2]["findings"]] == ["f-1"]

    asyncio.run(main())


def test_all_attempts_forwarded_under_a_cited_observation_are_retained(tmp_path):
    async def main():
        sink, packets = MemorySink(), []
        request = await _history_request(sink)
        window = request.context["recent_history"]["action_attempts"]
        first = window["items"][0]
        second = copy.deepcopy(first)
        execution = sink.entries[first["trace_seq"]]
        effect = copy.deepcopy(sink.entries[first["effect_trace_seq"]]["body"])
        effect["observed"] = {"metric": {"delta": 0.3}}
        second["trace_seq"] = sink.append("execution", execution["body"], author="bridge", turn=0,
                                           refs=execution["refs"])
        second["effect_trace_seq"] = sink.append("effect", effect, author="core", turn=0,
                                                  refs=[second["trace_seq"]])
        second["observed_effect"] = effect["observed"]
        window["items"].append(second)
        window["total"] += 1
        helper, _, _ = _helper(tmp_path, _candidate_send(packets), sink=sink,
                                effects={"move": Effect(expected=2.0)})
        published = helper.adopt_result(1, await helper.run(request))
        assert published.result["candidate_finding"]["reason"] is None
        assert sink.of_kind("finding")[0]["refs"] == sorted({first["observation_trace_seq"],
            first["trace_seq"], first["effect_trace_seq"], second["trace_seq"], second["effect_trace_seq"]})
    asyncio.run(main())


def test_file_trace_snapshot_cites_observation_execution_and_effect(tmp_path):
    async def main():
        run = RunRecord(run_id="r1", game_project="game", clock_mode="realtime", models=[],
                        objective="Inspect.", budget=Budget(), core_version="0.0.1")
        sink = TraceWriter(tmp_path / "trace.jsonl", run)
        try:
            request = await _history_request(sink, replayable=True)
            row = request.context["recent_history"]["action_attempts"]["items"][0]
            helper, _, _ = _helper(tmp_path, _candidate_send([]), sink=sink,
                                    effects={"move": Effect(expected=2.0)})
            published = helper.adopt_result(1, await helper.run(request))
            assert published.result["candidate_finding"]["reason"] is None
            finding = next(entry for entry in read_trace(sink.path) if entry.kind.value == "finding")
            assert finding.refs == sorted([row["observation_trace_seq"], row["trace_seq"], row["effect_trace_seq"]])
            entries = [entry.model_dump(mode="json") for entry in read_trace(sink.path)]
            reported = report.evidence({entry["trace_seq"]: entry for entry in entries}, finding.body, None)
            assert [entry["kind"] for entry in reported] == ["observation", "execution", "effect"]
            assert reported[0]["bridge_seq"] == 100 and "bridge_seq" not in reported[1]
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(two_actions().model_dump_json(), encoding="utf-8")
            plan = reproduce.build_plan(sink.path, manifest_path)
            assert plan["effect"]["scored_trace_seq"] == row["trace_seq"]
            assert plan["inputs"][0]["choice_id"] == "a" and plan["inputs"][0]["action"] == "move"
        finally:
            sink.close()
    asyncio.run(main())


@pytest.mark.parametrize("tamper", ["observation_ref", "execution_ref", "effect_ref", "missing_effect",
                                    "source_binding", "choice_binding", "action_binding", "effect_link",
                                    "expected", "observed", "terminal", "missing_provenance"])
def test_forged_or_unavailable_history_provenance_refuses_the_whole_finding(tmp_path, tamper):
    async def main():
        sink, packets = MemorySink(), []
        request = await _history_request(sink)
        row = request.context["recent_history"]["action_attempts"]["items"][0]
        execution = sink.entries[row["trace_seq"]]
        effect = sink.entries[row["effect_trace_seq"]]
        if tamper == "observation_ref":
            row["observation_trace_seq"] = row["trace_seq"]
        elif tamper == "execution_ref":
            row["trace_seq"] = row["observation_trace_seq"]
        elif tamper == "effect_ref":
            row["effect_trace_seq"] = 99999
        elif tamper == "missing_effect":
            row["effect_trace_seq"] = None
        elif tamper == "source_binding":
            execution["body"]["request"]["based_on_seq"] = 101
        elif tamper == "choice_binding":
            row["choice_id"] = "other"
        elif tamper == "action_binding":
            effect["body"]["action"] = "other"
        elif tamper == "effect_link":
            effect["refs"] = []
        elif tamper in ("expected", "observed"):
            row[f"{tamper}_effect"]["metric"]["delta"] = 999
        elif tamper == "terminal":
            row["release_verified"] = False
        else:
            del row["observed_effect"]
        helper, _, _ = _helper(tmp_path, _candidate_send(packets), sink=sink,
                                effects={"move": Effect(expected=2.0)})
        finished = await helper.run(request)
        published = helper.adopt_result(1, finished)
        assert published.result["candidate_finding"]["reason"].startswith("not recorded:")
        assert sink.of_kind("finding") == [] and helper.findings == []

    asyncio.run(main())


def test_observation_only_finding_and_future_effect_check_need_no_past_action(tmp_path):
    async def main():
        sink, packets = MemorySink(), []
        obs = sink.append("observation", {"seq": 7}, author="bridge", turn=0)
        helper, _, _ = _helper(tmp_path, _candidate_send(packets, cited=7), sink=sink,
                                effects={"move": Effect(expected=2.0)})
        finished = await helper.run(_escalation_request(0, counter=0))
        published = helper.adopt_result(0, finished)
        assert published.result["candidate_finding"]["reason"] is None
        finding = sink.of_kind("finding")[0]
        assert finding["refs"] == [obs] and finding["body"]["state"] == "candidate"
        assert finding["body"]["repro"] is None
    asyncio.run(main())


def test_finding_snapshot_cannot_gain_later_evidence_or_mutated_context(tmp_path):
    async def main():
        sink, packets = MemorySink(), []
        request = await _history_request(sink)
        row = request.context["recent_history"]["action_attempts"]["items"][0]
        original_effect = copy.deepcopy(sink.entries[row["effect_trace_seq"]])
        row["effect_trace_seq"] = len(sink.entries)  # a future trace entry is not yet evidence
        started, release = threading.Event(), threading.Event()
        send = _candidate_send(packets)
        def blocked(body):
            started.set()
            assert release.wait(3)
            return send(body)
        helper, _, _ = _helper(tmp_path, blocked, sink=sink, effects={"move": Effect(expected=2.0)})
        task = asyncio.create_task(helper.run(request))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            sink.append("effect", original_effect["body"], author="core", turn=0, refs=original_effect["refs"])
            row["effect_trace_seq"] = original_effect["trace_seq"]
        finally:
            release.set()
        published = helper.adopt_result(1, await task)
        assert published.result["candidate_finding"]["reason"].startswith("not recorded:")
        assert sink.of_kind("finding") == [] and helper.findings == []
        assert packets[0]["recent"][1]["effect_trace_seq"] != original_effect["trace_seq"]
    asyncio.run(main())


def test_observation_added_during_provider_call_is_not_new_evidence(tmp_path):
    async def main():
        started, release = threading.Event(), threading.Event()
        def send(body):
            started.set()
            assert release.wait(3)
            return _send_candidate_current(body)
        helper, _, sink = _helper(tmp_path, send)
        task = asyncio.create_task(helper.run(_escalation_request(0, counter=0)))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            sink.append("observation", {"seq": 7}, author="bridge", turn=0)
        finally:
            release.set()
        published = helper.adopt_result(0, await task)
        assert "not recorded:" in published.result["candidate_finding"]["reason"]
        assert sink.of_kind("finding") == [] and helper.findings == []
        assert sink.of_kind("investigation")[0]["body"]["evidence_trace_seq"] is None
    asyncio.run(main())


def test_completion_cannot_start_a_scoring_window_before_adoption(tmp_path):
    async def main():
        helper, tracker, sink = _helper(tmp_path, _send)
        finished = await helper.run(_escalation_request(0, counter=0))
        assert tracker.active is None and tracker.history == []
        assert helper.hypotheses == {} and helper._shown == {}
        assert [entry["body"]["event"] for entry in sink.of_kind("investigation")] == ["hypothesis_directive"]
        reports = [_report(0, "escalate"), _report(1, "wait")]
        assert not any("Frontier directive" in line for line in
                       await tracker.facts(reports, _observation(8, 3.0, 1)))
        assert tracker.history == []  # even a satisfying reading cannot close the unstated directive

        published = helper.adopt_result(0, finished)
        assert published.result["directive_id"] == "d-0"
        assert published.result["hypotheses"][0]["id"] == "hyp-0-0"
        assert tracker.active["first_turn"] is None and len(tracker.history) == 1
        assert helper.adopt_result(0, finished) is published
        assert helper.adopt_result(0, published) is published
        assert len(tracker.history) == 1
        lines = await tracker.facts(reports, _observation(9, 4.0, 0))
        assert "status open" in lines[-1]
        assert tracker.active["first_turn"] == 2 and tracker.active["first_game_s"] == 4.0
        assert tracker.active["window"]["turns"] == 0
        assert tracker.active["deadline_game_s"] == 14.0
        assert len([e for e in sink.of_kind("investigation") if e["body"]["event"] == "directive"]) == 1

    asyncio.run(main())


def test_discard_keeps_unadopted_hypotheses_and_directive_out_of_later_packets(tmp_path):
    async def main():
        packets = []
        def send(body):
            packet = json.loads(body["messages"][2]["content"])
            packets.append(packet)
            return _send_candidate_current(body)

        helper, tracker, sink = _helper(tmp_path, send)
        sink.append("observation", {"seq": 7}, author="bridge", turn=0)
        first = await helper.run(_escalation_request(0, counter=0))
        helper.discard_result(0)
        assert helper.adopt_result(0, first) is first
        assert sink.of_kind("finding") == [] and helper.findings == []
        assert helper.hypotheses == {} and helper._shown == {} and tracker.history == []
        second = await helper.run(_escalation_request(1, counter=0))
        assert packets[1]["hypotheses"] == [] and packets[1]["directive"] is None and packets[1]["findings"] == []
        published = helper.adopt_result(1, second)
        assert [h["id"] for h in published.result["hypotheses"]] == ["hyp-1-0"]
        assert tracker.active["directive_id"] == "d-1"
        assert [e["body"]["event"] for e in sink.of_kind("investigation")] == [
            "hypothesis_directive", "hypothesis_directive", "directive"]
        assert len(helper.ledger.rows) == 2 and all(row["parse_valid"] for row in helper.ledger.rows)

    asyncio.run(main())


def test_a_replacement_or_mutated_result_cannot_publish_the_staged_answer(tmp_path):
    async def main():
        helper, tracker, sink = _helper(tmp_path, _send_candidate_current)
        sink.append("observation", {"seq": 7}, author="bridge", turn=0)
        first = await helper.run(_escalation_request(0, counter=0))
        # Even a replacement with equal contents must not acquire this request's staged state.
        replacement = HelpResult(source=first.source, result=copy.deepcopy(first.result))
        assert helper.adopt_result(0, replacement) is replacement
        assert helper.adopt_result(0, first) is first
        assert tracker.history == [] and helper.hypotheses == {} and helper._shown == {}
        assert sink.of_kind("finding") == [] and helper.findings == []

        second = await helper.run(_escalation_request(1, counter=0))
        second.result["hypotheses"][0]["statement"] = "replacement content"
        assert helper.adopt_result(1, second) is second
        assert tracker.history == [] and helper.hypotheses == {} and helper._shown == {}
        assert sink.of_kind("finding") == [] and helper.findings == []

    asyncio.run(main())


def test_replacement_before_completion_also_prevents_late_publication(tmp_path):
    async def main():
        started, release = threading.Event(), threading.Event()
        def send(body):
            started.set()
            assert release.wait(3)
            return _send_candidate_current(body)

        helper, tracker, sink = _helper(tmp_path, send)
        sink.append("observation", {"seq": 7}, author="bridge", turn=0)
        task = asyncio.create_task(helper.run(_escalation_request(0, counter=0)))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            replacement = HelpResult(source="other", result={"message": "replacement"})
            assert helper.adopt_result(0, replacement) is replacement
        finally:
            release.set()
        finished = await task
        assert helper.adopt_result(0, finished) is finished
        assert tracker.history == [] and helper.hypotheses == {} and helper._shown == {}
        assert sink.of_kind("finding") == [] and helper.findings == []
        assert helper.ledger.rows[0]["parse_valid"] is True

    asyncio.run(main())


def test_cancelled_request_cannot_leave_a_stage_or_poison_a_later_packet(tmp_path):
    async def main():
        started, release = threading.Event(), threading.Event()
        packets = []
        def send(body):
            packet = json.loads(body["messages"][2]["content"])
            packets.append(packet)
            if len(packets) == 1:
                started.set()
                assert release.wait(3)
            return _send_candidate_current(body)

        helper, tracker, sink = _helper(tmp_path, send)
        sink.append("observation", {"seq": 7}, author="bridge", turn=0)
        task = asyncio.create_task(helper.run(_escalation_request(0, counter=0)))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            helper.discard_result(0)
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert tracker.history == [] and helper.hypotheses == {} and helper._shown == {}
        assert sink.of_kind("finding") == [] and helper.findings == []
        assert helper.ledger.rows[0]["directive_result"] == "cancelled"
        assert helper.ledger.rows[0]["cost_usd"] is None
        second = await helper.run(_escalation_request(1, counter=0))
        assert packets[1]["hypotheses"] == [] and packets[1]["directive"] is None and packets[1]["findings"] == []
        assert helper.adopt_result(1, second).result["hypotheses"][0]["id"] == "hyp-1-0"

    asyncio.run(main())


def test_cancellation_resistant_late_completion_cannot_publish_a_finding(tmp_path, monkeypatch):
    async def main():
        started, release = threading.Event(), threading.Event()
        packets = []
        send_answer = _candidate_send(packets, cited=7, effect=False)
        def send(body):
            started.set()
            assert release.wait(3)
            return send_answer(body)

        original_to_thread = asyncio.to_thread
        async def resistant(func, *args, **kwargs):
            if func is not send:
                return await original_to_thread(func, *args, **kwargs)
            worker = asyncio.create_task(original_to_thread(func, *args, **kwargs))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                return await worker  # a provider adapter can finish even after cancellation

        monkeypatch.setattr(asyncio, "to_thread", resistant)
        sink = MemorySink()
        sink.append("observation", {"seq": 7}, author="bridge", turn=0)
        helper, _, _ = _helper(tmp_path, send, sink=sink)
        task = asyncio.create_task(helper.run(_escalation_request(0, counter=0)))
        try:
            assert await original_to_thread(started.wait, 3)
            helper.discard_result(0)
            task.cancel()
        finally:
            release.set()
        finished = await task
        assert helper.adopt_result(0, finished) is finished
        assert helper.findings == [] and sink.of_kind("finding") == []
        assert helper.ledger.rows[0]["parse_valid"] is True
        await helper.run(_escalation_request(1, counter=0))
        assert packets[1]["findings"] == []
    asyncio.run(main())


@pytest.mark.parametrize("field,value", [
    ("statement", "A different statement."),
    ("confirm_if", "A different confirmation."),
    ("refute_if", "A different refutation."),
    ("status", "contradicted"),
    ("supporting_seq", [7, 8]),
    ("contradicting_seq", [7]),
])
def test_every_changed_hypothesis_field_is_shown_in_full(tmp_path, field, value):
    helper, _, _ = _helper(tmp_path, _send)
    row = json.loads(_answer_json(7))["hypotheses"][0]
    assert helper._guidance({"hypotheses": [row]})["hypotheses"] == [row]
    changed = {**row, field: value}
    view = helper._guidance({"hypotheses": [changed]})
    assert view["hypotheses"] == [changed] and "restated_hypotheses" not in view


def test_discarded_revision_does_not_change_prior_hypothesis_or_compaction(tmp_path):
    async def main():
        packets = []
        def send(body):
            packet = json.loads(body["messages"][2]["content"])
            packets.append(packet)
            answer = _answer_for(packet)
            if len(packets) > 1:
                answer["hypotheses"][0]["id"] = "hyp-0-0"
                answer["directive"] = None
            if len(packets) == 2:
                answer["hypotheses"][0].update(statement="Discard this revision.", contradicting_seq=[7])
            return _reply(answer)

        helper, _, _ = _helper(tmp_path, send)
        first = await helper.run(_escalation_request(0, counter=0))
        first_view = helper.adopt_result(0, first).result
        discarded = await helper.run(_escalation_request(1, counter=0))
        helper.discard_result(1)
        assert helper.adopt_result(1, discarded) is discarded
        third = await helper.run(_escalation_request(2, counter=0))
        assert packets[2]["hypotheses"] == first_view["hypotheses"]
        final_view = helper.adopt_result(2, third).result
        assert final_view["hypotheses"] == [] and final_view["restated_hypotheses"] == ["hyp-0-0"]
        assert helper.hypotheses["hyp-0-0"]["contradicting_seq"] == []

    asyncio.run(main())


def test_revisions_and_contradictions_are_full_and_only_identical_versions_compact(tmp_path):
    async def main():
        packets, answers = [], []
        def send(body):
            packet = json.loads(body["messages"][2]["content"])
            packets.append(packet)
            answer = _answer_for(packet)
            if answers:
                answer["hypotheses"][0]["id"] = "hyp-0-0"
                answer["directive"] = None
                answer["hypotheses"][0].update(statement="The opposite may be true.",
                                                supporting_seq=[], contradicting_seq=[7])
            if len(answers) == 3:
                answer["hypotheses"][0]["status"] = "contradicted"
            answers.append(copy.deepcopy(answer))
            return _reply(answer)

        helper, _, sink = _helper(tmp_path, send)
        guidance = []
        for turn in range(4):
            finished = await helper.run(_escalation_request(turn, counter=0))
            guidance.append(helper.adopt_result(turn, finished).result)

        assert guidance[0]["hypotheses"] == answers[0]["hypotheses"]
        assert guidance[1]["hypotheses"] == answers[1]["hypotheses"]  # same status, new text and evidence
        assert guidance[2]["hypotheses"] == [] and guidance[2]["restated_hypotheses"] == ["hyp-0-0"]
        assert guidance[3]["hypotheses"] == answers[3]["hypotheses"]  # status change in full
        assert guidance[0]["hypotheses"][0]["supporting_seq"] == [7]
        assert guidance[1]["hypotheses"][0]["contradicting_seq"] == [7]
        assert packets[2]["hypotheses"][0]["supporting_seq"] == [7]
        assert packets[2]["hypotheses"][0]["contradicting_seq"] == [7]
        assert helper.hypotheses["hyp-0-0"]["supporting_seq"] == [7]
        assert helper.hypotheses["hyp-0-0"]["contradicting_seq"] == [7]
        investigations = [e["body"]["hypothesis"] for e in sink.of_kind("investigation")
                          if e["body"]["event"] == "hypothesis_directive"]
        assert investigations == [answer["hypotheses"] for answer in answers]
        assert [row["answer"] for row in sorted(helper.ledger.rows, key=lambda row: row["turn"])] == answers

    asyncio.run(main())


def test_unknown_values_remain_unknown_across_publication(tmp_path):
    async def main():
        def send(body):
            packet = json.loads(body["messages"][2]["content"])
            answer = _answer_for(packet)
            answer["directive"]["expected"] = {"field": "public.counter", "op": "changed",
                                                "value": None, "tolerance": None}
            return _reply(answer)

        helper, tracker, _ = _helper(tmp_path, send)
        request = _escalation_request(0, counter=None)
        request.context["as_of"]["game_s"] = None
        finished = await helper.run(request)
        assert finished.result["directive"]["deadline_game_s"] is None
        helper.adopt_result(0, finished)
        assert tracker.active["baseline"] is None and tracker.active["requested_game_s"] is None
        await tracker.facts([], _observation(8, None, None))
        assert tracker.active["baseline"] is None and tracker.active["baseline_source"] is None
        assert tracker.active["deadline_game_s"] is None and tracker.active["status"] == "open"

    asyncio.run(main())


def test_cap_and_invalid_answer_do_not_publish_new_directives(tmp_path):
    async def main():
        helper, tracker, _ = _helper(tmp_path / "cap", _send, max_calls=1)
        first = await helper.run(_escalation_request(0, counter=0))
        helper.adopt_result(0, first)
        active = tracker.active
        shown = copy.deepcopy(helper._shown)
        capped = await helper.run(_escalation_request(1, counter=0))
        assert helper.adopt_result(1, capped) is capped and capped.result["directive"] is None
        assert tracker.active is active and len(tracker.history) == 1 and helper._shown == shown

        def invalid(_body):
            return 200, {"model": provider.MODEL, "choices": [{"finish_reason": "stop",
                    "message": {"content": "not-json"}}]}, 1.0, None
        failed, failed_tracker, sink = _helper(tmp_path / "invalid", invalid)
        result = await failed.run(_escalation_request(0, counter=0))
        assert failed.adopt_result(0, result) is result and result.result["directive"] is None
        assert failed_tracker.history == [] and failed.hypotheses == {} and failed._shown == {}
        assert sink.of_kind("investigation")[-1]["body"]["outcome"] == "unknown"
        assert failed.ledger.rows[0]["parse_valid"] is False

    asyncio.run(main())
