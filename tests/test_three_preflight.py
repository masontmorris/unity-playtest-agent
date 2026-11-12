import json
from pathlib import Path

import pytest

from playtest.contracts.action_catalog import ActionCatalog
from playtest.preflight.three import GameDeclarations, ThreeStudy, compile_study, load_study, study_dict
from playtest.preflight.three_session import _dump
from playtest.runner.catalog import verify_host_catalog
from playtest.preflight.catalog import serialize
from test_input_probe import record, receipt


def study(tmp_path):
    readers = {"speed": {"kind": "number", "why": "Speed"}, "unknown": {"kind": "number", "why": "Unknown"},
               "bad": {"why": "May fail"}, "absent": {"why": "May be absent"}}
    catalog_data = {"actions": {"advance": {"backend": "input_system", "inputs": [
        {"device": "Keyboard", "control": "w", "x": None, "y": None}], "default_lease_s": 0.2}}}
    rb, cb = json.dumps({"readers": readers}).encode(), json.dumps(catalog_data).encode()
    import hashlib
    source = {"commit": "a", "dirty": False, "readers_sha256": hashlib.sha256(rb).hexdigest(),
              "catalog_sha256": hashlib.sha256(cb).hexdigest()}
    decl = GameDeclarations(tmp_path, tmp_path / "actions.json", readers, ActionCatalog.model_validate(catalog_data), source, rb, cb)
    execution = record([receipt()]).model_dump(mode="json")
    attempts = []
    for n in range(2):
        attempts.append({"name": "advance", "declaration": decl.catalog.actions["advance"].model_dump(mode="json"),
                         "execution": [execution], "based_on_seq": 1, "action_id": "probe-1",
                         "before_observation": {"session": "s", "seq": 1},
                         "idle_start": {"position": [0, 0, 0], "real_time_s": 1.0},
                         "before_transform": {"position": [0, 0, 0], "real_time_s": 1.5},
                         "after_transform": {"position": [1, 0, 0], "real_time_s": 1.7}, "view": None})
    stopped = {"protocol": "upa/1", "session": "s", "seq": 5, "game_s": 1.0,
               "wall_utc": "2026-01-01T00:00:00Z", "type": "stopped", "released": [], "release_verified": True}
    return ThreeStudy(decl, "s", {"public": {"speed": 3, "unknown": None, "bad": None},
                       "reader_errors": [{"reader": "bad", "error": "failure"}]},
                      {"id": "entity", "parent_id": None, "name_path": ["body"]}, [], attempts, stopped, None,
                      {"query": "", "settle_s": 60})


def test_compile_saved_study_and_catalog_pin(tmp_path):
    source = study(tmp_path)
    saved = tmp_path / "study.json"
    saved.write_bytes(_dump(study_dict(source)))
    loaded = load_study(saved)
    first = compile_study(source)
    second = compile_study(loaded)
    assert first.manifest.model_dump(mode="json") == second.manifest.model_dump(mode="json")
    assert [(r.alias, r.kind) for r in first.manifest.readers] == [("speed", "number"), ("unknown", None)]
    assert {x["id"] for x in first.manifest.selection.unoffered_fields} == {"public.bad", "public.absent"}
    assert first.manifest.actions[0].execution_supported is True
    assert first.manifest.actions[0].hold_effect is True
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_bytes(_dump(serialize(first.catalog)))
    import hashlib
    verify_host_catalog(first.manifest, catalog_path, hashlib.sha256(catalog_path.read_bytes()).hexdigest())


def test_fatal_or_unverified_stop_refuses_publication(tmp_path):
    from dataclasses import replace
    source = study(tmp_path)
    with pytest.raises(ValueError, match="fatal"):
        compile_study(replace(source, fatal_error="disconnected"))
    with pytest.raises(ValueError, match="verified release"):
        compile_study(replace(source, stop={**source.stop, "release_verified": False}))


def test_catalog_caps_are_checked_before_host_start(tmp_path, monkeypatch):
    from playtest.preflight.three import load_declarations
    playtest = tmp_path / "playtest"
    playtest.mkdir()
    (playtest / "readers.json").write_text(json.dumps({"readers": {"state": {"why": "state"}}}))
    definition = {"backend": "input_system", "inputs": [{"device": "Keyboard", "control": "w", "x": None, "y": None}], "default_lease_s": 0.2}
    (playtest / "actions.json").write_text(json.dumps({"actions": {f"action_{i}": definition for i in range(19)}}))
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("cap check must precede source and process work"))
    with pytest.raises(ValueError, match=r"19 actions .*0 skills .*1 readers"):
        load_declarations(tmp_path)


