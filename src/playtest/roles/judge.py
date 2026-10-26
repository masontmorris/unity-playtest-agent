"""Video judge (stretch, #51): focused perceptual questions about one clip.

The judge sees the clip and neutral questions, never S1's account, the issued actions or the
intended outcome: a judge shown the issued actions echoed them (a wall-bounce score rose from 0.23
to 0.97, build bible 2). Its answers are observations for the record, never a verdict that routes.

Each call is one clip with its own questions, so the question ids are a per-call enum; no chain of
judge calls shares a schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import Field

from playtest.contracts.common import Strict

from .strict import enum_str, render

QuestionId = enum_str("question_ids")


class JudgeItem(Strict):
    question_id: QuestionId
    answer: Literal["yes", "no", "unclear"]
    seen_at_s: list[float] = Field(description="Clip seconds that show it; empty when unclear")
    note: str = Field(description="What is visible, in a few words")


class Unexpected(Strict):
    at_s: float | None
    description: str


class JudgeAnswer(Strict):
    answers: list[JudgeItem]
    unexpected: list[Unexpected] = Field(max_length=5)


SYSTEM = (
    "You judge short game clips. Answer each question only from what the clip shows, with the clip times that "
    "show it; answer unclear when the clip cannot settle it. Then list anything visually unexpected, such as "
    "rendering, motion, collision or interface state that looks wrong. Describe what you see, not what it means "
    "or why it happened."
)


def schema(question_ids: Sequence[str], **render_args: Any) -> dict[str, Any]:
    return render(JudgeAnswer, {"question_ids": question_ids}, **render_args)


def review(answer: JudgeAnswer, question_ids: Sequence[str], clip_s: float) -> list[str]:
    """Every question answered exactly once, with times inside the clip."""
    problems: list[str] = []
    asked = list(question_ids)
    seen = [a.question_id for a in answer.answers]
    for q in asked:
        if seen.count(q) != 1:
            problems.append(f"answers: question {q!r} is answered {seen.count(q)} times")
    for i, a in enumerate(answer.answers):
        if any(not 0 <= t <= clip_s for t in a.seen_at_s):
            problems.append(f"answers[{i}]: a time is outside the {clip_s:g} s clip")
    for i, u in enumerate(answer.unexpected):
        if not u.description.strip():
            problems.append(f"unexpected[{i}]: description is empty")
        if u.at_s is not None and not 0 <= u.at_s <= clip_s:
            problems.append(f"unexpected[{i}]: at_s is outside the {clip_s:g} s clip")
    return problems
