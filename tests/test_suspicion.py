"""The watch: per-reader lines, and the suspicions it raises from invariants, ranges and effects.

A suspicion is a fact: these tests pin what it states, when it is raised (when a state begins, not on every
turn it lasts), what it cites, and that it is never silent about unknowns, never advice and never zero.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import SuspicionBasis
from playtest.contracts.loop import Choice
from playtest.contracts.manifest import ManifestInvariant
from playtest.core.ports import EffectComparison, EffectReport, ReaderSample
from playtest.core.suspicion import RANGE_MIN_READINGS, ReaderWatch, fmt, suspicion_line

START = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
ADVICE = ("should", "recommend", "consider", "advise", "escalate", "help", "try ", "ask ")


def observation(seq: int, **public) -> Observation:
    return Observation(session="s", seq=seq, game_s=seq * 0.5, wall_utc=START, clock_mode="realtime", scene="main",
                       playing=True, paused=False, time_scale=1.0, frame=seq, public=public, ui=None, frame_ref=None,
                       reader_errors=[])


def rule(kind: str, **fields) -> ManifestInvariant:
    base = dict(kind=kind, name="rule", source="test", catches="a test case", field=None, direction=None, min=None,
                max=None, when=None, then=None, unless=None)
    return ManifestInvariant.model_validate({**base, **fields})


def feed(watch: ReaderWatch, values: list, alias: str = "r", *, start_turn: int = 0):
    """Observe one reader's values in order; returns each turn's `Noticed`."""
    return [watch.observe(observation(start_turn + i + 1, **{alias: value}), None, turn=start_turn + i,
                          trace_seq=100 + start_turn + i) for i, value in enumerate(values)]


def raised(notices) -> list:
    return [s for n in notices for s in n.suspicions]


# --- the lines S1 reads ------------------------------------------------------------------------


def test_each_reader_states_its_current_previous_and_earlier_range_without_the_current_reading():
    watch = ReaderWatch({"r": "number"}, min_readings=3)
    first, second, third = feed(watch, [4.0, 6, 5.5])
    assert first.lines == ("public.r: now 4; previous unknown; earlier readings: none.",)
    assert second.lines == ("public.r: now 6; previous 4; earlier range 4 to 4 (1 earlier reading).",)
    assert third.lines == ("public.r: now 5.5; previous 6; earlier range 4 to 6 (2 earlier readings).",)


def test_lines_follow_the_manifest_order_and_state_unknown_values_as_unknown_not_zero():
    watch = ReaderWatch({"b": "number", "a": "bool"}, min_readings=3)
    watch.observe(observation(1, a=True, b=3), None, turn=0, trace_seq=1)
    notice = watch.observe(observation(2, a=None), None, turn=1, trace_seq=2)       # b is missing, a is unread
    assert notice.lines == ("public.b: now unknown; previous 3; earlier range 3 to 3 (1 earlier reading).",
                            "public.a: now unknown; previous true; earlier values: true (1 earlier reading).")
    assert notice.suspicions == ()


def test_kinds_have_their_own_range_wording():
    watch = ReaderWatch({"flag": "bool", "name": "string", "vec": "vector", "blob": "other"}, min_readings=3)
    watch.observe(observation(1, flag=False, name="a", vec=[0, 1.5], blob={"k": 1}), None, turn=0, trace_seq=1)
    watch.observe(observation(2, flag=True, name="b", vec=[2, 0.5], blob={"k": 2}), None, turn=1, trace_seq=2)
    notice = watch.observe(observation(3, flag=True, name="b", vec=[1, 1], blob={"k": 2}), None, turn=2, trace_seq=3)
    assert notice.lines == (
        "public.flag: now true; previous true; earlier values: false, true (2 earlier readings).",
        'public.name: now "b"; previous "b"; earlier values: "a", "b" (2 earlier readings).',
        "public.vec: now [1, 1]; previous [2, 0.5]; earlier range per component: [0 to 2, 0.5 to 1.5] (2 earlier readings).",
        'public.blob: now {"k": 2}; previous {"k": 2}; no range is stated for this kind of value (2 earlier readings).')


def test_a_header_states_when_the_readers_were_read_against_the_observation():
    watch = ReaderWatch({"r": "number"})
    sample = ReaderSample(values={"r": 1.0}, errors={}, inspection_seq=88, game_s=12.5)
    notice = watch.observe(observation(30, r=1.0), sample, turn=0, trace_seq=1)
    assert notice.lines[0] == "The readers were read at game time 12.50 s (inspection seq 88); this observation is at 15.00 s."
    failed = ReaderSample(values={"r": None}, errors={"r": "x"}, problem="the inspection failed")
    assert watch.observe(observation(31, r=None), failed, turn=1, trace_seq=2).lines[0] == (
        "The readers could not be read this turn (the inspection failed); their values are unknown.")
    unknown_time = ReaderSample(values={"r": 1.0}, errors={}, inspection_seq=None, game_s=None)
    assert "game time unknown (inspection seq unknown)" in watch.observe(
        observation(32, r=1.0), unknown_time, turn=2, trace_seq=3).lines[0]


def test_numbers_are_stated_to_six_significant_digits_and_zero_has_no_sign():
    assert fmt(-0.0) == "0" and fmt(1 / 3) == "0.333333" and fmt(2.0) == "2" and fmt(5) == "5" and fmt(None) == "unknown"
    assert fmt(True) == "true" and fmt([1.5, None]) == "[1.5, unknown]" and fmt("a") == '"a"'
    assert fmt(-115.23456789) == "-115.235"


# --- leaving the range ---------------------------------------------------------------------------


def test_a_reader_that_leaves_its_range_raises_one_suspicion_that_states_the_numbers():
    watch = ReaderWatch({"r": "number"}, min_readings=3)
    notices = feed(watch, [1, 2, 1, 2, 1, 5])
    (s,) = raised(notices)
    assert raised(notices[:-1]) == [] and notices[-1].suspicions == (s,)
    assert s.basis is SuspicionBasis.range_exit and s.suspicion_id == "s1" and s.subject == "public.r"
    assert s.statement == "public.r is 5: outside the range 1 to 2 that its 5 earlier readings showed."
    assert (s.turn, s.based_on_seq, s.game_s) == (5, 6, 3.0)
    assert s.detail["field"] == "public.r" and s.detail["value"] == 5 and s.detail["previous"] == 1
    assert (s.detail["min"], s.detail["max"], s.detail["readings"], s.detail["required_readings"]) == (1, 2, 5, 3)


def test_a_range_exit_cites_the_current_observation_and_the_ones_that_set_the_range():
    watch = ReaderWatch({"r": "number"}, min_readings=3)
    notices = feed(watch, [3, 1, 2, 2, 9])           # trace_seq 100.. ; the minimum was read at 101, the maximum at 100
    (s,) = raised(notices)
    assert s.evidence_seq == [100, 101, 104]
    assert s.detail["min_seq"] == 101 and s.detail["max_seq"] == 100


def test_a_departure_is_raised_when_it_begins_and_not_again_while_it_lasts():
    watch = ReaderWatch({"r": "number"}, min_readings=3)
    notices = feed(watch, [1, 2, 1, 2, 1, 5, 6, 7, 8])      # 5 leaves the range; 6, 7 and 8 are new highs in the same departure
    assert [n.suspicions != () for n in notices] == [False] * 5 + [True] + [False] * 3
    assert "public.r: now 8; previous 7; earlier range 1 to 7 (8 earlier readings)." == notices[-1].lines[-1]


def test_a_reader_that_came_back_inside_and_leaves_again_raises_again():
    watch = ReaderWatch({"r": "number"}, min_readings=3)
    notices = feed(watch, [1, 2, 1, 2, 1, 5, 3, 3, 9])       # 3 is inside the extended range 1 to 5; 9 leaves it
    assert [i for i, n in enumerate(notices) if n.suspicions] == [5, 8]
    assert [s.suspicion_id for s in raised(notices)] == ["s1", "s2"]


def test_an_unread_value_neither_raises_nor_ends_a_departure():
    watch = ReaderWatch({"r": "number"}, min_readings=3)
    notices = feed(watch, [1, 2, 1, 2, 1, 5, None, 6])        # the departure continues through the unknown reading
    assert [i for i, n in enumerate(notices) if n.suspicions] == [5]
    assert notices[6].lines[-1] == "public.r: now unknown; previous 5; earlier range 1 to 5 (6 earlier readings)."
    assert notices[7].lines[-1].startswith("public.r: now 6; previous unknown;")
    before_unknown = feed(ReaderWatch({"r": "number"}, min_readings=3), [1, 2, 1, 2, None, None, None, 9])
    assert [i for i, n in enumerate(before_unknown) if n.suspicions] == [7]


def test_no_range_is_stated_before_the_session_has_shown_one():
    assert RANGE_MIN_READINGS == 10
    watch = ReaderWatch({"r": "number"})
    assert raised(feed(watch, [1, 2, 3, 4, 5, 6, 7, 8, 9, 50])) == []           # nine earlier readings: not yet a range
    watch = ReaderWatch({"r": "number"})
    (s,) = raised(feed(watch, [1, 2, 3, 4, 5, 6, 7, 8, 9, 9, 50]))
    assert s.statement == "public.r is 50: outside the range 1 to 9 that its 10 earlier readings showed."
    with pytest.raises(ValueError):
        ReaderWatch({"r": "number"}, min_readings=0)
    with pytest.raises(ValueError):
        ReaderWatch({"r": "number"}, min_readings=True)


def test_a_new_value_of_a_flag_or_a_string_is_a_departure():
    watch = ReaderWatch({"flag": "bool"}, min_readings=3)
    (s,) = raised(feed(watch, [False, False, False, True], "flag"))
    assert s.statement == "public.flag is true; none of its 3 earlier readings had this value (earlier values: false)."
    assert s.detail["values"] == [False]
    watch = ReaderWatch({"name": "string"}, min_readings=3)
    (s,) = raised(feed(watch, ["a", "b", "a", "c"], "name"))
    assert s.statement == 'public.name is "c"; none of its 3 earlier readings had this value (earlier values: "a", "b").'


def test_a_vector_leaves_its_range_when_any_component_does():
    watch = ReaderWatch({"v": "vector"}, min_readings=3)
    notices = feed(watch, [[0, 1, 2], [1, 1, 3], [0, 2, 2], [0.5, 1.5, 2.5], [0.5, 1.5, 9], [0.5, 1.5, 2.5]], "v")
    (s,) = raised(notices)
    assert notices[4].suspicions == (s,)
    assert s.statement == ("public.v is [0.5, 1.5, 9]: component 2 is 9, outside 2 to 3, "
                           "the range its 4 earlier readings showed.")
    assert s.detail["components"] == [2] and s.detail["min"] == [0.0, 1.0, 2.0] and s.detail["max"] == [1.0, 2.0, 3.0]


def test_a_different_type_of_value_is_stated_as_such():
    watch = ReaderWatch({"r": None}, min_readings=3)
    notices = feed(watch, [1, 2, 3, "high"])
    (s,) = raised(notices)
    assert s.statement == 'public.r is "high"; it is a string value and its 3 earlier readings were number values.'
    after = feed(watch, ["low", "mid"], start_turn=4)
    assert raised(after) == []                       # the range started again with the string


def test_a_set_that_keeps_growing_is_not_a_range():
    watch = ReaderWatch({"name": "string"}, min_readings=3)
    feed(watch, ["a", "a", "a"], "name")
    notices = feed(watch, [f"v{i}" for i in range(25)], "name", start_turn=3)
    assert [i for i, n in enumerate(notices) if n.suspicions] == [0]       # v0 begins a departure; the rest continue it
    last = feed(watch, ["a", "zzz"], "name", start_turn=28)
    assert raised(last) == []                                              # a value never seen before is no longer a departure
    assert "more than 20 different ones" in last[-1].lines[-1]


# --- invariants --------------------------------------------------------------------------------


def test_a_violated_invariant_raises_once_then_is_restated_as_still_and_ends_when_it_holds():
    watch = ReaderWatch({"count": "number"}, [rule("range", name="count_sane", field="count", min=0.0, max=5.0)])
    n = feed(watch, [3, 7, 8, 9, 2, 6], "count")
    assert [bool(x.suspicions) for x in n] == [False, True, False, False, False, True]
    first = n[1].suspicions[0]
    assert first.basis is SuspicionBasis.invariant and first.subject == "count_sane" and first.suspicion_id == "s1"
    assert first.statement == "Invariant count_sane does not hold: count is 7, above the maximum 5 (count is 7)."
    assert first.detail["invariant"] == "count_sane" and first.detail["readings"] == {"count": 7}
    assert first.evidence_seq == [100, 101]                      # the observation before and the one it was found in
    assert n[2].lines[0] == ("Invariant count_sane still does not hold (since turn 1, suspicion s1): "
                             "count is 8, above the maximum 5 (count is 8).")
    assert n[3].lines[0].startswith("Invariant count_sane still does not hold (since turn 1")
    assert not any(line.startswith("Invariant") for line in n[4].lines)
    assert n[5].suspicions[0].suspicion_id == "s2"


def test_an_invariant_nobody_could_score_is_silent_and_does_not_end_a_violation():
    watch = ReaderWatch({"count": "number"}, [rule("range", name="count_sane", field="count", min=0.0, max=5.0)])
    n = feed(watch, [9, None, 9], "count")
    assert [bool(x.suspicions) for x in n] == [True, False, False]
    assert not any(line.startswith("Invariant") for line in n[1].lines)             # unknown: nothing is stated
    assert n[2].lines[0].startswith("Invariant count_sane still does not hold")     # and the violation was not forgotten


def test_an_implies_invariant_uses_the_previous_observation_as_its_baseline():
    paused_freezes_move = rule("implies", name="paused_freezes_move",
                               when={"field": "paused", "op": "==", "value": True},
                               then={"field": "moved", "op": "unchanged", "value": None})
    watch = ReaderWatch({"paused": "bool", "moved": "vector"}, [paused_freezes_move], min_readings=50)

    def step(seq, paused, moved):
        return watch.observe(observation(seq, paused=paused, moved=moved), None, turn=seq, trace_seq=seq)

    assert step(1, False, [0, 0, 1]).suspicions == ()
    assert step(2, True, [0, 0, 1]).suspicions == ()                # paused, and it did not change: holds
    (s,) = step(3, True, [0, 0, 2]).suspicions                      # paused, and it changed: violated
    assert s.statement.startswith("Invariant paused_freezes_move does not hold: paused == True held, and moved unchanged did not")
    assert s.detail["baselines"] == {"moved": [0, 0, 1]} and s.detail["readings"]["moved"] == [0, 0, 2]


# --- effects ------------------------------------------------------------------------------------


def comparison(observed: float, *, expected=2.0, allowed=0.0572, spread=0.0072) -> EffectComparison:
    return EffectComparison(metric="net displacement", unit="world units", expected=expected, observed=observed,
                            spread=spread, allowed=allowed, basis="the calibration's spread 0.0072 plus a measurement allowance of 0.05")


def test_an_effect_beyond_the_recorded_spread_raises_a_suspicion_that_states_both_numbers():
    watch = ReaderWatch({})
    watch.observe(observation(7), None, turn=4, trace_seq=30)
    move = Choice(id="move_forward", kind="action", label="Move", action="move_forward", source="manifest")
    (s,) = watch.effect(move, EffectReport(comparison=comparison(0.0)), turn=4, trace_seqs=[31, 32])
    assert s.basis is SuspicionBasis.effect_mismatch and s.subject == "move_forward" and s.suspicion_id == "s1"
    assert s.statement == ("move_forward: measured net displacement 0 world units against a calibrated 2 world units; "
                           "the difference -2 world units is more than the 0.0572 world units the calibration's recorded spread "
                           "allows (the calibration's spread 0.0072 plus a measurement allowance of 0.05).")
    assert (s.turn, s.based_on_seq, s.evidence_seq) == (4, 7, [31, 32])
    assert s.detail["difference"] == -2.0 and s.detail["allowed"] == 0.0572 and s.detail["expected"] == 2.0
    assert watch.raised == 1


def test_an_effect_inside_the_allowance_or_not_measured_or_without_a_recorded_spread_raises_nothing():
    watch = ReaderWatch({})
    move = Choice(id="move_forward", kind="action", label="Move", action="move_forward", source="manifest")
    assert watch.effect(move, EffectReport(comparison=comparison(2.05)), turn=0, trace_seqs=[]) == []
    assert watch.effect(move, EffectReport(comparison=comparison(2.0572)), turn=0, trace_seqs=[]) == []   # at the allowance
    assert watch.effect(move, EffectReport(comparison=None), turn=0, trace_seqs=[]) == []                  # unknown is not a mismatch
    assert watch.effect(move, EffectReport(), turn=0, trace_seqs=[]) == []


def test_an_effect_with_a_number_nobody_could_measure_raises_nothing():
    watch = ReaderWatch({})
    move = Choice(id="move_forward", kind="action", label="Move", action="move_forward", source="manifest")
    for bad in (float("nan"), float("inf"), None):
        report = EffectReport(comparison=EffectComparison(
            metric="net displacement", unit=None, expected=2.0, observed=bad, spread=0.01, allowed=0.06,  # type: ignore[arg-type]
            basis="a test allowance"))
        assert watch.effect(move, report, turn=0, trace_seqs=[]) == []
    assert watch.raised == 0


def test_ids_run_on_across_every_kind_and_the_count_matches():
    watch = ReaderWatch({"count": "number"}, [rule("range", name="sane", field="count", min=0.0, max=5.0)])
    move = Choice(id="m", kind="action", label="Move", action="m", source="manifest")
    first = watch.observe(observation(1, count=9), None, turn=0, trace_seq=1)
    second = watch.effect(move, EffectReport(comparison=comparison(0.0)), turn=0, trace_seqs=[2])
    assert [s.suspicion_id for s in (*first.suspicions, *second)] == ["s1", "s2"] and watch.raised == 2


# --- what the lines and statements never are ----------------------------------------------------


def test_nothing_the_watch_states_is_advice():
    watch = ReaderWatch({"r": "number", "flag": "bool"}, [rule("range", name="sane", field="r", min=0.0, max=5.0)],
                        min_readings=3)
    move = Choice(id="m", kind="action", label="Move", action="m", source="manifest")
    text = []
    for i, value in enumerate([1, 2, 1, 2, 1, 9, 10, 2]):
        notice = watch.observe(observation(i + 1, r=value, flag=bool(i % 3 == 0)), ReaderSample(
            values={"r": value, "flag": True}, errors={}, inspection_seq=i, game_s=float(i)), turn=i, trace_seq=i)
        text += [*notice.lines, *(suspicion_line(s, i) for s in notice.suspicions)]
    text += [s.statement for s in watch.effect(move, EffectReport(comparison=comparison(0.0)), turn=0, trace_seqs=[])]
    assert len(text) > 20
    lowered = [line.lower() for line in text]
    assert not [line for line in lowered if any(word in line for word in ADVICE)]


def test_a_suspicion_line_names_its_id_basis_and_trace_entry():
    watch = ReaderWatch({"r": "number"}, min_readings=3)
    (s,) = raised(feed(watch, [1, 2, 1, 2, 1, 5]))
    assert suspicion_line(s, 412) == (
        "Suspicion s1 (range_exit, trace_seq 412): public.r is 5: outside the range 1 to 2 that its 5 earlier readings showed.")
    assert suspicion_line(s, None).startswith("Suspicion s1 (range_exit): ")


# --- drift ---------------------------------------------------------------------------------------


def drifts(notices) -> list:
    return [s for s in raised(notices) if s.basis.value == "drift"]


def test_a_reading_that_keeps_setting_new_lows_raises_a_drift_once_and_is_restated_while_it_lasts():
    watch = ReaderWatch({"r": "number"}, min_readings=3, drift_readings=4)
    notices = feed(watch, [5.0, -1, -2, -3, -4, -5, -6])
    (drift,) = drifts(notices)
    assert drift.turn == 4 and drift.subject == "public.r" and drift.evidence_seq == [101, 104]
    assert drift.statement == "public.r has set a new low at each of its last 4 known readings, since turn 1: from 5 to -4."
    assert drift.detail == {"field": "public.r", "direction": "low", "readings": 4, "required_readings": 4,
                            "from": 5.0, "value": -4, "since_turn": 1, "since_seq": 101}
    assert notices[5].lines[-1] == (f"public.r still sets a new low at every known reading (since turn 1, suspicion "
                                    f"{drift.suspicion_id}): 5 in a row, from 5 to -5.")
    assert "still sets" in notices[6].lines[-1] and "6 in a row" in notices[6].lines[-1]
    assert [s.basis.value for s in raised(notices)] == ["range_exit", "drift"]   # the range exit is raised once


def test_an_unknown_reading_neither_extends_nor_ends_a_run_and_a_new_run_can_raise_again():
    watch = ReaderWatch({"r": "number"}, min_readings=3, drift_readings=3)
    first = feed(watch, [5.0, 1, None, 0, -1])                       # 1, 0, -1 are three new lows; None is skipped
    assert [s.detail["readings"] for s in drifts(first)] == [3]
    second = feed(watch, [-1, 9, 10, 11], start_turn=5)              # -1 sets nothing: the low run ends
    assert [(s.detail["direction"], s.detail["readings"]) for s in drifts(second)] == [("high", 3)]
    assert not any("still sets" in line for line in second[0].lines)


def test_a_short_countdown_is_no_drift_at_the_default_threshold_but_a_long_fall_is():
    watch = ReaderWatch({"r": "number"})
    assert watch.drift_readings == 20
    assert drifts(feed(watch, [5.0, 4, 3, 2, 1, 0, 0, 0])) == []
    long = drifts(feed(ReaderWatch({"r": "number"}), [float(-i) for i in range(30)]))
    assert [s.detail["readings"] for s in long] == [20]


def test_the_drift_threshold_must_be_a_positive_integer():
    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError, match="drift_readings"):
            ReaderWatch({"r": "number"}, drift_readings=bad)


def test_measured_surroundings_ride_on_the_shortfall_suspicion_verbatim():
    watch = ReaderWatch({})
    move = Choice(id="move_forward", kind="action", label="Move", action="move_forward", source="manifest")
    base = comparison(0.0)
    report = EffectReport(comparison=EffectComparison(
        metric=base.metric, unit=base.unit, expected=base.expected, observed=base.observed, spread=base.spread,
        allowed=base.allowed, basis=base.basis, context="low cast hit at 0.38; high cast clear"))
    (s,) = watch.effect(move, report, turn=7, trace_seqs=[40])
    assert s.statement.endswith("At the observation the action was bound to: low cast hit at 0.38; high cast clear.")
    assert s.detail["context"] == "low cast hit at 0.38; high cast clear"
    (plain,) = watch.effect(move, EffectReport(comparison=base), turn=8, trace_seqs=[41])
    assert "bound to" not in plain.statement and plain.detail["context"] is None
