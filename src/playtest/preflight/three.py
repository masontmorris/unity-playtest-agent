from __future__ import annotations

import hashlib
import json
import math
import re
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playtest.contracts.action_catalog import ActionCatalog, control_type
from playtest.contracts.bridge import ExecutionRecord, StopResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight import calibrate, input_probe, manifest as manifest_checks
from playtest.preflight.catalog import export
from playtest.preflight.packet import field_kind
from playtest.roles.preflight import AXIS_CONTROLS
from playtest.runner.choices import build_choices


@dataclass(frozen=True)
class GameDeclarations:
    game: Path
    catalog_path: Path
    readers: dict[str, dict[str, Any]]
    catalog: ActionCatalog
    source: dict[str, Any]
    readers_bytes: bytes
    catalog_bytes: bytes


@dataclass(frozen=True)
class ThreeStudy:
    declarations: GameDeclarations
    session: str
    initial_observation: dict[str, Any]
    entity: dict[str, Any]
    entity_trials: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    stop: dict[str, Any]
    image: dict[str, Any] | None
    launch: dict[str, Any]
    fatal_error: str | None = None


@dataclass(frozen=True)
class CompiledStudy:
    manifest: PreflightManifest
    calibration: CalibrationRecord
    entity: dict[str, Any]
    catalog: ActionCatalog


def _json(raw: bytes) -> Any:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _worktree_digest(game: Path) -> str:
    listing = subprocess.run(["git", "-C", str(game), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             check=True, capture_output=True).stdout
    digest = hashlib.sha256()
    for raw in sorted(set(listing.split(b"\0")) - {b""}):
        path = game / os.fsdecode(raw)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        if path.is_symlink():
            digest.update(b"link\0" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"missing\0")
    return digest.hexdigest()


def load_declarations(game: Path, catalog: Path | None = None) -> GameDeclarations:
    game = game.resolve()
    reader_path = game / "playtest" / "readers.json"
    catalog_path = catalog or Path("playtest/actions.json")
    catalog_path = catalog_path if catalog_path.is_absolute() else game / catalog_path
    catalog_path = catalog_path.resolve()
    reader_bytes, catalog_bytes = reader_path.read_bytes(), catalog_path.read_bytes()
    readers = _json(reader_bytes).get("readers")
    if not isinstance(readers, dict):
        raise ValueError("readers.json needs a readers object")
    for alias, row in readers.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", alias) or not isinstance(row, dict) or not isinstance(row.get("why"), str) or not row["why"].strip():
            raise ValueError(f"invalid reader declaration {alias!r}")
    raw_catalog = _json(catalog_bytes)
    rows = raw_catalog.get("actions") if isinstance(raw_catalog, dict) else None
    if not isinstance(rows, dict) or any(not isinstance(value, dict) for value in rows.values()):
        raise ValueError("catalog needs an actions object")
    skills = sum("steps" in value and value["steps"] is not None for value in rows.values())
    actions = len(rows) - skills
    if actions > 18 or skills > 6 or len(readers) > 16:
        raise ValueError(f"catalog exceeds caps: {actions} actions (max 18), {skills} skills (max 6), {len(readers)} readers (max 16)")
    parsed = ActionCatalog.model_validate(raw_catalog, strict=True)
    if not actions or not readers:
        raise ValueError("a three.js study requires at least one action and reader")
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(game), *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    source = {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--untracked-files=all")),
              "readers_sha256": _hash(reader_bytes), "catalog_sha256": _hash(catalog_bytes),
              "readers_path": str(reader_path), "catalog_path": str(catalog_path), "worktree_sha256": _worktree_digest(game)}
    return GameDeclarations(game, catalog_path, readers, parsed, source, reader_bytes, catalog_bytes)


def study_dict(study: ThreeStudy) -> dict[str, Any]:
    decl = study.declarations
    return {"session": study.session, "initial_observation": study.initial_observation, "entity": study.entity,
            "entity_trials": study.entity_trials, "attempts": study.attempts, "stop": study.stop,
            "image": study.image, "launch": study.launch, "fatal_error": study.fatal_error, "source": decl.source,
            "game": str(decl.game), "catalog_path": str(decl.catalog_path),
            "readers_json": decl.readers_bytes.decode("utf-8"), "catalog_json": decl.catalog_bytes.decode("utf-8")}


def load_study(path: Path) -> ThreeStudy:
    data = _json(path.read_bytes())
    readers_bytes, catalog_bytes = data["readers_json"].encode("utf-8"), data["catalog_json"].encode("utf-8")
    if _hash(readers_bytes) != data["source"]["readers_sha256"] or _hash(catalog_bytes) != data["source"]["catalog_sha256"]:
        raise ValueError("saved declarations differ from their recorded digests")
    decl = GameDeclarations(Path(data["game"]), Path(data["catalog_path"]), _json(readers_bytes)["readers"],
                            ActionCatalog.model_validate(_json(catalog_bytes)), data["source"], readers_bytes, catalog_bytes)
    return ThreeStudy(decl, data["session"], data["initial_observation"], data["entity"], data["entity_trials"],
                      data["attempts"], data["stop"], data["image"], data["launch"], data["fatal_error"])


def _reading(observation: dict[str, Any], alias: str) -> tuple[bool, Any, str | None]:
    errors = {item["reader"]: item["error"] for item in observation.get("reader_errors", [])}
    if alias in errors:
        return False, None, errors[alias]
    public = observation.get("public") or {}
    if alias not in public:
        return False, None, "not published in the observation"
    return True, public[alias], None


def _descriptor(alias: str, value: Any, hints: dict[str, Any]) -> dict[str, Any]:
    if value is None:
        kind, state = None, "null"
    elif isinstance(value, bool):
        kind, state = "boolean", "present"
    elif isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError(f"reader {alias!r} is not finite")
        kind, state = "number", "present"
    elif isinstance(value, str):
        kind, state = "string", "present"
    elif isinstance(value, list):
        if any(isinstance(item, float) and not math.isfinite(item) for item in value):
            raise ValueError(f"reader {alias!r} is not finite")
        numeric = all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in value)
        kind, state = (f"vector{len(value)}" if numeric and len(value) in (2, 3, 4) else "other"), "present"
    else:
        kind, state = "other", "present"
    return {"id": "public." + alias, "field_type": None, "value_state": state, "value_kind": kind,
            "author_hints": hints}


