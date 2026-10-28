"""Requests are stable and transport errors never become model answers."""

import base64
import pytest

from playtest.preflight.packet import Packet, Screenshot
from playtest.preflight.provider import CONTRIBUTOR_MODEL, Muse, build_request
from playtest.roles import preflight, strict


def packet(image=None):
    return Packet("packet", preflight.Discovery({"f": "System.Single"}, {"w": "Button"}), {"f": "field"},
                  frozenset(), image)


def messages(p):
    return [{"role": "system", "content": preflight.SYSTEM},
            {"role": "user", "content": preflight.first_turn(p.text, "readers")}]


def test_request_keeps_schema_and_image_fixed_across_stages():
    image = Screenshot(b"\x89PNG\r\n\x1a\nfixture", "image/png", "test capture")
    p = packet(image)
    schema = preflight.schema(p.discovery)
    first = build_request(p, messages(p), schema)
    later = build_request(p, messages(p) + [{"role": "assistant", "content": "{}"},
                                           {"role": "user", "content": preflight.STAGE_PROMPTS["actions"]}], schema)
    assert strict.canonical(first["response_format"]) == strict.canonical(later["response_format"])
    assert first["messages"][1] == later["messages"][1]
    url = first["messages"][1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(url.split(",", 1)[1]) == image.data
    assert first["model"] == "muse-spark-1.3"


def test_valid_response_keeps_usage_and_unknown_cost():
    p = packet()
    response = {"model": "muse-spark-1.3", "usage": {"prompt_tokens": 10},
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    provider = Muse(p, send=lambda _: (200, response, 100.0, None))
    result = provider(messages(p), preflight.schema(p.discovery))
    assert result.content == "{}" and result.error is None
    assert result.metadata["resolved_model"] == "muse-spark-1.3"
    assert result.metadata["usage"] == {"prompt_tokens": 10}
    assert result.metadata["estimated_cost_usd"] is None
    assert result.metadata["reported_cost_usd"] is None


def test_transport_refusal_and_truncation_are_not_answers():
    p = packet()
    schema = preflight.schema(p.discovery)
    cases = [
        (None, None, 2.0, "timeout"),
        (200, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, 2.0, None),
        (200, {"choices": [{"finish_reason": "stop", "message": {"refusal": "no"}}]}, 2.0, None),
        (200, {"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]}, 2.0, None),
        (200, {"choices": [{"finish_reason": "tool_calls", "message": {"content": ""}}]}, 2.0, None),
    ]
    for reply, kind in zip(cases, ("transport", "truncated", "refusal", "refusal", "provider")):
        result = Muse(p, send=lambda _, reply=reply: reply)(messages(p), schema)
        assert result.content is None and result.error
        assert result.error_kind == kind
        assert result.metadata["estimated_cost_usd"] is None


def test_mismatched_discovery_schema_is_rejected_before_transport():
    p = packet()
    other = preflight.Discovery({"other": "int"}, {"w": "Button"})
    sent = []
    client = Muse(p, send=lambda request: sent.append(request))
    with pytest.raises(ValueError, match="schema does not match"):
        client(messages(p), preflight.schema(other))
    assert sent == []


def test_contributor_is_explicit_and_priced_as_contributor():
    p = packet()
    schema = preflight.schema(p.discovery)
    requests = []
    response = {"model": CONTRIBUTOR_MODEL, "usage": {"prompt_tokens": 1000, "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 500}},
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    def send(body):
        requests.append(body)
        return 200, response, 1.0, None
    result = Muse(p, send=send, model=CONTRIBUTOR_MODEL)(messages(p), schema)
    assert requests[0]["model"] == CONTRIBUTOR_MODEL
    assert result.metadata["model"] == CONTRIBUTOR_MODEL
    assert result.metadata["estimated_cost_usd"] == pytest.approx(0.000071)
