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

The check is scored with the shared evaluator (`roles.checks.evaluate`), first on the baseline:
- **unknown:** the check already held after the reset, before any input, so the replay cannot show it (an
  `unchanged` check always does), or the baseline could not be scored;
- **reproduced:** the baseline did not satisfy it, and some later reading did;
- **not reproduced:** later readings were scored and none satisfied it;
- **unknown:** no later reading could be scored.

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

from playtest.contracts.investigation import Comparator, Finding, FindingState, Repro, ReproCheck
from playtest.contracts.manifest import PreflightManifest
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError, read_project_lock
from playtest.roles import checks

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


def build_plan(trace_path: Path, manifest_path: Path, *, finding_id: str | None = None,
               repro_check: Mapping[str, Any] | None = None, attempts: int = ATTEMPTS, settle_s: float = SETTLE_S,
               start_settle_s: float = START_SETTLE_S, sample_interval_s: float = SAMPLE_INTERVAL_S) -> dict[str, Any]:
    raw = trace_path.read_bytes()
    entries = read_trace(trace_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest = PreflightManifest.model_validate_json(manifest_bytes, strict=True)
    findings = latest_findings(entries)
    if finding_id is None:
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
    alias = check.field.removeprefix("public.")
    kinds = {reader.alias: reader.kind for reader in manifest.readers}
    if alias not in kinds:
        raise ValueError(f"repro_check field {check.field!r} is not one of the manifest's readers")
    through = evidence_turn(entries, finding.evidence_seq)
    last_evidence = max(finding.evidence_seq)
    inputs, skipped = recorded_inputs(entries, last_evidence)
    if not inputs:
        raise ValueError("no input completed before the finding's evidence; there is nothing to replay")
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
        "repro_check_kind": kinds[alias],
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


async def run_attempt(plan: Mapping[str, Any], number: int, reload: Callable[[int], Awaitable[dict[str, Any]]],
                      connect: Callable[[], Awaitable[Any]], readers_for: Callable[[Any], Any]) -> dict[str, Any]:
    check = ReproCheck.model_validate(plan["repro_check"])
    kind = plan.get("repro_check_kind")
    alias = check.field.removeprefix("public.")
    attempt: dict[str, Any] = {"number": number, "valid": False, "result": None, "reason": None, "reload": None,
                               "baseline": None, "baseline_satisfied": None, "inputs": [], "readings": [],
                               "error": None, "stop": None}
    client = None
    loop = asyncio.get_running_loop()
    try:
        attempt["reload"] = await reload(number)
        client = await connect()
        readers = readers_for(client)
        await readers.install()
        await asyncio.sleep(plan["start_settle_s"])
        first = await readers.read()
        baseline = (getattr(first, "values", {}) or {}).get(alias)
        attempt["baseline"] = baseline
        attempt["baseline_satisfied"] = score(check, kind, baseline, baseline) if baseline is not None else None
        started = loop.time()
        for item in plan["inputs"]:
            delay = started + item["offset_s"] - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
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
            sample = await readers.read()
            attempt["readings"].append(_reading(sample, alias, check, kind, baseline, f"after turn {item['turn']}",
                                                loop.time() - started))
        else:
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


def summarize(plan: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [a for a in attempts if a["valid"]]
    k = sum(a["result"] == "reproduced" for a in valid)
    unknown = sum(a["result"] == "unknown" for a in valid)
    n = len(valid) - unknown
    deviations = [abs(row["deviation_s"]) for a in attempts for row in a["inputs"]]
    return {"planned": plan["attempts"], "attempted": len(attempts), "valid": len(valid), "k": k, "n": n,
            "unknown": unknown,
            "basis": ("k counts valid attempts where the repro_check did not hold after the reset and a later "
                      "reading satisfied it; n counts valid attempts that scored the baseline as not holding and "
                      "at least one later reading. Unknown attempts (including a check that already held after "
                      "the reset) and invalid (diverged or failed) attempts are in neither"),
            "invalid": [{"number": a["number"], "reason": a["reason"]} for a in attempts if not a["valid"]],
            "input_timing": {"max_abs_deviation_s": round(max(deviations), 3) if deviations else None,
                             "inputs_replayed": len(deviations)}}


def finding_update(plan: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any] | None:
    """The finding's next state: reproduced when k >= 1, repro_attempted when k = 0 of n >= 1, none otherwise."""
    if summary["n"] < 1:
        return None
    timing = summary["input_timing"]["max_abs_deviation_s"]
    check = plan["repro_check"]
    tolerance = (f"repro_check tolerance {check.get('tolerance')}; replayed inputs started within "
                 f"{timing} s of their recorded offsets")
    finding = dict(plan["finding"])
    finding["state"] = (FindingState.reproduced if summary["k"] >= 1 else FindingState.repro_attempted).value
    finding["repro"] = Repro(attempts=summary["n"], reproduced=summary["k"], clock_mode=CLOCK, reset=RESET,
                             tolerance=tolerance).model_dump(mode="json")
    return Finding.model_validate(finding).model_dump(mode="json")


async def measure(plan: Mapping[str, Any], reload: Callable[[int], Awaitable[dict[str, Any]]],
                  connect: Callable[[], Awaitable[Any]], readers_for: Callable[[Any], Any]) -> dict[str, Any]:
    attempts = []
    for number in range(1, plan["attempts"] + 1):
        attempt = await run_attempt(plan, number, reload, connect, readers_for)
        attempts.append(attempt)
        if attempt["error"] is not None and attempt["reload"] is None:
            break  # the reset itself failed: no further attempt can start from a known state
    summary = summarize(plan, attempts)
    status = ("measured" if summary["valid"] == plan["attempts"] and summary["unknown"] == 0
              else "inconclusive" if summary["n"] == 0 else "measured_with_gaps")
    return {"schema_version": SCHEMA_VERSION, "kind": plan["kind"], "is_s1_turn": False, "model_calls": 0,
            "status": status, "finding_id": plan["finding_id"], "repro_check": plan["repro_check"],
            "reset": RESET, "clock_mode": CLOCK, "summary": summary, "attempts": attempts,
            "finding_update": finding_update(plan, summary), "finished_at_utc": _utcnow()}


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
            plan = build_plan(args.trace, args.manifest, finding_id=args.finding, repro_check=check,
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

    report = asyncio.run(measure(plan, reload, connect, lambda client: FieldReaders(client, manifest.readers)))
    report["plan_sha256"] = _sha(plan_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")
    s = report["summary"]
    print(f"reproduce: {report['status']} k={s['k']} n={s['n']} unknown={s['unknown']} attempted={s['attempted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
