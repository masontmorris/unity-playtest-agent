"""The frontier's answer to an escalation: contract, prompt and validation (build bible 5.6, 6, 9).

One schema serves every frontier call in a run. Its enums are run-stable: the observation fields a
setpoint may test, the fields a finding's repro_check may test (those, plus `effect.<action>` for each
action calibration measured), and the manifest actions and skills a choice may instantiate. Per-call
references (sequence numbers, hypothesis ids) are named in the packet and checked by `review`.
A problem rejects only the item it is in, with its reason; nothing is repaired or substituted.

The frontier never issues input. Its directive is a controlled test that code scores, its choices
instantiate manifest actions and skills for S1 to pick, and its finding is a candidate whose state
code sets. What S1 does with any of it is S1's decision.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field

from playtest.contracts.common import Strict
from playtest.contracts.investigation import (
    EFFECT_PREFIX,
    MAX_OBJECTIVE_QUOTE,
    Comparator,
    Directive,
    Expected,
    ExpectedSource,
    Finding,
    FindingState,
    ReproCheck,
    Target,
    effect_action,
)
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource

from .checks import CHANGE, ORDERING, ValueKind, check_problems, evaluate, field_value
from .strict import enum_str, render

FieldPath = enum_str("fields")
CheckFieldPath = enum_str("check_fields")
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


class ReproCheckOut(Setpoint):
    """What a replay can score: a manifest field, or an action's effect (effect.<action>), one comparator, one value."""

    field: CheckFieldPath


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
    objective_quote: str = Field(
        max_length=MAX_OBJECTIVE_QUOTE,
        description="A verbatim substring, at most 200 characters, copied exactly from the packet's objective: "
                    "the part of it this advice serves. Not a paraphrase and not a target inferred from a reader")
    continue_if: list[str] = Field(
        description="What S1 can inspect that would show this advice still serves the objective. Each entry names "
                    "an observable, not a code condition; write unknown and say what the packet does not show "
                    "rather than invent one")
    replan_if: list[str] = Field(
        description="What S1 can inspect that would show this approach no longer serves the objective, so S1 may "
                    "replan. Each entry names an observable, not a code condition; write unknown rather than "
                    "invent one")


class HypothesisOut(Strict):
    confirm_if: str
    contradicting_seq: list[int]
    id: str = Field(description="An open hypothesis id, or one of new_hypothesis_ids")
    refute_if: str
    statement: str
    status: Literal["open", "supported", "contradicted", "withdrawn"]
    supporting_seq: list[int]
    what_one_would_expect: str = Field(
        description="What someone playing this game would expect in the situation this hypothesis is about, judged from "
                    "the game as it presents itself and any declared values, never from calibration on this build; "
                    "then whether the measurement meets it, falls short of it or leaves it unknown, with the numbers")


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
    repro_check: ReproCheckOut | None = Field(
        description="What a replay from a reset must show to reproduce it: false right after the reset, true once "
                    "the bug shows. null when no field can show it. An effect.<action> field is the net "
                    "displacement of one execution of that action; it must not hold for the expected displacement "
                    "the manifest lists for it")
    severity: Literal["blocker", "major", "minor", "cosmetic", "unknown"]
    severity_reason: str
    steps: list[str] = Field(max_length=MAX_STEPS)
    title: str
    uncertainty: str


