"""The compact-menu S1 context: the menu's own request shape, its pending question in the same call, its view.

These exercise the wiring, not the labels: one request per turn with the menu's state, a fourth question on a
turn that offers help, S1's own answer read back from it, and the measured positions the adapter keeps. No
network, no Unity, no key.
"""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from loop_fakes import choices
from s1_fakes import Send, approval
from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceSet
from playtest.core.context import build_context, canonical
from playtest.runner import menu
from playtest.runner.options.memory import OutcomeMemory
from playtest.runner.s1 import BATCH_INSTRUCTIONS, SystemOneS1

ARM = "liquid-d1-ultra"
MODEL = "d1-ultra"
CELL = 0.5

MENU_STATE = {"objective", "progress", "directive", "pending_help", "facts", "game_view", "set_version",
              "built_from_seq", "choices"}
PENDING_IDS = {"c1", "c2", "c3", "w"}  # the offered actions and the wait: the executable choices


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    for name in [n for n in os.environ if n.endswith("_API_KEY")]:
        monkeypatch.delenv(name)


def observation(*, position=None, seq: int = 7, session: str = "s") -> Observation:
    public: dict[str, Any] = {}
    if position is not None:
        public["controlled"] = {"object_id": "o", "path": "p", "position": list(position),
                                "velocity": None, "source": "rigidbody"}
    return Observation(session=session, seq=seq, game_s=3.0,
                       wall_utc=datetime(2026, 11, 1, tzinfo=timezone.utc), clock_mode=ClockMode.realtime,
                       scene="a", playing=True, paused=False, time_scale=1.0, frame=9, public=public)


def context(*, position=None, seq: int = 7, session: str = "s", offered=None, **extra: Any) -> dict[str, Any]:
    built = build_context(
        objective="Exercise the controls.", observation=observation(position=position, seq=seq, session=session),
        choices=ChoiceSet(set_version=4, built_from_seq=seq,
                          choices=list(offered) if offered is not None else choices()))
    if session is not None:
        # The loop's payload does not echo the bridge session; a caller that wants the fallback to see it says so.
        built["observation"]["session"] = session
    built.update(extra)
    return built


def adapter(tmp_path: Path, *replies: Any, memory: OutcomeMemory | None = None, **options: Any):
    limits = approval()
    limits["arms"] = {ARM: limits["arms"]["mammoth-1"]}
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    send = Send(tmp_path, *replies)
    port = SystemOneS1(ledger, max_calls=10, send=send, arm=ARM, s1_context="compact-menu",
                       menu_memory=memory if memory is not None else OutcomeMemory(cell=CELL), **options)
    return port, send, ledger


def decide(port: SystemOneS1, ctx: dict[str, Any], **options: Any):
    return asyncio.run(port.decide(ctx, **options))


def test_a_menu_turn_sends_one_request_with_the_menus_own_state(tmp_path: Path):
    port, send, ledger = adapter(tmp_path, lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None))
    plain = [choice for choice in choices() if choice.id in ("c1", "c2", "c3", "w", "f")]
    ctx = context(offered=plain)
    got = decide(port, ctx)
    body = send.bodies[0]
    assert set(body) == {"model", "questions", "state"} and body["model"] == MODEL
    state = body["state"]
    assert set(state) == MENU_STATE
    assert state["objective"] == ctx["objective"] and state["set_version"] == 4 and state["built_from_seq"] == 7
    assert "observation" not in canonical(state) and "recent_history" not in canonical(state)
    # No help offered, so the three menu questions over the same labels and no pending one: the payload
    # carries no `instructions` at all.
    assert list(body["questions"]) == [menu.PRIMARY, menu.BLOCKED_QUESTION, menu.SUCCEEDED_QUESTION]
    assert body["questions"][menu.PRIMARY]["instructions"] == menu.MENU_INSTRUCTIONS
    labels = {item["id"]: item["label"] for item in state["choices"]}
    assert body["questions"][menu.PRIMARY]["criteria"] == labels
    assert got.answer["choice_id"] == "c1" and got.answer["set_version"] == 4 and "while_pending" not in got.answer
    assert len(send.bodies) == 1 == len(got.calls) and got.failure is None
    assert got.calls[0].request_sha256 == digest(canonical(body))
    assert ledger.arm_rows()[0]["question"] == menu.PRIMARY
    # The hash names the texts this mode sends, including the pending question it can add.
    assert port.prompt_sha256 == digest(canonical({
        menu.PRIMARY: menu.MENU_INSTRUCTIONS, menu.BLOCKED_QUESTION: menu.IF_BLOCKED,
        menu.SUCCEEDED_QUESTION: menu.IF_SUCCEEDED, "while_pending": BATCH_INSTRUCTIONS}))
    assert port.s1_context == "compact-menu"


