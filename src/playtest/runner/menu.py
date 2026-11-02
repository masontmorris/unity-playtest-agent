"""The compact menu S1 chooses from: one uniformly shaped label per choice, and measured progress.

S1 scores the offered labels against the objective and takes the top one, so a label is only useful when it
can be compared with the others. This module puts everything comparable in the same three fields:

    <intent> · <expected> · here: <measured history>

and replaces the observation, a UI text dump and recent-history block with one line of measured numbers.
Every number is measured or declared elsewhere: an option's expected quantity is the measured expectation
its spec already states, an action's effect is the calibrated net displacement its manifest label already
carries, and the history is `OutcomeMemory.suffix`. Nothing here ranks, filters or recommends a choice, and
nothing here reads a game, object, control or genre name.

`progress_line` states, along each world axis that measured variation, the furthest point reached in either
direction, the current position and, when known, whether the entity is on its floor; a probe reading adds
where an obstruction or the floor's edge is. An axis nobody measured says unmeasured, never zero.

`compact_menu` returns the state payload: objective, progress, the open directive and any pending help, the
facts the caller chose to pass, the same game-view note the full layout uses, the labelled choices, and, when
the caller passes any, `recent`: the last six choices with the duration each measurably took and the result it
measurably had, plus any measured displacement, endpoint and execution status. An unmeasured goal stays null.
No motion magnitude stands in for an uncalibrated skill's purpose. A result nobody measured stays `unmeasured`. It
carries `set_version` and `built_from_seq` so a replay can bind it to the choice set and observation it came
from. It drops the UI dump, per-choice calibration arithmetic and the full layout's recent-history block by
design.

`menu_request` is the request body in the shape the provider has always been sent, with the three contingent
questions S1 may answer ahead of time: what next, and what next if the chosen option ends blocked or ends
succeeded. With a frame it attaches `images[0]` through `playtest.runner.image_transport`, exactly as the
adapter does, beside the payload's own game-view note.

Wiring is opt-in (`--s1-context compact-menu`, `UPA_RUN_S1_CONTEXT`). It needs the run's measured positions
(the adapter accumulates them per context and clears them at a reset), the option memory of the options layer
(so `--options` is required, for the measured cell), and S1's own answer about input while help is pending
(the adapter adds `while_pending` to the same request on an escalate turn). The default mode is untouched.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from playtest.contracts.loop import ChoiceKind
from playtest.runner.image_transport import attach_images
from playtest.runner.options.memory import MINUS, UNTRIED, OutcomeMemory

# The one question text the compact menu sends. It is short because the labels carry the state; it is stated
# once, in the question, because the payload no longer carries an `instructions` copy.
MENU_INSTRUCTIONS = (
    "Choose exactly one id from choices: the one that best advances the objective now or resolves an "
    "uncertainty blocking it. Facts are measured; null means unknown, never zero. `recent` lists the last "
    "choices and what each measurably did. Input completion and displacement do not establish objective "
    "completion; goal_outcome null means unmeasured."
)
IF_BLOCKED = ("For a choice with kind=option only: if it ends blocked, what next? "
              "This branch is unused for action and skill choices.")
IF_SUCCEEDED = ("For a choice with kind=option only: if it ends succeeded, what next? "
                "This branch is unused for action and skill choices.")
PRIMARY = "next_action"
BLOCKED_QUESTION = "if_blocked"
SUCCEEDED_QUESTION = "if_succeeded"

DEAD_END_TRIED = 3  # choices tried at a place, all failed, before its untried labels say so
LABEL_LIMIT = 90  # past this a label stops being comparable at a glance; a longer history is clipped and marked
RECENT_LIMIT = 6  # measured choices the payload states: the newest six, newest last
UNMEASURED = "unmeasured"  # a result nobody measured, never a claimed success or failure
SEPARATOR = " \u00b7 "  # the middle dot between a label's three fields
AXES = ("x", "y", "z")
GROUND_AXES = (0, 2)  # the plane a move travels in; the up axis is never a direction label
GROUNDED_FRACTION = 0.05  # a twentieth of one measured step: floor at the anchor's own origin
LIQUID_ARM = "liquid-d1-ultra"  # the arm whose verified image transport is a plain `images` array

_EFFECT = re.compile(r"calibrated net displacement\s+(-?\d+(?:\.\d+)?)")
_NO_EFFECT = re.compile(r"calibrated effect unknown|no calibrated effect")


def progress_line(*, positions: Sequence[Sequence[float]], now: Sequence[float] | None,
                  start: Sequence[float] | None, grounded: bool | None, probe: Mapping | None,
                  map_line: str | None = None) -> str:
    """One line of measured numbers: furthest reached along each varied axis, where the entity is now.

    `positions` are every measured `public.controlled.position` so far, `start` the first one; `now` is this
    turn's position and may repeat the last. An axis is computed only from finite vectors; an unmeasured
    reading is left out rather than counted as zero, and no position at all says `unmeasured`.
    `map_line`, when the caller has one, is the memory's felt-map reading for this facing, appended as it is.
    """
    measured = [value for value in map(_vector, positions) if value is not None]
    current = _vector(now)
    if current is not None:
        measured.append(current)
    reached: list[str] = []
    where: list[str] = []
    if measured:
        length = min(len(value) for value in measured)
        first = _vector(start) or measured[0]
        reached.append(f"furthest +x {_n(_max_at(measured, 0))} (start {_n(first[0])})")
        reached.append(f"furthest \u2212x {_n(_min_at(measured, 0))}")
        if length > 2 and _varies(measured, 2):
            reached.append(f"furthest +z {_n(_max_at(measured, 2))}")
            reached.append(f"furthest \u2212z {_n(_min_at(measured, 2))}")
        if current is None:
            where.append("now unmeasured")
        else:
            shown = [index for index in range(min(length, len(current)))
                     if index == 0 or any(value[index] != 0 for value in measured)]
            where.append("now (" + ", ".join(_n(current[index]) for index in shown) + ")")
    else:
        reached.append("unmeasured")
    if grounded is not None:
        where.append("grounded" if grounded else "not grounded")
    line = f"progress: {', '.join(reached)}" + (f"; {' '.join(where)}" if where else "")
    summary = _probe_summary(probe)
    if summary is not None:
        line += f"; {summary}"
    if map_line is not None:
        line += f" \u00b7 {map_line}"
    return line


def option_label(choice, memory: OutcomeMemory, scene: str | None,
                 position: Sequence[float] | None) -> str:
    """One uniform label for any choice: what it is, what it is expected to do, what it did here.

    An option's intent is its spec's first clause and its expectation the measured numbers that spec states;
    a plain action's or skill's intent is its manifest label's first clause, which says what it does, and its
    expectation the one calibrated number that label carries, with the sample variance and trial count
    stripped. Wait, finish, escalate, delegate and restart are fixed short labels. Deterministic for the same
    choice, memory and position.
    """
    kind = _kind(choice)
    if kind is ChoiceKind.option:
        return _fit(_option_intent(choice), _option_expected(choice),
                    _suffix(memory, scene, position, choice.option_id or choice.id))
    if kind in (ChoiceKind.action, ChoiceKind.skill):
        name = (getattr(choice, "action", None) if kind is ChoiceKind.action
                else getattr(choice, "skill", None)) or choice.id
        label = getattr(choice, "label", "") or ""
        # The manifest label's first clause says what the action does ("move right on stick", "jump"); the
        # tuned action or skill name says only which input it is. Keeping the clause is what makes a plain
        # action comparable with an option at a pit edge, so it is the intent, not the identifier.
        intent = _first_clause(label) or str(name)
        return _fit(intent, _effect(label), _suffix(memory, scene, position, choice.id))
    fixed = _fixed(kind, choice)
    if fixed is not None:
        return fixed
    return _fit(_first_clause(getattr(choice, "label", "") or ""), "", "")


def measured_position(observation) -> tuple[float, ...] | None:
    """The controlled entity's measured position, or `None` when this observation carries none.

    One reader of `public.controlled.position`, shared by the payload and by the adapter that keeps the
    run's position history, so both mean the same measurement by the same rules.
    """
    public = _field(observation, "public")
    controlled = public.get("controlled") if isinstance(public, Mapping) else None
    return _vector(controlled.get("position")) if isinstance(controlled, Mapping) else None


def _facing(positions: Sequence[Sequence[float]], now: Sequence[float] | None) -> tuple[float, ...] | None:
    """The direction the entity last measurably moved: this turn's position less the one before it.

    A turn that did not move has no facing, and neither does a run with no earlier measurement: the felt map
    is then not walked, rather than walked along an axis the run never measured.
    """
    current = _vector(now)
    history = [value for value in map(_vector, positions) if value is not None]
    if current is None or not history:
        return None
    previous = history[-1]
    length = min(len(current), len(previous))
    delta = tuple(current[index] - previous[index] for index in range(length))
    return delta if any(delta) else None


def compact_menu(*, objective: str, observation, choices, memory: OutcomeMemory,
                 directive=None, pending_help: Mapping | None = None, facts: Sequence[str] = (),
                 game_view: str | None = None, positions: Sequence[Sequence[float]] = (),
                 recent: Sequence[Mapping] = (), help_record: tuple[int, int] | None = None,
                 running: Mapping | None = None, start_scene: str | None = None) -> dict:
    """The S1 state payload: what the objective is, how far the run got, and the labelled choices.

    `observation` is an `Observation` or its JSON mapping; it is read for the controlled pose, the scene, the
    probe and the sequence number, and it is handed to the memory so settled points keep being detected.
    `directive` is the open advice or None; `pending_help` the open request or None. Facts are only the lines
    the caller passes, and `game_view` is the note the full layout sends. `recent` is the measured choices the
    caller has already taken, oldest first; its newest six are carried as `id`, `s` and `result`, and when it
    is empty the payload has no such key at all, so its bytes are exactly what they were before. No UI dump,
    no per-choice calibration arithmetic and no recent-history block is included.
    """
    memory.observe(_Seen(observation))
    scene = _field(observation, "scene")
    public = _field(observation, "public")
    public = public if isinstance(public, Mapping) else {}
    position = measured_position(observation)
    probe = public.get("probe")
    measured = [value for value in map(_vector, positions) if value is not None]
    facing = _facing(measured, position)
    offered = list(getattr(choices, "choices", choices))
    seq = _field(observation, "seq")
    built_from = getattr(choices, "built_from_seq", seq)
    if isinstance(built_from, int) and isinstance(seq, int) and built_from != seq:
        raise ValueError(f"choices are bound to seq {built_from}, not the observation's {seq}")
    payload = {
        "objective": objective,
        "progress": progress_line(positions=measured, now=position,
                                  start=measured[0] if measured else None,
                                  grounded=_grounded(probe, memory.cell), probe=probe,
                                  map_line=memory.map_line(scene, None if position is None else list(position),
                                                           facing)),
        "directive": _directive_view(directive),
        "pending_help": dict(pending_help) if pending_help is not None else None,
        "facts": list(facts),
        "game_view": game_view,
        "set_version": int(getattr(choices, "set_version", 0) or 0),
        "built_from_seq": int(built_from if isinstance(built_from, int) else 0),
        "choices": [_choice_view(choice, memory, scene, position) for choice in offered],
    }
    if isinstance(running, Mapping) and isinstance(running.get("running_s"), (int, float)):
        # What is running is a state fact, not a label: a label that advertised it became the best match to re-pick.
        payload["running"] = {"id": running.get("choice_id"), "s": round(float(running["running_s"]), 1),
                              "note": "choosing it again keeps it going; another choice stops it first"}
    summary = memory.place_summary(scene, position) if hasattr(memory, "place_summary") else None
    tried = _onward_failures(summary, offered, measured, position)
    if tried is not None and tried >= DEAD_END_TRIED:
        # A place where nothing tried got on: each untried label says so, measured, since what was never tried
        # here is the one thing this place has not ruled out. Nothing is ranked or hidden.
        for row in payload["choices"]:
            if row["label"].endswith(UNTRIED):
                row["label"] = f"{row['label']}{SEPARATOR}{tried} others tried here, none got on"
    if start_scene is not None and scene == start_scene:
        # What finishing would end, measured: the run is still in the scene it began in. A completed scene loads
        # the next one, so an unchanged scene is the one completion fact the run measures.
        for row in payload["choices"]:
            if row["kind"] == ChoiceKind.finish.value:
                row["label"] = f"{row['label']}{SEPARATOR}scene unchanged since the run began"
    if help_record is not None and help_record[0]:
        # The escalate label carries its own history, as every other label does: the requests opened this
        # run and the directives that reached S1. Nothing here opens, repeats or refuses a request.
        asked, advised = help_record
        tail = f"so far: {asked} asked, {advised} gave advice"
        if pending_help is not None:
            tail += "; one pending"
        for row in payload["choices"]:
            if row["kind"] == ChoiceKind.escalate.value:
                row["label"] = f"{row['label']}{SEPARATOR}{tail}"
    if payload["directive"] is not None:
        # A restart invalidates the open directive (`DirectiveTracker.rebind`): the restart label states that
        # consequence, as every other label states what its choice measurably does.
        for row in payload["choices"]:
            if row["kind"] == ChoiceKind.restart_play_mode.value:
                row["label"] = f"{row['label']}{SEPARATOR}drops the open advice"
    rows = recent_rows(recent)
    if rows:  # only a run that measured choices grows the state; a payload without them keeps its bytes
        payload["recent"] = rows
    return payload


def _onward_failures(summary: Mapping[str, int] | None, offered, positions, position) -> int | None:
    """How many choices were tried here with none getting on, or `None` when one did or nothing says.

    Getting on is any `succeeded` ending except one along a direction back toward where the run started: going
    back the way the run came is always possible and rules nothing out ahead. The way back is measured, from the
    run's first position to this one; with no such measurement every success counts.
    """
    if not summary:
        return None
    now, first = _vector(position), (_vector(positions[0]) if positions else None)
    back = None
    if now is not None and first is not None:
        back = [a - b for a, b in zip(first, now)]
    directions = {getattr(choice, "option_id", None) or choice.id: _vector((getattr(choice, "params", None) or {})
                                                                            .get("direction"))
                  for choice in offered}
    for option_id, succeeded in summary.items():
        if not succeeded:
            continue
        direction = directions.get(option_id)
        if back is None or direction is None or sum(a * b for a, b in zip(direction, back)) <= 0:
            return None
    return len(summary)


def _choice_view(choice, memory: OutcomeMemory, scene, position) -> dict:
    """Short scoring label plus the scope and history that shortening must not erase."""
    kind = _kind(choice)
    label = option_label(choice, memory, scene, position)
    row = {"id": choice.id, "kind": kind.value if kind is not None else None, "label": label}
    # Each row goes out on every turn, 30 or so per request, so nothing the label already says is repeated: the
    # history only when the label does not carry all of it, the full text only when the label was clipped or is a
    # finish guard the fixed label cannot state. That outcomes are not objective completion is said once, in the
    # question (`MENU_INSTRUCTIONS`).
    if kind in (ChoiceKind.option, ChoiceKind.action, ChoiceKind.skill):
        history = _suffix(memory, scene, position, choice.option_id or choice.id)
        if history not in label:
            row["history"] = history
    full = getattr(choice, "label", "") or ""
    if full and (kind is ChoiceKind.finish or "\u2026" in label):
        row["details"] = full
    return row


def recent_rows(recent: Sequence[Mapping]) -> list[dict]:
    """The newest `RECENT_LIMIT` measured choices, with known motion and execution facts beside the result.

    `s` is the measured duration in seconds, or `null` when nothing measured one; `result` is the measured
    outcome the caller recorded, and `unmeasured` when it recorded none. A skill's input completion is not
    objective success. Optional vectors and execution status stay distinct from its unmeasured goal. Nothing is inferred, ranked or
    shortened: an entry with no id says nothing and is left out rather than named.
    """
    rows: list[dict] = []
    for item in list(recent)[-RECENT_LIMIT:]:
        ident = _field(item, "id")
        if not isinstance(ident, str) or not ident:
            continue
        result = _field(item, "result")
        row = {"id": ident, "s": _num(_field(item, "s")),
               "result": result if isinstance(result, str) and result else UNMEASURED}
        for key in ("displacement", "end_position"):
            measured = _vector(_field(item, key))
            if measured is not None:
                row[key] = list(measured)
        status = _field(item, "execution_status")
        if isinstance(status, str):
            row["execution_status"] = status
        if "goal_outcome" in item:
            row["goal_outcome"] = None  # no objective outcome is measured by these receipts
        rows.append(row)
    return rows


def menu_request(payload: dict, model: str, *, contingent: bool, image: bytes | None = None,
                 arm: str = LIQUID_ARM) -> dict:
    """The request body: `next_action` over the payload's labels, and the two conditional questions.

    With `contingent`, `if_blocked` and `if_succeeded` ask the same choice set in advance, so the next option
    can start without a second call. All three questions offer exactly the payload's criteria.

    `image` is this turn's game-view JPEG. It is attached as `images[0]` by the same helper the adapter sends
    with (`playtest.runner.image_transport`), beside the payload's own `game_view` note, so the compact menu
    carries the view the full layout carries and not one character of it is encoded twice.
    """
    criteria = {item["id"]: item["label"] for item in payload["choices"]}
    questions = {PRIMARY: {"type": "choice", "instructions": MENU_INSTRUCTIONS, "criteria": criteria}}
    if contingent:
        questions[BLOCKED_QUESTION] = {"type": "choice", "instructions": IF_BLOCKED, "criteria": criteria}
        questions[SUCCEEDED_QUESTION] = {"type": "choice", "instructions": IF_SUCCEEDED, "criteria": criteria}
    return attach_images({"model": model, "questions": questions, "state": payload}, arm, image)


# --- choice labels ----------------------------------------------------------------------


def _kind(choice) -> ChoiceKind | None:
    kind = getattr(choice, "kind", None)
    try:
        return ChoiceKind(kind)
    except ValueError:
        return None


def _fixed(kind: ChoiceKind | None, choice) -> str | None:
    """A short fixed label for the choices that carry no measured expectation."""
    if kind is ChoiceKind.wait:
        seconds = _num((getattr(choice, "params", None) or {}).get("duration_game_s"))
        return f"wait{SEPARATOR}{seconds:g} s no input" if seconds is not None else "wait"
    if kind is ChoiceKind.finish:
        return f"finish{SEPARATOR}end the session now"
    if kind is ChoiceKind.escalate:
        return f"escalate{SEPARATOR}ask for help"
    if kind is ChoiceKind.delegate:
        return f"delegate{SEPARATOR}hand a bounded task to a helper"
    if kind is ChoiceKind.restart_play_mode:
        return f"restart{SEPARATOR}restart the session from the start"
    return None


def _option_intent(choice) -> str:
    template = getattr(choice, "template", None)
    params = getattr(choice, "params", None) or {}
    if template == "wait":
        seconds = _num(params.get("seconds"))
        return f"wait {seconds:g} s" if seconds is not None else "wait"
    if template == "place":
        return "place"
    if template == "sweep":
        return "resolve what blocks progress here by trying each untried control once"
    label = _direction(params.get("direction"))
    if template in ("hop", "traverse"):
        # A hop that presses the rise again in the air (`options.air_rise`) is named apart from the one that does not.
        name = "hop ×2" if template == "hop" and params.get("air_press_s") else template
        return f"{name} {label}" if label is not None else _first_clause(getattr(choice, "label", "") or "")
    if template == "retreat":
        back = params.get("from")
        if isinstance(back, str) and back:
            return f"retreat from {back}"
        if label is not None:
            return f"retreat from {label}"
        return _first_clause(getattr(choice, "label", "") or "")
    return _first_clause(getattr(choice, "label", "") or "")


def _option_expected(choice) -> str:
    template = getattr(choice, "template", None)
    params = getattr(choice, "params", None) or {}
    if template == "sweep":
        return f"{len(params.get('actions') or ())} untried"
    if template == "wait":
        return "no input"
    if template == "place":
        point = _vector(params.get("position"))
        return "at (" + ", ".join(_n(value) for value in point) + ")" if point is not None else "position unmeasured"
    if template == "hop":
        parts = [f"run-up {_n(value)}" for value in (_num(params.get("run_up")),) if value is not None]
        air, high = _num(params.get("air_s")), _num(params.get("air_peak"))
        if air is not None and high is not None:
            # A measured flight (`options.air_rise`): the same basis for a hop and a hop that presses again.
            parts += [f"air {air:.2f} s", f"peak {high:.2f}"]
            return ", ".join(parts)
        flight = _num(params.get("flight_s"))
        peak = _num(params.get("rise_peak"))
        if flight is not None:
            parts.append(f"rise {flight:.2f} s")
        if peak is not None:
            parts.append(f"peak {peak:.2f}")
        return ", ".join(parts) if parts else "expectation unmeasured"
    if template == "traverse":
        parts = [f"{value:.2f} u/s" for value in (_num(params.get("speed")),) if value is not None]
        step = _num(params.get("step"))
        if step is not None:
            parts.append(f"step {step:.2f}")
        return ", ".join(parts) if parts else "expectation unmeasured"
    if template == "retreat":
        distance = _num(params.get("distance"))
        speed = _num(params.get("speed"))
        if distance is not None and speed is not None:
            return f"{distance:.2f} at {speed:.2f} u/s"
        return "expectation unmeasured"
    return _label_expectation(getattr(choice, "label", "") or "")


def _effect(label: str) -> str:
    """The one calibrated number a manifest label carries, with its variance and trial count removed."""
    match = _EFFECT.search(label or "")
    if match is not None:
        return f"{float(match.group(1)):.2f} units"
    if _NO_EFFECT.search(label or ""):
        return "effect unknown"
    return "effect unmeasured"


def _fit(intent: str, expected: str, suffix: str) -> str:
    """`<intent> · <expected> · <suffix>`, shortened to fit: expectation first, then a history tail mark.

    A history longer than the label can carry keeps its head and is marked with an ellipsis, so a clipped
    suffix is never mistaken for the whole measured record.
    """
    if len(intent) >= LABEL_LIMIT:
        # Leave room for expectation/history; the unabridged details are kept in the state.
        intent = intent[:LABEL_LIMIT // 2 - 1] + "\u2026"
    text = SEPARATOR.join(part for part in (intent, expected, suffix) if part)
    if len(text) <= LABEL_LIMIT:
        return text
    for shortened in (_first_segment(expected), ""):
        text = SEPARATOR.join(part for part in (intent, shortened, suffix) if part)
        if len(text) <= LABEL_LIMIT:
            return text
    return _clip_suffix(intent, suffix)


def _clip_suffix(intent: str, suffix: str) -> str:
    """The history's head that fits, keeping its `best` quantity, and an ellipsis for what it left out."""
    room = LABEL_LIMIT - len(intent) - len(SEPARATOR)
    if room < 2:
        # The intent alone has nearly filled the label: it is clipped, so no label leaves the limit.
        return intent[:LABEL_LIMIT - 1] + "\u2026"
    base, mark, best = suffix.partition(", best ")
    tail = f", best {best}" if mark else ""
    budget = room - len(tail) - 1
    if budget < 1:
        return f"{intent}{SEPARATOR}{suffix[:room - 1]}\u2026"
    clauses = base.split(", ")
    kept = clauses[0]
    for clause in clauses[1:]:
        candidate = f"{kept}, {clause}"
        if len(candidate) > budget:
            break
        kept = candidate
    text = f"{intent}{SEPARATOR}{kept}{tail}\u2026"
    if len(text) <= LABEL_LIMIT:
        return text
    if budget >= 1:  # the first clause alone is too long: keep its head and still mark it as clipped
        return f"{intent}{SEPARATOR}{kept[:budget]}\u2026"
    return intent[:LABEL_LIMIT - 1] + "\u2026"


