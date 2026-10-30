"""The frontier's answer to an escalation: contract, prompt and validation (build bible 5.6, 6, 9).

One schema serves every frontier call in a run. Its enums are run-stable: the observation fields a
setpoint may test, and the manifest actions and skills a choice may instantiate. Per-call
references (sequence numbers, hypothesis ids) are named in the packet and checked by `review`.
A problem rejects only the item it is in, with its reason; nothing is repaired or substituted.

The frontier never issues input. Its directive is a controlled test that code scores, its choices
instantiate manifest actions and skills for S1 to pick, and its finding is a candidate whose state
code sets. What S1 does with any of it is S1's decision.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field

from playtest.contracts.common import Strict
from playtest.contracts.investigation import (
    Comparator,
    Directive,
    Expected,
    ExpectedSource,
    Finding,
    FindingState,
    ReproCheck,
    Target,
)
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource

from .checks import CHANGE, ValueKind, check_problems, evaluate, field_value
from .strict import enum_str, render

FieldPath = enum_str("fields")
ManifestName = enum_str("manifest_names")

MAX_HYPOTHESES = 4
MAX_ADD = 3
MAX_PRUNE = 3
MAX_STEPS = 8


# --- answer models -------------------------------------------------------------------------------
# Only list caps are model constraints (the decoder cuts a list at its cap). Every value rule is in
# `review`, so one bad item is rejected on its own instead of failing the whole answer.


class TargetOut(Strict):
    kind: Literal["field", "entity", "ui_element", "location"]
    ref: str


class Setpoint(Strict):
    """A postcondition code can score: one manifest field, one comparator, one value."""

    field: FieldPath
    op: Comparator
    tolerance: float | None = Field(description="Only for numeric == or !=")
    value: float | str | bool | None = Field(description="null exactly for changed/unchanged")


class DirectiveOut(Strict):
    deadline_s: float | None = Field(
        description="Game seconds S1 has after it receives the directive; required for an unchanged setpoint")
    expected: Setpoint | None = Field(
        description="null when no field can show the outcome; scored unknown. Otherwise met the first time a "
                    "reading satisfies it, so it must not already hold. changed: met when the value first differs "
                    "from its value when S1 receives the directive. unchanged: met only if the value stays as it "
                    "is until the deadline")
    target: TargetOut | None
    tests: str | None = Field(description="Id of the hypothesis this tests, or null")
    text: str = Field(description="The controlled test, for S1, in one or two sentences")


class HypothesisOut(Strict):
    confirm_if: str
    contradicting_seq: list[int]
    id: str = Field(description="An open hypothesis id, or one of new_hypothesis_ids")
    refute_if: str
    statement: str
    status: Literal["open", "supported", "contradicted", "withdrawn"]
    supporting_seq: list[int]


class AddChoice(Strict):
    hold_s: float | None = Field(description="Hold override for an action; null keeps the manifest's")
    label: str
    name: ManifestName
    x: float | None = Field(description="Axis override in [-1, 1] for an axis action; null keeps the manifest's")
    y: float | None


class PruneChoice(Strict):
    name: ManifestName
    reason: str


class FindingOut(Strict):
    actual: str
    conditions: str
    evidence_seq: list[int]
    expected: str
    expected_source: ExpectedSource
    repro_check: Setpoint | None = Field(
        description="What a replay from a reset must show to reproduce it: false right after the reset, true once "
                    "the bug shows. null when no field can show it")
    severity: Literal["blocker", "major", "minor", "cosmetic", "unknown"]
    severity_reason: str
    steps: list[str] = Field(max_length=MAX_STEPS)
    title: str
    uncertainty: str


class FrontierAnswer(Strict):
    """Muse emits keys in sorted order: add_choices, candidate_finding, directive, hypotheses, prune_choices."""

    add_choices: list[AddChoice] = Field(max_length=MAX_ADD)
    candidate_finding: FindingOut | None
    directive: DirectiveOut | None = Field(description="null keeps the current directive")
    hypotheses: list[HypothesisOut] = Field(max_length=MAX_HYPOTHESES)
    prune_choices: list[PruneChoice] = Field(max_length=MAX_PRUNE)


# --- prompt --------------------------------------------------------------------------------------

SYSTEM = (
    "You are the frontier investigator for an autonomous game playtester. A fast decision model, S1, plays in "
    "real time and has asked for help. The game keeps running while you think, so your answer arrives seconds "
    "after the packet's as_of. You never issue input; S1 decides what to do with your answer.\n"
    "- hypotheses: interpretations of the evidence. Update an open hypothesis by its id, or start one with an id "
    "from new_hypothesis_ids. An update replaces the statement, status, confirm_if and refute_if; its "
    "supporting_seq and contradicting_seq list only evidence this packet adds, and earlier citations stay on "
    "record. Cite only sequence numbers that appear in the packet, and say what would confirm and what would "
    "refute each.\n"
    "- directive: one controlled test for S1, or null to keep the current directive. Its expected setpoint must "
    "be checkable by code: a manifest field, an operator, and a value of that field's type. Every operator "
    "except changed and unchanged is met the first time a reading satisfies it, so the setpoint must not "
    "already hold in the packet's observation; one that does is rejected. changed and unchanged take value null "
    "and compare with the value when S1 receives the directive. changed is met when the value first differs from "
    "it. unchanged is met only if the value stays as it is until the deadline, and it needs deadline_s. A "
    "tolerance applies only to numeric == or !=. If no field can show the outcome, set expected to null; the "
    "directive is then scored unknown. deadline_s is game seconds S1 has after it receives the directive.\n"
    "- add_choices: manifest actions or skills S1 should be able to pick during this directive, optionally with "
    "a hold or axis override for an action. prune_choices: manifest actions or skills to hide during this "
    "directive. Both end when the directive closes. S1 always has wait, escalate and finish; they are not manifest "
    "choices.\n"
    "- candidate_finding: only when cited evidence already shows behaviour that contradicts an expectation; "
    "otherwise null. Say where the expectation comes from and what is still uncertain. repro_check is what a "
    "replay must show. A replay starts from a reset, and a check that already holds right after the reset cannot "
    "show the bug, so it never counts: an unchanged check always holds then, and so does a value the game shows "
    "at rest, such as a speed of zero. Choose a check only the bug makes true, or null if no field can show it. "
    "The packet's findings are the candidate findings already recorded in this run; do not propose one of them "
    "again.\n"
    "- A field's calibration, in the manifest, says how it changed when preflight sampled it on this same build "
    "before play. Behaviour that calibration already saw, such as a field that never changed, was there before "
    "S1 played.\n"
    "Observations are measured; null means unknown, not zero."
)


# --- run and packet context ----------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    axis: bool = False  # True when the action drives an axis control
    max_hold_s: float | None = None  # None: holds cannot be overridden


@dataclass(frozen=True)
class Manifest:
    """What a run's frontier schema and checks need from the manifest. Stable for the whole run."""

    fields: Mapping[str, ValueKind]  # observation path -> kind, e.g. "public.<alias>" -> "bool"
    actions: Mapping[str, ActionSpec]
    skills: Collection[str]
    # Observation path -> when preflight calibration saw the field change on this build ("input", "idle", "both"
    # or "no_change_observed"); a field calibration could not classify is absent. Shown to the model, never scored.
    calibration: Mapping[str, str] = field(default_factory=dict, compare=False)
    # Observation path -> what the manifest says about the field: "source" (the game field it reads) and
    # "preflight_note" (the preflight model's own description when it chose the field). Shown, never scored.
    field_notes: Mapping[str, Mapping[str, str]] = field(default_factory=dict, compare=False)

    def enums(self) -> dict[str, list[str]]:
        return {"fields": list(self.fields), "manifest_names": [*self.actions, *self.skills]}


