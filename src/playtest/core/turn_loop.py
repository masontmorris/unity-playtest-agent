"""Turn loop: S1's answer is validated, bound and executed, never repaired.

Each turn observes once, checks its epoch boundary, adopts a helper result that was already finished
at turn entry, builds the `ChoiceSet` that answer must
name (bound to that observation's `seq`), states the measured facts, builds S1's context (the
observation's age, `pending_help`, the choices, and the directives the facts source tracks, asked for
straight after its facts), asks the S1 port, and validates the answer with
`parse_decision`. The loop then executes only the chosen action, or on a help turn exactly the
`while_pending` choice S1 named. Every step is written to the injected `TraceSink`
(`playtest.core.trace_sink` defines the vocabulary); with no sink it records nothing.

Law 1 branches:

- invalid answer, or a provider that gave none: the turn is rejected, no input is sent, nothing is
  repaired or substituted, and the problem is recorded.
- `wait` / `finish`: no input. In real time a `wait` choice whose `params` hold a positive finite
  `duration_game_s` then sleeps that many game seconds (`duration / time_scale` wall seconds) before the
  turn ends, so the next observation is taken after it; a wait without the param ends at once. A stepped
  clock never sleeps.
- action / skill: one `ActRequest` bound to the observation the set was built from; a bridge
  rejection is recorded as `rejected_stale` with no substitute input.
- escalate / delegate: at most one helper request is open. A repeat while one is open is recorded
  and starts nothing. `while_pending` runs exactly the choice S1 named; a missing or invalid one
  defaults to `wait` and records `policy_default`, while the valid primary survives.

A finished helper's result is adopted once, after a later turn's observation passes the epoch check
and before facts and context are built, and shown to S1 with its source and age. A boundary discards
pending help. `stop` cancels a pending helper and releases every held input. Nothing here retries,
and no score, threshold or timer opens help or picks an action.

With a reader port, the readers are read once before each observation (never between it and the act) and
their values are installed in that observation's `public`. With a watch, each observation and each measured
effect is read once more: the watch's lines join the facts S1 reads, and every suspicion it raises is
written to the trace as a `suspicion` entry and stated to S1 as a fact. A suspicion is a fact; it is not
help, it ranks nothing and it changes no choice.

A scripted source passed to `run_turn` (a callable taking the set and the reading) replaces the S1
port for that turn: it builds no context and makes no provider call.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import inspect
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from playtest.contracts.bridge import Observation, StopResult
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Suspicion
from playtest.contracts.loop import (
    Choice,
    ChoiceKind,
    ChoiceSet,
    Decision,
    OperationOutcome,
    OperationStatus,
    TurnOutcome,
    TurnRecord,
    WhilePending,
)
from playtest.contracts.trace import TraceAuthor, TraceKind

from .bridge_client import BridgeClient
from .context import build_context, canonical
from .decision_memory import DecisionMemory, json_ready as _json_ready
from .decisions import parse_decision
from .execution import ExecutionController
from .facts import RecordFacts
from .findings import RaisedSuspicion
from .observations import ObservationStore
from .ports import (
    DirectiveView,
    EffectProbe,
    EffectReport,
    Executed,
    FactsSource,
    HelpPort,
    HelpRequest,
    HelpResult,
    ReaderPort,
    ReaderSample,
    RestartOutcome,
    RestartPort,
    S1Answer,
    S1Call,
    S1Port,
    TurnReport,
    ViewSource,
    Watch,
)
from .readers import install
from .suspicion import suspicion_line
from .trace_sink import TraceSink

DecisionSource = Callable[[ChoiceSet, Observation | None], Any]

_HELP_KINDS = (ChoiceKind.escalate, ChoiceKind.delegate)
_EXECUTABLE_KINDS = (ChoiceKind.action, ChoiceKind.skill)
_NO_SOURCE: Any = object()
_STOP_REASONS = ("user", "reset", "scene_change", "disconnect", "error", "finish")
# A reading earlier than its own timestamp by less than the clock can resolve is zero, not unknown.
_WALL_RESOLUTION_S = time.get_clock_info("time").resolution


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_s(since: datetime | None, now: datetime) -> float | None:
    """Wall seconds from `since` to `now`; unknown when `since` is unknown or `now` is earlier by more than the clock resolves."""
    if since is None:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    delta = (now - since).total_seconds()
    if delta >= 0:
        return delta
    return 0.0 if delta >= -_WALL_RESOLUTION_S else None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class _Pending:
    """The one open help request."""

    turn: int
    kind: str
    choice_id: str
    role: str
    label: str
    reason: str | None
    opened_seq: int
    opened_game_s: float | None
    opened_wall: datetime
    entry: int | None = None
    task: "asyncio.Task[HelpResult] | None" = None
    resolved: HelpResult | None = None
    finished_wall: datetime | None = None


@dataclass(frozen=True)
class _Guidance:
    """An adopted helper result. It stays in S1's context, with a growing age, once adopted."""

    source: str | None
    kind: str
    choice_id: str
    requested_turn: int
    adopted_turn: int
    finished_wall: datetime
    waited_s: float | None
    result: Any = field(default=None)


