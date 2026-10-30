"""Bounded Liquid d1-ultra rate probe: replay frozen session-02 S1 calls unpaced.

Operational throughput only. This replays the exact request bodies recorded in
``runs/b04-hunt/session-02/trace.jsonl`` (each rebuilt body must hash to the stored
``s1_call.body.request_sha256`` before anything is sent), with the exact JPEG bytes
named by ``game_view_sha256``, in chronological recorded order, cycling if the caps
allow. It measures what one sequential, unpaced stream achieved; it does not score
choices, does not discover the provider's true limit, and never executes a game action.

Caps (earliest wins): first 429 / non-200 / invalid reply / missing usage or cost /
cost above the reserved bound, the per-run call and cost caps from the validated
approval (never above the hard maxima of 300 calls and $0.10), or 180 s wall from
first dispatch through final completion. Cost is enforced by reserving $0.0006
before every call through the shared ledger (``playtest.bench.call_budget``).
No retries, no concurrency. The reserved bound is an empirical estimate under the
user's unverified-price override: session-02's 108 calls at the same model peaked
at 9,677 input tokens and $0.000406434, so $0.0006 leaves about 48% headroom; it is
not guaranteed provider pricing. At most 166 calls fit under $0.10 at that bound.
The paced follow-up freezes 100 calls, $0.06, 180 s and ``--min-interval-s 1.0``
(start-to-start, no catch-up bursts) in its plan.

Transport note: live S1 sends with ``urllib`` (one connection per call, 60 s socket
timeout). This probe sends the same canonical bytes and headers with one persistent
``httpx`` client and bounds each in-flight call by the remaining wall time, so the
whole probe including an in-flight call stays inside 180 s. This persistent
``httpx`` transport is not the live ``urllib`` path; its latency and throughput
are not equal to live-run throughput.

    uv run --extra probe python scripts/probes/rate_probe_liquid_d1_ultra.py verify \
        --trace runs/b04-hunt/session-02/trace.jsonl \
        --frames-dir runs/b04-hunt/session-02/recording/frames \
        --recording runs/b04-hunt/session-02/recording/recording.json
    uv run --extra probe python scripts/probes/rate_probe_liquid_d1_ultra.py run \
        --trace ... --frames-dir ... --recording ... \
        --approval runs/b04-hunt/approval-rate-probe.json \
        --directory runs/b04-hunt/rate-probe [--env-file <main checkout .env>]
    uv run --extra probe python scripts/probes/rate_probe_liquid_d1_ultra.py score \
        --directory runs/b04-hunt/rate-probe
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from playtest.bench import call_budget, replay  # noqa: E402
from playtest.bench.record import digest  # noqa: E402
from playtest.core.context import canonical  # noqa: E402
from playtest.runner.s1 import FOLLOW_UP_INSTRUCTIONS  # noqa: E402

ARM = "liquid-d1-ultra"
MODEL = "d1-ultra"
PRIMARY = "next_action"
MAX_AUTH_CALLS = 300
HARD_MAX_CALLS = 300
MAX_WALL_S = 180.0
MAX_COST_USD = Decimal("0.10")
HARD_MAX_COST_USD = Decimal("0.10")
RESERVED_PER_CALL_USD = Decimal("0.0006")
WINDOW_S = 60.0
RECOMMENDATION_MIN_SPAN_S = 90.0
RECOMMENDATION_ROUND_TOKENS = 10000
PENDING_KINDS = ("wait", "action", "skill")
Transport = Callable[[dict[str, Any], float], Awaitable[tuple[int | None, Any, str | None]]]


def effective_max_calls(max_cost: Decimal = MAX_COST_USD, bound: Decimal = RESERVED_PER_CALL_USD,
                        auth_cap: int = MAX_AUTH_CALLS) -> int:
    """Calls that fit under the cost ceiling at the fixed reservation, within authorization."""
    return min(auth_cap, int(max_cost // bound))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_cost(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def rebuild_body(payload: dict[str, Any], question: str, image: bytes | None) -> dict[str, Any]:
    """The canonical S1 request for one recorded call, mirroring ``SystemOneS1._call``."""
    choices = payload["choices"]
    if question == PRIMARY:
        instructions = payload["instructions"]
        criteria = {item["id"]: item["label"] for item in choices}
    else:
        instructions = FOLLOW_UP_INSTRUCTIONS
        criteria = {item["id"]: item["label"] for item in choices if item["kind"] in PENDING_KINDS}
    body: dict[str, Any] = {"model": MODEL,
                            "questions": {question: {"type": "choice", "instructions": instructions,
                                                     "criteria": criteria}},
                            "state": payload}
    if image is not None:
        body["images"] = ["data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")]
    return body


def choice_for(question: str, data: Any, offered: set[str]) -> tuple[str | None, Any, str | None]:
    """Offered choice and distribution for one reply, reusing the production answer check."""
    if not isinstance(data, dict):
        return None, None, "invalid or unoffered choice"
    if question == PRIMARY:
        return replay.parse_answer(data, offered)
    answers = data.get("answers")
    wrapped = {"answers": {PRIMARY: answers.get(question) if isinstance(answers, dict) else None}}
    return replay.parse_answer(wrapped, offered)


def usage_of(data: Any) -> tuple[int | None, int | None, float | None, dict[str, Any] | None]:
    """(input, total, cost, raw usage); any of the first three is ``None`` when unreported or invalid."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None, None, None, None
    tokens_in = usage.get("input_tokens")
    tokens_out = usage.get("output_tokens")
    cost = usage.get("cost")
    valid_in = tokens_in if _is_int(tokens_in) and tokens_in >= 0 else None
    valid_out = tokens_out if _is_int(tokens_out) and tokens_out >= 0 else None
    total = valid_in + valid_out if valid_in is not None and valid_out is not None else None
    return valid_in, total, (float(cost) if _is_cost(cost) else None), usage


