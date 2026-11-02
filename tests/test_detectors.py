"""The generic detectors: measured facts about the run itself, raised as suspicions with evidence.

Each detector states the value it measured and the bound it crossed, and every bound comes from something
this run measured: the finite readings of the same component, the floor the body stood on, the intervals
before this one, the monitor's own sequence, the maximum the run declared. These tests pin what is stated,
when it is raised (once per episode, after the body returned inside and crossed again), that it is silent
when its bound is unmeasured, and that the detectors are opt-in and numbered after the readers' suspicions.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from runner_manifest import manifest as build_manifest

from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import SuspicionBasis
from playtest.core.suspicion import ReaderWatch
from playtest.runner import cli
from playtest.runner.detectors import (
    FALL_PEAK_FACTOR,
    HITCH_K,
    HITCH_MIN_INTERVALS,
    HITCH_WINDOW,
    DetectorWatch,
    FellOut,
    Hitch,
    NonFinitePose,
    ReaderOverMax,
    RuntimeException,
    build_detectors,
    detectors_config,
    view_of,
)

START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
REQUIRED = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
DEFAULT = object()


def observation(seq: int, *, position=None, velocity=None, floor=None, game_s=DEFAULT, wall_s=None,
                scale: float = 1.0, monitor=None, **public) -> Observation:
    """One observation the detectors may read: a pose, a down-ray surface and a monitor are optional."""
    body: dict[str, object] = {}
    if position is not None:
        body["position"] = list(position)
    if velocity is not None:
        body["velocity"] = list(velocity)
    if body:
        public["controlled"] = body
    if floor is not None:
        public["probe"] = {"under": {"hit": True, "point": [0.0, floor, 0.0]}}
    if monitor is not None:
        public["runtime_errors"] = monitor
    return Observation(session="s", seq=seq, game_s=seq * 0.2 if game_s is DEFAULT else game_s,
                       wall_utc=START + timedelta(seconds=0.2 * seq if wall_s is None else wall_s),
                       clock_mode="realtime", scene="main", playing=True, paused=False, time_scale=scale,
                       frame=seq, public=public, ui=None, frame_ref=None, reader_errors=[])


def views(*observations: Observation) -> list:
    return [view_of(item, turn=index, trace_seq=100 + index) for index, item in enumerate(observations)]


def found(detector, *observations: Observation) -> list:
    return [detection for view in views(*observations) for detection in detector.observe(view)]


def standing(seq: int, y: float, *, velocity=(0.0, 0.0, 0.0), **kwargs) -> Observation:
    """The body at rest on a surface: the only observation that measures a floor."""
    return observation(seq, position=[0.0, y, 0.0], velocity=list(velocity), floor=y, **kwargs)


def falling(seq: int, y: float, **kwargs) -> Observation:
    return observation(seq, position=[0.0, y, 0.0], velocity=[0.0, -1.0, 0.0], **kwargs)


def pace(seq: int, wall_s: float, game_s, *, scale: float = 1.0, **kwargs) -> Observation:
    """An observation at a given wall and game time: the numbers the hitch compares."""
    return observation(seq, wall_s=wall_s, game_s=game_s, scale=scale, **kwargs)


def error_event(sequence, message: str = "NullReferenceException: object reference not set", *,
                kind: str = "Exception", count: int = 1) -> dict:
    return {"type": kind, "message": message, "first_sequence": sequence, "last_sequence": sequence,
            "count": count}


def monitor(*events: dict, observed: int | None = None, truncated: bool = False) -> dict:
    return {"observed": sum(event.get("count", 1) for event in events) if observed is None else observed,
            "events": list(events), "truncated": truncated}


# --- the detectors and the plan's record of them -------------------------------------------------


def test_the_five_detectors_are_built_in_a_fixed_order_and_the_plan_records_each_bound():
    detectors = build_detectors(rise_peak=2.5, peak_source="the calibration's measured rise peak")
    assert [detector.name for detector in detectors] == [
        "non_finite_pose", "fell_out", "hitch", "runtime_exception", "reader_over_max"]
    config = detectors_config(detectors)
    assert config["enabled"] is True and config["names"] == [item.name for item in detectors]
    assert config["fell_out"]["rise_peak"] == 2.5 and config["fell_out"]["factor"] == FALL_PEAK_FACTOR
    assert config["fell_out"]["silent"] is None
    assert config["hitch"]["k"] == HITCH_K == 5 and config["hitch"]["window"] == HITCH_WINDOW == 40
    assert config["hitch"]["min_intervals"] == HITCH_MIN_INTERVALS
    assert config["runtime_exception"]["source"] == "public.runtime_errors"
    assert "declared maximum" in config["reader_over_max"]["silent"]


def test_with_no_measured_rise_the_fall_bound_is_recorded_as_unknown_and_the_detector_stays_silent():
    detectors = build_detectors(rise_peak=None, peak_source="the calibration measured no rise")
    fall = next(detector for detector in detectors if detector.name == "fell_out")
    assert detectors_config([fall])["fell_out"]["rise_peak"] == "unknown"
    assert "no measured rise peak" in detectors_config([fall])["fell_out"]["silent"]
    assert found(fall, standing(1, -2.8), falling(2, -1000.0)) == []


def test_a_declared_maximum_reaches_the_plan_and_the_detector():
    detectors = build_detectors(rise_peak=None, peak_source="unknown", maxima={"r": 10.0})
    over = next(detector for detector in detectors if detector.name == "reader_over_max")
    assert detectors_config([over])["reader_over_max"]["maxima"] == {"r": 10.0}
    assert found(over, observation(1, r=12.0))[0].basis is SuspicionBasis.reader_over_max


# --- a pose or velocity component that is not a finite number ------------------------------------


def test_a_non_finite_position_component_is_stated_once_with_the_finite_readings_it_crossed():
    detector = NonFinitePose()
    detections = found(
        detector,
        observation(1, position=[1.0, 2.0, 0.0], velocity=[0.0, 0.0, 0.0]),
        observation(2, position=[1.5, math.nan, 0.0], velocity=[0.0, 0.0, 0.0]),
        observation(3, position=[1.25, math.inf, 0.0], velocity=[0.0, 0.0, 0.0]),   # the same episode
        observation(4, position=[1.0, 3.0, 0.0], velocity=[0.0, 0.0, 0.0]),         # back inside
        observation(5, position=[1.0, math.nan, 0.0], velocity=[0.0, 0.0, 0.0]),    # a new episode
    )
    assert len(detections) == 2
    first, second = detections
    assert first.basis is SuspicionBasis.non_finite_pose and first.subject == "public.controlled.position"
    assert first.statement == (
        "public.controlled.position has a component that is not a finite number: component 1 is nan, which is "
        "not a finite number; its finite readings so far were 2 to 2 (1 reading).")
    assert first.evidence == (101,)
    assert first.detail["field"] == "public.controlled.position" and first.detail["components"] == [1]
    assert math.isnan(first.detail["value"][1])
    assert first.detail["measured"] == [{"component": 1, "value": math.nan, "finite_min": 2.0,
                                         "finite_max": 2.0, "readings": 1}]
    assert second.statement.endswith("its finite readings so far were 2 to 3 (2 readings).")


def test_a_non_finite_velocity_component_is_measured_against_its_own_finite_readings():
    detector = NonFinitePose()
    (detection,) = found(detector, observation(1, velocity=[0.0, 0.0, 0.0]),
                         observation(2, velocity=[0.0, math.inf, 0.0]))
    assert detection.subject == "public.controlled.velocity"
    assert detection.statement == (
        "public.controlled.velocity has a component that is not a finite number: component 1 is inf, which is "
        "not a finite number; its finite readings so far were 0 to 0 (1 reading).")
    assert detection.evidence == (101,)


def test_an_unmeasured_pose_neither_raises_nor_ends_an_episode():
    detector = NonFinitePose()
    detections = found(
        detector,
        observation(1, position=[0.0, math.nan, 0.0]),
        observation(2, velocity=[0.0, 0.0, 0.0]),                      # no pose at all: unmeasured
        observation(3, position=[1.0, "high", 2.0]),                   # not a vector of numbers: unmeasured
        observation(4, position=[0.0, math.nan, 0.0]),                 # still the first episode
        observation(5, position=[0.0, 1.0, 0.0]),                      # back inside
        observation(6, position=[0.0, math.nan, 0.0]),                 # a new episode
    )
    assert [detection.evidence for detection in detections] == [(100,), (105,)]


# --- below the lowest floor the body was measured standing on -----------------------------------


def test_a_fall_below_the_lowest_measured_floor_by_twice_the_measured_rise_is_stated():
    detector = FellOut(2.0, peak_source="the calibration's measured rise peak")
    (detection,) = found(detector, standing(1, -2.8), falling(2, -9.0))
    assert detection.basis is SuspicionBasis.fell_out and detection.subject == "public.controlled.position"
    assert detection.statement == (
        "public.controlled.position y is -9, below the lowest floor measured for it so far (-2.8, turn 0, "
        "trace_seq 100), by 6.2; the bound is 2 times the measured rise peak 2 below that floor: -6.8.")
    assert detection.evidence == (100, 101)
    assert detection.detail == {"field": "public.controlled.position", "y": -9.0, "floor_y": -2.8,
                                "floor_turn": 0, "floor_seq": 100, "depth": 6.2, "rise_peak": 2.0,
                                "peak_source": "the calibration's measured rise peak", "factor": 2.0,
                                "bound_y": -6.8, "surface_y": None}


def test_a_fall_is_stated_once_and_again_only_after_the_body_came_back_inside():
    detector = FellOut(2.0, peak_source="the calibration's measured rise peak")
    detections = found(detector, standing(1, -2.8), falling(2, -9.0), falling(3, -12.0),
                       falling(4, -3.0), falling(5, -9.0))
    assert len(detections) == 2


def test_the_bound_is_strict_so_the_lowest_y_inside_it_is_not_a_fall():
    detector = FellOut(2.0, peak_source="the calibration's measured rise peak")
    assert found(detector, standing(1, -2.8), falling(2, -6.8)) == []


def test_a_pit_the_body_comes_to_rest_in_is_a_floor_so_a_game_floor_is_not_a_fall():
    detector = FellOut(2.0, peak_source="the calibration's measured rise peak")
    observations = [standing(1, -2.8), falling(2, -4.8), standing(3, -4.8), falling(4, -4.8)]
    assert found(detector, *observations) == []
    assert detector.config()["floor"].startswith("the body y of an observation whose velocity was zero")


def test_no_measured_floor_or_no_finite_y_states_nothing():
    detector = FellOut(2.0, peak_source="the calibration's measured rise peak")
    assert found(detector, falling(1, -1000.0)) == []                 # never stood on anything
    assert found(detector, standing(1, math.nan), falling(2, -1000.0)) == []


def test_the_fall_bound_must_be_a_positive_finite_rise():
    for bad in (0.0, -1.0, math.nan, math.inf):
        with pytest.raises(ValueError):
            FellOut(bad, peak_source="a test peak")
    with pytest.raises(ValueError):
        FellOut(2.0, peak_source="a test peak", factor=0.0)


# --- an observation interval far longer than this run's recent ones ------------------------------


def base_pace(count: int = 9) -> list[Observation]:
    return [pace(seq, 0.2 * (seq - 1), 0.2 * (seq - 1)) for seq in range(1, count + 1)]


def test_a_hitch_is_a_wall_interval_far_longer_than_recent_ones_in_which_the_game_clock_lost_the_time():
    detector = Hitch()
    (detection,) = found(detector, *base_pace(), pace(10, 3.6, 1.6))
    assert detection.basis is SuspicionBasis.hitch and detection.subject == "observation interval"
    assert detection.statement == (
        "The wall clock advanced 2 s between observation seq 9 (frame 9, game 1.6 s) and seq 10 (frame 10, "
        "game 1.6 s), more than 5 times the median 0.2 s of the last 8 intervals (1 s); the game clock "
        "stalled in that interval: it advanced 0 s, below the 0.4 s it had to advance to keep pace with the "
        "wall clock at time scale 1 (2 s of game time was due).")
    assert detection.evidence == (108, 109)
    assert detection.detail["wall_s"] == 2.0 and detection.detail["game_s"] == 0.0
    assert detection.detail["median_wall_s"] == 0.2 and detection.detail["bound_s"] == 1.0
    assert detection.detail["intervals"] == 8 and detection.detail["stalled"] is True
    assert (detection.detail["time_scale"], detection.detail["game_due_s"],
            detection.detail["min_game_s"]) == (1.0, 2.0, 0.4)
    assert (detection.detail["from_seq"], detection.detail["to_seq"]) == (9, 10)


def test_a_slow_turn_that_advanced_the_game_clock_with_the_wall_clock_is_not_a_hitch():
    detector = Hitch()
    assert found(detector, *base_pace(), pace(10, 3.6, 3.6)) == []
    scaled = Hitch()
    page = [*base_pace(), pace(10, 3.6, 3.4, scale=0.5)]   # 1.8 s advanced where 1.0 s was due at time scale 0.5
    assert found(scaled, *page) == []


def test_a_game_clock_that_fell_behind_without_stopping_is_a_hitch_too():
    detector = Hitch()
    (detection,) = found(detector, *base_pace(), pace(10, 3.6, 1.8))
    assert detection.detail["stalled"] is False and round(detection.detail["game_s"], 6) == 0.2
    assert detection.statement.endswith(
        "it advanced 0.2 s, below the 0.4 s it had to advance to keep pace with the wall clock at time scale "
        "1 (2 s of game time was due).")


def test_a_game_clock_that_went_backwards_is_a_reset_not_a_pace_to_measure():
    assert found(Hitch(), *base_pace(), pace(10, 3.6, 0.5)) == []


def test_a_hitch_needs_enough_earlier_intervals_and_a_game_clock_to_disagree():
    detector = Hitch()
    assert found(detector, *base_pace(8), pace(9, 2.4, 1.4)) == []       # seven earlier intervals
    (detection,) = found(Hitch(), *base_pace(8), pace(9, 2.4, 1.4), pace(10, 4.6, 1.4))
    assert detection.detail["intervals"] == 8
    quiet = Hitch()
    assert found(quiet, *[pace(seq, 0.2 * (seq - 1), None) for seq in range(1, 10)], pace(10, 3.6, None)) == []


def test_a_hitch_is_stated_once_per_episode_and_a_restart_drops_the_baseline():
    detector = Hitch()
    page = [*base_pace(), pace(10, 3.6, 1.6), pace(11, 6.0, 1.8)]
    assert len(found(detector, *page)) == 1
    detector.rebind()
    assert found(detector, pace(12, 60.0, 1.9)) == []


def test_the_hitch_constants_must_make_a_window_a_measurement_can_use():
    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            Hitch(k=bad)
    with pytest.raises(ValueError):
        Hitch(min_intervals=HITCH_WINDOW + 1)


# --- a new runtime error entry ------------------------------------------------------------------


def test_a_new_runtime_error_entry_is_stated_once_with_the_monitors_own_numbers():
    detector = RuntimeException()
    event = error_event(12, count=2)
    (detection,) = found(detector, observation(1, monitor=monitor(event)))
    assert detection.basis is SuspicionBasis.runtime_exception
    assert detection.subject == "public.runtime_errors[Exception]"
    assert detection.statement == (
        'A new runtime error entry was recorded during play: type Exception, message "NullReferenceException: '
        'object reference not set" (first recorded at monitor sequence 12; 2 occurrences; the monitor has '
        'counted 2 error messages this run).')
    assert detection.evidence == (100,)
    assert detection.detail["first_sequence"] == 12 and detection.detail["count"] == 2
    assert detection.detail["event"]["last_sequence"] == 12


def test_the_same_entry_folded_again_is_not_a_new_fact_but_a_new_entry_is():
    detector = RuntimeException()
    first = monitor(error_event(12))
    later = monitor(error_event(12, count=3), error_event(13, "InvalidOperationException", count=1), observed=4)
    detections = found(detector, observation(1, monitor=first), observation(2, monitor=first),
                       observation(3, monitor=later))
    assert [detection.detail["first_sequence"] for detection in detections] == [12, 13]
    assert detections[1].subject == "public.runtime_errors[Exception]"
    assert "2 occurrences" not in detections[1].statement and "1 occurrence" in detections[1].statement


def test_an_entry_that_names_no_error_is_not_stated_and_an_unsequenced_entry_is_deduped_by_its_text():
    detector = RuntimeException()
    warning = monitor(error_event(5, "the frame took 40 ms", kind="Warning"))
    assert found(detector, observation(1, monitor=warning)) == []
    unsequenced = monitor({"type": "Error", "message": "boom"})
    assert len(found(detector, observation(1, monitor=unsequenced), observation(2, monitor=unsequenced))) == 1
    other = monitor({"type": "Error", "message": "bang"})
    assert len(found(RuntimeException(), observation(1, monitor=unsequenced), observation(2, monitor=other))) == 2


def test_a_long_message_is_quoted_to_a_head_and_the_full_text_rides_on_the_detail():
    detector = RuntimeException(head_chars=10)
    (detection,) = found(detector, observation(1, monitor=monitor(error_event(1, "abcdefghijklmno"))))
    assert '"abcdefghi…"' in detection.statement
    assert detection.detail["message_head"] == "abcdefghi…" and detection.detail["message"] == "abcdefghijklmno"


def test_no_monitor_in_the_observation_states_nothing_and_a_restart_forgets_the_sequences():
    detector = RuntimeException()
    assert found(detector, observation(1), observation(2, monitor={"events": "not a list"})) == []
    event = monitor(error_event(7))
    assert len(found(detector, observation(1, monitor=event))) == 1
    detector.rebind()
    assert len(found(detector, observation(2, monitor=event))) == 1
    with pytest.raises(ValueError):
        RuntimeException(head_chars=0)


# --- a declared reader above its declared maximum -----------------------------------------------


def test_a_reader_above_its_declared_maximum_is_stated_once_per_episode():
    detector = ReaderOverMax({"r": 10.0}, source="a test maximum")
    detections = found(detector, observation(1, r=3.0), observation(2, r=12.0), observation(3, r=13.0),
                       observation(4, r=5.0), observation(5, r=12.0))
    assert [detection.statement for detection in detections] == [
        "public.r is 12, above the declared maximum 10 (a test maximum)."] * 2
    first = detections[0]
    assert first.basis is SuspicionBasis.reader_over_max and first.subject == "public.r"
    assert first.detail == {"field": "public.r", "value": 12.0, "declared_max": 10.0,
                            "source": "a test maximum"}
    assert first.evidence == (101,)


def test_an_unmeasured_reader_value_neither_raises_nor_ends_the_episode():
    detector = ReaderOverMax({"r": 10.0}, source="a test maximum")
    detections = found(detector, observation(1, r=12.0), observation(2, r=None), observation(3, r=13.0),
                       observation(4, r=5.0), observation(5, r=12.0))
    assert len(detections) == 2
    fresh = ReaderOverMax({"r": 10.0}, source="a test maximum")
    assert len(found(fresh, observation(1, r=12.0), observation(2, r=math.nan), observation(3, r=13.0))) == 1


def test_with_no_declared_maximum_nothing_is_compared_and_the_plan_says_why():
    detector = ReaderOverMax({}, source="an operator declaration")
    assert found(detector, observation(1, r=1e9)) == []
    assert detectors_config([detector])["reader_over_max"]["silent"] == (
        "no reader maximum is declared, and no manifest reader declares one: the manifest needs a declared maximum")


def test_a_declared_maximum_must_be_a_finite_number():
    for bad in (math.nan, math.inf, None, True, "10"):
        with pytest.raises(ValueError):
            ReaderOverMax({"r": bad}, source="a test maximum")


# --- one watch: the readers' suspicions, then the detectors' ------------------------------------


def test_a_detector_suspicion_follows_the_readers_and_adds_no_line_of_its_own():
    inner = ReaderWatch({"r": "number"}, min_readings=3)
    watch = DetectorWatch(inner, [ReaderOverMax({"r": 10.0}, source="a test maximum")])
    notices = [watch.observe(observation(index + 1, r=value), None, turn=index, trace_seq=100 + index)
               for index, value in enumerate([1.0, 2.0, 1.0, 2.0, 50.0, 51.0])]
    assert [len(notice.lines) for notice in notices] == [1] * 6
    assert notices[3].suspicions == ()
    raised = notices[4].suspicions
    assert [(item.suspicion_id, item.basis) for item in raised] == [
        ("s1", SuspicionBasis.range_exit), ("s2", SuspicionBasis.reader_over_max)]
    assert raised[1].statement == "public.r is 50, above the declared maximum 10 (a test maximum)."
    assert (raised[1].turn, raised[1].based_on_seq, raised[1].game_s) == (4, 5, 1.0)
    assert raised[1].evidence_seq == [104]
    assert notices[5].suspicions == ()                       # still above the maximum: the same episode
    assert inner.raised == 2 and watch.config()["names"] == ["reader_over_max"]


def test_a_restart_drops_every_detectors_baseline_and_the_ids_run_on():
    inner = ReaderWatch({})
    watch = DetectorWatch(inner, [FellOut(2.0, peak_source="a test peak")])
    first = watch.observe(observation(1, position=[0.0, -2.8, 0.0], velocity=[0.0, 0.0, 0.0], floor=-2.8),
                          None, turn=0, trace_seq=1)
    assert first.suspicions == ()
    (below,) = watch.observe(falling(2, -9.0), None, turn=1, trace_seq=2).suspicions
    assert below.suspicion_id == "s1" and below.basis is SuspicionBasis.fell_out
    watch.rebind()
    assert watch.observe(falling(3, -9.0), None, turn=2, trace_seq=3).suspicions == ()   # no floor yet
    watch.observe(standing(4, -2.8), None, turn=3, trace_seq=4)
    (again,) = watch.observe(falling(5, -9.0), None, turn=4, trace_seq=5).suspicions
    assert again.suspicion_id == "s2" and again.turn == 4


def test_the_detectors_are_off_by_default_and_take_the_environment(monkeypatch):
    monkeypatch.delenv("UPA_RUN_DETECTORS", raising=False)
    assert cli._parser().parse_args(REQUIRED).detectors is False
    assert cli._parser().parse_args([*REQUIRED, "--detectors"]).detectors is True
    for raw, expected in (("1", True), ("true", True), ("0", False), ("false", False)):
        monkeypatch.setenv("UPA_RUN_DETECTORS", raw)
        assert cli._parser().parse_args(REQUIRED).detectors is expected


def test_a_declared_maximum_must_name_a_reader_the_manifest_declares(monkeypatch):
    manifest = build_manifest()
    parser = cli._parser()
    chosen = parser.parse_args([*REQUIRED, "--detectors", "--detector-max", "speed=10", "--detector-max", "speed=12"])
    assert cli._detector_maxima(parser, chosen, manifest) == {"speed": 12.0}
    for arguments in (["--detectors", "--detector-max", "other=1"],      # not a declared reader
                      ["--detector-max", "speed=1"],                     # a bound without the detectors
                      ["--detectors", "--detector-max", "speed=lots"],   # not a number
                      ["--detectors", "--detector-max", "speed=inf"],    # not finite
                      ["--detectors", "--detector-max", "speed"]):       # no value
        with pytest.raises(SystemExit):
            cli._detector_maxima(parser, parser.parse_args([*REQUIRED, *arguments]), manifest)
    monkeypatch.setenv("UPA_RUN_DETECTOR_MAX", "speed=10, speed=12")
    environment = cli._parser().parse_args([*REQUIRED, "--detectors"])
    assert cli._detector_maxima(cli._parser(), environment, manifest) == {"speed": 12.0}
