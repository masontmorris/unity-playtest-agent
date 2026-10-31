"""Host-control mailbox records for an Editor-owned Play Mode session.

These are local control records, not wire messages: they never inherit `Envelope` and they carry no
gameplay input and no credentials. One run owns one control directory outside the game project. The
host and the Python runner speak through atomically published JSON files there, with these exact
filenames and field names (see `docs/design/play-mode-restart.md`).

A generation is a canonical lowercase, hyphenated UUID created once per new play instance
(`Guid.ToString("D")` / `str(uuid.uuid4())`). Both sides reject a stale or mismatched run, ticket or
generation, bad types and extra keys. A ready result must carry every readiness timestamp, a scene
path, a scene handle and a distinct new generation; failed and cancelled results must say why and
must not name a new generation. Anything neither side measured is `null`, never zero or a default.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .common import Strict

GENERATION_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
GENERATION_RE = re.compile(GENERATION_PATTERN)
SHA256_PATTERN = r"^[0-9a-f]{64}$"


def is_generation(value: str | None) -> bool:
    """Whether `value` is one canonical generation string."""
    return isinstance(value, str) and GENERATION_RE.fullmatch(value) is not None


def _aware(value: datetime | None) -> datetime | None:
    """A timestamp is either unknown or carries an offset; a naive one is not a measured instant."""
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("timestamps must be timezone-aware")
    return value





class HostPhase(str, Enum):
    starting = "starting"
    ready = "ready"
    stopping = "stopping"
    stopped = "stopped"
    failed = "failed"


class SessionControlConfig(Strict):
    """`config.json`: what run this control directory belongs to and how long it may last."""

    version: Literal[1]
    run_id: str = Field(min_length=1)
    project_path: str = Field(min_length=1)
    catalog_path: str = Field(min_length=1)
    catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    deadline_utc: datetime

    @field_validator("deadline_utc")
    @classmethod
    def _deadline_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class HostState(Strict):
    """`host-state.json`: what the Editor-owned host last recorded about itself."""

    version: Literal[1]
    run_id: str = Field(min_length=1)
    phase: HostPhase
    generation: str | None = Field(None, pattern=GENERATION_PATTERN)
    scene_path: str | None = None
    scene_handle: int | None = None
    ready_utc: datetime | None = None
    error: str | None = None
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("ready_utc")
    @classmethod
    def _ready_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def _phase_fields(self) -> "HostState":
        if self.phase is HostPhase.ready:
            if (self.generation is None or self.scene_path is None or self.scene_handle is None
                    or self.ready_utc is None):
                raise ValueError(
                    "a ready host state needs a generation, scene_path, scene_handle and ready_utc")
            if self.error is not None:
                raise ValueError("a ready host state cannot carry an error")
        if self.phase is HostPhase.failed and not (self.error and self.error.strip()):
            raise ValueError("a failed host state needs a non-empty error")
        return self

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


class RestartRequest(Strict):
    """`request.json`: one explicit S1-selected restart, dispatched after a verified release."""

    version: Literal[1]
    run_id: str = Field(min_length=1)
    ticket_id: str = Field(min_length=1)
    operation: Literal["restart_play_mode"]
    from_generation: str = Field(pattern=GENERATION_PATTERN)
    deadline_utc: datetime

    @field_validator("deadline_utc")
    @classmethod
    def _deadline_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class RestartPhase(str, Enum):
    accepted = "accepted"
    stopped = "stopped"
    starting = "starting"
    ready = "ready"
    failed = "failed"
    cancelled = "cancelled"


class RestartResult(Strict):
    """`result.json`: the host's one terminal record for one ticket. It does not attest the handshake."""

    version: Literal[1]
    run_id: str = Field(min_length=1)
    ticket_id: str = Field(min_length=1)
    phase: RestartPhase
    from_generation: str = Field(pattern=GENERATION_PATTERN)
    to_generation: str | None = Field(None, pattern=GENERATION_PATTERN)
    entered_edit_mode_utc: datetime | None = None
    entered_play_mode_utc: datetime | None = None
    ready_utc: datetime | None = None
    scene_path: str | None = None
    scene_handle: int | None = None
    failure_reason: str | None = None

    @field_validator("entered_edit_mode_utc", "entered_play_mode_utc", "ready_utc")
    @classmethod
    def _times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def _phase_fields(self) -> "RestartResult":
        if self.phase is RestartPhase.ready:
            missing = [name for name in ("entered_edit_mode_utc", "entered_play_mode_utc", "ready_utc",
                                         "scene_path", "scene_handle", "to_generation")
                       if getattr(self, name) is None]
            if missing:
                raise ValueError(f"a ready result needs {', '.join(missing)}")
            if not (self.scene_path and self.scene_path.strip()):
                raise ValueError("a ready result needs a non-empty scene_path")
            if self.to_generation == self.from_generation:
                raise ValueError("a ready result needs a generation distinct from the one it restarted")
            if self.failure_reason is not None:
                raise ValueError("a ready result cannot carry a failure reason")
            if self.entered_edit_mode_utc > self.entered_play_mode_utc:
                raise ValueError("a ready result cannot observe play mode before edit mode")
            if self.entered_play_mode_utc > self.ready_utc:
                raise ValueError("a ready result cannot observe readiness before play mode")
        else:
            if self.phase in (RestartPhase.failed, RestartPhase.cancelled):
                if not (self.failure_reason and self.failure_reason.strip()):
                    raise ValueError(f"a {self.phase.value} result needs a non-empty failure_reason")
            elif self.failure_reason is not None:
                raise ValueError(f"a {self.phase.value} result cannot carry a failure reason")
            if self.to_generation is not None:
                raise ValueError(
                    f"a {self.phase.value} result cannot carry a to_generation; only ready does")
        return self

    @property
    def terminal(self) -> bool:
        return self.phase in (RestartPhase.ready, RestartPhase.failed, RestartPhase.cancelled)


class RestartCancel(Strict):
    """`cancel.json`: fail closed for one ticket; a late transition can never publish success for it."""

    version: Literal[1]
    run_id: str = Field(min_length=1)
    ticket_id: str = Field(min_length=1)
    operation: Literal["cancel_restart"]
