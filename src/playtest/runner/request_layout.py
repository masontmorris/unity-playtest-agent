"""One versioned, lossless request-body layout: send each duplicated text once.

The provider has no memory: every request must carry everything S1 can see. This module never removes
information from a request. It removes a *second copy* of a text that the same request already states
verbatim, and records where the first copy is:

* `state.instructions`, when it is byte-identical to the one question's `instructions`;
* a `state.choices[i].label`, when it is byte-identical to that question's `criteria[id]`.

Every other part of the body is untouched: choice `id`/`kind`/`params`/`action`/`skill`/
`precondition`/`source`, all other keys, and `null` wherever the sender put `null`. When a question's
criteria cover only some kinds (a follow-up covers the choices S1 may pick while help is pending), the
labels of the other choices are not in that criteria and stay inline, so they cannot disappear.

The encoded body carries one marker inside `state` (opaque to the provider) with the layout version,
the readable location to restore from, and the SHA-256 of the body's text that was encoded (everything
but the transport's image array). `decode` reads nothing but the body it is given: it never reads a
file, a corpus or the original context, and it refuses a marker that is malformed, names a choice or
question that is not there, or no longer restores the encoded body exactly (a changed referenced
label, a dropped key, a swapped value).

Nothing here ranks, filters, suppresses or rewrites a choice or a fact, and nothing here chooses.
`encode` is a pure function of the body; `decode(encode(body)) == body`.
"""

from __future__ import annotations

import copy
import hashlib
from enum import Enum
from typing import Any

from playtest.core.context import canonical

MARKER = "_s1_layout"  # inside `state`, which the provider treats as opaque
VERSION = "dup-state-ref/1"
_REQUIRED = frozenset({"version", "rule", "question", "instructions_removed", "labels_removed", "body_sha256"})


class CodecError(ValueError):
    """The body is not one this layout can read back exactly."""


class RequestLayout(str, Enum):
    """How the adapter renders the request body. `full` is the shipped body, byte for byte."""

    full = "full"
    duplicate_state_reference = VERSION


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text_body(body: dict[str, Any]) -> dict[str, Any]:
    """The part of a request this layout encodes: everything but the transport's `images` array.

    The marker's digest binds these bytes only. An image is an opaque data URL this module never reads,
    edits or references, and the adapter may attach it after the body is encoded.
    """
    return {key: value for key, value in body.items() if key != "images"}


