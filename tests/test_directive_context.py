"""S1's context states the directives its facts source tracks, with the provenance that source measured.

These tests read the serialized payload S1 received (the fake S1 round-trips it through `canonical`), not the
tracker's return value. They establish what is delivered, when, and that nothing S1 may choose, or the loop does,
changes because of it. They say nothing about whether a model navigates better for having it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Comparator, Directive, DirectiveOutcome, Expected
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, ChoiceSource
from playtest.contracts.trace import TraceAuthor, TraceEntry, TraceKind
from playtest.core.adherence import check_adherence
from playtest.core.context import build_context, canonical
from playtest.core.ports import DirectiveView, HelpResult, S1Answer, S1Call, ViewSample
from playtest.core.trace_replay import replay as replay_trace
from playtest.core.trace_sink import MemorySink
from playtest.core.turn_loop import TurnLoop
from playtest.runner.directives import DirectiveTracker, _game_age
from playtest.runner.pacing import PacingFacts
from playtest.runner.resources import SessionFacts
from test_decision_memory import Bridge, S1, WALL, observation
from test_frontier_help import _report
from test_frontier_publication import _answer_for, _helper, _reply, _send
from test_loop_help_publication import live_choices
from test_runner_session import harness_trace
from turn_trace_check import problems as trace_problems

SECTIONS = ("directive", "directive_history", "directive_provenance")


# --- the builder --------------------------------------------------------------------------------


def _obs(seq: int = 7, game_s: float | None = 12.5) -> Observation:
    return Observation(session="s", seq=seq, game_s=game_s, wall_utc=datetime(2026, 10, 27, tzinfo=timezone.utc),
                       clock_mode=ClockMode.realtime, scene="scene-a", playing=True, paused=False, time_scale=1.0,
                       frame=3, public={"gauge": 1.0})


def _choice_set(seq: int = 7) -> ChoiceSet:
    return ChoiceSet(set_version=1, built_from_seq=seq, choices=[
        Choice(id="c1", kind=ChoiceKind.wait, label="Wait", source=ChoiceSource.core)])


def _directive(directive_id: str, outcome: DirectiveOutcome = DirectiveOutcome.open) -> Directive:
    return Directive(directive_id=directive_id, model="m", based_on_seq=6, text=f"Advice {directive_id}.",
                     expected=Expected(field="public.gauge", op=Comparator.ge, value=2), outcome=outcome)


def _build(**kwargs: Any) -> dict[str, Any]:
    return build_context(objective="Explore.", observation=_obs(), choices=_choice_set(), **kwargs)


def test_provenance_is_absent_unless_supplied_and_then_the_payload_keeps_its_bytes():
    plain = _build()
    assert "directive_provenance" not in plain
    for nothing in (None, {}):
        assert canonical(_build(directive_provenance=nothing)) == canonical(plain)
    shown = _build(directive=_directive("d-1"), directive_history=[_directive("d-0", DirectiveOutcome.not_met)])
    assert "directive_provenance" not in shown  # directives alone are the builder's old behaviour, key for key
    assert {k: v for k, v in shown.items() if k not in SECTIONS} == {k: v for k, v in plain.items() if k not in SECTIONS}


def test_provenance_is_copied_in_and_out_and_keeps_its_nulls():
    supplied = {"d-1": {"requested": {"turn": 3, "seq": 100, "game_s": None}, "list": [1, {"deep": 2}]}}
    payload = _build(directive=_directive("d-1"), directive_provenance=supplied)
    assert payload["directive_provenance"] == supplied
    supplied["d-1"]["requested"]["seq"] = 999
    supplied["d-1"]["list"][1]["deep"] = 9
    assert payload["directive_provenance"]["d-1"]["requested"] == {"turn": 3, "seq": 100, "game_s": None}
    assert payload["directive_provenance"]["d-1"]["list"] == [1, {"deep": 2}]
    payload["directive_provenance"]["d-1"]["requested"]["turn"] = 77
    assert supplied["d-1"]["requested"]["turn"] == 3


def test_provenance_for_a_directive_the_payload_does_not_show_is_refused():
    with pytest.raises(ValueError, match="does not show"):
        _build(directive=_directive("d-1"), directive_provenance={"d-2": {"requested": {}}})
    with pytest.raises(ValueError, match="does not show"):
        _build(directive_provenance={"d-1": {"requested": {}}})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_provenance_that_is_not_finite_json_is_refused(value):
    with pytest.raises(ValueError, match="finite JSON"):
        _build(directive=_directive("d-1"), directive_provenance={"d-1": {"game_s": value}})
    with pytest.raises(ValueError, match="finite JSON"):
        DirectiveView(current=_directive("d-1"), provenance={"d-1": {"game_s": value}})


def test_the_builder_shows_the_open_directive_apart_from_the_closed_ones_with_their_own_outcomes():
    closed = [_directive("d-0", DirectiveOutcome.not_met), _directive("d-1", DirectiveOutcome.met)]
    payload = _build(directive=_directive("d-2"), directive_history=closed,
                     directive_provenance={"d-0": {"n": 0}, "d-2": {"n": 2}})
    assert payload["directive"]["directive_id"] == "d-2" and payload["directive"]["outcome"] == "open"
    assert [(d["directive_id"], d["outcome"]) for d in payload["directive_history"]] == [
        ("d-0", "not_met"), ("d-1", "met")]
    assert payload["directive_provenance"] == {"d-0": {"n": 0}, "d-2": {"n": 2}}  # d-1 had none; none is invented


def test_a_view_keeps_open_and_closed_apart_and_every_id_once():
    view = DirectiveView(current=_directive("d-2"), closed=[_directive("d-1", DirectiveOutcome.met)],
                         provenance={"d-1": {}, "d-2": {}})
    assert isinstance(view.closed, tuple) and view.current.directive_id == "d-2"
    with pytest.raises(ValueError, match="not open"):
        DirectiveView(current=_directive("d-1", DirectiveOutcome.met))
    with pytest.raises(ValueError, match="cannot be open"):
        DirectiveView(closed=(_directive("d-1"),))
    with pytest.raises(ValueError, match="twice"):
        DirectiveView(current=_directive("d-1"), closed=(_directive("d-1", DirectiveOutcome.met),))
    with pytest.raises(ValueError, match="does not list"):
        DirectiveView(current=_directive("d-1"), provenance={"d-9": {}})
    assert DirectiveView() == DirectiveView(current=None, closed=(), provenance={})


# --- the tracker's view -------------------------------------------------------------------------


def _row(directive_id: str, *, op: str = "changed", value: Any = None, baseline: Any = 0, deadline: float = 50.0,
         requested_game_s: float | None = 1.0, scene: str = "scene") -> dict[str, Any]:
    """What `FrontierHelp` hands the tracker: the directive's record, with the tracker's own scoring fields."""
    expected = {"field": "public.counter", "op": op, **({} if value is None else {"value": value})}
    spec = {"directive_id": directive_id, "model": "m", "based_on_seq": 4, "text": f"Advice {directive_id}.",
            "expected": expected, "deadline_game_s": deadline}
    return {**spec, "kind": "number", "baseline": baseline, "scene": scene, "last_frame": 0, "last_game_s": 0,
            "requested_game_s": requested_game_s, "directive": dict(spec)}


def _at(seq: int, game_s: float | None, counter: Any = 0, *, scene: str = "scene", session: str = "t") -> Observation:
    public = {} if counter is None else {"counter": counter}
    return Observation(session=session, seq=seq, game_s=game_s, wall_utc=WALL, clock_mode=ClockMode.realtime,
                       scene=scene, playing=True, paused=False, time_scale=1, frame=seq, public=public)


def _tracked(*rows: dict[str, Any], turn: int = 6, seq: int = 8, game_s: float | None = 8.5,
             counter: Any = 0) -> tuple[DirectiveTracker, Observation]:
    """A tracker that adopted each row at request turn 2 / bridge seq 4 and evaluated one observation at `turn`."""
    tracker = DirectiveTracker(MemorySink())
    for row in rows:
        tracker.adopt(row, request_turn=2, based_on_seq=4)
    obs = _at(seq, game_s, counter)
    asyncio.run(tracker.facts([_report(i, "wait") for i in range(turn)], obs))
    return tracker, obs


def test_nothing_is_stated_before_the_tracker_has_evaluated_that_observation():
    tracker, obs = DirectiveTracker(MemorySink()), _at(8, 8.5)
    assert tracker.directive_view(obs) is None                                  # nothing adopted
    tracker.adopt(_row("d-1", deadline=5.0, requested_game_s=0.5), request_turn=2, based_on_seq=4)
    assert tracker.directive_view(obs) is None                                  # adopted, not yet evaluated
    assert tracker.history[0]["directive"]["deadline_game_s"] == 5.0            # what an early read would have stated
    asyncio.run(tracker.facts([_report(i, "wait") for i in range(6)], obs))
    view = tracker.directive_view(obs)
    assert view.current.deadline_game_s == 8.5 + (5.0 - 0.5)                    # counted from the statement, not the ask
    assert tracker.directive_view(_at(9, 9.5)) is None                          # another observation is not the evaluated one
    assert tracker.directive_view(_at(8, 8.5, session="other")) is None         # nor the same seq of another session
    tracker.adopt(_row("d-2"), request_turn=6, based_on_seq=8)                  # adopted after that evaluation...
    assert tracker.directive_view(obs) is None                                  # ...is not stated as if it had been scored
    asyncio.run(tracker.facts([_report(i, "wait") for i in range(7)], obs))
    assert [d.directive_id for d in tracker.directive_view(obs).closed] == ["d-1"]


def test_the_view_states_where_a_directive_was_asked_and_first_stated_without_mixing_the_ids():
    tracker, obs = _tracked(_row("d-1"), turn=6, seq=8, game_s=8.5)
    view = tracker.directive_view(obs)
    assert [d.directive_id for d in (view.current, *view.closed)] == ["d-1"]
    assert view.provenance == {"d-1": {
        "requested": {"turn": 2, "seq": 4, "game_s": 1.0},                      # the ask: turn 2, bridge seq 4
        "first_stated": {"turn": 6, "seq": 8, "game_s": 8.5},                   # S1 first read it at turn 6, bridge seq 8
        "age_game_s": {"since_requested": 7.5, "since_first_stated": 0.0},
        "expires_in_game_s": 49.0,                                             # deadline 57.5 minus this game_s
        "scored_on": "reader_setpoint",
        "baseline": {"value": 0, "source": "first_statement"}}}
    later = _at(11, 12.0)
    asyncio.run(tracker.facts([_report(i, "wait") for i in range(7)], later))
    ages = tracker.directive_view(later).provenance["d-1"]
    assert ages["first_stated"] == {"turn": 6, "seq": 8, "game_s": 8.5}        # fixed once stated
    assert ages["age_game_s"] == {"since_requested": 11.0, "since_first_stated": 3.5}
    assert ages["expires_in_game_s"] == 45.5                                   # same deadline, later clock


def test_unknown_clocks_stay_null_and_a_measured_zero_stays_zero():
    tracker, obs = _tracked(_row("d-1", requested_game_s=None), game_s=None)
    unknown = tracker.directive_view(obs).provenance["d-1"]
    assert unknown["requested"] == {"turn": 2, "seq": 4, "game_s": None}
    assert unknown["first_stated"] == {"turn": 6, "seq": 8, "game_s": None}
    assert unknown["age_game_s"] == {"since_requested": None, "since_first_stated": None}
    assert unknown["expires_in_game_s"] is None                                 # no clock, so no time left
    tracker, obs = _tracked(_row("d-1", requested_game_s=None), game_s=8.5)
    mixed = tracker.directive_view(obs).provenance["d-1"]["age_game_s"]
    assert mixed["since_requested"] is None                                     # no wall-time stand-in for the ask
    assert mixed["since_first_stated"] == 0.0 and mixed["since_first_stated"] is not None
    assert tracker.directive_view(obs).provenance["d-1"]["expires_in_game_s"] == 41.5  # deadline not re-anchored: no ask clock


@pytest.mark.parametrize(("now", "then", "expected"), [
    (None, 1.0, None), (1.0, None, None), (None, None, None), (5.0, 3.0, 2.0), (3.0, 3.0, 0.0),
    (3.0, 5.0, None), (float("inf"), 1.0, None), (float("nan"), 1.0, None)])
def test_a_game_age_is_measured_or_unknown(now, then, expected):
    assert _game_age(now, then) == expected
    assert (_game_age(now, then) is None) == (expected is None)


def test_a_directive_never_stated_has_null_stamps_not_the_stamps_of_another():
    tracker, obs = _tracked(_row("d-1"), turn=3, seq=6, game_s=6.5)
    tracker.adopt(_row("d-2"), request_turn=7, based_on_seq=9)                  # supersedes d-1 before d-2 is stated
    asyncio.run(tracker.facts([_report(i, "wait") for i in range(8)], _at(10, 10.5)))
    view = tracker.directive_view(_at(10, 10.5))
    assert [d.directive_id for d in view.closed] == ["d-1"] and view.current.directive_id == "d-2"
    assert view.provenance["d-1"]["first_stated"] == {"turn": 3, "seq": 6, "game_s": 6.5}
    assert view.provenance["d-1"]["superseded_by"] == "d-2"
    assert view.provenance["d-2"]["requested"] == {"turn": 7, "seq": 9, "game_s": 1.0}
    assert view.provenance["d-2"]["first_stated"] == {"turn": 8, "seq": 10, "game_s": 10.5}
    assert "superseded_by" not in view.provenance["d-2"]
    # d-3 is replaced by d-4 before any context states it, so it was never first stated.
    tracker.adopt(_row("d-3"), request_turn=9, based_on_seq=11)
    tracker.adopt(_row("d-4"), request_turn=10, based_on_seq=12)
    asyncio.run(tracker.facts([_report(i, "wait") for i in range(11)], _at(13, 13.5)))
    view = tracker.directive_view(_at(13, 13.5))
    assert view.provenance["d-3"]["first_stated"] == {"turn": None, "seq": None, "game_s": None}
    assert view.provenance["d-3"]["requested"] == {"turn": 9, "seq": 11, "game_s": 1.0}
    assert view.provenance["d-3"]["superseded_by"] == "d-4"


_OUTCOMES = [("met", 0.5, 1, "scene"), ("not_met", 2.0, 0, "scene"), ("expired", 2.0, None, "scene"),
             ("invalidated", 0.5, 0, "new-scene"), ("unknown", 0.5, None, "scene")]


@pytest.mark.parametrize(("status", "game_s", "counter", "scene"), _OUTCOMES)
def test_each_outcome_is_listed_closed_with_its_own_status_and_no_age(status, game_s, counter, scene):
    tracker = DirectiveTracker(MemorySink())
    tracker.adopt(_row("d-1", op=">=", value=1, baseline=0, deadline=1.0 if status in ("not_met", "expired") else 10.0,
                       requested_game_s=None), request_turn=0, based_on_seq=0)
    obs = _at(1, game_s, counter, scene=scene)
    asyncio.run(tracker.facts([], obs))
    if status == "unknown":
        tracker.finish(turn=1, based_on_seq=1)
    view = tracker.directive_view(obs)
    assert view.current is None
    assert [(d.directive_id, d.outcome.value) for d in view.closed] == [("d-1", status)]
    origin = view.provenance["d-1"]
    assert "age_game_s" not in origin                                           # only advice that is still open is aged
    assert "expires_in_game_s" not in origin                                    # and only it states time left
    assert origin["requested"] == {"turn": 0, "seq": 0, "game_s": None}         # never asked at a known game time
    assert origin["scored_on"] == "reader_setpoint" and "baseline" not in origin  # a `>=` setpoint has no baseline


def test_a_directive_with_no_setpoint_says_nothing_was_scored_on_a_reader():
    row = _row("d-1")
    row["expected"] = row["directive"]["expected"] = None
    tracker, obs = _tracked(row)
    origin = tracker.directive_view(obs).provenance["d-1"]
    assert origin["scored_on"] is None and "baseline" not in origin
    assert tracker.directive_view(obs).current.expected is None


@pytest.mark.parametrize(("public_counter", "escalation_baseline", "expected"), [
    (7, 4, {"value": 7, "source": "first_statement"}),     # re-taken from the observation that first states it
    (None, 4, {"value": 4, "source": "escalation"}),       # the field was unreadable then: the ask's own reading
    (None, None, {"value": None, "source": None}),         # never read: unknown, not zero
])
def test_a_changed_setpoint_states_the_baseline_it_compares_with_and_where_it_came_from(
        public_counter, escalation_baseline, expected):
    tracker, obs = _tracked(_row("d-1", baseline=escalation_baseline), counter=public_counter)
    assert tracker.directive_view(obs).provenance["d-1"]["baseline"] == expected


def test_a_view_is_a_copy_later_updates_and_edits_cannot_reach():
    tracker, obs = _tracked(_row("d-1", op=">=", value=1, deadline=50.0, requested_game_s=None, baseline=0))
    before = tracker.directive_view(obs)
    snapshot = canonical({"c": before.current.model_dump(mode="json"), "p": before.provenance})
    before.provenance["d-1"]["requested"]["seq"] = 999                          # editing a handed-out view...
    again = tracker.directive_view(obs)
    assert again.provenance["d-1"]["requested"]["seq"] == 4                     # ...changes nothing the tracker holds
    later = _at(9, 9.5, 5)                                                      # the setpoint is met on a later turn
    asyncio.run(tracker.facts([_report(i, "wait") for i in range(7)], later))
    assert tracker.active is None and tracker.directive_view(later).current is None
    assert canonical({"c": again.current.model_dump(mode="json"), "p": {
        "d-1": {**again.provenance["d-1"], "requested": {"turn": 2, "seq": 4, "game_s": None}}}}) == snapshot
    assert again.current.outcome is DirectiveOutcome.open                       # the earlier view still says open
    assert tracker.directive_view(obs) is None                                  # and the old observation is no longer current


def test_mutable_values_in_a_handed_out_view_cannot_change_the_tracker():
    row = _row("d-1")
    row["kind"] = "vector"
    tracker, obs = _tracked(row, counter=[0, 1, 2])
    view = tracker.directive_view(obs)
    assert view.current is not None
    view.provenance["d-1"]["baseline"]["value"][0] = 999
    assert tracker.active["baseline"] == [0, 1, 2]
    assert tracker.directive_view(obs).provenance["d-1"]["baseline"]["value"] == [0, 1, 2]


def test_session_and_pacing_wrappers_forward_the_view_unchanged():
    tracker, obs = _tracked(_row("d-1"))
    sessions = SessionFacts(started=0.0, wall_limit_s=10.0, clock=lambda: 0.0, s1=None, s1_call_cap=None, inner=tracker)
    paced = PacingFacts(inner=sessions)
    expected = tracker.directive_view(obs)
    assert expected is not None and sessions.directive_view(obs) == expected and paced.directive_view(obs) == expected
    bare = PacingFacts(inner=SessionFacts(started=0.0, wall_limit_s=10.0, clock=lambda: 0.0, s1=None, s1_call_cap=None))
    assert bare.directive_view(obs) is None and PacingFacts().directive_view(obs) is None


# --- the loop, end to end -----------------------------------------------------------------------


def _obs_at(seq: int, counter: Any = 0, **update: Any) -> Observation:
    return observation(seq).model_copy(update={"public": {"counter": counter}, **update})


def _answer_with(body, **directive: Any) -> tuple:
    packet = json.loads(body["messages"][2]["content"])
    answer = _answer_for(packet)
    answer["directive"].update(directive)
    return _reply(answer)


def _send_changed(body):
    """A frontier answer whose setpoint is only that a reader value changed."""
    return _answer_with(body, expected={"field": "public.counter", "op": "changed", "value": None, "tolerance": None})


def _send_open_ended(body):
    return _answer_with(body, deadline_s=None)


def _send_targeted(body):
    return _answer_with(body, target={"kind": "location", "ref": "spot-a"})


class NoView:
    """The tracker's facts and nothing else: a source that does not implement `directive_view`."""

    def __init__(self, inner):
        self._inner = inner

    async def facts(self, reports, observation):
        return await self._inner.facts(reports, observation)


