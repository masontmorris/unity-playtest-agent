"""The System One adapter: the proven request shape, one follow-up on help turns, and no policy of its own."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from loop_fakes import choices
from s1_fakes import Send, approval, reply
from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceSet
from playtest.core.context import INSTRUCTIONS, build_context, canonical
from playtest.runner.s1 import ARM, FOLLOW_UP_INSTRUCTIONS, SystemOneS1

MODEL = replay.ARMS[ARM][0]


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    """No test sees, scans for or can print a credential from the machine's own environment."""
    for name in [n for n in os.environ if n.endswith("_API_KEY")]:
        monkeypatch.delenv(name)


def context(version: int = 4) -> dict[str, Any]:
    obs = Observation(session="s", seq=7, game_s=3.0, wall_utc=datetime(2026, 10, 28, tzinfo=timezone.utc),
                      clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False, time_scale=1.0, frame=9)
    return build_context(objective="Exercise the controls.", observation=obs,
                         choices=ChoiceSet(set_version=version, built_from_seq=7, choices=choices()))


def adapter(tmp_path: Path, *replies: Any, max_calls: int = 10, limit: int = 12) -> tuple[SystemOneS1, Send, call_budget.Ledger]:
    ledger = call_budget.Ledger(tmp_path, approval(limit), ARM)
    send = Send(tmp_path, *replies)
    return SystemOneS1(ledger, max_calls=max_calls, send=send), send, ledger


def decide(port: SystemOneS1, ctx: dict[str, Any]):
    return asyncio.run(port.decide(ctx))


def test_an_ordinary_turn_makes_one_call_in_the_proven_shape(tmp_path: Path):
    port, send, ledger = adapter(tmp_path, reply("next_action", "c1", favoured="c2"))
    ctx = context()
    got = decide(port, ctx)
    body = send.bodies[0]
    assert set(body) == {"model", "questions", "state"} and body["model"] == MODEL and body["state"] == ctx
    assert body["questions"] == {"next_action": {
        "type": "choice", "instructions": INSTRUCTIONS,
        "criteria": {c["id"]: c["label"] for c in ctx["choices"]}}}
    # The answer is the choice the provider named; its distribution is recorded, never used to select.
    assert got.answer["choice_id"] == "c1" and got.answer["set_version"] == 4 and "while_pending" not in got.answer
    assert got.answer["distribution"]["c2"] == 0.9 and got.failure is None and got.halt is None
    assert len(send.bodies) == 1 and len(got.calls) == 1
    call = got.calls[0]
    assert (call.question, call.provider, call.model, call.resolved_model) == (
        "next_action", "baseten", MODEL, "mammoth-1-resolved")
    assert call.request_sha256 == digest(canonical(body)) and call.http_status == 200 and call.problem is None
    assert call.latency_ms == 12.0 and call.usage == {"tokens": 5} and call.response["answers"]["next_action"]
    assert ledger.arm_rows()[0]["question"] == "next_action"


def test_a_wait_finish_or_action_answer_never_triggers_a_follow_up(tmp_path: Path):
    port, send, _ = adapter(tmp_path, reply("next_action", "w"), reply("next_action", "f"), reply("next_action", "c3"))
    for _ in range(3):
        decide(port, context())
    assert len(send.bodies) == 3


def test_an_escalation_makes_one_follow_up_and_a_continue_maps_to_that_choice(tmp_path: Path):
    port, send, ledger = adapter(tmp_path, reply("next_action", "h"), reply("while_pending", "c2"))
    ctx = context()
    got = decide(port, ctx)
    assert len(send.bodies) == 2
    follow = send.bodies[1]
    assert set(follow) == {"model", "questions", "state"} and follow["state"] == ctx
    assert list(follow["questions"]) == ["while_pending"]
    question = follow["questions"]["while_pending"]
    assert question["type"] == "choice" and question["instructions"] == FOLLOW_UP_INSTRUCTIONS
    # Criteria are the wait choice and the executable choices; help and finish are not continuations.
    assert list(question["criteria"]) == ["c1", "c2", "c3", "w"]
    assert got.answer["choice_id"] == "h" and got.answer["while_pending"] == {"mode": "continue", "choice_id": "c2"}
    assert [c.question for c in got.calls] == ["next_action", "while_pending"]
    assert all(c.problem is None for c in got.calls)
    assert sorted(row["question"] for row in ledger.arm_rows()) == ["next_action", "while_pending"]


def test_a_wait_follow_up_maps_to_wait_and_a_delegation_asks_too(tmp_path: Path):
    port, send, _ = adapter(tmp_path, reply("next_action", "d"), reply("while_pending", "w"))
    got = decide(port, context())
    assert got.answer["choice_id"] == "d" and got.answer["while_pending"] == {"mode": "wait"}
    assert len(send.bodies) == 2


