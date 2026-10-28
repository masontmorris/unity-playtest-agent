"""Assemble and revalidate a local manifest from accepted preflight answers."""

from __future__ import annotations

import hashlib
import json

from playtest.contracts.manifest import PreflightManifest
from playtest.preflight.packet import Packet, field_kind, field_offered
from playtest.preflight import stages
from playtest.preflight.stages import RunResult
from playtest.roles import preflight
from playtest.roles.checks import value_kind


def _coverage(data: dict, readers: list[dict], actions: list[dict], skills: list[dict]) -> list[str]:
    coverage = ["field freshness is unknown until calibration field identity is established",
                "the current bridge cannot execute multi-step skills",
                "selected readers are not installed or verified in the live observation source",
                "the manifest action catalog is not installed or verified in a live run",
                "reset has not been verified as part of this manifest run",
                "reset method candidates are not emitted by the current discovery collector"]
    if data["image"] is None:
        coverage.append("no screenshot was supplied")
    if data["calibration"]["unsupported_bindings"]:
        coverage.append("some discovered bindings are not supported by the keyboard backend")
    if data["calibration"]["unoffered_controls"]:
        coverage.append("some discovered controls lack a valid calibration trial")
    if data["unoffered_fields"]:
        coverage.append("some discovered fields could not be read and were not offered as readers")
    if not data["offered_fields"]:
        coverage.append("no discovered field was offered as a reader")
    if not data["offered_controls"]:
        coverage.append("no measured control was offered")
    if any(r["descriptor"].get("value_state") == "null" for r in readers):
        coverage.append("one or more selected readers were null during discovery; their value kind is unknown")
    if any(a["hold_effect"] is None for a in actions):
        coverage.append("some selected action holds have no established effective response")
    if any(a["hold_effect"] is False for a in actions):
        coverage.append("some tested holds did not meet the repeated entity-displacement threshold")
    if any(not a["execution_supported"] for a in actions):
        coverage.append("some selected actions cannot be executed by the current bridge")
    if skills:
        coverage.append("selected skills are preserved but cannot be executed by the current bridge")
    if any(step["hold_s"] is None for skill in skills for step in skill["steps"]):
        coverage.append("some skill step tap durations are unknown; declared skill bounds are unverified")
    if any(r["descriptor"].get("scene_id") != data["calibration"]["entity"].get("scene_id") for r in readers):
        coverage.append("one or more selected fields are outside the calibrated entity's scene, or their scene is unknown")
    if any(cap["truncated"] is not False or cap["observed"] is None for cap in data["inventory_caps"].values()):
        coverage.append("inventory coverage is incomplete or unknown")
    if any(cap["truncated"] is not False or cap["observed"] is None for cap in data["discovery_caps"].values()):
        coverage.append("some discovery coverage is incomplete or unknown")
    return coverage


def _selection(data: dict, readers: list[dict], actions: list[dict], skills: list[dict], gaps: dict) -> dict:
    offered = data["offered_fields"]
    selected = {r["selection_id"] for r in readers}
    return {"offered_fields": offered, "unselected_field_ids": [f["id"] for f in offered if f["id"] not in selected],
            "unoffered_fields": data["unoffered_fields"], "offered_controls": data["offered_controls"],
            "unoffered_controls": data["calibration"]["unoffered_controls"],
            "unsupported_bindings": data["calibration"]["unsupported_bindings"],
            "reader_cap": preflight.MAX_READERS, "reader_cap_reached": len(readers) == preflight.MAX_READERS,
            "action_cap": preflight.MAX_ACTIONS, "action_cap_reached": len(actions) == preflight.MAX_ACTIONS,
            "skill_cap": preflight.MAX_SKILLS, "skill_cap_reached": len(skills) == preflight.MAX_SKILLS,
            "gap_caps_reached": {k: len(gaps[k]) == preflight.MAX_GAPS
                                 for k in ("unobservable", "uncontrollable", "unjudgeable")}}


