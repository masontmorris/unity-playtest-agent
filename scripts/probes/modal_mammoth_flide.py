"""Offline-only Modal Server prototype for the Mammoth-1 / Flide latency pilot.

This file is a private, temporary prototype for the bounded H200-versus-B200
pilot in ``docs/design/mammoth-modal-latency-pilot.md``. Running it as a script
prints its frozen manifest only. It does not deploy, build an image, contact
Modal or Hugging Face, invoke a model, read a credential, or spend money. The
parent process owns deployment, requests and teardown under the approved pilot.

Project laws preserved here:

* S1, not code, decides actions and escalation. This server only exposes Flide's
  native ``POST /v1/systemone`` (and ``POST /flush_cache``); it never ranks,
  substitutes, suppresses, or routes a choice.
* The core is name-blind; this probe adds no game knowledge and does not touch
  ``src/playtest/core/``.
* Measured, not inferred: every runtime fact this module cannot measure is
  reported as ``null``/``None``, never as zero, success, or failure.
* This repo is public: the offline prototype reads no credential and prints or
  stores none. A container may inherit an ambient ``HF_TOKEN`` for snapshot
  access; it is never logged. Full run logs stay under the local ``runs/`` tree.
* Budget before spend: this module makes no paid call and requires no model
  request. The parent must record call counts, estimated cost and a verified
  balance before deploying.

Pinned deployment assumptions:

* Base image ``python:3.12.13-slim-bookworm`` at the amd64 digest below, plus
  PyPI ``torch==2.14.0`` / ``torchvision==0.29.0`` (the CUDA 13 build) and
  ``flide==0.1.7``.
* Modal's H200/B200 host driver must run the CUDA 13 PyTorch build. There is no
  automatic fallback to a CUDA 12 build, a different GPU, dtype, engine, model,
  or revision.
* Flide documents precompiled kernels for sm90 (H100/H200) on PyTorch 2.13 and
  2.14. H200 therefore starts without kernel compilation. B200 is sm100, so its
  first start compiles Flide's kernels into the mounted Triton cache; that work
  and its cost are startup work, not client inference requests.
* The first-ever container start downloads the pinned HF snapshot into a Modal
  Volume. Later starts reuse the content-addressed snapshot. The snapshot is
  used read-only and is never passed through ``local_dir`` or modified.
* Flide 0.1.7 creates its FastAPI app after loading the engine when
  ``--frontends 0``, and ``GET /health`` then returns ``status: ok``. Readiness
  requires that response before ``@modal.enter`` returns, with native route
  checks as a fallback. This remains a liveness check, not proof that every
  weight is resident; the parent must stop the arm on any log error or
  unexpected restart.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import modal

APP_NAME = "mammoth-flide-pilot"
GPU_ENV = "MAMMOTH_FLIDE_GPU"
GPU_CHOICES = ("H200", "B200")

MODEL_REPO = "baselabs/mammoth-1"
MODEL_REVISION = "61cec44c281c693992e3d6ebeb44dd3402dfa186"
MODEL_NAME = "mammoth-1"

FLIDE_VERSION = "0.1.7"
TORCH_VERSION = "2.14.0"
TORCHVISION_VERSION = "0.29.0"
TRANSFORMERS_VERSION = "5.22.1"
HUGGINGFACE_HUB_VERSION = "2.4.0"
FLASH_LINEAR_ATTENTION_VERSION = "0.5.2"
ACCELERATE_VERSION = "1.15.0"
FASTAPI_VERSION = "0.162.0"
UVICORN_VERSION = "0.54.0"
PILLOW_VERSION = "13.0.0"
PYDANTIC_VERSION = "2.14.1"

# python:3.12.13-slim-bookworm, linux/amd64 manifest digest from Docker Hub.
BASE_IMAGE_DIGEST = "sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af"
BASE_IMAGE = f"python:3.12.13-slim-bookworm@{BASE_IMAGE_DIGEST}"

PIP_PINS: dict[str, str] = {
    "torch": TORCH_VERSION,
    "torchvision": TORCHVISION_VERSION,
    "flide": FLIDE_VERSION,
    "transformers": TRANSFORMERS_VERSION,
    "huggingface-hub": HUGGINGFACE_HUB_VERSION,
    "flash-linear-attention": FLASH_LINEAR_ATTENTION_VERSION,
    "accelerate": ACCELERATE_VERSION,
    "fastapi": FASTAPI_VERSION,
    "uvicorn": UVICORN_VERSION,
    "pillow": PILLOW_VERSION,
    "pydantic": PYDANTIC_VERSION,
}
REPORT_PACKAGES = tuple(PIP_PINS) + (
    "numpy",
    "safetensors",
    "tokenizers",
    "triton",
    "nvidia-cuda-runtime",
    "cuda-toolkit",
)

APT_PACKAGES = ("ca-certificates", "curl", "libgomp1", "libgl1", "libglib2.0-0")

CACHE_VOLUME_NAME = "mammoth-flide-pilot-cache"
CACHE_MOUNT = "/cache"
HF_HOME = "/cache/hf"
TRITON_CACHE_DIR = "/cache/triton"
TORCHINDUCTOR_CACHE_DIR = "/cache/torchinductor"

PORT = 8000
REGION = "us-east"
MIN_CONTAINERS = 0
MAX_CONTAINERS = 1
TARGET_CONCURRENCY = 1
MAX_CONCURRENCY = 1
BUFFER_CONTAINERS = 0
SCALEDOWN_WINDOW_S = 120
STARTUP_TIMEOUT_S = 1800
READY_TIMEOUT_S = STARTUP_TIMEOUT_S - 120
EXIT_GRACE_PERIOD_S = 30
CPU_CORES = 4.0
MEMORY_MIB = 65536
EPHEMERAL_DISK_MIB = None  # use Modal's default; explicit requests have a 512 GiB minimum
POLL_INTERVAL_S = 2.0
HTTP_TIMEOUT_S = 2.0
REPORT_PREFIX = "MAMMOTH_FLIDE_REPORT "

# Explicit environment-selected arm. There is intentionally no default: a
# deployment without a valid value fails before any image or container exists.
def resolve_gpu(value: str | None) -> str:
    """Return exactly H200 or B200, or raise; never chooses a fallback."""
    if value is None or not value.strip():
        raise ValueError(
            f"{GPU_ENV} must be set explicitly to one of {', '.join(GPU_CHOICES)}; "
            "there is no default GPU and no fallback"
        )
    normalized = value.strip().upper()
    if normalized not in GPU_CHOICES:
        raise ValueError(
            f"{GPU_ENV}={value!r} is not one of {', '.join(GPU_CHOICES)}; "
            "there is no default GPU and no fallback"
        )
    return normalized


GPU = resolve_gpu(os.environ.get(GPU_ENV))


LOCKED_REQUIREMENTS = """accelerate==1.15.0
annotated-doc==0.0.5
annotated-types==0.8.0
anyio==4.16.0
click==8.5.0
cuda-bindings==13.4.4
cuda-pathfinder==1.8.5
cuda-toolkit==13.0.3.0
einops==0.8.2
fastapi==0.162.0
filelock==4.2.7
fla-core==0.5.2
flash-linear-attention==0.5.2
flide==0.1.7
fsspec==2026.9.0
h11==0.16.0
hf-xet==1.8.0
httpcore2==2.16.0
httpx2==2.16.0
huggingface-hub==2.4.0
idna==3.20
jinja2==3.1.6
markdown-it-py==4.2.0
markupsafe==3.0.4
mdurl==0.1.2
mpmath==1.3.0
networkx==3.7
numpy==2.5.4
nvidia-cublas==13.1.1.3
nvidia-cuda-cupti==13.0.85
nvidia-cuda-nvrtc==13.0.88
nvidia-cuda-runtime==13.0.96
nvidia-cudnn-cu13==9.24.0.43
nvidia-cufft==12.0.0.61
nvidia-cufile==1.15.1.6
nvidia-curand==10.4.0.35
nvidia-cusolver==12.0.4.66
nvidia-cusparse==12.6.3.3
nvidia-cusparselt-cu13==0.8.1
nvidia-nccl-cu13==2.30.7
nvidia-nvjitlink==13.4.92
nvidia-nvshmem-cu13==3.4.5
nvidia-nvtx==13.0.85
opentelemetry-api==1.45.2
packaging==26.3
pillow==13.0.0
psutil==7.2.2
pydantic==2.14.1
pydantic-core==2.50.1
pygments==2.21.0
pyyaml==6.0.3
regex==2026.9.29
rich==15.0.0
safetensors==0.8.0
setuptools==84.2.1
shellingham==1.5.4
starlette==1.10.0
sympy==1.14.0
tokenizers==0.23.3
torch==2.14.0
torchvision==0.29.0
tqdm==4.70.1
transformers==5.22.1
triton==3.8.0
truststore==0.10.5
typer==0.27.3
typing-extensions==4.16.0
typing-inspection==0.4.4
uvicorn==0.54.0"""


def image_install_requirements() -> list[str]:
    """Fully resolved Linux/Python 3.12 pins, mirrored in the lock file."""
    return LOCKED_REQUIREMENTS.splitlines()


def image_environment() -> dict[str, str]:
    """Non-secret environment shared by image and serve process."""
    return {
        # Also present in the image/container so importing this module inside
        # the container resolves the same explicit arm without a default.
        "MAMMOTH_FLIDE_GPU": GPU,
        "HF_HOME": HF_HOME,
        "TRITON_CACHE_DIR": TRITON_CACHE_DIR,
        "TORCHINDUCTOR_CACHE_DIR": TORCHINDUCTOR_CACHE_DIR,
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "PYTHONUNBUFFERED": "1",
    }


def build_image(modal_module: Any = modal) -> Any:
    """Build the pinned Linux image; no image is built by importing this file."""
    return (
        modal_module.Image.from_registry(BASE_IMAGE)
        .apt_install(*APT_PACKAGES)
        .pip_install(*image_install_requirements(), extra_options="--no-cache-dir")
        .env(image_environment())
    )


CACHE_VOLUME = modal.Volume.from_name(CACHE_VOLUME_NAME, create_if_missing=True)
IMAGE = build_image()


SERVER_CONFIG: dict[str, Any] = {
    "image": IMAGE,
    "gpu": GPU,  # one GPU type, selected explicitly by MAMMOTH_FLIDE_GPU
    "env": {"MAMMOTH_FLIDE_GPU": GPU},
    "volumes": {CACHE_MOUNT: CACHE_VOLUME},
    "cpu": CPU_CORES,
    "memory": MEMORY_MIB,
    "port": PORT,
    "unauthenticated": False,  # Modal proxy auth remains required
    "h2_enabled": False,
    "target_concurrency": TARGET_CONCURRENCY,
    "max_concurrency": MAX_CONCURRENCY,
    "min_containers": MIN_CONTAINERS,
    "max_containers": MAX_CONTAINERS,
    "buffer_containers": BUFFER_CONTAINERS,
    "scaledown_window": SCALEDOWN_WINDOW_S,
    "startup_timeout": STARTUP_TIMEOUT_S,
    "exit_grace_period": EXIT_GRACE_PERIOD_S,
    "routing_region": REGION,
    "compute_region": REGION,
}


def server_config_public() -> dict[str, Any]:
    """JSON-safe view of the server configuration for manifests and reports."""
    return {
        "app": APP_NAME,
        "image": BASE_IMAGE,
        "gpu": GPU,
        "routing_region": REGION,
        "compute_region": REGION,
        "port": PORT,
        "unauthenticated": False,
        "h2_enabled": False,
        "target_concurrency": TARGET_CONCURRENCY,
        "max_concurrency": MAX_CONCURRENCY,
        "min_containers": MIN_CONTAINERS,
        "max_containers": MAX_CONTAINERS,
        "buffer_containers": BUFFER_CONTAINERS,
        "scaledown_window_s": SCALEDOWN_WINDOW_S,
        "startup_timeout_s": STARTUP_TIMEOUT_S,
        "exit_grace_period_s": EXIT_GRACE_PERIOD_S,
        "cpu_cores": CPU_CORES,
        "memory_mib": MEMORY_MIB,
        "ephemeral_disk_mib": EPHEMERAL_DISK_MIB,
        "volumes": {CACHE_MOUNT: CACHE_VOLUME_NAME},
    }


def default_hf_cache() -> Path:
    """Hugging Face cache under the mounted volume, or HF_HOME if overridden."""
    return Path(os.environ.get("HF_HOME", HF_HOME)) / "hub"


def ensure_snapshot(
    *,
    cache_dir: Path | None = None,
    download: Callable[..., str] | None = None,
) -> Path:
    """Download the exact pinned revision and return its immutable snapshot path.

    ``snapshot_download`` writes the content-addressed HF cache and returns a
    path ending in the resolved commit. We deliberately do not pass
    ``local_dir`` and do not edit the snapshot, so the revision path is the
    immutable local artifact. A different resolved commit is a hard failure.
    """
    target = Path(cache_dir) if cache_dir is not None else default_hf_cache()
    target.mkdir(parents=True, exist_ok=True)
    if download is None:
        from huggingface_hub import snapshot_download

        download = snapshot_download

    path = Path(
        download(
            repo_id=MODEL_REPO,
            revision=MODEL_REVISION,
            cache_dir=str(target),
        )
    )
    if path.name != MODEL_REVISION:
        raise RuntimeError(
            f"snapshot_download resolved {path.name!r}, not the pinned revision "
            f"{MODEL_REVISION}; refusing to serve a different model"
        )
    if not path.is_dir():
        raise RuntimeError(f"pinned snapshot is not a directory: {path}")
    if not (path / "config.json").is_file():
        raise RuntimeError(f"pinned snapshot has no config.json: {path}")
    return path


def build_serve_command(snapshot: Path, *, flide_bin: str = "flide") -> list[str]:
    """Native Flide command with the pilot's fixed semantic settings.

    ``--dtype bfloat16``, ``--batch-window-ms 0``, ``--session-cache-gb 0`` and
    ``--frontends 0`` are explicit so a changing default cannot silently alter
    precision, batching, cache state, or process topology. Debias remains
    Flide's default 1 because the pilot freezes the default, not a shifted one.
    """
    path = Path(snapshot)
    if path.name != MODEL_REVISION:
        raise ValueError(
            f"refusing to serve {path.name!r}; the only allowed revision is {MODEL_REVISION}"
        )
    return [
        flide_bin,
        "serve",
        "--model",
        str(path),
        "--host",
        "0.0.0.0",
        "--port",
        str(PORT),
        "--dtype",
        "bfloat16",
        "--batch-window-ms",
        "0",
        "--session-cache-gb",
        "0",
        "--frontends",
        "0",
    ]


def serve_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for the child process; preserves inherited runtime variables."""
    env = dict(os.environ if base is None else base)
    env.update(image_environment())
    return env


