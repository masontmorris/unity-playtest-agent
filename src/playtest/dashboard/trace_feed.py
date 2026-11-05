"""Follow a session's append-only `trace.jsonl` and turn each entry into a small event for the live view.

A trace line is flushed whole, so a line without its newline yet is a partial tail: it is held until the rest
arrives, never parsed early. A line that does not parse is reported as a `corrupt` event, not skipped silently.
The bulky kinds (S1's full context, raw bridge execution records) are recognised from the line's envelope and not
parsed at all; the choice set is parsed only to keep each choice's label.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

KIND = re.compile(rb'"kind":\s*"([a-z_]+)"')
SKIPPED = frozenset({b"context", b"execution", b"effect"})
LABEL_LIMIT = 90
TEXT_LIMIT = 600


def short_label(label: str | None) -> str | None:
    """A choice label's lead clause: the text before its first ':' or ';', capped."""
    if not isinstance(label, str) or not label.strip():
        return None
    head = re.split(r"[:;]", label, maxsplit=1)[0].strip()
    return head if len(head) <= LABEL_LIMIT else head[: LABEL_LIMIT - 1] + "…"


def _text(value: Any, limit: int = TEXT_LIMIT) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hypotheses(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [{"id": item.get("id"), "statement": _text(item.get("statement")), "status": item.get("status")}
            for item in items if isinstance(item, dict)]


def _directive(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {"id": value.get("directive_id"), "text": _text(value.get("text")), "target": value.get("target"),
            "expected": value.get("expected")}


class TraceTail:
    """Reads only what was appended since the last call. Not thread-safe; one per follower."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.line_no = 0
        self.labels: dict[str, str] = {}
        self._partial = b""

    def read(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                if size < self.offset:  # the file was replaced: start again rather than read from the middle
                    self.offset, self.line_no, self._partial = 0, 0, b""
                handle.seek(self.offset)
                chunk = handle.read()
        except FileNotFoundError:
            return []
        if not chunk:
            return []
        self.offset += len(chunk)
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        events = []
        for raw in lines:
            self.line_no += 1
            if not raw.strip():
                continue
            event = self._event(raw)
            if event is not None:
                events.append(event)
        return events

    def _event(self, raw: bytes) -> dict[str, Any] | None:
        kind = KIND.search(raw[:600])
        if kind is not None and kind.group(1) in SKIPPED:
            return None
        try:
            entry = json.loads(raw)
            body = entry["body"]
            kind_name = entry["kind"]
        except (ValueError, KeyError, TypeError) as exc:
            return {"type": "corrupt", "line": self.line_no, "error": str(exc)[:200]}
        if kind_name == "choice_set":
            for choice in body.get("choices") or []:
                if isinstance(choice, dict) and choice.get("id"):
                    self.labels[choice["id"]] = short_label(choice.get("label")) or choice["id"]
            return None
        compact = compact_body(kind_name, body, self.labels)
        if compact is None:
            return None
        return {"type": kind_name, "seq": entry.get("trace_seq"), "turn": entry.get("turn"),
                "wall_utc": entry.get("wall_utc"), "author": entry.get("author"), **compact}


def compact_body(kind: str, body: Any, labels: dict[str, str]) -> dict[str, Any] | None:
    """The fields the live view shows for one trace entry; None for a kind it does not show."""
    if not isinstance(body, dict):
        return {"body": _text(body)}
    if kind == "run":
        return {"run_id": body.get("run_id"), "game_project": body.get("game_project"), "scene": body.get("scene"),
                "objective": _text(body.get("objective"), 2000), "clock_mode": body.get("clock_mode"),
                "budget": body.get("budget"),
                "models": [{"role": m.get("role"), "provider": m.get("provider"), "model": m.get("model")}
                           for m in body.get("models") or [] if isinstance(m, dict)]}
    if kind == "observation":
        return {"game_s": body.get("game_s"), "scene": body.get("scene"), "playing": body.get("playing"),
                "paused": body.get("paused")}
    if kind == "decision":
        decision = body.get("decision") or {}
        choice = decision.get("choice_id")
        distribution = decision.get("distribution") or {}
        probability = distribution.get(choice) if isinstance(distribution, dict) else None
        return {"choice_id": choice, "label": labels.get(choice) if choice else None,
                "probability": probability if isinstance(probability, (int, float)) else None,
                "reason": _text(decision.get("reason")), "source": body.get("source"),
                "condition": body.get("condition"), "valid": body.get("valid"), "error": _text(body.get("error")),
                "policy_default": body.get("policy_default"), "while_pending": decision.get("while_pending")}
    if kind == "turn":
        return {"raw_choice": body.get("raw_choice"), "effective_choice": body.get("effective_choice"),
                "executed_action": body.get("executed_action"), "outcome": body.get("outcome"),
                "label": labels.get(body.get("effective_choice") or ""),
                "latency_ms": body.get("latency_ms"), "while_pending": body.get("while_pending"),
                "policy_default": body.get("policy_default")}
    if kind == "s1_call":
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return {"question": body.get("question"), "model": body.get("model"), "http_status": body.get("http_status"),
                "latency_ms": body.get("latency_ms"), "problem": _text(body.get("problem")),
                "cost": usage.get("cost"), "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens")}
    if kind == "option":
        outcome = body.get("outcome") if isinstance(body.get("outcome"), dict) else {}
        return {"option_id": body.get("option_id"), "template": body.get("template"),
                "label": labels.get(body.get("option_id") or ""),
                "terminated_by": outcome.get("terminated_by"), "duration_s": outcome.get("duration_s")}
    if kind == "suspicion":
        return {key: body.get(key) for key in ("suspicion_id", "basis", "subject", "statement", "game_s")}
    if kind == "finding":
        return {"finding_id": body.get("finding_id"), "state": body.get("state"), "title": _text(body.get("title")),
                "severity": body.get("severity"), "expected": _text(body.get("expected")),
                "actual": _text(body.get("actual"))}
    if kind == "help":
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        return {"event": body.get("event"), "help_kind": body.get("kind"), "opened_turn": body.get("opened_turn"),
                "source": body.get("source"), "reason": _text(body.get("reason")),
                "hypotheses": _hypotheses(result.get("hypotheses")), "directive": _directive(result.get("directive"))}
    if kind == "investigation":
        return {"event": body.get("event"), "request_turn": body.get("request_turn"),
                "hypotheses": _hypotheses(body.get("hypothesis")), "directive": _directive(body.get("directive")),
                "keep": _text(body.get("keep")), "outcome": _text(body.get("outcome")),
                "reason": _text(body.get("reason"))}
    if kind == "restart":
        return {"body": _text(body)}
    if kind == "stop":
        return {"reason": body.get("reason"), "error": _text(body.get("error"))}
    if kind == "end":
        return {"status": body.get("status"), "error": _text(body.get("error")), "turns": body.get("turns")}
    return None
