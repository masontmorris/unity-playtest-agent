"""The opt-in state order: the same values, stable-first keys, and today's bytes by default.

Nothing here asserts that a provider reads one order better than another: that is unmeasured and the
paid comparison is designed, not run, in `docs/design/s1-opt-state-order.md`. These tests pin the three
properties the option must have: with it off the request is the one the adapter always sent; with it on
the sent text parses back to exactly the same object; and the recorded hash is the hash of the bytes
sent, which a reader can rebuild from the traced payload.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from loop_fakes import choices as core_choices
from s1_fakes import Send, approval, reply
from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Comparator, Directive, DirectiveOutcome, Expected
from playtest.contracts.loop import ChoiceSet
from playtest.core.context import build_context, canonical
from playtest.runner import request_layout
from playtest.runner.request_layout import RequestLayout
from playtest.runner.s1 import SystemOneS1
from playtest.runner.state_order import STATE_ORDER, StateOrder, ordered_text, state_keys
from test_runner_s1 import context

ARM = "liquid-d1-ultra"
META = "meta-muse-spark-s1"


def primitive(seq: int = 7) -> dict[str, Any]:
    """The payload this adapter is usually given: the required keys and nothing optional."""
    return context()


def rich() -> dict[str, Any]:
    """A payload with every optional key `build_context` can add, built by `build_context` itself."""
    observation = Observation(session="s", seq=7, game_s=3.0, wall_utc=datetime(2026, 10, 28, tzinfo=timezone.utc),
                              clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False,
                              time_scale=1.0, frame=9, public={"gauge": 2.5}, ui={"canvases": []})
    expected = Expected(field="public.gauge", op=Comparator.ge, value=2)
    open_directive = Directive(directive_id="d-2", model="m", based_on_seq=6, text="Keep looking.",
                               expected=expected, outcome=DirectiveOutcome.open)
    closed = Directive(directive_id="d-1", model="m", based_on_seq=5, text="Look first.",
                       expected=expected, outcome=DirectiveOutcome.not_met)
    return build_context(
        objective="Exercise the controls.", observation=observation,
        choices=ChoiceSet(set_version=4, built_from_seq=7, choices=core_choices()),
        facts=["measured: a hold moved the entity 2.0 units", "measured: null means unknown"],
        directive=open_directive, directive_history=[closed], pending_help={"helper": "frontier"},
        age_s=0.25, sample_gap_s=0.5, game_view="the attached view",
        guidance=[{"item": "inspect the thing"}], recent_history={"turns": [{"turn": 1}]},
        directive_provenance={"d-2": {"requested": {"turn": 1}}, "d-1": {"requested": {"turn": 0}}},
        reset_summary={"episodes": 2},
    )


def body_of(payload: dict[str, Any], question: str = "next_action") -> dict[str, Any]:
    return {"model": "d1-ultra",
            "questions": {question: {"type": "choice", "instructions": payload["instructions"],
                                     "criteria": {item["id"]: item["label"] for item in payload["choices"]}}},
            "state": payload}


def state_key_order(text: str) -> list[str]:
    """The `state` object's key order in a JSON text, read from the text alone."""
    pairs = json.loads(text, object_pairs_hook=lambda items: items)
    return [name for name, _ in dict(pairs)["state"]]


def adapter(tmp_path: Path, *replies: Any, arm: str = ARM, **options: Any) -> tuple[SystemOneS1, Send]:
    limits = approval()
    limits["arms"] = {arm: limits["arms"]["mammoth-1"]}
    ledger = call_budget.Ledger(tmp_path, limits, arm)
    send = Send(tmp_path, *replies)
    return SystemOneS1(ledger, max_calls=10, send=send, arm=arm, **options), send


# --------------------------------------------------------------------------- default is today's body
def test_the_default_hands_the_sender_a_body_and_hashes_it_as_before(tmp_path: Path):
    port, send = adapter(tmp_path, reply("next_action", "c1"))
    payload = primitive()
    got = asyncio.run(port.decide(payload))
    assert port.state_order == "canonical" and StateOrder(port.state_order) is StateOrder.canonical
    # The sender still serializes the body itself, exactly as the bench and the live runner always did.
    assert send.texts == [None] and isinstance(send.bodies[0], dict)
    assert send.bodies[0]["state"] == payload
    assert got.calls[0].request_sha256 == digest(canonical(send.bodies[0]))