def _first_segment(expected: str) -> str:
    return expected.split(",")[0].strip()


def _first_clause(label: str) -> str:
    """The first clause of a label: its intent, before any expectation or history separator."""
    head = label.split(" \u2014 ")[0].split(":")[0].strip()
    return head or label.strip()


def _label_expectation(label: str) -> str:
    head = label.split(" \u2014 ")[0]
    return head.split(":", 1)[1].strip() if ":" in head else ""


def _suffix(memory: OutcomeMemory, scene: str | None, position: Sequence[float] | None,
            option_id: str) -> str:
    return memory.suffix(scene, None if position is None else list(position), option_id)


def _direction(value: Any) -> str | None:
    """A measured unit vector's dominant ground axis, with its sign: `+x`, `-z`, ..."""
    unit = _vector(value)
    if unit is None:
        return None
    axes = [(index, unit[index]) for index in GROUND_AXES if index < len(unit)]
    if not axes:
        return None
    index, component = max(axes, key=lambda item: abs(item[1]))
    if component == 0:
        return None
    return ("+" if component > 0 else "-") + AXES[index]


# --- the payload's parts ----------------------------------------------------------------


def _directive_view(directive: Any) -> dict | None:
    """The open directive as id, text and its checkable setpoint; nothing else is carried."""
    if directive is None:
        return None
    if isinstance(directive, Mapping):
        ident, text = directive.get("directive_id"), directive.get("text")
        expected, tolerance = directive.get("expected"), directive.get("tolerance")
    else:
        ident, text = getattr(directive, "directive_id", None), getattr(directive, "text", None)
        expected, tolerance = getattr(directive, "expected", None), getattr(directive, "tolerance", None)
    if not isinstance(ident, str) or not ident or not isinstance(text, str) or not text:
        raise ValueError("a directive the payload shows needs an id and a text")
    outcome = directive.get("outcome") if isinstance(directive, Mapping) else getattr(directive, "outcome", None)
    outcome = outcome.value if isinstance(outcome, Enum) else outcome
    setpoint = _setpoint(expected, tolerance)
    if setpoint is not None and outcome == "met":  # a held directive: its check is met, its advice still stands
        setpoint = f"{setpoint} (met)"
    return {"id": ident, "text": text, "setpoint": setpoint}


