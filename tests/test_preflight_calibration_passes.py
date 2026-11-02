"""What a calibration pass does with a game that changes its own state between trials.

Every fixture here is synthetic: invented ids, no game, key or scene names. The rules under test are
general. A game state change no control produced is recorded as an unattributed session event with the
readings that measured it, it blames no control, and the pass ends without pressing another control. A
control that pass finished measuring is carried into the next pass with its own trials, held windows and
its own session's idle baseline, and is never pressed again. A control interrupted partway is measured
whole in one later session. Neither rule changes what a trial says about its own press.
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from playtest.preflight import calibrate

from test_preflight_calibration_clock import RecordingBridge, recorder, selected

ONE_HOLD = (0.1,)
TWO_HOLDS = (0.1, 0.6)


class WorldEventBridge(RecordingBridge):
    """The game keeps running through every press, then changes its own state between two trials.

    `after_acts` presses are allowed to finish, each with its own closing read. The world then changes
    with no input: after the closing read of that trial, so the change is inside the gap between two
    trials and never inside a trial's own window.
    """

    def __init__(self, controls=("one",), *, after_acts=1, event="stop", value=1, **kwargs):
        super().__init__(controls=controls, **kwargs)
        self.after_acts = after_acts
        self.world_event = event
        self.value = value
        self.acts_done = 0
        self.closing_read = False

    async def act_with_inspections(self, **kwargs):
        # The same held-input trial as the parent, with time advancing during the field sampling so each
        # sample of the window carries its own measured `real_time_s`, as a real one does.
        control = self._control(kwargs)
        self.events.append("act:" + control)
        self.position += 1
        self.held = control
        start = 1000 + self.real
        samples = [await self.inspect("fields") for _ in range(4)]
        self.held = None
        self._press(control)
        return [self.record("completed", start, 1000 + self.real + 0.1)], samples

    def _press(self, control):
        super()._press(control)
        self.acts_done += 1

    def record(self, status, start, end):
        # The release walls are part of the execution record a session event quotes.
        return SimpleNamespace(status=SimpleNamespace(value=status), release_verified=status == "completed",
                               input_start_wall_s=start, input_end_wall_s=end,
                               model_dump=lambda **_: {"status": status, "release_verified": status == "completed",
                                                       "input_start_wall_s": start, "input_end_wall_s": end})

    def _world_change(self):
        if self.world_event == "scene":
            self.scene = "scene-two"
        else:
            self.game_frozen = True

    async def inspect(self, stage):
        response = await super().inspect(stage)
        if stage == "transforms" and self.world_event and self.acts_done >= self.after_acts:
            self.closing_read = True
        if stage == "fields":
            for field in response.data["fields"]:
                field["value"] = self.value
        return response

    async def observe_once(self):
        observation = await super().observe_once()
        if self.closing_read and self.world_event:
            # The trial's own closing observation is already taken; the world changes after it.
            self.closing_read = False
            self._world_change()
        return observation


def plan(monkeypatch, holds=ONE_HOLD):
    """A short synthetic plan: one hold per control, no repeats, one held-window sample set."""
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", holds)
    monkeypatch.setattr(calibrate, "REPEATS", 1)
    monkeypatch.setattr(calibrate, "FIELD_REPEATS", 1)


def run(bridge, monkeypatch, *, controls=None, restore=None, prior=None, holds=ONE_HOLD):
    plan(monkeypatch, holds)
    return calibrate.asyncio.run(calibrate.measure(bridge, selected(*(controls or bridge.controls)),
                                                   restore=restore, prior=prior))


def trials_for(result, control):
    return [trial for trial in result["trials"] if trial["control"] == control]


def prior_document(state_changes=None, sessions=None):
    return {"schema_version": 1, "state_changes": state_changes or [], "sessions": sessions or []}


def test_a_game_that_changes_between_trials_is_an_unattributed_event_that_ends_the_pass(monkeypatch):
    bridge = WorldEventBridge(controls=("one", "two"), after_acts=1)
    result = run(bridge, monkeypatch)
    events = calibrate.unattributed_events(result)
    assert len(events) == 1
    event = events[0]
    # Nothing was pressed: the event is filed among the trials for its evidence but is not a trial.
    assert event["unattributed"] is True and event["kind"] == "session_event"
    assert event["control"] is None and event["valid"] is None and event["error"] is None
    assert event["cause"] == "unknown"
    # It says where it sits: after the last trial that finished, at its own index in the record.
    assert event["trial"] == 1 and event["after_trial"] == 0
    last = result["trials"][event["after_trial"]]
    assert last["control"] == event["last_control"] == "one"
    assert event["last_release_wall_s"] == last["execution"][-1]["input_end_wall_s"]
    # It carries measured readings, not an inference about who stopped the game.
    assert event["clock"]["verdict"] == "stopped"
    assert event["clock"]["game_time_s"]["delta_s"] == 0.0
    assert event["clock"]["real_time_s"]["delta_s"] > 0.1
    assert event["position"] == last["after"]["position"] == [1.0, 0, 0]
    assert "between two trials" in event["reason"] and "no input" in event["reason"]
    # No control is blamed, and the pass pressed nothing after the change.
    assert all(trial.get("state_change") is None for trial in result["trials"])
    assert "act:two" not in bridge.events
    assert sum(1 for item in bridge.events if item.startswith("act:")) == 1
    assert result["status"] == "inconclusive"
    assert "without pressing another control" in result["error"]
    # Everything this pass did finish is still summarised for a later pass to carry.
    assert result["freshness"]["field"]["idle"]["sample_count"] == len(result["idle_fields"])
    assert [summary["control"] for summary in result["action_summaries"]] == ["one", "two"]
    assert calibrate.remaining_controls(result) == ["two"]
    assert [entry["control"] for entry in calibrate.completed_controls(result)] == ["one"]
    # The event is a recorded fact about the session, not one of the pass's trials.
    assert result["plan"]["budget"]["trials_recorded"] == 1


def test_a_control_interrupted_between_trials_is_measured_whole_in_the_next_pass(monkeypatch):
    first = WorldEventBridge(controls=("one", "two"), after_acts=1)
    record = run(first, monkeypatch, holds=TWO_HOLDS)
    assert calibrate.completed_controls(record) == []
    assert calibrate.remaining_controls(record) == ["one", "two"]
    prior = calibrate.merge_prior(prior_document(), record, 1)
    # A pass that finished no control carries only its session event, with its own baseline.
    assert prior["state_changes"] == []
    assert len(prior["sessions"]) == 1
    session = prior["sessions"][0]
    assert session["controls"] == [] and session["idle_fields"] == record["idle_fields"]
    assert [event["trial"] for event in session["session_events"]] == [1]
    # The next pass presses the interrupted control again and measures its whole trial set here.
    second = WorldEventBridge(controls=("one", "two"), after_acts=100)
    carried = run(second, monkeypatch, prior=prior, holds=TWO_HOLDS)
    measured = trials_for(carried, "one")
    assert len(measured) == 2
    assert all(trial["valid"] is True and "prior_pass" not in trial for trial in measured)
    assert [trial["requested_hold_s"] for trial in measured] == [0.1, 0.6]
    assert second.events.count("act:one") == 2
    # The interrupted pass's evidence is still in the record, marked with the pass that measured it.
    events = calibrate.unattributed_events(carried)
    assert len(events) == 1 and events[0]["prior_pass"] == 1
    assert events[0]["clock"]["verdict"] == "stopped"
    assert [entry["control"] for entry in calibrate.completed_controls(carried)] == ["one", "two"]


def test_a_completed_control_is_carried_with_its_own_baseline_and_never_pressed_again(monkeypatch):
    first = WorldEventBridge(controls=("one",), value=1)
    record = run(first, monkeypatch)
    assert [entry["control"] for entry in calibrate.completed_controls(record)] == ["one"]
    prior = calibrate.merge_prior(prior_document(), record, 1)
    session = prior["sessions"][0]
    assert session["pass"] == 1
    assert session["idle_fields"] == record["idle_fields"]
    assert session["controls"][0]["input_field_segments"] == record["input_field_segments"]
    # A second session with a different baseline measures the remaining control only.
    second = WorldEventBridge(controls=("one", "two"), value=7)
    carried = run(second, monkeypatch, prior=prior)
    assert "act:one" not in second.events
    carried_trials = trials_for(carried, "one")
    assert len(carried_trials) == 1
    assert carried_trials[0]["prior_pass"] == 1 and carried_trials[0]["valid"] is True
    assert carried_trials[0] == dict(record["trials"][0], prior_pass=1)
    assert trials_for(carried, "two") and all(trial["valid"] is True for trial in trials_for(carried, "two"))
    # The carried measurement keeps its own session's held window, tagged with the baseline it belongs to.
    carried_segments = [segment for segment in carried["input_field_segments"] if segment.get("prior_pass")]
    assert len(carried_segments) == 1
    assert carried_segments[0]["baseline_pass"] == 1 and carried_segments[0]["prior_pass"] == 1
    assert carried_segments[0]["samples"] == record["input_field_segments"][0]["samples"]
    own_segments = [segment for segment in carried["input_field_segments"] if "prior_pass" not in segment]
    assert [segment["control"] for segment in own_segments] == ["two"]
    # Each session's idle baseline stays a window of its own, and the plan says which is which.
    assert carried["later_idle_windows"][0] == session["idle_fields"]
    assert carried["later_idle_windows"][1] == session["later_idle_windows"][0]
    assert carried["plan"]["prior_sessions"] == [{"pass": 1, "controls": ["one"], "idle_fields_window": 0,
                                                  "later_idle_windows": [0, 1]}]
    assert carried["idle_fields"] != session["idle_fields"]
    # Two baselines that disagree are two windows: nothing is compared across the edge between them.
    idle = carried["freshness"]["field"]["idle"]
    assert idle["segment_count"] == 1 + len(carried["later_idle_windows"]) == 4
    assert idle["segments"][1]["sample_count"] == len(session["idle_fields"])
    assert idle["changed_intervals"] == 0 and idle["classification"] == "no_change_observed"
    # The carried control is not measured again: this pass completes only the control it pressed.
    assert [entry["control"] for entry in calibrate.completed_controls(carried)] == ["two"]
    assert calibrate.remaining_controls(carried, prior) == []


def test_three_passes_converge_with_every_control_carried_and_every_event_kept(monkeypatch):
    prior = prior_document()
    remaining_by_pass = []
    for pass_number in (1, 2, 3):
        bridge = WorldEventBridge(controls=("one", "two", "three"), after_acts=1)
        record = run(bridge, monkeypatch, prior=prior)
        remaining = calibrate.remaining_controls(record, prior)
        remaining_by_pass.append(sorted(remaining))
        assert calibrate.new_state_changes(record, prior) == []
        prior = calibrate.merge_prior(prior, record, pass_number)
    assert remaining_by_pass == [["three", "two"], ["two"], []]
    assert [session["pass"] for session in prior["sessions"]] == [1, 2, 3]
    # The first two passes stopped between trials; the third ran out of controls to press and finished.
    assert [len(session["session_events"]) for session in prior["sessions"]] == [1, 1, 0]
    assert [control["control"] for session in prior["sessions"] for control in session["controls"]] == \
        ["one", "three", "two"]
    # The pass with nothing left to measure assembles the record from what earlier sessions settled.
    final = run(WorldEventBridge(controls=("one", "two", "three")), monkeypatch, prior=prior)
    assert final["status"] == "measured_with_gaps"
    assert calibrate.remaining_controls(final, prior) == []
    assert final["action_summaries"] and all(summary["control"] for summary in final["action_summaries"])
    assert [trial["prior_pass"] for trial in trials_for(final, "one")] == [1]
    assert [trial["prior_pass"] for trial in trials_for(final, "three")] == [2]
    assert [trial["prior_pass"] for trial in trials_for(final, "two")] == [3]
    events = calibrate.unattributed_events(final)
    assert [event["prior_pass"] for event in events] == [1, 2]
    assert all(event["cause"] == "unknown" and event["control"] is None for event in events)
    # Assembling added no new session, and no control is listed twice.
    assert calibrate.merge_prior(prior, final, 4) == prior


def test_a_change_across_a_trial_is_still_attributed_to_its_press(monkeypatch):
    bridge = WorldEventBridge(controls=("one", "two"), event=None, stops_on="one")
    calls = []
    result = run(bridge, monkeypatch, restore=recorder(bridge, calls), holds=TWO_HOLDS)
    stopped = trials_for(result, "one")[0]
    assert "unattributed" not in stopped and stopped["control"] == "one"
    assert stopped["state_change"]["control"] == "one"
    assert stopped["state_change"]["evidence"]["verdict"] == "stopped"
    assert "the game clock stopped" in stopped["error"]
    assert len(calls) == 1
    # The in-trial change is not a session event, and no between-trial check fired around it.
    assert calibrate.unattributed_events(result) == []
    assert result["trials"][1]["control"] == "two"
    assert calibrate.remaining_controls(result) == []
    assert result["status"] == "measured_with_gaps"


def test_a_single_pass_with_no_prior_is_unchanged(monkeypatch):
    bridge = WorldEventBridge(controls=("one", "two"), event=None)
    result = run(bridge, monkeypatch)
    assert result["status"] == "measured_with_gaps"
    assert len(result["trials"]) == 2 and all(trial["valid"] is True for trial in result["trials"])
    assert all("prior_pass" not in trial for trial in result["trials"])
    assert all("prior_pass" not in segment and "baseline_pass" not in segment
               for segment in result["input_field_segments"])
    assert calibrate.unattributed_events(result) == []
    assert "prior_sessions" not in result["plan"]
    assert len(result["later_idle_windows"]) == 1
    assert result["plan"]["budget"]["trials_recorded"] == 2
    assert calibrate.remaining_controls(result) == []
    assert [entry["control"] for entry in calibrate.completed_controls(result)] == ["one", "two"]


def load_driver():
    """The pass driver, loaded by path: its file name has a hyphen and it is not a package module."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "unity" / "calibrate-passes.py"
    spec = importlib.util.spec_from_file_location("calibrate_passes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_driver_only_carries_a_change_its_own_trial_attributed():
    driver = load_driver()
    attributed = [{"control": "one", "clock": {"verdict": "stopped"}},
                  {"control": "two", "clock": {"verdict": "scene_changed"}}]
    assert driver.stopped_before_press(attributed) == []
    assert driver.stopped_before_press(attributed + [{"control": "three", "clock": {"verdict": "unknown"}},
                                                     {"control": "four", "clock": {"verdict": "running"}},
                                                     {"control": "five"}]) == \
        ["three", "four", "five"]


def test_the_driver_carries_a_pass_the_game_ended_between_trials():
    driver = load_driver()
    stopped = {"trials": [{"control": "one", "valid": True},
                          {"control": None, "unattributed": True, "cause": "unknown", "prior_pass": None}]}
    carried = {"trials": [{"control": "one", "valid": True},
                          {"control": None, "unattributed": True, "cause": "unknown", "prior_pass": 1}]}
    measured = {"trials": [{"control": "one", "valid": True}]}
    assert driver.ended_by_session_event(stopped) is True
    assert driver.ended_by_session_event(carried) is False
    assert driver.ended_by_session_event(measured) is False


def session(pass_number=1, control="one"):
    """One carried session in the shape `merge_prior` writes, for the rejection cases below."""
    return {"pass": pass_number,
            "idle_fields": [{"real_time_s": 1.0, "fields": []}],
            "later_idle_windows": [],
            "controls": [{"control": control,
                          "trials": [{"control": control, "requested_hold_s": 0.1, "valid": True}],
                          "input_field_segments": []}],
            "session_events": []}


def changed_session(**changes):
    entry = session()
    entry.update(changes)
    return {"state_changes": [], "sessions": [entry]}


def test_a_prior_file_round_trips_its_sessions(monkeypatch, tmp_path):
    record = run(WorldEventBridge(controls=("one", "two"), after_acts=1), monkeypatch)
    prior = calibrate.merge_prior(prior_document(), record, 1)
    path = tmp_path / "prior.json"
    path.write_text(json.dumps(prior), encoding="utf-8")
    loaded = calibrate.load_prior_state_changes(path)
    # The file is the prior document as written: JSON turns the record's vector tuples into lists, which
    # is how the record itself is stored too.
    assert loaded == json.loads(json.dumps(prior))
    assert loaded["sessions"][0]["controls"][0]["trials"][0]["control"] == "one"
    assert loaded["sessions"][0]["session_events"][0]["unattributed"] is True


@pytest.mark.parametrize("payload", [
    {"state_changes": [], "sessions": {}},
    {"state_changes": [], "sessions": [{}]},
    changed_session(**{"pass": 0}),
    changed_session(**{"pass": "one"}),
    changed_session(idle_fields=[]),
    changed_session(idle_fields=["not a sample"]),
    changed_session(later_idle_windows=["not a window"]),
    changed_session(later_idle_windows=[["not a sample"]]),
    changed_session(controls=[], session_events=[]),
    changed_session(extra_key=1),
    changed_session(controls=[{"control": "one", "trials": [], "input_field_segments": []}]),
    changed_session(controls=[{"control": "one", "input_field_segments": []}]),
    changed_session(controls=[{"control": "one", "trials": [{"control": "two", "valid": True}],
                              "input_field_segments": []}]),
    changed_session(controls=[{"control": "one", "trials": [{"control": "one", "valid": None}],
                              "input_field_segments": []}]),
    changed_session(controls=[{"control": "one", "trials": [
        {"control": "one", "valid": True, "state_change": {"control": "one"}}], "input_field_segments": []}]),
    changed_session(controls=[{"control": "one", "trials": [{"control": "one", "valid": True}],
                              "input_field_segments": ["not a segment"]}]),
    changed_session(session_events=[{"unattributed": True, "control": "one", "cause": "unknown"}]),
    changed_session(session_events=[{"unattributed": True, "control": None, "cause": "input"}]),
    changed_session(session_events=[{"unattributed": False, "control": None, "cause": "unknown"}]),
    {"state_changes": [], "sessions": [session(pass_number=1), session(pass_number=1)]},
    {"state_changes": [], "sessions": [session(pass_number=1), session(pass_number=2)]},
    {"state_changes": [{"control": "one", "state_change": {"control": "one"}}], "sessions": [session()]},
    {"state_changes": [{"control": "one", "state_change": {"control": "one"}}],
     "sessions": [session(pass_number=2, control="one")]},
])
def test_a_malformed_prior_session_is_rejected(payload, tmp_path):
    path = tmp_path / "prior.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        calibrate.load_prior_state_changes(path)


def test_an_old_prior_file_of_state_changes_only_is_still_accepted():
    prior = calibrate._prior_document({"state_changes": [{"control": "one",
                                                          "state_change": {"control": "one"}}]})
    assert prior["sessions"] == []
    assert [entry["control"] for entry in prior["state_changes"]] == ["one"]
