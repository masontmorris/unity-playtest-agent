"""S1's contingent answers: a branch it named ahead of time runs without asking again.

The option's measured ending selects the branch, but only while nothing else moved the question: an
unchanged scene, no help pending, and the named id still offered by the set this observation built.
Any other ending, a withdrawn id or a second turn in a row asks S1 afresh, and the trace says why.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loop_fakes import choices as base_choices
from loop_fakes import rig, run
from loop_fakes import FakeGame, PositionedGame
from turn_trace_check import problems
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceSet
from playtest.contracts.trace import TraceEntry
from playtest.core.adherence import check_adherence
from playtest.core.decisions import parse_decision
from playtest.core.trace_replay import replay
from playtest.runner import cli


def option() -> Choice:
    return Choice(id="traverse+x", kind="option", label="Traverse", option_id="traverse+x",
                  template="traverse", params={"direction": [1, 0, 0]}, source="manifest")


# The measured step a place is filed by, as the runner's option memory supplies it.
CELL = 0.5


async def placed(*steps, **kwargs):
    """A rig whose fake game publishes a measured pose, and the loop bound to the memory's cell."""
    kwargs.setdefault("game_class", PositionedGame)
    kwargs.setdefault("contingent_cell", CELL)
    return await rig(*steps, **kwargs)


class Port:
    """An option port that ends the traverse in one scripted way, as the bridge measures it."""

    def __init__(self, ended: str) -> None:
        self.ended = ended

    async def run(self, choice: Choice) -> dict:
        return {"option_id": choice.option_id, "template": choice.template, "params": choice.params,
                "start_position": [0, 0, 0], "end_position": [1, 0, 0], "displacement": 1.0,
                "terminated_by": self.ended, "duration_s": 0.5, "scene": "scene", "receipts": []}


def first(**ids: str) -> dict:
    """Turn zero's answer: select the option and say what to do in the ending it may reach."""
    return {"choice_id": "traverse+x", "contingent": dict(ids)}


def entries(r) -> list[TraceEntry]:
    """The sink's rows as trace entries the replay accepts, with the harness run record put first."""
    rows = [{**row, "run_id": "test"} for row in r.sink.entries]
    rows[0]["body"] = _run_body()
    return [TraceEntry.model_validate(row) for row in rows]


def _run_body() -> dict:
    body = json.loads(Path("fixtures/contracts/trace_entry/valid-run.json").read_text())["body"]
    return {**body, "run_id": "test"}


def decision_body(r, turn: int) -> dict:
    return [row for row in r.sink.of_kind("decision") if row["turn"] == turn][0]["body"]


def calls(r, turn: int) -> list[dict]:
    return [row for row in r.sink.of_kind("s1_call") if row["turn"] == turn]


async def finished(r) -> None:
    await r.loop.stop("finish")


@pytest.mark.parametrize("ended, branch, expected", [
    ("blocked", "if_blocked", "c2"),
    ("succeeded", "if_succeeded", "c3"),
])
def test_a_measured_ending_runs_s1s_branch_with_no_call(ended, branch, expected):
    async def main():
        r = await placed(first(if_blocked="c2", if_succeeded="c3"),
                      choices=[*base_choices(), option()], option_port=Port(ended))
        try:
            await r.turns(1)
            assert r.loop.turns[0].executed_action == "traverse+x"
            await r.turns(1)  # the contingent turn: S1's own answer runs, with no provider call
            assert len(r.s1.contexts) == 1 and not calls(r, 1)
            record = r.loop.turns[1]
            assert (record.raw_choice, record.effective_choice, record.executed_action) == (expected,) * 3
            body = decision_body(r, 1)
            assert (body["source"], body["from_turn"], body["condition"]) == ("contingent", 0, ended)
            assert body["decision"].get("contingent") is None  # the reused answer names no further branch
            assert decision_body(r, 0)["decision"]["contingent"][branch] == expected
            assert not check_adherence(r.loop.turns)
            await finished(r)
            assert not problems(r.sink.entries)
            assert not replay(entries(r))
        finally:
            await r.close()
    run(main)


def test_the_answer_is_bound_to_the_cell_it_was_made_in():
    """A contingent answer runs only in the cell the body stood in when S1 gave it."""
    async def main():
        r = await placed(first(if_succeeded="c3"), "c1",
                         choices=[*base_choices(), option()], option_port=Port("succeeded"))
        try:
            await r.turns(2)
            assert decision_body(r, 0)["cell"] == [0, 0, 0], "the place the answer was made in"
            body = decision_body(r, 1)
            assert (body["source"], body["condition"], body["cell"]) == ("contingent", "succeeded", [0, 0, 0])
            assert r.loop.turns[1].executed_action == "c3"
            await finished(r)
            assert not problems(r.sink.entries) and not replay(entries(r))
        finally:
            await r.close()
    run(main)


