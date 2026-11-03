"""Send one frozen Flide precision arm (BF16 or FP8): 8 warmups + 20 measurements.

This is a paid-call client. It never executes a returned choice. The six image warmups are there to
initialize the image processor in each of Flide's four planning threads before anything is measured.
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

NAMES = ("short-text", "long-text", "short-image", "long-image")
TOKENS = {"short-text": 4196, "long-text": 7476, "short-image": 8798, "long-image": 16290}


def schedule():
    warmups = [("short-text", True), ("long-text", True)] + [("short-image", True), ("long-image", True)] * 3
    measurements = [(name, False) for name in NAMES for _ in range(5)]
    random.Random(20261102).shuffle(measurements)
    return warmups + measurements


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("expected an HTTPS server base URL")
    key, secret = os.environ.get("MODAL_PROXY_TOKEN_ID"), os.environ.get("MODAL_PROXY_TOKEN_SECRET")
    if not key or not secret:
        raise ValueError("Modal proxy credentials are required in the environment")
    manifest = json.loads((args.inputs / "manifest.json").read_bytes())
    expected = {row["name"]: row["flide_body_sha256"] for row in manifest["contexts"]}
    bodies = {}
    for name in NAMES:
        data = (args.inputs / f"{name}-flide.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != expected[name]:
            raise ValueError(f"frozen body hash mismatch for {name}")
        bodies[name] = data
    args.output.mkdir(parents=True, exist_ok=False)
    ledger = args.output / "calls.jsonl"
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=120)
    try:
        for index, (name, warmup) in enumerate(schedule()):
            data = bodies[name]
            offered = set(json.loads(data)["questions"]["next_action"]["criteria"])
            start = time.perf_counter()
            try:
                conn.request("POST", "/v1/systemone", body=data,
                             headers={"Content-Type": "application/json", "Modal-Key": key, "Modal-Secret": secret})
                response = conn.getresponse()
                raw, status = response.read(), response.status
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    value = None
                problem = None if status == 200 and valid(value, offered) else "invalid_response"
                if problem is None and value.get("usage", {}).get("input_tokens") != TOKENS[name]:
                    problem = "token_count_mismatch"
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
