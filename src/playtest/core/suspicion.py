"""The watch: what the loop measures about the readers and effects, stated as facts and recorded as suspicions.

`ReaderWatch` reads each observation once, in turn order, and for every installed reader states a line: the
current value, the previous value and the range its earlier readings showed ("range so far", which never
includes the current reading). It raises a suspicion, which is a recorded fact and nothing more, when

- a manifest invariant does not hold for the readings (`playtest.core.evaluate`, which scores each check with the
  shared `playtest.roles.checks.evaluate`);
- a reader leaves the range its earlier readings in the session showed;
- an action's measured effect differs from the calibrated one by more than the calibration's recorded spread
  allows (the probe measures both and supplies the allowance).

A suspicion is a fact. It is written to the trace and stated to S1, and it opens no help, ranks nothing and
changes no choice (S1 ownership rules 3 and 4). Nothing here knows a game's names: readers, rules and
metrics arrive from the manifest and the probe.

An event is raised when a state begins, not on every turn it lasts. A reader that has left its range is
raised once, and is raised again only after a reading came back inside; an invariant that stays violated is
raised once and then restated as "still" while it holds. A reader's own line states its current value against
its earlier range every turn, so the departure stays visible without a new record each turn.

A range needs `min_readings` earlier known readings before leaving it is stated: fewer is a point or a pair,
not a range the session has shown. The default is fixed here, ahead of any run; it only gates a fact.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import Suspicion, SuspicionBasis
from playtest.contracts.loop import Choice
from playtest.contracts.manifest import ManifestInvariant
from playtest.roles.checks import ValueKind

from .evaluate import evaluate_invariant, is_number, same
from .ports import EffectReport, Noticed, ReaderSample

RANGE_MIN_READINGS = 10
SEEN_VALUES_CAP = 20  # more distinct values than this is a set that keeps growing, not a range
_TEXT_CHARS = 60


def fmt(value: Any) -> str:
    """A reading as a fact states it: numbers to six significant digits, `unknown` for no reading."""
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value + 0.0:.6g}"
    if isinstance(value, str):
        text = json.dumps(value, ensure_ascii=False)
        return text if len(text) <= _TEXT_CHARS else text[:_TEXT_CHARS - 1] + "…"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(fmt(item) for item in value) + "]"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= _TEXT_CHARS else text[:_TEXT_CHARS - 1] + "…"


def suspicion_line(suspicion: Suspicion, trace_seq: int | None) -> str:
    """The fact S1 reads for a recorded suspicion: its id, basis and trace entry, then the measurement."""
    where = f", trace_seq {trace_seq}" if trace_seq is not None else ""
    return f"Suspicion {suspicion.suspicion_id} ({suspicion.basis.value}{where}): {suspicion.statement}"


def shape_of(value: Any) -> str | None:
    """The comparable shape of one reading; `None` for no reading."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)) and value and all(is_number(item) for item in value):
        return "vector"
    return "other"


