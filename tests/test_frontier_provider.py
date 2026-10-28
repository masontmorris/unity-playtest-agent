"""Muse frontier transport: one stable request, clip binding, strict decode, measured cost (#33).

The transport never retries, never substitutes an answer and never puts a credential or a local
path in what it returns. A semantic problem is returned on its own item; a malformed or incomplete
answer is rejected whole.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.error
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, ChoiceSource, TurnOutcome, TurnRecord, WhilePending
from playtest.core.context import canonical
from playtest.frontier import provider
from playtest.frontier.packet import Packet, build_packet
from playtest.roles import frontier, strict


def manifest() -> frontier.Manifest:
    fields = {
        "public.counter": "number",
        "public.flag": "bool",
        "scene": "string",
    }
    return frontier.Manifest(
        fields=fields,
        actions={
            "move": frontier.ActionSpec(axis=True, max_hold_s=3.0),
            "use": frontier.ActionSpec(),
        },
        skills=["reset"],
    )


def observation(**overrides) -> Observation:
    values = {
        "session": "run-1",
        "seq": 10,
        "game_s": 12.5,
        "wall_utc": datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc),
        "clock_mode": ClockMode.realtime,
        "scene": "scene-a",
        "playing": True,
        "paused": False,
        "time_scale": 1.0,
        "frame": 100,
        "public": {"counter": 2, "flag": True},
        "ui": {"panel": None},
        "reader_errors": [],
    }
    values.update(overrides)
    return Observation(**values)


def choice_set(**overrides) -> ChoiceSet:
    values = {
        "set_version": 3,
        "built_from_seq": 10,
        "choices": [
            Choice(
                id="c1",
                kind=ChoiceKind.action,
                label="Move",
                action="move",
                params={"hold_s": 1.0},
                source=ChoiceSource.manifest,
            ),
            Choice(id="c2", kind=ChoiceKind.wait, label="Wait", source=ChoiceSource.core),
            Choice(
                id="c3",
                kind=ChoiceKind.escalate,
                label="Ask for help",
                role="frontier",
                source=ChoiceSource.core,
            ),
        ],
    }
    values.update(overrides)
    return ChoiceSet(**values)


def escalation(**overrides) -> TurnRecord:
    values = {
        "turn": 4,
        "set_version": 3,
        "based_on_seq": 10,
        "raw_choice": "c3",
        "effective_choice": "c3",
        "executed_action": None,
        "outcome": TurnOutcome.escalated,
        "while_pending": WhilePending(mode="wait"),
        "policy_default": False,
        "latency_ms": 8.0,
    }
    values.update(overrides)
    return TurnRecord(**values)


def sample(**overrides) -> Packet:
    values = {
        "objective": "Find a state that disagrees with the stated expectation.",
        "observation": observation(),
        "choices": choice_set(),
        "escalation": escalation(),
        "facts": [{"text": "flag changed after the action", "seqs": [9, 10]}],
        "recent": [{"seq": 9, "public": {"flag": False}}],
        "directive": None,
        "hypotheses": (),
        "new_hypothesis_ids": ["h2"],
        "age_s": 0.25,
        "clip": None,
    }
    values.update(overrides)
    return build_packet(**values)


def descriptor(path: Path) -> dict:
    data = path.read_bytes()
    return {
        "from_seq": 9,
        "to_seq": 10,
        "seconds": 1.0,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "mime": "video/mp4",
    }


EMPTY_ANSWER = {
    "add_choices": [],
    "candidate_finding": None,
    "directive": None,
    "hypotheses": [],
    "prune_choices": [],
}


def response(content, *, finish="stop", usage=None, refusal=None, model="muse-spark-1.3"):
    message = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    return {
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    }


# --- request -------------------------------------------------------------------------------------


@pytest.mark.parametrize("field, value", [("deadline_s", "2.0"), ("deadline_s", True)])
def test_schema_types_are_not_silently_coerced(field, value):
    directive = {"deadline_s": None, "expected": None, "target": None, "tests": None, "text": "Observe."}
    directive[field] = value
    result = provider.decode_response(response(json.dumps({**EMPTY_ANSWER, "directive": directive})),
                                      manifest(), sample())
    assert result.answer is None and result.review is None and result.errors


def test_request_has_the_verbatim_prompt_stable_manifest_and_run_schema():
    body = provider.build_request(manifest(), sample())
    assert body["model"] == "muse-spark-1.3"
    assert provider.MODEL == "muse-spark-1.3"
    assert provider.URL == "https://api.meta.ai/v1/chat/completions"
    assert body["messages"][0] == {"role": "system", "content": frontier.SYSTEM}
    assert body["messages"][1]["content"] == provider.manifest_message(manifest())
    assert body["messages"][2]["content"] == sample().canonical()
    assert json.loads(body["messages"][2]["content"])["objective"].startswith("Find a state")
    assert body["response_format"] == strict.response_format("frontier_answer", frontier.schema(manifest()))
    assert body["reasoning_effort"] == "minimal"
    assert body["max_tokens"] == 4096


def test_manifest_message_lists_fields_action_bounds_and_skills_without_order_dependence():
    text = provider.manifest_message(manifest())
    value = json.loads(text)
    assert value["fields"] == [
        {"path": "public.counter", "kind": "number"},
        {"path": "public.flag", "kind": "bool"},
        {"path": "scene", "kind": "string"},
    ]
    assert value["actions"] == [
        {"name": "move", "axis": True, "max_hold_s": 3.0},
        {"name": "use", "axis": False, "max_hold_s": None},
    ]
    assert value["skills"] == ["reset"]
    reordered = frontier.Manifest(
        fields=dict(reversed(list(manifest().fields.items()))),
        actions=dict(reversed(list(manifest().actions.items()))),
        skills={"reset"},
    )
    assert provider.manifest_message(reordered) == text


def test_schema_and_manifest_message_are_stable_while_the_packet_changes():
    first = provider.build_request(manifest(), sample())
    second = provider.build_request(
        manifest(),
        sample(objective="A different objective.", age_s=None, facts=[], recent=[{"seq": 11}]),
    )
    assert first["messages"][:2] == second["messages"][:2]
    assert canonical(first["response_format"]) == canonical(second["response_format"])
    assert first["messages"][2] != second["messages"][2]


def test_a_clip_is_attached_only_when_the_packet_descriptor_and_file_agree(tmp_path):
    path = tmp_path / "clip.mp4"
    data = b"\x00\x00\x00\x18ftypmp42sample-mp4-bytes"
    path.write_bytes(data)
    clipped = sample(clip=descriptor(path))
    body = provider.build_request(manifest(), clipped, clip_path=path)

    content = body["messages"][2]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": clipped.canonical()}
    assert content[1]["type"] == "video_url"
    url = content[1]["video_url"]["url"]
    assert url.startswith("data:video/mp4;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == data
    text = canonical(body)
    assert str(path) not in text
    assert str(tmp_path) not in text
    assert path.name not in text


def test_a_clip_file_without_a_packet_descriptor_is_refused(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not-attached")
    with pytest.raises(ValueError, match="declares no clip"):
        provider.build_request(manifest(), sample(), clip_path=path)


def test_a_packet_clip_without_a_file_is_refused(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"attached")
    clipped = sample(clip=descriptor(path))
    with pytest.raises(ValueError, match="no clip file"):
        provider.build_request(manifest(), clipped)


def test_a_clip_that_does_not_match_its_descriptor_is_refused(tmp_path):
    path = tmp_path / "clip.mp4"
    data = b"measured-clip-bytes"
    path.write_bytes(data)
    clipped = sample(clip=descriptor(path))
    with pytest.raises(ValueError, match="size"):
        provider.build_request(
            manifest(), replace(clipped, payload={**clipped.payload, "clip": {**descriptor(path), "bytes": len(data) + 1}}),
            clip_path=path,
        )
    with pytest.raises(ValueError, match="sha256"):
        provider.build_request(
            manifest(), replace(clipped, payload={**clipped.payload, "clip": {**descriptor(path), "sha256": "0" * 64}}),
            clip_path=path,
        )
    with pytest.raises(ValueError, match="MP4"):
        provider.build_request(
            manifest(), replace(clipped, payload={**clipped.payload, "clip": {**descriptor(path), "mime": "video/webm"}}),
            clip_path=path,
        )
    with pytest.raises(ValueError, match="sha256"):
        provider.build_request(
            manifest(),
            replace(
                clipped,
                payload={**clipped.payload, "clip": {"from_seq": 9, "to_seq": 10, "seconds": 1.0}},
            ),
            clip_path=path,
        )


def test_redacted_media_keeps_size_and_hash_but_no_base64(tmp_path):
    path = tmp_path / "clip.mp4"
    data = b"\x00\x00\x00\x18ftypmp42redact-me"
    path.write_bytes(data)
    body = provider.build_request(manifest(), sample(clip=descriptor(path)), clip_path=path)
    redacted = provider.redact_media(body)

    text = canonical(redacted)
    assert "base64" not in text
    assert base64.b64encode(data).decode("ascii") not in text
    assert str(path) not in text
    media = redacted["messages"][2]["content"][1]["video_url"]
    assert media == {
        "redacted": True,
        "mime": "video/mp4",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert "data:video/mp4;base64," in canonical(body)


def test_max_tokens_must_be_positive():
    assert provider.build_request(manifest(), sample(), max_tokens=123)["max_tokens"] == 123
    with pytest.raises(ValueError, match="max_tokens"):
        provider.build_request(manifest(), sample(), max_tokens=0)


# --- decoding ------------------------------------------------------------------------------------


def test_a_valid_answer_keeps_its_model_usage_and_item_review():
    body = response(json.dumps(EMPTY_ANSWER), usage={"prompt_tokens": 10, "completion_tokens": 2})
    result = provider.decode_response(body, manifest(), sample())
    assert result.errors == []
    assert isinstance(result.answer, frontier.FrontierAnswer)
    assert result.review is not None and result.review.clean
    assert result.resolved_model == "muse-spark-1.3"
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 2}


COST_CASES = [
    (
        "cached_discount",
        {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 400}},
        pytest.approx((600 * 1.25 + 400 * 0.15 + 200 * 4.25) / 1_000_000),
    ),
    (
        "zero_cached_is_measured",
        {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 0}},
        pytest.approx((1000 * 1.25 + 200 * 4.25) / 1_000_000),
    ),
    (
        "no_cached_details",
        {"prompt_tokens": 1000, "completion_tokens": 200},
        None,
    ),
    (
        "cached_missing",
        {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {}},
        None,
    ),
    (
        "cached_over_prompt",
        {"prompt_tokens": 100, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 101}},
        None,
    ),
    (
        "prompt_not_numeric",
        {"prompt_tokens": "1000", "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 0}},
        None,
    ),
    (
        "completion_bool",
        {"prompt_tokens": 1000, "completion_tokens": True, "prompt_tokens_details": {"cached_tokens": 0}},
        None,
    ),
    (
        "cached_not_numeric",
        {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": "many"}},
        None,
    ),
    (
        "prompt_missing",
        {"completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 0}},
        None,
    ),
    (
        "prompt_nonfinite",
        {"prompt_tokens": float("nan"), "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 0}},
        None,
    ),
]


@pytest.mark.parametrize(
    "usage, expected",
    [(usage, expected) for _, usage, expected in COST_CASES],
    ids=[name for name, _, _ in COST_CASES],
)
def test_cost_comes_only_from_validated_token_counts(usage, expected):
    result = provider.decode_response(response(json.dumps(EMPTY_ANSWER), usage=usage), manifest(), sample())
    assert result.answer is not None
    if expected is None:
        assert result.cost_usd is None
    else:
        assert result.cost_usd == expected


def test_non_object_usage_leaves_cost_unknown_without_discarding_the_answer():
    result = provider.decode_response(response(json.dumps(EMPTY_ANSWER), usage="not-an-object"), manifest(), sample())
    assert result.answer is not None
    assert result.usage is None
    assert result.cost_usd is None


MALFORMED = [
    ("no_response", None, "not a JSON object"),
    ("not_an_object", "text", "not a JSON object"),
    ("no_choices", {}, "no choices"),
    ("empty_choices", {"choices": []}, "no choices"),
    ("error_envelope", {"error": {"message": "secret server text"}}, "reports an error"),
    ("choice_not_object", {"choices": [[]]}, "not an object"),
    ("no_message", {"choices": [{"finish_reason": "stop"}]}, "no message"),
    ("refusal", response(json.dumps(EMPTY_ANSWER), refusal="I cannot help"), "refused"),
    ("truncated", response(json.dumps(EMPTY_ANSWER), finish="length"), "not 'stop'"),
    ("missing_finish", response(json.dumps(EMPTY_ANSWER), finish=None), "not 'stop'"),
    ("not_text", response(None), "not text"),
    ("list_content", response([{"type": "text", "text": "x"}]), "not text"),
    ("not_json", response("not json"), "not JSON"),
    ("empty_object", response("{}"), "add_choices"),
    ("extra_field", response(json.dumps(EMPTY_ANSWER | {"unexpected": 1})), "unexpected"),
]


@pytest.mark.parametrize(
    "data, needle",
    [(data, needle) for _, data, needle in MALFORMED],
    ids=[name for name, _, _ in MALFORMED],
)
def test_malformed_refused_or_truncated_answers_are_rejected_without_repair(data, needle):
    result = provider.decode_response(data, manifest(), sample())
    assert result.answer is None
    assert result.review is None
    assert result.errors
    assert any(needle in error for error in result.errors)
    assert "secret server text" not in " ".join(result.errors)


def test_semantic_problems_are_isolated_on_their_items_and_returned():
    content = json.dumps(
        {
            "add_choices": [
                {"name": "not_in_manifest", "label": "Unknown action", "hold_s": None, "x": None, "y": None}
            ],
            "candidate_finding": None,
            "directive": {
                "text": "Test the flag.",
                "tests": "h2",
                "target": None,
                "expected": {"field": "public.not_a_field", "op": "==", "value": True, "tolerance": None},
                "deadline_s": 5.0,
            },
            "hypotheses": [
                {
                    "id": "h2",
                    "statement": "the flag follows the action",
                    "status": "open",
                    "supporting_seq": [],
                    "contradicting_seq": [],
                    "confirm_if": "it changes again",
                    "refute_if": "it never changes",
                }
            ],
            "prune_choices": [{"name": "not_a_skill", "reason": "not needed"}],
        }
    )
    result = provider.decode_response(response(content), manifest(), sample())
    assert result.errors == []
    assert result.answer is not None
    assert result.review is not None
    assert [problem.item for problem in result.review.problems] == [
        "directive",
        "add_choices[0]",
        "prune_choices[0]",
    ]
    assert not result.review.clean
    assert result.answer.directive is not None


def test_unknown_dynamic_enum_values_are_review_problems_not_parse_errors():
    content = json.dumps(
        EMPTY_ANSWER
        | {
            "add_choices": [
                {"name": "not_in_manifest", "label": "Unknown action", "hold_s": None, "x": None, "y": None}
            ]
        }
    )
    result = provider.decode_response(response(content), manifest(), sample())
    assert result.errors == []
    assert result.answer is not None
    assert result.review is not None
    assert {problem.item for problem in result.review.problems} == {"add_choices[0]"}
    assert any("not a manifest action or skill" in problem.reason for problem in result.review.problems)


def test_schema_shape_errors_never_return_a_partial_answer():
    broken = dict(EMPTY_ANSWER)
    broken["hypotheses"] = [
        {
            "id": "h2",
            "statement": "incomplete item",
            "status": "open",
            "supporting_seq": [],
            "contradicting_seq": [],
            "confirm_if": "x",
            "refute_if": "y",
            "extra": True,
        }
    ]
    result = provider.decode_response(response(json.dumps(broken)), manifest(), sample())
    assert result.answer is None
    assert result.review is None
    assert result.errors


def test_a_nonfinite_answer_is_rejected_as_invalid_json():
    content = json.dumps(
        EMPTY_ANSWER
        | {
            "directive": {
                "text": "Test the counter.",
                "tests": None,
                "target": None,
                "expected": {
                    "field": "public.counter",
                    "op": "==",
                    "value": float("nan"),
                    "tolerance": None,
                },
                "deadline_s": 5.0,
            }
        }
    )
    assert "NaN" in content
    result = provider.decode_response(response(content), manifest(), sample())
    assert result.answer is None
    assert result.review is None
    assert any("finite JSON" in error for error in result.errors)


# --- transport -----------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_send_meta_posts_once_with_the_env_credential(monkeypatch):
    secret = "sk-test-secret-value"
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append((request, timeout))
        return FakeResponse(200, b'{"ok": true}')

    monkeypatch.setenv("META_API_KEY", secret)
    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    body = {"model": "m", "messages": []}
    status, data, latency, problem = provider.send_meta(body)

    assert (status, data, problem) == (200, {"ok": True}, None)
    assert isinstance(latency, float) and latency >= 0
    assert len(calls) == 1
    request, timeout = calls[0]
    assert timeout == 60
    assert request.full_url == provider.URL
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {secret}"
    assert request.get_header("Content-type") == "application/json"
    assert request.data == canonical(body).encode("utf-8")
    assert secret not in repr((status, data, latency, problem))


def test_send_meta_does_not_read_or_return_an_error_body(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        raise urllib.error.HTTPError(
            provider.URL, 429, "Too Many Requests", None, io.BytesIO(b'{"error":{"message":"secret"}}')
        )

    monkeypatch.setenv("META_API_KEY", "sk-test-secret-value")
    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    status, data, latency, problem = provider.send_meta({"messages": []})
    assert status == 429
    assert data is None
    assert problem == "HTTP error"
    assert isinstance(latency, float)
    assert "secret" not in (problem or "")
    assert len(calls) == 1


def test_send_meta_reports_transport_and_malformed_responses(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "sk-test-secret-value")
    replies = iter([
        b"not json",
        b"[]",
        b'{"value": NaN}',
    ])
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return FakeResponse(200, next(replies))

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    first = provider.send_meta({"messages": []})
    assert first[0] == 200 and first[1] is None and first[3] == "response was not JSON"
    second = provider.send_meta({"messages": []})
    assert second[0] == 200 and second[1] is None and second[3] == "response was not a JSON object"
    third = provider.send_meta({"messages": []})
    assert third[0] == 200 and third[1] is None and third[3] == "response was not JSON"
    assert len(calls) == 3

    def broken_urlopen(request, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(provider.urllib.request, "urlopen", broken_urlopen)
    status, data, latency, problem = provider.send_meta({"messages": []})
    assert (status, data, problem) == (None, None, "transport error")
    assert isinstance(latency, float)


def test_send_meta_requires_the_environment_credential(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    with pytest.raises(ValueError, match="META_API_KEY"):
        provider.send_meta({"messages": []})


def test_send_meta_rejects_a_non_object_body(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "sk-test-secret-value")
    with pytest.raises(ValueError, match="non-empty object"):
        provider.send_meta([])