def _setpoint(expected: Any, tolerance: Any) -> str | None:
    if expected is None:
        return None
    if isinstance(expected, Mapping):
        field, op, value = expected.get("field"), expected.get("op"), expected.get("value")
    else:
        field, op = getattr(expected, "field", None), getattr(expected, "op", None)
        value = getattr(expected, "value", None)
    if isinstance(op, Enum):
        op = op.value
    if not isinstance(field, str) or not field or not isinstance(op, str) or not op:
        return None
    text = f"{field} {op}"
    if value is not None:
        try:
            text += " " + json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError):
            pass
    if isinstance(tolerance, (int, float)) and not isinstance(tolerance, bool) and math.isfinite(tolerance):
        text += f" (tol {tolerance:g})"
    return text


def _grounded(probe: Any, cell: float) -> bool | None:
    """True when the floor under the anchor is at the anchor's own origin, within a twentieth of a step.

    Only a measured hit at the anchor's own spot can say this. A body whose origin sits above its feet
    reports a floor further down, and that is unknown here rather than a claim that it is airborne.
    """
    if not isinstance(probe, Mapping):
        return None
    under = probe.get("under")
    if not isinstance(under, Mapping) or under.get("hit") is not True:
        return None
    delta = _num(under.get("delta_y"))
    return True if delta is not None and abs(delta) <= GROUNDED_FRACTION * cell else None


