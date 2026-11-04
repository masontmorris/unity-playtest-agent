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
MOVES_BODY = 0.05          # a verb that moves the body this far by itself is a movement, not chorded
TRIALS = 2                 # presses per state: only what changed every time counts, so a rare flip does not
IDLE_PREPASS_S = 3.0       # one idle window before any press, so readers that change on their own clock are ambient
SHARED_VERBS = 3           # an asymmetry this many verbs share in one direction is the direction's (a pickup walked into)
AIR_SPAN_S = 1.0           # how long past the rise's hold an in-air window keeps reading: a body can lift after release
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


def _move_pairs(body: Sequence[Any], world: Sequence[Any]) -> tuple[list[Any], list[list[str]]]:
    """The directions verbs are chorded with, and the measured opposite pairs among them.

    Every measured opposite pair is used: the body's own (forward and back) and the world's (a first-person body's
    strafes, a side-view body's left and right), each direction once. With no opposite pair, the first direction
    alone, as before: a verb is then measured moving, but no symmetry can be compared.
    """
    moves: list[Any] = []
    pairs: list[list[str]] = []
    for group in (body, world):
        pair = _opposite_pair(group)
        if len(pair) == 2 and not {d.action for d in pair} & {d.action for d in moves}:
            moves += pair
            pairs.append([d.action for d in pair])
    if not moves:
        moves = _opposite_pair(body or world)[:1]
    return moves, pairs


def plan_guards(catalog: Mapping[str, ActionDefinition], primitives: Any) -> dict[str, Any]:
    """The verbs, the directions they are chorded with, the rise, and the generated chords.

    Chords use every measured opposite pair of directions (`_move_pairs`). A verb is an action (not a skill) that
    calibration did not classify as a move or the rise, and whose controls are disjoint from every measured
    direction's and the rise's. Nothing is chorded without a direction.
    """
    world = list(getattr(primitives, "directions", ()) or ())
    body = list(getattr(primitives, "body_directions", ()) or ())
    moves, pairs = _move_pairs(body, world)
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
    return {"verbs": verbs, "moves": [m.action for m in moves], "pairs": pairs,
            "rise": rise if rise in catalog else None, "chords": chords}


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


def _same_state(a: tuple[dict[str, Any], Any], b: tuple[dict[str, Any], Any]) -> bool:
    (values_a, position_a), (values_b, position_b) = a, b
    if (position_a is None) != (position_b is None):
        return False
    if position_a is not None and not all(abs(x - y) <= EPS for x, y in zip(position_a, position_b)):
        return False
    return values_a.keys() == values_b.keys() and all(_same(values_a[k], values_b[k]) for k in values_a)


@dataclass
class _Window:
    """What one press measured: every key that differed from the reading before it, and how far the body went."""

    changed: dict[str, list[Any]]
    displacement: float | None
    status: str | None
    pressed_at: float | None = None   # how far the body was from its start when the second press was sent
    before: dict[str, Any] | None = None  # every key as read right before the press
    persisted: frozenset = frozenset()    # keys still changed at the reading just after the press ended
    peak: float | None = None             # the furthest the body got from its start, at any reading (a jump lands)
    left_at: float | None = None          # seconds after the press at which the body was first read off its start
    path: list | None = None              # (seconds after the press, distance from the start) at each reading


