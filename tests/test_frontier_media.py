"""Clip capture from the bridge's JPEG window (#33).

Synthetic frames are produced by ffmpeg in the test's temporary folder; no media is committed.
Tests that inspect a real MP4 skip when ffmpeg or ffprobe is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from playtest.frontier import media

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg and ffprobe are required")
STATUS = SimpleNamespace(capture=True, capture_self_test="passed")


def _jpeg(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"color=c={color}:s=64x36:d=1", "-frames:v", "1", str(path)],
                   check=True, capture_output=True)


def _stamp(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def _frames(tmp_path: Path) -> Path:
    return tmp_path / "game" / media.FRAME_SUBDIR


def _generation_frames(tmp_path: Path, generation: str) -> Path:
    return media.frame_directory(tmp_path / "game", generation)


def _raw_frame(path: Path, when: datetime) -> None:
    """A listed JPEG-shaped file enough for selection tests that never encode a clip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8stub")
    _stamp(path, when)


def _capture(tmp_path: Path, *, since: datetime, until: datetime, ffmpeg: str = "ffmpeg",
             output: str = "clip"):
    return media.capture_clip(project=tmp_path / "game", status=STATUS, since_utc=since, until_utc=until,
                              output_dir=tmp_path / output, ffmpeg=ffmpeg)


