"""The live smoke test runs the production loop; a stale, invalid or failed turn is recorded, never repaired."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from fake_game import FakeGame
from playtest.bench import live
from playtest.contracts.bridge import RejectReason
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource
from playtest.core.adherence import check_adherence
from playtest.core.bridge_client import BridgeClient

TOKEN = "live-test-token"


def _offered() -> list[Choice]:
    label = "Request a 0.1-second hold of {}; calibrated net displacement {} world units (sample variance 0.000100, n=2)."
    controls = [
        Choice(id="c1", kind=ChoiceKind.action, label=label.format("a", "6.000"), action="shift",
               params={"delta": [1.0, 0.0, 0.0]}, source=ChoiceSource.manifest),
        Choice(id="c2", kind=ChoiceKind.action, label=label.format("b", "0.000"), action="increment",
               source=ChoiceSource.manifest)]
    return controls + live.core_choices()


def _approval(calls: int = 12) -> dict:
    limit = {"max_calls": calls, "max_cost_usd": 0.01, "per_call_max_usd": 0.0001, "verified_balance_usd": 1,
             "balance_checked_at_utc": "2026-01-01T00:00:00Z", "price_valid_until_utc": "2099-01-01T00:00:00Z"}
    return {"approved_by": "Test User", "approved_at_utc": "2026-01-01T00:00:00Z", "max_calls": calls,
            "max_cost_usd": 0.01, "arms": {"mammoth-1": limit}}


def _answer(criteria: dict, choice: str):
    others = len(criteria) - 1
    probabilities = {item: (0.9 if item == choice else 0.1 / others) for item in criteria}
    return 200, {"model": "mammoth-1", "answers": {"next_action": {"type": "choice", "choice": choice,
                                                                  "probabilities": probabilities}}}, 12.0, None


class Model:
    """A scripted stand-in for the provider. By default it answers the first unchecked control, then `finish`."""

    def __init__(self, choices: dict | None = None, raw: dict | None = None, before: dict | None = None) -> None:
        self.choices, self.raw, self.before = choices or {}, raw or {}, before or {}
        self.bodies: list[dict] = []

    def __call__(self, arm: str, body: dict):
        index = len(self.bodies)
        self.bodies.append(body)
        if index in self.before:
            self.before[index]()
        if index in self.raw:
            return self.raw[index]
        criteria = body["questions"]["next_action"]["criteria"]
        accept, _ = live.accepted_ids([k for k in criteria if re.fullmatch(r"c\d+", k)], body["state"]["facts"])
        return _answer(criteria, self.choices.get(index, sorted(accept)[0]))


def _episode(tmp_path: Path, model: Model, *, max_turns: int = 8, approval: dict | None = None,
             setup=None, monkeypatch=None):
    """Run one episode of the production loop against the fake game. Returns what a test needs to assert on."""
    out: dict = {}

    async def main() -> None:
        game = FakeGame(token=TOKEN)
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN)
        await client.connect()
        if setup:
            setup(game, model)
        s1 = live.LiveS1(tmp_path / "run", approval or _approval(), send=model)
        section = {"episode": 1, "ids": [], "choices": [], "turns": [], "summary": None, "adherence": None}
        count = {"n": 0}

        async def snapshot():
            count["n"] += 1
            return {"inspection_seq": count["n"], "game_time_s": float(count["n"]),
                    "position": [6.0 * game.applied().count("shift"), 0.0, 0.0]}

        try:
            out["loop"] = await live.run_episode(live.Capturing(client), episode=1, offered=_offered(), snapshot=snapshot,
                                                 s1=s1, max_turns=max_turns, section=section, save=lambda: None,
                                                 clock=lambda: 0.0)
        except live.ProviderStop as exc:
            out["stop"] = exc
        finally:
            out["applied"], out["section"], out["s1"] = list(game.applied()), section, s1
            await client.aclose()
            await game.stop()

    asyncio.run(main())
    return out


def test_an_episode_runs_the_production_loop_to_finish(tmp_path: Path) -> None:
    out = _episode(tmp_path, Model())
    turns = out["section"]["turns"]
    assert [t["outcome"] for t in turns] == ["executed", "executed", "finished"]
    assert [t["effective_choice"] for t in turns] == ["c1", "c2", "finish"]
    assert out["applied"] == ["shift", "increment"]
    assert all(t["acceptable"] is True and t["miss"] is None for t in turns)
    assert turns[1]["facts"][0].startswith("c1 (shift) held for ") and "6.0 world units" in turns[1]["facts"][0]
    assert any(f.startswith("c2 (increment) held for ") for f in turns[2]["facts"])
    assert turns[0]["expected_displacement"] == 6.0 and turns[0]["observed_displacement"] == 6.0
    assert turns[1]["expected_displacement"] == 0.0 and turns[1]["observed_displacement"] == 0.0
    summary = out["section"]["summary"]
    assert summary["finished"] and summary["finished_with_all_controls"] and summary["controls_completed"] == 2
    assert summary["top1"] == {"hits": 3, "scored": 3} and summary["stale"] == 0 and summary["invalid"] == 0
    assert out["section"]["adherence"] == {"rate": 1.0, "violations": []}
    assert check_adherence(out["loop"].turns) == []
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "run" / "calls").glob("*.json")]
    assert len(rows) == 3 and all(r["status"] == "finished" and r["http_status"] == 200 for r in rows)
    assert all(t["age_s"] is not None and 0 <= t["age_s"] < 5 for t in turns)


def test_a_stale_rejection_is_recorded_and_the_control_stays_unchecked(tmp_path: Path) -> None:
    def setup(game, model):
        model.before = {0: lambda: setattr(game, "reject_acts", RejectReason.stale_age),
                        1: lambda: setattr(game, "reject_acts", None)}

    out = _episode(tmp_path, Model(), setup=setup)
    turns = out["section"]["turns"]
    assert [t["outcome"] for t in turns] == ["rejected_stale", "executed", "executed", "finished"]
    assert turns[0]["executed_action"] is None and turns[0]["execution"]["reject_reason"] == "stale_age"
    assert turns[0]["acceptable"] is True and turns[0]["miss"] is None
    assert "c1 (shift) was rejected by the bridge (stale_age); no input was sent." in turns[1]["facts"]
    assert turns[1]["completed_before"] == 0 and "c1" in turns[1]["accept"]
    assert out["applied"] == ["shift", "increment"]
    assert out["section"]["summary"]["stale"] == 1 and out["section"]["summary"]["finished_with_all_controls"]
    assert out["section"]["adherence"]["violations"] == []


def test_a_help_turn_defaults_to_wait_and_pending_help_reaches_the_next_context(tmp_path: Path) -> None:
    model = Model(choices={0: "escalate"})
    out = _episode(tmp_path, model)
    turns = out["section"]["turns"]
    assert turns[0]["outcome"] == "escalated" and turns[0]["policy_default"] is True
    assert turns[0]["executed_action"] is None and turns[0]["miss"] == "escalate" and turns[0]["acceptable"] is False
    assert model.bodies[0]["state"]["pending_help"] is None
    help_state = model.bodies[1]["state"]["pending_help"]
    assert help_state["kind"] == "escalate" and help_state["choice_id"] == "escalate"
    assert out["section"]["summary"]["misses"] == {"escalate": 1} and out["section"]["summary"]["pending_helper_at_end"]
    assert out["section"]["adherence"]["violations"] == []


def test_an_invalid_answer_rejects_one_turn_and_the_run_continues(tmp_path: Path) -> None:
    bad = (200, {"model": "mammoth-1", "answers": {"next_action": {"type": "choice", "choice": "zz"}}}, 9.0, None)
    out = _episode(tmp_path, Model(raw={0: bad}))
    turns = out["section"]["turns"]
    assert turns[0]["outcome"] == "rejected_invalid" and turns[0]["raw_choice"] is None
    assert turns[0]["acceptable"] is None and turns[0]["miss"] is None and turns[0]["executed_action"] is None
    assert "The last answer did not name an offered choice; no input was sent." in turns[1]["facts"]
    assert out["applied"] == ["shift", "increment"] and out["section"]["summary"]["finished_with_all_controls"]
    assert out["section"]["summary"]["invalid"] == 1 and out["section"]["adherence"]["violations"] == []


def test_a_provider_failure_stops_the_run_with_no_retry(tmp_path: Path) -> None:
    model = Model(raw={1: (None, None, 5.0, "transport error")})
    out = _episode(tmp_path, model)
    assert isinstance(out["stop"], live.ProviderStop) and "no retry" in str(out["stop"])
    assert len(model.bodies) == 2 and out["applied"] == ["shift"]
    rows = sorted((json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "run" / "calls").glob("*.json")),
                  key=lambda r: r["turn"])
    assert [r["http_status"] for r in rows] == [200, None] and rows[1]["problem"] == "transport error"
    assert len(out["section"]["turns"]) == 1


def test_the_call_that_would_exceed_the_envelope_is_never_made(tmp_path: Path) -> None:
    model = Model()
    out = _episode(tmp_path, model, approval=_approval(calls=1))
    assert isinstance(out["stop"], live.ProviderStop) and "envelope" in str(out["stop"])
    assert len(model.bodies) == 1 and out["applied"] == ["shift"]


def test_the_completed_rule_ignores_rejections_and_names_each_miss() -> None:
    facts = ["c1 (shift) held for 1.2 seconds with verified release; net displacement between inspection seq 1 and 2: "
             "6.0 world units.",
             "c2 (increment) was rejected by the bridge (stale_age); no input was sent.",
             "Entity position from inspection seq 2 at game time 2.0: [6.0, 0.0, 0.0] in Unity world units."]
    accept, done = live.accepted_ids(["c1", "c2"], facts)
    assert done == {"c1"} and accept == {"c2"}
    assert live.accepted_ids(["c1"], facts[:1])[0] == {"finish"}
    assert [live.miss_kind(choice, accept, done) for choice in
            ("c2", "c1", "finish", "wait", "escalate", "delegate", "zz", None)] == [
        None, "re-check", "premature-finish", "wait", "escalate", "delegate", "other", None]


def test_main_refuses_without_an_approval_or_a_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("UPA_LIVE_APPROVAL", raising=False)
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    base = ["--project", str(tmp_path / "game"), "--entity", str(tmp_path / "e.json"),
            "--calibration", str(tmp_path / "c.json"), "--output", str(tmp_path / "out" / "trace.json")]
    with pytest.raises(SystemExit) as no_approval:
        live.main(base)
    assert no_approval.value.code == 2
    approval = tmp_path / "approval.json"
    approval.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as no_key:
        live.main([*base, "--approval", str(approval)])
    assert no_key.value.code == 2
    assert not (tmp_path / "out").exists()
