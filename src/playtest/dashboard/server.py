"""`playtest-dashboard`: serve the dashboard on loopback.

    playtest-dashboard --runs runs [--port 8765]

Endpoints (all GET unless noted; `run` is a folder relative to the runs root):
  /api/runs                       hunts and sessions found
  /api/run?run=                   one hunt (steps, active step, log tail) or session (models, objective, cost)
  /api/events?run=&after=         server-sent events: the session trace, compacted, as it is appended
  /api/frame.jpg?run=&after=      the session's newest game frame; 204 when it is still the one named in `after`
  /api/findings?run=              findings with their replay and recorded evidence
  /api/file?run=&path=            a recorded file inside the run (video, image, markdown, json, log)
  /api/launch/options, /api/launch/check?config=, POST /api/launch/quote, POST /api/launch, /api/jobs

Nothing here acts on a running session: no endpoint chooses, pauses, steers, stops or asks for help.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import frames, runs
from .launch import LaunchError, Launcher
from .trace_feed import TraceTail

STATIC = Path(__file__).with_name("static")
SERVED_SUFFIXES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".png": "image/png", ".md": "text/markdown",
                   ".json": "application/json", ".log": "text/plain", ".txt": "text/plain"}
POLL_S = 0.25
HEARTBEAT_S = 15.0
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


class Dashboard:
    def __init__(self, runs_root: Path, launcher: Launcher | None = None):
        self.runs_root = runs_root.resolve()
        self.launcher = launcher or Launcher(self.runs_root)
        self._cards: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._cards_lock = threading.Lock()

    def run_dir(self, run: str | None) -> Path:
        if not run:
            raise LookupError("run is required")
        folder = (self.runs_root / run).resolve()
        if folder != self.runs_root and self.runs_root not in folder.parents:
            raise LookupError("run is outside the runs folder")
        if not folder.is_dir():
            raise LookupError(f"no run {run}")
        return folder

    def cards(self) -> list[dict[str, Any]]:
        with self._cards_lock:
            stamp, cards = self._cards
            if time.monotonic() - stamp > 3.0:
                cards = runs.discover(self.runs_root)
                self._cards = (time.monotonic(), cards)
        return [self._with_job(card) for card in cards]

    def _with_job(self, card: dict[str, Any]) -> dict[str, Any]:
        """A hunt the dashboard started is running while its process is, whatever its files say."""
        for job in self.launcher.jobs.values():
            if card.get("kind") == "hunt" and Path(job.output).resolve() == (self.runs_root / card["id"]).resolve():
                view = job.view()
                status = "running" if view["running"] else card["status"]
                if not view["running"] and view["exit_code"] not in (0, None) and card["status"] != "complete":
                    status = f"failed (exit {view['exit_code']})"
                return {**card, "status": status, "job": view}
        return card

    def detail(self, run: str) -> dict[str, Any]:
        target = (self.runs_root / run).resolve() if run else None
        job = next((job for job in self.launcher.jobs.values() if Path(job.output).resolve() == target), None)
        if job is not None and not runs.is_hunt(target):  # just launched: the hunt has not written its config yet
            view = job.view()
            return {"kind": "hunt", "id": run, "name": job.name, "pending": True, "job": view,
                    "status": "starting" if view["running"] else f"failed (exit {view['exit_code']})",
                    "steps": [], "active": None, "log_tail": runs.tail_text(job.log, 30)}
        folder = self.run_dir(run)
        if runs.is_hunt(folder):
            detail = runs.hunt_detail(self.runs_root, folder)
            job = self._with_job({"kind": "hunt", "id": detail["id"], "status": detail["status"]})
            if job["status"] != detail["status"]:
                detail["status"] = job["status"]
                detail["job"] = job.get("job")
                if job["status"] == "running":
                    detail["steps"] = runs.hunt_steps(folder)
                    detail["active"] = next((s for s in detail["steps"] if s["state"] == "running"), None)
            return detail
        if runs.is_session(folder):
            return runs.session_detail(self.runs_root, folder)
        raise LookupError(f"{run} is neither a hunt nor a session")

    def findings(self, run: str) -> dict[str, Any]:
        folder = self.run_dir(run)
        if runs.is_hunt(folder):
            return runs.hunt_findings(self.runs_root, folder)
        if runs.is_session(folder):
            return {"final": runs.session_status(folder) not in ("live",), "unmet": None,
                    "findings": runs.session_findings(self.runs_root, folder), "cost_usd": None,
                    "sessions": [folder.name]}
        raise LookupError(f"{run} is neither a hunt nor a session")

    def recorded_file(self, run: str, relative: str | None) -> Path:
        folder = self.run_dir(run)
        if not relative:
            raise LookupError("path is required")
        path = (folder / relative).resolve()
        if folder not in path.parents or path.suffix.lower() not in SERVED_SUFFIXES or not path.is_file():
            raise LookupError("not a recorded file of this run")
        return path

    def launch_job_detail(self, name: str) -> dict[str, Any] | None:
        job = self.launcher.jobs.get(name)
        if job is None:
            return None
        return {**job.view(), "log_tail": runs.tail_text(job.log, 30)}


class Handler(BaseHTTPRequestHandler):
    dashboard: Dashboard
    server_version = "playtest-dashboard"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # quiet: the streams poll constantly
        return

    # --- plumbing -------------------------------------------------------------------------------------------

    def _local(self) -> bool:
        """Loopback only, by Host header too: a page on another site cannot reach this through DNS rebinding."""
        return urlparse("//" + (self.headers.get("Host") or "")).hostname in LOCAL_HOSTS

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, value: Any, status: int = 200) -> None:
        self._send(status, json.dumps(value, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _query(self) -> dict[str, str]:
        return {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}

    # --- routes ---------------------------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._local():
            return self._error(HTTPStatus.FORBIDDEN, "loopback only")
        route, query = urlparse(self.path).path, self._query()
        board = self.dashboard
        try:
            if route in ("/", "/index.html"):
                return self._static("index.html")
            if route.startswith("/static/"):
                return self._static(route.removeprefix("/static/"))
            if route == "/api/runs":
                job = board.launcher.running()
                running = None if job is None else {
                    **job.view(), "run": job.output.resolve().relative_to(board.runs_root).as_posix()}
                return self._json({"runs_root": str(board.runs_root), "runs": board.cards(), "running": running})
            if route == "/api/run":
                return self._json(board.detail(query.get("run", "")))
            if route == "/api/findings":
                return self._json(board.findings(query.get("run", "")))
            if route == "/api/events":
                return self._events(board.run_dir(query.get("run")), query)
            if route == "/api/frame.jpg":
                frame = frames.latest_frame(board.run_dir(query.get("run")))
                if frame is None:
                    return self._error(HTTPStatus.NOT_FOUND, "no frame")
                headers = {"X-Frame-Source": frame.source, "X-Frame-Name": frame.name,
                           "X-Frame-Written": frame.written_utc}
                if query.get("after") == f"{frame.source}/{frame.name}":  # nothing newer than what it shows
                    return self._send(HTTPStatus.NO_CONTENT, b"", "image/jpeg", headers)
                return self._send(200, frame.data, "image/jpeg", headers)
            if route == "/api/file":
                return self._file(board.recorded_file(query.get("run", ""), query.get("path")))
            if route == "/api/launch/options":
                return self._json(board.launcher.options())
            if route == "/api/launch/check":
                return self._json(board.launcher.check(query.get("config", "")))
            if route == "/api/jobs":
                return self._json({"jobs": [job.view() for job in board.launcher.jobs.values()]})
            if route == "/api/job":
                detail = board.launch_job_detail(query.get("name", ""))
                return self._json(detail) if detail else self._error(HTTPStatus.NOT_FOUND, "no such job")
        except LookupError as exc:
            return self._error(HTTPStatus.NOT_FOUND, str(exc))
        except LaunchError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        self._error(HTTPStatus.NOT_FOUND, "no such route")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        if not self._local():
            return self._error(HTTPStatus.FORBIDDEN, "loopback only")
        # A JSON body cannot be sent cross-site without a CORS preflight this server never answers.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send application/json")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            if not isinstance(request, dict):
                raise ValueError("expected an object")
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, f"bad JSON: {exc}")
        route = urlparse(self.path).path
        try:
            if route == "/api/launch/quote":
                return self._json(self.dashboard.launcher.quote(request))
            if route == "/api/launch":
                job = self.dashboard.launcher.start(request)
                return self._json({"job": job.view(),
                                   "run": job.output.resolve().relative_to(self.dashboard.runs_root).as_posix()})
        except LaunchError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        self._error(HTTPStatus.NOT_FOUND, "no such route")

    def _static(self, name: str) -> None:
        path = (STATIC / name).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "no such file")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or path.suffix == ".js":
            content_type = ("text/javascript" if path.suffix == ".js" else content_type) + "; charset=utf-8"
        self._send(200, path.read_bytes(), content_type)

    def _file(self, path: Path) -> None:
        """A recorded file, with byte ranges so a video can seek to the evidence."""
        size = path.stat().st_size
        content_type = SERVED_SUFFIXES[path.suffix.lower()]
        if content_type.startswith("text/") or content_type == "application/json":
            content_type += "; charset=utf-8"
        match = RANGE.match(self.headers.get("Range") or "")
        start, end, status = 0, size - 1, 200
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            else:
                start = max(0, size - int(match.group(2)))
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _stream_headers(self, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _events(self, session: Path, query: dict[str, str]) -> None:
        """The trace as server-sent events. A reconnect resumes after the last id it saw; the stream ends after
        the trace's `end` entry, or says the session folder holds no trace."""
        if not runs.is_session(session):
            raise LookupError("not a session (no trace.jsonl)")
        try:
            after = int(self.headers.get("Last-Event-ID") or query.get("after") or -1)
        except ValueError:
            after = -1
        self._stream_headers("text/event-stream; charset=utf-8")
        tail = TraceTail(session / "trace.jsonl")
        last_write = time.monotonic()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while True:
                batch = tail.read()
                sent = False
                ended = False
                for event in batch:
                    seq = event.get("seq")
                    if isinstance(seq, int) and seq <= after:
                        continue
                    data = json.dumps(event, default=str, ensure_ascii=False)
                    prefix = f"id: {seq}\n" if isinstance(seq, int) else ""
                    self.wfile.write(f"{prefix}data: {data}\n\n".encode("utf-8"))
                    sent = True
                    ended = ended or event.get("type") == "end"
                if sent:
                    self.wfile.flush()
                    last_write = time.monotonic()
                if ended:
                    self.wfile.write(b"event: closed\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                if not batch and time.monotonic() - last_write > HEARTBEAT_S:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                time.sleep(POLL_S)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return


def serve(runs_root: Path, host: str = "127.0.0.1", port: int = 8765,
          launcher: Launcher | None = None) -> ThreadingHTTPServer:
    dashboard = Dashboard(runs_root, launcher)
    handler = type("BoundHandler", (Handler,), {"dashboard": dashboard})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="playtest-dashboard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=Path, default=Path("runs"), help="the runs folder (default ./runs)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not args.runs.is_dir():
        print(f"playtest-dashboard: no runs folder at {args.runs.resolve()}", file=sys.stderr)
        return 1
    server = serve(args.runs, port=args.port)
    server.RequestHandlerClass.dashboard.launcher.warm()
    print(f"playtest-dashboard: http://127.0.0.1:{server.server_address[1]}/  (runs: {args.runs.resolve()})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