class CalledS1(S1):
    """The shared fake S1, plus the provider call a real port records for every turn (a replay needs it)."""

    async def decide(self, context):
        answered = await super().decide(context)
        call = S1Call(question="next_action", provider="fake", model="fake", http_status=200, latency_ms=1.0)
        return S1Answer(answer=answered.answer, calls=(call,))


@dataclass
class Run:
    loop: TurnLoop
    bridge: Bridge
    s1: S1
    tracker: DirectiveTracker
    helper: Any
    sink: MemorySink

    @property
    def contexts(self) -> list[dict[str, Any]]:
        return self.s1.contexts


async def drive(tmp_path, observations, steps, plan, *, send=_send, view=True, s1_type=CalledS1,
                game_view=None) -> Run:
    """Run turns ("t") and let the pending help finish between them ("s"). Nothing in the fakes yields to the
    event loop on its own, so the helper's answer is adopted exactly where the plan lets it finish."""
    helper, tracker, sink = _helper(tmp_path, send, sink=harness_trace())
    bridge, s1 = Bridge(list(observations)), s1_type(steps)
    loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=helper, view=game_view,
                    facts=tracker if view else NoView(tracker), sink=sink, clock=lambda: WALL)
    try:
        for step in plan:
            if step == "s":
                await loop._pending.task
            else:
                await loop.run_turn()
    finally:
        await loop.stop("user")
    return Run(loop, bridge, s1, tracker, helper, sink)


