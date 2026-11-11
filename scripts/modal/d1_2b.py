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

With `D1_2B_ADAPTER=<run id>`, the same server deploys as its own app, `d1-2b-s1-lora`. It loads the LoRA adapter
that decision-lab saved at `/runs/lora/<run id>/adapter` on the `decision-lab-runs` Volume and merges it into the
plain linears before d1 fuses them, as W += (alpha / r) B A. `/health` and each reply's `model` name the adapter
and its hash, so every recorded call says which weights answered. The adapter changes the weights only; the
server still returns d1-2B's answer unchanged.

    D1_2B_GPU=A10 D1_2B_ADAPTER=<run id> modal deploy scripts/modal/d1_2b.py
    modal app stop d1-2b-s1-lora
"""

import os
import re
from pathlib import Path

import modal

ADAPTER = os.environ.get("D1_2B_ADAPTER", "").strip()
if ADAPTER and not re.fullmatch(r"[0-9a-f]{32}", ADAPTER):
    raise ValueError("D1_2B_ADAPTER must be a decision-lab LoRA run id (32 hex characters)")
APP_NAME = "d1-2b-s1-lora" if ADAPTER else "d1-2b-s1"
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
         .env({"D1_2B_GPU": GPU, "D1_2B_IDLE_S": str(IDLE_S), "D1_2B_ADAPTER": ADAPTER, "HF_HOME": f"{CACHE_MOUNT}/hf",
               "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false", "PYTHONUNBUFFERED": "1"})
         .add_local_file(SERVER_FILE, "/root/d1_server.py"))
CACHE_VOLUME = modal.Volume.from_name("d1-2b-s1-weights", create_if_missing=True)
RUNS_MOUNT = "/runs"
VOLUMES = {CACHE_MOUNT: CACHE_VOLUME}
if ADAPTER:  # decision-lab's runs Volume, read only: the server never writes to it
    VOLUMES[RUNS_MOUNT] = modal.Volume.from_name("decision-lab-runs").read_only()
app = modal.App(APP_NAME)


def merge_lora(model, directory: Path) -> dict:
    """Add a saved peft LoRA adapter into the model's plain linears; return what was merged."""
    import hashlib
    import json

    import torch
    from safetensors.torch import load_file

    config = json.loads((directory / "adapter_config.json").read_text(encoding="utf-8"))
    if (config.get("peft_type") != "LORA" or config.get("use_dora") or config.get("rank_pattern")
            or config.get("alpha_pattern") or config.get("lora_bias")):
        raise ValueError("only a plain LoRA adapter can be merged")
    scale = config["lora_alpha"] / config["r"]
    weights = directory / "adapter_model.safetensors"
    tensors = load_file(str(weights))
    params = dict(model.named_parameters())
    suffix_a, suffix_b = ".lora_A.weight", ".lora_B.weight"
    bases = sorted(key[: -len(suffix_a)] for key in tensors if key.endswith(suffix_a))
    if not bases or len(tensors) != 2 * len(bases) or any(b + suffix_b not in tensors for b in bases):
        raise ValueError("the adapter's A and B weights do not pair up")
    with torch.no_grad():
        for base in bases:
            weight = params[base + ".weight"]  # KeyError if the adapter names a linear this model lacks
            delta = tensors[base + suffix_b].float() @ tensors[base + suffix_a].float()
            weight.copy_((weight.float() + scale * delta.to(weight.device)).to(weight.dtype))
    return {"linears": len(bases), "scale": scale, "r": config["r"],
            "sha256": hashlib.sha256(weights.read_bytes()).hexdigest()}


@app.cls(image=IMAGE, gpu=GPU, volumes=VOLUMES, cpu=2.0, memory=16384, min_containers=0,
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
        merged = {}
        prepare = None
        if ADAPTER:
            def prepare(model):
                merged.update(merge_lora(model, Path(RUNS_MOUNT) / "lora" / ADAPTER / "adapter"))
        self.model = d1_server.load(d1_server.REVISION, chunk=1024, prepare=prepare)
        CACHE_VOLUME.commit()
        # `load` routes d1 to its masked SDPA path only when this build lacks flash attention.
        attention = "fused" if torch.backends.cuda.is_flash_attention_available() else "sdpa_masked(chunk=1024)"
        # The reply's `model` is recorded as each call's resolved model, so it names the adapter (at most 64).
        self.model_name = (f"d1-2b@{d1_server.REVISION[:8]}+lora-{ADAPTER[:8]}-{merged['sha256'][:8]}"
                           if ADAPTER else None)
        self.info = {"status": "ok", "model": f"d1-2b@{d1_server.REVISION}", "gpu": GPU,
                     "device": torch.cuda.get_device_name(0), "attention": attention, "idle_s": IDLE_S}
        if ADAPTER:
            self.info.update(adapter=ADAPTER, adapter_merge=merged, resolved_model=self.model_name)
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
                if self.model_name:
                    result["model"] = self.model_name
            except (ValueError, KeyError, TypeError) as exc:
                return JSONResponse({"error": str(exc)[:200]}, status_code=400)
            except Exception as exc:  # a model failure is the caller's provider failure, not a server crash
                return JSONResponse({"error": type(exc).__name__}, status_code=500)
            return JSONResponse(result)

        return api
