"""Offline input probe: exact receipts, k/n failures and manifest evidence are explicit."""

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from playtest.contracts.action_catalog import ActionCatalog, ActionDefinition, ActionStep, InputBinding
from playtest.contracts.bridge import ExecutionRecord, InspectionCap
from playtest.contracts.manifest import PreflightManifest
from playtest.core.bridge_client import BridgeError
from playtest.preflight import input_probe
from playtest.preflight.input_probe import (
    Declaration,
    ProbePlan,
    ProbeReader,
    evaluate_attempt,
    frame_record,
    resolve_plan,
)


def cap(n=1):
    return InspectionCap(limit=100, observed=n, returned=n, truncated=False)


def receipt(control="w", duration=0.201, start=1000.0, release=True, x=None, y=None,
            delta_x=None, delta_y=None, step=0, device="Keyboard"):
    return {"step_index": step, "device": device, "control": control, "x": x, "y": y,
            "input_start_wall_s": start, "input_end_wall_s": None if duration is None else start + duration,
            "applied_duration_s": duration, "applied_delta_x": delta_x, "applied_delta_y": delta_y,
            "release_verified": release}


def record(receipts=None, *, status="completed", release=True, duration=0.201, start=1000.0,
           reject_reason=None):
    body = {
        "protocol": "upa/1", "session": "s", "seq": 1, "game_s": 1.0,
        "wall_utc": "2026-01-01T00:00:00Z", "type": "execution", "action_id": "probe-1",
        "status": status, "validated_against_seq": 1, "release_verified": release,
        "input_start_wall_s": start if status != "rejected" else None,
        "input_end_wall_s": start + duration if status != "rejected" and duration is not None else None,
        "applied_duration_s": duration if status != "rejected" else None,
        "applied_inputs": receipts or [],
    }
    if reject_reason is not None:
        body["reject_reason"] = reject_reason
    return ExecutionRecord.model_validate(body)


def released_state(controls=None):
    return {
        "real_time_s": 1.05,
        "sample_wall_s": 1000.55,
        "input_state": {
            "active": True,
            "error": None,
            "update_type": "dynamic",
            "real_time_s": 1.0,
            "wall_s": 1000.5,
            "devices": {"Keyboard": {"controls": controls or {"w": False}},
                        "Mouse": {"controls": {"delta": False}}},
        },
    }


def action_declaration():
    return Declaration("move", "action",
                       (InputBinding(device="Keyboard", control="w", x=None, y=None),), hold_s=0.2)


def skill_declaration():
    return Declaration("dash_then_turn", "skill", steps=(
        ActionStep(inputs=[InputBinding(device="Keyboard", control="w", x=None, y=None),
                           InputBinding(device="Keyboard", control="a", x=None, y=None)],
                   hold_s=0.2, wait_s=0.1),
        ActionStep(inputs=[InputBinding(device="Keyboard", control="w", x=None, y=None)],
                   hold_s=0.1, wait_s=0.0),
    ), max_s=0.5)


def test_a_matching_completed_receipt_is_valid():
    judged = evaluate_attempt(action_declaration(), [record([receipt()])], released_state())
    assert judged["valid"] is True and judged["executed"] is True and judged["problems"] == []


@pytest.mark.parametrize("receipts, needle", [
    ([receipt(control="a")], "control"),
    ([receipt(duration=0.9, start=1000.0)], "declared"),
    ([receipt(duration=None)], "no measured applied duration"),
    ([receipt(release=False)], "release was not verified"),
    ([receipt(), receipt(control="a")], "expected 1 applied-input receipts"),
])
def test_wrong_control_value_hold_duration_release_or_count_is_rejected(receipts, needle):
    judged = evaluate_attempt(action_declaration(), [record(receipts)], released_state())
    assert judged["valid"] is False
    assert any(needle in problem for problem in judged["problems"])