def _question(body: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The single question this request asks, or `None` when the shape is not one to reference."""
    questions = body.get("questions")
    if not isinstance(questions, dict) or len(questions) != 1:
        return None
    name = next(iter(questions))
    spec = questions[name]
    if not isinstance(name, str) or not isinstance(spec, dict):
        return None
    return name, spec


def encode(body: dict[str, Any]) -> dict[str, Any]:
    """A copy of `body` with each text the same request states twice sent once, plus its marker.

    Returns an unchanged copy when nothing is duplicated, so a body that cannot be referenced is sent
    exactly as it was. Raises `CodecError` only when `state` already carries this marker: overwriting
    a caller's key would change what S1 sees.
    """
    if not isinstance(body, dict):
        raise CodecError("a request body is a JSON object")
    state = body.get("state")
    if isinstance(state, dict) and MARKER in state:
        raise CodecError(f"the body already carries {MARKER!r}")
    found = _question(body)
    if not isinstance(state, dict) or found is None:
        return copy.deepcopy(body)
    name, spec = found
    encoded = copy.deepcopy(body)
    encoded_state = encoded["state"]
    encoded_spec = encoded["questions"][name]
    instructions_removed = False
    sent = encoded_state.get("instructions")
    if isinstance(sent, str) and encoded_spec.get("instructions") == sent:
        del encoded_state["instructions"]
        instructions_removed = True
    labels_removed: list[str] = []
    criteria = encoded_spec.get("criteria")
    choices = encoded_state.get("choices")
    if isinstance(criteria, dict) and isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            choice_id, label = choice.get("id"), choice.get("label")
            if isinstance(choice_id, str) and isinstance(label, str) and criteria.get(choice_id) == label:
                del choice["label"]
                if choice_id not in labels_removed:  # one reference per id, even if the body repeats it
                    labels_removed.append(choice_id)
    if not instructions_removed and not labels_removed:
        return copy.deepcopy(body)
    encoded_state[MARKER] = {
        "version": VERSION,
        "rule": (f"Only one copy of each duplicated text is sent: restore state.instructions from "
                 f"questions.{name}.instructions and each labels_removed label from questions.{name}.criteria."),
        "question": name,
        "instructions_removed": instructions_removed,
        "labels_removed": labels_removed,
        "body_sha256": _digest(canonical(_text_body(body))),
    }
    return encoded


def decode(body: dict[str, Any]) -> dict[str, Any]:
    """The exact body `encode` was given, restored from `body` alone.

    A body without the marker is returned unchanged: it is already a full request. With the marker,
    every reference is followed inside this body and the result must hash to the encoded body's own
    `body_sha256`; otherwise this raises `CodecError` rather than return something that was not sent.
    """
    if not isinstance(body, dict):
        raise CodecError("a request body is a JSON object")
    state = body.get("state")
    if not isinstance(state, dict) or MARKER not in state:
        return copy.deepcopy(body)
    marker = state[MARKER]
    if not isinstance(marker, dict):
        raise CodecError(f"{MARKER!r} is not an object")
    if missed := _REQUIRED - set(marker):
        raise CodecError(f"{MARKER!r} is missing {sorted(missed)}")
    if extra := set(marker) - _REQUIRED:
        raise CodecError(f"{MARKER!r} carries unknown fields {sorted(extra)}")
    if marker["version"] != VERSION:
        raise CodecError(f"unsupported layout version {marker['version']!r}")
    question = marker["question"]
    if not isinstance(question, str) or not question:
        raise CodecError("the marker names no question")
    questions = body.get("questions")
    if not isinstance(questions, dict) or set(questions) != {question}:
        raise CodecError(f"the marker's question {question!r} is not the request's only question")
    spec = questions[question]
    if not isinstance(spec, dict):
        raise CodecError(f"questions.{question} is not an object")
    decoded = copy.deepcopy(body)
    decoded_state = decoded["state"]
    decoded_spec = decoded["questions"][question]
    decoded_state.pop(MARKER)
    removed = marker["instructions_removed"]
    if not isinstance(removed, bool):
        raise CodecError("instructions_removed is not a boolean")
    if removed:
        if "instructions" in decoded_state:
            raise CodecError("the marker says state.instructions was removed, but it is still stated")
        kept = decoded_spec.get("instructions")
        if not isinstance(kept, str):
            raise CodecError(f"questions.{question}.instructions holds no text to restore")
        decoded_state["instructions"] = kept
    labels = marker["labels_removed"]
    if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
        raise CodecError("labels_removed is not a list of ids")
    if len(set(labels)) != len(labels):
        raise CodecError("labels_removed names an id twice")
    if labels:
        criteria = decoded_spec.get("criteria")
        if not isinstance(criteria, dict):
            raise CodecError(f"questions.{question}.criteria is not an object")
        choices = decoded_state.get("choices")
        if not isinstance(choices, list):
            raise CodecError("the marker says labels were removed, but state.choices is not a list")
        for choice_id in labels:
            matched = [choice for choice in choices
                       if isinstance(choice, dict) and choice.get("id") == choice_id]
            if not matched:
                raise CodecError(f"the marker names a choice that is not in the body: {choice_id!r}")
            if choice_id not in criteria:
                raise CodecError(f"questions.{question}.criteria has no label for {choice_id!r}")
            label = criteria[choice_id]
            if not isinstance(label, str):
                raise CodecError(f"the criteria label for {choice_id!r} is not text")
            for choice in matched:
                if "label" in choice:
                    raise CodecError(f"choice {choice_id!r} still states the label it should have lost")
                choice["label"] = label
    if _digest(canonical(_text_body(decoded))) != marker["body_sha256"]:
        raise CodecError("the body no longer restores what was encoded; a referenced text or key changed")
    return decoded
