"""Shared helpers for the day-1 model probes. Not part of the product.

Raw results go to runs/probes/ (gitignored). Request bodies are stored with images and video
replaced by a size note, and no header is ever written, so keys never reach disk or stdout.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs" / "probes"
ASSETS = RUNS / "assets"


def env(name: str) -> str | None:
    """Process environment first, then the repo's .env. Never printed."""
    value = os.environ.get(name) or dotenv_values(ROOT / ".env").get(name)
    return value.strip().strip("'\"") if value else None


def run_dir(tag: str) -> Path:
    d = RUNS / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def asset(name: str, lavfi: str, *extra: str) -> Path:
    """A synthetic test-pattern frame or clip, generated with ffmpeg on first use."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    path = ASSETS / name
    if not path.exists():
        if not shutil.which("ffmpeg"):
            raise SystemExit("ffmpeg is needed to generate probe assets")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", lavfi, *extra, "-y", str(path)], check=True)
    return path


def frame_png() -> Path:
    return asset("frame_1280x720.png", "testsrc2=size=1280x720:rate=1", "-frames:v", "1")


def clip_mp4() -> Path:
    return asset("clip_640x360_10fps_4s.mp4", "testsrc2=size=640x360:rate=10", "-t", "4",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an")


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def redact_media(obj: Any) -> Any:
    if isinstance(obj, str) and obj.startswith("data:") and ";base64," in obj:
        return f"[{obj.split(';', 1)[0][5:]}, {len(obj) * 3 // 4} bytes]"
    if isinstance(obj, dict):
        return {k: redact_media(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_media(v) for v in obj]
    return obj


class Recorder:
    """Appends one JSON line per call as it completes (results survive a crash or a 402)."""

    def __init__(self, directory: Path):
        self.path = directory / "calls.jsonl"

    def write(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(redact_media(row), ensure_ascii=False) + "\n")


def timed() -> float:
    return time.perf_counter()


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1]
