from __future__ import annotations

import math

import pytest

from playtest.preflight.freshness import SAMPLING_LIMITATION, analyze_freshness

MISSING = object()
_UNSET = object()


def row(
    value,
    *,
    field_id: str = "f",
    state: str = "present",
    kind: str | None = None,
    is_null=_UNSET,
    truncated=_UNSET,
) -> dict:
    """Build one field row; ``MISSING`` in a series means the id was absent."""
    if kind is None:
        if value is None:
            kind = "null"
        elif isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, int):
            kind = "integer"
        elif isinstance(value, float):
            kind = "number"
        elif isinstance(value, str):
            kind = "string"
        else:
            kind = "unsupported"

    if state == "present" and value is None:
        state = "null"
    if is_null is _UNSET:
        if state == "null":
            is_null = True
        elif state == "present":
            is_null = False
        else:
            is_null = None
    if truncated is _UNSET:
        truncated = False if kind == "string" and state == "present" else None

    return {
        "id": field_id,
        "value_state": state,
        "value_kind": kind,
        "value": value,
        "is_null": is_null,
        "value_truncated": truncated,
        "read_error": None,
    }


def series(items, *, field_id: str = "f", start: float = 0.0, step: float = 0.1) -> list[dict]:
    samples = []
    for index, item in enumerate(items):
        if item is MISSING:
            fields = []
        elif isinstance(item, list):
            fields = item
        elif isinstance(item, dict):
            fields = [item]
        else:
            fields = [row(item, field_id=field_id)]
        samples.append({"real_time_s": start + index * step, "fields": fields})
    return samples


def test_continuous_change_at_idle_is_observed_in_idle_only():
    result = analyze_freshness(series([0, 1, 2, 3, 4]), [])

    evidence = result["f"]
    idle = evidence["idle"]
    assert idle["sample_count"] == 5
    assert idle["field_present_samples"] == 5
    assert idle["comparable_samples"] == 5
    assert idle["comparable_intervals"] == 4
    assert idle["changed_intervals"] == 4
    assert idle["unchanged_intervals"] == 0
    assert idle["unknown_intervals"] == 0
    assert idle["consecutive_changed_intervals_max"] == 4
    assert idle["complete"] is True
    assert idle["classification"] == "observed_continuous"
    assert evidence["input"]["classification"] == "unknown"
    assert evidence["classification"] == "idle"


def test_change_during_input_is_observed_in_input_only():
    idle = series([3, 3, 3, 3])
    segments = [series([0, 1, 2], start=10.0)]

    evidence = analyze_freshness(idle, segments)["f"]
    assert evidence["idle"]["classification"] == "no_change_observed"
    assert evidence["input"]["classification"] == "observed_continuous"
    assert evidence["input"]["changed_intervals"] == 2
    assert evidence["input"]["consecutive_changed_intervals_max"] == 2
    assert evidence["input"]["complete"] is True
    assert evidence["classification"] == "input"


def test_both_windows_can_show_change():
    idle = series([0, 1, 2])
    segments = [series([5, 6, 7], start=20.0)]

    evidence = analyze_freshness(idle, segments)["f"]
    assert evidence["idle"]["classification"] == "observed_continuous"
    assert evidence["input"]["classification"] == "observed_continuous"
    assert evidence["classification"] == "both"


def test_one_transition_is_observed_change_not_continuous():
    evidence = analyze_freshness(series([0, 1, 1]), [])["f"]
    idle = evidence["idle"]

    assert idle["comparable_intervals"] == 2
    assert idle["changed_intervals"] == 1
    assert idle["consecutive_changed_intervals_max"] == 1
    assert idle["classification"] == "observed_change"
    assert evidence["classification"] == "idle"
    assert evidence["continuous_classification"] == "change_observed_without_continuous_evidence"


def test_isolated_events_are_not_continuous():
    evidence = analyze_freshness(series([0, 0, 1, 1, 2, 2]), [])["f"]
    idle = evidence["idle"]

    assert idle["comparable_intervals"] == 5
    assert idle["changed_intervals"] == 2
    assert idle["consecutive_changed_intervals_max"] == 1
    assert idle["classification"] == "observed_change"