def test_naming_canonical_explicitly_sends_the_same_bytes_as_the_default(tmp_path: Path):
    sent = []
    for name, options in (("default", {}), ("canonical", {"state_order": "canonical"}),
                          ("enum", {"state_order": StateOrder.canonical})):
        port, send = adapter(tmp_path / name, reply("next_action", "c1"), **options)
        got = asyncio.run(port.decide(primitive()))
        sent.append((got.calls[0].request_sha256, canonical(send.bodies[0]), send.texts))
    assert sent[0] == sent[1] == sent[2]
    assert all(texts == [None] for _, _, texts in sent)


def test_the_default_body_is_still_the_bytes_this_adapter_sent_before_the_option():
    """Pinned: the fixture body's canonical text and the payload's own hash must not move."""
    payload = primitive()
    body = body_of(payload)
    assert digest(canonical(body)) == "30837cd47cb3bbaaaf7da84306aaad85fbbbf3dd112bf2fc83ad3b5ee79e1d87"
    assert digest(canonical(payload)) == "4487deb71e909edd91adede337dad658cfa8d38f8d5a24bace0f12645b9749bf"
    # And the option, on, does not touch the payload's own bytes: only the request's state order.
    assert digest(canonical(json.loads(ordered_text(body)))) == digest(canonical(body))


# --------------------------------------------------------------------------- the ordered text
@pytest.mark.parametrize("payload", [primitive(), rich()])
def test_the_ordered_text_parses_back_to_the_identical_object(payload: dict[str, Any]):
    body = body_of(payload)
    text = ordered_text(body)
    assert json.loads(text) == json.loads(canonical(body))
    assert canonical(json.loads(text)) == canonical(body)
    # Same characters, so nothing was added, dropped, doubled or rewritten; only the order moved.
    assert sorted(text) == sorted(canonical(body)) and len(text) == len(canonical(body))


@pytest.mark.parametrize("payload", [primitive(), rich()])
def test_every_state_key_is_present_once_in_the_declared_order(payload: dict[str, Any]):
    order = state_key_order(ordered_text(body_of(payload)))
    assert order == [key for key in STATE_ORDER if key in payload]
    assert sorted(order) == sorted(payload) and len(order) == len(payload)


def test_the_values_sit_where_the_stable_first_rule_puts_them():
    payload = rich()
    order = state_key_order(ordered_text(body_of(payload)))
    # Stable first, the turn's news nearest the question: choices before the version that names it,
    # the observation and the open help request after every fact.
    assert order.index("instructions") < order.index("objective") < order.index("choices")
    assert order.index("choices") < order.index("choice_set_version") < order.index("facts")
    assert order[-2:] == ["observation", "pending_help"]
    assert order.index("as_of") < order.index("observation")
    assert order.index("game_view") < order.index("observation")


def test_a_key_the_order_does_not_name_is_still_sent_once_and_goes_last():
    payload = primitive()
    payload["aaa_unexpected"] = {"kept": True}
    order = state_key_order(ordered_text(body_of(payload)))
    assert order[-1] == "aaa_unexpected" and len(order) == len(payload)
    assert json.loads(ordered_text(body_of(payload))) == json.loads(canonical(body_of(payload)))


def test_state_keys_keeps_a_key_the_order_does_not_name():
    assert state_keys({"zzz": 1, "facts": []})[-1] == "zzz"
    assert state_keys({"facts": [], "b": 2, "a": 1}) == ("facts", "a", "b")


def test_a_body_whose_state_is_not_an_object_is_rendered_as_canonical_would():
    body = {"model": "d1-ultra", "state": ["not", "an", "object"]}
    assert ordered_text(body) == canonical(body)


def test_the_question_and_the_transport_keep_their_own_key_order():
    body = body_of(rich())
    body["images"] = ["data:image/jpeg;base64,AAAA"]
    text = ordered_text(body)
    # The outer body still follows the canonical (sorted) order, so `images` stays first, as today.
    assert text.startswith('{"images":["data:image/jpeg;base64,AAAA"],"model":"d1-ultra",'
                           '"questions":{"next_action":{')
    assert json.loads(text) == json.loads(canonical(body))


