from __future__ import annotations

import json

import pytest

from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, RunRecord
from playtest.contracts.trace import TraceAuthor, TraceKind
from playtest.core.trace import TraceWriter, read_trace


def run():
    return RunRecord(run_id="r-test", game_project="Example", clock_mode=ClockMode.realtime,
                     models=[], objective="test", budget=Budget(), core_version="0.0.1")


def test_exclusive_run_first_sequence_close_and_redaction(tmp_path):
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path, run(), secrets=["secret-token"])
    assert writer.append("stop", {"reason": "finish", "result": None, "error": "secret-token"},
                         author=TraceAuthor.core) == 1
    writer.close("stopped")
    with pytest.raises(ValueError):
        writer.append("stop", {"reason": "finish", "result": None, "error": None}, author="core")
    rows = read_trace(path)
    assert [e.trace_seq for e in rows] == [0, 1, 2]
    assert rows[0].kind is TraceKind.run and rows[0].body["run_id"] == "r-test"
    assert rows[-1].body == {"status": "stopped", "error": None, "turns": 0, "redactions": 1}
    assert "secret-token" not in path.read_text()
    with pytest.raises(FileExistsError): TraceWriter(path, run())


def test_validation_failure_does_not_change_file(tmp_path):
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path, run()) as writer:
        before = path.read_bytes()
        with pytest.raises(Exception):
            writer.append("stop", {"bad": True}, author="core")
        assert path.read_bytes() == before


def test_unparseable_and_truncated_final_line_are_reported(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"not": "a trace"}')
    with pytest.raises(ValueError, match="final line"):
        read_trace(path)
    path.write_text(json.dumps({"trace_seq": 0}) + "\n")
    with pytest.raises(ValueError, match="final line"):
        read_trace(path)


def test_non_strict_reader_returns_intact_prefix_and_tail_error(tmp_path):
    path = tmp_path / "tail.jsonl"
    with TraceWriter(path, run()) as writer:
        writer.append("stop", {"reason":"finish","result":None,"error":None}, author="core")
    path.write_bytes(path.read_bytes() + b'{"broken":')
    entries, tail_error = read_trace(path, strict=False)
    assert len(entries) == 3
    assert tail_error and "final line" in tail_error


def test_nonfinal_invalid_line_still_fails_in_non_strict_mode(tmp_path):
    path = tmp_path / "bad-middle.jsonl"
    path.write_bytes(b'{"broken":}\n{}\n')
    with pytest.raises(ValueError, match="line 1"):
        read_trace(path, strict=False)


def test_verify_reports_bad_tail_after_replaying_prefix(tmp_path, monkeypatch, capsys):
    from playtest.core import trace_cli

    path = tmp_path / "tail-verify.jsonl"
    with TraceWriter(path, run()) as writer:
        writer.append("stop", {"reason":"finish","result":None,"error":None}, author="core")
    path.write_bytes(path.read_bytes() + b'{"bad":')
    monkeypatch.setattr("sys.argv", ["playtest-trace", "verify", str(path)])
    assert trace_cli.main() == 1
    output = capsys.readouterr().out
    assert "problems=1" in output
    assert "truncated or unparseable final line" in output