def assemble(packet: Packet, result: RunResult) -> PreflightManifest:
    """Bind the model's unchanged selections to discovery and measurement evidence.

    A partial chain is not a complete manifest. Field freshness stays unknown here because
    the existing calibration record lacks the full descriptors needed to establish identity
    across captures. The original measurements remain in the packet for review.
    """
    if any(stage.answer is None for stage in (result.readers, result.actions, result.progress)):
        raise ValueError("all three stages must be accepted before assembling a manifest")
    a4 = result.readers.answer.stage_4
    a5 = result.actions.answer.stage_5
    a6 = result.progress.answer.stage_6
    if a4 is None or a5 is None or a6 is None:
        raise ValueError("an accepted stage is missing its answer")
    answer = preflight.StageAnswer(stage_4=a4, stage_5=a5, stage_6=a6)
    problems = preflight.review(answer, "all", packet.discovery) + packet.review_bridge(answer, "actions")
    if problems:
        raise ValueError("the accepted stages no longer validate: " + "; ".join(problems))
    data = json.loads(packet.text)
    fields = {row["id"]: row for row in data["discovery"]["fields"]}
    controls = {row["id"]: row for row in data["offered_controls"]}
    summaries = {row["control"]: row for row in data["calibration"]["control_summaries"]}
    offered_fields = {row["id"]: row["source_field_id"] for row in data["offered_fields"]}
    if (set(packet.field_bindings) != set(packet.discovery.field_types) or
            len(set(packet.field_bindings.values())) != len(packet.field_bindings) or
            offered_fields != packet.field_bindings or
            not set(packet.field_bindings.values()) <= set(fields) or
            set(controls) != set(packet.discovery.control_types)
            or set(summaries) != set(controls)):
        raise ValueError("packet discovery and evidence do not match")
    readers = []
    for reader in a4.readers:
        source_id = packet.field_bindings[reader.field_id]
        descriptor = fields[source_id]
        declared = descriptor.get("field_type")
        if (declared if isinstance(declared, str) else "") != packet.discovery.field_types[reader.field_id]:
            raise ValueError("packet reader type differs from discovery")
        kind = field_kind(descriptor)
        if packet.discovery.field_kinds.get(reader.field_id, value_kind(declared)) != kind:
            raise ValueError("packet reader kind differs from its descriptor")
        readers.append({**reader.model_dump(mode="json"), "selection_id": reader.field_id, "field_id": source_id,
                        "declared_type": declared,
                        "kind": kind, "descriptor": descriptor, "freshness": None,
                        "freshness_reason": "field identity across captures is not established",
                        "freshness_evidence": next((dict(row, identity_verified=False) for row in
                            data["calibration"]["freshness"]["id_matches_unverified"] if row["field_id"] == reader.field_id), None)})
    actions = []
    for action in a5.actions:
        ids = [item.control_id for item in action.inputs]
        measured = [summaries[control] for control in ids]
        tap = measured[0]["lowest_tested_effective_requested_hold_s"] if action.hold_s is None and len(ids) == 1 else None
        if tap is not None and tap <= 0:
            tap = None
        tap_matches = [h for h in measured[0]["holds"] if tap is not None and h["requested_hold_s"] == tap]
        if tap is not None and (len(tap_matches) != 1 or tap_matches[0]["effective_in_all_repeats"] is not True):
            tap = None
        requested = action.hold_s if action.hold_s is not None else tap
        matching = [h["effective_in_all_repeats"] for h in measured[0]["holds"]
                    if requested is not None and h["requested_hold_s"] == requested] if len(ids) == 1 else []
        axis = any(packet.discovery.control_types[control] in preflight.AXIS_CONTROLS for control in ids)
        supported = len(ids) == 1 and not axis and requested is not None and requested <= 3600
        actions.append({**action.model_dump(mode="json"), "calibrations": measured, "tap_hold_s": tap,
                        "axis": axis, "max_hold_s": None,
                        "hold_effect": matching[0] if len(matching) == 1 else None,
                        "execution_supported": supported})
    skills = []
    for skill in a5.skills:
        ids = list(dict.fromkeys(item.control_id for step in skill.steps for item in step.inputs))
        skills.append({**skill.model_dump(mode="json"), "calibrations": [summaries[k] for k in ids],
                       "execution_supported": False})
    coverage = _coverage(data, readers, actions, skills)
    model_gaps = {"unobservable": a4.unobservable, "uncontrollable": a5.uncontrollable,
                  "unjudgeable": a6.unjudgeable}
    image = data["image"]
    manifest = PreflightManifest.model_validate({
        "schema_version": 2, "packet_sha256": hashlib.sha256(packet.text.encode("utf-8")).hexdigest(),
        "entity_id": data["calibration"]["entity_id"], "entity": data["calibration"]["entity"],
        "inventory_caps": data["inventory_caps"], "discovery_caps": data["discovery_caps"],
        "image": image, "readers": readers, "actions": actions, "skills": skills,
        "progress": [p.model_dump(mode="json") for p in a6.progress],
        "invariants": [i.model_dump(mode="json") for i in a6.invariants],
        "gaps": {**model_gaps, "scope": "model_report_relative_to_offered_and_selected_evidence", "coverage": coverage},
        "selection": _selection(data, readers, actions, skills, model_gaps),
        "reset_bindings": None,
        "reset_bindings_reason": "discovery did not emit callable reset candidates; no binding was selected",
    })
    problems = validate_evidence(manifest, packet)
    if problems:
        raise ValueError("manifest validation failed: " + "; ".join(problems))
    return manifest


