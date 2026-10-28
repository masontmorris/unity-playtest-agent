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
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from playtest.contracts.bridge import StopResult
from playtest.contracts.loop import ChoiceKind
from playtest.core.adherence import Violation, check_adherence
from playtest.core.bridge_client import BridgeClient
from playtest.core.ports import EffectProbe, HelpPort, S1Port
from playtest.core.trace_sink import TraceSink
from playtest.core.turn_loop import TurnLoop

from .choices import ManifestChoices

MAX_PROVIDER_FAILURES = 3
WALL_LIMIT_S = 600.0
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


async def run_session(
    client: BridgeClient,
    built: ManifestChoices,
    *,
    s1: S1Port,
    objective: str,
    probe: EffectProbe | None = None,
    help: HelpPort | None = None,
    sink: TraceSink | None = None,
    max_age_s: float = 0.5,
    wall_limit_s: float = WALL_LIMIT_S,
    max_provider_failures: int = MAX_PROVIDER_FAILURES,
    clock: Callable[[], float] = time.monotonic,
) -> SessionResult:
    if max_provider_failures < 1:
        raise ValueError("max_provider_failures must be at least 1")
    loop = TurnLoop(
        client, choices=built.choices, max_age_s=max_age_s, leases=built.leases,
        objective=objective, s1=s1, help=help, effects=probe, sink=sink,
    )
    deadline = clock() + wall_limit_s
    failures = total_failures = 0
    ending: str | None = None
    error: str | None = None
    try:
        while ending is None:
            if clock() >= deadline:
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
    )