def _play(tmp_path, observations, steps, plan, **kwargs) -> Run:
    return asyncio.run(drive(tmp_path, observations, steps, plan, **kwargs))


def _nothing_stated(ctx: dict[str, Any]) -> bool:
    return ctx["directive"] is None and ctx["directive_history"] == [] and "directive_provenance" not in ctx


def _stamp(turn: int | None, seq: int | None, game_s: float | None) -> dict[str, Any]:
    return {"turn": turn, "seq": seq, "game_s": game_s}


def test_an_open_directive_and_then_its_closed_outcome_reach_s1_with_the_provenance_measured(tmp_path):
    observations = [_obs_at(100 + i, 1 if i == 5 else 0) for i in range(6)]
    run = _play(tmp_path, observations, ["h", "w", "w", "w", "w", "w"], "ttt" "s" "ttt")
    ctx = run.contexts
    for waiting in ctx[:3]:  # help is pending (or only asked for): nothing is advice yet
        assert _nothing_stated(waiting)
    assert ctx[1]["pending_help"] is not None and ctx[2]["pending_help"] is not None

    first = ctx[3]  # adopted at turn 3, when the answer to the turn-0 request was ready
    assert first["pending_help"] is None and first["directive_history"] == []
    assert first["directive"]["directive_id"] == "d-0" and first["directive"]["outcome"] == "open"
    assert first["directive"]["source"] == "frontier" and first["directive"]["model"]
    assert first["directive"]["based_on_seq"] == 100                             # the ask's bridge observation
    assert first["directive"]["deadline_game_s"] == 113.0                       # 103 (first stated) + 10 s, not 100 + 10
    assert "target" not in first["directive"]                                   # none was given, none is invented
    assert first["directive_provenance"] == {"d-0": {
        "requested": _stamp(0, 100, 100.0), "first_stated": _stamp(3, 103, 103.0),
        "age_game_s": {"since_requested": 3.0, "since_first_stated": 0.0},
        "expires_in_game_s": 10.0, "scored_on": "reader_setpoint"}}
    assert first["as_of"]["seq"] == 103

    second = ctx[4]
    assert second["directive_provenance"]["d-0"]["first_stated"] == _stamp(3, 103, 103.0)
    assert second["directive_provenance"]["d-0"]["age_game_s"] == {"since_requested": 4.0, "since_first_stated": 1.0}
    assert second["directive_provenance"]["d-0"]["expires_in_game_s"] == 9.0      # 113 - the 104 this turn saw

    closed = ctx[5]  # the reader reached the setpoint on this turn's observation
    assert closed["directive"] is None
    (done,) = closed["directive_history"]
    assert (done["directive_id"], done["outcome"]) == ("d-0", "met")
    assert done["window"]["inputs_completed"] == 0 and done["window"]["waits"] == 2  # no input of S1's was involved
    assert closed["directive_provenance"] == {"d-0": {
        "requested": _stamp(0, 100, 100.0), "first_stated": _stamp(3, 103, 103.0), "scored_on": "reader_setpoint"}}

    # A bridge `seq` is never a trace entry number: the provenance names the first, a closed directive's
    # `evidence_seq` is the second (the contract's own convention for that field).
    sink = run.sink
    for seq in (100, 103):
        assert sink.trace_seq_for_observation(seq) not in (seq, None)
    assert done["evidence_seq"] == [sink.trace_seq_for_observation(105)] and done["evidence_seq"] != [105]
    # The existing fact lines stay beside the typed record, so the prose is stated twice and nothing is lost.
    for number in (3, 4, 5):
        assert any(line.startswith("Frontier directive d-0:") for line in ctx[number]["facts"])
    assert "status met" in next(line for line in closed["facts"] if line.startswith("Frontier directive d-0:"))


