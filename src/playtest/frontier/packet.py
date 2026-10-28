"""One frontier escalation packet: what S1 saw, offered and chose (#33).

The packet is assembled from records the loop already measured: the observation the choice set was
bound to, the full choice set, the escalation turn, facts, recent records, hypotheses and the live
directive. Nothing is ranked, recommended or replaced, and S1's rationale and distribution are not
part of it (build bible section 6; S1 ownership rules 1-3).

Citable sequence numbers come only from explicit fields: the observation, a fact's `seqs`, and a
recent row's `seq`. Numbers nested elsewhere are evidence for a reader, not citations the frontier
may use. Missing ages and values stay `null`; they are never filled with zero.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import Directive, DirectiveOutcome
from playtest.contracts.loop import ChoiceKind, ChoiceSet, TurnOutcome, TurnRecord
from playtest.core.context import canonical
from playtest.roles import frontier

HYPOTHESIS_STATUSES = frozenset({"open", "supported", "contradicted", "withdrawn"})

_RATIONALE_KEYS = frozenset({"reason", "distribution"})
_RECORD_KEYS = frozenset({"turn", "decision"})

_PATH_KEYS = frozenset({"path", "file_path", "local_path", "absolute_path"})
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\|file:)", re.IGNORECASE)
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")

_ESCALATION_FIELDS = (
    "turn",
    "set_version",
    "based_on_seq",
    "raw_choice",
    "effective_choice",
    "executed_action",
    "policy_default",
    "latency_ms",
)


@dataclass(frozen=True)
class Packet:
    """A serialized escalation payload and the references it makes citable."""

    payload: dict[str, Any]
    refs: frontier.PacketRefs

    def canonical(self) -> str:
        """The exact text this packet contributes to a request; stable for equal payloads."""
        return canonical(self.payload)


def build_packet(
    *,
    objective: str,
    observation: Observation,
    choices: ChoiceSet,
    escalation: TurnRecord,
    facts: Sequence[dict[str, Any]] = (),
    recent: Sequence[dict[str, Any]] = (),
    directive: Directive | None = None,
    directive_history: Sequence[Directive] = (),
    hypotheses: Sequence[dict[str, Any]] = (),
    new_hypothesis_ids: Sequence[str] = (),
    age_s: float | None = None,
    clip: dict[str, Any] | None = None,
    clip_unavailable_reason: str | None = None,
) -> Packet:
    """Build one packet without changing what S1 saw or chose.

    Raises `ValueError` when the records do not share one binding, when the escalation turn did not
    select an escalate choice, when a record is malformed, or when the result is not finite JSON.
    """
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("the objective is empty")
    _check_binding(observation, choices, escalation)

    age = _age(age_s)
    fact_rows = [_fact(fact, i) for i, fact in enumerate(facts)]
    recent_rows = [_recent(row, i) for i, row in enumerate(recent)]
    hypothesis_rows = _hypotheses(hypotheses)
    new_ids = _new_ids(new_hypothesis_ids)
    directive_row = _directive(directive, where="directive")
    history_rows = [_directive(item, where=f"directive_history[{i}]") for i, item in enumerate(directive_history)]
    clip_row, reason = _clip(clip, clip_unavailable_reason)
    escalation_row = _escalation(escalation)

    payload: dict[str, Any] = {
        "objective": objective,
        "as_of": {
            "seq": observation.seq,
            "game_s": observation.game_s,
            "age_s": age,
        },
        "observation": {
            "seq": observation.seq,
            "game_s": observation.game_s,
            "clock_mode": observation.clock_mode.value,
            "scene": observation.scene,
            "playing": observation.playing,
            "paused": observation.paused,
            "time_scale": observation.time_scale,
            "frame": observation.frame,
            "public": copy.deepcopy(observation.public),
            "ui": copy.deepcopy(observation.ui),
            "reader_errors": [error.model_dump(mode="json") for error in observation.reader_errors],
        },
        "escalation": escalation_row,
        "choice_set_version": choices.set_version,
        "choices": [choice.model_dump(mode="json", exclude_none=True) for choice in choices.choices],
        "directive": directive_row,
        "directive_history": history_rows,
        "facts": fact_rows,
        "recent": recent_rows,
        "hypotheses": hypothesis_rows,
        "new_hypothesis_ids": new_ids,
        "clip": clip_row,
        "clip_unavailable_reason": reason,
    }

    seqs = {observation.seq}
    for fact in fact_rows:
        seqs.update(fact["seqs"])
    for row in recent_rows:
        seqs.add(row["seq"])
    refs = frontier.PacketRefs(
        seqs=frozenset(seqs),
        hypotheses=frozenset(row["id"] for row in hypothesis_rows if row["status"] == "open"),
        new_hypothesis_ids=tuple(new_ids),
        open_directive=directive is not None and directive.outcome is DirectiveOutcome.open,
    )
    packet = Packet(payload=payload, refs=refs)
    try:
        packet.canonical()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"the packet is not finite JSON: {exc}") from None
    return packet


# --- binding checks ------------------------------------------------------------------------------


def _check_binding(observation: Observation, choices: ChoiceSet, escalation: TurnRecord) -> None:
    if choices.built_from_seq != observation.seq:
        raise ValueError(
            f"choices are bound to seq {choices.built_from_seq}, not the observation's {observation.seq}"
        )
    if escalation.based_on_seq != observation.seq:
        raise ValueError(
            f"the escalation turn is based on seq {escalation.based_on_seq}, not the observation's {observation.seq}"
        )
    if escalation.set_version != choices.set_version:
        raise ValueError(
            f"the escalation turn used choice set version {escalation.set_version}, "
            f"not the supplied version {choices.set_version}"
        )
    if escalation.outcome is TurnOutcome.delegated:
        raise ValueError("a delegated turn is not a frontier escalation")
    if escalation.outcome is not TurnOutcome.escalated:
        raise ValueError(f"the escalation turn outcome is {escalation.outcome.value!r}, not 'escalated'")
    if escalation.raw_choice is None:
        raise ValueError("the escalation turn has no raw choice")
    chosen = choices.get(escalation.raw_choice)
    if chosen is None:
        raise ValueError(f"the escalation choice {escalation.raw_choice!r} is not in the choice set")
    if chosen.kind is not ChoiceKind.escalate:
        raise ValueError(f"the raw choice {chosen.id!r} is {chosen.kind.value!r}, not 'escalate'")
    if escalation.effective_choice is not None and escalation.effective_choice != chosen.id:
        raise ValueError(
            f"the escalation turn acted on {escalation.effective_choice!r}, not the selected {chosen.id!r}"
        )


# --- record normalization ------------------------------------------------------------------------


def _age(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("the observation age must be a number or null")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("the observation age must be finite and non-negative")
    return number


def _fact(fact: Mapping[str, Any], index: int) -> dict[str, Any]:
    where = f"facts[{index}]"
    if not isinstance(fact, Mapping):
        raise ValueError(f"{where} must be an object")
    if set(fact) != {"text", "seqs"}:
        raise ValueError(f"{where} must carry exactly 'text' and 'seqs'")
    text = fact["text"]
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{where}.text is empty")
    return {"text": text, "seqs": _seqs(fact["seqs"], f"{where}.seqs")}


def _recent(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    where = f"recent[{index}]"
    if not isinstance(row, Mapping):
        raise ValueError(f"{where} must be an object")
    if "seq" not in row:
        raise ValueError(f"{where} needs an explicit seq")
    _seq(row["seq"], f"{where}.seq")
    copied = copy.deepcopy(dict(row))
    _no_rationale(copied, where)
    return copied


def _hypotheses(hypotheses: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, row in enumerate(hypotheses):
        where = f"hypotheses[{i}]"
        if not isinstance(row, Mapping):
            raise ValueError(f"{where} must be an object")
        hypothesis_id = row.get("id")
        if not isinstance(hypothesis_id, str) or not hypothesis_id.strip():
            raise ValueError(f"{where}.id is empty")
        if hypothesis_id in seen:
            raise ValueError(f"{where}.id {hypothesis_id!r} appears twice")
        status = row.get("status")
        if status not in HYPOTHESIS_STATUSES:
            raise ValueError(f"{where}.status {status!r} is not one of {sorted(HYPOTHESIS_STATUSES)}")
        rows.append(copy.deepcopy(dict(row)))
        seen.add(hypothesis_id)
    return rows


def _new_ids(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for i, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"new_hypothesis_ids[{i}] is empty")
        if value in seen:
            raise ValueError(f"new_hypothesis_ids[{i}] {value!r} appears twice")
        out.append(value)
        seen.add(value)
    return out


def _directive(value: Directive | None, *, where: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Directive):
        raise ValueError(f"{where} must be a Directive record")
    # Every field, including a null deadline or missing evidence and its outcome, stays explicit.
    return value.model_dump(mode="json")


def _escalation(record: TurnRecord) -> dict[str, Any]:
    out = {name: getattr(record, name) for name in _ESCALATION_FIELDS}
    out["outcome"] = record.outcome.value
    pending = record.while_pending
    out["while_pending"] = None if pending is None else pending.model_dump(mode="json", exclude_none=True)
    return out


def _clip(clip: dict[str, Any] | None, reason: str | None) -> tuple[dict[str, Any] | None, str | None]:
    if clip is not None and reason is not None:
        raise ValueError("a clip descriptor and a clip-unavailable reason cannot both be present")
    if clip is None:
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("clip_unavailable_reason is empty")
        return None, reason
    if not isinstance(clip, Mapping):
        raise ValueError("the clip descriptor must be an object")
    row = copy.deepcopy(dict(clip))
    _no_local_path(row, "clip")
    digest = row.get("sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("clip.sha256 must be a 64-character hex digest")
    size = row.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("clip.bytes must be a positive integer")
    if "mime" in row and row["mime"] != "video/mp4":
        raise ValueError("the clip descriptor must describe an MP4")
    return row, None


# --- helpers -------------------------------------------------------------------------------------


def _seq(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{where} must be a non-negative integer")
    return value


def _seqs(value: Any, where: str) -> list[int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{where} must be a list of sequence numbers")
    return [_seq(item, f"{where}[{i}]") for i, item in enumerate(value)]


def _no_rationale(node: Any, where: str) -> None:
    """S1's reason and distribution are record-only; record rows never carry them.

    Only the row itself and nested turn/decision records are inspected. A measured public field
    that happens to be named `reason` is data, not S1's rationale.
    """
    if not isinstance(node, Mapping):
        return
    for key, value in node.items():
        if key in _RATIONALE_KEYS:
            raise ValueError(f"{where} must not carry S1 {key!r}")
        if key in _RECORD_KEYS and isinstance(value, Mapping):
            _no_rationale(value, where)


def _no_local_path(node: Any, where: str) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in _PATH_KEYS:
                raise ValueError(f"{where} must not carry a filesystem path")
            _no_local_path(value, where)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _no_local_path(item, where)
    elif isinstance(node, str) and _ABSOLUTE_PATH.match(node):
        raise ValueError(f"{where} must not carry a local absolute path")