def test_exact_values_and_chords_keep_separate_motion_evidence(tmp_path):
    from dataclasses import replace
    source = study(tmp_path)
    definitions = {
        "forward": {"backend": "input_system", "inputs": [{"device": "Gamepad", "control": "leftStick", "x": 0.0, "y": 1.0}], "default_lease_s": 0.6},
        "back": {"backend": "input_system", "inputs": [{"device": "Gamepad", "control": "leftStick", "x": 0.0, "y": -1.0}], "default_lease_s": 0.6},
        "chord": {"backend": "input_system", "inputs": [{"device": "Gamepad", "control": "leftStick", "x": 0.0, "y": 1.0}, {"device": "Gamepad", "control": "leftShoulder", "x": None, "y": None}], "default_lease_s": 0.6},
    }
    catalog = ActionCatalog.model_validate({"actions": definitions})
    attempts = []
    for name, direction, repeats in [("forward", 1, 2), ("back", -1, 1), ("chord", 1, 2)]:
        receipts = [receipt(device="Gamepad", control="leftStick", x=0, y=direction, duration=0.6)]
        if name == "chord":
            receipts.append(receipt(device="Gamepad", control="leftShoulder", duration=0.6))
        for _ in range(repeats):
            trial = dict(source.attempts[0], name=name, declaration=catalog.actions[name].model_dump(mode="json"),
                         execution=[record(receipts, duration=0.6).model_dump(mode="json")],
                         after_transform={"position": [direction, 0, 0], "real_time_s": 1.7})
            attempts.append(trial)
    decl = replace(source.declarations, catalog=catalog)
    compiled = compile_study(replace(source, declarations=decl, attempts=attempts))
    values = {a.name: a for a in compiled.manifest.actions}
    assert values["forward"].hold_effect is True
    assert values["forward"].calibrations[0].holds[0].mean_delta == (1.0, 0.0, 0.0)
    assert values["back"].hold_effect is None
    assert values["back"].calibrations[0].holds[0].mean_delta == (-1.0, 0.0, 0.0)
    assert values["chord"].execution_supported is True
    assert values["chord"].hold_effect is None and all(not c.holds for c in values["chord"].calibrations)


def test_skill_requires_exact_ordered_receipts(tmp_path):
    from dataclasses import replace
    from test_input_probe import skill_declaration
    source = study(tmp_path)
    skill = skill_declaration()
    catalog = ActionCatalog(actions={**source.declarations.catalog.actions, "dash_then_turn": skill.definition()})
    ordered = [receipt(control="w", duration=0.2, step=0), receipt(control="a", duration=0.2, step=0),
               receipt(control="w", duration=0.1, step=1, start=1000.3)]
    def attempt(receipts):
        return {"name": "dash_then_turn", "declaration": catalog.actions["dash_then_turn"].model_dump(mode="json"),
                "execution": [record(receipts, duration=0.4).model_dump(mode="json")], "based_on_seq": 1,
                "action_id": "probe-1", "before_observation": {"session": "s", "seq": 1}}
    base = replace(source, declarations=replace(source.declarations, catalog=catalog))
    good = compile_study(replace(base, attempts=[*source.attempts, attempt(ordered)]))
    bad = compile_study(replace(base, attempts=[*source.attempts, attempt([ordered[1], ordered[0], ordered[2]])]))
    assert good.manifest.skills[0].execution_supported is True
    assert bad.manifest.skills[0].execution_supported is False


def test_wrong_session_and_sequence_cannot_prove_execution(tmp_path):
    from dataclasses import replace
    source = study(tmp_path)
    wrong = dict(source.attempts[0], based_on_seq=2)
    with pytest.raises(ValueError, match="no executable"):
        compile_study(replace(source, attempts=[wrong]))
    wrong_session = dict(source.attempts[0], execution=[{**source.attempts[0]["execution"][0], "session": "other"}])
    with pytest.raises(ValueError, match="another session"):
        compile_study(replace(source, attempts=[wrong_session]))


def bundle(tmp_path):
    source = study(tmp_path)
    compiled = compile_study(source)
    files = {"study.json": study_dict(source), "manifest.json": compiled.manifest.model_dump(mode="json"),
             "entity.json": compiled.entity, "calibration.json": compiled.calibration.model_dump(mode="json"),
             "catalog.json": serialize(compiled.catalog)}
    import hashlib
    for name, value in files.items():
        (tmp_path / name).write_bytes(_dump(value))
    marker = {"schema_version": 1, "game": str(tmp_path), "catalog_path": str(tmp_path / "actions.json"),
              "source": source.declarations.source, "launch": source.launch, "entity_action": "advance",
              "files": {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in files}}
    complete = tmp_path / "complete.json"
    complete.write_bytes(_dump(marker))
    return source, complete, marker


