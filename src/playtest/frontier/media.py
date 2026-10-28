"""Clip capture from the bridge's existing JPEG window (#33).

The bridge writes offscreen-composite JPEG frames under `<project>/Temp/playtest-bridge-frames`
at a nominal 10 Hz. This module copies a bounded window of those files into a per-call folder,
records every source COPY's hash and modification time in a local manifest, and encodes an MP4
whose durations follow those measured modification times. The modification time is the only
timestamp the files carry: it is the JPEG writer's completion time, not the capture time or game
time, so alignment with the trace is explicitly approximate.

Capture is available only when the bridge status says `capture` is on and its self-test passed.
The bridge keeps a rolling window, so a listed frame can be deleted before it is measured or copied.
Such a frame is skipped rather than failing the capture, the window is re-listed a bounded number of
times when a scan comes back unusable, and the number of frames that could not be measured is
recorded in the local manifest. A window with fewer than two measurable frames, a missing encoder or
an encoding error returns no clip and a concrete reason; this module never writes a stand-in video
and never claims a frame count it did not copy.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FRAME_SUBDIR = Path("Temp") / "playtest-bridge-frames"
SELF_TEST_NAME = "self-test.jpg"
WINDOW_S = 4.0
WINDOW_ATTEMPTS = 3
NOMINAL_RATE_HZ = 10.0
WIDTH, HEIGHT = 640, 360
TIMESTAMP_BASIS = "frame_file_mtime_utc"
CAVEAT = ("alignment is approximate: frame order and durations use JPEG writer completion "
          "times, not capture time or game time; game time per frame is unknown")
SOURCE = "bridge_offscreen_composite"
CONSUMER = "frontier"


@dataclass(frozen=True)
class Clip:
    """One measured clip, or the explicit reason there is none."""

    descriptor: dict[str, Any] | None
    path: Path | None
    reason: str | None
    manifest: dict[str, Any] | None


def capture_clip(
    *,
    project: Path,
    status: Any | None,
    since_utc: datetime,
    until_utc: datetime,
    output_dir: Path,
    ffmpeg: str = "ffmpeg",
    timeout_s: float = 120.0,
) -> Clip:
    """Copy and encode the last `WINDOW_S` seconds of bridge frames, bounded by the run.

    `since_utc` is the run or episode start wall time and `until_utc` is the escalating
    observation's wall time. `output_dir` must be new; an existing folder is never overwritten.
    """
    gated = _gate(status)
    if gated is not None:
        return Clip(None, None, gated, None)
    since, until = _utc(since_utc), _utc(until_utc)
    if since is None or until is None:
        return Clip(None, None, "the clip window needs timezone-aware wall times", None)
    if until < since:
        return Clip(None, None, "the clip window ends before it starts", None)
    start = max(since, until - timedelta(seconds=WINDOW_S))
    source = project / FRAME_SUBDIR
    if not source.is_dir():
        return Clip(None, None, "the bridge frame folder is unavailable", None)
    selected: list[tuple[datetime, Path]] = []
    skipped = 0
    for attempt in range(1, WINDOW_ATTEMPTS + 1):
        selected, vanished, reason = _scan_window(source, start, until)
        skipped += vanished
        if reason is not None:
            return Clip(None, None, reason, None)
        if len(selected) >= 2 and len({mtime for mtime, _ in selected}) >= 2:
            break
        if attempt == WINDOW_ATTEMPTS:
            if len(selected) < 2:
                return Clip(None, None, "fewer than two capture frames fall in the window", None)
            return Clip(None, None, "the capture frames have no distinct modification times", None)

    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return Clip(None, None, "the clip output folder already exists", None)
    except OSError:
        return Clip(None, None, "the clip output folder could not be created", None)

    frames, vanished, reason = _copy_frames(selected, output_dir)
    skipped += vanished
    if reason is not None:
        return Clip(None, None, reason, None)
    if len(frames) < 2:
        return Clip(None, None, "fewer than two capture frames could be copied", None)
    if len({frame["mtime_utc"] for frame in frames}) < 2:
        return Clip(None, None, "the capture frames have no distinct modification times", None)

    times = [datetime.fromisoformat(frame["mtime_utc"]) for frame in frames]
    durations = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    if any(not math.isfinite(value) or value <= 0 for value in durations):
        return Clip(None, None, "the capture frame order is not strictly increasing", None)
    durations.append(1.0 / NOMINAL_RATE_HZ)
    encoded_s = sum(durations)

    encoder = shutil.which(ffmpeg) if not Path(ffmpeg).is_file() else ffmpeg
    if not encoder:
        return Clip(None, None, "ffmpeg is not available", None)
    concat = ["ffconcat version 1.0"]
    for frame, duration in zip(frames, durations):
        concat.append(f"file '{frame['file']}'")
        concat.append(f"duration {duration:.6f}")
    (output_dir / "frames.ffconcat").write_text("\n".join(concat) + "\n", encoding="utf-8")
    clip_path = output_dir / "clip.mp4"
    command = [
        encoder, "-nostdin", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", "frames.ffconcat",
        "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
               f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-color_range", "tv",
        "-vsync", "vfr", "-an", "-movflags", "+faststart",
        "-t", f"{encoded_s:.6f}", "clip.mp4",
    ]
    try:
        completed = subprocess.run(command, cwd=str(output_dir), capture_output=True, text=True,
                                   timeout=timeout_s, shell=False, check=False)
    except subprocess.TimeoutExpired:
        return Clip(None, None, "ffmpeg timed out while encoding the clip", None)
    except OSError:
        return Clip(None, None, "ffmpeg could not be run", None)
    if completed.returncode != 0:
        return Clip(None, None, f"ffmpeg could not encode the clip (exit {completed.returncode})", None)
    try:
        raw = clip_path.read_bytes()
    except OSError:
        return Clip(None, None, "ffmpeg produced no clip file", None)
    if not raw:
        return Clip(None, None, "ffmpeg produced an empty clip file", None)

    measured_duration = _duration(clip_path)

    descriptor = {
        "sha256": _sha(raw),
        "bytes": len(raw),
        "mime": "video/mp4",
        "source": SOURCE,
        "consumer": CONSUMER,
        "frame_count": len(frames),
        "width": WIDTH,
        "height": HEIGHT,
        "start_wall_utc": times[0].isoformat(),
        "end_wall_utc": times[-1].isoformat(),
        "nominal_duration_s": round(len(frames) / NOMINAL_RATE_HZ, 6),
        "target_duration_s": round(encoded_s, 6),
        "encoded_duration_s": measured_duration,
        "final_frame_hold_s": 1.0 / NOMINAL_RATE_HZ,
        "timestamp_basis": TIMESTAMP_BASIS,
        "frame_game_s": None,
        "caveat": CAVEAT,
    }
    manifest = {
        "timestamp_basis": TIMESTAMP_BASIS,
        "caveat": CAVEAT,
        "source_directory": FRAME_SUBDIR.as_posix(),
        "window": {"since_utc": since.isoformat(), "until_utc": until.isoformat(),
                   "selected_start_utc": start.isoformat()},
        "frames": frames,
        "frames_skipped": skipped,
        "clip": clip_path.name,
        "encoded_duration_s": descriptor["encoded_duration_s"],
        "target_duration_s": descriptor["target_duration_s"],
        "final_frame_hold_s": descriptor["final_frame_hold_s"],
        "nominal_duration_s": descriptor["nominal_duration_s"],
    }
    try:
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except OSError:
        return Clip(None, None, "the clip manifest could not be written", None)
    return Clip(descriptor, clip_path, None, manifest)


def _scan_window(
    source: Path, start: datetime, until: datetime
) -> tuple[list[tuple[datetime, Path]], int, str | None]:
    """Measure the frames whose write time falls in the window, skipping evicted ones.

    The bridge's rolling window deletes its oldest frame on every write, so a file that is listed
    here can be gone before it is measured. Such a frame is skipped and counted: the window keeps
    only the frames that could actually be measured, never one that was guessed.
    """
    try:
        paths = sorted(
            (path for path in source.iterdir()
             if path.is_file() and path.suffix.lower() == ".jpg"
             and path.name.lower() != SELF_TEST_NAME),
            key=lambda path: path.name,
        )
    except OSError:
        return [], 0, "the bridge frame folder could not be read"
    selected: list[tuple[datetime, Path]] = []
    skipped = 0
    for path in paths:
        try:
            modified = path.stat().st_mtime
        except OSError:
            skipped += 1
            continue
        mtime = _utc(datetime.fromtimestamp(modified, tz=timezone.utc))
        if mtime is not None and start <= mtime <= until:
            selected.append((mtime, path))
    selected.sort(key=lambda item: (item[0], item[1].name))
    return selected, skipped, None


def _copy_frames(
    selected: list[tuple[datetime, Path]], output_dir: Path
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Copy the selected frames, skipping any the rolling window removed after the scan.

    A frame that still exists but changed between the scan and its read is not an eviction: that is
    an integrity failure and fails closed. The copied files are numbered by copy order, so a skipped
    frame leaves no gap for the concat list to resolve.
    """
    frames: list[dict[str, Any]] = []
    skipped = 0
    for mtime, path in selected:
        try:
            data = path.read_bytes()
            after = path.stat()
        except OSError:
            skipped += 1
            continue
        if len(data) != after.st_size or _utc(datetime.fromtimestamp(after.st_mtime, tz=timezone.utc)) != mtime:
            return [], skipped, "a capture frame changed while it was read"
        if not data.startswith(b"\xff\xd8"):
            return [], skipped, "a capture file is not a JPEG frame"
        name = f"frame-{len(frames) + 1:06d}.jpg"
        try:
            (output_dir / name).write_bytes(data)
        except OSError:
            return [], skipped, "a capture frame could not be copied"
        frames.append({"file": name, "source": path.name, "sha256": _sha(data), "bytes": len(data),
                       "mtime_utc": mtime.isoformat()})
    return frames, skipped, None


def _gate(status: Any | None) -> str | None:
    if status is None:
        return "the bridge status is unavailable"
    if getattr(status, "capture", None) is not True:
        return "bridge capture is disabled"
    self_test = getattr(status, "capture_self_test", None)
    if self_test != "passed":
        return f"the capture self-test did not pass ({self_test if self_test is not None else 'unknown'})"
    return None


def _duration(path: Path) -> float | None:
    """Read the container duration; a missing probe leaves the measurement unknown."""
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        completed = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10, check=True, shell=False)
        value = float(json.loads(completed.stdout)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