def _probe_summary(probe: Any) -> str | None:
    """Where the first obstruction is, and where the floor ahead ends, from the probe's own readings."""
    if not isinstance(probe, Mapping):
        return None
    parts: list[str] = []
    under = probe.get("under")
    no_floor = isinstance(under, Mapping) and under.get("hit") is False
    if no_floor:
        parts.append("no floor under")
    hits: list[float] = []
    clear: list[float] = []
    for cast in probe.get("blockers") or ():
        if not isinstance(cast, Mapping):
            continue
        distance, reach = _num(cast.get("distance")), _num(cast.get("max_distance"))
        if cast.get("hit") is True and distance is not None:
            hits.append(distance)
        elif cast.get("hit") is False and reach is not None:
            clear.append(reach)
    if hits:
        parts.append(f"blocker {_n(min(hits))} ahead")
    elif clear:
        parts.append(f"no blocker within {_n(min(clear))}")
    if not no_floor:
        edges = [_num(sample.get("forward")) for sample in probe.get("samples") or ()
                 if isinstance(sample, Mapping) and sample.get("hit") is False]
        known = [edge for edge in edges if edge is not None and edge > 0]
        if known:
            parts.append(f"floor edge {_n(min(known))} ahead")
    return ", ".join(parts) if parts else None


