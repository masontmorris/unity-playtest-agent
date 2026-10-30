"""Offline checks for the Liquid d1-ultra rate probe: reconstruction, stops, budget, windows."""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probes" / "rate_probe_liquid_d1_ultra.py"
spec = importlib.util.spec_from_file_location("rate_probe_liquid_d1_ultra", SCRIPT)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)

from playtest.bench import call_budget  # noqa: E402
from playtest.bench.record import digest  # noqa: E402
from playtest.core.context import canonical  # noqa: E402


def payload(*, sent: bool = True) -> dict:
    sha = digest(b"fake-jpeg") if sent else None
    return {"objective": "objective", "as_of": {"seq": 7, "game_s": 1.0, "age_s": 0.0, "sample_gap_s": None},
            "observation": {"scene": "s", "playing": True, "paused": False, "time_scale": 1.0,
                            "public": {}, "ui": {"canvases": []}, "reader_errors": []},
            "facts": ["fact"], "directive": None, "directive_history": [], "pending_help": None,
            "choice_set_version": 0,
            "choices": [{"id": "go", "kind": "action", "label": "Go", "action": "go"},
                        {"id": "wait", "kind": "wait", "label": "Wait"},
                        {"id": "help", "kind": "escalate", "label": "Help"}],
            "instructions": "instructions", "game_view": "view" if sent else None,
            "game_view_sha256": sha, "game_view_status": "sent" if sent else "omitted"}


def source(question: str = "next_action", *, sent: bool = True) -> dict:
    body = probe.rebuild_body(payload(sent=sent), question, b"fake-jpeg" if sent else None)
    return {"source_trace_seq": 4, "context_trace_seq": 3, "turn": 0, "question": question,
            "based_on_seq": 7, "request_sha256": digest(canonical(body)),
            "game_view_sha256": payload(sent=sent)["game_view_sha256"], "body": body,
            "historical_input_tokens": 100, "historical_cost_usd": 0.0001}


def approval(directory: Path, *, max_cost: str = "0.0018") -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    value = {"approved_by": "Test User", "approved_at_utc": "2026-10-30T00:00:00+00:00",
             "max_calls": 3, "max_cost_usd": max_cost,
             "unverified_balance_override": {"approved_by_user": True, "reason": "test override"},
             "arms": {probe.ARM: {"max_calls": 3, "per_call_max_usd": str(probe.RESERVED_PER_CALL_USD),
                                  "max_cost_usd": max_cost, "verified_balance_usd": None,
                                  "balance_checked_at_utc": None, "allow_unverified_price": True,
                                  "estimated_price_basis": "test estimate", "estimate_use_by_utc": later}}}
    (directory / "approval.json").write_text(json.dumps(value), encoding="utf-8")
    return value


def ledger(tmp_path: Path) -> call_budget.Ledger:
    return call_budget.Ledger(tmp_path / "run", approval(tmp_path), probe.ARM)


def ok_reply(question: str = "next_action", **usage) -> tuple[int, dict, None]:
    base = {"input_tokens": 5000, "output_tokens": 0, "cost": 0.0002}
    base.update(usage)
    return 200, {"model": "d1-ultra", "usage": base,
                 "answers": {question: {"type": "choice", "choice": "go" if question == "next_action" else "wait"}}}, None


def test_rebuild_matches_the_s1_encoding_for_both_questions() -> None:
    primary = probe.rebuild_body(payload(), "next_action", b"fake-jpeg")
    assert set(primary["questions"]["next_action"]["criteria"]) == {"go", "wait", "help"}
    assert primary["images"][0].startswith("data:image/jpeg;base64,")
    follow = probe.rebuild_body(payload(), "while_pending", b"fake-jpeg")
    assert set(follow["questions"]["while_pending"]["criteria"]) == {"go", "wait"}
    assert follow["questions"]["while_pending"]["instructions"] == probe.FOLLOW_UP_INSTRUCTIONS
    bare = probe.rebuild_body(payload(sent=False), "next_action", None)
    assert "images" not in bare


