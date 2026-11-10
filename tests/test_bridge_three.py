"""The production client against the three.js bridge, end to end.

Node runs the real relay and the real page connector with a simulated page (a timer-driven clock, a
small scene of object doubles and a grabber that yields a fixed tiny JPEG), so this needs no browser
and no GPU. The client finds the bridge through the project lock exactly as it finds the Unity
bridge, and the runner's own readers consume its transforms and frames.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from playtest.contracts.bridge import Engine
from playtest.core.bridge_client import BridgeClient
from playtest.preflight.calibrate import _entity_snapshot
from playtest.runner.game_view import BridgeView

SIM_PAGE = Path(__file__).resolve().parents[1] / "bridge-three" / "support" / "sim-page.mjs"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("CI must provide Node for the three.js bridge test")
        pytest.skip("Node is not installed")
    return node


@pytest.fixture
def sim_page(tmp_path):
    process = subprocess.Popen(
        [_node(), str(SIM_PAGE), "--project", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        ready = process.stdout.readline()
        if json.loads(ready or "null") != {"ready": True}:
            process.kill()
            pytest.fail(f"the simulated page did not start: {process.stderr.read()}")
        yield tmp_path
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_client_drives_the_three_bridge(sim_page):
    async def main():
        client = await BridgeClient.connect_from_project(sim_page)
        try:
            status = client.status
            assert status.engine is Engine.three
            assert status.unity_version is None
            assert status.input_backends == []

            first = await client.observe_once()
            assert first.scene == "sim" and first.playing
            assert first.view is not None and first.view.yaw_deg == pytest.approx(90)
            assert first.reader_errors == []

            await client.clock("pause")
            paused = await client.observe_once()
            assert paused.paused
            await asyncio.sleep(0.1)
            still = await client.observe_once()
            assert still.game_s == paused.game_s and still.frame == paused.frame

            await client.clock("step", step_game_s=0.5)
            stepped = await client.observe_once()
            assert stepped.game_s == pytest.approx(paused.game_s + 0.5)
            assert stepped.frame > paused.frame

            await client.clock("resume")
            await asyncio.sleep(0.1)
            resumed = await client.observe_once()
            assert resumed.game_s > stepped.game_s and not resumed.paused

            stopped = await client.stop()
            assert stopped is not None and stopped.released == [] and stopped.release_verified
        finally:
            await client.aclose()

    asyncio.run(main())


def test_runner_readers_consume_three_transforms_and_frames(sim_page):
    async def main():
        started = datetime.now(timezone.utc) - timedelta(seconds=1)
        client = await BridgeClient.connect_from_project(sim_page)
        try:
            reading = _entity_snapshot(await client.inspect("transforms"), "scene:sim::0")
            assert reading["position"] == [1, 0.5, -2]
            assert reading["basis_state"] == "present"
            assert reading["yaw_deg"] == pytest.approx(180)
            assert reading["runtime_errors"]["active"] is True and reading["runtime_errors"]["observed"] == 0

            status = None
            for _ in range(50):
                status = await client.clock("resume")
                if status.capture_self_test != "unknown":
                    break
                await asyncio.sleep(0.05)
            assert status.capture is True and status.capture_self_test == "passed"

            observation = await client.observe_once(with_frame=True)
            frame = Path(observation.frame_ref)
            assert frame.parent == sim_page / "Temp" / "playtest-bridge-frames"
            assert frame.read_bytes().startswith(bytes([0xFF, 0xD8]))

            sample = BridgeView(sim_page, status, started_utc=started).sample(observation)
            assert sample.status == "sent", sample.statement
            assert "32x16" in sample.statement
        finally:
            await client.aclose()

    asyncio.run(main())