class TurnLoop:
    def __init__(
        self,
        client: BridgeClient,
        *,
        choices: Sequence[Choice],
        clock_mode: ClockMode = ClockMode.realtime,
        max_age_s: float = 0.5,
        lease_s: float | None = None,
        leases: Mapping[str, float] | None = None,
        depends_on: tuple[str, ...] = (),
        objective: str | None = None,
        s1: S1Port | None = None,
        view: ViewSource | None = None,
        help: HelpPort | None = None,
        facts: FactsSource | None = None,
        effects: EffectProbe | None = None,
        readers: ReaderPort | None = None,
        watch: Watch | None = None,
        sink: TraceSink | None = None,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        restart: RestartPort | None = None,
        generation: str | None = None,
        time_left: Callable[[], float | None] | None = None,
    ) -> None:
        if s1 is not None and not (objective and objective.strip()):
            raise ValueError("a loop with an S1 port needs an objective")
        if restart is not None and not any(choice.kind is ChoiceKind.restart_play_mode for choice in choices):
            raise ValueError("a loop with a restart port must offer the restart choice")
        self._client = client
        self._original_client = client  # the caller owns this one; adopted clients are closed here
        self._choices = list(choices)
        ChoiceSet(set_version=0, built_from_seq=0, choices=self._choices)  # fail now on duplicate ids
        self._clock_mode = clock_mode
        self._store = ObservationStore()
        self._executor_options = {"max_age_s": max_age_s, "lease_s": lease_s, "leases": leases,
                                  "depends_on": depends_on}
        self._executor = ExecutionController(client, **self._executor_options)
        self._objective = objective
        self._s1 = s1
        self._view = view
        self._help = help
        self._facts: FactsSource = facts if facts is not None else RecordFacts()
        self._effects = effects
        self._readers = readers
        self._watch = watch
        self._restart = restart
        self._generation = generation
        self._time_left = time_left
        self._reset_summary: dict[str, Any] | None = None
        self._suspicions: list[RaisedSuspicion] = []
        self._unshown: list[RaisedSuspicion] = []  # raised since S1's last context, to be stated in the next one
        self._observation_entries: dict[int, int] = {}
        self._sink = sink
        self._clock = clock
        self._sleep = sleep
        self._set_version = 0
        self._turns: list[TurnRecord] = []
        self._reports: list[TurnReport] = []
        self._memory = DecisionMemory()
        self._memory_previous: Observation | None = None
        self._previous: Observation | None = None
        self._pending: _Pending | None = None
        self._retired_helpers: set[asyncio.Task[HelpResult]] = set()
        self._adoptions: list[Any] = []
        self._guidance: list[_Guidance] = []
        self._cancelled_helpers = 0
        self._halt: str | None = None
        self._stopped = False
        self._stop_result: StopResult | None = None

    @property
    def store(self) -> ObservationStore:
        return self._store

    @property
    def client(self) -> BridgeClient:
        """The client the loop currently acts through: the original, or the restarted session's new one."""
        return self._client

    @property
    def generation(self) -> str | None:
        """The play generation the current client belongs to; `None` outside an Editor-owned host."""
        return self._generation

    @property
    def turns(self) -> list[TurnRecord]:
        return list(self._turns)

    @property
    def restart_history(self) -> list[OperationOutcome]:
        """Every selected host operation with its explicit outcome, in turn order."""
        return [record.operation for record in self._turns if record.operation is not None]

    @property
    def reports(self) -> list[TurnReport]:
        return list(self._reports)

    @property
    def last_report(self) -> TurnReport | None:
        return self._reports[-1] if self._reports else None

    @property
    def halt_reason(self) -> str | None:
        """Why the S1 port asked the harness to end the run, or `None`."""
        return self._halt

    @property
    def suspicions(self) -> list[RaisedSuspicion]:
        """Every suspicion raised so far with the trace entry it was written as, in the order raised."""
        return list(self._suspicions)

    @property
    def observation_entries(self) -> dict[int, int]:
        """Bridge observation `seq` to the `trace_seq` of its entry, for citing what a model saw."""
        return dict(self._observation_entries)

    @property
    def helper_count(self) -> int:
        return 1 if self._pending is not None else 0

    @property
    def pending_helper(self) -> dict[str, Any] | None:
        if self._pending is None:
            return None
        return {"turn": self._pending.turn, "kind": self._pending.kind, "choice": self._pending.choice_id}

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

    # --- one turn ---------------------------------------------------------------------------

    async def run_turn(self, decide: DecisionSource | Any = _NO_SOURCE) -> TurnRecord:
        scripted = decide is not _NO_SOURCE
        if not scripted and self._s1 is None:
            raise ValueError("no decision source: give the loop an S1 port or pass a scripted source")
        turn = len(self._turns)
        pending = self._pending
        ready: HelpResult | asyncio.Task[HelpResult] | None = None
        if pending is not None:
            if pending.resolved is not None:
                ready = pending.resolved
            elif pending.task is not None and pending.task.done():
                ready = pending.task
        if self._effects is not None:
            await self._effects.before_turn(turn)
        sample = await self._readers.read() if self._readers is not None else None  # before the observation, never between it and the act
        started = time.perf_counter()
        observation = await self._client.observe_once()
        if sample is not None:
            observation = install(observation, sample)
        self._store.add(observation)
        observed = self._emit(TraceKind.observation, observation.model_dump(mode="json"), TraceAuthor.bridge, turn)
        if observed is not None:
            self._observation_entries[observation.seq] = observed
        boundary = self._memory.observe(self._memory_previous, observation, turn=turn, trace_seq=observed)
        self._memory_previous = observation
        if boundary and self._pending is not None:
            self._cancel_pending(self._pending, "observation_boundary", turn=turn)
        elif ready is not None and pending is self._pending:
            self._adopt_finished_help(pending, ready, turn)
        choice_set = ChoiceSet(
            set_version=self._set_version,
            built_from_seq=observation.seq,
            choices=self._choices,
        )
        self._set_version += 1
        offered = self._emit(TraceKind.choice_set, choice_set.model_dump(mode="json"), TraceAuthor.core, turn, [observed])
        noticed = self._notice(observation, sample, turn, observed, stated=not scripted)

        payload: dict[str, Any] | None = None
        context: int | None = None
        calls: list[int | None] = []
        if scripted:
            answer = S1Answer(answer=decide(choice_set, observation) if callable(decide) else decide)
        else:
            facts = [*await self._facts.facts(self._reports, observation), *noticed]
            directives = self._directive_view(observation)  # right after the facts that evaluated this observation
            view = self._view.sample(observation) if self._view is not None else None
            now = self._clock()
            reset_summary, self._reset_summary = self._reset_summary, None
            payload = build_context(
                objective=self._objective or "",
                observation=observation,
                choices=choice_set,
                facts=facts,
                directive=directives.current if directives is not None else None,
                directive_history=directives.closed if directives is not None else (),
                directive_provenance=directives.provenance if directives is not None else None,
                pending_help=self._pending_view(observation, now),
                age_s=_elapsed_s(observation.wall_utc, now),
                sample_gap_s=self._gap_s(observation),
                guidance=self._guidance_view(now),
                game_view=view.statement if view is not None else None,
                recent_history=self._memory.snapshot() if self._memory.has_records else None,
                reset_summary=reset_summary,
            )
            if view is not None:
                payload["game_view_sha256"] = view.sha256
                payload["game_view_status"] = view.status
            context = self._emit(
                TraceKind.context,
                {"sha256": _sha256(canonical(payload)), "payload": payload},
                TraceAuthor.core, turn, [observed, offered],
            )
            answer = await self._ask(payload, view.image if view is not None else None, view is not None)
            calls = [self._emit(TraceKind.s1_call, call.body(), TraceAuthor.s1, turn, [context])
                     for call in answer.calls]

        parsed = parse_decision(answer.answer, choice_set, self._clock_mode)
        decided = self._emit(
            TraceKind.decision,
            {
                "answer": _json_ready(answer.answer),
                "clock_mode": self._clock_mode.value,
                "valid": parsed.valid,
                "error": parsed.error,
                "policy_default": parsed.policy_default,
                "decision": parsed.decision.model_dump(mode="json") if parsed.decision is not None else None,
            },
            TraceAuthor.core, turn, [offered, *calls],
        )
        raw_choice = answer.answer.get("choice_id") if isinstance(answer.answer, dict) else None
        if not isinstance(raw_choice, str):
            raw_choice = None

        sent: list[Executed] = []
        entries: list[int | None] = []
        effect_entries: list[int | None] = []
        selected: Choice | None = None
        executed_id: str | None = None
        while_pending: WhilePending | None = None
        policy_default = False
        help_event: str | None = None
        operation: OperationOutcome | None = None
        restart_refs: list[int | None] = []

        if not parsed.valid or parsed.decision is None:
            outcome = TurnOutcome.rejected_invalid
        else:
            decision = parsed.decision
            selected = choice_set.get(decision.choice_id)
            assert selected is not None
            if selected.kind is ChoiceKind.restart_play_mode:
                operation, restart_entries = await self._restart_turn(selected, decided, turn)
                restart_refs = restart_entries
                outcome = TurnOutcome.restarted
            elif selected.kind in _EXECUTABLE_KINDS:
                executed, entry, effect_entry = await self._send(
                    selected, observation, "choice", decided, turn, observed, raw_choice, selected.id, help_event)
                sent.append(executed)
                entries.append(entry)
                effect_entries.append(effect_entry)
                if executed.execution.rejected:
                    outcome = TurnOutcome.rejected_stale
                else:
                    outcome, executed_id = TurnOutcome.executed, selected.id
            elif selected.kind in _HELP_KINDS:
                help_event = self._help_request(selected, decision, observation, payload, turn, decided)
                while_pending = decision.while_pending
                policy_default = parsed.policy_default
                if while_pending is None:
                    # Stepped clocks carry no pending input; the help is recorded as waited so the trace stays adherence-clean.
                    while_pending, policy_default = WhilePending(mode="wait"), True
                if while_pending.mode == "continue":
                    target = choice_set.get(while_pending.choice_id or "")
                    if target is not None and target.kind in _EXECUTABLE_KINDS:
                        executed, entry, effect_entry = await self._send(
                            target, observation, "while_pending", decided, turn, observed, raw_choice,
                            selected.id, help_event)
                        sent.append(executed)
                        entries.append(entry)
                        effect_entries.append(effect_entry)
                        if not executed.execution.rejected:
                            executed_id = target.id
                outcome = TurnOutcome.escalated if selected.kind is ChoiceKind.escalate else TurnOutcome.delegated
            else:
                outcome = TurnOutcome.finished
                if selected.kind is ChoiceKind.wait:
                    await self._wait_out(selected, observation)

        record = TurnRecord(
            turn=turn,
            set_version=choice_set.set_version,
            based_on_seq=observation.seq,
            raw_choice=raw_choice,
            effective_choice=selected.id if selected is not None else None,
            executed_action=executed_id,
            outcome=outcome,
            while_pending=while_pending,
            policy_default=policy_default,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            operation=operation,
        )
        self._turns.append(record)
        turn_entry = self._emit(
            TraceKind.turn, record.model_dump(mode="json"), TraceAuthor.core, turn,
            [observed, offered, context, decided, *entries, *restart_refs],
        )
        report = TurnReport(
            record=record,
            observation=observation,
            selected=selected,
            problem=None if parsed.valid else parsed.error,
            failure=answer.failure,
            help_event=help_event,
            executed=tuple(sent),
            halt=answer.halt,
        )
        self._reports.append(report)
        if operation is not None and operation.status is OperationStatus.ready:
            # The adopted first observation is the new epoch's only current reading. The pre-reset one must
            # not enter the fresh memory, nor stand as `_previous` for the next gap or boundary check.
            adopted = self._store.latest()
            if adopted is not None:
                self._previous = adopted
                self._memory_previous = adopted
        else:
            self._memory.record(report, observation_trace_seq=observed, turn_trace_seq=turn_entry,
                                execution_trace_seqs=entries, effect_trace_seqs=effect_entries)
            self._previous = observation
        if answer.halt is not None:
            self._halt = answer.halt
        return record

    async def run(self, decide: DecisionSource | Any = _NO_SOURCE, *, max_turns: int = 50) -> list[TurnRecord]:
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

    # --- the S1 port ------------------------------------------------------------------------

    async def _ask(self, payload: dict[str, Any], image: bytes | None = None, with_view: bool = False) -> S1Answer:
        """One provider turn. A port that raises is a provider failure: recorded, never retried or replaced."""
        assert self._s1 is not None
        try:
            got = (await self._s1.decide(copy.deepcopy(payload), image=image) if with_view
                   else await self._s1.decide(copy.deepcopy(payload)))
        except Exception as exc:  # noqa: BLE001 - a failed provider rejects the turn; it never ends it with a guess
            problem = f"port error: {type(exc).__name__}"
            return S1Answer(answer=None, calls=(S1Call(problem=problem),), failure=problem)
        if not isinstance(got, S1Answer):
            problem = "port error: the port did not return an S1Answer"
            return S1Answer(answer=None, calls=(S1Call(problem=problem),), failure=problem)
        if got.answer is None and got.failure is None:
            return dataclasses.replace(got, failure="the provider gave no answer")
        return got

    def _directive_view(self, observation: Observation) -> DirectiveView | None:
        """The directives the facts source tracks, as of this observation, or `None` for no section.

        Asked once per context, straight after that source's `facts` evaluated this observation, which is after
        the epoch check and any adoption: it can state only what was adopted in this epoch, scored against this
        reading. A source without the hook, one that tracks nothing, and one that raises or answers with
        anything but a `DirectiveView` all give S1 the context it would have had. The directives still reach it as
        facts, and nothing it may choose changes.
        """
        collect = getattr(self._facts, "directive_view", None)
        if collect is None:
            return None
        try:
            view = collect(observation)
        except Exception:  # noqa: BLE001 - context only: a faulty source gives S1 less, never a different turn
            return None
        return view if isinstance(view, DirectiveView) else None

    def _gap_s(self, observation: Observation) -> float | None:
        """Wall seconds since the previous turn's observation; unknown on the first turn."""
        if self._previous is None:
            return None
        return _elapsed_s(self._previous.wall_utc, observation.wall_utc)

    # --- waiting ----------------------------------------------------------------------------

    async def _wait_out(self, choice: Choice, observation: Observation) -> None:
        """Sleep a wait choice's `duration_game_s` of game time. No input is sent; only the next observation waits."""
        seconds = self._wait_s(choice, observation)
        if seconds is not None:
            await self._sleep(seconds)

    def _wait_s(self, choice: Choice, observation: Observation) -> float | None:
        """Wall seconds to sleep, or `None` for none: a stepped clock, no valid duration, or a game clock not running."""
        if self._clock_mode is not ClockMode.realtime:
            return None
        duration = choice.params.get("duration_game_s")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            return None
        if not math.isfinite(duration) or duration <= 0 or not observation.time_scale > 0:
            return None
        seconds = duration / observation.time_scale
        return seconds if math.isfinite(seconds) and seconds > 0 else None

    # --- host restart -----------------------------------------------------------------------

    def _operation_deadline_s(self) -> float:
        """The smaller of the run's remaining wall time and the operation's 60-second bound."""
        limit = 60.0
        if self._time_left is not None:
            left = self._time_left()
            if left is not None and math.isfinite(left):
                limit = min(limit, max(0.0, left))
        return limit

    async def _release_for_restart(self, turn: int) -> bool:
        """Invalidate pending help, then release every held input; only a verified release permits dispatch."""
        pending = self._pending
        if pending is not None:
            self._cancel_pending(pending, "restart", turn=turn)
        self._memory.mark_boundary("reset")
        result = await self._client.stop("reset")
        self._stop_result = result
        return result is not None and result.release_verified is True

    async def _restart_turn(
        self, choice: Choice, decided: int | None, turn: int,
    ) -> tuple[OperationOutcome, list[int | None]]:
        """Run one S1-selected host restart outside decision turns and record exactly one request and terminal entry."""
        ticket = str(uuid.uuid4())
        from_generation = self._generation
        request_entry = self._emit(
            TraceKind.restart,
            {"event": "request", "ticket_id": ticket, "from_generation": from_generation,
             "to_generation": None, "status": "requested", "reason": None,
             "release_verified": None, "checks": 0, "waited_s": 0.0},
            TraceAuthor.core, turn, [decided],
        )
        started = self._clock()
        released: bool | None = None
        outcome: RestartOutcome | None = None
        if self._restart is None or from_generation is None:
            reason = ("no host restart operation is configured" if self._restart is None
                      else "the current play generation is unknown")
            outcome = RestartOutcome(status=OperationStatus.failed, ticket_id=ticket,
                                     from_generation=from_generation, reason=reason)
        else:
            try:
                released = await self._release_for_restart(turn)
            except Exception as exc:  # noqa: BLE001 - an unverified release fails closed, never retried
                # Nothing was measured about the release: `null`, not a claimed `false`.
                outcome = RestartOutcome(
                    status=OperationStatus.failed, ticket_id=ticket, from_generation=from_generation,
                    reason=f"input release failed: {type(exc).__name__}: {exc}")
            if outcome is None and not released:
                outcome = RestartOutcome(
                    status=OperationStatus.failed, ticket_id=ticket, from_generation=from_generation,
                    reason="input release was not verified")
            if outcome is None:
                try:
                    outcome = await self._restart.restart(
                        ticket_id=ticket, from_generation=from_generation,
                        previous_endpoint=self._client.endpoint,
                        previous_session=self._client.session,
                        deadline_s=self._operation_deadline_s(), release_verified=released)
                except Exception as exc:  # noqa: BLE001 - a failed operation leaves no usable session
                    outcome = RestartOutcome(
                        status=OperationStatus.failed, ticket_id=ticket, from_generation=from_generation,
                        reason=f"host restart failed: {type(exc).__name__}: {exc}")
        if outcome.status is OperationStatus.ready:
            if outcome.client is None or outcome.observation is None:
                outcome = dataclasses.replace(
                    outcome, status=OperationStatus.failed, client=None, observation=None,
                    reason="the host reported ready without a new client and first observation")
            else:
                try:
                    await self._adopt_restart(outcome, turn)
                except Exception as exc:  # noqa: BLE001 - keep the new client so the run can release it
                    outcome = dataclasses.replace(
                        outcome, status=OperationStatus.failed,
                        reason=f"the restarted session could not be rebound: {type(exc).__name__}: {exc}")
        waited = outcome.waited_s
        if waited is None:
            waited = max(0.0, (self._clock() - started).total_seconds())
        terminal_entry = self._emit(
            TraceKind.restart,
            {"event": "completed" if outcome.status is OperationStatus.ready else "failed",
             "ticket_id": ticket, "from_generation": from_generation,
             "to_generation": outcome.to_generation,
             "status": outcome.status.value, "reason": outcome.reason,
             "release_verified": released,
             "checks": outcome.checks, "waited_s": waited},
            TraceAuthor.core, turn, [request_entry],
        )
        operation = OperationOutcome(
            kind="restart_play_mode", status=outcome.status, ticket_id=ticket,
            from_generation=from_generation, to_generation=outcome.to_generation, reason=outcome.reason)
        return operation, [request_entry, terminal_entry]

    async def _adopt_restart(self, outcome: RestartOutcome, turn: int) -> None:
        """Adopt the new client and reset every scene-bound reading, keeping prior evidence as history.

        A client this loop adopted for an earlier generation is closed here: the caller owns only the
        client it passed in. The facts source is rebound too, so a directive or open hypothesis bound to
        the previous session is invalidated now rather than lingering until some later facts call.
        """
        assert outcome.client is not None and outcome.observation is not None
        assert outcome.to_generation is not None
        previous_client = self._client
        self._client = outcome.client
        self._executor = ExecutionController(outcome.client, **self._executor_options)
        self._store = ObservationStore()
        self._store.add(outcome.observation)
        self._observation_entries = {}
        self._memory = DecisionMemory()
        self._memory_previous = None
        self._previous = None
        self._guidance.clear()
        self._unshown.clear()
        self._pending = None
        self._generation = outcome.to_generation
        for port in (self._view, self._readers, self._watch, self._effects, self._help, self._facts):
            rebind = getattr(port, "rebind", None)
            if rebind is None:
                continue
            result = rebind(client=outcome.client, observation=outcome.observation,
                            generation=outcome.to_generation)
            if inspect.isawaitable(result):
                await result
        if previous_client is not self._original_client:
            await _close_quietly(previous_client)
        self._reset_summary = {
            "turn": turn,
            "from_generation": outcome.from_generation,
            "to_generation": outcome.to_generation,
            "observation_seq": outcome.observation.seq,
            "scene_path": outcome.scene_path,
            "waited_s": outcome.waited_s,
            "checks": outcome.checks,
            "invalidated": ("scene-bound guidance, directives, open hypotheses, readers and effect "
                            "baselines; earlier evidence is historical"),
        }

    # --- execution --------------------------------------------------------------------------

    async def _send(
        self, choice: Choice, observation: Observation, purpose: str, decided: int | None, turn: int,
        observed: int | None, raw_choice: str | None, effective_choice: str, help_event: str | None,
    ) -> tuple[Executed, int | None, int | None]:
        """Send one chosen input, bound to the observation its set was built from, and measure its effect."""
        request = self._executor.request_for(choice, based_on_seq=observation.seq)
        try:
            execution = await self._executor.send(request)
        except BaseException:
            # The request may have reached the bridge; the trace keeps it even though no record came back.
            entry = self._emit(
                TraceKind.execution, {"purpose": purpose, "request": request, "records": []},
                TraceAuthor.bridge, turn, [decided],
            )
            self._memory.interrupted_attempt(
                turn=turn, observation=observation, observation_trace_seq=observed,
                raw_choice=raw_choice, effective_choice=effective_choice, help_event=help_event,
                choice_id=choice.id, purpose=purpose, execution=None, effect=None,
                trace_seq=entry, effect_trace_seq=None)
            raise
        entry = self._emit(
            TraceKind.execution,
            {"purpose": purpose, "request": request,
             "records": [record.model_dump(mode="json") for record in execution.records]},
            TraceAuthor.bridge, turn, [decided],
        )
        effect = None
        measured = None
        try:
            if not execution.rejected and self._effects is not None:
                effect = await self._effects.measure(choice, execution)
                if effect is not None:
                    measured = self._emit(
                        TraceKind.effect,
                        {"choice_id": choice.id, "action": request["action"],
                         "expected": _json_ready(effect.expected), "observed": _json_ready(effect.observed)},
                        TraceAuthor.core, turn, [entry],
                    )
                    self._note_effect(choice, effect, turn, [seq for seq in (entry, measured) if seq is not None])
        except BaseException:
            self._memory.interrupted_attempt(
                turn=turn, observation=observation, observation_trace_seq=observed,
                raw_choice=raw_choice, effective_choice=effective_choice, help_event=help_event,
                choice_id=choice.id, purpose=purpose, execution=execution, effect=effect,
                trace_seq=entry, effect_trace_seq=measured)
            raise
        return Executed(purpose=purpose, choice=choice, execution=execution, effect=effect), entry, measured

    # --- suspicions -------------------------------------------------------------------------

    def _notice(self, observation: Observation, sample: ReaderSample | None, turn: int, observed: int | None,
                *, stated: bool) -> list[str]:
        """What the watch states about this observation, as facts: its lines, then every suspicion not yet stated.

        New suspicions are written to the trace as they are raised. A turn with no S1 context (`stated` false,
        a scripted turn) records them and leaves the ones still waiting to be stated for the next context.
        Stating them changes nothing S1 may choose.
        """
        if self._watch is None:
            return []
        noticed = self._watch.observe(observation, sample, turn=turn, trace_seq=observed)
        recorded = [self._record_suspicion(suspicion, turn) for suspicion in noticed.suspicions]
        if not stated:
            return []
        lines = list(noticed.lines)
        lines += [suspicion_line(item.suspicion, item.trace_seq) for item in [*self._unshown, *recorded]]
        self._unshown.clear()
        return lines

    def _note_effect(self, choice: Choice, effect: EffectReport, turn: int, cited: list[int]) -> None:
        """Record what a measured effect makes the watch raise; S1 reads it in the next context, with the effect's own line."""
        if self._watch is None:
            return
        for suspicion in self._watch.effect(choice, effect, turn=turn, trace_seqs=cited):
            self._unshown.append(self._record_suspicion(suspicion, turn))

    def _record_suspicion(self, suspicion: Suspicion, turn: int) -> RaisedSuspicion:
        seq = self._emit(TraceKind.suspicion, suspicion.model_dump(mode="json"), TraceAuthor.core, turn,
                         suspicion.evidence_seq)
        raised = RaisedSuspicion(seq, suspicion)
        self._suspicions.append(raised)
        return raised

    # --- help -------------------------------------------------------------------------------

    def _help_request(
        self,
        choice: Choice,
        decision: Decision,
        observation: Observation,
        payload: dict[str, Any] | None,
        turn: int,
        decided: int | None,
    ) -> str:
        """Open the one help request, or record a repeat while one is open. Returns the event."""
        if self._pending is not None:
            self._emit(
                TraceKind.help,
                {"event": "repeat", "choice_id": choice.id, "kind": choice.kind.value,
                 "opened_turn": self._pending.turn, "source": None, "result": None, "reason": None},
                TraceAuthor.core, turn, [decided],
            )
            return "repeat"
        pending = _Pending(
            turn=turn,
            kind=choice.kind.value,
            choice_id=choice.id,
            role=choice.role or "",
            label=choice.label,
            reason=decision.reason,
            opened_seq=observation.seq,
            opened_game_s=observation.game_s,
            opened_wall=self._clock(),
        )
        pending.entry = self._emit(
            TraceKind.help,
            {"event": "opened", "choice_id": choice.id, "kind": choice.kind.value,
             "opened_turn": turn, "source": None, "result": None, "reason": None},
            TraceAuthor.core, turn, [decided],
        )
        if self._help is not None:
            request = HelpRequest(
                kind=pending.kind, choice_id=pending.choice_id, role=pending.role, label=pending.label,
                reason=pending.reason, turn=turn, based_on_seq=observation.seq,
                context=copy.deepcopy(payload) if payload is not None else {},
            )
            pending.task = asyncio.ensure_future(self._help.run(request))
            def finished(_task: asyncio.Task[HelpResult], p: _Pending = pending) -> None:
                if p.resolved is None:
                    p.finished_wall = self._clock()

            pending.task.add_done_callback(finished)
        self._pending = pending
        return "opened"

    def _adopt_finished_help(self, pending: _Pending, ready: HelpResult | asyncio.Task[HelpResult],
                             turn: int) -> None:
        """Adopt the result that was ready at entry, after the observation epoch check."""
        if self._pending is not pending:
            return
        if isinstance(ready, HelpResult):
            if pending.resolved is ready:
                self._adopt(pending, ready, turn)
            return
        task = ready
        if pending.task is not task or pending.resolved is not None or not task.done():
            return
        if task.cancelled():
            self._drop_pending(pending, "cancelled", turn=turn)
            return
        failure = task.exception()
        if failure is not None:
            result = HelpResult(source=None, result={"error": type(failure).__name__})
        else:
            value = task.result()
            result = value if isinstance(value, HelpResult) else HelpResult(source=None, result=value)
        self._adopt(pending, result, turn)

    def _adopt(self, pending: _Pending, result: HelpResult, turn: int) -> None:
        self._pending = None
        publish = getattr(self._help, "adopt_result", None)
        if publish is not None:
            result = publish(pending.turn, result)
            if not isinstance(result, HelpResult):
                raise TypeError("help publication must return a HelpResult")
        finished = pending.finished_wall or self._clock()
        shown = _json_ready(result.result)
        self._adoptions.append(result.result)
        self._guidance.append(_Guidance(
            source=result.source,
            kind=pending.kind,
            choice_id=pending.choice_id,
            requested_turn=pending.turn,
            adopted_turn=turn,
            finished_wall=finished,
            waited_s=_elapsed_s(pending.opened_wall, finished),
            result=shown,
        ))
        self._emit(
            TraceKind.help,
            {"event": "adopted", "choice_id": pending.choice_id, "kind": pending.kind,
             "opened_turn": pending.turn, "source": result.source, "result": shown, "reason": None},
            TraceAuthor.core, turn, [pending.entry],
        )

    def resolve_helper(self, result: Any, *, source: str | None = None) -> bool:
        """Stage one external result for adoption after a later observation's epoch check."""
        pending = self._pending
        if pending is None or pending.resolved is not None:
            return False
        discard = getattr(self._help, "discard_result", None)
        if discard is not None:
            discard(pending.turn)
        if pending.task is not None:
            self._retire_helper(pending.task)
            pending.task = None
        pending.finished_wall = self._clock()
        pending.resolved = HelpResult(source=source, result=result)
        return True

    def _drop_pending(self, pending: _Pending, reason: str, *, turn: int | None) -> None:
        self._pending = None
        discard = getattr(self._help, "discard_result", None)
        if discard is not None:
            discard(pending.turn)
        self._cancelled_helpers += 1
        self._emit(
            TraceKind.help,
            {"event": "cancelled", "choice_id": pending.choice_id, "kind": pending.kind,
             "opened_turn": pending.turn, "source": None, "result": None, "reason": reason},
            TraceAuthor.core, turn, [pending.entry],
        )

    def _cancel_pending(self, pending: _Pending, reason: str, *, turn: int | None) -> None:
        """Invalidate first, then cancel and consume even a cancellation-resistant task's result."""
        self._drop_pending(pending, reason, turn=turn)
        task = pending.task
        if task is not None:
            self._retire_helper(task)

    def _retire_helper(self, task: asyncio.Task[HelpResult]) -> None:
        """Cancel a replaced task and consume its eventual result or exception."""
        if task.done():
            if not task.cancelled():
                task.exception()
            return
        # Do not hold up the turn if the helper suppresses cancellation. Its discard hook has
        # already invalidated any staged or late result; retain the task until it finishes.
        self._retired_helpers.add(task)

        def consume(done: asyncio.Task[HelpResult]) -> None:
            self._retired_helpers.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(consume)
        task.cancel()

    def _pending_view(self, observation: Observation, now: datetime) -> dict[str, Any] | None:
        """The open request as S1 reads it: what was asked, how long ago in game and wall seconds, and what it waits on."""
        pending = self._pending
        if pending is None:
            return None
        game = None
        if observation.game_s is not None and pending.opened_game_s is not None:
            elapsed = observation.game_s - pending.opened_game_s
            game = elapsed if elapsed >= 0 else None
        return {
            "kind": pending.kind,
            "choice_id": pending.choice_id,
            "label": pending.label,
            "reason": pending.reason,
            "opened_turn": pending.turn,
            "opened_seq": pending.opened_seq,
            "since_game_s": pending.opened_game_s,
            "age_game_s": game,
            "age_wall_s": _elapsed_s(pending.opened_wall, now),
            "waiting_on": pending.role,
            "helper_attached": self._help is not None,
        }

    def _guidance_view(self, now: datetime) -> list[dict[str, Any]] | Mapping[str, Any]:
        """The adopted results as S1 reads them: this list, or the port's layout of exactly this list.

        A port that implements `render_guidance(items)` and `restore_guidance(view)` is believed only when
        `restore_guidance` gives back the very list it was handed. A layout that is lossy, malformed or raises
        is never sent; the list goes as it is, so a faulty renderer can make a context larger and nothing else.
        """
        items = [
            {
                "source": item.source,
                "kind": item.kind,
                "choice_id": item.choice_id,
                "requested_turn": item.requested_turn,
                "adopted_turn": item.adopted_turn,
                "age_s": _elapsed_s(item.finished_wall, now),
                "waited_s": item.waited_s,
                "result": item.result,
            }
            for item in self._guidance
        ]
        render = getattr(self._help, "render_guidance", None)
        restore = getattr(self._help, "restore_guidance", None)
        if not items or render is None or restore is None:
            return items
        try:
            view = render(copy.deepcopy(items))
            if isinstance(view, (Mapping, list)) and canonical(restore(copy.deepcopy(view))) == canonical(items):
                return view
        except Exception:  # noqa: BLE001 - whatever went wrong, S1 gets the list unchanged
            pass
        return items

    # --- stop -------------------------------------------------------------------------------

    async def stop(self, reason: str = "finish", *, error: str | None = None) -> StopResult | None:
        """Cancel a pending helper and release every held input, verified by the bridge. Runs once."""
        if reason not in _STOP_REASONS:
            raise ValueError(f"stop reason must be one of {', '.join(_STOP_REASONS)}, not {reason!r}")
        if self._stopped:
            return self._stop_result
        self._stopped = True
        if reason in ("reset", "scene_change"):
            self._memory.mark_boundary(reason)
        pending = self._pending
        if pending is not None:
            task = pending.task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            elif task is not None and not task.cancelled():
                task.exception()  # a failure nobody adopted is still retrieved, not left to warn at exit
            self._drop_pending(pending, reason, turn=None)
        try:
            result = await self._client.stop(reason)
        except BaseException as exc:
            self._emit(
                TraceKind.stop, {"reason": reason, "result": None, "error": error or type(exc).__name__},
                TraceAuthor.core, None,
            )
            raise
        self._stop_result = result
        self._emit(
            TraceKind.stop,
            {"reason": reason, "result": result.model_dump(mode="json") if result is not None else None,
             "error": error},
            TraceAuthor.core, None,
        )
        return result

    # --- trace ------------------------------------------------------------------------------

    def _emit(
        self,
        kind: TraceKind,
        body: Mapping[str, Any],
        author: TraceAuthor,
        turn: int | None,
        refs: Sequence[int | None] = (),
    ) -> int | None:
        if self._sink is None:
            return None
        return self._sink.append(kind, body, author=author, turn=turn, refs=[r for r in refs if r is not None])


async def _close_quietly(client: BridgeClient) -> None:
    """Close a client the loop no longer acts through; a cleanup failure never changes a decision."""
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 - cleanup of an already released session
        pass
