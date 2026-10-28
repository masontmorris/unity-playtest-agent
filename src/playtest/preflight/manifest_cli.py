"""Run preflight stages against recorded Editor evidence and write a local manifest."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sys

from playtest.contracts.bridge import InspectResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.preflight import cache, manifest, packet, provider, stages
from playtest.roles import preflight, strict


def _approved_budget(max_retries: int, approved_calls: int, approved_usd: str,
                     model: str = provider.meta.MODEL) -> tuple[int, Decimal]:
    """Conservative planning envelope using the recorded model context and token prices."""
    if approved_calls < 3:
        raise ValueError("approval must cover at least one call for each of the three stages")
    calls = min(3 * (1 + max_retries), approved_calls)
    if model not in provider.MODEL_PRICES:
        raise ValueError("unsupported preflight model")
    price_input, _, price_output = provider.MODEL_PRICES[model]
    per_call = (Decimal("1000000") * Decimal(str(price_input)) +
                Decimal("32000") * Decimal(str(price_output))) / Decimal("1000000")
    estimate = calls * per_call
    try:
        budget = Decimal(approved_usd)
    except InvalidOperation:
        raise ValueError("approved estimate must be a finite nonnegative dollar amount") from None
    if not budget.is_finite() or budget < 0:
        raise ValueError("approved estimate must be a finite nonnegative dollar amount")
    if budget < estimate:
        raise ValueError(f"approval must cover {calls} calls and an estimated ${estimate:.3f}")
    return calls, estimate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="inspected Unity project")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--screenshot-mime", choices=("image/png", "image/jpeg"), required=True)
    parser.add_argument("--screenshot-provenance", required=True, help="how and when the game frame was captured")
    parser.add_argument("--screenshot-same-session", choices=("yes", "no", "unknown"), default="unknown")
    parser.add_argument("--output", type=Path, required=True, help="new manifest outside the Unity project")
    parser.add_argument("--ledger", type=Path, required=True, help="new per-attempt JSONL outside the Unity project")
    parser.add_argument("--max-retries", type=int, choices=range(4), default=3)
    parser.add_argument("--model", choices=tuple(provider.MODEL_PRICES), default=provider.meta.MODEL)
    parser.add_argument("--cache-dir", type=Path, help="local selection cache outside the Unity project")
    parser.add_argument("--current-before", type=Path,
                        help="authored snapshot captured before the current inspections and calibration")
    parser.add_argument("--approved-calls", type=int, help="user-approved maximum request count on a cache miss")
    parser.add_argument("--approved-estimate-usd", help="user-approved planning estimate in USD on a cache miss")
    args = parser.parse_args(argv)
    project, output, ledger = args.project.resolve(), args.output.resolve(), args.ledger.resolve()
    if not project.is_dir():
        parser.error("--project must be an existing Unity project directory")
    if output == ledger or output.is_relative_to(project) or ledger.is_relative_to(project):
        parser.error("manifest and ledger must be different paths outside the Unity project")
    if output.exists() or ledger.exists():
        parser.error("manifest and ledger must be new files")
    cache_dir = args.cache_dir.resolve() if args.cache_dir is not None else None
    if cache_dir is not None and (cache_dir == project or cache_dir.is_relative_to(project)):
        parser.error("the cache directory must be outside the Unity project")
    try:
        inventory = InspectResult.model_validate_json(args.inventory.read_text(encoding="utf-8"))
        discovery = InspectResult.model_validate_json(args.discovery.read_text(encoding="utf-8"))
        calibration = CalibrationRecord.model_validate_json(args.calibration.read_text(encoding="utf-8"))
        same_session = {"yes": True, "no": False, "unknown": None}[args.screenshot_same_session]
        image = packet.Screenshot(args.screenshot.read_bytes(), args.screenshot_mime, args.screenshot_provenance,
                                  same_session=same_session)
        built = packet.build(inventory, discovery, calibration, image=image)
        # Render before creating a ledger or making any paid request.
        preflight.schema(built.discovery)
        if cache_dir is not None:
            cached = detail = None
            try:
                if args.current_before is None:
                    raise ValueError("cache replay requires --current-before from before the current preflight")
                before = cache._snapshot_from_data(json.loads(args.current_before.read_text(encoding="utf-8")))
                authored = cache.capture_authored(project)
                # This preflight's own inspections are the descriptors' current resolution.
                cache._check_window(before, authored, inventory, discovery, calibration)
                identity = cache.fingerprint(authored, inventory, discovery, model=args.model)
                print(f"preflight cache fingerprint {identity['sha256']}")
                entry = cache.read_entry(cache_dir, identity["sha256"])
                if entry is not None:
                    cached, detail = cache.replay_entry(entry, authored, inventory, discovery, calibration, image,
                                                        model=args.model)
                    detail.update(authored_before=cache._snapshot_data(before),
                                  authored_after=cache._snapshot_data(authored))
                else:
                    print("preflight cache miss: no entry for the current fingerprint")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                print(f"preflight cache miss: {exc}")
            if cached is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps({"kind": "cache_hit", **detail}, ensure_ascii=False,
                                            allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                with output.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(cached.model_dump_json(indent=2) + "\n")
                print(f"cache hit: zero model calls; manifest saved to {output}")
                print("stage 7 remains a separate live verification")
                return 0
        if args.approved_calls is None or args.approved_estimate_usd is None:
            raise ValueError("a cache miss needs an approved call count and estimated dollar envelope")
        max_calls, estimate = _approved_budget(args.max_retries, args.approved_calls, args.approved_estimate_usd, args.model)
        print(f"approved envelope: at most {max_calls} calls, estimated up to ${estimate:.3f} "
              "at the recorded prices and context bound; actual charges are not capped by this estimate")
        output.parent.mkdir(parents=True, exist_ok=True)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("x", encoding="utf-8", newline="\n") as stream:
            requests = 0
            collaborator = provider.Muse(built, model=args.model)
            def write(row: dict) -> None:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            def call(messages, schema):
                nonlocal requests
                if requests >= max_calls:
                    raise ValueError("the approved call envelope is exhausted")
                requests += 1
                request = provider.build_request(built, messages, schema, model=args.model)
                request_hash = hashlib.sha256(strict.canonical(request).encode("utf-8")).hexdigest()
                write({"kind": "reservation", "request": requests, "request_sha256": request_hash,
                       "model": args.model, "estimated_per_call_usd": str(estimate / max_calls)})
                return collaborator(messages, schema)
            def record(attempt: stages.Attempt) -> None:
                write({"kind": "attempt", "request": requests, **asdict(attempt)})
            result = stages.run(built, call, record, max_retries=args.max_retries)
        if any(part.answer is None for part in (result.readers, result.actions, result.progress)):
            print(f"preflight did not complete; attempts are recorded in {ledger}", file=sys.stderr)
            return 1
        assembled = manifest.assemble(built, result)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(assembled.model_dump_json(indent=2) + "\n")
    except (OSError, ValueError, TypeError) as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 1
    print(f"manifest saved to {output}; attempts saved to {ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
