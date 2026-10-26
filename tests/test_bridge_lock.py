"""Project-lock discovery for the real Unity bridge.

The lock is the only endpoint advertisement: port, protocol and per-session
token. These tests cover validation and a real client handshake against the
fake game with a lock file pointing at it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import (
    BridgeLockError,
    lock_path_for_project,
    read_bridge_lock,
    read_project_lock,
)

from fake_game import FakeGame

TOKEN = "lock-test-token"


def _write_lock(
    project: Path,
    port: int,
    *,
    token: str = TOKEN,
    protocol: str = "upa/0",
    started_utc: str | None = None,
) -> Path:
    directory = project / "Temp"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "playtest-bridge.lock"
    body = {
        "protocol": protocol,
        "pid": 4242,
        "port": port,
        "token": token,
        "started_utc": started_utc
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_lock_path_is_the_project_temp_file(tmp_path: Path):
    path = lock_path_for_project(tmp_path)
    assert path == tmp_path / "Temp" / "playtest-bridge.lock"
    assert ".lock" in path.name


def test_read_project_lock_round_trips_the_endpoint(tmp_path: Path):
    path = _write_lock(tmp_path, 38123)

    endpoint = read_project_lock(tmp_path)

    assert endpoint.path == path
    assert endpoint.protocol == "upa/0"
    assert endpoint.pid == 4242
    assert endpoint.port == 38123
    assert endpoint.token == TOKEN
    assert endpoint.started_utc.tzinfo is not None
    assert endpoint.host == "127.0.0.1"
    assert read_bridge_lock(path) == endpoint


def test_missing_lock_is_typed(tmp_path: Path):
    with pytest.raises(BridgeLockError) as exc_info:
        read_project_lock(tmp_path)
    assert exc_info.value.code == "missing"


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "[]",
        json.dumps({"protocol": "upa/0"}),
        json.dumps({"protocol": "upa/0", "pid": 0, "port": 1, "token": "x", "started_utc": "2026-01-01T00:00:00Z"}),
        json.dumps({"protocol": "upa/0", "pid": 1, "port": 0, "token": "x", "started_utc": "2026-01-01T00:00:00Z"}),
        json.dumps({"protocol": "upa/0", "pid": 1, "port": 1, "token": "", "started_utc": "2026-01-01T00:00:00Z"}),
        json.dumps({"protocol": "upa/0", "pid": 1, "port": 1, "token": "x", "started_utc": "not-a-time"}),
    ],
)
def test_invalid_lock_is_typed(tmp_path: Path, body: str):
    path = tmp_path / "playtest-bridge.lock"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(BridgeLockError) as exc_info:
        read_bridge_lock(path)
    assert exc_info.value.code == "invalid"


def test_unsupported_protocol_is_reported_as_protocol(tmp_path: Path):
    path = _write_lock(tmp_path, 38123, protocol="upa/9")

    with pytest.raises(BridgeLockError) as exc_info:
        read_bridge_lock(path)
    assert exc_info.value.code == "protocol"


def test_connect_from_project_uses_the_lock_port_and_token(tmp_path: Path):
    async def main():
        game = FakeGame(token=TOKEN)
        port = await game.start()
        try:
            _write_lock(tmp_path, port)
            client = await BridgeClient.connect_from_project(tmp_path)
            try:
                assert client.status is not None
                assert client.status.type == "status"
                assert client.endpoint is not None
                assert client.endpoint.port == port
                observation = await client.observe_once()
                assert observation.seq >= 0
            finally:
                await client.aclose()
        finally:
            await game.stop()

    asyncio.run(main())


def test_connect_from_lock_accepts_an_explicit_path(tmp_path: Path):
    async def main():
        game = FakeGame(token=TOKEN)
        port = await game.start()
        try:
            path = _write_lock(tmp_path, port)
            client = await BridgeClient.connect_from_lock(path)
            try:
                assert client.status is not None
                assert client.endpoint is not None
                assert client.endpoint.path == path
            finally:
                await client.aclose()
        finally:
            await game.stop()

    asyncio.run(main())


def test_wrong_lock_token_is_an_auth_failure(tmp_path: Path):
    async def main():
        game = FakeGame(token=TOKEN)
        port = await game.start()
        try:
            _write_lock(tmp_path, port, token="not-the-editor-token")
            with pytest.raises(BridgeError) as exc_info:
                await BridgeClient.connect_from_project(tmp_path)
            assert exc_info.value.code == "auth"
        finally:
            await game.stop()

    asyncio.run(main())