@dataclass(frozen=True)
class PacketRefs:
    """What one escalation packet makes citable. Changes every call, so it is never in the schema."""

    seqs: frozenset[int]
    hypotheses: frozenset[str] = frozenset()
    new_hypothesis_ids: tuple[str, ...] = ()
    open_directive: bool = False
    # The request observation's public readings, as the packet shows them. `review` rejects a reach setpoint
    # they already satisfy. None when the caller has none: nothing is then judged to hold. A mapping is not
    # hashable, so it stays out of the generated eq and hash.
    public: Mapping[str, Any] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Problem:
    item: str  # "hypotheses[1]", "directive", "add_choices[0]", "candidate_finding", ...
    reason: str


@dataclass
class Review:
    problems: list[Problem] = field(default_factory=list)

    def rejected(self, item: str) -> bool:
        return any(p.item == item for p in self.problems)

    @property
    def clean(self) -> bool:
        return not self.problems


def schema(manifest: Manifest, **render_args: Any) -> dict[str, Any]:
    """The provider schema for a run: identical for every frontier call in it."""
    return render(FrontierAnswer, manifest.enums(), **render_args)


# --- validation ----------------------------------------------------------------------------------


def review(answer: FrontierAnswer, manifest: Manifest, refs: PacketRefs) -> Review:
    out = Review()

    def bad(item: str, reason: str) -> None:
        out.problems.append(Problem(item, reason))

    def cite(item: str, what: str, seqs: Sequence[int]) -> None:
        unknown = sorted(set(seqs) - refs.seqs)
        if unknown:
            bad(item, f"{what} cites sequence numbers not in the packet: {unknown}")

    def setpoint(item: str, sp: Setpoint) -> None:
        kind = manifest.fields.get(sp.field)
        if kind is None:
            bad(item, f"{sp.field!r} is not a manifest field")
            return
        for reason in check_problems(item, kind, sp.op, sp.value, sp.tolerance):
            bad(item, reason)

    def holds_now(sp: Setpoint) -> bool:
        """A reach setpoint the request observation already satisfies is met before S1 can act, so it tests nothing.

        Only a measured, scoreable reading counts: an unknown one (None) is not a reason to reject.
        """
        kind = manifest.fields.get(sp.field)
        if refs.public is None or kind is None or sp.op in CHANGE:
            return False
        value = field_value(refs.public, sp.field.removeprefix("public."))
        return evaluate(value, kind=kind, op=sp.op, expected=sp.value, tolerance=sp.tolerance) is True

    def text(item: str, **values: str) -> None:
        for name, value in values.items():
            if not value.strip():
                bad(item, f"{name} is empty")

    ids: set[str] = set()
    new_left = list(refs.new_hypothesis_ids)
    for i, h in enumerate(answer.hypotheses):
        item = f"hypotheses[{i}]"
        text(item, statement=h.statement, confirm_if=h.confirm_if, refute_if=h.refute_if)
        if h.id in ids:
            bad(item, f"hypothesis {h.id!r} appears twice")
        elif h.id not in refs.hypotheses:
            if h.id in new_left:
                new_left.remove(h.id)
            else:
                bad(item, f"{h.id!r} is neither an open hypothesis nor one of new_hypothesis_ids")
        ids.add(h.id)
        cite(item, "supporting_seq", h.supporting_seq)
        cite(item, "contradicting_seq", h.contradicting_seq)

    # A directive may test an open hypothesis or one this answer adds, if that item was accepted.
    testable = set(refs.hypotheses) | {
        h.id for i, h in enumerate(answer.hypotheses) if not out.rejected(f"hypotheses[{i}]")}
    d = answer.directive
    if d is not None:
        text("directive", text=d.text)
        if d.deadline_s is not None and d.deadline_s <= 0:
            bad("directive", "deadline_s must be positive")
        if d.target is not None and not d.target.ref.strip():
            bad("directive", "target.ref is empty")
        if d.tests is not None and d.tests not in testable:
            bad("directive", f"tests {d.tests!r}, which is not an open or accepted hypothesis")
        if d.expected is not None:
            setpoint("directive", d.expected)
            if d.expected.op is Comparator.unchanged and d.deadline_s is None:
                bad("directive", "an unchanged setpoint is met only if the value stays as it is until the "
                                 "deadline, so the directive needs deadline_s")
            if holds_now(d.expected):
                bad("directive", "the setpoint already holds at the request observation, so it tests nothing")

    unbounded = d is None and not refs.open_directive  # added and pruned choices live as long as a directive
    added: set[str] = set()
    for i, a in enumerate(answer.add_choices):
        item = f"add_choices[{i}]"
        if unbounded:
            bad(item, "no directive is open to bound this choice")
        text(item, label=a.label)
        if a.hold_s is not None and a.hold_s <= 0:
            bad(item, "hold_s must be positive")
        if any(v is not None and not -1 <= v <= 1 for v in (a.x, a.y)):
            bad(item, "x and y must be in [-1, 1]")
        spec = manifest.actions.get(a.name)
        if spec is None and a.name not in manifest.skills:
            bad(item, f"{a.name!r} is not a manifest action or skill")
            continue
        axis = (a.x, a.y)
        if spec is None and (a.hold_s is not None or axis != (None, None)):
            bad(item, f"{a.name!r} is a skill; it takes no hold or axis override")
        if spec is not None:
            if a.hold_s is not None and (spec.max_hold_s is None or a.hold_s > spec.max_hold_s):
                bad(item, f"hold_s {a.hold_s:g} is over the manifest bound {spec.max_hold_s}")
            if axis != (None, None) and not spec.axis:
                bad(item, f"{a.name!r} drives no axis control")
            elif None in axis and axis != (None, None):
                bad(item, "an axis override needs both x and y")
        key = repr((a.name, a.hold_s, axis))
        if key in added:
            bad(item, f"{a.name!r} is added twice with the same parameters")
        added.add(key)
    pruned: set[str] = set()
    added_names = {a.name for a in answer.add_choices}
    for i, p in enumerate(answer.prune_choices):
        item = f"prune_choices[{i}]"
        if unbounded:
            bad(item, "no directive is open to bound this pruning")
        text(item, reason=p.reason)
        if p.name in added_names:
            bad(item, f"{p.name!r} is both added and pruned")
        if p.name in pruned:
            bad(item, f"{p.name!r} is pruned twice")
        pruned.add(p.name)

    f = answer.candidate_finding
    if f is not None:
        text("candidate_finding", title=f.title, expected=f.expected, actual=f.actual,
             severity_reason=f.severity_reason, uncertainty=f.uncertainty)
        if not f.evidence_seq:
            bad("candidate_finding", "a candidate finding must cite evidence_seq")
        if not f.steps or not all(step.strip() for step in f.steps):
            bad("candidate_finding", "steps must be non-empty")
        cite("candidate_finding", "evidence_seq", f.evidence_seq)
        if f.repro_check is not None:
            setpoint("candidate_finding", f.repro_check)
    return out


