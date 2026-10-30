"""Replay one arm over the shuffled-protocol corpus with a bounded ledger, and score it.

This is a robustness check of a single arm, not a comparison. It reuses the first replay's request
body, answer parser, transport and approval checks without editing them, because that run's plan
pins their source hash.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from playtest.bench import record as recording
from playtest.bench import replay
from playtest.bench.record import PASSES, context_for, digest
from playtest.bench.replay import Send, accepted, parse_answer, request_body, send_systemone, validate_approval
from playtest.core.context import canonical

ARM = "mammoth-1"
CONTROLS = 8
REPEATS = 5
MARGINAL = 0.6
COMPLETED = re.compile(r"^(c\d+) \(")
SOURCES = {"retest_source_sha256": Path(__file__), "replay_source_sha256": Path(replay.__file__),
           "record_source_sha256": Path(recording.__file__)}


def _expected_ids(pass_no: int) -> list[str]:
    return [f"pass-{pass_no:02d}-s{step}" for step in range(1, CONTROLS + 1)] + [f"pass-{pass_no:02d}-fin"]


def _permutation(value: Any, base: int) -> list[int]:
    if not isinstance(value, list) or len(value) != CONTROLS or not all(type(item) is int for item in value) \
            or sorted(value) != list(range(base, base + CONTROLS)):
        raise ValueError("a pass plan is not a permutation")
    return value


def completed_ids(frame: dict[str, Any]) -> set[str]:
    """The controls a frame's own facts report as checked."""
    return {match.group(1) for fact in frame["facts"] if (match := COMPLETED.match(fact))}


def load_corpus(path: Path) -> tuple[dict[str, Any], str]:
    corpus, corpus_sha = replay._load(path)
    protocol, frames = corpus.get("protocol"), corpus.get("frames")
    seed = protocol.get("seed") if isinstance(protocol, dict) else None
    if corpus.get("status") != "complete" or corpus.get("version") != 2 or not isinstance(protocol, dict) \
            or protocol.get("name") != "shuffled" or isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("a complete shuffled-protocol recording is required")
    passes = protocol.get("passes")
    if not isinstance(passes, list) or len(passes) != PASSES or not isinstance(frames, list) \
            or len(frames) != PASSES * (CONTROLS + 1):
        raise ValueError(f"the recording must hold {PASSES} passes of {CONTROLS + 1} frames")
    if corpus.get("stop", {}).get("release_verified") is not True:
        raise ValueError("the recording did not verify final input release")
    for key in ("game_commit", "repo_commit", "unity_version", "bridge_source_sha256", "python_source_sha256",
                "entity_sha256", "calibration_sha256", "instructions_sha256", "objective_sha256"):
        if not isinstance(corpus.get(key), str) or not corpus[key]:
            raise ValueError(f"the recording has no {key}")
    before, after = corpus.get("game_worktree_before"), corpus.get("game_worktree_after")
    if not isinstance(before, dict) or before != after or before.get("commit") != corpus["game_commit"]:
        raise ValueError("the game worktree is unknown or changed during recording")
    hold = corpus.get("requested_hold_s")
    if isinstance(hold, bool) or not isinstance(hold, (float, int)) or not math.isfinite(hold) or hold <= 0:
        raise ValueError("the recording has no valid requested hold")
    order = corpus.get("control_order")
    if not isinstance(order, list) or len(order) != CONTROLS or len(set(order)) != CONTROLS \
            or not all(isinstance(item, str) for item in order):
        raise ValueError("the recording has no valid control order")
    if [frame.get("id") for frame in frames] != [i for p in range(1, PASSES + 1) for i in _expected_ids(p)]:
        raise ValueError("frame ids do not follow the pass structure")
    ascending = list(range(1, CONTROLS + 1))
    for pass_no, plan in enumerate(passes, 1):
        if not isinstance(plan, dict) or plan.get("pass") != pass_no:
            raise ValueError("the pass plans are out of order")
        ids, visit = _permutation(plan.get("ids"), 1), _permutation(plan.get("visit"), 0)
        if ids == ascending or visit == list(range(CONTROLS)) or [ids[i] for i in visit] == ascending:
            raise ValueError(f"pass {pass_no} is not shuffled")
        expected_actions = {f"c{ids[i]}": order[i] for i in range(CONTROLS)}
        for step, frame in enumerate(frames[(pass_no - 1) * (CONTROLS + 1):pass_no * (CONTROLS + 1)]):
            if frame.get("pass") != pass_no or frame.get("payload_sha256") != digest(canonical(context_for(frame))):
                raise ValueError(f"context changed for {frame.get('id')}")
            offered = frame["choice_set"]["choices"]
            actions = {c["id"]: c.get("action") for c in offered if c.get("kind") == "action"}
            if actions != expected_actions or {c["id"] for c in offered if c.get("kind") != "action"} != {"wait", "finish"}:
                raise ValueError(f"the offered choices differ from the pass plan for {frame['id']}")
            terminal = step == CONTROLS
            if frame.get("kind") != ("terminal" if terminal else "check") or frame.get("position") != step + 1 \
                    or frame.get("scripted_choice_id") != (None if terminal else f"c{ids[visit[step]]}"):
                raise ValueError(f"{frame['id']} does not follow the pass plan")
            executions = frame.get("execution")
            if terminal:
                if executions is not None or frame.get("after_observation") is not None:
                    raise ValueError(f"{frame['id']} should not have held a control")
                continue
            if not isinstance(executions, list) or not executions or executions[-1].get("status") != "completed" \
                    or executions[-1].get("release_verified") is not True or frame.get("after_observation") is None:
                raise ValueError(f"recording evidence is incomplete for {frame['id']}")
            duration = executions[-1].get("applied_duration_s")
            if isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration) \
                    or duration <= 0:
                raise ValueError(f"the measured hold is unavailable or invalid for {frame['id']}")
    return corpus, corpus_sha


