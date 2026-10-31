"""The opt-in single-copy request layout: exact round trip, honest failures, no change by default.

The layout removes only a text the same request already states verbatim (one question's instructions and
the labels its criteria already carry), and records where to read it back. These tests use hand-built
bodies and real adapter sends, not recorded game runs.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from loop_fakes import choices
from s1_fakes import Send, approval, reply
from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceSet
from playtest.core.context import INSTRUCTIONS, build_context, canonical
from playtest.runner import request_layout as layout
from playtest.runner.request_layout import MARKER, VERSION, CodecError, RequestLayout
from playtest.runner.s1 import FOLLOW_UP_INSTRUCTIONS, SystemOneS1

PRIMARY = "next_action"
FOLLOW_UP = "while_pending"
PENDING_KINDS = ("wait", "action", "skill")
ARM = "liquid-d1-ultra"


def choice(choice_id: str, kind: str, label: Any, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"id": choice_id, "kind": kind, "label": label, "source": "core"}
    item.update(extra)
    return item


ALL_CHOICES = [
    choice("c1", "action", "Shift left", action="shift", params={"delta": [-1.0, 0.0, 0.0]},
           precondition={"kind": "visible"}),
    choice("c2", "action", "Hold still", action="hold", params={"tag": "primary"}),
    choice("h", "escalate", "Ask for help", role="frontier"),
    choice("d", "delegate", "Hand off a task", role="helper"),
    choice("w", "wait", "Wait"),
    choice("f", "finish", "Finish"),
]


def state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "objective": "Exercise the controls.",
        "instructions": INSTRUCTIONS,
        "as_of": {"seq": 7, "game_s": 3.0},
        "choice_set_version": 4,
        "choices": copy.deepcopy(ALL_CHOICES),
        "facts": ["measured: null means unknown"],
        "pending_help": None,
    }
    base.update(over)
    return base


def criteria_for(payload: dict[str, Any], kinds: tuple[str, ...] | None = None) -> dict[str, str]:
    return {item["id"]: item["label"] for item in payload["choices"]
            if kinds is None or item["kind"] in kinds}


def request(question: str = PRIMARY, *, payload: dict[str, Any] | None = None,
            instructions: str | None = None, criteria: dict[str, str] | None = None,
            **body_extra: Any) -> dict[str, Any]:
    payload = state() if payload is None else payload
    if instructions is None:
        instructions = INSTRUCTIONS if question == PRIMARY else FOLLOW_UP_INSTRUCTIONS
    if criteria is None:
        criteria = criteria_for(payload, None if question == PRIMARY else PENDING_KINDS)
    body: dict[str, Any] = {
        "model": "d1-ultra",
        "questions": {question: {"type": "choice", "instructions": instructions, "criteria": criteria}},
        "state": payload,
    }
    body.update(body_extra)
    return body


def roundtrip(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Encode, serialise to JSON text, parse it back, decode: nothing shared but the bytes."""
    encoded = layout.encode(body)
    reloaded = json.loads(canonical(encoded))
    decoded = layout.decode(reloaded)
    assert canonical(decoded) == canonical(body)
    return encoded, decoded


def rehash(encoded: dict[str, Any]) -> None:
    """Point the marker's digest at the mutated body, so only the reference checks can reject it."""
    stripped = copy.deepcopy(encoded)
    stripped["state"].pop(MARKER)
    encoded["state"][MARKER]["body_sha256"] = digest(canonical(stripped))


# --------------------------------------------------------------------------- exactness
def test_a_primary_body_carries_each_duplicated_text_once():
    body = request()
    encoded, decoded = roundtrip(body)
    question = encoded["questions"][PRIMARY]
    # The selected question's own text is untouched.
    assert question == body["questions"][PRIMARY]
    assert "instructions" not in encoded["state"]
    assert all("label" not in item for item in encoded["state"]["choices"])
    marker = encoded["state"][MARKER]
    assert marker["version"] == VERSION and marker["question"] == PRIMARY
    assert marker["instructions_removed"] is True
    assert marker["labels_removed"] == [item["id"] for item in body["state"]["choices"]]
    # Every other part of every choice is still there, including null-vs-missing detail.
    for supplied, sent in zip(body["state"]["choices"], encoded["state"]["choices"]):
        assert {key: value for key, value in supplied.items() if key != "label"} == sent
    # The body sent is smaller than the full one, and decode gives the full one back.
    assert len(canonical(encoded)) < len(canonical(body))
    assert decoded == body and decoded is not body


