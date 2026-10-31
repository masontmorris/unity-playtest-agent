"""Replay frozen S1 contexts with a bounded ledger and report paired outcomes."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from playtest.bench.record import _save, digest, recorded_payload
from playtest.core.context import canonical

# Arm: (model, key variable, endpoint variable). Liquid's endpoint is its general decisions endpoint and the
# request's `model` picks the model, so both Liquid arms share LIQUID_D1.1_URL.
ARMS = {
    "mammoth-1": ("decision-models/baseten-mammoth-27b-latest", "BASETEN_API_KEY", None),
    "elephant-1": ("decision-models/baseten-elephant-9b-latest", "BASETEN_API_KEY", None),
    "liquid-d1.1": ("d1.1", "LIQUID_API_KEY", "LIQUID_D1.1_URL"),
    "liquid-d1-ultra": ("d1-ultra", "LIQUID_API_KEY", "LIQUID_D1.1_URL"),
    "meta-muse-spark-s1": ("muse-spark-1.3", "META_API_KEY", None),
}
BASETEN_URL = "https://inference.baseten.co/v1/systemone"


def provider_of(arm: str) -> str:
    """The provider an arm bills through: the key a balance check and an approval cap are counted against."""
    if arm in {"muse-spark-1.3", "meta-muse-spark-s1"}:
        return "meta"
    return "liquid" if arm.startswith("liquid-") else "baseten"
Send = Callable[[str, dict[str, Any]], tuple[int | None, dict[str, Any] | None, float, str | None]]


def _load(path: Path) -> tuple[dict[str, Any], str]:
    data = path.read_bytes()
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value, digest(data)


def load_inputs(corpus_path: Path, labels_path: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], str, str]:
    corpus, corpus_sha = _load(corpus_path)
    labels, label_sha = _load(labels_path)
    frames = corpus.get("frames")
    if corpus.get("status") != "complete" or not isinstance(frames, list) or len(frames) != 40:
        raise ValueError("a complete 40-frame recording is required")
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
    if labels.get("corpus_sha256") != corpus_sha or not isinstance(labels.get("labels"), list):
        raise ValueError("labels are missing or bound to a different corpus")
    by_id = {}
    for frame in frames:
        frame_id = frame.get("id")
        if not isinstance(frame_id, str) or not frame_id or frame_id in by_id:
            raise ValueError("frame ids must be unique and nonempty")
        if recorded_payload(frame) is None:
            raise ValueError(f"context changed for {frame_id}")
        executions = frame.get("execution")
        if not isinstance(executions, list) or not executions or executions[-1].get("status") != "completed" or \
                executions[-1].get("release_verified") is not True or frame.get("after_observation") is None:
            raise ValueError(f"recording evidence is incomplete for {frame_id}")
        duration = executions[-1].get("applied_duration_s")
        if (isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration)
                or duration <= 0):
            raise ValueError(f"the measured hold is unavailable or invalid for {frame_id}")
        by_id[frame_id] = frame
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identities: set[tuple[str, str]] = set()
    for row in labels["labels"]:
        frame_id = row.get("frame_id")
        frame = by_id.get(frame_id)
        if frame is None or row.get("payload_sha256") != frame["payload_sha256"]:
            raise ValueError("a label refers to a missing or changed frame")
        accepted = row.get("acceptable")
        choice_ids = {choice["id"] for choice in frame["choice_set"]["choices"]}
        if not isinstance(accepted, list) or not accepted or len(set(accepted)) != len(accepted) or \
                not set(accepted) <= choice_ids:
            raise ValueError(f"invalid accept-list for {frame_id}")
        if not all(isinstance(row.get(key), str) and row[key].strip() for key in ("labeller", "date", "rationale")):
            raise ValueError(f"label attribution is incomplete for {frame_id}")
        try:
            dt.date.fromisoformat(row["date"])
        except ValueError as exc:
            raise ValueError(f"invalid label date for {frame_id}") from exc
        identity = (frame_id, row["labeller"].strip())
        if identity in identities:
            raise ValueError(f"duplicate label by the same labeller for {frame_id}")
        identities.add(identity)
        grouped[frame_id].append(row)
    if set(grouped) != set(by_id):
        raise ValueError("every frame needs at least one attributed label")
    return corpus, dict(grouped), corpus_sha, label_sha


def accepted(labels: list[dict[str, Any]]) -> set[str] | None:
    sets = [set(row["acceptable"]) for row in labels]
    return sets[0] if all(item == sets[0] for item in sets[1:]) else None


def request_body(frame: dict[str, Any], model: str) -> dict[str, Any]:
    payload = recorded_payload(frame)
    if payload is None:
        raise ValueError("the production context no longer matches the recorded payload")
    criteria = {choice["id"]: choice["label"] for choice in payload["choices"]}
    return {"model": model, "questions": {"next_action": {
        "type": "choice", "instructions": payload["instructions"], "criteria": criteria}}, "state": payload}


def parse_answer(data: dict[str, Any] | None, offered: set[str]) -> tuple[str | None, dict[str, float] | None, str | None]:
    answer = ((data or {}).get("answers") or {}).get("next_action")
    if not isinstance(answer, dict) or answer.get("type") != "choice" or answer.get("choice") not in offered:
        return None, None, "invalid or unoffered choice"
    selected = answer["choice"]
    probabilities = answer.get("probabilities")
    if probabilities is None:
        return selected, None, None
    if not isinstance(probabilities, dict) or set(probabilities) != offered or any(
        isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or p < 0 or p > 1
        for p in probabilities.values()
    ) or abs(sum(probabilities.values()) - 1.0) > 0.01:
        return selected, None, "invalid distribution"
    return selected, probabilities, None


def send_systemone(arm: str, body: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None, float, str | None]:
    if arm == "meta-muse-spark-s1":
        from playtest.frontier.provider import send_meta
        return send_meta(body)
    _, key_env, url_env = ARMS[arm]
    key, url = os.environ.get(key_env), (os.environ.get(url_env) if url_env else BASETEN_URL)
    if not key or not url:
        raise ValueError(f"missing endpoint or credential for {arm}")
    request = urllib.request.Request(url, data=canonical(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None, (time.monotonic() - started) * 1000, "HTTP error"
    except (OSError, urllib.error.URLError):
        return None, None, (time.monotonic() - started) * 1000, "transport error"
    latency = (time.monotonic() - started) * 1000
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return status, None, latency, "response was not JSON"
    return status, data if isinstance(data, dict) else None, latency, None


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def _price_deadline(limit: dict[str, Any], arm: str) -> dt.datetime:
    value = limit.get("price_valid_until_utc")
    if value is None and limit.get("allow_unverified_price") is True:
        if not isinstance(limit.get("estimated_price_basis"), str) or not limit["estimated_price_basis"].strip():
            raise ValueError(f"{arm} needs the basis for its unverified price estimate")
        value = limit.get("estimate_use_by_utc")
    if not isinstance(value, str):
        raise ValueError(f"{arm} needs a pricing validity deadline")
    try:
        deadline = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{arm} has an invalid pricing validity deadline") from exc
    if deadline.tzinfo is None:
        raise ValueError(f"{arm} pricing deadline needs a timezone")
    return deadline.astimezone(dt.timezone.utc)


def validate_approval(approval: dict[str, Any], arms: list[str]) -> None:
    if not isinstance(approval.get("approved_by"), str) or not approval["approved_by"].strip() or \
            not isinstance(approval.get("approved_at_utc"), str) or not approval["approved_at_utc"].strip():
        raise ValueError("the user's recorded approval and timestamp are required")
    if not isinstance(approval.get("max_calls"), int) or isinstance(approval["max_calls"], bool) or approval["max_calls"] < 1:
        raise ValueError("approval needs a positive overall call limit")
    _decimal(approval.get("max_cost_usd"), "overall cost limit")
    limits = approval.get("arms")
    if not isinstance(limits, dict):
        raise ValueError("approval needs per-arm limits and verified balances")
    provider_caps: dict[str, Decimal] = defaultdict(Decimal)
    provider_balances: dict[str, list[Decimal]] = defaultdict(list)
    override = approval.get("unverified_balance_override")
    balance_override = (isinstance(override, dict) and override.get("approved_by_user") is True and
                        isinstance(override.get("reason"), str) and bool(override["reason"].strip()))
    for arm in arms:
        limit = limits.get(arm)
        if not isinstance(limit, dict) or not isinstance(limit.get("max_calls"), int) or \
                isinstance(limit.get("max_calls"), bool) or limit["max_calls"] < 1:
            raise ValueError(f"{arm} needs a positive call limit")
        bound = _decimal(limit.get("per_call_max_usd"), f"{arm} per-call bound")
        maximum = _decimal(limit.get("max_cost_usd"), f"{arm} cost limit")
        balance = None
        if limit.get("verified_balance_usd") is None and not limit.get("balance_checked_at_utc"):
            if not balance_override:
                raise ValueError(f"{arm} needs a verified balance or explicit user override")
        else:
            balance = _decimal(limit.get("verified_balance_usd"), f"{arm} verified balance")
            if not isinstance(limit.get("balance_checked_at_utc"), str) or not limit["balance_checked_at_utc"].strip():
                raise ValueError(f"{arm} needs a balance-check timestamp")
        if (balance is not None and maximum > balance) or bound > maximum:
            raise ValueError(f"{arm} exceeds its verified balance or per-arm envelope")
        if dt.datetime.now(dt.timezone.utc) >= _price_deadline(limit, arm):
            raise ValueError(f"{arm} pricing validity has expired; verify the rate again")
        provider = provider_of(arm)
        provider_caps[provider] += maximum
        if balance is not None:
            provider_balances[provider].append(balance)
    for provider, maximum in provider_caps.items():
        if provider_balances[provider] and maximum > min(provider_balances[provider]):
            raise ValueError(f"{provider} arm limits exceed their shared verified balance")


def _write(path: Path, data: dict[str, Any], *, exclusive: bool = False) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        return
    _save(path, data)


def _calls(directory: Path) -> list[dict[str, Any]]:
    return [_load(path)[0] for path in sorted(directory.glob("*.json"))]


def run(corpus_path: Path, labels_path: Path, approval_path: Path, directory: Path, *, phase: str,
        arms: list[str], seed: int, send: Send = send_systemone) -> int:
    corpus, labels, corpus_sha, label_sha = load_inputs(corpus_path, labels_path)
    approval, approval_sha = _load(approval_path)
    if not arms or len(set(arms)) != len(arms) or any(arm not in ARMS for arm in arms):
        raise ValueError("choose distinct supported arms")
    validate_approval(approval, arms)
    if phase not in {"noise", "sweep"}:
        raise ValueError("phase must be noise or sweep")
    directory.mkdir(parents=True, exist_ok=True)
    call_dir = directory / "calls"
    call_dir.mkdir(exist_ok=True)
    plan = {"version": 1, "corpus_sha256": corpus_sha, "labels_sha256": label_sha,
            "approval_sha256": approval_sha, "replay_source_sha256": digest(Path(__file__).read_bytes()),
            "arms": arms, "seed": seed,
            "budget_assumptions": {"balance_override": approval.get("unverified_balance_override"),
                "unverified_price_arms": [arm for arm in arms if approval["arms"][arm].get("allow_unverified_price") is True]}}
    plan_path = directory / "plan.json"
    if plan_path.exists():
        if _load(plan_path)[0] != plan:
            raise ValueError("the saved run plan differs; do not mix frozen inputs")
    else:
        _write(plan_path, plan, exclusive=True)
    rows = _calls(call_dir)
    if any(row.get("status") == "in_flight" for row in rows):
        raise ValueError("a prior call has unknown completion/billing; reconcile it before resuming")
    if any(row.get("cost_bound_exceeded") is True for row in rows):
        raise ValueError("a prior call exceeded its cost bound; reconcile the envelope before resuming")
    # A failed arm stays stopped; healthy arms can still complete their predeclared work.
    blocked = {row["arm"] for row in rows if row.get("http_status") != 200 or row.get("choice") is None}
    if phase == "sweep":
        for arm in arms:
            if arm in blocked:
                continue
            for frame in corpus["frames"][:10]:
                matching = [row for row in rows if row.get("phase") == "noise" and row.get("arm") == arm
                            and row.get("frame_id") == frame["id"]]
                if len(matching) != 5 or sum(row.get("choice") is not None for row in matching) < 2:
                    raise ValueError("the predeclared noise repeats have not been measured for every arm/frame")
    repetitions = 5 if phase == "noise" else 1
    frames = corpus["frames"][:10] if phase == "noise" else corpus["frames"]
    schedule = [(arm, frame, repeat) for arm in arms for frame in frames for repeat in range(repetitions)]
    random.Random(seed + (0 if phase == "noise" else 1)).shuffle(schedule)
    # A transport, auth, billing, rate-limit or server failure stops that arm. In particular,
    # an unknown HTTP outcome must not be followed by another potentially billed request.
    completed = {row["slot"] for row in rows}
    made = 0
    for schedule_index, (arm, frame, repeat) in enumerate(schedule):
        body = request_body(frame, ARMS[arm][0])
        request_sha = digest(canonical(body))
        slot = digest(canonical({"phase": phase, "arm": arm, "frame": frame["id"],
                                 "repeat": repeat, "request_sha256": request_sha}))
        if slot in completed or arm in blocked:
            continue
        if dt.datetime.now(dt.timezone.utc) >= _price_deadline(approval["arms"][arm], arm):
            raise ValueError(f"{arm} pricing validity expired during the run; stopped before another call")
        bound = _decimal(approval["arms"][arm]["per_call_max_usd"], "per-call bound")
        total_bound = sum((Decimal(str(row["reserved_usd"])) for row in rows), Decimal(0))
        arm_rows = [row for row in rows if row["arm"] == arm]
        arm_bound = sum((Decimal(str(row["reserved_usd"])) for row in arm_rows), Decimal(0))
        if len(rows) >= approval["max_calls"] or len(arm_rows) >= approval["arms"][arm]["max_calls"] or \
                total_bound + bound > _decimal(approval["max_cost_usd"], "overall limit") or \
                arm_bound + bound > _decimal(approval["arms"][arm]["max_cost_usd"], "arm limit"):
            raise ValueError("the approved call or cost envelope would be exceeded")
        row = {"slot": slot, "phase": phase, "arm": arm, "frame_id": frame["id"], "repeat": repeat,
               "schedule_index": schedule_index,
               "payload_sha256": frame["payload_sha256"], "request_sha256": request_sha,
               "requested_model": ARMS[arm][0], "reserved_usd": str(bound), "status": "in_flight"}
        path = call_dir / f"{slot}.json"
        _write(path, row, exclusive=True)  # Reserve before the POST; a crash must not cause a retry.
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
        _write(path, row)
        rows.append(row)
        completed.add(slot)
        made += 1
        if row.get("cost_bound_exceeded") is True:
            raise ValueError("a provider-reported cost exceeded the per-call bound; stopped all arms")
        if row["http_status"] != 200 or row.get("choice") is None:
            blocked.add(arm)
    return made


def score(corpus_path: Path, labels_path: Path, directory: Path, *, control: str,
          frozen_labels_path: Path | None = None) -> dict[str, Any]:
    corpus, labels, corpus_sha, label_sha = load_inputs(corpus_path, labels_path)
    label_document, _ = _load(labels_path)
    plan, _ = _load(directory / "plan.json")
    frozen_sha = label_sha
    original_authorship = label_document.get("authorship", {"kind": "unknown"})
    review = None
    if frozen_labels_path is not None:
        _, _, _, frozen_sha = load_inputs(corpus_path, frozen_labels_path)
        original_document, _ = _load(frozen_labels_path)
        original_authorship = original_document.get("authorship", {"kind": "unknown"})
        authorship = label_document.get("authorship")
        if not isinstance(authorship, dict) or authorship.get("human_review") != "completed" or \
                not isinstance(authorship.get("reviewer"), str) or not authorship["reviewer"].strip() or \
                not isinstance(authorship.get("reviewed_at"), str):
            raise ValueError("post-hoc labels need completed human review, reviewer and review date")
        try:
            dt.date.fromisoformat(authorship["reviewed_at"])
        except ValueError as exc:
            raise ValueError("the post-hoc review date must be an ISO date") from exc
        review = {"reviewer": authorship["reviewer"], "reviewed_at": authorship["reviewed_at"]}
    if plan.get("corpus_sha256") != corpus_sha or plan.get("labels_sha256") != frozen_sha:
        raise ValueError("the saved calls do not match the frozen corpus and labels")
    if plan.get("replay_source_sha256") != digest(Path(__file__).read_bytes()):
        raise ValueError("the replay and scoring code changed since the run was planned")
    if control not in plan["arms"]:
        raise ValueError("the control is not a recorded arm")
    rows = _calls(directory / "calls")
    by_frame = {frame["id"]: frame for frame in corpus["frames"]}
    for row in rows:
        frame = by_frame.get(row.get("frame_id"))
        if frame is None or row.get("payload_sha256") != frame["payload_sha256"] or row.get("arm") not in plan["arms"]:
            raise ValueError("a call does not belong to the frozen run")
    disagreements = [frame_id for frame_id, entries in labels.items() if accepted(entries) is None]
    durations = [frame["execution"][-1]["applied_duration_s"] for frame in corpus["frames"]]
    output: dict[str, Any] = {"control": control, "budget_assumptions": plan.get("budget_assumptions"),
                              "label_disagreements": disagreements,
                              "label_authorship": label_document.get("authorship", {"kind": "unknown"}),
                              "label_provenance": {"mode": "post_hoc_review" if review else "frozen",
                                  "frozen_labels_sha256": frozen_sha, "scoring_labels_sha256": label_sha,
                                  "original_authorship": original_authorship, "review": review},
                              "recording_hold_s": {"requested": corpus["requested_hold_s"],
                                  "min": min(durations), "max": max(durations),
                                  "median": statistics.median(durations)},
                              "arms": {}, "paired": {}, "verdict": "inconclusive"}
    selected: dict[str, dict[str, bool | None]] = {}
    for arm in plan["arms"]:
        sweep = [row for row in rows if row.get("phase") == "sweep" and row["arm"] == arm]
        if len({row["frame_id"] for row in sweep}) != len(sweep):
            raise ValueError("duplicate sweep calls for an arm and frame")
        values: dict[str, bool | None] = {}
        masses, latencies = [], []
        for row in sweep:
            valid = accepted(labels[row["frame_id"]])
            choice = row.get("choice")
            values[row["frame_id"]] = choice in valid if valid is not None and choice is not None else None
            distribution = row.get("distribution")
            if valid is not None and isinstance(distribution, dict):
                masses.append(sum(distribution[item] for item in valid))
            if isinstance(row.get("latency_ms"), (int, float)):
                latencies.append(row["latency_ms"])
        selected[arm] = values
        scored = [v for v in values.values() if v is not None]
        noise = [row for row in rows if row.get("phase") == "noise" and row["arm"] == arm]
        flips = comparisons = 0
        ranges = []
        for frame in corpus["frames"][:10]:
            ordered = sorted((row for row in noise if row["frame_id"] == frame["id"]), key=lambda r: r["schedule_index"])
            for left, right in zip(ordered, ordered[1:]):
                if left.get("choice") is not None and right.get("choice") is not None:
                    comparisons += 1
                    flips += left["choice"] != right["choice"]
            valid = accepted(labels[frame["id"]])
            probabilities = [sum(row["distribution"][item] for item in valid) for row in ordered
                             if valid is not None and isinstance(row.get("distribution"), dict)]
            if len(probabilities) >= 2:
                ranges.append({"frame_id": frame["id"], "min": min(probabilities), "max": max(probabilities)})
        latencies.sort()
        costs = [row.get("cost_usd") for row in sweep]
        known_cost = sum(cost for cost in costs if isinstance(cost, (int, float)))
        complete_cost = bool(sweep) and all(isinstance(cost, (int, float)) for cost in costs)
        billed = [row.get("billed") for row in sweep]
        succeeded = sum(row.get("choice") is not None for row in sweep)
        output["arms"][arm] = {"labelled_frames": len(labels), "attempted": len(sweep),
            "succeeded": succeeded,
            "scored": len(scored), "unknown": len(sweep) - len(scored), "not_attempted": len(by_frame) - len(sweep),
            "top1_acceptable": sum(scored) / len(scored) if scored else None,
            "acceptable_mass_mean": statistics.mean(masses) if masses else None,
            "acceptable_mass_n": len(masses), "latency_p50_ms": statistics.median(latencies) if latencies else None,
            "latency_p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
            "billed_calls": sum(billed) if billed and all(isinstance(item, bool) for item in billed) else None,
            "cost_usd": known_cost if complete_cost else None,
            "known_cost_subtotal_usd": known_cost if any(isinstance(cost, (int, float)) for cost in costs) else None,
            "cost_per_100_decisions_usd": (known_cost * 100 / succeeded) if complete_cost and succeeded else None,
            "noise_attempted": len(noise), "noise_flip_pairs": comparisons, "noise_flips": flips,
            "noise_flip_rate": flips / comparisons if comparisons else None, "noise_mass_ranges": ranges}
    for arm in plan["arms"]:
        if arm == control:
            continue
        pairs = [(frame_id, selected[arm][frame_id], selected[control][frame_id])
                 for frame_id in selected[arm].keys() & selected[control].keys()
                 if selected[arm][frame_id] is not None and selected[control][frame_id] is not None]
        wins = [fid for fid, candidate, base in pairs if candidate and not base]
        losses = [fid for fid, candidate, base in pairs if base and not candidate]
        n = len(pairs)
        difference = (len(wins) - len(losses)) / n if n else None
        by_pass = []
        for pass_no in sorted({by_frame[frame_id].get("pass") for frame_id, _, _ in pairs if by_frame[frame_id].get("pass") is not None}):
            current = [(candidate, base) for frame_id, candidate, base in pairs if by_frame[frame_id].get("pass") == pass_no]
            by_pass.append({"pass": pass_no, "n": len(current),
                            "difference": sum(int(candidate) - int(base) for candidate, base in current) / len(current)})
        output["paired"][arm] = {"n": n, "wins": wins, "losses": losses,
            "ties": n - len(wins) - len(losses), "difference": difference,
            "differences_by_pass": by_pass,
            "sampling_interval": None,
            "sampling_interval_reason": "frames and passes share one recording; independent sampling is not established",
            "unpaired_or_unknown": len(by_frame) - n}
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run")
    for name in ("corpus", "labels", "approval", "directory"):
        runner.add_argument(f"--{name}", type=Path, required=True)
    runner.add_argument("--phase", choices=("noise", "sweep"), required=True)
    runner.add_argument("--arms", nargs="+", choices=tuple(ARMS), required=True)
    runner.add_argument("--seed", type=int, required=True)
    reporter = sub.add_parser("score")
    for name in ("corpus", "labels", "directory"):
        reporter.add_argument(f"--{name}", type=Path, required=True)
    reporter.add_argument("--control", choices=tuple(ARMS), required=True)
    reporter.add_argument("--frozen-labels", type=Path, help="original labels pinned by the run; --labels is a reviewed revision")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            print(f"new calls: {run(args.corpus, args.labels, args.approval, args.directory, phase=args.phase, arms=args.arms, seed=args.seed)}")
        else:
            print(json.dumps(score(args.corpus, args.labels, args.directory, control=args.control,
                                   frozen_labels_path=args.frozen_labels), indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
