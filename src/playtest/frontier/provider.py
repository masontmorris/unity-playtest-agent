"""Muse Chat Completions transport for the frontier (#33).

One run-stable manifest message, one packet message, one schema-constrained answer. The transport
sends once: a transport failure is reported, never retried or moved to another model. An answer
that is refused, truncated or malformed stays rejected; the parser never repairs it. Semantic
problems are returned per item by `playtest.roles.frontier.review`, not folded into a substitute
answer.

The clip is attached as a data URI, so no filesystem path reaches the request or a saved record.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from playtest.core.context import canonical
from playtest.roles import frontier, strict

from .packet import Packet

MODEL = "muse-spark-1.3"
URL = "https://api.meta.ai/v1/chat/completions"

# USD per million tokens. A cached prompt token replaces the normal prompt rate; it is not added.
PRICE_INPUT_PER_MTOK = 1.25
PRICE_CACHED_INPUT_PER_MTOK = 0.15
PRICE_OUTPUT_PER_MTOK = 4.25

_TIMEOUT_S = 60
_MANIFEST_NOTE = (
    "A null value means unknown, not zero. Each seq is a bridge record number; game_s is game time. "
    "A directive setpoint must name one of these fields and use a value of its kind. Cite only the "
    "observation seq, a fact's seqs and a recent row's seq: numbers inside hypotheses or directives "
    "are earlier records, not new citations. A field's calibration, where present, is how it changed when "
    "preflight sampled it on this build before play, idle and with each control held."
)
# Preflight's freshness classes (`preflight.freshness`), worded for the model. A field without one is unknown.
CALIBRATION_LABELS = {
    "input": "changed only while a control was held",
    "idle": "changed only with no control held",
    "both": "changed with and without a control held",
    "no_change_observed": "no change seen, with or without a control held",
}
_DATA_URL = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Result:
    """One decoded frontier call. Semantic problems stay in `review`; errors mean no answer."""

    answer: frontier.FrontierAnswer | None
    review: frontier.Review | None
    errors: list[str]
    resolved_model: str | None
    usage: dict[str, Any] | None
    cost_usd: float | None


def manifest_message(manifest: frontier.Manifest) -> str:
    """The run-stable first user message: fields, action bounds and skills, sorted for caching."""
    fields = []
    for path in sorted(manifest.fields):
        row = {"path": path, "kind": manifest.fields[path]}
        label = CALIBRATION_LABELS.get(manifest.calibration.get(path, ""))
        if label is not None:
            row["calibration"] = label
        fields.append(row)
    actions = [
        {"name": name, "axis": spec.axis, "max_hold_s": spec.max_hold_s}
        for name, spec in sorted(manifest.actions.items())
    ]
    skills = sorted(manifest.skills)
    return canonical({"fields": fields, "actions": actions, "skills": skills, "notes": _MANIFEST_NOTE})


def build_request(
    manifest: frontier.Manifest,
    packet: Packet,
    *,
    clip_path: Path | None = None,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Build the one request for this packet. The answer schema is stable for the whole run."""
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    if not isinstance(packet, Packet):
        raise ValueError("build_request needs a Packet")
    video = _video_part(packet, clip_path)
    text = packet.canonical()
    if video is None:
        packet_content: Any = text
    else:
        packet_content = [{"type": "text", "text": text}, video]
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": frontier.SYSTEM},
            {"role": "user", "content": manifest_message(manifest)},
            {"role": "user", "content": packet_content},
        ],
        "response_format": strict.response_format("frontier_answer", frontier.schema(manifest)),
        "reasoning_effort": "minimal",
        "max_tokens": max_tokens,
    }


