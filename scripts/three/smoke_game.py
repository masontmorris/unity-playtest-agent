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


def wait_http(port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"the dev server did not open port {port}")


async def drive(game: Path, actions: list[str], settle_s: float) -> dict:
    deadline = time.monotonic() + 120
    while True:
        try:
            client = await BridgeClient.connect_from_project(game)
            break
        except Exception:  # the page attaches once the game has built its scene
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(1)
    out: dict = {}
    try:
        status = client.status
        out["status"] = {"engine": status.engine.value, "input_backends": [b.value for b in status.input_backends],
                         "capture": status.capture, "capture_self_test": status.capture_self_test}
        await asyncio.sleep(settle_s)
        first = await client.observe_once()
        later = client.status  # the latest status the bridge pushed
        if later is not None:
            out["status"]["capture_after_settle"] = {"capture": later.capture, "capture_self_test": later.capture_self_test}
        out["observe"] = {"scene": first.scene, "playing": first.playing, "game_s": first.game_s,
                          "frame": first.frame, "view": first.view.model_dump() if first.view else None}
        acted = {}
        for index, name in enumerate(actions):
            based = await client.observe_once()
            records = await client.act(action_id=f"smoke-{index}", choice_id=name, action=name,
                                       based_on_seq=based.seq, max_age_s=5, params={}, depends_on=[])
            terminal = records[-1]
            after = await client.observe_once()
            acted[name] = {"status": terminal.status.value, "release_verified": terminal.release_verified,
                           "yaw_change": None if not (after.view and based.view) else
                           round(after.view.yaw_deg - based.view.yaw_deg, 2)}
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--query", default="", help="extra URL query, e.g. forceWebGL=1")
    parser.add_argument("--act", nargs="*", default=[])
    parser.add_argument("--settle", type=float, default=5.0)
    args = parser.parse_args()
    node, npx = shutil.which("node"), shutil.which("npx")
    game = args.game.resolve()
    procs = []
    try:
        relay = subprocess.Popen([node, str(RELAY), "--project", str(game), "--catalog", str(args.catalog.resolve())],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                 errors="replace")
        procs.append(relay)
        endpoint = json.loads(relay.stdout.readline())["playtest"]
        port = free_port()
        dev = subprocess.Popen([npx, "vite", "--port", str(port), "--strictPort", "--host", "127.0.0.1"], cwd=game,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=os.name == "nt")
        procs.append(dev)
        wait_http(port, 90)
        query = "playtest=" + urllib.parse.quote(endpoint, safe="") + (f"&{args.query}" if args.query else "")
        page = subprocess.Popen([node, str(HERE / "open_page.mjs"), str(game), f"http://127.0.0.1:{port}/?{query}"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        procs.append(page)
        summary = asyncio.run(drive(game, args.act, args.settle))
        print(json.dumps(summary, indent=1, default=str))
        return 0
    finally:
        for proc in reversed(procs):
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if procs and procs[-1].stdout:
            tail = procs[-1].stdout.read()[-2000:] if procs[-1].poll() is not None else ""
            if tail.strip():
                print("page output:", tail, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
