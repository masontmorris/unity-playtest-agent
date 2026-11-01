"""Choose a preflight scene from completed per-scene entity measurements."""

from typing import Any


def select_scene(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Record every candidate; select the first with an identified controlled entity.

    The caller must measure every enabled scene in build order. Failed and missing
    measurements remain unknown, rather than evidence that a scene cannot work.
    """
    paths = [row.get("scene_path") for row in candidates]
    if any(not isinstance(p, str) or not p for p in paths) or len(set(paths)) != len(paths):
        raise ValueError("candidate scene paths must be unique nonempty strings")
    evidence = []
    for row in candidates:
        measurement = row.get("measurement")
        identified = (isinstance(measurement, dict)
                      and measurement.get("status") == "identified"
                      and isinstance(measurement.get("entity_id"), str)
                      and bool(measurement["entity_id"])
                      and isinstance(measurement.get("entity"), dict)
                      and measurement["entity"].get("scene_path") == row["scene_path"])
        evidence.append({"scene_path": row["scene_path"],
                         "status": "identified" if identified else "unknown",
                         "entity_id": measurement["entity_id"] if identified else None,
                         "measurement_path": row.get("measurement_path"),
                         "reason": None if identified else (
                             (measurement.get("error") or measurement.get("status") or "measurement unavailable") if isinstance(measurement, dict) else "measurement unavailable")})
    chosen = next((row for row in evidence if row["status"] == "identified"), None)
    return {"status": "selected" if chosen else "unknown",
            "scene_path": chosen["scene_path"] if chosen else None,
            "candidates": evidence}
