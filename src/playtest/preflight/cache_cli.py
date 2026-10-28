"""Capture authored snapshots and seal a validated local preflight selection cache."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from playtest.contracts.bridge import InspectResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight import cache, packet, provider


def _outside(path: Path, project: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved == project or resolved.is_relative_to(project):
        raise ValueError(f"{label} must be outside the Unity project")
    return resolved


def _read_image(path: Path | None, value: PreflightManifest) -> packet.Screenshot | None:
    if value.image is None:
        if path is not None:
            raise ValueError("the manifest does not describe a screenshot")
        return None
    if path is None:
        raise ValueError("the manifest requires its original screenshot")
    return packet.Screenshot(path.read_bytes(), value.image.mime, value.image.provenance,
                             value.image.same_session, value.image.capture_game_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot", help="capture authored files before or after a preflight")
    snapshot.add_argument("--project", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    store = sub.add_parser("store", help="seal a complete, validated selection and its evidence")
    store.add_argument("--project", type=Path, required=True)
    store.add_argument("--before", type=Path, required=True)
    store.add_argument("--after", type=Path, required=True)
    store.add_argument("--inventory", type=Path, required=True)
    store.add_argument("--discovery", type=Path, required=True)
    store.add_argument("--calibration", type=Path, required=True)
    store.add_argument("--manifest", type=Path, required=True)
    store.add_argument("--ledger", type=Path, required=True)
    store.add_argument("--screenshot", type=Path)
    store.add_argument("--cache-dir", type=Path, required=True)
    store.add_argument("--model", choices=tuple(provider.MODEL_PRICES), required=True)
    args = parser.parse_args(argv)
    try:
        project = args.project.resolve(strict=True)
        if not project.is_dir():
            raise ValueError("the Unity project directory does not exist")
        if args.command == "snapshot":
            output = _outside(args.output, project, "snapshot output")
            if output.exists():
                raise ValueError("the snapshot output already exists")
            value = cache.capture_authored(project)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(cache._snapshot_data(value), stream, sort_keys=True, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
            print(f"authored snapshot saved to {output}; sha256 {value.sha256}")
            return 0

        directory = _outside(args.cache_dir, project, "cache directory")
        before = cache._snapshot_from_data(json.loads(args.before.read_text(encoding="utf-8")))
        after = cache._snapshot_from_data(json.loads(args.after.read_text(encoding="utf-8")))
        for path in (args.manifest, args.ledger):
            timestamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if timestamp < before.finished_utc or timestamp > after.started_utc:
                raise ValueError("the authored snapshots do not bracket the manifest and ledger files")
        if cache.capture_authored(project).files != after.files:
            raise ValueError("the current authored project differs from the after snapshot")
        inventory = InspectResult.model_validate_json(args.inventory.read_text(encoding="utf-8"))
        discovery = InspectResult.model_validate_json(args.discovery.read_text(encoding="utf-8"))
        calibration = CalibrationRecord.model_validate_json(args.calibration.read_text(encoding="utf-8"))
        value = PreflightManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"), strict=True)
        ledger = [json.loads(line) for line in args.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows, reservations = cache.split_ledger(ledger)
        entry = cache.create_entry(before, after, inventory, discovery, calibration, _read_image(args.screenshot, value),
                                   value, rows, reservations, model=args.model)
        output = cache.write_entry(directory, entry)
        print(f"validated selection cache saved to {output}")
        print("stage 7 remains a separate live verification")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"preflight cache failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
