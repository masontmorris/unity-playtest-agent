"""Reproduce a finding k of n times by replaying a session's recorded inputs after a reset (#38).

This is the operator-run `reproduce` for Day 6. It is a code-driven procedure, not an S1 turn, and it calls no
model. Making the same engine a choice S1 can take mid-session is #127.

**`plan`** reads a finished session's trace. It takes a finding (the latest candidate, unless one is named)
and its `repro_check`, the predicate a replay must show. Then it lists the inputs that reached the game before
the finding's last cited evidence entry: every execution, in trace order, whose terminal record completed. An
input sent after that entry cannot have caused what it shows, and replaying it could undo it. For each input
it keeps the action, its params, its lease, and its start time relative to the first input, as the bridge
measured it. Inputs that were rejected or failed sent nothing in the session, so they are listed as skipped,
never replayed. When the evidence is an observation, the plan also keeps its time relative to the first input.

**`run`** executes the plan inside a PlayMode host that serves the manifest's catalog and reloads the scene on
request. Each attempt:
1. requests a numbered scene reload, the calibrated reset;
2. reconnects and installs the manifest's readers, then waits briefly;
3. reads the baseline;
4. replays every input at its recorded offset, once, never retried or replaced;
5. reads the readers after each input, then through a settle window that also covers the evidence's time.

A reader check (`public.<reader>`) is scored with the shared evaluator (`roles.checks.evaluate`), first on the
baseline:
- **unknown:** the check already held after the reset, before any input, so the replay cannot show it (an
  `unchanged` check always does), or the baseline could not be scored;
- **reproduced:** the baseline did not satisfy it, and some later reading did;
- **not reproduced:** later readings were scored and none satisfied it;
- **unknown:** no later reading could be scored.

An effect check (`effect.<action>`, #154) is the net displacement of one execution of an action, in world units.
It shows an action that stops having its effect, which leaves the readers at rest and so can never pass the
baseline rule. The plan names the calibrated displacement (`build_choices(...).expected`) and marks the scored
input: the last replayed execution of that action. Each execution of the action is measured between two position
snapshots, as the live probe does. It is scored on the scored execution's measurement:
- **unknown:** the check holds for the calibrated displacement, so the replay cannot show a difference;
- **reproduced:** the scored execution's measured displacement satisfies the check;
- **not reproduced:** it was measured and does not;
- **unknown:** it was not measured (an unknown position is unknown, never zero).

An attempt in which an input was rejected or failed diverged from the session. It is invalid: kept with its
reason, and never counted in n.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from playtest.contracts.investigation import (EFFECT_PREFIX, Comparator, Finding, FindingState, Repro, ReproCheck,
                                              effect_action)
from playtest.contracts.manifest import PreflightManifest
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError, read_project_lock
from playtest.roles import checks
from playtest.runner.choices import UNIT, build_choices
from playtest.runner.probe import Snapshot, allowance, displacement_between, make_snapshot, read_snapshot

SCHEMA_VERSION = 1
RESET = "scene_reload"
CLOCK = "realtime"
ATTEMPTS = 5
SETTLE_S = 3.0
START_SETTLE_S = 1.0
SAMPLE_INTERVAL_S = 0.25
MAX_AGE_S = 2.0
RELOAD_TIMEOUT_S = 120.0
CLIENT_TIMEOUT_S = 120.0
HANDSHAKE_POLL_S = 0.05
# Wall-clock allowances the host adds to the plan's own replay time when it sizes its timeout.
RELOAD_ALLOWANCE_S = 70.0
CONNECT_ALLOWANCE_S = 20.0
PER_INPUT_ALLOWANCE_S = 2.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


# --- plan ----------------------------------------------------------------------------------------


def read_trace(path: Path) -> list[dict[str, Any]]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def latest_findings(entries: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each finding's newest state with its trace_seq, keyed by finding_id."""
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("kind") == "finding" and isinstance(entry.get("body"), dict):
            out[entry["body"]["finding_id"]] = {"trace_seq": entry["trace_seq"], "finding": entry["body"]}
    return out


