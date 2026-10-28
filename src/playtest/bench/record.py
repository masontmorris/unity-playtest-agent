"""Record pre-hold decision frames through the live bridge, without calling a model."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, ChoiceSource
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError
from playtest.core.context import INSTRUCTIONS, build_context, canonical
from playtest.preflight.calibrate import _entity_snapshot, _new_runtime_errors, discover_actions
from playtest.preflight.entity import _complete, _norm, _sub, _vector

HOLD_S = 1.2
PASSES = 5
OBJECTIVE = (
    "In this pass, check each offered control once, then finish. A check is a completed "
    "hold requested for 1.2 seconds with verified release; inspect its measured duration and effect."
)


PROTOCOLS = ("ascending", "shuffled")


def pass_plan(protocol: str, seed: int | None, pass_no: int, count: int = 8) -> tuple[list[int], list[int]]:
    """Ids and hold order for one pass.

    Control `i` (in sorted-name order) gets the id `c{ids[i]}`, and the controls are held in `visit`
    order. `ascending` is the first corpus: ids and hold order both follow the sorted names.
    `shuffled` draws two independent permutations from the seed and redraws an identity id map, an
    identity hold order, or a hold order whose ids ascend, so completed checks are not an id prefix.
    """
    identity = list(range(count))
    ascending = [i + 1 for i in identity]
    if protocol == "ascending":
        return ascending, identity
    if protocol != "shuffled" or isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("the shuffled protocol needs an integer seed")
    rng = random.Random(f"s1-bench-shuffled:{seed}:{pass_no}")
    while True:
        ids, visit = ascending[:], identity[:]
        rng.shuffle(ids)
        rng.shuffle(visit)
        if ids != ascending and visit != identity and [ids[i] for i in visit] != ascending:
            return ids, visit


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def context_for(frame: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the actual payload, rejecting a frame bound to another observation."""
    return build_context(
        objective=frame["objective"],
        observation=Observation.model_validate(frame["observation"]),
        choices=ChoiceSet.model_validate(frame["choice_set"]),
        facts=frame["facts"],
        age_s=frame.get("age_s"),
        sample_gap_s=frame.get("sample_gap_s"),
        game_view=frame.get("game_view"),
    )


def _descriptor(action: dict[str, Any]) -> str:
    return canonical({"control": action["control"], "action": action["action"],
                      "bindings": action["bindings"], "resolution": action["resolution"]})


def calibrated_choices(calibration: dict[str, Any], discovery: dict[str, Any]) -> list[Choice]:
    """Use only controls with two measured holds and unchanged discovered descriptors."""
    actions = calibration.get("actions")
    summaries = calibration.get("action_summaries")
    if not isinstance(actions, list) or not isinstance(summaries, list):
        raise ValueError("calibration has no action catalog and summaries")
    if calibration.get("action_cap", {}).get("truncated") is not False:
        raise ValueError("calibration's action catalog is incomplete")
    live, _ = discover_actions(discovery)
    live_by_control = {item["control"]: item for item in live}
    if len(live_by_control) != len(live):
        raise ValueError("live controls are ambiguous")
    summary_by_control = {item.get("control"): item for item in summaries}
    if len(summary_by_control) != len(summaries):
        raise ValueError("calibration summaries are ambiguous")
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for action in actions:
        control = action["control"]
        summary = summary_by_control.get(control, {})
        holds = [h for h in summary.get("holds", []) if h.get("requested_hold_s") == HOLD_S]
        if len(holds) != 1 or holds[0].get("valid_trials", 0) < 2:
            continue
        if control not in live_by_control or _descriptor(action) != _descriptor(live_by_control[control]):
            raise ValueError(f"calibrated descriptor for {control!r} no longer resolves")
        eligible.append((action, holds[0]))
    eligible.sort(key=lambda pair: pair[0]["control"])
    if len(eligible) != 8 or len({item[0]["control"] for item in eligible}) != 8:
        raise ValueError("the predeclared corpus requires eight distinct calibrated controls")
    choices = []
    for i, (action, hold) in enumerate(eligible, 1):
        mean = hold.get("mean_magnitude")
        variance = hold.get("sample_variance")
        if not isinstance(mean, (int, float)) or isinstance(mean, bool) or not math.isfinite(mean):
            raise ValueError("calibrated effect is unknown")
        if not isinstance(variance, (int, float)) or isinstance(variance, bool) or not math.isfinite(variance) or variance < 0:
            raise ValueError("calibrated variance is unknown")
        label = (f"Request a {HOLD_S:g}-second hold of {action['control']}; calibrated net displacement "
                 f"{mean:.3f} world units (sample variance {variance:.6f}, n={hold['valid_trials']}).")
        choices.append(Choice(id=f"c{i}", kind=ChoiceKind.action, label=label,
                              action=action["action"], params={"duration_game_s": HOLD_S},
                              source=ChoiceSource.manifest))
    choices.extend((Choice(id="wait", kind=ChoiceKind.wait, label="Wait", source=ChoiceSource.core),
                    Choice(id="finish", kind=ChoiceKind.finish, label="Finish", source=ChoiceSource.core)))
    return choices


