"""Execution controller: bind one chosen action to its observation and run it.

The controller sends a single `ActRequest` and awaits the terminal `ExecutionRecord`. A rejection
comes back as data on that record, never as an exception to repair, and the controller never
retries or substitutes. It returns the request it sent and every record the bridge answered with,
because the trace keeps both. The lease an input runs for is data supplied by the host (one number
for every choice, or one per choice id); the controller never derives it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from playtest.contracts.bridge import ExecStatus, ExecutionRecord
from playtest.contracts.loop import Choice, ChoiceKind

from .bridge_client import BridgeClient

_EXECUTABLE = (ChoiceKind.action, ChoiceKind.skill)


@dataclass(frozen=True)
class Execution:
    """What was sent for one choice and what the bridge answered, terminal record last."""

    request: dict[str, Any]
    records: tuple[ExecutionRecord, ...]

    @property
    def terminal(self) -> ExecutionRecord:
        return self.records[-1]

    @property
    def rejected(self) -> bool:
        return self.terminal.status is ExecStatus.rejected


class ExecutionController:
    def __init__(
        self,
        client: BridgeClient,
        *,
        max_age_s: float = 0.5,
        lease_s: float | None = None,
        leases: Mapping[str, float] | None = None,
        depends_on: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._max_age_s = max_age_s
        self._lease_s = lease_s
        self._leases = dict(leases or {})
        self._depends_on = depends_on

    @property
    def max_age_s(self) -> float:
        return self._max_age_s

    @property
    def lease_s(self) -> float | None:
        return self._lease_s

    @lease_s.setter
    def lease_s(self, value: float | None) -> None:
        self._lease_s = value

    def request_for(
        self,
        choice: Choice,
        *,
        based_on_seq: int,
        max_age_s: float | None = None,
        lease_s: float | None = None,
        depends_on: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """The `act` arguments for one executable choice, bound to the observation it was offered on."""
        if choice.kind not in _EXECUTABLE:
            raise ValueError(f"choice {choice.id!r} of kind {choice.kind.value!r} carries no input")
        action = choice.action if choice.action is not None else choice.skill
        assert action is not None
        if lease_s is None:
            lease_s = self._leases.get(choice.id, self._lease_s)
        return {
            "action_id": uuid.uuid4().hex,
            "choice_id": choice.id,
            "action": action,
            "params": dict(choice.params),
            "based_on_seq": based_on_seq,
            "max_age_s": max_age_s if max_age_s is not None else self._max_age_s,
            "depends_on": list(depends_on if depends_on is not None else self._depends_on),
            "lease_s": lease_s,
        }

    async def send(self, request: dict[str, Any]) -> Execution:
        records = await self._client.act(**request)
        return Execution(request=request, records=tuple(records))

    async def execute(
        self,
        choice: Choice,
        *,
        based_on_seq: int,
        max_age_s: float | None = None,
        lease_s: float | None = None,
        depends_on: tuple[str, ...] | None = None,
    ) -> Execution:
        request = self.request_for(
            choice, based_on_seq=based_on_seq, max_age_s=max_age_s, lease_s=lease_s, depends_on=depends_on
        )
        return await self.send(request)
