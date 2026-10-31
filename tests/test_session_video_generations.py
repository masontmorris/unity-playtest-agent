"""The generation-mode recorder: each play instance's own frames, never a previous generation's.

The frames are real files in a temporary project and the host's records are real control files; only the
encoder is a stub, so these tests never need ffmpeg. A host transition is written by the recorder's own
injected sleep, exactly where a live host would publish it: after the recorder has read the previous
state and while it waits for the next sweep.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from playtest.frontier import media
from playtest.runner import session_video as video

JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"
SECOND_NS = 1_000_000_000
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
RUN = "run-generations-1"
SCENE = "Assets/Scenes/Game.unity"
G1, G2, G3 = (str(uuid.uuid4()) for _ in range(3))


def test_cli_keeps_a_summary_when_the_host_never_became_ready(tmp_path):
    project, _ = _project(tmp_path)
    host = Host(tmp_path / "control", project)
    stop = tmp_path / "stop"
    stop.write_text("")
    output = tmp_path / "recording"

    assert video.main(["--project", str(project), "--output", str(output),
                       "--stop-file", str(stop), "--control-directory", str(host.directory)]) == 0

    summary = json.loads((output / video.SUMMARY_NAME).read_text())
    assert summary["frames_copied"] == 0
    assert summary["video"] is None


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "game"
    source = project / media.FRAME_SUBDIR
    source.mkdir(parents=True)
    return project, source


def _frame(directory: Path, sequence: int, at_ns: int, data: bytes = JPEG) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"f-{sequence:06d}.jpg"
    path.write_bytes(data)
    path.stat()
    import os
    os.utime(path, ns=(at_ns, at_ns))
    return path


class Host:
    """The control directory the wrapper would create, with atomically published JSON records."""

    def __init__(self, directory: Path, project: Path, *, run_id: str = RUN) -> None:
        self.directory, self.run_id = directory, run_id
        directory.mkdir(parents=True)
        self._write("config.json", {
            "version": 1, "run_id": run_id, "project_path": str(project),
            "catalog_path": str(project / "catalog.json"), "catalog_sha256": "a" * 64,
            "deadline_utc": (NOW + timedelta(hours=1)).isoformat(),
        })

    def _write(self, name: str, body: dict[str, Any]) -> None:
        (self.directory / name).write_text(json.dumps(body), encoding="utf-8")

    def ready(self, generation: str, *, seconds: int = 1, run_id: str | None = None) -> None:
        self._write("host-state.json", {
            "version": 1, "run_id": run_id or self.run_id, "phase": "ready", "generation": generation,
            "scene_path": SCENE, "scene_handle": 7,
            "ready_utc": (NOW + timedelta(seconds=seconds)).isoformat(), "error": None,
            "capabilities": ["restart_play_mode"],
        })


def _encoder(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(frames: list[dict[str, Any]], directory: Path, **options: Any) -> dict[str, Any]:
        calls.append({"sequences": [frame["sequence"] for frame in frames], "directory": directory, **options})
        name = options.get("video_name", video.VIDEO_NAME)
        return {"video": {"file": name, "sha256": "0" * 64, "bytes": 1, "mime": "video/mp4",
                          "frame_count": len(frames), "target_duration_s": 0.0,
                          "encoded_duration_s": None, "durations_floored": 0,
                          "final_frame_hold_s": 0.1},
                "reason": None}

    monkeypatch.setattr(video, "encode", fake)
    return calls


class FakeTime:
    """The recorder's clock, advanced only by its own sleep, with a scripted host action per wait."""

    def __init__(self, actions: list[Any]) -> None:
        self.now = 0.0
        self.actions = deque(actions)
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.actions:
            self.actions.popleft()()

    def time_ns(self) -> int:
        return time.time_ns()


def _clock(monkeypatch: pytest.MonkeyPatch, actions: list[Any]) -> FakeTime:
    """Replace the recorder's time module with a fake clock; it never really waits."""
    fake = FakeTime(actions)
    monkeypatch.setattr(video, "time", fake)
    return fake


