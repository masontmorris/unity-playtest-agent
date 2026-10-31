"""The operator scripted-hold probe: it sends what the script says, once, and reports what moved."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from playtest.bench import scripted


def _script(tmp_path: Path, holds: list[dict], **extra) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"schema_version": 1, "holds": holds, **extra}), encoding="utf-8")
    return path


def _hold(action: str = "preflight_key_w", lease: float = 0.4) -> dict:
    return {"action": action, "lease_s": lease}


class FakeClient:
    def __init__(self, statuses: list[str] | None = None, probe: dict | None = ...) -> None:
        self.statuses, self.sent, self.observes, self.closed = list(statuses or []), [], 0, False
        self.probe = ({"version": 1, "anchor": {"kind": "path", "name": "Anchor", "position": [0.0, 0.08, -27.0]}}
                      if probe is ... else probe)
        self.seq = 0

    async def observe_once(self):
        self.observes += 1
        self.seq += 1
        public = {"camera": {"position": [0.0, 1.78, 0.0]}}
        if self.probe is not None:
            public["probe"] = self.probe
        return SimpleNamespace(seq=self.seq, frame=10 * self.seq, game_s=float(self.seq),
                               scene="Assets/Main.unity", playing=True, paused=False,
                               public=public, view=None)

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


class Snapshots:
    """Each call yields the next reading in order, so a test states exactly what was seen when."""

    def __init__(self, *readings: dict) -> None:
        self.readings = list(readings)

    async def __call__(self) -> dict:
        return self.readings.pop(0)


def _at(x: float, seq: int) -> dict:
    return {"position": [x, 0.0, -27.0], "world_forward": [1.0, 0.0, 0.0], "yaw_deg": 90.0,
            "game_time_s": float(seq), "inspection_seq": seq}


def _run(script: dict, client: FakeClient, snapshots: Snapshots) -> dict:
    return asyncio.run(scripted.drive(client, snapshots, script))


def test_a_script_needs_holds_with_a_positive_lease(tmp_path):
    script, sha = scripted.load_script(_script(tmp_path, [_hold("a", 0.4), _hold("b", 1.0)]))
    assert [h["action"] for h in script["holds"]] == ["a", "b"] and len(sha) == 64
    assert script["settle_s"] == scripted.DEFAULT_SETTLE_S and script["max_age_s"] == scripted.DEFAULT_MAX_AGE_S
    with pytest.raises(ValueError, match="nonempty holds"):
        scripted.load_script(_script(tmp_path, []))
    with pytest.raises(ValueError, match="nonempty action"):
        scripted.load_script(_script(tmp_path, [_hold("  ", 0.4)]))
    with pytest.raises(ValueError, match="lease_s must be positive"):
        scripted.load_script(_script(tmp_path, [_hold("a", 0.0)]))
    with pytest.raises(ValueError, match="finite number"):
        scripted.load_script(_script(tmp_path, [_hold("a", True)]))
    with pytest.raises(ValueError, match="schema_version"):
        path = tmp_path / "old.json"
        path.write_text(json.dumps({"holds": [_hold()]}), encoding="utf-8")
        scripted.load_script(path)
    with pytest.raises(ValueError, match="negative"):
        scripted.load_script(_script(tmp_path, [_hold()], settle_s=-1))


def test_each_hold_is_sent_once_in_order_bound_to_a_fresh_observation(tmp_path):
    script, _ = scripted.load_script(_script(tmp_path, [_hold("a", 0.4), _hold("b", 1.0)], settle_s=0.0))
    client = FakeClient()
    report = _run(script, client, Snapshots(_at(16.5, 1), _at(18.5, 2), _at(18.5, 3), _at(18.75, 4)))
    assert report["status"] == "complete" and report["model_calls"] == 0 and report["is_s1_turn"] is False
    assert [(s["action"], s["lease_s"]) for s in client.sent] == [("a", 0.4), ("b", 1.0)]
    assert [s["based_on_seq"] for s in client.sent] == [1, 3]      # a fresh observation before each hold
    assert len({s["action_id"] for s in client.sent}) == 2
    first, second = report["holds"]
    assert first["net_displacement"] == pytest.approx(2.0)
    assert second["net_displacement"] == pytest.approx(0.25)
    assert first["before"]["yaw_deg"] == 90.0 and first["after"]["inspection_seq"] == 2
    assert first["closing_observation"]["camera"]["position"] == [0.0, 1.78, 0.0]
    assert first["observation"]["probe"] == client.probe          # the geometry field travels with the report
    assert second["closing_observation"]["probe"] == client.probe


def test_a_game_without_the_geometry_field_leaves_it_null(tmp_path):
    script, _ = scripted.load_script(_script(tmp_path, [_hold("a")], settle_s=0.0))
    report = _run(script, FakeClient(probe=None), Snapshots(_at(0, 1), _at(2, 2)))
    row = report["holds"][0]
    assert row["observation"]["probe"] is None and row["closing_observation"]["probe"] is None
    assert row["net_displacement"] == pytest.approx(2.0)          # the hold is still measured without it


def test_a_rejected_hold_stops_the_run_and_nothing_is_retried(tmp_path):
    script, _ = scripted.load_script(_script(tmp_path, [_hold("a"), _hold("b"), _hold("c")], settle_s=0.0))
    client = FakeClient(["completed", "rejected", "completed"])
    report = _run(script, client, Snapshots(_at(0, 1), _at(2, 2), _at(2, 3), _at(2, 4)))
    assert report["status"] == "incomplete"
    assert "hold 2 (b) ended rejected (stale_age)" in report["reason"]
    assert [s["action"] for s in client.sent] == ["a", "b"]
    assert len(report["holds"]) == 2 and report["holds"][-1]["reject_reason"] == "stale_age"


def test_an_unmeasurable_reading_is_null_never_zero(tmp_path):
    script, _ = scripted.load_script(_script(tmp_path, [_hold("a")], settle_s=0.0))
    unknown = {"position": None, "inspection_seq": 5, "game_time_s": 5.0}
    report = _run(script, FakeClient(), Snapshots(_at(0, 1), unknown))
    row = report["holds"][0]
    assert report["status"] == "complete"
    assert row["net_displacement"] is None and row["unit"] == "world units"
    assert row["before"]["position"] == [0.0, 0.0, -27.0] and row["after"]["position"] is None
    assert row["problem"] == "a position was unknown"


def test_the_entity_record_names_what_is_measured():
    assert scripted.entity_id_of({"entity_id": "scene:Assets/Main.unity@1::5"}) == "scene:Assets/Main.unity@1::5"
    with pytest.raises(ValueError, match="no entity_id"):
        scripted.entity_id_of({})