@dataclass
class _Track:
    """What one reader's known readings have shown so far in the session."""

    alias: str
    count: int = 0
    shape: str | None = None
    low: float | None = None
    high: float | None = None
    low_seq: int | None = None
    high_seq: int | None = None
    seen: list[Any] = field(default_factory=list)
    open_set: bool = False
    lows: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    previous: Any = None  # the previous observation's reading; None when it was unknown or there was none
    outside: bool = False  # whether the last known reading left the range of those before it

    def earlier(self) -> str:
        """The range so far, as the reader's line states it."""
        if self.count == 0:
            return "earlier readings: none"
        noun = "reading" if self.count == 1 else "readings"
        if self.shape == "number":
            span = f"earlier range {fmt(self.low)} to {fmt(self.high)}"
        elif self.shape in ("bool", "string"):
            span = ("earlier values: more than %d different ones" % SEEN_VALUES_CAP if self.open_set
                    else "earlier values: " + ", ".join(fmt(item) for item in self.seen))
        elif self.shape == "vector":
            span = "earlier range per component: [" + ", ".join(
                f"{fmt(lo)} to {fmt(hi)}" for lo, hi in zip(self.lows, self.highs)) + "]"
        else:
            span = "no range is stated for this kind of value"
        return f"{span} ({self.count} earlier {noun})"

    def departure(self, value: Any, min_readings: int) -> tuple[str, dict[str, Any]] | None:
        """How `value` leaves the range of the earlier readings, or `None` when it does not or no range exists yet."""
        shape = shape_of(value)
        if shape is None or self.count < min_readings:
            return None
        base: dict[str, Any] = {"readings": self.count, "shape": self.shape}
        if shape != self.shape:
            return (f"it is a {shape} value and its {self.count} earlier readings were {self.shape} values", base)
        if shape == "number" and (value < self.low or value > self.high):
            return (f"outside the range {fmt(self.low)} to {fmt(self.high)} that its {self.count} earlier readings "
                    f"showed", {**base, "min": self.low, "max": self.high, "min_seq": self.low_seq,
                                "max_seq": self.high_seq})
        if shape in ("bool", "string") and not self.open_set and not any(same(value, item) for item in self.seen):
            return (f"none of its {self.count} earlier readings had this value (earlier values: "
                    f"{', '.join(fmt(item) for item in self.seen)})", {**base, "values": list(self.seen)})
        if shape == "vector":
            if len(value) != len(self.lows):
                return (f"it has {len(value)} components and its {self.count} earlier readings had {len(self.lows)}", base)
            out = [i for i, (item, lo, hi) in enumerate(zip(value, self.lows, self.highs)) if item < lo or item > hi]
            if out:
                parts = ", ".join(f"component {i} is {fmt(value[i])}, outside {fmt(self.lows[i])} to {fmt(self.highs[i])}"
                                  for i in out)
                return (f"{parts}, the range its {self.count} earlier readings showed",
                        {**base, "components": out, "min": list(self.lows), "max": list(self.highs)})
        return None

    def add(self, value: Any, trace_seq: int | None) -> None:
        """Fold one known reading into the range. A reading of another shape starts the range again."""
        shape = shape_of(value)
        if shape is None:
            return
        if shape != self.shape:
            self.count, self.shape = 0, shape
            self.low = self.high = self.low_seq = self.high_seq = None
            self.seen, self.open_set, self.lows, self.highs = [], False, [], []
        if shape == "number":
            if self.low is None or value < self.low:
                self.low, self.low_seq = value, trace_seq
            if self.high is None or value > self.high:
                self.high, self.high_seq = value, trace_seq
        elif shape in ("bool", "string") and not self.open_set and not any(same(value, item) for item in self.seen):
            if len(self.seen) >= SEEN_VALUES_CAP:
                self.open_set = True
            else:
                self.seen.append(value)
        elif shape == "vector":
            if len(value) != len(self.lows):
                self.lows, self.highs = list(map(float, value)), list(map(float, value))
            else:
                self.lows = [min(lo, item) for lo, item in zip(self.lows, value)]
                self.highs = [max(hi, item) for hi, item in zip(self.highs, value)]
        self.count += 1