class FrontierAnswer(Strict):
    """Muse emits keys in sorted order: add_choices, candidate_finding, directive, hypotheses, prune_choices."""

    add_choices: list[AddChoice] = Field(max_length=MAX_ADD)
    candidate_finding: FindingOut | None
    directive: DirectiveOut | None = Field(
        description="null requests no replacement: the current directive is neither renewed nor extended, and this "
                    "answer may arrive after its deadline")
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
    "refute each. The packet's suspicions are every suspicion raised so far in this run; account for each in a "
    "hypothesis (open, supported or contradicted) or a candidate_finding.\n"
    "- directive: one controlled test for S1, or null to request no replacement. null never renews, extends or "
    "restarts the current directive: it keeps its own deadline, which may already have passed when your answer "
    "arrives. The packet's directive row states expires_in_game_s, the game seconds left at as_of, so read it "
    "before you decide to keep or replace; null there means the deadline or the clock is unknown, never zero. "
    "Its expected setpoint must "
    "be checkable by code: a manifest field, an operator, and a value of that field's type. Every operator "
    "except changed and unchanged is met the first time a reading satisfies it, so the setpoint must not "
    "already hold in the packet's observation; one that does is rejected. changed and unchanged take value null "
    "and compare with the value when S1 receives the directive. changed is met when the value first differs from "
    "it. unchanged is met only if the value stays as it is until the deadline, and it needs deadline_s. A "
    "tolerance applies only to numeric == or !=. If no field can show the outcome, set expected to null; the "
    "directive is then scored unknown. deadline_s is game seconds S1 has after it receives the directive.\n"
    "- objective attribution: every directive you write carries objective_quote, continue_if and replan_if. "
    "objective_quote is copied verbatim, at most 200 characters, from the packet's objective: the part of it your "
    "advice serves. It must be an exact substring of that objective string, not a paraphrase, not a target or "
    "reader alias, and not a goal you infer from the evidence; a directive rejected for any other reason is not "
    "salvaged by it. A prerequisite the objective needs first, such as recovering the play session, still quotes "
    "the part of the objective it serves and explains that relationship in your text. continue_if and replan_if "
    "list what S1 can inspect that would show the advice still serves the objective, or no longer does. Name "
    "observables S1 and the packet can check, never a predicate code evaluates, and never a threshold, timer, "
    "score or action to take. If the packet does not show what would settle it, write unknown and say what is "
    "missing; do not invent a condition. These fields are your advice, shown to S1 as written: no code branch "
    "acts on them, they never extend the deadline or request help, and S1 alone decides what to do.\n"
    "- add_choices: manifest actions or skills S1 should be able to pick during this directive, optionally with "
    "a hold or axis override for an action. prune_choices: manifest actions or skills to hide during this "
    "directive. Both end when the directive closes. S1 always has wait, escalate and finish; they are not manifest "
    "choices.\n"
    "- exploration targets: the clip is your only view of the level, and it covers only the few seconds its "
    "descriptor states. When it shows a part of the level (an object, a surface or a region of terrain) that the "
    "recorded actions do not appear to have reached or used, you may add a hypothesis about it and a directive that "
    "tests that hypothesis by sending S1 to try the part. This adds to your answer and never replaces accounting "
    f"for each suspicion. An answer lists at most {MAX_HYPOTHESES} hypotheses, and one you leave out stays open as "
    "recorded, so an unchanged one need not be restated to make room. Ground the part in the clip: say what it "
    "shows and where in the view it appears, in the directive's text, which S1 reads, and in its target (kind "
    "location). Say unknown for what the clip and the recent rows do not settle: its distance, its size, whether it "
    "can be reached or used, whether an earlier action tried it. The recent rows are a bounded window (the "
    "history_window row says how many it omits), so a part with no attempt shown there may have been tried before. "
    "With no clip attached, or nothing you can make out in it, propose none. The hypothesis says what an attempt "
    "would show: its confirm_if and refute_if name readings, or the effect.<action> displacement of an offered "
    "action against the expected value the manifest lists, that tell the outcomes apart once S1 has tried. The "
    "directive's text proposes, in one or two sentences, how S1 can go and try the part: choices from the "
    "packet's choices, named by id, to turn toward it, approach it and then use it. Give deadline_s enough game "
    "seconds for several turns. Set expected only to a field the attempt itself would change; a field any ordinary "
    "movement changes tests nothing, so then set it to null. What the clip shows is a reason to try a part, not "
    "evidence about it, so it supports no candidate_finding by itself.\n"
    "- navigation: a directive that moves S1 keeps it on surface the packet shows it can stand on. The clip shows "
    "what is in view, not how far the level goes: a wide open stretch may end at an edge with nothing beyond it, "
    "and one step past that edge loses the run, so never send S1 toward, across or out over ground whose "
    "connection to where S1 stands the clip does not show. Approach a part from the side S1 is already on, and "
    "when the recent rows show it beside the structure it reached, have it try that structure from where it "
    "stands rather than back away for a wider view, which spends the approach. Line up before moving: turn until "
    "the part sits in the centre of the view, then move at it, and after each move turn back onto it while the "
    "part drifts toward the edge of the view, because a heading that only passes beside the part spends the "
    "approach. Turn steps have fixed sizes and need not land on the centre: when one step overshoots it and the "
    "step back undershoots it, have S1 take the smaller step that lands nearest and move then, because two turns "
    "that undo each other leave S1 where it started.\n"
    "- repeated attempts: directive_history lists the directives that have closed in this run, each with its "
    "outcome, and the recent rows carry each attempt's measured effect. Read both before you issue one, because "
    "an outcome of unknown only says no field could score the directive, not that the attempt worked. A "
    "direction has not succeeded when any of these holds: a closed directive carries not_met or expired; its "
    "error_class says S1 could not attempt it as written; the recent rows show the action it asked for measured "
    "short of the expected displacement the manifest lists for that action, or the change it tested never "
    "appeared; or an earlier directive already asked for the same action at the same part. In each case the next "
    "directive must differ in at least one way you can name in its text: the part or the side of the structure it "
    "tries, the choice sequence it asks S1 for, or the expectation it tests. Trying another part of the same "
    "structure is a different approach; asking for more of the same action at the same place is not, and neither "
    "is restating the earlier text with a longer deadline or a firmer tone. When the packet leaves you no "
    "different approach you can name, say in the directive which part you cannot reach and from where, rather "
    "than repeating the last attempt. This bounds your directive, not S1: S1 may still choose the same action, "
    "and a hypothesis is not contradicted merely because the directive that tested it closed not_met.\n"
    "- expectation of play: every hypothesis states what_one_would_expect: what someone playing this game would "
    "expect, judged from the game as it presents itself (what its structures, controls and other parts are "
    "evidently built for) and from any declared values, never from calibration: calibration measured this build, so "
    "a bug on it is already inside that measurement. 'It matches calibration' and 'it is an ordinary collision or "
    "transient' explain a reading; they do not say whether someone playing would accept it. When the measurement "
    "falls short of the expectation (a control that has none of its effect where someone playing expects it to "
    "work, movement they expect to make that stops, an effect "
    "that appears in one situation and not in a like one), do not settle the hypothesis as normal: propose a "
    "candidate_finding with expected_source interpretation, cite the seqs, put the expectation in expected, and say "
    "in uncertainty why the game might still intend it. A replay tests every candidate, so a wrong one costs a "
    "replay; a missed one is lost.\n"
    "- candidate_finding: only when cited evidence already shows behaviour that contradicts an expectation; "
    "otherwise null. Say where the expectation comes from and what is still uncertain. repro_check is what a "
    "replay must show. A replay starts from a reset, and a check that already holds right after the reset cannot "
    "show the bug, so it never counts: an unchanged check always holds then, and so does a value the game shows "
    "at rest, such as a speed of zero. Choose a check only the bug makes true, or null if no field can show it. "
    "When an action stops having its calibrated effect, no reader need differ from rest, so the check may name "
    "effect.<action> from the manifest's effects: the net displacement of one execution of that action, in world "
    "units, with <, <=, > or >= and a number. The check must not hold for the expected displacement listed there. "
    "The packet's findings are the candidate findings already recorded in this run; do not propose one of them "
    "again.\n"
    "- A field's calibration, in the manifest, says how it changed when preflight sampled it on this same build "
    "before play. Calibration says whether a field changed on this build, not whether the change is right. A "
    "field's source names the game field it reads: that identifies the field and does not say how it should "
    "behave. A field's preflight_note and the alias in its path are the preflight model's inference about the "
    "field, and the manifest marks the note unverified. However obvious they look, what they suggest is a "
    "possible interpretation, not a confirmed expectation of the game. A reading in the facts that departs from "
    "what a note or name suggests (a countdown, fraction or flag that runs past the bounds the note gives it, or "
    "keeps moving after the event it describes has ended) is a hypothesis, not yet a contradicted expectation: "
    "the game may depart from the note, or the note may be wrong, so use confirm_if and refute_if to say what "
    "would tell the two apart. Neither calibration having seen the field change nor S1 having escalated about "
    "something else excuses such a reading; weigh it. If you propose a candidate_finding from it, give "
    "expected_source interpretation (a note or name is never an invariant or a developer_assertion) and cite the "
    "observation seq. Say in uncertainty that the expectation rests on a note or name nothing has verified, and "
    "give severity unknown unless something besides that note or name supports a severity.\n"
    "Observations are measured; null means unknown, not zero."
)

