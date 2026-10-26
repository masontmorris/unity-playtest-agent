"""Turn loop: S1's answer is validated, bound and executed, never repaired.

Each turn observes once, builds the `ChoiceSet` that answer must name, and
calls the scripted decision source (a callable taking the set and the latest
reading and returning the raw answer). `parse_decision` validates the
answer; the loop then executes only the chosen action, or on a help turn
exactly the `while_pending` choice S1 named, and writes a `TurnRecord`.

Law 1 branches:

- invalid answer: the turn is rejected, no input is sent, nothing is repaired.
- `wait` / `finish`: no input.
- action / skill: one `ActRequest` bound to the observation the set was
  built from; a bridge rejection is recorded as `rejected_stale` with no
  substitute input.
- escalate / delegate: one pending helper at most; a `continue`
  `while_pending` runs exactly that choice's input; a missing or invalid
  secondary defaults to `wait` and records `policy_default`, while the valid
  primary survives.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from playtest.contracts.bridge import ExecStatus, Observation, StopResult
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import (
    Choice,
    ChoiceKind,
    ChoiceSet,
    TurnOutcome,
    TurnRecord,
    WhilePending,
)

from .bridge_client import BridgeClient
from .decisions import parse_decision
from .execution import ExecutionController
from .observations import ObservationStore

DecisionSource = Callable[[ChoiceSet, Observation | None], Any]

_HELP_KINDS = (ChoiceKind.escalate, ChoiceKind.delegate)
_EXECUTABLE_KINDS = (ChoiceKind.action, ChoiceKind.skill)


class TurnLoop:
    def __init__(
        self,
        client: BridgeClient,
        *,
        choices: Sequence[Choice],
        clock_mode: ClockMode = ClockMode.realtime,
        max_age_s: float = 0.5,
        lease_s: float | None = None,
        depends_on: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._choices = list(choices)
        self._clock_mode = clock_mode
        self._store = ObservationStore()
        self._executor = ExecutionController(
            client, max_age_s=max_age_s, lease_s=lease_s, depends_on=depends_on
        )
        self._set_version = 0
        self._turns: list[TurnRecord] = []
        self._pending: dict[str, Any] | None = None
        self._adoptions: list[Any] = []
        self._cancelled_helpers = 0

    @property
    def store(self) -> ObservationStore:
        return self._store

    @property
    def turns(self) -> list[TurnRecord]:
        return list(self._turns)

    @property
    def helper_count(self) -> int:
        return 1 if self._pending is not None else 0

    @property
    def pending_helper(self) -> dict[str, Any] | None:
        return dict(self._pending) if self._pending is not None else None

    @property
    def adoptions(self) -> list[Any]:
        return list(self._adoptions)

    @property
    def cancelled_helpers(self) -> int:
        return self._cancelled_helpers

    @property
    def lease_s(self) -> float | None:
        return self._executor.lease_s

    @lease_s.setter
    def lease_s(self, value: float | None) -> None:
        self._executor.lease_s = value

    async def run_turn(self, decide: DecisionSource | Any) -> TurnRecord:
        turn = len(self._turns)
        started = time.perf_counter()
        observation = await self._client.observe_once()
        self._store.add(observation)
        choice_set = ChoiceSet(
            set_version=self._set_version,
            built_from_seq=observation.seq,
            choices=self._choices,
        )
        self._set_version += 1
        raw = decide(choice_set, observation) if callable(decide) else decide
        parsed = parse_decision(raw, choice_set, self._clock_mode)
        raw_choice = raw.get("choice_id") if isinstance(raw, dict) else None
        if not isinstance(raw_choice, str):
            raw_choice = None

        if not parsed.valid:
            return self._record(
                turn, choice_set.set_version, observation.seq,
                raw_choice, None, None, TurnOutcome.rejected_invalid,
                None, False, started,
            )
        decision = parsed.decision
        assert decision is not None
        choice = choice_set.get(decision.choice_id)
        assert choice is not None

        if choice.kind in _EXECUTABLE_KINDS:
            record = await self._executor.execute(choice, based_on_seq=observation.seq)
            if record.status is ExecStatus.rejected:
                return self._record(
                    turn, choice_set.set_version, observation.seq,
                    raw_choice, choice.id, None, TurnOutcome.rejected_stale,
                    None, False, started,
                )
            return self._record(
                turn, choice_set.set_version, observation.seq,
                raw_choice, choice.id, choice.id, TurnOutcome.executed,
                None, False, started,
            )

        if choice.kind in _HELP_KINDS:
            pending = decision.while_pending
            policy_default = parsed.policy_default
            if pending is None:
                # Stepped clocks carry no pending input; the help is recorded
                # as waited so the trace stays adherence-clean.
                pending = WhilePending(mode="wait")
                policy_default = True
            executed: str | None = None
            if pending.mode == "continue":
                assert pending.choice_id is not None
                target = choice_set.get(pending.choice_id)
                assert target is not None
                if target.kind in _EXECUTABLE_KINDS:
                    record = await self._executor.execute(target, based_on_seq=observation.seq)
                    if record.status is not ExecStatus.rejected:
                        executed = target.id
            if self._pending is None:
                self._pending = {"turn": turn, "kind": choice.kind.value, "choice": choice.id}
            outcome = TurnOutcome.escalated if choice.kind is ChoiceKind.escalate else TurnOutcome.delegated
            return self._record(
                turn, choice_set.set_version, observation.seq,
                raw_choice, choice.id, executed, outcome,
                pending, policy_default, started,
            )

        return self._record(
            turn, choice_set.set_version, observation.seq,
            raw_choice, choice.id, None, TurnOutcome.finished,
            None, False, started,
        )

    async def run(self, decide: DecisionSource, *, max_turns: int = 50) -> list[TurnRecord]:
        """Run turns until the source picks a `finish` choice."""
        while len(self._turns) < max_turns:
            record = await self.run_turn(decide)
            if record.effective_choice is not None:
                chosen = next((c for c in self._choices if c.id == record.effective_choice), None)
                if chosen is not None and chosen.kind is ChoiceKind.finish:
                    break
        else:
            raise RuntimeError(f"script did not finish within {max_turns} turns")
        return self.turns

    def resolve_helper(self, result: Any) -> bool:
        """Adopt a pending helper's result exactly once."""
        if self._pending is None:
            return False
        self._adoptions.append(result)
        self._pending = None
        return True

    async def stop(self, reason: str = "finish") -> StopResult | None:
        if self._pending is not None:
            self._pending = None
            self._cancelled_helpers += 1
        return await self._client.stop(reason)

    def _record(
        self,
        turn: int,
        set_version: int,
        based_on_seq: int,
        raw_choice: str | None,
        effective_choice: str | None,
        executed_action: str | None,
        outcome: TurnOutcome,
        while_pending: WhilePending | None,
        policy_default: bool,
        started: float,
    ) -> TurnRecord:
        record = TurnRecord(
            turn=turn,
            set_version=set_version,
            based_on_seq=based_on_seq,
            raw_choice=raw_choice,
            effective_choice=effective_choice,
            executed_action=executed_action,
            outcome=outcome,
            while_pending=while_pending,
            policy_default=policy_default,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
        self._turns.append(record)
        return record