def test_a_follow_up_keeps_the_labels_its_criteria_do_not_carry():
    payload = state()
    body = request(FOLLOW_UP, payload=payload)
    encoded, _ = roundtrip(body)
    sent = {item["id"]: item for item in encoded["state"]["choices"]}
    for item in payload["choices"]:
        if item["kind"] in PENDING_KINDS:
            assert "label" not in sent[item["id"]]
        else:  # escalate, delegate, finish: not offered while help is pending, so the label stays
            assert sent[item["id"]]["label"] == item["label"]
    assert sorted(encoded["state"][MARKER]["labels_removed"]) == ["c1", "c2", "w"]


def test_a_label_that_differs_from_its_criteria_is_kept_inline():
    payload = state()
    body = request(criteria={item["id"]: item["label"].upper() for item in payload["choices"]})
    encoded, _ = roundtrip(body)
    assert [item["label"] for item in encoded["state"]["choices"]] == [item["label"] for item in payload["choices"]]
    # Only the instructions duplicate was exact, so only that one is referenced.
    assert encoded["state"][MARKER]["labels_removed"] == []
    assert encoded["state"][MARKER]["instructions_removed"] is True


def test_non_ascii_text_round_trips_byte_for_byte():
    payload = state(instructions=INSTRUCTIONS + " Übergröße — naïve café ✓")
    payload["choices"][0]["label"] = "Avancer → côté gauche"
    body = request(payload=payload, instructions=payload["instructions"])
    assert body["questions"][PRIMARY]["instructions"] == payload["instructions"]
    encoded, decoded = roundtrip(body)
    assert canonical(encoded).count("Avancer") == 1
    assert decoded["state"]["choices"][0]["label"] == "Avancer → côté gauche"
    assert json.loads(canonical(encoded))["state"][MARKER]["labels_removed"] == ["c1", "c2", "h", "d", "w", "f"]


def test_null_stays_null_and_a_missing_key_stays_missing():
    payload = state(instructions=None)
    payload["choices"][0]["label"] = None
    payload["choices"][1].pop("label")  # missing, not null
    body = request(payload=payload, criteria={"c2": "Hold still", "w": "Wait"})
    encoded, decoded = roundtrip(body)
    assert encoded["state"]["instructions"] is None and encoded["state"][MARKER]["instructions_removed"] is False
    assert encoded["state"]["choices"][0]["label"] is None
    assert "label" not in encoded["state"]["choices"][1]
    assert encoded["state"][MARKER]["labels_removed"] == ["w"]
    assert decoded["state"]["instructions"] is None and "instructions" in decoded["state"]
    assert decoded["state"]["choices"][0]["label"] is None and "label" not in decoded["state"]["choices"][1]


def test_unknown_keys_and_extra_metadata_are_preserved():
    payload = state(extra_reader={"unit": "m", "value": None})
    payload["observation"] = {"seq": 7, "public": {"a": 1}, "unknown": [1, None, "ü"]}
    body = request(payload=payload, trace={"note": None})
    body["state"]["choices"][0]["precondition"] = None
    encoded, _ = roundtrip(body)
    assert encoded["trace"] == {"note": None}
    assert encoded["state"]["observation"] == payload["observation"]
    assert encoded["state"]["choices"][0]["precondition"] is None
    assert encoded["state"]["extra_reader"] == {"unit": "m", "value": None}


def test_encoding_is_deterministic_and_leaves_the_input_untouched():
    body = request()
    before = copy.deepcopy(body)
    first, second = layout.encode(body), layout.encode(body)
    assert canonical(first) == canonical(second)
    assert body == before and MARKER not in body["state"]
    # A body without a duplicate is sent as it was; decoding it is the identity.
    plain = request(criteria={}, instructions="different wording")
    assert canonical(layout.encode(plain)) == canonical(plain)
    assert layout.decode(json.loads(canonical(plain))) == plain


