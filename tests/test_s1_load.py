"""The latency test sends production payloads at planned sizes, reserves every call and never retries."""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

from playtest.bench import load, load_resume
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, ChoiceSource

SEED = 2510


def _frame(index: int) -> dict:
    seq = 100 + index
    observation = Observation(session="synthetic", seq=seq, game_s=float(seq), wall_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
                              clock_mode=ClockMode.realtime, scene="scene", playing=True, paused=False, time_scale=1,
                              frame=seq, reader_errors=[])
    controls = [Choice(id=f"c{i}", kind=ChoiceKind.action, label=f"Control {i}", action=f"action{i}",
                       params={"duration_game_s": 1.2}, source=ChoiceSource.manifest) for i in range(1, 9)]
    choices = ChoiceSet(set_version=index, built_from_seq=seq, choices=controls + [
        Choice(id="wait", kind=ChoiceKind.wait, label="Wait", source=ChoiceSource.core),
        Choice(id="finish", kind=ChoiceKind.finish, label="Finish", source=ChoiceSource.core)])
    return {"id": f"frame-{index:02d}", "objective": "Check each control once, then finish.",
            "observation": observation.model_dump(mode="json"), "choice_set": choices.model_dump(mode="json"),
            "facts": [f"Entity position from inspection seq {seq} at game time {seq}.0: [{index}.0, 0.0, 0.0]."],
            "age_s": 0.01, "sample_gap_s": None, "game_view": None}


def _inputs(tmp_path: Path, *, calls: int = 41) -> dict:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"status": "complete", "frames": [_frame(i) for i in range(45)]}), encoding="utf-8")
    pool = tmp_path / "pool.json"  # Enough distinct lines to reach the full size several times over.
    pool.write_text(json.dumps({"turns": [{"facts": [
        f"c{i % 8 + 1} (action{i % 8 + 1}) held for 1.2 seconds with verified release; net displacement between "
        f"inspection seq {i} and {i + 4}: {i / 7:.6f} world units, measured against an idle baseline of the same length."
        for i in range(600)]}]}), encoding="utf-8")
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + (1280).to_bytes(4, "big")
                      + (720).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + bytes(64))
    limit = {"max_calls": calls, "max_cost_usd": 0.205, "per_call_max_usd": 0.005, "verified_balance_usd": 1,
             "balance_checked_at_utc": "2026-01-01T00:00:00Z", "price_valid_until_utc": "2099-01-01T00:00:00Z"}
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"approved_by": "Test User", "approved_at_utc": "2026-01-01T00:00:00Z",
                                    "max_calls": calls, "max_cost_usd": 0.205, "arms": {"mammoth-1": limit}}),
                        encoding="utf-8")
    return {"corpus_path": corpus, "pool_paths": [pool], "frame_path": frame, "approval_path": approval,
            "directory": tmp_path / "run"}


