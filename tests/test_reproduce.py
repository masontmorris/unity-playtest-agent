"""`reproduce` (#38): plan from a finished trace, replay once per attempt after a reset, and count k of n honestly.

No Unity and no model: a synthetic trace, a fake bridge client and fake readers stand in. What is checked is the
bookkeeping a report will rest on: which inputs replay, at what offsets, how each reading is scored, and that a
diverged or unscoreable attempt never counts as a reproduction or as a miss.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from datetime import datetime, timezone

from loop_fakes import TOKEN, TransformsGame
from runner_manifest import action, fixture, hold, manifest as build_manifest
from playtest.contracts.investigation import Finding, FindingState
from playtest.contracts.manifest import PreflightManifest
from playtest.core.bridge_client import BridgeClient
from playtest.preflight.verify import MEASUREMENT_TOLERANCE
from playtest.runner import reproduce
from playtest.runner.probe import make_snapshot


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(PreflightManifest.model_validate(fixture()).model_dump_json(), encoding="utf-8")
    return path


def _utc(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat()


def _execution(seq: int, turn: int, action: str, status: str, start: float | None, lease: float | None = 0.4,
               purpose: str = "choice", params: dict[str, Any] | None = None) -> dict[str, Any]:
    record = {"status": status, "input_start_wall_s": start, "applied_duration_s": lease if status == "completed" else None,
              "release_verified": True if status == "completed" else None,
              "reject_reason": "stale_age" if status == "rejected" else None}
    return {"trace_seq": seq, "kind": "execution", "turn": turn,
            "body": {"purpose": purpose,
                     "request": {"action": action, "choice_id": action, "params": params or {}, "lease_s": lease},
                     "records": [record]}}


def _finding(seq: int, evidence: list[int], check: dict[str, Any] | None, state: str = "candidate") -> dict[str, Any]:
    body = {"finding_id": "f-7", "state": state, "title": "Speed jumps after a short hold", "expected": "Speed stays low",
            "expected_source": "invariant", "actual": "Speed reached 9", "evidence_seq": evidence, "repro_check": check}
    return {"trace_seq": seq, "kind": "finding", "turn": None, "body": body}


def _trace(tmp_path: Path, *, check: dict[str, Any] | None = None, extra: list[dict[str, Any]] = ()) -> Path:
    check = check if check is not None else {"field": "public.speed", "op": ">=", "value": 9, "tolerance": None}
    entries = [
        {"trace_seq": 0, "kind": "run", "turn": None, "body": {"run_id": "r1", "game_commit": "abc"}},
        {"trace_seq": 1, "kind": "observation", "turn": 0, "body": {}},
        _execution(2, 0, "move", "completed", 1000.0),
        {"trace_seq": 3, "kind": "observation", "turn": 1, "body": {}},
        _execution(4, 1, "move", "rejected", None),                 # sent nothing in the session: skipped
        {"trace_seq": 5, "kind": "observation", "turn": 2, "body": {}},
        _execution(6, 2, "move", "completed", 1001.5, lease=0.8, purpose="while_pending"),
        {"trace_seq": 7, "kind": "observation", "turn": 3, "body": {"wall_utc": _utc(1002.7)}},
        _execution(8, 3, "move", "completed", 1003.0),             # sent after the evidence: not replayed
        _finding(9, [7, 3], check),
        *extra,
    ]
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


def test_the_plan_replays_only_what_executed_before_the_evidence_at_recorded_offsets(tmp_path):
    plan = reproduce.build_plan(_trace(tmp_path), _manifest(tmp_path), attempts=3)
    assert (plan["finding_id"], plan["evidence_turn"], plan["repro_check_source"]) == ("f-7", 3, "finding")
    assert plan["evidence_offset_s"] == 2.7   # the last cited observation, on the bridge clock the inputs use
    assert [(item["turn"], item["offset_s"], item["lease_s"], item["purpose"]) for item in plan["inputs"]] == [
        (0, 0.0, 0.4, "choice"), (2, 1.5, 0.8, "while_pending")]
    assert plan["skipped_inputs"] == {"rejected": 1}
    assert plan["repro_check_kind"] == "number" and plan["model_calls"] == 0 and plan["reset"] == "scene_reload"
    assert plan["host_budget_s"] > 3 * (reproduce.RELOAD_ALLOWANCE_S + 1.5)
    assert plan["effect"] is None and not any("scored" in item for item in plan["inputs"])   # a reader check scores no input


def test_the_plan_refuses_what_it_cannot_replay_or_score(tmp_path):
    manifest = _manifest(tmp_path)
    with pytest.raises(ValueError, match="no repro_check"):
        reproduce.build_plan(_no_check(tmp_path / "a"), manifest)
    with pytest.raises(ValueError, match="not one of the manifest's readers"):
        reproduce.build_plan(_trace(tmp_path / "b", check={"field": "public.missing", "op": ">", "value": 1}), manifest)
    with pytest.raises(ValueError, match="no finding"):
        reproduce.build_plan(_trace(tmp_path / "c"), manifest, finding_id="f-99")
    override = {"field": "public.speed", "op": "changed", "value": None}
    plan = reproduce.build_plan(_no_check(tmp_path / "d"), manifest, repro_check=override)
    assert plan["repro_check"]["op"] == "changed" and plan["repro_check_source"] == "operator"


def _no_check(tmp_path: Path) -> Path:
    path = _trace(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    entry["body"]["repro_check"] = None
    lines[-1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class FakeClient:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses, self.sent, self.closed, self.seq = list(statuses), [], False, 0

    async def observe_once(self):
        self.seq += 1
        return SimpleNamespace(seq=self.seq)

    async def act(self, **kwargs):
        self.sent.append(kwargs)
        status = self.statuses.pop(0) if self.statuses else "completed"
        reason = SimpleNamespace(value="stale_age") if status == "rejected" else None
        return [SimpleNamespace(status=SimpleNamespace(value=status), reject_reason=reason,
                                release_verified=True if status == "completed" else None,
                                applied_duration_s=kwargs.get("lease_s") if status == "completed" else None)]

    async def aclose(self):
        self.closed = True
        return None


class FakeReaders:
    def __init__(self, values: list[Any]) -> None:
        self.values = list(values)

    async def install(self):
        return None

    async def read(self):
        value = self.values.pop(0) if self.values else (self.values_last if hasattr(self, "values_last") else None)
        return SimpleNamespace(values={"speed": value}, errors={}, game_s=1.0, problem=None)


def _plan(tmp_path: Path, attempts: int = 1) -> dict[str, Any]:
    plan = reproduce.build_plan(_trace(tmp_path), _manifest(tmp_path), attempts=attempts)
    plan.update(start_settle_s=0.0, settle_s=0.02, sample_interval_s=0.01)
    for item in plan["inputs"]:
        item["offset_s"] = item["offset_s"] / 100   # keep the test fast; offsets are still honoured in order
    plan["evidence_offset_s"] = plan["evidence_offset_s"] / 100
    return plan


def _run(plan, client: FakeClient, readers: FakeReaders, number: int = 1):
    async def reload(n):
        return {"number": n, "scene_path": "Assets/Main.unity"}

    async def connect():
        return client

    return asyncio.run(reproduce.run_attempt(plan, number, reload, connect, lambda _client: readers))


def test_an_attempt_reproduces_when_a_reading_satisfies_the_check_and_replays_each_input_once(tmp_path):
    plan = _plan(tmp_path)
    client = FakeClient(["completed", "completed"])
    attempt = _run(plan, client, FakeReaders([1.0, 2.0, 9.5]))          # baseline, after input 1, after input 2
    assert attempt["valid"] and attempt["result"] == "reproduced" and client.closed
    assert [kw["action"] for kw in client.sent] == ["move", "move"] and [kw["lease_s"] for kw in client.sent] == [0.4, 0.8]
    assert [row["recorded_offset_s"] for row in attempt["inputs"]] == [0.0, 0.015]
    assert attempt["baseline"] == 1.0 and attempt["readings"][1]["satisfied"] is True


def test_scored_readings_that_never_satisfy_the_check_are_a_miss_and_unscoreable_ones_are_unknown(tmp_path):
    plan = _plan(tmp_path)
    readers = FakeReaders([1.0, 2.0, 3.0]); readers.values_last = 3.0
    miss = _run(plan, FakeClient(["completed", "completed"]), readers)
    assert (miss["valid"], miss["result"]) == (True, "not_reproduced")
    blind = _run(plan, FakeClient(["completed", "completed"]), FakeReaders([None, None, None]))
    assert (blind["valid"], blind["result"]) == (True, "unknown")


def test_a_check_that_already_holds_after_the_reset_is_unknown_whatever_the_replay_shows(tmp_path):
    plan = _plan(tmp_path)
    readers = FakeReaders([9.5, 9.6, 9.7]); readers.values_last = 9.7      # speed >= 9 before any input
    held = _run(plan, FakeClient(["completed", "completed"]), readers)
    assert (held["valid"], held["result"], held["baseline_satisfied"]) == (True, "unknown", True)
    assert "already held after the reset" in held["reason"]
    plan["repro_check"] = {"field": "public.speed", "op": "unchanged", "value": None, "tolerance": None}
    readers = FakeReaders([1.0, 1.0, 1.0]); readers.values_last = 1.0
    unchanged = _run(plan, FakeClient(["completed", "completed"]), readers)
    assert (unchanged["result"], unchanged["baseline_satisfied"]) == ("unknown", True)   # always holds at the baseline
    plan["repro_check"] = {"field": "public.speed", "op": "changed", "value": None, "tolerance": None}
    readers = FakeReaders([1.0, 1.0, 2.0]); readers.values_last = 2.0
    changed = _run(plan, FakeClient(["completed", "completed"]), readers)
    assert (changed["result"], changed["baseline_satisfied"]) == ("reproduced", False)


def test_the_settle_window_reaches_past_the_evidence_time(tmp_path):
    plan = _plan(tmp_path)
    plan["evidence_offset_s"] = 0.15                                    # later than the last input plus settle_s
    readers = FakeReaders([1.0, 2.0, 3.0]); readers.values_last = 3.0
    attempt = _run(plan, FakeClient(["completed", "completed"]), readers)
    assert attempt["readings"][-1]["elapsed_s"] >= 0.15 + plan["settle_s"]


def test_a_rejected_replay_input_makes_the_attempt_diverge_and_it_is_never_counted(tmp_path):
    plan = _plan(tmp_path)
    client = FakeClient(["rejected"])
    attempt = _run(plan, client, FakeReaders([1.0, 9.9]))
    assert attempt["valid"] is False and attempt["result"] is None and len(client.sent) == 1   # never retried
    assert "diverged at turn 0" in attempt["reason"] and "stale_age" in attempt["reason"]


def test_k_of_n_keeps_unknown_and_invalid_attempts_out_and_moves_the_finding_honestly(tmp_path):
    plan = _plan(tmp_path, attempts=4)
    attempts = [{"number": 1, "valid": True, "result": "reproduced", "reason": "", "inputs": [{"deviation_s": 0.01}]},
                {"number": 2, "valid": True, "result": "not_reproduced", "reason": "", "inputs": [{"deviation_s": -0.03}]},
                {"number": 3, "valid": True, "result": "unknown", "reason": "", "inputs": []},
                {"number": 4, "valid": False, "result": None, "reason": "diverged", "inputs": []}]
    summary = reproduce.summarize(plan, attempts)
    assert (summary["k"], summary["n"], summary["unknown"], summary["valid"]) == (1, 2, 1, 3)
    assert summary["invalid"] == [{"number": 4, "reason": "diverged"}]
    assert summary["input_timing"]["max_abs_deviation_s"] == 0.03
    update = Finding.model_validate(reproduce.finding_update(plan, summary))
    assert update.state is FindingState.reproduced and (update.repro.attempts, update.repro.reproduced) == (2, 1)
    assert update.repro.reset == "scene_reload" and update.repro.clock_mode.value == "realtime"
    none = reproduce.summarize(plan, [attempts[1] | {"result": "not_reproduced"}])
    assert Finding.model_validate(reproduce.finding_update(plan, none)).state is FindingState.repro_attempted
    assert reproduce.finding_update(plan, reproduce.summarize(plan, [attempts[2]])) is None   # nothing scored
    control = {**plan, "repro_check_source": "operator"}      # the finding has its own check; this one replaced it
    assert reproduce.finding_update(control, summary) is None
    standin = {**control, "finding": {**plan["finding"], "repro_check": None}}   # it had none: the operator's counts
    assert Finding.model_validate(reproduce.finding_update(standin, summary)).state is FindingState.reproduced


def test_measure_runs_every_attempt_and_reports_k_of_n(tmp_path):
    plan = _plan(tmp_path, attempts=2)
    readers = iter([FakeReaders([1.0, 2.0, 9.0]), FakeReaders([1.0, 2.0, 3.0])])

    async def reload(n):
        return {"number": n}

    async def connect():
        return FakeClient(["completed", "completed"])

    def readers_for(_client):
        r = next(readers); r.values_last = 3.0
        return r

    report = asyncio.run(reproduce.measure(plan, reload, connect, readers_for))
    assert report["status"] == "measured" and (report["summary"]["k"], report["summary"]["n"]) == (1, 2)
    assert report["model_calls"] == 0 and report["finding_update"]["state"] == "reproduced"
    assert report["effect"] is None and report["summary"]["basis"] == reproduce.READER_BASIS   # a reader check, as before


# --- effect checks (#154) ------------------------------------------------------------------------
#
# An action that stops having its effect leaves every reader at rest, so no reader check can show it. An effect check
# compares the measured net displacement of the replayed execution with a threshold. No Unity and no model: a scripted
# position source stands in for `inspect("transforms")`, and the fake client of the tests above for the bridge.


def _effect_manifest(tmp_path: Path, move_hold: float = 0.8) -> Path:
    """A valid manifest whose `move`, `look` and `strafe` calibration measured, and whose `use` it did not."""
    built = build_manifest(
        action("move", "w", hold_s=move_hold, holds=[hold(move_hold, mean=6.04, variance=0.0001)]),
        action("look", "a", hold_s=0.25, holds=[hold(0.25, mean=0.0, effective=False)]),
        action("strafe", "d", hold_s=0.3, holds=[hold(0.3, mean=3.0, variance=0.0004)]),
        action("use", "e", hold_s=0.2))
    path = tmp_path / "effect-manifest.json"
    path.write_text(built.model_dump_json(), encoding="utf-8")
    return path


EFFECT_CHECK = {"field": "effect.move", "op": "<", "value": 3.0, "tolerance": None}
MOVE, LOOK = ("move", "completed", 1000.0, 0.8, None), ("look", "completed", 1001.0, 0.25, None)


def _effect_trace(tmp_path: Path, *, check: Any = ..., executions: list[tuple] | None = None) -> Path:
    """A session of `(action, status, start, lease, params)` executions, then the evidence, then one more `move`.

    By default: move, look, move. The last `move` before the evidence is the one an effect check scores.
    """
    check = EFFECT_CHECK if check is ... else check
    executions = executions if executions is not None else [MOVE, LOOK, ("move", "completed", 1002.0, 0.8, None)]
    entries: list[dict[str, Any]] = [
        {"trace_seq": 0, "kind": "run", "turn": None, "body": {"run_id": "r1", "game_commit": "abc"}}]
    for turn, (name, status, start, lease, params) in enumerate(executions):
        entries.append({"trace_seq": len(entries), "kind": "observation", "turn": turn, "body": {}})
        entries.append(_execution(len(entries), turn, name, status, start, lease=lease, params=params))
    turn = len(executions)
    entries.append({"trace_seq": len(entries), "kind": "observation", "turn": turn,
                    "body": {"wall_utc": _utc(1003.5)}})
    evidence = len(entries) - 1
    entries.append(_execution(len(entries), turn, "move", "completed", 1004.0, lease=0.8))   # after the evidence
    entries.append(_finding(len(entries), [evidence], check))
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


def _effect_plan(tmp_path: Path, attempts: int = 1, **kwargs: Any) -> dict[str, Any]:
    plan = reproduce.build_plan(_effect_trace(tmp_path, **kwargs), _effect_manifest(tmp_path), attempts=attempts)
    plan["start_settle_s"] = 0.0
    for item in plan["inputs"]:
        item["offset_s"] = item["offset_s"] / 100   # keep the test fast; offsets are still honoured in order
    return plan


class FakeSnapshots:
    """A scripted position source: each call yields the next reading, or raises it, and notes itself in `events`."""

    def __init__(self, *readings: Any, events: list[str] | None = None) -> None:
        self.readings, self.events, self.calls = list(readings), events if events is not None else [], 0

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        self.events.append("snapshot")
        reading = self.readings.pop(0)          # over-reading raises IndexError, so the attempt is not valid
        if isinstance(reading, Exception):
            raise reading
        return reading


def _at(x: float, seq: int) -> dict[str, Any]:
    return {"position": [x, 0.0, 0.0], "inspection_seq": seq, "game_time_s": float(seq)}


class LoggedClient(FakeClient):
    """The fake client, noting each observe and act in the log it shares with the position source."""

    def __init__(self, statuses: list[str], events: list[str]) -> None:
        super().__init__(statuses)
        self.events = events

    async def observe_once(self):
        self.events.append("observe")
        return await super().observe_once()

    async def act(self, **kwargs):
        self.events.append(f"act {kwargs['action']}")
        return await super().act(**kwargs)


def _no_readers(_client):
    raise AssertionError("an effect check reads no reader")


def _run_effect(plan, client, snapshots, number: int = 1):
    async def reload(n):
        return {"number": n, "scene_path": "Assets/Main.unity"}

    async def connect():
        return client

    return asyncio.run(reproduce.run_attempt(plan, number, reload, connect, _no_readers, lambda _client: snapshots))


STOPPED = (_at(0, 1), _at(6.0, 2), _at(6.0, 3), _at(6.05, 4))      # the first move carries it 6.0; the last, 0.05
CARRIED = (_at(0, 1), _at(6.0, 2), _at(6.0, 3), _at(12.0, 4))      # both carry it 6.0


def test_an_effect_plan_names_the_calibrated_displacement_and_marks_the_last_execution_of_the_action(tmp_path):
    plan = reproduce.build_plan(_effect_trace(tmp_path), _effect_manifest(tmp_path), attempts=3)
    effect = plan["effect"]
    assert (effect["action"], effect["expected_displacement"], effect["unit"]) == ("move", 6.04, "world units")
    assert effect["spread"] == pytest.approx(0.01)                                # sqrt of the calibration's variance
    assert effect["allowance"] == pytest.approx(0.01 + MEASUREMENT_TOLERANCE)     # the live probe's formula
    assert (effect["requested_hold_s"], effect["valid_trials"], effect["sample_variance"]) == (0.8, 2, 0.0001)
    assert (effect["executions"], effect["scored_turn"], effect["scored_trace_seq"]) == (2, 2, 6)
    assert [(item["turn"], item["action"], item.get("scored", False)) for item in plan["inputs"]] == [
        (0, "move", False), (1, "look", False), (2, "move", True)]               # the move after the evidence is not here
    assert (plan["repro_check_kind"], plan["repro_check"], plan["repro_check_source"]) == ("number", EFFECT_CHECK, "finding")
    assert plan["model_calls"] == 0 and plan["evidence_turn"] == 3


def test_an_operator_effect_check_stands_in_for_a_finding_that_had_none(tmp_path):
    plan = reproduce.build_plan(_effect_trace(tmp_path, check=None), _effect_manifest(tmp_path),
                                repro_check={"field": "effect.move", "op": ">=", "value": 9})
    assert plan["repro_check_source"] == "operator" and plan["effect"]["scored_turn"] == 2


def test_a_calibrated_displacement_of_zero_is_a_measured_number_not_a_missing_one(tmp_path):
    check = {"field": "effect.look", "op": ">", "value": 1.0, "tolerance": None}
    plan = reproduce.build_plan(_effect_trace(tmp_path, check=check), _effect_manifest(tmp_path))
    assert plan["effect"]["action"] == "look" and plan["effect"]["expected_displacement"] == 0.0
    assert plan["effect"]["scored_turn"] == 1


def test_only_the_scored_execution_has_to_be_the_calibrated_declaration(tmp_path):
    other_hold = ("move", "completed", 1000.0, 0.5, None)
    assert reproduce.build_plan(_effect_trace(tmp_path / "a", executions=[other_hold, MOVE]),
                                _effect_manifest(tmp_path))["effect"]["scored_turn"] == 1
    default_hold = ("move", "completed", 1000.0, None, None)    # no lease recorded: the declaration's own hold
    assert reproduce.build_plan(_effect_trace(tmp_path / "b", executions=[default_hold]),
                                _effect_manifest(tmp_path))["effect"]["scored_turn"] == 0


EFFECT_REFUSALS = [
    ("action_not_in_the_manifest", {"field": "effect.fly", "op": "<", "value": 1}, None,
     "'fly', which is not an action in the manifest"),
    ("action_calibration_did_not_measure", {"field": "effect.use", "op": "<", "value": 1}, None,
     "'use' has no calibrated expected displacement in the manifest"),
    ("action_never_executed_before_the_evidence", {"field": "effect.strafe", "op": "<", "value": 1}, None,
     "no input that completed before the finding's evidence is an execution of 'strafe'"),
    ("action_only_rejected", {"field": "effect.strafe", "op": "<", "value": 1},
     [MOVE, ("strafe", "rejected", None, 0.3, None)],
     "no input that completed before the finding's evidence is an execution of 'strafe'"),
    ("scored_execution_held_another_time", EFFECT_CHECK, [MOVE, ("move", "completed", 1002.0, 0.5, None)],
     "was not the calibrated declaration (params {}, lease 0.5 s; calibrated for a 0.8 s hold)"),
    ("scored_execution_overrode_a_parameter", EFFECT_CHECK, [MOVE, ("move", "completed", 1002.0, 0.8, {"hold_s": 0.5})],
     "was not the calibrated declaration (params {'hold_s': 0.5}"),
]


@pytest.mark.parametrize("check, executions, needle", [(c, e, n) for _, c, e, n in EFFECT_REFUSALS],
                         ids=[name for name, _, _, _ in EFFECT_REFUSALS])
def test_an_effect_plan_is_refused_when_calibration_or_the_trace_cannot_stand_behind_it(tmp_path, check, executions, needle):
    trace = _effect_trace(tmp_path / "t", check=check | {"tolerance": None}, executions=executions)
    with pytest.raises(ValueError, match=re.escape(needle)):
        reproduce.build_plan(trace, _effect_manifest(tmp_path))


def test_an_effect_attempt_is_reproduced_when_the_scored_executions_displacement_satisfies_the_check(tmp_path):
    plan = _effect_plan(tmp_path)
    events: list[str] = []
    client = LoggedClient(["completed"] * 3, events)
    attempt = _run_effect(plan, client, FakeSnapshots(*STOPPED, events=events))
    assert attempt["valid"] and attempt["result"] == "reproduced" and client.closed
    effect = attempt["effect"]
    assert effect["measured"] == pytest.approx(0.05) and effect["holds_for_expected"] is False
    assert [(m["turn"], m["scored"], m["satisfied"]) for m in effect["measurements"]] == [(0, False, False), (2, True, True)]
    assert [m["net_displacement"] for m in effect["measurements"]] == [pytest.approx(6.0), pytest.approx(0.05)]
    assert [(m["from_inspection_seq"], m["to_inspection_seq"], m["window_game_s"]) for m in effect["measurements"]] == [
        (1, 2, 1.0), (3, 4, 1.0)]
    assert "0.050 world units" in attempt["reason"] and "calibrated 6.040" in attempt["reason"]
    assert attempt["baseline"] is None and attempt["readings"] == []             # no reader was read
    assert [row["action"] for row in attempt["inputs"]] == ["move", "look", "move"]   # every input replayed once, in order


def test_the_scored_execution_decides_not_any_execution_of_the_action(tmp_path):
    plan = _effect_plan(tmp_path)
    # The first move carried the entity 0.1 (it satisfies the check); the scored, last move carried it 6.0 (it does not).
    attempt = _run_effect(plan, FakeClient(["completed"] * 3), FakeSnapshots(_at(0, 1), _at(0.1, 2), _at(0.1, 3), _at(6.1, 4)))
    assert attempt["valid"] and attempt["result"] == "not_reproduced"
    assert [m["satisfied"] for m in attempt["effect"]["measurements"]] == [True, False]
    assert attempt["effect"]["measured"] == pytest.approx(6.0)
    assert "does not satisfy the repro_check" in attempt["reason"]
    carried = _run_effect(plan, FakeClient(["completed"] * 3), FakeSnapshots(*CARRIED))
    assert (carried["valid"], carried["result"]) == (True, "not_reproduced")


UNMEASURED = [
    ("after_snapshot_failed", [_at(0, 1), _at(6.0, 2), _at(6.0, 3), ValueError("the selected entity is missing from the transform snapshot")],
     "the selected entity is missing from the transform snapshot"),
    ("before_snapshot_failed", [_at(0, 1), _at(6.0, 2), ValueError("transform snapshot has no objects list"), _at(6.05, 4)],
     "transform snapshot has no objects list"),
    ("position_unknown", [_at(0, 1), _at(6.0, 2), _at(6.0, 3), {"position": None, "inspection_seq": 4, "game_time_s": 4.0}],
     "a position was unknown"),
]


@pytest.mark.parametrize("script, problem", [(s, p) for _, s, p in UNMEASURED], ids=[n for n, _, _ in UNMEASURED])
def test_an_effect_attempt_whose_scored_execution_was_not_measured_is_unknown_never_zero(tmp_path, script, problem):
    # A displacement of zero would satisfy `< 3.0`; an unmeasured one must not.
    attempt = _run_effect(_effect_plan(tmp_path), FakeClient(["completed"] * 3), FakeSnapshots(*script))
    assert (attempt["valid"], attempt["result"]) == (True, "unknown")
    scored = attempt["effect"]["measurements"][-1]
    assert scored["scored"] and scored["net_displacement"] is None and scored["satisfied"] is None
    assert scored["problem"] == problem and attempt["effect"]["measured"] is None
    assert "was not measured" in attempt["reason"] and problem in attempt["reason"]


def test_an_unmeasured_execution_that_is_not_scored_does_not_stop_the_scored_one_counting(tmp_path):
    script = [ValueError("transform snapshot has no objects list"), _at(6.0, 2), _at(6.0, 3), _at(6.05, 4)]
    attempt = _run_effect(_effect_plan(tmp_path), FakeClient(["completed"] * 3), FakeSnapshots(*script))
    assert (attempt["valid"], attempt["result"]) == (True, "reproduced")
    assert attempt["effect"]["measurements"][0]["net_displacement"] is None and attempt["effect"]["measured"] == pytest.approx(0.05)


@pytest.mark.parametrize("op, value, holds", [
    ("<", 10.0, True), ("<=", 6.04, True), (">", 3.0, True), (">=", 6.04, True),    # each holds for the calibrated 6.04
    ("<", 6.04, False), (">", 6.04, False), ("<=", 6.0, False), (">=", 6.05, False),   # none does (a strict bound, at the edge)
])
def test_a_check_the_calibrated_effect_satisfies_cannot_show_a_difference_so_the_attempt_is_unknown(tmp_path, op, value, holds):
    plan = _effect_plan(tmp_path)
    plan["repro_check"] = {"field": "effect.move", "op": op, "value": value, "tolerance": None}
    attempt = _run_effect(plan, FakeClient(["completed"] * 3), FakeSnapshots(*STOPPED))
    assert attempt["effect"]["holds_for_expected"] is holds
    assert attempt["valid"] is True
    if holds:
        assert attempt["result"] == "unknown"
        assert "holds for the calibrated effect (6.040 world units), so the replay cannot show a difference" in attempt["reason"]
        assert attempt["effect"]["measured"] == pytest.approx(0.05)         # what was measured is still kept
    else:
        assert attempt["result"] in ("reproduced", "not_reproduced")        # the measurement decides


def test_an_effect_attempt_takes_a_snapshot_before_the_observation_and_after_the_record_for_the_action_only(tmp_path):
    events: list[str] = []
    client = LoggedClient(["completed"] * 3, events)
    snapshots = FakeSnapshots(*STOPPED, events=events)
    attempt = _run_effect(_effect_plan(tmp_path), client, snapshots)
    assert attempt["result"] == "reproduced" and snapshots.calls == 4
    # The reading before an input is never taken between observing and acting, so it adds nothing to the act's age.
    assert events == ["snapshot", "observe", "act move", "snapshot",
                      "observe", "act look",
                      "snapshot", "observe", "act move", "snapshot"]
    assert [kw["lease_s"] for kw in client.sent] == [0.8, 0.25, 0.8]


def test_a_rejected_replay_input_diverges_an_effect_attempt_and_it_is_never_counted(tmp_path):
    snapshots = FakeSnapshots(_at(0, 1))
    attempt = _run_effect(_effect_plan(tmp_path), FakeClient(["rejected"]), snapshots)
    assert attempt["valid"] is False and attempt["result"] is None
    assert "diverged at turn 0" in attempt["reason"] and "stale_age" in attempt["reason"]
    assert attempt["effect"]["measurements"] == [] and snapshots.calls == 1      # nothing sent, so nothing to measure
    later = _run_effect(_effect_plan(tmp_path / "b"), FakeClient(["completed", "rejected"]),
                        FakeSnapshots(_at(0, 1), _at(6.0, 2)))
    assert later["valid"] is False and "diverged at turn 1" in later["reason"] and len(later["effect"]["measurements"]) == 1


def test_an_effect_attempt_needs_a_snapshot_source_and_measure_says_so_before_any_reset(tmp_path):
    plan = _effect_plan(tmp_path)
    resets: list[int] = []

    async def reload(n):
        resets.append(n)
        return {"number": n}

    async def connect():
        return FakeClient([])

    with pytest.raises(ValueError, match="needs a position snapshot source"):
        asyncio.run(reproduce.measure(plan, reload, connect, _no_readers))
    assert resets == []
    attempt = asyncio.run(reproduce.run_attempt(plan, 1, reload, connect, _no_readers))
    assert attempt["valid"] is False and "needs a position snapshot source" in attempt["error"]


def test_measure_reports_k_of_n_for_an_effect_check_and_moves_the_finding_on_its_own_check(tmp_path):
    plan = _effect_plan(tmp_path, attempts=3)
    sources = iter([FakeSnapshots(*STOPPED), FakeSnapshots(*STOPPED), FakeSnapshots(*CARRIED)])

    async def reload(n):
        return {"number": n}

    async def connect():
        return FakeClient(["completed"] * 3)

    report = asyncio.run(reproduce.measure(plan, reload, connect, _no_readers, lambda _client: next(sources)))
    summary = report["summary"]
    assert report["status"] == "measured" and (summary["k"], summary["n"], summary["unknown"]) == (2, 3, 0)
    assert summary["basis"] == reproduce.EFFECT_BASIS and "measured net displacement" in summary["basis"]
    assert report["effect"] == plan["effect"] and report["model_calls"] == 0
    assert [a["effect"]["measured"] for a in report["attempts"]] == [pytest.approx(0.05), pytest.approx(0.05), pytest.approx(6.0)]
    update = Finding.model_validate(report["finding_update"])
    assert update.state is FindingState.reproduced and (update.repro.attempts, update.repro.reproduced) == (3, 2)
    assert "net displacement of one execution of move between two position snapshots" in update.repro.tolerance
    json.dumps(report, allow_nan=False)                                          # the report is plain JSON data


def test_a_check_that_holds_for_the_calibrated_effect_is_unknown_in_every_attempt_and_moves_nothing(tmp_path):
    plan = _effect_plan(tmp_path, attempts=2)
    plan["repro_check"] = {"field": "effect.move", "op": "<", "value": 10.0, "tolerance": None}
    sources = iter([FakeSnapshots(*STOPPED), FakeSnapshots(*STOPPED)])

    async def reload(n):
        return {"number": n}

    async def connect():
        return FakeClient(["completed"] * 3)

    report = asyncio.run(reproduce.measure(plan, reload, connect, _no_readers, lambda _client: next(sources)))
    assert (report["summary"]["k"], report["summary"]["n"], report["summary"]["unknown"]) == (0, 0, 2)
    assert report["status"] == "inconclusive" and report["finding_update"] is None


class StuckGame(TransformsGame):
    """A fake world whose `increment` completes and releases but no longer moves the entity: the action lost its effect."""

    def _apply(self, request, seq, session):
        return super(TransformsGame, self)._apply(request, seq, session)      # the record, without the move


def _replay_against(game_class, tmp_path: Path, check: dict[str, Any], entity: str = "entity") -> dict[str, Any]:
    """The whole replay of two attempts, through the real client and the real snapshot source, against a fake bridge."""
    executions = [("move", "completed", 1000.0, 0.2, None), ("look", "completed", 1001.0, 0.25, None),
                  ("move", "completed", 1002.0, 0.2, None)]
    plan = reproduce.build_plan(_effect_trace(tmp_path, check=check, executions=executions),
                                _effect_manifest(tmp_path, move_hold=0.2), attempts=2)
    plan["start_settle_s"] = 0.0
    for item in plan["inputs"]:
        item["offset_s"] = item["offset_s"] / 100

    async def reload(n):
        return {"number": n}

    async def main():
        # The fake world runs `increment` for both names: it is the behavior that completes with its release verified.
        game = game_class(token=TOKEN, aliases={"move": "increment", "look": "increment"})
        port = await game.start()

        async def connect():
            client = BridgeClient("127.0.0.1", port, TOKEN)
            await client.connect()
            return client

        try:
            return await reproduce.measure(plan, reload, connect, _no_readers,
                                           lambda client: make_snapshot(client, entity))
        finally:
            await game.stop()

    return asyncio.run(main())


def test_against_a_fake_bridge_an_action_that_lost_its_effect_reproduces_and_a_working_one_does_not(tmp_path):
    stuck = _replay_against(StuckGame, tmp_path / "stuck", EFFECT_CHECK)           # effect.move < 3.0
    assert (stuck["summary"]["k"], stuck["summary"]["n"], stuck["status"]) == (2, 2, "measured")
    assert [a["effect"]["measured"] for a in stuck["attempts"]] == [pytest.approx(0.0)] * 2
    assert stuck["finding_update"]["state"] == "reproduced"
    working = _replay_against(TransformsGame, tmp_path / "working", EFFECT_CHECK)
    assert (working["summary"]["k"], working["summary"]["n"]) == (0, 2)
    assert [a["effect"]["measured"] for a in working["attempts"]] == [pytest.approx(6.0)] * 2
    assert working["finding_update"]["state"] == "repro_attempted"
    measurements = [m for a in working["attempts"] for m in a["effect"]["measurements"]]
    assert len(measurements) == 4 and all(m["problem"] is None for m in measurements)         # each move, both attempts
    assert all(m["to_inspection_seq"] > m["from_inspection_seq"] for m in measurements)     # two real inspections each


def test_against_a_fake_bridge_an_entity_that_cannot_be_found_leaves_every_attempt_unknown_not_zero(tmp_path):
    report = _replay_against(StuckGame, tmp_path, EFFECT_CHECK, entity="no-such-entity")    # zero would satisfy < 3.0
    assert (report["summary"]["k"], report["summary"]["n"], report["summary"]["unknown"]) == (0, 0, 2)
    assert report["status"] == "inconclusive" and report["finding_update"] is None
    for attempt in report["attempts"]:
        assert attempt["effect"]["measured"] is None
        assert "the selected entity is missing from the transform snapshot" in attempt["reason"]


def test_the_plan_command_writes_the_effect_plan_and_says_what_it_scores(tmp_path, capsys):
    trace, manifest = _effect_trace(tmp_path), _effect_manifest(tmp_path)
    output = tmp_path / "out" / "plan.json"
    assert reproduce.main(["plan", "--trace", str(trace), "--manifest", str(manifest), "--output", str(output)]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["effect"]["action"] == "move" and [i.get("scored", False) for i in written["inputs"]] == [False, False, True]
    printed = capsys.readouterr().out
    assert "effect: move calibrated 6.040 world units" in printed and "scored at turn 2 of 2 execution(s)" in printed
    check = tmp_path / "check.json"                                              # a refusal is reported and writes nothing
    check.write_text(json.dumps({"field": "effect.use", "op": "<", "value": 1}), encoding="utf-8")
    refused = tmp_path / "refused" / "plan.json"
    assert reproduce.main(["plan", "--trace", str(trace), "--manifest", str(manifest), "--repro-check", str(check),
                           "--output", str(refused)]) == 1
    assert "no calibrated expected displacement" in capsys.readouterr().err and not refused.exists()