def _source_hash(root: Path, suffixes: set[str]) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in suffixes
                       and "__pycache__" not in p.parts):
        h.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def _git_head(path: Path) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _game_worktree(path: Path) -> dict[str, Any] | None:
    """Fingerprint tracked changes and unignored untracked files, without copying game data."""
    try:
        names = subprocess.check_output(["git", "-C", str(path), "diff", "--name-only", "-z", "HEAD"],
                                        stderr=subprocess.DEVNULL)
        names += subprocess.check_output(["git", "-C", str(path), "ls-files", "--others", "--exclude-standard", "-z"],
                                         stderr=subprocess.DEVNULL)
        paths = sorted(set(name.decode("utf-8") for name in names.split(b"\0") if name))
        files: dict[str, str | None] = {}
        for relative in paths:
            target = (path / relative).resolve()
            if not target.is_relative_to(path.resolve()):
                raise ValueError("a game worktree path escapes the project")
            files[relative] = digest(target.read_bytes()) if target.is_file() else None
        return {"commit": _git_head(path), "changed_files_sha256": files}
    except (OSError, UnicodeError, subprocess.CalledProcessError):
        return None


def _save(output: Path, data: dict[str, Any]) -> None:
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    # Windows denies replacement while another process briefly has the destination open.
    # Keep the complete temporary checkpoint and retry; never overwrite it with partial JSON.
    for attempt in range(40):
        try:
            temporary.replace(output)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


