"""`--guards-only`: measure the guard table from a fresh start and end before play, with no S1 call.

The refusal is a startup error, before any file is read and before any paid call: without `--measure-guards` there
is nothing for the session to do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playtest.runner import cli

ROOT = Path(__file__).resolve().parents[1]


def argv(tmp_path, *extra: str) -> list[str]:
    return ["--project", str(tmp_path / "game"), "--entity", "e", "--calibration", "c",
            "--output", str(tmp_path / "out"), "--approval", "a", "--max-calls", "1",
            "--manifest", "m", "--catalog", "cat", "--catalog-sha256", "0", *extra]


def test_guards_only_needs_the_guard_measurement(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(argv(tmp_path, "--guards-only"))
    assert "--guards-only measures the guard table" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_guards_only_is_a_flag_and_its_variable(monkeypatch):
    base = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    assert cli._parser().parse_args(base).guards_only is False
    assert cli._parser().parse_args([*base, "--guards-only"]).guards_only is True
    monkeypatch.setenv("UPA_RUN_GUARDS_ONLY", "1")
    assert cli._parser().parse_args(base).guards_only is True


def test_the_session_script_passes_it_and_the_reproduction_script_uses_it():
    session = (ROOT / "scripts/unity/run-session.ps1").read_text(encoding="utf-8")
    assert "$env:UPA_RUN_GUARDS_ONLY" in session and "-GuardsOnly measures the guard table" in session
    reproduce = (ROOT / "scripts/unity/measure-guards.ps1").read_text(encoding="utf-8")
    assert "'-MeasureGuards', '-GuardsOnly'" in reproduce and "playtest.runner.expectations score" in reproduce
