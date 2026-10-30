"""Record a whole session's play view from the bridge's frame window.

The bridge renders the game view offscreen and writes each composite as a JPEG named `f-<counter>.jpg`
into `<project>/Temp/playtest-bridge-frames`. It keeps only a rolling window of recent frames
(`frontier.media` reads the same window for clips). This recorder runs beside a session as its own
process. It copies every new frame out of the window before the bridge evicts it, and at the end it
encodes the copies, in counter order, into one MP4.

It is passive. It never talks to the bridge, the runner or S1, and nothing in a session reads what it
writes, so it cannot change a decision.

- **Stale frames.** Frames written before the recorder started are ignored, so a window left over from
  an earlier session is never taken for this one.
- **Missing frames.** A frame the window evicted before it could be copied shows as a gap in the
  bridge's counter. It is counted, never filled in.
- **Timing.** Frame durations come from the JPEG writer's completion times (`media.TIMESTAMP_BASIS`),
  not game time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playtest.frontier import media

FRAME_NAME = re.compile(r"^f-(\d{6,})\.jpg$")
INTERVAL_S = 0.25
MAX_WALL_S = 3600.0
MIN_DURATION_S = 0.001
ENCODE_TIMEOUT_S = 900.0
VIDEO_NAME = "session.mp4"
SUMMARY_NAME = "recording.json"
CONCAT_NAME = "frames.ffconcat"
SOURCE = "bridge_offscreen_composite"
CONSUMER = "session_recording"


def _utc_from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1e9, tz=timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Recorder:
    """Copies new bridge frames into `frames_dir`, numbered by the bridge's own counter."""

    source: Path
    frames_dir: Path
    started_ns: int
    frames: dict[int, dict[str, Any]] = field(default_factory=dict)
    vanished: int = 0
    stale: set[str] = field(default_factory=set)
    unreadable: int = 0

    def sweep(self) -> int:
        """Copy every frame not yet copied. Returns how many were copied in this sweep."""
        try:
            entries = list(self.source.iterdir())
        except OSError:
            return 0  # the window does not exist yet, or is being cleared; try again on the next sweep
        copied = 0
        for path in sorted(entries, key=lambda item: item.name):
            match = FRAME_NAME.match(path.name)
            if match is None:
                continue  # the self-test image, a partial `.tmp` write, or anything else
            sequence = int(match.group(1))
            if sequence in self.frames or path.name in self.stale:
                continue
            try:
                before = path.stat()
                data = path.read_bytes()
                after = path.stat()
            except OSError:
                self.vanished += 1  # evicted between the listing and the read; the counter gap shows it too
                continue
            if before.st_mtime_ns < self.started_ns:
                self.stale.add(path.name)
                continue
            if (len(data) != after.st_size or after.st_mtime_ns != before.st_mtime_ns
                    or not data.startswith(b"\xff\xd8")):
                self.unreadable += 1  # not a finished JPEG frame; leave it and look again on the next sweep
                continue
            name = f"frame-{sequence:06d}.jpg"
            (self.frames_dir / name).write_bytes(data)
            self.frames[sequence] = {"file": name, "source": path.name, "sequence": sequence,
                                     "mtime_utc": _utc_from_ns(before.st_mtime_ns).isoformat(),
                                     "mtime_ns": before.st_mtime_ns, "bytes": len(data), "sha256": _sha(data)}
            copied += 1
        return copied

    def ordered(self) -> list[dict[str, Any]]:
        return [self.frames[key] for key in sorted(self.frames)]

    def gaps(self) -> int:
        """Frames the bridge counted between the first and last copied ones that were never copied."""
        sequences = sorted(self.frames)
        return sum(b - a - 1 for a, b in zip(sequences, sequences[1:]))


def durations(frames: list[dict[str, Any]]) -> tuple[list[float], int]:
    """Each frame's display time from the next frame's write time, and how many had to be floored.

    Frames are shown in the bridge's counter order. The writer threads can finish slightly out of order,
    so a non-positive difference is floored at `MIN_DURATION_S` and counted rather than reordered.
    The last frame is held for one nominal frame.
    """
    values: list[float] = []
    floored = 0
    for a, b in zip(frames, frames[1:]):
        value = (b["mtime_ns"] - a["mtime_ns"]) / 1e9
        if not math.isfinite(value) or value < MIN_DURATION_S:
            value, floored = MIN_DURATION_S, floored + 1
        values.append(value)
    if frames:
        values.append(1.0 / media.NOMINAL_RATE_HZ)
    return values, floored


