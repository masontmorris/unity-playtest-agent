"""Mammoth-1 on Modal: the server behind the `mammoth-1-modal` S1 arm.

Flide 0.1.7 serves `baselabs/mammoth-1` at a pinned revision in BF16 on one B200 (or H200), the
configuration the 2 November pilot measured and checked against the published OneJev runtime
(`docs/probes/2026-11-02-mammoth-modal-fast-pilot.md`). Two things are added around Flide:

* **The `images` transport.** The S1 adapter sends images as a top-level `images` array of data URLs,
  the transport the Liquid arms take. Flide ignores that key and wants `media` entries with an
  `<image:N>` placeholder in `state`. A small proxy on the routed port rewrites one into the other
  exactly as the pilot's verified bodies were built: `state` becomes `{"state": <state>, "images":
  ["<image:1>", ...]}`, in that order, and every other value is passed through in the order received.
  A body without images is forwarded byte for byte.
* **Planner warmup.** Flide's planning threads each initialize an image processor on their first image,
  which cost about one second per thread in the pilot. Before the container takes traffic, a synthetic
  text request and rounds of four concurrent synthetic image requests run until a whole round plans in
  under `WARM_PLAN_S`. The result is logged; a container that never warms still serves, slower.

Project laws kept here: the server never selects, ranks, substitutes or suppresses an S1 choice; it
returns Flide's answer unchanged. It knows nothing about any game. It reads no credential and logs none;
Modal proxy authentication guards the endpoint. No cost is reported per call: Modal bills container
time, so a run's `cost_usd` stays null.

Deploy (the GPU and idle window are read at deploy time and baked into the image environment):

    MAMMOTH_FLIDE_GPU=B200 modal deploy scripts/modal/mammoth_flide.py

The weights stay in the `mammoth-1-flide-weights` Volume between starts, so only the first start
downloads them. The app scales to zero after `MAMMOTH_FLIDE_IDLE_S` idle seconds (default 300).
"""

# No `from __future__ import annotations`: FastAPI must resolve the proxy's locally imported Request
# type at definition time, or it reads the parameter as a required query field and answers 422.
import concurrent.futures as cf
import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import modal

APP_NAME = "mammoth-1-flide"
SERVER_NAME = "MammothFlideServer"
MODEL_REPO = "baselabs/mammoth-1"
MODEL_REVISION = "61cec44c281c693992e3d6ebeb44dd3402dfa186"
MODEL_NAME = "mammoth-1"
FLIDE_VERSION = "0.1.7"

GPU_CHOICES = ("B200", "H200")
GPU = os.environ.get("MAMMOTH_FLIDE_GPU", "B200").strip().upper()
if GPU not in GPU_CHOICES:
    raise ValueError(f"MAMMOTH_FLIDE_GPU must be one of {', '.join(GPU_CHOICES)}")
IDLE_S = int(os.environ.get("MAMMOTH_FLIDE_IDLE_S", "300"))
if not 60 <= IDLE_S <= 3600:
    raise ValueError("MAMMOTH_FLIDE_IDLE_S must be between 60 and 3600 seconds")

BASE_IMAGE_DIGEST = "sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af"
BASE_IMAGE = f"python:3.12.13-slim-bookworm@{BASE_IMAGE_DIGEST}"
APT_PACKAGES = ("ca-certificates", "curl", "libgomp1", "libgl1", "libglib2.0-0")
PIP_PINS = {"torch": "2.14.0", "torchvision": "0.29.0", "flide": FLIDE_VERSION, "transformers": "5.22.1",
            "huggingface-hub": "2.4.0", "flash-linear-attention": "0.5.2", "accelerate": "1.15.0",
            "fastapi": "0.162.0", "uvicorn": "0.54.0", "pillow": "13.0.0", "pydantic": "2.14.1"}

CACHE_VOLUME_NAME = "mammoth-1-flide-weights"
CACHE_MOUNT = "/cache"
HF_HOME = "/cache/hf"

