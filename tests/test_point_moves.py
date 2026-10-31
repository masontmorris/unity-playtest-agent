"""Offline measured point-move selection, derivation, binding, and execution."""
import asyncio
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from runner_manifest import action, manifest
from test_point_turns import sample as turns_sample
from test_speed_finish_options import PlanFiles, plan_run
from playtest.preflight.catalog import export, serialize, main as export_main
from playtest.runner import cli
from playtest.runner.catalog import verify_host_catalog
from playtest.runner.choices import build_choices
from playtest.runner.point_moves import forward_step, pointing_moves
from playtest.runner.point_turns import gate_choices


def trial(control="q", hold=.4, distance=2, angle=0, yaw=75, **updates):
    radians = math.radians(yaw + angle)
    row = dict(control=control, requested_hold_s=hold, valid=True, declared_x=None, declared_y=None,
               effect_delta=[distance * math.sin(radians), 0, distance * math.cos(radians)],
               view_before={"yaw_deg": yaw}, view_after={"yaw_deg": yaw})
    return {**row, **updates}


def sample():
    turns, r = turns_sample()
    m = manifest(*(a.model_dump(mode="json") for a in turns.actions), action("arbitrary", "q", hold_s=.4))
    r["entity_id"] = m.entity_id
    r["trials"] = [trial(), trial(yaw=-130)]
    return m, r


def test_selection_measured_frame_not_names_or_world_average():
    m, r = sample()
    selected = forward_step(m, r)
    assert selected.action == "arbitrary"
    assert selected.displacement == pytest.approx(2)
    assert selected.direction_deg == pytest.approx(0, abs=1e-12)
    assert selected.definition.default_lease_s == .4
    for t in r["trials"]:
        t["raw_delta"] = t.pop("effect_delta")
    assert forward_step(m, r) == selected


@pytest.mark.parametrize("update", [dict(effect_delta=None), dict(effect_delta=[0,0,0]),
    dict(effect_delta=[0,2,0]), dict(view_before={}), dict(view_after={}),
    dict(view_before={"yaw_deg": float("nan")}), dict(valid=False), dict(declared_x=1),
    dict(requested_hold_s=.2), dict(effect_delta=[float("inf"),0,1])])
def test_refusal_missing_or_nonforward_measurements(update):
    m, r = sample()
    r["trials"] = [trial(**update), trial(**update)]
    with pytest.raises(ValueError, match="measured forward step"):
        forward_step(m, r)


def test_angle_boundary_and_unknown_spread():
    m, r = sample()
    for angle in (-20, 20):
        r["trials"] = [trial(angle=angle), trial(angle=angle)]
        assert forward_step(m, r).direction_deg == pytest.approx(angle)
    for trials in ([trial(angle=20.01)] * 2, [trial()], []):
        r["trials"] = trials
        with pytest.raises(ValueError, match="measured forward step"):
            forward_step(m, r)


def test_spread_then_hold_then_manifest_order():
    m, r = sample()
    m = manifest(action("first", "q", hold_s=.4), action("shorter", "e", hold_s=.1))
    r["entity_id"] = m.entity_id
    r["trials"] = [trial(yaw=0), trial(yaw=0), trial("e", .1, 1, yaw=0), trial("e", .1, 1.1, yaw=0)]
    assert forward_step(m, r).action == "first"  # less spread beats shorter hold
    r["trials"][-1] = trial("e", .1, 1, yaw=0)
    assert forward_step(m, r).action == "shorter"
    m = manifest(action("z_first", "q", hold_s=.4), action("a_second", "e", hold_s=.4))
    r["trials"] = [trial(yaw=0)] * 2 + [trial("e", yaw=0)] * 2
    assert forward_step(m, r).action == "z_first"


