"""One Muse request per preflight attempt; stage policy stays in the runner."""

from __future__ import annotations

import base64
import copy
from collections.abc import Callable
from typing import Any

from playtest.frontier import provider as meta
from playtest.preflight.packet import Packet
from playtest.preflight.stages import CallResult
from playtest.roles import preflight, strict

CONTRIBUTOR_MODEL = "muse-spark-1.3-contributor"
MODEL_PRICES = {
    meta.MODEL: (1.25, 0.15, 4.25),
    CONTRIBUTOR_MODEL: (0.10, 0.002, 0.20),
}


def build_request(packet: Packet, messages: list[dict[str, str]], schema: dict[str, Any], *,
                  model: str = meta.MODEL) -> dict[str, Any]:
    """Build a request with a stable schema and, when available, the exact captured image."""
    if len(messages) < 2 or messages[0] != {"role": "system", "content": preflight.SYSTEM}:
        raise ValueError("the preflight system message is missing or changed")
    if messages[1] != {"role": "user", "content": preflight.first_turn(packet.text, "readers")}:
        raise ValueError("the first message does not match this packet")
    if strict.canonical(schema) != strict.canonical(preflight.schema(packet.discovery)):
        raise ValueError("the preflight schema does not match this packet's discovery")
    if model not in MODEL_PRICES:
        raise ValueError("unsupported preflight model")
    output_messages: list[dict[str, Any]] = copy.deepcopy(messages)
    if packet.image is not None:
        descriptor = packet.image.descriptor()
        data = base64.b64encode(packet.image.data).decode("ascii")
        output_messages[1]["content"] = [
            {"type": "text", "text": messages[1]["content"]},
            {"type": "image_url", "image_url": {"url": f"data:{descriptor['mime']};base64,{data}"}},
        ]
    return {"model": model, "messages": output_messages,
            "response_format": strict.response_format("preflight_answer", schema),
            "reasoning_effort": "minimal", "max_tokens": 32000}


class Muse:
    """Adapter for `stages.run`. No retries, fallback, or action selection live here."""

    def __init__(self, packet: Packet,
                 send: Callable[[dict[str, Any]], tuple[int | None, dict[str, Any] | None, float, str | None]]
                 = meta.send_meta, *, model: str = meta.MODEL) -> None:
        if model not in MODEL_PRICES:
            raise ValueError("unsupported preflight model")
        self.packet = packet
        self.send = send
        self.model = model

    def __call__(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> CallResult:
        request = build_request(self.packet, messages, schema, model=self.model)
        status, data, latency, error = self.send(request)
        usage = meta._usage_copy(data.get("usage")) if isinstance(data, dict) else None
        model = data.get("model") if isinstance(data, dict) else None
        model = model if isinstance(model, str) and model else None
        metadata = {"endpoint": meta.URL, "model": self.model, "resolved_model": model,
                    "status": status, "latency_ms": latency, "usage": usage,
                    "estimated_cost_usd": _cost(usage, self.model), "reported_cost_usd": None,
                    "image": self.packet.image.descriptor() if self.packet.image is not None else None}
        if error is not None or status != 200:
            return CallResult(None, error or f"HTTP status {status}", metadata,
                              "transport" if status is None else "provider")
        if not isinstance(data, dict) or data.get("error") is not None:
            return CallResult(None, "the response is missing or reports an error", metadata, "provider")
        if model is not None and model != self.model:
            return CallResult(None, "the provider resolved a different model", metadata, "provider")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return CallResult(None, "the response has no first choice", metadata, "provider")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            return CallResult(None, "the first choice has no message", metadata, "provider")
        if message.get("refusal"):
            return CallResult(None, "the model refused the request", metadata, "refusal")
        finish = choice.get("finish_reason")
        if finish != "stop":
            kind = "truncated" if finish == "length" else "refusal" if finish == "content_filter" else "provider"
            return CallResult(None, f"the answer is not complete: finish_reason is {finish!r}", metadata, kind)
        content = message.get("content")
        if not isinstance(content, str):
            return CallResult(None, "the answer content is not text", metadata, "provider")
        return CallResult(content, None, metadata)


def _cost(usage: dict[str, Any] | None, model: str) -> float | None:
    if not isinstance(usage, dict):
        return None
    prompt, completion = meta._count(usage.get("prompt_tokens")), meta._count(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    cached = meta._count(details.get("cached_tokens")) if isinstance(details, dict) else None
    if prompt is None or completion is None or cached is None or cached > prompt:
        return None
    price_input, price_cached, price_output = MODEL_PRICES[model]
    return ((prompt - cached) * price_input + cached * price_cached + completion * price_output) / 1_000_000
