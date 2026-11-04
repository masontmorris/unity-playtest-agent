"""The session driver: run the loop until S1 finishes, the clock runs out, or a harness rule stops it.

The loop owns each turn; this module only decides when to stop asking for turns. Every stop is a
fail-closed harness rule, never an action choice or an escalation:

- S1 chose `finish` (a normal end);
- the wall-clock limit passed (a normal end);
- N consecutive turns got no usable answer from the provider (default 3), or the provider port asked
  to halt (its call budget is spent);
- an exception: a closed bridge, a failed reading, anything unexpected.

Whatever the cause, the loop is then stopped: a pending helper is cancelled, every held input is
released and the bridge verifies it. A run whose release is unverified is `inconclusive`, never complete.

With a `TokenBudget` the session awaits `budget.ready()` before each turn. That is before the turn's
observation, so the wait never ages the observation an action is bound to. The wait only delays when
the next turn begins: it is not a stop rule, it never decides or opens help, and S1 reads the measured
wait as a fact in that turn's context (`PacingFacts`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from playtest.contracts.bridge import StopResult
from playtest.contracts.loop import Choice, ChoiceKind, OperationOutcome, OperationStatus
from playtest.core.adherence import Violation, check_adherence
from playtest.core.bridge_client import BridgeClient
from playtest.core.context import INSTRUCTIONS
from playtest.core.ports import (
    EffectProbe,
    Executed,
    FactsSource,
    HelpPort,
    ReaderPort,
    RestartPort,
    S1Port,
    ViewSource,
    Watch,
)
from playtest.core.trace_sink import TraceSink
from playtest.core.turn_loop import TurnLoop

from .choices import ManifestChoices, held_choice_labels, with_held_choices
from .pacing import PacingFacts, PacingStats, TokenBudget
from .resources import SessionFacts

MAX_PROVIDER_FAILURES = 3
WALL_LIMIT_S = 600.0
# A restart that timed out with no more than this much wall time left was cut by the run's own end: the
# loop's restart deadline is the run's remaining wall time, so its timeout is the wall limit, not a host fault.
RESTART_WALL_SLACK_S = 0.5
# The oldest observation the bridge will still run an action against. The action can only be as fresh as
# S1's decision is fast. The age it measured at the bridge (observation to act) was:
# - 29 Oct reader run: median 0.41 s, 95th percentile 0.65 s;
# - Day 5 gate run-02: median 0.52 s, 95th percentile 0.75 s.
# At 0.5 s, 12% and 85% of actions were rejected, so S1 could not act (#132). Below the decision latency,
# a stale rejection buys no fresher decision. Lower this if S1 gets faster.
MAX_AGE_S = 0.8
_NORMAL_ENDS = ("finish", "wall_limit")
_RULE_ENDS = ("provider_failures", "halt")


@dataclass(frozen=True)
class SessionResult:
    status: str  # "complete" | "stopped" | "inconclusive"
    ending: str  # "finish" | "wall_limit" | "provider_failures" | "halt" | "error"
    error: str | None
    turns: int
    outcomes: dict[str, int]
    provider_failures: int
    stop: StopResult | None
    adherence: list[Violation]
    loop: TurnLoop
    pacing: PacingStats | None = None  # `None` when the session ran without a token budget
    restart_history: tuple[OperationOutcome, ...] = ()  # every selected host operation and its outcome


# A held input is continued only where its calibration measured movement over a sustained hold: a tap (a
# jump), a toggle, or an input that moved nothing would change meaning if held for seconds.
_SUSTAINED_HOLD_S = 0.2


def continuous_choice_ids(built: ManifestChoices) -> list[str]:
    """Keyboard-only, non-skill actions whose calibrated hold was sustained and moved the controlled entity."""
    ids = []
    for name, declaration in built.catalog.actions.items():
        expected = built.expected.get(name) or {}
        moved, hold = expected.get("net_displacement"), expected.get("requested_hold_s")
        if (not declaration.is_skill and declaration.inputs
                and all(binding.device == "Keyboard" for binding in declaration.inputs)
                and isinstance(moved, (int, float)) and moved > 0
                and isinstance(hold, (int, float)) and hold > _SUSTAINED_HOLD_S):
            ids.append(name)
    return ids


# The choices the turn loop executes itself, through its own effect path. An option is not one of them: the
# option executor measures and files its own outcome where it runs.
_PLAIN_KINDS = (ChoiceKind.action, ChoiceKind.skill)


def _measured(value: Any) -> list[float] | None:
    """A measured vector as floats, or `None` when it is not one (unknown is never zero)."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    out: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        out.append(number)
    return out or None


