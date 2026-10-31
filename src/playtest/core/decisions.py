"""Validate S1's raw answer against the choice set it was given.

Law 1: an invalid answer rejects the turn. It is never repaired, re-ranked or replaced, and the
returned distribution is never consulted. A malformed secondary field (`while_pending`) never
invalidates a valid primary choice; it defaults to `wait` and the default is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceKind, ChoiceSet, Decision, WhilePending

_HELP_KINDS = (ChoiceKind.escalate, ChoiceKind.delegate)
_WAIT = WhilePending(mode="wait")


@dataclass(frozen=True)
class Parsed:
    decision: Decision | None
    error: str | None = None
    policy_default: bool = False

    @property
    def valid(self) -> bool:
        return self.decision is not None


def parse_decision(raw: Any, choices: ChoiceSet, clock_mode: ClockMode) -> Parsed:
    if not isinstance(raw, dict):
        return Parsed(None, "answer is not an object")
    secondary = raw.get("while_pending")
    try:
        decision = Decision.model_validate({**raw, "while_pending": None})
    except ValidationError as exc:
        return Parsed(None, f"invalid decision: {exc.errors()[0]['msg']}")

    if decision.set_version != choices.set_version:
        return Parsed(None, f"set_version {decision.set_version} != current {choices.set_version}")
    chosen = choices.get(decision.choice_id)
    if chosen is None:
        return Parsed(None, f"unknown choice_id {decision.choice_id!r}")

    if chosen.kind not in _HELP_KINDS or clock_mode is ClockMode.stepped:
        # while_pending only means something on a help turn in real time; ignore it elsewhere.
        return Parsed(decision)

    pending = _secondary(secondary, choices)
    return Parsed(decision.model_copy(update={"while_pending": pending or _WAIT}), policy_default=pending is None)


def _secondary(raw: Any, choices: ChoiceSet) -> WhilePending | None:
    if raw is None:
        return None
    try:
        pending = WhilePending.model_validate(raw)
    except ValidationError:
        return None
    if pending.mode == "continue":
        target = choices.get(pending.choice_id or "")
        if target is None or target.kind in _HELP_KINDS or target.kind in (ChoiceKind.finish,
                                                                         ChoiceKind.restart_play_mode):
            return None
    return pending
