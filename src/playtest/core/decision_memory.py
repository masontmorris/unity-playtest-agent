"""Bounded, chronological facts from completed turns and sent execution attempts.

The two windows advance independently. Counts are scoped to the current observation epoch;
the last boundary reports the cumulative number of records discarded from earlier epochs.
No observations, provider explanations, or choice ordering are retained here.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from playtest.contracts.bridge import TERMINAL_STATUSES, Observation

from .execution import Execution
from .ports import EffectReport, TurnReport

_LIMIT = 8


def json_ready(value: Any) -> Any:
    """Copy data into JSON-native values, matching the loop's trace serialization."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return repr(value)


class DecisionMemory:
    def __init__(self) -> None:
        self._turns: deque[dict[str, Any]] = deque(maxlen=_LIMIT)
        self._attempts: deque[dict[str, Any]] = deque(maxlen=_LIMIT)
        self._turn_total = 0
        self._attempt_total = 0
        self._discarded_turns = 0
        self._discarded_attempts = 0
        self._epoch = 0
        self._boundary: dict[str, Any] | None = None
        self._explicit_boundary: str | None = None

    def mark_boundary(self, reason: str) -> None:
        """Remember an explicit reset/scene stop until the next observation."""
        self._explicit_boundary = reason

    @property
    def has_records(self) -> bool:
        return bool(self._turn_total or self._attempt_total or self._boundary)

    def observe(self, previous: Observation | None, current: Observation, *, turn: int,
                trace_seq: int | None) -> bool:
        """Start a new epoch on explicit signals; return whether a boundary was observed."""
        reasons: list[str] = []
        if self._explicit_boundary is not None:
            reasons.append(self._explicit_boundary)
        self._explicit_boundary = None
        if previous is not None:
            if current.session != previous.session:
                reasons.append("session_change")
            if current.scene != previous.scene and "scene_change" not in reasons:
                reasons.append("scene_change")
            if current.frame < previous.frame:
                reasons.append("frame_regression")
            if (current.game_s is not None and previous.game_s is not None
                    and current.game_s < previous.game_s):
                reasons.append("game_time_regression")
        if not reasons:
            return False
        self._discarded_turns += self._turn_total
        self._discarded_attempts += self._attempt_total
        self._turns.clear()
        self._attempts.clear()
        self._turn_total = self._attempt_total = 0
        self._epoch += 1
        self._boundary = {
            "reasons": reasons,
            "turn": turn,
            "observation_seq": current.seq,
            "trace_seq": trace_seq,
            "discarded_turns": self._discarded_turns,
            "discarded_action_attempts": self._discarded_attempts,
        }
        return True

    def record(self, report: TurnReport, *, observation_trace_seq: int | None,
               turn_trace_seq: int | None, execution_trace_seqs: Sequence[int | None],
               effect_trace_seqs: Sequence[int | None]) -> None:
        record, observation = report.record, report.observation
        common = {
            "turn": record.turn,
            "observation_seq": observation.seq,
            "observation_trace_seq": observation_trace_seq,
            "game_s": observation.game_s,
            "raw_choice": record.raw_choice,
            "effective_choice": record.effective_choice,
            "executed_action": record.executed_action,
            "outcome": record.outcome.value,
            "help_event": report.help_event,
        }
        self._turns.append({**common, "trace_seq": turn_trace_seq})
        self._turn_total += 1
        for sent, execution_trace, effect_trace in zip(
                report.executed, execution_trace_seqs, effect_trace_seqs, strict=True):
            self._attempts.append({**common, **_attempt(sent.choice.id, sent.purpose, sent.execution,
                                                       sent.effect, execution_trace, effect_trace)})
            self._attempt_total += 1

    def interrupted_attempt(self, *, turn: int, observation: Observation, observation_trace_seq: int | None,
                            raw_choice: str | None, effective_choice: str, help_event: str | None,
                            choice_id: str, purpose: str, execution: Execution | None,
                            effect: EffectReport | None, trace_seq: int | None,
                            effect_trace_seq: int | None) -> None:
        """Keep a sent attempt if a turn exits before its turn record can be completed."""
        self._attempts.append({
            "turn": turn, "observation_seq": observation.seq, "observation_trace_seq": observation_trace_seq,
            "game_s": observation.game_s, "raw_choice": raw_choice, "effective_choice": effective_choice,
            "executed_action": None, "outcome": None, "help_event": help_event,
            **_attempt(choice_id, purpose, execution, effect, trace_seq, effect_trace_seq),
        })
        self._attempt_total += 1

    def snapshot(self) -> dict[str, Any]:
        """Return an independent copy; later turns and callers cannot alter earlier snapshots."""
        return copy.deepcopy({
            "epoch": self._epoch,
            "boundary": self._boundary,
            "turns": {"limit": _LIMIT, "total": self._turn_total,
                      "omitted": self._turn_total - len(self._turns), "items": list(self._turns)},
            "action_attempts": {"limit": _LIMIT, "total": self._attempt_total,
                                "omitted": self._attempt_total - len(self._attempts),
                                "items": list(self._attempts)},
        })


def _attempt(choice_id: str, purpose: str, execution: Execution | None, effect: EffectReport | None,
             trace_seq: int | None, effect_trace_seq: int | None) -> dict[str, Any]:
    records = execution.records if execution is not None else ()
    last = records[-1] if records else None
    terminal = last if last is not None and last.status in TERMINAL_STATUSES else None
    return {
        "trace_seq": trace_seq,
        "choice_id": choice_id,
        "purpose": purpose,
        "terminal_status": terminal.status.value if terminal is not None else None,
        "reject_reason": (terminal.reject_reason.value if terminal is not None
                          and terminal.reject_reason is not None else None),
        "applied_duration_s": terminal.applied_duration_s if terminal is not None else None,
        "release_verified": terminal.release_verified if terminal is not None else None,
        "expected_effect": json_ready(effect.expected) if effect is not None else None,
        "observed_effect": json_ready(effect.observed) if effect is not None else None,
        "effect_fact": effect.fact if effect is not None else None,
        "effect_trace_seq": effect_trace_seq,
        "bridge_expected_effects": ([item.model_dump(mode="json") for item in terminal.expected_effect]
                                    if terminal is not None and terminal.expected_effect else None),
        "bridge_observed_effects": ([item.model_dump(mode="json") for item in terminal.observed_effect]
                                    if terminal is not None and terminal.observed_effect else None),
    }
