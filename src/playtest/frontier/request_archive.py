"""Preserve exact frontier JSON bodies locally, before dispatch, without transport credentials."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playtest.core.context import canonical


@dataclass(frozen=True)
class RequestArtifact:
    relative_path: str
    sha256: str


def preserve_request(directory: Path, body: dict[str, Any]) -> RequestArtifact:
    """Store the bytes `send_meta` serializes, including any clip data URI, not headers or keys.

    Content-addressed names link directly to the call ledger's request hash. An existing matching
    artifact is reused; a damaged artifact is refused, never overwritten. This is evidence storage,
    not a response cache: callers still reserve and send each authorized request normally.
    """
    data = canonical(body).encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    relative = Path("requests") / "frontier" / f"{sha}.json"
    target = directory / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError("the stored frontier request does not match its content hash")
        return RequestArtifact(relative.as_posix(), sha)

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=f".{sha}.", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return RequestArtifact(relative.as_posix(), sha)
