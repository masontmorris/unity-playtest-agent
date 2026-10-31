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


def with_effects(**effects) -> frontier.Manifest:
    return replace(manifest(), effects=effects)


def test_manifest_message_lists_each_calibrated_effect_with_its_expected_displacement_and_allowance():
    text = provider.manifest_message(
        with_effects(move=frontier.Effect(expected=6.04, allowance=0.06), use=frontier.Effect(expected=0.0)))
    value = json.loads(text)
    assert value["effects"] == [
        {"path": "effect.move", "kind": "number", "unit": "world units", "expected": 6.04, "allowance": 0.06},
        {"path": "effect.use", "kind": "number", "unit": "world units", "expected": 0.0, "allowance": None},
    ]
    assert "Only a repro_check may name an effects path; a directive setpoint may not." in value["notes"]
    assert ("Its allowance is how far a measurement may differ from it before calibration's recorded spread no "
            "longer accounts for it; null means calibration recorded no spread.") in value["notes"]
    assert provider.manifest_message(with_effects(use=frontier.Effect(expected=0.0), move=frontier.Effect(
        expected=6.04, allowance=0.06))) == text                          # insertion order does not matter


def test_a_manifest_without_effects_is_worded_exactly_as_before():
    value = json.loads(provider.manifest_message(manifest()))
    assert "effects" not in value and "effects row" not in value["notes"]
    assert value["notes"] == provider._MANIFEST_NOTE


def test_a_request_carries_the_effect_rows_and_a_schema_that_offers_them_to_a_finding_only():
    body = provider.build_request(with_effects(move=frontier.Effect(expected=6.04, allowance=0.06)), sample())
    assert json.loads(body["messages"][1]["content"])["effects"][0]["path"] == "effect.move"
    defs = body["response_format"]["json_schema"]["schema"]["$defs"]
    assert "effect.move" in defs["check_fields"]["enum"] and "effect.move" not in defs["fields"]["enum"]


def finding_answer(repro_check) -> str:
    finding = {"actual": "The entity barely moved.", "conditions": "After one move.", "evidence_seq": [10],
               "expected": "A move carries the entity about as far as calibration measured.",
               "expected_source": "interpretation", "repro_check": repro_check, "severity": "major",
               "severity_reason": "The control stops working.", "steps": ["Do the move."], "title": "A move does not move",
               "uncertainty": "One sample."}
    return json.dumps({**EMPTY_ANSWER, "candidate_finding": finding})


def test_a_decoded_effect_check_reviews_clean_for_a_calibrated_action_and_is_reported_for_any_other():
    check = {"field": "effect.move", "op": "<", "tolerance": None, "value": 1.5}
    known = with_effects(move=frontier.Effect(expected=6.04, allowance=0.06))
    result = provider.decode_response(response(finding_answer(check)), known, sample())
    assert result.errors == [] and result.review is not None and result.review.clean
    assert result.answer.candidate_finding.repro_check.field == "effect.move"
    unknown = provider.decode_response(response(finding_answer(check)), manifest(), sample())     # no effects calibrated
    assert unknown.answer is not None and [p.item for p in unknown.review.problems] == ["candidate_finding"]
    assert "has no calibrated expected displacement" in unknown.review.problems[0].reason
    equality = provider.decode_response(response(finding_answer({**check, "op": "==", "value": 0.0})), known, sample())
    assert [p.item for p in equality.review.problems] == ["candidate_finding"]


def test_a_directive_that_names_an_effect_field_is_reported_on_the_directive():
    directive = {"deadline_s": 5.0, "expected": {"field": "effect.move", "op": "<", "tolerance": None, "value": 1.5},
                 "target": None, "tests": None, "text": "Move once.",
                 "objective_quote": "Find a state that disagrees",
                 "continue_if": ["the measured displacement still follows the move"],
                 "replan_if": ["the measured displacement contradicts the move"]}
    known = with_effects(move=frontier.Effect(expected=6.04, allowance=0.06))
    result = provider.decode_response(response(json.dumps({**EMPTY_ANSWER, "directive": directive})), known, sample())
    assert [p.item for p in result.review.problems] == ["directive"]
    assert "'effect.move' is not a manifest field" in result.review.problems[0].reason


# --- what a field row says, and where each statement came from ------------------------------------------------------

NOTE = "Reason R: tracks quantity Q \u2014 a ratio, \"0 to 1\""        # opaque, with characters a serializer could mangle


def noted(fields=None, **parts) -> frontier.Manifest:
    """A manifest over opaque fields whose rows differ only in what was recorded about them."""
    fields = fields or {"public.alpha": "number", "public.beta": "number", "public.gamma": "number", "public.delta": "bool"}
    return frontier.Manifest(fields=fields, actions=manifest().actions, skills=manifest().skills, **parts)


def field_rows(manifest_: frontier.Manifest) -> dict[str, dict]:
    return {row["path"]: row for row in json.loads(provider.manifest_message(manifest_))["fields"]}


