"""Offline guards for the paid Mammoth Modal pilot."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "probes"))
from mammoth_modal_calls import schedule, valid
from mammoth_modal_inputs import canonical, encode_body, make_bodies, read_context, sha
from mammoth_image_correctness_calls import schedule as image_schedule


def context():
    return {"instructions": "choose", "choices": [{"id": "a", "label": "A"},
                                                    {"id": "b", "label": "B"}]}


def test_image_must_match_recorded_view_and_preserves_state():
    source = b"jpeg bytes"
    state = {**context(), "game_view_status": "sent", "game_view_sha256": sha(source)}
    flide, reference = make_bodies(state, source)
    assert flide["state"] == {"state": state, "images": ["<image:1>"]}
    assert reference["state"] == state
    assert flide["questions"] == reference["questions"]
    sent_flide = json.loads(encode_body(flide, flide_image=True))
    sent_reference = json.loads(encode_body(reference))
    native_reference = {"state": sent_reference["state"], "images": ["<image:1>"]}
    assert json.dumps(sent_flide["state"], ensure_ascii=False, indent=2) == json.dumps(
        native_reference, ensure_ascii=False, indent=2)
    with pytest.raises(ValueError):
        make_bodies(state, b"different image")
    with pytest.raises(ValueError):
        make_bodies(state, None)


def test_trace_context_hash_mismatch_is_refused(tmp_path):
    row = {"kind": "context", "turn": 1, "body": {"payload": context(), "sha256": "wrong"}}
    trace = tmp_path / "trace.jsonl"
    trace.write_bytes(canonical(row) + b"\n")
    with pytest.raises(ValueError, match="hash"):
        read_context(trace, 1)
    row["body"]["sha256"] = sha(canonical(row["body"]["payload"]))
    trace.write_bytes(canonical(row) + b"\n")
    assert read_context(trace, 1)[0] == context()


def test_fixed_schedule_and_no_repair_of_invalid_answer():
    calls = schedule()
    assert len(calls) == 15 and sum(warmup for _, warmup in calls) == 3
    assert all(sum(name == item and not warmup for name, warmup in calls) == 3
               for item in ("short-text", "long-text", "short-image", "long-image"))
    good = {"answers": {"next_action": {"choice": "a", "probabilities": {"a": 0.6, "b": 0.4}}}}
    assert valid(good, {"a", "b"})
    good["answers"]["next_action"]["choice"] = "missing"
    assert not valid(good, {"a", "b"})
    good["answers"]["next_action"]["choice"] = "a"
    good["answers"]["next_action"]["probabilities"]["b"] = float("nan")
    assert not valid(good, {"a", "b"})


def test_image_correctness_schedule_is_paired_and_bounded():
    calls = image_schedule()
    assert len(calls) == 15 and sum(warmup for _, warmup in calls) == 3
    assert all(sum(name == case and not warmup for name, warmup in calls) == 3
               for case in ("short-real", "short-black", "long-real", "long-black"))
