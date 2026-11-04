"""Expectations written before measurement, scored against the measured guard tables.

A finding needs an oracle: something that says what should have happened. A post-session reviewer who reads the
whole trace and then decides what was wrong forms the expectation after seeing the outcome, so flicker, a lagging
reader or a rule of the game reads as a bug. Here the expectation comes first. The frontier is given only what the
game declares before play: each verb's stated intent and controls, the readers with the fields they read and the
methods that write them, and the values the authors set. It sees no source text (a seeded line would read as
intended) and no measurement (noise would read as a bug). It writes falsifiable predictions in the guard table's
own terms: in this state, this verb changes this observable, in this direction.

Code then scores each prediction against every fresh session's guard table, mechanically. A prediction that held
in no more than half of them is a mismatch: the game's own declaration says one thing, the measurement another,
k of n times from a clean start. Only effects that should happen are scored; an effect that needs a condition the
guard states do not give (a target in range, ammunition) is set aside, and an observable that changes by itself
in that state cannot show a press's effect, so it is never scored as missing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from playtest.runner.declared import declared_configuration, object_configuration

CAMERA = ("camera.position", "camera.rotation", "camera.field_of_view")
BODY = "body_motion"  # the body moved measurably differently from the same state without the verb
STATES = ("rest", "moving", "air")
EXPECT = ("changes", "increases", "decreases", "reaches", "moves_body")
TOLERANCE = 0.05   # a reaches prediction holds within this share of its value (or of 1, for a value near 0)
DISPLACEMENT_RATIO = 0.25
BASIS = ("declared_value", "intent", "names")
MODEL = "muse-spark-1.3"  # the default; `--model` on predict and investigate chooses another priced one
TIMEOUT_S = 180.0
MAX_WRITERS = 6
UNOBSERVED = 4.0   # a reading gap this many usual intervals long is a stretch nothing was read in, not a slow turn

SYSTEM = (
    "You write down, before anything is measured, what each verb of a game you have not seen should do. A verb is "
    "an input other than the movement and the rise (jump). Each verb will be pressed in these states, each against "
    "the same state without it: rest (standing still on the ground, nothing pressed for a while, so any cooldown "
    "has run out), each listed move held (the move's name is the state), and air (pressed at the top of a single "
    "jump from the ground, so anything a game allows once per jump is still unused). Nothing to interact with, pick "
    "up or hit is known to be near. What can be measured is the listed readers (each reads one field of the game; "
    "written_by names the methods that write that field), the camera's position, rotation and field of view "
    "(camera.position, camera.rotation, camera.field_of_view), and body_motion: whether the body got measurably "
    "further from where it started, at its furthest, than in the same state without the verb. "
    "declared_configuration and referenced_assets are the values the authors set, read before play.\n"
    "Write predictions: verb, state, observable, expect, value. state is rest, air, a move's name, or moving for "
    "every listed move alike. expect is changes, increases or decreases for a reader or a camera part; reaches, "
    "with value, when a declared value says what the reading must become (a field of view, a count, a flag), "
    "giving the arithmetic in because; or moves_body for body_motion. value is null unless expect is reaches. Use "
    "reaches only for a reading the declared value is the destination of (a view that settles at a declared field "
    "of view, a count that stops at a declared maximum, a flag), never for a timer, a cooldown or a blend, which "
    "count through values and may count either way. Use "
    "increases or decreases only when the direction follows from the names or declared values (a blend toward its "
    "full value, a view that narrows); otherwise use changes. A prediction says the effect should be measured every "
    "time the verb is pressed in that state, so predict only what the game's own declarations say: a stated "
    "intent, a declared value (cite it in because), or the names of the action, the fields and the methods that "
    "write them. basis names which. Predict only effects that should happen. Whether a verb works at rest, while "
    "moving one way or the other, or in the air is often a deliberate rule (a sprint only while moving forward, a "
    "dash only while a direction is held, an attack only on the ground): predict a verb in a state only when its "
    "declarations say it works there, not merely that it exists, and use moving only when they say it works in "
    "every direction. When an effect needs a condition the states do not give (a target in range, something to "
    "pick up, ammunition, a menu open, an enemy near), state that condition in precondition: such a prediction is "
    "kept but not scored. precondition is null only when the effect must happen whenever the verb is pressed in "
    "that state. In a move's state, a reader the move itself changes (its own input, the body's speed) cannot "
    "show the verb. Every prediction is scored by code against the measurement, and one that is wrong becomes a "
    "false report, so write only the ones you would stake on, with the one observable that most directly shows "
    "the effect, and at most four per verb. The rise itself may be predicted in the air state only: pressed again "
    "at the top of its own jump (a double or air jump), with observable body_motion and expect moves_body, when "
    "the declarations say a second jump exists.\n"
    "Then write what must hold in play, which is scored over the readings of whole play sessions (one reading "
    "about every 0.8 s): ranges, a reader that must stay within a declared bound at every reading (a health never "
    "above its declared maximum, a count never above a declared limit), with min or max and the declared value it "
    "comes from; and transitions, a reader change that must be followed within within_s seconds by another "
    "reader's change (when the health drops, the invulnerability flag becomes true; when dead becomes true, the "
    "health is restored by the declared amount within the respawn time). A transition's when_change is increases, "
    "decreases or becomes (with when_value); then_expect is changes, increases, decreases or reaches (with "
    "then_value). Write durations too: a reader that, once it takes a value, keeps it for a declared number of "
    "seconds and then leaves it (a dash flag on for the declared dash duration, invulnerability on for the "
    "declared invulnerability time), with the value and the seconds. Use only readers, only rules the declarations "
    "state, never a rule the game might break on purpose (a cheat, a pickup that overfills, a mode), and never a "
    "timer or a cooldown as a bound. Cover every declared value that bounds, times or follows from something a "
    "reader shows, at most ten of each.\n"
    "Write action rules too: when the player presses an input (a verb, a move or the rise) and, if the rule needs "
    "one, a reader satisfies a condition just before the press, another reader must follow within within_s "
    "seconds (attack, with its cooldown run out, spawns a projectile; dash sets the dash flag and clears it after "
    "the declared duration). These are scored at every press in recorded play, so a condition matters: give it as "
    "a reader compared with a value (condition_observable, condition_op, condition_value), or, when no reader can "
    "express it (an NPC in range, an enemy near, standing on a pickup), describe it in situation, in a few words a "
    "player could bring about, with the reader condition null: such a rule is tested in a directed session. "
    "situation is null when the press alone must do it. Do not repeat what the guard predictions already say; at "
    "most ten. An effect that lasts only while the input is held (an input value, a stance, a zoom) is the guard "
    "predictions' to state; an action rule is for what a press leaves behind for at least a moment after it ends.\n"
    "Last, write magnitudes: a declared size the guard measurements can check, in world units and seconds. "
    "move_speed is a move's average speed over its press (state null); rise_height is how high the rise alone "
    "lifts the body (state null); verb_speed is the body's average speed over a verb's press while a move is held "
    "(state: the move's name). Each verb's and move's press length is given (pressed_s, pressed_with_move_s). Give "
    "one only when a declared value fixes it with arithmetic you can show in because (a declared speed applied for "
    "the whole press; a jump height from a declared jump speed and gravity you can see declared), never when an "
    "undeclared factor (acceleration, a gravity you cannot see, a curve) sets it. At most eight."
)


PLAY_BASIS = ("declared_value", "intent", "names")
NUMBER_OR_NULL = {"type": ["number", "null"]}
VALUE = {"type": ["number", "boolean", "null"]}
RANGE = {"type": "object", "additionalProperties": False,
         "required": ["observable", "min", "max", "basis", "because"],
         "properties": {"observable": {"type": "string"}, "min": NUMBER_OR_NULL, "max": NUMBER_OR_NULL,
                        "basis": {"type": "string", "enum": list(PLAY_BASIS)}, "because": {"type": "string"}}}
TRANSITION = {"type": "object", "additionalProperties": False,
              "required": ["when", "when_change", "when_value", "then", "then_expect", "then_value", "within_s",
                           "basis", "because"],
              "properties": {"when": {"type": "string"},
                             "when_change": {"type": "string", "enum": ["increases", "decreases", "becomes"]},
                             "when_value": VALUE, "then": {"type": "string"},
                             "then_expect": {"type": "string", "enum": ["changes", "increases", "decreases", "reaches"]},
                             "then_value": VALUE, "within_s": {"type": "number"},
                             "basis": {"type": "string", "enum": list(PLAY_BASIS)}, "because": {"type": "string"}}}


DURATION = {"type": "object", "additionalProperties": False,
            "required": ["observable", "value", "seconds", "basis", "because"],
            "properties": {"observable": {"type": "string"}, "value": {"type": ["number", "boolean"]},
                           "seconds": {"type": "number"},
                           "basis": {"type": "string", "enum": list(PLAY_BASIS)}, "because": {"type": "string"}}}
QUANTITIES = ("move_speed", "rise_height", "verb_speed")
CONDITION_OPS = ["==", "!=", "<", "<=", ">", ">="]
ACTION_RULE = {"type": "object", "additionalProperties": False,
               "required": ["action", "condition_observable", "condition_op", "condition_value", "situation",
                            "target_object", "within", "then", "then_expect", "then_value", "within_s", "basis",
                            "because"],
               "properties": {"action": {"type": "string", "description": "the input pressed: a listed verb, move or rise"},
                              "condition_observable": {"type": ["string", "null"],
                                                       "description": "a listed reader's alias, or null"},
                              "condition_op": {"type": ["string", "null"], "enum": [*CONDITION_OPS, None]},
                              "condition_value": {"type": ["number", "boolean", "string", "null"]},
                              "situation": {"type": ["string", "null"]},
                              "target_object": {"type": ["string", "null"],
                                                "description": "for a situation near a scene object: its id from "
                                                               "objects; otherwise null"},
                              "within": {"type": ["number", "null"],
                                         "description": "with target_object: how near it, in world units, the body "
                                                        "must be for the situation to hold"},
                              "then": {"type": "string", "description": "the alias of the listed reader that must "
                                                                        "follow the press"},
                              "then_expect": {"type": "string", "enum": ["changes", "increases", "decreases", "reaches"],
                                              "description": "how that reader must follow"},
                              "then_value": VALUE, "within_s": {"type": "number"},
                              "basis": {"type": "string", "enum": list(PLAY_BASIS)}, "because": {"type": "string"}}}


def schema(moves: Sequence[str]) -> dict[str, Any]:
    """The answer's shape: a state is rest, air, moving (every move alike) or one move's name."""
    magnitude = {"type": "object", "additionalProperties": False,
                 "required": ["quantity", "action", "state", "value", "basis", "because"],
                 "properties": {"quantity": {"type": "string", "enum": list(QUANTITIES)}, "action": {"type": "string"},
                                "state": {"type": ["string", "null"], "enum": [*moves, None]},
                                "value": {"type": "number"},
                                "basis": {"type": "string", "enum": list(PLAY_BASIS)}, "because": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["predictions", "ranges", "transitions", "durations", "magnitudes", "action_rules"],
            "properties": {
        "action_rules": {"type": "array", "maxItems": 10, "items": ACTION_RULE},
        "ranges": {"type": "array", "maxItems": 10, "items": RANGE},
        "transitions": {"type": "array", "maxItems": 10, "items": TRANSITION},
        "durations": {"type": "array", "maxItems": 10, "items": DURATION},
        "magnitudes": {"type": "array", "maxItems": 8, "items": magnitude},
        "predictions": {"type": "array", "maxItems": 60, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["verb", "state", "observable", "expect", "value", "basis", "because", "precondition"],
            "properties": {"verb": {"type": "string"},
                           "state": {"type": "string", "enum": [*STATES, *moves]},
                           "observable": {"type": "string"}, "expect": {"type": "string", "enum": list(EXPECT)},
                           "value": {"type": ["number", "boolean", "null"]},
                           "basis": {"type": "string", "enum": list(BASIS)}, "because": {"type": "string"},
                           "precondition": {"type": ["string", "null"]}}}}}}



def _short(member: str) -> str:
    """`Assembly:Namespace.Type:Member(...)` as `Type.Member`."""
    parts = member.split(":")
    owner = parts[1].split(".")[-1] if len(parts) > 2 else ""
    name = parts[-1].split("(")[0]
    return f"{owner}.{name}" if owner else name


def _controls(action: Mapping[str, Any]) -> list[str]:
    paths = [b.get("path") for c in action.get("calibrations") or () for b in c.get("bindings") or ()]
    return sorted({p for p in paths if isinstance(p, str)})


MAX_ASSET_VALUES = 96


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text or text.startswith(("{", "[")):
        return None
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return text[:256]


def asset_values(text: str) -> dict[str, Any]:
    """The authored scalar fields of a text-serialized Unity asset: one level of nesting, Unity's own m_ fields left out."""
    values: dict[str, Any] = {}
    parent = None
    inside = False
    for raw in text.splitlines():
        if raw.startswith("MonoBehaviour:"):
            inside = True
            continue
        if not inside or not raw.strip() or raw.lstrip().startswith("- "):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            break
        key, _, value = raw.strip().partition(":")
        if indent == 2:
            parent = None
            if key.startswith("m_"):
                continue
            if value.strip():
                if (scalar := _scalar(value)) is not None:
                    values[key] = scalar
            else:
                parent = key
        elif indent == 4 and parent and value.strip() and (scalar := _scalar(value)) is not None:
            values[f"{parent}.{key}"] = scalar
    return values