def evidence_turn(entries: Sequence[Mapping[str, Any]], evidence: Sequence[int]) -> int:
    """The latest turn among the cited entries; an entry outside any turn counts as the last turn before it."""
    by_seq = {entry["trace_seq"]: entry for entry in entries}
    turns: list[int] = []
    for seq in evidence:
        if seq not in by_seq:
            raise ValueError(f"the finding cites trace_seq {seq}, which is not in the trace")
        turn = by_seq[seq].get("turn")
        if turn is None:
            earlier = [e.get("turn") for e in entries if e["trace_seq"] < seq and isinstance(e.get("turn"), int)]
            if not earlier:
                raise ValueError(f"trace_seq {seq} has no turn before it")
            turn = earlier[-1]
        turns.append(turn)
    if not turns:
        raise ValueError("the finding cites no evidence")
    return max(turns)


def recorded_inputs(entries: Sequence[Mapping[str, Any]], before_seq: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The inputs that executed before entry `before_seq`, with offsets from the first; and the skipped ones by status."""
    inputs: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    for entry in entries:
        if entry.get("kind") != "execution" or not isinstance(entry.get("turn"), int) or entry["trace_seq"] >= before_seq:
            continue
        body = entry["body"]
        records = body.get("records") or []
        terminal = records[-1] if records else None
        status = terminal.get("status") if isinstance(terminal, dict) else None
        start = _number(terminal.get("input_start_wall_s")) if isinstance(terminal, dict) else None
        if status != "completed" or start is None or terminal.get("release_verified") is not True:
            skipped[str(status or "unrecorded")] += 1
            continue
        request = body["request"]
        inputs.append({"turn": entry["turn"], "trace_seq": entry["trace_seq"], "purpose": body.get("purpose"),
                       "action": request["action"], "choice_id": request.get("choice_id"),
                       "params": request.get("params") or {}, "lease_s": request.get("lease_s"),
                       "recorded_start_wall_s": start,
                       "recorded_applied_s": _number(terminal.get("applied_duration_s"))})
    if inputs:
        first = inputs[0]["recorded_start_wall_s"]
        for item in inputs:
            item["offset_s"] = round(item["recorded_start_wall_s"] - first, 6)
    return inputs, dict(skipped)


def evidence_offset(entries: Sequence[Mapping[str, Any]], evidence_seq: int, first_start_wall_s: float) -> float | None:
    """When the cited entry is an observation, its bridge time relative to the first input; otherwise None.

    Both times come from the bridge's wall clock: the observation's `wall_utc` and the input's `input_start_wall_s`.
    """
    entry = next((e for e in entries if e["trace_seq"] == evidence_seq), None)
    wall = entry["body"].get("wall_utc") if entry is not None and entry.get("kind") == "observation" else None
    if not isinstance(wall, str):
        return None
    try:
        seen = datetime.fromisoformat(wall)
    except ValueError:
        return None
    if seen.tzinfo is None:
        return None
    return round(seen.timestamp() - first_start_wall_s, 6)


def effect_plan(manifest: PreflightManifest, action: str, inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """The calibrated effect an `effect.<action>` check is compared with, and the replayed input it scores.

    The scored input is the last execution of the action before the evidence; it is marked `scored` in `inputs`.
    Calibration measured one declaration, held for one hold, so it speaks only for an execution of the action as
    declared. A plan is refused when the manifest has no calibrated displacement for the action, when no replayed
    input is an execution of it, or when the scored one was not the calibrated declaration (a parameter override
    or another hold): the replay would compare a measurement with a number that is not its own.
    """
    if all(declared.name != action for declared in manifest.actions):
        raise ValueError(f"repro_check field {EFFECT_PREFIX}{action} names {action!r}, which is not an action in the manifest")
    expected = build_choices(manifest).expected.get(action)
    displacement = _number(expected.get("net_displacement")) if expected else None
    if expected is None or displacement is None:
        raise ValueError(f"{action!r} has no calibrated expected displacement in the manifest, so an effect check "
                         "on it has nothing to be compared with")
    scored = next((item for item in reversed(inputs) if item["action"] == action), None)
    if scored is None:
        raise ValueError(f"no input that completed before the finding's evidence is an execution of {action!r}, "
                         "so there is no effect to measure")
    hold, lease = _number(expected.get("requested_hold_s")), _number(scored.get("lease_s"))
    if scored["params"] or (lease is not None and (hold is None or abs(lease - hold) > 1e-9)):
        raise ValueError(f"the last execution of {action!r} before the evidence (turn {scored['turn']}) was not the "
                         f"calibrated declaration (params {scored['params']}, lease {scored['lease_s']} s; calibrated "
                         f"for a {hold} s hold), so calibration's displacement is not its own")
    scored["scored"] = True
    bounds = allowance(expected)
    return {"action": action, "expected_displacement": displacement, "unit": UNIT, "requested_hold_s": hold,
            "sample_variance": expected.get("sample_variance"), "valid_trials": expected.get("valid_trials"),
            "spread": bounds[0] if bounds is not None else None,
            "allowance": bounds[1] if bounds is not None else None,
            "executions": sum(item["action"] == action for item in inputs),
            "scored_turn": scored["turn"], "scored_trace_seq": scored["trace_seq"]}


def build_plan(trace_path: Path, manifest_path: Path, *, finding_id: str | None = None,
               finding_record: Mapping[str, Any] | None = None,
               repro_check: Mapping[str, Any] | None = None, attempts: int = ATTEMPTS, settle_s: float = SETTLE_S,
               start_settle_s: float = START_SETTLE_S, sample_interval_s: float = SAMPLE_INTERVAL_S) -> dict[str, Any]:
    if finding_id is not None and finding_record is not None:
        raise ValueError("name a trace finding or give a finding record, not both")
    raw = trace_path.read_bytes()
    entries = read_trace(trace_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest = PreflightManifest.model_validate_json(manifest_bytes, strict=True)
    findings = latest_findings(entries)
    if finding_record is not None:
        # A candidate recorded outside the trace (a post-session review): its evidence_seq still names trace entries.
        chosen = {"finding": dict(finding_record), "trace_seq": None}
    elif finding_id is None:
        candidates = [f for f in findings.values() if f["finding"].get("state") == FindingState.candidate.value]
        if not candidates:
            raise ValueError("the trace has no candidate finding; name one with --finding")
        chosen = max(candidates, key=lambda f: f["trace_seq"])
    elif finding_id in findings:
        chosen = findings[finding_id]
    else:
        raise ValueError(f"the trace has no finding {finding_id!r}")
    finding = Finding.model_validate(chosen["finding"])
    if repro_check is not None:
        check, check_source = ReproCheck.model_validate(repro_check), "operator"
    elif finding.repro_check is not None:
        check, check_source = finding.repro_check, "finding"
    else:
        raise ValueError(f"finding {finding.finding_id!r} has no repro_check; supply one")
    action = effect_action(check.field)
    alias = check.field.removeprefix("public.")
    kinds = {reader.alias: reader.kind for reader in manifest.readers}
    if action is None and alias not in kinds:
        raise ValueError(f"repro_check field {check.field!r} is not one of the manifest's readers")
    through = evidence_turn(entries, finding.evidence_seq)
    last_evidence = max(finding.evidence_seq)
    inputs, skipped = recorded_inputs(entries, last_evidence)
    if not inputs:
        raise ValueError("no input completed before the finding's evidence; there is nothing to replay")
    effect = effect_plan(manifest, action, inputs) if action is not None else None
    for name, value in (("attempts", attempts), ("settle_s", settle_s), ("start_settle_s", start_settle_s),
                        ("sample_interval_s", sample_interval_s)):
        if _number(value) is None or value <= 0:
            raise ValueError(f"{name} must be positive")
    seen_s = evidence_offset(entries, last_evidence, inputs[0]["recorded_start_wall_s"])
    last = inputs[-1]
    replay_s = (max(last["offset_s"] + (_number(last["lease_s"]) or 0.0), seen_s or 0.0)
                + settle_s + start_settle_s)
    run = next((e["body"] for e in entries if e.get("kind") == "run"), {})
    return {
        "schema_version": SCHEMA_VERSION, "kind": "replay_reproduction", "is_s1_turn": False, "model_calls": 0,
        "trace_sha256": _sha(raw), "run_id": run.get("run_id"), "game_commit": run.get("game_commit"),
        "manifest_sha256": _sha(manifest_bytes),
        "finding_id": finding.finding_id, "finding_trace_seq": chosen["trace_seq"],
        "finding": finding.model_dump(mode="json"),
        "repro_check": check.model_dump(mode="json"), "repro_check_source": check_source,
        "repro_check_kind": "number" if effect is not None else kinds[alias], "effect": effect,
        "evidence_seq": finding.evidence_seq, "evidence_turn": through, "evidence_offset_s": seen_s,
        "inputs": inputs, "skipped_inputs": skipped,
        "attempts": attempts, "settle_s": settle_s, "start_settle_s": start_settle_s,
        "sample_interval_s": sample_interval_s, "reset": RESET, "clock_mode": CLOCK,
        "host_budget_s": round(attempts * (RELOAD_ALLOWANCE_S + CONNECT_ALLOWANCE_S + replay_s
                                           + PER_INPUT_ALLOWANCE_S * len(inputs)) + 60.0, 1),
    }


# --- run -----------------------------------------------------------------------------------------


def score(check: ReproCheck, kind: Any, value: Any, baseline: Any) -> bool | None:
    """The shared evaluator on one reading: True, False, or None when the reading cannot be scored."""
    if kind is None:
        return None  # the manifest gives the reader no comparable kind
    return checks.evaluate(value, kind=kind, op=Comparator(check.op), expected=check.value,
                           baseline=baseline, tolerance=check.tolerance)


def reload_paths(directory: Path, number: int) -> tuple[Path, Path]:
    return directory / f"reload-{number}.request.json", directory / f"reload-{number}.done.json"


async def request_reload(directory: Path, number: int, timeout_s: float) -> dict[str, Any]:
    """Ask the host for reload `number` once and wait for its completion; a timeout is an error, never a retry."""
    request_path, done_path = reload_paths(directory, number)
    temporary = request_path.with_name(request_path.name + ".writing")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump({"schema_version": SCHEMA_VERSION, "number": number, "requested_at_utc": _utcnow()}, stream)
    if request_path.exists() or done_path.exists():
        raise ValueError(f"the handshake files for reload {number} already exist")
    temporary.replace(request_path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not done_path.exists():
        if loop.time() >= deadline:
            raise TimeoutError(f"scene reload {number} did not complete within {timeout_s:g} s")
        await asyncio.sleep(HANDSHAKE_POLL_S)
    done = json.loads(done_path.read_text(encoding="utf-8"))
    if done.get("method") != RESET or done.get("reloaded") is not True or done.get("number") != number:
        raise ValueError(f"the host did not report scene reload {number} as completed")
    return {key: done.get(key) for key in ("number", "scene_path", "requested_at_utc", "completed_at_utc",
                                           "game_time_s", "real_time_s", "frame")}


def _reading(sample: Any, alias: str, check: ReproCheck, kind: Any, baseline: Any, label: str,
             elapsed_s: float) -> dict[str, Any]:
    values = getattr(sample, "values", {}) or {}
    value = values.get(alias)
    return {"at": label, "elapsed_s": round(elapsed_s, 3), "game_s": getattr(sample, "game_s", None),
            "value": value, "error": (getattr(sample, "errors", {}) or {}).get(alias) or getattr(sample, "problem", None),
            "satisfied": score(check, kind, value, baseline) if value is not None else None}


def _measurement(item: Mapping[str, Any], terminal: Any, before: tuple[dict[str, Any] | None, str | None],
                 after: tuple[dict[str, Any] | None, str | None], check: ReproCheck) -> dict[str, Any]:
    """One replayed execution's net displacement, from the snapshots before and after it.

    The same measurement as the live probe's (`probe.displacement_between`). A snapshot that could not be taken, or
    one with an unknown position, leaves the displacement None with the reason: unmeasured, never zero.
    """
    observed = displacement_between(before[0], after[0]) or {}
    distance = observed.get("net_displacement")
    return {"turn": item["turn"], "trace_seq": item.get("trace_seq"), "scored": bool(item.get("scored")),
            "net_displacement": distance, "unit": UNIT,
            "from_inspection_seq": observed.get("from_inspection_seq"),
            "to_inspection_seq": observed.get("to_inspection_seq"), "window_game_s": observed.get("window_game_s"),
            "applied_s": getattr(terminal, "applied_duration_s", None),
            "problem": None if distance is not None else (before[1] or after[1] or "a position was unknown"),
            "satisfied": score(check, "number", distance, None) if distance is not None else None}


def _score_effect(attempt: dict[str, Any]) -> None:
    """The result of an effect attempt whose inputs all completed: the counterpart of the baseline rule.

    A check the calibrated displacement satisfies cannot show a difference, so the attempt is unknown whatever was
    measured. Otherwise the scored execution's own measurement decides.
    """
    effect = attempt["effect"]
    scored = next((m for m in effect["measurements"] if m["scored"]), None)
    effect["measured"] = scored["net_displacement"] if scored is not None else None
    unit, calibrated = effect["unit"], effect["expected_displacement"]
    if effect["holds_for_expected"] is True:
        attempt.update(valid=True, result="unknown",
                       reason=f"the repro_check holds for the calibrated effect ({calibrated:.3f} {unit}), "
                              "so the replay cannot show a difference")
    elif effect["holds_for_expected"] is None:
        attempt.update(valid=True, result="unknown",
                       reason="the repro_check could not be scored against the calibrated effect")
    elif scored is None:
        attempt.update(valid=True, result="unknown", reason="no replayed execution was marked as scored")
    elif scored["net_displacement"] is None:
        attempt.update(valid=True, result="unknown",
                       reason=f"the scored execution's displacement was not measured ({scored['problem']})")
    elif scored["satisfied"] is True:
        attempt.update(valid=True, result="reproduced",
                       reason=f"the scored execution's measured displacement {scored['net_displacement']:.3f} {unit} "
                              f"satisfies the repro_check (calibrated {calibrated:.3f})")
    else:
        attempt.update(valid=True, result="not_reproduced",
                       reason=f"the scored execution's measured displacement {scored['net_displacement']:.3f} {unit} "
                              f"does not satisfy the repro_check (calibrated {calibrated:.3f})")


async def run_attempt(plan: Mapping[str, Any], number: int, reload: Callable[[int], Awaitable[dict[str, Any]]],
                      connect: Callable[[], Awaitable[Any]], readers_for: Callable[[Any], Any],
                      snapshot_for: Callable[[Any], Snapshot] | None = None) -> dict[str, Any]:
    """One attempt. A reader check reads the readers; an effect check reads position snapshots instead, so it
    needs `snapshot_for` (the client's snapshot source) and never calls `readers_for`."""
    check = ReproCheck.model_validate(plan["repro_check"])
    kind = plan.get("repro_check_kind")
    effect = plan.get("effect")
    alias = check.field.removeprefix("public.")
    attempt: dict[str, Any] = {"number": number, "valid": False, "result": None, "reason": None, "reload": None,
                               "baseline": None, "baseline_satisfied": None, "inputs": [], "readings": [],
                               "effect": None, "error": None, "stop": None}
    client = None
    readers = snapshot = baseline = None
    loop = asyncio.get_running_loop()
    try:
        attempt["reload"] = await reload(number)
        client = await connect()
        if effect is None:
            readers = readers_for(client)
            await readers.install()
        else:
            if snapshot_for is None:
                raise ValueError("an effect check needs a position snapshot source")
            snapshot = snapshot_for(client)
            attempt["effect"] = {
                "action": effect["action"], "expected_displacement": effect["expected_displacement"],
                "allowance": effect.get("allowance"), "unit": effect.get("unit") or UNIT,
                "holds_for_expected": score(check, "number", effect["expected_displacement"], None),
                "measured": None, "measurements": []}
        await asyncio.sleep(plan["start_settle_s"])
        if readers is not None:
            first = await readers.read()
            baseline = (getattr(first, "values", {}) or {}).get(alias)
            attempt["baseline"] = baseline
            attempt["baseline_satisfied"] = score(check, kind, baseline, baseline) if baseline is not None else None
        started = loop.time()
        for item in plan["inputs"]:
            delay = started + item["offset_s"] - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            # Like the live probe, the reading before an input is taken before the observation the act is bound to,
            # so it adds nothing to that observation's age.
            measures = effect is not None and item["action"] == effect["action"]
            before = await read_snapshot(snapshot) if measures else None
            observation = await client.observe_once()
            sent_s = loop.time() - started
            action_id = f"reproduce-{uuid.uuid4().hex}"
            records = await client.act(action_id=action_id, choice_id=item.get("choice_id") or item["action"],
                                       action=item["action"], params=item.get("params") or None,
                                       based_on_seq=observation.seq, max_age_s=MAX_AGE_S, lease_s=item.get("lease_s"))
            terminal = records[-1]
            status = getattr(terminal.status, "value", terminal.status)
            row = {"turn": item["turn"], "action": item["action"], "recorded_offset_s": item["offset_s"],
                   "sent_offset_s": round(sent_s, 3), "deviation_s": round(sent_s - item["offset_s"], 3),
                   "status": status, "reject_reason": getattr(getattr(terminal, "reject_reason", None), "value", None),
                   "release_verified": terminal.release_verified,
                   "applied_s": terminal.applied_duration_s, "recorded_applied_s": item.get("recorded_applied_s")}
            attempt["inputs"].append(row)
            if status != "completed" or terminal.release_verified is not True:
                attempt["reason"] = (f"the replay diverged at turn {item['turn']}: {item['action']} ended {status}"
                                     + (f" ({row['reject_reason']})" if row["reject_reason"] else ""))
                break
            if measures:  # right after the terminal record, as the live probe does
                attempt["effect"]["measurements"].append(
                    _measurement(item, terminal, before, await read_snapshot(snapshot), check))
            if readers is not None:
                sample = await readers.read()
                attempt["readings"].append(_reading(sample, alias, check, kind, baseline, f"after turn {item['turn']}",
                                                    loop.time() - started))
        else:
            if effect is not None:
                _score_effect(attempt)  # a displacement is measured at its input; there is no settle window to read
                return attempt
            settle_until = loop.time() + plan["settle_s"]
            seen_s = _number(plan.get("evidence_offset_s"))
            if seen_s is not None:
                settle_until = max(settle_until, started + seen_s + plan["settle_s"])
            while loop.time() < settle_until:
                await asyncio.sleep(plan["sample_interval_s"])
                sample = await readers.read()
                attempt["readings"].append(_reading(sample, alias, check, kind, baseline, "settle",
                                                    loop.time() - started))
            outcomes = [reading["satisfied"] for reading in attempt["readings"]]
            if attempt["baseline_satisfied"] is True:
                attempt.update(valid=True, result="unknown",
                               reason="the repro_check already held after the reset, before any input, "
                                      "so the replay cannot show it")
            elif attempt["baseline_satisfied"] is None:
                attempt.update(valid=True, result="unknown",
                               reason="the repro_check could not be scored before any input")
            elif any(outcome is True for outcome in outcomes):
                attempt.update(valid=True, result="reproduced",
                               reason="the repro_check did not hold after the reset, and a later reading satisfied it")
            elif any(outcome is False for outcome in outcomes):
                attempt.update(valid=True, result="not_reproduced",
                               reason="readings were scored and none satisfied the repro_check")
            else:
                attempt.update(valid=True, result="unknown", reason="no reading could be scored")
    except (BridgeError, BridgeLockError, ValueError, OSError, TimeoutError) as exc:
        attempt["error"] = str(exc) or type(exc).__name__
        attempt["reason"] = attempt["reason"] or f"the attempt stopped: {attempt['error']}"
    except Exception as exc:  # noqa: BLE001 - the report must be written whatever failed
        attempt["error"] = f"unexpected {type(exc).__name__}: {exc}"
        attempt["reason"] = attempt["reason"] or f"the attempt stopped: {attempt['error']}"
    finally:
        if client is not None:
            try:
                stop = await client.aclose()
                attempt["stop"] = stop.model_dump(mode="json") if stop is not None else None
            except Exception as exc:  # noqa: BLE001 - a failed close leaves the stop unknown, never the report lost
                attempt["stop"] = {"error": f"{type(exc).__name__}: {exc}"}
    return attempt


READER_BASIS = ("k counts valid attempts where the repro_check did not hold after the reset and a later "
                "reading satisfied it; n counts valid attempts that scored the baseline as not holding and "
                "at least one later reading. Unknown attempts (including a check that already held after "
                "the reset) and invalid (diverged or failed) attempts are in neither")
EFFECT_BASIS = ("k counts valid attempts where the scored execution's measured net displacement satisfied the "
                "repro_check, which does not hold for the calibrated displacement; n counts valid attempts that "
                "measured it. Unknown attempts (including a check that holds for the calibrated displacement, and "
                "a displacement that could not be measured) and invalid (diverged or failed) attempts are in "
                "neither")


def summarize(plan: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [a for a in attempts if a["valid"]]
    k = sum(a["result"] == "reproduced" for a in valid)
    unknown = sum(a["result"] == "unknown" for a in valid)
    n = len(valid) - unknown
    deviations = [abs(row["deviation_s"]) for a in attempts for row in a["inputs"]]
    return {"planned": plan["attempts"], "attempted": len(attempts), "valid": len(valid), "k": k, "n": n,
            "unknown": unknown,
            "basis": EFFECT_BASIS if plan.get("effect") is not None else READER_BASIS,
            "invalid": [{"number": a["number"], "reason": a["reason"]} for a in attempts if not a["valid"]],
            "input_timing": {"max_abs_deviation_s": round(max(deviations), 3) if deviations else None,
                             "inputs_replayed": len(deviations)}}


OVERRIDE_REASON = "an operator check overrode the finding's own, so the finding's state is unchanged"
UNSCORED_REASON = "no attempt was scored, so the finding's state is unchanged"


def overrides_finding(plan: Mapping[str, Any]) -> bool:
    """True when an operator check replaced the finding's own check, so the replay tested something else."""
    return plan.get("repro_check_source") == "operator" and (plan.get("finding") or {}).get("repro_check") is not None


def finding_update(plan: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any] | None:
    """The finding's next state: reproduced when k >= 1, repro_attempted when k = 0 of n >= 1, none otherwise.

    An operator check that overrides the finding's own is a different test, such as a control. Its replay is
    reported, but it never changes the finding's state.
    """
    if summary["n"] < 1 or overrides_finding(plan):
        return None
    timing = summary["input_timing"]["max_abs_deviation_s"]
    check, effect = plan["repro_check"], plan.get("effect")
    measured = (f"repro_check tolerance {check.get('tolerance')}" if effect is None else
                f"net displacement of one execution of {effect['action']} between two position snapshots")
    tolerance = f"{measured}; replayed inputs started within {timing} s of their recorded offsets"
    finding = dict(plan["finding"])
    finding["state"] = (FindingState.reproduced if summary["k"] >= 1 else FindingState.repro_attempted).value
    finding["repro"] = Repro(attempts=summary["n"], reproduced=summary["k"], clock_mode=CLOCK, reset=RESET,
                             tolerance=tolerance).model_dump(mode="json")
    return Finding.model_validate(finding).model_dump(mode="json")


async def measure(plan: Mapping[str, Any], reload: Callable[[int], Awaitable[dict[str, Any]]],
                  connect: Callable[[], Awaitable[Any]], readers_for: Callable[[Any], Any],
                  snapshot_for: Callable[[Any], Snapshot] | None = None) -> dict[str, Any]:
    if plan.get("effect") is not None and snapshot_for is None:
        raise ValueError("the plan's check is an effect check, which needs a position snapshot source")
    attempts = []
    for number in range(1, plan["attempts"] + 1):
        attempt = await run_attempt(plan, number, reload, connect, readers_for, snapshot_for)
        attempts.append(attempt)
        if attempt["error"] is not None and attempt["reload"] is None:
            break  # the reset itself failed: no further attempt can start from a known state
    summary = summarize(plan, attempts)
    status = ("measured" if summary["valid"] == plan["attempts"] and summary["unknown"] == 0
              else "inconclusive" if summary["n"] == 0 else "measured_with_gaps")
    update = finding_update(plan, summary)
    update_reason = None if update is not None else OVERRIDE_REASON if overrides_finding(plan) else UNSCORED_REASON
    return {"schema_version": SCHEMA_VERSION, "kind": plan["kind"], "is_s1_turn": False, "model_calls": 0,
            "status": status, "finding_id": plan["finding_id"], "repro_check": plan["repro_check"],
            "repro_check_source": plan.get("repro_check_source"), "effect": plan.get("effect"),
            "reset": RESET, "clock_mode": CLOCK, "summary": summary, "attempts": attempts,
            "finding_update": update, "finding_update_reason": update_reason, "finished_at_utc": _utcnow()}


# --- command line --------------------------------------------------------------------------------


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="playtest-reproduce", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    planner = sub.add_parser("plan", help="list the inputs to replay and the check; makes no call")
    planner.add_argument("--trace", type=Path, required=True)
    planner.add_argument("--manifest", type=Path, required=True)
    planner.add_argument("--finding", default=None, help="finding_id; default: the latest candidate")
    planner.add_argument("--repro-check", type=Path, default=None, help="a JSON ReproCheck, if the finding has none")
    planner.add_argument("--finding-file", type=Path, default=None,
                         help="a JSON Finding recorded outside the trace, such as a post-session review's candidate")
    planner.add_argument("--attempts", type=int, default=ATTEMPTS)
    planner.add_argument("--settle-s", type=float, default=SETTLE_S)
    planner.add_argument("--output", type=Path, required=True)
    runner = sub.add_parser("run", help="replay the plan in a running PlayMode host")
    for name in ("project", "plan", "manifest", "output", "handshake-dir"):
        runner.add_argument(f"--{name}", type=Path, required=True)
    runner.add_argument("--reload-timeout", type=float, default=RELOAD_TIMEOUT_S)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.command == "plan":
        try:
            check = json.loads(args.repro_check.read_text(encoding="utf-8")) if args.repro_check else None
            record = json.loads(args.finding_file.read_text(encoding="utf-8")) if args.finding_file else None
            plan = build_plan(args.trace, args.manifest, finding_id=args.finding, finding_record=record, repro_check=check,
                              attempts=args.attempts, settle_s=args.settle_s)
        except (OSError, ValueError, KeyError) as exc:
            print(f"reproduce plan: {exc}", file=sys.stderr)
            return 1
        if args.output.exists():
            print("reproduce plan: the output must be new", file=sys.stderr)
            return 1
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(plan, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(f"plan: {len(plan['inputs'])} inputs before the evidence at trace_seq {max(plan['evidence_seq'])} "
              f"(turn {plan['evidence_turn']}), check {plan['repro_check']}")
        if plan["effect"] is not None:
            effect = plan["effect"]
            allowed = "unknown" if effect["allowance"] is None else f"{effect['allowance']:.3f}"
            print(f"effect: {effect['action']} calibrated {effect['expected_displacement']:.3f} {effect['unit']} "
                  f"(allowance {allowed}); scored at turn {effect['scored_turn']} of "
                  f"{effect['executions']} execution(s)")
        return 0
    project, output, handshake = args.project.resolve(), args.output.resolve(), args.handshake_dir.resolve()
    try:
        plan_bytes = args.plan.read_bytes()
        plan = json.loads(plan_bytes)
        manifest_bytes = args.manifest.read_bytes()
        if _sha(manifest_bytes) != plan["manifest_sha256"]:
            raise ValueError("the manifest is not the one the plan was made from")
        manifest = PreflightManifest.model_validate_json(manifest_bytes, strict=True)
        read_project_lock(project)
        if output.exists() or output.is_relative_to(project) or handshake.is_relative_to(project):
            raise ValueError("the report must be new, and it and the handshake must be outside the project")
        handshake.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, KeyError, BridgeLockError) as exc:
        print(f"reproduce run: {exc}", file=sys.stderr)
        return 1
    from playtest.runner.readers import FieldReaders

    async def reload(number: int) -> dict[str, Any]:
        return await request_reload(handshake, number, args.reload_timeout)

    async def connect() -> BridgeClient:
        return await BridgeClient.connect_from_project(project, timeout_s=CLIENT_TIMEOUT_S)

    report = asyncio.run(measure(plan, reload, connect, lambda client: FieldReaders(client, manifest.readers),
                                 lambda client: make_snapshot(client, manifest.entity_id)))
    report["plan_sha256"] = _sha(plan_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")
    s = report["summary"]
    print(f"reproduce: {report['status']} k={s['k']} n={s['n']} unknown={s['unknown']} attempted={s['attempted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
