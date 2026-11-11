"""Measured objective items, independent of who supplied them."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import Strict
from .investigation import DirectiveOutcome, Expected


class PressCount(Strict):
    kind: Literal["press_count"] = "press_count"
    action: str = Field(min_length=1)
    required: int = Field(gt=0)
    window_s: float | None = Field(None, ge=0)
    region_id: str | None = None
    within: float | None = Field(None, gt=0)


class Postcondition(Strict):
    kind: Literal["postcondition"] = "postcondition"
    expected: Expected | None
    deadline_game_s: float | None = Field(None, gt=0)


Criterion = Annotated[PressCount | Postcondition, Field(discriminator="kind")]


class ObjectiveItem(Strict):
    item_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    text: str = Field(min_length=1)
    criterion: Criterion
    count: int | None = Field(None, ge=0)
    sent: int | None = Field(None, ge=0, description="Presses of the input that reached the game, scored or not; "
                                                     "null when the source does not count them")
    status: DirectiveOutcome
    evidence_seq: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _progress(self) -> "ObjectiveItem":
        if self.count is not None and not isinstance(self.criterion, PressCount):
            raise ValueError("a count requires a press-count criterion")
        if self.sent is not None and not isinstance(self.criterion, PressCount):
            raise ValueError("a sent count requires a press-count criterion")
        if self.count is None and isinstance(self.criterion, PressCount) and self.status is not DirectiveOutcome.unknown:
            raise ValueError("an unmeasured press count has unknown status")
        if any(seq < 0 for seq in self.evidence_seq):
            raise ValueError("evidence sequence numbers must be non-negative")
        return self
