"""The report export (#39): one run folder and its replays in, report.json and report.md out.

A synthetic run folder stands in for a session: a trace with one recorded finding and one the runner could not
record, call records with measured and unknown costs, the frontier's clip and a session recording. Three replay
folders stand in for `reproduce`: one of the finding's own check, one operator control, and one made from a
different trace. What is checked is that nothing is dropped or filled in.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playtest.runner import report

T0 = datetime(2026, 10, 30, 1, 0, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _write(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else (json.dumps(value) + "\n").encode("utf-8"))
    return path


def _finding(title: str = "Speed jumps after a short hold") -> dict[str, Any]:
    return {"finding_id": "f-1", "state": "candidate", "title": title, "expected": "Speed stays below 5",
            "expected_source": "developer_assertion", "actual": "Speed read 9.5", "conditions": "After one move",
            "steps": ["Reload the scene", "Move once"], "evidence_seq": [3], "clips": [], "repro": None,
            "uncertainty": "One sample only", "severity": "major", "severity_reason": "It breaks the level",
            "repro_check": {"field": "public.speed", "op": ">=", "value": 9, "tolerance": None}}


def _run_folder(tmp_path: Path, *, title: str = "Speed jumps after a short hold", frontier_cost: Any = 0.01,
                frontier_reserved: Any = "0.05") -> Path:
    run = tmp_path / "run"
    entries = [
        {"kind": "run", "author": "harness", "turn": None, "body": {
            "run_id": "r-1", "game_project": "Game", "game_commit": "abc1234def", "scene": "Main",
            "clock_mode": "realtime", "objective": "Explore", "unity_version": "6000.5.4f1", "game_worktree": "clean",
            "game_modified_paths": [], "budget": {"max_usd": 1.0, "max_wall_s": 600.0, "max_frontier_calls": 3}}},
        {"kind": "observation", "author": "harness", "turn": 0,
         "body": {"seq": 10, "game_s": 1.0, "wall_utc": _at(1), "scene": "Main", "public": {"speed": 1.0}}},
        {"kind": "execution", "author": "harness", "turn": 0, "body": {}},
        {"kind": "observation", "author": "harness", "turn": 1,
         "body": {"seq": 11, "game_s": 2.0, "wall_utc": _at(2), "scene": "Main", "public": {"speed": 9.5}}},
        {"kind": "help", "author": "core", "turn": None, "body": {
            "event": "adopted", "opened_turn": 1, "source": "frontier",
            "result": {"candidate_finding": {"finding_id": "f-1", "reason": None}}}},
        {"kind": "finding", "author": "frontier", "turn": None, "body": _finding(title)},
        {"kind": "help", "author": "core", "turn": None, "body": {
            "event": "adopted", "opened_turn": 2, "source": "frontier",
            "result": {"candidate_finding": {"finding_id": "f-2",
                                             "reason": "not recorded: cited observations with no trace entry: [99]"}}}},
        {"kind": "end", "author": "harness", "turn": None, "body": {"status": "complete", "turns": 3}},
    ]
    lines = [json.dumps({"trace_seq": i, "wall_utc": _at(i), **entry}) for i, entry in enumerate(entries)]
    _write(run / "trace.jsonl", ("\n".join(lines) + "\n").encode("utf-8"))
    _write(run / "run.json", {"status": "complete", "ending": "finish",
                              "s1": {"provider": "p", "model": "m", "calls": 2, "call_cap": 10},
                              "frontier": {"enabled": True, "model": "fm", "calls": 1, "call_cap": 3},
                              "manifest": {"sha256": "a" * 64, "origin": "test manifest"},
                              "adherence": {"rate": 1.0, "violations": []}})
    clip = _write(run / "clips" / "t1" / "clip.mp4", b"clip bytes")
    _write(run / "calls" / "1.json", {"arm": "a", "status": "finished", "cost_usd": 0.001, "reserved_usd": "0.002"})
    _write(run / "calls" / "2.json", {"arm": "a", "status": "failed", "cost_usd": None, "reserved_usd": "0.002"})
    _write(run / "calls" / "3.json", {"role": "frontier", "arm": "fm", "request_turn": 1, "status": "finished",
                                      "cost_usd": frontier_cost, "reserved_usd": frontier_reserved,
                                      "clip": {"sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
                                               "encoded_duration_s": 3.9, "caveat": "approximate"}})
    _write(run / "recording" / "session.mp4", b"video")
    _write(run / "recording" / "recording.json", {
        "video": {"file": "session.mp4", "sha256": "v", "encoded_duration_s": 60.0},
        "first_frame_utc": _at(0), "last_frame_utc": _at(60), "caveat": "approximate"})
    return run


def _replay(tmp_path: Path, name: str, trace: Path, *, source: str = "finding", update: bool = True,
            k: int = 1, n: int = 1) -> Path:
    folder = tmp_path / name
    check = ({"field": "public.speed", "op": ">=", "value": 9, "tolerance": None} if source == "finding"
             else {"field": "public.speed", "op": "changed", "value": None, "tolerance": None})
    _write(folder / "plan.json", {"trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(), "finding_id": "f-1",
                                  "finding": _finding(),
                                  "repro_check": check, "repro_check_source": source, "repro_check_kind": "number",
                                  "inputs": [{}, {}], "skipped_inputs": {"rejected": 1}, "evidence_turn": 1})
    attempts = [
        {"number": 1, "valid": True, "result": "reproduced", "reason": "a later reading satisfied it",
         "baseline": 1.0, "baseline_satisfied": False, "readings": [{}, {}], "inputs": [{"deviation_s": 0.02}]},
        {"number": 2, "valid": True, "result": "unknown", "reason": "the baseline could not be scored",
         "baseline": None, "baseline_satisfied": None, "readings": [], "inputs": [{"deviation_s": -0.01}]},
        {"number": 3, "valid": False, "result": None, "reason": "the replay diverged at turn 0",
         "baseline": 1.0, "baseline_satisfied": False, "readings": [], "inputs": [{"deviation_s": 0.0}]},
    ]
    _write(folder / "reproduce.json", {
        "finding_id": "f-1", "model_calls": 0, "status": "measured_with_gaps", "repro_check": check,
        "repro_check_source": source, "reset": "scene_reload", "clock_mode": "realtime", "attempts": attempts,
        "summary": {"planned": 3, "attempted": 3, "valid": 2, "k": k, "n": n, "unknown": 1,
                    "invalid": [{"number": 3, "reason": "the replay diverged at turn 0"}],
                    "input_timing": {"max_abs_deviation_s": 0.02, "inputs_replayed": 3}},
        "finding_update": {**_finding(), "state": "reproduced"} if update else None,
        "finding_update_reason": None if update else "an operator check overrode the finding's own, so the "
                                                     "finding's state is unchanged",
        "finished_at_utc": _at(100)})
    return folder


def _built(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    run = _run_folder(tmp_path, **kwargs)
    trace = run / "trace.jsonl"
    other = _write(tmp_path / "other-trace.jsonl", b"{}\n")
    dirs = [_replay(tmp_path, "own", trace), _replay(tmp_path, "control", trace, source="operator", update=False,
                                                     k=3, n=3),
            _replay(tmp_path, "elsewhere", other)]
    return report.build_report(run, reproduce_dirs=dirs)


def test_a_reproduced_finding_keeps_its_record_and_gains_what_the_replays_measured(tmp_path):
    built = _built(tmp_path)
    (f,) = built["findings"]
    assert (f["finding_id"], f["recorded_state"], f["state"]) == ("f-1", "candidate", "reproduced")
    assert f["reproduced"] == {"k": 1, "n": 1}          # only the replay that may change the state counts
    assert (f["expected_source"], f["severity"], f["steps"]) == ("developer_assertion", "major",
                                                                 ["Reload the scene", "Move once"])
    assert f["evidence"] == [{"trace_seq": 3, "kind": "observation", "turn": 1, "bridge_seq": 11, "game_s": 2.0,
                              "wall_utc": _at(2), "scene": "Main", "video_s": 2.0, "check_field": "public.speed",
                              "check_value": 9.5}]
    assert f["clip"]["path"] == "clips/t1/clip.mp4" and f["clip"]["sha256_matches"] is True
    assert f["proposed_at_request_turn"] == 1 and f["author"] == "frontier"


def test_failed_unscored_and_control_replays_stay_visible_and_only_the_findings_own_check_moves_it(tmp_path):
    built = _built(tmp_path)
    own, control = built["findings"][0]["replays"]
    assert [a["result"] for a in own["attempts"]] == ["reproduced", "unknown", "invalid"]
    assert own["attempts"][2]["reason"] == "the replay diverged at turn 0" and own["changes_state"] is True
    assert control["check_source"] == "operator" and control["k"] == 3 and control["changes_state"] is False
    assert "overrode" in control["finding_update_reason"]
    assert built["unmatched_replays"][0]["reason"].startswith("made from a different trace")
    notes = built["findings"][0]["remaining_uncertainty"]
    assert notes[0] == "Frontier: One sample only"
    assert any("1 of 3 attempts unknown" in note for note in notes)
    assert any("1 attempts invalid" in note for note in notes)


def test_an_older_control_report_that_claims_a_reproduction_cannot_move_the_finding(tmp_path):
    run = _run_folder(tmp_path)
    old = _replay(tmp_path, "old-control", run / "trace.jsonl", source="operator", update=True, k=3, n=3)
    built = report.build_report(run, reproduce_dirs=[old])
    (f,) = built["findings"]
    assert f["state"] == "candidate" and f["reproduced"] is None
    assert f["replays"][0]["changes_state"] is False and "overrode" in f["replays"][0]["finding_update_reason"]


def test_a_proposal_the_runner_could_not_record_is_listed_with_its_reason(tmp_path):
    built = _built(tmp_path)
    assert built["proposed_not_recorded"] == [{"finding_id": "f-2", "request_turn": 2, "help_trace_seq": 6,
                                               "reason": "not recorded: cited observations with no trace entry: [99]"}]


def test_cost_keeps_unknown_calls_apart_from_measured_ones(tmp_path):
    cost = _built(tmp_path)["cost"]
    assert (cost["s1"]["calls"], cost["s1"]["measured_calls"], cost["s1"]["measured_usd"]) == (2, 1, 0.001)
    assert (cost["s1"]["unknown_calls"], cost["s1"]["unknown_reserved_usd"]) == (1, 0.002)
    assert cost["frontier"]["measured_usd"] == 0.01 and cost["replays"] == {"runs": 2, "model_calls": 0}
    assert cost["total"] == {"calls": 3, "measured_usd": 0.011, "unknown_calls": 1, "upper_bound_usd": 0.013}
    unbounded = report.build_report(_run_folder(tmp_path / "u", frontier_cost=None, frontier_reserved=None))["cost"]
    assert unbounded["frontier"]["unknown_reserved_usd"] is None and unbounded["total"]["upper_bound_usd"] is None
    assert unbounded["total"]["measured_usd"] == 0.001        # an unknown cost is never counted as zero


def test_the_markdown_shows_every_attempt_the_cost_and_escapes_table_cells(tmp_path):
    built = _built(tmp_path, title="Speed | jumps")
    md = report.render_markdown(built)
    assert "## f-1: Speed \\| jumps" in md
    assert "| State | reproduced (1 of 1); recorded as candidate |" in md
    assert "| 3 | invalid | 1.0 | the replay diverged at turn 0 |" in md
    assert "Did not change the finding's state: an operator check overrode" in md
    assert "game_s 2.00, scene Main, `public.speed` = 9.5, 0:02.0 into the session video" in md
    assert "## Proposed but not recorded" in md and "## Replays not applied" in md
    assert "| Total | 3 | $0.0110 | 1 (upper bound $0.0130) |" in md


def test_a_run_without_findings_still_reports_its_configuration_and_cost(tmp_path):
    run = _run_folder(tmp_path)
    lines = [line for line in (run / "trace.jsonl").read_text(encoding="utf-8").splitlines()
             if json.loads(line)["kind"] != "finding"]
    (run / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    built = report.build_report(run)
    assert built["findings"] == [] and built["run"]["game_commit"] == "abc1234def"
    assert "No findings were recorded in this run." in report.render_markdown(built)


def test_the_command_writes_both_files_to_a_new_folder_and_refuses_an_existing_one(tmp_path, capsys):
    run = _run_folder(tmp_path)
    out = tmp_path / "report"
    assert report.main(["--run", str(run), "--output", str(out)]) == 0
    assert json.loads((out / "report.json").read_text(encoding="utf-8"))["kind"] == "playtest_report"
    assert (out / "report.md").read_text(encoding="utf-8").startswith("# Playtest report: Game at abc1234")
    assert report.main(["--run", str(run), "--output", str(out)]) == 1
    assert report.main(["--run", str(run), "--finding", "f-9", "--output", str(tmp_path / "x")]) == 1
    assert "no finding f-9" in capsys.readouterr().err
