"""Offline measured choices, exact execution, opt-in defaults; no Editor or provider calls."""
import asyncio
import copy
import hashlib
import json

import pytest
from runner_manifest import action, calibration, hold, manifest, vector
from playtest.contracts.calibration import CalibrationRecord
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight.calibrate import discover_actions, negated_view_action
from playtest.preflight.catalog import export, serialize
from playtest.preflight.manifest import calibration_for
from playtest.preflight.update_calibration import update, main as update_main
from playtest.runner.choices import build_choices
from playtest.runner.catalog import verify_host_catalog
from test_speed_finish_options import plan_run


def measurement(seconds, yaw, spread=0):
    return {**hold(seconds), "mean_view_turn": {"trials": 2, "yaw_deg": yaw, "pitch_deg": 0, "spread_deg": spread}}


def sample():
    return manifest(action("look", vector("Gamepad", "rightStick", 1, 0), hold_s=0.1,
        holds=[measurement(.05, 18.38, .16), measurement(.1, 36.21, .03),
               measurement(.2, 74.11, 3.93), measurement(.4, 144.23, .05),
               measurement(1.2, 2, 187), measurement(.3, 4.99), measurement(.6, 50, 5.01)]))


def record(m):
    return {"entity_id": m.entity_id, "action_summaries": [c.model_dump(mode="json") for a in m.actions for c in a.calibrations]}


def turns(built):
    return [c for c in built.choices if c.id.startswith("turn.")]


def test_measured_holds_signs_filters_labels_and_catalog(tmp_path):
    m = sample()
    raw = record(m)
    built = build_choices(m, turn_sizes=True, calibration=raw)
    assert [c.id for c in turns(built)] == [f"turn.gamepad_rightstick.pos{h}" for h in ("0.05", "0.1", "0.2", "0.4")]
    assert turns(built)[0].label == "turn view right 18.4\u00b0 (calibrated: Gamepad rightStick x=1, y=0 for 0.05 s, spread 0.2\u00b0)"
    assert built.catalog == export(m, turn_sizes=True, calibration=raw)
    for c in turns(built):
        d = built.catalog.actions[c.action]
        assert len(d.inputs) == 1 and d.inputs[0].x == 1
        assert d.default_lease_s == built.leases[c.id]
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(serialize(built.catalog)))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert verify_host_catalog(m, path, sha, turn_sizes=True, calibration=raw).catalog == built.catalog
    with pytest.raises(ValueError):
        verify_host_catalog(m, path, sha)
    left = copy.deepcopy(raw["action_summaries"][0])
    left["declared_x"] = -1
    left["holds"] = [measurement(.05, -17, 5)]
    raw["action_summaries"].append(left)
    both = build_choices(m, turn_sizes=True, calibration=raw)
    assert turns(both)[-1].id == "turn.gamepad_rightstick.neg0.05"
    assert "turn view left 17.0" in turns(both)[-1].label
    assert both.catalog.actions[turns(both)[-1].action].inputs[0].x == -1


def test_mouse_is_one_impulse_and_unknown_is_not_a_turn():
    m = manifest(action("look", vector("Mouse", "delta", 1, 0), hold_s=.1,
        holds=[measurement(.05, 7.68), measurement(.1, 7.68), measurement(.2, 7.68)]))
    built = build_choices(m, turn_sizes=True)
    assert len(turns(built)) == 1
    assert turns(built)[0].id == "turn.mouse_delta.pos0.05"
    assert "one relative impulse within 0.05 s" in turns(built)[0].label
    unknown = manifest(action("look", vector("Gamepad", "rightStick", 1, 0), hold_s=.1, holds=[hold(.1)]))
    assert not turns(build_choices(unknown, turn_sizes=True))


def test_off_is_byte_identical_even_with_new_measurements():
    m = sample()
    before = build_choices(m)
    after = build_choices(m, turn_sizes=False, calibration=record(m))
    encode = lambda b: json.dumps({"choices": [c.model_dump(mode="json") for c in b.choices],
        "catalog": serialize(b.catalog), "leases": b.leases, "expected": b.expected}, sort_keys=True)
    assert encode(before) == encode(after)
    assert export(m) == export(m, turn_sizes=False, calibration=record(m))


