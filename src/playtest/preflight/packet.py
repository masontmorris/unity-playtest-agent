"""Adapt Editor discovery and measured calibration for model preflight stages.

Control ids in this packet identify physical device controls (keyboard keys, mouse delta/buttons
and gamepad sticks, triggers, dpad and buttons), not Unity's logical InputAction axes. Each
offered control carries the one declared value calibration measured. Bindings outside the
supported root controls remain visible as coverage gaps; they are not turned into actions by
code.
"""

from __future__ import annotations

import json
import hashlib
import copy
from dataclasses import dataclass
from typing import Any

from playtest.contracts.bridge import InspectResult, InputExecutionEvidence
from playtest.contracts.calibration import ActionCalibration, CalibrationRecord
from playtest.preflight.calibrate import discover_actions
from playtest.roles import preflight


@dataclass(frozen=True)
class Packet:
    text: str
    discovery: preflight.Discovery
    field_bindings: dict[str, str]
    measured_taps: frozenset[str]
    image: "Screenshot | None"
    execution_evidence: tuple[InputExecutionEvidence, ...] = ()

    @property
    def action_prompt(self) -> str:
        return (preflight.STAGE_PROMPTS["actions"] + "\n"
                "Calibration lists one measured declared value and the tested requested holds for each physical "
                "control; whether repeated trials showed an entity-displacement effect; and never implies that a "
                "different value, a combined input or a different hold was measured. Mouse delta is one relative "
                "impulse per apply: x and y are normalized displacement and the runtime applies x * 64, y * 64 "
                "pixels. A keyboard or device button takes null x and y; a vector control takes both values in "
                "[-1, 1]; a one-dimensional axis uses x with y = 0. Triggers use x in [0, 1]; dpad components "
                "must each be -1, 0 or 1. Gamepad values are sent unchanged. Absolute "
                "pointer, touch, VR and remapping are unsupported. You may choose chords and bounded skills; "
                "execution support is established later only by release-verified evidence for the exact declared "
                "inputs, values, holds and step order, not by your declaration. Describe game limitations separately "
                "from bridge or instrumentation limitations.")

    def review_bridge(self, answer: preflight.StageAnswer, stage: preflight.Stage) -> list[str]:
        """Bridge limitations are recorded in the manifest; they do not reject model selections."""
        return []


@dataclass(frozen=True)
class Screenshot:
    """Screenshot bytes supplied by the caller, with their stated capture provenance."""

    data: bytes
    mime: str
    provenance: str
    same_session: bool | None = None
    capture_game_s: float | None = None

    def descriptor(self) -> dict[str, Any]:
        if self.mime not in ("image/png", "image/jpeg") or not isinstance(self.data, bytes) or not self.data:
            raise ValueError("a screenshot needs PNG or JPEG bytes")
        signature = b"\x89PNG\r\n\x1a\n" if self.mime == "image/png" else b"\xff\xd8\xff"
        if not self.data.startswith(signature):
            raise ValueError("screenshot bytes do not match the declared MIME type")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ValueError("a screenshot needs capture provenance")
        if self.same_session is not None and not isinstance(self.same_session, bool):
            raise ValueError("screenshot same_session must be true, false or null")
        if self.capture_game_s is not None:
            import math
            if isinstance(self.capture_game_s, bool) or not isinstance(self.capture_game_s, (int, float)) or not math.isfinite(self.capture_game_s) or self.capture_game_s < 0:
                raise ValueError("screenshot capture game time must be nonnegative finite seconds or null")
        return {"mime": self.mime, "bytes": len(self.data),
                "sha256": hashlib.sha256(self.data).hexdigest(), "provenance": self.provenance,
                "same_session": self.same_session, "capture_game_s": self.capture_game_s}


def _complete_rows(snapshot: InspectResult, name: str) -> list[dict[str, Any]]:
    cap = snapshot.caps.get(name)
    rows = snapshot.data.get(name)
    if cap is None or cap.observed is None or cap.truncated is not False:
        raise ValueError(f"discovery has incomplete {name} coverage")
    if not isinstance(rows, list) or len(rows) != cap.returned or any(not isinstance(x, dict) for x in rows):
        raise ValueError(f"discovery {name} rows do not match their coverage")
    return rows


