"""Pacing: keep S1's calls under the provider's token rate limit by delaying when a turn begins.

Baseten rate-limits `mammoth-1` to 100,000 tokens per minute. An executed turn costs about 2,200 input
tokens and spends over a second holding its input, which lands near the limit; a wait, a stale rejection
or a help follow-up has no hold and pushes past it. A 429 is a provider failure (the turn is rejected and
three in a row end the run), so the runner paces itself first.

Each arm's default lives in `TOKENS_PER_MINUTE_BY_ARM`; an arm without an entry uses the shared
`TOKENS_PER_MINUTE`. An explicit `--tokens-per-minute` or `UPA_RUN_TOKENS_PER_MINUTE` overrides the selected
arm's default, and `run-session.ps1` passes a rate only when `-TokensPerMinute` is given.

`TokenBudget` remembers what each call used for the last minute. `ready()` is awaited before a turn
starts, and returns once the tokens in that window plus what the next call is expected to use fit under
the limit. It only delays: it never skips, replaces or alters a decision, it is not an escalation, it can
never open help, and nothing retries. The wait is a measured fact that S1 reads on the next context
(`PacingFacts`), stated as a measurement and never as advice.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.core.facts import RecordFacts
from playtest.core.ports import DirectiveView, FactsSource, TurnReport

TOKENS_PER_MINUTE = 85_000  # the shared fallback: Baseten's 100,000 less 15% headroom
# Per-arm operating settings. This is the one table: the CLI resolves a rate from it, an explicit flag or
# environment variable overrides it, and run-session.ps1 passes nothing unless -TokensPerMinute is given.
# d1-ultra: 80% of the minimum completed 60 s window in the 100-call paced probe, rounded down.
# This is an operating setting, not the unpublished provider limit. See docs/probes/2026-10-30-liquid-d1-ultra-rate.md.
# Meta's limit is unpublished; no 429 has been seen. mammoth-1-modal is our own one-GPU server with no
# provider limit; its own queue paces it. So are d1-2b-local, local-d1-2b and modal-d1-2b: each serves one request at a time.
TOKENS_PER_MINUTE_BY_ARM = {"liquid-d1-ultra": 280_000, "meta-muse-spark-s1": 2_000_000,
                            "mammoth-1-modal": 2_000_000, "d1-2b-local": 2_000_000,
                            "modal-d1-2b": 2_000_000, "local-d1-2b": 2_000_000}
INITIAL_ESTIMATE = 3000  # what the first call is expected to use, before any call has been counted
_EPSILON = 1e-9  # a call whose tokens leave the window within this many seconds has left it


def default_tokens_per_minute(arm: str) -> int:
    """The rate an arm is paced at when neither the flag nor the environment gives one."""
    return TOKENS_PER_MINUTE_BY_ARM.get(arm, TOKENS_PER_MINUTE)


def resolve_tokens_per_minute(arm: str, configured: int | None) -> int:
    """The configured rate when one was given, otherwise the selected arm's default."""
    return default_tokens_per_minute(arm) if configured is None else configured


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


_ANCHORS = ("end", "send")


