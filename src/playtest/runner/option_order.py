from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any

from playtest.core.context import canonical
from playtest.runner.wire import render


class OptionOrder(str, Enum):
    sorted = "sorted"
    manifest = "manifest"
    shuffled = "shuffled"


def ordered_ids(criteria: dict[str, str], mode: OptionOrder, seed: int | None, episode: int, turn: int, question: str) -> list[str]:
    ids = list(criteria)
    if mode is OptionOrder.sorted:
        return sorted(ids)
    if mode is OptionOrder.shuffled:
        def key(choice: str) -> bytes:
            return hashlib.sha256(canonical([seed, episode, turn, question, choice]).encode('utf-8')).digest()
        return sorted(ids, key=lambda choice: (key(choice), choice))
    return ids


def serialize(body: dict[str, Any], orders: dict[str, list[str]]) -> str:
    """Canonical JSON except for the explicitly ordered question criteria maps (`playtest.runner.wire.render`)."""
    return render(body, criteria=orders)
