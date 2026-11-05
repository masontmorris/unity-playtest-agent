"""What is under `runs/`: hunts, their steps and sessions, and stand-alone sessions, read from the files they wrote.

A hunt is a folder holding a hunt `config.json` (or a finished `hunt.json`); a session is a folder holding a
`trace.jsonl`. Status comes from what was recorded: a session's `end` entry, a hunt's report. A session with no
`end` entry is only called live while something is still writing to it; otherwise its status is unknown.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from playtest.runner.hunt import REQUIRED as HUNT_KEYS

SKIP_DIRS = frozenset({"recording", "clips", "requests", "calls", "control", "memory", "frames", "replays",
                       "guards", "node_modules", "__pycache__"})
MAX_DEPTH = 4
LIVE_WINDOW_S = 45.0
PREDICT_PARTS = ("all", "actions", "magnitudes")
SESSION_DIR = re.compile(r"session-(\d+)")
SEQ = re.compile(rb'"trace_seq":\s*(\d+)')
WALL = re.compile(rb'"wall_utc":\s*"([^"]+)"')
KIND = re.compile(rb'"kind":\s*"([a-z_]+)"')


def ledger_cost(folder: Path) -> float | None:
    """What the folder's call ledgers recorded, in USD; None when no ledger row with a cost exists yet."""
    total, rows = 0.0, 0
    for row in folder.rglob("calls/*.json"):
        cost = (read_json(row) or {}).get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total, rows = total + cost, rows + 1
    return round(total, 4) if rows else None


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_hunt(folder: Path) -> bool:
    if (folder / "hunt.json").is_file():
        return True
    config = read_json(folder / "config.json")
    return isinstance(config, dict) and all(key in config for key in HUNT_KEYS)


def is_session(folder: Path) -> bool:
    return (folder / "trace.jsonl").is_file()


def discover(root: Path) -> list[dict[str, Any]]:
    """Every hunt and stand-alone session under `root`, newest first."""
    found = []
    for folder in _walk(root, 0):
        if is_hunt(folder):
            found.append(hunt_card(root, folder))
        elif is_session(folder):
            found.append(session_card(root, folder))
    return sorted(found, key=lambda card: card.get("modified") or 0, reverse=True)


def _walk(folder: Path, depth: int) -> Iterator[Path]:
    if depth > MAX_DEPTH:
        return
    try:
        children = sorted(path for path in folder.iterdir() if path.is_dir())
    except OSError:
        return
    for child in children:
        if child.name in SKIP_DIRS or child.name.startswith("."):
            continue
        if is_hunt(child) or is_session(child):
            yield child
        else:
            yield from _walk(child, depth + 1)


def run_id(root: Path, folder: Path) -> str:
    return folder.resolve().relative_to(root.resolve()).as_posix()


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _first_entry(trace: Path) -> dict[str, Any] | None:
    try:
        with trace.open("rb") as handle:
            line = handle.readline()
        return json.loads(line) if line.endswith(b"\n") else None
    except (OSError, ValueError):
        return None


def _end_entry(trace: Path) -> dict[str, Any] | None:
    """The trace's `end` entry when its last line is one; the writer puts nothing after it."""
    try:
        with trace.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            tail = handle.read().rstrip(b"\n").rsplit(b"\n", 1)[-1]
        entry = json.loads(tail)
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) and entry.get("kind") == "end" else None


def session_status(folder: Path) -> str:
    end = _end_entry(folder / "trace.jsonl")
    if end is not None:
        return str((end.get("body") or {}).get("status") or "ended")
    modified = _mtime(folder / "trace.jsonl")
    if modified is not None and time.time() - modified < LIVE_WINDOW_S:
        return "live"
    state = read_json(folder / "control" / "host-state.json")
    if isinstance(state, dict) and state.get("phase") in ("starting", "ready"):
        return "live"
    return "unknown"


def session_card(root: Path, folder: Path) -> dict[str, Any]:
    first = _first_entry(folder / "trace.jsonl") or {}
    body = first.get("body") if first.get("kind") == "run" else {}
    record = read_json(folder / "run.json") or {}
    return {"kind": "session", "id": run_id(root, folder), "name": folder.name,
            "game": (body or {}).get("game_project"), "scene": (body or {}).get("scene"),
            "started_utc": first.get("wall_utc"), "status": session_status(folder),
            "turns": record.get("turns"), "ending": record.get("ending"),
            "suspicions": (record.get("suspicions") or {}).get("total"),
            "modified": _mtime(folder / "trace.jsonl")}


def hunt_card(root: Path, folder: Path) -> dict[str, Any]:
    config = read_json(folder / "config.json") or {}
    report = read_json(folder / "hunt.json")
    sessions = sorted(path for path in folder.iterdir() if path.is_dir() and SESSION_DIR.fullmatch(path.name))
    findings = report.get("findings") if isinstance(report, dict) else None
    return {"kind": "hunt", "id": run_id(root, folder), "name": folder.name, "game": config.get("game"),
            "status": hunt_status(folder, report), "started_utc": _started(folder),
            "sessions": len(sessions), "sessions_planned": config.get("sessions"),
            "findings": len(findings) if isinstance(findings, list) else None,
            "reproduced": sum(1 for f in findings if ((f.get("replay") or {}).get("k") or 0) > 0)
            if isinstance(findings, list) else None,
            "cost_usd": report.get("cost_usd") if isinstance(report, dict) else None,
            "modified": _newest_mtime(folder)}


