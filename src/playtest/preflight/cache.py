"""Conservative authored-project identity and descriptor resolution for preflight.

The packet hash identifies one model request, including transient observations.  It is
not a cache key.  These helpers deliberately fail closed when discovery is truncated or
reports an error, or when a selected descriptor cannot be resolved uniquely in a later
inspection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playtest.contracts import bridge as bridge_contract, calibration as calibration_contract
from playtest.contracts import action_catalog as action_catalog_contract
from playtest.contracts.bridge import InspectResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight import calibrate, calibration_record, entity, freshness, manifest, packet, provider, stages
from playtest.roles import checks, preflight, strict


FINGERPRINT_VERSION = 1
_INVENTORY_CAPS = frozenset({"packages", "scenes", "scripts", "input_assets", "legacy_axes"})
_DISCOVERY_CAPS = frozenset({"scenes", "game_objects", "components", "fields", "bindings",
                             "input_asset_actions", "input_asset_bindings", "source_files",
                             "source_characters", "il_writes", "il_issues"})
_IL_SECTIONS = ("il_issues", "il_write_sets", "il_unlisted_writes")
_BINDING_KEYS = (
    "source_field_name", "source_declaring_type", "source_declaring_assembly", "source_field_type",
    "source_input_kind", "binding_scope", "asset_path", "map_name", "action_name", "action_type",
    "expected_control_type", "action_interactions", "action_processors", "binding_name", "binding_path",
    "binding_interactions", "binding_processors", "binding_groups", "binding_action_name",
    "binding_is_composite", "binding_is_part_of_composite",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_file(path: Path) -> bytes:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("project files must not be symbolic links or junctions")
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("a project file changed while it was fingerprinted")
    return data


def _walk(root: Path, prefix: str) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ValueError(f"the authored {prefix} directory is missing or is a link")
    result: dict[str, str] = {}

    def on_error(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
        base = Path(directory)
        for name in dirs:
            child = base / name
            if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                raise ValueError("authored project directories must not contain links")
        # Git internals are not game assets; the file contents in the worktree are hashed.
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in sorted(files):
            file = base / name
            key = f"{prefix}/{file.relative_to(root).as_posix()}"
            result[key] = hashlib.sha256(_read_file(file)).hexdigest()
    return result


def _normalize_local_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_local_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_local_paths(item) for item in value]
    if isinstance(value, str) and value.startswith("file:"):
        return "file:<local-package>"
    return value


@dataclass(frozen=True)
class AuthoredSnapshot:
    """File digests only; no absolute paths, source contents or runtime values."""

    files: dict[str, str]
    started_utc: datetime | None = None
    finished_utc: datetime | None = None

    @property
    def sha256(self) -> str:
        return _digest(self.files)


def capture_authored(project: Path) -> AuthoredSnapshot:
    """Hash project assets, settings and local package contents without writing to the game.

    A local package's logical name, rather than its machine-specific file path, identifies
    its files. Unknown local dependencies in the lock file stop capture rather than being
    omitted. Call before and after preflight; matching snapshots detect changes during it.
    """
    started = datetime.now(timezone.utc)
    project = project.resolve(strict=True)
    files: dict[str, str] = {}
    for name in ("Assets", "Packages", "ProjectSettings"):
        files.update(_walk(project / name, name))
    package_file = project / "Packages" / "manifest.json"
    lock_file = project / "Packages" / "packages-lock.json"
    packages = json.loads(_read_file(package_file))
    dependencies = packages.get("dependencies") if isinstance(packages, dict) else None
    if not isinstance(dependencies, dict):
        raise ValueError("the package manifest has no dependency map")
    local: dict[str, str] = {}
    for name, value in dependencies.items():
        if isinstance(value, str) and value.startswith("file:"):
            if not isinstance(name, str) or not name or not value[5:]:
                raise ValueError("a local package dependency is invalid")
            local[name] = value
            location = Path(value[5:])
            if not location.is_absolute():
                location = project / "Packages" / location
            files.update(_walk(location.resolve(strict=True), f"local-packages/{name}"))
    files["Packages/manifest.json"] = _digest(_normalize_local_paths(packages))
    if lock_file.exists():
        lock = json.loads(_read_file(lock_file))
        locked = lock.get("dependencies") if isinstance(lock, dict) else None
        if not isinstance(locked, dict):
            raise ValueError("the package lock has no dependency map")
        for name, row in locked.items():
            if isinstance(row, dict) and row.get("source") == "local":
                if name not in local or row.get("version") != local[name]:
                    raise ValueError("a locked local package is missing from or differs from the manifest")
        files["Packages/packages-lock.json"] = _digest(_normalize_local_paths(lock))
    return AuthoredSnapshot(dict(sorted(files.items())), started, datetime.now(timezone.utc))


def _complete(snapshot: InspectResult, stage: str, required: frozenset[str]) -> None:
    """Require error-free, untruncated coverage; only recorded IL issues may leave it uncertain.

    The IL scanner records every indirect field address (common for struct fields) as an
    issue and then marks its downstream IL caps uncertain. That is a deterministic property
    of the compiled code, so it does not disqualify a source; the IL evidence itself is part
    of the fingerprint instead. Any other unknown coverage, and any truncation, does.
    """
    if snapshot.stage != stage or snapshot.errors:
        raise ValueError(f"{stage} inspection has errors or the wrong stage")
    if not required <= snapshot.caps.keys():
        raise ValueError(f"{stage} inspection is missing coverage")
    for name, cap in snapshot.caps.items():
        if cap.truncated is True:
            raise ValueError(f"{stage} inspection truncated its {name} coverage")
        if not name.startswith("il_") and (cap.observed is None or cap.truncated is not False):
            raise ValueError(f"{stage} inspection has incomplete {name} coverage")
    sections = (_INVENTORY_CAPS if stage == "inventory" else
                frozenset({"scenes", "game_objects", "components", "fields", "bindings",
                           "input_asset_actions", "input_asset_bindings", "il_issues", "il_writes"}))
    for name in sections:
        rows = snapshot.data.get("il_write_sets" if name == "il_writes" else name)
        if name == "il_writes":
            # Write sets group the returned writes by field; count the writers they hold.
            if not isinstance(rows, list) or any(not isinstance(row, dict) or
                    not isinstance(row.get("direct_writers"), list) for row in rows):
                raise ValueError("discovery il_write_sets are malformed")
            unlisted = snapshot.data.get("il_unlisted_writes")
            if not isinstance(unlisted, list) or any(not isinstance(row, dict) or
                    not isinstance(row.get("direct_writers"), list) for row in unlisted):
                raise ValueError("discovery il_unlisted_writes are malformed")
            count = sum(len(row["direct_writers"]) for row in rows + unlisted)
            if count != snapshot.caps[name].returned:
                raise ValueError("discovery il_writes does not match its coverage")
            continue
        if not isinstance(rows, list) or len(rows) != snapshot.caps[name].returned:
            raise ValueError(f"{stage} {name} does not match its coverage")
    if stage == "discovery":
        complete = snapshot.data.get("il_complete")
        if not isinstance(complete, bool) or (not complete and not snapshot.data["il_issues"]):
            raise ValueError("discovery's IL analysis is incomplete for an unrecorded reason")


def fingerprint(authored: AuthoredSnapshot, inventory: InspectResult, discovery: InspectResult,
                *, model: str) -> dict[str, Any]:
    """Identify authored structure, inspection bounds and the exact selection policy.

    This omits session ids, object ordering, field values and images. A hit additionally
    requires resolving frozen selections against a current Editor inspection.
    """
    _complete(inventory, "inventory", _INVENTORY_CAPS)
    _complete(discovery, "discovery", _DISCOVERY_CAPS)
    if model not in provider.MODEL_PRICES:
        raise ValueError("the preflight model is unsupported")
    unity = inventory.data.get("unity_version")
    if not isinstance(unity, str) or not unity:
        raise ValueError("the inspected Unity version is unknown")
    input_handling = inventory.data.get("input_handling")
    if input_handling not in ("legacy", "input_system", "both"):
        raise ValueError("the inspected input handling is unknown")
    inventory_structure = {"input_handling": input_handling}
    for name in _INVENTORY_CAPS:
        rows = inventory.data[name]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"inventory has malformed {name} descriptors")
        if name in ("scenes", "scripts", "input_assets") and any(
                not isinstance(row.get("path"), str) or not row["path"] for row in rows):
            raise ValueError(f"inventory has {name} without authored paths")
        inventory_structure[name] = sorted(rows, key=_canonical)
    bindings = []
    for section in ("bindings", "input_asset_bindings"):
        rows = discovery.data.get(section)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"discovery has invalid {section}")
        if len(rows) != discovery.caps[section].returned:
            raise ValueError(f"discovery {section} does not match its coverage")
        bindings.extend({"section": section, **{key: row.get(key) for key in _BINDING_KEYS}} for row in rows)
    bindings.sort(key=_canonical)
    # The IL evidence is derived from the compiled code, which the authored files do not
    # fully determine (the active build target's defines are not authored, for example).
    il: dict[str, Any] = {"il_complete": discovery.data["il_complete"], "il_scope": discovery.data.get("il_scope"),
                          "caps": {k: v.model_dump(mode="json") for k, v in sorted(discovery.caps.items())
                                   if k.startswith("il_")}}
    for section in _IL_SECTIONS:
        rows = discovery.data.get(section)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"discovery has invalid {section}")
        il[section] = sorted(rows, key=_canonical)
    policy_modules = (preflight, checks, strict, packet, stages, provider, manifest, calibrate,
                      calibration_record, entity, freshness, bridge_contract, calibration_contract,
                      action_catalog_contract)
    policy = {module.__name__: hashlib.sha256(_read_file(Path(module.__file__))).hexdigest()
              for module in policy_modules}
    policy[__name__] = hashlib.sha256(_read_file(Path(__file__))).hexdigest()
    schema = PreflightManifest.model_json_schema()
    identity = {
        "version": FINGERPRINT_VERSION, "authored_sha256": authored.sha256,
        "unity_version": unity, "model": model,
        "inventory_structure_sha256": _digest(inventory_structure),
        "manifest_schema_sha256": _digest(schema),
        "answer_schema_sha256": _digest(preflight.StageAnswer.model_json_schema()),
        "prompt_and_policy_sha256": _digest(policy),
        "inventory_caps": {k: v.limit for k, v in sorted(inventory.caps.items())},
        "discovery_caps": {k: v.limit for k, v in sorted(discovery.caps.items())},
        "bindings_sha256": _digest(bindings),
        "il_evidence_sha256": _digest(il),
    }
    return {"sha256": _digest(identity), "identity": identity}


def _rows(data: dict[str, Any], name: str) -> dict[str, dict[str, Any]]:
    rows = data.get(name)
    if not isinstance(rows, list):
        raise ValueError(f"discovery is missing {name}")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in indexed:
            raise ValueError(f"discovery has invalid or duplicate {name} ids")
        indexed[row["id"]] = row
    return indexed


def _object_key(row: dict[str, Any]) -> dict[str, str]:
    scene, path = row.get("scene_path"), row.get("path")
    if not isinstance(scene, str) or not scene or not isinstance(path, str) or not path:
        raise ValueError("a selected object has no scene path or hierarchy path")
    return {"scene_path": scene, "object_path": path}


def _field_key(row: dict[str, Any], objects: dict[str, dict[str, Any]],
               components: dict[str, dict[str, Any]]) -> dict[str, Any]:
    obj = objects.get(row.get("object_id"))
    component = components.get(row.get("component_id"))
    if obj is None or component is None or component.get("object_id") != row.get("object_id"):
        raise ValueError("a selected field has no matching object and component")
    key = {**_object_key(obj), "component_index": component.get("index"),
           "component_type": component.get("type_id"), "declaration_id": row.get("declaration_id"),
           "field_type": row.get("field_type"), "field_type_assembly": row.get("field_type_assembly")}
    if (isinstance(key["component_index"], bool) or not isinstance(key["component_index"], int) or
            key["component_index"] < 0 or any(not isinstance(key[name], str) or not key[name]
            for name in ("component_type", "declaration_id", "field_type", "field_type_assembly"))):
        raise ValueError("a selected field has an incomplete structural descriptor")
    return key


def resolve_readers(manifest_value: PreflightManifest, original: InspectResult,
                    current: InspectResult) -> dict[str, str]:
    """Return alias -> current Editor id; never replace or mutate a manifest selection."""
    _complete(original, "discovery", _DISCOVERY_CAPS)
    _complete(current, "discovery", _DISCOVERY_CAPS)
    old_objects, old_components, old_fields = (_rows(original.data, k) for k in ("game_objects", "components", "fields"))
    new_objects, new_components, new_fields = (_rows(current.data, k) for k in ("game_objects", "components", "fields"))
    old_paths: dict[str, int] = {}
    new_paths: dict[str, int] = {}
    for objects, counts in ((old_objects, old_paths), (new_objects, new_paths)):
        for obj in objects.values():
            try:
                key = _canonical(_object_key(obj))
            except ValueError:
                continue
            counts[key] = counts.get(key, 0) + 1
    index: dict[str, list[str]] = {}
    for ident, row in new_fields.items():
        try:
            key = _field_key(row, new_objects, new_components)
        except ValueError:
            continue
        index.setdefault(_canonical(key), []).append(ident)
    resolved: dict[str, str] = {}
    for reader in manifest_value.readers:
        row = old_fields.get(reader.field_id)
        if row is None or {k: v for k, v in row.items() if k not in ("value", "enum_name")} != reader.descriptor:
            raise ValueError(f"reader {reader.alias} does not match its original descriptor")
        key = _field_key(row, old_objects, old_components)
        object_key = _canonical({"scene_path": key["scene_path"], "object_path": key["object_path"]})
        matches = index.get(_canonical(key), [])
        if old_paths.get(object_key) != 1 or new_paths.get(object_key) != 1 or len(matches) != 1:
            raise ValueError(f"reader {reader.alias} no longer resolves uniquely")
        current_field = new_fields[matches[0]]
        # A reader that was null at discovery keeps its unknown kind; it must still be null.
        if not packet.field_offered(current_field) or packet.field_kind(current_field) != reader.kind:
            raise ValueError(f"reader {reader.alias} is unreadable or has a different current kind")
        resolved[reader.alias] = matches[0]
    return resolved


def resolve_entity(manifest_value: PreflightManifest, original: InspectResult,
                   current: InspectResult) -> str:
    """Resolve the calibrated entity by frozen scene and hierarchy descriptors."""
    _complete(original, "discovery", _DISCOVERY_CAPS)
    _complete(current, "discovery", _DISCOVERY_CAPS)
    old_objects = _rows(original.data, "game_objects")
    new_objects = _rows(current.data, "game_objects")
    old = old_objects.get(manifest_value.entity_id)
    if old is None or manifest_value.entity.get("id") != manifest_value.entity_id:
        raise ValueError("the calibrated entity does not match its original inspection")
    key = _object_key(old)
    if _object_key(manifest_value.entity) != key:
        raise ValueError("the manifest entity descriptor differs from discovery")
    def matches(objects: dict[str, dict[str, Any]]) -> list[str]:
        found = []
        for ident, obj in objects.items():
            try:
                if _object_key(obj) == key:
                    found.append(ident)
            except ValueError:
                continue
        return found
    previous, current_matches = matches(old_objects), matches(new_objects)
    if previous != [manifest_value.entity_id] or len(current_matches) != 1:
        raise ValueError("the calibrated entity no longer resolves uniquely")
    return current_matches[0]


def _binding_identity(binding: dict[str, Any], fields: dict[str, dict[str, Any]],
                      objects: dict[str, dict[str, Any]], components: dict[str, dict[str, Any]]) -> str:
    """Binding identity excludes within-run ids but retains authored and effective paths."""
    source = binding.get("source_field_id")
    if source is not None:
        if not isinstance(source, str) or source not in fields:
            raise ValueError("a selected binding has no source field descriptor")
        owner = _field_key(fields[source], objects, components)
    else:
        owner = None
        if not isinstance(binding.get("asset_path"), str) or not binding["asset_path"]:
            raise ValueError("a selected binding has no stable source")
    for name in ("binding_id", "action_id", "binding_effective_path"):
        if not isinstance(binding.get(name), str) or not binding[name]:
            raise ValueError(f"a selected binding has no {name}")
    return _canonical({**{key: binding.get(key) for key in _BINDING_KEYS}, "owner": owner,
                       "binding_id": binding["binding_id"], "action_id": binding["action_id"],
                       "binding_effective_path": binding["binding_effective_path"]})


def resolve_controls(manifest_value: PreflightManifest, original: InspectResult,
                     current: InspectResult, current_packet: packet.Packet) -> dict[str, str]:
    """Require each selected physical control's frozen bindings to still match exactly."""
    _complete(original, "discovery", _DISCOVERY_CAPS)
    _complete(current, "discovery", _DISCOVERY_CAPS)
    selected = {i.control_id for action in manifest_value.actions for i in action.inputs}
    selected.update(i.control_id for skill in manifest_value.skills for step in skill.steps for i in step.inputs)
    resolved: dict[str, str] = {}
    all_sources = []
    for snapshot in (original, current):
        objects, components, fields = (_rows(snapshot.data, key) for key in ("game_objects", "components", "fields"))
        bindings = _rows(snapshot.data, "bindings")
        assets = _rows(snapshot.data, "input_asset_bindings")
        if set(bindings) & set(assets):
            raise ValueError("discovery has duplicate binding ids across sections")
        bindings.update(assets)
        choices, _ = calibrate.discover_actions(snapshot.data)
        by_control = {item["control"]: item for item in choices}
        if len(by_control) != len(choices):
            raise ValueError("discovery has ambiguous controls")
        all_sources.append((objects, components, fields, bindings, by_control))
    for control in selected:
        if control not in current_packet.discovery.control_types:
            raise ValueError(f"control {control} is not offered by the current calibration")
        signatures = []
        for objects, components, fields, bindings, choices in all_sources:
            choice = choices.get(control)
            if choice is None:
                raise ValueError(f"control {control} no longer resolves")
            identities = []
            for reference in choice["bindings"]:
                row = bindings.get(reference.get("binding_id"))
                if row is None:
                    raise ValueError(f"control {control} has an unresolved binding")
                identities.append(_binding_identity(row, fields, objects, components))
            if not identities or len(identities) != len(set(identities)):
                raise ValueError(f"control {control} has ambiguous binding descriptors")
            signatures.append((choice["resolution"], sorted(identities)))
        if signatures[0] != signatures[1]:
            raise ValueError(f"control {control} bindings changed")
        resolved[control] = control
    return resolved