def load_labels(path: Path, corpus: dict[str, Any], corpus_sha: str) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], str]:
    document, label_sha = replay._load(path)
    if document.get("corpus_sha256") != corpus_sha or not isinstance(document.get("labels"), list):
        raise ValueError("labels are missing or bound to a different corpus")
    by_id = {frame["id"]: frame for frame in corpus["frames"]}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identities: set[tuple[str, str]] = set()
    for row in document["labels"]:
        frame = by_id.get(row.get("frame_id"))
        if frame is None or row.get("payload_sha256") != frame["payload_sha256"]:
            raise ValueError("a label refers to a missing or changed frame")
        accept = row.get("acceptable")
        offered = {choice["id"] for choice in frame["choice_set"]["choices"]}
        if not isinstance(accept, list) or not accept or len(set(accept)) != len(accept) or not set(accept) <= offered:
            raise ValueError(f"invalid accept-list for {frame['id']}")
        if not all(isinstance(row.get(key), str) and row[key].strip() for key in ("labeller", "date", "rationale")):
            raise ValueError(f"label attribution is incomplete for {frame['id']}")
        try:
            dt.date.fromisoformat(row["date"])
        except ValueError as exc:
            raise ValueError(f"invalid label date for {frame['id']}") from exc
        identity = (row["frame_id"], row["labeller"].strip())
        if identity in identities:
            raise ValueError(f"duplicate label by the same labeller for {frame['id']}")
        identities.add(identity)
        grouped[row["frame_id"]].append(row)
    if set(grouped) != set(by_id):
        raise ValueError("every frame needs at least one attributed label")
    return document, dict(grouped), label_sha


