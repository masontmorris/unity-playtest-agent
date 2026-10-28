"""Validate a saved preflight manifest against its recorded source evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from playtest.contracts.bridge import InspectResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight import manifest, packet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, help="the original screenshot, if the manifest has one")
    parser.add_argument("--expected-model", choices=("muse-spark-1.3", "muse-spark-1.3-contributor"))
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
        built = packet.build(InspectResult.model_validate_json(args.inventory.read_text(encoding="utf-8")),
                             InspectResult.model_validate_json(args.discovery.read_text(encoding="utf-8")),
                             CalibrationRecord.model_validate_json(args.calibration.read_text(encoding="utf-8")),
                             image=image)
        ledger = [json.loads(line) for line in args.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
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
