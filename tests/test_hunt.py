"""The hunt runs each existing step once, in order, resumes from what is there, and reports what came of it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from playtest.runner import hunt


def _config(tmp_path: Path, **extra) -> dict:
    for name in ("entity", "calibration", "manifest", "discovery", "approval"):
        (tmp_path / f"{name}.json").write_text("{}", encoding="utf-8")
    raw = {"game": "Test game", "project": str(tmp_path / "game"), "ref": "eval/x/all",
           **{name: str(tmp_path / f"{name}.json") for name in ("entity", "calibration", "manifest", "discovery",
                                                                "approval")}, "sessions": 2, **extra}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return hunt.load_config(path)


ACTIONS = {"action_rules": [], "situations": [
    {"action": "interact", "situation": "near NPC to talk to", "region_id": "situation_1", "within": 5.0,
     "condition_observable": None}]}
FINDING = {"finding_id": "f-9", "state": "candidate", "title": "talking does nothing", "expected": "dialogue starts",
           "actual": "it never does", "repro_check": {"field": "public.dialogue_typing", "op": "!=", "value": True,
                                                      "after_action": "interact", "within_region": "situation_1"}}


class Steps:
    """Stands in for every child process: writes what the real step would, and notes what was asked."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, log, env=None):
        self.calls.append(list(args))
        joined = " ".join(map(str, args))
        out = Path(args[args.index("-OutputDirectory") + 1]) if "-OutputDirectory" in args else None
        if "-GuardsOnly" in args:
            out.mkdir(parents=True)
            (out / "guards.json").write_text("{}", encoding="utf-8")
        elif "predict" in args:
            folder = Path(args[args.index("--output") + 1])
            folder.mkdir(parents=True)
            part = args[args.index("--part") + 1]
            (folder / "expectations.json").write_text(json.dumps(ACTIONS if part == "actions" else {"predictions": []}),
                                                      encoding="utf-8")
        elif "reproduce.ps1" in joined:
            out.mkdir(parents=True)
            (out / "reproduce.json").write_text(json.dumps({"status": "measured", "summary": {
                "k": 5, "n": 5, "unknown": 0, "attempted": 5}}), encoding="utf-8")
        elif "run-session.ps1" in joined:
            out.mkdir(parents=True)
            (out / "run.json").write_text("{}", encoding="utf-8")
            (out / "guards.json").write_text("{}", encoding="utf-8")
            entries = [{"trace_seq": 1, "kind": "finding", "body": FINDING}] if out.name == "session-01" else []
            (out / "trace.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return 0


@pytest.fixture
def steps(monkeypatch):
    fake = Steps()
    monkeypatch.setattr(hunt, "_run", fake)
    monkeypatch.setattr(hunt, "project_ref", lambda project: "eval/x/all")
    monkeypatch.setattr(hunt, "credentials", lambda: {"META_API_KEY": "k"})
    scored = []
    monkeypatch.setattr(hunt, "scores_step", lambda *a: scored.append(a) or {
        "guards": {"scored": []}, "play": {"scored": []},
        "rules": {"scored": [{"verdict": "mismatch", "line": "pressing interact: dialogue_typing reaches true"}]}})
    return fake


def test_a_hunt_runs_each_step_in_order_replays_what_was_filed_and_reports_it(tmp_path, steps):
    config = _config(tmp_path)
    result = hunt.hunt(config, tmp_path / "out", say=lambda text: None)
    kinds = [("guards" if "-GuardsOnly" in c else "predict " + c[c.index("--part") + 1] if "predict" in c
              else "replay" if any("reproduce.ps1" in str(a) for a in c) else "session") for c in steps.calls]
    assert kinds == ["guards", "predict all", "predict actions", "predict magnitudes", "session", "session", "replay"]
    session = steps.calls[4]
    assert Path(session[session.index("-SituationsPath") + 1]).parent.name == "expect-actions"
    assert Path(session[session.index("-ExpectationsPath") + 1]).parent.name == "expect-all"
    assert "-PressReadings" in session and "-MeasureGuards" in session
    objective = (tmp_path / "out" / "objective.txt").read_text(encoding="utf-8")
    assert objective.startswith("Try each of these on purpose") and "test:<input>" in objective
    assert [f["finding_id"] for f in result["findings"]] == ["f-9"]
    assert result["findings"][0]["replay"]["k"] == 5
    text = (tmp_path / "out" / "hunt.md").read_text(encoding="utf-8")
    assert "**f-9**" in text and "reproduced 5 of 5" in text and "dialogue_typing reaches true" in text

    steps.calls.clear()   # a second run resumes: nothing already done runs again
    hunt.hunt(config, tmp_path / "out", say=lambda text: None)
    assert steps.calls == []


def test_the_declared_sizes_the_focused_call_wrote_are_stated_in_each_session_beside_the_full_predictions(
        tmp_path, steps, monkeypatch):
    size = {"quantity": "move_speed", "action": "move_right", "state": None, "value": 10.0, "basis": "declared_value",
            "because": "speed 10"}
    original = steps.__call__

    def with_sizes(args, log, env=None):
        code = original(args, log, env)
        if "predict" in args and args[args.index("--part") + 1] == "magnitudes":
            Path(args[args.index("--output") + 1], "expectations.json").write_text(
                json.dumps({"predictions": [], "magnitudes": [size]}), encoding="utf-8")
        return code

    monkeypatch.setattr(hunt, "_run", with_sizes)
    hunt.hunt(_config(tmp_path), tmp_path / "out", say=lambda text: None)
    session = next(c for c in steps.calls if "-ExpectationsPath" in c)
    stated = Path(session[session.index("-ExpectationsPath") + 1])
    assert stated.name == "expectations-session.json"
    assert json.loads(stated.read_text(encoding="utf-8")) == {"predictions": [], "magnitudes": [size]}


def test_the_report_lists_every_declared_size_and_counts_a_size_off_in_most_tables_as_unmet(tmp_path):
    sizes = [{"verdict": "held", "line": "move speed of move_right: expected 10"},
             {"verdict": "mismatch", "line": "verb speed of dash while move_right held: expected 15"}]
    scores = {"guards": {"scored": []}, "magnitudes": {"scored": sizes}, "play": {"scored": []},
              "rules": {"scored": []}}
    text = hunt.report(_config(tmp_path), tmp_path, [], [], scores)
    unmet, measured = text.split("## Declared sizes measured on the guard tables")
    assert "verb speed of dash" in unmet and "move speed of move_right" not in unmet
    assert "- held: move speed of move_right" in measured and "- mismatch: verb speed of dash" in measured


def test_a_hunt_refuses_a_project_on_another_branch_and_a_config_with_unknown_keys(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(hunt, "project_ref", lambda project: "upa/clean")
    with pytest.raises(hunt.HuntError, match="check out the build under test"):
        hunt.hunt(config, tmp_path / "out", say=lambda text: None)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({**json.loads((tmp_path / "config.json").read_text()), "wall": 5}), encoding="utf-8")
    with pytest.raises(hunt.HuntError, match="unknown keys wall"):
        hunt.load_config(path)


def test_a_finding_with_no_replay_check_is_reported_as_not_replayable(tmp_path, steps, monkeypatch):
    FINDING_NONE = {**FINDING, "repro_check": None}
    original = steps.__call__

    def without_check(args, log, env=None):
        code = original(args, log, env)
        if "-OutputDirectory" in args and args[args.index("-OutputDirectory") + 1].endswith("session-01"):
            Path(args[args.index("-OutputDirectory") + 1], "trace.jsonl").write_text(
                json.dumps({"trace_seq": 1, "kind": "finding", "body": FINDING_NONE}) + "\n", encoding="utf-8")
        return code

    monkeypatch.setattr(hunt, "_run", without_check)
    result = hunt.hunt(_config(tmp_path), tmp_path / "out", say=lambda text: None)
    assert result["findings"][0]["replay"] == {"status": "no repro_check"}
    assert not any("reproduce.ps1" in " ".join(map(str, c)) for c in steps.calls)


def test_a_lock_no_running_unity_holds_is_removed_and_one_that_may_be_held_is_left(tmp_path, monkeypatch):
    project = tmp_path / "Game Project"
    (project / "Temp").mkdir(parents=True)
    lock = project / "Temp" / "UnityLockfile"
    said = []
    lock.write_text("", encoding="utf-8")
    monkeypatch.setattr(hunt, "unity_projects", lambda: [hunt._norm(f'unity.exe -projectpath "{project}" -batchmode')])
    hunt.clear_stale_lock(project, said.append)
    assert lock.is_file() and said == []
    monkeypatch.setattr(hunt, "unity_projects", lambda: None)   # cannot tell: left alone
    hunt.clear_stale_lock(project, said.append)
    assert lock.is_file()
    monkeypatch.setattr(hunt, "unity_projects", lambda: [hunt._norm("unity.exe -projectPath C:/Other")])
    hunt.clear_stale_lock(project, said.append)
    assert not lock.is_file() and said and "stale Unity lock" in said[0]


def test_a_prompt_is_quoted_first_in_the_objective_and_an_overlong_one_is_refused(tmp_path, steps):
    config = _config(tmp_path, prompt="  check the doors open  ")
    assert config["prompt"] == "check the doors open"
    hunt.hunt(config, tmp_path / "out", say=lambda text: None)
    objective = (tmp_path / "out" / "objective.txt").read_text(encoding="utf-8")
    assert objective.startswith('The developer asked this playtest to look at: "check the doors open".')
    assert "Try each of these on purpose" in objective
    with pytest.raises(hunt.HuntError, match="prompt"):
        _config(tmp_path, prompt="x" * (hunt.PROMPT_LIMIT + 1))
    assert _config(tmp_path, prompt="   ")["prompt"] is None

