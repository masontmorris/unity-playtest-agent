"""Serve the open-weights d1-2B on this PC as the `d1-2b-local` S1 arm.

    $env:D1_LOCAL_TOKEN = [guid]::NewGuid().ToString('N')
    python C:\\Users\\mason\\.gpu\\gpu_lease.py run --holder unity-playtest-agent --purpose "d1-2b-local S1 shim" -- `
        C:\\Users\\mason\\decision-lab\\.venv\\Scripts\\python.exe scripts\\s1\\d1_local_server.py --port 8765

The server loads the model once, prints one JSON ready line on stdout, then answers
`POST /v1/systemone` with the shape Liquid's decisions endpoint takes: `{"model", "questions", "state"}` and an
optional `images` array of data URLs. It calls `model.system_one(state, questions[, media])` and returns that
answer as the model gave it, beside a `usage` that adds the forward pass's measured `inference_ms` and a
`cost` of 0: nothing here bills. It never picks, repairs or re-ranks a choice; an answer the model got wrong
is sent as is, and the runner's `parse_answer` rejects it.

It binds 127.0.0.1 only and wants `Authorization: Bearer <D1_LOCAL_TOKEN>` on every route. The token is read
from the environment, must be at least 8 characters (the runner redacts only values that long from traces),
and is never printed or logged. Requests are served one forward pass at a time. One line per request goes to
stderr. The model is open weights under the LFM Open License; its outputs are for dev runs only.

stdlib only at import: `--help` and `--fake` need no GPU, and decision-lab, torch and the weights are loaded
only when the real model is asked for. Run the real model under decision-lab's venv and the GPU lease.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hmac
import io
import json
import os
import re
import socketserver
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

MODEL_NAME = "d1-2b"  # the model id a request names, and the arm's `ARMS` entry
FAKE_MODEL_NAME = "d1-2b-fake"  # what a stand-in's replies call themselves
HOST = "127.0.0.1"
TOKEN_VARIABLE = "D1_LOCAL_TOKEN"
MIN_TOKEN_CHARS = 8
DEFAULT_DECISION_LAB = Path(r"C:\Users\mason\decision-lab")
HUB_REPO = "models--LiquidAI--d1-2B"
BODY_KEYS = frozenset({"model", "questions", "state", "images"})
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_IMAGES = 4
IMAGE_URL = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,(.*)", re.DOTALL)


class Refused(Exception):
    """A request this server turns away, with the HTTP status and error kind it answers."""

    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


@dataclass(frozen=True)
class Served:
    """What one server answers with: the model, how a picture becomes the media it takes, and its provenance."""

    model: Any
    token: str
    decode: Callable[[bytes], Any]
    resolved: str = MODEL_NAME
    revision: str | None = None
    kernels: dict[str, str] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, compare=False)


def decode_picture(raw: bytes) -> Any:
    """A PIL image, which is what `system_one` takes as a picture. Decoded here so a bad file is a 400."""
    from PIL import Image

    image = Image.open(io.BytesIO(raw))
    image.load()
    return image


class FakeModel:
    """A stand-in with `system_one`'s signature, for tests and `--fake`: no GPU, and no judgment worth the name.

    It answers every choice question with the first criterion at a uniform distribution, so the shape
    is right and the choice is meaningless. Its replies name `d1-2b-fake`.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[Any, dict[str, Any], list[Any] | None]] = []

    def system_one(self, state: Any, questions: dict[str, Any], media: list[Any] | None = None) -> dict[str, Any]:
        self.requests.append((state, questions, media))
        answers = {}
        for name, question in questions.items():
            criteria = question.get("criteria") if isinstance(question, dict) else None
            if not isinstance(question, dict) or question.get("type") != "choice" or not criteria:
                raise ValueError(f"question {name!r}: the fake model answers choice questions only")
            share = 1.0 / len(criteria)
            answers[name] = {"type": "choice", "choice": next(iter(criteria)), "confidence": share,
                             "probabilities": {key: share for key in criteria}}
        size = len(json.dumps([state, questions], default=str))
        return {"answers": answers, "usage": {"input_tokens": size // 4, "output_tokens": 0}}


def _plain(value: Any) -> Any:
    """JSON for the numbers a tensor library hands back; anything else is a bug worth a 500."""
    if hasattr(value, "item") and callable(value.item):
        return value.item()
    if hasattr(value, "tolist") and callable(value.tolist):
        return value.tolist()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _pictures(images: Any, decode: Callable[[bytes], Any]) -> list[Any]:
    if not isinstance(images, list) or not all(isinstance(url, str) for url in images):
        raise Refused(400, "invalid_request", "images must be a list of data:image/...;base64 URLs")
    if len(images) > MAX_IMAGES:
        raise Refused(400, "invalid_request", f"at most {MAX_IMAGES} images")
    pictures = []
    for index, url in enumerate(images, 1):
        found = IMAGE_URL.fullmatch(url)
        try:
            if found is None:
                raise ValueError("not a data:image URL")
            pictures.append(decode(base64.b64decode(found.group(1), validate=True)))
        except (ValueError, binascii.Error, OSError) as exc:
            raise Refused(400, "invalid_request", f"image {index} cannot be decoded ({type(exc).__name__})") from None
    return pictures


def respond(served: Served, body: Any) -> bytes:
    """The response bytes for one `/v1/systemone` body, or a `Refused` for a body the model never saw."""
    if not isinstance(body, dict):
        raise Refused(400, "invalid_request", "the body must be a JSON object")
    unknown = sorted(set(body) - BODY_KEYS)
    if unknown:
        raise Refused(400, "invalid_request", f"unsupported fields: {', '.join(unknown)}")
    if body.get("model") not in (None, MODEL_NAME):
        raise Refused(400, "invalid_request", f"this server serves {MODEL_NAME}, not {body['model']!r}")
    questions = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise Refused(400, "invalid_request", "questions must be a non-empty object")
    pictures = _pictures(body["images"], served.decode) if "images" in body else []
    if "state" not in body and not pictures:
        raise Refused(400, "invalid_request", "send a state, images, or both")
    with served.lock:
        started = time.perf_counter()
        try:
            result = (served.model.system_one(body.get("state"), questions, media=pictures) if pictures
                      else served.model.system_one(body.get("state"), questions))
        except ValueError as exc:  # the model refused the question or the state: nothing was read
            raise Refused(400, "invalid_request", str(exc)[:500]) from None
        inference_ms = (time.perf_counter() - started) * 1000
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise Refused(502, "bad_model_output", "the model returned no answers")
    usage = result.get("usage")
    usage = {**usage} if isinstance(usage, dict) else {}
    usage.update(cost=0.0, inference_ms=round(inference_ms, 3))
    reply = {**result, "model": served.resolved, "usage": usage}
    for key, value in (("revision", served.revision), ("kernels", served.kernels)):
        if value is not None:
            reply[key] = value
    try:
        return json.dumps(reply, allow_nan=False, default=_plain).encode("utf-8")
    except ValueError:  # JSON has no NaN or infinity; say so rather than send a body no client reads the same
        raise Refused(502, "bad_model_output", "the model's answer holds a non-finite number") from None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # the runner keeps its connection alive between calls
    server_version = "d1-local"
    sys_version = ""
    timeout = 60  # an idle connection or a stalled upload; a forward pass is not a socket read

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def _serve(self) -> None:
        served: Served = self.server.served  # type: ignore[attr-defined]
        started = time.perf_counter()
        try:
            raw = self._read_body() if self.command == "POST" else b""  # before the answer, or a 401 resets the socket
            self._authorize(served.token)
            payload = self._route(served, raw)
            status = 200
        except Refused as refusal:
            status = refusal.status
            payload = json.dumps({"error": {"type": refusal.kind, "message": refusal.message}}).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the server
            status = 500
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            payload = json.dumps({"error": {"type": "server_error", "message": message}}).encode("utf-8")
        self._send(status, payload)
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            print(f"d1-local: {self.command} {self.path} {status} {elapsed_ms:.0f} ms", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            raise Refused(411, "length_required", "send a Content-Length")
        if not (length.isascii() and length.isdigit()) or int(length) > MAX_BODY_BYTES:
            raise Refused(413, "too_large", f"the body must be at most {MAX_BODY_BYTES} bytes")
        return self.rfile.read(int(length))

    def _authorize(self, token: str) -> None:
        sent = self.headers.get("Authorization") or ""
        if not hmac.compare_digest(sent.encode("utf-8"), f"Bearer {token}".encode("utf-8")):
            raise Refused(401, "unauthorized", "a valid bearer token is required")

    def _route(self, served: Served, raw: bytes) -> bytes:
        if self.path == "/health" and self.command == "GET":
            return json.dumps({"status": "ok", "model": served.resolved}).encode("utf-8")
        if self.path != "/v1/systemone":
            raise Refused(404, "not_found", "no such route")
        if self.command != "POST":
            raise Refused(405, "method_not_allowed", "POST a body to /v1/systemone")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise Refused(400, "invalid_request", "the body is not JSON") from None
        return respond(served, body)

    def _send(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if status >= 400:  # whatever follows an error on this connection is not worth parsing
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows this would let another process share the port
    daemon_threads = True

    def __init__(self, served: Served, port: int = 0) -> None:
        super().__init__((HOST, port), Handler)
        self.served = served

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # not HTTPServer's, whose getfqdn lookup can stall on Windows
        self.server_name, self.server_port = self.server_address[:2]


def make_server(served: Served, port: int = 0) -> Server:
    """A server bound to loopback, not yet serving. `port` 0 picks a free one; read it from `server_address`."""
    if len(served.token) < MIN_TOKEN_CHARS:
        raise ValueError(f"the bearer token must be at least {MIN_TOKEN_CHARS} characters")
    return Server(served, port)


def hub_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    return Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"


def load_real(decision_lab: Path, snapshot: Path | None, kernels: str) -> tuple[Any, str, dict[str, str]]:
    """decision-lab's `load_d1` over the pinned d1-2B snapshot: the model, its revision and the kernels it chose.

    Needs CUDA, transformers >= 5.19 and decision-lab's venv; `load_d1` sets the Hub offline.
    """
    if not (decision_lab / "decision_lab" / "d1.py").is_file():
        raise SystemExit(f"d1_local_server: {decision_lab} is not a decision-lab checkout (--decision-lab)")
    sys.path.insert(0, str(decision_lab))
    from decision_lab.d1 import load_d1
    from decision_lab.modal_config import D1_REVISION

    directory = snapshot or hub_cache() / HUB_REPO / "snapshots" / D1_REVISION
    if not (directory / "config.json").is_file():
        raise SystemExit(f"d1_local_server: no d1-2B snapshot at {directory} (--snapshot)")
    model, selected = load_d1(directory, kernels)
    return model, directory.name, selected


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=0, help="the loopback port (default 0: a free one, named in the ready line)")
    ap.add_argument("--decision-lab", type=Path, default=DEFAULT_DECISION_LAB,
                    help=f"the decision-lab checkout whose load_d1 loads the model (default {DEFAULT_DECISION_LAB})")
    ap.add_argument("--snapshot", type=Path,
                    help="the d1-2B snapshot directory (default: the revision decision-lab pins, in the Hub cache)")
    ap.add_argument("--kernels", choices=("auto", "portable"), default="auto",
                    help="decision-lab's kernel routing; the ready line says which kernels were chosen")
    ap.add_argument("--fake", action="store_true",
                    help="serve a stand-in that answers the first criterion: no GPU, for tests (replies name d1-2b-fake)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    token = os.environ.get(TOKEN_VARIABLE, "")
    if len(token) < MIN_TOKEN_CHARS:
        print(f"d1_local_server: {TOKEN_VARIABLE} must be set to at least {MIN_TOKEN_CHARS} characters", file=sys.stderr)
        return 2
    if not 0 <= args.port <= 65535:
        print("d1_local_server: --port must be 0 to 65535", file=sys.stderr)
        return 2
    if args.fake:
        served = Served(FakeModel(), token, lambda raw: raw, resolved=FAKE_MODEL_NAME)
    else:
        with contextlib.redirect_stdout(sys.stderr):  # nothing the loader prints may come before the ready line
            model, revision, kernels = load_real(args.decision_lab, args.snapshot, args.kernels)
        served = Served(model, token, decode_picture, revision=revision, kernels=kernels)
    server = make_server(served, args.port)
    print(json.dumps({"ready": True, "port": server.server_address[1], "model": served.resolved,
                      "revision": served.revision, "kernels": served.kernels, "fake": args.fake}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