def validate(manifest: PreflightManifest) -> list[str]:
    """Run dynamic reference and semantic checks not encoded by the exported schema."""
    problems: list[str] = []
    try:
        json.dumps(manifest.model_dump(mode="json"), allow_nan=False)
    except (TypeError, ValueError):
        problems.append("manifest contains non-finite or non-JSON data")
    field_types: dict[str, str] = {}
    for index, reader in enumerate(manifest.readers):
        declared = reader.declared_type or ""
        field_types[reader.selection_id] = declared
        if reader.descriptor.get("id") != reader.field_id or reader.descriptor.get("field_type") != reader.declared_type:
            problems.append(f"readers[{index}]: descriptor does not match its field id and type")
        expected_kind = field_kind(reader.descriptor)
        if reader.kind != expected_kind:
            problems.append(f"readers[{index}]: kind does not match the observed serialization")
        if not field_offered(reader.descriptor):
            problems.append(f"readers[{index}]: field was not readable in discovery")
        if reader.freshness is not None or not reader.freshness_reason:
            problems.append(f"readers[{index}]: freshness is not established across these captures")
    controls = {row["id"]: row["control_type"] for row in manifest.selection.offered_controls}
    field_kinds = {r.selection_id: field_kind(r.descriptor) for r in manifest.readers}
    discovery = preflight.Discovery(field_types, controls, field_kinds)
    a4 = {"readers": [{"field_id": r.selection_id, "alias": r.alias, "why": r.why} for r in manifest.readers],
          "unobservable": manifest.gaps.unobservable}
    a5 = {"actions": [{"name": a.name, "intent": a.intent, "inputs": [i.model_dump(mode="json") for i in a.inputs],
                       "hold_s": a.hold_s, "preconditions": [p.model_dump(mode="json") for p in a.preconditions]}
                      for a in manifest.actions],
          "skills": [{"name": s.name, "purpose": s.purpose, "max_s": s.max_s,
                      "steps": [step.model_dump(mode="json") for step in s.steps]} for s in manifest.skills],
          "uncontrollable": manifest.gaps.uncontrollable}
    a6 = {"progress": [p.model_dump(mode="json") for p in manifest.progress],
          "invariants": [i.model_dump(mode="json") for i in manifest.invariants],
          "unjudgeable": manifest.gaps.unjudgeable}
    try:
        answer = preflight.StageAnswer.model_validate({"stage_4": a4, "stage_5": a5, "stage_6": a6})
    except ValueError as exc:
        return problems + [str(exc)]
    return problems + preflight.review(answer, "all", discovery)


def validate_evidence(manifest: PreflightManifest, packet: Packet) -> list[str]:
    """Check a saved manifest against its exact source packet as well as its semantics."""
    problems = validate(manifest)
    if manifest.packet_sha256 != hashlib.sha256(packet.text.encode("utf-8")).hexdigest():
        problems.append("the packet hash does not match the manifest")
    data = json.loads(packet.text)
    fields = {row["id"]: row for row in data["discovery"]["fields"]}
    summaries = {row["control"]: row for row in data["calibration"]["control_summaries"]}
    if ({k: v.model_dump(mode="json") for k, v in manifest.inventory_caps.items()} != data["inventory_caps"] or
            {k: v.model_dump(mode="json") for k, v in manifest.discovery_caps.items()} != data["discovery_caps"]):
        problems.append("inspection coverage differs from the packet")
    readers = [r.model_dump(mode="json") for r in manifest.readers]
    actions = [a.model_dump(mode="json") for a in manifest.actions]
    skills = [s.model_dump(mode="json") for s in manifest.skills]
    model_gaps = {k: getattr(manifest.gaps, k) for k in ("unobservable", "uncontrollable", "unjudgeable")}
    if manifest.gaps.coverage != _coverage(data, readers, actions, skills):
        problems.append("coverage gaps differ from the packet and selections")
    if manifest.selection.model_dump(mode="json") != _selection(data, readers, actions, skills, model_gaps):
        problems.append("selection coverage differs from the packet and selections")
    for index, reader in enumerate(manifest.readers):
        if (packet.field_bindings.get(reader.selection_id) != reader.field_id or
                fields.get(reader.field_id) != reader.descriptor):
            problems.append(f"readers[{index}]: binding or descriptor differs from the packet")
        expected_freshness = next((dict(row, identity_verified=False) for row in
            data["calibration"]["freshness"]["id_matches_unverified"] if row["field_id"] == reader.selection_id), None)
        if reader.freshness_evidence != expected_freshness:
            problems.append(f"readers[{index}]: freshness evidence differs from the packet")
    for index, action in enumerate(manifest.actions):
        for calibration in action.calibrations:
            if summaries.get(calibration.control) != calibration.model_dump(mode="json"):
                problems.append(f"actions[{index}]: calibration differs from the packet")
    for index, skill in enumerate(manifest.skills):
        for calibration in skill.calibrations:
            if summaries.get(calibration.control) != calibration.model_dump(mode="json"):
                problems.append(f"skills[{index}]: calibration differs from the packet")
    if manifest.entity_id != data["calibration"]["entity_id"] or manifest.entity != data["calibration"]["entity"]:
        problems.append("the calibrated entity differs from the packet")
    expected_image = data["image"]
    if (manifest.image.model_dump(mode="json") if manifest.image is not None else None) != expected_image:
        problems.append("the screenshot descriptor differs from the packet")
    return problems