def project_reader(project: Path, ref: str | None = None) -> Callable[[str], str | None]:
    """A project-relative path's text, from the working tree or, given a ref, from that commit (no checkout)."""
    def read(path: str) -> str | None:
        try:
            if ref is None:
                return (project / path).read_text(encoding="utf-8")
            done = subprocess.run(["git", "-C", str(project), "show", f"{ref}:{path}"], capture_output=True, check=True)
            return done.stdout.decode("utf-8")
        except (OSError, UnicodeDecodeError, subprocess.CalledProcessError):
            return None
    return read


def referenced_assets(discovery: Mapping[str, Any], entity_object_id: str,
                      read: Callable[[str], str | None]) -> list[dict[str, Any]]:
    """Authored values in the assets the entity's own fields reference (a config the controller reads its tuning
    from): discovery reads scene objects only, so a value kept in an asset is otherwise never declared."""
    data = discovery.get("data") or {}
    objects = {o["id"]: o for o in data.get("game_objects") or ()}
    included = {entity_object_id}
    while True:
        children = {oid for oid, obj in objects.items() if obj.get("parent_id") in included}
        if children <= included:
            break
        included |= children
    out, seen = [], set()
    for row in data.get("fields") or ():
        value = row.get("value")
        if row.get("object_id") not in included or not isinstance(value, Mapping):
            continue
        path = value.get("asset_path")
        if value.get("kind") != "asset_object" or not isinstance(path, str) or not path.endswith(".asset") or path in seen:
            continue
        seen.add(path)
        text = read(path)
        if text is None or not text.startswith("%YAML"):
            continue  # a binary-serialized asset: nothing to read without the Editor
        values = asset_values(text)
        if values:
            out.append({"asset": path, "type": str(value.get("type") or "").split(".")[-1],
                        "referenced_by": row.get("declaring_type", "").split(".")[-1] + "." + row.get("name", ""),
                        "values": dict(list(values.items())[:MAX_ASSET_VALUES])})
    return out


BUILT_IN = ("UnityEngine", "TMPro", "Unity.", "Cinemachine", "mscorlib", "System")
MAX_OBJECTS = 60


def authored_objects(discovery: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The scene objects that carry the game's own scripts, with their path and those scripts' names: what a
    situation can be near. An engine or package component alone does not make an object the game's."""
    data = discovery.get("data") or {}
    paths = {o["id"]: o.get("path") or o.get("name") for o in data.get("game_objects") or ()}
    scripts: dict[str, list[str]] = {}
    for component in data.get("components") or ():
        kind, assembly = component.get("type") or "", component.get("assembly") or ""
        if not kind or any(kind.startswith(b) or assembly.startswith(b) for b in BUILT_IN):
            continue
        scripts.setdefault(component.get("object_id"), []).append(kind.split(".")[-1])
    return [{"id": oid, "path": paths.get(oid), "scripts": sorted(set(names))}
            for oid, names in list(scripts.items())[:MAX_OBJECTS] if oid in paths]


def build_packet(manifest: Mapping[str, Any], discovery: Mapping[str, Any],
                 guards: Mapping[str, Any], read: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """What the game declares before play, for the verbs a guard table measures; no measurement is included."""
    actions = {a["name"]: a for a in manifest.get("actions") or ()}

    def described(name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        action = actions.get(name) or {}
        return {"name": name, "intent": action.get("intent"), "controls": _controls(action)}

    writers: dict[str, list[str]] = {}
    for row in (discovery.get("data") or {}).get("il_write_sets") or ():
        names = sorted({_short(w["method"]) for w in row.get("direct_writers") or () if not w.get("constructor")})
        if names:
            writers[row["field_id"]] = names
    readers = []
    for reader in manifest.get("readers") or ():
        descriptor = reader.get("descriptor") or {}
        declaration = descriptor.get("declaration_id") or ""
        entry = {"alias": reader["alias"], "kind": reader.get("kind"),
                 "field": (descriptor.get("declaring_type") or "").split(".")[-1] + "." + (descriptor.get("name") or "")}
        if writers.get(declaration):
            entry["written_by"] = writers[declaration][:MAX_WRITERS]
        readers.append(entry)
    def pressed(verb: str) -> dict[str, Any]:
        # How long the guard table presses it: a plan parameter, so a declared speed can be turned into a distance.
        row = (guards.get("verbs") or {}).get(verb) or {}
        held = [c.get("held_s") for c in (row.get("moving") or {}).values() if c and c.get("held_s")]
        return {"pressed_s": row.get("hold_s"), "pressed_with_move_s": held[0] if held else None}

    objects = authored_objects(discovery)
    packet = {"objects": objects,
              "verbs": [{**described(v), **pressed(v)} for v in (guards.get("verbs") or {})],
              "moves": [described(m) for m in guards.get("moves") or ()], "rise": described(guards.get("rise")),
              "readers": readers, "camera": list(CAMERA),
              "declared_configuration": declared_configuration(discovery, manifest["entity_id"])}
    others = object_configuration(discovery, [o["id"] for o in objects if o["id"] != manifest["entity_id"]],
                                  skip_types=BUILT_IN)
    if others is not None:
        packet["object_configuration"] = others   # what the objects a situation can be near declare
    if read is not None and (assets := referenced_assets(discovery, manifest["entity_id"], read)):
        packet["referenced_assets"] = assets
    return packet


MAGNITUDE_SYSTEM = (
    "You write down, before anything is measured, the sizes a game you have not seen declares for its body's "
    "motion, in world units and seconds, so code can check them against measurement. move_speed is a move's "
    "average speed over its press (state null). rise_height is how high the rise alone lifts the body (state "
    "null). verb_speed is the body's average speed over a verb's press while a move is held (state: the move's "
    "name). Each verb's and move's press length is given (pressed_s, pressed_with_move_s). For every declared "
    "value that sets one of these (a move speed, a sprint or dash speed, a jump speed or height with the gravity "
    "that brings it down), write the magnitude it implies and show the arithmetic in because. A declared speed "
    "applied for the whole press is its own magnitude; a burst shorter than the press averages with the move's "
    "speed over the rest of it. Whether a verb works while moving one way or the other is often a deliberate rule "
    "(a sprint only while moving forward): give verb_speed only for a move its declarations say it works with. "
    "Leave out a magnitude only when a factor you cannot see in the declarations "
    "sets it (an undeclared gravity or acceleration curve). basis is declared_value. At most eight."
)


ACTION_SYSTEM = (
    "You write down, before anything is measured, what pressing each input of a game you have not seen must make "
    "happen in play, so code can check it at every press in a recorded session and in a session that tries each one "
    "on purpose. An action rule: when the player presses an input (a verb, a move or the rise) and, if the rule "
    "needs one, a reader satisfies a condition just before the press, another reader must follow within within_s "
    "seconds: then names that reader by its alias, and then_expect says how it follows: changes, increases, "
    "decreases, or reaches then_value. Readings come about every 0.3 to "
    "0.8 s, so within_s is at least 1. Cover every verb whose effect a reader can show, and the rise: what each "
    "press starts (a projectile spawned, a flag set, a dialogue begun, a count raised) and, as a separate rule, "
    "what it ends when a declared duration says it ends. Readings are taken just before and just after each press "
    "and about every 0.8 s, so state only what a press leaves behind for at least a moment after it ends, never an "
    "effect that lasts only while the input is held (an input value, a stance, a zoom): the guard predictions "
    "measure those. Give a condition as a reader compared with a value "
    "(condition_observable, condition_op, condition_value) when the effect needs one a reader shows (a cooldown "
    "run out, standing on the ground, not dead). When no reader can express the condition (an NPC in range, an "
    "enemy near, standing on a pickup), describe it in situation, in a few words a player could bring about, and "
    "leave the reader condition null. When the situation is being near or at a scene object (an NPC to talk to, a "
    "pickup, a hazard, an enemy), name it: target_object is its id from objects (each listed with its path and the "
    "game's own scripts on it), and within is how near it the body must be, in world units, for the press to count. "
    "object_configuration lists the values authored on those objects (each with its object_id): when one declares "
    "how near the body must be (a range, a radius, a distance), within is that value, cited in because. "
    "The session measures that distance at every press and leads the player there. situation is null when the "
    "press alone must do it. Use only what the "
    "declarations say: an intent, a declared value (cite it in because), or the names of the action, the fields and "
    "the methods that write them. A timer or a cooldown counts through values and may count either way: never "
    "predict its direction, only that it changes. At most ten."
)


def actions_schema() -> dict[str, Any]:
    full = schema([])
    return {"type": "object", "additionalProperties": False, "required": ["action_rules"],
            "properties": {"action_rules": full["properties"]["action_rules"]}}


def magnitude_schema(moves: Sequence[str]) -> dict[str, Any]:
    full = schema(moves)
    return {"type": "object", "additionalProperties": False, "required": ["magnitudes"],
            "properties": {"magnitudes": full["properties"]["magnitudes"]}}


def request_body(packet: Mapping[str, Any], part: str = "all") -> dict[str, Any]:
    moves = [m["name"] for m in packet.get("moves") or () if m]
    if part == "actions":
        return {"model": MODEL, "reasoning_effort": "low", "max_tokens": 8000,
                "messages": [{"role": "system", "content": ACTION_SYSTEM},
                             {"role": "user", "content": json.dumps(packet, separators=(",", ":"))}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "action_rules", "strict": True, "schema": actions_schema()}}}
    if part == "magnitudes":
        return {"model": MODEL, "reasoning_effort": "low", "max_tokens": 4000,
                "messages": [{"role": "system", "content": MAGNITUDE_SYSTEM},
                             {"role": "user", "content": json.dumps(packet, separators=(",", ":"))}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "magnitudes", "strict": True, "schema": magnitude_schema(moves)}}}
    return _full_request(packet)


def _full_request(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {"model": MODEL, "reasoning_effort": "low", "max_tokens": 8000,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps(packet, separators=(",", ":"))}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "expectations", "strict": True,
                "schema": schema([m["name"] for m in packet.get("moves") or () if m])}}}