def test_a_pointer_impulse_needs_the_applied_pixel_delta_not_a_duration():
    declaration = Declaration("look", "action", (InputBinding(
        device="Mouse", control="delta", x=1.0, y=0.0),), hold_s=0.05)
    good = record([receipt(device="Mouse", control="delta", x=1.0, y=0.0, duration=None,
                           delta_x=64.0, delta_y=0.0)], duration=None)
    assert evaluate_attempt(declaration, [good], released_state({"delta": False}))["valid"] is True
    missing = record([receipt(device="Mouse", control="delta", x=1.0, y=0.0, duration=None)],
                     duration=None)
    judged = evaluate_attempt(declaration, [missing], released_state({"delta": False}))
    assert judged["valid"] is False and any("applied delta" in p for p in judged["problems"])


def test_skill_receipt_order_and_missing_step_are_rejected():
    declaration = skill_declaration()
    first = receipt(control="w", duration=0.2, step=0)
    second = receipt(control="a", duration=0.2, step=0)
    third = receipt(control="w", duration=0.1, step=1, start=1000.3)
    released = released_state({"w": False, "a": False})
    good = record([first, second, third], duration=0.4, start=1000.0)
    assert evaluate_attempt(declaration, [good], released)["valid"] is True
    reordered = record([second, first, third], duration=0.4, start=1000.0)
    judged = evaluate_attempt(declaration, [reordered], released)
    assert judged["valid"] is False and any("control" in p for p in judged["problems"])
    missing = record([first, second], duration=0.4, start=1000.0)
    judged = evaluate_attempt(declaration, [missing], released)
    assert judged["valid"] is False and any("expected 3" in p for p in judged["problems"])


def test_unmeasured_release_is_not_success_and_a_held_control_fails():
    judged = evaluate_attempt(action_declaration(), [record([receipt(release=None)], release=None)],
                              released_state())
    assert judged["valid"] is False and "did not verify input release" in " ".join(judged["problems"])
    judged = evaluate_attempt(action_declaration(), [record([receipt()])], released_state({"w": True}))
    assert judged["valid"] is False and judged["neutral_release"]["verified"] is False
    unknown = evaluate_attempt(action_declaration(), [record([receipt()])], None)
    assert unknown["neutral_release"]["verified"] is None and unknown["valid"] is True


def test_stale_rejection_executes_nothing_and_has_no_receipts():
    rejected = record([], status="rejected", release=None, duration=None, reject_reason="stale_age")
    judged = evaluate_attempt(action_declaration(), [rejected], released_state())
    assert judged["executed"] is False and judged["valid"] is False
    assert rejected.applied_inputs == []
    assert any("executed nothing" in problem for problem in judged["problems"])


def test_plan_resolution_requires_matching_catalog_and_manifest_declarations():
    catalog = ActionCatalog(actions={
        "move": ActionDefinition(backend="input_system",
                                 inputs=[{"device": "Keyboard", "control": "w", "x": None, "y": None}],
                                 default_lease_s=0.2)})
    plan = ProbePlan(schema_version=1, actions=[{"name": "move", "repeats": 1}])
    assert resolve_plan(plan, catalog, None)[0].hold_s == 0.2
    with pytest.raises(ValueError, match="neither"):
        resolve_plan(ProbePlan(schema_version=1, actions=[{"name": "other", "repeats": 1}]), catalog, None)


def test_frame_record_hashes_and_measures_a_png(tmp_path):
    path = tmp_path / "frame.png"
    import struct

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 64, 32) + b"rest")
    result = frame_record(str(path))
    assert result["width"] == 64 and result["height"] == 32
    assert result["sha256"] and result["readable"] is True
    assert frame_record(None)["sha256"] is None
    assert frame_record(str(tmp_path / "missing.png"))["readable"] is False


def test_probe_frame_archive_survives_temporary_capture_cleanup(tmp_path):
    import hashlib
    import struct

    capture = tmp_path / "temporary.png"
    data = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 64, 32) + b"captured bytes"
    capture.write_bytes(data)
    result = frame_record(str(capture), archive_dir=tmp_path / "runs" / "frames")
    capture.unlink()
    assert result["archived"] is True and result["source_ref"] == str(capture)
    archived = Path(result["ref"])
    assert archived.read_bytes() == data
    assert hashlib.sha256(archived.read_bytes()).hexdigest() == result["sha256"]


