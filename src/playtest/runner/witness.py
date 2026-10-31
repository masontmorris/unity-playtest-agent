"""The System Zero witness: a clip reviewer that never acts (docs/design/system-zero-witness-check.md).

One HTTP decide per clip. The clip is a silent H.264 MP4 of at most 10 s, sent as the request's only attachment
(System Zero takes video alone, sampled at 2 frames a second from the start). The state says what the clip is
and which inputs were executed in it, measured from a trace or a scripted plan; an input list that wasn't
measured is `null`, never empty. Three fixed, name-blind questions ask whether something visibly went wrong,
what kind of problem it was, and when.

The answers are evidence only. Nothing here, and nothing that reads a witness entry, opens help, ranks a choice
or promotes a finding: S1 owns escalation. A refused question is `unknown`, never "no problem".
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from playtest.bench import system_zero

MAX_CLIP_S = 10.0  # SDK 0.4.2 VIDEO_PROFILE max_seconds; a 10.04 s container was refused as media_too_large
MAX_CLIP_BYTES = 2_621_440  # System Zero's decoded inline limit across a request's files
VIDEO_CHOICE_OPTIONS = 10  # VIDEO_PROFILE choice_options (2, 10): a video request's choice questions take at most 10
SAMPLING = "System Zero samples 2 frames per second from the start of the clip, at most 20 frames."
KINDS = {
    "none": "nothing looks wrong",
    "movement_blocked": "the player stops or cannot move where the way looks open",
    "object_frozen": "an object that should move or animate stays frozen",
    "input_no_effect": "a listed input had no visible result",
    "view_changed_without_input": "the view zooms, jumps or turns with no input that explains it",
    "object_vanished_or_appeared": "an object disappears or appears without cause",
    "passed_through_solid": "the player or an object passes through something solid",
    "visual_glitch": "flicker, missing textures or other rendering faults",
    "other": "something else looks wrong",
}
SEGMENTS = {"s0_2": "0 to 2 s of the clip", "s2_4": "2 to 4 s", "s4_6": "4 to 6 s", "s6_8": "6 to 8 s",
            "s8_10": "8 to 10 s", "none": "nothing looks wrong"}
QUESTIONS: dict[str, dict[str, Any]] = {
    "problem_visible": {
        "type": "boolean",
        "instructions": ("Watch the clip of a game being played. Does it show the game behaving wrongly, in a way a "
                         "player would report as a bug? Judge only what the frames show, against the inputs listed "
                         "in the state."),
        "criteria": {"true": "the clip shows behaviour a player would report as a bug",
                     "false": "everything shown looks like the game working as intended"},
    },
    "kind": {"type": "choice",
             "instructions": "If something in the clip looks wrong, which kind of problem is it? Choose none if nothing does.",
             "criteria": KINDS},
    "when": {"type": "choice",
             "instructions": "When in the clip does the problem first show? Choose none if nothing looks wrong.",
             "criteria": SEGMENTS},
}


def prompt_sha256() -> str:
    return hashlib.sha256(json.dumps(QUESTIONS, sort_keys=True).encode("utf-8")).hexdigest()


def cut_clip(source: Path, start_s: float, duration_s: float, out: Path, *, speed: float = 1.0,
             ffmpeg: str = "ffmpeg") -> dict[str, Any]:
    """Cut `duration_s` of `source` from `start_s` into a silent H.264 MP4 at `speed` (0.5 is half speed).

    The encoding is deliberately plain: baseline profile, no B-frames, a constant 10 fps (the bridge's own frame
    rate) and no edit list. System Zero refused a High-profile, variable-rate cut as `media_decode_failed`.

    Raises `ValueError` when the clip would be longer than System Zero takes or the encoder fails.
    """
    if not 0 < speed <= 1 or duration_s <= 0 or start_s < 0:
        raise ValueError("speed must be in (0, 1]; the window must be positive and start at or after 0")
    if duration_s / speed > MAX_CLIP_S + 1e-9:
        raise ValueError(f"{duration_s} s at speed {speed} exceeds {MAX_CLIP_S} s")
    encoder = ffmpeg if Path(ffmpeg).is_file() else shutil.which(ffmpeg)
    if not encoder:
        raise ValueError("ffmpeg is not available")
    filters = [] if speed == 1 else ["-vf", f"setpts={1 / speed:g}*PTS"]
    command = [encoder, "-v", "error", "-y", "-ss", f"{start_s:.3f}", "-t", f"{duration_s:.3f}", "-i", str(source),
               *filters, "-an", "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "baseline", "-bf", "0",
               "-pix_fmt", "yuv420p", "-r", "10", "-fps_mode", "cfr", "-use_editlist", "0",
               "-movflags", "+faststart", str(out)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if completed.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        raise ValueError(f"ffmpeg could not cut the clip (exit {completed.returncode})")
    data = out.read_bytes()
    if len(data) > MAX_CLIP_BYTES:
        raise ValueError(f"the clip is {len(data)} bytes, over System Zero's {MAX_CLIP_BYTES}")
    measured = _duration(out, encoder)
    if measured is None or measured > MAX_CLIP_S:
        raise ValueError(f"the clip's container lasts {measured} s; System Zero takes at most {MAX_CLIP_S} s")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "duration_s": measured,
            "source_start_s": start_s, "source_duration_s": duration_s, "speed": speed}


def _duration(path: Path, encoder: str) -> float | None:
    """The container duration ffprobe reports, or `None` when it can't be read."""
    probe = Path(encoder).with_name("ffprobe" + Path(encoder).suffix)
    command = [str(probe) if probe.is_file() else (shutil.which("ffprobe") or "ffprobe"), "-v", "error",
               "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
        return round(float(completed.stdout.strip()), 3)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def request(clip: bytes, *, duration_s: float, speed: float, inputs: Sequence[Mapping[str, Any]] | None,
            objective: str | None = None) -> dict[str, Any]:
    """The decide body for one clip: the clip as the only attachment, the measured context as state."""
    state = {
        "clip": {"duration_s": duration_s, "playback_speed": speed,
                 "playback_note": "1.0 is real time; 0.5 means the clip is slowed to half speed",
                 "sampling": SAMPLING, "source": "the game view, recorded from the game's own render"},
        "inputs": [dict(item) for item in inputs] if inputs is not None else None,
        "inputs_note": "t_s is seconds into the clip at playback speed; null means the inputs were not measured",
        "objective": objective,
    }
    url = "data:video/mp4;base64," + base64.b64encode(clip).decode("ascii")
    return {"model": system_zero.MODEL, "questions": QUESTIONS, "state": state,
            "attachments": [{"name": "clip", "type": "video", "data": url}]}


def decode(data: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Each question's answer, or `{"unknown": reason}` when it was refused, missing or malformed."""
    answers = (data or {}).get("answers")
    out: dict[str, dict[str, Any]] = {}
    for name, question in QUESTIONS.items():
        answer = answers.get(name) if isinstance(answers, dict) else None
        if isinstance(answer, dict) and isinstance(answer.get("error"), dict):
            out[name] = {"unknown": f"refused: {answer['error'].get('code')}"}
        elif question["type"] == "boolean" and isinstance(answer, dict) and isinstance(answer.get("decision"), bool) \
                and isinstance(answer.get("probability"), (int, float)):
            out[name] = {"decision": answer["decision"], "probability": float(answer["probability"])}
        elif question["type"] == "choice" and isinstance(answer, dict) and answer.get("choice") in question["criteria"]:
            out[name] = {"decision": answer["choice"], "probabilities": answer.get("probabilities")}
        else:
            out[name] = {"unknown": "no valid answer"}
    return out
