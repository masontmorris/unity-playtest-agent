"""The catalog a manifest session's host serves, checked against the manifest before any paid call.

`playtest.preflight.catalog.export` is the one authority for which declarations can run and what each one
is. `scripts/unity/run-session.ps1` writes that export with `playtest-export-catalog`, pins the file's
SHA-256, and hands both to the PlayMode host, which registers exactly that file and nothing else. This
module is the client's half of failing closed: before the bridge is contacted it re-reads the same file,
checks the pinned digest, parses it strictly, and requires it to equal what the manifest exports now. A
missing, unreadable, altered, invalid or different catalog is an error; there is no fallback to another
catalog, and nothing is repaired or filled in.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from playtest.contracts.action_catalog import ActionCatalog, declaration_problems
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight.catalog import export

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

PATH_VARIABLE = "UPA_SESSION_CATALOG"
SHA256_VARIABLE = "UPA_SESSION_CATALOG_SHA256"


@dataclass(frozen=True)
class HostCatalog:
    """The catalog file the host was given: its digest as read, and the declarations it holds."""

    sha256: str
    catalog: ActionCatalog


def verify_host_catalog(manifest: PreflightManifest, path: Path | None, sha256: str | None) -> HostCatalog:
    """Return the host's catalog if, and only if, it is exactly what `manifest` exports.

    Raises `ValueError` for an absent path or digest, an unreadable file, a digest that differs from the
    pinned one (the file changed after it was exported), a file that is not a strict `ActionCatalog`, and any
    difference from `playtest.preflight.catalog.export(manifest)`: a missing declaration, an extra one, or a
    declaration whose inputs, values, holds, waits or bound differ.
    """
    if path is None:
        raise ValueError(f"no session catalog: the host serves only the catalog the manifest exports "
                         f"(set {PATH_VARIABLE} or pass --catalog)")
    pinned = (sha256 or "").strip().lower()
    if not _SHA256.fullmatch(pinned):
        raise ValueError(f"no pinned session catalog digest: {SHA256_VARIABLE} (or --catalog-sha256) must be "
                         f"the file's SHA-256 as 64 hex digits")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read the session catalog: {exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != pinned:
        raise ValueError(f"the session catalog's SHA-256 is {actual}, not the pinned {pinned}: "
                         f"it is not the file that was exported")
    try:
        served = ActionCatalog.model_validate_json(raw, strict=True)
    except ValueError as exc:  # pydantic's ValidationError is one
        raise ValueError(f"the session catalog is not a valid action catalog: {exc}") from exc
    problems = differences(export(manifest), served)
    if problems:
        raise ValueError("the session catalog is not the manifest's export: " + "; ".join(problems))
    return HostCatalog(sha256=actual, catalog=served)


def differences(expected: ActionCatalog, served: ActionCatalog) -> list[str]:
    """Every way `served` differs from `expected`, by declaration name; empty when they are the same."""
    wanted, got = expected.actions, served.actions
    problems = [f"{name!r} is supported by the manifest but the host does not serve it" for name in wanted if name not in got]
    problems += [f"{name!r} is served by the host but the manifest does not support it" for name in got if name not in wanted]
    for name, declared in wanted.items():
        if name in got:
            detail = declaration_problems(declared, got[name])
            if detail:
                problems.append(f"{name!r} differs from the manifest's declaration: " + "; ".join(detail))
    return problems