def test_the_body_sha_binds_the_encoded_bytes_only_through_its_own_marker():
    body = request()
    encoded = layout.encode(body)
    assert encoded["state"][MARKER]["body_sha256"] == digest(canonical(body))
    stripped = copy.deepcopy(encoded)
    stripped["state"].pop(MARKER)
    assert digest(canonical(stripped)) != encoded["state"][MARKER]["body_sha256"]


# --------------------------------------------------------------------------- refusals
def test_a_changed_referenced_label_is_refused():
    encoded = layout.encode(request())
    encoded["questions"][PRIMARY]["criteria"]["c1"] = "Shift right"
    with pytest.raises(CodecError, match="no longer restores"):
        layout.decode(encoded)


def test_a_missing_selected_choice_is_refused():
    encoded = layout.encode(request())
    encoded["state"]["choices"] = [item for item in encoded["state"]["choices"] if item["id"] != "c1"]
    with pytest.raises(CodecError, match="not in the body"):
        layout.decode(encoded)


def test_a_label_missing_from_the_referenced_criteria_is_refused():
    encoded = layout.encode(request())
    del encoded["questions"][PRIMARY]["criteria"]["c2"]
    rehash(encoded)
    with pytest.raises(CodecError, match="no label for 'c2'"):
        layout.decode(encoded)


def test_a_marker_naming_another_question_is_refused():
    encoded = layout.encode(request())
    encoded["questions"]["other"] = encoded["questions"].pop(PRIMARY)
    with pytest.raises(CodecError, match="only question"):
        layout.decode(encoded)


def test_an_unsupported_version_is_refused():
    encoded = layout.encode(request())
    encoded["state"][MARKER]["version"] = "dup-state-ref/2"
    with pytest.raises(CodecError, match="unsupported layout version"):
        layout.decode(encoded)


@pytest.mark.parametrize("mutate,message", [
    (lambda marker: marker.update(unexpected=1), "unknown fields"),
    (lambda marker: marker.pop("labels_removed"), "missing"),
    (lambda marker: marker.update(instructions_removed=1), "not a boolean"),
    (lambda marker: marker.update(labels_removed=["c1", "c1"]), "twice"),
    (lambda marker: marker.update(labels_removed=["c1", 7]), "list of ids"),
    (lambda marker: marker.update(question=""), "names no question"),
])
def test_a_malformed_marker_is_refused(mutate, message):
    encoded = layout.encode(request())
    mutate(encoded["state"][MARKER])
    with pytest.raises(CodecError, match=message):
        layout.decode(encoded)


def test_a_marker_that_is_not_an_object_is_refused():
    encoded = layout.encode(request())
    encoded["state"][MARKER] = []
    with pytest.raises(CodecError, match="not an object"):
        layout.decode(encoded)


def test_decode_never_reads_a_body_that_was_not_encoded():
    encoded = layout.encode(request())
    encoded["state"]["instructions"] = INSTRUCTIONS  # a second copy reappears: it must not be overwritten
    rehash(encoded)
    with pytest.raises(CodecError, match="still stated"):
        layout.decode(encoded)


def test_encoding_refuses_to_overwrite_a_caller_key():
    body = request()
    body["state"][MARKER] = {"mine": True}
    with pytest.raises(CodecError, match="already carries"):
        layout.encode(body)
    assert body["state"][MARKER] == {"mine": True}


def test_an_unknown_layout_name_is_refused_before_any_call(tmp_path: Path):
    ledger = call_budget.Ledger(tmp_path, approval(), "mammoth-1")
    with pytest.raises(ValueError, match="unknown S1 request layout"):
        SystemOneS1(ledger, max_calls=1, layout="dup-state")
    assert not list((tmp_path / "calls").glob("*.json"))


def test_the_layout_needs_a_plain_state_and_says_so(tmp_path: Path):
    ledger = call_budget.Ledger(tmp_path, approval(), "mammoth-1")
    with pytest.raises(ValueError, match="inside a message"):
        SystemOneS1(ledger, max_calls=1, arm="mammoth-1", layout=RequestLayout.duplicate_state_reference)
    assert not list((tmp_path / "calls").glob("*.json"))