def draft_labels(corpus_path: Path, output: Path, *, date: str) -> int:
    """Write the mechanical draft: unchecked controls on a check frame, `finish` on the terminal frame."""
    corpus, corpus_sha = load_corpus(corpus_path)
    dt.date.fromisoformat(date)
    rows = []
    for frame in corpus["frames"]:
        earlier = [f for f in corpus["frames"] if f["pass"] == frame["pass"]][:frame["position"] - 1]
        done = completed_ids(frame)
        if {f["scripted_choice_id"] for f in earlier} != done:
            raise ValueError(f"recorded executions and facts disagree for {frame['id']}")
        controls = sorted((c["id"] for c in frame["choice_set"]["choices"] if c["kind"] == "action"),
                          key=lambda item: int(item[1:]))
        unchecked = [item for item in controls if item not in done]
        checked = ", ".join(sorted(done, key=lambda item: int(item[1:]))) or "none"
        if frame["kind"] == "terminal":
            if unchecked:
                raise ValueError(f"{frame['id']} is terminal but controls are unchecked")
            acceptable = ["finish"]
            rationale = (f"Pass {frame['pass']}: completed checks are {checked}. Every control is checked, so "
                         "finishing completes the stated checklist; repeating a control or waiting would not advance it.")
        else:
            if not unchecked:
                raise ValueError(f"{frame['id']} is a check frame with nothing unchecked")
            acceptable = unchecked
            rationale = (f"Pass {frame['pass']}, step {frame['position']}: completed checks are {checked}. The unchecked "
                         f"controls {', '.join(unchecked)} advance the stated checklist; repeating a completed control, "
                         "waiting or finishing would not.")
        rows.append({"frame_id": frame["id"], "payload_sha256": frame["payload_sha256"], "acceptable": acceptable,
                     "labeller": "Claude Sonnet 5.5 (assistant draft; mechanical rule from the recorded facts)",
                     "date": date, "rationale": rationale})
    document = {"version": 2, "corpus_sha256": corpus_sha,
                "authorship": {"kind": "assistant_draft", "author": "Claude Sonnet 5.5", "human_review": "pending",
                               "reviewer": None, "reviewed_at": None},
                "labels": rows}
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return len(rows)


def schedule_for(corpus: dict[str, Any], seed: int) -> tuple[list[tuple[dict[str, Any], int]], list[str]]:
    """Every frame once, then a seeded sample of frames a second time, in a seeded order."""
    frames = corpus["frames"]
    by_id = {frame["id"]: frame for frame in frames}
    repeated = sorted(random.Random(f"s1-bench-retest-repeat:{seed}").sample(sorted(by_id), REPEATS))
    entries = [(frame, 0) for frame in frames] + [(by_id[frame_id], 1) for frame_id in repeated]
    random.Random(f"s1-bench-retest-order:{seed}").shuffle(entries)
    return entries, repeated


