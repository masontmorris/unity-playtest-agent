"""Read-only post-session review packet construction."""
from collections.abc import Mapping

from playtest.runner.declared import declared_configuration


def _round(value):
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value


def _reference(value):
    return isinstance(value, Mapping) and ("id" in value or "kind" in value)


def build_review_packet(trace_rows, manifest, discovery):
    observations = {row["turn"]: row["body"] for row in trace_rows if row["kind"] == "observation"}
    turns = {row["body"]["turn"]: row["body"] for row in trace_rows if row["kind"] == "turn"}
    effects = {row["turn"]: row["body"] for row in trace_rows if row["kind"] == "effect"}
    public = lambda turn: (observations.get(turn) or {}).get("public") or {}
    # A reference may be null in one observation and populated in another.
    reference_names = {key for obs in observations.values() for key, value in
                       (obs.get("public") or {}).items() if _reference(value)}
    actions = []
    for turn in sorted(turns):
        record = turns[turn]
        before, after = public(turn), public(turn + 1)
        if record["outcome"] != "executed" or not after:
            continue
        changed = {key: [_round(before.get(key)), _round(after.get(key))]
                   for key in sorted(set(before) | set(after))
                   if key not in {"probe", "camera"} and key not in reference_names
                   and before.get(key) != after.get(key)}
        position = (before.get("probe") or {}).get("anchor", {}).get("position")
        actions.append({"turn": turn, "choice": record["executed_action"] or record["effective_choice"],
                        "height_before": _round(position[1]) if position else None,
                        "references_set": sorted(key for key in reference_names if before.get(key) is not None),
                        "displacement": _round(((effects.get(turn) or {}).get("observed") or {}).get("net_displacement")),
                        "changed": changed})
    intents = {a["name"]: a["intent"] for a in manifest["actions"]} | {
        s["name"]: s["purpose"] for s in manifest["skills"]}
    return {"intents": intents, "declared_configuration": declared_configuration(discovery, manifest["entity_id"]),
            "actions": actions}