class _Seen:
    """The few observation fields the memory reads, from an observation object or its JSON mapping."""

    def __init__(self, observation: Any) -> None:
        self._observation = observation

    @property
    def scene(self) -> Any:
        return _field(self._observation, "scene")

    @property
    def paused(self) -> Any:
        return _field(self._observation, "paused")

    @property
    def time_scale(self) -> Any:
        return _field(self._observation, "time_scale")

    @property
    def public(self) -> Mapping[str, Any]:
        public = _field(self._observation, "public")
        return public if isinstance(public, Mapping) else {}


# --- numbers ----------------------------------------------------------------------------


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _vector(value: Any) -> tuple[float, ...] | None:
    """A measured vector as finite floats, or None when it is not one (unknown is never zero)."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        return None
    out: list[float] = []
    for item in value:
        number = _num(item)
        if number is None:
            return None
        out.append(number)
    return tuple(out)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _n(value: float) -> str:
    """A quantity to one decimal, negative with the typographic minus and no negative zero."""
    rounded = round(float(value), 1)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:.1f}".replace("-", MINUS, 1)


def _varies(measured: Sequence[Sequence[float]], axis: int) -> bool:
    values = [value[axis] for value in measured]
    return min(values) != max(values)


def _max_at(measured: Sequence[Sequence[float]], axis: int) -> float:
    return max(value[axis] for value in measured)


def _min_at(measured: Sequence[Sequence[float]], axis: int) -> float:
    return min(value[axis] for value in measured)
