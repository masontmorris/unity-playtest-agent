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