def test_exact_nine_sequences_and_search():
    m, r = sample()
    rows = pointing_moves(m, r)
    from playtest.runner.point_turns import pointing_turns
    turns = {t.choice.id:t for t in pointing_turns(m, r)}
    from playtest.runner.turn_sizes import measured_turns
    measured = list(measured_turns(m, r, include_yaw=True))
    indices = [(4,4), (4,4,8), (5,8,8), (4,8), (), (0,9), (1,9,9), (0,0,9), (0,0)]
    for row, sequence in zip(rows[:9], indices):
        assert row.sequence == (*[measured[i][0].id for i in sequence], "arbitrary")
        assert [(s.inputs,s.hold_s,s.wait_s) for s in row.definition.steps[:-1]] == [
            (measured[i][1].inputs, measured[i][1].default_lease_s, .1) for i in sequence]
    for k, row in enumerate(rows[:9], 1):
        assert row.choice.id == f"move.strip{k}"
        assert row.choice.skill == f"move_strip{k}"
        turn = turns.get(f"point.strip{k}")
        assert row.target == (turn.target if turn else 0)
        assert row.total == (turn.total if turn else 0)
        expected_steps = [s.model_copy(update={"wait_s": .1}) for s in turn.definition.steps] if turn else []
        from playtest.contracts.action_catalog import ActionStep
        expected_steps.append(ActionStep(inputs=export(m).actions["arbitrary"].inputs, hold_s=.4, wait_s=0))
        assert row.definition.steps == expected_steps
        assert row.definition.max_s == sum(s.hold_s+s.wait_s for s in expected_steps) + 1
        assert row.choice.label.endswith("step forward 2.0 units")
    assert rows[-2:] == list(turns.values())[-2:]  # search stays turn-only
    assert len(rows[4].definition.steps) == 1


@pytest.mark.parametrize("fov", [None, False, "65", float("nan"), 60, 64.98, 65.02])
def test_fov_always_offers_centre_and_search(fov):
    m, r = sample()
    b = build_choices(m, point_moves=True, calibration=r)
    offered = gate_choices(b.choices, SimpleNamespace(public={"camera":{"field_of_view":fov}}), b.point_fov)
    assert [c.id for c in offered if c.id.startswith("move.")] == ["move.strip5"]
    assert len([c for c in offered if c.id.startswith("point.search_")]) == 2
    for value in (65, 64.99, 65.01):
        assert gate_choices(b.choices, SimpleNamespace(public={"camera":{"field_of_view":value}}),65) == b.choices


@pytest.mark.parametrize("other", ["point_turns", "turn_sizes"])
def test_mutual_exclusions(other, monkeypatch, capsys, tmp_path):
    m, r = sample()
    for fn in (build_choices, export):
        with pytest.raises(ValueError, match="mutually exclusive"):
            fn(m, point_moves=True, **{other:True})
    monkeypatch.setenv("UPA_RUN_" + other.upper(), "1")
    files = PlanFiles(tmp_path/"cli")
    with pytest.raises(SystemExit):
        cli.main(files.args("--point-moves"))
    assert "mutually exclusive" in capsys.readouterr().err
    files = PlanFiles(tmp_path/"export")
    assert export_main(["--manifest",str(files.manifest),"--output",str(tmp_path/"new.json"),
                        "--point-moves", "--"+other.replace("_","-")]) == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_catalog_hash_off_bytes_and_plan(tmp_path, monkeypatch, plan_run):
    m, r = sample()
    b = build_choices(m, point_moves=True, calibration=r)
    assert b.catalog == export(m, point_moves=True, calibration=r)
    path = tmp_path/"catalog.json"
    path.write_text(json.dumps(serialize(b.catalog)))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert verify_host_catalog(m,path,sha,point_moves=True,calibration=r).catalog == b.catalog
    with pytest.raises(ValueError,match="not the manifest's export"):
        verify_host_catalog(m,path,sha,point_moves=True,calibration=r,capture_size=(640,360))
    path.write_text(path.read_text()+" ")
    with pytest.raises(ValueError,match="SHA-256"):
        verify_host_catalog(m,path,sha,point_moves=True,calibration=r)
    def encode(b):
        return json.dumps(dict(choices=[c.model_dump(mode="json") for c in b.choices],
                               catalog=serialize(b.catalog),leases=b.leases,expected=b.expected))
    assert encode(build_choices(m)) == encode(build_choices(m,point_moves=False,calibration=r))
    assert serialize(export(m)) == serialize(export(m,point_moves=False,calibration=r))
    assert "point_moves" not in plan_run()
    async def stub(*args, **kwargs):
        return {"status":"complete", "turns":1, "ending":"finish"}
    monkeypatch.setattr(cli,"_run",stub)
    files = PlanFiles(tmp_path/"plan")
    files.manifest.write_text(m.model_dump_json())
    files.calibration.write_text(json.dumps(r))
    files.catalog.write_text(json.dumps(serialize(b.catalog)))
    files.catalog_sha = hashlib.sha256(files.catalog.read_bytes()).hexdigest()
    monkeypatch.setenv("UPA_RUN_POINT_MOVES","1")
    assert cli.main(files.args()) == 0
    assert json.loads((files.out/"plan.json").read_text())["point_moves"] is True


