"""The `mammoth-1-modal` S1 arm: its server module's request rewriting and warmup, and the client wiring.

Offline: no Modal app is deployed, no model is called and no credential is read. The server module is
loaded with a small stand-in for `modal`, so only its pure functions run here.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

from playtest.bench import replay
from playtest.runner import modal_wake
from playtest.runner.image_transport import IMAGE_TRANSPORT, attach_images
from playtest.runner.output import secret_values
from playtest.runner.pacing import default_tokens_per_minute

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "modal" / "mammoth_flide.py"
ARM = "mammoth-1-modal"
JPEG_URL = "data:image/jpeg;base64,/9j/AAAA"


def _fake_modal() -> types.ModuleType:
    class _Chain:
        def __getattr__(self, name):
            return lambda *args, **kwargs: self

    module = types.ModuleType("modal")
    module.Image = types.SimpleNamespace(from_registry=lambda *args, **kwargs: _Chain())
    module.Volume = types.SimpleNamespace(from_name=lambda *args, **kwargs: object())
    module.App = lambda name: types.SimpleNamespace(server=lambda **kwargs: (lambda cls: cls))
    module.enter = lambda *args, **kwargs: (lambda fn: fn)
    module.exit = lambda *args, **kwargs: (lambda fn: fn)
    return module


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setitem(sys.modules, "modal", _fake_modal())
    monkeypatch.delenv("MAMMOTH_FLIDE_GPU", raising=False)
    spec = importlib.util.spec_from_file_location("mammoth_flide_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _body(**extra) -> dict:
    questions = {"next_action": {"type": "choice", "instructions": "pick", "criteria": {"b": "B", "a": "A"}}}
    # `state` keys deliberately out of alphabetical order: the server must keep the order it received.
    return {"model": "mammoth-1", "questions": questions, "state": {"zeta": 1, "alpha": {"y": 2, "x": 3}}, **extra}


def _raw(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --- the server's images transport -----------------------------------------------------------------------


def test_a_body_without_images_is_forwarded_byte_for_byte(server):
    raw = _raw(_body())
    assert server.translate(raw) is raw


def test_images_become_flide_media_with_the_state_wrapped_in_received_order(server):
    out = server.translate(_raw(_body(images=[JPEG_URL, JPEG_URL])))
    value = json.loads(out)
    assert list(value) == ["media", "model", "questions", "state"]            # top level sorted, as verified
    assert list(value["state"]) == ["state", "images"]                        # the image after the state
    assert value["state"]["images"] == ["<image:1>", "<image:2>"]
    assert list(value["state"]["state"]) == ["zeta", "alpha"]                 # never re-sorted
    assert list(value["state"]["state"]["alpha"]) == ["y", "x"]
    assert list(value["questions"]["next_action"]["criteria"]) == ["b", "a"]  # option order kept
    assert value["media"] == [{"data": JPEG_URL, "type": "image"}] * 2
    assert out.startswith(b'{"media":[{"data":')                              # entry keys sorted too


def test_an_empty_images_array_is_dropped_without_wrapping(server):
    value = json.loads(server.translate(_raw(_body(images=[]))))
    assert "images" not in value and "media" not in value
    assert value["state"] == {"zeta": 1, "alpha": {"y": 2, "x": 3}}


@pytest.mark.parametrize("extra, message", [
    ({"media": []}, "not media"),
    ({"images": "x"}, "data:image"),
    ({"images": ["https://example.com/a.jpg"]}, "data:image"),
    ({"images": [JPEG_URL] * 5}, "at most"),
])
def test_ambiguous_or_invalid_images_are_refused(server, extra, message):
    with pytest.raises(server.BadRequest, match=message):
        server.translate(_raw(_body(**extra)))


def test_a_body_that_is_not_a_json_object_is_refused(server):
    for raw in (b"not json", b"[1, 2]"):
        with pytest.raises(server.BadRequest):
            server.translate(raw)


def test_the_embedded_lock_matches_the_pilots_reviewed_lock(server):
    lock = (ROOT / "scripts" / "probes" / "mammoth_flide_requirements.txt").read_text(encoding="utf-8")
    assert server.LOCKED_REQUIREMENTS.splitlines() == lock.strip().splitlines()


def test_the_serve_command_states_the_measured_settings(server):
    snapshot = Path("/cache/hf/hub/snapshots") / server.MODEL_REVISION
    command = server.serve_command(snapshot, "flide")
    pairs = dict(zip(command[2::2], command[3::2]))
    assert pairs["--dtype"] == "bfloat16" and pairs["--session-cache-gb"] == "0"
    assert pairs["--port"] == str(server.FLIDE_PORT) != str(server.PORT)
    assert pairs["--host"] == "127.0.0.1" and pairs["--planners"] == "4"
    assert "--quantize" not in command                                        # FP8 failed the 0.05 rule
    with pytest.raises(ValueError):
        server.serve_command(Path("/cache/other-revision"), "flide")


def test_the_default_gpu_is_b200(server):
    assert server.GPU == "B200" and server.IDLE_S == 300


# --- warmup ----------------------------------------------------------------------------------------------


def _answer(plan_s):
    return {"answers": {}, "onejev": {"timing": {"plan_s": plan_s}}}


def test_warmup_stops_after_the_first_round_that_plans_fast(server):
    plans = iter([0.04] + [0.02] * 2 + [1.1] * 4 + [0.01] * 4)
    sent = []

    def post(body):
        sent.append(body)
        return {**_answer(next(plans)), "usage": {"input_tokens": len(body) // 4}}

    report = server.warm_planners(post, b"jpeg", text_words=(100, 400))
    assert report["warm"] is True and report["text_plan_s"] == 0.04
    assert [size["tokens"] for size in report["text_sizes"]] == [len(sent[1]) // 4, len(sent[2]) // 4]
    assert len(sent[2]) > len(sent[1]) > len(sent[0])                         # padded to each size
    assert report["rounds"] == [[1.1] * 4, [0.01] * 4]
    assert len(sent) == 11 and b'"media"' in sent[-1] and b'"media"' not in sent[2]


def test_a_missing_timing_leaves_the_pool_unproven(server):
    report = server.warm_planners(lambda body: {"answers": {}}, b"jpeg", rounds=2, text_words=())
    assert report == {"text_plan_s": None, "text_sizes": [], "rounds": [[None] * 4, [None] * 4], "warm": False}


def test_warmup_requests_are_synthetic_and_in_the_proxied_shape(server):
    value = json.loads(server.warmup_body(b"\xff\xd8jpeg"))
    assert value["state"]["state"] == {"note": "server warmup"}
    assert value["media"][0]["data"].startswith("data:image/jpeg;base64,")


# --- the client arm --------------------------------------------------------------------------------------


@pytest.fixture
def modal_env(monkeypatch):
    monkeypatch.setenv("MODAL_MAMMOTH_TOKEN_ID", "wk-test-token-id")
    monkeypatch.setenv("MODAL_MAMMOTH_TOKEN_SECRET", "ws-test-token-secret")
    monkeypatch.setenv("MODAL_MAMMOTH_URL", "https://example--mammoth-1-flide.modal.run/")


def test_the_arm_is_registered_with_its_own_provider_and_credentials():
    assert replay.ARMS[ARM][0] == "mammoth-1"
    assert replay.provider_of(ARM) == "modal"
    assert replay.credential_variables(ARM) == ("MODAL_MAMMOTH_TOKEN_ID", "MODAL_MAMMOTH_URL",
                                                 "MODAL_MAMMOTH_TOKEN_SECRET")
    assert replay.credential_variables("liquid-d1-ultra") == ("LIQUID_API_KEY", "LIQUID_D1.1_URL")
    assert replay.credential_variables("mammoth-1") == ("BASETEN_API_KEY",)


def test_the_endpoint_uses_modal_proxy_headers_on_the_systemone_path(modal_env):
    url, headers = replay.endpoint(ARM)
    assert url == "https://example--mammoth-1-flide.modal.run/v1/systemone"
    assert headers == {"Modal-Key": "wk-test-token-id", "Modal-Secret": "ws-test-token-secret"}


def test_a_missing_secret_leaves_the_endpoint_unset(modal_env, monkeypatch):
    monkeypatch.delenv("MODAL_MAMMOTH_TOKEN_SECRET")
    assert replay.endpoint(ARM) == (None, {})
    with pytest.raises(ValueError, match="missing endpoint"):
        replay.send_systemone(ARM, {"questions": {}})


def test_send_posts_the_canonical_body_with_the_proxy_headers(modal_env, monkeypatch):
    seen = {}

    def fake_send(url, payload, headers):
        seen.update(url=url, payload=payload, headers=headers)
        return 200, b'{"model":"mammoth-1","answers":{}}'

    monkeypatch.delenv("UPA_S1_KEEPALIVE", raising=False)
    monkeypatch.setattr(replay, "_send_keepalive", fake_send)
    status, data, _, problem = replay.send_systemone(ARM, {"state": {"b": 1, "a": 2}, "questions": {}})
    assert (status, problem, data["model"]) == (200, None, "mammoth-1")
    assert seen["url"].endswith("/v1/systemone")
    assert seen["headers"]["Modal-Key"] == "wk-test-token-id" and "Authorization" not in seen["headers"]
    assert seen["payload"] == b'{"questions":{},"state":{"a":2,"b":1}}'


def test_the_arm_takes_the_plain_images_transport():
    assert IMAGE_TRANSPORT[ARM] == "images"
    body = attach_images({"state": {}}, ARM, b"\xff\xd8")
    assert body["images"][0].startswith("data:image/jpeg;base64,")


def test_the_arm_is_not_paced_to_a_provider_limit():
    assert default_tokens_per_minute(ARM) >= 1_000_000


def test_proxy_token_values_are_screened_from_traces(monkeypatch):
    monkeypatch.setattr("os.environ", {"MODAL_MAMMOTH_TOKEN_SECRET": "placeholder-token-secret",
                                       "MODAL_MAMMOTH_TOKEN_ID": "placeholder-token-id",
                                       "MODAL_MAMMOTH_URL": "https://not-a-secret.example"})
    assert secret_values() == ["placeholder-token-id", "placeholder-token-secret"]


# --- waking the server -----------------------------------------------------------------------------------


class _Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status


def _clock():
    now = [0.0]
    return (lambda: now[0]), (lambda seconds: now.__setitem__(0, now[0] + seconds))


def test_wake_waits_through_a_cold_start(modal_env):
    answers = [urllib.error.HTTPError("u", 503, "loading", {}, None), TimeoutError(),
               _Response(200, {"status": "ok"})]
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("Modal-key")))
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monotonic, sleep = _clock()
    logs = []
    elapsed = modal_wake.wake(ARM, urlopen=urlopen, monotonic=monotonic, sleep=sleep, log=logs.append)
    assert elapsed == 10.0 and len(logs) == 1
    assert seen[0] == ("https://example--mammoth-1-flide.modal.run/health", "wk-test-token-id")


@pytest.mark.parametrize("code", [401, 403, 404])
def test_wake_stops_on_a_wrong_token_or_url(modal_env, code):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError("u", code, "no", {}, None)

    with pytest.raises(modal_wake.WakeError, match=str(code)):
        modal_wake.wake(ARM, urlopen=urlopen, sleep=lambda s: None)


def test_wake_gives_up_at_its_deadline(modal_env):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError("u", 503, "loading", {}, None)

    monotonic, sleep = _clock()
    with pytest.raises(modal_wake.WakeError, match="not ready"):
        modal_wake.wake(ARM, timeout_s=30, urlopen=urlopen, monotonic=monotonic, sleep=sleep, log=lambda s: None)


def test_the_proxy_forwards_translated_bodies_and_refuses_bad_ones(server, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import threading

    from fastapi.testclient import TestClient

    seen = []

    def forward(method, path, data=None, timeout=0):
        seen.append((method, path, data))
        return 200, b'{"model":"mammoth-1"}', {"content-type": "application/json", "x-decision-inference-ms": "9.5"}

    monkeypatch.setattr(server, "_forward", forward)
    ready = threading.Event()
    client = TestClient(server.make_proxy(ready))
    assert client.get("/health").status_code == 503                           # not before warmup
    ready.set()
    response = client.post("/v1/systemone", content=_raw(_body(images=[JPEG_URL])))
    assert response.status_code == 200 and response.headers["x-decision-inference-ms"] == "9.5"
    assert seen[-1][:2] == ("POST", "/v1/systemone") and b'"media"' in seen[-1][2]
    assert client.post("/v1/systemone", content=b'{"media":[]}').status_code == 422
