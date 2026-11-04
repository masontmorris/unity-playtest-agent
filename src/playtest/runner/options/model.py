"""Option types shared by the options layer (docs/design/options-layer.md, section 7).

An option is a closed-loop routine S1 chooses as one choice: it runs over measured primitives until a measured
termination condition and returns a measured outcome. These are its plain data types; nothing here decides.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

TERMINATIONS = ("succeeded", "blocked", "fell", "state_changed", "timed_out", "rejected", "unmeasured", "interrupted")
TEMPLATES = ("traverse", "retreat", "hop", "wait", "place", "sweep", "press_test")


@dataclass(frozen=True)
class OptionSpec:
    """One offerable option, built from measured primitives."""

    option_id: str
    template: str
    params: dict[str, Any]
    intent: str

    def __post_init__(self) -> None:
        if self.template not in TEMPLATES:
            raise ValueError(f"unknown option template {self.template!r}")
        if not self.option_id:
            raise ValueError("an option needs an id")


@dataclass(frozen=True)
class OptionOutcome:
    """What one executed option measurably did. Unknown values are None, never zero."""

    option_id: str
    template: str
    params: dict[str, Any]
    start_position: list[float] | None
    end_position: list[float] | None
    displacement: list[float] | None
    terminated_by: str
    duration_s: float | None
    receipts: list[dict[str, Any]] = field(default_factory=list)
    scene: str | None = None
    # What the option measured about the place beside what it was asked to do (e.g. a wall's minimum
    # height): additive, and kept apart from params, which stay exactly the spec's.
    measured: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.terminated_by not in TERMINATIONS:
            raise ValueError(f"unknown termination {self.terminated_by!r}")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
