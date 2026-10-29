"""Execution controller: bind one chosen action to its observation and run it.

The controller sends a single `ActRequest` and awaits the terminal
`ExecutionRecord`. A rejection comes back as data on that record, never as
an exception to repair, and the controller never retries or substitutes.
"""

from __future__ import annotations

import uuid

from playtest.contracts.bridge import ExecutionRecord
from playtest.contracts.loop import Choice, ChoiceKind

from .bridge_client import BridgeClient

_EXECUTABLE = (ChoiceKind.action, ChoiceKind.skill)


class ExecutionController:
    def __init__(
        self,
        client: BridgeClient,
        *,
        max_age_s: float = 0.5,
        lease_s: float | None = None,
        depends_on: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._max_age_s = max_age_s
        self._lease_s = lease_s
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

    async def execute(
        self,
        choice: Choice,
        *,
        based_on_seq: int,
        max_age_s: float | None = None,
        lease_s: float | None = None,
        depends_on: tuple[str, ...] | None = None,
    ) -> ExecutionRecord:
        if choice.kind not in _EXECUTABLE:
            raise ValueError(f"choice {choice.id!r} of kind {choice.kind.value!r} carries no input")
        action = choice.action if choice.action is not None else choice.skill
        assert action is not None
        # A skill owns its ordered holds and total bound in the catalog. The default
        # action lease must not replace that bound. An explicit caller lease still
        # reaches the bridge unchanged for validation against the skill's max_s.
        bound_lease = lease_s
        if bound_lease is None and choice.kind is ChoiceKind.action:
            bound_lease = self._lease_s
        records = await self._client.act(
            action_id=uuid.uuid4().hex,
            choice_id=choice.id,
            action=action,
            params=dict(choice.params),
            based_on_seq=based_on_seq,
            max_age_s=max_age_s if max_age_s is not None else self._max_age_s,
            depends_on=list(depends_on if depends_on is not None else self._depends_on),
            lease_s=bound_lease,
        )
        return records[-1]