def _started(folder: Path) -> str | None:
    modified = _mtime(folder / "config.json")
    return datetime.fromtimestamp(modified).astimezone().isoformat() if modified else None


def _newest_mtime(folder: Path) -> float | None:
    """When anything in the hunt's own folder or its sessions' traces last changed."""
    candidates = [_mtime(folder)]
    try:
        for child in folder.iterdir():
            candidates.append(_mtime(child))
            if child.is_dir():
                candidates.append(_mtime(child / "trace.jsonl"))
    except OSError:
        pass
    known = [value for value in candidates if value is not None]
    return max(known) if known else None


def hunt_status(folder: Path, report: Any = None) -> str:
    if isinstance(report, dict):
        return "complete"
    newest = _newest_mtime(folder)
    log = folder.parent / f"{folder.name}.log"
    log_time = _mtime(log)
    if log_time is not None:
        newest = max(newest or 0, log_time)
    if newest is not None and time.time() - newest < LIVE_WINDOW_S * 4:
        return "running"
    return "stopped"  # no report and nothing written lately: interrupted, or failed; its log says which


def hunt_steps(folder: Path) -> list[dict[str, Any]]:
    """Each hunt step and what its folder shows: done, running (started, no result), or pending."""
    config = read_json(folder / "config.json") or {}

    def step(key: str, label: str, done: bool, started: bool, **extra: Any) -> dict[str, Any]:
        return {"key": key, "label": label, "state": "done" if done else "running" if started else "pending",
                **extra}

    steps = [step("guards", "Guard table", (folder / "guards" / "guards.json").is_file(),
                  (folder / "guards").exists(), session="guards" if is_session(folder / "guards") else None)]
    for part in PREDICT_PARTS:
        target = folder / f"expect-{part}"
        steps.append(step(f"predict-{part}", f"Predict: {part}", (target / "expectations.json").is_file(),
                          target.exists()))
    planned = int(config.get("sessions") or 0)
    numbers = {int(m.group(1)) for path in folder.iterdir()
               if path.is_dir() and (m := SESSION_DIR.fullmatch(path.name))}
    for number in sorted(numbers | set(range(1, planned + 1))):
        name = f"session-{number:02d}"
        target = folder / name
        steps.append(step(name, f"Session {number}", (target / "run.json").is_file(), target.exists(),
                          session=name, status=session_status(target) if is_session(target) else None))
    replays = folder / "replays"
    replay_dirs = sorted(path for path in replays.iterdir() if path.is_dir()) if replays.is_dir() else []
    steps.append(step("replays", "Replays", (folder / "hunt.json").is_file(), bool(replay_dirs),
                      count=len(replay_dirs),
                      finished=sum(1 for path in replay_dirs if (path / "reproduce.json").is_file())))
    steps.append(step("report", "Report", (folder / "hunt.json").is_file(), False))
    for index, item in enumerate(steps):  # started, no result, and the hunt went on: the step left nothing
        if item["state"] == "running" and any(later["state"] != "pending" for later in steps[index + 1:]):
            item["state"] = "no result"
    return steps


def hunt_detail(root: Path, folder: Path) -> dict[str, Any]:
    card = hunt_card(root, folder)
    steps = hunt_steps(folder)
    if card["status"] != "running":
        for item in steps:
            if item["state"] == "running":
                item["state"] = "no result"
    active = next((s for s in steps if s["state"] == "running"), None)
    log = folder.parent / f"{folder.name}.log"
    config = read_json(folder / "config.json") or {}
    return {**card, "config": {key: config.get(key) for key in (
                "game", "project", "ref", "sessions", "wall_limit_s", "max_calls", "max_frontier_calls",
                "frontier_model", "attempts", "prompt")},
            "steps": steps, "active": active, "cost_usd": card["cost_usd"] if card["cost_usd"] is not None
            else ledger_cost(folder),
            "log_tail": tail_text(log, 30), "report_md": (folder / "hunt.md").is_file()}


def session_detail(root: Path, folder: Path) -> dict[str, Any]:
    card = session_card(root, folder)
    first = _first_entry(folder / "trace.jsonl") or {}
    run = first.get("body") if first.get("kind") == "run" else None
    control = read_json(folder / "control" / "config.json") or {}
    record = read_json(folder / "run.json")
    return {**card, "objective": (run or {}).get("objective"),
            "models": [{"role": m.get("role"), "provider": m.get("provider"), "model": m.get("model")}
                       for m in (run or {}).get("models") or []],
            "budget": (run or {}).get("budget"), "deadline_utc": control.get("deadline_utc"),
            "record": record, "cost_usd": ledger_cost(folder)}


def tail_text(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 16384))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


