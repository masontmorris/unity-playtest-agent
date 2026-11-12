"""Smoke-test a three.js game behind the playtest bridge, with the production client.

    uv run python scripts/three/smoke_game.py --game DIR --catalog FILE [--query forceWebGL=1] [--act a b ...]

Starts the relay with the game's catalog, the game's Vite dev server and a headless Chromium page with
?playtest=<relay url>, then checks hello and status, observe, act for each named action, a transforms
inspection, frame capture and stop. Prints one JSON summary. Nothing is sent anywhere but loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from playtest.core.bridge_client import BridgeClient

HERE = Path(__file__).resolve().parent
RELAY = HERE.parents[1] / "bridge-three" / "relay" / "relay.mjs"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_http(port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"the dev server did not open port {port}")


async def drive(game: Path, actions: list[str], settle_s: float, out: dict) -> dict:
    deadline = time.monotonic() + 120
    while True:
        try:
            client = await BridgeClient.connect_from_project(game)
            break
        except Exception:  # the page attaches once the game has built its scene
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(1)
    try:
        status = client.status
        out["status"] = {"engine": status.engine.value, "input_backends": [b.value for b in status.input_backends],
                         "capture": status.capture, "capture_self_test": status.capture_self_test}
        # Wait out the settle, sampling how fast game time and frames advance and how long observe takes,
        # so a stall (the page blocked) can be told from a slow page.
        samples = out.setdefault("settle_samples", [])  # kept in `out` as they come, so a failure still shows them
        settle_end = time.monotonic() + settle_s
        while time.monotonic() < settle_end:
            asked = time.monotonic()
            try:
                seen = await client.observe_once()
                samples.append({"t": round(asked, 2), "observe_s": round(time.monotonic() - asked, 2),
                                "game_s": seen.game_s, "frame": seen.frame})
            except Exception as exc:  # noqa: BLE001 - recorded, not fatal, while the page loads
                samples.append({"t": round(asked, 2), "error": str(exc)[:80]})
            await asyncio.sleep(2)
        out["settle_samples"] = samples[-8:]  # trimmed once the settle completes
        first = await client.observe_once()
        later = client.status  # the latest status the bridge pushed
        if later is not None:
            out["status"]["capture_after_settle"] = {"capture": later.capture, "capture_self_test": later.capture_self_test}
        out["observe"] = {"scene": first.scene, "playing": first.playing, "game_s": first.game_s,
                          "frame": first.frame, "view": first.view.model_dump() if first.view else None,
                          "public": first.public, "reader_errors": [e.model_dump() for e in first.reader_errors]}
        acted = {}
        for index, name in enumerate(actions):
            based = await client.observe_once()
            records = await client.act(action_id=f"smoke-{index}", choice_id=name, action=name,
                                       based_on_seq=based.seq, max_age_s=5, params={}, depends_on=[])
            terminal = records[-1]
            after = await client.observe_once()
            acted[name] = {"status": terminal.status.value, "release_verified": terminal.release_verified,
                           "yaw_change": None if not (after.view and based.view) else
                           round(after.view.yaw_deg - based.view.yaw_deg, 2),
                           "readers_changed": {k: [based.public.get(k), v] for k, v in after.public.items()
                                               if based.public.get(k) != v}}
        out["act"] = acted
        inspection = await client.inspect("transforms")
        body = inspection if isinstance(inspection, dict) else inspection.model_dump()
        data = body.get("data") or {}
        listed = (data.get("game_objects") or []) if isinstance(data, dict) else []
        out["inspect"] = {"objects": len(listed), "runtime_errors": data.get("runtime_errors") if isinstance(data, dict) else None}
        frames = sorted((game / "Temp" / "playtest-bridge-frames").glob("*.jpg"))
        if frames:  # the relay clears its frames on shutdown; keep the last one for a look
            kept = game / "Temp" / "smoke-last-frame.jpg"
            kept.write_bytes(frames[-1].read_bytes())
            out["last_frame"] = str(kept)
        stopped = await client.stop()
        out["stop"] = {"release_verified": getattr(stopped, "release_verified", None)}
    finally:
        await client.aclose()
    frames = game / "Temp" / "playtest-bridge-frames"
    out["frames_written"] = len(list(frames.glob("*.jpg"))) if frames.is_dir() else 0
    return out


def _stop(proc: subprocess.Popen) -> None:
    """Stop a child and everything it started. The page closes its browser once stdin closes, so it gets a moment."""
    if proc.stdin:
        try:
            proc.stdin.close()
            proc.wait(timeout=10)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print(f"could not stop child process {proc.pid}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--query", default="", help="extra URL query, e.g. forceWebGL=1")
    parser.add_argument("--act", nargs="*", default=[])
    parser.add_argument("--settle", type=float, default=5.0)
    parser.add_argument("--capture-hz", type=float, default=None, help="bridge capture rate; 0 turns capture off")
    args = parser.parse_args()
    node = shutil.which("node")
    game = args.game.resolve()
    lock = game / "Temp" / "playtest-bridge.lock"
    if lock.is_file():
        # Two sessions on one game share its lock and frames folder, so a client can reach the other's page.
        try:
            pid = json.loads(lock.read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError):
            pid = None
        if pid and _alive(int(pid)):
            print(f"another bridge (pid {pid}) holds {lock}; run one session per game at a time", file=sys.stderr)
            return 2
        lock.unlink(missing_ok=True)
    procs = []
    page_lines: list[str] = []
    try:
        relay = subprocess.Popen([node, str(RELAY), "--project", str(game), "--catalog", str(args.catalog.resolve())],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                 errors="replace")
        procs.append(relay)
        endpoint = json.loads(relay.stdout.readline())["playtest"]
        port = free_port()
        # Vite runs under node directly: through npx and a shell, stopping the shell left the server running.
        vite = game / "node_modules" / "vite" / "bin" / "vite.js"
        dev = subprocess.Popen([node, str(vite), "--port", str(port), "--strictPort", "--host", "127.0.0.1"], cwd=game,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(dev)
        wait_http(port, 90)
        query = "playtest=" + urllib.parse.quote(endpoint, safe="") + (f"&{args.query}" if args.query else "")
        if args.capture_hz is not None:
            query += f"&playtest_capture_hz={args.capture_hz:g}"
        page = subprocess.Popen([node, str(HERE / "open_page.mjs"), str(game), f"http://127.0.0.1:{port}/?{query}"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        procs.append(page)
        out: dict = {}
        # The page reports its real frame rate and capture timings every few seconds; keep them as they come,
        # and keep its other lines (console errors) for the output's tail.
        health = out.setdefault("page_health", [])

        def read_page() -> None:
            for line in page.stdout:
                try:
                    body = json.loads(line)
                except ValueError:
                    body = None
                if isinstance(body, dict) and "page_health" in body:
                    health.append({"t": round(time.monotonic(), 2), **body["page_health"]})
                else:
                    page_lines.append(line)

        threading.Thread(target=read_page, daemon=True).start()
        try:
            asyncio.run(drive(game, args.act, args.settle, out))
        except Exception as exc:  # report what was measured before the failure, then fail
            out["failed"] = f"{type(exc).__name__}: {exc}"[:300]
            print(json.dumps(out, indent=1, default=str))
            return 1
        print(json.dumps(out, indent=1, default=str))
        return 0
    finally:
        for proc in reversed(procs):
            _stop(proc)
        if page_lines:
            print("page output:", "".join(page_lines)[-2000:], file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
