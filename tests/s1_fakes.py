"""Fakes for the System One adapter and the runner tests: approvals, scripted provider replies, a recording `send`."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

ARM = "mammoth-1"


def approval(calls: int = 12) -> dict[str, Any]:
    limit = {"max_calls": calls, "max_cost_usd": 0.01, "per_call_max_usd": 0.0001, "verified_balance_usd": 1,
             "balance_checked_at_utc": "2026-01-01T00:00:00Z", "price_valid_until_utc": "2099-01-01T00:00:00Z"}
    return {"approved_by": "Test User", "approved_at_utc": "2026-01-01T00:00:00Z", "max_calls": calls,
            "max_cost_usd": 0.01, "arms": {ARM: limit}}


def reply(question: str, choice: str, favoured: str | None = None, usage: dict[str, Any] | None = None):
    """A 200 reply that names `choice`; `favoured` carries the most probability, to show it never selects.

    The default usage has no `input_tokens`, as the early smoke replies had; `usage` replaces it.
    """
    def make(body: dict[str, Any]):
        criteria = body["questions"][question]["criteria"]
        top = favoured or choice
        probabilities = {key: (0.9 if key == top else 0.1 / (len(criteria) - 1)) for key in criteria}
        answer = {"type": "choice", "choice": choice, "probabilities": probabilities}
        reported = {"tokens": 5} if usage is None else usage
        return 200, {"model": "mammoth-1-resolved", "answers": {question: answer}, "usage": reported}, 12.0, None
    return make


class Send:
    """A fake `send`: scripted replies, with what the adapter did around each call recorded."""

    def __init__(self, directory: Path, *replies: Any) -> None:
        self._directory = directory
        self._replies = list(replies)
        self.bodies: list[dict[str, Any]] = []
        self.threads: list[int] = []
        self.reserved: list[list[str]] = []

    def __call__(self, arm: str, body: dict[str, Any]):
        self.bodies.append(body)
        self.threads.append(threading.get_ident())
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self._directory / "calls").glob("*.json"))]
        self.reserved.append([row["status"] for row in rows])
        step = self._replies.pop(0)
        return step(body) if callable(step) else step
