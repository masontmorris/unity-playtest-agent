"""S1 adherence check over a run's turn records (law 1, a hard gate).

For every turn, the choice the loop acted on is the choice S1 returned, and the input that reached
the bridge is that choice, or on a help turn exactly the `while_pending` choice S1 named. Any
difference is a bug and fails the run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from playtest.contracts.loop import TurnOutcome, TurnRecord

_NO_INPUT = (TurnOutcome.rejected_invalid, TurnOutcome.finished, TurnOutcome.restarted)
_HELP = (TurnOutcome.escalated, TurnOutcome.delegated)


@dataclass(frozen=True)
class Violation:
    turn: int
    problem: str


def check_adherence(turns: Iterable[TurnRecord]) -> list[Violation]:
    out: list[Violation] = []
    for t in turns:
        if t.outcome is TurnOutcome.rejected_invalid:
            if t.effective_choice is not None:
                out.append(Violation(t.turn, "rejected turn has an effective choice (answer was repaired)"))
        elif t.effective_choice != t.raw_choice:
            out.append(Violation(t.turn, f"effective {t.effective_choice!r} != raw {t.raw_choice!r}"))

        if t.outcome in _NO_INPUT:
            allowed = None
        elif t.outcome in _HELP:
            if t.while_pending is None:
                out.append(Violation(t.turn, "help turn has no recorded while_pending"))
                continue
            allowed = t.while_pending.choice_id if t.while_pending.mode == "continue" else None
        else:
            allowed = t.effective_choice
        if t.executed_action is not None and t.executed_action != allowed:
            out.append(Violation(t.turn, f"executed {t.executed_action!r}, allowed {allowed!r}"))
        if t.outcome is TurnOutcome.executed and t.executed_action is None:
            out.append(Violation(t.turn, "turn marked executed but no input reached the bridge"))
    return out


def adherence_rate(turns: Iterable[TurnRecord]) -> float | None:
    turns = list(turns)
    if not turns:
        return None
    bad = {v.turn for v in check_adherence(turns)}
    return 1 - len(bad) / len(turns)
