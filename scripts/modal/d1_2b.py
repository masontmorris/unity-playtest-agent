"""Liquid's open d1-2B on Modal: the server behind the `modal-d1-2b` S1 arm.

It is scripts/local/d1_server.py's model behind a Modal web endpoint instead of on this machine's GPU, so the
game does not share a card with S1. The local server's own `load` and `answer` run inside the container,
unchanged, so both arms decide the same way: the body is `{"model", "questions", "state", "images"}`, the
questions are answered in the order they arrive, `state` is passed as received, and the reply is
`{"model", "answers", "usage"}` with `usage.cost` 0. Modal bills container time, so a run's `cost_usd` stays null.

Project laws kept here: the server never selects, ranks, substitutes or suppresses an S1 choice; it returns
d1-2B's answer unchanged. It knows nothing about any game. It reads no credential and logs none; Modal proxy
authentication guards the endpoint, with the same workspace proxy token the `mammoth-1-modal` arm sends.

On Linux the PyTorch build has flash attention, so `load` keeps d1's fused attention instead of the Windows
unfused path; `/health` reports which attention path is in use, so a run can state it.

Deploy (the GPU and idle window are read at deploy time and baked into the image environment), then stop:

    D1_2B_GPU=A10 modal deploy scripts/modal/d1_2b.py
    modal app stop d1-2b-s1

The weights stay in the `d1-2b-s1-weights` Volume between starts. The app scales to zero after `D1_2B_IDLE_S`
idle seconds (default 300).
"""

import os
from pathlib import Path

import modal

APP_NAME = "d1-2b-s1"
GPU_CHOICES = ("A10", "L4", "L40S", "A100", "H100")
GPU = os.environ.get("D1_2B_GPU", "A10").strip().upper()
if GPU not in GPU_CHOICES:
    raise ValueError(f"D1_2B_GPU must be one of {', '.join(GPU_CHOICES)}")
IDLE_S = int(os.environ.get("D1_2B_IDLE_S", "300"))
if not 60 <= IDLE_S <= 3600:
    raise ValueError("D1_2B_IDLE_S must be between 60 and 3600 seconds")

CACHE_MOUNT = "/cache"
SERVER_FILE = Path(__file__).resolve().parents[1] / "local" / "d1_server.py"

IMAGE = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("torch==2.11.0", "torchvision==0.26.0", index_url="https://download.pytorch.org/whl/cu128")
         .pip_install("transformers==5.19.0", "safetensors", "pillow", "huggingface_hub[hf_xet]", "fastapi")
         .env({"D1_2B_GPU": GPU, "D1_2B_IDLE_S": str(IDLE_S), "HF_HOME": f"{CACHE_MOUNT}/hf",
               "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false", "PYTHONUNBUFFERED": "1"})
         .add_local_file(SERVER_FILE, "/root/d1_server.py"))
CACHE_VOLUME = modal.Volume.from_name("d1-2b-s1-weights", create_if_missing=True)
app = modal.App(APP_NAME)


@app.cls(image=IMAGE, gpu=GPU, volumes={CACHE_MOUNT: CACHE_VOLUME}, cpu=2.0, memory=16384, min_containers=0,
         max_containers=1, buffer_containers=0, scaledown_window=IDLE_S, timeout=600, startup_timeout=1200)
class D1Server:
    @modal.enter()
    def start(self) -> None:
        import sys

        import torch

        sys.path.insert(0, "/root")
        import d1_server

        if not torch.cuda.is_available():
            raise RuntimeError("no CUDA device")
        self.server = d1_server
        self.model = d1_server.load(d1_server.REVISION, chunk=1024)
        CACHE_VOLUME.commit()
        # `load` routes d1 to its masked SDPA path only when this build lacks flash attention.
        attention = "fused" if torch.backends.cuda.is_flash_attention_available() else "sdpa_masked(chunk=1024)"
        self.info = {"status": "ok", "model": f"d1-2b@{d1_server.REVISION}", "gpu": GPU,
                     "device": torch.cuda.get_device_name(0), "attention": attention, "idle_s": IDLE_S}
        print(self.info, flush=True)

    @modal.asgi_app(requires_proxy_auth=True)
    def web(self):
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        api = FastAPI(title="d1-2B S1", docs_url=None, redoc_url=None, openapi_url=None)

        @api.get("/health")
        def health() -> JSONResponse:
            return JSONResponse(self.info)

        @api.post("/v1/systemone")
        async def systemone(request: Request) -> JSONResponse:
            import json

            import anyio

            try:
                body = json.loads(await request.body())
                result = await anyio.to_thread.run_sync(lambda: self.server.answer(self.model, body))
            except (ValueError, KeyError, TypeError) as exc:
                return JSONResponse({"error": str(exc)[:200]}, status_code=400)
            except Exception as exc:  # a model failure is the caller's provider failure, not a server crash
                return JSONResponse({"error": type(exc).__name__}, status_code=500)
            return JSONResponse(result)

        return api
