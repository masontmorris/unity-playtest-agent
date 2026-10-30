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
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from playtest.contracts.bridge import StopResult
from playtest.contracts.loop import ChoiceKind
from playtest.core.adherence import Violation, check_adherence
from playtest.core.bridge_client import BridgeClient
from playtest.core.ports import EffectProbe, FactsSource, HelpPort, ReaderPort, S1Port, Watch
from playtest.core.trace_sink import TraceSink
from playtest.core.turn_loop import TurnLoop

from .choices import ManifestChoices
from .pacing import PacingFacts, PacingStats, TokenBudget

MAX_PROVIDER_FAILURES = 3
WALL_LIMIT_S = 600.0
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


async def run_session(
    client: BridgeClient,
    built: ManifestChoices,
    *,
    s1: S1Port,
    objective: str,
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
) -> SessionResult:
    """`budget` paces the turns; `sleep` is how the loop waits out a wait choice's duration.

    `readers` installs the manifest's readers in every observation and `watch` states what they and the
    effects measured as facts and suspicions; neither stops the session, opens help or picks anything.
    """
    if max_provider_failures < 1:
        raise ValueError("max_provider_failures must be at least 1")
    paced = PacingFacts(inner=facts) if budget is not None else None
    loop = TurnLoop(
        client, choices=built.choices, max_age_s=max_age_s, leases=built.leases,
        objective=objective, s1=s1, help=help, facts=paced if paced is not None else facts,
        effects=probe, readers=readers, watch=watch, sink=sink, sleep=sleep,
    )
    deadline = clock() + wall_limit_s
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
            await loop.run_turn()
            report = loop.last_report
            assert report is not None
            failures = failures + 1 if report.provider_failed else 0
            total_failures += 1 if report.provider_failed else 0
            if report.selected is not None and report.selected.kind is ChoiceKind.finish:
                ending = "finish"
            elif loop.halt_reason is not None:
                ending, error = "halt", loop.halt_reason
            elif failures >= max_provider_failures:
                ending, error = "provider_failures", f"{failures} consecutive provider failures"
    except Exception as exc:  # noqa: BLE001 - fail closed: record the cause, then release below
        ending, error = "error", f"{type(exc).__name__}: {exc}"
    except BaseException:
        await loop.stop("error", error="interrupted")
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
            facts.finish(turn=max(0, len(loop.turns) - 1), based_on_seq=latest.seq,
                         evidence_trace_seq=(sink.trace_seq_for_observation(latest.seq)
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
    return SessionResult(
        status=status, ending=ending or "error", error=error, turns=len(turns), outcomes=outcomes,
        provider_failures=total_failures, stop=stop, adherence=violations, loop=loop,
        pacing=PacingStats(waited_s, longest_s, turns_waited) if budget is not None else None,
    )