def test_output_must_stay_under_runs(tmp_path):
    repo = tmp_path / "repo"
    assert input_probe._under_runs(repo / "runs" / "probe.json", repo) is True
    assert input_probe._under_runs(repo / "probe.json", repo) is False


def test_update_manifest_binds_only_named_successful_evidence(tmp_path):
    source = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "preflight_manifest" / "valid-basic.json"
    runs = tmp_path / "repo" / "runs"
    runs.mkdir(parents=True)
    path = runs / "manifest.json"
    shutil.copyfile(source, path)
    declaration = action_declaration()
    judged = evaluate_attempt(declaration, [record([receipt()])], released_state())
    assert judged["valid"] is True
    results = [{"name": "move", "kind": "action", "attempts_made": 1, "successes": 1, "attempts": [
        {"valid": True, "execution": [record([receipt()]).model_dump(mode="json")],
         "execution_evidence": judged["execution_evidence"]}]}]
    update = input_probe.update_manifest(path, results)
    assert update["updates"] == [{"name": "move", "kind": "action", "supported": True,
                                  "attempts_made": 1, "successes": 1}]
    value = PreflightManifest.model_validate_json(path.read_text(encoding="utf-8"), strict=True)
    assert value.actions[0].execution_supported is True
    assert input_probe.manifest_module.ACTION_EXECUTION_GAP not in value.gaps.coverage
    assert value.actions[0].execution_evidence.receipts[0].control == "w"
    assert value.actions[0].execution_evidence.declaration.default_lease_s == 0.2
    cleared = input_probe.update_manifest(path, [{"name": "move", "kind": "action",
                                                  "attempts_made": 1, "successes": 0,
                                                  "attempts": [{"valid": False, "execution": []}]}])
    assert cleared["updates"] == [{"name": "move", "kind": "action", "supported": False,
                                   "attempts_made": 1, "successes": 0}]
    value = PreflightManifest.model_validate_json(path.read_text(encoding="utf-8"), strict=True)
    assert value.actions[0].execution_supported is False and value.actions[0].execution_evidence is None
    assert input_probe.manifest_module.ACTION_EXECUTION_GAP in value.gaps.coverage


class FakeProbeClient:
    def __init__(self):
        self.observations = 0
        self.act_calls = []
        self.actions = 0
        self.fail_after_act = False
        self.act_error = None
        self.receipt_release = True

    async def observe_once(self, *, with_frame=False):
        self.observations += 1
        return SimpleNamespace(seq=self.observations, frame=self.observations, frame_ref=None,
                               public={"hp": 10 + self.observations})

    async def inspect(self, stage):
        from datetime import datetime, timezone

        if stage == "transforms":
            data = {"position_space": "world", "position_unit": "unity_world_unit",
                    "position_order": ["x", "y", "z"], "time_unit": "seconds",
                    "frame": self.observations, "real_time_s": 10.0 + self.observations,
                    "game_time_s": 10.0,
                    "game_objects": [{"id": "entity", "parent_id": None, "position": [0.0, 0.0, 0.0],
                                      "world_forward": [0.0, 0.0, 1.0], "world_right": [1.0, 0.0, 0.0],
                                      "basis_state": "present"}]}
            return SimpleNamespace(stage="transforms", errors=[], wall_utc=datetime(
                2026, 1, 1, tzinfo=timezone.utc), data=data, caps={"game_objects": cap()})
        assert stage == "fields"
        if self.fail_after_act and self.actions:
            raise BridgeError("closed", "the bridge stopped answering after the action")
        real = 10.0 + self.observations
        data = {"real_time_s": real, "game_time_s": 10.0, "time_unit": "seconds",
                "sample_wall_s": 1000.55, "frame": self.observations,
                "input_state": {
                    "active": True, "error": None, "update_type": "dynamic",
                    "real_time_s": real, "wall_s": 1000.5,
                    "devices": {"Keyboard": {"controls": {"w": False}},
                                "Mouse": {"controls": {"delta": False}}}},
                "fields": [{"id": "field", "scene_id": "scene", "value_state": "present",
                            "value_kind": "integer", "value": self.observations}]}
        return SimpleNamespace(stage="fields", errors=[], wall_utc=datetime(
            2026, 1, 1, tzinfo=timezone.utc), data=data,
            caps={"game_objects": cap(), "fields": cap()})

    async def act(self, **kwargs):
        self.act_calls.append(kwargs)
        if self.act_error is not None:
            raise self.act_error
        self.actions += 1
        if self.actions == 2:
            return [record([], status="rejected", release=None, duration=None, reject_reason="stale_age")]
        return [record([receipt(release=self.receipt_release)])]


