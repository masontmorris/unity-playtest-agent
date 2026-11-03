"""Offline checks for the Mammoth-1 / Flide Modal Server prototype.

No Modal app is deployed, no image is built, no model is contacted, and no
credential is read. The real-Modal import check is skipped when Modal is not
installed in the test environment; the remaining checks inject a small local
fake so the app definition can be exercised without cloud dependencies.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import itertools
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probes" / "modal_mammoth_flide.py"
_MODULE_COUNTER = itertools.count(1)


def test_embedded_requirements_match_reviewed_lock(monkeypatch):
    module = _load(monkeypatch)
    lock = SCRIPT.with_name("mammoth_flide_requirements.txt").read_text(encoding="utf-8")
    assert module.image_install_requirements() == lock.strip().splitlines()
    assert "ephemeral_disk" not in module.SERVER_CONFIG


class _FakeImage:
    def __init__(self, ref: str):
        self.ref = ref
        self.calls: list[tuple] = []

    @classmethod
    def from_registry(cls, ref: str, **kwargs):
        return cls(ref)

    def apt_install(self, *packages: str):
        self.calls.append(("apt_install", tuple(packages)))
        return self

    def pip_install(self, *packages: str, **kwargs):
        self.calls.append(("pip_install", tuple(packages), dict(kwargs)))
        return self

    def env(self, values: dict[str, str], **kwargs):
        self.calls.append(("env", dict(values)))
        return self


class _FakeVolume:
    def __init__(self, name: str, *, create_if_missing: bool = False):
        self.name = name
        self.create_if_missing = create_if_missing
        self.commits = 0

    @classmethod
    def from_name(cls, name: str, **kwargs):
        return cls(name, create_if_missing=kwargs.get("create_if_missing", False))

    def commit(self) -> None:
        self.commits += 1


class _FakeApp:
    def __init__(self, name: str):
        self.name = name
        self.server_kwargs: dict | None = None
        self.server_class: type | None = None

    def server(self, **kwargs):
        def register(cls):
            self.server_kwargs = dict(kwargs)
            self.server_class = cls
            return cls

        return register


def _fake_modal() -> types.ModuleType:
    module = types.ModuleType("modal")
    module.App = _FakeApp
    module.Image = _FakeImage
    module.Volume = _FakeVolume
    module.Server = object
    module.enter = lambda *args, **kwargs: (lambda fn: fn)
    module.exit = lambda *args, **kwargs: (lambda fn: fn)
    module.__version__ = "test-fake"
    return module


def _load(monkeypatch: pytest.MonkeyPatch, *, gpu: str | None = "H200", fake: bool = True):
    if gpu is None:
        monkeypatch.delenv("MAMMOTH_FLIDE_GPU", raising=False)
    else:
        monkeypatch.setenv("MAMMOTH_FLIDE_GPU", gpu)
    if fake:
        monkeypatch.setitem(sys.modules, "modal", _fake_modal())
    else:
        monkeypatch.delitem(sys.modules, "modal", raising=False)

    name = f"modal_mammoth_flide_test_{next(_MODULE_COUNTER)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class _Response:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Proc:
    def __init__(self, code: int | None = None):
        self.code = code

    def poll(self):
        return self.code


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_module_compiles_without_cloud_imports() -> None:
    compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")


def test_gpu_is_selected_explicitly_and_has_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch, gpu="h200")
    assert module.GPU == "H200"
    assert module.resolve_gpu(" b200 ") == "B200"
    for bad in (None, "", "A100", "H200,B200", "H200 B200"):
        with pytest.raises(ValueError, match=module.GPU_ENV):
            module.resolve_gpu(bad)


def test_import_without_gpu_setting_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="MAMMOTH_FLIDE_GPU"):
        _load(monkeypatch, gpu=None)


def test_server_is_private_regional_and_single_container(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)
    config = module.SERVER_CONFIG
    assert config["unauthenticated"] is False
    assert config["routing_region"] == config["compute_region"] == "us-east"
    assert config["gpu"] == "H200" and isinstance(config["gpu"], str)
    assert config["env"] == {"MAMMOTH_FLIDE_GPU": "H200"}
    assert config["max_containers"] == 1
    assert config["min_containers"] == 0
    assert config["target_concurrency"] == 1
    assert config["max_concurrency"] == 1
    assert config["scaledown_window"] == 120
    assert config["startup_timeout"] == 1800
    assert config["port"] == 8000
    assert config["volumes"] == {module.CACHE_MOUNT: module.CACHE_VOLUME}
    assert module.APP.server_kwargs == config
    assert module.APP.server_class is module.MammothFlideServer


def test_b200_arm_changes_only_the_gpu_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    h200 = _load(monkeypatch, gpu="H200")
    b200 = _load(monkeypatch, gpu="B200")
    assert h200.SERVER_CONFIG["gpu"] == "H200"
    assert b200.SERVER_CONFIG["gpu"] == "B200"
    assert h200.SERVER_CONFIG["env"] == {"MAMMOTH_FLIDE_GPU": "H200"}
    assert b200.SERVER_CONFIG["env"] == {"MAMMOTH_FLIDE_GPU": "B200"}
    for key in (
        "routing_region",
        "compute_region",
        "max_containers",
        "min_containers",
        "target_concurrency",
        "max_concurrency",
        "scaledown_window",
        "startup_timeout",
        "unauthenticated",
    ):
        assert h200.SERVER_CONFIG[key] == b200.SERVER_CONFIG[key]


def test_image_is_digest_pinned_and_installs_exact_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)
    image = module.IMAGE
    assert image.ref == module.BASE_IMAGE
    assert module.BASE_IMAGE.startswith("python:3.12.13-slim-bookworm@sha256:")
    assert len(module.BASE_IMAGE_DIGEST) == len("sha256:") + 64
    assert [call[0] for call in image.calls] == ["apt_install", "pip_install", "env"]
    assert set(module.APT_PACKAGES) <= set(image.calls[0][1])
    assert image.calls[1][1] == tuple(module.image_install_requirements())
    assert image.calls[1][2] == {"extra_options": "--no-cache-dir"}
    assert "torch==2.14.0" in image.calls[1][1]
    assert "torchvision==0.29.0" in image.calls[1][1]
    assert "flide==0.1.7" in image.calls[1][1]
    assert image.calls[2][1] == module.image_environment()
    assert image.calls[2][1]["MAMMOTH_FLIDE_GPU"] == "H200"
    assert image.calls[2][1]["HF_HOME"] == "/cache/hf"
    assert image.calls[2][1]["TRITON_CACHE_DIR"] == "/cache/triton"


def test_snapshot_download_pins_revision_and_uses_no_local_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load(monkeypatch)
    calls: dict = {}

    def download(**kwargs):
        calls.update(kwargs)
        path = Path(kwargs["cache_dir"]) / "models--baselabs--mammoth-1" / "snapshots" / kwargs["revision"]
        path.mkdir(parents=True)
        (path / "config.json").write_text("{}", encoding="utf-8")
        return str(path)

    result = module.ensure_snapshot(cache_dir=tmp_path, download=download)
    assert result.name == module.MODEL_REVISION
    assert calls == {
        "repo_id": module.MODEL_REPO,
        "revision": module.MODEL_REVISION,
        "cache_dir": str(tmp_path),
    }

    def wrong_commit(**kwargs):
        path = Path(kwargs["cache_dir"]) / "snapshots" / ("0" * 40)
        path.mkdir(parents=True)
        (path / "config.json").write_text("{}", encoding="utf-8")
        return str(path)

    with pytest.raises(RuntimeError, match="pinned revision"):
        module.ensure_snapshot(cache_dir=tmp_path, download=wrong_commit)

    def missing_config(**kwargs):
        path = Path(kwargs["cache_dir"]) / "snapshots" / kwargs["revision"]
        path.mkdir(parents=True)
        return str(path)

    with pytest.raises(RuntimeError, match="config.json"):
        module.ensure_snapshot(cache_dir=tmp_path, download=missing_config)


def test_native_flide_command_freezes_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)
    snapshot = Path("/cache/hf/hub/models--baselabs--mammoth-1/snapshots") / module.MODEL_REVISION
    assert module.build_serve_command(snapshot) == [
        "flide",
        "serve",
        "--model",
        str(snapshot),
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--dtype",
        "bfloat16",
        "--batch-window-ms",
        "0",
        "--session-cache-gb",
        "0",
        "--frontends",
        "0",
    ]
    assert module.build_serve_command(snapshot, flide_bin="/opt/flide")[0] == "/opt/flide"
    with pytest.raises(ValueError, match="only allowed revision"):
        module.build_serve_command(Path("/cache/hf/hub/snapshots/deadbeef"))


def test_wait_for_ready_requires_the_native_route(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)
    clock = _Clock()

    def open_url(url: str, timeout: float):
        if clock.now < 1.0:
            raise urllib.error.URLError("still loading")
        if url.endswith("/health"):
            return _Response(200, b'{"status": "ok"}')
        raise AssertionError(url)

    ready = module.wait_for_ready(
        _Proc(),
        timeout_s=10,
        urlopen=open_url,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        poll_interval_s=1.0,
    )
    assert ready == 1.0


def test_wait_for_ready_accepts_405_as_route_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)

    def open_url(url: str, timeout: float):
        if url.endswith("/health") or url.endswith("/openapi.json"):
            raise urllib.error.HTTPError(url, 404, "not found", None, None)
        raise urllib.error.HTTPError(url, 405, "method not allowed", None, None)

    assert module.wait_for_ready(_Proc(), timeout_s=1, urlopen=open_url, monotonic=lambda: 0.0) == 0.0


def test_loading_health_is_not_overridden_by_route_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)
    calls: list[str] = []

    def open_url(url: str, timeout: float):
        calls.append(url)
        if url.endswith("/health"):
            raise urllib.error.HTTPError(url, 503, "loading", None, None)
        return _Response(200, json.dumps({"paths": {"/v1/systemone": {}}}).encode())

    clock = _Clock()
    with pytest.raises(TimeoutError, match="did not expose"):
        module.wait_for_ready(
            _Proc(),
            timeout_s=2,
            urlopen=open_url,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            poll_interval_s=1.0,
        )
    assert calls and all(url.endswith("/health") for url in calls)


def test_wait_for_ready_times_out_and_detects_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)

    def never(url: str, timeout: float):
        raise urllib.error.URLError("not up")

    clock = _Clock()
    with pytest.raises(TimeoutError, match="did not expose"):
        module.wait_for_ready(
            _Proc(),
            timeout_s=3,
            urlopen=never,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            poll_interval_s=1.0,
        )
    with pytest.raises(RuntimeError, match="exited during startup"):
        module.wait_for_ready(_Proc(7), timeout_s=3, urlopen=never)


def test_stop_process_terminates_once(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)

    class Terminating:
        def __init__(self):
            self.code = None
            self.terminated = False

        def poll(self):
            return self.code

        def terminate(self):
            self.terminated = True
            self.code = 0

        def wait(self, timeout=None):
            return self.code

    process = Terminating()
    module.stop_process(process, grace_s=1)
    assert process.terminated is True


def test_require_cuda_refuses_a_different_assigned_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)

    class Cuda:
        def __init__(self, name: str):
            self.name = name

        def is_available(self) -> bool:
            return True

        def get_device_name(self, index: int) -> str:
            return self.name

        def get_device_properties(self, index: int):
            return types.SimpleNamespace(major=9, minor=0, total_memory=149 * 1024 * 1024 * 1024)

    wrong = types.SimpleNamespace(cuda=Cuda("NVIDIA H100"), version=types.SimpleNamespace(cuda="13.0"))
    with pytest.raises(RuntimeError, match="different hardware"):
        module.require_cuda("H200", wrong)

    right = types.SimpleNamespace(cuda=Cuda("NVIDIA H200"), version=types.SimpleNamespace(cuda="13.0"))
    facts = module.require_cuda("H200", right)
    assert facts["device_name"] == "NVIDIA H200"
    assert facts["capability"] == "9.0"
    assert facts["cuda_version"] == "13.0"


def test_driver_facts_keeps_missing_values_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)
    completed = types.SimpleNamespace(
        returncode=0, stdout="NVIDIA B200, 580.65, 183359 MiB\n"
    )
    assert module.driver_facts(lambda *args, **kwargs: completed) == {
        "name": "NVIDIA B200",
        "driver_version": "580.65",
        "memory_total": "183359 MiB",
    }
    failed = types.SimpleNamespace(returncode=1, stdout="")
    assert module.driver_facts(lambda *args, **kwargs: failed) == {
        "name": None,
        "driver_version": None,
        "memory_total": None,
    }


def test_runtime_report_preserves_unknowns_as_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load(monkeypatch)
    snapshot = tmp_path / "snapshots" / module.MODEL_REVISION
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")

    report = module.runtime_report(
        snapshot=snapshot,
        gpu="B200",
        gpu_facts={
            "device_name": "NVIDIA B200",
            "capability": "10.0",
            "memory_mib": 180000,
            "cuda_version": "13.0",
        },
        ready_s=12.5,
        versions={"flide": "0.1.7", "torch": None},
        driver={"name": "NVIDIA B200", "driver_version": None, "memory_total": None},
        generated_at="2026-11-02T00:00:00+00:00",
    )
    assert report["packages"]["flide"] == "0.1.7"
    assert report["packages"]["torch"] is None
    assert report["packages"]["numpy"] is None
    assert report["startup"]["flide_route_ready_s"] == 12.5
    assert report["startup"]["startup_elapsed_s"] is None
    assert report["gpu"]["requested"] == "B200"
    assert report["gpu"]["driver"]["driver_version"] is None
    assert report["auth"] == {"mode": "modal-proxy", "unauthenticated": False}
    assert report["generated_at_utc"] == "2026-11-02T00:00:00+00:00"
    assert report["model"]["files"]["config.json"] == hashlib.sha256(b"{}").hexdigest()
    assert report["model"]["files"]["readout.json"] is None
    assert module.snapshot_file_hashes(snapshot) == {
        "config.json": hashlib.sha256(b"{}").hexdigest(),
        "readout.json": None,
    }

    with pytest.raises(ValueError, match="non-pinned snapshot"):
        module.runtime_report(
            snapshot=tmp_path / "deadbeef",
            gpu="B200",
            gpu_facts={},
            ready_s=None,
            versions={},
            driver=None,
        )

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert not ({"token", "secret", "api_key", "hf_token"} & set(keys(report)))


def test_verify_pins_rejects_drift_and_missing_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)

    def exact(name: str) -> str:
        return module.PIP_PINS[name]

    assert module.verify_pins(exact) == module.PIP_PINS

    def drifted(name: str) -> str:
        return "2.13.0" if name == "torch" else module.PIP_PINS[name]

    with pytest.raises(RuntimeError, match="torch"):
        module.verify_pins(drifted)

    def missing(name: str) -> str:
        if name == "flide":
            raise importlib.metadata.PackageNotFoundError(name)
        return module.PIP_PINS[name]

    with pytest.raises(RuntimeError, match="flide"):
        module.verify_pins(missing)


def test_static_manifest_is_offline_and_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)
    manifest = module.static_manifest()
    assert manifest["cloud_actions"] == "none"
    assert manifest["pilot"]["ceiling_usd"] == 20
    assert manifest["model"]["revision"] == "61cec44c281c693992e3d6ebeb44dd3402dfa186"
    assert manifest["server"]["unauthenticated"] is False
    assert manifest["server"]["min_containers"] == 0
    assert manifest["server"]["max_containers"] == 1
    assert manifest["server"]["target_concurrency"] == 1
    assert manifest["server"]["routing_region"] == "us-east"
    assert manifest["server"]["compute_region"] == "us-east"
    assert "flide==0.1.7" in manifest["package_requirements"]
    json.dumps(manifest)  # must remain JSON-safe


@pytest.mark.skipif(importlib.util.find_spec("modal") is None, reason="Modal is not installed")
def test_module_imports_with_installed_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch, gpu="H200", fake=False)
    assert module.APP.name == module.APP_NAME
    assert module.SERVER_CONFIG["unauthenticated"] is False
    assert module.BASE_IMAGE.startswith("python:3.12.13-slim-bookworm@sha256:")