class Provider:
    """A scripted provider: answers `c1`, with a latency set by the condition, unless told to fail."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.fail_at, self.bodies = fail_at, []

    def __call__(self, body: dict):
        index = len(self.bodies)
        self.bodies.append(body)
        if index == self.fail_at:
            return 500, None, 40.0, "HTTP error", False
        framed = "messages" in body["state"]
        state = json.loads(body["state"]["messages"][0]["content"][0]["text"]) if framed else body["state"]
        latency = 100.0 * len(state["facts"]) + (300.0 if framed else 0.0) + index
        return (200, {"model": "mammoth-1", "usage": {"input_tokens": 1000 + 10 * len(state["facts"])},
                      "answers": {"next_action": {"type": "choice", "choice": "c1"}}}, latency, None, index == 0)


def test_the_schedule_warms_up_then_sends_every_condition_once_a_round() -> None:
    order = load.schedule(SEED)
    assert order[0] == "warmup" and len(order) == 1 + 5 * load.ROUNDS
    assert all(sorted(order[1 + 5 * r:6 + 5 * r]) == sorted(load.CONDITIONS) for r in range(load.ROUNDS))
    assert order == load.schedule(SEED) and order != load.schedule(SEED + 1)


def test_padding_reaches_the_target_with_older_facts_first_and_no_repeats(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    pool, frame = load.fact_pool(inputs["pool_paths"]), _frame(3)
    body, size = load.build("full", frame, pool, random.Random(1), None)
    facts = body["state"]["facts"]
    assert size["estimated_text_tokens"] >= load.TARGET_TOKENS["full"] and size["padding_lines"] == len(facts) - 1
    assert facts[-1] == frame["facts"][0] and len(set(facts)) == len(facts)
    assert [c["id"] for c in body["state"]["choices"]] == [f"c{i}" for i in range(1, 9)] + [
        "wait", "escalate", "delegate", "finish"]
    assert body["state"]["choices"][0]["label"] == "Control 1" and body["state"]["game_view"] is None
    base, base_size = load.build("base", frame, pool, random.Random(1), None)
    assert base["state"]["facts"] == frame["facts"] and base_size["padding_lines"] == 0
    with pytest.raises(ValueError, match="cannot reach"):
        load.build("full", frame, pool[:10], random.Random(1), None)


def test_frame_conditions_send_the_state_as_text_beside_the_image(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    pool, url = load.fact_pool(inputs["pool_paths"]), "data:image/png;base64,AAAA"
    body, size = load.build("full+frame", _frame(4), pool, random.Random(2), url)
    text, image = body["state"]["messages"][0]["content"]
    state = json.loads(text["text"])
    assert image == {"type": "image_url", "image_url": {"url": url}} and size["image"] is True
    assert state["game_view"] == load.GAME_VIEW and size["estimated_text_tokens"] >= load.TARGET_TOKENS["full+frame"]
    assert set(body["questions"]["next_action"]["criteria"]) == {c["id"] for c in state["choices"]}


def test_a_run_reserves_every_call_and_stops_on_a_failure_without_retry(tmp_path: Path) -> None:
    inputs, provider, pauses = _inputs(tmp_path), Provider(fail_at=3), []
    with pytest.raises(RuntimeError, match="no retry"):
        load.run(**inputs, seed=SEED, send=provider, sleep=pauses.append)
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((inputs["directory"] / "calls").glob("*.json"))]
    assert len(provider.bodies) == 4 and len(rows) == 4 and pauses == [load.PAUSE_S] * 3
    assert [r["http_status"] for r in rows] == [200, 200, 200, 500] and all(r["status"] == "finished" for r in rows)
    plan = json.loads((inputs["directory"] / "plan.json").read_text(encoding="utf-8"))
    assert [r["condition"] for r in rows] == [item["condition"] for item in plan["schedule"][:4]]
    assert len({item["frame_id"] for item in plan["schedule"]}) == len(plan["schedule"]) == 41
    assert plan["frame"]["width"] == 1280 and plan["frame"]["height"] == 720


def test_a_run_refuses_an_envelope_that_cannot_cover_the_plan(tmp_path: Path) -> None:
    inputs, provider = _inputs(tmp_path, calls=40), Provider()
    with pytest.raises(ValueError, match="envelope"):
        load.run(**inputs, seed=SEED, send=provider, sleep=lambda s: None)
    assert provider.bodies == [] and not inputs["directory"].exists()


def test_score_summarizes_each_condition_and_checks_the_schedule(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    assert load.run(**inputs, seed=SEED, send=Provider(), sleep=lambda s: None) == 41
    report = load.score(inputs["directory"])
    assert report["attempted"] == report["answered"] == 41 and report["stopped_early"] is False
    assert report["warmup"]["attempted"] == 1 and report["warmup"]["new_connection_calls"] == 1
    for name in load.CONDITIONS:
        summary = report["conditions"][name]
        assert summary["attempted"] == summary["answered"] == 8 and summary["p95_ms"] == summary["max_ms"]
        assert summary["invalid_answers"] == 0 and summary["new_connection_calls"] == 0
    assert report["conditions"]["base"]["at_or_over_slow_ms"] == 0
    assert report["conditions"]["full"]["at_or_over_slow_ms"] == 8
    assert report["conditions"]["full"]["input_tokens"]["min"] > report["conditions"]["base"]["input_tokens"]["max"]
    first = sorted((inputs["directory"] / "calls").glob("*.json"))[1]
    row = json.loads(first.read_text(encoding="utf-8"))
    row["condition"] = "full" if row["condition"] != "full" else "base"
    first.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="schedule"):
        load.score(inputs["directory"])


def test_a_stopped_run_resumes_into_a_new_directory_and_leaves_the_first_untouched(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    with pytest.raises(RuntimeError, match="no retry"):
        load.run(**inputs, seed=SEED, send=Provider(fail_at=3), sleep=lambda s: None)
    first = inputs["directory"]
    before = {p.name: p.read_bytes() for p in (first / "calls").glob("*.json")}
    second, provider = tmp_path / "run-2", Provider()
    made = load_resume.resume(first, second, inputs["corpus_path"], inputs["pool_paths"], inputs["frame_path"],
                              inputs["approval_path"], reason="the user chose to resume", send=provider,
                              sleep=lambda s: None)
    assert made == len(provider.bodies) == 1 + 38
    assert {p.name: p.read_bytes() for p in (first / "calls").glob("*.json")} == before
    assert all((second / "calls" / name).read_bytes() == raw for name, raw in before.items() if name != "003.json")
    warmup = json.loads((second / "warmup.json").read_text(encoding="utf-8"))
    reconciliation = json.loads((second / "reconciliation.json").read_text(encoding="utf-8"))
    assert warmup["condition"] == "warmup" and warmup["index"] is None and warmup["http_status"] == 200
    assert reconciliation["resumed_from_index"] == 3 and [r["file"] for r in reconciliation["failed_rows"]] == ["003.json"]
    report = load.score(second)
    assert report["attempted"] == report["answered"] == 41 and report["stopped_early"] is False
    with pytest.raises(ValueError, match="nothing to resume"):
        load_resume.resume(second, tmp_path / "run-3", inputs["corpus_path"], inputs["pool_paths"],
                           inputs["frame_path"], inputs["approval_path"], reason="again", send=Provider(),
                           sleep=lambda s: None)