@pytest.mark.parametrize("strip,fov,executes", [(3,65,True),(3,60,False),(5,None,True)])
def test_runtime_executes_only_selected_skill(strip, fov, executes):
    from test_runner_session import connected, closed, session, CatalogGame, FakeS1, harness_trace
    async def run():
        m, r = sample()
        b = build_choices(m,point_moves=True,calibration=r)
        game,client = await connected(CatalogGame,catalog=b.catalog)
        original = client.observe_once
        async def observe():
            obs = await original()
            return obs.model_copy(update={"public":{**obs.public,"camera":{"field_of_view":fov}}})
        client.observe_once = observe
        sink = harness_trace()
        try:
            result = await session(client,b,s1=FakeS1(f"move.strip{strip}","finish"),sink=sink,objective="Exercise controls.")
            requests = [e["body"]["request"] for e in sink.of_kind("execution")]
            assert len(requests) == int(executes)
            assert result.loop.turns[0].executed_action == (f"move.strip{strip}" if executes else None)
            if executes:
                assert requests[0]["action"] == f"move_strip{strip}"
            assert result.adherence == []
        finally:
            await closed(game,client)
    asyncio.run(run())


def test_continuous_walk_released_before_selected_point_skill():
    from loop_fakes import rig, choices
    from fake_game import FakeGame
    from catalog_game import CatalogGame
    from playtest.core.adherence import check_adherence
    async def run():
        m, r = sample()
        row = pointing_moves(m,r)[2]
        class MixedGame(FakeGame):
            _catalog = export(m,point_moves=True,calibration=r)
            _effects = {}
            def _apply(self, request, seq, session):
                if request.action == row.choice.skill:
                    assert not self._held  # release must precede the selected skill
                    return CatalogGame._apply(self, request, seq, session)
                return super()._apply(request, seq, session)
        test = await rig("w", "c3", row.choice.id, game_class=MixedGame,
            choices=[*choices(),row.choice], continuous_input=True, continuous_choices=["c3"],
            game={"action_run_s":.08,"lease_terminals":True},leases={"c3":.08})
        try:
            await test.loop.run_turn()
            await asyncio.sleep(.08)
            await test.loop.run_turn()
            assert test.game._held
            record = await test.loop.run_turn()
            assert record.executed_action == row.choice.id
            assert not test.game._held
            applied = [e for e in test.game.input_log if e["event"] == "applied"]
            assert len(applied) == 2
            assert applied[-1]["action"] == row.choice.skill
            assert not check_adherence(test.loop.turns)
        finally:
            await test.close()
    asyncio.run(run())


def test_launcher_exclusion_and_environment_lifecycle():
    text = Path("scripts/unity/run-session.ps1").read_text()
    assert "[switch]$PointMoves" in text
    assert "($PointMoves -and ($PointTurns -or $TurnSizes))" in text
    assert "'UPA_RUN_POINT_MOVES'" in text
    assert "$env:UPA_RUN_POINT_MOVES = if ($PointMoves)" in text
    assert "'--point-moves', '--calibration'" in text
