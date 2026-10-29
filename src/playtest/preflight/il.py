"""Complete a bridge discovery snapshot with the IL write scan, which runs outside the Editor.

The bridge lists the compiled assemblies that hold authored scripts (`il_inputs`). A standalone
.NET tool scans them; this module runs the tool and appends its sections to the snapshot, so the
saved discovery carries the same IL sections as when the bridge scanned for itself.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from playtest.contracts.bridge import InspectionCap, InspectResult


TOOL_PROJECT = Path(__file__).resolve().parents[3] / "tools" / "ilscan" / "src" / "Playtest.IlScan.csproj"
TIMEOUT_S = 900
_INPUT_KEYS = frozenset({"assemblies", "references", "complete"})
_DOCUMENT_KEYS = ("data", "caps", "errors")
_DATA_KEYS = ("il_write_sets", "il_unlisted_writes", "il_issues", "il_scope", "il_complete")
_LIST_KEYS = ("il_write_sets", "il_unlisted_writes", "il_issues")
_CAP_KEYS = ("il_types", "il_fields", "il_methods", "il_writes", "il_issues")


def _problems(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                     for item in error.errors(include_url=False)[:3])


def _check_discovery(value: Any, label: str) -> None:
    try:
        result = InspectResult.model_validate(value)
    except ValidationError as exc:
        raise ValueError(f"{label} is not a valid inspection result: {_problems(exc)}") from None
    if result.stage != "discovery":
        raise ValueError(f"{label} is not a discovery inspection")


def _inputs(value: Any) -> dict[str, Any]:
    """Return the bridge's `il_inputs` once it is known to have exactly the agreed shape."""
    if not isinstance(value, dict) or set(value) != _INPUT_KEYS:
        raise ValueError("the bridge snapshot's il_inputs must be an object with exactly "
                         "assemblies, references and complete")
    assemblies, references = value["assemblies"], value["references"]
    if not isinstance(assemblies, list):
        raise ValueError("il_inputs assemblies must be a list")
    names: set[str] = set()
    for row in assemblies:
        if not isinstance(row, dict) or set(row) != {"name", "path"}:
            raise ValueError("each il_inputs assembly must be an object with exactly name and path")
        if any(not isinstance(row[key], str) or not row[key] for key in ("name", "path")):
            raise ValueError("each il_inputs assembly needs a non-empty name and path")
        if row["name"] in names:
            raise ValueError(f"il_inputs lists the assembly {row['name']} more than once")
        names.add(row["name"])
    if not isinstance(references, list) or any(not isinstance(item, str) or not item for item in references):
        raise ValueError("il_inputs references must be a list of non-empty strings")
    if not isinstance(value["complete"], bool):
        raise ValueError("il_inputs complete must be a boolean")
    return value


def _check_document(value: Any) -> None:
    """Check the tool's document against the agreed shape; row contents are left to the consumer."""
    if not isinstance(value, dict) or tuple(value) != _DOCUMENT_KEYS:
        raise ValueError("the IL scan document must have exactly data, caps and errors, in that order")
    data, caps, errors = value["data"], value["caps"], value["errors"]
    if not isinstance(data, dict) or tuple(data) != _DATA_KEYS:
        raise ValueError(f"the IL scan data must have exactly {', '.join(_DATA_KEYS)}, in that order")
    if not isinstance(caps, dict) or tuple(caps) != _CAP_KEYS:
        raise ValueError(f"the IL scan caps must have exactly {', '.join(_CAP_KEYS)}, in that order")
    for name, cap in caps.items():
        try:
            # Strict, so a number or flag in disguise (true, 0, "5") is refused, not coerced.
            InspectionCap.model_validate(cap, strict=True)
        except ValidationError as exc:
            raise ValueError(f"the IL scan {name} cap is invalid: {_problems(exc)}") from None
    for name in _LIST_KEYS:
        if not isinstance(data[name], list):
            raise ValueError(f"the IL scan {name} must be a list")
    if not isinstance(data["il_scope"], str) or not data["il_scope"]:
        raise ValueError("the IL scan il_scope must be a non-empty string")
    if not isinstance(data["il_complete"], bool):
        raise ValueError("the IL scan il_complete must be a boolean")
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
        raise ValueError("the IL scan errors must be a list of strings")


