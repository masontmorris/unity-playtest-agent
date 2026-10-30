from __future__ import annotations

import asyncio
import copy
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from playtest.core.ports import HelpResult
from playtest.core.trace_sink import MemorySink
from playtest.frontier import provider
from playtest.frontier.media import Clip
from playtest.runner.directives import DirectiveTracker
from playtest.runner.frontier_help import FrontierHelp
from test_frontier_help import _answer_json, _approval, _escalation_request, _observation, _report
from test_runner_session import two_actions


def _reply(answer):
    return 200, {"model": provider.MODEL,
                 "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 5,
                           "prompt_tokens_details": {"cached_tokens": 0}}}, 1.0, None


def _helper(tmp_path, send, *, max_calls=4):
    sink = MemorySink()
    tracker = DirectiveTracker(sink)
    approval = _approval()
    approval["max_calls"] = approval["arms"][provider.MODEL]["max_calls"] = max_calls
    approval["max_cost_usd"] = approval["arms"][provider.MODEL]["max_cost_usd"] = max_calls
    manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                               actions=two_actions().actions, skills=[])
    helper = FrontierHelp(project=tmp_path, status=None, manifest=manifest, directory=tmp_path / "run",
                          approval=approval, sink=sink, tracker=tracker, started_utc=datetime.now(timezone.utc),
                          send=send, capture=lambda **_kwargs: Clip(None, None, "offline fake", None),
                          max_calls=max_calls)
    return helper, tracker, sink


def _answer_for(packet):
    answer = json.loads(_answer_json(packet["observation"]["seq"]))
    hypothesis_id = packet["new_hypothesis_ids"][0]
    answer["hypotheses"][0]["id"] = hypothesis_id
    answer["directive"]["tests"] = hypothesis_id
    return answer


def _send(body):
    return _reply(_answer_for(json.loads(body["messages"][2]["content"])))


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
            return _reply(_answer_for(packet))

        helper, tracker, sink = _helper(tmp_path, send)
        first = await helper.run(_escalation_request(0, counter=0))
        helper.discard_result(0)
        assert helper.adopt_result(0, first) is first
        assert helper.hypotheses == {} and helper._shown == {} and tracker.history == []
        second = await helper.run(_escalation_request(1, counter=0))
        assert packets[1]["hypotheses"] == [] and packets[1]["directive"] is None
        published = helper.adopt_result(1, second)
        assert [h["id"] for h in published.result["hypotheses"]] == ["hyp-1-0"]
        assert tracker.active["directive_id"] == "d-1"
        assert [e["body"]["event"] for e in sink.of_kind("investigation")] == [
            "hypothesis_directive", "hypothesis_directive", "directive"]
        assert len(helper.ledger.rows) == 2 and all(row["parse_valid"] for row in helper.ledger.rows)

    asyncio.run(main())


def test_a_replacement_or_mutated_result_cannot_publish_the_staged_answer(tmp_path):
    async def main():
        helper, tracker, _ = _helper(tmp_path, _send)
        first = await helper.run(_escalation_request(0, counter=0))
        # Even a replacement with equal contents must not acquire this request's staged state.
        replacement = HelpResult(source=first.source, result=copy.deepcopy(first.result))
        assert helper.adopt_result(0, replacement) is replacement
        assert helper.adopt_result(0, first) is first
        assert tracker.history == [] and helper.hypotheses == {} and helper._shown == {}

        second = await helper.run(_escalation_request(1, counter=0))
        second.result["hypotheses"][0]["statement"] = "replacement content"
        assert helper.adopt_result(1, second) is second
        assert tracker.history == [] and helper.hypotheses == {} and helper._shown == {}

    asyncio.run(main())


def test_replacement_before_completion_also_prevents_late_publication(tmp_path):
    async def main():
        started, release = threading.Event(), threading.Event()
        def send(body):
            started.set()
            assert release.wait(3)
            return _send(body)

        helper, tracker, _ = _helper(tmp_path, send)
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
            return _reply(_answer_for(packet))

        helper, tracker, _ = _helper(tmp_path, send)
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
        assert helper.ledger.rows[0]["directive_result"] == "cancelled"
        assert helper.ledger.rows[0]["cost_usd"] is None
        second = await helper.run(_escalation_request(1, counter=0))
        assert packets[1]["hypotheses"] == [] and packets[1]["directive"] is None
        assert helper.adopt_result(1, second).result["hypotheses"][0]["id"] == "hyp-1-0"

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