class ReaderWatch:
    """The `Watch` for a manifest's readers and invariants. It keeps the state; the loop only calls it.

    `kinds` maps each reader alias to its manifest kind (`None` when unknown), in the order its lines are
    stated. `invariants` are the manifest's own rules, scored as written.
    """

    def __init__(self, kinds: Mapping[str, ValueKind | None], invariants: Sequence[ManifestInvariant] = (),
                 *, min_readings: int = RANGE_MIN_READINGS) -> None:
        if isinstance(min_readings, bool) or not isinstance(min_readings, int) or min_readings < 1:
            raise ValueError("min_readings must be a positive integer")
        self._kinds = dict(kinds)
        self._invariants = list(invariants)
        self._min = min_readings
        self._tracks = {alias: _Track(alias) for alias in self._kinds}
        self._before: dict[str, Any] | None = None
        self._before_seq: int | None = None
        self._violated: dict[str, tuple[int, str]] = {}  # invariant name -> (turn it began, its suspicion id)
        self._raised = 0
        self._seq: int | None = None
        self._game_s: float | None = None

    @property
    def raised(self) -> int:
        """How many suspicions this watch has raised."""
        return self._raised

    @property
    def min_readings(self) -> int:
        return self._min

    def observe(self, observation: Observation, sample: ReaderSample | None, *, turn: int,
                trace_seq: int | None) -> Noticed:
        self._seq, self._game_s = observation.seq, observation.game_s
        now = {alias: observation.public.get(alias) for alias in self._kinds}
        lines: list[str] = []
        raised: list[Suspicion] = []
        header = self._header(observation, sample)
        if header is not None:
            lines.append(header)

        for rule in self._invariants:
            self._invariant(rule, now, turn, trace_seq, observation, lines, raised)

        for alias, track in self._tracks.items():
            value = now[alias]
            lines.append(f"public.{alias}: now {fmt(value)}; previous {fmt(track.previous)}; {track.earlier()}.")
            left = track.departure(value, self._min)
            if left is not None and not track.outside:
                raised.append(self._range_exit(track, value, left, turn, trace_seq, observation, sample))
            if shape_of(value) is not None:
                track.outside = left is not None
                track.add(value, trace_seq)
            track.previous = value
        self._before, self._before_seq = now, trace_seq
        return Noticed(lines=tuple(lines), suspicions=tuple(raised))

    def effect(self, choice: Choice, report: EffectReport, *, turn: int, trace_seqs: Sequence[int]) -> list[Suspicion]:
        comparison = report.comparison
        if comparison is None:
            return []
        numbers = (comparison.expected, comparison.observed, comparison.spread, comparison.allowed)
        if not all(is_number(value) for value in numbers):  # a number nobody could measure is unknown, not a difference
            return []
        difference = comparison.observed - comparison.expected
        if abs(difference) <= comparison.allowed:
            return []
        unit = f" {comparison.unit}" if comparison.unit else ""
        statement = (f"{choice.id}: measured {comparison.metric} {fmt(comparison.observed)}{unit} against a calibrated "
                     f"{fmt(comparison.expected)}{unit}; the difference {fmt(difference)}{unit} is more than the "
                     f"{fmt(comparison.allowed)}{unit} the calibration's recorded spread allows "
                     f"({comparison.basis}).")
        detail = {"choice_id": choice.id, "metric": comparison.metric, "unit": comparison.unit,
                  "expected": comparison.expected, "observed": comparison.observed, "difference": difference,
                  "spread": comparison.spread, "allowed": comparison.allowed, "basis": comparison.basis}
        return [self._suspicion(SuspicionBasis.effect_mismatch, turn, choice.id, statement, detail, trace_seqs)]

    # --- the pieces -----------------------------------------------------------------------------

    @staticmethod
    def _header(observation: Observation, sample: ReaderSample | None) -> str | None:
        """When the readers were read, against the observation they were installed in."""
        if sample is None:
            return None
        if sample.problem is not None:
            return f"The readers could not be read this turn ({sample.problem}); their values are unknown."
        read = "unknown" if sample.game_s is None else f"{sample.game_s:.2f} s"
        seen = "unknown" if observation.game_s is None else f"{observation.game_s:.2f} s"
        return f"The readers were read at game time {read} (inspection seq {fmt(sample.inspection_seq)}); this observation is at {seen}."

    def _invariant(self, rule: ManifestInvariant, now: dict[str, Any], turn: int, trace_seq: int | None,
                   observation: Observation, lines: list[str], raised: list[Suspicion]) -> None:
        result = evaluate_invariant(rule, now, self._before, self._kinds)
        if result.holds is True:
            self._violated.pop(rule.name, None)
            return
        if result.holds is None:
            return
        shown = "; ".join(f"{name} is {fmt(value)}" for name, value in result.readings.items())
        earlier = "; ".join(f"{name} was {fmt(value)}" for name, value in result.baselines.items())
        detail = f"{result.reason} ({shown}{'; ' + earlier + ' before' if earlier else ''})"
        if rule.name in self._violated:
            since, suspicion_id = self._violated[rule.name]
            lines.append(f"Invariant {rule.name} still does not hold (since turn {since}, suspicion {suspicion_id}): {detail}.")
            return
        evidence = [seq for seq in (self._before_seq, trace_seq) if seq is not None]
        suspicion = self._suspicion(
            SuspicionBasis.invariant, turn, rule.name, f"Invariant {rule.name} does not hold: {detail}.",
            {"invariant": rule.name, "rule": rule.kind, "readings": result.readings, "baselines": result.baselines,
             "reason": result.reason}, evidence)
        self._violated[rule.name] = (turn, suspicion.suspicion_id)
        raised.append(suspicion)

    def _range_exit(self, track: _Track, value: Any, left: tuple[str, dict[str, Any]], turn: int,
                    trace_seq: int | None, observation: Observation, sample: ReaderSample | None) -> Suspicion:
        clause, detail = left
        shape = shape_of(value)
        statement = (f"public.{track.alias} is {fmt(value)}: {clause}." if shape in ("number", "vector")
                     else f"public.{track.alias} is {fmt(value)}; {clause}.")
        evidence = sorted({seq for seq in (trace_seq, detail.get("min_seq"), detail.get("max_seq")) if isinstance(seq, int)})
        data = {"field": f"public.{track.alias}", "value": value, "previous": track.previous, **detail,
                "required_readings": self._min,
                "read_game_s": sample.game_s if sample is not None else None,
                "inspection_seq": sample.inspection_seq if sample is not None else None}
        return self._suspicion(SuspicionBasis.range_exit, turn, f"public.{track.alias}", statement, data, evidence)

    def _suspicion(self, basis: SuspicionBasis, turn: int, subject: str, statement: str, detail: dict[str, Any],
                   evidence: Sequence[int]) -> Suspicion:
        self._raised += 1
        return Suspicion(
            suspicion_id=f"s{self._raised}", basis=basis, turn=turn, based_on_seq=self._seq or 0, game_s=self._game_s,
            subject=subject, statement=statement, detail=detail,
            evidence_seq=list(dict.fromkeys(item for item in evidence if item is not None)))