def test_measure_reports_k_n_binds_freshness_and_stops_after_a_rejection():
    plan = ProbePlan(schema_version=1, actions=[{"name": "move", "repeats": 4}],
                     readers=[{"alias": "score", "field_id": "field"}], public_fields=["hp"],
                     scene_id="scene", settle_s=0, max_age_s=2.0, depends_on=["public.hp"])
    client = FakeProbeClient()
    results = asyncio.run(input_probe.measure(
        client, plan, [action_declaration()], entity_id=None, scene_id="scene",
        readers=[ProbeReader(alias="score", field_id="field")]))
    target = results[0]
    assert target["successes"] == 1 and target["attempts_made"] == 2
    assert target["k_over_n"] == "1/2" and target["repeats_requested"] == 4
    assert client.act_calls[0]["based_on_seq"] == 1
    assert client.act_calls[1]["based_on_seq"] == 3
    assert all(call["params"] == {} for call in client.act_calls)
    assert target["attempts"][0]["public"]["hp"]["changed"] is True
    assert target["attempts"][0]["readers"]["score"]["before"]["value"] == 0
    assert target["attempts"][1]["executed"] is False


class ViewProbeClient(FakeProbeClient):
    """A client whose game view turns right 7.68 degrees on every act, wrapping at the seam, and says so."""

    def __init__(self, reports_view=True):
        super().__init__()
        self.yaw = 175.0
        self.reports_view = reports_view

    async def observe_once(self, *, with_frame=False):
        observation = await super().observe_once(with_frame=with_frame)
        if self.reports_view:
            observation.view = SimpleNamespace(yaw_deg=self.yaw, pitch_deg=1.5)
        return observation

    async def act(self, **kwargs):
        records = await super().act(**kwargs)
        self.yaw = (self.yaw + 7.68 + 180.0) % 360.0 - 180.0
        return records


def test_probe_records_the_game_view_before_and_after_and_its_turn_across_the_seam():
    plan = ProbePlan(schema_version=1, actions=[{"name": "move", "repeats": 1}], scene_id="scene", settle_s=0)
    attempt = asyncio.run(input_probe.measure(
        ViewProbeClient(), plan, [action_declaration()], entity_id=None, scene_id="scene",
        readers=[]))[0]["attempts"][0]
    assert attempt["view"]["before"]["yaw_deg"] == 175.0 and attempt["view"]["after"]["yaw_deg"] == pytest.approx(-177.32)
    assert attempt["view"]["turn"]["yaw_deg"] == pytest.approx(7.68)
    assert attempt["view"]["turn"]["pitch_deg"] == pytest.approx(0.0)
    assert attempt["view"]["before"]["seq"] == attempt["based_on_seq"]


def test_probe_without_a_game_view_records_it_as_unknown_not_zero():
    plan = ProbePlan(schema_version=1, actions=[{"name": "move", "repeats": 1}], scene_id="scene", settle_s=0)
    attempt = asyncio.run(input_probe.measure(
        ViewProbeClient(reports_view=False), plan, [action_declaration()], entity_id=None, scene_id="scene",
        readers=[]))[0]["attempts"][0]
    assert attempt["view"] == {"before": None, "after": None, "turn": None}


