"""Keep the exact state each S1 request carried, in the run folder, before the call is sent.

The trace's `context` entry is the payload the core built. That is what S1 reads in the `full` context
mode, but the compact menu replaces it with a shorter state built inside the adapter, and that state was
nowhere in the run folder. A reviewer quoting "the labels S1 saw" from the trace was quoting the wrong
thing.

One file per request, `requests/s1/<turn>-<question>.json`, holds:

- `turn`, `question`: which request of the run this is, and the question it asked (the file name repeats them);
- `question_ids`: every question id the request carried, in the request's own order;
- `sha256`: the digest the `s1_call` trace body records as `request_sha256`;
- `image_sha256`: the digest of the image attached to the call, or null;
- `hashing`: how to reproduce `sha256` from this file;
- and the request body's own keys — `model`, `state`, `questions`, or `messages`/`response_format` for a
  layout that renders the payload as messages — with any image redacted in place.

The record's own keys are exactly `RECORD_KEYS`; everything else in the file is the posted body. So the
reproducible case is `sha256 == digest(canonical(<file without RECORD_KEYS>))`, which for a compact-menu
request is its `model`, `state` and `questions`: exactly the state S1 was sent. A request posted as
rendered text, or carrying an image, is recorded as such in `hashing`: its digest covers bytes this file
does not hold.

Nothing here decides anything. A file is evidence storage, not a cache: the call is reserved and sent
whatever happens to the evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from playtest.core.context import canonical

# The record's own keys. Every other key in the file is the request body as it was posted.
RECORD_KEYS = ("turn", "question", "question_ids", "sha256", "image_sha256", "hashing")

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
REPRODUCED = ("sha256 is the digest of the canonical body posted for this call, and this file is that body: "
              "reproduce it as sha256(canonical(<this file without \"turn\", \"question\", \"question_ids\", "
              "\"sha256\", \"image_sha256\", \"hashing\">)).")
WITH_IMAGE = ("sha256 is the digest of the canonical body posted for this call, which carried an image: the "
              "image's own sha256 is recorded here and its bytes are not, so the digest needs the image to "
              "be reproduced. Every other value is this file's own.")
RENDERED = ("sha256 is the digest of the request text this adapter rendered and posted; this file holds the "
            "body that text was rendered from, so the digest is reproduced only by rendering it the same way.")


def request_file(relative_directory: Path, turn: int, question: str) -> Path:
    """Where one call's state is kept: `requests/s1/<turn>-<question>.json`, the question made file-safe."""
    safe = _UNSAFE.sub("-", question).strip("-") or "question"
    return relative_directory / f"{turn}-{safe}.json"


def request_record(*, turn: int, question: str, body: Mapping[str, Any], sha256: str,
                   image: bytes | None = None, rendered: bool = False) -> dict[str, Any]:
    """The file's contents for one call: the posted body, the digest, and how to re-derive it.

    The body is copied with the image redacted in place, so a file never keeps an encoded game view.
    """
    posted = _redact(dict(body), image)
    questions = posted.get("questions")
    question_ids = [str(name) for name in questions] if isinstance(questions, Mapping) else [question]
    if question not in question_ids:
        question_ids = [question, *question_ids]
    record: dict[str, Any] = {
        "turn": turn,
        "question": question,
        "question_ids": question_ids,
        "sha256": sha256,
        "image_sha256": hashlib.sha256(image).hexdigest() if image is not None else None,
        "hashing": RENDERED if rendered else (WITH_IMAGE if image is not None else REPRODUCED),
        **posted,
    }
    return record


def recorded_digest(record: Mapping[str, Any]) -> str:
    """The digest this file reproduces: its own body keys, canonicalized exactly as the adapter hashed them."""
    body = {key: value for key, value in record.items() if key not in RECORD_KEYS}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def write_request(directory: Path, record: Mapping[str, Any], turn: int, question: str) -> str:
    """Write one record atomically and return its path relative to the run directory.

    An existing file with the same name is replaced only by identical bytes: a run may not silently rewrite
    the evidence for a call it already made.
    """
    target = request_file(directory, turn, question)
    data = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError(f"the stored S1 request {target.name} does not match this call")
        return _relative(target, directory)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=f".{target.stem}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return _relative(target, directory)


def _relative(target: Path, directory: Path) -> str:
    """The run-directory-relative path, with forward slashes, as the trace and reports name files."""
    try:
        return target.relative_to(directory.parents[1]).as_posix() if len(directory.parents) > 1 else target.name
    except ValueError:  # a directory outside any run folder: the path it was written to is the honest name
        return target.as_posix()


def _redact(body: dict[str, Any], image: bytes | None) -> dict[str, Any]:
    """The body with the attached image taken out: an encoded view is never kept beside the state it rode with."""
    if image is not None:
        body.pop("images", None)
        encoded = base64.b64encode(image).decode("ascii")
        body = _replace(body, ("data:image/jpeg;base64," + encoded, encoded), "[image omitted]")
    return body


def _replace(value: Any, needles: tuple[str, ...], marker: str) -> Any:
    if isinstance(value, str):
        for needle in needles:
            value = value.replace(needle, marker)
        return value
    if isinstance(value, list):
        return [_replace(item, needles, marker) for item in value]
    if isinstance(value, dict):
        return {_replace(key, needles, marker): _replace(item, needles, marker) for key, item in value.items()}
    return value
