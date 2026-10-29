"""The session recorder copies every bridge frame once, ignores what is not this session's, and keeps counter order.

Synthetic frames are written in the test's temporary folder; no media is committed. The test that encodes a real
MP4 skips when ffmpeg or ffprobe is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from playtest.frontier import media
from playtest.runner import session_video as video

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg and ffprobe are required")
JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"
SECOND_NS = 1_000_000_000


def _window(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "game"
    source = project / media.FRAME_SUBDIR
    source.mkdir(parents=True)
    return project, source


def _frame(source: Path, sequence: int, at_ns: int, data: bytes = JPEG) -> Path:
    path = source / f"f-{sequence:06d}.jpg"
    path.write_bytes(data)
    os.utime(path, ns=(at_ns, at_ns))
    return path


def _recorder(tmp_path: Path, source: Path, started_ns: int) -> video.Recorder:
    frames = tmp_path / "out" / "frames"
    frames.mkdir(parents=True)
    return video.Recorder(source, frames, started_ns)


def test_a_sweep_copies_each_new_frame_once_and_ignores_everything_that_is_not_one(tmp_path):
    _, source = _window(tmp_path)
    start = time.time_ns()
    for sequence in (1, 2, 3):
        _frame(source, sequence, start + sequence * SECOND_NS // 10)
    (source / media.SELF_TEST_NAME).write_bytes(JPEG)                  # the bridge's self-test image
    (source / "f-000004.jpg.tmp").write_bytes(JPEG[:10])                # a write still in progress
    _frame(source, 5, start + SECOND_NS, data=b"not a jpeg")           # unreadable: left for a later sweep
    _frame(source, 9, start - 10 * SECOND_NS)                           # an earlier session's frame
    recorder = _recorder(tmp_path, source, start)

    assert recorder.sweep() == 3
    assert recorder.sweep() == 0                                        # nothing is copied twice
    assert [frame["sequence"] for frame in recorder.ordered()] == [1, 2, 3]
    assert recorder.stale == {"f-000009.jpg"} and recorder.unreadable == 2
    copied = recorder.frames_dir / "frame-000002.jpg"
    assert copied.read_bytes() == JPEG and recorder.frames[2]["source"] == "f-000002.jpg"


def test_a_frame_the_window_evicted_first_is_a_counted_gap_never_a_filled_one(tmp_path):
    _, source = _window(tmp_path)
    start = time.time_ns()
    for sequence in (1, 2, 5, 6):
        _frame(source, sequence, start + sequence * SECOND_NS // 10)
    recorder = _recorder(tmp_path, source, start)
    recorder.sweep()
    assert recorder.gaps() == 2 and sorted(recorder.frames) == [1, 2, 5, 6]


def test_durations_follow_write_times_and_floor_an_out_of_order_write():
    frames = [{"mtime_ns": value} for value in (0, SECOND_NS // 10, SECOND_NS // 10, SECOND_NS // 20)]
    values, floored = video.durations(frames)
    assert values == pytest.approx([0.1, video.MIN_DURATION_S, video.MIN_DURATION_S, 1.0 / media.NOMINAL_RATE_HZ])
    assert floored == 2


def test_encoding_needs_two_frames_and_an_encoder(tmp_path):
    one = [{"file": "frame-000001.jpg", "mtime_ns": 0}]
    assert video.encode(one, tmp_path) == {"video": None, "reason": "fewer than two frames were copied"}
    two = one + [{"file": "frame-000002.jpg", "mtime_ns": SECOND_NS // 10}]
    assert video.encode(two, tmp_path, ffmpeg="no-such-encoder")["reason"] == "ffmpeg is not available"


def test_the_recorder_stops_at_its_wall_limit_and_says_why_there_is_no_video(tmp_path):
    project, _ = _window(tmp_path)
    summary = video.record(project, tmp_path / "out", tmp_path / "stop", interval_s=0.02, max_wall_s=0.1)
    assert summary["ended_by"] == "max_wall_s" and summary["frames_copied"] == 0
    assert summary["video"] is None and summary["reason"] == "fewer than two frames were copied"
    assert summary["first_sequence"] is None and summary["frames_missed_between"] == 0


@needs_ffmpeg
def test_a_recorded_session_is_encoded_in_counter_order_with_its_write_times(tmp_path):
    project, source = _window(tmp_path)
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10",
                    "-frames:v", "6", str(source / "f-%06d.jpg")], check=True)
    soon = time.time_ns() + 2 * SECOND_NS                              # after the recorder starts, so not stale
    for sequence in range(1, 7):
        path = source / f"f-{sequence:06d}.jpg"
        os.utime(path, ns=(soon + sequence * SECOND_NS // 10,) * 2)
    stop = tmp_path / "stop"
    stop.write_text("", encoding="utf-8")                              # one final sweep, then encode
    summary = video.record(project, tmp_path / "out", stop)

    assert summary["ended_by"] == "stop_file" and summary["frames_copied"] == 6
    assert (summary["first_sequence"], summary["last_sequence"], summary["frames_missed_between"]) == (1, 6, 0)
    clip = summary["video"]
    assert clip is not None and clip["frame_count"] == 6 and clip["durations_floored"] == 0
    assert clip["target_duration_s"] == pytest.approx(0.6)
    assert clip["encoded_duration_s"] == pytest.approx(0.6, abs=0.15)
    assert (tmp_path / "out" / video.VIDEO_NAME).stat().st_size == clip["bytes"]
    assert [frame["sequence"] for frame in summary["frames"]] == [1, 2, 3, 4, 5, 6]
    json.dumps(summary, allow_nan=False)


def test_the_command_line_refuses_an_output_inside_the_project_or_one_that_exists(tmp_path):
    project, _ = _window(tmp_path)
    stop = tmp_path / "stop"
    with pytest.raises(SystemExit):
        video.main(["--project", str(project), "--output", str(project / "rec"), "--stop-file", str(stop)])
    taken = tmp_path / "taken"
    taken.mkdir()
    with pytest.raises(SystemExit):
        video.main(["--project", str(project), "--output", str(taken), "--stop-file", str(stop)])
    with pytest.raises(SystemExit):
        video.main(["--project", str(project), "--output", str(tmp_path / "new"), "--stop-file", str(stop),
                    "--interval-s", "0"])
