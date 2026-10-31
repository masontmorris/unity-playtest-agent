"""The payload S1 receives: facts, the observation they came from, and the choices.

The turn loop builds one of these per turn and hands it to the provider port. Everything here is
measured or declared elsewhere: facts are strings written by monitors and directive scoring, the
observation is the reading the choice set was bound to, and the choices are the manifest's. This
module never drops, reorders, ranks or rewrites any of them, and it never adds a recommendation
(build bible section 6; S1 ownership rule 3).

`as_of` carries the age the caller measured. A missing age stays `null`; it is never inferred from
wall time, because the game clock can be paused or stepped.

`pending_help` is always present: `null` when no help request is open, an object when one is.
`guidance` is present only once a helper's result has been adopted: a list of the adopted items, or
the mapping a helper laid them out as (the caller has checked that it loses none of them). `recent_history`
is present only when supplied. A payload built without either keeps its exact bytes (frozen corpora
hash their payloads); a mapping and the history are copied so later changes cannot alter the request.

`directive` is the open directive and `directive_history` the closed ones, each with the outcome it ended
with. `directive_provenance` is present only when the caller supplies what its source measured about those
directives (by directive id): where each was asked and first stated, and its age against this observation.
It may name only a directive the payload shows. A directive is a helper's advice, so its outcome scores a
setpoint over a reader and nothing more; nothing here says a goal was reached.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import Directive
from playtest.contracts.loop import ChoiceSet

INSTRUCTIONS = (
    "This is one decision turn. Choose exactly one id from choices. Make an explicit destination or "
    "test in the objective the primary task; explore controls and investigate as needed to support it. "
    "If the objective is broad exploration, pursue that instead. Choose an offered choice that advances "
    "the objective or resolves an uncertainty blocking it. When applicable, orient toward the intended "
    "part, approach it, try the interaction, and inspect the next evidence. Reassess a failed approach "
    "or ask for help as you judge appropriate. Finish when the objective's completion conditions are met "
    "or you judge no useful attempt remains. Elapsed time, a single input finishing, or a reader setpoint "
    "met alone does not demonstrate task completion. Directive text is the frontier's advice from an earlier "
    "observation; directive_provenance, when provided, identifies that observation and the advice's age. "
    "Weigh it against current observations and images; its description may be outdated. "
    "Facts are measured; null means unknown, never zero. "
    "Only the listed choices exist. On an escalate or delegate turn, also say what to do while help is "
    "pending. When an image is present, it is the current game view."
)


def build_context(
    *,
    objective: str,
    observation: Observation,
    choices: ChoiceSet,
    facts: Sequence[str] = (),
    directive: Directive | None = None,
    directive_history: Sequence[Directive] = (),
    pending_help: Mapping[str, Any] | None = None,
    age_s: float | None = None,
    sample_gap_s: float | None = None,
    game_view: str | None = None,
    guidance: Sequence[Mapping[str, Any]] | Mapping[str, Any] = (),
    recent_history: Mapping[str, Any] | None = None,
    directive_provenance: Mapping[str, Mapping[str, Any]] | None = None,
    reset_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the S1 request payload without changing what S1 is allowed to see.

    Raises `ValueError` when the choices were built from a different reading than the observation,
    because a choice set that is not bound to its observation cannot be executed safely, and when
    `directive_provenance` names a directive the payload does not show or is not finite JSON.
    """
    if not objective.strip():
        raise ValueError("the objective is empty")
    if choices.built_from_seq != observation.seq:
        raise ValueError(
            f"choices are bound to seq {choices.built_from_seq}, not the observation's {observation.seq}"
        )
    if age_s is not None and age_s < 0:
        raise ValueError("observation age cannot be negative")

    payload: dict[str, Any] = {
        "objective": objective,
        "as_of": {
            "seq": observation.seq,
            "game_s": observation.game_s,
            "age_s": age_s,
            "sample_gap_s": sample_gap_s,
        },
        "observation": {
            "scene": observation.scene,
            "playing": observation.playing,
            "paused": observation.paused,
            "time_scale": observation.time_scale,
            "public": observation.public,
            "ui": observation.ui,
            "reader_errors": [error.model_dump(mode="json") for error in observation.reader_errors],
        },
        "facts": list(facts),
        "directive": _dump(directive),
        "directive_history": [_dump(item) for item in directive_history],
        "pending_help": dict(pending_help) if pending_help is not None else None,
        "choice_set_version": choices.set_version,
        "choices": [choice.model_dump(mode="json", exclude_none=True) for choice in choices.choices],
        "instructions": INSTRUCTIONS,
        "game_view": game_view,
    }
    if guidance:
        payload["guidance"] = (copy.deepcopy(dict(guidance)) if isinstance(guidance, Mapping)
                               else [dict(item) for item in guidance])
    if recent_history is not None:
        payload["recent_history"] = copy.deepcopy(dict(recent_history))
    if directive_provenance:
        payload["directive_provenance"] = _provenance(directive_provenance, directive, directive_history)
    if reset_summary is not None:
        payload["reset_summary"] = copy.deepcopy(dict(reset_summary))
    return payload


def canonical(payload: Mapping[str, Any]) -> str:
    """Stable text for hashing and caching: same payload, same bytes, wherever it was built."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _dump(directive: Directive | None) -> dict[str, Any] | None:
    return None if directive is None else directive.model_dump(mode="json", exclude_none=True)


def _provenance(provenance: Mapping[str, Mapping[str, Any]], directive: Directive | None,
                directive_history: Sequence[Directive]) -> dict[str, Any]:
    """An independent copy of what a source measured about each directive shown.

    A directive the payload does not show cannot have provenance here: that would attach one directive's
    origin to another's text. Values pass through as the source gave them; a missing one stays `null`.
    """
    shown = {item.directive_id for item in (directive, *directive_history) if item is not None}
    stray = [key for key in provenance if key not in shown]
    if stray:
        raise ValueError(f"directive provenance names {stray}, which the payload does not show")
    try:
        return json.loads(json.dumps({key: dict(value) for key, value in provenance.items()}, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"directive provenance is not finite JSON: {exc}") from None