# --- findings and their evidence -----------------------------------------------------------------------------

_INDEX_CACHE: dict[Path, tuple[tuple[int, float], dict[str, Any]]] = {}


def trace_index(trace: Path) -> dict[str, Any]:
    """One pass over a trace: each entry's wall time, and the finding entries in full. Cached by size and mtime."""
    try:
        stat = trace.stat()
    except OSError:
        return {"wall": {}, "findings": {}, "history": {}}
    key = (stat.st_size, stat.st_mtime)
    cached = _INDEX_CACHE.get(trace)
    if cached is not None and cached[0] == key:
        return cached[1]
    wall: dict[int, str] = {}
    latest: dict[str, dict[str, Any]] = {}
    history: dict[str, list[dict[str, Any]]] = {}
    with trace.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            head = raw[:1200]
            seq, stamp, kind = SEQ.search(head), WALL.search(head), KIND.search(head)
            if seq is None:
                continue
            if stamp is not None:
                wall[int(seq.group(1))] = stamp.group(1).decode()
            if kind is not None and kind.group(1) == b"finding":
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                body = entry.get("body") or {}
                finding_id = body.get("finding_id")
                if finding_id:
                    latest[finding_id] = {**body, "trace_seq": entry.get("trace_seq"), "author": entry.get("author"),
                                          "wall_utc": entry.get("wall_utc")}
                    history.setdefault(finding_id, []).append(
                        {"state": body.get("state"), "trace_seq": entry.get("trace_seq"),
                         "wall_utc": entry.get("wall_utc")})
    index = {"wall": wall, "findings": latest, "history": history}
    _INDEX_CACHE[trace] = (key, index)
    return index


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def video_moment(session: Path, at_utc: str | None) -> dict[str, Any] | None:
    """Where a wall-clock moment falls in the session recording: the generation video covering it and the offset.

    The recording's frame times are JPEG write times, so the offset is approximate; it says so.
    """
    moment = _parse_utc(at_utc)
    recording = read_json(session / "recording" / "recording.json")
    if moment is None or not isinstance(recording, dict):
        return None
    for generation in recording.get("generations") or []:
        first, last = _parse_utc(generation.get("first_frame_utc")), _parse_utc(generation.get("last_frame_utc"))
        video = generation.get("video") or {}
        if first is None or last is None or not video.get("file"):
            continue
        if first <= moment <= last:
            return {"file": f"recording/{video['file']}", "offset_s": round((moment - first).total_seconds(), 2),
                    "approximate": True, "basis": recording.get("timestamp_basis")}
    return None


def covering_clips(session: Path, at_utc: str | None) -> list[dict[str, Any]]:
    """Frontier clips whose capture window contains the moment: what the frontier was shown around it."""
    moment = _parse_utc(at_utc)
    clips = session / "clips"
    if moment is None or not clips.is_dir():
        return []
    found = []
    for folder in sorted(clips.iterdir()):
        manifest = read_json(folder / "manifest.json")
        if not isinstance(manifest, dict) or not (folder / "clip.mp4").is_file():
            continue
        window = manifest.get("window") or {}
        start = _parse_utc(window.get("selected_start_utc") or window.get("since_utc"))
        end = _parse_utc(window.get("until_utc"))
        if start is not None and end is not None and start <= moment <= end:
            found.append({"file": f"clips/{folder.name}/clip.mp4", "turn_folder": folder.name})
    return found


def with_evidence(session: Path, finding: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    cited = [seq for seq in finding.get("evidence_seq") or [] if isinstance(seq, int)]
    moment = next((index["wall"].get(seq) for seq in cited if index["wall"].get(seq)), None)
    return {**finding, "evidence": {
        "cited_utc": moment, "video": video_moment(session, moment), "clips": covering_clips(session, moment),
        "history": index["history"].get(finding.get("finding_id"), [])}}


def session_findings(root: Path, session: Path) -> list[dict[str, Any]]:
    index = trace_index(session / "trace.jsonl")
    return [{**with_evidence(session, finding, index), "session": session.name,
             "session_id": run_id(root, session), "replay": None}
            for finding in index["findings"].values()]


def hunt_findings(root: Path, folder: Path) -> dict[str, Any]:
    """What the hunt filed: its report's findings with their replays once written, else each session's so far."""
    report = read_json(folder / "hunt.json")
    sessions = sorted(path for path in folder.iterdir()
                      if path.is_dir() and SESSION_DIR.fullmatch(path.name) and is_session(path))
    if isinstance(report, dict):
        items = []
        for finding in report.get("findings") or []:
            session = folder / str(finding.get("session"))
            index = trace_index(session / "trace.jsonl")
            items.append({**with_evidence(session, finding, index), "session_id": run_id(root, session)})
        return {"final": True, "findings": items, "unmet": report.get("unmet") or [],
                "cost_usd": report.get("cost_usd"), "sessions": report.get("sessions")}
    items = [item for session in sessions for item in session_findings(root, session)]
    return {"final": False, "findings": items, "unmet": None, "cost_usd": ledger_cost(folder),
            "sessions": [path.name for path in sessions]}