def undriven_axes(value: PreflightManifest) -> list[str]:
    """Offered analog axes that no declared action drives make a selection incomplete.

    Controls the bridge could not offer at all are bridge gaps that a new model answer
    cannot close, so they do not count; an offered axis left undriven could be. This is a
    selection-coverage fact, not a claim that the driving declaration is executable.
    """
    driven = {item.control_id for action in value.actions for item in action.inputs}
    driven.update(item.control_id for skill in value.skills for step in skill.steps for item in step.inputs)
    return sorted(row["id"] for row in value.selection.offered_controls
                  if row.get("control_type") in preflight.AXIS_CONTROLS and row["id"] not in driven)


def replay_selections(manifest_value: PreflightManifest, original: InspectResult,
                      current: InspectResult, current_packet: packet.Packet) -> tuple[PreflightManifest, dict[str, Any]]:
    """Rebind an already validated selection to a new packet without choosing anything.

    The original cache entry and its ledger must be validated before this function is
    called. Only session-specific field identifiers are remapped. Every declared action,
    chord and skill is replayed as declared, including those the bridge cannot execute;
    their execution support, evidence and hold effects are re-derived from the current
    packet, so neither the cached calibration nor cached execution receipts are presented
    as proof in the new session.
    """
    problems = manifest.validate(manifest_value)
    if problems:
        raise ValueError("the cached manifest is invalid: " + "; ".join(problems))
    reader_ids = resolve_readers(manifest_value, original, current)
    entity_id = resolve_entity(manifest_value, original, current)
    controls = resolve_controls(manifest_value, original, current, current_packet)
    payload = json.loads(current_packet.text)
    if payload["calibration"]["entity_id"] != entity_id:
        raise ValueError("the current calibration is not bound to the resolved entity")
    tokens = {source: token for token, source in current_packet.field_bindings.items()}
    if len(tokens) != len(current_packet.field_bindings) or any(source not in tokens for source in reader_ids.values()):
        raise ValueError("the resolved readers are not offered uniquely by the current packet")
    readers = [{"field_id": tokens[reader_ids[row.alias]], "alias": row.alias, "why": row.why}
               for row in manifest_value.readers]
    actions = [{"name": a.name, "intent": a.intent,
                "inputs": [{"control_id": i.control_id, "x": i.x, "y": i.y} for i in a.inputs],
                "hold_s": a.hold_s, "preconditions": [p.model_dump(mode="json") for p in a.preconditions]}
               for a in manifest_value.actions]
    skills = [{"name": s.name, "purpose": s.purpose, "max_s": s.max_s,
               "steps": [{"inputs": [{"control_id": i.control_id, "x": i.x, "y": i.y} for i in step.inputs],
                          "hold_s": step.hold_s, "wait_s": step.wait_s} for step in s.steps]}
              for s in manifest_value.skills]
    a4 = preflight.StageAnswer.model_validate({"stage_4": {"readers": readers,
        "unobservable": manifest_value.gaps.unobservable}, "stage_5": None, "stage_6": None})
    a5 = preflight.StageAnswer.model_validate({"stage_4": None, "stage_5": {"actions": actions,
        "skills": skills, "uncontrollable": manifest_value.gaps.uncontrollable}, "stage_6": None})
    a6 = preflight.StageAnswer.model_validate({"stage_4": None, "stage_5": None, "stage_6": {
        "progress": [p.model_dump(mode="json") for p in manifest_value.progress],
        "invariants": [i.model_dump(mode="json") for i in manifest_value.invariants],
        "unjudgeable": manifest_value.gaps.unjudgeable}})
    run = stages.RunResult(stages.StageResult(a4, ()), stages.StageResult(a5, ()), stages.StageResult(a6, ()))
    value = manifest.assemble(current_packet, run)
    if undriven_axes(value):
        raise ValueError("the selection is incomplete: offered analog axes have no executable action")
    return value, {"entity_id": entity_id, "reader_ids": reader_ids, "controls": controls, "model_calls": 0}