def test_pointer_wrong_declared_value_is_rejected():
    declaration = Declaration("look", "action", (InputBinding(
        device="Mouse", control="delta", x=1.0, y=0.0),), hold_s=0.05)
    wrong = record([receipt(device="Mouse", control="delta", x=0.5, y=0.0, duration=None,
                           delta_x=32.0, delta_y=0.0)], duration=None)
    judged = evaluate_attempt(declaration, [wrong], released_state({"delta": False}))
    assert judged["valid"] is False and any("x is" in problem for problem in judged["problems"])


def test_pointer_impulse_requires_the_declared_pixel_delta_without_repair():
    declaration = Declaration("look", "action", (InputBinding(
        device="Mouse", control="delta", x=1.0, y=0.0),), hold_s=0.05)
    # A zero delta under a 1.0 declaration used to count as "both deltas present".
    zero = record([receipt(device="Mouse", control="delta", x=1.0, y=0.0, duration=None,
                           delta_x=0.0, delta_y=0.0)], duration=None)
    judged = evaluate_attempt(declaration, [zero], released_state({"delta": False}))
    assert judged["valid"] is False
    assert any("declared 64 px" in problem for problem in judged["problems"])
    # One pixel short is outside the declared float-transport allowance; it is not clamped.
    short = record([receipt(device="Mouse", control="delta", x=1.0, y=0.0, duration=None,
                            delta_x=63.0, delta_y=0.0)], duration=None)
    assert evaluate_attempt(declaration, [short], released_state({"delta": False}))["valid"] is False


def test_chord_inputs_must_share_the_apply_and_release_window():
    declaration = Declaration("chord", "action", (
        InputBinding(device="Keyboard", control="w", x=None, y=None),
        InputBinding(device="Keyboard", control="a", x=None, y=None)), hold_s=0.2)
    released = released_state({"w": False, "a": False})
    good = record([receipt(control="w", duration=0.2),
                   receipt(control="a", duration=0.2)], duration=0.2, start=1000.0)
    assert evaluate_attempt(declaration, [good], released)["valid"] is True
    staggered_apply = record([receipt(control="w", duration=0.2),
                              receipt(control="a", duration=0.2, start=1000.2)],
                             duration=0.4, start=1000.0)
    judged = evaluate_attempt(declaration, [staggered_apply], released)
    assert judged["valid"] is False
    assert any("not applied together" in problem for problem in judged["problems"])
    staggered_release = record([receipt(control="w", duration=0.2),
                                receipt(control="a", duration=0.5)], duration=0.5, start=1000.0)
    judged = evaluate_attempt(declaration, [staggered_release], released)
    assert judged["valid"] is False
    assert any("not released together" in problem for problem in judged["problems"])


def test_skill_waits_and_final_wait_come_from_measured_receipt_times():
    declaration = skill_declaration()
    released = released_state({"w": False, "a": False})
    first = receipt(control="w", duration=0.2, step=0)
    second = receipt(control="a", duration=0.2, step=0)
    too_soon = receipt(control="w", duration=0.1, step=1, start=1000.25)
    judged = evaluate_attempt(declaration, [record([first, second, too_soon], duration=0.35,
                                                  start=1000.0)], released)
    assert judged["valid"] is False
    assert any("less than the declared 0.1 s wait" in problem for problem in judged["problems"])
    # The terminal end must cover the final declared wait; it is not inferred from max_s.
    waited = Declaration("waited", "skill", steps=(
        ActionStep(inputs=[InputBinding(device="Keyboard", control="w", x=None, y=None)],
                   hold_s=0.2, wait_s=0.1),
        ActionStep(inputs=[InputBinding(device="Keyboard", control="a", x=None, y=None)],
                   hold_s=0.2, wait_s=0.1),
    ), max_s=1.0)
    no_final_wait = record([receipt(control="w", duration=0.2, step=0),
                            receipt(control="a", duration=0.2, step=1, start=1000.3)],
                           duration=0.5, start=1000.0)
    judged = evaluate_attempt(waited, [no_final_wait], released)
    assert judged["valid"] is False
    assert any("final wait" in problem for problem in judged["problems"])
    # A skill cannot borrow a configured total: the measured total must be present.
    missing_total = record([first, second, receipt(control="w", duration=0.1, step=1, start=1000.3)],
                           duration=None, start=1000.0)
    judged = evaluate_attempt(declaration, [missing_total], released)
    assert judged["valid"] is False
    assert any("no measured total applied duration" in problem for problem in judged["problems"])


