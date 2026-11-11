from __future__ import annotations

import asyncio

import pytest

from loop_fakes import FakeHelp, pick, rig
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Directive, Expected
from playtest.core.adherence import check_adherence
from playtest.core.context import build_context, canonical
from playtest.core.facts import RecordFacts
from playtest.core.trace_sink import MemorySink
from playtest.runner.cli import _parser
from playtest.runner.directives import DirectiveTracker
from playtest.runner.expectations import (action_events, play_presses, play_readings, score_situations,
                                          situation_presses)
from playtest.runner.objectives import objective_items, objective_line, objective_summary, with_objective_facts


def rule(action="look_right", **updates):
    return {"action": action, "situation": None, "condition_observable": None, "condition_op": None,
            "condition_value": None, "then": "value", "then_expect": "changes", "then_value": None,
            "within_s": 1.0, "basis": "intent", "because": "test", **updates}


def trace(presses=(), *, value=True):
    rows = [{"trace_seq": i, "kind": "observation", "body": {"wall_utc": float(i),
             "public": {"value": i} if value else {}}} for i in range(21)]
    for i, (at, status, option) in enumerate(presses):
        if option:
            body = {"receipts": [{"type": "request", "action_id": str(i), "action": "look_right"},
                                 {"type": "execution", "action_id": str(i), "status": status,
                                  "input_start_wall_s": at, "applied_duration_s": 0.1}]}
            rows.append({"trace_seq": 30 + i, "kind": "option", "body": body})
        else:
            rows.append({"trace_seq": 30 + i, "kind": "execution", "body": {
                "request": {"action": "look_right"}, "records": [{"status": status,
                "input_start_wall_s": at, "applied_duration_s": 0.1}]}})
    return rows


CONTROLS = {"look_right": frozenset({("pointer", "right")})}


def test_count_uses_scorer_spacing_option_receipts_and_rejections():
    rows = trace([(0.2, "completed", False), (0.8, "completed", False), (4.2, "completed", True),
                  (7.2, "rejected", False), (10.2, "completed", False)])
    item = objective_items({"action_rules": [rule()]}, rows, CONTROLS)[0]
    events = action_events(rule(), [play_readings(rows)], [play_presses(rows)], [CONTROLS])
    assert len(events) == item.count == 3
    assert item.status.value == "open" and item.evidence_seq == [0, 4, 10]


def test_region_scoring_uses_the_scorers_press_time_evidence_and_keeps_unread_unknown():
    candidate = rule(situation="near the target", target_object="target", region_id="region_1", within=2.0)
    rows = trace([(2.2, "completed", False), (6.2, "completed", False)])
    for n, (at, x) in enumerate(((2.2, 1), (6.2, 5))):
        rows.append({"trace_seq": 40 + n, "kind": "effect", "body": {"observed": {
            "readers_before": {"wall_s": at, "values": {"value": n}}, "from_position": [x, 0, 0],
            "targets_before": {"target": {"position": [0, 0, 0]}}}}})
    score = score_situations([candidate], [play_readings(rows)], [play_presses(rows)], [CONTROLS],
                             [situation_presses(rows)])[0]
    item = objective_items({"situations": [candidate]}, rows, CONTROLS)[0]
    assert score["n"] == item.count == 1 and score["situation_not_met"] == 1
    missing = objective_items({"situations": [candidate]}, rows[:-1], CONTROLS)[0]
    assert missing.count == 1 and missing.status.value == "unknown"


def test_four_counted_presses_remain_met_when_a_later_window_is_incomplete():
    rows = trace([(0.2, "completed", False), (3.2, "completed", False), (6.2, "completed", False),
                  (9.2, "completed", False), (19.2, "completed", False)])
    item = objective_items({"action_rules": [rule()]}, rows, CONTROLS)[0]
    assert item.count == 4 and item.status.value == "met"


def test_missing_readings_and_uncheckable_situations_stay_in_the_list():
    rows = trace([(2.2, "completed", False)], value=False)
    items = objective_items({"action_rules": [rule()], "situations": [rule(action="other", situation="somewhere")]},
                            rows, CONTROLS)
    assert [(item.count, item.status.value) for item in items] == [(None, "unknown"), (0, "open")]


def test_a_step_whose_input_was_never_sent_reads_zero_and_open_not_unknown():
    # Nothing to measure an effect with, and nothing pressed: zero presses is still a measured count (#270).
    items = objective_items({"action_rules": [rule(), rule(action="look_up")]}, trace(value=False), CONTROLS)
    assert [(item.count, item.status.value) for item in items] == [(0, "open"), (0, "open")]
    assert "press look_up: 0 of 4 presses, open" in objective_line(items, None)