def _control_id(item: Any) -> str:
    return f"{item.device.lower()}_{item.control.lower()}"


def _input(item: Any) -> dict[str, Any]:
    return {"control_id": _control_id(item), **item.model_dump(mode="json")}


def _empty_calibration(item: Any) -> dict[str, Any]:
    return {"control": _control_id(item), "bindings": [], "resolution": "declared_three_input",
            "device": item.device, "control_name": item.control, "control_type": control_type(item.device, item.control),
            "declared_x": item.x, "declared_y": item.y, "holds": [],
            "lowest_tested_effective_requested_hold_s": None, "shortest_observed_effective_hold_s": None,
            "minimum_hold_scope": "not measured", "inverted_relative_to_entity_basis": None,
            "signed_basis_projections": [], "dead_zone": None, "dead_zone_reason": "not measured",
            "sensitivity": None, "sensitivity_reason": "not measured"}


def compile_study(study: ThreeStudy) -> CompiledStudy:
    if study.fatal_error:
        raise ValueError(f"fatal acquisition prevents publication: {study.fatal_error}")
    stop = StopResult.model_validate(study.stop)
    if stop.release_verified is not True or stop.session != study.session:
        raise ValueError("the measurement session did not stop with verified release")
    decl = study.declarations
    readers, offered_fields, unoffered = [], [], []
    for alias, hints in decl.readers.items():
        known, value, reason = _reading(study.initial_observation, alias)
        field_id = "public." + alias
        if not known:
            unoffered.append({"id": field_id, "reason": reason})
            continue
        descriptor = _descriptor(alias, value, hints)
        offered_fields.append({"id": field_id, "source_field_id": field_id, "field_type": None})
        readers.append({"alias": alias, "selection_id": field_id, "field_id": field_id, "why": hints["why"],
                        "declared_type": None, "kind": field_kind(descriptor), "descriptor": descriptor,
                        "freshness": None, "freshness_reason": "freshness was not measured", "freshness_evidence": None})
    if not readers:
        raise ValueError("none of the declared readers was readable in the measurement observation")
    controls: dict[str, dict[str, Any]] = {}
    actions, skills, all_calibrations, all_evidence, all_trials = [], [], [], [], []
    for name, definition in decl.catalog.actions.items():
        attempts = [entry for entry in study.attempts if entry["name"] == name]
        declaration = input_probe._catalog_declaration(decl.catalog, name)
        evidence = None
        motion_trials = []
        for attempt in attempts:
            if attempt.get("declaration") != definition.model_dump(mode="json"):
                raise ValueError(f"{name!r} attempt does not match its declared inputs")
            records = [ExecutionRecord.model_validate(row) for row in attempt.get("execution", [])]
            if not records:
                continue
            if any(record.session != study.session for record in records):
                raise ValueError(f"{name!r} execution came from another session")
            if any(record.action_id != attempt.get("action_id") for record in records):
                raise ValueError(f"{name!r} execution is not bound to its request")
            judged = input_probe.evaluate_attempt(declaration, records, None)
            before_observation = attempt.get("before_observation") or {}
            bound = (before_observation.get("session") == study.session
                     and before_observation.get("seq") == attempt.get("based_on_seq")
                     and records[-1].validated_against_seq == attempt.get("based_on_seq"))
            if judged["valid"] and bound:
                evidence = judged["execution_evidence"]
            idle, before, after = attempt.get("idle_start"), attempt.get("before_transform"), attempt.get("after_transform")
            if not definition.is_skill and len(definition.inputs) == 1 and idle and before and after:
                try:
                    measured = calibrate._trial_measure(before, after, idle, records, definition.default_lease_s)
                    measured["valid"] = measured["valid"] and judged["valid"] and bound
                    measured["view_delta"] = (attempt.get("view") or {}).get("turn")
                    motion_trials.append(measured)
                    all_trials.append(measured)
                except ValueError:
                    pass
        inputs = definition.inputs if not definition.is_skill else [i for step in definition.steps for i in step.inputs]
        for item in inputs:
            ident = _control_id(item)
            row = {"id": ident, "device": item.device, "control": item.control,
                   "control_type": control_type(item.device, item.control), "bindings": [], "resolution": "declared_three_input"}
            if ident in controls and controls[ident] != row:
                raise ValueError(f"physical control id collision: {ident}")
            controls[ident] = row
        if evidence is not None:
            all_evidence.append(evidence)
        if not definition.is_skill:
            calibrations = [_empty_calibration(item) for item in definition.inputs]
            if len(definition.inputs) == 1:
                shape = dict(calibrations[0], x=definition.inputs[0].x, y=definition.inputs[0].y)
                summary = calibrate.summarize_action(shape, motion_trials, holds=(definition.default_lease_s,))
                calibrations[0] = summary
                effect = summary["holds"][0]["effective_in_all_repeats"]
            else:
                effect = None
            all_calibrations.extend(calibrations)
            actions.append({"name": name, "intent": "unknown", "inputs": [_input(i) for i in definition.inputs],
                            "hold_s": definition.default_lease_s, "tap_hold_s": None, "preconditions": [],
                            "calibrations": calibrations, "axis": any(control_type(i.device, i.control) in AXIS_CONTROLS for i in definition.inputs),
                            "max_hold_s": None, "hold_effect": effect, "execution_evidence": evidence,
                            "execution_supported": evidence is not None})
        else:
            unique = {_control_id(i): i for i in inputs}
            calibrations = [_empty_calibration(i) for i in unique.values()]
            skills.append({"name": name, "purpose": "unknown", "steps": [{"inputs": [_input(i) for i in step.inputs],
                           "hold_s": step.hold_s, "tap_hold_s": None, "wait_s": step.wait_s} for step in definition.steps],
                           "max_s": definition.max_s, "calibrations": calibrations, "execution_evidence": evidence,
                           "execution_supported": evidence is not None})
            all_calibrations.extend(calibrations)
    source = json.dumps(study_dict(study), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    coverage = ["no model selection or model gap report ran; empty model gap lists are unknown, not gap-free",
                "readers and actions come from game declarations", "freshness, reset and clock were not measured",
                "source and study: " + json.dumps(decl.source, sort_keys=True), "session: " + study.session]
    coverage.extend(f"reader {row['id']} unread: {row['reason']}" for row in unoffered)
    data = {"schema_version": 2, "packet_sha256": _hash(source), "entity_id": study.entity["id"],
            "entity": study.entity, "inventory_caps": {}, "discovery_caps": {}, "image": study.image,
            "readers": readers, "actions": actions, "skills": skills, "progress": [], "invariants": [],
            "gaps": {"scope": "model_report_relative_to_offered_and_selected_evidence", "unobservable": [],
                     "uncontrollable": [], "unjudgeable": [], "coverage": coverage},
            "selection": {"offered_fields": offered_fields, "unselected_field_ids": [], "unoffered_fields": unoffered,
                          "offered_controls": list(controls.values()), "unoffered_controls": [], "unsupported_bindings": [],
                          "reader_cap": 16, "reader_cap_reached": len(readers) == 16, "action_cap": 18,
                          "action_cap_reached": len(actions) == 18, "skill_cap": 6, "skill_cap_reached": len(skills) == 6,
                          "gap_caps_reached": {"unobservable": False, "uncontrollable": False, "unjudgeable": False}},
            "reset_bindings": None, "reset_bindings_reason": "reset was not measured"}
    manifest_checks.refresh_execution_coverage(data)
    manifest = PreflightManifest.model_validate(data)
    problems = manifest_checks.validate(manifest)
    if problems:
        raise ValueError("manifest validation failed: " + "; ".join(problems))
    if not any(choice.source.value == "manifest" for choice in build_choices(manifest).choices):
        raise ValueError("manifest offers no executable choice")
    calibration = CalibrationRecord.model_validate({"schema_version": 1, "status": "inconclusive",
        "error": "freshness, clock and reset were not measured", "entity_id": study.entity["id"], "entity": study.entity,
        "coordinate_frame": "three bridge world frame", "units": "three_world_unit", "physical_unit": None,
        "plan": {"session": study.session}, "action_cap": {"limit": 18, "observed": len(actions), "returned": len(actions), "truncated": False},
        "actions": [], "unsupported_bindings": [], "discovery_coverage": {}, "idle_fields": [], "input_field_segments": [],
        "field_sampling_gap": "not measured", "trials": all_trials, "action_summaries": all_calibrations,
        "execution_evidence": all_evidence, "freshness": None, "clock": None, "reset": None,
        "runtime_errors": None, "stop": study.stop})
    return CompiledStudy(manifest, calibration, {"entity_id": study.entity["id"], "entity": study.entity,
                         "trials": study.entity_trials}, export(manifest))