def _snapshot_data(snapshot: AuthoredSnapshot) -> dict[str, Any]:
    return {"files": snapshot.files,
            "started_utc": snapshot.started_utc.isoformat() if snapshot.started_utc else None,
            "finished_utc": snapshot.finished_utc.isoformat() if snapshot.finished_utc else None}


def _snapshot_from_data(value: Any) -> AuthoredSnapshot:
    if not isinstance(value, dict) or set(value) != {"files", "started_utc", "finished_utc"}:
        raise ValueError("the authored snapshot is malformed")
    files = value["files"]
    if (not isinstance(files, dict) or not files or any(not isinstance(k, str) or not k or
            not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
            for k, v in files.items())):
        raise ValueError("the authored snapshot file hashes are malformed")
    try:
        started = datetime.fromisoformat(value["started_utc"])
        finished = datetime.fromisoformat(value["finished_utc"])
    except (TypeError, ValueError):
        raise ValueError("the authored snapshot timestamps are unknown") from None
    if started.utcoffset() is None or finished.utcoffset() is None or finished < started:
        raise ValueError("the authored snapshot timestamps are invalid")
    return AuthoredSnapshot(files, started, finished)


def _check_window(before: AuthoredSnapshot, after: AuthoredSnapshot, inventory: InspectResult,
                  discovery: InspectResult, calibration: CalibrationRecord) -> None:
    if (before.started_utc is None or before.finished_utc is None or
            after.started_utc is None or after.finished_utc is None):
        raise ValueError("before and after snapshots require capture timestamps")
    if before.files != after.files:
        raise ValueError("authored project files changed during preflight")
    if (before.started_utc > before.finished_utc or before.finished_utc > after.started_utc or
            after.started_utc > after.finished_utc):
        raise ValueError("authored snapshot capture order is invalid")
    if calibration.stop is None:
        raise ValueError("calibration has no terminal timestamp")
    inspections = (inventory.wall_utc, discovery.wall_utc, calibration.stop.wall_utc)
    if any(t.utcoffset() is None or t < before.finished_utc or t > after.started_utc for t in inspections):
        raise ValueError("the authored snapshots do not bracket the inspection and calibration")