def test_a_rejected_press_alone_still_counts_as_never_sent():
    items = objective_items({"action_rules": [rule()]}, trace([(2.2, "rejected", False)], value=False), CONTROLS)
    assert (items[0].count, items[0].status.value) == (0, "open")


def test_a_choice_holding_the_inputs_controls_counts_as_sent():
    controls = {**CONTROLS, "g_move_look_right": frozenset({("pointer", "right"), ("key", "w")})}
    rows = trace(value=False)
    rows.append({"trace_seq": 50, "kind": "execution", "body": {"request": {"action": "g_move_look_right"},
                 "records": [{"status": "completed", "input_start_wall_s": 2.2, "applied_duration_s": 0.1}]}})
    item = objective_items({"action_rules": [rule()]}, rows, controls)[0]
    assert (item.count, item.sent, item.status.value) == (None, 1, "unknown")
    assert "press look_right: 1 sent, none measurable, unknown" in objective_line([item], None)


def test_one_plan_step_with_conflicting_scorer_counts_stays_unknown():
    rows = trace([(2.2, "completed", False), (4.2, "completed", False)])
    items = objective_items({"action_rules": [rule(within_s=1), rule(within_s=3)]}, rows, CONTROLS)
    assert len(items) == 1 and items[0].count is None and items[0].status.value == "unknown"


@pytest.mark.parametrize(("value", "time", "expected"), [(2, 1, "met"), (0, 11, "not_met"),
                                                           (None, 11, "expired"), (0, 1, "open")])
def test_directive_items_mirror_the_directive_tracker(value, time, expected):
    sink = MemorySink()
    tracker = DirectiveTracker(sink)
    directive = Directive(directive_id="d1", model="m", based_on_seq=0, text="A reading",
                          expected=Expected(field="public.counter", op=">=", value=2), deadline_game_s=10)
    row = {**directive.model_dump(mode="json"), "directive": directive.model_dump(mode="json"),
           "kind": "number", "scene": "scene", "requested_game_s": None}
    tracker.adopt(row, request_turn=0, based_on_seq=0)
    obs = Observation(session="s", seq=1, game_s=time, wall_utc="2026-01-01T00:00:00Z",
                      clock_mode=ClockMode.realtime, scene="scene", playing=True, paused=False,
                      time_scale=1, frame=1, public={} if value is None else {"counter": value})
    asyncio.run(tracker.facts([], obs))
    view = tracker.directive_view(obs)
    items = objective_items({}, sink.entries, {}, view)
    assert items[0].status.value == expected
    assert items[0].status == (view.current or view.closed[0]).outcome
    if expected != "open":
        assert objective_items({}, sink.entries, {}) == items


def test_trace_context_reconstructs_the_first_stated_deadline():
    from playtest.core.ports import DirectiveView
    original = Directive(directive_id="d", model="m", based_on_seq=0, text="A reading",
                         expected=Expected(field="public.x", op=">=", value=1), deadline_game_s=10)
    stated = original.model_copy(update={"deadline_game_s": 25})
    rows = [{"kind": "investigation", "body": {"event": "directive", "directive": original.model_dump(mode="json")}},
            {"kind": "context", "body": {"payload": {"directive": stated.model_dump(mode="json"),
                                                        "directive_history": []}}}]
    assert objective_items({}, rows, {}) == objective_items({}, rows, {}, DirectiveView(current=stated))


def test_finished_and_invalidated_directives_keep_the_trackers_outcomes():
    for invalidated in (False, True):
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        directive = Directive(directive_id="d", model="m", based_on_seq=0, text="A reading",
                              expected=Expected(field="public.x", op=">=", value=1), deadline_game_s=30)
        tracker.adopt({**directive.model_dump(mode="json"), "directive": directive.model_dump(mode="json"),
                       "kind": "number", "scene": "scene", "requested_game_s": None},
                      request_turn=0, based_on_seq=0)
        obs = Observation(session="s", seq=1, game_s=1, wall_utc="2026-01-01T00:00:00Z",
                          clock_mode=ClockMode.realtime, scene="scene", playing=True, paused=False,
                          time_scale=1, frame=1, public={"x": 0})
        asyncio.run(tracker.facts([], obs))
        if invalidated:
            tracker.rebind(observation=obs.model_copy(update={"session": "new", "seq": 2}))
        else:
            tracker.finish(turn=0, based_on_seq=1)
        item = objective_items({}, sink.entries, {})[0]
        assert item.status.value == ("invalidated" if invalidated else "unknown")