class _Meter:
    def __init__(self, client: Any, readers: Any, clock: Callable[[], float],
                 sleep: Callable[[float], Awaitable[Any]]) -> None:
        self.client, self.readers, self.clock, self.sleep = client, readers, clock, sleep
        self.acts = 0
        self.ambient: set[str] = set()   # keys that change on their own clock, which settling does not wait for

    async def snapshot(self) -> tuple[dict[str, Any], tuple[float, ...] | None, Any]:
        sample = await self.readers.read() if self.readers is not None else None
        observation = await self.client.observe_once()
        public = getattr(observation, "public", None) or {}
        values = getattr(sample, "values", {}) or {}
        return _flat(values, public), _position(public), observation

    async def settle(self) -> None:
        """Wait until the body and every reader that does not change on its own clock read still.

        A previous press can keep changing a reader after the body stops (a counter reset on landing); that change
        would otherwise land in the next press's window.
        """
        started, last, still = self.clock(), None, 0
        while self.clock() - started < SETTLE_S:
            values, position, _ = await self.snapshot()
            current = ({k: v for k, v in values.items() if k not in self.ambient}, position)
            if last is not None and _same_state(current, last):
                still += 1
                if still >= SETTLED_READINGS:
                    return
            else:
                still = 0
            last = current
            await self.sleep(0.05)

    async def window(self, action: str | None, hold_s: float, *, then: str | None = None,
                     then_after_s: float | None = None, then_hold_s: float | None = None,
                     span_s: float | None = None) -> _Window:
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

        pressed_at = None
        peak = 0.0 if start is not None else None

        def reach(now: tuple[float, ...] | None) -> None:
            nonlocal peak
            if peak is not None and now is not None and len(start) == len(now):
                peak = max(peak, math.dist(start, now))

        try:
            if action is not None:
                await press(action, observation.seq, hold_s)
            began, pressed_then = self.clock(), False
            # Readings run for `span_s` (default: the hold); the first press is held for `hold_s` only, so a body
            # that lifts after the rise is released is still read in the air.
            span = max(hold_s, span_s or 0.0)
            left_at = None
            path: list = []
            while self.clock() - began < span:
                values, now, observation = await self.snapshot()
                note(values)
                reach(now)
                elapsed = self.clock() - began
                if start is not None and now is not None and len(start) == len(now):
                    path.append((_round(elapsed), _round(math.dist(start, now))))
                if left_at is None and start is not None and now is not None and len(start) == len(now) \
                        and math.dist(start, now) > EPS:
                    left_at = elapsed
                if then is not None and not pressed_then and elapsed >= (then_after_s or 0.0):
                    await press(then, observation.seq, then_hold_s or max(hold_s - elapsed, SAMPLE_S))
                    pressed_then = True
                    if start is not None and now is not None and len(start) == len(now):
                        pressed_at = math.dist(start, now)
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
        reach(end)
        displacement = (math.dist(start, end) if start is not None and end is not None and len(start) == len(end)
                        else None)
        persisted = frozenset(k for k, v in after.items() if k in before and not _same(v, before[k]))
        return _Window(changed=changed, displacement=displacement, status=status, pressed_at=pressed_at,
                       before=before, peak=peak, left_at=left_at, path=path, persisted=persisted)


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


