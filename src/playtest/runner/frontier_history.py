"""Forward the history S1 actually saw, with observation and trace citations kept distinct."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any


def recent_records(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Use the exact bounded windows; do not re-select records or infer missing evidence.

    A packet's citable ``seq`` is a bridge observation sequence. ``trace_seq`` and
    ``effect_trace_seq`` remain trace references and are never recast as observations.
    An action row is anchored at its decision observation; effect fields retain their
    own recorded measurement references and are not presented as that observation's values.
    """
    history = context.get("recent_history")
    if history is None:
        return []
    rows: list[dict[str, Any]] = []
    windows: dict[str, Any] = {}
    for name in ("turns", "action_attempts"):
        window = history[name]
        windows[name] = {key: copy.deepcopy(window[key]) for key in ("limit", "total", "omitted")}
        for item in window["items"]:
            seq = item["observation_seq"]
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise ValueError("history needs a measured bridge observation sequence")
            rows.append({**copy.deepcopy(item), "seq": seq, "record_kind": name})
    rows.sort(key=lambda row: (row["turn"], 0 if row["record_kind"] == "action_attempts" else 1))
    summary = {"seq": context["as_of"]["seq"], "record_kind": "history_window",
               "epoch": copy.deepcopy(history["epoch"]), "boundary": copy.deepcopy(history["boundary"]),
               "windows": windows}
    return [summary, *rows]
