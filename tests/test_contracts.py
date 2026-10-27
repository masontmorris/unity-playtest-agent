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