def _advance(displacement: Sequence[float], unit: Sequence[float]) -> float | None:
    """The measured displacement along a unit direction, or `None` when their lengths differ."""
    if len(displacement) != len(unit):
        return None
    return sum(a * b for a, b in zip(displacement, unit))


def calibrated_direction(primitives: Any, choice_id: str) -> Any:
    """The measured direction whose offered action is this choice, or `None` when calibration measured none.

    A choice with a calibrated direction is one the primitives can say where it moves; a skill or a chord
    (an action combining controls) usually has none, and is then only known by how far it went.
    """
    for direction in getattr(primitives, "directions", ()) or ():
        if direction.action == choice_id:
            return direction
        if any(action == choice_id for action, _control in direction.alternatives):
            return direction
    return None


def success_margin(primitives: Any, speed: float | None) -> float | None:
    """One measured short step, or the measured speed over one observation period: the executor's own margin.

    `runner.options.executor.OptionExecutor._success_margin` is the rule this mirrors, from the same two
    measured quantities; an executor is not built here, so the rule is stated once for both. `None` when
    neither quantity is measured: an unknown margin decides nothing, and is never zero.
    """
    from .options.executor import PERIOD_S, SUCCESS_MARGIN_STEPS

    step = getattr(primitives, "short_step", None)
    if isinstance(step, (int, float)) and not isinstance(step, bool) and math.isfinite(step) and step > 0:
        return float(step) * SUCCESS_MARGIN_STEPS
    if speed:
        return float(speed) * PERIOD_S
    return None


def _fell(displacement: Sequence[float] | None, primitives: Any) -> bool:
    """Whether the body dropped past the options' measured fall bound, whatever the choice was for.

    `runner.options.executor.fell_bound` is the rule an option's `fell` ending uses. A drop is measured
    without knowing a skill's purpose, so it is the one ending an uncalibrated choice can still have.
    """
    from .options.executor import UP, fell_bound

    bound = fell_bound(primitives)
    if bound is None or displacement is None or len(displacement) <= UP:
        return False
    return -displacement[UP] > bound


def _termination(executed: Executed, displacement: list[float] | None, direction: Any,
                 primitives: Any) -> str:
    """How one executed choice ended, by the rule an option's ending uses: did the body measurably get anywhere.

    A rejected input never ran, and an effect with no measured displacement says nothing about the body, so
    both are recorded as such. A drop past the measured fall bound is `fell` for any choice, as it is for an
    option. Displacement along a calibrated direction can be compared with the executor's success margin.
    Without a direction there is no effect criterion: moving far does not verify the purpose of an arbitrary
    action or skill. Its measured displacement stays in the outcome separately.
    """
    if executed.execution is None or executed.execution.rejected:
        return "rejected"
    if _fell(displacement, primitives):
        return "fell"
    if displacement is None or direction is None:
        return "unmeasured"
    margin = success_margin(primitives, direction.speed)
    if margin is None:
        return "unmeasured"
    reached = _advance(displacement, direction.unit)
    if reached is None:
        return "unmeasured"
    return "succeeded" if reached >= margin else "blocked"


