"""Validate S1's raw answer against the choice set it was given.

Law 1: an invalid answer rejects the turn. It is never repaired, re-ranked or replaced, and the
returned distribution is never consulted. A malformed secondary field never invalidates a valid
primary choice: `while_pending` defaults to `wait` and the default is recorded, and an invalid
`contingent` branch (not a string, or not an id this set offers) is nulled on its own and recorded,
so the branch it spoiled can never be acted on while the other branch and the primary survive.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceKind, ChoiceSet, Contingent, Decision, WhilePending

_HELP_KINDS = (ChoiceKind.escalate, ChoiceKind.delegate)
_BRANCHES = ("if_blocked", "if_succeeded")
_WAIT = WhilePending(mode="wait")


@dataclass(frozen=True)
class Parsed:
    decision: Decision | None
    error: str | None = None
    policy_default: bool = False
    contingent_problems: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.decision is not None


def parse_decision(raw: Any, choices: ChoiceSet, clock_mode: ClockMode) -> Parsed:
    if not isinstance(raw, dict):
        return Parsed(None, "answer is not an object")
    secondary = raw.get("while_pending")
    try:
        # Both secondary answers are judged on their own below: a bad one nulls itself, never the primary.
        decision = Decision.model_validate({**raw, "while_pending": None, "contingent": None})
    except ValidationError as exc:
        return Parsed(None, f"invalid decision: {exc.errors()[0]['msg']}")

    if decision.set_version != choices.set_version:
        return Parsed(None, f"set_version {decision.set_version} != current {choices.set_version}")
    chosen = choices.get(decision.choice_id)
    if chosen is None:
        return Parsed(None, f"unknown choice_id {decision.choice_id!r}")

    contingent, problems = _contingent(raw.get("contingent"), choices)
    decision = decision.model_copy(update={"contingent": contingent})

    if chosen.kind not in _HELP_KINDS or clock_mode is ClockMode.stepped:
        # while_pending only means something on a help turn in real time; ignore it elsewhere.
        return Parsed(decision, contingent_problems=problems)

    pending = _secondary(secondary, choices)
    return Parsed(decision.model_copy(update={"while_pending": pending or _WAIT}),
                  policy_default=pending is None, contingent_problems=problems)


def _contingent(raw: Any, choices: ChoiceSet) -> tuple[Contingent | None, tuple[str, ...]]:
    """The valid branches S1 gave, and one problem line per branch that was dropped."""
    if raw is None:
        return None, ()
    if not isinstance(raw, Mapping):
        return None, ("contingent answer is not an object",)
    kept: dict[str, str | None] = {}
    problems: list[str] = []
    for field in _BRANCHES:
        value = raw.get(field)
        if value is None:
            kept[field] = None
        elif not isinstance(value, str):
            kept[field] = None
            problems.append(f"{field} is not a choice id")
        elif choices.get(value) is None:
            kept[field] = None
            problems.append(f"{field} {value!r} is not an offered choice")
        else:
            kept[field] = value
    if kept["if_blocked"] is None and kept["if_succeeded"] is None:
        return None, tuple(problems)
    return Contingent(**kept), tuple(problems)


def _secondary(raw: Any, choices: ChoiceSet) -> WhilePending | None:
    if raw is None:
        return None
    try:
        pending = WhilePending.model_validate(raw)
    except ValidationError:
        return None
    if pending.mode == "continue":
        target = choices.get(pending.choice_id or "")
        if target is None or target.kind in _HELP_KINDS or target.kind in (ChoiceKind.option, ChoiceKind.finish,
                                                                         ChoiceKind.restart_play_mode):
            return None
    return pending
