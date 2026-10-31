from __future__ import annotations

import pytest

from playtest.runner import cli
from playtest.runner.request_layout import RequestLayout

REQUIRED = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]


def test_the_s1_layout_defaults_to_the_full_body_and_accepts_the_lossless_layout(monkeypatch):
    monkeypatch.delenv("UPA_RUN_S1_LAYOUT", raising=False)
    assert cli._parser().parse_args(REQUIRED).s1_layout == RequestLayout.full.value
    chosen = cli._parser().parse_args([*REQUIRED, "--s1-layout", RequestLayout.duplicate_state_reference.value])
    assert chosen.s1_layout == "dup-state-ref/1"
    monkeypatch.setenv("UPA_RUN_S1_LAYOUT", "dup-state-ref/1")
    assert cli._parser().parse_args(REQUIRED).s1_layout == "dup-state-ref/1"
    with pytest.raises(SystemExit):
        cli._parser().parse_args([*REQUIRED, "--s1-layout", "compact"])