def test_prepared_bundle_refuses_drift_and_changed_launch(tmp_path, monkeypatch):
    from dataclasses import replace
    from playtest.preflight import three_cli
    source, complete, marker = bundle(tmp_path)
    monkeypatch.setattr(three_cli, "load_declarations", lambda *a: source.declarations)
    three_cli._load_complete(complete, query="", settle=60)
    with pytest.raises(ValueError, match="launch query"):
        three_cli._load_complete(complete, query="spawn=elsewhere", settle=60)
    monkeypatch.setattr(three_cli, "load_declarations", lambda *a: replace(source.declarations, source={**source.declarations.source, "dirty": True}))
    with pytest.raises(ValueError, match="clean game checkout"):
        three_cli._load_complete(complete, query="", settle=60)
    monkeypatch.setattr(three_cli, "load_declarations", lambda *a: source.declarations)
    (tmp_path / "catalog.json").write_text("{}")
    with pytest.raises(ValueError, match="completion marker"):
        three_cli._load_complete(complete, query="", settle=60)


def test_interrupted_publication_never_writes_completion_marker(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from playtest.preflight import three_session
    source = study(tmp_path)
    @contextmanager
    def noop(*args, **kwargs):
        yield
    async def acquire(*args, **kwargs):
        return source
    monkeypatch.setattr(three_session, "game_claim", noop)
    monkeypatch.setattr(three_session, "serve", noop)
    monkeypatch.setattr(three_session, "measure", acquire)
    monkeypatch.setattr(three_session, "load_declarations", lambda *a: source.declarations)
    original = Path.write_bytes
    def interrupted(path, data):
        if path.name == "catalog.json":
            raise OSError("interrupted")
        return original(path, data)
    monkeypatch.setattr(Path, "write_bytes", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        three_session.prepare(source.declarations, "advance", tmp_path / "output")
    attempts = list((tmp_path / "output").iterdir())
    assert len(attempts) == 1 and not (attempts[0] / "complete.json").exists()


def test_handoff_serves_pinned_export_and_owned_runner_arguments(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from playtest.preflight import three_cli
    from playtest.runner import cli as runner_cli
    source, complete, marker = bundle(tmp_path)
    seen = {}
    @contextmanager
    def claim(*args):
        yield
    @contextmanager
    def serve(game, catalog, query, page_log=None):
        seen["catalog"] = catalog
        yield
    async def rebind(*args):
        seen["rebind"] = True
    monkeypatch.setattr(three_cli, "game_claim", claim)
    monkeypatch.setattr(three_cli, "serve", serve)
    monkeypatch.setattr(three_cli, "_rebind", rebind)
    monkeypatch.setattr(runner_cli, "main", lambda args: seen.update(argv=args) or 0)
    compiled = compile_study(source)
    assert three_cli.run_prepared(complete, marker, compiled.manifest, ["--max-calls", "1"]) == 0
    assert seen["catalog"] == tmp_path / "catalog.json" and seen["rebind"]
    args = seen["argv"]
    assert args[args.index("--catalog") + 1] == str(seen["catalog"])
    assert args[args.index("--catalog-sha256") + 1] == marker["files"]["catalog.json"]
    with pytest.raises(ValueError, match="runner options"):
        three_cli.run_prepared(complete, marker, compiled.manifest, ["--catalog", "other"])


def transform_response(name="body", parent=None):
    from types import SimpleNamespace
    from playtest.contracts.bridge import InspectionCap
    return SimpleNamespace(stage="transforms", errors=[], caps={"game_objects": InspectionCap(limit=10, observed=1, returned=1, truncated=False)},
        data={"position_space": "world", "position_unit": "three_world_unit", "position_order": ["x", "y", "z"],
              "time_unit": "seconds", "frame": 1, "real_time_s": 1.0, "game_time_s": 1.0,
              "game_objects": [{"id": "entity", "parent_id": parent, "name": name, "position": [0, 0, 0]}]})


def test_fresh_page_rejects_changed_identity_and_unread_public(tmp_path, monkeypatch):
    import asyncio
    from playtest.preflight import three_cli
    from test_readers import observation
    source = study(tmp_path)
    manifest = compile_study(source).manifest
    class Client:
        def __init__(self, name, public):
            self.name, self.public, self.closed = name, public, False
        async def inspect(self, stage):
            assert stage == "transforms"
            return transform_response(name=self.name)
        async def observe_once(self):
            return observation(**self.public)
        async def aclose(self):
            self.closed = True
    client = Client("body", {"speed": 1, "unknown": None})
    async def connect(game):
        return client
    monkeypatch.setattr(three_cli, "connect", connect)
    asyncio.run(three_cli._rebind(tmp_path, source.entity, manifest.readers, 0))
    assert client.closed
    client = Client("replacement", {"speed": 1, "unknown": None})
    with pytest.raises(ValueError, match="name path"):
        asyncio.run(three_cli._rebind(tmp_path, source.entity, manifest.readers, 0))
    assert client.closed
    client = Client("body", {"speed": 1})
    with pytest.raises(ValueError, match="absent"):
        asyncio.run(three_cli._rebind(tmp_path, source.entity, manifest.readers, 0))
    assert client.closed


def test_fresh_page_waits_for_a_slow_inspection(tmp_path, monkeypatch):
    import asyncio
    from fake_game import FakeGame
    from playtest.contracts.bridge import Engine
    from playtest.core.bridge_client import BridgeClient
    from playtest.preflight import three_cli, three_session

    class SlowPage(FakeGame):
        def _status(self, seq, session):
            return super()._status(seq, session).model_copy(update={"engine": Engine.three, "unity_version": None})

        def _inspection(self, request, seq, session):
            result = transform_response()
            return super()._inspection(request, seq, session).model_copy(
                update={"data": result.data, "caps": result.caps, "errors": result.errors}
            )

        def _public(self):
            return {"speed": 1, "unknown": None}

    async def run():
        game = SlowPage(token="slow-page", inspect_delay_s=0.25)
        port = await game.start()

        async def connect_from_project(cls, project, *, timeout_s=10.0):
            client = cls("127.0.0.1", port, "slow-page", timeout_s=timeout_s / 100)
            await client.connect()
            return client

        monkeypatch.setattr(three_session.BridgeClient, "connect_from_project", classmethod(connect_from_project))
        source = study(tmp_path)
        try:
            await three_cli._rebind(tmp_path, source.entity, compile_study(source).manifest.readers, 0)
        finally:
            await game.stop()

    asyncio.run(run())


def test_fatal_probe_stops_remaining_inputs_and_keeps_diagnostics(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from playtest.contracts.bridge import StopResult
    from playtest.preflight import three_session
    from test_readers import observation
    source = study(tmp_path)
    calls = []
    class Client:
        status = SimpleNamespace(session="s")
        async def observe_once(self, **kwargs):
            return observation(speed=3).model_copy(update={"seq": 1, "frame_ref": "frame.jpg"})
        async def inspect(self, stage):
            return transform_response()
        async def act(self, **kwargs):
            return [record([receipt()])]
        async def stop(self):
            calls.append("stop")
            return StopResult.model_validate(source.stop)
        async def aclose(self):
            calls.append("closed")
    async def connect(game):
        return Client()
    async def probe(client, plan, declarations, **kwargs):
        calls.append(declarations[0].name)
        return [{"fatal_error": "terminal release unverified"}]
    async def quiet(*args):
        return None
    monkeypatch.setattr(three_session, "connect", connect)
    monkeypatch.setattr(three_session.input_probe, "measure", probe)
    monkeypatch.setattr(three_session, "_quiet", quiet)
    monkeypatch.setattr(three_session.entity, "analyze", lambda trials: {"status": "identified", "entity_id": "entity", "independent_candidates": ["entity"]})
    monkeypatch.setattr(three_session.input_probe, "frame_record", lambda *a, **k: {"readable": True, "archived": True,
                         "width": 10, "height": 10, "ref": "f.jpg", "bytes": 100, "sha256": "a" * 64})
    with pytest.raises(ValueError, match="terminal release unverified"):
        asyncio.run(three_session.measure(source.declarations, "advance", 0, "", tmp_path))
    assert calls == ["advance", "stop", "closed"]
    diagnostic = json.loads((tmp_path / "study.json").read_text(encoding="utf-8"))
    assert "terminal release unverified" in diagnostic["fatal_error"]
    assert not (tmp_path / "complete.json").exists()
    assert '"kind": "fatal"' in (tmp_path / "session.partial.jsonl").read_text()