# --------------------------------------------------------------------------- adapter
def context() -> dict[str, Any]:
    obs = Observation(session="s", seq=7, game_s=3.0,
                      wall_utc=datetime(2026, 10, 28, tzinfo=timezone.utc), clock_mode=ClockMode.realtime,
                      scene="a", playing=True, paused=False, time_scale=1.0, frame=9)
    return build_context(objective="Exercise the controls.", observation=obs,
                         choices=ChoiceSet(set_version=4, built_from_seq=7, choices=choices()))


def liquid(tmp_path: Path, *replies: Any, layout_choice: Any = RequestLayout.full):
    limits = approval()
    limits["arms"] = {ARM: limits["arms"]["mammoth-1"]}
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    send = Send(tmp_path, *replies)
    port = SystemOneS1(ledger, max_calls=10, send=send, arm=ARM, game_view=True, layout=layout_choice)
    return port, send


def viewed() -> tuple[dict[str, Any], bytes]:
    raw = b"sample jpeg bytes"
    ctx = context()
    ctx.update(game_view="the attached view", game_view_sha256=digest(raw), game_view_status="sent")
    return ctx, raw


def test_the_default_layout_sends_the_context_exactly_as_before(tmp_path: Path):
    port, send = liquid(tmp_path, reply(PRIMARY, "h"), reply(FOLLOW_UP, "w"))
    ctx, raw = viewed()
    call = asyncio.run(port.decide(ctx, image=raw))
    assert port.layout == "full"
    for body in send.bodies:
        question = next(iter(body["questions"]))
        assert body["state"] == ctx and body["state"] is ctx
        assert set(body) == {"model", "questions", "state", "images"}
        assert body["questions"][question]["instructions"] in (INSTRUCTIONS, FOLLOW_UP_INSTRUCTIONS)
        assert body["questions"][question]["criteria"] == {
            item["id"]: item["label"] for item in ctx["choices"]
            if question == PRIMARY or item["kind"] in PENDING_KINDS}
    assert call.failure is None


def test_the_opt_in_layout_sends_a_body_that_restores_itself(tmp_path: Path):
    port, send = liquid(tmp_path, reply(PRIMARY, "h"), reply(FOLLOW_UP, "w"),
                        layout_choice=RequestLayout.duplicate_state_reference)
    ctx, raw = viewed()
    got = asyncio.run(port.decide(ctx, image=raw))
    assert port.layout == VERSION and got.failure is None and len(send.bodies) == 2
    assert MARKER not in ctx and ctx["instructions"] == INSTRUCTIONS  # the caller's context is untouched
    full_text = 0
    sent_text = 0
    for body, call in zip(send.bodies, got.calls):
        question = next(iter(body["questions"]))
        assert body["images"] == ["data:image/jpeg;base64," + base64.b64encode(raw).decode()]
        assert call.request_sha256 == digest(canonical(body))  # the hash is of the bytes that were sent
        assert body["questions"][question]["instructions"] == (
            INSTRUCTIONS if question == PRIMARY else FOLLOW_UP_INSTRUCTIONS)
        assert body["state"][MARKER]["version"] == VERSION
        full = {"model": body["model"], "questions": copy.deepcopy(body["questions"]),
                "state": copy.deepcopy(ctx), "images": body["images"]}
        assert canonical(layout.decode(json.loads(canonical(body)))) == canonical(full)
        full_text += len(canonical({**full, "images": []}))
        sent_text += len(canonical({**body, "images": []}))
        assert canonical({**body, "images": []}) != canonical({**full, "images": []})
    assert sent_text != full_text  # the marker's own overhead is reported by the local corpus check
    # The follow-up keeps the labels of the choices it could not offer.
    follow = send.bodies[1]["state"]
    assert {item["id"] for item in follow["choices"] if "label" not in item} == {"c1", "c2", "c3", "w"}
    assert {item["label"] for item in follow["choices"] if "label" in item} == {
        "Escalate", "Delegate", "Finish"}


def test_the_opt_in_layout_sends_no_image_and_still_restores(tmp_path: Path):
    port, send = liquid(tmp_path, reply(PRIMARY, "c2"), layout_choice="dup-state-ref/1")
    ctx = context()
    got = asyncio.run(port.decide(ctx))
    body = send.bodies[0]
    assert "images" not in body and body["state"][MARKER]["labels_removed"]
    assert got.answer["choice_id"] == "c2"
    assert layout.decode(json.loads(canonical(body)))["state"] == ctx