def test_evidence_requires_the_exact_declaration_before_measurement_tolerance():
    from playtest.contracts.action_catalog import ActionDefinition, evidence_problems
    from playtest.contracts.bridge import InputExecutionEvidence

    requested = ActionDefinition(backend="input_system",
                                 inputs=[{"device": "Keyboard", "control": "w", "x": None, "y": None}],
                                 default_lease_s=0.2)
    receipt = {"step_index": 0, "device": "Keyboard", "control": "w", "x": None, "y": None,
               "input_start_wall_s": 1000.0, "input_end_wall_s": 1000.24,
               "applied_duration_s": 0.24, "applied_delta_x": None, "applied_delta_y": None,
               "release_verified": True}
    changed_hold = InputExecutionEvidence(
        action="preflight_key_w", session="s", status="completed", release_verified=True,
        declaration=ActionDefinition(backend="input_system",
                                     inputs=[{"device": "Keyboard", "control": "w", "x": None, "y": None}],
                                     default_lease_s=0.24),
        applied_duration_s=0.24, input_start_wall_s=1000.0, input_end_wall_s=1000.24,
        receipts=[receipt], wall_utc="2026-01-01T00:00:00Z")
    problems = evidence_problems(changed_hold, requested)
    assert any("declaration hold is 0.24" in problem for problem in problems)
    # The same exact declaration under a different harness label is still the same input.
    same = InputExecutionEvidence(
        action="preflight_key_w", session="s", status="completed", release_verified=True,
        declaration=requested, applied_duration_s=0.201, input_start_wall_s=1000.0,
        input_end_wall_s=1000.201,
        receipts=[dict(receipt, input_end_wall_s=1000.201, applied_duration_s=0.201)],
        wall_utc="2026-01-01T00:00:00Z")
    assert evidence_problems(same, requested) == []
    # A changed wait or max_s is a different skill, not a measurement variance.
    skill = ActionDefinition(backend="input_system", steps=[
        {"inputs": [{"device": "Keyboard", "control": "w", "x": None, "y": None}],
         "hold_s": 0.2, "wait_s": 0.1}], max_s=1.0)
    receipt_skill = {"step_index": 0, "device": "Keyboard", "control": "w", "x": None, "y": None,
                     "input_start_wall_s": 1000.0, "input_end_wall_s": 1000.2,
                     "applied_duration_s": 0.2, "applied_delta_x": None, "applied_delta_y": None,
                     "release_verified": True}
    moved_wait = InputExecutionEvidence(
        action="sequence", session="s", status="completed", release_verified=True,
        declaration=ActionDefinition(backend="input_system", steps=[
            {"inputs": [{"device": "Keyboard", "control": "w", "x": None, "y": None}],
             "hold_s": 0.2, "wait_s": 0.2}], max_s=1.0),
        applied_duration_s=0.4, input_start_wall_s=1000.0, input_end_wall_s=1000.4,
        receipts=[receipt_skill], wall_utc="2026-01-01T00:00:00Z")
    assert any("wait is 0.2" in problem for problem in evidence_problems(moved_wait, skill))
    moved_max = moved_wait.model_copy(update={
        "declaration": skill.model_copy(update={"steps": [skill.steps[0].model_copy(update={"wait_s": 0.1})],
                                                "max_s": 2.0})})
    assert any("max_s is 2.0" in problem for problem in evidence_problems(moved_max, skill))