# --------------------------------------------------------------------------- the wire and the trace
def test_the_adapter_sends_the_ordered_text_and_hashes_those_exact_bytes(tmp_path: Path):
    port, send = adapter(tmp_path, reply("next_action", "c1"), state_order="stable-first/1")
    payload = rich()
    untouched = copy.deepcopy(payload)
    got = asyncio.run(port.decide(payload))
    text = send.texts[0]
    assert port.state_order == "stable-first/1" and isinstance(text, str)
    assert got.calls[0].request_sha256 == digest(text)          # the hash covers the bytes sent
    assert send.bodies[0] == json.loads(canonical(body_of(payload)))
    # The payload the context record carries, plus the question, rebuilds the sent text exactly.
    assert ordered_text(send.bodies[0]) == text
    # And the order is visible to a reader: the two candidate serializations hash differently.
    assert got.calls[0].request_sha256 != digest(canonical(send.bodies[0]))
    assert payload == untouched and digest(canonical(payload)) == digest(canonical(untouched))


def test_a_follow_up_call_sends_the_same_state_in_the_same_order(tmp_path: Path):
    port, send = adapter(tmp_path, reply("next_action", "h"), reply("while_pending", "w"),
                         state_order="stable-first/1")
    got = asyncio.run(port.decide(primitive()))
    assert [keys for keys in (state_key_order(text) for text in send.texts)] == [
        [key for key in STATE_ORDER if key in primitive()]] * 2
    assert got.answer["while_pending"] == {"mode": "wait"}
    assert [call.request_sha256 for call in got.calls] == [digest(text) for text in send.texts]


def test_with_a_view_the_image_is_attached_and_the_ordered_text_binds_it(tmp_path: Path):
    import base64

    raw = b"sample jpeg bytes"
    payload = primitive()
    payload.update(game_view="the attached view", game_view_sha256=digest(raw), game_view_status="sent")
    port, send = adapter(tmp_path, reply("next_action", "c1"), game_view=True, state_order="stable-first/1")
    got = asyncio.run(port.decide(payload, image=raw))
    text = send.texts[0]
    url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    assert text.startswith(f'{{"images":["{url}"]')                 # the image leads, as it does today
    assert got.calls[0].request_sha256 == digest(text)
    assert list(json.loads(text)["state"]) == [key for key in STATE_ORDER if key in payload]


def test_the_layout_and_the_order_compose(tmp_path: Path):
    payload = rich()
    port, send = adapter(tmp_path, reply("next_action", "c1"),
                         layout=RequestLayout.duplicate_state_reference, state_order="stable-first/1")
    got = asyncio.run(port.decide(payload))
    text = send.texts[0]
    assert got.calls[0].request_sha256 == digest(text)
    sent = json.loads(text)
    # The layout removes the text it already states in the question, so `instructions` is the one key
    # absent here; the marker is a key the order does not name, so it is carried last and appears once.
    assert "instructions" not in sent["state"] and request_layout.MARKER in sent["state"]
    assert state_key_order(text) == [key for key in STATE_ORDER if key in sent["state"]] + [request_layout.MARKER]
    assert sent["state"][request_layout.MARKER]["version"] == request_layout.VERSION
    restored = request_layout.decode(json.loads(text))
    assert restored["state"] == payload and json.loads(canonical(restored)) == json.loads(canonical(body_of(payload)))


# --------------------------------------------------------------------------- refusals
def test_the_meta_arm_keeps_its_own_order_and_refuses_this_one(tmp_path: Path):
    limits = approval()
    limits["arms"][META] = limits["arms"].pop("mammoth-1")
    limits["arms"][META]["per_call_max_usd"] = 0.001
    ledger = call_budget.Ledger(tmp_path, limits, META)
    with pytest.raises(ValueError, match="own stable-first text"):
        SystemOneS1(ledger, max_calls=1, arm=META, state_order="stable-first/1")
    assert not list((tmp_path / "calls").glob("*.json"))         # refused before any call
    # Its default is unchanged: the arm renders its own stable-first user text, not this module's.
    port = SystemOneS1(ledger, max_calls=1, arm=META)
    assert port.state_order == "canonical"


def test_an_unknown_state_order_is_refused(tmp_path: Path):
    ledger = call_budget.Ledger(tmp_path, approval(), "mammoth-1")
    with pytest.raises(ValueError, match="unknown S1 state order"):
        SystemOneS1(ledger, max_calls=1, state_order="stable")
    assert not list((tmp_path / "calls").glob("*.json"))