def test_the_frontiers_objective_attribution_reaches_s1_verbatim_and_survives_the_close(tmp_path):
    observations = [_obs_at(100 + i, 1 if i == 5 else 0) for i in range(6)]
    run = _play(tmp_path, observations, ["h", "w", "w", "w", "w", "w"], "ttt" "s" "ttt")
    ctx = run.contexts
    first = ctx[3]["directive"]
    assert first["objective_quote"] == "Inspect."                                # the run objective, as quoted
    assert first["continue_if"] == ["the measured count still moves toward the objective"]
    assert first["replan_if"] == ["the measured count contradicts the objective"]
    # A closed directive keeps the same words; nothing rewrites, drops or scores them.
    (done,) = ctx[5]["directive_history"]
    assert (done["objective_quote"], done["continue_if"], done["replan_if"]) == (
        "Inspect.", ["the measured count still moves toward the objective"],
        ["the measured count contradicts the objective"])
    assert [r.effective_choice for r in run.loop.turns] == ["h", "w", "w", "w", "w", "w"]


def test_a_directive_recorded_before_the_attribution_fields_stays_readable_and_is_not_backfilled(tmp_path):
    tracker, obs = _tracked(_row("d-1"))
    current = tracker.directive_view(obs).current
    assert (current.objective_quote, current.continue_if, current.replan_if) == (None, None, None)
    payload = _build(directive=current)
    assert not {"objective_quote", "continue_if", "replan_if"} & set(payload["directive"])


def test_the_recorded_trace_holds_exactly_what_s1_received_and_replays_clean(tmp_path):
    observations = [_obs_at(100 + i, 1 if i == 5 else 0) for i in range(6)]
    run = _play(tmp_path, observations, ["h", "w", "w", "w", "w", "w"], "ttt" "s" "ttt")
    recorded = [entry["body"]["payload"] for entry in run.sink.of_kind("context")]
    assert [canonical(payload) for payload in recorded] == [canonical(ctx) for ctx in run.contexts]
    assert any("directive_provenance" in payload for payload in recorded)
    run.sink.append(TraceKind.end, {"status": "complete", "error": None, "turns": len(run.loop.turns),
                                    "redactions": 0}, author=TraceAuthor.harness)
    validated = [TraceEntry.model_validate({"run_id": "r1", **row}) for row in run.sink.entries]
    assert replay_trace(validated) == [] and trace_problems(run.sink.entries) == []