def test_an_answer_made_in_another_place_asks_s1_afresh():
    """live-15: the answer was given at the ledge base and the next turn ran on the ledge top, a different
    place with its own history. The answer is spent there, and S1 is asked from that new place."""
    async def main():
        r = await placed(first(if_succeeded="c3"), "c1",
                         choices=[*base_choices(), option()], option_port=Port("succeeded"))
        try:
            await r.turns(1)
            r.game._pos = [1.0, 0.0, 0.0]        # the option left the body in another cell
            await r.turns(1)
            assert len(r.s1.contexts) == 2 and len(calls(r, 1)) == 1
            body = decision_body(r, 1)
            assert "source" not in body
            assert body["contingent_skipped"].startswith("place_changed:")
            assert (body["from_cell"], body["cell"]) == ([0, 0, 0], [2, 0, 0])
            assert r.loop.turns[1].executed_action == "c1"
            await finished(r)
            assert not problems(r.sink.entries) and not replay(entries(r))
        finally:
            await r.close()
    run(main)


def test_an_unmeasured_place_spends_the_answer():
    """No pose in the observation is an unknown place, never an assumed unchanged one."""
    async def main():
        r = await placed(first(if_succeeded="c3"), "c1",
                         choices=[*base_choices(), option()], option_port=Port("succeeded"),
                         game_class=FakeGame)
        try:
            await r.turns(2)
            assert len(r.s1.contexts) == 2 and len(calls(r, 1)) == 1
            assert "cell" not in decision_body(r, 0), "no measured place, so none is claimed"
            body = decision_body(r, 1)
            assert "source" not in body
            assert body["contingent_skipped"].startswith("place_unknown:")
            assert body["cell"] is None and body["from_cell"] is None
            assert r.loop.turns[1].executed_action == "c1"
            await finished(r)
            assert not problems(r.sink.entries) and not replay(entries(r))
        finally:
            await r.close()
    run(main)


def test_replay_flags_a_contingent_answer_that_changed_place():
    async def main():
        r = await placed(first(if_blocked="c2"), "c3",
                         choices=[*base_choices(), option()], option_port=Port("blocked"))
        try:
            await r.turns(2)
            await finished(r)
            rows = [{**row, "run_id": "test"} for row in r.sink.entries]
            rows[0]["body"] = _run_body()
            decided = next(i for i, row in enumerate(rows) if row["kind"] == "decision" and row["turn"] == 1)
            rows[decided]["body"]["cell"] = [7, 0, 0]
            found = [p.message for p in replay([TraceEntry.model_validate(row) for row in rows])]
            assert any("the place changed from cell" in p for p in found), found
        finally:
            await r.close()
    run(main)


def test_any_other_ending_asks_s1_afresh():
    async def main():
        r = await placed(first(if_blocked="c2"), "c3",
                      choices=[*base_choices(), option()], option_port=Port("fell"))
        try:
            await r.turns(2)
            assert len(r.s1.contexts) == 2 and len(calls(r, 1)) == 1
            body = decision_body(r, 1)
            assert "source" not in body and "contingent_skipped" not in body
            assert r.loop.turns[1].executed_action == "c3"
            await finished(r)
            assert not problems(r.sink.entries) and not replay(entries(r))
        finally:
            await r.close()
    run(main)


def test_a_withdrawn_id_asks_s1_afresh_and_says_why():
    async def main():
        seen = {"sets": 0}

        def drop_after_first(offered, observation):
            seen["sets"] += 1
            return [c for c in offered if not (c.id == "c2" and seen["sets"] > 1)]

        r = await placed(first(if_blocked="c2"), "c3",
                      choices=[*base_choices(), option()], option_port=Port("blocked"),
                      choice_filter=drop_after_first)
        try:
            await r.turns(2)
            assert len(r.s1.contexts) == 2 and len(calls(r, 1)) == 1
            body = decision_body(r, 1)
            assert "source" not in body
            assert "c2" in body["contingent_skipped"] and "no longer offered" in body["contingent_skipped"]
            assert r.loop.turns[1].executed_action == "c3"
            await finished(r)
            assert not problems(r.sink.entries) and not replay(entries(r))
        finally:
            await r.close()
    run(main)


def test_a_contingent_turn_is_not_built_on_another():
    async def main():
        r = await placed(first(if_blocked="c2"), "c3",
                      choices=[*base_choices(), option()], option_port=Port("blocked"))
        try:
            await r.turns(3)
            assert len(r.s1.contexts) == 2 and len(calls(r, 2)) == 1
            assert decision_body(r, 1)["source"] == "contingent"
            assert "source" not in decision_body(r, 2)
            await finished(r)
            assert not problems(r.sink.entries) and not replay(entries(r))
        finally:
            await r.close()
    run(main)