def test_constant_field_needs_two_comparable_intervals():
    three = analyze_freshness(series([4, 4, 4]), [])["f"]
    assert three["idle"]["comparable_intervals"] == 2
    assert three["idle"]["changed_intervals"] == 0
    assert three["idle"]["consecutive_changed_intervals_max"] == 0
    assert three["idle"]["classification"] == "no_change_observed"

    two = analyze_freshness(series([4, 4]), [])["f"]
    assert two["idle"]["comparable_intervals"] == 1
    assert two["idle"]["classification"] == "unknown"

    both = analyze_freshness(series([4, 4, 4]), [series([4, 4, 4], start=10.0)])["f"]
    assert both["classification"] == "no_change_observed"


def test_unsupported_state_is_unknown_not_unchanged_or_zero():
    idle = series([row(1), row(None, state="unsupported", kind="unsupported"), row(1), row(1)])

    evidence = analyze_freshness(idle, [])["f"]["idle"]
    assert evidence["sample_count"] == 4
    assert evidence["field_present_samples"] == 4
    assert evidence["comparable_samples"] == 3
    assert evidence["comparable_intervals"] == 1
    assert evidence["changed_intervals"] == 0
    assert evidence["unknown_intervals"] == 2
    assert evidence["classification"] == "unknown"


def test_missing_field_is_unknown_and_not_treated_as_null():
    idle = series([row(1), MISSING, row(1)])

    evidence = analyze_freshness(idle, [])["f"]["idle"]
    assert evidence["sample_count"] == 3
    assert evidence["field_present_samples"] == 2
    assert evidence["comparable_samples"] == 2
    assert evidence["comparable_intervals"] == 0
    assert evidence["changed_intervals"] == 0
    assert evidence["unknown_intervals"] == 2
    assert evidence["classification"] == "unknown"


def test_a_field_missing_from_idle_window_can_still_have_input_evidence():
    result = analyze_freshness(series([MISSING, MISSING]), [series([0, 1])])

    evidence = result["f"]
    assert evidence["idle"]["sample_count"] == 2
    assert evidence["idle"]["field_present_samples"] == 0
    assert evidence["idle"]["classification"] == "unknown"
    assert evidence["input"]["classification"] == "observed_change"
    assert evidence["classification"] == "input"


def test_explicit_null_is_a_measured_value():
    constant = analyze_freshness(series([row(None), row(None), row(None)]), [])["f"]
    assert constant["idle"]["comparable_intervals"] == 2
    assert constant["idle"]["changed_intervals"] == 0
    assert constant["idle"]["classification"] == "no_change_observed"

    transition = analyze_freshness(series([row(None), row(0)]), [])["f"]
    assert transition["idle"]["changed_intervals"] == 1
    assert transition["idle"]["classification"] == "observed_change"

    unmeasured = analyze_freshness(series([row(None, is_null=False), row(0)]), [])["f"]
    assert unmeasured["idle"]["comparable_intervals"] == 0
    assert unmeasured["idle"]["changed_intervals"] == 0
    assert unmeasured["idle"]["classification"] == "unknown"


def test_truncated_or_unmeasured_string_is_unknown():
    truncated = analyze_freshness(
        series(
            [
                row("abc", truncated=False),
                row("abc", truncated=True),
                row("abc", truncated=False),
            ]
        ),
        [],
    )["f"]["idle"]
    assert truncated["comparable_intervals"] == 0
    assert truncated["changed_intervals"] == 0
    assert truncated["unknown_intervals"] == 2
    assert truncated["classification"] == "unknown"

    unmeasured = analyze_freshness(series([row("a", truncated=None), row("b", truncated=None)]), [])["f"]
    assert unmeasured["idle"]["comparable_intervals"] == 0
    assert unmeasured["idle"]["classification"] == "unknown"

    whole = analyze_freshness(series(["a", "b"]), [])["f"]
    assert whole["idle"]["changed_intervals"] == 1
    assert whole["idle"]["classification"] == "observed_change"


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_nonfinite_number_is_unknown(bad):
    evidence = analyze_freshness(series([1.0, bad, 1.0]), [])["f"]["idle"]
    assert evidence["comparable_intervals"] == 0
    assert evidence["changed_intervals"] == 0
    assert evidence["unknown_intervals"] == 2
    assert evidence["classification"] == "unknown"


@pytest.mark.parametrize("state", ["unsupported", "error", "destroyed"])
def test_non_present_states_are_unknown(state):
    bad = row(None, state=state, kind=state, is_null=True)
    evidence = analyze_freshness(series([row(1), bad, row(1)]), [])["f"]["idle"]
    assert evidence["comparable_intervals"] == 0
    assert evidence["unknown_intervals"] == 2
    assert evidence["classification"] == "unknown"


