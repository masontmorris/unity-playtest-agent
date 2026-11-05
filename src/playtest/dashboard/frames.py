"""The newest game frame of one session, for the live viewport.

While the host is up, the bridge writes about 10 frames a second into the project's frame window for the current
play generation (`Temp/playtest-bridge-frames/<generation>/f-NNNNNN.jpg`, written to a temporary name and moved
into place, the oldest deleted). Afterwards the session's own recording holds the frames it copied
(`recording/frames/<generation>/frame-NNNNNN.jpg`). Only those two folders, found from the session's own control
files, are ever read, and only files whose names match the frame pattern.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from playtest.contracts.session_control import is_generation
from playtest.frontier.media import frame_directory

LIVE_FRAME = re.compile(r"f-(\d+)\.jpg")
RECORDED_FRAME = re.compile(r"frame-(\d+)\.jpg")
LIVE_PHASES = frozenset({"starting", "ready", "stopping"})
MAX_FRAME_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Frame:
    data: bytes
    source: str  # "live" or "recording"
    name: str
    written_utc: str


def _json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def live_directory(session: Path) -> Path | None:
    """The frame window the host is writing for this session now, or None when the host is not up."""
    state = _json(session / "control" / "host-state.json")
    config = _json(session / "control" / "config.json")
    if not state or not config or state.get("phase") not in LIVE_PHASES:
        return None
    generation, project = state.get("generation"), config.get("project_path")
    if not is_generation(generation) or not isinstance(project, str) or not project:
        return None
    return frame_directory(Path(project), generation)


def recorded_directory(session: Path) -> Path | None:
    """The session recording's newest generation folder, or None when nothing was recorded."""
    root = session / "recording" / "frames"
    try:
        folders = [path for path in root.iterdir() if path.is_dir() and is_generation(path.name)]
    except OSError:
        return None
    return max(folders, key=lambda path: path.stat().st_mtime_ns) if folders else None


def _newest(folder: Path, pattern: re.Pattern[str]) -> list[Path]:
    """Frame files newest first, by the counter in their names."""
    try:
        names = [(int(match.group(1)), entry.name) for entry in folder.iterdir()
                 if (match := pattern.fullmatch(entry.name)) is not None]
    except OSError:
        return []
    return [folder / name for _, name in sorted(names, reverse=True)]


def _read(path: Path) -> bytes | None:
    """A whole JPEG, or None when it vanished, is still being replaced, or is not a complete JPEG."""
    try:
        before = path.stat()
        if before.st_size > MAX_FRAME_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) != before.st_size or not data.startswith(b"\xff\xd8") or not data.rstrip(b"\0").endswith(b"\xff\xd9"):
        return None
    return data


def latest_frame(session: Path) -> Frame | None:
    for source, folder, pattern in (("live", live_directory(session), LIVE_FRAME),
                                    ("recording", recorded_directory(session), RECORDED_FRAME)):
        if folder is None:
            continue
        for path in _newest(folder, pattern)[:3]:  # the newest may be deleted under us; try the next ones
            data = _read(path)
            if data is not None:
                try:
                    written = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                except OSError:
                    written = "unknown"
                return Frame(data, source, path.name, written)
    return None
