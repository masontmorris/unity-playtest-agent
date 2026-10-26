"""Synthetic escalation packets for the frontier schema probe (#73). Not part of the product.

The content is invented and uses generic names. Each scenario is what the core would send when S1
escalates: a run-stable manifest message first, then one packet. Sequence numbers follow the v0
bridge convention (#14, #19): they are the bridge's own per-sender counter, shared by its
observations and execution records, so they are unique within a session.

  A  a save made while holding the item loads without it (a candidate finding is plausible)
  B  forward inputs stop moving the entity while a directive is open (control problem)
  C  follow-up: the directive testing A's hypothesis ended not_met on a precondition
  D  the screen went dark after a load while every field stayed unchanged (no field can score it)
"""

from __future__ import annotations

import json
from typing import Any

from playtest.roles import frontier

OBJECTIVE = "Explore the item and save systems. Look for state that is wrong after a save is loaded or the scene resets."

FIELDS: list[dict[str, str]] = [
    {"path": "scene", "kind": "string", "about": "active scene name"},
    {"path": "paused", "kind": "bool", "about": "the game is paused"},
    {"path": "public.entity_pos", "kind": "vector", "about": "controlled entity position, metres"},
    {"path": "public.entity_speed", "kind": "number", "about": "controlled entity speed, m/s"},
    {"path": "public.item_held", "kind": "bool", "about": "the entity holds the item"},
    {"path": "public.item_count", "kind": "number", "about": "items collected this session"},
    {"path": "public.gate_open", "kind": "bool", "about": "the gate ahead of the start point is open"},
    {"path": "public.save_slot_used", "kind": "bool", "about": "a saved game exists"},
    {"path": "public.timer_s", "kind": "number", "about": "level timer, seconds"},
]

ACTIONS: list[dict[str, Any]] = [
    {"name": "move_forward", "intent": "walk forward", "axis": True, "hold_s": 1.0, "max_hold_s": 3.0,
     "calibrated": "3.2 +/- 0.4 m per 1.0 s"},
    {"name": "move_back", "intent": "walk backward", "axis": True, "hold_s": 1.0, "max_hold_s": 3.0,
     "calibrated": "2.9 +/- 0.3 m per 1.0 s"},
    {"name": "strafe_left", "intent": "step left", "axis": True, "hold_s": 0.5, "max_hold_s": 2.0,
     "calibrated": "1.4 +/- 0.2 m per 0.5 s"},
    {"name": "strafe_right", "intent": "step right", "axis": True, "hold_s": 0.5, "max_hold_s": 2.0,
     "calibrated": "1.4 +/- 0.2 m per 0.5 s"},
    {"name": "turn_left", "intent": "turn 30 degrees left", "axis": True, "hold_s": 0.3, "max_hold_s": 1.5,
     "calibrated": "29 +/- 3 degrees per 0.3 s"},
    {"name": "turn_right", "intent": "turn 30 degrees right", "axis": True, "hold_s": 0.3, "max_hold_s": 1.5,
     "calibrated": "29 +/- 3 degrees per 0.3 s"},
    {"name": "interact", "intent": "use the highlighted object", "axis": False, "hold_s": None, "max_hold_s": None,
     "calibrated": "no position effect"},
]
SKILLS: list[dict[str, str]] = [
    {"name": "save_game", "purpose": "open the menu and save to the slot"},
    {"name": "load_save", "purpose": "open the menu and load the saved slot"},
    {"name": "reset_scene", "purpose": "reload the scene from its start state"},
]


def manifest() -> frontier.Manifest:
    return frontier.Manifest(
        fields={f["path"]: f["kind"] for f in FIELDS},  # type: ignore[misc]
        actions={a["name"]: frontier.ActionSpec(axis=a["axis"], max_hold_s=a["max_hold_s"]) for a in ACTIONS},
        skills=[s["name"] for s in SKILLS],
    )


def manifest_message() -> str:
    """The run-stable first user message. Byte-identical for every escalation in a run."""
    return json.dumps({"objective": OBJECTIVE, "clock_mode": "realtime", "fields": FIELDS, "actions": ACTIONS,
                       "skills": SKILLS,
                       "notes": "seq values are bridge observation and execution sequence numbers; game_s is "
                                "game time. A null field value is unknown, not zero."},
                      separators=(",", ":"))


def _obs(seq: int, game_s: float, **public: Any) -> dict[str, Any]:
    return {"seq": seq, "type": "observation", "game_s": game_s, "public": public}


def _exe(seq: int, choice: str, start: float, end: float | None, status: str = "completed", **extra: Any) -> dict:
    return {"seq": seq, "type": "execution", "choice": choice, "status": status, "input_game_s": [start, end], **extra}