def test_an_escalate_turn_asks_four_questions_and_reads_the_batched_answer(tmp_path: Path):
    seen: dict[str, Any] = {}

    def response(body):
        seen["body"] = body
        return 200, {"answers": {
            "next_action": {"type": "choice", "choice": "h"},
            "if_blocked": {"type": "choice", "choice": "c2"},
            "if_succeeded": {"type": "choice", "choice": "c3"},
            "while_pending": {"type": "choice", "choice": "c2"}}}, 10.0, None

    port, send, ledger = adapter(tmp_path, response)
    got = decide(port, context())
    body = send.bodies[0]
    assert list(body["questions"]) == [menu.PRIMARY, menu.BLOCKED_QUESTION, menu.SUCCEEDED_QUESTION, "while_pending"]
    pending = body["questions"]["while_pending"]
    assert pending["instructions"] == BATCH_INSTRUCTIONS
    labels = {item["id"]: item["label"] for item in body["state"]["choices"]}
    assert set(pending["criteria"]) == PENDING_IDS
    assert pending["criteria"] == {choice_id: labels[choice_id] for choice_id in PENDING_IDS}
    # One call, and S1's own answer is the while_pending the decision carries: never the policy default.
    assert len(send.bodies) == 1 == len(got.calls) == len(ledger.arm_rows())
    assert got.answer["choice_id"] == "h" and got.answer["while_pending"] == {"mode": "continue", "choice_id": "c2"}
    assert ledger.arm_rows()[0]["conditional_answer"]["condition"] == "met"
    # The contingent answers stay in the trace's response, exactly as the provider sent them.
    answers = got.calls[0].response["answers"]
    assert answers["if_blocked"]["choice"] == "c2" and answers["if_succeeded"]["choice"] == "c3"
    assert seen["body"] is not None


def test_a_wait_choice_while_pending_maps_to_wait(tmp_path: Path):
    def response(body):
        return 200, {"answers": {"next_action": {"type": "choice", "choice": "d"},
                                 "while_pending": {"type": "choice", "choice": "w"}}}, 10.0, None

    port, send, _ = adapter(tmp_path, response)
    got = decide(port, context())
    assert got.answer["while_pending"] == {"mode": "wait"} and len(send.bodies) == 1


def test_a_turn_without_help_offered_asks_no_pending_question(tmp_path: Path):
    offered = [choice for choice in choices() if choice.id in ("c1", "w", "f")]
    port, send, _ = adapter(tmp_path, lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None))
    got = decide(port, context(offered=offered))
    assert list(send.bodies[0]["questions"]) == [menu.PRIMARY, menu.BLOCKED_QUESTION, menu.SUCCEEDED_QUESTION]
    assert "while_pending" not in got.answer


@pytest.mark.parametrize("options,match", [
    ({"layout": "dup-state-ref/1"}, "request layout"),
    ({"compact_state": "object"}, "compact state codec"),
    ({"option_order": "manifest", "order_seed": 1}, "option ordering"),
    ({"arm": "meta-muse-spark-s1"}, "own user text"),
])
def test_menu_mode_refuses_combinations_it_cannot_hold(tmp_path: Path, options: dict, match: str):
    limits = approval()
    limits["arms"] = {ARM: limits["arms"]["mammoth-1"]}
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    with pytest.raises(ValueError, match=match):
        SystemOneS1(ledger, max_calls=1, send=Send(tmp_path), s1_context="compact-menu",
                    menu_memory=OutcomeMemory(cell=CELL), **{**({"arm": ARM}), **options})
    assert not list((tmp_path / "calls").glob("*.json"))


def test_menu_mode_needs_the_options_layers_memory(tmp_path: Path):
    limits = approval()
    limits["arms"] = {ARM: limits["arms"]["mammoth-1"]}
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    with pytest.raises(ValueError, match="outcome memory"):
        SystemOneS1(ledger, max_calls=1, send=Send(tmp_path), arm=ARM, s1_context="compact-menu")
    with pytest.raises(ValueError, match="unknown S1 context"):
        SystemOneS1(ledger, max_calls=1, arm=ARM, s1_context="compact")
    # The default mode is untouched by all of this.
    plain = SystemOneS1(ledger, max_calls=1, arm=ARM, send=Send(tmp_path))
    assert plain.s1_context == "full" and "observation" not in plain._prompts


