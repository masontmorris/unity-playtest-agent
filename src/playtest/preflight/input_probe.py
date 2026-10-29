"""Offline named-declaration probe for catalog actions and bounded skills.

This module is a measurement tool, not part of the S1 turn loop and not a manifest writer by
policy. It sends exactly the declarations named in a plan, repeats each one, and records every
success and failure as k/n. It makes no model calls and never chooses, ranks, substitutes or
repairs an action.

Inputs come from `--plan`; the modern catalog may be named in the plan or supplied through
UPA_INPUT_PROBE_CATALOG. A plan may also name a manifest to resolve declarations and to update
that run-specific manifest's exact `execution_evidence` after validation. All output stays
under `<repo>/runs` (UPA_REPO_ROOT, default current directory). A separate generic PlayMode
harness may start the bridge and run this module with `--project`, `--plan` and `--output`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import struct
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from playtest.contracts.action_catalog import (
    ActionCatalog,
    ActionDefinition,
    ActionStep,
    InputBinding,
    binding_requirements,
    evidence_problems,
    receipt_problems,
    step_requirements,
)
from playtest.contracts.bridge import InputExecutionEvidence
from playtest.contracts.common import Strict
from playtest.contracts.manifest import PreflightManifest
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError
from playtest.preflight import calibrate, manifest as manifest_module


NAME = r"^[a-z][a-z0-9_]{0,39}$"
PLAN_SCHEMA_VERSION = 1
MAX_REPEATS = 20
MAX_FRAME_BYTES = 32 * 1024 * 1024


class ProbeTarget(Strict):
    name: str = Field(pattern=NAME, description="A catalog action name or a manifest action/skill name")
    repeats: int = Field(ge=1, le=MAX_REPEATS, description="Attempts requested; every failure is retained")


class ProbeReader(Strict):
    alias: str = Field(pattern=NAME)
    field_id: str = Field(min_length=1, description="The exact Editor field id to sample")


class ProbePlan(Strict):
    schema_version: Literal[1]
    actions: list[ProbeTarget] = Field(min_length=1, max_length=14)
    catalog: str | None = Field(None, description="Modern catalog path; defaults to UPA_INPUT_PROBE_CATALOG")
    manifest: str | None = Field(None, description="Manifest path for declaration resolution and evidence update")
    update_manifest: str | None = Field(None, description="Run-specific manifest under runs/ to update")
    entity_id: str | None = Field(None, description="Entity to read transforms from; defaults to the manifest entity")
    scene_id: str | None = Field(None, description="Scene for field samples; defaults to the manifest entity scene")
    readers: list[ProbeReader] = Field(default_factory=list, description="Fields to sample; defaults to manifest readers")
    public_fields: list[str] = Field(default_factory=list, description="Observation public keys to sample")
    depends_on: list[str] = Field(default_factory=list, description="Public freshness fields sent unchanged")
    max_age_s: float = Field(2.0, gt=0, le=60)
    settle_s: float = Field(0.15, ge=0, le=10)

    @model_validator(mode="after")
    def _unique_names(self) -> "ProbePlan":
        names = [item.name for item in self.actions]
        if len(names) != len(set(names)):
            raise ValueError("a plan cannot name the same declaration twice")
        aliases = [item.alias for item in self.readers]
        if len(aliases) != len(set(aliases)):
            raise ValueError("plan reader aliases must be unique")
        return self


@dataclass(frozen=True)
class Declaration:
    name: str
    kind: Literal["action", "skill"]
    inputs: tuple[InputBinding, ...] = ()
    hold_s: float | None = None
    steps: tuple[ActionStep, ...] = ()
    max_s: float | None = None
    source: str = "catalog"

    @property
    def lease_s(self) -> float | None:
        return self.max_s if self.kind == "skill" else self.hold_s

    def definition(self) -> ActionDefinition:
        if self.kind == "action":
            return ActionDefinition(backend="input_system", inputs=list(self.inputs),
                                    default_lease_s=self.hold_s)
        return ActionDefinition(backend="input_system", steps=list(self.steps), max_s=self.max_s)

    def requirements(self):
        if self.kind == "action":
            return () if self.hold_s is None else binding_requirements(self.inputs, self.hold_s)
        return step_requirements(self.steps)

    def controls(self) -> list[tuple[str, str]]:
        if self.kind == "action":
            return [(item.device, item.control) for item in self.inputs]
        return [(item.device, item.control) for step in self.steps for item in step.inputs]


def _binding(item) -> InputBinding:
    return InputBinding(device=item.device, control=item.control, x=item.x, y=item.y)


def _manifest_declaration(value: PreflightManifest, name: str) -> Declaration:
    actions = [item for item in value.actions if item.name == name]
    skills = [item for item in value.skills if item.name == name]
    if len(actions) + len(skills) != 1:
        raise ValueError(f"{name!r} is not exactly one manifest action or skill")
    if actions:
        action = actions[0]
        hold = action.hold_s if action.hold_s is not None else action.tap_hold_s
        if hold is None:
            raise ValueError(f"manifest action {name!r} has no resolved hold to probe")
        return Declaration(name, "action", tuple(_binding(item) for item in action.inputs),
                           hold_s=hold, source="manifest")
    skill = skills[0]
    steps = []
    for step in skill.steps:
        hold = step.hold_s if step.hold_s is not None else step.tap_hold_s
        if hold is None:
            raise ValueError(f"manifest skill {name!r} has an unresolved step hold")
        steps.append(ActionStep(inputs=[_binding(item) for item in step.inputs],
                                hold_s=hold, wait_s=step.wait_s))
    return Declaration(name, "skill", steps=tuple(steps), max_s=skill.max_s, source="manifest")


def _catalog_declaration(value: ActionCatalog, name: str) -> Declaration:
    definition = value.declaration(name)
    if definition.is_skill:
        return Declaration(name, "skill", steps=tuple(definition.steps), max_s=definition.max_s,
                           source="catalog")
    return Declaration(name, "action", inputs=tuple(definition.inputs),
                       hold_s=definition.default_lease_s, source="catalog")


def _canonical(declaration: Declaration) -> str:
    return json.dumps(declaration.definition().model_dump(mode="json"), sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def resolve_plan(plan: ProbePlan, catalog: ActionCatalog | None,
                 manifest_value: PreflightManifest | None) -> list[Declaration]:
    """Resolve each named declaration; a catalog and manifest declaration must agree exactly."""
    resolved = []
    for target in plan.actions:
        from_catalog = None
        from_manifest = None
        if catalog is not None and target.name in catalog.actions:
            from_catalog = _catalog_declaration(catalog, target.name)
        if manifest_value is not None and (any(a.name == target.name for a in manifest_value.actions)
                                          or any(s.name == target.name for s in manifest_value.skills)):
            from_manifest = _manifest_declaration(manifest_value, target.name)
        if from_catalog is None and from_manifest is None:
            raise ValueError(f"{target.name!r} is neither a catalog action nor a manifest declaration")
        if from_catalog is not None and from_manifest is not None and _canonical(from_catalog) != _canonical(from_manifest):
            raise ValueError(f"{target.name!r} has different catalog and manifest declarations")
        resolved.append(from_catalog or from_manifest)
    return resolved


def _resolve_path(value: str | None, base: Path) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path)


def load_plan(path: Path) -> ProbePlan:
    return ProbePlan.model_validate_json(path.read_text(encoding="utf-8"), strict=True)


def load_catalog(path: Path | None) -> ActionCatalog | None:
    if path is None:
        return None
    return ActionCatalog.model_validate_json(path.read_text(encoding="utf-8"), strict=True)


def load_manifest(path: Path | None) -> PreflightManifest | None:
    if path is None:
        return None
    return PreflightManifest.model_validate_json(path.read_text(encoding="utf-8"), strict=True)


# --- reading -----------------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width > 0 and height > 0 else None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9):
            index += 2
            continue
        if index + 4 > len(data):
            return None
        length = struct.unpack(">H", data[index + 2:index + 4])[0]
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if index + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return (width, height) if width > 0 and height > 0 else None
        index += 2 + length
    return None


def frame_record(ref: str | None, *, archive_dir: Path | None = None) -> dict[str, Any]:
    """Hash an available frame file and report its dimensions; missing evidence stays null."""
    result = {"ref": ref if isinstance(ref, str) and ref else None, "sha256": None,
              "width": None, "height": None, "bytes": None, "readable": None}
    if result["ref"] is None:
        return result
    path = Path(result["ref"])
    try:
        if not path.is_file():
            result["readable"] = False
            return result
        size = path.stat().st_size
        if size > MAX_FRAME_BYTES:
            result["readable"] = False
            return result
        data = path.read_bytes()
    except OSError:
        result["readable"] = False
        return result
    dims = _png_dimensions(data) or _jpeg_dimensions(data)
    result.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), readable=True,
                  width=dims[0] if dims else None, height=dims[1] if dims else None)
    if archive_dir is not None:
        result["source_ref"] = result["ref"]
        suffix = ".png" if _png_dimensions(data) else ".jpg"
        archived = archive_dir / (result["sha256"] + suffix)
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            if not archived.exists():
                with archived.open("xb") as stream:
                    stream.write(data)
            if hashlib.sha256(archived.read_bytes()).hexdigest() != result["sha256"]:
                raise ValueError("archived frame hash does not match the captured frame")
            result.update(ref=str(archived), archived=True)
        except (OSError, ValueError) as exc:
            result.update(archived=False, archive_error=str(exc))
    return result


def _transform(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    forward = calibrate._vector(snapshot.get("world_forward"))
    right = calibrate._vector(snapshot.get("world_right"))
    basis = snapshot.get("basis_state")
    yaw = pitch = None
    if basis == "present" and forward is not None and calibrate._norm(forward) > 0:
        yaw = math.degrees(math.atan2(forward[0], forward[2]))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, -forward[1]))))
    return {
        "frame": snapshot.get("frame"), "real_time_s": snapshot.get("real_time_s"),
        "game_time_s": snapshot.get("game_time_s"), "wall_utc": snapshot.get("wall_utc"),
        "position": list(calibrate._vector(snapshot.get("position"))) if calibrate._vector(snapshot.get("position")) else None,
        "world_forward": list(forward) if forward is not None else None,
        "world_right": list(right) if right is not None else None,
        "basis_state": basis,
        "runtime_errors": snapshot.get("runtime_errors"),
        "coverage": snapshot.get("coverage"),
        "yaw_deg": yaw,
        "pitch_deg": pitch,
        "basis_convention": ("Unity yaw = atan2(forward.x, forward.z) and pitch = asin(-forward.y) in degrees; "
                             "derived from the measured basis, not a game-specific camera convention"),
    }


def _transform_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    if before is None or after is None:
        return None
    start, end = _number_pair(before.get("position")), _number_pair(after.get("position"))
    yaw = None
    if before.get("yaw_deg") is not None and after.get("yaw_deg") is not None:
        yaw = (after["yaw_deg"] - before["yaw_deg"] + 180.0) % 360.0 - 180.0
    return {
        "position": ([end[i] - start[i] for i in range(3)] if start is not None and end is not None else None),
        "yaw_deg": yaw,
        "pitch_deg": (after["pitch_deg"] - before["pitch_deg"]
                      if before.get("pitch_deg") is not None and after.get("pitch_deg") is not None else None),
    }


def _number_pair(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    numbers = [_number(item) for item in value]
    return None if any(item is None for item in numbers) else numbers


def _field_values(snapshot: dict[str, Any] | None, readers: list[ProbeReader]) -> dict[str, Any]:
    if snapshot is None:
        return {reader.alias: {"field_id": reader.field_id, "value_state": None, "value_kind": None,
                               "value": None} for reader in readers}
    rows = snapshot.get("fields") if isinstance(snapshot.get("fields"), list) else []
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    result = {}
    for reader in readers:
        row = by_id.get(reader.field_id)
        result[reader.alias] = {
            "field_id": reader.field_id,
            "value_state": None if row is None else row.get("value_state"),
            "value_kind": None if row is None else row.get("value_kind"),
            "value": None if row is None else row.get("value"),
        }
    return result


def _public_values(observation: Any, names: list[str]) -> dict[str, Any]:
    public = getattr(observation, "public", None)
    return {name: (public.get(name) if isinstance(public, dict) else None) for name in names}


def _delta(before: Any, after: Any) -> tuple[float | None, bool | None]:
    first, second = _number(before), _number(after)
    if first is not None and second is not None:
        return second - first, before != after
    if isinstance(before, (str, bool)) and isinstance(after, (str, bool)):
        return None, before != after
    return None, None


def _reader_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for alias in sorted(set(before) | set(after)):
        first, second = before.get(alias), after.get(alias)
        delta, changed = _delta(None if first is None else first.get("value"),
                                None if second is None else second.get("value"))
        result[alias] = {"before": first, "after": second, "delta": delta, "changed": changed}
    return result


def _public_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for name in sorted(set(before) | set(after)):
        delta, changed = _delta(before.get(name), after.get(name))
        result[name] = {"before": before.get(name), "after": after.get(name), "delta": delta, "changed": changed}
    return result


def _state(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    state = snapshot.get("input_state")
    return state if isinstance(state, dict) else None


def neutral_release(declaration: Declaration, snapshot: dict[str, Any] | None, *,
                    terminal_end_wall_s: float | None = None) -> dict[str, Any]:
    """Read the post-action device state; missing or stale state is unknown, never released.

    The sample must be an active, error-free input state captured within the declared
    freshness bound of the field sample and at or after the terminal execution end. A stale
    or inactive snapshot cannot prove a neutral release, and an unknown independent sample
    stays unknown even when per-input receipts separately prove release.
    """
    state = _state(snapshot)
    result: dict[str, Any] = {"input_state_available": state is not None, "state_active": None,
                              "state_error": None, "update_type": None, "state_wall_s": None,
                              "sample_wall_s": None if snapshot is None else snapshot.get("sample_wall_s"),
                              "sample_real_time_s": None if snapshot is None else snapshot.get("real_time_s"),
                              "fresh": None, "controls": [], "verified": None, "reason": None}
    if state is None:
        result["reason"] = "no input state was sampled after the action"
        return result
    result.update(state_active=state.get("active"), state_error=state.get("error"),
                  update_type=state.get("update_type"), state_wall_s=state.get("wall_s"))
    if state.get("active") is not True:
        result["reason"] = "the input state was not active when sampled"
        return result
    if state.get("error") is not None:
        result["reason"] = "the input state reported an error"
        return result
    if state.get("update_type") not in ("dynamic", "fixed"):
        result["reason"] = "the input state update type is unknown"
        return result
    captured_real = _number(state.get("real_time_s"))
    sample_real = _number(snapshot.get("real_time_s"))
    if captured_real is None or sample_real is None:
        result["reason"] = "the input state or field sample has no measured real time"
        return result
    age = sample_real - captured_real
    if age < 0 or age > calibrate.MAX_KEYBOARD_STATE_AGE_S:
        result["reason"] = f"the input state is {age:.3f} s from the field sample"
        return result
    state_wall = _number(state.get("wall_s"))
    sample_wall = _number(snapshot.get("sample_wall_s"))
    terminal_end = _number(terminal_end_wall_s)
    if state_wall is None or sample_wall is None or terminal_end is None:
        result["reason"] = "the input state, field sample or terminal end has no measured wall time"
        return result
    if state_wall > sample_wall + calibrate.MAX_KEYBOARD_STATE_AGE_S:
        result["reason"] = "the input state was captured after its field sample"
        return result
    if state_wall < terminal_end - calibrate.MAX_KEYBOARD_STATE_AGE_S:
        result["reason"] = "the input state predates the terminal execution end"
        return result
    result["fresh"] = True
    rows = []
    for device, control in declaration.controls():
        held = calibrate._control_held(state, device, control)
        rows.append({"device": device, "control": control, "held": held})
    result["controls"] = rows
    if any(row["held"] is True for row in rows):
        result["verified"] = False
        result["reason"] = "a declared control is still held"
    elif any(row["held"] is None for row in rows):
        result["reason"] = "a declared control has no measured device state"
    else:
        result["verified"] = True
        result["reason"] = "every declared control is measured as released"
    return result


def evaluate_attempt(declaration: Declaration, records: list[Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Judge one attempt using only measured receipts and release state."""
    terminal = records[-1]
    status = terminal.status.value if hasattr(terminal.status, "value") else str(terminal.status)
    receipts = list(getattr(terminal, "applied_inputs", ()))
    definition = declaration.definition()
    release = neutral_release(declaration, snapshot, terminal_end_wall_s=terminal.input_end_wall_s)
    problems = []
    evidence = None
    if status == "rejected":
        problems.append(f"the bridge rejected the request ({terminal.reject_reason.value if terminal.reject_reason else 'unknown'}) and executed nothing")
    elif status != "completed":
        problems.append(f"the terminal status is {status!r}")
    if receipts:
        evidence = InputExecutionEvidence(
            action=declaration.name, session=terminal.session, status=terminal.status,
            release_verified=bool(terminal.release_verified), declaration=definition,
            applied_duration_s=terminal.applied_duration_s,
            input_start_wall_s=terminal.input_start_wall_s, input_end_wall_s=terminal.input_end_wall_s,
            receipts=receipts, wall_utc=terminal.wall_utc)
        problems += evidence_problems(evidence, definition)
    elif status == "completed":
        problems.append("a completed execution reported no applied-input receipts")
    else:
        problems += receipt_problems(declaration.requirements(), receipts, declared=definition,
                                    applied_total_s=terminal.applied_duration_s,
                                    terminal_start_s=terminal.input_start_wall_s,
                                    terminal_end_s=terminal.input_end_wall_s)
    if terminal.release_verified is not True:
        problems.append("the terminal execution did not verify input release")
    if release["verified"] is False:
        problems.append("the post-action device state still holds a declared control")
    valid = not problems
    return {
        "status": status,
        "executed": status != "rejected" and bool(receipts),
        "valid": valid,
        "problems": problems,
        "release_verified_terminal": terminal.release_verified,
        "neutral_release": release,
        "declaration": definition.model_dump(mode="json"),
        "execution_evidence": (evidence.model_dump(mode="json")
                               if evidence is not None and valid else None),
    }


