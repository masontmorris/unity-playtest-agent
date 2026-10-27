"""Measure which scene object responds to discovered keyboard controls.

This is an active preflight probe, not a gameplay policy. It uses the bridge's
ordinary observe, act, inspect and stop operations and retains the measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError


KEY_PATH = re.compile(r"^<Keyboard>/([a-zA-Z][a-zA-Z0-9]*)$", re.IGNORECASE)
MAX_CONTROLS = 32
MEASUREMENT_BUDGET_S = 150
LEASE_S = 0.4
IDLE_INTERVAL_S = 0.1
IDLE_INTERVALS = 4
SETTLE_S = 0.5
IDLE_TOLERANCE = 0.01
MIN_RESPONSE = 0.03
MIN_DIRECTION_COSINE = 0.8
MIN_WINDOW_RATIO = 0.5
MAX_WINDOW_RATIO = 2.0
VIEWPOINT_MATCH_ABSOLUTE = 0.03
VIEWPOINT_MATCH_RELATIVE = 0.02


def candidate_controls(discovery: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Find keyboard paths from vector-valued bindings; preserve unsupported evidence."""
    controls: dict[str, dict[str, Any]] = {}
    unsupported: list[dict[str, Any]] = []
    for section in ("bindings", "input_asset_bindings"):
        for binding in discovery.get(section, []):
            if binding.get("expected_control_type") != "Vector2" or binding.get("binding_is_composite"):
                continue
            path = binding.get("binding_effective_path")
            match = KEY_PATH.fullmatch(path) if isinstance(path, str) else None
            if not match:
                unsupported.append({"binding_id": binding.get("id"), "path": path})
                continue
            key = match.group(1).lower()
            entry = controls.setdefault(key, {"control": key, "action": "preflight_key_" + key,
                                              "binding_ids": [], "source_object_ids": []})
            entry["binding_ids"].append(binding.get("id"))
            source = binding.get("source_object_id")
            if isinstance(source, str) and source not in entry["source_object_ids"]:
                entry["source_object_ids"].append(source)
    return [controls[k] for k in sorted(controls)], unsupported


def _vector(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    if any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in value):
        return None
    return float(value[0]), float(value[1]), float(value[2])


def _sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(x - y for x, y in zip(a, b))  # type: ignore[return-value]