def run(corpus_path: Path, labels_path: Path, approval_path: Path, directory: Path, *, seed: int,
        send: Send = send_systemone, arm: str = ARM) -> int:
    if arm not in replay.ARMS:
        raise ValueError(f"unknown S1 arm {arm!r}")
    corpus, corpus_sha = load_corpus(corpus_path)
    _, _, label_sha = load_labels(labels_path, corpus, corpus_sha)
    approval, approval_sha = replay._load(approval_path)
    validate_approval(approval, [arm])
    entries, repeated = schedule_for(corpus, seed)
    limit = approval["arms"][arm]
    bound = replay._decimal(limit["per_call_max_usd"], "per-call bound")
    if len(entries) > approval["max_calls"] or len(entries) > limit["max_calls"] \
            or bound * len(entries) > replay._decimal(approval["max_cost_usd"], "overall limit") \
            or bound * len(entries) > replay._decimal(limit["max_cost_usd"], "arm limit"):
        raise ValueError("the approved call or cost envelope is smaller than the schedule")
    design = Path(__file__).resolve().parents[3] / "docs" / "design" / "s1-bench.md"
    plan = {"version": 1, "protocol": "shuffled-retest", "arm": arm, "corpus_sha256": corpus_sha,
            "labels_sha256": label_sha, "approval_sha256": approval_sha,
            **{key: digest(path.read_bytes()) for key, path in SOURCES.items()},
            "design_sha256": digest(design.read_bytes()) if design.is_file() else None,
            "seed": seed, "scheduled_calls": len(entries), "repeated_frames": repeated,
            "budget_assumptions": {"balance_override": approval.get("unverified_balance_override"),
                                   "unverified_price_arms": [arm] if limit.get("allow_unverified_price") is True else []}}
    directory.mkdir(parents=True, exist_ok=True)
    call_dir = directory / "calls"
    call_dir.mkdir(exist_ok=True)
    plan_path = directory / "plan.json"
    if plan_path.exists():
        saved = replay._load(plan_path)[0]
        if {k: v for k, v in saved.items() if k != "design_sha256"} != {k: v for k, v in plan.items() if k != "design_sha256"}:
            raise ValueError("the saved run plan differs; do not mix frozen inputs")
    else:
        replay._write(plan_path, plan, exclusive=True)
    rows = replay._calls(call_dir)
    if any(row.get("status") == "in_flight" for row in rows):
        raise ValueError("a prior call has unknown completion/billing; reconcile it before resuming")
    if any(row.get("cost_bound_exceeded") is True for row in rows):
        raise ValueError("a prior call exceeded its cost bound; reconcile the envelope before resuming")
    if any(row.get("http_status") != 200 or row.get("choice") is None for row in rows):
        return 0  # A failed arm stays stopped; it is never retried.
    completed = {row["slot"] for row in rows}
    made = 0
    for index, (frame, repeat) in enumerate(entries):
        body = request_body(frame, replay.ARMS[arm][0])
        request_sha = digest(canonical(body))
        phase = "sweep" if repeat == 0 else "repeat"
        slot = digest(canonical({"phase": phase, "arm": arm, "frame": frame["id"], "repeat": repeat,
                                 "request_sha256": request_sha}))
        if slot in completed:
            continue
        if dt.datetime.now(dt.timezone.utc) >= replay._price_deadline(limit, arm):
            raise ValueError(f"{arm} pricing validity expired during the run; stopped before another call")
        total_bound = sum((Decimal(str(row["reserved_usd"])) for row in rows), Decimal(0))
        if len(rows) >= approval["max_calls"] or len(rows) >= limit["max_calls"] \
                or total_bound + bound > replay._decimal(approval["max_cost_usd"], "overall limit") \
                or total_bound + bound > replay._decimal(limit["max_cost_usd"], "arm limit"):
            raise ValueError("the approved call or cost envelope would be exceeded")
        row = {"slot": slot, "phase": phase, "arm": arm, "frame_id": frame["id"], "repeat": repeat,
               "schedule_index": index, "payload_sha256": frame["payload_sha256"], "request_sha256": request_sha,
               "requested_model": replay.ARMS[arm][0], "reserved_usd": str(bound), "status": "in_flight"}
        path = call_dir / f"{slot}.json"
        replay._write(path, row, exclusive=True)  # Reserve before the POST; a crash must not cause a retry.
        try:
            http_status, data, latency_ms, problem = send(arm, body)
            offered = {choice["id"] for choice in frame["choice_set"]["choices"]}
            choice, distribution, answer_problem = parse_answer(data if http_status == 200 else None, offered)
            usage = (data or {}).get("usage")
            raw_cost = usage.get("cost") if isinstance(usage, dict) else None
            cost = (float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                    and math.isfinite(raw_cost) and raw_cost >= 0 else None)
            row.update(status="finished", http_status=http_status, choice=choice, distribution=distribution,
                       problem=problem or answer_problem, latency_ms=latency_ms,
                       resolved_model=(data or {}).get("model"), usage=usage,
                       provider_answer=((data or {}).get("answers") or {}).get("next_action"),
                       billed=(cost > 0 if cost is not None else None), cost_usd=cost,
                       cost_bound_exceeded=(Decimal(str(cost)) > bound if cost is not None else False))
        except Exception as exc:  # Preserve the reservation when a transport adapter fails.
            row.update(status="finished", http_status=None, choice=None, distribution=None,
                       problem=f"adapter error: {type(exc).__name__}", latency_ms=None,
                       resolved_model=None, usage=None, billed=None, cost_usd=None)
        replay._write(path, row)
        rows.append(row)
        completed.add(slot)
        made += 1
        if row.get("cost_bound_exceeded") is True:
            raise ValueError("a provider-reported cost exceeded the per-call bound; stopped")
        if row["http_status"] != 200 or row.get("choice") is None:
            break  # Transport, auth, billing, rate-limit or server failure: no further requests.
    return made