def _probe_ready(base_url: str, open_url: Callable[..., Any], timeout_s: float) -> bool:
    """True only when Flide reports its model ready (no inference call).

    Flide's native ``GET /health`` returns ``status: ok`` only after the engine
    loads (with ``--frontends 0`` the app is created after loading). The route
    checks remain as a compatibility fallback: FastAPI's OpenAPI document names
    ``/v1/systemone``, and a GET to that POST-only route returning 405/422 proves
    the route exists without executing a model request.
    """
    try:
        with open_url(f"{base_url}/health", timeout=timeout_s) as response:
            if getattr(response, "status", None) == 200:
                try:
                    health = json.loads(response.read())
                except (TypeError, ValueError):
                    health = {}
                if isinstance(health, dict) and health.get("status") == "ok":
                    return True
                # The native health endpoint answered but says it is still
                # loading; a route check must not override that.
                return False
    except urllib.error.HTTPError as exc:
        # A present health endpoint that is not healthy is authoritative. Only
        # a missing endpoint (older Flide) falls through to route checks.
        if exc.code != 404:
            return False
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        pass

    try:
        with open_url(f"{base_url}/openapi.json", timeout=timeout_s) as response:
            if getattr(response, "status", None) == 200:
                try:
                    document = json.loads(response.read())
                except (TypeError, ValueError):
                    document = {}
                paths = document.get("paths") if isinstance(document, dict) else None
                if isinstance(paths, dict) and "/v1/systemone" in paths:
                    return True
    except urllib.error.HTTPError:
        pass
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        pass

    try:
        with open_url(f"{base_url}/v1/systemone", timeout=timeout_s) as response:
            return getattr(response, "status", None) in (200, 405)
    except urllib.error.HTTPError as exc:
        return exc.code in (405, 422)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


