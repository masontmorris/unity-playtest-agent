"""The shuffled re-test records a pass plan, labels it mechanically and never repairs a model's choice."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from playtest.bench import record as recording
from playtest.bench import retest
from playtest.bench.record import digest, pass_plan
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource

SEED = 2410
COMPLETED = re.compile(r"^(c\d+) \(")


def _save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _choices() -> list[Choice]:
    controls = [Choice(id=f"c{i}", kind=ChoiceKind.action, label=f"Control {i}", action=f"action{i}",
                       params={"duration_game_s": 1.2}, source=ChoiceSource.manifest) for i in range(1, 9)]
    return controls + [Choice(id="wait", kind=ChoiceKind.wait, label="Wait", source=ChoiceSource.core),
                       Choice(id="finish", kind=ChoiceKind.finish, label="Finish", source=ChoiceSource.core)]


class _Client:
    def __init__(self) -> None:
        self.seq = 0
        self.sent: list[dict] = []

    async def inspect(self, stage):
        self.seq += 1
        return SimpleNamespace(seq=self.seq)

    async def observe_once(self):
        self.seq += 1
        return Observation(session="synthetic", seq=self.seq, game_s=float(self.seq),
                           wall_utc=datetime(2026, 1, 1, tzinfo=timezone.utc), clock_mode=ClockMode.realtime,
                           scene="scene", playing=True, paused=False, time_scale=1, frame=self.seq, reader_errors=[])

    async def act(self, **kwargs):
        self.sent.append(kwargs)
        return [SimpleNamespace(status=SimpleNamespace(value="completed"), release_verified=True,
                                applied_duration_s=1.2, model_dump=lambda mode: {
                                    "status": "completed", "release_verified": True, "applied_duration_s": 1.2})]


def _record(tmp_path: Path, monkeypatch, *, protocol: str, seed: int | None = SEED) -> tuple[dict, _Client]:
    discovery = {"inspection_coverage": {name: {"truncated": False} for name in
                 ("game_objects", "components", "bindings", "input_asset_bindings")},
                 "game_objects": [{"id": "entity", "scene_id": "scene", "scene_path": "scene", "path": "object"}]}
    monkeypatch.setattr(recording, "_complete", lambda response, stage: discovery)
    monkeypatch.setattr(recording, "calibrated_choices", lambda calibration, current: _choices())
    monkeypatch.setattr(recording, "_new_runtime_errors", lambda sample, previous: (previous, []))
    monkeypatch.setattr(recording, "_entity_snapshot", lambda response, entity: {
        "position": [float(response.seq), 0.0, 0.0], "game_time_s": float(response.seq)})

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(recording.asyncio, "sleep", no_sleep)
    selected = {"status": "identified", "entity_id": "entity", "stop": {"release_verified": True},
                "entity": {"scene_id": "scene", "scene_path": "scene", "path": "object"}}
    client = _Client()
    result: dict = {"version": 2 if protocol == "shuffled" else 1, "status": "recording", "frames": []}
    asyncio.run(recording.record(client, selected, {"entity_id": "entity"}, result, tmp_path / "partial.json",
                                 protocol=protocol, seed=seed))
    return result, client


def _corpus(tmp_path: Path, monkeypatch) -> Path:
    result, _ = _record(tmp_path, monkeypatch, protocol="shuffled")
    result.update(status="complete", stop={"release_verified": True}, requested_hold_s=1.2, game_commit="game",
                  repo_commit="repo", unity_version="unity", bridge_source_sha256="b", python_source_sha256="p",
                  entity_sha256="e", calibration_sha256="c", instructions_sha256="i", objective_sha256="o",
                  game_worktree_before={"commit": "game", "changed_files_sha256": {}},
                  game_worktree_after={"commit": "game", "changed_files_sha256": {}})
    path = tmp_path / "corpus.json"
    _save(path, result)
    return path


def _labelled(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    corpus = _corpus(tmp_path, monkeypatch)
    labels = tmp_path / "labels.json"
    retest.draft_labels(corpus, labels, date="2026-10-27")
    return corpus, labels


def _approval(tmp_path: Path, calls: int = 50) -> Path:
    limit = {"max_calls": calls, "max_cost_usd": 0.01, "per_call_max_usd": 0.0001, "verified_balance_usd": 1,
             "balance_checked_at_utc": "2026-01-01T00:00:00Z", "price_valid_until_utc": "2099-01-01T00:00:00Z"}
    path = tmp_path / "approval.json"
    _save(path, {"approved_by": "Test User", "approved_at_utc": "2026-01-01T00:00:00Z", "max_calls": calls,
                 "max_cost_usd": 0.01, "arms": {"mammoth-1": limit}})
    return path


def _answer(arm: str, criteria: dict, choice: str):
    others = len(criteria) - 1
    probabilities = {item: (0.9 if item == choice else 0.1 / others) for item in criteria}
    return 200, {"model": arm, "answers": {"next_action": {"type": "choice", "choice": choice,
                                                           "probabilities": probabilities}}}, 12.0, None


def _state(body: dict) -> tuple[list[str], list[str], list[str]]:
    criteria = body["questions"]["next_action"]["criteria"]
    done = [m.group(1) for fact in body["state"]["facts"] if (m := COMPLETED.match(fact))]
    controls = sorted((item for item in criteria if re.fullmatch(r"c\d+", item)), key=lambda item: int(item[1:]))
    return done, controls, [item for item in controls if item not in done]


def _perfect(arm: str, body: dict):
    done, controls, todo = _state(body)
    return _answer(arm, body["questions"]["next_action"]["criteria"], todo[0] if todo else "finish")


def test_pass_plan_is_deterministic_and_breaks_the_first_corpus_regularities() -> None:
    ascending = list(range(1, 9))
    assert pass_plan("ascending", None, 1) == (ascending, list(range(8)))
    plans = [pass_plan("shuffled", SEED, pass_no) for pass_no in range(1, 6)]
    assert plans == [pass_plan("shuffled", SEED, pass_no) for pass_no in range(1, 6)]
    assert len({(tuple(ids), tuple(visit)) for ids, visit in plans}) == 5
    for ids, visit in plans:
        assert sorted(ids) == ascending and sorted(visit) == list(range(8))
        assert ids != ascending and visit != list(range(8)) and [ids[i] for i in visit] != ascending
    assert pass_plan("shuffled", SEED + 1, 1) != plans[0]
    with pytest.raises(ValueError, match="integer seed"):
        pass_plan("shuffled", None, 1)


def test_shuffled_recording_follows_the_plan_and_adds_terminal_frames(tmp_path: Path, monkeypatch) -> None:
    result, client = _record(tmp_path, monkeypatch, protocol="shuffled")
    frames = result["frames"]
    assert len(frames) == 45 and len(client.sent) == 40 and len(result["protocol"]["passes"]) == 5
    for pass_no in range(1, 6):
        ids, visit = pass_plan("shuffled", SEED, pass_no)
        assert result["protocol"]["passes"][pass_no - 1] == {"pass": pass_no, "ids": ids, "visit": visit}
        mine = [f for f in frames if f["pass"] == pass_no]
        assert [f["id"] for f in mine] == [f"pass-{pass_no:02d}-s{k}" for k in range(1, 9)] + [f"pass-{pass_no:02d}-fin"]
        assert [f["scripted_choice_id"] for f in mine[:8]] == [f"c{ids[i]}" for i in visit]
        terminal = mine[8]
        assert terminal["kind"] == "terminal" and terminal["execution"] is None and terminal["scripted_choice_id"] is None
        assert len([f for f in terminal["facts"] if COMPLETED.match(f)]) == 8
        assert {c["id"]: c["action"] for c in terminal["choice_set"]["choices"] if c["kind"] == "action"} == {
            f"c{ids[i]}": f"action{i + 1}" for i in range(8)}
        listed = [c["id"] for c in terminal["choice_set"]["choices"]]
        assert listed == [f"c{n}" for n in range(1, 9)] + ["wait", "finish"]
    assert [item["action_id"] for item in client.sent[:2]] == ["bench-1-" + f["scripted_choice_id"] for f in frames[:2]]


def test_ascending_recording_keeps_the_first_corpus_shape(tmp_path: Path, monkeypatch) -> None:
    result, client = _record(tmp_path, monkeypatch, protocol="ascending", seed=None)
    assert len(result["frames"]) == 40 and "protocol" not in result
    assert [f["id"] for f in result["frames"][:3]] == ["pass-01-c1", "pass-01-c2", "pass-01-c3"]
    assert all("kind" not in f and "position" not in f for f in result["frames"])
    assert [item["choice_id"] for item in client.sent[:8]] == [f"c{i}" for i in range(1, 9)]


def test_draft_labels_accept_unchecked_controls_then_finish(tmp_path: Path, monkeypatch) -> None:
    corpus, labels = _labelled(tmp_path, monkeypatch)
    data = json.loads(labels.read_text(encoding="utf-8"))
    assert data["authorship"] == {"kind": "assistant_draft", "author": "Claude Sonnet 5.5",
                                  "human_review": "pending", "reviewer": None, "reviewed_at": None}
    rows = {row["frame_id"]: row for row in data["labels"]}
    assert len(rows) == 45
    for pass_no in range(1, 6):
        for step in range(1, 9):
            assert len(rows[f"pass-{pass_no:02d}-s{step}"]["acceptable"]) == 9 - step
        assert rows[f"pass-{pass_no:02d}-fin"]["acceptable"] == ["finish"]
    frame = json.loads(corpus.read_text(encoding="utf-8"))["frames"][3]
    done = {m.group(1) for fact in frame["facts"] if (m := COMPLETED.match(fact))}
    assert not done & set(rows[frame["id"]]["acceptable"])
    with pytest.raises(FileExistsError):
        retest.draft_labels(corpus, labels, date="2026-10-27")
    retest.load_labels(labels, *retest.load_corpus(corpus))


def test_labelling_stops_when_recorded_facts_and_executions_disagree(tmp_path: Path, monkeypatch) -> None:
    corpus = _corpus(tmp_path, monkeypatch)
    monkeypatch.setattr(retest, "completed_ids", lambda frame: {"c1"})
    with pytest.raises(ValueError, match="executions and facts disagree"):
        retest.draft_labels(corpus, tmp_path / "labels.json", date="2026-10-27")


def test_corpus_that_deviates_from_its_plan_is_rejected(tmp_path: Path, monkeypatch) -> None:
    corpus = _corpus(tmp_path, monkeypatch)
    original = json.loads(corpus.read_text(encoding="utf-8"))

    def broken(edit, message: str) -> None:
        data = json.loads(json.dumps(original))
        edit(data)
        path = tmp_path / "broken.json"
        _save(path, data)
        with pytest.raises(ValueError, match=message):
            retest.load_corpus(path)

    broken(lambda d: d["protocol"]["passes"][0].update(ids=list(range(1, 9))), "not shuffled")
    broken(lambda d: d["protocol"]["passes"][0].update(visit=list(reversed(d["protocol"]["passes"][0]["visit"]))),
           "does not follow the pass plan|differ from the pass plan")
    broken(lambda d: d["protocol"]["passes"][1].update(ids=[1, 1, 3, 4, 5, 6, 7, 8]), "not a permutation")
    broken(lambda d: d["frames"][8].update(execution=[{"status": "completed"}]), "should not have held|payload|context")
    broken(lambda d: d.update(version=1), "shuffled-protocol recording is required")
    broken(lambda d: d["frames"].pop(), "passes of 9 frames")
    broken(lambda d: d.update(game_worktree_after={"commit": "game", "changed_files_sha256": {"x": "1"}}),
           "worktree is unknown or changed")


def test_run_makes_each_scheduled_call_once_and_resumes_without_new_calls(tmp_path: Path, monkeypatch) -> None:
    corpus, labels = _labelled(tmp_path, monkeypatch)
    approval, directory, sent = _approval(tmp_path), tmp_path / "run", []

    def fake(arm: str, body: dict):
        sent.append(body)
        return _perfect(arm, body)

    assert retest.run(corpus, labels, approval, directory, seed=7, send=fake) == 50
    assert retest.run(corpus, labels, approval, directory, seed=7, send=fake) == 0
    assert len(sent) == 50 and len(list((directory / "calls").glob("*.json"))) == 50
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    assert plan["scheduled_calls"] == 50 and len(plan["repeated_frames"]) == 5 and plan["arm"] == "mammoth-1"
    for key in ("corpus_sha256", "labels_sha256", "approval_sha256", "retest_source_sha256",
                "replay_source_sha256", "record_source_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", plan[key])
    report = retest.score(corpus, labels, directory)
    assert report["top1_acceptable"]["all"] == {"hits": 45, "scored": 45, "frames": 45}
    assert report["top1_acceptable"]["terminal"] == {"hits": 5, "scored": 5, "frames": 5}
    assert report["misses"] == [] and report["unknown"] == 0 and report["attempted"] == 45
    assert report["determinism"]["repeated"] == 5 and report["determinism"]["identical_distribution"] == 5
    assert report["determinism"]["max_probability_difference"] == 0
    assert report["by_position"]["9"]["hits"] == 5 and report["by_pass"]["3"]["frames"] == 9
    assert report["acceptable_mass"]["min"] == pytest.approx(0.9) and report["acceptable_mass"]["marginal_hits"] == []
    assert report["completed_set_is_id_prefix"]["frames"] == 35
    assert report["completed_set_is_id_prefix"]["prefix"] < 35
    assert report["cost"] == {"calls_reporting_cost": 0, "cost_usd": None}
    assert report["label_authorship"]["human_review"] == "pending"
    assert "verdict" not in report


def test_envelope_smaller_than_the_schedule_stops_before_any_call(tmp_path: Path, monkeypatch) -> None:
    corpus, labels = _labelled(tmp_path, monkeypatch)
    sent: list[str] = []
    with pytest.raises(ValueError, match="envelope is smaller than the schedule"):
        retest.run(corpus, labels, _approval(tmp_path, calls=49), tmp_path / "run", seed=7,
                   send=lambda arm, body: sent.append(arm))
    assert sent == [] and not (tmp_path / "run" / "calls").exists()


def test_a_failed_call_stops_the_arm_without_retries(tmp_path: Path, monkeypatch) -> None:
    corpus, labels = _labelled(tmp_path, monkeypatch)
    sent: list[str] = []

    def failed(arm: str, body: dict):
        sent.append(arm)
        return 401, None, 3.0, "HTTP error"

    directory = tmp_path / "run"
    assert retest.run(corpus, labels, _approval(tmp_path), directory, seed=7, send=failed) == 1
    assert retest.run(corpus, labels, _approval(tmp_path), directory, seed=7, send=failed) == 0
    assert len(sent) == 1
    report = retest.score(corpus, labels, directory)
    assert report["attempted"] <= 1 and report["scored"] == 0 and report["unknown"] == 45
    assert report["misses"] == [] and report["not_attempted"] >= 44


def test_every_miss_is_named_by_kind_and_none_is_repaired(tmp_path: Path, monkeypatch) -> None:
    corpus, labels = _labelled(tmp_path, monkeypatch)

    def flawed(arm: str, body: dict):
        done, controls, todo = _state(body)
        criteria = body["questions"]["next_action"]["criteria"]
        if len(done) == 3:
            return _answer(arm, criteria, done[0])        # re-check
        if len(done) == 4:
            return _answer(arm, criteria, "finish")       # premature finish
        if len(done) == 5:
            return _answer(arm, criteria, "wait")
        if len(done) == 8:
            return _answer(arm, criteria, controls[0])    # never finishes
        return _answer(arm, criteria, todo[0])

    directory = tmp_path / "run"
    assert retest.run(corpus, labels, _approval(tmp_path), directory, seed=3, send=flawed) == 50
    report = retest.score(corpus, labels, directory)
    assert report["top1_acceptable"]["check"] == {"hits": 25, "scored": 40, "frames": 40}
    assert report["top1_acceptable"]["terminal"]["hits"] == 0
    assert report["miss_kinds"] == {"re-check": 5, "premature-finish": 5, "wait": 5, "missed-finish": 5}
    assert len(report["misses"]) == 20
    assert report["by_position"]["4"]["hits"] == 0 and report["by_position"]["1"]["hits"] == 5


def test_changed_source_or_inputs_refuse_to_score(tmp_path: Path, monkeypatch) -> None:
    corpus, labels = _labelled(tmp_path, monkeypatch)
    directory = tmp_path / "run"
    retest.run(corpus, labels, _approval(tmp_path), directory, seed=7, send=_perfect)
    plan_path = directory / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for key, message in (("retest_source_sha256", "retest.py changed"), ("replay_source_sha256", "replay.py changed"),
                         ("record_source_sha256", "record.py changed"), ("labels_sha256", "frozen corpus and labels")):
        changed = dict(plan, **{key: digest("different")})
        _save(plan_path, changed)
        with pytest.raises(ValueError, match=message):
            retest.score(corpus, labels, directory)
    _save(plan_path, plan)
    retest.score(corpus, labels, directory)
    with pytest.raises(ValueError, match="differs"):
        retest.run(corpus, labels, _approval(tmp_path, calls=60), directory, seed=8, send=_perfect)
