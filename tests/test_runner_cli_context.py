"""The `--s1-context` flag: `full` by default, and the compact menu only with what it needs.

The refusal is a startup error, before any file is read and before any paid call: the menu's measured
cell comes from the options layer, so a run without `--options` must stop at the command line.
"""

from __future__ import annotations

import pytest

from playtest.runner import cli

REQUIRED = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]


def argv(tmp_path, *extra: str) -> list[str]:
    return ["--project", str(tmp_path / "game"), "--entity", "e", "--calibration", "c",
            "--output", str(tmp_path / "out"), "--approval", "a", "--max-calls", "1",
            "--manifest", "m", "--catalog", "cat", "--catalog-sha256", "0", *extra]


def test_the_context_defaults_to_full_and_accepts_the_compact_menu(monkeypatch):
    monkeypatch.delenv("UPA_RUN_S1_CONTEXT", raising=False)
    assert cli._parser().parse_args(REQUIRED).s1_context == "full"
    assert cli._parser().parse_args([*REQUIRED, "--s1-context", "compact-menu"]).s1_context == "compact-menu"
    monkeypatch.setenv("UPA_RUN_S1_CONTEXT", "compact-menu")
    assert cli._parser().parse_args(REQUIRED).s1_context == "compact-menu"
    with pytest.raises(SystemExit):
        cli._parser().parse_args([*REQUIRED, "--s1-context", "menu"])


def test_compact_menu_without_options_stops_at_startup(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("UPA_RUN_S1_CONTEXT", raising=False)
    with pytest.raises(SystemExit) as raised:
        cli.main(argv(tmp_path, "--s1-context", "compact-menu"))
    assert raised.value.code == 2
    assert "compact-menu requires --options" in capsys.readouterr().err


@pytest.mark.parametrize("extra,message", [
    (("--s1-layout", "dup-state-ref/1"), "cannot be combined with --s1-layout"),
    (("--s1-compact-state", "object"), "cannot be combined with --s1-compact-state"),
    (("--s1-pending-question", "batched"), "cannot be combined with --s1-pending-question"),
])
def test_compact_menu_refuses_the_layers_it_replaces(tmp_path, capsys, monkeypatch, extra, message):
    monkeypatch.delenv("UPA_RUN_S1_CONTEXT", raising=False)
    with pytest.raises(SystemExit) as raised:
        cli.main(argv(tmp_path, "--s1-context", "compact-menu", "--options", *extra))
    assert raised.value.code == 2
    assert message in capsys.readouterr().err