def merge(snapshot: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """Append the tool's sections to a bridge discovery snapshot, in place of its `il_inputs`.

    The tool's data and caps keys follow the snapshot's, in the tool's order, and its errors
    follow the snapshot's errors. A key that is already present is an error. The snapshot is
    parsed JSON and is neither changed nor round-tripped through the contract model, so every
    other value stays exactly as parsed.
    """
    _check_discovery(snapshot, "the bridge snapshot")
    _check_document(document)
    data, caps = dict(snapshot["data"]), dict(snapshot["caps"])
    try:
        del data["il_inputs"]
    except KeyError:
        raise ValueError("the bridge snapshot has no il_inputs to replace") from None
    for label, existing, added in (("data", data, document["data"]), ("caps", caps, document["caps"])):
        clash = [key for key in added if key in existing]
        if clash:
            raise ValueError(f"the bridge snapshot already has IL {label}: {', '.join(clash)}")
    merged = dict(snapshot)
    merged["data"] = {**data, **document["data"]}
    merged["caps"] = {**caps, **document["caps"]}
    merged["errors"] = [*snapshot["errors"], *document["errors"]]
    _check_discovery(merged, "the merged discovery")
    return merged


def _detail(result: Any) -> str:
    # A failed build reports on stdout, so fall back to it when stderr says nothing.
    for name in ("stderr", "stdout"):
        text = (getattr(result, name, None) or "").strip()
        if text:
            return text
    return "no output"


def scan(inputs: dict[str, Any], *, tool_project: Path = TOOL_PROJECT,
         run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Run the IL scan tool on `inputs` and return the document it wrote; keep nothing on failure."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        source, target = Path(directory) / "inputs.json", Path(directory) / "document.json"
        with source.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(inputs, stream, ensure_ascii=False)
        command = ["dotnet", "run", "--project", str(tool_project), "--configuration", "Release",
                   "--verbosity", "quiet", "--", "--input", str(source), "--output", str(target)]
        try:
            result = run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         check=False, timeout=TIMEOUT_S)
        except FileNotFoundError:
            raise ValueError("the IL scan tool needs the .NET SDK (dotnet) on PATH") from None
        except subprocess.TimeoutExpired:
            raise ValueError(f"the IL scan tool did not finish within {TIMEOUT_S} seconds") from None
        if result.returncode != 0:
            raise ValueError(f"the IL scan tool failed (exit {result.returncode}): {_detail(result)}")
        try:
            # utf-8-sig also reads a document that starts with a byte order mark.
            document = json.loads(target.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise ValueError("the IL scan tool exited cleanly but wrote no document") from None
        except ValueError as exc:
            raise ValueError(f"the IL scan document is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(document, dict):
        raise ValueError("the IL scan document is not a JSON object")
    return document


def complete(snapshot: dict[str, Any], **scan_kwargs: Any) -> dict[str, Any]:
    """Scan the assemblies a bridge discovery snapshot lists and return it with the IL sections.

    Nothing is scanned, and so nothing is returned, unless the snapshot and its `il_inputs`
    are well formed; the scan's own document is checked again before it is merged.
    """
    _check_discovery(snapshot, "the bridge snapshot")
    inputs = _inputs(snapshot["data"].get("il_inputs"))
    return merge(snapshot, scan(inputs, **scan_kwargs))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True, help="discovery snapshot saved from the bridge")
    parser.add_argument("--output", type=Path, required=True, help="new JSON file for the completed discovery")
    parser.add_argument("--tool-project", type=Path, default=TOOL_PROJECT, help="IL scan tool project file")
    args = parser.parse_args(argv)

    output = args.output.resolve()
    if output.exists():
        parser.error("--output already exists; choose a new file")

    try:
        snapshot = json.loads(args.discovery.read_text(encoding="utf-8"))
        text = json.dumps(complete(snapshot, tool_project=args.tool_project), indent=2, ensure_ascii=False) + "\n"
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except (OSError, ValueError) as exc:
        print(f"discovery completion failed: {exc}", file=sys.stderr)
        return 1
    print(f"completed discovery saved to {output}; review its caps and errors for coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