def _index(rows: list[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        ident = row.get(key)
        if not isinstance(ident, str) or not ident or ident in result:
            raise ValueError(f"{label} have missing or duplicate {key} values")
        result[ident] = row
    return result


def _digest(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def field_kind(row: dict[str, Any]) -> str | None:
    """Use the Editor's observed serialization kind; unmeasured values stay unknown."""
    if row.get("value_state") != "present":
        return None
    kind = row.get("value_kind")
    if kind in ("integer", "number", "enum"):
        return "number"
    if kind == "boolean":
        return "bool"
    if kind in ("character", "string"):
        return "string"
    if kind in ("vector2", "vector3", "vector4", "vector2_int", "vector3_int", "quaternion", "color", "color32", "rect", "bounds"):
        return "vector"
    return "other"


def field_offered(row: dict[str, Any]) -> bool:
    """Do not offer a field the inspection explicitly could not read."""
    return row.get("value_state") in ("present", "null") and row.get("value_kind") not in (
        "unsupported", "error", "special_number", "special_vector")


PAUSE_ERROR_SAMPLES = 3
PAUSE_ERROR_TEXT_CHARS = 160


def pause_runtime_errors(evidence: Any) -> dict[str, Any] | None:
    """What the game logged while the clock was paused or stepped, as measured by calibration.

    Counts come from the monitor's counter and are exact; the messages are a bounded sample and
    may be incomplete, which is stated. A record without clock error evidence gives None: that is
    unmeasured, not "no errors". Support for stepped mode is not decided here or anywhere in code.
    """
    if not isinstance(evidence, dict) or not isinstance(evidence.get("windows"), dict):
        return None
    windows = {name: {"meaning": window.get("meaning"), "count": window.get("count"),
                      "sample_complete": window.get("complete"), "reason": window.get("reason")}
               for name, window in evidence["windows"].items() if isinstance(window, dict)}
    samples = []
    for name, window in evidence["windows"].items():
        for kind in window.get("kinds", []) if isinstance(window, dict) else []:
            if isinstance(kind, dict) and len(samples) < PAUSE_ERROR_SAMPLES:
                message = kind.get("message")
                samples.append({"window": name, "type": kind.get("type"),
                                "message": message[:PAUSE_ERROR_TEXT_CHARS] if isinstance(message, str) else None,
                                "occurrences_in_sample": kind.get("occurrences_in_sample")})
    return {"errors_observed": evidence.get("errors_observed"), "total_count": evidence.get("total_count"),
            "sample_complete": evidence.get("sample_complete"), "gap": evidence.get("gap"),
            "windows": windows, "sample": samples,
            "note": ("counts are exact monitor-counter differences between consecutive reads taken around the "
                     "pause, the 0.2 s paused interval, the step, the resume and a short interval with the clock "
                     "running again; messages are a bounded sample")}


def build(inventory: InspectResult, discovery: InspectResult, calibration: CalibrationRecord,
          *, image: Screenshot | None = None) -> Packet:
    """Build a deterministic packet from compatible stage 1 and stage 3 records.

    A current inspection must resolve the measured entity and the same discovered bindings.
    Only keys with at least one valid calibration trial are offered; excluded bindings and
    their coverage are included as facts. Freshness is passed through as measured, including
    unknowns. A screenshot is not available from these records and is explicitly marked null.
    """
    if inventory.stage != "inventory" or inventory.errors:
        raise ValueError("an inventory inspection without errors is required")
    if discovery.stage != "discovery" or discovery.errors:
        raise ValueError("a discovery inspection without errors is required")
    if "il_inputs" in discovery.data:
        # The bridge's own snapshot names the IL scan's inputs, local file paths among them. Only a
        # snapshot merged with the scan (tools/ilscan) is evidence for the model.
        raise ValueError("discovery has not been merged with the IL write scan")
    if calibration.status != "measured_with_gaps" and not (
            calibration.status == "inconclusive" and not calibration.actions and calibration.error):
        raise ValueError("completed calibration, or an inconclusive record with no controls and a reason, is required")
    if calibration.action_cap.truncated is not False or calibration.action_cap.observed is None:
        raise ValueError("calibration action coverage is incomplete")
    fields = _complete_rows(discovery, "fields")
    objects = _complete_rows(discovery, "game_objects")
    _complete_rows(discovery, "components")
    _complete_rows(discovery, "bindings")
    _complete_rows(discovery, "input_asset_bindings")
    indexed_fields = _index(fields, "id", "fields")
    indexed_objects = _index(objects, "id", "game objects")
    current_entity = indexed_objects.get(calibration.entity_id)
    entity_keys = ("scene_id", "scene_path", "path")
    if (calibration.entity.get("id") != calibration.entity_id or current_entity is None or
            any(not isinstance(calibration.entity.get(k), str) or
                                     not calibration.entity[k] or current_entity.get(k) != calibration.entity[k]
                                     for k in entity_keys)):
        raise ValueError("calibrated entity no longer resolves to the same scene and path")
    current_actions, unsupported = discover_actions(discovery.data)
    current = _index(current_actions, "control", "discovered controls")
    calibrated = _index(calibration.actions, "control", "calibration controls")
    if current != calibrated or calibration.action_cap.returned != len(calibrated):
        raise ValueError("discovered controls or bindings differ from calibration")
    summaries = _index([ActionCalibration.model_validate(a).model_dump(mode="json")
                        for a in calibration.action_summaries],
                       "control", "calibration summaries")
    if set(summaries) - set(calibrated):
        raise ValueError("a calibration summary names an undiscovered control")
    if any(summaries[k]["bindings"] != calibrated[k].get("bindings") or
           summaries[k]["resolution"] != calibrated[k].get("resolution") for k in summaries):
        raise ValueError("a calibration summary does not match its discovered bindings")
    tested = {t.get("control") for t in calibration.trials if t.get("valid") is True}
    offered = sorted(set(calibrated) & set(summaries) & tested)
    source = discovery.data.get("source_excerpts")
    if not isinstance(source, list):
        raise ValueError("discovery has no source excerpts")
    for name in ("source_files", "source_characters"):
        if name not in discovery.caps:
            raise ValueError(f"discovery has no {name} coverage")
    # The Editor ids can exceed the provider's enum-string budget. These local, deterministic
    # tokens are an exact bijection to the untouched descriptors; they are not name matching.
    all_bindings = {f"field_{index:05d}": ident for index, ident in enumerate(sorted(indexed_fields))}
    bindings = {token: ident for token, ident in all_bindings.items() if field_offered(indexed_fields[ident])}
    field_types: dict[str, str] = {}
    for token, ident in bindings.items():
        row = indexed_fields[ident]
        declared = row.get("field_type")
        field_types[token] = declared if isinstance(declared, str) else ""
    field_kinds = {token: field_kind(indexed_fields[ident]) for token, ident in bindings.items()}
    role_discovery = preflight.Discovery(
        field_types,
        {key: summaries[key].get("control_type") or "Button" for key in offered},
        field_kinds,
    )
    freshness = calibration.freshness or {}
    matched_freshness = []
    for token, ident in bindings.items():
        measurement = freshness.get(ident)
        if isinstance(measurement, dict):
            matched_freshness.append({"field_id": token, "classification": measurement.get("classification"),
                                      "continuous_classification": measurement.get("continuous_classification"),
                                      "sampling_limitation": measurement.get("sampling_limitation")})
    clock = calibration.clock or {}
    reset = calibration.reset or {}
    scene_reset = reset.get("scene_reload") if isinstance(reset.get("scene_reload"), dict) else {}
    # Source code and descriptors are necessary for preflight; runtime field values are not.
    # Omit them explicitly from the model packet while preserving value_state and value_kind.
    discovery_projection = copy.deepcopy(discovery.data)
    discovery_projection["fields"] = [{k: v for k, v in row.items() if k not in ("value", "enum_name")}
                                      for row in fields]
    payload = {
        "inventory": inventory.data,
        "inventory_caps": {k: v.model_dump(mode="json") for k, v in inventory.caps.items()},
        "discovery": discovery_projection,
        "discovery_projection_note": "Runtime field values and enum names are omitted; source excerpts are included.",
        "discovery_caps": {k: v.model_dump(mode="json") for k, v in discovery.caps.items()},
        "offered_fields": [{"id": token, "source_field_id": ident, "field_type": field_types[token],
                            "same_scene_as_entity": indexed_fields[ident].get("scene_id") == calibration.entity.get("scene_id")
                            if isinstance(indexed_fields[ident].get("scene_id"), str) else None}
                           for token, ident in bindings.items()],
        "unoffered_fields": [{"source_field_id": ident, "value_state": indexed_fields[ident].get("value_state"),
                              "value_kind": indexed_fields[ident].get("value_kind")}
                             for token, ident in all_bindings.items() if token not in bindings],
        "field_id_note": ("Select only offered_fields[].id as field_id. source_field_id is its exact "
                          "Editor descriptor id for provenance, not a selectable id."),
        "calibration": {
            "entity_id": calibration.entity_id, "entity": calibration.entity,
            "coordinate_frame": calibration.coordinate_frame, "units": calibration.units,
            "physical_unit": calibration.physical_unit,
            "freshness": {"id_matches_unverified": matched_freshness, "total_fields": len(freshness),
                          "source_sha256": _digest(calibration.freshness)},
            "clock": {"stepped_mode_supported": clock.get("stepped_mode_supported"),
                      "reason": clock.get("reason"), "error": clock.get("error"),
                      "pause_runtime_errors": pause_runtime_errors(clock.get("runtime_errors")),
                      "source_sha256": _digest(calibration.clock)},
            "field_sampling_gap": calibration.field_sampling_gap,
            "reset": {"scene_reload": {k: v for k, v in scene_reset.items() if k not in ("fields", "evidence", "trials")},
                      "save_load": reset.get("save_load"), "checkpoint": reset.get("checkpoint"),
                      "source_sha256": _digest(calibration.reset)},
            "control_summaries": [summaries[k] for k in offered],
            "field_identity_note": ("Freshness uses within-run field ids. The calibration record does not "
                                    "preserve full field descriptors, so matching ids alone do not prove "
                                    "identity across separately captured sessions."),
            "summary_note": ("Full freshness, clock and reset samples are omitted from this prompt and remain "
                             "in the local calibration record; the hashes identify the supplied source data."),
            "unsupported_bindings": unsupported,
            "unoffered_controls": [calibrated[k] for k in sorted(set(calibrated) - set(offered))],
        },
        "offered_controls": [{
            "id": key, **calibrated[key],
            "device": summaries[key].get("device") or "Keyboard",
            "control": summaries[key].get("control_name") or summaries[key].get("control") or key,
            "control_type": summaries[key].get("control_type") or "Button",
            "declared_x": summaries[key].get("declared_x"),
            "declared_y": summaries[key].get("declared_y"),
        } for key in offered],
        "image": image.descriptor() if image is not None else None,
        "image_unavailable_reason": ("no screenshot was supplied" if image is None else None),
        "capability_note": (
            "Each declared act names its physical device controls, normalized values and its hold or ordered "
            "steps. Mouse delta is one relative impulse per apply, with x and y multiplied by the fixed 64-pixel "
            "component bound; it is never repeated every update. Gamepad values go unchanged into device state. "
            "Keyboard and device buttons take null x and y; vector controls take both values; a one-dimensional "
            "axis uses x with y = 0. Triggers use x in [0, 1]; dpad components must each be -1, 0 or 1. "
            "Relative scroll, absolute pointer, touch, VR and remapping are unsupported. "
            "A null action hold requests a tap; if no tested requested hold exists its duration is unknown. "
            "Separately measured keys do not prove a chord, a different value or a skill. Execution support and "
            "effects remain unknown until exact release-verified evidence exists."
        ),
    }
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"preflight evidence is not finite JSON: {exc}") from None
    measured_taps = frozenset(k for k in offered if
                              summaries[k]["shortest_observed_effective_hold_s"] is not None and
                              0 < summaries[k]["shortest_observed_effective_hold_s"] <= 3600)
    return Packet(text, role_discovery, bindings, measured_taps, image,
                  tuple(calibration.execution_evidence))