def validate_ledger(manifest: PreflightManifest, packet: Packet, rows: list[dict],
                    reservations: list[dict], *, expected_model: str | None = None) -> list[str]:
    """Recheck the recorded answers and compare the selections to the saved manifest.

    This checks consistency, not authenticity: the local ledger is not signed by a provider.
    """
    cursor = 0
    aliases: dict[str, str] = {}
    accepted: dict[str, stages.StageResult] = {}
    from playtest.preflight import provider
    from playtest.roles import strict
    if len(reservations) != len(rows):
        return ["ledger reservations and attempts do not match"]
    model = reservations[0].get("model") if reservations else None
    if model not in provider.MODEL_PRICES or any(r.get("model") != model for r in reservations):
        return ["ledger has an unsupported or changing requested model"]
    if expected_model is not None and model != expected_model:
        return ["ledger requested model differs from the required model"]
    schema = preflight.schema(packet.discovery)
    messages = [{"role": "system", "content": preflight.SYSTEM},
                {"role": "user", "content": preflight.first_turn(packet.text, "readers")}]
    for stage in stages.STAGES:
        if stage != "readers":
            messages.append({"role": "user", "content": packet.action_prompt if stage == "actions"
                             else preflight.STAGE_PROMPTS[stage]})
        stage_start = cursor
        stage_rows = []
        while cursor < len(rows) and isinstance(rows[cursor], dict) and rows[cursor].get("stage") == stage:
            stage_rows.append(rows[cursor])
            cursor += 1
        if not 1 <= len(stage_rows) <= stages.MAX_RETRIES + 1:
            return [f"{stage}: ledger must contain one to four ordered attempts"]
        answer = None
        for index, row in enumerate(stage_rows):
            request = provider.build_request(packet, messages, schema, model=model)
            digest = hashlib.sha256(strict.canonical(request).encode("utf-8")).hexdigest()
            reservation = reservations[stage_start + index]
            if reservation.get("request_sha256") != digest:
                return [f"{stage}: reserved request hash or model does not match the reconstructed request"]
            metadata = row.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("model") != model:
                return [f"{stage}: recorded requested model differs from the reservation"]
            resolved = metadata.get("resolved_model")
            if (resolved is not None and resolved != model) or (expected_model is not None and resolved != expected_model):
                return [f"{stage}: resolved model does not match the required model"]
            if (isinstance(row.get("number"), bool) or row.get("number") != index + 1 or
                    row.get("transport_error") is not None or row.get("failure_kind") is not None or
                    row.get("failure") is not None):
                return [f"{stage}: ledger attempt order or transport outcome is invalid"]
            content = row.get("content")
            if not isinstance(content, str):
                return [f"{stage}: ledger answer is not text"]
            answer, problems = stages._validate(content, stage, packet.discovery, aliases)
            if answer is not None:
                problems = packet.review_bridge(answer, stage)
                if problems:
                    answer = None
            if row.get("problems") != problems:
                return [f"{stage}: recorded validation problems differ from revalidation"]
            if (index < len(stage_rows) - 1 and answer is not None) or (index == len(stage_rows) - 1 and answer is None):
                return [f"{stage}: ledger must stop on its first accepted answer"]
            messages.append({"role": "assistant", "content": content})
            if answer is None:
                messages.append({"role": "user", "content": preflight.retry_turn(stage, problems)})
        assert answer is not None
        accepted[stage] = stages.StageResult(answer, ())
        if stage == "readers":
            aliases = answer.aliases()
    if cursor != len(rows):
        return ["ledger has extra or out-of-order attempts"]
    try:
        expected = assemble(packet, stages.RunResult(accepted["readers"], accepted["actions"], accepted["progress"]))
    except ValueError as exc:
        return [f"ledger answers cannot assemble a manifest: {exc}"]
    if expected.model_dump(mode="json") != manifest.model_dump(mode="json"):
        return ["the manifest differs from the recorded accepted answers"]
    return []
