"""Synthetic sample packets shaped like real S1 and frontier requests.

The content is invented for the probes. It is sized to match the plan (S1 at most 12k tokens),
and it is not a quality benchmark: that is the S1 bench's job (#24).
"""

from __future__ import annotations

import json
from typing import Any

CHOICES: dict[str, str] = {
    "c1": "Move forward 1.0 s (calibrated: 3.2 +/- 0.4 m)",
    "c2": "Move backward 1.0 s (calibrated: 2.9 +/- 0.3 m)",
    "c3": "Strafe left 0.5 s (calibrated: 1.4 +/- 0.2 m)",
    "c4": "Strafe right 0.5 s (calibrated: 1.4 +/- 0.2 m)",
    "c5": "Turn left 30 degrees",
    "c6": "Turn right 30 degrees",
    "c7": "Jump",
    "c8": "Interact with the highlighted object",
    "c9": "Reload checkpoint (skill)",
    "c10": "Ask the frontier for help (escalate); choose what to do while it thinks",
    "c11": "Wait 0.5 s",
    "c12": "Finish the run",
}

INSTRUCTIONS = (
    "You are playtesting a game to find bugs. Choose the single next choice. Only the listed choices exist. "
    "Escalate when you cannot make progress on the objective or you see something that may be a bug and need "
    "a controlled test. Facts are measured; null means unknown, not zero."
)


def _entry(i: int) -> dict[str, Any]:
    return {
        "seq": 1000 + i * 7,
        "game_s": round(20 + i * 0.7, 2),
        "choice": f"c{1 + i % 8}",
        "result": "completed",
        "expected": {"field": "public.r_entity.pos", "delta_m": 3.2, "spread_m": 0.4},
        "observed": {"field": "public.r_entity.pos", "delta_m": round(3.0 + (i % 5) * 0.1, 2)},
        "note": "door state unchanged" if i % 4 else "counter r_counter increased by 1",
    }


def s1_state(history: int) -> dict[str, Any]:
    return {
        "objective": "Explore the checkpoint and pickup systems; look for state that is wrong after a reload.",
        "as_of": {"seq": 1842, "game_s": 61.25, "age_s": 0.21, "sample_gap_s": 0.05},
        "observation": {
            "scene": "Level1", "playing": True,
            "public": {"r_entity": {"pos": [1.2, 0.0, 4.8], "vel": [0.0, 0.0, 0.0]}, "r_counter": 3,
                       "r_item_held": False, "r_door_open": None},
            "reader_errors": [{"reader": "r_door_open", "error": "target not found"}],
        },
        "facts": [
            "3 of the last 4 move inputs produced < 0.1 m displacement",
            "invariant 'r_counter never decreases' holds over 512 samples",
            "r_item_held changed true -> false at seq 1801 after 'Reload checkpoint'",
        ],
        "directive": {"id": "d11", "text": "Pick up the item, reload, check it is still held.",
                      "expected": "public.r_item_held == true", "deadline_game_s": 90, "outcome": "open"},
        "pending_help": None,
        "recent": [_entry(i) for i in range(history)],
        "choice_set_version": 57,
    }


def s1_body(model: str, history: int, image_data_url: str | None = None, image_transport: str = "messages") -> dict:
    state: Any = s1_state(history)
    body: dict[str, Any] = {
        "model": model,
        "questions": {"next_action": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CHOICES}},
    }
    if image_data_url and image_transport == "messages":  # Baseten: OpenAI-style content parts inside state
        state = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": json.dumps(state)},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]}]}
    elif image_data_url and image_transport == "images":  # Liquid: top-level images array
        state["game_view"] = "images[0]: the current game frame"
        body["images"] = [image_data_url]
    body["state"] = state
    return body


FRONTIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["directive", "hypotheses", "add_choices", "candidate_finding"],
    "properties": {
        "directive": {
            "type": "object", "additionalProperties": False,
            "required": ["text", "expected", "deadline_game_s"],
            "properties": {
                "text": {"type": "string"},
                "expected": {"anyOf": [{"type": "null"}, {
                    "type": "object", "additionalProperties": False, "required": ["field", "op", "value"],
                    "properties": {"field": {"type": "string"},
                                   "op": {"type": "string", "enum": ["==", "!=", "<", "<=", ">", ">=", "changed", "unchanged"]},
                                   "value": {"type": ["string", "number", "boolean", "null"]}}}]},
                "deadline_game_s": {"type": "number"},
            },
        },
        "hypotheses": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["text", "confirm_if", "refute_if"],
            "properties": {"text": {"type": "string"}, "confirm_if": {"type": "string"}, "refute_if": {"type": "string"}}}},
        "add_choices": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["skill_or_action", "params"],
            # Strict mode rejects open objects, so parameters are name/value pairs.
            "properties": {"skill_or_action": {"type": "string"}, "params": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["name", "value"],
                "properties": {"name": {"type": "string"}, "value": {"type": ["string", "number", "boolean"]}}}}}}},
        "candidate_finding": {"anyOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False, "required": ["title", "evidence_seq"],
            "properties": {"title": {"type": "string"}, "evidence_seq": {"type": "array", "items": {"type": "integer"}}}}]},
    },
}

FRONTIER_SYSTEM = (
    "You are the frontier investigator for an autonomous game playtester. A fast decision model (S1) plays and "
    "has escalated to you. Diagnose what is happening, state hypotheses with what would confirm and refute each, "
    "and return one directive with a checkable postcondition over a public observation field (or null if none "
    "can be stated). You never issue input. Cite only trace sequence numbers present in the packet."
)


def frontier_user(history: int) -> str:
    packet = {"escalation": {"by": "s1", "reason_choice": "c10", "while_pending": {"mode": "wait"}},
              **s1_state(history), "choices_s1_had": CHOICES,
              "manifest_skills": ["reload_checkpoint", "reset_scene", "hold_action(action, duration_s)"],
              "open_hypotheses": []}
    return json.dumps(packet)