def test_fact_lists_every_item_and_gives_no_recommendation():
    directive = Directive(directive_id="d", model="m", based_on_seq=1, text="A measured field",
                          expected=Expected(field="public.x", op=">=", value=1), deadline_game_s=20)
    from playtest.core.ports import DirectiveView
    items = objective_items({"action_rules": [rule()]}, trace(), CONTROLS, DirectiveView(current=directive))
    line = objective_line(items, 8)
    assert line == ("Objective items (measured by code): (1) [hunt_plan] press look_right: 0 of 4 presses, open; "
                    "(2) [frontier_directive] A measured field: open, deadline in 12 s.")
    assert not any(word in line.lower() for word in ("should", "try", "next", "recommend", "consider"))
    assert objective_summary(items)["by_source"] == {"hunt_plan": {"met": 0, "total": 1},
                                                      "frontier_directive": {"met": 0, "total": 1}}


def test_seven_step_fact_and_directive_use_stable_plan_order():
    names = ["look_right", "look_left", "look_up", "look_down", "fire", "reload", "interact"]
    rules = {"action_rules": [rule(name) for name in names]}
    controls = {name: frozenset({("device", name)}) for name in names}
    rows = [{"trace_seq": i, "kind": "observation", "body": {"wall_utc": float(i),
             "public": {"value": i}}} for i in range(60)]
    for name, count in (("look_right", 6), ("look_left", 16)):
        for n in range(count):
            rows.append({"trace_seq": len(rows), "kind": "execution", "body": {"request": {"action": name},
                         "records": [{"status": "completed", "input_start_wall_s": 1.2 + n * 3,
                                      "applied_duration_s": 0.1}]}})
    directive = Directive(directive_id="d", model="m", based_on_seq=1, text="The measured field",
                          expected=Expected(field="public.x", op=">=", value=1), deadline_game_s=20)
    from playtest.core.ports import DirectiveView
    items = objective_items(rules, rows, controls, DirectiveView(current=directive))
    assert objective_line(items, 8) == (
        "Objective items (measured by code): (1) [hunt_plan] press look_right: 6 of 4 presses, met; "
        "(2) [hunt_plan] press look_left: 16 of 4 presses, met; "
        "(3) [hunt_plan] press look_up: 0 of 4 presses, open; "
        "(4) [hunt_plan] press look_down: 0 of 4 presses, open; "
        "(5) [hunt_plan] press fire: 0 of 4 presses, open; "
        "(6) [hunt_plan] press reload: 0 of 4 presses, open; "
        "(7) [hunt_plan] press interact: 0 of 4 presses, open; "
        "(8) [frontier_directive] The measured field: open, deadline in 12 s.")


def test_flag_defaults_off_and_preserves_the_exact_built_payload(monkeypatch):
    monkeypatch.delenv("UPA_RUN_OBJECTIVE_FACTS", raising=False)
    args = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    assert not _parser().parse_args(args).objective_facts
    monkeypatch.setenv("UPA_RUN_OBJECTIVE_FACTS", "1")
    assert _parser().parse_args(args).objective_facts
    async def check():
        r = await rig("w")
        try:
            obs = await r.client.observe_once()
            from playtest.contracts.loop import ChoiceSet
            from loop_fakes import choices as all_choices
            choices = ChoiceSet(set_version=0, built_from_seq=obs.seq, choices=all_choices())
            inner = RecordFacts()
            off = with_objective_facts(inner, False, r.sink, {"action_rules": [rule()]}, CONTROLS)
            before = build_context(objective="Explore.", observation=obs, choices=choices,
                                   facts=await inner.facts([], obs))
            after = build_context(objective="Explore.", observation=obs, choices=choices,
                                  facts=await off.facts([], obs))
            assert canonical(before) == canonical(after)
        finally:
            await r.close()
    asyncio.run(check())


def test_objectives_never_replace_rank_suppress_or_open_help_for_choices():
    async def check():
        helper = FakeHelp()
        r = await rig("c2", "c1", pick("missing"), "h", "c2", help=helper)
        r.loop._facts = with_objective_facts(r.loop._facts, True, r.sink, {"action_rules": [rule()]}, CONTROLS)
        try:
            await r.turns(5)
            assert [t.raw_choice for t in r.loop.turns] == ["c2", "c1", "missing", "h", "c2"]
            assert [t.executed_action for t in r.loop.turns] == ["c2", "c1", None, None, "c2"]
            assert r.game.applied() == ["increment", "shift", "increment"]
            assert len(helper.requests) == 1
            assert [entry["turn"] for entry in r.helps("opened")] == [3]
            assert all([c["id"] for c in context["choices"]] == ["c1", "c2", "c3", "h", "d", "w", "f"]
                       for context in r.s1.contexts)
            assert all(any(line.startswith("Objective items") for line in context["facts"])
                       for context in r.s1.contexts)
            assert check_adherence(r.loop.turns) == []
            await r.loop.stop("finish")
        finally:
            await r.close()
    asyncio.run(check())