def _script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_offline_rebuilder_reproduces_old_and_new_style_traced_payloads(tmp_path):
    observations = [_obs_at(100 + i, 1 if i == 5 else 0) for i in range(6)]
    run = _play(tmp_path, observations, ["h", "w", "w", "w", "w", "w"], "ttt" "s" "ttt")
    rebuild = _script("guidance_layout_trace").rebuild
    for payload in run.contexts:  # the first three carry no provenance; the rest do
        assert canonical(rebuild(payload, payload.get("guidance", ()))) == canonical(payload)
    assert "directive_provenance" in run.contexts[3] and "directive_provenance" not in run.contexts[0]


def test_the_offline_rebuilder_preserves_the_recorded_prompt_across_policy_versions(tmp_path):
    from playtest.bench.record import BENCH_INSTRUCTIONS

    rebuild = _script("guidance_layout_trace").rebuild
    for instructions in (BENCH_INSTRUCTIONS, "An explicitly versioned historical decision policy."):
        payload = _build()
        payload["instructions"] = instructions
        assert canonical(rebuild(payload, ())) == canonical(payload)


def test_the_directive_sections_are_the_only_difference_and_no_choice_action_or_help_changes(tmp_path):
    def play(view: bool) -> Run:
        observations = [_obs_at(100 + i, 1 if i == 6 else 0) for i in range(7)]
        return _play(tmp_path / ("with" if view else "without"), observations,
                     ["a", "h", "w", "b", "w", "w", "w"], "tt" "s" "ttttt", view=view)

    (tmp_path / "with").mkdir()
    (tmp_path / "without").mkdir()
    shown, plain = play(True), play(False)
    assert any(ctx["directive"] or ctx["directive_history"] for ctx in shown.contexts)  # the comparison is not vacuous
    assert all(_nothing_stated(ctx) for ctx in plain.contexts)

    def rest(run: Run) -> list[str]:
        return [canonical({k: v for k, v in ctx.items() if k not in SECTIONS}) for ctx in run.contexts]

    def turns(run: Run):
        return [(r.turn, r.raw_choice, r.effective_choice, r.executed_action, r.outcome, r.while_pending,
                 r.policy_default) for r in run.loop.turns]

    def sent(run: Run):
        return [(r["choice_id"], r["action"], r["based_on_seq"]) for r in run.bridge.requests]

    def helps(run: Run):
        return [(e["turn"], e["body"]["event"], e["body"]["opened_turn"]) for e in run.sink.of_kind("help")]

    assert rest(shown) == rest(plain)                                           # facts, guidance, history, choices...
    assert [c["choices"] for c in shown.contexts] == [c["choices"] for c in plain.contexts]
    assert turns(shown) == turns(plain) and sent(shown) == sent(plain) and helps(shown) == helps(plain)
    assert sent(shown) == [("a", "move", 100), ("b", "look", 103)]              # only what S1 chose was sent
    assert check_adherence(shown.loop.turns) == [] and check_adherence(plain.loop.turns) == []


def _send_stale_description(body):
    return _answer_with(
        body, text="The clip shows only open flat ground to the horizon with no stepped structures in view. "
                   "Use look to rotate in place until the stepped structures sit in the centre of the view, then stop.",
        target={"kind": "location", "ref": "stepped-structures-out-of-view"})


class SeesFrame(CalledS1):
    """An S1 whose port takes the game view image, as the live adapter does."""

    async def decide(self, context, *, image=None):
        return await super().decide(context)


class NewestFrame:
    def sample(self, observation):
        return ViewSample(statement=f"frame-{observation.seq:06d}.jpg is the newest game view",
                          sha256="ab" * 32, status="sent", image=b"jpeg")


def test_old_advice_beside_a_newer_view_carries_what_s1_needs_to_compare_them_and_is_not_judged(tmp_path):
    """Session 12, turn 82: the frontier's description (structures out of view) was composed 22 bridge seqs and
    8.8 game seconds before the context that stated it, beside a game view that showed them."""
    asked = _obs_at(409, game_s=262.08777371031744)
    stated = _obs_at(431, game_s=270.9062326105442)
    run = _play(tmp_path, [asked, stated, _obs_at(432, game_s=271.9)], ["h", "a", "w"], "t" "s" "tt",
                send=_send_stale_description, s1_type=SeesFrame, game_view=NewestFrame())
    first = run.contexts[1]
    assert "no stepped structures in view" in first["directive"]["text"]            # the old description, as the frontier wrote it
    assert first["directive"]["target"]["ref"] == "stepped-structures-out-of-view"
    assert first["game_view"] == "frame-000431.jpg is the newest game view" and first["game_view_status"] == "sent"
    origin = first["directive_provenance"]["d-0"]
    assert origin["requested"] == _stamp(0, 409, 262.08777371031744)                 # the observation it was composed on
    assert origin["first_stated"] == _stamp(1, 431, 270.9062326105442)               # the observation with the newer view
    assert origin["age_game_s"]["since_requested"] == pytest.approx(8.818458900226744)
    assert origin["age_game_s"]["since_first_stated"] == 0.0
    assert origin["expires_in_game_s"] == 10.0                                       # anchored to the statement, not the ask
    # S1 chose a move over the advised turn: it ran as chosen, and nothing says it was wrong or that the advice was.
    record = run.loop.turns[1]
    assert (record.raw_choice, record.effective_choice, record.executed_action) == ("a", "a", "a")
    assert record.policy_default is False and run.loop.helper_count == 0
    assert [(r["choice_id"], r["based_on_seq"]) for r in run.bridge.requests] == [("a", 431)]
    for ctx in run.contexts[1:]:
        judging = [line for line in ctx["facts"] if "d-0" in line and not line.startswith("Frontier directive d-0:")]
        assert judging == []
    assert check_adherence(run.loop.turns) == []


class Tampering(CalledS1):
    """An S1 that edits the context object it was handed, after it has been recorded."""

    async def decide(self, context):
        answered = await super().decide(context)
        if context["directive"] is not None:
            context["directive"]["text"] = "TAMPERED"
            context["directive"]["expected"]["value"] = 999
            context["directive_provenance"]["d-0"]["requested"]["seq"] = -1
            context["directive_provenance"]["d-0"]["age_game_s"]["since_requested"] = -1.0
        return answered


