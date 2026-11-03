"""Execute one predeclared, no-retry Mammoth Modal pilot arm.

Requires a ready, private Modal Server and four bodies frozen by
``mammoth_modal_inputs.py``. This client does not start or stop the server.
Calling it incurs GPU charges; approval and a balance check are prerequisites.
Proxy credentials are read from environment variables, never printed or stored.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import random
import time
import urllib.parse
from pathlib import Path


NAMES = ("short-text", "long-text", "short-image", "long-image")
SEED = 20261102


def schedule() -> list[tuple[str, bool]]:
    result = [("short-text", True)] * 3
    measured = [(name, False) for name in NAMES for _ in range(3)]
    random.Random(SEED).shuffle(measured)
    return result + measured


def valid(response: object, offered: set[str]) -> bool:
    if not isinstance(response, dict):
        return False
    answers = response.get("answers")
    answer = answers.get("next_action") if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("choice") not in offered:
        return False
    probs = answer.get("probabilities")
    if not isinstance(probs, dict) or set(probs) != offered:
        return False
    values = list(probs.values())
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               and 0 <= v <= 1 for v in values) and abs(sum(values) - 1) <= 0.001


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--kind", required=True, choices=("flide", "reference"))
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("use an HTTPS server base URL with no path")
    key, secret = os.environ.get("MODAL_PROXY_TOKEN_ID"), os.environ.get("MODAL_PROXY_TOKEN_SECRET")
    if not key or not secret:
        raise ValueError("Modal proxy credentials are required in the environment")
    manifest = json.loads((args.inputs / "manifest.json").read_bytes())
    rows = {r["name"]: r for r in manifest["contexts"]}
    bodies = {}
    for name in NAMES:
        data = (args.inputs / f"{name}-{args.kind}.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != rows[name][f"{args.kind}_body_sha256"]:
            raise ValueError(f"frozen body hash mismatch: {name}")
        bodies[name] = data
    args.output.mkdir(parents=True, exist_ok=False)
    ledger = args.output / "calls.jsonl"
    endpoint = "/v1/systemone" if args.kind == "flide" else "/v1/system-one"
    conn = http.client.HTTPSConnection(parsed.hostname, port=parsed.port, timeout=120)
    try:
        for index, (name, warmup) in enumerate(schedule()):
            data = bodies[name]
            offered = set(json.loads(data)["questions"]["next_action"]["criteria"])
            start = time.perf_counter()
            try:
                conn.request("POST", endpoint, body=data, headers={"Content-Type": "application/json",
                             "Modal-Key": key, "Modal-Secret": secret})
                resp = conn.getresponse()
                raw, status = resp.read(), resp.status
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    result = None
                issue = None if status == 200 and valid(result, offered) else "invalid_response"
                timing = resp.getheader("X-Decision-Inference-Ms")
            except (OSError, http.client.HTTPException) as exc:
                status, result, timing, issue = None, None, None, type(exc).__name__
            elapsed = (time.perf_counter() - start) * 1000
            row = {"index": index, "context": name, "warmup": warmup,
                   "body_sha256": hashlib.sha256(data).hexdigest(), "status": status,
                   "http_ms": elapsed, "server_inference_ms": timing, "problem": issue,
                   "response": result}
            with ledger.open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
            if issue:
                raise RuntimeError(f"stopped after request {index}: {issue} (HTTP {status})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
