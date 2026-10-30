"""The System One adapter: the proven request shape, one follow-up on help turns, and no policy of its own."""

from __future__ import annotations

import asyncio
import json
import math
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
from playtest.runner.s1 import ARM as DEFAULT_ARM, FOLLOW_UP_INSTRUCTIONS, SystemOneS1

ARM = "mammoth-1"  # these tests exercise the Baseten path; the default arm is checked separately

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


class Meter:
    """A token meter that keeps what the adapter reported: (tokens, estimated)."""

    def __init__(self) -> None:
        self.records: list[tuple[int, bool]] = []

    def record(self, tokens: int, *, estimated: bool = False) -> None:
        self.records.append((tokens, estimated))


def adapter(tmp_path: Path, *replies: Any, max_calls: int = 10, limit: int = 12,
            budget: Meter | None = None) -> tuple[SystemOneS1, Send, call_budget.Ledger]:
    ledger = call_budget.Ledger(tmp_path, approval(limit), ARM)
    send = Send(tmp_path, *replies)
    return SystemOneS1(ledger, max_calls=max_calls, send=send, arm=ARM, budget=budget), send, ledger


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


def test_the_liquid_d1_ultra_arm_sends_the_same_shape_to_liquid_and_bills_there(tmp_path: Path):
    arm = "liquid-d1-ultra"
    limits = approval()
    limits["arms"] = {arm: limits["arms"][ARM]}
    ledger = call_budget.Ledger(tmp_path, limits, arm)
    send = Send(tmp_path, reply("next_action", "c1"))
    port = SystemOneS1(ledger, max_calls=10, send=send, arm=arm)
    got = decide(port, context())
    assert send.bodies[0]["model"] == "d1-ultra" and set(send.bodies[0]) == {"model", "questions", "state"}
    assert (port.provider, port.model) == ("liquid", "d1-ultra") and got.calls[0].provider == "liquid"
    assert ledger.arm_rows()[0]["arm"] == arm
    assert replay.ARMS[arm][1:] == replay.ARMS["liquid-d1.1"][1:]   # one Liquid key and decisions endpoint
    assert {replay.provider_of(a) for a in ("liquid-d1.1", arm)} == {"liquid"}
    assert replay.provider_of("mammoth-1") == "baseten" and replay.provider_of("muse-spark-1.3") == "meta"
    # Since #100 an adapter built without an arm runs on d1-ultra.
    assert DEFAULT_ARM == arm and SystemOneS1(ledger, max_calls=1).model == "d1-ultra"


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


# --- tokens reported for pacing ---------------------------------------------------------------


def estimate_for(body: dict[str, Any]) -> int:
    return math.ceil(len(canonical(body)) / 2.5)


def test_input_tokens_alone_are_reported_when_the_provider_gave_no_output_count(tmp_path: Path):
    meter = Meter()
    port, _, _ = adapter(tmp_path, reply("next_action", "w", usage={"input_tokens": 2214}), budget=meter)
    decide(port, context())
    assert meter.records == [(2214, False)]


def test_input_and_output_tokens_are_both_counted(tmp_path: Path):
    meter = Meter()
    port, _, _ = adapter(tmp_path, reply("next_action", "w", usage={"input_tokens": 2214, "output_tokens": 61}),
                         budget=meter)
    decide(port, context())
    assert meter.records == [(2275, False)]


def test_missing_usage_is_estimated_from_the_request_and_marked_as_an_estimate(tmp_path: Path):
    meter = Meter()
    unusable = [{"tokens": 5}, {"input_tokens": "2214"}, {"input_tokens": -3}, {"input_tokens": True},
                {"output_tokens": 61}, "usage", None]
    replies = [reply("next_action", "w", usage=value if value is not None else {}) for value in unusable]
    port, send, _ = adapter(tmp_path, *replies, budget=meter)
    for _ in unusable:
        decide(port, context())
    # None of those carries an input count, so each is the length of its own request over 2.5, rounded up.
    assert meter.records == [(estimate_for(body), True) for body in send.bodies]
    assert len(meter.records) == len(unusable)


