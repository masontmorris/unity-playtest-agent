"""Run output: the run record that opens a trace, its `end` entry, and the files a run leaves.

Until the trace writer (#30) lands, a run keeps its trace in a `MemorySink` and dumps it once, at the
end, as `trace.jsonl`: the `run` entry first, the loop's entries, the `end` entry last. A harness that
injects its own sink owns `run` and `end` instead and never calls these functions. Full traces stay
local under `runs/`; nothing here is meant to be committed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import UNKNOWN, Budget, ModelRef, RunRecord
from playtest.contracts.trace import TraceAuthor, TraceKind
from playtest.core.trace_sink import MemorySink

REDACTED = "[redacted]"


def core_version() -> str:
    try:
        return metadata.version("unity-playtest-agent")
    except metadata.PackageNotFoundError:
        return "0.0.1"


def build_run_record(
    *,
    run_id: str,
    project: Path,
    objective: str,
    s1_provider: str,
    s1_model: str,
    prompt_sha256: str,
    wall_limit_s: float,
    max_usd: float | None,
    manifest_fingerprint: str,
    game_commit: str | None,
    unity_version: str | None,
    bridge_version: str | None,
    input_mode: str | None,
) -> RunRecord:
    """The run record from what this process measured; anything it did not measure stays `unknown`."""
    return RunRecord(
        run_id=run_id,
        game_project=project.name,
        game_commit=game_commit or UNKNOWN,
        unity_version=unity_version or UNKNOWN,
        manifest_fingerprint=manifest_fingerprint,
        clock_mode=ClockMode.realtime,
        input_mode=input_mode or UNKNOWN,
        models=[ModelRef(role="s1", provider=s1_provider, model=s1_model, prompt_hash=prompt_sha256)],
        objective=objective,
        budget=Budget(max_wall_s=wall_limit_s, max_usd=max_usd, max_frontier_calls=0),
        bridge_version=bridge_version or UNKNOWN,
        core_version=core_version(),
    )


def open_trace(record: RunRecord) -> MemorySink:
    sink = MemorySink()
    sink.append(TraceKind.run, record.model_dump(mode="json"), author=TraceAuthor.harness)
    return sink


def secret_values(environ: Mapping[str, str] | None = None) -> list[str]:
    """Values of every `*_API_KEY` variable in the environment, so a trace can be checked for them."""
    env = os.environ if environ is None else environ
    return sorted({value for name, value in env.items() if name.endswith("_API_KEY") and len(value) >= 8})


def close_trace(sink: MemorySink, *, status: str, error: str | None, turns: int, secrets: Sequence[str]) -> int:
    """Append the `end` entry and return how many secret values the dump will replace."""
    redactions = sum(_dumps(entry).count(secret) for entry in sink.entries for secret in secrets)
    sink.append(
        TraceKind.end,
        {"status": status, "error": error, "turns": turns, "redactions": redactions},
        author=TraceAuthor.harness,
    )
    return redactions


def write_trace(path: Path, entries: Iterable[Mapping[str, Any]], secrets: Sequence[str]) -> None:
    """Write one JSON object per line, replacing any secret value, to a file that must not exist."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for entry in entries:
            line = _dumps(entry)
            for secret in secrets:
                line = line.replace(secret, REDACTED)
            stream.write(line + "\n")


def _dumps(entry: Mapping[str, Any]) -> str:
    try:
        return json.dumps(entry, ensure_ascii=False, allow_nan=False)
    except ValueError:  # a game that reports NaN must not cost the run its trace
        return json.dumps(entry, ensure_ascii=False)
