"""The live trial harness's pipeline without a browser: smoke manifest, probe catalog, input probe, run catalog.

The simulated page stands in for Chrome and the game, so this checks that the input probe measures
release-verified evidence through the three.js bridge, and that once every declaration is supported
the exported run catalog is byte-identical to the probe catalog the relay already serves.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from playtest.preflight import catalog as catalog_export
from playtest.preflight import input_probe

from test_bridge_three import SIM_PAGE, _node

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "three"))
import live_trial  # noqa: E402

SPEC = {"scene_id": "sim", "entity_id": "scene:sim::0", "actions": [
    {"name": "go", "control": "w", "hold_s": 0.2}, {"name": "up", "control": "space", "hold_s": 0.08}]}


def test_probe_supports_declarations_and_the_run_catalog_matches(tmp_path, monkeypatch):
    runs = tmp_path / "runs" / "trial"
    runs.mkdir(parents=True)
    monkeypatch.setenv("UPA_REPO_ROOT", str(tmp_path))
    manifest = runs / "manifest.json"
    manifest.write_text(json.dumps(live_trial.build_manifest(SPEC)), encoding="utf-8")
    probe_catalog = runs / "probe-catalog.json"
    live_trial.write_catalog(probe_catalog, live_trial.declared_catalog(manifest))
    project = tmp_path / "project"
    project.mkdir()
    page = subprocess.Popen([_node(), str(SIM_PAGE), "--project", str(project), "--catalog", str(probe_catalog)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        if json.loads(page.stdout.readline() or "null") != {"ready": True}:
            pytest.fail("the simulated page did not start")
        plan = runs / "plan.json"
        plan.write_text(json.dumps({
            "schema_version": 1, "catalog": str(probe_catalog), "manifest": str(manifest),
            "update_manifest": str(manifest), "entity_id": SPEC["entity_id"],
            "actions": [{"name": item["name"], "repeats": 2} for item in SPEC["actions"]], "max_age_s": 5.0}),
            encoding="utf-8")
        assert input_probe.main(["--project", str(project), "--plan", str(plan), "--output",
                                 str(runs / "probe.json")]) == 0
    finally:
        page.stdin.close()
        page.wait(timeout=10)

    probe = json.loads((runs / "probe.json").read_text(encoding="utf-8"))
    assert probe["totals"]["k_over_n"] == "4/4"
    for target in probe["targets"]:
        for attempt in target["attempts"]:
            assert attempt["neutral_release"]["verified"] is True, attempt["neutral_release"]["reason"]
    updated = json.loads(manifest.read_text(encoding="utf-8"))
    assert [row["execution_supported"] for row in updated["actions"]] == [True, True]

    run_catalog = runs / "catalog.json"
    assert catalog_export.main(["--manifest", str(manifest), "--output", str(run_catalog)]) == 0
    assert run_catalog.read_bytes() == probe_catalog.read_bytes()