def test_a_failed_or_invalid_follow_up_leaves_while_pending_absent(tmp_path: Path):
    unoffered = (200, {"answers": {"while_pending": {"type": "choice", "choice": "zz"}}}, 9.0, None)
    for bad in ((500, None, 5.0, "HTTP error"), (None, None, 5.0, "transport error"), unoffered,
                (200, {"answers": {}}, 5.0, None)):
        port, send, _ = adapter(tmp_path / f"d{abs(hash(str(bad)))}", reply("next_action", "h"), bad)
        got = decide(port, context())
        # The primary stands; the follow-up is recorded and is not a provider failure of the turn.
        assert got.answer["choice_id"] == "h" and "while_pending" not in got.answer and got.failure is None
        assert len(got.calls) == 2 and got.calls[1].question == "while_pending" and got.calls[1].problem


def test_an_http_failure_gives_no_answer_and_no_follow_up(tmp_path: Path):
    for status, problem, text in ((500, "HTTP error", "HTTP error (HTTP 500)"), (None, "transport error", "transport error")):
        port, send, _ = adapter(tmp_path / str(status), (status, None, 5.0, problem))
        got = decide(port, context())
        assert got.answer is None and got.failure == text and len(send.bodies) == 1
        assert got.calls[0].problem == problem and got.calls[0].http_status == status


def test_an_unoffered_or_unparseable_answer_is_a_provider_failure(tmp_path: Path):
    zz = (200, {"answers": {"next_action": {"type": "choice", "choice": "zz"}}}, 9.0, None)
    notjson = (200, None, 9.0, "response was not JSON")
    for index, bad in enumerate((zz, notjson)):
        port, _, _ = adapter(tmp_path / str(index), bad)
        got = decide(port, context())
        assert got.answer is None and got.failure and got.calls[0].problem


def test_a_call_is_reserved_before_it_is_sent_and_runs_off_the_event_loop_thread(tmp_path: Path):
    port, send, ledger = adapter(tmp_path, reply("next_action", "h"), reply("while_pending", "w"))
    decide(port, context())
    assert send.reserved == [["in_flight"], ["finished", "in_flight"]]
    assert threading.get_ident() not in send.threads and len(set(send.threads)) >= 1
    assert [row["status"] for row in ledger.arm_rows()] == ["finished", "finished"]


def test_an_adapter_exception_is_a_failure_with_the_reservation_kept_and_no_retry(tmp_path: Path):
    def boom(body):
        raise RuntimeError("socket exploded")

    port, send, ledger = adapter(tmp_path, boom, reply("next_action", "c1"))
    got = decide(port, context())
    assert got.answer is None and got.failure == "adapter error: RuntimeError" and len(send.bodies) == 1
    assert ledger.arm_rows()[0]["problem"] == "adapter error: RuntimeError"
    assert ledger.arm_rows()[0]["status"] == "finished"


def test_the_call_cap_and_the_approved_envelope_stop_calls_before_they_are_sent(tmp_path: Path):
    port, send, _ = adapter(tmp_path / "cap", reply("next_action", "h"), max_calls=1)
    got = decide(port, context())
    # The primary used the only call; the follow-up is refused, recorded, and asks the harness to stop.
    assert got.answer["choice_id"] == "h" and "while_pending" not in got.answer
    assert got.halt == "the run's call cap was reached" and len(send.bodies) == 1
    assert got.calls[1].problem == "the run's call cap was reached; no call was made"
    after = decide(port, context())
    assert after.answer is None and after.halt and len(send.bodies) == 1

    port2, send2, _ = adapter(tmp_path / "envelope", reply("next_action", "w"), max_calls=5, limit=1)
    assert decide(port2, context()).answer["choice_id"] == "w"
    refused = decide(port2, context())
    assert refused.answer is None and refused.halt and "envelope" in refused.halt and len(send2.bodies) == 1


def test_a_provider_cost_over_the_bound_halts_the_run(tmp_path: Path):
    costly = (200, {"answers": {"next_action": {"type": "choice", "choice": "w"}}, "usage": {"cost": 5.0}}, 9.0, None)
    port, _, _ = adapter(tmp_path, costly)
    got = decide(port, context())
    assert got.answer["choice_id"] == "w" and got.halt == "a provider-reported cost exceeded the per-call bound"


def test_provider_model_and_prompt_hash_are_exposed_for_the_run_record(tmp_path: Path):
    port, _, _ = adapter(tmp_path)
    assert (port.provider, port.model) == ("baseten", MODEL)
    assert port.prompt_sha256 == digest(canonical({"next_action": INSTRUCTIONS, "while_pending": FOLLOW_UP_INSTRUCTIONS}))
    assert len(port.prompt_sha256) == 64


def test_no_credential_reaches_a_record(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-secret-value-1234")
    port, _, ledger = adapter(tmp_path, reply("next_action", "h"), reply("while_pending", "w"))
    got = decide(port, context())
    text = json.dumps([call.body() for call in got.calls]) + json.dumps(ledger.arm_rows())
    assert "sk-test-secret-value-1234" not in text and "Authorization" not in text
