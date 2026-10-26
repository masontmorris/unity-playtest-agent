"""Preflight stages 4-6: the model's answer, the stage prompts and validation (build bible 7).

The model chooses from what code discovered. A reader is a discovered field id, held to an enum in
the schema; an action's inputs are discovered control ids, also an enum. Code never adds to or
removes from what the model chose: a missing control is a gap, not a synthesized action. An answer
is checked in three layers: the schema (constrained decoding, then the model classes), references
(ids exist; later stages name stage-4 readers by alias) and consistency (stage scope, checks code
can score). The exact problems go back to the model on a retry; nothing is repaired.

One schema serves every stage of a project's chain, so `response_format` stays byte-identical
across stages and retries; #70 found that a changed `response_format` lost provider cache reuse.
Each stage owns a nullable part (`stage_4`, `stage_5`, `stage_6`): a stage answer fills its own
part and leaves the others null, and stage "all" fills all three in one call. Parts are nullable
objects rather than an `anyOf` of stage objects because Muse picks an object branch by the first
sorted key (#73), and the part names sort in stage order, which is the order Muse emits them.

Checks name a reader by its alias, the key S1 and the frontier see as `public.<alias>`, so the
discovered-field enum appears once in the schema. Repeated at every field reference, it rendered
nine times and cost about 7,500 prompt tokens for a 224-field project (#73).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from playtest.contracts.common import Strict
from playtest.contracts.investigation import Comparator

from .checks import check_problems, value_kind
from .strict import enum_str, render

FieldId = enum_str("field_ids")
ControlId = enum_str("control_ids")
Stage = Literal["readers", "actions", "progress", "all"]

NAME = r"^[a-z][a-z0-9_]{0,39}$"
MAX_READERS = 16
MAX_ACTIONS = 14
MAX_SKILLS = 6
MAX_INPUTS = 3
MAX_STEPS = 8
MAX_PRECONDITIONS = 3
MAX_PROGRESS = 6
MAX_INVARIANTS = 12
MAX_GAPS = 8

# Input System expected-control types whose value is an axis, not a press.
AXIS_CONTROLS = frozenset({"Vector2", "Vector3", "Axis", "Stick", "Dpad", "Delta"})
ONE_D_CONTROLS = frozenset({"Axis"})
PARTS: dict[str, str] = {"readers": "stage_4", "actions": "stage_5", "progress": "stage_6"}


# --- answer models -------------------------------------------------------------------------------


class Check(Strict):
    """One reader compared with one value; code evaluates it on every observation."""

    field: str = Field(pattern=NAME, description="A stage-4 reader alias")
    op: Comparator
    value: float | str | bool | None = Field(description="null exactly for changed/unchanged")


class Reader(Strict):
    alias: str = Field(pattern=NAME, description="Short unique snake_case name the decision model sees")
    field_id: FieldId
    why: str = Field(min_length=1)


class ControlInput(Strict):
    control_id: ControlId
    x: float | None = Field(ge=-1, le=1, description="Axis value in [-1, 1] for an axis control; null for a button")
    y: float | None = Field(ge=-1, le=1, description="Second axis value; 0 for a one-dimensional axis, null for a button")


class Action(Strict):
    hold_s: float | None = Field(gt=0, description="Seconds to hold; null taps buttons at the measured minimum hold")
    inputs: list[ControlInput] = Field(min_length=1, max_length=MAX_INPUTS, description="Controls held together")
    intent: str = Field(min_length=1, description="What it is for. Not a measured effect: calibration measures that")
    name: str = Field(pattern=NAME)
    preconditions: list[Check] = Field(max_length=MAX_PRECONDITIONS)


class SkillStep(Strict):
    hold_s: float | None = Field(gt=0)
    inputs: list[ControlInput] = Field(min_length=1, max_length=MAX_INPUTS)
    wait_s: float = Field(ge=0, description="Seconds to wait after this step")


class Skill(Strict):
    max_s: float = Field(gt=0, description="Upper bound on the whole skill, seconds")
    name: str = Field(pattern=NAME)
    purpose: str = Field(min_length=1)
    steps: list[SkillStep] = Field(min_length=1, max_length=MAX_STEPS)


class ProgressField(Strict):
    direction: Literal["up", "down"]
    field: str = Field(pattern=NAME, description="A stage-4 reader alias")
    why: str = Field(min_length=1)


class Invariant(Strict):
    """One flat rule. range uses field with min and/or max; monotonic uses field and direction; implies
    uses when and then. Fields a kind does not use are null; `unless` excuses range and monotonic."""

    catches: str = Field(min_length=1, description="The bug a violation would reveal")
    direction: Literal["non_decreasing", "non_increasing"] | None
    field: str | None = Field(pattern=NAME, description="A stage-4 reader alias")
    kind: Literal["range", "monotonic", "implies"]
    max: float | None
    min: float | None
    name: str = Field(pattern=NAME)
    source: str = Field(min_length=1, description="Code that guarantees the rule, e.g. Class.Method")
    then: Check | None
    unless: Check | None
    when: Check | None


class ReadersPart(Strict):
    readers: list[Reader] = Field(max_length=MAX_READERS)
    unobservable: list[str] = Field(max_length=MAX_GAPS, description="State a playtester needs that no reader covers")


class ActionsPart(Strict):
    actions: list[Action] = Field(max_length=MAX_ACTIONS)
    skills: list[Skill] = Field(max_length=MAX_SKILLS)
    uncontrollable: list[str] = Field(max_length=MAX_GAPS, description="What a playtester must do that no control supports")


class ProgressPart(Strict):
    invariants: list[Invariant] = Field(max_length=MAX_INVARIANTS)
    progress: list[ProgressField] = Field(max_length=MAX_PROGRESS)
    unjudgeable: list[str] = Field(max_length=MAX_GAPS, description="What stops judging success or noticing a bug")


class StageAnswer(Strict):
    stage_4: ReadersPart | None
    stage_5: ActionsPart | None
    stage_6: ProgressPart | None

    def aliases(self) -> dict[str, str]:
        return {} if self.stage_4 is None else {r.alias: r.field_id for r in self.stage_4.readers}


# --- prompts -------------------------------------------------------------------------------------

SYSTEM = (
    "You are the preflight analyst for an autonomous game playtester. Code has already discovered the game's "
    "fields and input controls; they are listed below with ids, followed by the game's source. Choose only from "
    "those ids and never invent ids, fields, controls or code. One JSON schema serves every stage: fill the part "
    "for the stage you are asked for and set the other parts to null."
)

CHECKS = (
    "A check names a stage-4 reader by its alias and compares it with one value: changed and unchanged take value "
    "null and compare with the previous sample; <, <=, > and >= need a number; == and != need a value of the "
    "reader's type. Vector readers support only changed and unchanged."
)

_BODY = {
    "readers": (
        "Stage 4, readers. Choose up to 16 fields worth reading on every observation: runtime state that changes "
        "during play and that someone watching the game could notice. Prefer runtime state over configuration. "
        "Give each a short unique snake_case alias and one line on why. Under unobservable, list state a "
        "playtester needs that no chosen field covers."
    ),
    "actions": (
        "Stage 5, actions and skills. Compose up to 14 actions from the discovered controls only. An action holds "
        "one to three controls together: give each axis control x and y in [-1, 1] (y = 0 for a one-dimensional "
        "axis) and each button null x and y, plus the hold time in seconds (null taps buttons). State the intent "
        "in a few words; calibration, not you, measures the effect. Add preconditions only where an action makes "
        "no sense otherwise. Then up to 6 skills: short bounded sequences of steps, such as a recovery or reaching "
        "a menu, with a total time bound. Under uncontrollable, list what a playtester must do that no control "
        "supports."
    ),
    "progress": (
        "Stage 6, progress, invariants and gaps. Use stage-4 readers only. Progress is a reader that counts toward "
        "an objective, such as a score, a collected count or a level index, with the direction it moves; timers, "
        "ages, blends, input values and activity counts are not progress. If no reader tracks progress, leave "
        "progress empty and say so under unjudgeable. Write invariants that must hold in normal play, as one of "
        "three kinds: range (field with min and/or max), monotonic (field with a direction) or implies (a "
        "when-check and a then-check on the same sample); range and monotonic may add an unless-check that "
        "excuses a sample. Leave the fields a kind does not use null. Code checks every rule on every sample, "
        "including mid-transition: a blend or timer that eases toward a value after its flag changes does not "
        "yet satisfy an implies rule over that value. Skip vacuous rules: one that restates a clamp, min, max or "
        "assignment in the code that writes the field, or a bound its updates cannot cross (a timer or counter "
        "that only counts up is never negative), can only catch a code change. Name the code that "
        "guarantees each rule and the bug a violation would reveal. Under unjudgeable, list what would stop a "
        "playtester from judging success or noticing a bug."
    ),
}

STAGE_PROMPTS: dict[str, str] = {
    "readers": f"{_BODY['readers']} Fill stage_4; set stage_5 and stage_6 to null.",
    "actions": f"{_BODY['actions']} Fill stage_5; set stage_4 and stage_6 to null. {CHECKS}",
    "progress": f"{_BODY['progress']} Fill stage_6; set stage_4 and stage_5 to null. {CHECKS}",
    "all": (
        "Answer stages 4, 5 and 6 together: fill stage_4, stage_5 and stage_6. Preconditions, progress and "
        "invariants use the readers chosen in stage_4 of this answer.\n"
        + "\n".join(_BODY[s] for s in ("readers", "actions", "progress"))
        + f"\n{CHECKS}"
    ),
}


def first_turn(packet: str, stage: Stage) -> str:
    """The first user message: the shared packet, then the stage instructions last."""
    return f"{packet}\n\n=== INSTRUCTIONS ===\n{STAGE_PROMPTS[stage]}"


def retry_turn(stage: Stage, problems: Sequence[str]) -> str:
    """A retry appended after a rejected answer: the exact problems, nothing else."""
    listed = "\n".join(f"- {p}" for p in problems)
    return f"That {stage} answer was rejected:\n{listed}\nAnswer the {stage} stage again, fixing exactly these problems."


# --- discovery input -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Discovery:
    """What stage 1 found, as the schema and validation need it: ids and declared types."""

    field_types: Mapping[str, str]
    control_types: Mapping[str, str]

    @classmethod
    def from_records(cls, fields: Sequence[Mapping[str, Any]], controls: Sequence[Mapping[str, Any]]) -> Discovery:
        return cls({f["id"]: str(f.get("type") or "") for f in fields},
                   {c["id"]: str(c.get("control_type") or "Button") for c in controls})

    def enums(self) -> dict[str, list[str]]:
        return {"field_ids": list(self.field_types), "control_ids": list(self.control_types)}


def schema(discovery: Discovery, **render_args: Any) -> dict[str, Any]:
    """The provider schema for a project: identical for every stage and retry of its chain."""
    return render(StageAnswer, discovery.enums(), **render_args)


# --- validation ----------------------------------------------------------------------------------


def review(answer: StageAnswer, stage: Stage, discovery: Discovery, readers: Mapping[str, str] | None = None) -> list[str]:
    """Reference and consistency problems in a decoded answer; empty when it is accepted.

    `readers` maps the aliases of the stage-4 answer accepted earlier in the chain to field ids.
    For stage "all" the answer's own stage_4 counts instead.
    """
    problems: list[str] = []
    wanted = set(PARTS.values()) if stage == "all" else {PARTS[stage]}
    for part in PARTS.values():
        present = getattr(answer, part) is not None
        if part in wanted and not present:
            problems.append(f"{part}: fill this part for the {stage} stage")
        elif part not in wanted and present:
            problems.append(f"{part}: must be null when answering the {stage} stage")
    aliases = answer.aliases() if stage == "all" else dict(readers or {})
    if answer.stage_4 is not None:
        problems += _readers(answer.stage_4, discovery)
    if answer.stage_5 is not None:
        problems += _actions(answer.stage_5, discovery, aliases)
    if answer.stage_6 is not None:
        problems += _progress(answer.stage_6, discovery, aliases)
    return problems


def _reader_kind(where: str, alias: str | None, discovery: Discovery,
                 aliases: Mapping[str, str]) -> tuple[str | None, list[str]]:
    if alias is None:
        return None, [f"{where}: names no reader"]
    field_id = aliases.get(alias)
    if field_id is None:
        return None, [f"{where}: {alias!r} is not a stage-4 reader alias"]
    return value_kind(discovery.field_types.get(field_id)), []


def _check(where: str, check: Check, discovery: Discovery, aliases: Mapping[str, str]) -> list[str]:
    kind, found = _reader_kind(f"{where}.field", check.field, discovery, aliases)
    if kind is None:
        return found
    return check_problems(where, kind, check.op, check.value)  # type: ignore[arg-type]


def _readers(part: ReadersPart, discovery: Discovery) -> list[str]:
    problems: list[str] = []
    if not part.readers:
        problems.append("stage_4.readers: choose at least one field")
    seen_ids: set[str] = set()
    seen_alias: set[str] = set()
    for i, r in enumerate(part.readers):
        where = f"stage_4.readers[{i}]"
        if r.field_id not in discovery.field_types:
            problems.append(f"{where}.field_id: {r.field_id!r} is not a discovered field")
        if r.field_id in seen_ids:
            problems.append(f"{where}: field {r.field_id!r} is chosen twice")
        if r.alias in seen_alias:
            problems.append(f"{where}: alias {r.alias!r} is used twice")
        seen_ids.add(r.field_id)
        seen_alias.add(r.alias)
    return problems


def _inputs(where: str, inputs: Sequence[ControlInput], hold_s: float | None, discovery: Discovery) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for j, c in enumerate(inputs):
        at = f"{where}.inputs[{j}]"
        kind = discovery.control_types.get(c.control_id)
        if kind is None:
            problems.append(f"{at}: {c.control_id!r} is not a discovered control")
            continue
        if c.control_id in seen:
            problems.append(f"{at}: control {c.control_id!r} appears twice")
        seen.add(c.control_id)
        if kind in AXIS_CONTROLS:
            if c.x is None or c.y is None:
                problems.append(f"{at}: {c.control_id!r} is a {kind} control and needs x and y")
            elif kind in ONE_D_CONTROLS and c.y != 0:
                problems.append(f"{at}: {c.control_id!r} is one-dimensional; y must be 0")
            if hold_s is None:
                problems.append(f"{where}: an axis input needs a hold time")
        elif c.x is not None or c.y is not None:
            problems.append(f"{at}: {c.control_id!r} is a {kind} control; x and y must be null")
    return problems


def _actions(part: ActionsPart, discovery: Discovery, aliases: Mapping[str, str]) -> list[str]:
    problems: list[str] = []
    if not part.actions:
        problems.append("stage_5.actions: compose at least one action")
    names: set[str] = set()
    for i, a in enumerate(part.actions):
        where = f"stage_5.actions[{i}]"
        if a.name in names:
            problems.append(f"{where}: name {a.name!r} is used twice")
        names.add(a.name)
        problems += _inputs(where, a.inputs, a.hold_s, discovery)
        for k, pre in enumerate(a.preconditions):
            problems += _check(f"{where}.preconditions[{k}]", pre, discovery, aliases)
    for i, s in enumerate(part.skills):
        where = f"stage_5.skills[{i}]"
        if s.name in names:
            problems.append(f"{where}: name {s.name!r} is already an action or skill")
        names.add(s.name)
        total = 0.0
        for j, step in enumerate(s.steps):
            problems += _inputs(f"{where}.steps[{j}]", step.inputs, step.hold_s, discovery)
            total += (step.hold_s or 0.0) + step.wait_s
        if total > s.max_s:
            problems.append(f"{where}: steps take {total:g} s, more than max_s {s.max_s:g}")
    return problems


_USES = {"range": {"field", "min", "max", "unless"}, "monotonic": {"field", "direction", "unless"},
         "implies": {"when", "then"}}


def _progress(part: ProgressPart, discovery: Discovery, aliases: Mapping[str, str]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for i, p in enumerate(part.progress):
        where = f"stage_6.progress[{i}]"
        kind, found = _reader_kind(f"{where}.field", p.field, discovery, aliases)
        if kind is not None and kind not in ("number", "bool"):
            found.append(f"{where}: progress needs a numeric or bool reader")
        if p.field in seen:
            found.append(f"{where}: reader {p.field!r} is listed twice")
        seen.add(p.field)
        problems += found
    names: set[str] = set()
    for i, rule in enumerate(part.invariants):
        where = f"stage_6.invariants[{i}]"
        if rule.name in names:
            problems.append(f"{where}: name {rule.name!r} is used twice")
        names.add(rule.name)
        unused = sorted(k for k in ("field", "min", "max", "direction", "when", "then", "unless")
                        if k not in _USES[rule.kind] and getattr(rule, k) is not None)
        if unused:
            problems.append(f"{where}: a {rule.kind} rule leaves {', '.join(unused)} null")
        if rule.kind == "implies":
            for side in ("when", "then"):
                check = getattr(rule, side)
                if check is None:
                    problems.append(f"{where}: an implies rule needs {side}")
                else:
                    problems += _check(f"{where}.{side}", check, discovery, aliases)
            if rule.when is not None and rule.when == rule.then:
                problems.append(f"{where}: when and then are the same check")
            continue
        kind, found = _reader_kind(f"{where}.field", rule.field, discovery, aliases)
        if kind is not None and kind != "number":
            found.append(f"{where}: a {rule.kind} rule needs a numeric reader")
        problems += found
        if rule.kind == "range":
            if rule.min is None and rule.max is None:
                problems.append(f"{where}: a range needs min, max or both")
            elif rule.min is not None and rule.max is not None and rule.min > rule.max:
                problems.append(f"{where}: min {rule.min:g} is above max {rule.max:g}")
        elif rule.direction is None:
            problems.append(f"{where}: a monotonic rule needs a direction")
        if rule.unless is not None:
            problems += _check(f"{where}.unless", rule.unless, discovery, aliases)
    return problems