def _image_data(image: packet.Screenshot | None) -> dict[str, Any] | None:
    if image is None:
        return None
    image.descriptor()
    return {"data": base64.b64encode(image.data).decode("ascii"), "mime": image.mime,
            "provenance": image.provenance, "same_session": image.same_session,
            "capture_game_s": image.capture_game_s}


def _image_from_data(value: Any) -> packet.Screenshot | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"data", "mime", "provenance", "same_session", "capture_game_s"}:
        raise ValueError("the cached screenshot is malformed")
    try:
        raw = base64.b64decode(value["data"], validate=True)
    except (ValueError, TypeError):
        raise ValueError("the cached screenshot bytes are malformed") from None
    image = packet.Screenshot(raw, value["mime"], value["provenance"], value["same_session"], value["capture_game_s"])
    image.descriptor()
    return image


def split_ledger(ledger: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Require an ordered, fully settled reservation for every recorded attempt."""
    if not isinstance(ledger, list) or not ledger or len(ledger) % 2:
        raise ValueError("the source ledger has an unresolved or missing request")
    rows, reservations = [], []
    for index in range(0, len(ledger), 2):
        reservation, attempt = ledger[index:index + 2]
        number = index // 2 + 1
        if (not isinstance(reservation, dict) or not isinstance(attempt, dict) or
                reservation.get("kind") != "reservation" or attempt.get("kind") != "attempt" or
                isinstance(reservation.get("request"), bool) or reservation.get("request") != number or
                isinstance(attempt.get("request"), bool) or attempt.get("request") != number or
                not isinstance(reservation.get("request_sha256"), str) or
                len(reservation["request_sha256"]) != 64):
            raise ValueError("the source ledger reservations and attempts are not paired and ordered")
        reservations.append(reservation)
        rows.append(attempt)
    return rows, reservations


def create_entry(before: AuthoredSnapshot, after: AuthoredSnapshot, inventory: InspectResult,
                 discovery: InspectResult, calibration: CalibrationRecord, image: packet.Screenshot | None,
                 value: PreflightManifest, rows: list[dict[str, Any]], reservations: list[dict[str, Any]],
                 *, model: str) -> dict[str, Any]:
    """Seal a completed 4–6 selection with its original evidence and ledger.

    This caches model selections only. Stage 7 remains a separate live measurement;
    a cache hit does not claim that an action or reset was verified in the new session.
    """
    _check_window(before, after, inventory, discovery, calibration)
    if calibration.status != "measured_with_gaps" or calibration.error is not None:
        raise ValueError("completed calibration is required for a cache entry")
    identity = fingerprint(before, inventory, discovery, model=model)
    original_packet = packet.build(inventory, discovery, calibration, image=image)
    if len(rows) != len(reservations):
        raise ValueError("the source ledger has unpaired requests")
    paired = [item for pair in zip(reservations, rows) for item in pair]
    split_ledger(paired)
    problems = manifest.validate_evidence(value, original_packet) + manifest.validate_ledger(
        value, original_packet, rows, reservations, expected_model=model)
    if problems:
        raise ValueError("the source manifest or ledger is invalid: " + "; ".join(problems))
    replayed, _ = replay_selections(value, discovery, discovery, original_packet)
    if manifest._without_proof(replayed.model_dump(mode="json")) != manifest._without_proof(
            value.model_dump(mode="json")):
        raise ValueError("the source selection cannot be reconstructed exactly")
    source = {"before": _snapshot_data(before), "after": _snapshot_data(after),
              "inventory": inventory.model_dump(mode="json"), "discovery": discovery.model_dump(mode="json"),
              "calibration": calibration.model_dump(mode="json"), "image": _image_data(image),
              "manifest": value.model_dump(mode="json"), "rows": rows, "reservations": reservations,
              "model": model}
    return {"version": 1, "fingerprint": identity, "source_sha256": _digest(source), "source": source}


def replay_entry(entry: dict[str, Any], authored: AuthoredSnapshot, inventory: InspectResult,
                 discovery: InspectResult, calibration: CalibrationRecord, image: packet.Screenshot | None,
                 *, model: str) -> tuple[PreflightManifest, dict[str, Any]]:
    """Revalidate a sealed source and bind its unchanged choices to fresh evidence.

    The current inventory, discovery and calibration are this preflight's own Editor
    inspections; the caller must check that its authored snapshots bracket them. The
    frozen descriptors are resolved against that discovery, not against the source's.
    """
    if not isinstance(entry, dict) or set(entry) != {"version", "fingerprint", "source_sha256", "source"} or entry["version"] != 1:
        raise ValueError("the cache entry has an unknown format")
    source = entry["source"]
    if not isinstance(source, dict) or _digest(source) != entry["source_sha256"]:
        raise ValueError("the cache source hash does not match")
    if source.get("model") != model:
        raise ValueError("the cached selection used a different model")
    try:
        before, after = (_snapshot_from_data(source[k]) for k in ("before", "after"))
        old_inventory = InspectResult.model_validate(source["inventory"])
        old_discovery = InspectResult.model_validate(source["discovery"])
        old_calibration = CalibrationRecord.model_validate(source["calibration"])
        old_manifest = PreflightManifest.model_validate(source["manifest"])
        old_image = _image_from_data(source["image"])
        rows, reservations = source["rows"], source["reservations"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"the cache source is malformed: {exc}") from None
    if not isinstance(rows, list) or not isinstance(reservations, list):
        raise ValueError("the cached ledger is malformed")
    verified = create_entry(before, after, old_inventory, old_discovery, old_calibration, old_image,
                            old_manifest, rows, reservations, model=model)
    if verified != entry:
        raise ValueError("the cache source no longer reconstructs its identity")
    current_identity = fingerprint(authored, inventory, discovery, model=model)
    if current_identity != entry["fingerprint"]:
        raise ValueError("the authored fingerprint changed")
    if calibration.status != "measured_with_gaps" or calibration.error is not None:
        raise ValueError("the current calibration is incomplete")
    if (calibration.stop is None or old_calibration.stop is None or
            calibration.stop.session == old_calibration.stop.session or
            calibration.stop.wall_utc <= after.started_utc):
        raise ValueError("the cache requires a new calibration after the source preflight")
    # The capture harness reuses a fixed inspection session label, so only time separates runs.
    if inventory.wall_utc <= after.started_utc or discovery.wall_utc <= after.started_utc:
        raise ValueError("the cache requires a new inventory and discovery after the source preflight")
    current_packet = packet.build(inventory, discovery, calibration, image=image)
    value, mapping = replay_selections(old_manifest, old_discovery, discovery, current_packet)
    return value, {"fingerprint": current_identity["sha256"], "source_sha256": entry["source_sha256"],
                   "model": model, "stage7_verified": False, **mapping}


def write_entry(directory: Path, entry: dict[str, Any]) -> Path:
    """Publish a new local entry exclusively; never replace another preflight result."""
    identity = entry.get("fingerprint")
    key = identity.get("sha256") if isinstance(identity, dict) else None
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("the cache fingerprint is invalid")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (key + ".json")
    serialized = _canonical(entry) + "\n"
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".preflight-", suffix=".tmp", dir=directory)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link publishes a complete file exclusively; os.replace would overwrite a cache.
        os.link(temporary, path)
    except FileExistsError:
        raise ValueError("the cache entry already exists") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def read_entry(directory: Path, fingerprint_sha256: str) -> dict[str, Any] | None:
    if (len(fingerprint_sha256) != 64 or
            any(c not in "0123456789abcdef" for c in fingerprint_sha256)):
        raise ValueError("the cache fingerprint is invalid")
    path = directory / (fingerprint_sha256 + ".json")
    if not path.exists():
        return None
    value = json.loads(_read_file(path))
    if not isinstance(value, dict):
        raise ValueError("the cache entry is malformed")
    return value