def _run(project: Path, tmp_path: Path, control: Path, stop: Path) -> dict[str, Any]:
    return video.record_generations(project, tmp_path / "out", stop, control, interval_s=0.01,
                                    max_wall_s=60.0, ready_wait_s=30.0)


def test_each_published_generation_is_recorded_from_its_own_directory_only(tmp_path, monkeypatch):
    project, source = _project(tmp_path)
    host = Host(tmp_path / "control", project)
    calls = _encoder(monkeypatch)
    old = time.time_ns() - 600 * SECOND_NS

    # Frames from before this run, and a directory for a generation the host never published.
    _frame(source, 900, time.time_ns() + SECOND_NS)            # the legacy flat window, fresh mtime
    _frame(source / G3, 901, time.time_ns() + SECOND_NS)       # a foreign generation's directory
    _frame(source / G1, 1, old + 1)
    _frame(source / G1, 2, old + 2)
    (source / G1 / media.SELF_TEST_NAME).write_bytes(JPEG)
    (source / G1 / "f-000003.jpg.tmp").write_bytes(JPEG[:10])
    _frame(source / G2, 7, old + 10)                          # the counter starts again; sequences repeat
    _frame(source / G2, 8, old + 11)
    _frame(source / G2, 9, old + 12, data=b"not a jpeg")
    stop = tmp_path / "recording.stop"

    slept = _clock(monkeypatch, [
        lambda: host.ready(G1, seconds=1),
        lambda: host.ready(G2, seconds=5),
        lambda: host.ready(G1, seconds=1),       # the host's stale state must not reopen a closed generation
        lambda: stop.write_text("", encoding="utf-8"),
    ])
    summary = _run(project, tmp_path, host.directory, stop)

    assert summary["ended_by"] == "stop_file" and summary["mode"] == "generations"
    assert (summary["generation_count"], summary["boundaries"]) == (2, 1)
    assert [row["generation"] for row in summary["generations"]] == [G1, G2]
    assert summary["frames_copied"] == 4                 # 1, 2 from G1 and 7, 8 from G2; frame 9 is not a JPEG
    assert slept.slept                                   # it did wait between sweeps

    first, second = summary["generations"]
    assert (first["first_sequence"], first["last_sequence"]) == (1, 2)
    assert (second["first_sequence"], second["last_sequence"]) == (7, 8)
    assert [(row["generation"], row["sequence"]) for row in first["frames"]] == [(G1, 1), (G1, 2)]
    assert [(row["generation"], row["sequence"]) for row in second["frames"]] == [(G2, 7), (G2, 8)]
    assert second["unfinished_reads"] >= 1
    assert first["frames_directory"] == G1 and second["frames_directory"] == G2
    assert (tmp_path / "out" / "frames" / G1 / "frame-000001.jpg").read_bytes() == JPEG
    assert not (tmp_path / "out" / "frames" / G3).exists()     # nothing was read from the flat window or G3
    assert not any(
        frame["source"] in {"f-000900.jpg", "f-000901.jpg"}
        for row in summary["generations"]
        for frame in row["frames"]
    )
    # Every generation is encoded on its own, with its own name and frame list.
    assert [call["sequences"] for call in calls] == [[1, 2], [7, 8]]
    assert [call["relative"] for call in calls] == [f"frames/{G1}", f"frames/{G2}"]
    assert [call["video_name"] for call in calls] == [f"session-{G1}.mp4", f"session-{G2}.mp4"]
    assert summary["video"] is None and summary["reason"] == (
        "2 play generations were recorded; each has its own video")
    assert len(summary["videos"]) == 2
    json.dumps(summary, allow_nan=False)


