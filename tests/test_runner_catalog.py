"""The catalog a session host serves must be exactly what the manifest exports, or the run does not start.

The client re-reads the file the host was given and fails closed on every way it can be wrong: absent, altered
since export, invalid, or different (a missing or extra declaration, a chord cut to its first key, a changed
value, hold, wait or bound, a legacy keyboard catalog). Nothing is repaired and no other catalog is used.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from playtest.contracts.action_catalog import ActionCatalog
from playtest.preflight import catalog as exporter
from playtest.preflight.catalog import export, serialize
from playtest.runner.catalog import HostCatalog, differences, verify_host_catalog

from runner_manifest import action, all_modes, button, hold, manifest, skill, vector

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "action_catalog" / "valid-session-export.json"
# The SHA-256 of the committed golden's bytes. PowerShell's Get-FileHash, Python's hashlib and the C# host all
# compute it, and `BridgeSessionCatalogTests` pins the same literal; change the golden and all three move together.
GOLDEN_SHA256 = "6405eb6c3f96a642b475691827a4f1a061b33981e980eea146ae0be5db61dc29"


def pinned(path: Path, text: str | None = None) -> str:
    """Write `text` to `path` if given and return the SHA-256 of what the file holds."""
    if text is not None:
        path.write_text(text, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exported(tmp_path: Path, value, name: str = "catalog.json") -> tuple[Path, str]:
    """The catalog file `run-session.ps1` would write for `value`, and the digest it pins."""
    source = tmp_path / f"{name}.manifest.json"
    source.write_text(value.model_dump_json(), encoding="utf-8")
    path = tmp_path / name
    assert exporter.main(["--manifest", str(source), "--output", str(path)]) == 0
    return path, pinned(path)


def refused(value, path, sha, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        verify_host_catalog(value, path, sha)


def test_the_exact_export_is_accepted_and_returns_what_the_host_serves(tmp_path: Path):
    value = all_modes()
    path, sha = exported(tmp_path, value)
    host = verify_host_catalog(value, path, sha)
    assert isinstance(host, HostCatalog) and host.sha256 == sha
    assert host.catalog == export(value) and list(host.catalog.actions) != []
    assert verify_host_catalog(value, path, sha.upper()).sha256 == sha     # a digest is hex, whatever its case


def test_the_committed_session_catalog_is_the_real_exporters_output_for_every_declared_mode():
    """The C# host tests read this file, so it must be what the exporter writes, not a hand-made copy of it."""
    value = all_modes()
    assert json.loads(GOLDEN.read_text(encoding="utf-8")) == serialize(export(value))
    assert set(export(value).actions) == {"key_hold", "key_chord", "device_chord", "pointer_impulse", "stick_value",
                                          "trigger_value", "dpad_value", "pad_button", "mouse_button", "key_sequence",
                                          "pointer_sequence"}
    assert not {"unproven_chord", "unproven_skill"} & set(export(value).actions)
    assert ActionCatalog.model_validate_json(GOLDEN.read_bytes(), strict=True) == export(value)


def test_the_committed_session_catalog_bytes_are_what_the_exporter_cli_writes(tmp_path: Path):
    path, sha = exported(tmp_path, all_modes())
    assert path.read_bytes() == GOLDEN.read_bytes()
    assert sha == hashlib.sha256(GOLDEN.read_bytes()).hexdigest() == GOLDEN_SHA256   # the pin the host and client check
    assert b"\r" not in GOLDEN.read_bytes()                                          # LF on every checkout


def test_an_absent_path_or_digest_is_refused_not_defaulted(tmp_path: Path):
    value = manifest(action("move", "w", holds=[hold(0.2)]))
    path, sha = exported(tmp_path, value)
    refused(value, None, sha, "no session catalog")
    refused(value, path, None, "no pinned session catalog digest")
    refused(value, path, "", "no pinned session catalog digest")
    refused(value, path, "abc123", "no pinned session catalog digest")
    refused(value, path, "z" * 64, "no pinned session catalog digest")


