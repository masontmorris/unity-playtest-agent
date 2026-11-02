"""A packet the model cannot read is projected once the offer budget or the context needs it (#42).

The offer bounds what the model may choose; these tests bound what it is shown. A packet inside
both limits must be exactly what it was before the context rule existed, and every row that leaves
a projected packet must remain in the sidecar the packet names.
"""

import hashlib
import json

import pytest

from playtest.preflight import manifest, packet as pack, stages
from test_preflight_offer_budget import cap, evidence, make

# The packet sections a packet inside both limits carried before the context rule existed.
UNCHANGED_KEYS = {
    "inventory", "inventory_caps", "discovery", "discovery_projection_note", "discovery_caps",
    "offered_fields", "unoffered_fields", "field_id_note", "calibration", "offered_controls",
    "image", "image_unavailable_reason", "capability_note",
}
PROJECTION_KEYS = {"unoffered_fields_note", "sidecar", "context_budget"}


def tokens(text):
    return pack.estimated_prompt_tokens(len(text))


def test_a_packet_inside_both_limits_carries_every_row_and_no_new_key():
    snapshot, record = evidence(field_count=6)
    built = make(snapshot, record)
    data = json.loads(built.text)
    assert set(data) == UNCHANGED_KEYS
    assert built.sidecar is None
    assert [row["id"] for row in data["discovery"]["fields"]] == [row["id"] for row in snapshot["data"]["fields"]]
    assert len(data["unoffered_fields"]) == 0


def test_the_limit_itself_does_not_project_and_one_token_more_does(monkeypatch):
    snapshot, record = evidence(field_count=6)
    inside = make(snapshot, record)
    monkeypatch.setattr(pack, "CONTEXT_LIMIT_TOKENS", tokens(inside.text))
    assert json.loads(make(snapshot, record).text) == json.loads(inside.text)
    assert make(snapshot, record).sidecar is None
    # One token less than the packet needs is over the context, so the rule engages. Nothing is
    # unoffered here, so the field projection has nothing to move and the packet cannot be made to
    # fit: it refuses instead of sending a request that is known to overflow.
    monkeypatch.setattr(pack, "CONTEXT_LIMIT_TOKENS", tokens(inside.text) - 1)
    with pytest.raises(ValueError, match="no request was sent"):
        make(snapshot, record)


def test_the_offer_budget_moves_every_unoffered_row_to_the_sidecar():
    snapshot, record = evidence(field_count=1100)
    built = make(snapshot, record)
    data = json.loads(built.text)
    assert set(data) - UNCHANGED_KEYS == PROJECTION_KEYS | {"offer_budget", "offer_note"}
    # Only offered fields keep a full descriptor row, and every offered field keeps one.
    rows = data["discovery"]["fields"]
    assert [row["id"] for row in rows] == [ident for ident in
                                           (row["id"] for row in snapshot["data"]["fields"])
                                           if ident in set(built.field_bindings.values())]
    assert {row["id"] for row in rows} == set(built.field_bindings.values())
    # The packet counts what it does not carry, by reason, tier and declaring assembly.
    summary = data["unoffered_fields"]
    assert [row["reason"] for row in summary] == [pack.OFFER_BUDGET_REASON]
    assert summary[0]["count"] == data["offer_budget"]["unoffered_fields"] == 1100 - len(rows)
    assert summary[0]["by_tier"] == {pack.TIER_PROJECT: summary[0]["count"]}
    assert summary[0]["by_declaring_assembly"] == {"Game": summary[0]["count"]}
    # The sidecar holds exactly those rows, and the packet records its hash and row count.
    assert built.sidecar is not None
    sidecar = json.loads(built.sidecar)
    assert len(sidecar["fields"]) == data["sidecar"]["rows"] == data["sidecar"]["field_rows"] == summary[0]["count"]
    assert data["sidecar"]["object_rows"] == 0
    assert data["sidecar"]["sha256"] == hashlib.sha256(built.sidecar.encode("utf-8")).hexdigest()
    assert {row["source_field_id"] for row in sidecar["fields"]} == (
        set(row["id"] for row in snapshot["data"]["fields"]) - set(built.field_bindings.values()))
    assert all("descriptor" in row for row in sidecar["fields"])
    # The context rule's own numbers are recorded, from the measured ratio.
    budget = data["context_budget"]
    assert budget["chars_per_token"] == pack.MEASURED_CHARS_PER_TOKEN
    assert budget["limit_tokens"] == pack.CONTEXT_LIMIT_TOKENS
    assert budget["estimated_prompt_tokens"] == tokens(built.text)
    assert budget["packet_chars"] == len(built.text)
    assert budget["before_projection"]["packet_chars"] > budget["packet_chars"]