def test_negated_probe_plan_is_data_driven():
    actions, _ = discover_actions({"bindings": [{"binding_effective_path": "<Gamepad>/leftStick"}]})
    a = actions[0]
    assert negated_view_action(a, []) is None
    assert negated_view_action(a, [{"valid": False, "view_delta": {"yaw_deg": 30}}]) is None
    assert negated_view_action(a, [{"valid": True, "view_delta": {"yaw_deg": 0}}]) is None
    opposite = negated_view_action(a, [{"valid": True, "view_delta": {"yaw_deg": 1}}])
    assert (opposite["x"], opposite["y"]) == (-1, 0)
    assert opposite["action"] == a["action"] + "_negated"


def test_offline_refresh_exact_sign_and_determinism(tmp_path):
    from pathlib import Path
    fixtures = Path(__file__).resolve().parents[1] / "fixtures/contracts/calibration_record"
    source = next(fixtures.glob("valid-*.json"))
    raw = json.loads(source.read_text())
    m = sample()
    raw.update(entity_id=m.entity_id, entity=m.entity, action_summaries=record(m)["action_summaries"])
    # Fixtures may have a different entity id in reset evidence; record validation does not reinterpret it.
    left = copy.deepcopy(raw["action_summaries"][0]); left["declared_x"] = -1
    left["holds"] = [measurement(.1, -35)]
    raw["action_summaries"].append(left)
    a = m.actions[0].model_dump(mode="json")
    a["inputs"][0]["x"] = -1
    a["execution_supported"] = False; a["execution_evidence"] = None; a["hold_effect"] = None
    m = manifest(a)
    c = CalibrationRecord.model_validate(raw)
    updated = update(m, c)
    assert updated.actions[0].calibrations[0].declared_x == -1
    assert updated.actions[0].hold_effect is True
    assert not updated.actions[0].execution_supported
    assert update(m, c) == updated
    mp, cp = tmp_path / "manifest.json", tmp_path / "record.json"
    mp.write_text(m.model_dump_json()); cp.write_text(c.model_dump_json())
    outputs = [tmp_path / "one.json", tmp_path / "two.json"]
    for output in outputs:
        assert update_main(["--manifest", str(mp), "--calibration", str(cp), "--output", str(output)]) == 0
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    with pytest.raises(FileExistsError):
        update_main(["--manifest", str(mp), "--calibration", str(cp), "--output", str(outputs[0])])
    with pytest.raises(ValueError):
        update(m, c.model_copy(update={"entity_id": "different"}))


def test_plan_only_records_enabled_mode(plan_run, monkeypatch):
    monkeypatch.delenv("UPA_RUN_TURN_SIZES", raising=False)
    assert "turn_sizes" not in plan_run()
    assert plan_run("--turn-sizes")["turn_sizes"] is True
    monkeypatch.setenv("UPA_RUN_TURN_SIZES", "1")
    assert plan_run()["turn_sizes"] is True


def test_selected_turn_executes_its_one_declared_input():
    from test_runner_session import connected, closed, session, CatalogGame, FakeS1, harness_trace
    async def run():
        built = build_choices(sample(), turn_sizes=True)
        selected = turns(built)[0]
        game, client = await connected(CatalogGame, catalog=built.catalog)
        sink = harness_trace()
        try:
            result = await session(client, built, s1=FakeS1(selected.id, "finish"), objective="Exercise controls.", sink=sink)
            assert result.ending == "finish"
            requests = [e["body"]["request"] for e in sink.of_kind("execution")]
            assert len(requests) == 1
            assert requests[0]["action"] == selected.action
            assert requests[0]["lease_s"] == .05
            assert result.loop.turns[0].executed_action == selected.id
        finally:
            await closed(game, client)
    asyncio.run(run())


