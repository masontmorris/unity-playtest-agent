"""The production client against the three.js bridge, end to end.

Node runs the real relay and the real page connector with a simulated page (a timer-driven clock and
a camera double), so this needs no browser and no GPU. The client finds the bridge through the
project lock exactly as it finds the Unity bridge.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from playtest.contracts.bridge import Engine
from playtest.core.bridge_client import BridgeClient

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
            assert status.input_backends == [] and status.capture is False

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
