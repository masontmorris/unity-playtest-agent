"""Build provenance from measured run inputs and explicit unknowns (build bible 5.9)."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import playtest
from playtest.contracts.bridge import Status
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, ModelRef, RunRecord, UNKNOWN


def build_run_record(*, game_project: str | Path, status: Status | None, scene: str | None,
                     manifest_fingerprint: str | None, clock_mode: ClockMode,
                     input_mode: str | None, models: list[ModelRef], objective: str,
                     budget: Budget, reset_method: str | None) -> RunRecord:
    project = Path(game_project)
    worktree = "unknown"
    modified_paths: list[str] = []
    try:
        commit = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        if not commit:
            commit = UNKNOWN
    except (OSError, subprocess.SubprocessError):
        commit = UNKNOWN
    try:
        output = subprocess.run(["git", "-C", str(project), "status", "--porcelain", "-z", "--untracked-files=normal"],
                                check=True, capture_output=True, text=True, timeout=10).stdout
        fields = iter(output.split("\0"))
        for item in fields:
            if not item:
                continue
            modified_paths.append(item[3:])
            if "R" in item[:2] or "C" in item[:2]:
                next(fields, None)  # a rename or copy is followed by its source path
        worktree = "modified" if modified_paths else "clean"
    except (OSError, subprocess.SubprocessError):
        worktree = "unknown"
        modified_paths = []
    return RunRecord(
        run_id=str(uuid.uuid4()), game_project=project.name or UNKNOWN, game_commit=commit,
        game_worktree=worktree, game_modified_paths=modified_paths,
        unity_version=status.unity_version if status is not None else UNKNOWN,
        scene=scene if scene is not None else UNKNOWN,
        manifest_fingerprint=manifest_fingerprint if manifest_fingerprint is not None else UNKNOWN,
        clock_mode=clock_mode, input_mode=input_mode if input_mode is not None else UNKNOWN,
        models=models, objective=objective, budget=budget,
        reset_method=reset_method if reset_method is not None else UNKNOWN,
        bridge_version=status.bridge_version if status is not None else UNKNOWN,
        core_version=playtest.__version__,
    )
