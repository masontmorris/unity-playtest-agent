from __future__ import annotations

import json
from pathlib import Path

import pytest

from playtest.runner import reproduce


def test_a_finding_record_and_a_trace_finding_id_are_mutually_exclusive(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="not both"):
        reproduce.build_plan(trace, manifest, finding_id="f-1", finding_record={"finding_id": "r-1"})


def test_the_plan_cli_accepts_a_finding_file():
    args = reproduce._parse(["plan", "--trace", "t", "--manifest", "m", "--output", "o", "--finding-file", "f.json"])
    assert args.finding_file == Path("f.json")