async def _read_fields(client: BridgeClient, scene_id: str | None) -> dict[str, Any] | None:
    if scene_id is None:
        return None
    response = await client.inspect("fields")
    return calibrate._field_snapshot(response, scene_id)


async def _read_entity(client: BridgeClient, entity_id: str | None) -> dict[str, Any] | None:
    if entity_id is None:
        return None
    return await calibrate._read_entity(client, entity_id)


# --- probe run ---------------------------------------------------------------------------------


async def measure(client: BridgeClient, plan: ProbePlan, declarations: list[Declaration], *,
                  entity_id: str | None, scene_id: str | None,
                  readers: list[ProbeReader], media_dir: Path | None = None) -> list[dict[str, Any]]:
    """Run every named declaration for its declared repeats; retain every failure.

    Execution records are retained immediately after `act` returns. Any later transport
    failure keeps those records but fails the attempt closed and stops every remaining
    declaration: a release that could not be observed must not be treated as neutral.
    Counts are the attempts actually made; none are inferred.
    """
    results = []
    fatal = None
    plan_names = [item.name for item in plan.actions]
    for declaration in declarations:
        target = plan.actions[plan_names.index(declaration.name)]
        attempts = []
        if fatal is not None:
            results.append({
                "name": declaration.name, "kind": declaration.kind, "source": declaration.source,
                "declaration": declaration.definition().model_dump(mode="json"),
                "repeats_requested": target.repeats, "attempts_made": 0, "successes": 0,
                "k_over_n": None, "attempts": [],
                "skipped": f"the probe stopped before this declaration: {fatal}",
            })
            continue
        for repeat in range(1, target.repeats + 1):
            record = {"repeat": repeat, "based_on_seq": None, "observation_frame": None,
                      "before_transform": None, "after_transform": None, "transform_delta": None,
                      "readers": None, "public": None, "execution": [], "frames": {"before": None, "after": None},
                      "declaration": declaration.definition().model_dump(mode="json"),
                      "valid": False, "problems": [], "status": None, "executed": False,
                      "release_verified_terminal": None, "neutral_release": None,
                      "execution_evidence": None}
            records = None
            terminal = None
            act_requested = False
            try:
                before_transform = await _read_entity(client, entity_id)
                before_fields = await _read_fields(client, scene_id) if (scene_id is not None) else None
                observation = await client.observe_once(with_frame=True)
                before_public = _public_values(observation, plan.public_fields)
                act_requested = True
                records = await client.act(
                    action_id=f"probe-{uuid.uuid4().hex}", choice_id=f"probe-{declaration.name}",
                    action=declaration.name, params={}, based_on_seq=observation.seq,
                    max_age_s=plan.max_age_s, depends_on=plan.depends_on,
                    lease_s=declaration.lease_s)
                # Keep the measured execution even if every later read fails.
                record["execution"] = [item.model_dump(mode="json") for item in records]
                if not records:
                    raise ValueError("the bridge returned no execution record")
                terminal = records[-1]
                record["status"] = getattr(terminal.status, "value", terminal.status)
                record["release_verified_terminal"] = terminal.release_verified
                if plan.settle_s:
                    await asyncio.sleep(plan.settle_s)
                after_transform = await _read_entity(client, entity_id)
                after_fields = await _read_fields(client, scene_id) if (scene_id is not None) else None
                after_observation = await client.observe_once(with_frame=True)
            except (BridgeError, ValueError) as exc:
                record["problems"] = [f"{type(exc).__name__}: {exc}"]
                record["valid"] = False
                if records:
                    record["execution_error"] = (f"execution was measured but the post-action state could not be "
                                                 f"read; release is unverified")
                    record["problems"].append(record["execution_error"])
                    fatal = (f"after declaring {declaration.name!r} the post-action state could not be read, so "
                             f"release is unverified: {type(exc).__name__}: {exc}")
                elif act_requested:
                    record["problems"].append("the act request failed before a terminal record arrived; "
                                              "release is unverified")
                    fatal = (f"declaring {declaration.name!r} failed before a terminal execution record, so "
                             f"release is unverified: {type(exc).__name__}: {exc}")
                elif isinstance(exc, BridgeError):
                    fatal = str(exc)
                attempts.append(record)
                break
            record.update(
                based_on_seq=observation.seq,
                observation_frame=observation.frame,
                observation_public=before_public,
            )
            judged = evaluate_attempt(declaration, records, after_fields)
            record.update(judged)
            record["before_transform"] = _transform(before_transform)
            record["after_transform"] = _transform(after_transform)
            record["transform_delta"] = _transform_delta(record["before_transform"], record["after_transform"])
            record["readers"] = (_reader_deltas(_field_values(before_fields, readers),
                                                _field_values(after_fields, readers))
                                 if scene_id is not None else None)
            before_public = _public_values(observation, plan.public_fields)
            after_public = _public_values(after_observation, plan.public_fields)
            record["public"] = _public_deltas(before_public, after_public)
            record["frames"] = {"before": frame_record(observation.frame_ref, archive_dir=media_dir),
                                "after": frame_record(after_observation.frame_ref, archive_dir=media_dir)}
            for key, captured in (("before", observation), ("after", after_observation)):
                stamp = getattr(captured, "wall_utc", None)
                record["frames"][key].update(
                    consumer="preflight_input_probe", source="bridge_composite_capture",
                    seq=captured.seq, frame=captured.frame, game_s=getattr(captured, "game_s", None),
                    wall_utc=stamp.isoformat() if stamp is not None else None)
            attempts.append(record)
            if judged["status"] == "rejected":
                break
            terminal_receipts = (record["execution"][-1].get("applied_inputs", [])
                                 if record["execution"] else [])
            if any(item.get("release_verified") is not True for item in terminal_receipts):
                fatal = (f"declaration {declaration.name!r} did not verify release for every applied input; "
                         f"no dependent input was applied afterwards")
                break
            if judged["release_verified_terminal"] is not True:
                fatal = (f"declaration {declaration.name!r} did not verify terminal input release; "
                         f"no dependent input was applied afterwards")
                break
            if judged["neutral_release"]["verified"] is False:
                fatal = (f"declaration {declaration.name!r} still held a declared control after the action; "
                         f"no dependent input was applied afterwards")
                break
            if judged["status"] != "completed":
                break
        successes = sum(1 for item in attempts if item["valid"])
        results.append({
            "name": declaration.name,
            "kind": declaration.kind,
            "source": declaration.source,
            "declaration": declaration.definition().model_dump(mode="json"),
            "repeats_requested": target.repeats,
            "attempts_made": len(attempts),
            "successes": successes,
            "k_over_n": f"{successes}/{len(attempts)}",
            "attempts": attempts,
        })
        if fatal is not None:
            continue
    if fatal is not None:
        results.append({"fatal_error": fatal})
    return results


