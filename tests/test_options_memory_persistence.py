"""Outcome memory across runs, and the felt map it keeps: files, merges and measured map lines.

Every probe here is synthetic and every assertion is about a quantity, a cell or a format, never about a game.
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource
from playtest.runner.menu import compact_menu, progress_line
from playtest.runner.options.memory import OutcomeMemory, memory_file
from playtest.runner.options.model import OptionOutcome

CELL = 1.0
SEEN = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def observation(scene: str = "scene-a", position=(0.0, 0.0, 0.0), wall_utc: datetime = SEEN,
                **readers) -> Observation:
    """One observation carrying the controlled pose and any declared reader values."""
    public = {"controlled": {"object_id": "o", "path": "p", "position": list(position),
                             "velocity": None, "source": "rigidbody"}}
    public.update(readers)
    return Observation(session="s", seq=0, game_s=1.0, wall_utc=wall_utc, clock_mode=ClockMode.realtime,
                       scene=scene, playing=True, paused=False, time_scale=1.0, frame=0, public=public)


def outcome(option_id: str = "hop+x", terminated_by: str = "blocked",
            start: list[float] | None = None, displacement: list[float] | None = None,
            direction: list[float] | None = (1.0, 0.0, 0.0),
            scene: str | None = "scene-a") -> OptionOutcome:
    params = {} if direction is None else {"direction": list(direction)}
    return OptionOutcome(option_id=option_id, template="traverse", params=params,
                         start_position=None if start is None else list(start),
                         end_position=None, displacement=displacement, terminated_by=terminated_by,
                         duration_s=0.5, scene=scene)


# --- synthetic probes -------------------------------------------------------------


def ray(*, point=None, hit=True, forward=0.0, lateral=0.0) -> dict:
    """One down-ray or forward sample: where it landed, or that it landed nowhere."""
    row = {"forward": forward, "lateral": lateral, "hit": hit}
    if point is not None:
        row["point"] = list(point)
    return row


def cast(*, point=None, height=0.9, hit=True) -> dict:
    """One obstruction cast at a stated height."""
    row = {"height": height, "max_distance": 1.8, "hit": hit, "distance": None}
    if point is not None:
        row["point"] = list(point)
    return row


def probe(*, plane="2d", forward=(1.0, 0.0, 0.0), anchor=(0.0, 0.0, 0.0),
          under=None, samples=(), backward=(), blockers=()) -> dict:
    """A probe reading in the version-2 shape the bridge publishes."""
    return {"version": 2,
            "anchor": {"kind": "controlled", "name": "p", "position": list(anchor)},
            "basis": {"plane": plane, "forward": list(forward), "forward_source": "measured",
                      "lateral": [0.0, 0.0, 1.0]},
            "under": under if under is not None else ray(point=anchor),
            "samples": list(samples), "backward": list(backward), "blockers": list(blockers)}


def felt_2d() -> dict:
    """Floor under and one step ahead, a wall two steps ahead, a drop one step behind."""
    return probe(under=ray(point=(0.0, 0.0, 0.0)),
                 samples=[ray(point=(1.0, 0.0, 0.0), forward=1.0)],
                 backward=[ray(hit=False, forward=-1.0, lateral=0.0)],
                 blockers=[cast(point=(2.0, 0.0, 0.0), height=0.9)])


def felt_3d() -> dict:
    return probe(plane="3d", under=ray(point=(0.0, 0.0, 0.0)),
                 samples=[ray(point=(1.0, 0.0, 0.0), forward=1.0)],
                 blockers=[cast(point=(2.0, 0.0, 0.0), height=1.9)])


# --- the file --------------------------------------------------------------------


def test_a_saved_memory_round_trips_through_its_file(tmp_path):
    memory = OutcomeMemory(CELL)
    memory.record(outcome(start=[0.5, 0.0, 0.0], displacement=[0.4, 0.0, 0.0]))
    memory.record(outcome(option_id="hop-x", terminated_by="fell", start=[0.5, 0.0, 0.0],
                          displacement=[-0.2, -0.6, 0.0]))
    memory.note_run("run-1")
    path = tmp_path / "memory.json"
    memory.save(path)
    loaded = OutcomeMemory.load(path, CELL)
    assert loaded.to_json() == memory.to_json()
    assert json.loads(path.read_text(encoding="utf-8")) == memory.to_json()
    assert loaded.suffix("scene-a", [0.5, 0.0, 0.0], "hop+x") == memory.suffix(
        "scene-a", [0.5, 0.0, 0.0], "hop+x")
    assert loaded.stats() == memory.stats() == {"run_ids": ["run-1"], "entries": 2, "outcomes": 2,
                                                "settled_points": 0, "felt_cells": 0}


def test_a_loaded_memory_keeps_what_earlier_runs_measured(tmp_path):
    """Loading happens before the first turn, so a label already states the earlier history."""
    earlier = OutcomeMemory(CELL)
    for _ in range(3):
        earlier.record(outcome(start=[0.5, 0.0, 0.0], displacement=[0.4, 0.0, 0.0]))
    path = tmp_path / "m.json"
    earlier.save(path)

    memory = OutcomeMemory.load(path, CELL)
    assert memory.suffix("scene-a", [0.5, 0.0, 0.0], "hop+x") == "here: 3 tried \u2014 blocked 3, best +0.4"
    memory.record(outcome(start=[0.5, 0.0, 0.0], displacement=[0.4, 0.0, 0.0]))
    memory.save(path)
    assert OutcomeMemory.load(path, CELL).suffix("scene-a", [0.5, 0.0, 0.0], "hop+x").startswith("here: 4 tried")


def test_autosave_writes_after_each_outcome_without_being_asked(tmp_path):
    path = tmp_path / "m.json"
    memory = OutcomeMemory(CELL)
    memory.autosave(path)
    assert not path.exists()  # naming the file measures nothing yet
    memory.record(outcome(start=[0.5, 0.0, 0.0], displacement=[0.4, 0.0, 0.0]))
    assert OutcomeMemory.load(path, CELL).stats()["outcomes"] == 1
    memory.record(outcome(start=[0.5, 0.0, 0.0], displacement=[0.4, 0.0, 0.0]))
    assert OutcomeMemory.load(path, CELL).stats()["outcomes"] == 2


def test_a_cell_measured_with_another_step_is_refused(tmp_path):
    path = tmp_path / "m.json"
    OutcomeMemory(CELL).save(path)
    with pytest.raises(ValueError, match="cell of 1.0000 world units and this run measured 2.0000"):
        OutcomeMemory.load(path, 2.0)
    with pytest.raises(ValueError, match="cannot merge"):
        OutcomeMemory(CELL).merge(OutcomeMemory(2.0))


def test_one_file_per_game_and_scene():
    files = {memory_file("somewhere", "project-a", scene) for scene in ("scene-a", None)}
    assert files == {Path("somewhere/project-a--scene-a.json"),
                     Path("somewhere/project-a--unknown.json")}


# --- merging ---------------------------------------------------------------------


def test_merge_adds_counts_unions_points_and_keeps_the_newest_felt_reading():
    first, second = OutcomeMemory(CELL), OutcomeMemory(CELL)
    first.record(outcome(start=[0.5, 0.0, 0.0], displacement=[0.4, 0.0, 0.0]))
    first.record(outcome(start=[0.5, 0.0, 0.0], displacement=[0.4, 0.0, 0.0]))
    second.record(outcome(start=[0.5, 0.0, 0.0], displacement=[0.2, 0.0, 0.0]))
    second.record(outcome(start=[0.5, 0.0, 0.0], displacement=[0.6, 0.0, 0.0], terminated_by="succeeded"))
    for memory, stamp, height in ((first, SEEN, 0.0),
                                  (second, datetime(2026, 11, 2, tzinfo=timezone.utc), 0.4)):
        for _ in range(3):  # three still observations settle one point
            memory.observe(observation(position=(0.5, 0.0, 0.0), wall_utc=stamp,
                                       probe=probe(under=ray(point=(0.0, height, 0.0)))))
    first.note_run("run-1")
    second.note_run("run-2")

    first.merge(second)
    assert first.stats()["run_ids"] == ["run-1", "run-2"]
    assert first.suffix("scene-a", [0.5, 0.0, 0.0], "hop+x") == "here: 4 tried \u2014 succeeded 1, blocked 3, best +0.6"
    assert first.settled_points("scene-a") == [[0.5, 0.0, 0.0]]
    cells = first.to_json()["felt"]
    assert [row["height"] for row in cells] == [0.4]  # the later reading of the same cell wins
    assert sum(row["samples"] for row in cells) == 6


# --- the felt map ----------------------------------------------------------------


def test_a_2d_probe_reads_floor_wall_and_drop_along_the_motion_axis():
    memory = OutcomeMemory(CELL)
    memory.observe(observation(**{"probe": felt_2d()}))
    here = [0.0, 0.0, 0.0]
    ahead = [1.0, 0.0, 0.0]
    assert memory.map_line("scene-a", here, ahead) == (
        "floor known 1.0 ahead, then wall \u22650.9 high; drop 1.0 behind")
    assert memory.wall_ahead("scene-a", here, ahead) == 0.9
    assert memory.map_line("scene-a", here, [-1.0, 0.0, 0.0]) == (
        "drop 1.0 ahead; floor known 1.0 behind, then wall \u22650.9 high")


def test_a_3d_probe_reads_the_full_cell_under_one_forward_axis():
    memory = OutcomeMemory(CELL)
    memory.observe(observation(**{"probe": felt_3d()}))
    assert memory.map_line("scene-a", [0.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == (
        "floor known 1.0 ahead, then wall \u22651.9 high")
    assert memory.wall_ahead("scene-a", [0.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 1.9
    assert all(len(row["cell"]) == 3 for row in memory.to_json()["felt"])


def test_unmeasured_stays_unmeasured():
    memory = OutcomeMemory(CELL)
    here = [0.0, 0.0, 0.0]
    ahead = [1.0, 0.0, 0.0]
    assert memory.map_line("scene-a", here, ahead) is None
    assert memory.wall_ahead("scene-a", here, ahead) is None
    assert memory.map_line(None, None, None) is None
    assert memory.wall_ahead("scene-a", here, [0.0, 0.0, 0.0]) is None
    memory.observe(observation(**{"probe": probe(samples=[ray(hit=None, forward=1.0)],
                                                 under=ray(hit=None))}))
    assert memory.stats()["felt_cells"] == 0
    assert memory.map_line("scene-a", here, ahead) is None


def test_an_unmeasured_column_stops_the_wall_search_before_a_farther_wall():
    memory = OutcomeMemory(CELL)
    memory.observe(observation(**{"probe": probe(blockers=[cast(point=(3.0, 0.0, 0.0), height=0.9)])}))
    assert memory.wall_ahead("scene-a", [0.0, 0.0, 0.0], [1.0, 0.0, 0.0]) is None


def test_a_reading_in_another_frame_is_not_filed():
    memory = OutcomeMemory(CELL)
    memory.observe(observation(**{"probe": felt_2d()}))
    before = memory.to_json()["felt"]
    memory.observe(observation(**{"probe": probe(plane="3d", anchor=(5.0, 5.0, 5.0),
                                                 under=ray(point=(5.0, 5.0, 5.0)))}))
    assert memory.to_json()["felt"] == before


def test_a_probe_without_a_frame_files_nothing():
    memory = OutcomeMemory(CELL)
    memory.observe(observation(probe={"under": {"hit": True, "delta_y": 0.0}}))
    assert memory.stats()["felt_cells"] == 0


# --- the menu --------------------------------------------------------------------


def test_progress_line_appends_the_map_line_as_it_is():
    line = progress_line(positions=[(0.0, 0.0, 0.0)], now=(1.0, 0.0, 0.0), start=(0.0, 0.0, 0.0),
                         grounded=True, probe=None, map_line="floor known 1.0 ahead")
    assert line.endswith(" \u00b7 floor known 1.0 ahead")


class _StubMemory:
    """A memory that states one map line and records the facing it was asked about."""

    cell = CELL

    def __init__(self) -> None:
        self.directions: list = []

    def observe(self, observation) -> None:
        pass

    def map_line(self, scene, position, direction):
        self.directions.append(direction)
        return None if direction is None else "floor known 1.0 ahead"

    def suffix(self, scene, position, option_id) -> str:
        return "here: untried"


def _menu(positions, now):
    memory = _StubMemory()
    choice = Choice(id="m", kind=ChoiceKind.action, action="m", source=ChoiceSource.manifest,
                    label="move +x: request a hold; calibrated net displacement 1.0 world units.")
    choices = types.SimpleNamespace(choices=[choice], set_version=0, built_from_seq=0)
    payload = compact_menu(objective="o", observation=observation(position=now), choices=choices,
                           memory=memory, positions=positions)
    return memory, payload


def test_compact_menu_walks_the_felt_map_along_the_last_measured_displacement():
    memory, payload = _menu([(0.0, 0.0, 0.0)], (1.0, 0.0, 0.0))
    assert memory.directions == [(1.0, 0.0, 0.0)]
    assert payload["progress"].endswith(" \u00b7 floor known 1.0 ahead")


def test_compact_menu_has_no_facing_when_the_position_did_not_move():
    memory, payload = _menu([(1.0, 0.0, 0.0)], (1.0, 0.0, 0.0))
    assert memory.directions == [None]
    assert " \u00b7 " not in payload["progress"]


# --- the flag --------------------------------------------------------------------


def _argv(tmp_path, *extra: str) -> list[str]:
    return ["--project", str(tmp_path / "game"), "--entity", "e", "--calibration", "c",
            "--output", str(tmp_path / "out"), "--approval", "a", "--max-calls", "1",
            "--manifest", "m", "--catalog", "cat", "--catalog-sha256", "0", *extra]


def test_the_memory_dir_flag_is_off_by_default_and_reads_the_environment(monkeypatch):
    from playtest.runner import cli

    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    monkeypatch.delenv("UPA_RUN_MEMORY_DIR", raising=False)
    assert cli._parser().parse_args(required).memory_dir is None
    monkeypatch.setenv("UPA_RUN_MEMORY_DIR", "somewhere")
    assert cli._parser().parse_args(required).memory_dir == Path("somewhere")


def test_memory_dir_stops_at_startup_without_options_or_inside_the_game(tmp_path, capsys, monkeypatch):
    from playtest.runner import cli

    monkeypatch.delenv("UPA_RUN_MEMORY_DIR", raising=False)
    monkeypatch.setenv("LIQUID_API_KEY", "test")  # the key is only checked for by name
    monkeypatch.setenv("LIQUID_D1.1_URL", "test")
    with pytest.raises(SystemExit) as raised:
        cli.main(_argv(tmp_path, "--memory-dir", str(tmp_path / "memory")))
    assert raised.value.code == 2
    assert "--memory-dir requires --options" in capsys.readouterr().err
    with pytest.raises(SystemExit) as raised:
        cli.main(_argv(tmp_path, "--options", "--memory-dir", str(tmp_path / "game" / "memory")))
    assert raised.value.code == 2
    assert "must be outside the Unity project" in capsys.readouterr().err


def test_the_plan_states_what_the_memory_loaded_and_saved(tmp_path):
    from playtest.runner import cli

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"version": 1, "kind": "session"}), encoding="utf-8")
    facts = {"dir": "memory", "file": "game--scene.json", "loaded": {"entries": 2}, "saved": None, "error": None}
    cli._plan_memory(tmp_path, facts)
    assert json.loads(plan.read_text(encoding="utf-8"))["memory"] == facts
    cli._plan_memory(tmp_path / "elsewhere", facts)  # a folder with no plan is left alone, not created
