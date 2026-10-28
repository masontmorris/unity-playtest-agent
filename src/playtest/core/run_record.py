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
    try:
        commit = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        if not commit:
            commit = UNKNOWN
    except (OSError, subprocess.SubprocessError):
        commit = UNKNOWN
    return RunRecord(
        run_id=str(uuid.uuid4()), game_project=project.name or UNKNOWN, game_commit=commit,
        unity_version=status.unity_version if status is not None else UNKNOWN,
        scene=scene if scene is not None else UNKNOWN,
        manifest_fingerprint=manifest_fingerprint if manifest_fingerprint is not None else UNKNOWN,
        clock_mode=clock_mode, input_mode=input_mode if input_mode is not None else UNKNOWN,
        models=models, objective=objective, budget=budget,
        reset_method=reset_method if reset_method is not None else UNKNOWN,
        bridge_version=status.bridge_version if status is not None else UNKNOWN,
        core_version=playtest.__version__,
    )
