"""The `d1-2b-local` S1 arm: the loopback shim in scripts/s1/d1_local_server.py and the arm's wiring.

Offline and GPU-free. The shim runs with its stand-in model (`--fake`) or an injected one, and the real-load
path runs against a stub decision-lab package. Nothing here loads d1-2B, imports torch or touches CUDA.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from catalog_game import CatalogGame
from loop_fakes import GameThread, choices
from s1_fakes import Send, reply
from test_runner_session import Project, two_actions, with_fake_send

from playtest.bench import call_budget, replay
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, ModelRef, RunRecord
from playtest.contracts.loop import ChoiceSet
from playtest.contracts.trace import TraceKind
from playtest.core.context import build_context
from playtest.core.trace import read_trace
from playtest.preflight.catalog import export
from playtest.runner import approval as approval_cli
from playtest.runner import cli
from playtest.runner.image_transport import IMAGE_TRANSPORT, attach_images
from playtest.runner.output import secret_values
from playtest.runner.pacing import default_tokens_per_minute
from playtest.runner.s1 import SystemOneS1

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "s1" / "d1_local_server.py"
ARM = "d1-2b-local"
TOKEN = "local-test-token-0000"
JPEG = b"\xff\xd8\xff\xe0jpeg-bytes"
JPEG_URL = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode("ascii")


@pytest.fixture(autouse=True)
def no_ambient_local_arm(monkeypatch):
    """A shim or token left in the machine's environment must not reach a test."""
    for name in ("D1_LOCAL_TOKEN", "D1_LOCAL_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("UPA_S1_KEEPALIVE", raising=False)


def body(**extra: Any) -> dict[str, Any]:
    questions = {"next_action": {"type": "choice", "instructions": "Pick one.",
                                 "criteria": {"c1": "First", "c2": "Second"}}}
    return {"model": "d1-2b", "questions": questions, "state": {"zeta": 1, "alpha": {"y": 2}}, **extra}


def answered(choice: str = "c1", probabilities: dict[str, float] | None = None,
             usage: dict[str, Any] | None = None) -> dict[str, Any]:
    probabilities = {"c1": 0.7, "c2": 0.3} if probabilities is None else probabilities
    found = {"type": "choice", "choice": choice, "confidence": 0.7, "probabilities": probabilities}
    return {"answers": {"next_action": found}, **({"usage": usage} if usage is not None else {})}


# --- the shim as a process ---------------------------------------------------------------------------------


class Shim:
    def __init__(self, process: subprocess.Popen, ready: dict[str, Any], log: Path, token: str) -> None:
        self.process, self.ready, self.log, self.token = process, ready, log, token
        self.url = f"http://127.0.0.1:{ready['port']}"

    def use(self, monkeypatch) -> None:
        """Point the arm at this shim, the way an operator's environment does."""
        monkeypatch.setenv("D1_LOCAL_URL", self.url)
        monkeypatch.setenv("D1_LOCAL_TOKEN", self.token)

    def stderr(self) -> str:
        return self.log.read_text(encoding="utf-8")


@contextlib.contextmanager
def running_shim(tmp_path: Path, *args: str, token: str = TOKEN, env: dict[str, str] | None = None):
    log = tmp_path / "shim.stderr"
    with log.open("wb") as stderr:
        process = subprocess.Popen([sys.executable, str(SERVER), *args], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=stderr,
                                   env={**os.environ, "D1_LOCAL_TOKEN": token, **(env or {})})
    lines: list[bytes] = []
    reader = threading.Thread(target=lambda: lines.append(process.stdout.readline()), daemon=True)
    reader.start()
    reader.join(30)
    try:
        if not lines or not lines[0]:
            raise AssertionError(f"the shim printed no ready line: {log.read_text(encoding='utf-8')}")
        yield Shim(process, json.loads(lines[0]), log, token)
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
        process.stdout.close()
        with replay._pool_lock:
            replay._connection_pool.clear()


def test_send_systemone_reaches_the_shim_and_the_reply_parses(tmp_path: Path, monkeypatch):
    with running_shim(tmp_path, "--fake") as shim:
        assert shim.ready["ready"] is True and shim.ready["fake"] is True and shim.ready["port"] > 0
        shim.use(monkeypatch)
        for _ in range(2):  # the second call rides the pooled keep-alive connection
            status, data, latency_ms, problem = replay.send_systemone(ARM, body())
            assert (status, problem) == (200, None) and latency_ms >= 0
            assert replay.parse_answer(data, {"c1", "c2"}) == ("c1", {"c1": 0.5, "c2": 0.5}, None)
        assert data["model"] == "d1-2b-fake"
        assert data["usage"]["cost"] == 0.0 and data["usage"]["inference_ms"] >= 0
        assert isinstance(data["usage"]["input_tokens"], int) and data["usage"]["output_tokens"] == 0


def test_a_missing_or_wrong_token_is_a_401_and_no_token_is_ever_logged(tmp_path: Path, monkeypatch):
    other = "wrong-test-token-0000"
    with running_shim(tmp_path, "--fake") as shim:
        shim.use(monkeypatch)
        payload = json.dumps(body()).encode("utf-8")
        for headers in ({}, {"Authorization": "Bearer"}, {"Authorization": f"Basic {TOKEN}"}):
            request = urllib.request.Request(shim.url + "/v1/systemone", data=payload, method="POST",
                                             headers={"Content-Type": "application/json", **headers})
            with pytest.raises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(request, timeout=10)
            assert refused.value.code == 401 and refused.value.headers["WWW-Authenticate"] == "Bearer"
        monkeypatch.setenv("D1_LOCAL_TOKEN", other)
        status, data, _, problem = replay.send_systemone(ARM, body())
        assert (status, data, problem) == (401, None, "HTTP error")
        monkeypatch.setenv("D1_LOCAL_TOKEN", TOKEN)
        assert replay.send_systemone(ARM, body())[0] == 200
        health = urllib.request.Request(shim.url + "/health", headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(health, timeout=10) as response:
            assert json.loads(response.read()) == {"status": "ok", "model": "d1-2b-fake"}
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(shim.url + "/health", timeout=10)
        assert refused.value.code == 401
    log = shim.stderr()
    assert "401" in log and TOKEN not in log and other not in log


@pytest.mark.parametrize("token", ["", "abc123"])
def test_the_shim_will_not_start_without_a_token_of_eight_characters(token):
    done = subprocess.run([sys.executable, str(SERVER), "--fake"], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "D1_LOCAL_TOKEN": token})
    assert done.returncode == 2 and "D1_LOCAL_TOKEN" in done.stderr and not done.stdout
    assert not token or token not in done.stderr


def test_help_needs_nothing_beyond_the_standard_library():
    done = subprocess.run([sys.executable, "-S", str(SERVER), "--help"], capture_output=True, text=True, timeout=60,
                          env={key: value for key, value in os.environ.items() if key != "D1_LOCAL_TOKEN"})
    assert done.returncode == 0 and "--fake" in done.stdout and "--decision-lab" in done.stdout


def test_the_real_path_loads_through_decision_labs_load_d1_over_the_pinned_snapshot(tmp_path: Path):
    revision = "a" * 40
    lab = tmp_path / "lab"
    (lab / "decision_lab").mkdir(parents=True)
    (lab / "decision_lab" / "__init__.py").write_text("", encoding="utf-8")
    (lab / "decision_lab" / "modal_config.py").write_text(f'D1_REVISION = "{revision}"\n', encoding="utf-8")
    (lab / "decision_lab" / "d1.py").write_text(
        "class Model:\n"
        "    def system_one(self, state, questions, media=None):\n"
        "        return {'answers': {n: {'type': 'choice', 'choice': 'c2', 'probabilities': {'c1': 0.25, 'c2': 0.75}}\n"
        "                            for n in questions}, 'usage': {'input_tokens': 9, 'output_tokens': 0}}\n"
        "def load_d1(snapshot_dir, kernels='auto'):\n"
        "    print('loader chatter that must not precede the ready line')\n"
        "    return Model(), {'attention': kernels, 'conv': snapshot_dir.name}\n", encoding="utf-8")
    hub = tmp_path / "hub"
    snapshot = hub / "models--LiquidAI--d1-2B" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    environment = {"HF_HUB_CACHE": str(hub)}
    with running_shim(tmp_path, "--decision-lab", str(lab), "--kernels", "portable", env=environment) as shim:
        assert shim.ready == {"ready": True, "port": shim.ready["port"], "model": "d1-2b", "revision": revision,
                              "kernels": {"attention": "portable", "conv": revision}, "fake": False}
        request = urllib.request.Request(shim.url + "/v1/systemone", data=json.dumps(body()).encode("utf-8"),
                                         method="POST", headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
        assert data["answers"]["next_action"]["choice"] == "c2"
        assert (data["model"], data["revision"], data["kernels"]["attention"]) == ("d1-2b", revision, "portable")
    missing = subprocess.run([sys.executable, str(SERVER), "--decision-lab", str(lab)], capture_output=True, text=True,
                             timeout=60, env={**os.environ, "D1_LOCAL_TOKEN": TOKEN, "HF_HUB_CACHE": str(tmp_path / "none")})
    assert missing.returncode != 0 and "--snapshot" in missing.stderr and not missing.stdout


# --- the shim in process, over a model that records what it was asked ------------------------------------


@pytest.fixture
def module(monkeypatch):
    spec = importlib.util.spec_from_file_location("d1_local_server_under_test", SERVER)
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)
    return loaded


class Recorder:
    def __init__(self, result: Any) -> None:
        self.result, self.calls = result, []

    def system_one(self, state, questions, media=None):
        self.calls.append((state, questions, media))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def post(self, payload: Any, *, token: str = TOKEN, raw: bytes | None = None) -> tuple[int, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = json.dumps(payload).encode("utf-8") if raw is None else raw
            connection.request("POST", "/v1/systemone", body=data, headers={"Authorization": f"Bearer {token}"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


@contextlib.contextmanager
def serving(module, model, decode=lambda raw: ("picture", raw)):
    server = module.make_server(module.Served(model, TOKEN, decode))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def test_an_answer_the_model_got_wrong_passes_through_and_the_runners_parser_rejects_it(module):
    cases = [
        (answered("not-offered", {"c1": 0.5, "c2": 0.5}), (None, None, "invalid or unoffered choice")),
        (answered("c2", {"c1": 0.9, "c2": 0.1}), ("c2", {"c1": 0.9, "c2": 0.1}, None)),   # never re-ranked
        (answered("c1", {"c1": 0.9, "c2": 0.9}), ("c1", None, "invalid distribution")),
    ]
    for result, parsed in cases:
        with serving(module, Recorder(result)) as client:
            status, data = client.post(body())
        assert status == 200 and data["answers"] == result["answers"]
        assert replay.parse_answer(data, {"c1", "c2"}) == parsed


def test_usage_adds_the_measured_forward_pass_and_zero_cost_and_invents_nothing_else(module):
    with serving(module, Recorder(answered(usage={"input_tokens": 7}))) as client:
        _, data = client.post(body())
    assert set(data["usage"]) == {"input_tokens", "cost", "inference_ms"}      # no output_tokens made up
    assert data["usage"]["input_tokens"] == 7 and data["usage"]["cost"] == 0.0 and data["usage"]["inference_ms"] >= 0
    with serving(module, Recorder(answered())) as client:
        _, data = client.post(body())
    assert set(data["usage"]) == {"cost", "inference_ms"} and data["model"] == "d1-2b"
    assert "revision" not in data and "kernels" not in data


def test_images_reach_the_model_as_decoded_media_and_a_text_request_sends_none(module):
    recorder = Recorder(answered())
    with serving(module, recorder) as client:
        assert client.post(body())[0] == 200
        assert client.post(body(images=[JPEG_URL, JPEG_URL]))[0] == 200
    assert recorder.calls[0][2] is None
    state, questions, media = recorder.calls[1]
    assert state == body()["state"] and questions == body()["questions"] and media == [("picture", JPEG)] * 2


@pytest.mark.parametrize("images", [
    "x", ["https://example.com/a.jpg"], ["data:image/jpeg;base64,!!not-base64!!"], [JPEG_URL] * 5, [7], None])
def test_an_image_the_server_cannot_decode_is_refused_before_the_model(module, images):
    recorder = Recorder(answered())
    with serving(module, recorder) as client:
        status, data = client.post(body(images=images))
    assert status == 400 and data["error"]["type"] == "invalid_request" and recorder.calls == []


def test_a_picture_the_decoder_rejects_is_a_400(module):
    def decode(raw):
        raise OSError("cannot identify image file")

    with serving(module, Recorder(answered()), decode) as client:
        status, data = client.post(body(images=[JPEG_URL]))
    assert status == 400 and "image 1" in data["error"]["message"]


@pytest.mark.parametrize("payload, raw", [
    (None, b"not json"), ([1, 2], None), ({**body(), "media": []}, None), ({**body(), "model": "d1-ultra"}, None),
    ({**body(), "questions": {}}, None), ({"model": "d1-2b", "questions": body()["questions"]}, None)])
def test_a_request_the_model_should_never_see_is_a_400(module, payload, raw):
    recorder = Recorder(answered())
    with serving(module, recorder) as client:
        status, data = client.post(payload, raw=raw)
    assert status == 400 and data["error"]["type"] == "invalid_request" and recorder.calls == []


def test_a_refusing_model_is_a_400_a_crashing_one_a_500_and_the_server_keeps_serving(module):
    recorder = Recorder(ValueError("question 'next_action': choice with no options"))
    with serving(module, recorder) as client:
        status, data = client.post(body())
        assert status == 400 and "choice with no options" in data["error"]["message"]
        recorder.result = RuntimeError("CUDA out of memory")
        status, data = client.post(body())
        assert status == 500 and data["error"]["type"] == "server_error" and "CUDA out of memory" in data["error"]["message"]
        recorder.result = answered()
        assert client.post(body())[0] == 200


def test_a_non_finite_number_in_the_answer_is_not_sent(module):
    with serving(module, Recorder(answered(probabilities={"c1": float("nan"), "c2": 0.5}))) as client:
        status, data = client.post(body())
    assert status == 502 and data["error"]["type"] == "bad_model_output"


def test_numbers_from_a_tensor_library_are_sent_as_plain_json(module):
    class Scalar:
        def __init__(self, value):
            self.value = value

        def item(self):
            return self.value

    result = answered(probabilities={"c1": Scalar(0.25), "c2": Scalar(0.75)})
    with serving(module, Recorder(result)) as client:
        status, data = client.post(body())
    assert status == 200 and data["answers"]["next_action"]["probabilities"] == {"c1": 0.25, "c2": 0.75}


def test_requests_are_served_one_forward_pass_at_a_time(module):
    class Slow(Recorder):
        active = peak = 0

        def system_one(self, state, questions, media=None):
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
            time.sleep(0.05)
            type(self).active -= 1
            return super().system_one(state, questions, media)

    results: list[int] = []
    with serving(module, Slow(answered())) as client:
        threads = [threading.Thread(target=lambda: results.append(client.post(body())[0])) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    assert results == [200, 200, 200] and Slow.peak == 1


def test_a_token_shorter_than_the_redaction_minimum_cannot_start_a_server(module):
    with pytest.raises(ValueError, match="at least 8"):
        module.make_server(module.Served(Recorder(answered()), "short", lambda raw: raw))


# --- the arm's wiring --------------------------------------------------------------------------------------


def local_approval(calls: int = 12, overall: int | None = None) -> dict[str, Any]:
    limit = {"max_calls": calls, "per_call_max_usd": 0, "max_cost_usd": 0}
    return {"approved_by": "Test User", "approved_at_utc": "2026-01-01T00:00:00Z",
            "max_calls": calls if overall is None else overall, "max_cost_usd": 0, "arms": {ARM: limit}}


def context() -> dict[str, Any]:
    obs = Observation(session="s", seq=7, game_s=3.0, wall_utc=datetime(2026, 10, 28, tzinfo=timezone.utc),
                      clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False, time_scale=1.0, frame=9)
    return build_context(objective="Exercise the controls.", observation=obs,
                         choices=ChoiceSet(set_version=4, built_from_seq=7, choices=choices()))


def local_reply(question: str, choice: str, cost: float = 0.0):
    inner = reply(question, choice, usage={"input_tokens": 40, "output_tokens": 0, "cost": cost, "inference_ms": 31.5})

    def make(request: dict[str, Any]):
        status, data, latency, problem = inner(request)
        return status, {**data, "model": "d1-2b-fake"}, latency, problem
    return make


def test_the_arm_is_a_local_provider_serving_d1_2b_with_its_own_credentials(tmp_path: Path):
    assert replay.ARMS[ARM] == ("d1-2b", "D1_LOCAL_TOKEN", "D1_LOCAL_URL")
    assert replay.provider_of(ARM) == "local" and ARM in replay.LOCAL_ARMS
    assert replay.credential_variables(ARM) == ("D1_LOCAL_TOKEN", "D1_LOCAL_URL")
    port = SystemOneS1(call_budget.Ledger(tmp_path, local_approval(), ARM), max_calls=1, arm=ARM)
    assert (port.provider, port.model) == ("local", "d1-2b")
    assert replay.provider_of("liquid-d1-ultra") == "liquid" and ARM not in {"liquid-d1.1", "liquid-d1-ultra"}


def test_a_local_turn_reserves_nothing_and_records_a_zero_cost_under_the_local_provider(tmp_path: Path):
    ledger = call_budget.Ledger(tmp_path, local_approval(), ARM)
    send = Send(tmp_path, local_reply("next_action", "c1"))
    got = asyncio.run(SystemOneS1(ledger, max_calls=5, send=send, arm=ARM).decide(context()))
    assert got.answer["choice_id"] == "c1" and got.failure is None and got.halt is None
    call = got.calls[0]
    assert (call.provider, call.model, call.resolved_model) == ("local", "d1-2b", "d1-2b-fake")
    assert send.bodies[0]["model"] == "d1-2b" and send.reserved == [["in_flight"]]
    (row,) = ledger.arm_rows()
    assert (row["arm"], row["reserved_usd"], row["cost_usd"], row["cost_bound_exceeded"]) == (ARM, "0", 0.0, False)
    assert row["usage"] == {"input_tokens": 40, "output_tokens": 0, "cost": 0.0, "inference_ms": 31.5}


def test_a_local_arm_that_reports_any_cost_halts_the_run(tmp_path: Path):
    ledger = call_budget.Ledger(tmp_path, local_approval(), ARM)
    send = Send(tmp_path, local_reply("next_action", "c1", cost=0.001))
    got = asyncio.run(SystemOneS1(ledger, max_calls=5, send=send, arm=ARM).decide(context()))
    assert got.halt == "a provider-reported cost exceeded the per-call bound"
    assert ledger.arm_rows()[0]["cost_bound_exceeded"] is True


def test_the_approved_call_counts_still_bind_a_local_arm(tmp_path: Path):
    def reserve(ledger, turn):
        return ledger.reserve(episode=1, turn=turn, based_on_seq=0, request_sha256=f"{turn:064d}", requested_model="d1-2b")

    arm_capped = call_budget.Ledger(tmp_path / "arm", local_approval(2, overall=9), ARM)
    for turn in (1, 2):
        reserve(arm_capped, turn).finish(http_status=200, cost_usd=0.0)
    with pytest.raises(call_budget.BudgetStop, match="envelope"):
        reserve(arm_capped, 3)
    overall_capped = call_budget.Ledger(tmp_path / "overall", local_approval(9, overall=1), ARM)
    reserve(overall_capped, 1).finish(http_status=200, cost_usd=0.0)
    with pytest.raises(call_budget.BudgetStop, match="envelope"):
        reserve(overall_capped, 2)
    cli._fits(local_approval(5), 5, 0, arm=ARM)
    with pytest.raises(ValueError, match="smaller than the call cap"):
        cli._fits(local_approval(5), 6, 0, arm=ARM)


def test_a_local_approval_states_zero_bounds_and_needs_no_balance_or_price():
    replay.validate_approval(local_approval(), [ARM])
    for field, value in (("per_call_max_usd", 0.001), ("max_cost_usd", 1)):
        broken = local_approval()
        broken["arms"][ARM][field] = value
        with pytest.raises(ValueError, match="must be 0"):
            replay.validate_approval(broken, [ARM])
    for edit in ({"max_calls": 0}, {"max_calls": None}):
        broken = local_approval()
        broken["arms"][ARM].update(edit)
        with pytest.raises(ValueError, match="positive call limit"):
            replay.validate_approval(broken, [ARM])
    no_overall = local_approval()
    no_overall["max_calls"] = 0
    with pytest.raises(ValueError, match="overall call limit"):
        replay.validate_approval(no_overall, [ARM])


def test_a_zero_overall_cap_is_for_local_only_approvals_and_paid_arms_still_need_positive_bounds():
    mixed = local_approval()
    mixed["arms"]["muse-spark-1.3"] = {"max_calls": 2, "per_call_max_usd": 0.1, "max_cost_usd": 0.2,
                                       "verified_balance_usd": 5, "balance_checked_at_utc": "2026-01-01T00:00:00Z",
                                       "price_valid_until_utc": "2099-01-01T00:00:00Z"}
    with pytest.raises(ValueError, match="overall cost limit"):
        replay.validate_approval(mixed, [ARM, "muse-spark-1.3"])
    mixed["max_cost_usd"] = 0.5
    replay.validate_approval(mixed, [ARM, "muse-spark-1.3"])
    for paid in ("mammoth-1", "liquid-d1-ultra", "mammoth-1-modal"):
        with pytest.raises(ValueError, match="finite positive"):
            replay.arm_money(paid, 0, "bound")
    assert replay.arm_money(ARM, "0.0", "bound") == 0


def test_playtest_approval_writes_a_local_only_file_the_runner_accepts(tmp_path: Path, capsys):
    path = tmp_path / "approval.json"
    assert approval_cli.main(["--arm", f"{ARM}=40", "--approved-by", "user", "--output", str(path)]) == 0
    assert "local, $0/call x 40" in capsys.readouterr().out
    approval = json.loads(path.read_text(encoding="utf-8"))
    assert (approval["max_calls"], approval["max_cost_usd"]) == (40, 0)
    limit = approval["arms"][ARM]
    assert (limit["max_calls"], limit["per_call_max_usd"], limit["max_cost_usd"]) == (40, 0, 0)
    assert "verified_balance_usd" not in limit and "price_valid_until_utc" not in limit
    replay.validate_approval(approval, [ARM])
    cli._fits(approval, 40, 0, arm=ARM)
    with pytest.raises(ValueError, match="smaller than the call cap"):
        cli._fits(approval, 41, 0, arm=ARM)


@pytest.mark.parametrize("extra, message", [
    (["--bound", f"{ARM}=0.1"], "its bound is 0"),
    (["--ledger", f"{ARM}=somewhere"], "no ledger to measure"),
    (["--max-cost-usd", "-1"], "non-negative"),
])
def test_playtest_approval_refuses_money_for_a_local_arm(tmp_path: Path, capsys, extra, message):
    argv = ["--arm", f"{ARM}=4", "--approved-by", "user", "--output", str(tmp_path / "approval.json"), *extra]
    assert approval_cli.main(argv) == 1 and message in capsys.readouterr().err
    assert not (tmp_path / "approval.json").exists()


def test_playtest_approval_still_needs_a_cap_when_any_arm_is_billed(tmp_path: Path, capsys):
    (tmp_path / "ledger" / "calls").mkdir(parents=True)
    argv = ["--arm", f"{ARM}=4", "--arm", "mammoth-1=2", "--approved-by", "user", "--bound", "mammoth-1=0.01",
            "--ledger", "mammoth-1=" + str(tmp_path / "ledger"), "--output", str(tmp_path / "approval.json")]
    assert approval_cli.main(argv) == 1 and "--max-cost-usd is required" in capsys.readouterr().err


def test_the_endpoint_is_the_shims_systemone_route_with_a_bearer_token(monkeypatch):
    assert replay.endpoint(ARM) == (None, {})
    monkeypatch.setenv("D1_LOCAL_TOKEN", TOKEN)
    assert replay.endpoint(ARM) == (None, {})                                   # no URL yet
    for host in ("http://127.0.0.1:8765/", "http://localhost:8765", "http://[::1]:8765"):
        monkeypatch.setenv("D1_LOCAL_URL", host)
        url, headers = replay.endpoint(ARM)
        assert url == host.rstrip("/") + "/v1/systemone" and headers == {"Authorization": f"Bearer {TOKEN}"}
    monkeypatch.delenv("D1_LOCAL_TOKEN")
    assert replay.endpoint(ARM) == (None, {})


@pytest.mark.parametrize("url", ["https://example--mammoth-1-flide.modal.run", "http://192.168.1.20:8765",
                                 "http://127.0.0.1.evil.example", "http://0.0.0.0:8765"])
def test_a_local_arm_will_not_send_game_state_off_this_machine(monkeypatch, url):
    monkeypatch.setenv("D1_LOCAL_TOKEN", TOKEN)
    monkeypatch.setenv("D1_LOCAL_URL", url)
    with pytest.raises(ValueError, match="D1_LOCAL_URL must name this machine"):
        replay.endpoint(ARM)
    with pytest.raises(ValueError, match="D1_LOCAL_URL must name this machine"):
        replay.send_systemone(ARM, body())


def test_images_pacing_and_redaction_are_wired_for_the_arm():
    assert IMAGE_TRANSPORT[ARM] == "images"
    assert attach_images({"state": {}}, ARM, b"\xff\xd8")["images"][0].startswith("data:image/jpeg;base64,")
    assert default_tokens_per_minute(ARM) >= 1_000_000                          # the GPU paces it, not a provider
    environment = {"D1_LOCAL_TOKEN": "placeholder-token", "D1_LOCAL_URL": "http://127.0.0.1:8765",
                   "LIQUID_API_KEY": "placeholder-liquid-key"}
    assert secret_values(environment) == ["placeholder-liquid-key", "placeholder-token"]


def test_the_cli_refuses_a_local_arm_without_its_token_or_with_a_remote_url(tmp_path: Path, monkeypatch, capsys):
    files = Project(tmp_path, 1)
    monkeypatch.setenv("D1_LOCAL_URL", "http://127.0.0.1:8765")
    with pytest.raises(SystemExit) as refused:
        cli.main([*files.args(), "--s1-arm", ARM])
    assert refused.value.code == 2 and "D1_LOCAL_TOKEN is not set" in capsys.readouterr().err
    monkeypatch.setenv("D1_LOCAL_TOKEN", TOKEN)
    monkeypatch.setenv("D1_LOCAL_URL", "https://example.invalid")
    with pytest.raises(SystemExit) as remote:
        cli.main([*files.args(), "--s1-arm", ARM])
    assert remote.value.code == 2 and "must name this machine" in capsys.readouterr().err
    assert not files.out.exists()                                               # nothing was created or contacted


def test_the_cli_runs_a_local_arm_session_and_the_record_names_local_d1_2b_at_zero_cost(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("D1_LOCAL_TOKEN", TOKEN)
    monkeypatch.setenv("D1_LOCAL_URL", "http://127.0.0.1:8765")
    with GameThread(CatalogGame, catalog=export(two_actions()), effects={"move": (6.0, 0.0, 0.0)}) as thread:
        files = Project(tmp_path, thread.port, git=True)
        files.approval.write_text(json.dumps(local_approval(12)), encoding="utf-8")
        send = Send(files.out, local_reply("next_action", "move"), local_reply("next_action", "look"),
                    local_reply("next_action", "finish"))
        with_fake_send(monkeypatch, send)
        assert cli.main(files.args(s1_arm=ARM)) == 0
        assert thread.game.applied() == ["move", "look"]
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    assert (summary["status"], summary["ending"], summary["turns"]) == ("complete", "finish", 3)
    assert (summary["s1"]["provider"], summary["s1"]["model"], summary["s1"]["calls"]) == ("local", "d1-2b", 3)
    plan = json.loads((files.out / "plan.json").read_text(encoding="utf-8"))
    assert plan["arm"] == ARM and plan["tokens_per_minute"] == summary["settings"]["tokens_per_minute"] == 2_000_000
    entries = read_trace(files.out / "trace.jsonl")
    record = RunRecord.model_validate(entries[0].body)
    assert record.models == [ModelRef(role="s1", provider="local", model="d1-2b", prompt_hash=summary["s1"]["prompt_sha256"])]
    assert record.budget == Budget(max_wall_s=60.0, max_usd=None, max_frontier_calls=0)   # a zero cap cannot be stated
    calls = [entry.body for entry in entries if entry.kind is TraceKind.s1_call]
    assert [(c["provider"], c["model"], c["resolved_model"]) for c in calls] == [("local", "d1-2b", "d1-2b-fake")] * 3
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in (files.out / "calls").glob("*.json")]
    assert len(rows) == 3 and {(r["arm"], r["reserved_usd"], r["cost_usd"], r["cost_bound_exceeded"]) for r in rows} == {
        (ARM, "0", 0.0, False)}
    everything = "".join(p.read_text(encoding="utf-8") for p in files.out.rglob("*") if p.is_file())
    assert "liquid" not in everything.lower() and TOKEN not in everything and TOKEN not in capsys.readouterr().out


def test_the_cli_plays_over_http_against_the_fake_shim_and_its_replies_are_recorded_as_sent(tmp_path: Path, monkeypatch):
    with running_shim(tmp_path, "--fake") as shim, \
            GameThread(CatalogGame, catalog=export(two_actions()), effects={"move": (6.0, 0.0, 0.0)}) as thread:
        shim.use(monkeypatch)
        files = Project(tmp_path, thread.port, git=True)
        files.approval.write_text(json.dumps(local_approval(12)), encoding="utf-8")
        assert cli.main(files.args(s1_arm=ARM)) == 0                            # no `send` fake: the calls cross loopback
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    assert (summary["status"], summary["ending"]) == ("complete", "finish")      # the stand-in picks the first criterion
    assert (summary["s1"]["provider"], summary["s1"]["model"]) == ("local", "d1-2b") and summary["redactions"] == 0
    calls = [e.body for e in read_trace(files.out / "trace.jsonl") if e.kind is TraceKind.s1_call]
    assert len(calls) == summary["s1"]["calls"] >= 1
    for call in calls:
        assert (call["http_status"], call["problem"], call["resolved_model"]) == (200, None, "d1-2b-fake")
        assert call["usage"]["cost"] == 0.0 and call["usage"]["inference_ms"] >= 0
        assert call["response"]["model"] == "d1-2b-fake" and call["response"]["answers"]["next_action"]["type"] == "choice"
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in (files.out / "calls").glob("*.json")]
    assert {(r["reserved_usd"], r["cost_usd"], r["http_status"]) for r in rows} == {("0", 0.0, 200)}
    assert TOKEN not in shim.stderr()