def _evidence_from(attempt: dict[str, Any]) -> InputExecutionEvidence | None:
    """Revalidate the exact declaration-bearing evidence the probe recorded for one attempt."""
    if not attempt.get("valid"):
        return None
    value = attempt.get("execution_evidence")
    if not isinstance(value, dict):
        raise ValueError("a valid probe attempt has no exact declaration-bearing execution evidence")
    return InputExecutionEvidence.model_validate(value)


def update_manifest(path: Path, results: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind successful evidence to the named declarations and clear failed proof.

    Only declarations named by this probe are touched. A failed probe clears prior evidence:
    support is session-scoped and cannot be inherited from an earlier run. Every failure
    stays in the probe results; promoting the most recent success does not clear them.
    """
    before = path.read_bytes()
    value = PreflightManifest.model_validate_json(before.decode("utf-8"), strict=True)
    problems = manifest_module.validate(value)
    if problems:
        raise ValueError("the manifest is invalid before evidence update: " + "; ".join(problems))
    data = json.loads(before)
    updates = []
    by_name = {item.get("name"): item for item in results if isinstance(item, dict) and item.get("name")}
    for item in data.get("actions", []):
        target = by_name.get(item.get("name"))
        if target is None:
            continue
        successes = [row for row in target.get("attempts", []) if isinstance(row, dict) and row.get("valid")]
        evidence = _evidence_from(successes[-1]) if successes else None
        item["execution_evidence"] = evidence.model_dump(mode="json") if evidence is not None else None
        item["execution_supported"] = evidence is not None
        updates.append({"name": item["name"], "kind": "action", "supported": evidence is not None,
                        "attempts_made": target.get("attempts_made"),
                        "successes": target.get("successes")})
    for item in data.get("skills", []):
        target = by_name.get(item.get("name"))
        if target is None:
            continue
        successes = [row for row in target.get("attempts", []) if isinstance(row, dict) and row.get("valid")]
        evidence = _evidence_from(successes[-1]) if successes else None
        item["execution_evidence"] = evidence.model_dump(mode="json") if evidence is not None else None
        item["execution_supported"] = evidence is not None
        updates.append({"name": item["name"], "kind": "skill", "supported": evidence is not None,
                        "attempts_made": target.get("attempts_made"),
                        "successes": target.get("successes")})
    manifest_module.refresh_execution_coverage(data)
    revalidated = PreflightManifest.model_validate(data)
    problems = manifest_module.validate(revalidated)
    if problems:
        raise ValueError("the updated manifest is invalid: " + "; ".join(problems))
    serialized = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(serialized, encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    return {"path": str(path), "sha256_before": hashlib.sha256(before).hexdigest(),
            "sha256_after": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "updates": updates}


def _under_runs(path: Path, repo_root: Path) -> bool:
    try:
        return path.resolve().is_relative_to((repo_root / "runs").resolve())
    except OSError:
        return False


def _reject_inside_project(path: Path, project: Path) -> None:
    if path.resolve() == project.resolve() or path.resolve().is_relative_to(project.resolve()):
        raise ValueError(f"{path} is inside the Unity project; probe artifacts are written under runs/")


async def _run(project: Path, plan_path: Path, output: Path, repo_root: Path) -> dict[str, Any]:
    plan = load_plan(plan_path)
    plan_base = plan_path.parent
    catalog_path = _resolve_path(plan.catalog or os.environ.get("UPA_INPUT_PROBE_CATALOG"), plan_base)
    manifest_path = _resolve_path(plan.manifest, plan_base)
    catalog = load_catalog(catalog_path)
    manifest_value = load_manifest(manifest_path)
    if catalog is None and manifest_value is None:
        raise ValueError("the plan names neither a catalog nor a manifest")
    declarations = resolve_plan(plan, catalog, manifest_value)
    entity_id = plan.entity_id or (manifest_value.entity_id if manifest_value is not None else None)
    scene_id = plan.scene_id or (manifest_value.entity.get("scene_id") if manifest_value is not None else None)
    readers = plan.readers or ([ProbeReader(alias=item.alias, field_id=item.field_id)
                                for item in manifest_value.readers] if manifest_value is not None else [])
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    stop = None
    stop_error = None
    try:
        results = await measure(client, plan, declarations, entity_id=entity_id, scene_id=scene_id,
                                readers=readers, media_dir=output.with_name(output.stem + "-frames"))
    finally:
        try:
            stop = await client.aclose()
        except Exception as exc:  # shutdown evidence is recorded, never hidden
            stop_error = f"{type(exc).__name__}: {exc}"
    stop_value = stop.model_dump(mode="json") if stop is not None else None
    stop_verified = stop is not None and stop.release_verified is True
    fatal = next((row["fatal_error"] for row in results if "fatal_error" in row), None)
    update = None
    update_error = None
    update_path = _resolve_path(plan.update_manifest, plan_base)
    if update_path is not None:
        if not _under_runs(update_path, repo_root):
            update_error = "the manifest to update must be under runs/"
        elif update_path.resolve().is_relative_to(project.resolve()):
            update_error = "the manifest to update must be outside the Unity project"
        elif not stop_verified:
            update_error = "bridge shutdown did not verify input release; evidence was not written"
        elif fatal is not None:
            update_error = f"the probe stopped on a bridge error: {fatal}"
        else:
            try:
                update = update_manifest(update_path, results)
            except (OSError, ValueError, TypeError) as exc:
                update_error = str(exc)
    made = sum(item.get("attempts_made", 0) for item in results if "name" in item)
    successes = sum(item.get("successes", 0) for item in results if "name" in item)
    if fatal is not None or not stop_verified:
        status = "inconclusive"
    elif made and successes == made:
        status = "completed"
    else:
        status = "completed_with_failures"
    return {
        "schema_version": 1,
        "probe": "preflight_input_probe",
        "is_s1_turn": False,
        "model_calls": 0,
        "claim_scope": ("named declarations measured in one real session: applied-input receipts, release, entity "
                        "transform and sampled field/public changes; not a game-effect calibration and not an S1 turn"),
        "status": status,
        "error": fatal,
        "project": project.name,
        "catalog_sha256": (hashlib.sha256(catalog_path.read_bytes()).hexdigest()
                           if catalog_path is not None and catalog_path.is_file() else None),
        "manifest_sha256": (hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                            if manifest_path is not None and manifest_path.is_file() else None),
        "plan": plan.model_dump(mode="json"),
        "targets": results,
        "totals": {"attempts": made, "successes": successes, "k_over_n": f"{successes}/{made}"},
        "manifest_update": update,
        "manifest_update_error": update_error,
        "stop": stop_value,
        "stop_error": stop_error,
        "stop_release_verified": stop_verified,
        "timestamps": {"finished_wall_utc": datetime.now(timezone.utc).isoformat()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new JSON file under <repo>/runs")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    repo_root = Path(os.environ.get("UPA_REPO_ROOT") or Path.cwd()).resolve()
    output = args.output.resolve()
    try:
        _reject_inside_project(output, project)
        if not _under_runs(output, repo_root):
            parser.error("--output must be under <repo>/runs (UPA_REPO_ROOT)")
        if output.exists():
            parser.error("--output already exists")
        result = asyncio.run(_run(project, args.plan.resolve(), output, repo_root))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (BridgeError, BridgeLockError, OSError, ValueError, TypeError) as exc:
        print(f"input probe failed: {exc}", file=sys.stderr)
        return 1
    print(f"input probe saved to {output}: {result['status']} ({result['totals']['k_over_n']})")
    return 0 if result["status"].startswith("completed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
