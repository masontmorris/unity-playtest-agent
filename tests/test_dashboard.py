"""The dashboard: trace tailing, run discovery, evidence timing, frames, launch guards and the HTTP surface.

Everything runs offline on folders written here; no Unity, no model call, no child process.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from playtest.dashboard import frames, launch, runs
from playtest.dashboard.server import serve
from playtest.dashboard.trace_feed import TraceTail, short_label

GEN = "1a744c64-bb53-49f7-b96a-9d68b2ece041"
T0 = datetime(2026, 11, 4, 16, 0, 0, tzinfo=timezone.utc)
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64 + b"\xff\xd9"


def entry(seq: int, kind: str, body: dict, turn: int | None = None, at: float = 0.0) -> dict:
    return {"trace_seq": seq, "run_id": "r", "kind": kind, "author": "core", "turn": turn, "refs": [],
            "wall_utc": (T0 + timedelta(seconds=at)).isoformat(), "body": body}


def line(item: dict) -> bytes:
    return (json.dumps(item) + "\n").encode()


def write_session(folder: Path, entries: list[dict], *, ended: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    rows = list(entries)
    if ended:
        rows.append(entry(len(rows), "end", {"status": "complete", "error": None, "turns": 1, "redactions": 0}))
    (folder / "trace.jsonl").write_bytes(b"".join(line(row) for row in rows))
    return folder


FINDING = {"finding_id": "f-3", "state": "candidate", "title": "a control does nothing", "expected": "it moves",
           "expected_source": "developer_assertion", "actual": "it did not", "evidence_seq": [2], "clips": []}


def session_entries() -> list[dict]:
    return [
        entry(0, "run", {"run_id": "r", "game_project": "Game", "scene": "S", "objective": "play",
                         "models": [{"role": "s1", "provider": "p", "model": "m"}], "budget": {}}),
        entry(1, "choice_set", {"set_version": 1, "choices": [{"id": "go", "label": "go forward: hold w; more"}]},
              turn=0),
        entry(2, "decision", {"decision": {"choice_id": "go", "reason": None, "distribution": {"go": 0.9}},
                              "valid": True}, turn=0, at=10),
        entry(3, "turn", {"raw_choice": "go", "effective_choice": "go", "executed_action": "go",
                          "outcome": "executed", "latency_ms": 500}, turn=0, at=10.5),
        entry(4, "context", {"sha256": "x", "payload": {"big": "y" * 1000}}, turn=1),
        entry(5, "finding", FINDING, at=20),
    ]


# --- trace tail ----------------------------------------------------------------------------------------------


def test_tail_holds_a_partial_line_and_reports_corrupt_lines(tmp_path):
    trace = tmp_path / "trace.jsonl"
    rows = session_entries()
    trace.write_bytes(line(rows[0]) + line(rows[1]) + line(rows[2])[:20])
    tail = TraceTail(trace)
    assert [event["type"] for event in tail.read()] == ["run"]  # the choice set only feeds labels
    with trace.open("ab") as handle:
        handle.write(line(rows[2])[20:] + b"{not json\n" + line(rows[3]) + line(rows[4]))
    events = tail.read()
    assert [event["type"] for event in events] == ["decision", "corrupt", "turn"]  # context is never parsed
    assert events[0]["label"] == "go forward" and events[0]["probability"] == 0.9
    assert events[2]["label"] == "go forward" and events[2]["outcome"] == "executed"
    assert tail.read() == []


def test_tail_starts_again_when_the_file_is_replaced(tmp_path):
    trace = tmp_path / "trace.jsonl"
    rows = session_entries()
    trace.write_bytes(line(rows[0]) + line(rows[2]))
    tail = TraceTail(trace)
    assert len(tail.read()) == 2
    trace.write_bytes(line(rows[0]))
    assert [event["seq"] for event in tail.read()] == [0]


def test_short_label():
    assert short_label("toggle stance: hold ctrl; spread 0") == "toggle stance"
    assert short_label("") is None
    assert len(short_label("x" * 200)) == 90


# --- discovery and status ------------------------------------------------------------------------------------


def hunt_folder(root: Path, name: str = "h-01", sessions: int = 2) -> Path:
    folder = root / "hunts" / name
    folder.mkdir(parents=True)
    config = {key: "x" for key in ("project", "ref", "entity", "calibration", "manifest", "discovery", "approval")}
    (folder / "config.json").write_text(json.dumps({**config, "game": "Game", "sessions": sessions}))
    return folder


def test_discover_finds_hunts_and_sessions_but_not_sessions_inside_hunts(tmp_path):
    hunt = hunt_folder(tmp_path)
    write_session(hunt / "session-01", session_entries())
    write_session(tmp_path / "solo" / "live-1", session_entries())
    cards = {card["id"]: card for card in runs.discover(tmp_path)}
    assert set(cards) == {"hunts/h-01", "solo/live-1"}
    assert cards["hunts/h-01"]["kind"] == "hunt" and cards["hunts/h-01"]["sessions"] == 1
    assert cards["solo/live-1"]["status"] == "complete" and cards["solo/live-1"]["game"] == "Game"


def test_a_session_without_an_end_is_live_only_while_written(tmp_path):
    session = write_session(tmp_path / "s", session_entries(), ended=False)
    assert runs.session_status(session) == "live"
    old = datetime.now().timestamp() - 3600
    os.utime(session / "trace.jsonl", (old, old))
    assert runs.session_status(session) == "unknown"


def test_hunt_steps_follow_what_each_folder_holds(tmp_path):
    hunt = hunt_folder(tmp_path, sessions=2)
    (hunt / "guards").mkdir()
    (hunt / "guards" / "guards.json").write_text("{}")
    for part in ("all", "actions", "magnitudes"):
        (hunt / f"expect-{part}").mkdir()
        (hunt / f"expect-{part}" / "expectations.json").write_text("{}")
    write_session(hunt / "session-01", session_entries())
    (hunt / "session-01" / "run.json").write_text("{}")
    write_session(hunt / "session-02", session_entries(), ended=False)
    states = {step["key"]: step["state"] for step in runs.hunt_steps(hunt)}
    assert states == {"guards": "done", "predict-all": "done", "predict-actions": "done", "predict-magnitudes": "done",
                      "session-01": "done", "session-02": "running", "replays": "pending", "report": "pending"}


def test_a_step_left_without_a_result_when_the_hunt_moved_on(tmp_path):
    hunt = hunt_folder(tmp_path, sessions=2)
    write_session(hunt / "session-01", session_entries())  # no run.json
    write_session(hunt / "session-02", session_entries(), ended=False)
    states = {step["key"]: step["state"] for step in runs.hunt_steps(hunt)}
    assert states["session-01"] == "no result" and states["session-02"] == "running"


def test_spend_is_unknown_until_a_ledger_row_exists(tmp_path):
    assert runs.ledger_cost(tmp_path) is None
    (tmp_path / "calls").mkdir()
    (tmp_path / "calls" / "a.json").write_text(json.dumps({"status": "reserved"}))
    assert runs.ledger_cost(tmp_path) is None
    (tmp_path / "calls" / "b.json").write_text(json.dumps({"cost_usd": 0.25}))
    assert runs.ledger_cost(tmp_path) == 0.25


# --- findings and their evidence -----------------------------------------------------------------------------


def add_recording(session: Path) -> None:
    (session / "recording").mkdir(parents=True)
    (session / "recording" / "recording.json").write_text(json.dumps({
        "timestamp_basis": "frame_file_mtime_utc",
        "generations": [{"generation": GEN, "first_frame_utc": (T0 + timedelta(seconds=4)).isoformat(),
                         "last_frame_utc": (T0 + timedelta(seconds=60)).isoformat(),
                         "video": {"file": "session.mp4"}}]}))
    clip = session / "clips" / "t9"
    clip.mkdir(parents=True)
    (clip / "clip.mp4").write_bytes(b"0")
    (clip / "manifest.json").write_text(json.dumps({"window": {
        "since_utc": T0.isoformat(), "selected_start_utc": (T0 + timedelta(seconds=8)).isoformat(),
        "until_utc": (T0 + timedelta(seconds=12)).isoformat()}}))
    late = session / "clips" / "t30"
    late.mkdir()
    (late / "clip.mp4").write_bytes(b"0")
    (late / "manifest.json").write_text(json.dumps({"window": {
        "since_utc": T0.isoformat(), "selected_start_utc": (T0 + timedelta(seconds=40)).isoformat(),
        "until_utc": (T0 + timedelta(seconds=44)).isoformat()}}))


def test_a_finding_points_at_the_recording_moment_it_cites(tmp_path):
    session = write_session(tmp_path / "s", session_entries())
    add_recording(session)
    found = runs.session_findings(tmp_path, session)
    assert len(found) == 1
    evidence = found[0]["evidence"]
    assert evidence["cited_utc"] == (T0 + timedelta(seconds=10)).isoformat()  # trace_seq 2, the decision
    assert evidence["video"] == {"file": "recording/session.mp4", "offset_s": 6.0, "approximate": True,
                                 "basis": "frame_file_mtime_utc"}
    assert [clip["turn_folder"] for clip in evidence["clips"]] == ["t9"]  # only the clip whose window holds it
    assert [item["state"] for item in evidence["history"]] == ["candidate"]


def test_no_recording_means_no_video_rather_than_a_guess(tmp_path):
    session = write_session(tmp_path / "s", session_entries())
    assert runs.session_findings(tmp_path, session)[0]["evidence"]["video"] is None


def test_hunt_findings_prefer_the_report_and_its_replays(tmp_path):
    hunt = hunt_folder(tmp_path)
    write_session(hunt / "session-01", session_entries())
    report = {"findings": [{**FINDING, "session": "session-01", "replay": {"status": "measured", "k": 4, "n": 5}}],
              "unmet": ["a rule"], "cost_usd": 0.5, "sessions": ["session-01"]}
    (hunt / "hunt.json").write_text(json.dumps(report))
    result = runs.hunt_findings(tmp_path, hunt)
    assert result["final"] is True and result["unmet"] == ["a rule"]
    assert result["findings"][0]["replay"]["k"] == 4 and result["findings"][0]["session_id"] == "hunts/h-01/session-01"


# --- frames --------------------------------------------------------------------------------------------------


def live_session(tmp_path: Path, phase: str = "ready") -> tuple[Path, Path]:
    session = write_session(tmp_path / "s", session_entries(), ended=False)
    project = tmp_path / "project"
    window = project / "Temp" / "playtest-bridge-frames" / GEN
    window.mkdir(parents=True)
    (session / "control").mkdir()
    (session / "control" / "config.json").write_text(json.dumps({"project_path": str(project)}))
    (session / "control" / "host-state.json").write_text(json.dumps({"phase": phase, "generation": GEN}))
    return session, window


def test_latest_frame_is_the_highest_counter_and_skips_other_files(tmp_path):
    session, window = live_session(tmp_path)
    (window / "f-000002.jpg").write_bytes(JPEG)
    (window / "f-000010.jpg").write_bytes(JPEG + b"")
    (window / "f-000011.jpg.tmp").write_bytes(JPEG)
    (window / "self-test.jpg").write_bytes(JPEG)
    frame = frames.latest_frame(session)
    assert frame is not None and frame.source == "live" and frame.name == "f-000010.jpg"


def test_an_incomplete_frame_is_passed_over(tmp_path):
    session, window = live_session(tmp_path)
    (window / "f-000001.jpg").write_bytes(JPEG)
    (window / "f-000002.jpg").write_bytes(JPEG[:-2])
    assert frames.latest_frame(session).name == "f-000001.jpg"


def test_a_stopped_host_falls_back_to_the_recording(tmp_path):
    session, window = live_session(tmp_path, phase="stopped")
    (window / "f-000001.jpg").write_bytes(JPEG)
    assert frames.latest_frame(session) is None
    recorded = session / "recording" / "frames" / GEN
    recorded.mkdir(parents=True)
    (recorded / "frame-000007.jpg").write_bytes(JPEG)
    frame = frames.latest_frame(session)
    assert frame.source == "recording" and frame.name == "frame-000007.jpg"


# --- launch --------------------------------------------------------------------------------------------------

S1_ARM, FRONTIER = "liquid-d1-ultra", "muse-spark-1.3"


def ledger(folder: Path, arm: str, costs: list[float]) -> Path:
    (folder / "calls").mkdir(parents=True)
    for index, cost in enumerate(costs):
        (folder / "calls" / f"{arm}-{index}.json").write_text(json.dumps(
            {"arm": arm, "http_status": 200, "cost_usd": cost, "status": "finished"}))
    return folder


@pytest.fixture
def launch_root(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    (root / "hunts").mkdir(parents=True)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in ("entity", "calibration", "manifest", "discovery"):
        (inputs / f"{name}.json").write_text("{}")
    s1 = ledger(tmp_path / "ledger-s1", S1_ARM, [0.0002] * 9 + [0.0004])
    frontier = ledger(tmp_path / "ledger-frontier", FRONTIER, [0.02] * 9 + [0.03])
    (inputs / "approval.json").write_text(json.dumps({"arms": {
        S1_ARM: {"evidence": {"ledgers": [str(s1)]}}, FRONTIER: {"evidence": {"ledgers": [str(frontier)]}}}}))
    config = {"game": "Game", "project": str(tmp_path / "project"), "ref": "eval/x",
              **{name: f"inputs/{name}.json" for name in ("entity", "calibration", "manifest", "discovery",
                                                          "approval")}}
    (root / "hunts" / "game.json").write_text(json.dumps(config))
    monkeypatch.setattr(launch, "session_s1_arm", lambda: S1_ARM)
    prepared = []
    monkeypatch.setattr(launch, "readiness", lambda config: {"blockers": [], "prepare": [{"step": "wire", "text": ""}]})
    monkeypatch.setattr(launch, "prepare_project", lambda config, steps: prepared.append(steps))
    spawned = []

    def spawn(config_path, output, log):
        spawned.append((config_path, output, log))
        return FakeProcess()

    return root, launch.Launcher(root, spawn=spawn), spawned, prepared


class FakeProcess:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


def test_the_quote_is_every_allowed_call_at_the_measured_bound(launch_root):
    _, launcher, _, _ = launch_root
    quote = launcher.quote({"config": "game", "preset": "quick"})["quote"]
    # bounds: largest measured cost x 1.5 -> S1 0.0006, frontier 0.045
    assert quote["bounds"] == {"S1": 0.0006, "frontier": 0.045}
    # one session: 300 x 0.0006 + 6 x 0.045 = 0.45; plus 3 predictions x 0.045 = 0.135
    assert quote["session_cap_usd"] == 0.45 and quote["usd"] == 0.59 and quote["problems"] == []


def test_a_model_with_no_measured_calls_cannot_be_quoted(launch_root):
    _, launcher, _, _ = launch_root
    quote = launcher.quote({"config": "game", "settings": {"frontier_model": "muse-wonder-1.0"}})["quote"]
    assert quote["usd"] is None and "muse-wonder-1.0" in quote["problems"][0]


def test_launch_needs_the_exact_amount_then_prepares_writes_its_approval_and_starts(launch_root):
    root, launcher, spawned, prepared = launch_root
    usd = launcher.quote({"config": "game", "preset": "quick"})["quote"]["usd"]
    request = {"config": "game", "preset": "quick", "prompt": "  check the doors  ", "approved_usd": usd}
    with pytest.raises(launch.LaunchError, match="confirm the amount"):
        launcher.start({**request, "approved_usd": 0.01})
    with pytest.raises(launch.LaunchError, match="name"):
        launcher.start({**request, "name": "../escape"})
    with pytest.raises(launch.LaunchError, match="sessions"):
        launcher.start({**request, "settings": {"sessions": 99}})
    with pytest.raises(launch.LaunchError, match="prompt"):
        launcher.start({**request, "prompt": "x" * 3000})
    assert spawned == [] and prepared == []
    job = launcher.start(request)
    assert prepared == [[{"step": "wire", "text": ""}]]
    config_path, output, log = spawned[0]
    assert output == root / "hunts" / "game-01" and log == root / "hunts" / "game-01.log"
    derived = json.loads(config_path.read_text())
    assert derived["prompt"] == "check the doors" and derived["sessions"] == 1 and derived["max_calls"] == 300
    assert Path(derived["manifest"]).is_file()  # resolved against the repository that owns runs/
    approval = json.loads(Path(derived["approval"]).read_text())
    assert approval["approved_by"] == "user" and approval["max_cost_usd"] == 0.45
    assert {arm: limit["max_calls"] for arm, limit in approval["arms"].items()} == {S1_ARM: 300, FRONTIER: 6}
    assert "$0.59" in approval["unverified_balance_override"]["reason"]
    assert job.view()["running"] is True and job.view()["prompt"] == "check the doors"
    with pytest.raises(launch.LaunchError, match="still running"):
        launcher.start(request)
    job.process.code = 0
    with pytest.raises(launch.LaunchError, match="already exists"):
        launcher.start({**request, "name": "game-01"})


def test_a_blocker_refuses_the_launch_before_anything_changes(launch_root, monkeypatch):
    _, launcher, spawned, prepared = launch_root
    monkeypatch.setattr(launch, "readiness", lambda config: {"blockers": ["A Unity Editor has the game project open"],
                                                              "prepare": []})
    usd = launcher.quote({"config": "game"})["quote"]["usd"]
    with pytest.raises(launch.LaunchError, match="Editor"):
        launcher.start({"config": "game", "approved_usd": usd})
    assert spawned == [] and prepared == []


def test_readiness_lists_the_checkout_and_the_wiring_start_would_do(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "Packages").mkdir(parents=True)
    (project / "Packages" / "manifest.json").write_text(json.dumps(
        {"dependencies": {"com.playtest.bridge": "file:C:/elsewhere/bridge"}}))
    monkeypatch.setattr(launch.hunt_cli, "unity_projects", lambda: [])
    monkeypatch.setattr(launch.hunt_cli, "project_ref", lambda path: "main")
    monkeypatch.setattr(launch, "credential_check", lambda: [])
    ready = launch.readiness({"project": str(project), "ref": "eval/x", "missing_files": []})
    assert ready["blockers"] == []
    assert [step["step"] for step in ready["prepare"]] == ["checkout", "wire"]
    (project / "Packages" / "manifest.json").write_text(json.dumps(
        {"dependencies": {"com.playtest.bridge": launch.bridge_dependency()}}))
    monkeypatch.setattr(launch.hunt_cli, "project_ref", lambda path: "eval/x")
    monkeypatch.setattr(launch.hunt_cli, "unity_projects", lambda: [str(project).replace("\\", "/").lower()])
    ready = launch.readiness({"project": str(project), "ref": "eval/x", "missing_files": []})
    assert ready["prepare"] == [] and "Editor" in ready["blockers"][0]


# --- HTTP ----------------------------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    session = write_session(tmp_path / "solo" / "s-1", session_entries())
    add_recording(session)
    (session / "recording" / "session.mp4").write_bytes(bytes(range(256)) * 4)
    (tmp_path / "secret.txt").write_text("no")
    httpd = serve(tmp_path, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def request(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


def test_http_lists_runs_and_serves_recorded_files_by_range(server):
    response, data = request(server, "GET", "/api/runs")
    assert response.status == 200 and [run["id"] for run in json.loads(data)["runs"]] == ["solo/s-1"]
    response, data = request(server, "GET", "/api/file?run=solo/s-1&path=recording/session.mp4",
                             headers={"Range": "bytes=10-19"})
    assert response.status == 206 and data == bytes(range(10, 20))
    assert response.getheader("Content-Range") == "bytes 10-19/1024"
    response, data = request(server, "GET", "/")
    assert response.status == 200 and b"Playtest Agent" in data


@pytest.mark.parametrize("path", ["/api/file?run=solo/s-1&path=../../secret.txt",
                                  "/api/file?run=solo/s-1&path=trace.jsonl",
                                  "/api/run?run=../",
                                  "/static/../server.py"])
def test_http_serves_nothing_outside_the_run(server, path):
    response, _ = request(server, "GET", path)
    assert response.status == 404


def test_http_refuses_other_hosts_and_non_json_posts(server):
    response, _ = request(server, "GET", "/api/runs", headers={"Host": "attacker.example"})
    assert response.status == 403
    response, _ = request(server, "POST", "/api/launch", body=b"config=x",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status == 415
    response, _ = request(server, "POST", "/api/launch", body=b"{}",
                          headers={"Content-Type": "application/json", "Origin": "http://attacker.example"})
    assert response.status == 403


def test_http_streams_the_trace_until_its_end(server):
    response, data = request(server, "GET", "/api/events?run=solo/s-1&after=1")
    assert response.status == 200
    events = [json.loads(chunk[len("data: "):]) for chunk in data.decode().split("\n")
              if chunk.startswith("data: {\"")]
    assert [event["type"] for event in events] == ["decision", "turn", "finding", "end"]
    assert b"event: closed" in data


def test_http_frame_says_when_nothing_is_newer(server, tmp_path):
    response, _ = request(server, "GET", "/api/frame.jpg?run=solo/s-1")
    assert response.status == 404  # no frame recorded yet
    recorded = tmp_path / "solo" / "s-1" / "recording" / "frames" / GEN
    recorded.mkdir(parents=True)
    (recorded / "frame-000003.jpg").write_bytes(JPEG)
    response, data = request(server, "GET", "/api/frame.jpg?run=solo/s-1")
    assert response.status == 200 and data == JPEG and response.getheader("X-Frame-Name") == "frame-000003.jpg"
    response, data = request(server, "GET", "/api/frame.jpg?run=solo/s-1&after=recording/frame-000003.jpg")
    assert response.status == 204 and data == b""