def test_s1_editing_the_context_it_received_changes_nothing_the_tracker_or_trace_holds(tmp_path):
    observations = [_obs_at(100 + i) for i in range(5)]
    run = _play(tmp_path, observations, ["h", "w", "w", "w", "w"], "t" "s" "tttt", s1_type=Tampering)
    original = "Check whether the measured count reaches one."
    assert run.tracker.history[0]["directive"]["text"] == original
    assert run.tracker.history[0]["directive"]["expected"]["value"] == 1.0
    recorded = [entry["body"]["payload"] for entry in run.sink.of_kind("context")]
    for payload in (*recorded[1:], *run.contexts[1:]):  # what the trace holds, and what each later turn was handed
        assert payload["directive"]["text"] == original and payload["directive"]["expected"]["value"] == 1.0
        assert payload["directive_provenance"]["d-0"]["requested"]["seq"] == 100
    assert run.contexts[2]["directive_provenance"]["d-0"]["age_game_s"]["since_requested"] == 2.0


def test_a_closed_history_keeps_every_directive_in_order_and_names_who_replaced_whom(tmp_path):
    observations = [_obs_at(100 + i) for i in range(4)]
    run = _play(tmp_path, observations, ["h", "h", "h", "w"], "t" "s" "t" "s" "t" "s" "t")
    ctx = run.contexts
    assert ctx[1]["directive"]["directive_id"] == "d-0" and ctx[1]["directive_history"] == []
    assert ctx[2]["directive"]["directive_id"] == "d-1" and ctx[2]["directive"]["parent"] == "d-0"
    assert [(d["directive_id"], d["outcome"]) for d in ctx[2]["directive_history"]] == [("d-0", "invalidated")]
    last = ctx[3]
    assert last["directive"]["directive_id"] == "d-2" and last["directive"]["parent"] == "d-1"
    assert [(d["directive_id"], d["outcome"]) for d in last["directive_history"]] == [
        ("d-0", "invalidated"), ("d-1", "invalidated")]
    kept = {item["directive"]["directive_id"]: item["directive"]["text"] for item in run.tracker.history}
    assert {d["directive_id"]: d["text"] for d in last["directive_history"]} == {k: kept[k] for k in ("d-0", "d-1")}
    origin = last["directive_provenance"]
    assert origin["d-0"]["superseded_by"] == "d-1" and origin["d-1"]["superseded_by"] == "d-2"
    assert "superseded_by" not in origin["d-2"] and "age_game_s" not in origin["d-0"] and "age_game_s" in origin["d-2"]
    assert "expires_in_game_s" in origin["d-2"]                                   # time left is stated for the open one only
    assert "expires_in_game_s" not in origin["d-0"] and "expires_in_game_s" not in origin["d-1"]
    assert origin["d-1"]["requested"] == _stamp(1, 101, 101.0) and origin["d-1"]["first_stated"] == _stamp(2, 102, 102.0)
    assert origin["d-2"]["requested"] == _stamp(2, 102, 102.0) and origin["d-2"]["first_stated"] == _stamp(3, 103, 103.0)


def test_a_not_met_with_no_attempt_keeps_its_error_class_and_window_in_the_history(tmp_path):
    observations = [_obs_at(100), _obs_at(101), _obs_at(102), _obs_at(103, game_s=120.0)]
    run = _play(tmp_path, observations, ["h", "w", "w", "w"], "t" "s" "ttt")
    (missed,) = run.contexts[3]["directive_history"]
    assert missed["outcome"] == "not_met" and missed["error_class"] == "no_attempt"
    assert missed["window"]["inputs_sent"] == 0 and missed["window"]["waits"] == 2
    assert run.contexts[3]["directive"] is None


def test_a_met_setpoint_from_a_passive_change_is_not_stated_as_a_goal_reached(tmp_path):
    observations = [_obs_at(100), _obs_at(101), _obs_at(102), _obs_at(103), _obs_at(104, 7)]
    run = _play(tmp_path, observations, ["h", "w", "w", "w", "w"], "tt" "s" "ttt", send=_send_changed)
    ctx = run.contexts
    assert ctx[2]["directive"]["expected"] == {"field": "public.counter", "op": "changed"}
    assert ctx[2]["directive_provenance"]["d-0"]["baseline"] == {"value": 0, "source": "first_statement"}
    (met,) = ctx[4]["directive_history"]
    assert met["outcome"] == "met" and met["window"]["inputs_sent"] == 0 and met["window"]["inputs_completed"] == 0
    origin = ctx[4]["directive_provenance"]["d-0"]
    assert origin["scored_on"] == "reader_setpoint" and origin["baseline"] == {"value": 0, "source": "first_statement"}
    # No field says a goal was reached: the record is the contract's own, and the provenance only names what was
    # measured (where it was asked and stated, what it was scored on, and what the setpoint compares with).
    assert set(met) <= set(Directive.model_fields)
    assert set(origin) == {"requested", "first_stated", "scored_on", "baseline"}
    assert met["text"] == run.tracker.history[0]["directive"]["text"]            # the advice is stated as given
    # Nothing finished, repeated or asked for on S1's behalf: the loop did only what S1 chose, which was to wait.
    assert [r.effective_choice for r in run.loop.turns] == ["h", "w", "w", "w", "w"]
    assert run.bridge.requests == [] and run.loop.helper_count == 0 and check_adherence(run.loop.turns) == []
    assert len(run.sink.of_kind("help")) == 2                                    # the one it opened, and its adoption


def test_a_directive_given_a_target_keeps_it_and_one_without_gets_none(tmp_path):
    run = _play(tmp_path, [_obs_at(100), _obs_at(101)], ["h", "w"], "t" "s" "t", send=_send_targeted)
    assert run.contexts[1]["directive"]["target"] == {"kind": "location", "ref": "spot-a"}


def test_old_advice_is_aged_not_hidden_and_no_age_limit_changes_the_turn(tmp_path):
    observations = [_obs_at(100), _obs_at(101), _obs_at(102), _obs_at(103, game_s=9000.0)]
    run = _play(tmp_path, observations, ["h", "w", "w", "w"], "t" "s" "ttt", send=_send_open_ended)
    recent, old = run.contexts[2], run.contexts[3]
    assert old["directive"]["directive_id"] == "d-0" and old["directive_history"] == []
    assert old["directive_provenance"]["d-0"]["age_game_s"] == {"since_requested": 8900.0, "since_first_stated": 8899.0}
    assert old["directive_provenance"]["d-0"]["expires_in_game_s"] is None       # no deadline, so no time left to state
    assert "deadline_game_s" not in old["directive"]
    assert old["choices"] == recent["choices"] and old["pending_help"] is None
    assert run.loop.helper_count == 0 and run.bridge.requests == []