def test_a_missing_or_unreadable_file_is_refused(tmp_path: Path):
    value = manifest(action("move", "w", holds=[hold(0.2)]))
    refused(value, tmp_path / "absent.json", "0" * 64, "cannot read the session catalog")
    refused(value, tmp_path, "0" * 64, "cannot read the session catalog")           # a directory is not a file


def test_a_file_altered_after_export_is_refused_even_when_it_is_still_a_valid_catalog(tmp_path: Path):
    value = manifest(action("move", "w", hold_s=0.2, holds=[hold(0.2)]))
    path, sha = exported(tmp_path, value)
    path.write_text(path.read_text(encoding="utf-8").replace("0.2", "0.25"), encoding="utf-8")
    assert ActionCatalog.model_validate_json(path.read_bytes(), strict=True).actions["move"].default_lease_s == 0.25
    refused(value, path, sha, "not the file that was exported")
    spaced, spaced_sha = exported(tmp_path, value, "spaced.json")
    spaced.write_text(spaced.read_text(encoding="utf-8") + "\n", encoding="utf-8")      # even whitespace changes the digest
    refused(value, spaced, spaced_sha, "not the file that was exported")


@pytest.mark.parametrize("text, why", [
    ("{ not json", "not a valid action catalog"),
    ("[]", "not a valid action catalog"),
    ('{"actions": {}, "schema": 1}', "not a valid action catalog"),            # strict: no extra member
    ('{"actions": {"Move": {"backend": "input_system", "inputs": [{"device": "Keyboard", "control": "w", "x": null, '
     '"y": null}], "default_lease_s": 0.2}}}', "not a valid action catalog"),   # not a lower_snake_case name
    ('{"actions": {"move": {"backend": "input_system", "inputs": [{"device": "Touchscreen", "control": "primaryTouch", '
     '"x": null, "y": null}], "default_lease_s": 0.2}}}', "not a valid action catalog"),
])
def test_an_invalid_catalog_is_refused_with_its_own_digest_pinned(tmp_path: Path, text: str, why: str):
    value = manifest(action("move", "w", holds=[hold(0.2)]))
    path = tmp_path / "catalog.json"
    refused(value, path, pinned(path, text), why)


def test_the_legacy_keyboard_only_catalog_is_not_a_session_catalog(tmp_path: Path):
    """The bench host's per-key `preflight_key_*` shorthand never serves a session, even with its digest pinned."""
    value = manifest(action("move", "w", holds=[hold(0.2)]))
    legacy = {"actions": {f"preflight_key_{k}": {"backend": "input_system", "device": "Keyboard", "control": k,
                                                  "default_lease_s": 0.4} for k in ("w", "a")}}
    path = tmp_path / "catalog.json"
    refused(value, path, pinned(path, json.dumps(legacy)), "not a valid action catalog")
    modern = {"actions": {"preflight_key_w": {"backend": "input_system", "default_lease_s": 0.4,
                                              "inputs": [{"device": "Keyboard", "control": "w", "x": None, "y": None}]}}}
    refused(value, path, pinned(path, json.dumps(modern)),
            "'move' is supported by the manifest but the host does not serve it; "
            "'preflight_key_w' is served by the host but the manifest does not support it")


def test_a_catalog_exported_from_another_manifest_is_refused_by_name(tmp_path: Path):
    value = manifest(action("move", "w", holds=[hold(0.2)]), action("strafe", ["w", "a"]))
    other, other_sha = exported(tmp_path, manifest(action("move", "w", holds=[hold(0.2)]), action("jump", "space")), "other.json")
    refused(value, other, other_sha,
            "'strafe' is supported by the manifest but the host does not serve it; "
            "'jump' is served by the host but the manifest does not support it")
    missing, missing_sha = exported(tmp_path, manifest(action("move", "w", holds=[hold(0.2)])), "missing.json")
    refused(value, missing, missing_sha, "'strafe' is supported by the manifest but the host does not serve it")
    extra, extra_sha = exported(tmp_path, manifest(
        action("move", "w", holds=[hold(0.2)]), action("strafe", ["w", "a"]),
        skills=(skill("dash", [(["w"], 0.1, 0.0)], 0.5),)), "extra.json")
    refused(value, extra, extra_sha, "'dash' is served by the host but the manifest does not support it")