def choice_outcome(choice: Choice, executed: Executed, *, primitives: Any,
                   scene: str | None) -> OptionOutcome:
    """One executed plain action or skill as the same record an option's outcome takes.

    The place, the displacement and the ending come from the effect the turn loop measured, so this memory
    covers the choices that ran and not only the options that ran. Unknown parts stay `None`: an effect that
    measured no position leaves the record unplaced rather than filed under a guessed cell, and a choice with
    no calibrated direction carries none, so its best advance stays unknown instead of invented.
    """
    from .options.model import OptionOutcome

    observed = executed.effect.observed if executed.effect is not None else None
    fields = observed if isinstance(observed, Mapping) else {}
    displacement = _measured(fields.get("displacement"))
    direction = calibrated_direction(primitives, choice.id)
    params: dict[str, Any] = {}
    if direction is not None:
        params["direction"] = [float(value) for value in direction.unit]
    duration = fields.get("applied_duration_s")
    measured_duration = (float(duration) if isinstance(duration, (int, float))
                         and not isinstance(duration, bool) and math.isfinite(duration) else None)
    return OptionOutcome(
        option_id=choice.id,
        template=choice.kind.value,  # "action" or "skill": the record says which kind of choice ran
        params=params,
        start_position=_measured(fields.get("from_position")),
        end_position=_measured(fields.get("to_position")),
        displacement=displacement,
        terminated_by=_termination(executed, displacement, direction, primitives),
        duration_s=measured_duration,
        receipts=[],
        scene=scene,
    )


def remember_executed(memory: Any, primitives: Any, report: Any) -> None:
    """File every plain action and skill one turn executed under the cell it started from.

    Options file their own outcome inside the option port, so this adds the turn loop's own executions and
    changes nothing about the option path. A run without option memory records nothing.
    """
    moved = getattr(report, "finished_move", None)  # a background action or skill collected on this turn
    sent = (*getattr(report, "executed", ()), *((moved[1],) if moved is not None else ()))
    if not sent:
        return
    scene = getattr(getattr(report, "observation", None), "scene", None)
    for executed in sent:
        choice = getattr(executed, "choice", None)
        if choice is None or choice.kind not in _PLAIN_KINDS:
            continue
        memory.record(choice_outcome(choice, executed, primitives=primitives, scene=scene))