class TokenBudget:
    """Tokens used in a trailing window, and the wait that keeps the next call under the limit.

    `record` stores a call's tokens in the clock's trailing window. The next call is expected to use what
    the most recent recorded call used, or `initial_estimate` before the first. A call whose expected tokens
    alone exceed the limit goes out once the window is empty: waiting longer could never make it fit.

    `anchor` names the moment a call's entry is stamped with: `"end"` (default) uses the clock as `record`
    runs, which is when the response arrived; `"send"` uses `record`'s clock less the `sent_ago` a caller
    measured, which is when the request was posted. A provider counts a request's input tokens when it
    receives the request, so the send stamp is the faithful one. Pacing from send also stops each call's own
    latency adding to the next wait. In the 31 October FPSKit traces, on 149 of 150 turns that waited, the
    wait ended within 50 ms of the previous reply plus its spacing. Replaying those traces with the send stamp
    predicts 35.1 → 41.8 and 31.7 → 37.1 decisions a minute; that is a prediction, not yet a measurement.
    Requests are built the same way; only the measured wait that `PacingFacts` reports gets shorter.

    With `spread`, `burst` is the share of the limit the window may hold before spacing starts: below it a
    call goes as soon as it fits, so a short session that never nears the limit never waits. `0.0` spaces
    every call, as before.
    """

    def __init__(
        self,
        tokens_per_minute: int | float,
        *,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        initial_estimate: int | float = INITIAL_ESTIMATE,
        spread: bool = False,
        burst: float = 0.0,
        anchor: str = "end",
    ) -> None:
        if isinstance(burst, bool) or not isinstance(burst, (int, float)) or not math.isfinite(burst) or not 0 <= burst <= 1:
            raise ValueError("burst must be a share of the limit from 0 to 1")
        if not isinstance(anchor, str) or anchor not in _ANCHORS:
            raise ValueError("anchor must be 'end' or 'send'")
        self._anchor = anchor
        self._spread = spread
        self._burst = float(burst)
        self._limit = _positive(tokens_per_minute, "tokens_per_minute")
        self._window_s = _positive(window_s, "window_s")
        self._initial = _positive(initial_estimate, "initial_estimate")
        self._clock = clock
        self._sleep = sleep
        self._window: deque[tuple[float, int | float]] = deque()
        self._last: int | float | None = None
        self._next_extra = 0
        self._recorded = 0
        self._estimated = 0

    @property
    def limit(self) -> int | float:
        return self._limit

    @property
    def burst(self) -> float:
        """The share of the limit the window may hold before spacing starts (0.0: every call is spaced)."""
        return self._burst

    @property
    def anchor(self) -> str:
        """Which moment a call's entry is stamped with: `"end"` (record time) or `"send"` (the post)."""
        return self._anchor

    @property
    def recorded(self) -> int:
        """How many calls have been counted."""
        return self._recorded

    @property
    def estimated(self) -> int:
        """How many of those were estimates because the provider reported no usage."""
        return self._estimated

    @property
    def in_window(self) -> int | float:
        """Tokens counted in the trailing window as of now."""
        self._expire(self._clock())
        return sum(tokens for _, tokens in self._window)

    @property
    def expected(self) -> int | float:
        """Last call plus any image reserve, or the initial estimate before any call."""
        return self._last + self._next_extra if self._last is not None else self._initial

    def record(self, tokens: int | float, *, estimated: bool = False, sent_ago: float = 0.0) -> None:
        """Count one call's tokens. An estimate counts like a measurement and is only marked.

        `sent_ago` is how many seconds before this call the request was sent. It is validated always and
        used only under the `"send"` anchor, where the entry is stamped `now - sent_ago`, never earlier than
        the newest entry already in the window: the deque stays in time order, which `_expire` relies on.
        """
        if isinstance(tokens, bool) or not isinstance(tokens, (int, float)) or not math.isfinite(tokens) or tokens < 0:
            raise ValueError("tokens must be a finite non-negative number")
        if (isinstance(sent_ago, bool) or not isinstance(sent_ago, (int, float))
                or not math.isfinite(sent_ago) or sent_ago < 0):
            raise ValueError("sent_ago must be a finite non-negative number of seconds")
        now = self._clock()
        self._expire(now)
        at = now - sent_ago if self._anchor == "send" else now
        if self._window and at < self._window[-1][0]:
            at = self._window[-1][0]
        self._window.append((at, tokens))
        self._last = tokens
        self._recorded += 1
        self._estimated += 1 if estimated else 0

    def reserve_next_image(self, tokens: int) -> None:
        """Reserve image tokens if the last counted call had no image but the next one may."""
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("image reserve must be a non-negative integer")
        self._next_extra = tokens

    async def ready(self) -> float:
        """Wait until the next call fits under the limit. Returns the seconds waited: exactly 0.0 when it already fit."""
        started = now = self._clock()
        waited = 0.0
        while (delay := self._delay(now)) > 0.0:
            await self._sleep(delay)
            now = self._clock()
            waited = max(0.0, now - started)
        return waited

    def _expire(self, now: float) -> None:
        while self._window and self._window[0][0] + self._window_s - now <= _EPSILON:
            self._window.popleft()

    def _delay(self, now: float) -> float:
        """Seconds until the next call may go; 0.0 if it may go now.

        The window rule: enough of the oldest calls have left the window for the next one to fit. With `spread`,
        also the spacing rule: the next call starts no sooner after the last counted one than its expected tokens
        take at the limit's rate. Spacing keeps the same limit but turns a burst followed by a long stall (the
        window filling, then draining) into short waits on every turn. It applies once the window and the next
        call would hold more than `burst` of the limit.

        That "last counted one" is where the anchor matters: under `"end"` it is when the entry was recorded,
        and the wait therefore begins after the previous response arrived; under `"send"` it is when the entry's
        request was posted, so the wait begins from the post and overlaps the response's own latency. Both
        measure spacing from the same kind of stamp this budget already stores; neither changes the limit.
        """
        self._expire(now)
        spacing = 0.0
        held = sum(tokens for _, tokens in self._window)
        if self._spread and self._window and held + self.expected > self._burst * self._limit:
            last_at = self._window[-1][0]
            spacing = max(0.0, last_at + self.expected / self._limit * self._window_s - now)
        excess = held + self.expected - self._limit
        if excess <= 0:
            return spacing
        leaves = now
        for at, tokens in self._window:
            leaves = at + self._window_s
            excess -= tokens
            if excess <= 0:
                break
        return max(spacing, leaves - now)


@dataclass(frozen=True)
class PacingStats:
    """What pacing cost a session: the seconds it waited before turns, the longest single wait, how many turns waited."""

    waited_s: float
    longest_wait_s: float
    turns_waited: int


def wait_fact(seconds: float) -> str:
    """The one line S1 reads after a wait: a measurement, never advice."""
    shown = f"{seconds:.2f}" if seconds >= 0.01 else f"{seconds:.4f}"
    return f"Before this observation the harness waited {shown} s to stay under the provider's token rate limit."


class PacingFacts:
    """The loop's facts, plus a line stating the wait made before this turn's observation, when there was one.

    The session notes each turn's wait before the turn runs; the line is stated once, in that turn's
    context, and a turn that did not wait carries nothing extra.
    """

    def __init__(self, inner: FactsSource | None = None) -> None:
        self._inner: FactsSource = inner if inner is not None else RecordFacts()
        self._waited_s = 0.0

    def note(self, seconds: float) -> None:
        """Record the wait made before the coming observation (0.0 for none)."""
        self._waited_s = seconds

    async def facts(self, reports: Sequence[TurnReport], observation: Observation) -> list[str]:
        lines = list(await self._inner.facts(reports, observation))
        waited, self._waited_s = self._waited_s, 0.0
        if waited > 0:
            lines.append(wait_fact(waited))
        return lines

    def directive_view(self, observation: Observation) -> DirectiveView | None:
        """The wrapped source's directives, unchanged; `None` when it tracks none."""
        view = getattr(self._inner, "directive_view", None)
        return view(observation) if view is not None else None

    def rebind(self, *, client: Any = None, observation: Observation | None = None,
               generation: str | None = None) -> None:
        """Forward a play-session boundary to the wrapped source; a source without the hook changes nothing."""
        rebind = getattr(self._inner, "rebind", None)
        if rebind is not None:
            rebind(client=client, observation=observation, generation=generation)
