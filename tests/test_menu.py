"""The compact menu: one comparable label per choice, measured progress, and a three-question request.

Every assertion here is about a shape, a quantity or a size, never about a game: a label says what an option
is, what it measured, and what happened here, and nothing else that S1 would have to read.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Comparator, Directive, Expected
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, ChoiceSource
from playtest.core.context import canonical
from playtest.runner.menu import (
    LABEL_LIMIT,
    BLOCKED_QUESTION,
    PRIMARY,
    SEPARATOR,
    SUCCEEDED_QUESTION,
    compact_menu,
    menu_request,
    option_label,
    progress_line,
)
from playtest.runner.options.memory import MINUS, OutcomeMemory
from playtest.runner.options.model import OptionOutcome

CELL = 0.5


def observation(position=(0.0, 0.0, 0.0), probe=None, seq: int = 4, scene: str = "scene-a") -> Observation:
    public: dict = {"controlled": {"object_id": "o", "path": "p", "position": list(position),
                                   "velocity": None, "source": "rigidbody"}}
    if probe is not None:
        public["probe"] = probe
    return Observation(session="s", seq=seq, game_s=1.0,
                       wall_utc=datetime(2026, 11, 1, tzinfo=timezone.utc),
                       clock_mode=ClockMode.realtime, scene=scene, playing=True, paused=False,
                       time_scale=1.0, frame=0, public=public)


def option(option_id: str, template: str, params: dict, intent: str) -> Choice:
    return Choice(id=option_id, kind=ChoiceKind.option, option_id=option_id, template=template,
                  params=params, label=f"{intent} \u2014 here: untried", source=ChoiceSource.manifest)


def choices(seq: int = 4, version: int = 3) -> ChoiceSet:
    return ChoiceSet(set_version=version, built_from_seq=seq, choices=[
        option("hop+x", "hop", {"direction": [1, 0, 0], "action": "m", "rise_action": "r", "chord": "c",
                                "run_up": 11.3, "launch_distance": 5.65, "flight_s": 1.2,
                                "rise_peak": 2.48, "speed": 9.2, "step": 0.5},
               "hop +x: run-up 11.30, rise 1.20 s, peak 2.48"),
        option("traverse+x", "traverse", {"direction": [1, 0, 0], "distance": None, "action": "m",
                                          "speed": 3.1, "step": 0.55},
               "traverse +x: 3.10 u/s, step 0.55"),
        option("retreat+x:1.2", "retreat", {"direction": [-1, 0, 0], "distance": 1.2, "action": "b",
                                            "speed": 3.1, "step": 0.55, "from": "+x"},
               "retreat from +x: 1.20 at 3.10 u/s"),
        option("wait:0.5", "wait", {"seconds": 0.5}, "wait 0.5 s"),
        Choice(id="move_forward", kind=ChoiceKind.action, action="move_forward", source=ChoiceSource.manifest,
               label=("Move forward: request a 0.5-second hold of W; calibrated net displacement 2.354 world "
                      "units (sample variance 0.000123, n=3).")),
        Choice(id="combo", kind=ChoiceKind.skill, skill="combo", source=ChoiceSource.manifest,
               label=("Combo: request this ordered sequence of 2 steps, at most 1.5 s in total: step 1: hold W "
                      "for 0.5 s; step 2: hold Space for 0.5 s; no calibrated effect is recorded for this "
                      "sequence.")),
        Choice(id="wait", kind=ChoiceKind.wait, params={"duration_game_s": 0.5}, source=ChoiceSource.core,
               label="Wait 0.5 s with no input"),
        Choice(id="escalate", kind=ChoiceKind.escalate, role="frontier", source=ChoiceSource.core,
               label="Ask the frontier model for help"),
        Choice(id="delegate", kind=ChoiceKind.delegate, role="helper", source=ChoiceSource.core,
               label="Hand a bounded task to a helper"),
        Choice(id="restart_play_mode", kind=ChoiceKind.restart_play_mode, source=ChoiceSource.core,
               label="Restart Play Mode: stop the play session and start it again from the beginning"),
        Choice(id="finish", kind=ChoiceKind.finish, source=ChoiceSource.core, label="Finish"),
    ])


def outcome(option_id: str = "hop+x", terminated_by: str = "blocked", start=(0.0, 0.0, 0.0),
            direction=(1.0, 0.0, 0.0)) -> OptionOutcome:
    end = (start[0] + 1.0, start[1], start[2])
    return OptionOutcome(option_id=option_id, template="hop", params={"direction": list(direction)},
                         start_position=list(start), end_position=list(end),
                         displacement=[b - a for a, b in zip(start, end)],
                         terminated_by=terminated_by, duration_s=0.5, scene="scene-a")


def test_every_kind_has_one_shape_and_fits():
    memory = OutcomeMemory(cell=CELL)
    seen = observation()
    labels = {choice.id: option_label(choice, memory, seen.scene, [0.0, 0.0, 0.0])
              for choice in choices().choices}
    assert len(labels) == 11
    for choice_id, label in labels.items():
        assert label.count(SEPARATOR) >= 1, label
        assert len(label) <= 90, (choice_id, len(label), label)
    assert labels["hop+x"].startswith(f"hop +x{SEPARATOR}run-up 11.3, rise 1.20 s, peak 2.48{SEPARATOR}")
    assert labels["traverse+x"].startswith(f"traverse +x{SEPARATOR}3.10 u/s, step 0.55{SEPARATOR}")
    assert labels["retreat+x:1.2"].startswith(f"retreat from +x{SEPARATOR}1.20 at 3.10 u/s{SEPARATOR}")
    assert labels["wait:0.5"].startswith(f"wait 0.5 s{SEPARATOR}")
    assert labels["move_forward"].startswith(f"Move forward{SEPARATOR}2.35 units{SEPARATOR}")
    assert labels["combo"].startswith(f"Combo{SEPARATOR}effect unknown{SEPARATOR}")
    assert labels["wait"].startswith(f"wait{SEPARATOR}0.5 s no input")
    assert labels["escalate"].startswith(f"escalate{SEPARATOR}")
    assert labels["delegate"].startswith(f"delegate{SEPARATOR}")
    assert labels["restart_play_mode"].startswith(f"restart{SEPARATOR}")
    assert labels["finish"].startswith(f"finish{SEPARATOR}")
    assert labels["hop+x"].endswith("here: untried")


def test_labels_carry_measured_history_and_stay_short():
    memory = OutcomeMemory(cell=CELL)
    for terminated_by in ("blocked", "blocked", "fell", "succeeded", "timed_out", "state_changed", "rejected"):
        memory.record(outcome(terminated_by=terminated_by))
    hop = next(choice for choice in choices().choices if choice.id == "hop+x")
    label = option_label(hop, memory, "scene-a", [0.0, 0.0, 0.0])
    assert "here: 7 tried" in label
    assert "blocked 2" in label
    assert "best +1.0" in label
    assert len(label) <= 90, (len(label), label)
    # A different cell, an unmeasured position and a different scene keep their own honest answers.
    assert option_label(hop, memory, "scene-a", [40.0, 0.0, 0.0]).endswith("here: untried")
    assert option_label(hop, memory, "scene-a", None).endswith("here: position unmeasured")
    assert option_label(hop, memory, "scene-b", [0.0, 0.0, 0.0]).endswith("here: untried")


def test_option_label_reads_a_spec_choice_without_params():
    memory = OutcomeMemory(cell=CELL)
    bare = Choice(id="hop-x", kind=ChoiceKind.option, option_id="hop-x", template="hop",
                  params={}, label="hop -x: run-up 11.30, rise 1.20 s, peak 2.48 \u2014 here: untried",
                  source=ChoiceSource.manifest)
    label = option_label(bare, memory, "scene-a", [0.0, 0.0, 0.0])
    assert label.startswith("hop -x") and "expectation unmeasured" in label


def test_progress_line_states_measured_positions():
    positions = [(-69.3, -0.8, 0.0), (-40.0, -0.8, 0.0), (-8.8, -0.8, 0.0)]
    line = progress_line(positions=positions, now=(-8.8, -0.8, 0.0), start=positions[0],
                         grounded=True, probe=None)
    assert line == (f"progress: furthest +x {MINUS}8.8 (start {MINUS}69.3), "
                    f"furthest {MINUS}x {MINUS}69.3; now ({MINUS}8.8, {MINUS}0.8) grounded")


def test_progress_line_reports_a_varied_ground_axis():
    positions = [(0.0, 0.0, 0.0), (0.0, 0.0, 2.5)]
    line = progress_line(positions=positions, now=(0.0, 0.0, 2.5), start=positions[0],
                         grounded=False, probe=None)
    assert "furthest +z 2.5" in line and f"furthest {MINUS}z 0.0" in line
    assert f"now (0.0, 2.5) not grounded" in line


def test_progress_line_keeps_unknowns_unmeasured():
    assert progress_line(positions=[], now=None, start=None, grounded=None, probe=None) == (
        "progress: unmeasured")
    line = progress_line(positions=[(1.0, 0.0, 0.0)], now=None, start=None, grounded=None, probe=None)
    assert "furthest +x 1.0 (start 1.0)" in line
    assert "now unmeasured" in line
    assert "grounded" not in line


def test_progress_line_summarises_the_probe():
    probe = {
        "under": {"hit": True, "delta_y": 0.0},
        "blockers": [{"height": 0.15, "max_distance": 1.8, "hit": True, "distance": 1.2},
                     {"height": 0.9, "max_distance": 1.8, "hit": False, "distance": None}],
        "samples": [{"forward": 0.6, "lateral": 0.0, "hit": True},
                    {"forward": 1.2, "lateral": 0.0, "hit": False}],
    }
    line = progress_line(positions=[(0.0, 0.0, 0.0)], now=(0.0, 0.0, 0.0), start=None,
                         grounded=True, probe=probe)
    assert "blocker 1.2 ahead" in line
    assert "floor edge 1.2 ahead" in line
    assert line.count(";") == 2


def test_progress_line_reports_a_gap_under_the_entity():
    probe = {"under": {"hit": False, "delta_y": None},
             "blockers": [{"height": 0.15, "max_distance": 1.8, "hit": False, "distance": None}]}
    line = progress_line(positions=[(0.0, 0.0, 0.0)], now=(0.0, 0.0, 0.0), start=None,
                         grounded=None, probe=probe)
    assert "no floor under" in line
    assert "no blocker within 1.8" in line
    assert "floor edge" not in line


def test_compact_menu_payload_has_no_ui_dump_and_no_variance():
    seen = observation(position=(-8.8, -0.8, 0.0), probe={"under": {"hit": True, "delta_y": 0.0}})
    memory = OutcomeMemory(cell=CELL)
    memory.record(outcome())
    directive = Directive(directive_id="d1", model="m", based_on_seq=3, text="Reach the far side.",
                          expected=Expected(field="public.unread", op=Comparator.eq, value=True))
    payload = compact_menu(
        objective="Reach the far side.", observation=seen, choices=choices(), memory=memory,
        directive=directive, pending_help={"kind": "escalate", "choice_id": "escalate"},
        facts=["one measured fact"], game_view="a note about the view",
        positions=[(-69.3, -0.8, 0.0), (-8.8, -0.8, 0.0)])
    assert set(payload) == {"objective", "progress", "directive", "pending_help", "facts", "game_view",
                            "set_version", "built_from_seq", "choices"}
    assert payload["set_version"] == 3 and payload["built_from_seq"] == 4
    assert payload["directive"] == {"id": "d1", "text": "Reach the far side.",
                                    "setpoint": "public.unread == true"}
    assert payload["facts"] == ["one measured fact"]
    assert payload["game_view"] == "a note about the view"
    assert payload["progress"].startswith("progress: furthest +x")
    assert payload["progress"].endswith("grounded")
    text = canonical(payload)
    for absent in ("variance", "n=", "world units", "canvases", "\"ui\"", "recent_history",
                   "choice_set_version", "observation"):
        assert absent not in text, absent
    assert [item["id"] for item in payload["choices"]] == [c.id for c in choices().choices]
    assert all("calibrated" not in item["label"] for item in payload["choices"])


def test_the_restart_label_says_it_drops_the_open_advice():
    """A restart invalidates the open directive; its label states that, and only while one is open."""
    built = {"objective": "o", "observation": observation(), "choices": choices(),
             "memory": OutcomeMemory(cell=CELL)}
    directive = Directive(directive_id="d1", model="m", based_on_seq=3, text="Jump the gap.")
    restart = lambda payload: next(row["label"] for row in payload["choices"] if row["id"] == "restart_play_mode")
    assert restart(compact_menu(**built)) == "restart · restart the session from the start"
    assert restart(compact_menu(**built, directive=directive)) == (
        "restart · restart the session from the start · drops the open advice")


def test_the_escalate_label_states_the_runs_help_record():
    """Asked and advised counts, and an open request, on the escalate label; nothing before the first request."""
    built = {"objective": "o", "observation": observation(), "choices": choices(),
             "memory": OutcomeMemory(cell=CELL)}
    escalate = lambda payload: next(row["label"] for row in payload["choices"] if row["id"] == "escalate")
    assert escalate(compact_menu(**built)) == "escalate · ask for help"
    assert escalate(compact_menu(**built, help_record=(0, 0))) == "escalate · ask for help"
    assert escalate(compact_menu(**built, help_record=(14, 0))) == (
        "escalate · ask for help · so far: 14 asked, 0 gave advice")
    pending = compact_menu(**built, help_record=(3, 1), pending_help={"kind": "escalate", "opened_turn": 9})
    assert escalate(pending) == "escalate · ask for help · so far: 3 asked, 1 gave advice; one pending"
    assert len(escalate(pending)) <= LABEL_LIMIT


def test_compact_menu_refuses_choices_bound_to_another_reading():
    with pytest.raises(ValueError):
        compact_menu(objective="o", observation=observation(seq=5), choices=choices(seq=4),
                     memory=OutcomeMemory(cell=CELL))


def test_the_payload_without_recent_keeps_its_bytes():
    """A run that has measured no choice sends exactly the state it sent before `recent` existed."""
    built = {"objective": "o", "observation": observation(), "choices": choices(),
             "memory": OutcomeMemory(cell=CELL)}
    plain = compact_menu(**built)
    assert set(plain) == {"objective", "progress", "directive", "pending_help", "facts", "game_view",
                          "set_version", "built_from_seq", "choices"}
    assert "recent" not in canonical(plain)
    assert canonical(plain) == canonical(compact_menu(**built, recent=()))
    assert canonical(plain) == canonical(compact_menu(**built, recent=[]))


def test_recent_states_the_last_six_choices_newest_last():
    memory = OutcomeMemory(cell=CELL)
    recent = [{"id": "traverse+x", "s": 1.25, "result": "succeeded"},
              {"id": "hop+x", "s": 0.5, "result": "blocked"},
              {"id": "move_forward", "s": 0.25, "result": "blocked"},
              {"id": "wait", "s": None, "result": "wait"},
              {"id": "finish", "s": None, "result": "unmeasured"},
              {"id": "hop+x", "s": None, "result": "fell"},
              {"id": "traverse+x", "s": 0.75, "result": "timed_out"}]
    payload = compact_menu(objective="o", observation=observation(), choices=choices(), memory=memory,
                           recent=recent)
    rows = payload["recent"]
    assert [row["id"] for row in rows] == ["hop+x", "move_forward", "wait", "finish", "hop+x", "traverse+x"]
    assert [row["result"] for row in rows] == ["blocked", "blocked", "wait", "unmeasured", "fell", "timed_out"]
    assert [row["s"] for row in rows] == [0.5, 0.25, None, None, None, 0.75]
    assert all(set(row) == {"id", "s", "result"} for row in rows)
    # The state text carries the same order, newest last, and costs the payload under 300 characters.
    text = canonical(payload)
    assert [row["id"] for row in json.loads(text)["recent"]] == [row["id"] for row in rows]
    without = canonical(compact_menu(objective="o", observation=observation(), choices=choices(),
                                     memory=memory))
    assert len(text) - len(without) < 300, len(text) - len(without)
    # Quantities only: no advice, no verdict word that recommends anything.
    for word in ("try", "should", "avoid", "repeat", "recommend", "best"):
        assert word not in text.lower()


def test_menu_request_asks_three_questions_over_the_same_criteria():
    payload = compact_menu(objective="o", observation=observation(), choices=choices(),
                           memory=OutcomeMemory(cell=CELL))
    body = menu_request(payload, "liquid-d1-ultra", contingent=True)
    questions = body["questions"]
    assert list(questions) == [PRIMARY, BLOCKED_QUESTION, SUCCEEDED_QUESTION]
    assert questions[PRIMARY]["criteria"] == questions[BLOCKED_QUESTION]["criteria"]
    assert questions[PRIMARY]["criteria"] == questions[SUCCEEDED_QUESTION]["criteria"]
    assert questions[PRIMARY]["criteria"] == {item["id"]: item["label"] for item in payload["choices"]}
    assert body["state"] is payload and body["model"] == "liquid-d1-ultra"
    assert questions[PRIMARY]["instructions"] != questions[BLOCKED_QUESTION]["instructions"]
    assert "blocked" in questions[BLOCKED_QUESTION]["instructions"]
    assert "succeeded" in questions[SUCCEEDED_QUESTION]["instructions"]
    assert json.loads(canonical(body))["questions"][PRIMARY]["type"] == "choice"
    plain = menu_request(payload, "liquid-d1-ultra", contingent=False)
    assert list(plain["questions"]) == [PRIMARY]


def test_a_plain_action_and_a_skill_keep_their_intent_clause():
    """A manifest label says what an action does; the tuned id says only which input it is."""
    memory = OutcomeMemory(cell=CELL)
    stick = Choice(id="stick_right", kind=ChoiceKind.action, action="stick_right",
                   source=ChoiceSource.manifest,
                   label=("move right on stick: request a 0.4-second hold of Gamepad leftStick (x=1, y=0); "
                          "calibrated net displacement 3.780 world units (sample variance 0.0012, n=4)."))
    jump = Choice(id="jump_pad", kind=ChoiceKind.action, action="jump_pad", source=ChoiceSource.manifest,
                  label=("jump: request a 0.2-second hold of Space; calibrated net displacement 1.204 world "
                         "units (sample variance 0.0031, n=5)."))
    skill = Choice(id="combo_right_jump", kind=ChoiceKind.skill, skill="combo_right_jump",
                   source=ChoiceSource.manifest,
                   label=("run and jump right: request this ordered sequence of 2 steps, at most 1.5 s in "
                          "total: step 1: hold right for 0.5 s; no calibrated effect is recorded for this "
                          "sequence."))
    label = option_label(stick, memory, "scene-a", [0.0, 0.0, 0.0])
    assert label == f"move right on stick{SEPARATOR}3.78 units{SEPARATOR}here: untried"
    assert option_label(jump, memory, "scene-a", [0.0, 0.0, 0.0]).startswith(
        f"jump{SEPARATOR}1.20 units{SEPARATOR}")
    assert option_label(skill, memory, "scene-a", [0.0, 0.0, 0.0]).startswith(
        f"run and jump right{SEPARATOR}effect unknown{SEPARATOR}")
    # A label with no clause at all still names the choice, and every label stays inside the limit.
    bare = Choice(id="plain_step", kind=ChoiceKind.action, action="plain_step",
                  source=ChoiceSource.manifest, label="plain_step")
    assert option_label(bare, memory, "scene-a", [0.0, 0.0, 0.0]).startswith("plain_step")
    long_label = Choice(id="long", kind=ChoiceKind.action, action="long", source=ChoiceSource.manifest,
                        label="hold right and walk steadily all the way to the far side of the pit and stop "
                              "there: calibrated net displacement 3.780 world units (sample variance 0.0012, n=3).")
    clipped = option_label(long_label, memory, "scene-a", [0.0, 0.0, 0.0])
    assert clipped.startswith("hold right and walk steadily all the way to the far side")
    assert len(clipped) <= 90 and clipped.endswith("\u2026")


def test_menu_request_attaches_the_game_view_as_images_zero():
    raw = b"a 640x480 jpeg standing in for the frame"
    payload = compact_menu(objective="o", observation=observation(), choices=choices(),
                           memory=OutcomeMemory(cell=CELL), game_view="The attached image is the current game view.")
    assert payload["game_view"] == "The attached image is the current game view."
    body = menu_request(payload, "d1-ultra", contingent=True, image=raw)
    url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    assert body["images"] == [url] and list(body) == ["model", "questions", "state", "images"]
    assert body["state"]["game_view"] == payload["game_view"]
    # No image, no key: the request is exactly the body the earlier bench sent.
    assert "images" not in menu_request(payload, "d1-ultra", contingent=True)
    # An arm that carries the image inside a message cannot take one in a plain state body.
    with pytest.raises(ValueError, match="plain images array"):
        menu_request(payload, "mammoth-1", contingent=True, image=raw, arm="mammoth-1")


def test_eight_option_menu_stays_small():
    memory = OutcomeMemory(cell=CELL)
    many = [option(f"traverse{i:+d}x", "traverse",
                   {"direction": [1, 0, 0], "distance": None, "action": "m", "speed": 3.1,
                    "step": 0.55}, "traverse +x: 3.10 u/s, step 0.55") for i in range(8)]
    many[0] = option("hop+x", "hop", {"direction": [1, 0, 0], "action": "m", "run_up": 11.3,
                                      "flight_s": 1.2, "rise_peak": 2.48, "speed": 9.2, "step": 0.5},
                     "hop +x: run-up 11.30, rise 1.20 s, peak 2.48")
    for terminated_by in ("blocked", "blocked", "fell"):
        memory.record(outcome(terminated_by=terminated_by))
    menu = ChoiceSet(set_version=9, built_from_seq=4, choices=[
        *many,
        Choice(id="wait", kind=ChoiceKind.wait, params={"duration_game_s": 0.5},
               source=ChoiceSource.core, label="Wait 0.5 s with no input"),
        Choice(id="escalate", kind=ChoiceKind.escalate, role="frontier", source=ChoiceSource.core,
               label="Ask the frontier model for help"),
        Choice(id="finish", kind=ChoiceKind.finish, source=ChoiceSource.core, label="Finish"),
    ])
    payload = compact_menu(objective="Reach the far side of the area without falling.", observation=observation(),
                           choices=menu, memory=memory, facts=["a measured fact"],
                           positions=[(0.0, 0.0, 0.0), (-8.8, -0.8, 0.0)])
    text = canonical(payload)
    assert len(text) < 6000, (len(text), text)
    assert len(canonical(menu_request(payload, "liquid-d1-ultra", contingent=True))) < 10000


def test_a_long_skill_keeps_its_scope_and_measured_history():
    memory = OutcomeMemory(cell=CELL)
    memory.record(OptionOutcome("combo", "skill", {}, [0, 0, 0], [6, -28, 0], [6, -28, 0],
                                "unmeasured", 1.5, scene="scene-a"))
    scope = "Effect outside the measured starting location is unmeasured."
    description = ("A measured compound input sequence " * 4) + ": " + scope
    skill = Choice(id="combo", kind=ChoiceKind.skill, skill="combo", label=description, source="manifest")
    offered = ChoiceSet(set_version=3, built_from_seq=4, choices=[skill])
    payload = compact_menu(objective="Explore.", observation=observation(), choices=offered, memory=memory)
    row = payload["choices"][0]
    assert row["id"] == "combo" and row["kind"] == "skill"
    assert row["details"] == description and scope in row["details"]
    assert "unmeasured 1" in row["label"] and "history" not in row   # the label carries it: not repeated
    assert len(row["label"]) <= 90
    request = menu_request(payload, "liquid-d1-ultra", contingent=True)
    assert request["questions"]["next_action"]["criteria"] == {"combo": row["label"]}
    assert "unused for action and skill" in request["questions"]["if_blocked"]["instructions"]


def test_conditional_finish_wording_remains_visible_in_compact_state():
    guard = "Finish only after the objective's completion conditions are measured as met."
    offered = ChoiceSet(set_version=3, built_from_seq=4, choices=[
        Choice(id="finish", kind=ChoiceKind.finish, label=guard, source="core")])
    row = compact_menu(objective="Explore.", observation=observation(), choices=offered,
                       memory=OutcomeMemory(cell=CELL))["choices"][0]
    assert row["details"] == guard and row["kind"] == "finish"


def test_a_raised_pivot_does_not_claim_the_body_is_airborne():
    seen = observation(probe={"under": {"hit": True, "delta_y": -1.2}})
    payload = compact_menu(objective="Explore.", observation=seen, choices=choices(),
                           memory=OutcomeMemory(cell=CELL), facts=["A declared contact reader reports true."])
    assert "not grounded" not in payload["progress"]
    assert payload["facts"] == ["A declared contact reader reports true."]


def test_a_held_met_directive_shows_its_setpoint_as_met():
    built = {"objective": "o", "observation": observation(), "choices": choices(),
             "memory": OutcomeMemory(cell=CELL)}
    directive = {"directive_id": "d1", "text": "Jump the gap.", "outcome": "met",
                 "expected": {"field": "public.grounded", "op": "==", "value": False}}
    assert compact_menu(**built, directive=directive)["directive"]["setpoint"].endswith("(met)")
    directive["outcome"] = "open"
    assert not compact_menu(**built, directive=directive)["directive"]["setpoint"].endswith("(met)")


def test_a_running_move_is_a_state_fact_and_never_changes_its_label():
    built = {"objective": "o", "observation": observation(), "choices": choices(),
             "memory": OutcomeMemory(cell=CELL)}
    plain = compact_menu(**built)
    running = compact_menu(**built, running={"choice_id": "hop+x", "running_s": 1.234})
    assert [row["label"] for row in running["choices"]] == [row["label"] for row in plain["choices"]]
    assert running["running"]["id"] == "hop+x" and running["running"]["s"] == 1.2
    assert "running" not in plain


def test_a_hop_that_presses_again_in_the_air_is_named_apart():
    double = option("hop2+x", "hop", {"direction": [1, 0, 0], "action": "m", "chord": "c", "run_up": 11.3,
                                      "flight_s": 1.9, "rise_peak": 5.3, "air_press_s": 0.3},
                    "hop ×2 +x: run-up 11.30, rise 1.90 s, peak 5.30")
    label = option_label(double, OutcomeMemory(cell=CELL), "scene-a", (0.0, 0.0, 0.0))
    assert label.startswith("hop ×2 +x") and "peak 5.30" in label


def test_a_fall_never_sets_the_best_advance():
    """Ground covered while falling below the route is not an advance: only an ending that held it counts."""
    memory = OutcomeMemory(cell=CELL)
    memory.record(outcome("hop+x", "fell"))
    memory.record(outcome("hop+x", "fell"))
    assert memory.suffix("scene-a", [0.0, 0.0, 0.0], "hop+x") == "here: 2 tried — fell 2"
    memory.record(outcome("hop+x", "succeeded"))
    assert "best +1" in memory.suffix("scene-a", [0.0, 0.0, 0.0], "hop+x")


def test_the_finish_label_states_that_the_scene_has_not_changed():
    built = {"objective": "o", "observation": observation(scene="scene-a"), "choices": choices(),
             "memory": OutcomeMemory(cell=CELL)}
    finish = lambda payload: next(row["label"] for row in payload["choices"] if row["id"] == "finish")
    assert finish(compact_menu(**built)) == "finish · end the session now"
    assert finish(compact_menu(**built, start_scene="scene-a")).endswith("scene unchanged since the run began")
    assert finish(compact_menu(**built, start_scene="scene-0")) == "finish · end the session now"


def test_untried_labels_say_when_nothing_tried_here_got_on():
    """Every choice tried here failed, or only went back toward the start: the untried labels say so."""
    memory = OutcomeMemory(cell=CELL)
    built = {"objective": "o", "observation": observation(position=(0.0, 0.0, 0.0)), "choices": choices(),
             "memory": memory, "positions": [(-60.0, 0.0, 0.0)]}
    hop = lambda payload: next(row["label"] for row in payload["choices"] if row["id"] == "move_forward")
    assert "none got on" not in hop(compact_menu(**built))
    for option_id in ("hop+x", "traverse+x"):
        memory.record(outcome(option_id, "blocked"))
    memory.record(outcome("retreat+x:1.2", "succeeded", direction=(-1.0, 0.0, 0.0)))  # back toward the start
    assert hop(compact_menu(**built)).endswith("3 others tried here, none got on")
    memory.record(outcome("traverse+x", "succeeded"))                                  # onward: not a dead end
    assert "none got on" not in hop(compact_menu(**built))


def test_a_body_relative_traverse_is_named_by_its_body_axis():
    forward = option("traverse:forward", "traverse",
                     {"direction": [0.0, 0.0, 1.0], "basis": "body", "label": "forward", "speed": 5.0, "step": 0.4},
                     "traverse forward: 5.00 u/s, step 0.40")
    label = option_label(forward, OutcomeMemory(cell=CELL), "scene-a", (0.0, 0.0, 0.0))
    assert label.startswith("traverse forward") and "+z" not in label