def test_a_declaration_that_was_cut_down_or_changed_is_refused_not_matched_by_name(tmp_path: Path):
    """Same names, valid catalogs, but not the declarations the manifest proved: each change is named."""
    value = manifest(
        action("strafe", ["w", "a"], hold_s=0.2),
        action("look", vector("Mouse", "delta", 0.5, -0.25), hold_s=0.1),
        action("steer", vector("Gamepad", "leftStick", 0.3, 1.0), hold_s=0.5),
        skills=(skill("dash", [(["w"], 0.2, 0.1), (["a"], 0.2, 0.0)], 1.0),))
    served = serialize(export(value))

    def variant(change) -> dict:
        body = json.loads(json.dumps(served))
        change(body["actions"])
        return body

    cases = {
        "chord cut to its first key": (variant(lambda a: a["strafe"]["inputs"].pop()), "strafe.*has 1 inputs, expected 2"),
        "chord reordered": (variant(lambda a: a["strafe"]["inputs"].reverse()), "strafe.*is Keyboard/a, expected Keyboard/w"),
        "chord hold changed": (variant(lambda a: a["strafe"].update(default_lease_s=0.3)), "strafe.*hold is 0.3 s, expected 0.2 s"),
        "mouse value changed": (variant(lambda a: a["look"]["inputs"][0].update(x=1.0)), "look.*x=1.0, y=-0.25, expected x=0.5"),
        "mouse value lost": (variant(lambda a: a["look"]["inputs"][0].update(x=0.0, y=0.0)), "look.*x=0.0, y=0.0, expected x=0.5"),
        "stick direction changed": (variant(lambda a: a["steer"]["inputs"][0].update(y=0.0)), "steer.*y=0.0, expected x=0.3, y=1.0"),
        "stick became another control": (variant(lambda a: a["steer"]["inputs"][0].update(control="rightStick")),
                                         "steer.*Gamepad/rightStick, expected Gamepad/leftStick"),
        "skill step wait changed": (variant(lambda a: a["dash"]["steps"][0].update(wait_s=0.0)), "dash.*wait is 0.0 s, expected 0.1 s"),
        "skill bound changed": (variant(lambda a: a["dash"].update(max_s=2.0)), "dash.*max_s is 2.0 s, expected 1.0 s"),
        "skill lost a step": (variant(lambda a: a["dash"]["steps"].pop()), "dash.*has 1 steps, expected 2"),
        "skill became an action": (variant(lambda a: a.update(dash={
            "backend": "input_system", "inputs": a["dash"]["steps"][0]["inputs"], "default_lease_s": 0.2})),
                                   "dash.*is a action, expected a skill"),
    }
    for label, (body, match) in cases.items():
        path = tmp_path / "variant.json"
        sha = pinned(path, json.dumps(body))
        with pytest.raises(ValueError, match=match) as raised:
            verify_host_catalog(value, path, sha)
        assert "the session catalog is not the manifest's export" in str(raised.value), label


def test_differences_name_every_way_two_catalogs_part():
    value = manifest(action("move", "w", holds=[hold(0.2)]), action("strafe", ["w", "a"]))
    wanted = export(value)
    assert differences(wanted, export(value)) == []
    assert differences(wanted, ActionCatalog(actions={})) == [
        "'move' is supported by the manifest but the host does not serve it",
        "'strafe' is supported by the manifest but the host does not serve it"]
    only = ActionCatalog(actions={"strafe": wanted.actions["strafe"], "jump": export(
        manifest(action("jump", button("Gamepad", "buttonSouth"), hold_s=0.1))).actions["jump"]})
    assert differences(wanted, only) == [
        "'move' is supported by the manifest but the host does not serve it",
        "'jump' is served by the host but the manifest does not support it"]


def test_an_empty_export_is_matched_by_an_empty_catalog_and_left_to_the_caller_to_refuse(tmp_path: Path):
    """Nothing to serve is not a mismatch; the CLI separately refuses a manifest that offers no executable action."""
    value = manifest(action("move", "w", supported=False))
    path, sha = exported(tmp_path, value)
    assert verify_host_catalog(value, path, sha).catalog.actions == {}
