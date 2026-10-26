"""Read the project lock written by the Unity bridge package.

The lock is the endpoint advertisement for a real Editor: the bridge writes
its pid, loopback port, protocol and a fresh per-session token to
`<project>/Temp/playtest-bridge.lock` while it runs. The core reads that file
instead of guessing a port or sharing a token out of band.

This module is transport-only. It validates the lock and reports facts; it
does not decide whether a stale pid is safe to use. A missing or malformed
lock is a typed failure, never a default port or token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from playtest.contracts.common import PROTOCOL

LOCK_FILE_NAME = "playtest-bridge.lock"
LOCK_DIRECTORY = "Temp"


class BridgeLockError(Exception):
    """A missing, malformed or unsupported project lock."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BridgeEndpoint:
    """The reachable endpoint advertised by the lock file."""

    path: Path
    protocol: str
    pid: int
    port: int
    token: str
    started_utc: datetime

    @property
    def host(self) -> str:
        return "127.0.0.1"


def lock_path_for_project(project: str | Path) -> Path:
    """Return `<project>/Temp/playtest-bridge.lock` without requiring it to exist."""
    return Path(project) / LOCK_DIRECTORY / LOCK_FILE_NAME


def read_project_lock(project: str | Path) -> BridgeEndpoint:
    """Read the lock for a Unity project root."""
    return read_bridge_lock(lock_path_for_project(project))


def read_bridge_lock(lock_path: str | Path) -> BridgeEndpoint:
    """Read and validate one lock file. Unknown extra fields are tolerated."""
    path = Path(lock_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BridgeLockError("missing", f"bridge lock not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise BridgeLockError("invalid", f"bridge lock is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise BridgeLockError("missing", f"bridge lock could not be read: {path}: {exc}") from exc

    try:
        body = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BridgeLockError("invalid", f"bridge lock is not valid JSON: {path}") from exc

    if not isinstance(body, dict):
        raise BridgeLockError("invalid", f"bridge lock must contain a JSON object: {path}")

    protocol = _required(body, "protocol", str, path)
    pid = _required(body, "pid", int, path)
    port = _required(body, "port", int, path)
    token = _required(body, "token", str, path)
    started_raw = _required(body, "started_utc", str, path)

    if protocol != PROTOCOL:
        raise BridgeLockError(
            "protocol",
            f"bridge lock speaks {protocol!r}, this core speaks {PROTOCOL!r}: {path}",
        )
    if isinstance(pid, bool) or pid <= 0:
        raise BridgeLockError("invalid", f"bridge lock pid must be positive: {path}")
    if isinstance(port, bool) or port <= 0 or port > 65535:
        raise BridgeLockError("invalid", f"bridge lock port must be 1-65535: {path}")
    if not token:
        raise BridgeLockError("invalid", f"bridge lock token must be non-empty: {path}")

    try:
        started_utc = _parse_utc(started_raw)
    except ValueError as exc:
        raise BridgeLockError(
            "invalid", f"bridge lock started_utc is not an ISO-8601 timestamp: {path}"
        ) from exc

    return BridgeEndpoint(
        path=path,
        protocol=protocol,
        pid=pid,
        port=port,
        token=token,
        started_utc=started_utc,
    )


def _required(body: dict, name: str, kind: type, path: Path) -> object:
    if name not in body:
        raise BridgeLockError("invalid", f"bridge lock is missing {name!r}: {path}")
    value = body[name]
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is int):
        raise BridgeLockError("invalid", f"bridge lock {name!r} has the wrong type: {path}")
    return value


def _parse_utc(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
