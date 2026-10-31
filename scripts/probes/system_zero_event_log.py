"""F5 of the System Zero format probe: the state an append-only stream would hold (docs/probes/2026-10-31-system-zero-format.md).

A stream decision is an ordinary decide over the stream's accumulated state, so this sends that state over HTTP.
For frame k of a pass: item 0 is a header with frame k's non-per-turn payload fields; then one item per frame
of the same pass up to k, in recorded order, with that frame's `as_of`, `observation` and the facts new since
the previous frame. Question instructions stay G2's. Rows have the format probe's shape, so its `score`
command reads this directory. Reuses the probe's loaders and ledger rules; never retries.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from playtest.bench import replay, system_zero
from playtest.bench.record import digest, recorded_payload
from playtest.core.context import canonical

_spec = importlib.util.spec_from_file_location("system_zero_format", Path(__file__).with_name("system_zero_format.py"))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

FORMAT = "event-log"
BOUND = Decimal("0.0012")
PER_TURN = ("as_of", "observation", "facts")


def event_log(frame: dict[str, Any], frames: list[dict[str, Any]]) -> list[Any]:
    """The stream's array after this pass's appends up to `frame`. Raises if it would lose a fact of `frame`."""
    same_pass = sorted((f for f in frames if f["pass"] == frame["pass"]
                        and f["observation"]["seq"] <= frame["observation"]["seq"]),
                       key=lambda f: f["observation"]["seq"])
    payload = recorded_payload(frame)
    items: list[Any] = [{key: value for key, value in payload.items() if key not in PER_TURN}]
    seen: list[str] = []
    for earlier in same_pass:
        step = recorded_payload(earlier)
        new = [fact for fact in step["facts"] if fact not in seen]
        seen.extend(new)
        items.append({"as_of": step["as_of"], "observation": step["observation"], "new_facts": new})
    if same_pass[-1]["id"] != frame["id"] or any(fact not in seen for fact in payload["facts"]):
        raise ValueError(f"{frame['id']}: the log would not hold this frame's state")
    return items


def body_for(frame: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
    payload = recorded_payload(frame)
    items = [(c["id"], c["label"], c["kind"]) for c in payload["choices"]]
    questions = system_zero.layout("next_action", payload["instructions"], items, media=False)
    return {"model": system_zero.MODEL, "questions": questions, "state": event_log(frame, frames)}


def run(args: argparse.Namespace) -> int:
    shuffled, shuffled_labels, shuffled_sha = probe.load(args.shuffled_corpus, args.shuffled_labels)
    first, _, first_sha = probe.load(args.first_corpus, args.first_labels)
    approval, approval_sha = replay._load(args.approval)
    replay.validate_approval(approval, [system_zero.ARM])
    args.directory.mkdir(parents=True, exist_ok=True)
    (args.directory / "calls").mkdir(exist_ok=True)
    plan = {"version": 1, "format": FORMAT, "script_sha256": digest(Path(__file__).read_bytes()),
            "probe_source_sha256": digest(Path(probe.__file__).read_bytes()), "model": system_zero.MODEL,
            "seed": probe.SEED, "shuffled_corpus_sha256": shuffled_sha, "first_corpus_sha256": first_sha,
            "approval_sha256": approval_sha, "predeclaration": "docs/probes/2026-10-31-system-zero-format.md (amendment)"}
    plan_path = args.directory / "plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise ValueError("the saved plan differs; do not mix frozen inputs")
    else:
        probe._write(plan_path, plan, exclusive=True)
    if args.stage == "A":
        by_id = {f["id"]: f for f in shuffled}
        entries = [("shuffled", f, 0) for f in shuffled] + [("shuffled", by_id[i], 1) for i in probe.REPEATED]
    else:
        hits = probe.stage_scores(probe._rows(args.directory), "A", shuffled_labels).get(FORMAT, {"hits": 0})["hits"]
        entries = [("first", f, 0) for f in first] if hits >= probe.PASS_BAR else []
    random.Random(f"{probe.SEED}-{FORMAT}-{args.stage}").shuffle(entries)
    # The ledger is shared with the probe's own directory: both spend from one approval.
    rows = probe._rows(args.directory)
    shared = probe._rows(args.shared) if args.shared else []
    if any(row.get("status") == "in_flight" for row in rows + shared):
        raise ValueError("a prior call has an unknown outcome; reconcile it before resuming")
    done, made = {row["slot"] for row in rows}, 0
    limit = Decimal(str(approval["max_cost_usd"]))
    for corpus, frame, repeat in entries:
        frames = shuffled if corpus == "shuffled" else first
        body = body_for(frame, frames)
        request_sha = digest(canonical(body))
        slot = digest(canonical({"stage": args.stage, "format": FORMAT, "frame": frame["id"], "repeat": repeat,
                                 "request_sha256": request_sha}))
        if slot in done:
            continue
        reserved = sum((Decimal(row["reserved_usd"]) for row in rows + shared), Decimal(0))
        if len(rows) + len(shared) >= approval["max_calls"] or reserved + BOUND > limit:
            print(f"envelope reached after {made} new calls", file=sys.stderr)
            break
        row = {"slot": slot, "stage": args.stage, "format": FORMAT, "corpus": corpus, "frame_id": frame["id"],
               "repeat": repeat, "payload_sha256": frame["payload_sha256"], "request_sha256": request_sha,
               "reserved_usd": str(BOUND), "status": "in_flight", "log_items": len(body["state"])}
        path = args.directory / "calls" / f"{slot}.json"
        probe._write(path, row, exclusive=True)
        try:
            status, data, latency_ms, problem = system_zero.send(system_zero.ARM, body)
        except Exception as exc:  # noqa: BLE001 - the outcome is unknown; keep the row and stop
            row.update(status="finished", http_status=None, problem=f"adapter error: {type(exc).__name__}", failed=True)
            probe._write(path, row)
            print(f"stopped: {row['problem']}", file=sys.stderr)
            return 1
        offered = {c["id"] for c in recorded_payload(frame)["choices"]}
        choice, distribution, answer_problem = system_zero.decode("next_action", body["questions"],
                                                                  data if status == 200 else None, offered)
        row.update(status="finished", http_status=status, latency_ms=latency_ms, problem=problem,
                   resolved_model=(data or {}).get("model"), usage=(data or {}).get("usage"),
                   answers=(data or {}).get("answers") if status == 200 else None, choice=choice,
                   distribution=distribution, answer_problem=answer_problem, failed=status != 200)
        probe._write(path, row)
        rows.append(row)
        made += 1
        if row["failed"]:
            print(f"stopped: HTTP {status} {problem}", file=sys.stderr)
            return 1
    print(f"new calls: {made}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("A", "B"), required=True)
    for name in ("shuffled-corpus", "shuffled-labels", "first-corpus", "first-labels", "directory", "approval"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--shared", type=Path, help="the format probe's directory, whose rows share the envelope")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
