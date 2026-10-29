from __future__ import annotations

import json
from pathlib import Path

import pytest

from playtest.preflight import cli, il


@pytest.mark.parametrize("stage", ["inventory", "transforms", "fields"])
def test_capture_closes_the_client_and_saves_its_exact_response(monkeypatch, tmp_path: Path, stage: str):
    events: list[object] = []

    class Response:
        def model_dump_json(self, *, indent: int) -> str:
            assert indent == 2
            return '{"caps": {}, "errors": []}'

    class Client:
        async def inspect(self, stage: str):
            events.append(stage)
            return Response()

        async def aclose(self):
            events.append("closed")

    async def connect(project: Path, *, timeout_s: float):
        events.extend([project, timeout_s])
        return Client()

    monkeypatch.setattr(cli.BridgeClient, "connect_from_project", connect)
    monkeypatch.setattr(il, "complete", lambda *args, **kwargs: pytest.fail("only discovery is completed"))
    output = tmp_path / "out" / "capture.json"
    project = tmp_path / "game"
    assert cli.main(["--project", str(project), "--stage", stage, "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == '{"caps": {}, "errors": []}\n'
    assert events == [project.resolve(), 120.0, stage, "closed"]


def test_discovery_is_completed_with_the_il_scan_once_the_client_is_closed(monkeypatch, tmp_path: Path):
    events: list[object] = []
    sent = {"stage": "discovery", "data": {"il_inputs": {"assemblies": []}}, "wall_utc": "2026-10-29T12:00:00Z"}
    completed = {"stage": "discovery", "data": {"il_complete": True, "note": "caf\u00e9"}}

    class Response:
        def model_dump_json(self) -> str:
            return json.dumps(sent)

    class Client:
        async def inspect(self, stage: str):
            events.append(stage)
            return Response()

        async def aclose(self):
            events.append("closed")

    async def connect(project: Path, *, timeout_s: float):
        return Client()

    def complete(snapshot: dict):
        assert snapshot == sent
        events.append("completed")
        return completed

    monkeypatch.setattr(cli.BridgeClient, "connect_from_project", connect)
    monkeypatch.setattr(il, "complete", complete)
    output = tmp_path / "out" / "discovery.json"
    assert cli.main(["--project", str(tmp_path / "game"), "--stage", "discovery", "--output", str(output)]) == 0
    expected = json.dumps(completed, indent=2, ensure_ascii=False) + "\n"
    assert output.read_bytes() == expected.encode("utf-8")
    assert events == ["discovery", "closed", "completed"]


def test_a_failed_il_scan_is_reported_without_a_partial_file(monkeypatch, tmp_path: Path, capsys):
    closed = []

    class Response:
        def model_dump_json(self) -> str:
            return '{"stage": "discovery"}'

    class Client:
        async def inspect(self, stage: str):
            return Response()

        async def aclose(self):
            closed.append(True)

    async def connect(project: Path, *, timeout_s: float):
        return Client()

    def complete(snapshot: dict):
        raise ValueError("the IL scan tool needs the .NET SDK (dotnet) on PATH")

    monkeypatch.setattr(cli.BridgeClient, "connect_from_project", connect)
    monkeypatch.setattr(il, "complete", complete)
    output = tmp_path / "out" / "discovery.json"
    assert cli.main(["--project", str(tmp_path / "game"), "--stage", "discovery", "--output", str(output)]) == 1
    assert "inspection failed: the IL scan tool needs the .NET SDK" in capsys.readouterr().err
    assert closed == [True]
    assert not output.exists()
    assert not output.parent.exists()


def test_capture_closes_the_client_when_inspection_fails(monkeypatch, tmp_path: Path):
    closed = []

    class Client:
        async def inspect(self, stage: str):
            raise ValueError("not available")

        async def aclose(self):
            closed.append(True)

    async def connect(project: Path, *, timeout_s: float):
        return Client()

    monkeypatch.setattr(cli.BridgeClient, "connect_from_project", connect)
    output = tmp_path / "capture.json"
    assert cli.main(["--project", str(tmp_path / "game"), "--stage", "inventory", "--output", str(output)]) == 1
    assert closed == [True]
    assert not output.exists()


def test_refuses_to_write_inside_game_or_overwrite(tmp_path: Path):
    project = tmp_path / "game"
    project.mkdir()
    existing = tmp_path / "existing.json"
    existing.write_text("keep", encoding="utf-8")
    for output in (project / "dump.json", existing):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--project", str(project), "--stage", "inventory", "--output", str(output)])
        assert exc.value.code == 2
    assert existing.read_text(encoding="utf-8") == "keep"


def test_missing_bridge_is_reported_without_a_partial_file(tmp_path: Path, capsys):
    output = tmp_path / "capture.json"
    assert cli.main(["--project", str(tmp_path / "game"), "--stage", "inventory", "--output", str(output)]) == 1
    assert "bridge lock not found" in capsys.readouterr().err
    assert not output.exists()
