"""The compact menu's `recent`: what each finished turn measurably did, told to the adapter once per turn.

The harness calls `SystemOneS1.note_turn` with the loop's report. An option states its own termination and
duration; a plain action or skill states the verdict the options memory files for it, from the same rule and
the same applied hold; a wait says `wait` and a finish says `finished`. Nothing is ranked, and a result nobody
measured stays `unmeasured`. No network, no Unity, no key.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from loop_fakes import choices
from s1_fakes import Send, approval
from playtest.bench import call_budget
from playtest.contracts.bridge import ExecStatus, ExecutionRecord, Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, TurnOutcome, TurnRecord
from playtest.core.context import build_context
from playtest.core.execution import Execution
from playtest.core.ports import EffectReport, Executed, TurnReport
from playtest.runner import menu
from playtest.runner.options.memory import OutcomeMemory
from playtest.runner.s1 import SystemOneS1

ARM = "liquid-d1-ultra"
CELL = 0.5
# One measured short step, the margin the options memory judges an action's displacement by.
PRIMITIVES = SimpleNamespace(short_step=0.5, directions=())


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    for name in [n for n in os.environ if n.endswith("_API_KEY")]:
        monkeypatch.delenv(name)


def observation(seq: int = 7) -> Observation:
    return Observation(session="s", seq=seq, game_s=3.0,
                       wall_utc=datetime(2026, 11, 1, tzinfo=timezone.utc), clock_mode=ClockMode.realtime,
                       scene="a", playing=True, paused=False, time_scale=1.0, frame=9,
                       public={"controlled": {"object_id": "o", "path": "p", "position": [0.0, 0.0, 0.0],
                                             "velocity": None, "source": "rigidbody"}})


def context(seq: int = 7) -> dict[str, Any]:
    return build_context(objective="Exercise the controls.", observation=observation(seq),
                         choices=ChoiceSet(set_version=4, built_from_seq=seq, choices=choices()))


def adapter(tmp_path: Path, *replies: Any, **options: Any):
    limits = approval()
    limits["arms"] = {ARM: limits["arms"]["mammoth-1"]}
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    send = Send(tmp_path, *replies)
    port = SystemOneS1(ledger, max_calls=10, send=send, arm=ARM, s1_context="compact-menu",
                       menu_memory=OutcomeMemory(cell=CELL), **options)
    return port, send


def turn(turn_no: int, choice_id: str, outcome: TurnOutcome = TurnOutcome.executed,
         executed: str | None = None) -> TurnRecord:
    return TurnRecord(turn=turn_no, set_version=turn_no, based_on_seq=turn_no, raw_choice=choice_id,
                      effective_choice=choice_id,
                      executed_action=executed if executed is not None else choice_id, outcome=outcome)


def option_report(turn_no: int, option_id: str, ended: str, seconds: float | None) -> TurnReport:
    choice = Choice(id=option_id, kind=ChoiceKind.option, option_id=option_id, template="traverse",
                    params={"direction": [1, 0, 0]}, label="traverse", source="manifest")
    return TurnReport(
        record=turn(turn_no, option_id), observation=observation(), selected=choice, problem=None,
        failure=None, help_event=None,
        option_outcome={"option_id": option_id, "template": "traverse", "params": {"direction": [1, 0, 0]},
                        "start_position": [0.0, 0.0, 0.0], "end_position": [1.0, 0.0, 0.0],
                        "displacement": [1.0, 0.0, 0.0], "terminated_by": ended, "duration_s": seconds,
                        "scene": "a"})


def action_report(turn_no: int, choice_id: str, moved: list[float] | None,
                  applied: float | None = 0.25, kind=ChoiceKind.action) -> TurnReport:
    choice = Choice(id=choice_id, kind=kind, action=choice_id if kind is ChoiceKind.action else None,
                    skill=choice_id if kind is ChoiceKind.skill else None, label=choice_id, source="manifest")
    receipt = ExecutionRecord(session="s", seq=3, game_s=1.0,
                              wall_utc=datetime(2026, 11, 1, tzinfo=timezone.utc), action_id="a",
                              status=ExecStatus.completed, applied_duration_s=applied, release_verified=True)
    observed = (None if moved is None
                else {"displacement": list(moved), "applied_duration_s": applied})
    executed = Executed(purpose="choice", choice=choice,
                        execution=Execution(request={"choice_id": choice_id}, records=(receipt,)),
                        effect=EffectReport(observed=observed))
    return TurnReport(record=turn(turn_no, choice_id), observation=observation(), selected=choice,
                      problem=None, failure=None, help_event=None, executed=(executed,))


def wait_report(turn_no: int) -> TurnReport:
    choice = Choice(id="w", kind=ChoiceKind.wait, label="Wait", source="core")
    return TurnReport(record=turn(turn_no, "w", outcome=TurnOutcome.finished, executed=None),
                      observation=observation(), selected=choice, problem=None, failure=None,
                      help_event=None)


def finish_report(turn_no: int) -> TurnReport:
    choice = Choice(id="f", kind=ChoiceKind.finish, label="Finish", source="core")
    return TurnReport(record=turn(turn_no, "f", outcome=TurnOutcome.finished, executed=None),
                      observation=observation(), selected=choice, problem=None, failure=None,
                      help_event=None)


def decide(port: SystemOneS1, ctx: dict[str, Any]):
    return asyncio.run(port.decide(ctx))


def test_recent_carries_the_six_newest_turns_with_what_each_measured(tmp_path: Path):
    port, send = adapter(tmp_path, lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None))
    reports = [
        option_report(0, "traverse+x", "blocked", 0.75),
        action_report(1, "c1", [0.6, 0.0, 0.0]),      # no calibrated criterion: measured motion only
        action_report(2, "c2", [0.1, 0.0, 0.0]),
        action_report(3, "c3", None),                 # no measured displacement: unmeasured
        option_report(4, "traverse-x", "fell", None), # an option ending over an unmeasured duration
        wait_report(5),
        finish_report(6),
    ]
    for report in reports:
        port.note_turn(report, PRIMITIVES)
    decide(port, context())
    rows = send.bodies[0]["state"]["recent"]
    assert [row["id"] for row in rows] == ["c1", "c2", "c3", "traverse-x", "w", "f"]
    assert [row["result"] for row in rows] == ["unmeasured", "unmeasured", "unmeasured", "fell", "wait", "finished"]
    assert [row["s"] for row in rows] == [0.25, 0.25, None, None, None, None]
    assert all(row["execution_status"] == "completed" and row["goal_outcome"] is None for row in rows[:3])
    assert [row.get("displacement") for row in rows[:3]] == [[0.6, 0.0, 0.0], [0.1, 0.0, 0.0], None]
    assert all(set(row) == {"id", "s", "result"} for row in rows[3:])
    # The menu's own question states the field as a fact about the state, not as a rule to follow.
    assert "`recent` lists the last choices and what each measurably did." in menu.MENU_INSTRUCTIONS


def test_an_option_states_its_own_termination_and_duration(tmp_path: Path):
    port, send = adapter(tmp_path, lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None))
    port.note_turn(option_report(0, "hop+x", "state_changed", 1.25), PRIMITIVES)
    decide(port, context())
    assert send.bodies[0]["state"]["recent"] == [{"id": "hop+x", "s": 1.25, "result": "state_changed"}]


def test_a_turn_nobody_reported_leaves_the_state_without_recent(tmp_path: Path):
    port, send = adapter(tmp_path, lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None))
    decide(port, context())
    assert "recent" not in send.bodies[0]["state"]


def test_a_plain_action_without_measured_primitives_stays_unmeasured(tmp_path: Path):
    """No measured margin, no verdict: the entry says unmeasured rather than guessing one."""
    port, send = adapter(tmp_path, lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None))
    port.note_turn(action_report(0, "c1", [0.6, 0.0, 0.0]), None)
    decide(port, context())
    assert send.bodies[0]["state"]["recent"] == [{"id": "c1", "s": None, "result": "unmeasured",
        "displacement": [0.6, 0.0, 0.0], "execution_status": "completed", "goal_outcome": None}]


def test_a_large_skill_drop_is_a_fall_without_claiming_a_goal_outcome(tmp_path):
    """A drop past the measured fall bound is `fell` for a skill, as for an option; the goal stays unknown."""
    port, send = adapter(tmp_path, lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None))
    port.note_turn(action_report(0, "combo", [6.0, -28.62, 0.0], kind=ChoiceKind.skill), PRIMITIVES)
    decide(port, context())
    row = send.bodies[0]["state"]["recent"][0]
    assert row["result"] == "fell" and row["goal_outcome"] is None
    assert row["displacement"] == [6.0, -28.62, 0.0]
    assert row["execution_status"] == "completed"  # input completion is a separate fact


def test_the_escalate_label_counts_requests_and_directives_across_turns(tmp_path: Path):
    """Each distinct opened request and directive id counts once; repeats of the same one do not."""
    port, send = adapter(tmp_path, *[lambda body: (200, {"answers": {
        "next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None)] * 3)
    first = context(7)
    first["pending_help"] = {"kind": "escalate", "choice_id": "h", "opened_turn": 3}
    decide(port, first)
    second = context(8)
    second["pending_help"] = {"kind": "escalate", "choice_id": "h", "opened_turn": 3}
    second["directive"] = {"directive_id": "d-3", "text": "Hop the gap."}
    decide(port, second)
    third = context(9)
    third["directive"] = {"directive_id": "d-3", "text": "Hop the gap."}
    decide(port, third)
    labels = [body["questions"]["next_action"]["criteria"]["h"] for body in send.bodies]
    assert labels[0].endswith("so far: 1 asked, 0 gave advice; one pending")
    assert labels[1].endswith("so far: 1 asked, 1 gave advice; one pending")
    assert labels[2].endswith("so far: 1 asked, 1 gave advice")


def test_a_met_directive_is_held_until_its_deadline_and_never_across_a_reset(tmp_path: Path):
    """Opted in, the newest directive that closed met is shown until its deadline; one from before a reset is not."""
    replies = [lambda body: (200, {"answers": {"next_action": {"type": "choice", "choice": "c1"}}}, 10.0, None)] * 4
    met = {"directive_id": "d-5", "text": "Hop the gap.", "outcome": "met", "deadline_game_s": 20.0,
           "expected": {"field": "public.grounded", "op": "==", "value": False}}

    def at(seq: int, game_s: float, **extra: Any) -> dict[str, Any]:
        ctx = context(seq)
        ctx["as_of"]["game_s"] = game_s
        ctx["directive_history"] = [met]
        ctx.update(extra)
        return ctx

    port, send = adapter(tmp_path, *replies, hold_met_directive=True)
    for ctx in (at(7, 10.0), at(8, 21.0), at(9, 5.0, reset_summary={"from_generation": "a", "to_generation": "b"})):
        decide(port, ctx)
    shown = [body["state"]["directive"] for body in send.bodies]
    assert shown[0] == {"id": "d-5", "text": "Hop the gap.", "setpoint": "public.grounded == false (met)"}
    assert shown[1] is None                                          # past its deadline
    assert shown[2] is None                                          # listed before the reset: another session
    plain, sent = adapter(tmp_path / "plain", *replies)
    decide(plain, at(7, 10.0))
    assert sent.bodies[0]["state"]["directive"] is None
