"""The `modal-d1-2b` S1 arm: its client wiring and its server module's settings.

Offline: no Modal app is deployed, no model is called and no credential is read. The server module is
loaded with a small stand-in for `modal`, so only its module-level settings run here.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from playtest.bench import replay
from playtest.runner import modal_wake
from playtest.runner.image_transport import IMAGE_TRANSPORT
from playtest.runner.pacing import default_tokens_per_minute

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "modal" / "d1_2b.py"
ARM = "modal-d1-2b"


def _fake_modal() -> types.ModuleType:
    class _Chain:
        def __getattr__(self, name):
            return lambda *args, **kwargs: self

    module = types.ModuleType("modal")
    module.Image = types.SimpleNamespace(debian_slim=lambda *args, **kwargs: _Chain())
    module.Volume = types.SimpleNamespace(from_name=lambda *args, **kwargs: object())
    module.App = lambda name: types.SimpleNamespace(name=name, cls=lambda **kwargs: (lambda cls: cls))
    module.enter = lambda *args, **kwargs: (lambda fn: fn)
    module.asgi_app = lambda *args, **kwargs: (lambda fn: fn)
    return module


def _load(monkeypatch, **env):
    monkeypatch.setitem(sys.modules, "modal", _fake_modal())
    for name in ("D1_2B_GPU", "D1_2B_IDLE_S"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("d1_2b_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_arm_posts_to_the_modal_url_with_the_workspace_proxy_token(monkeypatch):
    monkeypatch.setenv("MODAL_D1_2B_URL", "https://example--d1-2b-s1-d1server-web.modal.run/")
    monkeypatch.setenv("MODAL_MAMMOTH_TOKEN_ID", "wk-id")
    monkeypatch.setenv("MODAL_MAMMOTH_TOKEN_SECRET", "ws-secret")
    url, headers = replay.endpoint(ARM)
    assert url == "https://example--d1-2b-s1-d1server-web.modal.run/v1/systemone"
    assert headers == {"Modal-Key": "wk-id", "Modal-Secret": "ws-secret"}
    assert modal_wake.health_url(ARM)[0] == "https://example--d1-2b-s1-d1server-web.modal.run/health"
    assert replay.credential_variables(ARM) == ("MODAL_MAMMOTH_TOKEN_ID", "MODAL_D1_2B_URL",
                                                "MODAL_MAMMOTH_TOKEN_SECRET")
    assert replay.provider_of(ARM) == "modal"


def test_the_arm_has_no_endpoint_without_every_credential(monkeypatch):
    monkeypatch.setenv("MODAL_D1_2B_URL", "https://example.modal.run")
    monkeypatch.setenv("MODAL_MAMMOTH_TOKEN_ID", "wk-id")
    monkeypatch.delenv("MODAL_MAMMOTH_TOKEN_SECRET", raising=False)
    assert replay.endpoint(ARM) == (None, {})


def test_the_arm_sends_images_and_is_paced_like_the_local_arm():
    assert IMAGE_TRANSPORT[ARM] == IMAGE_TRANSPORT["local-d1-2b"] == "images"
    assert default_tokens_per_minute(ARM) == default_tokens_per_minute("local-d1-2b")
    assert replay.ARMS[ARM][0] == replay.ARMS["local-d1-2b"][0] == "d1-2b"


def test_the_session_script_checks_the_arm_credentials_by_name():
    script = (ROOT / "scripts" / "unity" / "run-session.ps1").read_text(encoding="utf-8")
    assert "'modal-d1-2b' = @('MODAL_MAMMOTH_TOKEN_ID', 'MODAL_D1_2B_URL', 'MODAL_MAMMOTH_TOKEN_SECRET')" in script


def test_the_server_reads_its_gpu_and_idle_window_at_deploy_time(monkeypatch):
    module = _load(monkeypatch)
    assert (module.APP_NAME, module.GPU, module.IDLE_S) == ("d1-2b-s1", "A10", 300)
    assert module.SERVER_FILE == ROOT / "scripts" / "local" / "d1_server.py"
    assert _load(monkeypatch, D1_2B_GPU="l40s", D1_2B_IDLE_S="900").GPU == "L40S"
    with pytest.raises(ValueError, match="D1_2B_GPU"):
        _load(monkeypatch, D1_2B_GPU="T4")
    with pytest.raises(ValueError, match="D1_2B_IDLE_S"):
        _load(monkeypatch, D1_2B_IDLE_S="30")