def test_positions_accumulate_across_turns_and_clear_at_a_reset(tmp_path: Path):
    port, send, _ = adapter(tmp_path, *([lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None)] * 4))
    decide(port, context(position=(0.0, 0.0, 0.0)))
    decide(port, context(position=(5.0, 0.0, 0.0)))
    decide(port, context(position=(9.0, 0.0, 0.0), reset_summary={"turn": 2, "to_generation": "g2"}))
    decide(port, context(position=(11.0, 0.0, 0.0)))
    progress = [body["state"]["progress"] for body in send.bodies]
    assert "furthest +x 0.0 (start 0.0)" in progress[0]
    assert "furthest +x 5.0 (start 0.0)" in progress[1]
    # The turn after a restart starts its own record: no distance spans two generations.
    assert "furthest +x 9.0 (start 9.0)" in progress[2]
    assert "furthest +x 11.0 (start 9.0)" in progress[3]


def test_a_changed_session_id_also_clears_the_position_history(tmp_path: Path):
    port, send, _ = adapter(tmp_path, *([lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None)] * 2))
    decide(port, context(position=(0.0, 0.0, 0.0), session="s1"))
    decide(port, context(position=(7.0, 0.0, 0.0), session="s2"))
    assert "furthest +x 7.0 (start 7.0)" in send.bodies[1]["state"]["progress"]


def test_the_menu_carries_the_same_game_view_note_and_image(tmp_path: Path):
    raw = b"a 640x480 frame"
    ctx = context(game_view="The attached image is the current game view.",
                  game_view_sha256=digest(raw), game_view_status="sent")
    port, send, _ = adapter(tmp_path, lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None), game_view=True)
    got = decide(port, ctx, image=raw)
    body = send.bodies[0]
    assert body["state"]["game_view"] == ctx["game_view"]
    assert body["images"] == ["data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")]
    assert port.view_counts == {"enabled": True, "sent": 1, "omitted": 0, "stale": 0}
    assert got.answer["choice_id"] == "c1"
    # A menu turn that omits the image is counted as omitted and still sends the note.
    asyncio.run(port.decide(context(game_view="Game view unknown; no image attached.",
                                    game_view_sha256=None, game_view_status="missing")))
    assert "images" not in send.bodies[1] and port.view_counts["omitted"] == 1


def test_contingent_answers_do_not_relabel_the_primary_call_or_replace_its_choice(tmp_path):
    # Different heads intentionally disagree; only next_action selects this turn's choice.
    helper_id = next(c.id for c in choices() if c.kind.value == "escalate")
    reply = lambda body: (200, {"answers": {
        "if_succeeded": {"type": "choice", "choice": "c2"},
        "if_blocked": {"type": "choice", "choice": helper_id},
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None)
    port, send, ledger = adapter(tmp_path, reply, contingent=True)
    got = decide(port, context())
    assert got.answer["choice_id"] == "c1"
    assert got.answer["contingent"] == {"if_blocked": helper_id, "if_succeeded": "c2"}
    assert len(got.calls) == 1 and got.calls[0].question == "next_action"
    assert ledger.arm_rows()[0]["question"] == "next_action"
    assert got.calls[0].request_sha256 == digest(canonical(send.bodies[0]))


def test_measured_restart_counts_survive_position_history_reset(tmp_path):
    from types import SimpleNamespace
    from playtest.contracts.loop import Choice, ChoiceKind, TurnOutcome
    reply = lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None)
    port, send, _ = adapter(tmp_path, reply)
    selected = Choice(id="restart", kind=ChoiceKind.restart_play_mode, label="Restart", source="core")
    for status in ("ready", "failed"):
        record = SimpleNamespace(effective_choice="restart", outcome=TurnOutcome.restarted,
                                 operation=SimpleNamespace(status=status))
        port.note_turn(SimpleNamespace(selected=selected, record=record))
    decide(port, context(position=(9, 0, 0), reset_summary={"to_generation": "new"}))
    state = send.bodies[0]["state"]
    assert state["run_resets"] == {"requested": 2, "ready": 1}
    assert "start 9.0" in state["progress"]  # reset coordinates stay separate from run-wide counts
    assert "escalate" not in state["run_resets"]  # measured counts do not select or recommend help
