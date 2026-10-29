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
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.trace import TraceEntry
from playtest.core.bridge_client import BridgeClient
from playtest.core.ports import S1Answer, S1Call
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
        ready = threading.Event()
        suspicion_fact = "Suspicion fact (test fixture): a reader trend is under review."

        def fake_send(body):
            packet = json.loads(body["messages"][2]["content"])
            assert suspicion_fact in [fact["text"] for fact in packet["facts"]]
            result = _meta_reply(body)
            ready.set()
            return result

        class S1:
            contexts = []
            def answer(self, context, choice, while_pending=None):
                value = {"choice_id": choice, "set_version": context["choice_set_version"]}
                if while_pending is not None:
                    value["while_pending"] = while_pending
                return S1Answer(answer=value, calls=(S1Call(question="next_action", provider="fake", model="fake",
                                                            http_status=200, latency_ms=1.0),))
            async def decide(self, context):
                self.contexts.append(context)
                if len(self.contexts) == 1:
                    return self.answer(context, "escalate", {"mode": "continue", "choice_id": "move"})
                await asyncio.to_thread(ready.wait, 3)
                for _ in range(100):
                    if tracker.active is not None:
                        break
                    await asyncio.sleep(0.01)
                assert any("Frontier directive" in line for line in context["facts"])
                assert suspicion_fact in context["facts"]
                assert suspicion_fact in context["facts"]
                assert context["guidance"][0]["result"]["hypotheses"][0]["confirm_if"] == "The count reaches one."
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
        failures = []
        class ObserveFailure:
            async def run(self, request):
                try:
                    return await helper.run(request)
                except Exception as exc:
                    import traceback
                    failures.append(traceback.format_exc())
                    raise
        try:
            fake_s1 = S1()
            result = await session(client, build_choices(two_actions()), s1=fake_s1, objective=OBJECTIVE,
                                   help=ObserveFailure(), facts=StubSuspicionFacts(), sink=sink, wall_limit_s=10.0)
            assert result.status == "complete", f"{result.error}; failures={failures}; help={sink.of_kind('help')}; invest={sink.of_kind('investigation')}"
            assert helper.calls == 1
            assert helper.ledger.rows[0]["status"] == "finished"
            assert helper.ledger.rows[0]["cost_usd"] is not None
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


def test_invalid_frontier_answer_is_recorded_without_synthesizing_hypothesis_or_directive(tmp_path: Path):
    async def main():
        game, client = await connected()
        sink = harness_trace()
        ready = threading.Event()
        tracker = DirectiveTracker(sink)
        stub_manifest = SimpleNamespace(readers=[SimpleNamespace(alias="counter", kind="number")],
                                        actions=two_actions().actions, skills=[])
        def invalid_send(_body):
            ready.set()
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
                await asyncio.to_thread(ready.wait, 3)
                assert "guidance" in context
                assert context["guidance"][-1]["result"]["directive"] is None
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
