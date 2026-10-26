"""Day-1 S1 candidate probe (#5): latency, image support, context size and the answer contract.

Per candidate: 1 warm-up, 5 small text calls, 3 large text calls (near the 12k design budget) and
2 small calls with a 1280x720 frame. Calls run one at a time. A candidate that fails auth or
billing stops after its first error, so a broken endpoint costs one call.

    uv run --extra probe python scripts/probes/probe_s1.py [--only mammoth-1,elephant-1]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from common import Recorder, data_url, env, frame_png, pct, run_dir, timed  # noqa: E402
from packets import CHOICES, s1_body  # noqa: E402

BASETEN = "https://inference.baseten.co/v1/systemone"
SMALL, LARGE = 8, 110  # history entries


@dataclass(frozen=True)
class Candidate:
    name: str
    url_env: str | None
    url: str | None
    model: str
    key_env: str
    image_transport: str


CANDIDATES = [
    Candidate("mammoth-1", None, BASETEN, "decision-models/baseten-mammoth-27b-latest", "BASETEN_API_KEY", "messages"),
    Candidate("elephant-1", None, BASETEN, "decision-models/baseten-elephant-9b-latest", "BASETEN_API_KEY", "messages"),
    Candidate("liquid-d1.1", "LIQUID_D1.1_URL", None, "d1.1", "LIQUID_API_KEY", "images"),
]


def check_answer(data: dict) -> str | None:
    """None if the answer meets the typed-choice contract, else what is wrong."""
    ans = (data.get("answers") or {}).get("next_action")
    if not isinstance(ans, dict) or ans.get("type") != "choice":
        return "no answers.next_action of type choice"
    if ans.get("choice") not in CHOICES:
        return f"choice {ans.get('choice')!r} not offered"
    probs = ans.get("probabilities")
    if not isinstance(probs, dict) or set(probs) != set(CHOICES):
        return "probabilities do not cover exactly the offered choices"
    if not all(isinstance(p, (int, float)) and math.isfinite(p) and 0 <= p <= 1 for p in probs.values()):
        return "probability out of range"
    if abs(sum(probs.values()) - 1) > 0.01:
        return f"probabilities sum to {sum(probs.values()):.3f}"
    return None


def usage_tokens(data: dict) -> int | None:
    u = data.get("usage") or {}
    for k in ("input_tokens", "prompt_tokens", "total_input_tokens"):
        if isinstance(u.get(k), int):
            return u[k]
    return None


async def probe(c: Candidate, client: httpx.AsyncClient, rec: Recorder, image: str) -> dict:
    url = c.url or env(c.url_env or "")
    key = env(c.key_env)
    summary: dict = {"candidate": c.name, "model": c.model, "calls": 0, "errors": []}
    if not url or not key:
        summary["errors"].append(f"missing {'url' if not url else c.key_env}")
        return summary
    plan = [("warmup", SMALL, False)] + [("small", SMALL, False)] * 5 + [("large", LARGE, False)] * 3 + [("image", SMALL, True)] * 2
    lat: dict[str, list[float]] = {"small": [], "large": [], "image": []}
    tokens: dict[str, list[int]] = {"small": [], "large": [], "image": []}
    for kind, history, with_image in plan:
        body = s1_body(c.model, history, image if with_image else None, c.image_transport)
        t0 = timed()
        try:
            r = await client.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
            ms = (timed() - t0) * 1000
            status, text = r.status_code, r.text
        except httpx.HTTPError as exc:
            ms, status, text = (timed() - t0) * 1000, None, f"{type(exc).__name__}: {exc}"
        summary["calls"] += 1
        data: dict = {}
        try:
            data = json.loads(text) if status else {}
        except ValueError:
            pass
        problem = None if status == 200 else f"HTTP {status}: {text[:300]}"
        if problem is None:
            problem = check_answer(data)
        row = {"candidate": c.name, "kind": kind, "status": status, "latency_ms": round(ms, 1), "problem": problem,
               "resolved_model": data.get("model"), "usage": data.get("usage"),
               "answer": (data.get("answers") or {}).get("next_action"), "request": body}
        rec.write(row)
        print(f"  {c.name:12} {kind:6} {status} {ms:7.0f} ms  tok={usage_tokens(data)}  {problem or 'ok'}", flush=True)
        if problem:
            summary["errors"].append(f"{kind}: {problem}")
            if status in (401, 402, 403, 404) or status is None:
                break  # auth, billing, wrong endpoint or network: stop this candidate
            continue
        summary["resolved_model"] = data.get("model")
        if kind != "warmup":
            lat[kind].append(ms)
            if (t := usage_tokens(data)) is not None:
                tokens[kind].append(t)
    for kind in lat:
        summary[kind] = {"n": len(lat[kind]), "p50_ms": pct(lat[kind], 50), "p95_ms": pct(lat[kind], 95),
                         "input_tokens": sorted(set(tokens[kind])) or None}
    return summary


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated candidate names")
    args = ap.parse_args()
    chosen = [c for c in CANDIDATES if not args.only or c.name in args.only.split(",")]
    out = run_dir("s1")
    rec = Recorder(out)
    image = data_url(frame_png(), "image/png")
    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        for c in chosen:
            print(f"probing {c.name}", flush=True)
            results.append(await probe(c, client, rec, image))
    (out / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    print(f"raw calls: {rec.path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
