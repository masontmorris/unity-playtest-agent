from __future__ import annotations

from pathlib import Path

import pytest

from playtest.preflight import cli


def test_capture_closes_the_client_and_saves_its_exact_response(monkeypatch, tmp_path: Path):
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
    output = tmp_path / "out" / "capture.json"
    project = tmp_path / "game"
    assert cli.main(["--project", str(project), "--stage", "discovery", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == '{"caps": {}, "errors": []}\n'
    assert events == [project.resolve(), 120.0, "discovery", "closed"]


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