async def run_session(
    client: BridgeClient,
    built: ManifestChoices,
    *,
    s1: S1Port,
    objective: str,
    view: ViewSource | None = None,
    probe: EffectProbe | None = None,
    help: HelpPort | None = None,
    readers: ReaderPort | None = None,
    watch: Watch | None = None,
    sink: TraceSink | None = None,
    max_age_s: float = MAX_AGE_S,
    wall_limit_s: float = WALL_LIMIT_S,
    max_provider_failures: int = MAX_PROVIDER_FAILURES,
    clock: Callable[[], float] = time.monotonic,
    budget: TokenBudget | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    facts: FactsSource | None = None,
    s1_call_cap: int | None = None,
    restart: RestartPort | None = None,
    generation: str | None = None,
    continuous_input: bool = False,
    option_primitives: Any = None,
    option_memory: Any = None,
    placement: bool = False,
    reach_regions: Any = (),
    instructions: str = INSTRUCTIONS,
    keep_help_across_restart: bool = False,
    decide_during_options: bool = False,
    press_tests: Any = (),
) -> SessionResult:
    """`budget` paces the turns; `sleep` is how the loop waits out a wait choice's duration.

    `readers` installs the manifest's readers in every observation and `watch` states what they and the
    effects measured as facts and suspicions; neither stops the session, opens help or picks anything.

    With `restart`, an S1-selected host operation runs outside decision turns under the smaller of the
    remaining wall time and the operation's own bound. Its budgets, pacing and deadlines persist; a
    non-ready outcome ends the run closed and is never retried or resumed.

    With `option_memory`, every executed plain action and skill is recorded in the same shape an option's
    outcome takes, so a place's history covers what ran there and not only the options: an action that was
    blocked eleven times stops reading `untried`. The option port records its own outcomes, unchanged.

    `instructions` is the one question text every payload carries; its default keeps today's bytes.

    `press_tests` are press-test options (`expectations.press_test_specs`) offered beside the others while their
    control is offered; like every option, only S1 selects one.
    """
    if max_provider_failures < 1:
        raise ValueError("max_provider_failures must be at least 1")
    started = clock()
    deadline = started + wall_limit_s
    resources = SessionFacts(started=started, wall_limit_s=wall_limit_s, clock=clock,
                             s1=s1, s1_call_cap=s1_call_cap, help=help, inner=facts)
    paced = PacingFacts(inner=resources) if budget is not None else None
    eligible = continuous_choice_ids(built) if continuous_input else []
    configured = with_held_choices(built, eligible) if continuous_input else built
    held_ids = [f"{name}.held" for name in eligible]
    from .point_turns import gate_choices
    option_port = None
    if option_primitives is not None:
        from .options.executor import OptionExecutor
        from .options.model import OptionOutcome, OptionSpec
        class RunnerOptionPort:
            flight: dict = {}  # measured flights shared by every option this run

            running: Any = None  # the executor of the option now running, for `interrupt`

            async def run(self, choice):
                executor = OptionExecutor(loop.client, option_primitives, flight=self.flight, readers=readers)
                self.running = executor
                try:
                    result = await executor.run(OptionSpec(choice.option_id, choice.template, choice.params,
                                                           choice.label))
                finally:
                    self.running = None
                option_memory.record(result)
                for row in result.measured.get("tried", []) if result.template == "sweep" else ():
                    start, end = row.get("start"), row.get("end")
                    moved = [b - a for a, b in zip(start, end)] if start and end else None
                    option_memory.record(OptionOutcome(row["action"], "action", {}, start, end, moved,
                                                       "unmeasured", None, scene=result.scene))
                return result.to_json()

            def interrupt(self, reason):
                if self.running is not None:
                    self.running.interrupt(reason)

            async def until_landed(self):
                if self.running is not None:
                    await self.running.until_landed()

        option_port = RunnerOptionPort()

    def filter_choices(choices, observation):
        if configured.point_fov is not None:
            choices = gate_choices(choices, observation, configured.point_fov)
        if option_primitives is not None:
            from .choices import options_choices
            choices = options_choices(list(choices), option_primitives, option_memory, observation,
                                      placement=placement, regions=reach_regions, tests=press_tests)
        return choices

    def file_settled(report):
        # Background work collected before S1 is asked: filed now, so this turn's question states its result.
        if option_memory is not None and option_primitives is not None:
            remember_executed(option_memory, option_primitives, report)
            note = getattr(s1, "note_turn", None)
            if note is not None:
                note(report, option_primitives)
    loop = TurnLoop(
        client, choices=configured.choices, max_age_s=max_age_s, leases=configured.leases,
        objective=objective, s1=s1, view=view, help=help, facts=paced if paced is not None else resources,
        effects=probe, readers=readers, watch=watch, sink=sink, sleep=sleep,
        restart=restart, generation=generation, time_left=lambda: deadline - clock(),
        continuous_input=continuous_input,
        continuous_choices=held_ids,
        continuous_labels=(lambda interval, lease: held_choice_labels(configured, held_ids, interval, lease))
        if continuous_input else None,
        choice_filter=filter_choices if configured.point_fov is not None or option_primitives is not None else None,
        option_port=option_port,
        # The same measured step the option memory files places by: a reused conditional answer is bound to it.
        contingent_cell=getattr(option_memory, "cell", None),
        instructions=instructions,
        keep_help_across_restart=keep_help_across_restart,
        decide_during_options=decide_during_options,
        on_settled=file_settled if decide_during_options else None,
    )
    bind = getattr(probe, "bind_observation_source", None)
    if bind is not None:  # the probe states the surroundings of the observation each action was bound to
        bind(lambda: loop.store.latest())
    restarts: list[OperationOutcome] = []
    failures = total_failures = 0
    waited_s = longest_s = 0.0
    turns_waited = 0
    ending: str | None = None
    error: str | None = None
    try:
        while ending is None:
            if clock() >= deadline:
                ending = "wall_limit"
                break
            if budget is not None and paced is not None:
                waited = await budget.ready()
                paced.note(waited)
                if waited > 0:
                    waited_s, longest_s, turns_waited = waited_s + waited, max(longest_s, waited), turns_waited + 1
                    if clock() >= deadline:  # the wait itself used up the time allowed
                        ending = "wall_limit"
                        break
            record = await loop.run_turn()
            report = loop.last_report
            assert report is not None
            if option_memory is not None and option_primitives is not None:
                if report.settled_before_decision:  # its finished work was filed before the decision
                    report = dataclasses.replace(report, finished_option=None, finished_move=None)
                remember_executed(option_memory, option_primitives, report)
                # The same measured verdict, offered to an adapter that states the last choices: a port
                # without the hook is unaffected, and this records nothing itself.
                note = getattr(s1, "note_turn", None)
                if note is not None:
                    note(report, option_primitives)
            failures = failures + 1 if report.provider_failed else 0
            total_failures += 1 if report.provider_failed else 0
            if record.operation is not None:
                restarts.append(record.operation)
                if (record.operation.status is OperationStatus.timeout
                        and deadline - clock() <= RESTART_WALL_SLACK_S):
                    # The loop bounds a restart by the run's remaining wall time: it ran out mid-restart.
                    ending = "wall_limit"
                    break
                if record.operation.status is not OperationStatus.ready:
                    reason = record.operation.reason or "no reason was recorded"
                    ending, error = "error", f"restart_play_mode {record.operation.status.value}: {reason}"
                    break
            if report.selected is not None and report.selected.kind is ChoiceKind.finish:
                ending = "finish"
            elif loop.halt_reason is not None:
                ending, error = "halt", loop.halt_reason
            elif failures >= max_provider_failures:
                ending, error = "provider_failures", f"{failures} consecutive provider failures"
    except Exception as exc:  # noqa: BLE001 - fail closed: record the cause, then release below
        ending, error = "error", f"{type(exc).__name__}: {exc}"
    except BaseException:
        try:
            await loop.stop("error", error="interrupted")
        finally:
            await _close_adopted(loop, client)
        raise

    stop: StopResult | None = None
    try:
        stop = await loop.stop("finish" if ending in _NORMAL_ENDS else "error", error=error)
    except Exception as exc:  # noqa: BLE001 - report an unverifiable release as such
        error = f"{error}; " if error else ""
        error += f"stopping failed: {type(exc).__name__}: {exc}"
    if facts is not None and hasattr(facts, "finish"):
        latest = loop.store.latest()
        if latest is not None:
            # The bridge may reuse a seq in a new play session; cite only this reading's own session.
            facts.finish(turn=max(0, len(loop.turns) - 1), based_on_seq=latest.seq,
                         evidence_trace_seq=(sink.trace_seq_for_observation(latest.seq, latest.session)
                                             if sink is not None and hasattr(sink, "trace_seq_for_observation")
                                             else None))
    released = stop is not None and stop.release_verified is True
    violations = check_adherence(loop.turns)
    if not released:
        error = f"{error}; " if error else ""
        error += "input release was not verified"
    if violations:
        error = f"{error}; " if error else ""
        error += f"{len(violations)} adherence violation(s)"

    if released and not violations and ending in _NORMAL_ENDS:
        status = "complete"
    elif released and not violations and ending in _RULE_ENDS:
        status = "stopped"
    else:
        status = "inconclusive"
    turns = loop.turns
    outcomes: dict[str, Any] = dict(Counter(record.outcome.value for record in turns))
    await _close_adopted(loop, client)
    return SessionResult(
        status=status, ending=ending or "error", error=error, turns=len(turns), outcomes=outcomes,
        provider_failures=total_failures, stop=stop, adherence=violations, loop=loop,
        pacing=PacingStats(waited_s, longest_s, turns_waited) if budget is not None else None,
        restart_history=tuple(restarts),
    )


async def _close_adopted(loop: TurnLoop, original: BridgeClient) -> None:
    """Close the client a restart adopted; the caller owns the one it passed in.

    The release already happened through `loop.stop`. A close failure here is cleanup noise, not evidence
    that the release failed, so it is never folded into the run's status.
    """
    adopted = loop.client
    if adopted is original:
        return
    try:
        await adopted.aclose()
    except Exception:  # noqa: BLE001 - cleanup of an already released session
        pass