# Added to the system prompt only when the manifest declares reach regions, so a manifest with none sends
# exactly the prompt it always did.
DECLARED_SYSTEM = (
    "\n- declared configuration: declared_configuration lists values the game's authors set (speeds, limits, sizes, "
    "counts, permission flags) as discovery read them in play before any input, each with the component and field "
    "it came from. The value is measured; what it governs is your reading of its name, its type and the values "
    "beside it, so state that reading. It is the closest thing the packet holds to the authors' intent, and "
    "stronger than a reader's preflight note. Weigh measurements against the values they bear on, with the "
    "arithmetic: a speed against an action's displacement per second of hold; a limit (a height, distance, angle "
    "or count) against a measured surface, gap, slope or repetition the entity failed or managed to cross; a "
    "permission flag against an attempt that produced none of its effect. When a measurement falls outside what a "
    "declared value says should happen and nothing measured explains the difference, a candidate_finding may give "
    "expected_source developer_assertion: quote the field and value in expected, show the arithmetic in actual, and "
    "say in uncertainty what your reading of the field assumes."
)


PROGRESS_SYSTEM = (
    "\n- declared reach regions: a progress row is an operator's declaration of an entry volume, not a "
    "measurement and not a verified target. Its author, source and verification status say where the "
    "declaration came from; nothing has checked the place it describes. The run measures only the declared "
    "pose source (the probe anchor's world position) at the packet's observation and derives "
    "progress.<region_id>.distance in world units and progress.<region_id>.inside. Null means unknown, never "
    "zero and never success. A small distance or membership does not show the objective was completed, that a "
    "particular object was contacted, or that any ascent or defect test was performed. Only the distance of a "
    "region this manifest declares is a setpoint field; inside is shown but is not a setpoint field, and a "
    "region the manifest does not declare has no field at all."
)


