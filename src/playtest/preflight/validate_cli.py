"""Validate a saved preflight manifest against its recorded source evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from playtest.contracts.bridge import InspectResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight import cache, manifest, packet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, help="the original screenshot, if the manifest has one")
    parser.add_argument("--expected-model", choices=("muse-spark-1.3", "muse-spark-1.3-contributor"))
    parser.add_argument("--project", type=Path, help="the Unity project, required for cache-hit validation")
    parser.add_argument("--cache-dir", type=Path, help="the selection cache, required for cache-hit validation")
    args = parser.parse_args(argv)
    try:
        value = PreflightManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"), strict=True)
        image = None
        if value.image is not None:
            if args.screenshot is None:
                raise ValueError("the manifest requires its original screenshot")
            image = packet.Screenshot(args.screenshot.read_bytes(), value.image.mime, value.image.provenance,
                                      value.image.same_session, value.image.capture_game_s)
        elif args.screenshot is not None:
            raise ValueError("the manifest does not describe a screenshot")
        inventory = InspectResult.model_validate_json(args.inventory.read_text(encoding="utf-8"))
        discovery = InspectResult.model_validate_json(args.discovery.read_text(encoding="utf-8"))
        calibration = CalibrationRecord.model_validate_json(args.calibration.read_text(encoding="utf-8"))
        built = packet.build(inventory, discovery, calibration, image=image)
        ledger = [json.loads(line) for line in args.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(ledger) == 1 and isinstance(ledger[0], dict) and ledger[0].get("kind") == "cache_hit":
            if args.project is None or args.cache_dir is None:
                raise ValueError("cache-hit validation requires --project and --cache-dir")
            project, directory = args.project.resolve(strict=True), args.cache_dir.resolve()
            if directory == project or directory.is_relative_to(project):
                raise ValueError("the cache directory must be outside the Unity project")
            event = ledger[0]
            model = event.get("model")
            if args.expected_model is not None and model != args.expected_model:
                raise ValueError("the cached model differs from the required model")
            key = event.get("fingerprint")
            if not isinstance(key, str):
                raise ValueError("the cache-hit fingerprint is missing")
            entry = cache.read_entry(directory, key)
            if entry is None:
                raise ValueError("the referenced cache entry is unavailable")
            before = cache._snapshot_from_data(event.get("authored_before"))
            after = cache._snapshot_from_data(event.get("authored_after"))
            cache._check_window(before, after, inventory, discovery, calibration)
            if cache.capture_authored(project).files != after.files:
                raise ValueError("the authored project differs from the cache hit's after snapshot")
            replayed, detail = cache.replay_entry(entry, after, inventory, discovery, calibration, image,
                                                   model=model)
            detail.update(authored_before=cache._snapshot_data(before), authored_after=cache._snapshot_data(after))
            if replayed != value or event != {"kind": "cache_hit", **detail}:
                raise ValueError("the cache-hit ledger or manifest differs from replay")
            print("manifest and cache source validate against current authored files and descriptors")
            print("live action effects and reset verification remain unverified")
            return 0
        if len(ledger) % 2:
            raise ValueError("ledger has an unresolved in-flight request")
        rows = []
        reservations = []
        for offset in range(0, len(ledger), 2):
            reservation, attempt = ledger[offset:offset + 2]
            if (not isinstance(reservation, dict) or not isinstance(attempt, dict) or
                    reservation.get("kind") != "reservation" or attempt.get("kind") != "attempt" or
                    reservation.get("request") != offset // 2 + 1 or
                    attempt.get("request") != reservation.get("request") or
                    not isinstance(reservation.get("request_sha256"), str) or
                    len(reservation["request_sha256"]) != 64):
                raise ValueError("ledger reservations and attempts are not paired and ordered")
            rows.append(attempt)
            reservations.append(reservation)
        problems = manifest.validate_evidence(value, built) + manifest.validate_ledger(
            value, built, rows, reservations, expected_model=args.expected_model)
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
    except (OSError, ValueError, TypeError) as exc:
        print(f"manifest validation failed: {exc}", file=sys.stderr)
        return 1
    print("manifest schema, references, semantics, source packet and recorded answers validate")
    print("live reader installation, action effects and reset verification remain unverified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