def test_the_context_limit_alone_projects_the_rows_it_can(monkeypatch):
    """A packet can be over the context with every readable field offered."""
    snapshot, record = evidence(field_count=20)
    for row in snapshot["data"]["fields"][1:]:
        row.update(value_state="unsupported", value_kind="unsupported", declaring_type="T" * 2000)
    wide = make(snapshot, record)
    assert "offer_budget" not in json.loads(wide.text) and wide.sidecar is None
    monkeypatch.setattr(pack, "CONTEXT_LIMIT_TOKENS", max(tokens(wide.text) // 2, 1))
    built = make(snapshot, record)
    data = json.loads(built.text)
    assert "offer_budget" not in data  # the offer was never bounded; only the context was
    assert data["context_budget"]["applied"] is True
    assert [row["reason"] for row in data["unoffered_fields"]] == [pack.NOT_READABLE_REASON]
    assert data["unoffered_fields"][0]["count"] == 19
    assert len(data["discovery"]["fields"]) == 1
    assert data["discovery"]["fields"][0]["id"] == "field:0000"
    assert data["sidecar"]["field_rows"] == 19
    assert tokens(built.text) <= pack.CONTEXT_LIMIT_TOKENS


def test_object_sections_follow_the_fields_when_the_packet_is_still_too_large(monkeypatch):
    snapshot, record = evidence(field_count=1100)
    # Objects that declare no offered field, each with a row too large to keep once the fields are
    # summarized: this fixture's fields name no object at all, so every component and binding goes.
    snapshot["data"]["components"] = [{"id": f"component:{index}", "object_id": "decor", "missing": False,
                                       "type": "T" * 2000} for index in range(4)]
    snapshot["caps"]["components"] = cap(4)
    monkeypatch.setattr(pack, "CONTEXT_LIMIT_TOKENS", 10 ** 9)
    first = make(snapshot, record)
    data = json.loads(first.text)
    assert len(data["discovery"]["components"]) == 4 and "unoffered_objects" not in data
    # Set the bound just under what the field projection still needs: the object sections that
    # describe objects with no offered field are the next thing that can go.
    monkeypatch.setattr(pack, "CONTEXT_LIMIT_TOKENS", tokens(first.text) - 100)
    built = make(snapshot, record)
    data = json.loads(built.text)
    assert tokens(built.text) <= pack.CONTEXT_LIMIT_TOKENS
    assert data["unoffered_objects"]["rows_left_out"] == {"components": 4, "bindings": 1}
    assert data["discovery"]["components"] == [] and data["discovery"]["bindings"] == []
    sidecar = json.loads(built.sidecar)
    assert data["sidecar"]["object_rows"] == len(sidecar["objects"]) == 5
    assert data["sidecar"]["rows"] == data["sidecar"]["field_rows"] + 5
    assert {row["section"] for row in sidecar["objects"]} == {"components", "bindings"}
    assert [row for row in sidecar["objects"]
            if row["section"] == "bindings"][0]["row"]["source_object_id"] == "entity"


def test_a_packet_that_still_cannot_fit_raises_before_any_request(monkeypatch):
    snapshot, record = evidence(field_count=1100)
    monkeypatch.setattr(pack, "CONTEXT_LIMIT_TOKENS", 100)
    with pytest.raises(ValueError) as excinfo:
        make(snapshot, record)
    assert "no request was sent" in str(excinfo.value)
    assert str(pack.CONTEXT_LIMIT_TOKENS) in str(excinfo.value)


def test_a_projected_packet_still_assembles_and_validates_a_manifest():
    snapshot, record = evidence(field_count=1100)
    built = make(snapshot, record)
    token = next(iter(built.field_bindings))
    answers = [
        {"stage_4": {"readers": [{"field_id": token, "alias": "speed", "why": "observe"}],
                     "unobservable": ["missing state"]}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "move", "intent": "move", "hold_s": 0.2,
                                                   "inputs": [{"control_id": "w", "x": None, "y": None}],
                                                   "preconditions": []}], "skills": [],
                                      "uncontrollable": ["missing input"]}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [],
                                                       "unjudgeable": ["missing objective"]}},
    ]
    replies = iter(answers)
    result = stages.run(built, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {"usage": None}),
                        lambda _: None)
    assembled = manifest.assemble(built, result)
    assert assembled.packet_sha256 == hashlib.sha256(built.text.encode("utf-8")).hexdigest()
    assert manifest.validate_evidence(assembled, built) == []
    # The manifest's selection keeps the packet's compact summary and the sidecar's identity.
    assert assembled.selection.unoffered_fields == json.loads(built.text)["unoffered_fields"]
    assert assembled.selection.unoffered_fields[0]["count"] == json.loads(built.text)["offer_budget"]["unoffered_fields"]
