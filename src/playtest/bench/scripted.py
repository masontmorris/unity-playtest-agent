"""Drive a fixed, operator-authored hold script through a bridge host, with no model call (#192).

The turn loop cannot be steered everywhere a measurement is needed: session 10 reached the side staircases and
pressed a side face for 98 turns instead of walking round to the low end, so no run has yet touched a 0.25 riser.
This module is the operator's answer: the game is set up to start the controlled entity where the probe needs it,
and then the probe sends the same input holds a session would, in the order written.

It chooses nothing. Every hold is sent exactly once, bound to a fresh observation through the same `act` path a
session uses, and never retried or substituted. Around each hold it reads the entity's transform, so the report
states what moved and by how much: an unmeasured reading is `null`, never zero. A hold the game rejects or fails
ends the run as `incomplete` with its reason, and the exit code is non-zero, so a host never mistakes it for a
complete measurement. No game code, catalog or scene value is changed here, and no model is called.

The script comes from `UPA_SCRIPTED_PLAN`, because the bench host fixes the client's command line:

    {"schema_version": 1, "settle_s": 0.5, "max_age_s": 2.0,
     "holds": [{"action": "preflight_key_w", "lease_s": 0.4}, {"action": "preflight_key_w", "lease_s": 0.4}]}

`settle_s` is the pause after each hold's terminal record, before the closing observation and transform read;
`max_age_s` is the observation age the host may accept, as in a session.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError
from playtest.runner.probe import displacement_between, make_snapshot, read_snapshot

SCHEMA_VERSION = 1
KIND = "scripted_hold_probe"
DEFAULT_SETTLE_S = 0.5
DEFAULT_MAX_AGE_S = 2.0
PLAN_VARIABLE = "UPA_SCRIPTED_PLAN"

Snapshot = Callable[[], Awaitable[dict[str, Any]]]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def load_script(path: Path) -> tuple[dict[str, Any], str]:
    """The validated script and the SHA-256 of the bytes it was read from.

    Only what the probe uses is validated, and every field it relies on must be present and sound: a hold needs
    a nonempty action name and a positive lease. Unknown top-level keys are ignored so a script can carry an
    operator's own notes.
    """
    data = path.read_bytes()
    try:
        script = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the script is not valid JSON: {exc}") from exc
    if not isinstance(script, dict):
        raise ValueError("the script must be a JSON object")
    if script.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"the script's schema_version must be {SCHEMA_VERSION}")
    holds = script.get("holds")
    if not isinstance(holds, list) or not holds:
        raise ValueError("the script needs a nonempty holds list")
    cleaned = []
    for index, hold in enumerate(holds, 1):
        if not isinstance(hold, dict):
            raise ValueError(f"hold {index} must be an object")
        action = hold.get("action")
        if not isinstance(action, str) or not action.strip():
            raise ValueError(f"hold {index} needs a nonempty action name")
        lease = _number(hold.get("lease_s"), f"hold {index} lease_s")
        if lease <= 0:
            raise ValueError(f"hold {index} lease_s must be positive")
        cleaned.append({"action": action, "lease_s": lease})
    settle = _number(script.get("settle_s", DEFAULT_SETTLE_S), "settle_s")
    if settle < 0:
        raise ValueError("settle_s must not be negative")
    max_age = _number(script.get("max_age_s", DEFAULT_MAX_AGE_S), "max_age_s")
    if max_age <= 0:
        raise ValueError("max_age_s must be positive")
    return {"schema_version": SCHEMA_VERSION, "settle_s": settle, "max_age_s": max_age,
            "holds": cleaned}, _sha(data)


def _transform(reading: Mapping[str, Any] | None, problem: str | None) -> dict[str, Any]:
    """The transform reading a hold is measured between, with its position, heading and time, or why not."""
    if reading is None:
        return {"position": None, "yaw_deg": None, "game_time_s": None, "inspection_seq": None, "problem": problem}
    return {"position": reading.get("position"), "yaw_deg": reading.get("yaw_deg"),
            "game_time_s": reading.get("game_time_s"), "inspection_seq": reading.get("inspection_seq"),
            "problem": None}


def _view(observation: Any) -> dict[str, Any]:
    """What the observation published: the camera facts, the frame, the game clock and its own view angles."""
    public = getattr(observation, "public", None) or {}
    view = getattr(observation, "view", None)
    return {"seq": getattr(observation, "seq", None), "frame": getattr(observation, "frame", None),
            "game_s": getattr(observation, "game_s", None), "scene": getattr(observation, "scene", None),
            "playing": getattr(observation, "playing", None), "paused": getattr(observation, "paused", None),
            "camera": public.get("camera") if isinstance(public, Mapping) else None,
            "view": view.model_dump(mode="json") if hasattr(view, "model_dump") else view}


def _status(terminal: Any) -> str:
    status = getattr(terminal, "status", None)
    return str(getattr(status, "value", status))


async def drive(client: Any, snapshot: Snapshot, script: Mapping[str, Any],
                run_id: str | None = None) -> dict[str, Any]:
    """Send every hold once and report what each did; stop at the first hold that does not complete.

    A hold is bound to a fresh observation, exactly as a session binds one, so the observation can never be older
    than `max_age_s`. The closing observation is taken after `settle_s`, so the camera and the entity are read
    after the movement has finished rather than at its last frame.
    """
    started = _utcnow()
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "kind": KIND, "is_s1_turn": False,
                              "model_calls": 0, "run_id": run_id, "started_at_utc": started,
                              "status": "incomplete", "reason": None, "holds": [], "stop": None}
    for index, hold in enumerate(script["holds"], 1):
        observation = await client.observe_once()
        before, before_problem = await read_snapshot(snapshot)
        action_id = f"scripted-{uuid.uuid4().hex}"
        try:
            records = await client.act(action_id=action_id, choice_id=hold["action"], action=hold["action"],
                                       based_on_seq=getattr(observation, "seq", None),
                                       max_age_s=script["max_age_s"], lease_s=hold["lease_s"])
        except BridgeError as exc:
            report.update(status="incomplete", reason=f"hold {index} ({hold['action']}) could not be sent: {exc}")
            break
        terminal = records[-1]
        status = _status(terminal)
        await asyncio.sleep(script["settle_s"])
        after, after_problem = await read_snapshot(snapshot)
        closing = await client.observe_once()
        observed = displacement_between(before, after)
        row = {"index": index, "action": hold["action"], "action_id": action_id, "lease_s": hold["lease_s"],
               "observation": _view(observation), "closing_observation": _view(closing),
               "status": status,
               "reject_reason": getattr(getattr(terminal, "reject_reason", None), "value", None),
               "release_verified": getattr(terminal, "release_verified", None),
               "applied_s": getattr(terminal, "applied_duration_s", None),
               "before": _transform(before, before_problem), "after": _transform(after, after_problem),
               "net_displacement": observed.get("net_displacement") if observed is not None else None,
               "unit": observed.get("unit") if observed is not None else None,
               "problem": (None if observed is not None and observed.get("net_displacement") is not None
                           else before_problem or after_problem or "a position was unknown")}
        report["holds"].append(row)
        if status != "completed" or row["release_verified"] is not True:
            report["reason"] = (f"hold {index} ({hold['action']}) ended {status}"
                                + (f" ({row['reject_reason']})" if row["reject_reason"] else "")
                                + "; the run stops here and nothing is retried")
            break
    else:
        report.update(status="complete")
    report["finished_at_utc"] = _utcnow()
    return report


def entity_id_of(record: Mapping[str, Any]) -> str:
    """The entity the probe measures, from the entity record the sessions use."""
    entity_id = record.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        raise ValueError("the entity record has no entity_id")
    return entity_id


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="playtest.bench.scripted", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # The bench host fixes this command line; --calibration is accepted because every bench client gets it.
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--entity", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--script", type=Path, default=None, help=f"default: ${PLAN_VARIABLE}")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import os

    args = _parse(argv)
    script_path = args.script or (Path(os.environ[PLAN_VARIABLE]) if os.environ.get(PLAN_VARIABLE) else None)
    if script_path is None:
        print(f"scripted probe: give --script or set {PLAN_VARIABLE}", file=sys.stderr)
        return 2
    try:
        script, script_sha = load_script(script_path)
        entity_id = entity_id_of(json.loads(args.entity.read_bytes()))
        if args.output.exists():
            raise ValueError("the output must be new")
    except (OSError, ValueError) as exc:
        print(f"scripted probe: {exc}", file=sys.stderr)
        return 2

    async def run() -> dict[str, Any]:
        client = await BridgeClient.connect_from_project(args.project, timeout_s=args.timeout_s)
        try:
            report = await drive(client, make_snapshot(client, entity_id), script)
        finally:
            stop = await client.aclose()
            report["stop"] = stop.model_dump(mode="json") if stop is not None else None
        return report

    try:
        report = asyncio.run(run())
    except (BridgeError, BridgeLockError, ValueError, OSError, TimeoutError) as exc:
        print(f"scripted probe: {exc}", file=sys.stderr)
        return 1
    report.update(entity_id=entity_id, script_sha256=script_sha, script=script)
    if report.get("stop") is None or (report["stop"] or {}).get("release_verified") is not True:
        report["status"] = "incomplete"
        report["reason"] = report.get("reason") or "the bridge shutdown did not verify input release"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")
    moved = [row for row in report["holds"] if row["net_displacement"] is not None]
    print(f"scripted probe: {report['status']}; {len(report['holds'])} hold(s), {len(moved)} measured")
    for row in report["holds"]:
        distance = "unknown" if row["net_displacement"] is None else f"{row['net_displacement']:.3f} {row['unit']}"
        after = row["after"]["position"]
        camera = ((row["closing_observation"].get("camera") or {}).get("position")
                  if isinstance(row["closing_observation"].get("camera"), Mapping) else None)
        print(f"  {row['index']}. {row['action']} {row['status']}; net {distance}; "
              f"at {after}; camera {camera}")
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
