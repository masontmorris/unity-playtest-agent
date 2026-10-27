"""The bench freezes payloads, retains unknowns and never repairs a model choice."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from playtest.bench.record import calibrated_choices, context_for, digest
from playtest.bench.labels import make_sheet
from playtest.bench.replay import accepted, load_inputs, parse_answer, run, score
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, ChoiceSource
from playtest.core.context import canonical


def _save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _inputs(root: Path) -> tuple[Path, Path, Path]:
    choices = [Choice(id="c1", kind=ChoiceKind.action, label="First", action="alpha", source=ChoiceSource.manifest),
               Choice(id="c2", kind=ChoiceKind.action, label="Second", action="beta", source=ChoiceSource.manifest)]
    frames = []
    for i in range(40):
        obs = Observation(session="synthetic", seq=i + 1, game_s=float(i),
                          wall_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
                          clock_mode=ClockMode.realtime, scene="synthetic", playing=True, paused=False,
                          time_scale=1, frame=i, public={"missing": None}, reader_errors=[])
        frame = {"id": f"frame-{i:02d}", "objective": "Check the inputs.",
                 "observation": obs.model_dump(mode="json"),
                 "choice_set": ChoiceSet(set_version=i, built_from_seq=obs.seq, choices=choices).model_dump(mode="json"),
                 "facts": [f"Prior count: {i}"], "age_s": None, "sample_gap_s": None, "game_view": None,
                 "execution": [{"status": "completed", "release_verified": True, "applied_duration_s": 1.2}],
                 "after_observation": obs.model_dump(mode="json")}
        frame["payload_sha256"] = digest(canonical(context_for(frame)))
        frames.append(frame)
    corpus = root / "corpus.json"
    _save(corpus, {"status": "complete", "stop": {"release_verified": True}, "frames": frames,
                   "requested_hold_s": 1.2, "game_commit": "synthetic-game",
                   "repo_commit": "synthetic-repo", "unity_version": "synthetic",
                   "bridge_source_sha256": "b", "python_source_sha256": "p", "entity_sha256": "e",
                   "calibration_sha256": "c", "instructions_sha256": "i", "objective_sha256": "o",
                   "game_worktree_before": {"commit": "synthetic-game", "changed_files_sha256": {}},
                   "game_worktree_after": {"commit": "synthetic-game", "changed_files_sha256": {}}})
    labels = root / "labels.json"
    _save(labels, {"corpus_sha256": digest(corpus.read_bytes()), "labels": [
        {"frame_id": f["id"], "payload_sha256": f["payload_sha256"], "acceptable": ["c1"],
         "labeller": "Test Reviewer", "date": "2026-01-01", "rationale": "Synthetic test."} for f in frames]})
    approval = root / "approval.json"
    limits = {arm: {"max_calls": 90, "max_cost_usd": 0.01, "per_call_max_usd": 0.0001,
                    "verified_balance_usd": 1, "balance_checked_at_utc": "2026-01-01T00:00:00Z",
                    "price_valid_until_utc": "2099-01-01T00:00:00Z"}
              for arm in ("mammoth-1", "elephant-1")}
    _save(approval, {"approved_by": "Test User", "approved_at_utc": "2026-01-01T00:00:00Z",
                     "max_calls": 180, "max_cost_usd": 0.02, "arms": limits})
    return corpus, labels, approval


def test_paired_replay_repeats_are_distinct_and_resume_without_calls(tmp_path: Path) -> None:
    corpus, labels, approval = _inputs(tmp_path)
    arms = ["mammoth-1", "elephant-1"]
    requests: list[tuple[str, dict]] = []

    def fake(arm: str, body: dict):
        requests.append((arm, body))
        seq = body["state"]["as_of"]["seq"]
        choice = "c2" if arm == "elephant-1" and seq % 2 == 0 else "c1"
        return 200, {"model": arm, "answers": {"next_action": {
            "type": "choice", "choice": choice, "probabilities": {"c1": 0.6, "c2": 0.4}}}}, 10.0, None

    directory = tmp_path / "run"
    with pytest.raises(ValueError, match="noise repeats"):
        run(corpus, labels, approval, directory, phase="sweep", arms=arms, seed=7, send=fake)
    assert run(corpus, labels, approval, directory, phase="noise", arms=arms, seed=7, send=fake) == 100
    assert run(corpus, labels, approval, directory, phase="noise", arms=arms, seed=7, send=fake) == 0
    assert run(corpus, labels, approval, directory, phase="sweep", arms=arms, seed=7, send=fake) == 80
    assert len(requests) == 180
    report = score(corpus, labels, directory, control="mammoth-1")
    assert report["arms"]["mammoth-1"]["top1_acceptable"] == 1
    assert report["arms"]["elephant-1"]["top1_acceptable"] == 0.5
    assert report["arms"]["mammoth-1"]["noise_flip_rate"] == 0
    assert report["paired"]["elephant-1"]["n"] == 40
    assert len(report["paired"]["elephant-1"]["losses"]) == 20
    assert report["paired"]["elephant-1"]["ties"] == 20
    assert report["verdict"] == "inconclusive"


def test_unoffered_choice_is_not_repaired_from_distribution() -> None:
    data = {"answers": {"next_action": {"type": "choice", "choice": "missing",
                                          "probabilities": {"c1": 1.0}}}}
    assert parse_answer(data, {"c1"}) == (None, None, "invalid or unoffered choice")
    data["answers"]["next_action"]["choice"] = "c1"
    data["answers"]["next_action"]["probabilities"] = {"c1": 4.0}
    assert parse_answer(data, {"c1"}) == ("c1", None, "invalid distribution")


def test_changed_context_or_unbound_labels_fail_closed(tmp_path: Path) -> None:
    corpus, labels, _ = _inputs(tmp_path)
    data = json.loads(corpus.read_text(encoding="utf-8"))
    data["frames"][0]["facts"] = ["changed"]
    _save(corpus, data)
    with pytest.raises(ValueError, match="different corpus"):
        load_inputs(corpus, labels)
    label_data = json.loads(labels.read_text(encoding="utf-8"))
    label_data["corpus_sha256"] = digest(corpus.read_bytes())
    _save(labels, label_data)
    with pytest.raises(ValueError, match="context changed"):
        load_inputs(corpus, labels)


def test_reviewed_labels_rescore_only_with_the_frozen_original_and_attribution(tmp_path: Path) -> None:
    corpus, frozen, approval = _inputs(tmp_path)
    directory = tmp_path / "run"

    def fake(arm: str, body: dict):
        return 200, {"model": arm, "answers": {"next_action": {"type": "choice", "choice": "c1"}}}, 5.0, None

    run(corpus, frozen, approval, directory, phase="noise", arms=["mammoth-1"], seed=4, send=fake)
    run(corpus, frozen, approval, directory, phase="sweep", arms=["mammoth-1"], seed=4, send=fake)
    revised = tmp_path / "reviewed.json"
    data = json.loads(frozen.read_text(encoding="utf-8"))
    data["labels"][0]["acceptable"] = ["c2"]
    data["authorship"] = {"kind": "assistant_draft", "human_review": "pending"}
    _save(revised, data)
    with pytest.raises(ValueError, match="frozen corpus and labels"):
        score(corpus, revised, directory, control="mammoth-1")
    with pytest.raises(ValueError, match="completed human review"):
        score(corpus, revised, directory, control="mammoth-1", frozen_labels_path=frozen)
    data["authorship"].update(human_review="completed", reviewer="Test Reviewer", reviewed_at="2026-01-02")
    _save(revised, data)
    report = score(corpus, revised, directory, control="mammoth-1", frozen_labels_path=frozen)
    assert report["arms"]["mammoth-1"]["top1_acceptable"] == 39 / 40
    assert report["label_provenance"] == {
        "mode": "post_hoc_review", "frozen_labels_sha256": digest(frozen.read_bytes()),
        "scoring_labels_sha256": digest(revised.read_bytes()), "original_authorship": {"kind": "unknown"},
        "review": {"reviewer": "Test Reviewer", "reviewed_at": "2026-01-02"}}
    _save(frozen, data)
    with pytest.raises(ValueError, match="frozen corpus and labels"):
        score(corpus, revised, directory, control="mammoth-1", frozen_labels_path=frozen)


def test_uncertain_transport_stops_that_arm_without_retries(tmp_path: Path) -> None:
    corpus, labels, approval = _inputs(tmp_path)
    requests = []

    def failed(arm: str, body: dict):
        requests.append(arm)
        return None, None, 5.0, "transport error"

    directory = tmp_path / "run"
    assert run(corpus, labels, approval, directory, phase="noise", arms=["mammoth-1"], seed=1, send=failed) == 1
    assert run(corpus, labels, approval, directory, phase="noise", arms=["mammoth-1"], seed=1, send=failed) == 0
    assert requests == ["mammoth-1"]
    rows = [json.loads(p.read_text()) for p in (directory / "calls").glob("*.json")]
    assert rows[0]["choice"] is None and rows[0]["billed"] is None


def test_invalid_success_response_stops_that_arm_without_retries(tmp_path: Path) -> None:
    corpus, labels, approval = _inputs(tmp_path)
    sent = []
    def invalid(arm: str, body: dict):
        sent.append(arm)
        return 200, {"answers": {}}, 1.0, None
    directory = tmp_path / "run"
    assert run(corpus, labels, approval, directory, phase="noise", arms=["mammoth-1"], seed=1, send=invalid) == 1
    assert run(corpus, labels, approval, directory, phase="noise", arms=["mammoth-1"], seed=1, send=invalid) == 0
    assert sent == ["mammoth-1"]


def test_failed_arm_stays_stopped_while_a_healthy_arm_can_complete(tmp_path: Path) -> None:
    corpus, labels, approval = _inputs(tmp_path)
    arms = ["mammoth-1", "elephant-1"]
    sent = []
    def fake(arm: str, body: dict):
        sent.append(arm)
        if arm == "mammoth-1":
            return 401, None, 1.0, "auth error"
        return 200, {"model": arm, "answers": {"next_action": {"type": "choice", "choice": "c1"}}}, 1.0, None
    directory = tmp_path / "run"
    assert run(corpus, labels, approval, directory, phase="noise", arms=arms, seed=2, send=fake) == 51
    assert run(corpus, labels, approval, directory, phase="sweep", arms=arms, seed=2, send=fake) == 40
    assert sent.count("mammoth-1") == 1
    report = score(corpus, labels, directory, control="mammoth-1")
    assert report["arms"]["elephant-1"]["scored"] == 40
    assert report["paired"]["elephant-1"]["n"] == 0


def test_reported_cost_over_bound_stops_all_further_calls(tmp_path: Path) -> None:
    corpus, labels, approval = _inputs(tmp_path)
    sent = []
    def expensive(arm: str, body: dict):
        sent.append(arm)
        return 200, {"model": arm, "usage": {"cost": 0.001}, "answers": {"next_action": {
            "type": "choice", "choice": "c1"}}}, 5.0, None
    directory = tmp_path / "run"
    with pytest.raises(ValueError, match="exceeded the per-call bound"):
        run(corpus, labels, approval, directory, phase="noise", arms=["mammoth-1"], seed=1, send=expensive)
    assert len(sent) == 1
    row = json.loads(next((directory / "calls").glob("*.json")).read_text())
    assert row["cost_usd"] == 0.001 and row["cost_bound_exceeded"] is True
    with pytest.raises(ValueError, match="reconcile the envelope"):
        run(corpus, labels, approval, directory, phase="noise", arms=["mammoth-1"], seed=1, send=expensive)
    assert len(sent) == 1


def test_expired_pricing_stops_before_the_first_call(tmp_path: Path) -> None:
    corpus, labels, approval = _inputs(tmp_path)
    data = json.loads(approval.read_text(encoding="utf-8"))
    data["arms"]["mammoth-1"]["price_valid_until_utc"] = "2020-01-01T00:00:00Z"
    _save(approval, data)
    calls = []
    with pytest.raises(ValueError, match="pricing validity has expired"):
        run(corpus, labels, approval, tmp_path / "run", phase="noise", arms=["mammoth-1"], seed=1,
            send=lambda arm, body: calls.append(arm))
    assert calls == []


def test_unknown_budget_inputs_require_explicit_overrides(tmp_path: Path) -> None:
    corpus, labels, approval = _inputs(tmp_path)
    data = json.loads(approval.read_text(encoding="utf-8"))
    arm = data["arms"]["mammoth-1"]
    arm["verified_balance_usd"] = arm["balance_checked_at_utc"] = None
    arm["price_valid_until_utc"] = None
    _save(approval, data)
    with pytest.raises(ValueError, match="verified balance or explicit user override"):
        run(corpus, labels, approval, tmp_path / "run", phase="noise", arms=["mammoth-1"], seed=1)
    data["unverified_balance_override"] = {"approved_by_user": True, "reason": "Explicit test instruction"}
    _save(approval, data)
    with pytest.raises(ValueError, match="pricing validity deadline"):
        run(corpus, labels, approval, tmp_path / "run", phase="noise", arms=["mammoth-1"], seed=1)
    arm.update(allow_unverified_price=True, estimated_price_basis="Prior test quote", estimate_use_by_utc="2099-01-01T00:00:00Z")
    _save(approval, data)
    run(corpus, labels, approval, tmp_path / "run", phase="noise", arms=["mammoth-1"], seed=1,
        send=lambda arm, body: (401, None, 1.0, "test stop"))
    plan = json.loads((tmp_path / "run" / "plan.json").read_text(encoding="utf-8"))
    assert plan["budget_assumptions"]["unverified_price_arms"] == ["mammoth-1"]
    assert plan["budget_assumptions"]["balance_override"]["approved_by_user"] is True


def test_label_sheet_has_no_automatic_labels_and_never_overwrites(tmp_path: Path) -> None:
    corpus, _, _ = _inputs(tmp_path)
    sheet = tmp_path / "review"
    make_sheet(corpus, sheet)
    rows = json.loads((sheet / "labels.json").read_text(encoding="utf-8"))["labels"]
    assert len(rows) == 40
    assert all(not row["acceptable"] and not row["labeller"] and not row["rationale"] for row in rows)
    with pytest.raises(ValueError, match="every frame|invalid accept-list"):
        load_inputs(corpus, sheet / "labels.json")
    with pytest.raises(ValueError, match="never overwritten"):
        make_sheet(corpus, sheet)


def test_disagreeing_human_labels_are_not_averaged_or_silently_resolved() -> None:
    assert accepted([{"acceptable": ["c1"]}, {"acceptable": ["c2"]}]) is None
    assert accepted([{"acceptable": ["c1", "c2"]}, {"acceptable": ["c2", "c1"]}]) == {"c1", "c2"}


def test_checkpoint_retries_a_temporary_windows_read_lock(tmp_path: Path, monkeypatch) -> None:
    from playtest.bench import record as recording

    destination = tmp_path / "frames.json"
    destination.write_text('{"old": true}', encoding="utf-8")
    original_replace = Path.replace
    attempts = []

    def replace(path, target):
        attempts.append(path)
        if len(attempts) < 3:
            raise PermissionError("simulated sharing violation")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(recording.time, "sleep", lambda delay: None)
    recording._save(destination, {"new": True})
    assert len(attempts) == 3
    assert json.loads(destination.read_text(encoding="utf-8")) == {"new": True}
    assert not destination.with_suffix(".json.tmp").exists()


def test_recording_stops_after_failed_execution_and_keeps_prior_evidence(tmp_path: Path, monkeypatch) -> None:
    from playtest.bench import record as recording

    choices = [Choice(id=f"c{i}", kind=ChoiceKind.action, label=f"Control {i}", action=f"action{i}",
                      params={"duration_game_s": 1.2}, source=ChoiceSource.manifest) for i in range(1, 9)]
    choices.append(Choice(id="finish", kind=ChoiceKind.finish, label="Finish", source=ChoiceSource.core))
    discovery = {"inspection_coverage": {name: {"truncated": False} for name in
                 ("game_objects", "components", "bindings", "input_asset_bindings")},
                 "game_objects": [{"id": "entity", "scene_id": "scene", "scene_path": "scene", "path": "object"}]}
    monkeypatch.setattr(recording, "_complete", lambda response, stage: discovery)
    monkeypatch.setattr(recording, "calibrated_choices", lambda calibration, current: choices)
    monkeypatch.setattr(recording, "_new_runtime_errors", lambda sample, previous: (previous, []))
    monkeypatch.setattr(recording, "_entity_snapshot", lambda response, entity: {
        "position": [float(response.seq), 0.0, 0.0], "game_time_s": float(response.seq)})

    class Client:
        def __init__(self):
            self.seq = 0
            self.sent = []

        async def inspect(self, stage):
            self.seq += 1
            return SimpleNamespace(seq=self.seq)

        async def observe_once(self):
            self.seq += 1
            return Observation(session="synthetic", seq=self.seq, game_s=float(self.seq),
                               wall_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
                               clock_mode=ClockMode.realtime, scene="scene", playing=True, paused=False,
                               time_scale=1, frame=self.seq, reader_errors=[])

        async def act(self, **kwargs):
            self.sent.append(kwargs)
            success = len(self.sent) == 1
            return [SimpleNamespace(status=SimpleNamespace(value="completed" if success else "failed"),
                release_verified=success, applied_duration_s=1.2, model_dump=lambda mode: {
                    "status": "completed" if success else "failed", "release_verified": success})]

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(recording.asyncio, "sleep", no_sleep)
    selected = {"status": "identified", "entity_id": "entity", "stop": {"release_verified": True},
                "entity": {"scene_id": "scene", "scene_path": "scene", "path": "object"}}
    client = Client()
    result = {"status": "recording", "frames": []}
    output = tmp_path / "partial.json"
    with pytest.raises(ValueError, match="did not complete"):
        asyncio.run(recording.record(client, selected, {"entity_id": "entity"}, result, output))
    retained = json.loads(output.read_text(encoding="utf-8"))["frames"]
    assert len(retained) == 2
    assert [item["choice_id"] for item in client.sent] == ["c1", "c2"]
    assert client.sent[0]["params"] == {"duration_game_s": 1.2}
    assert "c1" in retained[1]["facts"][0]
    assert retained[1]["execution"][-1]["status"] == "failed"
    assert retained[1]["after_observation"] is None


def test_calibrated_catalog_rejects_changed_descriptors() -> None:
    actions, summaries, bindings = [], [], []
    for i, control in enumerate("abcdefgh"):
        binding = {"id": f"b{i}", "action_id": f"a{i}", "binding_effective_path": f"<Keyboard>/{control}",
                   "binding_name": "part", "source_object_id": "object"}
        bindings.append(binding)
        actions.append({"control": control, "action": f"preflight_key_{control}", "resolution": "unity_key_name",
                        "bindings": [{"binding_id": f"b{i}", "action_id": f"a{i}",
                                      "path": f"<Keyboard>/{control}", "part": "part", "source_object_id": "object"}]})
        summaries.append({"control": control, "holds": [{"requested_hold_s": 1.2, "valid_trials": 2,
                           "mean_magnitude": float(i), "sample_variance": 0.0}]})
    calibration = {"action_cap": {"truncated": False}, "actions": actions, "action_summaries": summaries}
    discovery = {"bindings": bindings, "input_asset_bindings": []}
    assert len(calibrated_choices(calibration, discovery)) == 10
    bindings[0]["source_object_id"] = "different"
    with pytest.raises(ValueError, match="no longer resolves"):
        calibrated_choices(calibration, discovery)
