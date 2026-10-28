"""Continue a latency run that stopped, without editing the frozen runner.

The continuation directory gets the original plan and every answered row copied byte for byte, then the
rest of the same schedule, built and sent as `load.run` builds and sends them. A new connection opens with
one warm-up call outside the schedule, as the original run's did. The first directory is left untouched,
and a reconciliation record says what was copied and why the run resumed.

    python -m playtest.bench.load_resume --first <stopped run> --directory <new directory> --corpus ... \
        --pool ... [--pool ...] --frame <png> --approval <json> --reason "<why the run resumed>"
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import random
import sys
import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from playtest.bench import load, replay
from playtest.bench.record import digest
from playtest.core.context import canonical


def _send_one(send: load.Send, body: dict[str, Any], row: dict[str, Any], path: Path, bound: Decimal) -> None:
    """Reserve, send and record one call exactly as `load.run` does; raise to stop the run."""
    replay._write(path, row, exclusive=True)  # Reserve before the POST; a crash must not cause a retry.
    try:
        status, data, latency_ms, problem, new = send(body)
    except Exception as exc:  # Keep the reservation: an unknown outcome is never retried.
        status, data, latency_ms, problem, new = None, None, None, f"adapter error: {type(exc).__name__}", None
    offered = set(body["questions"]["next_action"]["criteria"])
    choice, _, answer_problem = replay.parse_answer(data if status == 200 else None, offered)
    usage = (data or {}).get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    row.update(status="finished", http_status=status, latency_ms=latency_ms, new_connection=new, problem=problem,
               valid_answer=status == 200 and answer_problem is None, choice=choice,
               resolved_model=(data or {}).get("model"), usage=usage,
               cost_usd=cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)
    replay._write(path, row)
    if status != 200:
        raise RuntimeError(f"{load.ARM} returned {status or 'a transport failure'} ({problem}); stopped with no retry")
    if row["cost_usd"] is not None and Decimal(str(row["cost_usd"])) > bound:
        raise RuntimeError("a provider-reported cost exceeded the per-call bound")


def resume(first: Path, directory: Path, corpus_path: Path, pool_paths: Sequence[Path], frame_path: Path,
           approval_path: Path, *, reason: str, send: load.Send | None = None,
           sleep: Callable[[float], None] = time.sleep) -> int:
    """Send the rest of a stopped run's schedule into a new directory. Returns the calls made."""
    if not reason.strip():
        raise ValueError("record why the run resumed")
    plan_raw = (first / "plan.json").read_bytes()
    plan = json.loads(plan_raw)
    for key, path in load.SOURCES.items():
        if plan.get(key) != digest(path.read_bytes()):
            raise ValueError(f"{path.name} changed since the run was planned")
    corpus_raw, image = corpus_path.read_bytes(), frame_path.read_bytes()
    if digest(corpus_raw) != plan["corpus_sha256"] or digest(image) != plan["frame"]["sha256"] or \
            [digest(path.read_bytes()) for path in pool_paths] != [item["sha256"] for item in plan["pool"]]:
        raise ValueError("the corpus, fact sources or frame differ from the plan")
    order, frames = load.schedule(plan["seed"]), json.loads(corpus_raw)["frames"]
    chosen = random.Random(f"s1-load-frames:{plan['seed']}").sample(frames, len(order))
    if [(i, c, f["id"]) for i, (c, f) in enumerate(zip(order, chosen))] != \
            [(s["index"], s["condition"], s["frame_id"]) for s in plan["schedule"]]:
        raise ValueError("the schedule no longer matches the plan")
    rows = sorted(replay._calls(first / "calls"), key=lambda r: r["index"])
    start = sum(1 for r in rows if r.get("http_status") == 200)
    if [r["index"] for r in rows[:start]] != list(range(start)) or \
            any(r.get("http_status") != 200 for r in rows[:start]) or len(rows) > start + 1:
        raise ValueError("only answered calls followed by at most one failed call can resume")
    remaining = len(order) - start
    if remaining == 0:
        raise ValueError("the run is complete; nothing to resume")
    approval, approval_sha = replay._load(approval_path)
    replay.validate_approval(approval, [load.ARM])
    limit = approval["arms"][load.ARM]
    bound = replay._decimal(limit["per_call_max_usd"], "per-call bound")
    cap = min(replay._decimal(approval["max_cost_usd"], "overall limit"), replay._decimal(limit["max_cost_usd"], "arm limit"))
    if remaining + 1 > min(approval["max_calls"], limit["max_calls"]) or bound * (remaining + 1) > cap:
        raise ValueError("the approved envelope does not cover the warm-up and the remaining calls")
    if send is None:
        key = os.environ.get(replay.ARMS[load.ARM][1])
        if not key:
            raise ValueError(f"{replay.ARMS[load.ARM][1]} is not set")
        send = load.Connection(key)
    calls = directory / "calls"
    calls.mkdir(parents=True)  # A new directory only: existing evidence is never overwritten.
    (directory / "plan.json").write_bytes(plan_raw)
    copied = []
    for path in sorted((first / "calls").glob("*.json")):
        raw = path.read_bytes()
        if json.loads(raw)["index"] < start:
            (calls / path.name).write_bytes(raw)
            copied.append({"file": path.name, "sha256": digest(raw)})
    failed = [{"file": p.name, "sha256": digest(p.read_bytes())} for p in sorted((first / "calls").glob("*.json"))
              if json.loads(p.read_bytes())["index"] >= start]
    used = {f["id"] for f in chosen}
    spare = next(f for f in frames if f["id"] not in used)
    replay._write(directory / "reconciliation.json", {
        "kind": "s1-load-continuation", "first_directory": str(first), "plan_sha256": digest(plan_raw),
        "copied_rows": copied, "failed_rows": failed, "resumed_from_index": start, "remaining_calls": remaining,
        "warmup_frame_id": spare["id"], "approval_sha256": approval_sha, "reason": reason,
        "resume_source_sha256": digest(Path(__file__).read_bytes()),
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()}, exclusive=True)
    pool = load.fact_pool(pool_paths)
    image_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    made = 0
    try:
        jobs = [("warmup", spare, directory / "warmup.json", random.Random(f"s1-load-resume:{plan['seed']}:{start}"))]
        jobs += [(order[i], chosen[i], calls / f"{i:03d}.json", random.Random(f"s1-load-pool:{plan['seed']}:{i}"))
                 for i in range(start, len(order))]
        for number, (condition, frame, path, rng) in enumerate(jobs):
            if dt.datetime.now(dt.timezone.utc) >= replay._price_deadline(limit, load.ARM):
                raise RuntimeError(f"{load.ARM} pricing validity expired; stopped before another call")
            body, size = load.build("base" if condition == "warmup" else condition, frame, pool, rng, image_url)
            index = None if number == 0 else start + number - 1
            row = {"index": index, "condition": condition, "frame_id": frame["id"], "arm": load.ARM,
                   "request_sha256": digest(canonical(body)), "requested_model": body["model"],
                   "reserved_usd": str(bound), "status": "in_flight", "continuation": True, **size}
            made += 1
            _send_one(send, body, row, path, bound)
            if number < len(jobs) - 1:
                sleep(load.PAUSE_S)
    finally:
        if isinstance(send, load.Connection):
            send.close()
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("first", "directory", "corpus", "frame", "approval"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--pool", type=Path, action="append", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        made = resume(args.first, args.directory, args.corpus, args.pool, args.frame, args.approval, reason=args.reason)
        print(f"calls made: {made}")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, StopIteration) as exc:
        print(f"load_resume: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