# --- conversion into the record contracts --------------------------------------------------------


def to_directive(d: DirectiveOut, *, directive_id: str, parent: str | None, model: str, based_on_seq: int,
                 received_game_s: float | None) -> Directive:
    """The directive record. The deadline becomes absolute game time from when S1 received it;
    with no game clock it stays null and expiry cannot be scored."""
    expected = None
    tolerance = None
    if d.expected is not None:
        expected = Expected(field=d.expected.field, op=d.expected.op, value=d.expected.value)
        tolerance = d.expected.tolerance
    deadline = None
    if d.deadline_s is not None and received_game_s is not None:
        deadline = received_game_s + d.deadline_s
    target = Target(kind=d.target.kind, ref=d.target.ref) if d.target is not None else None
    return Directive(directive_id=directive_id, parent=parent, model=model, based_on_seq=based_on_seq, text=d.text,
                     target=target, expected=expected, tolerance=tolerance, deadline_game_s=deadline)


def to_choices(adds: Sequence[AddChoice], manifest: Manifest, *, ids: Sequence[str],
               ttl_game_s: float | None) -> list[Choice]:
    """Frontier choices that instantiate manifest actions and skills. Ids come from the choice builder."""
    out = []
    for choice_id, a in zip(ids, adds, strict=True):
        is_action = a.name in manifest.actions
        params: dict[str, Any] = {}
        if a.hold_s is not None:
            params["hold_s"] = a.hold_s
        if a.x is not None and a.y is not None:
            params["axis"] = {"x": a.x, "y": a.y}
        out.append(Choice(id=choice_id, kind=ChoiceKind.action if is_action else ChoiceKind.skill, label=a.label,
                          action=a.name if is_action else None, skill=None if is_action else a.name, params=params,
                          source=ChoiceSource.frontier, ttl_game_s=ttl_game_s))
    return out


def to_finding(f: FindingOut, *, finding_id: str, evidence_seq: Sequence[int] | None = None) -> Finding:
    """A candidate finding. The model never sets the state.

    The answer cites packet sequence numbers (bridge observations). A trace finding must cite trace entries,
    so a caller that writes it passes the mapped `evidence_seq`. Without one, the answer's own numbers are
    kept, as in a record that never enters a trace.
    """
    check = f.repro_check
    return Finding(finding_id=finding_id, state=FindingState.candidate, title=f.title, expected=f.expected,
                   expected_source=f.expected_source, actual=f.actual, conditions=f.conditions or None,
                   steps=list(f.steps), evidence_seq=list(f.evidence_seq if evidence_seq is None else evidence_seq),
                   uncertainty=f.uncertainty, severity=f.severity, severity_reason=f.severity_reason or None,
                   repro_check=(ReproCheck(field=check.field, op=check.op, value=check.value, tolerance=check.tolerance)
                                if check is not None else None))
