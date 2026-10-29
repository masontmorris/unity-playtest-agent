"""A protocol peer that serves an exported action catalog the way the bridge host does.

`FakeGame` runs three hand-made actions. This peer serves an `ActionCatalog` instead: the file a session
host registers. It applies the order of rules `BridgeServer.HandleAct` and `BridgeActionLease.TryBegin`
apply to an `act`: an unknown name is rejected first; then the stale checks; then a request carrying
params is rejected, a skill's lease must be absent or exactly its `max_s`, and an action's lease (or its
declared default) is its hold. What runs is exactly the declaration. It records every declared input,
value, hold, wait and bound, and answers with one receipt per input in step order, built from the
declaration alone: it chooses, clamps and reinterprets nothing.

The entity's displacement is the fake world's own physics (`effects`: a declaration name to the vector it
adds), so an effect probe has something real to measure. Time is not simulated: a hold completes at once.
This is a test double for the host contract, not evidence about the Unity host.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from loop_fakes import TransformsGame
from playtest.contracts.action_catalog import (
    MAX_HOLD_S,
    MOUSE_DELTA_PIXEL_BOUND,
    ActionCatalog,
    ActionDefinition,
    ActionStep,
    is_pointer_impulse,
)
from playtest.contracts.bridge import ActRequest, AppliedInput, ExecStatus, ExecutionRecord, RejectReason
from playtest.preflight.catalog import serialize


class CatalogGame(TransformsGame):
    def __init__(self, *, catalog: ActionCatalog, effects: Mapping[str, Sequence[float]] | None = None,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._catalog = catalog
        self._effects = {name: [float(v) for v in delta] for name, delta in (effects or {}).items()}

    @property
    def catalog(self) -> ActionCatalog:
        return self._catalog

    def runs(self) -> list[dict[str, Any]]:
        """Every declaration that was applied, in order, with the lease it was asked to run under."""
        return [entry for entry in self.input_log if entry["event"] == "applied"]

    def _reject(self, request: ActRequest, seq: int, session: str, reason: RejectReason, *,
                known: bool) -> ExecutionRecord:
        return self._execution(
            seq, session, action_id=request.action_id, status=ExecStatus.rejected, reject_reason=reason,
            validated_against_seq=request.based_on_seq if known else None)

    def _apply(self, request: ActRequest, seq: int, session: str) -> ExecutionRecord:
        definition = self._catalog.actions.get(request.action)
        if definition is None:
            return self._reject(request, seq, session, RejectReason.unknown_action, known=False)
        stale = self._check_stale(request)
        if stale is not None:
            return self._reject(request, seq, session, stale, known=request.based_on_seq in self._obs_history)
        if _begin_problem(definition, request) is not None:
            return self._reject(request, seq, session, RejectReason.invalid_params, known=True)

        hold = request.lease_s if request.lease_s is not None else definition.default_lease_s
        started_game_s, started_wall_s = self._game_s(), time.time()
        receipts, total_s = _receipts(definition, hold, started_wall_s)
        for axis, amount in enumerate(self._effects.get(request.action, ())):
            self._pos[axis] += amount
        self.input_log.append({
            "event": "applied", "action": request.action, "choice_id": request.choice_id,
            "action_id": request.action_id, "params": dict(request.params), "lease_s": request.lease_s,
            "declaration": serialize(ActionCatalog(actions={request.action: definition}))["actions"][request.action],
            "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
        })
        return ExecutionRecord(
            **self._envelope(seq, session), action_id=request.action_id, status=ExecStatus.completed,
            validated_against_seq=request.based_on_seq,
            input_start_game_s=started_game_s, input_end_game_s=started_game_s + total_s,
            input_start_wall_s=started_wall_s, input_end_wall_s=started_wall_s + total_s,
            applied_duration_s=total_s, release_verified=True, applied_inputs=receipts)


def _begin_problem(definition: ActionDefinition, request: ActRequest) -> str | None:
    """The rules `BridgeActionLease.TryBegin` applies before it touches a device, in its order."""
    if request.params:
        return "act params are not supported: an action runs exactly its declared catalog values"
    if definition.is_skill:
        if request.lease_s is not None and request.lease_s != definition.max_s:
            return "lease_s must be absent or exactly max_s for a skill"
        return None
    lease = request.lease_s if request.lease_s is not None else definition.default_lease_s
    if lease is None or lease <= 0 or lease > MAX_HOLD_S:
        return "lease_s must be positive and at most the catalog maximum"
    return None


def _receipts(definition: ActionDefinition, hold: float | None, start_s: float) -> tuple[list[AppliedInput], float]:
    """One receipt per declared input, in step order, and the total seconds the declaration takes."""
    if definition.is_skill:
        steps: Sequence[ActionStep] = definition.steps or []
    else:
        assert hold is not None and definition.inputs is not None
        steps = [ActionStep(inputs=definition.inputs, hold_s=hold, wait_s=0.0)]
    receipts: list[AppliedInput] = []
    now = start_s
    for index, step in enumerate(steps):
        for item in step.inputs:
            impulse = is_pointer_impulse(item.device, item.control)
            receipts.append(AppliedInput(
                step_index=index, device=item.device, control=item.control, x=item.x, y=item.y,
                input_start_wall_s=now, input_end_wall_s=now + step.hold_s,
                applied_duration_s=None if impulse else step.hold_s,
                applied_delta_x=item.x * MOUSE_DELTA_PIXEL_BOUND if impulse else None,
                applied_delta_y=item.y * MOUSE_DELTA_PIXEL_BOUND if impulse else None,
                release_verified=True))
        now += step.hold_s + step.wait_s
    return receipts, now - start_s
