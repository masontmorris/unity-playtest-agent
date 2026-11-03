"""Send one frozen image-correctness arm: 3 warmups + 12 measurements.

This is a paid-call client. It never executes a returned choice. Black frames
are diagnostic controls paired with the original text, not live game views.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import time
import urllib.parse
from pathlib import Path

from mammoth_modal_calls import valid

NAMES = ("short-real", "short-black", "long-real", "long-black")


def schedule():
    warmups = [(name, True) for name in ("short-real", "long-real", "short-black")]
    measurements = [(name, False) for name in NAMES for _ in range(3)]
    random.Random(20261102).shuffle(measurements)
    return warmups + measurements


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--kind", choices=("flide", "reference"), required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--cpu-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("expected an HTTPS server base URL")
    key, secret = os.environ.get("MODAL_PROXY_TOKEN_ID"), os.environ.get("MODAL_PROXY_TOKEN_SECRET")
    if not key or not secret:
        raise ValueError("Modal proxy credentials are required in the environment")
    manifest = json.loads((args.inputs / "manifest.json").read_bytes())
    entries = {row["name"]: row for row in manifest["cases"]}
    cpu = json.loads(args.cpu_result.read_bytes())
    bodies = {}
    for name in NAMES:
        data = (args.inputs / f"{name}-{args.kind}.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != entries[name]["body_sha256"][args.kind]:
            raise ValueError(f"frozen body hash mismatch for {name}")
        left, right = cpu["flide"][name], cpu["reference"][name]
        pixels = cpu["pixel_comparison"][name]
        if any(left[k] != right[k] for k in ("prompt_sha256", "ids_sha256", "prefix_boundary", "grid")) \
           or left["image_tokens"] != 300 or pixels["max_abs_difference"] is None \
           or pixels["max_abs_difference"] > 1e-5:
            raise ValueError(f"CPU preprocessing comparison failed for {name}")
        bodies[name] = data
    args.output.mkdir(parents=True, exist_ok=False)
    ledger = args.output / "calls.jsonl"
    endpoint = "/v1/systemone" if args.kind == "flide" else "/v1/system-one"
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=120)
    try:
        for index, (name, warmup) in enumerate(schedule()):
            data = bodies[name]
            offered = set(json.loads(data)["questions"]["next_action"]["criteria"])
            start = time.perf_counter()
            try:
                conn.request("POST", endpoint, body=data,
                             headers={"Content-Type": "application/json", "Modal-Key": key, "Modal-Secret": secret})
                response = conn.getresponse()
                raw, status = response.read(), response.status
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    value = None
                problem = None if status == 200 and valid(value, offered) else "invalid_response"
                expected = cpu[args.kind][name]
                if problem is None and value.get("usage", {}).get("input_tokens") != expected["tokens"]:
                    problem = "runtime_token_count_mismatch"
                if problem is None and args.kind == "flide" and \
                   value.get("onejev", {}).get("prompt_sha256") != expected["prompt_sha256"]:
                    problem = "runtime_prompt_hash_mismatch"
                inference_ms = response.getheader("X-Decision-Inference-Ms")
            except (OSError, http.client.HTTPException) as exc:
                status, value, inference_ms, problem = None, None, None, type(exc).__name__
            row = {"index": index, "case": name, "warmup": warmup, "body_sha256": hashlib.sha256(data).hexdigest(),
                   "status": status, "http_ms": (time.perf_counter() - start) * 1000,
                   "server_inference_ms": inference_ms, "problem": problem, "response": value}
            with ledger.open("a", encoding="utf8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if problem:
                raise RuntimeError(f"stopped after request {index}: {problem} (HTTP {status})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