@needs_ffmpeg
def test_a_measured_window_encodes_a_real_mp4_with_gap_durations_and_hashes(tmp_path: Path) -> None:
    frames = _frames(tmp_path)
    base = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    _jpeg(frames / "f-000001.jpg", "red")
    _jpeg(frames / "f-000002.jpg", "green")
    _jpeg(frames / "f-000003.jpg", "blue")
    _stamp(frames / "f-000001.jpg", base)
    _stamp(frames / "f-000002.jpg", base + timedelta(seconds=0.2))
    _stamp(frames / "f-000003.jpg", base + timedelta(seconds=1.2))

    clip = _capture(tmp_path, since=base - timedelta(seconds=5), until=base + timedelta(seconds=2))
    assert clip.reason is None and clip.path is not None and clip.manifest is not None
    descriptor = clip.descriptor
    assert descriptor is not None
    assert descriptor["frame_count"] == 3
    assert (descriptor["width"], descriptor["height"]) == (640, 360)
    assert descriptor["timestamp_basis"] == "frame_file_mtime_utc"
    assert descriptor["frame_game_s"] is None
    assert descriptor["source"] == "bridge_offscreen_composite" and descriptor["consumer"] == "frontier"
    assert descriptor["mime"] == "video/mp4"
    assert descriptor["target_duration_s"] == pytest.approx(1.3, abs=0.001)
    assert descriptor["encoded_duration_s"] == pytest.approx(1.3, abs=0.15)
    assert descriptor["nominal_duration_s"] == pytest.approx(0.3)
    assert descriptor["encoded_duration_s"] > descriptor["nominal_duration_s"] + 0.8  # the gap is not compressed
    assert descriptor["start_wall_utc"] == base.isoformat()
    assert descriptor["end_wall_utc"] == (base + timedelta(seconds=1.2)).isoformat()
    assert "approximate" in descriptor["caveat"]

    raw = clip.path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == descriptor["sha256"] and len(raw) == descriptor["bytes"]
    probe = json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_name,width,height,pix_fmt:format=duration",
         "-of", "json", str(clip.path)], capture_output=True, text=True, check=True).stdout)
    streams = probe["streams"]
    assert len(streams) == 1  # no audio
    assert streams[0]["codec_name"] == "h264"
    assert (streams[0]["width"], streams[0]["height"]) == (640, 360)
    assert streams[0]["pix_fmt"] == "yuv420p"
    assert float(probe["format"]["duration"]) == pytest.approx(1.3, abs=0.15)
    assert descriptor["encoded_duration_s"] == float(probe["format"]["duration"])

    manifest = json.loads((tmp_path / "clip" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["timestamp_basis"] == media.TIMESTAMP_BASIS
    assert [frame["source"] for frame in manifest["frames"]] == ["f-000001.jpg", "f-000002.jpg", "f-000003.jpg"]
    assert [frame["mtime_utc"] for frame in manifest["frames"]] == [
        base.isoformat(), (base + timedelta(seconds=0.2)).isoformat(), (base + timedelta(seconds=1.2)).isoformat()]
    for frame in manifest["frames"]:
        source = (frames / frame["source"]).read_bytes()
        assert frame["sha256"] == hashlib.sha256(source).hexdigest() and frame["bytes"] == len(source)
    assert manifest["source_directory"] == "Temp/playtest-bridge-frames"


@needs_ffmpeg
def test_the_window_is_bounded_by_the_episode_start_and_the_escalation_time(tmp_path: Path) -> None:
    frames = _frames(tmp_path)
    until = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    _jpeg(frames / "f-000001.jpg", "red")
    _jpeg(frames / "f-000002.jpg", "green")
    _jpeg(frames / "f-000003.jpg", "blue")
    _jpeg(frames / "f-000004.jpg", "yellow")
    _stamp(frames / "f-000001.jpg", until - timedelta(hours=1))       # an older session
    _stamp(frames / "f-000002.jpg", until - timedelta(seconds=2))
    _stamp(frames / "f-000003.jpg", until - timedelta(seconds=0.5))
    _stamp(frames / "f-000004.jpg", until + timedelta(seconds=2))     # after the observation
    clip = _capture(tmp_path, since=until - timedelta(seconds=10), until=until)
    assert clip.reason is None and clip.descriptor is not None
    assert clip.descriptor["frame_count"] == 2
    manifest = json.loads((tmp_path / "clip" / "manifest.json").read_text(encoding="utf-8"))
    assert [frame["source"] for frame in manifest["frames"]] == ["f-000002.jpg", "f-000003.jpg"]


@needs_ffmpeg
def test_capture_orders_frames_by_measured_time_not_by_name(tmp_path: Path) -> None:
    frames = _frames(tmp_path)
    until = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    _jpeg(frames / "f-000001.jpg", "red")
    _jpeg(frames / "f-000002.jpg", "green")
    _stamp(frames / "f-000001.jpg", until - timedelta(seconds=0.2))
    _stamp(frames / "f-000002.jpg", until - timedelta(seconds=1.0))
    clip = _capture(tmp_path, since=until - timedelta(seconds=5), until=until)
    assert clip.manifest is not None
    assert [frame["source"] for frame in clip.manifest["frames"]] == ["f-000002.jpg", "f-000001.jpg"]


def test_capture_gates_on_status_and_self_test_without_creating_a_clip(tmp_path: Path) -> None:
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    for status, needle in [
        (None, "status is unavailable"),
        (SimpleNamespace(capture=False, capture_self_test="passed"), "capture is disabled"),
        (SimpleNamespace(capture=True, capture_self_test="failed"), "self-test did not pass"),
        (SimpleNamespace(capture=True, capture_self_test="unknown"), "self-test did not pass"),
    ]:
        clip = media.capture_clip(project=tmp_path / "game", status=status, since_utc=since,
                                  until_utc=since + timedelta(seconds=1), output_dir=tmp_path / "clip")
        assert clip.descriptor is None and clip.path is None and needle in (clip.reason or "")
    assert not (tmp_path / "clip").exists()


@needs_ffmpeg
def test_missing_or_ambiguous_frames_return_a_reason_not_a_video(tmp_path: Path) -> None:
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    missing = media.capture_clip(project=tmp_path / "game", status=STATUS, since_utc=since,
                                 until_utc=since + timedelta(seconds=1), output_dir=tmp_path / "none")
    assert missing.descriptor is None and "frame folder is unavailable" in (missing.reason or "")

    frames = _frames(tmp_path)
    _jpeg(frames / "f-000001.jpg", "red")
    _stamp(frames / "f-000001.jpg", since)
    single = media.capture_clip(project=tmp_path / "game", status=STATUS, since_utc=since,
                                until_utc=since + timedelta(seconds=1), output_dir=tmp_path / "one")
    assert single.descriptor is None and "fewer than two" in (single.reason or "")

    _jpeg(frames / "f-000002.jpg", "green")
    _stamp(frames / "f-000002.jpg", since)
    same = media.capture_clip(project=tmp_path / "game", status=STATUS, since_utc=since,
                              until_utc=since + timedelta(seconds=1), output_dir=tmp_path / "same")
    assert same.descriptor is None and "no distinct modification times" in (same.reason or "")


@needs_ffmpeg
def test_missing_or_failing_ffmpeg_returns_a_reason(tmp_path: Path) -> None:
    frames = _frames(tmp_path)
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    _jpeg(frames / "f-000001.jpg", "red")
    _jpeg(frames / "f-000002.jpg", "green")
    _stamp(frames / "f-000001.jpg", since)
    _stamp(frames / "f-000002.jpg", since + timedelta(seconds=0.1))
    missing = _capture(tmp_path, since=since, until=since + timedelta(seconds=1),
                       ffmpeg=str(tmp_path / "no-such-ffmpeg.exe"), output="missing-encoder")
    assert missing.descriptor is None and "ffmpeg is not available" in (missing.reason or "")
    failing = _capture(tmp_path, since=since, until=since + timedelta(seconds=1), ffmpeg=sys.executable,
                       output="failing-encoder")
    assert failing.descriptor is None and "could not encode" in (failing.reason or "")


@needs_ffmpeg
def test_a_frame_that_changes_during_the_read_is_an_explicit_failure(tmp_path: Path, monkeypatch) -> None:
    frames = _frames(tmp_path)
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    _jpeg(frames / "f-000001.jpg", "red")
    _jpeg(frames / "f-000002.jpg", "green")
    _stamp(frames / "f-000001.jpg", since)
    _stamp(frames / "f-000002.jpg", since + timedelta(seconds=0.1))
    original = Path.read_bytes

    def racy(path: Path) -> bytes:
        data = original(path)
        if path.name == "f-000002.jpg":
            later = since + timedelta(seconds=0.5)
            os.utime(path, (later.timestamp(), later.timestamp()))
        return data

    monkeypatch.setattr(Path, "read_bytes", racy)
    clip = _capture(tmp_path, since=since, until=since + timedelta(seconds=1))
    assert clip.descriptor is None and "changed while it was read" in (clip.reason or "")


@needs_ffmpeg
def test_a_frame_evicted_after_the_listing_is_skipped_not_fatal(tmp_path: Path, monkeypatch) -> None:
    frames = _frames(tmp_path)
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    for index, color in enumerate(["red", "green", "blue"], 1):
        _jpeg(frames / f"f-{index:06d}.jpg", color)
        _stamp(frames / f"f-{index:06d}.jpg", since + timedelta(seconds=0.1 * index))
    original = Path.is_file

    def evicting(path: Path) -> bool:
        present = original(path)
        if present and path.name == "f-000002.jpg":
            path.unlink()          # the rolling window removes the frame after the listing saw it
        return present

    monkeypatch.setattr(Path, "is_file", evicting)
    clip = _capture(tmp_path, since=since, until=since + timedelta(seconds=1))
    assert clip.reason is None and clip.descriptor is not None and clip.manifest is not None
    assert [frame["source"] for frame in clip.manifest["frames"]] == ["f-000001.jpg", "f-000003.jpg"]
    assert clip.manifest["frames_skipped"] == 1
    assert clip.descriptor["frame_count"] == 2


@needs_ffmpeg
def test_a_frame_evicted_after_the_scan_is_skipped_not_fatal(tmp_path: Path, monkeypatch) -> None:
    frames = _frames(tmp_path)
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    for index, color in enumerate(["red", "green", "blue"], 1):
        _jpeg(frames / f"f-{index:06d}.jpg", color)
        _stamp(frames / f"f-{index:06d}.jpg", since + timedelta(seconds=0.1 * index))
    original = Path.read_bytes

    def evicting(path: Path) -> bytes:
        if path.name == "f-000003.jpg" and path.parent == frames:
            path.unlink()          # the rolling window removes the frame between the scan and the copy
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", evicting)
    clip = _capture(tmp_path, since=since, until=since + timedelta(seconds=1))
    assert clip.reason is None and clip.descriptor is not None and clip.manifest is not None
    assert [frame["source"] for frame in clip.manifest["frames"]] == ["f-000001.jpg", "f-000002.jpg"]
    assert [frame["file"] for frame in clip.manifest["frames"]] == ["frame-000001.jpg", "frame-000002.jpg"]
    assert clip.manifest["frames_skipped"] == 1
    assert clip.descriptor["frame_count"] == 2


@needs_ffmpeg
def test_an_unusable_window_is_relisted_only_a_bounded_number_of_times(tmp_path: Path, monkeypatch) -> None:
    frames = _frames(tmp_path)
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    _jpeg(frames / "f-000001.jpg", "red")
    _stamp(frames / "f-000001.jpg", since)
    original = Path.iterdir
    scans = {"count": 0}

    def counting(path: Path):
        if path == frames:
            scans["count"] += 1
        return original(path)

    monkeypatch.setattr(Path, "iterdir", counting)
    clip = _capture(tmp_path, since=since, until=since + timedelta(seconds=1))
    assert clip.descriptor is None and "fewer than two" in (clip.reason or "")
    assert scans["count"] == media.WINDOW_ATTEMPTS
    assert not (tmp_path / "clip").exists()


@needs_ffmpeg
def test_the_capture_self_test_image_is_never_a_frame(tmp_path: Path) -> None:
    frames = _frames(tmp_path)
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    _jpeg(frames / "f-000001.jpg", "red")
    _jpeg(frames / "f-000002.jpg", "green")
    _jpeg(frames / media.SELF_TEST_NAME, "black")
    _stamp(frames / "f-000001.jpg", since + timedelta(seconds=0.1))
    _stamp(frames / "f-000002.jpg", since + timedelta(seconds=0.2))
    _stamp(frames / media.SELF_TEST_NAME, since + timedelta(seconds=0.15))
    clip = _capture(tmp_path, since=since, until=since + timedelta(seconds=1))
    assert clip.manifest is not None
    assert [frame["source"] for frame in clip.manifest["frames"]] == ["f-000001.jpg", "f-000002.jpg"]
    assert clip.manifest["frames_skipped"] == 0


@needs_ffmpeg
def test_a_second_capture_into_the_same_folder_never_overwrites_it(tmp_path: Path) -> None:
    frames = _frames(tmp_path)
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    _jpeg(frames / "f-000001.jpg", "red")
    _jpeg(frames / "f-000002.jpg", "green")
    _stamp(frames / "f-000001.jpg", since)
    _stamp(frames / "f-000002.jpg", since + timedelta(seconds=0.1))
    first = _capture(tmp_path, since=since, until=since + timedelta(seconds=1))
    assert first.descriptor is not None
    again = _capture(tmp_path, since=since, until=since + timedelta(seconds=1))
    assert again.descriptor is None and "already exists" in (again.reason or "")


def test_a_generation_reads_only_its_own_frame_directory(tmp_path: Path) -> None:
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    current, prior = str(uuid.uuid4()), str(uuid.uuid4())
    _raw_frame(_frames(tmp_path) / "f-000001.jpg", since)
    _raw_frame(_frames(tmp_path) / "f-000002.jpg", since + timedelta(seconds=0.1))
    _raw_frame(_generation_frames(tmp_path, prior) / "f-000003.jpg", since)
    _raw_frame(_generation_frames(tmp_path, prior) / "f-000004.jpg", since + timedelta(seconds=0.1))
    _raw_frame(_generation_frames(tmp_path, current) / "f-000005.jpg", since)
    only_one = media.capture_clip(project=tmp_path / "game", status=STATUS, since_utc=since,
                                  until_utc=since + timedelta(seconds=1), output_dir=tmp_path / "one",
                                  generation=current)
    assert only_one.descriptor is None and "fewer than two" in (only_one.reason or "")
    assert not (tmp_path / "one").exists()
    missing = media.capture_clip(project=tmp_path / "game", status=STATUS, since_utc=since,
                                 until_utc=since + timedelta(seconds=1), output_dir=tmp_path / "missing",
                                 generation=str(uuid.uuid4()))
    assert missing.descriptor is None and "frame folder is unavailable" in (missing.reason or "")
    malformed = media.capture_clip(project=tmp_path / "game", status=STATUS, since_utc=since,
                                   until_utc=since + timedelta(seconds=1), output_dir=tmp_path / "bad",
                                   generation="not-a-generation")
    assert malformed.descriptor is None and "canonical UUID" in (malformed.reason or "")
    assert media.frame_directory(tmp_path / "game", current) == tmp_path / "game" / media.FRAME_SUBDIR / current


@needs_ffmpeg
def test_a_generation_clip_records_the_generation_and_that_instances_frames(tmp_path: Path) -> None:
    since = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    generation = str(uuid.uuid4())
    frames = _generation_frames(tmp_path, generation)
    _jpeg(frames / "f-000001.jpg", "red")
    _jpeg(frames / "f-000002.jpg", "green")
    _stamp(frames / "f-000001.jpg", since)
    _stamp(frames / "f-000002.jpg", since + timedelta(seconds=0.1))
    clip = media.capture_clip(project=tmp_path / "game", status=STATUS, since_utc=since,
                              until_utc=since + timedelta(seconds=1), output_dir=tmp_path / "generation",
                              generation=generation)
    assert clip.descriptor is not None and clip.manifest is not None
    assert clip.descriptor["generation"] == generation
    assert clip.manifest["source_directory"] == (media.FRAME_SUBDIR / generation).as_posix()
    assert [frame["source"] for frame in clip.manifest["frames"]] == ["f-000001.jpg", "f-000002.jpg"]
