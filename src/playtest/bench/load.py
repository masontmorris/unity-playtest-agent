"""Mammoth latency at full context size (#25): production payloads at five sizes, sent as a live loop would.

Each call's state is `build_context` output for a recorded frame, optionally with more measured facts and
a game frame. One persistent HTTPS connection carries every call, and the runner pauses after each answer
as the live loop's hold does. Answers are checked for validity only: the padded facts mix lines from other
passes, so nothing here scores a choice. The design is predeclared in `docs/design/s1-bench.md`.

    python -m playtest.bench.load run --corpus <frames.json> --pool <recording or trace> [--pool ...] \
        --frame <png> --approval <json> --directory <new run directory> --seed 2510
    python -m playtest.bench.load score --directory <run directory>
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import http.client
import json
import math
import os
import random
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from playtest.bench import live, replay
from playtest.bench.record import digest
from playtest.contracts.bridge import Observation
from playtest.contracts.loop import ChoiceKind, ChoiceSet
from playtest.core import context as context_module
from playtest.core.context import build_context, canonical

ARM = "mammoth-1"
CONDITIONS = ("base", "base+frame", "mid", "full", "full+frame")
TARGET_TOKENS = {"mid": 6000, "full": 11000, "full+frame": 11000}
TOKENS_PER_CHAR = 0.3675  # mammoth-1's reported input tokens over the canonical body length, on the re-test's 45 calls
ROUNDS, PAUSE_S, SLOW_MS = 8, 1.2, 500.0
GAME_VIEW = "The attached image is the current 1280x720 game frame."
HOST, PATH = "inference.baseten.co", "/v1/systemone"
SOURCES = {"load_source_sha256": Path(__file__), "replay_source_sha256": Path(replay.__file__),
           "live_source_sha256": Path(live.__file__), "context_source_sha256": Path(context_module.__file__)}
Send = Callable[[dict[str, Any]], tuple[int | None, dict[str, Any] | None, float, str | None, bool]]


def schedule(seed: int) -> list[str]:
    """A warm-up call, then rounds that each send every condition once in a seeded order."""
    rng = random.Random(f"s1-load:{seed}")
    order = ["warmup"]
    for _ in range(ROUNDS):
        batch = list(CONDITIONS)
        rng.shuffle(batch)
        order.extend(batch)
    return order


def fact_pool(paths: Sequence[Path]) -> list[str]:
    """Every distinct fact line in the given recordings and traces, in first-seen order."""
    lines: list[str] = []
    for path in paths:
        _collect(json.loads(path.read_bytes()), lines)
    return list(dict.fromkeys(line for line in lines if line.strip()))


def _collect(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "facts" and isinstance(item, list) and all(isinstance(line, str) for line in item):
                out.extend(item)
            else:
                _collect(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect(item, out)


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("the frame is not a PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def payload_for(frame: dict[str, Any], extra: Sequence[str], game_view: str | None) -> dict[str, Any]:
    """The production payload for a recorded frame, with the live test's choice set and any older facts first."""
    recorded = ChoiceSet.model_validate(frame["choice_set"])
    choices = ChoiceSet(set_version=recorded.set_version, built_from_seq=recorded.built_from_seq,
                        choices=[c for c in recorded.choices if c.kind is ChoiceKind.action] + live.core_choices())
    return build_context(objective=frame["objective"], observation=Observation.model_validate(frame["observation"]),
                         choices=choices, facts=[*extra, *frame["facts"]], age_s=frame.get("age_s"),
                         sample_gap_s=frame.get("sample_gap_s"), game_view=game_view)


def body_for(payload: dict[str, Any], image_url: str | None) -> dict[str, Any]:
    state: Any = payload
    if image_url is not None:  # Baseten's image transport, as on Day 1: the state as text beside an image part.
        state = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": canonical(payload)},
            {"type": "image_url", "image_url": {"url": image_url}}]}]}
    return {"model": replay.ARMS[ARM][0], "questions": {"next_action": {
        "type": "choice", "instructions": payload["instructions"],
        "criteria": {c["id"]: c["label"] for c in payload["choices"]}}}, "state": state}


