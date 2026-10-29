"""Catalog export offers supported declarations only and keeps their exact values."""

import json
from pathlib import Path

from playtest.contracts.action_catalog import ActionCatalog
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight.catalog import export, serialize

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "preflight_manifest"


def load(name):
    return PreflightManifest.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"), strict=True)


def test_export_contains_only_declarations_with_stored_execution_evidence():
    value = load("valid-execution-evidence.json")
    catalog = export(value)
    assert set(catalog.actions) == {"move"}
    definition = catalog.actions["move"]
    assert definition.default_lease_s == 0.2
    assert definition.inputs[0].device == "Keyboard" and definition.inputs[0].control == "w"


def test_an_unproven_declaration_is_not_exported_as_executable():
    value = load("valid-chord-skill.json")
    assert not value.actions[0].execution_supported and not value.skills[0].execution_supported
    assert export(value).actions == {}


def test_exported_catalog_round_trips_the_shared_contract(tmp_path):
    catalog = export(load("valid-execution-evidence.json"))
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(serialize(catalog)), encoding="utf-8")
    assert ActionCatalog.model_validate_json(path.read_text(encoding="utf-8"), strict=True) == catalog


def test_serialized_catalog_keeps_null_button_values_and_only_the_used_mode():
    text = serialize(export(load("valid-execution-evidence.json")))["actions"]["move"]
    assert text["inputs"] == [{"device": "Keyboard", "control": "w", "x": None, "y": None}]
    assert "steps" not in text and "max_s" not in text
    skill = ActionCatalog(actions={"step": {
        "backend": "input_system",
        "steps": [{"inputs": [{"device": "Keyboard", "control": "w", "x": None, "y": None}],
                   "hold_s": 0.2, "wait_s": 0.1}],
        "max_s": 1.0}})
    row = serialize(skill)["actions"]["step"]
    assert "inputs" not in row and "default_lease_s" not in row and row["max_s"] == 1.0