def test_read_error_is_unknown_even_with_a_present_state():
    bad = row(1)
    bad["read_error"] = "partial read failed"
    evidence = analyze_freshness(series([row(1), bad, row(1)]), [])["f"]["idle"]
    assert evidence["comparable_intervals"] == 0
    assert evidence["changed_intervals"] == 0
    assert evidence["unknown_intervals"] == 2
    assert evidence["classification"] == "unknown"


@pytest.mark.parametrize("kind", ["object_reference", "input_action", "special_number", "null"])
def test_non_comparable_kinds_are_unknown(kind):
    bad = row("x", kind=kind)
    evidence = analyze_freshness(series([row(1), bad, row(1)]), [])["f"]["idle"]
    assert evidence["comparable_intervals"] == 0
    assert evidence["changed_intervals"] == 0
    assert evidence["unknown_intervals"] == 2
    assert evidence["classification"] == "unknown"


def test_boolean_false_is_not_integer_zero():
    evidence = analyze_freshness(series([row(False), row(0)]), [])["f"]["idle"]
    assert evidence["comparable_intervals"] == 1
    assert evidence["changed_intervals"] == 1
    assert evidence["classification"] == "observed_change"


def test_duplicate_field_id_in_one_sample_raises():
    sample = {"real_time_s": 0.0, "fields": [row(0), row(1)]}
    with pytest.raises(ValueError, match="repeats field id"):
        analyze_freshness([sample], [])


@pytest.mark.parametrize("second_time", [0.0, -0.1, math.inf, math.nan])
def test_idle_timestamps_must_strictly_increase_and_be_finite(second_time):
    idle = [
        {"real_time_s": 0.0, "fields": [row(0)]},
        {"real_time_s": second_time, "fields": [row(0)]},
    ]
    with pytest.raises(ValueError, match="strictly increase|non-finite"):
        analyze_freshness(idle, [])


def test_input_timestamps_are_checked_within_each_segment():
    bad_segment = [
        {"real_time_s": 1.0, "fields": [row(0)]},
        {"real_time_s": 1.0, "fields": [row(0)]},
    ]
    with pytest.raises(ValueError, match="strictly increase"):
        analyze_freshness([], [bad_segment])

    # Separate windows are independent: their times do not have to follow on
    # from one another, and overlapping time ranges are not joined.
    segments = [series([0, 1], start=5.0), series([2, 3], start=0.0)]
    evidence = analyze_freshness(series([7, 7, 7], start=0.0), segments)["f"]
    assert evidence["input"]["segment_count"] == 2


def test_segments_are_never_joined_into_a_consecutive_run():
    # Joined, 0 -> 1 -> 2 -> 3 would be three consecutive changes. As distinct
    # windows, each segment contains one changed interval and the maximum is 1.
    segments = [series([0, 1], start=0.0), series([2, 3], start=0.0)]
    evidence = analyze_freshness(series([5, 5, 5]), segments)["f"]

    assert evidence["input"]["segment_count"] == 2
    assert evidence["input"]["changed_intervals"] == 2
    assert evidence["input"]["consecutive_changed_intervals_max"] == 1
    assert evidence["input"]["classification"] == "observed_change"
    assert [item["consecutive_changed_intervals_max"] for item in evidence["input"]["segments"]] == [1, 1]


def test_segment_edge_is_not_an_interval():
    segments = [series([0, 0, 0], start=0.0), series([1, 1, 1], start=0.0)]
    evidence = analyze_freshness(series([4, 4, 4]), segments)["f"]

    assert evidence["input"]["comparable_intervals"] == 4
    assert evidence["input"]["changed_intervals"] == 0
    assert evidence["input"]["classification"] == "no_change_observed"
    assert evidence["input"]["complete"] is True
    assert evidence["classification"] == "no_change_observed"


