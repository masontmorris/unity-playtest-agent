"""A small trace checker for the loop tests: the rules `playtest.core.trace_sink` says a replay applies.

It is not the replay (#30). It re-derives what it can from the entries and lists every difference, so a
test can assert that a trace the loop wrote is clean: contiguous sequence numbers, the entry order and
refs of each turn, bodies with the documented keys, the choice set bound to its observation, the
context hash, `parse_decision` reproducing each decision, the turn record agreeing with its entries,
the adherence check, and the help lifecycle (one open request, each adopted or cancelled once).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceKind, ChoiceSet, TurnRecord
from playtest.core.adherence import check_adherence
from playtest.core.context import canonical
from playtest.core.decisions import parse_decision

AUTHORS = {
    "run": "harness", "observation": "bridge", "choice_set": "core", "context": "core", "s1_call": "s1",
    "decision": "core", "execution": "bridge", "effect": "core", "help": "core",
    "investigation": "frontier", "suspicion": "core", "turn": "core",
    "stop": "core", "end": "harness", "finding": "core",
}
# A finding is written by whoever judges: the core, the frontier, or the harness after the run.
EXTRA_AUTHORS = {"finding": ("frontier", "helper", "harness")}
BODY_KEYS = {
    "context": {"sha256", "payload"},
    "s1_call": {"question", "provider", "model", "resolved_model", "request_sha256", "http_status",
                "latency_ms", "problem", "response", "usage"},
    "decision": {"answer", "clock_mode", "valid", "error", "policy_default", "decision"},
    "execution": {"purpose", "request", "records"},
    "effect": {"choice_id", "action", "expected", "observed"},
    "help": {"event", "choice_id", "kind", "opened_turn", "source", "result", "reason"},
    "investigation": {"event", "request_turn", "based_on_seq", "outcome_seq", "hypothesis", "directive",
                       "outcome", "evidence_trace_seq", "reason"},
    "stop": {"reason", "result", "error"},
    "end": {"status", "error", "turns", "redactions"},
}
REQUEST_KEYS = {"action_id", "choice_id", "action", "params", "based_on_seq", "max_age_s", "depends_on", "lease_s"}
TOKENS = {"observation": "O", "choice_set": "S", "context": "C", "s1_call": "1", "decision": "D",
          "execution": "X", "effect": "E", "turn": "T", "suspicion": "P", "finding": "F"}
# [adopted] observation choice_set suspicion* context s1_call* decision [opened|repeat] (execution [effect suspicion*])* turn
ORDER = re.compile(r"^A?OSP*C1*DH?(X(EP*)?){0,2}T$")


def problems(entries: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for index, entry in enumerate(entries):
        if entry["trace_seq"] != index:
            found.append(f"entry {index} has trace_seq {entry['trace_seq']}")
        if any(not 0 <= ref < index for ref in entry["refs"]):
            found.append(f"entry {index} cites a ref that is not earlier")
        if entry["author"] != AUTHORS[entry["kind"]] and entry["author"] not in EXTRA_AUTHORS.get(entry["kind"], ()):
            found.append(f"entry {index} ({entry['kind']}) is authored by {entry['author']}")
        keys = BODY_KEYS.get(entry["kind"])
        if keys is not None and set(entry["body"]) != keys:
            found.append(f"entry {index} ({entry['kind']}) has body keys {sorted(entry['body'])}")
        if entry["kind"] == "execution" and set(entry["body"]["request"]) != REQUEST_KEYS:
            found.append(f"entry {index} execution request has keys {sorted(entry['body']['request'])}")
    if not entries or entries[0]["kind"] != "run":
        found.append("entry 0 is not the run entry")

    by_turn: dict[int, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry["turn"] is not None:
            by_turn.setdefault(entry["turn"], []).append(entry)
    if sorted(by_turn) != list(range(len(by_turn))):
        found.append(f"turn numbers are not 0..{len(by_turn) - 1}: {sorted(by_turn)}")
    records: list[TurnRecord] = []
    previous_version = -1
    for number in sorted(by_turn):
        group = by_turn[number]
        tokens = "".join(_token(e) for e in group)
        if not tokens.endswith("T"):
            found.append(f"turn {number} stops before its turn entry: {tokens}")
            continue
        if not ORDER.match(tokens):
            found.append(f"turn {number} has entries in the order {tokens}")
            continue
        try:
            found += _turn(number, group, previous_version, records)
            previous_version = ChoiceSet.model_validate(_one(group, "choice_set")["body"]).set_version
        except (KeyError, TypeError, ValueError) as exc:
            found.append(f"turn {number} has a malformed entry: {type(exc).__name__}: {exc}")
    found += [f"adherence: turn {v.turn}: {v.problem}" for v in check_adherence(records)]
    found += _help_lifecycle(entries)
    return found


def _token(entry: dict[str, Any]) -> str:
    if entry["kind"] == "help":
        return "A" if entry["body"]["event"] == "adopted" else "H"
    if entry["kind"] == "investigation":
        return ""
    return TOKENS[entry["kind"]]


def _one(group: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(e for e in group if e["kind"] == kind)


def _all(group: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in group if e["kind"] == kind]


def _turn(number: int, group: list[dict[str, Any]], previous_version: int, records: list[TurnRecord]) -> list[str]:
    found: list[str] = []
    obs, offered = _one(group, "observation"), _one(group, "choice_set")
    context, decision = _one(group, "context"), _one(group, "decision")
    turn_entry = _one(group, "turn")
    calls, executions = _all(group, "s1_call"), _all(group, "execution")
    refs = {
        "observation": ([], obs), "choice_set": ([obs["trace_seq"]], offered),
        "context": ([obs["trace_seq"], offered["trace_seq"]], context),
        "decision": ([offered["trace_seq"], *[c["trace_seq"] for c in calls]], decision),
    }
    for name, (expected, entry) in refs.items():
        if entry["refs"] != expected:
            found.append(f"turn {number}: {name} refs {entry['refs']}, expected {expected}")
    for call in calls:
        if call["refs"] != [context["trace_seq"]]:
            found.append(f"turn {number}: s1_call refs {call['refs']}")
    for execution in executions:
        if execution["refs"] != [decision["trace_seq"]]:
            found.append(f"turn {number}: execution refs {execution['refs']}")
    for effect in _all(group, "effect"):
        owner = group[group.index(effect) - 1]
        if owner["kind"] != "execution" or effect["refs"] != [owner["trace_seq"]]:
            found.append(f"turn {number}: effect does not follow and cite its execution")
    for event in _all(group, "help"):
        if event["body"]["event"] in ("opened", "repeat") and event["refs"] != [decision["trace_seq"]]:
            found.append(f"turn {number}: help {event['body']['event']} refs {event['refs']}")
    expected_turn_refs = [obs["trace_seq"], offered["trace_seq"], context["trace_seq"], decision["trace_seq"],
                          *[e["trace_seq"] for e in executions]]
    if turn_entry["refs"] != expected_turn_refs:
        found.append(f"turn {number}: turn refs {turn_entry['refs']}, expected {expected_turn_refs}")

    choice_set = ChoiceSet.model_validate(offered["body"])
    if choice_set.built_from_seq != obs["body"]["seq"]:
        found.append(f"turn {number}: the choice set is not bound to its observation")
    if choice_set.set_version <= previous_version:
        found.append(f"turn {number}: set_version {choice_set.set_version} did not increase")

    payload = context["body"]["payload"]
    if hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest() != context["body"]["sha256"]:
        found.append(f"turn {number}: the context hash does not match its payload")
    if payload["as_of"]["seq"] != obs["body"]["seq"] or payload["choice_set_version"] != choice_set.set_version:
        found.append(f"turn {number}: the payload is not the turn's observation and set")
    if payload["choices"] != [c.model_dump(mode="json", exclude_none=True) for c in choice_set.choices]:
        found.append(f"turn {number}: the payload's choices differ from the set")
    if "pending_help" not in payload:
        found.append(f"turn {number}: the payload has no pending_help key")

    body = decision["body"]
    parsed = parse_decision(body["answer"], choice_set, ClockMode(body["clock_mode"]))
    again = parsed.decision.model_dump(mode="json") if parsed.decision is not None else None
    if (parsed.valid, parsed.error, parsed.policy_default, again) != (
            body["valid"], body["error"], body["policy_default"], body["decision"]):
        found.append(f"turn {number}: parse_decision does not reproduce the decision entry")

    record = TurnRecord.model_validate(turn_entry["body"])
    records.append(record)
    answer = body["answer"]
    raw = answer["choice_id"] if isinstance(answer, dict) and isinstance(answer.get("choice_id"), str) else None
    sent = [e["body"]["request"]["choice_id"] for e in executions
            if e["body"]["records"] and e["body"]["records"][-1]["status"] != "rejected"]
    selected = choice_set.get(parsed.decision.choice_id) if parsed.decision is not None else None
    expected_fields = {
        "set_version": choice_set.set_version, "based_on_seq": obs["body"]["seq"], "raw_choice": raw,
        "effective_choice": selected.id if selected is not None else None,
        "executed_action": sent[0] if sent else None,
    }
    for name, expected in expected_fields.items():
        if getattr(record, name) != expected:
            found.append(f"turn {number}: {name} is {getattr(record, name)!r}, entries say {expected!r}")
    if selected is None:
        expected_outcome = "rejected_invalid"
    elif selected.kind in (ChoiceKind.action, ChoiceKind.skill):
        expected_outcome = "executed" if sent else "rejected_stale"
    elif selected.kind is ChoiceKind.escalate:
        expected_outcome = "escalated"
    elif selected.kind is ChoiceKind.delegate:
        expected_outcome = "delegated"
    else:
        expected_outcome = "finished"
    if record.outcome.value != expected_outcome:
        found.append(f"turn {number}: outcome {record.outcome.value}, entries say {expected_outcome}")
    return found


def _help_lifecycle(entries: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    open_entry: dict[str, Any] | None = None
    closed: set[int] = set()
    for entry in entries:
        if entry["kind"] != "help":
            continue
        event = entry["body"]["event"]
        if event == "opened":
            if open_entry is not None:
                found.append(f"entry {entry['trace_seq']} opens a request while one is open")
            open_entry = entry
        elif open_entry is None:
            found.append(f"entry {entry['trace_seq']} ({event}) has no open request")
        elif event == "repeat":
            if entry["body"]["opened_turn"] != open_entry["turn"]:
                found.append(f"entry {entry['trace_seq']} repeats a request that was opened in another turn")
        else:
            if entry["refs"] != [open_entry["trace_seq"]] or open_entry["trace_seq"] in closed:
                found.append(f"entry {entry['trace_seq']} ({event}) does not close the open request once")
            closed.add(open_entry["trace_seq"])
            open_entry = None
    return found