async def record(client: BridgeClient, selected: dict[str, Any], calibration: dict[str, Any],
                 result: dict[str, Any], output: Path, *, protocol: str = "ascending",
                 seed: int | None = None) -> None:
    if selected.get("status") != "identified" or calibration.get("entity_id") != selected.get("entity_id"):
        raise ValueError("the identified entity and calibrated entity do not match")
    if selected.get("stop", {}).get("release_verified") is not True:
        raise ValueError("the stage 2 record has no verified release")
    discovery = _complete(await client.inspect("discovery"), "discovery")
    for name in ("game_objects", "components", "bindings", "input_asset_bindings"):
        if discovery.get("inspection_coverage", {}).get(name, {}).get("truncated") is not False:
            raise ValueError(f"live discovery has incomplete {name} coverage")
    entity_id = selected["entity_id"]
    entities = [item for item in discovery.get("game_objects", []) if item.get("id") == entity_id]
    if len(entities) != 1 or not isinstance(selected.get("entity"), dict):
        raise ValueError("the entity does not resolve uniquely")
    if any(entities[0].get(k) != selected["entity"].get(k) for k in ("scene_id", "scene_path", "path")):
        raise ValueError("the entity descriptor changed")
    choices = calibrated_choices(calibration, discovery)
    controls = [choice for choice in choices if choice.kind is ChoiceKind.action]
    core = [choice for choice in choices if choice.kind is not ChoiceKind.action]
    result["control_order"] = [choice.action for choice in controls]
    result["choice_provenance"] = "calibration-derived provisional catalog, not a preflight manifest"
    if protocol == "shuffled":
        result.setdefault("protocol", {"name": protocol, "seed": seed, "passes": []})
    _save(output, result)
    initial = _entity_snapshot(await client.inspect("transforms"), entity_id)
    seen_errors, errors = _new_runtime_errors(initial, 0)
    if errors:
        raise ValueError("game errors were already present before recording")

    async def decide(pass_no: int, frame_id: str, offered: list[Choice], completed: list[str],
                     scripted: str | None, **extra: Any) -> tuple[dict[str, Any], Observation, dict[str, Any]]:
        """Observe, build and checkpoint one decision frame; nothing is sent to the game."""
        nonlocal seen_errors
        before_response = await client.inspect("transforms")
        before = _entity_snapshot(before_response, entity_id)
        before["inspection_seq"] = before_response.seq
        seen_errors, errors = _new_runtime_errors(before, seen_errors)
        if errors:
            raise ValueError("game errors appeared before the next hold")
        obs = await client.observe_once()
        if obs.clock_mode.value != "realtime" or not obs.playing or obs.paused or obs.time_scale != 1.0:
            raise ValueError("a decision observation was not in unpaused real time at scale 1")
        choice_set = ChoiceSet(set_version=len(result["frames"]), built_from_seq=obs.seq, choices=offered)
        facts = list(completed)
        facts.append(f"Entity position from inspection seq {before['inspection_seq']} at game time {before['game_time_s']}: "
                     f"{before['position']} in Unity world units.")
        frame = {"id": frame_id, "pass": pass_no, **extra,
                 "objective": OBJECTIVE, "observation": obs.model_dump(mode="json"),
                 "choice_set": choice_set.model_dump(mode="json"), "facts": facts,
                 "age_s": None, "sample_gap_s": None, "game_view": None,
                 "before_entity": before, "scripted_choice_id": scripted,
                 "execution": None, "after_observation": None, "after_entity": None}
        frame["payload_sha256"] = digest(canonical(context_for(frame)))
        result["frames"].append(frame)
        _save(output, result)  # Preserve an attempted turn if the bridge disconnects.
        return frame, obs, before

    for pass_no in range(1, PASSES + 1):
        ids, visit = pass_plan(protocol, seed, pass_no, len(controls))
        if protocol == "shuffled":
            result["protocol"]["passes"].append({"pass": pass_no, "ids": ids, "visit": visit})
        held = [control.model_copy(update={"id": f"c{ids[i]}"}) for i, control in enumerate(controls)]
        offered = sorted(held, key=lambda item: int(item.id[1:])) + core
        completed: list[str] = []
        for step, index in enumerate(visit, 1):
            choice = held[index]
            shuffled = protocol == "shuffled"
            frame, obs, before = await decide(
                pass_no, f"pass-{pass_no:02d}-s{step}" if shuffled else f"pass-{pass_no:02d}-{choice.id}",
                offered, completed, choice.id, **({"position": step, "kind": "check"} if shuffled else {}))
            executions = await client.act(action_id=f"bench-{pass_no}-{choice.id}", choice_id=choice.id,
                                          action=choice.action, params=dict(choice.params), based_on_seq=obs.seq,
                                          max_age_s=2.0, lease_s=HOLD_S)
            frame["execution"] = [item.model_dump(mode="json") for item in executions]
            terminal = executions[-1]
            duration = terminal.applied_duration_s
            if terminal.status.value != "completed" or terminal.release_verified is not True:
                _save(output, result)
                raise ValueError(f"{choice.id} did not complete with verified release")
            if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration)
                    or duration <= 0):
                _save(output, result)
                raise ValueError(f"{choice.id} has an unknown or invalid measured hold")
            await asyncio.sleep(0.08)
            after_response = await client.inspect("transforms")
            after = _entity_snapshot(after_response, entity_id)
            after["inspection_seq"] = after_response.seq
            frame["after_entity"] = after
            after_obs = await client.observe_once()
            frame["after_observation"] = after_obs.model_dump(mode="json")
            _save(output, result)
            if after_obs.clock_mode.value != "realtime" or not after_obs.playing or after_obs.paused or after_obs.time_scale != 1.0:
                raise ValueError("the observation after a hold was not in unpaused real time at scale 1")
            seen_errors, errors = _new_runtime_errors(after, seen_errors)
            if errors:
                raise ValueError(f"game errors were observed during or after {choice.id}")
            p0, p1 = _vector(before.get("position")), _vector(after.get("position"))
            distance = _norm(_sub(p1, p0)) if p0 is not None and p1 is not None else None
            completed.append(f"{choice.id} ({choice.action}) held for {duration} seconds with verified release; "
                             f"net displacement between inspection seq {before['inspection_seq']} and "
                             f"{after['inspection_seq']}: {distance} world units.")
            await asyncio.sleep(0.2)
        if protocol == "shuffled":
            # Every control is checked, so this is the decision the first corpus never recorded.
            await decide(pass_no, f"pass-{pass_no:02d}-fin", offered, completed, None,
                         position=len(visit) + 1, kind="terminal")
    final = _entity_snapshot(await client.inspect("transforms"), entity_id)
    _, errors = _new_runtime_errors(final, seen_errors)
    if errors:
        raise ValueError("game errors appeared after the final hold")
    result["status"] = "complete"
    _save(output, result)