def test_replay_refuses_a_contingent_turn_that_follows_another():
    async def main():
        r = await placed(first(if_blocked="c2"), "c3",
                      choices=[*base_choices(), option()], option_port=Port("blocked"))
        try:
            await r.turns(3)
            await finished(r)
            rows = [{**row, "run_id": "test"} for row in r.sink.entries]
            rows[0]["body"] = _run_body()
            # Claim the third turn reused turn two's branch: no earlier turn answered for it, and no fresh call.
            decided = next(i for i, row in enumerate(rows) if row["kind"] == "decision" and row["turn"] == 2)
            call = next(i for i, row in enumerate(rows) if row["kind"] == "s1_call" and row["turn"] == 2)
            dropped = rows[call]["trace_seq"]
            rows[decided]["body"].update(source="contingent", from_turn=1, condition="blocked")
            rows[decided]["refs"] = [ref for ref in rows[decided]["refs"] if ref != dropped]
            del rows[call]
            found = [p.message for p in replay([TraceEntry.model_validate(row) for row in rows])]
            assert any("follows another" in p for p in found), found
            assert any("holds no contingent answer" in p for p in found), found
        finally:
            await r.close()
    run(main)


def test_adherence_flags_an_input_that_differs_from_the_contingent_id():
    async def main():
        r = await placed(first(if_blocked="c2"),
                      choices=[*base_choices(), option()], option_port=Port("blocked"))
        try:
            await r.turns(2)
            assert not check_adherence(r.loop.turns)
            swapped = [r.loop.turns[0], r.loop.turns[1].model_copy(update={"executed_action": "c3"})]
            assert any("executed 'c3', allowed 'c2'" in v.problem for v in check_adherence(swapped))
            await finished(r)
            rows = [{**row, "run_id": "test"} for row in r.sink.entries]
            rows[0]["body"] = _run_body()
            turn = next(i for i, row in enumerate(rows) if row["kind"] == "turn" and row["turn"] == 1)
            rows[turn]["body"]["executed_action"] = "c3"
            found = [p.message for p in replay([TraceEntry.model_validate(row) for row in rows])]
            assert any("executed 'c3', allowed 'c2'" in p for p in found), found
        finally:
            await r.close()
    run(main)


def test_the_other_refusals_ask_s1_afresh():
    """A scene boundary or help still pending after the option ended: the branch is spent, not acted on."""
    async def main():
        r = await placed(first(if_blocked="c2"),
                      choices=[*base_choices(), option()], option_port=Port("blocked"))
        try:
            await r.turns(1)
            candidate = {"choice_id": "c2", "condition": "blocked", "from_turn": 0, "decided": 1,
                         "cell": [0, 0, 0]}
            offered = ChoiceSet(set_version=1, built_from_seq=2, choices=[*base_choices(), option()])
            assert r.loop._contingent_for(candidate, offered, False, [0, 0, 0]) == (candidate, None)
            assert r.loop._contingent_for(candidate, offered, True, [0, 0, 0]) == (
                None, "the observation crossed a scene boundary after the option ended; asked S1 afresh")
            r.loop._pending = object()  # what a still-open help looks like to this check
            assert r.loop._contingent_for(candidate, offered, False, [0, 0, 0]) == (
                None, "help is pending after the option ended; asked S1 afresh")
        finally:
            await r.close()
    run(main)


def test_an_invalid_branch_is_dropped_alone():
    choices = ChoiceSet(set_version=0, built_from_seq=1, choices=base_choices())
    parsed = parse_decision({"choice_id": "c1", "set_version": 0,
                             "contingent": {"if_blocked": 17, "if_succeeded": "c3"}}, choices,
                            ClockMode.realtime)
    assert parsed.valid and parsed.decision is not None and parsed.decision.contingent is not None
    assert (parsed.decision.contingent.if_blocked, parsed.decision.contingent.if_succeeded) == (None, "c3")
    assert len(parsed.contingent_problems) == 1 and "if_blocked" in parsed.contingent_problems[0]
    # An id the set no longer offers is dropped the same way, and the primary choice survives both.
    offered = parse_decision({"choice_id": "c1", "set_version": 0,
                              "contingent": {"if_blocked": "gone", "if_succeeded": "c3"}}, choices,
                             ClockMode.realtime)
    assert offered.valid and offered.decision is not None and offered.decision.contingent is not None
    assert offered.decision.contingent.if_succeeded == "c3"
    assert "not an offered choice" in offered.contingent_problems[0]


def test_the_flag_needs_the_compact_menu(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("UPA_RUN_CONTINGENT", raising=False)
    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    assert cli._parser().parse_args(required).contingent is False
    assert cli._parser().parse_args([*required, "--contingent"]).contingent is True
    monkeypatch.setenv("UPA_RUN_CONTINGENT", "1")
    assert cli._parser().parse_args(required).contingent is True
    with pytest.raises(SystemExit) as raised:
        cli.main(["--project", str(tmp_path / "game"), "--entity", "e", "--calibration", "c",
                  "--output", str(tmp_path / "out"), "--approval", "a", "--max-calls", "1",
                  "--manifest", "m", "--catalog", "cat", "--catalog-sha256", "0", "--contingent"])
    assert raised.value.code == 2
    assert "--contingent requires --s1-context compact-menu" in capsys.readouterr().err


def test_the_unity_script_passes_the_switch_through():
    text = Path("scripts/unity/run-session.ps1").read_text()
    assert "[switch]$Contingent" in text
    assert "-Contingent needs -S1Context compact-menu" in text
    assert "$env:UPA_RUN_CONTINGENT" in text
