"""Read one bridge offscreen JPEG as evidence for an S1 turn; never persist its bytes."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.core.ports import ViewSample
from playtest.frontier import media
from playtest.runner.session_video import FRAME_NAME

# Ten nominal 10 Hz frame intervals: tolerate a brief capture delay without presenting an old view as current.
MAX_VIEW_AGE_S = 1.0
MAX_FRAME_BYTES = 32 * 1024 * 1024


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Read dimensions from a complete JPEG's SOF marker without decoding or altering its bytes."""
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return None
    pos = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while pos < len(data):
        if data[pos] != 0xFF:
            return None
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            return None
        marker = data[pos]
        pos += 1
        if marker == 0xDA or marker == 0xD9:
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > len(data):
            return None
        size = int.from_bytes(data[pos:pos + 2], "big")
        if size < 2 or pos + size > len(data):
            return None
        if marker in sof:
            if size < 8:
                return None
            height = int.from_bytes(data[pos + 3:pos + 5], "big")
            width = int.from_bytes(data[pos + 5:pos + 7], "big")
            return (width, height) if width and height else None
        pos += size
    return None


class BridgeView:
    """Use file completion time, not an invented capture time, to align a frame with an observation.

    With a play generation the source is that generation's own frame directory; a frame from an earlier
    play instance can never be presented as the current view, whatever its timestamp says. A restart
    rebinds the source and the start time, so the old generation's frames are not candidates at all.
    """

    def __init__(self, project: Path, status: Any, *, started_utc: datetime,
                 generation: str | None = None) -> None:
        if started_utc.tzinfo is None or started_utc.utcoffset() is None:
            raise ValueError("view source requires a timezone-aware start")
        self._project = project
        self._status = status
        self._started_ns = int(started_utc.timestamp() * 1_000_000_000)
        self._generation = generation
        self._source = media.frame_directory(project, generation)

    def rebind(self, *, client: Any = None, observation: Observation | None = None,
               generation: str | None = None) -> None:
        """Point at the restarted play instance's frames, starting now; earlier frames stop being candidates."""
        if generation is None:
            raise ValueError("a restarted view needs the new play generation")
        self._generation = generation
        self._source = media.frame_directory(self._project, generation)
        started = observation.wall_utc if observation is not None else None
        if started is None or started.tzinfo is None or started.utcoffset() is None:
            raise ValueError("a restarted view needs a timezone-aware first observation")
        self._started_ns = int(started.timestamp() * 1_000_000_000)

    def sample(self, observation: Observation) -> ViewSample:
        wall = observation.wall_utc
        if media._gate(self._status) is not None or wall is None or wall.tzinfo is None or wall.utcoffset() is None:
            return self._missing()
        until_ns = int(wall.timestamp() * 1_000_000_000)
        candidates: list[tuple[int, str, Path]] = []
        try:
            for path in self._source.iterdir():
                if FRAME_NAME.fullmatch(path.name) is None:
                    continue
                try:
                    info = path.stat()
                except OSError:
                    continue
                if self._started_ns <= info.st_mtime_ns <= until_ns:
                    candidates.append((info.st_mtime_ns, path.name, path))
        except OSError:
            return self._missing()
        if not candidates:
            return self._missing()
        mtime_ns, _, path = max(candidates)
        age = (until_ns - mtime_ns) / 1_000_000_000
        completed = datetime.fromtimestamp(mtime_ns / 1_000_000_000, timezone.utc).isoformat()
        timing = (f"Capture time unknown; JPEG write completed at {completed} UTC, "
                  f"{age:.6f} s before the observation (approximate).")
        if age > MAX_VIEW_AGE_S:
            return ViewSample(f"Game view too old; no image attached. {timing}", None, "stale")
        try:
            before = path.stat()
            if before.st_size > MAX_FRAME_BYTES or before.st_mtime_ns != mtime_ns:
                return self._missing()
            data = path.read_bytes()
            after = path.stat()
        except OSError:
            return self._missing()
        if (before.st_size != len(data) or after.st_size != len(data) or after.st_mtime_ns != mtime_ns):
            return self._missing()
        size = jpeg_size(data)
        if size is None:
            return self._missing()
        width, height = size
        return ViewSample(f"The attached image is the current game view, {width}x{height} pixels. {timing}",
                          hashlib.sha256(data).hexdigest(), "sent", data)

    @staticmethod
    def _missing() -> ViewSample:
        return ViewSample("Game view unknown; no image attached (frame or timing unavailable).", None, "missing")
