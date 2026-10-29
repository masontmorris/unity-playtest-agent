"""Shared contract pieces: the message envelope and the enums every message uses.

Statuses are enums, never free strings (build bible section 5).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL = "upa/1"


class Strict(BaseModel):
    """Base for every contract: unknown fields are an error, not silently kept."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ClockMode(str, Enum):
    realtime = "realtime"
    stepped = "stepped"


class Envelope(Strict):
    """Fields every wire message carries."""

    protocol: str = Field(PROTOCOL, pattern=r"^upa/\d+$")
    session: str = Field(min_length=1)
    seq: int = Field(ge=0)
    game_s: float | None = Field(None, ge=0, description="Game clock; null when no game is running")
    wall_utc: datetime