def test_active_value_keeps_unmeasured_nonfinite_and_all_null_unknown():
    from playtest.preflight import calibrate

    assert calibrate._active_value(float("nan")) is None
    assert calibrate._active_value(float("inf")) is None
    assert calibrate._active_value(None) is None
    assert calibrate._active_value({}) is None
    assert calibrate._active_value({"x": None, "y": None}) is None
    assert calibrate._active_value([None, None]) is None
    assert calibrate._active_value({"active": False}) is False


def test_neutral_release_is_unknown_for_stale_inactive_or_errored_state():
    declaration = action_declaration()
    stale = released_state()
    stale["real_time_s"] = 1.5
    assert input_probe.neutral_release(declaration, stale, terminal_end_wall_s=1000.201)["verified"] is None
    inactive = released_state()
    inactive["input_state"]["active"] = False
    assert input_probe.neutral_release(declaration, inactive, terminal_end_wall_s=1000.201)["verified"] is None
    errored = released_state()
    errored["input_state"]["error"] = "reader failed"
    assert input_probe.neutral_release(declaration, errored, terminal_end_wall_s=1000.201)["verified"] is None
    before_end = released_state()
    before_end["input_state"]["wall_s"] = 1000.0
    assert input_probe.neutral_release(declaration, before_end, terminal_end_wall_s=1000.201)["verified"] is None
    unknown_control = released_state()
    unknown_control["input_state"]["devices"]["Keyboard"]["controls"] = {"a": False}
    release = input_probe.neutral_release(declaration, unknown_control, terminal_end_wall_s=1000.201)
    assert release["verified"] is None and release["reason"] == "a declared control has no measured device state"


def test_a_stale_neutral_sample_stays_unknown_and_is_not_reported_as_passed():
    stale = released_state()
    stale["real_time_s"] = 1.5
    judged = evaluate_attempt(action_declaration(), [record([receipt()])], stale)
    # Per-input device receipts prove release, so support can stand; the independent neutral
    # sample remains explicitly unknown rather than being relabelled as a pass.
    assert judged["valid"] is True
    assert judged["neutral_release"]["verified"] is None
    assert judged["neutral_release"]["fresh"] is None


def test_measure_awaits_the_entity_reader_and_records_both_transforms():
    plan = ProbePlan(schema_version=1, actions=[{"name": "move", "repeats": 1}],
                     scene_id="scene", settle_s=0)
    client = FakeProbeClient()
    results = asyncio.run(input_probe.measure(
        client, plan, [action_declaration()], entity_id="entity", scene_id="scene",
        readers=[ProbeReader(alias="score", field_id="field")]))
    attempt = results[0]["attempts"][0]
    assert attempt["valid"] is True, attempt["problems"]
    assert attempt["before_transform"]["position"] == [0.0, 0.0, 0.0]
    assert attempt["after_transform"]["basis_state"] == "present"
    assert attempt["transform_delta"]["position"] == [0.0, 0.0, 0.0]
    assert results[0]["k_over_n"] == "1/1"


def test_measure_retains_execution_and_stops_all_inputs_when_the_after_read_fails():
    plan = ProbePlan(schema_version=1, actions=[{"name": "move", "repeats": 2},
                                                {"name": "look", "repeats": 2}],
                     scene_id="scene", settle_s=0)
    look = Declaration("look", "action", (InputBinding(
        device="Mouse", control="delta", x=1.0, y=0.0),), hold_s=0.05)
    client = FakeProbeClient()
    client.fail_after_act = True
    results = asyncio.run(input_probe.measure(
        client, plan, [action_declaration(), look], entity_id="entity", scene_id="scene",
        readers=[ProbeReader(alias="score", field_id="field")]))
    target = results[0]
    assert target["attempts_made"] == 1 and target["k_over_n"] == "0/1"
    attempt = target["attempts"][0]
    assert attempt["valid"] is False
    assert attempt["execution"][-1]["applied_inputs"][0]["control"] == "w"
    assert attempt["release_verified_terminal"] is True
    assert any("release is unverified" in problem for problem in attempt["problems"])
    assert any("release is unverified" in row["fatal_error"] for row in results if "fatal_error" in row)
    # The later declaration was never attempted and no invented count suggests it was.
    assert results[1]["attempts_made"] == 0 and results[1]["k_over_n"] is None
    assert results[1]["skipped"]
    assert len(client.act_calls) == 1


