"""Scene-bound object ids without the loaded-scene index.

The bridge names a scene `scene:<path>@<index>`, where the index is the scene's position among loaded scenes, and
every object, component, field and binding id under it starts with that name. A Play Mode test host loads its own
scene first, so a game scene is `@1` there and `@0` under the Editor-owned session host; the objects are the same.
These helpers compare ids with that index removed. A caller accepts such a match only when it is unique.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, TypeVar

_SCENE_INDEX = re.compile(r"^(scene:.+?)@\d+(?=::|$)")

T = TypeVar("T")


def without_scene_index(value: Any) -> Any:
    """The id with its loaded-scene index removed; anything that is not a scene-bound id is returned as is."""
    return _SCENE_INDEX.sub(r"\1", value) if isinstance(value, str) else value


def unique_by_scene_free_id(rows: Mapping[str, T], wanted: str) -> T | None:
    """The one row whose id equals `wanted` once both lose their scene index, or None when zero or several do."""
    key = without_scene_index(wanted)
    matches = [row for row_id, row in rows.items() if without_scene_index(row_id) == key]
    return matches[0] if len(matches) == 1 else None


def scene_free_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any] | None]:
    """Rows keyed by scene-free id; a key two rows share maps to None, so it never resolves."""
    out: dict[str, Mapping[str, Any] | None] = {}
    for row in rows:
        key = without_scene_index(row.get("id"))
        if isinstance(key, str):
            out[key] = None if key in out else row
    return out
