import asyncio
import json

import pytest

from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.runner.s1 import SystemOneS1
from s1_fakes import Send, approval
from test_runner_s1 import context, Meter

ARM = "meta-muse-spark-s1"


def response(choice, status=200, finish="stop"):
    return status, {"choices": [{"finish_reason": finish, "message": {"content": json.dumps({"choice": choice})}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                              "prompt_tokens_details": {"cached_tokens": 20}}}, 1, None


def setup(tmp_path, *responses, view=False):
    limits = approval()
    limits["arms"][ARM] = limits["arms"].pop("mammoth-1")
    limits["arms"][ARM]["per_call_max_usd"] = 0.001
    replay.validate_approval(limits, [ARM])
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    send = Send(tmp_path, *responses)
    meter = Meter()
    return SystemOneS1(ledger, max_calls=10, send=send, arm=ARM, budget=meter, game_view=view), send, ledger, meter


def test_request_and_cost(tmp_path):
    port, send, ledger, meter = setup(tmp_path, response("c1"))
    ctx = context()
    answer = asyncio.run(port.decide(ctx))
    body = send.bodies[0]
    assert body["response_format"]["json_schema"]["schema"]["properties"]["choice"]["enum"] == [c["id"] for c in ctx["choices"]]
    assert json.loads(body["messages"][1]["content"]) == {"state": ctx, "criteria": {c["id"]: c["label"] for c in ctx["choices"]}}
    assert answer.answer["choice_id"] == "c1" and "distribution" not in answer.answer
    assert ledger.arm_rows()[0]["cost_usd"] == pytest.approx((80 * 1.25 + 20 * .15 + 10 * 4.25) / 1e6)
    assert meter.records == [(110, False)]


@pytest.mark.parametrize("reply", [response("bad"), response("c1", 402), response("c1", finish="length"),
                                   (200, {"choices": [{"finish_reason": "stop", "message": {"content": "bad"}}]}, 1, None)])
def test_invalid_fails_closed(tmp_path, reply):
    port, send, _, _ = setup(tmp_path, reply)
    answer = asyncio.run(port.decide(context()))
    assert answer.answer is None and answer.failure and len(send.bodies) == 1


def test_image_and_pending_subset(tmp_path):
    ctx = context()
    help_choice = next(c for c in ctx["choices"] if c["kind"] in ("escalate", "delegate"))
    pending = [c for c in ctx["choices"] if c["kind"] in ("wait", "action", "skill")]
    image = b"jpeg"
    ctx.update(game_view_sha256=digest(image), game_view_status="sent")
    port, send, _, _ = setup(tmp_path, response(help_choice["id"]), response(pending[0]["id"]), view=True)
    answer = asyncio.run(port.decide(ctx, image=image))
    assert answer.answer["choice_id"] == help_choice["id"] and "while_pending" in answer.answer
    assert send.bodies[0]["messages"][1]["content"][1]["type"] == "image_url"
    assert send.bodies[1]["response_format"]["json_schema"]["schema"]["properties"]["choice"]["enum"] == [c["id"] for c in pending]


def test_non_full_layout_rejected(tmp_path):
    limits = approval()
    limits["arms"][ARM] = limits["arms"].pop("mammoth-1")
    with pytest.raises(ValueError, match="layout"):
        SystemOneS1(call_budget.Ledger(tmp_path, limits, ARM), max_calls=1, arm=ARM, layout="lossless")
