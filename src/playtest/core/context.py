"""The payload S1 receives: facts, the observation they came from, and the choices.

The turn loop builds one of these per turn and hands it to the provider port. Everything here is
measured or declared elsewhere: facts are strings written by monitors and directive scoring, the
observation is the reading the choice set was bound to, and the choices are the manifest's. This
module never drops, reorders, ranks or rewrites any of them, and it never adds a recommendation
(build bible section 6; S1 ownership rule 3).

`as_of` carries the age the caller measured. A missing age stays `null`; it is never inferred from
wall time, because the game clock can be paused or stepped.

`pending_help` is always present: `null` when no help request is open, an object when one is.
`guidance` is present only once a helper's result has been adopted, so a payload built without any
keeps its exact bytes (frozen corpora hash their payloads). Each entry carries its source and age.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import Directive
from playtest.contracts.loop import ChoiceSet

INSTRUCTIONS = (
    "This is one decision turn. Choose exactly one id from choices. Facts are measured; null means "
    "unknown, never zero. Only the listed choices exist. On an escalate or delegate turn, also say "
    "what to do while help is pending. When an image is present, it is the current game view."
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
    guidance: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the S1 request payload without changing what S1 is allowed to see.

    Raises `ValueError` when the choices were built from a different reading than the observation,
    because a choice set that is not bound to its observation cannot be executed safely.
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
        payload["guidance"] = [dict(item) for item in guidance]
    return payload


def canonical(payload: Mapping[str, Any]) -> str:
    """Stable text for hashing and caching: same payload, same bytes, wherever it was built."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _dump(directive: Directive | None) -> dict[str, Any] | None:
    return None if directive is None else directive.model_dump(mode="json", exclude_none=True)
