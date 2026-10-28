from __future__ import annotations

import subprocess

from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, UNKNOWN
from playtest.core.run_record import build_run_record


def build(path):
    return build_run_record(game_project=path, status=None, scene=None, manifest_fingerprint=None,
                           clock_mode=ClockMode.realtime, input_mode=None, models=[], objective="test",
                           budget=Budget(), reset_method=None)


def test_unknowns_and_no_absolute_path(tmp_path):
    rec = build(tmp_path)
    assert rec.game_project == tmp_path.name
    assert rec.game_commit == UNKNOWN and rec.unity_version == UNKNOWN
    assert rec.scene == rec.manifest_fingerprint == rec.input_mode == rec.reset_method == UNKNOWN
    assert str(tmp_path) not in rec.model_dump_json()


def test_commit_is_measured_from_git_repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "initial"], check=True, capture_output=True)
    commit = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    assert build(tmp_path).game_commit == commit


def test_non_git_directory_is_unknown(tmp_path):
    assert build(tmp_path).game_commit == UNKNOWN


def test_modified_worktree_paths_are_measured(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "initial"], check=True, capture_output=True)
    (tmp_path / "changed.txt").write_text("changed")
    rec = build(tmp_path)
    assert rec.game_worktree == "modified"
    assert rec.game_modified_paths == ["changed.txt"]


def test_renames_and_untracked_directories_are_listed_once(tmp_path):
    git = ["git", "-C", str(tmp_path), "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "old.txt").write_text("kept")
    subprocess.run([*git, "add", "old.txt"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run([*git, "mv", "old.txt", "new.txt"], check=True, capture_output=True)
    (tmp_path / "scratch").mkdir()
    for name in ("a.txt", "b.txt"):
        (tmp_path / "scratch" / name).write_text(name)
    rec = build(tmp_path)
    assert rec.game_worktree == "modified"
    assert sorted(rec.game_modified_paths) == ["new.txt", "scratch/"]