def _tally(items: list[dict[str, Any]]) -> dict[str, int]:
    scored = [item["acceptable"] for item in items if item["acceptable"] is not None]
    return {"hits": sum(scored), "scored": len(scored), "frames": len(items)}


def score(corpus_path: Path, labels_path: Path, directory: Path) -> dict[str, Any]:
    corpus, corpus_sha = load_corpus(corpus_path)
    document, labels, label_sha = load_labels(labels_path, corpus, corpus_sha)
    plan = replay._load(directory / "plan.json")[0]
    arm = plan.get("arm", ARM)  # the arm the run was planned for
    if plan.get("corpus_sha256") != corpus_sha or plan.get("labels_sha256") != label_sha:
        raise ValueError("the saved calls do not match the frozen corpus and labels")
    for key, path in SOURCES.items():
        if plan.get(key) != digest(path.read_bytes()):
            raise ValueError(f"{path.name} changed since the run was planned")
    rows = replay._calls(directory / "calls")
    frames = {frame["id"]: frame for frame in corpus["frames"]}
    for row in rows:
        frame = frames.get(row.get("frame_id"))
        if frame is None or row.get("payload_sha256") != frame["payload_sha256"] or row.get("arm") != arm:
            raise ValueError("a call does not belong to the frozen run")
    sweep_rows = [row for row in rows if row.get("phase") == "sweep"]
    sweep = {row["frame_id"]: row for row in sweep_rows}
    if len(sweep) != len(sweep_rows):
        raise ValueError("duplicate sweep calls for a frame")
    per_frame = []
    for frame in corpus["frames"]:
        row, valid = sweep.get(frame["id"]), accepted(labels[frame["id"]])
        choice = row.get("choice") if row else None
        distribution = row.get("distribution") if row and isinstance(row.get("distribution"), dict) else None
        hit = (choice in valid) if valid is not None and choice is not None else None
        done, kind = completed_ids(frame), None
        if hit is False:
            if frame["kind"] == "terminal":
                kind = "wait" if choice == "wait" else "missed-finish"
            else:
                kind = "re-check" if choice in done else {"finish": "premature-finish", "wait": "wait"}.get(choice, "other")
        per_frame.append({
            "frame_id": frame["id"], "pass": frame["pass"], "position": frame["position"], "kind": frame["kind"],
            "completed": len(done), "choice": choice, "acceptable": hit, "miss": kind,
            "acceptable_mass": sum(distribution[item] for item in valid) if valid is not None and distribution else None,
            "top_probability": max(distribution.values()) if distribution else None})
    check = [item for item in per_frame if item["kind"] == "check"]
    terminal = [item for item in per_frame if item["kind"] == "terminal"]
    masses = [item["acceptable_mass"] for item in per_frame if item["acceptable_mass"] is not None]
    prefix = [item for item in check if item["completed"] >= 1]
    prefix_hits = sum(completed_ids(frames[item["frame_id"]]) == {f"c{i}" for i in range(1, item["completed"] + 1)}
                      for item in prefix)
    repeated = []
    for row in (r for r in rows if r.get("phase") == "repeat"):
        first = sweep.get(row["frame_id"])
        if first is None or first.get("distribution") is None or row.get("distribution") is None:
            repeated.append({"frame_id": row["frame_id"], "comparable": False})
            continue
        spread = max(abs(first["distribution"][k] - row["distribution"][k]) for k in first["distribution"])
        repeated.append({"frame_id": row["frame_id"], "comparable": True,
                         "same_choice": first["choice"] == row["choice"],
                         "identical_distribution": first["distribution"] == row["distribution"],
                         "max_probability_difference": spread})
    latencies = sorted(row["latency_ms"] for row in sweep_rows if isinstance(row.get("latency_ms"), (int, float)))
    costs = [row.get("cost_usd") for row in rows]
    durations = [f["execution"][-1]["applied_duration_s"] for f in corpus["frames"] if f["kind"] == "check"]
    succeeded = sum(item["choice"] is not None for item in per_frame)
    return {
        "arm": arm, "protocol_seed": corpus["protocol"]["seed"],
        "label_authorship": document.get("authorship", {"kind": "unknown"}),
        "label_disagreements": [fid for fid, entries in labels.items() if accepted(entries) is None],
        "budget_assumptions": plan.get("budget_assumptions"),
        "resolved_models": sorted({str(row.get("resolved_model")) for row in rows}),
        "recording_hold_s": {"requested": corpus["requested_hold_s"], "min": min(durations),
                             "median": statistics.median(durations), "max": max(durations)},
        "frames": len(per_frame), "attempted": len(sweep_rows), "succeeded": succeeded,
        "scored": sum(item["acceptable"] is not None for item in per_frame),
        "unknown": sum(item["acceptable"] is None for item in per_frame),
        "not_attempted": len(per_frame) - len(sweep_rows),
        "top1_acceptable": {"all": _tally(per_frame), "check": _tally(check), "terminal": _tally(terminal)},
        "by_position": {str(p): _tally([i for i in per_frame if i["position"] == p]) for p in range(1, CONTROLS + 2)},
        "by_pass": {str(p): _tally([i for i in per_frame if i["pass"] == p]) for p in range(1, PASSES + 1)},
        "acceptable_mass": {"n": len(masses), "mean": statistics.mean(masses) if masses else None,
                            "min": min(masses) if masses else None,
                            "marginal_hits": [i["frame_id"] for i in per_frame
                                              if i["acceptable"] and i["acceptable_mass"] is not None
                                              and i["acceptable_mass"] < MARGINAL]},
        "misses": [{k: i[k] for k in ("frame_id", "kind", "miss", "choice", "acceptable_mass")}
                   for i in per_frame if i["acceptable"] is False],
        "miss_kinds": dict(Counter(i["miss"] for i in per_frame if i["miss"])),
        "completed_set_is_id_prefix": {"frames": len(prefix), "prefix": prefix_hits},
        "determinism": {"repeated": len(repeated), "comparable": sum(r["comparable"] for r in repeated),
                        "same_choice": sum(r.get("same_choice") is True for r in repeated),
                        "identical_distribution": sum(r.get("identical_distribution") is True for r in repeated),
                        "max_probability_difference": max((r["max_probability_difference"] for r in repeated
                                                           if r["comparable"]), default=None),
                        "frames": repeated},
        "latency_p50_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
        "latency_note": "measured with a clock that steps in about 15.6 ms",
        "cost": {"calls_reporting_cost": sum(isinstance(c, (int, float)) for c in costs),
                 "cost_usd": sum(c for c in costs if isinstance(c, (int, float)))
                 if costs and all(isinstance(c, (int, float)) for c in costs) else None},
        "per_frame": per_frame,
        "interpretation": "not computed; a person reads the misses against the predeclared rule in docs/design/s1-bench.md",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    labeller = sub.add_parser("labels", help="write the mechanical draft labels for a recorded corpus")
    labeller.add_argument("--corpus", type=Path, required=True)
    labeller.add_argument("--output", type=Path, required=True)
    labeller.add_argument("--date", required=True, help="ISO date to record on the labels")
    runner = sub.add_parser("run", help="make the approved calls")
    for name in ("corpus", "labels", "approval", "directory"):
        runner.add_argument(f"--{name}", type=Path, required=True)
    runner.add_argument("--seed", type=int, required=True)
    runner.add_argument("--arm", choices=tuple(replay.ARMS), default=ARM, help=f"the S1 arm (default {ARM})")
    reporter = sub.add_parser("score", help="score saved calls; makes no call")
    for name in ("corpus", "labels", "directory"):
        reporter.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "labels":
            print(f"wrote {draft_labels(args.corpus, args.output, date=args.date)} draft labels")
        elif args.command == "run":
            print(f"new calls: {run(args.corpus, args.labels, args.approval, args.directory, seed=args.seed, arm=args.arm)}")
        else:
            print(json.dumps(score(args.corpus, args.labels, args.directory), indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"retest: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