def encode(frames: list[dict[str, Any]], directory: Path, *, ffmpeg: str = "ffmpeg",
           timeout_s: float = ENCODE_TIMEOUT_S) -> dict[str, Any]:
    """Encode the copied frames into `session.mp4`. Returns the video's descriptor, or the reason there is none."""
    if len(frames) < 2:
        return {"video": None, "reason": "fewer than two frames were copied"}
    encoder = ffmpeg if Path(ffmpeg).is_file() else shutil.which(ffmpeg)
    if not encoder:
        return {"video": None, "reason": "ffmpeg is not available"}
    values, floored = durations(frames)
    lines = ["ffconcat version 1.0"]
    for frame, value in zip(frames, values):
        lines += [f"file 'frames/{frame['file']}'", f"duration {value:.6f}"]
    (directory / CONCAT_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    target_s = sum(values)
    command = [encoder, "-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", CONCAT_NAME,
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,format=yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-color_range", "tv",
               "-vsync", "vfr", "-an", "-movflags", "+faststart", "-t", f"{target_s:.6f}", VIDEO_NAME]
    try:
        completed = subprocess.run(command, cwd=str(directory), capture_output=True, text=True,
                                   timeout=timeout_s, shell=False, check=False)
    except subprocess.TimeoutExpired:
        return {"video": None, "reason": "ffmpeg timed out while encoding the session"}
    except OSError:
        return {"video": None, "reason": "ffmpeg could not be run"}
    if completed.returncode != 0:
        return {"video": None, "reason": f"ffmpeg could not encode the session (exit {completed.returncode})",
                "ffmpeg_error": completed.stderr.strip()[-2000:]}
    path = directory / VIDEO_NAME
    try:
        raw = path.read_bytes()
    except OSError:
        return {"video": None, "reason": "ffmpeg produced no video file"}
    if not raw:
        return {"video": None, "reason": "ffmpeg produced an empty video file"}
    return {"video": {"file": VIDEO_NAME, "sha256": _sha(raw), "bytes": len(raw), "mime": "video/mp4",
                      "frame_count": len(frames), "target_duration_s": round(target_s, 6),
                      "encoded_duration_s": media._duration(path), "durations_floored": floored,
                      "final_frame_hold_s": 1.0 / media.NOMINAL_RATE_HZ},
            "reason": None}


def record(project: Path, output: Path, stop_file: Path, *, interval_s: float = INTERVAL_S,
           max_wall_s: float = MAX_WALL_S, ffmpeg: str = "ffmpeg") -> dict[str, Any]:
    """Copy frames until `stop_file` appears or `max_wall_s` passes, then encode them and summarize."""
    frames_dir = output / "frames"
    frames_dir.mkdir(parents=True, exist_ok=False)
    started_ns = time.time_ns()
    recorder = Recorder(project / media.FRAME_SUBDIR, frames_dir, started_ns)
    deadline = time.monotonic() + max_wall_s
    ended = "stop_file"
    while not stop_file.exists():
        if time.monotonic() >= deadline:
            ended = "max_wall_s"
            break
        recorder.sweep()
        time.sleep(interval_s)
    recorder.sweep()  # the frames written after the last sweep, while they are still in the window
    stopped_ns = time.time_ns()
    frames = recorder.ordered()
    result = encode(frames, output, ffmpeg=ffmpeg)
    return {
        "version": 1, "source": SOURCE, "consumer": CONSUMER,
        "source_directory": media.FRAME_SUBDIR.as_posix(),
        "started_utc": _utc_from_ns(started_ns).isoformat(), "stopped_utc": _utc_from_ns(stopped_ns).isoformat(),
        "ended_by": ended, "interval_s": interval_s,
        "frames_copied": len(frames),
        "first_sequence": frames[0]["sequence"] if frames else None,
        "last_sequence": frames[-1]["sequence"] if frames else None,
        "frames_missed_between": recorder.gaps(),
        "frames_vanished_during_copy": recorder.vanished,
        "frames_stale_ignored": len(recorder.stale),
        "unfinished_reads": recorder.unreadable,
        "first_frame_utc": frames[0]["mtime_utc"] if frames else None,
        "last_frame_utc": frames[-1]["mtime_utc"] if frames else None,
        "timestamp_basis": media.TIMESTAMP_BASIS, "caveat": media.CAVEAT,
        **result,
        "frames": [{key: frame[key] for key in ("file", "source", "sequence", "mtime_utc", "bytes", "sha256")}
                   for frame in frames],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="playtest-session-video", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, required=True, help="the Unity project whose bridge writes frames")
    parser.add_argument("--output", type=Path, required=True, help="new directory outside the project")
    parser.add_argument("--stop-file", type=Path, required=True, help="the recorder stops when this file appears")
    parser.add_argument("--interval-s", type=float, default=INTERVAL_S, help="seconds between sweeps of the window")
    parser.add_argument("--max-wall-s", type=float, default=MAX_WALL_S, help="stop after this many seconds regardless")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    if not project.is_dir():
        parser.error("--project must be an existing directory")
    if output.is_relative_to(project):
        parser.error("--output must be outside the Unity project")
    if output.exists():
        parser.error("--output must be a new directory")
    for value, name in ((args.interval_s, "--interval-s"), (args.max_wall_s, "--max-wall-s")):
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{name} must be finite and positive")
    summary = record(project, output, args.stop_file.resolve(), interval_s=args.interval_s,
                     max_wall_s=args.max_wall_s, ffmpeg=args.ffmpeg)
    (output / SUMMARY_NAME).write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    video = summary["video"]
    print(f"session recording: {summary['frames_copied']} frames, {summary['frames_missed_between']} missed; "
          + (f"{video['file']} {video['encoded_duration_s']} s" if video else f"no video: {summary['reason']}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
