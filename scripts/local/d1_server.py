"""Liquid's open d1-2B on this machine: the server behind the `local-d1-2b` S1 arm.

It speaks Liquid's decisions API, the body the Liquid arms post: `{"model", "questions", "state", "images"}`. The
questions are answered in the order they arrive (canonical JSON, so sorted), `state` is passed as received, and
`images[0]` (a JPEG data URL) is the state's one picture. The reply is Liquid's shape: `{"model", "answers",
"usage"}`, with `usage.cost` 0, since nothing is billed. Requests are served one at a time, on one GPU.

It needs its own environment, not the repository's: `torch` (CUDA), `transformers>=5.19` and `pillow`. Start it
with the token the arm will send in `LOCAL_D1_KEY`, then point `LOCAL_D1_URL` at it:

    LOCAL_D1_KEY=<any token> python scripts/local/d1_server.py --port 8765

On a PyTorch build without flash attention (the Windows wheels), d1's own unfused path is used: masked SDPA in
`--chunk` queries at a time, the same function. The CUDA cache is emptied before and after each request: prompts
of varying length otherwise fragment it past a 12 GB card and the driver spills to host memory, and between
requests the game being played renders on the same card.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import io
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import torch
from PIL import Image
from transformers import AutoModel

REPO_ID = "LiquidAI/d1-2B"
REVISION = "652f920e544ea37f81e4b0ce89b81d4ed0da90b1"  # the checkpoint every local-d1-2b run so far used


def load(revision: str, chunk: int):
    model = AutoModel.from_pretrained(REPO_ID, revision=revision, trust_remote_code=True,
                                      dtype=torch.bfloat16).to("cuda")
    if not torch.backends.cuda.is_flash_attention_available():
        for name, module in list(sys.modules.items()):
            if "LiquidAI" in name and name.endswith(".attention"):
                module._fused = lambda q: False
                module.CHUNK = chunk
    model.engine  # builds the runner (tokenizer, calibration) before the first request
    return model


def image(url: str) -> Image.Image:
    head, _, data = url.partition(",")
    if not head.startswith("data:image/") or ";base64" not in head:
        raise ValueError("an image must be a base64 data URL")
    return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")


def answer(model, body: dict) -> dict:
    questions, state = body.get("questions"), body.get("state")
    if not isinstance(questions, dict) or not questions:
        raise ValueError("the body has no questions")
    media = [image(u) for u in body.get("images") or []]
    torch.cuda.empty_cache()
    try:
        out = model.system_one(state, questions, media=media or None)
    finally:
        torch.cuda.empty_cache()  # hand the card back between requests: the game renders on it too
    answers = {name: {"type": questions[name].get("type"), **reply} for name, reply in out["answers"].items()}
    usage = {"input_tokens": out["usage"]["input_tokens"], "output_tokens": 0, "cost": 0.0}
    return {"model": f"d1-2b@{model.d1_revision or REVISION}"[:64], "answers": answers, "usage": usage}


def serve(model, token: str, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, as the arm's sender pools connections

        def _reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return self._reply(401, {"error": "unauthorized"})
            started = time.monotonic()
            try:
                result = answer(model, json.loads(raw))
            except (ValueError, KeyError, TypeError) as exc:
                return self._reply(400, {"error": str(exc)[:200]})
            except Exception as exc:  # a model failure is the caller's provider failure, not a server crash
                return self._reply(500, {"error": type(exc).__name__})
            self._reply(200, result)
            print(f"{time.strftime('%H:%M:%S')} {len(result['answers'])} questions, "
                  f"{result['usage']['input_tokens']} tokens, {time.monotonic() - started:.2f} s", flush=True)

        def log_message(self, *args):  # one line per request above; never the body
            pass

    HTTPServer((host, port), Handler).serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("--chunk", type=int, default=1024, help="queries per masked SDPA call on the unfused path")
    args = parser.parse_args()
    token = os.environ.get("LOCAL_D1_KEY")
    if not token:
        parser.error("set LOCAL_D1_KEY to the token the arm will send")
    model = load(args.revision, args.chunk)
    print(f"d1-2B {args.revision[:12]} on http://{args.host}:{args.port}", flush=True)
    serve(model, token, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
