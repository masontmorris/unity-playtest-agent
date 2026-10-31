"""The System Zero witness: one video attachment, fixed questions, refusals kept unknown, clips within limits."""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from playtest.bench import system_zero
from playtest.runner import witness


def test_the_request_sends_the_clip_alone_with_the_fixed_questions_and_measured_inputs():
    body = witness.request(b"\x00\x00\x00\x18ftypmp42", duration_s=10.0, speed=0.5,
                           inputs=[{"t_s": 1.0, "action": "move_forward", "held_s": 0.8}])
    assert set(body) == {"model", "questions", "state", "attachments"} and body["model"] == system_zero.MODEL
    assert body["questions"] == witness.QUESTIONS and list(body["questions"]) == ["problem_visible", "kind", "when"]
    (attachment,) = body["attachments"]
    assert attachment["type"] == "video" and attachment["name"] == "clip"
    assert base64.b64decode(attachment["data"].removeprefix("data:video/mp4;base64,")) == b"\x00\x00\x00\x18ftypmp42"
    assert body["state"]["clip"]["playback_speed"] == 0.5 and body["state"]["inputs"][0]["action"] == "move_forward"
    # Unmeasured inputs are null, never an empty list that would claim "no input".
    assert witness.request(b"x", duration_s=4.0, speed=1.0, inputs=None)["state"]["inputs"] is None


def test_choice_questions_fit_the_video_option_limit():
    for question in witness.QUESTIONS.values():
        if question["type"] == "choice":
            assert system_zero.MIN_OPTIONS <= len(question["criteria"]) <= witness.VIDEO_CHOICE_OPTIONS


def test_a_refused_or_missing_answer_is_unknown_never_no_problem():
    data = {"answers": {"problem_visible": {"error": {"code": "question_not_grounded", "retryable": False}},
                        "kind": {"type": "choice", "choice": "object_frozen", "probabilities": {"object_frozen": 1.0}}}}
    got = witness.decode(data)
    assert got["problem_visible"] == {"unknown": "refused: question_not_grounded"}
    assert got["kind"]["decision"] == "object_frozen" and got["when"] == {"unknown": "no valid answer"}
    assert witness.decode(None)["problem_visible"] == {"unknown": "no valid answer"}
    ok = witness.decode({"answers": {"problem_visible": {"type": "boolean", "decision": False, "probability": 0.1}}})
    assert ok["problem_visible"] == {"decision": False, "probability": 0.1}


def test_a_clip_longer_than_system_zero_takes_is_refused_before_encoding(tmp_path: Path):
    with pytest.raises(ValueError, match="exceeds"):
        witness.cut_clip(tmp_path / "in.mp4", 0.0, 6.0, tmp_path / "out.mp4", speed=0.5)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_clip_is_cut_silent_and_at_the_requested_speed(tmp_path: Path):
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
    half = witness.cut_clip(source, 1.0, 4.0, tmp_path / "half.mp4", speed=0.5)
    assert 7.9 <= half["duration_s"] <= 8.1 and half["bytes"] > 0 and half["speed"] == 0.5   # measured, not assumed
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                            "-of", "csv=p=0", str(tmp_path / "half.mp4")], capture_output=True, text=True, check=True)
    assert probe.stdout.strip().splitlines() == ["h264,video"]   # one H.264 stream and no audio