def test_a_single_generation_keeps_the_session_mp4_name(tmp_path, monkeypatch):
    project, source = _project(tmp_path)
    host = Host(tmp_path / "control", project)
    calls = _encoder(monkeypatch)
    now = time.time_ns()
    _frame(source / G1, 1, now - SECOND_NS)
    _frame(source / G1, 2, now)
    stop = tmp_path / "recording.stop"
    _steps = _clock(monkeypatch, [lambda: host.ready(G1), lambda: stop.write_text("", encoding="utf-8")])

    summary = _run(project, tmp_path, host.directory, stop)

    assert summary["generation_count"] == 1 and summary["boundaries"] == 0
    assert calls[0]["video_name"] == "session.mp4" and calls[0]["relative"] == f"frames/{G1}"
    assert summary["video"] is not None and summary["video"]["file"] == "session.mp4"
    assert summary["reason"] is None
    assert summary["first_frame_utc"] is not None and summary["last_frame_utc"] is not None


def test_no_ready_generation_within_the_bound_never_falls_back_to_the_flat_window(tmp_path, monkeypatch):
    project, source = _project(tmp_path)
    host = Host(tmp_path / "control", project)
    calls = _encoder(monkeypatch)
    _frame(source, 1, time.time_ns())                         # fresh flat frames are still not this run's
    _frame(source, 2, time.time_ns() + 1)
    stop = tmp_path / "recording.stop"
    clock = _clock(monkeypatch, [])

    summary = video.record_generations(project, tmp_path / "out", stop, host.directory, interval_s=0.01,
                                       max_wall_s=60.0, ready_wait_s=0.05)

    assert summary["ended_by"] == "ready_timeout" and summary["generation_count"] == 0
    assert summary["frames_copied"] == 0 and summary["video"] is None and calls == []
    assert "flat window was not read" in summary["reason"] and len(clock.slept) == 5
    assert not (tmp_path / "out" / "frames").exists()


def test_a_ready_state_for_another_run_is_not_this_runs_generation(tmp_path, monkeypatch):
    project, source = _project(tmp_path)
    host = Host(tmp_path / "control", project)
    calls = _encoder(monkeypatch)
    _frame(source / G1, 1, time.time_ns())
    _frame(source / G1, 2, time.time_ns() + 1)
    stop = tmp_path / "recording.stop"
    _clock(monkeypatch, [lambda: host.ready(G1, run_id="another-run"),
                         lambda: stop.write_text("", encoding="utf-8")])

    summary = _run(project, tmp_path, host.directory, stop)

    assert summary["generation_count"] == 0 and calls == []
    assert summary["frames_copied"] == 0 and summary["ended_by"] == "stop_file"


def test_the_command_line_takes_one_way_of_choosing_frames(tmp_path):
    project, _ = _project(tmp_path)
    control = tmp_path / "control"
    control.mkdir()
    stop = tmp_path / "stop"
    with pytest.raises(SystemExit):
        video.main(["--project", str(project), "--output", str(tmp_path / "out"), "--stop-file", str(stop),
                    "--generation", G1, "--control-directory", str(control)])
    with pytest.raises(SystemExit):
        video.main(["--project", str(project), "--output", str(tmp_path / "out"), "--stop-file", str(stop),
                    "--control-directory", str(tmp_path / "missing")])
    with pytest.raises(SystemExit):
        video.main(["--project", str(project), "--output", str(tmp_path / "out"), "--stop-file", str(stop),
                    "--control-directory", str(control), "--ready-wait-s", "0"])


def test_the_wrapper_passes_the_same_control_directory_to_the_recorder(tmp_path):
    """The seam is the wrapper's own control directory: no second convention for frames."""
    script = (Path(__file__).resolve().parents[1] / "scripts/unity/run-session.ps1").read_text(encoding="utf-8")
    control = script[script.index("$controlDirectory = Join-Path $output 'control'"):]
    record = script[script.index("if ($RecordFrames) {"):]
    assert "$controlDirectory = Join-Path $output 'control'" in control
    assert "'-ControlDirectory', $controlDirectory" in script            # the supervisor reads it
    assert '--control-directory "' in record and '$controlDirectory' in record[record.index("--control-directory"):]
    assert record.index("--control-directory") < record.index("Start-Process")