def _row(trials: Sequence[tuple[dict[str, list[Any]], _Window]], baseline: _Window,
         held_s: float | None = None) -> dict[str, Any]:
    """What every trial measured: the keys all of them changed, and a different motion only when each moved so."""
    keys = set.intersection(*(set(added) for added, _ in trials)) if trials else set()
    first, window = trials[0]
    moves = [_moved_differently(w, baseline) for _, w in trials]
    moved = None if any(m is None for m in moves) else all(moves)
    return {"effect": bool(keys) or bool(moved), "changed": {k: _round(first[k]) for k in sorted(keys)},
            "displacement": _round(window.displacement), "baseline_displacement": _round(baseline.displacement),
            "moved_differently": moved, "trials": len(trials),
            "held_s": _round(held_s),  # how long the press, and its baseline's, was held: displacement over it is a speed
            # What each trial changed on its own: an effect seen in some trials is intermittent, not missing.
            "trial_changed": [sorted(added) for added, _ in trials],
            # Of those, the keys still changed just after the press ended: an effect that is gone by then lasts only
            # while the input is held, and a reading taken after a press cannot show it.
            "trial_persisted": [sorted(set(added) & window.persisted) for added, window in trials],
            # What a scorer needs to tell a missing effect from one the state hides: the keys the state changes by
            # itself, each trial's readings before its press (a reader already at its limit cannot move further),
            # and the furthest the body got, against the state's own furthest.
            "baseline_changed": sorted(baseline.changed),
            "starts": [{k: _round(v) for k, v in (w.before or {}).items()} for _, w in trials],
            "peak_displacement": _round(window.peak), "baseline_peak_displacement": _round(baseline.peak),
            "pressed_at": [_round(w.pressed_at) for _, w in trials] if any(w.pressed_at is not None for _, w in trials)
            else None,
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
    meter.ambient = set(prepass.changed)
    trends = {key: _trend(values) for key, values in prepass.changed.items()}

    async def trials(action: str, hold_s: float, baseline: _Window, ambient: set[str], **press: Any) -> dict[str, Any]:
        measured = []
        for _ in range(TRIALS):
            await meter.settle()
            window = await meter.window(action, hold_s, **press)
            measured.append((_added(window, baseline, ambient, trends), window))
        return _row(measured, baseline, hold_s)

    for verb in plan["verbs"]:
        hold = catalog[verb].default_lease_s or DEFAULT_HOLD_S
        await meter.settle()
        idle = await meter.window(None, hold)
        ambient = set(idle.changed) | set(prepass.changed)
        row: dict[str, Any] = {"hold_s": _round(hold), "ambient": sorted(ambient),
                               "rest": await trials(verb, hold, idle, ambient), "moving": {}, "air": None}
        if row["rest"].get("moved_differently") is True and (row["rest"].get("displacement") or 0) > MOVES_BODY:
            # It moves the body by itself: a movement the calibration never measured, not a verb to chord.
            row["moves_body"] = True
            table[verb] = row
            continue
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
            # The baseline reads for as long as the trials do, so what the landing changes is the rise's alone.
            air_span = rise_hold + AIR_SPAN_S
            jump = await meter.window(rise, rise_hold, span_s=air_span)
            # The verb goes in at the rise alone's highest reading, the apex it measured, a little early so the
            # trial's own reading there is not missed; without one (no position), at the planned apex.
            highest = max(jump.path or (), key=lambda point: point[1], default=None)
            press_at = (max(highest[0] - SAMPLE_S, 0.0) if highest is not None and highest[1] > EPS else apex_s)
            row["air"] = await trials(rise, rise_hold, jump, ambient, then=verb, then_after_s=press_at,
                                      then_hold_s=hold, span_s=air_span)
            if row["air"] is not None:
                # How the in-air press was timed: the rise alone's readings, and when it was first read off the ground.
                row["air"]["timing"] = {"apex_s": _round(apex_s), "rise_hold_s": _round(rise_hold),
                                        "rise_left_at": _round(jump.left_at), "pressed_after_s": _round(press_at),
                                        "rise_path": jump.path}
        table[verb] = row
    return {"verbs": table, "moves": plan["moves"], "pairs": plan["pairs"], "rise": rise, "notes": notes}


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


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _components(value: Any) -> list[float | None] | None:
    if isinstance(value, (list, tuple)):
        return [_number(v) for v in value]
    number = _number(value)
    return None if number is None else [number]


ANGLES = ("camera.rotation",)  # Euler degrees: they wrap at 360, so they are compared unwrapped around one reading


def _at_limit(row: Mapping[str, Any], cell: Mapping[str, Any], key: str) -> bool:
    """Whether a trial in the cell began with `key` already at the end the verb drives it to, in every component the
    verb drives (a camera rotation follows the pitch to its clamp).

    The verb's direction is read from the cells where it changed the key (a look down raises the pitch, a crouch
    raises its blend). A trial that began at or beyond the furthest value seen that way, starts included (a second
    trial can push a view to its clamp past what the first one recorded), cannot show the press: its absence there is
    no contrast. A reader that began at its rest value, the end the verb drives it from, is not at a limit.
    """
    cells = [c for c in (row.get("rest"), *(row.get("moving") or {}).values(), row.get("air")) if c]
    changed = [[parts for raw in ((c.get("changed") or {}).get(key) or ()) if (parts := _components(raw))]
               for c in cells]
    widths = {len(parts) for series in changed for parts in series}
    if len(widths) != 1:
        return False
    width = widths.pop()
    starts_all = [parts for c in cells for start in c.get("starts") or ()
                  if (parts := _components((start or {}).get(key))) and len(parts) == width]
    starts_here = [parts for start in cell.get("starts") or ()
                   if (parts := _components((start or {}).get(key))) and len(parts) == width]
    driven = 0
    for i in range(width):
        series = [[p[i] for p in s if p[i] is not None] for s in changed]
        reference = next((values[0] for values in series if values), None)
        if reference is None:
            continue
        unwrap = ((lambda v: reference + ((v - reference + 180.0) % 360.0) - 180.0) if key in ANGLES
                  else (lambda v: v))
        series = [[unwrap(v) for v in values] for values in series]
        steps = [max(values[1:], key=lambda v: abs(v - values[0])) - values[0] for values in series if len(values) >= 2]
        direction = (sum(steps) > EPS) - (sum(steps) < -EPS)
        if not direction:
            continue
        driven += 1
        seen = [v for values in series for v in values] + [unwrap(p[i]) for p in starts_all if p[i] is not None]
        end_value = max(seen) if direction > 0 else min(seen)
        if not any((unwrap(p[i]) - end_value) * direction >= -EPS for p in starts_here if p[i] is not None):
            return False
    return driven > 0


def own_keys(manifest: Any) -> set[str] | None:
    """The keys that belong to the controlled body: readers of the entity or an object under it, the camera parts and
    the body's displacement. A reader on another object (a pickup the body walks into) is not the body's own."""
    readers = manifest.get("readers") if isinstance(manifest, Mapping) else getattr(manifest, "readers", None)
    entity = manifest.get("entity_id") if isinstance(manifest, Mapping) else getattr(manifest, "entity_id", None)
    if not readers or not entity:
        return None
    own = {"displacement", "camera.position", "camera.rotation", "camera.field_of_view"}
    for reader in readers:
        descriptor = reader.get("descriptor") if isinstance(reader, Mapping) else getattr(reader, "descriptor", None)
        alias = reader.get("alias") if isinstance(reader, Mapping) else getattr(reader, "alias", None)
        object_id = (descriptor or {}).get("object_id") if isinstance(descriptor, Mapping) else getattr(
            descriptor, "object_id", None)
        if alias and isinstance(object_id, str) and (object_id == entity or object_id.startswith(entity + ".")):
            own.add(alias)
    return own


def asymmetries(guards: Mapping[str, Any], *, dropped: list[dict[str, Any]] | None = None,
                own: set[str] | None = None) -> list[dict[str, Any]]:
    """Every verb effect seen while one move of a measured opposite pair was held and not while the other was.

    Two kinds are not the verb's and are left out (into `dropped`, with the reason, when a list is given):
    - **the direction's:** the same key asymmetric the same way for SHARED_VERBS or more verbs is what holding that
      direction does in this place (the body walks into a pickup, a timer on the far side runs), not any one verb;
    - **at a limit:** where the opposite cell began with the key already where the verb's presses end, its absence
      there shows nothing;
    - **not the body's:** with `own` (`own_keys`), a key outside it belongs to another object, which holding one
      direction or the other walks the body into or away from.
    """
    found = _raw_asymmetries(guards)
    shared: dict[tuple[str, str, str], set[str]] = {}
    for item in found:
        shared.setdefault((item["key"], item["present_while"], item["absent_while"]), set()).add(item["verb"])
    out = []
    for item in found:
        verbs = shared[(item["key"], item["present_while"], item["absent_while"])]
        row = (guards.get("verbs") or {}).get(item["verb"]) or {}
        absent = (row.get("moving") or {}).get(item["absent_while"]) or {}
        reason = ("not the body's own" if own is not None and item["key"] not in own
                  else f"the direction's: {len(verbs)} verbs share it" if len(verbs) >= SHARED_VERBS
                  else "at a limit where it was absent"
                  if item["key"] != "displacement" and _at_limit(row, absent, item["key"]) else None)
        if reason is None:
            out.append(item)
        elif dropped is not None:
            dropped.append({**item, "reason": reason})
    return out


def _raw_asymmetries(guards: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every verb effect seen while one move of a measured opposite pair was held and not while the other was.

    The moves of a pair are opposite by measurement, the pairs a game is evidently built to treat alike; rest
    against moving, ground against air, or one pair against another differ by design and are not compared. An
    asymmetry is a measured fact about this build, not a verdict: whether it is a bug is for the review and the
    frontier, and how often it recurs from a fresh start is for a tally of fresh sessions.
    """
    out: list[dict[str, Any]] = []
    moves = list(guards.get("moves") or ())
    # A table from before pairs were recorded measured one pair: its two moves.
    pairs = [list(p) for p in guards.get("pairs") or ()] or ([moves] if len(moves) == 2 else [])
    for verb, row in sorted((guards.get("verbs") or {}).items()):
        cells = row.get("moving") or {}
        for pair in pairs:
            if all(m in cells and cells[m] for m in pair):
                out += _pair_asymmetries(verb, cells, pair)
    return out


def _pair_asymmetries(verb: str, cells: Mapping[str, Any], pair: Sequence[str]) -> list[dict[str, Any]]:
    """What the verb changed while one move of an opposite pair was held and not while the other was."""
    out: list[dict[str, Any]] = []
    for present, absent in (pair, pair[::-1]):
        here, there = cells[present], cells[absent]
        for key in sorted(set(here.get("changed") or {}) - set(there.get("changed") or {})):
            out.append({"verb": verb, "key": key, "present_while": present, "absent_while": absent})
        if here.get("moved_differently") is True and there.get("moved_differently") is False:
            out.append({"verb": verb, "key": "displacement", "present_while": present, "absent_while": absent})
    return out


def asymmetry_line(item: Mapping[str, Any]) -> str:
    what = "moved the body differently" if item["key"] == "displacement" else f"changed {item['key']}"
    return (f"{item['verb']} {what} while {item['present_while']} was held, and not while "
            f"{item['absent_while']} (its measured opposite) was held")


def tally(tables: Sequence[Mapping[str, Any]], own: set[str] | None = None) -> dict[str, Any]:
    """How many guard tables, one per fresh session, showed each asymmetry: k of n."""
    counts: dict[tuple[str, str, str, str], int] = {}
    for table in tables:
        for item in asymmetries(table, own=own):
            ident = (item["verb"], item["key"], item["present_while"], item["absent_while"])
            counts[ident] = counts.get(ident, 0) + 1
    return {"n": len(tables), "asymmetries": [
        {"verb": v, "key": k, "present_while": p, "absent_while": a, "k": count}
        for (v, k, p, a), count in sorted(counts.items())]}


def compare_tallies(seeded: Mapping[str, Any], control: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each asymmetry the seeded build showed, against the control.

    Specific: in more than half the seeded sessions and never on the control, for a key the control shows
    asymmetric for no other verb. Sporadic: never on the control but in half the seeded sessions or fewer. Noisy key:
    the control shows the key asymmetric for another verb. Not specific: the control shows this asymmetry too.
    """
    control_k = {(a["verb"], a["key"], a["present_while"], a["absent_while"]): a["k"]
                 for a in control.get("asymmetries") or ()}
    # A key the control build shows asymmetric for another verb flips on its own: no asymmetry of it is specific.
    # The same verb's asymmetry on the control is not noise: mirrored, it is the contrast a reversed guard shows.
    noisy: dict[str, set[str]] = {}
    for a in control.get("asymmetries") or ():
        noisy.setdefault(a["key"], set()).add(a["verb"])
    out = []
    for item in seeded.get("asymmetries") or ():
        ident = (item["verb"], item["key"], item["present_while"], item["absent_while"])
        k_control = control_k.get(ident, 0)
        if seeded["n"] < 1 or control.get("n", 0) < 1:
            verdict = "inconclusive"
        elif k_control >= 1:
            verdict = "not_specific"
        elif noisy.get(item["key"], set()) - {item["verb"]}:
            verdict = "noisy_key"  # never this asymmetry on the control, but the key is asymmetric there for others
        elif 2 * item["k"] > seeded["n"]:
            verdict = "specific"
        else:
            verdict = "sporadic"  # never on the control, but in no more than half the seeded sessions: noise-sized
        out.append({**item, "n": seeded["n"], "control_k": k_control, "control_n": control.get("n", 0),
                    "verdict": verdict, "line": asymmetry_line(item)})
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """`tally` guard tables from fresh sessions, or `compare` a seeded tally with a control's."""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="python -m playtest.runner.guards", description=main.__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    counter = sub.add_parser("tally")
    counter.add_argument("--runs", type=Path, nargs="+", required=True, help="run folders, each with guards.json")
    counter.add_argument("--output", type=Path, required=True)
    counter.add_argument("--manifest", type=Path, help="the run's manifest: asymmetries are then judged on the "
                         "controlled body's own readers (own_keys)")
    comparer = sub.add_parser("compare")
    for name in ("seeded", "control", "output"):
        comparer.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("the output must be new")
    if args.command == "tally":
        tables = [json.loads((run / "guards.json").read_text(encoding="utf-8")) for run in args.runs]
        own = own_keys(json.loads(args.manifest.read_text(encoding="utf-8"))) if args.manifest else None
        result: Any = {**tally(tables, own), "runs": [str(run) for run in args.runs]}
    else:
        result = {"comparisons": compare_tallies(json.loads(args.seeded.read_text(encoding="utf-8")),
                                                 json.loads(args.control.read_text(encoding="utf-8")))}
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for row in result.get("comparisons") or result.get("asymmetries") or []:
        print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def unlocking_chords(guards: Mapping[str, Any], chords: Sequence[GuardChord],
                     labels: Mapping[str, str]) -> list[tuple[GuardChord, str]]:
    """The chords that measurably do what their verb alone does not, each with a label stating what that was.

    A chord is offered to play only when its measured cell changed a key the verb's rest cell did not, or moved
    the body differently from the direction alone. Anything else adds nothing the verb and the direction do not
    already offer. `labels` are the verb's and the direction's own labels; their first clause names each.
    """
    out = []
    for chord in chords:
        row = (guards.get("verbs") or {}).get(chord.verb) or {}
        cell = (row.get("moving") or {}).get(chord.move)
        if not cell:
            continue
        new = sorted(set(cell.get("changed") or {}) - set(((row.get("rest") or {}).get("changed") or {})))
        if not new and cell.get("moved_differently") is not True:
            continue
        verb = (labels.get(chord.verb) or chord.verb).split(":")[0]
        move = (labels.get(chord.move) or chord.move).split(":")[0]
        measured = ", ".join(new) + (" changed" if new else "")
        if cell.get("moved_differently") is True:
            measured += ("; " if new else "") + "the body moved differently from the direction alone"
        out.append((chord, f"{verb} while {move}: both held together; measured before play: {measured}"))
    return out