def test_a_game_clock_that_is_unknown_stays_null_in_what_s1_receives(tmp_path):
    unknown = [_obs_at(100 + i, game_s=None) for i in range(3)]
    run = _play(tmp_path, unknown, ["h", "w", "w"], "t" "s" "tt")
    for ctx in run.contexts[1:]:
        assert ctx["as_of"]["game_s"] is None
        assert ctx["directive_provenance"]["d-0"]["requested"] == _stamp(0, 100, None)
        assert ctx["directive_provenance"]["d-0"]["first_stated"] == _stamp(1, 101, None)
        assert ctx["directive_provenance"]["d-0"]["age_game_s"] == {"since_requested": None, "since_first_stated": None}
        assert ctx["directive_provenance"]["d-0"]["expires_in_game_s"] is None    # no clock: unknown, never zero
    assert "deadline_game_s" not in run.contexts[1]["directive"]                 # no clock, no invented deadline

    mixed = [_obs_at(100, game_s=None), _obs_at(101), _obs_at(102)]
    run = _play(tmp_path / "mixed", mixed, ["h", "w", "w"], "t" "s" "tt")
    assert run.contexts[1]["directive_provenance"]["d-0"]["age_game_s"] == {
        "since_requested": None, "since_first_stated": 0.0}
    assert run.contexts[2]["directive_provenance"]["d-0"]["age_game_s"] == {
        "since_requested": None, "since_first_stated": 1.0}
    assert run.contexts[2]["directive_provenance"]["d-0"]["expires_in_game_s"] is None  # the ask had no game clock


@pytest.mark.parametrize("change", [{"scene": "s1"}, {"game_s": 50.0}, {"frame": 1}, {"session": "new-session"}],
                         ids=["scene", "clock", "frame", "session"])
def test_a_boundary_closes_the_open_directive_in_the_context_that_crosses_it(tmp_path, change):
    observations = [_obs_at(100), _obs_at(101), _obs_at(102, **change), _obs_at(103, **change)]
    run = _play(tmp_path, observations, ["h", "w", "w", "w"], "t" "s" "ttt")
    assert run.contexts[1]["directive"]["directive_id"] == "d-0"
    for crossed in run.contexts[2:]:
        assert crossed["directive"] is None
        (history,) = crossed["directive_history"]
        assert (history["directive_id"], history["outcome"]) == ("d-0", "invalidated")
        assert history["window"]["turns"] >= 0
        origin = crossed["directive_provenance"]["d-0"]
        assert "age_game_s" not in origin and "superseded_by" not in origin
        assert "expires_in_game_s" not in origin                                 # closed advice states no countdown
        assert origin["first_stated"] == _stamp(1, 101, 101.0)
    assert run.contexts[2]["recent_history"]["boundary"] is not None


