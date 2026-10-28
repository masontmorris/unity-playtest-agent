"""Append-only trace storage: each validated line survives a later crash (build bible 5.7)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from playtest.contracts.investigation import RunRecord
from playtest.contracts.trace import TraceAuthor, TraceEntry, TraceKind


class TraceWriter:
    def __init__(self, path: str | Path, run: RunRecord, *, secrets: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self.run_id = run.run_id
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))
        self.redactions = 0
        self.turns = 0
        self._closed = False
        self._seq = 0
        self._file = self.path.open("x", encoding="utf-8", newline="\n")
        try:
            self.append(TraceKind.run, run.model_dump(mode="json"), author=TraceAuthor.harness)
        except Exception:
            self._file.close()
            self.path.unlink(missing_ok=True)
            raise

    @property
    def next_seq(self) -> int:
        return self._seq

    def append(self, kind: TraceKind | str, body: Mapping[str, Any], *, author: TraceAuthor | str,
               turn: int | None = None, refs: Sequence[int] = ()) -> int:
        if self._closed:
            raise ValueError("trace is closed")
        seq = self.next_seq
        # Mirror MemorySink's enum, turn and ref checks.
        kind = TraceKind(kind)
        author = TraceAuthor(author)
        if turn is not None and (isinstance(turn, bool) or not isinstance(turn, int) or turn < 0):
            raise ValueError(f"turn must be a non-negative integer or None, not {turn!r}")
        cited = list(refs)
        if any(isinstance(r, bool) or not isinstance(r, int) or not 0 <= r < seq for r in cited):
            raise ValueError(f"refs must cite earlier entries (0..{seq - 1}), not {cited!r}")
        row = {"trace_seq": seq, "run_id": self.run_id, "kind": kind.value, "author": author.value,
               "turn": turn, "refs": cited, "wall_utc": datetime.now(timezone.utc).isoformat(),
               "body": dict(body)}
        # Validate exactly what is written: the serialized, redacted line, before touching the file.
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        replacements = 0
        for secret in self._secrets:
            escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
            count = line.count(escaped)
            if count:
                line = line.replace(escaped, "[redacted]")
                replacements += count
        parsed = json.loads(line)
        TraceEntry.model_validate(parsed)
        self._file.write(line + "\n")
        self._file.flush()
        self._seq = seq + 1
        self.redactions += replacements
        if kind is TraceKind.turn:
            self.turns += 1
        return seq

    def close(self, status: str = "complete", error: str | None = None) -> None:
        if self._closed:
            return
        self.append(TraceKind.end, {"status": status, "error": error, "turns": self.turns,
                                    "redactions": self.redactions}, author=TraceAuthor.harness)
        self._closed = True
        self._file.close()

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        self.close("inconclusive" if exc is not None else "complete", str(exc) if exc is not None else None)


def read_trace(path: str | Path, *, strict: bool = True) -> list[TraceEntry] | tuple[list[TraceEntry], str | None]:
    raw = Path(path).read_bytes()
    lines = raw.splitlines(keepends=True)
    entries: list[TraceEntry] = []
    for index, line in enumerate(lines, 1):
        final = index == len(lines)
        try:
            text = line.decode("utf-8")
            if final and not line.endswith((b"\n", b"\r")):
                raise ValueError("truncated final line (missing newline)")
            entries.append(TraceEntry.model_validate(json.loads(text)))
        except Exception as exc:
            if final and not strict:
                return entries, f"truncated or unparseable final line {index}: {exc}"
            if final:
                raise ValueError(f"truncated or unparseable final line {index}: {exc}") from exc
            raise ValueError(f"invalid trace line {index}: {exc}") from exc
    return entries if strict else (entries, None)