def classify(status: int | None, choice: str | None, total: int | None, cost: float | None,
             bound: Decimal, *, timed_out: bool = False) -> str | None:
    """Stop reason for a finished call, or ``None`` when it may be followed by another call."""
    if timed_out:
        return "deadline_exceeded_in_flight"
    if status == 429:
        return "rate_limited_429"
    if status in (401, 402, 403):
        return "auth_or_billing_error"
    if status != 200:
        return "http_or_transport_failure"
    if choice is None:
        return "invalid_reply"
    if total is None or cost is None:
        return "missing_usage_or_cost"
    if Decimal(str(cost)) > bound:
        return "cost_exceeded_reserved_bound"
    return None


def full_window_stats(points: list[tuple[float, int]], started: float,
                      window_s: float = WINDOW_S) -> tuple[int | None, int | None]:
    """(min, max) tokens in any fully elapsed trailing window; ``(None, None)`` without evidence.

    ``points`` are (completion time, total tokens) on one monotonic clock and ``started``
    is the first dispatch on that clock. Only windows ending at least ``window_s`` after
    the first dispatch count, so a short burst is never reported as a full-window rate.
    Each eligible window is the trailing ``(at - window_s, at]`` sum at a completion.
    """
    ordered = sorted(points)
    low: int | None = None
    high: int | None = None
    left = 0
    running = 0
    for at, tokens in ordered:
        running += tokens
        while ordered[left][0] <= at - window_s:
            running -= ordered[left][1]
            left += 1
        if at - started >= window_s:
            low = running if low is None else min(low, running)
            high = running if high is None else max(high, running)
    return low, high


def full_window_tpm(points: list[tuple[float, int]], started: float, window_s: float = WINDOW_S) -> int | None:
    """Best (max) tokens in any fully elapsed trailing window; ``None`` without 60 s of evidence."""
    return full_window_stats(points, started, window_s)[1]


