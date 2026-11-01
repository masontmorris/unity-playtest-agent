"""A choice set of exactly one choice runs without a provider call (borrowed-from-autopilot item 2).

There is nothing to select, so no code selects anything: the turn still builds and records the context it
would have been sent, executes the one choice, and names `source: "only_choice"` in its decision. A set of two
or more choices is S1's to answer, and a replay accepts a turn with no call only when its recorded set held
exactly one choice.
"""

from __future__ import annotations

import json
from pathlib import Path

from loop_fakes import rig, run
from turn_trace_check import problems as trace_problems
from playtest.contracts.loop import Choice
from playtest.contracts.trace import TraceEntry
from playtest.core.adherence import check_adherence
from playtest.core.trace_replay import replay


def wait_choice() -> Choice:
    return Choice(id="w", kind="wait", label="Wait", source="core")


def finish_choice() -> Choice:
    return Choice(id="f", kind="finish", label="Finish", source="core")


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


def test_a_single_choice_runs_with_no_call_and_replays_clean():
    async def main():
        r = await rig(choices=[wait_choice()])
        try:
            await r.turns(1)
            assert r.s1.contexts == [], "S1 is not asked when one choice is offered"
            assert not calls(r, 0)
            record = r.loop.turns[0]
            assert (record.raw_choice, record.effective_choice, record.executed_action) == ("w", "w", None)
            assert record.outcome.value == "finished" and record.policy_default is False
            body = decision_body(r, 0)
            assert body["source"] == "only_choice"
            assert body["policy_default"] is False and body["valid"] is True
            assert body["decision"]["choice_id"] == "w"
            # The context the turn would have been sent is still built and recorded.
            assert len(r.sink.of_kind("context")) == 1
            assert not check_adherence(r.loop.turns)
            await r.loop.stop("finish")
            assert not trace_problems(r.sink.entries)
            assert not replay(entries(r))
        finally:
            await r.close()
    run(main)


def test_two_choices_are_still_answered_by_s1():
    async def main():
        r = await rig("w", choices=[wait_choice(), finish_choice()])
        try:
            await r.turns(1)
            assert len(r.s1.contexts) == 1 and len(calls(r, 0)) == 1
            body = decision_body(r, 0)
            assert "source" not in body and body["valid"] is True
            await r.loop.stop("finish")
            assert not replay(entries(r))
        finally:
            await r.close()
    run(main)


def stripped(rows: list[dict], turn: int) -> list[dict]:
    """The trace with one turn's provider call removed and every later sequence number closed up."""
    gone = next(row["trace_seq"] for row in rows if row["kind"] == "s1_call" and row["turn"] == turn)
    kept: list[dict] = []
    for row in rows:
        if row["trace_seq"] == gone:
            continue
        row = dict(row)
        row["trace_seq"] -= 1 if row["trace_seq"] > gone else 0
        row["refs"] = [ref - 1 if ref > gone else ref for ref in row["refs"] if ref != gone]
        kept.append(row)
    return kept


def test_a_no_call_turn_over_two_choices_is_a_replay_problem():
    """Both recorders reject it: the set it recorded offered more than the one choice it claims."""
    async def main():
        r = await rig("w", choices=[wait_choice(), finish_choice()])
        try:
            await r.turns(1)
            rows = stripped([{**row, "run_id": "test"} for row in r.sink.entries], 0)
            rows[0]["body"] = _run_body()
            decision = next(row for row in rows if row["kind"] == "decision")
            decision["body"] = {**decision["body"], "source": "only_choice"}
            found = [str(problem) for problem in replay([TraceEntry.model_validate(row) for row in rows])]
            assert any("only-choice decision was recorded for a set of more than one choice" in item
                       for item in found), found
            reported = trace_problems(rows)
            assert any("only-choice decision names a set of more than one choice" in item
                       for item in reported), reported
        finally:
            await r.close()
    run(main)