def screened(predictions: Sequence[Mapping[str, Any]], packet: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    """Predictions code can score, and the rest with the reason each cannot be."""
    verbs = {v["name"] for v in packet.get("verbs") or ()}
    rise = (packet.get("rise") or {}).get("name")
    observables = {r["alias"] for r in packet.get("readers") or ()} | set(CAMERA) | {BODY}
    kept, set_aside = [], []
    for item in predictions:
        reason = None
        if rise is not None and item.get("verb") == rise:
            if (item.get("state"), item.get("observable"), item.get("expect")) != ("air", BODY, "moves_body"):
                reason = "the rise is measured only pressed again in the air, by how high the body goes"
        elif item.get("verb") not in verbs:
            reason = "not a measured verb"
        elif item.get("observable") not in observables:
            reason = "not a measured observable"
        elif (item.get("expect") == "moves_body") != (item.get("observable") == BODY):
            reason = "moves_body goes with body_motion only"
        elif (item.get("expect") == "reaches") != (item.get("value") is not None):
            reason = "a value goes with reaches only"
        elif item.get("precondition"):
            reason = "needs a condition the guard states do not give"
        (set_aside if reason else kept).append({**item, **({"set_aside": reason} if reason else {})})
    return kept, set_aside


def screened_play(answer: Mapping[str, Any], packet: Mapping[str, Any]) -> tuple[dict, list[dict]]:
    """Ranges and transitions code can score over play, and the rest with the reason each cannot be."""
    kinds = {r["alias"]: r.get("kind") for r in packet.get("readers") or ()}
    kept: dict[str, list] = {"ranges": [], "transitions": [], "durations": [], "magnitudes": [],
                             "action_rules": [], "situations": []}
    set_aside = []
    for item in answer.get("ranges") or ():
        reason = ("not a reader" if item.get("observable") not in kinds
                  else "not a number reader" if kinds[item["observable"]] != "number"
                  else "no bound" if item.get("min") is None and item.get("max") is None else None)
        (set_aside if reason else kept["ranges"]).append({**item, **({"set_aside": reason} if reason else {})})
    for item in answer.get("transitions") or ():
        reason = None
        if item.get("when") not in kinds or item.get("then") not in kinds:
            reason = "not a reader"
        elif (item.get("when_change") == "becomes") != (item.get("when_value") is not None):
            reason = "a when_value goes with becomes only"
        elif (item.get("then_expect") == "reaches") != (item.get("then_value") is not None):
            reason = "a then_value goes with reaches only"
        elif not isinstance(item.get("within_s"), (int, float)) or not 0 < item["within_s"] <= 30:
            reason = "within_s must be a positive number of seconds, at most 30"
        (set_aside if reason else kept["transitions"]).append({**item, **({"set_aside": reason} if reason else {})})
    for item in answer.get("durations") or ():
        reason = ("not a reader" if item.get("observable") not in kinds
                  else "seconds must be positive, at most 30"
                  if not isinstance(item.get("seconds"), (int, float)) or not 0 < item["seconds"] <= 30 else None)
        (set_aside if reason else kept["durations"]).append({**item, **({"set_aside": reason} if reason else {})})
    verbs = {v["name"] for v in packet.get("verbs") or () if v}
    moves = {m["name"] for m in packet.get("moves") or () if m}
    rise = (packet.get("rise") or {}).get("name")
    for item in answer.get("magnitudes") or ():
        quantity, action, state = item.get("quantity"), item.get("action"), item.get("state")
        reason = None
        if not isinstance(item.get("value"), (int, float)) or item["value"] <= 0:
            reason = "a magnitude must be positive"
        elif quantity == "move_speed" and (action not in moves or state is not None):
            reason = "move_speed names a measured move, with no state"
        elif quantity == "rise_height" and (action != rise or state is not None):
            reason = "rise_height names the measured rise, with no state"
        elif quantity == "verb_speed" and (action not in verbs or state not in moves):
            reason = "verb_speed names a measured verb and a measured move"
        (set_aside if reason else kept["magnitudes"]).append({**item, **({"set_aside": reason} if reason else {})})
    actions = verbs | moves | ({rise} if rise else set())
    for item in answer.get("action_rules") or ():
        condition = [item.get(k) for k in ("condition_observable", "condition_op", "condition_value")]
        reason = None
        if item.get("action") not in actions:
            reason = "not a measured input"
        elif item.get("then") not in kinds:
            reason = "not a reader"
        elif any(v is not None for v in condition) and (condition[0] not in kinds or condition[1] is None
                                                         or condition[2] is None):
            reason = "a condition is a reader, a comparator and a value"
        elif (item.get("then_expect") == "reaches") != (item.get("then_value") is not None):
            reason = "a then_value goes with reaches only"
        elif not isinstance(item.get("within_s"), (int, float)) or not 0 < item["within_s"] <= 30:
            reason = "within_s must be a positive number of seconds, at most 30"
        objects = {o["id"] for o in packet.get("objects") or ()}
        if reason is None and item.get("target_object") is not None:
            if not item.get("situation"):
                reason = "a target_object goes with a situation"
            elif item["target_object"] not in objects:
                reason = "not a listed scene object"
            elif not isinstance(item.get("within"), (int, float)) or not 0 < item["within"] <= 20:
                reason = "within must be a positive distance, at most 20"
        if reason:
            set_aside.append({**item, "set_aside": reason})
        elif item.get("situation"):
            if item.get("target_object") is not None:
                path = next((o.get("path") for o in packet.get("objects") or () if o["id"] == item["target_object"]), None)
                item = {**item, "target_path": path,
                        "region_id": f"situation_{sum(1 for k in kept['situations'] if k.get('target_object')) + 1}"}
            kept["situations"].append(item)   # a condition no reader shows: tested in a directed session
        else:
            kept["action_rules"].append(item)
    return kept, set_aside


def play_readings(trace_rows: Sequence[Mapping[str, Any]]) -> list[tuple[float, int, dict[str, Any]]]:
    """Every observation's public readers, by wall time: (epoch seconds, trace_seq, values); with the press-anchored
    readings of effect records and the readings a press test took."""
    from playtest.runner.review import _epoch

    out = []
    for row in trace_rows:
        body = row.get("body") or {}
        if row.get("kind") == "observation":
            at = _epoch(body.get("wall_utc"))
            if at is not None:
                out.append((at, row.get("trace_seq"), dict(body.get("public") or {})))
        elif row.get("kind") == "effect":
            # A press-anchored reading (`--press-readings`): every reader just before and just after one input.
            observed = body.get("observed") or {}
            for key in ("readers_before", "readers_after"):
                reading = observed.get(key)
                if isinstance(reading, Mapping) and isinstance(reading.get("wall_s"), (int, float)):
                    out.append((float(reading["wall_s"]), row.get("trace_seq"), dict(reading.get("values") or {})))
        elif row.get("kind") == "option":
            # A press test reads the readers itself while S1 waits for it: around each press and through each wait.
            for reading in ((body.get("outcome") or {}).get("measured") or {}).get("readings") or ():
                if (isinstance(reading, Mapping) and isinstance(reading.get("wall_s"), (int, float))
                        and reading.get("values")):
                    out.append((float(reading["wall_s"]), row.get("trace_seq"), dict(reading["values"])))
    out.sort(key=lambda item: item[0])
    return out


def _close(a: Any, b: Any) -> bool:
    if isinstance(b, bool) or isinstance(a, bool) or not isinstance(b, (int, float)):
        return a == b
    number = _number(a)
    return number is not None and abs(number - b) <= TOLERANCE * max(abs(b), 1.0)


def score_ranges(ranges: Sequence[Mapping[str, Any]],
                 sessions: Sequence[Sequence[tuple[float, int, dict]]]) -> list[dict[str, Any]]:
    """Each range over every session's readings: the sessions where a reading left it, with the first such one."""
    out = []
    for item in ranges:
        low, high, name = item.get("min"), item.get("max"), item["observable"]
        slack = lambda bound: 1e-3 + 0.01 * abs(bound)
        violated, read, first = 0, 0, None
        for readings in sessions:
            values = [(seq, _number(values.get(name))) for _, seq, values in readings]
            values = [(seq, v) for seq, v in values if v is not None]
            if not values:
                continue
            read += 1
            outside = [(seq, v) for seq, v in values
                       if (low is not None and v < low - slack(low)) or (high is not None and v > high + slack(high))]
            if outside:
                violated += 1
                first = first or outside[0]
        bound = " and ".join(part for part in (f"at least {low:g}" if low is not None else "",
                                               f"at most {high:g}" if high is not None else "") if part)
        verdict = "unmeasured" if not read else "mismatch" if violated else "held"
        out.append({**item, "k": violated, "n": read, "verdict": verdict, "first_violation": first,
                    "line": f"{name} should stay {bound} ({item['basis']}: {item['because']}); "
                            f"it left that range in {violated} of {read} play sessions"
                            + (f" (first: {first[1]:g} at trace seq {first[0]})" if first else "")})
    return out


# How far a measured magnitude may sit from its declared value: sampling only lowers a peak (a reading rarely lands
# on the apex), and a press's average speed includes its start. Set from the clean builds' guard tables (20 FPSKit,
# 42 DragonBlaze): a move's speed stayed within -3.1%/+8.7% of its declared value in all 76 readings; a verb's speed
# left ±15% in 6 of 186, never in more than 2 of one magnitude's 24. A size is a mismatch only when most tables
# measure it outside its band.
BANDS = {"move_speed": (0.10, 0.10), "verb_speed": (0.15, 0.15), "rise_height": (0.25, 0.1)}


def _median(values: Sequence[float]) -> float | None:
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def measured_magnitude(item: Mapping[str, Any], table: Mapping[str, Any]) -> float | None:
    """The quantity as one guard table measured it, or None when the table cannot show it."""
    verbs = table.get("verbs") or {}
    quantity, action = item["quantity"], item["action"]
    if quantity == "move_speed":
        speeds = [c["baseline_displacement"] / c["held_s"] for row in verbs.values()
                  for move, c in (row.get("moving") or {}).items()
                  if move == action and c and isinstance(c.get("held_s"), (int, float)) and c["held_s"] > 0
                  and isinstance(c.get("baseline_displacement"), (int, float))]
        return _median(speeds)
    if quantity == "rise_height":
        peaks = [row["air"]["baseline_peak_displacement"] for row in verbs.values()
                 if row.get("air") and isinstance(row["air"].get("baseline_peak_displacement"), (int, float))]
        return _median(peaks)
    cell = ((verbs.get(action) or {}).get("moving") or {}).get(item.get("state"))
    if cell and isinstance(cell.get("held_s"), (int, float)) and cell["held_s"] > 0 \
            and isinstance(cell.get("displacement"), (int, float)):
        return cell["displacement"] / cell["held_s"]
    return None


def score_magnitudes(magnitudes: Sequence[Mapping[str, Any]], tables: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Each declared magnitude against every fresh session's guard table: k of n outside its band."""
    out = []
    for item in magnitudes:
        low, high = BANDS[item["quantity"]]
        measured = [m for m in (measured_magnitude(item, t) for t in tables) if m is not None]
        off = [m for m in measured if not item["value"] * (1 - low) <= m <= item["value"] * (1 + high)]
        verdict = ("unmeasured" if not measured else "mismatch" if 2 * len(off) > len(measured)
                   else "held" if not off else "mixed")
        what = item["quantity"].replace("_", " ") + f" of {item['action']}" + (
            f" while {item['state']} held" if item.get("state") else "")
        shown = ", ".join(f"{m:.2f}" for m in measured)
        out.append({**item, "k": len(off), "n": len(measured), "measured": [round(m, 3) for m in measured],
                    "verdict": verdict,
                    "line": f"{what}: expected {item['value']:g} ({item['basis']}: {item['because']}); measured "
                            f"{shown or 'nothing'}, outside -{low:.0%}/+{high:.0%} in {len(off)} of {len(measured)} "
                            f"fresh sessions"})
    return out


def _usual_gap(readings: Sequence[tuple[float, int, dict]]) -> float:
    gaps = sorted(b[0] - a[0] for a, b in zip(readings, readings[1:]))
    return gaps[len(gaps) // 2] if gaps else 0.0


def duration_events(item: Mapping[str, Any], sessions: Sequence[Sequence[tuple[float, int, dict]]]) -> list[dict]:
    """Every episode of a duration's value seen whole in play, with whether its length fits the declared one.

    Readings are about 0.8 s apart, so an episode's true length lies between its first and last reading with the
    value (lower bound) and the readings just outside it (upper bound). A declared length outside that interval,
    with a tenth of its own length as slack, does not fit. An episode at a session's edge, or with an unread
    stretch beside it, is not counted.
    """
    events = []
    name, value, seconds = item["observable"], item["value"], item["seconds"]
    for index, readings in enumerate(sessions):
        usual = _usual_gap(readings)
        on = [name in values and _close(values[name], value) for _, _, values in readings]
        i = 1
        while i < len(readings) - 1:
            if not on[i] or on[i - 1]:
                i += 1
                continue
            j = i
            while j + 1 < len(readings) and on[j + 1]:
                j += 1
            if j + 1 >= len(readings):
                break  # still on when the session ended
            times = [readings[k][0] for k in range(i - 1, j + 2)]
            if not (usual and any(b - a > UNOBSERVED * usual for a, b in zip(times, times[1:]))):
                lower, upper = readings[j][0] - readings[i][0], readings[j + 1][0] - readings[i - 1][0]
                slack = 0.1 * seconds + 0.05
                events.append({"session": index, "seq": readings[i][1], "at": readings[i][0],
                               "window": (readings[i - 1][0], readings[j + 1][0]),
                               "before": readings[i - 1][2], "trigger": readings[i][2],
                               "lasted": (round(lower, 2), round(upper, 2)),
                               "followed": lower - slack <= seconds <= upper + slack})
            i = j + 1
    return events


def score_durations(durations: Sequence[Mapping[str, Any]],
                    sessions: Sequence[Sequence[tuple[float, int, dict]]]) -> list[dict[str, Any]]:
    """Each declared duration against every episode of its value seen whole in play (`duration_events`)."""
    out = []
    for item in durations:
        events = duration_events(item, sessions)
        failed = [e for e in events if not e["followed"]]
        verdict = ("unmeasured" if len(events) < 2 else "mismatch" if 2 * len(failed) > len(events)
                   else "held" if not failed else "mixed")
        examples = [(e["seq"], *e["lasted"]) for e in failed]
        out.append({**item, "k": len(failed), "n": len(events), "verdict": verdict, "examples": examples[:5],
                    "line": f"{item['observable']} at {json.dumps(item['value'])} should last {item['seconds']:g} s "
                            f"({item['basis']}: {item['because']}); it did not in {len(failed)} of {len(events)} "
                            f"episodes seen whole in play"
                            + ("" if not examples else " (lasted between " + "; ".join(
                                f"{lo:g} and {hi:g} s at trace seq {seq}" for seq, lo, hi in examples[:3]) + ")")})
    return out


def play_presses(trace_rows: Sequence[Mapping[str, Any]]) -> list[tuple[float, float, str]]:
    """Every action the game received, plain or inside an option, by start: (start, end, action).

    An option's receipts name each action it sent and when it started; a rejected one never reached the game.
    """
    from playtest.runner.review import _epoch

    out = []
    for row in trace_rows:
        body = row.get("body") or {}
        if row.get("kind") == "execution":
            records = body.get("records") or []
            terminal = records[-1] if records else {}
            start = _epoch(terminal.get("input_start_wall_s"))
            held = terminal.get("applied_duration_s")
            action = (body.get("request") or {}).get("action")
            if terminal.get("status") in ("completed", "cancelled") and start is not None and action:
                out.append((start, start + (held if isinstance(held, (int, float)) else 0.0), action))
        elif row.get("kind") == "option":
            names = {r.get("action_id"): r.get("action") for r in body.get("receipts") or ()
                     if isinstance(r, Mapping) and r.get("type") == "request"}
            for r in body.get("receipts") or ():
                if not isinstance(r, Mapping) or r.get("type") != "execution" or r.get("status") == "rejected":
                    continue
                start = _epoch(r.get("input_start_wall_s"))
                held = r.get("applied_duration_s")
                action = names.get(r.get("action_id"))
                if start is not None and action and r.get("status") != "running":
                    out.append((start, start + (held if isinstance(held, (int, float)) else 0.0), action))
    return sorted(set(out))


def catalog_controls(catalog: Mapping[str, Any]) -> dict[str, frozenset]:
    """Each served action's controls, with the value each is driven to: an action presses a verb when its controls
    include all of the verb's. One stick pushed up and the same stick pushed down are different inputs."""
    return {name: frozenset((i.get("device"), i.get("control"), i.get("x"), i.get("y")) for i in (d.get("inputs") or ()))
            for name, d in (catalog.get("actions") or {}).items()}


def _condition_holds(item: Mapping[str, Any], values: Mapping[str, Any]) -> bool | None:
    name = item.get("condition_observable")
    if name is None:
        return True
    if name not in values:
        return None
    return _holds({"before": values}, {"observable": name, "at": "before", "op": item["condition_op"],
                                       "value": item["condition_value"]})


def action_events(item: Mapping[str, Any], sessions: Sequence[Sequence[tuple[float, int, dict]]],
                  presses: Sequence[Sequence[tuple[float, float, str]]], controls: Sequence[Mapping[str, frozenset]],
                  skipped: dict[str, int] | None = None,
                  limits: list[tuple[float, int]] | None = None) -> list[dict[str, Any]]:
    """Every press of the rule's input (alone or inside a chord or option that includes its controls) whose
    consequence could be read, with whether it followed in time.

    The consequence counts from the reading just before the press up to the first reading at or after within_s past
    the press's end, so a reading's lag cannot fail it. A press is not counted when its condition did not hold or
    could not be read just before it, when the next press of the same input lands inside its window, when the session ended
    inside its window, when an unread stretch lies inside it, or when its reader was not read just before it or
    anywhere in its window.
    """
    skipped = skipped if skipped is not None else {}
    for key in ("condition_not_met", "overlapped", "window_cut", "unobserved_gap", "at_limit", "unread"):
        skipped.setdefault(key, 0)
    events = []
    for index, (readings, pressed, served) in enumerate(zip(sessions, presses, controls)):
        own = served.get(item["action"])
        if not own:
            continue
        usual = _usual_gap(readings)
        times = [r[0] for r in readings]
        mine = [(start, end, action) for start, end, action in pressed if own <= served.get(action, frozenset())]
        for number, (start, end, action) in enumerate(mine):
            if number + 1 < len(mine) and mine[number + 1][0] <= end + item["within_s"]:
                # The next press of the same input lands inside this one's window: what follows is not this one's.
                skipped["overlapped"] += 1
                continue
            before_index = max((i for i, t in enumerate(times) if t <= start), default=None)
            if before_index is None:
                skipped["window_cut"] += 1
                continue
            before = readings[before_index][2]
            condition = _condition_holds(item, before)
            if not condition:
                skipped["condition_not_met"] += 1
                continue
            window, stamps = [], [readings[before_index][0]]
            for at, _, values in readings[before_index + 1:]:
                window.append(values.get(item["then"]))
                stamps.append(at)
                if at >= end + item["within_s"]:
                    break
            else:
                skipped["window_cut"] += 1
                continue
            if usual and any(b - a > UNOBSERVED * usual for a, b in zip(stamps, stamps[1:])):
                skipped["unobserved_gap"] += 1
                continue
            if before.get(item["then"]) is None or all(v is None for v in window):
                # Its reader was not read before the press or in its window: unknown, never a failure.
                skipped["unread"] += 1
                continue
            events.append({"session": index, "seq": readings[before_index][1], "at": start, "action": action,
                           "before": before, "trigger": readings[before_index + 1][2] if window else before,
                           "followed": _followed(item, before.get(item["then"]), window), "window": window})
        _drop_at_limit(item, readings, events, index, skipped, limits)
    for event in events:
        event.pop("window", None)
    return events


def _drop_at_limit(item: Mapping[str, Any], readings: Sequence[tuple[float, int, dict]], events: list[dict[str, Any]],
                   session: int, skipped: dict[str, int], limits: list[tuple[float, int]] | None = None) -> None:
    """Leave out this session's failed presses that began with the reader already where the input drives it.

    The input's direction is read from its own presses that were followed (a look down raised the pitch). A failed
    press whose reader began at the furthest value the session ever read in that direction (a view already at its
    clamp) cannot show the press. With no followed press there is no direction, and nothing is left out: an input
    that never moves its reader stays failed.
    """
    mine = [e for e in events if e["session"] == session]
    steps = []
    for e in mine:
        start = _number(e["before"].get(item["then"]))
        values = [v for v in (_number(x) for x in e.get("window") or ()) if v is not None]
        if e["followed"] and start is not None and values:
            steps.append(max(values, key=lambda v: abs(v - start)) - start)
    total = sum(steps)
    direction = (total > 1e-3) - (total < -1e-3)
    if not direction:
        return
    seen = [v for _, _, values in readings if (v := _number(values.get(item["then"]))) is not None]
    if not seen:
        return
    end = max(seen) if direction > 0 else min(seen)
    for e in mine:
        start = _number(e["before"].get(item["then"]))
        if not e["followed"] and start is not None and (start - end) * direction >= -1e-3:
            events.remove(e)
            skipped["at_limit"] = skipped.get("at_limit", 0) + 1
            if limits is not None and (end, direction) not in limits:
                limits.append((end, direction))


def guard_evidence(verb: str, key: str, tables: Sequence[Mapping[str, Any]]) -> str | None:
    """What the guard tables measured of `verb` changing `key`: "lasts" (still changed just after a press ended, in
    some cell), "while_held" (changed only while held), "sometimes" (in some trials of a cell and not in others, and
    in every trial of none: a fresh, controlled press shows it only some of the time), "never" (in no cell of any
    table), or None (no table measured the verb). The tables say what a reading can show, never whether a rule
    held."""
    rows = [(t.get("verbs") or {}).get(verb) for t in tables]
    rows = [r for r in rows if r]
    if not rows:
        return None
    changed = persisted = every_trial = False
    for row in rows:
        for cell in (row.get("rest"), *(row.get("moving") or {}).values(), row.get("air")):
            if not cell:
                continue
            trials = [keys or () for keys in cell.get("trial_changed") or ()]
            if key in (cell.get("changed") or {}) or any(key in keys for keys in trials):
                changed = True
                every_trial = every_trial or not trials or all(key in keys for keys in trials)
            if any(key in keys for keys in cell.get("trial_persisted") or ()):
                persisted = True
    if not changed:
        return "never"
    if not persisted and any("trial_persisted" in (c or {}) for row in rows
                             for c in (row.get("rest"), *(row.get("moving") or {}).values(), row.get("air"))):
        return "while_held"
    return "lasts" if every_trial else "sometimes"


def score_action_rules(rules: Sequence[Mapping[str, Any]], sessions: Sequence[Sequence[tuple[float, int, dict]]],
                       presses: Sequence[Sequence[tuple[float, float, str]]],
                       controls: Sequence[Mapping[str, frozenset]],
                       guards: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """Each action rule at every counted press (`action_events`): k of n not followed in time.

    With guard tables of the same build, a rule that failed is read against what they measured of its input
    changing its reader: changed only while held, a press-anchored reading cannot show it (while_held); changed in
    only some trials of a fresh press and in every trial of none, a failed press is what they measured too
    (intermittent); changed in some measured state but not in these presses, it is a guard (guarded); never changed
    anywhere, it stays a mismatch, the strongest evidence.
    """
    out = []
    for item in rules:
        skipped: dict[str, int] = {}
        limits: list[tuple[float, int]] = []
        events = action_events(item, sessions, presses, controls, skipped, limits)
        failed = [e for e in events if not e["followed"]]
        limit = (" or ".join(f"{value:g}" for value, _ in limits) + ", the furthest value read in the direction this "
                 "input moves it (" + "; ".join(sorted({"increasing" if d > 0 else "decreasing" for _, d in limits}))
                 + ")") if limits else ""
        verdict = ("unmeasured" if len(events) < 2 else "mismatch" if 2 * len(failed) > len(events)
                   else "held" if not failed else "mixed")
        evidence = guard_evidence(item["action"], item["then"], guards) if guards else None
        if verdict in ("mismatch", "mixed") and evidence == "while_held":
            verdict = "while_held"
        elif verdict in ("mismatch", "mixed") and evidence == "sometimes":
            verdict = "intermittent"
        elif verdict == "mismatch" and evidence == "lasts":
            verdict = "guarded"
        condition = ("" if item.get("condition_observable") is None else
                     f" with {item['condition_observable']} {item['condition_op']} {json.dumps(item['condition_value'])}")
        then = (f"{item['then']} reaches {json.dumps(item['then_value'])}" if item["then_expect"] == "reaches"
                else f"{item['then']} {item['then_expect']}")
        examples = [e["seq"] for e in failed]
        out.append({**item, "k": len(failed), "n": len(events), "verdict": verdict, "examples": examples[:5],
                    "not_counted": skipped, "guard_evidence": evidence,
                    "line": f"pressing {item['action']}{condition}: {then} within {item['within_s']:g} s "
                            f"({item['basis']}: {item['because']}); not in {len(failed)} of {len(events)} presses "
                            f"in play" + (f" (at trace seq {', '.join(map(str, examples[:5]))})" if examples else "")
                            + (f"; {skipped['at_limit']} presses began with {item['then']} already at {limit}, and "
                               f"were not counted" if skipped.get("at_limit") else "")})
    return out


def _triggered(item: Mapping[str, Any], before: Any, after: Any) -> bool:
    change = item["when_change"]
    if change == "becomes":
        return not _close(before, item["when_value"]) and _close(after, item["when_value"])
    a, b = _number(before), _number(after)
    if a is None or b is None:
        return False
    return b > a + 1e-3 if change == "increases" else b < a - 1e-3


def _followed(item: Mapping[str, Any], start: Any, later: Sequence[Any]) -> bool:
    expect = item["then_expect"]
    for value in later:
        if expect == "reaches":
            if _close(value, item["then_value"]):
                return True
        elif expect == "changes":
            if value != start and not _close(value, start):
                return True
        else:
            a, b = _number(start), _number(value)
            if a is not None and b is not None and ((b > a + 1e-3) if expect == "increases" else (b < a - 1e-3)):
                return True
    return False


def transition_events(item: Mapping[str, Any], sessions: Sequence[Sequence[tuple[float, int, dict]]],
                      skipped: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Every trigger of a transition that could show its consequence, with whether it was followed in time.

    The consequence counts from the reading before the trigger (it may land in the same frame) up to the first
    reading at or after within_s past the trigger's reading, so a reading's lag cannot fail it. A trigger is not
    counted when nothing could show its consequence: the session ended inside the window; the window has a gap of
    more than UNOBSERVED times the session's usual reading interval (a reload or a respawn the readings did not see),
    against ordinary slow turns, which stay within three; or it came inside the window of the trigger before it,
    whose consequence may still hold (a second hit while the first one's invulnerability lasts).
    """
    skipped = skipped if skipped is not None else {}
    for key in ("window_cut", "unobserved_gap", "overlapped"):
        skipped.setdefault(key, 0)
    events = []
    for index, readings in enumerate(sessions):
        usual = _usual_gap(readings)
        open_until = None
        for i in range(1, len(readings)):
            (_, _, a), (at, seq, b) = readings[i - 1], readings[i]
            if item["when"] not in a or item["when"] not in b or not _triggered(item, a[item["when"]], b[item["when"]]):
                continue
            overlapped = open_until is not None and at <= open_until
            open_until = at + item["within_s"]
            if overlapped:
                skipped["overlapped"] += 1
                continue
            window, times = [], [at]
            for later_at, _, values in readings[i:]:
                window.append(values.get(item["then"]))
                times.append(later_at)
                if later_at >= at + item["within_s"]:
                    break
            else:
                skipped["window_cut"] += 1  # the session ended before the window did: not a measurement
                continue
            if usual and any(t2 - t1 > UNOBSERVED * usual for t1, t2 in zip(times, times[1:])):
                skipped["unobserved_gap"] += 1
                continue
            events.append({"session": index, "seq": seq, "at": at, "before": a, "trigger": b,
                           "followed": _followed(item, a.get(item["then"]), window)})
    return events


def score_transitions(transitions: Sequence[Mapping[str, Any]],
                      sessions: Sequence[Sequence[tuple[float, int, dict]]]) -> list[dict[str, Any]]:
    """Each transition at every counted trigger (`transition_events`): k of n not followed in time."""
    out = []
    for item in transitions:
        skipped: dict[str, int] = {}
        events = transition_events(item, sessions, skipped)
        failed = [e for e in events if not e["followed"]]
        examples = [e["seq"] for e in failed]
        verdict = ("unmeasured" if len(events) < 2 else "mismatch" if 2 * len(failed) > len(events)
                   else "held" if not failed else "mixed")
        when = (f"{item['when']} becomes {json.dumps(item['when_value'])}" if item["when_change"] == "becomes"
                else f"{item['when']} {item['when_change']}")
        then = (f"{item['then']} reaches {json.dumps(item['then_value'])}" if item["then_expect"] == "reaches"
                else f"{item['then']} {item['then_expect']}")
        out.append({**item, "k": len(failed), "n": len(events), "verdict": verdict, "examples": examples[:5],
                    "not_counted": skipped,
                    "line": f"when {when}, {then} within {item['within_s']:g} s ({item['basis']}: {item['because']}); "
                            f"not in {len(failed)} of {len(events)} times it happened in play"
                            + (f" (at trace seq {', '.join(map(str, examples[:5]))})" if examples else "")})
    return out


# --- investigating a rule that failed sometimes -------------------------------------------------------------------

INVESTIGATE_SYSTEM = (
    "A rule the game's declarations state was checked by code over recorded play. For a transition, each event is "
    "a time its trigger was seen, and followed says whether its consequence came in time. For a duration, each "
    "event is an episode of the reader's value, and followed says whether its length fits the declared one "
    "(lasted gives the shortest and longest it can have been). It held at most events and failed at some. Each "
    "event carries every reader's reading just before it began (before) and as it began (trigger), the last input "
    "the player gave before it and how long before that input ended, for a duration the inputs given while it "
    "lasted (inputs_during, by name, with how many times), and the seconds since the session began. Say what "
    "condition, true at the failures and false at the events that were followed, would explain the failures: a "
    "reader compared with a value, before or at the start; the last input (observable last_action, op == or !=, "
    "value the input's name); or an input given during it (observable inputs_during, op contains, value the "
    "input's name). A failure the rule's own cause repeating would explain (the input that sets a flag given again "
    "while it is on) is worth naming. Give up to four, the most explanatory first. Code will test each against "
    "every event; one that does not separate them will be reported as such, so do not stretch. If nothing in the "
    "events separates them, give none."
)
HYPOTHESES = {"type": "object", "additionalProperties": False, "required": ["hypotheses"], "properties": {
    "hypotheses": {"type": "array", "maxItems": 4, "items": {
        "type": "object", "additionalProperties": False, "required": ["observable", "at", "op", "value", "explains"],
        "properties": {"observable": {"type": "string"}, "at": {"type": "string", "enum": ["before", "trigger"]},
                       "op": {"type": "string", "enum": ["==", "!=", "<", "<=", ">", ">=", "contains"]},
                       "value": {"type": ["number", "boolean", "string"]}, "explains": {"type": "string"}}}}}}
NOISE = ("camera", "controlled", "probe")
INVESTIGATE_MAX_TOKENS = 16000  # 4000 ran out inside the reasoning on 40 duration episodes
MAX_EVENTS = 40


def play_inputs(trace_rows: Sequence[Mapping[str, Any]]) -> list[tuple[float, float | None, str | None]]:
    """Every input the game received, by start: (start, end, choice)."""
    from playtest.runner.review import _inputs

    inputs, _ = _inputs(list(trace_rows))
    return [(item["start"], item["end"], item["choice"]) for item in inputs]


def _flat_readers(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if k not in NOISE and not isinstance(v, (dict, list))}


def describe_events(events: Sequence[Mapping[str, Any]], inputs: Sequence[Sequence[tuple]],
                    starts: Sequence[float]) -> list[dict[str, Any]]:
    """The events as the investigation reads them: readers, the last input before each, time into the session."""
    out = []
    for event in events:
        last = None
        for start, end, choice in inputs[event["session"]]:
            if start < event["at"]:
                last = (choice, None if end is None else round(event["at"] - end, 2))
        described = {"event": len(out), "followed": event["followed"], "before": _flat_readers(event["before"]),
                     "trigger": _flat_readers(event["trigger"]),
                     "last_action": last[0] if last else None,
                     "last_action_ended_s_before": last[1] if last else None,
                     "session_s": round(event["at"] - starts[event["session"]], 1)}
        if "window" in event:
            # A duration's episode: what was pressed while it lasted, by name, with how many times.
            begin, end = event["window"]
            during: dict[str, int] = {}
            for start, _, choice in inputs[event["session"]]:
                if begin <= start <= end and choice is not None:
                    during[choice] = during.get(choice, 0) + 1
            described["inputs_during"] = during
            described["lasted"] = list(event["lasted"])
        out.append(described)
    return out


def _holds(event: Mapping[str, Any], hypothesis: Mapping[str, Any]) -> bool | None:
    if hypothesis["observable"] == "inputs_during":
        if "inputs_during" not in event or hypothesis["op"] not in ("contains", "==", "!="):
            return None
        present = hypothesis["value"] in event["inputs_during"]
        return not present if hypothesis["op"] == "!=" else present
    if hypothesis["op"] == "contains":
        return None
    if hypothesis["observable"] == "last_action":
        value = event.get("last_action")
    else:
        source = event.get(hypothesis["at"]) or {}
        if hypothesis["observable"] not in source:
            return None
        value = source[hypothesis["observable"]]
    target, op = hypothesis["value"], hypothesis["op"]
    if op in ("==", "!="):
        same = _close(value, target) if isinstance(target, (int, float)) and not isinstance(target, bool) else value == target
        return same if op == "==" else not same
    a, b = _number(value), _number(target)
    if a is None or b is None:
        return None
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def _fisher_greater(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact p that failures are this concentrated where the condition holds.

    a: failed and held, b: followed and held, c: failed and not held, d: followed and not held.
    """
    from math import comb

    row, col, n = a + b, a + c, a + b + c + d
    total = comb(n, col)
    return sum(comb(row, x) * comb(n - row, col - x) for x in range(a, min(row, col) + 1)) / total if total else 1.0


def check_hypotheses(hypotheses: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Each proposed condition against every event: a 2x2 count and how surely it separates the failures.

    The condition was proposed after seeing these same events, so a separation here is a lead for a directed test,
    not a finding: `supported` needs at least three failures, all of them under the condition, and p < 0.05.
    """
    out = []
    for hypothesis in hypotheses:
        counts = {"failed_held": 0, "followed_held": 0, "failed_not": 0, "followed_not": 0, "unknown": 0}
        for event in events:
            held = _holds(event, hypothesis)
            if held is None:
                counts["unknown"] += 1
                continue
            counts[("failed" if not event["followed"] else "followed") + ("_held" if held else "_not")] += 1
        p = _fisher_greater(counts["failed_held"], counts["followed_held"], counts["failed_not"], counts["followed_not"])
        failures = counts["failed_held"] + counts["failed_not"]
        supported = failures >= 3 and counts["failed_not"] == 0 and p < 0.05
        verdict = "supported" if supported else "separates" if p < 0.05 else "does not separate"
        condition = f"{hypothesis['observable']} {hypothesis['op']} {json.dumps(hypothesis['value'])}" + (
            "" if hypothesis["observable"] in ("last_action", "inputs_during")
            else " (just before it began)" if hypothesis["at"] == "before" else " (as it began)")
        out.append({**hypothesis, **counts, "p": round(p, 4), "verdict": verdict,
                    "line": f"{condition}: {verdict}; failed {counts['failed_held']} of "
                            f"{counts['failed_held'] + counts['followed_held']} events where it held and "
                            f"{counts['failed_not']} of {counts['failed_not'] + counts['followed_not']} where it did "
                            f"not (one-sided Fisher p = {p:.3f}; proposed after seeing these events)"})
    return out


def investigation_packet(rule: Mapping[str, Any], described: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The rule and its events, every failure and the followed ones spread evenly, up to MAX_EVENTS."""
    failed = [e for e in described if not e["followed"]]
    followed = [e for e in described if e["followed"]]
    room = max(MAX_EVENTS - len(failed), 0)
    step = max(len(followed) / room, 1.0) if room else 0
    sample = [followed[int(i * step)] for i in range(min(room, len(followed)))] if room else []
    return {"rule": {k: v for k, v in rule.items() if k not in ("basis", "set_aside")},
            "events": sorted(failed + sample, key=lambda e: e["event"])}


# A reader named for an angle, in degrees: its readings wrap (356 -> 43 is a turn of +47, not -313). Read from the
# alias the preflight chose, the same rule for every game; any other reader's readings are taken as they are.
ANGLE_NAME = re.compile(r"(yaw|pitch|roll|heading|angle|rotation|euler)", re.IGNORECASE)


def is_angle(observable: str) -> bool:
    return observable == "camera.rotation" or bool(ANGLE_NAME.search(observable.split(".")[-1]))


def _direction(values: Sequence[Any], angle: bool = False) -> int | None:
    """+1 or -1 by the reading furthest from the first one, or None when the readings are not numbers. An angle's
    readings are unwrapped around the first one, so a turn across 0/360 keeps its sign."""
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(numbers) != len(values) or len(numbers) < 2:
        return None
    first = numbers[0]
    if angle:
        numbers = [first + ((v - first + 180.0) % 360.0) - 180.0 for v in numbers]
    furthest = max(numbers[1:], key=lambda v: abs(v - first))
    return (furthest > first) - (furthest < first)


def _cells(table: Mapping[str, Any], verb: str, state: str) -> list[tuple[str, Any]]:
    row = (table.get("verbs") or {}).get(verb) or {}
    if state == "rest":
        return [("at rest", row.get("rest"))]
    if state == "air":
        return [("in the air", row.get("air"))]
    moving = row.get("moving") or {}
    moves = (table.get("moves") or ()) if state == "moving" else [state]
    return [(f"while {move} held", moving.get(move)) for move in moves]


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _furthest(values: Sequence[Any]) -> Any:
    """The reading furthest from the first one (for a flag or a text, the last one)."""
    first = _number(values[0]) if values else None
    numbers = [_number(v) for v in values[1:]]
    if first is None or not numbers or any(n is None for n in numbers):
        return values[-1] if values else None
    return max(numbers, key=lambda n: abs(n - first))


def _at_limit(observable: str, row: Mapping[str, Any], cell: Mapping[str, Any]) -> bool:
    """Whether a trial in this cell began with the reader already at the end the verb drives it to: a reader at its
    limit (a view pitched all the way up) cannot show a press that would move it further. The guard table's own
    test (`guards._at_limit`): direction-aware, starts included, vectors per component, angles unwrapped."""
    from playtest.runner.guards import _at_limit as at_limit

    return at_limit(row, cell, observable)


def _reached(value: Any, target: Any) -> bool:
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        return value == target
    number = _number(value)
    return number is not None and abs(number - target) <= TOLERANCE * max(abs(target), 1.0)


def outcome(prediction: Mapping[str, Any], row: Mapping[str, Any], cell: Mapping[str, Any] | None) -> str:
    """held; missing, reversed or wrong_value (the prediction failed); or ambient (the observable changes by itself
    here), masked (the state without the verb changes it), at_limit (it began where the verb's presses end),
    not_airborne (an in-air cell whose presses all went in before the body moved) or unmeasured, none of which can
    show the press."""
    if not cell:
        return "unmeasured"
    pressed = cell.get("pressed_at")
    if isinstance(pressed, list) and pressed and all(isinstance(d, (int, float)) and d <= 1e-3 for d in pressed):
        return "not_airborne"  # every in-air press went in before the body had left where it started
    if prediction["expect"] == "moves_body":
        peak, base = cell.get("peak_displacement"), cell.get("baseline_peak_displacement")
        if isinstance(peak, (int, float)) and isinstance(base, (int, float)):
            moved = peak > 1e-3 if base <= 1e-3 else abs(peak - base) > DISPLACEMENT_RATIO * base
        else:
            moved = cell.get("moved_differently")
        return "unmeasured" if moved is None else "held" if moved else "missing"
    observable = prediction["observable"]
    values = (cell.get("changed") or {}).get(observable)
    if values is None:
        if any(observable in keys for keys in cell.get("trial_changed") or ()):
            return "held"  # seen in some trials and not all: a flicker or a lagging reader, not a missing effect
        if observable in (row.get("ambient") or ()):
            return "ambient"
        if observable in (cell.get("baseline_changed") or ()):
            return "masked"
        if _at_limit(observable, row, cell):
            return "at_limit"
        return "missing"
    if prediction["expect"] == "changes" or observable in (row.get("ambient") or ()):
        # A reader that changes by itself is credited to a press only when the press moves it against its own
        # trend (a reset), so the direction of what was measured is never the prediction's to test.
        return "held"
    if prediction["expect"] == "reaches":
        return "held" if _reached(_furthest(values), prediction["value"]) else "wrong_value"
    direction = _direction(values, is_angle(observable))
    if direction is None:
        return "held"
    return "held" if direction == (1 if prediction["expect"] == "increases" else -1) else "reversed"


def score(predictions: Sequence[Mapping[str, Any]], tables: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each prediction in each measured cell of its state, across the fresh sessions' guard tables: k of n missing.

    A mismatch is a prediction that failed (missing or reversed) in more than half the sessions that measured it.
    """
    out = []
    for prediction in predictions:
        if any(prediction["verb"] == table.get("rise") for table in tables):
            out.append(_score_rise_air(prediction, tables))
            continue
        labels = list(dict.fromkeys(label for table in tables
                                    for label, _ in _cells(table, prediction["verb"], prediction["state"])))
        for label in labels:
            outcomes = []
            for table in tables:
                row = (table.get("verbs") or {}).get(prediction["verb"]) or {}
                cell = dict(_cells(table, prediction["verb"], prediction["state"])).get(label)
                outcomes.append(outcome(prediction, row, cell))
            measured = [o for o in outcomes if o in ("held", "missing", "reversed", "wrong_value")]
            failed = [o for o in measured if o != "held"]
            verdict = ("mismatch" if 2 * len(failed) > len(measured) else "held" if not failed else "mixed")
            if not measured:
                # Why nothing could show the press: the most common reason across the sessions.
                verdict = max(("ambient", "masked", "at_limit", "not_airborne", "unmeasured"), key=outcomes.count)
            elsewhere = None
            if verdict == "mismatch" and all(o == "missing" for o in failed):
                elsewhere = _works_elsewhere(prediction, label, tables)
                if elsewhere:
                    verdict = "guarded"
            text = line(prediction, label, failed, measured)
            if elsewhere:
                text += (f", though in most of them it did {elsewhere}: a guard, which is a rule of the game or a bug "
                         "the declarations must decide")
            out.append({**{k: prediction.get(k) for k in ("verb", "state", "observable", "expect", "value", "basis",
                                                           "because")},
                        "cell": label, "outcomes": outcomes, "k": len(failed), "n": len(measured),
                        "verdict": verdict, "line": text, **({"works": elsewhere} if elsewhere else {})})
    return out


def _state_of(label: str) -> str:
    return "rest" if label == "at rest" else "air" if label == "in the air" else "moving"


def _works_elsewhere(prediction: Mapping[str, Any], label: str, tables: Sequence[Mapping[str, Any]]) -> str | None:
    """Where, in another state, the verb changed the predicted observable in more than half the tables, or None.

    A missing effect the same verb shows in another state (at rest, while moving, in the air) is a guard: the input
    works, only not here. Another move of the same state does not count: an effect present while one direction is
    held and missing while another is, is a symmetry the game is evidently built to keep, and stays a mismatch, as
    an input with no effect anywhere does.
    """
    here = _state_of(label)
    probe = prediction if prediction["expect"] == "moves_body" else {**prediction, "expect": "changes"}
    counts: dict[str, int] = {}
    for table in tables:
        row = (table.get("verbs") or {}).get(prediction["verb"]) or {}
        seen = set()
        for state in ("rest", "moving", "air"):
            if state == here:
                continue
            for other_label, cell in _cells(table, prediction["verb"], state):
                if cell and outcome(probe, row, cell) == "held":
                    seen.add(other_label)
        for other_label in seen:
            counts[other_label] = counts.get(other_label, 0) + 1
    where = [other for other, k in counts.items() if 2 * k > len(tables)]
    return " and ".join(sorted(where)) if where else None


def _score_rise_air(prediction: Mapping[str, Any], tables: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The rise pressed again at its own apex (`rise_air`, the options layer's air-rise probe) against a rise alone:
    held when the second press lifted the body higher by more than the probe's share of a rise."""
    outcomes = []
    for table in tables:
        air = table.get("rise_air")
        if not air or air.get("pressed_peak") is None or air.get("rest_peak") is None:
            outcomes.append("unmeasured")
        else:
            outcomes.append("held" if air.get("gained") else "missing")
    measured = [o for o in outcomes if o != "unmeasured"]
    failed = [o for o in measured if o != "held"]
    verdict = ("unmeasured" if not measured else "mismatch" if 2 * len(failed) > len(measured)
               else "held" if not failed else "mixed")
    label = "pressed again in the air"
    return {**{k: prediction.get(k) for k in ("verb", "state", "observable", "expect", "value", "basis", "because")},
            "cell": label, "outcomes": outcomes, "k": len(failed), "n": len(measured), "verdict": verdict,
            "line": line(prediction, label, failed, measured)}


def line(prediction: Mapping[str, Any], label: str, failed: Sequence[str], measured: Sequence[str]) -> str:
    if prediction["expect"] == "moves_body":
        what = "to move the body differently"
    elif prediction["expect"] == "reaches":
        what = f"{prediction['observable']} to reach {json.dumps(prediction['value'])}"
    else:
        what = (f"to {'change' if prediction['expect'] == 'changes' else prediction['expect'][:-1]} "
                f"{prediction['observable']}")
    how = ("the other way" if failed and all(f == "reversed" for f in failed)
           else "a different value" if failed and all(f == "wrong_value" for f in failed) else "not")
    return (f"{prediction['verb']} {label}: expected {what} ({prediction['basis']}: {prediction['because']}); "
            f"{how} in {len(failed)} of {len(measured)} fresh sessions")


TIERS = (("mismatch", "Not met", "the expectation failed in most of the sessions that could show it"),
         ("guarded", "Works elsewhere, not here", "the effect was missing here in most sessions but the same input "
                                                   "showed it in another state: a guard, a rule of the game or a bug"),
         ("mixed", "Sporadic", "it failed in some sessions or events and held in others: a candidate for a "
                               "controlled test, not a finding"),
         ("held", "Held", "it was met every time it could be shown"))
UNSHOWN = ("ambient", "masked", "at_limit", "not_airborne", "while_held", "intermittent", "unmeasured")


def report(guard_rows: Sequence[Mapping[str, Any]], play_rows: Sequence[Mapping[str, Any]],
           set_aside: Sequence[Mapping[str, Any]], *, title: str) -> str:
    """The expectations, grouped by what measurement made of them, as Markdown: what the game declared, what was
    measured, and k of n from clean starts. Nothing here is a verdict about intent; each line says what failed."""
    rows = [*guard_rows, *play_rows]
    lines = [f"# {title}", "",
             "Expectations were written from the game's declarations before any measurement (stated intents, "
             "authored values, field and method names), then scored by code against fresh sessions: guard tables "
             "measured from a clean start, and the readings of whole play sessions.", ""]
    rows = [tiered(dict(r)) for r in rows]
    for verdict, heading, meaning in TIERS:
        chosen = [r for r in rows if r["verdict"] == verdict]
        if verdict == "mismatch":
            # The strongest first: a declared value or a stated intent, then what only the names suggested.
            chosen.sort(key=lambda r: r.get("tier") == "names_only")
        lines += [f"## {heading} ({len(chosen)})", "", f"*{meaning}.*", ""]
        lines += [f"- {r['line']}" for r in chosen] or ["- none"]
        lines.append("")
    unshown = [r for r in rows if r["verdict"] in UNSHOWN]
    lines += [f"## Could not be shown ({len(unshown)})", "",
              "*ambient: the reader changes by itself there; masked: the state without the verb changes it; "
              "at_limit: it began where the verb's presses end; not_airborne: the in-air press went in before the body "
              "left the ground; unmeasured: no session measured that cell.*", ""]
    lines += [f"- {r['verdict']}: " + (f"{r['verb']} {r['cell']}, {r['observable']}" if "cell" in r else r["line"])
              for r in unshown] or ["- none"]
    lines += ["", f"## Set aside before scoring ({len(set_aside)})", ""]
    lines += [f"- {item.get('verb') or item.get('action') or item.get('observable') or item.get('when')}: "
              f"{item['set_aside']}"
              + (f" ({item['precondition']})" if item.get("precondition") else "") for item in set_aside] or ["- none"]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """`predict` expectations from the declarations (one paid call), or `score` them against guard tables."""
    parser = argparse.ArgumentParser(prog="python -m playtest.runner.expectations", description=main.__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    predict = sub.add_parser("predict")
    for name in ("manifest", "discovery", "guards", "approval", "output"):
        predict.add_argument(f"--{name}", type=Path, required=True)
    predict.add_argument("--project", type=Path, help="the Unity project, so the assets the entity references "
                         "can be read")
    predict.add_argument("--ref", help="the build under test's commit or branch in --project (default: its working "
                         "tree)")
    predict.add_argument("--model", default=MODEL, help="the frontier model that writes the expectations")
    predict.add_argument("--part", choices=("all", "magnitudes", "actions"), default="all",
                         help="magnitudes or actions: one focused call for the declared sizes, or for what each "
                              "press must make happen in play, only")
    scorer = sub.add_parser("score")
    scorer.add_argument("--expectations", type=Path, required=True)
    scorer.add_argument("--runs", type=Path, nargs="+", required=True, help="run folders, each with guards.json")
    scorer.add_argument("--output", type=Path, required=True)
    reporter = sub.add_parser("report")
    reporter.add_argument("--guards-score", type=Path, required=True, help="the output of score")
    reporter.add_argument("--play-score", type=Path, help="the output of score-play")
    reporter.add_argument("--title", default="Expectations against measurement")
    reporter.add_argument("--output", type=Path, required=True, help="a new Markdown file")
    investigator = sub.add_parser("investigate")
    investigator.add_argument("--expectations", type=Path, required=True)
    investigator.add_argument("--runs", type=Path, nargs="+", required=True, help="run folders, each with trace.jsonl")
    investigator.add_argument("--approval", type=Path, required=True)
    investigator.add_argument("--model", default=MODEL, help="the frontier model that proposes the conditions")
    investigator.add_argument("--output", type=Path, required=True, help="a new folder")
    planner = sub.add_parser("test-plan")
    planner.add_argument("--expectations", type=Path, nargs="+", required=True,
                         help="expectations.json files whose action rules and situations to try")
    planner.add_argument("--output", type=Path, required=True, help="a new objective text file")
    player = sub.add_parser("score-play")
    player.add_argument("--expectations", type=Path, required=True)
    player.add_argument("--runs", type=Path, nargs="+", required=True, help="run folders, each with trace.jsonl")
    player.add_argument("--output", type=Path, required=True)
    player.add_argument("--guards", type=Path, nargs="*", help="more run folders of the same build whose guards.json "
                        "says what a press-anchored reading can show")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("the output must be new")
    if getattr(args, "model", None) is not None:
        from playtest.frontier import provider as _provider

        _provider.use_model(args.model)   # its price; requests and ledgers below name it
        globals()["MODEL"] = args.model
    if args.command == "report":
        guard = json.loads(args.guards_score.read_text(encoding="utf-8"))
        play = json.loads(args.play_score.read_text(encoding="utf-8")) if args.play_score else {"scored": []}
        text = report(guard["scored"], play["scored"], guard.get("set_aside") or [], title=args.title)
        args.output.write_text(text, encoding="utf-8", newline="\n")
        print(text)
        return 0
    if args.command == "test-plan":
        merged: dict[str, list] = {"action_rules": [], "situations": []}
        for path in args.expectations:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            for key in merged:
                merged[key] += loaded.get(key) or []
        text = test_plan(merged)
        if not text:
            parser.error("no action rule or situation to try")
        args.output.write_text(text + "\n", encoding="utf-8", newline="\n")
        print(text)
        return 0
    if args.command == "investigate":
        return investigate(args)
    if args.command == "score-play":
        expectations = json.loads(args.expectations.read_text(encoding="utf-8"))
        traces = [[json.loads(line) for line in (run / "trace.jsonl").read_text(encoding="utf-8").splitlines()
                   if line.strip()] for run in args.runs]
        sessions = [play_readings(rows) for rows in traces]
        presses = [play_presses(rows) for rows in traces]
        controls = [catalog_controls(json.loads((run / "catalog.json").read_text(encoding="utf-8")))
                    if (run / "catalog.json").is_file() else {} for run in args.runs]
        guard_tables = [json.loads((run / "guards.json").read_text(encoding="utf-8"))
                        for run in [*args.runs, *(args.guards or ())] if (run / "guards.json").is_file()]
        rows = (score_ranges(expectations.get("ranges") or (), sessions)
                + score_transitions(expectations.get("transitions") or (), sessions)
                + score_durations(expectations.get("durations") or (), sessions)
                + score_action_rules(expectations.get("action_rules") or (), sessions, presses, controls,
                                     guard_tables)
                + score_situations(expectations.get("situations") or (), sessions, presses, controls,
                                   [situation_presses(rows) for rows in traces], guard_tables))
        mismatches = [r for r in rows if r["verdict"] == "mismatch"]
        result = {"runs": [str(r) for r in args.runs], "mismatches": mismatches, "scored": rows}
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
        for row in rows:
            print(row["verdict"], "|", row["line"])
        return 0
    if args.command == "score":
        expectations = json.loads(args.expectations.read_text(encoding="utf-8"))
        tables = [json.loads((run / "guards.json").read_text(encoding="utf-8")) for run in args.runs]
        rows = score(expectations["predictions"], tables) + score_magnitudes(expectations.get("magnitudes") or (),
                                                                               tables)
        mismatches = [r for r in rows if r["verdict"] == "mismatch"]
        result = {"runs": [str(r) for r in args.runs], "set_aside": expectations.get("set_aside") or [],
                  "mismatches": mismatches, "scored": rows}
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
        for row in mismatches:
            print(row["line"])
        print(f"{len(mismatches)} mismatches of {len(rows)} scored cells; {len(result['set_aside'])} set aside")
        return 0

    from playtest.bench import call_budget, replay
    from playtest.frontier import provider

    approval, _ = replay._load(args.approval)
    replay.validate_approval(approval, [MODEL])
    if not os.environ.get("META_API_KEY"):
        parser.error("META_API_KEY is required")
    manifest, _ = replay._load(args.manifest)
    discovery, _ = replay._load(args.discovery)
    guards = json.loads(args.guards.read_text(encoding="utf-8"))
    packet = build_packet(manifest, discovery, guards,
                          project_reader(args.project, args.ref) if args.project is not None else None)
    body = request_body(packet, args.part)
    sha = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "packet.json").write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8", newline="\n")
    ledger = call_budget.Ledger(args.output, approval, MODEL)
    reservation = ledger.reserve(episode=0, turn=0, based_on_seq=0, request_sha256=sha, requested_model=MODEL)
    try:
        status, data, latency, problem = provider.send_meta(body, timeout_s=TIMEOUT_S)
        usage = (data or {}).get("usage") or None
        cost = provider._cost(usage)
        reservation.finish(http_status=status, latency_ms=latency, cost_usd=cost, usage=usage, problem=problem)
    except Exception:
        reservation.finish(cost_usd=None, problem="the call failed before a response was recorded")
        raise
    # The paid call is recorded before its answer is read, so a parse fault cannot lose its cost.
    answer = json.loads(data["choices"][0]["message"]["content"]) if status == 200 and data else None
    kept, set_aside = screened((answer or {}).get("predictions") or (), packet)
    play, play_aside = screened_play(answer or {}, packet)
    result = {"request_sha256": sha, "status": status, "problem": problem, "usage": usage, "cost": cost,
              "predictions": kept, **play, "set_aside": set_aside + play_aside, "answer": answer}
    (args.output / "expectations.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"status {status}, cost {cost}, {len(kept)} predictions, {len(play['ranges'])} ranges, "
          f"{len(play['transitions'])} transitions, {len(play['durations'])} durations, "
          f"{len(play['magnitudes'])} magnitudes, {len(play['action_rules'])} action rules, "
          f"{len(play['situations'])} situations kept; {len(set_aside) + len(play_aside)} set aside")
    return 0 if status == 200 else 1


def situation_regions(situations: Sequence[Mapping[str, Any]], positions: Mapping[str, Any], *, scene: str,
                      paths: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """A reach region around each situation's object, where one transforms reading at the session's start put it.

    The agent declares these itself, from its own pre-registered situations: author, source and verification say
    so, and none is an operator's assertion or a verified target. S1 reads each region's measured distance as a fact
    and the frontier may set one as a directive's setpoint; whether a press was made in the situation is measured
    again at the press (`targets_before`), since an object can move.
    """
    regions = []
    for number, item in enumerate(situations, 1):
        target = item.get("target_object")
        region_id = item.get("region_id") or f"situation_{number}"
        placed = positions.get(target) if target else None
        position = (placed or {}).get("position") if isinstance(placed, Mapping) else None
        if not isinstance(position, (list, tuple)) or len(position) != 3:
            continue
        r = float(item["within"])
        regions.append({
            "region_id": region_id, "label": item.get("target_path") or (paths or {}).get(target) or target,
            "purpose": f"{item['situation']}, then press {item['action']}", "frame": "world", "scene": scene,
            "bounds": {"min": [float(v) - r for v in position], "max": [float(v) + r for v in position]},
            "pose": "probe_anchor", "author": "the agent's pre-registered expectations (a situation)",
            "source": "the situation's object position from the transforms reading at this session's start",
            "verification": "unverified"})
    return regions


def _in_situation(item: Mapping[str, Any], observed: Mapping[str, Any]) -> bool | None:
    """Whether the body was within the situation's distance of its object at the press, or None when unread."""
    target = item.get("target_object")
    if target is None:
        return None
    placed = (observed.get("targets_before") or {}).get(target)
    body = observed.get("from_position")
    if not isinstance(placed, Mapping) or not isinstance(placed.get("position"), (list, tuple)) or \
            not isinstance(body, (list, tuple)) or len(body) != len(placed["position"]):
        return None
    if placed.get("active") is False:
        return False   # a collected pickup or a despawned enemy is no longer there to be near
    import math as _math
    return _math.dist(body, placed["position"]) <= float(item["within"])


def situation_presses(trace_rows: Sequence[Mapping[str, Any]]) -> list[tuple[float, Mapping[str, Any]]]:
    """Each press's effect record (`observed`) by its start, for the situation check at that press."""
    from playtest.runner.review import _epoch

    out = []
    for row in trace_rows:
        if row.get("kind") == "effect":
            observed = (row.get("body") or {}).get("observed") or {}
            before = (observed.get("readers_before") or {}).get("wall_s")
            if isinstance(before, (int, float)):
                out.append((float(before), observed))
    return sorted(out, key=lambda item: item[0])


def score_situations(situations: Sequence[Mapping[str, Any]], sessions: Sequence[Sequence[tuple[float, int, dict]]],
                     presses: Sequence[Sequence[tuple[float, float, str]]], controls: Sequence[Mapping[str, frozenset]],
                     effects: Sequence[Sequence[tuple[float, Mapping[str, Any]]]],
                     guards: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """A situation's rule at the presses made in the situation, measured at each press: the body within its
    distance of the situation's object (an object no longer active is not there). A press outside it, or one whose
    situation could not be read, is not counted."""
    out = []
    for item in situations:
        if item.get("target_object") is None:
            continue
        kept_presses, skipped_out, unread = [], 0, 0
        for pressed, recorded, served in zip(presses, effects, controls):
            mine = []
            verb = served.get(item["action"])
            for start, end, action in pressed:
                if action != item["action"] and not (verb and verb <= served.get(action, frozenset())):
                    continue   # another input's press says nothing about this rule's situation
                observed = next((o for t, o in recorded if abs(t - start) <= 1.5 and t <= start + 0.05), None)
                where = _in_situation(item, observed) if observed is not None else None
                if where is None:
                    unread += 1
                elif where:
                    mine.append((start, end, action))
                else:
                    skipped_out += 1
            kept_presses.append(mine)
        (row,) = score_action_rules([item], sessions, kept_presses, controls, guards)
        row["situation_not_met"], row["situation_unread"] = skipped_out, unread
        row["line"] += f"; {skipped_out} presses outside the situation and {unread} unread were not counted"
        out.append(row)
    return out


LIVE_RULES_SHOWN = 8


def replay_check(rule: Mapping[str, Any]) -> dict[str, Any] | None:
    """The replay check a finding about this rule would carry, made from the rule as written: its reader read right
    after each press of its action (inside its situation's region, for a situation), failing to do what the rule
    says. None for a rule with a reader condition, which a replay cannot hold to: every press would be scored."""
    if rule.get("condition_observable") is not None or not rule.get("then") or not rule.get("action"):
        return None
    check: dict[str, Any] = {"field": f"public.{rule['then']}", "after_action": rule["action"]}
    if rule.get("then_expect") == "reaches":
        value = rule.get("then_value")
        check.update(op="!=", value=value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            check["tolerance"] = round(TOLERANCE * max(abs(value), 1.0), 6)
    elif rule.get("then_expect") in ("changes", "increases", "decreases"):
        check.update(op="unchanged", value=None)
    else:
        return None
    if rule.get("target_object") and rule.get("region_id"):
        check["within_region"] = rule["region_id"]
    return check


def live_rule_lines(expectations: Mapping[str, Any], trace_rows: Sequence[Mapping[str, Any]],
                    controls: Mapping[str, frozenset], guards: Mapping[str, Any] | None = None,
                    limit: int = LIVE_RULES_SHOWN) -> list[str]:
    """Each pre-registered action rule and situation, scored by code on this session's presses so far, as one line
    with its verdict: the same scoring `score-play` does after the session. A rule with fewer than two counted
    presses is left out (nothing measured yet), and so is one the guard tables say a press-anchored reading cannot
    show (while_held, intermittent): its failed presses measure the reading, not the game. A measurement for the
    frontier, never a decision: it judges them.
    """
    sessions, presses = [play_readings(trace_rows)], [play_presses(trace_rows)]
    tables = [guards] if guards else []
    rows = (score_action_rules(expectations.get("action_rules") or (), sessions, presses, [controls], tables)
            + score_situations(expectations.get("situations") or (), sessions, presses, [controls],
                               [situation_presses(trace_rows)], tables))
    out = []
    for row in rows:
        if row.get("verdict") in ("unmeasured", "while_held", "intermittent"):
            continue
        line = f"{row['verdict']}: {row['line']}"
        check = replay_check(row) if row.get("verdict") == "mismatch" else None
        if check is not None:
            line += f"; a finding about it can carry this repro_check: {json.dumps(check)}"
        out.append(line)
    return out[:limit]


PRESS_TEST_PRESSES = 4      # the presses one test makes: the test plan's own "4 times or more"
PRESS_TEST_MARGIN_S = 0.5   # past a rule's window, so the next press is not inside it (`action_events`)
PRESS_TEST_MAX_WINDOW_S = 5.0   # a longer window would make one test outlast S1's attention: not offered


def press_test_specs(*expectations: Mapping[str, Any]) -> list[Any]:
    """One press-test option per input a pre-registered action rule presses: that input pressed
    `PRESS_TEST_PRESSES` times, each followed by the longest of its rules' windows plus a margin with nothing
    pressed, so no press lands inside the last one's window and every press can be counted. A situation's rule
    gets none: whether a press was made in its situation is read from the press's own effect record, which an
    option's presses do not have.

    Written by code from the rules, never by a model; offered as a choice S1 may take or leave, never run by
    itself. An input whose window is longer than `PRESS_TEST_MAX_WINDOW_S` gets none.
    """
    from .options.model import OptionSpec

    windows: dict[str, float] = {}
    for doc in expectations:
        for rule in doc.get("action_rules") or ():
            action, within = rule.get("action"), rule.get("within_s")
            if isinstance(action, str) and action and isinstance(within, (int, float)) and within > 0:
                windows[action] = max(windows.get(action, 0.0), float(within))
    specs = []
    for action, within in windows.items():
        if within > PRESS_TEST_MAX_WINDOW_S:
            continue
        wait = round(within + PRESS_TEST_MARGIN_S, 3)
        total = PRESS_TEST_PRESSES * (wait + 0.2)
        specs.append(OptionSpec(
            option_id=f"test:{action}", template="press_test",
            params={"action": action, "presses": PRESS_TEST_PRESSES, "wait_s": wait},
            intent=(f"test {action}: press it {PRESS_TEST_PRESSES} times, each followed by {wait:g} s with nothing "
                    f"pressed, about {total:.0f} s")))
    return specs


def test_plan(expectations: Mapping[str, Any], *, presses: int = 4, tests: bool = False) -> str:
    """A session objective that asks for each action rule's press to be made on purpose, in plain words.

    Written by code from the pre-registered rules, never by a model, so it says nothing the rules do not: the input
    to press, the situation to bring about first when a rule needs one, and, for a reader condition, only that the
    effect needs it (S1 sees the readers). Each press is to be repeated, apart, so the scorer has several to count.
    """
    lines = []
    rules = [*(expectations.get("action_rules") or ()), *(expectations.get("situations") or ())]
    for rule in rules:
        step = f"press {rule['action']}"
        if rule.get("situation"):
            where = (f" (go to the place the progress fact {rule['region_id']} measures, within "
                     f"{rule['within']:g})" if rule.get("region_id") else "")
            step = f"{rule['situation'].rstrip('.')}{where}, then {step}"
        if rule.get("condition_observable"):
            step += (f" when {rule['condition_observable']} {rule['condition_op']} "
                     f"{json.dumps(rule['condition_value'])}")
        lines.append(step)
    steps = list(dict.fromkeys(lines))
    if not steps:
        return ""
    numbered = "; ".join(f"({n}) {step}" for n, step in enumerate(steps, 1))
    offered = (" A test option for an input (test:<input>) makes its presses that far apart for you." if tests
               else "")
    return (f"Try each of these on purpose, {presses} times or more, a few seconds apart, so each one can be seen on "
            f"its own: {numbered}.{offered} Then keep exploring.")


def _send(body: Mapping[str, Any], approval: Mapping[str, Any], folder: Path, name: str) -> tuple[Any, Any]:
    """One paid call, recorded in the folder's ledger before its answer is read."""
    from playtest.bench import call_budget
    from playtest.frontier import provider

    sha = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    ledger = call_budget.Ledger(folder / name, dict(approval), MODEL)
    reservation = ledger.reserve(episode=0, turn=0, based_on_seq=0, request_sha256=sha, requested_model=MODEL)
    try:
        status, data, latency, problem = provider.send_meta(dict(body), timeout_s=TIMEOUT_S)
        usage = (data or {}).get("usage") or None
        cost = provider._cost(usage)
        reservation.finish(http_status=status, latency_ms=latency, cost_usd=cost, usage=usage, problem=problem)
    except Exception:
        reservation.finish(cost_usd=None, problem="the call failed before a response was recorded")
        raise
    choice = ((data or {}).get("choices") or [{}])[0] if status == 200 else {}
    content = (choice.get("message") or {}).get("content")
    answer = json.loads(content) if isinstance(content, str) and content.strip() else None
    if status == 200 and answer is None:
        # A reply with no content (the reasoning used every token): recorded as that, never read as "none found".
        problem = problem or f"no answer content (finish_reason {choice.get('finish_reason')!r})"
    return answer, {"request_sha256": sha, "status": status, "problem": problem, "usage": usage, "cost": cost}


def investigate(args: argparse.Namespace) -> int:
    """For each play rule that failed sometimes: the frontier proposes what separates its failures, code tests it."""
    from playtest.bench import replay

    approval, _ = replay._load(args.approval)
    replay.validate_approval(approval, [MODEL])
    if not os.environ.get("META_API_KEY"):
        raise SystemExit("META_API_KEY is required")
    expectations = json.loads(args.expectations.read_text(encoding="utf-8"))
    traces = [[json.loads(line) for line in (run / "trace.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()] for run in args.runs]
    sessions = [play_readings(rows) for rows in traces]
    inputs = [play_inputs(rows) for rows in traces]
    starts = [readings[0][0] if readings else 0.0 for readings in sessions]
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    rules = [(f"transition-{n}", rule, transition_events) for n, rule in
             enumerate(expectations.get("transitions") or (), 1)]
    rules += [(f"duration-{n}", rule, duration_events) for n, rule in enumerate(expectations.get("durations") or (), 1)]
    for name, rule, events_of in rules:
        events = events_of(rule, sessions)
        if not any(not e["followed"] for e in events) or all(not e["followed"] for e in events):
            continue  # held every time, or failed every time (a mismatch, not a condition to find)
        described = describe_events(events, inputs, starts)
        packet = investigation_packet(rule, described)
        body = {"model": MODEL, "reasoning_effort": "low", "max_tokens": INVESTIGATE_MAX_TOKENS,
                "messages": [{"role": "system", "content": INVESTIGATE_SYSTEM},
                             {"role": "user", "content": json.dumps(packet, separators=(",", ":"))}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "hypotheses", "strict": True, "schema": HYPOTHESES}}}
        answer, call = _send(body, approval, args.output, name)
        tested = check_hypotheses((answer or {}).get("hypotheses") or (), described)
        results.append({"name": name, "rule": rule, "events": len(described),
                        "failed": sum(not e["followed"] for e in described), "call": call,
                        "answered": answer is not None, "hypotheses": tested})
        print(f"{name}: {len(described)} events, {results[-1]['failed']} failed, cost {call['cost']}"
              + ("" if answer is not None else f"; no answer: {call['problem']}"))
        for row in tested:
            print("  " + row["line"])
        # Written after every rule, so a later fault cannot lose a paid answer.
        (args.output / "investigation.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8",
                                                         newline="\n")
    if not results:
        (args.output / "investigation.json").write_text("[]\n", encoding="utf-8", newline="\n")
    return 0




class ExpectationFacts:
    """Append the expectations this session's guard table did not meet to the inner source's facts, each turn.

    They are measured facts for S1 to weigh, as any other fact: S1, not code, decides whether one is worth
    escalating. Everything else is forwarded unchanged.
    """

    PREFIX = "Measured before play on this build, against what its declarations say: "

    def __init__(self, inner: Any, lines: Sequence[str]) -> None:
        self._inner, self._lines = inner, [self.PREFIX + line for line in lines]

    async def facts(self, reports: Sequence[Any], observation: Any) -> list[str]:
        return [*await self._inner.facts(reports, observation), *self._lines]

    def directive_view(self, observation: Any) -> Any:
        view = getattr(self._inner, "directive_view", None)
        return view(observation) if view is not None else None

    def finish(self, **values: Any) -> None:
        finish = getattr(self._inner, "finish", None)
        if finish is not None:
            finish(**values)

    def rebind(self, **values: Any) -> None:
        rebind = getattr(self._inner, "rebind", None)
        if rebind is not None:
            rebind(**values)


def unmet_in_session(expectations: Mapping[str, Any], guards: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The predictions and declared sizes this one session's guard table did not meet (k of 1), each labelled by
    its oracle."""
    rows = (score(expectations.get("predictions") or (), [guards])
            + score_magnitudes(expectations.get("magnitudes") or (), [guards]))
    return [tiered(row) for row in rows if row["verdict"] == "mismatch"]


NAMES_ONLY = " [inferred from names only: no stated intent or declared value says so]"


def tiered(row: dict[str, Any]) -> dict[str, Any]:
    """A mismatch whose only oracle is the names of the action, fields and methods is the weakest evidence: the
    names can suggest an effect the game never had (a dash that writes an invisibility flag elsewhere). It keeps
    its verdict, and its line says so, so S1 and the report weigh it; a stated intent or a declared value says more.
    """
    if row.get("verdict") == "mismatch" and row.get("basis") == "names" and NAMES_ONLY not in row.get("line", ""):
        return {**row, "tier": "names_only", "line": row["line"] + NAMES_ONLY}
    return row


if __name__ == "__main__":
    raise SystemExit(main())