# --- run and packet context ----------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    axis: bool = False  # True when the action drives an axis control
    max_hold_s: float | None = None  # None: holds cannot be overridden


@dataclass(frozen=True)
class Effect:
    """What calibration measured for one action: its net displacement, and how far a measurement may differ.

    The `effect.<action>` field a repro_check may name. Shown to the model so a check can be chosen that the
    expected displacement does not satisfy; a replay compares a measurement with it, and nothing here judges.
    """

    expected: float  # net displacement of one execution, as calibrated on this build
    allowance: float | None = None  # how far a measurement may differ before calibration's spread no longer accounts for it
    unit: str = "world units"


@dataclass(frozen=True)
class ProgressRegion:
    """One manifest-declared reach volume, as the frontier is shown it.

    This is the operator's declaration, not a measurement. The run derives a distance from the declared
    measured pose; the declaration's meaning and any task completion remain unverified.
    """

    region_id: str
    label: str
    purpose: str
    frame: str
    scene: str
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]
    pose: str
    author: str
    source: str
    verification: str

    @property
    def distance_field(self) -> str:
        return f"progress.{self.region_id}.distance"

    @property
    def inside_field(self) -> str:
        return f"progress.{self.region_id}.inside"


@dataclass(frozen=True)
class Manifest:
    """What a run's frontier schema and checks need from the manifest. Stable for the whole run."""

    fields: Mapping[str, ValueKind]  # observation path -> kind, e.g. "public.<alias>" -> "bool"
    actions: Mapping[str, ActionSpec]
    skills: Collection[str]
    # Observation path -> when preflight calibration saw the field change on this build ("input", "idle", "both"
    # or "no_change_observed"); a field calibration could not classify is absent. Shown to the model, never scored.
    calibration: Mapping[str, str] = field(default_factory=dict, compare=False)
    # Observation path -> what the manifest says about the field: "source" (the game field it reads, as the game
    # reflected it) and "preflight_note" (the preflight model's own reason for choosing the field: an inference
    # nothing has verified, which the manifest message labels as one). Shown, never scored.
    field_notes: Mapping[str, Mapping[str, str]] = field(default_factory=dict, compare=False)
    # Action name -> its calibrated net displacement. Each is a repro_check field, `effect.<action>`; a directive
    # setpoint may not name one.
    effects: Mapping[str, Effect] = field(default_factory=dict)
    # Region id -> its operator declaration. Only declarations the manifest actually contains reach the model,
    # the schema's setpoint fields and the packet. Empty for a manifest that declared none.
    progress: Mapping[str, ProgressRegion] = field(default_factory=dict, compare=False)

    declared: Mapping[str, Any] | None = field(default=None, compare=False)

    def enums(self) -> dict[str, list[str]]:
        return {"fields": list(self.fields), "manifest_names": [*self.actions, *self.skills],
                "check_fields": [*self.fields, *(f"{EFFECT_PREFIX}{name}" for name in sorted(self.effects))]}