def _norm(v: tuple[float, float, float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _objects(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = snapshot.get("game_objects")
    if not isinstance(rows, list):
        raise ValueError("transform snapshot has no objects list")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in result:
            raise ValueError("transform snapshot contains an invalid or duplicate object id")
        if "parent_id" not in row or (row["parent_id"] is not None and not isinstance(row["parent_id"], str)):
            raise ValueError("transform snapshot contains an unknown parent")
        if _vector(row.get("position")) is None:
            raise ValueError("transform snapshot contains an unknown position")
        result[row["id"]] = row
    return result


def analyze(trials: list[dict[str, Any]]) -> dict[str, Any]:
    """Require two repeatable responses and a quiet sampled baseline per control.

    A responding descendant of another responding object is grouped with its
    ancestor. Independent responding roots are reported as ambiguous.
    """
    by_control: dict[str, list[dict[str, Any]]] = {}
    for trial in trials:
        by_control.setdefault(trial["control"], []).append(trial)

    evidence: dict[str, dict[str, Any]] = {}
    parents: dict[str, str | None] = {}
    all_ids: set[str] | None = None
    for control, attempts in sorted(by_control.items()):
        if len(attempts) != 2:
            raise ValueError("each control requires exactly two trials")
        per_attempt = []
        for trial in attempts:
            if len(trial["idle"]) != IDLE_INTERVALS + 1:
                raise ValueError("an idle baseline is incomplete")
            idle_times = [s["real_time_s"] for s in trial["idle"]]
            if any(b <= a for a, b in zip(idle_times, idle_times[1:])):
                raise ValueError("idle samples do not advance in time")
            idle_elapsed = idle_times[-1] - idle_times[0]
            action_elapsed = trial["after"]["real_time_s"] - trial["before"]["real_time_s"]
            if action_elapsed <= 0 or not MIN_WINDOW_RATIO <= idle_elapsed / action_elapsed <= MAX_WINDOW_RATIO:
                raise ValueError("idle and action windows are not comparable")
            frames = [_objects(s) for s in trial["idle"] + [trial["before"], trial["after"]]]
            ids = set(frames[0])
            if any(set(frame) != ids for frame in frames[1:]):
                raise ValueError("the object set changed during a trial")
            if all_ids is not None and ids != all_ids:
                raise ValueError("the object set changed between trials")
            all_ids = ids
            measurements = {}
            for object_id in ids:
                rows = [frame[object_id] for frame in frames]
                parent = rows[0].get("parent_id")
                if any(row.get("parent_id") != parent for row in rows):
                    raise ValueError("an object changed parent during a trial")
                if object_id in parents and parents[object_id] != parent:
                    raise ValueError("an object changed parent between trials")
                parents[object_id] = parent
                positions = [_vector(row["position"]) for row in rows]
                start = positions[0]
                assert start is not None and all(p is not None for p in positions)
                idle_peak = max(_norm(_sub(p, start)) for p in positions[1 : IDLE_INTERVALS + 1])  # type: ignore[arg-type]
                delta = _sub(positions[-1], positions[-2])  # type: ignore[arg-type]
                measurements[object_id] = {"idle_peak": idle_peak, "delta": delta, "distance": _norm(delta)}
            per_attempt.append(measurements)
        if set(per_attempt[0]) != set(per_attempt[1]):
            raise ValueError("the object set changed between trials")
        for object_id in per_attempt[0]:
            first, second = (sample[object_id] for sample in per_attempt)
            a, b = first["delta"], second["delta"]
            magnitude = first["distance"] * second["distance"]
            cosine = sum(x * y for x, y in zip(a, b)) / magnitude if magnitude else None
            quiet = first["idle_peak"] <= IDLE_TOLERANCE and second["idle_peak"] <= IDLE_TOLERANCE
            responsive = (
                quiet and first["distance"] >= MIN_RESPONSE and second["distance"] >= MIN_RESPONSE
                and cosine is not None and cosine >= MIN_DIRECTION_COSINE
            )
            evidence.setdefault(object_id, {})[control] = {
                "trials": [first, second], "direction_cosine": cosine,
                "idle_quiet": quiet, "responsive": responsive,
            }

    # Movement in any observed idle window disqualifies the object globally, not
    # just for that control: it is known to move without our input.
    responsive_ids = {
        key for key, values in evidence.items()
        if all(v["idle_quiet"] for v in values.values()) and any(v["responsive"] for v in values.values())
    }
    roots = set(responsive_ids)
    for object_id in responsive_ids:
        visited = {object_id}
        parent = parents.get(object_id)
        while parent is not None:
            if parent in visited:
                raise ValueError("object hierarchy contains a cycle")
            visited.add(parent)
            if parent in responsive_ids:
                roots.discard(object_id)
                break
            parent = parents.get(parent)
    result = "identified" if len(roots) == 1 else "ambiguous" if roots else "inconclusive"
    return {"status": result, "entity_id": next(iter(roots)) if len(roots) == 1 else None,
            "independent_candidates": sorted(roots), "objects": evidence}


def resolve_viewpoints(analysis: dict[str, Any], discovery: dict[str, Any],
                       candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Recognize camera viewpoints that track an input-bound, measured object.

    Motion correlation alone does not prove a hierarchy. Only camera components
    without their own discovered input or physical body can be classified this
    way, and they must match an input-bound responder in two non-parallel
    directions. Every original responsive root remains in the output.
    """
    roots = set(analysis["independent_candidates"])
    raw_roots = sorted(roots)
    parents = {o["id"]: o.get("parent_id") for o in discovery.get("game_objects", [])}
    components: dict[str, set[str]] = {}
    for component in discovery.get("components", []):
        if isinstance(component.get("object_id"), str) and isinstance(component.get("type"), str):
            components.setdefault(component["object_id"], set()).add(component["type"])

    def contains(root: str, child: str) -> bool:
        seen = set()
        while child is not None and child not in seen:
            if child == root:
                return True
            seen.add(child)
            child = parents.get(child)
        return False

    sources = {source for candidate in candidates for source in candidate.get("source_object_ids", [])}
    all_binding_sources = {
        binding["source_object_id"]
        for section in ("bindings", "input_asset_bindings")
        for binding in discovery.get(section, [])
        if isinstance(binding.get("source_object_id"), str)
    }
    input_roots = {root for root in roots if any(contains(root, source) for source in sources)}
    body_types = {"UnityEngine.CharacterController", "UnityEngine.Rigidbody", "UnityEngine.Rigidbody2D"}
    matches = []
    for viewpoint in sorted(roots):
        types = components.get(viewpoint, set())
        descendants = {child for child in parents if contains(viewpoint, child)}
        descendant_types = {kind for child in descendants for kind in components.get(child, set())}
        has_body = bool(body_types & descendant_types)
        has_input = any(contains(viewpoint, source) for source in all_binding_sources | sources)
        if "UnityEngine.Camera" not in types or has_body or has_input:
            continue
        compatible = []
        for anchor in sorted(input_roots - {viewpoint}):
            view_measurements = analysis["objects"][viewpoint]
            body_measurements = analysis["objects"][anchor]
            if set(view_measurements) != set(body_measurements):
                continue
            differences = []
            directions = []
            for control in sorted(body_measurements):
                body = body_measurements[control]
                view = view_measurements[control]
                owned = any(candidate["control"] == control and
                            any(contains(anchor, source) for source in candidate.get("source_object_ids", []))
                            for candidate in candidates)
                for b, v in zip(body["trials"], view["trials"]):
                    difference = _norm(_sub(tuple(b["delta"]), tuple(v["delta"])))
                    tolerance = max(VIEWPOINT_MATCH_ABSOLUTE,
                                    VIEWPOINT_MATCH_RELATIVE * max(b["distance"], v["distance"]))
                    differences.append({"control": control, "difference": difference, "tolerance": tolerance})
                if owned and body["responsive"] and view["responsive"]:
                    directions.append(tuple(body["trials"][0]["delta"]))
            non_parallel = any(
                abs(sum(x * y for x, y in zip(a, b)) / (_norm(a) * _norm(b))) <= 0.9
                for i, a in enumerate(directions) for b in directions[i + 1 :]
                if _norm(a) > 0 and _norm(b) > 0
            )
            if differences and all(row["difference"] <= row["tolerance"] for row in differences) and non_parallel:
                compatible.append({"viewpoint_id": viewpoint, "entity_id": anchor,
                                   "viewpoint_component_types": sorted(types),
                                   "viewpoint_descendant_component_types": sorted(descendant_types),
                                   "displacement_comparisons": differences})
        if len(compatible) == 1:
            roots.remove(viewpoint)
            matches.append(compatible[0])
    status = "identified" if len(roots) == 1 else "ambiguous" if roots else "inconclusive"
    return {"status": status, "entity_id": next(iter(roots)) if len(roots) == 1 else None,
            "independent_candidates": sorted(roots), "raw_responsive_roots": raw_roots,
            "viewpoint_matches": matches}


def _complete(snapshot: Any, stage: str) -> dict[str, Any]:
    if snapshot.stage != stage or snapshot.errors:
        raise ValueError(f"{stage} inspection has errors or the wrong stage")
    required = (("game_objects", "components", "bindings", "input_asset_bindings")
                if stage == "discovery" else ("game_objects",))
    for name in required:
        cap = snapshot.caps.get(name)
        if cap is None or cap.truncated is not False or cap.observed is None:
            raise ValueError(f"{stage} inspection has incomplete {name} coverage")
    if stage == "transforms":
        data = snapshot.data
        if (data.get("position_space") != "world" or data.get("position_unit") != "unity_world_unit"
                or data.get("position_order") != ["x", "y", "z"] or data.get("time_unit") != "seconds"):
            raise ValueError("transform coordinates or units are unknown")
        frame = data.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
            raise ValueError("transform frame is unknown")
        for name in ("real_time_s", "game_time_s"):
            value = data.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"transform {name} is unknown")
    data = dict(snapshot.data)
    data["inspection_coverage"] = {
        name: {"limit": cap.limit, "observed": cap.observed, "returned": cap.returned,
               "truncated": cap.truncated}
        for name, cap in snapshot.caps.items()
    }
    return data


async def measure(client: BridgeClient) -> dict[str, Any]:
    discovery = _complete(await client.inspect("discovery"), "discovery")
    candidates, unsupported = candidate_controls(discovery)
    discovered_objects = discovery.get("game_objects")
    if not isinstance(discovered_objects, list) or any(
        not isinstance(o, dict) or not isinstance(o.get("id"), str) for o in discovered_objects
    ):
        raise ValueError("discovery has invalid object ids")
    discovered_ids = {o["id"] for o in discovered_objects}
    if len(discovered_ids) != len(discovered_objects):
        raise ValueError("discovery has duplicate object ids")

    async def sample() -> dict[str, Any]:
        snapshot = _complete(await client.inspect("transforms"), "transforms")
        if set(_objects(snapshot)) != discovered_ids:
            raise ValueError("the object set changed after discovery")
        return snapshot

    cap = {"limit": MAX_CONTROLS, "observed": len(candidates),
           "returned": min(len(candidates), MAX_CONTROLS), "truncated": len(candidates) > MAX_CONTROLS}
    result: dict[str, Any] = {
        "schema_version": 1, "coordinate_frame": "Unity world space",
        "axis_order": ["x", "y", "z"], "units": "Unity world units", "physical_unit": None,
        "parameters": {"lease_s": LEASE_S, "idle_intervals": IDLE_INTERVALS,
                       "idle_interval_s": IDLE_INTERVAL_S, "settle_s": SETTLE_S,
                       "measurement_budget_s": MEASUREMENT_BUDGET_S,
                       "idle_tolerance": IDLE_TOLERANCE,
                       "minimum_response": MIN_RESPONSE, "minimum_direction_cosine": MIN_DIRECTION_COSINE,
                       "idle_to_action_window_ratio": [MIN_WINDOW_RATIO, MAX_WINDOW_RATIO],
                       "viewpoint_match_absolute": VIEWPOINT_MATCH_ABSOLUTE,
                       "viewpoint_match_relative": VIEWPOINT_MATCH_RELATIVE},
        "candidate_cap": cap, "candidates": candidates[:MAX_CONTROLS],
        "discovery_coverage": discovery["inspection_coverage"],
        "discovery_object_ids": sorted(discovered_ids),
        "unsupported_vector_bindings": unsupported, "trials": [],
        "status": "inconclusive", "entity_id": None, "independent_candidates": [], "error": None,
    }
    if cap["truncated"] or not candidates:
        result["error"] = "candidate list is incomplete" if cap["truncated"] else "no supported candidate controls"
        return result
    try:
        async with asyncio.timeout(MEASUREMENT_BUDGET_S):
            await asyncio.sleep(0.75)
            for candidate in candidates:
                for attempt in range(2):
                    idle = [await sample()]
                    for _ in range(IDLE_INTERVALS):
                        await asyncio.sleep(IDLE_INTERVAL_S)
                        idle.append(await sample())
                    before = await sample()
                    observation = await client.observe_once()
                    action_id = f"preflight-{candidate['control']}-{attempt + 1}"
                    trial = {"control": candidate["control"], "attempt": attempt + 1,
                             "idle": idle, "before": before, "after": None,
                             "observation_seq": observation.seq, "execution": None}
                    result["trials"].append(trial)
                    records = await client.act(action_id=action_id, choice_id=action_id,
                                               action=candidate["action"], based_on_seq=observation.seq,
                                               max_age_s=2.0, lease_s=LEASE_S)
                    terminal = records[-1]
                    trial["execution"] = [r.model_dump(mode="json") for r in records]
                    if terminal.status.value != "completed" or terminal.release_verified is not True:
                        raise ValueError(f"{action_id} did not complete with verified release")
                    trial["after"] = await sample()
                    await asyncio.sleep(SETTLE_S)
        result.update(analyze(result["trials"]))
        result.update(resolve_viewpoints(result, discovery, candidates))
        objects = {o["id"]: o for o in discovery.get("game_objects", [])}
        result["entity"] = objects.get(result["entity_id"]) if result["entity_id"] else None
        if result["entity_id"] and result["entity"] is None:
            raise ValueError("the measured entity is missing from discovery")
    except (BridgeError, ValueError) as exc:
        result.update(status="inconclusive", entity_id=None, independent_candidates=[], error=str(exc))
    except TimeoutError:
        result.update(status="inconclusive", entity_id=None, independent_candidates=[],
                      error="the measurement exceeded its wall-clock budget")
    return result


async def _run(project: Path) -> dict[str, Any]:
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    try:
        result = await measure(client)
    finally:
        stop = await client.aclose()
    result["stop"] = stop.model_dump(mode="json") if stop is not None else None
    if stop is None or stop.release_verified is not True:
        result.update(status="inconclusive", entity_id=None, entity=None, independent_candidates=[],
                      error="bridge shutdown did not verify input release")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new evidence file outside the Unity project")
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    if output.is_relative_to(project):
        parser.error("--output must be outside the Unity project")
    if output.exists():
        parser.error("--output already exists")
    try:
        result = asyncio.run(_run(project))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (BridgeError, BridgeLockError, OSError, ValueError) as exc:
        print(f"measurement failed: {exc}", file=sys.stderr)
        return 1
    print(f"measurement saved to {output}: {result['status']}")
    return 0 if result["status"] == "identified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
