"""Measured guards: what each verb does at rest, while the body moves, and in the air, measured before play.

Calibration measures every action from rest, so an action's measured effect is a function of the action alone.
A game's input is a guarded transition: the same press dashes only while a direction is held, attacks only on the
ground, or changes a reader only while it is held. Read from rest, a guard looks like a missing effect, and a
missing effect looks like a guard. This measures each verb in a few states the measured primitives can put the
body in, each against a baseline of that state without the verb, so what the verb adds is what is reported:

- **rest:** the verb alone, against an idle window of the same length (readers that change by themselves, such as
  timers, are ambient and never count as the verb's effect);
- **moving:** the verb held together with a measured direction (a generated chord), against the direction alone;
- **air:** the verb pressed while the body is airborne from the measured rise, against the rise alone.

A verb is an executable action whose controls are none of the movement's or the rise's: it is told apart by its
inputs, never by its name. Readers are sampled while the input is held and once after it ends, so an effect that
lasts only while held is seen. The table is a measurement and decides nothing: no choice is added, removed or
ranked here, and a verb with no effect in a state is reported as that, whether it is a rule of the game or a bug.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from playtest.contracts.action_catalog import MAX_INPUTS, NAME, ActionDefinition, InputBinding

PREFIX = "g_"
MAX_AGE_S = 1.0
PROBE_ID = "guards"
SAMPLE_S = 0.08            # how often readers are read while an input is held
AFTER_S = 0.15             # one reading this long after the input ends
SETTLE_S = 1.5             # longest wait for the body to come to rest between probes
SETTLED_READINGS = 3
DISPLACEMENT_RATIO = 0.25  # a chord moves differently from its direction alone past this share
EPS = 1e-3
DEFAULT_HOLD_S = 0.3
TRIALS = 2                 # presses per state: only what changed every time counts, so a rare flip does not
IDLE_PREPASS_S = 3.0       # one idle window before any press, so readers that change on their own clock are ambient
_NAME = re.compile(NAME)


@dataclass(frozen=True)
class GuardChord:
    name: str            # the generated catalog name
    move: str            # the direction's action
    verb: str            # the verb's action
    definition: ActionDefinition


def chord_name(move: str, verb: str) -> str:
    """A catalog-valid, deterministic name for the chord of `move` and `verb`."""
    name = f"{PREFIX}{move}_{verb}"
    if _NAME.fullmatch(name):
        return name
    return PREFIX + hashlib.sha1(f"{move}\0{verb}".encode()).hexdigest()[:12]


def _controls(definition: ActionDefinition) -> set[tuple[str, str]]:
    return {(item.device, item.control) for item in definition.inputs or ()}


def _opposite_pair(directions: Sequence[Any]) -> list[Any]:
    """The first measured direction and the one measured opposite it, when there is one."""
    if not directions:
        return []
    first = directions[0]
    for other in directions[1:]:
        dot = sum(a * b for a, b in zip(first.unit, other.unit))
        if dot < -0.9:
            return [first, other]
    return [first]


def plan_guards(catalog: Mapping[str, ActionDefinition], primitives: Any) -> dict[str, Any]:
    """The verbs, the two directions they are chorded with, the rise, and the generated chords.

    Chords use the entity-relative directions when there are any (the body's own forward and back), else the world
    ones. A verb is an action (not a skill) that calibration did not classify as a move or the rise, and whose
    controls are disjoint from every measured direction's and the rise's. Nothing is chorded without a direction.
    """
    world = list(getattr(primitives, "directions", ()) or ())
    body = list(getattr(primitives, "body_directions", ()) or ())
    moves = _opposite_pair(body or world)
    rise = getattr(getattr(primitives, "rise", None), "action", None)
    moving = {name for d in [*world, *body] for name in [d.action, *(a for a, _ in d.alternatives)]}
    moving |= {p.action for p in getattr(primitives, "classified", ()) or () if getattr(p, "kind", None) in ("move", "rise")}
    reserved: set[tuple[str, str]] = set()
    for name in [*moving, *( [rise] if rise else [])]:
        if name in catalog and catalog[name].inputs:
            reserved |= _controls(catalog[name])
    verbs = [name for name, definition in catalog.items()
             if definition.inputs and name not in moving and name != rise and not name.startswith(PREFIX)
             and not (_controls(definition) & reserved)]
    chords: list[GuardChord] = []
    for verb in verbs:
        for move in moves:
            if move.action not in catalog or not catalog[move.action].inputs:
                continue
            inputs = [*catalog[move.action].inputs, *catalog[verb].inputs]
            if len(inputs) > MAX_INPUTS:
                continue
            hold = max(catalog[verb].default_lease_s or DEFAULT_HOLD_S, catalog[move.action].default_lease_s or 0)
            chords.append(GuardChord(chord_name(move.action, verb), move.action, verb,
                                     ActionDefinition(backend="input_system",
                                                      inputs=[InputBinding.model_validate(i.model_dump()) for i in inputs],
                                                      default_lease_s=hold)))
    return {"verbs": verbs, "moves": [m.action for m in moves], "rise": rise if rise in catalog else None,
            "chords": chords}


def _flat(values: Mapping[str, Any], public: Mapping[str, Any]) -> dict[str, Any]:
    """Readers, plus the camera parts and the body position the observation carries."""
    out = dict(values)
    camera = public.get("camera") if isinstance(public.get("camera"), Mapping) else {}
    for part in ("position", "rotation", "field_of_view"):
        if part in camera:
            out[f"camera.{part}"] = camera[part]
    return out


def _position(public: Mapping[str, Any]) -> tuple[float, ...] | None:
    controlled = public.get("controlled") if isinstance(public.get("controlled"), Mapping) else {}
    value = controlled.get("position")
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-3, abs_tol=1e-3)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) == len(b):
        return all(_same(x, y) for x, y in zip(a, b))
    return a == b


@dataclass
class _Window:
    """What one press measured: every key that differed from the reading before it, and how far the body went."""

    changed: dict[str, list[Any]]
    displacement: float | None
    status: str | None


class _Meter:
    def __init__(self, client: Any, readers: Any, clock: Callable[[], float],
                 sleep: Callable[[float], Awaitable[Any]]) -> None:
        self.client, self.readers, self.clock, self.sleep = client, readers, clock, sleep
        self.acts = 0

    async def snapshot(self) -> tuple[dict[str, Any], tuple[float, ...] | None, Any]:
        sample = await self.readers.read() if self.readers is not None else None
        observation = await self.client.observe_once()
        public = getattr(observation, "public", None) or {}
        values = getattr(sample, "values", {}) or {}
        return _flat(values, public), _position(public), observation

    async def settle(self) -> None:
        started, last, still = self.clock(), None, 0
        while self.clock() - started < SETTLE_S:
            observation = await self.client.observe_once()
            position = _position(getattr(observation, "public", None) or {})
            if position is not None and last is not None and all(abs(a - b) <= EPS for a, b in zip(position, last)):
                still += 1
                if still >= SETTLED_READINGS:
                    return
            else:
                still = 0
            last = position
            await self.sleep(0.05)

    async def window(self, action: str | None, hold_s: float, *, then: str | None = None,
                     then_after_s: float | None = None) -> _Window:
        """Press `action` (None: no input) for `hold_s`; optionally press `then` `then_after_s` into it.

        Every key that read differently from the snapshot before the press, at any reading while held or just after,
        is changed; its values are [before, every distinct later value].
        """
        before, start, observation = await self.snapshot()
        started: list[str] = []
        status = None
        changed: dict[str, list[Any]] = {}

        def note(values: Mapping[str, Any]) -> None:
            for key, value in values.items():
                if key in before and not _same(value, before[key]):
                    seen = changed.setdefault(key, [before[key]])
                    if not any(_same(value, v) for v in seen[1:]):
                        seen.append(value)

        async def press(name: str, seq: int, lease: float) -> None:
            self.acts += 1
            action_id = f"{PROBE_ID}.{self.acts}"
            started.append(action_id)
            await self.client.start_act(action_id=action_id, choice_id=PROBE_ID, action=name, params={},
                                        based_on_seq=seq, max_age_s=MAX_AGE_S, lease_s=lease)

        try:
            if action is not None:
                await press(action, observation.seq, hold_s)
            began, pressed_then = self.clock(), False
            while self.clock() - began < hold_s:
                values, _, observation = await self.snapshot()
                note(values)
                if then is not None and not pressed_then and self.clock() - began >= (then_after_s or 0.0):
                    await press(then, observation.seq, max(hold_s - (self.clock() - began), SAMPLE_S))
                    pressed_then = True
                await self.sleep(SAMPLE_S)
        finally:
            if started:
                await self.client.stop("release")
            for action_id in started:
                try:
                    records = await self.client.collect_act(action_id)
                    status = getattr(getattr(records[-1], "status", None), "value", None)
                except Exception:  # noqa: BLE001 - a receipt that never came is not a measurement
                    status = "unrecorded"
        await self.sleep(AFTER_S)
        after, end, _ = await self.snapshot()
        note(after)
        displacement = (math.dist(start, end) if start is not None and end is not None and len(start) == len(end)
                        else None)
        return _Window(changed=changed, displacement=displacement, status=status)


def _trend(values: Sequence[Any]) -> int:
    """+1 or -1 when a numeric key only moved one way in a window (a timer counting), else 0."""
    numbers = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(numbers) < 2 or len(numbers) != len(values):
        return 0
    steps = [b - a for a, b in zip(numbers, numbers[1:])]
    if all(step > EPS for step in steps):
        return 1
    if all(step < -EPS for step in steps):
        return -1
    return 0


def _against(values: Sequence[Any], trend: int) -> bool:
    """Whether a key moved against `trend` at any step (a counting timer reset)."""
    numbers = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return trend != 0 and any((b - a) * trend < -EPS for a, b in zip(numbers, numbers[1:]))


def _added(window: _Window, baseline: _Window, ambient: set[str],
           trends: Mapping[str, int] | None = None) -> dict[str, list[Any]]:
    """Keys the press changed that neither its state's baseline nor the idle windows changed at all.

    A key that also changes in the baseline (a position while moving) is the state's, not the verb's, however its
    values differ: how far the body went is compared separately, by displacement. An ambient key that only counts
    one way while idle (a timer) still counts when the press moves it the other way: that is a reset, not its clock.
    """
    trends = trends or {}
    return {key: values for key, values in window.changed.items()
            if (key not in ambient and key not in baseline.changed) or _against(values, trends.get(key, 0))}


def _moved_differently(window: _Window, baseline: _Window) -> bool | None:
    if window.displacement is None or baseline.displacement is None:
        return None
    if baseline.displacement <= EPS:
        return window.displacement > EPS
    return abs(window.displacement - baseline.displacement) > DISPLACEMENT_RATIO * baseline.displacement


def _row(trials: Sequence[tuple[dict[str, list[Any]], _Window]], baseline: _Window) -> dict[str, Any]:
    """What every trial measured: the keys all of them changed, and a different motion only when each moved so."""
    keys = set.intersection(*(set(added) for added, _ in trials)) if trials else set()
    first, window = trials[0]
    moves = [_moved_differently(w, baseline) for _, w in trials]
    moved = None if any(m is None for m in moves) else all(moves)
    return {"effect": bool(keys) or bool(moved), "changed": {k: _round(first[k]) for k in sorted(keys)},
            "displacement": _round(window.displacement), "baseline_displacement": _round(baseline.displacement),
            "moved_differently": moved, "trials": len(trials),
            "status": ",".join(sorted({str(w.status) for _, w in trials}))}


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, (list, tuple)):
        return [_round(v) for v in value]
    return value


async def measure_guards(client: Any, readers: Any, catalog: Mapping[str, ActionDefinition], primitives: Any, *,
                         clock: Callable[[], float] = time.monotonic,
                         sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> dict[str, Any]:
    """Each verb at rest, while moving along each planned direction, and in the air, each against its baseline."""
    plan = plan_guards(catalog, primitives)
    meter = _Meter(client, readers, clock, sleep)
    table: dict[str, Any] = {}
    notes: list[str] = []
    rise, rise_hold = plan["rise"], getattr(primitives, "rise_duration_s", None) or DEFAULT_HOLD_S
    apex_s = (getattr(primitives, "air_press_s", None) or rise_hold / 2.0)
    by_verb: dict[str, list[GuardChord]] = {}
    for chord in plan["chords"]:
        by_verb.setdefault(chord.verb, []).append(chord)
    served = set(catalog)
    await meter.settle()
    prepass = await meter.window(None, IDLE_PREPASS_S)
    trends = {key: _trend(values) for key, values in prepass.changed.items()}

    async def trials(action: str, hold_s: float, baseline: _Window, ambient: set[str], **press: Any) -> dict[str, Any]:
        measured = []
        for _ in range(TRIALS):
            await meter.settle()
            window = await meter.window(action, hold_s, **press)
            measured.append((_added(window, baseline, ambient, trends), window))
        return _row(measured, baseline)

    for verb in plan["verbs"]:
        hold = catalog[verb].default_lease_s or DEFAULT_HOLD_S
        await meter.settle()
        idle = await meter.window(None, hold)
        ambient = set(idle.changed) | set(prepass.changed)
        row: dict[str, Any] = {"hold_s": _round(hold), "ambient": sorted(ambient),
                               "rest": await trials(verb, hold, idle, ambient), "moving": {}, "air": None}
        for chord in by_verb.get(verb, []):
            if chord.name not in served:
                notes.append(f"chord {chord.name} is not in the served catalog; {verb} was not measured moving")
                continue
            chord_hold = chord.definition.default_lease_s or hold
            await meter.settle()
            baseline = await meter.window(chord.move, chord_hold)
            row["moving"][chord.move] = await trials(chord.name, chord_hold, baseline, ambient)
        if rise is not None:
            await meter.settle()
            jump = await meter.window(rise, rise_hold)
            row["air"] = await trials(rise, rise_hold, jump, ambient, then=verb, then_after_s=apex_s)
        table[verb] = row
    return {"verbs": table, "moves": plan["moves"], "rise": rise, "notes": notes}


def summary_lines(guards: Mapping[str, Any]) -> list[str]:
    """One line per verb: in which measured states it had an effect, and what it changed there."""
    lines = []
    for verb, row in (guards.get("verbs") or {}).items():
        parts = []
        for state, cell in [("at rest", row.get("rest")), *((f"while {m} held", c) for m, c in
                                                            (row.get("moving") or {}).items()),
                            ("in the air", row.get("air"))]:
            if not cell:
                continue
            what = ", ".join(cell["changed"]) or ("movement" if cell.get("moved_differently") else "")
            parts.append(f"{state}: {what if cell['effect'] else 'no effect'}")
        lines.append(f"{verb}: " + "; ".join(parts))
    return lines