def test_calibration_measures_both_values_and_keeps_their_trials_separate(monkeypatch):
    from playtest.preflight import calibrate
    from test_preflight_calibration import TurningBridge, selected_entity
    async def no_delay(_):
        pass
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (.05, .1))
    discovered, _ = discover_actions({"bindings": [{"binding_effective_path": "<Mouse>/delta"}]})
    monkeypatch.setattr(calibrate, "discover_actions", lambda _: (discovered, []))
    class SignedBridge(TurningBridge):
        def spin(self):
            direction = -1 if self.calls[-1].endswith("_negated") else 1
            self.yaw = (self.yaw + direction * self.turn + 180) % 360 - 180
    client = SignedBridge()
    result = asyncio.run(calibrate.measure(client, selected_entity(), both_signs=True))
    assert result["status"] == "measured_with_gaps"
    assert result["plan"]["negated_view_actions"][0]["x"] == -1
    assert result["plan"]["budget"]["planned_trials"] == 2 * len(calibrate.trial_schedule())
    assert [r["declared_x"] for r in result["action_summaries"]] == [1, -1]
    for row in result["action_summaries"]:
        assert all(h["mean_view_turn"]["yaw_deg"] == pytest.approx(row["declared_x"] * 7.68) for h in row["holds"])
        assert all(h["valid_trials"] >= 2 for h in row["holds"])
    baseline = asyncio.run(calibrate.measure(SignedBridge(), selected_entity()))
    assert "both_signs" not in baseline["plan"]
    assert len(baseline["action_summaries"]) == 1
    assert all("declared_x" not in trial for trial in baseline["trials"])


def test_packet_retains_both_values_and_assembly_matches_the_declaration():
    from test_preflight_packet import evidence, make
    from playtest.preflight import stages
    from playtest.preflight.manifest import assemble, validate_evidence
    snapshot, raw = evidence()
    snapshot["data"]["bindings"][0]["binding_effective_path"] = "<Gamepad>/rightStick"
    actions, _ = discover_actions(snapshot["data"])
    raw["actions"] = actions
    rows = record(sample())["action_summaries"]
    for row in rows:
        row["bindings"] = actions[0]["bindings"]
        row["resolution"] = actions[0]["resolution"]
    left = copy.deepcopy(rows[0]); left["declared_x"] = -1
    left["holds"] = [measurement(.1, -35)]
    raw["action_summaries"] = [*rows, left]
    raw["trials"] = [{"control": actions[0]["control"], "valid": True}]
    packet = make(snapshot, raw)
    assert len(json.loads(packet.text)["calibration"]["control_summaries"]) == 2
    replies = iter([
        {"stage_4": {"readers": [], "unobservable": ["unknown"]}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "look", "intent": "look", "hold_s": .1,
         "inputs": [{"control_id": actions[0]["control"], "x": -1, "y": 0}], "preconditions": []}],
         "skills": [], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": ["unknown"]}}])
    result = stages.run(packet, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {"usage": None}), lambda _: None)
    assembled = assemble(packet, result)
    assert assembled.actions[0].calibrations[0].declared_x == -1
    assert validate_evidence(assembled, packet) == []


def test_a_longer_hold_that_turned_less_has_wrapped_and_is_not_offered():
    # The 31 October FPSKit calibration: the left stick turned 144.3° in 0.4 s but read 72.9° after 1.2 s,
    # with a spread under 5°, because the view had spun past a full circle.
    m = manifest(action("look", vector("Gamepad", "rightStick", 1, 0), hold_s=0.1,
        holds=[measurement(.05, 18.27, .09), measurement(.1, 36.25, .3), measurement(.2, 72.2, .06),
               measurement(.4, 144.22, .03), measurement(1.2, 72.91, 3.72)]))
    built = build_choices(m, turn_sizes=True, calibration=record(m))
    assert [c.id for c in turns(built)] == [f"turn.gamepad_rightstick.pos{h}" for h in ("0.05", "0.1", "0.2", "0.4")]