def test_incomplete_input_segments_are_preserved_as_coverage_metadata():
    segments = [
        series([0, row(None, state="unsupported", kind="unsupported"), 1]),
        series([5]),
        [],
    ]
    evidence = analyze_freshness(series([1, 1, 1]), segments)["f"]

    assert evidence["input"]["segment_count"] == 3
    assert evidence["input"]["sample_count"] == 4
    assert evidence["input"]["field_present_samples"] == 4
    assert evidence["input"]["comparable_samples"] == 3
    assert evidence["input"]["comparable_intervals"] == 0
    assert evidence["input"]["unknown_intervals"] == 2
    assert evidence["input"]["incomplete_segments"] == [0, 1, 2]
    assert evidence["input"]["complete"] is False
    assert [item["index"] for item in evidence["input"]["segments"]] == [0, 1, 2]
    assert evidence["input"]["segments"][0]["unknown_intervals"] == 2
    assert evidence["input"]["segments"][1]["sample_count"] == 1
    assert evidence["input"]["segments"][1]["classification"] == "unknown"
    assert evidence["input"]["segments"][2]["sample_count"] == 0
    assert evidence["input"]["classification"] == "unknown"
    assert evidence["classification"] == "unknown"


def test_comparison_is_exact_and_sampling_limitation_is_explicit():
    evidence = analyze_freshness(series([1.0, 1.0 + 1e-12]), [])["f"]

    assert evidence["idle"]["changed_intervals"] == 1
    assert evidence["idle"]["classification"] == "observed_change"
    assert evidence["sampling_limitation"] == SAMPLING_LIMITATION
    assert "variance" not in evidence
    assert "timestamps" not in evidence


def test_empty_input_has_no_fabricated_evidence():
    result = analyze_freshness([], [])
    assert result == {}

    evidence = analyze_freshness(series([1, 1, 1]), [])["f"]
    assert evidence["input"]["segment_count"] == 0
    assert evidence["input"]["sample_count"] == 0
    assert evidence["input"]["complete"] is False
    assert evidence["input"]["classification"] == "unknown"
    assert evidence["classification"] == "unknown"


def vec(components, kind="vector3") -> dict:
    return row(components, kind=kind)


def test_vector_fields_are_compared_component_by_component_with_exact_equality():
    still = analyze_freshness(series([vec([1.0, 2.0, 3.0]), vec([1.0, 2.0, 3.0]), vec([1.0, 2.0, 3.0])]), [])["f"]
    assert still["idle"]["comparable_intervals"] == 2
    assert still["idle"]["changed_intervals"] == 0
    assert still["idle"]["classification"] == "no_change_observed"

    nudged = [vec([0.0, 0.0, 0.0]), vec([0.0, 0.0, 1e-9]), vec([0.0, 0.0, 2e-9])]
    moving = analyze_freshness(series(nudged), [])["f"]["idle"]
    assert moving["changed_intervals"] == 2
    assert moving["classification"] == "observed_continuous"


def test_the_same_numbers_under_different_vector_kinds_are_different_values():
    same_numbers = [vec([1.0, 2.0, 3.0, 4.0], kind="vector4"), vec([1.0, 2.0, 3.0, 4.0], kind="quaternion")]
    assert analyze_freshness(series(same_numbers), [])["f"]["idle"]["changed_intervals"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        [1.0, 2.0],
        [1.0, 2.0, 3.0, 4.0],
        [1.0, True, 3.0],
        [1.0, math.nan, 3.0],
        [1.0, math.inf, 3.0],
        [1.0, "2", 3.0],
        "1,2,3",
    ],
)
def test_malformed_vector_values_are_unknown(bad):
    samples = series([vec([1.0, 2.0, 3.0]), vec(bad), vec([1.0, 2.0, 3.0])])
    evidence = analyze_freshness(samples, [])["f"]["idle"]
    assert evidence["comparable_intervals"] == 0
    assert evidence["changed_intervals"] == 0
    assert evidence["unknown_intervals"] == 2
    assert evidence["classification"] == "unknown"


def test_whole_number_vectors_reject_fractions_and_special_vectors_are_unknown():
    cells = analyze_freshness(series([vec([1, 2], kind="vector2_int")] * 3), [])["f"]["idle"]
    assert cells["comparable_intervals"] == 2
    assert cells["classification"] == "no_change_observed"

    fractional = [vec([1, 2], kind="vector2_int"), vec([1.5, 2], kind="vector2_int"), vec([1, 2], kind="vector2_int")]
    assert analyze_freshness(series(fractional), [])["f"]["idle"]["classification"] == "unknown"

    special = [vec([1.0, 2.0, 3.0]), row(["NaN", 2.0, 3.0], kind="special_vector"), vec([1.0, 2.0, 3.0])]
    assert analyze_freshness(series(special), [])["f"]["idle"]["classification"] == "unknown"