def test_an_act_failure_without_a_terminal_record_stops_dependent_inputs():
    plan = ProbePlan(schema_version=1, actions=[{"name": "move", "repeats": 2},
                                                {"name": "look", "repeats": 2}],
                     scene_id="scene", settle_s=0)
    look = Declaration("look", "action", (InputBinding(
        device="Mouse", control="delta", x=1.0, y=0.0),), hold_s=0.05)
    client = FakeProbeClient()
    client.act_error = BridgeError("timeout", "no terminal record arrived")
    results = asyncio.run(input_probe.measure(
        client, plan, [action_declaration(), look], entity_id="entity", scene_id="scene",
        readers=[ProbeReader(alias="score", field_id="field")]))
    target = results[0]
    assert target["attempts_made"] == 1 and target["k_over_n"] == "0/1"
    attempt = target["attempts"][0]
    assert attempt["valid"] is False and attempt["execution"] == []
    assert any("release is unverified" in problem for problem in attempt["problems"])
    assert results[1]["attempts_made"] == 0 and results[1]["k_over_n"] is None
    assert len(client.act_calls) == 1


def test_an_unverified_input_receipt_stops_all_later_declarations():
    plan = ProbePlan(schema_version=1, actions=[{"name": "move", "repeats": 2},
                                                {"name": "look", "repeats": 2}],
                     scene_id="scene", settle_s=0)
    look = Declaration("look", "action", (InputBinding(
        device="Mouse", control="delta", x=1.0, y=0.0),), hold_s=0.05)
    client = FakeProbeClient()
    client.receipt_release = False
    results = asyncio.run(input_probe.measure(
        client, plan, [action_declaration(), look], entity_id=None, scene_id="scene",
        readers=[ProbeReader(alias="score", field_id="field")]))
    target = results[0]
    assert target["attempts_made"] == 1 and target["k_over_n"] == "0/1"
    assert any("release was not verified" in problem for problem in target["attempts"][0]["problems"])
    assert any("did not verify release for every applied input" in row["fatal_error"]
               for row in results if "fatal_error" in row)
    assert results[1]["attempts_made"] == 0 and results[1]["k_over_n"] is None
    assert len(client.act_calls) == 1


def test_update_manifest_promotes_the_last_success_without_clearing_failures(tmp_path):
    source = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "preflight_manifest" / "valid-basic.json"
    runs = tmp_path / "repo" / "runs"
    runs.mkdir(parents=True)
    path = runs / "manifest.json"
    shutil.copyfile(source, path)
    declaration = action_declaration()
    judged = evaluate_attempt(declaration, [record([receipt()])], released_state())
    attempts = [{"valid": False, "problems": ["first repeat failed"], "execution": []},
                {"valid": True, "execution": [record([receipt()]).model_dump(mode="json")],
                 "execution_evidence": judged["execution_evidence"]}]
    results = [{"name": "move", "kind": "action", "attempts_made": 2, "successes": 1,
                "attempts": attempts}]
    update = input_probe.update_manifest(path, results)
    assert update["updates"] == [{"name": "move", "kind": "action", "supported": True,
                                  "attempts_made": 2, "successes": 1}]
    assert len(results[0]["attempts"]) == 2 and results[0]["attempts"][0]["valid"] is False
    value = PreflightManifest.model_validate_json(path.read_text(encoding="utf-8"), strict=True)
    assert value.actions[0].execution_supported is True
