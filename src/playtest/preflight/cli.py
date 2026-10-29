"""Save one Editor inspection response outside the inspected Unity project.

A discovery response is completed with the IL write scan, which runs outside the Editor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from playtest.contracts.bridge import InspectResult
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError
from playtest.preflight import il


async def _inspect(project: Path, stage: str, timeout_s: float) -> InspectResult:
    client = await BridgeClient.connect_from_project(project, timeout_s=timeout_s)
    try:
        return await client.inspect(stage)
    finally:
        await client.aclose()


def _render(stage: str, response: InspectResult) -> str:
    if stage != "discovery":
        return response.model_dump_json(indent=2) + "\n"
    # The scan can run for minutes, so it starts only after the bridge connection is closed.
    snapshot = json.loads(response.model_dump_json())
    return json.dumps(il.complete(snapshot), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="Unity project with a running bridge")
    parser.add_argument("--stage", choices=("inventory", "discovery", "transforms", "fields"), required=True)
    parser.add_argument("--output", type=Path, required=True, help="new JSON file outside the Unity project")
    parser.add_argument("--timeout", type=float, default=120.0, help="maximum seconds to wait for the bridge")
    args = parser.parse_args(argv)

    project = args.project.resolve()
    output = args.output.resolve()
    if output.is_relative_to(project):
        parser.error("--output must be outside the Unity project")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if output.exists():
        parser.error("--output already exists; choose a new file")

    try:
        text = _render(args.stage, asyncio.run(_inspect(project, args.stage, args.timeout)))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except (BridgeError, BridgeLockError, OSError, ValueError) as exc:
        print(f"inspection failed: {exc}", file=sys.stderr)
        return 1
    print(f"inspection saved to {output}; review its caps and errors for coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