def test_held_changes_are_split_by_key_and_the_pooled_class_is_kept():
    windows = [series([0, 1, 2], start=10.0), series([0, 1, 2], start=20.0), series([9, 9, 9], start=30.0)]
    evidence = analyze_freshness(series([5, 5, 5]), windows, input_controls=["w", "w", "s"])["f"]

    assert evidence["held_controls"] == {"changed": ["w"], "unchanged": ["s"], "unknown": []}
    w = evidence["input_by_control"]["w"]
    assert w["windows"] == [0, 1]
    assert w["window_count"] == 2
    assert w["classification"] == "observed_continuous"
    assert w["windows_agree"] is True
    assert evidence["input_by_control"]["s"]["classification"] == "no_change_observed"
    assert evidence["input"]["classification"] == "observed_continuous"
    assert evidence["varied_between_windows"] == []


def test_a_key_whose_repeated_windows_disagree_is_named_and_not_averaged_away():
    windows = [series([0, 1, 2], start=0.0), series([4, 4, 4], start=10.0)]
    evidence = analyze_freshness(series([1, 1, 1]), windows, input_controls=["space", "space"])["f"]

    space = evidence["input_by_control"]["space"]
    assert space["windows_agree"] is False
    assert space["window_classifications"] == {"no_change_observed": 1, "observed_continuous": 1}
    assert evidence["varied_between_windows"] == [{"scope": "input", "control": "space"}]
    assert evidence["held_controls"]["changed"] == ["space"]


def test_a_key_with_no_comparable_windows_is_unknown_not_unchanged():
    unreadable = row(None, state="unsupported", kind="unsupported")
    windows = [series([unreadable, unreadable, unreadable]), series([3, 3, 3], start=10.0)]
    evidence = analyze_freshness(series([1, 1, 1]), windows, input_controls=["a", "d"])["f"]
    assert evidence["held_controls"] == {"changed": [], "unchanged": ["d"], "unknown": ["a"]}


def test_controls_are_none_when_absent_and_must_match_the_windows():
    evidence = analyze_freshness(series([1, 1, 1]), [series([0, 1, 2])])["f"]
    assert evidence["input_by_control"] is None
    assert evidence["held_controls"] is None
    assert evidence["varied_between_windows"] is None

    with pytest.raises(ValueError, match="input_controls"):
        analyze_freshness(series([1, 1, 1]), [series([0, 1])], input_controls=[])
    with pytest.raises(ValueError, match="input_controls"):
        analyze_freshness(series([1, 1, 1]), [series([0, 1])], input_controls=[""])


def test_later_idle_windows_are_separate_windows_that_can_disagree():
    evidence = analyze_freshness(series([0, 1, 2]), [], later_idle_windows=[series([5, 5, 5], start=100.0)])["f"]

    idle = evidence["idle"]
    assert idle["segment_count"] == 2
    assert idle["comparable_intervals"] == 4
    assert idle["changed_intervals"] == 2
    assert idle["consecutive_changed_intervals_max"] == 2
    assert idle["window_classifications"] == {"no_change_observed": 1, "observed_continuous": 1}
    assert idle["windows_agree"] is False
    assert evidence["varied_between_windows"] == [{"scope": "idle"}]
    assert evidence["classification"] == "idle"


def test_the_edge_between_idle_windows_is_not_an_interval():
    evidence = analyze_freshness(series([0, 0, 0]), [], later_idle_windows=[series([9, 9, 9], start=50.0)])["f"]
    assert evidence["idle"]["comparable_intervals"] == 4
    assert evidence["idle"]["changed_intervals"] == 0
    assert evidence["idle"]["windows_agree"] is True
    assert evidence["idle"]["classification"] == "no_change_observed"


def test_later_idle_window_timestamps_are_checked_on_their_own():
    bad = [{"real_time_s": 1.0, "fields": [row(0)]}, {"real_time_s": 1.0, "fields": [row(0)]}]
    with pytest.raises(ValueError, match="idle window 1.*strictly increase"):
        analyze_freshness(series([0, 1]), [], later_idle_windows=[bad])
    with pytest.raises(ValueError, match="later_idle_windows"):
        analyze_freshness(series([0, 1]), [], later_idle_windows="nope")