def build(condition: str, frame: dict[str, Any], pool: Sequence[str], rng: random.Random,
          image_url: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The request body for one call and what was added to reach its size."""
    framed = condition.endswith("+frame")
    game_view = GAME_VIEW if framed else None
    extra: list[str] = []
    target = TARGET_TOKENS.get(condition)
    if target is not None:
        own = set(frame["facts"])
        candidates = [line for line in pool if line not in own]
        rng.shuffle(candidates)
        chars = len(canonical(body_for(payload_for(frame, [], game_view), None)))
        for line in candidates:
            if chars * TOKENS_PER_CHAR >= target:
                break
            extra.append(line)
            chars += len(json.dumps(line, ensure_ascii=False)) + 1
        if chars * TOKENS_PER_CHAR < target:
            raise ValueError(f"the fact pool cannot reach {target} tokens without repeating a line")
    payload = payload_for(frame, extra, game_view)
    text_chars = len(canonical(body_for(payload, None)))
    return body_for(payload, image_url if framed else None), {
        "padding_lines": len(extra), "text_chars": text_chars,
        "estimated_text_tokens": round(text_chars * TOKENS_PER_CHAR), "image": framed}


class Connection:
    """One HTTPS connection kept open across calls, as a production adapter would hold it."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.conn = http.client.HTTPSConnection(HOST, timeout=60)

    def __call__(self, body: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None, float, str | None, bool]:
        new = self.conn.sock is None
        data = canonical(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"}
        started = time.perf_counter()
        try:
            self.conn.request("POST", PATH, body=data, headers=headers)
            response = self.conn.getresponse()
            raw, status = response.read(), response.status
        except (OSError, http.client.HTTPException):
            self.conn.close()
            return None, None, (time.perf_counter() - started) * 1000, "transport error", new
        latency = (time.perf_counter() - started) * 1000
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return status, None, latency, "response was not JSON", new
        return status, parsed if isinstance(parsed, dict) else None, latency, None if status == 200 else "HTTP error", new

    def close(self) -> None:
        self.conn.close()


def run(corpus_path: Path, pool_paths: Sequence[Path], frame_path: Path, approval_path: Path, directory: Path, *,
        seed: int, send: Send | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Make the approved calls, writing each to the ledger before it is sent. Returns the calls made."""
    corpus_raw = corpus_path.read_bytes()
    corpus = json.loads(corpus_raw)
    frames = corpus.get("frames")
    order = schedule(seed)
    if corpus.get("status") != "complete" or not isinstance(frames, list) or len(frames) < len(order):
        raise ValueError("a complete corpus with a frame for every call is required")
    approval, approval_sha = replay._load(approval_path)
    replay.validate_approval(approval, [ARM])
    limit = approval["arms"][ARM]
    bound = replay._decimal(limit["per_call_max_usd"], "per-call bound")
    cap = min(replay._decimal(approval["max_cost_usd"], "overall limit"), replay._decimal(limit["max_cost_usd"], "arm limit"))
    if len(order) > min(approval["max_calls"], limit["max_calls"]) or bound * len(order) > cap:
        raise ValueError("the approved envelope does not cover the planned calls")
    image = frame_path.read_bytes()
    width, height = png_size(image)
    image_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    pool = fact_pool(pool_paths)
    chosen = random.Random(f"s1-load-frames:{seed}").sample(frames, len(order))
    if send is None:
        key = os.environ.get(replay.ARMS[ARM][1])
        if not key:
            raise ValueError(f"{replay.ARMS[ARM][1]} is not set")
        send = Connection(key)
    calls = directory / "calls"
    calls.mkdir(parents=True)  # A new directory only: existing evidence is never overwritten.
    replay._write(directory / "plan.json", {
        "version": 1, "kind": "s1-load", "arm": ARM, "model": replay.ARMS[ARM][0], "seed": seed,
        "conditions": list(CONDITIONS), "target_tokens": TARGET_TOKENS, "tokens_per_char": TOKENS_PER_CHAR,
        "rounds": ROUNDS, "pause_s": PAUSE_S, "slow_ms": SLOW_MS, "transport": "one persistent HTTPS connection",
        "schedule": [{"index": i, "condition": c, "frame_id": f["id"]} for i, (c, f) in enumerate(zip(order, chosen))],
        "corpus_sha256": digest(corpus_raw),
        "pool": [{"path": str(path), "sha256": digest(path.read_bytes())} for path in pool_paths],
        "pool_lines": len(pool), "frame": {"sha256": digest(image), "bytes": len(image), "width": width, "height": height},
        "approval_sha256": approval_sha, "reserved_per_call_usd": str(bound),
        **{key: digest(path.read_bytes()) for key, path in SOURCES.items()},
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()}, exclusive=True)
    made = 0
    try:
        for index, (condition, frame) in enumerate(zip(order, chosen)):
            if dt.datetime.now(dt.timezone.utc) >= replay._price_deadline(limit, ARM):
                raise RuntimeError(f"{ARM} pricing validity expired; stopped before another call")
            body, size = build("base" if condition == "warmup" else condition, frame, pool,
                               random.Random(f"s1-load-pool:{seed}:{index}"), image_url)
            row = {"index": index, "condition": condition, "frame_id": frame["id"], "arm": ARM,
                   "request_sha256": digest(canonical(body)), "requested_model": body["model"],
                   "reserved_usd": str(bound), "status": "in_flight", **size}
            path = calls / f"{index:03d}.json"
            replay._write(path, row, exclusive=True)  # Reserve before the POST; a crash must not cause a retry.
            made += 1
            try:
                status, data, latency_ms, problem, new = send(body)
            except Exception as exc:  # Keep the reservation: an unknown outcome is never retried.
                status, data, latency_ms, problem, new = None, None, None, f"adapter error: {type(exc).__name__}", None
            offered = set(body["questions"]["next_action"]["criteria"])
            choice, _, answer_problem = replay.parse_answer(data if status == 200 else None, offered)
            usage = (data or {}).get("usage")
            cost = usage.get("cost") if isinstance(usage, dict) else None
            row.update(status="finished", http_status=status, latency_ms=latency_ms, new_connection=new,
                       problem=problem, valid_answer=status == 200 and answer_problem is None, choice=choice,
                       resolved_model=(data or {}).get("model"), usage=usage,
                       cost_usd=cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)
            replay._write(path, row)
            if status != 200:
                raise RuntimeError(f"{ARM} returned {status or 'a transport failure'} ({problem}); stopped with no retry")
            if row["cost_usd"] is not None and Decimal(str(row["cost_usd"])) > bound:
                raise RuntimeError("a provider-reported cost exceeded the per-call bound")
            if index < len(order) - 1:
                sleep(PAUSE_S)
    finally:
        if isinstance(send, Connection):
            send.close()
    return made


def _percentile(values: list[float], q: float) -> float | None:
    ordered = sorted(values)
    return ordered[math.ceil(q * len(ordered)) - 1] if ordered else None


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answered = [r for r in rows if r.get("http_status") == 200 and isinstance(r.get("latency_ms"), (int, float))]
    latencies = [r["latency_ms"] for r in answered]
    tokens = [r["usage"]["input_tokens"] for r in answered
              if isinstance(r.get("usage"), dict) and isinstance(r["usage"].get("input_tokens"), int)]
    return {"attempted": len(rows), "answered": len(answered),
            "p50_ms": statistics.median(latencies) if latencies else None,
            "p95_ms": _percentile(latencies, 0.95), "min_ms": min(latencies, default=None),
            "max_ms": max(latencies, default=None),
            "at_or_over_slow_ms": sum(value >= SLOW_MS for value in latencies),
            "input_tokens": {"min": min(tokens), "median": statistics.median(tokens), "max": max(tokens)} if tokens else None,
            "estimated_text_tokens_median": statistics.median(r["estimated_text_tokens"] for r in rows) if rows else None,
            "invalid_answers": sum(r.get("valid_answer") is False for r in answered),
            "new_connection_calls": sum(r.get("new_connection") is True for r in rows)}


def score(directory: Path) -> dict[str, Any]:
    """Summarize the saved calls; makes no call."""
    plan = replay._load(directory / "plan.json")[0]
    for key, path in SOURCES.items():
        if plan.get(key) != digest(path.read_bytes()):
            raise ValueError(f"{path.name} changed since the run was planned")
    rows = sorted(replay._calls(directory / "calls"), key=lambda r: r["index"])
    expected = {item["index"]: item for item in plan["schedule"]}
    for row in rows:
        planned = expected.get(row.get("index"))
        if planned is None or planned["condition"] != row.get("condition") or planned["frame_id"] != row.get("frame_id"):
            raise ValueError("a call does not belong to the planned schedule")
    costs = [row.get("cost_usd") for row in rows]
    return {
        "arm": ARM, "resolved_models": sorted({str(row.get("resolved_model")) for row in rows}),
        "planned": len(plan["schedule"]), "attempted": len(rows),
        "answered": sum(row.get("http_status") == 200 for row in rows),
        "stopped_early": len(rows) < len(plan["schedule"]) or any(row.get("http_status") != 200 for row in rows),
        "warmup": _summary([row for row in rows if row["condition"] == "warmup"]),
        "conditions": {name: _summary([row for row in rows if row["condition"] == name]) for name in CONDITIONS},
        "p95_note": "nearest rank; with 8 calls a condition's p95 is its slowest call",
        "latency_note": "time.perf_counter from sending the request to reading the whole answer",
        "cost": {"calls_reporting_cost": sum(isinstance(c, (int, float)) for c in costs),
                 "cost_usd": sum(costs) if costs and all(isinstance(c, (int, float)) for c in costs) else None},
        "interpretation": "not computed; a person reads the latencies against the loop's timing",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="make the approved calls")
    for name in ("corpus", "frame", "approval", "directory"):
        runner.add_argument(f"--{name}", type=Path, required=True)
    runner.add_argument("--pool", type=Path, action="append", required=True, help="a recording or trace with facts")
    runner.add_argument("--seed", type=int, required=True)
    reporter = sub.add_parser("score", help="summarize saved calls; makes no call")
    reporter.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            print(f"calls made: {run(args.corpus, args.pool, args.frame, args.approval, args.directory, seed=args.seed)}")
        else:
            print(json.dumps(score(args.directory), indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"load: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
