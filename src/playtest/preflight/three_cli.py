"""Prepare a three.js game from declarations and measurements, without a model call."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from playtest.contracts.manifest import PreflightManifest
from playtest.preflight import entity, manifest as manifest_checks
from playtest.preflight.three import load_declarations
from playtest.preflight.three_session import _identity, _sha, connect, game_claim, prepare, serve
from playtest.runner.catalog import verify_host_catalog
from playtest.runner.choices import build_choices
from playtest.runner.readers import published_install


def _load_complete(path: Path, *, query: str | None, settle: float | None) -> tuple[dict, PreflightManifest]:
    path = path.resolve()
    marker = json.loads(path.read_text(encoding="utf-8"))
    if marker.get("schema_version") != 1 or set(marker.get("files", {})) != {"study.json", "manifest.json", "entity.json", "calibration.json", "catalog.json"}:
        raise ValueError("the prepared bundle is incomplete")
    for name, expected in marker["files"].items():
        if _sha(path.parent / name) != expected:
            raise ValueError(f"prepared file {name} differs from its completion marker")
    decl = load_declarations(Path(marker["game"]), Path(marker["catalog_path"]))
    if decl.source["dirty"] or marker["source"]["dirty"] or decl.source != marker["source"]:
        raise ValueError("prepared reuse requires the same clean game checkout and declarations")
    if query is not None and query != marker["launch"]["query"]:
        raise ValueError("prepared reuse requires the same launch query")
    if settle is not None and settle != marker["launch"]["settle_s"]:
        raise ValueError("prepared reuse requires the same settle setting")
    manifest = PreflightManifest.model_validate_json((path.parent / "manifest.json").read_bytes(), strict=True)
    problems = manifest_checks.validate(manifest)
    if problems:
        raise ValueError("prepared manifest is invalid: " + "; ".join(problems))
    verify_host_catalog(manifest, path.parent / "catalog.json", marker["files"]["catalog.json"])
    if not any(c.source.value == "manifest" for c in build_choices(manifest).choices):
        raise ValueError("manifest offers no executable action")
    return marker, manifest


async def _rebind(game: Path, expected: dict, readers: list, settle_s: float) -> None:
    # A fresh page compiles three.js pipelines in single tasks of up to about 11 s, beyond the ordinary bridge timeout.
    client = await connect(game, timeout_s=20.0)
    try:
        await asyncio.sleep(settle_s)
        data = entity._complete(await client.inspect("transforms"), "transforms")
        identities = _identity(data)
        actual = identities.get(expected["id"])
        if actual != (expected.get("parent_id"), tuple(expected["name_path"])):
            raise ValueError("the fresh page's entity id, name path or parent differs from the measured entity")
        published_install(await client.observe_once(), readers)
    finally:
        await client.aclose()


def run_prepared(complete: Path, marker: dict, manifest: PreflightManifest, passthrough: list[str]) -> int:
    allowed = {"--approval", "--max-calls", "--s1-arm", "--objective", "--tokens-per-minute", "--wall-limit-s"}
    if any(arg.startswith("--") and arg.split("=", 1)[0] not in allowed for arg in passthrough):
        raise ValueError("runner options may only set approval, call cap, arm, objective, pacing or wall limit")
    directory = complete.resolve().parent
    game = Path(marker["game"])
    catalog = directory / "catalog.json"
    with game_claim(game):
        with serve(game, catalog, marker["launch"]["query"], directory / "rebind-page.log"):
            asyncio.run(_rebind(game, manifest.entity, manifest.readers, marker["launch"]["settle_s"]))
            from playtest.runner import cli as runner_cli
            argv = ["--project", str(game), "--entity", str(directory / "entity.json"),
                    "--calibration", str(directory / "calibration.json"), "--manifest", str(directory / "manifest.json"),
                    "--catalog", str(catalog), "--catalog-sha256", marker["files"]["catalog.json"],
                    "--manifest-origin", f"three preflight {complete.resolve()}", "--output", str(directory / "hunt" / "summary.json"),
                    *passthrough]
            inherited = {key: value for key, value in os.environ.items() if key.startswith("UPA_RUN_") or key.startswith("UPA_SESSION_")}
            try:
                for key in inherited:
                    del os.environ[key]
                return runner_cli.main(argv)
            finally:
                os.environ.update(inherited)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    split = raw.index("--") if "--" in raw else len(raw)
    parser = argparse.ArgumentParser(prog="playtest-preflight-three", description=__doc__)
    parser.add_argument("--game", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--entity-action")
    parser.add_argument("--entity-id", help="when several roots respond alike, the measured root to use")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--query", default=None)
    parser.add_argument("--settle", type=float, default=None)
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--run", action="store_true", help="run playtest-run after preparation")
    args = parser.parse_args(raw[:split])
    passthrough = raw[split + 1:]
    if passthrough and not args.run:
        parser.error("runner arguments require --run")
    try:
        if args.prepared:
            if args.game or args.catalog or args.entity_action or args.entity_id or args.output:
                parser.error("--prepared replaces game, catalog, entity action, entity id and output")
            complete = args.prepared
            marker, manifest = _load_complete(complete, query=args.query, settle=args.settle)
        else:
            if args.game is None or args.entity_action is None or args.output is None:
                parser.error("--game, --entity-action and --output are required")
            decl = load_declarations(args.game, args.catalog)
            complete = prepare(decl, args.entity_action, args.output, query=args.query or "", settle_s=60 if args.settle is None else args.settle,
                               entity_id=args.entity_id)
            marker = json.loads(complete.read_text(encoding="utf-8"))
            manifest = PreflightManifest.model_validate_json((complete.parent / "manifest.json").read_bytes(), strict=True)
        print(json.dumps({"complete": str(complete.resolve()), "readers": len(manifest.readers),
                          "actions": len(manifest.actions), "supported": sum(a.execution_supported for a in manifest.actions)}))
        if args.run:
            return run_prepared(complete, marker, manifest, passthrough)
        return 0
    except (OSError, ValueError) as exc:
        print(f"three preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