PORT = 8000  # the routed port: the images proxy
FLIDE_PORT = 8001  # Flide itself, reachable only inside the container
REGION = "us-east"
MAX_CONCURRENCY = 8  # two or more S1 sessions may share the container; Flide queues their rounds
STARTUP_TIMEOUT_S = 1800
READY_TIMEOUT_S = STARTUP_TIMEOUT_S - 300
PLANNERS = 4  # Flide's default, as measured
WARM_ROUNDS = 4
WARM_PLAN_S = 0.3  # a planner initializing its image processor took about 1 s; a warm one 6-20 ms
# Filler words for text warmups at about these prompt sizes: the first real 4k-token call after a cold start
# took 6.9 s when only a tiny prompt had been warmed, so every size S1 sends is run once first.
WARM_TEXT_WORDS = (1000, 4000, 8000, 15000)
WARM_IMAGE_WORDS = 6000  # image warmups at a game-sized prompt, not a tiny one
MAX_IMAGES = 4
REPORT_PREFIX = "MAMMOTH_FLIDE_REPORT "

# The pilot's resolved Linux/Python 3.12 lock (scripts/probes/mammoth_flide_requirements.txt); a test keeps
# the two identical. Embedded because Modal mounts only this file into the container.
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


# --- the images transport -------------------------------------------------------------------------------


class BadRequest(ValueError):
    """A request the proxy refuses before Flide sees it (answered 422)."""


def _dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def translate(raw: bytes) -> bytes:
    """Flide's body for a request in the `images` transport; a body without images is returned as is.

    The top-level keys are written sorted, as the pilot's verified bodies were; every value, including
    `state` and the order of its own keys, is passed through as received. `state` is wrapped with one
    `<image:N>` placeholder per image after it, the order the OneJev reference adapter builds.
    """
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("request body is not JSON") from None
    if not isinstance(body, dict):
        raise BadRequest("request body must be a JSON object")
    if "media" in body:
        raise BadRequest("send images in the top-level images array, not media")
    if "images" not in body:
        return raw
    images = body["images"]
    if not isinstance(images, list) or not all(isinstance(url, str) and url.startswith("data:image/")
                                               for url in images):
        raise BadRequest("images must be a list of data:image/... URLs")
    if len(images) > MAX_IMAGES:
        raise BadRequest(f"at most {MAX_IMAGES} images")
    out = {key: value for key, value in body.items() if key != "images"}
    if images:
        out["state"] = {"state": body.get("state"),
                        "images": [f"<image:{index + 1}>" for index in range(len(images))]}
        out["media"] = [{"data": url, "type": "image"} for url in images]  # keys sorted, as verified
    return _dumps({key: out[key] for key in sorted(out)})


# --- warmup ---------------------------------------------------------------------------------------------


