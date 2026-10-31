"""Offline pointing geometry, measured compositions, catalog binding and runtime validity."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from runner_manifest import manifest, action, vector
from test_turn_sizes import measurement, record
from test_speed_finish_options import PlanFiles, plan_run
from playtest.preflight.catalog import export, serialize
from playtest.runner.choices import build_choices, with_held_choices
from playtest.runner.catalog import verify_host_catalog
from playtest.runner.point_turns import strip_angles, compose, measured_fov, pointing_turns, gate_choices
from playtest.runner.turn_sizes import measured_turns
from playtest.runner import cli


def sample():
    m = manifest(*[action(name, vector(device, control, sign, 0), hold_s=.1,
        holds=[measurement(h, yaw) for h, yaw in rows]) for name, device, control, sign, rows in (
        ('look', 'Gamepad', 'rightStick', 1, [( .05,18.27),(.1,36.25),(.2,72.2),(.4,144.22)]),
        ('left', 'Gamepad', 'rightStick', -1, [(.05,-18.17),(.1,-36.18),(.2,-72.23),(.4,-144.34)]),
        ('mouse', 'Mouse', 'delta', 1, [(.05,7.68)]),
        ('mouseleft', 'Mouse', 'delta', -1, [(.05,-7.68)]))])
    r = record(m)
    r['clock'] = {'resume': {'public': {'camera': {'field_of_view': 65.0}}}}
    return m, r


def test_geometry_and_exact_sequences():
    angles = strip_angles(65)
    assert list(angles) == [1,2,3,4,6,7,8,9]
    assert list(angles.values()) == pytest.approx([-37.05449178,-29.52221286,-20.68268099,-10.68946642,
                                                   10.68946642,20.68268099,29.52221286,37.05449178])
    assert strip_angles(65, (1280,960)) == angles
    assert strip_angles(65, (640,360)) != angles
    m, r = sample()
    measured = list(measured_turns(m,r,include_yaw=True))
    expected = [(4,4), (4,4,8), (5,8,8), (4,8), (0,9), (1,9,9), (0,0,9), (0,0)]
    assert [compose(a,measured) for a in angles.values()] == expected
    assert compose(36, [(None,None,18), (None,None,36)]) == (1,)


def test_only_measured_steps_and_search_turns():
    m,r = sample()
    rows = pointing_turns(m,r)
    allowed = {c.id:d for c,d in measured_turns(m,r)}
    for row in rows:
        assert 1 <= len(row.sequence) <= 3
        for i,(name,step) in enumerate(zip(row.sequence,row.definition.steps)):
            assert step.inputs == allowed[name].inputs
            assert step.hold_s == allowed[name].default_lease_s
            assert step.wait_s == (.1 if i < len(row.sequence)-1 else 0)
        assert row.definition.max_s == sum(s.hold_s+s.wait_s for s in row.definition.steps) + 1.0
    assert [(r.choice.id,r.total) for r in rows[-2:]] == [('point.search_left',-144.34),('point.search_right',144.22)]


@pytest.mark.parametrize('value', [None, False, '65', float('nan'), 64.98, 65.02])
def test_fov_gate(value):
    m,r = sample()
    b = build_choices(m,point_turns=True,calibration=r)
    obs = SimpleNamespace(public={'camera':{'field_of_view':value}})
    offered = gate_choices(b.choices,obs,b.point_fov)
    assert offered == [c for c in b.choices if not c.id.startswith('point.strip')]
    assert len([c for c in offered if c.id.startswith('point.search')]) == 2
    for value in (65,64.99,65.01):
        obs.public['camera']['field_of_view'] = value
        assert gate_choices(b.choices,obs,b.point_fov) == b.choices
    assert with_held_choices(b,[]).point_fov == 65
    for public in ({}, {'camera':None}, {'camera':'unknown'}):
        assert gate_choices(b.choices,SimpleNamespace(public=public),65) == offered


def test_missing_conflicting_fov_and_exclusion(tmp_path,monkeypatch):
    m,r = sample()
    with pytest.raises(ValueError,match='measured camera field_of_view'):
        build_choices(m,point_turns=True,calibration=record(m))
    r['clock']['pause'] = {'observation':{'public':{'camera':{'field_of_view':60}}}}
    with pytest.raises(ValueError,match='consistent'):
        measured_fov(r)
    with pytest.raises(ValueError,match='mutually exclusive'):
        export(m,turn_sizes=True,point_turns=True)
    with pytest.raises(ValueError,match='mutually exclusive'):
        build_choices(m,turn_sizes=True,point_turns=True)
    monkeypatch.setenv('UPA_RUN_TURN_SIZES','1')
    with pytest.raises(SystemExit):
        cli.main(['--point-turns'])


def test_catalog_hash_and_off_bytes(tmp_path):
    m,r = sample()
    b = build_choices(m,point_turns=True,calibration=r)
    assert b.catalog == export(m,point_turns=True,calibration=r)
    assert all(b.catalog.actions[c.skill].is_skill for c in b.choices if c.id.startswith('point.'))
    path = tmp_path/'catalog.json'
    path.write_text(json.dumps(serialize(b.catalog)))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert verify_host_catalog(m,path,sha,point_turns=True,calibration=r).catalog == b.catalog
    with pytest.raises(ValueError,match="not the manifest's export"):
        verify_host_catalog(m,path,sha,point_turns=True,calibration=r,capture_size=(640,360))
    path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='SHA-256'):
        verify_host_catalog(m,path,sha,point_turns=True,calibration=r)
    def encode(b):
        return json.dumps({'choices':[c.model_dump(mode='json') for c in b.choices],
                          'catalog':serialize(b.catalog),'leases':b.leases,'expected':b.expected})
    assert encode(build_choices(m)) == encode(build_choices(m,point_turns=False,calibration=r))
    assert serialize(export(m)) == serialize(export(m,point_turns=False,calibration=r))


def test_plan_key(tmp_path,monkeypatch,plan_run):
    assert 'point_turns' not in plan_run()
    async def stub(*args,**kwargs):
        return {'status':'complete','turns':1,'ending':'finish'}
    monkeypatch.setattr(cli,'_run',stub)
    files = PlanFiles(tmp_path/'point')
    m,r = sample()
    files.manifest.write_text(m.model_dump_json())
    files.calibration.write_text(json.dumps(r))
    files.catalog.write_text(json.dumps(serialize(export(m,point_turns=True,calibration=r))))
    files.catalog_sha = hashlib.sha256(files.catalog.read_bytes()).hexdigest()
    monkeypatch.setenv('UPA_RUN_POINT_TURNS','1')
    assert cli.main(files.args()) == 0
    assert json.loads((files.out/'plan.json').read_text())['point_turns'] is True


@pytest.mark.parametrize('fov,executes', [(65,True),(60,False),(None,False)])
def test_runtime_gate_and_exact_selected_skill(fov,executes):
    from test_runner_session import connected,closed,session,CatalogGame,FakeS1,harness_trace
    async def run():
        m,r = sample()
        b = build_choices(m,point_turns=True,calibration=r)
        game,client = await connected(CatalogGame,catalog=b.catalog)
        original = client.observe_once
        async def observe():
            obs = await original()
            return obs.model_copy(update={'public':{**obs.public,'camera':{'field_of_view':fov}}})
        client.observe_once = observe
        sink = harness_trace()
        try:
            result = await session(client,b,s1=FakeS1('point.strip3','finish'),sink=sink,objective='Exercise controls.')
            requests = [e['body']['request'] for e in sink.of_kind('execution')]
            assert len(requests) == int(executes)
            if executes:
                assert requests[0]['action'] == 'point_strip3'
                assert result.loop.turns[0].executed_action == 'point.strip3'
            else:
                assert result.loop.turns[0].executed_action is None
            assert result.adherence == []
        finally:
            await closed(game,client)
    asyncio.run(run())
