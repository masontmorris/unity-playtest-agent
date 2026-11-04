"""A replay can score only the executions made inside a reach region, and replays only the last life."""

from __future__ import annotations

import asyncio
import json

import pytest

from test_reproduce import FakeClient, FakeReaders, FakeSnapshots, _at, _execution, _manifest, _trace
from playtest.contracts.investigation import ReproCheck
from playtest.runner import reproduce

CHECK = {"field": "public.speed", "op": "unchanged", "value": None, "after_action": "move",
         "within_region": "situation_1"}


def _regions(folder, centre_x=5.0):
    (folder / "situation-regions.json").write_text(json.dumps({"regions": [
        {"region_id": "situation_1", "bounds": {"min": [centre_x - 1, -1, -1], "max": [centre_x + 1, 1, 1]}}]}),
        encoding="utf-8")


def _plan(tmp_path):
    trace = _trace(tmp_path, check=CHECK)
    _regions(tmp_path)
    plan = reproduce.build_plan(trace, _manifest(tmp_path), attempts=1)
    plan.update(start_settle_s=0.0, settle_s=0.02, sample_interval_s=0.01)
    for item in plan["inputs"]:
        item["offset_s"] = item["offset_s"] / 100
    return plan


def _run(plan, client, readers, snapshots):
    async def reload(n):
        return {"number": n}

    async def connect():
        return client

    return asyncio.run(reproduce.run_attempt(plan, 1, reload, connect, lambda _c: readers, lambda _c: snapshots))


def test_a_region_needs_the_action_it_conditions():
    with pytest.raises(ValueError, match="name the action"):
        ReproCheck(field="public.speed", op="unchanged", within_region="situation_1")
    assert "within_region" not in ReproCheck(field="public.speed", op="unchanged",
                                             after_action="move").model_dump(mode="json")


def test_the_plan_takes_the_region_the_session_saved_beside_its_trace_and_refuses_an_unknown_one(tmp_path):
    plan = _plan(tmp_path)
    assert plan["region"] == {"region_id": "situation_1", "min": [4.0, -1.0, -1.0], "max": [6.0, 1.0, 1.0],
                              "source": "situation-regions.json"}
    with pytest.raises(ValueError, match="neither a manifest reach region"):
        reproduce.build_plan(_trace(tmp_path / "other", check=CHECK | {"within_region": "nowhere"}),
                             _manifest(tmp_path))


def test_only_the_executions_made_inside_the_region_are_scored(tmp_path):
    plan = _plan(tmp_path)
    # The first move is made far from the region, the second inside it. Reads: the baseline, the reading after the
    # first (not scored), then before and after the second.
    attempt = _run(plan, FakeClient([]), FakeReaders([0.0, 0.0, 0.0, 0.0]), FakeSnapshots(_at(0.0, 1), _at(5.0, 2)))
    assert attempt["region_presses"] == [{"turn": 0, "inside": False, "position": [0.0, 0.0, 0.0]},
                                         {"turn": 2, "inside": True, "position": [5.0, 0.0, 0.0]}]
    assert [r.get("after_action") for r in attempt["readings"]] == [None, "move"]
    assert attempt["result"] == "reproduced"
    # The press inside it changed the reader, as on a working build: not reproduced, whatever the far one did.
    attempt = _run(plan, FakeClient([]), FakeReaders([0.0, 0.0, 0.0, 5.0]), FakeSnapshots(_at(0.0, 1), _at(5.0, 2)))
    assert attempt["result"] == "not_reproduced"


def test_no_execution_inside_the_region_leaves_the_attempt_unknown(tmp_path):
    plan = _plan(tmp_path)
    attempt = _run(plan, FakeClient([]), FakeReaders([0.0, 0.0, 0.0]), FakeSnapshots(_at(0.0, 1), ValueError("gone")))
    assert [p["inside"] for p in attempt["region_presses"]] == [False, None]
    assert attempt["result"] == "unknown" and "inside the check's region" in attempt["reason"]


def test_a_restarted_session_is_replayed_from_its_last_restart_before_the_evidence(tmp_path):
    check = {"field": "public.speed", "op": ">=", "value": 9, "tolerance": None}
    trace = _trace(tmp_path, check=check)
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    # A restart between the first move (seq 2) and the second (seq 6): only the second is this life's.
    rows.insert(3, {"trace_seq": 2.5, "kind": "restart", "turn": None, "body": {}})
    trace.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    plan = reproduce.build_plan(trace, _manifest(tmp_path))
    assert [item["trace_seq"] for item in plan["inputs"]] == [6]
    assert plan["inputs"][0]["offset_s"] == 0.0 and plan["skipped_inputs"]["before_last_restart"] == 1