@pytest.mark.parametrize("change", [{"scene": "s1"}, {"game_s": 5.0}], ids=["scene", "clock"])
@pytest.mark.parametrize("completion", ["before_entry", "during_observation"])
def test_help_from_an_earlier_epoch_never_reaches_a_context_and_new_help_carries_its_own_provenance(
        tmp_path, change, completion):
    async def scenario():
        helper, tracker, sink = _helper(tmp_path, _send, sink=harness_trace())
        first, second = _obs_at(100), _obs_at(101, **change)
        third = second.model_copy(update={"seq": 102, "frame": second.frame + 1, "game_s": second.game_s + 1})
        fourth = third.model_copy(update={"seq": 103, "frame": third.frame + 1, "game_s": third.game_s + 1})
        bridge, s1 = Bridge([first, second, third, fourth]), S1(["h", "a", "h", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=helper, facts=tracker,
                        sink=sink, clock=lambda: WALL)
        try:
            await loop.run_turn()
            old = loop._pending.task
            if completion == "before_entry":
                await old
                assert 0 in helper._staged                                       # finished and staged, not adopted
            else:
                original = bridge.observe_once

                async def finish_while_observing():
                    await old
                    return await original()

                bridge.observe_once = finish_while_observing
            await loop.run_turn()                                                # the boundary turn
            assert _nothing_stated(s1.contexts[1]) and s1.contexts[1]["pending_help"] is None
            assert tracker.history == [] and helper._staged == {}
            await loop.run_turn()                                                # S1 asks again, in the new epoch
            await loop._pending.task
            await loop.run_turn()
            adopted = s1.contexts[3]
            assert _nothing_stated(s1.contexts[2])
            assert adopted["directive"]["directive_id"] == "d-2"
            assert adopted["directive_provenance"] == {"d-2": {
                "requested": _stamp(2, 102, third.game_s), "first_stated": _stamp(3, 103, fourth.game_s),
                "age_game_s": {"since_requested": 1.0, "since_first_stated": 0.0},
                "expires_in_game_s": 10.0, "scored_on": "reader_setpoint"}}
            assert all('"directive_id":"d-0"' not in canonical(ctx) for ctx in s1.contexts)
            assert [item["directive"]["directive_id"] for item in tracker.history] == ["d-2"]
            assert [(r["choice_id"], r["based_on_seq"]) for r in bridge.requests] == [("a", 101)]
            assert check_adherence(loop.turns) == []
        finally:
            await loop.stop("user")

    asyncio.run(scenario())


def test_an_operator_replacement_is_guidance_and_never_becomes_a_directive(tmp_path):
    async def scenario():
        helper, tracker, sink = _helper(tmp_path, _send, sink=harness_trace())
        bridge, s1 = Bridge([_obs_at(100 + i) for i in range(3)]), S1(["h", "w", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=helper, facts=tracker,
                        sink=sink, clock=lambda: WALL)
        try:
            await loop.run_turn()
            frontier_task = loop._pending.task
            assert loop.resolve_helper({"advice": "from the operator"}, source="operator")
            await loop.run_turn()
            await asyncio.gather(frontier_task, return_exceptions=True)          # the replaced request ends late
            await loop.run_turn()
            for ctx in s1.contexts:
                assert _nothing_stated(ctx)
            assert "from the operator" in canonical(s1.contexts[1]["guidance"])
            assert tracker.history == [] and helper._staged == {} and bridge.requests == []
        finally:
            await loop.stop("user")

    asyncio.run(scenario())


@pytest.mark.parametrize("late_failure", [False, True])
def test_a_cancelled_helper_that_finishes_late_cannot_reach_a_context(tmp_path, late_failure):
    async def scenario():
        helper, tracker, sink = _helper(tmp_path, _send, sink=harness_trace())
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Resistant:
            async def run(self, request):
                if request.turn == 0:
                    started.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        await release.wait()
                    if late_failure:
                        raise RuntimeError("late helper failure")
                return await helper.run(request)

            def discard_result(self, turn):
                helper.discard_result(turn)

            def adopt_result(self, turn, result):
                return helper.adopt_result(turn, result)

        observations = [_obs_at(100), _obs_at(101, scene="s1"), _obs_at(102, scene="s1"), _obs_at(103, scene="s1")]
        bridge, s1 = Bridge(observations), S1(["h", "a", "h", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=Resistant(), facts=tracker,
                        sink=sink, clock=lambda: WALL)
        try:
            await loop.run_turn()
            old = loop._pending.task
            await started.wait()
            await loop.run_turn()                                                # the boundary; must not wait for it
            await cancelled.wait()
            release.set()
            await asyncio.gather(old, return_exceptions=True)                    # it finishes after being cancelled
            await asyncio.sleep(0)
            await loop.run_turn()
            await loop._pending.task
            await loop.run_turn()
            assert _nothing_stated(s1.contexts[1]) and _nothing_stated(s1.contexts[2])
            assert s1.contexts[3]["directive"]["directive_id"] == "d-2"
            assert list(s1.contexts[3]["directive_provenance"]) == ["d-2"]
            assert all('"directive_id":"d-0"' not in canonical(ctx) for ctx in s1.contexts)
        finally:
            release.set()
            await loop.stop("user")

    asyncio.run(scenario())


def test_without_a_helper_or_a_directive_the_payload_has_the_old_keys_and_bytes(tmp_path):
    async def play(facts):
        bridge, s1 = Bridge([_obs_at(100), _obs_at(101)]), S1(["a", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, facts=facts, clock=lambda: WALL)
        await loop.run_turn()
        await loop.run_turn()
        await loop.stop("user")
        return s1.contexts

    def wrapped(inner):
        return PacingFacts(inner=SessionFacts(started=0.0, wall_limit_s=10.0, clock=lambda: 0.0, s1=None,
                                              s1_call_cap=None, inner=inner))

    plain = asyncio.run(play(None))
    for facts in (DirectiveTracker(MemorySink()), wrapped(DirectiveTracker(MemorySink())), wrapped(None)):
        contexts = asyncio.run(play(facts))
        assert all(_nothing_stated(ctx) for ctx in contexts)
        assert [ctx["choices"] for ctx in contexts] == [ctx["choices"] for ctx in plain]
    tracked = asyncio.run(play(DirectiveTracker(MemorySink())))
    assert [canonical(ctx) for ctx in tracked] == [canonical(ctx) for ctx in plain]  # a tracker with nothing is silent


class _Source:
    """A facts source whose directive hook misbehaves in the way the test asks."""

    def __init__(self, mode: str):
        self.mode = mode

    async def facts(self, reports, observation):
        return []

    def directive_view(self, observation):
        if self.mode == "raises":
            raise RuntimeError("the hook failed")
        if self.mode == "not_a_view":
            return {"current": None, "closed": [], "provenance": {}}
        if self.mode == "malformed":
            return DirectiveView(current=_directive("d-9"), provenance={"d-1": {}})
        if self.mode == "not_finite":
            return DirectiveView(current=_directive("d-9"), provenance={"d-9": {"game_s": float("nan")}})
        return None


@pytest.mark.parametrize("mode", ["raises", "not_a_view", "malformed", "not_finite", "none"])
def test_a_faulty_or_silent_source_gives_s1_the_context_it_would_have_had_and_changes_nothing_else(mode):
    async def play(facts):
        bridge, s1 = Bridge([_obs_at(100), _obs_at(101)]), S1(["a", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, facts=facts, clock=lambda: WALL)
        await loop.run_turn()
        await loop.run_turn()
        await loop.stop("user")
        return s1.contexts, [(r.raw_choice, r.effective_choice, r.executed_action) for r in loop.turns], bridge.requests

    contexts, turns, requests = asyncio.run(play(_Source(mode)))
    control_contexts, control_turns, _ = asyncio.run(play(NoView(_Source(mode))))
    assert [canonical(c) for c in contexts] == [canonical(c) for c in control_contexts]
    assert turns == control_turns == [("a", "a", "a"), ("w", "w", None)] and len(requests) == 1


def _spy(events: list[str]):
    class SpyBridge(Bridge):
        async def observe_once(self):
            events.append("observe")
            return await super().observe_once()

    class SpyHelp:
        adopted = False

        async def run(self, request):
            return HelpResult(source="spy", result={"advice": "x"})

        def adopt_result(self, turn, result):
            events.append(f"adopt {turn}")
            SpyHelp.adopted = True
            return result

        def discard_result(self, turn):
            events.append(f"discard {turn}")

    class SpyFacts:
        async def facts(self, reports, observation):
            events.append(f"facts {observation.seq}")
            return []

        def directive_view(self, observation):
            events.append(f"view {observation.seq}")
            if not SpyHelp.adopted:
                return None
            return DirectiveView(current=_directive("d-spy"), provenance={"d-spy": {"seen_at": observation.seq}})

    return SpyBridge, SpyHelp, SpyFacts


@pytest.mark.parametrize("boundary", [False, True])
def test_the_view_is_asked_for_only_after_the_epoch_check_adoption_and_facts_of_that_observation(boundary):
    async def scenario():
        events: list[str] = []
        bridge_type, help_type, facts_type = _spy(events)
        second = _obs_at(101, scene="s1") if boundary else _obs_at(101)
        bridge, s1 = bridge_type([_obs_at(100), second, _obs_at(102)]), S1(["h", "w", "w"])
        loop = TurnLoop(bridge, choices=live_choices(), objective="Inspect.", s1=s1, help=help_type(),
                        facts=facts_type(), clock=lambda: WALL)
        try:
            await loop.run_turn()
            assert events == ["observe", "facts 100", "view 100"]
            await loop._pending.task
            events.clear()
            await loop.run_turn()
            if boundary:  # the earlier epoch's finished help is discarded before facts and never adopted
                assert events == ["observe", "discard 0", "facts 101", "view 101"]
                assert _nothing_stated(s1.contexts[1])
            else:  # adopted after the observation, then facts, then the view that can state it
                assert events == ["observe", "adopt 0", "facts 101", "view 101"]
                assert s1.contexts[1]["directive"]["directive_id"] == "d-spy"
                assert s1.contexts[1]["directive_provenance"] == {"d-spy": {"seen_at": 101}}
            assert _nothing_stated(s1.contexts[0])
            events.clear()
            await loop.run_turn(lambda choice_set, observed: {"choice_id": "w", "set_version": choice_set.set_version})
            assert events == ["observe"]                                         # a scripted turn builds no context
        finally:
            await loop.stop("user")

    asyncio.run(scenario())


def test_a_restart_invalidates_the_open_directive_and_keeps_it_as_attributed_history():
    tracker, before = _tracked(_row("d-1", op=">=", value=5, deadline=100.0), counter=1)
    assert tracker.active is not None and tracker.active["status"] == "open"

    after = _at(20, 20.0, counter=1, session="after")
    tracker.rebind(observation=after, generation="g-2")

    # The open directive is closed at once, not left current; it stays in the history with its outcome.
    assert tracker.active is None
    closed = tracker.history[-1]
    assert (closed["directive_id"], closed["status"], closed["outcome"]) == ("d-1", "invalidated", "invalidated")
    assert closed["invalidated_generation"] == "g-2"
    assert "play session restarted" in closed["reason"]
    # No new directive is created, and no directive is stated until the new observation is evaluated.
    assert len(tracker.history) == 1
    assert tracker.directive_view(after) is None