def test_corpus_verification_rejects_a_tampered_frame(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    image = b"fake-jpeg"
    (frames / "frame-000001.jpg").write_bytes(image)
    sha = digest(image)
    body_payload = payload()
    body_payload["game_view_sha256"] = sha
    trace = tmp_path / "trace.jsonl"
    body = probe.rebuild_body(body_payload, "next_action", image)
    rows = [{"trace_seq": 3, "kind": "context", "refs": [], "body": {"sha256": digest(canonical(body_payload)),
                                                                     "payload": body_payload}},
            {"trace_seq": 4, "kind": "s1_call", "turn": 0, "refs": [3],
             "body": {"question": "next_action", "request_sha256": digest(canonical(body)),
                      "usage": {"input_tokens": 100, "cost": 0.0001}}}]
    trace.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    recording = tmp_path / "recording.json"
    recording.write_text(json.dumps({"frames": [{"file": "frame-000001.jpg", "sha256": sha}]}), encoding="utf-8")
    assert len(probe.load_corpus(trace, frames, recording)["sources"]) == 1
    (frames / "frame-000001.jpg").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="no longer hash"):
        probe.load_corpus(trace, frames, recording)


def test_effective_cap_and_window_qualification() -> None:
    assert probe.effective_max_calls() == 166
    assert probe.full_window_tpm([(10.0, 1000), (20.0, 1000)], 0.0) is None
    points = [(float(t), 1000) for t in range(0, 121, 10)]
    assert probe.full_window_tpm(points, 0.0) == 6000  # trailing (t-60, t]: six 10 s-spaced calls
    summary = probe.summarize([{"probe_index": 0, "http_status": 200, "stop_reason": None,
                                "input_tokens": 10, "total_tokens": 10, "cost_usd": 0.0001,
                                "reserved_usd": "0.0006", "latency_ms": 5.0, "completion_offset_s": 1.0}])
    assert summary["full_60s_window_tokens_per_minute"] is None
    assert summary["recommended_tokens_per_minute"] is None


def test_first_429_stops_without_retry(tmp_path: Path) -> None:
    calls: list[dict] = []

    async def send(body: dict, timeout_s: float):
        calls.append(body)
        return 429, {"usage": {"input_tokens": 1, "cost": 0.0001}}, "HTTP error"

    summary = asyncio.run(probe.execute([source()], ledger(tmp_path), send, max_calls=5))
    assert len(calls) == 1 and summary["attempted"] == 1 and summary["ended_by"] == "rate_limited_429"


def test_missing_cost_keeps_the_reservation_and_stops(tmp_path: Path) -> None:
    async def send(body: dict, timeout_s: float):
        return 200, {"model": "d1-ultra", "usage": {"input_tokens": 10},
                     "answers": {"next_action": {"type": "choice", "choice": "go"}}}, None

    summary = asyncio.run(probe.execute([source()], ledger(tmp_path), send, max_calls=5))
    assert summary["ended_by"] == "missing_usage_or_cost"
    assert summary["cost_usd"]["calls_without_reported_cost"] == 1
    assert summary["cost_usd"]["conservative_total"] == "0.0006"


def test_cost_above_the_bound_stops_and_budget_caps_predispatch(tmp_path: Path) -> None:
    async def dear(body: dict, timeout_s: float):
        return ok_reply(cost=0.0007)

    summary = asyncio.run(probe.execute([source()], ledger(tmp_path), dear, max_calls=5))
    assert summary["ended_by"] == "cost_exceeded_reserved_bound" and summary["attempted"] == 1

    async def cheap(body: dict, timeout_s: float):
        return ok_reply()

    capped = asyncio.run(probe.execute([source()], ledger(tmp_path / "b"), cheap, max_calls=10))
    # The test approval holds $0.0018: exactly three reservations fit, the fourth never sends.
    assert capped["attempted"] == 3 and capped["ended_by"] == "budget_exhausted"


def test_in_flight_call_is_bounded_by_the_remaining_wall(tmp_path: Path) -> None:
    async def slow(body: dict, timeout_s: float):
        await asyncio.sleep(1.0)
        return ok_reply()

    summary = asyncio.run(probe.execute([source()], ledger(tmp_path), slow, max_calls=5, max_wall_s=0.05))
    assert summary["attempted"] == 1 and summary["ended_by"] == "deadline_exceeded_in_flight"


def caps_approval(*, max_calls: int, max_cost: str) -> dict:
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    return {"approved_by": "Test User", "approved_at_utc": "2026-10-30T00:00:00+00:00",
            "max_calls": max_calls, "max_cost_usd": max_cost,
            "unverified_balance_override": {"approved_by_user": True, "reason": "test override"},
            "arms": {probe.ARM: {"max_calls": max_calls, "per_call_max_usd": str(probe.RESERVED_PER_CALL_USD),
                                 "max_cost_usd": max_cost, "verified_balance_usd": None,
                                 "balance_checked_at_utc": None, "allow_unverified_price": True,
                                 "estimated_price_basis": "test estimate", "estimate_use_by_utc": later}}}


