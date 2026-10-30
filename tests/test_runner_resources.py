"""Resource visibility is a measurement, including when S1 chooses exhausted help."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from loop_fakes import FakeS1, FakeTime, pick
from test_runner_session import OBJECTIVE, closed, connected, session, two_actions

from playtest.contracts.bridge import Observation
from playtest.core.ports import HelpResult
from playtest.runner.choices import build_choices
from playtest.runner.resources import PREFIX, SessionFacts


def test_elapsed_time_and_live_call_counts_remain_distinct_from_unknowns():
    clock = FakeTime()
    s1 = SimpleNamespace(calls_reserved=2)
    helper = SimpleNamespace(calls=4, max_calls=5)
    facts = SessionFacts(started=clock.now, wall_limit_s=120, clock=clock.clock,
                         s1=s1, s1_call_cap=10, help=helper)
    first = facts.snapshot()
    assert first["wall_s"] == {"elapsed": 0.0, "limit": 120, "remaining": 120.0}
    assert first["s1_calls"] == {"reserved": 2, "cap": 10, "remaining": 8, "state": "within_call_cap"}
    assert first["frontier_calls"]["remaining"] == 1
    clock.now += 17
    helper.calls = 5
    s1.calls_reserved = 3
    later = facts.snapshot()
    assert later["wall_s"]["remaining"] == 103
    assert later["frontier_calls"] == {"reserved": 5, "cap": 5, "remaining": 0, "state": "call_cap_exhausted"}
    assert later["s1_calls"]["remaining"] == 7
    assert first["frontier_calls"]["remaining"] == 1  # earlier payload is a snapshot
    clock.now += 200
    assert facts.snapshot()["wall_s"]["remaining"] == 0
    missing = SessionFacts(started=0, wall_limit_s=120, clock=lambda: 0,
                           s1=object(), s1_call_cap=None, help=object()).snapshot()
    assert missing["s1_calls"] == {"reserved": None, "cap": None, "remaining": None, "state": "unknown"}
    assert missing["frontier_calls"]["remaining"] is None
    disabled = SessionFacts(started=0, wall_limit_s=120, clock=lambda: 0,
                            s1=object(), s1_call_cap=None).snapshot()
    assert disabled["frontier_calls"] == {"reserved": 0, "cap": 0, "remaining": 0, "state": "not_configured"}


def test_resource_facts_preserve_inner_facts_and_unmeasured_counters():
    class Inner:
        async def facts(self, reports, observation):
            return ["An earlier measured outcome."]

    async def run():
        facts = SessionFacts(started=0, wall_limit_s=120, clock=lambda: 12,
                             s1=SimpleNamespace(calls_reserved=True), s1_call_cap=5,
                             help=SimpleNamespace(calls=-1, max_calls=5), inner=Inner())
        observation = Observation(session="test", seq=1, game_s=1, scene="s", frame=1, wall_utc=datetime.now(timezone.utc),
                                  clock_mode="realtime", playing=True, paused=False, time_scale=1.0)
        lines = await facts.facts([], observation)
        assert lines[0] == "An earlier measured outcome."
        resource = json.loads(lines[-1].removeprefix(PREFIX))
        assert resource["s1_calls"]["reserved"] is None and resource["s1_calls"]["remaining"] is None
        assert resource["frontier_calls"]["state"] == "unknown"

    asyncio.run(run())


def test_exhausted_help_is_visible_to_s1_and_never_suppresses_its_choice():
    class ExhaustedHelp:
        calls = max_calls = 15
        requests = 0

        async def run(self, request):
            self.requests += 1
            return HelpResult(source="frontier", result={"error": "approved frontier call cap reached"})

    class CountingS1(FakeS1):
        calls_reserved = 0

        async def decide(self, context):
            result = await super().decide(context)
            self.calls_reserved += 1
            return result

    async def run():
        clock = FakeTime()
        helper = ExhaustedHelp()
        s1 = CountingS1(pick("escalate", while_pending={"mode": "wait"}), "wait", "finish")
        game, client = await connected()
        try:
            result = await session(client, build_choices(two_actions()), s1=s1, objective=OBJECTIVE,
                                   help=helper, s1_call_cap=10, clock=clock.clock)
            assert result.ending == "finish" and result.turns == 3 and result.adherence == []
            assert helper.requests == 1  # only S1's selected escalation, not a resource-triggered request
            assert game.applied() == []
            for index, context in enumerate(s1.contexts):
                resources = json.loads(next(line for line in context["facts"] if line.startswith(PREFIX))[len(PREFIX):])
                assert resources["s1_calls"]["remaining"] == 10 - index
                assert resources["frontier_calls"]["remaining"] == 0
                assert {"escalate", "wait", "finish"} <= {choice["id"] for choice in context["choices"]}
        finally:
            await closed(game, client)

    asyncio.run(run())
