"""Contracts: fixtures validate as labelled, and committed schemas match the models."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from playtest.contracts import CONTRACTS
from playtest.contracts.export import SCHEMA_DIR, render

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"
CASES = sorted(FIXTURES.glob("*/*.json"))


def _id(p: Path) -> str:
    return f"{p.parent.name}/{p.stem}"


def test_every_fixture_dir_is_a_contract():
    assert {p.name for p in FIXTURES.iterdir() if p.is_dir()} <= set(CONTRACTS)


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_every_contract_has_a_valid_fixture(name):
    assert list((FIXTURES / name).glob("valid-*.json")), f"no valid fixture for {name}"


@pytest.mark.parametrize("path", CASES, ids=_id)
def test_fixture_against_model(path):
    model = CONTRACTS[path.parent.name]
    body = json.loads(path.read_text(encoding="utf-8"))
    if path.stem.startswith("valid-"):
        model.model_validate(body)
    else:
        assert path.stem.startswith("invalid-")
        with pytest.raises(ValidationError):
            model.model_validate(body)


@pytest.mark.parametrize("path", [p for p in CASES if p.stem.startswith("valid-")], ids=_id)
def test_valid_fixture_against_exported_schema(path):
    """The schema the C# side reads accepts everything the models accept.

    Cross-field rules (model validators) are not expressible in the exported schema; the C# DTOs
    check them and run against the same invalid fixtures.
    """
    schema = json.loads((SCHEMA_DIR / f"{path.parent.name}.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_schema_not_drifted(name):
    committed = SCHEMA_DIR / f"{name}.schema.json"
    assert committed.exists(), "run `uv run playtest-export-schemas`"
    assert committed.read_text(encoding="utf-8") == render(name), "schema drifted: run `uv run playtest-export-schemas`"


def test_inspection_caps_fail_for_their_intended_rule():
    """Cap fixtures must fail on the cross-field rule, not on a missing field or bad type."""
    reasons = {
        "invalid-returned-over-limit": "returned must not exceed limit",
        "invalid-observed-below-returned": "observed must be at least returned",
        "invalid-truncated-false-below-observed": "truncated is false",
        "invalid-truncated-true-complete": "truncated is true",
        "invalid-transform-observed-below-returned": "observed must be at least returned",
    }
    for stem, reason in reasons.items():
        path = FIXTURES / "inspection_result" / f"{stem}.json"
        with pytest.raises(ValidationError) as excinfo:
            CONTRACTS["inspection_result"].model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert reason in str(excinfo.value), f"{stem} failed for the wrong reason"


def test_observation_view_invalid_fixtures_fail_for_their_intended_rule():
    """A view is measured as a pair: a missing pitch is not a pitch of zero, and a pitch past the pole is not a view."""
    reasons = {
        "invalid-view-pitch-out-of-range": "less than or equal to 90",
        "invalid-view-missing-pitch": "pitch_deg",
    }
    for stem, reason in reasons.items():
        path = FIXTURES / "observation" / f"{stem}.json"
        with pytest.raises(ValidationError) as excinfo:
            CONTRACTS["observation"].model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert reason in str(excinfo.value), f"{stem} failed for the wrong reason"
        assert "view" in str(excinfo.value), f"{stem} failed outside the view"


def test_an_unknown_view_is_null_and_never_a_heading_of_zero():
    unknown = CONTRACTS["observation"].model_validate(json.loads(
        (FIXTURES / "observation" / "valid-view-unknown.json").read_text(encoding="utf-8")))
    absent = CONTRACTS["observation"].model_validate(json.loads(
        (FIXTURES / "observation" / "valid-basic.json").read_text(encoding="utf-8")))
    seen = CONTRACTS["observation"].model_validate(json.loads(
        (FIXTURES / "observation" / "valid-view.json").read_text(encoding="utf-8")))
    assert unknown.view is None and absent.view is None
    assert (seen.view.yaw_deg, seen.view.pitch_deg) == (-172.32, 12.5)


def test_calibration_view_turn_fixture_fails_for_its_intended_rule():
    path = FIXTURES / "calibration_record" / "invalid-view-turn-more-trials-than-valid.json"
    with pytest.raises(ValidationError, match="a view turn cannot average more trials than were valid"):
        CONTRACTS["calibration_record"].model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_manifest_rejects_an_effect_claim_for_an_unmeasured_axis_value():
    path = FIXTURES / "preflight_manifest" / "invalid-effect-unmeasured-axis-value.json"
    with pytest.raises(ValidationError, match="hold effect must reflect the matching calibration trials"):
        CONTRACTS["preflight_manifest"].model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_action_catalog_invalid_fixtures_fail_for_their_intended_rule():
    reasons = {
        "invalid-duplicate-control": "appears twice",
        "invalid-oversize-inputs": "at most 3 items",
        "invalid-missing-axis-value": "vector control",
        "invalid-button-with-values": "button; x and y must be null",
        "invalid-skill-over-budget": "more than max_s",
        "invalid-both-inputs-and-steps": "never both",
        "invalid-lease-too-long": "less than or equal to 3600",
        "invalid-unsupported-control": "not a supported root control",
        "invalid-one-dimensional-y": "y must be 0",
        "invalid-negative-trigger": "trigger; x must be between 0 and 1",
        "invalid-fractional-dpad": "dpad is digital",
        "invalid-nine-steps": "at most 8 items",
        "invalid-action-name": "lower_snake_case",
    }
    for stem, reason in reasons.items():
        path = FIXTURES / "action_catalog" / f"{stem}.json"
        with pytest.raises(ValidationError) as excinfo:
            CONTRACTS["action_catalog"].model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert reason in str(excinfo.value), f"{stem} failed for the wrong reason"


def test_manifest_execution_evidence_fixtures_fail_for_their_intended_rule():
    reasons = {
        "invalid-support-without-evidence": "execution support must reflect",
        "invalid-physical-binding": "physical input must match its calibrated control binding",
        "invalid-evidence-missing-duration": "no measured applied duration",
        "invalid-evidence-unverified-release": "release was not verified",
        "invalid-evidence-changed-control": "control is 'a'",
        "invalid-evidence-extra-receipt": "expected 1 applied-input receipts",
        "invalid-evidence-changed-hold": "does not match the declared",
        "invalid-evidence-terminal-release-false": "did not verify input release",
        "invalid-evidence-reordered-inputs": "control is 'a'",
        "invalid-skill-missing-step": "expected 2 applied-input receipts",
        "invalid-evidence-declaration-changed-hold": "declaration hold is 0.21 s, expected 0.2 s",
        "invalid-evidence-declaration-missing": "declaration",
        "invalid-evidence-pointer-delta": "applied delta x 0 px does not match the declared 64 px",
        "invalid-evidence-chord-window": "inputs were not applied together",
        "invalid-evidence-skill-wait": "less than the declared 0.1 s wait",
        "invalid-evidence-skill-final-wait": "final wait",
        "invalid-evidence-skill-no-measured-total": "no measured total applied duration",
    }
    for stem, reason in reasons.items():
        path = FIXTURES / "preflight_manifest" / f"{stem}.json"
        with pytest.raises(ValidationError) as excinfo:
            CONTRACTS["preflight_manifest"].model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert reason in str(excinfo.value), f"{stem} failed for the wrong reason"


def test_execution_receipt_invalid_fixtures_fail_for_their_intended_rule():
    reasons = {
        "invalid-receipt-negative-duration": "greater than or equal to 0",
        "invalid-receipt-step-order": "execution order",
        "invalid-rejected-with-receipts": "cannot have receipts",
    }
    for stem, reason in reasons.items():
        path = FIXTURES / "execution_record" / f"{stem}.json"
        with pytest.raises(ValidationError) as excinfo:
            CONTRACTS["execution_record"].model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert reason in str(excinfo.value), f"{stem} failed for the wrong reason"


def test_investigation_trace_uncited_evidence_fails_for_the_intended_rule():
    path = FIXTURES / "trace_entry" / "invalid-investigation-uncited-evidence.json"
    with pytest.raises(ValidationError, match="evidence_trace_seq must be null or an earlier cited trace sequence"):
        CONTRACTS["trace_entry"].model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_suspicion_invalid_fixtures_fail_for_their_intended_rule():
    reasons = {
        "invalid-blank-statement": "a suspicion needs a statement",
        "invalid-duplicate-evidence": "cites the same entry twice",
        "invalid-negative-evidence": "non-negative trace sequence numbers",
        "invalid-unknown-basis": "invariant",       # the enum error lists the closed set of bases
        "invalid-extra-field": "Extra inputs are not permitted",
    }
    for stem, reason in reasons.items():
        path = FIXTURES / "suspicion" / f"{stem}.json"
        with pytest.raises(ValidationError) as excinfo:
            CONTRACTS["suspicion"].model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert reason in str(excinfo.value), f"{stem} failed for the wrong reason"


def test_effect_check_invalid_fixtures_fail_for_their_intended_rule():
    reasons = {
        "invalid-effect-check-equality": "an effect check compares with <, <=, > or >=, not '=='",
        "invalid-effect-check-changed": "an effect check compares with <, <=, > or >=, not 'changed'",
        "invalid-effect-check-non-numeric-value": "an effect check compares with a finite number",
    }
    for stem, reason in reasons.items():
        path = FIXTURES / "finding" / f"{stem}.json"
        with pytest.raises(ValidationError) as excinfo:
            CONTRACTS["finding"].model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert reason in str(excinfo.value), f"{stem} failed for the wrong reason"


def _with_check(**check):
    body = json.loads((FIXTURES / "finding" / "valid-candidate-with-effect-check.json").read_text(encoding="utf-8"))
    body["repro_check"] = {**body["repro_check"], **check}
    return body


@pytest.mark.parametrize("check, reason", [
    ({"op": "unchanged", "value": None}, "not 'unchanged'"),
    ({"op": "!=", "value": 3}, "not '!='"),
    ({"op": "<", "value": True}, "compares with a finite number"),
    ({"op": "<", "value": float("inf")}, "compares with a finite number"),
    ({"op": "<", "value": 1.5, "tolerance": 0.1}, "takes no tolerance"),
    ({"op": "<", "value": None}, "'<' needs a value"),
    ({"field": "effect."}, "names an action"),
    ({"field": "effect.  "}, "names an action"),
], ids=["unchanged", "not_equal", "bool_value", "infinite_value", "tolerance", "no_value", "no_action", "blank_action"])
def test_an_effect_check_is_a_bound_on_a_finite_number(check, reason):
    with pytest.raises(ValidationError) as excinfo:
        CONTRACTS["finding"].model_validate(_with_check(**check))
    assert reason in str(excinfo.value)


@pytest.mark.parametrize("op, value", [("<", 1), ("<=", 1.5), (">", 0), (">=", 6.04)])
def test_every_ordering_comparator_takes_an_effect_check(op, value):
    finding = CONTRACTS["finding"].model_validate(_with_check(op=op, value=value))
    assert (finding.repro_check.field, finding.repro_check.op.value, finding.repro_check.value) == ("effect.move", op, value)


def test_a_reader_check_is_judged_as_before():
    """Only `effect.` fields are bound to a number: a reader check keeps `==` with a tolerance, `changed` and a string."""
    for check in ({"field": "public.counter", "op": "==", "value": 2, "tolerance": 0.5},
                  {"field": "public.counter", "op": "changed", "value": None},
                  {"field": "public.state", "op": "!=", "value": "open"},
                  {"field": "public.effect.speed", "op": "unchanged", "value": None}):   # not the "effect." prefix
        CONTRACTS["finding"].model_validate(_with_check(**check))


def test_suspicion_and_finding_trace_entries_fail_for_their_intended_rule():
    reasons = {
        "invalid-suspicion-turn-differs": "suspicion turn must equal the entry's turn",
        "invalid-suspicion-evidence-not-in-refs": "a suspicion's evidence_seq must be among the entry's refs",
        "invalid-suspicion-null-turn": "turn is null exactly for run, stop, end, or a stop cancel",
        "invalid-finding-evidence-not-in-refs": "a finding's evidence_seq must be among the entry's refs",
        "invalid-finding-candidate-no-evidence": "a finding past 'suspicion' must cite trace sequence numbers",
    }
    for stem, reason in reasons.items():
        path = FIXTURES / "trace_entry" / f"{stem}.json"
        with pytest.raises(ValidationError) as excinfo:
            CONTRACTS["trace_entry"].model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert reason in str(excinfo.value), f"{stem} failed for the wrong reason"