def test_validated_caps_followup_original_and_hard_maxima() -> None:
    assert probe.validated_caps(caps_approval(max_calls=100, max_cost="0.06")) == (100, Decimal("0.06"))
    assert probe.validated_caps(caps_approval(max_calls=166, max_cost="0.10")) == (166, Decimal("0.10"))
    with pytest.raises(ValueError, match="hard"):
        probe.validated_caps(caps_approval(max_calls=100, max_cost="0.11"))
    with pytest.raises(ValueError, match="hard"):
        probe.validated_caps(caps_approval(max_calls=301, max_cost="0.10"))


def test_missing_output_tokens_stays_unknown_and_stops(tmp_path: Path) -> None:
    assert probe.usage_of({"usage": {"input_tokens": 5, "cost": 0.0001}})[:2] == (5, None)

    async def send(body: dict, timeout_s: float):
        return 200, {"model": "d1-ultra", "usage": {"input_tokens": 5000, "cost": 0.0002},
                     "answers": {"next_action": {"type": "choice", "choice": "go"}}}, None

    summary = asyncio.run(probe.execute([source()], ledger(tmp_path), send, max_calls=5))
    assert summary["attempted"] == 1 and summary["ended_by"] == "missing_usage_or_cost"
    assert summary["output_tokens"]["total"] is None and summary["recommended_tokens_per_minute"] is None


def test_start_spacing_with_fake_clock_and_no_catchup(tmp_path: Path) -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        async def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    dispatches: list[float] = []

    async def send(body: dict, timeout_s: float):
        dispatches.append(clock.now)
        clock.now += 0.3
        return ok_reply()

    summary = asyncio.run(probe.execute([source()], ledger(tmp_path), send, max_calls=3,
                                        min_interval_s=1.0, monotonic=clock.monotonic, sleep=clock.sleep))
    assert summary["attempted"] == 3 and summary["succeeded"] == 3
    assert dispatches == [0.0, 1.0, 2.0]
    assert all(b - a >= 1.0 for a, b in zip(dispatches, dispatches[1:]))


def test_deadline_after_reservation_records_reserved_but_unsent(tmp_path: Path) -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    inner = ledger(tmp_path)
    sends: list[dict] = []

    class AdvancingLedger:
        calls = 0

        def reserve(self, **kwargs):
            reservation = inner.reserve(**kwargs)
            self.calls += 1
            if self.calls == 2:
                clock.now += 10.0
            return reservation

    async def send(body: dict, timeout_s: float):
        sends.append(body)
        clock.now += 0.1
        return ok_reply()

    summary = asyncio.run(probe.execute([source()], AdvancingLedger(), send, max_calls=5,
                                        max_wall_s=5.0, monotonic=clock.monotonic))
    assert len(sends) == 1 and summary["attempted"] == 2
    assert summary["ended_by"] == "deadline_exhausted_before_dispatch"
    assert inner.rows[-1]["reserved_but_unsent"] is True and inner.rows[-1]["http_status"] is None
    assert summary["recommended_tokens_per_minute"] is None


def test_min_window_recommendation_uses_min_rounded_down_to_10000() -> None:
    rows = []
    for index, at in enumerate(range(10, 101, 10)):
        total = 20000 if index < 6 else 10000
        rows.append({"probe_index": index, "http_status": 200, "stop_reason": None,
                     "resolved_model": "d1-ultra", "input_tokens": total, "total_tokens": total,
                     "cost_usd": 0.0002, "reserved_usd": "0.0006", "latency_ms": 100.0,
                     "completion_offset_s": float(at)})
    summary = probe.summarize(rows, expected_calls=10)
    assert summary["full_60s_window_min_tokens_per_minute"] == 80000
    assert summary["full_60s_window_max_tokens_per_minute"] == 120000
    assert summary["recommended_tokens_per_minute"] == 60000
    failed = rows + [{"probe_index": 10, "http_status": 429, "stop_reason": "rate_limited_429",
                      "resolved_model": "d1-ultra", "input_tokens": None, "total_tokens": None,
                      "cost_usd": None, "reserved_usd": "0.0006", "latency_ms": None,
                      "completion_offset_s": 101.0}]
    assert probe.summarize(failed, expected_calls=10)["recommended_tokens_per_minute"] is None


def test_only_named_credentials_are_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIQUID_API_KEY", raising=False)
    monkeypatch.delenv("LIQUID_D1.1_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('OTHER=1\nLIQUID_D1.1_URL="https://example.invalid/x"\nLIQUID_API_KEY=secret\n',
                        encoding="utf-8")
    url, key = probe.load_credentials(env_file)
    assert url.endswith("/x") and key == "secret"