def test_a_field_row_keeps_what_was_measured_reflected_and_inferred_under_separate_keys():
    rows = field_rows(noted(
        calibration={"public.alpha": "input", "public.beta": "no_change_observed"},
        field_notes={"public.alpha": {"source": "Owner.m_Alpha", "preflight_note": NOTE},
                     "public.beta": {"source": "Owner.m_Beta"},
                     "public.gamma": {"preflight_note": "Reason G"}}))
    assert rows["public.alpha"] == {
        "path": "public.alpha", "kind": "number",
        "calibration": provider.CALIBRATION_LABELS["input"],                  # measured on this build
        "source": "Owner.m_Alpha",                                            # reflected from the game
        "preflight_note": NOTE,                                               # the preflight model's words, as written
        "preflight_note_origin": "preflight_model_inference",
        "preflight_note_verification": "unverified"}
    # What was not recorded is not there, so it is unknown: no note means no provenance either, and no source or
    # class is filled in from a note.
    assert rows["public.beta"] == {"path": "public.beta", "kind": "number",
                                   "calibration": provider.CALIBRATION_LABELS["no_change_observed"],
                                   "source": "Owner.m_Beta"}
    assert rows["public.gamma"] == {"path": "public.gamma", "kind": "number", "preflight_note": "Reason G",
                                    "preflight_note_origin": "preflight_model_inference",
                                    "preflight_note_verification": "unverified"}
    assert rows["public.delta"] == {"path": "public.delta", "kind": "bool"}


@pytest.mark.parametrize("text", [
    "Counts down to the next event", "Elapsed time since the last event", "Fraction of capacity in use",
    "Set while the effect is active", "Reason R", "a", "x" * 400,
], ids=["countdown", "elapsed", "fraction", "flag", "opaque", "one_character", "long"])
def test_a_note_is_sent_as_recorded_and_labelled_the_same_whatever_it_says(text):
    # Nothing here reads the note: no wording is dropped, softened or trusted more because of what it says.
    row = field_rows(noted(field_notes={"public.alpha": {"preflight_note": text}}))["public.alpha"]
    assert row == {"path": "public.alpha", "kind": "number", "preflight_note": text,
                   "preflight_note_origin": "preflight_model_inference", "preflight_note_verification": "unverified"}


@pytest.mark.parametrize("blank", ["", "  ", "\n", None, 5, ["Reason"]], ids=["empty", "spaces", "newline", "none", "number", "list"])
def test_a_note_that_is_not_text_is_left_out_with_no_provenance_made_up_for_it(blank):
    assert field_rows(noted(field_notes={"public.alpha": {"preflight_note": blank, "source": blank}}))["public.alpha"] == {
        "path": "public.alpha", "kind": "number"}


def test_the_manifest_notes_name_every_key_a_field_row_can_carry():
    full = noted(calibration={"public.alpha": "idle"},
                 field_notes={"public.alpha": {"source": "Owner.m_Alpha", "preflight_note": NOTE}})
    value = json.loads(provider.manifest_message(full))
    carried = {key for row in value["fields"] for key in row} - {"path", "kind"}
    assert carried == {"calibration", "source", "preflight_note", "preflight_note_origin", "preflight_note_verification"}
    assert all(key in value["notes"] for key in carried)                      # the model is told what each one is
    assert "meant to be" not in value["notes"]                                # a note is not a statement of intent


def test_the_prompt_names_the_row_keys_it_reasons_about():
    row = field_rows(noted(calibration={"public.alpha": "idle"},
                           field_notes={"public.alpha": {"source": "Owner.m_Alpha", "preflight_note": NOTE}}))["public.alpha"]
    for key in ("calibration", "source", "preflight_note"):
        assert key in row and key in frontier.SYSTEM


def test_notes_are_laid_out_the_same_whatever_order_they_were_recorded_in():
    def flipped(mapping):
        return dict(reversed(list(mapping.items())))

    calibration = {"public.alpha": "input", "public.beta": "idle"}
    notes = {"public.alpha": {"source": "Owner.m_Alpha", "preflight_note": NOTE},
             "public.beta": {"preflight_note": "Reason B", "source": "Owner.m_Beta"}}
    forward = noted(calibration=calibration, field_notes=notes)
    backward = noted(fields=flipped(forward.fields), calibration=flipped(calibration),
                     field_notes={path: flipped(note) for path, note in flipped(notes).items()})
    assert provider.manifest_message(forward) == provider.manifest_message(backward)


def test_notes_change_what_the_frontier_is_told_about_a_field_and_nothing_it_may_answer_or_choose():
    plain = noted()
    labelled = noted(calibration={"public.alpha": "input"},
                     field_notes={"public.alpha": {"source": "Owner.m_Alpha", "preflight_note": NOTE}})
    before, after = provider.build_request(plain, sample()), provider.build_request(labelled, sample())
    assert before["messages"][1] != after["messages"][1]
    assert before["messages"][0] == after["messages"][0]                      # the system prompt
    assert before["messages"][2] == after["messages"][2]                      # the packet, with the choices S1 had
    assert before["response_format"] == after["response_format"]              # the closed answer schema
    assert canonical(frontier.schema(plain)) == canonical(frontier.schema(labelled))
    assert {key: before[key] for key in before if key != "messages"} == {key: after[key] for key in after if key != "messages"}


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
                "objective_quote": "Find a state that disagrees",
                "continue_if": ["the flag reading still follows the action"],
                "replan_if": ["the flag reading contradicts the action"],
            },
            "hypotheses": [
                {
                    "id": "h2",
                    "statement": "the flag follows the action",
                    "status": "open",
                    "supporting_seq": [],
                    "contradicting_seq": [],
                    "confirm_if": "it changes again",
                    "refute_if": "it never changes", "what_one_would_expect": "unknown",
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
            "refute_if": "y", "what_one_would_expect": "unknown",
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
                "objective_quote": "Find a state that disagrees",
                "continue_if": ["the counter reading still follows the action"],
                "replan_if": ["the counter reading contradicts the action"],
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