@dataclass(frozen=True)
class PacketRefs:
    """What one escalation packet makes citable. Changes every call, so it is never in the schema.

    `objective` is the exact objective string the packet carried. `review` checks a directive's
    objective_quote against it character for character; the check never rewrites the quote or the objective.
    """

    seqs: frozenset[int]
    objective: str
    hypotheses: frozenset[str] = frozenset()
    new_hypothesis_ids: tuple[str, ...] = ()
    open_directive: bool = False
    # The request observation's public readings, as the packet shows them. `review` rejects a reach setpoint
    # they already satisfy. None when the caller has none: nothing is then judged to hold. A mapping is not
    # hashable, so it stays out of the generated eq and hash.
    public: Mapping[str, Any] | None = field(default=None, compare=False)
    # Derived readings at the request observation, by field path (`progress.<id>.distance`). A derived field
    # absent here was not measured, so it cannot be judged to hold. Declared progress fields only.
    derived: Mapping[str, Any] | None = field(default=None, compare=False)


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

    def repro(item: str, sp: ReproCheckOut) -> None:
        """A reader check is judged like a setpoint. An effect check names an action with a calibrated
        displacement and compares it with `<`, `<=`, `>` or `>=` and a number: the schema lists only such
        actions, but a decoded answer is a plain string, so it is checked here."""
        action = effect_action(sp.field)
        if action is None:
            setpoint(item, sp)
            return
        if action not in manifest.effects:
            bad(item, f"{sp.field!r} names {action!r}, which has no calibrated expected displacement in the manifest")
        for reason in check_problems(item, "number", sp.op, sp.value, sp.tolerance):
            bad(item, reason)
        if sp.op not in ORDERING:
            bad(item, f"an effect check compares with <, <=, > or >=, not '{sp.op.value}'")
        if isinstance(sp.value, float) and not math.isfinite(sp.value):
            bad(item, "an effect check compares with a finite number")

    def holds_now(sp: Setpoint) -> bool:
        """A reach setpoint the request observation already satisfies is met before S1 can act, so it tests nothing.

        Only a measured, scoreable reading counts: an unknown one (None) is not a reason to reject.
        """
        kind = manifest.fields.get(sp.field)
        if kind is None or sp.op in CHANGE:
            return False
        if refs.derived is not None and sp.field in refs.derived:
            value = refs.derived[sp.field]
        elif refs.public is not None:
            value = field_value(refs.public, sp.field.removeprefix("public."))
        else:
            return False
        return evaluate(value, kind=kind, op=sp.op, expected=sp.value, tolerance=sp.tolerance) is True

    def text(item: str, **values: str) -> None:
        for name, value in values.items():
            if not value.strip():
                bad(item, f"{name} is empty")

    def attribution(item: str, d: DirectiveOut) -> None:
        """The frontier's own objective attribution, checked against this packet's exact objective.

        A quote that is not a substring is rejected as written: it is never trimmed, repaired, matched
        loosely or replaced with a target. The conditions are only checked for being present and non-empty;
        their meaning is advice for S1, so no code reads them, ranks them or turns one into a condition it
        evaluates. "unknown" is an accepted entry precisely because a condition the packet cannot support
        must stay unknown rather than be invented.
        """
        quote = d.objective_quote
        if not quote.strip():
            bad(item, "objective_quote is empty")
        elif len(quote) > MAX_OBJECTIVE_QUOTE:
            bad(item, f"objective_quote is {len(quote)} characters; the limit is {MAX_OBJECTIVE_QUOTE}")
        elif quote not in refs.objective:
            bad(item, "objective_quote is not a verbatim substring of the packet's objective")
        for name, conditions in (("continue_if", d.continue_if), ("replan_if", d.replan_if)):
            if not conditions:
                bad(item, f"{name} is empty; say unknown explicitly instead of omitting it")
            elif any(not condition.strip() for condition in conditions):
                bad(item, f"{name} has an empty entry")

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
        attribution("directive", d)
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
            repro("candidate_finding", f.repro_check)
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
    # The attribution is carried exactly as the frontier wrote it. Nothing here fills in a missing quote or
    # condition, and nothing derives one from the target, the reader or the evidence.
    return Directive(directive_id=directive_id, parent=parent, model=model, based_on_seq=based_on_seq, text=d.text,
                     target=target, expected=expected, tolerance=tolerance, deadline_game_s=deadline,
                     objective_quote=d.objective_quote, continue_if=list(d.continue_if),
                     replan_if=list(d.replan_if))


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
