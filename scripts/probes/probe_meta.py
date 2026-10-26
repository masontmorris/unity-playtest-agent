"""Day-1 Meta Model API probe (#4): Muse Spark 1.3 as frontier and preflight model.

Six Chat Completions calls with JSON-schema structured output:
  1-2  small escalation packet, sent twice (prefix-cache hit on the second?)
  3    large packet (~11k tokens), reasoning_effort=low
  4    same large packet, model-default effort (latency and cache across an effort change)
  5-6  small packet plus a 4 s, 640x360, 10 fps clip, sent twice (video tokens; cache with video)

Estimated cost before running: about 30k input tokens plus video and up to about 15k output
tokens at $1.25 / $4.25 per million, so under $0.15.

`--plan minimal` runs three calls at reasoning_effort=minimal instead (small, large, small + clip),
under $0.05.

    uv run --extra probe python scripts/probes/probe_meta.py [--plan minimal]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import jsonschema

sys.path.insert(0, str(Path(__file__).parent))
from common import Recorder, clip_mp4, data_url, env, run_dir, timed  # noqa: E402
from packets import FRONTIER_SCHEMA, FRONTIER_SYSTEM, frontier_user  # noqa: E402

URL = "https://api.meta.ai/v1/chat/completions"
MODEL = "muse-spark-1.3"
PRICE = {"input": 1.25, "cached": 0.15, "output": 4.25}  # USD per 1M tokens, Standard mode, 26 Oct 2026


def body(history: int, effort: str | None, video: str | None) -> dict:
    user: list[dict] | str = frontier_user(history)
    if video:
        user = [{"type": "text", "text": "Clip of the last 4 s of play, aligned with `recent`.\n" + user},
                {"type": "video_url", "video_url": {"url": video}}]
    b = {"model": MODEL, "max_tokens": 16000,
         "messages": [{"role": "system", "content": FRONTIER_SYSTEM}, {"role": "user", "content": user}],
         "response_format": {"type": "json_schema", "json_schema": {"name": "FrontierResponse", "strict": True,
                                                                    "schema": FRONTIER_SCHEMA}}}
    if effort:
        b["reasoning_effort"] = effort
    return b


def cost(u: dict) -> float | None:
    try:
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return ((u["prompt_tokens"] - cached) * PRICE["input"] + cached * PRICE["cached"]
                + u["completion_tokens"] * PRICE["output"]) / 1e6
    except (KeyError, TypeError):
        return None


async def main() -> int:
    key = env("META_API_KEY")
    if not key:
        print("META_API_KEY is not set")
        return 1
    minimal = sys.argv[1:] == ["--plan", "minimal"]
    out = run_dir("meta-minimal" if minimal else "meta")
    rec = Recorder(out)
    video = data_url(clip_mp4(), "video/mp4")
    if minimal:
        plan = [("small-minimal", 8, "minimal", None), ("large-minimal", 110, "minimal", None),
                ("video-minimal", 8, "minimal", video)]
    else:
        plan = [("small", 8, None, None), ("small-repeat", 8, None, None),
                ("large-low", 110, "low", None), ("large-default", 110, None, None),
                ("video", 8, None, video), ("video-repeat", 8, None, video)]
    total = 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
        for kind, history, effort, vid in plan:
            b = body(history, effort, vid)
            t0 = timed()
            try:
                r = await client.post(URL, json=b, headers={"Authorization": f"Bearer {key}"})
                status, text = r.status_code, r.text
            except httpx.HTTPError as exc:
                status, text = None, f"{type(exc).__name__}: {exc}"
            ms = (timed() - t0) * 1000
            data: dict = {}
            try:
                data = json.loads(text) if status else {}
            except ValueError:
                pass
            problem, parsed = (None if status == 200 else f"HTTP {status}: {text[:400]}"), None
            if problem is None:
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
                try:
                    parsed = json.loads(content)
                    jsonschema.validate(parsed, FRONTIER_SCHEMA)
                except (TypeError, ValueError, jsonschema.ValidationError) as exc:
                    problem = f"invalid structured output: {str(exc)[:200]}"
            u = data.get("usage") or {}
            c = cost(u)
            total += c or 0
            rec.write({"kind": kind, "status": status, "latency_ms": round(ms, 1), "problem": problem,
                       "resolved_model": data.get("model"), "usage": u, "est_cost_usd": c, "output": parsed,
                       "request": b})
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
            reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
            print(f"{kind:14} {status} {ms/1000:6.2f} s  in={u.get('prompt_tokens')} cached={cached} "
                  f"out={u.get('completion_tokens')} reasoning={reasoning} ${c if c is None else round(c, 4)}  "
                  f"{problem or 'ok'}", flush=True)
            if status is None or 400 <= status < 500:
                break  # a request or account error repeats on every call; stop instead of paying for it
    print(f"estimated total ${total:.4f}; raw calls: {rec.path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
