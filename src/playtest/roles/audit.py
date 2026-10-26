"""Scheduled audit (stretch, #54): one read-only review of a filtered record per interval.

The auditor's notes are facts with evidence for the record and S1's context. They never escalate,
route or choose (law 2), and they carry no recommended action (law 3).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Literal

from pydantic import Field

from playtest.contracts.common import Strict

from .strict import render


class AuditNote(Strict):
    """Value rules are in `review`, so a bad note is rejected alone."""

    kind: Literal["missed_evidence", "contradiction", "stale_hypothesis", "unscored_directive", "control_problem",
                  "other"]
    evidence_seq: list[int]
    text: str = Field(description="What the record shows, without a recommendation")


class AuditAnswer(Strict):
    notes: list[AuditNote] = Field(max_length=8)


SYSTEM = (
    "You audit an autonomous game playtester's recent record. Report what a careful reviewer would flag: evidence "
    "nobody acted on, records that contradict each other, hypotheses left untested, directives that were never "
    "scored, and actions whose observed effect keeps differing from the expected one. State facts and cite their "
    "sequence numbers. Do not recommend actions; another model decides what to do."
)


def schema(**render_args: Any) -> dict[str, Any]:
    return render(AuditAnswer, **render_args)


def review(answer: AuditAnswer, seqs: Collection[int]) -> list[str]:
    known = set(seqs)
    problems: list[str] = []
    for i, n in enumerate(answer.notes):
        if not n.text.strip():
            problems.append(f"notes[{i}]: text is empty")
        if not n.evidence_seq:
            problems.append(f"notes[{i}]: cites no sequence numbers")
        elif set(n.evidence_seq) - known:
            problems.append(f"notes[{i}]: cites sequence numbers not in the record: {sorted(set(n.evidence_seq) - known)}")
    return problems