async def _run(project: Path, selected: dict[str, Any], calibration: dict[str, Any],
               result: dict[str, Any], output: Path, protocol: str = "ascending", seed: int | None = None) -> None:
    def mark_inconclusive(message: str) -> None:
        previous = result.get("error")
        result.update(status="inconclusive", error=f"{previous}; {message}" if previous else message)

    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    try:
        result["unity_version"] = client.status.unity_version if client.status is not None else None
        await record(client, selected, calibration, result, output, protocol=protocol, seed=seed)
    except (BridgeError, ValueError, KeyError, TypeError) as exc:
        mark_inconclusive(f"{type(exc).__name__}: {exc}")
    finally:
        stop = await client.aclose()
        result["stop"] = stop.model_dump(mode="json") if stop is not None else None
        result["game_worktree_after"] = _game_worktree(project)
        if stop is None or stop.release_verified is not True:
            mark_inconclusive("bridge shutdown did not verify input release")
        if result["game_worktree_before"] is None or result["game_worktree_after"] is None:
            mark_inconclusive("the game worktree could not be fingerprinted")
        elif result["game_worktree_before"] != result["game_worktree_after"]:
            mark_inconclusive("the game worktree changed during recording")
        _save(output, result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--entity", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    # The Unity host starts this process with a fixed command line, so the environment can supply these.
    parser.add_argument("--protocol", choices=PROTOCOLS, default=os.environ.get("UPA_BENCH_PROTOCOL") or "ascending")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    protocol = args.protocol
    seed = args.seed
    if seed is None and os.environ.get("UPA_BENCH_SEED"):
        try:
            seed = int(os.environ["UPA_BENCH_SEED"])
        except ValueError:
            parser.error("UPA_BENCH_SEED must be an integer")
    if protocol not in PROTOCOLS:
        parser.error(f"--protocol must be one of {', '.join(PROTOCOLS)}")
    if protocol == "shuffled" and seed is None:
        parser.error("the shuffled protocol needs --seed")
    project, output = args.project.resolve(), args.output.resolve()
    if output.is_relative_to(project) or output.exists():
        parser.error("--output must be a new file outside the Unity project")
    repo = Path(__file__).resolve().parents[3]
    try:
        entity_bytes, calibration_bytes = args.entity.read_bytes(), args.calibration.read_bytes()
        selected, calibration = json.loads(entity_bytes), json.loads(calibration_bytes)
        result: dict[str, Any] = {
            "version": 2 if protocol == "shuffled" else 1, "status": "recording", "error": None,
            "game_commit": _git_head(project), "repo_commit": _git_head(repo),
            "game_worktree_before": _game_worktree(project),
            "bridge_source_sha256": _source_hash(repo / "bridge", {".cs", ".asmdef", ".json"}),
            "python_source_sha256": _source_hash(repo / "src" / "playtest", {".py"}),
            "entity_sha256": digest(entity_bytes), "calibration_sha256": digest(calibration_bytes),
            "instructions_sha256": digest(INSTRUCTIONS), "objective_sha256": digest(OBJECTIVE),
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "input_mode": "input_system",
            "clock_mode": "realtime", "reset": "one scene load; no reset between passes",
            "requested_hold_s": HOLD_S, "controller_lease_s": HOLD_S, "frames": [],
        }
        if protocol == "shuffled":
            result["protocol"] = {"name": protocol, "seed": seed, "passes": []}
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, allow_nan=False)
        asyncio.run(_run(project, selected, calibration, result, output, protocol, seed))
    except (OSError, ValueError, BridgeError, BridgeLockError) as exc:
        print(f"recording failed: {exc}", file=sys.stderr)
        return 1
    print(f"recorded {len(result['frames'])} decision frames: {result['status']}")
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
