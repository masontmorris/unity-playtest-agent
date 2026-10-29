"""Verify one named declaration against its stored release-verified execution evidence.

This is the complementary verifier to stage 7's single-button measurement. It does not send
input, choose, rank or repair a declaration. It reports whether the declared inputs, values,
holds and ordered steps are exactly covered by the evidence, and whether release was verified.
"""

from __future__ import annotations

from typing import Any

from playtest.contracts.action_catalog import (
    ActionDefinition,
    ActionStep,
    InputBinding,
    binding_requirements,
    evidence_problems,
    step_requirements,
)
from playtest.contracts.manifest import ManifestAction, ManifestSkill, PreflightManifest


def _binding(item) -> InputBinding:
    return InputBinding(device=item.device, control=item.control, x=item.x, y=item.y)


def _hold(value) -> float:
    return value.hold_s if value.hold_s is not None else value.tap_hold_s


def _action_definition(action: ManifestAction) -> ActionDefinition | None:
    hold = _hold(action)
    if hold is None:
        return None
    return ActionDefinition(backend="input_system",
                            inputs=[_binding(item) for item in action.inputs],
                            default_lease_s=hold)


def _action_requirements(action: ManifestAction):
    definition = _action_definition(action)
    return () if definition is None else binding_requirements(definition.inputs or (), definition.default_lease_s)


def _skill_definition(skill: ManifestSkill) -> ActionDefinition | None:
    if any(_hold(step) is None for step in skill.steps):
        return None
    return ActionDefinition(backend="input_system", steps=[
        ActionStep(inputs=[_binding(item) for item in step.inputs], hold_s=_hold(step), wait_s=step.wait_s)
        for step in skill.steps], max_s=skill.max_s)


def _skill_requirements(skill: ManifestSkill):
    definition = _skill_definition(skill)
    return () if definition is None else step_requirements(definition.steps or ())


def verify_declaration(value: PreflightManifest, name: str) -> dict[str, Any]:
    """Return an evidence review for exactly one manifest action or skill name."""
    if not isinstance(name, str) or not name:
        raise ValueError("a declaration name is required")
    actions = [item for item in value.actions if item.name == name]
    skills = [item for item in value.skills if item.name == name]
    if len(actions) + len(skills) != 1:
        raise ValueError(f"{name!r} is not exactly one manifest action or skill")
    declaration = actions[0] if actions else skills[0]
    kind = "action" if actions else "skill"
    definition = _action_definition(declaration) if kind == "action" else _skill_definition(declaration)
    requirements = (_action_requirements(declaration) if kind == "action"
                    else _skill_requirements(declaration))
    evidence = declaration.execution_evidence
    if definition is None:
        problems = ["the declaration has no resolved positive holds"]
    else:
        problems = list(evidence_problems(evidence, definition))
    supported = declaration.execution_supported
    if supported != (not problems):
        problems.append("the manifest's execution_supported flag disagrees with this evidence review")
    return {
        "claim_scope": ("one declaration's stored execution receipts and release verification; not a game-effect "
                        "claim and not a fresh session"),
        "name": name,
        "kind": kind,
        "analysis": "stored_execution_evidence",
        "status": "supported" if supported and not problems else "unsupported",
        "execution_supported": supported,
        "problems": problems,
        "requirements": [{
            "step_index": item.step_index,
            "device": item.device,
            "control": item.control,
            "x": item.x,
            "y": item.y,
            "declared_hold_s": item.declared_hold_s,
            "pointer_impulse": item.pointer_impulse,
            "wait_s": item.wait_s,
        } for item in requirements],
        "evidence": None if evidence is None else {
            "action": evidence.action,
            "session": evidence.session,
            "status": evidence.status.value,
            "release_verified": evidence.release_verified,
            "declaration": evidence.declaration.model_dump(mode="json"),
            "applied_duration_s": evidence.applied_duration_s,
            "input_start_wall_s": evidence.input_start_wall_s,
            "input_end_wall_s": evidence.input_end_wall_s,
            "receipt_count": len(evidence.receipts),
            "wall_utc": evidence.wall_utc.isoformat() if evidence.wall_utc is not None else None,
        },
    }