def test_a_reply_without_a_usage_key_at_all_is_estimated_too(tmp_path: Path):
    meter = Meter()
    bare = (200, {"answers": {"next_action": {"type": "choice", "choice": "w"}}}, 9.0, None)
    port, send, _ = adapter(tmp_path, bare, budget=meter)
    decide(port, context())
    assert meter.records == [(estimate_for(send.bodies[0]), True)]


def test_a_rate_limited_call_is_a_provider_failure_counted_as_an_estimate_and_never_retried(tmp_path: Path):
    meter = Meter()
    port, send, _ = adapter(tmp_path, (429, None, 5.0, "HTTP error"), reply("next_action", "c1"), budget=meter)
    got = decide(port, context())
    assert got.answer is None and got.failure == "HTTP error (HTTP 429)" and got.halt is None
    assert len(send.bodies) == 1 and got.calls[0].http_status == 429          # one request: nothing was retried
    assert meter.records == [(estimate_for(send.bodies[0]), True)]            # it still spent a request's tokens


def test_every_call_of_a_help_turn_is_reported_and_a_429_on_the_follow_up_leaves_while_pending_absent(tmp_path: Path):
    meter = Meter()
    port, send, _ = adapter(tmp_path, reply("next_action", "h", usage={"input_tokens": 2200, "output_tokens": 40}),
                            (429, None, 5.0, "HTTP error"), budget=meter)
    got = decide(port, context())
    assert got.answer["choice_id"] == "h" and "while_pending" not in got.answer and got.failure is None
    assert [c.question for c in got.calls] == ["next_action", "while_pending"] and got.calls[1].http_status == 429
    assert len(send.bodies) == 2
    assert meter.records == [(2240, False), (estimate_for(send.bodies[1]), True)]


def test_an_adapter_exception_is_counted_as_an_estimate_because_the_request_may_have_been_sent(tmp_path: Path):
    def boom(body):
        raise RuntimeError("socket exploded")

    meter = Meter()
    port, send, _ = adapter(tmp_path, boom, budget=meter)
    got = decide(port, context())
    assert got.answer is None and got.failure == "adapter error: RuntimeError"
    assert meter.records == [(estimate_for(send.bodies[0]), True)]


def test_a_call_that_was_never_sent_reports_nothing(tmp_path: Path):
    capped, enveloped = Meter(), Meter()
    port, send, _ = adapter(tmp_path / "cap", reply("next_action", "c1"), max_calls=1, budget=capped)
    decide(port, context())
    assert decide(port, context()).halt and len(send.bodies) == 1             # the cap was reached: nothing was sent
    assert len(capped.records) == 1
    port2, send2, _ = adapter(tmp_path / "envelope", reply("next_action", "w"), max_calls=5, limit=1, budget=enveloped)
    decide(port2, context())
    assert decide(port2, context()).halt and len(send2.bodies) == 1           # the approved envelope is spent
    assert len(enveloped.records) == 1


def test_without_a_meter_the_adapter_behaves_as_before(tmp_path: Path):
    port, send, _ = adapter(tmp_path, reply("next_action", "c1"))
    assert decide(port, context()).answer["choice_id"] == "c1" and len(send.bodies) == 1


def test_pacing_changes_nothing_the_adapter_returns(tmp_path: Path):
    answers = []
    for budget in (None, Meter()):
        port, send, _ = adapter(tmp_path / str(budget is None), reply("next_action", "h", favoured="c2"),
                                reply("while_pending", "c1"), budget=budget)
        got = decide(port, context())
        answers.append((got.answer, [c.body() for c in got.calls], got.failure, got.halt))
    assert answers[0] == answers[1]


