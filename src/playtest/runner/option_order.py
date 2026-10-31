from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any

from playtest.core.context import canonical


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
    """Canonical JSON except for the explicitly ordered question criteria maps."""
    def encode(value: Any, path: tuple[str, ...]) -> str:
        if isinstance(value, dict):
            keys = sorted(value)
            if len(path) == 3 and path[0] == 'questions' and path[2] == 'criteria':
                keys = orders[path[1]]
                if len(keys) != len(value) or set(keys) != set(value):
                    raise ValueError('criteria order must contain each id exactly once')
            return '{' + ','.join(canonical(k) + ':' + encode(value[k], path + (k,)) for k in keys) + '}'
        if isinstance(value, (list, tuple)):
            return '[' + ','.join(encode(v, path + ('[]',)) for v in value) + ']'
        return canonical(value)
    return encode(body, ())