def send_meta(body: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None, float, str | None]:
    """POST once to Muse. Returns status, decoded object, latency and a non-secret problem.

    The credential comes from `META_API_KEY` and is never returned, logged or included in a
    problem. An HTTP error's body is not read: it can echo the request.
    """
    if not isinstance(body, dict) or not body:
        raise ValueError("the request body must be a non-empty object")
    key = os.environ.get("META_API_KEY")
    if not key:
        raise ValueError("META_API_KEY is not set")
    request = urllib.request.Request(
        URL,
        data=canonical(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None, (time.monotonic() - started) * 1000.0, "HTTP error"
    except (OSError, urllib.error.URLError):
        return None, None, (time.monotonic() - started) * 1000.0, "transport error"
    latency = (time.monotonic() - started) * 1000.0
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        return status, None, latency, "response was not JSON"
    if not isinstance(value, dict):
        return status, None, latency, "response was not a JSON object"
    return status, value, latency, None


def decode_response(
    data: dict[str, Any] | None,
    manifest: frontier.Manifest,
    packet: Packet,
) -> Result:
    """Decode one response. Refusal, truncation, a bad envelope or bad JSON returns no answer."""
    if not isinstance(data, dict):
        return Result(None, None, ["the response is not a JSON object"], None, None, None)

    resolved_model = data.get("model") if isinstance(data.get("model"), str) and data["model"] else None
    usage = _usage_copy(data.get("usage"))
    cost = _cost(usage)
    if data.get("error") is not None:
        return Result(None, None, ["the response reports an error"], resolved_model, usage, cost)

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return Result(None, None, ["the response has no choices"], resolved_model, usage, cost)
    choice = choices[0]
    if not isinstance(choice, dict):
        return Result(None, None, ["the first choice is not an object"], resolved_model, usage, cost)
    message = choice.get("message")
    if not isinstance(message, dict):
        return Result(None, None, ["the first choice has no message"], resolved_model, usage, cost)

    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return Result(None, None, ["the model refused the request"], resolved_model, usage, cost)

    finish = choice.get("finish_reason")
    if finish != "stop":
        return Result(
            None,
            None,
            [f"the answer is not complete: finish_reason is {finish!r}, not 'stop'"],
            resolved_model,
            usage,
            cost,
        )

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return Result(None, None, ["the answer content is not text"], resolved_model, usage, cost)

    value, errors = strict.parse(frontier.FrontierAnswer, content)
    if value is None:
        return Result(None, None, errors, resolved_model, usage, cost)
    # The role parser reports useful JSON/shape errors, but its normal Pydantic mode can
    # coerce strings to numbers and booleans. A provider answer must satisfy the sent types.
    try:
        answer = frontier.FrontierAnswer.model_validate_json(content, strict=True)
    except ValidationError as exc:
        errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        return Result(None, None, errors, resolved_model, usage, cost)
    try:
        canonical(answer.model_dump(mode="json"))
    except (TypeError, ValueError) as exc:
        return Result(None, None, [f"the answer is not finite JSON: {exc}"], resolved_model, usage, cost)
    review = _review_answer(answer, manifest, packet)
    return Result(answer, review, [], resolved_model, usage, cost)


def redact_media(request: dict[str, Any]) -> dict[str, Any]:
    """A persisted copy of a request with every media data URI replaced by size and hash."""
    out = copy.deepcopy(request)
    messages = out.get("messages")
    if not isinstance(messages, list):
        return out
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "video_url":
                part["video_url"] = _media_summary(part.get("video_url"))
    return out


# --- helpers -------------------------------------------------------------------------------------


def _review_answer(answer: frontier.FrontierAnswer, manifest: frontier.Manifest, packet: Packet) -> frontier.Review:
    """The semantic review plus the dynamic enum `review` does not check on its own."""
    review = frontier.review(answer, manifest, packet.refs)
    known = set(manifest.actions) | set(manifest.skills)
    # `PruneChoice.name` is a manifest enum in the rendered schema. Pydantic keeps it a string,
    # so an unknown name must be reported here rather than decoded as valid.
    for i, item in enumerate(answer.prune_choices):
        if item.name not in known:
            review.problems.append(
                frontier.Problem(f"prune_choices[{i}]", f"{item.name!r} is not a manifest action or skill")
            )
    return review


def _video_part(packet: Packet, clip_path: Path | None) -> dict[str, Any] | None:
    clip = packet.payload.get("clip") if isinstance(packet.payload, dict) else None
    if clip is None:
        if clip_path is not None:
            raise ValueError("a clip file was supplied but the packet declares no clip")
        return None
    if clip_path is None:
        raise ValueError("the packet declares a clip but no clip file was supplied")
    if not isinstance(clip, Mapping):
        raise ValueError("the clip descriptor is not an object")
    expected_sha = clip.get("sha256")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
        raise ValueError("the clip descriptor needs a 64-character sha256 digest")
    expected_bytes = clip.get("bytes")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes <= 0:
        raise ValueError("the clip descriptor needs a positive byte count")
    if clip.get("mime", "video/mp4") != "video/mp4":
        raise ValueError("only an MP4 clip can be attached")
    try:
        raw = clip_path.read_bytes()
    except OSError:
        raise ValueError("the clip file could not be read") from None
    if len(raw) != expected_bytes:
        raise ValueError(f"clip size {len(raw)} does not match the packet descriptor {expected_bytes}")
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha.lower():
        raise ValueError("clip sha256 does not match the packet descriptor")
    encoded = base64.b64encode(raw).decode("ascii")
    return {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{encoded}"}}


def _media_summary(value: Any) -> dict[str, Any]:
    url = value.get("url") if isinstance(value, Mapping) else None
    if not isinstance(url, str):
        return {"redacted": True}
    match = _DATA_URL.match(url)
    if match is None:
        return {"redacted": True}
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (ValueError, TypeError):
        return {"redacted": True}
    return {
        "redacted": True,
        "mime": match.group(1),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    if value < 0:
        return None
    return int(value)


def _reject_constant(value: str) -> Any:
    """JSON has no NaN or Infinity; treat either as a malformed response."""
    raise ValueError(f"{value} is not valid JSON")


def _usage_copy(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    copied = copy.deepcopy(value)
    try:
        canonical(copied)
    except (TypeError, ValueError):
        return None
    return copied


def _cost(usage: Mapping[str, Any] | None) -> float | None:
    """Cost from validated token counts; null when any input the price needs is unknown."""
    if not isinstance(usage, Mapping):
        return None
    prompt = _count(usage.get("prompt_tokens"))
    completion = _count(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    if prompt is None or completion is None or not isinstance(details, Mapping):
        return None
    cached = _count(details.get("cached_tokens"))
    if cached is None or cached > prompt:
        return None
    return (
        (prompt - cached) * PRICE_INPUT_PER_MTOK
        + cached * PRICE_CACHED_INPUT_PER_MTOK
        + completion * PRICE_OUTPUT_PER_MTOK
    ) / 1_000_000