CHOICES = [
    {"id": "c1", "label": "move_forward 1.0 s"}, {"id": "c2", "label": "move_back 1.0 s"},
    {"id": "c3", "label": "strafe_left 0.5 s"}, {"id": "c4", "label": "strafe_right 0.5 s"},
    {"id": "c5", "label": "turn_left"}, {"id": "c6", "label": "turn_right"}, {"id": "c7", "label": "interact"},
    {"id": "c8", "label": "save_game (skill)"}, {"id": "c9", "label": "load_save (skill)"},
    {"id": "c10", "label": "reset_scene (skill)"}, {"id": "c11", "label": "wait 0.5 s"},
    {"id": "c12", "label": "Ask the frontier for help"}, {"id": "c13", "label": "Finish the run"},
]

SCENARIOS: dict[str, dict[str, Any]] = {
    "A": {
        "as_of": {"seq": 2046, "game_s": 74.1, "age_s": 0.22, "sample_gap_s": 0.05},
        "escalation": {"by": "s1", "choice_id": "c12", "while_pending": {"mode": "wait"}},
        "directive": None,
        "directive_history": [],
        "hypotheses": [],
        "findings": [],
        "new_hypothesis_ids": ["h1", "h2", "h3"],
        "facts": [
            {"text": "public.item_held changed true -> false across load_save (seq 2031 -> 2040)", "seq": [2031, 2040]},
            {"text": "rule item_count_never_decreases violated: 1 -> 0 at seq 2040", "seq": [2040]},
            {"text": "the save at seq 2022 completed while item_held was true", "seq": [2022, 2024]},
        ],
        "recent": [
            _obs(2001, 61.0, item_held=False, item_count=0, gate_open=False, save_slot_used=False, entity_speed=0.0),
            _exe(2008, "interact", 62.0, 62.1),
            _obs(2010, 62.4, item_held=True, item_count=1, gate_open=False, save_slot_used=False, entity_speed=0.0),
            _exe(2022, "save_game", 66.0, 68.2),
            _obs(2024, 68.4, item_held=True, item_count=1, gate_open=False, save_slot_used=True, entity_speed=0.0),
            _obs(2031, 70.0, item_held=True, item_count=1, gate_open=False, save_slot_used=True, entity_speed=0.0),
            _exe(2038, "load_save", 70.2, 72.1),
            _obs(2040, 72.3, item_held=False, item_count=0, gate_open=False, save_slot_used=True, entity_speed=0.0),
            _obs(2046, 74.1, item_held=False, item_count=0, gate_open=False, save_slot_used=True, entity_speed=0.0),
        ],
        "choices_s1_had": CHOICES,
        "clip": None,
    },
    "B": {
        "as_of": {"seq": 3020, "game_s": 41.6, "age_s": 0.25, "sample_gap_s": 0.05},
        "escalation": {"by": "s1", "choice_id": "c12", "while_pending": {"mode": "continue", "choice_id": "c5"}},
        "directive": {"id": "d3", "text": "Walk to the gate and open it.",
                      "expected": {"field": "public.gate_open", "op": "==", "value": True},
                      "deadline_game_s": 60.0, "outcome": "open"},
        "directive_history": [],
        "hypotheses": [],
        "findings": [],
        "new_hypothesis_ids": ["h4", "h5", "h6"],
        "facts": [
            {"text": "4 of the last 4 move_forward inputs moved the entity < 0.1 m; calibration expects "
                     "3.2 +/- 0.4 m per 1.0 s", "seq": [3003, 3007, 3011, 3015]},
            {"text": "entity_speed stayed 0.0 during all four inputs", "seq": [3004, 3008, 3012, 3016]},
            {"text": "gate_open has been false since seq 2990", "seq": [2990]},
        ],
        "recent": [
            _obs(2990, 36.0, gate_open=False, entity_pos=[4.1, 0.0, 9.6], entity_speed=0.0, paused=False),
            *[r for k, s in enumerate((3003, 3007, 3011, 3015)) for r in (
                _exe(s, "move_forward", 37.0 + k, 38.0 + k,
                     expected_effect=[{"field": "public.entity_pos", "delta": 3.2, "spread": 0.4, "unit": "m"}],
                     observed_effect=[{"field": "public.entity_pos", "delta": 0.04 * (k + 1), "spread": None,
                                       "unit": "m"}]),
                _obs(s + 1, 38.1 + k, gate_open=False, entity_pos=[4.1, 0.0, 9.6 + 0.01 * k], entity_speed=0.0,
                     paused=False),
            )],
            _obs(3020, 41.6, gate_open=False, entity_pos=[4.1, 0.0, 9.64], entity_speed=0.0, paused=False),
        ],
        "choices_s1_had": CHOICES,
        "clip": None,
    },
    "C": {
        "as_of": {"seq": 2140, "game_s": 101.3, "age_s": 0.2, "sample_gap_s": 0.05},
        "escalation": {"by": "s1", "choice_id": "c12", "while_pending": {"mode": "wait"}},
        "directive": None,
        "directive_history": [
            {"id": "d1", "text": "Pick up the item, save, load the save, then check item_held.",
             "expected": {"field": "public.item_held", "op": "==", "value": True}, "deadline_game_s": 95.0,
             "outcome": "not_met", "error_class": "precondition_false", "evidence_seq": [2131]},
        ],
        "hypotheses": [
            {"id": "h1", "statement": "Loading a save drops the held item", "status": "open",
             "supporting_seq": [2040], "contradicting_seq": []},
        ],
        "findings": [],
        "new_hypothesis_ids": ["h2", "h3"],
        "facts": [
            {"text": "interact was rejected stale_fields twice: depends_on public.item_held changed before the input",
             "seq": [2112, 2118]},
            {"text": "item_held stayed false for the whole d1 window", "seq": [2101, 2131]},
        ],
        "recent": [
            _obs(2101, 80.2, item_held=False, item_count=0, save_slot_used=True, entity_speed=0.0),
            _exe(2112, "interact", 84.0, None, status="rejected", reject_reason="stale_fields"),
            _exe(2118, "interact", 86.5, None, status="rejected", reject_reason="stale_fields"),
            _exe(2124, "save_game", 88.0, 90.1),
            _exe(2128, "load_save", 90.4, 92.2),
            _obs(2131, 95.0, item_held=False, item_count=0, save_slot_used=True, entity_speed=0.0),
            _obs(2140, 101.3, item_held=False, item_count=0, save_slot_used=True, entity_speed=0.0),
        ],
        "choices_s1_had": CHOICES,
        "clip": None,
    },
    "D": {
        "as_of": {"seq": 4031, "game_s": 130.4, "age_s": 0.3, "sample_gap_s": 0.05},
        "escalation": {"by": "s1", "choice_id": "c12", "while_pending": {"mode": "wait"}},
        "directive": None,
        "directive_history": [],
        "hypotheses": [],
        "findings": [],
        "new_hypothesis_ids": ["h7", "h8", "h9"],
        "facts": [
            {"text": "capture: mean frame luminance 0.02 (near black) for 21 consecutive frames after load_save, "
                     "then back to 0.41", "seq": [4012, 4030]},
            {"text": "every public field was unchanged from seq 4008 to seq 4031", "seq": [4008, 4031]},
        ],
        "recent": [
            _obs(4008, 125.9, item_held=True, item_count=2, gate_open=True, save_slot_used=True, entity_speed=0.0,
                 paused=False),
            _exe(4010, "load_save", 126.0, 127.9),
            _obs(4012, 128.0, item_held=True, item_count=2, gate_open=True, save_slot_used=True, entity_speed=0.0,
                 paused=False),
            _obs(4030, 130.1, item_held=True, item_count=2, gate_open=True, save_slot_used=True, entity_speed=0.0,
                 paused=False),
            _obs(4031, 130.4, item_held=True, item_count=2, gate_open=True, save_slot_used=True, entity_speed=0.0,
                 paused=False),
        ],
        "choices_s1_had": CHOICES,
        "clip": {"from_seq": 4008, "to_seq": 4031, "seconds": 4.0, "attached": False},
    },
}


def packet_message(name: str, *, clip_attached: bool = False) -> str:
    packet = json.loads(json.dumps(SCENARIOS[name]))
    if clip_attached and packet.get("clip"):
        packet["clip"]["attached"] = True
    return json.dumps({"escalation_packet": packet}, separators=(",", ":"))


def citable(name: str) -> frozenset[int]:
    """Every sequence number the packet shows, so every one the frontier may cite."""
    found: set[int] = set()

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, int) and not isinstance(node, bool) and key in (
                "seq", "supporting_seq", "contradicting_seq", "evidence_seq", "from_seq", "to_seq"):
            found.add(node)

    walk(SCENARIOS[name])
    return frozenset(found)


def refs(name: str) -> frontier.PacketRefs:
    p = SCENARIOS[name]
    return frontier.PacketRefs(seqs=citable(name), hypotheses=frozenset(h["id"] for h in p["hypotheses"]),
                               new_hypothesis_ids=tuple(p["new_hypothesis_ids"]),
                               open_directive=p["directive"] is not None)
