"""The CLI writes a durable attempt ledger and never fabricates a partial manifest."""

import json
import pytest

from playtest.contracts.manifest import PreflightManifest
from playtest.preflight import manifest_cli, stages, validate_cli
from test_preflight_packet import evidence


def files(tmp_path):
    snapshot, calibration = evidence()
    inventory = dict(snapshot, stage="inventory", data={}, caps={})
    project = tmp_path / "game"
    project.mkdir()
    paths = {}
    for name, data in (("inventory", inventory), ("discovery", snapshot), ("calibration", calibration)):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(data), encoding="utf-8")
    image = tmp_path / "frame.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    args = ["--project", str(project), "--inventory", str(paths["inventory"]),
            "--discovery", str(paths["discovery"]), "--calibration", str(paths["calibration"]),
            "--screenshot", str(image), "--screenshot-mime", "image/png",
            "--screenshot-provenance", "fixture capture", "--output", str(tmp_path / "manifest.json"),
            "--ledger", str(tmp_path / "ledger.jsonl"), "--approved-calls", "12",
            "--approved-estimate-usd", "16.64"]
    return args


@pytest.mark.parametrize("model", ["muse-spark-1.3", "muse-spark-1.3-contributor"])
def test_end_to_end_writes_each_call_and_a_validated_manifest(tmp_path, monkeypatch, model):
    args = files(tmp_path) + ["--model", model]
    answers = iter([
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "move", "intent": "move", "hold_s": 0.2,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}],
         "skills": [], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [],
         "unjudgeable": ["no progress field"]}},
    ])
    monkeypatch.setattr(manifest_cli.provider, "Muse", lambda _, **__: lambda *_: stages.CallResult(
        json.dumps(next(answers)), None, {"model": model, "resolved_model": model, "usage": None}))
    assert manifest_cli.main(args) == 0
    rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in rows] == ["reservation", "attempt"] * 3
    assert all(r["model"] == model for r in rows[::2])
    assert [r["stage"] for r in rows[1::2]] == ["readers", "actions", "progress"]
    result = PreflightManifest.model_validate_json((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert result.actions[0].name == "move" and result.image.provenance == "fixture capture"
    check = ["--manifest", str(tmp_path / "manifest.json"), "--ledger", str(tmp_path / "ledger.jsonl"),
             "--inventory", str(tmp_path / "inventory.json"),
             "--discovery", str(tmp_path / "discovery.json"), "--calibration", str(tmp_path / "calibration.json"),
             "--screenshot", str(tmp_path / "frame.png"), "--expected-model", model]
    assert validate_cli.main(check) == 0
    ledger = tmp_path / "ledger.jsonl"
    original = ledger.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in original.splitlines()]
    rows[1]["metadata"]["resolved_model"] = "different"
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert validate_cli.main(check) == 1
    rows = [json.loads(line) for line in original.splitlines()]
    changed = json.loads(rows[3]["content"])
    changed["stage_5"]["actions"][0]["name"] = "different"
    rows[3]["content"] = json.dumps(changed)
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert validate_cli.main(check) == 1
    ledger.write_text(original, encoding="utf-8")
    (tmp_path / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    assert validate_cli.main(check) == 1


def test_failure_keeps_ledger_and_does_not_write_manifest(tmp_path, monkeypatch):
    args = files(tmp_path) + ["--max-retries", "0"]
    monkeypatch.setattr(manifest_cli.provider, "Muse", lambda _, **__: lambda *_: stages.CallResult("{}", None, {}))
    assert manifest_cli.main(args) == 1
    assert not (tmp_path / "manifest.json").exists()
    rows = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2 and json.loads(rows[1])["problems"]


def test_insufficient_approval_prevents_calls_and_ledger(tmp_path, monkeypatch):
    args = files(tmp_path)
    args[args.index("--approved-calls") + 1] = "2"
    monkeypatch.setattr(manifest_cli.provider, "Muse", lambda _, **__: (_ for _ in ()).throw(AssertionError("called")))
    assert manifest_cli.main(args) == 1
    assert not (tmp_path / "ledger.jsonl").exists()


def test_contributor_budget_is_explicit_and_cheaper():
    from playtest.preflight.provider import CONTRIBUTOR_MODEL
    calls, estimate = manifest_cli._approved_budget(3, 12, "1.28", CONTRIBUTOR_MODEL)
    assert calls == 12 and str(estimate) == "1.2768"
    calls, estimate = manifest_cli._approved_budget(3, 9, "0.96", CONTRIBUTOR_MODEL)
    assert calls == 9 and str(estimate) == "0.9576"


def test_crash_during_request_preserves_unresolved_reservation(tmp_path, monkeypatch):
    args = files(tmp_path)
    def fail(*_):
        raise OSError("connection interrupted")
    monkeypatch.setattr(manifest_cli.provider, "Muse", lambda _, **__: fail)
    assert manifest_cli.main(args) == 1
    rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "reservation"
    assert not (tmp_path / "manifest.json").exists()