def synthetic_jpeg(width: int = 640, height: int = 480) -> bytes:
    """A flat gray JPEG at the game view's size: it initializes a planner and shows nothing."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (96, 96, 96)).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def warmup_body(image: bytes | None, words: int = 0) -> bytes:
    """A synthetic two-option request, padded with `words` filler words; its answer is discarded and never
    reaches a game."""
    import base64

    questions = {"next_action": {"type": "choice", "instructions": "Warmup request; pick either option.",
                                 "criteria": {"first": "the first option", "second": "the second option"}}}
    state: dict[str, Any] = {"note": "server warmup"}
    if words:
        state["filler"] = " ".join(["the"] * words)
    body: dict[str, Any] = {"model": MODEL_NAME, "questions": questions, "state": state}
    if image is not None:
        body["images"] = ["data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")]
    return translate(_dumps(body))


def plan_seconds(response: Any) -> float | None:
    """Flide's reported planning time for one answer, or None when it reported none."""
    timing = ((response or {}).get("onejev") or {}).get("timing") if isinstance(response, dict) else None
    value = timing.get("plan_s") if isinstance(timing, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def input_tokens(response: Any) -> int | None:
    usage = response.get("usage") if isinstance(response, dict) else None
    value = usage.get("input_tokens") if isinstance(usage, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def warm_planners(post: Callable[[bytes], Any], image: bytes, *, planners: int = PLANNERS,
                  rounds: int = WARM_ROUNDS, threshold_s: float = WARM_PLAN_S,
                  text_words: tuple[int, ...] = WARM_TEXT_WORDS) -> dict[str, Any]:
    """Run each prompt size once, warm every planning thread with an image, and report what was measured.

    Flide's planners are a pool of `planners` threads started together. A round sends `planners` image
    requests at once; a cold thread is busy for about a second, so a round's requests land on distinct
    threads. The pool is warm when a whole round plans under `threshold_s`. A missing timing is unknown
    and keeps the pool unproven.
    """
    report: dict[str, Any] = {"text_plan_s": plan_seconds(post(warmup_body(None))), "text_sizes": [],
                              "rounds": [], "warm": False}
    for words in text_words:
        started = time.monotonic()
        tokens = input_tokens(post(warmup_body(None, words)))
        report["text_sizes"].append({"tokens": tokens, "seconds": round(time.monotonic() - started, 3)})
    body = warmup_body(image, WARM_IMAGE_WORDS if text_words else 0)
    with cf.ThreadPoolExecutor(max_workers=planners) as pool:
        for _ in range(rounds):
            seconds = list(pool.map(lambda _: plan_seconds(post(body)), range(planners)))
            report["rounds"].append(seconds)
            if all(value is not None and value < threshold_s for value in seconds):
                report["warm"] = True
                break
    return report


# --- the proxy ------------------------------------------------------------------------------------------


def _forward(method: str, path: str, data: bytes | None = None, timeout: float = 120.0) -> tuple[int, bytes, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{FLIDE_PORT}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def make_proxy(ready: threading.Event) -> Any:
    """The routed app: `/v1/systemone` in the images transport, `/health` and `/v1/models` passed through."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response

    app = FastAPI(title="mammoth-1 (Flide) images proxy", docs_url=None, redoc_url=None, openapi_url=None)

    def passthrough(status: int, data: bytes, headers: dict) -> Response:
        keep = {key: value for key, value in headers.items() if key.lower() == "x-decision-inference-ms"}
        return Response(content=data, status_code=status, headers=keep,
                        media_type=headers.get("content-type", "application/json"))

    @app.get("/health")
    def health() -> Response:
        if not ready.is_set():
            return JSONResponse({"status": "loading", "model": MODEL_NAME}, status_code=503)
        return passthrough(*_forward("GET", "/health", timeout=10))

    @app.get("/v1/models")
    def models() -> Response:
        return passthrough(*_forward("GET", "/v1/models", timeout=10))

    @app.post("/v1/systemone")
    async def systemone(request: Request) -> Response:
        raw = await request.body()
        try:
            body = translate(raw)
        except BadRequest as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        import anyio

        return passthrough(*await anyio.to_thread.run_sync(lambda: _forward("POST", "/v1/systemone", body)))

    return app


def _serve_proxy(app: Any) -> None:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning", access_log=False))
    threading.Thread(target=server.run, name="images-proxy", daemon=True).start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("the images proxy did not start listening")


# --- container lifecycle --------------------------------------------------------------------------------


def serve_command(snapshot: Path, flide_bin: str) -> list[str]:
    """Flide's serve command with the pilot's settings stated, so a changed default cannot alter them.

    No session cache: Mammoth reads its prefix bidirectionally, so no state from an earlier turn is
    reusable, and Flide refuses session caching for such plans anyway.
    """
    if Path(snapshot).name != MODEL_REVISION:
        raise ValueError(f"refusing to serve {Path(snapshot).name!r}; only {MODEL_REVISION} is allowed")
    return [flide_bin, "serve", "--model", str(snapshot), "--host", "127.0.0.1", "--port", str(FLIDE_PORT),
            "--dtype", "bfloat16", "--batch-window-ms", "0", "--session-cache-gb", "0", "--frontends", "0",
            "--planners", str(PLANNERS)]


def verify_pins() -> None:
    mismatches = []
    for name, expected in PIP_PINS.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        if actual != expected:
            mismatches.append(f"{name}: expected {expected}, got {actual!r}")
    if mismatches:
        raise RuntimeError("installed image does not match the pins: " + "; ".join(mismatches))


def ensure_snapshot() -> Path:
    from huggingface_hub import snapshot_download

    path = Path(snapshot_download(repo_id=MODEL_REPO, revision=MODEL_REVISION, cache_dir=f"{HF_HOME}/hub"))
    if path.name != MODEL_REVISION or not (path / "config.json").is_file():
        raise RuntimeError(f"the pinned snapshot did not resolve to {MODEL_REVISION}")
    return path


def wait_for_flide(process: subprocess.Popen, timeout_s: float) -> float:
    started = time.monotonic()
    while True:
        if process.poll() is not None:
            raise RuntimeError(f"flide serve exited during startup with status {process.returncode}")
        try:
            status, data, _ = _forward("GET", "/health", timeout=2)
            if status == 200 and json.loads(data).get("status") == "ok":
                return time.monotonic() - started
        except (OSError, ValueError):
            pass
        if time.monotonic() - started >= timeout_s:
            raise TimeoutError(f"flide serve was not ready within {timeout_s:.0f}s")
        time.sleep(2)


def image_environment() -> dict[str, str]:
    return {"MAMMOTH_FLIDE_GPU": GPU, "MAMMOTH_FLIDE_IDLE_S": str(IDLE_S), "HF_HOME": HF_HOME,
            "TRITON_CACHE_DIR": "/cache/triton", "TORCHINDUCTOR_CACHE_DIR": "/cache/torchinductor",
            "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false", "PYTHONUNBUFFERED": "1"}


IMAGE = (modal.Image.from_registry(BASE_IMAGE)
         .apt_install(*APT_PACKAGES)
         .pip_install(*LOCKED_REQUIREMENTS.splitlines(), extra_options="--no-cache-dir")
         .env(image_environment()))
CACHE_VOLUME = modal.Volume.from_name(CACHE_VOLUME_NAME, create_if_missing=True)
app = modal.App(APP_NAME)


@app.server(image=IMAGE, gpu=GPU, volumes={CACHE_MOUNT: CACHE_VOLUME}, cpu=4.0, memory=65536, port=PORT,
            unauthenticated=False, h2_enabled=False, target_concurrency=MAX_CONCURRENCY,
            max_concurrency=MAX_CONCURRENCY, min_containers=0, max_containers=1, buffer_containers=0,
            scaledown_window=IDLE_S, startup_timeout=STARTUP_TIMEOUT_S, exit_grace_period=30,
            routing_region=REGION, compute_region=REGION)
class MammothFlideServer:
    """Starts Flide, warms its planners, then opens the images proxy on the routed port."""

    process: subprocess.Popen | None = None

    @modal.enter()
    def start(self) -> None:
        import torch

        started = time.monotonic()
        verify_pins()
        if not torch.cuda.is_available() or GPU not in torch.cuda.get_device_name(0).upper():
            raise RuntimeError(f"requested {GPU}; refusing to serve on other hardware")
        snapshot = ensure_snapshot()
        CACHE_VOLUME.commit()
        flide = shutil.which("flide")
        if flide is None:
            raise RuntimeError("flide is not on PATH in the pinned image")
        self.process = subprocess.Popen(serve_command(snapshot, flide), stdout=sys.stdout, stderr=subprocess.STDOUT)
        try:
            ready_s = wait_for_flide(self.process, READY_TIMEOUT_S - (time.monotonic() - started))
            warm = warm_planners(lambda body: json.loads(_forward("POST", "/v1/systemone", body)[1]),
                                 synthetic_jpeg())
            ready = threading.Event()
            ready.set()
            _serve_proxy(make_proxy(ready))
        except BaseException:
            self.stop()
            raise
        config = hashlib.sha256((snapshot / "config.json").read_bytes()).hexdigest()
        print(REPORT_PREFIX + json.dumps({
            "app": APP_NAME, "gpu": GPU, "device": torch.cuda.get_device_name(0), "flide": FLIDE_VERSION,
            "model": {"repo": MODEL_REPO, "revision": MODEL_REVISION, "config_sha256": config},
            "flide_ready_s": round(ready_s, 1), "startup_s": round(time.monotonic() - started, 1),
            "warmup": warm, "idle_s": IDLE_S}, sort_keys=True), flush=True)

    @modal.exit()
    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.kill()
