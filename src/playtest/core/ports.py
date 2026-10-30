"""The seams the turn loop calls through, and the plain data that crosses them.

A provider answers a context, a helper answers a request, a facts source states measurements, a probe
reports an effect, a reader port reads the game's own values and a watch states what they and the effects
measured. None of them chooses what the loop executes (S1 owns the actuator) and none of them
can open help (S1 owns escalation): the loop records what each returned and acts only on the answer S1
gave. Anything a port did not measure is `None`, never zero, success or failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import Suspicion
from playtest.contracts.loop import Choice, TurnRecord

from .execution import Execution


# --- S1 -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class S1Call:
    """One provider call. `body()` is the `s1_call` trace entry; headers and credentials are never part of it."""

    question: str | None = None
    provider: str | None = None
    model: str | None = None
    resolved_model: str | None = None
    request_sha256: str | None = None
    http_status: int | None = None
    latency_ms: float | None = None
    problem: str | None = None
    response: Any = None
    usage: Any = None

    def body(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "provider": self.provider,
            "model": self.model,
            "resolved_model": self.resolved_model,
            "request_sha256": self.request_sha256,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "problem": self.problem,
            "response": self.response,
            "usage": self.usage,
        }


@dataclass(frozen=True)
class S1Answer:
    """What a provider port returns for one turn.

    `answer` is exactly what `parse_decision` judges, or `None` when the provider gave nothing usable.
    `calls` is every provider call made for the turn, in order: a help turn may add a follow-up.
    `failure` says why there is no usable answer (an HTTP or transport error, or an answer that could
    not be parsed or named no offered choice). It is a provider failure, not an invalid choice.
    `halt` asks the harness to end the run, for example because the call budget is spent; the turn is
    still recorded as rejected.
    """

    answer: Any
    calls: tuple[S1Call, ...] = ()
    failure: str | None = None
    halt: str | None = None


@dataclass(frozen=True)
class ViewSample:
    """A measured view statement and digest; bytes are passed only to the provider port."""

    statement: str
    sha256: str | None
    status: str  # sent, missing, or stale
    image: bytes | None = None


class ViewSource(Protocol):
    def sample(self, observation: Observation) -> ViewSample:
        """Measure the latest available view against this observation."""
        ...


class S1Port(Protocol):
    async def decide(self, context: Mapping[str, Any], *, image: bytes | None = None) -> S1Answer:
        """Answer one turn from the exact context the loop built. Blocking I/O must not block the event loop."""
        ...


# --- help -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HelpRequest:
    """What S1 asked for when it chose an escalate or delegate choice."""

    kind: str
    choice_id: str
    role: str
    label: str
    reason: str | None
    turn: int
    based_on_seq: int
    context: Mapping[str, Any]


@dataclass(frozen=True)
class HelpResult:
    """A finished helper's answer and who produced it. The loop shows both to S1 with the result's age."""

    source: str | None
    result: Any


class HelpPort(Protocol):
    async def run(self, request: HelpRequest) -> HelpResult:
        """Answer one request. The loop runs it as a task and cancels it on reset, scene change or stop."""
        ...


# --- effects and facts ----------------------------------------------------------------------


@dataclass(frozen=True)
class EffectComparison:
    """One measured number against the calibrated one and the spread the calibration recorded around it.

    The probe fills it only when it measured the effect and the calibration recorded both the expected value
    and a spread; otherwise the report has none. `allowed` is how far the observed value may be from the
    expected one before the difference is more than the recorded spread accounts for, and `basis` says how it
    was derived (stated before any run).
    """

    metric: str
    unit: str | None
    expected: float
    observed: float
    spread: float
    allowed: float
    basis: str


@dataclass(frozen=True)
class EffectReport:
    """An action's expected and observed effect, plus the fact line S1 reads next turn.

    The objects are the probe's own shape; the loop records them and shows the line. Unmeasured parts are `None`.
    """

    expected: Mapping[str, Any] | None = None
    observed: Mapping[str, Any] | None = None
    fact: str | None = None
    comparison: EffectComparison | None = None


class EffectProbe(Protocol):
    async def before_turn(self, turn: int) -> None:
        """Take any reading the next measurement needs. Runs before the turn's observation, never between it and the act."""
        ...

    async def measure(self, choice: Choice, execution: Execution) -> EffectReport | None:
        """Measure one executed input right after its terminal record."""
        ...


@dataclass(frozen=True)
class Executed:
    """One input the loop sent in a turn: why, which choice, what the bridge answered, and any measured effect."""

    purpose: str
    choice: Choice
    execution: Execution
    effect: EffectReport | None = None


@dataclass(frozen=True)
class TurnReport:
    """The loop's record of one finished turn, for the facts source and for the harness that drives the loop."""

    record: TurnRecord
    observation: Observation
    selected: Choice | None
    problem: str | None
    failure: str | None
    help_event: str | None
    executed: tuple[Executed, ...] = ()
    halt: str | None = None

    @property
    def provider_failed(self) -> bool:
        return self.failure is not None


class FactsSource(Protocol):
    async def facts(self, reports: Sequence[TurnReport], observation: Observation) -> Sequence[str]:
        """Measured fact strings for the decision about `observation`. Measurements and counts, never advice."""
        ...


# --- readers and the watch -------------------------------------------------------------------


@dataclass(frozen=True)
class ReaderSample:
    """One reading of the installed readers, taken before a turn's observation.

    `values` has every installed alias: the measured value, or `None` when that read failed (`errors` says
    why, per alias). `problem` is set when no reading could be taken at all; every value is then `None`.
    A value nobody measured is `None`, never zero, false or an earlier value.
    """

    values: Mapping[str, Any]
    errors: Mapping[str, str]
    inspection_seq: int | None = None
    game_s: float | None = None
    problem: str | None = None


class ReaderPort(Protocol):
    async def read(self) -> ReaderSample:
        """Read every installed reader once. Runs before the turn's observation, never between it and the act."""
        ...


@dataclass(frozen=True)
class Noticed:
    """What the watch states about one observation: fact lines for S1, and suspicions not yet recorded.

    The lines are measurements. The loop records each suspicion as a trace entry and states it to S1 as a
    fact; neither changes what S1 may choose or opens help.
    """

    lines: tuple[str, ...] = ()
    suspicions: tuple[Suspicion, ...] = ()


class Watch(Protocol):
    def observe(self, observation: Observation, sample: ReaderSample | None, *, turn: int,
                trace_seq: int | None) -> Noticed:
        """Read one observation, once, in turn order. `trace_seq` is the observation's trace entry, if traced."""
        ...

    def effect(self, choice: Choice, report: EffectReport, *, turn: int,
               trace_seqs: Sequence[int]) -> Sequence[Suspicion]:
        """Read one measured effect. `trace_seqs` are the execution and effect entries it rests on."""
        ...