def test_no_credential_reaches_a_record(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-secret-value-1234")
    port, _, ledger = adapter(tmp_path, reply("next_action", "h"), reply("while_pending", "w"))
    got = decide(port, context())
    text = json.dumps([call.body() for call in got.calls]) + json.dumps(ledger.arm_rows())
    assert "sk-test-secret-value-1234" not in text and "Authorization" not in text

@pytest.mark.parametrize("arm,transport,extra", [("mammoth-1", "messages", 1000),
                                                  ("elephant-1", "messages", 1000),
                                                  ("liquid-d1.1", "images", 1400)])
def test_game_view_uses_proven_transport_and_counts_image_tokens(tmp_path, arm, transport, extra):
    import base64
    from playtest.runner.pacing import INITIAL_ESTIMATE, TokenBudget

    raw = b"sample image bytes"
    ctx = context()
    ctx.update(game_view="the attached view", game_view_sha256=digest(raw), game_view_status="sent")
    limits = approval()
    limits["arms"] = {arm: limits["arms"][ARM]}
    ledger = call_budget.Ledger(tmp_path, limits, arm)
    send = Send(tmp_path, reply("next_action", "h"), reply("while_pending", "w"))
    budget = TokenBudget(100000, initial_estimate=INITIAL_ESTIMATE + extra)
    port = SystemOneS1(ledger, max_calls=10, send=send, arm=arm, budget=budget, game_view=True)
    assert budget.expected == INITIAL_ESTIMATE + extra
    got = asyncio.run(port.decide(ctx, image=raw))
    assert got.answer["choice_id"] == "h" and got.answer["while_pending"] == {"mode": "wait"}
    url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    for body in send.bodies:
        if transport == "messages":
            parts = body["state"]["messages"][0]["content"]
            assert json.loads(parts[0]["text"]) == ctx and parts[1]["image_url"]["url"] == url
        else:
            assert body["state"] == ctx and body["images"] == [url]
    assert port.view_counts == {"enabled": True, "sent": 2, "omitted": 0, "stale": 0}
    # The estimate is based on the text body, plus image tokens, never base64 character count.
    last = send.bodies[-1]
    plain = {"model": last["model"], "questions": last["questions"], "state": ctx}
    assert budget.expected == math.ceil(len(canonical(plain)) / 2.5) + extra
    assert budget.in_window > 2 * extra and budget.estimated == 2


def test_view_omission_counts_and_forecasts_next_image_without_changing_choice(tmp_path):
    from playtest.runner.pacing import TokenBudget
    ctx = context()
    ctx.update(game_view="too old", game_view_sha256=None, game_view_status="stale")
    ledger = call_budget.Ledger(tmp_path, approval(), ARM)
    send = Send(tmp_path, reply("next_action", "c1", usage={"input_tokens": 123, "output_tokens": 4}))
    budget = TokenBudget(100000)
    port = SystemOneS1(ledger, max_calls=2, send=send, arm=ARM, budget=budget, game_view=True)
    result = asyncio.run(port.decide(ctx))
    assert result.answer["choice_id"] == "c1" and send.bodies[0]["state"] == ctx
    assert port.view_counts == {"enabled": True, "sent": 0, "omitted": 1, "stale": 1}
    assert budget.in_window == 127 and budget.expected == 1127 and budget.estimated == 0
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(port.decide(ctx, image=b"wrong"))
    assert len(send.bodies) == 1


def test_unverified_arm_is_refused_before_any_call(tmp_path, monkeypatch):
    from playtest.runner import s1 as s1_module
    monkeypatch.delitem(s1_module.IMAGE_TRANSPORT, "liquid-d1-ultra")   # every real arm is verified; drop one
    ledger = call_budget.Ledger(tmp_path, approval(), ARM)
    with pytest.raises(ValueError, match="no verified image transport"):
        SystemOneS1(ledger, max_calls=1, arm="liquid-d1-ultra", game_view=True)
    assert not list((tmp_path / "calls").glob("*.json"))


def test_image_echo_is_removed_from_recorded_response(tmp_path):
    import base64
    raw = b"private-image"
    encoded = base64.b64encode(raw).decode()
    ctx = context()
    ctx.update(game_view="the attached view", game_view_sha256=digest(raw), game_view_status="sent")
    def echoes(body):
        answer = reply("next_action", "c1")(body)
        answer[1]["echo"] = "data:image/jpeg;base64," + encoded
        answer[1]["usage"]["echo"] = encoded
        return answer
    ledger = call_budget.Ledger(tmp_path, approval(), ARM)
    send = Send(tmp_path, echoes)
    port = SystemOneS1(ledger, max_calls=1, send=send, arm=ARM, game_view=True)
    got = asyncio.run(port.decide(ctx, image=raw))
    assert encoded not in json.dumps(got.calls[0].body())
    assert encoded not in json.dumps(ledger.arm_rows())