def wait_for_ready(
    process: Any,
    *,
    port: int = PORT,
    timeout_s: float = READY_TIMEOUT_S,
    urlopen: Callable[..., Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_s: float = POLL_INTERVAL_S,
    http_timeout_s: float = HTTP_TIMEOUT_S,
) -> float:
    """Block until Flide's native route responds; return measured readiness time.

    A process exit raises immediately. A timeout raises rather than allowing the
    server to accept traffic before the route is ready.
    """
    if timeout_s <= 0:
        raise TimeoutError("no startup budget remains for the Flide readiness check")
    open_url = urllib.request.urlopen if urlopen is None else urlopen
    base_url = f"http://127.0.0.1:{port}"
    started = monotonic()
    while True:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"flide serve exited during startup with status {code}")
        if _probe_ready(base_url, open_url, http_timeout_s):
            return monotonic() - started
        if monotonic() - started >= timeout_s:
            raise TimeoutError(
                f"flide serve did not expose /v1/systemone within {timeout_s:.0f}s"
            )
        sleep(poll_interval_s)


def stop_process(process: Any, *, grace_s: float = EXIT_GRACE_PERIOD_S) -> None:
    """Terminate then kill the child process; safe to call more than once."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def require_cuda(gpu: str, torch_module: Any | None = None) -> dict[str, Any]:
    """Fail unless CUDA is available and the assigned device matches the arm."""
    if torch_module is None:
        import torch as torch_module

    if not torch_module.cuda.is_available():
        raise RuntimeError(
            f"CUDA is not available in the {gpu} container; refusing to continue"
        )
    device_name = str(torch_module.cuda.get_device_name(0))
    if gpu.upper() not in device_name.upper():
        raise RuntimeError(
            f"requested {gpu} but Modal assigned {device_name!r}; "
            "refusing to serve on different hardware"
        )
    properties = torch_module.cuda.get_device_properties(0)
    return {
        "device_name": device_name,
        "capability": f"{getattr(properties, 'major', '?')}.{getattr(properties, 'minor', '?')}",
        "memory_mib": int(properties.total_memory // (1024 * 1024)),
        "cuda_version": getattr(getattr(torch_module, "version", None), "cuda", None),
    }


def driver_facts(run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Best-effort driver facts; missing measurements remain None."""
    runner = subprocess.run if run is None else run
    unknown = {"name": None, "driver_version": None, "memory_total": None}
    try:
        completed = runner(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return unknown
    if getattr(completed, "returncode", 1) != 0:
        return unknown
    output = getattr(completed, "stdout", "") or ""
    line = next((row.strip() for row in output.splitlines() if row.strip()), "")
    if not line:
        return unknown
    parts = [part.strip() for part in line.split(",")]
    return {
        "name": parts[0] or None,
        "driver_version": parts[1] if len(parts) > 1 and parts[1] else None,
        "memory_total": parts[2] if len(parts) > 2 and parts[2] else None,
    }


def snapshot_file_hashes(
    snapshot: Path, names: Sequence[str] = ("config.json", "readout.json")
) -> dict[str, str | None]:
    """SHA256 of small checkpoint metadata files; absent files stay unknown."""
    hashes: dict[str, str | None] = {}
    for name in names:
        path = Path(snapshot) / name
        if not path.is_file():
            hashes[name] = None
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[name] = digest.hexdigest()
    return hashes


def actual_versions(
    version_fn: Callable[[str], str] | None = None,
    names: Sequence[str] = REPORT_PACKAGES,
) -> dict[str, str | None]:
    """Installed distribution versions; a missing package is None, not a guess."""
    lookup = importlib.metadata.version if version_fn is None else version_fn
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = lookup(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def verify_pins(version_fn: Callable[[str], str] | None = None) -> dict[str, str | None]:
    """Fail fast if any direct image pin has drifted; return the measured set."""
    actual = actual_versions(version_fn, names=tuple(PIP_PINS))
    mismatches = [
        f"{name}: expected {expected}, got {actual[name]!r}"
        for name, expected in PIP_PINS.items()
        if actual[name] != expected
    ]
    if mismatches:
        raise RuntimeError("installed image does not match the frozen pins: " + "; ".join(mismatches))
    return actual


def runtime_report(
    *,
    snapshot: Path,
    gpu: str,
    gpu_facts: Mapping[str, Any],
    ready_s: float | None,
    versions: Mapping[str, str | None],
    driver: Mapping[str, Any] | None,
    file_hashes: Mapping[str, str | None] | None = None,
    startup_elapsed_s: float | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """One JSON-safe startup record for the parent's compact run record."""
    path = Path(snapshot)
    if path.name != MODEL_REVISION:
        raise ValueError(f"report received non-pinned snapshot {path.name!r}")
    return {
        "report_version": 1,
        "generated_at_utc": generated_at or datetime.now(timezone.utc).isoformat(),
        "app": APP_NAME,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "model": {
            "name": MODEL_NAME,
            "repo": MODEL_REPO,
            "revision": MODEL_REVISION,
            "snapshot": str(path),
            "files": dict(file_hashes if file_hashes is not None else snapshot_file_hashes(path)),
        },
        "pins": dict(PIP_PINS),
        "packages": {name: versions.get(name) for name in REPORT_PACKAGES},
        "image": {
            "base": BASE_IMAGE,
            "base_digest": BASE_IMAGE_DIGEST,
            "modal_image_id": None,  # unknown until Modal builds it; parent records it
        },
        "gpu": {
            "requested": gpu,
            "actual": dict(gpu_facts),
            "driver": dict(driver) if driver is not None else None,
        },
        "server": server_config_public(),
        "auth": {"mode": "modal-proxy", "unauthenticated": False},
        "startup": {
            "flide_route_ready_s": ready_s,
            "startup_elapsed_s": startup_elapsed_s,
            "ready_timeout_s": READY_TIMEOUT_S,
            "startup_timeout_s": STARTUP_TIMEOUT_S,
            "readiness_probe": "GET /health status=ok, then native route checks, before enter returns",
        },
        "cache": {
            "volume": CACHE_VOLUME_NAME,
            "mount": CACHE_MOUNT,
            "hf_home": HF_HOME,
            "triton": TRITON_CACHE_DIR,
        },
        "notes": [
            "Native Flide /v1/systemone only; no image, state, or game integration.",
            "No S1 choice was selected, ranked, or executed by this server.",
            "Download and kernel compilation are startup work, not client inference requests.",
        ],
    }


def emit_report(report: Mapping[str, Any]) -> None:
    """Write a single compact line; Modal logs carry it, no secret is included."""
    print(REPORT_PREFIX + json.dumps(report, sort_keys=True, separators=(",", ":")), flush=True)


def static_manifest() -> dict[str, Any]:
    """Frozen, JSON-safe description that can be read without any cloud call."""
    return {
        "report_version": 1,
        "status": "offline prototype; not deployed, built, or invoked",
        "cloud_actions": "none",
        "app": APP_NAME,
        "pilot": {
            "ceiling_usd": 20,
            "flide_arms": list(GPU_CHOICES),
            "flide_requests_per_arm": 15,
            "total_requests_with_onejev_reference": 63,
            "budget_check_required": True,
            "note": "operator stop, not a provider-enforced cap",
        },
        "model": {"name": MODEL_NAME, "repo": MODEL_REPO, "revision": MODEL_REVISION},
        "base_image": BASE_IMAGE,
        "base_image_digest": BASE_IMAGE_DIGEST,
        "package_pins": dict(PIP_PINS),
        "package_requirements": image_install_requirements(),
        "server": server_config_public(),
        "command_template": [
            "flide",
            "serve",
            "--model",
            f"<snapshot>/{MODEL_REVISION}",
            "--host",
            "0.0.0.0",
            "--port",
            str(PORT),
            "--dtype",
            "bfloat16",
            "--batch-window-ms",
            "0",
            "--session-cache-gb",
            "0",
            "--frontends",
            "0",
        ],
        "cache": {"volume": CACHE_VOLUME_NAME, "mount": CACHE_MOUNT},
        "teardown": (
            "Parent stops the app and deletes the cache volume after recording; "
            "this module performs no teardown and no scheduled action."
        ),
        "constraints": [
            "one GPU type per deployment; no fallback",
            "one arm deployed at a time; fixed app name so arms replace rather than overlap",
            "max_containers=1, min_containers=0, target_concurrency=1, max_concurrency=1",
            "Modal proxy authentication required; unauthenticated=False",
            "routing_region=us-east and compute_region=us-east",
            "native Flide API only; no game integration",
        ],
    }


APP = modal.App(APP_NAME)


@APP.server(**SERVER_CONFIG)
class MammothFlideServer:
    """Starts the pinned Flide process and blocks entry until its route is live."""

    process: subprocess.Popen | None = None
    snapshot: Path | None = None
    ready_s: float | None = None

    @modal.enter()
    def start(self) -> None:
        started = time.monotonic()
        verify_pins()
        gpu_facts = require_cuda(GPU)

        self.snapshot = ensure_snapshot()
        # Persist the content-addressed HF snapshot before any process starts.
        CACHE_VOLUME.commit()

        remaining = READY_TIMEOUT_S - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("startup budget exhausted before flide serve was launched")

        flide = shutil.which("flide")
        if flide is None:
            raise RuntimeError("flide executable is not on PATH in the pinned image")

        try:
            self.process = subprocess.Popen(
                build_serve_command(self.snapshot, flide_bin=flide),
                env=serve_env(),
                stdout=sys.stdout,
                stderr=subprocess.STDOUT,
            )
            self.ready_s = wait_for_ready(self.process, timeout_s=remaining)
            report = runtime_report(
                snapshot=self.snapshot,
                gpu=GPU,
                gpu_facts=gpu_facts,
                ready_s=self.ready_s,
                versions=actual_versions(),
                driver=driver_facts(),
                file_hashes=snapshot_file_hashes(self.snapshot),
                startup_elapsed_s=time.monotonic() - started,
            )
            emit_report(report)
        except BaseException:
            stop_process(self.process)
            self.process = None
            raise

    @modal.exit()
    def stop(self) -> None:
        stop_process(self.process)
        self.process = None


if __name__ == "__main__":
    print(json.dumps(static_manifest(), indent=2, sort_keys=True))