def summarize(rows: list[dict[str, Any]], *, started_offset: float = 0.0,
              ended_by: str | None = None, expected_calls: int | None = None,
              min_span_s: float = RECOMMENDATION_MIN_SPAN_S) -> dict[str, Any]:
    """Throughput summary for finished ledger rows; makes no call.

    Primary metric is fully elapsed rolling 60 s tokens from completed successful
    calls. A recommendation is made only when every attempted call succeeded with
    no stop reason, the span reaches ``min_span_s``, the expected call count (when
    given) succeeded, and a full window exists: 80% of the MIN eligible full 60 s
    window, rounded down to the nearest 10,000 tokens/min. Any 429, non-200,
    invalid choice, missing usage/cost, over-bound or deadline row leaves the
    recommendation ``None``. Unknown token or cost values stay ``None``.
    """
    ordered = sorted(rows, key=lambda row: row.get("probe_index", 0))
    successes = [row for row in ordered if row.get("stop_reason") is None and row.get("http_status") == 200]
    tokens_in = [row["input_tokens"] for row in successes if _is_int(row.get("input_tokens"))]
    tokens_out = [row["total_tokens"] - row["input_tokens"] for row in successes
                  if _is_int(row.get("total_tokens")) and _is_int(row.get("input_tokens"))]
    costs = [row["cost_usd"] for row in ordered if _is_cost(row.get("cost_usd"))]
    unknown = len(ordered) - len(costs)
    latencies = [row["latency_ms"] for row in successes
                 if isinstance(row.get("latency_ms"), (int, float)) and not isinstance(row.get("latency_ms"), bool)]
    sorted_lat = sorted(latencies)
    elapsed = max((row.get("completion_offset_s") or 0.0) for row in ordered) if ordered else 0.0
    points = [(started_offset + (row.get("completion_offset_s") or 0.0), row["total_tokens"])
              for row in successes if _is_int(row.get("total_tokens"))]
    window_min, window_max = full_window_stats(points, started_offset)
    total_tokens = sum(tokens for _, tokens in points)
    all_succeeded = bool(ordered) and len(successes) == len(ordered)
    count_ok = expected_calls is None or len(successes) == expected_calls
    span_ok = elapsed >= min_span_s
    recommended = None
    if window_min is not None and all_succeeded and count_ok and span_ok:
        recommended = int((Decimal(window_min) * Decimal("0.8")
                           // Decimal(RECOMMENDATION_ROUND_TOKENS)) * Decimal(RECOMMENDATION_ROUND_TOKENS))
    if recommended is not None:
        basis = ("80% of the MIN fully elapsed 60 s window, rounded down to the nearest "
                 "10,000 tokens/min (headroom across every observed complete window); "
                 "an observed achieved rate for this one paced stream, not a provider "
                 "limit; the parent makes the final operating-setting decision")
    elif window_min is None:
        basis = "none: less than 60 s of eligible evidence, so no sustained rate is claimed"
    elif not all_succeeded:
        basis = "none: a failed, invalid, missing-usage/cost, over-bound or deadline row is present"
    elif not count_ok:
        basis = "none: the expected call count did not all succeed"
    else:
        basis = "none: the span is below the predeclared minimum for a recommendation"
    return {
        "attempted": len(ordered),
        "succeeded": len(successes),
        "statuses": dict(sorted(Counter(str(row.get("http_status")) for row in ordered).items())),
        "resolved_models": dict(sorted(Counter(str(row.get("resolved_model")) for row in ordered).items())),
        "stop_reason": ordered[-1].get("stop_reason") if ordered else None,
        "ended_by": ended_by or (ordered[-1].get("stop_reason") if ordered else "no_calls"),
        "input_tokens": {"total": sum(tokens_in), "min": min(tokens_in, default=None),
                         "median": statistics.median(tokens_in) if tokens_in else None,
                         "max": max(tokens_in, default=None)},
        "output_tokens": {"total": sum(tokens_out) if len(tokens_out) else None,
                          "min": min(tokens_out, default=None),
                          "median": statistics.median(tokens_out) if tokens_out else None,
                          "max": max(tokens_out, default=None)},
        "total_tokens": total_tokens,
        "cost_usd": {"reported_total": sum(costs) if costs else None,
                     "reserved_total": str(sum(Decimal(str(row.get("reserved_usd", "0"))) for row in ordered)),
                     "conservative_total": (str(sum(Decimal(str(cost)) for cost in costs)
                                                + RESERVED_PER_CALL_USD * unknown) if ordered else "0"),
                     "calls_without_reported_cost": unknown},
        "latency_ms": {"median": statistics.median(latencies) if latencies else None,
                       "p95": (sorted_lat[math.ceil(0.95 * len(sorted_lat)) - 1] if sorted_lat else None),
                       "min": min(latencies, default=None), "max": max(latencies, default=None)},
        "elapsed_s": elapsed,
        "average_tokens_per_minute": (total_tokens * 60.0 / elapsed if elapsed > 0 else None),
        "full_60s_window_tokens_per_minute": window_max,
        "full_60s_window_min_tokens_per_minute": window_min,
        "full_60s_window_max_tokens_per_minute": window_max,
        "full_window_available": window_min is not None,
        "recommended_tokens_per_minute": recommended,
        "recommendation_basis": basis,
    }


def load_corpus(trace_path: Path, frames_dir: Path, recording_path: Path | None) -> dict[str, Any]:
    """Load and fully verify the frozen corpus. Raises ``ValueError`` before any spend on drift."""
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    contexts = {row["trace_seq"]: row["body"]["payload"] for row in rows if row.get("kind") == "context"}
    for seq, payload in contexts.items():
        recorded = next(row["body"]["sha256"] for row in rows if row.get("trace_seq") == seq)
        if digest(canonical(payload)) != recorded:
            raise ValueError(f"context at trace seq {seq} no longer hashes to its recorded payload hash")
    index: dict[str, Path] = {}
    if recording_path is not None:
        recording = json.loads(recording_path.read_text(encoding="utf-8"))
        for frame in recording.get("frames", []):
            index[frame["sha256"]] = frames_dir / frame["file"]
    sources: list[dict[str, Any]] = []
    images: dict[str, bytes] = {}
    for row in [item for item in rows if item.get("kind") == "s1_call"]:
        body_record = row["body"]
        refs = row.get("refs") or []
        if len(refs) != 1 or refs[0] not in contexts:
            raise ValueError(f"s1_call at trace seq {row['trace_seq']} does not name exactly one context")
        payload = contexts[refs[0]]
        status, sha = payload.get("game_view_status"), payload.get("game_view_sha256")
        image = None
        if status == "sent":
            if sha not in images:
                path = index.get(sha)  # type: ignore[arg-type]
                if path is None or not path.is_file():
                    if not index:  # No recording index: hash the directory once.
                        for candidate in frames_dir.glob("*.jpg"):
                            index[digest(candidate.read_bytes())] = candidate
                        path = index.get(sha)  # type: ignore[arg-type]
                    if path is None:
                        raise ValueError(f"no recorded frame matches game_view_sha256 at trace seq {refs[0]}")
                data = path.read_bytes()  # type: ignore[union-attr]
                if digest(data) != sha:
                    raise ValueError(f"frame bytes for trace seq {refs[0]} no longer hash to game_view_sha256")
                images[sha] = data  # type: ignore[index]
            image = images[sha]  # type: ignore[index]
        elif not (status in ("omitted", "stale") and sha is None):
            # Session-02 sent every view; anything else must be explicit, never silently dropped.
            raise ValueError(f"unhandled game view state at trace seq {refs[0]}: {status!r}")
        body = rebuild_body(payload, body_record["question"], image)
        request_sha = digest(canonical(body))
        if request_sha != body_record["request_sha256"]:
            raise ValueError(f"rebuilt request for trace seq {row['trace_seq']} does not match its recorded hash")
        usage = body_record.get("usage") or {}
        sources.append({"source_trace_seq": row["trace_seq"], "context_trace_seq": refs[0],
                        "turn": row.get("turn"), "question": body_record["question"],
                        "based_on_seq": payload["as_of"]["seq"], "request_sha256": request_sha,
                        "game_view_sha256": sha, "body": body,
                        "historical_input_tokens": usage.get("input_tokens"),
                        "historical_cost_usd": usage.get("cost")})
    if not sources:
        raise ValueError("the trace holds no S1 calls")
    for source in sources:
        if not _is_int(source["historical_input_tokens"]) or not _is_cost(source["historical_cost_usd"]):
            raise ValueError("historical usage is incomplete; the reserved bound cannot be verified")
        if Decimal(str(source["historical_cost_usd"])) > RESERVED_PER_CALL_USD:
            raise ValueError("a historical call cost more than the reserved bound; refusing to proceed")
    recorded_contexts = {row["trace_seq"]: row["body"]["sha256"] for row in rows if row.get("kind") == "context"}
    return {"sources": sources,
            "trace_sha256": digest(trace_path.read_bytes()),
            "contexts_sha256": digest(canonical([recorded_contexts[key] for key in sorted(recorded_contexts)])),
            "requests_sha256": digest(canonical([source["request_sha256"] for source in sources])),
            "images_sha256": digest(canonical(sorted(images))),
            "unique_contexts": len(contexts), "unique_images": len(images),
            "historical_max_input_tokens": max(source["historical_input_tokens"] for source in sources),
            "historical_max_cost_usd": max(source["historical_cost_usd"] for source in sources),
            "historical_total_cost_usd": sum(source["historical_cost_usd"] for source in sources)}


def validated_caps(approval: dict[str, Any]) -> tuple[int, Decimal]:
    """Per-run (call cap, cost ceiling) from the validated approval, within hard maxima.

    The approval may freeze any cap up to 300 calls and $0.10; the effective call
    cap also fits the cost ceiling at the fixed $0.0006 reservation. The first
    probe (166 / $0.10) and the paced follow-up (100 / $0.06) both satisfy this.
    """
    replay.validate_approval(approval, [ARM])
    limit = approval["arms"][ARM]
    if replay._decimal(limit["per_call_max_usd"], "per-call bound") != RESERVED_PER_CALL_USD:
        raise ValueError("the approval's per-call bound is not the probe's reserved bound")
    overall = replay._decimal(approval["max_cost_usd"], "overall limit")
    arm_cost = replay._decimal(limit["max_cost_usd"], "arm limit")
    if overall > HARD_MAX_COST_USD or arm_cost > HARD_MAX_COST_USD:
        raise ValueError("the approval exceeds the probe's hard $0.10 ceiling")
    if approval["max_calls"] > HARD_MAX_CALLS or limit["max_calls"] > HARD_MAX_CALLS:
        raise ValueError("the approval exceeds the probe's hard 300-call maximum")
    cap = min(approval["max_calls"], limit["max_calls"],
              int(overall // RESERVED_PER_CALL_USD), int(arm_cost // RESERVED_PER_CALL_USD),
              HARD_MAX_CALLS)
    if cap < 1:
        raise ValueError("the approval leaves no call under its caps")
    return cap, overall


def check_approval(approval: dict[str, Any]) -> None:
    """The probe's own envelope checks, on top of the shared approval validation."""
    validated_caps(approval)


def load_credentials(env_file: Path | None) -> tuple[str, str]:
    """Endpoint and key from the environment, else the two named keys in ``env_file``. Never printed."""
    url, key = os.environ.get("LIQUID_D1.1_URL"), os.environ.get("LIQUID_API_KEY")
    if (not url or not key) and env_file is not None:
        wanted = {"LIQUID_D1.1_URL": url, "LIQUID_API_KEY": key}
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            name = name.strip().removeprefix("export ").strip()
            if sep and name in wanted and not wanted[name]:
                wanted[name] = value.strip().strip('"').strip("'")
        url, key = wanted["LIQUID_D1.1_URL"], wanted["LIQUID_API_KEY"]
    if not url or not key:
        raise ValueError("LIQUID_D1.1_URL and LIQUID_API_KEY must be set (environment or --env-file)")
    return url, key


async def httpx_transport(url: str, key: str) -> tuple[Any, Transport]:
    """One persistent client; each call posts the canonical bytes with the production headers."""
    import httpx

    client = httpx.AsyncClient()

    async def send(body: dict[str, Any], timeout_s: float) -> tuple[int | None, Any, str | None]:
        try:
            response = await client.post(url, content=canonical(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {key}"},
                                         timeout=httpx.Timeout(timeout_s))
        except httpx.HTTPError as exc:
            return None, None, f"transport error: {type(exc).__name__}"
        try:
            data = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            return response.status_code, None, "response was not JSON"
        return response.status_code, (data if isinstance(data, dict) else None), None

    return client, send


async def execute(sources: list[dict[str, Any]], ledger: call_budget.Ledger, send: Transport, *,
                  max_calls: int | None = None, max_wall_s: float = MAX_WALL_S,
                  bound: Decimal = RESERVED_PER_CALL_USD, min_interval_s: float = 0.0,
                  monotonic: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], Awaitable[None]] | None = None) -> dict[str, Any]:
    """Send the predeclared order sequentially until a cap or the first failure.

    Start-to-start spacing of ``min_interval_s`` is enforced by sleeping before
    the reservation (bounded by the total deadline, never a catch-up burst), and
    the remaining deadline is recomputed after the disk reservation before each
    send, so ledger I/O cannot extend the network call beyond the deadline. If
    the deadline is exhausted after a reservation, that reserved-but-unsent call
    is finished explicitly and nothing is sent.
    """
    if isinstance(min_interval_s, bool) or not isinstance(min_interval_s, (int, float)) \
            or not math.isfinite(min_interval_s) or min_interval_s < 0:
        raise ValueError("min_interval_s must be a finite nonnegative number")
    sleep_fn = sleep if sleep is not None else asyncio.sleep
    cap = effective_max_calls() if max_calls is None else max_calls
    started: float | None = None
    last_dispatch: float | None = None
    rows: list[dict[str, Any]] = []
    ended_by = "call_cap"
    for index in range(cap):
        source = sources[index % len(sources)]
        if started is not None and last_dispatch is not None and min_interval_s > 0:
            wait = min_interval_s - (monotonic() - last_dispatch)
            if wait > 0:
                remaining_before_sleep = max_wall_s - (monotonic() - started)
                if remaining_before_sleep <= 0:
                    ended_by = "wall_limit"
                    break
                await sleep_fn(min(wait, remaining_before_sleep))
                if monotonic() - started >= max_wall_s:
                    ended_by = "wall_limit"
                    break
        now = monotonic()
        if started is not None and now - started >= max_wall_s:
            ended_by = "wall_limit"
            break
        try:
            reservation = ledger.reserve(episode=1, turn=index, based_on_seq=source["based_on_seq"],
                                         request_sha256=source["request_sha256"], requested_model=MODEL,
                                         extra={"probe_index": index, "cycle": index // len(sources),
                                                "source_trace_seq": source["source_trace_seq"],
                                                "context_trace_seq": source["context_trace_seq"],
                                                "question": source["question"]})
        except call_budget.BudgetStop:
            ended_by = "budget_exhausted"
            break
        if started is None:
            started = monotonic()
        remaining = max_wall_s - (monotonic() - started)
        if remaining <= 0:
            row = reservation.finish(
                http_status=None, choice=None, distribution=None,
                problem="reserved but not sent: deadline exhausted after reservation",
                latency_ms=None, resolved_model=None, usage=None, cost_usd=None,
                probe_index=index, dispatch_offset_s=None,
                completion_offset_s=monotonic() - started, input_tokens=None,
                total_tokens=None, stop_reason="deadline_exhausted_before_dispatch",
                reserved_but_unsent=True)
            rows.append(row)
            ended_by = "deadline_exhausted_before_dispatch"
            break
        dispatch_at = monotonic()
        last_dispatch = dispatch_at
        dispatch_offset = dispatch_at - started
        timed_out = False
        try:
            status, data, problem = await asyncio.wait_for(send(source["body"], remaining), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError):
            status, data, problem, timed_out = None, None, "deadline exceeded during the call", True
        except Exception as exc:  # Keep the reservation: an unknown outcome is never retried.
            status, data, problem = None, None, f"adapter error: {type(exc).__name__}"
        completion_offset = monotonic() - started
        latency_ms = max(0.0, (completion_offset - dispatch_offset) * 1000)
        offered = set(source["body"]["questions"][source["question"]]["criteria"])
        choice, distribution, answer_problem = choice_for(source["question"], data, offered)
        tokens_in, total, cost, usage = usage_of(data)
        reason = classify(status, choice, total, cost, bound, timed_out=timed_out)
        row = reservation.finish(http_status=status, choice=choice, distribution=distribution,
                                 problem=problem or answer_problem, latency_ms=latency_ms,
                                 resolved_model=data.get("model") if isinstance(data, dict) else None,
                                 usage=usage, cost_usd=cost,
                                 probe_index=index, dispatch_offset_s=dispatch_offset,
                                 completion_offset_s=completion_offset, input_tokens=tokens_in,
                                 total_tokens=total, stop_reason=reason)
        rows.append(row)
        if reason is not None:
            ended_by = reason
            break
        if monotonic() - started >= max_wall_s:
            ended_by = "wall_limit"
            break
    else:
        ended_by = "call_cap"
    if rows and rows[-1].get("stop_reason") is not None:
        ended_by = str(rows[-1]["stop_reason"])
    return summarize(rows, ended_by=ended_by, expected_calls=cap)


def _git_head() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def _write_new(path: Path, data: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def verify(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.trace, args.frames_dir, args.recording)
    print(json.dumps({"sources": len(corpus["sources"]), "unique_contexts": corpus["unique_contexts"],
                      "unique_images": corpus["unique_images"],
                      "historical_max_input_tokens": corpus["historical_max_input_tokens"],
                      "historical_max_cost_usd": corpus["historical_max_cost_usd"],
                      "trace_sha256": corpus["trace_sha256"], "requests_sha256": corpus["requests_sha256"],
                      "effective_max_calls": effective_max_calls()}, indent=2))
    return 0


async def _run(args: argparse.Namespace) -> int:
    min_interval_s = getattr(args, "min_interval_s", 0.0)
    if isinstance(min_interval_s, bool) or not isinstance(min_interval_s, (int, float)) \
            or not math.isfinite(min_interval_s) or min_interval_s < 0:
        raise ValueError("--min-interval-s must be a finite nonnegative number")
    corpus = load_corpus(args.trace, args.frames_dir, args.recording)
    approval, approval_sha = replay._load(args.approval)
    call_cap, cost_ceiling = validated_caps(approval)
    if args.directory.exists() and any(args.directory.iterdir()):
        raise ValueError("the run directory already holds evidence; refusing to rerun or mix probes")
    url, _key = load_credentials(args.env_file)
    args.directory.mkdir(parents=True, exist_ok=True)
    ordering = (f"first {call_cap} calls in chronological recorded order (trace_seq), no cycling "
                if call_cap <= len(corpus["sources"]) else
                "chronological recorded order (trace_seq), cycling from the start within the caps")
    _write_new(args.directory / "plan.json", {
        "version": 1, "kind": "liquid-d1-ultra-rate-probe", "arm": ARM, "model": MODEL,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "code_head": _git_head(),
        "script_sha256": digest(Path(__file__).read_bytes()), "approval_sha256": approval_sha,
        "trace_sha256": corpus["trace_sha256"], "contexts_sha256": corpus["contexts_sha256"],
        "requests_sha256": corpus["requests_sha256"], "images_sha256": corpus["images_sha256"],
        "unique_contexts": corpus["unique_contexts"], "unique_images": corpus["unique_images"],
        "source_calls": len(corpus["sources"]), "corpus_calls_used": min(call_cap, len(corpus["sources"])),
        "ordering": ordering,
        "caps": {"authorized_calls": HARD_MAX_CALLS, "effective_calls": call_cap,
                 "wall_s": MAX_WALL_S, "cost_ceiling_usd": str(cost_ceiling),
                 "reserved_per_call_usd": str(RESERVED_PER_CALL_USD),
                 "min_interval_s": float(min_interval_s),
                 "hard_max_calls": HARD_MAX_CALLS, "hard_max_cost_usd": str(HARD_MAX_COST_USD)},
        "scoring": {
            "design": "operational test of this one sequential paced stream; not a provider "
                      "limit and not gameplay or choice quality",
            "primary_metric": "fully elapsed rolling 60 s tokens from completed successful "
                              "calls (trailing (t-60, t] at each completion at least 60 s "
                              "after first dispatch); min and max eligible windows reported",
            "recommendation_rule": f"only if all {call_cap} calls succeed spanning at least "
                                   f"{RECOMMENDATION_MIN_SPAN_S:.0f} s with no 429, non-200, "
                                   "invalid choice, missing usage or cost, over-bound or "
                                   "deadline error: 80% of the MIN full 60 s window rounded "
                                   "down to the nearest 10,000 tokens/min; otherwise null",
            "failure_rule": "any 429, non-200, invalid choice, missing usage or cost, "
                            "cost over the reserved bound, or deadline error: report and "
                            "make no recommendation",
            "decision": "the parent makes the final operating-setting decision"},
        "historical": {"max_input_tokens": corpus["historical_max_input_tokens"],
                       "max_cost_usd": corpus["historical_max_cost_usd"],
                       "total_cost_usd": corpus["historical_total_cost_usd"]},
        "effort": None, "clock": "static replay; original contexts realtime, no game clock",
        "transport": "one persistent httpx connection, canonical bytes, remaining-wall per-call "
                     "timeout; live S1 uses urllib per call, so throughput is not claimed "
                     "equal to the live path"})
    ledger = call_budget.Ledger(args.directory, approval, ARM)
    client, send = await httpx_transport(url, os.environ.get("LIQUID_API_KEY") or _key)
    try:
        summary = await execute(corpus["sources"], ledger, send, max_calls=call_cap,
                                max_wall_s=MAX_WALL_S, min_interval_s=float(min_interval_s))
    finally:
        await client.aclose()
    _write_new(args.directory / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


def score(args: argparse.Namespace) -> int:
    rows = replay._calls(args.directory / "calls")
    last = sorted(rows, key=lambda row: row.get("probe_index", 0))[-1] if rows else None
    ended = str(last["stop_reason"]) if last and last.get("stop_reason") else "cap_or_wall_reached"
    expected = None
    plan_path = args.directory / "plan.json"
    if plan_path.is_file():
        plan, _ = replay._load(plan_path)
        caps = plan.get("caps") if isinstance(plan.get("caps"), dict) else {}
        if _is_int(caps.get("effective_calls")):
            expected = caps["effective_calls"]
    print(json.dumps(summarize(rows, ended_by=ended if rows else "no_calls",
                               expected_calls=expected), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "run"):
        child = sub.add_parser(name)
        child.add_argument("--trace", type=Path, required=True)
        child.add_argument("--frames-dir", type=Path, required=True)
        child.add_argument("--recording", type=Path, default=None)
    runner = sub.choices["run"]
    runner.add_argument("--approval", type=Path, required=True)
    runner.add_argument("--directory", type=Path, required=True)
    runner.add_argument("--env-file", type=Path, default=None)
    runner.add_argument("--min-interval-s", type=float, default=0.0,
                        help="minimum start-to-start spacing in seconds (paced follow-up: 1.0)")
    reporter = sub.add_parser("score")
    reporter.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            return verify(args)
        if args.command == "run":
            return asyncio.run(_run(args))
        return score(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"rate-probe: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
