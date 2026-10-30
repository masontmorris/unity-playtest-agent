"""`reproduce` (#38): plan from a finished trace, replay once per attempt after a reset, and count k of n honestly.

No Unity and no model: a synthetic trace, a fake bridge client and fake readers stand in. What is checked is the
bookkeeping a report will rest on: which inputs replay, at what offsets, how each reading is scored, and that a
diverged or unscoreable attempt never counts as a reproduction or as a miss.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from datetime import datetime, timezone

from runner_manifest import fixture
from playtest.contracts.investigation import Finding, FindingState
from playtest.contracts.manifest import PreflightManifest
from playtest.runner import reproduce


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(PreflightManifest.model_validate(fixture()).model_dump_json(), encoding="utf-8")
    return path


def _utc(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat()


def _execution(seq: int, turn: int, action: str, status: str, start: float | None, lease: float = 0.4,
               purpose: str = "choice") -> dict[str, Any]:
    record = {"status": status, "input_start_wall_s": start, "applied_duration_s": lease if status == "completed" else None,
              "release_verified": True if status == "completed" else None,
              "reject_reason": "stale_age" if status == "rejected" else None}
    return {"trace_seq": seq, "kind": "execution", "turn": turn,
            "body": {"purpose": purpose, "request": {"action": action, "choice_id": action, "params": {}, "lease_s": lease},
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
